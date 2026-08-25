"""Regression tests for the credential-pool provider-mismatch guard with
custom providers (Bernard's Fireworks report, June 2026).

Custom endpoints carry two naming conventions for the same provider: the
agent's ``provider`` attribute is the generic ``"custom"`` label while the
pool is keyed ``custom:<normalized-name>`` (``CUSTOM_POOL_PREFIX``).  The
defensive guard in ``recover_with_credential_pool`` compared the two
literally, logged "Credential pool provider mismatch: pool=custom:<name>,
agent=custom", and skipped recovery — so 401/429 recovery (refresh,
rotation) never ran for ANY custom-provider user.

The fix accepts the pair only when the agent's current base_url resolves to
the same pool key, preserving the guard's original purpose (#33088/#33163:
never mutate the primary's pool while a fallback provider is active).
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import (
    _is_official_opencode_go_url,
    recover_with_credential_pool,
)
from agent.error_classifier import FailoverReason


FIREWORKS_URL = "https://api.fireworks.ai/inference/v1"


def _agent(provider, base_url, pool_provider):
    agent = MagicMock()
    agent.provider = provider
    agent.base_url = base_url
    pool = MagicMock()
    pool.provider = pool_provider
    agent._credential_pool = pool
    return agent, pool


class TestCustomPoolMismatchGuard:

    def test_unrelated_custom_pool_still_guarded(self):
        """agent=custom pointed at a DIFFERENT endpoint than the pool's
        custom provider must still skip pool mutation."""
        agent, pool = _agent(
            "custom", "https://other-endpoint.example/v1", "custom:fireworks"
        )
        with patch(
            "agent.credential_pool.get_custom_provider_pool_key",
            return_value="custom:other",
        ):
            recovered, _ = recover_with_credential_pool(
                agent,
                status_code=401,
                has_retried_429=False,
                classified_reason=FailoverReason.auth,
            )
        assert recovered is False
        assert not pool.method_calls

    def test_fallback_provider_still_guarded(self):
        """Original #33088/#33163 contract: when a fallback provider is
        active (agent.provider != pool.provider, non-custom), the pool is
        never mutated."""
        agent, pool = _agent("openai-codex", "https://chatgpt.com/backend-api", "custom:fireworks")
        recovered, _ = recover_with_credential_pool(
            agent,
            status_code=401,
            has_retried_429=False,
            classified_reason=FailoverReason.auth,
        )
        assert recovered is False
        assert not pool.method_calls

    def test_custom_opencode_go_url_rotates_go_pool(self):
        agent, pool = _agent(
            "custom",
            "https://opencode.ai/zen/go/v1",
            "opencode-go",
        )
        nxt = MagicMock()
        nxt.id = "personal-go"
        agent._is_entitlement_failure.return_value = False
        pool.try_refresh_matching.return_value = None
        pool.mark_exhausted_and_rotate.return_value = nxt
        recovered, _ = recover_with_credential_pool(
            agent,
            status_code=401,
            has_retried_429=False,
            classified_reason=FailoverReason.auth,
        )
        assert recovered is True
        pool.mark_exhausted_and_rotate.assert_called_once()
        agent._swap_credential.assert_called_once_with(nxt)



class TestOfficialOpenCodeGoUrlDetection:
    """Which base URLs count as the official OpenCode Go host.

    A delegation child reporting ``provider=custom`` on the Go host must
    still rotate the Go credential pool. Too loose and an unrelated custom
    endpoint mutates CodeWalnut credentials; too strict and the original
    model-hop bug returns.
    """

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://opencode.ai/zen/go",
            "https://opencode.ai/zen/go/v1",
            "https://OpenCode.ai/Zen/Go/v1",
            "  https://opencode.ai/zen/go/v1  ",
        ],
    )
    def test_official_go_urls_are_recognised(self, base_url):
        assert _is_official_opencode_go_url(base_url) is True

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://opencode.ai/zen/pro",
            "https://openrouter.ai/api/v1",
            "https://api.openai.com/v1",
            "https://opencode.ai/zen",
        ],
    )
    def test_other_endpoints_are_not_the_go_pool(self, base_url):
        assert _is_official_opencode_go_url(base_url) is False

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_missing_base_url_is_not_the_go_pool(self, empty):
        """An agent with no base_url must not be rotated into the Go pool."""
        assert _is_official_opencode_go_url(empty) is False

    def test_non_string_base_url_does_not_raise(self):
        """base_url comes off a live agent object and is not always a str."""
        assert _is_official_opencode_go_url(object()) is False
