"""Regression tests: MCP watchdog must not spawn under .app-bundled interpreters.

Background (2026-08-24, Doc): the fleet's per-agent gateway identity bundles
(``~/Tools/tcc-identity/apps/<Name>.app``) contain a bare CPython copy with no
``pyvenv.cfg``. Such a binary boots only when ``PYTHONHOME``/``PYTHONPATH`` are
preset; the gateway's sitecustomize deliberately strips those for children.
``_wrap_command_with_watchdog`` used to return ``sys.executable`` verbatim, so
every stdio MCP server died at interpreter init ("Fatal Python error:
init_fs_encoding … No module named 'encodings'") and parked.
"""

import os
import sys
import unittest
from unittest import mock

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import _watchdog_interpreter, _wrap_command_with_watchdog

WATCHDOG_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py")
)


class WatchdogInterpreterTests(unittest.TestCase):
    def test_plain_executable_passthrough(self):
        """A normal (non-bundled) executable is returned unchanged."""
        with mock.patch.object(sys, "executable", "/usr/bin/python3"):
            self.assertEqual(_watchdog_interpreter(), "/usr/bin/python3")

    def test_venv_executable_passthrough(self):
        """A venv python (has pyvenv.cfg beside it) is returned unchanged."""
        venv_python = "/Users/x/.venvs/hermes/bin/python"
        with mock.patch.object(sys, "executable", venv_python):
            self.assertEqual(_watchdog_interpreter(), venv_python)

    def test_bundled_executable_rehomed_to_sibling(self):
        """An .app-bundled interpreter re-homes to base_prefix/bin/python3."""
        fake_base = "/fake/uv/cpython-3.12-macos-aarch64-none"
        fake_sibling = os.path.join(fake_base, "bin", "python3")
        bundled = "/Tools/tcc-identity/apps/Doc.app/Contents/MacOS/Doc"
        with mock.patch.object(sys, "executable", bundled), \
             mock.patch.object(sys, "base_prefix", fake_base), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.access", return_value=True):
            self.assertEqual(_watchdog_interpreter(), fake_sibling)

    def test_bundled_prefers_python3_over_python(self):
        """When only the bare `python` sibling exists, it is chosen."""
        fake_base = "/fake/base"
        bundled = "/Tools/tcc-identity/apps/Bond.app/Contents/MacOS/Bond"
        with mock.patch.object(sys, "executable", bundled), \
             mock.patch.object(sys, "base_prefix", fake_base), \
             mock.patch(
                 "os.path.isfile",
                 side_effect=lambda p: p.endswith("/python"),
             ), \
             mock.patch("os.access", return_value=True):
            self.assertEqual(
                _watchdog_interpreter(),
                os.path.join(fake_base, "bin", "python"),
            )

    def test_bundled_without_sibling_falls_back(self):
        """No usable sibling: legacy behavior (warn + sys.executable)."""
        bundled = "/Tools/tcc-identity/apps/Sky.app/Contents/MacOS/Sky"
        with mock.patch.object(sys, "executable", bundled), \
             mock.patch.object(sys, "base_prefix", "/fake/base"), \
             mock.patch("os.path.isfile", return_value=False):
            self.assertEqual(_watchdog_interpreter(), bundled)

    def test_bundled_empty_base_prefix_falls_back(self):
        bundled = "/Tools/tcc-identity/apps/Jeeves.app/Contents/MacOS/Jeeves"
        with mock.patch.object(sys, "executable", bundled), \
             mock.patch.object(sys, "base_prefix", ""), \
             mock.patch.object(sys, "prefix", ""):
            self.assertEqual(_watchdog_interpreter(), bundled)

    def test_wrap_command_uses_watchdog_interpreter(self):
        """End-to-end: wrapped argv runs the watchdog via the re-homed python."""
        fake_sibling = "/fake/base/bin/python3"
        real_command = "/usr/local/bin/some-mcp-server"
        real_args = ["--flag"]
        bundled = "/Tools/tcc-identity/apps/Doc.app/Contents/MacOS/Doc"
        posix_case = (
            mock.patch.object(sys, "executable", bundled),
            mock.patch.object(sys, "base_prefix", "/fake/base"),
            mock.patch.object(os, "name", "posix"),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("os.access", return_value=True),
            mock.patch.object(mcp_tool.os, "getpid", return_value=4242),
        )
        with contextlib_exit_stack(posix_case):
            wrapped_command, wrapped_args = _wrap_command_with_watchdog(
                real_command, list(real_args)
            )
        self.assertEqual(wrapped_command, fake_sibling)
        # argv shape: <watchdog.py> --ppid 4242 -- <real command> <real args>
        self.assertEqual(wrapped_args[0], WATCHDOG_SCRIPT)
        self.assertEqual(wrapped_args[1:3], ["--ppid", "4242"])
        separator_index = wrapped_args.index("--")
        self.assertEqual(
            wrapped_args[separator_index + 1:], [real_command] + real_args
        )
        self.assertTrue(os.path.exists(WATCHDOG_SCRIPT))

    def test_wrap_command_non_posix_noop(self):
        """Non-POSIX platforms keep the no-op passthrough."""
        with mock.patch.object(os, "name", "nt"):
            command = "/bin/tool"
            args = ["--x"]
            self.assertEqual(
                _wrap_command_with_watchdog(command, list(args)),
                (command, args),
            )


def contextlib_exit_stack(patchers):
    import contextlib
    stack = contextlib.ExitStack()
    for patcher in patchers:
        stack.enter_context(patcher)
    return stack


if __name__ == "__main__":
    unittest.main()
