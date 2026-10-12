"""B2: actual dangling identity under installed wrappers, never live negatives.

Regression source precedes implementation. Execution requires affected safety
clearance; the original core failure remains the observed RED.
"""
import errno
import os
from pathlib import Path
import sqlite3
import stat
from dataclasses import replace

import pytest

from hermes_constants import named_profile_has_identity
from scripts import runner_scratch as scratch
from tests.home_io_guard import HomeIOGuard


def _model(tmp_path):
    lease = scratch.claim_scratch_root(str(tmp_path / 'task')).create_worker()
    home = lease.worker / 'profiles' / 'legacy'
    home.mkdir(parents=True)
    marker = home / 'config.yaml'
    return lease, home, marker


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('target_kind', ['absolute', 'relative', 'chain', 'directory-alias'])
def test_dangling_identity_uses_real_wrappers_without_general_io_allowance(tmp_path, monkeypatch, target_kind):
    lease, home, marker = _model(tmp_path)
    missing = lease.worker / 'gone' / 'config.yaml'
    if target_kind == 'relative':
        marker.symlink_to('gone/config.yaml')
    elif target_kind == 'chain':
        link = lease.worker / 'second-marker'
        link.symlink_to(missing)
        marker.symlink_to(link)
    elif target_kind == 'directory-alias':
        directory = lease.worker / 'directory'
        directory.mkdir()
        alias = lease.worker / 'directory-alias'
        alias.symlink_to(directory, target_is_directory=True)
        marker.symlink_to(alias / 'missing')
    else:
        marker.symlink_to(missing)
    original_link = os.readlink(marker)
    real_home = (os.environ['HOME'], Path.home())
    with monkeypatch.context() as scoped:
        guard = HomeIOGuard(lambda: [tmp_path], scratch=lease)
        guard.install(scoped)
        assert not marker.is_file()  # actual following stat yields ENOENT
        assert marker.is_symlink()  # actual no-follow identity, not a stub
        assert named_profile_has_identity(home)
        assert stat.S_ISLNK(os.lstat(marker).st_mode)
        assert stat.S_ISLNK(os.stat(marker, follow_symlinks=False).st_mode)
        assert os.readlink(marker) == original_link
        with pytest.raises(FileNotFoundError) as absent:
            os.stat(marker)
        assert absent.value.errno == errno.ENOENT
        fd = os.open(home, os.O_RDONLY)
        try:
            assert stat.S_ISLNK(os.stat('config.yaml', dir_fd=fd, follow_symlinks=False).st_mode)
            assert os.readlink('config.yaml', dir_fd=fd) == original_link
            with pytest.raises(FileNotFoundError):
                os.stat('config.yaml', dir_fd=fd)
        finally:
            os.close(fd)
        # Metadata must not authorize a general lease, access(), or data I/O.
        assert not lease.contains(marker)
        assert not lease.allows(marker, [tmp_path])
        with pytest.raises(AssertionError, match='REAL hermes home'):
            guard.check(marker, metadata=True)  # generic metadata is not authority
        assert not lease.allows_missing_metadata(marker, [tmp_path], operation='access')
        with pytest.raises(AssertionError, match='REAL hermes home'):
            os.access(marker, os.F_OK)
        with pytest.raises(AssertionError, match='REAL hermes home'):
            marker.read_bytes()
    assert (os.environ['HOME'], Path.home()) == real_home
    assert os.readlink(marker) == original_link


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('boundary', [
    'outside-existing', 'outside-missing', 'sibling-task', 'native-model',
    'profile-model', 'nested-deny', 'deny-intermediate', 'lexical-deny',
    'parent-traversal', 'target-traversal', 'cycle', 'dangling-parent',
    'escaping-parent', 'multi-link',
])
@pytest.mark.parametrize('operation', ['stat', 'lstat', 'readlink'])
def test_marker_metadata_refuses_unsafe_resolution_chain(tmp_path, monkeypatch, boundary, operation):
    lease, home, marker = _model(tmp_path)
    outside = tmp_path / 'protected'
    outside.mkdir()
    witness_path = outside / 'canary'
    witness_path.write_bytes(b'unchanged')
    deny = lease.task / 'deny'
    deny.mkdir()
    roots = [tmp_path, deny]
    target = lease.worker / 'gone' / 'config.yaml'
    with witness_path.open('rb') as witness:
        if boundary == 'outside-existing':
            target = witness_path
        elif boundary == 'outside-missing':
            target = outside / 'missing'
        elif boundary in ('sibling-task', 'native-model', 'profile-model'):
            target = tmp_path / boundary / 'missing'
        elif boundary == 'nested-deny':
            target = deny / 'missing'
        elif boundary == 'deny-intermediate':
            target = deny / 'link'
            target.symlink_to(lease.worker / 'missing')
        elif boundary == 'lexical-deny':
            marker = deny / 'config.yaml'
        elif boundary == 'target-traversal':
            target = home / '..' / 'missing'
        elif boundary == 'cycle':
            target = lease.worker / 'loop'
            target.symlink_to(marker)
        elif boundary in ('dangling-parent', 'escaping-parent'):
            parent = lease.worker / 'alias'
            parent.symlink_to(lease.worker / 'missing-directory' if boundary == 'dangling-parent' else outside)
            target = parent / 'missing'
        elif boundary == 'multi-link':
            target = lease.worker / 'hard'
            os.link(witness_path, target)
        marker.symlink_to(target)
        query = home / '..' / 'legacy' / 'config.yaml' if boundary == 'parent-traversal' else marker
        with monkeypatch.context() as scoped:
            HomeIOGuard(lambda: roots, scratch=lease).install(scoped)
            with pytest.raises(AssertionError, match='REAL hermes home'):
                getattr(os, operation)(query)
            fd = os.open(lease.task, os.O_RDONLY)
            try:
                with pytest.raises(AssertionError, match='REAL hermes home'):
                    getattr(os, operation)(str(query.relative_to(lease.task)), dir_fd=fd)
            finally:
                os.close(fd)
        assert witness.read() == b'unchanged'
    # In the multi-link row the outer guard rightly refuses reopening the inode.
    # The descriptor witness above proves unchanged content without bypassing it.


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('operation', ['read', 'write', 'fd-open', 'sqlite', 'unlink', 'replace', 'chmod', 'mkdir'])
def test_dangling_metadata_does_not_expand_ordinary_operations(tmp_path, monkeypatch, operation):
    lease, _, marker = _model(tmp_path)
    missing = lease.worker / 'missing'
    marker.symlink_to(missing)
    good = lease.worker / 'good'
    good.write_bytes(b'good')
    with monkeypatch.context() as scoped:
        HomeIOGuard(lambda: [tmp_path], scratch=lease).install(scoped)
        assert marker.is_symlink()
        with pytest.raises(AssertionError, match='REAL hermes home'):
            if operation == 'read':
                marker.read_bytes()
            elif operation == 'write':
                marker.write_bytes(b'bad')
            elif operation == 'fd-open':
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
            elif operation == 'sqlite':
                sqlite3.connect(marker).close()
            elif operation == 'unlink':
                marker.unlink()
            elif operation == 'replace':
                os.replace(good, marker)
            elif operation == 'chmod':
                os.chmod(marker, 0o600)
            else:
                marker.mkdir()
        assert not lease.contains(marker)
    assert os.readlink(marker) == str(missing)
    assert not missing.exists()
    assert good.read_bytes() == b'good'
    assert not (lease.worker / 'missing-journal').exists()
    assert not (lease.worker / 'missing-wal').exists()
    assert not (lease.worker / 'missing-shm').exists()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('fault', [errno.EACCES, errno.EIO, errno.ELOOP, errno.ENOTDIR])
@pytest.mark.parametrize('probe', ['_LSTAT', '_STAT', '_READLINK'])
def test_metadata_uncertainty_fails_closed_without_shared_os_mutation(tmp_path, monkeypatch, fault, probe):
    lease, _, marker = _model(tmp_path)
    marker.symlink_to(lease.worker / 'missing')
    original = getattr(scratch, probe)

    def uncertain(path, *args, **kwargs):
        if Path(path) == marker:
            raise OSError(fault, 'inert metadata uncertainty')
        return original(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        # Only the captured consumer dependency is replaced, never shared os.
        scoped.setattr(scratch, probe, uncertain)
        HomeIOGuard(lambda: [tmp_path], scratch=lease).install(scoped)
        with pytest.raises(AssertionError, match='REAL hermes home'):
            os.stat(marker)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('changed', ['task_identity', 'worker_identity', 'home'])
def test_dangling_metadata_requires_original_lease_identity(tmp_path, monkeypatch, changed):
    lease, _, marker = _model(tmp_path)
    marker.symlink_to(lease.worker / 'missing')
    value = 'not-the-real-home' if changed == 'home' else (0, 0, 0)
    stale = replace(lease, **{changed: value})
    with monkeypatch.context() as scoped:
        HomeIOGuard(lambda: [tmp_path], scratch=stale).install(scoped)
        with pytest.raises(AssertionError, match='REAL hermes home'):
            os.lstat(marker)
