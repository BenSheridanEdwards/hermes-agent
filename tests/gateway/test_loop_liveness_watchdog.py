"""Gateway event-loop freeze backstops for issue #69089."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gateway.shutdown_watchdog import (
    _arm_loop_floor_timer,
    start_loop_liveness_watchdog,
)


@pytest.fixture(autouse=True)
def _isolate_faulthandler_deadline():
    """Unit tests must never leave a process-global hard-exit timer armed."""
    with (
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback_later"),
        patch("gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"),
    ):
        yield


def _immediate_loop() -> MagicMock:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: callback()
    return loop


_SUBPROCESS_EXERCISE = r"""
import asyncio, ctypes, sys
from gateway.shutdown_watchdog import start_loop_liveness_watchdog

def hold_gil(seconds):
    if sys.platform == "win32":
        library = ctypes.PyDLL("kernel32")
        library.Sleep.argtypes = [ctypes.c_ulong]
        library.Sleep.restype = None
        library.Sleep(int(seconds * 1000))
        return
    library = ctypes.PyDLL(None)
    library.sleep.argtypes = [ctypes.c_uint]
    library.sleep.restype = ctypes.c_uint
    library.sleep(int(seconds))

async def exercise():
    handle = start_loop_liveness_watchdog(
        asyncio.get_running_loop(),
        probe_interval=0.25,
        probe_timeout=0.20,
        max_strikes=1,
        exit_code=75,
    )
    assert handle is not None and handle.is_alive()
    handle.notify_loop_progress()
    await asyncio.sleep(0.30)
    print("ARMED", flush=True)
    if sys.argv[1] == "healthy":
        for _ in range(15):
            await asyncio.sleep(0.20)
            handle.notify_loop_progress()
        handle.stop()
        handle.join(0.5)
    elif sys.argv[1] == "stopped":
        handle.stop()
        handle.join(0.5)
        hold_gil(2)
    elif sys.argv[1] == "loop_blocked":
        import time
        time.sleep(4)
    else:
        hold_gil(4)

asyncio.run(exercise())
"""


def _run_watchdog_child(mode: str, tmp_path: Path, timeout: float = 5.0):
    env = dict(
        os.environ,
        HERMES_HOME=str(tmp_path),
        PYTHONDONTWRITEBYTECODE="1",
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", _SUBPROCESS_EXERCISE, mode],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"watchdog child {mode!r} survived {timeout}s; "
            f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
        )
    return result, time.monotonic() - started


def _assert_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path):
    result, elapsed = _run_watchdog_child("gil_held", tmp_path)

    assert result.stdout.strip() == "ARMED"
    assert result.returncode != 0
    assert result.returncode == 1
    assert elapsed < 5.0
    dump = (tmp_path / "logs" / "gateway-loop-liveness-watchdog.log").read_text(
        encoding="utf-8"
    )
    assert "Timeout" in dump
    assert "exercise" in dump


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_linux_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path):
    _assert_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path)


@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_macos_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path):
    _assert_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path)


@pytest.mark.windows_only
@pytest.mark.live_system_guard_bypass
def test_windows_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path):
    _assert_gil_held_c_call_dumps_stacks_and_exits_before_deadline(tmp_path)


@pytest.mark.live_system_guard_bypass
def test_healthy_loop_survives_repeated_hard_deadlines(tmp_path):
    result, elapsed = _run_watchdog_child("healthy", tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ARMED"
    assert elapsed >= 3.0


def _assert_stopping_loop_watchdog_disarms_hard_deadline(tmp_path):
    result, elapsed = _run_watchdog_child("stopped", tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ARMED"
    assert elapsed >= 2.0


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_linux_stopping_loop_watchdog_disarms_hard_deadline(tmp_path):
    _assert_stopping_loop_watchdog_disarms_hard_deadline(tmp_path)


@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_macos_stopping_loop_watchdog_disarms_hard_deadline(tmp_path):
    _assert_stopping_loop_watchdog_disarms_hard_deadline(tmp_path)


@pytest.mark.windows_only
@pytest.mark.live_system_guard_bypass
def test_windows_stopping_loop_watchdog_disarms_hard_deadline(tmp_path):
    _assert_stopping_loop_watchdog_disarms_hard_deadline(tmp_path)


@pytest.mark.live_system_guard_bypass
def test_gil_releasing_frozen_loop_keeps_existing_recovery(tmp_path):
    result, elapsed = _run_watchdog_child("loop_blocked", tmp_path)

    assert result.returncode == 75
    assert elapsed < 5.0
    assert "missed 1 consecutive liveness probes" in result.stderr
    assert "Current thread" in result.stderr


def test_loop_liveness_watchdog_stop_during_dump_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    handle_ready = threading.Event()
    handle_ref = {}
    exit_codes = []

    def stop_during_dump(*_args, **_kwargs) -> None:
        assert handle_ready.wait(timeout=2.0)
        handle_ref["handle"].stop()

    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback",
            side_effect=stop_during_dump,
        ) as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        handle_ref["handle"] = handle
        handle_ready.set()
        handle.join(timeout=2.0)

    assert not handle.is_alive()
    critical.assert_called_once()
    dump.assert_called_once_with(all_threads=True)
    assert exit_codes == []


def test_faulthandler_deadline_has_single_lifecycle_owner():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    files_at_cancel = []

    with (
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
    ):
        cancel_deadline.side_effect = lambda: files_at_cancel.append(
            arm_deadline.call_args.kwargs["file"].closed
        )
        owner = start_loop_liveness_watchdog(loop, probe_interval=10.0)
        non_owner = start_loop_liveness_watchdog(loop, probe_interval=10.0)
        assert owner is not None and non_owner is not None
        assert arm_deadline.call_count == 0
        assert owner.notify_loop_progress() is True
        first_file = arm_deadline.call_args.kwargs["file"]
        assert owner.notify_loop_progress() is True
        assert arm_deadline.call_args.kwargs["file"] is first_file
        assert non_owner.notify_loop_progress() is False

        owner.stop()
        owner.join(timeout=1.0)
        cancel_deadline.assert_called_once_with()

        assert non_owner.notify_loop_progress() is True
        owner.stop()
        cancel_deadline.assert_called_once_with()

        non_owner.stop()
        non_owner.join(timeout=1.0)

    owned_files = [call.kwargs["file"] for call in arm_deadline.call_args_list]
    assert arm_deadline.call_count == 3
    assert cancel_deadline.call_count == 2
    assert all(call.kwargs["repeat"] is False for call in arm_deadline.call_args_list)
    assert all(call.kwargs["exit"] is True for call in arm_deadline.call_args_list)
    assert files_at_cancel == [False, False]
    assert all(file.closed for file in owned_files)


def test_closed_loop_never_activates_native_deadline_before_heartbeat():
    closed_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    closed_loop.call_soon_threadsafe.side_effect = RuntimeError("closed")

    with (
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch("gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"),
    ):
        closed = start_loop_liveness_watchdog(closed_loop, probe_interval=0.01)
        assert closed is not None
        closed.join(timeout=1.0)
        assert not closed.is_alive()

        assert arm_deadline.call_count == 0
        closed.stop()


def test_native_arm_failure_is_visible_inactive_and_not_cancelled(caplog):
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with (
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later",
            side_effect=RuntimeError("unable to start watchdog thread"),
        ) as arm_deadline,
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
    ):
        handle = start_loop_liveness_watchdog(loop, probe_interval=10.0)
        assert handle is not None
        try:
            assert handle.notify_loop_progress() is False
            assert handle.notify_loop_progress() is False
        finally:
            handle.stop()
            handle.join(timeout=1.0)

    assert arm_deadline.call_count == 1
    cancel_deadline.assert_not_called()
    assert arm_deadline.call_args.kwargs["file"].closed
    assert "could not arm" in caplog.text


def test_concurrent_native_deadline_activation_has_one_owner():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    first = start_loop_liveness_watchdog(loop, probe_interval=10.0)
    second = start_loop_liveness_watchdog(loop, probe_interval=10.0)
    assert first is not None and second is not None
    contenders = [first, second]
    ready = threading.Barrier(3)
    results = {}

    def activate(index: int) -> None:
        ready.wait(timeout=2.0)
        results[index] = contenders[index].notify_loop_progress()

    with (
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
    ):
        threads = [
            threading.Thread(target=activate, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        ready.wait(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)

        assert all(not thread.is_alive() for thread in threads)
        assert sorted(results.values()) == [False, True]
        arm_deadline.assert_called_once()

        owner_index = next(index for index, acquired in results.items() if acquired)
        next_index = 1 - owner_index
        contenders[owner_index].stop()
        assert contenders[next_index].notify_loop_progress() is True

        contenders[next_index].stop()
        for contender in contenders:
            contender.join(timeout=1.0)

    assert arm_deadline.call_count == 2
    assert cancel_deadline.call_count == 2


def test_python_thread_start_failure_keeps_native_deadline_available(caplog):
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    with (
        patch(
            "gateway.shutdown_watchdog.threading.Thread.start", side_effect=RuntimeError
        ),
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch("gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"),
    ):
        handle = start_loop_liveness_watchdog(loop)
        assert handle is not None
        assert not handle.is_alive()
        assert handle.notify_loop_progress() is True
        handle.stop()

    arm_deadline.assert_called_once()
    assert "Failed to start gateway loop liveness watchdog" in caplog.text


def test_loop_liveness_watchdog_stop_during_final_miss_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()
    probe_event_ref = {}
    handle_ref = {}
    exit_codes = []

    class FinalStrikeLimit:
        def __gt__(self, _strikes: int) -> bool:
            # If strike evaluation is reached, keep recheck #2 from masking a
            # missing post-probe recheck #1 in this boundary test.
            handle_ref["handle"]._stop_event.clear()
            return False

    def hold_scheduled_probe(callback) -> None:
        probe_event_ref["event"] = callback.__self__
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop,
            probe_interval=0.01,
            probe_timeout=0.01,
            max_strikes=FinalStrikeLimit(),
        )
        assert handle is not None
        handle_ref["handle"] = handle
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        def stop_during_miss() -> bool:
            handle.stop()
            return False

        probe_event_ref["event"].is_set = stop_during_miss
        release_probe.set()
        handle.join(timeout=1.0)

    assert not handle.is_alive()
    assert exit_codes == []
    critical.assert_not_called()
    dump.assert_not_called()


def test_loop_liveness_watchdog_stop_after_first_recheck_skips_final_actions():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()

    def hold_scheduled_probe(callback) -> None:
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit") as hard_exit,
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        original_is_set = handle._stop_event.is_set
        is_set_calls = 0

        def stop_on_final_recheck() -> bool:
            nonlocal is_set_calls
            is_set_calls += 1
            # With the forced immediate timeout: _wait_for_probe is call 1,
            # recheck #1 is call 2, and recheck #2 is call 3.
            if is_set_calls == 3:
                handle.stop()
            return original_is_set()

        handle._stop_event.is_set = stop_on_final_recheck
        with patch(
            "gateway.shutdown_watchdog.time.monotonic", side_effect=[0.0, 1.0]
        ):
            release_probe.set()
            handle.join(timeout=1.0)

    assert is_set_calls == 3
    assert not handle.is_alive()
    critical.assert_not_called()
    dump.assert_not_called()
    hard_exit.assert_not_called()


def test_gateway_config_loop_watchdog_round_trip():
    """loop_watchdog is a config.yaml knob: default on, nested-gateway form honored."""
    from gateway.config import GatewayConfig

    assert GatewayConfig.from_dict({}).loop_watchdog is True
    assert GatewayConfig.from_dict({"loop_watchdog": False}).loop_watchdog is False
    assert (
        GatewayConfig.from_dict(
            {"gateway": {"loop_watchdog": "off"}}
        ).loop_watchdog
        is False
    )
    config = GatewayConfig.from_dict({"loop_watchdog": False})
    assert config.to_dict()["loop_watchdog"] is False


def test_gateway_runner_liveness_guards_start_and_stop():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._loop_floor_timer_handle = None
    runner._loop_liveness_watchdog = None
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    floor_timer = MagicMock()
    watchdog = MagicMock()
    watchdog.is_alive.return_value = True

    with (
        patch(
            "gateway.run._arm_loop_floor_timer", return_value=floor_timer
        ) as arm_floor,
        patch(
            "gateway.run.start_loop_liveness_watchdog", return_value=watchdog
        ) as start_watchdog,
    ):
        runner._start_loop_liveness_guards(loop)

    arm_floor.assert_called_once_with(loop)
    start_watchdog.assert_called_once_with(loop)
    assert runner._loop_floor_timer_handle is floor_timer
    assert runner._loop_liveness_watchdog is watchdog

    runner._stop_loop_liveness_guards()

    watchdog.stop.assert_called_once_with()
    floor_timer.cancel.assert_called_once_with()
    assert runner._loop_liveness_watchdog is None
    assert runner._loop_floor_timer_handle is None


def test_gateway_runner_stops_dead_watchdog_before_replacement():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._loop_floor_timer_handle = MagicMock()
    stale_watchdog = MagicMock()
    stale_watchdog.is_alive.return_value = False
    runner._loop_liveness_watchdog = stale_watchdog
    replacement = MagicMock()
    replacement.is_alive.return_value = True
    loop = MagicMock(spec=asyncio.AbstractEventLoop)

    with patch(
        "gateway.run.start_loop_liveness_watchdog", return_value=replacement
    ) as start_watchdog:
        runner._start_loop_liveness_guards(loop)

    stale_watchdog.stop.assert_called_once_with()
    start_watchdog.assert_called_once_with(loop)
    assert runner._loop_liveness_watchdog is replacement


def test_gateway_runner_loop_watchdog_opt_out_arms_nothing():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock(loop_watchdog=False)
    runner._loop_floor_timer_handle = None
    runner._loop_liveness_watchdog = None
    loop = MagicMock(spec=asyncio.AbstractEventLoop)

    with (
        patch("gateway.run._arm_loop_floor_timer") as arm_floor,
        patch("gateway.run.start_loop_liveness_watchdog") as start_watchdog,
    ):
        runner._start_loop_liveness_guards(loop)

    arm_floor.assert_not_called()
    start_watchdog.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "enabled", "expected_progress_calls"),
    ((False, True, 0), (True, False, 0), (True, True, 1)),
)
async def test_gateway_heartbeat_activates_native_deadline_only_when_running_and_enabled(
    running, enabled, expected_progress_calls
):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock(loop_watchdog=enabled)
    runner._running = running
    runner._gateway_started_at = time.time()
    runner._loop_heartbeat_task = None
    runner._background_tasks = set()
    runner._loop_liveness_watchdog = MagicMock()

    with patch("gateway.shutdown_watchdog.write_loop_heartbeat") as write_heartbeat:
        runner._start_loop_heartbeat_task()
        await asyncio.sleep(0)

    write_heartbeat.assert_called_once()
    assert runner._loop_liveness_watchdog.notify_loop_progress.call_count == (
        expected_progress_calls
    )
    runner._loop_heartbeat_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner._loop_heartbeat_task


@pytest.mark.asyncio
async def test_gateway_queued_heartbeat_cannot_rearm_deadline_after_guards_stop():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock(loop_watchdog=True)
    runner._running = True
    runner._gateway_started_at = time.time()
    runner._loop_heartbeat_task = None
    runner._background_tasks = set()
    runner._loop_floor_timer_handle = MagicMock()
    watchdog = start_loop_liveness_watchdog(
        MagicMock(spec=asyncio.AbstractEventLoop), probe_interval=10.0
    )
    assert watchdog is not None
    runner._loop_liveness_watchdog = watchdog

    with (
        patch("gateway.shutdown_watchdog.write_loop_heartbeat") as write_heartbeat,
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback_later"
        ) as arm_deadline,
        patch(
            "gateway.shutdown_watchdog.faulthandler.cancel_dump_traceback_later"
        ) as cancel_deadline,
    ):
        runner._start_loop_heartbeat_task()
        heartbeat_task = cast(asyncio.Task, runner._loop_heartbeat_task)
        runner._stop_loop_liveness_guards()
        await asyncio.sleep(0)

        write_heartbeat.assert_called_once()
        arm_deadline.assert_not_called()
        cancel_deadline.assert_not_called()

        heartbeat_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat_task

    watchdog.join(timeout=1.0)
    assert not watchdog.is_alive()
