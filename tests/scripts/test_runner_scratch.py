"""Real canonical wrapper/worker tests; REAL HOME is never replaced.

Source only. All nested scratch is beneath pytest's already-owned task root.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = 'tests/scripts/fixtures/runner_scratch_probe.py'


def _shell(root_args, *test_args, runner_options=()):
    env = {
        'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'],
        'HERMES_PYTHON': sys.executable, 'OPENAI_API_KEY': 'inert-canary',
        'PYTHONPATH': 'inert-path-canary',
        # Do not inherit an unrelated system TMPDIR into shell startup.
        'TMPDIR': os.environ['TMPDIR'], 'TMP': os.environ['TMPDIR'],
        'TEMP': os.environ['TMPDIR'],
    }
    return subprocess.run(
        ['bash', str(ROOT / 'scripts/run_tests.sh'), *root_args,
         '-j', '1', '--file-retries', '0', '--file-timeout', '60', *runner_options, *test_args],
        cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=120,
    )


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('joined', [False, True])
def test_explicit_root_contains_real_child_without_replacing_home(tmp_path, joined, record_property):
    from hermes_state_guard import _real_platform_state_root

    scratch = tmp_path / 'new-task with spaces'
    arguments = ([f'--scratch-root={scratch}'] if joined
                 else ['--scratch-root', str(scratch)])
    result = _shell(arguments, PROBE, '-s')
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((scratch / 'probe-receipt.json').read_text(encoding='utf-8'))
    for key in ('tempfile', 'pytest_tmp', 'pytest_cache', 'home',
                'collection_root', 'collection_log', 'external_temp'):
        assert Path(receipt[key]).is_relative_to(scratch), receipt
    assert receipt['os_home'] == os.environ['HOME']
    collection = receipt['collection_liveness']
    assert collection['guarded'] is False
    assert collection['log_written'] and collection['mkdir_succeeded']
    assert collection['deleted_profile_refused'] and collection['missing_profile_refused']
    assert collection['contained_alias_refused'] and collection['owned_listener_stopped']
    assert collection['external_tombstones'] == []
    assert collection['tombstone_paths'] and collection['profile_root_paths']
    for value in collection['tombstone_paths'] + collection['profile_root_paths']:
        assert Path(value).is_relative_to(scratch)
    assert receipt['native_root'] == str(_real_platform_state_root())
    assert receipt['bytecode_disabled']
    assert receipt['synthetic_secret_absent']
    assert receipt['pythonpath_canary_absent']
    assert receipt['guard_negatives'] == ['native-state', 'native-config', 'native-auth',
                                         'profile-state', 'profile-config', 'profile-auth',
                                         'scratch-sibling']
    assert receipt['tz'] == 'UTC'
    assert receipt['isolation_marker']
    assert receipt['python'] == sys.executable
    assert not list(scratch.glob('r-*')), 'per-file cleanup must finish'
    assert (scratch / 'test_durations.json').is_file()
    record_property('child_receipt', json.dumps(receipt, sort_keys=True))
    record_property('workers_cleaned', True)
    # Reusing another run's directory is not ownership.
    again = _shell(arguments, PROBE)
    assert again.returncode == 2, again.stdout + again.stderr


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', [
    'missing-parent', 'relative', 'existing', 'file', 'symlink',
    'parent-symlink', 'parent-traversal', 'home', 'hermes-home', 'readonly', 'empty', 'newline',
])
def test_invalid_scratch_refused_before_any_worker_writes(tmp_path, kind):
    scratch = tmp_path / 'scratch'
    parent = tmp_path / 'readonly'
    value = str(scratch)
    if kind == 'missing-parent':
        value = str(scratch / 'missing')
    elif kind == 'relative':
        value = 'relative-scratch'
    elif kind == 'existing':
        scratch.mkdir()
    elif kind == 'file':
        scratch.write_text('retain', encoding='utf-8')
    elif kind in ('symlink', 'parent-symlink'):
        target = tmp_path / 'target'
        target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
        if kind == 'parent-symlink':
            value = str(scratch / 'new-child')
    elif kind == 'parent-traversal':
        value = str(tmp_path / 'unused' / '..' / 'scratch')
    elif kind == 'home':
        value = os.environ['HOME']
    elif kind == 'hermes-home':
        value = str(Path(os.environ['HOME']) / '.hermes')
    elif kind == 'readonly':
        parent.mkdir(mode=0o500)
        value = str(parent / 'new-task')
    elif kind == 'empty':
        value = ''
    elif kind == 'newline':
        value += '\n'
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    try:
        result = _shell([f'--scratch-root={value}'], PROBE)
        assert result.returncode == 2, result.stdout + result.stderr
        assert '--scratch-root' in result.stdout + result.stderr
        assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    finally:
        if kind == 'readonly':
            parent.chmod(0o700)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('override', [
    '--basetemp=/outside', '-o', '--override-ini=cache_dir=/outside',
    '--runner-owned-scratch={}',
])
def test_explicit_root_rejects_competing_pytest_locations(tmp_path, override):
    scratch = tmp_path / 'new-task'
    tail = [override, 'cache_dir=/outside'] if override == '-o' else [override]
    result = _shell(['--scratch-root', str(scratch)], PROBE, '--', *tail)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not scratch.exists()


@pytest.mark.platforms('posix')
def test_hidden_long_root_is_not_silently_relocated(tmp_path):
    parent = tmp_path / '.hidden'
    parent.mkdir()
    scratch = parent / ('long-' * 24)
    result = _shell(['--scratch-root', str(scratch)], PROBE)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((scratch / 'probe-receipt.json').read_text(encoding='utf-8'))
    assert Path(receipt['tempfile']).is_relative_to(scratch)


@pytest.mark.platforms('posix')
def test_duplicate_scratch_option_is_usage_error(tmp_path):
    scratch = tmp_path / 'new-task'
    result = _shell(['--scratch-root', str(scratch), '--scratch-root', str(scratch)])
    assert result.returncode == 2, result.stdout + result.stderr
    assert not scratch.exists()


@pytest.mark.platforms('posix')
@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0,
                    reason='readonly cleanup regression requires an unprivileged parent')
def test_actual_parent_cleanup_preserves_outward_link_targets(tmp_path, record_property):
    import stat

    task = tmp_path / 'cleanup-parent-task'
    result = _shell(['--scratch-root', str(task)],
                    'tests/scripts/fixtures/runner_cleanup_probe.py')
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((task / 'cleanup-receipt.json').read_text())
    assert receipt['child_pid'] != receipt['parent_pid']
    assert not Path(receipt['worker']).exists()
    assert not list(task.glob('r-*'))
    canary = Path(receipt['canary'])
    assert canary.read_bytes() == b'outside worker: retain bytes and mode'
    assert stat.S_IMODE(canary.stat().st_mode) == receipt['mode']
    assert canary.stat().st_nlink == 1
    assert list(canary.parent.iterdir()) == [canary]
    record_property('cleanup_receipt', json.dumps({
        **receipt, 'worker_removed': not Path(receipt['worker']).exists(),
        'canary_bytes': canary.read_bytes().hex(),
        'canary_mode_after': stat.S_IMODE(canary.stat().st_mode),
        'canary_links_after': canary.stat().st_nlink,
    }, sort_keys=True))


@pytest.mark.platforms('posix')
def test_parallel_retry_receipts_and_task_footprint(tmp_path):
    scratch = tmp_path / 'parallel-task'
    canary = tmp_path / 'sibling-canary'
    canary.write_bytes(b'unchanged')
    probes = []
    for name in ('a', 'b'):
        probe = tmp_path / f'test_retry_{name}.py'
        probe.write_text(
            'import json, os\nfrom tests.scratch_isolation import LEASE\n'
            'def test_retry_identity():\n'
            f'    first = LEASE.task / "attempt-{name}.json"\n'
            f'    second = LEASE.task / "retry-{name}.json"\n'
            '    data = {"worker": str(LEASE.worker), "pid": os.getpid(), "parent": os.getppid()}\n'
            '    if not first.exists():\n'
            '        first.write_text(json.dumps(data))\n'
            '        assert False, "inert first-attempt failure"\n'
            '    old = json.loads(first.read_text())\n'
            '    assert old["worker"] != data["worker"]\n'
            '    assert old["parent"] == data["parent"]\n'
            '    second.write_text(json.dumps(data))\n', encoding='utf-8')
        probes.append(str(probe))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    # Outside-repo inert modules load the SAME conftest via an ordinary plugin
    # argument, without disabling guards or dependency plugin autoload.
    result = _shell(['--scratch-root', str(scratch)], *probes, '--', '-p', 'tests.conftest',
                    runner_options=('-j', '2', '--file-retries', '1'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FLAKY' in result.stdout
    records = [json.loads((scratch / f'{phase}-{name}.json').read_text())
               for phase in ('attempt', 'retry') for name in ('a', 'b')]
    assert len({r['worker'] for r in records}) == 4
    assert not list(scratch.glob('r-*'))
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    # Flaky durations deliberately remain uncached. Only explicit receipts stay.
    assert {p.name for p in scratch.iterdir()} == {'attempt-a.json', 'attempt-b.json', 'retry-a.json', 'retry-b.json'}
