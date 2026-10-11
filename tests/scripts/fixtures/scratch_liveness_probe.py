"""Inert profile-shaped F5 behavior probe, shared by collection and test phase.

This is regression source, not an execution receipt. No HOME or os/tempfile
probe substitution; all concrete mutation targets belong to a fresh model.
"""
from contextlib import contextmanager
import logging
import os
from pathlib import Path

import pytest

from scripts.runner_scratch import claim_scratch_root
from tests.home_io_guard import HomeIOGuard


@contextmanager
def isolated_logging_state():
    """Pause prior resources; own a distinct real queue until it is drained.

    Test-side only, serial helper use. No prior file/queue handler is closed.
    The yielded finish callback can run before model patches unwind; teardown
    repeats it harmlessly, then restores/restarts the exact prior resources.
    """
    import hermes_logging as logs

    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    prior_level = root.level
    levels = {name: logging.getLogger(name).level for name in logs._NOISY_LOGGERS}
    names = ('_log_queue', '_queue_listener', '_queued_file_handlers',
             '_queue_atexit_registered', '_logging_initialized', '_fallback_warned')
    with logs._queue_state_lock:
        prior = {name: getattr(logs, name) for name in names}
        if prior['_queue_listener'] is not None:
            # Drain and pause before model env/resolver/guard patches exist.
            prior['_queue_listener'].stop()
        for handler in prior_handlers:
            root.removeHandler(handler)
        logs._log_queue = None
        logs._queue_listener = None
        logs._queued_file_handlers = []
        # This scope owns synchronous teardown, not a process-exit callback.
        logs._queue_atexit_registered = True
        logs._logging_initialized = False

    def finish():
        with logs._queue_state_lock:
            if logs._queue_listener is not None:
                logs._queue_listener.stop()  # join drains pending real emissions
                logs._queue_listener = None
            owned = list(logs._queued_file_handlers)
            if any(handler in prior['_queued_file_handlers'] for handler in owned):
                raise AssertionError('prior logging resources entered model ownership')
            for handler in owned:
                handler.close()
            logs._queued_file_handlers.clear()
            logs._log_queue = None
            for handler in list(root.handlers):
                if handler in prior_handlers or not getattr(handler, '_hermes_queue', False):
                    raise AssertionError('unowned root handler in model logging scope')
                root.removeHandler(handler)
                handler.close()

    try:
        yield finish
    finally:
        try:
            finish()
        finally:
            with logs._queue_state_lock:
                for name, value in prior.items():
                    setattr(logs, name, value)
                for handler in prior_handlers:
                    root.addHandler(handler)
                root.setLevel(prior_level)
                for name, level in levels.items():
                    logging.getLogger(name).setLevel(level)
                if prior['_queue_listener'] is not None:
                    prior['_queue_listener'].start()


def exercise_liveness(model: Path, *, guarded: bool) -> dict:
    import hermes_constants as constants
    import hermes_logging
    from hermes_state_guard import _real_platform_state_root
    from tests.scratch_isolation import bounded_profile_resolver

    real_home = os.environ['HOME']
    real_native = _real_platform_state_root()
    native = model / '.hermes'
    parent = native / 'profiles' / 'owner' / 'cache' / 'scratch'
    parent.mkdir(parents=True, mode=0o700)
    lease = claim_scratch_root(str(parent / 'task')).create_worker()
    home = lease.worker / 'isolated-home'
    home.mkdir()
    # The containing profile is deliberately tombstoned. Isolated test I/O
    # must neither depend on nor consult it, even before the function guard.
    external = native / 'profiles' / '.deleted' / 'owner'
    external.parent.mkdir()
    external.write_text('model tombstone', encoding='utf-8')
    external_calls = []
    tombstones = []
    probes = []
    original_tombstone = constants.profile_tombstone_path
    original_profiles_root = constants._is_hermes_profiles_root

    def observe_tombstone(profile):
        path = original_tombstone(profile)
        tombstones.append(str(path))
        if not path.is_relative_to(lease.task):
            external_calls.append(str(path))
            raise AssertionError('external model tombstone lookup')
        return path

    def observe_profiles_root(path):
        probes.append(str(path))
        assert path.is_relative_to(lease.task), 'external profile-root lookup'
        return original_profiles_root(path)

    root_logger = logging.getLogger()
    with isolated_logging_state() as finish, pytest.MonkeyPatch.context() as patcher:
        try:
            patcher.setenv('HERMES_HOME', str(home))
            patcher.setattr(constants, 'named_profile_home', bounded_profile_resolver(
                lease, constants.named_profile_home))
            patcher.setattr(constants, 'profile_tombstone_path', observe_tombstone)
            patcher.setattr(constants, '_is_hermes_profiles_root', observe_profiles_root)
            if guarded:
                HomeIOGuard(lambda: [native], scratch=lease).install(patcher)
            created = constants.mkdir_under_hermes_home(home / 'created' / 'nested')
            assert created.is_dir()
            assert constants.named_profile_home(home / 'logs') is None
            assert constants.get_default_hermes_root() == home
            log_dir = hermes_logging.setup_logging(hermes_home=home, force=True)
            root_logger.warning('inert liveness success')
            hermes_logging.flush_log_queue()
            assert hermes_logging._queue_listener is not None
            owned_thread = hermes_logging._queue_listener._thread
            assert owned_thread is not None
            log_path = log_dir / 'agent.log'
            assert 'inert liveness success' in log_path.read_text(encoding='utf-8')

            # A real profile-shaped model INSIDE the task keeps production
            # recognition and missing/deleted refusal, not a blanket None.
            inner_root = lease.worker / 'inner' / '.hermes'
            profile = inner_root / 'profiles' / 'worker'
            profile.mkdir(parents=True)
            assert constants.named_profile_home(profile / 'logs') == profile
            assert constants.mkdir_under_hermes_home(profile / 'logs').is_dir()
            constants.mark_named_profile_deleted(profile)
            destination = lease.worker / 'ordinary-destination'
            destination.mkdir()
            (destination / 'canary').write_bytes(b'unchanged destination')
            alias = profile / 'contained-alias'
            alias.symlink_to(destination, target_is_directory=True)
            # Physical containment must not erase the lexical tombstoned owner.
            with pytest.raises(FileNotFoundError, match='Named profile home'):
                constants.mkdir_under_hermes_home(alias / 'must-not-exist')
            with pytest.raises(FileNotFoundError, match='Named profile home'):
                hermes_logging.setup_logging(hermes_home=alias, force=True)
            assert list(destination.iterdir()) == [destination / 'canary']
            assert (destination / 'canary').read_bytes() == b'unchanged destination'
            with pytest.raises(FileNotFoundError, match='Named profile home'):
                constants.mkdir_under_hermes_home(profile / 'after-delete')
            assert not (profile / 'after-delete').exists()
            constants.clear_named_profile_deleted(profile)
            assert constants.mkdir_under_hermes_home(profile / 'after-clear').is_dir()
            assert constants.mkdir_under_hermes_home(alias / 'after-clear').is_dir()
            missing = inner_root / 'profiles' / 'missing'
            with pytest.raises(FileNotFoundError, match='Named profile home'):
                constants.mkdir_under_hermes_home(missing / 'logs')
            assert not missing.exists()
            assert tombstones and probes
            assert external_calls == []
            assert os.environ['HOME'] == real_home
            assert _real_platform_state_root() == real_native
        finally:
            # Join/drain/close model resources BEFORE resolver/env/guard patches
            # unwind. The outer owner then restores the paused prior resources.
            finish()
    assert not owned_thread.is_alive()
    assert external.read_text(encoding='utf-8') == 'model tombstone'
    return {'log_written': True, 'mkdir_succeeded': True,
            'external_tombstones': external_calls, 'tombstone_paths': tombstones,
            'profile_root_paths': probes, 'deleted_profile_refused': True,
            'missing_profile_refused': True, 'guarded': guarded,
            'contained_alias_refused': True, 'owned_listener_stopped': True}
