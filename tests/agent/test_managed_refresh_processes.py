"""Portable real-process refresh tests. Provider transport is always synthetic."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / 'tests/fixtures/credential_refresh_worker.py'
PROVIDERS = ['anthropic', 'openai-codex', 'xai-oauth']


def fixture(tmp_path, provider):
    root = tmp_path / 'root'
    for name in ['one', 'two']:
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('credential_policy:\n  file: assignments.json\n')
        (home / 'assignments.json').write_text(json.dumps({'version': 1, 'environment': {}, 'managed_environment': [],
            'accounts': {provider: {'store': 'root', 'ids': ['shared']}}}))
    exp = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=')
    row = {'id': 'shared', 'label': 'synthetic', 'source': 'manual:hermes_pkce' if provider == 'anthropic' else 'manual:device_code',
        'auth_type': 'oauth', 'priority': 0, 'access_token': f'fixture.{exp}.fixture', 'refresh_token': 'synthetic-old',
        'expires_at_ms': 1, 'last_refresh': '2000-01-01T00:00:00Z'}
    (root / 'auth.json').write_text(json.dumps({'credential_pool': {provider: [row, {**row, 'id': 'unassigned'}]}}))
    return root


def start(root, provider, profile='one', mode='normal', gate='-'):
    env = {k: v for k, v in os.environ.items() if k in ['PATH', 'LANG', 'SYSTEMROOT']}
    env.update(HOME=str(root.parent), HERMES_HOME=str(root / 'profiles' / profile),
               PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.Popen([sys.executable, str(WORKER), str(root), profile, provider, mode, str(gate)],
        cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(process):
    try:
        out, err = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    assert process.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def calls(root):
    return (root / 'calls').read_text().splitlines() if (root / 'calls').exists() else []


@pytest.mark.parametrize('provider', PROVIDERS)
def test_processes_share_one_rotation_and_preserve_other_rows(tmp_path, provider):
    root = fixture(tmp_path, provider)
    before = json.loads((root / 'auth.json').read_text())['credential_pool'][provider][1]
    gate = root / 'gate'
    processes = [start(root, provider, profile=name, gate=gate) for name in ['one', 'two']]
    try:
        deadline = time.monotonic() + 15
        while not all((root / ('ready-' + name)).exists() for name in ['one', 'two']):
            assert time.monotonic() < deadline
            time.sleep(.01)
        gate.touch()
        results = [finish(p) for p in processes]
        assert all(r == {'selected': True, 'fresh': True} for r in results)
        assert len(calls(root)) == 1
        assert json.loads((root / 'auth.json').read_text())['credential_pool'][provider][1] == before
        assert not any((root / 'profiles' / name / 'auth.json').exists() for name in ['one', 'two'])
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_post_rotation_disk_failure_is_not_replayed_by_fresh_process(tmp_path, provider):
    root = fixture(tmp_path, provider)
    assert finish(start(root, provider, mode='fail-save'))['selected'] is False
    assert len(calls(root)) == 1
    assert finish(start(root, provider, profile='two'))['selected'] is False
    assert len(calls(root)) == 1, 'fresh process replayed a consumed grant'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_crash_after_post_survives_restart_without_replay(tmp_path, provider):
    root = fixture(tmp_path, provider)
    process = start(root, provider, mode='crash-after-post')
    process.communicate(timeout=25)
    assert process.returncode == 23
    assert len(calls(root)) == 1
    assert finish(start(root, provider, profile='two'))['selected'] is False
    assert len(calls(root)) == 1
    intent = (root / 'auth.json.rotation-intent.json').read_text()
    assert 'synthetic-old' not in intent and 'synthetic-next' not in intent
    assert (root / 'auth.json.rotation-intent.json').stat().st_mode & 0o777 == 0o600
    # An owner's fresh generation, not deletion of the intent, is recovery.
    store = json.loads((root / 'auth.json').read_text())
    store['credential_pool'][provider][0]['refresh_token'] = 'synthetic-owner-reauth'
    (root / 'auth.json').write_text(json.dumps(store))
    assert finish(start(root, provider))['fresh'] is True
    assert len(calls(root)) == 2


@pytest.mark.parametrize('provider', PROVIDERS)
def test_ambiguous_outage_requires_new_owner_generation_not_cooldown(tmp_path, provider):
    root = fixture(tmp_path, provider)
    finish(start(root, provider, mode='outage'))
    assert len(calls(root)) == 1
    assert json.loads((root / 'auth.json.rotation-intent.json').read_text())
    # Advance the fixture's persisted cooldown, without an administrative reset.
    store = json.loads((root / 'auth.json').read_text())
    for row in store['credential_pool'][provider]:
        row['last_status_at'] = 1
    (root / 'auth.json').write_text(json.dumps(store))
    assert finish(start(root, provider, profile='two'))['selected'] is False
    assert len(calls(root)) == 1
    store['credential_pool'][provider][0]['refresh_token'] = 'synthetic-owner-replacement'
    (root / 'auth.json').write_text(json.dumps(store))
    assert finish(start(root, provider, profile='two'))['fresh'] is True
    assert len(calls(root)) == 2
    records = json.loads((root / 'auth.json.rotation-intent.json').read_text())
    assert any(row['state'] == 'pending' for row in records.values())
    assert any(row['state'] == 'committed' for row in records.values())


@pytest.mark.parametrize('provider', PROVIDERS)
def test_uncertain_timeout_is_not_retried_in_process_or_after_restart(tmp_path, provider):
    root = fixture(tmp_path, provider)
    assert finish(start(root, provider, mode='timeout'))['selected'] is False
    assert len(calls(root)) == 1
    assert finish(start(root, provider, profile='two'))['selected'] is False
    assert len(calls(root)) == 1


@pytest.mark.parametrize('provider', PROVIDERS)
def test_revocation_is_observed_by_fresh_consumer_without_refresh(tmp_path, provider):
    root = fixture(tmp_path, provider)
    manifest = root / 'profiles/two/assignments.json'
    data = json.loads(manifest.read_text())
    data['accounts'] = {}
    manifest.write_text(json.dumps(data))
    assert finish(start(root, provider, profile='two'))['selected'] is False
    assert calls(root) == []


@pytest.mark.parametrize('provider', PROVIDERS)
def test_unwritable_preflight_makes_no_provider_call(tmp_path, provider):
    root = fixture(tmp_path, provider)
    result = finish(start(root, provider, mode='preflight-failure'))
    assert result['selected'] is False
    assert calls(root) == []
