"""Test-only receipt consumption; the public interface stays --scratch-root.

No inherited environment value grants an exception. Only the canonical runner's
explicit child argument is consumed, with parent/HOME/directory identity checks.
"""
import os
from pathlib import Path
import sys

from scripts.runner_scratch import receive_worker


def read_lease():
    prefix = '--runner-owned-scratch='
    if '--runner-owned-scratch' in sys.argv[1:]:
        raise ValueError('runner scratch receipt must use its canonical joined form')
    receipts = [arg[len(prefix):] for arg in sys.argv[1:] if arg.startswith(prefix)]
    if not receipts:
        return None
    if len(receipts) != 1:
        raise ValueError('duplicate runner scratch receipt')
    lease = receive_worker(receipts[0])
    for key in ('TMPDIR', 'TMP', 'TEMP', 'PYTEST_DEBUG_TEMPROOT'):
        if os.environ.get(key) != str(lease.worker):
            raise ValueError(f'runner scratch receipt disagrees with {key}')
    return lease


LEASE = read_lease()


def retain_temp(path: Path) -> bool:
    return LEASE is not None and LEASE.contains(path)


def bounded_profile_resolver(lease, original):
    """Keep a test home from inheriting the enclosing operator's profile.

    Only a validated task descendant gets a bounded ancestor walk. Inner
    profile models keep the production recognition/deletion rules; every
    other path delegates unchanged. This changes test resolution, not file
    permissions: the existing native/profile deny roots still apply.
    """
    if lease is None:
        return original

    def named_profile_home(path):
        import hermes_constants as constants

        current = Path(path)
        current = current if current.is_absolute() else Path.cwd() / current
        if not current.is_relative_to(lease.task):
            return original(path)
        lease.verify()
        if not lease.contains(current):
            raise ValueError('invalid owned scratch profile path')
        # contains() validates the followed target separately. Classification
        # must retain the lexical profile owner: P/alias -> ordinary in-task D
        # does not waive P's tombstone in production mkdir/logging semantics.
        for candidate in (current, *current.parents):
            if candidate == lease.task:
                return None
            if candidate.name == '.hermes':
                return None
            if candidate.parent.name != 'profiles' or candidate.name.startswith('.'):
                continue
            profiles = candidate.parent
            # Recognition may read parent markers or .deleted. Neither may
            # escape through the task boundary or an in-task symlink.
            if not profiles.parent.is_relative_to(lease.task):
                return None
            probes = [profiles / constants._DELETED_PROFILES_DIR]
            probes.extend(profiles.parent / marker for marker in constants._HERMES_HOME_MARKERS)
            if not all(lease.contains(probe) for probe in probes):
                raise ValueError('invalid owned scratch profile metadata path')
            if constants._is_hermes_profiles_root(profiles):
                tombstone = profiles / constants._DELETED_PROFILES_DIR / candidate.name
                if not lease.contains(tombstone):
                    raise ValueError('invalid owned scratch profile tombstone path')
                return candidate
        return None

    return named_profile_home
