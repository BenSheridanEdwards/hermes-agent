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


def _parse_expected(raw: Optional[str]) -> Optional[Dict[str, str]]:
    if raw is None:
        return None
    kind, sep, value = str(raw).partition("=")
    if not sep or not kind.strip() or not value.strip():
        raise SystemExit("--expect-principal must be KIND=VALUE (for example chatgpt_account_id=...).")
    return {"kind": kind.strip(), "value": value.strip()}


def _write_result(path: Optional[str], result: Dict[str, Any]) -> None:
    """Atomic, non-secret structured result for the caller (written before exiting)."""
    if not path:
        return
    import json
    import os
    import tempfile
    from pathlib import Path
    target = Path(path)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".reauth-result-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "at": auth_mod._utc_now_z(), **result}, stream)
        os.replace(temporary, target)
    except OSError:
        from pathlib import Path as _P
        _P(temporary).unlink(missing_ok=True)
        raise


def _finish(args, provider: str, row_id: Any, status: str, reason: str, principal=None) -> None:
    """A non-commit outcome: write the structured result, then exit non-zero (exit 0 is reserved for committed)."""
    _write_result(getattr(args, "result_file", None),
                  {"status": status, "provider": provider, "row_id": row_id, "principal": principal, "reason": reason})
    raise SystemExit(f"Not saved ({status}): {reason}")


def auth_reauth_command(args) -> None:
    from hermes_cli.auth_commands import _OAUTH_ADD_SPECS, _normalize_provider
    from agent.credential_identity import observed_principal, principal_matches
    from pathlib import Path

    provider = _normalize_provider(getattr(args, "provider", ""))
    if provider not in REAUTH_PROVIDERS:
        raise SystemExit(f"`hermes auth reauth` does not support {provider or 'this provider'}; "
                         f"supported: {', '.join(REAUTH_PROVIDERS)}.")
    target = getattr(args, "target", None)
    expected = _parse_expected(getattr(args, "expect_principal", None))
    cancel_file = getattr(args, "cancel_file", None)
    store_path = auth_mod._auth_file_path()
    store_label = str(store_path)

    # Resolve before login: a typo must not cost the user a device-code round trip.
    with auth_mod._auth_store_lock():
        row = _find_row(_pool_rows(auth_mod._load_auth_store(), provider), target, provider, store_label)
    row_id, label = row.get("id"), row.get("label")
    print(f'Renewing {provider} credential "{label}" (id={row_id}) in {store_label}')

    spec = _OAUTH_ADD_SPECS[provider]
    try:
        creds = spec.login(args)
    except BaseException as error:  # noqa: BLE001 -- report then re-raise (KeyboardInterrupt/SIGTERM included)
        cancelled = bool(cancel_file) and Path(cancel_file).exists()
        _write_result(getattr(args, "result_file", None), {"status": "cancelled" if cancelled else "error", "provider": provider,
                      "row_id": row_id, "principal": None, "reason": "sign-in cancelled" if cancelled else f"sign-in failed: {type(error).__name__}"})
        raise
    access_token = spec.token(creds)
    fields = spec.fields(creds, provider)
    # The account that actually signed in, observed by Hermes on the new tokens (never trusted from the caller).
    observed = observed_principal(provider, {"access_token": access_token, "id_token": (creds.get("tokens") or {}).get("id_token")
                                             if isinstance(creds, dict) else None, **(fields if isinstance(fields, dict) else {})})

    with auth_mod._auth_store_lock():
        # Commit point: cancellation, row identity and the expected account are all checked inside the same critical
        # section as the save, so a cancel or a wrong account can never land after the check.
        if cancel_file and Path(cancel_file).exists():
            _finish(args, provider, row_id, "cancelled", "cancelled before commit; nothing was written")
        if expected is not None and not principal_matches(observed, expected):
            _finish(args, provider, row_id, "rejected",
                    "the account that signed in is not the expected account" if observed else
                    "the signed-in account's stable identity could not be observed; nothing was written",
                    principal={"kind": observed["kind"], "value": observed["value"]} if observed else None)
        auth_store = auth_mod._load_auth_store()
        rows = _pool_rows(auth_store, provider)
        current = next((r for r in rows if r.get("id") == row_id), None)
        if current is None:
            _write_result(getattr(args, "result_file", None), {"status": "error", "provider": provider, "row_id": row_id, "principal": None,
                          "reason": "the row disappeared during sign-in; nothing was written"})
            raise SystemExit(f'{provider} credential "{label}" (id={row_id}) disappeared from '
                             f"{store_label} during sign-in; nothing was written.")
        old_refresh = current.get("refresh_token")
        target_source = str(current.get("source") or "")
        now = time.time()
        copies = [r for r in rows if r is not current and old_refresh
                  and r.get("refresh_token") == old_refresh]
        for renewed in (current, *copies):
            _apply_tokens(renewed, access_token, fields, now)
            if observed:
                renewed["principal"] = dict(observed)
            else:
                renewed.pop("principal", None)
        synced = _sync_singleton(auth_store, provider, creds, target_source, old_refresh)
        auth_mod._save_auth_store(auth_store)

    _write_result(getattr(args, "result_file", None), {"status": "committed", "provider": provider, "row_id": row_id,
                  "principal": {"kind": observed["kind"], "value": observed["value"]} if observed else None, "reason": None})
    print(f'Reauthenticated {provider} credential "{label}" (id={row_id}) in place; status: ok')
    if copies:
        print(f"Also renewed {len(copies)} copy row(s) of the same login: "
              + ", ".join(str(r.get("id")) for r in copies))
    if synced:
        print(f"Synced providers.{provider} singleton.")
