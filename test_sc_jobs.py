"""The job queue in sc_store (PLAN P5): enqueue, lease with a token, commit exactly once, finalize on the last
slot, retry passes, attempts capped at 3, pause/resume/stop, startup reset, progress. No network.

Run: .venv/bin/python -B -m unittest test_sc_jobs
"""
import datetime
import os
import tempfile
import unittest

import sc_store
from test_sc_store import FAKE_URLS, FakeClock, base_rows, fake_finalize

MK = "www.maskura.co.uk"
EBAY = "www.ebay.co.uk"
AMZ = "www.amazon.co.uk"


class JobCase(unittest.TestCase):
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

    def items(self, job_id):
        return [dict(r) for r in self.store._conn().execute(
            "SELECT * FROM job_items WHERE job_id = ? ORDER BY id", (job_id,))]

    def run_item(self, item, raw=None):
        """Lease-free helper: what a worker does with one leased item."""
        if item["slot"] == sc_store.MASKURA_SLOT:
            raw = raw or {"url": item["url"], "price_gbp": 20.0, "status": "OK", "fetched_at": self.store.now()}
        else:
            raw = raw or {"url": item["url"], "verdict": "PRICE", "price_gbp": 9.0, "available": True}
        return self.store.commit_item(item["id"], item["lease_token"], raw)

    def drain(self, hosts=(MK, EBAY, AMZ)):
        done = 0
        while True:
            item = self.store.lease_item("w1", list(hosts))
            if item is None:
                return done
            self.run_item(item)
            done += 1


class Enqueue(JobCase):
    def test_a_full_job_queues_every_checkable_slot_and_skips_archived(self):
        pid3 = self.pid_at(3)
        self.store.archive(pid3, "duplicate", actor="Asha", expected_version=0)
        job = self.store.enqueue_job("full", actor="Asha")
        items = self.items(job)
        slots = {(i["product_id"], i["slot"]) for i in items}
        self.assertIn((self.pid_at(1), sc_store.MASKURA_SLOT), slots)
        self.assertIn((self.pid_at(1), 2), slots, "the Amazon link of row 1")
        self.assertNotIn((self.pid_at(2), 2), slots, "an empty source cell has nothing to check")
        self.assertNotIn((self.pid_at(5), sc_store.MASKURA_SLOT), slots, "row 5 has no Maskura link")
        self.assertFalse(any(i["product_id"] == pid3 for i in items))
        info = self.store.job(job)
        self.assertEqual((info["total"], info["params"]["skipped_archived"]), (5, 1))
        self.assertEqual({i["host"] for i in items}, {MK, EBAY, AMZ})

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.enqueue_job("everything", actor="Asha")


class LeaseAndCommit(JobCase):
    def test_lease_follows_ready_hosts_and_job_priority(self):
        full = self.store.enqueue_job("full", actor="Asha")
        recheck = self.store.enqueue_job("recheck", actor="Asha", product_ids=[self.pid_at(6)], maskura_only=True,
                                         priority=sc_store.PRIORITY_TOP)
        self.store.activate_jobs()
        self.assertIsNone(self.store.lease_item("w1", ["www.example.com"]))
        item = self.store.lease_item("w1", [MK])
        self.assertEqual((item["job_id"], item["product_id"], item["slot"]), (recheck, self.pid_at(6), 0))
        self.assertEqual(item["url"], "https://www.maskura.co.uk/products/barbell-weight-set")
        self.assertTrue(item["lease_token"])
        self.assertEqual(self.store.lease_item("w1", [EBAY])["job_id"], full)

    def test_a_result_is_committed_exactly_once(self):
        self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        item = self.store.lease_item("w1", [EBAY])
        self.assertTrue(self.run_item(item)["committed"])
        self.assertFalse(self.run_item(item)["committed"], "the same lease cannot commit twice")
        self.assertFalse(self.store.commit_item(item["id"], "forged-token", {})["committed"])

    def test_a_link_changed_since_enqueue_drops_the_stale_result(self):
        self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        pid = self.pid_at(2)
        item = self.store.lease_item("w1", [MK])
        while item["product_id"] != pid:
            self.run_item(item)
            item = self.store.lease_item("w1", [MK])
        info = FAKE_URLS.normalize_maskura_url("maskura.co.uk/products/kettle-new")
        self.store.set_link(pid, info, actor="Asha", method="manual", confidence=None, expected_link_version=0)
        res = self.run_item(item)
        self.assertEqual((res["committed"], res["stale"]), (True, True))
        self.assertIsNone(self.store.get_product(pid)["maskura"])

    def test_the_last_slot_finalizes_the_product_and_finishes_the_job(self):
        job = self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        self.assertEqual(self.drain(), len(self.items(job)))
        p = self.store.get_product(self.pid_at(1))
        self.assertFalse(p["being_checked"])
        self.assertIsNotNone(p["last_crawled_at"])
        info = self.store.job(job)
        self.assertEqual((info["status"], info["done"], info["total"]), ("done", 6, 6))

    def test_a_full_job_resets_the_reviewed_total_when_it_starts(self):
        self.store._conn().execute("UPDATE products SET had_problem = 1, has_problem = 0")
        self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        self.assertEqual(self.store._conn().execute("SELECT SUM(had_problem) FROM products").fetchone()[0], 0)


class Retries(JobCase):
    def test_failed_attempts_wait_for_the_retry_pass_and_stop_at_three(self):
        self.store.enqueue_job("recheck", actor="Asha", product_ids=[self.pid_at(2)], maskura_only=True)
        self.store.activate_jobs()
        for attempt in (1, 2):
            item = self.store.lease_item("w1", [MK])
            res = self.store.release_item(item["id"], item["lease_token"], count_attempt=True, error="blocked")
            self.assertEqual((res["attempts"], res["final"]), (attempt, False))
            self.assertIsNone(self.store.lease_item("w1", [MK]), "a retry waits for the retry pass")
            self.assertEqual(self.store.retry_pass(), 1)
        item = self.store.lease_item("w1", [MK])
        self.assertEqual(item["attempts"], 2)
        self.assertTrue(item["last_try"], "the third attempt is the last: its result is final")

    def test_a_release_that_does_not_count_goes_straight_back(self):
        self.store.enqueue_job("recheck", actor="Asha", product_ids=[self.pid_at(2)], maskura_only=True)
        self.store.activate_jobs()
        item = self.store.lease_item("w1", [MK])
        res = self.store.release_item(item["id"], item["lease_token"], count_attempt=False)
        self.assertEqual(res["attempts"], 0)
        self.assertIsNotNone(self.store.lease_item("w1", [MK]))

    def test_startup_resets_leases_left_by_a_dead_worker(self):
        self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        item = self.store.lease_item("w1", [EBAY])
        self.assertEqual(self.store.reset_leases(), 1)
        self.assertFalse(self.run_item(item)["committed"], "the dead worker's token is void")
        self.assertEqual(self.store.lease_item("w2", [EBAY])["id"], item["id"])


class Controls(JobCase):
    def test_pause_resume_and_stop(self):
        job = self.store.enqueue_job("full", actor="Asha")
        self.store.activate_jobs()
        self.store.pause_job(job, actor="Sam", reason="by hand")
        self.assertIsNone(self.store.lease_item("w1", [MK, EBAY, AMZ]))
        info = self.store.job(job)
        self.assertEqual((info["status"], info["paused_by"], info["pause_reason"]), ("paused", "Sam", "by hand"))
        self.store.resume_job(job, actor="Sam")
        self.assertIsNotNone(self.store.lease_item("w1", [MK]))
        self.store.stop_job(job, actor="Sam")
        self.assertEqual(self.store.job(job)["status"], "stopped")
        self.assertIsNone(self.store.lease_item("w1", [MK, EBAY, AMZ]))
        with self.assertRaises(ValueError):
            self.store.resume_job(job, actor="Sam")

    def test_heartbeat_and_host_health(self):
        self.store.heartbeat("w1", pid=123, hostname="laptop", state={"threads": 6})
        beat = self.store.worker_status()
        self.assertTrue(beat["alive"])
        self.clock.advance(seconds=45)
        self.assertFalse(self.store.worker_status()["alive"])
        self.store.save_host_health({MK: {"gap_s": 2.0, "cooldown_until": "2026-10-16T03:20:00Z",
                                          "last_outcome": "challenge", "recent_blocks": 1}})
        self.assertEqual(self.store.host_health()[MK]["last_outcome"], "challenge")


class Progress(JobCase):
    def test_progress_counts_products_hosts_and_gives_an_eta_range(self):
        self.assertIsNone(self.store.progress()["job"])
        job = self.store.enqueue_job("full", actor="Asha")
        queued = self.store.progress()["job"]
        self.assertEqual(queued["state"], "queued")
        self.assertRegex(queued["created_at"], r"Z$", "a queued job says when it was queued")
        self.store.activate_jobs()
        for _ in range(4):
            item = self.store.lease_item("w1", [EBAY])
            self.run_item(item)
            self.clock.advance(minutes=1)
        prog = self.store.progress()["job"]
        self.assertEqual((prog["id"], prog["kind"], prog["state"]), (job, "full", "running"))
        self.assertEqual(prog["total"], 6)
        hosts = {h["host"]: h for h in prog["hosts"]}
        self.assertEqual(hosts[EBAY]["done"], 4)
        self.assertGreater(hosts[EBAY]["total"], 4)
        self.assertIsNone(prog["eta_low_s"], "no estimate while a host with work left has no rate yet")
        for host in (MK, AMZ):
            self.run_item(self.store.lease_item("w1", [host]))
            self.clock.advance(minutes=1)
        prog = self.store.progress()["job"]
        self.assertLess(prog["eta_low_s"], prog["eta_high_s"])
        self.assertTrue(prog["kind_word"])
        self.assertIn("{time}", prog["state_word"])


if __name__ == "__main__":
    unittest.main()
