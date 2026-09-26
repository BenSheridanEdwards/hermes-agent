"""ACP requests dispatched through the existing gateway, guards, store and agent cache."""
import asyncio
from dataclasses import replace
import uuid
import sqlite3

from gateway.acp_observer import AttachedTurn
from gateway.acp_admission import AdmissionMethods, AdmissionStore, negotiated
from gateway.acp_delivery import delivery_frame
from gateway.acp_frames import text_frames
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


async def start_attachment(runner):
    if not runner.config.acp_enabled:
        return None
    from acp_adapter.local_transport import ACPListener
    from hermes_constants import get_hermes_home
    bridge = GatewayACPBridge(runner)
    listener = ACPListener(get_hermes_home(), bridge.dispatch)
    await listener.start()
    bridge.install_adapter()
    runner._acp_listener = listener
    return listener


class LocalAttachmentAdapter(BasePlatformAdapter):
    """Generic local route. Native-platform sessions keep their existing adapter."""
    supports_async_delivery = True

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.LOCAL)

    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        bridge = getattr(self.gateway_runner, "_acp_bridge", None)
        try:
            if bridge is not None and bridge.completion_tasks:
                await asyncio.shield(asyncio.gather(*bridge.completion_tasks, return_exceptions=True))
        finally:
            listener = getattr(self.gateway_runner, "_acp_listener", None)
            if listener is not None:
                await listener.close()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        bridge = getattr(getattr(self, "gateway_runner", None), "_acp_bridge", None)
        if bridge is None:
            return SendResult(success=False, error="Local ACP delivery owner unavailable")
        source = SessionSource(platform=self.platform, chat_id=chat_id, user_id="local-owner")
        key = bridge.runner._session_key_for_source(source)
        entry = await bridge.runner.async_session_store.lookup_by_session_key(key)
        if entry is None:
            return SendResult(success=False, error="Local ACP route unavailable")
        # The handler already reconciled an explicitly attached model turn. The
        # base adapter's final send is an acknowledgement, not another text copy.
        turn = bridge.turns.get(key)
        if turn is not None and turn.closed and self._session_tasks.get(key) is asyncio.current_task():
            return SendResult(success=True)
        message = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": entry.session_id, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": content}}}}
        try:
            ident = await bridge.deliver(entry.session_id, message)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=str(ident))

    async def get_chat_info(self, chat_id):
        return {"name": "Local ACP attachment", "type": "dm"}


class GatewayACPBridge(AdmissionMethods):
    def __init__(self, runner):
        from gateway.acp_delivery import DeliveryJournal
        self.runner = runner
        self.turns = {}
        self.subscribers = {}
        self.completion_tasks = set()
        self.delivery_lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.admissions = AdmissionStore(runner.config.sessions_dir)
        self.journal = DeliveryJournal(runner.config.sessions_dir)
        runner._acp_bridge = self

    def check_subscription(self, sid, emit):
        connection = getattr(emit, "connection", emit)
        owned = sum(getattr(e, "connection", e) is connection
                    for key, e in self.subscribers.items() if key != sid)
        if owned >= 16 or (sid not in self.subscribers and len(self.subscribers) >= 128):
            raise ValueError("subscription_capacity: at most 16 sessions per connection")
        if getattr(connection, "closed", lambda: False)():
            raise ConnectionError("ACP subscriber disconnected")

    def detach(self, connection):
        for sid, emit in tuple(self.subscribers.items()):
            if getattr(emit, "connection", emit) is connection:
                self.subscribers.pop(sid, None)
        for turn in self.turns.values():
            if getattr(turn.emit, "connection", turn.emit) is connection:
                turn.emit = lambda _: None

    async def deliver(self, session_id, message, *, negotiated_only=False):
        async with self.delivery_lock:
            ident = await asyncio.to_thread(self.journal.append, session_id, message)
            message = delivery_frame(ident, message)
            emit = self.subscribers.get(session_id)
            if emit is not None and (not negotiated_only or negotiated(emit)):
                emit(message)
            return ident

    async def observe_background(self, event):
        if not event.internal or event.source.platform != Platform.LOCAL:
            return None
        key = self.runner._session_key_for_source(event.source)
        entry = await self.runner.async_session_store.lookup_by_session_key(key)
        if entry is None or (key in self.turns and not self.turns[key].closed):
            return None
        turn = AttachedTurn(entry.session_id, key, lambda message: self.subscribers.get(
            entry.session_id, lambda _: None)(message))
        self.turns[key] = turn
        task = asyncio.current_task()
        task.add_done_callback(lambda _: self.track_completion(turn, task))
        return turn

    def track_completion(self, turn, task):
        from agent.async_utils import consume_detached_task_result
        delivery = asyncio.create_task(self.finish_turn(turn, task))
        self.completion_tasks.add(delivery)
        delivery.add_done_callback(self.completion_tasks.discard)
        delivery.add_done_callback(consume_detached_task_result)
        return delivery

    async def finish_turn(self, turn, task):
        try:
            result = await asyncio.shield(turn.done)
            if task is not None:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
            if turn.overflow:
                raise RuntimeError("ACP observer overflow: transcript remains in canonical storage")
            if result.get("failed") or result.get("error") or (
                    result.get("completed") is False and not result.get("interrupted")):
                raise RuntimeError(result.get("error") or "Canonical turn did not complete")
            await asyncio.sleep(0)
            for frame in text_frames(turn.session_id, turn.turn_id + ":assistant",
                    result.get("final_response", ""), kind="final", turn_id=turn.turn_id):
                await self.deliver(turn.session_id, frame, negotiated_only=True)
            if getattr(turn, "admission", None) is not None:
                await asyncio.to_thread(self.admissions.update, turn.admission,
                    status="cancelled" if result.get("interrupted") else "completed")
            await self.deliver(turn.session_id, turn.terminal_message(result))
            return {"stopReason": "cancelled" if result.get("interrupted") else "end_turn",
                    "_meta": {"turnId": turn.turn_id}}
        except Exception as exc:
            if getattr(turn, "admission", None) is not None:
                await asyncio.to_thread(self.admissions.update, turn.admission, status="error", error=str(exc)[:1024])
            await self.deliver(turn.session_id, turn.terminal_message(error=exc))
            raise
        finally:
            if self.turns.get(turn.session_key) is turn:
                self.turns.pop(turn.session_key, None)

    def install_adapter(self):
        if Platform.LOCAL not in self.runner.adapters:
            adapter = LocalAttachmentAdapter()
            adapter.gateway_runner = self.runner
            self.runner._wire_adapter_handlers(adapter)
            self.runner.adapters[Platform.LOCAL] = adapter

    async def dispatch(self, request, emit):
        handlers = {"initialize": self.initialize, "session/new": self.new,
                    "session/load": self.load, "session/prompt": self.prompt, "session/cancel": self.cancel,
                    "_hermes/turn/admit": self.admit, "_hermes/turn/status": self.turn_status}
        handler = handlers.get(request.get("method"))
        if handler is None:
            raise ValueError("Unsupported attached ACP method")
        return await handler(request.get("params") or {}, emit)

    async def initialize(self, params, emit):
        requested = (params.get("clientCapabilities", {}).get("_meta", {}).get("hermesAttachment", {}))
        if requested and requested.get("version") != 1:
            raise ValueError("Unsupported hermesAttachment version")
        connection = getattr(emit, "connection", emit)
        if hasattr(connection, "__dict__"):
            connection.hermes_attachment = requested.get("version") == 1
        return {"protocolVersion": 1, "agentInfo": {"name": "hermes-gateway", "version": "1"},
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {}, "mcpCapabilities": {},
                    "_meta": {"hermesAttachment": {"version": 1, "features": {
                        "historyFreeReplay": True, "deliveryReplay": True,
                        "activeTurnSnapshot": True, "terminalReceipts": True,
                        "canonicalAsyncWake": True, "retainedAdmission": True}}}},
                "authMethods": []}

    @staticmethod
    def check_options(params):
        if params.get("mcpServers"):
            raise ValueError("Client MCP servers are not supported in attachment mode")

    async def new(self, params, emit):
        self.check_options(params)
        source = SessionSource(platform=Platform.LOCAL, chat_id="acp-" + uuid.uuid4().hex,
                               user_id="local-owner", role_authorized=True)
        entry = await self.runner.async_session_store.get_or_create_session(source)
        cursor = await asyncio.to_thread(self.journal.checkpoint)
        return {"sessionId": entry.session_id, "_meta": {"lastDeliveryId": cursor}}

    async def entry(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Missing sessionId")
        entry = await self.runner.async_session_store.lookup_by_session_id(session_id)
        if entry is None or entry.origin is None:
            raise ValueError("Session is not a current gateway route")
        if self.runner._session_key_for_source(entry.origin) != entry.session_key:
            raise ValueError("Session route does not match its origin")
        if self.runner._adapter_for_source(entry.origin) is None:
            raise ValueError("Session's canonical adapter is unavailable")
        return entry

    async def load(self, params, emit):
        from gateway.acp_replay import load
        return await load(self, params, emit)

    async def cancel(self, params, emit):
        entry = await self.entry(params.get("sessionId"))
        turn = self.turns.get(entry.session_key)
        if turn is None or turn.session_id != entry.session_id or turn.closed or not turn.current():
            raise ValueError("No current attached turn to cancel")
        if params.get("turnId", turn.turn_id) != turn.turn_id:
            raise ValueError("turn_mismatch: cancellation targets a different turn")
        source = replace(entry.origin, role_authorized=True)
        adapter = self.runner._adapter_for_source(source)
        await adapter.handle_message(MessageEvent(text="/stop", source=source))
        return {}

    async def prompt(self, params, emit, *, admission=None):
        if (not self.runner._running or self.runner._draining or
                self.runner._startup_restore_in_progress):
            raise RuntimeError("Gateway is not ready to accept attached prompts")
        entry = await self.entry(params.get("sessionId"))
        prompt = params.get("prompt")
        if not isinstance(prompt, list) or not prompt or any(
                not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str)
                for p in prompt):
            raise ValueError("Attachment prompts support text blocks only")
        text = "\n".join(p["text"] for p in prompt)
        command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if command in {"/approve", "/deny"}:
            turn = self.turns.get(entry.session_key)
            if turn is None or turn.closed or not turn.current():
                raise ValueError("No current attached turn awaiting control")
            source = replace(entry.origin, role_authorized=True)
            await self.runner._adapter_for_source(source).handle_message(MessageEvent(text=text, source=source))
            return {"stopReason": "end_turn"}
        from tools.clarify_gateway import get_pending_for_session
        turn = self.turns.get(entry.session_key)
        if (turn is not None and not turn.closed and turn.current()
                and not text.lstrip().startswith("/")
                and get_pending_for_session(entry.session_key, include_choice_prompts=True) is not None):
            if admission is not None:
                raise ValueError("Canonical session is busy; use session/prompt for clarification control")
            source = replace(entry.origin, role_authorized=True)
            # Both canonical guards recognize a pending clarify; never resolve it
            # here or let the answer become a second model turn.
            await self.runner._adapter_for_source(source).handle_message(MessageEvent(text=text, source=source))
            return {"stopReason": "end_turn"}
        if text.lstrip().startswith("/"):
            raise ValueError("Session mutations are not supported through attached prompts")
        source = replace(entry.origin, role_authorized=True)
        adapter = self.runner._adapter_for_source(source)
        key = entry.session_key
        state = self.runner._peek_session_state(key)
        if key in self.turns or key in adapter._active_sessions or (state and state.turn.agent):
            raise ValueError("Canonical session is busy")
        self.check_subscription(entry.session_id, emit)
        self.subscribers[entry.session_id] = emit
        turn = AttachedTurn(entry.session_id, key, emit)
        turn.admission = admission
        if admission is not None:
            turn.turn_id = admission["turnId"]
        event = MessageEvent(text=text, source=source, internal=False, allow_gateway_control=False,
                             metadata={"gateway_session_key": key, "gateway_session_id": entry.session_id,
                                       "gateway_session_strict": True})
        event._acp_turn = turn
        self.turns[key] = turn
        try:
            await adapter.handle_message(event)
            if not event._gateway_accepted:
                raise RuntimeError("Gateway refused prompt admission")
            task = adapter._session_tasks.get(key)
            completion = self.track_completion(turn, task)
            if admission is not None:
                return await asyncio.to_thread(self.admissions.update, admission, status="in_progress")
            return await asyncio.shield(completion)
        finally:
            if not event._gateway_accepted:
                self.turns.pop(key, None)
