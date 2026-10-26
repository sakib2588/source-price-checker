"""A source listing with variations at different prices and no variation in its link (no ?var=).

Before this change such a source was compared on the page's default price against one Maskura variant, so
a product could read OK while another variant lost money. Now each Maskura variant is compared with the
eBay variations its name can match (every variation when none match); a product is OK only when even the
dearest candidate leaves a margin, and VARIANT_UNCLEAR when the answer depends on which variation is bought.

Run: .venv/bin/python -B -m unittest test_sc_variations
"""
import unittest

from checker import sc_crawl
from checker import sc_labels
from checker import sc_review_book
from checker import sc_store
from checker import sc_variants

BLANKET = {"status": "OK", "price_gbp": 30.0, "title": "Electric Blanket", "variant_id": None,
           "variants": [{"id": "1", "title": "Single", "price_gbp": 30.0},
                        {"id": "2", "title": "Double", "price_gbp": 36.0},
                        {"id": "3", "title": "King", "price_gbp": 40.0}]}
ONE_PRICE = {"status": "OK", "price_gbp": 30.0, "title": "Steel Macebell", "variant_id": None,
             "variants": [{"id": "9", "title": "Default Title", "price_gbp": 30.0}]}


def offer(label, price, available=True):
    return {"id": label, "label": label, "price_gbp": price, "available": available}


BLANKET_OFFERS = [offer("Single - 90x190cm", 24.99), offer("Double - 120x150cm", 29.99),
                  offer("King - 137x165cm", 32.99)]
WEIGHTS = [offer("3 kg", 10.0), offer("5 kg", 25.0), offer("8 kg", 35.0)]


def src(offers, **kw):
    d = {"url": "https://www.ebay.co.uk/itm/277068622190", "platform": "ebay", "verdict": "PRICE",
         "price_gbp": min(o["price_gbp"] for o in offers if o["price_gbp"] is not None), "available": True,
         "title": "Electric blanket single double king", "error": None, "offers": offers}
    d.update(kw)
    return d


def single(price, **kw):
    d = {"url": "https://www.ebay.co.uk/itm/2", "platform": "ebay", "verdict": "PRICE", "price_gbp": price,
         "available": True, "title": "Electric blanket", "error": None}
    d.update(kw)
    return d


class VariationRange(unittest.TestCase):
    def test_every_variant_matched_and_cheaper_is_ok_on_the_worst_margin(self):
        got = sc_variants.variation_range(BLANKET, src(BLANKET_OFFERS))
        self.assertEqual(got["result"], "ok")
        self.assertAlmostEqual(got["margin"], 30.0 - 24.99)
        self.assertEqual((got["maskura_price"], got["source_price"]), (30.0, 24.99))

    def test_one_matched_variant_dearer_on_ebay_is_dearer(self):
        offers = BLANKET_OFFERS[:2] + [offer("King - 137x165cm", 41.0)]
        got = sc_variants.variation_range(BLANKET, src(offers))
        self.assertEqual(got["result"], "dearer")
        self.assertAlmostEqual(got["margin"], -1.0)
        self.assertEqual((got["maskura_price"], got["source_price"]), (40.0, 41.0))

    def test_unmatched_variant_inside_the_price_range_is_unclear(self):
        got = sc_variants.variation_range(ONE_PRICE, src(WEIGHTS))
        self.assertEqual(got["result"], "unclear")

    def test_unmatched_variant_dearer_than_every_variation_is_ok(self):
        got = sc_variants.variation_range({**ONE_PRICE, "variants": [
            {"id": "9", "title": "Default Title", "price_gbp": 40.0}]}, src(WEIGHTS))
        self.assertEqual((got["result"], got["source_price"]), ("ok", 35.0))

    def test_unmatched_variant_cheaper_than_every_variation_is_dearer(self):
        got = sc_variants.variation_range({**ONE_PRICE, "variants": [
            {"id": "9", "title": "Default Title", "price_gbp": 8.0}]}, src(WEIGHTS))
        self.assertEqual((got["result"], got["source_price"]), ("dearer", 10.0))

    def test_several_matching_variations_at_different_prices_is_unclear(self):
        offers = [offer("King / Blue", 30.0), offer("King / Red", 45.0), offer("Single / Blue", 20.0)]
        got = sc_variants.variation_range({**BLANKET, "variants": [BLANKET["variants"][2]]}, src(offers))
        self.assertEqual(got["result"], "unclear")

    def test_the_variant_in_the_maskura_link_is_the_only_one_compared(self):
        offers = [offer("Single - 90x190cm", 31.0), offer("King - 137x165cm", 32.99)]
        got = sc_variants.variation_range({**BLANKET, "variant_id": "3"}, src(offers))
        self.assertEqual(got["result"], "ok", "Single at 31.00 is dearer, but the link names King")

    def test_not_used_when_the_link_names_a_variation(self):
        self.assertIsNone(sc_variants.variation_range(BLANKET, src(BLANKET_OFFERS, variation_label="King")))

    def test_not_used_when_every_variation_has_the_same_price(self):
        same = [offer("Red", 10.0), offer("Blue", 10.0)]
        self.assertIsNone(sc_variants.variation_range(BLANKET, src(same)))

    def test_not_used_for_a_single_listing(self):
        self.assertIsNone(sc_variants.variation_range(BLANKET, single(10.0)))

    def test_variations_without_a_price_are_left_out(self):
        offers = [offer("3 kg", 10.0), offer("5 kg", None), offer("8 kg", 10.0)]
        self.assertIsNone(sc_variants.variation_range(ONE_PRICE, src(offers, price_gbp=10.0)))


# Real listing from the 24 Oct check ("Kettlebell with Soft Body"): every weight is cheaper on eBay.
KETTLEBELL = {"status": "OK", "price_gbp": 24.99, "title": "Kettlebell with Soft Body", "variant_id": None,
              "variants": [{"id": "1", "title": "4 KG", "price_gbp": 24.99},
                           {"id": "2", "title": "6 KG", "price_gbp": 24.99},
                           {"id": "3", "title": "8 KG", "price_gbp": 26.69},
                           {"id": "4", "title": "10 KG", "price_gbp": 29.99},
                           {"id": "5", "title": "Full Set (4+6+8+10)Kg", "price_gbp": 99.99}]}
KETTLEBELL_OFFERS = [offer("10KG", 19.99), offer("4KG", 14.99), offer("2 x 10KG", 39.99), offer("2 x 8KG", 38.99),
                     offer("2 x 6KG", 35.99), offer("2 x 4KG", 29.99), offer("4kg+6kg+8kg+10kg", 72.99),
                     offer("8KG", 19.99), offer("6KG", 17.99)]


class NameMatching(unittest.TestCase):
    def test_a_space_before_the_unit_still_matches(self):
        m = {**KETTLEBELL, "variants": [KETTLEBELL["variants"][3]]}
        got = sc_variants.variation_range(m, src([offer("4KG", 14.99), offer("10KG", 19.99),
                                                  offer("Full set", 72.99)]))
        self.assertEqual((got["result"], got["source_price"]), ("ok", 19.99))

    def test_the_closest_name_wins_over_a_multipack(self):
        m = {**KETTLEBELL, "variants": [KETTLEBELL["variants"][0]]}
        got = sc_variants.variation_range(m, src([offer("4KG", 14.99), offer("2 x 4KG", 29.99)]))
        self.assertEqual((got["result"], got["source_price"]), ("ok", 14.99))

    def test_real_kettlebell_listing_is_ok(self):
        out = sc_crawl.finalize_product(KETTLEBELL, [src(KETTLEBELL_OFFERS, title="Soft kettlebell")])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))


class DecideWithVariations(unittest.TestCase):
    def fin(self, m, sources):
        return sc_crawl.finalize_product(m, sources)

    def test_page_price_no_longer_hides_a_dearer_variant(self):
        # The page shows Single at 24.99, below every Maskura price, but King costs 41.00 on eBay.
        offers = BLANKET_OFFERS[:2] + [offer("King - 137x165cm", 41.0)]
        out = self.fin(BLANKET, [src(offers)])
        self.assertEqual((out["status"], out["flags"]), ("SOURCE_DEARER", ["S1_INVERTED"]))

    def test_every_variant_cheaper_is_ok_with_no_flags(self):
        out = self.fin(BLANKET, [src(BLANKET_OFFERS)])
        self.assertEqual((out["status"], out["flags"]), ("OK", []))

    def test_unclear_variant_is_a_problem_with_its_own_flag(self):
        out = self.fin(ONE_PRICE, [src(WEIGHTS)])
        self.assertEqual((out["status"], out["best_source"]), ("VARIANT_UNCLEAR", 1))
        self.assertEqual(out["flags"], ["S1_VARIANT_UNCLEAR"])
        self.assertIn("Source 1 has variations; the link does not name one", out["notes"])

    def test_a_clear_cheaper_backup_source_makes_it_ok(self):
        out = self.fin(ONE_PRICE, [src(WEIGHTS), single(12.0)])
        self.assertEqual((out["status"], out["best_source"], out["flags"]), ("OK", 2, []))

    def test_unclear_wins_over_a_dearer_backup_source(self):
        out = self.fin(ONE_PRICE, [src(WEIGHTS), single(31.0)])
        self.assertEqual((out["status"], out["best_source"]), ("VARIANT_UNCLEAR", 1))

    def test_out_of_stock_listing_with_variations_still_needs_a_new_source(self):
        out = self.fin(ONE_PRICE, [src(WEIGHTS, available=False)])
        self.assertEqual(out["status"], "NO_BUYABLE_SOURCE")


class UnclearIsAProblem(unittest.TestCase):
    def test_has_problem(self):
        self.assertTrue(sc_store.problem_from({"status": "VARIANT_UNCLEAR", "flags": []}, maskura_status="OK"))

    def test_flag_has_plain_words_and_no_change_reasons(self):
        got = sc_labels.problem("S1_VARIANT_UNCLEAR")
        self.assertEqual(got["word"], "Variation not named")
        self.assertEqual(got["severity"], "warn")
        self.assertIn("VARIANT_UNCLEAR", sc_labels.NO_CHANGE_REASONS)


class ReviewSheet(unittest.TestCase):
    def row(self, m, sources):
        p = {"maskura_status": "OK", "maskura_result_json": __import__("json").dumps(m)}
        srcs = [(i, s["url"], s, None) for i, s in enumerate(sources, 1)]
        return srcs, sc_review_book.decide(p, srcs)

    def test_unclear_has_a_status_row_and_a_reason(self):
        self.assertIn("VARIANT_UNCLEAR", sc_review_book.STATUS)
        srcs, (m, status, best, notes, mp, how, sp) = self.row(ONE_PRICE, [src(WEIGHTS)])
        self.assertEqual((status, how), ("VARIANT_UNCLEAR", "variations"))
        self.assertEqual(sc_review_book.reason_text(status, srcs, mp, best, sp),
                         "Variation not named (Source 1 has variations from £10.00 to £35.00)")

    def test_dearer_variant_shows_the_prices_that_decided_it(self):
        offers = BLANKET_OFFERS[:2] + [offer("King - 137x165cm", 41.0)]
        srcs, (m, status, best, notes, mp, how, sp) = self.row(BLANKET, [src(offers)])
        self.assertEqual((status, mp, sp, how), ("SOURCE_DEARER", 40.0, 41.0, "variations"))
        self.assertEqual(sc_review_book.reason_text(status, srcs, mp, best, sp),
                         "Source costs the same or more (Maskura £40.00, Source 1 £41.00)")

    def test_single_listing_rows_are_unchanged(self):
        _, (m, status, best, notes, mp, how, sp) = self.row(BLANKET, [single(31.0)])
        self.assertEqual((status, mp, sp, how), ("SOURCE_DEARER", 30.0, 31.0, "cheapest"))


if __name__ == "__main__":
    unittest.main()
