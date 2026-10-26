"""Unit tests for title_quality: placeholder and junk titles, readable fallback names, URL cleanup,
suspect-margin flag and plain-language reasons for missing prices.

Pure functions, no network, no files.

Run: python3 -B -m unittest test_title_quality
"""
import unittest

from checker import title_quality as tq


class PlaceholderTitles(unittest.TestCase):
    def test_shopify_default_variant_title_is_a_placeholder(self):
        for value in ("Default Title", "default title", "  Default Title  ", "Default", "Title"):
            self.assertTrue(tq.is_placeholder_title(value), value)

    def test_blank_and_dash_values_are_placeholders(self):
        for value in (None, "", "   ", "-", "—", "N/A", "n/a", "Untitled", "null", "None"):
            self.assertTrue(tq.is_placeholder_title(value), repr(value))

    def test_real_titles_are_not_placeholders(self):
        for value in ("Electric Body Brush", "Default Settings Wall Clock", "Title Case Notebook"):
            self.assertFalse(tq.is_placeholder_title(value), value)


class CleanTitle(unittest.TestCase):
    def test_placeholder_becomes_none(self):
        self.assertIsNone(tq.clean_title("Default Title"))

    def test_known_junk_page_titles_become_none(self):
        for value in ("Discover | eBay", "Page Not Found", "404 Not Found", "Access Denied",
                      "Just a moment...", "Robot Check", "Attention Required! | Cloudflare",
                      "Error 404", "503 Service Unavailable"):
            self.assertIsNone(tq.clean_title(value), value)

    def test_whitespace_is_collapsed_and_trimmed(self):
        self.assertEqual(tq.clean_title("  Yoga   Chair \n Foldable "), "Yoga Chair Foldable")

    def test_control_characters_are_removed_not_turned_into_spaces(self):
        self.assertEqual(tq.clean_title("bad\x0bname"), "badname")
        self.assertEqual(tq.clean_title("a\x00b\x1fc"), "abc")
        self.assertEqual(tq.clean_title("tab\tseparated"), "tab separated")

    def test_real_title_with_platform_suffix_is_kept(self):
        self.assertEqual(tq.clean_title("Nasal Strips | eBay"), "Nasal Strips | eBay")

    def test_product_that_mentions_not_found_is_kept(self):
        self.assertEqual(tq.clean_title("Lost and Found Keyring Tag"), "Lost and Found Keyring Tag")


class NameFromUrl(unittest.TestCase):
    def test_shopify_handle_becomes_readable_name(self):
        url = "https://www.maskura.co.uk/products/king-size-electric-blankets-on-sale?_pos=1&_psq=x"
        self.assertEqual(tq.name_from_url(url), "King Size Electric Blankets On Sale")

    def test_non_product_url_gives_none(self):
        self.assertIsNone(tq.name_from_url("https://www.maskura.co.uk/collections/fitness"))
        self.assertIsNone(tq.name_from_url(""))
        self.assertIsNone(tq.name_from_url(None))


class SessionParams(unittest.TestCase):
    def test_search_session_params_are_removed(self):
        url = "https://www.maskura.co.uk/products/electric-body-brush?_pos=1&_psq=Electric+Body+Brush&_ss=e&_v=1.0"
        self.assertEqual(tq.strip_session_params(url),
                         "https://www.maskura.co.uk/products/electric-body-brush")

    def test_variant_is_kept(self):
        url = "https://www.maskura.co.uk/products/x?variant=4455&_pos=2&_ss=r"
        self.assertEqual(tq.strip_session_params(url),
                         "https://www.maskura.co.uk/products/x?variant=4455")

    def test_url_without_query_is_unchanged(self):
        url = "https://www.maskura.co.uk/products/x"
        self.assertEqual(tq.strip_session_params(url), url)

    def test_empty_values_pass_through(self):
        self.assertEqual(tq.strip_session_params(""), "")
        self.assertIsNone(tq.strip_session_params(None))


class DisplayName(unittest.TestCase):
    def test_sheet_name_wins(self):
        row = {"row_id": 3, "product_name": "Jaw Exerciser", "maskura_title": "Jawline Exercise Ball"}
        self.assertEqual(tq.display_name(row), ("Jaw Exerciser", "sheet"))

    def test_placeholder_sheet_name_falls_back_to_maskura_title(self):
        row = {"row_id": 3, "product_name": "Default Title", "maskura_title": "Jawline Exercise Ball"}
        self.assertEqual(tq.display_name(row), ("Jawline Exercise Ball", "maskura"))

    def test_both_placeholders_fall_back_to_source_title(self):
        row = {"row_id": 9, "product_name": "", "maskura_title": "Default Title",
               "sources": [{"title": "Discover | eBay"}, {"title": "Yoga Chair Foldable | eBay"}]}
        self.assertEqual(tq.display_name(row), ("Yoga Chair Foldable", "source"))

    def test_then_falls_back_to_url_handle(self):
        row = {"row_id": 9, "product_name": "", "maskura_title": "",
               "maskura_url": "https://www.maskura.co.uk/products/feet-up-yoga-stool?_pos=1",
               "sources": []}
        self.assertEqual(tq.display_name(row), ("Feet Up Yoga Stool", "url"))

    def test_nothing_usable_gives_explicit_label_not_a_dash(self):
        row = {"row_id": 962, "product_name": "", "maskura_title": "", "maskura_url": "", "sources": []}
        self.assertEqual(tq.display_name(row), ("Unnamed product (row 962)", "none"))

    def test_never_returns_placeholder_text(self):
        row = {"row_id": 1, "product_name": "Default Title", "maskura_title": "Default Title",
               "maskura_url": "", "sources": []}
        name, _ = tq.display_name(row)
        self.assertNotIn("Default Title", name)


class PlatformLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(tq.platform_label("ebay"), "eBay")
        self.assertEqual(tq.platform_label("amazon"), "Amazon")
        self.assertEqual(tq.platform_label("generic"), "Other site")
        self.assertEqual(tq.platform_label("maskura"), "Maskura")
        self.assertEqual(tq.platform_label("none"), "No link")
        self.assertEqual(tq.platform_label("aliexpress"), "AliExpress")
        self.assertEqual(tq.platform_label(None), "No link")
        self.assertEqual(tq.platform_label("somenewsite"), "Somenewsite")


class MarginSuspect(unittest.TestCase):
    def test_normal_margins_are_not_suspect(self):
        self.assertIsNone(tq.margin_suspect(12.99, 10.99))
        self.assertIsNone(tq.margin_suspect(29.99, 12.00))

    def test_more_than_four_times_the_source_is_suspect(self):
        self.assertIn("4", tq.margin_suspect(99.0, 9.99))
        self.assertIsNotNone(tq.margin_suspect(299.0, 56.5))
        self.assertIsNotNone(tq.margin_suspect(19.99, 4.99))

    def test_tiny_source_price_is_suspect(self):
        self.assertIsNotNone(tq.margin_suspect(9.99, 0.17))

    def test_far_below_source_is_suspect(self):
        self.assertIsNotNone(tq.margin_suspect(4.0, 20.0))

    def test_missing_prices_are_not_suspect(self):
        self.assertIsNone(tq.margin_suspect(None, 5.0))
        self.assertIsNone(tq.margin_suspect(5.0, None))
        self.assertIsNone(tq.margin_suspect(5.0, 0))


class NoPriceReason(unittest.TestCase):
    def test_reasons(self):
        cases = [
            ({"platform": "none", "url": ""}, "No link in sheet"),
            ({"platform": "none", "url": "https://x", "verdict": "NOT_CHECKED"}, "Not checked yet"),
            ({"platform": "ebay", "available": False, "price_gbp": None}, "Out of stock, no price shown"),
            ({"platform": "ebay", "error": "NO_CONTENT: eBay shell page, no product content"},
             "Page showed no product details"),
            ({"platform": "amazon", "error": "BLOCKED: challenge-platform"}, "Site blocked the check"),
            ({"platform": "ebay", "avail_text": "Page not found (removed)", "available": False},
             "Listing removed"),
            ({"platform": "generic", "error": "PARSER_GAP: product page with no readable price"},
             "Price not found on page"),
            ({"platform": "generic", "error": "ERROR: TimeoutError: "}, "Page did not respond"),
            ({"platform": "generic", "error": "Fetch failed: boom"}, "Could not read page"),
            ({"platform": "generic"}, "Price not found on page"),
            ({"platform": "amazon", "url": "https://www.amazon.com/dp/B0", "verdict": "NON_GBP"},
             "Price is not in pounds"),
            ({"platform": "ebay", "url": "https://www.ebay.co.uk/itm/1", "verdict": "NOT_FOUND"},
             "Listing removed"),
            ({"platform": "ebay", "url": "https://www.ebay.co.uk/itm/1", "verdict": "ENDED_REDIRECT"},
             "Listing ended or replaced by another item"),
        ]
        for source, expected in cases:
            self.assertEqual(tq.no_price_reason(source), expected, source)


class MaskuraStatus(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(tq.maskura_status({"maskura_error": "Maskura product not found (404)"}),
                         "Maskura link is broken (404)")
        self.assertEqual(tq.maskura_status({"maskura_error": "fetch failed: HTTPStatusError"}),
                         "Maskura page could not be reached")
        self.assertEqual(tq.maskura_status({"maskura_error": "Maskura price not crawled in this batch"}),
                         "Maskura price not checked yet")
        self.assertEqual(tq.maskura_status({"maskura_error": "not a Maskura product URL"}),
                         "Link is not a product page")
        self.assertIsNone(tq.maskura_status({"maskura_error": ""}))
        self.assertIsNone(tq.maskura_status({}))


if __name__ == "__main__":
    unittest.main()
