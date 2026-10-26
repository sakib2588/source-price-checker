"""Bucket every product under the owner's rule (2026-10-23) and print the counts. Read-only.

The rule: read the Maskura price, read the source price of the same variant; a buyable source priced
strictly below Maskura is OK and gets no flag. This report reads the stored results as they are and
never writes to the database it is given.

Usage:
  python3 scripts/rescore_report.py --db PATH [--json OUT]
  python3 scripts/rescore_report.py --refinalize-copy PATH   # re-run finalize on a COPY, then report

--refinalize-copy re-runs sc_crawl.finalize_product on every stored product and saves it through
sc_store.Store.finalize, exactly as the worker does at the end of a crawl. It refuses the live database.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

LIVE_DB = os.path.expanduser("~/maskura-group/maskura-portal/source_check.db")


def _buyable(s):
    return bool(s and s.get("url") and s.get("verdict") == "PRICE" and s.get("available") is not False
                and s.get("price_gbp"))


def bucket(maskura, sources):
    if not maskura:
        return "NO_MASKURA_LINK"
    if maskura.get("status") == "NOT_FOUND":
        return "MASKURA_GONE"
    if maskura.get("status") != "OK" or not maskura.get("price_gbp"):
        return "MASKURA_UNREADABLE"
    linked = [s for s in sources if s and s.get("url")]
    if not linked:
        return "NO_SOURCE_LINK"
    live = [float(s["price_gbp"]) for s in linked if _buyable(s)]
    if not live:
        return "NO_BUYABLE_SOURCE"
    return "OK" if min(live) < float(maskura["price_gbp"]) else "SOURCE_DEARER"


def counts(db_path):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    srcs = collections.defaultdict(list)
    for r in c.execute("SELECT product_id, raw_json FROM source_results ORDER BY product_id, slot"):
        srcs[r["product_id"]].append(json.loads(r["raw_json"] or "{}"))
    out = collections.Counter()
    for p in c.execute("SELECT id, maskura_status, maskura_result_json, has_problem FROM products "
                       "WHERE COALESCE(archived, 0) = 0"):
        m = None if p["maskura_status"] == "NO_LINK" else json.loads(p["maskura_result_json"] or "{}")
        out[(bucket(m, srcs[p["id"]]), "flagged" if p["has_problem"] else "clean")] += 1
    c.close()
    return out


def refinalize_copy(db_path):
    """Re-finalize every product in a copy of the database. Returns {status: count}."""
    if os.path.realpath(db_path) == os.path.realpath(LIVE_DB):
        raise SystemExit("refusing to write the live database; copy it first")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from checker import sc_crawl
    from checker import sc_store
    st = sc_store.Store(db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    statuses = collections.Counter()
    for p in c.execute("SELECT id, maskura_result_json FROM products WHERE COALESCE(archived, 0) = 0").fetchall():
        sources = [dict(sc_store.NO_URL_SOURCE) if not s["url"] else json.loads(s["raw_json"] or "{}")
                   for s in c.execute("SELECT url, raw_json FROM source_results WHERE product_id = ? ORDER BY slot",
                                      (p["id"],))]
        m = json.loads(p["maskura_result_json"]) if p["maskura_result_json"] else None
        fin = sc_crawl.finalize_product(m, sources)
        st.finalize(p["id"], fin)
        statuses[(fin["status"], "flagged" if sc_store.problem_from(fin, None) else "clean")] += 1
    c.close()
    return statuses


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db")
    ap.add_argument("--json")
    ap.add_argument("--refinalize-copy", metavar="PATH")
    a = ap.parse_args(argv)
    if a.refinalize_copy:
        got = refinalize_copy(a.refinalize_copy)
        for (st, f), n in sorted(got.items()):
            print(f"{n:5}  {st:20} [{f}]")
        print(f"{sum(n for (_, f), n in got.items() if f == 'flagged'):5}  flagged in total (new rule)")
        if a.json:
            with open(a.json, "w") as fh:
                json.dump({f"{k}|{f}": n for (k, f), n in sorted(got.items())}, fh, indent=1)
        return
    if not a.db:
        ap.error("--db or --refinalize-copy is required")
    got = counts(a.db)
    for (b, f), n in sorted(got.items()):
        print(f"{n:5}  {b:20} [{f}]")
    print(f"{sum(n for (_, f), n in got.items() if f == 'flagged'):5}  flagged in total")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({f"{b}|{f}": n for (b, f), n in sorted(got.items())}, fh, indent=1)


if __name__ == "__main__":
    main()
