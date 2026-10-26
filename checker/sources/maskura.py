"""Maskura UK (Shopify) product extractor.

Uses the Shopify product JSON endpoint (/products/<handle>.json) first —
fast, no HTML parsing, no JS needed. Falls back to DOM scrape.
"""
from __future__ import annotations
import json, re
from typing import Optional
from urllib.parse import parse_qs, urlsplit
from bs4 import BeautifulSoup
from .base import SourceResult


def _shopify_json_url(url: str) -> Optional[str]:
    m = re.match(r"(https?://[^/]+/products/[^/?#]+)", url)
    if m:
        return m.group(1) + ".json"
    return None


def _pick_variant(variants: list, url: str):
    """(variant, note). The variant named in the link wins; otherwise the first one, and the note
    says so when the product has several prices so the choice is never silent."""
    want = parse_qs(urlsplit(url or "").query).get("variant", [None])[0]
    if want:
        for v in variants:
            if str(v.get("id")) == want:
                return v, "variant from link"
        return None, "variant in link not found on product"
    prices = set()
    for v in variants:
        try:
            prices.add(float(v.get("price") or 0))
        except (TypeError, ValueError):
            pass
    if len(prices) > 1:
        return variants[0], "no variant in link, first variant used (product has several prices)"
    return variants[0], "single price"


def extract(url: str, html: str, json_data: Optional[dict] = None) -> SourceResult:
    result = SourceResult(url=url, platform="maskura")

    # --- Shopify product JSON (preferred) --------------------------------
    if json_data:
        product = json_data.get("product", {})
        result.title = product.get("title")
        variants = product.get("variants", [])
        if variants:
            v, result.note = _pick_variant(variants, url)
            if v is None:
                result.error = result.note
                result.note = ""
                return result
            try:
                result.price_gbp = float(v.get("price") or 0) or None
            except (ValueError, TypeError):
                pass
            # inventory_quantity is None when Shopify inventory is not tracked
            inv = v.get("inventory_quantity")
            if inv is None:
                # Shopify store not tracking stock — assume available if product exists
                result.available = True
                result.availability_text = "Available (stock not tracked)"
            elif isinstance(inv, (int, float)):
                result.available = inv > 0
                result.availability_text = "In Stock" if result.available else "Out of Stock"
            else:
                result.available = None
                result.availability_text = ""
        return result

    # --- DOM fallback ----------------------------------------------------
    soup = BeautifulSoup(html, "lxml")

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
                result.price_gbp = float(str(offers["price"]).replace(",", ""))
            avail = offers.get("availability", "")
            result.available = "InStock" in avail
            result.availability_text = "In Stock" if result.available else "Out of Stock"
            if result.title:
                return result

    for sel in ("h1.product__title", "h1[itemprop=name]", "h1"):
        el = soup.select_one(sel)
        if el:
            result.title = el.get_text(strip=True)
            break

    price_el = soup.select_one(".price__current, [class*=price]")
    if price_el:
        m2 = re.search(r"[\d,]+\.?\d*", price_el.get_text())
        if m2:
            result.price_gbp = float(m2.group().replace(",", ""))

    return result


def shopify_json_url(url: str) -> Optional[str]:
    return _shopify_json_url(url)
