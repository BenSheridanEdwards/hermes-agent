"""``security.launchd_allowed_labels`` lets an app installer reload its own LaunchAgent from inside
the gateway (Fleet's ``scripts/install.sh`` bootstraps ``ai.fleet.desktop``); a script that also names
a hermes-gateway label is still refused, and with no allowlist every bootstrap stays refused."""

import os
from pathlib import Path

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as guard

FLEET_INSTALL = """#!/bin/bash
label="ai.fleet.desktop"
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
"""


def _allow(*labels):
    config = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    config.write_text("security:\n  launchd_allowed_labels: [" + ", ".join(labels) + "]\n")


def _install_script(tmp_path, body=FLEET_INSTALL):
    script = tmp_path / "install.sh"
    script.write_text(body)
    script.chmod(0o755)
    return f"bash {script}"


def test_allowed_app_installer_reloads_its_launch_agent(tmp_path):
    _allow("ai.fleet.desktop")
    assert guard(_install_script(tmp_path), cwd=str(tmp_path)) is False


def test_installer_stays_refused_without_an_allowlist(tmp_path):
    _allow()
    assert guard(_install_script(tmp_path), cwd=str(tmp_path)) is True


def test_allowed_label_cannot_carry_a_gateway_job(tmp_path):
    _allow("ai.fleet.desktop")
    laundered = FLEET_INSTALL + "launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.hermes.gateway.restart-once.plist\n"
    assert guard(_install_script(tmp_path, laundered), cwd=str(tmp_path)) is True
