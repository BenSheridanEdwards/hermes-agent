"""Regression tests for duplicate STT on one queued voice event."""

import asyncio
from unittest.mock import AsyncMock, patch

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_pending_voice_reuses_stt_between_interrupt_and_queue_drain():
    """The interrupt peek and post-run drain share one pending event.

    The first stage must own the STT request and transcript echo; the second
    stage must reuse that result instead of charging the provider again.
    """
    async def exercise():
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(stt_enabled=True, stt_echo_transcripts=True)

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
        )
        event = MessageEvent(
            text="",
            message_type=MessageType.VOICE,
            source=source,
            media_urls=["/tmp/pending-voice.ogg"],
            media_types=["audio/ogg"],
        )
        echo_adapter = AsyncMock()

        with patch(
            "tools.transcription_tools.transcribe_audio",
            return_value={
                "success": True,
                "transcript": "one transcript only",
                "provider": "xai",
            },
        ) as transcribe:
            first_text, first_transcripts = await runner._prepare_voice_event_transcription(
                event=event,
                user_text=event.text,
                audio_paths=event.media_urls,
                echo_adapter=echo_adapter,
            )
            second_text, second_transcripts = await runner._prepare_voice_event_transcription(
                event=event,
                user_text=event.text,
                audio_paths=event.media_urls,
                echo_adapter=echo_adapter,
            )

        assert first_text == second_text
        assert first_transcripts == second_transcripts == ["one transcript only"]
        transcribe.assert_called_once_with("/tmp/pending-voice.ogg")
        echo_adapter.send.assert_awaited_once()

    asyncio.run(exercise())
