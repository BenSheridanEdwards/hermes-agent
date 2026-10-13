"""Pool administrative operations must not invent changes to assigned authority."""
import json
import pytest


@pytest.mark.parametrize('operation', ['add', 'remove', 'move', 'reset', 'reset_all'])
@pytest.mark.parametrize('binding', ['root', 'profile', 'environment'])
def test_assigned_pool_admin_refuses_before_memory_or_disk_mutation(tmp_path, monkeypatch, operation, binding):
    import hermes_constants
    from agent.credential_pool import load_pool, PooledCredential
    from agent.credential_policy import CredentialPolicyError
    root = tmp_path / 'root'
    home = root / 'profiles' / 'one'
    home.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: root)
    manifest = {'version': 1, 'environment': {}, 'managed_environment': [],
                'accounts': {'openai-codex': {'store': binding, 'ids': ['one']}} if binding != 'environment' else {}}
    (home / 'assignments.json').write_text(json.dumps(manifest))
    (home / 'config.yaml').write_text('credential_policy:\n  file: assignments.json\n')
    row = {'id': 'one', 'label': 'one', 'source': 'manual', 'auth_type': 'api_key', 'priority': 0,
           'access_token': 'synthetic', 'last_status': 'exhausted'}
    store = (root if binding == 'root' else home) / 'auth.json'
    store.write_text(json.dumps({'credential_pool': {'openai-codex': [row]}}))
    pool = load_pool('openai-codex')
    before = store.read_bytes()
    before_entries = [e.to_dict() for e in pool.entries()]
    actions = {'add': lambda: pool.add_entry(PooledCredential.from_dict('openai-codex', {**row, 'id': 'new'})),
               'remove': lambda: pool.remove_index(1), 'move': lambda: pool.move_entry('one', 2),
               'reset': lambda: pool.reset_status('one'), 'reset_all': pool.reset_statuses}
    with pytest.raises(CredentialPolicyError, match='credential manager'):
        actions[operation]()
    assert store.read_bytes() == before
    assert [e.to_dict() for e in pool.entries()] == before_entries
    assert [e.to_dict() for e in load_pool('openai-codex').entries()] == before_entries
