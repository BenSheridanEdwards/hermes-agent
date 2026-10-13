"""Real Desktop router contracts under synthetic assignment authority.

No live provider traffic, no alternate server sessions, no production stores.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    import hermes_constants
    from hermes_cli.web_routers import config_env, oauth, ops
    from hermes_cli import env_loader
    root = tmp_path / 'root'
    home = root / 'profiles' / 'selected'
    home.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: root)
    token = set_hermes_home_override(home)
    manifest = {'version': 1, 'environment': {'OPENROUTER_API_KEY': 'fixture_vault'},
                'managed_environment': ['OPENROUTER_API_KEY'],
                'accounts': {'openai-codex': {'store': 'root', 'ids': ['assigned']}}}
    (home / 'assignments.json').write_text(json.dumps(manifest))
    (home / 'config.yaml').write_text('credential_policy:\n  file: assignments.json\nsecrets:\n  fixture_vault:\n    enabled: true\n')
    (home / '.env').write_text('OPENROUTER_API_KEY=synthetic-stale\n')
    rows = [{'id': name, 'source': 'manual:device_code', 'label': name, 'priority': i,
             'auth_type': 'oauth', 'access_token': 'synthetic-' + name,
             'refresh_token': 'synthetic-refresh-' + name} for i, name in enumerate(['assigned', 'other'])]
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': rows}}))
    from agent.secret_sources.base import SecretSource, FetchResult
    from agent.secret_sources.registry import register_source
    class Vault(SecretSource):
        name = 'fixture_vault'
        label = 'Fixture'
        shape = 'mapped'
        override_existing_default = True
        def fetch(self, cfg, home_path):
            return FetchResult(secrets={'OPENROUTER_API_KEY': 'synthetic-assigned'})
    env_loader.reset_secret_source_cache()
    register_source(Vault(), replace=True, scope=str(home.resolve()))
    env_loader.load_hermes_dotenv()
    app = FastAPI()
    for router in (config_env.router, oauth.router, ops.router):
        app.include_router(router)
    # HTTP authentication is not under test; retain real profile scoping,
    # real policy loading and real credential storage/lifecycle boundaries.
    monkeypatch.setattr(oauth, '_require_token', lambda request: None)
    with TestClient(app) as client:
        yield client, root, home, manifest
    env_loader.reset_secret_source_cache()
    reset_hermes_home_override(token)


def test_keys_report_effective_assignment_not_stale_dotenv(managed):
    client, root, home, manifest = managed
    row = client.get('/api/env').json()['OPENROUTER_API_KEY']
    assert row['authority'] == 'assignment'
    assert row['source'] == 'fixture_vault'
    assert row['is_set'] is True
    assert row['editable'] is False
    assert row['redacted_value'] is None
    manifest['environment'] = {}
    (home / 'assignments.json').write_text(json.dumps(manifest))
    row = client.get('/api/env').json()['OPENROUTER_API_KEY']
    assert row['is_set'] is False


@pytest.mark.parametrize('key', ['OPENROUTER_API_KEY', 'OPENAI_API_KEY'])
def test_managed_key_save_is_refused_without_writes_or_auto_grants(managed, key):
    client, root, home, manifest = managed
    before = {p: p.read_bytes() for p in [root / 'auth.json', home / '.env', home / 'assignments.json']}
    response = client.put('/api/env', json={'key': key, 'value': 'synthetic-new'})
    assert response.status_code == 409
    assert 'credential manager' in response.json()['detail']
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not (home / 'auth.json').exists()


def test_managed_oauth_start_refuses_before_any_login(managed, monkeypatch):
    from hermes_cli.web_routers import oauth
    client, root, home, manifest = managed
    async def forbidden(*args, **kwargs):
        pytest.fail('consumer attempted a provider login')
    monkeypatch.setattr(oauth, '_start_device_code_flow', forbidden)
    response = client.post('/api/providers/oauth/openai-codex/start')
    assert response.status_code == 409
    assert 'credential manager' in response.json()['detail']


def test_oauth_status_reports_only_bound_rows_without_singleton_probe(managed, monkeypatch):
    from hermes_cli import auth
    client, root, home, manifest = managed
    monkeypatch.setattr(auth, 'get_codex_auth_status', lambda: pytest.fail('unscoped singleton probe'))
    providers = client.get('/api/providers/oauth').json()['providers']
    row = next(p for p in providers if p['id'] == 'openai-codex')
    assert row['status']['entry_ids'] == ['assigned']
    assert row['disconnectable'] is False
    assert row['status']['token_preview'] is None
    manifest['accounts'] = {}
    (home / 'assignments.json').write_text(json.dumps(manifest))
    row = next(p for p in client.get('/api/providers/oauth').json()['providers'] if p['id'] == 'openai-codex')
    assert row['status']['logged_in'] is False


def test_pool_lists_env_assignment_and_refuses_admin_mutations(managed):
    client, root, home, manifest = managed
    data = client.get('/api/credentials/pool').json()
    providers = {p['provider']: p for p in data['providers']}
    assert 'openrouter' in providers
    assert providers['openai-codex']['editable'] is False
    before = (root / 'auth.json').read_bytes()
    assert client.post('/api/credentials/pool', json={'provider': 'openai-codex', 'api_key': 'synthetic'}).status_code == 409
    assert client.delete('/api/credentials/pool/openai-codex/1').status_code == 409
    assert client.delete('/api/providers/oauth/openai-codex').status_code == 409
    assert (root / 'auth.json').read_bytes() == before
    assert not (home / 'auth.json').exists()


def test_pending_login_cannot_save_after_profile_becomes_managed(managed, monkeypatch):
    from hermes_cli.web_routers import oauth
    client, root, home, manifest = managed
    managed_config = (home / 'config.yaml').read_text()
    (home / 'config.yaml').write_text('{}')
    sid, session = oauth._new_oauth_session('openai-codex', 'device_code')
    monkeypatch.setattr(oauth, '_codex_request_user_code', lambda _: {'user_code': 'fixture', 'device_auth_id': 'fixture', 'interval': 3})
    monkeypatch.setattr(oauth, '_codex_poll_authorization', lambda *a: {})
    def exchange(*args):
        (home / 'config.yaml').write_text(managed_config)
        return {'access_token': 'synthetic-new', 'refresh_token': 'synthetic-new-refresh'}
    monkeypatch.setattr(oauth, '_codex_exchange_tokens', exchange)
    before = (root / 'auth.json').read_bytes()
    oauth._codex_full_login_worker(sid)
    assert session['status'] == 'error'
    assert not (home / 'auth.json').exists()
    assert (root / 'auth.json').read_bytes() == before
    oauth._oauth_sessions.pop(sid, None)


def test_unmanaged_local_save_still_works(managed):
    client, root, home, manifest = managed
    (home / 'config.yaml').write_text('{}')
    assert client.put('/api/env', json={'key': 'CUSTOM_TEST_KEY', 'value': 'synthetic-local'}).status_code == 200
    assert 'CUSTOM_TEST_KEY=synthetic-local' in (home / '.env').read_text()
