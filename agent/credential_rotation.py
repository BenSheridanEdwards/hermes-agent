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
        data = json.loads(path.read_text())
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
        with os.fdopen(fd, 'w') as stream:
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


def begin(pool, entry):
    policy = getattr(pool, '_credential_policy', None)
    if policy is None or pool.provider not in {'anthropic', 'openai-codex', 'xai-oauth'}:
        return None
    owner = policy.store_path(pool.provider)
    if owner is None:
        raise CredentialPolicyError('Assigned refresh has no durable owner')
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
    """A received HTTP error is not an indeterminate transport timeout."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return 400 <= exc.code < 600
    response = getattr(exc, 'response', None)
    code = getattr(response, 'status_code', None)
    return isinstance(code, int) and 400 <= code < 600
