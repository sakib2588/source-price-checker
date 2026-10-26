"""Which Maskura price a source is compared with (owner's rule, 2026-10-23).

The variant named in the Maskura link wins. Otherwise the Maskura variant whose name appears in the
source (its variation label, then its title) is used when exactly one variant matches best. Otherwise
the cheapest Maskura variant is used, so a product judged OK is never losing money on any variant.
Pure, no I/O.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[^a-z0-9.]+")
_IGNORE = {"", "default", "title"}
# "4 KG" and "4KG" are the same size: a number and its unit are joined before splitting into words.
_UNIT = re.compile(r"(\d)\s+(kg|g|mg|ml|l|cm|mm|m|oz|lb|lbs|pcs|pc|in|inch|ft)\b")


def _words(text):
    text = _UNIT.sub(r"\1\2", str(text or "").lower())
    return {w.strip(".") for w in _SPLIT.split(text)} - _IGNORE - {""}


def _priced(variants):
    out = []
    for v in variants or []:
        try:
            if v.get("price_gbp") is not None and float(v["price_gbp"]) > 0:
                out.append(v)
        except (TypeError, ValueError):
            continue
    return out


def match_variant(maskura_variants, source_text):
    """(variant, how): how is "single", "name" or "cheapest"; (None, "none") when no variant has a price."""
    vs = _priced(maskura_variants)
    if not vs:
        return None, "none"
    if len({float(v["price_gbp"]) for v in vs}) == 1:
        return vs[0], "single"
    src = _words(source_text)
    scored = [(len(w), v) for v in vs for w in [_words(v.get("title"))] if w and w <= src]
    if scored:
        best = max(n for n, _ in scored)
        top = [v for n, v in scored if n == best]
        if len(top) == 1:
            return top[0], "name"
    return min(vs, key=lambda v: float(v["price_gbp"])), "cheapest"


def _offer_prices(source):
    out = []
    for o in (source or {}).get("offers") or []:
        try:
            price = float(o.get("price_gbp"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            out.append((_words(o.get("label")), price))
    return out


def variation_range(maskura, source):
    """How a source listing with variations at different prices compares when its link names none.

    Each Maskura variant (only the one in the Maskura link, when it names one) is compared with the
    variations its name matches, or with every variation when none match. "ok" when even the dearest
    candidate leaves a margin, "dearer" when even the cheapest does not, "unclear" otherwise. Returns
    {"result", "margin", "maskura_price", "source_price"} (the deciding pair; None for unclear), or None
    when the rule does not apply: the link names a variation, or the listing has one price."""
    if (source or {}).get("variation_label"):
        return None
    offers = _offer_prices(source)
    if len({p for _, p in offers}) < 2:
        return None
    vs = _priced((maskura or {}).get("variants"))
    want = (maskura or {}).get("variant_id")
    targets = [v for v in vs if want and str(v.get("id")) == str(want)] or vs
    if not targets:
        price = (maskura or {}).get("price_gbp")
        if not price:
            return None
        targets = [{"title": "", "price_gbp": price}]
    worst = losing = None
    unclear = False
    for v in targets:
        mp, name = float(v["price_gbp"]), _words(v.get("title"))
        # The closest names win, so "4 kg" matches "4KG" and not also "2 x 4KG".
        named = [(len(w - name), p) for w, p in offers if name and name <= w]
        cands = [p for extra, p in named if extra == min(e for e, _ in named)] or [p for _, p in offers]
        low, high = mp - max(cands), mp - min(cands)
        if high <= 0:
            if losing is None or high < losing[0]:
                losing = (high, mp, min(cands))
        elif low <= 0:
            unclear = True
        elif worst is None or low < worst[0]:
            worst = (low, mp, max(cands))
    if losing:
        result, (margin, mp, sp) = "dearer", losing
    elif unclear:
        return {"result": "unclear", "margin": None, "maskura_price": None, "source_price": None}
    else:
        result, (margin, mp, sp) = "ok", worst
    return {"result": result, "margin": round(margin, 2), "maskura_price": mp, "source_price": sp}


def compare_price(maskura, source):
    """(Maskura price to compare with this source, how it was chosen).

    how: "link" (variant in the Maskura link), "single", "name", "cheapest" or "product" (no variant
    prices known, the product price is used)."""
    vs = (maskura or {}).get("variants") or []
    want = (maskura or {}).get("variant_id")
    if want:
        v = next((x for x in vs if str(x.get("id")) == str(want)), None)
        if v and v.get("price_gbp"):
            return float(v["price_gbp"]), "link"
    label = (source or {}).get("variation_label")
    if label:
        v, how = match_variant(vs, label)
        if how in ("name", "single"):
            return float(v["price_gbp"]), how
    v, how = match_variant(vs, (source or {}).get("title"))
    if v is None:
        price = (maskura or {}).get("price_gbp")
        return (float(price) if price else None), "product"
    return float(v["price_gbp"]), how
