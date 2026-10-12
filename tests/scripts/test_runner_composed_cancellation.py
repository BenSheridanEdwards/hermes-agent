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

    monkeypatch.setattr(runner, '_owned_cleanup', fail_storage)
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


@contextmanager
def _inert_owner(monkeypatch, tmp_path, *, constructor_fault=False, capture_fault=False, nested_refusal=False):
    """Real Popen/collector/IPC, inert child in the enclosing test session.

    No second session owner can escape outer cancellation. The fixture alone
    reaps these direct children; the production group closer is not exercised
    by this seam test (the cancellation tests above exercise that separately).
    """
    from multiprocessing import Pipe
    import threading
    from scripts import run_tests_parallel as runner
    from scripts.runner_scratch import claim_scratch_root
    from types import SimpleNamespace

    owner = runner._ProcessOwner()
    lease = claim_scratch_root(str(tmp_path / 'inert-owner')).create_worker()
    parent: dict = dict(proc=SimpleNamespace(pid=os.getpid(), poll=lambda: None), parent=None,
                  lease=lease, task=lease.task, deadline=time.monotonic() + 30,
                  closing=False, closed=False, cleanup_error=None)
    owner.nodes.append(parent)
    created = []
    original_init = runner._HeldPopen.__init__
    original_execute = runner._HeldPopen._execute_child
    original_communicate = runner._HeldPopen.communicate
    endpoint = threading.local()
    endpoint_lock = threading.Lock()

    def close_endpoint(proc):
        with endpoint_lock:
            fd = getattr(proc, '_test_endpoint', None)
            proc._test_endpoint = None
        if fd is not None:
            os.close(fd)

    def inert_init(proc, cmd, **kwargs):
        created.append(proc)  # Own fixture descriptors even if construction fails.
        # These bytes are a canary, never pytest or a substitute executor.
        code = ('import os,time; os.write(1,b"primary stdout\\n"); '
                'os.write(2,b"primary stderr\\n"); time.sleep(0.1); raise SystemExit(1)')
        if constructor_fault:
            code = 'import time; time.sleep(15)'
        if nested_refusal and cmd[:3] != [sys.executable, '-m', 'pytest']:
            proc._test_task = Path(cmd[3])
            proc._test_endpoint = os.dup(kwargs['pass_fds'][0])
            code = ('import os,select,time; from pathlib import Path\n'
                    'os.write(1,b"runner failure propagation canary\\n")\n'
                    'os.write(2,b"shell primary stderr\\n")\n'
                    'until=time.monotonic()+15; data=bytearray()\n'
                    'while time.monotonic()<until:\n'
                    '    if not select.select([0],[],[],max(0,until-time.monotonic()))[0]: break\n'
                    '    chunk=os.read(0,8192)\n'
                    '    if not chunk: break\n'
                    '    data.extend(chunk)\n'
                    f'Path({str(proc._test_task / "executed")!r}).write_text("executed")\n'
                    'print(data.decode(),flush=True)\n'
                    'raise SystemExit(1)')
        kwargs.update(start_new_session=False, pass_fds=())
        original_init(proc, [sys.executable, '-I', '-S', '-B', '-c', code], **kwargs)

    def communicate(proc, input=None, **kwargs):
        if not hasattr(proc, '_test_task'):
            return original_communicate(proc, input=input, **kwargs)
        # Run the actual nested caller in a thread instead of another test
        # executor. Only OS-parent receipt stamping is modeled for this thread;
        # the receiver, endpoint ancestry, transport and deletion are real.
        endpoint.fd, endpoint.pid = proc._test_endpoint, proc.pid
        try:
            claim = claim_scratch_root(str(proc._test_task))
            try:
                runner._run_explicit_file_once(Path('inert.py'), [],
                    Path(runner.__file__).resolve().parent.parent, 30, claim)
            except BaseException as error:
                input = repr(error)
            else:
                input = 'missing expected infrastructure refusal'
        finally:
            close_endpoint(proc)
            del endpoint.fd, endpoint.pid
        return original_communicate(proc, input=input, **kwargs)

    if nested_refusal:
        from multiprocessing.connection import Connection
        from scripts import runner_scratch
        original_exchange = runner._owner_exchange
        original_receipt = runner_scratch.worker_receipt

        def exchange(message):
            if not hasattr(endpoint, 'fd'):
                return original_exchange(message)
            connection = Connection(os.dup(endpoint.fd))
            try:
                connection.send_bytes(json.dumps(message).encode())
                return json.loads(connection.recv_bytes())
            finally:
                connection.close()

        def receipt(lease):
            raw = original_receipt(lease)
            if not hasattr(endpoint, 'pid'):
                return raw
            data = json.loads(raw)
            data['parent'] = endpoint.pid
            return json.dumps(data)

        monkeypatch.setattr(runner, '_owner_exchange', exchange)
        monkeypatch.setattr(runner_scratch, 'worker_receipt', receipt)
        monkeypatch.setattr(runner._HeldPopen, 'communicate', communicate)

    def execute(proc, *args, **kwargs):
        original_execute(proc, *args, **kwargs)
        if constructor_fault:
            assert proc._child_created and proc.pid
            proc._test_identity = dict(pid=proc.pid, created=psutil.Process(proc.pid).create_time())
            assert os.getsid(proc.pid) == os.getsid(0), 'inert child must remain outer-contained'
            assert _running(proc._test_identity)
            raise OSError('inert post-child constructor fault')

    def close_direct(node):
        # Retained unreaped direct child, no PID lookup/name scan. Group closure
        # is inappropriate here because this inert child shares the test group.
        proc = node['proc']
        if not node['closed']:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            os.waitpid(proc.pid, 0)
            node['closed'] = True
            node['closed_at'] = time.monotonic()
        return node['cleanup_error']

    monkeypatch.setattr(runner._HeldPopen, '__init__', inert_init)
    monkeypatch.setattr(runner._HeldPopen, '_execute_child', execute)
    monkeypatch.setattr(owner, '_close', close_direct)
    if capture_fault:
        def fail_translation(*args):
            raise OSError('inert collector failure after bytes')
        monkeypatch.setattr(runner._HeldPopen, '_translate_newlines', fail_translation)
    server, client = Pipe()
    service = None
    primary = None
    errors = []
    try:
        service = threading.Thread(target=owner._serve, args=(server, parent), daemon=True)
        service.start()
        monkeypatch.setenv(runner._OWNER_ENV, str(client.fileno()))
        yield runner, owner, parent, created
    except BaseException as error:
        primary = error
    finally:
        try:
            client.close()
            if service is not None and service.ident is not None:
                service.join(timeout=5)
                if service.is_alive():
                    raise RuntimeError('inert owner service did not close')
            else:
                server.close()
        except BaseException as error:
            errors.append(error)
        # Continue legitimate creator cleanup even if another resource failed.
        for node in owner.nodes[1:]:
            try:
                if getattr(node['proc'], '_child_created', False) and not node['closed']:
                    close_direct(node)
            except BaseException as error:
                errors.append(error)
        active_readers = set()
        for node in owner.nodes[1:]:
            reader = node.get('reader')
            if reader is not None and reader.ident is not None:
                reader.join(timeout=5)
                if reader.is_alive():
                    active_readers.add(node['proc'])
                    errors.append(RuntimeError('inert output reader did not close; descriptors retained'))
        for proc in created:
            if proc in active_readers:
                continue  # Never block on a live reader's stream lock or reuse its endpoint FD.
            try:
                close_endpoint(proc)
            except BaseException as error:
                errors.append(error)
            for name in ('stdin', 'stdout', 'stderr'):
                try:
                    stream = getattr(proc, name, None)
                    if stream is not None and not stream.closed:
                        stream.close()
                except BaseException as error:
                    errors.append(error)
    if errors:
        raise BaseExceptionGroup('inert fixture primary and cleanup', ([primary] if primary else []) + errors)
    if primary is not None:
        raise primary


@pytest.mark.platforms('posix')
def test_post_child_constructor_failure_retains_handle_and_ancestor_veto(tmp_path, monkeypatch):
    from tests.scripts.fixtures import compatibility_runner as helper
    with _inert_owner(monkeypatch, tmp_path, constructor_fault=True) as (_, owner, parent, created):
        started = time.monotonic()
        with pytest.raises(ExceptionGroup) as failure:
            helper.run(parent['lease'].worker, '--help', timeout=10)
        assert 'post-child constructor fault' in repr(failure.value)
        assert len(created) == 1 and created[0]._child_created
        assert not _alive(created[0]._test_identity)
        assert time.monotonic() - started < 15, 'self-expiry is not constructor reclamation'
        child = owner.nodes[-1]
        assert child['proc'] is created[0] and child['parent'] is parent
        assert child['closed'] and child['cleanup_error']
        assert parent['cleanup_error'], 'constructor uncertainty must reach containing deletion'
        parent['finished'] = True
        assert owner.cleanup(0)['cleanup_error']
        assert parent['lease'].worker.exists()


@pytest.mark.platforms('posix')
def test_collector_failure_preserves_observed_bytes_at_real_helper(tmp_path, monkeypatch):
    from tests.scripts.fixtures import compatibility_runner as helper
    with _inert_owner(monkeypatch, tmp_path, capture_fault=True) as (_, owner, parent, _):
        with pytest.raises(ExceptionGroup) as failure:
            helper.run(parent['lease'].worker, '--help', timeout=10)
        primary, cleanup = failure.value.exceptions
        assert 'primary stdout' in str(primary) and 'primary stderr' in str(primary)
        assert 'collector failure after bytes' in str(cleanup)
        assert 'capture incomplete' in str(cleanup)
        assert owner.nodes[-1]['capture_complete'] is False
        assert parent['cleanup_error'] and parent['lease'].worker.exists()


@pytest.mark.platforms('posix')
def test_nested_storage_refusal_cannot_pass_expected_shell_failure(tmp_path, monkeypatch, real_bash):
    from tests.scripts.test_run_tests_shell import test_shell_runner_executes_tests_and_propagates_failure

    with _inert_owner(monkeypatch, tmp_path, nested_refusal=True) as (runner, owner, parent, _):
        refused = []
        def refuse(temp, lease):
            refused.append(lease)
            (lease.worker / 'retained-primary').write_text('owned evidence')
            raise OSError('inert nested storage refusal')
        monkeypatch.setattr(runner, '_cleanup_worker', refuse)
        # This is the actual expected-failure consumer, not a success-response
        # mock. It must throw before accepting nonzero + assertion substring.
        with pytest.raises(ExceptionGroup) as failure:
            test_shell_runner_executes_tests_and_propagates_failure(
                parent['lease'].worker, real_bash, monkeypatch)
        primary, cleanup = failure.value.exceptions
        assert 'runner failure propagation canary' in str(primary)
        assert 'primary stdout' in str(primary) and 'primary stderr' in str(primary)
        assert 'shell primary stderr' in str(primary)
        assert 'inert nested storage refusal' in str(cleanup)
        assert len(refused) == 1 and refused[0].worker.exists()
        assert len(owner.nodes) == 3, 'enclosing worker, nested shell, nested pytest'
        assert all(node['cleanup_error'] for node in owner.nodes)
        parent['finished'] = True
        reply = owner.cleanup(0)
        assert reply['cleanup_error'] and len(refused) == 1, 'ancestor must veto before deletion'
        assert (refused[0].worker / 'retained-primary').read_text() == 'owned evidence'
        assert parent['lease'].worker.exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['success', 'refusal', 'lost-ack', 'rejected-ack', 'closing'])
def test_setup_cleanup_acknowledgement_controls_ancestor_deletion(tmp_path, monkeypatch, mode):
    from scripts import runner_scratch

    with _inert_owner(monkeypatch, tmp_path) as (runner, owner, parent, created):
        claim = runner_scratch.claim_scratch_root(str(parent['lease'].worker / 'setup-task'))
        original_cleanup = runner._cleanup_worker
        original_exchange = runner._owner_exchange
        deleted = []

        def receipt_failure(lease):
            raise OSError('inert setup receipt failure')

        def cleanup(temp, lease):
            assert str(lease.worker) in parent['pending_storage'], 'begin must precede deletion'
            if mode == 'refusal':
                raise OSError('inert setup storage refusal')
            original_cleanup(temp, lease)
            deleted.append(temp)

        def exchange(message):
            if message['op'] == 'setup_cleanup' and message['phase'] == 'end':
                if mode == 'lost-ack':
                    raise BrokenPipeError('inert lost setup acknowledgement')
                if mode == 'rejected-ack':
                    message = dict(message, worker=message['worker'] + '-not-pending')
            return original_exchange(message)  # Actual IPC/server acknowledgement, not a reply mock.

        monkeypatch.setattr(runner_scratch, 'worker_receipt', receipt_failure)
        monkeypatch.setattr(runner, '_cleanup_worker', cleanup)
        monkeypatch.setattr(runner, '_owner_exchange', exchange)
        parent['closing'] = mode == 'closing'
        expected = OSError if mode == 'success' else BaseExceptionGroup
        with pytest.raises(expected) as failure:
            runner._run_explicit_file_once(Path('inert.py'), [], tmp_path, 30, claim)
        assert 'inert setup receipt failure' in repr(failure.value)
        assert created == [], 'setup failure must not enter a process constructor'
        if mode == 'success':
            assert len(deleted) == 1 and list(claim.task.iterdir()) == []
            assert parent['pending_storage'] == {} and parent['cleanup_error'] is None
            parent['finished'] = True
            monkeypatch.setattr(runner, '_cleanup_worker', original_cleanup)
            assert owner.cleanup(0)['cleanup_error'] is None
            assert not parent['lease'].worker.exists()
        else:
            assert parent.get('pending_storage') or parent['cleanup_error']
            parent['finished'] = True
            before = list(deleted)
            assert owner.cleanup(0)['cleanup_error']
            assert deleted == before and parent['lease'].worker.exists()
            if mode in ('refusal', 'closing'):
                assert len(list(claim.task.iterdir())) == 1 and not deleted
            else:
                assert len(deleted) == 1 and list(claim.task.iterdir()) == []
