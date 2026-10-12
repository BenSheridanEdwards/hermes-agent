# Darwin live-leader lifecycle

This is a source candidate awaiting affected safety review and canonical execution, not a runtime-success claim.

## Loading and authority

Only the explicit `_ProcessOwner.run` path on Darwin selects `scripts/runner_anchor.py`. Linux and other existing POSIX callers retain direct `_HeldPopen` execution. Native Windows supervision remains unsupported. The non-explicit legacy executor is unchanged.

The checked-in helper is launched with the same prepared interpreter, absolute source path and `-I -S -B`. Those flags apply only to the stdlib control helper. The actual pytest or `scripts/run_tests.sh` worker command, environment, normal plugin discovery and guards remain unchanged. This is not a generated launcher or an alternate test entrypoint.

The owner first validates the canonical command and lease exactly as before. The helper receives that command only over a private inherited socket, validates the canonical command class again, and passes only the existing registered-owner endpoint to the worker. The control socket and parent-exit kqueue never enter the worker. No pathname socket, credential transfer, arbitrary PID request, guard exemption or policy setting is introduced.

A pytest receipt is verified against the helper's actual parent, then reissued by `worker_receipt` for the helper's actual worker child. The owner records the worker PID separately from the anchor PID. Nested receipts remain checked against the actual registered worker PID, not the anchor. The worker PID is retained with WNOWAIT inside the anchor until closure, so receipt parentage cannot be assigned to a reused worker PID.

## States and deadlines

1. Registered: the outer owner retains the `_HeldPopen` object before launch entry, as before.
2. Ready: the new session leader registers a Darwin NOTE_EXIT watch for its actual creator and sends its own PID. The owner requires that exact PID.
3. Started: a bounded launch frame conveys only the canonical argv and existing absolute deadline. The helper starts the worker in its own group/session, sends its distinct PID, and closes its own stdin/stdout/stderr copies. The worker owns the original pipe ends.
4. Worker result: a fresh WNOWAIT terminal witness is transported once, independently of output EOF and anchor status. Invalid/missing/duplicate/truncated records fail closed. The helper stays alive and retains the worker's zombie identity.
5. Closing: under the existing subtree lock, the owner seals registered launches and requests closure. The helper serializes the result and closure acknowledgement under one send lock; no result may follow the acknowledgement. The acknowledgement does not exit the anchor.
6. Signal: the owner requires a fresh direct-child witness showing the anchor is still nonterminal, plus matching SID and PGID. An unexpected terminal anchor or any identity/permission failure vetoes cleanup. Timeout without a worker result also requires the worker still belong to that anchored group. Only then does the existing group SIGKILL occur. No errno exemption or per-PID fallback exists.
7. Observe/reap: all registered sessions still pass the existing session observation/poll boundary. The Darwin path refuses unclassified lookup gaps except the members positively covered by successful group signaling. It also requires acknowledged control EOF and the exact anchor reap with SIGKILL status. No inferred census completion is used.
8. Storage: output capture, original lease identity, subtree vetoes and pending storage acknowledgement still gate deletion. Anchor failure is not an ordinary worker failure and cannot enter a clean retry/cache path.

Startup is bounded by the lesser of five seconds and the existing file deadline. Worker result wait uses that file deadline. Closure handshakes, signaling, observation and protocol EOF share five seconds; the existing five-second output capture bound remains. Protocol frames are capped at 65536 bytes. The helper's failure horizon is the file deadline plus ten seconds, not an extension of the owner's job deadline.

## Cancellation and failures

One protocol reader per anchor permits ancestor cancellation while a run thread waits for its worker result. A closure acknowledgement without a worker result wakes that waiter as cancellation, never as test exit zero. Every independently registered session retains its own owner handle and closure path.

Normal success kills and reaps the anchor; its exit status is never substituted for the worker status. Raw worker stdout/stderr remain separate from the bounded protocol facts. Output EOF alone cannot complete a worker. Pipes held open by descendants still trigger the existing capture veto.

On protocol/spawn failure the helper reports the error and remains a live target for legitimate owner cleanup. The owner retains the error even if group reclamation succeeds. On an actual signal denial, closing IPC does not cause the helper to retry while the creator remains alive. Parent death is independently established by the kernel-bound NOTE_EXIT subscription, not by EOF, a PID lookup, or a reparenting timing guess. Only that event permits the helper to signal its own still-live session. It cannot delete storage or turn the failed owner's result into success.

If NOTE_EXIT registration itself fails, startup is not acknowledged and the worker is not launched. If the owning process disappears, its independently registered anchors each have a separate creator-exit subscription. The fallback covers their own session/group only, not arbitrary unrelated processes.

## Explicit limitations

A live anchor is not universal descendant containment. An unregistered process that independently changes session/group is not converted into an owned target by PID scanning. Registered independent sessions are covered by the existing subtree; visible remaining same-session groups veto deletion rather than being adopted and signaled. Observation syscalls retain their existing synchronous/non-preemptible limitations. This candidate does not promise arbitrary hostile-process containment or prove that a complete-looking global census is atomic.

Darwin kqueue registration, real startup/result ordering, EOF, cancellation and native group behavior still require the held canonical enclosing-owner execution after the same Sherlock's finite affected safety review. Inert protocol and owner tests do not substitute for that evidence. Actual signal/observation/reap failures can leave retained processes/storage for legitimate disposition; there is no permission-enforcement workaround.

## Tests and consumers

`test_runner_darwin_completion.py` preserves and adapts the prior regression contract. A valid signal to a live anchor replaces the obsolete no-signal expectation. It uses the real owner, protocol channel, lease gate and main/Future accounting, with inert constructor/kernel/peer seams. It retains the numeric-generation counterexample, distinct worker statuses, timeout/cancellation, independent registered sessions and failure vetoes.

`test_runner_anchor_protocol.py` covers bounded framing, ordering, identity and duplicate handling. Existing R1/R2 producer/consumer logic is unchanged. Direct-Popen diagnostic and outer-contained collector fixtures explicitly select the unchanged direct path; they do not pretend to implement an anchor peer. Existing live compatibility assertions now relate worker PID to reported owned session rather than assuming the worker is the session leader. Their live/crash workloads remain held.
