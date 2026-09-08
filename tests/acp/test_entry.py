"""Tests for acp_adapter.entry startup wiring."""

import sys

import acp
import pytest

from acp_adapter import entry


def test_main_enables_unstable_protocol(monkeypatch):
    calls = {}

    async def fake_run_agent(agent, **kwargs):
        calls["kwargs"] = kwargs

    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_load_env", lambda: None)
    monkeypatch.setattr(acp, "run_agent", fake_run_agent)

    entry.main([])

    assert calls["kwargs"]["use_unstable_protocol"] is True


def test_main_skips_configured_mcp_discovery_when_requested(monkeypatch):
    discovery_calls = []

    async def fake_run_agent(agent, **kwargs):
        pass

    monkeypatch.setattr(entry, "_setup_logging", lambda: None)
    monkeypatch.setattr(entry, "_load_env", lambda: None)
    monkeypatch.setenv("HERMES_ACP_SKIP_CONFIGURED_MCP", "1")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.discover_mcp_tools",
        lambda: discovery_calls.append(True),
    )
    monkeypatch.setattr(acp, "run_agent", fake_run_agent)

    entry.main([])

    assert discovery_calls == []


def test_load_env_marks_acp_hosted_and_keeps_host_identity(tmp_path, monkeypatch):
    """Under Buzz Desktop's managed-agent harness the inherited BUZZ_PRIVATE_KEY
    must survive the profile .env load, and the profile must not supply the
    BUZZ_AUTH_TAG that goes with it: the attestation is bound to the signing key,
    so a split identity fails relay verification. _load_env is the first dotenv
    load of a ``hermes-acp`` process, so it is where the marker gets set."""
    import os

    import hermes_cli.env_loader as env_loader

    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text(
        "BUZZ_PRIVATE_KEY=profile-key\nBUZZ_AUTH_TAG=profile-tag\n", encoding="utf-8"
    )
    monkeypatch.setattr(entry, "get_hermes_home", lambda: home)
    # The real _load_env reaches _apply_managed_env(); keep a developer's own managed dir and ambient
    # Buzz env out of the assertions.
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    for key in ("BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL", "BUZZ_MANAGED_AGENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_loader, "_ACP_HOSTED", False)
    monkeypatch.setattr(env_loader, "_ACP_HOST_ENV", {})
    monkeypatch.setattr(env_loader, "_ACP_RESTORE_LOGGED", False)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")

    entry._load_env()

    assert env_loader.is_acp_hosted() is True
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert "BUZZ_AUTH_TAG" not in os.environ










def test_main_setup_offers_browser_install_when_tty(monkeypatch):
    """When stdin is a TTY and the user answers yes, model setup is followed
    by a browser-tools bootstrap call."""
    monkeypatch.setattr("hermes_cli.main.main", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "y")

    bootstrap_calls = []
    monkeypatch.setattr(
        entry,
        "_run_setup_browser",
        lambda assume_yes=False: bootstrap_calls.append(assume_yes) or 0,
    )

    entry.main(["--setup"])

    assert bootstrap_calls == [False]










def test_main_setup_browser_propagates_browser_failure(monkeypatch):
    """If browser install fails, exit code is 1."""
    def fake_ensure(dep, interactive=True):
        return dep != "browser"  # browser fails

    monkeypatch.setattr("hermes_cli.dep_ensure.ensure_dependency", fake_ensure)

    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--setup-browser"])
    assert excinfo.value.code == 1
