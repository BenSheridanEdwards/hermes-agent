"""Canonical nested compatibility launches. No test executor or policy replacement."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]


def command(parent, *arguments):
    """Name an absent task; only the unchanged runner may claim it exclusively."""
    task = Path(parent) / ('compat-' + uuid.uuid4().hex)
    return ['bash', str(ROOT / 'scripts/run_tests.sh'), '--scratch-root', str(task),
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


def run(parent, *arguments, timeout=60, input=None, **extra_env):
    argv = command(parent, *arguments)
    measured = subprocess.run(argv, cwd=ROOT, env=environment(**extra_env),
                              input=input, capture_output=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    result = ProbeResult(measured.args, measured.returncode, measured.stdout, measured.stderr)
    result.task = Path(argv[3])
    # Do not mask the command's original failure with a cleanup attempt. The
    # runner owns cleanup; retain its output and any residue for the outer test.
    return result
