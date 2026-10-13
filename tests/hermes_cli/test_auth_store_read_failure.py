"""A transient read failure on auth.json must not degrade to an empty store.

``_load_auth_store`` treated every exception as corruption and returned
``{"version": ..., "providers": {}}``. This module does read-modify-write in
roughly fifteen places, so an ``OSError`` (EMFILE under fd exhaustion, EACCES,
EIO, a stalled mount) followed by any ``_save_auth_store`` rewrote auth.json
with an empty provider set and destroyed every stored credential.

Present corruption now also fails closed, preserving the original bytes.
Neither empty-store recovery nor extra credential copies are implicit repairs.
"""

import errno
import json
import logging

import pytest

import hermes_cli.auth as auth
from agent.credential_policy import CredentialPolicyError


@pytest.fixture
def store_file(tmp_path):
    f = tmp_path / "auth.json"
    f.write_text(
        json.dumps({"version": 1, "providers": {"nous": {"api_key": "secret"}}}),
        encoding="utf-8",
    )
    return f


def _fail_read(exc):
    def _read(self, *args, **kwargs):
        raise exc
    return _read


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.EMFILE, "Too many open files"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
    ],
    ids=["emfile", "eacces", "eio"],
)
def test_read_failure_raises_and_leaves_the_store_alone(store_file, monkeypatch, exc):
    from pathlib import Path

    before = store_file.read_bytes()
    monkeypatch.setattr(Path, "read_text", _fail_read(exc))

    with pytest.raises(CredentialPolicyError, match="Credential store is invalid or unreadable"):
        auth._load_auth_store(store_file)

    assert store_file.read_bytes() == before, "the store on disk was modified"
    assert not store_file.with_suffix(".json.corrupt").exists(), (
        "a read failure is not corruption and must not write a .corrupt sidecar"
    )


def test_unparseable_json_fails_closed_and_preserves_the_original(store_file):
    store_file.write_text("{ not json", encoding="utf-8")

    with pytest.raises(CredentialPolicyError, match="Credential store is invalid or unreadable"):
        auth._load_auth_store(store_file)
    corrupt = store_file.with_suffix(".json.corrupt")
    assert not corrupt.exists(), "a consumer must not create another credential holder"
    assert store_file.read_text(encoding="utf-8") == "{ not json"


def test_healthy_store_is_returned_unchanged(store_file):
    result = auth._load_auth_store(store_file)
    assert result["providers"]["nous"]["api_key"] == "secret"


def test_log_does_not_claim_a_backup_that_was_not_written(
    store_file, monkeypatch, caplog
):
    """The old message advertised the .corrupt path even when copy2 failed."""
    import shutil

    store_file.write_text("{ not json", encoding="utf-8")

    def _no_copy(*args, **kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(shutil, "copy2", _no_copy)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.auth"):
        with pytest.raises(CredentialPolicyError):
            auth._load_auth_store(store_file)

    assert store_file.read_text(encoding="utf-8") == "{ not json"
    assert not store_file.with_suffix(".json.corrupt").exists()
    text = caplog.text
    assert "secret" not in text
    assert "Corrupt file preserved at" not in text
