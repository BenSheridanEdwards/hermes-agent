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
is checked again and the newest owner row is adopted. The lock and durable
intent follow the canonical owner, including an unmanaged/root-fallback caller;
consumer policy presence is not the fence. A private 0600
`auth.json.rotation-intent.json` records generation fingerprints, never tokens,
before the POST. Successful commits retain predecessor fences rather than
forgetting that a generation was used. Row renames cannot authorize replay.

A crash, reset, truncation, timeout, generic 503 or any other unsupported HTTP
classification leaves uncertainty fenced. No provider-specific nonconsumption
guarantee has been established, including for all 4xx responses. Anthropic does
not try a second endpoint with the same refresh generation after an exception.
Owner-store write-through failures propagate; they cannot silently clear intent.

A newly authorized replacement generation can recover without erasing its
predecessor. A known fenced entry is not eligible, but another explicitly
eligible independent account can serve. This narrow distinction does not catch
or ignore policy drift, malformed stores or unknown recovery metadata.
Known root copies and renamed predecessors are refused, not elected or deleted.
Canonical file references are not turned into separate credential copies.

Present invalid owner stores are not empty stores: malformed JSON, container
shapes, identifiers/duplicates, token types and expiries fail with a fixed
sanitized error and unchanged source bytes. Corruption no longer creates an
implicit credential backup or an empty mutable replacement.

The owner alone performs reauthentication and explicit reference migration.
Consumer loading no longer invokes heuristic fork consolidation. Legacy repair
utility tests are explicit synthetic owner exercises, not authorization to use
that heuristic on live credentials. A lost successor cannot be fabricated:
reauthenticate through the owner and retain coherent store/intent history.

## Assignment publication and recovery

Native policy v2 requires a committed matching owner publication. A staged or
failed update never falls back to an older grant. Permission history is retained.
Fleet writes its narrow configuration connection as publisher; a blocked consumer
does not rewrite its own authority. The exact `auth policy-capabilities` query is
pure metadata and remains available before credential loading. This is not a
blanket auth/config exemption. A substantive global publication may require
managed readers to reload; no-op syncs preserve the revision.

These are cooperative store/consumer guarantees, not process isolation against
hostile same-UID code or proof of opaque permanent grant-family identity. External
CLI-owned singleton paths retain their separate existing ownership contracts;
do not infer cross-vendor locking or live consolidation from pooled-store tests.
No live authentication, credential migration or deletion was exercised here.

## Tests and limits

`tests/agent/test_managed_refresh_processes.py` starts actual independent Python
processes against isolated roots with synthetic transport. It tests all three
providers: Anthropic, Codex and xAI; competing refreshers, fresh-loader adoption,
preflight and post-rotation disk errors, crash/restart, ambiguous timeout and
ambiguous-outage fencing and explicit-generation recovery. It has no Windows-only gate and runs on Darwin/Linux.
A run on Darwin does not establish Linux execution. Desktop router tests use
the actual FastAPI routers and real assignment resolver, without a substitute
Desktop server or live credential probe.

The full Desktop unit suite mocks the native Electron API at its explicit
unit-test boundary. This neither packages Electron nor verifies native/signing
integration. Deployment, reauthentication, scheduler changes and security-policy
changes require their separate live approvals.
