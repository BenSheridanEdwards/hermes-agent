# Gateway attachment v1: client contract

This contract applies **only** to `hermes acp --attach`, forwarding NDJSON
JSON-RPC to an already-running, opted-in gateway. Standalone `hermes acp` is
unchanged. It is a same-UID local-owner interface on Linux/macOS, not a remote
or multi-tenant API. Do not expose it over TCP. Buzz keeps its existing relay,
owner signing, encryption, and authorization boundaries.

## Negotiation

Send this on every replacement connection, then check the returned version and
all features needed by the consumer. Unknown versions fail with JSON-RPC error
`-32000`; extension admission/status methods refuse unnegotiated connections.
Do not infer support from a process name or an unsolicited notification.

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"_meta":{"hermesAttachment":{"version":1}}}}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentInfo":{"name":"hermes-gateway","version":"1"},"agentCapabilities":{"loadSession":true,"promptCapabilities":{},"mcpCapabilities":{},"_meta":{"hermesAttachment":{"version":1,"features":{"historyFreeReplay":true,"deliveryReplay":true,"activeTurnSnapshot":true,"terminalReceipts":true,"canonicalAsyncWake":true,"retainedAdmission":true}}}},"authMethods":[]}}
```

The examples below use symbolic session `S`, turn `T`, and row IDs. Substitute
returned IDs literally; they are not paths or client-generated session names.

## New session, admission, and the next prompt

```json
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/tmp","mcpServers":[]}}
{"jsonrpc":"2.0","id":2,"result":{"sessionId":"S","_meta":{"lastDeliveryId":0}}}
```

Persist the Buzz scope → `sessionId` mapping **and this initial cursor** before
admitting work. The initial cursor may be nonzero: it fences unrelated earlier
journal retention. New sessions do not automatically subscribe. Admission or a
fully drained `session/load` subscribes. `cwd` is ignored; gateway configuration
owns working directory and tools. Client-provided MCP servers are rejected.
An ambiguously received `session/new` can leave an unused session, but does not
execute a model. Session creation itself is not idempotent.

For durable retry identity use **`_hermes/turn/admit`**, not `session/prompt`:

```json
{"jsonrpc":"2.0","id":3,"method":"_hermes/turn/admit","params":{"sessionId":"S","admissionId":"buzz-event-abc","prompt":[{"type":"text","text":"Do the work"}]}}
{"jsonrpc":"2.0","id":3,"result":{"sessionId":"S","admissionId":"buzz-event-abc","turnId":"T","status":"in_progress"}}
```

Persist `(scope, trigger, sessionId, admissionId, prompt)` before writing the
request. `admissionId` is 1–128 ASCII letters/digits or `_ . : -`, scoped by
session; reuse the **identical prompt blocks**, not merely equivalent joined
text. Hermes commits a claim before canonical admission. Retrying the same
identity returns the retained receipt and never invokes the model again.
Different content with that identity fails `admission_conflict`. A very fast
turn may already return a terminal status instead of `in_progress`.

The response means acceptance, **not completion**. Streamed frames can arrive
before the admission response. Correlate them by session/turn, never by text.
For the next user prompt, wait for terminal completion and admit a new trigger
identity on the **same session**. This reuses the canonical agent/history/cache.
Busy admission fails rather than silently merging or queuing a user prompt.

```json
{"jsonrpc":"2.0","id":4,"method":"_hermes/turn/status","params":{"sessionId":"S","admissionId":"buzz-event-abc"}}
{"jsonrpc":"2.0","id":4,"result":{"sessionId":"S","admissionId":"buzz-event-abc","turnId":"T","status":"completed"}}
```

Statuses: `pending` (claim recorded), `in_progress`, `completed`, `cancelled`,
`error`, `rejected`, `unknown`, `not_found`. Error/rejection receipts include a
bounded `error` string. `not_found` omits `turnId`. After gateway restart a
nonterminal claim from the previous owner returns `unknown` with
`owner_restarted: outcome ambiguous; do not resubmit`. It is **not** re-run.
Terminal receipts survive gateway recreation. There is no model crash-resume
or exactly-once execution/delivery promise. A failure between claim and actual
admission is deliberately conservative. Never convert `unknown` into a new
admission ID automatically. Retained `rejected` identities are not retried as
new work either; any deliberate new attempt is a new user action.

## Live and authoritative final frames

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"S","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Working"}},"_meta":{"turnId":"T","messageId":"T:assistant","kind":"live","operation":"append"}}}
```

Tools have stable `(sessionId, turnId, toolCallId)` identity, a message ID of
`T:tool:CALL`, and `operation: merge`; apply partial fields without erasing
previous input/title/status. A spawned/background tool is not automatically
completed: running/null-exit producer results remain `in_progress`, failures
are `failed`.

Authoritative final text is separate from commentary/deltas. It is durably
journaled before successful terminal publication, and replaces the same
`T:assistant` buffer. It is **not** embedded in terminal metadata:

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"S","_meta":{"kind":"final","operation":"replace","messageId":"T:assistant","part":0,"parts":1,"replay":false,"turnId":"T","deliveryId":17},"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Done"}}}}
{"jsonrpc":"2.0","method":"_hermes/turn_complete","params":{"sessionId":"S","turnId":"T","stopReason":"end_turn","_meta":{"deliveryId":18,"replay":false}}}
```

For multipart `replace`, stage parts by `(sessionId, messageId, kind)` and part
index. Start a new replacement at part zero. Replace atomically after all
`parts` are present; never append a replacement to the previous live buffer.
Parts can span delivery pages. Persist either the staged parts with the cursor,
or keep the checkpoint before part zero until the entire group is durable.
On errors, do not publish an incomplete final group as a successful reply.

Operational notices are durable `session/update` text frames with
`_meta.deliveryId`. Their stable message key is `(sessionId, deliveryId)`;
they need not belong to a model turn. Final frames also carry delivery IDs.
Raw live deltas/tool progress are ephemeral; only final text, terminal receipts,
and local notices are journaled. Final replacements are broadcast live to
negotiated clients; ordinary non-negotiated attachment clients keep append-only
streaming behavior. Ordinary standalone ACP is a different server.

## Reconnect and pagination (observe; do not prompt)

```json
{"jsonrpc":"2.0","id":5,"method":"session/load","params":{"sessionId":"S","cwd":"/tmp","mcpServers":[],"_meta":{"history":false,"afterDeliveryId":18}}}
{"jsonrpc":"2.0","id":5,"result":{"_meta":{"lastDeliveryId":82,"hasMoreDeliveries":true,"replayComplete":false,"historyIncluded":false,"historyTruncated":false,"historyLimit":64}}}
```

Persist received rows, then repeat load with `history:false` and the returned
`lastDeliveryId` until `hasMoreDeliveries:false`. A page is at most 64 rows and
512 KiB of encoded replay frames. Replayed rows retain their IDs and have
`_meta.replay:true`; retain metadata such as `kind:final` and multipart indices.
IDs are global journal integers and can skip numbers for a particular session.
Treat them as opaque ordered cursors, **not** an expected `previous + 1` sequence.
Use an integer representation safe for signed 64-bit IDs.

There is no live subscriber between pages, including when this connection was
previously subscribed. Final-page journal replay, active snapshot, response,
and live subscription are fenced under the delivery lock. The transport drains
replay, reserves room for the bounded snapshot plus response, then enqueues them
and enables live sources without an intervening await. A new live journal row
cannot overtake an older next-page row. Do not send concurrent loads/prompts for
the same session; one logical subscriber per session is supported, and the last
successful final load replaces the previous subscriber.

An active load sends a replacement text snapshot and up to 16 retained tool
snapshots, before its response. For example:

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"S","_meta":{"kind":"snapshot","operation":"replace","messageId":"T:assistant","part":0,"parts":1,"replay":true,"turnId":"T"},"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Working"}}}}
{"jsonrpc":"2.0","id":6,"result":{"_meta":{"lastDeliveryId":82,"hasMoreDeliveries":false,"replayComplete":true,"historyIncluded":false,"historyTruncated":false,"historyLimit":64,"activeTurn":{"turnId":"T","status":"in_progress","snapshotTruncated":false}}}}
```

Tool snapshots use `kind:snapshot`, `operation:replace`, and their tool message
IDs. Older completed tool cards may be evicted from the bounded snapshot;
`activeTurn.snapshotTruncated:true` explicitly reports that omission. Active
cards are not silently evicted. Later live deltas extend the replacement;
a later final replaces it. Disconnect unregisters socket emitters but does not
cancel the canonical worker. Reconnect, query the admission identity, and load.
Do **not** inject synthetic “background completed” prompts. Hermes's canonical
wake/delegation/process machinery owns follow-up model turns, including when
Buzz is absent. Their finals and terminal receipts appear in the same journal.

With `history:true` (default), load first emits a bounded canonical transcript
view with `kind:history`, `operation:replace`, `messageId:history:ROW_ID`, and
`replay:true`. This is **not an outbound reply**. These row IDs are distinct from
attachment turn-message IDs; legacy/native rows have no invented attachment
turn ID. Treat the transcript as a separate restored-history view, not an append
to the live/outbound stream, and never deduplicate legitimate repeated messages
by comparing text. Historical tool cards are not reconstructed. History is a
newest suffix of at most 64 rows/512 KiB; `historyTruncated:true` means incomplete
history (including an oversized row). Subsequent delivery pages should always
set `history:false` to avoid repeating history.

## Cancellation, controls, and errors

```json
{"jsonrpc":"2.0","id":7,"method":"session/cancel","params":{"sessionId":"S","turnId":"T"}}
{"jsonrpc":"2.0","id":7,"result":{}}
{"jsonrpc":"2.0","method":"_hermes/turn_complete","params":{"sessionId":"S","turnId":"T","stopReason":"cancelled","_meta":{"deliveryId":19,"replay":false}}}
```

Cancel is a request to the canonical `/stop` path; its empty response does not
mean the worker has finished. Wait for the correlated receipt/status. A supplied
wrong `turnId` fails `turn_mismatch`, never cancels a replacement turn. A model
failure or observation failure instead emits a terminal `error` with **no**
`stopReason`:

```json
{"jsonrpc":"2.0","method":"_hermes/turn_complete","params":{"sessionId":"S","turnId":"T","error":"provider broke","_meta":{"deliveryId":20,"replay":false}}}
```

`session/prompt` remains a native blocking request, returning
`{"stopReason":"end_turn","_meta":{"turnId":"T"}}`, `cancelled`, or a JSON-RPC
error. A custom receipt must **not** complete a pending native JSON-RPC request;
only its matching response ID does. Do not retry an ambiguously admitted native
prompt; it has no client admission identity. Use `session/prompt` for canonical
`/approve`, `/deny`, and a pending clarification answer; their response acknowledges
the control, not the active model turn's completion. Approval is never bypassed.

## Bounds and recovery boundaries

- 8 clients, 8 pending requests per client, 64 pending RPC requests globally
  including disconnected native prompt owners; 16 subscribed sessions per
  connection, 128 globally. Closed subscribers are removed, not accumulated.
- 256 KiB NDJSON frames; live data has reserved response capacity in the 64-frame
  queue. Nonreading clients are disconnected; replay/writer waits time out after
  five seconds. A subscriber never backpressures the model worker.
- Journal: 4096 rows and 16 MiB serialized message payload, whichever is reached
  first. SQLite overhead/free pages are additional and reused; this is not a
  16 MiB filesystem quota. Pruning commits with the append and advances a single
  global retention floor, not an unbounded per-session tombstone table.
- An old cursor fails explicitly, e.g.
  `{"jsonrpc":"2.0","id":8,"error":{"code":-32000,"message":"replay_gap: cursor 0 precedes retention floor 42"}}`.
  The global floor is conservative: it can reject an old checkpoint even when
  the removed rows belonged to other sessions. Do not advance past a gap and
  claim successful delivery. Surface it for reconciliation using canonical
  history/operator policy. Old oversized journal rows also fail explicitly.
- Notices larger than the 240 KiB encoded delivery budget are rejected **before**
  append/send success. Authoritative final text is split at Unicode character
  boundaries into 8192-character parts. Text/snapshot buffers cap at 262144
  characters; individual tool updates cap at 96 KiB encoded, merged tool snapshots
  at 240 KiB, pending observer callbacks at 64, retained tool cards at 16. Excess active cards or text/update overflow is
  an explicit observation failure; it does not stop canonical execution or
  certify successful delivery. Canonical storage remains the recovery source.
- Admission identity storage caps at 4096 records and rejects new claims with
  `admission_capacity`. It does not silently expire identities and risk executing
  an old trigger again. There is no automatic purge/reset endpoint in v1;
  capacity requires operator-controlled archival/rotation and client coordination.
- Final parts, terminal journal rows, canonical model history, and the admission
  status database are not one distributed transaction. An owner crash can leave
  a partial final or a completed status without a terminal journal row. Retrying
  a retained admission never re-runs work; clients use status + journal, stage
  multipart data, and surface ambiguous/incomplete outcomes. Disk failure may
  prevent even an error receipt from being persisted. No end-to-end exactly-once
  claim is made.
- Gateway restart does not restore active in-memory text/tool snapshots. Native
  async wake recovery remains canonical, not an ACP-client responsibility.
  Native-platform delivery/approval continues through that platform's adapter;
  only local-route background wakes are automatically attached as new observed
  turns. Media prompts, client MCP/file/terminal services, mode/model mutation,
  Windows attachment, and remote TCP attachment are not supported.

## Buzz integration checklist

1. Negotiate v1 and keep existing fail-closed guards when the capability is absent.
2. Durably map scope/trigger → session/admission; retain initial and subsequent
   delivery cursors with staged multipart frames and outbound publication state.
3. Use admission/status for recoverable new work, load for observation, and native
   prompt only for explicit controls or deliberately nonrecoverable native work.
4. Never republish history/snapshots/deltas as separate final messages. Upsert
   live observer cards; publish complete authoritative final groups according to
   Buzz's existing signed/encrypted outbound path. Deduplicate receipts by turn
   and journal rows by session/delivery ID, including native-response overlap.
5. Advance the replay checkpoint only after durable consumer acceptance, not
   after raw observer enqueue. Relay publication and the Hermes journal have no
   shared transaction; Buzz must implement its own durable delivery state.
6. Handle success, cancellation, error, unknown admission, truncation and replay
   gaps visibly. Keep reading after a load response to receive live frames.
7. Do not cut over deployments merely because these server tests pass: the Buzz
   harness/desktop consumers still need implementation and an end-to-end test.

## Verification

Run via `scripts/run_tests.sh` with a temporary `HERMES_HOME` (the canonical
wrapper supplies isolation). `tests/gateway/test_acp_attachment_contract.py`
uses the actual Unix listener, canonical `GatewayRunner`, stores and adapter;
only model execution is doubled in the model-path tests. It covers fresh/two
prompts, active reconnect and idempotent admission, history-free pagination,
64-row replay plus large active snapshots, Unicode final replay, oversized send
rejection, cancelled shutdown, subscriber cleanup/caps, retention gaps,
error/cancel correlation, restart ambiguity and canonical background wakes.
`test_acp_admission.py` exercises retained status and storage bounds. Existing
`test_acp_attach_*.py`, `test_acp_delivery.py`, and `tests/acp_adapter/` cover
CLI forwarding, ordinary ACP and earlier gateway attachment regressions.
