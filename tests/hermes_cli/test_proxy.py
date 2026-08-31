"""Tests for the `hermes proxy` subcommand and its upstream adapters."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter, XAIGrokComposerAdapter


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# NousPortalAdapter
# ---------------------------------------------------------------------------


def _write_auth_store(hermes_home: Path, nous_state: Dict[str, Any]) -> Path:
    """Write an auth.json with the given nous state into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {"nous": nous_state},
    }))
    return auth_path




def test_nous_adapter_concurrent_refresh_serialized(tmp_path, monkeypatch):
    """Two parallel get_credential() calls must serialize through the lock."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "a", "refresh_token": "r",
    })

    call_log: list = []
    in_flight = threading.Event()
    overlap_detected = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    def serializing_refresh(**kwargs):
        # If another thread is already inside refresh, the lock is broken.
        if in_flight.is_set():
            overlap_detected.set()
        in_flight.set()
        try:
            call_log.append(threading.current_thread().ident)
            # Simulate refresh latency so any race window is exposed.
            import time
            time.sleep(0.05)
            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            return {
                "api_key": f"key-{idx}",
                "expires_at": "2099-01-01T00:00:00Z",
                "base_url": "https://inference-api.nousresearch.com/v1",
            }
        finally:
            in_flight.clear()

    adapter = NousPortalAdapter()
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(adapter.get_credential().bearer)
        except Exception as exc:  # pragma: no cover - shouldn't happen
            errors.append(exc)

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=serializing_refresh,
    ):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"workers errored: {errors}"
    assert len(results) == 3
    assert len(call_log) == 3
    assert not overlap_detected.is_set(), "refresh calls overlapped — lock is broken"
    assert all(r.startswith("key-") for r in results)


# ---------------------------------------------------------------------------
# XAIGrokAdapter
# ---------------------------------------------------------------------------


def _write_xai_pool_entry(
    hermes_home: Path,
    *,
    access_token: str = "xai-access-token",
    refresh_token: str = "xai-refresh-token",
    base_url: str = "https://api.x.ai/v1",
    source: str = "manual:xai_pkce",
) -> Path:
    """Write an xai-oauth pool entry into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai123",
                    "label": "xai-test",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": source,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "base_url": base_url,
                }
            ]
        },
    }))
    return auth_path


def _configure_external_oauth_owner(hermes_home: Path) -> None:
    (hermes_home / "config.yaml").write_text(
        "oauth:\n  refresh_owner: external\n",
        encoding="utf-8",
    )


def test_xai_adapter_not_authenticated_when_no_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {},
    }))
    assert not XAIGrokAdapter().is_authenticated()


def test_xai_adapter_retry_rotates_pool_entry_on_429(tmp_path, monkeypatch):
    """429 from xAI must rotate to the next pool entry, not attempt refresh.

    Pre-fix (#28932) ``get_retry_credential`` only fired on 401, so a 429
    rate-limit response flowed back to the client unchanged AND the
    rate-limited bearer stayed active for the next request — defeating
    the whole point of pool rotation.

    Post-fix: 429 lands on ``mark_exhausted_and_rotate`` (no refresh —
    that's irrelevant for rate limits), stamps the 1-hour cooldown
    via ``EXHAUSTED_TTL_429_SECONDS`` on the offending key, and
    returns the next available credential.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Two pool entries so rotation has somewhere to go.
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai-first",
                    "label": "xai-first",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": "manual:xai_pkce",
                    "access_token": "first-access-token",
                    "refresh_token": "first-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
                {
                    "id": "xai-second",
                    "label": "xai-second",
                    "auth_type": "oauth",
                    "priority": 1,
                    "source": "manual:xai_pkce",
                    "access_token": "second-access-token",
                    "refresh_token": "second-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
            ]
        },
    }))

    # Refresh must NOT be called on the 429 path — guard against
    # the fix accidentally trying to refresh-on-rate-limit.
    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on 429")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    assert failed.bearer == "first-access-token", "starting bearer should be the first entry"

    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=429,
    )

    assert retry is not None, "429 must rotate to next pool entry"
    assert retry.bearer == "second-access-token", (
        f"expected rotation to second entry, got {retry.bearer!r}"
    )


# ---------------------------------------------------------------------------
# Server: path filtering + forwarding
#
# We run the proxy AND a fake upstream as real aiohttp servers on ephemeral
# ports. Avoids pytest-aiohttp's fixtures (extra dependency for one test file).
# ---------------------------------------------------------------------------

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from hermes_cli.proxy.server import create_app  # noqa: E402


class FakeAdapter(UpstreamAdapter):
    """A test adapter that returns a fixed credential without touching disk."""

    def __init__(self, base_url: str, bearer: str = "test-bearer",
                 allowed=None, raise_on_credential=False,
                 retry_bearer: str | None = None):
        self._base_url = base_url
        self._bearer = bearer
        self._allowed = frozenset(allowed or ["/chat/completions"])
        self._raise = raise_on_credential
        self._retry_bearer = retry_bearer
        self.calls = 0
        self.retry_calls = 0

    @property
    def name(self): return "fake"

    @property
    def display_name(self): return "Fake Provider"

    @property
    def allowed_paths(self): return self._allowed

    def is_authenticated(self): return True

    def get_credential(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("simulated auth failure")
        return UpstreamCredential(
            bearer=self._bearer, base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )

    def get_retry_credential(self, *, failed_credential, status_code):
        _ = failed_credential
        self.retry_calls += 1
        if status_code != 401 or not self._retry_bearer:
            return None
        return UpstreamCredential(
            bearer=self._retry_bearer,
            base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )


async def _start_runner(app: "web.Application"):
    """Spin up an aiohttp app on an ephemeral localhost port. Returns (runner, base_url)."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    sockets = list(site._server.sockets)  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _build_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def echo(request):
        body = await request.read()
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": request.headers.get("Authorization"),
            "headers": {key.lower(): value for key, value in request.headers.items()},
            "path_qs": request.path_qs,
            "body": body.decode("utf-8") if body else "",
        })
        return web.json_response({"echoed": True, "path": request.path})

    async def sse(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        for chunk in [b"data: hello\n\n", b"data: world\n\n", b"data: [DONE]\n\n"]:
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", echo)
    app.router.add_route("*", "/v1/responses", echo)
    app.router.add_route("*", "/v1/embeddings", echo)
    app.router.add_route("*", "/v1/sse", sse)
    return app


def _build_retrying_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def maybe_unauthorized(request):
        body = await request.read()
        auth = request.headers.get("Authorization")
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": auth,
            "body": body.decode("utf-8") if body else "",
        })
        if auth == "Bearer jwt-bearer":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", maybe_unauthorized)
    return app






def test_server_strips_client_auth_header():
    """The client's Authorization header MUST NOT reach the upstream."""
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", bearer="ours")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={},
                    headers={"Authorization": "Bearer SHOULD_NOT_LEAK"},
                ) as resp:
                    await resp.read()
            assert captured["requests"][0]["auth"] == "Bearer ours"
            assert "SHOULD_NOT_LEAK" not in captured["requests"][0]["auth"]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_xai_composer_forwards_only_locked_responses_route(
    tmp_path, monkeypatch, caplog
):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _configure_external_oauth_owner(tmp_path)
        _write_xai_pool_entry(tmp_path, access_token="fleet-token-v1")

        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        monkeypatch.setattr(
            "hermes_cli.proxy.adapters.xai._COMPOSER_BASE_URL",
            f"{upstream_base}/v1",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(XAIGrokComposerAdapter()))
        body = json.dumps(
            {"model": "grok-composer-2.5", "input": "BODY-SECRET-MARKER"}
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/responses?trace=test",
                    data=body,
                    headers={
                        "Authorization": "Bearer client-dummy",
                        "Content-Type": "application/json",
                        "User-Agent": "conflicting-client",
                        "X-Grok-Client-Version": "999.0.0",
                        "X-Grok-Client-Identifier": "conflicting-client",
                        "X-XAI-Token-Auth": "conflicting-auth-mode",
                        "X-Grok-Model-Override": "conflicting-model",
                    },
                ) as resp:
                    assert resp.status == 200
                    await resp.read()
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

        assert len(captured["requests"]) == 1
        request = captured["requests"][0]
        assert request["path"] == "/v1/responses"
        assert request["path_qs"] == "/v1/responses?trace=test"
        assert request["body"] == body
        assert request["auth"] == "Bearer fleet-token-v1"
        headers = request["headers"]
        assert headers["user-agent"] == "Grok/0.2.117"
        assert headers["x-grok-client-version"] == "0.2.117"
        assert headers["x-grok-client-identifier"] == "grok-shell"
        assert headers["x-xai-token-auth"] == "xai-grok-cli"
        assert headers["x-grok-model-override"] == "grok-composer-2.5"
        assert "client-dummy" not in headers.values()
        assert "conflicting-model" not in headers.values()

    with caplog.at_level("DEBUG"):
        asyncio.run(run())
    assert "BODY-SECRET-MARKER" not in caplog.text
    assert "fleet-token-v1" not in caplog.text
    assert "cli-chat-proxy.grok.com" not in caplog.text


@pytest.mark.parametrize(
    ("method", "path", "body", "expected_status"),
    [
        ("POST", "/v1/chat/completions", {"model": "grok-composer-2.5"}, 404),
        ("GET", "/v1/responses", {"model": "grok-composer-2.5"}, 404),
        ("POST", "/v1/responses", {"model": "grok-4"}, 400),
        ("POST", "/v1/responses", {}, 400),
    ],
)
def test_xai_composer_rejects_unlocked_routes_and_models(
    tmp_path, monkeypatch, method, path, body, expected_status
):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _configure_external_oauth_owner(tmp_path)
        _write_xai_pool_entry(tmp_path)
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        monkeypatch.setattr(
            "hermes_cli.proxy.adapters.xai._COMPOSER_BASE_URL",
            f"{upstream_base}/v1",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(XAIGrokComposerAdapter()))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    f"{proxy_base}{path}",
                    json=body,
                ) as resp:
                    assert resp.status == expected_status
                    payload = await resp.json()
                    assert payload["error"]["code"] in {
                        "path_not_allowed",
                        "model_not_allowed",
                    }
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()
        assert captured["requests"] == []

    asyncio.run(run())


def test_xai_composer_adopts_rotated_disk_token_without_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_external_oauth_owner(tmp_path)
    _write_xai_pool_entry(tmp_path, access_token="fleet-token-v1")
    monkeypatch.setattr(
        "hermes_cli.auth.refresh_xai_oauth_pure",
        lambda *args, **kwargs: pytest.fail("subscription proxy must never refresh"),
    )

    adapter = XAIGrokComposerAdapter()
    first = adapter.get_credential()
    _write_xai_pool_entry(tmp_path, access_token="fleet-token-v2")
    second = adapter.get_credential()

    assert first.base_url == "https://cli-chat-proxy.grok.com/v1"
    assert first.bearer == "fleet-token-v1"
    assert second.bearer == "fleet-token-v2"


def test_xai_composer_requires_external_refresh_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)
    adapter = XAIGrokComposerAdapter()
    assert not adapter.is_authenticated()
    with pytest.raises(RuntimeError, match="refresh_owner=external"):
        adapter.get_credential()


def test_xai_composer_safe_errors_do_not_log_or_return_raw_details(
    tmp_path, monkeypatch, caplog
):
    async def run():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _configure_external_oauth_owner(tmp_path)
        _write_xai_pool_entry(tmp_path)
        adapter = XAIGrokComposerAdapter()
        monkeypatch.setattr(
            adapter,
            "get_credential",
            lambda: (_ for _ in ()).throw(RuntimeError("RAW-ERROR token-secret-marker")),
        )
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/responses",
                    json={"model": "grok-composer-2.5", "input": "hello"},
                ) as resp:
                    assert resp.status == 401
                    response_text = await resp.text()
        finally:
            await proxy_runner.cleanup()
        assert "RAW-ERROR" not in response_text
        assert "token-secret-marker" not in response_text

    with caplog.at_level("DEBUG"):
        asyncio.run(run())
    assert "RAW-ERROR" not in caplog.text
    assert "token-secret-marker" not in caplog.text
    assert "cli-chat-proxy.grok.com" not in caplog.text


def test_xai_composer_run_server_refuses_non_loopback_bind():
    from hermes_cli.proxy.server import run_server

    with pytest.raises(ValueError, match="only bind to loopback"):
        asyncio.run(
            run_server(
                XAIGrokComposerAdapter(),
                host="0.0.0.0",
                port=0,
                shutdown_event=asyncio.Event(),
            )
        )


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------






