"""Fetch and extract for the Source Checker: the safe HTTP client, the per-host pacer, supplier and
Maskura fetches, the Maskura catalog, and `finalize_product` (the only place derived fields are made).

No database and no global state: the client, pacer, clock and sleep are passed in.

Safe client (`build_client`)
----------------------------
The client is an `httpx.Client` subclass with two layers under it:

1. `_GuardTransport` runs for every hop, redirects included (httpx sends each redirect through the
   transport again). It resolves the host itself with a timeout (the resolver is injectable), refuses
   the hop with `UnsafeTarget` unless every address is public (`ipaddress.ip_address(x).is_global`,
   after unwrapping IPv4-mapped, 6to4, Teredo and NAT64 forms), and records the vetted addresses in a
   thread-local "pin" for the duration of the hop. It also clamps the hop's timeouts to the time left,
   empties redirect bodies, and wraps the response body so it stops at the deadline or the body cap.
2. The default inner transport is `PinnedHTTPTransport`, an `httpx.HTTPTransport` whose httpcore pool
   uses `PinnedBackend` as its network backend. httpcore asks the backend to `connect_tcp(host, port)`
   with the URL's host name; `PinnedBackend` ignores DNS and connects to the pinned, vetted address
   instead. httpcore then calls `start_tls(server_hostname=<host name>)` on that socket, so SNI and
   certificate checks still use the real name, and the Host header comes from the URL unchanged. A
   connect for a host with no pin is refused. Every socket read and write is clamped to the request's
   wall-clock deadline, so a server that trickles one byte at a time still hits it.

`SafeClient.send` sets the deadline for the whole request (all redirect hops) and reads the body
itself, counting decoded bytes, so a compressed body that expands past the cap is refused too.
`trust_env=False`, so no proxy or certificate settings come from the environment.

Pacer
-----
One `Pacer` holds the spacing and cool-downs per host; every request goes through `wait(host)` and
then `note(host, outcome)`. eBay uses an adaptive gap (2.5 to 6 s), other hosts 2.0 s plus jitter.

Behavior moved from app.py is unchanged except: no internal retries; an Amazon or eBay 503, a
BLOCK_RE match and an eBay script-only shell are BLOCKED; a Maskura `.json` 429 or 5xx is BLOCKED with
no HTML fallback; derived fields are made only in `finalize_product`.
"""
from __future__ import annotations

import collections
import datetime
import email.utils
import ipaddress
import math
import random
import html
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import httpcore
import httpx

from . import outcomes
from . import title_quality as tq
from .sc_urls import (AMAZON_ASIN_RE, EBAY_ITEM_RE, MASKURA_BASE, MASKURA_HOSTS, detect_platform, host_of,
                     maskura_url, normalize_maskura_url)
from .sources import amazon as _amazon
from .sources import ebay as _ebay
from .sources import generic as _generic
from .sources import maskura as _maskura

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-GB,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
REQUEST_TIMEOUT_S = 20.0
BLOCK_RE = outcomes.BLOCK_RE
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


# ── Errors ───────────────────────────────────────────────────────────────────

class TransientNetworkError(httpx.ConnectError):
    """The host name could not be resolved (DNS down, timed out, or no address). Worth retrying."""


class BotChallenge(httpx.RequestError):
    """The site sent us to Shopify's bot verification. The challenge URL is never requested, solved or replayed:
    the request is refused before it leaves, and the result is BLOCKED (P5)."""


CHALLENGE_MARK = "__shopify_bv_challenge"


def _refuse_challenge(request):
    if CHALLENGE_MARK in str(request.url):
        raise BotChallenge("The site asked for a bot check", request=request)


class UnsafeTarget(httpx.RequestError):
    """The request was refused by policy: a non-public address, a non-web scheme, or an unvetted host."""


class ResponseTooLarge(httpx.RequestError):
    """The response body went over the size cap."""


class DeadlineExceeded(httpx.ReadTimeout):
    """The request took longer than its wall-clock deadline (timed out)."""


# ── Address policy ───────────────────────────────────────────────────────────

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _bad_ip(ip) -> bool:
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def vet_address(address) -> str:
    """The address to connect to (IPv4-mapped IPv6 unwrapped to IPv4), or UnsafeTarget if it is not
    public. Embedded IPv4 addresses (6to4, Teredo, NAT64) must be public as well."""
    text = str(address).split("%", 1)[0].strip("[]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise UnsafeTarget(f"Not an IP address: {address!r}") from None
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        else:
            embedded = [ip.sixtofour] + list(ip.teredo or ())
            if ip in _NAT64:
                embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
            for e in embedded:
                if e is not None and _bad_ip(e):
                    raise UnsafeTarget(f"Refused: {address} embeds the non-public address {e}")
    if _bad_ip(ip):
        raise UnsafeTarget(f"Refused: {address} is not a public address")
    return str(ip)


def _getaddrinfo(host, port):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def system_resolver(host, port, timeout):
    """Addresses for a host via the system resolver, with a timeout (getaddrinfo has none of its own,
    so it runs in a daemon thread). Raises TransientNetworkError on failure or timeout."""
    box = {}

    def run():
        try:
            box["v"] = _getaddrinfo(host, port)
        except Exception as exc:  # noqa: BLE001 - reported below
            box["e"] = exc

    t = threading.Thread(target=run, name=f"dns-{host}", daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive():
        raise TransientNetworkError(f"DNS lookup for {host} timed out after {timeout:g} s")
    if "e" in box:
        raise TransientNetworkError(f"DNS lookup for {host} failed: {box['e']}")
    return [ai[4][0] for ai in box["v"]]


# ── Thread-local deadline and pins ───────────────────────────────────────────

_local = threading.local()


class _Scope:
    """Set a thread-local attribute for a block; deadlines only ever get earlier when nested."""

    def __init__(self, name, value, earliest=False):
        self.name, self.value, self.earliest = name, value, earliest

    def __enter__(self):
        self.old = getattr(_local, self.name, None)
        value = self.value
        if self.earliest and self.old is not None and value is not None:
            value = min(self.old, value)
        setattr(_local, self.name, value)
        return value

    def __exit__(self, *exc):
        setattr(_local, self.name, self.old)
        return False


def _deadline():
    return getattr(_local, "deadline", None)


def _clock_now():
    return getattr(_local, "clock", None) or time.monotonic


def _remaining(clock=None):
    d = _deadline()
    if d is None:
        return None
    return d - (clock or _clock_now())()


def _clamp(timeout, clock=None):
    left = _remaining(clock)
    if left is None:
        return timeout
    if left <= 0:
        raise httpcore.ReadTimeout("The request went past its deadline (timed out)")
    return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    """A socket stream whose every read and write waits no longer than the request deadline."""

    def __init__(self, inner):
        self._inner = inner

    def read(self, max_bytes, timeout=None):
        return self._inner.read(max_bytes, _clamp(timeout))

    def write(self, buffer, timeout=None):
        return self._inner.write(buffer, _clamp(timeout))

    def close(self):
        return self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(self._inner.start_tls(ssl_context, server_hostname, _clamp(timeout)))

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class PinnedBackend(httpcore.NetworkBackend):
    """Connects to the vetted address pinned for the host by `_GuardTransport`, never to DNS."""

    def __init__(self, inner=None):
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        pins = getattr(_local, "pins", None) or {}
        addresses = pins.get(str(host).lower())
        if not addresses:
            raise UnsafeTarget(f"Refused: no vetted address for {host}")
        last = None
        for address in addresses:
            try:
                stream = self._inner.connect_tcp(address, port, _clamp(timeout), local_address, socket_options)
                return _DeadlineStream(stream)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UnsafeTarget("Refused: unix sockets are not allowed")

    def sleep(self, seconds):
        self._inner.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """httpx.HTTPTransport with a `PinnedBackend` under its connection pool."""

    def __init__(self, network_backend=None, **kw):
        super().__init__(trust_env=False, **kw)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0,
            http1=True, http2=False, retries=0,
            network_backend=PinnedBackend(network_backend),
        )


class _GuardedStream(httpx.SyncByteStream):
    """A response body that stops at the deadline and at the raw size cap."""

    def __init__(self, inner, deadline, max_body, clock):
        self._inner, self._deadline, self._max, self._clock = inner, deadline, max_body, clock

    def __iter__(self):
        total = 0
        it = iter(self._inner)
        while True:
            with _Scope("deadline", self._deadline, earliest=True), _Scope("clock", self._clock):
                if self._clock() > self._deadline:
                    raise DeadlineExceeded("The response took longer than the deadline (timed out)")
                try:
                    chunk = next(it)
                except StopIteration:
                    return
            total += len(chunk)
            if total > self._max:
                raise ResponseTooLarge(f"The response is larger than {self._max} bytes")
            if self._clock() > self._deadline:
                raise DeadlineExceeded("The response took longer than the deadline (timed out)")
            yield chunk

    def close(self):
        close = getattr(self._inner, "close", None)
        if close:
            close()


class _GuardTransport(httpx.BaseTransport):
    def __init__(self, inner, resolver, *, deadline_s, dns_timeout_s, max_body, clock):
        self._inner, self._resolver = inner, resolver
        self._deadline_s, self._dns_timeout_s, self._max_body = deadline_s, dns_timeout_s, max_body
        self._clock = clock

    def _addresses(self, host, port, timeout):
        try:
            ipaddress.ip_address(host.split("%", 1)[0].strip("[]"))
        except ValueError:
            pass
        else:
            return [vet_address(host)]           # an IP literal is vetted as it is, no DNS
        try:
            found = self._resolver(host, port, timeout)
        except (TransientNetworkError, UnsafeTarget):
            raise
        except Exception as exc:  # noqa: BLE001 - any resolver failure is transient
            raise TransientNetworkError(f"DNS lookup for {host} failed: {exc}") from exc
        found = list(dict.fromkeys(found or ()))
        if not found:
            raise TransientNetworkError(f"DNS lookup for {host} returned no address")
        return [vet_address(a) for a in found]   # any non-public address refuses the whole host

    def handle_request(self, request):
        now = self._clock()
        current = _deadline()
        deadline = current if current is not None else now + self._deadline_s
        if request.url.scheme not in ("http", "https"):
            raise UnsafeTarget(f"Refused: the scheme {request.url.scheme!r} is not allowed")
        host = request.url.raw_host.decode("ascii").lower().rstrip(".")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if not host:
            raise UnsafeTarget("Refused: the URL has no host")
        left = deadline - now
        if left <= 0:
            raise DeadlineExceeded("The request went past its deadline (timed out)")
        addresses = self._addresses(host, port, min(self._dns_timeout_s, left))
        left = deadline - self._clock()
        if left <= 0:
            raise DeadlineExceeded("The request went past its deadline (timed out)")
        timeouts = request.extensions.get("timeout")
        if isinstance(timeouts, dict):
            for key in ("connect", "read", "write", "pool"):
                value = timeouts.get(key)
                timeouts[key] = left if value is None else min(value, left)
        with _Scope("deadline", deadline, earliest=True), _Scope("clock", self._clock), \
                _Scope("pins", {host: addresses}):
            response = self._inner.handle_request(request)
        return self._wrap(response, deadline)

    def _wrap(self, response, deadline):
        headers = httpx.Headers(response.headers)
        if response.status_code in _REDIRECT_STATUSES and "location" in headers:
            # httpx reads and decodes a redirect body before following it; never trust that body.
            response.close()
            for key in ("content-encoding", "content-length", "transfer-encoding"):
                headers.pop(key, None)
            return httpx.Response(response.status_code, headers=headers, content=b"",
                                  extensions=response.extensions)
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > self._max_body:
            response.close()
            raise ResponseTooLarge(f"The response is larger than {self._max_body} bytes")
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_GuardedStream(response.stream, deadline, self._max_body, self._clock),
                              extensions=response.extensions)

    def close(self):
        self._inner.close()


class SafeClient(httpx.Client):
    """httpx.Client with a wall-clock deadline per request (all hops) and a decoded body cap."""

    def __init__(self, *, deadline_s, max_body, clock, **kw):
        super().__init__(**kw)
        self.deadline_s, self.max_body, self._clock = deadline_s, max_body, clock

    def send(self, request, *, stream=False, **kw):
        deadline = self._clock() + self.deadline_s
        with _Scope("deadline", deadline, earliest=True), _Scope("clock", self._clock):
            response = super().send(request, stream=True, **kw)
            if stream:
                return response
            try:
                total, parts = 0, []
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_body:
                        raise ResponseTooLarge(f"The response is larger than {self.max_body} bytes")
                    parts.append(chunk)
                # httpx's own read() stores the body here; do the same so .text and .json() work.
                response._content = b"".join(parts)
            finally:
                response.close()
        return response


def build_client(*, deadline_s=45, max_body=5 * 1024 * 1024, dns_timeout_s=8, transport=None,
                 resolver=None, clock=None) -> httpx.Client:
    """The shared client. `transport` replaces the pinned network transport (tests pass
    httpx.MockTransport); `resolver(host, port, timeout) -> [address]` replaces DNS; `clock` replaces
    time.monotonic for the deadline."""
    clock = clock or time.monotonic
    inner = transport if transport is not None else PinnedHTTPTransport()
    guard = _GuardTransport(inner, resolver or system_resolver, deadline_s=deadline_s,
                            dns_timeout_s=dns_timeout_s, max_body=max_body, clock=clock)
    return SafeClient(deadline_s=deadline_s, max_body=max_body, clock=clock, transport=guard,
                      trust_env=False, follow_redirects=True, max_redirects=10,
                      timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
                      # Runs before every hop, redirects included: a bot-check URL is never requested.
                      event_hooks={"request": [_refuse_challenge]})


# ── Pacer ────────────────────────────────────────────────────────────────────

class _HostState:
    __slots__ = ("last", "jitter", "gap", "anchor", "bad", "cooldown_until", "level", "last_bad",
                 "last_outcome", "recent")

    def __init__(self, gap):
        self.last = None
        self.jitter = 0.0
        self.gap = gap
        self.anchor = None
        self.bad = collections.deque()
        self.cooldown_until = None
        self.level = 0
        self.last_bad = None
        self.last_outcome = None
        self.recent = collections.deque(maxlen=Pacer.EBAY_RECENT_N)   # eBay only: True for each refusal


class Pacer:
    """Spacing and cool-downs per host. Thread-safe; `clock`, `sleep` and `rng` are injectable.

    - eBay: the gap starts at 2.5 s, grows by 0.5 s for each blocked or shell result, shrinks by
      0.1 s for each full 5 minutes without one, and stays within 2.5 to 6 s. No jitter, so the
      bound holds exactly.
    - Other hosts: 2.0 s plus up to 1.0 s of jitter.
    - Cool-down (that host only): 3 blocked or shell results within 10 minutes start a cool-down of
      5, then 10, 20, 40 and at most 60 minutes. The step goes back to 5 minutes after 60 minutes
      with no blocked or shell result. A Retry-After value (seconds, capped at 60 minutes) on a
      failed request starts a cool-down of that length.
    - A bot check ("challenge") counts as blocked and starts a cool-down at once, on the same ladder,
      without waiting for a burst: the store's IP reputation is not worth a second try.
    - eBay, scattered refusals: when at least 20 of the last 40 eBay results are known and fewer than a
      quarter of them were refused, a burst of 3 pauses eBay for 2 minutes and the ladder does not climb.
      On 23 Oct 2026 eBay refused about 1 request in 10, one listing at a time, and each loaded on retry,
      yet the ladder reached 40 minutes and eBay sat idle most of the run. A bot check, or a quarter or
      more refused, goes back to the ladder.
    - The gap counts from the later of the slot `wait` reserved and the time `note` was called, so
      a slow response does not shorten the next gap.
    """

    EBAY_MIN, EBAY_MAX, EBAY_UP, EBAY_DOWN = 2.5, 6.0, 0.5, 0.1
    CLEAN_PERIOD_S = 300.0
    WINDOW_S = 600.0
    BURST = 3
    EBAY_RECENT_N, EBAY_RECENT_MIN, EBAY_SCATTERED_MAX, EBAY_SHORT_COOLDOWN_S = 40, 20, 0.25, 120.0
    COOLDOWNS_S = (300.0, 600.0, 1200.0, 2400.0, 3600.0)
    COOLDOWN_MAX_S = 3600.0
    LADDER_RESET_S = 3600.0
    OTHER_GAP, JITTER = 2.0, 1.0
    OUTCOMES = ("ok", "blocked", "shell", "error", "challenge")

    def __init__(self, clock=time.monotonic, sleep=time.sleep, rng=random.random):
        self._clock, self._sleep, self._rng = clock, sleep, rng
        self._lock = threading.Lock()
        self._hosts = {}

    @staticmethod
    def _key(host):
        return str(host or "").strip().lower().rstrip(".")

    def _is_ebay(self, key):
        return detect_platform(key) == "ebay"

    def _state(self, key):
        st = self._hosts.get(key)
        if st is None:
            st = self._hosts[key] = _HostState(self.EBAY_MIN if self._is_ebay(key) else self.OTHER_GAP)
        return st

    def _decay(self, st, now):
        if st.anchor is None or st.gap <= self.EBAY_MIN:
            return
        periods = math.floor((now - st.anchor) / self.CLEAN_PERIOD_S)
        if periods > 0:
            st.gap = round(max(self.EBAY_MIN, st.gap - self.EBAY_DOWN * periods), 3)
            st.anchor += periods * self.CLEAN_PERIOD_S

    def _gap(self, key, st, now):
        if self._is_ebay(key):
            self._decay(st, now)
            return st.gap
        return self.OTHER_GAP

    def _next_allowed(self, key, st, now):
        at = -math.inf if st.last is None else st.last + self._gap(key, st, now) + st.jitter
        if st.cooldown_until is not None:
            if st.cooldown_until <= now:
                st.cooldown_until = None
            else:
                at = max(at, st.cooldown_until)
        return at

    def _draw_jitter(self, key):
        return 0.0 if self._is_ebay(key) else self._rng() * self.JITTER

    def wait(self, host):
        """Block until the host's next slot, and reserve it."""
        key = self._key(host)
        with self._lock:
            st = self._state(key)
            now = self._clock()
            target = max(now, self._next_allowed(key, st, now))
            st.last = target
            st.jitter = self._draw_jitter(key)
        if target > now:
            self._sleep(target - now)

    def note(self, host, outcome, retry_after=None):
        """Record how a request to the host ended: ok, blocked, shell or error."""
        if outcome not in self.OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        key = self._key(host)
        with self._lock:
            st = self._state(key)
            now = self._clock()
            st.last = now if st.last is None else max(st.last, now)
            st.jitter = self._draw_jitter(key)
            st.last_outcome = outcome
            if self._is_ebay(key) and outcome != "error":
                st.recent.append(outcome != "ok")
            if outcome in ("blocked", "shell", "challenge"):
                if self._is_ebay(key):
                    self._decay(st, now)
                    st.gap = round(min(self.EBAY_MAX, st.gap + self.EBAY_UP), 3)
                    st.anchor = now
                if st.last_bad is not None and now - st.last_bad >= self.LADDER_RESET_S:
                    st.level = 0
                st.last_bad = now
                st.bad.append(now)
                while st.bad and st.bad[0] <= now - self.WINDOW_S:
                    st.bad.popleft()
                if len(st.bad) >= self.BURST or outcome == "challenge":
                    st.bad.clear()
                    if outcome != "challenge" and self._scattered(key, st):
                        self._start_cooldown(st, now, self.EBAY_SHORT_COOLDOWN_S)
                    else:
                        length = self.COOLDOWNS_S[min(st.level, len(self.COOLDOWNS_S) - 1)]
                        st.level += 1
                        self._start_cooldown(st, now, length)
            if retry_after is not None and outcome != "ok":
                try:
                    seconds = float(retry_after)
                except (TypeError, ValueError):
                    seconds = None
                if seconds is not None and seconds > 0:
                    self._start_cooldown(st, now, min(seconds, self.COOLDOWN_MAX_S))

    def _scattered(self, key, st):
        """eBay is refusing the odd request, not pushing back: enough recent results and under a quarter refused."""
        return (self._is_ebay(key) and len(st.recent) >= self.EBAY_RECENT_MIN
                and sum(st.recent) / len(st.recent) < self.EBAY_SCATTERED_MAX)

    @staticmethod
    def _start_cooldown(st, now, length):
        until = now + length
        st.cooldown_until = until if st.cooldown_until is None else max(st.cooldown_until, until)

    def ready(self, host) -> bool:
        key = self._key(host)
        with self._lock:
            st = self._state(key)
            now = self._clock()
            return now >= self._next_allowed(key, st, now)

    def cooldown_until(self, host):
        key = self._key(host)
        with self._lock:
            st = self._hosts.get(key)
            if st is None or st.cooldown_until is None:
                return None
            if st.cooldown_until <= self._clock():
                st.cooldown_until = None
                return None
            return st.cooldown_until

    def gap(self, host) -> float:
        key = self._key(host)
        with self._lock:
            return self._gap(key, self._state(key), self._clock())

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            out = {}
            for key, st in self._hosts.items():
                nxt = self._next_allowed(key, st, now)
                recent = sum(1 for t in st.bad if t > now - self.WINDOW_S)
                out[key] = {"gap_s": self._gap(key, st, now),
                            "next_allowed": None if nxt == -math.inf else nxt,
                            "ready": now >= nxt,
                            "cooldown_until": st.cooldown_until,
                            "cooldown_level": st.level,
                            "recent_blocks": recent,
                            "last_outcome": st.last_outcome}
            return out


# ── Helpers moved from app.py ────────────────────────────────────────────────

_VARIATION_RE = re.compile(r"[?&](?:var|variation|variant)=\d", re.I)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_error(error):
    """Error text with URLs removed, so words and numbers inside a URL are not mistaken for the cause."""
    return re.sub(r"https?://\S+", "", str(error or ""))


def build_notes(error, available, price_gbp, removal_comment):
    """Single Notes cell value: combines removal reason, error, and uncertainty signals."""
    parts = []
    if removal_comment:
        parts.append(removal_comment)
    elif error and error != "No URL":
        parts.append(f"Error: {error[:120]}")
    if available is None and price_gbp is not None:
        parts.append("Availability unknown — verify manually")
    if error and BLOCK_RE.search(_clean_error(error)):
        parts.append("Possible bot-wall — result may be incorrect")
    if not parts and available is False:
        parts.append("Out of stock on supplier")
    return " | ".join(parts)


def removal_comment(error, available, platform, url, verdict=""):
    if not url or not url.startswith("http"):
        return "No source URL in sheet"
    if not error:
        if verdict == "NOT_FOUND":
            return "Listing removed (the page says it no longer exists)"
        if verdict == "ENDED_REDIRECT":
            return "Listing ended, or the link now leads to a different item"
        if verdict == "NON_GBP":
            return "Price is not in pounds, so it is not compared"
    if error:
        e = _clean_error(error).lower()
        if re.search(r"\b404\b", e) or "not found" in e:  return "Listing removed or deleted (404)"
        if re.search(r"\b410\b", e):                      return "Listing permanently deleted (410)"
        if re.search(r"\b403\b", e) or "forbidden" in e:  return "Access blocked (403)"
        if re.search(r"\b401\b", e):                      return "Access blocked (401)"
        if re.search(r"\b429\b", e):                      return "Rate limited (429) — try again later"
        if re.search(r"\b5\d\d\b", e):                    return "Supplier site down (5xx)"
        if BLOCK_RE.search(e):                            return f"{platform.title()} bot-wall — manual check needed"
        if any(w in e for w in ("connect", "timeout", "timed out", "resolve", "name or service", "network")):
            return "Network error — listing may have been removed"
        return f"Error: {error[:100]}"
    if available is False:
        return f"Listed on {platform} but Out of Stock"
    return ""


def retry_after_s(response):
    """Retry-After in seconds, from either form (delta-seconds or an HTTP date), or None. A date in the
    past gives 0."""
    raw = response.headers.get("retry-after", "").strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def item_changed(url, final_url, platform):
    """True when the link now leads to a different item than the sheet named (eBay item id, Amazon
    ASIN). Tracking parameters and redirects that keep the same item are not changes."""
    rx = {"ebay": EBAY_ITEM_RE, "amazon": AMAZON_ASIN_RE}.get(platform)
    if not rx or not final_url:
        return False
    wanted = rx.search(str(url))
    if not wanted:
        return False
    got = rx.search(str(final_url))
    return not got or got.group(1).lower() != wanted.group(1).lower()


def non_gbp(url, platform):
    """eBay and Amazon sites outside the UK show prices in another currency; those are not compared."""
    host = (urlsplit(str(url)).hostname or "").lower()
    if platform == "amazon":
        return not host.endswith("amazon.co.uk")
    if platform == "ebay":
        return not host.endswith("ebay.co.uk")
    return False


def none_source():
    """The slot shape for a sheet cell with no supplier URL. It never produces a flag."""
    return {"url": "", "host": "", "platform": "none", "verdict": "NONE", "reason": "", "title": None,
            "price_gbp": None, "available": None, "avail_text": "", "final_url": "", "http_status": None,
            "error": "No URL", "removal_comment": "No source URL in sheet", "variation_unverified": False,
            "currency": None, "fetched_at": None, "soft_block": False}


def error_source(url, message):
    """A raw source for a fetch that failed outside the normal path (was _sc_error_source)."""
    out = _raw_shell(url or "")
    out.update(error=message, verdict="ERROR", reason=str(message)[:120],
               removal_comment=f"Thread error: {message}", fetched_at=utc_now_iso())
    return out


def _raw_shell(url):
    return {"url": url, "host": host_of(url), "platform": detect_platform(url), "verdict": None,
            "reason": "", "price_gbp": None, "available": None, "avail_text": "", "title": None,
            "final_url": "", "http_status": None, "error": None, "removal_comment": "",
            "variation_unverified": False, "currency": None, "fetched_at": None, "soft_block": False}


# ── Supplier fetch ───────────────────────────────────────────────────────────

def _parse(platform, url, r):
    if platform == "ebay":
        return _ebay.extract(url, r.text)
    if platform == "amazon":
        return _amazon.extract(url, r.text)
    # All other platforms: generic best-effort extractor, fed raw bytes so CJK pages decode right.
    return _generic.extract(url, r.content, declared_charset=getattr(r, "charset_encoding", None))


def _get(client, pacer, url, host):
    pacer.wait(host)
    return client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)


def _status_outcome(platform, status):
    if status in (401, 403, 429) or (status == 503 and platform in ("amazon", "ebay")):
        return "blocked"
    if status >= 500:
        return "error"
    return "ok"


def _ebay_item_page(url, response):
    """True when an eBay 404/410 body is still the item's own page (a title, not "We looked everywhere")."""
    if not response.content:
        return False
    try:
        page = _ebay.extract(url, response.text)
    except Exception:  # noqa: BLE001 - unreadable: keep the plain 404
        return False
    return bool(page.title) and getattr(page, "state", "") != "removed"


def fetch_source(client, pacer, url) -> dict:
    """Fetch and read one supplier page. Raw fields only; never raises.

    One request (two for an eBay script-only shell), each through `pacer.wait` and `pacer.note`.
    """
    url = str(url or "").strip()
    if not url:
        return none_source()
    platform, host = detect_platform(url), host_of(url)
    sd = _raw_shell(url)
    noted = False
    resp = res = None
    retry_after = None
    gone_but_shown = False
    try:
        resp = _get(client, pacer, url, host)
        resp.raise_for_status()
    except UnsafeTarget:
        pacer.note(host, "error")
        return error_source(url, "Blocked: URL resolves to an internal address")
    except BotChallenge:
        pacer.note(host, "challenge")
        out = error_source(url, "The site asked for a bot check")
        out.update(verdict="BLOCKED", challenge=True)
        return out
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        retry_after = retry_after_s(exc.response) if status in (429, 503) else None
        pacer.note(host, _status_outcome(platform, status), retry_after)
        noted = True
        if platform == "ebay" and status in (404, 410) and _ebay_item_page(url, exc.response):
            # eBay answers 404 for an ended or sold-out listing but still sends the item page: read it
            # as a page that cannot be bought, not as a removed listing (2026-10-23).
            resp, gone_but_shown = exc.response, True
        else:
            sd.update(error=f"Fetch failed: {exc}", http_status=status)
            resp = None
    except Exception as exc:  # noqa: BLE001 - every network failure becomes a named outcome
        pacer.note(host, "error")
        noted = True
        sd.update(error=f"Fetch failed: {exc}", http_status=getattr(getattr(exc, "response", None),
                                                                      "status_code", None),
                  transport_error=isinstance(exc, httpx.TransportError))
        resp = None

    if resp is not None:
        try:
            res = _parse(platform, url, resp)
            if platform == "ebay" and getattr(res, "state", "") == "shell":
                # A script-only shell is often a transient answer: read it once more, through the
                # pacer, unless the shell just started a cool-down.
                pacer.note(host, "shell")
                if pacer.cooldown_until(host) is None:
                    try:
                        again = _get(client, pacer, url, host)
                        again.raise_for_status()
                        res2 = _parse(platform, url, again)
                    except UnsafeTarget:
                        pacer.note(host, "error")
                        noted = True
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        pacer.note(host, _status_outcome(platform, status),
                                   retry_after_s(exc.response) if status in (429, 503) else None)
                        noted = True
                    except Exception:  # noqa: BLE001 - keep the first answer
                        pacer.note(host, "error")
                        noted = True
                    else:
                        resp, res = again, res2
                else:
                    noted = True
            final_url = str(getattr(resp, "url", "") or "")
            if gone_but_shown:
                res.available = False
                res.availability_text = res.availability_text or "Ended or sold out (eBay answered 404)"
            sd.update(title=tq.clean_title(res.title), price_gbp=res.price_gbp, available=res.available,
                      avail_text=res.availability_text, error=res.error, final_url=final_url,
                      http_status=resp.status_code)
            if getattr(res, "offers", None):
                sd["offers"] = res.offers
            if getattr(res, "variation_label", None):
                sd["variation_label"] = res.variation_label
            state = getattr(res, "state", "")
            redirected = item_changed(url, final_url, platform)
            is_non_gbp = non_gbp(url, platform)
        except Exception as exc:  # noqa: BLE001
            sd.update(title=None, price_gbp=None, available=None, avail_text="", error=f"Parse error: {exc}")
            state, redirected, is_non_gbp = "", False, False
    else:
        state, redirected, is_non_gbp = "", False, False

    verdict, reason = outcomes.classify({**sd, "state": state, "redirected": redirected,
                                         "non_gbp": is_non_gbp})
    if platform in ("amazon", "ebay") and sd.get("http_status") == 503:
        verdict, reason = "BLOCKED", "HTTP 503, the site refused the request"
    if platform == "ebay" and state == "shell" and verdict == "NO_CONTENT":
        verdict, reason = "BLOCKED", "eBay shell page, no product content (soft block)"
        sd["soft_block"] = True
    sd["verdict"], sd["reason"] = verdict, reason
    if verdict in outcomes.FAILURE_VERDICTS and not sd.get("error"):
        sd["error"] = f"{verdict}: {reason}"
    # A price that is not in pounds, or belongs to a different item, is kept for display but must
    # never become a margin.
    if verdict == "NON_GBP":
        sd["price_native"], sd["price_gbp"] = sd.get("price_gbp"), None
        sd["currency"] = "OTHER"
    elif verdict in ("NOT_FOUND", "ENDED_REDIRECT") and sd.get("price_gbp") is not None:
        sd["price_seen"], sd["price_gbp"] = sd.get("price_gbp"), None
    if sd.get("price_gbp") is not None:
        sd["currency"] = "GBP"
    # A link that names a variation: the page's default price may not be that variation's price.
    sd["variation_unverified"] = (bool(_VARIATION_RE.search(url)) and verdict == "PRICE"
                                  and not sd.get("variation_label"))
    sd["removal_comment"] = removal_comment(sd.get("error"), sd.get("available"), platform, url, verdict)
    sd["fetched_at"] = utc_now_iso()
    if not noted:
        if sd["soft_block"]:
            pacer.note(host, "shell")
        elif verdict == "BLOCKED":
            pacer.note(host, "blocked")
        else:
            pacer.note(host, "ok")
    return sd


# ── Maskura ──────────────────────────────────────────────────────────────────

def _variant_available(v):
    if isinstance(v.get("available"), bool):
        return v["available"]
    inv = v.get("inventory_quantity")
    if inv is None:
        return True
    if isinstance(inv, (int, float)):
        return inv > 0
    return None


def _variants(product):
    out = []
    for v in product.get("variants") or []:
        try:
            price = float(v.get("price") or 0) or None
        except (TypeError, ValueError):
            price = None
        out.append({"id": str(v.get("id")) if v.get("id") is not None else None, "title": v.get("title"),
                    "price_gbp": price, "available": _variant_available(v)})
    return out


def _maskura_shell(url):
    info = normalize_maskura_url(url)
    return {"url": info["url"] or str(url or ""), "handle": info["handle"], "variant_id": info["variant_id"],
            "title": None, "price_gbp": None, "available": None, "variants": [], "status": None,
            "http_status": None, "error": None, "note": "", "fetched_at": None}


def _from_json(out, json_data):
    product = json_data.get("product", json_data) if isinstance(json_data, dict) else {}
    res = _maskura.extract(out["url"], "", {"product": product})
    out["variants"] = _variants(product)
    out["title"] = tq.clean_title(res.title)
    out["price_gbp"] = res.price_gbp
    out["available"] = res.available
    out["note"] = getattr(res, "note", "") or ""
    # The products.json catalog carries a per-variant "available" flag; prefer it when present.
    chosen = None
    if out["variant_id"]:
        chosen = next((v for v in product.get("variants") or [] if str(v.get("id")) == out["variant_id"]), None)
    elif product.get("variants"):
        chosen = product["variants"][0]
    if chosen is not None and isinstance(chosen.get("available"), bool) and res.error is None:
        out["available"] = chosen["available"]
    if res.error:
        out["error"], out["status"] = res.error, "VARIANT_NOT_FOUND"
    else:
        out["status"] = "OK"
    return out


def _maskura_status_for(status):
    if status == 404:
        return "NOT_FOUND"
    if status in (401, 403, 429) or status >= 500:
        return "BLOCKED"
    return "ERROR"


def _maskura_failure(out, pacer, host, exc):
    """A Maskura request that raised: a bot check is BLOCKED (with an immediate cool-down); a transport failure
    is marked so the worker asks the breaker before it writes anything."""
    if isinstance(exc, BotChallenge):
        pacer.note(host, "challenge")
        out.update(status="BLOCKED", challenge=True, error="Maskura asked for a bot check")
        return out
    pacer.note(host, "error")
    out.update(status="ERROR", error=f"Fetch failed: {exc}",
               transport_error=isinstance(exc, httpx.TransportError))
    return out


def _maskura_moved_to(client, pacer, out, host):
    """(new product URL or None, failure status or None) for a product whose `.json` gave 404.

    One request to the product page. Shopify forwards a renamed handle there; the link's variant is
    carried over. A forward to a page that is not a product (home page, collection) is not a move. A
    request that fails, is refused or rate limited gives a retryable failure status instead of an answer."""
    try:
        r = _get(client, pacer, out["url"], host)
    except BotChallenge:
        pacer.note(host, "challenge")
        return None, "BLOCKED"
    except Exception:  # noqa: BLE001 - unknown, not "gone"
        pacer.note(host, "error")
        return None, "ERROR"
    if r.status_code in (401, 403, 429) or r.status_code >= 500:
        status = _maskura_status_for(r.status_code)
        pacer.note(host, "blocked" if status == "BLOCKED" else "error",
                   retry_after_s(r) if r.status_code in (429, 503) else None)
        return None, status
    pacer.note(host, "ok")
    if r.status_code != 200:
        return None, None
    info = normalize_maskura_url(str(getattr(r, "url", "") or ""))
    if not info.get("ok") or not info.get("handle") or info["handle"] == out["handle"]:
        return None, None
    new = f"{MASKURA_BASE}/products/{info['handle']}"
    if out.get("variant_id"):
        new += f"?variant={out['variant_id']}"
    return new, None


def fetch_maskura(client, pacer, url, _followed=False) -> dict:
    """Read one Maskura product through its Shopify `.json`; never raises.

    status: OK, NOT_FOUND (404), BLOCKED (401, 403, 429 or 5xx; no HTML fallback), VARIANT_NOT_FOUND
    (the link's variant is not on the product), BAD_LINK (not a Maskura product URL) or ERROR. The
    product HTML is read only when the `.json` answered 200 with a body that is not JSON.
    """
    out = _maskura_shell(url)
    out["fetched_at"] = utc_now_iso()
    if not out["handle"]:
        out.update(status="BAD_LINK", error="Not a Maskura product link")
        return out
    host = host_of(out["url"])
    json_url = _maskura.shopify_json_url(out["url"])
    try:
        r = _get(client, pacer, json_url, host)
    except Exception as exc:  # noqa: BLE001
        return _maskura_failure(out, pacer, host, exc)
    out["http_status"] = r.status_code
    if r.status_code == 404 and not _followed:
        # Shopify forwards a renamed product's page (301) but not its .json, so a 404 here can be a
        # product that only moved (274 of 316 "broken" links on 2026-10-23).
        pacer.note(host, "ok")
        moved, failure = _maskura_moved_to(client, pacer, out, host)
        if failure:
            # The check itself failed: retry later rather than call a product gone that may only have moved.
            out.update(status=failure, error="HTTP 404 on the product data and the product page could not "
                                             "be checked for a move")
            return out
        if moved:
            new = fetch_maskura(client, pacer, moved, _followed=True)
            if new.get("status") != "NOT_FOUND":
                new["moved_to"] = moved
                new["note"] = "; ".join(x for x in (f"Maskura link moved to /products/{new['handle']}",
                                                    new.get("note")) if x)
                return new
        out.update(status="NOT_FOUND", error="HTTP 404 from the Maskura product page")
        return out
    if r.status_code >= 400:
        status = _maskura_status_for(r.status_code)
        pacer.note(host, "blocked" if status == "BLOCKED" else ("ok" if status == "NOT_FOUND" else "error"),
                   retry_after_s(r) if r.status_code in (429, 503) else None)
        out.update(status=status, error=f"HTTP {r.status_code} from the Maskura product page")
        return out
    json_data = None
    if "json" in r.headers.get("content-type", ""):
        try:
            json_data = r.json()
        except ValueError:
            json_data = None
    pacer.note(host, "ok")
    if json_data:
        return _from_json(out, json_data)
    # The .json answered 200 without JSON: read the product page once instead.
    try:
        r = _get(client, pacer, out["url"], host)
    except Exception as exc:  # noqa: BLE001
        return _maskura_failure(out, pacer, host, exc)
    out["http_status"] = r.status_code
    if r.status_code >= 400:
        status = _maskura_status_for(r.status_code)
        pacer.note(host, "blocked" if status == "BLOCKED" else ("ok" if status == "NOT_FOUND" else "error"),
                   retry_after_s(r) if r.status_code in (429, 503) else None)
        out.update(status=status, error=f"HTTP {r.status_code} from the Maskura product page")
        return out
    pacer.note(host, "ok")
    try:
        res = _maskura.extract(out["url"], r.text)
    except Exception as exc:  # noqa: BLE001
        out.update(status="ERROR", error=f"Parse error: {exc}")
        return out
    out.update(title=tq.clean_title(res.title), price_gbp=res.price_gbp, available=res.available,
               note=getattr(res, "note", "") or "")
    if res.error:
        out.update(status="ERROR", error=res.error)
    elif out["price_gbp"] is None:
        out.update(status="ERROR", error="No price on the Maskura product page")
    else:
        out["status"] = "OK"
    return out


def catalog_to_maskura(product_json, variant_id=None) -> dict:
    """The `fetch_maskura` shape for one product from `products.json` (no request made)."""
    product = product_json.get("product", product_json) if isinstance(product_json, dict) else {}
    out = _maskura_shell(maskura_url(product.get("handle") or "", variant_id))
    out["fetched_at"] = utc_now_iso()
    if not out["handle"]:
        out.update(status="BAD_LINK", error="The catalog product has no usable handle")
        return out
    return _from_json(out, {"product": product})


def fetch_catalog(client, pacer, *, page_size=250, max_pages=400) -> dict:
    """Every product from `/products.json`, page by page, each page through the pacer.

    Stops at the first empty page. A failed page, a body that is not JSON, or an empty first page is
    an error, and `complete` stays False."""
    host = host_of(MASKURA_BASE)
    products, pages = [], 0
    for page in range(1, max_pages + 1):
        url = f"{MASKURA_BASE}/products.json?limit={int(page_size)}&page={page}"
        try:
            r = _get(client, pacer, url, host)
        except Exception as exc:  # noqa: BLE001
            pacer.note(host, "error")
            return {"products": products, "pages": pages, "complete": False,
                    "error": f"Page {page}: fetch failed: {exc}"}
        if r.status_code != 200:
            outcome = "blocked" if r.status_code in (401, 403, 429) or r.status_code >= 500 else "error"
            pacer.note(host, outcome, retry_after_s(r) if r.status_code in (429, 503) else None)
            return {"products": products, "pages": pages, "complete": False,
                    "error": f"Page {page}: HTTP {r.status_code}"}
        try:
            batch = r.json().get("products")
        except (ValueError, AttributeError):
            batch = None
        if not isinstance(batch, list):
            pacer.note(host, "error")
            return {"products": products, "pages": pages, "complete": False,
                    "error": f"Page {page}: the body is not a product list"}
        pacer.note(host, "ok")
        pages += 1
        if not batch:
            if not products:
                return {"products": [], "pages": pages, "complete": False,
                        "error": "The catalog returned no products on page 1"}
            return {"products": products, "pages": pages, "complete": True, "error": None}
        products.extend(batch)
    return {"products": products, "pages": pages, "complete": False,
            "error": f"Stopped after {max_pages} pages without reaching the end"}


class SitemapError(RuntimeError):
    """The sitemap could not be read completely; never treat a partial set as the truth."""


_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_HANDLE_RE = re.compile(r"/products/([^/?#<\s]+)")


def _sitemap_get(client, pacer, url, host):
    try:
        r = _get(client, pacer, url, host)
    except Exception as exc:  # noqa: BLE001
        pacer.note(host, "error")
        raise SitemapError(f"{url}: fetch failed: {exc}") from exc
    if r.status_code != 200:
        pacer.note(host, "blocked" if r.status_code in (401, 403, 429) or r.status_code >= 500 else "error",
                   retry_after_s(r) if r.status_code in (429, 503) else None)
        raise SitemapError(f"{url}: HTTP {r.status_code}")
    pacer.note(host, "ok")
    return r.text


_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_IMAGE_LOC_RE = re.compile(r"<image:loc>\s*([^<\s]+)\s*</image:loc>", re.I)
_IMAGE_TITLE_RE = re.compile(r"<image:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</image:title>", re.S | re.I)


def fetch_sitemap_products(client, pacer) -> list:
    """[{handle, title}] for every product in `/sitemap.xml`'s `sitemap_products_*.xml` files. Shopify puts the
    product title in each product's <image:title>, so this lists the whole catalog without paging
    /products.json (which the store's bot check refuses after page 1). title is None when a product has no
    image. Raises SitemapError when any file cannot be read."""
    host = host_of(MASKURA_BASE)
    index = _sitemap_get(client, pacer, f"{MASKURA_BASE}/sitemap.xml", host)
    children = [u.replace("&amp;", "&") for u in _LOC_RE.findall(index)
                if "sitemap_products_" in u and host_of(u) in MASKURA_HOSTS]
    if not children:
        raise SitemapError("The sitemap lists no product sitemaps")
    out, seen = [], set()
    for child in children:
        for block in _URL_BLOCK_RE.findall(_sitemap_get(client, pacer, child, host)):
            loc = _LOC_RE.search(block)
            m = _HANDLE_RE.search(loc.group(1)) if loc else None
            if not m or host_of(loc.group(1)) not in MASKURA_HOSTS:
                continue
            handle = m.group(1).lower()
            if handle in seen:
                continue
            seen.add(handle)
            t = _IMAGE_TITLE_RE.search(block)
            img = _IMAGE_LOC_RE.search(block)
            image = html.unescape(img.group(1)) if img else None
            out.append({"handle": handle, "title": html.unescape(t.group(1)).strip() if t else None,
                        # Only Shopify's CDN: the page shows no image from anywhere else (DESIGN.md Section 10.1).
                        "image": image if image and image.startswith("https://cdn.shopify.com/") else None})
    return out


def fetch_sitemap_handles(client, pacer) -> set:
    """Product handles from `/sitemap.xml` and each `sitemap_products_*.xml` it lists. Raises
    SitemapError when any file cannot be read."""
    host = host_of(MASKURA_BASE)
    index = _sitemap_get(client, pacer, f"{MASKURA_BASE}/sitemap.xml", host)
    children = [u.replace("&amp;", "&") for u in _LOC_RE.findall(index)
                if "sitemap_products_" in u and host_of(u) in MASKURA_HOSTS]
    if not children:
        raise SitemapError("The sitemap lists no product sitemaps")
    handles = set()
    for child in children:
        for loc in _LOC_RE.findall(_sitemap_get(client, pacer, child, host)):
            m = _HANDLE_RE.search(loc)
            if m and host_of(loc) in MASKURA_HOSTS:
                handles.add(m.group(1).lower())
    return handles


# ── Variant check (moved from app.py) ────────────────────────────────────────

_VARIANT_SIZE_RE = re.compile(
    r'\b(\d+(?:[.,]\d+)?)[- ]*'
    r'(?:pack|pcs?|pieces?|pairs?|sets?|kg|g\b|oz|lb|lbs?|l\b|ml|cl|'
    r'cm|mm|m\b|ft|inch(?:es)?|in\b|yards?|w\b|kw|v\b|hz|mah|ah)\b',
    re.I,
)
# "7-in-1", "6 in 1" combo counts; "x2" / "2x" multipliers.
_VARIANT_COMBO_RE = re.compile(
    r'\b(\d+)\s*[-]?\s*in\s*[-]?\s*1\b'  # N-in-1
    r'|(?<!\w)[xX](\d+)\b'               # x2 prefix
    r'|\b(\d+)[xX](?!\w)',               # 2x suffix
    re.I,
)
_VARIANT_WORD_RE = re.compile(
    r'\b(small|medium|large|xl|xxl|xs|xxs|single|double|king|queen|twin|'
    r'full|plus|mini|micro|nano|maxi|junior|toddler|adult|mens?|womens?|'
    r'pro|max|lite|ultra|basic|standard|premium|deluxe|'
    r'black|white|red|blue|green|pink|grey|gray|silver|gold|navy|'
    r'brown|purple|yellow|orange|beige|cream|clear|transparent|'
    r'cotton|polyester|fleece|wool|silk|nylon|rubber|leather|metal|'
    r'plastic|wood|bamboo|stainless|chrome|matte|gloss)\b',
    re.I,
)
# Following words that cancel an ambiguous size token (bedding words used as adjectives).
_VARIANT_CONTEXT_BLOCKS: dict = {
    "full":   {"body", "coverage", "grain", "moon", "face", "hd"},
    "double": {"chin", "sided", "dutch", "blind", "decker", "action"},
    "single": {"use", "sided", "colour", "color"},
    "twin":   {"engine", "turbo", "screw"},
}
# Trailing platform attribution to strip before tokenizing source titles.
_PLATFORM_SUFFIX_RE = re.compile(
    r'\s*[|:]\s*(?:ebay|amazon|etsy|argos|temu|aliexpress|wish)\b.*$',
    re.I,
)


def strip_platform_suffix(title: str) -> str:
    return _PLATFORM_SUFFIX_RE.sub("", title).strip()


def variant_tokens(title: str) -> set:
    tokens: set = set()
    norm = title.lower()
    for m in _VARIANT_SIZE_RE.finditer(title):
        tokens.add(re.sub(r'[\s-]+', '', m.group(0).lower()))
    for m in _VARIANT_COMBO_RE.finditer(title):
        raw = re.sub(r'[\s-]+', '', m.group(0).lower())
        if re.fullmatch(r'\d+x', raw):   # "2x" -> "x2"
            raw = 'x' + raw[:-1]
        tokens.add(raw)
    for m in _VARIANT_WORD_RE.finditer(title):
        word = m.group(1).lower()
        blockers = _VARIANT_CONTEXT_BLOCKS.get(word)
        if blockers:
            following = re.findall(r'\b\w+\b', norm[m.end():][:40])[:2]
            if any(w in blockers for w in following):
                continue
        tokens.add(word)
    return tokens


def variant_match(maskura_title: str, source_title: str) -> list:
    """Sorted tokens in the Maskura title missing from the source title. Empty = compatible."""
    if not maskura_title or not source_title:
        return []
    m_tok = variant_tokens(strip_platform_suffix(maskura_title))
    if not m_tok:
        return []
    s_tok = variant_tokens(strip_platform_suffix(source_title))
    return sorted(m_tok - s_tok)


# ── Finalize ─────────────────────────────────────────────────────────────────

# Every suffix finalize_product can emit (codes are S{i}_<suffix>, plus "M_ERR").
EMITTED_SUFFIXES = ("OOS", "ERR", "INVERTED", "VARIANT", "PRICE_SUSPECT", "VARIATION", "CURRENCY", "GONE",
                    "PENDING", "BLOCKED_RETRY", "M_ERR")
DERIVED_FIELDS = ("price_diff", "pct_vs_source", "variant_mismatch", "variant_mismatch_tokens",
                  "price_suspect", "notes")
# Worst first; used for worst_verdict.
VERDICT_ORDER = ("NOT_FOUND", "ENDED_REDIRECT", "OUT_OF_STOCK", "BLOCKED", "ERROR", "NO_CONTENT",
                 "PARSER_GAP", "NON_GBP", "PRICE")
_RETRYABLE_MASKURA = ("ERROR", "BLOCKED")


def _is_no_url(sd):
    return sd.get("verdict") == "NONE" or (not sd.get("url") and sd.get("platform") in (None, "", "none"))


def finalize_product(maskura, sources) -> dict:
    """Derived fields and flags for one product, from its raw Maskura and source results. Pure.

    `sources` is aligned to slots (index 0 is slot 1). Each entry is a raw fetch dict, a no-URL slot
    (`verdict="NONE"`, no flag), or None (not fetched yet: S{i}_PENDING). A raw dict with a truthy
    `retry_pending` (set by the worker when it will try again) gives S{i}_BLOCKED_RETRY for a BLOCKED
    verdict and S{i}_PENDING otherwise, and no final flags for that slot.
    """
    m = maskura or {}
    maskura_price = m.get("price_gbp")
    maskura_title = m.get("title") or ""
    out, flags = [], []
    being_checked = False
    verdicts = []
    for i, raw in enumerate(sources or [], 1):
        if raw is None:
            flags.append(f"S{i}_PENDING")
            being_checked = True
            out.append(None)
            continue
        sd = dict(raw)
        if _is_no_url(sd):
            sd.update({k: None for k in DERIVED_FIELDS})
            out.append(sd)
            continue
        if sd.get("variation_unverified") is None:
            sd["variation_unverified"] = (bool(_VARIATION_RE.search(str(sd.get("url") or "")))
                                          and sd.get("verdict") == "PRICE" and not sd.get("variation_label"))
        if sd.get("removal_comment") is None:
            sd["removal_comment"] = removal_comment(sd.get("error"), sd.get("available"),
                                                    sd.get("platform") or "generic", str(sd.get("url") or ""),
                                                    sd.get("verdict") or "")
        if maskura_price and sd.get("price_gbp"):
            mp, sp = float(maskura_price), float(sd["price_gbp"])
            sd["price_diff"] = round(mp - sp, 2)
            sd["pct_vs_source"] = round((mp - sp) / sp * 100, 1) if sp else None
        else:
            sd["price_diff"] = None
            sd["pct_vs_source"] = None
        sd["price_suspect"] = tq.margin_suspect(maskura_price, sd.get("price_gbp"))
        src_title = sd.get("title") or ""
        tokens = variant_match(maskura_title, src_title) if src_title else []
        sd["variant_mismatch_tokens"] = tokens
        sd["variant_mismatch"] = bool(tokens)
        sd["notes"] = build_notes(sd.get("error"), sd.get("available"), sd.get("price_gbp"),
                                  sd.get("removal_comment"))
        out.append(sd)

        verdict = sd.get("verdict")
        if not verdict or verdict == "PENDING":
            flags.append(f"S{i}_PENDING")
            being_checked = True
            continue
        if sd.get("retry_pending"):
            flags.append(f"S{i}_BLOCKED_RETRY" if verdict == "BLOCKED" else f"S{i}_PENDING")
            being_checked = True
            continue
        verdicts.append(verdict)
        if sd.get("available") is False:                  flags.append(f"S{i}_OOS")
        # "Could not read" only when no price was read and nothing else explains why. A page with a price
        # and a "verify manually" note, a removed listing, an out-of-stock page and a non-pound price are
        # not read failures (lead decision 2026-10-16).
        explained = (sd.get("available") is False or verdict in ("NOT_FOUND", "ENDED_REDIRECT", "NON_GBP"))
        if (sd.get("error") and sd["error"] != "No URL" and sd.get("price_gbp") is None
                and not explained):                       flags.append(f"S{i}_ERR")
        d = sd.get("price_diff")
        if d is not None and d < 0:                       flags.append(f"S{i}_INVERTED")
        if sd.get("variant_mismatch"):                    flags.append(f"S{i}_VARIANT")
        if sd.get("price_suspect"):                       flags.append(f"S{i}_PRICE_SUSPECT")
        if sd.get("variation_unverified"):                flags.append(f"S{i}_VARIATION")
        if verdict == "NON_GBP":                          flags.append(f"S{i}_CURRENCY")
        if verdict in ("NOT_FOUND", "ENDED_REDIRECT"):    flags.append(f"S{i}_GONE")

    if maskura is not None and m.get("status") in _RETRYABLE_MASKURA:
        if m.get("retry_pending"):
            being_checked = True
        else:
            flags.append("M_ERR")

    rank = {v: n for n, v in enumerate(VERDICT_ORDER)}
    worst = min(verdicts, key=lambda v: rank.get(v, len(rank))) if verdicts else "NONE"
    status, best, notes = decide_product(maskura, sources)
    return {"sources": out, "flags": _flags_for(status, best, flags), "worst_verdict": worst,
            "being_checked": being_checked, "status": status, "best_source": best, "notes": notes}


# ── Decision (owner's rule, 2026-10-23) ──────────────────────────────────────
# Read the Maskura price, read the source price of the same variant; a buyable source priced strictly
# below Maskura is OK and gets no flag. Only these statuses are problems (sc_store.PROBLEM_STATUSES).
_NEEDS_SOURCE_SUFFIXES = ("GONE", "OOS", "ERR", "CURRENCY", "BLOCKED_RETRY")


def _buyable(sd):
    return (sd is not None and not _is_no_url(sd) and sd.get("verdict") == "PRICE"
            and sd.get("available") is not False and bool(sd.get("price_gbp")))


def decide_product(maskura, sources):
    """(status, best source slot or None, notes) for one product. Pure.

    status: OK, SOURCE_DEARER, VARIANT_UNCLEAR, NO_BUYABLE_SOURCE, MASKURA_GONE, MASKURA_UNREADABLE,
    NO_MASKURA_LINK, NO_SOURCE_LINK or CHECKING."""
    from . import sc_variants
    notes = []
    sources = list(sources or [])
    # Empty slots are stored with no verdict: skip them before looking for unfinished ones.
    if any(s is None or (not _is_no_url(s) and (s.get("verdict") in (None, "PENDING") or s.get("retry_pending")))
           for s in sources):
        return "CHECKING", None, notes
    if maskura is None:
        return "NO_MASKURA_LINK", None, notes
    if maskura.get("retry_pending"):
        return "CHECKING", None, notes
    if maskura.get("status") == "NOT_FOUND":
        return "MASKURA_GONE", None, notes
    if maskura.get("status") != "OK" or not maskura.get("price_gbp"):
        return "MASKURA_UNREADABLE", None, notes
    if maskura.get("moved_to"):
        notes.append("Maskura link moved; update the sheet")
    linked = [(i, s) for i, s in enumerate(sources, 1) if not _is_no_url(s)]
    if not linked:
        return "NO_SOURCE_LINK", None, notes
    best = None
    unclear = []   # sources whose variations straddle Maskura's price (sc_variants.variation_range)
    for i, s in linked:
        if not _buyable(s):
            continue
        if s.get("offers") and not s.get("variation_label"):
            notes.append(f"Source {i} has variations; the link does not name one")
        vr = sc_variants.variation_range(maskura, s)
        if vr is not None:
            if vr["result"] == "unclear":
                unclear.append(i)
                continue
            margin = vr["margin"]
        else:
            mp, how = sc_variants.compare_price(maskura, s)
            if mp is None:
                continue
            if how == "cheapest":
                notes.append(f"Source {i}: variant not named, compared with the cheapest Maskura variant")
            margin = round(mp - float(s["price_gbp"]), 2)
        if best is None or margin > best[1]:
            best = (i, margin)
    if (best is None or best[1] <= 0) and unclear:
        return "VARIANT_UNCLEAR", unclear[0], notes
    if best is None:
        return "NO_BUYABLE_SOURCE", None, notes
    if best[1] <= 0:
        return "SOURCE_DEARER", best[0], notes
    notes += [f"Backup source not buyable (Source {i})" for i, s in linked if i != best[0] and not _buyable(s)]
    return "OK", best[0], notes


def _flags_for(status, best, flags):
    """Only the flags that explain the decision; an OK product has none."""
    if status == "CHECKING":
        return flags
    if status == "SOURCE_DEARER":
        return [f"S{best}_INVERTED"] + [f for f in flags if f == f"S{best}_PRICE_SUSPECT"]
    if status == "VARIANT_UNCLEAR":
        return [f"S{best}_VARIANT_UNCLEAR"]
    if status == "NO_BUYABLE_SOURCE":
        return [f for f in flags if f.startswith("S") and f.split("_", 1)[1] in _NEEDS_SOURCE_SUFFIXES]
    if status == "MASKURA_UNREADABLE":
        return [f for f in flags if f == "M_ERR"]
    return []
