"""Regression: xAI TTS retries once with refreshed OAuth credentials.

Mirrors the STT stale-bearer incident: a newly authorized account exists in
the OAuth pool but the selected bearer is stale, so ``/v1/tts`` rejects the
request. The retry must feed the rejected bearer back into the shared
credential resolver and repeat the request once with the fresh token.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from tools.tts_tool import _generate_xai_tts


@pytest.mark.parametrize("rejected_status", [401, 403])
def test_tts_retries_auth_rejection_with_refreshed_oauth_credentials(
    tmp_path, monkeypatch, rejected_status
):
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    rejected = MagicMock()
    rejected.status_code = rejected_status
    accepted = MagicMock()
    accepted.status_code = 200
    accepted.content = b"mp3-bytes"
    accepted.raise_for_status = MagicMock()

    out = tmp_path / "out.mp3"
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        side_effect=[
            {
                "api_key": "stale-oauth-token",
                "base_url": "https://api.x.ai/v1",
                "provider": "xai-oauth",
            },
            {
                "api_key": "fresh-oauth-token",
                "base_url": "https://api.x.ai/v1",
                "provider": "xai-oauth",
            },
        ],
    ) as mock_resolve, patch(
        "requests.post", side_effect=[rejected, accepted]
    ) as mock_post:
        result = _generate_xai_tts(
            "Hello there.",
            str(out),
            {"xai": {"voice_id": "ara", "language": "en"}},
        )

    assert result == str(out)
    assert out.read_bytes() == b"mp3-bytes"
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[0].kwargs["headers"]["Authorization"] == (
        "Bearer stale-oauth-token"
    )
    assert mock_post.call_args_list[1].kwargs["headers"]["Authorization"] == (
        "Bearer fresh-oauth-token"
    )
    assert mock_resolve.call_args_list == [
        call(),
        call(force_refresh=True, api_key_hint="stale-oauth-token"),
    ]
    # The rejected response must never have been trusted for output.
    rejected.raise_for_status.assert_not_called()


def test_tts_static_api_key_rejection_does_not_retry(tmp_path, monkeypatch):
    """Env-var credentials can't be refreshed — one request, error surfaces."""
    import requests as requests_lib

    monkeypatch.delenv("XAI_API_KEY", raising=False)

    rejected = MagicMock()
    rejected.status_code = 401
    rejected.raise_for_status.side_effect = requests_lib.HTTPError(
        response=rejected
    )

    out = tmp_path / "out.mp3"
    with patch(
        "tools.xai_http.resolve_xai_http_credentials",
        return_value={
            "api_key": "static-env-key",
            "base_url": "https://api.x.ai/v1",
            "provider": "xai",
        },
    ) as mock_resolve, patch(
        "requests.post", return_value=rejected
    ) as mock_post:
        with pytest.raises(requests_lib.HTTPError):
            _generate_xai_tts(
                "Hello there.",
                str(out),
                {"xai": {"voice_id": "ara", "language": "en"}},
            )

    assert mock_post.call_count == 1
    assert mock_resolve.call_args_list == [call()]
