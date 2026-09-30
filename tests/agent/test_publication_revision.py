import json
import pytest
from agent.credential_policy import CredentialPolicyError, load_policy


def setup(tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    policy = home / 'assignments.json'
    gate = tmp_path / 'authority-publication.json'
    home.joinpath('config.yaml').write_text(f'credential_policy:\n  file: {policy}\n', encoding='utf-8')
    data = {'version': 1, 'environment': {}, 'managed_environment': ['REVOKED_KEY'], 'accounts': {},
            'publication': {'file': str(gate), 'revision': 1}}
    policy.write_text(json.dumps(data), encoding='utf-8')
    gate.write_text(json.dumps({'version': 1, 'revision': 1, 'state': 'committed'}), encoding='utf-8')
    return home, policy, gate, data


@pytest.mark.parametrize('state', [None, [], {'version': 1, 'revision': 1, 'state': 'publishing'},
    {'version': 1, 'revision': 2, 'state': 'committed'}])
def test_publication_stages_never_fall_back_to_an_older_grant(tmp_path, state):
    home, policy, gate, data = setup(tmp_path)
    assert load_policy(home) is not None
    if state is None:
        gate.unlink()
    else:
        gate.write_text(json.dumps(state), encoding='utf-8')
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    assert json.loads(policy.read_text(encoding='utf-8')) == data


def test_capabilities_are_available_during_incomplete_publication(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'one'
    home.mkdir(parents=True)
    assignment = home / 'assignments.json'
    publication = root / 'authority-publication.json'
    (home / 'config.yaml').write_text(f'credential_policy:\n  file: {assignment}\n', encoding='utf-8')
    assignment.write_text(json.dumps({'version': 2, 'profile': 'one', 'environment': {}, 'accounts': {}, 'managed_environment': [], 'publication': {'file': str(publication), 'revision': 1}}), encoding='utf-8')
    publication.write_text(json.dumps({'version': 1, 'revision': 1, 'state': 'publishing'}), encoding='utf-8')
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', '--profile', 'one', 'auth', 'policy-capabilities'], cwd=repo,
        env={'HOME': str(tmp_path), 'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(repo), 'PYTHONDONTWRITEBYTECODE': '1'},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['credential_policy'] == 2


def test_revision_two_requires_coherent_republication_and_retains_revocations(tmp_path):
    home, policy, gate, data = setup(tmp_path)
    original = load_policy(home)
    gate.write_text(json.dumps({'version': 1, 'revision': 2, 'state': 'publishing'}), encoding='utf-8')
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    data['version'] = 2
    data['publication']['revision'] = 2
    policy.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    gate.write_text(json.dumps({'version': 1, 'revision': 2, 'state': 'committed'}), encoding='utf-8')
    current = load_policy(home)
    assert original is not None and current is not None
    assert current.revision != original.revision
    assert 'REVOKED_KEY' in current.managed_environment
