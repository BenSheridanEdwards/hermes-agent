---
title: External credential assignments
---

An operator can opt a profile into an external credential manager with
`hermes --profile NAME config set credential_policy.file /absolute/assignments.json`.
The manager owns assignment references; Hermes still owns OAuth tokens, refresh,
locking, and the credential stores. FLEET Access is the first consumer.
`hermes auth policy-capabilities` returns the supported contract without secrets.
Unconfigured profiles retain normal resolution.

```json
{
  "version": 1,
  "environment": {"GITHUB_TOKEN": "fleet_access", "OPENROUTER_API_KEY": "fleet_access"},
  "source_bindings": {"fleet_access": {"GitHub OAuth": "GITHUB_TOKEN", "OpenRouter": "OPENROUTER_API_KEY"}},
  "managed_environment": ["GITHUB_TOKEN", "GH_TOKEN", "OPENROUTER_API_KEY"],
  "accounts": {"openai-codex": {"store": "root", "ids": ["work-account-id"]}}
}
```

`environment` maps variable names to registered secret-source names. Other sources
are not fetched, unassigned outputs are discarded, and managed names cannot fall
back to `.env`, shell values, or preserved values. Keep revoked names in
`managed_environment` so an old `.env` cannot restore them. Values never belong in
this file. The manager must write it atomically. Optional `source_bindings` maps
source-specific credential identifiers to assigned environment names. The source
receives that snapshot as `cfg["credential_bindings"]`; it must use those exact
references rather than rereading mutable external assignments. A revision change
during hydration fails closed.

`accounts` references exact rows in the selected root or profile `auth.json`.
An explicit root reference wins even if the profile has local entries. Absent or
empty bindings do not inherit accounts. API-key providers can use freshly hydrated
assigned environment values; saved manual/environment rows and singleton discovery
are not fallback paths in managed mode. Explicit API-key arguments also cannot
bypass managed resolution. Configure the intended provider explicitly.

OAuth account assignments support OpenAI Codex, xAI OAuth, and Anthropic pool-owned
grants. External CLI-owned credentials, Copilot's token exchange, custom endpoints,
and non-pool providers are not covered by this contract. Do not opt such a profile
in until its required routes are supported or replaced with an assigned supported
route. Missing credentials fail with an assignment/sign-in error rather than using
another account. Adding an account is still Hermes's login operation, performed in
the owning store before the manager assigns its ID.

Refresh uses Hermes's provider code and the **owning store's lock**. Rotations update
only assigned existing rows, preserve unrelated accounts, and keep device-code
singleton state in that same store current. Environment credentials stay ephemeral;
selection does not create a reusable profile-local copy. Managers should observe
refresh rather than run a second periodic refresh service.

Local foreground, background, and PTY terminals receive explicitly assigned
`GITHUB_TOKEN`/`GH_TOKEN` values from the source snapshot. When only `GITHUB_TOKEN`
is assigned, `GH_TOKEN` receives the same value to prevent an ambient account from
winning GitHub CLI precedence. These names are excluded from persistent shell
snapshots. This is an operator policy permission, not a skill-provided environment
passthrough; execute-code and unrelated subprocesses remain scrubbed. Other tool
credentials retain their existing tool-specific delivery behavior.

Policy revisions are SHA-256 hashes of the manifest bytes. Changes invalidate old
pool selections; source changes require an agent reload. Already executing requests
and child processes can finish with their previously issued credentials. This is
configuration authority, not an OS sandbox or revocation of the provider's token.
The profile's `credential-policy-receipt.json` records revision, process, time,
applied variable names, missing variables, selected account ID, and terminal delivery
names. It contains no credential values. A receipt is historical evidence, not proof
that the process is still running or that GitHub granted access to a specific repo.

Rollback: detach the manifest with
`hermes --profile NAME config set credential_policy.file '' --force`, restore the
prior source configuration if needed, and restart the agent. Credential stores are
retained throughout adoption and revocation. A missing or invalid configured manifest
fails closed; restore a valid manifest before retrying normal operation.
