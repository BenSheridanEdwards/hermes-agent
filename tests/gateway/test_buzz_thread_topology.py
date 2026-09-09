"""E2E regression tests for the Buzz thread-topology salvage cluster.

Covers the composed behavior of PRs #77080 / #79578 / #80120 / #85613 /
#86232 / #89868 (+ issues #75082, #95841, #95842):

1. NIP-10 thread-root anchoring — replies join the EXISTING thread root
   instead of nesting a new sub-thread per turn, across send(),
   send_image(), and inbound session thread_id.
2. reply_in_thread / reply_to_mode config honoring — the opt-out posts
   flat on every send path, including progress routing and the
   out-of-process cron sender.
3. _PLATFORM_DEFAULTS coverage — buzz no longer inherits the verbose
   _GLOBAL_DEFAULTS (#95841).

Uses the real adapter module (no gateway process) with synthetic NIP-10
events shaped like live relay traffic.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_buzz_module():
    path = REPO_ROOT / "plugins" / "platforms" / "buzz" / "adapter.py"
    spec = importlib.util.spec_from_file_location("plugin_adapter_buzz_threads", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_buzz_mod = _load_buzz_module()
BuzzAdapter = _buzz_mod.BuzzAdapter

CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
OTHER_PUBKEY = "b" * 64
ROOT_EVT = "a" * 64
MID_EVT = "c" * 64


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch, tmp_path):
    for var in (
        "BUZZ_RELAY_URL", "BUZZ_CHANNELS", "BUZZ_HOME_CHANNEL",
        "BUZZ_POLL_INTERVAL", "BUZZ_CLI_PATH", "BUZZ_CREDENTIALS_FILE",
        "BUZZ_ALLOWED_USERS", "BUZZ_ALLOW_ALL_USERS", "BUZZ_PRIVATE_KEY",
        "BUZZ_REQUIRE_MENTION", "BUZZ_REPLY_IN_THREAD", "BUZZ_REPLY_TO_MODE",
        "BUZZ_TRANSPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _make_adapter(extra=None, **cfg_kwargs):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        enabled=True,
        extra={"relay_url": "https://test.relay", **(extra or {})},
        **cfg_kwargs,
    )
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = _buzz_mod.hex_to_npub(SELF_PUBKEY)
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    return adapter


class _CapturingCli:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"accepted": True, "event_id": "evt-out"}

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        return 0, json.dumps(self.payload), ""


def _nip10_reply_event(event_id, *, root, parent, content="in thread", pubkey=OTHER_PUBKEY):
    """A kind-9 event shaped like a live relay in-thread reply."""
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": 1000,
        "kind": 9,
        "tags": [
            ["h", CHANNEL],
            ["e", root, "", "root"],
            ["e", parent, "", "reply"],
        ],
    }


def _top_level_event(event_id, content="@Chip hello", pubkey=OTHER_PUBKEY):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": 1000,
        "kind": 9,
        "tags": [["h", CHANNEL]],
    }



async def _stub_cli(args, *, input_text=None):
    return 0, "[]", ""

# ── 1. Thread-root anchoring ──────────────────────────────────────────────


class TestThreadRootAnchoring:

    @pytest.mark.asyncio
    async def test_reply_to_in_thread_trigger_anchors_to_root(self):
        """E2E: inbound NIP-10 reply -> send(reply_to=<trigger>) -> --reply-to <root>."""
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter._run_cli = _stub_cli

        event = _nip10_reply_event("trigger-evt", root=ROOT_EVT, parent=MID_EVT,
                                   content="@Chip what next?")
        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "the answer", reply_to="trigger-evt")
        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == ROOT_EVT

    @pytest.mark.asyncio
    async def test_reply_to_top_level_trigger_opens_one_thread(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter._run_cli = _stub_cli

        await adapter._handle_event(
            CHANNEL, adapter._channel_state[CHANNEL], _top_level_event("root-msg")
        )
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "answer", reply_to="root-msg")
        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "root-msg"

    @pytest.mark.asyncio
    async def test_inbound_thread_id_is_nip10_root(self):
        """Session scoping: dispatched source.thread_id is the stable root."""
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        dispatched = []

        async def capture(event):
            dispatched.append(event)

        adapter._message_handler = AsyncMock()
        adapter.handle_message = capture
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter._run_cli = _stub_cli

        event = _nip10_reply_event("child-evt", root=ROOT_EVT, parent=MID_EVT,
                                   content="@Chip follow-up")
        await adapter._handle_event(CHANNEL, adapter._channel_state[CHANNEL], event)
        assert dispatched and dispatched[0].source.thread_id == ROOT_EVT

    @pytest.mark.asyncio
    async def test_send_image_anchors_to_root_too(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        adapter._record_thread_root(
            "trigger-evt", _nip10_reply_event("trigger-evt", root=ROOT_EVT, parent=MID_EVT)
        )
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send_image(CHANNEL, str(img), caption="pic", reply_to="trigger-evt")
        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == ROOT_EVT


# ── 2. Config honoring: reply_in_thread / reply_to_mode ─────────────────


class TestReplyThreadingConfig:

    @pytest.mark.asyncio
    async def test_default_threads_replies(self):
        adapter = _make_adapter()
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "hi", reply_to="evt-1")
        assert "--reply-to" in cli.calls[0][0]

    @pytest.mark.asyncio
    async def test_reply_in_thread_false_posts_flat(self):
        adapter = _make_adapter(extra={"reply_in_thread": False})
        assert adapter._reply_to_mode == "off"
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "hi", reply_to="evt-1",
                           metadata={"thread_id": "evt-1", "reply_to_message_id": "evt-1"})
        assert "--reply-to" not in cli.calls[0][0]

    @pytest.mark.asyncio
    async def test_reply_to_mode_off_posts_flat(self):
        adapter = _make_adapter(reply_to_mode="off")
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "hi", reply_to="evt-1")
        assert "--reply-to" not in cli.calls[0][0]

    @pytest.mark.asyncio
    async def test_env_reply_in_thread_false_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_REPLY_IN_THREAD", "false")
        adapter = _make_adapter()
        assert adapter._reply_to_mode == "off"

    @pytest.mark.asyncio
    async def test_reply_in_thread_true_keeps_threading(self):
        adapter = _make_adapter(extra={"reply_in_thread": True})
        assert adapter._reply_to_mode != "off"

    @pytest.mark.asyncio
    async def test_send_image_honors_opt_out(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter(extra={"reply_in_thread": False})
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send_image(CHANNEL, str(img), caption="pic", reply_to="evt-1")
        assert "--reply-to" not in cli.calls[0][0]

    def test_apply_yaml_config_bridges_keys(self, monkeypatch):
        monkeypatch.delenv("BUZZ_REPLY_IN_THREAD", raising=False)
        monkeypatch.delenv("BUZZ_REPLY_TO_MODE", raising=False)
        _buzz_mod._apply_yaml_config(
            {}, {"extra": {"reply_in_thread": False, "reply_to_mode": "off"}}
        )
        import os
        assert os.environ["BUZZ_REPLY_IN_THREAD"] == "false"
        assert os.environ["BUZZ_REPLY_TO_MODE"] == "off"
        monkeypatch.delenv("BUZZ_REPLY_IN_THREAD", raising=False)
        monkeypatch.delenv("BUZZ_REPLY_TO_MODE", raising=False)

    @pytest.mark.asyncio
    async def test_standalone_send_honors_opt_out(self, monkeypatch, tmp_path):
        """Out-of-process cron delivery must not thread when opted out."""
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("BUZZ_REPLY_IN_THREAD", "false")

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=None):
            captured["args"] = args
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        class _PC:
            extra = {"relay_url": "https://test.relay", "cli_path": str(fake_cli)}

        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1test")
        result = await _buzz_mod._standalone_send(_PC(), CHANNEL, "cron msg", thread_id="evt-1")
        assert result.get("success") is True
        assert "--reply-to" not in captured["args"]

    @pytest.mark.asyncio
    async def test_standalone_send_threads_by_default(self, monkeypatch, tmp_path):
        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n")
        fake_cli.chmod(0o755)
        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, auth_tag="", input_text=None, timeout=None):
            captured["args"] = args
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron"}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        class _PC:
            extra = {"relay_url": "https://test.relay", "cli_path": str(fake_cli)}

        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1test")
        result = await _buzz_mod._standalone_send(_PC(), CHANNEL, "cron msg", thread_id="evt-1")
        assert result.get("success") is True
        assert "--reply-to" in captured["args"]
        assert captured["args"][captured["args"].index("--reply-to") + 1] == "evt-1"


# ── 3. Progress routing honors the opt-out ───────────────────────────────


class TestProgressRouting:

    def test_buzz_progress_threads_by_default(self):
        from gateway.run import _resolve_progress_thread_id

        assert _resolve_progress_thread_id(
            "buzz", source_thread_id=None, event_message_id="evt-1",
            reply_in_thread=True,
        ) == "evt-1"

    def test_buzz_progress_flat_when_opted_out(self):
        from gateway.run import _resolve_progress_thread_id

        assert _resolve_progress_thread_id(
            "buzz", source_thread_id=None, event_message_id="evt-1",
            reply_in_thread=False,
        ) is None


# ── 4. Display defaults (#95841) ─────────────────────────────────────────


class TestDisplayDefaults:

    def test_buzz_has_platform_defaults_entry(self):
        from gateway.display_config import _PLATFORM_DEFAULTS

        assert "buzz" in _PLATFORM_DEFAULTS

    def test_buzz_does_not_inherit_verbose_global_tool_progress(self):
        from gateway.display_config import resolve_display_setting

        # No user config: must come from the buzz platform tier, not the
        # verbose _GLOBAL_DEFAULTS ("all").
        assert resolve_display_setting({}, "buzz", "tool_progress") != "all"


# ── 5. Direct-message carve-out (buzz#32) ────────────────────────────────

DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"
TEST_PRIVATE_KEY = "00" * 31 + "03"


def _dm_top_level_event(event_id, content="what is on my calendar?", pubkey=OTHER_PUBKEY):
    """A kind-9 DM with no NIP-10 tags: the everyday "user asks the agent something" event."""
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": 1000,
        "kind": 9,
        "tags": [["h", DM_CHANNEL]],
    }


def _dm_reply_event(event_id, *, root, parent, content="and tomorrow?", pubkey=OTHER_PUBKEY):
    """A kind-9 DM the human deliberately posted inside a thread."""
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": 1000,
        "kind": 9,
        "tags": [["h", DM_CHANNEL], ["e", root, "", "root"], ["e", parent, "", "reply"]],
    }


class _RelayWs:
    """Minimal relay websocket that accepts every EVENT frame it is handed."""

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


async def _publish_voice_note(adapter, channel, **kwargs):
    """Drive _publish_voice_note against a fake relay socket; returns the signed event."""
    import sys
    from types import ModuleType
    from unittest.mock import patch

    ws = _RelayWs()
    fake_ws_mod = ModuleType("websockets")
    fake_ws_mod.connect = lambda *a, **kw: ws
    adapter._private_key = TEST_PRIVATE_KEY
    adapter._authenticate_websocket = AsyncMock()
    desc = {"url": "https://test.relay/media/x.mp3", "sha256": "d" * 64, "size": 5}
    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        result = await adapter._publish_voice_note(
            channel, desc, "voice-note-1.mp3", "audio/mpeg", **kwargs)
    assert result.success is True
    return ws.sent[0][1]


def _reply_tags(event):
    return [tag for tag in event["tags"] if tag[0] == "e"]


async def _receive(adapter, channel, event):
    """Feed an inbound event through the real handler so thread state is recorded as it is live."""
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter.send_reaction = AsyncMock(return_value=True)
    adapter._run_cli = _stub_cli
    await adapter._handle_event(channel, adapter._channel_state[channel], event)


def _dm_adapter(**kwargs):
    adapter = _make_adapter(**kwargs)
    adapter._channel_state[DM_CHANNEL] = {"chat_type": "dm", "last_ts": 0, "seen": {}}
    return adapter


def _group_adapter(**kwargs):
    adapter = _make_adapter(**kwargs)
    adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
    return adapter


class TestDirectMessageReplyAnchoring:
    """A DM answer to a top-level message must itself be top-level.

    Buzz clients keep depth-1 replies out of the main timeline unless the event carries a
    broadcast tag, so an anchored answer in a 1:1 conversation renders nowhere the human is
    looking. Mirrors buzz-acp's own rule in crates/buzz-acp/src/queue.rs (format_prompt):
    in a DM, anchor only when the triggering event already carries a NIP-10 root.
    """

    @pytest.mark.asyncio
    async def test_dm_top_level_trigger_posts_flat(self):
        adapter = _dm_adapter()
        await _receive(adapter, DM_CHANNEL, _dm_top_level_event("dm-evt"))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(DM_CHANNEL, "the answer", reply_to="dm-evt")

        args, _ = cli.calls[0]
        assert "--reply-to" not in args

    @pytest.mark.asyncio
    async def test_channel_top_level_trigger_still_threads(self):
        """The control: channels are where threading is wanted and must not change."""
        adapter = _group_adapter()
        await _receive(adapter, CHANNEL, _top_level_event("chan-evt"))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(CHANNEL, "the answer", reply_to="chan-evt")

        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "chan-evt"

    @pytest.mark.asyncio
    async def test_dm_in_thread_trigger_still_joins_the_thread(self):
        """A thread the human started inside a DM is still answered in that thread."""
        adapter = _dm_adapter()
        await _receive(
            adapter, DM_CHANNEL,
            _dm_reply_event("dm-child", root=ROOT_EVT, parent=MID_EVT))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(DM_CHANNEL, "the answer", reply_to="dm-child")

        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == ROOT_EVT

    @pytest.mark.asyncio
    async def test_dm_progress_synthesised_thread_id_posts_flat(self):
        """Streaming and progress sends carry a thread_id synthesised from the trigger id
        (_resolve_progress_thread_id), which is not evidence of a real thread."""
        adapter = _dm_adapter()
        await _receive(adapter, DM_CHANNEL, _dm_top_level_event("dm-evt"))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send(
            DM_CHANNEL, "working on it",
            metadata={"thread_id": "dm-evt", "reply_to_message_id": "dm-evt"})

        args, _ = cli.calls[0]
        assert "--reply-to" not in args

    @pytest.mark.asyncio
    async def test_dm_carve_out_wins_over_reply_to_mode(self):
        """reply_to_mode selects WHICH anchor to thread on, never whether a DM is threaded."""
        for mode in ("first", "all"):
            adapter = _dm_adapter(reply_to_mode=mode)
            await _receive(adapter, DM_CHANNEL, _dm_top_level_event("dm-evt"))
            cli = _CapturingCli()
            adapter._run_cli = cli
            await adapter.send(DM_CHANNEL, "the answer", reply_to="dm-evt")
            args, _ = cli.calls[0]
            assert "--reply-to" not in args, f"reply_to_mode={mode} re-anchored a DM reply"

    @pytest.mark.asyncio
    async def test_unclassified_channel_keeps_threading(self):
        """Only a conversation the adapter classified as a DM takes the carve-out."""
        adapter = _make_adapter()
        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send("unknown-chat", "the answer", reply_to="evt-1")
        assert "--reply-to" in cli.calls[0][0]

    # ── Attachments publish through their own paths and need the same rule ──

    @pytest.mark.asyncio
    async def test_dm_file_attachment_posts_flat(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _dm_adapter()
        await _receive(adapter, DM_CHANNEL, _dm_top_level_event("dm-evt"))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send_image(DM_CHANNEL, str(img), caption="pic", reply_to="dm-evt")

        args, _ = cli.calls[0]
        assert "--file" in args and "--reply-to" not in args

    @pytest.mark.asyncio
    async def test_channel_file_attachment_still_threads(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _group_adapter()
        await _receive(adapter, CHANNEL, _top_level_event("chan-evt"))

        cli = _CapturingCli()
        adapter._run_cli = cli
        await adapter.send_image(CHANNEL, str(img), caption="pic", reply_to="chan-evt")

        args, _ = cli.calls[0]
        assert args[args.index("--reply-to") + 1] == "chan-evt"

    @pytest.mark.asyncio
    async def test_dm_voice_note_posts_flat(self):
        """Voice notes publish a signed event directly, bypassing --reply-to entirely."""
        adapter = _dm_adapter()
        await _receive(adapter, DM_CHANNEL, _dm_top_level_event("dm-evt"))

        event = await _publish_voice_note(
            adapter, DM_CHANNEL, caption="listen", reply_to="dm-evt", metadata=None)

        assert _reply_tags(event) == []

    @pytest.mark.asyncio
    async def test_channel_voice_note_still_threads(self):
        adapter = _group_adapter()
        await _receive(adapter, CHANNEL, _top_level_event("chan-evt"))

        event = await _publish_voice_note(
            adapter, CHANNEL, caption="listen", reply_to="chan-evt", metadata=None)

        assert _reply_tags(event) == [["e", "chan-evt", "", "reply"]]

    @pytest.mark.asyncio
    async def test_dm_voice_note_in_thread_still_joins_the_thread(self):
        adapter = _dm_adapter()
        await _receive(
            adapter, DM_CHANNEL,
            _dm_reply_event("dm-child", root=ROOT_EVT, parent=MID_EVT))

        event = await _publish_voice_note(
            adapter, DM_CHANNEL, caption="listen", reply_to="dm-child", metadata=None)

        assert _reply_tags(event) == [["e", ROOT_EVT, "", "reply"]]
