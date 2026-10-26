"""Source check accuracy (docs/source-check/ACCURACY_PLAN.md): the owner's rule decides a problem.

Run: uv run --with flask --with reportlab --with python-dotenv --with httpx --with tenacity \
        --with beautifulsoup4 --with lxml --with openpyxl --with xlsxwriter python3 -B -m unittest test_sc_accuracy
"""
import importlib.util

import httpx
import pathlib
import unittest

_spec = importlib.util.spec_from_file_location(
    "rescore_report", pathlib.Path(__file__).parent / "scripts" / "rescore_report.py")
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

M = {"status": "OK", "price_gbp": 20.0, "variants": [{"id": "1", "title": "Default Title", "price_gbp": 20.0}]}


def src(**kw):
    d = {"url": "https://www.ebay.co.uk/itm/1", "verdict": "PRICE", "price_gbp": 10.0, "available": True}
    d.update(kw)
    return d


class Bucket(unittest.TestCase):
    def test_cheaper_live_source_is_ok(self):
        self.assertEqual(rr.bucket(M, [src()]), "OK")

    def test_no_maskura(self):
        self.assertEqual(rr.bucket(None, [src()]), "NO_MASKURA_LINK")

    def test_only_dead_sources(self):
        self.assertEqual(rr.bucket(M, [src(verdict="NOT_FOUND", price_gbp=None)]), "NO_BUYABLE_SOURCE")

    def test_dearer(self):
        self.assertEqual(rr.bucket(M, [src(price_gbp=25.0)]), "SOURCE_DEARER")

    def test_equal_price_is_dearer(self):
        self.assertEqual(rr.bucket(M, [src(price_gbp=20.0)]), "SOURCE_DEARER")


import sc_crawl  # noqa: E402
from test_sc_crawl import RecordingPacer, html, jsonr, mock_client, routes  # noqa: E402

OLD = "https://www.maskura.co.uk/products/best-steam-inhaler-uk"
NEW = "https://www.maskura.co.uk/products/portable-steam-inhaler"
PRODUCT = {"product": {"title": "Portable Steam Inhaler",
                       "variants": [{"id": 1, "title": "Default Title", "price": "19.99"}]}}


class MaskuraMoved(unittest.TestCase):
    """Shopify forwards an old product handle (301) on the page but not on /products/<handle>.json."""

    def test_json_404_and_page_301_reads_the_new_handle(self):
        h = routes({OLD + ".json": [html(404)],
                    OLD: [html(301, headers={"location": NEW})],
                    NEW: [html(200, b"<html></html>")],
                    NEW + ".json": [jsonr(PRODUCT)]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["price_gbp"], 19.99)
        self.assertEqual(out["moved_to"], NEW)
        self.assertIn("moved", out["note"])

    def test_real_404_stays_not_found(self):
        h = routes({OLD + ".json": [html(404)], OLD: [html(404)]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "NOT_FOUND")
        self.assertNotIn("moved_to", out)

    def test_redirect_to_a_non_product_page_is_not_a_move(self):
        home = "https://www.maskura.co.uk/"
        h = routes({OLD + ".json": [html(404)], OLD: [html(301, headers={"location": home})],
                    home: [html(200, b"<html>home</html>")]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "NOT_FOUND")

    def test_failed_move_check_is_retryable_not_gone(self):
        for page in (html(429), html(503), html(403)):
            with self.subTest(status=page.status_code):
                h = routes({OLD + ".json": [html(404)], OLD: [page]})
                out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
                self.assertEqual(out["status"], "BLOCKED")

    def test_move_check_network_error_is_retryable(self):
        h = routes({OLD + ".json": [html(404)], OLD: [httpx.ConnectTimeout("slow")]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "ERROR")

    def test_moved_product_rate_limited_is_retryable_with_move_noted(self):
        h = routes({OLD + ".json": [html(404)], OLD: [html(301, headers={"location": NEW})],
                    NEW: [html(200, b"<html></html>")], NEW + ".json": [html(429)]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "BLOCKED")
        self.assertEqual(out["moved_to"], NEW)

    def test_variant_in_link_is_carried_to_the_new_handle(self):
        old_v = OLD + "?variant=7"
        prod = {"product": {"title": "Inhaler", "variants": [
            {"id": 6, "title": "Blue", "price": "10.00"}, {"id": 7, "title": "Red", "price": "12.00"}]}}
        h = routes({OLD + ".json": [html(404)], OLD: [html(301, headers={"location": NEW})],
                    NEW: [html(200, b"<html></html>")], NEW + ".json": [jsonr(prod)]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), old_v)
        self.assertEqual((out["status"], out["price_gbp"]), ("OK", 12.0))

    def test_new_handle_also_404_stays_not_found(self):
        h = routes({OLD + ".json": [html(404)], OLD: [html(301, headers={"location": NEW})],
                    NEW: [html(200, b"<html></html>")], NEW + ".json": [html(404)]})
        out = sc_crawl.fetch_maskura(mock_client(h), RecordingPacer(), OLD)
        self.assertEqual(out["status"], "NOT_FOUND")


FIX = pathlib.Path(__file__).parent / "fixtures"
EBAY = "https://www.ebay.co.uk/itm/185157700734"


class Ebay404(unittest.TestCase):
    """eBay answers 404 for an ended or sold-out listing but still sends the item page (2026-10-23)."""

    def fetch(self, body, status=404):
        h = routes({EBAY: [html(status, body)]})
        return sc_crawl.fetch_source(mock_client(h), RecordingPacer(), EBAY)

    def test_404_that_still_shows_the_item_is_out_of_stock_not_removed(self):
        sd = self.fetch((FIX / "ebay_404_outofstock.html").read_bytes())
        self.assertEqual(sd["verdict"], "OUT_OF_STOCK")
        self.assertIs(sd["available"], False)
        self.assertTrue(sd["title"].startswith("Women Waist Trainer"))
        self.assertNotIn("removed", (sd.get("removal_comment") or "").lower())

    def test_we_looked_everywhere_404_stays_not_found(self):
        sd = self.fetch((FIX / "ebay_404_removed.html").read_bytes())
        self.assertEqual(sd["verdict"], "NOT_FOUND")

    def test_empty_404_stays_not_found(self):
        self.assertEqual(self.fetch(b"")["verdict"], "NOT_FOUND")

    def test_404_item_page_never_gives_a_comparable_price(self):
        body = (FIX / "ebay_404_outofstock.html").read_text().replace(
            '"availability":"https://schema.org/OutOfStock"',
            '"availability":"https://schema.org/OutOfStock","price":"13.99"')
        sd = self.fetch(body.encode())
        self.assertIs(sd["available"], False, "an eBay 404 page is never a buyable source")


from sources import ebay as ebay_src  # noqa: E402
import sc_variants  # noqa: E402

VAR_ITEM = "https://www.ebay.co.uk/itm/253805285784"


class EbayVariations(unittest.TestCase):
    """A multi-variation listing keeps every variation's price and stock in page script (2026-10-23)."""

    def setUp(self):
        self.page = (FIX / "ebay_variations.html").read_text()

    def test_linked_variation_price_and_stock_are_read(self):
        r = ebay_src.extract(VAR_ITEM + "?var=553089464028", self.page)
        self.assertEqual(r.variation_label, "Pink / 8oz")
        self.assertEqual(r.price_gbp, 13.99)
        self.assertIs(r.available, False, "the page offer says InStock but the linked variation is sold out")
        self.assertEqual(len(r.offers), 42)

    def test_without_a_variation_in_the_link_the_page_price_stays(self):
        r = ebay_src.extract(VAR_ITEM, self.page)
        self.assertIsNone(r.variation_label)
        self.assertEqual((r.price_gbp, r.available), (13.99, True))
        self.assertEqual(len(r.offers), 42)

    def test_unknown_variation_in_the_link_is_said(self):
        r = ebay_src.extract(VAR_ITEM + "?var=1", self.page)
        self.assertIsNone(r.variation_label)
        self.assertIn("not found", r.note)

    def test_single_listing_has_no_offers(self):
        r = ebay_src.extract(EBAY, (FIX / "ebay_404_outofstock.html").read_text())
        self.assertEqual(r.offers, [])

    def test_fetch_keeps_the_variation_and_clears_the_unverified_flag(self):
        h = routes({VAR_ITEM + "?var=553089464028": [html(200, self.page.encode())]})
        sd = sc_crawl.fetch_source(mock_client(h), RecordingPacer(), VAR_ITEM + "?var=553089464028")
        self.assertEqual(sd["variation_label"], "Pink / 8oz")
        self.assertIs(sd["available"], False)
        self.assertFalse(sd["variation_unverified"])


V = [{"id": "1", "title": "Small / Black", "price_gbp": 9.99},
     {"id": "2", "title": "Large / Black", "price_gbp": 14.99},
     {"id": "3", "title": "Large / Red", "price_gbp": 15.99}]


class Variants(unittest.TestCase):
    def test_unique_name_match(self):
        v, how = sc_variants.match_variant(V, "Large Red heavy duty resistance band")
        self.assertEqual((v["id"], how), ("3", "name"))

    def test_ambiguous_falls_back_to_cheapest(self):
        v, how = sc_variants.match_variant(V, "Black resistance band")
        self.assertEqual((v["id"], how), ("1", "cheapest"))

    def test_no_words_falls_back_to_cheapest(self):
        v, how = sc_variants.match_variant(V, "resistance band")
        self.assertEqual((v["id"], how), ("1", "cheapest"))

    def test_one_price_is_single(self):
        v, how = sc_variants.match_variant([{"id": "9", "title": "Default Title", "price_gbp": 5.0}], "x")
        self.assertEqual((v["id"], how), ("9", "single"))

    def test_same_price_variants_are_single(self):
        vs = [{"id": "1", "title": "Red", "price_gbp": 5.0}, {"id": "2", "title": "Blue", "price_gbp": 5.0}]
        self.assertEqual(sc_variants.match_variant(vs, "x")[1], "single")

    def test_compare_uses_the_link_variant_first(self):
        m = {"variant_id": "2", "price_gbp": 14.99, "variants": V}
        self.assertEqual(sc_variants.compare_price(m, {"title": "Small"}), (14.99, "link"))

    def test_compare_uses_the_source_variation_label(self):
        m = {"variant_id": None, "price_gbp": 9.99, "variants": V}
        self.assertEqual(sc_variants.compare_price(m, {"title": "Band", "variation_label": "Large / Red"}),
                         (15.99, "name"))

    def test_compare_without_variants_uses_the_product_price(self):
        self.assertEqual(sc_variants.compare_price({"price_gbp": 7.5, "variants": []}, {"title": "x"}),
                         (7.5, "product"))

    def test_word_match_is_whole_words(self):
        vs = [{"id": "1", "title": "Red", "price_gbp": 5.0}, {"id": "2", "title": "Blue", "price_gbp": 9.0}]
        self.assertEqual(sc_variants.match_variant(vs, "Shredder tool")[1], "cheapest")


M2 = {"status": "OK", "price_gbp": 20.0, "title": "Yoga Mat",
      "variants": [{"id": "1", "title": "Default Title", "price_gbp": 20.0}]}
NO_URL = {"url": "", "platform": "none", "verdict": "NONE"}


def raw(**kw):
    d = {"url": "https://www.ebay.co.uk/itm/1", "platform": "ebay", "verdict": "PRICE", "price_gbp": 12.0,
         "available": True, "title": "Yoga Mat for women non slip", "error": None}
    d.update(kw)
    return d


GONE = dict(verdict="NOT_FOUND", price_gbp=None, available=None, error="Fetch failed: 404", http_status=404)


class Decide(unittest.TestCase):
    """The owner's rule (2026-10-23) decides a problem; nothing else does."""

    def fin(self, m, sources):
        return sc_crawl.finalize_product(m, sources)

    def test_cheaper_buyable_source_is_ok_with_no_flags(self):
        out = self.fin(M2, [raw()])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))

    def test_backup_source_gone_while_first_is_cheaper_is_ok_no_flag(self):
        out = self.fin(M2, [raw(), raw(**GONE)])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))
        self.assertIn("Backup source not buyable (Source 2)", out["notes"])

    def test_title_word_difference_does_not_flag(self):
        out = self.fin({**M2, "title": "Yoga Mat Women Men 183cm"}, [raw(title="Mat")])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))

    def test_odd_looking_margin_does_not_flag_an_ok_product(self):
        out = self.fin({**M2, "price_gbp": 90.0, "variants": []}, [raw(price_gbp=3.0)])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))

    def test_equal_price_costs_the_same_or_more(self):
        out = self.fin(M2, [raw(price_gbp=20.0)])
        self.assertEqual(out["status"], "SOURCE_DEARER")
        self.assertIn("S1_INVERTED", out["flags"])

    def test_dearer_flags_only_the_best_source(self):
        out = self.fin(M2, [raw(price_gbp=25.0), raw(price_gbp=22.0)])
        self.assertEqual((out["status"], out["best_source"]), ("SOURCE_DEARER", 2))
        self.assertEqual(out["flags"], ["S2_INVERTED"])

    def test_all_sources_gone_needs_new_source(self):
        out = self.fin(M2, [raw(**GONE), raw(verdict="OUT_OF_STOCK", price_gbp=None, available=False)])
        self.assertEqual(out["status"], "NO_BUYABLE_SOURCE")
        self.assertEqual(sorted(out["flags"]), ["S1_GONE", "S2_OOS"])

    def test_priced_but_out_of_stock_is_not_buyable(self):
        out = self.fin(M2, [raw(available=False)])
        self.assertEqual(out["status"], "NO_BUYABLE_SOURCE")
        self.assertEqual(out["flags"], ["S1_OOS"])

    def test_buyable_source_with_the_wrong_variant_uses_the_matched_variant_price(self):
        m = {"status": "OK", "price_gbp": 9.99, "title": "Band", "variant_id": None, "variants": V}
        # The source sells "Large / Red": compared with Maskura's 15.99, not the first variant's 9.99.
        out = self.fin(m, [raw(price_gbp=12.0, variation_label="Large / Red")])
        self.assertEqual(out["status"], "OK")

    def test_unnamed_variant_compares_with_the_cheapest_maskura_variant(self):
        m = {"status": "OK", "price_gbp": 15.99, "title": "Band", "variant_id": None, "variants": V}
        out = self.fin(m, [raw(price_gbp=12.0, title="Resistance band")])
        self.assertEqual(out["status"], "SOURCE_DEARER", "12.00 is not below the cheapest variant 9.99")

    def test_source_with_unnamed_variations_gets_a_note(self):
        offers = [{"id": "1", "label": "Red", "price_gbp": 5.0, "available": True}]
        out = self.fin(M2, [raw(offers=offers)])
        self.assertIn("Source 1 has variations; the link does not name one", out["notes"])

    def test_pending_is_checking(self):
        out = self.fin(M2, [None])
        self.assertEqual(out["status"], "CHECKING")
        self.assertEqual(out["flags"], ["S1_PENDING"])

    def test_empty_second_slot_is_not_checking(self):
        self.assertEqual(self.fin(M2, [raw(), dict(NO_URL)])["status"], "OK")

    def test_maskura_gone(self):
        self.assertEqual(self.fin({"status": "NOT_FOUND"}, [raw()])["status"], "MASKURA_GONE")

    def test_maskura_unreadable_keeps_m_err(self):
        out = self.fin({"status": "ERROR", "error": "boom"}, [raw()])
        self.assertEqual(out["status"], "MASKURA_UNREADABLE")
        self.assertIn("M_ERR", out["flags"])

    def test_no_maskura_link_is_not_a_crawler_problem(self):
        out = self.fin(None, [raw(**GONE)])
        self.assertEqual((out["status"], out["flags"]), ("NO_MASKURA_LINK", []))

    def test_no_source_link(self):
        out = self.fin(M2, [dict(NO_URL), dict(NO_URL)])
        self.assertEqual((out["status"], out["flags"]), ("NO_SOURCE_LINK", []))


class StoreUsesTheStatus(unittest.TestCase):
    def test_has_problem_follows_the_status(self):
        import sc_store
        self.assertTrue(sc_store.problem_from(
            {"status": "NO_BUYABLE_SOURCE", "flags": []}, maskura_status="OK"))
        self.assertFalse(sc_store.problem_from({"status": "OK", "flags": []}, maskura_status="OK"))
        self.assertFalse(sc_store.problem_from({"status": "NO_MASKURA_LINK", "flags": []}, maskura_status="NO_LINK"))
        self.assertTrue(sc_store.problem_from({"status": "MASKURA_GONE", "flags": []}, maskura_status="LINK_BROKEN"))

    def test_results_finalized_before_the_change_keep_the_old_rule(self):
        import sc_store
        self.assertTrue(sc_store.problem_from({"flags": ["S1_VARIANT"]}, maskura_status="OK"))
        self.assertTrue(sc_store.problem_from({"flags": []}, maskura_status="LINK_BROKEN"))
        self.assertFalse(sc_store.problem_from({"flags": ["S1_PENDING"]}, maskura_status="OK"))


if __name__ == "__main__":
    unittest.main()
