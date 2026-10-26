"""The Source Checker worker (PLAN P5): one process that works the job queue in source_check.db.

- Single instance: an flock on worker.lock. At startup, leases left by a dead worker go back to pending and their
  tokens are void, so nothing is committed twice.
- A pool of 6 threads: one reserved for the Maskura host, one for eBay, the rest share every other host. A thread
  only takes an item whose host is ready in the pacer, so 50 hanging long-tail hosts cannot slow eBay.
- Outcomes: a result is committed once (lease token). BLOCKED or ERROR goes to the retry pass; the third attempt
  is final. Shopify's bot check is BLOCKED at once, never followed, and cools the host down (sc_crawl).
- Breaker: a transport failure (nothing came back at all) makes the worker probe the network outside the pacers,
  with a HEAD to the Maskura home page and a TCP connect to 1.1.1.1:443. Only when BOTH fail does the breaker
  open: fetching stops, the item goes back without counting an attempt, and no ERROR is written. A clock jump
  (sleep or resume) re-probes. A site that answers 403 to everything is not a network problem.
- The main loop beats every 10 s, saves host health from the pacer, starts queued jobs (a full job resets the
  "had a problem" marker), runs retry passes, refreshes the catalog from the sitemaps (never /products.json),
  and holds the sleep and lid-switch inhibitor only while a job runs.

Run: .venv/bin/python -B sc_worker.py   (the systemd unit deploy/systemd/source-check-worker.service does this)
"""
import datetime
import fcntl
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx

import sc_crawl
import sc_store

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("sc_worker")

POOL = 6
HEARTBEAT_S = 10.0
IDLE_S = 0.5
MASKURA_HOST = "www.maskura.co.uk"
PROBE_TIMEOUT_S = 5.0
CLOCK_JUMP_S = 60.0
# Go/no-go after 2 hours of running time (thresholds also in docs/source-check/RUNBOOK.md; P6 calibrates them).
GO_NO_GO_AFTER_S = 7200
EBAY_BLOCK_RATE_MAX = 0.25
EBAY_MIN_RESULTS = 20
ITEMS_PER_HOUR_MIN = 300


def go_no_go(running_s, ebay_results, ebay_blocked, items_done):
    """None to carry on, or the plain reason to pause a full check after its first 2 hours."""
    if running_s < GO_NO_GO_AFTER_S:
        return None
    if ebay_results >= EBAY_MIN_RESULTS and ebay_blocked / ebay_results > EBAY_BLOCK_RATE_MAX:
        return (f"Go/no-go: eBay refused {round(100 * ebay_blocked / ebay_results)}% of requests in the first 2 hours "
                f"(the limit is {round(100 * EBAY_BLOCK_RATE_MAX)}%).")
    per_hour = items_done / (running_s / 3600)
    if per_hour < ITEMS_PER_HOUR_MIN:
        return f"Go/no-go: only {round(per_hour)} items an hour in the first 2 hours (the floor is {ITEMS_PER_HOUR_MIN})."
    return None


def role_hosts(role, hosts):
    """The hosts a thread of this role may take: its own reserved host, or every host that is not reserved."""
    if role == "maskura":
        return [h for h in hosts if "maskura." in h]
    if role == "ebay":
        return [h for h in hosts if "ebay." in h]
    return [h for h in hosts if "maskura." not in h and "ebay." not in h]


def roles(pool=POOL):
    return ["maskura", "ebay"] + ["general"] * max(0, pool - 2)


def outcome_of(item, raw):
    """ok | retry | transport for a fetched item (slot 0 is the Maskura page)."""
    if raw.get("transport_error"):
        return "transport"
    if item["slot"] == sc_store.MASKURA_SLOT:
        return "retry" if raw.get("status") in ("BLOCKED", "ERROR") else "ok"
    return "retry" if raw.get("verdict") in ("BLOCKED", "ERROR") else "ok"


# ── breaker ──────────────────────────────────────────────────────────────────

def probe_http(timeout=PROBE_TIMEOUT_S):
    """Success when ANY HTTP response arrives from the Maskura home page (outside the pacers)."""
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            client.head(f"https://{MASKURA_HOST}/")
        return True
    except httpx.TransportError:
        return False


def probe_tcp(timeout=PROBE_TIMEOUT_S):
    """Success when a TCP connection to 1.1.1.1:443 completes."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


class Breaker:
    """Opens only when both probes fail at the transport level; re-probes on a clock jump."""

    def __init__(self, probes=(probe_http, probe_tcp), *, mono=time.monotonic, wall=time.time, retry_s=15.0):
        self._probes = probes
        self._mono, self._wall = mono, wall
        self._lock = threading.Lock()
        self.retry_s = retry_s
        self.is_open = False
        self.opened_at = None
        self.probes_run = 0
        self._last = (mono(), wall())

    def probe(self):
        with self._lock:
            self.probes_run += 1
            ok = any(p() for p in self._probes)
            if ok:
                self.is_open, self.opened_at = False, None
            elif not self.is_open:
                self.is_open, self.opened_at = True, self._wall()
            return ok

    def report_transport_failure(self):
        """Called when a fetch got nothing back. True when the network is fine (the host's own problem)."""
        return self.probe()

    def check_clock_jump(self):
        """A wall clock that moved much more (or less) than the monotonic clock means the machine slept or the time
        was set: re-probe. Returns True when it re-probed."""
        mono, wall = self._mono(), self._wall()
        last_mono, last_wall = self._last
        self._last = (mono, wall)
        if abs((wall - last_wall) - (mono - last_mono)) > CLOCK_JUMP_S:
            self.probe()
            return True
        return False


# ── sleep inhibitor ──────────────────────────────────────────────────────────

class Inhibitor:
    """Holds the sleep and lid-switch inhibitor (systemd-inhibit) only while a job runs. A no-op without it.

    The held command is `tail --pid=<worker pid>`, not `sleep infinity`, so the inhibitor ends when the worker's
    process ends, even after a SIGKILL or a crash outside systemd (under the unit, KillMode=control-group also
    ends it)."""

    def __init__(self, enabled=True):
        self.enabled = enabled and shutil.which("systemd-inhibit") is not None
        self._proc = None

    def hold(self, on):
        if not self.enabled:
            return
        if on and self._proc is None:
            self._proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:handle-lid-switch", "--who=Source Checker",
                                           "--why=Checking products", "--mode=block",
                                           "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not on and self._proc is not None:
            self._proc.terminate()
            self._proc = None


# ── worker ───────────────────────────────────────────────────────────────────

class Worker:
    def __init__(self, store, *, client_factory=None, pacer=None, breaker=None, pool=POOL, worker_id=None,
                 inhibitor=None, catalog_fetch=None, mono=time.monotonic, wall=time.time, idle_s=IDLE_S,
                 heartbeat_s=HEARTBEAT_S, admin=None):
        self.store = store
        # An optional catalog object with maskura_result(url) and usable(): Maskura rows are answered from it
        # (no storefront requests). None, the default here: the public storefront.
        self.admin = admin
        self.client_factory = client_factory or sc_crawl.build_client
        self.pacer = pacer or sc_crawl.Pacer()
        self.breaker = breaker or Breaker()
        self.pool = pool
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.inhibitor = inhibitor or Inhibitor(enabled=False)
        self.catalog_fetch = catalog_fetch or sc_crawl.fetch_sitemap_products
        self._mono, self._wall = mono, wall
        self.idle_s = idle_s
        self.heartbeat_s = heartbeat_s
        self.stop = threading.Event()
        self.counts = {"committed": 0, "retry": 0, "requeued": 0, "final": 0, "stale": 0}
        self.by_job = {}          # job id -> {"ebay": results, "ebay_blocked": n, "items": n} for the go/no-go
        self._judged = set()
        self._count_lock = threading.Lock()
        self._threads = []

    def _count(self, key):
        with self._count_lock:
            self.counts[key] += 1

    # one item
    def _tally(self, item, raw, outcome):
        with self._count_lock:
            t = self.by_job.setdefault(item["job_id"], {"ebay": 0, "ebay_blocked": 0, "items": 0})
            if "ebay." in item["host"]:
                t["ebay"] += 1
                if raw.get("verdict") == "BLOCKED":
                    t["ebay_blocked"] += 1
            if outcome == "ok":
                t["items"] += 1

    def fetch_maskura(self, client, url):
        if self.admin is not None:
            raw = self.admin.maskura_result(url)
            if raw is not None:
                return raw
        return sc_crawl.fetch_maskura(client, self.pacer, url)

    def admin_ready(self):
        return self.admin is not None and self.admin.usable()

    def host_ready(self, host):
        # Through the Admin API a Maskura row makes no storefront request, so the storefront's cool-down does not apply.
        if "maskura." in host and self.admin_ready():
            return True
        return self.pacer.ready(host)

    def process(self, client, item):
        raw = (self.fetch_maskura(client, item["url"]) if item["slot"] == sc_store.MASKURA_SLOT
               else sc_crawl.fetch_source(client, self.pacer, item["url"]))
        outcome = outcome_of(item, raw)
        self._tally(item, raw, outcome)
        if outcome == "transport":
            if not self.breaker.report_transport_failure():
                # The network is down: give it back uncounted and write nothing.
                self.store.release_item(item["id"], item["lease_token"], count_attempt=False)
                self._count("requeued")
                return "requeued"
            outcome = "retry"   # the network is fine; this host failed, so it is an attempt like any other
        if outcome == "retry" and not item["last_try"]:
            self.store.release_item(item["id"], item["lease_token"], count_attempt=True,
                                    error=raw.get("error") or raw.get("verdict") or raw.get("status"))
            self._count("retry")
            return "retry"
        res = self.store.commit_item(item["id"], item["lease_token"], raw)
        if res["committed"]:
            self._count("stale" if res["stale"] else ("final" if outcome == "retry" else "committed"))
        return "committed" if res["committed"] else "lost"

    def _thread(self, role):
        client = self.client_factory()
        try:
            while not self.stop.is_set():
                if self.breaker.is_open:
                    self.stop.wait(self.idle_s)
                    continue
                hosts = [h for h in role_hosts(role, self.store.pending_hosts()) if self.host_ready(h)]
                item = self.store.lease_item(self.worker_id, hosts) if hosts else None
                if item is None:
                    self.stop.wait(self.idle_s)
                    continue
                try:
                    self.process(client, item)
                except Exception:  # noqa: BLE001 - one bad item must not stop the thread
                    log.exception("item %s failed", item["id"])
                    self.store.release_item(item["id"], item["lease_token"], count_attempt=True, error="worker error")
        finally:
            client.close()
            self.store.close()   # this thread's SQLite connection; a thread that ends must not leak it

    # main loop pieces
    def save_health(self):
        snap = self.pacer.snapshot()
        mono, wall = self._mono(), self._wall()
        hosts = {}
        for host, h in snap.items():
            until = h.get("cooldown_until")
            hosts[host] = {"gap_s": h.get("gap_s"), "recent_blocks": h.get("recent_blocks"),
                           "last_outcome": h.get("last_outcome"),
                           "cooldown_until": sc_store.utc_iso(datetime.datetime.fromtimestamp(
                               wall + (until - mono), datetime.timezone.utc)) if until else None}
        if hosts:
            self.store.save_host_health(hosts)

    def run_catalog_jobs(self, running):
        for job in running:
            if job["kind"] != "catalog_refresh":
                continue
            client = self.client_factory()
            try:
                products = self.catalog_fetch(client, self.pacer)
                self.store.replace_catalog(products, complete=False)
                self.store.finish_job(job["id"], error=None)
            except Exception as exc:  # noqa: BLE001
                self.store.finish_job(job["id"], error=f"The catalog could not be read: {exc}")
            finally:
                client.close()

    def judge(self, job, running_s):
        """Pause a full check that fails the go/no-go after its first 2 hours (once per job, across restarts).

        The pace is the job's committed items in the database (not products: on 23 Oct 2026 the products count, 171,
        was judged against the items floor and paused a run doing about 377 items an hour), and not this process's
        tallies: on 16 Oct 2026
        a worker restarted by go_live after the 2-hour mark judged the run on its own empty tally ("0 items an
        hour") and paused a healthy run. The once-per-job mark is kept in meta for the same reason. The eBay rate
        still needs EBAY_MIN_RESULTS seen by this process, so a late-starting worker does not judge it on a few."""
        if job["kind"] != "full" or job["id"] in self._judged or running_s < GO_NO_GO_AFTER_S:
            return None
        self._judged.add(job["id"])
        key = f"go_no_go_judged:{job['id']}"
        if self.store.get_meta(key):
            return None
        self.store.set_meta(key, sc_store.utc_iso())
        t = self.by_job.get(job["id"], {"ebay": 0, "ebay_blocked": 0, "items": 0})
        reason = go_no_go(running_s, t["ebay"], t["ebay_blocked"], self.store.items_done(job["id"]))
        if reason:
            self.store.pause_job(job["id"], actor="system", reason=reason)
        return reason

    def tick(self):
        """One pass of the main loop (also called directly by tests)."""
        self.breaker.check_clock_jump()
        if self.breaker.is_open:
            self.breaker.probe()
        running = self.store.activate_jobs()
        self.run_catalog_jobs(running)
        prog = self.store.progress().get("job")
        for job in running:
            if prog and prog["id"] == job["id"] and prog.get("running_s") is not None:
                self.judge(job, prog["running_s"])
        self.store.retry_pass()
        self.inhibitor.hold(bool(running))
        self.save_health()
        self.store.heartbeat(self.worker_id, pid=os.getpid(), hostname=socket.gethostname(),
                             state={"breaker_open": self.breaker.is_open, "counts": dict(self.counts),
                                    "threads": self.pool, "maskura_via": self.maskura_via()})

    def maskura_via(self):
        if self.admin is None:
            return {"source": "storefront"}
        return {"source": "shopify_admin" if self.admin.usable() else "storefront (Admin API failing)",
                "error": self.admin.last_error, "products": self.admin.stats.get("products")}

    def start(self):
        self.store.check_version()
        self.store.reset_leases()
        for role in roles(self.pool):
            t = threading.Thread(target=self._thread, args=(role,), name=f"sc-{role}", daemon=True)
            t.start()
            self._threads.append(t)

    def run(self, until=None):
        """Run until stop is set (or `until()` says so)."""
        self.start()
        try:
            while not self.stop.is_set():
                self.tick()
                if until is not None and until():
                    break
                self.stop.wait(self.heartbeat_s)
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop.set()
        for t in self._threads:
            t.join(timeout=60)
        self.inhibitor.hold(False)


def single_instance(path):
    """Take the worker lock or return None when another worker holds it."""
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = single_instance(os.path.join(HERE, "worker.lock"))
    if lock is None:
        sys.exit("Another Source Checker worker is already running.")
    store = sc_store.Store(os.environ.get("SC_DB_PATH") or os.path.join(HERE, "source_check.db"))
    store.migrate()
    worker = Worker(store, inhibitor=Inhibitor(enabled=True))
    try:
        worker.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
