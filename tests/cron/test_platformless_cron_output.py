"""Cron output on a gateway with no messaging platform.

A gateway may run with zero enabled platforms (scheduled work only). A job whose ``deliver``
names a platform then has nothing to send through: the run is recorded as a delivery failure
as before, and the output is ALSO written to the log so the result is visible to whoever tails
gateway.log. ``local`` and ``bot-chat`` targets never needed a platform and stay silent.
"""

import logging

import pytest

import cron.scheduler as s
from cron import scheduler_delivery as sched_delivery


@pytest.fixture
def platformless_env(monkeypatch, tmp_path):
    """Real delivery path, no enabled platform, bookkeeping primitives recorded."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    enabled: false\n    token: '123:abc'\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_cli.plugins as hp
    monkeypatch.setattr(hp, "discover_plugins", lambda *a, **k: None)

    state = {"marked": [], "saved": [], "finished": []}
    monkeypatch.setattr(s, "create_execution", lambda *_a, **_kw: {"id": "exec-noplat"})
    monkeypatch.setattr(s, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(s, "mark_execution_running", lambda _execution_id: {})
    monkeypatch.setattr(
        s, "save_job_output",
        lambda jid, out: state["saved"].append(jid) or f"/tmp/{jid}.txt",
    )
    monkeypatch.setattr(
        s, "mark_job_run",
        lambda *a, **kw: state["marked"].append((a, kw)) or True,
    )
    monkeypatch.setattr(
        s, "finish_execution",
        lambda *a, **kw: state["finished"].append((a, kw)),
    )
    monkeypatch.setattr(
        s, "_upsert_incident_for_failure", lambda *_a, **_kw: (False, None)
    )
    monkeypatch.setattr(s, "load_config", lambda: {})
    return state


def _succeeding_run_job(final):
    def _fake(job, **_kw):
        return (True, "raw output", final, None)
    return _fake


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_platform_bound_output_is_logged_when_no_platform_can_take_it(
    platformless_env, monkeypatch, caplog
):
    """A fired job addressed to a platform on a platform-less gateway: delivery is recorded
    as failed (unchanged) and the output itself lands in the log (new)."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("nightly brief: all quiet on the fleet"))

    with caplog.at_level(logging.INFO, logger="cron"):
        ok = s.run_one_job(
            {"id": "j-noplat", "name": "brief", "deliver": "telegram:123"},
            adapters={}, loop=None,
        )

    assert ok is True  # the job ran; delivery status is tracked separately
    msgs = _messages(caplog)
    assert any("not configured/enabled" in m for m in msgs)
    logged = [m for m in msgs if "output not delivered to any target" in m]
    assert len(logged) == 1
    assert "j-noplat" in logged[0]
    assert "nightly brief: all quiet on the fleet" in logged[0]
    assert "not configured/enabled" in logged[0]
    # Bookkeeping unchanged: the run is saved and marked with the delivery error.
    assert platformless_env["saved"] == ["j-noplat"]
    assert len(platformless_env["marked"]) == 1
    args, kw = platformless_env["marked"][0]
    assert args[0] == "j-noplat" and args[1] is True
    assert "not configured/enabled" in repr((args, kw))


def test_local_delivery_stays_silent_without_a_platform(platformless_env, monkeypatch, caplog):
    """``deliver: local`` never resolves a target, so nothing is undelivered and nothing is
    logged; the output file remains the record, exactly as before."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("kept local"))

    with caplog.at_level(logging.INFO, logger="cron"):
        ok = s.run_one_job(
            {"id": "j-local", "name": "quiet", "deliver": "local"}, adapters={}, loop=None,
        )

    assert ok is True
    assert not any("output not delivered" in m for m in _messages(caplog))
    args, _kw = platformless_env["marked"][0]
    assert args[0] == "j-local" and args[1] is True


def test_undelivered_output_log_is_bounded(caplog):
    """A long report must not flood the log: the line is capped and says where the rest is."""
    limit = sched_delivery._UNDELIVERED_OUTPUT_LOG_LIMIT
    big = "q" * (limit + 500)
    with caplog.at_level(logging.WARNING, logger="cron.scheduler_delivery"):
        sched_delivery._log_undelivered_output({"id": "j-big"}, big, ["platform 'telegram' down"])
    line = _messages(caplog)[-1]
    assert "truncated" in line and "last_output" in line
    assert line.count("q") == limit


def test_undelivered_output_log_skips_empty_content(caplog):
    with caplog.at_level(logging.WARNING, logger="cron.scheduler_delivery"):
        sched_delivery._log_undelivered_output({"id": "j-empty"}, "   \n", ["nope"])
    assert not any("output not delivered" in m for m in _messages(caplog))
