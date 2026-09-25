"""``_session/steering``: non-cancelling mid-turn steering over ACP (acp_adapter.steering)."""

import asyncio
from types import SimpleNamespace

import pytest
from acp.exceptions import RequestError
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from acp_adapter.steering import (
    OUTCOME_INJECTED, OUTCOME_STARTED_NEW_TURN, split_queue_directive, steer_text_from_params,
    strip_event_queue_directives,
)


class FakeAgent:
    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self._supports_active_turn_redirect = True
        self.steers = []
        self.redirects = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        final = f"ran: {user_message}"
        messages.append({"role": "assistant", "content": final})
        return {"final_response": final, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        return SimpleNamespace(outcome="allow")


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None


def make_agent_and_state():
    fake = FakeAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    acp_agent.on_connect(CaptureConn())
    return acp_agent, state, fake


def text_blocks(text):
    return [{"type": "text", "text": text}]


BUZZ_EVENT_BLOCK = (
    "<new-message>\n\n<buzz-event type=\"@mention\">\nEvent ID: abc\nChannel: fleet\nKind: 9\n"
    "From: chief\nTime: 2026-09-25T09:00:00+00:00\nContent: {content}\nTags: []\n</buzz-event>"
)


# ---- helpers ------------------------------------------------------------------


def test_steer_text_joins_text_blocks_and_ignores_others():
    params = {"prompt": [{"type": "text", "text": "a"}, {"type": "image", "data": "..."}, {"type": "text", "text": "b"}]}
    assert steer_text_from_params(params) == "a\nb"
    assert steer_text_from_params({"prompt": "  bare  "}) == "bare"
    assert steer_text_from_params({}) == ""


def test_queue_directive_recognised_at_message_start_and_on_content_line():
    assert split_queue_directive("/queue run the tests after") == (True, "run the tests after")
    queued, text = split_queue_directive(BUZZ_EVENT_BLOCK.format(content="/queue then deploy"))
    assert queued and "Content: then deploy" in text and "/queue" not in text


def test_queue_inside_prose_is_just_text():
    assert split_queue_directive("please /queue nothing") == (False, "please /queue nothing")
    assert split_queue_directive("Content: the /queue command") == (False, "Content: the /queue command")
    assert split_queue_directive("/queued up") == (False, "/queued up")


def test_strip_event_queue_directives_only_touches_content_lines():
    text = BUZZ_EVENT_BLOCK.format(content="/queue later")
    assert "Content: later" in strip_event_queue_directives(text)
    assert strip_event_queue_directives("/queue later") == "/queue later"


# ---- protocol -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_advertises_steering_support():
    acp_agent, _state, _fake = make_agent_and_state()
    response = await acp_agent.initialize()
    assert response.field_meta == {"steering": {"supported": True}}
    assert response.model_dump(by_alias=True)["_meta"]["steering"]["supported"] is True


@pytest.mark.asyncio
async def test_unknown_extension_method_is_method_not_found():
    acp_agent, _state, _fake = make_agent_and_state()
    with pytest.raises(RequestError):
        await acp_agent.ext_method("session/other", {})


@pytest.mark.asyncio
async def test_steering_unknown_session_is_invalid_params():
    acp_agent, _state, _fake = make_agent_and_state()
    with pytest.raises(RequestError):
        await acp_agent.ext_method("session/steering", {"sessionId": "nope", "prompt": text_blocks("x")})


@pytest.mark.asyncio
async def test_steering_redirects_the_active_turn_without_cancelling():
    acp_agent, state, fake = make_agent_and_state()
    state.is_running = True
    result = await acp_agent.ext_method(
        "session/steering", {"sessionId": state.session_id, "prompt": text_blocks("use the other branch")})
    assert result["outcome"] == OUTCOME_INJECTED
    assert fake.redirects == ["use the other branch"]
    assert fake.runs == [] and state.queued_prompts == []


@pytest.mark.asyncio
async def test_steering_falls_back_to_steer_when_redirect_is_unavailable():
    acp_agent, state, fake = make_agent_and_state()
    fake._supports_active_turn_redirect = False
    state.is_running = True
    result = await acp_agent.ext_method(
        "session/steering", {"sessionId": state.session_id, "prompt": text_blocks("prefer the simpler fix")})
    assert result["outcome"] == OUTCOME_INJECTED
    assert fake.steers == ["prefer the simpler fix"] and fake.redirects == []


@pytest.mark.asyncio
async def test_queue_directive_waits_for_the_turn_to_end():
    acp_agent, state, fake = make_agent_and_state()
    state.is_running = True
    body = BUZZ_EVENT_BLOCK.format(content="/queue then run the tests")
    result = await acp_agent.ext_method("session/steering", {"sessionId": state.session_id, "prompt": text_blocks(body)})
    assert result["outcome"] == OUTCOME_INJECTED and result["queued"] == 1
    assert fake.redirects == [] and fake.steers == []
    assert len(state.queued_prompts) == 1
    assert "Content: then run the tests" in state.queued_prompts[0].text
    assert "/queue" not in state.queued_prompts[0].text


@pytest.mark.asyncio
async def test_steering_an_idle_session_starts_a_new_turn():
    acp_agent, state, fake = make_agent_and_state()
    result = await acp_agent.ext_method(
        "session/steering", {"sessionId": state.session_id, "prompt": text_blocks("ship it")})
    assert result["outcome"] == OUTCOME_STARTED_NEW_TURN
    await asyncio.gather(*acp_agent._steering_turns)
    assert fake.runs == ["ship it"]
    assert state.is_running is False


@pytest.mark.asyncio
async def test_steering_when_the_turn_took_nothing_starts_a_new_turn():
    acp_agent, state, fake = make_agent_and_state()
    state.is_running = True
    fake.redirect = lambda text: False
    fake.steer = lambda text: False
    result = await acp_agent.ext_method(
        "session/steering", {"sessionId": state.session_id, "prompt": text_blocks("hello?")})
    assert result["outcome"] == OUTCOME_STARTED_NEW_TURN
    state.is_running = False  # release the fake turn so the new one can run
    await asyncio.gather(*acp_agent._steering_turns)
    assert fake.runs == ["hello?"]


@pytest.mark.asyncio
async def test_idle_prompt_drops_a_rendered_queue_directive():
    acp_agent, state, fake = make_agent_and_state()
    body = BUZZ_EVENT_BLOCK.format(content="/queue do the thing")
    response = await acp_agent.prompt(session_id=state.session_id, prompt=[TextContentBlock(type="text", text=body)])
    assert response.stop_reason == "end_turn"
    assert len(fake.runs) == 1 and "/queue" not in fake.runs[0] and "Content: do the thing" in fake.runs[0]


@pytest.mark.asyncio
async def test_concurrent_prompt_still_redirects_then_steers():
    acp_agent, state, fake = make_agent_and_state()
    state.is_running = True
    response = await acp_agent.prompt(
        session_id=state.session_id, prompt=[TextContentBlock(type="text", text="actually, stop at step 2")])
    assert response.stop_reason == "end_turn"
    assert fake.redirects == ["actually, stop at step 2"] and state.queued_prompts == []
