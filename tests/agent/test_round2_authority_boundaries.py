"""Round-two usability and sibling-format coverage; synthetic stores only."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('round2_faults', Path(__file__).with_name('test_rereviewer_authority_faults.py'))
faults = importlib.util.module_from_spec(spec)
spec.loader.exec_module(faults)
f = faults.f


@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_structured_nonhex_history_refuses_without_mutation(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    intent = root / 'auth.json.rotation-intent.json'
    intent.write_text(json.dumps({provider + ':damaged': {'generation': 'Z' * 64, 'state': 'committed'}}))
    before = intent.read_bytes()
    assert not f.finish(f.start(root, provider))['selected']
    assert f.calls(root) == []
    assert intent.read_bytes() == before


@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_independent_peer_accounts_both_remain_usable(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    data = json.loads((root / 'auth.json').read_text())
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: []}}))
    for name in ['one', 'two']:
        home = root / 'profiles' / name
        row = dict(data['credential_pool'][provider][0], refresh_token='synthetic-independent-' + name)
        (home / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [row]}}))
        policy = home / 'assignments.json'
        manifest = json.loads(policy.read_text())
        manifest['accounts'][provider]['store'] = 'profile'
        policy.write_text(json.dumps(manifest))
    results = [f.finish(f.start(root, provider, profile=name)) for name in ['one', 'two']]
    assert all(r['selected'] and r['fresh'] for r in results)
    assert len(f.calls(root)) == 2


def test_canonical_publication_gate_alias_is_usable(tmp_path):
    from agent.credential_policy import load_policy, CredentialPolicyError
    home, policy, gate, data = faults.publication_fixture(tmp_path)
    alias = tmp_path / 'gate-alias.json'
    alias.symlink_to(gate)
    data['publication']['file'] = str(alias)
    policy.write_text(json.dumps(data))
    assert load_policy(home) is not None
    gate.write_text(json.dumps({'version': 1, 'state': 'publishing', 'revision': 2}))
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    assert alias.is_symlink()
