"""Generic source extractor for international / unknown supplier sites.

Uses only stdlib (re + html.parser) so it works without bs4/lxml.
Tries in order:
  1. JSON-LD Product schema (including @graph wrappers, offer lists and AggregateOffer)
  2. Open Graph price meta tags
  3. Schema.org microdata (itemprop=price)
  4. Heuristic regex over common price CSS patterns (skips struck-through / "was" prices)

Results are always flagged as uncertain so the analyst knows to verify manually. Availability
is attempted from JSON-LD and OG meta; falls back to None (unknown) rather than guessing.

A price in a currency we have no rate for is reported as "not found" with the currency named,
never passed off as GBP.

Encodings: the charset declared by the HTTP header or a <meta> tag wins; otherwise UTF-8, then
GBK / Big5 / Shift-JIS / EUC-KR (Chinese, Japanese and Korean suppliers).
"""
from __future__ import annotations

import codecs
import json
import re
import warnings
from datetime import date
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .base import SourceResult

# Pages bigger than this are cut before parsing; nothing useful sits past the first few MB.
MAX_HTML_CHARS = 4_000_000

_GBP_RATE = {
    "usd": 0.79, "eur": 0.86, "cny": 0.11,
    "uah": 0.019, "jpy": 0.0052, "krw": 0.00059,
}
RATES_AS_OF = "2026-10-15"   # update this whenever _GBP_RATE is refreshed
_SYMBOL_CODE = {"£": "gbp", "€": "eur", "$": "usd", "US$": "usd"}
_YEN_SYMBOLS = ("¥", "￥")
_ALIASES = {"rmb": "cny", "yuan": "cny", "yen": "jpy", "won": "krw"}

_RATES_DATE = date.fromisoformat(RATES_AS_OF)
_RATES_AGE_DAYS = (date.today() - _RATES_DATE).days
if _RATES_AGE_DAYS > 30:
    warnings.warn(
        f"generic.py exchange rates are {_RATES_AGE_DAYS} days old (as of {RATES_AS_OF}). "
        "Update _GBP_RATE and RATES_AS_OF in sources/generic.py.",
        UserWarning,
        stacklevel=1,
    )

UNCERTAIN_NOTE = f"Generic extractor — verify manually (rates as of {RATES_AS_OF})"

# Tokens in a class attribute that mean "this is not the current price".
_NOT_CURRENT = {"old", "was", "before", "compare", "regular", "strike", "strikethrough", "original",
                "list", "msrp", "rrp", "del", "previous", "crossed", "retail", "from"}
_STRUCK_TAGS = {"s", "del", "strike"}

_CLASS_ATTR_RE = re.compile(r'class\s*=\s*["\']([^"\']{0,200})["\']', re.I)
_PRICE_AFTER_RE = re.compile(
    r"\s*(?:<[^<>]{0,200}>\s*){0,3}"
    r"(?P<sym>US\$|[£$€¥￥]|CNY|RMB|USD|GBP|EUR)?\s*(?P<num>\d[\d.,]{0,15})",
    re.I)
_TAG_BEFORE_RE = re.compile(r"<([a-z0-9]+)[^<>]{0,400}$", re.I)
_CHARSET_SNIFF_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", re.I)
_ITEMPROP_PRICE_RES = (
    re.compile(r'itemprop\s*=\s*["\']price["\'][^<>]{0,300}content\s*=\s*["\']([^"\']{1,40})["\']', re.I),
    re.compile(r'content\s*=\s*["\']([^"\']{1,40})["\'][^<>]{0,300}itemprop\s*=\s*["\']price["\']', re.I),
)
_ITEMPROP_CUR_RES = (
    re.compile(r'itemprop\s*=\s*["\']priceCurrency["\'][^<>]{0,300}content\s*=\s*["\']([^"\']{1,10})["\']', re.I),
    re.compile(r'content\s*=\s*["\']([^"\']{1,10})["\'][^<>]{0,300}itemprop\s*=\s*["\']priceCurrency["\']', re.I),
)


def _lookup_codec(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = name.strip().strip("\"'").lower()
    if name in ("gb2312", "gb_2312", "gb-2312"):
        name = "gb18030"          # superset; real GB2312 pages often use GBK-only characters
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _sniff_meta_charset(raw: bytes) -> Optional[str]:
    m = _CHARSET_SNIFF_RE.search(raw[:4096])
    return m.group(1).decode("ascii", "ignore") if m else None


def _decode(raw: bytes, declared: Optional[str] = None) -> str:
    """Decode a page: BOM, then header charset, then <meta> charset, then UTF-8, then CJK guesses."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    candidates: list[str] = []
    for name in (declared, _sniff_meta_charset(raw)):
        codec = _lookup_codec(name)
        if codec and codec not in candidates:
            candidates.append(codec)
    candidates += ["utf-8", "gb18030", "big5", "shift_jis", "euc_kr"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_price(text) -> Optional[float]:
    """First positive number in `text`, handling 1,299.00 / 1.299,00 / 12,50 / 1 299.

    Returns None for no digits, a leading minus sign, or zero.
    """
    s = str(text).replace("，", ",").replace("\xa0", " ").strip()
    if re.match(r"^[-−–]\s*[£$€¥￥]?\s*\d", s):
        return None
    m = re.search(r"\d[\d.,\s]*\d|\d", s)
    if not m:
        return None
    num = re.sub(r"\s+", "", m.group())
    if "," in num and "." in num:
        # whichever separator comes last is the decimal point
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        if num.count(",") == 1 and 1 <= len(tail) <= 2 and head:
            num = f"{head}.{tail}"          # 12,50 -> 12.50
        else:
            num = num.replace(",", "")      # 1,299 / 1,299,000
    elif num.count(".") > 1:
        head, _, tail = num.rpartition(".")
        num = head.replace(".", "") + "." + tail if len(tail) <= 2 else num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return value if value > 0 else None


def _currency_code(raw, url: str = "") -> Optional[str]:
    """Normalize a currency symbol or code. None = not stated; "?" = stated but not one we can convert."""
    if raw is None:
        return None
    c = str(raw).strip()
    if not c:
        return None
    if c in _SYMBOL_CODE:
        return _SYMBOL_CODE[c]
    if c in _YEN_SYMBOLS:
        host = (urlsplit(url if "//" in url else "//" + url).hostname or "").lower()
        return "jpy" if host.endswith(".jp") else "cny"
    code = _ALIASES.get(c.lower(), c.lower())
    return code if code == "gbp" or code in _GBP_RATE else "?"


def _leading_symbol(raw) -> Optional[str]:
    m = re.match(r"\s*(US\$|[£$€¥￥])", str(raw))
    return m.group(1) if m else None


def _to_gbp(value: float, code: str) -> Optional[float]:
    if code == "gbp":
        return round(value, 2)
    rate = _GBP_RATE.get(code)
    return round(value * rate, 2) if rate else None


class _MetaParser(HTMLParser):
    """Collects <meta> tag attributes, <script type=application/ld+json> bodies and <title>."""

    def __init__(self):
        super().__init__()
        self.metas: list[dict] = []
        self.ld_jsons: list[str] = []
        self._in_ld = False
        self._buf = ""
        self.title: str = ""
        self._in_title = False
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            self.metas.append(a)
        elif tag == "script" and a.get("type", "").split(";")[0].strip().lower() == "application/ld+json":
            self._in_ld = True
            self._buf = ""
        elif tag == "title" and not self._title_done:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld_jsons.append(self._buf)
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data):
        if self._in_ld:
            self._buf += data
        elif self._in_title:
            self.title += data


def _meta_val(metas: list[dict], *props) -> Optional[str]:
    for meta in metas:
        name = meta.get("property") or meta.get("name") or ""
        if name.lower() in props:
            return meta.get("content")
    return None


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return {str(x).rsplit("/", 1)[-1] for x in items if x}


def _walk(data, depth: int = 0) -> Iterable[dict]:
    """Every dict reachable through lists, @graph and nested values (bounded depth)."""
    if depth > 6:
        return
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item, depth + 1)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _offers_of(node: dict) -> list[dict]:
    out: list[dict] = []
    for o in _as_list(node.get("offers")):
        if isinstance(o, dict):
            out.append(o)
            out.extend(n for n in _as_list(o.get("offers")) if isinstance(n, dict))
    return out


def _availability(offer: dict) -> Optional[bool]:
    avail = str(offer.get("availability") or offer.get("itemAvailability") or "").lower()
    if "instock" in avail or "instore" in avail or "limitedavailability" in avail:
        return True
    if "outofstock" in avail or "discontinued" in avail or "soldout" in avail:
        return False
    return None


def _json_ld(raws: list[str]):
    """-> (title, price, currency_raw, available). Price is the raw number, not yet converted."""
    title = None
    price = currency = available = None
    for raw in raws:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for node in _walk(data):
            kinds = _types(node)
            is_product = "Product" in kinds
            is_offer = bool(kinds & {"Offer", "AggregateOffer"})
            if not (is_product or is_offer or "offers" in node):
                continue
            if is_product and not title and isinstance(node.get("name"), str):
                title = node["name"].strip() or None
            for offer in ([node] if is_offer else []) + _offers_of(node):
                if price is not None:
                    break
                spec = _as_list(offer.get("priceSpecification"))[0]
                spec = spec if isinstance(spec, dict) else {}
                raw_price = offer.get("price")
                if raw_price in (None, ""):
                    raw_price = offer.get("lowPrice")
                if raw_price in (None, ""):
                    raw_price = spec.get("price")
                value = _parse_price(raw_price) if raw_price not in (None, "") else None
                if value is not None:
                    price = value
                    currency = (offer.get("priceCurrency") or spec.get("priceCurrency")
                                or node.get("priceCurrency") or _leading_symbol(raw_price))
                    available = _availability(offer)
            if price is not None and available is None:
                for offer in _offers_of(node):
                    available = _availability(offer)
                    if available is not None:
                        break
        if price is not None:
            break
    return title, price, currency, available


def _heuristic_price(text: str, url: str):
    """Last resort: price-looking class names, data attributes and inline JSON. -> (value, code)."""
    candidates = 0
    for m in _CLASS_ATTR_RE.finditer(text):
        tokens = set(re.findall(r"[a-z0-9]+", m.group(1).lower()))
        if "price" not in tokens or tokens & _NOT_CURRENT:
            continue
        candidates += 1
        if candidates > 40:
            break
        tag = _TAG_BEFORE_RE.search(text[max(0, m.start() - 420):m.start()])
        if tag and tag.group(1).lower() in _STRUCK_TAGS:
            continue
        gt = text.find(">", m.end(), m.end() + 400)
        if gt < 0:
            continue
        pm = _PRICE_AFTER_RE.match(text, gt + 1)
        if pm:
            value = _parse_price(pm.group("num"))
            if value is not None:
                return value, _currency_code(pm.group("sym"), url)
    for pat in (r'data-price\s*=\s*["\']([0-9][0-9.,]{0,15})["\']',
                r'data-raw-price\s*=\s*["\']([0-9][0-9.,]{0,15})["\']',
                r'"price"\s*:\s*"([0-9][0-9.,]{0,15})"',
                r'"price"\s*:\s*([0-9][0-9.]{0,15})'):
        m = re.search(pat, text, re.I)
        if m:
            value = _parse_price(m.group(1))
            if value is not None:
                return value, None
    return None, None


def extract(url: str, html_bytes, declared_charset: Optional[str] = None) -> SourceResult:
    result = SourceResult(url=url, platform="generic")

    if isinstance(html_bytes, str):
        text = html_bytes
    elif isinstance(html_bytes, (bytes, bytearray)):
        text = _decode(bytes(html_bytes[:MAX_HTML_CHARS * 2]), declared_charset)
    else:
        text = ""
    text = text[:MAX_HTML_CHARS]

    parser = _MetaParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass

    ld_title, price, cur_raw, available = _json_ld(parser.ld_jsons)
    result.title = parser.title.strip() or _meta_val(parser.metas, "og:title") or ld_title or None
    code = _currency_code(cur_raw, url) if price is not None else None

    # 2 — Open Graph / meta
    if price is None:
        raw = _meta_val(parser.metas, "product:price:amount", "og:price:amount", "twitter:data1", "price")
        value = _parse_price(raw) if raw else None
        if value is not None:
            price = value
            cur_raw = (_meta_val(parser.metas, "product:price:currency", "og:price:currency", "price:currency")
                       or _leading_symbol(raw))
            code = _currency_code(cur_raw, url)

    # 3 — itemprop=price microdata
    if price is None:
        m = next((r.search(text) for r in _ITEMPROP_PRICE_RES if r.search(text)), None)
        value = _parse_price(m.group(1)) if m else None
        if value is not None:
            price = value
            cm = next((r.search(text) for r in _ITEMPROP_CUR_RES if r.search(text)), None)
            cur_raw = cm.group(1) if cm else None
            code = _currency_code(cur_raw, url)

    # 4 — heuristics
    if price is None:
        price, code = _heuristic_price(text, url)

    problem = ""
    price_gbp: Optional[float] = None
    if price is not None:
        if code == "?":
            problem = f"unrecognized currency {str(cur_raw).strip()[:12]}, price not converted"
        else:
            price_gbp = _to_gbp(price, code or "gbp")   # nothing stated: assume GBP, stays flagged

    result.price_gbp = price_gbp
    result.available = available
    result.availability_text = "unknown" if available is None else ("In stock" if available else "Out of stock")

    if price_gbp is None and available is None:
        result.error = "Generic extractor: " + (problem or "no price found on page")
    else:
        result.error = UNCERTAIN_NOTE
    return result
