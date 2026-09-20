"""Shared Codex readers must reject uncertain rotations and use root quota state."""

import base64
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.credential_pool import load_pool
from hermes_cli import auth


def _entry(identifier):
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": time.time() + 7200, "account": identifier,
    }).encode()).decode().rstrip("=")
    return {
        "id": identifier, "label": identifier, "priority": 0,
        "source": "manual:device_code", "auth_type": "oauth",
        "access_token": f"synthetic.{payload}.signature",
        "refresh_token": f"synthetic-refresh-{identifier}", "last_status": "ok",
    }


def _write_store(path, rows):
    path.write_text(json.dumps({"credential_pool": {"openai-codex": rows}}))


@pytest.fixture
def shared_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    root = tmp_path / "authority.json"
    (profile / "config.yaml").write_text(json.dumps({"oauth": {
        "refresh_owner": "runtime", "shared_codex_auth_path": str(root),
    }}))
    _write_store(root, [_entry("personal")])
    return root, profile


def test_failed_commit_cannot_lease_old_pair_through_any_reader(shared_stores):
    root, profile = shared_stores
    before = root.read_bytes()
    pool = load_pool("openai-codex")
    with patch.object(auth, "refresh_codex_oauth_pure", return_value={
        "access_token": _entry("rotated")["access_token"],
        "refresh_token": "synthetic-rotated-refresh",
    }), patch.object(auth, "_save_auth_store", side_effect=OSError("synthetic write failure")):
        with pytest.raises(OSError):
            pool._refresh_entry(pool.entries()[0], force=True)
    with patch.object(auth, "refresh_codex_oauth_pure") as refresh:
        assert load_pool("openai-codex").select() is None
        assert auth._selected_usable_oauth_pool_entry("openai-codex") is None
        assert auth._pool_codex_access_token() == ""
        for operation in [auth._read_codex_tokens,
                          auth.resolve_codex_runtime_credentials,
                          lambda: auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)]:
            with pytest.raises(auth.AuthError):
                operation()
        refresh.assert_not_called()
    assert root.read_bytes() == before
    assert not (profile / "auth.json").exists()


@pytest.mark.parametrize("marker_contents", ["", "g" * 64, "not-a-digest"])
def test_malformed_pending_marker_is_not_usable(shared_stores, marker_contents):
    root, _ = shared_stores
    auth._shared_codex_refresh_marker(root, "personal").write_text(marker_contents)
    assert load_pool("openai-codex").select() is None
    assert auth._pool_codex_access_token() == ""


def test_pending_account_does_not_hide_independent_or_reauthenticated_account(shared_stores):
    root, _ = shared_stores
    personal, work = _entry("personal"), _entry("work")
    _write_store(root, [personal, work])
    auth.begin_shared_codex_refresh(root, personal["id"], personal["refresh_token"])
    assert load_pool("openai-codex").select().id == "work"
    assert auth._pool_codex_access_token() == work["access_token"]
    personal["refresh_token"] = "synthetic-new-login"
    _write_store(root, [personal, work])
    assert load_pool("openai-codex").select().id == "personal"
    assert auth._pool_codex_access_token() == personal["access_token"]


def test_stale_waiter_cannot_adopt_newer_pair_with_unresolved_refresh(shared_stores):
    root, _ = shared_stores
    stale_pool = load_pool("openai-codex")
    stale = stale_pool.entries()[0]
    newer = _entry("personal")
    newer["refresh_token"] = "synthetic-newer-refresh"
    _write_store(root, [newer])
    auth.begin_shared_codex_refresh(root, newer["id"], newer["refresh_token"])
    with patch.object(auth, "refresh_codex_oauth_pure") as refresh:
        assert stale_pool._refresh_entry(stale, force=True) is None
        refresh.assert_not_called()


def _exhausted_entry(identifier):
    row = _entry(identifier)
    row.update(last_status="exhausted", last_status_at=time.time(),
               last_error_code=429, last_error_reason="usage_limit",
               last_error_reset_at=time.time() + 50000)
    return row


def test_quota_read_and_reset_use_root_and_preserve_other_account(shared_stores):
    root, profile = shared_stores
    personal, work = _exhausted_entry("personal"), _exhausted_entry("work")
    _write_store(root, [personal, work])
    _write_store(profile / "auth.json", [_exhausted_entry("local")])
    shadow = (profile / "auth.json").read_bytes()
    assert auth._codex_pool_rate_limit_status()["label"] == "personal"
    assert auth.clear_codex_pool_quota_cooldowns(personal["access_token"]) == 1
    persisted = json.loads(root.read_text())["credential_pool"]["openai-codex"]
    assert persisted[0]["last_status"] is None
    assert persisted[1] == work
    assert auth._codex_pool_rate_limit_status()["label"] == "work"
    assert (profile / "auth.json").read_bytes() == shadow


def test_quota_readers_fail_closed_when_authority_missing(shared_stores):
    root, profile = shared_stores
    _write_store(profile / "auth.json", [_exhausted_entry("local")])
    shadow = (profile / "auth.json").read_bytes()
    root.unlink()
    for operation in [auth._codex_pool_rate_limit_status, auth.clear_codex_pool_quota_cooldowns]:
        with pytest.raises(auth.AuthError):
            operation()
    assert (profile / "auth.json").read_bytes() == shadow


def test_quota_reset_does_not_claim_success_when_save_fails(shared_stores):
    root, _ = shared_stores
    _write_store(root, [_exhausted_entry("personal")])
    before = root.read_bytes()
    with patch.object(auth, "_save_auth_store", side_effect=OSError("synthetic write failure")):
        with pytest.raises(OSError):
            auth.clear_codex_pool_quota_cooldowns()
    assert root.read_bytes() == before


def test_quota_status_cannot_report_uncertain_pair_as_only_rate_limited(shared_stores):
    root, _ = shared_stores
    personal = _exhausted_entry("personal")
    _write_store(root, [personal])
    auth.begin_shared_codex_refresh(root, personal["id"], personal["refresh_token"])
    assert auth._codex_pool_rate_limit_status() is None
