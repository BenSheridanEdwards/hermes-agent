"""Supported account identity and bounded-operation evidence for managed credentials.

Additive companion contract for an external credential manager (Fleet Access, PR #2134):

* ``observed_principal(provider, row)`` -- the provider-qualified, stable account principal Hermes itself
  observes on an OAuth row's tokens. Only documented-stable kinds are reported: for ``openai-codex`` the
  ``chatgpt_account_id`` claim. ``sub`` and ``email`` are never promoted to a stable key, and providers without
  a supported stable claim (xAI, Anthropic) report ``None`` -- unknown stays unknown.
* ``bounded_operation(...)`` -- one bounded, read-only request made with the credential the managed runtime
  actually selected, through the runtime's own base URL. Its HTTP result (accepted / rejected) is recorded in the
  profile-local credential-policy receipt bound to the policy revision, provider, entry, observed principal and
  this runtime instance. Selection, refresh or quota are never recorded as an accepted operation.

Tokens never leave this process: only non-secret identifiers and the transport status are written.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional

#: One id per Python process: an operation record names the runtime instance that produced it.
RUNTIME_INSTANCE = uuid.uuid4().hex

#: Providers whose stable principal Hermes supports, and the claim it reads.
STABLE_PRINCIPAL_KINDS = {"openai-codex": "chatgpt_account_id"}

#: The bounded operation per provider: a read-only listing on the runtime's own base URL.
BOUNDED_OPERATIONS = {"openai-codex": ("models.list", "/models"), "xai-oauth": ("models.list", "/models")}


def observed_principal(provider: str, row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """``{"kind", "value", "source"}`` for a supported stable principal on the row's tokens, else ``None``."""
    kind = STABLE_PRINCIPAL_KINDS.get(provider)
    if kind is None or not isinstance(row, dict):
        return None
    from hermes_cli.auth_constants import _decode_jwt_claims
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    for name in ("id_token", "access_token"):
        claims = _decode_jwt_claims(row.get(name) or tokens.get(name))
        nested = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
        value = nested.get(kind) if isinstance(nested, dict) else None
        if isinstance(value, str) and value.strip():
            return {"kind": kind, "value": value.strip(), "source": f"{name}_claim"}
    return None


def principal_matches(observed: Optional[Dict[str, Any]], expected: Optional[Dict[str, Any]]) -> bool:
    return bool(observed and expected and observed.get("kind") == expected.get("kind")
                and observed.get("value") == expected.get("value"))


def bounded_operation(policy, provider: str, entry, runtime: Dict[str, Any], *, timeout: float = 20.0) -> Dict[str, Any]:
    """Make the provider's bounded request with the selected credential and record its transport result."""
    from agent.credential_policy import record_receipt
    kind, path = BOUNDED_OPERATIONS.get(provider, (None, None))
    principal = observed_principal(provider, {"access_token": getattr(entry, "access_token", None),
                                              "id_token": (getattr(entry, "extra", {}) or {}).get("id_token")})
    record = {"version": 1, "kind": kind, "provider": provider, "entry": getattr(entry, "id", None),
              "principal": {"kind": principal["kind"], "value": principal["value"]} if principal else None,
              "revision": policy.revision, "pid": os.getpid(), "instance": RUNTIME_INSTANCE}
    base_url = str(runtime.get("base_url") or "").rstrip("/")
    api_key = runtime.get("api_key")
    if kind is None or not base_url or not isinstance(api_key, str) or not api_key:
        record.update(status="unsupported", http_status=None)
    else:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "User-Agent": "hermes-credential-probe/1"}
        if principal and principal["kind"] == "chatgpt_account_id":
            headers["ChatGPT-Account-Id"] = principal["value"]
        request = urllib.request.Request(base_url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- runtime-owned base URL
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except (urllib.error.URLError, OSError, ValueError):
            status = None
        record.update(http_status=status,
                      status="accepted" if status is not None and 200 <= status < 300
                      else "rejected" if status in (401, 403) else "error")
    record["at"] = datetime.now(timezone.utc).isoformat()
    record_receipt(policy, operation=record)
    return record


def probe_command(args) -> None:
    """``hermes auth probe <provider>``: select the managed credential and run its bounded operation.

    Prints the non-secret record as JSON. Exit 0 only for ``accepted``; 3 for ``rejected``; 4 otherwise.
    """
    from agent.credential_policy import CredentialPolicyError, load_policy, select_managed_runtime
    provider = str(getattr(args, "provider", "") or "")
    try:
        policy = load_policy()
        if policy is None:
            raise CredentialPolicyError("This profile has no credential policy; nothing to probe")
        resolved, entry, runtime = select_managed_runtime(policy, provider)
        record = bounded_operation(policy, resolved, entry, runtime)
    except CredentialPolicyError as error:
        print(json.dumps({"version": 1, "status": "error", "reason": str(error)}))
        raise SystemExit(4)
    print(json.dumps(record))
    raise SystemExit(0 if record["status"] == "accepted" else 3 if record["status"] == "rejected" else 4)
