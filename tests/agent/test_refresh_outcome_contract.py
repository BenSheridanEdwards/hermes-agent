"""Adversarial transport contracts: no real provider/network operations."""
import io
import urllib.error
import urllib.request

import pytest


@pytest.mark.parametrize('outcome', ['reset', 'truncated', '400', '401', '429', '503'])
def test_anthropic_ambiguous_outcome_never_replays_at_alternate_endpoint(monkeypatch, outcome):
    from agent.anthropic_credentials import refresh_anthropic_oauth_pure
    calls = []

    def transport(*args, **kwargs):
        calls.append('POST')
        if outcome == 'reset':
            raise urllib.error.URLError(ConnectionResetError('synthetic response loss'))
        if outcome == 'truncated':
            return io.BytesIO(b'{"access_token":')
        # The synthetic upstream has accepted the request before its proxy
        # emits this response. HTTP class cannot establish nonconsumption.
        raise urllib.error.HTTPError('https://synthetic.invalid', int(outcome), 'synthetic', {}, None)

    monkeypatch.setattr(urllib.request, 'urlopen', transport)
    # This pure adapter's supported failure contract raises; do not swallow
    # the error merely to satisfy a reviewer fixture expecting a return.
    with pytest.raises(Exception):
        refresh_anthropic_oauth_pure('synthetic-predecessor')
    assert calls == ['POST']
