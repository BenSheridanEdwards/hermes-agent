"""Explicit canonical child fixture. No live-state operations are dispatched."""
import json
import os
from pathlib import Path
import sys
import tempfile
import logging
import subprocess

import pytest

from hermes_state_guard import _real_platform_state_root, _is_production_state_db
from tests.home_io_guard import HomeIOGuard
from tests.scratch_isolation import LEASE

_COLLECTION_ENV = dict(os.environ)
_COLLECTION_NATIVE = _real_platform_state_root()
if LEASE is not None:
    # Do real collection-time resolution and file logging, before autouse
    # guards exist. These assert isolation, not an import-time sandbox.
    from hermes_constants import get_default_hermes_root
    from hermes_logging import setup_logging, flush_log_queue
    from tests.scripts.fixtures.scratch_liveness_probe import exercise_liveness

    _COLLECTION_LIVENESS = exercise_liveness(
        LEASE.worker / 'collection-liveness-model', guarded=False)
    _COLLECTION_ROOT = get_default_hermes_root()
    assert LEASE.contains(_COLLECTION_ROOT)
    setup_logging()
    logging.getLogger('scratch.collection').warning('inert collection receipt')
    flush_log_queue()
    _COLLECTION_LOG = Path(os.environ['HERMES_HOME']) / 'logs' / 'agent.log'
    assert LEASE.contains(_COLLECTION_LOG)
    assert 'inert collection receipt' in _COLLECTION_LOG.read_text(encoding='utf-8')


def test_scratch_receipt(tmp_path, pytestconfig):
    from hermes_logging import flush_log_queue

    assert LEASE is not None
    logging.getLogger('scratch.session').warning('inert session after collection')
    flush_log_queue()
    assert 'inert session after collection' in _COLLECTION_LOG.read_text(encoding='utf-8')
    assert os.environ['HOME'] == LEASE.home
    raw = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--runner-owned-scratch='))
    assert json.loads(raw)['parent'] == os.getppid()
    native = _real_platform_state_root()
    assert native is not None and native == _COLLECTION_NATIVE
    pytestconfig.cache.set('scratch/probe', 'written')
    with tempfile.TemporaryFile() as stream:
        stream.write(b'inert')
    # Actual fixture I/O: production home resolution must now choose test data,
    # without moving the distinct real root used by the production state guard.
    from hermes_constants import get_default_hermes_root

    isolated = get_default_hermes_root()
    assert isolated.is_relative_to(LEASE.task)
    isolated.mkdir(parents=True, exist_ok=True)
    (isolated / 'config.yaml').write_text('inert: true\n', encoding='utf-8')
    assert (isolated / 'config.yaml').read_text(encoding='utf-8') == 'inert: true\n'
    child = subprocess.run([sys.executable, '-B', '-c',
        "import json,tempfile,pathlib; "
        "p=pathlib.Path(tempfile.mkdtemp(prefix='external-child-')); "
        "(p/'cache').write_text('inert'); print(json.dumps(str(p)))"],
        check=True, capture_output=True, text=True, timeout=20)
    external_temp = Path(json.loads(child.stdout))
    assert LEASE.contains(external_temp)
    assert (external_temp / 'cache').read_text() == 'inert'
    task = LEASE.task
    guard = HomeIOGuard(lambda: [native, task.parent], scratch=LEASE)
    profile = native / 'profiles' / 'inert-profile'
    targets = {
        'native-state': native / 'state.db', 'native-config': native / 'config.yaml',
        'native-auth': native / 'auth.json', 'profile-state': profile / 'state.db',
        'profile-config': profile / 'config.yaml', 'profile-auth': profile / 'auth.json',
        'scratch-sibling': LEASE.task.with_name(LEASE.task.name + '-sibling') / 'config.yaml',
    }
    for target in targets.values():
        # Guard entry point only. A broken predicate fails an assertion, never
        # falls through to an open of a real protected path.
        with pytest.raises(AssertionError, match='REAL hermes home'):
            guard.check(target, destructive=True)
    assert _is_production_state_db(native / 'state.db', native)
    assert _is_production_state_db(profile / 'state.db', native)
    receipt = {
        'tempfile': tempfile.gettempdir(), 'pytest_tmp': str(tmp_path),
        'pytest_cache': str(pytestconfig.cache._cachedir), 'home': os.environ['HERMES_HOME'],
        'os_home': os.environ['HOME'], 'native_root': str(native),
        'bytecode_disabled': sys.dont_write_bytecode,
        'synthetic_secret_absent': 'OPENAI_API_KEY' not in _COLLECTION_ENV,
        'pythonpath_canary_absent': 'inert-path-canary' not in _COLLECTION_ENV.get('PYTHONPATH', ''),
        'tz': os.environ['TZ'], 'isolation_marker': bool(os.environ.get('HERMES_TEST_ISOLATION')),
        'guard_negatives': list(targets), 'python': sys.executable,
        'parent': os.getppid(), 'collection_root': str(_COLLECTION_ROOT),
        'collection_log': str(_COLLECTION_LOG), 'external_temp': str(external_temp),
        'collection_liveness': _COLLECTION_LIVENESS,
    }
    # Successful child stdout is suppressed; retain evidence outside worker cleanup.
    with (LEASE.task / 'probe-receipt.json').open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, sort_keys=True)
