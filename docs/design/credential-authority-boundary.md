# Assigned credential consumer contract

Fleet owns grants and stable account IDs. An assigned Hermes profile consumes
that authority; Desktop and the CLI are not alternative grant writers.

## Desktop APIs

`credential_authority.py` uses the same `agent.credential_policy` resolver as
inference. Managed environment metadata comes from assigned root entries, not
local `.env` shadows. Managed OAuth status reports `credential_present`,
`assigned_entry_ids`, `authority=assignment`, `editable=false`, and
`state=assigned_unverified` (or `unassigned`). `logged_in=false` deliberately
does not turn stored metadata into an authenticated readiness assertion.

Provider-key saves preflight authority before any file write. Bulk saves
preflight the entire batch. Provider OAuth start, paste, disconnect and pool
add/remove return HTTP 409 in a policy-bound consumer, including when the
provider has no grant. Pending logins recheck the selected profile before
saving. These refusals direct the operator to the existing credential owner
interface; they do not silently edit assignments, append a pool ID, broaden
policy or return success for an ineffective local copy. Unmanaged profiles
retain their existing login and local-save behavior.

Pool add/remove/reorder/status-reset operations raise `CredentialPolicyError`
for a bound pool (including stale/revoked bindings and a pool acquired before
policy activation), before in-memory or disk mutation. Operational refresh
and cooldown bookkeeping remain separate from administration. Assigned
refresh retains the stable row ID and changes only the assigned row through
the existing owner-store write-through contract.

## Refresh durability and recovery

Within the existing owner auth-store process lock, the selected policy revision
is checked again, the newest root row is adopted, and a private 0600,
fsynced `.auth.json.rotation-intent` file records the provider, row ID and a
SHA-256 fingerprint of the pre-refresh generation. It contains no token values.
The intent is durable before the synthetic/provider POST. Once the new auth
row and required singleton write-through succeed, the intent is cleared.

A crash, ambiguous timeout or post-POST persistence failure leaves the intent.
A fresh process refuses to replay that same generation. Explicit HTTP rejection
clears it, so a definite outage can recover through the existing cooldown path.
Anthropic refresh does not try its alternate endpoint after an ambiguous timeout.
An explicitly reauthenticated replacement generation is not confused with the
old consumed generation. Consumers must not delete an unresolved intent to
force a retry. Restore/recovery must retain coherent owner-store and intent
state, never blindly restore and reuse an old single-use refresh grant.

This is fail-closed loss prevention, not recovery of a token that the provider
returned but the machine could not persist. That event requires the owner's
existing reauthentication flow. No automatic refresh was exercised against a
real provider while developing this patch.

## Tests and limits

`tests/agent/test_managed_refresh_processes.py` starts actual independent Python
processes against isolated roots with synthetic transport. It tests all three
providers: Anthropic, Codex and xAI; competing refreshers, fresh-loader adoption,
preflight and post-rotation disk errors, crash/restart, ambiguous timeout and
definite-outage recovery. It has no Windows-only gate and runs on Darwin/Linux.
A run on Darwin does not establish Linux execution. Desktop router tests use
the actual FastAPI routers and real assignment resolver, without a substitute
Desktop server or live credential probe.

The full Desktop unit suite mocks the native Electron API at its explicit
unit-test boundary. This neither packages Electron nor verifies native/signing
integration. Deployment, reauthentication, scheduler changes and security-policy
changes require their separate live approvals.
