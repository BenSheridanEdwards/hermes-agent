"""The ``hermes acp`` ACP-hosted marker gate in ``hermes_cli.main``.

``main`` loads the profile ``.env`` at import time, before ``cmd_acp`` dispatches, so the marker that
protects the host's managed identity has to be decided from ``sys.argv`` at module scope. The first
version gated on ``sys.argv[1:2] == ["acp"]``, which misses every legal ``hermes <zero-arg flag> acp``
invocation, and a missed marker was worse than no protection while the host snapshot was re-taken per
load, because the first unmarked load latched the profile's value as "host-owned".

The end-to-end cases run in a subprocess: the gate is import-time code, so it cannot be re-exercised by
re-importing an already-imported module in the test process.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.main import _argv_selects_acp

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    sys.argv = json.loads(os.environ["PROBE_ARGV"])
    import hermes_cli.main  # noqa: F401
    from hermes_cli.env_loader import is_acp_hosted
    print(json.dumps({
        "hosted": is_acp_hosted(),
        "key": os.environ.get("BUZZ_PRIVATE_KEY"),
        "tag": os.environ.get("BUZZ_AUTH_TAG"),
        "home": os.environ.get("HERMES_HOME"),
    }))
    """
)


def _run_probe(tmp_path, argv, env_extra):
    """Import ``hermes_cli.main`` in a clean subprocess with ``argv`` and report the resulting env."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "fakehome"),
        "PYTHONPATH": str(REPO_ROOT),
        "PROBE_ARGV": json.dumps(argv),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        **env_extra,
    }
    (tmp_path / "fakehome").mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, text=True, env=env, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "argv",
    [
        ["hermes", "acp"],
        ["hermes", "--yolo", "acp"],
        ["hermes", "--safe-mode", "acp"],
        ["hermes", "--dev", "acp"],
        ["hermes", "--yolo", "--dev", "acp"],
        ["hermes", "--profile=work", "acp"],
    ],
)
def test_argv_selects_acp_accepts_flags_before_the_subcommand(argv):
    """Zero-arg top-level options are legal before the subcommand, and
    BUZZ_ACP_AGENT_ARGS is operator-supplied, so the gate must not assume the
    subcommand sits at argv[1]."""
    assert _argv_selects_acp(argv[1:]) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["chat"],
        ["--yolo", "chat"],
        ["update"],
        ["--yolo"],
        ["acp-ish"],
    ],
)
def test_argv_selects_acp_rejects_everything_else(argv):
    """Only the acp subcommand arms the marker; no other entrypoint changes
    precedence."""
    assert _argv_selects_acp(argv) is False


def test_argv_selects_acp_defaults_to_sys_argv(monkeypatch):
    """The import-time call takes no argument and reads sys.argv, which
    _apply_profile_override has already stripped --profile/-p out of."""
    monkeypatch.setattr(sys, "argv", ["hermes", "--yolo", "acp"])
    assert _argv_selects_acp() is True
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    assert _argv_selects_acp() is False


def test_flagged_acp_invocation_keeps_the_managed_identity(tmp_path):
    """End to end through main's import-time load: `hermes --yolo acp` under the
    Buzz managed-agent harness keeps the host's key and does not let the profile
    .env supply the auth tag that belongs to a different key."""
    home = tmp_path / "hermes" / "profiles" / "p1"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "BUZZ_PRIVATE_KEY=profile-key\nBUZZ_AUTH_TAG=profile-tag\n", encoding="utf-8"
    )

    out = _run_probe(
        tmp_path,
        ["hermes", "--yolo", "acp"],
        {"HERMES_HOME": str(home), "BUZZ_MANAGED_AGENT": "1", "BUZZ_PRIVATE_KEY": "managed-key"},
    )

    assert out["hosted"] is True
    assert out["key"] == "managed-key"
    assert out["tag"] is None


def test_non_acp_invocation_leaves_precedence_alone(tmp_path):
    """The same profile under `hermes --yolo chat`: unmarked, so the .env keeps
    its documented override of inherited shell values."""
    home = tmp_path / "hermes" / "profiles" / "p1"
    home.mkdir(parents=True)
    (home / ".env").write_text("BUZZ_PRIVATE_KEY=profile-key\n", encoding="utf-8")

    out = _run_probe(
        tmp_path,
        ["hermes", "--yolo", "chat"],
        {"HERMES_HOME": str(home), "BUZZ_MANAGED_AGENT": "1", "BUZZ_PRIVATE_KEY": "managed-key"},
    )

    assert out["hosted"] is False
    assert out["key"] == "profile-key"


def test_hermes_home_snapshot_is_the_routed_profile_not_the_raw_host_value(tmp_path):
    """Documents what HERMES_HOME protection actually means: _apply_profile_override
    runs BEFORE the marker, so a host that points at the hermes ROOT is still
    redirected onto the sticky active_profile, and the snapshot pins that. The
    protection is against the profile's own .env, not against the profile router."""
    root = tmp_path / "hermes"
    (root / "profiles" / "other").mkdir(parents=True)
    (root / "active_profile").write_text("other\n", encoding="utf-8")
    (root / "profiles" / "other" / ".env").write_text(
        "HERMES_HOME=/profile/says/elsewhere\n", encoding="utf-8"
    )

    out = _run_probe(
        tmp_path,
        ["hermes", "acp"],
        {"HERMES_HOME": str(root)},
    )

    assert out["hosted"] is True
    assert out["home"] == str(root / "profiles" / "other")
