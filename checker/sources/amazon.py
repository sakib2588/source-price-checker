"""Amazon UK product extractor.

Amazon aggressively blocks scrapers, so we:
1. Use a desktop browser UA + Accept-Language: en-GB
2. Parse JSON-LD if present (often stripped), then DOM selectors
3. Detect bot-walls ("Robot Check") and report as error
"""
from __future__ import annotations
import json, re
from typing import Optional
from bs4 import BeautifulSoup
from .base import SourceResult
from .variations import amazon_label

_PRICE_RE = re.compile(r"[\d,]+\.?\d*")


def _clean_price(text: str) -> Optional[float]:
    text = text.replace(",", "").replace("£", "").strip()
    m = _PRICE_RE.search(text)
    return float(m.group()) if m else None


def extract(url: str, html: str) -> SourceResult:
    soup = BeautifulSoup(html, "lxml")
    result = SourceResult(url=url, platform="amazon")

    # Detect bot wall
    title_tag = soup.find("title")
    page_title = (title_tag.get_text(strip=True) if title_tag else "").lower()
    if "robot check" in page_title or "sign in" in page_title:
        result.error = "Bot-wall detected (Robot Check or Sign-in page)"
        return result

    # --- JSON-LD ---------------------------------------------------------
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if data.get("@type") == "Product":
            result.title = data.get("name")
            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("price"):
                result.price_gbp = float(str(offers["price"]).replace(",",""))
            avail = offers.get("availability", "")
            if "InStock" in avail:
                result.available = True
                result.availability_text = "In Stock"
            elif "OutOfStock" in avail:
                result.available = False
                result.availability_text = "Out of Stock"
            if result.title:
                return result

    # --- DOM fallback ----------------------------------------------------
    # Title
    for sel in ("#productTitle", "#title", "h1.product-title-word-break"):
        el = soup.select_one(sel)
        if el:
            result.title = el.get_text(strip=True)
            break

    # Price  (Amazon splits pounds and pence into separate spans)
    price_whole = soup.select_one(".a-price-whole")
    price_frac  = soup.select_one(".a-price-fraction")
    if price_whole:
        whole = _clean_price(price_whole.get_text(strip=True)) or 0
        frac  = float("0." + price_frac.get_text(strip=True)) if price_frac else 0.0
        result.price_gbp = round(whole + frac, 2)
    else:
        for sel in ("#priceblock_ourprice", "#priceblock_dealprice", "#price"):
            el = soup.select_one(sel)
            if el:
                result.price_gbp = _clean_price(el.get_text(strip=True))
                if result.price_gbp:
                    break

    # Availability
    avail_el = soup.select_one("#availability span, #outOfStock")
    if avail_el:
        txt = avail_el.get_text(strip=True)
        result.availability_text = txt
        if "in stock" in txt.lower():
            result.available = True
        elif any(w in txt.lower() for w in ("out of stock", "unavailable", "currently")):
            result.available = False

    # The linked ASIN's variation ("Red / Large"); Amazon puts no sibling prices on the page.
    result.variation_label = amazon_label(url, html)
    return result
