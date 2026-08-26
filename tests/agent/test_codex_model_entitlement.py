"""Codex plan-entitlement rejections rotate the credential, not the model.

A free/Plus ChatGPT account asked for a Pro-only Codex slug gets HTTP 400
"The '<slug>' model is not supported when using Codex with a ChatGPT account."
That is a property of the CREDENTIAL's plan, not of the request or the slug:
a sibling credential on a higher plan serves the identical call.

Before this was classified, it fell into ``format_error`` (abort), the pool was
never consulted, and the agent silently demoted onto the provider fallback
chain - running a whole fleet on free models while a healthy Pro credential sat
unused at the next pool priority.
"""
import pytest

from agent.error_classifier import classify_api_error, FailoverReason
from agent.credential_pool import CredentialPool, PooledCredential

CODEX_400 = (
    "Error code: 400 - {'detail': \"The 'gpt-5.6-sol' model is not supported "
    "when using Codex with a ChatGPT account.\"}"
)


class _Err(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


def _pool(strategy="fill_first"):
    def mk(cid, label, priority):
        return PooledCredential.from_dict("openai-codex", {
            "id": cid, "label": label, "auth_type": "oauth",
            "priority": priority, "source": "manual:device_code",
            "access_token": "tok-" + cid, "refresh_token": "r",
            "last_status": "ok",
            "base_url": "https://chatgpt.com/backend-api/codex",
        })
    pool = CredentialPool("openai-codex", [
        mk("free01", "Free Codex", 0),
        mk("pro01", "Pro Codex", 1),
    ])
    pool._strategy = strategy
    return pool


class TestClassification:
    def test_entitlement_400_rotates_credential(self):
        result = classify_api_error(
            _Err(CODEX_400, 400), provider="openai-codex", model="gpt-5.6-sol",
            approx_tokens=1000, context_length=272000, num_messages=5)
        assert result.reason is FailoverReason.model_not_entitled
        assert result.should_rotate_credential is True
        assert result.retryable is False

    def test_not_confused_with_a_bogus_slug(self):
        """Must not read as model_not_found - that would hop model, not rotate."""
        result = classify_api_error(
            _Err(CODEX_400, 400), provider="openai-codex", model="gpt-5.6-sol",
            approx_tokens=10, context_length=272000, num_messages=1)
        assert result.reason is not FailoverReason.model_not_found

    def test_genuinely_missing_model_still_classifies_as_not_found(self):
        result = classify_api_error(
            _Err("Error code: 404 - the model `gpt-9` does not exist", 404),
            provider="openai", model="gpt-9",
            approx_tokens=10, context_length=200000, num_messages=1)
        assert result.reason is FailoverReason.model_not_found

    def test_billing_exhaustion_still_classifies_as_billing(self):
        result = classify_api_error(
            _Err("Error code: 400 - insufficient credits", 400),
            provider="openai", model="gpt-5.5",
            approx_tokens=10, context_length=200000, num_messages=1)
        assert result.reason is FailoverReason.billing


class TestPoolBlocks:
    def test_blocked_credential_is_skipped_for_that_model_only(self):
        pool = _pool()
        pool.set_active_model("gpt-5.6-sol")
        assert pool.select().label == "Free Codex"

        pool.block_for_model("free01", "gpt-5.6-sol")

        pool.set_active_model("gpt-5.6-sol")
        assert pool.select().label == "Pro Codex"

        # The whole point: the free credential keeps filling first for the
        # models it IS entitled to, so its quota is not wasted.
        for shared in ("gpt-5.6-luna", "gpt-5.5", "gpt-5.6-terra"):
            pool.set_active_model(shared)
            assert pool.select().label == "Free Codex", shared

    def test_block_does_not_quarantine_the_credential(self):
        pool = _pool()
        pool.block_for_model("free01", "gpt-5.6-sol")
        entry = next(e for e in pool.entries() if e.id == "free01")
        assert entry.last_status == "ok"
        assert entry.last_error_reset_at is None

    def test_pool_degrades_when_no_credential_is_entitled(self):
        pool = _pool()
        pool.block_for_model("free01", "gpt-5.6-sol")
        pool.block_for_model("pro01", "gpt-5.6-sol")
        pool.set_active_model("gpt-5.6-sol")
        assert pool.select() is None
        # ...but only for that model.
        pool.set_active_model("gpt-5.6-luna")
        assert pool.select() is not None

    def test_unset_active_model_disables_filtering(self):
        """No model in play means we must not guess which blocks apply."""
        pool = _pool()
        pool.block_for_model("free01", "gpt-5.6-sol")
        pool.set_active_model(None)
        assert pool.select().label == "Free Codex"

    def test_is_blocked_for_model_reports_precisely(self):
        pool = _pool()
        pool.block_for_model("free01", "gpt-5.6-sol")
        assert pool.is_blocked_for_model("free01", "gpt-5.6-sol") is True
        assert pool.is_blocked_for_model("free01", "gpt-5.6-luna") is False
        assert pool.is_blocked_for_model("pro01", "gpt-5.6-sol") is False

    def test_blocks_are_not_persisted_to_disk(self):
        """A plan upgrade must be picked up on restart with no manual reset."""
        pool = _pool()
        pool.block_for_model("free01", "gpt-5.6-sol")
        assert "gpt-5.6-sol" not in pool.entries()[0].to_dict().get("blocked_models", [])

    @pytest.mark.parametrize("cid,model", [("", "gpt-5.6-sol"), ("free01", ""), ("", "")])
    def test_incomplete_identity_is_ignored(self, cid, model):
        pool = _pool()
        pool.block_for_model(cid, model)
        pool.set_active_model("gpt-5.6-sol")
        assert pool.select().label == "Free Codex"


class TestRoundRobinUnaffected:
    def test_round_robin_still_alternates_for_entitled_models(self):
        pool = _pool(strategy="round_robin")
        pool.block_for_model("free01", "gpt-5.6-sol")
        pool.set_active_model("gpt-5.6-luna")
        seen = {pool.select().label for _ in range(4)}
        assert seen == {"Free Codex", "Pro Codex"}
