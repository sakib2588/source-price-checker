"""Variation readers for supplier pages other than eBay: one reader per site type.

Every reader returns the same shape as the eBay extractor, [{id, label, price_gbp, available}], so
sc_variants compares them with Maskura's variants the same way. A reader answers only when it finds two
or more variations; otherwise it returns [] and the page is read as a single-price product.

  shopify      the product JSON a Shopify theme embeds (variants with price in pence and `available`)
  woocommerce  the `data-product_variations` attribute on the add-to-cart form
  jsonld       a schema.org ProductGroup (`hasVariant`) or a Product with several priced Offers
  amazon       no sibling prices are on an Amazon page: only the label of the linked ASIN

`read(url, text)` tries the first three in order. `pick_linked(url, offers)` finds the variation the
link itself names. Pure functions, no I/O.
"""
from __future__ import annotations
import html as _html
import json
import re
from urllib.parse import parse_qs, urlsplit

_DECODER = json.JSONDecoder()
_PRODUCT_JSON_RE = re.compile(
    r'<script[^>]+(?:data-product-json|id="ProductJson[^"]*"|data-product-variants)[^>]*>(.*?)</script>', re.S | re.I)
_WOO_RE = re.compile(r'data-product_variations=(["\'])(.*?)\1', re.S)
_LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)
_SKIP_LABELS = {"default title", "default", "title"}


def _price(value):
    try:
        return float(str(value).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return None


def _offer(vid, label, price, available):
    return {"id": str(vid or ""), "label": label.strip(), "price_gbp": price, "available": available}


def _enough(offers):
    return offers if len(offers) >= 2 else []


# ── Shopify ──────────────────────────────────────────────────────────────────

def _shopify_price(value):
    """Pence as an int in theme JSON, pounds as text in the .json endpoint, {amount, currencyCode} in pounds
    in the analytics payload (a currency other than GBP is not a pound price)."""
    if isinstance(value, dict):
        if str(value.get("currencyCode") or "GBP").upper() != "GBP":
            return None
        return _price(value.get("amount"))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return round(value / 100, 2)
    if isinstance(value, float):
        return round(value, 2)
    return _price(value)


def _handle(url):
    m = re.search(r"/products/([^/?#]+)", urlsplit(str(url or "")).path)
    return m.group(1).lower() if m else ""


def _shopify_variants(text, url=""):
    for m in _PRODUCT_JSON_RE.finditer(text):
        try:
            data = json.loads(_html.unescape(m.group(1)))
        except ValueError:
            continue
        data = data.get("product", data) if isinstance(data, dict) else data
        if isinstance(data, dict) and isinstance(data.get("variants"), list):
            return data["variants"]
    # A theme repeats `"variants":[` for recommended products too: take the list that belongs to the
    # product in the link (it names the variation, or the product's handle sits just before the list).
    handle = _handle(url)
    want = (parse_qs(urlsplit(str(url or "")).query).get("variant") or [None])[0]
    first = None
    i = text.find('"variants":[')
    while i >= 0:
        try:
            value = _DECODER.raw_decode(text[i + len('"variants":'):])[0]
        except ValueError:
            value = None
        if isinstance(value, list) and value and all(isinstance(v, dict) and "id" in v and "price" in v for v in value):
            if want and any(str(v["id"]) == want for v in value):
                return value
            if handle and f'"handle":"{handle}"' in text[max(0, i - 3000):i].lower():
                return value
            first = first or value
        i = text.find('"variants":[', i + 1)
    return [] if handle or want else (first or [])


def shopify(text, url=""):
    out = []
    for v in _shopify_variants(text, url):
        label = v.get("public_title") or " / ".join(str(v.get(k)) for k in ("option1", "option2", "option3") if v.get(k)) \
            or v.get("title") or ""
        if str(label).strip().lower() in _SKIP_LABELS:
            label = ""
        avail = v.get("available")
        out.append(_offer(v.get("id"), str(label), _shopify_price(v.get("price")), avail if isinstance(avail, bool) else None))
    return _enough(out)


# ── WooCommerce ──────────────────────────────────────────────────────────────

def woocommerce(text):
    m = _WOO_RE.search(text)
    if not m:
        return []
    try:
        rows = json.loads(_html.unescape(m.group(2)))
    except ValueError:
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        attrs = r.get("attributes") or {}
        label = " / ".join(str(v).replace("-", " ") for v in attrs.values() if v)
        stock = r.get("is_in_stock")
        out.append(_offer(r.get("variation_id"), label, _price(r.get("display_price")),
                          stock if isinstance(stock, bool) else None))
    return _enough(out)


# ── schema.org JSON-LD ───────────────────────────────────────────────────────

def _ld_nodes(text):
    for m in _LD_RE.finditer(text):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])


def _as_list(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


def _ld_offer(node, group_name):
    offers = [o for o in _as_list(node.get("offers")) if isinstance(o, dict)]
    if not offers:
        return None
    o = offers[0]
    cur = str(o.get("priceCurrency") or "GBP").upper()
    price = _price(o.get("price")) if cur == "GBP" else None
    avail = str(o.get("availability") or "")
    stock = True if "InStock" in avail else (False if "OutOfStock" in avail or "SoldOut" in avail else None)
    name = str(node.get("name") or "")
    if group_name and name.lower().startswith(group_name.lower()):
        name = name[len(group_name):].lstrip(" -:/|")
    return _offer(node.get("sku") or node.get("@id") or o.get("sku") or "", name, price, stock)


def jsonld(text):
    for node in _ld_nodes(text):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "ProductGroup" in kinds:
            name = str(node.get("name") or "")
            out = [_ld_offer(v, name) for v in _as_list(node.get("hasVariant")) if isinstance(v, dict)]
            return _enough([o for o in out if o])
        if "Product" in kinds:
            offers = [o for o in _as_list(node.get("offers")) if isinstance(o, dict) and o.get("price") is not None]
            if len(offers) >= 2:
                out = [_ld_offer({"name": o.get("name") or o.get("sku") or "", "sku": o.get("sku"),
                                  "offers": [o]}, "") for o in offers]
                return _enough([o for o in out if o])
    return []


def read(url, text):
    """Offers for the first site type that finds two or more variations; [] when none does."""
    for reader in (lambda t: shopify(t, url), woocommerce, jsonld):
        offers = reader(text)
        if offers:
            return offers
    return []


# ── the variation a link names ───────────────────────────────────────────────

def pick_linked(url, offers):
    """The offer the link names (?variant=, ?variation_id=, ?var=, or WooCommerce attribute_* values), else None."""
    q = parse_qs(urlsplit(str(url or "")).query)
    for key in ("variant", "variation_id", "var"):
        want = (q.get(key) or [None])[0]
        if want:
            hit = next((o for o in offers if o["id"] == want), None)
            if hit:
                return hit
    attrs = [str(v[0]).replace("-", " ").lower() for k, v in q.items() if k.startswith("attribute_") and v and v[0]]
    if attrs:
        hits = [o for o in offers if all(a in o["label"].lower() for a in attrs)]
        if len(hits) == 1:
            return hits[0]
    return None


# ── Amazon ───────────────────────────────────────────────────────────────────

def amazon_label(url, text):
    """The linked ASIN's variation, e.g. "Red / Large", from the page's dimensionValuesDisplayData; else None."""
    m = _ASIN_RE.search(urlsplit(str(url or "")).path)
    i = text.find('"dimensionValuesDisplayData"')
    if not m or i < 0:
        return None
    try:
        data = _DECODER.raw_decode(text[text.find(":", i) + 1:].lstrip())[0]
    except ValueError:
        return None
    values = data.get(m.group(1).upper()) if isinstance(data, dict) else None
    label = " / ".join(str(v) for v in values if v) if isinstance(values, list) else ""
    return label or None
