"""Client contracts over the production Unix transport and canonical runner."""
import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.gateway.test_acp_attach_canonical import runner, ModelDouble


@asynccontextmanager
async def connection(bridge):
    from acp_adapter.local_transport import ACPListener
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = ACPListener(Path(directory), bridge.dispatch)
        await listener.start()
        reader, writer = await asyncio.open_unix_connection(listener.path, limit=256 * 1024)
        async def rpc(ident, method, **params):
            writer.write(json.dumps(dict(jsonrpc="2.0", id=ident, method=method, params=params)).encode() + b"\n")
            await writer.drain()
            events = []
            while True:
                line = await asyncio.wait_for(reader.readline(), 20)
                assert line, "EOF before response"
                frame = json.loads(line)
                if frame.get("id") == ident:
                    return frame, events
                events.append(frame)
        try:
            yield rpc, listener
        finally:
            writer.close()
            await writer.wait_closed()
            await listener.close()


@pytest.mark.asyncio
async def test_full_replay_pages_survive_socket_and_do_not_subscribe_early(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    ids = []
    for i in range(70):
        ids.append(await bridge.deliver(sid, {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": str(i)}}}}))
    async with connection(bridge) as (rpc, listener):
        response, frames = await rpc(1, "session/load", sessionId=sid, _meta={"afterDeliveryId": 0, "history": False})
        assert "error" not in response, response
        assert response["result"]["_meta"]["hasMoreDeliveries"]
        assert sid not in bridge.subscribers
        cursor = response["result"]["_meta"]["lastDeliveryId"]
        response, more = await rpc(2, "session/load", sessionId=sid, _meta={"afterDeliveryId": cursor, "history": False})
        assert not response["result"]["_meta"]["hasMoreDeliveries"]
        assert [f["params"]["_meta"]["deliveryId"] for f in frames + more] == ids
        assert sid in bridge.subscribers


@pytest.mark.asyncio
async def test_oversized_notice_is_rejected_without_poisoning_replay(runner):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.config import Platform
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = await bridge.entry(sid)
    adapter = runner.adapters[Platform.LOCAL]
    rejected = await adapter.send(entry.origin.chat_id, "界" * (256 * 1024))
    assert not rejected.success
    assert bridge.journal.read(sid) == ([], False)
    sent = await adapter.send(entry.origin.chat_id, "kept")
    assert sent.success
    async with connection(bridge) as (rpc, _):
        response, frames = await rpc(1, "session/load", sessionId=sid, _meta={"history": False})
        assert "result" in response
        assert [f["params"]["update"]["content"]["text"] for f in frames] == ["kept"]


@pytest.mark.asyncio
async def test_cancelled_disconnect_closes_listener_without_cancelling_owner(runner):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.config import Platform
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    owner = asyncio.create_task(asyncio.Event().wait())
    bridge.completion_tasks.add(owner)
    async with connection(bridge) as (_, listener):
        runner._acp_listener = listener
        task = asyncio.create_task(runner.adapters[Platform.LOCAL].disconnect())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            assert not listener.server.is_serving()
            assert listener._lock_fd is None
            assert not listener.path.exists()
            assert not owner.cancelled()
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_retention_gap_and_subscriber_cleanup_are_explicit(runner, monkeypatch):
    import sqlite3
    from gateway import acp_delivery
    from gateway.acp_bridge import GatewayACPBridge
    monkeypatch.setattr(acp_delivery, "MAX_ROWS", 3, raising=False)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    for i in range(5):
        await bridge.deliver(sid, {"jsonrpc": "2.0", "method": "session/update", "params": {"number": i}})
    with sqlite3.connect(bridge.journal.path) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 3
    async with connection(bridge) as (rpc, listener):
        response, frames = await rpc(1, "session/load", sessionId=sid, _meta={"history": False})
        assert "replay_gap" in response["error"]["message"]
        assert frames == []
        response, frames = await rpc(2, "session/load", sessionId=sid, _meta={"history": False, "afterDeliveryId": 2})
        assert "result" in response
        assert len(frames) == 3
        created, _ = await rpc(3, "session/new")
        fresh = created["result"]
        result, frames = await rpc(4, "session/load", sessionId=fresh["sessionId"],
            _meta={"history": False, "afterDeliveryId": fresh["_meta"]["lastDeliveryId"]})
        assert "result" in result and frames == []
    async def detached():
        while bridge.subscribers:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(detached(), 2)


@pytest.mark.asyncio
async def test_negotiated_history_identity_and_history_free_cursor(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    async with connection(bridge) as (rpc, _):
        initialized, _ = await rpc(1, "initialize", protocolVersion=1,
            clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        cap = initialized["result"]["agentCapabilities"]["_meta"]["hermesAttachment"]
        assert cap["version"] == 1 and cap["features"]["historyFreeReplay"]
        created, _ = await rpc(2, "session/new")
        sid = created["result"]["sessionId"]
        turns = []
        for ident in (3, 4):
            result, events = await rpc(ident, "session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hi"}])
            assert result["result"]["stopReason"] == "end_turn"
            turns.append(result["result"]["_meta"]["turnId"])
            chunks = [e for e in events if e["method"] == "session/update" and e["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
            assert chunks and all(e["params"]["_meta"]["turnId"] == turns[-1] for e in chunks)
            tools = [e for e in events if e["method"] == "session/update" and e["params"]["update"].get("toolCallId")]
            assert all(e["params"]["_meta"]["messageId"] == turns[-1] + ":tool:" + e["params"]["update"]["toolCallId"] for e in tools)
            assert all(e["params"]["_meta"]["operation"] == "merge" for e in tools)
        assert turns[0] != turns[1]
        first, events = await rpc(5, "session/load", sessionId=sid)
        history = [e for e in events if e["params"].get("_meta", {}).get("kind") == "history"]
        assert len(history) == 4
        assert len({e["params"]["_meta"]["messageId"] for e in history}) == 4
        assert all(e["params"]["_meta"]["operation"] == "replace" for e in history)
        second, replay = await rpc(6, "session/load", sessionId=sid,
            _meta={"history": False, "afterDeliveryId": first["result"]["_meta"]["lastDeliveryId"]})
        assert replay == []
        assert second["result"]["_meta"]["replayComplete"]


@pytest.mark.asyncio
async def test_admission_retry_active_reconnect_and_restart_fail_closed(runner, monkeypatch):
    import threading
    from gateway.acp_bridge import GatewayACPBridge
    started, release = threading.Event(), threading.Event()
    calls = []
    def run(self, message, **kwargs):
        calls.append(message)
        self.stream_delta_callback("Working")
        self.tool_start_callback("work", "terminal", {})
        started.set()
        assert release.wait(10)
        return {"completed": True, "messages": [], "final_response": "Done"}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    params = dict(sessionId=sid, admissionId="buzz-trigger-1", prompt=[{"type": "text", "text": "work"}])
    try:
        async with connection(bridge) as (rpc, _):
            refused, _ = await rpc(1, "_hermes/turn/admit", **params)
            assert "error" in refused
            await rpc(2, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
            accepted, _ = await rpc(3, "_hermes/turn/admit", **params)
            assert "result" in accepted, accepted
            receipt = accepted["result"]
            assert receipt["status"] == "in_progress" and receipt["admissionId"] == params["admissionId"]
            assert await asyncio.to_thread(started.wait, 5)
        async with connection(bridge) as (rpc, _):
            await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
            retry, _ = await rpc(2, "_hermes/turn/admit", **params)
            assert retry["result"]["turnId"] == receipt["turnId"]
            loaded, events = await rpc(3, "session/load", sessionId=sid, _meta={"history": False})
            assert loaded["result"]["_meta"]["activeTurn"]["turnId"] == receipt["turnId"]
            assert "Working" in str(events) and "work" in str(events)
            assert all(e["params"]["_meta"]["operation"] == "replace" for e in events)
            conflict, _ = await rpc(4, "_hermes/turn/admit", **{**params, "prompt": [{"type": "text", "text": "different"}]})
            assert "admission_conflict" in conflict["error"]["message"]
            release.set()
            await asyncio.wait_for(asyncio.gather(*bridge.completion_tasks), 10)
            status, _ = await rpc(5, "_hermes/turn/status", sessionId=sid, admissionId=params["admissionId"])
            assert status["result"]["status"] == "completed"
            assert "finalResponse" not in status["result"]
        replacement = GatewayACPBridge(runner)
        async with connection(replacement) as (rpc, _):
            await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
            retained, _ = await rpc(2, "_hermes/turn/admit", **params)
            assert retained["result"]["status"] == "completed"
            assert retained["result"]["turnId"] == receipt["turnId"]
        assert calls == ["work"]
    finally:
        release.set()
        await asyncio.gather(*bridge.completion_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_large_final_is_durable_bounded_and_receipt_has_no_text(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    text = "界\\\"" * 70000
    monkeypatch.setattr(ModelDouble, "run_conversation", lambda *a, **kw: {
        "completed": True, "messages": [], "final_response": text})
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    async with connection(bridge) as (rpc, _):
        await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        created, _ = await rpc(2, "session/new")
        sid = created["result"]["sessionId"]
        response, events = await rpc(3, "session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hi"}])
        assert response["result"]["stopReason"] == "end_turn"
        assert "finalResponse" not in response["result"]["_meta"]
        receipts = [e for e in events if e["method"] == "_hermes/turn_complete"]
        assert receipts and "finalResponse" not in receipts[-1]["params"]
        cursor, finals = 0, []
        while True:
            loaded, page = await rpc(4, "session/load", sessionId=sid, _meta={"history": False, "afterDeliveryId": cursor})
            finals += [e for e in page if e["params"].get("_meta", {}).get("kind") == "final"]
            assert "result" in loaded, loaded
            cursor = loaded["result"]["_meta"]["lastDeliveryId"]
            if not loaded["result"]["_meta"]["hasMoreDeliveries"]:
                break
        assert "".join(e["params"]["update"]["content"]["text"] for e in finals) == text
        assert all(e["params"]["_meta"]["messageId"] == response["result"]["_meta"]["turnId"] + ":assistant" for e in finals)


@pytest.mark.asyncio
async def test_final_replay_page_reserves_snapshot_and_response_capacity(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.acp_observer import AttachedTurn
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = await bridge.entry(sid)
    turn = AttachedTurn(sid, entry.session_key, lambda _: None)
    turn.delivered_text = "x" * 262144
    turn.tool_state = {str(i): {"sessionUpdate": "tool_call", "toolCallId": str(i), "status": "in_progress"} for i in range(16)}
    bridge.turns[entry.session_key] = turn
    for i in range(64):
        await bridge.deliver(sid, {"jsonrpc": "2.0", "method": "session/update", "params": {"number": i}})
    async with connection(bridge) as (rpc, listener):
        await rpc(1, "initialize")
        server_writer = next(iter(listener.clients))
        original = server_writer.drain
        async def slow_drain():
            await asyncio.sleep(0.005)
            await original()
        monkeypatch.setattr(server_writer, "drain", slow_drain)
        result, frames = await rpc(2, "session/load", sessionId=sid, _meta={"history": False})
        assert "result" in result, result
        assert result["result"]["_meta"]["replayComplete"]
        assert len([f for f in frames if f["params"].get("_meta", {}).get("kind") == "snapshot"]) == 48


@pytest.mark.asyncio
async def test_live_subscription_capacity_is_bounded_per_connection(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    async with connection(bridge) as (rpc, _):
        for i in range(17):
            sid = (await bridge.new({}, lambda _: None))["sessionId"]
            result, _ = await rpc(i, "session/load", sessionId=sid, _meta={"history": False})
            if i < 16:
                assert "result" in result
            else:
                assert "subscription_capacity" in result["error"]["message"]
        assert len(bridge.subscribers) == 16


@pytest.mark.asyncio
async def test_ambiguous_owner_restart_never_reexecutes_admission(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    prompt = [{"type": "text", "text": "ambiguous"}]
    claimed = bridge.admissions.claim(sid, "trigger", prompt)
    replacement = GatewayACPBridge(runner)
    async with connection(replacement) as (rpc, _):
        await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        result, _ = await rpc(2, "_hermes/turn/admit", sessionId=sid, admissionId="trigger", prompt=prompt)
        assert result["result"]["status"] == "unknown"
        assert result["result"]["turnId"] == claimed["turnId"]
        assert "owner_restarted" in result["result"]["error"]
        assert ModelDouble.instances == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancelled", "error"])
async def test_negotiated_terminal_status_and_cancel_identity(runner, monkeypatch, outcome):
    import threading
    from gateway.acp_bridge import GatewayACPBridge
    entered, release = threading.Event(), threading.Event()
    def run(self, *a, **kw):
        entered.set()
        assert release.wait(10)
        if outcome == "error":
            return {"failed": True, "error": "provider broke", "messages": []}
        return {"interrupted": True, "messages": []}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    monkeypatch.setattr(ModelDouble, "interrupt", lambda *a, **kw: release.set())
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    try:
        async with connection(bridge) as (rpc, _):
            await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
            created, _ = await rpc(2, "session/new")
            sid = created["result"]["sessionId"]
            accepted, _ = await rpc(3, "_hermes/turn/admit", sessionId=sid, admissionId="trigger", prompt=[{"type": "text", "text": "work"}])
            turn_id = accepted["result"]["turnId"]
            assert await asyncio.to_thread(entered.wait, 5)
            if outcome == "cancelled":
                wrong, _ = await rpc(4, "session/cancel", sessionId=sid, turnId="different")
                assert "error" in wrong
                assert not release.is_set()
                right, _ = await rpc(5, "session/cancel", sessionId=sid, turnId=turn_id)
                assert "result" in right
            else:
                release.set()
            await asyncio.wait_for(asyncio.gather(*bridge.completion_tasks, return_exceptions=True), 10)
            status, _ = await rpc(6, "_hermes/turn/status", sessionId=sid, admissionId="trigger")
            assert status["result"]["status"] == outcome
            loaded, events = await rpc(7, "session/load", sessionId=sid, _meta={"history": False})
            receipts = [e for e in events if e["method"] == "_hermes/turn_complete"]
            assert len(receipts) == 1
            assert receipts[0]["params"]["turnId"] == turn_id
            if outcome == "error":
                assert "error" in receipts[0]["params"]
                assert "stopReason" not in receipts[0]["params"]
            else:
                assert receipts[0]["params"]["stopReason"] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(*bridge.completion_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_replay_pauses_existing_live_turn_and_large_history_is_chunked(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.acp_observer import AttachedTurn
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = await bridge.entry(sid)
    async with connection(bridge) as (rpc, _):
        await rpc(1, "session/load", sessionId=sid, _meta={"history": False})
        turn = AttachedTurn(sid, entry.session_key, bridge.subscribers[sid])
        bridge.turns[entry.session_key] = turn
        # Seed directly: no live notification before the replay request.
        for i in range(70):
            bridge.journal.append(sid, {"jsonrpc": "2.0", "method": "session/update", "params": {"number": i}})
        original = bridge.journal.read
        loop = asyncio.get_running_loop()
        def read(*args, **kwargs):
            loop.call_soon_threadsafe(turn.notice, "late delta")
            return original(*args, **kwargs)
        monkeypatch.setattr(bridge.journal, "read", read)
        result, events = await rpc(2, "session/load", sessionId=sid, _meta={"history": False})
        assert result["result"]["_meta"]["hasMoreDeliveries"]
        assert not any(e["params"].get("_meta", {}).get("kind") == "live" for e in events)
        monkeypatch.setattr(bridge.journal, "read", original)
        result, events = await rpc(3, "session/load", sessionId=sid, _meta={"history": False,
            "afterDeliveryId": result["result"]["_meta"]["lastDeliveryId"]})
        assert "late delta" in str(events)
        bridge.turns.clear()
    text = "界" * 100000
    def run(self, message, **kwargs):
        return {"completed": True, "agent_persisted": False, "final_response": text, "messages": [
            {"role": "user", "content": message}, {"role": "assistant", "content": text}]}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, lambda _: None)
    async with connection(bridge) as (rpc, _):
        result, events = await rpc(1, "session/load", sessionId=sid, _meta={"afterDeliveryId": 70})
        assert "result" in result
        history = [e for e in events if e["params"].get("_meta", {}).get("kind") == "history"
            and e["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
        assert "".join(e["params"]["update"]["content"]["text"] for e in history) == text


@pytest.mark.asyncio
async def test_admission_storage_failure_cannot_publish_success_receipt(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    original = bridge.admissions.update
    def update(receipt, **changes):
        if changes.get("status") == "completed":
            raise OSError("admission disk failed")
        return original(receipt, **changes)
    monkeypatch.setattr(bridge.admissions, "update", update)
    async with connection(bridge) as (rpc, _):
        await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        created, _ = await rpc(2, "session/new")
        sid = created["result"]["sessionId"]
        await rpc(3, "_hermes/turn/admit", sessionId=sid, admissionId="trigger", prompt=[{"type": "text", "text": "work"}])
        await asyncio.wait_for(asyncio.gather(*bridge.completion_tasks, return_exceptions=True), 10)
        _, events = await rpc(4, "session/load", sessionId=sid, _meta={"history": False})
        receipts = [e["params"] for e in events if e["method"] == "_hermes/turn_complete"]
        assert receipts and all("error" in receipt for receipt in receipts)


@pytest.mark.asyncio
async def test_canonical_background_wake_is_observable_after_owner_reopen(runner):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.config import Platform
    from gateway.wake import deliver_wake
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = await bridge.entry(sid)
    adapter = runner.adapters[Platform.LOCAL]
    await deliver_wake(adapter, text="Background result", source=entry.origin, session_id=sid)
    await asyncio.wait_for(adapter._session_tasks[entry.session_key], 10)
    async def complete():
        while entry.session_key in bridge.turns:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(complete(), 10)
    assert (await adapter.send(entry.origin.chat_id, "durable notice")).success
    replacement = GatewayACPBridge(runner)
    async with connection(replacement) as (rpc, _):
        await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        loaded, events = await rpc(2, "session/load", sessionId=sid, _meta={"history": False})
        assert "hello world" in str(events) and "durable notice" in str(events)
        assert any(e["method"] == "_hermes/turn_complete" for e in events)
        assert loaded["result"]["_meta"]["replayComplete"]
        assert len(ModelDouble.instances) == 1
        assert [m["content"] for m in ModelDouble.instances[0]._session_messages if m["role"] == "user"] == ["Background result"]


@pytest.mark.asyncio
async def test_many_sequential_tools_do_not_fail_a_longrunning_turn(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    def run(self, *args, **kwargs):
        for i in range(24):
            self.tool_start_callback(str(i), "terminal", {})
            self.tool_complete_callback(str(i), "terminal", {}, {"exit_code": 0})
        return {"completed": True, "final_response": "Done", "messages": []}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    async with connection(bridge) as (rpc, _):
        created, _ = await rpc(1, "session/new")
        result, _ = await rpc(2, "session/prompt", sessionId=created["result"]["sessionId"], prompt=[{"type": "text", "text": "work"}])
        assert result.get("result", {}).get("stopReason") == "end_turn", result


@pytest.mark.asyncio
async def test_admission_cannot_masquerade_as_clarification_control(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    asked = asyncio.Event()
    answers = []
    loop = asyncio.get_running_loop()
    def run(self, *args, **kwargs):
        loop.call_soon_threadsafe(asked.set)
        answers.append(self.clarify_callback("Where?", ["staging", "prod"]))
        return {"completed": True, "final_response": "Done", "messages": []}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    async with connection(bridge) as (rpc, _):
        await rpc(1, "initialize", clientCapabilities={"_meta": {"hermesAttachment": {"version": 1}}})
        created, _ = await rpc(2, "session/new")
        sid = created["result"]["sessionId"]
        await rpc(3, "_hermes/turn/admit", sessionId=sid, admissionId="work", prompt=[{"type": "text", "text": "work"}])
        from tools.clarify_gateway import get_pending_for_session, clear_session
        entry = await bridge.entry(sid)
        async def pending():
            while get_pending_for_session(entry.session_key, include_choice_prompts=True) is None:
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(pending(), 5)
            refused, _ = await rpc(4, "_hermes/turn/admit", sessionId=sid, admissionId="new-work", prompt=[{"type": "text", "text": "2"}])
            assert "error" in refused, refused
            assert answers == []
            answered, _ = await rpc(5, "session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "2"}])
            assert "result" in answered
            await asyncio.wait_for(asyncio.gather(*bridge.completion_tasks), 10)
            assert answers == ["prod"]
        finally:
            clear_session(entry.session_key)
            await asyncio.gather(*bridge.completion_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_merged_tool_snapshot_cannot_overflow_wire_frame(runner):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.acp_observer import AttachedTurn
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = await bridge.entry(sid)
    turn = AttachedTurn(sid, entry.session_key, lambda _: None)
    bridge.turns[entry.session_key] = turn
    turn.tool_start("call", "terminal", {"input": "x" * (90 * 1024)})
    turn.progress("tool.progress", call_id="call", preview="x" * (90 * 1024))
    turn.tool_complete("call", "terminal", {}, "x" * (90 * 1024))
    await asyncio.sleep(0)
    async with connection(bridge) as (rpc, _):
        result, _ = await rpc(1, "session/load", sessionId=sid, _meta={"history": False})
        assert "snapshot_gap" in result["error"]["message"]
        assert sid not in bridge.subscribers
        turn.notice("must not leak after failed load")
        await asyncio.sleep(0)
        _, events = await rpc(2, "initialize")
        assert events == []

