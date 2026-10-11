"""Recipient-safe credential management for an external credential manager (Fleet Access #2134).

I1: the stable principal Hermes observes on OAuth tokens (chatgpt_account_id only; unknown stays unknown) and the
    additive per-row ``expect`` constraint enforced at use time by ``CredentialPolicy.rows()``.
I2: ``hermes auth reauth --expect-principal/--result-file/--cancel-file``: a different account, a cancellation at
    commit or a vanished row writes nothing; only ``committed`` exits 0; the result file is structured and non-secret.
I3: ``bounded_operation`` records the transport result of the runtime's own request (a local controlled provider),
    bound to policy revision, entry, observed principal and runtime instance.

Same harness as test_auth_reauth.py: production parser + dispatch, temp HERMES_HOME, fake device-code login.
"""
import argparse
import base64
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod
from agent.credential_identity import RUNTIME_INSTANCE, bounded_operation, observed_principal
from hermes_cli import auth_commands
from hermes_cli.subcommands.auth import build_auth_parser


def _jwt(claims: dict) -> str:
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def _token(account: str) -> str:
    return _jwt({"sub": "user-" + account, "email": f"{account}@example.invalid",
                 "https://api.openai.com/auth": {"chatgpt_account_id": account}})


@pytest.fixture(autouse=True)
def isolated_external_auth_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))


def _run_cli(*argv: str) -> None:
    parser = argparse.ArgumentParser(prog="hermes")
    build_auth_parser(parser.add_subparsers(dest="command"), cmd_auth=auth_commands.auth_command)
    args = parser.parse_args(["auth", *argv])
    args.func(args)


def _auth_path() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "auth.json"


def _store(rows):
    path = _auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": rows}}), encoding="utf-8")


def _rows():
    return json.loads(_auth_path().read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]


def _row(row_id, account, **extra):
    return {"id": row_id, "label": row_id, "auth_type": "oauth", "priority": 0, "source": "manual:device_code",
            "access_token": _token(account), "refresh_token": "r-" + row_id, "base_url": "https://example.invalid/codex", **extra}


def _login_as(account: str, before_return=None):
    def login(*_args, **_kwargs):
        if before_return:
            before_return()
        return {"tokens": {"access_token": _token(account), "refresh_token": "new-r-" + account},
                "base_url": "https://example.invalid/codex", "last_refresh": "2026-10-11T00:00:00Z"}
    return login


def test_observed_principal_is_the_stable_claim_only_and_unknown_stays_unknown():
    assert observed_principal("openai-codex", {"access_token": _token("acct-1")}) == {
        "kind": "chatgpt_account_id", "value": "acct-1", "source": "access_token_claim"}
    # sub / email are never promoted to the stable key.
    assert observed_principal("openai-codex", {"access_token": _jwt({"sub": "u", "email": "e@x"})}) is None
    # Providers without a supported stable claim report unknown, whatever the token carries.
    assert observed_principal("xai-oauth", {"access_token": _token("acct-1")}) is None
    assert observed_principal("anthropic", {"access_token": "sk-ant-oat-x"}) is None


def test_reauth_commits_only_the_expected_account_and_persists_its_principal(tmp_path, monkeypatch):
    _store([_row("personal", "acct-personal"), _row("walnut", "acct-walnut")])
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _login_as("acct-personal"))
    result = tmp_path / "result.json"
    _run_cli("reauth", "openai-codex", "personal", "--expect-principal", "chatgpt_account_id=acct-personal",
             "--result-file", str(result), "--cancel-file", str(tmp_path / "cancel"))
    outcome = json.loads(result.read_text())
    assert (outcome["version"], outcome["status"], outcome["row_id"], outcome["principal"]) == (
        1, "committed", "personal", {"kind": "chatgpt_account_id", "value": "acct-personal"})
    assert "token" not in json.dumps(outcome).lower().replace("row_id", "")  # non-secret result
    personal = next(r for r in _rows() if r["id"] == "personal")
    assert personal["principal"]["value"] == "acct-personal"
    assert next(r for r in _rows() if r["id"] == "walnut")["access_token"] == _token("acct-walnut"), "other rows untouched"


def test_reauth_refuses_a_different_account_under_the_same_row_id_client_and_scope(tmp_path, monkeypatch):
    _store([_row("personal", "acct-personal", client_id="app", scope="openid")])
    before = _auth_path().read_text()
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _login_as("acct-intruder"))
    result = tmp_path / "result.json"
    with pytest.raises(SystemExit) as exit_info:
        _run_cli("reauth", "openai-codex", "personal", "--expect-principal", "chatgpt_account_id=acct-personal",
                 "--result-file", str(result))
    assert exit_info.value.code not in (0, None)
    assert json.loads(result.read_text())["status"] == "rejected"
    assert _auth_path().read_text() == before, "nothing was written"


def test_reauth_cancellation_at_commit_writes_nothing(tmp_path, monkeypatch):
    _store([_row("personal", "acct-personal")])
    before = _auth_path().read_text()
    cancel = tmp_path / "cancel"
    # The cancel request lands after the sign-in finished but before the commit critical section.
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _login_as("acct-personal", before_return=lambda: cancel.write_text("x")))
    result = tmp_path / "result.json"
    with pytest.raises(SystemExit):
        _run_cli("reauth", "openai-codex", "personal", "--expect-principal", "chatgpt_account_id=acct-personal",
                 "--result-file", str(result), "--cancel-file", str(cancel))
    assert json.loads(result.read_text())["status"] == "cancelled"
    assert _auth_path().read_text() == before


def test_reauth_without_recipient_options_keeps_the_original_behaviour(monkeypatch):
    _store([_row("personal", "acct-personal")])
    monkeypatch.setattr(auth_mod, "_codex_device_code_login", _login_as("acct-other"))
    _run_cli("reauth", "openai-codex", "personal")  # no expectation: renewal proceeds as before
    assert next(r for r in _rows() if r["id"] == "personal")["principal"]["value"] == "acct-other"


class _Provider(http.server.BaseHTTPRequestHandler):
    accept = {"good-key"}
    seen = []

    def do_POST(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        type(self).seen.append((self.path, auth, self.headers.get("ChatGPT-Account-Id")))
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        accepted = auth.removeprefix("Bearer ") in type(self).accept
        self.send_response(200 if accepted else 401)
        self.send_header("Content-Type", "text/event-stream" if accepted else "application/json")
        self.end_headers()
        event = {"type": "response.completed", "response": {"id": "controlled", "object": "response", "status": "completed",
                 "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}]}}
        self.wfile.write(("event: response.completed\ndata: " + json.dumps(event) + "\n\n").encode() if accepted else b"{}")

    def log_message(self, *_args):
        pass


@pytest.fixture
def provider():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Provider.seen.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_bounded_operation_records_the_transport_result_bound_to_revision_entry_principal_and_instance(tmp_path, provider):
    from agent.credential_policy import load_policy, select_managed_runtime
    home = Path(os.environ["HERMES_HOME"])
    access = tmp_path / "access"
    (access / "assignments").mkdir(parents=True)
    gate = access / "authority-publication.json"
    gate.write_text(json.dumps({"version": 1, "state": "committed", "revision": 1}))
    manifest = access / "assignments" / "recipient.json"
    manifest.write_text(json.dumps({"version": 2, "publication": {"file": str(gate), "revision": 1},
        "environment": {}, "source_bindings": {}, "managed_environment": [],
        "accounts": {"openai-codex": {"store": "profile", "ids": ["personal"], "expect": {
            "personal": {"principal": {"kind": "chatgpt_account_id", "value": "acct-personal"}}}}}}))
    _store([_row("personal", "acct-personal", base_url=provider)])
    (home / "config.yaml").write_text(f"model:\n  provider: openai-codex\n  default: controlled-model\ncredential_policy:\n  file: {manifest}\n  strict: true\n")
    policy = load_policy(home)
    assert policy is not None
    resolved, entry, runtime = select_managed_runtime(policy, "openai-codex")
    token = _token("acct-personal")
    _Provider.accept = {token}
    accepted = bounded_operation(policy, resolved, entry, runtime)
    assert (accepted["status"], accepted["http_status"], accepted["entry"], accepted["revision"], accepted["instance"]) == (
        "accepted", 200, "personal", policy.revision, RUNTIME_INSTANCE)
    assert accepted["principal"] == {"kind": "chatgpt_account_id", "value": "acct-personal"}
    assert _Provider.seen[-1] == ("/responses", f"Bearer {token}", "acct-personal"), "the real request carried the selected credential"
    receipt = json.loads((home / "credential-policy-receipt.json").read_text())
    assert receipt["operation"]["status"] == "accepted" and receipt["revision"] == policy.revision
    # The provider refuses the credential: recorded as rejected, never accepted.
    _Provider.accept = set()
    rejected = bounded_operation(policy, resolved, entry, runtime)
    assert (rejected["status"], rejected["http_status"]) == ("rejected", 401)
    # No supported operation (Anthropic) is unsupported, never accepted.
    assert bounded_operation(policy, "anthropic", entry, runtime)["status"] == "unsupported"
    before = len(_Provider.seen)
    gate.write_text(json.dumps({"version": 1, "state": "committed", "revision": 2}))
    assert bounded_operation(policy, resolved, entry, runtime)["status"] == "stale"
    assert len(_Provider.seen) == before
