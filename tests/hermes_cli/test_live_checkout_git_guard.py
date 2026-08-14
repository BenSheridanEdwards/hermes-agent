"""The test suite must never run repo-mutating git against its own checkout.

Regression tests for the conftest git guard added after the 2026-08-05
staging-worktree flip: a full-suite run inside a linked worktree (two live
gateways served from its editable venv) reached the real ``_cmd_update_impl``
and executed ``git fetch origin main`` → ``git checkout main`` →
``git merge --ff-only origin/main``, moving the checkout off its release
branch while the tests still reported green.

The guard is a *deny-list on write*: read-only git against the live checkout
stays allowed (``banner``/``diff``/``dashboard`` tests genuinely shell out to
it), and repos under ``tmp_path`` are never touched by the guard at all.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import _LIVE_CHECKOUT_ROOT, _git_guard_violation


class TestPredicate:
    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "fetch", "origin", "main"],
            ["git", "checkout", "main"],
            ["git", "merge", "--ff-only", "origin/main"],
            ["git", "pull", "--ff-only", "upstream", "main"],
            ["git", "reset", "--hard", "origin/main"],
            ["git", "stash", "push", "-u"],
            ["git", "clean", "-fd"],
            ["git", "config", "windows.appendAtomically", "false"],
            ["git", "branch", "-D", "runtime/fleet-20260805"],
            ["git", "-c", "windows.appendAtomically=false", "fetch", "origin"],
        ],
    )
    def test_blocks_mutating_git_at_live_checkout(self, argv):
        violation = _git_guard_violation(argv, cwd=str(_LIVE_CHECKOUT_ROOT))
        assert violation is not None, f"{argv} was allowed to write the checkout"
        assert str(_LIVE_CHECKOUT_ROOT) in violation

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "rev-parse", "--short=8", "HEAD"],
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            ["git", "describe", "--tags", "--abbrev=0"],
            ["git", "status", "--porcelain"],
            ["git", "diff", "--name-only"],
            ["git", "log", "--format=%H", "-n20"],
            ["git", "config", "--get", "core.autocrlf"],
            ["git", "remote", "get-url", "origin"],
            ["git", "branch", "--show-current"],
            ["git", "worktree", "list", "--porcelain"],
            ["git", "--version"],
        ],
    )
    def test_allows_read_only_git_at_live_checkout(self, argv):
        assert _git_guard_violation(argv, cwd=str(_LIVE_CHECKOUT_ROOT)) is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "init", "-b", "main"],
            ["git", "commit", "-m", "init"],
            ["git", "worktree", "add", "wt", "HEAD"],
            ["git", "fetch", "origin", "main"],
        ],
    )
    def test_ignores_sandboxed_repos(self, argv, tmp_path):
        """tmp_path fixtures keep doing real git — that is the whole point."""
        assert _git_guard_violation(argv, cwd=str(tmp_path)) is None

    def test_uses_dash_c_target_not_cwd(self, tmp_path):
        """``git -C <tmp> commit`` from the checkout writes to <tmp>, not here."""
        assert _git_guard_violation(
            ["git", "-C", str(tmp_path), "commit", "-m", "x"],
            cwd=str(_LIVE_CHECKOUT_ROOT),
        ) is None
        # ...and the reverse: -C pointing back INTO the checkout is blocked.
        assert _git_guard_violation(
            ["git", "-C", str(_LIVE_CHECKOUT_ROOT), "checkout", "main"],
            cwd=str(tmp_path),
        ) is not None

    def test_uses_destination_for_clone_and_init(self, tmp_path):
        """clone/init write to their positional destination, not the cwd."""
        assert _git_guard_violation(
            ["git", "clone", "http://127.0.0.1/x.git", str(tmp_path / "dest")],
            cwd=str(_LIVE_CHECKOUT_ROOT),
        ) is None
        assert _git_guard_violation(
            ["git", "init", "-q", str(tmp_path / "repo")],
            cwd=str(_LIVE_CHECKOUT_ROOT),
        ) is None

    def test_init_dash_b_main_is_not_a_destination(self, tmp_path):
        """`git init -b main` names a branch, not a path under the checkout."""
        assert _git_guard_violation(
            ["git", "-C", str(tmp_path), "init", "-q", "-b", "main"],
            cwd=str(_LIVE_CHECKOUT_ROOT),
        ) is None
        assert _git_guard_violation(
            ["git", "-C", str(tmp_path), "init", "-q"],
            cwd=str(_LIVE_CHECKOUT_ROOT),
        ) is None
        assert _git_guard_violation(
            ["git", "init", "-b", "main"],
            cwd=str(tmp_path),
        ) is None

    def test_defaults_to_process_cwd_when_unset(self, monkeypatch):
        """A bare ``subprocess.run(["git", ...])`` inherits pytest's cwd."""
        monkeypatch.chdir(_LIVE_CHECKOUT_ROOT)
        assert _git_guard_violation(["git", "fetch", "origin", "main"]) is not None

    def test_shell_string_form_is_parsed(self):
        assert _git_guard_violation(
            "git checkout main", cwd=str(_LIVE_CHECKOUT_ROOT)
        ) is not None

    def test_non_git_commands_are_not_the_guards_business(self):
        assert _git_guard_violation(
            ["echo", "git", "checkout", "main"], cwd=str(_LIVE_CHECKOUT_ROOT)
        ) is None


class TestWiredIntoSubprocess:
    """The predicate is only useful if the autouse fixture actually applies it."""

    def test_subprocess_run_is_blocked(self):
        with pytest.raises(RuntimeError, match="live-checkout git guard"):
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=str(_LIVE_CHECKOUT_ROOT),
                capture_output=True,
            )

    def test_popen_is_blocked(self):
        with pytest.raises(RuntimeError, match="live-checkout git guard"):
            subprocess.Popen(
                ["git", "checkout", "main"], cwd=str(_LIVE_CHECKOUT_ROOT)
            )

    def test_read_only_call_still_reaches_real_git(self):
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(_LIVE_CHECKOUT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip()
