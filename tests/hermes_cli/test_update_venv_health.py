"""Tests for the Windows half-updated-venv hardening (July 2026 incident).

Covers three additions to ``hermes update``:

1. ``_venv_core_imports_healthy`` — the venv health probe that lets an
   "Already up to date" checkout still repair a broken dependency install.
2. ``_detect_venv_python_processes`` — the venv-interpreter process guard
   that refuses to mutate the venv while a desktop backend / stray python
   holds .pyd files mapped.
3. The commit_count == 0 repair branch wiring in ``_cmd_update_impl``.

All Windows-specific paths are exercised via ``_is_windows`` patching so
they run on any host (same approach as test_update_concurrent_quarantine).
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# NOTE: deliberately no module-level ``from hermes_cli import main as
# cli_main``. Everything here goes through ``_live_main()`` — see its
# docstring for the incident that binding caused.


def _live_main():
    """Resolve ``hermes_cli.main`` the way production code does, at CALL time.

    ``from hermes_cli import main as cli_main`` at module scope binds the
    module object that existed when the file was *collected*. That binding can
    go stale:
    ``tests/hermes_cli/test_skills_subparser.py`` deletes
    ``sys.modules['hermes_cli.main']`` and re-imports it, so every test file
    collected before it keeps an alias to a module object nothing in
    production resolves any more.

    Production code reaches its own globals through
    ``hermes_cli.update_cmd._m()``, which imports ``hermes_cli.main``
    *freshly*. Patching the stale alias therefore silently no-ops: the guard
    stubs below never take effect and ``_cmd_update_impl`` runs the REAL
    update flow — ``git fetch origin main`` → ``git checkout main`` →
    ``git merge --ff-only origin/main`` — against whatever checkout pytest
    happens to be running in. That is exactly how a staging worktree was
    flipped onto ``main`` underneath two live gateways (2026-08-05).

    Always patch the object this returns, never a module-scope alias.

    Resolution deliberately mirrors ``_m()`` exactly — ``from hermes_cli
    import main`` reads the *package attribute*, which a re-import rebinds
    independently of the ``sys.modules`` entry. ``importlib.import_module``
    reads ``sys.modules`` and could hand back a different object again.
    """
    from hermes_cli import main as live_main

    return live_main


class _NoSubprocess:
    """Stand-in for ``update_cmd.subprocess`` — every entry point explodes.

    A correctly-stubbed ``_cmd_update_impl`` run reaches the venv-holder
    guard (``SystemExit(2)``) or the ``PROJECT_ROOT`` sentinel *before* it
    shells out to anything. So any subprocess call from this file means the
    stubs missed their seam, and the test must fail loudly rather than quietly
    running a real ``hermes update`` against the developer's checkout.
    """

    def __getattr__(self, name):
        def _blocked(*args, **kwargs):
            raise AssertionError(
                f"test_update_venv_health: subprocess.{name}({args!r}) escaped "
                "the stubs in _run_update_until_guard — _cmd_update_impl got "
                "past the venv-holder guard with unpatched module globals. "
                "See _live_main() for the stale-alias failure mode."
            )

        return _blocked


# ---------------------------------------------------------------------------
# _venv_core_imports_healthy
# ---------------------------------------------------------------------------




def _fake_venv_python(tmp_path, *, windows: bool = False):
    bin_dir = tmp_path / "venv" / ("Scripts" if windows else "bin")
    bin_dir.mkdir(parents=True)
    py = bin_dir / ("python.exe" if windows else "python")
    py.write_bytes(b"")
    return py




# ---------------------------------------------------------------------------
# _detect_venv_python_processes
# ---------------------------------------------------------------------------


def _proc(pid: int, exe: str, name: str, cmdline: list[str] | None = None, cwd: str = ""):
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "exe": exe,
        "name": name,
        "cmdline": cmdline or [],
        "cwd": cwd,
    }
    return proc




def test_detect_venv_python_excludes_self_and_ancestors(tmp_path):
    import os as _os

    venv_py = str(tmp_path / "venv" / "Scripts" / "python.exe")
    parent = MagicMock()
    parent.pid = 555
    me = MagicMock()
    me.parents.return_value = [parent]
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: iter(
            [
                _proc(_os.getpid(), venv_py, "python.exe"),
                _proc(555, venv_py, "hermes.exe"),
            ]
        ),
        Process=lambda *a, **k: me,
    )
    main_mod = _live_main()
    with patch.object(main_mod, "_is_windows", return_value=True), patch.object(
        main_mod, "PROJECT_ROOT", tmp_path
    ), patch.dict(sys.modules, {"psutil": fake_psutil}):
        assert main_mod._detect_venv_python_processes() == []




# ---------------------------------------------------------------------------
# --force vs --force-venv gating of the venv-holder guard
# ---------------------------------------------------------------------------


def _update_args(**overrides):
    defaults = dict(
        gateway=False,
        check=False,
        no_backup=True,
        backup=False,
        yes=True,
        branch=None,
        force=False,
        force_venv=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _run_update_until_guard(args):
    """Drive _cmd_update_impl just far enough to hit the venv-holder guard.

    Everything before the guard is stubbed; the guard firing is observed via
    SystemExit(2). The first statement AFTER the guard is
    ``git_dir = PROJECT_ROOT / ".git"`` — a PROJECT_ROOT sentinel whose
    ``__truediv__`` raises marks 'guard passed'.

    The stubs are applied to ``_live_main()`` — the module object production
    code actually resolves — not to the import-time ``cli_main`` alias, and
    ``update_cmd.subprocess`` is swapped for a tripwire. Between them, a stub
    that misses its seam fails the test instead of shelling out to real git.
    """
    main_mod = _live_main()
    update_cmd = importlib.import_module("hermes_cli.update_cmd")

    class _PastGuard(Exception):
        pass

    class _RootSentinel:
        def __truediv__(self, _other):
            raise _PastGuard

    with patch.object(main_mod, "_is_windows", return_value=True), patch.object(
        main_mod, "_venv_scripts_dir", return_value=None
    ), patch.object(main_mod, "_run_pre_update_backup"), patch.object(
        main_mod, "_pause_windows_gateways_for_update", return_value=None
    ), patch.object(
        main_mod, "_resume_windows_gateways_after_update"
    ), patch.object(
        main_mod,
        "_detect_venv_python_processes",
        return_value=[(101, "python.exe", "python.exe -m hermes_cli.main serve")],
    ), patch.object(
        # Pin the orphan classifier: this test exercises --force/--force-venv
        # gating, not orphan detection (covered in
        # test_update_orphan_backend_reap.py). None = "not provably orphaned"
        # → the guard refuses exactly as before the orphan-reap addition.
        cli_main, "_orphaned_desktop_backend_pids", return_value=None
    ), patch.object(
        cli_main, "PROJECT_ROOT", _RootSentinel()
    ), patch.object(
        update_cmd, "subprocess", _NoSubprocess()
    ):
        try:
            main_mod._cmd_update_impl(args, gateway_mode=False)
        except _PastGuard:
            return "past_guard"
        except SystemExit as exc:
            return f"exit_{exc.code}"
    return "returned"


@pytest.mark.parametrize(
    "force,force_venv,expected",
    [
        (False, False, "exit_2"),   # guard fires
        (True, False, "exit_2"),    # plain --force does NOT bypass the venv guard
        (False, True, "past_guard"),  # --force-venv is the explicit escape hatch
        (True, True, "past_guard"),
    ],
)
def test_venv_holder_guard_force_semantics(force, force_venv, expected, capsys):
    result = _run_update_until_guard(_update_args(force=force, force_venv=force_venv))
    assert result == expected, capsys.readouterr().out
