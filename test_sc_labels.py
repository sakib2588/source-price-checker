"""sc_labels: the one place for user-facing words. Pure, no network.

Run: .venv/bin/python -B -m unittest test_sc_labels
"""
import os
import re
import unittest

import sc_crawl
import sc_labels as L

SEVERITIES = {"bad", "warn", "ok", "neutral"}
GLYPHS = tuple(chr(cp) for cp in (0xA7, 0xB6, 0x2020, 0x2021))   # banned glyphs, by codepoint


class ProblemWords(unittest.TestCase):
    def test_glossary_words_verbatim(self):
        expected = {
            "INVERTED": ("Source costs more than Maskura price", "bad"),
            "OOS": ("Source out of stock", "warn"),
            "GONE": ("Listing removed", "warn"),
            "VARIANT": ("Variant may differ", "warn"),
            "VARIATION": ("Variation not checked", "warn"),
            "PRICE_SUSPECT": ("Price looks wrong", "warn"),
            "CURRENCY": ("Not in pounds", "warn"),
            "ERR": ("Could not read", "warn"),
            "PENDING": ("Being checked", "neutral"),
            "BLOCKED_RETRY": ("Being checked", "neutral"),
        }
        for suffix, (word, severity) in expected.items():
            with self.subTest(suffix=suffix):
                p = L.problem(f"S2_{suffix}", "ebay")
                self.assertEqual((p["word"], p["severity"]), (word, severity))
                self.assertEqual(p["code"], f"S2_{suffix}")
                self.assertEqual(p["source_label"], "eBay, link 2")

    def test_maskura_error_code(self):
        p = L.problem("M_ERR")
        self.assertEqual(p["source_label"], "Maskura")
        self.assertEqual(p["severity"], "warn")
        self.assertTrue(p["word"])

    def test_source_label_forms(self):
        self.assertEqual(L.problem("S1_OOS", "amazon")["source_label"], "Amazon, link 1")
        self.assertEqual(L.problem("S3_OOS", "eBay")["source_label"], "eBay, link 3")
        self.assertEqual(L.problem("S4_OOS", "generic")["source_label"], "Other site, link 4")
        self.assertEqual(L.problem("S4_OOS")["source_label"], "Link 4")

    def test_unknown_code_does_not_crash(self):
        p = L.problem("S1_SOMETHING_NEW", "ebay")
        self.assertIn(p["severity"], SEVERITIES)
        self.assertTrue(p["word"])

    def test_problems_sorted_most_severe_first_and_skip_transient(self):
        out = L.problems(["S1_OOS", "S2_INVERTED", "S3_PENDING"], ["ebay", "amazon", "ebay"])
        self.assertEqual([p["code"] for p in out], ["S2_INVERTED", "S1_OOS"])
        self.assertEqual(out[0]["source_label"], "Amazon, link 2")

    def test_problems_accept_the_legacy_joined_string(self):
        out = L.problems("S1_OOS; S1_ERR", ["ebay"])
        self.assertEqual([p["code"] for p in out], ["S1_OOS", "S1_ERR"])

    def test_row_problem_cell(self):
        self.assertEqual(L.row_problem_cell([], []),
                         {"word": "No problem found", "severity": "ok", "more": 0})
        self.assertEqual(L.row_problem_cell(["S1_OOS", "S2_INVERTED", "S2_VARIANT"], ["ebay", "ebay"]),
                         {"word": "Source costs more than Maskura price", "severity": "bad", "more": 2})
        cell = L.row_problem_cell(["S1_OOS", "S2_PENDING"], ["ebay", "ebay"])
        self.assertEqual((cell["word"], cell["severity"]), ("Being checked", "neutral"))
        self.assertEqual(L.row_problem_cell([], [], being_checked=True),
                         {"word": "Being checked", "severity": "neutral", "more": 0},
                         "imported but never crawled: no flags, still not clean")
        self.assertEqual(L.row_problem_cell(["S1_OOS"], ["ebay"], being_checked=True)["more"], 1)


class PageWords(unittest.TestCase):
    def test_filter_options_use_the_status_words(self):
        opts = L.filter_options()
        self.assertIn(("INVERTED", "Source costs more than Maskura price"), opts["problem"])
        self.assertNotIn("PENDING", [k for k, _ in opts["problem"]])
        self.assertEqual([k for k, _ in opts["maskura"]], list(L.MASKURA_STATUS))
        self.assertEqual([k for k, _ in opts["review"]], list(L.REVIEW_STATUS))

    def test_every_store_event_kind_has_a_phrase(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "sc_store.py"), encoding="utf-8") as fh:
            src = fh.read()
        kinds = set(re.findall(r'_event\(c, [a-z_\[\]"]+, "([a-z_]+)"', src))
        kinds |= set(re.findall(r'_apply_(?:review|link)_change\(c, p, "([a-z_]+)"', src))
        self.assertTrue(kinds)
        self.assertFalse(kinds - set(L.EVENT_WORDS), kinds - set(L.EVENT_WORDS))
        self.assertEqual(L.event_word("something_new"), "Changed")
        self.assertEqual(L.event_word("review", {"review_status": ["to_review", "no_change_needed"]}),
                         "Review: No change needed")
        self.assertEqual(L.event_word("review", {}), "Review changed")

    def test_tiles_have_words(self):
        for tile in L.TILES:
            self.assertTrue(tile["label"] and tile["noun"] and tile["hint"])


class EveryEmittedCodeHasALabel(unittest.TestCase):
    def test_declared_suffixes_are_labeled(self):
        labeled = set(L.all_problem_suffixes())
        self.assertTrue(set(sc_crawl.EMITTED_SUFFIXES) <= labeled,
                        set(sc_crawl.EMITTED_SUFFIXES) - labeled)

    def test_codes_from_real_finalize_runs_are_labeled(self):
        m = {"title": "Widget Black 2 Pack", "price_gbp": 10.0, "status": "OK"}
        raws = [
            {"url": "https://www.ebay.co.uk/itm/123456789", "platform": "ebay", "verdict": "PRICE",
             "price_gbp": 12.0, "title": "Widget Red", "available": False, "error": None},
            {"url": "https://www.amazon.com/dp/B012345678?var=1", "platform": "amazon", "verdict": "NON_GBP",
             "price_gbp": None, "error": None},
            {"url": "https://x.example/p", "platform": "generic", "verdict": "NOT_FOUND",
             "error": "Fetch failed: 404", "http_status": 404},
            {"url": "https://www.ebay.co.uk/itm/223456789?var=12", "platform": "ebay", "verdict": "PRICE",
             "price_gbp": 0.2, "variation_unverified": True, "title": "Widget Black 2 Pack"},
            {"url": "https://www.ebay.co.uk/itm/323456789", "platform": "ebay", "verdict": "BLOCKED",
             "error": "BLOCKED: x", "retry_pending": True},
            None,
            {"url": "", "platform": "none", "verdict": "NONE"},
            {"url": "https://y.example/p", "platform": "generic", "verdict": "ERROR", "price_gbp": None,
             "error": "Connection reset"},
        ]
        out = sc_crawl.finalize_product(m, raws)
        out2 = sc_crawl.finalize_product({"status": "ERROR", "error": "boom"}, [None])
        codes = out["flags"] + out2["flags"]
        suffixes = {c if c.startswith("M_") else c.split("_", 1)[1] for c in codes}
        self.assertTrue({"INVERTED", "OOS", "GONE", "VARIANT", "VARIATION", "PRICE_SUSPECT", "CURRENCY",
                         "ERR", "PENDING", "BLOCKED_RETRY", "M_ERR"} <= suffixes, suffixes)
        for code in codes:
            with self.subTest(code=code):
                suffix = code if code.startswith("M_") else code.split("_", 1)[1]
                self.assertIn(suffix, L.all_problem_suffixes())


class Tables(unittest.TestCase):
    def test_maskura_status(self):
        expected = {
            "OK": ("OK", "ok"),
            "LINK_CHANGED_CONFIRM": ("Link changed: confirm", "warn"),
            "LINK_CHANGED_SUGGESTION": ("Link changed: suggestion", "warn"),
            "READ_ERROR": ("Could not read Maskura page", "warn"),
            "NOT_ON_MASKURA": ("Not on Maskura", "bad"),
            "NO_LINK": ("No link", "neutral"),
            "LINK_BROKEN": ("Maskura link broken", "warn"),
        }
        self.assertEqual(set(L.MASKURA_STATUS), set(expected))
        for key, (word, sev) in expected.items():
            self.assertEqual((L.MASKURA_STATUS[key]["word"], L.MASKURA_STATUS[key]["severity"]), (word, sev))

    def test_review_status(self):
        expected = {
            "to_review": ("To review", "neutral"),
            "no_change_needed": ("No change needed", "ok"),
            "fixed_rechecking": ("Fixed: re-checking", "warn"),
            "fixed_confirmed": ("Fixed, confirmed", "ok"),
            "still_differs": ("Still differs", "bad"),
            "changed_since_review": ("Changed since review", "warn"),
        }
        self.assertEqual(set(L.REVIEW_STATUS), set(expected))
        for key, (word, sev) in expected.items():
            self.assertEqual((L.REVIEW_STATUS[key]["word"], L.REVIEW_STATUS[key]["severity"]), (word, sev))

    def test_archive_reasons(self):
        self.assertEqual(L.ARCHIVE_REASONS, {"not_sold": "Not sold any more", "duplicate": "Duplicate",
                                             "not_a_product": "Not a product row", "other": "Other"})

    def test_no_change_reasons_cover_every_final_problem(self):
        for suffix in L.all_problem_suffixes():
            if suffix in L.TRANSIENT_SUFFIXES:
                continue
            with self.subTest(suffix=suffix):
                reasons = L.NO_CHANGE_REASONS[suffix]
                self.assertTrue(3 <= len(reasons) <= 5)
                self.assertEqual(len(set(reasons)), len(reasons))

    def test_being_checked(self):
        self.assertEqual(L.BEING_CHECKED, {"word": "Being checked", "severity": "neutral"})

    def test_run_state_templates_only_use_time_and_name(self):
        self.assertTrue(L.RUN_STATE)
        for key, template in L.RUN_STATE.items():
            with self.subTest(state=key):
                fields = set(re.findall(r"{(\w*)}", template))
                self.assertTrue(fields <= {"time", "name"}, fields)
                template.format(time="10:00 AM", name="eBay")

    def test_legend_rows(self):
        rows = L.legend()
        words = {r["word"] for r in rows}
        for w in ("Source costs more than Maskura price", "No problem found", "Being checked",
                  "Not on Maskura", "Maskura link broken", "Still differs", "Not sold any more"):
            self.assertIn(w, words)
        for r in rows:
            self.assertEqual(set(r), {"column", "word", "severity", "meaning"})
            self.assertIn(r["severity"], SEVERITIES | {"faint"})
            self.assertTrue(r["meaning"])
        broken = next(r for r in rows if r["word"] == "Maskura link broken")
        self.assertEqual(broken["meaning"],
                         "The Maskura page at this link is gone. A link check will look for its new address.")

    def test_no_banned_glyphs_or_british_spelling(self):
        import inspect
        src = inspect.getsource(L)
        for g in GLYPHS:
            self.assertNotIn(g, src)
        self.assertIsNone(re.search(r"\b(colour|behaviour|catalogue|cancelled|labelled)\b", src, re.I))


if __name__ == "__main__":
    unittest.main()
