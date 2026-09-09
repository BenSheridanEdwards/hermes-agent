"""A managed agent's attested Buzz key must survive the profile ``.env`` load.

A Buzz-managed agent is launched with ``BUZZ_AUTH_TAG`` attesting one keypair.
When the same Hermes profile also runs a gateway, that gateway's own
``BUZZ_PRIVATE_KEY`` sits in the profile ``.env``, which is loaded with
``override=True``. Without the guard the gateway key replaces the attested one
and every ``buzz`` publish fails ``BUZZ_AUTH_TAG`` verification, so the agent
goes silent.
"""
from __future__ import annotations

import os

from hermes_cli import env_loader

HARNESS_KEY = "harness-key"
GATEWAY_KEY = "gateway-key"


def _run_override_load(monkeypatch, *, auth_tag: str | None) -> str | None:
    """Capture, simulate a profile .env override load, restore."""
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", HARNESS_KEY)
    if auth_tag is None:
        monkeypatch.delenv("BUZZ_AUTH_TAG", raising=False)
    else:
        monkeypatch.setenv("BUZZ_AUTH_TAG", auth_tag)

    saved = env_loader._capture_harness_buzz_identity()
    os.environ["BUZZ_PRIVATE_KEY"] = GATEWAY_KEY  # what override=True does
    env_loader._restore_harness_buzz_identity(saved)
    return os.environ.get("BUZZ_PRIVATE_KEY")


def test_attested_key_survives_the_profile_env_override(monkeypatch):
    assert _run_override_load(monkeypatch, auth_tag='["auth","owner","","sig"]') == HARNESS_KEY


def test_without_an_attestation_the_profile_env_still_wins(monkeypatch):
    # A plain gateway run has no harness identity to protect; the profile's own
    # key must keep winning exactly as before.
    assert _run_override_load(monkeypatch, auth_tag=None) == GATEWAY_KEY


def test_capture_is_empty_when_no_key_was_inherited(monkeypatch):
    monkeypatch.setenv("BUZZ_AUTH_TAG", '["auth","owner","","sig"]')
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    assert env_loader._capture_harness_buzz_identity() == {}
