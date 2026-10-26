"""SQLite store for the Source Checker (`source_check.db`). No network.

Connection discipline:
- one connection per thread (threading.local), WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON;
- every write runs in BEGIN IMMEDIATE and a busy error is retried with backoff;
- `migrate()` runs in BEGIN EXCLUSIVE and re-reads `user_version` inside the transaction;
- `check_version()` raises SchemaMismatch, so a worker built for another schema stops instead of writing.

Times are UTC ISO 8601 strings with whole seconds and a trailing Z. Money is a float in GBP or None.

Two kinds of version live on each product:
- per-row optimistic counters: `review_version` (review, archive, claim-free review state) and `link_version`
  (the Maskura slot only). `results_version` is bumped only when the result fingerprint changes;
- global change counters in `meta` (`results_seq`, `review_seq`). Each product stores the counter value of its
  last change, so `changes()` can answer "what changed in this view since N" with one indexed query.

The `sc_urls` functions are reached through `self.urls`, which defaults to a lazy import of `sc_urls`; tests and
the migration can inject a stand-in with the same contract.
"""
import contextlib
import datetime
import glob
import importlib
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time

SCHEMA_VERSION = 3

CLAIM_TTL = datetime.timedelta(hours=2)
SNAPSHOT_TTL = datetime.timedelta(hours=8)
PRICE_TOL_PCT = 0.02
PRICE_TOL_ABS = 0.50
BACKUP_KEEP = 48
BACKUP_GLOB = "source_check-*.db"

REVIEW_STATUSES = ("to_review", "no_change_needed", "fixed_rechecking", "fixed_confirmed", "still_differs",
                   "changed_since_review")
# Statuses a person may set directly. The rest are outcomes the store sets after a re-check.
MANUAL_REVIEW_STATUSES = ("to_review", "no_change_needed", "fixed_rechecking")
RESOLVED_REVIEW_STATUSES = ("no_change_needed", "fixed_confirmed")
ARCHIVE_REASON_KEYS = ("not_sold", "duplicate", "not_a_product", "other")
LINK_METHODS = ("manual", "auto", "suggestion")
JOB_KINDS = ("full", "recheck", "retry", "catalog_refresh", "link_fix", "changed")
BULK_ACTIONS = ("claim", "release_claim", "no_change_needed", "mark_fixed", "confirm_link", "archive", "restore")
TRANSIENT_FLAG_SUFFIXES = ("_PENDING", "_BLOCKED_RETRY")
SYSTEM_ACTOR = "system"
AUTO_FIX_ACTOR = "system (auto)"
MASKURA_SLOT = 0          # job_items.slot for the Maskura page; source slots are 1, 2, ...
MAX_ATTEMPTS = 3
PRIORITY_TOP = 100        # Mark fixed and Edit link re-checks jump the queue
REUSE_MAX_AGE_S = 7 * 86400   # owner, 2026-10-24: a result up to 7 days old is reused instead of checked again
HEARTBEAT_STALE_S = 30
ETA_WINDOW_S = 7200
# View orders. Every one ends in position and id, so the order is total and stable.
SORTS = {
    "row": "p.position IS NULL, p.position, p.id",
    "-row": "p.position IS NULL, p.position DESC, p.id DESC",
    "name": "NULLIF(TRIM(p.name), '') IS NULL, p.name COLLATE NOCASE, p.position, p.id",
    "-name": "NULLIF(TRIM(p.name), '') IS NULL, p.name COLLATE NOCASE DESC, p.position, p.id",
    "checked": "p.last_crawled_at IS NULL, p.last_crawled_at, p.position, p.id",
    "-checked": "p.last_crawled_at IS NULL, p.last_crawled_at DESC, p.position, p.id",
}
# Maskura statuses that are a problem on their own, with no source flag (INTERFACES, changed 2026-10-16).
MASKURA_PROBLEM_STATUSES = ("LINK_BROKEN", "NOT_ON_MASKURA")
# What finalize_product gets for a slot with no URL: never pending, never a problem.
NO_URL_SOURCE = {"url": "", "platform": "none", "verdict": "NONE"}

# Raw fields a source fetch produces (INTERFACES fetch_source). Derived fields are computed only by
# sc_crawl.finalize_product, so they are never stored as raw.
RAW_SOURCE_FIELDS = ("url", "host", "platform", "verdict", "reason", "price_gbp", "available", "avail_text",
                     "title", "final_url", "http_status", "error", "removal_comment", "variation_unverified",
                     "currency", "fetched_at", "soft_block", "price_native", "price_seen")
DERIVED_SOURCE_FIELDS = ("price_diff", "pct_vs_source", "variant_mismatch", "variant_mismatch_tokens",
                         "price_suspect", "notes")


# ── Errors ───────────────────────────────────────────────────────────────────

class StoreError(Exception):
    pass


class SchemaMismatch(StoreError):
    def __init__(self, found, expected):
        super().__init__(f"database schema is version {found}, this code expects {expected}")
        self.found, self.expected = found, expected


class SnapshotExpired(StoreError):
    """The view snapshot is unknown or older than 8 hours. The UI says: This view is out of date. Reload view."""


class StaleWrite(StoreError):
    """A worker result no longer applies: the lease was lost, or the slot's URL changed since enqueue."""


# ── Time ─────────────────────────────────────────────────────────────────────

def utc_iso(dt=None):
    """UTC ISO 8601 with whole seconds and a trailing Z, e.g. 2026-10-16T03:12:00Z."""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def backup_name(dt=None):
    """File name for an online backup, sortable as text: source_check-20261016T031200Z.db."""
    return "source_check-" + utc_iso(dt).replace("-", "").replace(":", "") + ".db"


# ── Small pure helpers ───────────────────────────────────────────────────────

def name_key(value):
    """Case-folded actor key; the same rule as security.name_key."""
    return " ".join((value or "").casefold().split())


def is_transient_flag(code):
    return any(str(code).endswith(s) for s in TRANSIENT_FLAG_SUFFIXES)


# The decision statuses that are problems (sc_crawl.decide_product, owner's rule 2026-10-23).
PROBLEM_STATUSES = ("SOURCE_DEARER", "NO_BUYABLE_SOURCE", "MASKURA_GONE", "MASKURA_UNREADABLE")


def problem_from(finalized, maskura_status):
    """has_problem for a finalized product: its decision status when it has one; results finalized
    before the decision existed keep the old rule (any lasting flag, or a broken Maskura link)."""
    status = finalized.get("status")
    if status is not None:
        return status in PROBLEM_STATUSES
    return (any(not is_transient_flag(f) for f in finalized.get("flags") or [])
            or maskura_status in MASKURA_PROBLEM_STATUSES)


def identity_key(norm, raw):
    """The sheet identity of a Maskura cell: hv:<handle>|<variant>, raw:<text>, or none:."""
    if norm and norm.get("ok") and norm.get("handle"):
        return f"hv:{norm['handle'].lower()}|{norm.get('variant_id') or ''}"
    text = " ".join(str(raw or "").split()).casefold()
    return f"raw:{text}" if text else "none:"


def _within_tolerance(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    tol = max(PRICE_TOL_PCT * max(abs(a), abs(b)), PRICE_TOL_ABS)
    return abs(a - b) <= tol + 1e-9


_FP_PRICE_KEYS = ("maskura_price", "best_source_price")


def fingerprints_equal(a, b):
    """Compare two fingerprints: exact on every field except the two prices, which use the 2% or 50p tolerance."""
    if a is None or b is None:
        return a == b
    if a == b:
        return True
    try:
        da, db = json.loads(a), json.loads(b)
    except (TypeError, ValueError):
        return False
    if set(da) != set(db):
        return False
    for key in da:
        if key in _FP_PRICE_KEYS:
            if not _within_tolerance(da[key], db[key]):
                return False
        elif da[key] != db[key]:
            return False
    return True


def derive_maskura_status(link_ok, needs_confirm, suggested_url, result):
    """The Maskura column word key (sc_labels.MASKURA_STATUS), or None while the slot has no result yet."""
    if not link_ok:
        return "NO_LINK"
    if needs_confirm:
        return "LINK_CHANGED_CONFIRM"
    if suggested_url:
        return "LINK_CHANGED_SUGGESTION"
    if not result:
        return None
    status = result.get("status")
    if status == "OK" or (status is None and result.get("price_gbp") is not None and not result.get("error")):
        return "OK"
    if status in ("NOT_FOUND", "VARIANT_NOT_FOUND"):
        # A 404 is usually a renamed store URL. Only the P4 link check, after the catalog passes its
        # completeness check, may call a product Not on Maskura.
        return "LINK_BROKEN"
    return "READ_ERROR"


def _loads(text, default=None):
    if text is None or text == "":
        return default
    return json.loads(text)


def _dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _is_busy(exc):
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _is_resolved(p):
    return (p["review_status"] in RESOLVED_REVIEW_STATUSES
            or (bool(p["link_confirmed_by"]) and not p["link_needs_confirm"] and not p["has_problem"]
                and p["review_status"] not in ("still_differs", "changed_since_review")))


def _needs_action(p):
    # Fixed: re-checking and Being checked wait on the worker, not on a person, so they are left out (each is
    # shown as its own count). Review actions are refused on both.
    return ((bool(p["has_problem"]) or bool(p["link_needs_confirm"])
             or p["review_status"] in ("still_differs", "changed_since_review"))
            and p["review_status"] != "fixed_rechecking" and not p["being_checked"]
            and not _is_resolved(p) and not p["archived"])


def _in_total(p):
    return (not p["archived"]
            and (bool(p["has_problem"]) or bool(p["had_problem"]) or bool(p["link_needs_confirm"])
                 or bool(p["link_confirmed_by"]) or p["review_status"] != "to_review"))


# SQL versions of the three definitions (PLAN, status glossary "Definitions"). Keep them in step with the
# Python functions above; the summary fixture test checks both.
_RESOLVED_SQL = ("(p.review_status IN ('no_change_needed','fixed_confirmed') OR (p.link_confirmed_by IS NOT NULL"
                 " AND p.link_needs_confirm = 0 AND p.has_problem = 0"
                 " AND p.review_status NOT IN ('still_differs','changed_since_review')))")
_NEEDS_SQL = ("((p.has_problem = 1 OR p.link_needs_confirm = 1"
              " OR p.review_status IN ('still_differs','changed_since_review'))"
              " AND p.review_status <> 'fixed_rechecking' AND p.being_checked = 0"
              f" AND NOT {_RESOLVED_SQL} AND p.archived = 0)")
_IN_TOTAL_SQL = ("(p.archived = 0 AND (p.has_problem = 1 OR p.had_problem = 1 OR p.link_needs_confirm = 1"
                 " OR p.link_confirmed_by IS NOT NULL OR p.review_status <> 'to_review'))")


# ── Schema ───────────────────────────────────────────────────────────────────

def _in_list(values):
    return ",".join(f"'{v}'" for v in values)


_SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sheets (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        sha256 TEXT,
        source_columns INTEGER,
        row_count INTEGER NOT NULL DEFAULT 0,
        meta_json TEXT NOT NULL DEFAULT '{}',
        report_json TEXT,
        uploaded_by TEXT NOT NULL,
        uploaded_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        position INTEGER,
        name TEXT NOT NULL DEFAULT '',
        sheet_maskura_raw TEXT NOT NULL DEFAULT '',
        sheet_key TEXT NOT NULL DEFAULT 'none:',
        sheet_url TEXT,
        source_signature TEXT NOT NULL DEFAULT '',
        present_in_sheet INTEGER NOT NULL DEFAULT 1,
        last_sheet_id INTEGER REFERENCES sheets(id),
        duplicate_group TEXT,
        duplicate_resolution TEXT CHECK (duplicate_resolution IS NULL OR duplicate_resolution = 'keep_both'),
        maskura_url TEXT,
        handle TEXT,
        variant_id TEXT,
        link_ok INTEGER NOT NULL DEFAULT 0,
        link_reason TEXT,
        link_source TEXT NOT NULL DEFAULT 'sheet' CHECK (link_source IN ('sheet',{_in_list(LINK_METHODS)})),
        link_confidence REAL,
        link_version INTEGER NOT NULL DEFAULT 0,
        link_edited_by TEXT,
        link_edited_at TEXT,
        link_needs_confirm INTEGER NOT NULL DEFAULT 0,
        link_confirmed_by TEXT,
        link_confirmed_at TEXT,
        suggested_url TEXT,
        suggested_handle TEXT,
        suggested_variant_id TEXT,
        suggestion_score REAL,
        suggested_at TEXT,
        maskura_status TEXT,
        maskura_result_json TEXT,
        maskura_fetched_at TEXT,
        flags_json TEXT NOT NULL DEFAULT '[]',
        worst_verdict TEXT,
        being_checked INTEGER NOT NULL DEFAULT 0,
        has_problem INTEGER NOT NULL DEFAULT 0,
        had_problem INTEGER NOT NULL DEFAULT 0,
        result_fingerprint TEXT,
        results_version INTEGER NOT NULL DEFAULT 0,
        results_seq INTEGER NOT NULL DEFAULT 0,
        last_crawled_at TEXT,
        review_status TEXT NOT NULL DEFAULT 'to_review' CHECK (review_status IN ({_in_list(REVIEW_STATUSES)})),
        review_reason TEXT,
        review_by TEXT,
        review_at TEXT,
        review_version INTEGER NOT NULL DEFAULT 0,
        review_seq INTEGER NOT NULL DEFAULT 0,
        result_fingerprint_at_review TEXT,
        claimed_by TEXT,
        claimed_key TEXT,
        claimed_at TEXT,
        archived INTEGER NOT NULL DEFAULT 0,
        archive_reason TEXT CHECK (archive_reason IS NULL OR archive_reason IN ({_in_list(ARCHIVE_REASON_KEYS)})),
        archive_note TEXT,
        archived_by TEXT,
        archived_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS products_position ON products(position, id)",
    "CREATE INDEX IF NOT EXISTS products_handle ON products(handle, variant_id)",
    "CREATE INDEX IF NOT EXISTS products_sheet_key ON products(sheet_key)",
    "CREATE INDEX IF NOT EXISTS products_review ON products(review_status)",
    "CREATE INDEX IF NOT EXISTS products_claim ON products(claimed_key, claimed_at)",
    "CREATE INDEX IF NOT EXISTS products_results_seq ON products(results_seq)",
    "CREATE INDEX IF NOT EXISTS products_review_seq ON products(review_seq)",
    """CREATE TABLE IF NOT EXISTS sheet_rows (
        id INTEGER PRIMARY KEY,
        sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        maskura_raw TEXT NOT NULL DEFAULT '',
        source_urls_json TEXT NOT NULL DEFAULT '[]',
        product_id INTEGER REFERENCES products(id),
        match_kind TEXT,
        UNIQUE (sheet_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS source_results (
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        slot INTEGER NOT NULL CHECK (slot >= 1),
        url TEXT NOT NULL DEFAULT '',
        canonical_id TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '',
        platform TEXT,
        url_version INTEGER NOT NULL DEFAULT 0,
        raw_json TEXT,
        verdict TEXT,
        fetched_at TEXT,
        derived_json TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (product_id, slot)
    )""",
    "CREATE INDEX IF NOT EXISTS source_results_platform ON source_results(platform)",
    """CREATE TABLE IF NOT EXISTS product_aliases (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        alias_key TEXT NOT NULL,
        source_signature TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (product_id, alias_key)
    )""",
    "CREATE INDEX IF NOT EXISTS product_aliases_key ON product_aliases(alias_key)",
    """CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('bulk','auto_fix','import','undo')),
        action TEXT,
        value_json TEXT,
        snapshot_id TEXT,
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        ok_count INTEGER NOT NULL DEFAULT 0,
        conflict_count INTEGER NOT NULL DEFAULT 0,
        conflicts_json TEXT NOT NULL DEFAULT '[]',
        undone_at TEXT,
        undone_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS product_events (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        batch_id INTEGER REFERENCES batches(id),
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL,
        changes_json TEXT NOT NULL DEFAULT '{}',
        note TEXT,
        undoable INTEGER NOT NULL DEFAULT 0,
        review_version_after INTEGER,
        link_version_after INTEGER,
        undo_of INTEGER REFERENCES product_events(id),
        undone_by INTEGER REFERENCES product_events(id)
    )""",
    "CREATE INDEX IF NOT EXISTS product_events_product ON product_events(product_id, id)",
    "CREATE INDEX IF NOT EXISTS product_events_batch ON product_events(batch_id)",
    """CREATE TABLE IF NOT EXISTS view_snapshots (
        id TEXT PRIMARY KEY,
        filter_json TEXT NOT NULL,
        ids_json TEXT NOT NULL,
        versions_json TEXT NOT NULL,
        count INTEGER NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS view_snapshots_expiry ON view_snapshots(expires_at)",
    f"""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ({_in_list(JOB_KINDS)})),
        status TEXT NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL DEFAULT 0,
        params_json TEXT NOT NULL DEFAULT '{{}}',
        total INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        paused_by TEXT,
        paused_at TEXT,
        pause_reason TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS job_items (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        slot INTEGER NOT NULL,
        host TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        leased_at TEXT,
        leased_by TEXT,
        results_version_at_enqueue INTEGER,
        review_version_at_enqueue INTEGER,
        link_version_at_enqueue INTEGER,
        url_version_at_enqueue INTEGER,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS job_items_pick ON job_items(job_id, status, host, priority)",
    "CREATE INDEX IF NOT EXISTS job_items_product ON job_items(product_id, slot)",
    """CREATE TABLE IF NOT EXISTS worker_heartbeat (
        worker_id TEXT PRIMARY KEY,
        pid INTEGER,
        hostname TEXT,
        schema_version INTEGER,
        started_at TEXT NOT NULL,
        beat_at TEXT NOT NULL,
        state_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS host_health (
        host TEXT PRIMARY KEY,
        gap_s REAL,
        cooldown_until TEXT,
        cooldown_step INTEGER NOT NULL DEFAULT 0,
        blocked_recent INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT,
        last_at TEXT,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS maskura_catalog (
        handle TEXT PRIMARY KEY,
        shopify_id INTEGER,
        title TEXT,
        variants_json TEXT NOT NULL DEFAULT '[]',
        product_json TEXT,
        job_id INTEGER REFERENCES jobs(id),
        fetched_at TEXT NOT NULL
    )""",
]

# v2 (2026-10-16): a view snapshot remembers its sort order.
_SCHEMA_V2 = ["ALTER TABLE view_snapshots ADD COLUMN sort TEXT NOT NULL DEFAULT 'row'"]

# v3 (2026-10-16): the Maskura catalog cache that link suggestions and the link_fix job read (PLAN P4).
_SCHEMA_V3 = ["""CREATE TABLE IF NOT EXISTS catalog_products (
        handle TEXT PRIMARY KEY,
        title TEXT,
        product_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )"""]



def _jobs_allow_every_kind(c):
    """Run by every migrate() (2026-10-24): the jobs CHECK rule lists every kind in JOB_KINDS ("changed" was added).

    Not a schema version: older code still opens the database after a go-live rollback, and the wider rule only
    adds an allowed value.

    SQLite cannot alter a CHECK rule, and rebuilding the table would cascade-delete job_items through the foreign key,
    so the rule's text is rewritten in place: the procedure SQLite documents for changes that leave the stored data's
    format alone (sqlite.org/lang_altertable.html, section 7). Existing rows already pass the wider rule."""
    sql = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()[0]
    new = re.sub(r"kind IN \([^)]*\)", f"kind IN ({_in_list(JOB_KINDS)})", sql, count=1)
    if new == sql:
        return
    version = c.execute("PRAGMA schema_version").fetchone()[0]
    c.execute("PRAGMA writable_schema = ON")
    c.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'jobs'", (new,))
    c.execute(f"PRAGMA schema_version = {int(version) + 1}")
    c.execute("PRAGMA writable_schema = OFF")


MIGRATIONS = {1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3}
assert max(MIGRATIONS) == SCHEMA_VERSION

_PRODUCT_INSERT_COLS = ("position", "name", "sheet_maskura_raw", "sheet_key", "sheet_url", "source_signature",
                        "present_in_sheet", "last_sheet_id", "maskura_url", "handle", "variant_id", "link_ok",
                        "link_reason", "link_source", "maskura_status", "created_at", "updated_at")


# ── Store ────────────────────────────────────────────────────────────────────

class Store:
    def __init__(self, path, *, urls=None, clock=None, busy_attempts=12, finalizer=None):
        self.path = str(path)
        self._urls = urls
        self._finalizer = finalizer
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._local = threading.local()
        self._busy_attempts = busy_attempts

    # ── plumbing ──

    @property
    def finalizer(self):
        """finalize_product(maskura, sources) -> finalized; sc_crawl's by default (imported on first use)."""
        if self._finalizer is None:
            self._finalizer = importlib.import_module("sc_crawl").finalize_product
        return self._finalizer

    @property
    def urls(self):
        if self._urls is None:
            self._urls = importlib.import_module("sc_urls")
        return self._urls

    def now_dt(self):
        dt = self._clock()
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)

    def now(self):
        return utc_iso(self.now_dt())

    def _backoff(self, attempt):
        time.sleep(min(1.0, 0.02 * (2 ** attempt)) * (0.5 + random.random()))

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        for attempt in range(self._busy_attempts):
            try:
                conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA foreign_keys = ON")
                break
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.close()
                    conn = None
                if not _is_busy(exc) or attempt == self._busy_attempts - 1:
                    raise
                self._backoff(attempt)
        self._local.conn = conn
        self._local.depth = 0
        return conn

    def close(self):
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            self._local.depth = 0

    def pragma(self, name):
        return self._conn().execute(f"PRAGMA {name}").fetchone()[0]

    @contextlib.contextmanager
    def transaction(self, mode="IMMEDIATE"):
        """A write transaction. Nested use joins the outer transaction (the migration composes calls this way)."""
        conn = self._conn()
        if self._local.depth:
            self._local.depth += 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return
        for attempt in range(self._busy_attempts):
            try:
                conn.execute(f"BEGIN {mode}")
                break
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == self._busy_attempts - 1:
                    raise
                self._backoff(attempt)
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            self._local.depth = 0
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        self._local.depth = 0
        for attempt in range(self._busy_attempts):
            try:
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == self._busy_attempts - 1:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                self._backoff(attempt)

    def _write(self, fn, mode="IMMEDIATE"):
        """Run fn(conn) in one write transaction; a busy error anywhere retries the whole unit with backoff."""
        conn = self._conn()
        if self._local.depth:
            return fn(conn)
        for attempt in range(self._busy_attempts):
            try:
                with self.transaction(mode) as c:
                    return fn(c)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == self._busy_attempts - 1:
                    raise
                self._backoff(attempt)

    def _read(self, fn):
        """Run fn(conn) in one read transaction, so multi-query reads see one consistent state."""
        conn = self._conn()
        if self._local.depth:
            return fn(conn)
        conn.execute("BEGIN DEFERRED")
        try:
            return fn(conn)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    # ── schema ──

    def migrate(self):
        """Bring the schema to SCHEMA_VERSION. Safe to run from several processes at once."""
        def run(c):
            found = c.execute("PRAGMA user_version").fetchone()[0]
            if found > SCHEMA_VERSION:
                raise SchemaMismatch(found, SCHEMA_VERSION)
            if found == SCHEMA_VERSION:
                _jobs_allow_every_kind(c)
                return {"from": found, "to": found, "changed": False}
            for target in range(found + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS[target]:
                    c.execute(stmt)
            now = self.now()
            for key in ("results_seq", "review_seq"):
                c.execute("INSERT OR IGNORE INTO meta(key, value, updated_at) VALUES (?, '0', ?)", (key, now))
            c.execute("INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES ('schema_migrated_at', ?, ?)",
                      (now, now))
            _jobs_allow_every_kind(c)
            c.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            return {"from": found, "to": SCHEMA_VERSION, "changed": True}
        return self._write(run, mode="EXCLUSIVE")

    def check_version(self):
        found = self.pragma("user_version")
        if found != SCHEMA_VERSION:
            raise SchemaMismatch(found, SCHEMA_VERSION)
        return found

    # ── meta ──

    def get_meta(self, key, default=None):
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        def run(c):
            c.execute("INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                      "SET value = excluded.value, updated_at = excluded.updated_at",
                      (key, None if value is None else str(value), self.now()))
        self._write(run)

    def _bump_seq(self, c, key):
        row = c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1, updated_at = ? WHERE key = ? "
                        "RETURNING value", (self.now(), key)).fetchone()
        return int(row[0])

    def _seq(self, c, key):
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return int(row[0]) if row else 0

    # ── reading products ──

    def _row(self, c, pid):
        return c.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def _sources(self, c, pids):
        out = {pid: [] for pid in pids}
        if not pids:
            return out
        rows = c.execute("SELECT * FROM source_results WHERE product_id IN (SELECT value FROM json_each(?)) "
                         "ORDER BY product_id, slot", (json.dumps(list(pids)),)).fetchall()
        for r in rows:
            out[r["product_id"]].append({
                "slot": r["slot"], "url": r["url"], "canonical_id": r["canonical_id"], "host": r["host"],
                "platform": r["platform"], "url_version": r["url_version"], "verdict": r["verdict"],
                "fetched_at": r["fetched_at"], "raw": _loads(r["raw_json"]), "derived": _loads(r["derived_json"]),
            })
        return out

    def _product_dict(self, row, sources):
        p = dict(row)
        p["flags"] = _loads(p.pop("flags_json"), [])
        p["maskura"] = _loads(p.pop("maskura_result_json"))
        for key in ("present_in_sheet", "link_ok", "link_needs_confirm", "being_checked", "has_problem",
                    "had_problem", "archived"):
            p[key] = bool(p[key])
        cutoff = utc_iso(self.now_dt() - CLAIM_TTL)
        p["claim_live"] = bool(p["claimed_key"]) and (p["claimed_at"] or "") >= cutoff
        p["resolved"] = _is_resolved(p)
        p["needs_action"] = _needs_action(p)
        p["tags"] = ([] if p["present_in_sheet"] else ["no_longer_in_sheet"]) + (
            ["possible_duplicate"] if p["duplicate_group"] else [])
        p["sources"] = sources
        return p

    def get_product(self, pid):
        def run(c):
            row = self._row(c, pid)
            if row is None:
                return None
            return self._product_dict(row, self._sources(c, [pid])[pid])
        return self._read(run)

    def _get_in_tx(self, c, pid):
        row = self._row(c, pid)
        return None if row is None else self._product_dict(row, self._sources(c, [pid])[pid])

    def iter_products(self, filter=None, *, chunk=500):
        """Every product matching a filter, in sheet order, read `chunk` at a time (keyset, so memory stays flat
        for the 12k export). Each item has its sources, like get_product."""
        where, params = self._filter_sql(filter or {})
        last_pos, last_id = -1, -1
        while True:
            def run(c):
                rows = c.execute(
                    f"SELECT p.* FROM products p WHERE {where} AND (COALESCE(p.position, 1e18), p.id) > (?, ?) "
                    "ORDER BY COALESCE(p.position, 1e18), p.id LIMIT ?", (*params, last_pos, last_id, int(chunk))).fetchall()
                sources = self._sources(c, [r["id"] for r in rows])
                return [self._product_dict(r, sources[r["id"]]) for r in rows]
            batch = self._read(run)
            if not batch:
                return
            yield from batch
            tail = batch[-1]
            last_pos = tail["position"] if tail["position"] is not None else 1e18
            last_id = tail["id"]

    def iter_events(self, *, exclude_kinds=("imported",), chunk=1000):
        """Every event newest first, with the product's name and sheet row, `chunk` at a time."""
        before = None
        while True:
            rows = self._conn().execute(
                "SELECT e.*, p.name AS product_name, p.position AS product_row FROM product_events e "
                "LEFT JOIN products p ON p.id = e.product_id WHERE e.kind NOT IN (SELECT value FROM json_each(?)) "
                "AND (? IS NULL OR e.id < ?) ORDER BY e.id DESC LIMIT ?",
                (json.dumps(list(exclude_kinds)), before, before, int(chunk))).fetchall()
            if not rows:
                return
            for r in rows:
                yield self._event_dict(r)
            before = rows[-1]["id"]

    def history(self, pid, limit=200):
        rows = self._conn().execute("SELECT * FROM product_events WHERE product_id = ? ORDER BY id DESC LIMIT ?",
                                    (pid, int(limit))).fetchall()
        return [self._event_dict(r) for r in rows]

    def event_at(self, pid, *, review_version=None, link_version=None):
        """The event that set this review (or link) version on this product. Versions only go up, so it is one
        event, and Undo built on it never reverts a later change by someone else."""
        if review_version is not None:
            row = self._conn().execute("SELECT id FROM product_events WHERE product_id = ? AND review_version_after = ? "
                                       "ORDER BY id DESC LIMIT 1", (pid, review_version)).fetchone()
        elif link_version is not None:
            row = self._conn().execute("SELECT id FROM product_events WHERE product_id = ? AND link_version_after = ? "
                                       "ORDER BY id DESC LIMIT 1", (pid, link_version)).fetchone()
        else:
            raise ValueError("event_at needs review_version or link_version")
        return row[0] if row else None

    def batch_exists(self, batch_id):
        return self._conn().execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is not None

    def event_product(self, event_id):
        row = self._conn().execute("SELECT product_id FROM product_events WHERE id = ?", (event_id,)).fetchone()
        return row[0] if row else None

    def recent_events(self, limit=50):
        """The latest events across all reviewers, with the product's name and sheet row for display."""
        rows = self._conn().execute(
            "SELECT e.*, p.name AS product_name, p.position AS product_row FROM product_events e "
            "LEFT JOIN products p ON p.id = e.product_id WHERE e.kind <> 'imported' ORDER BY e.id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(r):
        e = dict(r)
        e["changes"] = _loads(e.pop("changes_json"), {})
        e["undoable"] = bool(e["undoable"])
        return e

    def _event(self, c, pid, kind, actor, *, changes=None, note=None, batch_id=None, undoable=False,
               review_version_after=None, link_version_after=None, undo_of=None):
        cur = c.execute(
            "INSERT INTO product_events(product_id, batch_id, at, actor, kind, changes_json, note, undoable, "
            "review_version_after, link_version_after, undo_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, batch_id, self.now(), actor, kind, _dumps(changes or {}), note, 1 if undoable else 0,
             review_version_after, link_version_after, undo_of))
        return cur.lastrowid

    def _update(self, c, pid, fields):
        fields = dict(fields)
        fields["updated_at"] = self.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE products SET {cols} WHERE id = ?", (*fields.values(), pid))

    # ── import ──

    def _prepare_rows(self, rows):
        out, seen = [], set()
        for r in rows:
            position = int(r["position"])
            if position in seen:
                raise ValueError(f"duplicate sheet position {position}")
            seen.add(position)
            raw = str(r.get("maskura_raw") or "")
            if raw.strip():
                norm = self.urls.normalize_maskura_url(raw)
            else:
                norm = {"url": None, "handle": None, "variant_id": None, "raw": raw, "ok": False,
                        "reason": "empty"}
            src = [str(u or "").strip() for u in (r.get("source_urls") or [])]
            canon = [self.urls.canonical_source_id(u) if u else "" for u in src]
            out.append({
                "position": position, "name": str(r.get("name") or "").strip(), "maskura_raw": raw,
                "norm": norm, "key": identity_key(norm, raw), "source_urls": src, "canon": canon,
                "signature": self.urls.source_signature([u for u in src if u]),
            })
        return out

    def _plan_import(self, c, prepared):
        prods = {r["id"]: dict(r) for r in c.execute(
            "SELECT id, position, sheet_key, handle, variant_id, source_signature, archived, present_in_sheet, "
            "link_source, maskura_url, duplicate_resolution FROM products")}
        by_alias, by_key = {}, {}
        for r in c.execute("SELECT alias_key, product_id FROM product_aliases"):
            by_alias.setdefault(r[0], set()).add(r[1])
        for pid, p in prods.items():
            by_key.setdefault(p["sheet_key"], set()).add(pid)
            if p["handle"]:
                by_key.setdefault(f"hv:{p['handle'].lower()}|{p['variant_id'] or ''}", set()).add(pid)

        assign = {}          # row index -> (product id, kind)
        taken = set()
        order = sorted(range(len(prepared)), key=lambda i: prepared[i]["position"])
        tiers = (
            lambda row, p: row["signature"] == p["source_signature"] and row["position"] == p["position"],
            lambda row, p: row["signature"] == p["source_signature"],
            lambda row, p: row["position"] == p["position"],
            lambda row, p: True,
        )
        for kind, index in (("alias", by_alias), ("key", by_key)):
            for test in tiers:
                for i in order:
                    if i in assign:
                        continue
                    row = prepared[i]
                    cands = [prods[pid] for pid in index.get(row["key"], ()) if pid not in taken]
                    cands = [p for p in cands if test(row, p)]
                    if not cands:
                        continue
                    best = min(cands, key=lambda p: (abs((p["position"] or 0) - row["position"]), p["id"]))
                    assign[i] = (best["id"], kind)
                    taken.add(best["id"])

        groups = {}
        for i, row in enumerate(prepared):
            if row["key"].startswith("hv:"):
                groups.setdefault((row["key"], row["signature"]), []).append(i)
        dup_rows = {i for members in groups.values() if len(members) > 1 for i in members}
        dup_group_of = {i: f"{k}#{s}" for (k, s), members in groups.items() if len(members) > 1 for i in members}
        gone = [pid for pid in prods if pid not in taken]
        return {"prods": prods, "assign": assign, "gone": gone, "dup_rows": dup_rows, "dup_group_of": dup_group_of}

    def _report(self, plan, prepared, dry_run):
        prods = plan["prods"]
        cats = {k: [] for k in ("matched", "new", "gone", "link_differs", "possible_duplicates",
                                "archived_in_sheet", "no_link")}
        for i, row in enumerate(prepared):
            ref = {"position": row["position"], "name": row["name"]}
            hit = plan["assign"].get(i)
            if hit:
                p = prods[hit[0]]
                ref["product_id"] = p["id"]
                cats["matched"].append(ref)
                if p["archived"]:
                    cats["archived_in_sheet"].append(ref)
                if p["link_source"] != "sheet" and row["norm"].get("url") != p["maskura_url"]:
                    cats["link_differs"].append(dict(ref, sheet_url=row["norm"].get("url"),
                                                     corrected_url=p["maskura_url"]))
            else:
                cats["new"].append(ref)
            if i in plan["dup_rows"]:
                cats["possible_duplicates"].append(ref)
            if not row["norm"].get("ok"):
                cats["no_link"].append(dict(ref, raw=row["maskura_raw"][:120], reason=row["norm"].get("reason")))
        for pid in plan["gone"]:
            p = prods[pid]
            if p["present_in_sheet"] and not p["archived"]:
                cats["gone"].append({"position": p["position"], "product_id": pid})
        return {
            "dry_run": dry_run, "rows": len(prepared),
            "counts": {k: len(v) for k, v in cats.items()},
            "samples": {k: v[:10] for k, v in cats.items()},
        }

    def latest_sheet(self):
        """The newest applied sheet: {id, name, rows, uploaded_by, uploaded_at, counts}, or None."""
        r = self._conn().execute("SELECT id, name, row_count, uploaded_by, uploaded_at, report_json FROM sheets "
                                 "ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            return None
        return {"id": r["id"], "name": r["name"], "rows": r["row_count"], "uploaded_by": r["uploaded_by"],
                "uploaded_at": r["uploaded_at"], "counts": _loads(r["report_json"], {})}

    def sheet_assignments(self, sheet_id):
        """[(position, product id)] for an applied sheet, in sheet order."""
        return [(r[0], r[1]) for r in self._conn().execute(
            "SELECT position, product_id FROM sheet_rows WHERE sheet_id = ? ORDER BY position", (sheet_id,))]

    def match_rows(self, rows):
        """[(position, product id or None)] using the import matching order, without writing anything."""
        prepared = self._prepare_rows(rows)
        plan = self._read(lambda c: self._plan_import(c, prepared))
        return [(row["position"], (plan["assign"].get(i) or (None,))[0]) for i, row in enumerate(prepared)]

    def import_sheet(self, rows, *, sheet_meta=None, actor, dry_run=False):
        """Match sheet rows to products and apply the sheet, or only report what would change (dry_run)."""
        prepared = self._prepare_rows(rows)

        def run(c):
            plan = self._plan_import(c, prepared)
            report = self._report(plan, prepared, dry_run)
            if not dry_run:
                report["sheet_id"] = self._apply_import(c, plan, prepared, sheet_meta or {}, actor, report)
            return report
        return self._read(run) if dry_run else self._write(run)

    def _apply_import(self, c, plan, prepared, sheet_meta, actor, report):
        now = self.now()
        cur = c.execute("INSERT INTO batches(kind, action, actor, created_at, total) VALUES ('import', 'import', ?, "
                        "?, ?)", (actor, now, len(prepared)))
        batch_id = cur.lastrowid
        cur = c.execute(
            "INSERT INTO sheets(name, sha256, source_columns, row_count, meta_json, uploaded_by, uploaded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(sheet_meta.get("name") or ""), sheet_meta.get("sha256"), sheet_meta.get("source_columns"),
             len(prepared), _dumps(sheet_meta), actor, now))
        sheet_id = cur.lastrowid
        seq = self._bump_seq(c, "results_seq")
        prods = plan["prods"]
        row_products = {}

        for i, row in enumerate(prepared):
            norm = row["norm"]
            link_ok = bool(norm.get("ok"))
            dup_group = plan["dup_group_of"].get(i)
            hit = plan["assign"].get(i)
            sheet_fields = {
                "position": row["position"], "name": row["name"], "sheet_maskura_raw": row["maskura_raw"],
                "sheet_key": row["key"], "sheet_url": norm.get("url"), "source_signature": row["signature"],
                "present_in_sheet": 1, "last_sheet_id": sheet_id,
            }
            if hit is None:
                vals = dict(sheet_fields, maskura_url=norm.get("url"), handle=norm.get("handle"),
                            variant_id=norm.get("variant_id"), link_ok=1 if link_ok else 0,
                            link_reason=None if link_ok else norm.get("reason"), link_source="sheet",
                            maskura_status=derive_maskura_status(link_ok, False, None, None),
                            created_at=now, updated_at=now)
                cur = c.execute(f"INSERT INTO products({', '.join(_PRODUCT_INSERT_COLS)}) VALUES "
                                f"({', '.join('?' * len(_PRODUCT_INSERT_COLS))})",
                                tuple(vals[k] for k in _PRODUCT_INSERT_COLS))
                pid = cur.lastrowid
                # Never crawled: "Being checked" until the first finalize, never "No problem found".
                self._update(c, pid, {"duplicate_group": dup_group, "results_seq": seq,
                                      "being_checked": 1 if link_ok or any(row["source_urls"]) else 0})
                c.executemany(
                    "INSERT INTO source_results(product_id, slot, url, canonical_id, host, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    [(pid, s, u, row["canon"][s - 1], self.urls.host_of(u) if u else "", now)
                     for s, u in enumerate(row["source_urls"], 1)])
                self._event(c, pid, "imported", actor, batch_id=batch_id,
                            changes={"position": [None, row["position"]]})
                row_products[i] = (pid, "new")
                continue

            pid, kind = hit
            p = self._row(c, pid)
            fields = dict(sheet_fields)
            if not p["present_in_sheet"]:
                self._event(c, pid, "back_in_sheet", actor, batch_id=batch_id)
            if p["duplicate_resolution"] != "keep_both":
                fields["duplicate_group"] = dup_group
            link_changed = p["link_source"] == "sheet" and (p["maskura_url"], p["handle"], p["variant_id"]) != (
                norm.get("url"), norm.get("handle"), norm.get("variant_id"))
            if link_changed:
                # The sheet owns this link (nobody edited it in the portal), so the sheet's new link applies.
                fields.update(maskura_url=norm.get("url"), handle=norm.get("handle"),
                              variant_id=norm.get("variant_id"), link_ok=1 if link_ok else 0,
                              link_reason=None if link_ok else norm.get("reason"),
                              link_version=p["link_version"] + 1, maskura_result_json=None,
                              maskura_fetched_at=None,
                              maskura_status=derive_maskura_status(link_ok, False, p["suggested_url"], None))
                self._event(c, pid, "sheet_link_changed", actor, batch_id=batch_id,
                            changes={"maskura_url": [p["maskura_url"], norm.get("url")]},
                            link_version_after=p["link_version"] + 1)
            changed = any(p[k] != v for k, v in fields.items() if k != "last_sheet_id")
            slot_changes = self._apply_sources(c, pid, row, now)
            if slot_changes:
                self._event(c, pid, "source_url_changed", actor, batch_id=batch_id, changes=slot_changes)
            if (link_changed and link_ok) or any(new for _, new in slot_changes.values()):
                # The old result belongs to the old link, so the row waits for a fresh check.
                fields["being_checked"] = 1
            if changed or slot_changes:
                fields["results_seq"] = seq
                self._update(c, pid, fields)
            else:
                c.execute("UPDATE products SET last_sheet_id = ? WHERE id = ?", (sheet_id, pid))
            row_products[i] = (pid, kind)

        for pid in plan["gone"]:
            if prods[pid]["present_in_sheet"]:
                self._update(c, pid, {"present_in_sheet": 0, "results_seq": seq})
                self._event(c, pid, "gone_from_sheet", actor, batch_id=batch_id)

        c.executemany(
            "INSERT INTO sheet_rows(sheet_id, position, name, maskura_raw, source_urls_json, product_id, match_kind) "
            "VALUES (?,?,?,?,?,?,?)",
            [(sheet_id, row["position"], row["name"], row["maskura_raw"], _dumps(row["source_urls"]),
              row_products[i][0], row_products[i][1]) for i, row in enumerate(prepared)])
        c.execute("UPDATE sheets SET report_json = ? WHERE id = ?", (_dumps(report["counts"]), sheet_id))
        c.execute("UPDATE batches SET ok_count = ? WHERE id = ?", (len(prepared), batch_id))
        return sheet_id

    def _apply_sources(self, c, pid, row, now):
        """Update a matched product's source slots from the sheet. Returns {slot: [old url, new url]}."""
        existing = {r["slot"]: r for r in c.execute("SELECT * FROM source_results WHERE product_id = ?", (pid,))}
        changes = {}
        n = max(len(row["source_urls"]), max(existing, default=0))
        for slot in range(1, n + 1):
            url = row["source_urls"][slot - 1] if slot <= len(row["source_urls"]) else ""
            canon = row["canon"][slot - 1] if slot <= len(row["canon"]) else ""
            old = existing.get(slot)
            if old is None:
                c.execute("INSERT INTO source_results(product_id, slot, url, canonical_id, host, updated_at) "
                          "VALUES (?,?,?,?,?,?)", (pid, slot, url, canon, self.urls.host_of(url) if url else "", now))
                if url:
                    changes[str(slot)] = [None, url]
            elif old["canonical_id"] != canon:
                c.execute("UPDATE source_results SET url = ?, canonical_id = ?, host = ?, platform = NULL, "
                          "url_version = url_version + 1, raw_json = NULL, verdict = NULL, fetched_at = NULL, "
                          "derived_json = NULL, updated_at = ? WHERE product_id = ? AND slot = ?",
                          (url, canon, self.urls.host_of(url) if url else "", now, pid, slot))
                changes[str(slot)] = [old["url"], url]
            elif old["url"] != url:
                # Same listing, different tracking text: keep the result.
                c.execute("UPDATE source_results SET url = ?, updated_at = ? WHERE product_id = ? AND slot = ?",
                          (url, now, pid, slot))
        return changes

    # ── results ──

    def record_maskura(self, product_id, maskura_raw, *, link_version):
        """Store the raw Maskura fetch. Commits only when link_version still matches (no edit since enqueue)."""
        def run(c):
            p = self._row(c, product_id)
            if p is None or p["link_version"] != link_version:
                return False
            raw = dict(maskura_raw or {})
            self._update(c, product_id, {
                "maskura_result_json": _dumps(raw), "maskura_fetched_at": raw.get("fetched_at") or self.now(),
                "maskura_status": derive_maskura_status(p["link_ok"], p["link_needs_confirm"], p["suggested_url"],
                                                        raw),
            })
            return True
        return self._write(run)

    def record_source(self, product_id, slot, raw, *, lease_token=None):
        """Store one raw source fetch. Derived keys in `raw` are dropped; finalize() stores them."""
        def run(c):
            row = c.execute("SELECT * FROM source_results WHERE product_id = ? AND slot = ?",
                            (product_id, slot)).fetchone()
            if row is None:
                raise StaleWrite(f"product {product_id} has no source slot {slot}")
            if lease_token is not None:
                item = c.execute("SELECT id FROM job_items WHERE product_id = ? AND slot = ? AND lease_token = ? "
                                 "AND status = 'leased'", (product_id, slot, lease_token)).fetchone()
                if item is None:
                    raise StaleWrite("lease lost or already committed")
                c.execute("UPDATE job_items SET status = 'done', lease_token = NULL, updated_at = ? WHERE id = ?",
                          (self.now(), item["id"]))
            data = {k: v for k, v in dict(raw or {}).items() if k not in DERIVED_SOURCE_FIELDS}
            url = data.get("url")
            if url and self.urls.canonical_source_id(url) != row["canonical_id"]:
                raise StaleWrite(f"slot {slot} of product {product_id} now points at another listing")
            c.execute("UPDATE source_results SET raw_json = ?, verdict = ?, fetched_at = ?, "
                      "platform = COALESCE(?, platform), host = COALESCE(NULLIF(?, ''), host), updated_at = ? "
                      "WHERE product_id = ? AND slot = ?",
                      (_dumps(data), data.get("verdict"), data.get("fetched_at") or self.now(), data.get("platform"),
                       data.get("host") or "", self.now(), product_id, slot))
        self._write(run)

    def _fresh_cutoff(self, max_age_s):
        return utc_iso(self.now_dt() - datetime.timedelta(seconds=max_age_s))

    def reuse_recent_results(self, *, max_age_s=REUSE_MAX_AGE_S):
        """Copy a result at most max_age_s old from another row with the same listing (non-empty canonical_id) or the
        same Maskura page (handle and variant) into every slot that has no fresh result, through record_source and
        record_maskura, keeping the original fetched_at. Then finalize each touched product that has a fresh result
        for every part. Returns {"sources", "maskura", "finalized"}."""
        def run(c):
            cutoff = self._fresh_cutoff(max_age_s)
            touched, n_src, n_mk = set(), 0, 0
            for s in c.execute(
                    "SELECT s.product_id, s.slot, s.url, s.canonical_id FROM source_results s JOIN products p ON "
                    "p.id = s.product_id WHERE p.archived = 0 AND s.url <> '' AND s.canonical_id <> '' AND "
                    "(s.raw_json IS NULL OR s.fetched_at IS NULL OR s.fetched_at < ?)", (cutoff,)).fetchall():
                donor = c.execute(
                    "SELECT raw_json FROM source_results WHERE canonical_id = ? AND raw_json IS NOT NULL AND "
                    "fetched_at >= ? AND NOT (product_id = ? AND slot = ?) ORDER BY fetched_at DESC LIMIT 1",
                    (s["canonical_id"], cutoff, s["product_id"], s["slot"])).fetchone()
                if donor is None:
                    continue
                self.record_source(s["product_id"], s["slot"], dict(_loads(donor["raw_json"]), url=s["url"]))
                touched.add(s["product_id"])
                n_src += 1
            for p in c.execute(
                    "SELECT id, handle, variant_id, link_version FROM products WHERE archived = 0 AND link_ok = 1 "
                    "AND handle IS NOT NULL AND (maskura_result_json IS NULL OR maskura_fetched_at IS NULL OR "
                    "maskura_fetched_at < ?)", (cutoff,)).fetchall():
                donor = c.execute(
                    "SELECT maskura_result_json FROM products WHERE handle = ? AND variant_id IS ? AND id <> ? AND "
                    "maskura_result_json IS NOT NULL AND maskura_fetched_at >= ? ORDER BY maskura_fetched_at DESC "
                    "LIMIT 1", (p["handle"], p["variant_id"], p["id"], cutoff)).fetchone()
                if donor is None:
                    continue
                self.record_maskura(p["id"], _loads(donor["maskura_result_json"]), link_version=p["link_version"])
                touched.add(p["id"])
                n_mk += 1
            finalized = 0
            for pid in sorted(touched):
                if self._needs_check(c, pid, cutoff):
                    continue
                p = self._row(c, pid)
                sources = [dict(NO_URL_SOURCE) if not r["url"] else _loads(r["raw_json"]) for r in c.execute(
                    "SELECT url, raw_json FROM source_results WHERE product_id = ? ORDER BY slot", (pid,))]
                self.finalize(pid, self.finalizer(_loads(p["maskura_result_json"]), sources))
                finalized += 1
            return {"sources": n_src, "maskura": n_mk, "finalized": finalized}
        return self._write(run)

    def _needs_check(self, c, pid, cutoff):
        """The slots of one product that have no result from after cutoff (MASKURA_SLOT for the Maskura page)."""
        p = self._row(c, pid)
        out = []
        if p["link_ok"] and p["maskura_url"] and (not p["maskura_result_json"] or not p["maskura_fetched_at"]
                                                  or p["maskura_fetched_at"] < cutoff):
            out.append(MASKURA_SLOT)
        out += [r["slot"] for r in c.execute(
            "SELECT slot FROM source_results WHERE product_id = ? AND url <> '' AND (raw_json IS NULL OR fetched_at "
            "IS NULL OR fetched_at < ?) ORDER BY slot", (pid, cutoff))]
        return out

    def fingerprint(self, product_row, finalized):
        """The result fingerprint: verdict, final flags, availability per slot, Maskura status, two prices.

        The prices are kept exact here; `fingerprints_equal` compares them with the larger of 2% or 50p,
        and `finalize` keeps the earlier fingerprint while a new one is equal under that rule.
        """
        p = dict(product_row)
        maskura = p.get("maskura")
        if maskura is None:
            maskura = _loads(p.get("maskura_result_json"))
        mprice = (maskura or {}).get("price_gbp")
        sources = [s or {} for s in (finalized.get("sources") or [])]
        prices = [float(s["price_gbp"]) for s in sources if s.get("price_gbp") is not None]
        return _dumps({
            "verdict": finalized.get("worst_verdict"),
            "flags": sorted(f for f in (finalized.get("flags") or []) if not is_transient_flag(f)),
            "available": [s.get("available") for s in sources],
            "maskura_status": p.get("maskura_status"),
            "maskura_price": None if mprice is None else round(float(mprice), 2),
            "best_source_price": round(min(prices), 2) if prices else None,
        })

    def finalize(self, product_id, finalized, *, crawled_at=None, expected_review_version=None):
        """Store the derived fields and flags; bump results_version only when the fingerprint changed.

        Also applies the review rules that depend on results (never while the row is being checked):
        a resolved review whose fingerprint moved becomes Changed since review, and Fixed: re-checking becomes
        Fixed, confirmed or Still differs (only when expected_review_version, if given, still matches).
        """
        def run(c):
            p = self._row(c, product_id)
            if p is None:
                raise StoreError(f"no product {product_id}")
            now = self.now()
            flags = list(finalized.get("flags") or [])
            being_checked = bool(finalized.get("being_checked"))
            has_problem = problem_from(finalized, p["maskura_status"])
            for slot, s in enumerate(finalized.get("sources") or [], 1):
                derived = None if s is None else {k: s[k] for k in DERIVED_SOURCE_FIELDS if k in s}
                c.execute("UPDATE source_results SET derived_json = ?, updated_at = ? WHERE product_id = ? AND "
                          "slot = ?", (None if derived is None else _dumps(derived), now, product_id, slot))
            fp = self.fingerprint(p, finalized)
            changed = not fingerprints_equal(p["result_fingerprint"], fp)
            fields = {
                "flags_json": _dumps(flags), "worst_verdict": finalized.get("worst_verdict"),
                "being_checked": 1 if being_checked else 0, "has_problem": 1 if has_problem else 0,
                "had_problem": 1 if (has_problem or p["had_problem"]) else 0,
                "last_crawled_at": crawled_at or now,
            }
            anchor = p["result_fingerprint"]
            if changed:
                anchor = fp
                fields["result_fingerprint"] = fp
                fields["results_version"] = p["results_version"] + 1
            if changed or bool(p["being_checked"]) != being_checked or p["flags_json"] != fields["flags_json"]:
                fields["results_seq"] = self._bump_seq(c, "results_seq")

            if not being_checked:
                status = p["review_status"]
                new_status = None
                if (status in RESOLVED_REVIEW_STATUSES and p["result_fingerprint_at_review"] is not None
                        and not fingerprints_equal(p["result_fingerprint_at_review"], anchor)):
                    new_status = "changed_since_review"
                elif status == "fixed_rechecking" and (expected_review_version is None
                                                       or expected_review_version == p["review_version"]):
                    new_status = "still_differs" if has_problem else "fixed_confirmed"
                if new_status:
                    version = p["review_version"] + 1
                    fields.update(review_status=new_status, review_version=version,
                                  review_seq=self._bump_seq(c, "review_seq"))
                    if new_status != "changed_since_review":
                        fields["result_fingerprint_at_review"] = anchor
                    self._event(c, product_id, "review_outcome", SYSTEM_ACTOR,
                                changes={"review_status": [status, new_status]}, review_version_after=version)
            self._update(c, product_id, fields)
            return {"changed": changed, "results_version": fields.get("results_version", p["results_version"])}
        return self._write(run)

    # ── review operations (P3) ──

    def _conflict(self, p, reason, **extra):
        return dict({"reason": reason, "review_version": p["review_version"], "link_version": p["link_version"]},
                    **extra)

    def _touch_claim(self, p, actor, fields):
        if p["claimed_key"] and p["claimed_key"] == name_key(actor):
            fields["claimed_at"] = self.now()

    def _op_set_review(self, c, p, status, actor, reason, note, batch_id):
        if p["archived"]:
            return self._conflict(p, "archived")
        if p["being_checked"]:
            return self._conflict(p, "being_checked")
        now = self.now()
        new = {"review_status": status, "review_reason": reason if status == "no_change_needed" else None,
               "review_by": actor, "review_at": now, "result_fingerprint_at_review": p["result_fingerprint"]}
        changes = {k: [p[k], v] for k, v in new.items()}
        version = p["review_version"] + 1
        fields = dict(new, review_version=version, review_seq=self._bump_seq(c, "review_seq"))
        self._touch_claim(p, actor, fields)
        self._update(c, p["id"], fields)
        self._event(c, p["id"], "review", actor, changes=changes, batch_id=batch_id, undoable=True,
                    review_version_after=version, note=note)
        return None

    def _check_review_args(self, status, reason):
        if status not in MANUAL_REVIEW_STATUSES:
            raise ValueError(f"review status {status!r} cannot be set by hand")
        if status == "no_change_needed" and not (reason or "").strip():
            raise ValueError("No change needed takes a reason")

    def _versioned(self, pid, expected_version, op, version_field="review_version"):
        """Run op(c, p) -> conflict|None under a version check; return (ok, product, conflict)."""
        def run(c):
            p = self._row(c, pid)
            if p is None:
                return False, None, {"reason": "not_found"}
            if p[version_field] != expected_version:
                return False, self._get_in_tx(c, pid), self._conflict(p, "version")
            conflict = op(c, p)
            return conflict is None, self._get_in_tx(c, pid), conflict
        return self._write(run)

    def set_review(self, id, status, *, actor, expected_version, reason=None, note=None):
        self._check_review_args(status, reason)
        return self._versioned(id, expected_version,
                               lambda c, p: self._op_set_review(c, p, status, actor, reason, note, None))

    def add_note(self, id, text, *, actor):
        """Notes are append-only: one event per note, never edited or undone."""
        text = (text or "").strip()
        if not text:
            raise ValueError("a note needs text")

        def run(c):
            p = self._row(c, id)
            if p is None:
                return None
            fields = {"review_seq": self._bump_seq(c, "review_seq")}
            self._touch_claim(p, actor, fields)
            self._update(c, id, fields)
            return self._event(c, id, "note", actor, note=text)
        return self._write(run)

    def _claim_cutoff(self):
        return utc_iso(self.now_dt() - CLAIM_TTL)

    def _op_claim(self, c, p, actor, batch_id, seq):
        key = name_key(actor)
        if p["archived"]:
            return self._conflict(p, "archived")
        live = p["claimed_key"] and (p["claimed_at"] or "") >= self._claim_cutoff()
        if live and p["claimed_key"] != key:
            return self._conflict(p, "claimed", claimed_by=p["claimed_by"])
        self._update(c, p["id"], {"claimed_by": actor, "claimed_key": key, "claimed_at": self.now(),
                                  "review_seq": seq})
        if p["claimed_key"] != key:
            self._event(c, p["id"], "claim", actor, batch_id=batch_id,
                        changes={"claimed_by": [p["claimed_by"], actor]})
        return None

    def claim(self, ids, *, actor):
        """Mark rows as being reviewed by `actor`. Returns {claimed, taken, missing}."""
        def run(c):
            out = {"claimed": [], "taken": [], "missing": []}
            seq = self._bump_seq(c, "review_seq")
            for pid in ids:
                p = self._row(c, pid)
                if p is None:
                    out["missing"].append(pid)
                elif self._op_claim(c, p, actor, None, seq) is None:
                    out["claimed"].append(pid)
                else:
                    out["taken"].append(pid)
            return out
        return self._write(run)

    def release_claims(self, *, actor):
        def run(c):
            rows = c.execute("SELECT id, claimed_by FROM products WHERE claimed_key = ?",
                             (name_key(actor),)).fetchall()
            if rows:
                seq = self._bump_seq(c, "review_seq")
                for r in rows:
                    self._update(c, r["id"], {"claimed_by": None, "claimed_key": None, "claimed_at": None,
                                              "review_seq": seq})
                    self._event(c, r["id"], "release", actor, changes={"claimed_by": [r["claimed_by"], None]})
            return {"released": len(rows)}
        return self._write(run)

    def reset_had_problem(self):
        """Start a new "Reviewed N of TOTAL" count: called when a full job starts, because TOTAL counts the
        rows that have or had a problem in the CURRENT results. Returns the number of rows reset."""
        def run(c):
            cur = c.execute("UPDATE products SET had_problem = has_problem WHERE had_problem <> has_problem")
            if cur.rowcount:
                self._bump_seq(c, "results_seq")
            return cur.rowcount
        return self._write(run)

    def take_next(self, n, *, actor, view_filter=None):
        """Atomically claim the next n Needs action rows that are not being checked and not claimed by others."""
        def run(c):
            where, params = self._filter_sql(view_filter or {}, actor)
            key = name_key(actor)
            rows = c.execute(
                f"SELECT p.id FROM products p WHERE {where} AND {_NEEDS_SQL} AND p.being_checked = 0 "
                "AND (p.claimed_key IS NULL OR p.claimed_at < ?) ORDER BY p.position, p.id LIMIT ?",
                (*params, self._claim_cutoff(), max(0, int(n)))).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                seq = self._bump_seq(c, "review_seq")
                for pid in ids:
                    self._op_claim(c, self._row(c, pid), actor, None, seq)
            return {"claimed": ids, "actor_key": key}
        return self._write(run)

    def _op_archive(self, c, p, reason, actor, note, batch_id):
        if p["archived"]:
            return self._conflict(p, "already_archived")
        new = {"archived": 1, "archive_reason": reason, "archive_note": note, "archived_by": actor,
               "archived_at": self.now(), "claimed_by": None, "claimed_key": None, "claimed_at": None}
        return self._apply_review_change(c, p, "archive", actor, new, batch_id, note=note)

    def _op_restore(self, c, p, actor, note, batch_id):
        if not p["archived"]:
            return self._conflict(p, "not_archived")
        new = {"archived": 0, "archive_reason": None, "archive_note": None, "archived_by": None,
               "archived_at": None}
        return self._apply_review_change(c, p, "restore", actor, new, batch_id, note=note)

    def _apply_review_change(self, c, p, kind, actor, new, batch_id, note=None):
        changes = {k: [p[k], v] for k, v in new.items()}
        version = p["review_version"] + 1
        fields = dict(new, review_version=version, review_seq=self._bump_seq(c, "review_seq"))
        if "claimed_key" not in new:
            self._touch_claim(p, actor, fields)
        self._update(c, p["id"], fields)
        self._event(c, p["id"], kind, actor, changes=changes, batch_id=batch_id, undoable=True,
                    review_version_after=version, note=note)
        return None

    def _check_archive_args(self, reason, note):
        if reason not in ARCHIVE_REASON_KEYS:
            raise ValueError(f"unknown archive reason {reason!r}")
        if reason == "other" and not (note or "").strip():
            raise ValueError("Archive reason Other takes a note")

    def archive(self, id, reason, *, actor, expected_version, note=None):
        self._check_archive_args(reason, note)
        return self._versioned(id, expected_version, lambda c, p: self._op_archive(c, p, reason, actor, note, None))

    def restore(self, id, *, actor, expected_version, note=None):
        return self._versioned(id, expected_version, lambda c, p: self._op_restore(c, p, actor, note, None))

    def _link_fields(self, c, p, url_info, actor, method, confidence):
        now = self.now()
        needs_confirm = 1 if method == "auto" else 0
        new = {
            "maskura_url": url_info["url"], "handle": url_info.get("handle"), "variant_id": url_info.get("variant_id"),
            "link_ok": 1, "link_reason": None, "link_source": method, "link_confidence": confidence,
            "link_edited_by": actor, "link_edited_at": now, "link_needs_confirm": needs_confirm,
            "link_confirmed_by": actor if method != "auto" else None,
            "link_confirmed_at": now if method != "auto" else None,
            "suggested_url": None, "suggested_handle": None, "suggested_variant_id": None,
            "suggestion_score": None, "suggested_at": None,
            "maskura_result_json": None, "maskura_fetched_at": None,
            "maskura_status": derive_maskura_status(True, needs_confirm, None, None),
            # The old result belongs to the old link: the row waits for a fresh check (undo restores it).
            "being_checked": 1,
        }
        c.execute("INSERT OR IGNORE INTO product_aliases(product_id, alias_key, source_signature, created_by, "
                  "created_at) VALUES (?,?,?,?,?)", (p["id"], p["sheet_key"], p["source_signature"], actor, now))
        return new

    def _apply_link_change(self, c, p, kind, actor, new, batch_id):
        changes = {k: [p[k], v] for k, v in new.items()}
        review_v, link_v = p["review_version"] + 1, p["link_version"] + 1
        fields = dict(new, review_version=review_v, link_version=link_v,
                      review_seq=self._bump_seq(c, "review_seq"), results_seq=self._bump_seq(c, "results_seq"))
        self._touch_claim(p, actor, fields)
        self._update(c, p["id"], fields)
        self._event(c, p["id"], kind, actor, changes=changes, batch_id=batch_id, undoable=True,
                    review_version_after=review_v, link_version_after=link_v)

    def set_link(self, id, url_info, *, actor, method, confidence, expected_link_version, batch_id=None):
        """Point the Maskura slot at a new link. `method` auto leaves the row at Link changed: confirm."""
        if method not in LINK_METHODS:
            raise ValueError(f"unknown link method {method!r}")
        if not (url_info and url_info.get("ok") and url_info.get("url")):
            raise ValueError("set_link needs a normalized Maskura link (url_info ok)")

        def op(c, p):
            if p["archived"]:
                return self._conflict(p, "archived")
            new = self._link_fields(c, p, url_info, actor, method, confidence)
            self._apply_link_change(c, p, "set_link", actor, new, batch_id)
            return None
        return self._versioned(id, expected_link_version, op, version_field="link_version")

    def _op_confirm_link(self, c, p, actor, batch_id):
        if p["archived"]:
            return self._conflict(p, "archived")
        now = self.now()
        if p["link_needs_confirm"]:
            new = {"link_needs_confirm": 0, "link_confirmed_by": actor, "link_confirmed_at": now,
                   "maskura_status": derive_maskura_status(p["link_ok"], False, p["suggested_url"],
                                                           _loads(p["maskura_result_json"]))}
            self._apply_review_change(c, p, "confirm_link", actor, new, batch_id)
            return None
        if p["suggested_url"]:
            info = {"url": p["suggested_url"], "handle": p["suggested_handle"],
                    "variant_id": p["suggested_variant_id"]}
            new = self._link_fields(c, p, info, actor, "suggestion", p["suggestion_score"])
            self._apply_link_change(c, p, "confirm_link", actor, new, batch_id)
            return None
        return self._conflict(p, "nothing_to_confirm")

    def link_fix(self, decisions, *, approved_by, actor=AUTO_FIX_ACTOR):
        """Apply automatic link fixes as ONE undoable batch. Each row is left at "Link changed: confirm" until a
        person confirms it. `approved_by` records the person who checked the sample gate (PLAN P4); without it
        nothing runs. decisions: [{product_id, url_info, confidence, expected_link_version}]."""
        if not (approved_by or "").strip():
            raise ValueError("link_fix needs the person who checked the sample (approved_by)")
        batch_id = self.start_batch("auto_fix", actor=actor, action="link_fix")
        changed, conflicts = 0, []
        for d in decisions:
            ok, _p, _conflict = self.set_link(d["product_id"], d["url_info"], actor=actor, method="auto",
                                              confidence=d.get("confidence"),
                                              expected_link_version=d["expected_link_version"], batch_id=batch_id)
            if ok:
                changed += 1
            else:
                conflicts.append(d["product_id"])
        self.set_meta("link_fix_last_approval", approved_by.strip())
        return {"batch_id": batch_id, "changed": changed, "conflicts": conflicts, "total": len(decisions)}

    # ── catalog cache ──

    def replace_catalog(self, products, *, complete):
        """Swap in a fresh copy of the store's catalog (products.json products)."""
        now = self.now()

        def run(c):
            c.execute("DELETE FROM catalog_products")
            c.executemany("INSERT OR REPLACE INTO catalog_products(handle, title, product_json, fetched_at) "
                          "VALUES (?,?,?,?)",
                          [(str(p.get("handle") or "").lower(), p.get("title"), json.dumps(p), now)
                           for p in products if p.get("handle")])
            for key, value in (("catalog_fetched_at", now), ("catalog_complete", "1" if complete else "0")):
                c.execute("INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                          "SET value = excluded.value, updated_at = excluded.updated_at", (key, value, now))
        self._write(run)

    def catalog_products(self):
        return [json.loads(r[0]) for r in self._conn().execute("SELECT product_json FROM catalog_products")]

    def catalog_info(self):
        count = self._conn().execute("SELECT COUNT(*) FROM catalog_products").fetchone()[0]
        return {"count": count, "complete": self.get_meta("catalog_complete") == "1",
                "fetched_at": self.get_meta("catalog_fetched_at")}

    def confirm_link(self, id, *, actor, expected_version):
        return self._versioned(id, expected_version, lambda c, p: self._op_confirm_link(c, p, actor, None))

    def set_suggestion(self, id, url_info, *, score):
        """Record a Link changed: suggestion (P4 investigation). Not a review change; no version bump."""
        def run(c):
            p = self._row(c, id)
            if p is None:
                return False
            self._update(c, id, {
                "suggested_url": url_info["url"], "suggested_handle": url_info.get("handle"),
                "suggested_variant_id": url_info.get("variant_id"), "suggestion_score": score,
                "suggested_at": self.now(), "review_seq": self._bump_seq(c, "review_seq"),
                "maskura_status": derive_maskura_status(p["link_ok"], p["link_needs_confirm"], url_info["url"],
                                                        _loads(p["maskura_result_json"])),
            })
            return True
        return self._write(run)

    def bulk(self, action, snapshot_id, *, actor, value=None):
        """Apply one action to exactly the rows of a view snapshot, as one batch.

        Each row is checked against the review_version it had when the snapshot was made, so rows changed by
        someone else since are reported in `conflicts`. Rows the action does not apply to are in `skipped`.
        """
        if action not in BULK_ACTIONS:
            raise ValueError(f"unknown bulk action {action!r}")
        reason = note = None
        if action == "no_change_needed":
            reason = value if isinstance(value, str) else (value or {}).get("reason")
            self._check_review_args("no_change_needed", reason)
        if action == "archive":
            reason = value if isinstance(value, str) else (value or {}).get("reason")
            note = None if isinstance(value, str) else (value or {}).get("note")
            self._check_archive_args(reason, note)

        def run(c):
            snap = self._snapshot(c, snapshot_id)
            ids, versions = _loads(snap["ids_json"]), _loads(snap["versions_json"])
            cur = c.execute("INSERT INTO batches(kind, action, value_json, snapshot_id, actor, created_at, total) "
                            "VALUES ('bulk',?,?,?,?,?,?)",
                            (action, _dumps(value), snapshot_id, actor, self.now(), len(ids)))
            batch_id = cur.lastrowid
            ok, conflicts, skipped = [], [], []
            seq = self._bump_seq(c, "review_seq") if action in ("claim", "release_claim") else None
            for pid in ids:
                p = self._row(c, pid)
                if p is None:
                    skipped.append(pid)
                    continue
                if action == "claim":
                    res = self._op_claim(c, p, actor, batch_id, seq)
                elif action == "release_claim":
                    if p["claimed_key"] != name_key(actor):
                        skipped.append(pid)
                        continue
                    self._update(c, pid, {"claimed_by": None, "claimed_key": None, "claimed_at": None,
                                          "review_seq": seq})
                    self._event(c, pid, "release", actor, batch_id=batch_id,
                                changes={"claimed_by": [p["claimed_by"], None]})
                    res = None
                else:
                    if p["review_version"] != versions.get(str(pid)):
                        conflicts.append(pid)
                        continue
                    if action == "no_change_needed":
                        res = self._op_set_review(c, p, "no_change_needed", actor, reason, None, batch_id)
                    elif action == "mark_fixed":
                        res = self._op_set_review(c, p, "fixed_rechecking", actor, None, None, batch_id)
                    elif action == "confirm_link":
                        res = self._op_confirm_link(c, p, actor, batch_id)
                    elif action == "archive":
                        res = self._op_archive(c, p, reason, actor, note, batch_id)
                    else:
                        res = self._op_restore(c, p, actor, None, batch_id)
                if res is None:
                    ok.append(pid)
                elif res["reason"] == "claimed":
                    conflicts.append(pid)
                else:
                    skipped.append(pid)
            c.execute("UPDATE batches SET ok_count = ?, conflict_count = ?, conflicts_json = ? WHERE id = ?",
                      (len(ok), len(conflicts), _dumps(conflicts), batch_id))
            return {"batch_id": batch_id, "ok": len(ok), "conflicts": conflicts, "skipped": skipped,
                    "total": len(ids)}
        return self._write(run)

    def start_batch(self, kind, *, actor, action=None):
        """Open an empty batch (for example the auto-fix batch) to pass to set_link(batch_id=...)."""
        if kind not in ("bulk", "auto_fix"):
            raise ValueError(f"unknown batch kind {kind!r}")

        def run(c):
            return c.execute("INSERT INTO batches(kind, action, actor, created_at) VALUES (?,?,?,?)",
                             (kind, action or kind, actor, self.now())).lastrowid
        return self._write(run)

    _UNDO_FIELDS_LINK = ("maskura_url", "handle", "variant_id")

    def _undo_one(self, c, e, actor, batch_id=None):
        if e is None:
            return {"reason": "not_found"}
        if not e["undoable"]:
            return {"reason": "not_undoable"}
        if e["undone_by"] is not None:
            return {"reason": "already_undone"}
        p = self._row(c, e["product_id"])
        if e["review_version_after"] is not None and p["review_version"] != e["review_version_after"]:
            return self._conflict(p, "changed_since", event_id=e["id"])
        if e["link_version_after"] is not None and p["link_version"] != e["link_version_after"]:
            return self._conflict(p, "changed_since", event_id=e["id"])
        changes = _loads(e["changes_json"], {})
        fields = {k: old for k, (old, new) in changes.items()}
        reverse = {k: [new, old] for k, (old, new) in changes.items()}
        review_v = link_v = None
        if e["review_version_after"] is not None:
            review_v = p["review_version"] + 1
            fields["review_version"] = review_v
            fields["review_seq"] = self._bump_seq(c, "review_seq")
        if e["link_version_after"] is not None:
            link_v = p["link_version"] + 1
            fields["link_version"] = link_v
            fields["results_seq"] = self._bump_seq(c, "results_seq")
        self._update(c, p["id"], fields)
        new_id = self._event(c, p["id"], "undo", actor, changes=reverse, batch_id=batch_id, undo_of=e["id"],
                             review_version_after=review_v, link_version_after=link_v)
        c.execute("UPDATE product_events SET undone_by = ? WHERE id = ?", (new_id, e["id"]))
        return None

    def undo_event(self, event_id, *, actor):
        def run(c):
            e = c.execute("SELECT * FROM product_events WHERE id = ?", (event_id,)).fetchone()
            conflict = self._undo_one(c, e, actor)
            return {"ok": conflict is None, "conflict": conflict}
        return self._write(run)

    def undo_batch(self, batch_id, *, actor):
        """Undo every change in a batch that nobody changed since; the rest come back as conflicts."""
        def run(c):
            events = c.execute("SELECT * FROM product_events WHERE batch_id = ? AND undoable = 1 AND undone_by IS "
                               "NULL ORDER BY id DESC", (batch_id,)).fetchall()
            undo_batch_id = c.execute("INSERT INTO batches(kind, action, actor, created_at, total) VALUES "
                                      "('undo', ?, ?, ?, ?)", (f"undo:{batch_id}", actor, self.now(),
                                                              len(events))).lastrowid
            undone, conflicts = [], []
            for e in events:
                if self._undo_one(c, e, actor, undo_batch_id) is None:
                    undone.append(e["product_id"])
                else:
                    conflicts.append(e["product_id"])
            if not conflicts:
                c.execute("UPDATE batches SET undone_at = ?, undone_by = ? WHERE id = ?", (self.now(), actor,
                                                                                          batch_id))
            c.execute("UPDATE batches SET ok_count = ?, conflict_count = ?, conflicts_json = ? WHERE id = ?",
                      (len(undone), len(conflicts), _dumps(conflicts), undo_batch_id))
            return {"undone": undone, "conflicts": conflicts}
        return self._write(run)

    # ── views ──

    _FILTER_KEYS = ("archived", "needs_action", "problem", "maskura", "review", "claim", "platform", "q", "tag",
                    "batch", "outcome", "ids", "being_checked")

    def _filter_sql(self, flt, actor=None):
        f = dict(flt or {})
        unknown = set(f) - set(self._FILTER_KEYS)
        if unknown:
            raise ValueError(f"unknown filter keys: {sorted(unknown)}")
        clauses, params = [], []
        archived = f.get("archived", "exclude")
        if archived in (False, None, "exclude", ""):
            clauses.append("p.archived = 0")
        elif archived in (True, "only"):
            clauses.append("p.archived = 1")
        elif archived != "include":
            raise ValueError(f"archived filter {archived!r}")
        if f.get("needs_action"):
            clauses.append(_NEEDS_SQL)
        elif f.get("needs_action") is False:
            clauses.append(f"NOT COALESCE({_NEEDS_SQL}, 0)")
        if "being_checked" in f and f["being_checked"] is not None:
            clauses.append("p.being_checked = ?")
            params.append(1 if f["being_checked"] else 0)
        problem = f.get("problem")
        if problem == "any":
            clauses.append("p.has_problem = 1")
        elif problem == "none":
            clauses.append("p.has_problem = 0")
        elif problem:
            if problem == "M_ERR" or re.match(r"S\d+_", problem):
                clauses.append("EXISTS (SELECT 1 FROM json_each(p.flags_json) WHERE value = ?)")
                params.append(problem)
            else:
                clauses.append("EXISTS (SELECT 1 FROM json_each(p.flags_json) WHERE value GLOB ('S[0-9]*_' || ?))")
                params.append(problem)
        if f.get("maskura"):
            clauses.append("p.maskura_status = ?")
            params.append(f["maskura"])
        review = f.get("review")
        if review:
            review = [review] if isinstance(review, str) else list(review)
            clauses.append(f"p.review_status IN ({','.join('?' * len(review))})")
            params.extend(review)
        claim = f.get("claim")
        if claim:
            cutoff = self._claim_cutoff()
            if claim == "mine":
                clauses.append("p.claimed_key = ? AND p.claimed_at >= ?")
                params += [name_key(actor), cutoff]
            elif claim == "unclaimed":
                clauses.append("(p.claimed_key IS NULL OR p.claimed_at < ?)")
                params.append(cutoff)
            elif claim == "stale":
                clauses.append("p.claimed_key IS NOT NULL AND p.claimed_at < ?")
                params.append(cutoff)
            elif claim == "claimed":
                clauses.append("p.claimed_key IS NOT NULL AND p.claimed_at >= ?")
                params.append(cutoff)
            else:
                raise ValueError(f"claim filter {claim!r}")
        if f.get("platform"):
            clauses.append("EXISTS (SELECT 1 FROM source_results s WHERE s.product_id = p.id AND s.platform = ?)")
            params.append(f["platform"])
        q = (f.get("q") or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(p.name LIKE ? ESCAPE '\\' OR p.maskura_url LIKE ? ESCAPE '\\' "
                           "OR CAST(p.position AS TEXT) = ?)")
            params += [like, like, q]
        tag = f.get("tag")
        if tag == "no_longer_in_sheet":
            clauses.append("p.present_in_sheet = 0")
        elif tag == "possible_duplicate":
            clauses.append("p.duplicate_group IS NOT NULL")
        elif tag:
            raise ValueError(f"tag filter {tag!r}")
        if f.get("batch") is not None:
            if f.get("outcome", "ok") == "conflict":
                clauses.append("p.id IN (SELECT value FROM json_each((SELECT conflicts_json FROM batches "
                               "WHERE id = ?)))")
            else:
                clauses.append("EXISTS (SELECT 1 FROM product_events e WHERE e.product_id = p.id AND e.batch_id = ?)")
            params.append(int(f["batch"]))
        if f.get("ids") is not None:
            clauses.append("p.id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps([int(i) for i in f["ids"]]))
        return (" AND ".join(clauses) or "1"), params

    def create_snapshot(self, filter, *, actor, sort="row"):
        """Freeze the ordered ids (and their review versions) of a filtered view for 8 hours."""
        if sort not in SORTS:
            raise ValueError(f"unknown sort {sort!r}")

        def run(c):
            where, params = self._filter_sql(filter, actor)
            rows = c.execute(f"SELECT p.id, p.review_version FROM products p WHERE {where} "
                             f"ORDER BY {SORTS[sort]}", params).fetchall()
            now = self.now_dt()
            c.execute("DELETE FROM view_snapshots WHERE expires_at < ?", (utc_iso(now),))
            sid = secrets.token_urlsafe(12)
            c.execute("INSERT INTO view_snapshots(id, filter_json, ids_json, versions_json, count, created_by, "
                      "created_at, expires_at, sort) VALUES (?,?,?,?,?,?,?,?,?)",
                      (sid, _dumps(filter or {}), json.dumps([r["id"] for r in rows]),
                       json.dumps({str(r["id"]): r["review_version"] for r in rows}), len(rows), actor,
                       utc_iso(now), utc_iso(now + SNAPSHOT_TTL), sort))
            return sid
        return self._write(run)

    def _snapshot(self, c, snapshot_id):
        snap = c.execute("SELECT * FROM view_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if snap is None or snap["expires_at"] < self.now():
            raise SnapshotExpired(snapshot_id)
        return snap

    def snapshot_info(self, snapshot_id):
        def run(c):
            s = self._snapshot(c, snapshot_id)
            return {"id": s["id"], "filter": _loads(s["filter_json"]), "count": s["count"], "sort": s["sort"],
                    "created_by": s["created_by"], "created_at": s["created_at"], "expires_at": s["expires_at"]}
        return self._read(run)

    def page(self, snapshot_id, after=None, limit=50):
        """Keyset page over a snapshot: `after` is the `next` value of the previous page."""
        limit = max(1, min(int(limit), 500))
        start = -1 if after is None else int(after)

        def run(c):
            self._snapshot(c, snapshot_id)
            rows = c.execute(
                "SELECT j.key AS ord, p.* FROM json_each((SELECT ids_json FROM view_snapshots WHERE id = ?)) j "
                "JOIN products p ON p.id = j.value WHERE j.key > ? ORDER BY j.key LIMIT ?",
                (snapshot_id, start, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            sources = self._sources(c, [r["id"] for r in rows])
            items = []
            for r in rows:
                d = dict(r)
                d.pop("ord")
                items.append(self._product_dict(d, sources[r["id"]]))
            return {"items": items, "next": rows[-1]["ord"] if more and rows else None}
        return self._read(run)

    def summary(self, filter=None, *, actor=None):
        """Tile numbers for a filter (archived rows are always counted separately). `actor` is the viewer, for
        the claim filter "mine"."""
        flt = dict(filter or {})
        if flt.get("archived") not in (True, "only"):
            flt["archived"] = "include"

        def run(c):
            where, params = self._filter_sql(flt, actor)
            cutoff = self._claim_cutoff()
            r = c.execute(
                "SELECT COUNT(*) AS all_rows, "
                "COALESCE(SUM(p.archived = 0), 0) AS products, "
                "COALESCE(SUM(p.archived = 1), 0) AS archived, "
                f"COALESCE(SUM({_NEEDS_SQL}), 0) AS needs_action, "
                "COALESCE(SUM(p.archived = 0 AND p.being_checked = 1), 0) AS being_checked, "
                "COALESCE(SUM(p.archived = 0 AND p.review_status = 'fixed_rechecking'), 0) AS fixed_rechecking, "
                f"COALESCE(SUM({_IN_TOTAL_SQL}), 0) AS total, "
                f"COALESCE(SUM({_IN_TOTAL_SQL} AND {_RESOLVED_SQL}), 0) AS resolved, "
                "COALESCE(SUM(p.archived = 0 AND p.has_problem = 1), 0) AS with_problem, "
                "COALESCE(SUM(p.archived = 0 AND p.claimed_key IS NOT NULL AND p.claimed_at >= ?), 0) AS claimed, "
                "COALESCE(SUM(p.archived = 0 AND p.present_in_sheet = 0), 0) AS no_longer_in_sheet, "
                "COALESCE(SUM(p.archived = 0 AND p.duplicate_group IS NOT NULL), 0) AS possible_duplicate "
                f"FROM products p WHERE {where}", (cutoff, *params)).fetchone()
            by_review = {s: 0 for s in REVIEW_STATUSES}
            for row in c.execute(f"SELECT p.review_status, COUNT(*) FROM products p WHERE {where} AND "
                                 "p.archived = 0 GROUP BY 1", params):
                by_review[row[0]] = row[1]
            by_maskura = {row[0] or "none": row[1] for row in c.execute(
                f"SELECT p.maskura_status, COUNT(*) FROM products p WHERE {where} AND p.archived = 0 GROUP BY 1",
                params)}
            return {
                "products": r["products"], "archived": r["archived"], "needs_action": r["needs_action"],
                "being_checked": r["being_checked"], "fixed_rechecking": r["fixed_rechecking"],
                "reviewed": {"n": r["resolved"], "total": r["total"]}, "with_problem": r["with_problem"],
                "claimed": r["claimed"], "no_longer_in_sheet": r["no_longer_in_sheet"],
                "possible_duplicate": r["possible_duplicate"], "review": by_review, "maskura": by_maskura,
                "results_version": self._seq(c, "results_seq"), "review_version": self._seq(c, "review_seq"),
            }
        return self._read(run)

    def changes(self, snapshot_id, since_results_version, since_review_version):
        """Ids in the view whose results or review changed after the given global counters."""
        def run(c):
            self._snapshot(c, snapshot_id)
            rows = c.execute(
                "SELECT p.id FROM json_each((SELECT ids_json FROM view_snapshots WHERE id = ?)) j "
                "JOIN products p ON p.id = j.value WHERE p.results_seq > ? OR p.review_seq > ? ORDER BY j.key",
                (snapshot_id, int(since_results_version or 0), int(since_review_version or 0))).fetchall()
            return {"ids": [r["id"] for r in rows], "results_version": self._seq(c, "results_seq"),
                    "review_version": self._seq(c, "review_seq")}
        return self._read(run)

    # ── jobs (P5) ──

    def enqueue_job(self, kind, *, actor, product_ids=None, maskura_only=False, priority=0, params=None):
        """Queue a job of (product, slot) items: the Maskura page (slot 0) when the row has a usable link, and
        each source slot that holds a link (unless maskura_only). Archived rows are skipped and counted."""
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        # "changed": copy recent results first, then queue only the slots still missing a fresh one
        reused = self.reuse_recent_results() if kind == "changed" else None

        def run(c):
            now = self.now()
            cutoff = self._fresh_cutoff(REUSE_MAX_AGE_S) if kind == "changed" else None
            if product_ids is None:
                rows = c.execute("SELECT * FROM products WHERE archived = 0 ORDER BY position, id").fetchall()
                skipped = c.execute("SELECT COUNT(*) FROM products WHERE archived = 1").fetchone()[0]
            else:
                ids = json.dumps([int(i) for i in product_ids])
                rows = c.execute("SELECT * FROM products WHERE archived = 0 AND id IN (SELECT value FROM json_each(?)) "
                                 "ORDER BY position, id", (ids,)).fetchall()
                skipped = c.execute("SELECT COUNT(*) FROM products WHERE archived = 1 AND id IN "
                                    "(SELECT value FROM json_each(?))", (ids,)).fetchone()[0]
            info = dict(params or {}, skipped_archived=skipped, maskura_only=bool(maskura_only),
                        **({"reused": reused} if reused is not None else {}))
            job_id = c.execute("INSERT INTO jobs(kind, status, priority, params_json, created_by, created_at) "
                               "VALUES (?, 'queued', ?, ?, ?, ?)",
                               (kind, int(priority), _dumps(info), actor, now)).lastrowid
            items, products = [], set()
            for p in rows:
                want = set(self._needs_check(c, p["id"], cutoff)) if cutoff else None
                if p["link_ok"] and p["maskura_url"] and (want is None or MASKURA_SLOT in want):
                    items.append((job_id, p["id"], MASKURA_SLOT, self.urls.host_of(p["maskura_url"]), int(priority),
                                  p["results_seq"], p["review_version"], p["link_version"], None, now, now))
                    products.add(p["id"])
                if not maskura_only:
                    for s in c.execute("SELECT slot, url, host, url_version FROM source_results WHERE product_id = ? "
                                       "AND url <> '' ORDER BY slot", (p["id"],)):
                        if want is not None and s["slot"] not in want:
                            continue
                        items.append((job_id, p["id"], s["slot"], s["host"] or self.urls.host_of(s["url"]),
                                      int(priority), p["results_seq"], p["review_version"], p["link_version"],
                                      s["url_version"], now, now))
                        products.add(p["id"])
            c.executemany("INSERT INTO job_items(job_id, product_id, slot, host, priority, results_version_at_enqueue, "
                          "review_version_at_enqueue, link_version_at_enqueue, url_version_at_enqueue, created_at, "
                          "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", items)
            c.execute("UPDATE jobs SET total = ? WHERE id = ?", (len(products), job_id))
            if kind == "changed" and not items:   # nothing left to check; other kinds may run with no items
                c.execute("UPDATE jobs SET status = 'done', started_at = ?, finished_at = ? WHERE id = ?",
                          (now, now, job_id))
            return job_id
        return self._write(run)

    def job(self, job_id):
        row = self._conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["params"] = _loads(out.pop("params_json"), {})
        return out

    def activate_jobs(self):
        """Start every queued job. A full job resets the "had a problem" marker first, because TOTAL in
        "Reviewed N of TOTAL" counts the current results. Returns the running jobs."""
        def run(c):
            now = self.now()
            for j in c.execute("SELECT id, kind FROM jobs WHERE status = 'queued' ORDER BY priority DESC, id").fetchall():
                if j["kind"] == "full":
                    if c.execute("UPDATE products SET had_problem = has_problem WHERE had_problem <> has_problem").rowcount:
                        self._bump_seq(c, "results_seq")
                c.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (now, j["id"]))
            return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY priority DESC, id")]
        return self._write(run)

    def pending_hosts(self):
        """Hosts that have pending items in running jobs (the worker asks its pacer which are ready)."""
        return [r[0] for r in self._conn().execute(
            "SELECT DISTINCT i.host FROM job_items i JOIN jobs j ON j.id = i.job_id "
            "WHERE j.status = 'running' AND i.status = 'pending'")]

    def lease_item(self, worker_id, hosts):
        """Lease the most urgent pending item on one of `hosts`, with a fresh token. None when there is none."""
        hosts = [h for h in hosts if h]
        if not hosts:
            return None

        def run(c):
            row = c.execute(
                "SELECT i.* FROM job_items i JOIN jobs j ON j.id = i.job_id WHERE j.status = 'running' "
                "AND i.status = 'pending' AND i.host IN (SELECT value FROM json_each(?)) "
                "ORDER BY j.priority DESC, i.priority DESC, i.id LIMIT 1", (json.dumps(hosts),)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(16)
            c.execute("UPDATE job_items SET status = 'leased', lease_token = ?, leased_at = ?, leased_by = ?, "
                      "updated_at = ? WHERE id = ?", (token, self.now(), worker_id, self.now(), row["id"]))
            p = self._row(c, row["product_id"])
            if row["slot"] == MASKURA_SLOT:
                url = p["maskura_url"]
            else:
                url = c.execute("SELECT url FROM source_results WHERE product_id = ? AND slot = ?",
                                (row["product_id"], row["slot"])).fetchone()[0]
            return dict(row, status="leased", lease_token=token, url=url,
                        last_try=row["attempts"] + 1 >= MAX_ATTEMPTS)
        return self._write(run)

    def commit_item(self, item_id, token, raw):
        """Land one result exactly once (the lease token must still hold), then finalize the product when its last
        slot of this job is in, all in one transaction. A result for a link or listing that changed since enqueue
        is dropped (stale)."""
        def run(c):
            item = c.execute("SELECT * FROM job_items WHERE id = ? AND status = 'leased' AND lease_token = ?",
                             (item_id, token)).fetchone()
            if item is None:
                return {"committed": False, "stale": False, "finalized": False}
            pid, slot = item["product_id"], item["slot"]
            p = self._row(c, pid)
            stale = p is None or bool(p["archived"])
            if not stale and slot == MASKURA_SLOT:
                stale = p["link_version"] != item["link_version_at_enqueue"]
                if not stale:
                    self.record_maskura(pid, raw, link_version=p["link_version"])
            elif not stale:
                row = c.execute("SELECT url_version FROM source_results WHERE product_id = ? AND slot = ?",
                                (pid, slot)).fetchone()
                stale = row is None or row["url_version"] != item["url_version_at_enqueue"]
                if not stale:
                    try:
                        self.record_source(pid, slot, raw)
                    except StaleWrite:
                        stale = True
            c.execute("UPDATE job_items SET status = ?, attempts = attempts + 1, lease_token = NULL, last_error = ?, "
                      "updated_at = ? WHERE id = ?", ("stale" if stale else "done",
                                                      (raw or {}).get("error"), self.now(), item_id))
            finalized = self._finalize_if_complete(c, item["job_id"], pid)
            self._finish_if_complete(c, item["job_id"])
            return {"committed": True, "stale": stale, "finalized": finalized}
        return self._write(run)

    def release_item(self, item_id, token, *, count_attempt, error=None):
        """Give a leased item back. A counted attempt waits for the retry pass; an uncounted one (the network was
        down) goes straight back to pending. The worker commits the result itself on the last attempt."""
        def run(c):
            item = c.execute("SELECT * FROM job_items WHERE id = ? AND status = 'leased' AND lease_token = ?",
                             (item_id, token)).fetchone()
            if item is None:
                return {"released": False, "attempts": None, "final": False}
            attempts = item["attempts"] + (1 if count_attempt else 0)
            c.execute("UPDATE job_items SET status = ?, attempts = ?, lease_token = NULL, last_error = ?, updated_at = ? "
                      "WHERE id = ?", ("retry" if count_attempt else "pending", attempts, error, self.now(), item_id))
            return {"released": True, "attempts": attempts, "final": attempts >= MAX_ATTEMPTS}
        return self._write(run)

    def retry_pass(self):
        """When a running job has nothing pending or leased, its retry items go back to pending. Returns how many."""
        def run(c):
            moved = 0
            for j in c.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall():
                busy = c.execute("SELECT 1 FROM job_items WHERE job_id = ? AND status IN ('pending', 'leased') LIMIT 1",
                                 (j["id"],)).fetchone()
                if busy is None:
                    moved += c.execute("UPDATE job_items SET status = 'pending', updated_at = ? WHERE job_id = ? AND "
                                       "status = 'retry'", (self.now(), j["id"])).rowcount
            return moved
        return self._write(run)

    def _finalize_if_complete(self, c, job_id, pid):
        open_items = c.execute("SELECT 1 FROM job_items WHERE job_id = ? AND product_id = ? AND status IN "
                               "('pending', 'leased', 'retry') LIMIT 1", (job_id, pid)).fetchone()
        if open_items is not None:
            return False
        p = self._row(c, pid)
        if p is None:
            return False
        sources = []
        for s in c.execute("SELECT url, raw_json FROM source_results WHERE product_id = ? ORDER BY slot", (pid,)):
            sources.append(dict(NO_URL_SOURCE) if not s["url"] else _loads(s["raw_json"]))
        self.finalize(pid, self.finalizer(_loads(p["maskura_result_json"]), sources))
        c.execute("UPDATE jobs SET done = done + 1 WHERE id = ?", (job_id,))
        return True

    def _finish_if_complete(self, c, job_id):
        left = c.execute("SELECT 1 FROM job_items WHERE job_id = ? AND status IN ('pending', 'leased', 'retry') LIMIT 1",
                         (job_id,)).fetchone()
        if left is None:
            c.execute("UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ? AND status = 'running'",
                      (self.now(), job_id))

    def items_done(self, job_id):
        """Items of a job that have a committed result (done or stale): the pace the go/no-go judges."""
        return self._conn().execute("SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status IN ('done', 'stale')",
                                    (job_id,)).fetchone()[0]

    def check_counts(self):
        """What a check would cover: {products, archived, changed} (the start buttons' words use them). changed counts
        products with a link that has no result from the last REUSE_MAX_AGE_S, before any reuse."""
        c = self._conn()
        r = c.execute("SELECT COALESCE(SUM(archived = 0), 0), COALESCE(SUM(archived = 1), 0) FROM products").fetchone()
        cutoff = self._fresh_cutoff(REUSE_MAX_AGE_S)
        changed = sum(1 for (pid,) in c.execute("SELECT id FROM products WHERE archived = 0").fetchall()
                      if self._needs_check(c, pid, cutoff))
        return {"products": r[0], "archived": r[1], "changed": changed}

    def finish_job(self, job_id, *, error=None):
        """End a job that has no items (a catalog refresh): done, or failed with a plain error."""
        def run(c):
            c.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ? AND status = 'running'",
                      ("failed" if error else "done", self.now(), error, job_id))
        self._write(run)

    def reset_leases(self):
        """At worker startup (single instance): leases left by a dead worker go back to pending, and their tokens
        become void, so a late commit from the old process is refused."""
        def run(c):
            return c.execute("UPDATE job_items SET status = 'pending', lease_token = NULL, updated_at = ? "
                             "WHERE status = 'leased'", (self.now(),)).rowcount
        return self._write(run)

    def pause_job(self, job_id, *, actor, reason=None):
        def run(c):
            if not c.execute("UPDATE jobs SET status = 'paused', paused_by = ?, paused_at = ?, pause_reason = ? "
                             "WHERE id = ? AND status IN ('running', 'queued')",
                             (actor, self.now(), reason, job_id)).rowcount:
                raise ValueError("only a running or queued job can be paused")
        self._write(run)

    def resume_job(self, job_id, *, actor):
        def run(c):
            j = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if j is None or j["status"] != "paused":
                raise ValueError("only a paused job can be resumed")
            params = _loads(j["params_json"], {})
            paused_s = (self.now_dt() - parse_iso(j["paused_at"])).total_seconds() if j["paused_at"] else 0
            params["paused_s"] = round(params.get("paused_s", 0) + max(0, paused_s), 1)
            params["resumed_by"] = actor
            c.execute("UPDATE jobs SET status = ?, paused_by = NULL, paused_at = NULL, pause_reason = NULL, "
                      "params_json = ? WHERE id = ?", ("running" if j["started_at"] else "queued", _dumps(params), job_id))
        self._write(run)

    def stop_job(self, job_id, *, actor):
        """Stop for good: items not started are cancelled. Rows left half-checked stay Being checked."""
        def run(c):
            if not c.execute("UPDATE jobs SET status = 'stopped', finished_at = ?, error = ? WHERE id = ? AND "
                             "status IN ('running', 'queued', 'paused')",
                             (self.now(), f"Stopped by {actor}", job_id)).rowcount:
                raise ValueError("only an active job can be stopped")
            c.execute("UPDATE job_items SET status = 'cancelled', updated_at = ? WHERE job_id = ? AND status IN "
                      "('pending', 'retry')", (self.now(), job_id))
        self._write(run)

    def heartbeat(self, worker_id, *, pid=None, hostname=None, state=None):
        def run(c):
            now = self.now()
            c.execute("INSERT INTO worker_heartbeat(worker_id, pid, hostname, schema_version, started_at, beat_at, "
                      "state_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET pid = excluded.pid, "
                      "hostname = excluded.hostname, beat_at = excluded.beat_at, state_json = excluded.state_json",
                      (worker_id, pid, hostname, SCHEMA_VERSION, now, now, _dumps(state or {})))
        self._write(run)

    def worker_status(self):
        row = self._conn().execute("SELECT * FROM worker_heartbeat ORDER BY beat_at DESC LIMIT 1").fetchone()
        if row is None:
            return {"alive": False, "beat_at": None}
        age = (self.now_dt() - parse_iso(row["beat_at"])).total_seconds()
        return {"alive": age <= HEARTBEAT_STALE_S, "beat_at": row["beat_at"], "worker_id": row["worker_id"],
                "state": _loads(row["state_json"], {})}

    def save_host_health(self, hosts):
        """hosts: {host: {gap_s, cooldown_until (UTC ISO or None), last_outcome, recent_blocks}}."""
        def run(c):
            now = self.now()
            for host, h in hosts.items():
                c.execute("INSERT INTO host_health(host, gap_s, cooldown_until, blocked_recent, last_outcome, last_at, "
                          "snapshot_json, updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                          "gap_s = excluded.gap_s, cooldown_until = excluded.cooldown_until, blocked_recent = "
                          "excluded.blocked_recent, last_outcome = excluded.last_outcome, last_at = excluded.last_at, "
                          "snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at",
                          (host, h.get("gap_s"), h.get("cooldown_until"), int(h.get("recent_blocks") or 0),
                           h.get("last_outcome"), now, _dumps(h), now))
        self._write(run)

    def host_health(self):
        return {r["host"]: dict(r) for r in self._conn().execute("SELECT * FROM host_health")}

    def progress(self):
        """The progress panel's numbers (PLAN P5): the most recent active job, products done of total, items per
        host, and an ETA range: the slowest host's remaining items over its rate in the last 2 h of running time."""
        def run(c):
            j = c.execute("SELECT * FROM jobs WHERE status IN ('running', 'paused', 'queued') "
                          "ORDER BY priority DESC, id DESC LIMIT 1").fetchone()
            if j is None:
                return {"job": None}
            labels = importlib.import_module("sc_labels")
            now = self.now_dt()
            params = _loads(j["params_json"], {})
            started = parse_iso(j["started_at"]) if j["started_at"] else None
            running_s = 0.0
            if started:
                end = parse_iso(j["paused_at"]) if j["paused_at"] else now
                running_s = max(0.0, (end - started).total_seconds() - params.get("paused_s", 0))
            window = min(ETA_WINDOW_S, running_s) if running_s else 0
            since = utc_iso(now - datetime.timedelta(seconds=window)) if window else None
            health = {r["host"]: r for r in c.execute("SELECT * FROM host_health")}
            hosts, etas, unknown = [], [], False
            for r in c.execute("SELECT host, COUNT(*) AS total, SUM(status IN ('done', 'stale')) AS done, "
                               "SUM(status IN ('done', 'stale') AND updated_at >= ?) AS recent FROM job_items "
                               "WHERE job_id = ? AND status <> 'cancelled' GROUP BY host ORDER BY host",
                               (since or "9999", j["id"])):
                remaining = r["total"] - (r["done"] or 0)
                h = health.get(r["host"])
                entry = {"host": r["host"], "done": r["done"] or 0, "total": r["total"],
                         "gap_s": h["gap_s"] if h else None, "cooldown_until": h["cooldown_until"] if h else None,
                         "last_outcome": h["last_outcome"] if h else None}
                if h and h["cooldown_until"] and h["cooldown_until"] > utc_iso(now):
                    key = "challenge" if h["last_outcome"] == "challenge" else "cooling_down"
                    entry["note_word"] = labels.HOST_STATE[key].replace("{name}", _host_name(r["host"]))
                hosts.append(entry)
                if remaining > 0:
                    rate = (r["recent"] or 0) / window if window else 0
                    if rate > 0:
                        etas.append(remaining / rate)
                    else:
                        unknown = True
            eta = max(etas) if etas and not unknown else None
            state = j["status"]
            word = labels.RUN_STATE["paused" if state == "paused" else ("queued" if state == "queued" else "running")]
            return {"job": {
                "id": j["id"], "kind": j["kind"], "kind_word": labels.JOB_KINDS.get(j["kind"], j["kind"]),
                "state": state, "state_word": word.replace("{name}", j["paused_by"] or ""),
                "done": j["done"], "total": j["total"], "skipped_archived": params.get("skipped_archived", 0),
                "eta_low_s": round(eta * 0.8) if eta is not None else None,
                "eta_high_s": round(eta * 1.3) if eta is not None else None,
                "hosts": hosts, "created_at": j["created_at"], "started_at": j["started_at"],
                "paused_by": j["paused_by"],
                "running_s": round(running_s),
                "paused_at": j["paused_at"], "pause_reason": j["pause_reason"]}}
        return self._read(run)

    # ── backups ──

    def backup(self, dest_path):
        """Online backup through the sqlite3 backup API, then integrity_check on the copy."""
        dest_path = str(dest_path)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        tmp = dest_path + ".part"
        if os.path.exists(tmp):
            os.remove(tmp)
        dst = sqlite3.connect(tmp)
        try:
            self._conn().backup(dst)
            result = [r[0] for r in dst.execute("PRAGMA integrity_check").fetchall()]
        finally:
            dst.close()
        os.replace(tmp, dest_path)
        ok = result == ["ok"]
        at = self.now()
        if ok:
            self.set_meta("last_backup_at", at)
            self.set_meta("last_backup_path", dest_path)
        return {"path": dest_path, "bytes": os.path.getsize(dest_path), "ok": ok,
                "integrity": "ok" if ok else "; ".join(result)[:500], "at": at}

    prune_backups = staticmethod(lambda dir, keep=BACKUP_KEEP: prune_backups(dir, keep))


def prune_backups(dir, keep=BACKUP_KEEP):
    """Keep the newest `keep` source_check-<UTC>.db files in `dir`; return the paths removed."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    files = sorted(glob.glob(os.path.join(str(dir), BACKUP_GLOB)))
    removed = files[:-keep] if len(files) > keep else []
    for path in removed:
        os.remove(path)
    return removed


def _host_name(host):
    """A host as people say it: www.maskura.co.uk is "Maskura", www.ebay.co.uk is "eBay"."""
    h = str(host or "").lower()
    for key, name in (("maskura", "Maskura"), ("ebay.", "eBay"), ("amazon.", "Amazon")):
        if key in h:
            return name
    return h.removeprefix("www.")
