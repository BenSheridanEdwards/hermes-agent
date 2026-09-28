"""Durable admission state transitions and storage budgets."""
import sqlite3

import pytest

from gateway.acp_admission import AdmissionStore
from gateway.acp_delivery import DeliveryJournal


def test_terminal_admission_cannot_be_downgraded_by_late_acceptance(tmp_path):
    store = AdmissionStore(tmp_path)
    receipt = store.claim("session", "trigger", [{"type": "text", "text": "work"}])
    store.update(dict(receipt), status="completed")
    returned = store.update(receipt, status="in_progress")
    assert returned["status"] == "completed"
    assert store.get("session", "trigger")["status"] == "completed"


def test_retention_byte_budget_reports_gap_and_keeps_monotonic_ids(tmp_path, monkeypatch):
    from gateway import acp_delivery
    monkeypatch.setattr(acp_delivery, "MAX_BYTES", 1000)
    journal = DeliveryJournal(tmp_path)
    ids = [journal.append("sid", {"text": "x" * 500}) for _ in range(5)]
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT sum(length(CAST(message AS BLOB))) FROM deliveries").fetchone()[0] <= 1000
    with pytest.raises(ValueError, match="replay_gap"):
        journal.read("sid")
    rows, more = journal.read("sid", after=ids[-2])
    assert [i for i, _ in rows] == ids[-1:]
    assert not more


def test_admission_capacity_rejects_new_without_forgetting_old(tmp_path, monkeypatch):
    from gateway import acp_admission
    monkeypatch.setattr(acp_admission, "MAX_ADMISSIONS", 1)
    store = AdmissionStore(tmp_path)
    old = store.claim("sid", "one", [])
    with pytest.raises(ValueError, match="admission_capacity"):
        store.claim("sid", "two", [])
    assert AdmissionStore(tmp_path).get("sid", "one")["turnId"] == old["turnId"]
