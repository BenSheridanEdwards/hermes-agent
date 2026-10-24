"""A watch notice the session authority keeps refusing no longer floods the log or the queue.

Live gateways wrote 700-4,200 ``Watch pattern notification — injecting`` lines an hour per profile
(bond, sherlock, irene, jeeves, jarvis, torvalds), enough to evict the agent.log history readers
depend on: the line was logged before admission, so every refused retry logged it again; each
refusal also logged its own line; a refusal that might still clear was retried forever; and two
identical queued copies of one notice were each retried. Now ``injecting`` marks an admitted notice
only, refusals are logged at INFO at most once per interval per notice, a notice refused for longer
than the age cap is dropped with one WARNING naming the refusal, and identical queued copies collapse.

Real GatewayRunner, DiscordAdapter, SessionAuthority and drain over SQLite; no agent turn runs."""
import logging
import queue

import pytest

from tests.gateway.test_watch_notification_reset_pin import (
    _foreign_pin, _gateway, _notice, _queued_admissions, _restart_wait)
from tests.module_clock import install_module_clock

_INJECTING = 'Watch pattern notification — injecting'


async def _drain_for(runner, pending, clock, seconds):
    """Drain once per simulated second, the watcher's cadence, until the queue empties."""
    for _ in range(seconds):
        if pending.empty():
            return
        await runner._drain_watch_notifications(pending)
        clock.now += 1


def _lines(caplog, text, level=logging.DEBUG):
    return [r for r in caplog.records if text in r.getMessage() and r.levelno >= level]


@pytest.mark.asyncio
async def test_injecting_is_logged_only_for_an_admitted_notice(monkeypatch, caplog):
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        refused = queue.Queue()
        refused.put(_notice(entry, await _foreign_pin(runner)))
        clock = install_module_clock(monkeypatch, run_notifications)
        with caplog.at_level(logging.INFO, logger='gateway.run'):
            await _drain_for(runner, refused, clock, 1800)  # 30 simulated minutes of refusals
            assert not refused.empty()
            assert not _lines(caplog, _INJECTING), 'a refused notice was never injected'

            admitted = queue.Queue()
            admitted.put(dict(_notice(entry, entry.session_id), event_id='watch_match-2'))
            await runner._drain_watch_notifications(admitted)

        assert admitted.empty()
        assert len(_lines(caplog, _INJECTING)) == 1
        assert len(_queued_admissions(authority, entry.session_id)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_refusals_are_logged_at_info_once_per_interval(monkeypatch, caplog):
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        pending = queue.Queue()
        pending.put(_notice(entry, await _foreign_pin(runner)))
        clock = install_module_clock(monkeypatch, run_notifications)

        with caplog.at_level(logging.INFO, logger='gateway.run'):
            await _drain_for(runner, pending, clock, 1800)

        lines = _lines(caplog, 'not admitted')
        assert lines and all(r.levelno == logging.INFO for r in lines), [r.levelname for r in lines]
        assert all('admission_conflict' in r.getMessage() for r in lines)
        assert len(lines) <= 4, [r.getMessage() for r in lines]  # not one per retry (~30 in 30 minutes)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_notice_refused_past_the_age_cap_is_dropped_with_one_warning(monkeypatch, caplog):
    """A refusal nothing proves permanent (an owner that may still appear) backs off, but not forever."""
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        pending = queue.Queue()
        pending.put(_notice(entry, await _foreign_pin(runner)))
        clock = install_module_clock(monkeypatch, run_notifications)

        with caplog.at_level(logging.INFO, logger='gateway.run'):
            await _drain_for(runner, pending, clock, 2 * 3600)  # two simulated hours of refusals

        assert pending.empty(), 'a notice refused past the age cap is dropped'
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, warnings
        assert 'dropping the queued event' in warnings[0] and 'admission_conflict' in warnings[0]
        assert not _queued_admissions(authority, entry.session_id)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_notice_outlives_an_adapter_outage_longer_than_the_age_cap(monkeypatch, caplog):
    """The age cap counts refusals only. While no adapter can carry the notice nothing refused it, so
    it keeps backing off however long the outage lasts, and is admitted once the adapter is back."""
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        connected = runner.adapters
        runner.adapters = {}
        pending = queue.Queue()
        pending.put(_notice(entry, entry.session_id))
        clock = install_module_clock(monkeypatch, run_notifications)

        with caplog.at_level(logging.INFO, logger='gateway.run'):
            await _drain_for(runner, pending, clock, 2 * 3600)  # two simulated hours without an adapter
            assert not pending.empty(), 'a notice nothing refused is not dropped by the refusal age cap'

            runner.adapters = connected
            await _drain_for(runner, pending, clock, 120)

        assert pending.empty()
        assert len(_queued_admissions(authority, entry.session_id)) == 1
        assert not [r for r in caplog.records if 'dropping the queued event' in r.getMessage()]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_refused_replacement_tick_is_dropped_naming_its_refusal(monkeypatch, caplog):
    """A liveness tick that would replace a still-queued tick is admitted as a folded copy; when that
    copy is refused, the refusal still reaches the queued tick, so the age cap applies and names it."""
    from gateway import run_notifications
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        pending = queue.Queue()
        pending.put(_notice(entry, entry.session_id, 'heartbeat'))
        await runner._drain_watch_notifications(pending)
        assert pending.empty() and len(_queued_admissions(authority, entry.session_id)) == 1

        _restart_wait(runner)
        pending.put(dict(_notice(entry, entry.session_id, 'heartbeat'), event_id='heartbeat-2', seq=2))
        clock = install_module_clock(monkeypatch, run_notifications)
        with caplog.at_level(logging.INFO, logger='gateway.run'):
            await _drain_for(runner, pending, clock, 2 * 3600)

        assert pending.empty(), 'a tick refused past the age cap is dropped'
        warnings = [r.getMessage() for r in caplog.records if 'dropping the queued event' in r.getMessage()]
        assert len(warnings) == 1 and 'runtime_draining' in warnings[0], warnings
        assert [row['status'] for row in _queued_admissions(authority, entry.session_id)] == ['queued']
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('admissible', [True, False])
async def test_identical_queued_copies_of_a_notice_are_offered_once(monkeypatch, admissible):
    runner, authority, entry = await _gateway(monkeypatch)
    try:
        notice = _notice(entry, entry.session_id if admissible else await _foreign_pin(runner))
        pending = queue.Queue()
        pending.put(dict(notice))
        pending.put(dict(notice))
        offered = []
        inject = runner._inject_watch_notification

        async def counted(*args, **kwargs):
            offered.append(args)
            return await inject(*args, **kwargs)
        monkeypatch.setattr(runner, '_inject_watch_notification', counted)

        await runner._drain_watch_notifications(pending)

        assert len(offered) == 1
        assert pending.qsize() == (0 if admissible else 1)
        assert len(_queued_admissions(authority, entry.session_id)) == (1 if admissible else 0)
    finally:
        authority.db.close()
