"""Kanban refusal against disposable models, with the outer I/O guard intact."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc


def test_connect_succeeds_under_test_home(tmp_path, monkeypatch):
    home = tmp_path / 'hermes_home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    conn = kbc.connect()
    try:
        assert kanban_db.kanban_db_path().is_relative_to(home)
        conn.execute('CREATE TABLE compatibility_canary(value TEXT)')
        conn.execute("INSERT INTO compatibility_canary VALUES ('owned')")
        conn.commit()
        rows = conn.execute('SELECT value FROM compatibility_canary').fetchall()
        assert len(rows) == 1
        assert isinstance(rows[0], sqlite3.Row)
        assert rows[0]['value'] == 'owned'
    finally:
        conn.close()


@pytest.fixture
def kanban_model(tmp_path, monkeypatch):
    from tests import conftest

    root = tmp_path / 'protected-kanban'
    root.mkdir()
    db = root / 'kanban.db'
    conn = sqlite3.connect(db)
    try:
        conn.execute('CREATE TABLE witness(value TEXT)')
        conn.execute("INSERT INTO witness VALUES ('unchanged')")
        conn.commit()
    finally:
        conn.close()
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    calls = []
    original = sqlite3.connect  # still the real outer guarded connect

    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    with monkeypatch.context() as model:
        # Only Kanban's model changes. The outer native/sibling deny roots and
        # task lease are never removed; this model is legitimately owned there.
        model.setattr(conftest, '_REAL_KANBAN_ROOT', root)
        model.setattr(sqlite3, 'connect', observed)
        yield root, db, calls
        assert not calls, 'refused kbc.connect must not reach SQLite'
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    conn = sqlite3.connect(db)
    try:
        assert conn.execute('SELECT value FROM witness').fetchall() == [('unchanged',)]
    finally:
        conn.close()
    assert sorted(p.name for p in root.iterdir()) == sorted(before)


def test_connect_raises_when_kanban_home_is_real_root(kanban_model, monkeypatch):
    root, db, _ = kanban_model
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: root)
    monkeypatch.setattr(kanban_db, 'kanban_db_path', lambda board=None: db)
    with pytest.raises(RuntimeError, match='kanban_write_guard'):
        kbc.connect()


def test_connect_raises_for_explicit_db_path_under_real_root(kanban_model):
    _, db, _ = kanban_model
    with pytest.raises(RuntimeError, match='kanban_write_guard'):
        kbc.connect(db)


def test_outer_refusal_is_distinct_from_kanban_refusal(kanban_model, monkeypatch):
    from tests import conftest

    root, db, _ = kanban_model
    with monkeypatch.context() as model:
        model.setattr(conftest, '_REAL_HERMES_ROOT_CANDIDATES',
                      [*conftest._REAL_HERMES_ROOT_CANDIDATES, root])
        with pytest.raises(AssertionError, match='REAL hermes home'):
            kbc.connect(db)
