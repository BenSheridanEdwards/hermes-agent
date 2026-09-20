# Explicit shared Codex authority

Set this only for profiles that are allowed to use the same Codex accounts:

```yaml
oauth:
  refresh_owner: runtime
  shared_codex_auth_path: /absolute/path/to/shared/auth.json
```

The shared store must already contain a non-empty `credential_pool.openai-codex`
list of self-contained manual OAuth entries with distinct entry IDs. Create each
account with `hermes auth add openai-codex --label "Account name"` in the owning
root home. Do not copy tokens into profiles. This setting does not enroll tenants
or change any other provider's store. `refresh_owner` still applies to Codex and
xAI; preserve an external owner's schedule until its replacement is proven.

In shared mode, runtime and auxiliary readers use the configured pool even if a
profile still has old Codex entries. Reads do not remove those entries. Missing,
unreadable, or malformed shared data is an error, never permission to fall back
to profile tokens or import a Codex CLI token. Legacy singleton writes and refresh
calls are refused; use the pool's account operations instead.

Selection, health updates, and refresh persist to the shared store. Refresh holds
that store's lock across re-read, token exchange, and commit. The transaction
keeps that authority even if profile configuration changes during the exchange. A waiting process
adopts a committed rotation rather than spending it again. Only the account being
refreshed can replace its token pair; stale snapshots cannot restore an old pair
for another account. A stale failure from an old access token cannot poison its
replacement's health.

Before an exchange, Hermes writes a small refresh-intent sidecar next to the
shared store. It contains only a digest. A lost response or failed commit leaves
the intent in place and prevents replay of that uncertain token. Authenticate the
affected account again to recover; do not clear an intent and retry the old token.
Pending entries cannot be selected or adopted from an older cached pair. Dead
shared accounts remain in the store for recovery; removing the last account is
refused so the authority cannot become unreadable.

An explicit token-endpoint quota refusal clears the intent so a later retry is
possible. A successful commit clears it too. These sidecars are operational state,
not credentials or backups.

## Deployment

1. Back up the current root store and configuration with private permissions.
2. Establish and verify the intended accounts in the root. Keep identities and
   refresh-token lineages distinct.
3. Test the staged runtime with an isolated profile pointing at that root. Prove
   access, refresh, authoritative persistence, and use from a second process.
4. Migrate an idle profile. Preserve its model, account access, and unrelated
   providers. Remove old local entries only after the new path works, through a
   scoped, supported operation with recovery available.
5. Replace an external refresh schedule only after all affected providers have a
   proven owner. Verify the installed runtime and actual schedule afterward.

A passing synthetic test is not proof of live authentication or a completed
migration. No live configuration or credential change is applied by this feature
alone. Profiles without this setting retain the existing isolation behavior.
