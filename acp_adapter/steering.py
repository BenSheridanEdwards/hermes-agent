"""ACP ``_session/steering``: non-cancelling mid-turn steering for ACP hosts.

Hosts such as buzz-acp (and claude-agent-acp / codex-acp, which shipped the same extension)
send ``_session/steering`` with ``{sessionId, prompt}`` while a ``session/prompt`` is still in
flight. Without it a host that wants to deliver a follow-up has only ``session/cancel``: the
running turn is killed and restarted from a merged prompt, so every tool call already made is
thrown away. With it the follow-up lands inside the live turn, the way the Telegram gateway's
busy-text path already does with ``AIAgent.redirect()`` / ``steer()``.

The host discovers support through ``_meta.steering.supported: true`` on the ``initialize``
response (see :data:`STEERING_META`). The result carries an ``outcome``:

- ``injected`` — the follow-up reached the turn the host is waiting on, which keeps running.
- ``startedNewTurn`` — no turn was live (or it finished before the steer landed), so the
  adapter began a fresh turn carrying the message; the host must not renew its deadline.

Steering is the default. A follow-up whose content starts with ``/queue`` is held back and run
as its own turn once the current one finishes, never injected. Hosts that wrap the message in a
rendered event block (buzz-acp's ``Content: <text>`` line) are recognised too.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import acp
from acp.exceptions import RequestError
from acp.schema import TextContentBlock

from acp_adapter.session import QueuedPrompt, SessionState

logger = logging.getLogger(__name__)

# Wire method is ``_session/steering``; the ACP SDK strips the leading ``_`` before dispatch.
STEERING_METHOD = "session/steering"
STEERING_META: dict[str, Any] = {"steering": {"supported": True}}
OUTCOME_INJECTED = "injected"
OUTCOME_STARTED_NEW_TURN = "startedNewTurn"

# ``/queue`` either opens the message (plain editor/client) or opens the ``Content:`` value of a
# rendered event block (buzz-acp). Only that position counts: ``/queue`` inside prose is text.
_QUEUE_DIRECTIVE = re.compile(r"^(?P<lead>Content:[ \t]*)?/queue(?=\s|$)[ \t]*", re.MULTILINE)


def steer_text_from_params(params: dict[str, Any]) -> str:
    """Text of a steering request: ``prompt`` is ACP content blocks (only text blocks count),
    but a bare string is tolerated."""
    prompt = params.get("prompt")
    if isinstance(prompt, str):
        return prompt.strip()
    if not isinstance(prompt, list):
        return ""
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, dict):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        elif getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str):
            parts.append(block.text)
    return "\n".join(parts).strip()


def split_queue_directive(text: str) -> tuple[bool, str]:
    """``(queue_requested, text_without_directive)``.

    Recognises a leading ``/queue`` in the message itself, or one opening the ``Content:``
    line of a host-rendered event block. The ``Content:`` label is kept so the block still
    reads correctly; only the directive goes."""
    match = _QUEUE_DIRECTIVE.search(text)
    if match is None:
        return False, text
    if match.group("lead") is None and match.start() != 0:
        return False, text
    cleaned = text[: match.start()] + (match.group("lead") or "") + text[match.end():]
    return True, cleaned.strip()


def strip_event_queue_directives(text: str) -> str:
    """Remove ``/queue`` from every ``Content:`` line of a host-rendered prompt.

    Used on an ordinary ``session/prompt``: when nothing is running there is nothing to wait
    for, and the model must not see the directive as part of the user's words. A message that
    itself starts with ``/queue`` is a slash command and is left to the command handler."""
    return _QUEUE_DIRECTIVE.sub(lambda m: m.group("lead") or "", text) if "Content:" in text else text


class SteeringMixin:
    """``ext_method`` dispatch plus the steering handler; mixed into ``HermesACPAgent``.

    Expects ``self.session_manager``, ``self._conn`` and ``self.prompt`` from the host class."""

    session_manager: Any
    _conn: Any
    _steering_turns: set[asyncio.Task]

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == STEERING_METHOD:
            return await self._handle_steering(params or {})
        raise RequestError.method_not_found(f"_{method}")

    async def _handle_steering(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        state: SessionState | None = (
            self.session_manager.get_session(session_id) if isinstance(session_id, str) else None
        )
        if state is None:
            raise RequestError.invalid_params({"sessionId": session_id, "reason": "unknown session"})
        text = steer_text_from_params(params)
        if not text:
            raise RequestError.invalid_params({"prompt": "no text to steer with"})

        queue_requested, text = split_queue_directive(text)
        with state.runtime_lock:
            if state.is_running:
                if queue_requested:
                    state.queued_prompts.append(QueuedPrompt(text))
                    depth = len(state.queued_prompts)
                    logger.info("Session %s: /queue held a follow-up for after the turn (%d queued)", session_id, depth)
                    return {"outcome": OUTCOME_INJECTED, "queued": depth}
                if self._inject_into_active_turn(state, text):
                    logger.info("Session %s: steered the active turn (%d chars)", session_id, len(text))
                    return {"outcome": OUTCOME_INJECTED}
                logger.info("Session %s: active turn took no steer; running the message as a new turn", session_id)

        # Nothing live to steer into: the message becomes its own turn. The host is told so
        # it stops waiting on the turn it thought it was steering.
        self._start_steering_turn(state.session_id, text)
        return {"outcome": OUTCOME_STARTED_NEW_TURN}

    def _inject_into_active_turn(self, state: SessionState, text: str) -> bool:
        """Land ``text`` in the running turn. ``redirect`` (cancel only the in-flight model
        request, keep finished work, append the correction) when the runtime supports it,
        else ``steer`` (append to the next tool-result batch). Call with ``runtime_lock`` held."""
        agent = state.agent
        if agent is None:
            return False
        if getattr(agent, "_supports_active_turn_redirect", False) is True and hasattr(agent, "redirect"):
            try:
                if agent.redirect(text):
                    return True
            except Exception:
                logger.debug("ACP active-turn redirect failed for %s", state.session_id, exc_info=True)
        if hasattr(agent, "steer"):
            try:
                return bool(agent.steer(text))
            except Exception:
                logger.debug("ACP active-turn steer failed for %s", state.session_id, exc_info=True)
        return False

    def _start_steering_turn(self, session_id: str, text: str) -> asyncio.Task:
        """Run ``text`` as a fresh turn in the background; the host gets its updates through
        ``session/update`` like any other turn. Tasks are tracked so they are never collected
        mid-flight."""
        turns = getattr(self, "_steering_turns", None)
        if turns is None:
            turns = self._steering_turns = set()

        async def _run() -> None:
            conn = self._conn
            if conn:
                try:
                    await conn.session_update(session_id, acp.update_user_message_text(text))
                except Exception:
                    logger.debug("Could not echo steering prompt for %s", session_id, exc_info=True)
            await self.prompt(prompt=[TextContentBlock(type="text", text=text)], session_id=session_id)

        task = asyncio.get_running_loop().create_task(_run(), name=f"acp-steering-turn-{session_id}")
        turns.add(task)
        task.add_done_callback(turns.discard)
        return task
