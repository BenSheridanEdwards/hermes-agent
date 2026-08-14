"""Webhook sessions must not poison Telegram with failed auto-TTS voice.

Scar (Fleet Jarvis PR-gate, 2026-08-07): webhook has no native audio transport.
Base send_voice fallback becomes a Telegram text error
("Couldn't deliver the audio attachment") when deliver:telegram is set.
Voice must route to the deliver target adapter/chat instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_webhook_adapter import _make_adapter


@pytest.mark.asyncio
async def test_webhook_send_voice_routes_to_telegram_deliver_target():
    webhook = _make_adapter()
    telegram = MagicMock()
    telegram.send_voice = AsyncMock(return_value=SendResult(success=True))

    runner = MagicMock()
    runner.adapters = {Platform.TELEGRAM: telegram}
    runner._profile_adapters = {}
    runner.config = MagicMock()
    runner.config.get_home_channel = MagicMock(return_value=None)
    setattr(webhook, "gateway_runner", runner)

    session_chat = "whsec_abc"
    webhook._delivery_info[session_chat] = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "6576995088", "message_thread_id": 42},
    }

    result = await webhook.send_voice(
        chat_id=session_chat,
        audio_path="/tmp/voice.ogg",
        caption=None,
        metadata={"notify": True},
    )

    assert result.success is True
    telegram.send_voice.assert_awaited_once()
    kwargs = telegram.send_voice.await_args.kwargs
    assert kwargs["chat_id"] == "6576995088"
    assert kwargs["audio_path"] == "/tmp/voice.ogg"
    assert kwargs["reply_to"] is None
    assert kwargs["metadata"]["message_thread_id"] == 42
    assert kwargs["metadata"]["notify"] is True


@pytest.mark.asyncio
async def test_webhook_send_voice_log_deliver_is_quiet_success():
    webhook = _make_adapter()
    setattr(webhook, "gateway_runner", MagicMock())
    session_chat = "whsec_log"
    webhook._delivery_info[session_chat] = {"deliver": "log", "deliver_extra": {}}

    result = await webhook.send_voice(chat_id=session_chat, audio_path="/tmp/voice.ogg")

    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
async def test_webhook_send_voice_uses_home_channel_when_extra_chat_missing():
    webhook = _make_adapter()
    telegram = MagicMock()
    telegram.send_voice = AsyncMock(return_value=SendResult(success=True))

    home = MagicMock()
    home.chat_id = "999888777"

    runner = MagicMock()
    runner.adapters = {Platform.TELEGRAM: telegram}
    runner._profile_adapters = {}
    runner.config = MagicMock()
    runner.config.get_home_channel = MagicMock(return_value=home)
    setattr(webhook, "gateway_runner", runner)

    session_chat = "whsec_home"
    webhook._delivery_info[session_chat] = {
        "deliver": "telegram",
        "deliver_extra": {},
    }

    result = await webhook.send_voice(chat_id=session_chat, audio_path="/tmp/v.ogg")
    assert result.success is True
    assert telegram.send_voice.await_args.kwargs["chat_id"] == "999888777"
    runner.config.get_home_channel.assert_called()


def test_resolve_auto_tts_delivery_rewrites_webhook_to_telegram():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.config = MagicMock()
    runner.config.get_home_channel = MagicMock(return_value=None)

    webhook = MagicMock()
    telegram = MagicMock()
    runner.adapters[Platform.TELEGRAM] = telegram

    session_chat = "whsec_evt"
    webhook._delivery_info = {
        session_chat: {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "6576995088"},
        }
    }

    event = MessageEvent(
        text="trigger",
        source=SessionSource(
            platform=Platform.WEBHOOK,
            chat_id=session_chat,
            user_id="hook",
            user_name="hook",
        ),
        message_type=MessageType.TEXT,
    )

    with (
        patch.object(runner, "_adapter_for_source", return_value=webhook),
        patch.object(runner, "_reply_anchor_for_event", return_value="99"),
        patch.object(runner, "_thread_metadata_for_source", return_value={"k": 1}),
    ):
        adapter, chat_id, reply_to, thread_meta = runner._resolve_auto_tts_delivery(event)

    assert adapter is telegram
    assert chat_id == "6576995088"
    assert reply_to is None
    assert thread_meta is None


def test_resolve_auto_tts_delivery_leaves_telegram_events_alone():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    telegram = MagicMock()
    event = MessageEvent(
        text="hi",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            user_id="u",
            user_name="U",
        ),
        message_type=MessageType.TEXT,
    )
    with (
        patch.object(runner, "_adapter_for_source", return_value=telegram),
        patch.object(runner, "_reply_anchor_for_event", return_value="1"),
        patch.object(runner, "_thread_metadata_for_source", return_value={"t": True}),
    ):
        adapter, chat_id, reply_to, thread_meta = runner._resolve_auto_tts_delivery(event)

    assert adapter is telegram
    assert chat_id == "123"
    assert reply_to == "1"
    assert thread_meta == {"t": True}


@pytest.mark.asyncio
async def test_send_voice_reply_webhook_event_calls_telegram_not_webhook():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.config = MagicMock()
    runner.config.get_home_channel = MagicMock(return_value=None)
    runner._voice_mode = {}

    webhook = MagicMock()
    webhook.send_voice = AsyncMock()
    telegram = MagicMock()
    telegram.send_voice = AsyncMock(return_value=SendResult(success=True))
    runner.adapters[Platform.TELEGRAM] = telegram

    session_chat = "whsec_tts"
    webhook._delivery_info = {
        session_chat: {
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "6576995088"},
        }
    }

    event = MessageEvent(
        text="trigger",
        source=SessionSource(
            platform=Platform.WEBHOOK,
            chat_id=session_chat,
            user_id="hook",
            user_name="hook",
        ),
        message_type=MessageType.TEXT,
    )

    def fake_tts(*, text, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake-ogg")
        return (
            '{"success": true, "file_path": %s, "provider": "xai", '
            '"voice_compatible": true}' % (json_dumps_path(output_path))
        )

    with (
        patch.object(runner, "_adapter_for_source", return_value=webhook),
        patch.object(runner, "_reply_anchor_for_event", return_value=None),
        patch.object(runner, "_thread_metadata_for_source", return_value=None),
        patch.object(runner, "_get_guild_id", return_value=None),
        patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts),
        patch("tools.tts_tool._strip_markdown_for_tts", side_effect=lambda t: t),
    ):
        await runner._send_voice_reply(event, "hello chief")

    webhook.send_voice.assert_not_awaited()
    telegram.send_voice.assert_awaited_once()
    assert telegram.send_voice.await_args.kwargs["chat_id"] == "6576995088"


def json_dumps_path(path: str) -> str:
    import json

    return json.dumps(path)
