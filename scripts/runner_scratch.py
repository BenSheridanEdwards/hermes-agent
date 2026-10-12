"""Canonical-runner scratch validation and a process-bound test-only lease.

No production guard imports, HOME rewriting, or environment-based bypass.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

# Capture before pytest installs file wrappers. Used for lease identity and
# the operation-specific, contained missing-marker proof, never data reads.
_STAT = os.stat
_LSTAT = os.lstat
_READLINK = os.readlink


def native_root() -> Path:
    home = Path(os.path.expanduser('~'))
    return ((Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local'))) / 'hermes')
            if sys.platform == 'win32' else home / '.hermes').resolve()


def _identity(path: Path) -> tuple[int, int, int]:
    info = _STAT(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:  # windows-footgun: ok - explicit leases are POSIX-only
        raise ValueError('--scratch-root needs a private, owned directory (0700)')
    return info.st_dev, info.st_ino, info.st_uid


def _profile_task(path: Path, native: Path) -> bool:
    try:
        parts = path.relative_to(native).parts
    except ValueError:
        return False
    return (len(parts) >= 5 and parts[0] == 'profiles'
            and parts[1] not in ('.', '..', '.deleted')
            and parts[2:4] == ('cache', 'scratch'))


def validate_parent_metadata(info, *, uid: int, accessible: bool) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or info.st_mode & 0o022 or not info.st_mode & stat.S_IWUSR or not accessible):
        raise ValueError('--scratch-root parent must be owned, non-shared and writable')


def validate_scratch_root(value: str, *, new: bool = False) -> str:
    if not hasattr(os, 'getuid'):
        raise ValueError('--scratch-root ownership is currently POSIX-only')
    if not value or any(c in value for c in '\x00\r\n'):
        raise ValueError('--scratch-root must be a nonempty absolute directory')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('--scratch-root must be absolute without parent traversal')
    if path != path.resolve(strict=not new):
        raise ValueError('--scratch-root must be canonical, not a symlink alias')
    home, native = Path(os.path.expanduser('~')).resolve(), native_root()
    if path == Path(path.anchor) or path == home:
        raise ValueError('--scratch-root cannot be a filesystem root or HOME')
    if path.is_relative_to(native) and not _profile_task(path, native):
        raise ValueError('--scratch-root inside native home must be a task below a profile cache/scratch')
    custom = os.environ.get('HERMES_HOME')
    if custom:
        protected = Path(custom).resolve()
        if path.is_relative_to(protected) and not _profile_task(path, native):
            raise ValueError('--scratch-root cannot be inside a custom Hermes home')
    if new:
        if os.path.lexists(path):
            raise ValueError('--scratch-root must not exist; each invocation owns a fresh task')
        parent = path.parent
        info = parent.stat()
        validate_parent_metadata(info, uid=os.getuid(), accessible=os.access(parent, os.W_OK | os.X_OK))  # windows-footgun: ok - hasattr gate above
    else:
        _identity(path)
    return str(path)


@dataclass(frozen=True)
class ScratchClaim:
    task: Path
    task_identity: tuple[int, int, int]
    home: str
    native: str

    def verify(self):
        try:
            matches = (self.task == self.task.resolve()
                       and _identity(self.task) == self.task_identity
                       and self.home == os.path.expanduser('~')
                       and self.native == str(native_root()))
        except (OSError, RuntimeError):
            matches = False
        if not matches:
            raise ValueError('runner scratch task identity changed; retain uncertain paths')

    def create_worker(self) -> ScratchLease:
        self.verify()
        child = Path(tempfile.mkdtemp(prefix='r-', dir=self.task))
        # Capture once at creation, never re-bless a later pathname occupant.
        identity = _identity(child)
        self.verify()
        return ScratchLease(self.task, child, self.task_identity, identity, self.home)


def claim_scratch_root(value: str) -> ScratchClaim:
    root = Path(validate_scratch_root(value, new=True))
    os.mkdir(root, 0o700)  # exclusive: never adopt a previous or racing run
    return ScratchClaim(root, _identity(root), os.path.expanduser('~'), str(native_root()))


@dataclass(frozen=True)
class ScratchLease:
    task: Path
    worker: Path
    task_identity: tuple[int, int, int]
    worker_identity: tuple[int, int, int]
    home: str

    def verify(self):
        try:
            matches = (self.task == self.task.resolve() and self.worker == self.worker.resolve()
                       and _identity(self.task) == self.task_identity
                       and _identity(self.worker) == self.worker_identity
                       and self.home == os.path.expanduser('~'))
        except (OSError, RuntimeError):
            matches = False
        if not matches:
            raise ValueError('runner scratch directory identity changed; retain uncertain paths')

    def contains(self, value) -> bool:
        """Reject parent components before normalization; inspect the followed inode."""
        try:
            self.verify()
            path = Path(os.fsdecode(value))
            if '..' in path.parts:
                return False
            if not path.is_absolute():
                path = Path.cwd() / path
            resolved = path.resolve()
            if not path.is_relative_to(self.task) or not resolved.is_relative_to(self.task):
                return False
            try:
                # stat, not lstat: a symlink may point to an in-task hardlink.
                info = _STAT(resolved)
            except FileNotFoundError:
                # mkdir(parents=True) first attempts the deepest absent path.
                # Permit absent components, but not a dangling link masquerading
                # as absence. Other metadata errors reach the fail-closed handler.
                for component in (path, *path.parents):
                    if not component.is_relative_to(self.task):
                        break
                    try:
                        entry = _LSTAT(component)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(entry.st_mode):
                        _STAT(component)  # following a dangling link must fail
                return True
            return not stat.S_ISREG(info.st_mode) or info.st_nlink == 1
        except (OSError, ValueError, RuntimeError):
            return False

    def allows(self, value, roots) -> bool:
        """An explicit nested deny-model beats the enclosing task allowance."""
        if not self.contains(value):
            return False
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        for root in roots:
            root = Path(root)
            if root.is_relative_to(self.task) and (path.is_relative_to(root) or resolved.is_relative_to(root)):
                return False
        return True

    def allows_missing_metadata(self, value, roots, *, operation: str) -> bool:
        """Only stat/lstat/readlink of a proven contained dangling leaf link.

        A following stat must still report missing, and no data operation gains
        the general lease allowance. Inspect the whole chain, not realpath's
        best-effort missing suffix. This is a cooperative guard, not atomic path
        pinning against concurrent hostile replacement.
        """
        if operation not in ('stat', 'lstat', 'readlink'):
            return False
        try:
            self.verify()
            denied = tuple(Path(root) for root in roots if Path(root).is_relative_to(self.task))
            seen = set()

            def checked(path):
                if not path.is_absolute():
                    path = Path.cwd() / path
                if ('..' in path.parts or not path.is_relative_to(self.task)
                        or any(path.is_relative_to(root) for root in denied)):
                    raise ValueError('metadata chain leaves owned allowance')
                return path

            def walk(path):
                path = checked(path)
                current = self.task
                parts = path.relative_to(self.task).parts
                info = _LSTAT(current)
                for index, part in enumerate(parts):
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError('metadata parent is not a directory')
                    current = checked(current / part)
                    try:
                        info = _LSTAT(current)
                    except FileNotFoundError as exc:
                        if exc.errno != errno.ENOENT:
                            raise
                        # No unresolved parent alias reached this suffix: each
                        # previous component was inspected without following it.
                        return checked(current.joinpath(*parts[index + 1:])), None, False
                    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                        raise ValueError('metadata target has multiple links')
                    if stat.S_ISLNK(info.st_mode):
                        if current in seen or len(seen) >= 40:
                            raise ValueError('metadata link cycle or depth limit')
                        seen.add(current)
                        target = Path(_READLINK(current))
                        if not target.is_absolute():
                            target = current.parent / target
                        resolved, target_info, _ = walk(target)
                        if index == len(parts) - 1:
                            return resolved, target_info, True
                        # A dangling parent is not a missing marker. Existing
                        # owned directory aliases can continue to a leaf link.
                        if target_info is None or not stat.S_ISDIR(target_info.st_mode):
                            raise ValueError('metadata parent alias is unresolved')
                        current, info = resolved, target_info
                return current, info, False

            path = checked(Path(os.fsdecode(value)))
            _, info, leaf_link = walk(path)
            if not leaf_link or info is not None:
                return False
            try:
                _STAT(path)
            except FileNotFoundError as exc:
                if exc.errno != errno.ENOENT:
                    return False
            else:
                return False
            self.verify()
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def ancestor_metadata(self, value) -> bool:
        path = Path(os.fsdecode(value))
        if '..' in path.parts:
            return False
        return path in self.task.parents and path == path.resolve()


def worker_receipt(lease: ScratchLease) -> str:
    lease.verify()
    return json.dumps({
        'version': 1, 'task': str(lease.task), 'worker': str(lease.worker),
        'task_identity': lease.task_identity, 'worker_identity': lease.worker_identity,
        'home': lease.home, 'native': str(native_root()), 'parent': os.getpid(),
    }, separators=(',', ':'))


def receive_worker(value: str, *, expected_parent: int | None = None) -> ScratchLease:
    data = json.loads(value)
    keys = {'version', 'task', 'worker', 'task_identity', 'worker_identity', 'home', 'native', 'parent'}
    if (not isinstance(data, dict) or set(data) != keys
            or type(data['version']) is not int or data['version'] != 1
            or type(data['parent']) is not int
            or any(type(data[key]) is not str for key in ('task', 'worker', 'home', 'native'))):
        raise ValueError('invalid runner scratch receipt')
    for key in ('task_identity', 'worker_identity'):
        parts = data[key]
        if not isinstance(parts, list) or len(parts) != 3 or any(type(v) is not int or v < 0 for v in parts):
            raise ValueError('invalid runner scratch identity tuple')
    parent = os.getppid() if expected_parent is None else expected_parent
    if (data['parent'] != parent or data['home'] != os.path.expanduser('~')
            or data['native'] != str(native_root())):
        raise ValueError('runner scratch receipt does not match parent/HOME/native identity')
    root = Path(validate_scratch_root(data['task']))
    child = Path(data['worker'])
    if child.parent != root or child != child.resolve() or not child.name.startswith('r-'):
        raise ValueError('invalid runner scratch worker')
    lease = ScratchLease(root, child, tuple(data['task_identity']), tuple(data['worker_identity']), data['home'])
    lease.verify()
    return lease


def reject_location_overrides(arguments: list[str]) -> None:
    for i, token in enumerate(arguments):
        if (token == '--basetemp' or token.startswith('--basetemp=')
                or token.startswith('--runner-owned-scratch')):
            raise ValueError('--scratch-root owns internal pytest scratch arguments')
        ini = None
        if token in ('-o', '--override-ini') and i + 1 < len(arguments):
            ini = arguments[i + 1]
        elif token.startswith('--override-ini='):
            ini = token.split('=', 1)[1]
        elif token.startswith('-o') and len(token) > 2:
            ini = token[2:].lstrip('=')
        if ini and ini.split('=', 1)[0].strip() == 'cache_dir':
            raise ValueError('--scratch-root cannot be combined with a cache_dir override')


if __name__ == '__main__':
    try:
        if len(sys.argv) < 2:
            raise ValueError('--scratch-root requires a directory')
        root = validate_scratch_root(sys.argv[1], new=True)
        reject_location_overrides(sys.argv[2:])
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'error: --scratch-root: {exc}', file=sys.stderr)
        raise SystemExit(2)
    print(root)
