# ACP attachment verification receipt

Implementation-stage receipt, recorded before commit/publication. No deployment or live gateway/config changes were made. Later PR/CI receipts are tracked on the pull request.
Base: `3e7dbc609c06f2374036d1a1da24e67d9406b535`.

## Final focused result

Canonical wrapper, hermetic temporary HERMES_HOME, macOS, 24 per-file workers:
**82 passed, 0 failed across 12 files.** Includes ordinary ACP regressions and
18 production-socket attachment contract tests. Model-path tests replace only
model execution; the runner, session store, journal and socket are real.

```sh
HERMES_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python" scripts/run_tests.sh \
  tests/gateway/test_acp_admission.py tests/gateway/test_acp_attachment_contract.py \
  tests/gateway/test_acp_attach_canonical.py tests/gateway/test_acp_attach_transport.py \
  tests/gateway/test_acp_attach_observer.py tests/gateway/test_acp_delivery.py \
  tests/gateway/test_acp_attach_config.py tests/acp_adapter
```

Raw log: `../hermes-acp-verification/final-focused.log`.

RED/GREEN was observed for full replay EOF, oversize false send success,
cancelled disconnect retaining listener, unbounded retention/subscribers,
missing v1 capability/identities, missing admission API, huge-final EOF,
full replay + active snapshot EOF, wrong-turn cancellation, live leak during
replay, oversized history EOF, late acceptance downgrading completion,
false success receipt on admission storage failure, fresh-session cursor after
retention, long sequential-tool turn overflow, clarification-as-admission,
and oversized merged tool snapshot / failed-load live leakage.

## Broad regression and attribution

`scripts/run_tests.sh tests/gateway tests/acp_adapter` completed 804 files:
**7992 passed, 5 failed, 48 skipped**, with two files passing only on retry.
The final merged-tool-snapshot hardening was then added and verified in the
82-test focused run above (not another complete 804-file run).
Raw log: `../hermes-acp-verification/final-regression.log`.

Five deterministic failures reproduced **5/5 on both the candidate and an
isolated archive of the base SHA** using the same canonical wrapper:

- `test_buzz_adapter.py::TestInboundMediaAuthorizationGate::test_live_media_redacts_long_path_before_bounding`: macOS path length.
- `test_scale_to_zero.py::test_suspend_self_posts_suspend_for_this_machine`: Unix socket path length.
- `test_scale_to_zero.py::test_suspend_self_non_2xx_is_false_not_raise`: Unix socket path length.
- `test_shutdown_forensics.py::TestSpawnAsyncDiagnostic::test_spawns_subprocess_and_writes_output`: diagnostic unavailable on this host.
- `test_systemd_notify.py::test_notify_supports_systemd_abstract_socket`: Linux abstract socket on macOS.

`test_stream_consumer_wecom_native.py::TestClarifyEagerReseed::test_reopen_seed_opens_stream_before_any_delta`
failed in the first broad run, passed only on retry in the second, and failed
0/5 narrow attempts on each revision. It remains an unattributed timing flake.
`test_buzz_websocket.py::test_websocket_loop_keeps_an_idle_connection_whose_pong_returns`
also passed only on retry in the second broad run; no baseline attribution is
claimed for it. A complete serial suite and remote CI were not run. Do not call
the broad suite green.

Structured attribution and raw attempts: `../hermes-acp-verification/triage.json`
and `base-*.log` / `candidate-*.log`. The archive was removed after testing.

## Contract handoff / remaining boundaries

`docs/acp-gateway-attachment-v1.md` is the exact versioned client contract, with
negotiation, admit/status, load pagination, snapshot/replace, terminal, cancel,
error and Buzz implementation examples. `website/docs/user-guide/features/acp.md`
links the contract and removes the previous unsupported-active-reconnect claims.

The four reproduced transport/durability blockers are fixed. Successful fresh
sessions, two canonical prompts, active reconnect, background wake observation,
durable history-free replay, error/cancel and retained retry identity are tested.

Not an end-to-end Buzz cutover: its harness/desktop still need the negotiated
consumer and durable scope/admission/cursor/outbound bookkeeping. Same-UID Unix
only; no media/client MCP or remote TCP support. No exactly-once delivery or
crash-resume guarantee. Cross-database crash windows, explicit unknown prior-owner
admissions, retention gaps, bounded snapshots and fail-closed admission capacity
are documented rather than disguised as reliable automatic recovery.
