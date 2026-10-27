"""eBay Browse API lookup: the official, keyed way to read an eBay UK listing.

Reads title, price (GBP) and stock from api.ebay.com instead of scraping the item page, so it is fast
and never meets a bot check. It answers only when the API gave a clean hit; for everything else it
returns None and the caller scrapes the page as before (ended, removed or non-GBP listings, a
rate-limited or unconfigured key), so those verdicts do not change.

Keys come from the environment: EBAY_APP_ID and EBAY_CERT_ID (the production keyset's Client ID and
Client Secret). EBAY_API_DISABLE=1 turns it off; EBAY_API_DAILY_CAP (default 4500) stops it before
eBay's 5,000 calls a day limit and EBAY_API_MIN_GAP_S (default 0.2) spaces calls across all threads.
"""
from __future__ import annotations
import base64
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
import httpx
from .base import SourceResult

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_GB"
ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")
ITEM_GROUP_ERROR = 11006
TOKEN_MARGIN_S = 300.0
BACKOFF_S = 300.0
TIMEOUT_S = 15.0
IN_STOCK = {"IN_STOCK", "LIMITED_STOCK"}
OUT_OF_STOCK = {"OUT_OF_STOCK"}


def _utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _price_gbp(item):
    price = item.get("price") or {}
    if str(price.get("currency") or "").upper() != "GBP":
        return None
    try:
        return float(price.get("value"))
    except (TypeError, ValueError):
        return None


def _available(item):
    for est in item.get("estimatedAvailabilities") or []:
        status = str(est.get("estimatedAvailabilityStatus") or "").upper()
        if status in IN_STOCK:
            return True
        if status in OUT_OF_STOCK:
            return False
    return None


def _stock_text(available):
    return "" if available is None else ("In Stock" if available else "Out of Stock")


def _variation_id(item):
    parts = str(item.get("itemId") or "").split("|")
    return parts[2] if len(parts) == 3 and parts[2] != "0" else ""


def _aspects(item):
    return [(a.get("name"), a.get("value")) for a in item.get("localizedAspects") or []
            if a.get("name") and a.get("value") is not None]


def _group_offers(items):
    """[{id, label, price_gbp, available}] for an item group; the label joins the aspects that differ."""
    values = {}
    for it in items:
        for name, value in _aspects(it):
            values.setdefault(name, set()).add(str(value))
    varying = [n for n, v in values.items() if len(v) > 1]
    offers = []
    for it in items:
        have = dict(_aspects(it))
        offers.append({"id": _variation_id(it),
                       "label": " / ".join(str(have[n]) for n in varying if n in have),
                       "price_gbp": _price_gbp(it), "available": _available(it)})
    return offers


class EbayApi:
    def __init__(self, app_id, cert_id, *, client=None, clock=time.monotonic, sleep=time.sleep,
                 min_gap_s=0.2, daily_cap=4500):
        self._basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        self._client = client or httpx.Client(timeout=TIMEOUT_S, trust_env=False)
        self._clock, self._sleep = clock, sleep
        self._min_gap_s, self._daily_cap = float(min_gap_s), int(daily_cap)
        self._lock = threading.Lock()
        self._token, self._token_until = None, 0.0
        self._next_call, self._off_until = 0.0, 0.0
        self._day, self._calls = _utc_day(), 0

    # ── pacing and budget ────────────────────────────────────────────────────

    def _reserve(self):
        """Reserve one call slot; the seconds to wait before it, or None when the API must not be used now."""
        with self._lock:
            now = self._clock()
            if now < self._off_until:
                return None
            day = _utc_day()
            if day != self._day:
                self._day, self._calls = day, 0
            if self._calls >= self._daily_cap:
                return None
            self._calls += 1
            at = max(now, self._next_call)
            self._next_call = at + self._min_gap_s
            return at - now

    def _back_off(self):
        with self._lock:
            self._off_until = self._clock() + BACKOFF_S

    @property
    def calls_today(self):
        with self._lock:
            return self._calls if self._day == _utc_day() else 0

    # ── requests ─────────────────────────────────────────────────────────────

    def _bearer(self):
        with self._lock:
            if self._token and self._clock() < self._token_until:
                return self._token
        r = self._client.post(TOKEN_URL, data={"grant_type": "client_credentials", "scope": SCOPE},
                              headers={"Authorization": f"Basic {self._basic}"})
        r.raise_for_status()
        body = r.json()
        with self._lock:
            self._token = body["access_token"]
            self._token_until = self._clock() + float(body.get("expires_in", 7200)) - TOKEN_MARGIN_S
            return self._token

    def _forget_token(self):
        with self._lock:
            self._token, self._token_until = None, 0.0

    def _get(self, path, params):
        """(status, json) for one Browse call, or None when the API must be skipped for this link."""
        wait = self._reserve()
        if wait is None:
            return None
        if wait > 0:
            self._sleep(wait)
        try:
            for attempt in (0, 1):
                r = self._client.get(f"{BROWSE_URL}/{path}", params=params,
                                     headers={"Authorization": f"Bearer {self._bearer()}",
                                              "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE})
                if r.status_code == 401 and attempt == 0:
                    self._forget_token()
                    continue
                break
        except (httpx.HTTPError, ValueError, KeyError):
            self._back_off()
            return None
        if r.status_code in (429, 503) or (r.status_code == 403 and "rate" in r.text.lower()):
            self._back_off()
            return None
        try:
            return r.status_code, r.json()
        except ValueError:
            return None

    # ── lookup ───────────────────────────────────────────────────────────────

    def lookup(self, url):
        """A SourceResult for an eBay UK item link, or None to let the caller scrape the page."""
        parts = urlsplit(str(url or ""))
        m = ITEM_RE.search(parts.path)
        if not m or not (parts.hostname or "").lower().endswith("ebay.co.uk"):
            return None
        item_id = m.group(1)
        var = (parse_qs(parts.query).get("var") or [None])[0]
        if var:
            return self._group(url, item_id, var)
        got = self._get("get_item_by_legacy_id", {"legacy_item_id": item_id})
        if got is None:
            return None
        status, body = got
        if status == 200:
            return self._single(url, body)
        errors = body.get("errors") if isinstance(body, dict) else None
        if status == 400 and any(e.get("errorId") == ITEM_GROUP_ERROR for e in errors or []):
            return self._group(url, item_id, None)
        return None

    @staticmethod
    def _single(url, item):
        price = _price_gbp(item)
        if not item.get("title") or price is None:
            return None
        available = _available(item)
        return SourceResult(url=url, platform="ebay", title=item["title"], price_gbp=price, available=available,
                            availability_text=_stock_text(available), note="ebay api")

    def _group(self, url, item_id, var):
        got = self._get("get_items_by_item_group", {"item_group_id": item_id})
        if got is None or got[0] != 200:
            return None
        items = got[1].get("items") if isinstance(got[1], dict) else None
        if not items or not items[0].get("title"):
            return None
        offers = _group_offers(items)
        res = SourceResult(url=url, platform="ebay", title=items[0]["title"], offers=offers, note="ebay api")
        if var:
            chosen = next((o for o in offers if o["id"] == var), None)
            if chosen is None or chosen["price_gbp"] is None:
                return None
            res.variation_label = chosen["label"]
            res.price_gbp, res.available = chosen["price_gbp"], chosen["available"]
        else:
            priced = [o for o in offers if o["price_gbp"] is not None]
            if not priced:
                return None
            stocked = [o for o in priced if o["available"]]
            res.price_gbp = min(o["price_gbp"] for o in (stocked or priced))
            known = [o["available"] for o in offers if o["available"] is not None]
            res.available = any(known) if known else None
        res.availability_text = _stock_text(res.available)
        return res


_default = None
_default_lock = threading.Lock()


def default():
    """The shared client built from the environment, or None when no keys are set or it is disabled."""
    global _default
    if os.environ.get("EBAY_API_DISABLE") == "1":
        return None
    app_id, cert_id = os.environ.get("EBAY_APP_ID"), os.environ.get("EBAY_CERT_ID")
    if not app_id or not cert_id:
        return None
    with _default_lock:
        if _default is None:
            _default = EbayApi(app_id, cert_id,
                               min_gap_s=float(os.environ.get("EBAY_API_MIN_GAP_S", "0.2")),
                               daily_cap=int(os.environ.get("EBAY_API_DAILY_CAP", "4500")))
        return _default


def lookup(url):
    """Module-level entry used by fetch_source; never raises."""
    api = default()
    if api is None:
        return None
    try:
        return api.lookup(url)
    except Exception:  # noqa: BLE001 - any API trouble means: scrape the page instead
        return None
