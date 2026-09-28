"""`hermes auth reauth`: renew one pooled OAuth row in place (same id/label/priority/source).

Shared subscription logins (one row bound by many agents' credential assignments) must be renewed
without minting a new pool row: `auth add` always creates a fresh id, so every assignment bound to
the old id kept the dead token. These tests drive the production parser + command dispatch against
a temp HERMES_HOME with a fake device-code login (no network).
"""
import argparse
import json
import os
import time
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli import auth_commands
from hermes_cli.subcommands.auth import build_auth_parser

OLD_ACCESS, OLD_REFRESH = "fixture-old-access", "fixture-old-refresh"
NEW_ACCESS, NEW_REFRESH = "fixture-new-access", "fixture-new-refresh"


@pytest.fixture(autouse=True)
def isolated_external_auth_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))


def _run_cli(*argv: str) -> None:
    parser = argparse.ArgumentParser(prog="hermes")
    build_auth_parser(parser.add_subparsers(dest="command"), cmd_auth=auth_commands.auth_command)
    args = parser.parse_args(["auth", *argv])
    args.func(args)


def _auth_path() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "auth.json"


def _write_store(store: dict) -> None:
    path = _auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")


def _read_store() -> dict:
    return json.loads(_auth_path().read_text(encoding="utf-8"))


def _row(row_id, label, source, access, refresh, priority, **extra):
    return {"id": row_id, "label": label, "auth_type": "oauth", "priority": priority, "source": source,
            "access_token": access, "refresh_token": refresh, "base_url": "https://example.invalid/codex",
            "last_status": "exhausted", "last_status_at": time.time(), "last_error_code": 401,
            "last_error_reason": "token_expired", "last_error_reset_at": time.time() + 3600, **extra}


def _codex_store() -> dict:
    return {
        "version": 1, "active_provider": "openai-codex",
        "providers": {"openai-codex": {
            "tokens": {"access_token": OLD_ACCESS, "refresh_token": OLD_REFRESH},
            "last_refresh": "2026-01-01T00:00:00Z", "auth_mode": "chatgpt", "label": "Personal Codex"}},
        "credential_pool": {"openai-codex": [
            _row("16a4f9", "Personal Codex", "device_code", OLD_ACCESS, OLD_REFRESH, 0),
            _row("06c147", "CodeWalnut Codex", "manual:device_code", "fixture-cw-access",
                 "fixture-cw-refresh", 1),
        ]},
    }


def _fake_codex_login(calls):
    def login():
        calls.append("codex")
        return {"tokens": {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH},
                "base_url": "https://example.invalid/codex", "last_refresh": "2026-09-28T00:00:00Z",
                "auth_mode": "chatgpt", "source": "device-code"}
    return login


def test_reauth_renews_singleton_alias_row_in_place(monkeypatch):
    calls = []
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _fake_codex_login(calls))
    _write_store(_codex_store())
    before = _read_store()

    _run_cli("reauth", "openai-codex", "16a4f9", "--no-browser")

    after = _read_store()
    assert calls == ["codex"]
    rows = after["credential_pool"]["openai-codex"]
    # Never a new row: the pool keeps exactly the same ids in the same order.
    assert [r["id"] for r in rows] == [r["id"] for r in before["credential_pool"]["openai-codex"]]
    target = rows[0]
    for key in ("id", "label", "priority", "source", "auth_type"):
        assert target[key] == before["credential_pool"]["openai-codex"][0][key]
    assert (target["access_token"], target["refresh_token"]) == (NEW_ACCESS, NEW_REFRESH)
    assert target["last_refresh"] == "2026-09-28T00:00:00Z"
    assert target["last_status"] == "ok"
    for cleared in ("last_error_code", "last_error_reason", "last_error_reset_at"):
        assert target.get(cleared) is None
    # The device_code row aliases providers.openai-codex, so the singleton follows it.
    singleton = after["providers"]["openai-codex"]
    assert singleton["tokens"] == {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    assert singleton["label"] == "Personal Codex"
    # The independent CodeWalnut row is untouched.
    assert rows[1] == before["credential_pool"]["openai-codex"][1]


def test_reauth_by_label_renews_independent_row_without_touching_singleton(monkeypatch):
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _fake_codex_login([]))
    _write_store(_codex_store())
    before = _read_store()

    _run_cli("reauth", "openai-codex", "codewalnut codex")

    after = _read_store()
    rows = {r["id"]: r for r in after["credential_pool"]["openai-codex"]}
    assert len(rows) == 2
    assert (rows["06c147"]["access_token"], rows["06c147"]["refresh_token"]) == (NEW_ACCESS, NEW_REFRESH)
    assert rows["06c147"]["label"] == "CodeWalnut Codex"
    assert rows["06c147"]["priority"] == 1
    assert rows["16a4f9"] == before["credential_pool"]["openai-codex"][0]
    assert after["providers"] == before["providers"]


def test_reauth_refreshes_other_copies_of_the_same_lineage(monkeypatch):
    """A row that shares the target's old refresh token is a copy of the same single-use lineage."""
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _fake_codex_login([]))
    store = _codex_store()
    store["credential_pool"]["openai-codex"].append(
        _row("aa11bb", "Personal Codex copy", "manual:device_code", OLD_ACCESS, OLD_REFRESH, 2))
    _write_store(store)

    _run_cli("reauth", "openai-codex", "16a4f9")

    rows = {r["id"]: r for r in _read_store()["credential_pool"]["openai-codex"]}
    assert rows["aa11bb"]["refresh_token"] == NEW_REFRESH
    assert rows["aa11bb"]["label"] == "Personal Codex copy"


@pytest.mark.parametrize("target", ["missing", "3"])
def test_reauth_fails_loudly_before_login_when_row_not_found(monkeypatch, target):
    calls = []
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _fake_codex_login(calls))
    _write_store(_codex_store())
    before = _auth_path().read_bytes()

    with pytest.raises(SystemExit, match="No openai-codex credential"):
        _run_cli("reauth", "openai-codex", target)

    assert calls == []  # never starts a device-code login for a row that isn't there
    assert _auth_path().read_bytes() == before


def test_reauth_fails_without_creating_a_row_when_target_vanishes_during_login(monkeypatch):
    def login_then_row_removed():
        store = _read_store()
        store["credential_pool"]["openai-codex"] = store["credential_pool"]["openai-codex"][1:]
        _write_store(store)
        return _fake_codex_login([])()

    monkeypatch.setattr(auth_mod, "_codex_device_code_login", login_then_row_removed)
    _write_store(_codex_store())

    with pytest.raises(SystemExit, match="disappeared"):
        _run_cli("reauth", "openai-codex", "16a4f9")

    rows = _read_store()["credential_pool"]["openai-codex"]
    assert [r["id"] for r in rows] == ["06c147"]
    assert all(r["access_token"] != NEW_ACCESS for r in rows)


def test_reauth_ambiguous_label_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _fake_codex_login(calls))
    store = _codex_store()
    store["credential_pool"]["openai-codex"][1]["label"] = "Personal Codex"
    _write_store(store)

    with pytest.raises(SystemExit, match="Ambiguous"):
        _run_cli("reauth", "openai-codex", "Personal Codex")
    assert calls == []


def test_reauth_xai_passes_timeout_and_browser_flags(monkeypatch):
    seen = {}

    def fake_xai_login(*, timeout_seconds, open_browser):
        seen.update(timeout=timeout_seconds, open_browser=open_browser)
        return {"tokens": {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH},
                "discovery": {"token_endpoint": "https://example.invalid/token"}, "redirect_uri": "",
                "base_url": "https://example.invalid/xai", "last_refresh": "2026-09-28T00:00:00Z"}

    monkeypatch.setattr(auth_mod, "_xai_oauth_device_code_login", fake_xai_login)
    _write_store({"version": 1, "providers": {"xai-oauth": {
        "tokens": {"access_token": OLD_ACCESS, "refresh_token": OLD_REFRESH},
        "last_refresh": "2026-01-01T00:00:00Z", "auth_mode": "oauth_device_code"}},
        "credential_pool": {"xai-oauth": [_row("9f9f9f", "Grok", "device_code", OLD_ACCESS, OLD_REFRESH, 0)]}})

    _run_cli("reauth", "xai-oauth", "Grok", "--no-browser", "--timeout", "45")

    after = _read_store()
    assert seen == {"timeout": 45.0, "open_browser": False}
    row = after["credential_pool"]["xai-oauth"][0]
    assert (row["id"], row["label"], row["access_token"]) == ("9f9f9f", "Grok", NEW_ACCESS)
    assert after["providers"]["xai-oauth"]["tokens"]["refresh_token"] == NEW_REFRESH
    assert after["providers"]["xai-oauth"]["discovery"] == {"token_endpoint": "https://example.invalid/token"}


def test_reauth_rejects_providers_without_a_device_code_relogin(monkeypatch):
    _write_store({"version": 1, "credential_pool": {"openrouter": [
        {"id": "k1", "label": "key", "auth_type": "api_key", "priority": 0, "source": "manual",
         "access_token": "fixture-key"}]}})
    with pytest.raises(SystemExit, match="does not support"):
        _run_cli("reauth", "openrouter", "k1")
