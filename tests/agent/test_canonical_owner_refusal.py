"""Resolve copied-store reviewer cases by refusal, not implicit owner election."""
import json
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('canonical_process_fixture', Path(__file__).with_name('test_managed_refresh_processes.py'))
assert spec is not None and spec.loader is not None
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
fixture, start, finish, calls, PROVIDERS = f.fixture, f.start, f.finish, f.calls, f.PROVIDERS


@pytest.mark.parametrize('provider', PROVIDERS)
def test_independent_profile_store_remains_supported(tmp_path, provider):
    root = fixture(tmp_path, provider)
    home = root / 'profiles' / 'one'
    raw = json.loads((root / 'auth.json').read_text(encoding='utf-8'))
    row = dict(raw['credential_pool'][provider][0], refresh_token='synthetic-independent-grant')
    (home / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [row]}}), encoding='utf-8')
    policy = home / 'assignments.json'
    data = json.loads(policy.read_text(encoding='utf-8')); data['accounts'][provider]['store'] = 'profile'
    policy.write_text(json.dumps(data), encoding='utf-8')
    original_root = (root / 'auth.json').read_bytes()
    assert finish(start(root, provider))['selected']
    assert len(calls(root)) == 1
    assert (root / 'auth.json').read_bytes() == original_root


@pytest.mark.parametrize('provider', PROVIDERS)
def test_canonical_file_reference_is_not_turned_into_a_copy(tmp_path, provider):
    root = fixture(tmp_path, provider)
    home = root / 'profiles' / 'one'
    owner = root / 'auth.json'
    alias = home / 'auth.json'
    alias.symlink_to(owner)
    policy = home / 'assignments.json'
    data = json.loads(policy.read_text(encoding='utf-8')); data['accounts'][provider]['store'] = 'profile'
    policy.write_text(json.dumps(data), encoding='utf-8')
    before = json.loads(owner.read_text(encoding='utf-8'))['credential_pool'][provider][0]['access_token']
    assert finish(start(root, provider))['selected']
    assert len(calls(root)) == 1
    assert alias.is_symlink(), 'atomic save must target the canonical owner, not replace a reference'
    assert json.loads(owner.read_text(encoding='utf-8'))['credential_pool'][provider][0]['access_token'] != before


@pytest.mark.parametrize('provider', PROVIDERS)
def test_fenced_entry_does_not_disable_an_independent_authorized_account(tmp_path, provider):
    import hashlib
    import time
    root = fixture(tmp_path, provider)
    owner = root / 'auth.json'
    data = json.loads(owner.read_text(encoding='utf-8'))
    blocked = data['credential_pool'][provider][0]
    import base64
    deadline = time.time() + 86400  # Beyond xAI's documented hour-long proactive window.
    payload = base64.urlsafe_b64encode(json.dumps({'exp': deadline}).encode()).decode().rstrip('=')
    ready = dict(blocked, id='independent-ready', refresh_token='synthetic-independent',
                 access_token=f'fixture.{payload}.fixture', expires_at_ms=deadline * 1000, expires_at=deadline)
    data['credential_pool'][provider] = [blocked, ready]
    owner.write_text(json.dumps(data), encoding='utf-8')
    policy = root / 'profiles' / 'one' / 'assignments.json'
    manifest = json.loads(policy.read_text(encoding='utf-8'))
    manifest['accounts'][provider]['ids'].append('independent-ready')
    policy.write_text(json.dumps(manifest), encoding='utf-8')
    digest = hashlib.sha256(blocked['refresh_token'].encode()).hexdigest()
    owner.with_name('auth.json.rotation-intent.json').write_text(json.dumps({provider + ':shared': digest}), encoding='utf-8')
    result = finish(start(root, provider))
    assert result['selected'] and not result['fresh']
    assert calls(root) == [], 'the fenced predecessor must never be replayed'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_renamed_predecessor_copy_is_fenced_after_owner_rotates(tmp_path, provider):
    root = fixture(tmp_path, provider)
    owner_file = root / 'auth.json'
    data = json.loads(owner_file.read_text(encoding='utf-8'))
    original = dict(data['credential_pool'][provider][0])
    data['credential_pool'][provider] = [original]
    owner_file.write_text(json.dumps(data), encoding='utf-8')
    assert finish(start(root, provider))['selected']
    assert len(calls(root)) == 1
    home = root / 'profiles' / 'two'
    original['id'] = 'renamed-predecessor'
    copy = home / 'auth.json'
    copy.write_text(json.dumps({'credential_pool': {provider: [original]}}), encoding='utf-8')
    policy = home / 'assignments.json'
    manifest = json.loads(policy.read_text(encoding='utf-8'))
    manifest['accounts'][provider] = {'store': 'profile', 'ids': ['renamed-predecessor']}
    policy.write_text(json.dumps(manifest), encoding='utf-8')
    before = copy.read_bytes()
    assert not finish(start(root, provider, profile='two'))['selected']
    assert len(calls(root)) == 1
    assert copy.read_bytes() == before


@pytest.mark.parametrize('provider', PROVIDERS)
def test_known_copied_stores_refused_without_writes_or_owner_election(tmp_path, provider):
    root = fixture(tmp_path, provider)
    auth = json.loads((root / 'auth.json').read_text(encoding='utf-8'))
    auth['credential_pool'][provider][0]['id'] = 'renamed-copy'
    auth['credential_pool'][provider][0].pop('credential_fingerprint', None)
    paths = [root / 'auth.json']
    for slug in ('one', 'two'):
        home = root / 'profiles' / slug
        (home / 'auth.json').write_text(json.dumps(auth), encoding='utf-8')
        manifest_path = home / 'assignments.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['accounts'][provider] = {'store': 'profile', 'ids': ['renamed-copy']}
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        paths.extend([home / 'auth.json', manifest_path, home / 'config.yaml'])
    before = {p: p.read_bytes() for p in paths}
    a = start(root, provider, profile='one')
    b = start(root, provider, profile='two')
    assert finish(a) == {'selected': False, 'error_type': 'CredentialPolicyError'}
    assert finish(b) == {'selected': False, 'error_type': 'CredentialPolicyError'}
    assert calls(root) == []
    assert all(p.read_bytes() == content for p, content in before.items())
