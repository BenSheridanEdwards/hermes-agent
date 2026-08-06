"""Codex (openai-codex) OAuth refresh-ownership tests.

Mirrors the xAI external-ownership contract tests in
``test_auth_xai_oauth_provider.py``: under ``oauth.refresh_owner=external``
the runtime adopts scheduler-owned pool tokens but never spends the Codex
refresh token; interactive device-code login remains the single authorized
write path for new OAuth rows.
"""

import base64
import json
import time
import uuid

import pytest

from hermes_cli.auth import (
    AuthError,
    DEFAULT_CODEX_BASE_URL,
    _refresh_codex_auth_tokens,
    _save_codex_tokens,
    resolve_codex_runtime_credentials,
    runtime_owns_oauth_refresh,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .rstrip(b"=")
        .decode("utf-8")
    )
    return f"h.{encoded}.s"


def _setup_codex_auth(
    hermes_home,
    *,
    access_token: str = "access",
    refresh_token: str = "refresh",
):
    """Write Codex singleton tokens into the Hermes auth store."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    auth_store = {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "last_refresh": "2026-05-14T00:00:00Z",
                "auth_mode": "chatgpt",
            }
        },
    }
    auth_file = hermes_home / "auth.json"
    auth_file.write_text(json.dumps(auth_store, indent=2))
    return auth_file


def _write_external_owner_config(hermes_home):
    (hermes_home / "config.yaml").write_text("oauth:\n  refresh_owner: external\n")


def _pool_row(
    *,
    entry_id: str = "scheduler-row",
    access_token: str = "access-scheduler",
    refresh_token: str = "refresh-scheduler",
    source: str = "device_code",
):
    return {
        "id": entry_id,
        "label": "scheduler",
        "auth_type": "oauth",
        "priority": 0,
        "source": source,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "base_url": DEFAULT_CODEX_BASE_URL,
    }


# ---------------------------------------------------------------------------
# Ownership contract
# ---------------------------------------------------------------------------


def test_resolve_codex_credentials_refuses_external_owner_refresh(
    tmp_path, monkeypatch
):
    """force_refresh under external ownership adopts the pool without a POST."""
    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(
        hermes_home,
        access_token=_jwt_with_exp(int(time.time()) + 30),
    )
    _write_external_owner_config(hermes_home)
    auth_payload = json.loads((hermes_home / "auth.json").read_text())
    scheduler_access = _jwt_with_exp(int(time.time()) + 3 * 60 * 60)
    auth_payload["credential_pool"] = {
        "openai-codex": [_pool_row(access_token=scheduler_access)]
    }
    (hermes_home / "auth.json").write_text(json.dumps(auth_payload))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def _refresh_must_not_run(*_args, **_kwargs):
        raise AssertionError("runtime must not rotate externally managed OAuth")

    monkeypatch.setattr(
        "hermes_cli.auth.refresh_codex_oauth_pure", _refresh_must_not_run
    )
    monkeypatch.setattr(
        "hermes_cli.auth._refresh_codex_auth_tokens", _refresh_must_not_run
    )

    creds = resolve_codex_runtime_credentials(
        force_refresh=True, refresh_if_expiring=True
    )
    assert creds["api_key"] == scheduler_access
    assert creds["source"] == "credential_pool"


def test_resolve_codex_credentials_external_pool_unavailable(tmp_path, monkeypatch):
    """External owner + no scheduler-owned pool row is a typed failure."""
    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(hermes_home)
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as excinfo:
        resolve_codex_runtime_credentials()
    assert excinfo.value.code == "codex_external_pool_unavailable"
    assert excinfo.value.relogin_required is False


def test_refresh_codex_auth_tokens_forbidden_under_external_owner(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as excinfo:
        _refresh_codex_auth_tokens(
            {"access_token": "a", "refresh_token": "r"}, 5.0
        )
    assert excinfo.value.code == "codex_external_refresh_forbidden"


def test_external_refresh_owner_blocks_codex_pool_rotation(tmp_path, monkeypatch):
    """Pool refresh becomes adopt-from-disk; the refresh POST never fires."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}})
    )
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def _refresh_must_not_run(*_args, **_kwargs):
        raise AssertionError("runtime must not rotate externally managed OAuth")

    monkeypatch.setattr(
        "hermes_cli.auth.refresh_codex_oauth_pure", _refresh_must_not_run
    )

    near_expiry = _jwt_with_exp(int(time.time()) + 30)
    entry = PooledCredential(
        provider="openai-codex",
        id=uuid.uuid4().hex[:6],
        label="test",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="manual:device_code",
        access_token=near_expiry,
        refresh_token="rt-owned-by-scheduler",
        base_url=DEFAULT_CODEX_BASE_URL,
    )
    pool = CredentialPool("openai-codex", [entry])
    pool._persist(oauth_token_write_authority="external-scheduler")

    assert pool._entry_needs_refresh(entry) is False
    assert pool.select() is not None

    auth_payload = json.loads((hermes_home / "auth.json").read_text())
    scheduler_access = _jwt_with_exp(int(time.time()) + 3 * 60 * 60)
    scheduler_entry = auth_payload["credential_pool"]["openai-codex"][0]
    scheduler_entry["access_token"] = scheduler_access
    scheduler_entry["refresh_token"] = "rt-rotated-by-scheduler"
    (hermes_home / "auth.json").write_text(json.dumps(auth_payload))

    adopted = pool.try_refresh_current()
    assert adopted is not None
    assert adopted.access_token == scheduler_access
    assert adopted.refresh_token == "rt-rotated-by-scheduler"


def test_external_owner_skips_codex_singleton_seed(tmp_path, monkeypatch):
    """Under external ownership the pool never seeds from the singleton."""
    from agent.credential_pool import load_pool

    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(
        hermes_home,
        access_token="access-stale-singleton",
        refresh_token="refresh-stale-singleton",
    )
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    pool = load_pool("openai-codex")
    assert all(
        entry.source != "device_code" for entry in pool.entries()
    ), "singleton must not seed a pool row under external ownership"


def test_runtime_owner_still_seeds_codex_singleton(tmp_path, monkeypatch):
    """Control: runtime-owned behavior (no oauth block) keeps singleton seeding."""
    from agent.credential_pool import load_pool

    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(
        hermes_home,
        access_token="access-singleton",
        refresh_token="refresh-singleton",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    pool = load_pool("openai-codex")
    seeded = [entry for entry in pool.entries() if entry.source == "device_code"]
    assert len(seeded) == 1
    assert seeded[0].access_token == "access-singleton"


def test_external_owner_codex_entry_skips_singleton_adoption(tmp_path, monkeypatch):
    """_sync_codex_entry_from_auth_store is a no-op under external ownership."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential

    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(
        hermes_home,
        access_token="access-stale-singleton",
        refresh_token="refresh-stale-singleton",
    )
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    entry = PooledCredential(
        provider="openai-codex",
        id="scheduler-row",
        label="scheduler",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="device_code",
        access_token="access-scheduler",
        refresh_token="refresh-scheduler",
        base_url=DEFAULT_CODEX_BASE_URL,
    )
    pool = CredentialPool("openai-codex", [entry])
    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced.access_token == "access-scheduler"
    assert synced.refresh_token == "refresh-scheduler"


def test_external_owner_interactive_codex_reauth_updates_pool_row(
    tmp_path, monkeypatch
):
    """Interactive re-auth stays the authorized write path for existing rows."""
    hermes_home = tmp_path / "hermes"
    _setup_codex_auth(
        hermes_home,
        access_token="access-old-dead",
        refresh_token="refresh-old-dead",
    )
    auth_payload = json.loads((hermes_home / "auth.json").read_text())
    auth_payload["credential_pool"] = {
        "openai-codex": [
            _pool_row(
                entry_id="device-entry",
                access_token="access-old-dead",
                refresh_token="refresh-old-dead",
            )
        ]
    }
    (hermes_home / "auth.json").write_text(json.dumps(auth_payload))
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    _save_codex_tokens({
        "access_token": "access-from-reauth",
        "refresh_token": "refresh-from-reauth",
    })

    persisted = json.loads((hermes_home / "auth.json").read_text())
    persisted_entry = persisted["credential_pool"]["openai-codex"][0]
    assert persisted_entry["access_token"] == "access-from-reauth"
    assert persisted_entry["refresh_token"] == "refresh-from-reauth"
    resolved = resolve_codex_runtime_credentials()
    assert resolved["api_key"] == "access-from-reauth"


def test_external_owner_codex_reauth_appends_pool_row_when_missing(
    tmp_path, monkeypatch
):
    """Fleet recovery: re-auth with an empty pool must land a scheduler row."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}})
    )
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    _save_codex_tokens({
        "access_token": "access-from-recovery-login",
        "refresh_token": "refresh-from-recovery-login",
    })

    persisted = json.loads((hermes_home / "auth.json").read_text())
    rows = persisted["credential_pool"]["openai-codex"]
    assert len(rows) == 1
    assert rows[0]["source"] == "device_code"
    assert rows[0]["access_token"] == "access-from-recovery-login"
    assert rows[0]["refresh_token"] == "refresh-from-recovery-login"
    resolved = resolve_codex_runtime_credentials()
    assert resolved["api_key"] == "access-from-recovery-login"


def test_external_owner_add_entry_requires_interactive_login_authority(
    tmp_path, monkeypatch
):
    """Unauthorized add_entry drops the OAuth row; interactive-login persists it."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}})
    )
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def _make_entry():
        return PooledCredential(
            provider="openai-codex",
            id=uuid.uuid4().hex[:6],
            label="added",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source="manual:device_code",
            access_token="access-added",
            refresh_token="refresh-added",
            base_url=DEFAULT_CODEX_BASE_URL,
        )

    CredentialPool("openai-codex", []).add_entry(_make_entry())
    persisted = json.loads((hermes_home / "auth.json").read_text())
    unauthorized_rows = (
        persisted.get("credential_pool", {}).get("openai-codex") or []
    )
    assert unauthorized_rows == []

    CredentialPool("openai-codex", []).add_entry(
        _make_entry(), oauth_token_write_authority="interactive-login"
    )
    persisted = json.loads((hermes_home / "auth.json").read_text())
    authorized_rows = persisted["credential_pool"]["openai-codex"]
    assert len(authorized_rows) == 1
    assert authorized_rows[0]["access_token"] == "access-added"


def test_ownership_config_is_provider_scoped_not_global(tmp_path, monkeypatch):
    """The oauth block flips codex and xai together; nous stays runtime-owned."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    _write_external_owner_config(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert runtime_owns_oauth_refresh("openai-codex") is False
    assert runtime_owns_oauth_refresh("xai-oauth") is False
    assert runtime_owns_oauth_refresh("nous") is True
    assert runtime_owns_oauth_refresh("anthropic") is True
