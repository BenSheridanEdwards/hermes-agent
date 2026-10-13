"""Selected Desktop spawn arguments -> fresh real Hermes loader, synthetic homes only.

Executes the actual argument-preparation block, not a second implementation.
Does not boot Electron, serve, a gateway, or make provider requests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('selected', [None, 'default', 'chosen'])
def test_desktop_selected_profile_beats_unrelated_cli_sticky_profile(tmp_path, selected):
    source = ROOT / 'apps/desktop/electron/main.ts'
    node = shutil.which('node')
    assert node, 'Node required for Desktop spawn contract; not skipped'
    # The real main.ts block is valid JavaScript. Only evaluate this pure block,
    # never Electron startup (which could launch a production backend).
    js = """
const fs = require('node:fs'), vm = require('node:vm');
const text = fs.readFileSync(process.argv[1], 'utf8');
const start = text.indexOf("const backendArgs = ['serve'", text.indexOf('async function startHermes()'));
const end = text.indexOf('const setup =', start);
if (start < 0 || end < 0) throw new Error('Desktop spawn seam moved; review test');
const scope = {readActiveDesktopProfile: () => JSON.parse(process.argv[2])};
vm.runInNewContext(text.slice(start,end) + '; result = backendArgs', scope);
console.log(JSON.stringify(scope.result));
"""
    prepared = subprocess.run([node, '-e', js, str(source), json.dumps(selected)], capture_output=True, text=True, timeout=15, check=True)
    args = json.loads(prepared.stdout)
    assert args[:2] == ['--profile', selected or 'default']
    home = tmp_path / 'home'
    root = home / '.hermes'
    for profile in ['other', 'chosen']:
        (root / 'profiles' / profile).mkdir(parents=True)
        (root / 'profiles' / profile / 'config.yaml').write_text('model: synthetic\n')
    (root / 'active_profile').write_text('other')
    (root / 'config.yaml').write_text('model: synthetic\n')
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'HERMES_HOME': str(root),
           'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DESKTOP': '1',
           'HERMES_TEST_ISOLATION': str(root)}
    code = "import sys,os; sys.argv=['hermes']+sys.argv[1:]; import hermes_cli.main; print('PROFILE_HOME='+os.environ['HERMES_HOME'])"
    loaded = subprocess.run([sys.executable, '-c', code, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert loaded.returncode == 0, 'fresh synthetic Hermes loader failed'
    expected = root / 'profiles' / 'chosen' if selected == 'chosen' else root
    assert 'PROFILE_HOME=' + str(expected) in loaded.stdout
