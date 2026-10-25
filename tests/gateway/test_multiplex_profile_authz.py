"""Regression tests for multiplex profile-aware own-policy authorization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "WECOM_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "WECOM_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_multiplex_runner(monkeypatch):
    """Runner with default allowlist WeCom and secondary open-policy WeCom."""
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    default_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="allowlist",
        _group_policy="pairing",
    )
    secondary_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="open",
        _group_policy="open",
    )

    runner.adapters = {Platform.WECOM: default_adapter}
    runner._profile_adapters = {
        "coder": {Platform.WECOM: secondary_adapter},
    }
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner, default_adapter, secondary_adapter


def test_default_profile_still_trusts_own_allowlist(monkeypatch):
    """Default-profile allowlist trust is unchanged when profile is unstamped."""
    runner, _default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="allowed-user",
        chat_id="dm-chat",
        user_name="allowed-user",
        chat_type="dm",
        profile=None,
    )

    assert runner._is_user_authorized(source) is True


def test_active_profile_stamp_resolves_primary_adapter(monkeypatch):
    """A single-profile gateway stamps its active profile but stores adapters as primary."""
    runner, default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)
    runner._active_profile_name = lambda: "dev"

    assert runner._authorization_adapter(Platform.WECOM, profile="dev") is default_adapter


def test_scoped_secondary_profile_still_uses_profile_adapters(monkeypatch):
    """Runtime scope must not redirect secondary authz to primary adapters.

    ``_make_profile_message_handler`` wraps ``_handle_message`` in
    ``_profile_runtime_scope``, which overrides HERMES_HOME so
    ``get_active_profile_name()`` equals the secondary profile for that turn.
    Authorization must still read ``_profile_adapters[profile]``, not the
    empty primary ``self.adapters`` map — otherwise upstream-auth platforms
    such as A2A default-deny an already-authenticated peer (#80884). A
    secondary profile with NO registry entry still fails closed.
    """
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False

    secondary = SimpleNamespace(
        authorization_is_upstream=True,
        enforces_own_access_policy=False,
    )
    runner._profile_adapters = {"beta": {Platform("a2a"): secondary}}
    # Simulate the scoped turn: active profile name collapses to the secondary.
    runner._active_profile_name = lambda: "beta"

    assert runner._authorization_adapter(Platform("a2a"), profile="beta") is secondary

    source = SessionSource(
        platform=Platform("a2a"),
        chat_id="a2a-context",
        user_id="alpha",
        user_name="alpha",
        chat_type="dm",
        profile="beta",
    )
    assert runner._is_user_authorized(source) is True

    # Fail-closed guard is untouched: no registry entry -> no default fallback.
    runner._profile_adapters = {"beta": {}}
    assert runner._authorization_adapter(Platform("a2a"), profile="beta") is None


def test_secondary_allowlist_dm_behavior_ignores_unauthorized(monkeypatch):
    """Unauthorized-DM behavior must read the secondary adapter's dm_policy."""
    runner, _default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)
    secondary_adapter._dm_policy = "allowlist"

    assert runner._get_unauthorized_dm_behavior(
        Platform.WECOM,
        profile="coder",
    ) == "ignore"
    assert runner._get_unauthorized_dm_behavior(Platform.WECOM) == "ignore"


def test_adapter_auth_check_stamps_secondary_profile(monkeypatch):
    """The adapter auth-check callback must stamp its own secondary profile.

    Regression for the gap where ``_make_adapter_auth_check`` built a
    profile-less ``SessionSource``, so a secondary adapter's external-context
    authorization (e.g. Slack/Discord thread-reply lookups) silently
    resolved the *active* profile's allowlist scope instead of its own.
    """
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    captured: dict = {}

    def fake_is_user_authorized(source):
        captured["profile"] = source.profile
        return True

    runner._is_user_authorized = fake_is_user_authorized

    check = runner._make_adapter_auth_check(Platform.WECOM, profile_name="coder")
    assert check("some-user", "dm", "dm-chat") is True
    assert captured["profile"] == "coder"


def test_startup_guard_gateway_allow_all_reads_scope_not_environ(monkeypatch):
    """The GATEWAY_ALLOW_ALL_USERS opt-in check inside the startup guard
    must honor the active profile secret scope (#93522): the default
    profile's env-only opt-in must not leak into a secondary profile that
    never opted in, and a secondary profile's own scoped opt-in must be
    honored."""
    from agent import secret_scope
    from gateway.run import _own_policy_open_startup_violation

    _clear_auth_env(monkeypatch)
    cfg = GatewayConfig(multiplex_profiles=True)
    cfg.platforms = {
        Platform.WECOM: PlatformConfig(enabled=True, extra={"dm_policy": "open"}),
    }

    previous_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    try:
        token = secret_scope.set_secret_scope({"SOMETHING_ELSE": "x"})
        try:
            violation = _own_policy_open_startup_violation(cfg)
        finally:
            secret_scope.reset_secret_scope(token)
        assert violation is not None, "default profile's env opt-in must not leak into the scoped secondary profile"

        token = secret_scope.set_secret_scope({"GATEWAY_ALLOW_ALL_USERS": "true"})
        try:
            violation = _own_policy_open_startup_violation(cfg)
        finally:
            secret_scope.reset_secret_scope(token)
        assert violation is None, "the secondary profile's own scoped opt-in must be honored"
    finally:
        secret_scope.set_multiplex_active(previous_multiplex)


def test_secondary_open_policy_fails_startup_guard(monkeypatch):
    """Secondary profiles must pass the same open-policy startup guard."""
    from gateway.run import _own_policy_open_startup_violation

    _clear_auth_env(monkeypatch)

    secondary_cfg = GatewayConfig(multiplex_profiles=True)
    secondary_cfg.platforms = {
        Platform.WECOM: PlatformConfig(
            enabled=True,
            extra={"dm_policy": "open"},
        ),
    }

    violation = _own_policy_open_startup_violation(secondary_cfg)
    assert violation is not None
    assert "wecom" in violation
    assert "open policy" in violation


# ─────────────────────────────────────────────────────────────────────
# Plugin-platform extra.allowed_users fallback (#98738 / #82871)
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def chatx_registered():
    """Register a neutral plugin platform ``chatx``; returns ``register(allowed_users_env)``.

    A plugin platform has no static Platform member: ``Platform("chatx")`` resolves only once the
    registry knows the name, so every test registers before building its runner."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    def register(allowed_users_env="CHATX_ALLOWED_USERS"):
        platform_registry.register(PlatformEntry(
            name="chatx", label="ChatX", adapter_factory=lambda cfg: None, check_fn=lambda: True,
            allowed_users_env=allowed_users_env))
        return Platform("chatx")

    yield register
    platform_registry.unregister("chatx")


def _make_plugin_multiplex_runner(monkeypatch, extra, normalize_user_id=None):
    """Runner whose secondary 'coder' profile runs a live plugin-platform adapter."""
    from gateway.run import GatewayRunner

    for key in (
        "CHATX_ALLOWED_USERS",
        "CHATX_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    adapter = SimpleNamespace(config=PlatformConfig(enabled=True, extra=extra))
    if normalize_user_id is not None:
        adapter.normalize_user_id = normalize_user_id
    runner.adapters = {}
    runner._profile_adapters = {"coder": {Platform("chatx"): adapter}}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def _plugin_source(user_id):
    return SessionSource(
        platform=Platform("chatx"),
        user_id=user_id,
        chat_id="chat-1",
        user_name="member",
        chat_type="dm",
        profile="coder",
    )


def test_secondary_plugin_extra_allowed_users_authorizes_listed_user(monkeypatch, chatx_registered):
    """A secondary profile's extra.allowed_users must authorize its users when
    the env var only ever carried the default profile's list (#98738/#82871)."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={"allowed_users": ["member-1"]})

    assert runner._is_user_authorized(_plugin_source("member-1")) is True


def test_secondary_plugin_extra_allowed_users_denies_unlisted_sender(monkeypatch, chatx_registered):
    """Default-deny is preserved: a sender not in the profile's allowlist
    stays denied even though the adapter-level list admitted the message."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={"allowed_users": ["member-2"]})

    assert runner._is_user_authorized(_plugin_source("member-1")) is False


def test_secondary_plugin_without_extra_allowlist_stays_default_deny(monkeypatch, chatx_registered):
    """No extra.allowed_users configured: nothing changes, the default-deny
    path applies (no fail-open via an empty list)."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={})

    assert runner._is_user_authorized(_plugin_source("member-1")) is False


def test_extra_allowed_users_not_consulted_without_registry_declaration(monkeypatch, chatx_registered):
    """The fallback is gated on the platform's registry entry declaring
    allowed_users_env — a platform without that contract keeps the previous
    behavior even if its extra happens to hold an allowed_users key."""
    chatx_registered("")
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={"allowed_users": ["someone"]})

    assert runner._is_user_authorized(_plugin_source("someone")) is False


def test_extra_allowed_users_wildcard_authorizes_any_sender(monkeypatch, chatx_registered):
    """\"*\" in the profile's extra.allowed_users keeps the env-var wildcard
    semantics: any sender is authorized (still gated on the registry
    declaration)."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={"allowed_users": ["*"]})

    assert runner._is_user_authorized(_plugin_source("member-1")) is True


def test_extra_allowed_users_blank_entries_are_dropped_not_denials(monkeypatch, chatx_registered):
    """Blank/whitespace entries are dropped at parse; an otherwise-empty
    list behaves like the absent case (default-deny), not like a wildcard."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(monkeypatch, extra={"allowed_users": ["", "   ", ","]})

    assert runner._is_user_authorized(_plugin_source("member-1")) is False


def test_extra_allowed_users_entries_pass_through_adapter_normalize_hook(monkeypatch, chatx_registered):
    """An adapter's ``normalize_user_id`` maps each allowlist entry onto the id
    spelling it dispatches, so a differently spelled entry still matches; an
    entry that normalizes elsewhere stays a denial."""
    chatx_registered()
    runner = _make_plugin_multiplex_runner(
        monkeypatch, extra={"allowed_users": ["Member-1"]}, normalize_user_id=str.lower
    )

    assert runner._is_user_authorized(_plugin_source("member-1")) is True
    assert runner._is_user_authorized(_plugin_source("member-2")) is False
