"""Review regressions. All mutation targets are disposable; REAL HOME stays intact.

Authored before correction. Canonical execution only after independent clearance.
"""
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db, kanban_db_connect as kbc
from scripts import runner_scratch as scratch
from tests.home_io_guard import HomeIOGuard


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('group_exists', [True, False])
@pytest.mark.parametrize('lookup_gone', [True, False])
def test_exited_owned_session_keeps_unreaped_identity_when_lookup_is_gone(tmp_path, monkeypatch, group_exists, lookup_gone):
    """A Darwin-style dead leader is not proof that its pinned PID was lost.

    Model kernel calls only, never signal a live negative target. Exercise the
    actual closer and lease deletion gate; real-session coverage stays in the
    composed cancellation suite.
    """
    from types import SimpleNamespace
    from scripts import run_tests_parallel as runner
    from scripts.runner_scratch import claim_scratch_root
    import psutil

    lease = claim_scratch_root(str(tmp_path / 'dead-session')).create_worker()
    pid = 12345
    proc = SimpleNamespace(pid=pid, poll=lambda: 0)
    node = dict(proc=proc, parent=None, lease=lease, finished=True,
                closing=False, closed=False, cleanup_error=None,
                session_established=True)
    owner = runner._ProcessOwner()
    owner.nodes.append(node)
    events = []

    def waitid(kind, target, flags):
        assert kind == os.P_PID and target == pid
        assert flags == os.WEXITED | os.WNOHANG | os.WNOWAIT
        events.append('pin')
        return SimpleNamespace(si_pid=pid, si_code=os.CLD_EXITED, si_status=0)

    def missing(target):
        assert target == pid
        if not lookup_gone:
            return pid
        raise ProcessLookupError('model: exited leader has no queryable session')

    def killpg(target, sig):
        assert target == pid and 'pin' in events
        events.append('kill')
        if not group_exists:
            raise ProcessLookupError('model: empty group')

    def waitpid(target, flags):
        assert target == pid and flags == 0 and events[-1] != 'reap'
        events.append('reap')
        return pid, 0

    monkeypatch.setattr(runner.os, 'waitid', waitid)
    monkeypatch.setattr(runner.os, 'getsid', missing)
    monkeypatch.setattr(runner.os, 'getpgid', missing)
    monkeypatch.setattr(runner.os, 'killpg', killpg)
    monkeypatch.setattr(runner.os, 'waitpid', waitpid)
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter(()))
    assert owner._close(node) is None
    assert events[0] == 'pin' and 'kill' in events and events[-1] == 'reap'
    assert owner.cleanup(0)['cleanup_error'] is None
    assert not lease.worker.exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('uncertainty', ['reaped', 'unconfirmed-session', 'wrong-child', 'changed-session', 'live-missing-group'])
def test_unproven_session_still_vetoes_signalling_and_storage(tmp_path, monkeypatch, uncertainty):
    from types import SimpleNamespace
    from scripts import run_tests_parallel as runner
    from scripts.runner_scratch import claim_scratch_root
    import psutil

    lease = claim_scratch_root(str(tmp_path / 'uncertain-session')).create_worker()
    pid = 12345
    proc = SimpleNamespace(pid=pid, poll=lambda: 0)
    node = dict(proc=proc, parent=None, lease=lease, finished=True,
                closing=False, closed=False, cleanup_error=None,
                session_established=uncertainty != 'unconfirmed-session')
    owner = runner._ProcessOwner()
    owner.nodes.append(node)
    signalled = []

    def waitid(*args):
        if uncertainty == 'reaped':
            raise ChildProcessError('model: not our unreaped child')
        if uncertainty == 'live-missing-group':
            return None
        return SimpleNamespace(si_pid=pid + (uncertainty == 'wrong-child'),
                               si_code=os.CLD_EXITED, si_status=0)

    def identity(target):
        if uncertainty == 'changed-session':
            return pid + 1
        if uncertainty == 'live-missing-group':
            return pid
        raise ProcessLookupError('model: no session lookup')

    def killpg(target, sig):
        signalled.append(target)
        raise ProcessLookupError('model: group missing while child still live')

    monkeypatch.setattr(runner.os, 'waitid', waitid)
    monkeypatch.setattr(runner.os, 'getsid', identity)
    monkeypatch.setattr(runner.os, 'getpgid', identity)
    monkeypatch.setattr(runner.os, 'killpg', killpg)
    monkeypatch.setattr(runner.os, 'waitpid', lambda target, flags: (target, 0))
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter(()))
    assert owner._close(node)
    assert signalled == ([pid] if uncertainty == 'live-missing-group' else [])
    assert owner.cleanup(0)['cleanup_error']
    assert lease.worker.exists()


def _owned(parent):
    claim = scratch.claim_scratch_root(str(parent / 'task'))
    lease = claim.create_worker()
    return claim, lease


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('alias', ['parent', 'hardlink'])
@pytest.mark.parametrize('operation', ['read', 'write', 'fd-read', 'fd-write', 'fd-replace'])
def test_compound_aliases_refused_by_installed_wrappers(tmp_path, monkeypatch, alias, operation):
    claim, lease = _owned(tmp_path)
    protected = tmp_path / 'protected'
    (protected / 'subdir').mkdir(parents=True)
    canary = protected / 'config.yaml'
    canary.write_text('unchanged', encoding='utf-8')
    good = lease.task / 'good'
    good.write_text('good', encoding='utf-8')
    # The witness is opened before link creation and before installing the inner
    # guard. No disabling the outer task guard to inspect a multiply linked inode.
    with canary.open(encoding='utf-8') as witness:
        if alias == 'parent':
            (lease.task / 'link').symlink_to(protected / 'subdir', target_is_directory=True)
            relative = 'link/../config.yaml'
        else:
            os.link(canary, lease.task / 'hard')
            (lease.task / 'alias').symlink_to(lease.task / 'hard')
            relative = 'alias'
        guard = HomeIOGuard(lambda: [tmp_path], scratch=lease)
        with monkeypatch.context() as patcher:
            guard.install(patcher)
            fd = os.open(lease.task, os.O_RDONLY)
            try:
                with pytest.raises(AssertionError, match='REAL hermes home'):
                    if operation == 'read':
                        (lease.task / relative).read_text(encoding='utf-8')
                    elif operation == 'write':
                        (lease.task / relative).write_text('bad', encoding='utf-8')
                    elif operation == 'fd-replace':
                        os.replace('good', relative, src_dir_fd=fd, dst_dir_fd=fd)
                    else:
                        flags = os.O_RDONLY if operation == 'fd-read' else os.O_WRONLY | os.O_TRUNC
                        opened = os.open(relative, flags, dir_fd=fd)
                        os.close(opened)
            finally:
                os.close(fd)
        assert witness.read() == 'unchanged'
        assert good.read_text(encoding='utf-8') == 'good'


@pytest.mark.platforms('posix')
def test_nested_deny_model_wins_over_outer_task_allowance(tmp_path, monkeypatch):
    _, lease = _owned(tmp_path)
    model = lease.task / 'protected-model'
    model.mkdir()
    target = model / 'auth.json'
    target.write_text('unchanged', encoding='utf-8')
    with monkeypatch.context() as patcher:
        HomeIOGuard(lambda: [model], scratch=lease).install(patcher)
        with pytest.raises(AssertionError, match='REAL hermes home'):
            target.write_text('bad', encoding='utf-8')
        with pytest.raises(AssertionError, match='REAL hermes home'):
            target.read_text(encoding='utf-8')
    assert target.read_text(encoding='utf-8') == 'unchanged'


@pytest.mark.platforms('posix')
def test_real_kanban_connect_and_inert_protected_paths(tmp_path, monkeypatch):
    from tests import conftest
    _, lease = _owned(tmp_path)
    monkeypatch.setattr(conftest, '_REAL_KANBAN_ROOT', tmp_path)
    monkeypatch.setattr(conftest, '_OWNED_TEST_SCRATCH', lease)
    home = lease.task / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    connection = kbc.connect()
    try:
        assert connection.execute('select 1').fetchone()[0] == 1
    finally:
        connection.close()
    assert kanban_db.kanban_db_path().is_relative_to(lease.task)
    for path in (tmp_path / 'kanban.db', tmp_path / 'profiles/sibling/kanban.db',
                 tmp_path / 'sibling-task/kanban.db'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('inert', encoding='utf-8')
        with pytest.raises(RuntimeError, match='kanban_write_guard'):
            kbc.connect(path)
        assert path.read_text(encoding='utf-8') == 'inert'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('alias', ['escape', 'compound', 'hardlink'])
def test_relative_kanban_connect_and_alias_refusal(tmp_path, monkeypatch, alias):
    import sqlite3

    from tests import conftest
    from tests.scratch_isolation import LEASE

    dispatched = []
    original_connect = sqlite3.connect

    def observe_connect(*args, **kwargs):
        dispatched.append(args[0] if args else kwargs.get('database'))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', observe_connect)

    _, lease = _owned(tmp_path)
    monkeypatch.setattr(conftest, '_REAL_KANBAN_ROOT', tmp_path)
    monkeypatch.setattr(conftest, '_OWNED_TEST_SCRATCH', lease)
    monkeypatch.chdir(lease.task)
    monkeypatch.setenv('HERMES_HOME', str(lease.worker))
    connection = kbc.connect(Path('board.db'))
    try:
        assert connection.execute('select 1').fetchone()[0] == 1
        assert (lease.task / 'board.db').is_file()
    finally:
        connection.close()

    assert dispatched, 'positive must reach the actual SQLite connection'
    dispatched.clear()
    protected = tmp_path / 'protected'
    (protected / 'subdir').mkdir(parents=True)
    canary = protected / 'board.db'
    canary.write_text('unchanged', encoding='utf-8')
    with canary.open(encoding='utf-8') as witness:
        if alias == 'escape':
            target = Path('../protected/board.db')
        elif alias == 'compound':
            Path('link').symlink_to(protected / 'subdir', target_is_directory=True)
            target = Path('link/../board.db')
        else:
            os.link(canary, Path('hard'))
            Path('alias').symlink_to(lease.task / 'hard')
            target = Path('alias')
        # In explicit mode the outer I/O guard refuses the multiply linked
        # inode during resolve(), before the Kanban guard can return its own
        # refusal. Require that exact earlier failure, not arbitrary errors.
        early_io_refusal = alias == 'hardlink' and LEASE is not None
        error = AssertionError if early_io_refusal else RuntimeError
        message = 'TEST BUG: file I/O against the REAL hermes home:' if early_io_refusal else 'kanban_write_guard'
        with pytest.raises(error, match=message):
            kbc.connect(target)
        assert dispatched == [], 'denied alias must never reach SQLite'
        assert witness.read() == 'unchanged'
    assert not (protected / 'board.db-journal').exists()
    assert not (protected / 'board.db-wal').exists()
    assert not (protected / 'board.db-shm').exists()


@pytest.mark.platforms('posix')
def test_profile_liveness_with_guarded_real_logging(tmp_path):
    from tests.scripts.fixtures.scratch_liveness_probe import exercise_liveness

    receipt = exercise_liveness(tmp_path / 'liveness-model', guarded=True)
    assert receipt['log_written'] and receipt['mkdir_succeeded']
    assert receipt['external_tombstones'] == []
    assert receipt['deleted_profile_refused']
    assert receipt['missing_profile_refused']


@pytest.mark.platforms('posix')
def test_liveness_resolver_default_and_nonowned_delegation(tmp_path):
    from tests.scratch_isolation import bounded_profile_resolver

    calls = []
    sentinel = object()

    def original(path):
        calls.append(path)
        return sentinel

    assert bounded_profile_resolver(None, original) is original
    _, lease = _owned(tmp_path)
    resolver = bounded_profile_resolver(lease, original)
    outside = tmp_path / 'profiles' / 'sibling' / 'logs'
    assert resolver(outside) is sentinel
    assert calls == [outside]


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', ['parent', 'symlink', 'metadata-alias', 'replaced-task'])
def test_liveness_resolver_rejects_invalid_owned_inputs(tmp_path, kind):
    from tests.scratch_isolation import bounded_profile_resolver

    _, lease = _owned(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    canary = outside / 'worker'
    canary.write_text('unchanged', encoding='utf-8')

    def unexpected_fallback(path):
        raise AssertionError(f'owned invalid path delegated: {path}')

    resolver = bounded_profile_resolver(lease, unexpected_fallback)
    if kind == 'parent':
        target = lease.task / '..' / 'outside' / 'logs'
    elif kind == 'symlink':
        (lease.task / 'link').symlink_to(outside, target_is_directory=True)
        target = lease.task / 'link' / 'logs'
    elif kind == 'metadata-alias':
        profile = lease.worker / '.hermes' / 'profiles' / 'worker'
        profile.mkdir(parents=True)
        (profile.parent / '.deleted').symlink_to(outside, target_is_directory=True)
        target = profile / 'logs'
    else:
        lease.task.rename(tmp_path / 'retained-original')
        lease.task.mkdir(mode=0o700)
        target = lease.task / 'logs'
    with pytest.raises(ValueError, match='owned scratch|identity changed'):
        resolver(target)
    assert canary.read_text(encoding='utf-8') == 'unchanged'


@pytest.mark.platforms('posix')
def test_original_claim_not_replaced_before_worker_or_output(tmp_path):
    from scripts import run_tests_parallel as runner
    claim = scratch.claim_scratch_root(str(tmp_path / 'task'))
    claim.task.rename(tmp_path / 'original')
    claim.task.mkdir(mode=0o700)
    canary = claim.task / 'keep'
    canary.write_text('replacement', encoding='utf-8')
    with pytest.raises(ValueError, match='identity'):
        claim.create_worker()
    with pytest.raises(ValueError, match='identity'):
        runner._save_durations([], tmp_path, claim)
    assert list(claim.task.iterdir()) == [canary]


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('replace', ['task', 'worker'])
def test_original_worker_not_reissued_or_cleaned_after_replacement(tmp_path, replace):
    from scripts import run_tests_parallel as runner
    claim, lease = _owned(tmp_path)
    target = lease.task if replace == 'task' else lease.worker
    target.rename(tmp_path / 'original')
    target.mkdir(mode=0o700)
    canary = target / 'keep'
    canary.write_text('replacement', encoding='utf-8')
    with pytest.raises(ValueError, match='identity'):
        scratch.worker_receipt(lease)
    with pytest.raises(ValueError, match='identity'):
        runner._cleanup_worker(str(lease.worker), lease)
    assert canary.read_text(encoding='utf-8') == 'replacement'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('failure', ['receipt', 'spawn'])
def test_pre_spawn_failures_clean_only_owned_worker(tmp_path, monkeypatch, failure):
    from scripts import run_tests_parallel as runner
    claim = scratch.claim_scratch_root(str(tmp_path / 'task'))
    def fail(*args, **kwargs):
        raise OSError('inert injected ' + failure)
    if failure == 'receipt':
        monkeypatch.setattr(scratch, 'worker_receipt', fail)
        with pytest.raises(OSError, match='inert injected'):
            runner._run_one_file_once(Path('inert.py'), [], tmp_path, 2, claim)
    else:
        # Enter the real Popen constructor, fail before its OS launch seam,
        # and use the real owner and deletion acknowledgement. No reply model.
        owner = runner._ProcessOwner()
        # This constructor-only owner is local, not the enclosing IPC owner.
        # No launch can occur: _get_handles below fails before _execute_child.
        monkeypatch.setattr(runner, '_reserve_storage', lambda: None)
        def no_handles(*args, **kwargs):
            raise OSError('inert injected spawn')
        monkeypatch.setattr(runner._HeldPopen, '_get_handles', no_handles)
        monkeypatch.setattr(runner, '_owned_run', lambda cmd, **kw: owner.run(cmd=cmd, **kw))
        def cleanup(result):
            reply = owner.cleanup(result['node'])
            assert reply['cleanup_error'] is None
        monkeypatch.setattr(runner, '_owned_cleanup', cleanup)
        root = Path(runner.__file__).resolve().parent.parent
        result = runner._run_one_file_once(Path('inert.py'), [], root, 2, claim)
        assert result[1] == 125 and 'inert injected spawn' in result[2]
        assert len(owner.nodes) == 1
        assert not owner.nodes[0]['proc']._launch_entered
        assert owner.nodes[0]['closed'] and owner.nodes[0]['cleanup_error'] is None
    assert list(claim.task.iterdir()) == []


@pytest.mark.platforms('posix')
def test_readonly_worker_cleanup_preserves_task_and_siblings(tmp_path):
    from scripts import run_tests_parallel as runner
    claim, lease = _owned(tmp_path)
    stale = claim.task / 'r-retained-evidence'
    stale.mkdir(mode=0o700)
    (stale / 'keep').write_text('keep', encoding='utf-8')
    nested = lease.worker / 'readonly'
    nested.mkdir()
    (nested / 'data').write_text('inert', encoding='utf-8')
    nested.chmod(0o500)
    runner._cleanup_worker(str(lease.worker), lease)
    assert not lease.worker.exists()
    assert (stale / 'keep').read_text(encoding='utf-8') == 'keep'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', ['file', 'symlink', 'hardlink'])
def test_duration_collisions_preserve_existing_canary(tmp_path, kind):
    from scripts import run_tests_parallel as runner
    claim = scratch.claim_scratch_root(str(tmp_path / 'task'))
    canary = tmp_path / 'canary'
    canary.write_text('unchanged', encoding='utf-8')
    output = claim.task / runner._DURATIONS_FILE
    with canary.open(encoding='utf-8') as witness:
        if kind == 'file':
            output.write_text('unchanged', encoding='utf-8')
        elif kind == 'symlink':
            output.symlink_to(canary)
        else:
            os.link(canary, output)
        # Outer canonical guard may reject the hardlink before exclusive open.
        with pytest.raises((FileExistsError, AssertionError)):
            runner._save_durations([], tmp_path, claim)
        assert witness.read() == 'unchanged'


@pytest.mark.platforms('posix')
def test_competing_claim_loser_cannot_adopt_or_remove_winner(tmp_path):
    from threading import Barrier
    barrier = Barrier(2)
    def claim():
        barrier.wait(timeout=10)
        try:
            return scratch.claim_scratch_root(str(tmp_path / 'task'))
        except (ValueError, FileExistsError):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winners[0].verify()
    assert winners[0].task.is_dir()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('parent', 'not-pid'), ('parent', -1),
    ('home', '/inert-wrong-home'), ('native', '/inert-wrong-native'),
    ('worker', 7), ('task', None), ('worker_identity', [1, 2]),
    ('task_identity', 'bad'), ('extra', 'unexpected'),
])
def test_receiver_rejects_malformed_metadata(tmp_path, field, value):
    _, lease = _owned(tmp_path)
    data = json.loads(scratch.worker_receipt(lease))
    data[field] = value
    with pytest.raises(ValueError):
        scratch.receive_worker(json.dumps(data), expected_parent=os.getpid())


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('raw', ['{', '[]', 'null', 'true', '42', '"receipt"'])
def test_receiver_rejects_malformed_json_shape(raw):
    with pytest.raises(ValueError):
        scratch.receive_worker(raw)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('directory', ['task', 'worker'])
@pytest.mark.parametrize('defect', ['inode', 'uid', 'mode'])
def test_receiver_checks_both_original_directory_identities(tmp_path, directory, defect):
    _, lease = _owned(tmp_path)
    data = json.loads(scratch.worker_receipt(lease))
    path = getattr(lease, directory)
    if defect == 'mode':
        path.chmod(0o755)
    else:
        data[directory + '_identity'][1 if defect == 'inode' else 2] += 1
    try:
        with pytest.raises(ValueError):
            scratch.receive_worker(json.dumps(data), expected_parent=os.getpid())
    finally:
        if defect == 'mode':
            path.chmod(0o700)


@pytest.mark.platforms('posix')
def test_suffix_a_b_a_keeps_real_home_and_distinct_test_roots(monkeypatch):
    import hermes_constants as constants
    from hermes_state_guard import _real_platform_state_root
    home, native = Path.home(), _real_platform_state_root()
    roots = []
    for suffix in ('-inert-a', '-inert-b', '-inert-a'):
        monkeypatch.setenv('HERMES_DATA_DIR_SUFFIX', suffix)
        roots.append(constants._get_platform_default_hermes_home())
    assert roots[0] == roots[2] and roots[0] != roots[1]
    assert roots[0].name.endswith('-inert-a')
    assert roots[1].name.endswith('-inert-b')
    assert Path.home() == home and _real_platform_state_root() == native


@pytest.mark.platforms('posix')
def test_active_basetemp_enforcement_separate_from_synthetic_config(tmp_path):
    from tests import conftest
    _, lease = _owned(tmp_path)
    config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_given_basetemp=lease.worker / 'pytest'))
    conftest._enforce_owned_basetemp(config, lease)
    config._tmp_path_factory._given_basetemp = tmp_path / 'not-worker'
    with pytest.raises(pytest.UsageError, match='basetemp'):
        conftest._enforce_owned_basetemp(config, lease)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('defect', ['duplicate', 'separate', 'TMPDIR', 'TMP', 'TEMP', 'PYTEST_DEBUG_TEMPROOT'])
def test_receipt_ingress_rejects_argument_and_environment_disagreement(tmp_path, monkeypatch, defect):
    import sys
    from tests import scratch_isolation
    _, lease = _owned(tmp_path)
    data = json.loads(scratch.worker_receipt(lease))
    # read_lease uses the actual receiver parent, not its unit-test override.
    data['parent'] = os.getppid()
    raw = json.dumps(data)
    argv = ['pytest', '--runner-owned-scratch=' + raw]
    if defect == 'duplicate':
        argv.append(argv[-1])
    elif defect == 'separate':
        argv = ['pytest', '--runner-owned-scratch', raw]
    monkeypatch.setattr(sys, 'argv', argv)
    for key in ('TMPDIR', 'TMP', 'TEMP', 'PYTEST_DEBUG_TEMPROOT'):
        monkeypatch.setenv(key, str(lease.worker) if key != defect else str(tmp_path))
    with pytest.raises(ValueError):
        scratch_isolation.read_lease()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', ['root', 'profile', 'base', 'shared-parent', 'custom-home', 'alias-parent', 'missing-parent'])
def test_root_shape_matrix_without_live_writes(tmp_path, monkeypatch, kind):
    native = tmp_path / 'inert-native'
    base = native / 'profiles/inert/cache/scratch'
    base.mkdir(parents=True)
    # Inject only a disposable native identity into the location validator;
    # never HOME or any production guard. No chmod/chown of real directories.
    monkeypatch.setattr(scratch, 'native_root', lambda: native)
    values = {'root': '/', 'profile': str(native / 'profiles/inert'), 'base': str(base),
              'shared-parent': str(base / 'task'), 'custom-home': str(tmp_path / 'custom/task'),
              'alias-parent': str(tmp_path / 'alias/task'), 'missing-parent': str(tmp_path / 'absent/task')}
    if kind == 'shared-parent':
        base.chmod(0o777)
    elif kind == 'custom-home':
        (tmp_path / 'custom').mkdir()
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'custom'))
    elif kind == 'alias-parent':
        (tmp_path / 'alias').symlink_to(base, target_is_directory=True)
    try:
        with pytest.raises((ValueError, OSError)):
            scratch.claim_scratch_root(values[kind])
        assert not (base / 'task').exists()
    finally:
        base.chmod(0o700)


@pytest.mark.platforms('posix')
def test_default_duration_destination_and_no_receipt_remain_unchanged(tmp_path, monkeypatch):
    import sys
    from scripts import run_tests_parallel as runner
    from tests import scratch_isolation
    monkeypatch.setattr(sys, 'argv', ['pytest'])
    assert scratch_isolation.read_lease() is None
    runner._save_durations([(tmp_path / 'inert.py', 1.25)], tmp_path)
    assert json.loads((tmp_path / runner._DURATIONS_FILE).read_text()) == {'inert.py': 1.25}


@pytest.mark.platforms('posix')
def test_timeout_cleanup_and_no_retry_use_original_worker(tmp_path, monkeypatch):
    from scripts import run_tests_parallel as runner
    claim = scratch.claim_scratch_root(str(tmp_path / 'task'))
    events = []
    def dispatch(cmd, **kwargs):
        raw = next(arg.split('=', 1)[1] for arg in cmd if arg.startswith('--runner-owned-scratch='))
        lease = scratch.receive_worker(raw, expected_parent=os.getpid())
        events.append(('dispatch', lease.worker))
        return dict(returncode=124, stdout='inert timeout output', stderr='',
                    cleanup_error=None, timed_out=True, lease=lease)
    monkeypatch.setattr(runner, '_owned_run', dispatch)
    # This existing dispatch model owns no real IPC allocation transaction.
    monkeypatch.setattr(runner, '_reserve_storage', lambda: None)
    monkeypatch.setattr(runner, '_owned_cleanup', lambda result:
                        runner._cleanup_worker(str(result['lease'].worker), result['lease']))
    result = runner._run_one_file(Path('inert.py'), [], tmp_path, 2, retries=1, scratch_root=claim)
    assert result[1] == 124
    assert [event[0] for event in events] == ['dispatch']
    assert 'inert timeout output' in result[2]
    assert not events[0][1].exists()
    claim.verify()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('present', [False, True])
def test_optional_guard_discovery_against_inert_lookup_home(tmp_path, present):
    import subprocess
    home = tmp_path / 'lookup-home'
    (home / '.hermes').mkdir(parents=True)
    if present:
        (home / '.hermes/pytest_live_guard.py').write_text('# inert discovery canary\n')
    helper = Path(__file__).resolve().parents[2] / 'scripts/test_guard_plugin.sh'
    result = subprocess.run(['bash', '-c',
        '. "$1"; hermes_test_guard_plugin "$2"; printf "%s\\n%s\\n%s\\n" "$HOME" "$EXTRA_PYTHONPATH" "$EXTRA_PYTEST_PLUGINS"',
        'discovery-probe', str(helper), str(home)],
        capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.splitlines() == [os.environ['HOME'],
        str(home / '.hermes') if present else '', 'pytest_live_guard' if present else '']


@pytest.mark.platforms('posix')
def test_parent_metadata_gate_rejects_foreign_owner_without_chown(tmp_path):
    import stat
    # Inject stat DATA, not os.stat/tempfile probes or a fake host identity.
    info = tmp_path.stat()
    foreign = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=info.st_uid + 1)
    with pytest.raises(ValueError, match='parent'):
        scratch.validate_parent_metadata(foreign, uid=info.st_uid, accessible=True)
    scratch.validate_parent_metadata(info, uid=info.st_uid, accessible=True)
