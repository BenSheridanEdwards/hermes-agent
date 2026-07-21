"""Tests for the shared xAI OAuth refresh-and-retry policy.

:func:`tools.xai_http.refresh_rejected_oauth_bearer` is the single policy
chokepoint used by every direct xAI HTTP consumer (STT, TTS, web search,
X search, image generation) to decide whether a rejected bearer warrants
one refresh-and-retry. These tests pin the decision table; per-consumer
regressions live next to each consumer's own test module.
"""

from unittest.mock import patch

import pytest

from tools.xai_http import refresh_rejected_oauth_bearer


def _fresh_creds():
    return {
        "provider": "xai-oauth",
        "api_key": "fresh-oauth-token",
        "base_url": "https://api.x.ai/v1",
    }


@pytest.mark.parametrize("rejected_status", [401, 403])
def test_auth_rejection_on_oauth_path_refreshes(rejected_status):
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        return_value=_fresh_creds(),
    ) as mock_resolve:
        refreshed = refresh_rejected_oauth_bearer(
            status_code=rejected_status,
            provider="xai-oauth",
            rejected_bearer="stale-oauth-token",
            context="test",
        )

    assert refreshed == _fresh_creds()
    mock_resolve.assert_called_once_with(
        force_refresh=True,
        api_key_hint="stale-oauth-token",
    )


@pytest.mark.parametrize("status", [200, 400, 404, 429, 500, 503])
def test_non_auth_status_does_not_refresh(status):
    with patch("tools.xai_http.resolve_xai_http_credentials") as mock_resolve:
        refreshed = refresh_rejected_oauth_bearer(
            status_code=status,
            provider="xai-oauth",
            rejected_bearer="stale-oauth-token",
            context="test",
        )

    assert refreshed is None
    mock_resolve.assert_not_called()


@pytest.mark.parametrize("provider", ["xai", "", "something-else"])
def test_non_oauth_provider_does_not_refresh(provider):
    """A static XAI_API_KEY cannot be refreshed — retry must be skipped."""
    with patch("tools.xai_http.resolve_xai_http_credentials") as mock_resolve:
        refreshed = refresh_rejected_oauth_bearer(
            status_code=401,
            provider=provider,
            rejected_bearer="static-key",
            context="test",
        )

    assert refreshed is None
    mock_resolve.assert_not_called()


def test_refresh_failure_returns_none():
    """A resolver exception is logged, never propagated to the caller."""
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        side_effect=RuntimeError("pool locked"),
    ):
        refreshed = refresh_rejected_oauth_bearer(
            status_code=403,
            provider="xai-oauth",
            rejected_bearer="stale-oauth-token",
            context="test",
        )

    assert refreshed is None


def test_same_bearer_after_refresh_returns_none():
    """Refresh handing back the rejected bearer means retrying is pointless."""
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        return_value={
            "provider": "xai-oauth",
            "api_key": "stale-oauth-token",
            "base_url": "https://api.x.ai/v1",
        },
    ):
        refreshed = refresh_rejected_oauth_bearer(
            status_code=401,
            provider="xai-oauth",
            rejected_bearer="stale-oauth-token",
            context="test",
        )

    assert refreshed is None


def test_empty_bearer_after_refresh_returns_none():
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        return_value={"provider": "xai-oauth", "api_key": "", "base_url": ""},
    ):
        refreshed = refresh_rejected_oauth_bearer(
            status_code=401,
            provider="xai-oauth",
            rejected_bearer="stale-oauth-token",
            context="test",
        )

    assert refreshed is None
