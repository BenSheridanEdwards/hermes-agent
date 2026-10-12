"""One explicit collection seed for the shell failure-propagation regression.

Not an executor: the canonical runner launches pytest with its ordinary plugins
and receipt; pytest collects/runs the returned Module with normal fixtures.
"""
from pathlib import Path

import pytest

from tests.scratch_isolation import LEASE

SEED = Path(__file__).with_name('shell_failure.canary')
SOURCE = '''import json
import os
from pathlib import Path
import pytest
from tests import conftest
from tests.scratch_isolation import LEASE


def test_canary(request):
    LEASE.verify()
    required = {'_hermetic_environment', '_forbid_real_hermes_home_io', '_kanban_write_guard'}
    assert required <= set(request.fixturenames)
    assert request.config.pluginmanager.get_plugin(conftest.__file__) is conftest
    # Exercise the installed normal fixture guard, not a replacement guard or
    # a live negative target. Both spellings stay inside this owned task.
    with pytest.raises(AssertionError, match='REAL hermes home'):
        os.stat(LEASE.task / 'canary-source' / '..' / 'guard-probe')
    source = Path(__file__)
    assert source == LEASE.task / 'canary-source' / 'test_runner_canary.py'
    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'
    (LEASE.task / 'executed').write_text('executed', encoding='utf-8')
    (LEASE.task / 'canary.json').write_text(json.dumps({
        'task': str(LEASE.task), 'worker_parent': str(LEASE.worker.parent),
        'source': str(source), 'home': os.environ['HOME'],
        'pathext': os.environ['PATHEXT'], 'fixtures': sorted(request.fixturenames),
        'conftest': conftest.__file__, 'installed_guard_refused': True,
    }), encoding='utf-8')
    assert False, 'runner failure propagation canary'
'''


class OwnedCanaryModule(pytest.Module):
    def _getobj(self):
        # Keep the seed's collector path/nodeid so explicit path matching,
        # conftest hooks and fixture ancestry remain normal. Only the imported
        # Python source moves. Delegate loading to a normal pytest Module,
        # rather than importing a private pytest helper or executing it here.
        if LEASE is None:
            raise RuntimeError('shell canary requires a canonical owned worker receipt')
        LEASE.verify()
        directory = LEASE.task / 'canary-source'
        directory.mkdir(mode=0o700)  # exclusive, never adopt an earlier source
        source = directory / 'test_runner_canary.py'
        with source.open('x', encoding='utf-8') as stream:
            stream.write(SOURCE)
        LEASE.verify()
        return pytest.Module.from_parent(self, path=source).obj


def pytest_collect_file(file_path, parent):
    if file_path == SEED:
        return OwnedCanaryModule.from_parent(parent, path=file_path)
    return None
