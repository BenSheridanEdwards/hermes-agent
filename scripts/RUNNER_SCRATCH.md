# Explicit canonical-runner scratch, REAL HOME preserved

Explicit scratch is a POSIX-only opt-in to the canonical runner. The bounded
six-file regression manifest passed on macOS with the checkout's locked Python
3.14.7 environment: 151 passed, no failures or skips. Broader default-mode and
cross-platform compatibility remain pending; this is not live-runtime approval.

## Invocation contract

Use `scripts/run_tests.sh --scratch-root /canonical/parent/NEW-TASK ...`.
The parent must exist, be owned by the invoking user, writable and not group/world
writable. NEW-TASK must not exist. The runner claims it with exclusive mkdir(0700).
A repeated or racing invocation fails instead of adopting existing data. No new
Hermes configuration/environment setting is introduced. POSIX ownership is required
for explicit mode. Default root selection remains; directory-only cleanup safety
also applies to default workers. Unsupported no-follow permission repair fails,
rather than chmodding an unverified target; platform compatibility is not yet proven.

Inside the native Hermes home, only an explicitly named task descendant of
`profiles/<profile>/cache/scratch/` qualifies. The base, profile, native home and
siblings are never the exception. The profile name and user are not hardcoded.
Canonical path, UID, private mode, device/inode and worker-child checks fail closed.
No HOME substitution or production state-guard changes are made.

## Canonical flow

1. Shell validates without writing, preserves the existing interpreter-selection
   mechanism and optional `$HOME/.hermes/pytest_live_guard.py` discovery. In explicit
   mode it defers the pytest import until the runner has claimed the task, avoiding
   tempfile fallback while the new directory does not yet exist. Missing dependencies
   fail; explicit mode never syncs or installs an environment.
2. Runner keeps an immutable claim from exclusive task creation and captures each
   worker identity at creation. It verifies ORIGINAL identities before subsequent
   workers, receipt emission, duration output and cleanup. A changed/uncertain path
   is retained and reported as failure, never adopted or deleted. Receipt and spawn
   failures now enter the same worker cleanup as communication/timeout failures.
   It passes a JSON receipt in a reserved pytest argument. Users cannot
   supply that argument through the canonical runner. No environment variable grants
   an I/O exception. Receipt binds actual parent PID, unchanged HOME/native root,
   and private task/worker directory identities.
3. Test conftest consumes that receipt before its temporary allocation. Only those
   temporary paths are retained; an unexpected basetemp fails instead of relocating.
   Existing Hermes-default test isolation starts at collection for explicit mode,
   inside the worker. It does not change HOME, Path.home(), production
   hermes_state_guard._real_platform_state_root(), or the real-home deny-list.
4. HomeIOGuard rejects parent components before normalization, including dir_fd paths,
   and examines the final FOLLOWED inode, including symlink-to-hardlink compositions.
   It admits lexical AND resolved paths inside the unchanged owned task.
   An escape symlink, hardlinked regular file, changed inode, sibling, or destructive
   operation on the task root is rejected. Ancestor traversal permits metadata only,
   not directory listings, file reads or writes. Real native/profile state/auth/config
   outside the task remain denied. Nested explicit deny-models take precedence over
   the task allowance. Kanban's existing test guard consults the same allowance.
   No new bypass marker/variable is introduced. The allowance is TASK-wide, not
   isolation between workers in that task.
5. Existing env scrub, discovery, per-file interpreter/process isolation, timeout,
   and retry remain. Cleanup prepares only owned, same-device directories without
   following links, never chmods leaves, and reports failures/retained workers.
   Explicit mode redirects pytest cache and duration
   output, disables bytecode/precompile, and never sweeps unrelated caller entries.
   Duration output is exclusive-create, so test-created links/files cannot redirect it.
   Task root/output remain for evidence. Default disk-backed behavior stays unchanged.

Default rationale remains relevant: system temporary storage may be Linux tmpfs; hidden ancestors alter
search semantics; long paths can exceed AF_UNIX limits. Explicit mode does not turn
all suites into path-independent tests. The compatibility fixtures below use explicit
nested tasks. Their platform and crash-report limits need independent disposition.

## Historical correction-2 proposal, not execution approval

The current finite correction-4 manifest/environment is in its RESULT reports.
This older invocation is retained for provenance, not a command to replay:

    env -i PATH="$PATH" HOME=/Users/chief \
      TMPDIR=/Users/chief/.hermes/profiles/bond/cache/scratch \
      TMP=/Users/chief/.hermes/profiles/bond/cache/scratch \
      TEMP=/Users/chief/.hermes/profiles/bond/cache/scratch \
      HERMES_PYTHON=/Users/chief/Workspace/Agent-Workspaces/bond/scanner-regression-dev-home/installs/d900ef6b4478af14/test-environment/gen-0448991370234938afe3f7c6ab3102d7/venv/bin/python \
      bash scripts/run_tests.sh \
      --scratch-root /Users/chief/.hermes/profiles/bond/cache/scratch/runner-correction-2-review \
      -j 1 --file-retries 0 --file-timeout 180 \
      tests/scripts/test_runner_scratch.py tests/scripts/test_owned_scratch_boundary.py \
      tests/scripts/test_runner_scratch_review.py tests/test_basetemp_isolation.py \
      tests/test_log_isolation.py

The proposed task name must be absent; if occupied, fail and select a fresh name
through normal review, never clean/adopt it. Source inspection establishes the
location path, not installed dependency readiness. Verifier rechecks interpreter
identity/lock compatibility without installation and records actual child identity.
REAL HOME stays /Users/chief. All temp variables are explicit because the inherited
terminal TMPDIR was reported to point at system /var/folders. Recheck duration
inputs before execution: the existing duration scaler can raise --file-timeout.

## Boundary evidence designed, not executed

Correction-4 also checks trusted installation targets after following aliases,
with nested denies still winning and raw trusted parent traversal refused. SQLite
connect validates decoded file URI targets and sidecars, including positional uri;
mode/cache/immutable are supported, unsupported authority/VFS/query forms fail closed.
Empty temporary SQLite filenames are refused because they have no explicit target.
Owned disk and in-memory positives remain required. Inner-profile classification
retains lexical ancestry independently of physical containment. The liveness helper
owns a separate logging queue, drains it before observation/patch teardown, and
restores prior handlers/listener without closing them. The focused manifest exercises
these contracts. Native race-free confinement and concurrent logging reconfiguration
are not claimed; serial fixture use and unprivileged POSIX cleanup are the test contract.

- Real shell -> runner -> child tests retain REAL HOME, verify production native-root
  identity, use pytest/temp/cache, and check a production-resolved isolated config write.
- Installed HomeIOGuard wrappers exercise write/replace/sqlite/listing denials on an
  inert native/profile/sibling model while leaving the actual real root protected.
- Actual native/profile path denials are predicate-only, never attempted live writes.
- Symlink, hardlink, parent escape, duplicate claim, inode/parent receipt mismatch and
  task replacement tests must fail closed. No fixture-presence assertion substitutes
  for those operations.
- Correction-2 tests add installed-wrapper compound aliases, real Kanban connect,
  original-identity lifecycle/failed-spawn cleanup, receipt/root-shape matrices,
  A/B/A suffix resolution, synthetic-basetemp seam, concurrent claims, duration
  collisions, optional plugin lookup against an inert home argument, and actual
  canonical parallel/retry receipt and task-footprint assertions.
- Collection probe now performs real production logging and spawns a contained
  tempfile/cache child. This does not turn function-scoped guards into an import-time
  sandbox. Plugin startup and full source/venv/native pre/post footprints remain
  executor evidence, as do real timeout/grandchild cleanup and host platform results.
- Compatibility fixture changes below replace the earlier default-runner and bare-pytest
  paths. Their new workload remains unexecuted pending its separate safety gate.

This remains a Python test tripwire, not an OS sandbox against malicious same-user
code or arbitrary native/subprocess writes. Existing open-descriptor/native-library
limits and check/use races are not claimed solved. The runner is the trusted receipt
producer; the receipt is identity binding, not a secret credential. Independent
review must examine this boundary and bind the complete source before execution.

## Contained compatibility candidate, not runtime evidence

The compatibility authoring increment routes every nested runner through the real
canonical shell, using a new exclusively claimed task and the prepared interpreter.
No miniature runner remains, so no copied helper can silently drift or be omitted.
Normal conftest and plugin discovery apply to external generated probes. Witnesses,
retry markers, process receipts, pytest cache and duration outputs live in the new
task; no fixture writes them into a parent worker from a nested worker.

The grandchild test retains real process-group cleanup with no bypass marker. It
records PID/creation/session/group identity, bounds the child to 45 seconds, and
requests failure cleanup through a private stop file rather than a PID-only signal.
Success must precede self-expiry. Original failure and cleanup failure both survive.
The real SIGSEGV contract remains present and unexecuted. OS crash-report writes
cannot be confined by TMPDIR or a Python guard; its workload requires a separate
explicit boundary disposition, not an undocumented filter or a replacement signal.

Kanban negatives now use disposable models with real connect, zero downstream
SQLite calls and preserved bytes/rows/sidecar inventory. The normal native/sibling
I/O guard stays installed. Exported-temp coverage executes the complete actual
conftest body after supplying the export, rather than letting the shell scrub the
input first. An owned-positive case and environment/resolver restoration accompany
that negative. Default scratch selection is called with inert I/O dependencies,
not a globally patched os module or an actual shared temporary directory.

Logging and profile deletion reuse the reviewed queue owner with pytest capture
handlers preserved. Profile deletion resolves a disposable default-home model,
keeps HOME and Path.home intact, and stubs explicit service/process side-effect
seams. This does not verify service teardown or durable identity settlement.
Atomic-writer tests remain unchanged: their concrete targets are already inert.

Open implementation boundary: explicit --scratch-root rejects hosts without
getuid. The new nested launcher therefore cannot preserve native-Windows runner
coverage yet. Do not mark those tests POSIX-only to hide this regression or invoke
an uncontained Windows default path. The frozen runner/lease scope requires an
explicit disposition for a supported Windows containment path before this increment
can be considered cross-platform compatible or merge-ready. No new compatibility
tests have run; correction-4's 151 passes are evidence only for its earlier head.
