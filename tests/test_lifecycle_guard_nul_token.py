"""Regression tests: lifecycle guard must survive binary "script" references.

Reproduced 2026-08-24 (Doc): running
``PATH="<venv>/bin:$PATH" python3 -m unittest tests.test_mcp_watchdog_app_bundle``
crashed the terminal tool before execution with
``ValueError: embedded null byte`` from ``Path(candidate).expanduser()`` in
``cron/lifecycle_guard.py::_resolve_terminal_script_path``.

Chain: the guard's script walker yields every absolute-path executable token
(including the venv python on a PATH-prefixed command); ``_read_referenced_script``
correctly refuses binaries, but terminal_tool's ``_read_script_in_env`` fallback
decoded a binary's bytes and fed machine code back into the walker as if it were
shell text. A NUL-bearing token then hit ``Path(...).expanduser()``, which raises
ValueError outside any consumer try/except.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cron.lifecycle_guard import (
    _resolve_terminal_script_path,
    contains_gateway_lifecycle_command_or_referenced_script,
)


class NulTokenResolutionTests(unittest.TestCase):
    def test_nul_token_returns_nonexistent_sentinel(self):
        """A NUL-bearing candidate resolves to a safe nonexistent path."""
        resolved = _resolve_terminal_script_path(
            "/usr/bin/pytho\x00n3 - junk", cwd=None
        )
        self.assertFalse(resolved.exists())
        # And it must itself be usable through Path.expanduser without error.
        self.assertEqual(resolved.expanduser(), resolved)

    def test_normal_absolute_candidate_still_resolves(self):
        resolved = _resolve_terminal_script_path("/bin/sh", cwd=None)
        self.assertEqual(resolved, Path("/bin/sh"))

    def test_relative_candidate_joins_cwd(self):
        resolved = _resolve_terminal_script_path("scripts/restart.sh", cwd="/tmp/x")
        self.assertEqual(resolved, Path("/tmp/x/scripts/restart.sh"))


class BinaryReferenceGuardTests(unittest.TestCase):
    def test_binary_executable_reference_does_not_crash(self):
        """A command naming a real binary must not crash and must not block."""
        python = sys.executable  # a real Mach-O/ELF binary
        command = f"{python} -m some.module --flag"
        result = contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(Path(__file__).parent)
        )
        self.assertFalse(result)

    def test_binary_contents_do_not_reenter_walker_as_text(self):
        """Binary bytes decoded by a reader must not yield NUL tokens again."""
        binary_dir = Path(__file__).resolve().parent / "_lifeguard_bin_fixture"
        binary_dir.mkdir(exist_ok=True)
        try:
            binary_path = binary_dir / "fake-macho"
            binary_path.write_bytes(
                b"\xcf\xfa\xed\xfe\x00\x00\x00\x01"
                b"/bin/hermes\x00 gateway restart \x00"
            )
            # Simulate what _read_script_in_env used to produce: decoded
            # binary text handed back to the guard.
            decoded = binary_path.read_bytes().decode("utf-8", errors="replace")
            result = contains_gateway_lifecycle_command_or_referenced_script(
                f"/bin/sh {binary_path}", cwd=str(binary_dir)
            )
            self.assertFalse(result)
            # The old crash: Path(token).expanduser() on a NUL-bearing token.
            for token in decoded.split("\x00"):
                if token:
                    _resolve_terminal_script_path(token.strip(), cwd=str(binary_dir))
        finally:
            (binary_dir / "fake-macho").unlink(missing_ok=True)
            binary_dir.rmdir()

    def test_real_lifecycle_script_still_blocked(self):
        """Guard still blocks genuine restart scripts after the fix."""
        script_dir = Path(__file__).resolve().parent / "_lifeguard_bin_fixture"
        script_dir.mkdir(exist_ok=True)
        script = script_dir / "restart.sh"
        try:
            script.write_text("hermes gateway restart\n")
            result = contains_gateway_lifecycle_command_or_referenced_script(
                f"/bin/bash {script}", cwd=str(script_dir)
            )
            self.assertTrue(result)
        finally:
            script.unlink(missing_ok=True)
            script_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
