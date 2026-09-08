"""ACP voice turns: inbound audio attachments become transcripts, and a turn that started with
voice asks for a voice-first reply through the ``text_to_speech`` tool.

Hosts that carry attachments (Buzz's ``buzz-acp``) hand a voice note to ``session/prompt`` as a
``resource_link`` (file under the host's per-agent scratch dir), an embedded ``resource`` blob,
or an ``audio`` block. This module mirrors the gateway's voice pipeline without duplicating it:

* STT goes through ``gateway.run_inbound.transcribe_clip`` (configured provider, local fallback,
  the same neutral failure marker and empty-transcript sentinel).
* The voice-first rule mirrors ``BasePlatformAdapter._wants_auto_tts``: fire only when the turn
  carried audio and auto-TTS is on (``acp.auto_tts`` overriding ``voice.auto_tts``). Where the
  gateway synthesizes the final reply itself, an ACP host has no ``send_voice``; the reply audio
  is a file the host publishes when the reply names it (``MEDIA:<path>``), so the agent is asked
  to call ``text_to_speech`` and echo the ``MEDIA:`` line. The tool's default output directory is
  bound per turn (``tools.tts_tool.set_tts_output_dir``) to ``<session cwd>/<acp.voice_dir>`` so
  the file lands somewhere the host can read.
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from acp_adapter.content import AudioAttachment, PromptBlock, audio_attachments
from acp_adapter.session import acp_settings, acp_voice_reply_enabled

logger = logging.getLogger("acp_adapter.server")

DEFAULT_VOICE_DIR = "voice"
TTS_TOOL_NAME = "text_to_speech"
# Matches the STT upload cap (``tools.transcription_common.MAX_FILE_SIZE``); embedded audio above
# it is never written to disk.
_MAX_EMBEDDED_AUDIO_BYTES = 25 * 1024 * 1024


@dataclass
class VoiceTurn:
    """Per-turn voice state computed before the agent runs."""

    audio_notes: Dict[int, str] = field(default_factory=dict)  # prompt block index -> note
    transcripts: List[str] = field(default_factory=list)
    voice_reply: bool = False
    output_dir: Optional[str] = None
    temp_paths: List[str] = field(default_factory=list)

    @property
    def notes_text(self) -> str:
        return "\n\n".join(self.audio_notes[i] for i in sorted(self.audio_notes))

    def prompt_text(self, user_text: str) -> str:
        """Transcript notes ahead of the typed text (gateway ``_prepend_media_prefix`` order)."""
        notes = self.notes_text
        if notes and user_text:
            return f"{notes}\n\n{user_text}"
        return notes or user_text


def agent_has_tts_tool(agent: Any) -> bool:
    names = getattr(agent, "valid_tool_names", None)
    try:
        return bool(names) and TTS_TOOL_NAME in names
    except TypeError:
        return False


def voice_output_dir(cwd: str, config: Optional[dict] = None) -> Optional[str]:
    """Directory ``text_to_speech`` writes into for this session, or ``None`` to keep the tool
    default. ``acp.voice_dir`` (default ``voice``) is resolved against the session cwd; an empty
    value disables the binding. A path with whitespace is not used: the host parses ``MEDIA:``
    lines up to the first space, so such a file could never be published."""
    raw = acp_settings(config).get("voice_dir", DEFAULT_VOICE_DIR)
    sub = str(raw or "").strip()
    if not sub:
        return None
    path = os.path.abspath(os.path.join(os.path.expanduser(str(cwd or ".")), os.path.expanduser(sub)))
    if any(ch.isspace() for ch in path):
        logger.info("ACP voice output dir %s contains whitespace; keeping the text_to_speech default", path)
        return None
    return path


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


def _materialize(att: AudioAttachment) -> Optional[str]:
    """Write embedded audio bytes to a temp file (``<tmp>/hermes_voice/acp_in_*``); ``None`` when
    there is nothing to write or the payload exceeds the STT cap."""
    if att.path is not None or not att.data:
        return None
    if len(att.data) > _MAX_EMBEDDED_AUDIO_BYTES:
        return None
    directory = os.path.join(tempfile.gettempdir(), "hermes_voice")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"acp_in_{uuid.uuid4().hex[:12]}{att.suffix}")
    with open(path, "wb") as fh:
        fh.write(att.data)
    return path


def _unreadable_note(att: AudioAttachment) -> str:
    if att.data and len(att.data) > _MAX_EMBEDDED_AUDIO_BYTES:
        return (f"[The user sent an audio attachment ({att.display}, {att.mime}, {len(att.data)} bytes) "
                "that is too large to transcribe]")
    if att.path is not None:
        return f"[The user sent an audio attachment ({att.display}) but the file is not available: {att.path}]"
    return f"[The user sent an audio attachment ({att.display}, {att.mime}) that could not be read]"


async def _note_for(att: AudioAttachment, path: str, stt_enabled: bool) -> tuple[Optional[str], str]:
    from gateway.run_inbound import transcribe_clip, untranscribed_audio_note, voice_message_attached_note

    if not stt_enabled:
        return None, voice_message_attached_note(path)
    try:
        from tools import transcription_tools as stt

        return await transcribe_clip(
            path, stt.transcribe_audio, stt.transcribe_audio_local_fallback, source="acp")
    except Exception as exc:
        logger.error("ACP voice transcription error for %s: %s", att.display, exc)
        return None, untranscribed_audio_note(path)


async def prepare_voice_turn(
    prompt: list[PromptBlock], *, cwd: str, agent: Any, config: Optional[dict] = None,
) -> Optional[VoiceTurn]:
    """Transcribe every audio attachment in ``prompt`` and decide whether this turn wants a spoken
    reply. ``None`` when the prompt carries no audio (text/image/file prompts are untouched)."""
    attachments = audio_attachments(prompt)
    if not attachments:
        return None

    try:
        from tools.transcription_tools import is_stt_enabled
        stt_enabled = bool(is_stt_enabled())
    except Exception:
        stt_enabled = False

    turn = VoiceTurn()
    for att in attachments:
        path: Optional[str] = None
        if att.path is not None:
            # A link the host could not actually drop (or that points at a directory) is
            # reported, never handed to STT: the provider would only echo an upload error.
            if os.path.isfile(att.path):
                path = str(att.path)
        else:
            try:
                path = _materialize(att)
            except OSError:
                logger.warning("ACP: could not write embedded audio attachment", exc_info=True)
            if path:
                turn.temp_paths.append(path)
        if not path:
            turn.audio_notes[att.index] = _unreadable_note(att)
            continue
        transcript, note = await _note_for(att, path, stt_enabled)
        if transcript is not None:
            turn.transcripts.append(transcript)
        turn.audio_notes[att.index] = note

    turn.voice_reply = acp_voice_reply_enabled(config) and agent_has_tts_tool(agent)
    if turn.voice_reply:
        turn.output_dir = voice_output_dir(cwd, config)
    return turn


def bind_voice_turn(agent: Any, turn: VoiceTurn) -> Callable[[], None]:
    """Attach the voice-first instruction (API-time only, via ``ephemeral_system_prompt``) and the
    TTS output directory to the running turn; returns the undo callback."""
    undo: List[Callable[[], None]] = []
    if turn.voice_reply:
        previous = getattr(agent, "ephemeral_system_prompt", None)
        instruction = voice_reply_instruction(turn.output_dir)
        agent.ephemeral_system_prompt = f"{previous}\n\n{instruction}" if previous else instruction
        undo.append(lambda: setattr(agent, "ephemeral_system_prompt", previous))
        if turn.output_dir:
            from tools.tts_tool import reset_tts_output_dir, set_tts_output_dir

            token = set_tts_output_dir(turn.output_dir)
            undo.append(lambda: reset_tts_output_dir(token))

    def _restore() -> None:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                logger.debug("ACP voice turn restore failed", exc_info=True)

    return _restore


def cleanup_voice_turn(turn: Optional[VoiceTurn]) -> None:
    """Remove materialized embedded clips whose transcript succeeded (a failure note names the
    path, so that file stays for the agent to reach)."""
    if turn is None:
        return
    for path in turn.temp_paths:
        if any(path in note for note in turn.audio_notes.values()):
            continue
        try:
            os.remove(path)
        except OSError:
            pass
