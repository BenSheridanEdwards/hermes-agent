"""Exercise explicit Codex sharing through real stores, locks and pool methods."""
import base64
import json
import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import auth
from agent.credential_pool import CredentialPool, PooledCredential, load_pool


def access_token(expiry):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJub25lIn0.{payload}.synthetic'


def entry(identifier='personal', expired=False):
    return {'id': identifier, 'label': identifier, 'source': 'manual:device_code',
            'auth_type': 'oauth', 'priority': 0, 'access_token': access_token(time.time() + (-60 if expired else 3600)),
            'refresh_token': f'synthetic-refresh-{identifier}', 'last_status': 'ok'}


def write_store(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'credential_pool': {'openai-codex': entries}}))


def configure(profile, root):
    profile.mkdir(parents=True, exist_ok=True)
    profile.joinpath('config.yaml').write_text(json.dumps({'oauth': {
        'refresh_owner': 'runtime', 'shared_codex_auth_path': str(root)}}))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'shared' / 'auth.json'
    profile = tmp_path / 'independent-agent' / 'profile'
    write_store(root, [entry(), entry('work')])
    configure(profile, root)
    monkeypatch.setenv('HERMES_HOME', str(profile))
    return root, profile


def test_health_updates_stay_in_root_and_other_provider_stays_local(stores):
    root, profile = stores
    profile_auth = profile / 'auth.json'
    profile_auth.write_text(json.dumps({'providers': {'xai-oauth': {'unchanged': True}}}))
    before = profile_auth.read_bytes()
    pool = load_pool('openai-codex')
    pool._mark_exhausted(pool.entries()[0], 429)
    assert profile_auth.read_bytes() == before
    assert json.loads(root.read_text())['credential_pool']['openai-codex'][0]['last_status'] == 'exhausted'
    assert auth.shared_codex_auth_path('xai-oauth') is None


def test_explicit_shared_pool_ignores_profile_shadows_without_deleting_them(stores):
    root, profile = stores
    write_store(profile / 'auth.json', [entry('local')])
    before = (profile / 'auth.json').read_bytes()
    assert {row.id for row in load_pool('openai-codex').entries()} == {'personal', 'work'}
    assert {row['id'] for row in auth.read_credential_pool()['openai-codex']} == {'personal', 'work'}
    assert (profile / 'auth.json').read_bytes() == before


@pytest.mark.parametrize('contents', [None, '{bad', '[]', '{}', '{"credential_pool":{"openai-codex":[]}}'])
def test_missing_or_malformed_shared_store_never_falls_back_or_overwrites(stores, contents):
    root, profile = stores
    write_store(profile / 'auth.json', [entry('local')])
    if contents is None:
        root.unlink()
    else:
        root.write_text(contents)
    for operation in [lambda: load_pool('openai-codex'), lambda: auth.write_credential_pool('openai-codex', [entry()])]:
        with pytest.raises(auth.AuthError, match='missing or invalid'):
            operation()
    assert not root.exists() if contents is None else root.read_text() == contents
    assert not root.with_suffix('.json.corrupt').exists()


@pytest.mark.parametrize('setting', ['', 'relative/auth.json', 12, []])
def test_invalid_setting_fails_closed(stores, setting):
    root, profile = stores
    (profile / 'config.yaml').write_text(json.dumps({'oauth': {'shared_codex_auth_path': setting}}))
    with pytest.raises(auth.AuthError):
        load_pool('openai-codex')


def test_unconfigured_tenant_keeps_its_own_pool(stores, tmp_path, monkeypatch):
    root, profile = stores
    tenant = tmp_path / 'tenant'
    write_store(tenant / 'auth.json', [entry('tenant')])
    monkeypatch.setenv('HERMES_HOME', str(tenant))
    assert auth.shared_codex_auth_path('openai-codex') is None
    assert [row['id'] for row in auth.read_credential_pool('openai-codex')] == ['tenant']


def test_refresh_updates_one_account_and_stale_health_cannot_restore_old_tokens(stores):
    root, profile = stores
    pool = load_pool('openai-codex')
    stale = CredentialPool('openai-codex', pool.entries())
    previous_work = json.loads(root.read_text())['credential_pool']['openai-codex'][1]
    with patch.object(auth, 'refresh_codex_oauth_pure', return_value={
        'access_token': access_token(time.time() + 7200), 'refresh_token': 'synthetic-rotated'}):
        refreshed = pool._refresh_entry(pool.entries()[0], force=True)
    assert refreshed is not None
    stale._mark_exhausted(stale.entries()[0], 429)
    rows = json.loads(root.read_text())['credential_pool']['openai-codex']
    assert rows[0]['refresh_token'] == 'synthetic-rotated'
    assert rows[0]['last_status'] == 'ok'
    assert all(rows[1][field] == value for field, value in previous_work.items())
    assert not (profile / 'auth.json').exists()
    assert not list(root.parent.glob('*.refresh-pending'))


def test_uncertain_network_result_cannot_replay_refresh(stores):
    root, profile = stores
    pool = load_pool('openai-codex')
    with patch.object(auth, 'refresh_codex_oauth_pure', side_effect=TimeoutError('synthetic timeout')) as refresh:
        assert pool._refresh_entry(pool.entries()[0], force=True) is None
        reloaded = load_pool('openai-codex')
        assert reloaded._refresh_entry(reloaded.entries()[0], force=True) is None
        assert refresh.call_count == 1
    assert list(root.parent.glob('*.refresh-pending'))


def test_failed_commit_does_not_return_rotated_pair_or_replay_it(stores):
    root, profile = stores
    pool = load_pool('openai-codex')
    with patch.object(auth, 'refresh_codex_oauth_pure', return_value={
        'access_token': access_token(time.time() + 7200), 'refresh_token': 'synthetic-rotated'}), \
         patch.object(auth, '_save_auth_store', side_effect=OSError('synthetic disk failure')):
        with pytest.raises(OSError):
            pool._refresh_entry(pool.entries()[0], force=True)
    reloaded = load_pool('openai-codex')
    with patch.object(auth, 'refresh_codex_oauth_pure') as refresh:
        assert reloaded._refresh_entry(reloaded.entries()[0], force=True) is None
        refresh.assert_not_called()
    assert json.loads(root.read_text())['credential_pool']['openai-codex'][0]['refresh_token'] != 'synthetic-rotated'


def refresh_worker(profile, ready, start, results, calls):
    os.environ['HERMES_HOME'] = profile
    pool = load_pool('openai-codex')
    stale = pool.entries()[0]
    ready.put(True)
    start.wait(20)
    def rotate(access, refresh):
        with open(calls, 'a') as output:
            output.write('refresh\n')
        time.sleep(0.1)
        return {'access_token': access_token(time.time() + 7200), 'refresh_token': 'synthetic-rotated'}
    with patch.object(auth, 'refresh_codex_oauth_pure', side_effect=rotate):
        result = pool._refresh_entry(stale, force=False)
    results.put(result.refresh_token if result else None)


def test_two_profile_processes_refresh_once_and_share_result(stores, tmp_path):
    root, profile = stores
    write_store(root, [entry(expired=True), entry('work')])
    second_profile = tmp_path / 'second-agent'
    configure(second_profile, root)
    context = multiprocessing.get_context('spawn')
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    calls = tmp_path / 'calls.txt'
    workers = [context.Process(target=refresh_worker, args=(str(path), ready, start, results, str(calls)))
               for path in (profile, second_profile)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20)
        start.set()
        assert [results.get(timeout=20) for _ in workers] == ['synthetic-rotated'] * 2
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert calls.read_text().splitlines() == ['refresh']
        assert not (profile / 'auth.json').exists()
        assert not (second_profile / 'auth.json').exists()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def test_runtime_and_auxiliary_readers_cannot_use_local_singleton(stores):
    root, profile = stores
    local = entry('local')
    (profile / 'auth.json').write_text(json.dumps({'providers': {'openai-codex': {'tokens': local}}}))
    before = (profile / 'auth.json').read_bytes()
    expected = auth.read_credential_pool('openai-codex')[0]['access_token']
    assert auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)['api_key'] == expected
    assert auth._read_codex_tokens()['tokens']['access_token'] == expected
    assert auth._pool_codex_access_token() == expected
    root.unlink()
    for operation in [lambda: auth.resolve_codex_runtime_credentials(refresh_if_expiring=False),
                      auth._read_codex_tokens, auth._pool_codex_access_token]:
        with pytest.raises(auth.AuthError):
            operation()
    assert (profile / 'auth.json').read_bytes() == before


def test_malformed_configuration_cannot_select_local_shadow(stores):
    root, profile = stores
    write_store(profile / 'auth.json', [entry('local')])
    (profile / 'config.yaml').write_text('oauth: [broken')
    with pytest.raises(auth.AuthError):
        auth.read_credential_pool('openai-codex')


def test_interactive_replacement_updates_only_authorized_account(stores):
    root, profile = stores
    entries = auth.read_credential_pool('openai-codex')
    prior_work = dict(entries[1])
    entries[0]['refresh_token'] = 'synthetic-new-login'
    entries[1]['refresh_token'] = 'synthetic-stale-work'
    auth.write_credential_pool('openai-codex', entries, oauth_token_write_authority='interactive-login',
                               authorized_oauth_entry_ids=['personal'])
    actual = auth.read_credential_pool('openai-codex')
    assert actual[0]['refresh_token'] == 'synthetic-new-login'
    assert actual[1]['refresh_token'] == prior_work['refresh_token']


def test_stale_process_refreshing_other_account_preserves_prior_rotation(stores):
    root, profile = stores
    first, second = load_pool('openai-codex'), load_pool('openai-codex')
    with patch.object(auth, 'refresh_codex_oauth_pure', side_effect=[
        {'access_token': access_token(time.time()+7200), 'refresh_token': 'synthetic-personal-new'},
        {'access_token': access_token(time.time()+7200), 'refresh_token': 'synthetic-work-new'},
    ]):
        first._refresh_entry(first.entries()[0], force=True)
        second._refresh_entry(second.entries()[1], force=True)
    assert [row['refresh_token'] for row in auth.read_credential_pool('openai-codex')] == [
        'synthetic-personal-new', 'synthetic-work-new']


def test_reactive_waiter_adopts_rotation_without_second_post(stores):
    root, profile = stores
    first, second = load_pool('openai-codex'), load_pool('openai-codex')
    with patch.object(auth, 'refresh_codex_oauth_pure', return_value={
        'access_token': access_token(time.time()+7200), 'refresh_token': 'synthetic-rotated'}) as refresh:
        first._refresh_entry(first.entries()[0], force=True)
        result = second._refresh_entry(second.entries()[0], force=True)
        assert result.refresh_token == 'synthetic-rotated'
        assert refresh.call_count == 1


def test_singleton_write_and_refresh_refuse_shared_mode(stores):
    root, profile = stores
    with patch.object(auth, 'refresh_codex_oauth_pure') as refresh:
        with pytest.raises(auth.AuthError):
            auth._refresh_codex_auth_tokens({}, 10)
        with pytest.raises(auth.AuthError):
            auth._save_codex_tokens({})
        refresh.assert_not_called()
    assert not (profile / 'auth.json').exists()


def test_shared_dead_account_is_preserved_for_reauthentication(stores):
    root, profile = stores
    dead = entry()
    dead.update(last_status='dead', last_status_at=time.time() - 172800)
    write_store(root, [dead])
    before = root.read_bytes()
    assert load_pool('openai-codex').select() is None
    assert root.read_bytes() == before
    assert len(load_pool('openai-codex').entries()) == 1


@pytest.mark.parametrize('disable_sharing', [False, True])
def test_config_change_during_refresh_keeps_original_authority(stores, tmp_path, disable_sharing):
    root, profile = stores
    other = tmp_path / 'other' / 'auth.json'
    write_store(other, [entry()])
    other_before = other.read_bytes()
    pool = load_pool('openai-codex')

    def rotate(access, refresh):
        if disable_sharing:
            (profile / 'config.yaml').write_text('{}')
        else:
            configure(profile, other)
        return {'access_token': access_token(time.time() + 7200),
                'refresh_token': 'synthetic-rotated'}

    with patch.object(auth, 'refresh_codex_oauth_pure', side_effect=rotate):
        result = pool._refresh_entry(pool.entries()[0], force=True)
    assert result.refresh_token == 'synthetic-rotated'
    assert json.loads(root.read_text())['credential_pool']['openai-codex'][0]['refresh_token'] == 'synthetic-rotated'
    assert other.read_bytes() == other_before
    assert not (profile / 'auth.json').exists()
    assert not list(root.parent.glob('*.refresh-pending'))
    assert auth.shared_codex_auth_path('openai-codex') == (None if disable_sharing else other)


def test_removing_last_shared_account_cannot_corrupt_authority(stores):
    root, profile = stores
    write_store(root, [entry()])
    before = root.read_bytes()
    with pytest.raises(auth.AuthError, match='last shared Codex account'):
        auth.write_credential_pool('openai-codex', [], removed_ids=['personal'])
    assert root.read_bytes() == before
    assert len(load_pool('openai-codex').entries()) == 1


def test_owner_change_during_refresh_cannot_discard_rotation(stores):
    root, profile = stores
    pool = load_pool('openai-codex')

    def rotate(access, refresh):
        config = json.loads((profile / 'config.yaml').read_text())
        config['oauth']['refresh_owner'] = 'external'
        (profile / 'config.yaml').write_text(json.dumps(config))
        return {'access_token': access_token(time.time() + 7200),
                'refresh_token': 'synthetic-rotated'}

    with patch.object(auth, 'refresh_codex_oauth_pure', side_effect=rotate):
        result = pool._refresh_entry(pool.entries()[0], force=True)
    assert result.refresh_token == 'synthetic-rotated'
    assert json.loads(root.read_text())['credential_pool']['openai-codex'][0]['refresh_token'] == 'synthetic-rotated'
    assert not list(root.parent.glob('*.refresh-pending'))
    assert not auth.runtime_owns_oauth_refresh('openai-codex')


def test_explicit_external_writer_preserves_existing_authority_contract(stores):
    root, profile = stores
    config = json.loads((profile / 'config.yaml').read_text())
    config['oauth']['refresh_owner'] = 'external'
    (profile / 'config.yaml').write_text(json.dumps(config))
    rows = auth.read_credential_pool('openai-codex')
    rows[0]['access_token'] = access_token(time.time() + 7200)
    rows[0]['refresh_token'] = 'synthetic-scheduler-rotated'
    auth.write_credential_pool('openai-codex', rows, oauth_token_write_authority='external-scheduler')
    assert json.loads(root.read_text())['credential_pool']['openai-codex'][0]['refresh_token'] == 'synthetic-scheduler-rotated'
    assert not (profile / 'auth.json').exists()
