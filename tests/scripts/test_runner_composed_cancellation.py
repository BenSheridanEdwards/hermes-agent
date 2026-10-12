"""Inert composed cancellation regressions, held for native safety review.

Pre-fix leaks must not be executed just to manufacture a RED receipt.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager

import psutil
import pytest

from tests.scripts.fixtures.compatibility_runner import command, ProbeTimeout, run, run_command


def _alive(receipt):
    try:
        return psutil.Process(receipt['pid']).create_time() == receipt['created']
    except psutil.NoSuchProcess:
        return False


def _running(receipt):
    return _alive(receipt) and psutil.Process(receipt['pid']).status() != psutil.STATUS_ZOMBIE


def _leaf(path, *, sibling=False):
    path.write_text(
        'import json, os, time, psutil\nfrom tests.scratch_isolation import LEASE\n'
        'def test_wait():\n'
        '    data = dict(pid=os.getpid(), created=psutil.Process().create_time(), '
        'session=os.getsid(0), worker=str(LEASE.worker))\n'
        '    print("\\nCANCEL_LIVE " + json.dumps(data), flush=True)\n'
        "    receipt = LEASE.task / f'ready-{os.getpid()}.tmp'\n"
        "    receipt.write_text(json.dumps(data))\n"
        "    receipt.replace(LEASE.task / 'ready.json')\n"
        '    deadline = time.monotonic() + 50\n'
        "    while time.monotonic() < deadline and not (LEASE.task / 'stop').exists(): time.sleep(0.05)\n"
        + ("    assert (LEASE.task / 'stop').exists(), 'sibling must finish by owned stop request'\n" if sibling else ''),
        encoding='utf-8')


@contextmanager
def _unregistered_canary(parent):
    """Not a broker job or sibling owner node. Its creator retains the Popen.

    It inherits this enclosing test's session, outside every cancelled nested
    session. Thus even cancellation of the creator has the existing outer group
    fallback, rather than introducing an orphan escape hatch for a negative.
    """
    ready = parent / 'unowned-ready'
    child = subprocess.Popen(
        [sys.executable, '-I', '-S', '-B', '-c',
         'import os, pathlib, sys, time; '
         'p=pathlib.Path(sys.argv[1]); q=p.with_suffix(".tmp"); '
         'q.write_text(str(os.getpid())); q.replace(p); time.sleep(35)', str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        cwd=parent, close_fds=True)
    primary = None
    identity = None
    try:
        until = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert int(ready.read_text()) == child.pid
        identity = dict(pid=child.pid, created=psutil.Process(child.pid).create_time(),
                        session=os.getsid(child.pid))
        assert identity['session'] == os.getsid(0)
        yield identity
    except BaseException as error:
        primary = error
    finally:
        try:
            # This creator alone waits/reaps. A live or zombie direct child
            # therefore pins the PID; Popen.kill does not signal a reaped PID.
            child.kill()
            child.communicate(timeout=5)
            if identity is not None:
                assert not _alive(identity)
        except BaseException as cleanup:
            if primary is not None:
                raise BaseExceptionGroup('canary primary and creator cleanup', [primary, cleanup])
            raise
    if primary is not None:
        raise primary


@pytest.fixture(autouse=True)
def _native_launcher(real_bash, monkeypatch):
    from tests.scripts.fixtures import compatibility_runner
    monkeypatch.setattr(compatibility_runner, '_BASH', real_bash)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['direct', 'outer', 'retry', 'multi'])
def test_cancellation_reclaims_nested_sessions_before_storage(tmp_path, mode, record_property):
    leaf = tmp_path / 'test_leaf.py'
    _leaf(leaf)
    supervisor = tmp_path / 'test_supervisor.py'
    supervisor.write_text(
        'from tests.scripts.fixtures.compatibility_runner import run\n'
        'from tests.scratch_isolation import LEASE\n'
        'def test_nested():\n'
        f'    run(LEASE.worker, "--files", {str(leaf)!r}, "-s", timeout=60)\n', encoding='utf-8')
    selected = supervisor if mode == 'outer' else leaf
    args = ['--files', str(selected), '-s']
    first = tmp_path / 'test_first.py'
    if mode == 'retry':
        first.write_text('def test_fail():\n    assert False, "first-attempt retained"\n')
        args = ['--files', os.pathsep.join([str(first), str(leaf)]), '--file-retries', '1', '-s']
    if mode == 'multi':
        _leaf(first)
        args = ['--files', os.pathsep.join([str(first), str(leaf)]), '-j', '2', '-s']
    started = time.monotonic()
    if mode == 'outer':
        # Actual enclosing canonical pytest's 30s file deadline expires while
        # its nested canonical worker is alive. No helper cancellation instead.
        result = run(tmp_path, *args, timeout=60)
        assert result.returncode != 0
        assert 'owned process deadline' in result.stdout + result.stderr
        evidence = result.ownership
    else:
        with pytest.raises(ProbeTimeout) as failure:
            run(tmp_path, *args, timeout=12)
        evidence = failure.value.ownership
    assert not evidence['cleanup_error'], evidence
    assert all(row['deadline'] <= evidence['deadline'] for row in evidence['observations'])
    identities = []
    for row in evidence['observations']:
        if row['test_file'] not in {str(leaf), str(first)}:
            continue  # Do not count the same forwarded stdout again in supervisors.
        for line in row['stdout'].splitlines():
            if line.startswith('CANCEL_LIVE '):
                identities.append(json.loads(line.removeprefix('CANCEL_LIVE ')))
    # Actual test-body handshake, not merely Popen flags or launcher existence.
    assert identities, evidence
    assert all(identity['session'] == identity['pid'] for identity in identities)
    assert all(not _alive(identity) for identity in identities), evidence
    assert time.monotonic() - started < 50, 'self-expiry is not cancellation proof'
    record_property('cancelled_identities', json.dumps(identities))
    if mode == 'outer':
        assert all(not Path(identity['worker']).exists() for identity in identities)
    if mode == 'multi':
        assert {row['test_file'] for row in evidence['observations'] if row['test_file']} == {str(first), str(leaf)}
        assert len({identity['pid'] for identity in identities}) == 2
        leaves = [row for row in evidence['observations'] if row['test_file']]
        assert max(row['started'] for row in leaves) < min(row['closed_at'] for row in leaves)
    if mode == 'retry':
        attempts = [row for row in evidence['observations'] if row['test_file'] == str(first)]
        assert len(attempts) == 2
        assert attempts[0]['closed_at'] <= attempts[1]['started']
        assert all(row['deadline'] <= evidence['deadline'] for row in attempts)
        assert any('first-attempt retained' in row['stdout'] for row in attempts)


@pytest.mark.platforms('posix')
def test_cancellation_preserves_live_sibling_scope(tmp_path):
    sibling = tmp_path / 'test_sibling.py'
    _leaf(sibling, sibling=True)
    argv = command(tmp_path, '--files', sibling, '-s')
    task = Path(argv[3])
    leaf = tmp_path / 'test_cancelled.py'
    _leaf(leaf)
    with _unregistered_canary(tmp_path) as unowned, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_command, argv, timeout=60)
        try:
            until = time.monotonic() + 5
            while not (task / 'ready.json').exists() and time.monotonic() < until:
                time.sleep(0.05)
            identity = json.loads((task / 'ready.json').read_text())
            assert _running(unowned)
            with pytest.raises(ProbeTimeout) as failure:
                run(tmp_path, '--files', leaf, '-s', timeout=6)
            observations = failure.value.ownership['observations']
            assert any(row['test_file'] == str(leaf) and 'CANCEL_LIVE ' in row['stdout']
                       for row in observations), 'cancellation must reach an actually running leaf'
            assert unowned['pid'] not in {row['pid'] for row in observations}
            assert unowned['session'] not in {row['session'] for row in observations}
            assert _running(unowned), 'cancellation reached an unregistered creator-owned canary'
            assert _running(identity), 'cancellation crossed into its sibling owner'
        finally:
            if task.exists():
                (task / 'stop').touch()
        result = future.result(timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not _alive(identity)


@pytest.mark.platforms('posix')
def test_cleanup_failure_keeps_primary_and_storage(tmp_path):
    from scripts import run_tests_parallel as runner

    # Fault injection covers the deletion gate and error aggregation, never a
    # mock substituted for the real process effects in the tests above.
    canary = tmp_path / 'owned-worker'
    canary.mkdir()
    primary = TimeoutError('primary deadline')
    cleanup = RuntimeError('identity uncertain; residue retained')
    with pytest.raises(BaseExceptionGroup) as failure:
        runner._finish_owned_attempt(primary, cleanup, canary.rmdir)
    assert failure.value.exceptions == (primary, cleanup)
    assert canary.is_dir()
    runner._finish_owned_attempt(None, None, canary.rmdir)
    assert not canary.exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('failure_kind', ['process', 'storage'])
def test_nonzero_attempt_preserves_output_with_cleanup_failure(tmp_path, monkeypatch, failure_kind):
    from scripts import run_tests_parallel as runner
    from scripts.runner_scratch import claim_scratch_root

    claim = claim_scratch_root(str(tmp_path / 'task'))
    monkeypatch.setattr(runner, '_owned_run', lambda *a, **kw: dict(
        returncode=1, stdout='primary assertion witness', stderr='primary stderr witness',
        timed_out=False, cleanup_error='process residue' if failure_kind == 'process' else None))
    calls = []

    def fail_storage(*args):
        calls.append('delete')
        raise OSError('storage residue')

    monkeypatch.setattr(runner, '_cleanup_worker', fail_storage)
    with pytest.raises(BaseExceptionGroup) as caught:
        runner._run_one_file_once(Path('inert.py'), [], tmp_path, 30, claim)
    first, second = caught.value.exceptions
    assert 'primary assertion witness' in str(first)
    assert 'primary stderr witness' in str(first)
    assert f'{failure_kind} residue' in str(second)
    assert calls == ([] if failure_kind == 'process' else ['delete'])
    assert len(list(claim.task.iterdir())) == 1


@pytest.mark.platforms('posix')
def test_helper_retains_nonzero_output_with_cleanup_error(tmp_path, monkeypatch):
    from scripts import run_tests_parallel as runner
    from tests.scripts.fixtures import compatibility_runner as helper

    monkeypatch.setattr(runner, '_owned_run', lambda *a, **kw: dict(
        returncode=1, stdout='helper primary stdout', stderr='helper primary stderr',
        timed_out=False, cleanup_error='owned residue'))
    with pytest.raises(ExceptionGroup) as caught:
        helper.run(tmp_path, '--help')
    primary, cleanup = caught.value.exceptions
    assert 'helper primary stdout' in str(primary)
    assert 'helper primary stderr' in str(primary)
    assert 'owned residue' in str(cleanup)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('bad', ['launcher', 'location', 'activation', 'implicit'])
def test_owned_dispatch_rejects_noncanonical_requests_without_spawn(tmp_path, monkeypatch, real_bash, bad):
    from types import SimpleNamespace
    from scripts import run_tests_parallel as runner
    from scripts.runner_scratch import claim_scratch_root
    from tests.scripts.fixtures import compatibility_runner as helper

    lease = claim_scratch_root(str(tmp_path / 'task')).create_worker()
    parent = dict(proc=SimpleNamespace(pid=os.getpid(), poll=lambda: None),
                  closing=False, deadline=time.monotonic() + 30, lease=lease, task=lease.task)
    argv = helper.command(lease.worker, '--help')
    env = helper.environment()
    if bad == 'launcher':
        argv[0] = sys.executable
    elif bad == 'location':
        argv[3] = str(tmp_path / 'outside-owner')
    elif bad == 'activation':
        env['__HERMES_ACTIVATED'] = '1'
    else:
        argv[2:4] = []
    launches = []

    def forbidden_spawn(*args, **kwargs):
        launches.append(args)
        raise AssertionError('refused request reached Popen')

    monkeypatch.setattr(runner, '_HeldPopen', forbidden_spawn)
    with pytest.raises(ValueError):
        runner._ProcessOwner().run(cmd=argv, cwd=helper.ROOT, env=env, timeout=10, parent=parent)
    assert not launches
