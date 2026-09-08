"""ACP voice turns: inbound audio attachments become transcripts, and a turn that started with
voice asks for a voice-first reply through the ``text_to_speech`` tool.

Hosts that carry attachments (Buzz's ``buzz-acp``) hand a voice note to ``session/prompt`` as a
``resource_link`` (file under the host's per-agent scratch dir), an embedded ``resource`` blob,
or an ``audio`` block. This module mirrors the gateway's voice pipeline without duplicating it:

* STT goes through ``gateway.run_inbound.transcribe_clip`` (configured provider, local fallback,
  the same neutral failure marker and empty-transcript sentinel).
* A clip that arrives as bytes rather than a path is materialized with the gateway's
  ``cache_audio_from_bytes_async``, so inbound audio has one landing place across every surface,
  and is removed on every exit path of the turn (cancellation included).
* The voice-first rule mirrors ``BasePlatformAdapter._wants_auto_tts``: fire only when the turn
  carried audio and auto-TTS is on (``acp.auto_tts`` overriding ``voice.auto_tts``). Where the
  gateway synthesizes the final reply itself, an ACP host has no ``send_voice``; the reply audio
  is a file the host publishes when the reply names it (``MEDIA:<path>``), so the agent is asked
  to call ``text_to_speech`` and echo the ``MEDIA:`` line. The tool's default output directory is
  bound per turn (``tools.tts_tool.set_tts_output_dir``) to ``<session cwd>/<acp.voice_dir>`` so
  the file lands somewhere the host can read.
"""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from acp_adapter.content import AudioAttachment, PromptBlock, audio_attachments, join_audio_notes
from acp_adapter.session import acp_settings, acp_voice_reply_enabled, load_acp_config

logger = logging.getLogger("acp_adapter.server")

DEFAULT_VOICE_DIR = "voice"
TTS_TOOL_NAME = "text_to_speech"
# Matches the STT upload cap (``tools.transcription_common.MAX_FILE_SIZE``); embedded audio above
# it is never written to disk.
_MAX_EMBEDDED_AUDIO_BYTES = 25 * 1024 * 1024
# Owner-only: a voice note is the user's speech, and the audio cache directory itself is not
# private on every platform.
_CLIP_MODE = 0o600


@dataclass
class VoiceTurn:
    """Per-turn voice state computed before the agent runs."""

    audio_notes: Dict[int, str] = field(default_factory=dict)  # prompt block index -> note
    attachments: Dict[int, AudioAttachment] = field(default_factory=dict)
    transcripts: List[str] = field(default_factory=list)
    voice_reply: bool = False
    output_dir: Optional[str] = None
    temp_paths: List[str] = field(default_factory=list)
    # Clips a note points the agent at (STT failed, or STT is off): deleting them would break the
    # note. Explicit, because the note renders the path through ``to_agent_visible_cache_path``
    # and no longer contains the host path verbatim.
    keep_paths: set[str] = field(default_factory=set)

    @property
    def notes_text(self) -> str:
        return "\n\n".join(self.audio_notes[i] for i in sorted(self.audio_notes))

    def prompt_text(self, user_text: str) -> str:
        """Transcript notes ahead of the typed text (gateway ``_prepend_media_prefix`` order)."""
        return join_audio_notes([self.audio_notes[i] for i in sorted(self.audio_notes)], user_text)


def agent_has_tts_tool(agent: Any) -> bool:
    names = getattr(agent, "valid_tool_names", None)
    try:
        return bool(names) and TTS_TOOL_NAME in names
    except TypeError:
        return False


def _has_whitespace(path: str) -> bool:
    return any(ch.isspace() for ch in path)


def _cache_output_dir() -> Optional[str]:
    """The profile audio cache as a fallback TTS output directory, or ``None`` when it too has
    whitespace in it (nothing publishable is possible then)."""
    try:
        from gateway.platforms.base import get_audio_cache_dir

        path = str(get_audio_cache_dir())
    except Exception:
        logger.debug("ACP: audio cache directory unavailable", exc_info=True)
        return None
    return None if _has_whitespace(path) else path


def voice_output_dir(cwd: str, config: Optional[dict] = None) -> Optional[str]:
    """Directory ``text_to_speech`` writes into for this session, or ``None`` to keep the tool
    default. ``acp.voice_dir`` (default ``voice``) is resolved against the session cwd; an empty
    value disables the binding.

    A path with whitespace is never used: the host parses ``MEDIA:`` lines up to the first space
    (``buzz-acp`` ``media_publish.rs``, and the gateway's ``_TOOL_MEDIA_RE`` matches ``\\S+``), so a
    file there could never be published. ``~/Documents/My Project`` is an ordinary macOS cwd, so
    that case falls back to the profile audio cache rather than silently unbinding."""
    raw = acp_settings(config).get("voice_dir", DEFAULT_VOICE_DIR)
    sub = str(raw or "").strip()
    if not sub:
        return None
    path = os.path.abspath(os.path.join(os.path.expanduser(str(cwd or ".")), os.path.expanduser(sub)))
    if not _has_whitespace(path):
        return path
    fallback = _cache_output_dir()
    logger.warning(
        "ACP voice output dir %s contains whitespace and the host publishes MEDIA: paths only up "
        "to the first space; text_to_speech will write to %s instead",
        path, fallback or "its own default directory",
    )
    return fallback


def voice_reply_instruction(output_dir: Optional[str]) -> str:
    """System-level instruction for one voice turn (mirrors the gateway's voice-first rule)."""
    where = f" The file is written under {output_dir}." if output_dir else ""
    return (
        "VOICE TURN: the user sent this message as a voice note. Reply voice-first, as you would "
        "in a voice chat:\n"
        "1. Work out your answer as usual.\n"
        f"2. Call {TTS_TOOL_NAME} once with a spoken version of your answer: plain conversational "
        "sentences with no markdown, headings, code, URLs, or file paths, and keep it brief "
        "(what you would say aloud in well under a minute; summarise long material rather than "
        f"reading it out). Leave output_path unset.{where}\n"
        "3. Then give your normal text answer and end it with the MEDIA: line from the tool "
        "result (MEDIA: followed by the absolute file path), copied verbatim on its own line, "
        "so the host attaches the voice note.\n"
        f"If {TTS_TOOL_NAME} fails, answer in text only; do not retry it more than once."
    )


def _restrict(path: str) -> str:
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, _CLIP_MODE)
    return path


async def _materialize(att: AudioAttachment) -> Optional[str]:
    """Write embedded audio bytes into the profile audio cache and return the path; ``None`` when
    there is nothing to write or the payload exceeds the STT cap.

    The gateway's cache is the one landing place for inbound audio (issue #29): it sniffs the real
    container for the extension, applies the inbound media cap, writes off the event loop thread,
    and puts the file under HERMES_HOME, where ``to_agent_visible_cache_path`` can map it for a
    docker/ssh terminal backend and ``cleanup_audio_cache`` sweeps it instead of it living
    forever. The file is restricted to its owner with no await in between, so a cancellation
    cannot leave it world-readable; a cancellation during the write itself is the one case the
    caller cannot clean up, and the cache sweep is what collects that."""
    if att.path is not None or not att.data:
        return None
    if len(att.data) > _MAX_EMBEDDED_AUDIO_BYTES:
        return None
    from gateway.platforms.base import cache_audio_from_bytes_async

    return _restrict(await cache_audio_from_bytes_async(att.data, att.suffix))


def _unreadable_note(att: AudioAttachment) -> str:
    if att.data and len(att.data) > _MAX_EMBEDDED_AUDIO_BYTES:
        return (f"[The user sent an audio attachment ({att.display}, {att.mime}, {len(att.data)} bytes) "
                "that is too large to transcribe]")
    if att.path is not None:
        return f"[The user sent an audio attachment ({att.display}) but the file is not available: {att.path}]"
    return f"[The user sent an audio attachment ({att.display}, {att.mime}) that could not be read]"


async def _note_for(att: AudioAttachment, path: str, stt_enabled: bool) -> tuple[Optional[str], str, bool]:
    """``(transcript_or_None, note, note_points_at_the_file)``. The third value decides whether a
    materialized clip survives the turn: a note the agent can act on must not name a deleted
    file, but the empty-transcript sentinel names nothing."""
    from gateway.run_inbound import (
        _EMPTY_TRANSCRIPT_NOTE, transcribe_clip, untranscribed_audio_note, voice_message_attached_note,
    )

    if not stt_enabled:
        return None, voice_message_attached_note(path), True
    try:
        from tools import transcription_tools as stt

        transcript, note = await transcribe_clip(
            path, stt.transcribe_audio, stt.transcribe_audio_local_fallback, source="acp")
    except Exception as exc:
        logger.error("ACP voice transcription error for %s: %s", att.display, exc)
        return None, untranscribed_audio_note(path), True
    return transcript, note, transcript is None and note != _EMPTY_TRANSCRIPT_NOTE


async def prepare_voice_turn(
    prompt: list[PromptBlock], *, cwd: str, agent: Any, config: Optional[dict] = None,
) -> Optional[VoiceTurn]:
    """Transcribe every audio attachment in ``prompt`` and decide whether this turn wants a spoken
    reply. ``None`` when the prompt carries no audio (text/image/file prompts are untouched)."""
    attachments = audio_attachments(prompt)
    if not attachments:
        return None

    config = load_acp_config(config)
    try:
        from tools.transcription_tools import is_stt_enabled
        stt_enabled = bool(is_stt_enabled())
    except Exception:
        stt_enabled = False

    turn = VoiceTurn(attachments={att.index: att for att in attachments})
    try:
        for att in attachments:
            path: Optional[str] = None
            if att.path is not None:
                # A link the host could not actually drop (or that points at a directory) is
                # reported, never handed to STT: the provider would only echo an upload error.
                if os.path.isfile(att.path):
                    path = str(att.path)
            else:
                try:
                    path = await _materialize(att)
                except Exception:
                    logger.warning("ACP: could not write embedded audio attachment", exc_info=True)
                if path:
                    turn.temp_paths.append(path)
            if not path:
                turn.audio_notes[att.index] = _unreadable_note(att)
                continue
            transcript, note, keeps_file = await _note_for(att, path, stt_enabled)
            if transcript is not None:
                turn.transcripts.append(transcript)
            if keeps_file and path in turn.temp_paths:
                turn.keep_paths.add(path)
            turn.audio_notes[att.index] = note
    except BaseException:
        # Cancellation counts: the host disconnecting mid-STT raises CancelledError through
        # ``await``, and the turn (with it, every path we would clean up) is discarded here.
        cleanup_voice_turn(turn, force=True)
        raise

    turn.voice_reply = acp_voice_reply_enabled(config) and agent_has_tts_tool(agent)
    if turn.voice_reply:
        turn.output_dir = voice_output_dir(cwd, config)
    return turn


def replay_voice_turn(*, cwd: str, agent: Any, config: Optional[dict] = None) -> Optional[VoiceTurn]:
    """Voice state for a voice note that was queued while the session was busy and is replayed as
    plain text. The clip was transcribed once, at queue time, so only the voice-first reply is
    rebound; ``None`` when this session would not answer with voice anyway."""
    config = load_acp_config(config)
    if not (acp_voice_reply_enabled(config) and agent_has_tts_tool(agent)):
        return None
    return VoiceTurn(voice_reply=True, output_dir=voice_output_dir(cwd, config))


def bind_voice_turn(agent: Any, turn: VoiceTurn) -> Callable[[], None]:
    """Attach the voice-first instruction (API-time only, via ``ephemeral_system_prompt``) and the
    TTS output directory to the running turn; returns the undo callback."""
    undo: List[Callable[[], None]] = []

    def _restore() -> None:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                logger.debug("ACP voice turn restore failed", exc_info=True)

    if turn.voice_reply:
        try:
            # ContextVar first: it is the binding that can fail, and the caller's ExitStack never
            # sees a raising bind, so a half-applied turn would leave the VOICE TURN text on the
            # agent for every later text turn of the session.
            if turn.output_dir:
                from tools.tts_tool import reset_tts_output_dir, set_tts_output_dir

                token = set_tts_output_dir(turn.output_dir)
                undo.append(lambda: reset_tts_output_dir(token))
            previous = getattr(agent, "ephemeral_system_prompt", None)
            instruction = voice_reply_instruction(turn.output_dir)
            agent.ephemeral_system_prompt = f"{previous}\n\n{instruction}" if previous else instruction
            undo.append(lambda: setattr(agent, "ephemeral_system_prompt", previous))
        except BaseException:
            _restore()
            raise

    return _restore


def cleanup_voice_turn(turn: Optional[VoiceTurn], *, force: bool = False) -> None:
    """Remove the clips this turn materialized. Clips a delivered note points the agent at are
    kept (the audio cache sweep collects them later); ``force`` removes those too, for a turn that
    was cancelled or failed before any note could reach the agent. Idempotent."""
    if turn is None:
        return
    paths, turn.temp_paths = turn.temp_paths, []
    for path in paths:
        if not force and path in turn.keep_paths:
            turn.temp_paths.append(path)
            continue
        with contextlib.suppress(OSError):
            os.remove(path)
