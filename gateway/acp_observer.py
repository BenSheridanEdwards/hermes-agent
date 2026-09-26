"""Turn-local ACP observation; never owns an agent or changes its native delivery."""
import asyncio
from contextvars import ContextVar
import threading
import json
import uuid
from gateway.acp_frames import TEXT_PART, MAX_TEXT, MAX_TOOLS, text_frames

current_turn = ContextVar("gateway_acp_turn", default=None)


class AttachedTurn:
    def __init__(self, session_id, session_key, emit):
        self.session_id, self.session_key = session_id, session_key
        self.emit = emit
        self.turn_id = uuid.uuid4().hex
        self.delivered_text = ""
        self.tool_state = {}
        self.snapshot_truncated = False
        self.loop = asyncio.get_running_loop()
        self.done = self.loop.create_future()
        self.result = None
        self.streamed_text = ""
        self.closed = False
        self.overflow = False
        self.pending = 0
        self.lock = threading.Lock()
        self.current = lambda: True

    def update(self, update):
        with self.lock:
            if self.closed or not self.current():
                return
            if len(json.dumps(update).encode()) > 96 * 1024:
                self.overflow = True
                return
            if self.pending >= 64:
                self.overflow = True
                return
            self.pending += 1
            self.loop.call_soon_threadsafe(self._deliver, {
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": self.session_id, "update": update,
                           "_meta": {"turnId": self.turn_id,
                                     "messageId": self.turn_id + (":tool:" + str(update["toolCallId"]) if update.get("toolCallId") else ":assistant"),
                                     "kind": "live", "operation": "merge" if update.get("toolCallId") else "append"}},
            })

    def _deliver(self, message):
        with self.lock:
            self.pending -= 1
        if self.current():
            update = message["params"]["update"]
            if update["sessionUpdate"] == "agent_message_chunk":
                text = update["content"]["text"]
                if len(self.delivered_text) + len(text) > MAX_TEXT:
                    self.overflow = True
                    return
                self.delivered_text += text
            elif update.get("toolCallId"):
                ident = update["toolCallId"]
                if ident not in self.tool_state and len(self.tool_state) >= MAX_TOOLS:
                    retired = next((key for key, value in self.tool_state.items()
                                    if value.get("status") in {"completed", "failed"}), None)
                    if retired is None:
                        self.overflow = True
                        return
                    del self.tool_state[retired]
                    self.snapshot_truncated = True
                state = {**self.tool_state.get(ident, {}), **update, "sessionUpdate": "tool_call"}
                if len(json.dumps(state).encode()) > 240 * 1024:
                    self.overflow = True
                    return
                self.tool_state[ident] = state
            self.emit(message)

    def reattach(self, emit):
        """Loop-owned snapshot then live subscription, with no intervening await."""
        if self.overflow:
            raise ValueError("snapshot_gap: observer capacity exceeded; canonical transcript retained")
        self.emit = emit
        updates = list(self.tool_state.values())
        if self.delivered_text:
            for frame in text_frames(self.session_id, self.turn_id + ":assistant", self.delivered_text,
                                     kind="snapshot", turn_id=self.turn_id):
                emit(frame)
        for update in updates:
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": self.session_id, "update": update,
                "_meta": {"kind": "snapshot", "operation": "replace", "replay": True,
                          "turnId": self.turn_id, "messageId": self.turn_id + ":tool:" + str(update["toolCallId"])}}})

    def terminal_message(self, result=None, error=None):
        return {"jsonrpc": "2.0", "method": "_hermes/turn_complete", "params": {
            "sessionId": self.session_id, "turnId": self.turn_id,
            **({"error": str(error)[:1024]} if error else {
                "stopReason": "cancelled" if (result or {}).get("interrupted") else "end_turn"})}}

    def progress(self, event_type, tool_name=None, preview=None, args=None, **kwargs):
        call_id = kwargs.get("call_id") or kwargs.get("tool_call_id")
        if event_type == "tool.progress" and call_id:
            self.update({"sessionUpdate": "tool_call_update", "toolCallId": str(call_id),
                         "status": "in_progress", "content": [{"type": "content",
                         "content": {"type": "text", "text": str(preview or "")}}]})

    def text(self, text, *args, **kwargs):
        if text:
            if len(self.streamed_text) + len(text) > MAX_TEXT:
                self.overflow = True
                return
            self.streamed_text += text
            self.notice(text)

    def notice(self, text):
        if text:
            if len(text) > MAX_TEXT:
                self.overflow = True
                return
            for i in range(0, len(text), TEXT_PART):
                self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text[i:i + TEXT_PART]}})

    def tool_start(self, call_id, name, args):
        self.update({"sessionUpdate": "tool_call", "toolCallId": str(call_id),
                     "title": name, "kind": "other", "status": "in_progress", "rawInput": args})

    def tool_complete(self, call_id, name, args, result):
        parsed = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = None
        status = "completed"
        if isinstance(parsed, dict):
            code = parsed.get("exit_code", parsed.get("returncode"))
            if (parsed.get("error") or parsed.get("success") is False or code not in (None, 0)
                    or parsed.get("status") in ("failed", "error", "cancelled", "canceled")):
                status = "failed"
            elif parsed.get("status") in ("running", "pending", "queued") or (
                    "exit_code" in parsed and code is None) or (
                    name == "terminal" and parsed.get("session_id") and parsed.get("pid")):
                status = "in_progress"
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": str(call_id),
                     "status": status, "rawOutput": result})

    def bind(self, agent, ctx):
        if ctx.session_key != self.session_key or ctx.session_id != self.session_id:
            raise RuntimeError("ACP turn identity changed")
        self.current = ctx._run_still_current
        for attr, observer in (("stream_delta_callback", self.text),
                               ("tool_start_callback", self.tool_start),
                               ("tool_complete_callback", self.tool_complete),
                               ("tool_progress_callback", self.progress)):
            native = getattr(agent, attr, None)
            def composed(*args, _native=native, _observer=observer, **kwargs):
                _observer(*args, **kwargs)
                if _native:
                    return _native(*args, **kwargs)
            setattr(agent, attr, composed)


def observed_handler(handler, runner=None):
    async def dispatch(event):
        turn = getattr(event, "_acp_turn", None)
        bridge = getattr(runner, "_acp_bridge", None)
        if turn is None and bridge is not None:
            turn = await bridge.observe_background(event)
        if turn is None:
            return await handler(event)
        token = current_turn.set(turn)
        try:
            response = await handler(event)
            if turn.result is None:
                raise RuntimeError("Gateway did not execute the requested turn")
            if not turn.current() and not turn.result.get("interrupted"):
                raise RuntimeError("Canonical turn was superseded")
            final = (response if isinstance(response, str) else
                     getattr(event, "_streamed_final_response", None))
            if final is not None:
                turn.result = {**turn.result, "final_response": final}
            final = turn.result.get("final_response")
            if final:
                # ACP chunks are append-only. Extend a matching prefix; otherwise
                # retain commentary and append the authoritative final separately.
                prefix = turn.streamed_text
                remainder = final[len(prefix):] if final.startswith(prefix) else "\n\n" + final
                turn.notice(remainder)
            if not turn.done.done():
                turn.done.set_result(turn.result)
            return response
        except asyncio.CancelledError:
            if not turn.done.done():
                turn.done.set_result({"interrupted": True})
            raise
        except BaseException as exc:
            if not turn.done.done():
                turn.done.set_exception(RuntimeError(str(exc) or "Gateway turn cancelled"))
            raise
        finally:
            turn.closed = True
            current_turn.reset(token)
    return dispatch
