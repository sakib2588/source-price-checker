"""URL helpers for the Source Checker. Pure functions, no network.

- `normalize_maskura_url(raw)`: one Maskura product link per sheet cell, or a plain reason why not.
- `canonical_source_id(url)` and `source_signature(urls)`: stable ids for supplier links (eBay item id,
  Amazon ASIN with its region, otherwise host plus path) used to tell apart rows that share a handle.
- `host_of(url)`, `detect_platform(url)` and `normalize_source_url(value)`: moved from app.py
  (`_sc_detect_platform`, `_sc_normalize_url`) without behavior changes.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

MASKURA_HOSTS = frozenset({"maskura.co.uk", "www.maskura.co.uk"})
MASKURA_BASE = "https://www.maskura.co.uk"

_PRODUCT_PATH_RE = re.compile(r"^/(?:collections/[^/]+/)?products/([^/]+)/?$", re.I)
_HANDLE_OK_RE = re.compile(r"^[a-z0-9][a-z0-9._~%-]*$")
_DIGITS_RE = re.compile(r"^\d{1,20}$")
_OTHER_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

EBAY_ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")
AMAZON_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)


def _fail(raw: str, reason: str) -> dict:
    return {"url": None, "handle": None, "variant_id": None, "raw": raw, "ok": False, "reason": reason}


def normalize_maskura_url(raw) -> dict:
    """The canonical Maskura product URL for a sheet cell.

    Adds https:// and www., rewrites /collections/<x>/products/<y> to /products/<y>, lowercases the
    host and handle, drops the fragment and trailing slash, and keeps only a numeric `variant` query
    parameter. Only maskura.co.uk and www.maskura.co.uk are allowed.
    """
    text = "" if raw is None else str(raw)
    v = text.strip()
    if not v:
        return _fail(text, "The cell is empty")
    if any(c.isspace() for c in v):
        return _fail(text, "The cell holds text, not a link")
    if "://" in v:
        if not re.match(r"^https?://", v, re.I):
            return _fail(text, "Not a web link")
    elif _OTHER_SCHEME_RE.match(v) and not re.match(r"^[a-z0-9.-]+:\d+(/|$)", v, re.I):
        return _fail(text, "Not a web link")
    else:
        v = "https://" + v
    try:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return _fail(text, "Not a valid link")
    if parts.username is not None or parts.password is not None:
        return _fail(text, "Not a Maskura link")
    if host not in MASKURA_HOSTS:
        return _fail(text, "Not a Maskura link")
    if port not in (None, 80, 443):
        return _fail(text, "Not a Maskura link")
    m = _PRODUCT_PATH_RE.match(parts.path or "")
    if not m:
        return _fail(text, "Not a Maskura product page")
    handle = m.group(1).lower()
    if not _HANDLE_OK_RE.match(handle):
        return _fail(text, "Not a Maskura product page")
    variant = None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() == "variant" and _DIGITS_RE.match(value.strip()):
            variant = value.strip()
    url = f"{MASKURA_BASE}/products/{handle}" + (f"?variant={variant}" if variant else "")
    return {"url": url, "handle": handle, "variant_id": variant, "raw": text, "ok": True, "reason": None}


def maskura_url(handle: str, variant_id=None) -> str:
    """The canonical product URL for a handle (and optional variant)."""
    url = f"{MASKURA_BASE}/products/{str(handle).lower()}"
    return url + (f"?variant={variant_id}" if variant_id not in (None, "") else "")


def _split(url):
    u = str(url or "").strip()
    if not u:
        return None
    try:
        return urlsplit(u if "//" in u else "//" + u)
    except ValueError:
        return None


def host_of(url) -> str:
    """The lowercase host of a URL (scheme optional), or ""."""
    parts = _split(url)
    if parts is None:
        return ""
    try:
        return (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def canonical_source_id(url) -> str:
    """ebay:<item>, <amazon host>:<ASIN>, or <host>/<path>. Always lowercase; "" for a blank URL."""
    parts = _split(url)
    if parts is None:
        return ""
    host = host_of(url)
    path = parts.path or ""
    platform = detect_platform(url)
    if platform == "ebay":
        m = EBAY_ITEM_RE.search(path)
        if m:
            return f"ebay:{m.group(1)}".lower()
    if platform == "amazon":
        m = AMAZON_ASIN_RE.search(path)
        if m:
            region = host[4:] if host.startswith("www.") else host
            return f"{region}:{m.group(1)}".lower()
    return f"{host}/{path.strip('/')}".lower()


def source_signature(urls) -> str:
    """The canonical ids of the non-empty URLs, sorted and joined with '|'."""
    ids = (canonical_source_id(x) for x in (urls or []) if x and str(x).strip())
    return "|".join(sorted(i for i in ids if i))


# ── Moved from app.py ─────────────────────────────────────────────────────────

_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def normalize_source_url(value):
    """An http(s) URL for a sheet cell, or None if the cell is blank or not a link (was _sc_normalize_url).

    Repairs a missing scheme ('www.ebay.co.uk/itm/1'); rejects placeholders ('N/A', '-', 'TBC'),
    text with spaces, and non-web schemes.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v or any(c.isspace() for c in v):
        return None
    try:
        if "://" in v:
            if not re.match(r"^https?://", v, re.I):
                return None
            host = urlsplit(v).hostname or ""
            return v if _HOST_RE.match(host.lower()) and not host.rsplit(".", 1)[-1].isdigit() else None
        if re.match(r"^(javascript|mailto|data|file|tel|about|blob):", v, re.I):
            return None
        host = (urlsplit("https://" + v).hostname or "").lower()
    except ValueError:
        return None
    if _HOST_RE.match(host) and host.rsplit(".", 1)[-1].isalpha():
        return "https://" + v
    return None


_BRANDS = {
    "ebay": "ebay", "amazon": "amazon",
    # Chinese / Asian supplier platforms
    "aliexpress": "aliexpress", "alibaba": "alibaba", "dhgate": "dhgate", "made-in-china": "made-in-china",
    "1688": "1688", "taobao": "taobao", "tmall": "taobao", "jd": "jd",
    "pinduoduo": "pinduoduo", "yangkeduo": "pinduoduo",
    # Other international
    "walmart": "walmart", "etsy": "etsy", "wish": "wish",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}


def detect_platform(url) -> str:
    """Platform from the registered domain (ebay.co.uk -> ebay), never from words in the path or query."""
    u = str(url or "").strip()
    if not u:
        return "generic"
    try:
        host = (urlsplit(u if "//" in u else "//" + u).hostname or "").lower().rstrip(".")
    except ValueError:
        return "generic"
    if "maskura" in host:
        return "maskura"
    labels = host.split(".")
    if len(labels) < 2:
        return "generic"
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        label = labels[-3]
    else:
        label = labels[-2]
    return _BRANDS.get(label, "generic")
