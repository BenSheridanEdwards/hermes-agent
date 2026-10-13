"""F12: retained managed agents use ordinary dispatch, never the access probe.

All credential material is synthetic. Only a task-local loopback SSE provider is
reachable; policy publication and the SDK/request lifecycle are real.
"""
from contextlib import contextmanager
import base64
import http.server
import json
import socket
import threading

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@contextmanager
def recipient(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def loopback_provider(monkeypatch):
    seen = []

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                {"type": "response.output_text.delta", "delta": "OK", "output_index": 0,
                 "content_index": 0, "item_id": "controlled", "sequence_number": 1},
                {"type": "response.completed", "response": {"id": "controlled", "status": "completed",
                 "object": "response", "output": [], "usage": None}, "sequence_number": 2},
            ]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connect = socket.socket.connect

    def only_fixture_network(sock, address):
        assert address == server.server_address, f"non-fixture network forbidden: {address}"
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", only_fixture_network)
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("change", ["revoke", "remove-policy", "corrupt-policy"])
def test_retained_managed_codex_agent_denies_revoked_recipient_and_preserves_peer(tmp_path, monkeypatch, loopback_provider, change):
    from agent.credential_policy import CredentialPolicyError, load_policy, select_managed_runtime
    from run_agent import AIAgent

    url, seen = loopback_provider
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"}
    }).encode()).decode().rstrip("=")
    key = f"eyJhbGciOiJub25lIn0.{payload}.fixture"
    store = root / "auth.json"
    store.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [{
        "id": "shared-account", "label": "Fixture", "auth_type": "oauth", "priority": 0,
        "source": "manual:device_code", "access_token": key,
        "refresh_token": "fixture-refresh", "base_url": url,
    }]}}))
    assignments = tmp_path / "access" / "assignments"
    assignments.mkdir(parents=True)
    gate = assignments.parent / "authority-publication.json"
    homes = {name: root / "profiles" / name for name in ("alpha", "beta")}
    manifests = {name: assignments / f"{name}.json" for name in homes}

    def publish(revision, revoked=()):
        gate.write_text(json.dumps({"version": 1, "state": "publishing", "revision": revision}))
        for name, manifest in manifests.items():
            manifest.write_text(json.dumps({"version": 2,
                "publication": {"file": str(gate), "revision": revision},
                "environment": {}, "managed_environment": [], "source_bindings": {},
                "accounts": {} if name in revoked else {"openai-codex": {"store": "root", "ids": ["shared-account"]}},
            }))
        gate.write_text(json.dumps({"version": 1, "state": "committed", "revision": revision}))

    publish(1)
    agents = {}
    held_clients = {}
    try:
        for name, home in homes.items():
            home.mkdir(parents=True)
            (home / "config.yaml").write_text(
                f"credential_policy:\n  file: {manifests[name]}\nmodel:\n  provider: openai-codex\n  default: gpt-5.4\n")
            with recipient(home):
                policy = load_policy()
                assert policy is not None
                _, entry, runtime = select_managed_runtime(policy, "openai-codex")
                assert entry.id == "shared-account" and runtime["base_url"] == url
                agent = agents[name] = AIAgent(
                    model="gpt-5.4", provider=runtime["provider"], api_mode=runtime["api_mode"],
                    api_key=runtime["api_key"], base_url=runtime["base_url"],
                    credential_pool=runtime["credential_pool"], enabled_toolsets=[],
                    quiet_mode=True, skip_context_files=True, skip_memory=True, save_trajectories=False,
                )
                assert agent._credential_pool is runtime["credential_pool"]
                held_clients[name] = agent._create_request_openai_client(reason="codex_stream_request")
                agent._close_request_openai_client(held_clients[name], reason="request_complete")

        def infer(name):
            with recipient(homes[name]):
                return agents[name]._interruptible_api_call({
                    "model": "gpt-5.4", "input": f"ordinary-{name}", "store": False,
                })

        before_inference = len(seen)  # Init may probe the loopback model metadata.
        assert infer("alpha").output_text == "OK"
        assert infer("beta").output_text == "OK"
        assert len(seen) == before_inference + 2
        assert all(request["path"] == "/responses" and request["authorization"] == f"Bearer {key}"
                   and request["body"]["stream"] is True for request in seen[before_inference:])
        store_before = store.read_bytes()
        before_revoke = len(seen)
        publish(2, revoked={"alpha"} if change == "revoke" else ())
        if change == "remove-policy":
            (homes["alpha"] / "config.yaml").write_text("{}\n")
        elif change == "corrupt-policy":
            manifests["alpha"].write_text("{broken")
        # Same live instances, same cached transport, actual next ordinary inference.
        denied = None
        try:
            infer("alpha")
        except CredentialPolicyError as error:
            denied = error
        assert len(seen) == before_revoke, "revoked runtime must make zero post-revoke provider requests"
        assert denied is not None, "revocation must surface, not return an invented successful response"
        # Direct reuse of the already-held SDK client cannot borrow another
        # recipient's ambient profile or bypass the ordinary dispatch boundary.
        with recipient(homes["beta"]), pytest.raises(CredentialPolicyError):
            agents["alpha"]._run_codex_stream({"model": "gpt-5.4", "input": "held-alpha", "store": False},
                                               client=held_clients["alpha"])
        assert len(seen) == before_revoke
        assert infer("beta").output_text == "OK"
        assert len(seen) == before_revoke + 1 and seen[-1]["body"]["input"] == "ordinary-beta"
        with recipient(homes["beta"]):
            reused = agents["beta"]._create_request_openai_client(reason="codex_stream_request")
            assert reused is held_clients["beta"], "unaffected peer retained its authorized transport"
            agents["beta"]._close_request_openai_client(reused, reason="request_complete")
        assert store.read_bytes() == store_before, "revocation must not invalidate the shared account"
    finally:
        for agent in agents.values():
            agent._close_cached_request_openai_client(reason="test_teardown")
            agent.client.close()
