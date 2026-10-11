"""Source-first tests for a narrow scratch exception, never a fake HOME.

All mutation negatives use disposable canaries. Real protected paths are checked
by the guard predicate only, before any filesystem operation is dispatched.
"""
import os
from pathlib import Path
import sqlite3

import pytest

from scripts.runner_scratch import claim_scratch_root, worker_receipt, receive_worker
from tests.home_io_guard import HomeIOGuard


def _lease(tmp_path):
    task = tmp_path / 'owned-task'
    claim = claim_scratch_root(str(task))
    worker = claim.create_worker()
    # The production receiver binds to its actual parent. The pure decoder has
    # an explicit expected-parent argument for this same-process unit test.
    receipt = worker_receipt(worker)
    return receive_worker(receipt, expected_parent=os.getpid()), task


@pytest.mark.platforms('posix')
def test_owned_task_allows_real_io_but_keeps_inert_siblings_protected(tmp_path, monkeypatch):
    model_native = tmp_path / 'inert-native'
    base = model_native / 'profiles' / 'owner' / 'cache' / 'scratch'
    base.mkdir(parents=True)
    lease, task = _lease(base)
    sibling = model_native / 'profiles' / 'sibling'
    sibling.mkdir()
    targets = [model_native / 'state.db', model_native / 'config.yaml',
               model_native / 'auth.json', sibling / 'auth.json',
               sibling / 'config.yaml', sibling / 'state.db',
               base / 'sibling-task-canary']
    for target in targets:
        target.write_text('unchanged', encoding='utf-8')
    native = (Path.home() / '.hermes').resolve()
    guard = HomeIOGuard(lambda: [native, model_native], scratch=lease)
    with monkeypatch.context() as patcher:
        guard.install(patcher)
        good = task / 'permitted.txt'
        good.write_text('inert', encoding='utf-8')
        assert good.read_text(encoding='utf-8') == 'inert'
        with sqlite3.connect(task / 'test.db') as db:
            db.execute('create table inert (value text)')
        for target in targets:
            with pytest.raises(AssertionError, match='REAL hermes home'):
                target.write_text('bad', encoding='utf-8')
            with pytest.raises(AssertionError, match='REAL hermes home'):
                good.replace(target)
            with pytest.raises(AssertionError, match='REAL hermes home'):
                sqlite3.connect(target).close()
        with pytest.raises(AssertionError, match='REAL hermes home'):
            os.listdir(model_native)
    for target in targets:
        assert target.read_text(encoding='utf-8') == 'unchanged'


@pytest.mark.platforms('posix')
def test_real_native_and_profile_identities_are_not_replaced(tmp_path):
    from hermes_state_guard import _real_platform_state_root, _is_production_state_db

    before = _real_platform_state_root()
    assert before is not None
    lease, task = _lease(tmp_path)
    assert _real_platform_state_root() == before
    assert lease.home == os.path.expanduser('~')
    guard = HomeIOGuard(lambda: [before], scratch=lease)
    for target in (before / 'state.db', before / 'auth.json', before / 'config.yaml',
                   before / 'profiles' / 'inert-profile' / 'state.db',
                   before / 'profiles' / 'inert-profile' / 'auth.json',
                   before / 'profiles' / 'inert-profile' / 'config.yaml'):
        # Predicate only: never open, stat data, create, or modify real state.
        with pytest.raises(AssertionError, match='REAL hermes home'):
            guard.check(target, destructive=True)
    assert _is_production_state_db(before / 'state.db', before)
    assert _is_production_state_db(before / 'profiles' / 'inert-profile' / 'state.db', before)
    guard.check(task / 'legitimate.txt', destructive=True)


@pytest.mark.platforms('posix')
def test_symlink_hardlink_and_parent_escape_do_not_inherit_allowance(tmp_path):
    lease, task = _lease(tmp_path)
    outside = tmp_path / 'sibling-secret'
    outside.write_text('unchanged', encoding='utf-8')
    # Keep an inert read-only witness open before making the hardlink. The
    # outer canonical task guard must also refuse fresh opens of that inode
    # once it has multiple links; do not weaken it just to read our canary.
    with outside.open(encoding='utf-8') as witness:
        (task / 'alias').symlink_to(outside)
        os.link(outside, task / 'hardlink')
        guard = HomeIOGuard(lambda: [tmp_path], scratch=lease)
        for target in (task / 'alias', task / 'hardlink', task / '..' / outside.name,
                       task.parent, task.with_name(task.name + '-sibling')):
            with pytest.raises(AssertionError, match='REAL hermes home'):
                guard.check(target, destructive=True)
        assert witness.read() == 'unchanged'


@pytest.mark.platforms('posix')
def test_claim_is_exclusive_and_receipt_rejects_identity_mismatch(tmp_path):
    import json

    lease, task = _lease(tmp_path)
    with pytest.raises((ValueError, FileExistsError)):
        claim_scratch_root(str(task))
    receipt = json.loads(worker_receipt(lease))
    receipt['task_identity'][1] += 1
    with pytest.raises(ValueError):
        receive_worker(json.dumps(receipt), expected_parent=os.getpid())
    receipt = json.loads(worker_receipt(lease))
    receipt['parent'] += 1
    with pytest.raises(ValueError):
        receive_worker(json.dumps(receipt), expected_parent=os.getpid())
    assert lease.contains(task / 'fixture')


@pytest.mark.platforms('posix')
def test_replaced_task_loses_allowance(tmp_path):
    lease, task = _lease(tmp_path)
    task.rename(tmp_path / 'retained-original')
    task.mkdir(mode=0o700)
    assert not lease.contains(task / 'new-file')
    guard = HomeIOGuard(lambda: [tmp_path], scratch=lease)
    with pytest.raises(AssertionError, match='REAL hermes home'):
        guard.check(task / 'new-file', destructive=True)
