"""Runner compatibility. Real SIGSEGV remains a separately cleared workload.

No test in this file has been authorized by the containment source changes.
The crash probe may trigger operating-system reports outside task storage.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from tests.scripts.fixtures.compatibility_runner import command, run_command, run as run_probe


@pytest.fixture(autouse=True)
def _native_launcher(real_bash, monkeypatch):
    from tests.scripts.fixtures import compatibility_runner
    monkeypatch.setattr(compatibility_runner, '_BASH', real_bash)

ROOT = Path(__file__).resolve().parents[2]


def _runner():
    spec = importlib.util.spec_from_file_location('compatibility_rtp', ROOT / 'scripts/run_tests_parallel.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_probe_dir(tmp_path):
    probe = tmp_path / 'probe'
    probe.mkdir()
    (probe / 'test_flagprobe.py').write_text(
        'def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n', encoding='utf-8')
    return probe


def _run_runner(probe_dir, *extra):
    return run_probe(probe_dir.parent, '--paths', probe_dir, *extra)


def test_progress_output_tolerates_legacy_stdout_encoding(tmp_path, monkeypatch):
    # The canonical wrapper scrubs PYTHONIOENCODING. Call the actual codec
    # repair with real legacy streams, then exercise the full canonical runner.
    runner = _runner()
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='cp1252', errors='strict')
    with monkeypatch.context() as model:
        model.setattr(sys, 'stdout', stream)
        model.setattr(sys, 'stderr', stream)
        runner._make_stdio_glyph_safe()
        print('✓ → 日本')
        stream.flush()
        observed = buffer.getvalue().decode('utf-8')
    stream.close()
    assert observed == '✓ → 日本\n'
    proc = _run_runner(_make_probe_dir(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'UnicodeEncodeError' not in proc.stdout
    assert '2 tests passed' in proc.stdout


@pytest.mark.platforms('posix')
def test_grandchild_leak_is_killed_by_runner(tmp_path, record_property):
    """Real group cleanup; bounded child and no PID-only fallback signal.

    The child ignores SIGTERM in pytest's group and self-expires at 45 seconds.
    A private stop-file requests early exit on failure. Observation checks PID
    plus creation time; a recycled PID is never signalled. No guard bypass.
    """
    import psutil

    probe = tmp_path / 'test_leaker.py'
    argv = command(tmp_path, '--files', probe)
    task = Path(argv[3])
    handoff, stop = task / 'grandchild.json', task / 'stop-owned-grandchild'
    child_code = textwrap.dedent(f'''
        import json, os, signal, time
        from pathlib import Path
        import psutil
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        me = psutil.Process()
        print(json.dumps({{"pid": os.getpid(), "created": me.create_time(),
                          "pgid": os.getpgid(0), "session": os.getsid(0),
                          "born": time.monotonic()}}), flush=True)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not Path({str(stop)!r}).exists():
            time.sleep(0.05)
    ''')
    probe.write_text(textwrap.dedent(f'''
        import json, os, subprocess, sys
        from pathlib import Path
        def test_spawns_grandchild_and_walks_away():
            child = subprocess.Popen([sys.executable, '-B', '-c', {child_code!r}],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            data = json.loads(child.stdout.readline())
            child.stdout.close()
            data.update(test_pid=os.getpid(), test_pgid=os.getpgid(0))
            Path({str(handoff)!r}).write_text(json.dumps(data))
            assert data['pgid'] == data['test_pgid']
    '''), encoding='utf-8')
    receipt = None
    primary = None

    def owned_alive():
        if receipt is None:
            return False
        try:
            proc = psutil.Process(receipt['pid'])
            return proc.create_time() == receipt['created']
        except psutil.NoSuchProcess:
            return False

    try:
        result = run_command(argv, timeout=60)
        assert handoff.exists(), result.stdout + result.stderr
        receipt = json.loads(handoff.read_text())
        record_property('grandchild_identity', json.dumps(receipt, sort_keys=True))
        assert receipt['pgid'] == receipt['test_pgid'] == receipt['test_pid']
        assert result.returncode == 0, result.stdout + result.stderr
        deadline = time.monotonic() + 5
        while owned_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert time.monotonic() < receipt['born'] + 45, 'self-expiry cannot prove runner cleanup'
        assert not owned_alive(), f'owned grandchild survived runner exit: {receipt}; {result.stdout}'
    except BaseException as error:
        primary = error
        raise
    finally:
        # Timeout before handoff is bounded by self-expiry too. Retain the
        # primary failure and cleanup error separately; never kill a stale PID.
        try:
            if task.exists():
                stop.touch(exist_ok=False)
            if receipt is None and handoff.exists():
                receipt = json.loads(handoff.read_text())
            deadline = time.monotonic() + 5
            while owned_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if owned_alive():
                raise AssertionError(f'owned residue after stop request: {receipt}; self-expiry 45s')
        except BaseException as cleanup_error:
            if primary is not None:
                primary.add_note(f'cleanup also failed: {cleanup_error!r}')
            else:
                raise


@pytest.mark.parametrize('help_flag', ['-h', '--help'])
def test_help_prints_usage_without_discovering_or_running_tests(tmp_path, help_flag):
    proc = run_probe(tmp_path, '--paths', tmp_path / 'no-tests', help_flag, timeout=10)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'usage:' in proc.stdout
    assert 'Discovered' not in proc.stdout


def test_unknown_bare_flag_errors_with_usage_instead_of_sweeping(tmp_path):
    probe = _make_probe_dir(tmp_path)
    proc = _run_runner(probe, '--jbs')
    assert proc.returncode == 2, proc.stdout
    assert 'usage:' in proc.stdout and 'unrecognized arguments: --jbs' in proc.stdout
    assert 'Discovered' not in proc.stdout
    proc = _run_runner(probe, '-rA', '-x')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '2✓' in proc.stdout or '2 passed' in proc.stdout


def test_known_flag_missing_value_errors_with_usage_instead_of_sweeping(tmp_path):
    proc = _run_runner(_make_probe_dir(tmp_path), '--tb')
    assert proc.returncode == 2, proc.stdout
    assert 'usage:' in proc.stdout and '--tb: expected one argument' in proc.stdout
    assert 'Discovered' not in proc.stdout


def test_file_retry_self_heals_and_prints_both_attempts(tmp_path):
    probe = tmp_path / 'test_flaky_probe.py'
    probe.write_text(textwrap.dedent('''
        from tests.scratch_isolation import LEASE
        def test_flaky_once():
            marker = LEASE.task / 'ran-once'
            if not marker.exists():
                marker.write_text('failed once')
                assert False, 'simulated first-attempt flake'
            assert True
    '''), encoding='utf-8')
    proc = run_probe(tmp_path, '--files', probe, '--file-retries', '1', '-q')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'FLAKY file' in proc.stdout
    assert 'simulated first-attempt flake' in proc.stdout
    assert 'first-attempt output' in proc.stdout
    assert 'retry output' in proc.stdout


def test_zero_collected_across_run_fails_and_says_so(tmp_path):
    proc = _run_runner(_make_probe_dir(tmp_path), '-k', 'zzz_matches_nothing')
    assert proc.returncode == 1, proc.stdout
    assert 'NO TESTS RAN' in proc.stdout
    assert 'NOT a pass' in proc.stdout


@pytest.mark.parametrize('form,expected', [
    ('positional', ['alpha', 'beta']), ('bare-k', ['alpha']),
    ('node-id', ['alpha']), ('explicit-k', ['beta']),
    ('pathsep', ['alpha', 'beta', 'gamma']),
])
def test_runner_selection_records_actual_test_identity(tmp_path, form, expected):
    probe, other = tmp_path / 'probe', tmp_path / 'other'
    probe.mkdir()
    other.mkdir()
    for directory, filename, names in ((probe, 'test_flags.py', ['alpha', 'beta']),
                                       (other, 'test_other.py', ['gamma'])):
        (directory / filename).write_text('from tests.scratch_isolation import LEASE\n' + ''.join(
            f"def test_{name}():\n    (LEASE.task / 'witness-{name}').touch()\n" for name in names
        ), encoding='utf-8')
    target = str(probe / 'test_flags.py')
    arguments = {
        'positional': [str(probe), '-q'],
        'bare-k': ['--paths', str(probe), '-k', 'test_alpha'],
        'node-id': [target + '::test_alpha'],
        'explicit-k': [target + '::test_alpha', '-k', 'test_beta'],
        'pathsep': ['--paths', os.pathsep.join([str(probe), str(other)])],
    }[form]
    result = run_probe(tmp_path, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(path.name.removeprefix('witness-') for path in result.task.glob('witness-*')) == expected


@pytest.mark.platforms('posix')
def test_interpreter_crash_is_reported_as_a_crash_not_as_no_tests_ran(tmp_path):
    """Requires explicit disposition of OS crash-report writes outside task storage.

    Retained real SIGSEGV contract, not replaced by an exit-code mock or SIGKILL.
    """
    probe_dir = tmp_path / 'probe'
    probe_dir.mkdir()
    (probe_dir / 'test_probe_crash.py').write_text(textwrap.dedent('''
        import os, signal
        def test_before():
            assert True
        def test_crash():
            os.kill(os.getpid(), signal.SIGSEGV)
    '''), encoding='utf-8')
    proc = _run_runner(probe_dir, '--file-retries', '0')
    assert proc.returncode != 0
    assert '1 file CRASHED' in proc.stdout
    assert 'SIGSEGV' in proc.stdout
    assert 'where no tests ran' not in proc.stdout
    assert 'NO TESTS RAN' not in proc.stdout


def test_files_from_runs_exactly_the_listed_files(tmp_path):
    probe_dir = _make_probe_dir(tmp_path)
    extra = tmp_path / 'probe_extra'
    extra.mkdir()
    (extra / 'test_extra.py').write_text('def test_extra():\n    assert True\n', encoding='utf-8')
    list_file = tmp_path / 'files.txt'
    list_file.write_text(f"{probe_dir / 'test_flagprobe.py'}\n\n{extra / 'test_extra.py'}\n", encoding='utf-8')
    proc = _run_runner(probe_dir, '--files-from', list_file, '-q')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'Running 2 test files' in proc.stdout
    assert '3 passed' not in proc.stdout


def test_files_from_dash_reads_the_list_from_stdin(tmp_path):
    probe = _make_probe_dir(tmp_path)
    proc = run_probe(tmp_path, '--files-from', '-', '-q', input=f"{probe / 'test_flagprobe.py'}\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'Running 1 test files' in proc.stdout
    assert '✓2' in proc.stdout or '2 passed' in proc.stdout


def test_scratch_root_is_per_user(tmp_path, monkeypatch):
    """Real default selector, inert I/O dependencies. Host family stays native."""
    runner = _runner()
    created = []
    model = SimpleNamespace(name=os.name, path=SimpleNamespace(
        isdir=lambda _: False, join=os.path.join),
        makedirs=lambda path, **kw: created.append((path, kw)))
    uid = [1000]
    if hasattr(os, 'getuid'):
        model.getuid = lambda: uid[0]
    monkeypatch.setattr(runner, 'os', model)
    monkeypatch.setattr(runner, 'tempfile', SimpleNamespace(gettempdir=lambda: str(tmp_path)))
    mine = Path(runner._runner_scratch_root())
    uid[0] = 0
    theirs = Path(runner._runner_scratch_root())
    assert (mine != theirs) == hasattr(os, 'getuid')
    assert mine.parent == theirs.parent == tmp_path
    assert created == [(str(mine), {'exist_ok': True}), (str(theirs), {'exist_ok': True})]
    # This branch records makedirs without allocating a shared temporary root.
    if os.name != 'nt':
        model.path.isdir = lambda _: True
        disk = Path(runner._runner_scratch_root())
        assert disk.parent == Path('/var/tmp')  # no-tmp: ok - inert default-path contract
        assert disk.name == theirs.name
        assert created[-1] == (str(disk), {'exist_ok': True})


def test_off_host_note_names_platforms_specs_that_exclude_this_host(tmp_path):
    probe = _make_probe_dir(tmp_path)
    (probe / 'test_gated.py').write_text(
        "import pytest\n\n"
        "@pytest.mark.platforms('not linux')\ndef test_elsewhere():\n    assert True\n\n"
        "@pytest.mark.platforms('posix')\ndef test_posix():\n    assert True\n\n"
        "@pytest.mark.platforms('windows')\ndef test_windows():\n    assert True\n", encoding='utf-8')
    proc = _run_runner(probe)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    notes = [line for line in proc.stdout.splitlines() if 'SKIPPED on this host' in line]
    host = {'linux': 'linux', 'darwin': 'macos', 'win32': 'windows'}[sys.platform]
    off_host = {'windows'} - {host}
    if host == 'linux':
        off_host.add('not linux')
    if host == 'windows':
        off_host.add('posix')
    assert {n.split('platforms(')[1].split(')')[0].strip("'") for n in notes} == off_host, proc.stdout
    assert all('they run on the' in n for n in notes), proc.stdout


def test_slices_partition_the_suite_exactly_even_with_different_duration_caches(capsys):
    runner = _runner()
    root = Path('/repo')
    files = [root / f'tests/t_{i:03d}.py' for i in range(101)]
    caches = [{f'tests/t_{i:03d}.py': float(i) for i in range(101)}, {'tests/t_000.py': 500.0}, {}]
    slices = [runner._slice_files(list(reversed(files)) if i % 2 else files, i % 3 + 1, 3, caches[i % 3], root)
              for i in range(3)]
    assert sorted(f for s in slices for f in s) == sorted(files)
