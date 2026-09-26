"""Replay pagination and the atomic replay-to-live transport fence."""
import asyncio

from acp_adapter.attachment_emitter import replay_emit
from acp_adapter.local_transport import encode
from gateway.acp_delivery import delivery_frame, PAGE_BYTES
from gateway.acp_frames import text_frames


async def history_frames(bridge, sid):
    rows = await bridge.runner._session_db.get_messages(sid, limit=65, latest=True)
    selected, size = [], 0
    truncated = len(rows) > 64
    for row in reversed(rows[-64:]):
        kind = {"user": "user_message_chunk", "assistant": "agent_message_chunk"}.get(row.get("role"))
        if not kind or not isinstance(row.get("content"), str) or not row["content"]:
            continue
        try:
            frames = list(text_frames(sid, f"history:{row['id']}", row["content"], kind="history"))
        except ValueError:
            truncated = True
            break
        for frame in frames:
            frame["params"]["update"]["sessionUpdate"] = kind
        length = sum(len(encode(frame)) for frame in frames)
        if size + length > PAGE_BYTES:
            truncated = True
            break
        selected.append(frames)
        size += length
    return [frame for group in reversed(selected) for frame in group], truncated


async def load(bridge, params, emit):
    bridge.check_options(params)
    entry = await bridge.entry(params.get("sessionId"))
    options = params.get("_meta") or {}
    after = options.get("afterDeliveryId", 0)
    if type(after) is not int or not 0 <= after <= 9223372036854775807:
        raise ValueError("afterDeliveryId must be a non-negative signed 64-bit integer")
    if type(options.get("history", True)) is not bool:
        raise ValueError("history must be a boolean")
    async with bridge.delivery_lock:
        bridge.check_subscription(entry.session_id, emit)
        bridge.subscribers.pop(entry.session_id, None)
        turn = bridge.turns.get(entry.session_key)
        if turn is not None:
            turn.emit = lambda _: None
        deliveries, more = await asyncio.to_thread(bridge.journal.read, entry.session_id, after)
        frames, truncated = (await history_frames(bridge, entry.session_id)
                             if options.get("history", True) else ([], False))
        for frame in frames:
            await replay_emit(emit, frame)
        for ident, message in deliveries:
            await replay_emit(emit, delivery_frame(ident, message, replay=True))
        meta = {"lastDeliveryId": deliveries[-1][0] if deliveries else after,
                "hasMoreDeliveries": more, "replayComplete": not more,
                "historyIncluded": options.get("history", True), "historyTruncated": truncated,
                "historyLimit": 64}
        # Drain replay first. The remaining bounded snapshot (<=48 frames) and
        # response are enqueued synchronously, before enabling any live source.
        if hasattr(emit, "flush"):
            await emit.flush()
        bridge.check_subscription(entry.session_id, emit)
        turn = bridge.turns.get(entry.session_key)
        if not more and turn is not None and turn.session_id == entry.session_id and turn.current():
            turn.reattach(emit)
            meta["activeTurn"] = {"turnId": turn.turn_id, "status": "in_progress",
                                  "snapshotTruncated": turn.snapshot_truncated}
        result = {"_meta": meta}
        if hasattr(emit, "reply_nowait"):
            if "id" in emit.request:
                emit.reply_nowait({"jsonrpc": "2.0", "id": emit.request["id"], "result": result})
            emit.replied = True
        if not more:
            bridge.subscribers[entry.session_id] = emit
        return result
