"""ACP voice turns (issue #29): text_to_speech in the ACP toolset, audio attachments transcribed
before the model sees them, and the per-turn voice-first instruction."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import acp
from acp.schema import (
    AudioContentBlock,
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ResourceContentBlock,
    TextContentBlock,
)

from acp_adapter import voice
from acp_adapter.content import _content_blocks_to_openai_user_content, audio_attachments
from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager, _expand_acp_enabled_toolsets, default_acp_toolsets
from gateway.run_inbound import _EMPTY_TRANSCRIPT_NOTE, transcribe_clip
from tools import tts_tool

OGG_BYTES = b"OggS" + b"\x00" * 64


def _ogg(tmp_path, name="note.ogg"):
    path = tmp_path / name
    path.write_bytes(OGG_BYTES)
    return path


def _link(path, mime="audio/ogg", **extra):
    kwargs = {"type": "resource_link", "name": path.name, "uri": path.as_uri()}
    if mime is not None:
        kwargs["mimeType"] = mime
    kwargs.update(extra)
    return ResourceContentBlock(**kwargs)


@pytest.fixture
def stt(monkeypatch):
    """Mock STT provider: ``calls`` records paths; ``result``/``fallback`` shape the replies."""
    from tools import transcription_tools

    box = SimpleNamespace(calls=[], fallback_calls=[], enabled=True,
                          result={"success": True, "transcript": "what did I say"},
                          fallback={"success": False, "error": "no local model"})

    def _transcribe(path, model=None, source=None):
        box.calls.append((path, source))
        return dict(box.result)

    def _fallback(path, model=None):
        box.fallback_calls.append(path)
        return dict(box.fallback)

    monkeypatch.setattr(transcription_tools, "is_stt_enabled", lambda *_a, **_k: box.enabled)
    monkeypatch.setattr(transcription_tools, "transcribe_audio", _transcribe)
    monkeypatch.setattr(transcription_tools, "transcribe_audio_local_fallback", _fallback)
    return box


@pytest.fixture
def config(monkeypatch):
    """Config seen by ``acp_adapter.session`` helpers (``voice.auto_tts`` on by default)."""
    from hermes_cli import config as cfg

    data = {"voice": {"auto_tts": True}, "acp": {}}
    monkeypatch.setattr(cfg, "load_config", lambda *_a, **_k: data)
    return data


def _agent_with_tts(has_tool=True):
    return SimpleNamespace(valid_tool_names={"text_to_speech", "terminal"} if has_tool else {"terminal"},
                           ephemeral_system_prompt=None)


# ---------------------------------------------------------------------------
# (1) Toolset
# ---------------------------------------------------------------------------


class TestToolset:
    def test_hermes_acp_carries_text_to_speech(self):
        from toolsets import TOOLSETS

        tools = TOOLSETS["hermes-acp"]["tools"]
        assert "text_to_speech" in tools
        assert "clarify" not in tools and "send_message" not in tools

    def test_text_to_speech_dropped_when_no_tts_provider(self, monkeypatch):
        """The tool's check_fn is the gate: unconfigured TTS means no tool in the ACP list."""
        from tools.registry import registry

        entry = registry.get_entry("text_to_speech")
        assert entry is not None and entry.check_fn is tts_tool.check_tts_requirements
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "elevenlabs"})
        monkeypatch.setattr(tts_tool, "_resolve_provider_key", lambda *_a, **_k: None)
        assert tts_tool.check_tts_requirements() is False

    def test_default_toolsets_is_hermes_acp_without_config(self):
        assert default_acp_toolsets({}) == ["hermes-acp"]
        assert default_acp_toolsets({"acp": {"toolsets": []}}) == ["hermes-acp"]

    def test_acp_toolsets_override(self):
        assert default_acp_toolsets({"acp": {"toolsets": ["hermes-acp", "tts"]}}) == ["hermes-acp", "tts"]
        assert default_acp_toolsets({"acp": {"toolsets": "coding"}}) == ["coding"]

    def test_expand_uses_configured_default(self, config):
        config["acp"]["toolsets"] = ["coding"]
        assert _expand_acp_enabled_toolsets(None, mcp_server_names=["srv"]) == ["coding", "mcp-srv"]
        assert _expand_acp_enabled_toolsets(["hermes-acp"]) == ["hermes-acp"]


# ---------------------------------------------------------------------------
# (3) Detection: audio blocks never go through the binary/text inlining path
# ---------------------------------------------------------------------------


class TestDetection:
    def test_resource_link_by_mime_and_by_extension(self, tmp_path):
        by_mime = _link(_ogg(tmp_path, "a.bin"), mime="audio/ogg")
        by_ext = _link(_ogg(tmp_path, "b.m4a"), mime=None)
        found = audio_attachments([TextContentBlock(type="text", text="hi"), by_mime, by_ext])
        assert [a.index for a in found] == [1, 2]
        assert found[0].mime == "audio/ogg" and found[1].mime == "audio/mp4"
        assert found[0].path == tmp_path / "a.bin"

    def test_explicit_non_audio_mime_wins_over_extension(self, tmp_path):
        notes = tmp_path / "notes.wav"
        notes.write_text("just text", encoding="utf-8")
        assert audio_attachments([_link(notes, mime="text/plain")]) == []
        content = _content_blocks_to_openai_user_content([_link(notes, mime="text/plain")])
        assert "just text" in content

    def test_audio_block_and_embedded_blob(self):
        blob = base64.b64encode(OGG_BYTES).decode()
        prompt = [
            AudioContentBlock(type="audio", data=blob, mimeType="audio/ogg"),
            EmbeddedResourceContentBlock(
                type="resource",
                resource=BlobResourceContents(uri="file:///tmp/clip.mp3", blob=blob, mimeType="audio/mpeg"),
            ),
        ]
        found = audio_attachments(prompt)
        assert [a.suffix for a in found] == [".ogg", ".mp3"]
        assert all(a.data == OGG_BYTES for a in found)

    def test_embedded_audio_is_never_inlined_or_truncated(self):
        big = base64.b64encode(b"\x01" * (600 * 1024)).decode()
        block = EmbeddedResourceContentBlock(
            type="resource", resource=BlobResourceContents(uri="file:///tmp/big.wav", blob=big, mimeType="audio/wav"))
        content = _content_blocks_to_openai_user_content([TextContentBlock(type="text", text="listen"), block])
        assert isinstance(content, str)
        assert "Binary file omitted" not in content and "truncated" not in content
        assert content.startswith("[The user sent an audio attachment")
        assert content.endswith("listen")

    def test_audio_notes_lead_the_payload(self, tmp_path):
        link = _link(_ogg(tmp_path))
        content = _content_blocks_to_openai_user_content(
            [TextContentBlock(type="text", text="what did I say"), link], audio_notes={1: '"hello there"'})
        assert content == '"hello there"\nwhat did I say'

    def test_text_only_prompt_unchanged(self):
        assert _content_blocks_to_openai_user_content([TextContentBlock(type="text", text="/help")]) == "/help"


# ---------------------------------------------------------------------------
# (3) STT before the model: prepare_voice_turn
# ---------------------------------------------------------------------------


class TestPrepareVoiceTurn:
    @pytest.mark.asyncio
    async def test_no_audio_returns_none(self, tmp_path, stt, config):
        turn = await voice.prepare_voice_turn(
            [TextContentBlock(type="text", text="hi")], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn is None and stt.calls == []

    @pytest.mark.asyncio
    async def test_transcript_is_quoted_and_prepended(self, tmp_path, stt, config):
        clip = _ogg(tmp_path)
        turn = await voice.prepare_voice_turn(
            [_link(clip), TextContentBlock(type="text", text="typed too")], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [(str(clip), "acp")]
        assert turn.transcripts == ["what did I say"]
        assert turn.audio_notes == {0: '"what did I say"'}
        assert turn.prompt_text("typed too") == '"what did I say"\n\ntyped too'
        assert turn.prompt_text("") == '"what did I say"'

    @pytest.mark.asyncio
    async def test_stt_failure_falls_back_to_neutral_note(self, tmp_path, stt, config):
        clip = _ogg(tmp_path)
        stt.result = {"success": False, "error": "boom"}
        turn = await voice.prepare_voice_turn([_link(clip)], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.fallback_calls == [str(clip)]
        assert turn.transcripts == []
        note = turn.audio_notes[0]
        assert note.startswith("[voice message could not be transcribed automatically")
        assert clip.name in note
        assert "STT" not in note and "provider" not in note

    @pytest.mark.asyncio
    async def test_local_fallback_recovers(self, tmp_path, stt, config):
        clip = _ogg(tmp_path)
        stt.result = {"success": False, "error": "cloud down"}
        stt.fallback = {"success": True, "transcript": "local words"}
        turn = await voice.prepare_voice_turn([_link(clip)], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.audio_notes[0] == '"local words"'

    @pytest.mark.asyncio
    async def test_empty_transcript_sentinel(self, tmp_path, stt, config):
        stt.result = {"success": True, "transcript": "   "}
        turn = await voice.prepare_voice_turn([_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.audio_notes[0] == _EMPTY_TRANSCRIPT_NOTE

    @pytest.mark.asyncio
    async def test_stt_disabled_keeps_attached_note(self, tmp_path, stt, config):
        clip = _ogg(tmp_path)
        stt.enabled = False
        turn = await voice.prepare_voice_turn([_link(clip)], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == []
        assert turn.audio_notes[0] == f"[The user sent a voice message: {clip}]"

    @pytest.mark.asyncio
    async def test_missing_file_is_reported_not_transcribed(self, tmp_path, stt, config):
        ghost = tmp_path / "elsewhere" / "missing.ogg"
        turn = await voice.prepare_voice_turn([_link(ghost)], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and stt.fallback_calls == []
        assert "not available" in turn.audio_notes[0] and str(ghost) in turn.audio_notes[0]

    @pytest.mark.asyncio
    async def test_link_outside_cwd_is_still_a_host_file(self, tmp_path, stt, config):
        """The host owns attachment placement (its scratch dir is outside the session cwd)."""
        outside = tmp_path / "scratch"
        outside.mkdir()
        clip = _ogg(outside)
        turn = await voice.prepare_voice_turn([_link(clip)], cwd=str(tmp_path / "project"), agent=_agent_with_tts())
        assert stt.calls == [(str(clip), "acp")]

    @pytest.mark.asyncio
    async def test_embedded_blob_is_materialized_then_cleaned(self, tmp_path, stt, config, monkeypatch):
        monkeypatch.setattr(voice.tempfile, "gettempdir", lambda: str(tmp_path))
        block = AudioContentBlock(type="audio", data=base64.b64encode(OGG_BYTES).decode(), mimeType="audio/ogg")
        turn = await voice.prepare_voice_turn([block], cwd=str(tmp_path), agent=_agent_with_tts())
        [(path, _)] = stt.calls
        assert path.startswith(str(tmp_path / "hermes_voice" / "acp_in_")) and path.endswith(".ogg")
        assert turn.temp_paths == [path]
        voice.cleanup_voice_turn(turn)
        assert not (tmp_path / "hermes_voice").joinpath(path.split("/")[-1]).exists()

    @pytest.mark.asyncio
    async def test_failed_embedded_blob_keeps_file_named_in_note(self, tmp_path, stt, config, monkeypatch):
        monkeypatch.setattr(voice.tempfile, "gettempdir", lambda: str(tmp_path))
        stt.result = {"success": False, "error": "boom"}
        block = AudioContentBlock(type="audio", data=base64.b64encode(OGG_BYTES).decode(), mimeType="audio/ogg")
        turn = await voice.prepare_voice_turn([block], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        voice.cleanup_voice_turn(turn)
        assert (tmp_path / "hermes_voice" / path.split("/")[-1]).exists()

    @pytest.mark.asyncio
    async def test_oversized_embedded_blob_never_hits_disk(self, tmp_path, stt, config, monkeypatch):
        monkeypatch.setattr(voice, "_MAX_EMBEDDED_AUDIO_BYTES", 16)
        monkeypatch.setattr(voice.tempfile, "gettempdir", lambda: str(tmp_path))
        block = AudioContentBlock(type="audio", data=base64.b64encode(OGG_BYTES).decode(), mimeType="audio/ogg")
        turn = await voice.prepare_voice_turn([block], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and turn.temp_paths == []
        assert "too large" in turn.audio_notes[0]
        assert not (tmp_path / "hermes_voice").exists()


# ---------------------------------------------------------------------------
# (2) Voice-first gating and the per-turn instruction
# ---------------------------------------------------------------------------


class TestVoiceReply:
    @pytest.mark.asyncio
    async def test_voice_turn_with_tts_wants_spoken_reply(self, tmp_path, stt, config):
        turn = await voice.prepare_voice_turn([_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.voice_reply is True
        assert turn.output_dir == str(tmp_path / "voice")

    @pytest.mark.asyncio
    async def test_no_tts_tool_means_text_only(self, tmp_path, stt, config):
        turn = await voice.prepare_voice_turn(
            [_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts(has_tool=False))
        assert turn.voice_reply is False and turn.output_dir is None
        assert turn.audio_notes[0] == '"what did I say"'  # STT still ran

    @pytest.mark.asyncio
    async def test_auto_tts_off_means_text_only(self, tmp_path, stt, config):
        config["voice"]["auto_tts"] = False
        turn = await voice.prepare_voice_turn([_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.voice_reply is False

    @pytest.mark.asyncio
    async def test_acp_auto_tts_overrides_voice_default(self, tmp_path, stt, config):
        config["voice"]["auto_tts"] = False
        config["acp"]["auto_tts"] = True
        turn = await voice.prepare_voice_turn([_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.voice_reply is True
        config["voice"]["auto_tts"] = True
        config["acp"]["auto_tts"] = False
        turn = await voice.prepare_voice_turn([_link(_ogg(tmp_path))], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.voice_reply is False

    def test_voice_output_dir(self, tmp_path):
        assert voice.voice_output_dir(str(tmp_path), {}) == str(tmp_path / "voice")
        assert voice.voice_output_dir(str(tmp_path), {"acp": {"voice_dir": "out/audio"}}) == str(tmp_path / "out" / "audio")
        assert voice.voice_output_dir(str(tmp_path), {"acp": {"voice_dir": ""}}) is None
        spaced = tmp_path / "my project"
        assert voice.voice_output_dir(str(spaced), {}) is None

    def test_instruction_names_tool_and_media_line(self):
        text = voice.voice_reply_instruction("/work/voice")
        assert "VOICE TURN" in text and "text_to_speech" in text and "MEDIA:" in text
        assert "/work/voice" in text
        assert "output_path unset" in text
        assert "\u2014" not in text

    def test_bind_sets_instruction_and_tts_dir_then_restores(self, tmp_path):
        agent = SimpleNamespace(ephemeral_system_prompt="keep me", valid_tool_names={"text_to_speech"})
        turn = voice.VoiceTurn(voice_reply=True, output_dir=str(tmp_path / "voice"))
        before = tts_tool._default_output_dir()
        restore = voice.bind_voice_turn(agent, turn)
        try:
            assert agent.ephemeral_system_prompt.startswith("keep me\n\n")
            assert "VOICE TURN" in agent.ephemeral_system_prompt
            assert tts_tool._default_output_dir() == str(tmp_path / "voice")
        finally:
            restore()
        assert agent.ephemeral_system_prompt == "keep me"
        assert tts_tool._default_output_dir() == before

    def test_bind_is_noop_for_text_only_turn(self):
        agent = SimpleNamespace(ephemeral_system_prompt=None)
        restore = voice.bind_voice_turn(agent, voice.VoiceTurn(voice_reply=False))
        assert agent.ephemeral_system_prompt is None
        restore()

    def test_explicit_output_path_still_wins_over_bound_dir(self, tmp_path):
        token = tts_tool.set_tts_output_dir(str(tmp_path / "voice"))
        try:
            path, err = tts_tool._resolve_output_base(str(tmp_path / "explicit.mp3"), "xai", None, False)
        finally:
            tts_tool.reset_tts_output_dir(token)
        assert err is None and path == tmp_path / "explicit.mp3"


# ---------------------------------------------------------------------------
# Shared gateway helper
# ---------------------------------------------------------------------------


class TestTranscribeClip:
    @pytest.mark.asyncio
    async def test_success_fallback_failure_and_empty(self):
        ok = lambda p, m=None, s=None: {"success": True, "transcript": "words"}  # noqa: E731
        bad = lambda p, m=None, s=None: {"success": False, "error": "x"}  # noqa: E731
        empty = lambda p, m=None, s=None: {"success": True, "transcript": ""}  # noqa: E731
        assert await transcribe_clip("/c.ogg", ok, bad) == ("words", '"words"')
        assert await transcribe_clip("/c.ogg", bad, lambda p, m=None: {"success": True, "transcript": "local"}) == (
            "local", '"local"')
        transcript, note = await transcribe_clip("/c.ogg", bad, bad, failure_note=lambda p: f"failed {p}")
        assert transcript is None and note == "failed /c.ogg"
        assert await transcribe_clip("/c.ogg", empty, bad) == (None, _EMPTY_TRANSCRIPT_NOTE)

    @pytest.mark.asyncio
    async def test_source_label_forwarded(self):
        seen = []

        def _t(path, model=None, source=None):
            seen.append(source)
            return {"success": True, "transcript": "w"}

        await transcribe_clip("/c.ogg", _t, _t, source="acp")
        assert seen == ["acp"]


# ---------------------------------------------------------------------------
# End to end through HermesACPAgent.prompt
# ---------------------------------------------------------------------------


class TestPromptIntegration:
    async def _drive(self, prompt_blocks, tmp_path, has_tool=True):
        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        state = manager.get_session(resp.session_id)
        seen = {}

        def _run(*args, **kwargs):
            seen["user_message"] = kwargs.get("user_message")
            seen["persist"] = kwargs.get("persist_user_message")
            seen["ephemeral"] = state.agent.ephemeral_system_prompt
            seen["tts_dir"] = tts_tool._default_output_dir()
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run
        state.agent.model, state.agent.provider = "test-model", "openrouter"
        state.agent.ephemeral_system_prompt = None
        state.agent.valid_tool_names = {"text_to_speech", "terminal"} if has_tool else {"terminal"}
        conn = MagicMock(spec=acp.Client)
        conn.session_update = AsyncMock()
        server._conn = conn
        await server.prompt(prompt=prompt_blocks, session_id=resp.session_id)
        seen["ephemeral_after"] = state.agent.ephemeral_system_prompt
        seen["tts_dir_after"] = tts_tool._default_output_dir()
        return seen

    @pytest.mark.asyncio
    async def test_voice_prompt_is_transcribed_and_asks_for_voice_reply(self, tmp_path, stt, config):
        clip = _ogg(tmp_path)
        seen = await self._drive([_link(clip), TextContentBlock(type="text", text="what did I say")], tmp_path)
        assert stt.calls == [(str(clip), "acp")]
        assert seen["user_message"] == '"what did I say"\nwhat did I say'
        assert seen["persist"] == '"what did I say"\n\nwhat did I say'
        assert "VOICE TURN" in seen["ephemeral"] and "text_to_speech" in seen["ephemeral"]
        assert seen["tts_dir"] == str(tmp_path / "voice")
        assert seen["ephemeral_after"] is None
        assert seen["tts_dir_after"] != str(tmp_path / "voice")

    @pytest.mark.asyncio
    async def test_audio_only_prompt_runs(self, tmp_path, stt, config):
        seen = await self._drive([_link(_ogg(tmp_path))], tmp_path)
        assert seen["user_message"] == '"what did I say"'

    @pytest.mark.asyncio
    async def test_text_prompt_never_triggers_voice(self, tmp_path, stt, config):
        seen = await self._drive([TextContentBlock(type="text", text="hi")], tmp_path)
        assert stt.calls == []
        assert seen["user_message"] == "hi" and seen["ephemeral"] is None
        assert seen["tts_dir"] != str(tmp_path / "voice")

    @pytest.mark.asyncio
    async def test_tts_not_configured_gives_text_only_turn(self, tmp_path, stt, config):
        seen = await self._drive([_link(_ogg(tmp_path))], tmp_path, has_tool=False)
        assert seen["user_message"] == '"what did I say"'
        assert seen["ephemeral"] is None and seen["tts_dir"] != str(tmp_path / "voice")

    @pytest.mark.asyncio
    async def test_stt_exception_does_not_break_the_turn(self, tmp_path, stt, config, monkeypatch):
        from tools import transcription_tools

        def _explode(*_a, **_k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(transcription_tools, "transcribe_audio", _explode)
        clip = _ogg(tmp_path)
        seen = await self._drive([_link(clip), TextContentBlock(type="text", text="hello")], tmp_path)
        assert seen["user_message"].endswith("hello")
        assert "could not be transcribed" in seen["user_message"]
        assert "VOICE TURN" in seen["ephemeral"]


def test_tts_tool_result_reports_media_path():
    """The ACP instruction relies on the tool result carrying a ``MEDIA:<path>`` line."""
    assert tts_tool._media_tag(["/w/voice/tts_1.mp3"], False) == "MEDIA:/w/voice/tts_1.mp3"
    payload = json.loads(json.dumps({"media_tag": tts_tool._media_tag(["/w/voice/a.ogg"], True)}))
    assert payload["media_tag"].splitlines()[-1] == "MEDIA:/w/voice/a.ogg"
