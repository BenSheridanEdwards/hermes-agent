"""FLEET-owned OAuth grants are read-only in Hermes (design A).

A credential is externally owned when the profile's credential policy binds its
provider (a FLEET assignment) or config sets ``oauth.refresh_owner: external``.
Hermes must then never spend the refresh token — not on select/lease, not on a
401, not in the auxiliary client, not via the singleton resolvers — and must
not bench the shared grant or rotate onto another pooled account. It only
re-reads the bound store and adopts tokens another writer (FLEET) put there.

Every test drives a production seam and counts refresh POSTs at the network
primitives (``refresh_codex_oauth_pure`` / ``refresh_xai_oauth_pure``).
"""
import base64
import json
import time

import pytest
import yaml


def _jwt(exp):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{claims}.signature"


EXPIRED = _jwt(1)
CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _fresh(tag=""):
    # Distinct fresh tokens: vary exp so each JWT differs.
    return _jwt(int(time.time()) + 3600 + len(tag))


def _row(cid, access, refresh, **extra):
    return {"id": cid, "label": cid, "source": "device_code", "auth_type": "oauth", "priority": 0,
            "access_token": access, "refresh_token": refresh, "base_url": CODEX_URL, **extra}


@pytest.fixture
def posts(monkeypatch):
    """Record (never perform) every refresh-token POST; a POST returns a new pair."""
    import hermes_cli.auth as auth_mod
    calls = []

    def fake(provider):
        def refresh(access_token, refresh_token, **kwargs):
            calls.append((provider, refresh_token))
            return {"access_token": _fresh("posted"), "refresh_token": refresh_token + "-rotated",
                    "last_refresh": "2026-09-28T00:00:00Z"}
        return refresh

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", fake("openai-codex"))
    monkeypatch.setattr(auth_mod, "refresh_xai_oauth_pure", fake("xai-oauth"))
    return calls


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    return root


def _assigned_profile(root, monkeypatch, rows, ids):
    """A FLEET-assigned profile: accounts.openai-codex bound to the profile store."""
    home = root / "profiles" / "worker"
    home.mkdir(parents=True)
    manifest = home / "assignments.json"
    manifest.write_text(json.dumps({"version": 1, "environment": {}, "managed_environment": [],
                                    "accounts": {"openai-codex": {"store": "profile", "ids": ids}}}), encoding="utf-8")
    (home / "config.yaml").write_text(yaml.safe_dump({"credential_policy": {"file": str(manifest)}}), encoding="utf-8")
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": rows}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _fleet_writes(home, cid, access, refresh):
    """Simulate FLEET's refresher rotating the grant in the bound store."""
    path = home / "auth.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    for row in store["credential_pool"]["openai-codex"]:
        if row["id"] == cid:
            row["access_token"], row["refresh_token"] = access, refresh
    path.write_text(json.dumps(store), encoding="utf-8")


def _stored(home, cid):
    rows = json.loads((home / "auth.json").read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]
    return next(row for row in rows if row["id"] == cid)


class _Agent:
    """Minimal agent surface ``recover_with_credential_pool`` reads and writes."""

    def __init__(self, pool, api_key, entry_id):
        self._credential_pool = pool
        self.provider = pool.provider
        self.base_url = CODEX_URL
        self.api_mode = "codex_responses"
        self.api_key = api_key
        self._credential_pool_entry_id = entry_id
        self.swapped, self.warnings = [], []

    def _is_entitlement_failure(self, *_args):
        return False

    def _swap_credential(self, entry):
        self.swapped.append(entry)
        self.api_key = entry.runtime_api_key

    def _emit_warning(self, message):
        self.warnings.append(message)


def _recover_401(agent):
    from agent.agent_runtime_helpers import recover_with_credential_pool
    from agent.error_classifier import FailoverReason
    return recover_with_credential_pool(agent, status_code=401, has_retried_429=False,
                                        classified_reason=FailoverReason.auth,
                                        error_context={"message": "token expired"})


# ── FLEET assignment: select / lease ───────────────────────────────────────


def test_assigned_select_adopts_fleet_rotation_and_never_posts(home, monkeypatch, posts):
    worker = _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")

    # FLEET has not rotated yet: the entry stays selectable, Hermes does not refresh it.
    assert pool.select().access_token == EXPIRED
    assert posts == []

    fresh = _fresh("fleet")
    _fleet_writes(worker, "a", fresh, "rt-a2")
    selected = pool.select()
    assert (selected.access_token, selected.refresh_token) == (fresh, "rt-a2")
    assert posts == []
    assert _stored(worker, "a")["refresh_token"] == "rt-a2"


def test_assigned_acquire_lease_never_posts(home, monkeypatch, posts):
    worker = _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    assert pool.acquire_lease() == "a"
    fresh = _fresh("lease")
    _fleet_writes(worker, "a", fresh, "rt-a2")
    assert pool.acquire_lease() == "a"
    assert pool.current().access_token == fresh
    assert posts == []


# ── FLEET assignment: 401 recovery ─────────────────────────────────────────


def test_assigned_401_adopts_newer_store_token_and_retries_once(home, monkeypatch, posts):
    worker = _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    agent = _Agent(pool, EXPIRED, "a")

    fresh = _fresh("401")
    _fleet_writes(worker, "a", fresh, "rt-a2")
    assert _recover_401(agent) == (True, False)
    assert [e.access_token for e in agent.swapped] == [fresh]

    # The adopted token is rejected too and the store holds nothing newer: stop, don't loop.
    assert _recover_401(agent) == (False, False)
    assert len(agent.swapped) == 1
    assert posts == []


def test_assigned_401_unchanged_store_surfaces_fleet_error_without_exhausting_or_rotating(
    home, monkeypatch, posts, caplog,
):
    worker = _assigned_profile(
        home, monkeypatch,
        [_row("a", EXPIRED, "rt-a"), _row("b", _fresh("b"), "rt-b", priority=1)], ["a", "b"],
    )
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    agent = _Agent(pool, EXPIRED, "a")

    with caplog.at_level("ERROR"):
        assert _recover_401(agent) == (False, False)

    assert posts == []
    assert agent.swapped == []  # No silent account switch onto "b".
    for cid in ("a", "b"):
        assert _stored(worker, cid).get("last_status") in (None, "ok")
    assert any("FLEET" in w and "openai-codex" in w for w in agent.warnings)
    assert "renew the grant in FLEET" in caplog.text
    from agent.credential_policy import ExternalCredentialExpired
    assert isinstance(agent._external_credential_error, ExternalCredentialExpired)
    assert pool.current() is None or pool.current().id == "a"


def test_assigned_direct_mark_exhausted_on_auth_failure_is_refused(home, monkeypatch, posts):
    worker = _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    assert pool.mark_exhausted_and_rotate(status_code=401, api_key_hint=EXPIRED, credential_id="a") is None
    assert _stored(worker, "a").get("last_status") in (None, "ok")
    assert pool.try_refresh_matching(api_key_hint=EXPIRED, credential_id="a") is None
    assert posts == []


# ── FLEET assignment: auxiliary client ─────────────────────────────────────


def test_assigned_auxiliary_recovery_never_refreshes_or_exhausts(home, monkeypatch, posts):
    worker = _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    from agent import auxiliary_client as aux
    exc = Exception("Error code: 401 - token expired")
    exc.status_code = 401

    assert aux._refresh_provider_credentials("openai-codex") is False
    assert aux._recover_provider_pool("openai-codex", exc, failed_api_key=EXPIRED) is False
    assert _stored(worker, "a").get("last_status") in (None, "ok")
    assert posts == []

    _fleet_writes(worker, "a", _fresh("aux"), "rt-a2")
    assert aux._recover_provider_pool("openai-codex", exc, failed_api_key=EXPIRED) is True
    assert posts == []


# ── config oauth.refresh_owner: external (no assignment) ───────────────────


def _config_external(root, **tokens):
    (root / "config.yaml").write_text(yaml.safe_dump({"oauth": {"refresh_owner": "external"}}), encoding="utf-8")
    providers = {p: {"tokens": {"access_token": a, "refresh_token": r}} for p, (a, r) in tokens.items()}
    (root / "auth.json").write_text(json.dumps({"version": 1, "providers": providers}), encoding="utf-8")


def test_config_external_singleton_resolvers_refuse_forced_refresh(home, posts):
    from hermes_cli.auth import AuthError, resolve_codex_runtime_credentials, resolve_xai_oauth_runtime_credentials
    _config_external(home, **{"openai-codex": (EXPIRED, "rt-c"), "xai-oauth": (EXPIRED, "rt-x")})

    # Proactive: serve the stored token; the manager rotates it.
    assert resolve_codex_runtime_credentials()["api_key"] == EXPIRED
    assert resolve_xai_oauth_runtime_credentials()["api_key"] == EXPIRED
    # Forced (token rejected): refuse loudly, as an AuthError existing handlers understand.
    for resolve in (resolve_codex_runtime_credentials, resolve_xai_oauth_runtime_credentials):
        with pytest.raises(AuthError, match="FLEET") as info:
            resolve(force_refresh=True)
        from agent.credential_policy import ExternalCredentialExpired
        assert isinstance(info.value, ExternalCredentialExpired)
    assert posts == []
    from agent import auxiliary_client as aux
    assert aux._refresh_provider_credentials("openai-codex") is False
    assert aux._refresh_provider_credentials("xai-oauth") is False
    assert posts == []


def test_config_external_pool_select_never_posts(home, posts):
    _config_external(home)
    auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    auth["credential_pool"] = {"openai-codex": [_row("manual1", EXPIRED, "rt-m", source="manual:device_code")]}
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    assert pool.select().access_token == EXPIRED
    assert pool.try_refresh_current() is None
    assert posts == []
    assert pool.refresh_externally_owned() is True


# ── capabilities contract ──────────────────────────────────────────────────


def test_capabilities_report_effective_refresh_owner(home, monkeypatch, capsys):
    from agent.credential_policy import capabilities_command
    capabilities_command(None)
    unassigned = json.loads(capsys.readouterr().out)
    assert unassigned["refresh_owner"] == "hermes"
    assert set(unassigned) >= {"credential_policy", "account_providers", "refresh_owner", "activation"}

    _config_external(home)
    capabilities_command(None)
    assert json.loads(capsys.readouterr().out)["refresh_owner"] == "external"

    _assigned_profile(home, monkeypatch, [_row("a", EXPIRED, "rt-a")], ["a"])
    capabilities_command(None)
    assigned = json.loads(capsys.readouterr().out)
    assert assigned["refresh_owner"] == "external"
    # Additive field: lets a manager detect support before it assigns anything.
    assert "external" in assigned["refresh_owner_modes"]


# ── regression: unassigned profiles still self-refresh ─────────────────────


def test_unassigned_profile_still_self_refreshes_on_select_and_401(home, posts):
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {
        "openai-codex": [_row("m1", EXPIRED, "rt-m", source="manual:device_code")]}}), encoding="utf-8")
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    assert pool.select().refresh_token == "rt-m-rotated"
    assert posts == [("openai-codex", "rt-m")]

    agent = _Agent(pool, pool.current().access_token, "m1")
    recovered, _ = _recover_401(agent)
    assert recovered is True and posts[-1] == ("openai-codex", "rt-m-rotated")
    assert getattr(agent, "_external_credential_error", None) is None
