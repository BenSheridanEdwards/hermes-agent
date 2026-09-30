"""Durable pre-POST intent for assigned, single-use refresh grants.

The caller holds the owner auth-store lock across begin -> transport -> token
commit -> finish. Only generation fingerprints are stored, never tokens. An
unresolved intent refuses replay after restart. A newly authorized generation
can replace that intent; deleting this file is not a recovery procedure.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from agent.credential_policy import CredentialPolicyError


def _read(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CredentialPolicyError('Refresh intent is unreadable; contact the credential manager') from exc
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
        raise CredentialPolicyError('Refresh intent is invalid; contact the credential manager')
    return data


def _write(path, data):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.rotation-intent-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Atomic rename is not durable across power loss without directory sync.
        if os.name == 'posix':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        raise CredentialPolicyError('Cannot durably record refresh intent; no retry is safe until storage recovers') from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def owner_path(pool, entry):
    """Resolve custody independently of the requesting consumer's policy."""
    policy = getattr(pool, '_credential_policy', None)
    if policy is not None:
        owner = policy.store_path(pool.provider)
        if owner is None:
            raise CredentialPolicyError('Assigned refresh has no durable owner')
        return owner.resolve()
    from agent import credential_pool as pools
    if entry.id in getattr(pool, '_borrowed_root_ids', ()):
        owner = pools._borrowed_single_use_pool_root()
        if owner is None:
            raise CredentialPolicyError('Shared refresh owner is unavailable')
        return owner.resolve()
    return pools.auth_mod._auth_file_path().resolve()


def begin(pool, entry):
    if pool.provider not in {'anthropic', 'openai-codex', 'xai-oauth'}:
        return None
    owner = owner_path(pool, entry)
    path = owner.with_name(owner.name + '.rotation-intent.json')
    data = _read(path)
    key = pool.provider + ':' + entry.id
    generation = hashlib.sha256(entry.refresh_token.encode()).hexdigest()
    if data.get(key) == generation:
        raise CredentialPolicyError('Previous refresh outcome is uncertain; reconnect this account in the credential manager')
    data[key] = generation
    _write(path, data)  # Must succeed before the first provider call.
    return path, key, generation


def finish(intent):
    if intent is None:
        return
    path, key, generation = intent
    data = _read(path)
    if data.get(key) == generation:
        data.pop(key)
        _write(path, data)


def definite_rejection(exc):
    """No supported provider contract currently proves nonconsumption.

    HTTP classes, resets, truncation and timeouts cannot establish whether an
    upstream spent a rotating grant. Exceptions require a documented provider
    guarantee plus a regression test; none has been established here.
    """
    return False
