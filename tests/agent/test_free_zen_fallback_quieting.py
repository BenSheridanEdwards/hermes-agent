"""Which fallback transitions stay visible to operators.

Free OpenCode Zen models are capacity-shared and the router moves sessions on
and off them without anyone asking. Announcing those hops trained operators to
ignore the fallback notice, so they are suppressed. The risk is suppressing a
real routing change, so these tests pin both directions.
"""

import ast
import pathlib

import pytest

from agent import chat_completion_helpers
from agent.chat_completion_helpers import (
    _FREE_ZEN_MODELS,
    _FREE_ZEN_PROVIDER,
    _is_routine_free_zen_hop,
)

FREE = "x-preview-f-free"
OTHER_FREE = "mimo-v2.5-free"
PRIMARY = ("openai", "gpt-5.6-sol")


def hop(old, new, primary=PRIMARY):
    """Evaluate a (provider, model) -> (provider, model) transition."""
    return _is_routine_free_zen_hop(
        old_provider=old[0],
        old_model=old[1],
        new_provider=new[0],
        new_model=new[1],
        primary_provider=primary[0],
        primary_model=primary[1],
    )


class TestQuietTransitions:
    @pytest.mark.parametrize("model", sorted(_FREE_ZEN_MODELS))
    def test_hop_onto_a_free_zen_model_is_quiet(self, model):
        assert hop(PRIMARY, (_FREE_ZEN_PROVIDER, model)) is True

    @pytest.mark.parametrize("model", sorted(_FREE_ZEN_MODELS))
    def test_hop_off_free_zen_back_to_primary_is_quiet(self, model):
        assert hop((_FREE_ZEN_PROVIDER, model), PRIMARY) is True

    def test_provider_comparison_ignores_case_and_padding(self):
        assert hop(PRIMARY, ("  OpenCode-Zen ", FREE)) is True
        assert hop(("OPENCODE-ZEN", FREE), ("  OpenAI ", "gpt-5.6-sol")) is True

    def test_free_zen_to_free_zen_is_quiet_by_the_arrival_rule(self):
        assert hop((_FREE_ZEN_PROVIDER, FREE), (_FREE_ZEN_PROVIDER, OTHER_FREE)) is True

    def test_primary_model_padding_does_not_break_the_match(self):
        assert hop(
            (_FREE_ZEN_PROVIDER, FREE),
            ("openai", "gpt-5.6-sol"),
            primary=("openai", "  gpt-5.6-sol  "),
        ) is True


class TestVisibleTransitions:
    """The failure mode that matters: a real switch going unannounced."""

    def test_ordinary_provider_switch_is_announced(self):
        assert hop(PRIMARY, ("anthropic", "claude-opus-5")) is False

    def test_paid_model_on_the_zen_provider_is_announced(self):
        assert hop(PRIMARY, (_FREE_ZEN_PROVIDER, "zen-paid-model")) is False

    def test_free_zen_model_on_another_provider_is_announced(self):
        assert hop(PRIMARY, ("openrouter", FREE)) is False

    def test_leaving_free_zen_for_anything_but_the_primary_is_announced(self):
        assert hop((_FREE_ZEN_PROVIDER, FREE), ("anthropic", "claude-opus-5")) is False

    def test_leaving_free_zen_for_the_primary_provider_but_a_different_model(self):
        assert hop((_FREE_ZEN_PROVIDER, FREE), ("openai", "gpt-4o")) is False

    def test_unknown_primary_keeps_the_departure_visible(self):
        """No recorded primary means we cannot prove this is a hand-back."""
        assert hop(
            (_FREE_ZEN_PROVIDER, FREE), ("openai", "gpt-5.6-sol"), primary=(None, None)
        ) is False

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_providers_never_read_as_free_zen(self, blank):
        assert hop(PRIMARY, (blank, FREE)) is False


class TestNoticeIsActuallyGated:
    """The predicate is only useful if it gates the operator-visible surface.

    ``try_activate_fallback`` needs a large agent surface (provider client
    resolution, cache policy, URL classification) to reach the notice block, so
    driving it end to end here would test the fixture more than the behaviour.
    The wiring is asserted structurally instead, which is the same approach
    ``tests/agent/test_prompt_cache_ttl_propagation.py`` takes.
    """

    @staticmethod
    def _notice_guard():
        source = pathlib.Path(chat_completion_helpers.__file__).read_text()
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "try_activate_fallback"
        )
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
                continue
            call = test.operand
            if isinstance(call, ast.Call) and getattr(
                call.func, "id", None
            ) == "_is_routine_free_zen_hop":
                return node, call
        raise AssertionError(
            "no `if not _is_routine_free_zen_hop(...)` guard in try_activate_fallback"
        )

    def test_operator_notice_is_inside_the_guard(self):
        guard, _ = self._notice_guard()
        assigned = {
            target.attr
            for node in ast.walk(guard)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "_pending_fallback_notice" in assigned, (
            "the one-shot operator notice must be suppressed for routine hops"
        )

    def test_buffered_status_line_is_inside_the_guard(self):
        guard, _ = self._notice_guard()
        called = {
            node.func.attr
            for node in ast.walk(guard)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_buffer_status" in called

    def test_guard_is_asked_about_the_transition_it_is_deciding(self):
        """A guard fed the wrong endpoints would silence real switches."""
        _, call = self._notice_guard()
        assert {kw.arg for kw in call.keywords} == {
            "old_provider",
            "old_model",
            "new_provider",
            "new_model",
            "primary_provider",
            "primary_model",
        }

    def test_transition_logging_stays_outside_the_guard(self):
        """Quieting is an operator-surface decision, not a logging one.

        Every transition must remain in the log even when it is not announced,
        otherwise a silent hop becomes unreconstructable after the fact.
        """
        guard, _ = self._notice_guard()
        guarded = {id(node) for node in ast.walk(guard)}
        source = pathlib.Path(chat_completion_helpers.__file__).read_text()
        func = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.name == "try_activate_fallback"
        )
        activated_logs = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "Fallback activated" in str(node.args[0].value)
        ]
        assert activated_logs, "expected a 'Fallback activated' log line"
        assert all(id(node) not in guarded for node in activated_logs)
