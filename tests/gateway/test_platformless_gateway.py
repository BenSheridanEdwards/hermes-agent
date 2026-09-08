"""A gateway with zero enabled messaging platforms is a supported mode (upstream #5196).

It starts, reports ``running``, and keeps the cron ticker and housekeeping thread alive so a
profile whose chat face lives elsewhere (a managed harness, a desktop app) still gets its
scheduled work. These tests pin that contract so a future startup gate cannot quietly turn
"no platforms" into an EX_CONFIG exit; the fatal path stays reserved for real conflicts
(``test_runner_startup_failures.py``).
"""

import logging
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _start_gateway_start_cron_and_housekeeping
from gateway.status import read_runtime_status


def _wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


async def _assert_running_without_platforms(runner, caplog):
    with caplog.at_level(logging.INFO):
        ok = await runner.start()
    assert ok is True
    assert runner.should_exit_cleanly is False
    assert runner.exit_code is None
    assert runner._running is True
    assert runner.adapters == {}
    assert read_runtime_status()["gateway_state"] == "running"
    assert any("No messaging platforms enabled" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_runner_starts_and_stays_running_with_no_platforms_configured(
    monkeypatch, tmp_path, caplog
):
    """No platforms section at all: the gateway starts and reports ``running``."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    try:
        await _assert_running_without_platforms(runner, caplog)
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_runner_starts_when_every_platform_is_disabled(monkeypatch, tmp_path, caplog):
    """The chat face switched off (platform kept in config, ``enabled: false``) is the
    same supported mode, not a configuration error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=False, token="***"),
            Platform.DISCORD: PlatformConfig(enabled=False),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    try:
        await _assert_running_without_platforms(runner, caplog)
        state = read_runtime_status()
        assert state["gateway_state"] != "startup_failed"
        assert state.get("exit_reason") is None
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_cron_ticker_and_housekeeping_run_with_no_platforms(monkeypatch, tmp_path, caplog):
    """The scheduled-work threads come up with an empty adapter map: the in-process cron
    ticker records its heartbeat and housekeeping ticks, neither needing a platform."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    assert await runner.start() is True
    cron_stop = cron_thread = housekeeping_thread = None
    try:
        with caplog.at_level(logging.INFO):
            cron_stop, cron_provider, cron_thread, housekeeping_thread = (
                _start_gateway_start_cron_and_housekeeping(runner))
            assert cron_provider.name == "builtin"
            assert cron_thread.is_alive()
            assert housekeeping_thread.is_alive()
            assert _wait_until(lambda: any(
                "In-process cron scheduler started" in r.message for r in caplog.records))
            assert _wait_until(lambda: any(
                "Gateway housekeeping started" in r.message for r in caplog.records))
        # The ticker stamps liveness for `hermes cron status` even with nothing to deliver to.
        from cron.jobs import get_ticker_heartbeat_age
        assert _wait_until(lambda: get_ticker_heartbeat_age() is not None)
        assert runner.should_exit_cleanly is False
        assert read_runtime_status()["gateway_state"] == "running"
    finally:
        if cron_stop is not None:
            cron_stop.set()
        for thread in (cron_thread, housekeeping_thread):
            if thread is not None:
                thread.join(timeout=15)
                assert not thread.is_alive()
        await runner.stop()
