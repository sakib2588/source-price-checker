"""Name and value hygiene for Source Checker rows.

Pure functions, no I/O. Used both when a crawl is written (so bad values never reach the results
file) and when results are served (so older result files display correctly without a recrawl).

What it guards against:
  - Shopify's placeholder variant title ("Default Title") and blank or dash values used as a name.
  - Error and challenge pages whose <title> is stored as if it were a product name.
  - Search-session query strings left on Maskura links.
  - Margins that only make sense if one of the two prices was misread.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ── Placeholder and junk titles ──────────────────────────────────────────────

_PLACEHOLDERS = {
    "default title", "default", "title", "untitled", "n/a", "na", "null", "none",
    "unknown", "tbc", "-", "--", "–", "—",
}

_JUNK_PATTERNS = [
    r"discover\s*\|\s*ebay",
    r"(?:page|item|product|listing)?\s*not\s+found",
    r"\d{3}\s*[-:]?\s*(?:not found|forbidden|service unavailable|bad gateway|"
    r"gateway time-?out|internal server error|access denied)",
    r"(?:error|oops!?)(?:\s*\d{3})?.*",
    r"access denied.*",
    r"just a moment\.*",
    r"robot check",
    r"attention required!?.*",
    r"(?:sign|log)\s*in",
    r"are you a (?:robot|human)\??",
    r"verify you are (?:a )?human.*",
    r"security check.*",
    r"(?:amazon\.co\.uk|amazon\.com|amazon|ebay\.co\.uk|ebay)",
]
_JUNK_RE = re.compile(r"^(?:" + "|".join(_JUNK_PATTERNS) + r")$", re.I)

_PLATFORM_SUFFIX_RE = re.compile(
    r"\s*[|:]\s*(?:ebay|amazon|etsy|argos|temu|aliexpress|wish)\b.*$", re.I)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _collapse(value) -> str:
    # Drop control characters first: \x0b and \x0c count as whitespace and would become a space.
    text = _CONTROL_RE.sub("", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def is_placeholder_title(value) -> bool:
    return _collapse(value).lower() in _PLACEHOLDERS or not _collapse(value)


def clean_title(value):
    """The title with whitespace collapsed, or None when it is a placeholder or an error-page title."""
    text = _collapse(value)
    if not text or is_placeholder_title(text) or _JUNK_RE.match(text):
        return None
    return text


# ── Readable names from URLs ─────────────────────────────────────────────────

_HANDLE_RE = re.compile(r"/products/([^/?#]+)")


def name_from_url(url):
    """'.../products/king-size-electric-blankets' -> 'King Size Electric Blankets', else None."""
    if not url:
        return None
    try:
        path = urlsplit(str(url)).path
    except ValueError:
        return None
    m = _HANDLE_RE.search(path)
    if not m:
        return None
    words = [w for w in re.split(r"[-_+\s]+", m.group(1)) if w]
    if not words:
        return None
    return " ".join(w[:1].upper() + w[1:] for w in words)


# ── Link cleanup ─────────────────────────────────────────────────────────────

_SESSION_PARAMS = {
    "_pos", "_psq", "_ss", "_v", "_sid", "_fid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid",
}


def strip_session_params(url):
    """Remove search-session and tracking parameters; keep everything that selects the product
    (for example `variant=`). Returns the input unchanged when there is nothing to remove."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in _SESSION_PARAMS]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


# ── Display name ─────────────────────────────────────────────────────────────

def display_name(row):
    """(name, origin) for a result row. Never a placeholder, never a bare dash.

    origin is one of: sheet, maskura, source, url, none.
    """
    name = clean_title(row.get("product_name"))
    if name:
        return name, "sheet"
    name = clean_title(row.get("maskura_title"))
    if name:
        return name, "maskura"
    for sd in row.get("sources") or []:
        # Reject error-page titles on the raw text first: stripping "| eBay" from
        # "Discover | eBay" would leave a plausible-looking "Discover".
        raw = clean_title(sd.get("source_title") or sd.get("title"))
        name = clean_title(_PLATFORM_SUFFIX_RE.sub("", raw)) if raw else None
        if name:
            return name, "source"
    name = name_from_url(row.get("maskura_url"))
    if name:
        return name, "url"
    return f"Unnamed product (row {row.get('row_id', '?')})", "none"


# ── Labels and reasons ───────────────────────────────────────────────────────

_PLATFORM_LABELS = {
    "ebay": "eBay", "amazon": "Amazon", "generic": "Other site", "maskura": "Maskura",
    "none": "No link", "aliexpress": "AliExpress", "temu": "Temu", "etsy": "Etsy",
    "argos": "Argos", "taobao": "Taobao", "1688": "1688", "pinduoduo": "Pinduoduo",
}


def platform_label(platform):
    key = (platform or "none").strip().lower()
    return _PLATFORM_LABELS.get(key, key[:1].upper() + key[1:])


_MARGIN_HIGH = 4.0
_MARGIN_LOW = 0.25
_MIN_SOURCE_PRICE = 0.50


def margin_suspect(maskura_price, source_price):
    """A plain-language reason when the two prices are so far apart that a misread is likelier
    than a real margin, else None."""
    try:
        m, s = float(maskura_price), float(source_price)
    except (TypeError, ValueError):
        return None
    if m <= 0 or s <= 0:
        return None
    if s < _MIN_SOURCE_PRICE:
        return f"Source price under £{_MIN_SOURCE_PRICE:.2f}, probably a misread"
    ratio = m / s
    if ratio > _MARGIN_HIGH:
        return f"Maskura is {ratio:.1f}x the source price (over {_MARGIN_HIGH:g}x); check the source price"
    if ratio < _MARGIN_LOW:
        return f"Maskura is only {ratio:.2f}x the source price (under {_MARGIN_LOW:g}x); check the link"
    return None


def no_price_reason(source):
    """Why a source has no price, in words a client can read. None when a price exists."""
    if source.get("price_gbp") is not None:
        return None
    verdict = str(source.get("verdict") or "").upper()
    error = str(source.get("error") or "")
    err = error.upper()
    avail_text = str(source.get("avail_text") or "").lower()
    if verdict == "NOT_CHECKED":
        return "Not checked yet"
    if not source.get("url") and (source.get("platform") in (None, "", "none")):
        return "No link in sheet"
    if verdict == "NON_GBP":
        return "Price is not in pounds"
    if verdict == "ENDED_REDIRECT":
        return "Listing ended or replaced by another item"
    if verdict == "NOT_FOUND" or "not found" in avail_text or "ended" in avail_text:
        return "Listing removed"
    if err.startswith("BLOCKED") or "CHALLENGE" in err or "CAPTCHA" in err:
        return "Site blocked the check"
    if err.startswith("NO_CONTENT"):
        return "Page showed no product details"
    if err.startswith("PARSER_GAP"):
        return "Price not found on page"
    if "TIMEOUT" in err or "TIMED OUT" in err:
        return "Page did not respond"
    if source.get("available") is False and not error:
        return "Out of stock, no price shown"
    if error:
        return "Could not read page"
    return "Price not found on page"


def maskura_status(row):
    """A label for rows where the Maskura side could not be read, else None."""
    err = str(row.get("maskura_error") or "").lower()
    if not err:
        return None
    if "404" in err or "not found" in err:
        return "Maskura link is broken (404)"
    if "not a maskura product" in err:
        return "Link is not a product page"
    if "not crawled" in err:
        return "Maskura price not checked yet"
    if "fetch failed" in err or "timeout" in err:
        return "Maskura page could not be reached"
    return "Maskura price could not be read"
