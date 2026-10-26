"""eBay UK product extractor.

Scrapes: title, price, and stock status from an eBay item page.
Uses JSON-LD first (fast + stable), falls back to DOM selectors.
"""
from __future__ import annotations
import json, re
from typing import Optional
from urllib.parse import parse_qs, urlsplit
import httpx
from bs4 import BeautifulSoup
from .base import SourceResult

_PRICE_RE = re.compile(r"[\d,]+\.?\d*")


def _clean_price(text: str) -> Optional[float]:
    text = text.replace(",", "").replace("£", "").replace(" ", "").replace(" ", "").strip()
    m = _PRICE_RE.search(text)
    return float(m.group()) if m else None


_ENDED_PHRASES = ("this listing has ended", "this listing was ended", "bidding has ended")


def _page_state(html: str, result: SourceResult) -> str:
    """'removed', 'ended' or 'shell' when the page itself says so, else ''.

    A removed page and a script-only shell have no product at all, so the checks only run when no
    title or price was read; an ended listing can still carry its title and last price.
    """
    low = html.lower()
    ended = any(p in low for p in _ENDED_PHRASES)
    if result.title or result.price_gbp is not None:
        return "ended" if ended else ""
    if "we looked everywhere" in low:
        return "removed"
    if ended:
        return "ended"
    has_markup = bool(re.search(r"<h1[\s>]", html, re.I)) and len(html) > 5000
    return "" if has_markup else "shell"


def extract(url: str, html: str) -> SourceResult:
    """Parse a fetched eBay page. Returns a SourceResult with .state set when the page says so.

    A multi-variation listing also gets .offers (every variation's name, price and stock). When the
    link names a variation (?var=<id>), that variation's price and stock replace the page's default
    offer, which is often a different variation."""
    result = _extract_fields(url, html)
    result.state = _page_state(html, result)
    result.offers = _variations(html)
    want = (parse_qs(urlsplit(url or "").query).get("var") or [None])[0]
    if want and result.offers:
        chosen = next((o for o in result.offers if o["id"] == want), None)
        if chosen is None:
            result.note = "variation in link not found on the listing"
        else:
            result.variation_label = chosen["label"]
            if chosen["price_gbp"] is not None:
                result.price_gbp = chosen["price_gbp"]
            if chosen["available"] is not None:
                result.available = chosen["available"]
                result.availability_text = "In Stock" if chosen["available"] else "Out of Stock"
            result.note = "variation from link"
    return result


def _json_after(html: str, key: str):
    """The JSON value that follows "key": in page script, or None."""
    i = html.find(f'"{key}"')
    if i < 0:
        return None
    j = html.find(":", i)
    try:
        return json.JSONDecoder().raw_decode(html[j + 1:].lstrip())[0]
    except (ValueError, IndexError):
        return None


def _variations(html: str) -> list:
    """[{id, label, price_gbp, available}] from a multi-variation listing's page script; [] otherwise."""
    vmap = _json_after(html, "variationsMap")
    items = _json_after(html, "menuItemMap")
    menus = _json_after(html, "selectMenus")
    if not isinstance(vmap, dict) or not isinstance(items, dict) or not isinstance(menus, list):
        return []
    names = {}
    for menu in menus:
        for value_id in (menu or {}).get("menuItemValueIds") or []:
            item = items.get(str(value_id)) or {}
            for vid in item.get("matchingVariationIds") or []:
                names.setdefault(str(vid), []).append(str(item.get("valueName") or ""))
    out = []
    for vid, v in vmap.items():
        try:
            price = float(((v.get("binModel") or {}).get("price") or {}).get("value", {}).get("value"))
        except (TypeError, ValueError, AttributeError):
            price = None
        oos = (v.get("quantity") or {}).get("outOfStock")
        out.append({"id": str(vid), "label": " / ".join(n for n in names.get(str(vid), []) if n),
                    "price_gbp": price, "available": None if oos is None else not oos})
    return out


def _extract_fields(url: str, html: str) -> SourceResult:
    """Title, price and stock from an eBay page."""
    soup = BeautifulSoup(html, "lxml")
    result = SourceResult(url=url, platform="ebay")

    # --- Try JSON-LD first -------------------------------------------
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if data.get("@type") in ("Product", "ItemPage"):
            result.title = data.get("name")
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("price"):
                result.price_gbp = _clean_price(str(offers["price"]))
            avail = offers.get("availability", "")
            if "InStock" in avail or "InStoreOnly" in avail:
                result.available = True
                result.availability_text = "In Stock"
            elif "OutOfStock" in avail or "Discontinued" in avail:
                result.available = False
                result.availability_text = "Out of Stock"
            if result.title:
                return result   # JSON-LD gave us everything we need

    # --- DOM fallback ------------------------------------------------
    # Title
    for sel in ("#itemTitle", "h1.x-item-title__mainTitle", "h1[itemprop=name]"):
        el = soup.select_one(sel)
        if el:
            result.title = el.get_text(" ", strip=True).removeprefix("Details about").strip()
            break

    # Price
    for sel in ("#prcIsum", ".x-price-primary span", "[itemprop=price]"):
        el = soup.select_one(sel)
        if el:
            text = el.get("content") or el.get_text(strip=True)
            result.price_gbp = _clean_price(text)
            if result.price_gbp:
                break

    # Availability
    quantity_el = soup.select_one("#qtySubTxt, .d-quantity__availability")
    if quantity_el:
        qty_text = quantity_el.get_text(strip=True).lower()
        if "more than" in qty_text or "available" in qty_text:
            result.available = True
            result.availability_text = quantity_el.get_text(strip=True)
    sold_el = soup.select_one(
        ".vi-qtyS-hot-badge, #vi-cbox-bix-endedmsg, "
        "[class*=sold-out], [class*=ended], #DESOLD"
    )
    if sold_el is not None and result.available is None:
        result.available = False
        result.availability_text = "Sold / Ended"

    return result
