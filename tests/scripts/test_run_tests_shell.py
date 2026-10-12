"""The canonical shell must execute tests and propagate their actual failure."""
import json
import os
import pytest

from tests.scripts.fixtures.compatibility_runner import ROOT, run


@pytest.mark.platforms('posix')
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path, real_bash, monkeypatch):
    from tests.scripts.fixtures import compatibility_runner
    monkeypatch.setattr(compatibility_runner, '_BASH', real_bash)
    # The collector materializes the actual failing module only AFTER the
    # canonical nested task/worker receipt exists. No pre-created task/adoption.
    seed = ROOT / 'tests/scripts/fixtures/shell_failure.canary'
    result = run(tmp_path, seed, '--', '-p', 'tests.scripts.fixtures.shell_canary',
                 timeout=180, PATHEXT='.COM;.EXE;.BAT;.CMD')
    output = result.stdout + result.stderr
    assert (result.task / 'executed').read_text(encoding='utf-8') == 'executed', output
    receipt = json.loads((result.task / 'canary.json').read_text(encoding='utf-8'))
    assert receipt['task'] == str(result.task)
    assert receipt['source'] == str(result.task / 'canary-source' / 'test_runner_canary.py')
    assert receipt['pathext'] == '.COM;.EXE;.BAT;.CMD'
    assert receipt['worker_parent'] == str(result.task)
    assert receipt['home'] == os.environ['HOME']
    assert {'_hermetic_environment', '_forbid_real_hermes_home_io', '_kanban_write_guard'} <= set(receipt['fixtures'])
    assert result.returncode == 1, output
    assert 'runner failure propagation canary' in output
    assert '1 failed' in output
    assert 'INTERNALERROR' not in output
    assert not result.ownership['cleanup_error'], output
    assert not result.ownership['timed_out'], output
