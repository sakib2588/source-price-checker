"""Import the legacy source_check_results.json into source_check.db.

    .venv/bin/python scripts/migrate_json_to_db.py --json source_check_results.json --db source_check.db \
        [--dry-run] [--expect-count N]

Rules (INTERFACES.md, PLAN P2):
- the JSON's sha256 and mtime are recorded in `meta`;
- the first import runs in one transaction and writes the `first_import_done` marker; the branch is chosen by
  that marker, never by an empty database;
- `crawled_at` is the JSON file's mtime (no legacy row has its own);
- the derived source fields (price_diff, pct_vs_source, notes and the rest) are dropped, and
  sc_crawl.finalize_product recomputes them from the raw fields;
- a later run re-imports only when the sha256 differs, and then updates results only: it never touches the
  review, link, archive or alias fields, and it never creates products;
- --dry-run runs the whole procedure on a scratch copy of the database and prints the report; the real
  database is never opened for writing;
- --expect-count N exits 2, before anything is written, when the JSON does not hold exactly N rows.

Exit codes: 0 done (or nothing to do), 1 bad input, 2 count mismatch.
"""
import argparse
import datetime
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PORTAL_DIR not in sys.path:
    sys.path.insert(0, PORTAL_DIR)

from checker import sc_store  # noqa: E402

ACTOR = "system (migration)"
MASKURA_NOT_CRAWLED = "not crawled"


def file_facts(path):
    with open(path, "rb") as fh:
        data = fh.read()
    mtime = os.stat(path).st_mtime
    return data, hashlib.sha256(data).hexdigest(), sc_store.utc_iso(
        datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc))


def sheet_rows(records):
    return [{"position": int(r["row_id"]), "name": r.get("product_name") or "",
             "maskura_raw": r.get("maskura_url") or "",
             "source_urls": [(s or {}).get("url") or "" for s in (r.get("sources") or [])]}
            for r in records]


def maskura_raw_from_json(rec, product, crawled_at):
    """The fetch_maskura shape, rebuilt from a legacy row. None when the legacy run never fetched it."""
    error = (rec.get("maskura_error") or "").strip() or None
    if error and MASKURA_NOT_CRAWLED in error.lower():
        return None
    if error and ("404" in error or "not found" in error.lower()):
        status, http_status = "NOT_FOUND", 404
    elif error:
        status, http_status = "ERROR", None
    elif rec.get("maskura_price_gbp") is None:
        return None
    else:
        status, http_status = "OK", None
    return {"url": product["maskura_url"], "handle": product["handle"], "variant_id": product["variant_id"],
            "title": rec.get("maskura_title") or None, "price_gbp": rec.get("maskura_price_gbp"),
            "available": None, "variants": None, "status": status, "http_status": http_status, "error": error,
            "fetched_at": crawled_at, "note": rec.get("maskura_note") or None}


def source_raw_from_json(src, urls, crawled_at):
    """Raw fields only. A NOT_CHECKED entry was never fetched, so it gives None (the slot stays pending)."""
    if not src or not src.get("url") or src.get("verdict") in (None, "NOT_CHECKED"):
        return None
    raw = {k: src[k] for k in sc_store.RAW_SOURCE_FIELDS if k in src}
    raw["fetched_at"] = crawled_at
    raw.setdefault("host", urls.host_of(src["url"]))
    return raw


def finalize_input(product):
    """finalize_product(maskura, sources) arguments from the stored raw state of one product."""
    maskura = product["maskura"] if product["link_ok"] else None
    return maskura, [s["raw"] if s.get("url") else dict(sc_store.NO_URL_SOURCE) for s in product["sources"]]


def _apply_results(store, pid, rec, finalize_fn, crawled_at, counts, *, reimport):
    product = store.get_product(pid)
    if reimport and product["archived"]:
        counts["skipped_archived"] += 1
        return
    if product["link_ok"]:
        norm = store.urls.normalize_maskura_url(rec.get("maskura_url") or "")
        if norm.get("url") != product["maskura_url"]:
            counts["skipped_link_edited"] += 1
        else:
            raw = maskura_raw_from_json(rec, product, crawled_at)
            if raw is not None and store.record_maskura(pid, raw, link_version=product["link_version"]):
                counts["maskura_recorded"] += 1
    for slot, src in enumerate(rec.get("sources") or [], 1):
        raw = source_raw_from_json(src, store.urls, crawled_at)
        if raw is None:
            counts["sources_not_checked"] += 1
            continue
        try:
            store.record_source(pid, slot, raw)
            counts["sources_recorded"] += 1
        except sc_store.StaleWrite:
            counts["skipped_slot_changed"] += 1
    maskura, sources = finalize_input(store.get_product(pid))
    result = store.finalize(pid, finalize_fn(maskura, sources), crawled_at=crawled_at)
    counts["finalized"] += 1
    counts["results_changed"] += 1 if result["changed"] else 0


def _new_counts():
    return {k: 0 for k in ("maskura_recorded", "sources_recorded", "sources_not_checked", "skipped_link_edited",
                           "skipped_slot_changed", "skipped_archived", "unmatched", "finalized",
                           "results_changed")}


def run(store, records, sha, mtime_iso, json_path, finalize_fn):
    """Apply the JSON to an open, migrated store. Returns the report."""
    rows = sheet_rows(records)
    marker = store.get_meta("first_import_done")
    report = {"json_rows": len(rows), "sha256": sha, "mtime": mtime_iso}
    if marker and store.get_meta("json_sha256") == sha:
        report["mode"] = "unchanged"
        return report
    counts = _new_counts()
    by_position = {int(r["row_id"]): r for r in records}
    with store.transaction():
        if not marker:
            report["mode"] = "first_import"
            report["import"] = store.import_sheet(
                rows, sheet_meta={"name": os.path.basename(json_path), "sha256": sha, "source": "legacy_json"},
                actor=ACTOR, dry_run=False)
            sheet_id = report["import"]["sheet_id"]
            for position, pid in store.sheet_assignments(sheet_id):
                _apply_results(store, pid, by_position[position], finalize_fn, mtime_iso, counts, reimport=False)
            store.set_meta("migration_sheet_id", sheet_id)
            store.set_meta("first_import_done", store.now())
        else:
            report["mode"] = "reimport"
            for position, pid in store.match_rows(rows):
                if pid is None:
                    counts["unmatched"] += 1
                    continue
                _apply_results(store, pid, by_position[position], finalize_fn, mtime_iso, counts, reimport=True)
            store.set_meta("last_reimport_at", store.now())
        store.set_meta("json_sha256", sha)
        store.set_meta("json_mtime", mtime_iso)
        store.set_meta("json_path", os.path.abspath(json_path))
        store.set_meta("json_row_count", len(rows))
    report["results"] = counts
    return report


def _copy_db(src_path, dest_path):
    """Copy a live database through the backup API without writing to it."""
    src = sqlite3.connect(f"file:{os.path.abspath(src_path)}?mode=ro", uri=True)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def main(argv=None, *, finalize_fn=None, urls=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--expect-count", type=int)
    args = ap.parse_args(argv)

    try:
        data, sha, mtime_iso = file_facts(args.json)
        records = json.loads(data.decode("utf-8"))
        if not isinstance(records, list):
            raise ValueError("the JSON must be a list of rows")
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"could not read {args.json}: {exc}"}), file=out)
        return 1
    if args.expect_count is not None and len(records) != args.expect_count:
        print(json.dumps({"error": "count mismatch", "json_rows": len(records),
                          "expected": args.expect_count}), file=out)
        return 2
    if finalize_fn is None:
        finalize_fn = importlib.import_module("checker.sc_crawl").finalize_product

    scratch = None
    try:
        if args.dry_run:
            scratch = tempfile.mkdtemp(prefix="sc_migrate_dry_")
            db_path = os.path.join(scratch, "rehearsal.db")
            if os.path.exists(args.db):
                _copy_db(args.db, db_path)
        else:
            db_path = args.db
        store = sc_store.Store(db_path, urls=urls)
        store.migrate()
        report = run(store, records, sha, mtime_iso, args.json, finalize_fn)
        report["dry_run"] = bool(args.dry_run)
        if args.dry_run:
            summary = store.summary()
            report["after"] = {"products": summary["products"], "needs_action": summary["needs_action"],
                               "being_checked": summary["being_checked"], "with_problem": summary["with_problem"],
                               "maskura": summary["maskura"]}
        store.close()
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
