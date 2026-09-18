"""Opt-in credential assignments supplied by an external manager.

The manager owns references, not tokens. An absent setting preserves normal Hermes
resolution; an unreadable policy never falls back to ambient credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

from hermes_constants import get_hermes_home


class CredentialPolicyError(RuntimeError):
    pass


_environment_revisions: dict[str, str] = {}


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("Invalid credential assignment contract")


@dataclass(frozen=True)
class CredentialPolicy:
    revision: str
    environment: dict[str, str]
    managed_environment: frozenset[str]
    accounts: dict[str, dict]
    source_bindings: dict[str, dict[str, str]]
    home: Path

    def store_path(self, provider: str) -> Path | None:
        binding = self.accounts.get(provider)
        if binding is None:
            return None
        if binding["store"] == "profile":
            return self.home / "auth.json"
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root() / "auth.json"

    def rows(self, provider: str) -> list[dict]:
        path = self.store_path(provider)
        if path is None:
            return []
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise CredentialPolicyError("Assigned credential store is unreadable") from exc
        if not isinstance(store, dict) or not isinstance(store.get("credential_pool", {}), dict):
            raise CredentialPolicyError("Assigned credential store is invalid")
        rows = store.get("credential_pool", {}).get(provider, [])
        if not isinstance(rows, list):
            raise CredentialPolicyError("Assigned credential pool is invalid")
        wanted = self.accounts[provider]["ids"]
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
        return [by_id[cid] for cid in wanted if cid in by_id]


def load_policy(home: Path | None = None) -> CredentialPolicy | None:
    home = Path(home or get_hermes_home())
    config_path = home / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        raise CredentialPolicyError("Cannot read credential policy configuration") from exc
    setting = config.get("credential_policy")
    if not setting or setting == {"file": ""}:
        return None
    try:
        path = Path(setting["file"]).expanduser()
        if not path.is_absolute():
            path = home / path
        raw = path.read_bytes()
        data = json.loads(raw)
        env = data["environment"]
        managed = data["managed_environment"]
        accounts = data["accounts"]
        _require(isinstance(env, dict))
        bindings = data.get("source_bindings", {})
        _require(isinstance(bindings, dict))
        for source, refs in bindings.items():
            _require(isinstance(source, str) and isinstance(refs, dict))
            _require(all(isinstance(ref, str) and isinstance(name, str) and env.get(name) == source for ref, name in refs.items()))
        _require(data["version"] == 1 and isinstance(env, dict) and isinstance(accounts, dict))
        _require(isinstance(managed, list) and all(isinstance(n, str) for n in managed))
        names = set(managed) | set(env)
        _require(all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", n) for n in names))
        _require(not names.intersection({"HOME", "PATH", "HERMES_HOME", "BWS_ACCESS_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN", "PYTHONPATH", "NODE_OPTIONS"}))
        _require(all(isinstance(s, str) and re.fullmatch(r"[a-z][a-z0-9_]*", s) for s in env.values()))
        for provider, binding in accounts.items():
            _require(isinstance(provider, str) and isinstance(binding, dict))
            _require(binding["store"] in ("root", "profile") and isinstance(binding["ids"], list))
            _require(all(isinstance(cid, str) and cid for cid in binding["ids"]))
        return CredentialPolicy(hashlib.sha256(raw).hexdigest(), env, frozenset(names), accounts, bindings, home)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        raise CredentialPolicyError("Credential assignment policy is missing or invalid; reconnect the credential manager") from exc


def record_receipt(policy: CredentialPolicy, **fields) -> None:
    """Metadata only; a failed receipt must not interrupt a successful token rotation."""
    target = policy.home / "credential-policy-receipt.json"
    previous = {}
    try:
        candidate = json.loads(target.read_text(encoding="utf-8"))
        if candidate.get("revision") == policy.revision and candidate.get("pid") == os.getpid():
            previous = candidate
    except (OSError, ValueError):
        pass
    receipt = {**previous, "version": 1, "revision": policy.revision, "pid": os.getpid(),
               "at": datetime.now(timezone.utc).isoformat(), **fields}
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=policy.home, prefix=".credential-receipt-")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream)
        os.replace(temporary, target)
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def finish_environment(home: Path, environ, report, policy: CredentialPolicy | None) -> None:
    current = load_policy(home)
    if current != policy:
        for name in (policy.managed_environment if policy else frozenset()) | (current.managed_environment if current else frozenset()):
            environ.pop(name, None)
        raise CredentialPolicyError("Credential assignments changed during secret loading; reload the agent")
    if policy is None:
        return
    applied = {name: item.source for name, item in report.provenance.items()
               if policy.environment.get(name) == item.source}
    for name in policy.managed_environment:
        if name not in applied:
            environ.pop(name, None)
    _environment_revisions[str(policy.home.resolve())] = policy.revision
    record_receipt(policy, phase="environment", applied=applied,
                   missing=sorted(set(policy.environment) - set(applied)))


def managed_pool(provider: str, policy: CredentialPolicy):
    from agent.credential_pool import CredentialPool, PooledCredential, _seed_from_env
    entries = [PooledCredential.from_dict(provider, row) for row in policy.rows(provider)]
    # Never seed from singleton files, local auth copies, or a previously saved env row.
    env_entries = []
    _seed_from_env(provider, env_entries)
    entries.extend(entry for entry in env_entries if entry.source.removeprefix("env:") in policy.environment)
    pool = CredentialPool(provider, entries)
    pool._credential_policy = policy
    if policy.accounts.get(provider, {}).get("store") == "root":
        pool._borrowed_root_ids = {row["id"] for row in policy.rows(provider)}
    return pool


def assigned_env_value(name: str) -> str | None:
    """None means unmanaged; empty means managed but not delivered by the assigned source."""
    policy = load_policy()
    if policy is None:
        return None
    from hermes_cli.env_loader import get_secret_source_values
    # Values here are the orchestrator's scoped result, never a stale .env value.
    if name not in policy.environment:
        return ""
    if _environment_revisions.get(str(policy.home.resolve())) != policy.revision:
        raise CredentialPolicyError("Credential assignments changed; reload the agent to load secrets")
    return get_secret_source_values(policy.home).get(name, "")


def persist_managed(provider: str, payloads: list[dict], status_cleared_ids=None) -> bool:
    policy = load_policy()
    if policy is None:
        return False
    path = policy.store_path(provider)
    if path is not None:
        from agent.credential_pool import _update_root_pool_rows
        wanted = set(policy.accounts[provider]["ids"])
        _update_root_pool_rows(provider, [p for p in payloads if p.get("id") in wanted], path,
                               status_cleared_ids=status_cleared_ids)
    # Env references remain ephemeral. Revocation must not leave reusable pool copies.
    return True


def resolve_managed_runtime(policy: CredentialPolicy, requested: str, target_model=None):
    from hermes_cli import runtime_provider as rp
    from agent.credential_pool import load_pool
    provider = rp.resolve_provider(requested)
    pool = load_pool(provider)
    entry = pool.select()
    if entry is None:
        record_receipt(policy, phase="selection", provider=provider, status="needs-sign-in-or-assignment")
        raise CredentialPolicyError(f"No usable assigned credential for {provider}; open your credential manager")
    runtime = rp._resolve_runtime_from_pool_entry(provider=provider, entry=entry,
        requested_provider=requested, model_cfg=rp._get_model_config(), pool=pool, target_model=target_model)
    record_receipt(policy, phase="selection", provider=provider, entry=entry.id,
                   source=entry.source, status="selected")
    return runtime


def check_pool_revision(pool) -> None:
    expected = getattr(pool, "_credential_policy", None)
    if expected is None:
        return
    current = load_policy()
    if current is None or current.revision != expected.revision or current.home != expected.home:
        raise CredentialPolicyError("Credential assignments changed; reload this agent before continuing")


def sync_managed_entry(pool, entry):
    policy = getattr(pool, "_credential_policy", None)
    if policy is None:
        return None
    from agent.credential_pool import PooledCredential
    row = next((r for r in policy.rows(pool.provider) if r.get("id") == entry.id), None)
    if row is None:
        raise CredentialPolicyError("Assigned account was removed; reconnect in the credential manager")
    stored = PooledCredential.from_dict(pool.provider, row)
    if stored.access_token != entry.access_token or stored.refresh_token != entry.refresh_token:
        pool._replace_entry(entry, stored)
        return stored
    return entry


def persist_assignment(policy, provider, payloads, status_cleared_ids=None):
    path = policy.store_path(provider)
    if path is None:
        return
    from agent.credential_pool import _update_root_pool_rows
    wanted = set(policy.accounts[provider]["ids"])
    _update_root_pool_rows(provider, [p for p in payloads if p.get("id") in wanted], path,
                           status_cleared_ids=status_cleared_ids)


def restore_assigned_environment(home: Path, environ) -> None:
    policy = load_policy(home)
    if policy is None:
        return
    from hermes_cli.env_loader import get_secret_source_values
    values = get_secret_source_values(home)
    if _environment_revisions.get(str(policy.home.resolve())) != policy.revision:
        values = {}
    for name in policy.managed_environment:
        value = values.get(name) if name in policy.environment else None
        if value:
            environ[name] = value
        else:
            environ.pop(name, None)


def capabilities_command(_args) -> None:
    print(json.dumps({"credential_policy": 1, "account_providers": ["openai-codex", "xai-oauth", "anthropic"],
                      "refresh_owner": "hermes", "activation": "reload"}))


# Operator-owned assignment, deliberately narrower than skill env_passthrough.
# The local terminal can run gh/git; execute_code and other children remain scrubbed.
TERMINAL_CREDENTIAL_NAMES = frozenset({"GH_TOKEN", "GITHUB_TOKEN"})


def terminal_credentials() -> dict[str, str]:
    policy = load_policy()
    if policy is None:
        return {}
    from hermes_cli.env_loader import get_secret_source_values
    values = get_secret_source_values(policy.home)
    if TERMINAL_CREDENTIAL_NAMES.intersection(policy.environment) and _environment_revisions.get(str(policy.home.resolve())) != policy.revision:
        raise CredentialPolicyError("Credential assignments changed; reload the agent to load terminal credentials")
    granted = {name: values[name] for name in TERMINAL_CREDENTIAL_NAMES
               if name in policy.environment and values.get(name)}
    # gh gives GH_TOKEN precedence. An ambient GH_TOKEN must never select another account.
    if "GITHUB_TOKEN" in granted and "GH_TOKEN" not in granted:
        granted["GH_TOKEN"] = granted["GITHUB_TOKEN"]
    record_receipt(policy, phase="terminal", terminal_names=sorted(granted))
    return granted


def sync_managed_singleton(pool, entry) -> bool:
    policy = getattr(pool, "_credential_policy", None)
    if policy is None:
        return False
    if entry.source != "device_code":
        return True
    path = policy.store_path(pool.provider)
    if path is None:
        return True
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    with _auth_store_lock(target_path=path):
        store = _load_auth_store(path)
        state = store.get("providers", {}).get(pool.provider)
        if isinstance(state, dict) and pool._apply_entry_to_singleton_state(entry, state):
            _save_auth_store(store, target_path=path)
    return True
