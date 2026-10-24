"""Installed buzz-acp must see a live marker and receive one origin-scoped result.

This drives the real Fleet Buzz ACP executable against a fake engine, then the
real Hermes marker and ACP drain against an isolated home. No relay, no live
profile, no public channel.
"""

from __future__ import annotations

import json
import os
import selectors
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

BUZZ_ACP = "/Applications/Fleet Buzz.app/Contents/MacOS/buzz-acp"


def _write_engine(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
log = home / "engine-log.jsonl"

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def record(obj):
    with log.open("a") as fh:
        fh.write(json.dumps(obj) + "\\n")

session_id = None
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    record({"in": msg, "marker": (home / ".buzz-background-work").exists()})
    method = msg.get("method")
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": msg["params"].get("protocolVersion", 1),
            "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": False}},
            "agentInfo": {"name": "fake-hermes", "version": "0"},
        }})
    elif method == "session/new":
        session_id = "sess-origin"
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": session_id}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        text = " ".join(
            block.get("text", "")
            for block in msg["params"].get("prompt", [])
            if isinstance(block, dict)
        )
        record({"prompt_session": sid, "prompt": text, "marker_at_prompt": (home / ".buzz-background-work").read_text() if (home / ".buzz-background-work").exists() else None})
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "reported once"}},
        }})
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
    elif "id" in msg:
        emit({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
"""
    )
    path.chmod(0o755)


def _run_buzz(home: Path, engine: Path, *, seconds: float = 2.5) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home / "outer-home"),
        "HERMES_HOME": str(home),
        "BUZZ_ACP_AGENT_COMMAND": str(engine),
        "BUZZ_ACP_LAZY_POOL": "true",
        "BUZZ_ACP_IDLE_POOL_SLEEP": "1",
        "BUZZ_ACP_HEARTBEAT_INTERVAL": "3600",
        "BUZZ_ACP_REPLY_MODE": "harness",
        "BUZZ_ACP_RESPOND_TO": "owner-only",
        "BUZZ_RELAY_URL": "http://127.0.0.1:9",
        "BUZZ_PRIVATE_KEY": "",
        "NOSTR_PRIVATE_KEY": "",
        "TMPDIR": str(home / "tmp"),
    }
    (home / "outer-home").mkdir()
    (home / "tmp").mkdir()
    proc = subprocess.Popen(
        [BUZZ_ACP],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + seconds
    out, err = [], []
    while time.monotonic() < deadline and proc.poll() is None:
        for key, _ in sel.select(timeout=0.2):
            line = key.fileobj.readline()
            if not line:
                continue
            (out if key.fileobj is proc.stdout else err).append(line)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    return subprocess.CompletedProcess(proc.args, proc.returncode or 0, "".join(out), "".join(err))


def test_installed_buzz_keeps_pool_only_while_marker_is_live(tmp_path: Path):
    if not Path(BUZZ_ACP).is_file():
        pytest.skip("installed Fleet buzz-acp is not present")
    engine = tmp_path / "engine.py"
    _write_engine(engine)
    home = tmp_path / "home"
    home.mkdir()

    absent = _run_buzz(home, engine, seconds=1.2)
    assert not (home / "engine-log.jsonl").exists(), absent.stderr[-1000:]

    marker = home / ".buzz-background-work"
    marker.write_text(json.dumps({
        "pid": 1,
        "ts": int(time.time()),
        "running": ["d-running"],
        "pending": [{"delegation_id": "d-pending", "origin_session": "sess-origin"}],
    }) + "\n")
    present = _run_buzz(home, engine, seconds=2.5)
    log = (home / "engine-log.jsonl").read_text() if (home / "engine-log.jsonl").exists() else ""
    assert "sess-origin" in log, (log, present.stderr[-1500:])
    prompts = [json.loads(line) for line in log.splitlines() if '"prompt"' in line or "prompt_session" in line]
    assert prompts, log
    assert all(item.get("prompt_session") == "sess-origin" for item in prompts if "prompt_session" in item)
    assert "d-pending" in prompts[0]["marker_at_prompt"]


def test_repaired_marker_and_acp_drain_route_once(tmp_path, monkeypatch):
    import tools.async_delegation as ad
    from acp_adapter.server import HermesACPAgent
    from tools.process_registry import process_registry

    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    marker = tmp_path / ad._BACKGROUND_WORK_MARKER
    now = time.time()
    ad._persist_dispatch({"delegation_id": "d-run", "session_key": "sess-origin", "dispatched_at": now})
    running = json.loads(marker.read_text())
    assert running["running"] == ["d-run"]
    assert running["pending"] == []

    ad._persist_completion(
        {"delegation_id": "d-run", "status": "completed", "session_key": "sess-origin", "summary": "alpha"},
        {"summary": "alpha"},
    )
    queued = json.loads(marker.read_text())
    assert queued["pending"] == [{"delegation_id": "d-run", "origin_session": "sess-origin"}]

    # A cancelled running row must not remain advertised as live work.
    ad._persist_dispatch({"delegation_id": "d-cancel", "session_key": "sess-origin", "dispatched_at": now})
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE async_delegations SET state='cancelled', delivery_state='dropped' WHERE delegation_id='d-cancel'"
        )
    ad.refresh_background_work_marker()
    after_cancel = json.loads(marker.read_text())
    assert "d-cancel" not in after_cancel["running"]
    assert all(item["delegation_id"] != "d-cancel" for item in after_cancel["pending"])

    process_registry.completion_queue.put({
        "type": "async_delegation", "delegation_id": "d-run", "session_key": "sess-origin",
        "status": "completed", "summary": "alpha",
    })
    process_registry.completion_queue.put({
        "type": "async_delegation", "delegation_id": "foreign", "session_key": "other",
        "status": "completed", "summary": "beta",
    })
    agent = HermesACPAgent(session_manager=None)
    first, claims = agent._drain_ready_process_notifications("sess-origin")
    assert "alpha" in first and "beta" not in first and claims

    # Failed turn releases the claim and keeps the result pending.
    agent._settle_process_notifications(claims, delivered=False, attempted=True)
    row = ad.get_durable_delegation("d-run")
    assert row["delivery_state"] == "pending"
    assert json.loads(marker.read_text())["pending"][0]["origin_session"] == "sess-origin"

    second, claims2 = agent._drain_ready_process_notifications("sess-origin")
    assert "alpha" in second
    agent._settle_process_notifications(claims2, delivered=True, attempted=True)
    assert ad.get_durable_delegation("d-run")["delivery_state"] == "delivered"
    assert not marker.exists()
    third, _ = agent._drain_ready_process_notifications("sess-origin")
    assert third == ""
    leftover = []
    while not process_registry.completion_queue.empty():
        leftover.append(process_registry.completion_queue.get_nowait())
    assert [item["delegation_id"] for item in leftover] == ["foreign"]
