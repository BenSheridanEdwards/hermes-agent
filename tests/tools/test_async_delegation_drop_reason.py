"""The delegation ledger records why a completion was never delivered, in the ledger itself.

Every path that ends a row ``dropped`` persists a ``drop_reason``, and a deferral (a claim refunded
without spending a delivery attempt) is counted in ``delivery_deferrals`` with its
``last_defer_reason`` rather than vanishing into ``delivery_attempts``. Before this the reason lived
only in a log line, or nowhere (a deferral logged nothing above DEBUG). The columns are additive: an
existing ledger gains them on its next open and keeps its rows.
"""

import os
import queue
import sqlite3
import time

import psutil
import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _persisted(delegation_id: str, *, completed_at: float | None = None) -> None:
    event = {
        "type": "async_delegation", "delegation_id": delegation_id, "session_key": "agent:main:telegram:dm:1",
        "parent_session_id": "parent", "dispatched_at": time.time(), "status": "completed", "summary": "done",
    }
    if completed_at is not None:
        event["completed_at"] = completed_at
    ad._persist_dispatch(event)
    ad._persist_completion(event, {"status": "completed", "summary": "done"})


def _row(delegation_id: str) -> dict:
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone())


def _set(delegation_id: str, **cols) -> None:
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(f"UPDATE async_delegations SET {', '.join(f'{k}=?' for k in cols)} WHERE delegation_id=?",
                     (*cols.values(), delegation_id))


def _spend_budget(delegation_id: str) -> str:
    """Claim the row with its last delivery attempt, as a consumer about to fail it would."""
    _set(delegation_id, delivery_attempts=ad._MAX_DELIVERY_ATTEMPTS - 1)
    assert ad.claim_completion_delivery(delegation_id, "last")
    return "last"


def _drop_by_release(delegation_id):
    ad.release_completion_delivery(delegation_id, _spend_budget(delegation_id))


def _drop_by_gone_target(delegation_id):
    assert ad.claim_completion_delivery(delegation_id, "claim")
    ad.drop_completion_delivery(delegation_id, "claim")


def _drop_by_startup_replay(delegation_id):
    _set(delegation_id, completed_at=time.time() - ad._MAX_COMPLETION_REPLAY_AGE_S - 60)
    ad.restore_undelivered_completions(queue.Queue())


def _drop_by_orphan_sweep(delegation_id):
    # Its last claimant died holding the final attempt: the owner is gone and the claim lease expired.
    now = time.time()
    _set(delegation_id, delivery_attempts=ad._MAX_DELIVERY_ATTEMPTS, owner_pid=2 ** 22 + 4093, owner_started_at=1,
         updated_at=now - 3600, delivery_claim="dead", delivery_claimed_at=now - 3600)
    ad.sweep_orphaned_completions(queue.Queue(), now=now)


def _drop_by_requeue(delegation_id):
    assert ad.claim_completion_delivery(delegation_id, "claim")
    assert ad.complete_completion_delivery(delegation_id, "claim")
    _set(delegation_id, delivery_attempts=ad._MAX_DELIVERY_ATTEMPTS)
    ad.requeue_admitted_completion(delegation_id, queue.Queue())


def _drop_by_deferral_past_age_cap(delegation_id):
    _set(delegation_id, completed_at=time.time() - ad._MAX_COMPLETION_REPLAY_AGE_S - 60)
    assert ad.claim_completion_delivery(delegation_id, "claim")
    ad.defer_completion_delivery(delegation_id, "claim", reason="admission_conflict")


@pytest.mark.parametrize("drop, reason", [
    (_drop_by_release, ad.DROP_ATTEMPTS_EXHAUSTED),
    (_drop_by_gone_target, ad.DROP_TARGET_GONE),
    (_drop_by_startup_replay, ad.DROP_REPLAY_AGE_CAP),
    (_drop_by_orphan_sweep, ad.DROP_ATTEMPTS_EXHAUSTED),
    (_drop_by_requeue, ad.DROP_ATTEMPTS_EXHAUSTED),
    (_drop_by_deferral_past_age_cap, ad.DROP_DEFERRAL_AGE_CAP),
])
def test_every_drop_persists_why(drop, reason):
    _persisted("dropped-1")

    drop("dropped-1")

    row = _row("dropped-1")
    assert (row["delivery_state"], row["drop_reason"]) == ("dropped", reason)
    assert ad.get_durable_delegation("dropped-1")["drop_reason"] == reason


def test_a_deferral_is_counted_with_its_reason_and_refunds_the_attempt():
    _persisted("deferred-1")
    for reason in ("runtime_draining", "admission_conflict"):
        assert ad.claim_completion_delivery("deferred-1", reason)
        assert ad.defer_completion_delivery("deferred-1", reason, reason=reason)

    row = _row("deferred-1")
    assert (row["delivery_state"], row["delivery_attempts"], row["delivery_deferrals"]) == ("pending", 0, 2)
    assert (row["last_defer_reason"], row["drop_reason"]) == ("admission_conflict", None)
    assert row["delivery_claim"] is None


def test_a_deferral_inside_the_age_cap_never_drops():
    _persisted("young-1", completed_at=time.time() - ad._MAX_COMPLETION_REPLAY_AGE_S + 600)
    assert ad.claim_completion_delivery("young-1", "claim")

    assert ad.defer_completion_delivery("young-1", "claim", reason="runtime_draining")

    assert _row("young-1")["delivery_state"] == "pending"


def test_existing_ledger_gains_the_columns_and_keeps_its_rows():
    """The migration is additive: a ledger written before these columns opens, gains them with
    neutral defaults, and loses nothing."""
    path = ad._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT, state TEXT NOT NULL,
            dispatched_at REAL NOT NULL, completed_at REAL, updated_at REAL NOT NULL, event_json TEXT,
            result_json TEXT, delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0, delivered_at REAL, owner_pid INTEGER,
            owner_started_at INTEGER, task_json TEXT, owner_execution_id TEXT, delivery_claim TEXT,
            delivery_claimed_at REAL, origin_session_id TEXT NOT NULL DEFAULT '')""")
        conn.execute("""INSERT INTO async_delegations(delegation_id, origin_session, state, dispatched_at,
            completed_at, updated_at, delivery_state, delivery_attempts)
            VALUES ('legacy-1', 'agent:main:telegram:dm:1', 'completed', 1.0, 2.0, 3.0, 'dropped', 8)""")
        conn.commit()
    finally:
        conn.close()

    row = ad.get_durable_delegation("legacy-1")

    assert (row["delivery_state"], row["delivery_attempts"]) == ("dropped", 8)
    assert (row["delivery_deferrals"], row["last_defer_reason"], row["drop_reason"]) == (0, None, None)


def _worker_apply(tmp_path):
    """The owner-side handlers a managed worker's ledger proxy reaches (``delegation.*``)."""
    from contextlib import closing
    from gateway.session_worker_delegation import worker_delegation_handlers
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    with closing(sqlite3.connect(tmp_path / "state.db")) as conn:
        ad._initialize_schema(conn)
    handlers = worker_delegation_handlers("worker-a", os.getpid(), psutil.Process().create_time())

    def apply(name, **payload):
        return db._execute_write(lambda conn: handlers["delegation." + name](db, conn, "parent", payload))["value"]

    def row(delegation_id):
        with db._read_ctx() as conn:
            return dict(zip(("state", "attempts", "deferrals", "drop_reason"), conn.execute(
                """SELECT delivery_state, delivery_attempts, delivery_deferrals, drop_reason
                   FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()))

    def persisted(delegation_id, completed_at):
        apply("dispatch", delegation_id=delegation_id, task={"goal": "inert"}, dispatched_at=completed_at,
              parent_session_id="parent", session_key="", origin_ui_session_id="", origin_session_id="")
        apply("complete", event={"type": "async_delegation", "delegation_id": delegation_id,
                                 "parent_session_id": "parent", "session_key": "", "origin_ui_session_id": "",
                                 "origin_session_id": "", "status": "completed", "completed_at": completed_at},
              result={"summary": "done"})
    return db, apply, row, persisted


def test_worker_ledger_counts_deferrals_and_persists_drop_reasons(tmp_path):
    """A managed worker settles through the owner's closed handlers; they follow the same contract."""
    db, apply, row, persisted = _worker_apply(tmp_path)
    try:
        persisted("w-young", time.time())
        persisted("w-stale", time.time() - ad._MAX_COMPLETION_REPLAY_AGE_S - 60)
        persisted("w-gone", time.time())
        persisted("w-spent", time.time())
        for delegation_id in ("w-young", "w-stale"):
            assert apply("claim", delegation_id=delegation_id, claim_id="c")
            assert apply("defer", delegation_id=delegation_id, claim_id="c")
        assert apply("claim", delegation_id="w-gone", claim_id="c")
        assert apply("drop", delegation_id="w-gone", claim_id="c")
        for index in range(ad._MAX_DELIVERY_ATTEMPTS):
            assert apply("claim", delegation_id="w-spent", claim_id=f"c{index}")
            assert apply("release", delegation_id="w-spent", claim_id=f"c{index}")

        assert row("w-young") == {"state": "pending", "attempts": 0, "deferrals": 1, "drop_reason": None}
        assert row("w-stale") == {"state": "dropped", "attempts": 0, "deferrals": 1,
                                  "drop_reason": ad.DROP_DEFERRAL_AGE_CAP}
        assert row("w-gone")["drop_reason"] == ad.DROP_TARGET_GONE
        assert row("w-spent") == {"state": "dropped", "attempts": ad._MAX_DELIVERY_ATTEMPTS, "deferrals": 0,
                                  "drop_reason": ad.DROP_ATTEMPTS_EXHAUSTED}
    finally:
        db.close()
