"""Guard Python filesystem calls in tests, not arbitrary native/subprocess I/O."""
from __future__ import annotations

import builtins
from functools import lru_cache, wraps
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import url2pathname

_INTERPRETER_PREFIXES = tuple({
    Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)
} | {
    # A PM-activated developer shell runs sys.prefix's python against a dependency generation
    # whose site-packages sits under the (real) Hermes home; third-party imports from it are the
    # interpreter's installation, not Hermes state.
    Path(p).resolve() for p in sys.path if p and Path(p).name in ("site-packages", "dist-packages")
} | {
    # The default install checks the repo out INSIDE the home (install.sh:
    # INSTALL_DIR=$HERMES_HOME/hermes-agent). Reading test data, sources for tracebacks, or the
    # checkout's own .venv is not Hermes state; without this every run from a default install
    # trips on its first traceback.
    Path(__file__).resolve().parent.parent,
})
# The same prefixes as plain strings for the check() fast path. PurePath comparison folds case on
# Windows; ``os.path.normcase`` (identity on POSIX) reproduces that for string compares. Prefixes
# resolve once at import, as before: they are fixed for the process lifetime.
_normcase = os.path.normcase
_INTERPRETER_PREFIX_STRS = tuple(_normcase(os.fspath(p)) for p in _INTERPRETER_PREFIXES)


def _within(path: str, prefix: str) -> bool:
    """``Path(path).is_relative_to(prefix)`` for two normalized, case-folded absolute strings."""
    if path == prefix:
        return True
    return path.startswith(prefix) if prefix == os.sep else path.startswith(prefix + os.sep)


def _contains(path: str, prefix: str) -> bool:
    """``Path(prefix).is_relative_to(path)``: *path* is *prefix* or one of its ancestors."""
    if path == prefix:
        return True
    return prefix.startswith(path) if path == os.sep else prefix.startswith(path + os.sep)


class HomeIOGuard:
    def __init__(self, roots, installed_apps=lambda: (), *, scratch=None):
        self.roots = roots
        self.scratch = scratch
        # The machine's installed desktop app (``packaged_gui_app_paths()`` before any test ran):
        # deleting, replacing or writing into it is refused. Reads stay allowed: several update
        # paths only probe it.
        self.installed_apps = installed_apps
        self.checking = threading.local()
        self.directories: dict[int, Path] = {}

    def check(self, value, *, dir_fd=None, metadata=False, destructive=False):
        if value is None or isinstance(value, int) or getattr(self.checking, "active", False):
            return
        self.checking.active = True
        try:
            candidate = os.fsdecode(value)
            if candidate.startswith("~"):
                # A test may have patched Path.expanduser to fail; the guard must not
                # turn that into its own crash — the unexpanded path is checked instead.
                try:
                    candidate = os.fspath(Path(candidate).expanduser())
                except Exception:
                    pass
            if dir_fd is not None and not os.path.isabs(candidate):
                parent = self.directories.get(dir_fd)
                if parent is None:
                    raise AssertionError("TEST BUG: untracked dir_fd in guarded filesystem I/O")
                candidate = os.path.join(os.fspath(parent), candidate)
            # Keep the original spelling for physical resolution. Explicit
            # tasks and trusted-prefix inputs must not erase parent traversal.
            raw_path = Path(candidate) if os.path.isabs(candidate) else Path.cwd() / candidate
            raw_absolute = _normcase(os.fspath(raw_path))
            if '..' in raw_path.parts and (self.scratch is not None or any(
                    _within(raw_absolute, prefix) for prefix in _INTERPRETER_PREFIX_STRS)):
                self.refuse(value)
            absolute = (raw_absolute if self.scratch is not None
                        else _normcase(os.path.abspath(candidate)))
            # /proc/<pid>/fd/N is descriptor inspection (deleted-WAL holder scans stat the magic
            # link to compare inode identity); resolving it names whatever file that fd holds,
            # which is not I/O against the home.
            if metadata and (absolute == "/proc" or absolute.startswith("/proc" + os.sep)):
                return
            resolved = self._refuse_installed_app_change(value, absolute) if destructive else None
            roots = tuple(_normcase(os.fspath(r)) for r in self.roots())
            if self.scratch is not None:
                task = _normcase(os.fspath(self.scratch.task))
                if _within(absolute, task):
                    if (not self.scratch.allows(absolute, roots)
                            or (destructive and absolute == task)):
                        self.refuse(value)
                    return
                # realpath/mkdir traverses ancestors. Only metadata on the
                # exact canonical spine is needed, not reads/listing/writes.
                if metadata and self.scratch.ancestor_metadata(absolute):
                    return
            # Resolving the root itself (get_default_hermes_root's relative_to
            # probe) reads no state; only its contents are guarded.
            if metadata and absolute in roots:
                return
            # Source/interpreter allowances are narrow descendants of a denied
            # installation root, never authority over another denied target.
            # A nested deny-model inside a trusted prefix still wins.
            prefixes = _INTERPRETER_PREFIX_STRS
            if metadata:
                path = os.environ.get("PATH", "")
                cwd = os.getcwd() if self._relative_path_entries(path) else None
                if os.path.dirname(absolute) in self._path_entries(path, cwd):
                    prefixes = (*prefixes, _normcase(os.path.realpath(os.path.dirname(absolute))))

            def permitted_installation_target(target, root):
                return any(prefix != root and _within(prefix, root)
                           and (_within(target, prefix) or (metadata and _contains(target, prefix)))
                           for prefix in prefixes)

            # Refuse a lexical denied path before probing it, unless it is a
            # legitimate installation path. Then independently check the target
            # the OS follows; no lexical-prefix early return may skip this.
            for root in roots:
                if _within(absolute, root) and not permitted_installation_target(absolute, root):
                    self.refuse(value)
            if resolved is None:
                resolved = _normcase(os.path.realpath(raw_absolute))
            if metadata and resolved in roots:
                return
            for root in roots:
                if _within(resolved, root) and not permitted_installation_target(resolved, root):
                    self.refuse(value)
        finally:
            self.checking.active = False

    def _refuse_installed_app_change(self, value, absolute):
        """Fail on a change to an installed app (already normalized, literal and resolved);
        returns the resolved path so check() does not resolve it again, or None."""
        apps = self.installed_apps()
        if not apps:
            return None
        resolved = _normcase(os.path.realpath(absolute))
        for app in apps:
            if _within(absolute, app) or _within(resolved, app):
                import pytest  # noqa: PLC0415
                # pytest.fail, not AssertionError: removal helpers catch Exception and would log
                # the refusal as a warning while the test passed.
                pytest.fail(f"TEST BUG: changing the REAL installed Hermes desktop app: {value}\n"
                            "Stub hermes_cli.gui_uninstall.packaged_gui_app_paths to a temporary path.")
        return resolved

    @staticmethod
    def refuse(value):
        raise AssertionError(
            f"TEST BUG: file I/O against the REAL hermes home: {value}\n"
            "Use the isolated HERMES_HOME or a temporary fixture instead."
        )

    @staticmethod
    @lru_cache(maxsize=8)
    def _relative_path_entries(path: str) -> bool:
        return any(entry and not os.path.isabs(entry) for entry in path.split(os.pathsep))

    @staticmethod
    @lru_cache(maxsize=8)
    def _path_entries(path: str, cwd: str | None):
        # Relative PATH entries change meaning after chdir; absolute ones need no cwd.
        return frozenset(
            _normcase(os.path.normpath(os.path.join(cwd or "", entry)))
            for entry in path.split(os.pathsep) if entry
        )

    def install(self, monkeypatch):
        def wrap(module, name, parameters, *, metadata=False, destructive=False):
            original = getattr(module, name)

            @wraps(original)
            def guarded(*args, **kwargs):
                for index, (parameter, descriptor) in enumerate(parameters):
                    value = args[index] if index < len(args) else kwargs.get(parameter)
                    self.check(value, dir_fd=kwargs.get(descriptor) if descriptor else None, metadata=metadata,
                               destructive=destructive(args, kwargs) if callable(destructive) else destructive)
                return original(*args, **kwargs)

            monkeypatch.setattr(module, name, guarded)

        def open_writes(args, kwargs):
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            return any(flag in mode for flag in "wax+")

        for module in (builtins, io):
            wrap(module, "open", (("file", None),), destructive=open_writes)
        for name in ("mkdir", "unlink", "remove", "rmdir", "chmod", "utime"):
            wrap(os, name, (("path", "dir_fd"),), destructive=name != "mkdir")
        for name in ("stat", "lstat", "readlink", "access"):
            wrap(os, name, (("path", "dir_fd"),), metadata=True)
        for name in ("makedirs", "listdir", "scandir"):
            wrap(os, name, (("name" if name == "makedirs" else "path", None),))
        for name in ("rename", "replace"):
            wrap(os, name, (("src", "src_dir_fd"), ("dst", "dst_dir_fd")), destructive=True)
        wrap(shutil, "rmtree", (("path", "dir_fd"),), destructive=True)
        original_connect = sqlite3.connect

        @wraps(original_connect)
        def guarded_connect(*args, **kwargs):
            database = args[0] if args else kwargs.get('database')
            uri = args[7] if len(args) > 7 else kwargs.get('uri', False)
            target = self._sqlite_target(database, uri=uri)
            if target is not None:
                self.check(target)
                # SQLite may create these beside the database without going
                # through Python's open wrapper. Validate existing aliases too.
                for spelling in {os.fspath(target), os.path.realpath(target)}:
                    for suffix in ('-journal', '-wal', '-shm'):
                        self.check(spelling + suffix)
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, 'connect', guarded_connect)

        original_open, original_close = os.open, os.close

        @wraps(original_open)
        def guarded_open(path, flags, *args, **kwargs):
            self.check(path, dir_fd=kwargs.get("dir_fd"), destructive=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            fd = original_open(path, flags, *args, **kwargs)
            candidate = Path(os.fsdecode(path))
            if kwargs.get("dir_fd") is not None and not candidate.is_absolute():
                candidate = self.directories[kwargs["dir_fd"]] / candidate
            self.directories[fd] = candidate.absolute()
            return fd

        @wraps(original_close)
        def guarded_close(fd):
            # Forget the old owner before close releases the number for reuse
            # by another thread's open; afterwards we could erase its mapping.
            self.directories.pop(fd, None)
            return original_close(fd)

        monkeypatch.setattr(os, "open", guarded_open)
        monkeypatch.setattr(os, "close", guarded_close)

    @staticmethod
    def _sqlite_target(database, *, uri):
        """Decode supported SQLite URI filenames; refuse ambiguous VFS forms."""
        value = os.fsdecode(database)
        if value == ':memory:':
            return None
        if not value.startswith('file:'):
            if not value:
                raise AssertionError('TEST BUG: SQLite temporary database has no guarded target')
            return Path(value)
        error = 'TEST BUG: unsupported SQLite URI in guarded connect'
        if (not uri or '#' in value or any(ord(c) < 32 for c in value)
                or re.search(r'%(?![0-9a-fA-F]{2})', value)):
            raise AssertionError(error)
        try:
            parsed = urlsplit(value)
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                              errors='strict')
            options = dict(pairs)
            allowed = {'mode': {'ro', 'rw', 'rwc', 'memory'},
                       'cache': {'private', 'shared'}, 'immutable': {'0', '1'}}
            if (parsed.netloc not in ('', 'localhost') or len(options) != len(pairs)
                    or any(key not in allowed or val not in allowed[key] for key, val in pairs)):
                raise ValueError('unsupported authority or options')
            decoded = unquote(parsed.path, errors='strict')
            if not decoded or '\x00' in decoded:
                raise ValueError('empty or NUL filename')
            if decoded == ':memory:' or options.get('mode') == 'memory':
                return None
            # url2pathname decodes on Windows itself; do not double-unquote.
            filename = url2pathname(parsed.path) if os.name == 'nt' else decoded
            path = Path(filename)
            return path if path.is_absolute() else Path.cwd() / path
        except (ValueError, UnicodeError) as exc:
            raise AssertionError(error) from exc
