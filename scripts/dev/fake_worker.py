"""Run the real Source Checker worker against a FAKE network (for the kill -9 stress test). Never touches the
internet: every request is answered by an in-process fake site, slowly, so a test can kill the process mid-run.

Run: .venv/bin/python -B scripts/dev/fake_worker.py --db /tmp/sc.db --delay 0.05
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

import httpx  # noqa: E402

import sc_crawl  # noqa: E402
import sc_store  # noqa: E402
import sc_worker  # noqa: E402


class FastPacer(sc_crawl.Pacer):
    EBAY_MIN, EBAY_MAX, EBAY_UP, EBAY_DOWN = 0.01, 0.05, 0.01, 0.005
    OTHER_GAP, JITTER = 0.01, 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--delay", type=float, default=0.05)
    args = ap.parse_args()

    def site(request):
        time.sleep(args.delay)
        if request.url.host == "www.maskura.co.uk":
            handle = request.url.path.split("/products/")[1].removesuffix(".json")
            return httpx.Response(200, json={"product": {"handle": handle, "title": handle, "variants": [
                {"id": 1, "title": "Default Title", "price": "20.00", "available": True}]}})
        return httpx.Response(200, headers={"content-type": "text/html"}, html=(
            "<html><head><script type='application/ld+json'>{\"@type\": \"Product\", \"name\": \"Thing\", "
            "\"offers\": {\"price\": \"9.00\", \"priceCurrency\": \"GBP\", \"availability\": "
            "\"https://schema.org/InStock\"}}</script></head></html>"))

    store = sc_store.Store(args.db)
    store.check_version()
    factory = lambda: sc_crawl.build_client(transport=httpx.MockTransport(site),  # noqa: E731
                                            resolver=lambda h, p, t: ["93.184.216.34"])
    breaker = sc_worker.Breaker(probes=(lambda: True,))
    worker = sc_worker.Worker(store, client_factory=factory, pacer=FastPacer(), breaker=breaker, idle_s=0.02,
                              heartbeat_s=0.05, worker_id=f"fake:{os.getpid()}")

    def finished():
        jobs = store._conn().execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]
        return jobs == 0

    worker.run(until=finished)


if __name__ == "__main__":
    main()
