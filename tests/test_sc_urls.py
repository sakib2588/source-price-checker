"""sc_urls: Maskura link normalizing, canonical source ids, hosts and platforms. Pure, no network.

Run: .venv/bin/python -B -m unittest test_sc_urls
"""
import unittest

from checker import sc_urls as u

BASE = "https://www.maskura.co.uk/products/"


class NormalizeMaskura(unittest.TestCase):
    def n(self, raw):
        return u.normalize_maskura_url(raw)

    def test_shape(self):
        r = self.n("https://www.maskura.co.uk/products/widget")
        self.assertEqual(set(r), {"url", "handle", "variant_id", "raw", "ok", "reason"})
        self.assertEqual(r, {"url": BASE + "widget", "handle": "widget", "variant_id": None,
                             "raw": "https://www.maskura.co.uk/products/widget", "ok": True,
                             "reason": None})

    def test_row_1290_bare_host_gets_scheme_and_www(self):
        r = self.n("maskura.co.uk/products/barbell-weight-set")
        self.assertTrue(r["ok"])
        self.assertEqual(r["url"], "https://www.maskura.co.uk/products/barbell-weight-set")
        self.assertEqual(r["handle"], "barbell-weight-set")

    def test_row_314_product_text_is_not_a_link(self):
        for raw in ("Workout Leggings for Women - Sports & Gym Clothes Ideal for Running (maskura.co.",
                    "Workout Leggings for Women - Sports & Gym Clothes Ideal for Running (maskura.co.uk)"):
            with self.subTest(raw=raw):
                r = self.n(raw)
                self.assertFalse(r["ok"])
                self.assertIsNone(r["url"])
                self.assertIsNone(r["handle"])
                self.assertEqual(r["raw"], raw)
                self.assertTrue(r["reason"])

    def test_collection_path_becomes_product_path(self):
        r = self.n("https://maskura.co.uk/collections/gym/products/yoga-mat")
        self.assertEqual(r["url"], BASE + "yoga-mat")

    def test_only_variant_query_is_kept(self):
        r = self.n("https://www.maskura.co.uk/products/mat?_pos=1&_sid=ab&_ss=r&variant=4321&_v=1.0"
                   "&utm_source=x&_psq=mat")
        self.assertEqual(r["url"], BASE + "mat?variant=4321")
        self.assertEqual(r["variant_id"], "4321")

    def test_host_lowercased_fragment_and_trailing_slash_dropped(self):
        r = self.n("HTTP://WWW.Maskura.CO.UK/products/mat/#reviews")
        self.assertEqual(r["url"], BASE + "mat")

    def test_other_hosts_are_refused(self):
        for raw in ("https://www.ebay.co.uk/itm/123456789012", "https://maskura.co.uk.evil.com/products/x",
                    "https://evilmaskura.co.uk/products/x", "https://user@maskura.co.uk/products/x",
                    "https://maskura.co.uk:8443/products/x"):
            with self.subTest(raw=raw):
                r = self.n(raw)
                self.assertFalse(r["ok"])
                self.assertIsNone(r["url"])

    def test_non_product_pages_and_junk(self):
        for raw in (None, "", "   ", "N/A", "-", "https://www.maskura.co.uk/", "https://www.maskura.co.uk/collections/gym",
                    "javascript:alert(1)", "ftp://maskura.co.uk/products/x", "https://www.maskura.co.uk/products/"):
            with self.subTest(raw=raw):
                r = self.n(raw)
                self.assertFalse(r["ok"])
                self.assertIsNone(r["url"])
                self.assertEqual(r["raw"], "" if raw is None else raw)

    def test_non_numeric_variant_is_dropped(self):
        r = self.n("https://www.maskura.co.uk/products/mat?variant=abc")
        self.assertEqual((r["url"], r["variant_id"]), (BASE + "mat", None))

    def test_idempotent(self):
        once = self.n("maskura.co.uk/collections/a/products/Mat?variant=9&_pos=2")["url"]
        self.assertEqual(self.n(once)["url"], once)


class CanonicalIds(unittest.TestCase):
    def test_ebay_item(self):
        self.assertEqual(u.canonical_source_id("https://www.ebay.co.uk/itm/123456789012?hash=x"),
                         "ebay:123456789012")
        self.assertEqual(u.canonical_source_id("https://www.ebay.co.uk/itm/Some-Title/123456789012"),
                         "ebay:123456789012")

    def test_amazon_asin_keeps_region(self):
        self.assertEqual(u.canonical_source_id("https://www.amazon.co.uk/Widget/dp/B012345678/ref=sr_1"),
                         "amazon.co.uk:b012345678")
        self.assertEqual(u.canonical_source_id("https://amazon.com/gp/product/B012345678"),
                         "amazon.com:b012345678")

    def test_other_sites_use_host_and_path(self):
        self.assertEqual(u.canonical_source_id("https://Shop.Example.com/Item/42/?a=1#x"),
                         "shop.example.com/item/42")

    def test_missing_scheme_and_blank(self):
        self.assertEqual(u.canonical_source_id("www.ebay.co.uk/itm/123456789012"), "ebay:123456789012")
        self.assertEqual(u.canonical_source_id(""), "")
        self.assertEqual(u.canonical_source_id(None), "")

    def test_signature_sorted_and_skips_blanks(self):
        sig = u.source_signature(["https://www.ebay.co.uk/itm/222222222", "", None,
                                  "https://www.amazon.co.uk/dp/B012345678"])
        self.assertEqual(sig, "amazon.co.uk:b012345678|ebay:222222222")
        self.assertEqual(u.source_signature([]), "")


class HostsAndPlatforms(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(u.host_of("https://WWW.Ebay.co.uk/itm/1"), "www.ebay.co.uk")
        self.assertEqual(u.host_of("www.ebay.co.uk/itm/1"), "www.ebay.co.uk")
        self.assertEqual(u.host_of(""), "")
        self.assertEqual(u.host_of(None), "")
        self.assertEqual(u.host_of("http://[::1"), "")

    def test_detect_platform_from_registered_domain(self):
        self.assertEqual(u.detect_platform("https://www.ebay.co.uk/itm/1"), "ebay")
        self.assertEqual(u.detect_platform("https://www.amazon.com/dp/B012345678"), "amazon")
        self.assertEqual(u.detect_platform("https://shop.example.com/ebay-amazon"), "generic")
        self.assertEqual(u.detect_platform("https://www.maskura.co.uk/products/x"), "maskura")
        self.assertEqual(u.detect_platform("https://detail.tmall.com/item.htm"), "taobao")
        self.assertEqual(u.detect_platform(""), "generic")

    def test_normalize_source_url_matches_the_old_sheet_rules(self):
        self.assertEqual(u.normalize_source_url("www.ebay.co.uk/itm/1"), "https://www.ebay.co.uk/itm/1")
        for bad in (None, "", "N/A", "-", "TBC", "two words", "javascript:alert(1)", "http://10.0.0.1/x",
                    "ftp://x.com/a"):
            with self.subTest(bad=bad):
                self.assertIsNone(u.normalize_source_url(bad))


if __name__ == "__main__":
    unittest.main()
