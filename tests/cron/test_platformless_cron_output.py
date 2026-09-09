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
def gateway_mode_logging():
    """Real file logging in gateway mode, torn down after the test (see test_hermes_logging.py).

    The home directory comes from ``HERMES_HOME``, which the autouse ``_hermetic_environment``
    fixture already points at a per-test temporary directory."""
    home = Path(os.environ["HERMES_HOME"])
    root = logging.getLogger()
    pre_existing = list(root.handlers)
    prev_level = root.level
    prev_initialized = hermes_logging._logging_initialized
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
        hermes_logging._logging_initialized = prev_initialized


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


@pytest.mark.parametrize("deliver", ["Local", " local ", "LOCAL"])
def test_local_lane_is_matched_case_and_whitespace_insensitively(
    platformless_env, monkeypatch, caplog, deliver
):
    """``local`` is a lane keyword, not a platform name, and ``all``/``bot-chat`` are already
    matched case-insensitively. Before this, ``Local`` resolved to no target, reported a spurious
    ``no delivery target resolved for deliver=Local`` and (once unresolved lanes started logging)
    dumped the job body into agent.log and errors.log on every run."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("typo lane body"))

    with caplog.at_level(logging.INFO, logger="cron"):
        s.run_one_job(
            {"id": "j-typo", "name": "typo", "deliver": deliver}, adapters={}, loop=None,
        )

    msgs = _messages(caplog)
    assert not any("output not delivered" in m for m in msgs)
    assert not any("no delivery target resolved" in m for m in msgs)
    _args, kw = platformless_env["marked"][0]
    assert kw["delivery_error"] is None


def test_a_delivered_co_target_suppresses_the_body_for_a_failed_one(
    platformless_env, monkeypatch, caplog
):
    """The log gate is per job, not per target. A bot-chat receipt the live owner may already have
    consumed counts as delivered, so a platform target failing alongside it does not re-dump the
    body: something reached a human. The failure is still reported in the returned error."""
    monkeypatch.setattr(sched_delivery, "_deliver_to_bot_chat", _bot_chat_receipt("ambiguous"))
    monkeypatch.setattr(sched_delivery, "_record_delivery_verification", lambda *_a, **_kw: None)

    with caplog.at_level(logging.INFO, logger="cron"):
        error = sched_delivery._deliver_result(
            {"id": "j-cotarget", "name": "co", "deliver": "bot-chat,telegram:123"},
            "co-target body", adapters={}, loop=None,
        )

    assert error is not None
    assert "ambiguous" in error and "telegram" in error
    assert not any("output not delivered" in m for m in _messages(caplog))


def test_component_cron_filter_shows_the_header_but_drops_the_body(gateway_mode_logging):
    """Why the docs point operators at plain ``hermes logs`` and not ``--component cron``.

    ``hermes logs`` filters line by line and ``_line_matches_component`` needs a logger name on
    the line. This record is multi line: only the header carries the name, so a component filter
    prints the announcement and hides the output it announces. The docs used to recommend exactly
    that command. Run against real formatted lines so a format change cannot make this vacuous."""
    from hermes_cli import logs as cli_logs
    from hermes_logging import COMPONENT_PREFIXES

    sched_delivery._log_undelivered_output(
        {"id": "j-doc"}, "nightly brief\nall quiet on the fleet",
        ["platform 'telegram' not configured/enabled"])
    hermes_logging.flush_log_queue()

    lines = (gateway_mode_logging / "agent.log").read_text().splitlines()
    header = next(l for l in lines if "output not delivered to any target" in l)
    body = next(l for l in lines if l.strip() == "all quiet on the fleet")

    prefixes = COMPONENT_PREFIXES["cron"]
    assert cli_logs._line_matches_component(header, prefixes) is True
    assert cli_logs._line_matches_component(body, prefixes) is False
    # Unfiltered and level-filtered reads keep both: lines without a level pass the level filter.
    assert cli_logs._matches_filters(body) is True
    assert cli_logs._matches_filters(body, min_level="INFO") is True


def _failing_run_job(error):
    def _fake(job, **_kw):
        return (False, "raw output", "", error)
    return _fake


def _recorded_outcome(state):
    """The ``delivery_outcome`` handed to ``finish_execution``: what the executions ledger,
    ``hermes cron list`` and the agent-facing run summary all read back."""
    assert len(state["finished"]) == 1
    _args, kw = state["finished"][0]
    return kw.get("delivery_outcome")


def test_failing_run_on_the_origin_lane_logs_the_body_at_warning(
    platformless_env, monkeypatch, caplog
):
    """The level follows the RUN, not just the delivery. The origin-less ``origin`` lane is never
    a delivery failure, so keying the level on the delivery outcome alone sent a failure summary
    to INFO: a job failing every tick on a platform-less, home-channel-less gateway left
    ``errors.log`` completely empty, which is the mirror image of the bug the INFO lane fixed."""
    monkeypatch.setattr(s, "run_job", _failing_run_job("boom: the script exited 1"))

    with caplog.at_level(logging.INFO, logger="cron"):
        s.run_one_job(
            {"id": "j-failorigin", "name": "failing", "deliver": "origin"},
            adapters={}, loop=None,
        )

    records = _undelivered_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "boom: the script exited 1" in records[0].getMessage()


@pytest.mark.parametrize("raw,expected", [
    (" Local ", "local"),
    ("ORIGIN", "origin"),
    ("All", "all"),
    ("BOT-CHAT", "bot-chat"),
    (["Origin"], "origin"),
    (" local , ALL ", "local,all"),
    ("Telegram:ABC", "Telegram:ABC"),
    ("bot-chat:Ops", "bot-chat:Ops"),
    ("", "local"),
    (None, "local"),
    ("   ", "   "),
])
def test_normalize_deliver_value_canonicalizes_lane_keywords(raw, expected):
    """The lane is folded once, at the value every consumer reads. Lane keywords lowercase and
    lose their padding; a ``platform:chat_id`` or ``bot-chat:<profile>`` token keeps its case
    (chat ids are opaque, profile names belong to the profile layer); a whitespace-only value is
    left alone so it still surfaces as an unresolved target instead of a silent ``local``."""
    assert sched_delivery._normalize_deliver_value(raw) == expected


@pytest.mark.parametrize("deliver", ["Local", " local ", "LOCAL", "\tlocal\n", ["Local"]])
def test_mis_cased_local_lane_is_recorded_suppressed_not_delivered(
    platformless_env, monkeypatch, deliver
):
    """The lane fix has to reach the consumers that CLASSIFY the run, not just the ones that
    resolve targets. Folding the lane only inside the delivery module made ``deliver: Local``
    resolve to no target (correct, silent) while ``_classify_delivery_outcome`` still saw a
    non-``local`` string and recorded the run ``delivered`` with nothing sent."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("recorded lane body"))

    s.run_one_job(
        {"id": "j-recorded", "name": "recorded", "deliver": deliver}, adapters={}, loop=None,
    )

    assert _recorded_outcome(platformless_env) == "suppressed"


@pytest.mark.parametrize("deliver", ["Origin", " ORIGIN "])
def test_mis_cased_origin_lane_is_recorded_not_configured(
    platformless_env, monkeypatch, deliver
):
    """Same for the origin lane: an origin-less ``origin`` run is ``not_configured``. Compared
    raw, ``Origin`` missed the unresolved-origin check and was recorded ``delivered``."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("origin recorded body"))

    s.run_one_job(
        {"id": "j-recorded-origin", "name": "recorded", "deliver": deliver},
        adapters={}, loop=None,
    )

    assert _recorded_outcome(platformless_env) == "not_configured"


def test_crash_failure_on_a_mis_cased_origin_lane_is_recorded_not_configured(
    platformless_env, caplog,
):
    """The crash path classifies the lane on its own (a run that raised out of ``run_job`` never
    reaches the normal finalizer), so it needs the same fold."""
    with caplog.at_level(logging.INFO, logger="cron"):
        delivery_error, outcome = s._deliver_crash_failure(
            {"id": "j-crash", "name": "crash", "deliver": " ORIGIN "},
            "the runner raised", adapters={}, loop=None,
        )

    assert delivery_error is None
    assert outcome == "not_configured"


def test_classify_delivery_outcome_folds_the_lane_keyword():
    """The classifier does not trust its caller to have normalized: it is the last gate before a
    run is written down as ``delivered``, and the note the agent relays to the user follows it."""
    kw = dict(delivery_error=None, should_deliver=True, unresolved_origin=False,
              incident_acked=False, success=True)
    assert s._classify_delivery_outcome(normalized_deliver="local", **kw) == "suppressed"
    assert s._classify_delivery_outcome(normalized_deliver="Local", **kw) == "suppressed"
    assert s._classify_delivery_outcome(normalized_deliver=" local ", **kw) == "suppressed"
    # A real target is still a delivery.
    assert s._classify_delivery_outcome(normalized_deliver="telegram:123", **kw) == "delivered"


def test_origin_token_is_resolved_case_and_whitespace_insensitively():
    """The per-token resolver is also called directly (it is exported as
    ``_resolve_delivery_target``), so it folds the lane itself: `` ORIGIN `` reaches the origin
    it names instead of being parsed as a platform called "ORIGIN" and resolving to nothing."""
    job = {"id": "j-origin-token", "origin": {"platform": "telegram", "chat_id": 42}}

    target = sched_delivery._resolve_single_delivery_target(job, " ORIGIN ")

    assert target is not None
    assert target["platform"] == "telegram"
    assert str(target["chat_id"]) == "42"
    assert target["_resolved_from"] == "origin"


def test_target_resolution_does_not_assume_an_already_folded_lane(monkeypatch):
    """The ``local`` opt-out in ``_resolve_delivery_targets`` short-circuits before any token is
    resolved, and it folds the lane itself rather than relying on ``_normalize_deliver_value``
    having run. Pinned with the normalizer stubbed out, which is the only way to hand this layer
    a raw lane now that the value is canonical at the source."""
    monkeypatch.setattr(sched_delivery, "_normalize_deliver_value", lambda value: value)

    def _must_not_resolve(*_a, **_kw):
        raise AssertionError("the local lane must not reach target resolution")

    monkeypatch.setattr(sched_delivery, "_resolve_single_delivery_target", _must_not_resolve)

    assert sched_delivery._resolve_delivery_targets({"id": "j-raw", "deliver": " Local "}) == []


def test_unresolved_outcome_does_not_assume_an_already_folded_lane(monkeypatch):
    """Same contract one layer down, and the site that decides whether the body is logged at all:
    a raw `` Local `` must read as the local lane (no error, nothing logged), not as a platform
    named "Local" that resolves to nothing and dumps the job body on every run."""
    monkeypatch.setattr(sched_delivery, "_normalize_deliver_value", lambda value: value)

    outcome = sched_delivery._unresolved_delivery_outcome(
        {"id": "j-raw-outcome", "deliver": " Local "}, False)

    assert outcome == (None, None)


def test_the_unresolved_reason_carries_the_folded_lane(platformless_env, monkeypatch, caplog):
    """The reason string is written into ``last_delivery_error`` and into the log line, so it
    shows the canonical lane rather than replaying the padding it was typed with."""
    monkeypatch.setattr(s, "run_job", _succeeding_run_job("padded lane body"))

    with caplog.at_level(logging.INFO, logger="cron"):
        s.run_one_job(
            {"id": "j-padded", "name": "padded", "deliver": " all "}, adapters={}, loop=None,
        )

    reasons = [m for m in _messages(caplog) if "no delivery target resolved" in m]
    assert reasons and all("deliver=all" in m for m in reasons)
    assert not any("deliver= all" in m for m in reasons)
    _args, kw = platformless_env["marked"][0]
    assert kw["delivery_error"] == "no delivery target resolved for deliver=all"
