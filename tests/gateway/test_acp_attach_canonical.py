"""Canonical gateway execution, cache and persistence with a deterministic model double."""
import asyncio
from types import SimpleNamespace

import pytest


class ModelDouble:
    instances = []

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200000)
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.is_interrupted = False
        self._session_messages = []
        self.instances.append(self)

    def run_conversation(self, message, conversation_history=None, **kwargs):
        if self.stream_delta_callback:
            self.stream_delta_callback("hello ")
        if self.tool_start_callback:
            self.tool_start_callback("call-1", "terminal", {"command": "printf test"})
        if self.tool_complete_callback:
            self.tool_complete_callback("call-1", "terminal", {}, "test")
        if self.stream_delta_callback:
            self.stream_delta_callback("world")
        self._session_messages = [*(conversation_history or []),
                                  {"role": "user", "content": message},
                                  {"role": "assistant", "content": "hello world"}]
        return {"final_response": "hello world", "messages": self._session_messages,
                "completed": True, "agent_persisted": False}

    def interrupt(self, *args, **kwargs):
        self.is_interrupted = True


@pytest.fixture
def runner(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    import run_agent

    monkeypatch.setattr(GatewayRunner, "_init_startup_checks", lambda self: None)
    monkeypatch.setattr(run_agent, "AIAgent", ModelDouble)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
    ModelDouble.instances = []
    instance = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    instance._running = True
    monkeypatch.setattr(instance, "_resolve_session_agent_runtime", lambda **kw: ("test-model", {"provider": "openai", "api_key": "fake", "base_url": "http://invalid.invalid"}))
    yield instance
    instance._shutdown_executor()


@pytest.mark.asyncio
async def test_two_prompts_reuse_canonical_agent_and_store(runner):
    from gateway.acp_bridge import GatewayACPBridge

    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    updates = []
    async def rpc(method, **params):
        return await bridge.dispatch({"method": method, "params": params}, updates.append)
    created = await rpc("session/new", cwd="/tmp", mcpServers=[])
    sid = created["sessionId"]
    entry = runner.session_store.lookup_by_session_id(sid)
    assert entry is not None
    for _ in range(2):
        result = await asyncio.wait_for(rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hi"}]), 20)
        assert result["stopReason"] == "end_turn"
    assert len(ModelDouble.instances) == 1
    assert len([m for m in ModelDouble.instances[0]._session_messages if m["role"] == "user"]) == 2
    records = await runner._session_db.get_messages(sid)
    assert len([m for m in records if m["role"] == "user"]) == 2
    kinds = [u["params"]["update"]["sessionUpdate"] for u in updates if u["method"] == "session/update"]
    assert kinds == ["agent_message_chunk", "tool_call", "tool_call_update", "agent_message_chunk"] * 2
    assert (await rpc("session/load", sessionId=sid, cwd="/tmp", mcpServers=[]))["_meta"]["hasMoreDeliveries"] is False
    assert runner.session_store.lookup_by_session_id(sid).origin == entry.origin


@pytest.mark.asyncio
async def test_cli_attach_uses_opt_in_gateway_listener(runner, monkeypatch):
    import json
    import os
    import sys
    import tempfile
    from pathlib import Path
    from gateway.config import GatewayConfig
    from gateway.acp_bridge import start_attachment

    assert GatewayConfig.from_dict({}).acp_enabled is False
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        monkeypatch.setenv("HERMES_HOME", directory)
        assert await start_attachment(runner) is None
        runner.config = GatewayConfig.from_dict({"gateway": {"acp": {"enabled": True}}})
        server = await start_attachment(runner)
        assert server is not None
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hermes_cli.main", "acp", "--attach",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HERMES_HOME": directory})
        updates = []
        async def rpc(ident, method, params):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode() + b"\n")
            await proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 20)
                assert line, (await proc.stderr.read()).decode()
                message = json.loads(line)
                if message.get("id") == ident:
                    assert "error" not in message, message
                    return message["result"]
                updates.append(message)
        try:
            await rpc(1, "initialize", {"protocolVersion": 1})
            sid = (await rpc(2, "session/new", {"cwd": "/tmp", "mcpServers": []}))["sessionId"]
            for ident in (3, 4):
                assert (await rpc(ident, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}))["stopReason"] == "end_turn"
            assert len(ModelDouble.instances) == 1
            assert len([u for u in updates if u["method"] == "session/update"]) == 8
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
            await server.close()
        assert not (Path(directory) / "acp" / "gateway.sock").exists()


@pytest.mark.asyncio
async def test_gateway_lifecycle_owns_attachment(runner, monkeypatch):
    import tempfile
    from pathlib import Path
    from unittest.mock import AsyncMock

    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        monkeypatch.setenv("HERMES_HOME", directory)
        runner.config.acp_enabled = True
        for name in ("_start_install_faulthandler", "_start_log_startup_environment", "_start_startup_warmup", "_start_spawn_background_watchers", "_install_plugin_message_injector", "_wire_teams_pipeline_runtime", "_update_runtime_status"):
            monkeypatch.setattr(runner, name, lambda *a, **kw: None)
        monkeypatch.setattr(runner, "_start_check_access_policy", lambda: False)
        for name in ("_start_recover_previous_run", "_start_finish_wiring"):
            monkeypatch.setattr(runner, name, AsyncMock())
        assert await runner.start()
        path = Path(directory) / "acp" / "gateway.sock"
        assert path.exists()
        from gateway.config import Platform
        await runner.adapters[Platform.LOCAL].disconnect()
        assert not path.exists()


@pytest.mark.asyncio
async def test_cancel_is_scoped_to_attached_canonical_turn(runner, monkeypatch):
    import threading
    from gateway.acp_bridge import GatewayACPBridge
    started, released = threading.Event(), threading.Event()
    original = ModelDouble.run_conversation
    def run(self, message, **kwargs):
        started.set()
        assert released.wait(10)
        return {"interrupted": True, "completed": False, "messages": [], "final_response": ""}
    def interrupt(self, *args, **kwargs):
        self.is_interrupted = True
        released.set()
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    monkeypatch.setattr(ModelDouble, "interrupt", interrupt)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    params = {"sessionId": sid, "prompt": [{"type": "text", "text": "wait"}]}
    task = asyncio.create_task(bridge.prompt(params, lambda _: None))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        assert not task.done()
        await bridge.dispatch({"method": "session/cancel", "params": {"sessionId": sid}}, lambda _: None)
        assert (await asyncio.wait_for(task, 10))["stopReason"] == "cancelled"
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(ModelDouble, "run_conversation", original)
    assert (await bridge.prompt(params, lambda _: None))["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_local_approval_wait_is_visible_and_resolved_by_canonical_deny(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    from tools import approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from tools.approval_context import get_current_session_key
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 3)
    decisions = []
    def run(self, message, **kwargs):
        key = get_current_session_key()
        decisions.append(_await_gateway_decision(key, approval._gateway_notify_cbs[key],
                         {"command": "rm important-file", "description": "deletion"}))
        return {"completed": True, "messages": [], "final_response": "Denied"}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    shown = asyncio.Event()
    def emit(message):
        if "/approve" in str(message):
            shown.set()
    params = {"sessionId": sid, "prompt": [{"type": "text", "text": "do work"}]}
    task = asyncio.create_task(bridge.prompt(params, emit))
    try:
        await asyncio.wait_for(shown.wait(), 2)
        assert not task.done()
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "/deny"}]}, emit)
        assert (await asyncio.wait_for(task, 10))["stopReason"] == "end_turn"
        assert decisions[0]["choice"] == "deny"
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_clarification_reaches_client_and_answer_unblocks_canonical_wait(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    from tools import clarify_gateway as cg
    from tools.approval_context import get_current_session_key
    answers = []
    def run(self, message, **kwargs):
        answers.append(self.clarify_callback("Which environment?", ["staging", "prod"]))
        return {"completed": True, "messages": [], "final_response": "Selected " + str(answers[-1])}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    shown = asyncio.Event()
    def emit(message):
        if "Which environment?" in str(message):
            shown.set()
    task = asyncio.create_task(bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "work"}]}, emit))
    try:
        await asyncio.wait_for(shown.wait(), 2)
        assert not task.done()
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "2"}]}, emit)
        assert (await asyncio.wait_for(task, 10))["stopReason"] == "end_turn"
        assert answers == ["prod"]
        assert len(ModelDouble.instances) == 1
    finally:
        cg.clear_session(runner.session_store.lookup_by_session_id(sid).session_key)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nonstreaming_model_final_is_not_lost(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    monkeypatch.setattr(ModelDouble, "run_conversation", lambda self, *a, **kw: {
        "completed": True, "final_response": "Only final text", "messages": []})
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    updates = []
    assert (await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, updates.append))["stopReason"] == "end_turn"
    assert [u["params"]["update"]["content"]["text"] for u in updates if u["method"] == "session/update"] == ["Only final text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [{"failed": True, "error": "model broke"}, {"completed": False, "partial": True}])
async def test_model_failure_or_incomplete_is_not_success(runner, monkeypatch, outcome):
    from gateway.acp_bridge import GatewayACPBridge
    monkeypatch.setattr(ModelDouble, "run_conversation", lambda self, *a, **kw: {
        "messages": [], "final_response": "Partial answer", **outcome})
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    updates = []
    with pytest.raises(RuntimeError):
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, updates.append)
    assert updates[-1]["method"] == "_hermes/turn_complete"
    assert "error" in updates[-1]["params"]
    assert "stopReason" not in updates[-1]["params"]
    replay = []
    await bridge.load({"sessionId": sid}, replay.append)
    assert replay[-1]["params"]["error"] == updates[-1]["params"]["error"]


@pytest.mark.asyncio
async def test_stale_id_and_unsupported_mutations_never_dispatch(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = runner.session_store.lookup_by_session_id(sid)
    with pytest.raises(ValueError):
        await bridge.new({"mcpServers": [{"name": "evil", "command": "bash"}]}, lambda _: None)
    with pytest.raises(ValueError):
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "/model other"}]}, lambda _: None)
    runner.session_store.reset_session(entry.session_key)
    with pytest.raises(ValueError):
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, lambda _: None)
    assert ModelDouble.instances == []


@pytest.mark.asyncio
async def test_startup_restore_never_queues_an_attached_prompt_after_error(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    runner._startup_restore_in_progress = True
    with pytest.raises(RuntimeError):
        await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, lambda _: None)
    assert runner._startup_restore_queue == []
    assert ModelDouble.instances == []


@pytest.mark.asyncio
async def test_load_replays_canonical_store_without_rebuilding_agent(runner):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}, lambda _: None)
    updates = []
    await bridge.load({"sessionId": sid, "mcpServers": []}, updates.append)
    assert [u["params"]["update"]["sessionUpdate"] for u in updates if u["params"].get("_meta", {}).get("kind") == "history"] == ["user_message_chunk", "agent_message_chunk"]
    assert len(ModelDouble.instances) == 1
    assert updates[-1]["method"] == "_hermes/turn_complete"
    assert "finalResponse" not in updates[-1]["params"]
    assert [u["params"]["update"]["content"]["text"] for u in updates if u["params"].get("_meta", {}).get("kind") == "final"] == ["hello world"]


@pytest.mark.asyncio
async def test_loaded_native_route_keeps_adapter_agent_and_prompt_cache(runner):
    from gateway.acp_bridge import GatewayACPBridge, LocalAttachmentAdapter
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.platforms.event import MessageEvent
    from gateway.platforms.base import SendResult
    delivered = []
    class NativeAdapter(LocalAttachmentAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            delivered.append(content)
            return SendResult(success=True)
    adapter = NativeAdapter()
    adapter.platform = Platform.TELEGRAM
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._wire_adapter_handlers(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="owner", role_authorized=True)
    entry = runner.session_store.get_or_create_session(source)
    await adapter.handle_message(MessageEvent(text="native", source=source))
    await adapter._session_tasks[entry.session_key]
    cached = runner._agent_cache[entry.session_key]
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    await bridge.load({"sessionId": entry.session_id}, lambda _: None)
    await bridge.prompt({"sessionId": entry.session_id, "prompt": [{"type": "text", "text": "attached"}]}, lambda _: None)
    assert runner._agent_cache[entry.session_key][:2] == cached[:2]
    assert len(ModelDouble.instances) == 1
    assert runner._adapter_for_source(entry.origin) is adapter
    assert entry.origin.platform == Platform.TELEGRAM
    assert delivered.count("hello world") == 2


@pytest.mark.asyncio
async def test_operational_notice_during_turn_is_delivered_and_durable(runner, monkeypatch):
    from gateway.acp_bridge import GatewayACPBridge
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = runner.session_store.lookup_by_session_id(sid)
    loop = asyncio.get_running_loop()
    original = ModelDouble.run_conversation
    def run(self, *args, **kwargs):
        asyncio.run_coroutine_threadsafe(runner._deliver_platform_notice(entry.origin, "Operational notice"), loop).result(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    updates = []
    await bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "work"}]}, updates.append)
    assert "Operational notice" in str(updates)
    replay = []
    await bridge.load({"sessionId": sid}, replay.append)
    assert "Operational notice" in str(replay)


@pytest.mark.asyncio
async def test_local_background_wake_and_disconnected_notice_are_durable(runner):
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.config import Platform
    from gateway.wake import deliver_wake
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = runner.session_store.lookup_by_session_id(sid)
    adapter = runner.adapters[Platform.LOCAL]
    await deliver_wake(adapter, text="Background command exited 1", session_id=sid, source=entry.origin)
    await asyncio.wait_for(adapter._session_tasks[entry.session_key], 10)
    assert len(ModelDouble.instances) == 1
    assert (await adapter.send(entry.origin.chat_id, "Exit 1: command failed")).success
    # A new bridge has no live subscribers or memory of outbound delivery.
    replacement = GatewayACPBridge(runner)
    updates = []
    await replacement.load({"sessionId": sid}, updates.append)
    assert "hello world" in str(updates)
    assert "Exit 1: command failed" in str(updates)
    before = len(ModelDouble.instances)
    assert (await adapter.send(entry.origin.chat_id, "Later completion")).success
    assert "Later completion" in str(updates)
    assert len(ModelDouble.instances) == before


@pytest.mark.asyncio
async def test_background_wake_can_be_observed_while_running(runner, monkeypatch):
    import threading
    from gateway.acp_bridge import GatewayACPBridge
    from gateway.config import Platform
    from gateway.wake import deliver_wake
    started, release = threading.Event(), threading.Event()
    def run(self, *args, **kwargs):
        if self.tool_start_callback:
            self.tool_start_callback("background-work", "terminal", {})
        started.set()
        assert release.wait(10)
        if self.tool_complete_callback:
            self.tool_complete_callback("background-work", "terminal", {}, {"exit_code": 0})
        return {"completed": True, "messages": [], "final_response": "Async final"}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    entry = runner.session_store.lookup_by_session_id(sid)
    adapter = runner.adapters[Platform.LOCAL]
    await deliver_wake(adapter, text="Completion", source=entry.origin)
    task = adapter._session_tasks[entry.session_key]
    updates = []
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert (await bridge.load({"sessionId": sid}, updates.append))["_meta"]["activeTurn"]
        assert "background-work" in str(updates)
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
    # Delivery includes the task's terminal receipt, not merely admission.
    for _ in range(100):
        if any(u["method"] == "_hermes/turn_complete" for u in updates):
            break
        await asyncio.sleep(0.01)
    assert any(u["method"] == "_hermes/turn_complete" for u in updates)
    assert "Async final" in str(updates)


@pytest.mark.asyncio
async def test_load_during_active_turn_rebinds_tools_and_terminal(runner, monkeypatch):
    import threading
    from gateway.acp_bridge import GatewayACPBridge
    started, release = threading.Event(), threading.Event()
    def run(self, *args, **kwargs):
        self.stream_delta_callback("Working")
        self.tool_start_callback("in-flight", "terminal", {"command": "work"})
        started.set()
        assert release.wait(10)
        self.tool_complete_callback("in-flight", "terminal", {}, {"exit_code": 1})
        return {"completed": True, "messages": [], "final_response": "Failed command reported"}
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    old, new = [], []
    task = asyncio.create_task(bridge.prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "work"}]}, old.append))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        loaded = await bridge.load({"sessionId": sid}, new.append)
        assert loaded["_meta"]["activeTurn"]["status"] == "in_progress"
        assert any(m["params"].get("update", {}).get("toolCallId") == "in-flight" for m in new)
        old_count = len(old)
        release.set()
        await asyncio.wait_for(task, 10)
        assert len(old) == old_count
        assert any(m["params"].get("update", {}).get("status") == "failed" for m in new)
        assert "Failed command reported" in str(new)
        assert new[-1]["method"] == "_hermes/turn_complete"
        assert new[-1]["params"]["stopReason"] == "end_turn"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_during_model_execution_keeps_gateway_turn_and_reconnect_replays(runner, monkeypatch):
    import json
    import tempfile
    import threading
    from pathlib import Path
    from acp_adapter.local_transport import ACPListener
    from gateway.acp_bridge import GatewayACPBridge
    started, release = threading.Event(), threading.Event()
    original = ModelDouble.run_conversation
    def run(self, *args, **kwargs):
        self.tool_start_callback("waiting", "terminal", {"command": "wait"})
        started.set()
        assert release.wait(10)
        self.tool_complete_callback("waiting", "terminal", {}, {"exit_code": 0})
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ModelDouble, "run_conversation", run)
    bridge = GatewayACPBridge(runner)
    bridge.install_adapter()
    sid = (await bridge.new({}, lambda _: None))["sessionId"]
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = ACPListener(Path(directory), bridge.dispatch)
        await listener.start()
        reader, writer = await asyncio.open_unix_connection(listener.path)
        request = {"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {
            "sessionId": sid, "prompt": [{"type": "text", "text": "work"}]}}
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            assert await asyncio.to_thread(started.wait, 5)
            writer.close()
            await writer.wait_closed()
            assert ModelDouble.instances[0].is_interrupted is False
            reader, writer = await asyncio.open_unix_connection(listener.path)
            request.update(id=2, method="session/load", params={"sessionId": sid})
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            messages = []
            while True:
                item = json.loads(await asyncio.wait_for(reader.readline(), 5))
                messages.append(item)
                if item.get("id") == 2:
                    break
            assert messages[-1]["result"]["_meta"]["activeTurn"]["status"] == "in_progress"
            assert "waiting" in str(messages)
            release.set()
            while True:
                item = json.loads(await asyncio.wait_for(reader.readline(), 5))
                messages.append(item)
                if item.get("method") == "_hermes/turn_complete":
                    break
            assert "hello world" in "".join(m["params"]["update"]["content"]["text"] for m in messages
                if m.get("method") == "session/update" and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk")
            assert messages[-1]["params"]["stopReason"] == "end_turn"
            assert len(ModelDouble.instances) == 1
        finally:
            release.set()
            writer.close()
            await listener.close()
