"""sources.ebay_api: the eBay Browse API lookup, and its hook in sc_crawl.fetch_source.

No real network: every request goes to httpx.MockTransport.

Run: PYTHONPATH=. python -B -m unittest tests.test_sc_ebay_api
"""
import json
import unittest
from unittest import mock

import httpx

from checker import sc_crawl as c
from checker.sources import ebay_api as api

URL = "https://www.ebay.co.uk/itm/123456789012"
VAR_URL = URL + "?var=222"


def item(price="9.99", status="IN_STOCK", title="Widget", item_id="v1|123456789012|0", currency="GBP", **aspects):
    return {"itemId": item_id, "title": title, "price": {"value": price, "currency": currency},
            "estimatedAvailabilities": [{"estimatedAvailabilityStatus": status}],
            "localizedAspects": [{"name": k, "value": v} for k, v in aspects.items()]}


GROUP = {"items": [
    item("24.99", "OUT_OF_STOCK", item_id="v1|123456789012|111", Size="Single", Brand="X"),
    item("29.99", "IN_STOCK", item_id="v1|123456789012|222", Size="Double", Brand="X"),
    item("32.99", "IN_STOCK", item_id="v1|123456789012|333", Size="King", Brand="X"),
]}
GROUP_ERROR = {"errors": [{"errorId": 11006, "domain": "API_BROWSE"}]}


def make(routes, **kw):
    """An EbayApi whose HTTP goes to `routes`: a dict of path suffix -> (status, body) or a callable."""
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        for suffix, reply in routes.items():
            if request.url.path.endswith(suffix):
                status, body = reply(request) if callable(reply) else reply
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    kw.setdefault("min_gap_s", 0)
    kw.setdefault("sleep", lambda s: None)
    return api.EbayApi("app", "cert", client=client, **kw), calls


class LookupTests(unittest.TestCase):
    def test_plain_item(self):
        a, calls = make({"get_item_by_legacy_id": (200, item())})
        r = a.lookup(URL)
        self.assertEqual((r.title, r.price_gbp, r.available, r.availability_text), ("Widget", 9.99, True, "In Stock"))
        self.assertEqual(r.offers, [])
        self.assertEqual(calls[0].headers["authorization"].startswith("Basic "), True)
        self.assertEqual(calls[1].headers["authorization"], "Bearer tok")
        self.assertEqual(calls[1].headers["x-ebay-c-marketplace-id"], "EBAY_GB")

    def test_out_of_stock(self):
        a, _ = make({"get_item_by_legacy_id": (200, item(status="OUT_OF_STOCK"))})
        r = a.lookup(URL)
        self.assertEqual((r.available, r.availability_text), (False, "Out of Stock"))

    def test_token_is_reused(self):
        a, calls = make({"get_item_by_legacy_id": (200, item())})
        a.lookup(URL)
        a.lookup(URL)
        self.assertEqual(sum(1 for q in calls if q.url.path.endswith("/token")), 1)

    def test_item_group_without_a_variation_returns_every_offer(self):
        a, calls = make({"get_item_by_legacy_id": (400, GROUP_ERROR), "get_items_by_item_group": (200, GROUP)})
        r = a.lookup(URL)
        self.assertEqual([(o["id"], o["label"], o["price_gbp"], o["available"]) for o in r.offers],
                         [("111", "Single", 24.99, False), ("222", "Double", 29.99, True),
                          ("333", "King", 32.99, True)])
        self.assertEqual(r.price_gbp, 29.99)          # the cheapest variation that is in stock
        self.assertTrue(r.available)
        self.assertIsNone(r.variation_label)

    def test_linked_variation_is_priced_on_its_own(self):
        a, _ = make({"get_items_by_item_group": (200, GROUP)})
        r = a.lookup(VAR_URL)
        self.assertEqual((r.variation_label, r.price_gbp, r.available), ("Double", 29.99, True))
        self.assertEqual(len(r.offers), 3)

    def test_linked_variation_missing_falls_back(self):
        a, _ = make({"get_items_by_item_group": (200, GROUP)})
        self.assertIsNone(a.lookup(URL + "?var=999"))

    def test_all_variations_out_of_stock_uses_cheapest_and_is_unavailable(self):
        g = {"items": [item("5", "OUT_OF_STOCK", item_id="v1|1|1", Size="S"),
                       item("7", "OUT_OF_STOCK", item_id="v1|1|2", Size="M")]}
        a, _ = make({"get_item_by_legacy_id": (400, GROUP_ERROR), "get_items_by_item_group": (200, g)})
        r = a.lookup("https://www.ebay.co.uk/itm/123456789012")
        self.assertEqual((r.price_gbp, r.available), (5.0, False))

    def test_cases_that_fall_back_to_the_page(self):
        for name, routes, url in [
            ("ended listing", {"get_item_by_legacy_id": (404, {"errors": [{"errorId": 11001}]})}, URL),
            ("not pounds", {"get_item_by_legacy_id": (200, item(currency="USD"))}, URL),
            ("no price", {"get_item_by_legacy_id": (200, {"title": "x"})}, URL),
            ("other ebay site", {"get_item_by_legacy_id": (200, item())}, "https://www.ebay.com/itm/123456789012"),
            ("not an item link", {}, "https://www.ebay.co.uk/sch/i.html?_nkw=x"),
        ]:
            with self.subTest(name):
                a, _ = make(routes)
                self.assertIsNone(a.lookup(url))

    def test_rate_limit_switches_the_api_off_for_a_while(self):
        now = [0.0]
        a, calls = make({"get_item_by_legacy_id": (429, {"errors": [{"errorId": 2001}]})}, clock=lambda: now[0])
        self.assertIsNone(a.lookup(URL))
        n = len(calls)
        self.assertIsNone(a.lookup(URL))
        self.assertEqual(len(calls), n)               # skipped, no new request
        now[0] += api.BACKOFF_S + 1
        self.assertIsNone(a.lookup(URL))
        self.assertGreater(len(calls), n)             # tried again after the back-off

    def test_daily_cap_stops_the_calls(self):
        a, calls = make({"get_item_by_legacy_id": (200, item())}, daily_cap=2)
        self.assertIsNotNone(a.lookup(URL))
        self.assertIsNotNone(a.lookup(URL))
        self.assertIsNone(a.lookup(URL))
        self.assertEqual(a.calls_today, 2)

    def test_expired_token_is_refreshed_once(self):
        state = {"n": 0}

        def reply(request):
            state["n"] += 1
            return (401, {}) if state["n"] == 1 else (200, item())

        a, calls = make({"get_item_by_legacy_id": reply})
        self.assertIsNotNone(a.lookup(URL))
        self.assertEqual(sum(1 for q in calls if q.url.path.endswith("/token")), 2)

    def test_network_error_falls_back(self):
        def boom(request):
            raise httpx.ConnectError("down")

        a = api.EbayApi("app", "cert", client=httpx.Client(transport=httpx.MockTransport(boom)),
                        min_gap_s=0, sleep=lambda s: None)
        self.assertIsNone(a.lookup(URL))

    def test_pacing_spaces_calls(self):
        sleeps, now = [], [0.0]
        a, _ = make({"get_item_by_legacy_id": (200, item())}, min_gap_s=0.2, clock=lambda: now[0],
                    sleep=sleeps.append)
        a.lookup(URL)
        a.lookup(URL)
        self.assertEqual([round(s, 2) for s in sleeps], [0.2])


class EnvTests(unittest.TestCase):
    def setUp(self):
        api._default = None

    def tearDown(self):
        api._default = None

    def test_no_keys_means_no_api(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(api.default())
            self.assertIsNone(api.lookup(URL))

    def test_disable_switch(self):
        with mock.patch.dict("os.environ", {"EBAY_APP_ID": "a", "EBAY_CERT_ID": "b", "EBAY_API_DISABLE": "1"}, clear=True):
            self.assertIsNone(api.default())

    def test_keys_build_one_shared_client(self):
        with mock.patch.dict("os.environ", {"EBAY_APP_ID": "a", "EBAY_CERT_ID": "b"}, clear=True):
            self.assertIs(api.default(), api.default())


class FetchSourceHook(unittest.TestCase):
    """fetch_source uses the API result when there is one and never touches the page; else it scrapes."""

    class Pacer:
        def __init__(self):
            self.waited = []

        def wait(self, host):
            self.waited.append(host)

        def note(self, *a, **k):
            pass

        def cooldown_until(self, host):
            return None

    def client(self, hits):
        def handler(request):
            hits.append(str(request.url))
            return httpx.Response(200, text="<html><head><title>x</title></head><body>" + "x" * 6000 + "</body></html>")
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_api_hit_makes_no_page_request(self):
        res = api.EbayApi("a", "b", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
        from checker.sources.base import SourceResult
        hit = SourceResult(url=URL, platform="ebay", title="Widget", price_gbp=9.99, available=True,
                           availability_text="In Stock")
        hits, pacer = [], self.Pacer()
        with mock.patch.object(c._ebay_api, "lookup", return_value=hit):
            raw = c.fetch_source(self.client(hits), pacer, URL)
        self.assertEqual((raw["verdict"], raw["price_gbp"], raw["available"], raw["http_status"]),
                         ("PRICE", 9.99, True, 200))
        self.assertEqual((hits, pacer.waited), ([], []))

    def test_api_miss_scrapes_the_page(self):
        hits, pacer = [], self.Pacer()
        with mock.patch.object(c._ebay_api, "lookup", return_value=None):
            c.fetch_source(self.client(hits), pacer, URL)
        self.assertEqual(len(hits) >= 1, True)
        self.assertEqual(pacer.waited[0], "www.ebay.co.uk")

    def test_other_platforms_never_call_the_api(self):
        hits = []
        with mock.patch.object(c._ebay_api, "lookup") as look:
            c.fetch_source(self.client(hits), self.Pacer(), "https://example.com/p/1")
        look.assert_not_called()


if __name__ == "__main__":
    unittest.main()
