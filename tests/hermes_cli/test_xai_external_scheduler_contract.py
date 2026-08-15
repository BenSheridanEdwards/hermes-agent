"""Fleet-owned xAI OAuth refresh writer contract."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.auth import DEFAULT_XAI_OAUTH_BASE_URL


def _xai_entry(*, access_token: str, refresh_token: str, label: str = "Neo Grok Sub") -> dict:
    return {
        "id": "neo-grok-sub",
        "label": label,
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "base_url": DEFAULT_XAI_OAUTH_BASE_URL,
    }


def _write_external_store(hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "oauth:\n  refresh_owner: external\n",
        encoding="utf-8",
    )
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "credential_pool": {
                    "xai-oauth": [
                        _xai_entry(
                            access_token="old-access",
                            refresh_token="old-refresh",
                        )
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_external_scheduler_authority_updates_xai_oauth_tokens(tmp_path, monkeypatch):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    hermes_home = tmp_path / "hermes"
    _write_external_store(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    write_credential_pool(
        "xai-oauth",
        [_xai_entry(access_token="new-access", refresh_token="new-refresh")],
        oauth_token_write_authority="external-scheduler",
    )

    [persisted] = read_credential_pool("xai-oauth")
    assert persisted["access_token"] == "new-access"
    assert persisted["refresh_token"] == "new-refresh"


@pytest.mark.parametrize("authority", [None, "scheduler", "external_scheduler"])
def test_external_owner_rejects_unsanctioned_xai_token_writers(
    tmp_path,
    monkeypatch,
    authority,
):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    hermes_home = tmp_path / "hermes"
    _write_external_store(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    kwargs = {}
    if authority is not None:
        kwargs["oauth_token_write_authority"] = authority
    write_credential_pool(
        "xai-oauth",
        [_xai_entry(access_token="stolen-access", refresh_token="stolen-refresh")],
        **kwargs,
    )

    [persisted] = read_credential_pool("xai-oauth")
    assert persisted["access_token"] == "old-access"
    assert persisted["refresh_token"] == "old-refresh"


def test_standalone_xai_pool_write_remains_compatible(tmp_path, monkeypatch):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    write_credential_pool(
        "xai-oauth",
        [_xai_entry(access_token="standalone-access", refresh_token="standalone-refresh")],
    )

    [persisted] = read_credential_pool("xai-oauth")
    assert persisted["access_token"] == "standalone-access"
    assert persisted["refresh_token"] == "standalone-refresh"


@pytest.mark.parametrize("requested_label", [None, "Neo Grok Sub", "generic lane"])
def test_interactive_xai_add_uses_named_profile_dedicated_lane_without_secret_output(
    tmp_path,
    monkeypatch,
    capsys,
    requested_label,
):
    from hermes_cli.auth_commands import auth_add_command

    profile_home = tmp_path / ".hermes" / "profiles" / "neo"
    _write_external_store(profile_home)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(
        "hermes_cli.auth._xai_oauth_device_code_login",
        lambda **_kwargs: {
            "tokens": {
                "access_token": "interactive-access-secret",
                "refresh_token": "interactive-refresh-secret",
            },
            "base_url": DEFAULT_XAI_OAUTH_BASE_URL,
            "last_refresh": "2026-08-15T10:00:00Z",
        },
    )

    auth_add_command(
        SimpleNamespace(
            provider="xai-oauth",
            auth_type="oauth",
            api_key=None,
            label=requested_label,
            timeout=3,
            no_browser=True,
        )
    )

    payload = json.loads((profile_home / "auth.json").read_text(encoding="utf-8"))
    added = next(
        entry
        for entry in payload["credential_pool"]["xai-oauth"]
        if entry["access_token"] == "interactive-access-secret"
    )
    assert added["label"] == "Neo Grok Sub"
    output = capsys.readouterr().out
    assert "interactive-access-secret" not in output
    assert "interactive-refresh-secret" not in output
