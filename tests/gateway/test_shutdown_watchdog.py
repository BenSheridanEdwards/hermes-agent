"""Shutdown watchdog + loop heartbeat coverage for #66892.

The drain path is asyncio-based; a frozen loop makes every asyncio timeout
structurally unable to fire. These tests pin the out-of-loop backstop
(thread watchdog) and the loop-liveness heartbeat file contract.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest

from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    start_loop_liveness_watchdog,
    write_loop_heartbeat,
)


def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == 180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    snapshot_calls = []
    exit_codes = []

    def snapshot():
        snapshot_calls.append(1)
        return {"active_agents": 1, "draining": True}

    def fake_exit(code):
        exit_codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=snapshot,
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0), "watchdog did not fire"

    assert exit_codes == [9]
    assert snapshot_calls == [1]
    assert dump.is_file()
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert get_shutdown_watchdog_dump_path(tmp_path).name == "gateway-shutdown-watchdog.log"


def test_liveness_timer_is_cancelled_before_shutdown_watchdog_arms(tmp_path):
    loop = asyncio.new_event_loop()
    shutdown_fired = threading.Event()

    with (
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback_later"),
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
        patch(
            "gateway.shutdown_watchdog.os._exit",
            side_effect=lambda _code: shutdown_fired.set(),
        ),
    ):
        liveness = start_loop_liveness_watchdog(loop, probe_interval=10.0)
        assert liveness is not None
        assert liveness.notify_loop_progress() is True
        liveness.stop()
        liveness.join(timeout=1.0)
        cancel_deadline.assert_called_once_with()
        try:
            arm_shutdown_watchdog(0.01, dump_path=tmp_path / "shutdown.log")
            assert shutdown_fired.wait(timeout=2.0)
            cancel_deadline.assert_called_once_with()
        finally:
            liveness.stop()
            loop.close()


@pytest.mark.asyncio
async def test_loop_heartbeat_reports_progress_before_file_write():
    events = []

    async def no_wait(_delay):
        return None

    keep_running = iter((True, False))
    with (
        patch("gateway.shutdown_watchdog.asyncio.sleep", side_effect=no_wait),
        patch(
            "gateway.shutdown_watchdog.write_loop_heartbeat",
            side_effect=lambda **_kwargs: events.append("write"),
        ),
    ):
        await loop_heartbeat_forever(
            on_progress=lambda: events.append("progress"),
            should_continue=lambda: next(keep_running),
        )

    assert events == ["progress", "write"]


@pytest.mark.asyncio
async def test_disarm_blocks_heartbeat_rearm_after_shutdown_begins(tmp_path):
    """A heartbeat crossing stop() cannot resurrect the native deadline."""
    loop = asyncio.get_running_loop()
    keep_running = iter((True, True, False))

    with (
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
        patch("gateway.shutdown_watchdog.write_loop_heartbeat"),
    ):
        liveness = start_loop_liveness_watchdog(loop, probe_interval=10.0)
        assert liveness is not None

        async def begin_shutdown(_delay):
            liveness.stop()

        with patch(
            "gateway.shutdown_watchdog.asyncio.sleep", side_effect=begin_shutdown
        ):
            await loop_heartbeat_forever(
                on_progress=liveness.notify_loop_progress,
                should_continue=lambda: next(keep_running),
                home=tmp_path,
            )
        liveness.join(timeout=1.0)

    arm_deadline.assert_called_once()
    cancel_deadline.assert_called_once_with()
