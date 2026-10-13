"""Independent synthetic-only owner/publication adversarial re-review."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import os
import pytest

spec = importlib.util.spec_from_file_location('rr_helpers', Path(__file__).with_name('test_managed_refresh_processes.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_root_owner_refuses_a_known_profile_copy(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    owner = root / 'auth.json'
    data = json.loads(owner.read_text())
    data['credential_pool'][provider] = data['credential_pool'][provider][:1]
    owner.write_text(json.dumps(data))
    copy = root / 'profiles' / 'two' / 'auth.json'
    copy.write_text(json.dumps(data))
    before = {p: p.read_bytes() for p in [owner, copy]}
    result = f.finish(f.start(root, provider, profile='one'))
    print(json.dumps({'case': 'root-with-local-copy', 'provider': provider, 'result': result, 'posts': len(f.calls(root)), 'stores_unchanged': all(p.read_bytes() == b for p,b in before.items())}))
    assert f.calls(root) == [], 'canonical root must not spend a grant still held in a distinct profile store'
    assert not result['selected']
    assert all(p.read_bytes() == b for p,b in before.items())

@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_two_profile_copies_without_root_are_refused(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    data = json.loads((root / 'auth.json').read_text())
    data['credential_pool'][provider] = data['credential_pool'][provider][:1]
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: []}}))
    for name in ['one', 'two']:
        home = root / 'profiles' / name
        (home / 'auth.json').write_text(json.dumps(data))
        manifest = json.loads((home / 'assignments.json').read_text())
        manifest['accounts'][provider]['store'] = 'profile'
        (home / 'assignments.json').write_text(json.dumps(manifest))
    results = [f.finish(f.start(root, provider, profile=name)) for name in ['one', 'two']]
    print(json.dumps({'case': 'two-local-copies-empty-root', 'provider': provider, 'results': results, 'posts': len(f.calls(root))}))
    assert f.calls(root) == [], 'equal grants in distinct profile owner stores must not each rotate'
    assert all(not r['selected'] for r in results)

@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_nonhex_journal_generation_is_rejected_before_post(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    intent = root / 'auth.json.rotation-intent.json'
    intent.write_text(json.dumps({provider + ':damaged': 'Z' * 64}))
    result = f.finish(f.start(root, provider))
    print(json.dumps({'case': 'malformed-generation', 'provider': provider, 'result': result, 'posts': len(f.calls(root))}))
    assert f.calls(root) == [], 'a malformed present journal must be rejected, not treated as unrelated safe history'


def publication_fixture(tmp_path):
    home = tmp_path / 'home'
    access = tmp_path / 'access'
    (access / 'assignments').mkdir(parents=True)
    home.mkdir()
    policy = access / 'assignments' / 'one.json'
    gate = access / 'authority-publication.json'
    home.joinpath('config.yaml').write_text(f'credential_policy:\n  file: {policy}\n')
    data = {'version': 2, 'environment': {}, 'managed_environment': ['REVOKED_KEY'], 'accounts': {}, 'publication': {'file': str(gate), 'revision': 1}}
    policy.write_text(json.dumps(data))
    gate.write_text(json.dumps({'version': 1, 'state': 'committed', 'revision': 1}))
    return home, policy, gate, data


def test_publication_cannot_bind_an_unrelated_committed_gate(tmp_path):
    from agent.credential_policy import load_policy, CredentialPolicyError
    home, policy, gate, data = publication_fixture(tmp_path)
    decoy = tmp_path / 'unrelated-committed.json'
    decoy.write_text(json.dumps({'version': 1, 'state': 'committed', 'revision': 1}))
    gate.write_text(json.dumps({'version': 1, 'state': 'publishing', 'revision': 2}))
    data['publication']['file'] = str(decoy)
    policy.write_text(json.dumps(data))
    rejected = False
    try:
        load_policy(home)
    except CredentialPolicyError:
        rejected = True
    print(json.dumps({'case': 'wrong-publication-owner', 'rejected': rejected}))
    assert rejected, 'native reader must bind publication to its actual owner rather than any matching JSON file'


def test_publication_rejects_boolean_gate_version(tmp_path, monkeypatch):
    from agent.credential_policy import load_policy, CredentialPolicyError
    home, policy, gate, data = publication_fixture(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    gate.write_text(json.dumps({'version': True, 'state': 'committed', 'revision': 1}))
    with pytest.raises(CredentialPolicyError):
        load_policy(home)


def test_long_lived_pool_and_fresh_reader_reject_pending_then_cohere(tmp_path, monkeypatch):
    from agent.credential_policy import load_policy, managed_pool, CredentialPolicyError
    home, policy, gate, data = publication_fixture(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    data['accounts'] = {'openrouter': {'store': 'profile', 'ids': ['shared']}}
    policy.write_text(json.dumps(data))
    home.joinpath('auth.json').write_text(json.dumps({'credential_pool': {'openrouter': [{'id': 'shared', 'source': 'manual', 'auth_type': 'api_key', 'access_token': 'synthetic-ready-key'}]}}))
    original = load_policy(home)
    assert original is not None
    pool = managed_pool('openrouter', original)
    assert pool.select().access_token == 'synthetic-ready-key'
    gate.write_text(json.dumps({'version': 1, 'state': 'publishing', 'revision': 2}))
    with pytest.raises(CredentialPolicyError):
        pool.select()
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    # A completed no-op restores the old revision and existing usable pool.
    gate.write_text(json.dumps({'version': 1, 'state': 'committed', 'revision': 1}))
    assert pool.select().access_token == 'synthetic-ready-key'
    # A real revocation changes the manifest and denies the old pool plus fresh selection.
    data['accounts']['openrouter']['ids'] = []
    data['publication']['revision'] = 2
    policy.write_text(json.dumps(data))
    gate.write_text(json.dumps({'version': 1, 'state': 'committed', 'revision': 2}))
    with pytest.raises(CredentialPolicyError):
        pool.select()
    fresh = load_policy(home)
    assert fresh is not None and 'REVOKED_KEY' in fresh.managed_environment
    assert managed_pool('openrouter', fresh).select() is None


@pytest.mark.parametrize('provider', f.PROVIDERS)
def test_positive_shared_reference_fresh_consumers_use_one_rotation(tmp_path, provider):
    root = f.fixture(tmp_path, provider)
    owner = root / 'auth.json'
    before_other = json.loads(owner.read_text())['credential_pool'][provider][1]
    alias = root / 'profiles' / 'two' / 'auth.json'
    alias.symlink_to(owner)
    policy = root / 'profiles' / 'two' / 'assignments.json'
    data = json.loads(policy.read_text())
    data['accounts'][provider]['store'] = 'profile'
    policy.write_text(json.dumps(data))
    results = [f.finish(f.start(root, provider, profile=name)) for name in ['one', 'two', 'one']]
    print(json.dumps({'case': 'positive-root-and-canonical-reference', 'provider': provider, 'results': results, 'posts': len(f.calls(root))}))
    assert all(r == {'selected': True, 'fresh': True} for r in results)
    assert len(f.calls(root)) == 1
    assert alias.is_symlink()
    assert json.loads(owner.read_text())['credential_pool'][provider][1] == before_other


@pytest.mark.parametrize('argv', [['auth','policy-capabilities'], ['auth','status'], ['auth','policy-capabilities','--help']])
def test_only_literal_capability_query_bypasses_pending_policy(tmp_path, argv):
    home, policy, gate, data = publication_fixture(tmp_path)
    gate.write_text(json.dumps({'version': 1, 'state': 'publishing', 'revision': 2}))
    home.joinpath('auth.json').write_text('{synthetic-invalid-auth')
    repo = Path(__file__).resolve().parents[2]
    before = {p: p.read_bytes() for p in [policy, gate, home/'auth.json']}
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', *argv], cwd=repo,
        env={'HOME':str(tmp_path), 'HERMES_HOME':str(home), 'PATH':os.environ.get('PATH',''), 'PYTHONPATH':str(repo), 'PYTHONDONTWRITEBYTECODE':'1'},
        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=20)
    print(json.dumps({'argv': argv, 'exit':result.returncode}))
    if argv == ['auth','policy-capabilities']:
        assert result.returncode == 0
        assert json.loads(result.stdout)['credential_policy'] == 2
    else:
        assert result.returncode != 0, 'normal/help-bearing commands must still enforce policy'
    assert all(p.read_bytes() == b for p,b in before.items())
