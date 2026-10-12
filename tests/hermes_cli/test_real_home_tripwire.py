"""Real-home guards are exercised against disposable protected roots only."""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import sqlite3
import runpy
import sys
import tempfile

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def protected_home(tmp_path, monkeypatch):
    from tests import conftest

    root = tmp_path / "protected"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    (root / "empty").mkdir()
    original_roots = list(conftest._REAL_HERMES_ROOT_CANDIDATES)
    with monkeypatch.context() as model:
        model.setattr(conftest, "_REAL_HERMES_ROOT_CANDIDATES", [*original_roots, root])
        yield root
    # Only our disposable model is removed; native/sibling protection remains.


def _open_close(path):
    with open(path, encoding="utf-8"):
        pass


def _io_open_close(path):
    with io.open(path, encoding="utf-8"):
        pass


def _os_open_close(path):
    descriptor = os.open(path, os.O_RDONLY)
    os.close(descriptor)


_OPERATIONS = {
    "builtin-open": _open_close,
    "io-open": _io_open_close,
    "os-open": _os_open_close,
    "read-text": lambda path: path.read_text(encoding="utf-8"),
    "write-text": lambda path: path.write_text("changed", encoding="utf-8"),
    "read-bytes": lambda path: path.read_bytes(),
    "write-bytes": lambda path: path.write_bytes(b"changed"),
    "stat": lambda path: path.stat(),
    "lstat": lambda path: path.lstat(),
    "unlink": lambda path: path.unlink(),
    "remove": lambda path: os.remove(path),
    "rename-out": lambda path: path.rename(path.parent.parent / "moved.txt"),
    "replace-out": lambda path: path.replace(path.parent.parent / "replaced.txt"),
    "mkdir": lambda path: (path.parent / "new").mkdir(),
    "makedirs": lambda path: os.makedirs(path.parent / "deep" / "child"),
    "rmdir": lambda path: (path.parent / "empty").rmdir(),
    "rmtree": lambda path: shutil.rmtree(path.parent),
    "listdir": lambda path: os.listdir(path.parent),
    "scandir": lambda path: list(os.scandir(path.parent)),
    "sqlite": lambda path: sqlite3.connect(path.parent / "state.db").close(),
}


@pytest.mark.parametrize("operation", _OPERATIONS, ids=_OPERATIONS)
def test_io_guard_denies_protected_roots_before_mutation(protected_home, operation):
    with pytest.raises((AssertionError, pytest.fail.Exception), match="REAL hermes home"):
        _OPERATIONS[operation](protected_home / "file.txt")


def test_rename_cannot_overwrite_a_protected_destination(protected_home, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("external", encoding="utf-8")
    with pytest.raises((AssertionError, pytest.fail.Exception), match="REAL hermes home"):
        source.replace(protected_home / "file.txt")
    assert source.read_text(encoding="utf-8") == "external"


def test_installed_desktop_app_can_be_read_but_never_changed(tmp_path, monkeypatch):
    from tests import conftest

    app = tmp_path / "Applications" / "Hermes.app"
    (app / "Contents").mkdir(parents=True)
    plist = app / "Contents" / "Info.plist"
    plist.write_text("unchanged", encoding="utf-8")
    replacement = tmp_path / "Hermes.app.new"
    replacement.mkdir()
    changes = [lambda: shutil.rmtree(app), plist.unlink, lambda: replacement.replace(app),
               lambda: plist.write_text("clobbered", encoding="utf-8")]
    try:  # Windows without Developer Mode cannot create symlinks
        (tmp_path / "linked").symlink_to(app.parent, target_is_directory=True)
        changes.append(lambda: shutil.rmtree(tmp_path / "linked" / "Hermes.app"))
    except OSError:
        pass
    monkeypatch.setattr(conftest, "_REAL_INSTALLED_GUI_APPS", [os.path.normcase(os.path.realpath(app))])
    assert plist.read_text(encoding="utf-8") == "unchanged"
    for change in changes:
        with pytest.raises(pytest.fail.Exception, match="REAL installed Hermes desktop app"):
            change()
    assert plist.read_text(encoding="utf-8") == "unchanged"


def test_unprotected_paths_and_open_descriptors_still_work(protected_home, tmp_path):
    target = tmp_path / "allowed" / "file.txt"
    target.parent.mkdir()
    target.write_text("ok", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "ok"
    fd = os.open(target, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        assert stream.read() == "ok"
    moved = target.replace(target.with_name("moved.txt"))
    assert list(moved.parent.iterdir()) == [moved]
    moved.unlink()
    moved.parent.rmdir()
    connection = sqlite3.connect(tmp_path / "allowed.db")
    connection.close()


@pytest.mark.allow_real_home_io
def test_explicit_opt_out_allows_only_the_disposable_canary(protected_home):
    target = protected_home / "file.txt"
    target.write_text("opted out", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "opted out"


def test_close_keeps_a_reused_descriptors_new_owner(tmp_path, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    first, second = tmp_path / "first", tmp_path / "second"
    first.touch()
    second.touch()
    original_close = os.close
    reopened = []

    def close_and_reopen(fd):
        original_close(fd)
        reopened.append(os.open(second, os.O_RDONLY))

    guard = HomeIOGuard(lambda: [])
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "close", close_and_reopen)
            guard.install(patcher)
            fd = os.open(first, os.O_RDONLY)
            os.close(fd)
            assert reopened == [fd], "the test must exercise descriptor reuse"
            assert guard.directories[fd] == second
    finally:
        for fd in reopened:
            original_close(fd)


def test_path_metadata_exemption_tracks_path_changes(protected_home, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    target = protected_home / "file.txt"
    guard = HomeIOGuard(lambda: [protected_home])
    monkeypatch.setenv("PATH", str(protected_home))
    guard.check(target, metadata=True)  # executable lookup, not a state read
    monkeypatch.setenv("PATH", str(protected_home.parent))
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(target, metadata=True)


def test_relative_path_metadata_exemption_tracks_working_directory(protected_home, tmp_path, monkeypatch):
    from tests.home_io_guard import HomeIOGuard

    target = protected_home / "file.txt"
    guard = HomeIOGuard(lambda: [protected_home])
    other = tmp_path / "other"
    other.mkdir()
    (other / "nested").mkdir()
    monkeypatch.setenv("PATH", "../protected")
    monkeypatch.chdir(other)
    guard.check(target, metadata=True)
    monkeypatch.chdir(other / "nested")
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(target, metadata=True)


def test_checkout_inside_a_guarded_root_is_not_hermes_state():
    """The default install checks the repo out INSIDE the home (install.sh:
    INSTALL_DIR=$HERMES_HOME/hermes-agent): the checkout, its .venv and test
    data are exempt even when the guarded root contains them; siblings under
    that root are still refused."""
    from tests.home_io_guard import HomeIOGuard

    guard = HomeIOGuard(lambda: [PROJECT_ROOT.parent])
    guard.check(PROJECT_ROOT / "tests" / "home_io_guard.py")
    guard.check(PROJECT_ROOT / ".venv" / "bin" / "python", metadata=True)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(PROJECT_ROOT.parent / "config.yaml")


@pytest.mark.parametrize('exported', [True, False], ids=['reject-exported', 'retain-owned'])
def test_hermes_exported_scratch_tmp_is_not_the_test_temp_root(tmp_path, monkeypatch, exported):
    """Execute the actual complete conftest body after supplying the bad input.

    The canonical parent must not scrub this input before the decision. No
    subprocess, fake HOME, plugin disabling or extracted decision replica.
    TMP/TEMP retain the owned worker as the safe real tempfile fallback.
    """
    import hermes_constants as constants
    from hermes_state_guard import _real_platform_state_root
    from tests.scratch_isolation import LEASE

    native, real_home = _real_platform_state_root(), os.environ['HOME']
    owned_temp = LEASE.worker if LEASE is not None else tmp_path / 'safe-temp'
    if LEASE is None:
        owned_temp.mkdir()
    home = tmp_path / 'home'
    scratch = home / 'cache' / 'scratch'
    scratch.mkdir(parents=True)
    before = dict(os.environ)
    old_tempdir = tempfile.tempdir
    old_prefix = sys.pycache_prefix
    try:
        with monkeypatch.context() as model:
            # run_path installs collection-time resolvers by assignment. Capture
            # their exact previous objects for restoration, not new substitutes.
            model.setattr(constants, '_get_platform_default_hermes_home',
                          constants._get_platform_default_hermes_home)
            model.setattr(constants, 'named_profile_home', constants.named_profile_home)
            os.environ['HERMES_HOME'] = str(home)
            os.environ['TMPDIR'] = str(scratch if exported else owned_temp)
            os.environ['TMP'] = os.environ['TEMP'] = str(owned_temp)
            if exported:
                os.environ['HERMES_SCRATCH_DIR'] = str(scratch)
            else:
                os.environ.pop('HERMES_SCRATCH_DIR', None)
            # Assert input immediately before the real decision is loaded.
            assert os.environ['TMPDIR'] == str(scratch if exported else owned_temp)
            namespace = runpy.run_path(str(PROJECT_ROOT / 'tests/conftest.py'),
                                      run_name='compatibility_conftest_decision')
            assert namespace['_HERMES_EXPORTED_TMP'] == (str(scratch) if exported else '')
            assert 'HERMES_SCRATCH_DIR' not in os.environ
            assert Path(tempfile.gettempdir()) == owned_temp
            with tempfile.TemporaryDirectory() as made:
                assert Path(made).is_relative_to(owned_temp)
                assert not Path(made).is_relative_to(home)
            assert os.environ['HOME'] == real_home
            assert _real_platform_state_root() == native
    finally:
        os.environ.clear()
        os.environ.update(before)
        tempfile.tempdir = old_tempdir
        sys.pycache_prefix = old_prefix
