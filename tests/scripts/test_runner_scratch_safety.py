"""Finite correction-4 safety regressions. Inert targets; execution pending."""
import logging
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from scripts import runner_scratch as scratch
from tests import home_io_guard
from tests.home_io_guard import HomeIOGuard


def _lease(parent):
    return scratch.claim_scratch_root(str(parent / 'owned-task')).create_worker()


@pytest.mark.platforms('posix')
def test_cleanup_reports_retained_worker(tmp_path, monkeypatch):
    from scripts import run_tests_parallel as runner

    lease = _lease(tmp_path)
    canary = lease.worker / 'retained'
    canary.write_text('inert retained cleanup evidence')
    # Models an incomplete removal, not a successful cleanup witness.
    monkeypatch.setattr(runner.shutil, 'rmtree', lambda *args, **kwargs: None)
    with pytest.raises(OSError, match='cleanup.*retained'):
        runner._cleanup_worker(str(lease.worker), lease)
    assert canary.read_text() == 'inert retained cleanup evidence'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('alias', ['parent', 'symlink', 'nested-deny'])
@pytest.mark.parametrize('operation', ['read', 'write', 'open', 'fd-read', 'fd-write'])
def test_trusted_prefix_cannot_reach_denied_model(tmp_path, monkeypatch, explicit, alias, operation):
    lease = _lease(tmp_path) if explicit else None
    native = tmp_path / 'native-model'
    trusted = native / 'checkout'
    trusted.mkdir(parents=True)
    denied = trusted / 'private' if alias == 'nested-deny' else native / 'denied'
    denied.mkdir()
    canary = denied / 'config.yaml'
    canary.write_bytes(b'unchanged')
    good = trusted / 'source.txt'
    good.write_bytes(b'legitimate source access')
    if alias == 'parent':
        relative = Path(os.path.relpath(canary, trusted))
    elif alias == 'symlink':
        (trusted / 'alias').symlink_to(denied, target_is_directory=True)
        relative = Path('alias/config.yaml')
    else:
        relative = Path('private/config.yaml')
    original_prefixes = home_io_guard._INTERPRETER_PREFIX_STRS
    with monkeypatch.context() as patcher:
        patcher.setattr(home_io_guard, '_INTERPRETER_PREFIX_STRS',
                        (*original_prefixes, os.path.normcase(str(trusted))))
        HomeIOGuard(lambda: [native, denied], scratch=lease).install(patcher)
        assert good.read_bytes() == b'legitimate source access'
        good.write_bytes(b'legitimate source write')
        with open(sys.executable, 'rb') as interpreter:
            assert interpreter.read(1)
        fd = os.open(trusted, os.O_RDONLY)
        try:
            with pytest.raises(AssertionError, match='REAL hermes home'):
                if operation == 'read':
                    (trusted / relative).read_bytes()
                elif operation == 'write':
                    (trusted / relative).write_bytes(b'bad')
                elif operation == 'open':
                    opened = os.open(trusted / relative, os.O_WRONLY | os.O_TRUNC)
                    os.close(opened)
                else:
                    flags = os.O_RDONLY if operation == 'fd-read' else os.O_WRONLY | os.O_TRUNC
                    opened = os.open(relative, flags, dir_fd=fd)
                    os.close(opened)
        finally:
            os.close(fd)
    assert canary.read_bytes() == b'unchanged'
    assert good.read_bytes() == b'legitimate source write'


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('spelling', ['absolute', 'localhost', 'relative', 'positional'])
def test_sqlite_uri_refusal_preserves_rows_and_sidecars(tmp_path, monkeypatch, spelling, record_property):
    lease = _lease(tmp_path)
    denied = tmp_path / 'denied'
    denied.mkdir()
    database = denied / 'inert database.db'
    connection = sqlite3.connect(database)
    try:
        connection.execute('create table entries (value text)')
        connection.execute("insert into entries values ('original')")
        connection.commit()
    finally:
        connection.close()
    before = {p.name: p.read_bytes() for p in denied.iterdir()}
    uri = database.as_uri() + '?mode=rw'
    if spelling == 'localhost':
        uri = uri.replace('file:///', 'file://localhost/', 1)
    elif spelling == 'relative':
        monkeypatch.chdir(tmp_path)
        uri = 'file:denied/inert%20database.db?mode=rw'
    with monkeypatch.context() as patcher:
        HomeIOGuard(lambda: [denied], scratch=lease).install(patcher)
        with pytest.raises(AssertionError, match='REAL hermes home'):
            connection = (sqlite3.connect(uri, 5.0, 0, 'DEFERRED', True, sqlite3.Connection, 128, True)
                          if spelling == 'positional' else sqlite3.connect(uri, uri=True))
            try:
                connection.execute("insert into entries values ('must not land')")
                connection.commit()
            finally:
                connection.close()
    assert {p.name: p.read_bytes() for p in denied.iterdir()} == before
    connection = sqlite3.connect(database)
    try:
        rows_after = connection.execute('select value from entries').fetchall()
        assert rows_after == [('original',)]
    finally:
        connection.close()
    assert {p.name for p in denied.iterdir()} == set(before)
    record_property('database_bytes_unchanged', True)
    record_property('rows_after', repr(rows_after))
    record_property('sidecar_inventory_before_after', sorted(before))


@pytest.mark.platforms('posix')
def test_sqlite_owned_and_memory_targets_still_work(tmp_path, monkeypatch):
    lease = _lease(tmp_path)
    with monkeypatch.context() as patcher:
        HomeIOGuard(lambda: [tmp_path], scratch=lease).install(patcher)
        targets = [(lease.worker / 'plain.db', False),
                   ((lease.worker / 'uri database.db').as_uri() + '?mode=rwc', True),
                   (':memory:', False), ('file::memory:?cache=shared', True),
                   ('file:inert-memory?mode=memory&cache=shared', True)]
        for target, uri in targets:
            connection = sqlite3.connect(target, uri=uri)
            try:
                connection.execute('create table entries (value)')
                connection.execute('insert into entries values (7)')
                assert connection.execute('select value from entries').fetchone() == (7,)
            finally:
                connection.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('uri', [
    'file://remote.example/inert.db?mode=rw', 'file:inert.db?vfs=unknown',
    'file:inert.db?mode=rw&mode=memory', 'file:bad%00name.db',
    'file:bad%xx.db', 'file:inert.db#fragment', 'file:inert.db?nolock=1',
])
def test_sqlite_unsupported_uri_fails_closed(tmp_path, monkeypatch, uri):
    lease = _lease(tmp_path)
    with monkeypatch.context() as patcher:
        HomeIOGuard(lambda: [tmp_path], scratch=lease).install(patcher)
        with pytest.raises(AssertionError, match='SQLite URI'):
            sqlite3.connect(uri, uri=True)


@pytest.mark.platforms('posix')
def test_liveness_helper_restores_prior_queue_and_next_home(tmp_path, monkeypatch, record_property):
    import hermes_logging as logs
    from tests.scripts.fixtures.scratch_liveness_probe import (
        exercise_liveness, isolated_logging_state,
    )

    prior_home = tmp_path / 'prior-home'
    prior_home.mkdir()
    # Outer scope owns only this test's resources; nested helper must preserve
    # their exact identities and usable file streams, not close/recreate them.
    with isolated_logging_state() as finish, monkeypatch.context() as patcher:
        try:
            patcher.setenv('HERMES_HOME', str(prior_home))
            logs.setup_logging(hermes_home=prior_home, force=True)
            queue = logs._log_queue
            listener = logs._queue_listener
            files = tuple(logs._queued_file_handlers)
            handlers = tuple(logging.getLogger().handlers)
            logging.getLogger().warning('prior queued before helper')
            receipt = exercise_liveness(tmp_path / 'nested-model', guarded=True)
            assert receipt['owned_listener_stopped']
            assert logs._log_queue is queue and logs._queue_listener is listener
            assert tuple(logs._queued_file_handlers) == files
            assert tuple(logging.getLogger().handlers) == handlers
            assert all(handler.stream is not None and not handler.stream.closed for handler in files)
            logging.getLogger().warning('prior queued after helper')
            logs.flush_log_queue()
            text = (prior_home / 'logs/agent.log').read_text()
            assert 'prior queued before helper' in text
            assert 'prior queued after helper' in text
            assert 'inert liveness success' not in text
            record_property('prior_session_log', text)
            record_property('prior_logging_identity_restored', True)
        finally:
            finish()

    # Fresh session follows a fully torn-down helper, as in collection.
    with isolated_logging_state() as finish:
        receipt = exercise_liveness(tmp_path / 'fresh-model', guarded=False)
        assert receipt['owned_listener_stopped']
        assert logs._log_queue is None and logs._queue_listener is None
        assert logs._queued_file_handlers == []
        with monkeypatch.context() as patcher:
            try:
                next_home = tmp_path / 'next-home'
                next_home.mkdir()
                patcher.setenv('HERMES_HOME', str(next_home))
                logs.setup_logging(hermes_home=next_home, force=True)
                logging.getLogger().warning('next session after helper')
                logs.flush_log_queue()
                assert 'next session after helper' in (next_home / 'logs/agent.log').read_text()
                record_property('next_session_log', (next_home / 'logs/agent.log').read_text())
            finally:
                finish()
