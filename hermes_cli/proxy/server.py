"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

Listens on ``http://<host>:<port>/v1/<path>`` and forwards each request to
``<upstream-base-url>/<path>`` with the client's ``Authorization`` header
replaced by a freshly-resolved bearer from the configured adapter. The
response is streamed back unmodified, preserving SSE.

The server is intentionally minimal: it does NOT mediate, log, transform,
or rewrite request/response bodies. It's a credential-attaching forwarder.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Mapping, Optional

try:
    import aiohttp
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import (
    ProxyRequestError,
    UpstreamAdapter,
    UpstreamCredential,
)

logger = logging.getLogger(__name__)

# Headers we strip when forwarding to the upstream. ``host``/``content-length``
# are recomputed by aiohttp; ``authorization`` is replaced with our bearer.
# Everything else (content-type, accept, user-agent, x-* headers) passes through.
_HOP_BY_HOP_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "authorization",  # we replace this one
})

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"
# Body cap for forwarded requests. Chat-completion payloads with long agent
# conversations can be large; mirror api_server's MAX_REQUEST_BYTES (10 MB).
# client_max_size bounds every read path, including chunked bodies.
MAX_REQUEST_BYTES = 10_000_000
MAX_ATTESTATION_RESPONSE_BYTES = 1_000_000


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """Return an OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _filter_request_headers(headers: Mapping[str, str]) -> dict:
    """Strip hop-by-hop + auth headers from the inbound request."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


def _filter_response_headers(headers) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        # aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
        if key.lower() in {"content-encoding", "content-length"}:
            continue
        out[key] = value
    return out


def create_app(adapter: UpstreamAdapter) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    # AppKey ensures forward-compat with future aiohttp versions that strip
    # bare-string keys.
    _adapter_key = web.AppKey("adapter", UpstreamAdapter)
    app[_adapter_key] = adapter

    async def handle_health(request: "web.Request") -> "web.Response":
        attestation = adapter.health_attestation()
        if attestation is not None:
            status = 200 if attestation.get("status") == "ready" else 503
            return web.json_response(attestation, status=status)
        return web.json_response({
            "status": "ok",
            "upstream": adapter.display_name,
            "authenticated": adapter.is_authenticated(),
        })

    async def handle_model_attestation(request: "web.Request") -> "web.Response":
        if await request.read():
            return _json_error(
                400,
                "model attestation does not accept caller controls",
                code="attestation_input_not_allowed",
            )
        try:
            credential = adapter.get_credential()
        except Exception:
            logger.warning("proxy: model attestation credential resolution failed")
            return _json_error(
                401,
                "upstream credential unavailable",
                code="upstream_auth_failed",
            )

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
        controls = adapter.model_attestation_requests()
        results: list[tuple[int, bytes]] = []
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for body, headers in controls:
                    upstream_headers = dict(headers)
                    upstream_headers["Authorization"] = (
                        f"{credential.token_type} {credential.bearer}"
                    )
                    async with session.post(
                        f"{credential.base_url.rstrip('/')}/responses",
                        data=body,
                        headers=upstream_headers,
                        allow_redirects=False,
                    ) as response:
                        if response.status == 429:
                            retry_after = response.headers.get("Retry-After")
                            response_headers = (
                                {"Retry-After": retry_after} if retry_after else None
                            )
                            return web.json_response(
                                {
                                    "error": {
                                        "message": "model attestation was rate limited",
                                        "type": "rate_limited",
                                        "code": "rate_limited",
                                    }
                                },
                                status=429,
                                headers=response_headers,
                            )
                        response_body = await response.read()
                        if len(response_body) > MAX_ATTESTATION_RESPONSE_BYTES:
                            raise ProxyRequestError(
                                "model attestation response exceeded the size limit"
                            )
                        results.append((response.status, response_body))
        except ProxyRequestError:
            logger.warning("proxy: model attestation response was invalid")
            return _json_error(
                502,
                "model attestation response was invalid",
                code="attestation_failed",
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.warning("proxy: model attestation upstream unavailable")
            return _json_error(
                502,
                "model attestation upstream unavailable",
                code="upstream_unreachable",
            )
        try:
            return web.json_response(adapter.validate_model_attestation(results))
        except ProxyRequestError:
            logger.warning("proxy: model attestation controls failed")
            return _json_error(
                502,
                "model attestation controls failed",
                code="attestation_failed",
            )

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        # Extract the path *after* /v1
        rel_path = request.match_info.get("tail", "")
        rel_path = "/" + rel_path.lstrip("/")

        if rel_path not in adapter.allowed_paths or not adapter.request_method_allowed(
            request.method
        ):
            allowed = ", ".join(sorted(adapter.allowed_paths))
            return _json_error(
                404,
                f"Path /v1{rel_path} is not forwarded by this proxy. "
                f"Allowed: {allowed}",
                code="path_not_allowed",
            )

        try:
            cred = adapter.get_credential()
        except Exception as exc:
            if adapter.safe_error_messages:
                logger.warning("proxy: credential resolution failed")
                message = "upstream credential unavailable"
            else:
                logger.warning("proxy: credential resolution failed: %s", exc)
                message = str(exc)
            return _json_error(401, message, code="upstream_auth_failed")

        # Forward body verbatim. Read into memory once — request bodies for
        # chat/completions/embeddings are small (<1MB typically). If we ever
        # need to forward large multipart uploads we'll switch to streaming
        # the request body too.
        body = await request.read()
        try:
            body, prepared_headers = adapter.prepare_request(
                body=body,
                headers=_filter_request_headers(request.headers),
            )
        except ProxyRequestError as exc:
            return _json_error(400, str(exc), code=exc.code)

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        async def _send_upstream(active_cred: UpstreamCredential):
            upstream_url = f"{active_cred.base_url.rstrip('/')}{rel_path}"
            # Preserve query string verbatim.
            if request.query_string:
                upstream_url = f"{upstream_url}?{request.query_string}"

            fwd_headers = dict(prepared_headers)
            fwd_headers["Authorization"] = (
                f"{active_cred.token_type} {active_cred.bearer}"
            )

            if adapter.safe_error_messages:
                logger.debug("proxy: forwarding %s %s", request.method, rel_path)
            else:
                logger.debug(
                    "proxy: forwarding %s %s -> %s (body=%d bytes)",
                    request.method,
                    rel_path,
                    upstream_url,
                    len(body),
                )

            try:
                session = aiohttp.ClientSession(timeout=timeout)
            except Exception as exc:  # pragma: no cover - aiohttp setup issue
                raise RuntimeError(f"proxy session init failed: {exc}") from exc

            try:
                upstream_resp = await session.request(
                    request.method,
                    upstream_url,
                    data=body if body else None,
                    headers=fwd_headers,
                    allow_redirects=False,
                )
            except Exception:
                await session.close()
                raise
            return session, upstream_resp

        async def _open_upstream(active_cred: UpstreamCredential):
            try:
                return await _send_upstream(active_cred)
            except RuntimeError as exc:
                if adapter.safe_error_messages:
                    logger.warning("proxy: upstream session initialization failed")
                    message = "upstream session initialization failed"
                else:
                    message = str(exc)
                return _json_error(500, message), None
            except aiohttp.ClientError as exc:
                if adapter.safe_error_messages:
                    logger.warning("proxy: upstream connection failed")
                    message = "upstream connection failed"
                else:
                    logger.warning("proxy: upstream connection failed: %s", exc)
                    message = f"upstream connection failed: {exc}"
                return (
                    _json_error(
                        502,
                        message,
                        code="upstream_unreachable",
                    ),
                    None,
                )
            except asyncio.TimeoutError:
                return (
                    _json_error(
                        504,
                        "upstream request timed out",
                        code="upstream_timeout",
                    ),
                    None,
                )

        session_or_response, upstream_resp = await _open_upstream(cred)
        if upstream_resp is None:
            return session_or_response
        session = session_or_response

        if upstream_resp.status in {401, 429}:
            try:
                retry_cred = adapter.get_retry_credential(
                    failed_credential=cred,
                    status_code=upstream_resp.status,
                )
            except Exception as exc:
                if adapter.safe_error_messages:
                    logger.warning("proxy: retry credential resolution failed")
                else:
                    logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None

            if retry_cred is not None:
                upstream_resp.release()
                await session.close()
                session_or_response, upstream_resp = await _open_upstream(retry_cred)
                if upstream_resp is None:
                    return session_or_response
                session = session_or_response

        # Stream response back. Headers first, then chunked body.
        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=_filter_response_headers(upstream_resp.headers),
        )
        await resp.prepare(request)

        try:
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await resp.write(chunk)
        except (aiohttp.ClientError, asyncio.CancelledError) as exc:
            if adapter.safe_error_messages:
                logger.warning("proxy: streaming interrupted")
            else:
                logger.warning("proxy: streaming interrupted: %s", exc)
        finally:
            upstream_resp.release()
            await session.close()

        await resp.write_eof()
        return resp

    # /health doesn't go through the upstream
    app.router.add_get("/health", handle_health)
    if adapter.model_attestation_path is not None:
        app.router.add_post(adapter.model_attestation_path, handle_model_attestation)
    # Catch-all under /v1 — forwards if the path is allowed.
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)

    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set.

    If shutdown_event is None, runs until cancelled (Ctrl+C or SIGTERM).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    if adapter.loopback_only and host not in {"127.0.0.1", "::1"}:
        raise ValueError(f"{adapter.display_name} can only bind to loopback")

    app = create_app(adapter)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(
        "proxy: listening on http://%s:%d/v1 -> %s",
        host,
        port,
        adapter.display_name,
    )

    stop_event = shutdown_event or asyncio.Event()

    # Wire signal handlers when we own the loop's lifetime.
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                # Windows / restricted environments — Ctrl+C will still
                # raise KeyboardInterrupt and unwind us.
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = [
    "create_app",
    "run_server",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AIOHTTP_AVAILABLE",
]
