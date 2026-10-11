"""Leave inert permission/link fixtures for the canonical PARENT to clean.

No inner cleanup, no permission restoration and no in-process rmtree: a real
run_tests.sh parent must remove the worker after this pytest process exits.
"""
import json
import os
import stat

import pytest

from tests.scratch_isolation import LEASE


@pytest.mark.platforms('posix')
def test_leave_readonly_links_for_parent():
    assert LEASE is not None
    task, worker = LEASE.task, LEASE.worker
    sibling = task / 'retained-sibling'
    sibling.mkdir(mode=0o700)
    canary = sibling / 'canary'
    canary.write_bytes(b'outside worker: retain bytes and mode')
    canary.chmod(0o600)
    mode = stat.S_IMODE(canary.stat().st_mode)
    nested = worker / 'readonly'
    nested.mkdir()
    (nested / 'data').write_text('ordinary readonly subtree')
    (nested / 'link').symlink_to(canary)
    (nested / 'directory-link').symlink_to(sibling, target_is_directory=True)
    os.link(canary, nested / 'hardlink')
    child = nested / 'no-access'
    child.mkdir()
    (child / 'data').write_text('nested removal')
    child.chmod(0o000)
    nested.chmod(0o500)
    (task / 'cleanup-receipt.json').write_text(json.dumps({
        'worker': str(worker), 'canary': str(canary), 'mode': mode,
        'parent_pid': os.getppid(), 'child_pid': os.getpid(),
    }))
