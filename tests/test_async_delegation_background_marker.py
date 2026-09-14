"""The background-work marker advertises outstanding delegation work to a
supervising harness (buzz-acp) so it never tears the engine down mid-task and
wakes it to drain finished results. Marker present = work outstanding; absent =
nothing to protect. It is derived from the durable ledger at every transition."""

import json
import time

import pytest

import tools.async_delegation as ad


@pytest.fixture()
def isolated_ledger(tmp_path, monkeypatch):
    # The marker lives beside the ledger, so redirecting the ledger redirects it.
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    return tmp_path / ad._BACKGROUND_WORK_MARKER


def test_marker_tracks_dispatch_completion_and_delivery(isolated_ledger):
    marker = isolated_ledger
    assert not marker.exists()

    # Dispatch → a worker is running → the harness must keep the engine alive.
    ad._persist_dispatch({"delegation_id": "d1", "dispatched_at": time.time(), "session_key": "sess-origin"})
    assert marker.exists(), "a running delegation must set the marker"
    assert json.loads(marker.read_text())["pending"] == [], "a still-running delegation is not yet deliverable"

    # Finished but not yet delivered → still outstanding (the result needs a turn),
    # and the marker names the session it came from so the harness can deliver it
    # in that thread.
    ad._persist_completion({"delegation_id": "d1", "status": "completed"}, {"summary": "done"})
    assert marker.exists(), "an undelivered completion must keep the marker"
    assert json.loads(marker.read_text())["pending"] == [
        {"delegation_id": "d1", "origin_session": "sess-origin"}
    ], "a finished result must advertise its originating session"

    # Delivered → nothing left to protect → the harness may sleep the pool again.
    assert ad.mark_completion_delivered("d1") is True
    assert not marker.exists(), "a delivered completion must clear the marker"


def test_refresh_clears_a_stale_marker_when_ledger_is_empty(isolated_ledger):
    marker = isolated_ledger
    marker.write_text("999 1\n")
    ad.refresh_background_work_marker()
    assert not marker.exists(), "no outstanding rows → marker removed"
