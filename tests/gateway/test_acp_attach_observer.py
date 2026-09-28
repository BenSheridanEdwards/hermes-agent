import asyncio
from types import SimpleNamespace
import pytest


@pytest.mark.asyncio
async def test_observation_composes_native_callbacks_with_progress_and_fences_stale_turns():
    from gateway.acp_observer import AttachedTurn
    updates, native = [], []
    active = [True]
    turn = AttachedTurn("sid", "key", updates.append)
    agent = SimpleNamespace(stream_delta_callback=lambda text: native.append(text),
                            tool_progress_callback=lambda *a, **kw: native.append("progress"))
    ctx = SimpleNamespace(session_key="key", session_id="sid", _run_still_current=lambda: active[0])
    turn.bind(agent, ctx)
    agent.stream_delta_callback("hello")
    agent.tool_start_callback("one", "terminal", {})
    agent.tool_progress_callback("tool.progress", "terminal", "working", call_id="one")
    agent.tool_complete_callback("one", "terminal", {}, {"error": "failed"})
    await asyncio.sleep(0)
    active[0] = False
    agent.stream_delta_callback("stale")
    await asyncio.sleep(0)
    assert native == ["hello", "progress", "stale"]
    rows = [m["params"]["update"] for m in updates]
    assert len(rows) == 4
    assert rows[2]["toolCallId"] == "one"
    assert rows[2]["status"] == "in_progress"
    assert rows[3]["status"] == "failed"


@pytest.mark.asyncio
async def test_thread_to_loop_backlog_is_bounded():
    from gateway.acp_observer import AttachedTurn
    turn = AttachedTurn("sid", "key", lambda msg: None)
    # No loop yield: a fast model worker can outrun even a healthy subscriber.
    for _ in range(10000):
        turn.text("x")
    assert turn.overflow is True
    assert turn.pending <= 64
    await asyncio.sleep(0)
    assert turn.pending == 0


@pytest.mark.asyncio
async def test_generation_change_discards_already_buffered_frames():
    from gateway.acp_observer import AttachedTurn
    rows = []
    turn = AttachedTurn("sid", "key", rows.append)
    generation = [1]
    turn.current = lambda: generation[0] == 1
    turn.text("old generation")
    generation[0] = 2
    await asyncio.sleep(0)
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result,status", [
    ({"exit_code": 1, "error": None}, "failed"),
    ({"status": "failed"}, "failed"),
    ({"status": "cancelled"}, "failed"),
    ({"exit_code": None, "session_id": "bg", "status": "running"}, "in_progress"),
    ({"exit_code": 0}, "completed"),
    ({"exit_code": 0, "session_id": "process-1", "pid": 123}, "in_progress"),
])
async def test_tool_outcome_is_not_inferred_from_callback_name(result, status):
    from gateway.acp_observer import AttachedTurn
    rows = []
    turn = AttachedTurn("sid", "key", rows.append)
    turn.tool_complete("call", "terminal", {}, result)
    await asyncio.sleep(0)
    assert rows[-1]["params"]["update"]["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,final,expected", [
    ("Checking...", "The answer", "Checking...\n\nThe answer"),
    ("Partial", "Partial answer\nVerified", "Partial answer\nVerified"),
    ("Exact", "Exact", "Exact"),
])
async def test_authoritative_final_reconciles_stream(stream, final, expected):
    from gateway.acp_observer import AttachedTurn, observed_handler
    rows = []
    turn = AttachedTurn("sid", "key", rows.append)
    async def handler(event):
        turn.text(stream)
        turn.result = {"completed": True, "final_response": final}
    await observed_handler(handler)(SimpleNamespace(_acp_turn=turn))
    await asyncio.sleep(0)
    assert "".join(m["params"]["update"]["content"]["text"] for m in rows) == expected


@pytest.mark.asyncio
async def test_superseded_result_is_not_reported_as_success():
    from gateway.acp_observer import AttachedTurn, observed_handler
    turn = AttachedTurn("sid", "key", lambda _: None)
    turn.result = {"completed": True}
    turn.current = lambda: False
    async def handler(event):
        return None
    with pytest.raises(RuntimeError, match="superseded"):
        await observed_handler(handler)(SimpleNamespace(_acp_turn=turn))
    with pytest.raises(RuntimeError, match="superseded"):
        await turn.done
