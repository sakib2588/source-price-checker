"""sources.variations: one variation reader per site type, and how each feeds the Maskura comparison.

Run: PYTHONPATH=. python -B -m unittest tests.test_sc_site_variations
"""
import html
import json
import unittest

from checker import sc_variants
from checker.sources import amazon, generic, variations as v

PAD = "<!-- " + "x" * 6000 + " -->"


def page(body, head=""):
    return f"<html><head><title>Dumbbell</title>{head}</head><body>{body}{PAD}</body></html>"


SHOPIFY_JSON = {"id": 1, "title": "Dumbbell", "variants": [
    {"id": 11, "title": "1kg", "public_title": "1kg", "price": 1499, "available": True},
    {"id": 12, "title": "2kg", "public_title": "2kg", "price": 2299, "available": False},
    {"id": 13, "title": "4kg", "public_title": "4kg", "price": 3599, "available": True}]}
SHOPIFY = page(f'<script type="application/json" data-product-json>{json.dumps(SHOPIFY_JSON)}</script>')
SHOPIFY_META = page('<script>var meta = {"product":{"id":1,"variants":[{"id":21,"price":1000,"name":"D - Red","public_title":"Red"},'
                    '{"id":22,"price":1200,"name":"D - Blue","public_title":"Blue"}]}};</script>')
WOO_ROWS = [{"variation_id": 31, "attributes": {"attribute_pa_size": "2-x-4kg"}, "display_price": 49.99, "is_in_stock": True},
            {"variation_id": 32, "attributes": {"attribute_pa_size": "4kg"}, "display_price": 29.5, "is_in_stock": False}]
WOO = page(f"<form class='variations_form' data-product_variations='{html.escape(json.dumps(WOO_ROWS), quote=True)}'></form>"
           .replace("&#x27;", "&#39;"))
GROUP = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "Mat", "hasVariant": [
    {"@type": "Product", "name": "Mat - Blue", "sku": "M-B", "offers": {"price": "10.00", "priceCurrency": "GBP",
                                                                        "availability": "https://schema.org/InStock"}},
    {"@type": "Product", "name": "Mat - Pink", "sku": "M-P", "offers": {"price": "12.00", "priceCurrency": "GBP",
                                                                        "availability": "https://schema.org/OutOfStock"}},
    {"@type": "Product", "name": "Mat - USD", "sku": "M-U", "offers": {"price": "9.00", "priceCurrency": "USD"}}]}
JSONLD = page("", f'<script type="application/ld+json">{json.dumps(GROUP)}</script>')


class Readers(unittest.TestCase):
    def test_shopify_theme_json_prices_in_pounds(self):
        self.assertEqual([(o["id"], o["label"], o["price_gbp"], o["available"]) for o in v.shopify(SHOPIFY)],
                         [("11", "1kg", 14.99, True), ("12", "2kg", 22.99, False), ("13", "4kg", 35.99, True)])

    def test_shopify_meta_variants_have_no_stock_flag(self):
        offers = v.shopify(SHOPIFY_META)
        self.assertEqual([(o["label"], o["price_gbp"], o["available"]) for o in offers],
                         [("Red", 10.0, None), ("Blue", 12.0, None)])

    def test_shopify_analytics_payload_has_pounds_and_a_currency(self):
        text = page('<script>x={"variants":[{"id":"1","price":{"amount":152.49,"currencyCode":"GBP"},"title":"1800W"},'
                    '{"id":"2","price":{"amount":126.14,"currencyCode":"GBP"},"title":"1200W"},'
                    '{"id":"3","price":{"amount":9.0,"currencyCode":"USD"},"title":"USD"}]}</script>')
        self.assertEqual([(o["label"], o["price_gbp"]) for o in v.shopify(text)],
                         [("1800W", 152.49), ("1200W", 126.14), ("USD", None)])

    def test_shopify_takes_the_variants_of_the_product_in_the_link_not_a_recommended_one(self):
        rec = '"variants":[{"id":901,"price":100,"title":"A"},{"id":902,"price":200,"title":"B"}]'
        main = ('"handle":"dumbbell","variants":[{"id":11,"price":1499,"title":"1kg"},'
                '{"id":12,"price":2299,"title":"2kg"}]')
        text = page(f"<script>x={{{rec}}};y={{{main}}};</script>")
        self.assertEqual([o["id"] for o in v.read("https://s.co.uk/products/dumbbell", text)], ["11", "12"])
        self.assertEqual([o["id"] for o in v.read("https://s.co.uk/products/other?variant=12", text)], ["11", "12"])
        self.assertEqual(v.read("https://s.co.uk/products/unknown", text), [])

    def test_shopify_single_default_variant_is_not_a_choice(self):
        one = page('<script type="application/json" data-product-json>{"variants":[{"id":1,"title":"Default Title","price":500}]}</script>')
        self.assertEqual(v.shopify(one), [])

    def test_woocommerce(self):
        self.assertEqual([(o["id"], o["label"], o["price_gbp"], o["available"]) for o in v.woocommerce(WOO)],
                         [("31", "2 x 4kg", 49.99, True), ("32", "4kg", 29.5, False)])

    def test_jsonld_product_group_drops_the_group_name_and_non_gbp_prices(self):
        offers = v.jsonld(JSONLD)
        self.assertEqual([(o["label"], o["price_gbp"], o["available"]) for o in offers],
                         [("Blue", 10.0, True), ("Pink", 12.0, False), ("USD", None, None)])

    def test_jsonld_product_with_several_offers(self):
        ld = {"@type": "Product", "name": "Ball", "offers": [{"name": "Small", "price": "5", "priceCurrency": "GBP"},
                                                              {"name": "Large", "price": "8", "priceCurrency": "GBP"}]}
        offers = v.jsonld(page("", f'<script type="application/ld+json">{json.dumps(ld)}</script>'))
        self.assertEqual([(o["label"], o["price_gbp"]) for o in offers], [("Small", 5.0), ("Large", 8.0)])

    def test_a_plain_product_has_no_variations(self):
        plain = page("", '<script type="application/ld+json">{"@type":"Product","offers":{"price":"5"}}</script>')
        self.assertEqual(v.read("https://x.co.uk/p", plain), [])

    def test_broken_json_is_ignored(self):
        self.assertEqual(v.read("u", page('<script data-product-json>{nope</script><form data-product_variations="[bad">')), [])


class Linked(unittest.TestCase):
    OFFERS = [{"id": "11", "label": "1kg", "price_gbp": 1.0, "available": True},
              {"id": "12", "label": "2 x 4kg", "price_gbp": 2.0, "available": True},
              {"id": "13", "label": "4kg", "price_gbp": 3.0, "available": True}]

    def test_by_id_params(self):
        for q in ("variant=12", "variation_id=12", "var=12"):
            self.assertEqual(v.pick_linked("https://x.co.uk/p?" + q, self.OFFERS)["id"], "12")

    def test_by_woocommerce_attribute(self):
        self.assertEqual(v.pick_linked("https://x.co.uk/p?attribute_pa_size=2-x-4kg", self.OFFERS)["id"], "12")

    def test_ambiguous_attribute_names_nothing(self):
        self.assertIsNone(v.pick_linked("https://x.co.uk/p?attribute_pa_size=4kg", self.OFFERS))

    def test_no_param_or_unknown_id(self):
        self.assertIsNone(v.pick_linked("https://x.co.uk/p", self.OFFERS))
        self.assertIsNone(v.pick_linked("https://x.co.uk/p?variant=99", self.OFFERS))


class Extractors(unittest.TestCase):
    def test_generic_keeps_every_offer_and_prices_the_linked_one(self):
        r = generic.extract("https://shop.co.uk/products/d?variant=13", SHOPIFY)
        self.assertEqual((r.variation_label, r.price_gbp, r.available, len(r.offers)), ("4kg", 35.99, True, 3))

    def test_generic_without_a_link_variation_lists_offers_only(self):
        r = generic.extract("https://shop.co.uk/products/d", SHOPIFY)
        self.assertIsNone(r.variation_label)
        self.assertEqual(len(r.offers), 3)

    def test_generic_linked_out_of_stock_variation(self):
        r = generic.extract("https://shop.co.uk/products/d?variant=12", SHOPIFY)
        self.assertEqual((r.price_gbp, r.available), (22.99, False))

    def test_amazon_label_of_the_linked_asin(self):
        data = {"B000000001": ["Red", "Large"], "B000000002": ["Blue", "Small"]}
        text = page(f'<script>var x = {{"dimensionValuesDisplayData":{json.dumps(data)},"other":1}};</script>')
        self.assertEqual(v.amazon_label("https://www.amazon.co.uk/dp/B000000002?th=1", text), "Blue / Small")
        self.assertIsNone(v.amazon_label("https://www.amazon.co.uk/dp/B000000009", text))
        self.assertIsNone(v.amazon_label("https://www.amazon.co.uk/dp/B000000002", page("")))

    def test_amazon_extract_sets_the_label(self):
        data = {"B000000002": ["Blue", "Small"]}
        text = page(f'<span id="productTitle">Thing</span><script>{{"dimensionValuesDisplayData":{json.dumps(data)}}}</script>')
        self.assertEqual(amazon.extract("https://www.amazon.co.uk/dp/B000000002", text).variation_label, "Blue / Small")


class AgainstMaskura(unittest.TestCase):
    """The same comparison works for every site type, because every reader gives {id, label, price, stock}."""
    MASKURA = {"price_gbp": 14.0, "variants": [{"id": 1, "title": "1kg", "price_gbp": 19.99},
                                               {"id": 2, "title": "2kg", "price_gbp": 24.99},
                                               {"id": 3, "title": "4kg", "price_gbp": 34.99}]}

    def source(self, url, text):
        r = generic.extract(url, text)
        return {"offers": r.offers, "variation_label": r.variation_label, "title": r.title, "price_gbp": r.price_gbp}

    def test_shopify_supplier_matches_each_maskura_variant_by_name(self):
        res = sc_variants.variation_range(self.MASKURA, self.source("https://shop.co.uk/p", SHOPIFY))
        # 4kg sells for 34.99 on Maskura but costs 35.99 at the supplier: losing money.
        self.assertEqual(res["result"], "dearer")
        self.assertEqual((res["maskura_price"], res["source_price"]), (34.99, 35.99))

    def test_linked_variation_picks_the_matching_maskura_price(self):
        src = self.source("https://shop.co.uk/p?variant=11", SHOPIFY)
        self.assertEqual(sc_variants.compare_price(self.MASKURA, src), (19.99, "name"))

    def test_woocommerce_supplier(self):
        src = self.source("https://shop.co.uk/p", WOO)
        maskura = {"price_gbp": 60.0, "variants": [{"id": 1, "title": "2 x 4kg", "price_gbp": 59.99},
                                                   {"id": 2, "title": "4kg", "price_gbp": 39.99}]}
        res = sc_variants.variation_range(maskura, src)
        self.assertEqual((res["result"], res["margin"]), ("ok", 10.0))


if __name__ == "__main__":
    unittest.main()
