"""Inert evidence/accounting regressions. Authored while execution is held."""
import errno
import json
import os
from types import SimpleNamespace

import pytest


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('rc,output,complete,cleanup,expected,unknown,zero', [
    (0, '=== 8 passed, 83 deselected in 0.01s ===\n', True, True, '8 tests passed', False, False),
    (1, 'assertion detail\n=== 1 failed, 7 passed in 0.01s ===\n', True, True, '7 tests passed, 1 failed', False, False),
    (0, 'test_case PASSED\n', False, True, None, True, False),
    (0, '', True, True, None, True, False),
    (0, '=== 8 passed in 0.01s ===\n', False, True, None, True, False),
    (5, '=== 83 deselected in 0.01s ===\n', True, False, None, False, True),
    (5, '=== 83 deselected in 0.01s ===\n', True, True, None, False, True),
    (0, '=== 8 passed in 0.01s ===\n', True, False, '8 tests passed', False, False),
    (1, 'assertion detail\n=== 1 failed, 7 passed in 0.01s ===\n', True, False, '7 tests passed, 1 failed', False, False),
])
def test_owned_cleanup_outcome_reaches_real_aggregate(
        tmp_path, monkeypatch, capsys, rc, output, complete, cleanup, expected, unknown, zero):
    """Real lease, explicit consumer, retry boundary, future callback and main.

    Only launch/cleanup transport and discovery metadata are inert. No child,
    signal, native crash, or live negative target is used by these bodies.
    """
    from scripts import run_tests_parallel as runner
    calls, errors, deletions, saved = [], [], [], []
    original_finish = runner._finish_owned_attempt

    def finish(*args, **kwargs):
        try:
            return original_finish(*args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise

    def run(cmd, *, cwd, env, timeout, input=None, storage_reservation=None):
        calls.append(dict(cmd=cmd, cwd=cwd, env=env, timeout=timeout,
                          input=input, storage_reservation=storage_reservation))
        return dict(returncode=rc, stdout=output, stderr='raw stderr canary\n',
                    timed_out=False, capture_complete=complete, node=0,
                    cleanup_error='inert EPERM cleanup' if cleanup else None,
                    observations=[dict(pid=12345, cleanup_facts={'canary': True})])

    monkeypatch.setattr(runner, '_reserve_storage', lambda: None)
    monkeypatch.setattr(runner, '_owned_run', run)
    monkeypatch.setattr(runner, '_owned_cleanup', lambda result: deletions.append(result))
    monkeypatch.setattr(runner, '_finish_owned_attempt', finish)
    monkeypatch.setattr(runner, '_approximately_count_tests', lambda files, root: {f: 8 for f in files})
    monkeypatch.setattr(runner, '_load_durations', lambda root: {})
    monkeypatch.setattr(runner, '_off_host_marker_files', lambda files: {})
    monkeypatch.setattr(runner, '_save_durations', lambda *args: saved.append(args))
    monkeypatch.setattr(runner, '_FLAKY_RESULTS', [])
    # Restore the caller's process-local temp configuration after main.
    for key in ('TMPDIR', 'TMP', 'TEMP', 'PYTHONDONTWRITEBYTECODE'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    monkeypatch.setattr(runner.tempfile, 'tempdir', runner.tempfile.tempdir)
    monkeypatch.setattr(runner.sys, 'dont_write_bytecode', runner.sys.dont_write_bytecode)
    task = tmp_path / 'aggregate'
    monkeypatch.setattr(runner.sys, 'argv', ['run_tests_parallel.py', '--scratch-root', str(task),
                        '-j', '1', '--file-retries', '2', '--files', 'inert.py', '--', '-q'])
    result = runner.main()
    text = capsys.readouterr().out
    assert result == (0 if rc == 0 and not cleanup else 1)
    expected_attempts = 1 if cleanup or rc in (0, 5) else 1 + 2
    assert len(calls) == expected_attempts, 'only ordinary child failure may enter the configured retry'
    if expected:
        assert expected in text
    assert ('body counts UNKNOWN' in text) is unknown
    assert ('NO TESTS RAN' in text) is zero
    if cleanup:
        assert 'CLEANUP FAILED' in text
        assert 'pytest exited non-zero' not in text
        assert output in text and 'raw stderr canary' in text
        assert 'inert EPERM cleanup' in text and 'owned cleanup facts' in text
        assert not deletions and not saved
        assert task.exists() and any(task.iterdir()), 'original worker storage retained'
        assert len(errors) == 1 and isinstance(errors[0], ExceptionGroup)
        outcome = getattr(errors[0], '_runner_child_outcome')
        assert outcome['returncode'] == rc and outcome['stdout'] == output
        assert outcome['stderr'] == 'raw stderr canary\n'
        assert outcome['capture_complete'] is complete
        assert 'pytest child exited' in str(errors[0].exceptions[0])
        assert str(errors[0].exceptions[1]) == 'inert EPERM cleanup'
        assert bool(outcome['summary'].get('counts_unknown')) is unknown
    else:
        assert not errors and len(deletions) == expected_attempts
        # Capture actual canonical main output, not just the producer's tuple.
        # Every cleanly closed attempt reports facts, even rc0 and retried rc1.
        facts_lines = [line.removeprefix('owned cleanup facts: ') for line in text.splitlines()
                       if line.startswith('owned cleanup facts: ')]
        assert len(facts_lines) == expected_attempts
        assert all(json.loads(line) == [{'pid': 12345, 'facts': {'canary': True}}]
                   for line in facts_lines)
        assert 'CLEANUP FAILED' not in text
        for child in deletions:
            assert child['stdout'] == output
            assert child['stderr'] == 'raw stderr canary\n'
            assert child['returncode'] == rc
        if rc == 0:
            assert saved, 'ordinary clean pass still populates duration cache'
        elif rc == 1:
            assert not saved, 'ordinary child failure is not a clean duration'


@pytest.mark.platforms('posix')
def test_cleanup_call_facts_never_replace_original_fault(monkeypatch):
    from scripts import run_tests_parallel as runner
    calls, facts = [], {}
    fault = PermissionError(errno.EPERM, 'original inert refusal')

    def signal(target, sig):
        calls.append((target, sig))
        raise fault

    def broken_snapshot(pid):
        raise PermissionError(errno.EACCES, 'diagnostic access only')

    monkeypatch.setattr(runner, '_session_snapshot', broken_snapshot)
    with pytest.raises(PermissionError) as caught:
        runner._cleanup_call('killpg', 12345, signal, 12345, 9, evidence=facts)
    assert caught.value is fault and calls == [(12345, 9)]
    assert facts['killpg']['arguments'] == [12345, 9]
    assert facts['killpg']['callable']['qualname'].endswith('signal')
    assert facts['killpg']['callable']['is_posix_killpg'] is False
    assert facts['killpg']['snapshot']['state'] == 'unknown'
    assert facts['killpg']['outcome']['errno'] == errno.EPERM
    assert any('operation=killpg' in note for note in fault.__notes__)
    json.dumps(facts, allow_nan=False)


@pytest.mark.platforms('posix')
def test_cleanup_call_records_existing_wait_and_lookup_values():
    from scripts import run_tests_parallel as runner
    facts, calls = {}, []
    witness = SimpleNamespace(si_pid=12345, si_code=os.CLD_EXITED, si_status=0)

    def wait(*args):
        calls.append(args)
        return witness

    assert runner._cleanup_call('waitid/witness', 12345, wait, 1, 12345, 7,
                                evidence=facts) is witness
    assert calls == [(1, 12345, 7)]
    assert facts['waitid/witness']['outcome']['value'] == {
        'si_pid': 12345, 'si_code': os.CLD_EXITED, 'si_status': 0}
    for operation, value in [('getsid', 12345), ('getpgid', 12345), ('waitpid', (12345, 0))]:
        runner._cleanup_call(operation, 12345, lambda *args: value, 12345, evidence=facts)
        assert facts[operation]['outcome']['value'] == (list(value) if isinstance(value, tuple) else value)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['complete', 'access', 'limit'])
def test_session_snapshot_is_bounded_filtered_and_never_authority(monkeypatch, mode):
    from scripts import run_tests_parallel as runner
    import psutil
    queries = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            queries.append(('status', self.pid))
            assert self.pid in (12345, 12346), 'no unrelated status query'
            return 'zombie' if self.pid == 12345 else 'sleeping'

    def processes(attrs):
        assert attrs == ['pid']
        for pid in ([12345, 12346, 999] if mode != 'limit' else range(10000, 12000)):
            yield Process(pid)

    def session(pid):
        if mode == 'access' and pid == 999:
            raise PermissionError(errno.EPERM, 'inert lookup refusal')
        return 12345 if pid in (12345, 12346) else pid

    monkeypatch.setattr(psutil, 'Process', Process)
    monkeypatch.setattr(psutil, 'process_iter', processes)
    monkeypatch.setattr(runner.os, 'getsid', session)
    monkeypatch.setattr(runner.os, 'getpgid', lambda pid: 12345)
    monkeypatch.setattr(runner.time, 'monotonic', lambda: 0.0)
    facts = runner._session_snapshot(12345)
    assert facts['leader']['status'] == 'zombie'
    assert facts['scanned'] <= 1024
    assert facts['state'] == ('complete_racy' if mode == 'complete' else 'partial')
    if mode != 'limit':
        assert [p['pid'] for p in facts['members']] == [12345, 12346]
    if mode == 'access':
        assert facts['unresolved'] == 1
    if mode == 'limit':
        assert facts['reason'] == 'pid_limit'
    assert ('status', 999) not in queries
    assert 'absence_proven' not in facts
    json.dumps(facts, allow_nan=False)


def test_diagnostic_primitives_do_not_invoke_arbitrary_representation():
    from scripts import run_tests_parallel as runner

    class Opaque:
        def __repr__(self):
            raise AssertionError('must not render an arbitrary diagnostic object')

    value = runner._diagnostic_value({'opaque': Opaque(), 'huge': 'x' * 10000})
    assert value['opaque'] == {'state': 'unknown'}
    assert len(value['huge']) <= 512
    json.dumps(value, allow_nan=False)
