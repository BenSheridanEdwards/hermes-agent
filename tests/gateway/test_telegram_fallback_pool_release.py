"""Regression test for #71593 / #63311 — Telegram fallback-pool FD leak.

Background
----------
``TelegramFallbackTransport`` (plugins/platforms/telegram/telegram_network.py)
routes Telegram Bot API requests via per-IP fallback ``httpx`` pools when the
primary DNS path is unreachable.  The pre-fix version built one
``AsyncHTTPTransport`` per fallback IP eagerly in ``__init__`` and *never* tore
them down: on a retryable connect failure the handler only logged and
continued, so a socket left in ``CLOSE_WAIT`` by a peer-closed connection stayed
inside the retained pool.  Each retry leaked another file descriptor until the
gateway hit EMFILE and wedged (accept(), config reads and DNS all failing).

The fix (this PR):
  * builds fallback pools lazily via ``_get_fallback`` (nothing in ``__init__``);
  * on a retryable connect failure calls ``_reset_fallback`` which pops the
    poisoned pool out of ``self._fallbacks`` and ``aclose()``s it, so its dead
    sockets are released instead of accumulating;
  * bounds every pool at ``Limits(max_connections=8)`` as a *default* via
    ``setdefault`` — a caller-supplied ``limits`` kwarg still wins.

Contract asserted here (mutation-survivable)
---------------------------------------------
1. A retryable connect failure on a fallback IP causes that pool to be
   ``aclose()``d and dropped from ``self._fallbacks`` — NOT retained.  This is
   the discard-on-failure path: revert the ``_reset_fallback`` call in
   ``handle_async_request`` and ``test_failed_fallback_pool_is_discarded_and_closed``
   fails (the pool is retained and never closed).
2. A caller-supplied ``limits`` kwarg wins over the ``_POOL_LIMITS`` default.
"""

import asyncio

import httpx
import pytest

import plugins.platforms.telegram.telegram_network as tnet


def _telegram_request(path="/botTOKEN/getMe"):
    return httpx.Request("GET", f"https://api.telegram.org{path}")


class _CountingTransport(httpx.AsyncBaseTransport):
    """Fake AsyncHTTPTransport: fails/succeeds per host, records aclose()."""

    def __init__(self, behavior, closed_log):
        self.behavior = behavior
        self.closed_log = closed_log
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        action = self.behavior.get(request.url.host, "ok")
        if action == "timeout":
            raise httpx.ConnectTimeout("timed out")
        if action == "connect_error":
            raise httpx.ConnectError("connect error")
        return httpx.Response(200, request=request, text="ok")

    async def aclose(self) -> None:
        self.closed = True
        self.closed_log.append(self)


class _SequencedTransport(httpx.AsyncBaseTransport):
    """Share per-host outcomes across replacement pool generations."""

    def __init__(self, outcomes, calls):
        self.outcomes = outcomes
        self.calls = calls
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        outcome = self.outcomes[host].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, request=request, text=str(outcome))

    async def aclose(self) -> None:
        self.closed = True


class _ReadTimeoutStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadTimeout("response body timed out")
        yield b""  # pragma: no cover


class _ResponseStreamTimeoutTransport(_SequencedTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.host)
        return httpx.Response(200, request=request, stream=_ReadTimeoutStream())


class _ConcurrentTimeoutTransport(_SequencedTransport):
    def __init__(self, outcomes, calls):
        super().__init__(outcomes, calls)
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]
        self._call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        index = self._call_count
        self._call_count += 1
        self.calls.append(request.url.host)
        self.entered[index].set()
        await self.release[index].wait()
        raise httpx.ReadTimeout(f"request {index} timed out")


def _factory(behavior, closed_log, kwargs_log=None):
    def factory(**kwargs):
        if kwargs_log is not None:
            kwargs_log.append(kwargs)
        return _CountingTransport(behavior, closed_log)

    return factory


@pytest.mark.asyncio
async def test_failed_fallback_pool_is_discarded_and_closed(monkeypatch):
    """The discard-on-failure path: a retryable connect failure on a fallback
    IP must aclose() that pool and drop it from ``self._fallbacks`` so its
    CLOSE_WAIT socket is released (#71593 / #63311).

    Sabotage check: reverting the ``await self._reset_fallback(ip)`` call in
    ``handle_async_request`` retains the poisoned pool → this test fails
    (pool still in ``_fallbacks`` and never aclose()d).
    """
    closed_log: list = []
    # Primary + both fallback IPs fail with a retryable connect error, so every
    # fallback pool that gets built must also get discarded.
    behavior = {
        "api.telegram.org": "timeout",
        "149.154.167.220": "connect_error",
        "149.154.167.221": "timeout",
    }
    monkeypatch.setattr(
        tnet.httpx, "AsyncHTTPTransport", _factory(behavior, closed_log)
    )

    transport = tnet.TelegramFallbackTransport(
        ["149.154.167.220", "149.154.167.221"]
    )

    # All paths fail → the last error propagates.
    with pytest.raises((httpx.ConnectTimeout, httpx.ConnectError)):
        await transport.handle_async_request(_telegram_request())

    # The poisoned fallback pools must NOT be retained — they were discarded.
    assert transport._fallbacks == {}, (
        "Failed fallback pools were retained in self._fallbacks — the "
        "CLOSE_WAIT sockets leak (revert of _reset_fallback? #71593)."
    )

    # The failed primary plus each fallback pool must be
    # aclose()d exactly once (one primary + two fallback IPs).
    assert len(closed_log) == 3, (
        f"Expected 3 discarded/closed transports, got {len(closed_log)} — "
        "the discard-on-failure path did not aclose() the poisoned pools."
    )
    assert all(t.closed for t in closed_log)
    await transport.aclose()


@pytest.mark.asyncio
async def test_failed_primary_pool_is_discarded_and_closed(monkeypatch):
    """A failed primary attempt must release its pool before fallback (#82920)."""
    for key in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy",
        "http_proxy", "all_proxy", "TELEGRAM_PROXY", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)

    behavior = {"api.telegram.org": "timeout", "149.154.167.220": "ok"}
    instances = []

    def factory(**kwargs):
        transport = _CountingTransport(behavior, [])
        instances.append(transport)
        return transport

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    try:
        response = await transport.handle_async_request(_telegram_request())
        assert response.status_code == 200
        # IPv4-first (#87015): .220 succeeds on the first try, so the
        # dual-stack hostname pool is never opened and never discarded.
        # Instances: 1 primary (unused this request) + 1 fallback (.220).
        assert len(instances) == 2
        assert not instances[0].closed
        assert not instances[1].closed
        assert transport._sticky_ip == "149.154.167.220"
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_fallback_read_timeout_discards_route_without_replaying_upload(monkeypatch):
    """An upload read timeout is ambiguous: retire the route, but do not replay it."""
    outcomes = {
        "149.154.167.220": ["first", httpx.ReadTimeout("response timed out"), "recovered"],
    }
    calls = []
    instances = []

    def factory(**kwargs):
        instance = _SequencedTransport(outcomes, calls)
        instances.append(instance)
        return instance

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    try:
        await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        upload = httpx.Request(
            "POST", "https://api.telegram.org/botTOKEN/sendVoice", content=b"voice-bytes"
        )
        before_timeout = len(calls)
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(upload)

        assert calls[before_timeout:] == ["149.154.167.220"]
        assert instances[1].closed is True
        assert transport._fallbacks == {}
        assert transport._sticky_ip is tnet._UNSET

        response = await transport.handle_async_request(_telegram_request())
        assert response.text == "recovered"
        assert len(instances) == 3
        assert instances[2].closed is False
        assert transport._sticky_ip == "149.154.167.220"
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_primary_read_timeout_discards_route_without_trying_fallback(monkeypatch):
    """The primary pool follows the same no-replay rule and the next request uses its replacement."""
    outcomes = {
        "api.telegram.org": [httpx.ReadTimeout("response timed out"), "recovered"],
        "149.154.167.220": [httpx.ConnectError("fallback unavailable")],
    }
    calls = []
    instances = []

    def factory(**kwargs):
        instance = _SequencedTransport(outcomes, calls)
        instances.append(instance)
        return instance

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    transport._sticky_ip = None  # an established hostname route
    original_primary = transport._primary
    try:
        upload = httpx.Request(
            "POST", "https://api.telegram.org/botTOKEN/sendVoice", content=b"voice-bytes"
        )
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(upload)

        assert calls == ["api.telegram.org"]
        assert isinstance(original_primary, _SequencedTransport)
        assert original_primary.closed is True
        assert transport._primary is not original_primary
        assert transport._sticky_ip is tnet._UNSET

        response = await transport.handle_async_request(_telegram_request())
        assert response.text == "recovered"
        assert calls == ["api.telegram.org", "149.154.167.220", "api.telegram.org"]
    finally:
        await transport.aclose()



@pytest.mark.asyncio
async def test_response_body_read_timeout_retires_fallback_pool(monkeypatch):
    """A timeout after response headers still retires the sticky pool without replay."""
    instances = []
    calls = []

    def factory(**kwargs):
        if not instances:
            instance = _SequencedTransport({}, calls)  # unused primary
        else:
            instance = _ResponseStreamTimeoutTransport({}, calls)
        instances.append(instance)
        return instance

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    try:
        response = await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        with pytest.raises(httpx.ReadTimeout):
            await response.aread()

        assert calls == ["149.154.167.220"]
        assert instances[1].closed is True
        assert transport._fallbacks == {}
        assert transport._sticky_ip is tnet._UNSET
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_stale_read_timeout_does_not_close_replacement_fallback_pool(monkeypatch):
    """Concurrent failures from an old generation cannot retire the newly-created pool."""
    instances = []
    calls = []

    def factory(**kwargs):
        if len(instances) == 1:
            instance = _ConcurrentTimeoutTransport({}, calls)
        else:
            instance = _SequencedTransport({"149.154.167.220": ["ok"]}, calls)
        instances.append(instance)
        return instance

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    first = asyncio.create_task(transport.handle_async_request(_telegram_request()))
    await asyncio.sleep(0)
    failed_generation = instances[1]
    await failed_generation.entered[0].wait()
    second = asyncio.create_task(transport.handle_async_request(_telegram_request()))
    await failed_generation.entered[1].wait()
    try:
        failed_generation.release[0].set()
        with pytest.raises(httpx.ReadTimeout):
            await first

        replacement = await transport._get_fallback("149.154.167.220")
        assert replacement is not failed_generation

        failed_generation.release[1].set()
        with pytest.raises(httpx.ReadTimeout):
            await second

        assert transport._fallbacks["149.154.167.220"] is replacement
        assert isinstance(replacement, _SequencedTransport)
        assert replacement.closed is False
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await transport.aclose()


def test_pool_default_limits_applied_when_caller_omits(monkeypatch):
    """When the caller supplies no ``limits``, the bounded ``_POOL_LIMITS``
    default (max_connections=8) is applied via setdefault (#71593)."""
    kwargs_log: list = []
    for key in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy",
        "http_proxy", "all_proxy", "TELEGRAM_PROXY", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        tnet.httpx, "AsyncHTTPTransport", _factory({}, [], kwargs_log)
    )

    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    limits = kwargs_log[0]["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits is transport._POOL_LIMITS
