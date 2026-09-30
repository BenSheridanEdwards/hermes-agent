import json
import pytest


@pytest.mark.parametrize('value', [None, [], {'credential_pool': []}, {'providers': []},
    {'providers': {'openai-codex': None}}, {'credential_pool': {'openai-codex': {}}},
    {'credential_pool': {'openai-codex': [None]}},
    {'credential_pool': {'openai-codex': [{'id': 2}]}},
    {'credential_pool': {'openai-codex': [{'id': 'same'}, {'id': 'same'}]}},
    {'credential_pool': {'openai-codex': [{'id': 'one', 'expires_at': 'not-a-date'}]}}])
def test_present_invalid_owner_store_is_not_empty_or_mutated(tmp_path, value):
    from hermes_cli.auth import _load_auth_store
    from agent.credential_policy import CredentialPolicyError
    path = tmp_path / 'auth.json'
    before = json.dumps(value)
    path.write_text(before, encoding='utf-8')
    with pytest.raises(CredentialPolicyError, match='Credential store is invalid or unreadable'):
        _load_auth_store(path)
    assert path.read_text(encoding='utf-8') == before


def test_invalid_provider_response_cannot_replace_a_valid_owner_store(tmp_path):
    from hermes_cli import auth
    from agent.credential_policy import CredentialPolicyError
    path = tmp_path / 'auth.json'
    original = b'{"providers": {}}'
    path.write_bytes(original)
    with pytest.raises(CredentialPolicyError, match='Credential store is invalid or unreadable'):
        auth._save_auth_store({'providers': {'openai-codex': {'tokens': {'access_token': 123}}}}, path)
    assert path.read_bytes() == original


def test_invalid_json_is_not_copied_or_replaced(tmp_path):
    from hermes_cli.auth import _load_auth_store
    from agent.credential_policy import CredentialPolicyError
    path = tmp_path / 'auth.json'
    path.write_text('{synthetic-invalid', encoding='utf-8')
    before = set(tmp_path.iterdir())
    with pytest.raises(CredentialPolicyError, match='Credential store is invalid or unreadable'):
        _load_auth_store(path)
    assert path.read_text(encoding='utf-8') == '{synthetic-invalid'
    assert set(tmp_path.iterdir()) == before
