"""Tests for sc_store. The sc_urls and sc_crawl functions are small fakes that follow INTERFACES.md."""
import datetime
import multiprocessing
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import sc_store

ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


# ── fakes (contract: INTERFACES.md sc_urls / sc_crawl.finalize_product) ──────

class FakeUrls:
    @staticmethod
    def normalize_maskura_url(raw):
        text = str(raw or "").strip()
        out = {"url": None, "handle": None, "variant_id": None, "raw": raw, "ok": False, "reason": None}
        if not text:
            out["reason"] = "empty"
            return out
        candidate = text if re.match(r"https?://", text, re.I) else "https://" + text
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if " " in text or host not in ("maskura.co.uk", "www.maskura.co.uk"):
            out["reason"] = "not a Maskura link"
            return out
        m = re.search(r"/products/([^/?#]+)", parts.path)
        if not m:
            out["reason"] = "not a product link"
            return out
        handle = m.group(1).lower()
        variant = (parse_qs(parts.query).get("variant") or [None])[0]
        url = f"https://www.maskura.co.uk/products/{handle}" + (f"?variant={variant}" if variant else "")
        return dict(out, url=url, handle=handle, variant_id=variant, ok=True)

    @staticmethod
    def host_of(url):
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    @staticmethod
    def canonical_source_id(url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        m = re.search(r"/itm/(\d+)", parts.path)
        if "ebay." in host and m:
            return f"ebay:{m.group(1)}"
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parts.path, re.I)
        if "amazon." in host and m:
            return f"{host.removeprefix('www.')}:{m.group(1)}".lower()
        return f"{host}{parts.path.rstrip('/')}".lower()

    @staticmethod
    def source_signature(urls):
        return "|".join(sorted(FakeUrls.canonical_source_id(u) for u in urls if u))


FAKE_URLS = FakeUrls()


def fake_finalize(maskura, sources):
    flags, out, verdicts, being_checked = [], [], [], False
    mprice = (maskura or {}).get("price_gbp")
    if maskura and maskura.get("status") not in (None, "OK"):
        flags.append("M_ERR")
    for i, s in enumerate(sources, 1):
        if s is None:
            out.append(None)
            continue
        if s.get("verdict") == "NONE":
            out.append(dict(s))
            continue
        d, sprice = dict(s), s.get("price_gbp")
        d["price_diff"] = round(mprice - sprice, 2) if mprice is not None and sprice is not None else None
        d["pct_vs_source"] = None
        d["notes"] = ""
        if s.get("available") is False:
            flags.append(f"S{i}_OOS")
        if d["price_diff"] is not None and d["price_diff"] < 0:
            flags.append(f"S{i}_INVERTED")
        if s.get("verdict") == "ERROR":
            flags.append(f"S{i}_ERR")
        if s.get("verdict") == "BLOCKED":
            flags.append(f"S{i}_BLOCKED_RETRY")
            being_checked = True
        verdicts.append(s.get("verdict"))
        out.append(d)
    worst = next((v for v in verdicts if v != "PRICE"), "PRICE" if verdicts else None)
    return {"sources": out, "flags": flags, "worst_verdict": worst, "being_checked": being_checked}


class FakeClock:
    def __init__(self):
        self.t = datetime.datetime(2026, 10, 16, 3, 12, 0, 500000, tzinfo=datetime.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += datetime.timedelta(**kw)


def ebay(n):
    return f"https://www.ebay.co.uk/itm/{n}?hash=item{n}"


def amazon(asin):
    return f"https://www.amazon.co.uk/dp/{asin}?ref=x"


def mk(url):
    return f"https://www.maskura.co.uk/products/{url}?_pos=1&_psq=x"


def base_rows():
    return [
        {"position": 1, "name": "Blanket", "maskura_raw": mk("blanket"), "source_urls": [ebay(111), amazon("B000000001")]},
        {"position": 2, "name": "Kettle", "maskura_raw": mk("kettle"), "source_urls": [ebay(222), ""]},
        {"position": 3, "name": "Mug red", "maskura_raw": mk("mug"), "source_urls": [ebay(333), ""]},
        {"position": 4, "name": "Mug blue", "maskura_raw": mk("mug"), "source_urls": [ebay(444), ""]},
        {"position": 5, "name": "Leggings", "maskura_raw": "Workout Leggings (maskura.co.", "source_urls": [ebay(555)]},
        {"position": 6, "name": "Barbell", "maskura_raw": "maskura.co.uk/products/barbell-weight-set",
         "source_urls": [ebay(666), ""]},
    ]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "source_check.db")
        self.clock = FakeClock()
        self.store = sc_store.Store(self.path, urls=FAKE_URLS, clock=self.clock)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def load(self, rows=None):
        return self.store.import_sheet(rows or base_rows(), sheet_meta={"name": "t.csv"}, actor="Asha")

    def pid_at(self, position):
        return self.store._conn().execute("SELECT id FROM products WHERE position = ?", (position,)).fetchone()[0]

    def crawl(self, pid, *, mprice=20.0, sources=None, status="OK"):
        """Record a Maskura result and source results, then finalize. Returns finalize()'s result."""
        p = self.store.get_product(pid)
        if p["link_ok"]:
            self.assertTrue(self.store.record_maskura(
                pid, {"url": p["maskura_url"], "price_gbp": mprice, "status": status, "fetched_at": self.store.now()},
                link_version=p["link_version"]))
        for slot, raw in (sources or {}).items():
            self.store.record_source(pid, slot, dict(raw, url=p["sources"][slot - 1]["url"]))
        p = self.store.get_product(pid)
        return self.store.finalize(pid, fake_finalize(p["maskura"], [s["raw"] for s in p["sources"]]))

    def version(self, pid):
        return self.store.get_product(pid)["review_version"]

    def crawl_all(self):
        """Give every product a first, clean result, so review actions are allowed (not "Being checked")."""
        for (pid,) in self.store._conn().execute("SELECT id FROM products ORDER BY id").fetchall():
            self.crawl(pid)


def _mp_migrate(path, barrier, results):
    store = sc_store.Store(path, urls=FAKE_URLS)
    barrier.wait()
    try:
        store.migrate()
        results.put(("ok", store.check_version()))
    except Exception as exc:  # reported to the parent
        results.put(("error", repr(exc)))
    finally:
        store.close()


class TestConnection(StoreCase):
    def test_pragmas_on_every_thread(self):
        seen = []

        def probe():
            seen.append((self.store.pragma("journal_mode"), self.store.pragma("synchronous"),
                         self.store.pragma("busy_timeout"), self.store.pragma("foreign_keys"),
                         id(self.store._conn())))

        def probe_and_close():
            probe()
            self.store.close()
        t = threading.Thread(target=probe_and_close)
        t.start()
        t.join()
        probe()
        self.assertEqual([s[:4] for s in seen], [("wal", 1, 5000, 1)] * 2)
        self.assertNotEqual(seen[0][4], seen[1][4], "each thread has its own connection")

    def test_all_tables_exist(self):
        names = {r[0] for r in self.store._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("sheets", "sheet_rows", "products", "source_results", "product_aliases", "product_events",
                      "batches", "view_snapshots", "jobs", "job_items", "worker_heartbeat", "host_health",
                      "maskura_catalog", "meta"):
            self.assertIn(table, names)
        cols = {r[1]: r[2] for r in self.store._conn().execute("PRAGMA table_info(products)")}
        self.assertEqual(cols["id"], "INTEGER")
        for col in ("review_version", "result_fingerprint_at_review", "claimed_by", "claimed_at", "link_version",
                    "variant_id", "maskura_status", "suggested_url", "link_confirmed_by", "present_in_sheet",
                    "duplicate_group", "last_crawled_at", "archive_reason", "results_version"):
            self.assertIn(col, cols)

    def test_migrate_is_idempotent_and_versioned(self):
        self.assertFalse(self.store.migrate()["changed"])
        self.assertEqual(self.store.check_version(), sc_store.SCHEMA_VERSION)

    def test_check_version_raises_on_mismatch(self):
        fresh = sc_store.Store(os.path.join(self.tmp.name, "fresh.db"), urls=FAKE_URLS)
        with self.assertRaises(sc_store.SchemaMismatch):
            fresh.check_version()
        self.store._conn().execute(f"PRAGMA user_version = {sc_store.SCHEMA_VERSION + 1}")
        with self.assertRaises(sc_store.SchemaMismatch):
            self.store.check_version()
        with self.assertRaises(sc_store.SchemaMismatch):
            self.store.migrate()
        fresh.close()

    def test_a_version_1_database_upgrades_in_place(self):
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        for stmt in sc_store.MIGRATIONS[1]:
            conn.execute(stmt)
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        old = sc_store.Store(path, urls=FAKE_URLS, clock=self.clock)
        self.assertEqual(old.migrate(), {"from": 1, "to": sc_store.SCHEMA_VERSION, "changed": True})
        old.import_sheet(base_rows(), sheet_meta={}, actor="Asha")
        snap = old.create_snapshot({}, actor="Asha", sort="-row")
        self.assertEqual(old.snapshot_info(snap)["sort"], "-row")
        old.close()

    def test_migrate_lets_a_database_from_16_oct_queue_a_changed_job(self):
        # The live database was created with the job kinds of 16 Oct in its CHECK rule.
        path = os.path.join(self.tmp.name, "v3.db")
        old = sc_store.Store(path, urls=FAKE_URLS, clock=self.clock)
        old.migrate()
        old.import_sheet(base_rows(), sheet_meta={}, actor="Asha")
        old.enqueue_job("full", actor="Asha")
        old.close()
        conn = sqlite3.connect(path, isolation_level=None)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()[0]
        v = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'jobs'",
                     (sql.replace(",'changed'", ""),))
        conn.execute(f"PRAGMA schema_version = {v + 1}")
        conn.execute("PRAGMA writable_schema = OFF")
        items = conn.execute("SELECT COUNT(*) FROM job_items").fetchone()[0]
        conn.close()
        st = sc_store.Store(path, urls=FAKE_URLS, clock=self.clock)
        self.assertEqual(st.migrate()["from"], sc_store.SCHEMA_VERSION, "no version bump, so a rollback still opens it")
        st.enqueue_job("changed", actor="Asha")
        c = st._conn()
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM job_items").fetchone()[0], items)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO jobs(kind, created_by, created_at) VALUES ('bogus', 'x', 'x')")
        st.close()

    def test_two_processes_migrating_at_once(self):
        path = os.path.join(self.tmp.name, "race.db")
        ctx = multiprocessing.get_context("spawn")
        barrier, results = ctx.Barrier(3), ctx.Queue()
        procs = [ctx.Process(target=_mp_migrate, args=(path, barrier, results)) for _ in range(3)]
        for p in procs:
            p.start()
        got = [results.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(timeout=30)
        self.assertEqual(got, [("ok", sc_store.SCHEMA_VERSION)] * 3)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], sc_store.SCHEMA_VERSION)
        conn.close()

    def test_write_waits_for_a_held_lock(self):
        other = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        threading.Timer(0.3, lambda: other.execute("COMMIT")).start()
        started = time.monotonic()
        self.store.set_meta("probe", "1")
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(self.store.get_meta("probe"), "1")
        other.close()

    def test_times_are_utc_whole_seconds_with_z(self):
        self.assertTrue(ISO_RE.match(sc_store.utc_iso()))
        self.assertEqual(self.store.now(), "2026-10-16T03:12:00Z")
        self.load()
        self.crawl(self.pid_at(1))
        p = self.store.get_product(self.pid_at(1))
        ok, p, _ = self.store.set_review(p["id"], "no_change_needed", actor="Asha", expected_version=0, reason="r")
        for value in (p["created_at"], p["updated_at"], p["review_at"]):
            self.assertTrue(ISO_RE.match(value), value)
        self.assertTrue(all(ISO_RE.match(e["at"]) for e in self.store.history(p["id"])))


class TestImport(StoreCase):
    def test_first_import_report(self):
        report = self.load()
        c = report["counts"]
        self.assertEqual((c["new"], c["matched"], c["gone"], c["no_link"]), (6, 0, 0, 1))
        self.assertEqual(c["possible_duplicates"], 0, "rows that share a handle but not sources are not duplicates")
        self.assertEqual(report["samples"]["no_link"][0]["position"], 5)
        p6 = self.store.get_product(self.pid_at(6))
        self.assertEqual(p6["maskura_url"], "https://www.maskura.co.uk/products/barbell-weight-set")
        p5 = self.store.get_product(self.pid_at(5))
        self.assertEqual((p5["maskura_status"], p5["sheet_maskura_raw"]), ("NO_LINK", "Workout Leggings (maskura.co."))

    def test_identical_reupload_matches_everything(self):
        self.load()
        ids = [self.pid_at(i) for i in range(1, 7)]
        seq = self.store.summary()["results_version"]
        report = self.load()
        self.assertEqual(report["counts"]["matched"], 6)
        self.assertEqual(report["counts"]["new"] + report["counts"]["gone"], 0)
        self.assertEqual([self.pid_at(i) for i in range(1, 7)], ids)
        changed = self.store.changes(self.store.create_snapshot({}, actor="Asha"), seq, 10 ** 6)["ids"]
        self.assertEqual(changed, [])

    def test_changed_ebay_url_keeps_history(self):
        self.load()
        pid = self.pid_at(2)
        self.crawl(pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        self.store.set_review(pid, "no_change_needed", actor="Asha", expected_version=0, reason="fine")
        rows = base_rows()
        rows[1]["source_urls"] = [ebay(999), ""]
        report = self.load(rows)
        self.assertEqual(report["counts"]["new"], 0)
        p = self.store.get_product(self.pid_at(2))
        self.assertEqual(p["id"], pid)
        self.assertEqual(p["sources"][0]["url"], ebay(999))
        self.assertIsNone(p["sources"][0]["raw"], "the old listing's result is cleared")
        self.assertEqual(p["sources"][0]["url_version"], 1)
        self.assertEqual(p["review_status"], "no_change_needed")
        kinds = [e["kind"] for e in self.store.history(pid)]
        self.assertIn("source_url_changed", kinds)
        self.assertIn("review", kinds)

    def test_rows_sharing_a_handle_are_split_by_sources(self):
        self.load()
        red, blue = self.pid_at(3), self.pid_at(4)
        rows = base_rows()
        rows[2]["position"], rows[3]["position"] = 4, 3     # the two mug rows swap places
        self.load(rows)
        self.assertEqual(self.store.get_product(red)["name"], "Mug red")
        self.assertEqual(self.store.get_product(red)["position"], 4)
        self.assertEqual(self.store.get_product(blue)["position"], 3)

    def test_reupload_after_auto_fix_keeps_the_fix(self):
        self.load()
        pid = self.pid_at(1)
        fix = FAKE_URLS.normalize_maskura_url("https://www.maskura.co.uk/products/blanket-king")
        ok, p, _ = self.store.set_link(pid, fix, actor="system (auto)", method="auto", confidence=0.95,
                                       expected_link_version=0)
        self.assertTrue(ok)
        report = self.load()
        self.assertEqual(report["counts"]["new"], 0)
        self.assertEqual(report["counts"]["link_differs"], 1)
        p = self.store.get_product(pid)
        self.assertEqual(p["maskura_url"], fix["url"])
        self.assertEqual((p["link_source"], p["maskura_status"]), ("auto", "LINK_CHANGED_CONFIRM"))
        # The sheet later carries the new link, then the original again: the alias still finds the product.
        rows = base_rows()
        rows[0]["maskura_raw"] = fix["url"]
        self.assertEqual(self.load(rows)["counts"]["new"], 0)
        report = self.load()
        self.assertEqual(report["counts"]["new"], 0)
        self.assertEqual(self.store.get_product(pid)["maskura_url"], fix["url"])

    def test_gone_archived_and_duplicates(self):
        self.load()
        archived = self.pid_at(2)
        self.store.archive(archived, "not_sold", actor="Asha", expected_version=0)
        rows = [r for r in base_rows() if r["position"] != 6]
        rows.append({"position": 7, "name": "Blanket again", "maskura_raw": mk("blanket"),
                     "source_urls": [ebay(111), amazon("B000000001")]})
        report = self.load(rows)
        c = report["counts"]
        self.assertEqual((c["gone"], c["archived_in_sheet"], c["new"], c["possible_duplicates"]), (1, 1, 1, 2))
        gone = self.store.get_product(self.pid_at(6))
        self.assertEqual(gone["tags"], ["no_longer_in_sheet"])
        self.assertIn("possible_duplicate", self.store.get_product(self.pid_at(7))["tags"])

    def test_dry_run_writes_nothing(self):
        self.load()
        conn = self.store._conn()
        before = [tuple(r) for r in conn.execute("SELECT * FROM products ORDER BY id")]
        events = conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0]
        rows = base_rows()[:3]
        report = self.store.import_sheet(rows, sheet_meta={}, actor="Asha", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["counts"]["gone"], 3)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM products ORDER BY id")], before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0], events)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0], 1)

    def test_duplicate_positions_are_refused(self):
        rows = base_rows()
        rows[1]["position"] = 1
        with self.assertRaises(ValueError):
            self.load(rows)


class TestResults(StoreCase):
    def setUp(self):
        super().setUp()
        self.load()
        self.pid = self.pid_at(2)

    def test_record_maskura_stale_link_version(self):
        raw = {"url": "x", "price_gbp": 9.99, "status": "OK"}
        self.assertFalse(self.store.record_maskura(self.pid, raw, link_version=5))
        self.assertIsNone(self.store.get_product(self.pid)["maskura"])
        self.assertTrue(self.store.record_maskura(self.pid, raw, link_version=0))
        self.assertEqual(self.store.get_product(self.pid)["maskura_status"], "OK")

    def test_record_source_keeps_raw_only(self):
        self.store.record_source(self.pid, 1, {"url": ebay(222), "verdict": "PRICE", "price_gbp": 4.0,
                                               "price_diff": 1.0, "notes": "x", "pct_vs_source": 3.0})
        raw = self.store.get_product(self.pid)["sources"][0]["raw"]
        self.assertEqual(raw["price_gbp"], 4.0)
        for key in ("price_diff", "notes", "pct_vs_source"):
            self.assertNotIn(key, raw)
        with self.assertRaises(sc_store.StaleWrite):
            self.store.record_source(self.pid, 1, {"url": ebay(12345), "verdict": "PRICE"})

    def test_record_source_lease_token(self):
        c = self.store._conn()
        now = self.store.now()
        c.execute("INSERT INTO jobs(kind, created_by, created_at) VALUES ('full', 'Asha', ?)", (now,))
        c.execute("INSERT INTO job_items(job_id, product_id, slot, status, lease_token, created_at, updated_at) "
                  "VALUES (1, ?, 1, 'leased', 'tok', ?, ?)", (self.pid, now, now))
        with self.assertRaises(sc_store.StaleWrite):
            self.store.record_source(self.pid, 1, {"verdict": "PRICE"}, lease_token="wrong")
        self.store.record_source(self.pid, 1, {"verdict": "PRICE"}, lease_token="tok")
        with self.assertRaises(sc_store.StaleWrite):
            self.store.record_source(self.pid, 1, {"verdict": "PRICE"}, lease_token="tok")

    def test_identical_recrawl_keeps_version_and_review(self):
        src = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}}
        self.assertTrue(self.crawl(self.pid, sources=src)["changed"])
        p = self.store.get_product(self.pid)
        ok, _, _ = self.store.set_review(self.pid, "no_change_needed", actor="Asha",
                                         expected_version=p["review_version"], reason="fine")
        self.assertTrue(ok)
        result = self.crawl(self.pid, sources=src)
        p2 = self.store.get_product(self.pid)
        self.assertFalse(result["changed"])
        self.assertEqual(p2["results_version"], p["results_version"])
        self.assertEqual(p2["review_status"], "no_change_needed")
        self.assertTrue(ISO_RE.match(p2["last_crawled_at"]))

    def test_price_tolerance(self):
        src = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}}
        self.crawl(self.pid, mprice=10.0, sources=src)
        fp = self.store.get_product(self.pid)["result_fingerprint"]
        self.assertFalse(self.crawl(self.pid, mprice=10.45, sources=src)["changed"], "inside 50p")
        self.assertEqual(self.store.get_product(self.pid)["result_fingerprint"], fp)
        self.assertTrue(self.crawl(self.pid, mprice=10.60, sources=src)["changed"], "outside 50p")
        self.crawl(self.pid, mprice=100.0, sources=src)
        self.assertFalse(self.crawl(self.pid, mprice=101.9, sources=src)["changed"], "inside 2%")
        self.assertTrue(self.crawl(self.pid, mprice=102.5, sources=src)["changed"], "outside 2%")
        self.assertTrue(sc_store.fingerprints_equal(None, None))
        self.assertFalse(sc_store.fingerprints_equal(fp, None))

    def test_drift_is_measured_from_the_anchor(self):
        src = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}}
        self.crawl(self.pid, mprice=10.0, sources=src)
        self.assertFalse(self.crawl(self.pid, mprice=10.3, sources=src)["changed"])
        self.assertTrue(self.crawl(self.pid, mprice=10.6, sources=src)["changed"])

    def test_changed_since_review(self):
        self.crawl(self.pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False}})
        self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=self.version(self.pid),
                              reason="seasonal")
        self.crawl(self.pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        p = self.store.get_product(self.pid)
        self.assertEqual(p["review_status"], "changed_since_review")
        self.assertTrue(p["needs_action"])

    def test_partial_row_does_not_flip_review(self):
        self.crawl(self.pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False}})
        self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=self.version(self.pid),
                              reason="seasonal")
        self.crawl(self.pid, sources={1: {"verdict": "BLOCKED"}})
        p = self.store.get_product(self.pid)
        self.assertTrue(p["being_checked"])
        self.assertEqual(p["review_status"], "no_change_needed")

    def test_mark_fixed_outcomes(self):
        bad = {1: {"verdict": "PRICE", "price_gbp": 50.0, "available": True}}
        self.crawl(self.pid, sources=bad)
        self.store.set_review(self.pid, "fixed_rechecking", actor="Asha", expected_version=self.version(self.pid))
        self.crawl(self.pid, sources=bad)
        self.assertEqual(self.store.get_product(self.pid)["review_status"], "still_differs")
        self.store.set_review(self.pid, "fixed_rechecking", actor="Asha", expected_version=self.version(self.pid))
        self.crawl(self.pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        p = self.store.get_product(self.pid)
        self.assertEqual(p["review_status"], "fixed_confirmed")
        self.assertTrue(p["resolved"])


class TestReview(StoreCase):
    def setUp(self):
        super().setUp()
        self.load()
        self.crawl_all()
        self.pid = self.pid_at(1)

    def test_set_review_and_conflict(self):
        ok, p, conflict = self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=0,
                                                reason="r1", note="checked by phone")
        self.assertEqual((ok, p["review_version"], p["review_by"], conflict), (True, 1, "Asha", None))
        ok, p, conflict = self.store.set_review(self.pid, "to_review", actor="Ben", expected_version=0)
        self.assertFalse(ok)
        self.assertEqual((conflict["reason"], conflict["review_version"], p["review_status"]),
                         ("version", 1, "no_change_needed"))
        self.assertIn("checked by phone", [e["note"] for e in self.store.history(self.pid)])
        with self.assertRaises(ValueError):
            self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=1)
        with self.assertRaises(ValueError):
            self.store.set_review(self.pid, "fixed_confirmed", actor="Asha", expected_version=1)

    def test_being_checked_rows_are_not_reviewable(self):
        self.store.record_source(self.pid, 1, {"url": ebay(111), "verdict": "BLOCKED"})
        p = self.store.get_product(self.pid)
        self.store.finalize(self.pid, fake_finalize(None, [s["raw"] for s in p["sources"]]))
        ok, _, conflict = self.store.set_review(self.pid, "fixed_rechecking", actor="Asha", expected_version=0)
        self.assertEqual((ok, conflict["reason"]), (False, "being_checked"))

    def test_eight_threads_one_winner_per_version(self):
        for round_no in range(3):
            expected = self.version(self.pid)
            barrier, results = threading.Barrier(8), []

            def patch(i):
                barrier.wait()
                ok, _, conflict = self.store.set_review(self.pid, "no_change_needed", actor=f"user{i}",
                                                        expected_version=expected, reason=f"r{i}")
                results.append((ok, conflict and conflict["reason"]))
                self.store.close()
            threads = [threading.Thread(target=patch, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for ok, _ in results if ok), 1, f"round {round_no}")
            self.assertEqual(sorted(r for ok, r in results if not ok), ["version"] * 7)
            self.assertEqual(self.version(self.pid), expected + 1)

    def test_notes_are_append_only(self):
        self.store.add_note(self.pid, "first", actor="Asha")
        self.store.add_note(self.pid, "second", actor="Ben")
        notes = [e for e in self.store.history(self.pid) if e["kind"] == "note"]
        self.assertEqual([n["note"] for n in notes], ["second", "first"])
        self.assertFalse(any(n["undoable"] for n in notes))
        with self.assertRaises(ValueError):
            self.store.add_note(self.pid, "  ", actor="Asha")

    def test_claims_take_next_and_expiry(self):
        for pos in (1, 2, 3):
            pid = self.pid_at(pos)
            self.store.record_source(pid, 1, {"url": self.store.get_product(pid)["sources"][0]["url"],
                                              "verdict": "PRICE", "available": False})
            p = self.store.get_product(pid)
            self.store.finalize(pid, fake_finalize(None, [s["raw"] for s in p["sources"]]))
        got = self.store.take_next(2, actor="Asha", view_filter={})
        self.assertEqual(got["claimed"], [self.pid_at(1), self.pid_at(2)])
        self.assertEqual(self.store.take_next(5, actor="Ben", view_filter={})["claimed"], [self.pid_at(3)])
        self.assertEqual(self.store.claim([self.pid_at(1)], actor="ben")["taken"], [self.pid_at(1)])
        self.assertEqual(self.store.claim([self.pid_at(1)], actor="ASHA")["claimed"], [self.pid_at(1)])
        mine = self.store.create_snapshot({"claim": "mine"}, actor="asha")
        self.assertEqual(self.store.snapshot_info(mine)["count"], 2)
        self.clock.advance(hours=2, minutes=1)
        self.assertEqual(self.store.take_next(5, actor="Ben", view_filter={})["claimed"],
                         [self.pid_at(1), self.pid_at(2), self.pid_at(3)], "stale claims are free again")
        self.assertEqual(self.store.release_claims(actor="BEN")["released"], 3)
        self.assertFalse(self.store.get_product(self.pid_at(1))["claim_live"])

    def test_archive_and_restore(self):
        with self.assertRaises(ValueError):
            self.store.archive(self.pid, "bored", actor="Asha", expected_version=0)
        with self.assertRaises(ValueError):
            self.store.archive(self.pid, "other", actor="Asha", expected_version=0)
        ok, p, _ = self.store.archive(self.pid, "duplicate", actor="Asha", expected_version=0)
        self.assertTrue(ok and p["archived"])
        self.assertEqual((p["archive_reason"], p["archived_by"]), ("duplicate", "Asha"))
        ok, _, conflict = self.store.archive(self.pid, "duplicate", actor="Asha", expected_version=1)
        self.assertEqual(conflict["reason"], "already_archived")
        ok, p, _ = self.store.restore(self.pid, actor="Ben", expected_version=1)
        self.assertTrue(ok)
        self.assertFalse(p["archived"])

    def test_set_link_conflict_and_confirm(self):
        info = FAKE_URLS.normalize_maskura_url("maskura.co.uk/products/blanket-2?variant=7")
        ok, p, _ = self.store.set_link(self.pid, info, actor="system (auto)", method="auto", confidence=0.93,
                                       expected_link_version=0)
        self.assertTrue(ok)
        self.assertEqual((p["link_version"], p["variant_id"], p["link_needs_confirm"]), (1, "7", True))
        # The new link is waiting for its first check, so it is not Needs action yet (rows being checked never are).
        self.assertEqual((p["being_checked"], p["needs_action"]), (True, False))
        ok, _, conflict = self.store.set_link(self.pid, info, actor="Asha", method="manual", confidence=None,
                                              expected_link_version=0)
        self.assertEqual((ok, conflict["reason"]), (False, "version"))
        self.assertFalse(self.store.record_maskura(self.pid, {"status": "OK"}, link_version=0))
        ok, p, _ = self.store.confirm_link(self.pid, actor="Asha", expected_version=p["review_version"])
        self.assertTrue(ok)
        self.assertEqual((p["link_needs_confirm"], p["link_confirmed_by"]), (False, "Asha"))
        self.assertTrue(p["resolved"])
        ok, _, conflict = self.store.confirm_link(self.pid, actor="Asha", expected_version=p["review_version"])
        self.assertEqual(conflict["reason"], "nothing_to_confirm")
        with self.assertRaises(ValueError):
            self.store.set_link(self.pid, FAKE_URLS.normalize_maskura_url("junk"), actor="Asha", method="manual",
                                confidence=None, expected_link_version=1)

    def test_confirm_suggestion_applies_it(self):
        info = FAKE_URLS.normalize_maskura_url("maskura.co.uk/products/blanket-new")
        self.store.set_suggestion(self.pid, info, score=0.8)
        p = self.store.get_product(self.pid)
        self.assertEqual(p["maskura_status"], "LINK_CHANGED_SUGGESTION")
        ok, p, _ = self.store.confirm_link(self.pid, actor="Asha", expected_version=p["review_version"])
        self.assertTrue(ok)
        self.assertEqual((p["maskura_url"], p["link_source"], p["suggested_url"]), (info["url"], "suggestion", None))

    def test_undo_event_and_stale_undo(self):
        self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=0, reason="r")
        first = self.store.history(self.pid)[0]["id"]
        self.assertTrue(self.store.undo_event(first, actor="Asha")["ok"])
        p = self.store.get_product(self.pid)
        self.assertEqual((p["review_status"], p["review_version"]), ("to_review", 2))
        self.assertEqual(self.store.undo_event(first, actor="Asha")["conflict"]["reason"], "already_undone")
        self.store.set_review(self.pid, "no_change_needed", actor="Asha", expected_version=2, reason="r")
        second = self.store.history(self.pid)[0]["id"]
        self.store.set_review(self.pid, "to_review", actor="Ben", expected_version=3)
        result = self.store.undo_event(second, actor="Asha")
        self.assertEqual((result["ok"], result["conflict"]["reason"]), (False, "changed_since"))

    def test_undo_link_change(self):
        info = FAKE_URLS.normalize_maskura_url("maskura.co.uk/products/blanket-2")
        self.store.set_link(self.pid, info, actor="Asha", method="manual", confidence=None, expected_link_version=0)
        event = self.store.history(self.pid)[0]
        self.assertTrue(self.store.undo_event(event["id"], actor="Asha")["ok"])
        p = self.store.get_product(self.pid)
        self.assertEqual((p["maskura_url"], p["link_source"], p["link_version"]),
                         ("https://www.maskura.co.uk/products/blanket", "sheet", 2))

    def test_bulk_on_snapshot_and_partial_undo(self):
        snap = self.store.create_snapshot({"ids": [self.pid_at(1), self.pid_at(2), self.pid_at(3)]}, actor="Asha")
        self.store.set_review(self.pid_at(2), "fixed_rechecking", actor="Ben", expected_version=0)
        result = self.store.bulk("no_change_needed", snap, actor="Asha", value="supplier ok")
        self.assertEqual((result["ok"], result["conflicts"], result["total"]), (2, [self.pid_at(2)], 3))
        conflict_view = self.store.create_snapshot({"batch": result["batch_id"], "outcome": "conflict"}, actor="Asha")
        self.assertEqual(self.store.snapshot_info(conflict_view)["count"], 1)
        self.store.set_review(self.pid_at(3), "to_review", actor="Ben", expected_version=1)
        undo = self.store.undo_batch(result["batch_id"], actor="Asha")
        self.assertEqual((undo["undone"], undo["conflicts"]), ([self.pid_at(1)], [self.pid_at(3)]))
        self.assertEqual(self.store.get_product(self.pid_at(1))["review_status"], "to_review")

    def test_auto_fix_batch_undoes_in_full(self):
        batch = self.store.start_batch("auto_fix", actor="system (auto)")
        for pos, handle in ((1, "blanket-x"), (2, "kettle-x")):
            info = FAKE_URLS.normalize_maskura_url(f"maskura.co.uk/products/{handle}")
            self.store.set_link(self.pid_at(pos), info, actor="system (auto)", method="auto", confidence=0.95,
                                expected_link_version=0, batch_id=batch)
        undo = self.store.undo_batch(batch, actor="Asha")
        self.assertEqual((len(undo["undone"]), undo["conflicts"]), (2, []))
        self.assertEqual(self.store.get_product(self.pid_at(2))["maskura_url"],
                         "https://www.maskura.co.uk/products/kettle")


class TestViews(StoreCase):
    def setUp(self):
        super().setUp()
        self.load()

    def test_keyset_pages(self):
        snap = self.store.create_snapshot({"archived": "include"}, actor="Asha")
        seen, after, pages = [], None, 0
        while True:
            page = self.store.page(snap, after=after, limit=4)
            seen += [p["position"] for p in page["items"]]
            pages += 1
            after = page["next"]
            if after is None:
                break
        self.assertEqual((seen, pages), ([1, 2, 3, 4, 5, 6], 2))
        self.assertEqual(len(self.store.page(snap, limit=50)["items"][0]["sources"]), 2)

    def test_snapshot_sort_orders_the_whole_view(self):
        names = lambda snap: [p["name"] for p in self.store.page(snap, limit=50)["items"]]  # noqa: E731
        by_name = self.store.create_snapshot({}, actor="Asha", sort="name")
        self.assertEqual(names(by_name), ["Barbell", "Blanket", "Kettle", "Leggings", "Mug blue", "Mug red"])
        self.assertEqual(self.store.snapshot_info(by_name)["sort"], "name")
        self.assertEqual([p["position"] for p in self.store.page(
            self.store.create_snapshot({}, actor="Asha", sort="-row"), limit=50)["items"]], [6, 5, 4, 3, 2, 1])
        self.crawl(self.pid_at(4))
        self.clock.advance(minutes=1)
        self.crawl(self.pid_at(2))
        recent = self.store.create_snapshot({}, actor="Asha", sort="-checked")
        self.assertEqual([p["position"] for p in self.store.page(recent, limit=2)["items"]], [2, 4])
        self.assertEqual(self.store.snapshot_info(self.store.create_snapshot({}, actor="Asha"))["sort"], "row")
        self.store._conn().execute("UPDATE products SET name = '  ' WHERE position = 1")
        for sort in ("name", "-name"):
            self.assertEqual(self.store.page(self.store.create_snapshot({}, actor="Asha", sort=sort), limit=50)
                             ["items"][-1]["position"], 1, "a row with no name sorts last either way")
        with self.assertRaises(ValueError):
            self.store.create_snapshot({}, actor="Asha", sort="price; DROP TABLE products")

    def test_snapshot_expires_after_8_hours(self):
        snap = self.store.create_snapshot({}, actor="Asha")
        self.clock.advance(hours=7, minutes=59)
        self.store.page(snap)
        self.clock.advance(minutes=2)
        with self.assertRaises(sc_store.SnapshotExpired):
            self.store.page(snap)
        with self.assertRaises(sc_store.SnapshotExpired):
            self.store.changes(snap, 0, 0)
        with self.assertRaises(ValueError):
            self.store.create_snapshot({"color": "red"}, actor="Asha")

    def test_changes_since(self):
        self.crawl(self.pid_at(3))
        snap = self.store.create_snapshot({}, actor="Asha")
        base = self.store.changes(snap, 0, 0)
        self.store.set_review(self.pid_at(3), "to_review", actor="Asha", expected_version=0)
        delta = self.store.changes(snap, base["results_version"], base["review_version"])
        self.assertEqual(delta["ids"], [self.pid_at(3)])
        self.assertEqual(delta["review_version"], base["review_version"] + 1)

    def test_filters(self):
        self.store.record_source(self.pid_at(2), 1, {"url": ebay(222), "verdict": "PRICE", "available": False,
                                                     "platform": "ebay"})
        p = self.store.get_product(self.pid_at(2))
        self.store.finalize(p["id"], fake_finalize(None, [s["raw"] for s in p["sources"]]))
        count = lambda f: self.store.snapshot_info(self.store.create_snapshot(f, actor="Asha"))["count"]  # noqa: E731
        self.assertEqual(count({"problem": "OOS"}), 1)
        self.assertEqual(count({"problem": "S1_OOS"}), 1)
        self.assertEqual(count({"problem": "S2_OOS"}), 0)
        self.assertEqual(count({"platform": "ebay"}), 1)
        self.assertEqual(count({"q": "mug"}), 2)
        self.assertEqual(count({"q": "100%"}), 0)
        self.assertEqual(count({"maskura": "NO_LINK"}), 1)
        self.assertEqual(count({"needs_action": True}), 1)


class TestSummary(StoreCase):
    """The N of TOTAL definitions on a fixture counted by hand (PLAN, status glossary Definitions)."""

    def test_summary_matches_hand_count(self):
        rows = [{"position": i, "name": f"P{i}", "maskura_raw": mk(f"p{i}"), "source_urls": [ebay(1000 + i)]}
                for i in range(1, 9)]
        self.load(rows)
        pid = {i: self.pid_at(i) for i in range(1, 9)}
        oos = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False}}
        fine = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}}
        for i in (1, 2, 4, 5, 8):
            self.crawl(pid[i], sources=oos)
        self.crawl(pid[3], sources=fine)                                     # P3 never had a problem
        self.store.set_review(pid[2], "no_change_needed", actor="A", expected_version=self.version(pid[2]),
                              reason="r")                                    # P2 resolved
        self.store.set_review(pid[4], "fixed_rechecking", actor="A", expected_version=self.version(pid[4]))
        self.crawl(pid[4], sources=fine)                                     # P4 fixed, confirmed
        self.store.archive(pid[5], "not_sold", actor="A", expected_version=self.version(pid[5]))
        self.crawl(pid[6], sources={1: {"verdict": "BLOCKED"}})              # P6 being checked only
        self.crawl(pid[7], sources=fine)
        info = FAKE_URLS.normalize_maskura_url("maskura.co.uk/products/p7-new")
        self.store.set_link(pid[7], info, actor="system (auto)", method="auto", confidence=0.95,
                            expected_link_version=0)                         # P7 unconfirmed auto link
        self.store.set_review(pid[8], "fixed_rechecking", actor="A", expected_version=self.version(pid[8]))

        s = self.store.summary()
        self.assertEqual(self.store.get_product(pid[4])["review_status"], "fixed_confirmed")
        self.assertEqual((s["products"], s["archived"]), (7, 1))
        self.assertEqual(s["needs_action"], 1)                     # P1 (P7 and P8 wait on a re-check)
        self.assertEqual(s["reviewed"], {"n": 2, "total": 5})      # resolved P2, P4 of P1, P2, P4, P7, P8
        self.assertEqual((s["fixed_rechecking"], s["being_checked"], s["with_problem"]), (1, 2, 3))  # P6, P7
        self.assertEqual(s["review"]["fixed_confirmed"], 1)
        flagged = [i for i in range(1, 9) if self.store.get_product(pid[i])["needs_action"]]
        self.assertEqual(flagged, [1], "the Python and SQL definitions agree")
        self.assertEqual(self.store.summary({"q": "P1"})["needs_action"], 1)

    def test_a_row_being_checked_is_not_needs_action_even_with_a_problem(self):
        rows = [{"position": 1, "name": "P1", "maskura_raw": mk("p1"), "source_urls": [ebay(1001), ebay(1002)]}]
        self.load(rows)
        pid = self.pid_at(1)
        self.crawl(pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False},
                                 2: {"verdict": "BLOCKED"}})
        p = self.store.get_product(pid)
        self.assertEqual((p["has_problem"], p["being_checked"], p["needs_action"]), (True, True, False))
        s = self.store.summary()
        self.assertEqual((s["needs_action"], s["being_checked"], s["with_problem"]), (0, 1, 1))
        self.assertEqual(self.store.snapshot_info(
            self.store.create_snapshot({"needs_action": True}, actor="A"))["count"], 0)
        self.crawl(pid, sources={2: {"verdict": "PRICE", "price_gbp": 6.0, "available": True}})
        self.assertTrue(self.store.get_product(pid)["needs_action"])
        self.assertEqual(self.store.summary()["needs_action"], 1)


class TestBackups(StoreCase):
    def test_backup_and_prune(self):
        self.load()
        dest_dir = os.path.join(self.tmp.name, "backups")
        result = self.store.backup(os.path.join(dest_dir, sc_store.backup_name(self.clock())))
        self.assertTrue(result["ok"])
        self.assertEqual(result["integrity"], "ok")
        self.assertTrue(os.path.basename(result["path"]).startswith("source_check-20261016T031200Z"))
        copy = sqlite3.connect(result["path"])
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM products").fetchone()[0], 6)
        copy.close()
        self.assertEqual(self.store.get_meta("last_backup_at"), "2026-10-16T03:12:00Z")
        for i in range(5):
            self.clock.advance(hours=1)
            self.store.backup(os.path.join(dest_dir, sc_store.backup_name(self.clock())))
        removed = sc_store.prune_backups(dest_dir, keep=4)
        self.assertEqual(len(removed), 2)
        left = sorted(os.listdir(dest_dir))
        self.assertEqual(len(left), 4)
        self.assertEqual(left[-1], sc_store.backup_name(self.clock()))
        self.assertNotIn(os.path.basename(result["path"]), left)


class TestNotCheckedYet(StoreCase):
    """A row that was imported but never crawled is "Being checked", never "No problem found"."""

    def test_new_rows_with_something_to_check_start_being_checked(self):
        self.load(base_rows() + [{"position": 7, "name": "Nothing", "maskura_raw": "", "source_urls": ["", ""]}])
        self.assertTrue(all(self.store.get_product(self.pid_at(i))["being_checked"] for i in range(1, 7)))
        self.assertFalse(self.store.get_product(self.pid_at(7))["being_checked"])
        self.assertEqual(self.store.summary()["being_checked"], 6)

    def test_first_finalize_clears_it_and_an_unchecked_row_cannot_be_reviewed(self):
        self.load()
        pid = self.pid_at(2)
        ok, _, conflict = self.store.set_review(pid, "no_change_needed", actor="Asha", expected_version=0,
                                                reason="fine")
        self.assertFalse(ok)
        self.assertEqual(conflict["reason"], "being_checked")
        self.crawl(pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        self.assertFalse(self.store.get_product(pid)["being_checked"])

    def test_a_changed_link_or_source_url_starts_checking_again(self):
        self.load()
        for pos in (1, 2):
            self.crawl(self.pid_at(pos), sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        rows = base_rows()
        rows[0]["source_urls"][0] = ebay(999)
        rows[1]["maskura_raw"] = mk("kettle-new")
        self.load(rows)
        self.assertTrue(self.store.get_product(self.pid_at(1))["being_checked"])
        # A new handle is a new product (the old one is gone from the sheet); it starts unchecked too.
        new_kettle = self.store._conn().execute(
            "SELECT id FROM products WHERE position = 2 AND present_in_sheet = 1").fetchone()[0]
        self.assertTrue(self.store.get_product(new_kettle)["being_checked"])
        self.crawl(self.pid_at(3), sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        self.load(rows)
        self.assertFalse(self.store.get_product(self.pid_at(3))["being_checked"], "an unchanged row keeps its result")


class TestLinkChanges(StoreCase):
    """P4: a link change re-checks the row; the catalog cache; the gated link_fix batch."""

    def info(self, handle):
        return FAKE_URLS.normalize_maskura_url(f"maskura.co.uk/products/{handle}")

    def test_set_link_marks_the_row_being_checked_and_undo_restores_it(self):
        self.load()
        pid = self.pid_at(2)
        self.crawl(pid, sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        self.assertFalse(self.store.get_product(pid)["being_checked"])
        ok, p, _ = self.store.set_link(pid, self.info("kettle-new"), actor="Asha", method="manual", confidence=None,
                                       expected_link_version=0)
        self.assertTrue(ok)
        self.assertTrue(p["being_checked"])
        event = self.store.event_at(pid, link_version=p["link_version"])
        self.assertTrue(self.store.undo_event(event, actor="Asha")["ok"])
        p = self.store.get_product(pid)
        self.assertFalse(p["being_checked"])
        self.assertEqual(p["maskura_url"], "https://www.maskura.co.uk/products/kettle")

    def test_catalog_cache_is_replaced_whole(self):
        self.store.replace_catalog([{"handle": "a", "title": "A"}, {"handle": "b", "title": "B"}], complete=True)
        self.assertEqual(sorted(p["handle"] for p in self.store.catalog_products()), ["a", "b"])
        self.store.replace_catalog([{"handle": "c", "title": "C"}], complete=False)
        info = self.store.catalog_info()
        self.assertEqual((info["count"], info["complete"]), (1, False))
        self.assertRegex(info["fetched_at"], ISO_RE)

    def test_link_fix_needs_an_approval_and_is_one_undoable_batch(self):
        self.load()
        decisions = [{"product_id": self.pid_at(1), "url_info": self.info("blanket-new"), "confidence": 0.95,
                      "expected_link_version": 0},
                     {"product_id": self.pid_at(2), "url_info": self.info("kettle-new"), "confidence": 0.93,
                      "expected_link_version": 0}]
        with self.assertRaises(ValueError):
            self.store.link_fix(decisions, approved_by="")
        res = self.store.link_fix(decisions, approved_by="Asha, sample checked 2026-10-16")
        self.assertEqual((res["changed"], res["conflicts"]), (2, []))
        p = self.store.get_product(self.pid_at(1))
        self.assertEqual((p["link_source"], p["link_needs_confirm"], p["maskura_status"], p["link_edited_by"]),
                         ("auto", True, "LINK_CHANGED_CONFIRM", sc_store.AUTO_FIX_ACTOR))
        self.assertTrue(p["being_checked"])
        self.assertEqual(self.store.get_meta("link_fix_last_approval"), "Asha, sample checked 2026-10-16")
        undo = self.store.undo_batch(res["batch_id"], actor="Asha")
        self.assertEqual((len(undo["undone"]), undo["conflicts"]), (2, []))
        self.assertEqual(self.store.get_product(self.pid_at(1))["maskura_url"],
                         "https://www.maskura.co.uk/products/blanket")

    def test_link_fix_skips_a_row_edited_since(self):
        self.load()
        pid = self.pid_at(2)
        self.store.set_link(pid, self.info("kettle-by-hand"), actor="Asha", method="manual", confidence=None,
                            expected_link_version=0)
        res = self.store.link_fix([{"product_id": pid, "url_info": self.info("kettle-new"), "confidence": 0.95,
                                    "expected_link_version": 0}], approved_by="Asha")
        self.assertEqual((res["changed"], res["conflicts"]), (0, [pid]))
        self.assertEqual(self.store.get_product(pid)["link_source"], "manual")


if __name__ == "__main__":
    unittest.main()


class TestRulesDecided20261016(StoreCase):
    """INTERFACES.md "Store rules decided 2026-10-16" and the LINK_BROKEN status."""

    def setUp(self):
        super().setUp()
        rows = [{"position": i, "name": f"P{i}", "maskura_raw": mk(f"p{i}"), "source_urls": [ebay(2000 + i)]}
                for i in range(1, 5)]
        self.load(rows)
        self.pid = {i: self.pid_at(i) for i in range(1, 5)}

    def test_maskura_404_is_link_broken_and_needs_action(self):
        self.crawl(self.pid[1], status="NOT_FOUND", mprice=None,
                   sources={1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}})
        p = self.store.get_product(self.pid[1])
        self.assertEqual(p["maskura_status"], "LINK_BROKEN")
        self.assertNotEqual(p["maskura_status"], "NOT_ON_MASKURA")
        self.assertTrue(p["needs_action"])

    def test_missing_variant_is_link_broken(self):
        self.crawl(self.pid[2], status="VARIANT_NOT_FOUND", mprice=None)
        self.assertEqual(self.store.get_product(self.pid[2])["maskura_status"], "LINK_BROKEN")

    def test_fixed_rechecking_not_in_take_next(self):
        oos = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False}}
        for i in (1, 2):
            self.crawl(self.pid[i], sources=oos)
        self.store.set_review(self.pid[1], "fixed_rechecking", actor="A", expected_version=self.version(self.pid[1]))
        got = self.store.take_next(25, actor="Bea")["claimed"]
        self.assertEqual(got, [self.pid[2]])
        self.assertFalse(self.store.get_product(self.pid[1])["needs_action"])
        self.assertEqual(self.store.summary()["fixed_rechecking"], 1)

    def test_reset_had_problem_starts_a_new_total(self):
        oos = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": False}}
        fine = {1: {"verdict": "PRICE", "price_gbp": 5.0, "available": True}}
        self.crawl(self.pid[1], sources=oos)
        self.crawl(self.pid[1], sources=fine)            # had a problem, now clean
        self.crawl(self.pid[2], sources=oos)             # still has one
        self.assertEqual(self.store.summary()["reviewed"]["total"], 2)
        before = self.store.summary()["results_version"]
        self.assertEqual(self.store.reset_had_problem(), 1)
        s = self.store.summary()
        self.assertEqual(s["reviewed"]["total"], 1)
        self.assertGreater(s["results_version"], before)
        self.assertEqual(self.store.reset_had_problem(), 0)

    def test_no_url_slot_is_never_pending(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_json_to_db", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts",
                                               "migrate_json_to_db.py"))
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        self.load([{"position": 9, "name": "P9", "maskura_raw": mk("p9"), "source_urls": [ebay(2009), ""]}])
        _, sources = mig.finalize_input(self.store.get_product(self.pid_at(9)))
        self.assertIsNone(sources[0], "a slot with a URL and no result is pending")
        self.assertEqual(sources[1], sc_store.NO_URL_SOURCE)
