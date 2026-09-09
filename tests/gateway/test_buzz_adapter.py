"""Tests for the Buzz platform adapter plugin."""

import asyncio
import base64
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.platforms.base import CachedMedia, SendResult
from gateway.platforms.event import MessageType
from tests.gateway._plugin_adapter_loader import load_plugin_adapter
from gateway.platforms.event import MessageType

# Load plugins/platforms/buzz/adapter.py under a unique module name
# (plugin_adapter_buzz) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_buzz_mod = load_plugin_adapter("buzz")

BuzzAdapter = _buzz_mod.BuzzAdapter
hex_to_npub = _buzz_mod.hex_to_npub
npub_to_hex = _buzz_mod.npub_to_hex
_normalize_user_ref = _buzz_mod._normalize_user_ref
_cli_error_message = _buzz_mod._cli_error_message
_resolve_private_key = _buzz_mod._resolve_private_key
_resolve_auth_tag = _buzz_mod._resolve_auth_tag
_event_reply_parent_id = _buzz_mod._event_reply_parent_id
check_requirements = _buzz_mod.check_requirements
validate_config = _buzz_mod.validate_config
register = _buzz_mod.register
_env_enablement = _buzz_mod._env_enablement
_standalone_send = _buzz_mod._standalone_send

# Real key pair (Chip's public identity — public information, not a secret)
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
SELF_NPUB = "npub1nl2u0wnd8mezfknc74q7pl9ec58h9nrrakce4tnk434qgaxl4psqe5twr6"
OTHER_PUBKEY = "a" * 64
AGENT_PUBKEY = "b" * 64
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
# Real DM conversation as materialized by a hosted relay: `dms list` returns
# [] for it (#68871) while `channels list` shows it as name "DM", empty
# description, indistinguishable from a channel except via message p-tags.
DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"

_ENV_VARS = (
    "BUZZ_RELAY_URL",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_CHANNELS",
    "BUZZ_HOME_CHANNEL",
    "BUZZ_ALLOWED_USERS",
    "BUZZ_REACTION_ONLY_USERS",
    "BUZZ_ALLOW_ALL_USERS",
    "BUZZ_POLL_INTERVAL",
    "BUZZ_AUTH_TAG",
    "BUZZ_CLI_PATH",
    "BUZZ_CREDENTIALS_FILE",
    "BUZZ_AUTH_TAG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Keep tests hermetic: no ambient Buzz env vars or real credentials."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _event(event_id, pubkey=OTHER_PUBKEY, content="hello", created_at=1000, kind=9):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": [["h", CHANNEL]],
    }


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = SELF_NPUB
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    # GatewayRunner installs this callback before intake starts. Attachment
    # tests are authorized by default and override the callback at the boundary.
    adapter.set_authorization_check(lambda *_args: True)
    return adapter


class _ScriptedCli:
    """Fake ``_run_cli`` that routes on the buzz subcommand and records calls."""

    def __init__(self):
        self.responses = {}  # (group, cmd) -> list of (code, stdout, stderr)
        self.calls = []

    def script(self, group, cmd, payload, code=0, stderr=""):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.setdefault((group, cmd), []).append((code, stdout, stderr))

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        queue = self.responses.get((args[0], args[1]), [])
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        return 0, "[]", ""


# ── bech32 / identity helpers ─────────────────────────────────────────────


class TestBech32Helpers:

    def test_hex_to_npub_known_pair(self):
        assert hex_to_npub(SELF_PUBKEY) == SELF_NPUB

    def test_npub_to_hex_known_pair(self):
        assert npub_to_hex(SELF_NPUB) == SELF_PUBKEY


# ── Adapter init / config precedence ──────────────────────────────────────


class TestBuzzAdapterInit:


    def test_init_from_config_extra(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://cfg.relay",
                "channels": ["ccc"],
                "poll_interval": 2,
                "home_channel": "ccc",
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://cfg.relay"
        assert adapter.channels == ["ccc"]
        assert adapter.poll_interval == 2.0
        assert adapter.home_channel == "ccc"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://env.relay")
        from gateway.config import PlatformConfig
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"}))
        assert adapter.relay_url == "https://env.relay"


# ── Multiplex secondary-profile scope (#98738) ─────────────────────────────


@pytest.fixture
def multiplex_scope():
    """Install multiplex + a secondary-profile secret scope; restore after."""

    tokens = []

    def install(scope=None):
        from agent.secret_scope import set_multiplex_active, set_secret_scope

        set_multiplex_active(True)
        tokens.append(set_secret_scope(scope or {}))
        return tokens[-1]

    yield install

    from agent.secret_scope import reset_secret_scope, set_multiplex_active

    for token in reversed(tokens):
        reset_secret_scope(token)
    set_multiplex_active(False)


@pytest.fixture
def default_profile_env(monkeypatch):
    """The default profile's YAML-to-env bridge output in os.environ."""
    monkeypatch.setenv("BUZZ_RELAY_URL", "https://default.relay")
    monkeypatch.setenv("BUZZ_CHANNELS", "chan-a,chan-b,chan-c")
    monkeypatch.setenv("BUZZ_HOME_CHANNEL", "chan-a")
    monkeypatch.setenv("BUZZ_POLL_INTERVAL", "9")
    monkeypatch.setenv("BUZZ_CLI_PATH", "/default/bin/buzz")
    monkeypatch.setenv("BUZZ_TRANSPORT", "poll")
    monkeypatch.setenv("BUZZ_ALLOWED_USERS", "default-user-npub")
    monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", "/default/creds.json")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1default")
    monkeypatch.setenv("BUZZ_AUTH_TAG", '["auth","default-profile-tag","","x"]')


class TestMultiplexProfileScope:

    def test_secondary_extra_wins_over_default_profile_env(
        self, multiplex_scope, default_profile_env, tmp_path
    ):
        """The secondary profile's PlatformConfig is authoritative (#98738)."""
        from gateway.config import PlatformConfig

        cli = tmp_path / "buzz"
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        multiplex_scope()
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://profile.relay",
                "channels": ["pchan"],
                "home_channel": "pchan",
                "poll_interval": 2,
                "cli_path": str(cli),
                "transport": "websocket",
                "allowed_users": [SELF_NPUB],
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://profile.relay"
        assert adapter.channels == ["pchan"]
        assert adapter.home_channel == "pchan"
        assert adapter.poll_interval == 2.0
        assert adapter.cli_path == str(cli)
        assert adapter.transport == "websocket"
        assert adapter._allowed_pubkeys == {SELF_PUBKEY}

    def test_secondary_missing_keys_fail_closed(
        self, multiplex_scope, default_profile_env
    ):
        """Keys absent from the profile's config must NOT borrow the default
        profile's bridged env values — that would connect this adapter to the
        default profile's relay and watch its channels."""
        from gateway.config import PlatformConfig

        multiplex_scope()
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter.relay_url == ""
        assert adapter.channels == []
        assert adapter.home_channel == ""
        assert adapter.poll_interval == _buzz_mod._DEFAULT_POLL_INTERVAL
        assert adapter.transport == "auto"
        assert adapter._allowed_pubkeys == set()

    def test_secondary_credentials_file_not_borrowed(
        self, multiplex_scope, default_profile_env, tmp_path, monkeypatch
    ):
        """BUZZ_CREDENTIALS_FILE in env points at the DEFAULT profile's key
        file; the scoped adapter must not read the default identity's key."""
        default_creds = tmp_path / "default-creds.json"
        default_creds.write_text(
            json.dumps({"nsec": "nsec1default-identity"}), encoding="utf-8"
        )
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(default_creds))
        multiplex_scope()
        # Scope has no key: the profile is unconfigured and must fail closed
        # to "" rather than resolving the default profile's credentials.
        assert _buzz_mod._resolve_private_key({}) == ""

    def test_default_profile_unscoped_keeps_env_precedence(
        self, monkeypatch, default_profile_env
    ):
        """Multiplex ON but no scope (the DEFAULT profile constructs
        unscoped): env is its own bridge output and still wins."""
        from agent.secret_scope import set_multiplex_active
        from gateway.config import PlatformConfig

        set_multiplex_active(True)
        try:
            adapter = BuzzAdapter(
                PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"})
            )
        finally:
            set_multiplex_active(False)
        assert adapter.relay_url == "https://default.relay"

    def test_check_requirements_scoped_reads_profile_config(
        self, multiplex_scope, default_profile_env, tmp_path
    ):
        """The gate must consult the profile's own config.yaml + secret scope,
        not the default profile's env values."""
        import yaml
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"nsec": "nsec1profile"}), encoding="utf-8")
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "gateway": {
                        "platforms": {
                            "buzz": {
                                "enabled": True,
                                "extra": {
                                    "relay_url": "https://profile.relay",
                                    "credentials_file": str(creds),
                                },
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        multiplex_scope()
        token = set_hermes_home_override(str(tmp_path))
        try:
            # The default profile's env relay+key must NOT pass the gate on
            # their own for a profile without a buzz config...
            assert check_requirements() is True  # profile config passes
        finally:
            reset_hermes_home_override(token)

        # A profile whose config.yaml has no buzz entry fails closed even
        # though the default profile's env values are present.
        empty_home = tmp_path / "empty-profile"
        empty_home.mkdir()
        multiplex_scope()
        token = set_hermes_home_override(str(empty_home))
        try:
            assert check_requirements() is False
        finally:
            reset_hermes_home_override(token)

    def test_env_enablement_scoped_returns_none(self, multiplex_scope, default_profile_env):
        """Scoped env enablement must not fabricate Buzz for a profile from
        the default profile's env values."""
        multiplex_scope()
        assert _env_enablement() is None

    def test_apply_yaml_config_scoped_skips_env_bridge(
        self, multiplex_scope, default_profile_env, monkeypatch
    ):
        """A secondary profile's YAML values must not be pinned into the
        process env for every other profile (first-writer-wins)."""
        for var in ("BUZZ_RELAY_URL", "BUZZ_HOME_CHANNEL", "BUZZ_CHANNELS"):
            monkeypatch.delenv(var, raising=False)
        multiplex_scope()
        _buzz_mod._apply_yaml_config(
            {},
            {"extra": {"relay_url": "https://profile.relay", "home_channel": "pchan"}},
        )
        import os as _os

        assert "BUZZ_RELAY_URL" not in _os.environ
        assert "BUZZ_HOME_CHANNEL" not in _os.environ
        assert "BUZZ_CHANNELS" not in _os.environ

    def test_standalone_send_scoped_uses_profile_extra(
        self, multiplex_scope, default_profile_env, monkeypatch, tmp_path
    ):
        multiplex_scope()
        from gateway.config import PlatformConfig

        cli = tmp_path / "buzz"
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        calls = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=None):
            calls["relay"] = relay_url
            return 0, '{"accepted": true, "event_id": "e1"}', ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        monkeypatch.setattr(
            _buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1profile"
        )
        result = asyncio.run(
            _standalone_send(
                PlatformConfig(
                    enabled=True,
                    extra={"relay_url": "https://profile.relay", "cli_path": str(cli)},
                ),
                "chan-x",
                "hello",
            )
        )
        assert result.get("success") is True
        assert calls["relay"] == "https://profile.relay"

    def test_secondary_partial_extra_fills_missing_keys_from_defaults(
        self, multiplex_scope, default_profile_env
    ):
        """Partial config: configured keys win, unconfigured keys fall back to
        their own defaults — never to the default profile's env values."""
        from gateway.config import PlatformConfig

        multiplex_scope()
        adapter = BuzzAdapter(
            PlatformConfig(enabled=True, extra={"relay_url": "https://profile.relay"})
        )
        assert adapter.relay_url == "https://profile.relay"
        assert adapter.channels == []
        assert adapter.home_channel == ""
        assert adapter.poll_interval == _buzz_mod._DEFAULT_POLL_INTERVAL
        assert adapter.transport == "auto"
        assert adapter._allowed_pubkeys == set()

    def test_ws_auth_tag_not_borrowed_from_default_profile_env(
        self, multiplex_scope, default_profile_env
    ):
        """BUZZ_AUTH_TAG is per-identity NIP-OA owner attestation: a scoped
        secondary profile without one must not sign its NIP-42 auth event
        with the default profile's tag from os.environ (#98738)."""
        import asyncio as _asyncio

        multiplex_scope()
        adapter = BuzzAdapter.__new__(BuzzAdapter)
        adapter._private_key = "00" * 31 + "03"
        adapter._websocket_url = lambda: "wss://relay.example"

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def recv(self):
                if self.sent:
                    return json.dumps(["OK", self.sent[0][1]["id"], True, "ok"])
                return json.dumps(["AUTH", "challenge-1"])

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        ws = _FakeWS()
        _asyncio.run(adapter._authenticate_websocket(ws))
        tags = [t for t in ws.sent[0][1]["tags"] if t and t[0] == "auth"]
        assert tags == []

    def test_ws_auth_tag_scoped_profile_uses_its_own_tag(
        self, multiplex_scope
    ):
        """Positive control: a tag present in the profile's own secret scope
        IS attached to the NIP-42 auth event."""
        import asyncio as _asyncio

        profile_tag = json.dumps(["auth", "p" * 64, "", "q" * 128])
        multiplex_scope({"BUZZ_AUTH_TAG": profile_tag})
        adapter = BuzzAdapter.__new__(BuzzAdapter)
        adapter._private_key = "00" * 31 + "03"
        adapter._websocket_url = lambda: "wss://relay.example"

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def recv(self):
                if self.sent:
                    return json.dumps(["OK", self.sent[0][1]["id"], True, "ok"])
                return json.dumps(["AUTH", "challenge-1"])

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        ws = _FakeWS()
        _asyncio.run(adapter._authenticate_websocket(ws))
        tags = [t for t in ws.sent[0][1]["tags"] if t and t[0] == "auth"]
        assert tags == [json.loads(profile_tag)]

    def test_ws_auth_tag_unscoped_default_profile_keeps_env(
        self, default_profile_env
    ):
        """The default profile constructs unscoped even under multiplex, so
        its env-provided auth tag still applies (legacy behavior kept)."""
        import asyncio as _asyncio

        from agent.secret_scope import set_multiplex_active

        set_multiplex_active(True)
        try:
            adapter = BuzzAdapter.__new__(BuzzAdapter)
            adapter._private_key = "00" * 31 + "03"
            adapter._websocket_url = lambda: "wss://relay.example"

            class _FakeWS:
                def __init__(self):
                    self.sent = []

                async def recv(self):
                    if self.sent:
                        return json.dumps(["OK", self.sent[0][1]["id"], True, "ok"])
                    return json.dumps(["AUTH", "challenge-1"])

                async def send(self, raw):
                    self.sent.append(json.loads(raw))

            ws = _FakeWS()
            _asyncio.run(adapter._authenticate_websocket(ws))
        finally:
            set_multiplex_active(False)
        tags = [t for t in ws.sent[0][1]["tags"] if t and t[0] == "auth"]
        assert tags == [["auth", "default-profile-tag", "", "x"]]

    def test_validate_config_scoped_extra_is_authoritative(
        self, multiplex_scope, default_profile_env, tmp_path
    ):
        """Scoped validation reads the profile's extra, not the default
        profile's env relay/key: unconfigured fails closed, configured
        passes via its own credentials file."""
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"nsec": "nsec1profile"}), encoding="utf-8")
        from gateway.config import PlatformConfig

        multiplex_scope()
        assert validate_config(PlatformConfig(enabled=True, extra={})) is False
        assert (
            validate_config(
                PlatformConfig(
                    enabled=True,
                    extra={
                        "relay_url": "https://profile.relay",
                        "credentials_file": str(creds),
                    },
                )
            )
            is True
        )

    def test_validate_config_unscoped_keeps_env_precedence(
        self, default_profile_env
    ):
        """Single-profile/unscoped: env relay + env key still validate even
        with an empty extra mapping."""
        from gateway.config import PlatformConfig

        assert validate_config(PlatformConfig(enabled=True, extra={})) is True

    def test_standalone_send_scoped_target_falls_back_to_profile_home(
        self, multiplex_scope, default_profile_env, monkeypatch, tmp_path
    ):
        """With no explicit chat_id, the scoped standalone send targets the
        profile's own home_channel — never the default profile's env one."""
        multiplex_scope()
        from gateway.config import PlatformConfig

        cli = tmp_path / "buzz"
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        calls = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=None):
            calls["args"] = args
            return 0, '{"accepted": true, "event_id": "e1"}', ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        monkeypatch.setattr(
            _buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1profile"
        )
        result = asyncio.run(
            _standalone_send(
                PlatformConfig(
                    enabled=True,
                    extra={
                        "relay_url": "https://profile.relay",
                        "cli_path": str(cli),
                        "home_channel": "pchan",
                    },
                ),
                "",
                "hello",
            )
        )
        assert result.get("success") is True
        assert calls["args"][calls["args"].index("--channel") + 1] == "pchan"

    def test_standalone_send_scoped_without_target_fails_closed(
        self, multiplex_scope, default_profile_env, monkeypatch, tmp_path
    ):
        """No chat_id and no profile home_channel: the error is returned —
        the default profile's env BUZZ_HOME_CHANNEL must not be borrowed."""
        multiplex_scope()
        from gateway.config import PlatformConfig

        cli = tmp_path / "buzz"
        cli.write_text("#!/bin/sh\n", encoding="utf-8")

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=None):
            raise AssertionError("CLI must not run without a resolved target")

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        monkeypatch.setattr(
            _buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1profile"
        )
        result = asyncio.run(
            _standalone_send(
                PlatformConfig(
                    enabled=True,
                    extra={"relay_url": "https://profile.relay", "cli_path": str(cli)},
                ),
                "",
                "hello",
            )
        )
        assert result == {
            "error": "Buzz standalone send: no target channel (set BUZZ_HOME_CHANNEL)"
        }


# ── CLI error contract ────────────────────────────────────────────────────


class TestCliErrorContract:

    def test_parses_json_error(self):
        msg = _cli_error_message('{"error":"relay_error","message":"boom","retryable":false}', 2)
        assert "relay_error" in msg and "boom" in msg and "exit 2" in msg

    @pytest.mark.parametrize(
        "stderr",
        [
            "x" * 100_000,
            json.dumps({"error": "relay_error", "message": "x" * 100_000}),
        ],
    )
    def test_bounds_untrusted_cli_error_output(self, stderr):
        msg = _cli_error_message(stderr, 2)
        assert len(msg) <= 900
        assert msg.endswith("...")


# ── Seeding / high-water mark / de-dupe ───────────────────────────────────


class TestPollingDedupe:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_seed_sets_high_water_mark_without_dispatch(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip old history", created_at=100),
            _event("e2", content="@Chip newer history", created_at=200),
        ])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        state = adapter._channel_state[CHANNEL]
        assert state["last_ts"] == 200
        assert set(state["seen"]) == {"e1", "e2"}
        # Seeding must never replay history into the agent
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_new_event_dispatched_once(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", content="@Chip hi", created_at=100)])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Poll 1: seeded event + a genuinely new mention
        cli.responses.clear()
        cli.script("messages", "get", [
            _event("e1", content="@Chip hi", created_at=100),
            _event("e2", content="hey @Chip, ping", created_at=150),
        ])
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["e2"]
        assert adapter._dispatched[0]["text"] == "hey @Chip, ping"
        assert adapter._channel_state[CHANNEL]["last_ts"] == 150

        # Poll 2: identical response — the seen-id set must de-dupe
        await adapter._poll_channel(CHANNEL)
        assert len(adapter._dispatched) == 1

    @pytest.mark.asyncio
    async def test_malformed_attachment_url_does_not_abort_following_event(self, adapter):
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        malformed = _event("malformed-attachment", content="background chatter", created_at=159)
        malformed["tags"].append([
            "imeta",
            "url https://[invalid/media.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename invalid.bin",
        ])
        valid = _event("following-valid", content="@Chip still there?", created_at=160)
        cli = _ScriptedCli()
        cli.script("messages", "get", [malformed, valid])
        adapter._run_cli = cli

        await adapter._poll_channel(CHANNEL)

        assert [item["message_id"] for item in adapter._dispatched] == ["following-valid"]

    @pytest.mark.asyncio
    async def test_addressed_attachment_is_cached_and_dispatched_to_agent(self, adapter):
        attachment = CachedMedia(
            path="/agent/cache/doc_handoff.txt",
            media_type="text/plain",
            kind="document",
            display_name="handoff.txt",
        )
        adapter._download_attachment = AsyncMock(return_value=attachment)
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("attachment-event", content="@Chip inspect this", created_at=160)
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/abc.bin",
            "m text/plain",
            "x " + "a" * 64,
            "size 12",
            "filename handoff.txt",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        adapter._download_attachment.assert_awaited_once()
        dispatched = adapter._dispatched[-1]
        assert dispatched["media_urls"] == [attachment.path]
        assert dispatched["media_types"] == ["text/plain"]
        assert dispatched["message_type"] is MessageType.DOCUMENT
        assert attachment.context_note() not in dispatched["text"]
        assert dispatched["raw_message"] is event


class TestInboundAttachments:

    def test_imeta_total_declared_bytes_are_bounded(self):
        event = _event("bounded", content="@Chip files")
        per_file_size = 6 * 1024 * 1024
        for index in range(4):
            event["tags"].append([
                "imeta",
                f"url https://test.relay/media/{index}.bin",
                "m application/octet-stream",
                "x " + format(index + 1, "064x"),
                f"size {per_file_size}",
                f"filename {index}.bin",
            ])

        attachments = BuzzAdapter._imeta_attachments(event)

        assert len(attachments) == 3
        assert sum(item["size"] for item in attachments) <= 20 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_download_caches_only_exact_size_and_sha256(self, monkeypatch):
        import httpx

        payload = b"verified Buzz attachment"
        digest = hashlib.sha256(payload).hexdigest()
        real_async_client = httpx.AsyncClient

        def handler(request):
            assert request.url.host == "test.relay"
            return httpx.Response(
                200,
                content=payload,
                headers={"content-length": str(len(payload))},
            )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(handler),
                **kwargs,
            ),
        )
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://test.relay/media/verified.bin",
            "sha256": digest,
            "size": len(payload),
            "filename": "verified.txt",
            "mime_type": "text/plain",
        })

        assert cached is not None
        assert cached.kind == "document"
        assert cached.media_type == "text/plain"
        assert Path(cached.path).read_bytes() == payload

    @pytest.mark.asyncio
    async def test_download_rejects_untrusted_attachment_host_before_network(self, monkeypatch):
        import httpx

        def must_not_create_client(**kwargs):
            raise AssertionError("network client must not be created")

        monkeypatch.setattr(httpx, "AsyncClient", must_not_create_client)
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://untrusted.example/media/file.bin",
            "sha256": "a" * 64,
            "size": 12,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is None

    @pytest.mark.asyncio
    async def test_download_rejects_unconfigured_nondefault_port_before_network(self, monkeypatch):
        import httpx

        def must_not_create_client(**kwargs):
            raise AssertionError("network client must not be created")

        monkeypatch.setattr(httpx, "AsyncClient", must_not_create_client)
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://test.relay:8443/media/file.bin",
            "sha256": "a" * 64,
            "size": 12,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is None

    @pytest.mark.asyncio
    async def test_download_allows_explicitly_configured_nondefault_port(self, monkeypatch):
        import httpx

        payload = b"x"
        real_async_client = httpx.AsyncClient

        def handler(request):
            assert request.url.port == 8443
            return httpx.Response(200, content=payload, headers={"content-length": "1"})

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(handler),
                **kwargs,
            ),
        )
        adapter = _make_adapter({"attachment_hosts": ["test.relay:8443"]})

        cached = await adapter._download_attachment({
            "url": "https://test.relay:8443/media/file.bin",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": 1,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is not None

    @pytest.mark.asyncio
    async def test_unaddressed_channel_attachment_is_not_downloaded(self):
        adapter = _make_adapter()
        authorization_check = MagicMock(return_value=True)
        adapter.set_authorization_check(authorization_check)
        adapter._cache_inbound_attachments = AsyncMock()
        adapter._download_attachment = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("unaddressed-attachment", content="shared file")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 12",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        authorization_check.assert_not_called()
        adapter._cache_inbound_attachments.assert_not_awaited()
        adapter._download_attachment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_attachment_download_is_visible_to_agent(self):
        adapter = _make_adapter()
        adapter._download_attachment = AsyncMock(return_value=None)
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        dispatched = []

        async def capture(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture
        event = _event("failed-attachment", content="@Chip inspect this")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 12",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        assert "could not be downloaded" in dispatched[-1]["text"]
        assert dispatched[-1]["media_urls"] == []

    @pytest.mark.asyncio
    async def test_dispatch_builds_document_message_event_with_cached_path(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()

        await adapter._dispatch_message(
            text="inspect",
            chat_id=CHANNEL,
            chat_type="group",
            user_id=OTHER_PUBKEY,
            user_name="Other",
            message_id="document-event",
            created_at=1000,
            media_urls=["/agent/cache/doc_report.pdf"],
            media_types=["application/pdf"],
            message_type=MessageType.DOCUMENT,
            raw_message={"id": "document-event"},
        )

        call = adapter.handle_message.await_args
        assert call is not None
        dispatched_event = call.args[0]
        assert dispatched_event.message_type is MessageType.DOCUMENT
        assert dispatched_event.media_urls == ["/agent/cache/doc_report.pdf"]
        assert dispatched_event.media_types == ["application/pdf"]
        assert dispatched_event.media_text_inlined == [False]
        assert dispatched_event.raw_message == {"id": "document-event"}

    def test_imeta_sanitizes_filename_and_rejects_incomplete_metadata(self):
        event = _event("metadata", content="@Chip files")
        event["tags"].extend([
            [
                "imeta",
                "url https://test.relay/media/valid.bin",
                "m text/plain",
                "x " + "a" * 64,
                "size 12",
                "filename ../../private/report.txt",
            ],
            [
                "imeta",
                "url http://test.relay/media/insecure.bin",
                "x " + "b" * 64,
                "size 12",
                "filename insecure.bin",
            ],
            [
                "imeta",
                "url https://test.relay/media/no-hash.bin",
                "size 12",
                "filename no-hash.bin",
            ],
        ])

        attachments = BuzzAdapter._imeta_attachments(event)

        assert len(attachments) == 1
        assert attachments[0]["filename"] == "report.txt"

    @pytest.mark.asyncio
    async def test_attachment_only_dm_is_downloaded_and_dispatched(self):
        adapter = _make_adapter()
        attachment = CachedMedia(
            path="/agent/cache/doc_attachment.bin",
            media_type="application/octet-stream",
            kind="document",
            display_name="attachment.bin",
        )
        adapter._download_attachment = AsyncMock(return_value=attachment)
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[DM_CHANNEL] = {
            "chat_type": "dm",
            "last_ts": 0,
            "seen": {},
        }
        dispatched = []

        async def capture(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture
        event = _event("attachment-only", content="")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 12",
            "filename attachment.bin",
        ])

        await adapter._handle_event(
            DM_CHANNEL,
            adapter._channel_state[DM_CHANNEL],
            event,
        )

        adapter._download_attachment.assert_awaited_once()
        assert dispatched[-1]["media_urls"] == [attachment.path]
        assert dispatched[-1]["message_type"] is MessageType.DOCUMENT

    @pytest.mark.asyncio
    async def test_malformed_attachment_only_dm_dispatches_once_with_bounded_note(self):
        adapter = _make_adapter()
        adapter._download_attachment = AsyncMock()
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[DM_CHANNEL] = {
            "chat_type": "dm",
            "last_ts": 0,
            "seen": {},
        }
        adapter._dispatch_message = AsyncMock()
        event = _event("malformed-attachment-only", content="")
        event["tags"].append(["imeta", "url definitely-not-a-url"])

        await adapter._handle_event(DM_CHANNEL, adapter._channel_state[DM_CHANNEL], event)
        await adapter._handle_event(DM_CHANNEL, adapter._channel_state[DM_CHANNEL], event)

        adapter._download_attachment.assert_not_awaited()
        adapter._dispatch_message.assert_awaited_once()
        call = adapter._dispatch_message.await_args
        assert call is not None
        dispatched = call.kwargs
        assert "1 Buzz attachment(s) rejected" in dispatched["text"]
        assert len(dispatched["text"]) <= 80
        assert dispatched["media_urls"] == []

    @pytest.mark.asyncio
    async def test_excess_imeta_is_reported_while_accepted_files_remain_attached(self):
        adapter = _make_adapter()
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        adapter._download_attachment = AsyncMock(
            side_effect=lambda metadata: CachedMedia(
                f"/cache/{metadata['filename']}",
                metadata["mime_type"],
                "document",
                metadata["filename"],
            )
        )
        adapter._dispatch_message = AsyncMock()
        event = _event("excess-imeta", content="@Chip inspect")
        for index in range(5):
            event["tags"].append([
                "imeta",
                f"url https://test.relay/media/{index}.bin",
                "m application/octet-stream",
                "x " + format(index + 1, "064x"),
                "size 1",
                f"filename {index}.bin",
            ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        assert adapter._download_attachment.await_count == 4
        call = adapter._dispatch_message.await_args
        assert call is not None
        dispatched = call.kwargs
        assert "1 Buzz attachment(s) rejected" in dispatched["text"]
        assert len(dispatched["media_urls"]) == 4

    def test_imeta_bounds_filename_to_filesystem_safe_utf8_length(self):
        event = _event("long-name", content="@Chip file")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename " + ("é" * 180) + ".pdf",
        ])

        filename = BuzzAdapter._imeta_attachments(event)[0]["filename"]

        assert len(filename.encode("utf-8")) <= 120
        assert filename.endswith(".pdf")

    @pytest.mark.asyncio
    async def test_cache_write_failure_is_treated_as_failed_attachment(self, monkeypatch):
        import httpx

        payload = b"x"
        digest = hashlib.sha256(payload).hexdigest()
        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        content=payload,
                        headers={"content-length": "1"},
                    )
                ),
                **kwargs,
            ),
        )
        monkeypatch.setattr(
            _buzz_mod,
            "cache_media_bytes_async",
            AsyncMock(side_effect=OSError(36, "File name too long")),
        )
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://test.relay/media/file.bin",
            "sha256": digest,
            "size": 1,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is None

    @pytest.mark.asyncio
    async def test_download_has_total_deadline(self, monkeypatch):
        import httpx

        payload = b"x"
        real_async_client = httpx.AsyncClient

        async def slow_handler(_request):
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=payload, headers={"content-length": "1"})

        monkeypatch.setattr(_buzz_mod, "_ATTACHMENT_DOWNLOAD_TIMEOUT", 0.01)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(slow_handler),
                **kwargs,
            ),
        )
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://test.relay/media/file.bin",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": 1,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is None

    @pytest.mark.asyncio
    async def test_multiple_mixed_attachments_use_document_semantics(self):
        adapter = _make_adapter()
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        cached = [
            CachedMedia("/cache/image.png", "image/png", "image", "image.png"),
            CachedMedia("/cache/audio.mp3", "audio/mpeg", "audio", "audio.mp3"),
        ]
        adapter._cache_inbound_attachments = AsyncMock(return_value=cached)
        dispatched = []

        async def capture(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture
        event = _event("mixed", content="@Chip inspect")
        for index, mime_type in enumerate(("image/png", "audio/mpeg")):
            event["tags"].append([
                "imeta",
                f"url https://test.relay/media/{index}.bin",
                f"m {mime_type}",
                "x " + format(index + 1, "064x"),
                "size 1",
                f"filename {index}.bin",
            ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        assert dispatched[-1]["message_type"] is MessageType.DOCUMENT
        assert dispatched[-1]["media_types"] == ["image/png", "audio/mpeg"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "payload", "headers", "declared_size", "expected_digest"),
        [
            (302, b"", {}, 1, "a" * 64),
            (200, b"x", {"content-length": "invalid"}, 1, hashlib.sha256(b"x").hexdigest()),
            (200, b"x", {}, 2, hashlib.sha256(b"x").hexdigest()),
            (200, b"xx", {}, 1, hashlib.sha256(b"xx").hexdigest()),
            (200, b"x", {}, 1, "a" * 64),
        ],
    )
    async def test_download_rejects_invalid_response_or_integrity(
        self,
        monkeypatch,
        status,
        payload,
        headers,
        declared_size,
        expected_digest,
    ):
        import httpx

        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(status, content=payload, headers=headers)
                ),
                **kwargs,
            ),
        )
        adapter = _make_adapter()

        cached = await adapter._download_attachment({
            "url": "https://test.relay/media/file.bin",
            "sha256": expected_digest,
            "size": declared_size,
            "filename": "file.bin",
            "mime_type": "application/octet-stream",
        })

        assert cached is None

    def test_imeta_rejects_url_credentials_and_fragments_and_caps_items(self):
        event = _event("url-safety", content="@Chip files")
        event["tags"].extend([
            [
                "imeta",
                "url https://user:password@test.relay/media/private.bin",
                "x " + "a" * 64,
                "size 1",
                "filename private.bin",
            ],
            [
                "imeta",
                "url https://test.relay/media/fragment.bin#hidden",
                "x " + "b" * 64,
                "size 1",
                "filename fragment.bin",
            ],
        ])
        for index in range(6):
            event["tags"].append([
                "imeta",
                f"url https://test.relay/media/{index}.bin",
                "x " + format(index + 1, "064x"),
                "size 1",
                f"filename {index}.bin",
            ])

        attachments = BuzzAdapter._imeta_attachments(event)

        assert len(attachments) == 4
        assert all("@" not in item["url"] and "#" not in item["url"] for item in attachments)

    @pytest.mark.asyncio
    async def test_self_attachment_stops_before_authorization_or_cache(self):
        adapter = _make_adapter()
        authorization_check = MagicMock(return_value=True)
        adapter.set_authorization_check(authorization_check)
        adapter._cache_inbound_attachments = AsyncMock()
        adapter._dispatch_message = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("self-attachment", pubkey=SELF_PUBKEY, content="@Chip inspect")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        authorization_check.assert_not_called()
        adapter._cache_inbound_attachments.assert_not_awaited()
        adapter._dispatch_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unauthorized_sender_attachment_is_not_downloaded(self):
        adapter = _make_adapter()
        adapter._allowed_pubkeys = {"f" * 64}
        authorization_check = MagicMock(return_value=True)
        adapter.set_authorization_check(authorization_check)
        adapter._cache_inbound_attachments = AsyncMock()
        adapter._download_attachment = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("unauthorized-attachment", content="@Chip inspect")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        authorization_check.assert_not_called()
        adapter._cache_inbound_attachments.assert_not_awaited()
        adapter._download_attachment.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authorization", [False, None, "raise", "truthy"])
    async def test_non_true_gateway_authority_never_caches_even_for_locally_allowed_sender(
        self,
        authorization,
    ):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(side_effect=AssertionError("authorization test invoked Buzz CLI"))
        adapter._resolve_user_name = AsyncMock(return_value="Other")
        adapter._allowed_pubkeys = {OTHER_PUBKEY}
        if authorization is None:
            adapter.set_authorization_check(None)
        elif authorization == "raise":
            def raise_unknown(*_args):
                raise RuntimeError("authorization backend unavailable")

            adapter.set_authorization_check(raise_unknown)
        elif authorization == "truthy":
            adapter.set_authorization_check(lambda *_args: "AUTHORIZED")
        else:
            adapter.set_authorization_check(lambda *_args: False)
        adapter._cache_inbound_attachments = AsyncMock()
        adapter._dispatch_message = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event(f"gateway-{authorization}-attachment", content="@Chip inspect")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        adapter._cache_inbound_attachments.assert_not_awaited()
        adapter._dispatch_message.assert_awaited_once()
        call = adapter._dispatch_message.await_args
        assert call is not None
        assert call.kwargs["media_urls"] == []

    @pytest.mark.asyncio
    async def test_explicit_true_gateway_authority_caches_attachment(self):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(side_effect=AssertionError("authorization test invoked Buzz CLI"))
        adapter._resolve_user_name = AsyncMock(return_value="Other")
        authorization_check = MagicMock(return_value=True)
        adapter.set_authorization_check(authorization_check)
        cached = CachedMedia(
            "/cache/authorized.bin",
            "application/octet-stream",
            "document",
            "authorized.bin",
        )
        adapter._cache_inbound_attachments = AsyncMock(return_value=[cached])
        adapter._dispatch_message = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("gateway-authorized-attachment", content="@Chip inspect")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        authorization_check.assert_called_once_with(OTHER_PUBKEY, "group", CHANNEL)
        adapter._cache_inbound_attachments.assert_awaited_once()
        call = adapter._dispatch_message.await_args
        assert call is not None
        assert call.kwargs["media_urls"] == [cached.path]

    @pytest.mark.asyncio
    async def test_real_gateway_auth_callback_defaults_to_no_attachment_side_effects(
        self,
        monkeypatch,
    ):
        from gateway.config import GatewayConfig
        from gateway.run import GatewayRunner

        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner.adapters = {}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(side_effect=AssertionError("authorization test invoked Buzz CLI"))
        adapter._resolve_user_name = AsyncMock(return_value="Other")
        adapter._allowed_pubkeys = {OTHER_PUBKEY}
        adapter.set_authorization_check(runner._make_adapter_auth_check(adapter.platform))
        adapter._cache_inbound_attachments = AsyncMock()
        adapter._dispatch_message = AsyncMock()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        event = _event("gateway-default-denied-attachment", content="@Chip inspect")
        event["tags"].append([
            "imeta",
            "url https://test.relay/media/file.bin",
            "m application/octet-stream",
            "x " + "a" * 64,
            "size 1",
            "filename file.bin",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        runner.pairing_store.is_approved.assert_called_once_with("buzz", OTHER_PUBKEY)
        adapter._cache_inbound_attachments.assert_not_awaited()
        adapter._dispatch_message.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "mime_type", "expected_type"),
        [
            ("image", "image/png", MessageType.PHOTO),
            ("video", "video/mp4", MessageType.VIDEO),
            ("audio", "audio/mpeg", MessageType.AUDIO),
            ("document", "application/pdf", MessageType.DOCUMENT),
        ],
    )
    async def test_homogeneous_attachment_kind_sets_message_type(
        self,
        kind,
        mime_type,
        expected_type,
    ):
        adapter = _make_adapter()
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        cached = CachedMedia(
            f"/cache/file-{kind}",
            mime_type,
            kind,
            f"file-{kind}",
        )
        adapter._cache_inbound_attachments = AsyncMock(return_value=[cached])
        dispatched = []

        async def capture(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture
        event = _event(f"homogeneous-{kind}", content="@Chip inspect")
        event["tags"].append([
            "imeta",
            f"url https://test.relay/media/{kind}",
            f"m {mime_type}",
            "x " + "a" * 64,
            "size 1",
            f"filename file-{kind}",
        ])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        assert dispatched[-1]["message_type"] is expected_type


# ── Mention gating / DMs / authorization ──────────────────────────────────


class TestMentionGating:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_ignored(self, adapter):
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_name_mention_dispatched(self, adapter):
        await self._poll_with(adapter, _event("e1", content="hey @Chip can you help?", created_at=10))
        assert len(adapter._dispatched) == 1
        assert adapter._dispatched[0]["text"] == "hey @Chip can you help?"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            "Chip should stay silent",
            "ask @Chipmunk instead",
            "ask @Chip-bot instead",
            "email chip@example.com",
        ],
    )
    async def test_bare_or_prefix_name_does_not_dispatch(self, adapter, content):
        await self._poll_with(adapter, _event("e1", content=content, created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identity", [SELF_NPUB, SELF_PUBKEY])
    async def test_identity_text_dispatches(self, adapter, identity):
        await self._poll_with(
            adapter,
            _event("e1", content=f"please check {identity}", created_at=10),
        )
        assert len(adapter._dispatched) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [f"a{SELF_PUBKEY}b", f"x{SELF_NPUB}y"],
    )
    async def test_identity_substring_does_not_dispatch(self, adapter, content):
        await self._poll_with(
            adapter,
            _event("e1", content=content, created_at=10),
        )
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_signed_recipient_tag_dispatches_without_text_mention(self, adapter):
        event = _event("e1", content="please take a look", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])
        await self._poll_with(adapter, event)
        assert len(adapter._dispatched) == 1

    @pytest.mark.asyncio
    async def test_other_recipient_tag_does_not_dispatch(self, adapter):
        event = _event("e1", content="please take a look", created_at=10)
        event["tags"].append(["p", "b" * 64])
        await self._poll_with(adapter, event)
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_require_mention_false_still_dispatches_unaddressed_message(self, adapter):
        adapter.require_mention = False
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert len(adapter._dispatched) == 1

    def test_strip_mention_requires_at_for_display_name(self, adapter):
        assert adapter._strip_mention("@Chip: /whoami") == "/whoami"
        assert adapter._strip_mention("Chip: please review") == "Chip: please review"
        assert adapter._strip_mention("@Chip-bot: please review") == "@Chip-bot: please review"


    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized(self, adapter):
        adapter._allowed_pubkeys = {"b" * 64}
        await self._poll_with(adapter, _event("e1", content="@Chip hello", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_explicit_agent_tag_reacts_without_dispatch(self, adapter):
        adapter._allowed_pubkeys = {OTHER_PUBKEY}
        adapter._reaction_only_pubkeys = {AGENT_PUBKEY}
        adapter.send_reaction = AsyncMock(return_value=True)
        event = _event("e1", pubkey=AGENT_PUBKEY, content="@Chip coordinate", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])

        await self._poll_with(adapter, event)

        adapter.send_reaction.assert_awaited_once_with(CHANNEL, "e1", "👀")
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_allowlist_takes_precedence_over_reaction_only(self, adapter):
        adapter._allowed_pubkeys = {AGENT_PUBKEY}
        adapter._reaction_only_pubkeys = {AGENT_PUBKEY}
        adapter.send_reaction = AsyncMock(return_value=True)
        event = _event("e1", pubkey=AGENT_PUBKEY, content="@Chip coordinate", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])

        await self._poll_with(adapter, event)

        adapter.send_reaction.assert_not_awaited()
        assert len(adapter._dispatched) == 1

    @pytest.mark.asyncio
    async def test_agent_message_without_explicit_recipient_gets_no_reaction(self, adapter):
        adapter._allowed_pubkeys = {OTHER_PUBKEY}
        adapter._reaction_only_pubkeys = {AGENT_PUBKEY}
        adapter.send_reaction = AsyncMock(return_value=True)

        await self._poll_with(
            adapter,
            _event("e1", pubkey=AGENT_PUBKEY, content="@Chip coordinate", created_at=10),
        )

        adapter.send_reaction.assert_not_awaited()
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_unknown_sender_tag_gets_no_reaction(self, adapter):
        adapter._allowed_pubkeys = {OTHER_PUBKEY}
        adapter._reaction_only_pubkeys = {AGENT_PUBKEY}
        adapter.send_reaction = AsyncMock(return_value=True)
        event = _event("e1", pubkey="c" * 64, content="@Chip coordinate", created_at=10)
        event["tags"].append(["p", SELF_PUBKEY])

        await self._poll_with(adapter, event)

        adapter.send_reaction.assert_not_awaited()
        assert adapter._dispatched == []


# ── NIP-10 thread replies as addressed (issue #75826) ────────────────────
#
# With require_mention (default), channel replies whose direct parent is the
# agent's own message must dispatch even when the text has no @name — Buzz
# Desktop's natural reply affordance for /approve never types a mention.


def _tagged_event(event_id, channel, *, content, pubkey=OTHER_PUBKEY,
                  created_at=1000, kind=9, p=None, reply_to=None, root=None):
    """Event with the tag shapes observed on a live relay (h/p/e tags)."""
    tags = [["h", channel]]
    # NIP-10 order as Desktop emits: root first, then reply (when both set).
    if root:
        tags.append(["e", root, "", "root"])
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


class TestNip10ThreadReplyMentionGate:
    """require_mention + NIP-10 reply-to-own-message (#75826)."""

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = a._new_channel_state("group")
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    def test_event_reply_parent_prefers_reply_marker(self):
        ev = _tagged_event(
            "child", CHANNEL, content="ok", root="root-id", reply_to="parent-id"
        )
        assert _event_reply_parent_id(ev) == "parent-id"
        assert _event_reply_parent_id(
            _tagged_event("c2", CHANNEL, content="ok", root="only-root")
        ) == "only-root"

    @pytest.mark.asyncio
    async def test_thread_reply_to_own_message_dispatches_without_mention(self, adapter):
        # Live agent prompt lands first (self-echo is cached, not dispatched).
        await self._poll_with(
            adapter,
            _tagged_event(
                "agent-prompt",
                CHANNEL,
                content="⚠️ Dangerous command requires approval",
                pubkey=SELF_PUBKEY,
                created_at=10,
            ),
            _tagged_event(
                "user-reply",
                CHANNEL,
                content="sure go ahead",
                root="agent-prompt",
                reply_to="agent-prompt",
                created_at=11,
            ),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["user-reply"]
        assert adapter._dispatched[0]["text"] == "sure go ahead"
        assert adapter._dispatched[0]["reply_to_message_id"] == "agent-prompt"
        assert adapter._dispatched[0]["reply_to_is_own_message"] is True
        assert "approval" in (adapter._dispatched[0]["reply_to_text"] or "")

    @pytest.mark.asyncio
    async def test_approve_thread_reply_dispatches(self, adapter):
        await self._poll_with(
            adapter,
            _tagged_event(
                "agent-approve-prompt",
                CHANNEL,
                content="⚠️ Dangerous command requires approval",
                pubkey=SELF_PUBKEY,
                created_at=20,
            ),
            _tagged_event(
                "approve-msg",
                CHANNEL,
                content="/approve session",
                root="agent-approve-prompt",
                reply_to="agent-approve-prompt",
                created_at=21,
            ),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["approve-msg"]
        assert adapter._dispatched[0]["text"] == "/approve session"
        assert adapter._dispatched[0]["reply_to_is_own_message"] is True

    @pytest.mark.asyncio
    async def test_reply_to_other_user_stays_gated(self, adapter):
        third = "c" * 64
        await self._poll_with(
            adapter,
            _tagged_event(
                "other-msg",
                CHANNEL,
                content="anyone around?",
                pubkey=third,
                created_at=30,
            ),
            _tagged_event(
                "reply-other",
                CHANNEL,
                content="yeah I'm here",
                root="other-msg",
                reply_to="other-msg",
                created_at=31,
            ),
        )
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_reply_to_unknown_parent_stays_gated(self, adapter):
        await self._poll_with(
            adapter,
            _tagged_event(
                "orphan-reply",
                CHANNEL,
                content="/approve session",
                root="never-seen",
                reply_to="never-seen",
                created_at=40,
            ),
        )
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_seeded_own_history_matches_thread_reply(self, adapter):
        """Replies to agent messages sent before a gateway restart still match."""
        cli = _ScriptedCli()
        cli.script(
            "messages",
            "get",
            [
                _tagged_event(
                    "pre-restart-agent",
                    CHANNEL,
                    content="⚠️ Dangerous command requires approval",
                    pubkey=SELF_PUBKEY,
                    created_at=50,
                ),
            ],
        )
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")
        assert "pre-restart-agent" in adapter._channel_state[CHANNEL]["event_meta"]
        assert adapter._dispatched == []

        cli.responses.clear()
        cli.script(
            "messages",
            "get",
            [
                _tagged_event(
                    "post-restart-approve",
                    CHANNEL,
                    content="/approve always",
                    root="pre-restart-agent",
                    reply_to="pre-restart-agent",
                    created_at=51,
                ),
            ],
        )
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["post-restart-approve"]
        assert adapter._dispatched[0]["reply_to_is_own_message"] is True

    @pytest.mark.asyncio
    async def test_send_recorded_id_matches_thread_reply(self, adapter):
        """send()'s returned event_id is cached even without a WS/poll echo."""
        cli = _ScriptedCli()
        cli.script(
            "messages",
            "send",
            {"accepted": True, "event_id": "sent-prompt", "message": ""},
        )
        adapter._run_cli = cli
        result = await adapter.send(
            CHANNEL, "⚠️ Dangerous command requires approval"
        )
        assert result.success is True
        assert "sent-prompt" in adapter._channel_state[CHANNEL]["event_meta"]
        meta = adapter._channel_state[CHANNEL]["event_meta"]["sent-prompt"]
        assert meta[0] == SELF_PUBKEY

        cli.responses.clear()
        cli.script(
            "messages",
            "get",
            [
                _tagged_event(
                    "reply-to-send",
                    CHANNEL,
                    content="/approve session",
                    root="sent-prompt",
                    reply_to="sent-prompt",
                    created_at=61,
                ),
            ],
        )
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["reply-to-send"]
        assert adapter._dispatched[0]["reply_to_is_own_message"] is True
        assert adapter._dispatched[0]["reply_to_message_id"] == "sent-prompt"

    @pytest.mark.asyncio
    async def test_mention_path_still_populates_reply_context(self, adapter):
        """Visible @mention + thread reply still fills reply_to_* on dispatch."""
        await self._poll_with(
            adapter,
            _tagged_event(
                "agent-prior",
                CHANNEL,
                content="previous answer",
                pubkey=SELF_PUBKEY,
                created_at=70,
            ),
            _tagged_event(
                "mentioned-reply",
                CHANNEL,
                content="@Chip follow up please",
                root="agent-prior",
                reply_to="agent-prior",
                created_at=71,
            ),
        )
        assert len(adapter._dispatched) == 1
        d = adapter._dispatched[0]
        assert d["message_id"] == "mentioned-reply"
        assert d["text"] == "follow up please"  # leading @Chip stripped
        assert d["reply_to_message_id"] == "agent-prior"
        assert d["reply_to_author_id"] == SELF_PUBKEY
        assert d["reply_to_is_own_message"] is True
        assert d["reply_to_text"] == "previous answer"


# ── DM classification via p-tags (issue #68871) ──────────────────────────
#
# `buzz dms list` returns [] on some hosted relays, so DM conversations leak
# in via `channels list` and get seeded chat_type="group".  The adapter must
# reclassify them from the Nostr tags of real traffic: DM messages are
# p-tagged to our own pubkey WITHOUT the text mentioning us, while channel
# messages only ever p-tag us when the text visibly @mentions us.


class TestDmClassification:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        # Metadata exactly as `channels list` returns it on the hosted relay.
        a._channel_meta = {
            DM_CHANNEL: {"channel_id": DM_CHANNEL, "name": "DM", "description": ""},
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_names = {DM_CHANNEL: "DM", CHANNEL: "general"}
        # Both leaked in as group — the bug under test.
        a._channel_state[DM_CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, channel, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(channel)

    @pytest.mark.asyncio
    async def test_unmentioned_ptagged_dm_latches_and_dispatches(self, adapter):
        """The reported bug: a DM without an @mention must dispatch."""
        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="here's a test message", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


    @pytest.mark.asyncio
    async def test_general_reply_ptagging_self_stays_channel(self, adapter):
        """A #general reply to us p-tags our pubkey (observed live) — that
        must NOT reclassify the channel; mention gating still applies."""
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="@chip what's up?",
                          p=SELF_PUBKEY, reply_to="root-event"),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # It carried a mention, so it dispatches — but as a group message.
        assert [d["chat_type"] for d in adapter._dispatched] == ["group"]

        # And once the mention is absent, the channel gate drops the message
        # even though the earlier reply p-tagged us.
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e2", CHANNEL, content="thanks everyone", created_at=1001),
        )
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_channel_ptag_dispatches_without_latching(self, adapter):
        """A signed recipient tag wakes a channel without turning it into a DM."""
        adapter._channel_meta[CHANNEL]["description"] = ""
        adapter._channel_meta[CHANNEL]["name"] = "announcements"
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="fyi everyone", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]

        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event(
                "e2", CHANNEL, content="plain follow-up", created_at=1001, p=None
            ),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]

    @pytest.mark.asyncio
    async def test_missing_metadata_never_latches_group_as_dm(self, adapter):
        adapter._channel_meta.pop(CHANNEL)
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="tag-only mention", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._may_reclassify_as_dm(CHANNEL) is False


    @pytest.mark.asyncio
    async def test_dm_shaped_channel_discovered_when_dms_list_empty(self):
        """Fallback discovery: with `dms list` broken (returns []), a
        DM-shaped `channels list` entry gets watched. In watch-all mode a
        real channel is adopted too (live join, #75107) — but seeded from
        history, so nothing is replayed."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": DM_CHANNEL, "name": "DM", "description": "", "created_at": 1},
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        assert a._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert a._may_reclassify_as_dm(DM_CHANNEL) is True
        # Watch-all mode: the real channel is live-adopted (seeded, never
        # reclassified as DM).
        assert CHANNEL in a._channel_state
        assert a._channel_state[CHANNEL]["chat_type"] == "group"
        assert a._may_reclassify_as_dm(CHANNEL) is False
        # Adoption seeded it via a messages get call (history suppressed).
        assert any(c[0][:2] == ["messages", "get"] for c in cli.calls)

    @pytest.mark.asyncio
    async def test_explicit_watch_list_blocks_live_channel_adoption(self):
        """With an explicit channels: list, discovery must NOT adopt real
        channels outside that list — the user chose the watch set (#75107
        scoping)."""
        a = _make_adapter(extra={"channels": ["some-other-channel"]})
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        assert CHANNEL not in a._channel_state

    @pytest.mark.asyncio
    async def test_dm_metadata_promotes_existing_group_without_recipient_tag(self, adapter):
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [adapter._channel_meta[DM_CHANNEL]])
        adapter._run_cli = cli
        await adapter._discover_dms(seed=False)
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"

        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="no mention and no p tag"),
        )
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


class TestThreadRoots:

    @pytest.mark.asyncio
    async def test_inbound_root_e_tag_propagates_to_session_source(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {
            "chat_type": "group",
            "last_ts": 0,
            "seen": {},
        }
        dispatched = []

        async def capture(event):
            dispatched.append(event)

        adapter._message_handler = AsyncMock()
        adapter.handle_message = capture
        adapter._run_cli = _ScriptedCli()
        adapter.send_reaction = AsyncMock(return_value=True)
        event = _tagged_event("latest-child", CHANNEL, content="@Chip follow-up")
        event["tags"] += [
            ["e", "stable-root", "", "root"],
            ["e", "latest-parent", "", "reply"],
        ]

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        assert dispatched[0].source.thread_id == "stable-root"


# ── Sending ───────────────────────────────────────────────────────────────


class TestBuzzAdapterSend:

    @pytest.mark.asyncio
    async def test_send_success_via_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt123", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello **markdown**")
        assert result.success is True
        assert result.message_id == "evt123"

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        # Content travels via stdin (--content -), never argv
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "hello **markdown**"
        # Our own event id is marked seen for echo suppression
        assert "evt123" in adapter._channel_state[CHANNEL]["seen"]

    @pytest.mark.asyncio
    async def test_send_metadata_thread_id_uses_reply_to_flag(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt124", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(
            CHANNEL,
            "working",
            metadata={"thread_id": "buzz-event-123"},
        )

        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "buzz-event-123"

    @pytest.mark.asyncio
    async def test_send_uses_metadata_reply_to_message_id(self):
        """Gateway stream/progress pass reply anchors via metadata.

        Without honoring reply_to_message_id, mid-turn commentary posts as
        new top-level channel messages instead of thread replies.
        """
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-reply", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(
            CHANNEL,
            "threaded reply",
            metadata={"reply_to_message_id": "root-event-abc"},
        )
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert "--reply-to" in args
        assert args[args.index("--reply-to") + 1] == "root-event-abc"

    @pytest.mark.asyncio
    async def test_send_prefers_stable_thread_root_over_latest_reply(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt124"})
        adapter._run_cli = cli

        await adapter.send(
            CHANNEL,
            "threaded reply",
            reply_to="latest-child",
            metadata={"thread_id": "stable-root"},
        )

        args, _stdin = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "stable-root"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stdout", "error_fragment"),
        [
            ("not json", "invalid CLI response"),
            (json.dumps([]), "invalid CLI response"),
            (json.dumps({"event_id": "evt"}), "invalid CLI response"),
            (json.dumps({"accepted": True}), "invalid CLI response"),
            (json.dumps({"accepted": True, "event_id": "   "}), "invalid CLI response"),
        ],
    )
    async def test_send_rejects_invalid_zero_exit_receipt(self, stdout, error_fragment):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(return_value=(0, stdout, ""))

        result = await adapter.send(CHANNEL, "hello")

        assert result.success is False
        assert error_fragment in result.error
        assert result.message_id is None
        assert result.raw_response is None

    @pytest.mark.asyncio
    async def test_send_rejection_is_useful_and_bounded(self):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(
            return_value=(
                0,
                json.dumps({"accepted": False, "message": "upload rejected " + "x" * 100_000}),
                "",
            )
        )

        result = await adapter.send(CHANNEL, "hello")

        assert result.success is False
        assert "upload rejected" in result.error
        assert len(result.error) <= 1024
        assert result.raw_response is None

    @pytest.mark.asyncio
    async def test_send_success_exposes_only_verified_receipt(self):
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(
            return_value=(
                0,
                json.dumps({
                    "accepted": True,
                    "event_id": " evt-safe ",
                    "message": "x" * 100_000,
                    "raw_response": "y" * 100_000,
                }),
                "",
            )
        )

        result = await adapter.send(CHANNEL, "hello")

        assert result.success is True
        assert result.message_id == "evt-safe"
        assert result.raw_response is None

    @pytest.mark.asyncio
    async def test_send_image_local_file_uses_file_flag(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="screenshot")
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)

    @pytest.mark.asyncio
    async def test_send_image_local_file_prefers_stable_thread_root(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt127"})
        adapter._run_cli = cli

        await adapter.send_image(
            CHANNEL,
            str(img),
            reply_to="latest-child",
            metadata={"thread_id": "stable-root"},
        )

        args, _stdin = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "stable-root"

    @pytest.mark.asyncio
    async def test_send_retries_unresolved_presentation_mention_without_notifying(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script(
            "messages",
            "send",
            "",
            code=1,
            stderr=(
                "mention '@session' does not match a current channel member; "
                "retry with --mention <pubkey>"
            ),
        )
        cli.script(
            "messages",
            "send",
            {"accepted": True, "event_id": "evt124", "message": ""},
        )
        adapter._run_cli = cli

        result = await adapter.send(
            CHANNEL,
            "Continue in @session:default/20260809_092321_24aa09.",
        )

        assert result.success is True
        assert result.message_id == "evt124"
        # Composed with #83414 mention resolution: resolution probes
        # (channels members / messages get) precede the sends; assert on the
        # publish calls only.
        sends = [c for c in cli.calls if tuple(c[0][:2]) == ("messages", "send")]
        assert len(sends) == 2
        assert sends[0][1] == (
            "Continue in @session:default/20260809_092321_24aa09."
        )
        assert sends[1][1] == (
            "Continue in @\u200bsession:default/20260809_092321_24aa09."
        )

    @pytest.mark.asyncio
    async def test_send_does_not_retry_unrelated_cli_failure(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script(
            "messages",
            "send",
            "",
            code=1,
            stderr="relay unavailable",
        )
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello @session")

        assert result.success is False
        # Unrelated failures never retry the publish (mention-resolution
        # probes for "@" content are not sends).
        sends = [c for c in cli.calls if tuple(c[0][:2]) == ("messages", "send")]
        assert len(sends) == 1

    @pytest.mark.asyncio
    async def test_send_image_retries_unresolved_presentation_mention(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script(
            "messages",
            "send",
            "",
            code=1,
            stderr=(
                "mention '@session' does not match a current channel member; "
                "retry with --mention <pubkey>"
            ),
        )
        cli.script(
            "messages",
            "send",
            {"accepted": True, "event_id": "evt127", "message": ""},
        )
        adapter._run_cli = cli

        result = await adapter.send_image(
            CHANNEL,
            str(img),
            caption="See @session:default/example.",
        )

        assert result.success is True
        assert len(cli.calls) == 2
        assert cli.calls[0][1] == "See @session:default/example."
        assert cli.calls[1][1] == "See @\u200bsession:default/example."

    @pytest.mark.asyncio
    async def test_send_image_file_existing_local_path_stays_native_upload_after_probe_flip(
        self, tmp_path, monkeypatch
    ):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126b", "message": ""})
        adapter._run_cli = cli

        original_is_file = _buzz_mod.Path.is_file
        probe_results = iter([True, False])

        def sequential_is_file(path):
            if path == img:
                return next(probe_results, original_is_file(path))
            return original_is_file(path)

        monkeypatch.setattr(_buzz_mod.Path, "is_file", sequential_is_file)

        result = await adapter.send_image_file(CHANNEL, str(img), caption="screenshot")

        assert result.success is True
        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        assert args[args.index("--file") + 1] == str(img)
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "screenshot"

    @pytest.mark.asyncio
    async def test_send_image_file_uses_metadata_thread_id_when_reply_to_missing(self, tmp_path):
        img = tmp_path / "reply-shot.png"
        img.write_bytes(b"\x89PNG fake")
        thread_id = "b" * 64
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126c", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send_image_file(
            CHANNEL,
            str(img),
            caption="screenshot",
            metadata={"thread_id": thread_id},
        )

        assert result.success is True
        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        assert args[args.index("--file") + 1] == str(img)
        assert args[args.index("--content") + 1] == "-"
        assert args[args.index("--reply-to") + 1] == thread_id
        assert stdin_text == "screenshot"

    @pytest.mark.asyncio
    async def test_send_image_file_missing_local_path_uses_base_fallback_notice(self, tmp_path):
        missing = tmp_path / "missing.png"
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt127", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send_image_file(CHANNEL, str(missing), caption="screenshot")

        assert result.success is True
        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert "--file" not in args
        assert str(missing) not in args
        assert stdin_text == "screenshot\n⚠️ Couldn't deliver the image attachment."
        assert str(missing) not in stdin_text

    @pytest.mark.asyncio
    async def test_send_document_uses_native_file_flag(self, tmp_path):
        document = tmp_path / "package.zip"
        document.write_bytes(b"PK fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt128", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send_document(CHANNEL, str(document), caption="files")

        assert result.success is True
        args, stdin_text = cli.calls[0]
        assert args[args.index("--file") + 1] == str(document)
        assert stdin_text == "files"

    @pytest.mark.asyncio
    async def test_send_multiple_images_file_url_uses_native_file_send(self, tmp_path):
        img = tmp_path / "shot with spaces.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt127", "message": ""})
        adapter._run_cli = cli

        await adapter.send_multiple_images(CHANNEL, [(img.as_uri(), "screenshot")])

        assert len(cli.calls) == 1
        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        assert args[args.index("--file") + 1] == str(img)
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "screenshot"
        assert "Couldn't deliver the image attachment." not in stdin_text




# ── Thread anchoring ──────────────────────────────────────────────────────


# ── Voice notes ───────────────────────────────────────────────────────────

# BIP-340 test vector 0 private key: real signatures without any secret material.
TEST_PRIVATE_KEY = "00" * 31 + "03"
_nostr_auth = _buzz_mod._nostr_auth


def _decode_nostr_token(header: str) -> dict:
    """Unpack a ``Nostr <base64 event>`` Authorization value."""
    scheme, token = header.split(" ", 1)
    assert scheme == "Nostr"
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def _tag_map(event: dict) -> dict:
    return {tag[0]: tag[1] for tag in event["tags"]}


def _mock_http(monkeypatch, handler):
    """Route every httpx.AsyncClient through *handler* and return the recorded requests."""
    import httpx

    requests = []
    real_async_client = httpx.AsyncClient

    def recording_handler(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(recording_handler), **kwargs),
    )
    return requests


def _nip11(extensions):
    import httpx

    def handler(request):
        assert request.headers["accept"] == "application/nostr+json"
        return httpx.Response(200, json={"name": "test", "supported_extensions": extensions})

    return handler


def _fake_ffmpeg(monkeypatch, output: bytes = b"ID3fake-mp3", returncode: int = 0):
    """Stand in for the ffmpeg binary: record argv and write *output* to the last argument."""
    import subprocess

    calls = []

    def run(cmd, capture_output=True, timeout=None):
        calls.append(list(cmd))
        if returncode == 0:
            Path(cmd[-1]).write_bytes(output)
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"boom" if returncode else b"")

    monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


class TestVoiceNoteDelivery:

    @pytest.mark.asyncio
    async def test_relay_supports_audio_detects_extension_and_caches(self, monkeypatch):
        requests = _mock_http(monkeypatch, _nip11(["nip-oa", "buzz-audio"]))
        adapter = _make_adapter()

        assert await adapter._relay_supports_audio() is True
        assert await adapter._relay_supports_audio() is True

        assert len(requests) == 1
        assert str(requests[0].url) == "https://test.relay/"
        supported, probed_at = adapter._audio_extension_cache
        assert supported is True and probed_at > 0

    @pytest.mark.asyncio
    async def test_relay_supports_audio_reprobes_after_ttl(self, monkeypatch):
        import time

        requests = _mock_http(monkeypatch, _nip11([]))
        adapter = _make_adapter()
        adapter._audio_extension_cache = (True, time.monotonic() - _buzz_mod._AUDIO_EXTENSION_TTL - 1)

        assert await adapter._relay_supports_audio() is False
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_relay_supports_audio_treats_probe_failure_as_unsupported(self, monkeypatch):
        import httpx

        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        _mock_http(monkeypatch, handler)
        adapter = _make_adapter()

        assert await adapter._relay_supports_audio() is False
        assert adapter._audio_extension_cache[0] is False

    @pytest.mark.asyncio
    async def test_relay_supports_audio_follows_a_redirect_to_https(self, monkeypatch):
        """An http:// relay URL that 301s to https must not cache "no audio" for the whole TTL."""
        import httpx

        nip11 = _nip11(["buzz-audio"])

        def handler(request):
            if request.url.scheme == "http":
                return httpx.Response(301, headers={"location": "https://test.relay/"})
            return nip11(request)

        requests = _mock_http(monkeypatch, handler)
        adapter = _make_adapter({"relay_url": "http://test.relay"})

        assert await adapter._relay_supports_audio() is True
        assert [r.url.scheme for r in requests] == ["http", "https"]

    @pytest.mark.asyncio
    async def test_relay_supports_audio_survives_a_non_object_nip11_body(self, monkeypatch, caplog):
        """A JSON array is a well-formed "no extensions" answer, not a probe failure.

        The ``isinstance(doc, dict)`` guard is what makes it one: without it the ``.get`` raises an
        AttributeError that the broad ``except`` turns into a warning about an unusable relay, which is
        why this asserts on the log as well as the result.
        """
        import httpx
        import logging

        _mock_http(monkeypatch, lambda request: httpx.Response(200, json=["buzz-audio"]))
        adapter = _make_adapter()

        with caplog.at_level(logging.WARNING, logger=_buzz_mod.logger.name):
            assert await adapter._relay_supports_audio() is False
        assert "NIP-11 probe failed" not in caplog.text

    def test_blossom_auth_header_is_signed_upload_event(self):
        adapter = _make_adapter({"relay_url": "wss://test.relay:8443/relay"})
        adapter._private_key = TEST_PRIVATE_KEY
        digest = "ab" * 32

        event = _decode_nostr_token(adapter._blossom_auth_header(digest))

        assert event["kind"] == 24242
        assert event["content"] == "Upload file"
        assert event["pubkey"] == _nostr_auth.public_key_hex(TEST_PRIVATE_KEY)
        tags = _tag_map(event)
        assert tags["t"] == "upload"
        assert tags["x"] == digest
        assert tags["server"] == "test.relay:8443"
        assert int(tags["expiration"]) == event["created_at"] + _buzz_mod._BLOSSOM_AUTH_TTL
        serialized = json.dumps(
            [0, event["pubkey"], event["created_at"], 24242, event["tags"], event["content"]],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()
        assert event["id"] == hashlib.sha256(serialized).hexdigest()
        assert len(bytes.fromhex(event["sig"])) == 64

    @pytest.mark.asyncio
    async def test_blossom_upload_puts_blob_with_auth_and_tries_media_upload(self, monkeypatch, tmp_path):
        import httpx

        payload = b"mp3 frames"
        digest = hashlib.sha256(payload).hexdigest()
        descriptor = {"url": f"https://test.relay/media/{digest}.mp3", "sha256": digest, "size": len(payload), "type": "audio/mpeg"}

        def handler(request):
            if request.url.path == "/upload":
                return httpx.Response(404)
            assert request.method == "PUT"
            assert request.url.path == "/media/upload"
            assert request.content == payload
            assert request.headers["content-type"] == "audio/mpeg"
            assert request.headers["x-sha-256"] == digest
            assert request.headers["x-auth-tag"] == '["auth","a","b","c"]'
            event = _decode_nostr_token(request.headers["authorization"])
            assert _tag_map(event)["x"] == digest
            return httpx.Response(200, json=descriptor)

        requests = _mock_http(monkeypatch, handler)
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._auth_tag = '["auth","a","b","c"]'
        blob = tmp_path / "voice-note-1.mp3"
        blob.write_bytes(payload)

        desc, error = await adapter._blossom_upload(blob, "audio/mpeg")

        assert error is None
        assert desc == descriptor
        assert [r.url.path for r in requests] == ["/upload", "/media/upload"]

    @pytest.mark.asyncio
    async def test_blossom_upload_reports_relay_rejection(self, monkeypatch, tmp_path):
        import httpx

        _mock_http(monkeypatch, lambda request: httpx.Response(422, text="audio validation failed"))
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY
        blob = tmp_path / "voice-note-1.mp3"
        blob.write_bytes(b"x")

        desc, error = await adapter._blossom_upload(blob, "audio/mpeg")

        assert desc is None
        assert error == "HTTP 422: audio validation failed"

    def test_voice_note_imeta_marks_filename_duration_and_transcript(self):
        desc = {"url": "https://test.relay/media/abc.mp3", "sha256": "c" * 64, "size": 10, "duration": "3.2", "type": "audio/mpeg"}

        imeta = BuzzAdapter._voice_note_imeta(desc, "voice-note-7.mp3", "audio/mp4", "hello\tthere\nfriend\x00")

        assert imeta == [
            "imeta",
            "url https://test.relay/media/abc.mp3",
            "m audio/mpeg",
            "x " + "c" * 64,
            "size 10",
            "duration 3.2",
            "filename voice-note-7.mp3",
            "alt hello there\nfriend",  # the tab becomes a space; the NUL is dropped
        ]
        assert "alt" not in " ".join(BuzzAdapter._voice_note_imeta({"url": "u"}, "voice-note-8.mp4", "video/mp4", "  "))

    @pytest.mark.asyncio
    async def test_send_voice_prefers_mp3_upload_on_audio_relay(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        clean = tmp_path / "voice-note-1.mp3"
        clean.write_bytes(b"clean")
        conversions = []

        def convert(path, out, *, reencode=False):
            conversions.append((path, out, reencode))
            return clean

        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(_buzz_mod, "_convert_audio_to_clean_mp3", convert)
        monkeypatch.setattr(_buzz_mod, "_wrap_audio_in_voice_note_envelope", lambda *a: pytest.fail("envelope not needed"))
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        desc = {"url": "https://test.relay/media/x.mp3"}
        adapter._blossom_upload = AsyncMock(return_value=(desc, None))
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=True, message_id="evt-voice"))
        adapter._run_cli = AsyncMock(side_effect=AssertionError("CLI must not be used"))

        result = await adapter.send_voice(CHANNEL, str(src), caption="hi", reply_to="parent", metadata={"transcript": "hi"})

        assert result.success is True and result.message_id == "evt-voice"
        # The destination is allocated by send_voice before the transcode starts, so a cancelled turn
        # cannot orphan it, and it is unlinked once the upload that reads it has finished.
        (source, out, reencode), = conversions
        assert (source, reencode) == (src, False)
        assert out.parent == _buzz_mod._voice_note_workdir() and out.suffix == ".mp3" and not out.exists()
        adapter._blossom_upload.assert_awaited_once_with(clean, "audio/mpeg")
        adapter._publish_voice_note.assert_awaited_once_with(
            CHANNEL, desc, "voice-note-1.mp3", "audio/mpeg", caption="hi", reply_to="parent", metadata={"transcript": "hi"})

    @pytest.mark.asyncio
    async def test_send_voice_reencodes_once_when_copied_stream_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        clean = tmp_path / "voice-note-2.mp3"
        clean.write_bytes(b"clean")
        conversions = []

        def convert(path, out, *, reencode=False):
            conversions.append(reencode)
            return clean

        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(_buzz_mod, "_convert_audio_to_clean_mp3", convert)
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        adapter._blossom_upload = AsyncMock(side_effect=[(None, "HTTP 422: bad frames"), ({"url": "u"}, None)])
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=True, message_id="evt-2"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True
        assert conversions == [False, True]

    @pytest.mark.asyncio
    async def test_send_voice_uses_envelope_when_relay_lacks_audio(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        wrapped = tmp_path / "voice-note-3.mp4"
        wrapped.write_bytes(b"mp4")
        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(_buzz_mod, "_convert_audio_to_clean_mp3", lambda *a, **k: pytest.fail("mp3 path not expected"))
        monkeypatch.setattr(_buzz_mod, "_wrap_audio_in_voice_note_envelope", lambda path, out: wrapped)
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=False)
        adapter._blossom_upload = AsyncMock(side_effect=AssertionError("no direct upload without buzz-audio"))
        cli = _ScriptedCli()
        desc = {"url": "https://test.relay/media/x.mp4", "type": "video/mp4"}
        cli.script("upload", "file", desc)
        adapter._run_cli = cli
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=True, message_id="evt-3"))

        result = await adapter.send_voice(CHANNEL, str(src), caption="c")

        assert result.success is True
        assert cli.calls[0][0] == ["upload", "file", "--file", str(wrapped)]
        adapter._publish_voice_note.assert_awaited_once_with(
            CHANNEL, desc, "voice-note-3.mp4", "video/mp4", caption="c", reply_to=None, metadata=None)

    @pytest.mark.asyncio
    async def test_send_voice_falls_back_to_plain_attachment(self, monkeypatch, tmp_path):
        """A rejected publish degrades to the envelope as a plain --file upload; no ffmpeg degrades to the source."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        wrapped = tmp_path / "voice-note-4.mp4"
        wrapped.write_bytes(b"mp4")
        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(_buzz_mod, "_wrap_audio_in_voice_note_envelope", lambda path, out: wrapped)
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=False)
        cli = _ScriptedCli()
        cli.script("upload", "file", {"url": "https://test.relay/media/x.mp4"})
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-plain"})
        adapter._run_cli = cli
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=False, error="relay rejected voice note: nope"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True and result.message_id == "evt-plain"
        args, _stdin = cli.calls[-1]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--file") + 1] == str(wrapped)

        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: None)
        adapter._relay_supports_audio = AsyncMock(side_effect=AssertionError("no probe without ffmpeg"))
        result = await adapter.send_voice(CHANNEL, str(src))
        assert result.success is True
        assert cli.calls[-1][0][cli.calls[-1][0].index("--file") + 1] == str(src)

    @pytest.mark.asyncio
    async def test_publish_voice_note_sends_kind_9_event_with_imeta(self, monkeypatch):
        import sys
        from types import ModuleType
        from unittest.mock import patch

        class _RelayWs:
            def __init__(self):
                self.sent = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def send(self, raw):
                self.sent.append(json.loads(raw))

            async def recv(self):
                return json.dumps(["OK", self.sent[-1][1]["id"], True, ""])

        ws = _RelayWs()
        fake_ws_mod = ModuleType("websockets")
        fake_ws_mod.connect = lambda *args, **kwargs: ws
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._authenticate_websocket = AsyncMock()
        adapter._thread_roots["child"] = "root"
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        desc = {"url": "https://test.relay/media/x.mp3", "sha256": "d" * 64, "size": 5}

        with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
            result = await adapter._publish_voice_note(
                CHANNEL, desc, "voice-note-9.mp3", "audio/mpeg", caption="listen", reply_to="child",
                metadata={"transcript": "spoken words"})

        assert result.success is True
        adapter._authenticate_websocket.assert_awaited_once_with(ws)
        frame, event = ws.sent[0]
        assert frame == "EVENT"
        assert result.message_id == event["id"]
        assert event["kind"] == 9
        assert event["pubkey"] == _nostr_auth.public_key_hex(TEST_PRIVATE_KEY)
        assert event["content"] == "listen\n[voice-note-9.mp3](https://test.relay/media/x.mp3)"
        assert event["tags"][0] == ["h", CHANNEL]
        assert event["tags"][1][0] == "imeta"
        assert "filename voice-note-9.mp3" in event["tags"][1]
        assert "alt spoken words" in event["tags"][1]
        assert event["tags"][2] == ["e", "root", "", "reply"]
        assert event["id"] in adapter._channel_state[CHANNEL]["seen"]
        # Parity with send(): without the meta row a thread reply to the card alone would not read as a
        # reply to our own message in a require_mention channel, because our own relay echo is suppressed.
        assert adapter._channel_state[CHANNEL]["event_meta"][event["id"]] == (SELF_PUBKEY, event["content"])

    @pytest.mark.asyncio
    async def test_publish_voice_note_reports_relay_rejection(self, monkeypatch):
        import sys
        from types import ModuleType
        from unittest.mock import patch

        class _RejectingWs:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def send(self, raw):
                self.event_id = json.loads(raw)[1]["id"]

            async def recv(self):
                return json.dumps(["OK", self.event_id, False, "blocked: not a member"])

        fake_ws_mod = ModuleType("websockets")
        fake_ws_mod.connect = lambda *args, **kwargs: _RejectingWs()
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._authenticate_websocket = AsyncMock()

        with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
            result = await adapter._publish_voice_note(CHANNEL, {"url": "u"}, "voice-note-1.mp3", "audio/mpeg", caption=None, reply_to=None, metadata=None)

        assert result.success is False
        assert result.error == "relay rejected voice note: blocked: not a member"

    @pytest.mark.asyncio
    async def test_play_tts_file_attaches_transcript(self):
        adapter = _make_adapter()
        adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="evt-tts"))
        deliveries = []
        event = SimpleNamespace(source=SimpleNamespace(chat_id=CHANNEL))

        skipped_text = await adapter._play_tts_file(event, "spoken reply", "/tmp/tts.mp3", True, {"thread_id": "root"}, deliveries.append)

        # False on purpose: the text reply keeps going out on its own, through send(), which is the only
        # path with mention resolution and length chunking. A caption would lose both, and would lose the
        # text entirely whenever the voice paths degrade to a plain file attachment.
        assert skipped_text is False
        assert deliveries[0].message_id == "evt-tts"
        adapter.send_voice.assert_awaited_once_with(
            chat_id=CHANNEL, audio_path="/tmp/tts.mp3", metadata={"thread_id": "root", "transcript": "spoken reply"})

    @pytest.mark.asyncio
    async def test_play_tts_file_transcribes_the_first_chunk_only(self):
        """A reply split into several TTS files must not repeat the whole transcript under every card."""
        adapter = _make_adapter()
        adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="evt-tts"))
        event = SimpleNamespace(source=SimpleNamespace(chat_id=CHANNEL))

        await adapter._play_tts_file(event, "spoken reply", "/tmp/a.mp3", True, {}, lambda _r: None)
        await adapter._play_tts_file(event, "spoken reply", "/tmp/b.mp3", False, {}, lambda _r: None)

        metadatas = [call.kwargs["metadata"] for call in adapter.send_voice.await_args_list]
        assert metadatas == [{"transcript": "spoken reply"}, {}]

    def test_mp3_conversion_copies_mp3_frames_and_reencodes_other_formats(self, monkeypatch, tmp_path):
        # tempdir is redirected and every destination is caller-allocated under tmp_path: a unit test must
        # not drop scratch files into the workdir that every gateway on this host shares.
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        calls = _fake_ffmpeg(monkeypatch)
        mp3 = tmp_path / "reply.mp3"
        mp3.write_bytes(b"x")
        ogg = tmp_path / "reply.ogg"
        ogg.write_bytes(b"x")
        dest = [tmp_path / f"voice-note-{i}.mp3" for i in range(4)]

        out = _buzz_mod._convert_audio_to_clean_mp3(mp3, dest[0])
        _buzz_mod._convert_audio_to_clean_mp3(mp3, dest[1], reencode=True)
        _buzz_mod._convert_audio_to_clean_mp3(ogg, dest[2])

        assert out == dest[0]
        assert out.read_bytes() == b"ID3fake-mp3"
        codecs = [cmd[cmd.index("-c:a") + 1] for cmd in calls]
        assert codecs == ["copy", "libmp3lame", "libmp3lame"]
        assert all(cmd[0] == "/fake/ffmpeg" and "-map_metadata" in cmd and "-id3v2_version" in cmd for cmd in calls)
        assert _buzz_mod._convert_audio_to_clean_mp3(tmp_path / "missing.mp3", dest[3]) is None
        assert not (tmp_path / "hermes-buzz-voice").exists()

    def test_ffmpeg_failure_yields_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        _fake_ffmpeg(monkeypatch, returncode=1)
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"x")

        assert _buzz_mod._convert_audio_to_clean_mp3(src, _buzz_mod._voice_note_tempfile(".mp3")) is None
        assert _buzz_mod._wrap_audio_in_voice_note_envelope(src, _buzz_mod._voice_note_tempfile(".mp4")) is None
        # The mkstemp placeholder goes too; a failed transcode must not leave an empty file behind.
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    def test_converted_voice_files_get_unique_names(self, monkeypatch, tmp_path):
        """Millisecond stamps collide across the gateways sharing this host's temp dir; mkstemp names cannot."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

        names = {_buzz_mod._voice_note_tempfile(".mp3").name for _ in range(5)}
        names |= {_buzz_mod._voice_note_tempfile(".mp4").name for _ in range(5)}

        assert len(names) == 10
        assert all(n.startswith(_buzz_mod._VOICE_NOTE_PREFIX) for n in names)
        assert sorted(n[-4:] for n in names) == [".mp3"] * 5 + [".mp4"] * 5

    def test_ffmpeg_lookup_prefers_the_env_override_then_path(self, monkeypatch, tmp_path):
        """No hard-coded Homebrew path: BUZZ_FFMPEG_PATH wins, then PATH, then the usual install roots."""
        override = tmp_path / "custom-ffmpeg"
        override.write_bytes(b"#!/bin/sh\n")
        on_path = tmp_path / "path-ffmpeg"
        on_path.write_bytes(b"#!/bin/sh\n")
        monkeypatch.setattr(_buzz_mod.shutil, "which", lambda name: str(on_path))

        monkeypatch.setenv("BUZZ_FFMPEG_PATH", str(override))
        assert _buzz_mod._ffmpeg_path() == str(override)

        monkeypatch.setenv("BUZZ_FFMPEG_PATH", str(tmp_path / "does-not-exist"))
        assert _buzz_mod._ffmpeg_path() == str(on_path)

        monkeypatch.delenv("BUZZ_FFMPEG_PATH")
        monkeypatch.setattr(_buzz_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(_buzz_mod, "_FFMPEG_FALLBACKS", (str(tmp_path / "nope"), str(on_path)))
        assert _buzz_mod._ffmpeg_path() == str(on_path)

        monkeypatch.setattr(_buzz_mod, "_FFMPEG_FALLBACKS", ())
        assert _buzz_mod._ffmpeg_path() is None

    @pytest.mark.asyncio
    async def test_send_voice_unlinks_every_converted_file(self, monkeypatch, tmp_path):
        """Auto-TTS runs on every voice turn across every gateway; the scratch dir must not grow."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        _fake_ffmpeg(monkeypatch)
        src = tmp_path / "reply.wav"
        src.write_bytes(b"audio")
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        uploaded = []

        async def upload(path, mime):
            uploaded.append((Path(path).name, Path(path).is_file()))
            return {"url": "https://test.relay/media/x.mp3"}, None

        adapter._blossom_upload = upload
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=True, message_id="evt-clean"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True
        # The file still exists while it is being uploaded, and is gone once send_voice returns.
        assert len(uploaded) == 1 and uploaded[0][1] is True and uploaded[0][0].startswith(_buzz_mod._VOICE_NOTE_PREFIX)
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_send_voice_unlinks_scratch_files_on_the_fallback_path(self, monkeypatch, tmp_path):
        """The re-encode retry, the envelope, and the plain-file fallback all clean up after themselves."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        _fake_ffmpeg(monkeypatch)
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        adapter._blossom_upload = AsyncMock(side_effect=[(None, "HTTP 422: bad frames"), (None, "HTTP 422: bad frames")])
        cli = _ScriptedCli()
        cli.script("upload", "file", {"url": "https://test.relay/media/x.mp4"})
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-plain"})
        adapter._run_cli = cli
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=False, error="nope"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True and result.message_id == "evt-plain"
        assert adapter._blossom_upload.await_count == 2  # copied stream, then the re-encode
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_send_voice_degrades_to_a_plain_file_when_no_scratch_file_can_be_made(self, monkeypatch, tmp_path):
        """A full or read-only TMPDIR must cost the voice card, not the whole turn.

        send_voice runs inside the gateway's TTS hook, which has no ``except``: an OSError out of mkstemp
        would land in the turn's outer handler and the text reply would never be sent.
        """
        import errno

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.wav"
        src.write_bytes(b"audio")
        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")

        def no_space(**kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tempfile, "mkstemp", no_space)
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        adapter._blossom_upload = AsyncMock(side_effect=AssertionError("nothing was converted to upload"))
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-plain"})
        adapter._run_cli = cli

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True and result.message_id == "evt-plain"
        assert cli.calls[-1][0][cli.calls[-1][0].index("--file") + 1] == str(src)

    @pytest.mark.asyncio
    async def test_send_voice_returns_a_failure_result_instead_of_raising(self, monkeypatch, tmp_path):
        """Whatever goes wrong, send_voice answers with a SendResult so the text reply still goes out."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        _fake_ffmpeg(monkeypatch)
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        adapter._blossom_upload = AsyncMock(side_effect=RuntimeError("upload exploded"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is False and "upload exploded" in result.error
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []  # the scratch file goes even so

    @pytest.mark.asyncio
    async def test_send_voice_cleans_up_a_transcode_that_is_cancelled_mid_flight(self, monkeypatch, tmp_path):
        """Interrupts are routine on this fleet, and the transcode thread outlives the cancelled turn.

        The destination is allocated and registered before ``asyncio.to_thread`` starts, so the ``finally``
        unlinks it; the worker holds the open handle and its bytes go with the unlinked inode.
        """
        import threading
        import time as _time

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        src = tmp_path / "reply.wav"
        src.write_bytes(b"audio")
        started = threading.Event()

        def slow_convert(source, out, *, reencode=False):
            with out.open("wb") as handle:  # ffmpeg opens the destination before it writes anything
                started.set()
                _time.sleep(0.3)
                handle.write(b"late frames")
            return out

        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(_buzz_mod, "_convert_audio_to_clean_mp3", slow_convert)
        adapter = _make_adapter()
        adapter._relay_supports_audio = AsyncMock(return_value=True)

        task = asyncio.create_task(adapter.send_voice(CHANNEL, str(src)))
        await asyncio.to_thread(started.wait, 5)
        # The path exists, and is on send_voice's cleanup list, while the worker is still transcoding.
        assert len(list(_buzz_mod._voice_note_workdir().iterdir())) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(_time.sleep, 0.5)  # let the worker thread finish writing

        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_blossom_upload_reports_a_signing_failure_instead_of_raising(self, monkeypatch, tmp_path):
        """send_voice runs inside the gateway's TTS hook, which has no ``except``: a raise drops the text reply."""
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "")
        monkeypatch.setattr(_buzz_mod, "_resolve_auth_tag", lambda extra=None: "")
        blob = tmp_path / "voice-note-1.mp3"
        blob.write_bytes(b"x")
        adapter = _make_adapter()
        adapter._private_key = ""

        desc, error = await adapter._blossom_upload(blob, "audio/mpeg")

        assert desc is None and error.startswith("ValueError")

    @pytest.mark.asyncio
    async def test_send_voice_returns_a_result_when_the_key_cannot_sign(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "")
        monkeypatch.setattr(_buzz_mod, "_resolve_auth_tag", lambda extra=None: "")
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        _fake_ffmpeg(monkeypatch)
        src = tmp_path / "reply.mp3"
        src.write_bytes(b"audio")
        adapter = _make_adapter()
        adapter._private_key = ""
        adapter._relay_supports_audio = AsyncMock(return_value=True)
        cli = _ScriptedCli()
        cli.script("upload", "file", {"url": "https://test.relay/media/x.mp4"})
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-plain"})
        adapter._run_cli = cli
        adapter._publish_voice_note = AsyncMock(return_value=SendResult(success=False, error="unsigned"))

        result = await adapter.send_voice(CHANNEL, str(src))

        assert result.success is True and result.message_id == "evt-plain"
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_blossom_upload_resolves_credentials_lazily(self, monkeypatch, tmp_path):
        """A send that reaches an adapter whose connect() never ran still signs, the way _run_cli does."""
        import httpx

        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: TEST_PRIVATE_KEY)
        monkeypatch.setattr(_buzz_mod, "_resolve_auth_tag", lambda extra=None: '["auth","lazy"]')
        payload = b"mp3 frames"
        blob = tmp_path / "voice-note-1.mp3"
        blob.write_bytes(payload)
        descriptor = {"url": "https://test.relay/media/x.mp3"}
        requests = _mock_http(monkeypatch, lambda request: httpx.Response(200, json=descriptor))
        adapter = _make_adapter()
        adapter._private_key = adapter._auth_tag = ""

        desc, error = await adapter._blossom_upload(blob, "audio/mpeg")

        assert (desc, error) == (descriptor, None)
        assert requests[0].headers["x-auth-tag"] == '["auth","lazy"]'
        assert _tag_map(_decode_nostr_token(requests[0].headers["authorization"]))["x"] == hashlib.sha256(payload).hexdigest()


class TestVoiceNoteInbound:

    @staticmethod
    def _envelope_metadata(payload: bytes) -> dict:
        return {
            "url": "https://test.relay/media/envelope.mp4",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "filename": "voice-note-1710000000.mp4",
            "mime_type": "video/mp4",
        }

    @pytest.mark.asyncio
    async def test_voice_note_envelope_is_demuxed_to_audio(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"\x00\x00\x00\x18ftypmp42 envelope bytes"
        _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        calls = _fake_ffmpeg(monkeypatch, output=b"\xff\xfb\x90\x00mp3 frames")
        adapter = _make_adapter()

        cached = await adapter._download_attachment(self._envelope_metadata(payload))

        assert cached is not None
        assert cached.kind == "audio"
        assert cached.media_type == "audio/mpeg"
        assert cached.display_name == "voice-note-1710000000.mp3"
        assert Path(cached.path).read_bytes() == b"\xff\xfb\x90\x00mp3 frames"
        (cmd,) = calls
        assert cmd[cmd.index("-i") + 1].endswith(".mp4") and "-vn" in cmd and cmd[-1].endswith(".mp3")
        assert Path(cmd[cmd.index("-i") + 1]).exists() is False  # scratch files are cleaned up

    @pytest.mark.asyncio
    async def test_voice_note_envelope_stays_video_when_ffmpeg_fails(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"\x00\x00\x00\x18ftypmp42 envelope bytes"
        _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        _fake_ffmpeg(monkeypatch, returncode=1)
        adapter = _make_adapter()

        cached = await adapter._download_attachment(self._envelope_metadata(payload))

        assert cached is not None
        assert cached.kind == "video"
        assert cached.display_name == "voice-note-1710000000.mp4"

    @pytest.mark.asyncio
    async def test_envelope_demux_fails_soft_when_a_scratch_file_cannot_be_made(self, monkeypatch, tmp_path):
        """A second scratch file that cannot be created must cost the demux, not the whole inbound event.

        ``_download_attachment`` only wraps its HTTP block, so an OSError from here would propagate through
        ``_cache_inbound_attachments`` into ``_handle_event`` and drop every attachment on the message. The
        first file must not leak either.
        """
        import errno
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        payload = b"\x00\x00\x00\x18ftypmp42 envelope bytes"
        _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        _fake_ffmpeg(monkeypatch)
        real_mkstemp = tempfile.mkstemp
        made = []

        def second_one_fails(**kwargs):
            made.append(kwargs)
            if len(made) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_mkstemp(**kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", second_one_fails)
        adapter = _make_adapter()

        cached = await adapter._download_attachment(self._envelope_metadata(payload))

        # The envelope is cached as the video it arrived as, and no scratch file is left behind.
        assert cached is not None and cached.kind == "video"
        assert len(made) == 2
        assert list(_buzz_mod._voice_note_workdir().iterdir()) == []

    @pytest.mark.asyncio
    async def test_plain_mp4_is_not_demuxed(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"\x00\x00\x00\x18ftypmp42 a real video"
        _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        calls = _fake_ffmpeg(monkeypatch)
        adapter = _make_adapter()
        metadata = dict(self._envelope_metadata(payload), filename="clip.mp4")

        cached = await adapter._download_attachment(metadata)

        assert cached is not None and cached.kind == "video"
        assert calls == []


class TestAttachmentReadAuthorization:
    """Attachment downloads carry a signed Blossom ``get`` authorization; the relay answers 401 without one."""

    @staticmethod
    def _metadata(payload: bytes) -> dict:
        return {
            "url": "https://test.relay/media/report.pdf",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "filename": "report.pdf",
            "mime_type": "application/pdf",
        }

    def test_blossom_auth_header_signs_get_event(self):
        adapter = _make_adapter({"relay_url": "wss://test.relay"})
        adapter._private_key = TEST_PRIVATE_KEY
        digest = "cd" * 32

        event = _decode_nostr_token(adapter._blossom_auth_header(digest, verb="get"))

        assert event["kind"] == 24242
        assert event["content"] == "Get file"
        assert event["pubkey"] == _nostr_auth.public_key_hex(TEST_PRIVATE_KEY)
        tags = _tag_map(event)
        assert tags["t"] == "get"
        assert tags["x"] == digest
        assert tags["server"] == "test.relay"
        assert int(tags["expiration"]) > event["created_at"]

    def test_blossom_auth_header_server_tag_drops_userinfo_and_keeps_odd_ports(self):
        """The relay normalizes scheme, path and default ports away, but not userinfo: never sign it in."""
        for relay_url, expected in (
            ("wss://user:pw@test.relay/relay", "test.relay"),
            ("wss://test.relay:443", "test.relay"),
            ("https://TEST.Relay./media", "test.relay"),
            ("wss://test.relay:8443", "test.relay:8443"),
            ("http://test.relay:8080", "test.relay:8080"),
        ):
            adapter = _make_adapter({"relay_url": relay_url})
            adapter._private_key = TEST_PRIVATE_KEY
            event = _decode_nostr_token(adapter._blossom_auth_header("cd" * 32))
            assert _tag_map(event)["server"] == expected, relay_url

    def test_request_headers_carry_get_auth_and_owner_tag(self):
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._auth_tag = '["auth","owner-attestation"]'
        digest = "ef" * 32

        headers = adapter._attachment_request_headers({"sha256": digest}, "https://test.relay/media/x.pdf")

        assert headers["Accept-Encoding"] == "identity"
        assert headers["x-auth-tag"] == '["auth","owner-attestation"]'
        event = _decode_nostr_token(headers["Authorization"])
        assert _tag_map(event)["t"] == "get"
        assert _tag_map(event)["x"] == digest

    def test_request_headers_never_credential_a_third_party_attachment_host(self):
        """A configured CDN is a trusted origin to download from, never a party to hand relay credentials to."""
        adapter = _make_adapter({"attachment_hosts": ["cdn.other.example", "test.relay:8443"]})
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._auth_tag = '["auth","owner-attestation"]'
        digest = "ef" * 32

        for foreign in ("https://cdn.other.example/media/x.pdf", "https://test.relay:8443/media/x.pdf"):
            assert adapter._attachment_request_headers({"sha256": digest}, foreign) == {"Accept-Encoding": "identity"}
        # Same host, default port spelled out: still the relay, so still credentialed.
        assert "Authorization" in adapter._attachment_request_headers({"sha256": digest}, "https://test.relay:443/media/x.pdf")

    def test_request_headers_stay_unauthenticated_without_a_usable_key(self):
        url = "https://test.relay/media/x.pdf"
        adapter = _make_adapter()
        adapter._private_key = ""
        assert adapter._attachment_request_headers({"sha256": "ab" * 32}, url) == {"Accept-Encoding": "identity"}

        adapter._private_key = "nsec1test"  # cannot sign: the read still goes out, just unauthenticated
        assert "Authorization" not in adapter._attachment_request_headers({"sha256": "ab" * 32}, url)

    @pytest.mark.asyncio
    async def test_download_sends_signed_get_authorization_for_the_blob(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"%PDF-1.4\n% signed read\n"
        metadata = self._metadata(payload)
        requests = _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY

        cached = await adapter._download_attachment(metadata)

        assert cached is not None and cached.kind == "document"
        (request,) = requests
        assert request.headers["accept-encoding"] == "identity"
        event = _decode_nostr_token(request.headers["authorization"])
        assert event["kind"] == 24242
        assert _tag_map(event)["t"] == "get"
        assert _tag_map(event)["x"] == metadata["sha256"]

    @pytest.mark.asyncio
    async def test_download_from_a_configured_cdn_carries_no_relay_credentials(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"%PDF-1.4\n% public blob\n"
        metadata = dict(self._metadata(payload), url="https://cdn.other.example/media/report.pdf")
        requests = _mock_http(monkeypatch, lambda request: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))}))
        adapter = _make_adapter({"attachment_hosts": ["cdn.other.example"]})
        adapter._private_key = TEST_PRIVATE_KEY
        adapter._auth_tag = '["auth","owner-attestation"]'

        cached = await adapter._download_attachment(metadata)

        assert cached is not None  # the download still happens, just without our credentials
        (request,) = requests
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers
        assert "x-auth-tag" not in request.headers

    @pytest.mark.asyncio
    async def test_download_reports_unauthorized_read(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        payload = b"%PDF-1.4\n% denied\n"
        _mock_http(monkeypatch, lambda request: httpx.Response(401, json={"error": "unauthorized"}))
        adapter = _make_adapter()
        adapter._private_key = TEST_PRIVATE_KEY

        assert await adapter._download_attachment(self._metadata(payload)) is None


class TestVoiceNoteClassification:
    """Voice notes dispatch as MessageType.VOICE (the only type the gateway transcribes); other audio stays AUDIO."""

    MP3 = b"\xff\xfb\x90\x00voice note frames"

    @staticmethod
    def _audio_metadata(payload: bytes, filename: str) -> dict:
        return {
            "url": f"https://test.relay/media/{filename}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "filename": filename,
            "mime_type": "audio/mpeg",
        }

    def _adapter_with_attachments(self, attachments):
        adapter = _make_adapter()
        adapter._user_names[OTHER_PUBKEY] = "Other"
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._cache_inbound_attachments = AsyncMock(return_value=attachments)
        dispatched = []

        async def capture(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture
        return adapter, dispatched

    @staticmethod
    def _audio_event(count: int) -> dict:
        event = _event("voice", content="@Chip listen")
        for index in range(count):
            event["tags"].append([
                "imeta", f"url https://test.relay/media/{index}.mp3", "m audio/mpeg",
                "x " + format(index + 1, "064x"), "size 1", f"filename voice-note-{index}.mp3",
            ])
        return event

    @pytest.mark.asyncio
    async def test_download_marks_voice_note_audio(self, monkeypatch, tmp_path):
        import httpx

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        _mock_http(monkeypatch, lambda request: httpx.Response(200, content=self.MP3, headers={"content-length": str(len(self.MP3))}))
        adapter = _make_adapter()

        voice = await adapter._download_attachment(self._audio_metadata(self.MP3, "voice-note-1710000000.mp3"))
        plain = await adapter._download_attachment(self._audio_metadata(self.MP3, "song.mp3"))

        assert isinstance(voice, _buzz_mod._VoiceNoteMedia) and voice.kind == "audio"
        assert isinstance(plain, CachedMedia) and not isinstance(plain, _buzz_mod._VoiceNoteMedia)

    @pytest.mark.asyncio
    async def test_voice_note_attachment_dispatches_as_voice(self):
        cached = CachedMedia("/cache/audio/note.mp3", "audio/mpeg", "audio", "voice-note-1.mp3")
        adapter, dispatched = self._adapter_with_attachments([_buzz_mod._VoiceNoteMedia.of(cached)])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], self._audio_event(1))

        assert dispatched[-1]["message_type"] is MessageType.VOICE
        assert dispatched[-1]["media_types"] == ["audio/mpeg"]

    @pytest.mark.asyncio
    async def test_plain_audio_attachment_stays_audio(self):
        adapter, dispatched = self._adapter_with_attachments([CachedMedia("/cache/audio/song.mp3", "audio/mpeg", "audio", "song.mp3")])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], self._audio_event(1))

        assert dispatched[-1]["message_type"] is MessageType.AUDIO

    @pytest.mark.asyncio
    async def test_voice_note_beside_plain_audio_stays_audio(self):
        voice = _buzz_mod._VoiceNoteMedia.of(CachedMedia("/cache/audio/note.mp3", "audio/mpeg", "audio", "voice-note-1.mp3"))
        song = CachedMedia("/cache/audio/song.mp3", "audio/mpeg", "audio", "song.mp3")
        adapter, dispatched = self._adapter_with_attachments([voice, song])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], self._audio_event(2))

        assert dispatched[-1]["message_type"] is MessageType.AUDIO

    @pytest.mark.asyncio
    async def test_voice_note_beside_document_uses_document(self):
        voice = _buzz_mod._VoiceNoteMedia.of(CachedMedia("/cache/audio/note.mp3", "audio/mpeg", "audio", "voice-note-1.mp3"))
        report = CachedMedia("/cache/documents/report.pdf", "application/pdf", "document", "report.pdf")
        adapter, dispatched = self._adapter_with_attachments([voice, report])

        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], self._audio_event(2))

        assert dispatched[-1]["message_type"] is MessageType.DOCUMENT

    def _localizing_adapter(self, tmp_path, url_payload: bytes):
        """Adapter whose ``buzz media get`` writes *url_payload*, plus the captured MessageEvents."""
        adapter = _make_adapter()
        captured = []

        async def capture(event):
            captured.append(event)

        async def cli(args, *, input_text=None):
            assert args[:2] == ["media", "get"]
            Path(args[args.index("-o") + 1]).write_bytes(url_payload)
            return 0, "", ""

        adapter.handle_message = capture
        adapter._message_handler = AsyncMock()
        adapter._run_cli = cli
        adapter.send_reaction = AsyncMock(return_value=True)
        return adapter, captured

    @pytest.mark.asyncio
    async def test_url_copy_of_the_same_voice_note_is_dropped_and_voice_is_kept(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        note = tmp_path / "note.mp3"
        note.write_bytes(self.MP3)
        adapter, captured = self._localizing_adapter(tmp_path, self.MP3)
        media_url = f"https://test.relay/media/{hashlib.sha256(self.MP3).hexdigest()}.mp3"

        await adapter._dispatch_message(
            text=f"[voice-note-1.mp3]({media_url})", chat_id=CHANNEL, chat_type="dm", user_id=OTHER_PUBKEY,
            user_name="Joel", message_id="voice-dup", created_at=1004,
            media_urls=[str(note)], media_types=["audio/mpeg"], message_type=MessageType.VOICE,
        )

        (event,) = captured
        assert event.message_type is MessageType.VOICE
        assert event.media_urls == [str(note)]
        assert event.media_types == ["audio/mpeg"]

    @pytest.mark.asyncio
    async def test_distinct_url_audio_beside_a_voice_note_uses_document(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        note = tmp_path / "note.mp3"
        note.write_bytes(self.MP3)
        other = b"\xff\xfb\x90\x00a different recording"
        adapter, captured = self._localizing_adapter(tmp_path, other)
        media_url = f"https://test.relay/media/{hashlib.sha256(other).hexdigest()}.mp3"

        await adapter._dispatch_message(
            text=f"and this one {media_url}", chat_id=CHANNEL, chat_type="dm", user_id=OTHER_PUBKEY,
            user_name="Joel", message_id="voice-plus-audio", created_at=1005,
            media_urls=[str(note)], media_types=["audio/mpeg"], message_type=MessageType.VOICE,
        )

        (event,) = captured
        assert event.message_type is MessageType.DOCUMENT
        assert len(event.media_urls) == 2 and event.media_urls[0] == str(note)

    @pytest.mark.asyncio
    async def test_no_attachment_is_hashed_when_the_text_carries_no_media(self, monkeypatch, tmp_path):
        """Nothing to compare against on the common path, so do not read every attachment back off disk."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        note = tmp_path / "note.mp3"
        note.write_bytes(self.MP3)
        adapter, captured = self._localizing_adapter(tmp_path, self.MP3)
        adapter._run_cli = AsyncMock(side_effect=AssertionError("no URL refs to localize"))
        monkeypatch.setattr(_buzz_mod, "_content_digest", lambda path: pytest.fail(f"hashed {path}"))

        await adapter._dispatch_message(
            text="a plain caption", chat_id=CHANNEL, chat_type="dm", user_id=OTHER_PUBKEY,
            user_name="Joel", message_id="voice-only", created_at=1006,
            media_urls=[str(note)], media_types=["audio/mpeg"], message_type=MessageType.VOICE,
        )

        (event,) = captured
        assert event.message_type is MessageType.VOICE and event.media_urls == [str(note)]


class TestThreadAnchoring:
    """A reply must JOIN the thread it was triggered from, not nest a new one.

    The gateway hands adapters the triggering message's own id as the reply
    anchor. For a top-level message that correctly opens a thread; for a
    message already inside a thread it used to nest a fresh sub-thread under
    every answer (an endless ladder of one-message threads in Buzz).
    """

    @staticmethod
    def _event(eid, *e_tags):
        return {"id": eid, "tags": [["h", CHANNEL], *e_tags]}

    def test_top_level_message_has_no_root(self):
        a = _make_adapter()
        assert a._extract_thread_root(self._event("m1")) is None

    def test_thread_opener_roots_at_its_parent(self):
        a = _make_adapter()
        ev = self._event("m2", ["e", "root1", "", "reply"])
        assert a._extract_thread_root(ev) == "root1"

    def test_in_thread_message_uses_root_marker_not_parent(self):
        a = _make_adapter()
        ev = self._event("m3", ["e", "root1", "", "root"], ["e", "m2", "", "reply"])
        assert a._extract_thread_root(ev) == "root1"

    def test_legacy_unmarked_etag_treated_as_parent(self):
        a = _make_adapter()
        assert a._extract_thread_root(self._event("m4", ["e", "root1"])) == "root1"

    def test_reply_to_top_level_still_opens_a_thread(self):
        """Regression guard: the original (correct) behaviour must survive."""
        a = _make_adapter()
        a._record_thread_root("m1", self._event("m1"))
        assert a._resolve_reply_anchor("m1") == "m1"

    def test_reply_inside_thread_joins_that_thread(self):
        a = _make_adapter()
        a._record_thread_root("m3", self._event(
            "m3", ["e", "root1", "", "root"], ["e", "m2", "", "reply"]))
        assert a._resolve_reply_anchor("m3") == "root1"

    def test_unknown_and_empty_anchors_pass_through(self):
        a = _make_adapter()
        assert a._resolve_reply_anchor("never-seen") == "never-seen"
        assert a._resolve_reply_anchor(None) is None

    def test_root_cache_is_bounded_and_evicts_oldest(self):
        a = _make_adapter()
        for i in range(a._THREAD_ROOT_CACHE + 50):
            a._record_thread_root(f"id{i}", self._event(f"id{i}"))
        assert len(a._thread_roots) == a._THREAD_ROOT_CACHE
        assert "id0" not in a._thread_roots
        assert f"id{a._THREAD_ROOT_CACHE + 49}" in a._thread_roots

    @pytest.mark.asyncio
    async def test_send_anchors_to_root_not_trigger(self):
        """End-to-end through send(): argv must carry the thread root."""
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "dm", "last_ts": 0, "seen": {}}
        adapter._record_thread_root("m3", self._event(
            "m3", ["e", "root1", "", "root"], ["e", "m2", "", "reply"]))
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "e9", "message": ""})
        adapter._run_cli = cli

        await adapter.send(CHANNEL, "in-thread answer", reply_to="m3")
        args, _stdin = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "root1"


# ── Inbound media localisation ─────────────────────────────────────────────────


class TestInboundMediaLocalisation:

    @staticmethod
    def _capture_dispatch(adapter, *, failed_urls=None):
        captured = []
        cli_calls = []
        failed_urls = set(failed_urls or [])

        async def capture(event):
            captured.append(event)

        async def cli(args, *, input_text=None):
            cli_calls.append(list(args))
            assert args[:2] == ["media", "get"]
            url = args[-1]
            if url in failed_urls:
                return 2, "", '{"error":"relay_error","message":"denied"}'
            output_path = args[args.index("-o") + 1]
            if url.endswith(".jpg"):
                payload = b"\xff\xd8\xff\xe0JFIF test image"
            elif url.endswith(".pdf"):
                payload = b"%PDF-1.4\n% test document\n"
            else:
                payload = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            with open(output_path, "wb") as handle:
                handle.write(payload)
            return 0, "", ""

        adapter.handle_message = capture
        adapter._message_handler = AsyncMock()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter._run_cli = cli
        # Localisation spends the agent's Buzz credentials, so it is gated on
        # an explicit gateway authorization. The gateway registers this check
        # on every adapter it constructs; tests are authorized by default and
        # override the callback where the gate itself is under test.
        adapter.set_authorization_check(lambda *_args: True)
        return captured, cli_calls

    @pytest.mark.asyncio
    async def test_markdown_relay_image_preserves_alt_text_before_dispatch(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, cli_calls = self._capture_dispatch(adapter)
        media_url = f"https://test.relay/media/{'a' * 64}.png"

        await adapter._dispatch_message(
            text=f"Please inspect this screenshot\n\n![Login error dialog]({media_url})",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="media-event",
            created_at=1000,
        )

        assert len(captured) == 1
        event = captured[0]
        assert event.text == "Please inspect this screenshot\n\nLogin error dialog"
        assert event.message_type == MessageType.PHOTO
        assert event.media_types == ["image/png"]
        assert len(event.media_urls) == 1
        assert event.media_urls[0].startswith(str(tmp_path / "hermes" / "cache"))
        assert media_url not in event.text
        assert cli_calls[0][-1] == media_url

    @pytest.mark.asyncio
    async def test_bare_relay_image_url_is_localised(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, _calls = self._capture_dispatch(adapter)
        media_url = f"https://test.relay/media/{'b' * 64}.png"

        await adapter._dispatch_message(
            text=f"What is in this? {media_url}",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="bare-media-event",
            created_at=1001,
        )

        event = captured[0]
        assert event.text == "What is in this?"
        assert event.message_type == MessageType.PHOTO
        assert event.media_types == ["image/png"]
        assert len(event.media_urls) == 1

    @pytest.mark.asyncio
    async def test_image_only_message_gets_attachment_placeholder(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, _calls = self._capture_dispatch(adapter)
        media_url = f"https://test.relay/media/{'5' * 64}.png"

        await adapter._dispatch_message(
            text=f"![]({media_url})",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="image-only-event",
            created_at=1002,
        )

        event = captured[0]
        assert event.text == "(attachment)"
        assert event.message_type == MessageType.PHOTO
        assert event.media_types == ["image/png"]
        assert len(event.media_urls) == 1

    @pytest.mark.asyncio
    async def test_multiple_images_are_localised_in_content_order(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, calls = self._capture_dispatch(adapter)
        first = f"https://test.relay/media/{'c' * 64}.png"
        second = f"https://test.relay/media/{'d' * 64}.jpg"

        await adapter._dispatch_message(
            text=f"Compare these\n![]({first})\n{second}",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="multi-media-event",
            created_at=1002,
        )

        event = captured[0]
        assert event.text == "Compare these"
        assert event.message_type == MessageType.PHOTO
        assert event.media_types == ["image/png", "image/jpeg"]
        assert len(event.media_urls) == 2
        assert [call[-1] for call in calls] == [first, second]

    @pytest.mark.asyncio
    async def test_non_image_attachment_is_cached_as_document(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, _calls = self._capture_dispatch(adapter)
        media_url = f"https://test.relay/media/{'e' * 64}.pdf"

        await adapter._dispatch_message(
            text=f"Read this report\n{media_url}",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="document-media-event",
            created_at=1003,
        )

        event = captured[0]
        assert event.text == "Read this report"
        assert event.message_type == MessageType.DOCUMENT
        assert event.media_types == ["application/pdf"]
        assert len(event.media_urls) == 1
        assert "/cache/documents/" in event.media_urls[0]

    @pytest.mark.asyncio
    async def test_download_failure_preserves_caption_and_alt_text(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        media_url = f"https://test.relay/media/{'f' * 64}.png"
        captured, _calls = self._capture_dispatch(adapter, failed_urls=[media_url])

        await adapter._dispatch_message(
            text=f"The error is visible here\n![Checkout error dialog]({media_url})",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="failed-media-event",
            created_at=1004,
        )

        event = captured[0]
        assert event.text == "The error is visible here\nCheckout error dialog"
        assert event.message_type == MessageType.TEXT
        assert event.media_urls == []
        assert event.media_types == []

    @pytest.mark.asyncio
    async def test_one_failed_download_does_not_drop_other_media(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        failed = f"https://test.relay/media/{'2' * 64}.png"
        succeeded = f"https://test.relay/media/{'3' * 64}.jpg"
        captured, calls = self._capture_dispatch(adapter, failed_urls=[failed])

        await adapter._dispatch_message(
            text=f"Compare what loaded\n![]({failed})\n![]({succeeded})",
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="partial-media-event",
            created_at=1005,
        )

        event = captured[0]
        assert event.text == "Compare what loaded"
        assert event.message_type == MessageType.PHOTO
        assert event.media_types == ["image/jpeg"]
        assert len(event.media_urls) == 1
        assert [call[-1] for call in calls] == [failed, succeeded]

    @pytest.mark.asyncio
    async def test_real_event_handler_localises_media_before_gateway_dispatch(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        adapter._channel_state[DM_CHANNEL] = {
            "chat_type": "dm",
            "last_ts": 0,
            "seen": {},
        }
        captured = []
        media_url = f"https://test.relay/media/{'4' * 64}.png"

        async def capture(event):
            captured.append(event)

        async def cli(args, *, input_text=None):
            if args[:2] == ["users", "get"]:
                return 0, json.dumps([{"display_name": "Joel"}]), ""
            assert args[:2] == ["media", "get"]
            output_path = args[args.index("-o") + 1]
            payload = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
                "+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            with open(output_path, "wb") as handle:
                handle.write(payload)
            return 0, "", ""

        adapter.handle_message = capture
        adapter._message_handler = AsyncMock()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter._run_cli = cli
        # As the gateway does for every adapter it constructs; the gate itself
        # is covered by TestInboundMediaAuthorizationGate.
        adapter.set_authorization_check(lambda *_args: True)

        await adapter._handle_event(
            DM_CHANNEL,
            adapter._channel_state[DM_CHANNEL],
            _tagged_event(
                "handler-media-event",
                DM_CHANNEL,
                content=f"Can you read this? ![]({media_url})",
                p=SELF_PUBKEY,
            ),
        )

        assert len(captured) == 1
        assert captured[0].text == "Can you read this?"
        assert captured[0].message_type == MessageType.PHOTO
        assert len(captured[0].media_urls) == 1

    @pytest.mark.asyncio
    async def test_external_media_url_is_left_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        adapter = _make_adapter()
        captured, calls = self._capture_dispatch(adapter)
        media_url = f"https://cdn.example/media/{'1' * 64}.png"
        text = f"External reference: ![]({media_url})"

        await adapter._dispatch_message(
            text=text,
            chat_id=CHANNEL,
            chat_type="dm",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="external-media-event",
            created_at=1006,
        )

        event = captured[0]
        assert event.text == text
        assert event.message_type == MessageType.TEXT
        assert event.media_urls == []
        assert calls == []


class TestInboundMediaAuthorizationGate:
    """Authenticated retrieval must never run for an unauthorized sender.

    ``buzz media get`` signs the request with this agent's own key, so a
    relay object named by an unauthorized sender must not be fetched or
    cached. Every non-``True`` outcome — denial, no registered check, a
    raising check, or a truthy non-boolean — must fail closed and leave the
    message text exactly as it arrived.
    """

    @staticmethod
    def _media_text():
        return f"look at this ![shot](https://test.relay/media/{'a' * 64}.png)"

    async def _dispatch_with_check(self, adapter, monkeypatch, tmp_path, check):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        captured, cli_calls = TestInboundMediaLocalisation._capture_dispatch(adapter)
        adapter.set_authorization_check(check)
        text = self._media_text()

        await adapter._dispatch_message(
            text=text,
            chat_id=CHANNEL,
            chat_type="group",
            user_id=OTHER_PUBKEY,
            user_name="Joel",
            message_id="gated-media-event",
            created_at=1007,
        )
        return captured, cli_calls, text

    @pytest.mark.asyncio
    async def test_denied_sender_media_is_not_downloaded(self, monkeypatch, tmp_path):
        adapter = _make_adapter()
        captured, cli_calls, text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, lambda *_args: False
        )

        assert cli_calls == []
        assert captured[0].text == text
        assert captured[0].message_type == MessageType.TEXT
        assert captured[0].media_urls == []

    @pytest.mark.asyncio
    async def test_missing_authorization_check_blocks_download(self, monkeypatch, tmp_path):
        adapter = _make_adapter()
        captured, cli_calls, text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, None
        )

        assert cli_calls == []
        assert captured[0].text == text
        assert captured[0].media_urls == []

    @pytest.mark.asyncio
    async def test_raising_authorization_check_blocks_download(self, monkeypatch, tmp_path):
        def boom(*_args):
            raise RuntimeError("auth backend down")

        adapter = _make_adapter()
        captured, cli_calls, text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, boom
        )

        assert cli_calls == []
        assert captured[0].text == text
        assert captured[0].media_urls == []

    @pytest.mark.asyncio
    async def test_truthy_non_boolean_is_not_an_authorization(self, monkeypatch, tmp_path):
        """A non-boolean result must not be coerced into a credentialed fetch."""
        adapter = _make_adapter()
        captured, cli_calls, text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, lambda *_args: "allowed"
        )

        assert cli_calls == []
        assert captured[0].text == text
        assert captured[0].media_urls == []

    @pytest.mark.asyncio
    async def test_adapter_allowlist_does_not_override_gateway_denial(
        self, monkeypatch, tmp_path
    ):
        """``allowed_users`` is a pre-filter, not a second source of truth."""
        adapter = _make_adapter({"allowed_users": [OTHER_PUBKEY]})
        captured, cli_calls, _text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, lambda *_args: False
        )

        assert OTHER_PUBKEY in adapter._allowed_pubkeys
        assert cli_calls == []
        assert captured[0].media_urls == []

    @pytest.mark.asyncio
    async def test_authorized_sender_still_downloads(self, monkeypatch, tmp_path):
        """The gate must not break the happy path it protects."""
        adapter = _make_adapter()
        captured, cli_calls, _text = await self._dispatch_with_check(
            adapter, monkeypatch, tmp_path, lambda *_args: True
        )

        assert len(cli_calls) == 1
        assert captured[0].message_type == MessageType.PHOTO
        assert len(captured[0].media_urls) == 1



    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "suffix"),
        [
            ("send_image_file", ".png"),
            ("send_video", ".mp4"),
            ("send_voice", ".ogg"),
            ("send_document", ".pdf"),
        ],
    )
    async def test_live_media_capabilities_upload_local_file_with_caption_and_reply(
        self, tmp_path, monkeypatch, method_name, suffix
    ):
        media = tmp_path / f"attachment{suffix}"
        media.write_bytes(b"media")
        # Without ffmpeg send_voice cannot build a voice note and uses the plain --file path like the others.
        monkeypatch.setattr(_buzz_mod, "_ffmpeg_path", lambda: None)
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-media"})
        adapter._run_cli = cli

        result = await getattr(adapter, method_name)(
            CHANNEL,
            str(media),
            caption="caption",
            reply_to="latest-child",
            metadata={"thread_id": "stable-root"},
        )

        assert result.success is True
        assert result.message_id == "evt-media"
        args, stdin_text = cli.calls[0]
        assert args[args.index("--file") + 1] == str(media)
        # Threading contract (#99429): stable thread root beats latest-child.
        assert args[args.index("--reply-to") + 1] == "stable-root"
        assert stdin_text == "caption"

    @pytest.mark.asyncio
    async def test_live_media_rejects_zero_exit_without_verified_event_id(self, tmp_path):
        media = tmp_path / "attachment.pdf"
        media.write_bytes(b"media")
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(
            return_value=(0, json.dumps({"accepted": True}), "")
        )

        result = await adapter.send_document(CHANNEL, str(media))

        assert result.success is False
        assert result.error == "invalid CLI response"
        assert result.raw_response is None

    @pytest.mark.asyncio
    async def test_live_media_redacts_long_path_before_bounding(self, tmp_path):
        parent = tmp_path
        private_parts = []
        for index in range(6):
            part = f"private-{index}-" + ("x" * 150)
            private_parts.append(part)
            parent = parent / part
            parent.mkdir()
        media = parent / "handoff.txt"
        media.write_text("safe handoff", encoding="utf-8")
        adapter = _make_adapter()
        adapter._run_cli = AsyncMock(
            return_value=(
                2,
                "",
                json.dumps(
                    {
                        "error": "network",
                        "message": f"upload failed for {media}: " + ("z" * 1_000),
                    }
                ),
            )
        )

        result = await adapter.send_document(CHANNEL, str(media))

        assert result.success is False
        assert all(part not in result.error for part in private_parts)
        assert "handoff.txt" in result.error
        assert len(result.error) <= 900

    @pytest.mark.asyncio
    async def test_send_to_platform_live_buzz_delivers_all_media(self, monkeypatch, tmp_path):
        from gateway.config import Platform
        from tools.send_message_tool import _send_to_platform

        first = tmp_path / "first.txt"
        second = tmp_path / "second.pdf"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-text"})
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-first"})
        cli.script("messages", "send", {"accepted": True, "event_id": "evt-second"})
        adapter._run_cli = cli
        platform = Platform("buzz")
        runner = SimpleNamespace(adapters={platform: adapter})
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)

        result = await _send_to_platform(
            platform,
            SimpleNamespace(enabled=True, token=None, extra={}),
            CHANNEL,
            "attached files",
            thread_id="root-event",
            media_files=[(str(first), False), (str(second), False)],
        )

        assert result == {
            "success": True,
            "message_id": "evt-second",
            "media_delivered": True,
        }
        assert len(cli.calls) == 3
        assert cli.calls[0][1] == "attached files"
        assert [call[0][call[0].index("--file") + 1] for call in cli.calls[1:]] == [
            str(first),
            str(second),
        ]
        assert all(
            call[0][call[0].index("--reply-to") + 1] == "root-event"
            for call in cli.calls
        )


# ── Lifecycle ─────────────────────────────────────────────────────────────


class TestBuzzAdapterLifecycle:


    @pytest.mark.asyncio
    async def test_disconnect_releases_scoped_lock(self, monkeypatch):
        """The identity lock taken in connect() must be released on disconnect."""
        import gateway.status as gateway_status

        released = []
        monkeypatch.setattr(
            gateway_status,
            "release_scoped_lock",
            lambda platform, key: released.append((platform, key)),
        )
        adapter = _make_adapter()
        adapter._lock_key = "wss://relay.example:" + SELF_PUBKEY
        await adapter.disconnect()
        assert released == [("buzz", "wss://relay.example:" + SELF_PUBKEY)]
        assert adapter._lock_key is None

    @pytest.mark.asyncio
    async def test_connect_fails_when_identity_lock_held(self, monkeypatch):
        """A second profile using the same relay+pubkey must fail fast."""
        import gateway.status as gateway_status

        monkeypatch.setattr(
            gateway_status, "acquire_scoped_lock", lambda platform, key: False
        )
        adapter = _make_adapter()
        adapter.cli_path = "/fake/buzz"
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1test")
        cli = _ScriptedCli()
        cli.script(
            "users", "get",
            [{"pubkey": SELF_PUBKEY, "display_name": "Chip"}],
        )
        adapter._run_cli = cli
        assert await adapter.connect() is False
        assert adapter._lock_key is None


# ── Credentials / requirements ────────────────────────────────────────────


class TestCredentialResolution:

    def test_env_key_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        assert _resolve_private_key() == "nsec1fromenv"

    def test_credentials_file_fallback(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "npub": "npub1x"}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert _resolve_private_key() == "nsec1fromfile"

    def test_owner_auth_tag_from_credentials_file(self, monkeypatch, tmp_path):
        tag = ["auth", "b" * 64, "", "c" * 128]
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "auth_tag": tag}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert json.loads(_resolve_auth_tag()) == tag

    @pytest.mark.parametrize("route", ["env-var", "config-yaml-extra"])
    def test_credentials_file_tag_is_not_paired_with_an_env_key(self, monkeypatch, tmp_path, route):
        """A NIP-OA tag attests ONE key, and build_auth_event signs whatever pair
        it is handed without checking them against each other. So when the key
        comes from the environment, the credentials record did not supply it and
        may not supply the tag either.

        The config.yaml route is the one no env rule can reach: an ACP host that
        injects a managed BUZZ_PRIVATE_KEY, and whose env the loader has already
        reduced to exactly that key, still used to pick up the tag through the
        profile's own `extra["credentials_file"]` and sign the managed key with
        the profile owner's attestation."""
        tag = ["auth", "b" * 64, "", "c" * 128]
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "auth_tag": tag}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        monkeypatch.delenv("BUZZ_AUTH_TAG", raising=False)
        extra = None
        if route == "env-var":
            monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        else:
            monkeypatch.delenv("BUZZ_CREDENTIALS_FILE", raising=False)
            extra = {"credentials_file": str(creds)}

        assert _resolve_private_key(extra) == "nsec1fromenv"  # the env key is what signs
        assert _resolve_auth_tag(extra) == ""                 # so the file's tag must not ride along

        # Control: with no env key the record supplies BOTH, and the pair is consistent.
        monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
        assert _resolve_private_key(extra) == "nsec1fromfile"
        assert json.loads(_resolve_auth_tag(extra)) == tag

    def test_invalid_owner_auth_tag_fails_closed(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "auth_tag": ["bad"]}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        with pytest.raises(ValueError, match="auth tag"):
            _resolve_auth_tag()

    def test_scoped_credentials_and_tag_do_not_borrow_ambient_profile(self, monkeypatch, tmp_path):
        from agent import secret_scope as ss

        ambient_tag = ["auth", "a" * 64, "", "d" * 128]
        scoped_tag = ["auth", "b" * 64, "", "c" * 128]
        ambient = tmp_path / "ambient.json"
        scoped = tmp_path / "scoped.json"
        ambient.write_text(json.dumps({"nsec": "nsec1ambient", "auth_tag": ambient_tag}))
        scoped.write_text(json.dumps({"nsec": "nsec1scoped", "auth_tag": scoped_tag}))
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(ambient))
        monkeypatch.setenv("BUZZ_AUTH_TAG", json.dumps(ambient_tag))
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"BUZZ_CREDENTIALS_FILE": str(scoped)})
        try:
            assert _resolve_private_key() == "nsec1scoped"
            assert json.loads(_resolve_auth_tag()) == scoped_tag
        finally:
            ss.reset_secret_scope(token)
            ss.set_multiplex_active(False)

    def test_owner_auth_tag_uses_credentials_autodiscovery(self, monkeypatch, tmp_path):
        tag = ["auth", "b" * 64, "", "c" * 128]
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "auth_tag": tag}), encoding="utf-8")
        monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path)
        assert _resolve_private_key() == "nsec1fromfile"
        assert json.loads(_resolve_auth_tag()) == tag

    def test_empty_multiplex_scope_never_autodiscovers_ambient_credentials(self, monkeypatch, tmp_path):
        from agent import secret_scope as ss

        tag = ["auth", "a" * 64, "", "d" * 128]
        (tmp_path / "general_credentials.json").write_text(
            json.dumps({"nsec": "nsec1ambient", "auth_tag": tag}), encoding="utf-8"
        )
        monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path)
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({})
        try:
            assert _resolve_private_key() == ""
            assert _resolve_auth_tag() == ""
        finally:
            ss.reset_secret_scope(token)
            ss.set_multiplex_active(False)


# ── Env enablement / registration / standalone send ──────────────────────


class TestEnvEnablement:

    def test_returns_none_when_unconfigured(self):
        assert _env_enablement() is None


class TestBuzzPluginRegistration:

    def test_register_platform_contract(self):
        from gateway.platform_registry import platform_registry

        platform_registry.unregister("buzz")
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "buzz"
        assert kwargs["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
        assert callable(kwargs["standalone_sender_fn"])
        assert callable(kwargs["env_enablement_fn"])
        assert set(kwargs["required_env"]) == {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"}


class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_success(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=30.0):
            captured.update(cli_path=cli_path, args=args, relay_url=relay_url, auth_tag=auth_tag, input_text=input_text)
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(PlatformConfig(enabled=True, extra={}), CHANNEL, "cron says hi")
        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][:2] == ["messages", "send"]
        assert captured["input_text"] == "cron says hi"
        # The private key must never be part of argv
        assert all("nsec1x" not in str(a) for a in captured["args"])

    @pytest.mark.asyncio
    async def test_standalone_send_injects_owner_auth_tag_from_credentials_file(
        self, monkeypatch, tmp_path
    ):
        """Cron/standalone path must load NIP-OA auth_tag from credentials JSON.

        Main-line regression: when only BUZZ_PRIVATE_KEY is ambient and the
        credentials file holds auth_tag, omitting injection causes relay 403
        membership failures on owner-gated relays.
        """
        from gateway.config import PlatformConfig

        tag = ["auth", "b" * 64, "", "c" * 128]
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(
            json.dumps({"nsec": "nsec1fromfile", "auth_tag": tag}),
            encoding="utf-8",
        )
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("BUZZ_AUTH_TAG", raising=False)

        captured = {}

        async def fake_exec(
            cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=30.0
        ):
            captured.update(
                private_key=private_key,
                auth_tag=auth_tag,
                args=args,
                input_text=input_text,
            )
            return 0, json.dumps({"accepted": True, "event_id": "evt-auth", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}), CHANNEL, "cron needs owner auth"
        )
        assert result == {"success": True, "message_id": "evt-auth"}
        assert captured["private_key"] == "nsec1fromfile"
        assert json.loads(captured["auth_tag"]) == tag
        # Secrets stay out of argv (auth_tag is env-injected by _exec_buzz).
        joined_args = " ".join(str(a) for a in captured["args"])
        assert "nsec1fromfile" not in joined_args
        assert tag[1] not in joined_args
        assert tag[3] not in joined_args

    @pytest.mark.asyncio
    async def test_standalone_send_key_only_does_not_invent_auth_tag(
        self, monkeypatch, tmp_path
    ):
        """Direct private key without credentials_file must not invent an auth tag."""
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        monkeypatch.delenv("BUZZ_AUTH_TAG", raising=False)
        monkeypatch.delenv("BUZZ_CREDENTIALS_FILE", raising=False)
        # Ambient credentials dir must not be borrowed when a direct key is set.
        ambient = tmp_path / "ambient_credentials.json"
        ambient.write_text(
            json.dumps({"nsec": "nsec1ambient", "auth_tag": ["auth", "a" * 64, "", "d" * 128]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path)

        captured = {}

        async def fake_exec(
            cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=30.0
        ):
            captured.update(private_key=private_key, auth_tag=auth_tag)
            return 0, json.dumps({"accepted": True, "event_id": "evt-key", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)
        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}), CHANNEL, "key only"
        )
        assert result == {"success": True, "message_id": "evt-key"}
        assert captured["private_key"] == "nsec1x"
        assert captured["auth_tag"] == ""

    @pytest.mark.asyncio
    async def test_standalone_send_retries_unresolved_presentation_mention(
        self, monkeypatch, tmp_path
    ):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        sent = []

        async def fake_exec(
            cli_path,
            args,
            *,
            relay_url,
            private_key,
            auth_tag="",
            input_text=None,
            timeout=30.0,
        ):
            sent.append(input_text)
            if len(sent) == 1:
                return (
                    1,
                    "",
                    "user_error: mention '@session' does not match a current "
                    "channel member; retry with --mention <pubkey>",
                )
            return (
                0,
                json.dumps(
                    {"accepted": True, "event_id": "evt-standalone", "message": ""}
                ),
                "",
            )

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}),
            CHANNEL,
            "See @session:default/example.",
        )

        assert result == {"success": True, "message_id": "evt-standalone"}
        assert sent == [
            "See @session:default/example.",
            "See @\u200bsession:default/example.",
        ]

    @pytest.mark.asyncio
    async def test_standalone_send_extracts_path_from_media_descriptor(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        document = tmp_path / "report.txt"
        document.write_text("report", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=30.0):
            captured["args"] = args
            return 0, json.dumps({"accepted": True, "event_id": "evt-media"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}),
            CHANNEL,
            "attached",
            media_files=[(str(document), False)],
        )

        assert result == {
            "success": True,
            "message_id": "evt-media",
            "media_delivered": True,
        }
        file_index = captured["args"].index("--file")
        assert captured["args"][file_index + 1] == str(document)




# ── Editing and deleting (streaming) ──────────────────────────────────


class TestBuzzAdapterEdit:

    @pytest.mark.asyncio
    async def test_edit_targets_the_original_event_and_uses_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "edit", {"accepted": True, "event_id": "edit1", "message": ""})
        adapter._run_cli = cli

        result = await adapter.edit_message(CHANNEL, "orig1", "partial answer")
        assert result.success is True

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "edit"]
        assert args[args.index("--event") + 1] == "orig1"
        # Content travels via stdin (--content -), never argv, same as send
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "partial answer"

    @pytest.mark.asyncio
    async def test_edit_returns_the_original_id_not_the_cli_event_id(self):
        """The stream consumer re-edits ONE message id for the whole stream.

        buzz-cli reports a fresh event id for each edit; returning that would
        make the second edit address a message that was never sent.
        """
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "edit", {"accepted": True, "event_id": "edit1"})
        adapter._run_cli = cli

        result = await adapter.edit_message(CHANNEL, "orig1", "text")
        assert result.message_id == "orig1"

    @pytest.mark.asyncio
    async def test_edit_marks_its_own_event_seen(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "edit", {"accepted": True, "event_id": "edit1"})
        adapter._run_cli = cli

        await adapter.edit_message(CHANNEL, "orig1", "text")
        assert "edit1" in adapter._channel_state[CHANNEL]["seen"]

    @pytest.mark.asyncio
    async def test_edit_accepts_finalize_without_changing_behaviour(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "edit", {"accepted": True, "event_id": "edit1"})
        adapter._run_cli = cli

        result = await adapter.edit_message(CHANNEL, "orig1", "text", finalize=True)
        assert result.success is True
        assert len(cli.calls) == 1

    @pytest.mark.asyncio
    async def test_edit_without_a_message_id_never_calls_the_cli(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        adapter._run_cli = cli

        result = await adapter.edit_message(CHANNEL, "", "text")
        assert result.success is False
        assert cli.calls == []

    @pytest.mark.asyncio
    async def test_edit_with_empty_content_never_calls_the_cli(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        adapter._run_cli = cli

        result = await adapter.edit_message(CHANNEL, "orig1", "")
        assert result.success is False
        assert cli.calls == []

    @pytest.mark.asyncio
    async def test_edit_relay_error_is_retryable_but_bad_input_is_not(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "edit", "", code=2, stderr="relay unreachable")
        adapter._run_cli = cli
        relay_failure = await adapter.edit_message(CHANNEL, "orig1", "text")
        assert relay_failure.success is False
        assert relay_failure.retryable is True

        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "edit", "", code=1, stderr="bad input")
        adapter._run_cli = cli
        input_failure = await adapter.edit_message(CHANNEL, "orig1", "text")
        assert input_failure.success is False
        assert input_failure.retryable is False

    @pytest.mark.asyncio
    async def test_delete_targets_the_event(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "delete", {"accepted": True, "event_id": "del1"})
        adapter._run_cli = cli

        assert await adapter.delete_message(CHANNEL, "orig1") is True
        assert cli.calls[0][0] == ["messages", "delete", "--event", "orig1"]

    @pytest.mark.asyncio
    async def test_delete_failure_returns_false(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "delete", "", code=2, stderr="relay unreachable")
        adapter._run_cli = cli

        assert await adapter.delete_message(CHANNEL, "orig1") is False

    @pytest.mark.asyncio
    async def test_delete_without_a_message_id_never_calls_the_cli(self):
        adapter = _make_adapter()
        cli = _ScriptedCli()
        adapter._run_cli = cli

        assert await adapter.delete_message(CHANNEL, "") is False
        assert cli.calls == []
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            "not json",
            json.dumps([]),
            json.dumps({"event_id": "evt"}),
            json.dumps({"accepted": True}),
            json.dumps({"accepted": True, "event_id": ""}),
        ],
    )
    async def test_standalone_send_rejects_invalid_zero_exit_receipt(
        self, monkeypatch, tmp_path, stdout
    ):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        monkeypatch.setattr(
            _buzz_mod,
            "_exec_buzz",
            AsyncMock(return_value=(0, stdout, "")),
        )

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}), CHANNEL, "hello"
        )

        assert result == {"error": "Buzz standalone send failed: invalid CLI response"}

    @pytest.mark.asyncio
    async def test_standalone_send_rejection_is_useful_bounded_and_not_delivered(
        self, monkeypatch, tmp_path
    ):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))
        monkeypatch.setattr(
            _buzz_mod,
            "_exec_buzz",
            AsyncMock(
                return_value=(
                    0,
                    json.dumps({"accepted": False, "message": "upload rejected " + "x" * 100_000}),
                    "",
                )
            ),
        )

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}),
            CHANNEL,
            "hello",
            media_files=[("/tmp/report.txt", False)],
        )

        assert "upload rejected" in result["error"]
        assert len(result["error"]) <= 1024
        assert "success" not in result
        assert "media_delivered" not in result
# ── Durable channel cursors across restart (#90464) ───────────────────────


class TestChannelCursorPersistence:
    """A restart must resume from the saved cursor, not reseed from history.

    Seeding marks every event currently in the channel as seen. Anything that
    arrived while the gateway was down is in that history, so an unconditional
    reseed swallows it permanently even though the relay still has it.
    """

    @pytest.fixture
    def adapter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @staticmethod
    def _cursor_file(tmp_path):
        return tmp_path / "buzz" / "channel-cursors.json"

    async def _seed(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        adapter._load_cursors()
        await adapter._seed_channel(CHANNEL, chat_type="group")
        adapter._save_cursors()
        return cli

    @pytest.mark.asyncio
    async def test_seed_writes_a_cursor(self, adapter, tmp_path):
        await self._seed(adapter, _event("e1", created_at=100), _event("e2", created_at=200))

        saved = json.loads(self._cursor_file(tmp_path).read_text(encoding="utf-8"))
        assert saved["identity"] == SELF_PUBKEY
        assert saved["relay"] == "https://test.relay"
        assert saved["channels"][CHANNEL]["last_ts"] == 200
        assert saved["channels"][CHANNEL]["seen"] == ["e1", "e2"]

    @pytest.mark.asyncio
    async def test_restart_resumes_instead_of_reseeding(self, adapter, tmp_path, monkeypatch):
        await self._seed(adapter, _event("e1", content="@Chip first", created_at=100))

        # Restart. The relay now also holds a mention that landed while the
        # gateway was down, and it sits in the same history a reseed reads.
        restarted = _make_adapter()
        restarted._dispatched = []

        async def capture(**kwargs):
            restarted._dispatched.append(kwargs)

        restarted._dispatch_message = capture
        restarted._message_handler = AsyncMock()
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip first", created_at=100),
            _event("e2", content="@Chip sent while you were down", created_at=150),
        ])
        restarted._run_cli = cli
        restarted._load_cursors()
        await restarted._seed_channel(CHANNEL, chat_type="group")

        # Restoring must not spend a CLI call on history it is not going to use.
        assert cli.calls == []
        state = restarted._channel_state[CHANNEL]
        assert set(state["seen"]) == {"e1"}
        assert state["last_ts"] == 100

        # The first poll after the restart delivers the missed mention.
        await restarted._poll_channel(CHANNEL)
        assert [d["message_id"] for d in restarted._dispatched] == ["e2"]

    @pytest.mark.asyncio
    async def test_cursor_survives_only_for_the_same_identity_and_relay(self, adapter, tmp_path, monkeypatch):
        await self._seed(adapter, _event("e1", created_at=100))

        # Same machine, different bot: channel ids would collide but the event
        # stream behind them is another one, so the cursor must be ignored.
        other = _make_adapter()
        other._self_pubkey = OTHER_PUBKEY
        other._load_cursors()
        assert other._restored_cursors == {}

        elsewhere = _make_adapter({"relay_url": "https://other.relay"})
        elsewhere._load_cursors()
        assert elsewhere._restored_cursors == {}

    @pytest.mark.asyncio
    async def test_unreadable_cursor_file_falls_back_to_seeding(self, adapter, tmp_path):
        path = self._cursor_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", created_at=100)])
        adapter._run_cli = cli
        adapter._load_cursors()
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Degrades to the old behaviour rather than failing the connect.
        assert adapter._restored_cursors == {}
        assert set(adapter._channel_state[CHANNEL]["seen"]) == {"e1"}

    @pytest.mark.asyncio
    async def test_restored_seen_set_stays_bounded(self, adapter, tmp_path):
        cap = _buzz_mod._SEEN_CAP
        path = self._cursor_file(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "identity": SELF_PUBKEY,
                "relay": "https://test.relay",
                "channels": {
                    CHANNEL: {
                        "chat_type": "group",
                        "last_ts": 100,
                        "seen": [f"e{i}" for i in range(cap * 2)],
                    }
                },
            }),
            encoding="utf-8",
        )
        adapter._load_cursors()
        await adapter._seed_channel(CHANNEL, chat_type="group")

        seen = adapter._channel_state[CHANNEL]["seen"]
        assert len(seen) == cap
        # The newest ids are the ones worth keeping for de-dupe.
        assert f"e{cap * 2 - 1}" in seen
        assert "e0" not in seen

    @pytest.mark.asyncio
    async def test_idle_poll_does_not_rewrite_the_cursor(self, adapter, tmp_path):
        cli = await self._seed(adapter, _event("e1", created_at=100))
        path = self._cursor_file(tmp_path)
        before = path.stat().st_mtime_ns

        cli.responses.clear()
        cli.script("messages", "get", [_event("e1", created_at=100)])
        await adapter._poll_channel(CHANNEL)

        assert path.stat().st_mtime_ns == before
