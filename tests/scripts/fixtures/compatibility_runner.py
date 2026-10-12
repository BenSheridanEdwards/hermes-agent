"""Canonical nested compatibility launches. No test executor or policy replacement."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]
_BASH = 'bash'  # Test fixtures bind the repository's real_bash resolver.


def command(parent, *arguments):
    """Name an absent task; only the unchanged runner may claim it exclusively."""
    task = Path(parent) / ('compat-' + uuid.uuid4().hex)
    return [_BASH, str(ROOT / 'scripts/run_tests.sh'), '--scratch-root', str(task),
            '-j', '1', '--file-timeout', '30', '--file-retries', '0',
            '-p', 'tests.conftest', *map(str, arguments)]


def environment(**extra):
    # Native location variables only. In particular no activation, credentials,
    # plugin-disable settings, or HOME substitution. The wrapper discovers the
    # optional live guard again and applies its own canonical scrub.
    keys = ('PATH', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA',
            'APPDATA', 'SYSTEMROOT', 'ComSpec', 'PATHEXT', 'PROGRAMFILES',
            'ProgramFiles', 'PROGRAMDATA', 'ProgramData')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(HERMES_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE='1',
               TMPDIR=os.environ['TMPDIR'], TMP=os.environ['TMPDIR'],
               TEMP=os.environ['TMPDIR'])
    env.update(extra)
    return env


class ProbeResult(subprocess.CompletedProcess):
    task: Path
    ownership: dict


class ProbeTimeout(TimeoutError):
    ownership: dict


def run_command(argv, *, timeout=60, input=None, **extra_env):
    from scripts.run_tests_parallel import _owned_run

    if '_HERMES_RUNNER_OWNER_FD' not in os.environ:
        raise RuntimeError('compatibility probes require the enclosing canonical process owner')
    measured = _owned_run(argv, cwd=ROOT, env=environment(**extra_env), timeout=timeout, input=input)
    primary = ProbeTimeout(f'canonical helper deadline {timeout}s\n{measured["stdout"]}{measured["stderr"]}') if measured['timed_out'] else None
    if primary is not None:
        primary.ownership = measured
    cleanup = RuntimeError(measured['cleanup_error']) if measured['cleanup_error'] else None
    if cleanup and primary is None:
        primary = RuntimeError(f'canonical child exited {measured["returncode"]}\n'
                               f'{measured["stdout"]}{measured["stderr"]}')
    if primary and cleanup:
        raise ExceptionGroup('primary failure and cleanup failure', [primary, cleanup])
    if cleanup:
        raise cleanup
    if primary:
        raise primary
    result = ProbeResult(argv, measured['returncode'], measured['stdout'], measured['stderr'])
    result.task = Path(argv[3])
    result.ownership = measured
    return result


def run(parent, *arguments, timeout=60, input=None, **extra_env):
    return run_command(command(parent, *arguments), timeout=timeout, input=input, **extra_env)
