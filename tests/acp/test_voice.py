"""ACP voice turns (issue #29): text_to_speech in the ACP toolset, audio attachments transcribed
before the model sees them, and the per-turn voice-first instruction."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
import time
from pathlib import Path
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
from acp_adapter.session import (
    QueuedPrompt, SessionManager, _expand_acp_enabled_toolsets, default_acp_toolsets,
)
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


@pytest.fixture
def audio_cache(tmp_path, monkeypatch):
    """Redirect the profile audio cache (where embedded clips are materialized) into tmp_path."""
    from gateway.platforms import base

    cache = tmp_path / "hermes_audio_cache"
    monkeypatch.setattr(base, "AUDIO_CACHE_DIR", cache)
    return cache


def _agent_with_tts(has_tool=True):
    return SimpleNamespace(valid_tool_names={"text_to_speech", "terminal"} if has_tool else {"terminal"},
                           ephemeral_system_prompt=None)


# ---------------------------------------------------------------------------
# (1) Toolset
# ---------------------------------------------------------------------------


class TestToolset:
    def test_hermes_acp_itself_is_unchanged_by_the_voice_work(self):
        """text_to_speech rides in on the separate ``tts`` toolset, not on hermes-acp, so every
        other consumer of hermes-acp sees the list it always had."""
        from toolsets import TOOLSETS

        tools = TOOLSETS["hermes-acp"]["tools"]
        assert "text_to_speech" not in tools
        assert "clarify" not in tools and "send_message" not in tools
        assert TOOLSETS["tts"]["tools"] == ["text_to_speech"]

    def test_text_to_speech_dropped_when_no_tts_provider(self, monkeypatch):
        """The tool's check_fn is the gate: unconfigured TTS means no tool in the ACP list."""
        from tools.registry import registry

        entry = registry.get_entry("text_to_speech")
        assert entry is not None and entry.check_fn is tts_tool.check_tts_requirements
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "elevenlabs"})
        monkeypatch.setattr(tts_tool, "_resolve_provider_key", lambda *_a, **_k: None)
        assert tts_tool.check_tts_requirements() is False

    def test_acp_sessions_get_tts_on_top_by_default(self):
        assert default_acp_toolsets({}) == ["hermes-acp", "tts"]
        assert default_acp_toolsets({"acp": {"toolsets": []}}) == ["hermes-acp", "tts"]

    def test_acp_tts_false_opts_out(self):
        assert default_acp_toolsets({"acp": {"tts": False}}) == ["hermes-acp"]
        assert default_acp_toolsets({"acp": {"tts": False, "toolsets": ["coding"]}}) == ["coding"]
        # Any other value keeps the default on; only an explicit false opts out.
        assert default_acp_toolsets({"acp": {"tts": True}}) == ["hermes-acp", "tts"]

    def test_acp_toolsets_override(self):
        assert default_acp_toolsets({"acp": {"toolsets": ["hermes-acp", "tts"]}}) == ["hermes-acp", "tts"]
        assert default_acp_toolsets({"acp": {"toolsets": "coding"}}) == ["coding", "tts"]

    def test_expand_uses_configured_default(self, config):
        config["acp"]["toolsets"] = ["coding"]
        assert _expand_acp_enabled_toolsets(None, mcp_server_names=["srv"]) == ["coding", "tts", "mcp-srv"]
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
        """One separator: what is persisted as the user message and what the model sees agree."""
        link = _link(_ogg(tmp_path))
        blocks = [TextContentBlock(type="text", text="what did I say"), link]
        content = _content_blocks_to_openai_user_content(blocks, audio_notes={1: '"hello there"'})
        assert content == '"hello there"\n\nwhat did I say'
        turn = voice.VoiceTurn(audio_notes={1: '"hello there"'})
        assert turn.prompt_text("what did I say") == content

    def test_text_only_prompt_unchanged(self):
        assert _content_blocks_to_openai_user_content([TextContentBlock(type="text", text="/help")]) == "/help"

    def test_octet_stream_does_not_beat_an_audio_extension(self, tmp_path):
        """A host that labels its blob application/octet-stream must not send a voice note down
        the binary-omitted path."""
        clip = _ogg(tmp_path, "voice-note.ogg")
        [att] = audio_attachments([_link(clip, mime="application/octet-stream")])
        assert att.mime == "audio/ogg" and att.path == clip

    def test_extension_only_classification_checks_the_magic_bytes(self, tmp_path):
        """A text file named .wav is not audio, so it is never uploaded to STT unsniffed."""
        fake = tmp_path / "notes.wav"
        fake.write_text("dear diary, this is not a wav", encoding="utf-8")
        assert audio_attachments([_link(fake, mime=None)]) == []
        content = _content_blocks_to_openai_user_content([_link(fake, mime=None)])
        assert "dear diary" in content
        # A real container with the same extension-only link still classifies as audio.
        real = tmp_path / "real.wav"
        real.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        assert [a.mime for a in audio_attachments([_link(real, mime=None)])] == ["audio/wav"]

    def test_extension_only_link_to_a_missing_file_stays_audio(self, tmp_path):
        """Nothing to sniff: keep the extension's verdict so the note names the missing clip."""
        ghost = tmp_path / "gone.ogg"
        assert [a.mime for a in audio_attachments([_link(ghost, mime=None)])] == ["audio/ogg"]

    def test_unknown_audio_mime_suffix_is_a_container_not_bin(self):
        """``.bin`` is rejected by ``tools.transcription_audio`` on extension alone, so an
        ``audio/*`` type outside the map could only ever produce a failure note, and a failure
        note keeps its clip. The sniffer overrides this whenever it recognises the bytes."""
        from tools.transcription_common import SUPPORTED_FORMATS
        from acp_adapter.content import AudioAttachment

        def _suffix(mime):
            return AudioAttachment(index=0, uri="", display="voice-note", mime=mime).suffix

        assert _suffix("audio/amr") == ".ogg"
        assert _suffix("audio/3gpp") == ".ogg"
        assert _suffix("audio/x-caf") == ".caf"
        assert all(_suffix(m) in SUPPORTED_FORMATS for m in ("audio/amr", "audio/3gpp", "audio/x-caf"))

    def test_an_explicit_audio_mime_still_wins_without_sniffing(self, tmp_path):
        notes = tmp_path / "clip.ogg"
        notes.write_text("not really ogg", encoding="utf-8")
        assert [a.mime for a in audio_attachments([_link(notes, mime="audio/ogg")])] == ["audio/ogg"]


class TestBase64Payloads:
    """``b64decode(validate=True)`` rejects line-wrapped and URL-safe base64; the old fallback
    encoded the base64 *text* as the clip and uploaded that to the STT provider."""

    def _block(self, blob):
        return AudioContentBlock(type="audio", data=blob, mimeType="audio/ogg")

    def test_line_wrapped_base64_decodes_to_the_real_bytes(self):
        wrapped = base64.encodebytes(OGG_BYTES).decode()
        assert "\n" in wrapped
        [att] = audio_attachments([self._block(wrapped)])
        assert att.data == OGG_BYTES

    def test_urlsafe_alphabet_decodes_to_the_real_bytes(self):
        payload = b"\xfb\xff" + OGG_BYTES
        urlsafe = base64.urlsafe_b64encode(payload).decode()
        assert "-" in urlsafe or "_" in urlsafe
        [att] = audio_attachments([self._block(urlsafe)])
        assert att.data == payload

    def test_non_base64_is_refused_rather_than_treated_as_audio(self):
        [att] = audio_attachments([self._block("this is not base64 at all!!")])
        assert att.data is None

    @pytest.mark.asyncio
    async def test_non_base64_never_reaches_stt_or_disk(self, tmp_path, stt, config, audio_cache):
        turn = await voice.prepare_voice_turn(
            [self._block("this is not base64 at all!!")], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and turn.temp_paths == []
        assert "could not be read" in turn.audio_notes[0]
        assert not audio_cache.exists()


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


# ---------------------------------------------------------------------------
# (1) Embedded clips: the gateway audio cache, private, cleaned on every exit
# ---------------------------------------------------------------------------


def _blob_block(data=OGG_BYTES, mime="audio/ogg"):
    return AudioContentBlock(type="audio", data=base64.b64encode(data).decode(), mimeType=mime)


class TestEmbeddedClipFiles:
    @pytest.mark.asyncio
    async def test_materialized_into_the_audio_cache_and_cleaned(self, tmp_path, stt, config, audio_cache):
        """Reuses the gateway helper (issue #29): under HERMES_HOME, so the agent-visible path
        mapping and the cache sweep both reach it, and container-sniffed for its extension."""
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [(path, _)] = stt.calls
        assert Path(path).parent == audio_cache and path.endswith(".ogg")
        assert turn.temp_paths == [path]
        voice.cleanup_voice_turn(turn)
        assert not Path(path).exists()

    @pytest.mark.asyncio
    async def test_clip_is_owner_only(self, tmp_path, stt, config, audio_cache):
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_clip_is_private_from_creation_not_chmodded_after(
        self, tmp_path, stt, config, audio_cache, monkeypatch
    ):
        """Writing 0644 and chmodding after leaves a window where another local user can open the
        clip, and a cancellation delivered inside it leaves a 0644 file behind for good. With
        ``chmod`` neutered the file still has to come out owner-only, which only holds if the
        cache helper created it that way."""
        chmods = []
        monkeypatch.setattr(os, "chmod", lambda *a, **k: chmods.append(a))
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_unknown_audio_mime_reaches_stt_instead_of_a_bin_file(
        self, tmp_path, stt, config, audio_cache
    ):
        """An ``audio/*`` container the sniffer does not know (AMR, 3GPP, a vendor type) used to
        land as ``audio_<hex>.bin``, which the transcriber refuses by extension before uploading:
        the turn could only produce a failure note, and the failure note keeps the clip."""
        from tools.transcription_common import SUPPORTED_FORMATS

        unsniffable = b"#!AMR\n" + b"\x3c" * 64
        turn = await voice.prepare_voice_turn(
            [_blob_block(data=unsniffable, mime="audio/amr")], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert Path(path).suffix in SUPPORTED_FORMATS
        assert stt.calls == [(path, "acp")]
        assert turn.keep_paths == set()

    @pytest.mark.asyncio
    async def test_inbound_cap_refusal_says_too_large_not_unreadable(
        self, tmp_path, stt, config, audio_cache, monkeypatch
    ):
        """The configured inbound media cap can sit below our own 25 MiB, in which case the cache
        helper raises and the user is owed the "too large" note, not a generic read failure."""
        from gateway.platforms import base

        def _refuse(size, *, media_type="media", max_bytes=None):
            raise ValueError(f"Inbound {media_type} payload is too large ({size} bytes > 8 bytes)")

        monkeypatch.setattr(base, "validate_inbound_media_size", _refuse)
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and turn.temp_paths == []
        assert "too large to transcribe" in turn.audio_notes[0]
        assert not audio_cache.exists()  # refused before the cache is even touched

    @pytest.mark.asyncio
    async def test_stt_disabled_note_names_the_agent_visible_path(
        self, tmp_path, stt, config, audio_cache, monkeypatch
    ):
        """The STT-disabled note exists so the agent can open the clip, so it goes through the
        same mapping as the failure note: on a docker or ssh backend the host cache path is not
        the path the agent sees."""
        from tools import credential_files

        monkeypatch.setattr(credential_files, "to_agent_visible_cache_path",
                            lambda p, **_k: "/root/.hermes/cache/audio/" + os.path.basename(p))
        stt.enabled = False
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert turn.audio_notes[0] == (
            f"[The user sent a voice message: /root/.hermes/cache/audio/{os.path.basename(path)}]")
        assert turn.keep_paths == {path}

    @pytest.mark.asyncio
    async def test_extension_comes_from_the_bytes_not_the_declared_mime(self, tmp_path, stt, config, audio_cache):
        """The gateway cache sniffs the container, so a mislabelled voice note still lands as
        something STT and players can read."""
        turn = await voice.prepare_voice_turn(
            [_blob_block(mime="audio/wav")], cwd=str(tmp_path), agent=_agent_with_tts())
        assert turn.temp_paths[0].endswith(".ogg")

    @pytest.mark.asyncio
    async def test_failed_transcript_keeps_the_file_the_note_names(self, tmp_path, stt, config, audio_cache):
        stt.result = {"success": False, "error": "boom"}
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert turn.keep_paths == {path}
        voice.cleanup_voice_turn(turn)
        assert Path(path).exists()
        # ...but a turn that never delivered the note takes it with it.
        voice.cleanup_voice_turn(turn, force=True)
        assert not Path(path).exists()

    @pytest.mark.asyncio
    async def test_empty_transcript_names_no_file_so_the_clip_goes(self, tmp_path, stt, config, audio_cache):
        stt.result = {"success": True, "transcript": "  "}
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        [path] = turn.temp_paths
        assert turn.keep_paths == set()
        voice.cleanup_voice_turn(turn)
        assert not Path(path).exists()

    @pytest.mark.asyncio
    async def test_cancellation_during_stt_leaves_nothing_behind(
        self, tmp_path, stt, config, audio_cache, monkeypatch
    ):
        """Host disconnect mid-transcription: the VoiceTurn holding the path is discarded, so
        prepare_voice_turn has to clean up before the CancelledError propagates."""
        from tools import transcription_tools

        written: list[str] = []

        def _cancel(path, model=None, source=None):
            written.append(path)
            raise asyncio.CancelledError()

        monkeypatch.setattr(transcription_tools, "transcribe_audio", _cancel)
        with pytest.raises(asyncio.CancelledError):
            await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        assert written and not Path(written[0]).exists()
        assert list(audio_cache.iterdir()) == []

    @pytest.mark.asyncio
    async def test_cleanup_is_idempotent(self, tmp_path, stt, config, audio_cache):
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        voice.cleanup_voice_turn(turn)
        voice.cleanup_voice_turn(turn)
        assert turn.temp_paths == []

    @pytest.mark.asyncio
    async def test_oversized_embedded_blob_never_hits_disk(self, tmp_path, stt, config, audio_cache, monkeypatch):
        monkeypatch.setattr(voice, "_MAX_EMBEDDED_AUDIO_BYTES", 16)
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and turn.temp_paths == []
        assert "too large" in turn.audio_notes[0]
        assert not audio_cache.exists()

    @pytest.mark.asyncio
    async def test_write_failure_is_a_note_not_a_broken_turn(self, tmp_path, stt, config, monkeypatch):
        from gateway.platforms import base

        async def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(base, "cache_audio_from_bytes_async", _boom)
        turn = await voice.prepare_voice_turn([_blob_block()], cwd=str(tmp_path), agent=_agent_with_tts())
        assert stt.calls == [] and turn.temp_paths == []
        assert "could not be read" in turn.audio_notes[0]


class TestAudioCacheSweep:
    """``cleanup_audio_cache`` used to have one caller in the tree, the gateway's hourly
    housekeeping. An install that only ever runs ``hermes-acp`` (an editor host, or Buzz Desktop)
    never starts that loop, so every clip a failure note keeps would live for the life of the
    profile, and an ACP host with no STT provider fails every voice note."""

    def _clip(self, cache, name, age_hours):
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / name
        path.write_bytes(OGG_BYTES)
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def _server(self):
        return HermesACPAgent(session_manager=SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent")))

    @pytest.mark.asyncio
    async def test_new_session_prunes_stale_clips(self, tmp_path, audio_cache):
        stale = self._clip(audio_cache, "audio_stale.ogg", voice.AUDIO_CACHE_MAX_AGE_HOURS + 1)
        await self._server().new_session(cwd=str(tmp_path))
        assert not stale.exists()

    @pytest.mark.asyncio
    async def test_the_sweep_cannot_take_a_clip_this_session_just_wrote(self, tmp_path, audio_cache):
        """Age-based, so a live turn's clip is hours away from the cutoff even if another session
        starts mid-turn."""
        fresh = self._clip(audio_cache, "audio_fresh.ogg", 0)
        await self._server().new_session(cwd=str(tmp_path))
        assert fresh.exists()

    @pytest.mark.asyncio
    async def test_resumed_and_loaded_sessions_sweep_too(self, tmp_path, audio_cache):
        """A host that reconnects to an existing session never calls new_session."""
        server = self._server()
        resp = await server.new_session(cwd=str(tmp_path))
        for call in (server.load_session, server.resume_session):
            stale = self._clip(audio_cache, "audio_stale.ogg", voice.AUDIO_CACHE_MAX_AGE_HOURS + 1)
            await call(cwd=str(tmp_path), session_id=resp.session_id)
            assert not stale.exists()

    @pytest.mark.asyncio
    async def test_sweep_failure_never_breaks_session_start(self, tmp_path, audio_cache, monkeypatch):
        from gateway.platforms import base

        def _boom(**_kwargs):
            raise OSError("cache directory vanished")

        monkeypatch.setattr(base, "cleanup_audio_cache", _boom)
        resp = await self._server().new_session(cwd=str(tmp_path))
        assert resp.session_id


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

    def test_spaced_cwd_falls_back_to_the_audio_cache_with_a_warning(self, tmp_path, audio_cache, caplog):
        """A MEDIA: path is read up to the first space, so `~/Documents/My Project/voice` could
        never be published; bind a directory that can be, and say why."""
        spaced = tmp_path / "my project"
        with caplog.at_level("WARNING", logger="acp_adapter.server"):
            bound = voice.voice_output_dir(str(spaced), {})
        assert bound == str(audio_cache)
        assert " " not in bound
        assert any("whitespace" in r.getMessage() for r in caplog.records)

    def test_spaced_cache_fallback_gives_up_rather_than_binding_it(self, tmp_path, monkeypatch):
        from gateway.platforms import base

        monkeypatch.setattr(base, "AUDIO_CACHE_DIR", tmp_path / "cache dir")
        assert voice.voice_output_dir(str(tmp_path / "my project"), {}) is None

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

    def test_partial_bind_leaves_nothing_attached(self, tmp_path, monkeypatch):
        """The caller's ExitStack never sees a bind that raised, so bind_voice_turn has to undo
        its own half: otherwise the VOICE TURN text stays on the agent for later text turns."""
        class _Agent:
            ephemeral_system_prompt = "keep me"

            def __setattr__(self, name, value):
                raise RuntimeError("agent rejected the ephemeral prompt")

        agent = _Agent()
        turn = voice.VoiceTurn(voice_reply=True, output_dir=str(tmp_path / "voice"))
        before = tts_tool._default_output_dir()
        with pytest.raises(RuntimeError):
            voice.bind_voice_turn(agent, turn)
        assert agent.ephemeral_system_prompt == "keep me"
        assert tts_tool._default_output_dir() == before

    def test_bind_binds_the_output_dir_before_the_instruction(self, tmp_path):
        """The undo handler covers either order, so nothing else pins the ordering the comment
        argues for: the ContextVar is the half that can fail, and the instruction names the
        directory, so the directory has to be bound by the time the instruction is attached."""
        seen = {}

        class _Agent:
            valid_tool_names = {"text_to_speech"}
            ephemeral_system_prompt = None

            def __setattr__(self, name, value):
                seen.setdefault(name, tts_tool._default_output_dir())
                object.__setattr__(self, name, value)

        agent, out = _Agent(), str(tmp_path / "voice")
        restore = voice.bind_voice_turn(agent, voice.VoiceTurn(voice_reply=True, output_dir=out))
        try:
            assert seen["ephemeral_system_prompt"] == out
        finally:
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
        assert seen["user_message"] == '"what did I say"\n\nwhat did I say'
        assert seen["persist"] == seen["user_message"]
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
    async def test_embedded_clip_is_gone_when_the_turn_ends(self, tmp_path, stt, config, audio_cache):
        seen = await self._drive([_blob_block()], tmp_path)
        assert seen["user_message"] == '"what did I say"'
        assert list(audio_cache.iterdir()) == []

    @pytest.mark.asyncio
    async def test_cancelled_prompt_leaves_no_clip_behind(self, tmp_path, stt, config, audio_cache, monkeypatch):
        """Host disconnect while STT is running: the prompt task is cancelled and the VoiceTurn
        holding the path is discarded, so the clip has to be removed on the way out."""
        from tools import transcription_tools

        started, release = threading.Event(), threading.Event()

        def _hang(path, model=None, source=None):
            started.set()
            release.wait(5)
            return {"success": True, "transcript": "never delivered"}

        monkeypatch.setattr(transcription_tools, "transcribe_audio", _hang)
        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        server._conn = None

        task = asyncio.create_task(server.prompt(prompt=[_blob_block()], session_id=resp.session_id))
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert len(list(audio_cache.iterdir())) == 1  # written, STT in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(audio_cache.iterdir()) == []
        release.set()

    @pytest.mark.asyncio
    async def test_kept_clip_survives_a_cancel_while_the_agent_is_still_reading_it(
        self, tmp_path, stt, config, audio_cache
    ):
        """``handed_on`` is set when the executor accepts the work, not after the await:
        cancelling the await does not stop the worker thread, and the note it is holding names a
        clip it may still be about to open. Nothing else pins that placement."""
        stt.result = {"success": False, "error": "boom"}  # the note names the clip, so it is kept
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        read_back = []

        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        state = manager.get_session(resp.session_id)
        server._conn = None

        def _run(*_args, **_kwargs):
            started.set()
            release.wait(5)
            try:
                read_back.append(Path(stt.calls[0][0]).read_bytes())
            except OSError as exc:
                read_back.append(exc)
            done.set()
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run
        state.agent.ephemeral_system_prompt = None
        state.agent.valid_tool_names = {"text_to_speech"}

        task = asyncio.create_task(server.prompt(prompt=[_blob_block()], session_id=resp.session_id))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        while not done.is_set():
            await asyncio.sleep(0.01)
        assert read_back == [OGG_BYTES]
        assert Path(stt.calls[0][0]).exists()  # left for the cache sweep, not deleted underfoot

    @pytest.mark.asyncio
    async def test_kept_clip_goes_when_the_executor_never_accepts_the_turn(
        self, tmp_path, stt, config, audio_cache, monkeypatch
    ):
        """The other side of that placement: a submit that never happens (executor shut down
        between the claim and the run) hands nothing on, so a clip kept for a note no one will
        ever receive goes with the turn instead of waiting out the sweep."""
        from acp_adapter import server as server_module

        class _ShutDown:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("cannot schedule new futures after shutdown")

        monkeypatch.setattr(server_module, "_executor", _ShutDown())
        stt.result = {"success": False, "error": "boom"}
        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        server._conn = None

        await server.prompt(prompt=[_blob_block()], session_id=resp.session_id)
        assert stt.calls and not Path(stt.calls[0][0]).exists()
        assert list(audio_cache.iterdir()) == []

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


class TestQueuedVoiceNote:
    """A voice note that arrives while the session is busy is transcribed and queued as text; the
    replay must still answer voice-first, or the user gets a text reply to a voice message."""

    @pytest.mark.asyncio
    async def test_queue_entry_carries_the_voice_flag(self, tmp_path, stt, config):
        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        state = manager.get_session(resp.session_id)
        state.agent.valid_tool_names = {"text_to_speech"}
        state.agent.ephemeral_system_prompt = None
        state.is_running = True  # a turn is already in flight
        conn = MagicMock(spec=acp.Client)
        conn.session_update = AsyncMock()
        server._conn = conn

        await server.prompt(prompt=[_link(_ogg(tmp_path))], session_id=resp.session_id)
        assert state.queued_prompts == [QueuedPrompt('"what did I say"', voice=True)]

    def test_slash_queue_stays_a_text_turn(self):
        from acp_adapter.commands import _queue_prompt

        state = SimpleNamespace(runtime_lock=threading.Lock(), queued_prompts=[])
        _queue_prompt(state, "type this out")
        assert state.queued_prompts == [QueuedPrompt("type this out", voice=False)]

    @pytest.mark.asyncio
    async def test_replay_rebinds_the_voice_first_instruction(self, tmp_path, stt, config):
        """The replayed prompt is a plain TextContentBlock, so the flag is the only carrier."""
        manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
        server = HermesACPAgent(session_manager=manager)
        resp = await server.new_session(cwd=str(tmp_path))
        state = manager.get_session(resp.session_id)
        seen = {}

        def _run(*_args, **kwargs):
            seen["user_message"] = kwargs.get("user_message")
            seen["ephemeral"] = state.agent.ephemeral_system_prompt
            seen["tts_dir"] = tts_tool._default_output_dir()
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run
        state.agent.model, state.agent.provider = "test-model", "openrouter"
        state.agent.ephemeral_system_prompt = None
        state.agent.valid_tool_names = {"text_to_speech", "terminal"}
        conn = MagicMock(spec=acp.Client)
        conn.session_update = AsyncMock()
        server._conn = conn

        await server.prompt(prompt=[TextContentBlock(type="text", text='"second note"')],
                            session_id=resp.session_id, queued_voice=True)
        assert seen["user_message"] == '"second note"'
        assert "VOICE TURN" in seen["ephemeral"]
        assert seen["tts_dir"] == str(tmp_path / "voice")
        assert state.agent.ephemeral_system_prompt is None

    def test_replay_turn_is_none_when_the_session_would_not_speak(self, tmp_path, config):
        assert voice.replay_voice_turn(cwd=str(tmp_path), agent=_agent_with_tts(has_tool=False)) is None
        config["voice"]["auto_tts"] = False
        assert voice.replay_voice_turn(cwd=str(tmp_path), agent=_agent_with_tts()) is None


def test_tts_tool_result_reports_media_path():
    """The ACP instruction relies on the tool result carrying a ``MEDIA:<path>`` line."""
    assert tts_tool._media_tag(["/w/voice/tts_1.mp3"], False) == "MEDIA:/w/voice/tts_1.mp3"
    payload = json.loads(json.dumps({"media_tag": tts_tool._media_tag(["/w/voice/a.ogg"], True)}))
    assert payload["media_tag"].splitlines()[-1] == "MEDIA:/w/voice/a.ogg"
