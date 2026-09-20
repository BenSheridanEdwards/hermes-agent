"""External assignments exercise the real env → pool → runtime → refresh paths."""
import io
import json
import urllib.request

import pytest
import yaml


@pytest.fixture
def assigned(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    from hermes_cli import env_loader
    env_loader.reset_secret_source_cache()

    def profile(name, environment=None, accounts=None):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        manifest = home / "assignments.json"
        data = {"version": 1, "environment": environment or {}, "managed_environment": ["OPENROUTER_API_KEY"], "accounts": accounts or {}}
        manifest.write_text(json.dumps(data))
        (home / "config.yaml").write_text(yaml.safe_dump({"credential_policy": {"file": str(manifest)}, "model": {"provider": "openrouter", "default": "test"}, "secrets": {"fixture_vault": {"enabled": True}}}))
        return home, manifest, data

    return root, profile


def test_assign_use_revoke_never_falls_back_to_dotenv_or_saved_pool(assigned, monkeypatch):
    root, profile = assigned
    home, manifest, data = profile("one", {"OPENROUTER_API_KEY": "fixture_vault"})
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / ".env").write_text("OPENROUTER_API_KEY=stale-dotenv\n")
    old = {"credential_pool": {"openrouter": [{"id": "old", "source": "manual", "access_token": "saved-key", "auth_type": "api_key", "label": "old", "priority": 0}]}}
    (home / "auth.json").write_text(json.dumps(old))
    from agent.secret_sources.base import SecretSource, FetchResult
    from agent.secret_sources.registry import register_source
    class Vault(SecretSource):
        name = "fixture_vault"
        label = "Fixture"
        shape = "mapped"
        override_existing_default = True
        def fetch(self, cfg, home_path):
            return FetchResult(secrets={"OPENROUTER_API_KEY": "assigned-key", "EXTRA_TOKEN": "not-granted"})
    register_source(Vault(), replace=True, scope=str(home.resolve()))
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "assigned-key"
    assert "EXTRA_TOKEN" not in os.environ
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested="openrouter")
    assert runtime["api_key"] == "assigned-key"
    assert json.loads((home / "auth.json").read_text()) == old
    receipt = json.loads((home / "credential-policy-receipt.json").read_text())
    assert receipt["status"] == "selected" and "assigned-key" not in json.dumps(receipt)
    data["environment"] = {}
    manifest.write_text(json.dumps(data))
    from agent.credential_policy import CredentialPolicyError
    with pytest.raises(CredentialPolicyError, match="reload"):
        runtime["credential_pool"].select()
    load_hermes_dotenv()
    assert "OPENROUTER_API_KEY" not in os.environ
    with pytest.raises(CredentialPolicyError, match="No usable assigned"):
        resolve_runtime_provider(requested="openrouter", explicit_api_key="bypass-attempt")
    manifest.unlink()
    with pytest.raises(CredentialPolicyError, match="missing or invalid"):
        resolve_runtime_provider(requested="openrouter")


def test_shared_account_refresh_stays_in_owner_store_and_preserves_unassigned_rows(assigned, monkeypatch):
    root, profile = assigned
    account = {"id": "shared", "label": "Work", "source": "manual:hermes_pkce", "auth_type": "oauth", "priority": 0,
               "access_token": "sk-ant-oat01-old", "refresh_token": "rt-old", "expires_at_ms": 1, "base_url": "https://api.anthropic.com"}
    other = {**account, "id": "other", "access_token": "other", "refresh_token": "other"}
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [account, other]}}))
    bindings = {"anthropic": {"store": "root", "ids": ["shared"]}}
    homes = [profile(name, accounts=bindings)[0] for name in ("one", "two")]
    for home in homes:
        (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [other]}}))
    from agent.credential_pool import load_pool
    pools = []
    for home in homes:
        monkeypatch.setenv("HERMES_HOME", str(home))
        pools.append(load_pool("anthropic"))
    calls = []
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def refresh(request, timeout=None):
        body = json.loads(request.data)
        assert body["refresh_token"] == "rt-old" and not calls
        calls.append(body["refresh_token"])
        return Response(json.dumps({"access_token": "sk-ant-oat01-new", "refresh_token": "rt-new", "expires_in": 28800}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", refresh)
    for home, pool in zip(homes, pools):
        monkeypatch.setenv("HERMES_HOME", str(home))
        selected = pool.select()
        assert selected.id == "shared" and selected.access_token == "sk-ant-oat01-new"
    assert len(calls) == 1
    rows = json.loads((root / "auth.json").read_text())["credential_pool"]["anthropic"]
    assert rows[0]["refresh_token"] == "rt-new" and rows[1] == other
    for home in homes:
        assert json.loads((home / "auth.json").read_text())["credential_pool"]["anthropic"] == [other]


def test_github_grant_reaches_local_terminal_but_not_other_children_or_later_profiles(assigned, monkeypatch):
    root, profile = assigned
    home, manifest, data = profile("jarvis", {"GITHUB_TOKEN": "fixture_vault"})
    data["managed_environment"].append("GITHUB_TOKEN")
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GH_TOKEN", "wrong-ambient-account")
    from agent.secret_sources.base import SecretSource, FetchResult
    from agent.secret_sources.registry import register_source
    class Vault(SecretSource):
        name = "fixture_vault"
        label = "Fixture"
        shape = "mapped"
        override_existing_default = True
        def fetch(self, cfg, home_path):
            return FetchResult(secrets={"GITHUB_TOKEN": "assigned-github"})
    register_source(Vault(), replace=True, scope=str(home.resolve()))
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    from tools.environments.local import LocalEnvironment, hermes_subprocess_env, _sanitize_subprocess_env
    terminal = LocalEnvironment(cwd=str(home))
    result = terminal.execute('test "$GH_TOKEN" = assigned-github && test "$GITHUB_TOKEN" = assigned-github && echo assigned')
    assert result["returncode"] == 0 and "assigned" in result["output"]
    from tools.process_registry import ProcessRegistry
    assert ProcessRegistry._spawn_env({})["GH_TOKEN"] == "assigned-github"
    assert "GITHUB_TOKEN" not in hermes_subprocess_env(inherit_credentials=True)
    assert "GH_TOKEN" not in _sanitize_subprocess_env({"GH_TOKEN": "wrong-ambient-account"})
    data["environment"] = {}
    manifest.write_text(json.dumps(data))
    result = terminal.execute('test -z "$GH_TOKEN" && test -z "$GITHUB_TOKEN" && echo revoked')
    assert result["returncode"] == 0 and "revoked" in result["output"]
    assert "GITHUB_TOKEN" not in ProcessRegistry._spawn_env({})
    other, _, _ = profile("other")
    monkeypatch.setenv("HERMES_HOME", str(other))
    result = terminal.execute('test -z "$GH_TOKEN" && test -z "$GITHUB_TOKEN"')
    assert result["returncode"] == 0


def test_codex_rotation_updates_root_singleton_without_overwriting_profile_account(assigned, monkeypatch):
    import base64
    import time
    import httpx
    root, profile = assigned
    def token(exp):
        return 'eyJhbGciOiJub25lIn0.' + base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip('=') + '.signature'
    expired, fresh = token(1), token(int(time.time()) + 3600)
    account = {"id": "shared", "label": "Work", "source": "device_code", "auth_type": "oauth", "priority": 0,
               "access_token": expired, "refresh_token": "rt-old", "base_url": "https://chatgpt.com/backend-api/codex"}
    store = {"providers": {"openai-codex": {"tokens": {"access_token": expired, "refresh_token": "rt-old"}}},
             "credential_pool": {"openai-codex": [account]}}
    (root / "auth.json").write_text(json.dumps(store))
    home, _, _ = profile("codex", accounts={"openai-codex": {"store": "root", "ids": ["shared"]}})
    local = {"providers": {"openai-codex": {"tokens": {"access_token": "independent", "refresh_token": "independent-rt"}}}}
    (home / "auth.json").write_text(json.dumps(local))
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import auth_codex
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, **kwargs):
            assert kwargs["data"]["refresh_token"] == "rt-old"
            return httpx.Response(200, json={"access_token": fresh, "refresh_token": "rt-new"})
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: Client())
    from agent.credential_pool import load_pool
    assert load_pool("openai-codex").select().access_token == fresh
    saved = json.loads((root / "auth.json").read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["refresh_token"] == "rt-new"
    assert saved["credential_pool"]["openai-codex"][0]["refresh_token"] == "rt-new"
    assert json.loads((home / "auth.json").read_text()) == local


def test_reassignment_during_vault_fetch_cannot_label_old_value_as_new_assignment(assigned, monkeypatch):
    _, profile = assigned
    home, manifest, data = profile("one", {"OPENROUTER_API_KEY": "fixture_vault"})
    data["source_bindings"] = {"fixture_vault": {"old-account": "OPENROUTER_API_KEY"}}
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent.secret_sources.base import SecretSource, FetchResult
    from agent.secret_sources.registry import register_source, apply_all
    class Vault(SecretSource):
        name = "fixture_vault"
        label = "Fixture"
        shape = "mapped"
        override_existing_default = True
        def fetch(self, cfg, home_path):
            assert cfg["credential_bindings"] == {"old-account": "OPENROUTER_API_KEY"}
            data["source_bindings"] = {"fixture_vault": {"new-account": "OPENROUTER_API_KEY"}}
            manifest.write_text(json.dumps(data))
            return FetchResult(secrets={"OPENROUTER_API_KEY": "old-account-key"})
    register_source(Vault(), replace=True, scope=str(home.resolve()))
    from agent.credential_policy import CredentialPolicyError
    env = {}
    with pytest.raises(CredentialPolicyError, match="changed during secret loading"):
        apply_all({"fixture_vault": {"enabled": True}}, home, environ=env)
    assert "OPENROUTER_API_KEY" not in env
    assert not (home / "credential-policy-receipt.json").exists()


def test_pre_plugin_bootstrap_preserves_evidence_and_cannot_use_ambient_github_login(assigned, monkeypatch):
    _, profile = assigned
    home, _, _ = profile("jarvis", {"GITHUB_TOKEN": "fixture_vault"})
    monkeypatch.setenv("HERMES_HOME", str(home))
    receipt = home / "credential-policy-receipt.json"
    receipt.write_text(json.dumps({"phase": "terminal", "pid": 123, "terminal_names": ["GITHUB_TOKEN"]}))
    from agent.secret_sources.registry import apply_all
    apply_all({"fixture_vault": {"enabled": True}}, home, environ={})
    assert json.loads(receipt.read_text())["pid"] == 123
    from agent.credential_policy import terminal_credentials, CredentialPolicyError
    with pytest.raises(CredentialPolicyError, match="not delivered"):
        terminal_credentials()


@pytest.mark.parametrize("contents", ["credential_policy: [", "- invalid-root", "[]", "false",
    "credential_policy: {file: missing.json}\nmax_turns: 5\nagent: oops"])
def test_policy_config_parse_errors_fail_closed_and_restore_home(assigned, contents):
    from agent.credential_policy import CredentialPolicyError, load_policy
    from hermes_constants import get_hermes_home

    root, profile = assigned
    home, _, _ = profile("broken")
    (home / "config.yaml").write_text(contents)
    with pytest.raises(CredentialPolicyError, match="Cannot read"):
        load_policy(home)
    assert get_hermes_home() == root


def test_policy_uses_managed_overlay_and_expansion_for_explicit_profile(assigned, monkeypatch, tmp_path):
    from agent.credential_policy import load_policy
    from hermes_cli import managed_scope
    from hermes_constants import get_hermes_home

    root, profile = assigned
    home, manifest, _ = profile("explicit")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text('credential_policy:\n  file: "${ASSIGNMENT_PATH}"\n')
    (home / "config.yaml").write_text('credential_policy:\n  file: nonexistent.json\n')
    monkeypatch.setenv("ASSIGNMENT_PATH", str(manifest))
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    policy = load_policy(home)
    assert policy.home == home
    assert get_hermes_home() == root
    # A broken administrator overlay must not silently drop enforcement either.
    (managed / "config.yaml").write_text("credential_policy: [")
    from agent.credential_policy import CredentialPolicyError
    with pytest.raises(CredentialPolicyError, match="Cannot read"):
        load_policy(home)


def test_policy_strict_load_rejects_cached_recovery(assigned, monkeypatch):
    from agent.credential_policy import CredentialPolicyError, load_policy
    from hermes_cli.config import load_config

    _, profile = assigned
    home, _, _ = profile("cached")
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("model: test\n")
    load_config()  # Seed last-known-good state without assignments.
    (home / "config.yaml").write_text(
        "credential_policy: {file: missing.json}\nmax_turns: 5\nagent: oops")
    assert load_config()["credential_policy"] == {"file": ""}  # Interactive recovery.
    with pytest.raises(CredentialPolicyError, match="Cannot read"):
        load_policy(home)


def test_policy_lookup_does_not_initialize_absent_home(tmp_path, monkeypatch):
    from agent.credential_policy import load_policy
    from hermes_cli import managed_scope

    home = tmp_path / "not-created" / "profile"
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: None)
    assert load_policy(home) is None
    assert not home.parent.exists()


def test_policy_strict_reads_never_write_backups_or_skeleton(assigned, monkeypatch, tmp_path):
    from agent.credential_policy import CredentialPolicyError, load_policy
    from hermes_cli import managed_scope

    _, profile = assigned
    home, _, _ = profile("read-only")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("credential_policy: [")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    before_home = set(home.iterdir())
    before_managed = set(managed.iterdir())
    with pytest.raises(CredentialPolicyError, match="Cannot read"):
        load_policy(home)
    assert set(home.iterdir()) == before_home
    assert set(managed.iterdir()) == before_managed
