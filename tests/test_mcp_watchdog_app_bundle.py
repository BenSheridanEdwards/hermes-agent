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
import subprocess
import sys
import unittest
from unittest import mock

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import _watchdog_interpreter, _wrap_command_with_watchdog

WATCHDOG_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py")
)

BUNDLED = "/Tools/tcc-identity/apps/Doc.app/Contents/MacOS/Doc"


def _bundled_env(base_prefix, isfile, access=True):
    """Patch context bundle: sys.executable inside a .app + fake base_prefix."""
    return (
        mock.patch.object(sys, "executable", BUNDLED),
        mock.patch.object(sys, "base_prefix", base_prefix),
        mock.patch("os.path.isfile", side_effect=isfile),
        mock.patch("os.access", return_value=access),
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
        with context_stack(_bundled_env(fake_base, lambda p: p.endswith("/python3"))):
            self.assertEqual(_watchdog_interpreter(), fake_sibling)

    def test_bundled_prefers_python3_when_both_exist(self):
        """Advisor test 2: with BOTH siblings present, python3 must win."""
        fake_base = "/fake/base"
        chosen = {}

        def record(p):
            chosen["path"] = p
            return p.endswith("/python3") or p.endswith("/python")

        with context_stack(_bundled_env(fake_base, record)):
            _watchdog_interpreter()
        self.assertEqual(chosen["path"], os.path.join(fake_base, "bin", "python3"))

    def test_bundled_prefers_python_over_python(self):
        """When only the bare `python` sibling exists, it is chosen."""
        fake_base = "/fake/base"
        with context_stack(
            _bundled_env(
                fake_base,
                lambda p: p.endswith("/python") and not p.endswith("/python3"),
            )
        ):
            self.assertEqual(
                _watchdog_interpreter(),
                os.path.join(fake_base, "bin", "python"),
            )

    def test_bundled_nonexecutible_sibling_skipped(self):
        """Advisor test 3a: sibling exists but X_OK fails -> fall through."""
        bundled = BUNDLED
        fake_base = "/fake/base"
        with context_stack(
            (
                mock.patch.object(sys, "executable", bundled),
                mock.patch.object(sys, "base_prefix", fake_base),
                mock.patch("os.path.isfile", return_value=True),
                mock.patch("os.access", return_value=False),
            )
        ):
            self.assertEqual(_watchdog_interpreter(), bundled)

    def test_bundled_without_sibling_falls_back(self):
        """No usable sibling: legacy behavior (warn once + sys.executable)."""
        bundled = BUNDLED
        with context_stack(
            _bundled_env(bundled, lambda p: False)
        ):
            self.assertEqual(_watchdog_interpreter(), bundled)

    def test_fallback_warning_emitted_once(self):
        """Advisor nonblocking 3: the fallback warning fires only once."""
        bundled = BUNDLED
        with context_stack(_bundled_env(bundled, lambda p: False)):
            with mock.patch.object(mcp_tool.logger, "warning") as warn:
                mcp_tool._warned_bundled_watchdog_fallback = False
                _watchdog_interpreter()
                _watchdog_interpreter()
                _watchdog_interpreter()
        self.assertEqual(warn.call_count, 1)

    def test_case_insensitive_bundle_detection(self):
        """Advisor nonblocking 2: .App/Contents/macos/ also matches."""
        odd = "/Tools/tcc-identity/apps/Sky.App/Contents/macos/Sky"
        fake_base = "/fake/base"
        with mock.patch.object(sys, "executable", odd), \
             mock.patch.object(sys, "base_prefix", fake_base), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.access", return_value=True):
            self.assertEqual(
                _watchdog_interpreter(),
                os.path.join(fake_base, "bin", "python3"),
            )

    def test_bundled_empty_base_prefix_falls_back(self):
        bundled = BUNDLED
        with mock.patch.object(sys, "executable", bundled), \
             mock.patch.object(sys, "base_prefix", ""), \
             mock.patch.object(sys, "prefix", ""):
            self.assertEqual(_watchdog_interpreter(), bundled)

    def test_wrap_command_uses_watchdog_interpreter(self):
        """End-to-end: wrapped argv runs the watchdog via the re-homed python."""
        fake_sibling = "/fake/base/bin/python3"
        real_command = "/usr/local/bin/some-mcp-server"
        real_args = ["--flag"]
        posix_case = (
            mock.patch.object(sys, "executable", BUNDLED),
            mock.patch.object(sys, "base_prefix", "/fake/base"),
            mock.patch.object(os, "name", "posix"),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("os.access", return_value=True),
            mock.patch.object(mcp_tool.os, "getpid", return_value=4242),
        )
        with context_stack(posix_case):
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

    @unittest.skipUnless(
        os.environ.get("HERMES_TEST_REAL_BOOT") == "1",
        "real-boot check needs the fleet interpreter layout; "
        "set HERMES_TEST_REAL_BOOT=1",
    )
    def test_real_boot_under_sibling_interpreter(self):
        """Advisor blocking 3 / missing-test 1: the ACTUAL incident shape.

        Boots the watchdog under the out-of-bundle uv CPython with a fully
        stripped environment — exactly what gateway children receive. A
        wrong-but-plausible sibling (non-bootable binary, wrong prefix) fails
        this while passing every mocked test above.
        """
        base_prefix = getattr(sys, "base_prefix", "") or sys.prefix
        sibling = os.path.join(base_prefix, "bin", "python3")
        if not os.path.isfile(sibling):
            self.skipTest(f"no sibling interpreter at {sibling}")
        result = subprocess.run(
            [
                sibling,
                WATCHDOG_SCRIPT,
                "--ppid", str(os.getpid()),
                "--",
                "/bin/echo",
                "BOOT-OK",
            ],
            env={},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOOT-OK", result.stdout)

    def test_wrap_command_non_posix_noop(self):
        """Non-POSIX platforms keep the no-op passthrough."""
        with mock.patch.object(os, "name", "nt"):
            command = "/bin/tool"
            args = ["--x"]
            self.assertEqual(
                _wrap_command_with_watchdog(command, list(args)),
                (command, args),
            )


def context_stack(patchers):
    import contextlib
    stack = contextlib.ExitStack()
    for patcher in patchers:
        stack.enter_context(patcher)
    return stack


if __name__ == "__main__":
    unittest.main()
