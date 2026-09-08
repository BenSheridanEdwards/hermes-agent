"""Cron output on a gateway with no messaging platform.

A gateway may run with zero enabled platforms (scheduled work only). A job whose ``deliver``
names a platform then has nothing to send through: the run is recorded as a delivery failure
as before, and the output is ALSO written to the log so the result stays visible. That covers
both shapes of "nothing took it": a target that resolved and refused (``telegram:123``), and a
lane that resolves to no target at all on a platform-less gateway (``all``, and ``origin`` on a
CLI-created job with no captured origin).

The line comes from the ``cron.scheduler`` logger, so it lands in ``agent.log`` and NOT in
``gateway.log`` (pinned by ``test_undelivered_output_lands_in_agent_log_not_gateway_log``). It
reaches ``errors.log`` only when it is a real delivery failure: that lane logs at WARNING, while
the origin-less ``origin`` lane, which is recorded as a successful run, logs at INFO so routine
output does not rotate the error history away.

``local`` never wanted a target and stays silent (in any case or spacing); a ``bot-chat`` receipt
the live owner may have consumed is not treated as undelivered, and the gate is per job, so a
delivered co-target suppresses the body for a failed one.
"""

import logging
import os
from pathlib import Path

import pytest

import cron.scheduler as s
import hermes_logging
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


def test_deliver_all_output_is_logged_when_no_target_resolves(
    platformless_env, monkeypatch, caplog
):
    """``deliver: all`` on a platform-less gateway resolves to NO target, so it never reaches
    the per-target loop. The run is still recorded as a delivery failure and the output is
    logged: this is the gap the change set out to close."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("fleet report: 3 jobs, 0 failures"))

    with caplog.at_level(logging.INFO, logger="cron"):
        ok = s.run_one_job(
            {"id": "j-all", "name": "fleet", "deliver": "all"}, adapters={}, loop=None,
        )

    assert ok is True
    logged = [m for m in _messages(caplog) if "output not delivered to any target" in m]
    assert len(logged) == 1
    assert "j-all" in logged[0]
    assert "no delivery target resolved for deliver=all" in logged[0]
    assert "fleet report: 3 jobs, 0 failures" in logged[0]
    # Bookkeeping unchanged: still a delivery failure with the same reason.
    _args, kw = platformless_env["marked"][0]
    assert kw["delivery_error"] == "no delivery target resolved for deliver=all"


def test_deliver_origin_without_an_origin_logs_output_without_reporting_an_error(
    platformless_env, monkeypatch, caplog
):
    """``deliver: origin`` on a CLI-created job has no origin to resolve. That stays a
    non-failure (upstream #43014: no spurious error every run), but the output is no longer
    invisible: the body is logged alongside the existing skip notice."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("weekly digest: nothing to report"))

    with caplog.at_level(logging.INFO, logger="cron"):
        ok = s.run_one_job(
            {"id": "j-origin", "name": "digest", "deliver": "origin"}, adapters={}, loop=None,
        )

    assert ok is True
    msgs = _messages(caplog)
    assert any("skipping delivery (output saved in last_output)" in m for m in msgs)
    logged = [m for m in msgs if "output not delivered to any target" in m]
    assert len(logged) == 1
    assert "deliver=origin but no origin or home channels" in logged[0]
    assert "weekly digest: nothing to report" in logged[0]
    # Still not an error: the job is not marked delivery_failed.
    _args, kw = platformless_env["marked"][0]
    assert kw["delivery_error"] is None


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


def _bot_chat_receipt(status):
    """Stub the Bot Chat lane: record a receipt of *status* and return its error string."""
    def _fake(job, _content, profile):
        target = f"bot-chat:{profile or '(own)'}"
        job.setdefault("_bot_chat_delivery_receipts", {})[target] = {
            "status": status, "delivery_id": "abc123",
        }
        return f"{target} {status} (receipt abc123): completion unverified"
    return _fake


@pytest.mark.parametrize("status,expect_logged", [("ambiguous", False), ("failed", True)])
def test_ambiguous_bot_chat_receipt_does_not_log_the_body(
    platformless_env, monkeypatch, caplog, status, expect_logged
):
    """An ``ambiguous`` receipt is the state the delivery code refuses to replay because the
    live owner may already have consumed the output. Calling that "not delivered to any target"
    and dumping the body overstates what is known, so it is not logged; a receipt that really
    failed still is. Either way the receipt bookkeeping (the returned error) is unchanged."""
    monkeypatch.setattr(sched_delivery, "_deliver_to_bot_chat", _bot_chat_receipt(status))
    monkeypatch.setattr(sched_delivery, "_record_delivery_verification", lambda *_a, **_kw: None)

    with caplog.at_level(logging.INFO, logger="cron"):
        error = sched_delivery._deliver_result(
            {"id": f"j-{status}", "name": "bot", "deliver": "bot-chat"},
            "standup notes for the owner", adapters={}, loop=None,
        )

    assert error is not None and status in error
    logged = [m for m in _messages(caplog) if "output not delivered to any target" in m]
    assert bool(logged) is expect_logged
    if expect_logged:
        assert "standup notes for the owner" in logged[0]


@pytest.fixture
def gateway_mode_logging(tmp_path):
    """Real file logging in gateway mode, torn down after the test (see test_hermes_logging.py)."""
    home = Path(os.environ["HERMES_HOME"])
    root = logging.getLogger()
    pre_existing = list(root.handlers)
    prev_level = root.level
    hermes_logging._logging_initialized = False
    hermes_logging._reset_queued_handlers()
    log_dir = hermes_logging.setup_logging(hermes_home=home, mode="gateway", force=True)
    try:
        yield log_dir
    finally:
        hermes_logging._reset_queued_handlers()
        for handler in list(root.handlers):
            if handler not in pre_existing:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(prev_level)
        hermes_logging._logging_initialized = False


def test_undelivered_output_lands_in_agent_log_not_gateway_log(gateway_mode_logging):
    """Where the line actually goes. It is emitted by the ``cron.scheduler`` logger, and the
    gateway.log handler carries a component filter for ``gateway.*``, so the output surfaces in
    agent.log and (as a WARNING) errors.log. The docs and docstrings name those files."""
    sched_delivery._log_undelivered_output(
        {"id": "j-route"}, "routed output body", ["telegram not configured/enabled"])
    hermes_logging.flush_log_queue()

    assert "routed output body" in (gateway_mode_logging / "agent.log").read_text()
    assert "routed output body" in (gateway_mode_logging / "errors.log").read_text()
    gateway_log = gateway_mode_logging / "gateway.log"
    assert not gateway_log.exists() or "routed output body" not in gateway_log.read_text()


def _undelivered_records(caplog):
    return [r for r in caplog.records if "output not delivered to any target" in r.getMessage()]


def test_failed_delivery_logs_the_body_at_warning(platformless_env, monkeypatch, caplog):
    """A real delivery failure belongs in ``errors.log``, so it logs at WARNING."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("failed-lane body"))

    with caplog.at_level(logging.INFO, logger="cron"):
        s.run_one_job(
            {"id": "j-warn", "name": "warn", "deliver": "all"}, adapters={}, loop=None,
        )

    records = _undelivered_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_origin_without_an_origin_logs_the_body_at_info(platformless_env, monkeypatch, caplog):
    """The origin-less ``deliver: origin`` lane is NOT a delivery failure: the run is recorded ok.
    ``deliver`` defaults to ``origin`` for agent- and blueprint-created jobs, and CLI/TUI sessions
    never capture an origin, so on a home-channel-less gateway this lane fires on every run. At
    WARNING the body would land in ``logs/errors.log`` each time and rotate the real error history
    away, so this lane logs at INFO: still in ``agent.log``, still under ``hermes logs``."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("origin-lane body"))

    with caplog.at_level(logging.INFO, logger="cron"):
        s.run_one_job(
            {"id": "j-info", "name": "info", "deliver": "origin"}, adapters={}, loop=None,
        )

    records = _undelivered_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "origin-lane body" in records[0].getMessage()
    # The lane is still not an error, which is exactly why it must not be a WARNING.
    _args, kw = platformless_env["marked"][0]
    assert kw["delivery_error"] is None


def test_origin_lane_body_stays_out_of_the_error_log(gateway_mode_logging):
    """The level pin, through the real file handlers: INFO reaches agent.log only."""
    sched_delivery._log_undelivered_output(
        {"id": "j-lvl"}, "info lane body", ["deliver=origin but no origin or home channels"],
        level=logging.INFO)
    hermes_logging.flush_log_queue()

    assert "info lane body" in (gateway_mode_logging / "agent.log").read_text()
    errors_log = gateway_mode_logging / "errors.log"
    assert not errors_log.exists() or "info lane body" not in errors_log.read_text()


def test_undelivered_output_survives_undecodable_job_output(gateway_mode_logging):
    """Job output is decoded with ``surrogateescape``, so a non-UTF-8 byte from a script arrives
    as a lone surrogate. The rotating file handlers encode UTF-8 with no ``errors=``, so emitting
    one raises UnicodeEncodeError inside logging: the record is dropped whole and a traceback goes
    to stderr, losing the one line this change exists to write. Scrub before logging."""
    body = "report start \udcff\udce9 report end"

    sched_delivery._log_undelivered_output(
        {"id": "j-surrogate"}, body, ["platform 'telegram' not configured/enabled"])
    hermes_logging.flush_log_queue()

    written = (gateway_mode_logging / "agent.log").read_text()
    assert "report start" in written and "report end" in written
    assert "\udcff" not in written


def test_surrogates_in_the_error_reason_are_scrubbed_too(gateway_mode_logging):
    """The reason string is interpolated into the same record, so it needs the same treatment."""
    sched_delivery._log_undelivered_output(
        {"id": "j-surrogate-reason"}, "plain body", ["platform '\udcffbad' not configured/enabled"])
    hermes_logging.flush_log_queue()

    written = (gateway_mode_logging / "agent.log").read_text()
    assert "plain body" in written
    assert "not configured/enabled" in written


