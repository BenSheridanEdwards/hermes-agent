"""Regression coverage for generic, PID-bound platform runtime receipts.

The contract is deliberately roster-independent. Arbitrary platform names and
adapter state prove it applies to every current and future profile.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway import status
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


RUNTIME_RECEIPT = {
    "credential_source": "process_env",
    "authenticated": True,
    "bot_id": "123456789",
    "bot_username": "FutureAgentBot",
    "transport_mode": "polling",
    "transport_ready": True,
    "verified_at": "2026-08-15T15:00:00+00:00",
}


def test_arbitrary_platform_publishes_sanitized_pid_bound_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    status.write_runtime_status(
        gateway_state="running",
        platform="future-platform",
        platform_state="connected",
        platform_runtime=RUNTIME_RECEIPT,
    )

    payload = status.read_runtime_status()
    assert payload["platforms"]["future-platform"]["runtime"] == RUNTIME_RECEIPT
    assert payload["pid"] == status._build_pid_record()["pid"]

    on_disk = json.loads((tmp_path / "gateway_state.json").read_text())
    assert set(on_disk["platforms"]["future-platform"]["runtime"]) == set(RUNTIME_RECEIPT)
    assert "token" not in json.dumps(on_disk).lower()


def test_unknown_runtime_fields_fail_closed_without_persisting_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    unsafe = {**RUNTIME_RECEIPT, "token": "never-write-me"}

    with pytest.raises(ValueError, match="unapproved field"):
        status.write_runtime_status(
            platform="future-platform",
            platform_state="connected",
            platform_runtime=unsafe,
        )

    state_path = tmp_path / "gateway_state.json"
    assert not state_path.exists() or "never-write-me" not in state_path.read_text()


def test_new_pid_does_not_inherit_previous_runtime_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_path = tmp_path / "gateway_state.json"
    state_path.write_text(json.dumps({
        "kind": "hermes-gateway",
        "pid": 99999,
        "start_time": 1.0,
        "gateway_state": "running",
        "platforms": {
            "future-platform": {
                "state": "connected",
                "runtime": RUNTIME_RECEIPT,
            }
        },
    }))

    status.write_runtime_status(gateway_state="starting")

    payload = status.read_runtime_status()
    assert payload["pid"] != 99999
    assert payload["platforms"] == {}


def test_telegram_receipt_uses_runtime_adapter_state_without_roster_names():
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        token="not-persisted",
        credential_source="profile_env",
    )
    adapter._bot = SimpleNamespace(id=4242, username="ArbitraryBot")
    adapter._webhook_mode = True
    adapter._send_path_degraded = False

    receipt = adapter._telegram_runtime_receipt()

    assert receipt["credential_source"] == "profile_env"
    assert receipt["authenticated"] is True
    assert receipt["bot_id"] == "4242"
    assert receipt["bot_username"] == "ArbitraryBot"
    assert receipt["transport_mode"] == "webhook"
    assert receipt["transport_ready"] is True
    assert set(receipt) == set(RUNTIME_RECEIPT)
    assert "token" not in receipt
