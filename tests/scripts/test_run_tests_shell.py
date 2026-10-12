"""The canonical shell must execute tests and propagate their actual failure."""
import pytest

from tests.scripts.fixtures.compatibility_runner import run


@pytest.mark.platforms('posix')
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / 'test_runner_canary.py'
    case.write_text(
        'import os\nfrom tests.scratch_isolation import LEASE\n'
        'def test_canary():\n'
        "    (LEASE.task / 'executed').write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert False, 'runner failure propagation canary'\n", encoding='utf-8')
    result = run(tmp_path, case, timeout=180, PATHEXT='.COM;.EXE;.BAT;.CMD')
    assert (result.task / 'executed').read_text(encoding='utf-8') == 'executed', result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'runner failure propagation canary' in result.stdout + result.stderr
