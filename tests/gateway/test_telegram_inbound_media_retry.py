"""Regression tests for transient Telegram inbound-media acquisition failures."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram.error import TimedOut

from plugins.platforms.telegram.adapter import TelegramAdapter


def _voice_message(voice):
    return SimpleNamespace(
        caption=None,
        sticker=None,
        photo=None,
        voice=voice,
        audio=None,
        video=None,
        document=None,
        media_group_id=None,
    )


def _adapter_for_media_handler() -> Any:
    adapter = cast(Any, object.__new__(TelegramAdapter))
    adapter._max_doc_bytes = 20 * 1024 * 1024
    adapter._should_process_message = lambda message, *, is_command=False: True
    adapter._build_message_event = lambda _message, _type, update_id=None: SimpleNamespace(
        text="",
        media_urls=[],
        media_types=[],
    )
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter.handle_message = AsyncMock()
    adapter._surface_media_cache_failure = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_voice_get_file_read_timeout_retries_and_caches_once():
    adapter = _adapter_for_media_handler()
    telegram_timeout = TimedOut("Timed out")
    telegram_timeout.__cause__ = httpx.ReadTimeout(
        "telegram getFile response headers stalled"
    )
    file_obj = SimpleNamespace(
        file_path="voice/file.ogg",
        download_as_bytearray=AsyncMock(return_value=bytearray(b"OggSvoice")),
    )
    voice = SimpleNamespace(
        file_size=128,
        get_file=AsyncMock(
            side_effect=[telegram_timeout, file_obj]
        ),
    )
    update = SimpleNamespace(message=_voice_message(voice), update_id=1)

    with (
        patch(
            "plugins.platforms.telegram.adapter.cache_audio_from_bytes",
            return_value="/tmp/retried-voice.ogg",
        ) as cache_audio,
        patch("plugins.platforms.telegram.adapter.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    assert voice.get_file.await_count == 2
    file_obj.download_as_bytearray.assert_awaited_once()
    sleep.assert_awaited_once()
    cache_audio.assert_called_once_with(b"OggSvoice", ext=".ogg")
    adapter._surface_media_cache_failure.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.media_urls == ["/tmp/retried-voice.ogg"]
    assert event.media_types == ["audio/ogg"]


@pytest.mark.asyncio
async def test_voice_download_read_timeout_reacquires_file_and_retries():
    adapter = _adapter_for_media_handler()
    first_file = SimpleNamespace(
        file_path="voice/file.ogg",
        download_as_bytearray=AsyncMock(
            side_effect=httpx.ReadTimeout("telegram file download stalled")
        ),
    )
    second_file = SimpleNamespace(
        file_path="voice/file.ogg",
        download_as_bytearray=AsyncMock(return_value=bytearray(b"OggSretry")),
    )
    voice = SimpleNamespace(
        file_size=128,
        get_file=AsyncMock(side_effect=[first_file, second_file]),
    )
    update = SimpleNamespace(message=_voice_message(voice), update_id=2)

    with (
        patch(
            "plugins.platforms.telegram.adapter.cache_audio_from_bytes",
            return_value="/tmp/retried-download.ogg",
        ),
        patch("plugins.platforms.telegram.adapter.asyncio.sleep", new_callable=AsyncMock),
    ):
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    assert voice.get_file.await_count == 2
    first_file.download_as_bytearray.assert_awaited_once()
    second_file.download_as_bytearray.assert_awaited_once()
    adapter._surface_media_cache_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_transient_failure_stops_after_three_attempts():
    adapter = _adapter_for_media_handler()
    failures = [TimedOut(f"attempt {attempt} timed out") for attempt in range(1, 4)]
    voice = SimpleNamespace(
        file_size=128,
        get_file=AsyncMock(side_effect=failures),
    )
    update = SimpleNamespace(message=_voice_message(voice), update_id=3)

    with patch(
        "plugins.platforms.telegram.adapter.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    assert voice.get_file.await_count == 3
    assert sleep.await_count == 2
    adapter._surface_media_cache_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_non_network_failure_is_not_retried():
    adapter = _adapter_for_media_handler()
    failure = ValueError("invalid Telegram file metadata")
    voice = SimpleNamespace(
        file_size=128,
        get_file=AsyncMock(side_effect=failure),
    )
    update = SimpleNamespace(message=_voice_message(voice), update_id=4)

    with patch("plugins.platforms.telegram.adapter.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    voice.get_file.assert_awaited_once()
    sleep.assert_not_awaited()
    adapter._surface_media_cache_failure.assert_awaited_once()


def _media_message(**overrides):
    values = {
        "caption": None,
        "sticker": None,
        "photo": None,
        "voice": None,
        "audio": None,
        "video": None,
        "document": None,
        "media_group_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "kind", "cache_name", "cache_path"),
    [
        ("audio", "audio file", "cache_audio_from_bytes", "/tmp/audio.mp3"),
        ("video", "video file", "cache_video_from_bytes", "/tmp/video.mp4"),
    ],
)
async def test_audio_and_video_handlers_use_shared_download_helper(
    field, kind, cache_name, cache_path
):
    adapter = _adapter_for_media_handler()
    source = SimpleNamespace(file_size=128)
    message = _media_message(**{field: source})
    update = SimpleNamespace(message=message, update_id=5)
    file_obj = SimpleNamespace(file_path=f"media/{field}.mp4")

    with (
        patch(
            "plugins.platforms.telegram.adapter._download_inbound_telegram_media",
            new_callable=AsyncMock,
            return_value=(file_obj, b"media-bytes"),
        ) as download,
        patch(
            f"plugins.platforms.telegram.adapter.{cache_name}",
            return_value=cache_path,
        ),
    ):
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    download.assert_awaited_once_with(source, kind)
    adapter.handle_message.assert_awaited_once()
    adapter._surface_media_cache_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_handler_uses_shared_download_helper():
    adapter = _adapter_for_media_handler()
    adapter._photo_batch_key = MagicMock(return_value="photo-batch")
    adapter._enqueue_photo_event = MagicMock()
    source = SimpleNamespace(file_size=128)
    message = _media_message(photo=[source])
    update = SimpleNamespace(message=message, update_id=6)
    file_obj = SimpleNamespace(file_path="photos/image.jpg")

    with (
        patch(
            "plugins.platforms.telegram.adapter._download_inbound_telegram_media",
            new_callable=AsyncMock,
            return_value=(file_obj, b"image-bytes"),
        ) as download,
        patch(
            "plugins.platforms.telegram.adapter.cache_image_from_bytes",
            return_value="/tmp/photo.jpg",
        ),
    ):
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    download.assert_awaited_once_with(source, "photo")
    adapter._enqueue_photo_event.assert_called_once()
    adapter._surface_media_cache_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_handler_uses_shared_download_helper():
    adapter = _adapter_for_media_handler()
    source = SimpleNamespace(
        file_size=128,
        file_name="notes.txt",
        mime_type="text/plain",
    )
    message = _media_message(document=source)
    update = SimpleNamespace(message=message, update_id=7)
    cached = SimpleNamespace(
        path="/tmp/notes.txt",
        media_type="text/plain",
        kind="document",
    )

    with (
        patch(
            "plugins.platforms.telegram.adapter._download_inbound_telegram_media",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(file_path="docs/notes.txt"), b"hello"),
        ) as download,
        patch("gateway.platforms.base.cache_media_bytes", return_value=cached),
    ):
        await TelegramAdapter._handle_media_message(adapter, update, SimpleNamespace())

    download.assert_awaited_once_with(source, "document")
    adapter.handle_message.assert_awaited_once()
    adapter._surface_media_cache_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_sticker_uses_shared_download_helper():
    adapter = _adapter_for_media_handler()
    sticker = SimpleNamespace(
        emoji="🙂",
        set_name="set",
        is_animated=False,
        is_video=False,
        file_unique_id="sticker-1",
    )
    message = _media_message(sticker=sticker)
    event = cast(Any, SimpleNamespace(text=""))

    with (
        patch(
            "plugins.platforms.telegram.adapter._download_inbound_telegram_media",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(file_path="stickers/one.webp"), b"webp"),
        ) as download,
        patch("plugins.platforms.telegram.adapter.cache_image_from_bytes", return_value="/tmp/sticker.webp"),
        patch("gateway.sticker_cache.get_cached_description", return_value=None),
        patch("gateway.sticker_cache.cache_sticker_description"),
        patch(
            "tools.vision_tools.vision_analyze_tool",
            new_callable=AsyncMock,
            return_value='{"success": true, "analysis": "a smiling sticker"}',
        ),
    ):
        await TelegramAdapter._handle_sticker(adapter, message, event)

    download.assert_awaited_once_with(sticker, "sticker")
    assert "smiling sticker" in event.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["_cache_observed_media", "_cache_replied_media"])
async def test_observed_and_replied_media_use_shared_download_helper(method_name):
    adapter = _adapter_for_media_handler()
    source = SimpleNamespace(file_size=128)
    adapter._observed_media_source = MagicMock(
        return_value=(source, "note.txt", "text/plain", None)
    )
    cached = SimpleNamespace(
        path="/tmp/note.txt",
        media_type="text/plain",
        kind="document",
        display_name="note.txt",
        context_note=MagicMock(return_value="saved note"),
    )
    event = SimpleNamespace(
        text="",
        media_urls=[],
        media_types=[],
        message_type=None,
    )
    message = (
        SimpleNamespace(reply_to_message=SimpleNamespace())
        if method_name == "_cache_replied_media"
        else SimpleNamespace()
    )

    with (
        patch(
            "plugins.platforms.telegram.adapter._download_inbound_telegram_media",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(file_path="docs/note.txt"), b"note"),
        ) as download,
        patch("gateway.platforms.base.cache_media_bytes", return_value=cached),
    ):
        await getattr(TelegramAdapter, method_name)(adapter, message, event)

    download.assert_awaited_once_with(source, "attachment")
    assert event.media_urls == ["/tmp/note.txt"]
