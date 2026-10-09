"""The background-work marker advertises outstanding delegation work to a
supervising harness (buzz-acp) so it never tears the engine down mid-task and
wakes it to drain finished results. Marker present = work outstanding; absent =
nothing to protect. It is derived from the durable ledger at every transition.
"""

import json
import time

import tools.async_delegation as ad


def test_marker_tracks_dispatch_completion_and_delivery(tmp_path, monkeypatch):
    marker = tmp_path / ad._BACKGROUND_WORK_MARKER
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    assert not marker.exists()

    ad._persist_dispatch({
        "delegation_id": "d1",
        "session_key": "sess-a",
        "dispatched_at": time.time(),
    })
    assert marker.exists(), "a running delegation must set the marker"

    payload = json.loads(marker.read_text())
    assert payload["pending"] == []
    assert payload["running"] == ["d1"]

    ad._persist_completion(
        {"delegation_id": "d1", "status": "completed", "session_key": "sess-a"},
        {"summary": "done"},
    )
    payload = json.loads(marker.read_text())
    assert payload["running"] == []
    assert payload["pending"] == [{"delegation_id": "d1", "origin_session": "sess-a"}]

    assert ad.mark_completion_delivered("d1") is True
    assert not marker.exists(), "a delivered completion must clear the marker"


def test_refresh_clears_a_stale_marker_when_ledger_is_empty(tmp_path, monkeypatch):
    marker = tmp_path / ad._BACKGROUND_WORK_MARKER
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    marker.write_text("999 1\n")
    ad.refresh_background_work_marker()
    assert not marker.exists(), "no outstanding rows → marker removed"
