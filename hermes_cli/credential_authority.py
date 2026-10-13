"""Assignment-aware, refresh-free presentation and mutation boundary.

Consumers may use assigned credentials, not grant/revoke/change their authority.
Unmanaged profiles retain the ordinary local credential lifecycle.
"""
from agent.credential_policy import CredentialPolicyError, assigned_env_value, load_policy


def providers_for_environment(name):
    from hermes_cli.provider_catalog import provider_catalog
    return [d.slug for d in provider_catalog() if name in d.api_key_env_vars]


def environment_is_managed(policy, name):
    if policy is None:
        return False
    return name in policy.managed_environment or bool(providers_for_environment(name))


def require_local_credential_authority(env_var=None):
    policy = load_policy()
    if policy is not None and (env_var is None or environment_is_managed(policy, env_var)):
        raise CredentialPolicyError('Assigned credentials are read-only here; use your credential manager')


def environment_status(policy, name):
    """None means ordinary local behavior; managed rows never reveal token fragments."""
    if not environment_is_managed(policy, name):
        return None
    try:
        delivered = bool(assigned_env_value(name))
        state = 'delivered' if delivered else 'not-delivered-or-revoked'
    except CredentialPolicyError:
        delivered, state = False, 'reload-required'
    return {'is_set': delivered, 'redacted_value': None, 'authority': 'assignment',
            'source': policy.environment.get(name), 'editable': False, 'assignment_state': state}


def require_web_credential_authority(env_var=None):
    from fastapi import HTTPException
    try:
        require_local_credential_authority(env_var)
    except CredentialPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc


def assigned_oauth_status(provider):
    policy = load_policy()
    if policy is None:
        return None
    # No singleton probing, load_pool/select or provider transport: status must
    # never refresh merely because a preferences pane was opened.
    rows = policy.rows(provider)
    return {'logged_in': False, 'credential_present': any(bool(r.get('access_token')) for r in rows),
            'authority': 'assignment', 'source': 'credential-manager',
            'source_label': 'Credential manager (live authentication not probed)',
            'entry_ids': [r['id'] for r in rows], 'token_preview': None,
            'has_refresh_token': any(bool(r.get('refresh_token')) for r in rows),
            'expires_at': None, 'state': 'assigned-unverified' if rows else 'not-assigned-or-removed'}
