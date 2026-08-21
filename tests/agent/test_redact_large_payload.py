"""Regression: a large base64 tool result must not freeze the interpreter.

A Gmail attachment fetched through ``execute_code`` comes back as a JSON
envelope whose ``data`` field is one multi-megabyte single-line base64 blob.
That blob runs through ``redact_sensitive_text`` before it reaches the model.

``_CFG_DOTTED_RE`` used to scan it with unbounded ``[A-Za-z0-9_.\\-]`` runs —
and base64 lies entirely inside that character class, so the scan cost
O(n**2). ``sre`` never releases the GIL, so the whole gateway (event loop,
platform poller, liveness watchdog) starved behind one ``re.sub``. An agent
went unresponsive for nearly two hours and only ``SIGKILL`` recovered it.

These tests run the redaction in a CHILD PROCESS on purpose: on the unfixed
code the call cannot be interrupted from Python at all (signal handlers run
between bytecodes and there are no bytecodes), so an in-process timeout would
hang the whole suite instead of failing it.
"""

from __future__ import annotations

import base64
import json
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Big enough that the old O(n**2) scan cannot finish, small enough to build fast.
LARGE_ATTACHMENT_BYTES = 1024 * 1024
REDACTION_TIME_BUDGET_SECONDS = 60.0


def _gmail_attachment_envelope(raw_bytes: int, *, seed: int = 11) -> str:
    """A `gws … attachments get --format json` response: one giant base64 line."""
    generator = random.Random(seed)
    blob = base64.urlsafe_b64encode(generator.randbytes(raw_bytes)).decode()
    # Past roughly a megabyte a secret keyword turns up in a base64 blob BY
    # CHANCE ('auth' is four characters, matched case-insensitively), which is
    # what opens the redaction keyword gate in production. Pin it so the test
    # is deterministic instead of relying on that coin flip.
    blob = "auth" + blob[4:]
    return json.dumps({"size": raw_bytes, "data": blob})


def _redact_in_child_process(payload: str, timeout_seconds: float) -> str:
    """Redact `payload` in a subprocess, so a wedged regex fails instead of hangs."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from agent.redact import redact_sensitive_text
        sys.stdout.write(redact_sensitive_text(sys.stdin.read()))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(REPOSITORY_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_large_base64_attachment_redacts_within_budget():
    """The user-visible symptom: the agent stops responding at all."""
    payload = _gmail_attachment_envelope(LARGE_ATTACHMENT_BYTES)
    try:
        _redact_in_child_process(payload, REDACTION_TIME_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"redact_sensitive_text did not finish a {LARGE_ATTACHMENT_BYTES // 1024} KiB "
            f"base64 tool result within {REDACTION_TIME_BUDGET_SECONDS}s. This is the "
            "catastrophic-backtracking freeze: in production it holds the GIL and takes "
            "the whole gateway down with it."
        )


def test_a_long_line_does_not_hide_secrets_on_neighbouring_lines():
    """The long-line skip must not become a way to smuggle a secret past redaction."""
    giant_line = base64.urlsafe_b64encode(random.Random(3).randbytes(256 * 1024)).decode()
    text = f"app.api.key=sk-live-should-be-masked\n{giant_line}\nspring.datasource.password=hunter2"

    # Also via the child process: without the fix this input is quadratic too,
    # and an in-process call would hang the suite rather than fail it.
    try:
        redacted = _redact_in_child_process(text, REDACTION_TIME_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail(
            "redact_sensitive_text hung on a mixed short-line/long-line payload — "
            "the catastrophic-backtracking freeze."
        )

    assert "sk-live-should-be-masked" not in redacted
    assert "hunter2" not in redacted
    # The oversized line is not scanned by the config pattern, but an existing
    # long-opaque-token rule still elides its middle — that behaviour predates
    # this fix. What matters here is that it is not swallowed wholesale.
    assert giant_line[:64] in redacted


@pytest.mark.parametrize(
    "line, secret",
    [
        ("app.api.key=sk-live-abcdef123456", "sk-live-abcdef123456"),
        ("spring.datasource.password=hunter2", "hunter2"),
        ("  service.auth.token=eyJhbGciOi", "eyJhbGciOi"),
        ("export API_KEY=sk-abcdef", "sk-abcdef"),
    ],
)
def test_config_assignments_are_still_redacted(line, secret):
    """Bounding the runs must not narrow what counts as a secret."""
    from agent.redact import redact_sensitive_text

    assert secret not in redact_sensitive_text(line)


@pytest.mark.parametrize("line", ["author=Smith", "press.secretary=Smith"])
def test_prose_is_still_left_alone(line):
    """The prose guards that motivated the backtrackable runs still hold."""
    from agent.redact import redact_sensitive_text

    assert redact_sensitive_text(line) == line
