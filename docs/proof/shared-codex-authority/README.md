# Verification evidence

This is self-attested local verification on macOS, using the installed Hermes
Python environment and the canonical hermetic test runner. Network refreshes in
these tests use synthetic responses. Live account authentication, deployment,
and migration are not certified by these results.

The source hashes bind the raw focused output to the tested files.

```sh
scripts/run_tests.sh tests/agent/test_shared_codex_authority.py tests/agent/test_shared_codex_readers.py tests/hermes_cli/test_auth_codex_oauth_ownership.py tests/agent/test_credential_pool_anthropic_refresh_race.py tests/scripts/test_windows_footguns_full_repo_scan.py -j 4
```

Result: 53 passed. Ruff and `git diff --check` passed.

An earlier complete run on the same worktree, before the final review fixes,
reported 42,126 passed, 96 failed, 417 skipped and one flaky file. It is not an
all-green or final-head result. The new Windows text-encoding failure is fixed.
Three pre-existing Anthropic race-test adapter failures are also fixed here.
The remaining failures include installed dependency differences and macOS-only
platform behavior; baseline comparison runs are recorded separately. Hosted CI
is required before this patch is called merge-ready.

Screenshots: Not applicable. This change modifies credential storage and
refresh transactions, with no rendered UI change.
