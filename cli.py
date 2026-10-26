"""Check a sourcing sheet from the command line and write the review workbook.

    python cli.py check "All Products.csv"                 # every product, results in checks.db
    python cli.py check sheet.xlsx --limit 5               # the first 5 products, for a quick try
    python cli.py review --out review.xlsx                 # rebuild the workbook from stored results

The sheet needs Product Name, Maskura Link and Source 1 (Source 2 to Source 10 optional) columns; see sheet.py.
Results are kept in the SQLite file given by --db, so a second check of the same sheet reuses its products.
"""
import argparse
import hashlib
import logging
import os
import sys
import time

from checker import sc_review_book
from checker import sc_store
from checker import sc_worker
from checker import sheet

ACTOR = "cli"
FINISHED = ("done", "failed", "stopped", "paused")


class CliWorker(sc_worker.Worker):
    """The portal's worker without the go/no-go pause: the person at the terminal can stop a slow run (Ctrl+C)."""

    def judge(self, job, running_s):
        return None


def check(args):
    try:
        got = sheet.read_sheet_file(args.sheet)
    except (OSError, ValueError) as exc:
        sys.exit(f"Could not read {args.sheet}: {exc}")
    for w in got["warnings"]:
        print(f"Warning: {w}")
    rows = got["rows"][:args.limit] if args.limit else got["rows"]
    with open(args.sheet, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    store = sc_store.Store(args.db)
    store.migrate()
    store.import_sheet(rows, sheet_meta={"name": os.path.basename(args.sheet), "sha256": sha,
                                         "source_columns": len(got["columns"]["sources"]),
                                         "warnings": got["warnings"]}, actor=ACTOR)
    job_id = store.enqueue_job("full", actor=ACTOR)
    total = store.job(job_id)["total"]
    print(f"Checking {total:,} products from {os.path.basename(args.sheet)} (Ctrl+C stops; results so far are kept).")
    if not total:
        # A job with no links never commits an item, so the store would leave it running: end it here.
        store.stop_job(job_id, actor=ACTOR)
        print("Nothing to check: no row has a Maskura or source link.")
        review(args)
        return

    last = [0.0]

    def until():
        job = store.job(job_id)
        if time.monotonic() - last[0] >= args.every or job["status"] in FINISHED:
            last[0] = time.monotonic()
            print(f"  {job['status']}: {store.items_done(job_id):,} links checked")
        return job["status"] in FINISHED

    worker = CliWorker(store, heartbeat_s=2.0)
    try:
        worker.run(until=until)
    except KeyboardInterrupt:
        store.stop_job(job_id, actor=ACTOR)
        print("Stopped. The workbook below covers the links checked so far.")
    review(args)


def review(args):
    if not os.path.exists(args.db):
        sys.exit(f"No results file at {args.db}. Run a check first.")
    got = sc_review_book.build(args.db, args.out)
    print(f"Wrote {args.out}: {got['products']:,} products, {got['needs_action']:,} need action.")
    for status, n in got["status"].items():
        if n:
            print(f"  {sc_review_book.STATUS[status][0]}: {n:,}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Check source prices and stock for a sourcing sheet.")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="check every link in a sheet, then write the review workbook")
    c.add_argument("sheet", help=".csv or .xlsx sourcing sheet")
    c.add_argument("--limit", type=int, default=0, help="check only the first N products")
    c.add_argument("--every", type=float, default=30.0, help="seconds between progress lines")
    r = sub.add_parser("review", help="rebuild the review workbook from stored results")
    for sp in (c, r):
        sp.add_argument("--db", default="checks.db", help="SQLite results file (default: checks.db)")
        sp.add_argument("--out", default="review.xlsx", help="review workbook to write (default: review.xlsx)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    (check if args.command == "check" else review)(args)


if __name__ == "__main__":
    main()
