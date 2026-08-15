import json
from types import SimpleNamespace

import pytest

from gateway import status as gateway_status
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_arbitrary_single_profile_gateway_publishes_sanitized_telegram_runtime_receipt(
    monkeypatch, tmp_path
):
    """A future profile gets PID-bound Telegram proof without a roster entry."""
    state_path = tmp_path / "future-profile" / "gateway_state.json"
    monkeypatch.setattr(gateway_status, "_get_runtime_status_path", lambda: state_path)
    monkeypatch.setattr(
        gateway_status,
        "_build_pid_record",
        lambda: {
            "kind": "gateway",
            "pid": 424242,
            "argv": ["hermes", "--profile", "future-profile", "gateway", "run"],
            "start_time": 1234.5,
        },
    )

    config = PlatformConfig.from_dict({"enabled": True, "token": "secret-token"})
    adapter = TelegramAdapter(config)
    adapter._bot = SimpleNamespace(id=987654, username="future_profile_bot")

    adapter._mark_connected()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    runtime = state["platforms"]["telegram"]["runtime"]
    assert runtime == {
        "credential_source": "config_file",
        "authenticated": True,
        "bot_id": "987654",
        "bot_username": "future_profile_bot",
        "transport_mode": "polling",
        "transport_ready": True,
        "verified_at": runtime["verified_at"],
    }
    assert "secret-token" not in state_path.read_text(encoding="utf-8")


def test_runtime_receipt_rejects_unapproved_fields(monkeypatch, tmp_path):
    state_path = tmp_path / "arbitrary-profile" / "gateway_state.json"
    monkeypatch.setattr(gateway_status, "_get_runtime_status_path", lambda: state_path)

    receipt = {
        "credential_source": "profile_env",
        "authenticated": True,
        "bot_id": "123",
        "bot_username": "arbitrary_bot",
        "transport_mode": "polling",
        "transport_ready": True,
        "verified_at": "2026-08-15T16:00:00+00:00",
        "token": "must-never-be-persisted",
    }

    with pytest.raises(ValueError, match="unapproved field"):
        gateway_status.write_runtime_status(
            platform="telegram",
            platform_state="connected",
            platform_runtime=receipt,
        )

    assert not state_path.exists()


def test_multiplexed_gateway_does_not_claim_one_profiles_runtime_receipt(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(gateway_status, "_get_runtime_status_path", lambda: state_path)
    receipt = {
        "credential_source": "managed_env",
        "authenticated": True,
        "bot_id": "123",
        "bot_username": "arbitrary_bot",
        "transport_mode": "webhook",
        "transport_ready": True,
        "verified_at": "2026-08-15T16:00:00+00:00",
    }

    gateway_status.write_runtime_status(
        served_profiles=["future-alpha", "future-beta"],
        platform="telegram",
        platform_state="connected",
        platform_runtime=receipt,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["platforms"]["telegram"]["state"] == "connected"
    assert "runtime" not in state["platforms"]["telegram"]
