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
import uuid
from typing import Any, Dict, Optional

#: One id per Python process: an operation record names the runtime instance that produced it.
RUNTIME_INSTANCE = uuid.uuid4().hex

#: Providers whose stable principal Hermes supports, and the claim it reads.
STABLE_PRINCIPAL_KINDS = {"openai-codex": "chatgpt_account_id"}

def oauth_metadata(provider: str, creds: Dict[str, Any]) -> Dict[str, Any]:
    """Observed login metadata, never inferred from labels or a previous token."""
    from hermes_cli.auth_constants import _decode_jwt_claims
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        tokens = creds
    claims = _decode_jwt_claims(tokens.get("access_token"))
    scope = tokens.get("scope") or creds.get("scope") or claims.get("scope") or claims.get("scp")
    if isinstance(scope, list) and all(isinstance(item, str) for item in scope):
        scope = " ".join(scope)
    client = creds.get("client_id") or claims.get("client_id") or claims.get("azp")
    metadata: Dict[str, Any] = {"principal": observed_principal(provider, creds)}
    if isinstance(client, str) and client:
        metadata["client_id"] = client
    if isinstance(scope, str) and scope:
        metadata["scope"] = " ".join(sorted(set(scope.split())))
    if isinstance(creds.get("auth_mode"), str):
        metadata["auth_mode"] = creds["auth_mode"]
    return metadata


def observed_principal(provider: str, row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """``{"kind", "value", "source"}`` for a supported stable principal on the row's tokens, else ``None``."""
    kind = STABLE_PRINCIPAL_KINDS.get(provider)
    if kind is None or not isinstance(row, dict):
        return None
    from hermes_cli.auth_constants import _decode_jwt_claims
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    for name in ("access_token",):
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
    """One tool-free inference through the selected Responses runtime, never a metadata probe.

    Only a completed provider response with text counts. The same held pool must remain
    authorized before transport and at completion. Unsupported runtimes stay unverified.
    """
    from agent.credential_policy import CredentialPolicyError, check_pool_revision, load_policy, record_receipt
    from hermes_cli.config import load_config
    from openai import OpenAI, APIError
    import httpx
    import time

    principal = observed_principal(provider, entry.to_dict())
    record = {"version": 1, "kind": "responses.create", "provider": provider, "entry": entry.id,
              "principal": {"kind": principal["kind"], "value": principal["value"]} if principal else None,
              "revision": policy.revision, "pid": os.getpid(), "instance": RUNTIME_INSTANCE,
              "recipient": str(policy.home.resolve()), "status": "unsupported", "http_status": None}
    pool = runtime.get("credential_pool")
    model = (load_config().get("model") or {}).get("default")

    def current():
        if pool is None or getattr(pool, "_credential_policy", None) != policy or load_policy(policy.home) != policy:
            raise CredentialPolicyError("Runtime authority changed; reload before checking access")
        check_pool_revision(pool)
        row = next((r for r in policy.rows(provider) if r.get("id") == entry.id), None)
        if row is None or row.get("access_token") != runtime.get("api_key"):
            raise CredentialPolicyError("Selected account changed or was revoked")

    if provider == "openai-codex" and runtime.get("api_mode") == "codex_responses" and principal and model:
        try:
            current()
            from agent.codex_headers import codex_cloudflare_headers
            headers = codex_cloudflare_headers(runtime["api_key"], base_url=runtime["base_url"])
            headers["ChatGPT-Account-Id"] = principal["value"]
            started = time.monotonic()
            # Same SDK/Responses protocol as Hermes inference. No tools, history, retries,
            # redirects, ambient proxy or credential fallback. Never persist response content.
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as transport:
                with OpenAI(api_key=runtime["api_key"], base_url=runtime["base_url"],
                            default_headers=headers, max_retries=0, http_client=transport) as client:
                    with client.responses.create(model=model, instructions="Reply only OK.",
                            input="Check access. Reply OK.", store=False, stream=True) as stream:
                        record.update(status="error", http_status=200)
                        for event in stream:
                            if time.monotonic() - started > timeout:
                                raise TimeoutError("Operation deadline exceeded")
                            if event.type == "response.completed":
                                response = event.response
                                if response.status == "completed" and response.output_text:
                                    current()
                                    record.update(status="accepted")
                                break
        except CredentialPolicyError:
            record.update(status="stale", http_status=None)
        except APIError as error:
            status = getattr(error, "status_code", None)
            record.update(status="rejected" if status in (401, 403) else "error", http_status=status)
        except (OSError, ValueError, TimeoutError):
            record.update(status="error", http_status=None)
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
