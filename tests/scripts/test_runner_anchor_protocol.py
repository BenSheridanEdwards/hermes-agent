"""Inert protocol contracts. Behavioral execution is held for safety review."""
import json
import struct

import pytest

from scripts import runner_anchor as anchor


class Wire:
    def __init__(self, data):
        self.data = bytearray(data)

    def recv(self, size):
        data = bytes(self.data[:size])
        del self.data[:size]
        return data


def frame(value):
    raw = json.dumps(value).encode()
    return struct.pack('!I', len(raw)) + raw


@pytest.mark.parametrize('payload', [b'', b'\x00', struct.pack('!I', 8) + b'{}',
                                    struct.pack('!I', 1000000),
                                    frame({'type': 'ready'}) + b''])
def test_bounded_frame_rejects_missing_truncated_or_invalid_record(monkeypatch, payload):
    monkeypatch.setattr(anchor.select, 'select', lambda *args: ([args[0][0]], [], []))
    if payload == frame({'type': 'ready'}):
        assert anchor.receive(Wire(payload), 10**20) == {'type': 'ready'}
    elif not payload:
        assert anchor.receive(Wire(payload), 10**20) is None
    else:
        with pytest.raises((ValueError, EOFError)):
            anchor.receive(Wire(payload), 10**20)


def test_duplicate_json_keys_are_not_a_second_identity(monkeypatch):
    raw = b'{"type":"ready","pid":1,"pid":2}'
    monkeypatch.setattr(anchor.select, 'select', lambda *args: ([args[0][0]], [], []))
    with pytest.raises(ValueError):
        anchor.receive(Wire(struct.pack('!I', len(raw)) + raw), 10**20)


@pytest.mark.parametrize('rc', [0, 7, -11])
def test_worker_result_is_independent_of_anchor_exit(rc):
    state = anchor.Protocol(100)
    state.accept({'type': 'ready', 'pid': 100})
    state.accept({'type': 'started', 'pid': 101})
    state.accept({'type': 'result', 'pid': 101, 'returncode': rc})
    assert state.worker_pid == 101 and state.returncode == rc
    state.close_requested = True
    state.accept({'type': 'closing', 'pid': 100, 'result_sent': True})
    assert state.closing and state.returncode == rc


@pytest.mark.parametrize('bad', [
    {'type': 'ready', 'pid': 999},
    {'type': 'started', 'pid': 101},
    {'type': 'result', 'pid': 101, 'returncode': 0},
    {'type': 'closing', 'pid': 100, 'result_sent': False},
])
def test_out_of_order_or_foreign_startup_is_refused(bad):
    with pytest.raises(ValueError):
        anchor.Protocol(100).accept(bad)


@pytest.mark.parametrize('bad', [
    {'type': 'started', 'pid': 102},
    {'type': 'result', 'pid': 999, 'returncode': 0},
    {'type': 'result', 'pid': 101, 'returncode': True},
    {'type': 'result', 'pid': 101, 'returncode': 256},
    {'type': 'result', 'pid': 101, 'returncode': 0, 'extra': 'no'},
])
def test_worker_result_validation_is_fail_closed(bad):
    state = anchor.Protocol(100)
    state.accept({'type': 'ready', 'pid': 100})
    state.accept({'type': 'started', 'pid': 101})
    with pytest.raises(ValueError):
        state.accept(bad)


def test_duplicate_result_and_post_close_result_are_refused():
    state = anchor.Protocol(100)
    state.accept({'type': 'ready', 'pid': 100})
    state.accept({'type': 'started', 'pid': 101})
    result = {'type': 'result', 'pid': 101, 'returncode': 0}
    state.accept(result)
    with pytest.raises(ValueError):
        state.accept(result)
    state.close_requested = True
    state.accept({'type': 'closing', 'pid': 100, 'result_sent': True})
    with pytest.raises(ValueError):
        state.accept(result)


def test_timeout_closure_does_not_invent_worker_result():
    state = anchor.Protocol(100)
    state.accept({'type': 'ready', 'pid': 100})
    state.accept({'type': 'started', 'pid': 101})
    state.close_requested = True
    state.accept({'type': 'closing', 'pid': 100, 'result_sent': False})
    assert state.closing and state.returncode is None


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('parent_dead,spawn_failure', [(False, False), (True, False), (False, True)])
def test_helper_preserves_stdio_receipt_and_requires_parent_death_for_self_signal(
        tmp_path, monkeypatch, parent_dead, spawn_failure):
    import os
    import signal
    from types import SimpleNamespace
    from scripts.runner_scratch import claim_scratch_root, worker_receipt

    lease = claim_scratch_root(str(tmp_path / 'helper')).create_worker()
    receipt = worker_receipt(lease)
    creator = os.getpid()
    messages, launches, closed, signals = [], [], [], []
    argv = [anchor.sys.executable, '-m', 'pytest', 'inert.py', '--runner-owned-scratch=' + receipt]
    requests = iter([{'type': 'launch', 'argv': argv, 'deadline': 100.0}, {'type': 'close'}, None])

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class Watch:
        def __init__(self, pid):
            assert pid == creator

        def exited(self, deadline):
            return parent_dead

        def close(self):
            closed.append('watch')

    def popen(cmd, **kwargs):
        launches.append((cmd, kwargs))
        if spawn_failure:
            raise OSError('original inert spawn failure')
        return SimpleNamespace(pid=1001)

    monkeypatch.setattr(anchor, 'ParentExitWatch', Watch)
    monkeypatch.setattr(anchor.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(anchor.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(anchor, 'receive', lambda *args: next(requests))
    monkeypatch.setattr(anchor, 'send', lambda conn, value, deadline: messages.append(value))
    monkeypatch.setattr(anchor.os, 'getpid', lambda: 1000)
    monkeypatch.setattr(anchor.os, 'getppid', lambda: creator)
    monkeypatch.setattr(anchor.os, 'getsid', lambda pid: 1000)
    monkeypatch.setattr(anchor.os, 'getpgid', lambda pid: 1000)
    monkeypatch.setattr(anchor.os, 'close', lambda fd: closed.append(fd))
    monkeypatch.setattr(anchor.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(anchor.os, 'waitid', lambda kind, pid, flags: SimpleNamespace(
        si_pid=pid, si_code=os.CLD_EXITED, si_status=7))
    monkeypatch.setattr(anchor.subprocess, 'Popen', popen)
    connection = SimpleNamespace(close=lambda: closed.append('control'))
    assert anchor.serve_anchor(connection, 555) == 125  # EOF is not ordinary success.
    assert signals == ([(1000, signal.SIGKILL)] if parent_dead else [])
    cmd, kwargs = launches[0]
    assert cmd[:4] == argv[:4] and json.loads(cmd[4].split('=', 1)[1])['parent'] == 1000
    assert json.loads(receipt)['parent'] == creator, 'original receipt must not mutate'
    assert kwargs['start_new_session'] is False and kwargs['pass_fds'] == (555,)
    assert (kwargs['stdin'], kwargs['stdout'], kwargs['stderr']) == (0, 1, 2)
    assert 'env' not in kwargs, 'worker retains the canonical environment'
    assert 555 in closed and 'watch' in closed and 'control' in closed
    if spawn_failure:
        assert not any(message['type'] in ('started', 'result') for message in messages)
        assert any('original inert spawn failure' in message.get('error', '') for message in messages)
    else:
        assert {'type': 'result', 'pid': 1001, 'returncode': 7} in messages
        assert {'type': 'closing', 'pid': 1000, 'result_sent': True} in messages
        assert all(fd in closed for fd in (0, 1, 2))


@pytest.mark.parametrize('event_kind', ['exit', 'none', 'foreign', 'error'])
def test_parent_exit_watch_requires_bound_kernel_event(monkeypatch, event_kind):
    from types import SimpleNamespace
    calls = []
    constants = dict(KQ_FILTER_PROC=-5, KQ_EV_ADD=1, KQ_EV_ENABLE=4,
                     KQ_EV_ONESHOT=16, KQ_NOTE_EXIT=32, KQ_EV_ERROR=64)
    for key, value in constants.items():
        monkeypatch.setattr(anchor.select, key, value, raising=False)

    class Queue:
        def control(self, changes, count, timeout):
            calls.append((changes, count, timeout))
            if changes is not None or event_kind == 'none':
                return []
            return [SimpleNamespace(ident=999 if event_kind == 'foreign' else 100,
                                    filter=-5, flags=64 if event_kind == 'error' else 0, fflags=32)]

        def close(self):
            calls.append('closed')

    monkeypatch.setattr(anchor.select, 'kqueue', Queue, raising=False)
    monkeypatch.setattr(anchor.select, 'kevent', lambda ident, **kwargs: dict(ident=ident, **kwargs), raising=False)
    monkeypatch.setattr(anchor.time, 'monotonic', lambda: 5.0)
    watch = anchor.ParentExitWatch(100)
    assert calls[0][0][0]['ident'] == 100
    assert calls[0][0][0]['fflags'] & anchor.select.KQ_NOTE_EXIT
    if event_kind in ('foreign', 'error'):
        with pytest.raises(RuntimeError):
            watch.exited(10)
    else:
        assert watch.exited(10) is (event_kind == 'exit')
    watch.close()
    assert calls[-1] == 'closed'
