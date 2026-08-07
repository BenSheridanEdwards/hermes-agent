"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse
import sys


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.11+ crash.

    Python 3.11 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.

    The fresh import MUST be undone afterwards. Every test file collected
    before this one holds a ``from hermes_cli import main as cli_main``
    binding to the *pre-delete* module object. Leaving a different object in
    ``sys.modules`` splits the suite in two: those aliases patch one module
    while production code (``update_cmd._m()``) resolves the other, so
    ``patch.object(cli_main, ...)`` silently no-ops for the rest of the
    session. That is how test_update_venv_health.py came to run the real
    ``hermes update`` flow — fetch, checkout main, ff-merge — against the
    live checkout (fleet incident 2026-08-05).
    """
    # Force fresh import of the module where parser is constructed.
    # If there are duplicate 'skills' subparsers, this import will raise
    # argparse.ArgumentError at module load time.
    original = sys.modules.pop("hermes_cli.main", None)

    try:
        import hermes_cli.main  # noqa: F401
    except argparse.ArgumentError as e:
        if "conflicting subparser" in str(e):
            raise AssertionError(
                f"Duplicate subparser detected: {e}. "
                "See issue #898 for details."
            ) from e
        raise
    finally:
        # Restore module IDENTITY, not just presence: the re-imported object
        # above is a different object, and handing it back to the suite is
        # the state leak described in the docstring.
        #
        # BOTH bindings have to go back. Importing a submodule rebinds the
        # attribute on its package too, and ``from hermes_cli import main``
        # — the form production code uses in ``update_cmd._m()`` — reads that
        # package attribute, not ``sys.modules``. Restoring only the
        # ``sys.modules`` entry leaves the two pointing at different objects,
        # which is the same split by another route.
        if original is not None:
            sys.modules["hermes_cli.main"] = original
            setattr(sys.modules["hermes_cli"], "main", original)
