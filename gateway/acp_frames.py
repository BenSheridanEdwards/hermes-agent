"""Wire-sized, identity-bearing text snapshots for attachment v1."""
TEXT_PART = 8192
MAX_TEXT = 262144
MAX_TOOLS = 16


def text_frames(session_id, message_id, text, *, kind, turn_id=None):
    if len(text) > MAX_TEXT:
        raise ValueError("attachment_text_limit: text exceeds 262144 characters")
    parts = [text[i:i + TEXT_PART] for i in range(0, len(text), TEXT_PART)] or [""]
    for index, part in enumerate(parts):
        meta = {"kind": kind, "operation": "replace", "messageId": message_id,
                "part": index, "parts": len(parts), "replay": kind in {"history", "snapshot"}}
        if turn_id is not None:
            meta["turnId"] = turn_id
        yield {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "_meta": meta, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}}}}
