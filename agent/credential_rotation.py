"""Durable pre-POST intent for owner-scoped single-use refresh grants.

The caller holds the owner auth-store lock across begin -> transport -> token
commit -> finish. Only generation fingerprints are stored, never tokens. An
unresolved intent refuses replay after restart. A newly authorized generation
can advance without erasing predecessor fences; deleting this file is not recovery.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from agent.credential_policy import CredentialPolicyError


class RefreshGenerationFenced(CredentialPolicyError):
    """One interpreted entry is unusable; other explicitly eligible entries may work."""


def _contains_generation(data, provider, generation):
    return any(key.startswith(provider + ':') and (value if isinstance(value, str) else value['generation']) == generation
               for key, value in data.items())


def _read(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CredentialPolicyError('Refresh intent is unreadable; contact the credential manager') from exc
    def valid(value):
        if isinstance(value, str):
            return len(value) == 64
        return (isinstance(value, dict) and isinstance(value.get('generation'), str)
                and len(value['generation']) == 64 and value.get('state') in {'pending', 'committed'})
    if not isinstance(data, dict) or any(not isinstance(k, str) or not valid(v) for k, v in data.items()):
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
    if pool.provider not in {'anthropic', 'openai-codex', 'xai-oauth'} or not entry.refresh_token:
        return None
    owner = owner_path(pool, entry)
    generation = hashlib.sha256(entry.refresh_token.encode()).hexdigest()
    # Refuse known local copies of the root authority; never heal/delete them
    # or elect a new owner during a consumer read. No global lineage daemon.
    from hermes_constants import get_default_hermes_root
    from agent.credential_store_schema import read_store
    root = (get_default_hermes_root() / 'auth.json').resolve()
    if owner != root:
        root_store = read_store(root)
        if root_store is not None:
            holders = list(root_store.get('credential_pool', {}).get(pool.provider, []))
            holders.append(root_store.get('providers', {}).get(pool.provider, {}).get('tokens', {}))
            if any(row.get('refresh_token') == entry.refresh_token for row in holders):
                raise CredentialPolicyError('Copied rotating credential; use the canonical owner reference before refresh')
        root_history = _read(root.with_name(root.name + '.rotation-intent.json'))
        if _contains_generation(root_history, pool.provider, generation):
            raise CredentialPolicyError('Copied predecessor is fenced by its canonical owner; owner reconciliation required')
    path = owner.with_name(owner.name + '.rotation-intent.json')
    data = _read(path)
    key = pool.provider + ':' + generation
    if _contains_generation(data, pool.provider, generation):
        raise RefreshGenerationFenced('Refresh generation is spent or uncertain; owner recovery required')
    data[key] = {'generation': generation, 'entry': entry.id, 'state': 'pending',
                 'started_at': datetime.now(timezone.utc).isoformat()}
    _write(path, data)  # Must succeed before the first provider call.
    return path, key, generation


def finish(intent):
    if intent is None:
        return
    path, key, generation = intent
    data = _read(path)
    record = data.get(key)
    if isinstance(record, dict) and record['generation'] == generation:
        data[key] = {**record, 'state': 'committed', 'committed_at': datetime.now(timezone.utc).isoformat()}
        _write(path, data)


def definite_rejection(exc):
    """No supported provider contract currently proves nonconsumption.

    HTTP classes, resets, truncation and timeouts cannot establish whether an
    upstream spent a rotating grant. Exceptions require a documented provider
    guarantee plus a regression test; none has been established here.
    """
    return False
