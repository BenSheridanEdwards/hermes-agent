"""ACP must deliver a finished background result into the originating session turn.

buzz-acp keeps the engine alive from `.buzz-background-work` and then prompts the
originating session. Without a drain here, that prompt never sees the result.
"""

from __future__ import annotations

import asyncio

from acp.schema import TextContentBlock


class _Conn:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update):
        self.updates.append(update)


def test_origin_prompt_includes_finished_background_result_once(monkeypatch):
    from acp_adapter.server import HermesACPAgent
    from tools.process_registry import process_registry

    seen = {}

    class _State:
        session_id = "sess-a"
        history = []
        cancel_event = None
        agent = type("Agent", (), {"session_id": "sess-a"})()
        message_ids = None
        is_running = False
        current_prompt_text = ""
        queued_prompts = []
        command_op = None
        interrupted_prompt_text = ""
        runtime_lock = __import__("threading").Lock()

    state = _State()

    class _Sessions:
        def get_session(self, session_id):
            return state if session_id == "sess-a" else None

        def save_session(self, session_id):
            return None

    agent = HermesACPAgent(session_manager=_Sessions())
    agent._conn = _Conn()
    process_registry.completion_queue.put({
        "type": "async_delegation",
        "delegation_id": "d1",
        "session_key": "sess-a",
        "status": "completed",
        "summary": "worker finished the long task",
    })
    process_registry.completion_queue.put({
        "type": "async_delegation",
        "delegation_id": "d2",
        "session_key": "other-session",
        "status": "completed",
        "summary": "must not leak",
    })

    def _run_agent_turn(**kwargs):
        seen["user_text"] = kwargs["user_text"]
        return {"final_response": "Reported.", "messages": [{"role": "assistant", "content": "Reported."}]}

    monkeypatch.setattr(agent, "_run_agent_turn", _run_agent_turn)
    monkeypatch.setattr(agent, "_wire_turn_callbacks", lambda *a, **k: type("C", (), {"streamed": False, "tool_call_ids": None, "approval_cb": None, "edit_approval_requester": None})())
    monkeypatch.setattr(agent, "_flush_turn_tool_calls", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_send_usage_update", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda evt, consumer: evt["delegation_id"])
    settled = []
    monkeypatch.setattr(agent, "_settle_process_notifications", lambda claims, delivered, attempted=False: settled.append((claims, delivered, attempted)))

    response = asyncio.run(agent.prompt(
        prompt=[TextContentBlock(type="text", text="Heartbeat. Stay silent unless a result is ready.")],
        session_id="sess-a",
    ))

    assert response.stop_reason == "end_turn"
    assert "worker finished the long task" in seen["user_text"]
    assert "BACKGROUND WORK FINISHED" in seen["user_text"]
    assert "must not leak" not in seen["user_text"]
    assert settled and settled[0][1] is True and settled[0][2] is True
    leftover = []
    while not process_registry.completion_queue.empty():
        leftover.append(process_registry.completion_queue.get_nowait())
    assert [item["delegation_id"] for item in leftover] == ["d2"]
