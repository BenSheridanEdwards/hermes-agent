"""``hermes auth reauth <provider> <id-or-label>``: renew one pooled OAuth row in place.

``hermes auth add`` always mints a new pool row (new id). A subscription login shared by several
agents is bound by row id (credential assignments name ``{store, ids}``), so renewing it with
``add`` leaves every binding pointing at the dead row. ``reauth`` runs the provider's device-code
login and replaces the tokens of the named row -- same id, label, priority and source -- in the
store selected by ``HERMES_HOME`` / ``-p``. It never creates a row.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import hermes_cli.auth as auth_mod

# Providers whose login is a fresh device-code grant with a single-use refresh-token lineage.
REAUTH_PROVIDERS = ("openai-codex", "xai-oauth")

# Provider singleton (providers.<id>) fields refreshed alongside the tokens.
_SINGLETON_AUTH_MODE = {"openai-codex": "chatgpt", "xai-oauth": "oauth_device_code"}


def _pool_rows(auth_store: Dict[str, Any], provider: str) -> List[Dict[str, Any]]:
    pool = auth_store.get("credential_pool")
    rows = pool.get(provider) if isinstance(pool, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _find_row(rows: List[Dict[str, Any]], target: str, provider: str, store_label: str) -> Dict[str, Any]:
    """Exact id, else a unique case-insensitive label. Raises SystemExit otherwise (no index form:
    positions shift, a renewal must name the row it means)."""
    raw = str(target or "").strip()
    if not raw:
        raise SystemExit("A credential id or exact label is required.")
    for row in rows:
        if row.get("id") == raw:
            return row
    matches = [row for row in rows if str(row.get("label") or "").strip().lower() == raw.lower()]
    if len(matches) > 1:
        ids = ", ".join(str(row.get("id")) for row in matches)
        raise SystemExit(f'Ambiguous {provider} credential label "{raw}" in {store_label} '
                         f"(ids: {ids}). Pass the entry id instead.")
    if not matches:
        raise SystemExit(f'No {provider} credential with id or label "{raw}" in {store_label}. '
                         f"reauth renews an existing row and never creates one; see "
                         f"`hermes auth list {provider}`.")
    return matches[0]


def _apply_tokens(row: Dict[str, Any], access_token: str, fields: Dict[str, Any], now: float) -> None:
    row["access_token"] = access_token
    for key, value in fields.items():
        if value is not None:
            row[key] = value
    for status_field in auth_mod._POOL_STATUS_FIELDS:
        row[status_field] = None
    row["last_status"] = "ok"
    row["last_status_at"] = now


def _sync_singleton(auth_store: Dict[str, Any], provider: str, creds: Dict[str, Any],
                    target_source: str, old_refresh: Optional[str]) -> bool:
    """Keep ``providers.<provider>`` in step when the renewed row is (a copy of) the singleton."""
    providers = auth_store.get("providers")
    state = providers.get(provider) if isinstance(providers, dict) else None
    state_tokens = state.get("tokens") if isinstance(state, dict) else None
    same_lineage = bool(old_refresh) and isinstance(state_tokens, dict) and (
        state_tokens.get("refresh_token") == old_refresh)
    if target_source != "device_code" and not same_lineage:
        return False
    state = dict(state) if isinstance(state, dict) else {}
    state.update(tokens=creds["tokens"], last_refresh=creds.get("last_refresh") or auth_mod._utc_now_z(),
                 auth_mode=_SINGLETON_AUTH_MODE[provider])
    for key in ("discovery", "redirect_uri"):
        if creds.get(key):
            state[key] = creds[key]
    # set_active=False: renewing a login must not change which provider inference routes to.
    auth_mod._store_provider_state(auth_store, provider, state, set_active=False)
    return True


def auth_reauth_command(args) -> None:
    from hermes_cli.auth_commands import _OAUTH_ADD_SPECS, _normalize_provider

    provider = _normalize_provider(getattr(args, "provider", ""))
    if provider not in REAUTH_PROVIDERS:
        raise SystemExit(f"`hermes auth reauth` does not support {provider or 'this provider'}; "
                         f"supported: {', '.join(REAUTH_PROVIDERS)}.")
    target = getattr(args, "target", None)
    store_path = auth_mod._auth_file_path()
    store_label = str(store_path)

    # Resolve before login: a typo must not cost the user a device-code round trip.
    with auth_mod._auth_store_lock():
        row = _find_row(_pool_rows(auth_mod._load_auth_store(), provider), target, provider, store_label)
    row_id, label = row.get("id"), row.get("label")
    print(f'Renewing {provider} credential "{label}" (id={row_id}) in {store_label}')

    spec = _OAUTH_ADD_SPECS[provider]
    creds = spec.login(args)
    access_token = spec.token(creds)
    fields = spec.fields(creds, provider)

    with auth_mod._auth_store_lock():
        auth_store = auth_mod._load_auth_store()
        rows = _pool_rows(auth_store, provider)
        current = next((r for r in rows if r.get("id") == row_id), None)
        if current is None:
            raise SystemExit(f'{provider} credential "{label}" (id={row_id}) disappeared from '
                             f"{store_label} during sign-in; nothing was written.")
        old_refresh = current.get("refresh_token")
        target_source = str(current.get("source") or "")
        now = time.time()
        copies = [r for r in rows if r is not current and old_refresh
                  and r.get("refresh_token") == old_refresh]
        for renewed in (current, *copies):
            _apply_tokens(renewed, access_token, fields, now)
        synced = _sync_singleton(auth_store, provider, creds, target_source, old_refresh)
        auth_mod._save_auth_store(auth_store)

    print(f'Reauthenticated {provider} credential "{label}" (id={row_id}) in place; status: ok')
    if copies:
        print(f"Also renewed {len(copies)} copy row(s) of the same login: "
              + ", ".join(str(r.get("id")) for r in copies))
    if synced:
        print(f"Synced providers.{provider} singleton.")
