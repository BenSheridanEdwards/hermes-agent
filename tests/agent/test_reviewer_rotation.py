"""Reviewer-owned synthetic process reproductions; no provider/network access."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('review_process_helpers', Path(__file__).with_name('test_managed_refresh_processes.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth', 'anthropic'])
def test_http_503_does_not_prove_refresh_was_unconsumed(tmp_path, provider):
    root = helpers.fixture(tmp_path, provider)
    # A 503 response can be emitted after server-side consumption. Its HTTP
    # status alone is not proof that replaying the predecessor is safe.
    helpers.finish(helpers.start(root, provider, mode='outage'))
    intent = (root / 'auth.json.rotation-intent.json').read_text()
    assert intent != '{}', 'HTTP 503 erased the uncertain-generation fence'

@pytest.mark.parametrize('provider', ['openai-codex', 'xai-oauth', 'anthropic'])
def test_owner_without_consumer_policy_retains_post_failure_fence(tmp_path, provider):
    root = helpers.fixture(tmp_path, provider)
    store = json.loads((root / 'auth.json').read_text())
    store['credential_pool'][provider] = store['credential_pool'][provider][:1]
    (root / 'auth.json').write_text(json.dumps(store))
    for name in ['one', 'two']:
        (root / 'profiles' / name / 'config.yaml').write_text('model: synthetic\n')
    helpers.finish(helpers.start(root, provider, mode='fail-save'))
    persisted = json.loads((root / 'auth.json').read_text())
    assert persisted['credential_pool'][provider][0]['refresh_token'] == 'synthetic-old'
    before = len(helpers.calls(root))
    helpers.finish(helpers.start(root, provider, profile='two'))
    assert len(helpers.calls(root)) == before, 'unmanaged/owner path replayed the unpersisted predecessor'
