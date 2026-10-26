"""The Source Checker worker (PLAN P5) with fake transports and fake probes: no real network, ever.

Run: .venv/bin/python -B -m unittest test_sc_worker
"""
import os
import tempfile
import threading
import time
import unittest

import httpx

from checker import sc_crawl
from checker import sc_store
from checker import sc_worker
from test_sc_store import FAKE_URLS

MK = "www.maskura.co.uk"


class FastPacer(sc_crawl.Pacer):
    """The real pacing rules with tiny gaps and cool-downs, so a test run takes seconds."""
    EBAY_MIN, EBAY_MAX, EBAY_UP, EBAY_DOWN = 0.02, 0.1, 0.01, 0.005
    OTHER_GAP, JITTER = 0.01, 0.0
    COOLDOWNS_S = (1.5, 3.0, 6.0, 12.0, 24.0)
    COOLDOWN_MAX_S = 24.0


def public_resolver(host, port, timeout):
    return ["93.184.216.34"]


def maskura_json(handle, price=20.0):
    return {"product": {"handle": handle, "title": handle.title(),
                        "variants": [{"id": 1, "title": "Default Title", "price": str(price), "available": True}]}}


def ebay_page(price=9.0):
    return ("<html><head><script type='application/ld+json'>{\"@type\": \"Product\", \"name\": \"Thing\", "
            f"\"offers\": {{\"price\": \"{price}\", \"priceCurrency\": \"GBP\", "
            "\"availability\": \"https://schema.org/InStock\"}}</script></head><body>Thing</body></html>")


class FakeSite:
    """A thread-safe MockTransport handler with switchable behavior, recording every request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = []
        self.network_down = False
        self.maskura_challenge = False
        self.ebay_403 = False
        self.dead_hosts = set()
        self.hang_hosts = set()

    def __call__(self, request):
        url = str(request.url)
        host = request.url.host
        with self.lock:
            self.seen.append(url)
        if self.network_down:
            raise httpx.ConnectError("network down")
        if host in self.hang_hosts:
            time.sleep(0.6)
            return httpx.Response(200, html="<html>slow</html>")
        if host == MK:
            if self.maskura_challenge:
                return httpx.Response(302, headers={"location": url + "?__shopify_bv_challenge=x"})
            handle = request.url.path.split("/products/")[1].removesuffix(".json")
            return httpx.Response(200, json=maskura_json(handle))
        if "ebay." in host:
            if self.ebay_403:
                return httpx.Response(403, html="<html>denied</html>")
            return httpx.Response(200, html=ebay_page(), headers={"content-type": "text/html"})
        return httpx.Response(200, html=ebay_page(), headers={"content-type": "text/html"})


def rows(n, *, extra_hosts=()):
    out = []
    for i in range(1, n + 1):
        out.append({"position": i, "name": f"Product {i}", "maskura_raw": f"https://www.maskura.co.uk/products/p{i}",
                    "source_urls": [f"https://www.ebay.co.uk/itm/{100000 + i}"]})
    for j, host in enumerate(extra_hosts, start=n + 1):
        out.append({"position": j, "name": f"Tail {j}", "maskura_raw": "", "source_urls": [f"https://{host}/item/{j}"]})
    return out


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sc_store.Store(os.path.join(self.tmp.name, "sc.db"), urls=FAKE_URLS)
        self.store.migrate()
        self.site = FakeSite()
        self.probe_ok = True
        self.probes = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def probe(self):
        self.probes += 1
        return self.probe_ok

    def worker(self, **kw):
        breaker = sc_worker.Breaker(probes=(self.probe, self.probe), retry_s=0.05)
        deadline = kw.pop("deadline_s", 5)
        factory = lambda: sc_crawl.build_client(transport=httpx.MockTransport(self.site), resolver=public_resolver,  # noqa: E731
                                                deadline_s=deadline)
        return sc_worker.Worker(self.store, client_factory=factory, pacer=FastPacer(), breaker=breaker,
                                idle_s=0.02, heartbeat_s=0.05, worker_id="test", **kw)

    def run_until(self, worker, cond, timeout=30):
        t0 = time.monotonic()
        worker.run(until=lambda: cond() or time.monotonic() - t0 > timeout)
        return time.monotonic() - t0

    def items(self):
        return [dict(r) for r in self.store._conn().execute("SELECT * FROM job_items ORDER BY id")]

    def job_done(self, job):
        return lambda: self.store.job(job)["status"] in ("done", "stopped", "failed")


class HappyPath(WorkerCase):
    def test_a_full_job_commits_every_item_once_and_finalizes_every_product(self):
        self.store.import_sheet(rows(5), sheet_meta={}, actor="Asha")
        job = self.store.enqueue_job("full", actor="Asha")
        w = self.worker()
        self.run_until(w, self.job_done(job))
        info = self.store.job(job)
        self.assertEqual((info["status"], info["done"], info["total"]), ("done", 5, 5))
        self.assertTrue(all(i["status"] == "done" and i["attempts"] == 1 for i in self.items()))
        p = self.store.get_product(self.store._conn().execute("SELECT id FROM products WHERE position = 1").fetchone()[0])
        self.assertFalse(p["being_checked"])
        self.assertEqual(p["maskura"]["price_gbp"], 20.0)
        self.assertTrue(self.store.worker_status()["alive"])

    def test_worker_threads_close_their_database_connections(self):
        import gc
        import warnings
        self.store.import_sheet(rows(3), sheet_meta={}, actor="Asha")
        job = self.store.enqueue_job("full", actor="Asha")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.run_until(self.worker(), self.job_done(job))
            gc.collect()
        leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning) and "sqlite" in str(w.message)]
        self.assertEqual(leaks, [])


class BotCheck(WorkerCase):
    def test_a_maskura_challenge_blocks_cools_down_and_never_requests_the_challenge(self):
        self.store.import_sheet(rows(3), sheet_meta={}, actor="Asha")
        self.site.maskura_challenge = True
        job = self.store.enqueue_job("full", actor="Asha")
        w = self.worker()
        ebay_done = lambda: all(i["status"] == "done" for i in self.items() if i["slot"] == 1)  # noqa: E731
        self.run_until(w, ebay_done, timeout=10)
        self.assertFalse(any("__shopify_bv_challenge" in u for u in self.site.seen), "the challenge is never fetched")
        mk_requests = [u for u in self.site.seen if MK in u]
        self.assertEqual(len(mk_requests), 1, "the cool-down starts at the first challenge")
        health = self.store.host_health()[MK]
        self.assertEqual(health["last_outcome"], "challenge")
        self.assertIsNotNone(health["cooldown_until"])
        hosts = {h["host"]: h for h in self.store.progress()["job"]["hosts"]}
        self.assertEqual(hosts[MK]["note_word"], "Maskura asked for a bot check; paused until {time}")
        self.assertTrue(ebay_done(), "eBay carried on while Maskura cooled down")
        self.assertNotEqual(self.store.job(job)["status"], "done")


class Breaker(WorkerCase):
    def test_a_network_drop_opens_the_breaker_and_writes_no_error(self):
        self.store.import_sheet(rows(3), sheet_meta={}, actor="Asha")
        self.site.network_down = True
        self.probe_ok = False
        job = self.store.enqueue_job("full", actor="Asha")
        w = self.worker()
        self.run_until(w, lambda: w.breaker.is_open and w.counts["requeued"] >= 1, timeout=10)
        self.assertTrue(w.breaker.is_open)
        self.assertEqual(w.counts["committed"] + w.counts["final"], 0, "nothing was written while the network was down")
        self.assertTrue(all(i["attempts"] == 0 for i in self.items()), "no attempt was counted")
        self.assertIsNone(self.store._conn().execute("SELECT 1 FROM source_results WHERE raw_json IS NOT NULL").fetchone())
        # The network comes back: the same worker carries on and finishes.
        self.site.network_down = False
        self.probe_ok = True
        w2 = self.worker()
        self.run_until(w2, self.job_done(job))
        self.assertEqual(self.store.job(job)["status"], "done")

    def test_ebay_refusing_everything_never_opens_the_breaker(self):
        self.store.import_sheet(rows(2), sheet_meta={}, actor="Asha")
        self.site.ebay_403 = True
        job = self.store.enqueue_job("full", actor="Asha")
        w = self.worker()
        self.run_until(w, self.job_done(job), timeout=40)
        self.assertFalse(w.breaker.is_open)
        ebay = [i for i in self.items() if i["slot"] == 1]
        self.assertTrue(all(i["status"] == "done" and i["attempts"] == sc_store.MAX_ATTEMPTS for i in ebay),
                        "three attempts, the last one final")
        raw = self.store._conn().execute("SELECT verdict FROM source_results WHERE slot = 1").fetchall()
        self.assertEqual({r[0] for r in raw}, {"BLOCKED"})

    def test_a_dead_domain_fails_its_item_without_opening_the_breaker(self):
        self.store.import_sheet(rows(1, extra_hosts=["shop.deadsupplier.example"]), sheet_meta={}, actor="Asha")

        def resolver(host, port, timeout):
            if "deadsupplier" in host:
                raise sc_crawl.TransientNetworkError(f"{host}: name not found")
            return ["93.184.216.34"]

        job = self.store.enqueue_job("full", actor="Asha")
        breaker = sc_worker.Breaker(probes=(self.probe, self.probe))
        factory = lambda: sc_crawl.build_client(transport=httpx.MockTransport(self.site), resolver=resolver)  # noqa: E731
        w = sc_worker.Worker(self.store, client_factory=factory, pacer=FastPacer(), breaker=breaker, idle_s=0.02,
                             heartbeat_s=0.05, worker_id="test")
        self.run_until(w, self.job_done(job), timeout=40)
        self.assertFalse(w.breaker.is_open, "the probes passed, so it is the host, not the network")
        dead = [i for i in self.items() if "deadsupplier" in i["host"]][0]
        self.assertEqual((dead["status"], dead["attempts"]), ("done", sc_store.MAX_ATTEMPTS))

    def test_a_clock_jump_reprobes(self):
        mono, wall = [100.0], [5000.0]
        b = sc_worker.Breaker(probes=(self.probe,), mono=lambda: mono[0], wall=lambda: wall[0])
        mono[0] += 10
        wall[0] += 10
        self.assertFalse(b.check_clock_jump())
        mono[0] += 10
        wall[0] += 3600   # the laptop slept for an hour
        self.assertTrue(b.check_clock_jump())
        self.assertEqual(self.probes, 1)


class Deadlines(WorkerCase):
    def test_a_trickling_body_is_cut_off_at_the_deadline(self):
        class Trickle(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(100):
                    time.sleep(0.05)
                    yield b"x" * 10

        transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Trickle(),
                                                                 headers={"content-type": "text/html"}))
        client = sc_crawl.build_client(transport=transport, resolver=public_resolver, deadline_s=0.4)
        t0 = time.monotonic()
        out = sc_crawl.fetch_source(client, FastPacer(), "https://www.example-shop.co.uk/item/1")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(out["verdict"], "ERROR")

    def test_fifty_hanging_tail_hosts_do_not_slow_ebay(self):
        tails = [f"tail{i}.example-shop.co.uk" for i in range(50)]
        self.store.import_sheet(rows(15, extra_hosts=tails), sheet_meta={}, actor="Asha")
        self.site.hang_hosts = set(tails)
        self.store.enqueue_job("full", actor="Asha")
        w = self.worker(deadline_s=1)
        ebay_done = lambda: all(i["status"] == "done" for i in self.items() if "ebay." in i["host"])  # noqa: E731
        elapsed = self.run_until(w, ebay_done, timeout=30)
        self.assertTrue(ebay_done())
        tail_left = sum(1 for i in self.items() if "tail" in i["host"] and i["status"] != "done")
        self.assertGreater(tail_left, 0, "the tail was still working when eBay finished")
        self.assertLess(elapsed, 8.0, f"15 eBay items took {elapsed:.1f}s next to 50 hanging hosts")


class Catalog(WorkerCase):
    def test_catalog_refresh_reads_the_sitemaps_never_products_json(self):
        seen = []

        def fetch(client, pacer):
            seen.append("sitemap")
            return [{"handle": "a", "title": "A", "image": None}]

        job = self.store.enqueue_job("catalog_refresh", actor="Asha")
        w = self.worker(catalog_fetch=fetch)
        self.run_until(w, self.job_done(job), timeout=10)
        self.assertEqual((self.store.job(job)["status"], seen), ("done", ["sitemap"]))
        self.assertEqual(self.store.catalog_info()["count"], 1)
        self.assertFalse(any("products.json" in u for u in self.site.seen))


class KillNine(unittest.TestCase):
    """kill -9 the worker mid-run, start it again: every item is committed exactly once."""

    def test_a_killed_worker_leaves_nothing_committed_twice(self):
        import signal
        import sqlite3
        import subprocess
        import sys
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the repo root
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "sc.db")
            store = sc_store.Store(db)
            store.migrate()
            store.import_sheet(rows(20), sheet_meta={}, actor="Asha")
            job = store.enqueue_job("full", actor="Asha")
            store.close()
            cmd = [sys.executable, "-B", os.path.join(here, "scripts", "dev", "fake_worker.py"), "--db", db,
                   "--delay", "0.05"]

            def done_count():
                conn = sqlite3.connect(db, timeout=5)
                try:
                    return conn.execute("SELECT COUNT(*) FROM job_items WHERE status = 'done'").fetchone()[0]
                finally:
                    conn.close()

            proc = subprocess.Popen(cmd, cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.monotonic()
            while done_count() < 8 and time.monotonic() - t0 < 20:
                time.sleep(0.05)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            before = done_count()
            self.assertGreaterEqual(before, 8)
            self.assertLess(before, 40, "killed mid-run")
            proc = subprocess.run(cmd, cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            store = sc_store.Store(db)
            try:
                info = store.job(job)
                items = [dict(r) for r in store._conn().execute("SELECT status, attempts FROM job_items")]
                self.assertEqual((info["status"], info["done"], info["total"]), ("done", 20, 20))
                self.assertEqual(len(items), 40)
                self.assertTrue(all(i["status"] == "done" for i in items))
                self.assertEqual({i["attempts"] for i in items}, {1}, "no item was committed twice")
            finally:
                store.close()


class GoNoGo(unittest.TestCase):
    def test_thresholds(self):
        self.assertIsNone(sc_worker.go_no_go(3600, 100, 90, 10), "nothing is judged before 2 hours")
        self.assertIsNone(sc_worker.go_no_go(7200, 400, 40, 2000))
        reason = sc_worker.go_no_go(7200, 400, 120, 2000)
        self.assertIn("eBay refused 30%", reason)
        self.assertIn("only 250 items an hour", sc_worker.go_no_go(7200, 400, 10, 500))
        self.assertIsNone(sc_worker.go_no_go(7200, 10, 9, 2000), "too few eBay results to judge the rate")

    def test_a_failing_run_is_paused_once_by_system_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sc_store.Store(os.path.join(tmp, "sc.db"), urls=FAKE_URLS)
            store.migrate()
            store.import_sheet(rows(2), sheet_meta={}, actor="Asha")
            job = store.enqueue_job("full", actor="Asha")
            store.activate_jobs()
            w = sc_worker.Worker(store, breaker=sc_worker.Breaker(probes=(lambda: True,)), worker_id="t")
            w.by_job[job] = {"ebay": 100, "ebay_blocked": 60, "items": 1000}
            reason = w.judge(store.job(job), 7300)
            self.assertIn("eBay refused 60%", reason)
            info = store.job(job)
            self.assertEqual((info["status"], info["paused_by"], info["pause_reason"]), ("paused", "system", reason))
            self.assertIsNone(w.judge(store.job(job), 9000), "judged once per job")
            store.close()

    def _job_store(self, tmp):
        store = sc_store.Store(os.path.join(tmp, "sc.db"), urls=FAKE_URLS)
        store.migrate()
        store.import_sheet(rows(2), sheet_meta={}, actor="Asha")
        job = store.enqueue_job("full", actor="Asha")
        store.activate_jobs()
        return store, job

    def test_a_restarted_worker_judges_the_pace_from_the_database_not_its_empty_tally(self):
        # 16 Oct 2026: go_live restarted the worker 2 h 50 min into a healthy run; the new process had seen no
        # items, called it "0 items an hour" and paused the run.
        with tempfile.TemporaryDirectory() as tmp:
            store, job = self._job_store(tmp)
            fresh = sc_worker.Worker(store, breaker=sc_worker.Breaker(probes=(lambda: True,)), worker_id="t2")
            self.assertEqual(fresh.by_job, {})
            store.items_done = lambda job_id: 2300
            self.assertIsNone(fresh.judge(store.job(job), 10200))
            self.assertEqual(store.job(job)["status"], "running")
            store.close()

    def test_the_pace_counts_items_not_products(self):
        # 23 Oct 2026: 171 products but 754 items were done at 2 hours; the gate divided products by hours, called it
        # "85 items an hour" against an items floor of 300, and paused a run that was at about 377 items an hour.
        with tempfile.TemporaryDirectory() as tmp:
            store, job = self._job_store(tmp)
            w = sc_worker.Worker(store, breaker=sc_worker.Breaker(probes=(lambda: True,)), worker_id="t3")
            store.items_done = lambda job_id: 754
            self.assertIsNone(w.judge(dict(store.job(job), done=171), 7200))
            self.assertEqual(store.job(job)["status"], "running")
            store.close()

    def test_items_done_counts_committed_items_of_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, job = self._job_store(tmp)
            item = store.lease_item("w", [h for h in store.pending_hosts()])
            store.commit_item(item["id"], item["lease_token"], {"url": item["url"], "verdict": "PRICE",
                                                                 "price_gbp": 1.0, "status": "OK", "available": True})
            self.assertEqual(store.items_done(job), 1)
            store.close()

    def test_a_run_is_judged_once_even_across_worker_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, job = self._job_store(tmp)
            first = sc_worker.Worker(store, breaker=sc_worker.Breaker(probes=(lambda: True,)), worker_id="a")
            store.items_done = lambda job_id: 10
            self.assertIn("items an hour", first.judge(store.job(job), 7300))
            store.resume_job(job, actor="Asha")
            second = sc_worker.Worker(store, breaker=sc_worker.Breaker(probes=(lambda: True,)), worker_id="b")
            self.assertIsNone(second.judge(store.job(job), 9000), "a Resume after the go/no-go sticks")
            self.assertEqual(store.job(job)["status"], "running")
            store.close()


class InhibitorLife(unittest.TestCase):
    """The sleep inhibitor ends with the worker's pid, even after a SIGKILL outside systemd."""

    def test_the_inhibitor_dies_with_a_killed_worker(self):
        import signal
        import subprocess
        import sys
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the repo root
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "systemd-inhibit")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\n# Drop the --options, then run the command, as systemd-inhibit does.\n'
                         'while [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n')
            os.chmod(fake, 0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""))
            code = ("import sys, time; sys.path.insert(0, %r); from checker import sc_worker; i = sc_worker.Inhibitor(enabled=True); "
                    "assert i.enabled; i.hold(True); print(i._proc.pid, flush=True); time.sleep(60)" % here)
            child = subprocess.Popen([sys.executable, "-B", "-c", code], env=env, stdout=subprocess.PIPE, text=True)
            inhibitor_pid = int(child.stdout.readline())
            os.kill(inhibitor_pid, 0)                     # the inhibitor is running
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            child.stdout.close()
            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(inhibitor_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.1)
            if alive:
                os.kill(inhibitor_pid, signal.SIGKILL)
            self.assertFalse(alive, "the inhibitor outlived the killed worker")


class SingleInstance(unittest.TestCase):
    def test_a_second_worker_cannot_take_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "worker.lock")
            first = sc_worker.single_instance(path)
            self.assertIsNotNone(first)
            self.assertIsNone(sc_worker.single_instance(path))
            first.close()
            again = sc_worker.single_instance(path)
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
