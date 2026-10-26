"""sc_crawl: the safe client, the pacer, supplier and Maskura fetches, the catalog, and finalize_product.

No real network: every request goes to httpx.MockTransport or a fake httpcore backend, and DNS is a
fake resolver.

Run: .venv/bin/python -B -m unittest test_sc_crawl
"""
import gzip
import json
import re
import socket
import time
import unittest
from unittest import mock

import httpx

import sc_crawl as c

PUBLIC = "93.184.216.34"
PAD = "<!-- " + "x" * 6000 + " -->"
EBAY = "https://www.ebay.co.uk/itm/123456789012"
MASKURA = "https://www.maskura.co.uk/products/widget"
SOURCE_FIELDS = {"url", "host", "platform", "verdict", "reason", "price_gbp", "available", "avail_text",
                 "title", "final_url", "http_status", "error", "removal_comment", "variation_unverified",
                 "currency", "fetched_at", "soft_block"}
MASKURA_FIELDS = {"url", "handle", "variant_id", "title", "price_gbp", "available", "variants", "status",
                  "http_status", "error", "fetched_at"}
ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def ebay_page(body):
    return f"<html><head><title>x</title></head><body>{body}{PAD}</body></html>"


def ld(name, price):
    return (f'<script type="application/ld+json">{{"@type":"Product","name":"{name}",'
            f'"offers":{{"price":"{price}"}}}}</script>')


SHELL = b"<html><head><title>eBay</title></head><body></body></html>"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))
        self.now += seconds


def public_resolver(calls=None):
    def resolve(host, port, timeout):
        if calls is not None:
            calls.append((host, port, timeout))
        return [PUBLIC]
    return resolve


def mock_client(handler, resolver=None, **kw):
    return c.build_client(transport=httpx.MockTransport(handler), resolver=resolver or public_resolver(), **kw)


class RecordingPacer(c.Pacer):
    def __init__(self):
        self.clock = FakeClock()
        super().__init__(clock=self.clock, sleep=self.clock.sleep, rng=lambda: 0.0)
        self.waits, self.notes = [], []

    def wait(self, host):
        self.waits.append(host)
        super().wait(host)

    def note(self, host, outcome, retry_after=None):
        self.notes.append(outcome)
        super().note(host, outcome, retry_after)


def routes(table):
    """A MockTransport handler: url -> list of responses (the last one repeats). Records every call."""
    seen = []

    def handler(request):
        key = str(request.url)
        seen.append(key)
        items = table.get(key) or table.get(key.split("?")[0])
        if items is None:
            return httpx.Response(599, content=b"unrouted " + key.encode())
        n = sum(1 for s in seen if s == key) - 1
        item = items[min(n, len(items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item
    handler.seen = seen
    return handler


def html(status=200, body=b"", headers=None):
    return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode(),
                          headers=headers or {"content-type": "text/html"})


def jsonr(data, status=200):
    return httpx.Response(status, json=data)


# ── Safe client ──────────────────────────────────────────────────────────────

class SafeClientPolicy(unittest.TestCase):
    def test_settings(self):
        client = mock_client(lambda r: httpx.Response(200))
        self.assertFalse(client.trust_env)
        self.assertTrue(client.follow_redirects)

    def test_public_host_is_fetched_and_resolved_by_the_client(self):
        calls = []
        client = mock_client(lambda r: httpx.Response(200, content=b"hi"), resolver=public_resolver(calls))
        r = client.get("https://shop.example/p")
        self.assertEqual(r.text, "hi")
        self.assertEqual(calls[0][:2], ("shop.example", 443))
        self.assertLessEqual(calls[0][2], 8)

    def redirect_to(self, location):
        hits = []

        def handler(request):
            hits.append(request.url.host)
            if request.url.host == "shop.example":
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(200, content=b"internal secrets")
        client = mock_client(handler)
        with self.assertRaises(c.UnsafeTarget):
            client.get("https://shop.example/p")
        self.assertEqual(hits, ["shop.example"])        # the internal hop was never sent

    def test_redirect_to_loopback(self):
        self.redirect_to("http://127.0.0.1/admin")

    def test_redirect_to_metadata_address(self):
        self.redirect_to("http://169.254.169.254/latest/meta-data/")

    def test_redirect_to_v4_mapped_loopback(self):
        self.redirect_to("http://[::ffff:127.0.0.1]/admin")

    def test_redirect_to_a_name_that_resolves_inside(self):
        def resolver(host, port, timeout):
            return {"shop.example": [PUBLIC], "evil.example": ["::ffff:127.0.0.1"]}[host]

        def handler(request):
            return httpx.Response(302, headers={"location": "http://evil.example/"})
        with self.assertRaises(c.UnsafeTarget):
            mock_client(handler, resolver=resolver).get("https://shop.example/p")

    def test_any_private_address_refuses_the_host(self):
        client = mock_client(lambda r: httpx.Response(200), resolver=lambda h, p, t: [PUBLIC, "10.0.0.5"])
        with self.assertRaises(c.UnsafeTarget):
            client.get("https://shop.example/")

    def test_dns_down_is_transient(self):
        def resolver(host, port, timeout):
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        with self.assertRaises(c.TransientNetworkError) as ctx:
            mock_client(lambda r: httpx.Response(200), resolver=resolver).get("https://shop.example/")
        self.assertIsInstance(ctx.exception, httpx.ConnectError)

    def test_empty_dns_answer_is_transient(self):
        with self.assertRaises(c.TransientNetworkError):
            mock_client(lambda r: httpx.Response(200), resolver=lambda h, p, t: []).get("https://shop.example/")

    def test_system_resolver_times_out(self):
        def slow(host, port):
            time.sleep(2)
            return []
        with mock.patch.object(c, "_getaddrinfo", slow):
            client = c.build_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                                    dns_timeout_s=0.2)
            start = time.monotonic()
            with self.assertRaises(c.TransientNetworkError):
                client.get("https://shop.example/")
            self.assertLess(time.monotonic() - start, 1.5)

    def test_system_resolver_failure_is_transient(self):
        def broken(host, port):
            raise socket.gaierror(-2, "Name or service not known")
        with mock.patch.object(c, "_getaddrinfo", broken):
            with self.assertRaises(c.TransientNetworkError):
                c.system_resolver("shop.example", 443, 1.0)

    def test_trickled_body_hits_the_deadline(self):
        clock = FakeClock()

        class Trickle(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(1000):
                    clock.now += 1.0
                    yield b"x"
        client = mock_client(lambda r: httpx.Response(200, stream=Trickle()), deadline_s=10, clock=clock)
        with self.assertRaises(c.DeadlineExceeded):
            client.get("https://shop.example/")
        self.assertLessEqual(clock.now - 1000.0, 12)

    def test_trickled_body_hits_the_deadline_in_real_time(self):
        class Trickle(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(200):
                    time.sleep(0.02)
                    yield b"x"
        client = mock_client(lambda r: httpx.Response(200, stream=Trickle()), deadline_s=0.3)
        start = time.monotonic()
        with self.assertRaises(httpx.TimeoutException):
            client.get("https://shop.example/")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_deadline_covers_every_redirect_hop(self):
        clock = FakeClock()

        def handler(request):
            clock.now += 4
            return httpx.Response(302, headers={"location": f"https://shop.example/{clock.now}"})
        with self.assertRaises(httpx.TimeoutException):
            mock_client(handler, deadline_s=10, clock=clock).get("https://shop.example/")

    def test_oversize_body_with_length(self):
        client = mock_client(lambda r: httpx.Response(200, content=b"x" * 101), max_body=100)
        with self.assertRaises(c.ResponseTooLarge):
            client.get("https://shop.example/")

    def test_oversize_body_without_length(self):
        class Big(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(50):
                    yield b"x" * 10
        client = mock_client(lambda r: httpx.Response(200, stream=Big()), max_body=100)
        with self.assertRaises(c.ResponseTooLarge):
            client.get("https://shop.example/")

    def test_compressed_body_that_expands_past_the_cap(self):
        bomb = gzip.compress(b"\0" * 5000)
        client = mock_client(lambda r: httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}),
                             max_body=1000)
        self.assertLess(len(bomb), 1000)
        with self.assertRaises(c.ResponseTooLarge):
            client.get("https://shop.example/")

    def test_body_under_the_cap_reads_normally(self):
        body = gzip.compress(b'{"a": 1}')
        client = mock_client(lambda r: httpx.Response(200, content=body, headers={
            "content-encoding": "gzip", "content-type": "application/json"}), max_body=1000)
        self.assertEqual(client.get("https://shop.example/").json(), {"a": 1})

    def test_redirect_body_is_never_decoded(self):
        def handler(request):
            if request.url.path == "/a":
                return httpx.Response(301, stream=httpx.ByteStream(b"not gzip at all"),
                                      headers={"location": "/b", "content-encoding": "gzip"})
            return httpx.Response(200, content=b"done")
        r = mock_client(handler).get("https://shop.example/a")
        self.assertEqual((r.text, str(r.url)), ("done", "https://shop.example/b"))


class FakeStream:
    def __init__(self, log):
        self.log, self.sent, self.done = log, b"", False

    def write(self, buffer, timeout=None):
        self.sent += buffer
        self.log.append(("write", timeout))

    def read(self, max_bytes, timeout=None):
        self.log.append(("read", timeout))
        if self.done:
            return b""
        self.done = True
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok"

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log.append(("tls", server_hostname))
        return self

    def get_extra_info(self, info):
        return None

    def close(self):
        pass


class FakeBackend:
    def __init__(self):
        self.log, self.streams = [], []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.append(("connect", host, port, timeout))
        s = FakeStream(self.log)
        self.streams.append(s)
        return s

    def sleep(self, seconds):
        pass


class Pinning(unittest.TestCase):
    def test_connects_to_the_vetted_address_keeping_sni_and_host(self):
        backend = FakeBackend()
        client = c.build_client(transport=c.PinnedHTTPTransport(network_backend=backend),
                                resolver=public_resolver(), deadline_s=3)
        r = client.get("https://shop.example/p")
        self.assertEqual(r.text, "ok")
        connect = next(e for e in backend.log if e[0] == "connect")
        self.assertEqual(connect[1:3], (PUBLIC, 443))
        self.assertIn(("tls", "shop.example"), backend.log)
        self.assertIn(b"Host: shop.example\r\n", backend.streams[0].sent)
        # every socket wait is clamped to the 3 s deadline
        waits = [e[-1] for e in backend.log if e[0] in ("read", "write", "connect")]
        self.assertTrue(waits and all(w is not None and w <= 3 for w in waits), waits)

    def test_backend_refuses_a_host_without_a_pin(self):
        backend = c.PinnedBackend(FakeBackend())
        with self.assertRaises(c.UnsafeTarget):
            backend.connect_tcp("shop.example", 443)

    def test_unix_sockets_are_refused(self):
        with self.assertRaises(c.UnsafeTarget):
            c.PinnedBackend(FakeBackend()).connect_unix_socket("/run/x.sock")


# ── Pacer ────────────────────────────────────────────────────────────────────

class PacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.p = c.Pacer(clock=self.clock, sleep=self.clock.sleep, rng=lambda: 0.5)

    def block(self, host="www.ebay.co.uk", n=1, outcome="blocked", **kw):
        for _ in range(n):
            self.p.note(host, outcome, **kw)

    def test_first_request_does_not_wait(self):
        self.p.wait("shop.example")
        self.assertEqual(self.clock.slept, [])

    def test_other_hosts_use_two_seconds_plus_jitter(self):
        self.p.wait("shop.example")
        self.p.note("shop.example", "ok")
        self.p.wait("shop.example")
        self.assertEqual(self.clock.slept, [2.5])

    def test_gap_counts_from_the_end_of_a_slow_request(self):
        self.p.wait("shop.example")
        self.clock.now += 10
        self.p.note("shop.example", "ok")
        self.p.wait("shop.example")
        self.assertEqual(self.clock.slept, [2.5])

    def test_ebay_starts_at_two_and_a_half_seconds_without_jitter(self):
        self.p.wait("www.ebay.co.uk")
        self.p.note("www.ebay.co.uk", "ok")
        self.p.wait("www.ebay.co.uk")
        self.assertEqual(self.clock.slept, [2.5])

    def test_ebay_gap_grows_half_a_second_per_block_or_shell(self):
        self.block(n=1)
        self.block(n=1, outcome="shell")
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 3.5)

    def test_ebay_gap_is_capped_at_six_seconds(self):
        self.block(n=12)
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 6.0)

    def test_ebay_gap_falls_a_tenth_per_clean_five_minutes_down_to_the_floor(self):
        self.block(n=2)
        self.clock.now += 299
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 3.5)
        self.clock.now += 1
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 3.4)
        self.clock.now += 600
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 3.2)
        self.clock.now += 7200
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 2.5)

    def test_errors_and_ok_do_not_change_the_gap(self):
        self.block(n=5, outcome="error")
        self.block(n=5, outcome="ok")
        self.assertEqual(self.p.gap("www.ebay.co.uk"), 2.5)
        self.assertIsNone(self.p.cooldown_until("www.ebay.co.uk"))

    def test_three_blocks_within_ten_minutes_start_a_cool_down(self):
        self.block(n=2)
        self.assertIsNone(self.p.cooldown_until("www.ebay.co.uk"))
        self.block(n=1, outcome="shell")
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk"), self.clock.now + 300)
        self.assertFalse(self.p.ready("www.ebay.co.uk"))
        self.assertTrue(self.p.ready("shop.example"))          # that host only

    def test_blocks_spread_over_more_than_ten_minutes_do_not(self):
        for _ in range(3):
            self.block(n=1)
            self.clock.now += 301
        self.assertIsNone(self.p.cooldown_until("www.ebay.co.uk"))

    def test_cool_down_ladder(self):
        lengths = []
        for _ in range(5):
            self.block(n=3)
            lengths.append(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now)
            self.clock.now = self.p.cooldown_until("www.ebay.co.uk") - 60   # next burst within the hour
        self.assertEqual(lengths, [300, 600, 1200, 2400, 3600])

    def test_cool_down_never_exceeds_an_hour(self):
        self.block(n=3 * 8)
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 3600)

    def test_ladder_resets_after_an_hour_without_blocks(self):
        self.block(n=3)
        self.clock.now += 300 + 3600
        self.block(n=3)
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 300)

    def test_scattered_ebay_refusals_pause_two_minutes_without_climbing(self):
        # 23 Oct 2026: eBay refused about 1 request in 10, one listing at a time, and each one loaded on retry;
        # the ladder still climbed to 40 minutes and eBay sat idle most of the run.
        for _ in range(2):
            self.block(n=30, outcome="ok")
            self.block(n=3)
            self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 120)
            self.clock.now += 121
        self.block(n=1, outcome="challenge")                    # a bot check still climbs from the bottom
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 300)

    def test_ebay_refusing_a_quarter_or_more_keeps_the_ladder(self):
        self.block(n=12, outcome="ok")
        self.block(n=3)
        self.clock.now += 301
        self.block(n=3)                                         # 6 of 18 refused
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 600)

    def test_ebay_with_too_few_results_keeps_the_ladder(self):
        self.block(n=10, outcome="ok")
        self.block(n=3)
        self.assertEqual(self.p.cooldown_until("www.ebay.co.uk") - self.clock.now, 300)

    def test_other_hosts_keep_the_ladder_however_scattered(self):
        self.block(host="shop.example", n=40, outcome="ok")
        self.block(host="shop.example", n=3)
        self.assertEqual(self.p.cooldown_until("shop.example") - self.clock.now, 300)

    def test_wait_sleeps_through_a_cool_down(self):
        self.block(n=3)
        until = self.p.cooldown_until("www.ebay.co.uk")
        self.p.wait("www.ebay.co.uk")
        self.assertEqual(self.clock.now, until)
        self.assertTrue(self.p.ready("www.ebay.co.uk") or self.p.cooldown_until("www.ebay.co.uk") is None)

    def test_retry_after_sets_the_cool_down(self):
        self.p.note("shop.example", "blocked", retry_after=120)
        self.assertEqual(self.p.cooldown_until("shop.example"), self.clock.now + 120)
        self.p.note("other.example", "blocked", retry_after=10 ** 6)
        self.assertEqual(self.p.cooldown_until("other.example"), self.clock.now + 3600)
        self.p.note("third.example", "ok", retry_after=120)
        self.assertIsNone(self.p.cooldown_until("third.example"))

    def test_cool_down_ends(self):
        self.p.note("shop.example", "blocked", retry_after=60)
        self.clock.now += 61
        self.assertIsNone(self.p.cooldown_until("shop.example"))
        self.assertTrue(self.p.ready("shop.example"))

    def test_snapshot_and_bad_outcome(self):
        self.block(n=1)
        snap = self.p.snapshot()["www.ebay.co.uk"]
        self.assertEqual(snap["gap_s"], 3.0)
        self.assertEqual(snap["recent_blocks"], 1)
        self.assertEqual(snap["last_outcome"], "blocked")
        self.assertIn("cooldown_until", snap)
        with self.assertRaises(ValueError):
            self.p.note("x.example", "weird")


# ── fetch_source ─────────────────────────────────────────────────────────────

class FetchSource(unittest.TestCase):
    def fetch(self, table, url=EBAY, pacer=None):
        self.handler = routes(table)
        self.pacer = pacer or RecordingPacer()
        return c.fetch_source(mock_client(self.handler), self.pacer, url)

    def test_fields_and_no_derived_values(self):
        sd = self.fetch({EBAY: [html(body=ebay_page(ld("Widget", "9.99")))]})
        self.assertTrue(SOURCE_FIELDS <= set(sd), SOURCE_FIELDS - set(sd))
        for derived in c.DERIVED_FIELDS:
            self.assertNotIn(derived, sd)
        self.assertEqual((sd["verdict"], sd["price_gbp"], sd["currency"]), ("PRICE", 9.99, "GBP"))
        self.assertEqual((sd["host"], sd["platform"]), ("www.ebay.co.uk", "ebay"))
        self.assertRegex(sd["fetched_at"], ISO_Z)
        self.assertEqual(self.pacer.waits, ["www.ebay.co.uk"])
        self.assertEqual(self.pacer.notes, ["ok"])

    def test_shell_is_refetched_once_through_the_pacer_then_read(self):
        sd = self.fetch({EBAY: [html(body=SHELL), html(body=ebay_page(ld("Widget", "9.99")))]})
        self.assertEqual(len(self.handler.seen), 2)
        self.assertEqual(self.pacer.waits, ["www.ebay.co.uk"] * 2)
        self.assertEqual(self.pacer.notes, ["shell", "ok"])
        self.assertEqual((sd["verdict"], sd["price_gbp"], sd["soft_block"]), ("PRICE", 9.99, False))

    def test_shell_that_stays_a_shell_is_a_soft_block(self):
        sd = self.fetch({EBAY: [html(body=SHELL)]})
        self.assertEqual(len(self.handler.seen), 2)               # one re-fetch, not a loop
        self.assertEqual(self.pacer.notes, ["shell", "shell"])
        self.assertEqual((sd["verdict"], sd["soft_block"]), ("BLOCKED", True))
        self.assertTrue(sd["error"].startswith("BLOCKED"))

    def test_shell_that_starts_a_cool_down_is_not_refetched(self):
        pacer = RecordingPacer()
        pacer.note("www.ebay.co.uk", "blocked")
        pacer.note("www.ebay.co.uk", "blocked")
        sd = self.fetch({EBAY: [html(body=SHELL)]}, pacer=pacer)
        self.assertEqual(len(self.handler.seen), 1)
        self.assertEqual((sd["verdict"], sd["soft_block"]), ("BLOCKED", True))
        self.assertIsNotNone(pacer.cooldown_until("www.ebay.co.uk"))

    def test_removed_listing(self):
        sd = self.fetch({EBAY: [html(body=ebay_page("<h1>We looked everywhere.</h1>"))]})
        self.assertEqual((sd["verdict"], sd["price_gbp"]), ("NOT_FOUND", None))

    def test_redirect_to_a_different_item(self):
        other = "https://www.ebay.co.uk/itm/999999999999"
        sd = self.fetch({EBAY: [httpx.Response(302, headers={"location": other})],
                         other: [html(body=ebay_page(ld("Other thing", "4.50")))]})
        self.assertEqual((sd["verdict"], sd["final_url"], sd["price_gbp"], sd["price_seen"]),
                         ("ENDED_REDIRECT", other, None, 4.5))

    def test_tracking_redirect_to_the_same_item(self):
        same = EBAY + "?hash=item1"
        sd = self.fetch({EBAY: [httpx.Response(302, headers={"location": same})],
                         same: [html(body=ebay_page(ld("Widget", "9.99")))]})
        self.assertEqual(sd["verdict"], "PRICE")

    def test_http_404_and_403(self):
        self.assertEqual(self.fetch({EBAY: [html(404)]})["verdict"], "NOT_FOUND")
        sd = self.fetch({EBAY: [html(403)]})
        self.assertEqual((sd["verdict"], sd["http_status"]), ("BLOCKED", 403))
        self.assertEqual(self.pacer.notes, ["blocked"])

    def test_ebay_and_amazon_503_are_blocked_and_not_retried(self):
        for url in (EBAY, "https://www.amazon.co.uk/dp/B012345678"):
            with self.subTest(url=url):
                sd = self.fetch({url: [html(503)]}, url=url)
                self.assertEqual((sd["verdict"], sd["http_status"]), ("BLOCKED", 503))
                self.assertEqual(len(self.handler.seen), 1)
                self.assertEqual(self.pacer.notes, ["blocked"])

    def test_other_503_is_an_error_and_not_retried(self):
        url = "https://shop.example/p"
        sd = self.fetch({url: [html(503)]}, url=url)
        self.assertEqual(sd["verdict"], "ERROR")
        self.assertEqual(len(self.handler.seen), 1)
        self.assertEqual(self.pacer.notes, ["error"])

    def test_429_retry_after_starts_a_cool_down(self):
        sd = self.fetch({EBAY: [html(429, headers={"retry-after": "30"})]})
        self.assertEqual(sd["verdict"], "BLOCKED")
        self.assertEqual(self.pacer.cooldown_until("www.ebay.co.uk"), self.pacer.clock.now + 30)

    def test_amazon_robot_check_is_blocked(self):
        url = "https://www.amazon.co.uk/dp/B012345678"
        sd = self.fetch({url: [html(body="<html><head><title>Robot Check</title></head></html>")]}, url=url)
        self.assertEqual(sd["verdict"], "BLOCKED")
        self.assertEqual(self.pacer.notes, ["blocked"])

    def test_variation_in_the_link(self):
        url = EBAY + "?var=587128761961"
        self.assertTrue(self.fetch({url: [html(body=ebay_page(ld("W", "9.99")))]}, url=url)["variation_unverified"])
        self.assertFalse(self.fetch({EBAY: [html(body=ebay_page(ld("W", "9.99")))]})["variation_unverified"])

    def test_us_amazon_is_not_pounds(self):
        url = "https://www.amazon.com/dp/B012345678"
        page = (b'<html><body><span id="productTitle">Widget</span><span class="a-price-whole">89</span>'
                b'<span class="a-price-fraction">90</span></body></html>')
        sd = self.fetch({url: [html(body=page)]}, url=url)
        self.assertEqual((sd["verdict"], sd["price_gbp"], sd["price_native"], sd["currency"]),
                         ("NON_GBP", None, 89.9, "OTHER"))

    def test_uk_amazon_price(self):
        url = "https://www.amazon.co.uk/dp/B012345678"
        page = (b'<html><body><span id="productTitle">Widget</span><span class="a-price-whole">18</span>'
                b'<span class="a-price-fraction">82</span></body></html>')
        self.assertEqual(self.fetch({url: [html(body=page)]}, url=url)["price_gbp"], 18.82)

    def test_redirect_to_an_internal_address(self):
        sd = self.fetch({EBAY: [httpx.Response(302, headers={"location": "http://127.0.0.1/"})]})
        self.assertEqual(sd["verdict"], "ERROR")
        self.assertEqual(sd["error"], "Blocked: URL resolves to an internal address")
        self.assertEqual(self.pacer.notes, ["error"])

    def test_network_failure(self):
        sd = self.fetch({EBAY: [httpx.ConnectError("connection refused")]})
        self.assertEqual(sd["verdict"], "ERROR")
        self.assertTrue(sd["error"].startswith("Fetch failed"))
        self.assertEqual(self.pacer.notes, ["error"])

    def test_dns_down(self):
        def resolver(host, port, timeout):
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        pacer = RecordingPacer()
        sd = c.fetch_source(mock_client(routes({}), resolver=resolver), pacer, EBAY)
        self.assertEqual(sd["verdict"], "ERROR")
        self.assertEqual(pacer.notes, ["error"])

    def test_blank_url_is_the_no_url_shape(self):
        sd = c.fetch_source(None, None, "")
        self.assertEqual((sd["url"], sd["platform"], sd["verdict"]), ("", "none", "NONE"))


PRODUCT = {"product": {"title": "Widget", "handle": "widget", "variants": [
    {"id": 11, "title": "Small", "price": "10.00", "inventory_quantity": 3},
    {"id": 22, "title": "Large", "price": "15.00", "inventory_quantity": 0}]}}


class FetchMaskura(unittest.TestCase):
    def fetch(self, table, url=MASKURA):
        self.handler = routes(table)
        self.pacer = RecordingPacer()
        return c.fetch_maskura(mock_client(self.handler), self.pacer, url)

    def test_json_ok(self):
        m = self.fetch({MASKURA + ".json": [jsonr(PRODUCT)]})
        self.assertTrue(MASKURA_FIELDS <= set(m), MASKURA_FIELDS - set(m))
        self.assertEqual((m["status"], m["title"], m["price_gbp"], m["available"], m["handle"]),
                         ("OK", "Widget", 10.0, True, "widget"))
        self.assertEqual([v["id"] for v in m["variants"]], ["11", "22"])
        self.assertEqual(m["http_status"], 200)
        self.assertRegex(m["fetched_at"], ISO_Z)
        self.assertEqual(self.pacer.waits, ["www.maskura.co.uk"])

    def test_variant_from_the_link(self):
        m = self.fetch({MASKURA + ".json": [jsonr(PRODUCT)]}, url=MASKURA + "?variant=22&_pos=1")
        self.assertEqual((m["variant_id"], m["price_gbp"], m["available"]), ("22", 15.0, False))
        self.assertEqual(self.handler.seen, [MASKURA + ".json"])

    def test_variant_missing_from_the_product(self):
        m = self.fetch({MASKURA + ".json": [jsonr(PRODUCT)]}, url=MASKURA + "?variant=99")
        self.assertEqual(m["status"], "VARIANT_NOT_FOUND")

    def test_404_probes_the_page_once_for_a_move_then_is_not_found(self):
        # One extra request: the product page, to catch a Shopify handle redirect (ACCURACY_PLAN Task 2).
        m = self.fetch({MASKURA + ".json": [html(404)], MASKURA: [html(404)]})
        self.assertEqual((m["status"], m["http_status"]), ("NOT_FOUND", 404))
        self.assertEqual(len(self.handler.seen), 2)

    def test_429_and_5xx_are_blocked_without_fallback(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                m = self.fetch({MASKURA + ".json": [html(status)]})
                self.assertEqual((m["status"], m["http_status"]), ("BLOCKED", status))
                self.assertEqual(len(self.handler.seen), 1)
                self.assertEqual(self.pacer.notes, ["blocked"])

    def test_non_json_answer_falls_back_to_the_page(self):
        page = ('<html><script type="application/ld+json">{"@type":"Product","name":"Widget",'
                '"offers":{"price":"12.50","availability":"https://schema.org/InStock"}}</script></html>')
        m = self.fetch({MASKURA + ".json": [html(body="<html>hi</html>")], MASKURA: [html(body=page)]})
        self.assertEqual((m["status"], m["price_gbp"], m["available"]), ("OK", 12.5, True))
        self.assertEqual(self.pacer.waits, ["www.maskura.co.uk"] * 2)

    def test_network_error(self):
        m = self.fetch({MASKURA + ".json": [httpx.ConnectError("down")]})
        self.assertEqual(m["status"], "ERROR")
        self.assertEqual(len(self.handler.seen), 1)

    def test_bad_link_makes_no_request(self):
        m = self.fetch({}, url="https://www.ebay.co.uk/itm/1")
        self.assertEqual(m["status"], "BAD_LINK")
        self.assertEqual(self.handler.seen, [])

    def test_catalog_adapter_has_the_same_shape(self):
        prod = {"title": "Widget", "handle": "widget", "variants": [
            {"id": 11, "title": "Small", "price": "10.00", "available": False},
            {"id": 22, "title": "Large", "price": "15.00", "available": True}]}
        a = c.catalog_to_maskura(prod)
        self.assertEqual(set(a), set(self.fetch({MASKURA + ".json": [jsonr(PRODUCT)]})))
        self.assertEqual((a["status"], a["price_gbp"], a["available"], a["url"]), ("OK", 10.0, False, MASKURA))
        b = c.catalog_to_maskura({"product": prod}, variant_id="22")
        self.assertEqual((b["price_gbp"], b["available"], b["url"]), (15.0, True, MASKURA + "?variant=22"))


class Catalog(unittest.TestCase):
    def page(self, n):
        return f"https://www.maskura.co.uk/products.json?limit=2&page={n}"

    def run_catalog(self, table, **kw):
        self.handler = routes(table)
        self.pacer = RecordingPacer()
        return c.fetch_catalog(mock_client(self.handler), self.pacer, page_size=2, **kw)

    def test_reads_until_the_first_empty_page(self):
        out = self.run_catalog({self.page(1): [jsonr({"products": [{"handle": "a"}, {"handle": "b"}]})],
                                self.page(2): [jsonr({"products": [{"handle": "c"}]})],
                                self.page(3): [jsonr({"products": []})]})
        self.assertEqual((out["complete"], out["pages"], out["error"]), (True, 3, None))
        self.assertEqual([p["handle"] for p in out["products"]], ["a", "b", "c"])
        self.assertEqual(len(self.pacer.waits), 3)

    def test_429_is_an_error_not_the_end(self):
        out = self.run_catalog({self.page(1): [jsonr({"products": [{"handle": "a"}]})],
                                self.page(2): [html(429)]})
        self.assertFalse(out["complete"])
        self.assertIn("429", out["error"])
        self.assertEqual(len(out["products"]), 1)

    def test_empty_first_page_is_an_error(self):
        out = self.run_catalog({self.page(1): [jsonr({"products": []})]})
        self.assertFalse(out["complete"])
        self.assertTrue(out["error"])

    def test_body_that_is_not_json_is_an_error(self):
        out = self.run_catalog({self.page(1): [html(body="")]})
        self.assertFalse(out["complete"])
        self.assertTrue(out["error"])

    def test_max_pages(self):
        out = self.run_catalog({self.page(n): [jsonr({"products": [{"handle": str(n)}]})] for n in (1, 2)},
                               max_pages=2)
        self.assertFalse(out["complete"])
        self.assertEqual(out["pages"], 2)


class Sitemap(unittest.TestCase):
    def test_handles_from_product_sitemaps(self):
        index = ("<sitemapindex><sitemap><loc>https://www.maskura.co.uk/sitemap_products_1.xml?from=1&amp;to=9"
                 "</loc></sitemap><sitemap><loc>https://www.maskura.co.uk/sitemap_pages_1.xml</loc></sitemap>"
                 "<sitemap><loc>https://evil.example/sitemap_products_9.xml</loc></sitemap></sitemapindex>")
        child = ("<urlset><url><loc>https://www.maskura.co.uk/products/Widget</loc></url>"
                 "<url><loc>https://www.maskura.co.uk/products/mat</loc></url>"
                 "<url><loc>https://www.maskura.co.uk/</loc></url></urlset>")
        handler = routes({"https://www.maskura.co.uk/sitemap.xml": [html(body=index)],
                          "https://www.maskura.co.uk/sitemap_products_1.xml?from=1&to=9": [html(body=child)]})
        pacer = RecordingPacer()
        self.assertEqual(c.fetch_sitemap_handles(mock_client(handler), pacer), {"widget", "mat"})
        self.assertEqual(len(handler.seen), 2)
        self.assertEqual(len(pacer.waits), 2)

    def test_products_with_titles_from_image_tags(self):
        index = ("<sitemapindex><sitemap><loc>https://www.maskura.co.uk/sitemap_products_1.xml?from=1&amp;to=9"
                 "</loc></sitemap></sitemapindex>")
        child = ("<urlset><url><loc>https://www.maskura.co.uk/</loc></url>"
                 "<url><loc>https://www.maskura.co.uk/products/fungal-nail</loc><lastmod>2026-09-01</lastmod>"
                 "<image:image><image:loc>https://cdn.shopify.com/a.jpg</image:loc>"
                 "<image:title>Fungal Nail &amp; Toe Treatment</image:title></image:image></url>"
                 "<url><loc>https://www.maskura.co.uk/products/no-image</loc></url></urlset>")
        handler = routes({"https://www.maskura.co.uk/sitemap.xml": [html(body=index)],
                          "https://www.maskura.co.uk/sitemap_products_1.xml?from=1&to=9": [html(body=child)]})
        got = c.fetch_sitemap_products(mock_client(handler), RecordingPacer())
        self.assertEqual(got, [{"handle": "fungal-nail", "title": "Fungal Nail & Toe Treatment",
                                "image": "https://cdn.shopify.com/a.jpg"},
                               {"handle": "no-image", "title": None, "image": None}])

    def test_failure_raises(self):
        handler = routes({"https://www.maskura.co.uk/sitemap.xml": [html(429)]})
        with self.assertRaises(c.SitemapError):
            c.fetch_sitemap_handles(mock_client(handler), RecordingPacer())


class BotCheck(unittest.TestCase):
    """P5: Shopify's bot check is never followed, solved or retried; it is BLOCKED with an immediate cool-down."""
    CHALLENGE = "https://www.maskura.co.uk/products/widget.json?__shopify_bv_challenge=abc"

    def test_a_maskura_challenge_redirect_is_blocked_without_following_it(self):
        handler = routes({MASKURA + ".json": [httpx.Response(302, headers={"location": self.CHALLENGE})],
                          self.CHALLENGE: [html(403)]})
        pacer = RecordingPacer()
        out = c.fetch_maskura(mock_client(handler), pacer, MASKURA)
        self.assertEqual((out["status"], out["challenge"]), ("BLOCKED", True))
        self.assertIn("bot check", out["error"])
        self.assertEqual(handler.seen, [MASKURA + ".json"], "the challenge URL is never requested")
        self.assertEqual(pacer.notes, ["challenge"])
        until = pacer.cooldown_until("www.maskura.co.uk")
        self.assertAlmostEqual(until - pacer.clock(), 300.0, delta=1.0)

    def test_challenges_climb_the_cooldown_ladder(self):
        pacer = RecordingPacer()
        host = "www.maskura.co.uk"
        pacer.note(host, "challenge")
        first = pacer.cooldown_until(host) - pacer.clock()
        pacer.clock.sleep(400)
        pacer.note(host, "challenge")
        second = pacer.cooldown_until(host) - pacer.clock()
        self.assertEqual((round(first), round(second)), (300, 600))

    def test_a_supplier_challenge_is_blocked_too(self):
        challenge = EBAY + "?__shopify_bv_challenge=x"
        handler = routes({EBAY: [httpx.Response(302, headers={"location": challenge})]})
        out = c.fetch_source(mock_client(handler), RecordingPacer(), EBAY)
        self.assertEqual((out["verdict"], out.get("challenge")), ("BLOCKED", True))

    def test_a_transport_failure_is_marked_so_the_worker_can_ask_the_breaker(self):
        handler = routes({MASKURA + ".json": [httpx.ConnectError("network down")]})
        out = c.fetch_maskura(mock_client(handler), RecordingPacer(), MASKURA)
        self.assertEqual((out["status"], out.get("transport_error")), ("ERROR", True))
        handler = routes({EBAY: [httpx.ConnectError("network down")]})
        out = c.fetch_source(mock_client(handler), RecordingPacer(), EBAY)
        self.assertTrue(out.get("transport_error"))


# ── finalize_product ─────────────────────────────────────────────────────────

def raw(**kw):
    base = {"url": EBAY, "host": "www.ebay.co.uk", "platform": "ebay", "verdict": "PRICE", "reason": "",
            "price_gbp": 9.99, "available": True, "avail_text": "", "title": "Widget", "final_url": EBAY,
            "http_status": 200, "error": None, "removal_comment": "", "variation_unverified": False,
            "currency": "GBP", "fetched_at": "2026-10-16T03:12:00Z", "soft_block": False}
    base.update(kw)
    return base


M_OK = {"title": "Widget", "price_gbp": 20.0, "status": "OK"}
NO_URL = {"url": "", "platform": "none", "verdict": "NONE"}


class Finalize(unittest.TestCase):
    def test_slots_raw_no_url_pending(self):
        out = c.finalize_product(M_OK, [raw(available=False), NO_URL, None])
        self.assertEqual(out["flags"], ["S1_OOS", "S3_PENDING"])
        self.assertTrue(out["being_checked"])
        self.assertEqual(len(out["sources"]), 3)
        for f in c.DERIVED_FIELDS:
            self.assertIsNone(out["sources"][1][f])
        self.assertIsNone(out["sources"][2])

    def test_no_url_only(self):
        out = c.finalize_product(M_OK, [NO_URL, c.none_source()])
        self.assertEqual((out["flags"], out["being_checked"], out["worst_verdict"]), ([], False, "NONE"))

    def test_derived_values(self):
        sd = c.finalize_product(M_OK, [raw()])["sources"][0]
        self.assertEqual((sd["price_diff"], sd["pct_vs_source"]), (10.01, 100.2))
        self.assertEqual((sd["variant_mismatch"], sd["variant_mismatch_tokens"]), (False, []))
        self.assertIsNone(sd["price_suspect"])
        self.assertEqual(sd["notes"], "")

    def test_pure(self):
        r = raw()
        before = json.dumps(r, sort_keys=True)
        c.finalize_product(M_OK, [r])
        self.assertEqual(json.dumps(r, sort_keys=True), before)

    def test_flags(self):
        # Owner's rule (2026-10-23, docs/source-check/ACCURACY_PLAN.md): source 4 is buyable at 1.00 under
        # Maskura's 10.00, so the product is OK and carries no flag; the other sources' states stay in
        # their own derived fields and in worst_verdict.
        m = {"title": "Widget Black 2 Pack", "price_gbp": 10.0, "status": "OK"}
        sources = [
            raw(price_gbp=12.0, title="Widget Red"),
            raw(url="https://www.amazon.com/dp/B012345678", platform="amazon", verdict="NON_GBP", price_gbp=None,
                title=None),
            raw(verdict="NOT_FOUND", price_gbp=None, error="Fetch failed: Client error '404 Not Found'",
                http_status=404, title=None),
            raw(price_gbp=1.0, title="Widget Black 2 Pack", variation_unverified=True),
        ]
        out = c.finalize_product(m, sources)
        self.assertEqual((out["status"], out["best_source"], out["flags"]), ("OK", 4, []))
        self.assertEqual(out["worst_verdict"], "NOT_FOUND")
        self.assertEqual(out["sources"][0]["variant_mismatch_tokens"], ["2pack", "black"])
        self.assertTrue(out["sources"][3]["price_suspect"])
        self.assertFalse(out["being_checked"])
        # Without source 4 the cheapest buyable source costs more than Maskura: only that is flagged.
        out = c.finalize_product(m, sources[:3])
        self.assertEqual((out["status"], out["flags"]), ("SOURCE_DEARER", ["S1_INVERTED"]))

    def test_blocked_with_a_retry_pending_is_being_checked_not_an_error(self):
        out = c.finalize_product(M_OK, [raw(verdict="BLOCKED", price_gbp=None, error="BLOCKED: x",
                                            retry_pending=True),
                                        raw(verdict="ERROR", price_gbp=None, error="Fetch failed: x",
                                            retry_pending=True)])
        self.assertEqual(out["flags"], ["S1_BLOCKED_RETRY", "S2_PENDING"])
        self.assertTrue(out["being_checked"])

    def test_final_block_is_an_error(self):
        out = c.finalize_product(M_OK, [raw(verdict="BLOCKED", price_gbp=None, error="BLOCKED: x")])
        self.assertEqual(out["flags"], ["S1_ERR"])

    def test_maskura_read_error(self):
        self.assertEqual(c.finalize_product({"status": "ERROR", "error": "x"}, [raw()])["flags"], ["M_ERR"])
        self.assertEqual(c.finalize_product({"status": "BLOCKED", "error": "x"}, [])["flags"], ["M_ERR"])
        out = c.finalize_product({"status": "BLOCKED", "retry_pending": True}, [raw()])
        self.assertEqual((out["flags"], out["being_checked"]), ([], True))
        self.assertEqual(c.finalize_product({"status": "NOT_FOUND"}, [raw()])["flags"], [])

    def test_no_maskura_price_means_no_margin(self):
        sd = c.finalize_product(None, [raw()])["sources"][0]
        self.assertIsNone(sd["price_diff"])
        self.assertIsNone(sd["pct_vs_source"])

    def test_generic_note_with_a_price_is_not_err(self):
        # Changed 2026-10-16: the generic extractor's "verify manually" note sits in `error`, but a page
        # whose price was read is not a read failure, so it is no longer flagged S{i}_ERR.
        out = c.finalize_product(M_OK, [raw(url="https://shop.example/p", platform="generic",
                                            error="Generic extractor — verify manually (rates as of 2026-10-15)")])
        self.assertEqual(out["flags"], [])

    def test_missing_removal_comment_is_rebuilt(self):
        r = raw(verdict="NOT_FOUND", price_gbp=None)
        r.pop("removal_comment")
        sd = c.finalize_product(M_OK, [r])["sources"][0]
        self.assertEqual(sd["removal_comment"], "Listing removed (the page says it no longer exists)")


class NoBannedGlyphs(unittest.TestCase):
    def test_new_modules(self):
        for name in ("sc_crawl.py", "sc_urls.py", "sc_labels.py", "test_sc_crawl.py", "test_sc_urls.py",
                     "test_sc_labels.py"):
            with open(name, encoding="utf-8") as f:
                text = f.read()
            for glyph in (chr(cp) for cp in (0xA7, 0xB6, 0x2020, 0x2021)):
                self.assertNotIn(glyph, text, name)


class TestFlagQuirksFixed(unittest.TestCase):
    """Lead decisions 2026-10-16: no false "Could not read" flags, and Retry-After in both forms."""

    MASKURA = {"status": "OK", "price_gbp": 20.0, "title": "Mug"}

    def flags(self, raw):
        return c.finalize_product(dict(self.MASKURA), [raw])["flags"]

    def test_page_with_a_price_is_not_could_not_read(self):
        raw = {"url": "https://shop.example/p", "platform": "generic", "verdict": "PRICE", "price_gbp": 12.0,
               "available": True, "error": "Price found by heuristic; verify manually"}
        self.assertNotIn("S1_ERR", self.flags(raw))

    def test_404_is_removed_only(self):
        raw = {"url": "https://www.ebay.co.uk/itm/123456789012", "platform": "ebay", "verdict": "NOT_FOUND",
               "price_gbp": None, "available": None, "error": "HTTP 404", "http_status": 404}
        flags = self.flags(raw)
        self.assertIn("S1_GONE", flags)
        self.assertNotIn("S1_ERR", flags)

    def test_unread_page_is_could_not_read(self):
        raw = {"url": "https://shop.example/p", "platform": "generic", "verdict": "ERROR", "price_gbp": None,
               "available": None, "error": "Connection reset"}
        self.assertEqual(self.flags(raw), ["S1_ERR"])

    def test_out_of_stock_without_price_is_out_of_stock_only(self):
        raw = {"url": "https://shop.example/p", "platform": "generic", "verdict": "OUT_OF_STOCK",
               "price_gbp": None, "available": False, "error": "No price on page"}
        self.assertEqual(self.flags(raw), ["S1_OOS"])

    def test_retry_after_http_date(self):
        import email.utils
        when = email.utils.formatdate(time.time() + 120, usegmt=True)
        self.assertTrue(100 <= c.retry_after_s(httpx.Response(429, headers={"Retry-After": when})) <= 121)
        self.assertEqual(c.retry_after_s(httpx.Response(429, headers={"Retry-After": "30"})), 30.0)
        past = email.utils.formatdate(time.time() - 60, usegmt=True)
        self.assertEqual(c.retry_after_s(httpx.Response(429, headers={"Retry-After": past})), 0.0)
        self.assertIsNone(c.retry_after_s(httpx.Response(429, headers={"Retry-After": "soon"})))


if __name__ == "__main__":
    unittest.main()
