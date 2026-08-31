"""xAI Grok OAuth upstream adapter."""

from __future__ import annotations

import json
import logging
import threading
from typing import FrozenSet, Mapping, Optional

from agent.credential_pool import CredentialPool, PooledCredential, load_pool
from hermes_cli.auth import DEFAULT_XAI_OAUTH_BASE_URL, runtime_owns_oauth_refresh
from hermes_cli.proxy.adapters.base import (
    ProxyRequestError,
    UpstreamAdapter,
    UpstreamCredential,
)

logger = logging.getLogger(__name__)

_POOL_PROVIDER = "xai-oauth"

_COMPOSER_MODEL = "grok-composer-2.5"
_COMPOSER_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
_COMPOSER_ALLOWED_PATHS: FrozenSet[str] = frozenset({"/responses"})
_COMPOSER_IDENTITY_HEADERS = {
    "User-Agent": "Grok/0.2.117",
    "x-grok-client-version": "0.2.117",
    "x-grok-client-identifier": "grok-shell",
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-model-override": _COMPOSER_MODEL,
}

# xAI's public API is OpenAI-compatible for the endpoints Hermes commonly
# uses. The Responses endpoint is included because Hermes' native xAI runtime
# uses codex_responses mode.
_ALLOWED_PATHS: FrozenSet[str] = frozenset(
    {
        "/responses",
        "/chat/completions",
        "/completions",
        "/embeddings",
        "/models",
    }
)


class XAIGrokAdapter(UpstreamAdapter):
    """Proxy upstream for xAI Grok via Hermes-managed OAuth credentials."""

    auth_hint = "hermes auth add xai-oauth --type oauth"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: Optional[CredentialPool] = None

    @property
    def name(self) -> str:
        return "xai"

    @property
    def display_name(self) -> str:
        return "xAI Grok OAuth"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        pool = self._load_pool()
        return bool(pool and pool.has_available())

    def get_credential(self) -> UpstreamCredential:
        with self._lock:
            pool = self._load_pool()
            if pool is None or not pool.has_credentials():
                raise RuntimeError(
                    "No xAI OAuth credentials found. Run "
                    "`hermes auth add xai-oauth --type oauth` first."
                )

            entry = pool.select()
            if entry is None:
                raise RuntimeError(
                    "No available xAI OAuth credentials found. Run "
                    "`hermes auth reset xai-oauth` or re-authenticate with "
                    "`hermes auth add xai-oauth --type oauth`."
                )

            self._pool = pool
            return self._credential_from_entry(entry)

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        if status_code not in {401, 429}:
            return None

        with self._lock:
            pool = self._pool or self._load_pool()
            if pool is None:
                return None

            if status_code == 429:
                # Mark the rate-limited key with its 1-hour cooldown and rotate
                # to the next available credential. Returns None when the pool
                # has no other key to offer — the 429 will flow back to the client.
                refreshed = pool.mark_exhausted_and_rotate(status_code=status_code)
            else:
                refreshed = pool.try_refresh_current()
                if refreshed is None:
                    refreshed = pool.mark_exhausted_and_rotate(status_code=status_code)
            if refreshed is None:
                return None

            retry_cred = self._credential_from_entry(refreshed)
            if retry_cred.bearer == failed_credential.bearer:
                return None
            logger.info(
                "proxy: xAI upstream returned %s; retrying with rotated pool credential",
                status_code,
            )
            return retry_cred

    def _load_pool(self) -> Optional[CredentialPool]:
        try:
            return load_pool(_POOL_PROVIDER)
        except Exception as exc:
            logger.warning("proxy: failed to load xAI OAuth credential pool: %s", exc)
            return None

    def _credential_from_entry(self, entry: PooledCredential) -> UpstreamCredential:
        bearer = (
            getattr(entry, "runtime_api_key", None)
            or getattr(entry, "access_token", "")
            or ""
        )
        bearer = str(bearer).strip()
        if not bearer:
            raise RuntimeError(
                "xAI OAuth credential pool entry did not contain an access token. "
                "Re-authenticate with `hermes auth add xai-oauth --type oauth`."
            )

        base_url = (
            getattr(entry, "runtime_base_url", None)
            or getattr(entry, "base_url", None)
            or DEFAULT_XAI_OAUTH_BASE_URL
        )
        base_url = str(base_url or DEFAULT_XAI_OAUTH_BASE_URL).strip().rstrip("/")

        return UpstreamCredential(
            bearer=bearer,
            base_url=base_url or DEFAULT_XAI_OAUTH_BASE_URL,
            expires_at=getattr(entry, "expires_at", None),
        )


class XAIGrokComposerAdapter(XAIGrokAdapter):
    """Locked Grok Composer 2.5 route for an externally refreshed xAI seat."""

    @property
    def name(self) -> str:
        return "xai-composer"

    @property
    def display_name(self) -> str:
        return "xAI Grok Composer 2.5 subscription"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _COMPOSER_ALLOWED_PATHS

    @property
    def loopback_only(self) -> bool:
        return True

    @property
    def safe_error_messages(self) -> bool:
        return True

    def request_method_allowed(self, method: str) -> bool:
        return method.upper() == "POST"

    def is_authenticated(self) -> bool:
        return not runtime_owns_oauth_refresh(_POOL_PROVIDER) and super().is_authenticated()

    def get_credential(self) -> UpstreamCredential:
        if runtime_owns_oauth_refresh(_POOL_PROVIDER):
            raise RuntimeError(
                "Grok Composer subscription mode requires oauth.refresh_owner=external."
            )
        # XAIGrokAdapter reloads auth.json here. Do not retain a pool snapshot
        # across requests: Fleet is the sole refresh writer and may atomically
        # rotate the selected row at any time.
        credential = super().get_credential()
        return UpstreamCredential(
            bearer=credential.bearer,
            base_url=_COMPOSER_BASE_URL,
            expires_at=credential.expires_at,
        )

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        if status_code != 401:
            return None
        # A 401 may race Fleet's atomic token rotation. Re-read once and retry
        # only when disk now contains a different access token. Never refresh,
        # mark exhausted, rotate, or persist from this process.
        retry_credential = self.get_credential()
        if retry_credential.bearer == failed_credential.bearer:
            return None
        return retry_credential

    def prepare_request(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> tuple[bytes, dict[str, str]]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyRequestError(
                "Grok Composer requests require a JSON object body.",
                code="invalid_json",
            ) from exc
        if not isinstance(payload, dict):
            raise ProxyRequestError(
                "Grok Composer requests require a JSON object body."
            )
        if payload.get("model") != _COMPOSER_MODEL:
            raise ProxyRequestError(
                f"Grok Composer mode only accepts model {_COMPOSER_MODEL!r}.",
                code="model_not_allowed",
            )

        prepared = dict(headers)
        # Header names are case-insensitive. Remove all client-supplied identity
        # variants before attaching the locked profile so duplicate casing can
        # never smuggle a conflicting model/client identity upstream.
        locked_names = {name.lower() for name in _COMPOSER_IDENTITY_HEADERS}
        prepared = {
            name: value
            for name, value in prepared.items()
            if name.lower() not in locked_names
        }
        prepared.update(_COMPOSER_IDENTITY_HEADERS)
        return body, prepared


__all__ = ["XAIGrokAdapter", "XAIGrokComposerAdapter"]
