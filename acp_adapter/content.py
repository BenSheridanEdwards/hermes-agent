"""ACP prompt content blocks -> Hermes/OpenAI user-content payloads (text, images, resources)."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from acp.schema import (
    AudioContentBlock, BlobResourceContents, EmbeddedResourceContentBlock, ImageContentBlock,
    ResourceContentBlock, TextContentBlock, TextResourceContents,
)

logger = logging.getLogger("acp_adapter.server")

PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)

_MAX_ACP_RESOURCE_BYTES = 512 * 1024
_TEXT_RESOURCE_MIME_TYPES = {
    "application/json",
    "application/javascript",
    "application/typescript",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/sql",
}


def _resource_display_name(uri: str, name: str | None = None, title: str | None = None) -> str:
    """Human-readable attachment name for prompt context."""
    raw_name = (name or "").strip()
    raw_title = (title or "").strip()
    if raw_title and raw_name and raw_title != raw_name:
        return f"{raw_title} ({raw_name})"
    if raw_title or raw_name:
        return raw_title or raw_name
    parsed = urlparse(uri)
    candidate = parsed.path if parsed.scheme else uri
    return Path(unquote(candidate)).name or uri or "resource"


def _mime_main(mime_type: str | None) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def _is_text_resource(mime_type: str | None) -> bool:
    mime = _mime_main(mime_type)
    return mime.startswith("text/") or mime in _TEXT_RESOURCE_MIME_TYPES


def _is_image_resource(mime_type: str | None) -> bool:
    return _mime_main(mime_type).startswith("image/")


_IMAGE_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def _is_audio_resource(mime_type: str | None) -> bool:
    return _mime_main(mime_type).startswith("audio/")


# Voice-note and audio attachment extensions a host may link without a MIME type. ``.webm``
# is listed because voice recorders emit Opus-in-WebM; a WebM video linked as ``video/webm``
# is not matched (the MIME wins when present).
_AUDIO_SUFFIX_MIME = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}
_AUDIO_MIME_SUFFIX = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/webm": ".webm", "audio/flac": ".flac",
}


@dataclass
class AudioAttachment:
    """One audio prompt block. ``path`` is the local file for a ``resource_link``; ``data`` carries
    embedded bytes (embedded ``resource`` blob or ``audio`` block) until a caller materializes them."""

    index: int
    uri: str
    display: str
    mime: str
    path: Path | None = None
    data: bytes | None = None

    @property
    def suffix(self) -> str:
        if self.path is not None and self.path.suffix:
            return self.path.suffix.lower()
        return _AUDIO_MIME_SUFFIX.get(_mime_main(self.mime), ".bin")


# "I have bytes but no idea what they are" MIME types. Treating them as an explicit type would
# let ``application/octet-stream`` on a ``voice-note.ogg`` beat the extension and land the clip in
# the binary-omitted path, so they are read as "no MIME" and the extension decides.
_UNTYPED_MIME_TYPES = {"application/octet-stream", "binary/octet-stream", "application/binary"}

# Enough for every magic-byte check in ``tools.audio_container`` (ftyp brand ends at byte 12).
_SNIFF_BYTES = 16


def _extension_audio_mime(path: Path) -> str | None:
    """Audio MIME for a linked file that has no usable MIME type, or ``None`` when it is not audio.

    The extension only nominates: the file's own magic bytes confirm. Without that, a text file
    named ``notes.wav`` is uploaded to the STT provider unsniffed (the STT validators check
    symlink, existence, size and extension only). A file that cannot be read keeps the extension's
    verdict so the caller still reports it as a missing attachment instead of inlining it."""
    mime = _AUDIO_SUFFIX_MIME.get(path.suffix.lower())
    if mime is None:
        return None
    try:
        with path.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES)
    except OSError:
        return mime
    from tools.audio_container import sniff_container

    return mime if sniff_container(head) is not None else None


def _audio_mime_for(mime_type: str | None, path: Path | None) -> str | None:
    """Effective audio MIME for a block, or ``None`` when it is not audio. An explicit non-audio
    MIME wins over the extension (a ``text/plain`` file named ``notes.wav`` stays text)."""
    if mime_type and _mime_main(mime_type) not in _UNTYPED_MIME_TYPES:
        return mime_type if _is_audio_resource(mime_type) else None
    if path is not None:
        return _extension_audio_mime(path)
    return None


def _decode_blob(blob: str) -> bytes | None:
    """Base64 payload -> bytes, or ``None`` when it is not base64 at all.

    Line-wrapped payloads (``base64.encodebytes``, MIME encoders) and the URL-safe alphabet are
    normalised deliberately, because ``b64decode(validate=True)`` rejects both. Anything else is
    refused rather than re-encoded as its own UTF-8 bytes: for audio that fallback writes the
    base64 *text* to disk as a clip and uploads it to the STT provider."""
    compact = "".join(blob.split())
    if not compact:
        return None
    for altchars in (None, b"-_"):
        try:
            return base64.b64decode(compact, altchars=altchars, validate=True)
        except Exception:
            continue
    return None


def audio_attachment_from_block(index: int, block: Any) -> AudioAttachment | None:
    """Classify one prompt block as audio: a ``resource_link``/embedded ``resource`` whose MIME is
    ``audio/*`` (or, without a MIME, whose file extension is a known audio type *and* whose magic
    bytes agree), or an ``audio`` content block. Returns ``None`` for everything else. Reads at
    most ``_SNIFF_BYTES`` of a linked file and never inlines its contents."""
    if isinstance(block, AudioContentBlock):
        data = _attr(block, "data") or ""
        if data.startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        mime = _attr(block, "mime_type") or "audio/mpeg"
        return AudioAttachment(index=index, uri="", display="voice message", mime=mime, data=_decode_blob(data))

    if isinstance(block, ResourceContentBlock):
        uri = _attr(block, "uri")
        if not uri:
            return None
        path = _path_from_file_uri(uri)
        mime = _audio_mime_for(_attr(block, "mime_type"), path)
        if mime is None:
            return None
        display = _resource_display_name(uri, name=_attr(block, "name"), title=_attr(block, "title"))
        return AudioAttachment(index=index, uri=uri, display=display, mime=mime, path=path)

    if isinstance(block, EmbeddedResourceContentBlock):
        resource = getattr(block, "resource", None)
        if not isinstance(resource, BlobResourceContents):
            return None
        uri = _attr(resource, "uri") or ""
        mime = _audio_mime_for(_attr(resource, "mime_type"), _path_from_file_uri(uri) if uri else None)
        if mime is None:
            return None
        return AudioAttachment(
            index=index, uri=uri, display=_resource_display_name(uri) if uri else "voice message", mime=mime,
            data=_decode_blob(resource.blob or ""),
        )
    return None


def audio_attachments(prompt: list[PromptBlock]) -> list[AudioAttachment]:
    """Every audio attachment in ``prompt`` (prompt order)."""
    found = [audio_attachment_from_block(index, block) for index, block in enumerate(prompt)]
    return [att for att in found if att is not None]


def join_audio_notes(notes: list[str], text: str) -> str:
    """Transcript notes ahead of the typed text, blank-line separated (the gateway's
    ``_prepend_media_prefix`` shape). The one joiner, so what is persisted as the user message and
    what the model is shown cannot drift apart."""
    joined = "\n\n".join(notes)
    if joined and text:
        return f"{joined}\n\n{text}"
    return joined or text


def _audio_fallback_note(att: AudioAttachment) -> str:
    """Prompt text for an audio block that went through no voice preprocessing: name the file when
    there is one so the agent knows a clip was attached; never inline the bytes."""
    if att.path is not None:
        from gateway.run_inbound import voice_message_attached_note
        return voice_message_attached_note(str(att.path))
    size = len(att.data or b"")
    return f"[The user sent an audio attachment ({att.display}, {att.mime}, {size} bytes) that is not available as a file]"


def _path_from_file_uri(uri: str) -> Path | None:
    """Local file URI/path from an ACP client -> readable Path (None for non-file URIs).
    Windows drive forms (Zed via wsl.exe) become ``/mnt/<drive>/...``."""
    raw = (uri or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme != "file":
        return None

    if parsed.scheme == "file" and parsed.netloc and parsed.netloc not in {"", "localhost"}:
        return None
    path_text = unquote(parsed.path or "") if parsed.scheme == "file" else unquote(raw)

    # file:///C:/Users/... or C:\Users\...
    if len(path_text) >= 3 and path_text[0] == "/" and path_text[2] == ":" and path_text[1].isalpha():
        drive, rest = path_text[1], path_text[3:]
    elif len(path_text) >= 2 and path_text[1] == ":" and path_text[0].isalpha():
        drive, rest = path_text[0], path_text[2:]
    else:
        return Path(path_text)
    return Path("/mnt") / drive.lower() / rest.lstrip("/\\").replace("\\", "/")


def _decode_text_bytes(data: bytes, mime_type: str | None) -> str | None:
    """Decode resource bytes if they are probably text; return None for binary."""
    if b"\x00" in data and not _is_text_resource(mime_type):
        return None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Binary (ELF/Mach-O/PE), not a shell script: feeding its decoded bytes back into the guard tokenizes
    # machine code into bogus NUL-bearing paths and crashes the scanner (#77703). Mirror
    # lifecycle_guard._read_referenced_script and treat it as nothing to scan.
    return data.decode("utf-8", errors="replace")


def _format_resource_text(
    *, uri: str, body: str, name: str | None = None, title: str | None = None, note: str | None = None
) -> str:
    display = _resource_display_name(uri, name=name, title=title)
    header = f"[Attached file: {display}]"
    if note:
        header += f" ({note})"
    return f"{header}\nURI: {uri}\n\n{body}"


def _text_parts(**kwargs: Any) -> list[dict[str, Any]]:
    """Single OpenAI text part wrapping ``_format_resource_text(**kwargs)``."""
    return [{"type": "text", "text": _format_resource_text(**kwargs)}]


def _image_parts(uri: str, display: str, data: bytes, mime: str) -> list[dict[str, Any]]:
    """Text header + image_url data URL so vision models can see the attachment."""
    return [
        {"type": "text", "text": f"[Attached image: {display}]" + (f"\nURI: {uri}" if uri else "")},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}},
    ]


def _attr(obj: Any, name: str) -> str | None:
    """Stripped string attribute, ``None`` when missing/blank."""
    return str(getattr(obj, name, "") or "").strip() or None


def _resource_link_to_parts(block: ResourceContentBlock) -> list[dict[str, Any]]:
    """ACP resource_link -> OpenAI content parts: images become a text header + image_url,
    everything else a single text part with the inlined body (or a binary-omit note)."""
    uri = _attr(block, "uri")
    if not uri:
        return []

    name, title, mime_type = _attr(block, "name"), _attr(block, "title"), _attr(block, "mime_type")
    path = _path_from_file_uri(uri)
    ident = dict(uri=uri, name=name, title=title)

    if path is None:
        return _text_parts(
            **ident, body="[Resource link only; Hermes cannot read non-file ACP resource URIs directly.]"
        )

    image_mime = mime_type if _is_image_resource(mime_type) else _IMAGE_SUFFIX_MIME.get(path.suffix.lower())
    if image_mime and _is_image_resource(image_mime):
        try:
            size = path.stat().st_size
            if size > _MAX_ACP_RESOURCE_BYTES:
                return _text_parts(
                    **ident, body=f"[Image too large to inline: {size} bytes, cap={_MAX_ACP_RESOURCE_BYTES}]"
                )
            with path.open("rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.warning("ACP image resource read failed: %s", uri, exc_info=True)
            return _text_parts(**ident, body=f"[Could not read attached image: {exc}]")
        return _image_parts(uri, _resource_display_name(uri, name=name, title=title), data, image_mime)

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            data = fh.read(min(size, _MAX_ACP_RESOURCE_BYTES))
        text = _decode_text_bytes(data, mime_type)
        if text is None:
            return _text_parts(**ident, body=f"[Binary file omitted: {size} bytes, mime={mime_type or 'unknown'}]")
        note = f"truncated to {_MAX_ACP_RESOURCE_BYTES} of {size} bytes" if size > _MAX_ACP_RESOURCE_BYTES else None
        return _text_parts(**ident, body=text, note=note)
    except OSError as exc:
        logger.warning("ACP resource read failed: %s", uri, exc_info=True)
        return _text_parts(**ident, body=f"[Could not read attached file: {exc}]")


def _embedded_resource_to_parts(block: EmbeddedResourceContentBlock) -> list[dict[str, Any]]:
    resource = getattr(block, "resource", None)
    if resource is None:
        return []

    uri = _attr(resource, "uri") or ""
    mime_type = _attr(resource, "mime_type")

    if isinstance(resource, TextResourceContents):
        return _text_parts(uri=uri, body=resource.text)

    if isinstance(resource, BlobResourceContents):
        blob = resource.blob or ""
        # Not base64 at all: show the payload as the text it apparently is rather than dropping it.
        data = _decode_blob(blob)
        if data is None:
            data = blob.encode("utf-8", errors="replace")

        if _is_image_resource(mime_type):
            if len(data) > _MAX_ACP_RESOURCE_BYTES:
                return _text_parts(
                    uri=uri,
                    body=f"[Embedded image too large to inline: {len(data)} bytes, cap={_MAX_ACP_RESOURCE_BYTES}]",
                )
            return _image_parts(uri, _resource_display_name(uri), data, mime_type or "image/png")

        body = _decode_text_bytes(data[:_MAX_ACP_RESOURCE_BYTES], mime_type)
        if body is None:
            body = f"[Binary embedded file omitted: {len(data)} bytes, mime={mime_type or 'unknown'}]"
        elif len(data) > _MAX_ACP_RESOURCE_BYTES:
            body += f"\n\n[Truncated to {_MAX_ACP_RESOURCE_BYTES} of {len(data)} bytes]"
        return _text_parts(uri=uri, body=body)

    text = getattr(resource, "text", None)
    if text:
        return _text_parts(uri=uri, body=str(text))
    return []


def _extract_text(prompt: list[PromptBlock]) -> str:
    """Extract plain text from ACP content blocks for display/commands."""
    return "\n".join(str(block.text) for block in prompt if hasattr(block, "text"))


def _image_block_to_openai_part(block: ImageContentBlock) -> dict[str, Any] | None:
    """Convert an ACP image content block to OpenAI-style multimodal content."""
    data, uri = _attr(block, "data"), _attr(block, "uri")
    mime_type = _attr(block, "mime_type") or "image/png"
    if data:
        url = data if data.startswith("data:") else f"data:{mime_type};base64,{data}"
    elif uri:
        url = uri
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _append_parts(parts: list, text_parts: list[str], new_parts: list[dict[str, Any]]) -> None:
    for part in new_parts:
        parts.append(part)
        if part.get("type") == "text":
            text_parts.append(part["text"])


def _content_blocks_to_openai_user_content(
    prompt: list[PromptBlock], audio_notes: dict[int, str] | None = None,
    attachments: dict[int, AudioAttachment] | None = None,
) -> str | list[dict[str, Any]]:
    """Convert ACP prompt blocks into a Hermes/OpenAI-compatible user content payload.

    Audio blocks never reach the binary/text inlining path: each becomes the note in
    ``audio_notes`` (block index -> transcript or marker, from ``acp_adapter.voice``) or a
    fallback marker, and the notes lead the payload the way the gateway prepends transcripts.
    ``attachments`` is the classification ``acp_adapter.voice`` already did for this prompt; pass
    it so embedded blobs are base64-decoded once per turn rather than once per pass."""
    parts: list[dict[str, Any]] = []
    text_parts: list[str] = []
    audio_parts: list[str] = []

    for index, block in enumerate(prompt):
        attachment = (
            attachments.get(index) if attachments is not None else audio_attachment_from_block(index, block)
        )
        if attachment is not None:
            audio_parts.append((audio_notes or {}).get(index) or _audio_fallback_note(attachment))
        elif isinstance(block, TextContentBlock):
            if block.text:
                parts.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
        elif isinstance(block, ImageContentBlock):
            image_part = _image_block_to_openai_part(block)
            if image_part is not None:
                parts.append(image_part)
        elif isinstance(block, ResourceContentBlock):
            _append_parts(parts, text_parts, _resource_link_to_parts(block))
        elif isinstance(block, EmbeddedResourceContentBlock):
            _append_parts(parts, text_parts, _embedded_resource_to_parts(block))

    if audio_parts:
        parts = [{"type": "text", "text": note} for note in audio_parts] + parts

    if not parts:
        return _extract_text(prompt)

    # Pure text stays a string (slash commands, text-only providers); structured only for media.
    if all(part.get("type") == "text" for part in parts):
        return join_audio_notes(audio_parts, "\n".join(text_parts))

    return parts
