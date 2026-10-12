"""Inert Darwin live-leader contracts with consumer-local dependencies.

Adapted from the retained exhausted-child contract: a valid signal to a live
owned anchor is required, not forbidden. Kernel calls and the helper peer are
controlled; owner, protocol reader, leases, consumer and aggregate stay real.
"""
import errno
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def owned_model(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    import psutil
    from scripts import run_tests_parallel as runner
    from scripts import runner_anchor as wire
    from scripts.runner_scratch import claim_scratch_root, worker_receipt

    state = SimpleNamespace(mode='complete', pid=24680, foreign=13579, descendant=24683,
                            rc=0, live=False, events=[], procs={}, peers=[], threads=[],
                            waiting=threading.Event(), timeout=2,
                            stdout='=== 8 passed in 0.01s ===\n', stderr='raw stderr\n')
    # Do not mutate the shared os/threading modules used by pytest, asyncio,
    # multiprocessing or the live enclosing owner. Keep real unrelated APIs.
    monkeypatch.setattr(runner, 'os', SimpleNamespace(**vars(os)))
    runner.os.environ = dict(os.environ)
    monkeypatch.setattr(runner, 'sys', SimpleNamespace(**vars(runner.sys)))
    monkeypatch.setattr(runner, 'tempfile', SimpleNamespace(**vars(runner.tempfile)))

    class ModelThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.threads.append(self)

    for consumer in (runner, wire):
        monkeypatch.setattr(consumer, 'threading', SimpleNamespace(**vars(threading)))
        monkeypatch.setattr(consumer.threading, 'Thread', ModelThread)
    owner = runner._ProcessOwner()
    state.owner, state.runner = owner, runner
    state.wait_condition, state.wait_pids = threading.Condition(), set()
    original_wait = wire.AnchorChannel.wait_result

    def wait_result(channel):
        state.waiting.set()
        with state.wait_condition:
            state.wait_pids.add(channel.protocol.pid)
            state.wait_condition.notify_all()
        return original_wait(channel)

    def peer(proc, control):
        until = time.monotonic() + 10
        try:
            if state.mode == 'startup-missing':
                proc.stopped.wait(10)
                return
            wire.send(control, {'type': 'ready', 'pid': proc.pid}, until)
            launch = wire.receive(control, until)
            assert launch is not None and launch['type'] == 'launch'
            assert (launch['argv'][:3] == [runner.sys.executable, '-m', 'pytest']
                    or launch['argv'][1:3] == [str(Path(runner.__file__).with_name('run_tests.sh')), '--scratch-root'])
            if state.mode == 'worker-spawn-fault':
                wire.send(control, {'type': 'error', 'error': 'inert worker spawn failure'}, until)
                proc.stopped.wait(10)
                return
            wire.send(control, {'type': 'started', 'pid': proc.worker_pid}, until)
            result = {'type': 'result', 'pid': proc.worker_pid, 'returncode': state.rc}
            result_sent = state.mode not in ('timeout', 'cancelled', 'missing-result')
            if result_sent:
                if state.mode == 'malformed-result':
                    result['returncode'] = 'not a status'
                wire.send(control, result, until)
                if state.mode == 'duplicate-result':
                    wire.send(control, result, until)
            if state.mode == 'premature-death':
                proc.returncode = 125
                return
            request = wire.receive(control, until)
            if request is None:
                return
            assert request == {'type': 'close'}
            if state.mode != 'closure-missing':
                wire.send(control, {'type': 'closing', 'pid': proc.pid,
                                    'result_sent': result_sent}, until)
            proc.stopped.wait(10)
        except (OSError, EOFError, TimeoutError):
            # A deliberately failed owner disposition closes the fixture wire.
            pass
        finally:
            control.close()

    class Process:
        def __init__(self, cmd, **kwargs):
            assert kwargs['start_new_session'] is True
            if state.mode == 'before-launch':
                raise OSError('inert before launch')
            self._launch_entered = True
            self.pid = state.pid + len(state.procs) * 10
            self.worker_pid = self.pid + 1
            self.returncode = None
            self.stopped = threading.Event()
            self.anchored = len(cmd) == 7 and Path(cmd[4]).name == 'runner_anchor.py'
            state.procs[self.pid] = self
            if state.mode == 'after-launch':
                raise OSError('inert after launch before established session')
            if state.mode == 'pending-storage':
                owner.nodes[-1]['pending_storage'] = {'allocation:inert': 'unacknowledged storage'}
            if state.mode == 'prior-veto':
                owner.nodes[-1]['cleanup_error'] = 'original veto must survive'
            if not self.anchored:
                self.returncode = state.rc  # Old direct-child lifecycle, red-capable.
                return
            control = socket.socket(fileno=os.dup(int(cmd[5])))
            control.setblocking(False)
            state.peers.append(control)
            thread = threading.Thread(target=peer, args=(self, control), daemon=True)
            state.threads.append(thread)
            thread.start()

        def communicate(self, input=None):
            if state.mode == 'capture-fault':
                raise OSError('inert capture failure')
            return state.stdout, state.stderr

        def poll(self):
            return self.returncode

    def waitid(kind, pid, flags):
        assert kind == os.P_PID and flags == os.WEXITED | os.WNOHANG | os.WNOWAIT
        assert pid in state.procs, 'never witness a foreign PID'
        state.events.append(('witness', pid))
        if state.mode == 'reaped':
            raise ChildProcessError('inert already-reaped anchor')
        proc = state.procs[pid]
        if state.mode == 'wrong-child':
            return SimpleNamespace(si_pid=state.foreign, si_code=os.CLD_EXITED, si_status=0)
        if proc.returncode is None:
            return None
        return SimpleNamespace(si_pid=pid, si_code=os.CLD_KILLED if proc.returncode < 0 else os.CLD_EXITED,
                               si_status=abs(proc.returncode))

    def getsid(pid):
        state.events.append(('sid', pid))
        if pid in state.procs:
            if state.mode == 'foreign-leader':
                return state.foreign
            if state.mode == 'leader-access':
                raise PermissionError(errno.EPERM, 'inert leader lookup refused')
            if state.mode == 'leader-missing':
                raise ProcessLookupError(errno.ESRCH, 'inert leader lookup missing')
            return pid
        for proc in state.procs.values():
            if pid == proc.worker_pid:
                if state.mode == 'missing-result':
                    raise ProcessLookupError(errno.ESRCH, 'terminal worker with missing result')
                return proc.pid
        if state.mode == 'reused-population' and pid in (state.foreign, state.foreign + 1):
            state.events.append(('generation-gap', pid))
            return state.foreign  # Numeric stability is not generation identity.
        if pid == state.descendant:
            return state.pid
        if state.mode == 'lookup-access':
            raise PermissionError(errno.EACCES, 'inert unclassified process')
        if state.mode == 'lookup-gone':
            raise ProcessLookupError(errno.ESRCH, 'inert unclassified process')
        return state.foreign

    def processes(attrs):
        assert attrs == ['pid']
        if state.mode == 'observation-fault':
            raise PermissionError(errno.EACCES, 'inert observation refused')
        if state.mode == 'reused-population':
            for pid in (state.foreign, state.foreign + 1):
                yield SimpleNamespace(pid=pid)
        elif state.mode in ('lookup-access', 'lookup-gone'):
            yield SimpleNamespace(pid=state.foreign)
        elif state.live:
            yield SimpleNamespace(pid=state.descendant)

    def killpg(pid, sig):
        assert pid in state.procs and sig == signal.SIGKILL
        assert ('reap', pid) not in state.events
        state.events.append(('signal', pid))
        proc = state.procs[pid]
        if not proc.anchored or proc.returncode is not None or state.mode == 'signal-refusal':
            raise PermissionError(errno.EPERM, 'original inert signal refusal')
        proc.returncode = -signal.SIGKILL
        proc.stopped.set()
        state.live = False

    def waitpid(pid, flags):
        assert pid in state.procs and flags == 0
        state.events.append(('reap', pid))
        if state.mode == 'reap-fault':
            raise ChildProcessError('inert reap refused')
        return (state.foreign if state.mode == 'wrong-reap' else pid), signal.SIGKILL

    monkeypatch.setattr(runner, '_HeldPopen', Process)
    monkeypatch.setattr(runner, '_process_owner', owner)
    monkeypatch.setattr(runner, '_live_leader_platform', lambda: True, raising=False)
    runner.os.environ.pop(runner._OWNER_ENV, None)
    monkeypatch.setattr(wire, 'HANDSHAKE_SECONDS', 0.2)
    monkeypatch.setattr(wire.AnchorChannel, 'wait_result', wait_result)
    monkeypatch.setattr(runner.os, 'waitid', waitid)
    monkeypatch.setattr(runner.os, 'getsid', getsid)
    monkeypatch.setattr(runner.os, 'getpgid', getsid)
    monkeypatch.setattr(runner.os, 'killpg', killpg)
    monkeypatch.setattr(runner.os, 'waitpid', waitpid)
    original_cleanup_iter = runner._cleanup_iter

    def cleanup_iter(stage, pid, function, *args):
        # _close imports psutil locally. Intercept its observation dependency at
        # the existing runner seam, not on the shared process-inspection module.
        if stage == 'observation.process_iter':
            assert function is psutil.process_iter
            function = processes
        return original_cleanup_iter(stage, pid, function, *args)

    monkeypatch.setattr(runner, '_cleanup_iter', cleanup_iter)
    monkeypatch.setattr(runner, '_session_snapshot', lambda pid: {
        'state': 'partial', 'unresolved': 1, 'members': []})

    def run():
        state.lease = claim_scratch_root(str(tmp_path / 'model')).create_worker()
        return owner.run(cmd=[runner.sys.executable, '-m', 'pytest', 'inert.py',
                              '--runner-owned-scratch=' + worker_receipt(state.lease)],
                         cwd=Path(runner.__file__).resolve().parent.parent, env={}, timeout=state.timeout)

    state.run = run
    try:
        yield state
    finally:
        for proc in list(state.procs.values()):
            proc.stopped.set()
        for control in list(state.peers):
            try:
                control.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            control.close()
        for thread in state.threads:
            if thread.ident is not None:
                thread.join(timeout=30)
        # Join collectors/control readers/dispatchers as well as fixture peers.
        # If quiescence fails, leave only the consumer-local inert dependencies
        # installed: a failed test must never expose fake PIDs to native calls.
        assert not any(thread.is_alive() for thread in state.threads), 'inert fixture thread leaked; fakes retained'
        monkeypatch.undo()


@pytest.mark.platforms('posix')
def test_owned_fakes_leave_unrelated_asyncio_and_os_intact(owned_model):
    import asyncio
    assert owned_model.runner.os.killpg is not os.killpg
    assert owned_model.runner.os.waitpid is not os.waitpid
    assert owned_model.runner.os.environ is not os.environ
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result='unrelated')) == 'unrelated'
    finally:
        loop.close()
    assert loop.is_closed()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('rc', [0, 1, -11])
def test_exhausted_worker_completes_through_live_owned_leader(owned_model, rc):
    model = owned_model
    model.rc = rc
    result = model.run()
    assert result['cleanup_error'] is None
    assert result['returncode'] == rc and result['capture_complete'] is True
    assert (result['stdout'], result['stderr']) == (model.stdout, model.stderr)
    assert ('signal', model.pid) in model.events and ('reap', model.pid) in model.events
    assert model.procs[model.pid].returncode == -signal.SIGKILL
    assert result['returncode'] != model.procs[model.pid].returncode
    assert model.owner.cleanup(result['node'])['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
def test_live_same_session_descendant_still_requires_group_cleanup(owned_model):
    model = owned_model
    model.live = True
    result = model.run()
    assert result['cleanup_error'] is None and not model.live
    assert model.events.index(('signal', model.pid)) < model.events.index(('reap', model.pid))
    assert ('signal', model.foreign) not in model.events
    assert model.owner.cleanup(result['node'])['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', [
    'startup-missing', 'worker-spawn-fault', 'malformed-result', 'duplicate-result',
    'missing-result', 'premature-death', 'closure-missing',
    'reaped', 'wrong-child', 'foreign-leader', 'leader-access', 'leader-missing',
    'lookup-access', 'lookup-gone', 'observation-fault', 'reap-fault', 'wrong-reap',
    'signal-refusal', 'after-launch', 'capture-fault', 'pending-storage', 'prior-veto',
])
def test_uncertain_lifecycle_cannot_delete_original_lease(owned_model, mode):
    model = owned_model
    model.mode = mode
    result = model.run()
    assert result['cleanup_error'], 'uncertainty must stay a cleanup failure'
    assert model.owner.cleanup(result['node'])['cleanup_error']
    assert model.lease.worker.exists()
    assert ('signal', model.foreign) not in model.events
    if mode in ('reaped', 'wrong-child', 'foreign-leader', 'leader-access', 'leader-missing', 'premature-death'):
        assert not any(event[0] == 'signal' for event in model.events)
    if mode not in ('after-launch', 'capture-fault'):
        assert (result['stdout'], result['stderr']) == (model.stdout, model.stderr)
    if mode == 'prior-veto':
        assert 'original veto must survive' in result['cleanup_error']
    if mode == 'signal-refusal':
        assert 'original inert signal refusal' in result['cleanup_error']
        assert model.events.count(('signal', model.pid)) == 1


@pytest.mark.platforms('posix')
def test_proven_no_launch_remains_cleanable(owned_model):
    model = owned_model
    model.mode = 'before-launch'
    result = model.run()
    assert result['returncode'] == 125 and result['launch_failed']
    assert result['cleanup_error'] is None and result['capture_complete']
    assert not model.events
    assert model.owner.cleanup(result['node'])['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
def test_numeric_pid_stability_is_not_live_descendant_completion(owned_model):
    """Retain the rejected census counterexample; never delete a live lineage."""
    model = owned_model
    model.mode, model.live = 'reused-population', True
    result = model.run()
    assert result['cleanup_error'] or not model.live
    if model.live:
        assert model.owner.cleanup(result['node'])['cleanup_error']
        assert model.lease.worker.exists()
    else:
        assert ('signal', model.pid) in model.events


@pytest.mark.platforms('posix')
def test_worker_timeout_reclaims_live_anchor_without_claiming_worker_exit(owned_model):
    model = owned_model
    model.mode = 'timeout'
    result = model.run()
    assert result['returncode'] == 124 and result['timed_out']
    assert result['cleanup_error'] is None
    assert result['observations'][0]['cleanup_facts']['anchor']['worker_returncode'] is None
    assert ('signal', model.pid) in model.events
    assert model.owner.cleanup(result['node'])['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
def test_owner_cancellation_wakes_result_waiter_and_closes_before_delete(owned_model):
    model = owned_model
    model.mode, model.timeout = 'cancelled', 10
    results = []
    thread = threading.Thread(target=lambda: results.append(model.run()))
    model.threads.append(thread)
    thread.start()
    try:
        assert model.waiting.wait(2), 'worker did not enter result wait'
        assert model.owner._close(model.owner.nodes[0]) is None
    finally:
        thread.join(timeout=7)
    assert not thread.is_alive() and len(results) == 1
    assert results[0]['timed_out'] and results[0]['cleanup_error'] is None
    assert ('signal', model.pid) in model.events and ('reap', model.pid) in model.events
    assert model.owner.cleanup(results[0]['node'])['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
def test_registered_anchor_subtree_closes_before_containing_lease(owned_model):
    import shutil
    model = owned_model
    model.mode, model.timeout = 'cancelled', 10
    results = []
    root_thread = threading.Thread(target=lambda: results.append(model.run()))
    model.threads.append(root_thread)
    root_thread.start()
    assert model.waiting.wait(2)
    parent = model.owner.nodes[0]
    runner = model.runner
    cmd = [shutil.which('bash'), str(Path(runner.__file__).with_name('run_tests.sh')),
           '--scratch-root', str(parent['lease'].worker / 'nested-task'), '--files', 'inert.py']
    nested_thread = threading.Thread(target=lambda: results.append(model.owner.run(
        cmd=cmd, cwd=Path(runner.__file__).resolve().parent.parent,
        env={'HERMES_PYTHON': runner.sys.executable}, timeout=10, parent=parent)))
    model.threads.append(nested_thread)
    nested_thread.start()
    try:
        with model.wait_condition:
            assert model.wait_condition.wait_for(lambda: len(model.wait_pids) == 2, timeout=2)
        assert model.owner._close(parent) is None
    finally:
        root_thread.join(timeout=7)
        nested_thread.join(timeout=7)
    assert not root_thread.is_alive() and not nested_thread.is_alive()
    assert len(results) == 2 and all(result['cleanup_error'] is None for result in results)
    assert all(node['closed'] for node in model.owner.nodes)
    assert {pid for event, pid in model.events if event == 'signal'} == set(model.procs)
    assert {pid for event, pid in model.events if event == 'reap'} == set(model.procs)
    assert model.owner.cleanup(0)['cleanup_error'] is None
    assert not model.lease.worker.exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['complete', 'lookup-access', 'signal-refusal', 'duplicate-result'])
def test_real_main_keeps_child_accounting_and_uncertain_cleanup_veto(
        owned_model, tmp_path, monkeypatch, capsys, mode):
    model, runner = owned_model, owned_model.runner
    model.mode = mode
    saved, errors = [], []
    original_finish = runner._finish_owned_attempt

    def finish(*args, **kwargs):
        try:
            return original_finish(*args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise

    monkeypatch.setattr(runner, '_finish_owned_attempt', finish)
    monkeypatch.setattr(runner, '_approximately_count_tests', lambda files, root: {f: 8 for f in files})
    monkeypatch.setattr(runner, '_load_durations', lambda root: {})
    monkeypatch.setattr(runner, '_off_host_marker_files', lambda files: {})
    monkeypatch.setattr(runner, '_save_durations', lambda *args: saved.append(args))
    monkeypatch.setattr(runner, '_FLAKY_RESULTS', [])
    task = tmp_path / 'actual-main'
    # main reads its local sys.argv and passes explicit tokens to argparse.
    # Keep current capture streams without changing the shared sys module.
    runner.sys.stdout, runner.sys.stderr = sys.stdout, sys.stderr
    runner.sys.argv = ['run_tests_parallel.py', '--scratch-root', str(task),
                       '-j', '1', '--file-retries', '2', '--files', 'inert.py', '--', '-q']
    result = runner.main()
    text = capsys.readouterr().out
    assert '8 tests passed' in text and 'NO TESTS RAN' not in text
    assert len(model.owner.nodes) == 1, 'uncertain cleanup never becomes a retry'
    assert 'owned cleanup facts:' in text
    if mode == 'complete':
        assert result == 0 and saved and not errors
        assert 'CLEANUP FAILED' not in text
        assert not model.owner.nodes[0]['lease'].worker.exists()
    else:
        assert result == 1 and not saved and len(errors) == 1
        outcome = errors[0]._runner_child_outcome
        assert outcome['returncode'] == 0 and outcome['stdout'] == model.stdout
        assert outcome['stderr'] == model.stderr and outcome['summary']['passed'] == 8
        assert 'CLEANUP FAILED' in text and model.stderr.strip() in text
        assert model.owner.nodes[0]['lease'].worker.exists()
