"""Reusing a recent result for the same listing or Maskura page instead of checking it again (owner: 7 days)."""
import datetime
import os
import tempfile
import unittest

from checker import sc_store
from test_sc_store import FAKE_URLS, FakeClock, base_rows, ebay, fake_finalize, mk


class ReuseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = sc_store.Store(os.path.join(self.tmp.name, "sc.db"), urls=FAKE_URLS, clock=self.clock,
                                    finalizer=fake_finalize)
        self.store.migrate()
        self.store.import_sheet(base_rows(), sheet_meta={}, actor="Asha")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def pid_at(self, position):
        return self.store._conn().execute("SELECT id FROM products WHERE position = ?", (position,)).fetchone()[0]

    def check_everything(self):
        job = self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        while (item := self.store.lease_item("w1", ["www.maskura.co.uk", "www.ebay.co.uk", "www.amazon.co.uk"])):
            if item["slot"] == sc_store.MASKURA_SLOT:
                raw = {"url": item["url"], "price_gbp": 20.0, "status": "OK", "fetched_at": self.store.now()}
            else:
                raw = {"url": item["url"], "verdict": "PRICE", "price_gbp": 9.0, "available": True,
                       "fetched_at": self.store.now()}
            self.store.commit_item(item["id"], item["lease_token"], raw)
        return job

    def add_row(self, position, maskura, source):
        rows = base_rows() + [{"position": position, "name": "New", "maskura_raw": maskura, "source_urls": [source]}]
        self.store.import_sheet(rows, sheet_meta={}, actor="Asha")
        return self.pid_at(position)

    def source(self, pid, slot=1):
        return dict(self.store._conn().execute(
            "SELECT * FROM source_results WHERE product_id = ? AND slot = ?", (pid, slot)).fetchone())


class Reuse(ReuseCase):
    def test_a_new_row_with_a_checked_listing_and_page_takes_both_results(self):
        self.check_everything()
        pid = self.add_row(7, mk("blanket"), ebay(111))
        got = self.store.reuse_recent_results()
        self.assertEqual((got["sources"], got["maskura"], got["finalized"]), (1, 1, 1))
        row = self.source(pid)
        self.assertEqual(row["verdict"], "PRICE")
        self.assertEqual(row["fetched_at"], self.source(self.pid_at(1))["fetched_at"], "the check time is kept")
        p = self.store._row(self.store._conn(), pid)
        self.assertEqual(p["maskura_status"], "OK")
        self.assertEqual(p["being_checked"], 0)

    def test_a_result_older_than_seven_days_is_not_reused(self):
        self.check_everything()
        self.clock.advance(days=7, seconds=1)
        pid = self.add_row(7, mk("blanket"), ebay(111))
        got = self.store.reuse_recent_results()
        self.assertEqual((got["sources"], got["maskura"]), (0, 0))
        self.assertIsNone(self.source(pid)["raw_json"])

    def test_a_different_listing_or_page_is_never_reused(self):
        self.check_everything()
        pid = self.add_row(7, mk("brand-new-page"), ebay(999))
        got = self.store.reuse_recent_results()
        self.assertEqual((got["sources"], got["maskura"], got["finalized"]), (0, 0, 0))

    def test_a_row_still_missing_one_part_is_not_finalized(self):
        self.check_everything()
        pid = self.add_row(7, mk("brand-new-page"), ebay(111))
        got = self.store.reuse_recent_results()
        self.assertEqual((got["sources"], got["maskura"], got["finalized"]), (1, 0, 0))
        self.assertEqual(self.store._row(self.store._conn(), pid)["being_checked"], 1)


class ChangedJob(ReuseCase):
    def items(self, job):
        return {(r["product_id"], r["slot"]) for r in self.store._conn().execute(
            "SELECT product_id, slot FROM job_items WHERE job_id = ?", (job,))}

    def test_only_links_without_a_fresh_result_are_queued(self):
        self.check_everything()
        reused = self.add_row(7, mk("blanket"), ebay(111))
        new = self.add_row(8, mk("brand-new-page"), ebay(999))
        job = self.store.enqueue_job("changed", actor="Asha")
        self.assertEqual(self.items(job), {(new, sc_store.MASKURA_SLOT), (new, 1)})
        info = self.store.job(job)
        self.assertEqual(info["total"], 1)
        self.assertEqual(info["params"]["reused"]["finalized"], 1)
        self.assertFalse(any(pid == reused for pid, _ in self.items(job)))

    def test_nothing_to_check_makes_a_finished_job(self):
        self.check_everything()
        job = self.store.enqueue_job("changed", actor="Asha")
        self.assertEqual(self.store.job(job)["status"], "done")

    def test_results_older_than_seven_days_are_queued_again(self):
        self.check_everything()
        self.clock.advance(days=8)
        job = self.store.enqueue_job("changed", actor="Asha")
        full = self.store.enqueue_job("full", actor="Asha")
        self.assertEqual(self.items(job), self.items(full))

    def test_check_counts_says_how_many_products_need_a_check(self):
        self.check_everything()
        self.add_row(8, mk("brand-new-page"), ebay(999))
        self.assertEqual(self.store.check_counts()["changed"], 1)
