"""A completion the session authority refuses on every retry is counted, capped and dropped with a
reason, instead of being refunded forever.

A refused async completion settles its durable claim as a ``defer``: the attempt is refunded and
the row stays ``pending``. Each claim/defer pair also refreshed ``updated_at``, so a completion
refused on every retry (on 0.22.1's Studio build every Telegram completion was refused
``admission_conflict``) sat ``pending`` with ``delivery_attempts=0`` for days, out of reach of the
48 h replay cap that only a restart applied, and left no reason behind. Deferrals are now counted on
their own (``delivery_deferrals``, ``last_defer_reason``), the age cap applies where the deferral
happens, and every drop persists its ``drop_reason``.

Real GatewayRunner, DiscordAdapter, SessionAuthority, delivery watcher and SQLite ledger."""
import logging
import queue
import time

import pytest

from tests.gateway.test_watch_notification_reset_pin import (
    _foreign_pin, _gateway, _persist_delegation, _queued_admissions, _run_async_watcher)
from tests.module_clock import install_module_clock
from tests.waiting import async_wait_until


def _completion(monkeypatch, entry, pinned, delegation_id, *, age_s=0.0):
    """Persist a finished delegation pinned to ``pinned`` and put its event on a fresh delivery queue."""
    from tools import async_delegation as delegation
    from tools.process_registry import process_registry
    evt = _persist_delegation(entry, pinned, delegation_id)
    if age_s:
        then = time.time() - age_s
        with delegation._DB_LOCK, delegation._transaction() as conn:
            conn.execute("UPDATE async_delegations SET dispatched_at=?, completed_at=? WHERE delegation_id=?",
                         (then, then, delegation_id))
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    process_registry.completion_queue.put(dict(evt))
    return process_registry.completion_queue


def _newest_human_source(authority, entry):
    """The source of the route's newest committed human admission: what a completion is compared to."""
    from hermes_state_runtime import list_session_admissions
    rows = list_session_admissions(authority.db, session_id=authority.logical_owner(entry.session_id),
                                   pending_only=False)
    human = [row['payload']['native_text_v1'] for row in rows if 'native_text_v1' in row['payload']]
    return human[-1]['source'] if human else {}


def _ledger(delegation_id):
    from tools import async_delegation as delegation
    row = delegation.get_durable_delegation(delegation_id)
    return {key: row[key] for key in (
        'delivery_state', 'delivery_attempts', 'delivery_deferrals', 'last_defer_reason', 'drop_reason')}


@pytest.mark.asyncio
async def test_completion_is_delivered_when_the_human_turn_kept_its_reply_anchor(monkeypatch):
    """Telegram stamps the inbound message id on the source, so the route's committed human envelope
    keeps it while a synthetic completion clears it (#52694). On the Studio build that difference
    alone refused every completion ``admission_conflict`` and parked it in the defer loop; the whole
    delivery path (watcher, pre-flight, claim, admission, acknowledgement) now delivers it."""
    from gateway import run_notifications
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from tools import async_delegation as delegation
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        adapter = runner.adapters[Platform.DISCORD]
        anchored = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', message_id='m2')
        await adapter.handle_message(MessageEvent(text='and run the tests', source=anchored, message_id='m2'))
        await async_wait_until(lambda: _newest_human_source(authority, entry).get('message_id') == 'm2',
                               timeout=15, what='the anchored human turn to commit')
        _completion(monkeypatch, entry, entry.session_id, 'anchored-1')
        clock = install_module_clock(monkeypatch, run_notifications)

        await _run_async_watcher(runner, monkeypatch, clock, ticks=5)

        assert delegation.get_durable_delegation('anchored-1')['delivery_state'] == 'delivered'
        assert len(_queued_admissions(authority, entry.session_id)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_refused_completion_counts_each_deferral_and_logs_it_sparingly(monkeypatch, caplog):
    """A refusal that may still clear (an owner that can still appear, d6635ffe03b) spends no
    delivery attempt, but each deferral is counted with its refusal code, and the INFO line that
    names it is rate-limited (deferral 1, 2, 4, 8, ...) instead of repeating every retry."""
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        _completion(monkeypatch, entry, await _foreign_pin(runner), 'refused-1')
        clock = install_module_clock(monkeypatch, run_notifications)

        with caplog.at_level(logging.INFO, logger='tools.async_delegation'):
            await _run_async_watcher(runner, monkeypatch, clock, ticks=900)  # 15 simulated minutes

        row = _ledger('refused-1')
        assert row['delivery_state'] == 'pending' and row['delivery_attempts'] == 0
        assert row['delivery_deferrals'] >= 8, row
        assert row['last_defer_reason'] == 'admission_conflict' and row['drop_reason'] is None
        lines = [r.getMessage() for r in caplog.records
                 if r.name == 'tools.async_delegation' and 'refused-1' in r.getMessage()]
        assert lines and all('admission_conflict' in line for line in lines)
        assert len(lines) <= row['delivery_deferrals'].bit_length(), lines
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_completion_still_refused_past_the_age_cap_is_dropped_with_its_reason(monkeypatch, caplog):
    """The replay age cap no longer waits for a restart: a completion older than it that is refused
    again converges to ``dropped`` at that deferral, with the reason persisted and one WARNING, and the
    gateway stops offering it."""
    from gateway import run_notifications
    from tools import async_delegation as delegation
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        pending = _completion(monkeypatch, entry, await _foreign_pin(runner), 'stale-1',
                              age_s=delegation._MAX_COMPLETION_REPLAY_AGE_S + 3600)
        clock = install_module_clock(monkeypatch, run_notifications)

        with caplog.at_level(logging.INFO):
            await _run_async_watcher(runner, monkeypatch, clock, ticks=120)

        assert delegation.get_durable_delegation('stale-1')['delivery_state'] == 'dropped', 'refunded forever'
        assert _ledger('stale-1') == {
            'delivery_state': 'dropped', 'delivery_attempts': 0, 'delivery_deferrals': 1,
            'last_defer_reason': 'admission_conflict', 'drop_reason': delegation.DROP_DEFERRAL_AGE_CAP}
        assert pending.empty(), 'a dropped completion is not offered again'
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and 'stale-1' in r.getMessage()]
        assert len(warnings) == 1 and 'admission_conflict' in warnings[0], warnings
    finally:
        authority.db.close()
