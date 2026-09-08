import codecs
import importlib
import os
import sys

import pytest

from hermes_cli.env_loader import load_hermes_dotenv


def test_recovered_update_retry_skips_external_secret_sources(tmp_path, monkeypatch):
    """The post-recovery updater must not remap native vault dependencies."""
    import hermes_cli.env_loader as env_loader
    from hermes_cli import _early_recovery

    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("UPDATE_RETRY_DOTENV=loaded\n", encoding="utf-8")
    monkeypatch.delenv("UPDATE_RETRY_DOTENV", raising=False)
    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", True)
    external_calls = []
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda path: external_calls.append(path),
    )

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.environ["UPDATE_RETRY_DOTENV"] == "loaded"
    assert external_calls == []


def test_utf8_bom_does_not_mangle_first_key(tmp_path, monkeypatch):
    """A leading UTF-8 BOM must not prefix the first key name in os.environ.

    PowerShell 5.1 ``Set-Content -Encoding UTF8`` and Windows Notepad write
    a BOM (EF BB BF). With encoding=utf-8, python-dotenv keeps U+FEFF on the
    first key so the canonical name is absent and callers see "not configured".
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfFIRST_KEY=first-value\nSECOND_KEY=second-value\n"
    )

    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    monkeypatch.delenv("\ufeffFIRST_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("FIRST_KEY") == "first-value"
    assert os.getenv("SECOND_KEY") == "second-value"
    assert os.environ.get("\ufeffFIRST_KEY") is None


def test_bomless_utf8_env_still_loads(tmp_path, monkeypatch):
    """BOM-less UTF-8 .env files must keep loading after utf-8-sig."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-plain\nSECOND_KEY=ok\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_API_KEY") == "sk-plain"
    assert os.getenv("SECOND_KEY") == "ok"


def test_latin1_env_falls_back(tmp_path, monkeypatch):
    """Invalid UTF-8 bytes must still load via the latin-1 fallback."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # 0xE9 is "é" in latin-1 and not a valid UTF-8 lead sequence alone.
    env_file.write_bytes(b"LATIN1_VALUE=caf\xe9\n")

    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("LATIN1_VALUE") == "café"


def test_utf8_bom_preserves_first_api_key_name(tmp_path, monkeypatch):
    """Real-world case: BOM + first line is a provider API key name."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfANTHROPIC_API_KEY=sk-test-123\nSECOND_KEY=ok\n"
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    monkeypatch.delenv("\ufeffANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-test-123"
    assert os.getenv("SECOND_KEY") == "ok"
    assert os.environ.get("\ufeffANTHROPIC_API_KEY") is None


def test_utf8_bom_plus_invalid_utf8_preserves_first_key(tmp_path, monkeypatch):
    """BOM + non-UTF-8 body must load via latin-1 without mangling the first key.

    utf-8-sig only applies on the primary path. When invalid UTF-8 forces the
    latin-1 fallback, a leading EF BB BF would otherwise become part of the
    first key name under latin-1 and drop the canonical name.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # BOM + valid first key + latin-1 é (0xE9) in a later value.
    env_file.write_bytes(
        b"\xef\xbb\xbfANTHROPIC_API_KEY=sk-test-123\nBAD=caf\xe9\n"
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BAD", raising=False)
    monkeypatch.delenv("\ufeffANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-test-123"
    assert os.getenv("BAD") == "café"
    assert os.environ.get("\ufeffANTHROPIC_API_KEY") is None

def test_bomless_latin1_env_still_loads(tmp_path, monkeypatch):
    """BOM-less cp1252/latin-1 .env files must keep loading after the BOM strip."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_bytes(b"LATIN1_VALUE=caf\xe9\nOTHER=ok\n")

    monkeypatch.delenv("LATIN1_VALUE", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("LATIN1_VALUE") == "café"
    assert os.getenv("OTHER") == "ok"

def test_latin1_fallback_stream_honors_override(tmp_path, monkeypatch):
    """Stream-based latin-1 fallback must honor override= identically to dotenv_path."""
    from hermes_cli.env_loader import _load_dotenv_with_fallback

    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Invalid UTF-8 forces the stream/latin-1 path.
    env_file.write_bytes(b"OVERRIDE_PROBE=from-file\nLATIN1_VALUE=caf\xe9\n")

    monkeypatch.setenv("OVERRIDE_PROBE", "from-shell")
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    # override=False: shell value must win (same as dotenv_path form).
    _load_dotenv_with_fallback(env_file, override=False)
    assert os.getenv("OVERRIDE_PROBE") == "from-shell"
    assert os.getenv("LATIN1_VALUE") == "café"

    # override=True: file value must win (user-env path).
    _load_dotenv_with_fallback(env_file, override=True)
    assert os.getenv("OVERRIDE_PROBE") == "from-file"
    assert os.getenv("LATIN1_VALUE") == "café"

def test_latin1_fallback_stream_preserves_interpolation(tmp_path, monkeypatch):
    """Stream/latin-1 path must still expand ${VAR} like the dotenv_path form."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # 0xE9 forces latin-1 fallback; ${FOO} must still expand.
    env_file.write_bytes(b"FOO=bar\nBAR=${FOO}\nLATIN1_VALUE=caf\xe9\n")

    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("FOO") == "bar"
    assert os.getenv("BAR") == "bar"
    assert os.getenv("LATIN1_VALUE") == "café"

# ---------------------------------------------------------------------------
# UTF-16 / UTF-32 .env sanitizer coverage
#
# UTF-8 BOM handling for _load_dotenv_with_fallback is covered above (#65124).
# This section covers the sanitizer rewrite path for UTF-16/32 (and UTF-8 /
# cp1252 regression guards for that path).
# ---------------------------------------------------------------------------


def _assert_clean_utf8_env_on_disk(env_file, *, first_key: str) -> None:
    """On-disk file must be clean UTF-8: no BOM, no U+FFFD, canonical key."""
    after = env_file.read_bytes()
    assert not after.startswith(codecs.BOM_UTF8)
    assert not after.startswith(codecs.BOM_UTF16_LE)
    assert not after.startswith(codecs.BOM_UTF16_BE)
    text = after.decode("utf-8")  # strict — raises if not clean UTF-8
    assert "\ufffd" not in text
    assert text.startswith(f"{first_key}=") or f"\n{first_key}=" in text
    assert first_key.encode("ascii") in after




def test_utf16_le_bom_preserves_non_ascii_values(tmp_path, monkeypatch):
    """UTF-16-LE+BOM rewrite must preserve non-ASCII values (not just ASCII keys).

    Uses non-credential var names so _sanitize_loaded_credentials does not
    strip non-ASCII from values (that path only targets *_KEY/*_TOKEN/etc.).
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    content = "GREETING=café\nCJK_LABEL=日本語\n"
    env_file.write_bytes(codecs.BOM_UTF16_LE + content.encode("utf-16-le"))

    monkeypatch.delenv("GREETING", raising=False)
    monkeypatch.delenv("CJK_LABEL", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GREETING") == "café"
    assert os.getenv("CJK_LABEL") == "日本語"
    after = env_file.read_bytes()
    assert after.decode("utf-8")  # strict
    assert "café".encode("utf-8") in after
    assert "日本語".encode("utf-8") in after
    assert b"\xef\xbf\xbd" not in after


def test_utf32_le_bom_leaves_file_untouched(tmp_path, caplog):
    """UTF-32-LE BOM: refuse-to-mangle (leave bytes untouched + warning).

    UTF-32-LE's BOM starts with UTF-16-LE's FF FE; sniff order must check
    UTF-32 first so we never misdetect and corrupt.

    Exercises ``_sanitize_env_file_if_needed`` only: the dotenv load path
    is out of scope here (#65124's surface) and still cannot ingest UTF-32.
    """
    import logging

    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    env_file = tmp_path / ".env"
    content = "HERMES_TEST_KEY=hello_utf32\nSECOND_KEY=world\n"
    raw = codecs.BOM_UTF32_LE + content.encode("utf-32-le")
    env_file.write_bytes(raw)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        _sanitize_env_file_if_needed(env_file)

    assert env_file.read_bytes() == raw  # untouched
    assert any("UTF-32" in r.message for r in caplog.records)




def test_utf32_warning_fires_once_per_path(tmp_path, caplog, monkeypatch):
    """Three sanitize calls on the same UTF-32 file → exactly one warning.

    Matches house style for warn-once (module-level seen-set, same class as
    ``_WARNED_KEYS``): hot-reload / multi-entry load must not spam logs.
    """
    import logging

    import hermes_cli.env_loader as env_loader
    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    # Isolate process-level seen-set so other tests' paths don't leak in.
    monkeypatch.setattr(env_loader, "_WARNED_UTF32_PATHS", set())

    env_file = tmp_path / ".env"
    content = "HERMES_TEST_KEY=hello_utf32\nSECOND_KEY=world\n"
    raw = codecs.BOM_UTF32_LE + content.encode("utf-32-le")
    env_file.write_bytes(raw)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.env_loader"):
        _sanitize_env_file_if_needed(env_file)
        _sanitize_env_file_if_needed(env_file)
        _sanitize_env_file_if_needed(env_file)

    utf32_warnings = [r for r in caplog.records if "UTF-32" in r.message]
    assert len(utf32_warnings) == 1
    assert env_file.read_bytes() == raw




def test_plain_utf8_env_regression(tmp_path, monkeypatch):
    """Plain UTF-8 .env must keep loading after the UTF-16 sanitize changes."""
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    before = b"OPENAI_API_KEY=sk-plain\nSECOND_KEY=ok\n"
    env_file.write_bytes(before)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_API_KEY") == "sk-plain"
    assert os.getenv("SECOND_KEY") == "ok"
    # No spurious rewrite of an already-clean file.
    assert env_file.read_bytes() == before


def test_cp1252_env_regression_does_not_crash(tmp_path, monkeypatch):
    """cp1252/latin-1 body must not crash sanitize; ASCII keys still usable.

    0xE9 is 'é' in cp1252 and incomplete as UTF-8. First line does not begin
    with U+FFFD, so the FFFD guard must not refuse the whole file.

    Sanitize leaves the file bytes alone when the only "change" is
    errors=replace on values (original already replace-decoded equals
    sanitized), so _load_dotenv_with_fallback's latin-1 path recovers café.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    before = b"ASCII_KEY=ok\nLATIN1_VALUE=caf\xe9\n"
    env_file.write_bytes(before)

    monkeypatch.delenv("ASCII_KEY", raising=False)
    monkeypatch.delenv("LATIN1_VALUE", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("ASCII_KEY") == "ok"
    assert os.getenv("LATIN1_VALUE") == "café"
    # Sanitize must not have rewritten (would have persisted U+FFFD).
    assert env_file.read_bytes() == before


# ---------------------------------------------------------------------------
# Profile .env isolation: inherited known-key cleanup
# ---------------------------------------------------------------------------


def test_known_keys_absent_from_user_env_are_cleared(tmp_path, monkeypatch):
    """Known Hermes keys inherited from parent process are removed when absent
    from the profile's .env.

    This is the startup equivalent of ``reload_env()``'s known-key cleanup and
    fixes the isolation gap where one profile's ACP/provider settings silently
    leak into another profile's runtime via ``os.environ`` inheritance.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://profile.example/v1\n", encoding="utf-8"
    )

    # Inherited known keys from parent process / other profile
    monkeypatch.setenv("OPENAI_BASE_URL", "https://stale.example/v1")
    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")
    monkeypatch.setenv("COPILOT_CLI_PATH", "/usr/bin/claude-code")
    # Unrelated shell var must NOT be touched
    monkeypatch.setenv("MY_SHELL_ONLY_VAR", "keep-me")

    load_hermes_dotenv(hermes_home=home)

    # OPENAI_BASE_URL is defined in the profile .env → overridden to the new value
    assert os.getenv("OPENAI_BASE_URL") == "https://profile.example/v1"
    # HERMES_ACP_AUTH_METHOD and COPILOT_CLI_PATH are NOT in the profile .env → cleared
    assert "HERMES_ACP_AUTH_METHOD" not in os.environ
    assert "COPILOT_CLI_PATH" not in os.environ
    # Unrelated shell vars must survive
    assert os.getenv("MY_SHELL_ONLY_VAR") == "keep-me"


def test_empty_assignment_in_user_env_is_preserved(tmp_path, monkeypatch):
    """An explicit ``KEY=`` (empty value) in the profile .env keeps the key
    in ``os.environ`` — distinct from a key absent from .env entirely.

    Empty ``HERMES_ACP_AUTH_METHOD=`` tells the ACP adapter to skip
    ``authenticate`` (the key exists, its value is just empty).  This is the
    documented workaround for the leak and must still work after the cleanup.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text("HERMES_ACP_AUTH_METHOD=\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")
    monkeypatch.setenv("COPILOT_CLI_PATH", "/usr/bin/sneaky")  # NOT in .env → cleared

    load_hermes_dotenv(hermes_home=home)

    # KEY= in .env keeps the key (now empty string)
    assert "HERMES_ACP_AUTH_METHOD" in os.environ
    assert os.environ["HERMES_ACP_AUTH_METHOD"] == ""
    # COPILOT_CLI_PATH is absent from .env → cleared
    assert "COPILOT_CLI_PATH" not in os.environ


def test_no_user_env_does_not_clear_anything(tmp_path, monkeypatch):
    """When no profile .env exists (bare profile), load_hermes_dotenv must not
    wipe inherited known keys — the bare-profile case follows #66930 / #67027
    semantics and the user's shell environment should not be mutilated.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    # No .env in home — bare profile

    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("HERMES_ACP_AUTH_METHOD") == "cursor_login"
    assert os.getenv("PATH") == "/usr/bin:/bin"


def test_known_key_explicitly_set_in_user_env_is_kept(tmp_path, monkeypatch):
    """A known Hermes key that IS explicitly set in the profile .env survives
    the cleanup (overrides the inherited value).
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "HERMES_ACP_AUTH_METHOD=claude_code_cli\n", encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("HERMES_ACP_AUTH_METHOD") == "claude_code_cli"


def test_export_prefixed_known_key_in_user_env_is_kept(tmp_path, monkeypatch):
    """A known Hermes key defined with the bash-compatible ``export KEY=value``
    form in the profile .env must be recognized as defined and survive the
    cleanup - mirrors the ``export `` stripping in config.py's load_env()
    (#6659).
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "export HERMES_ACP_AUTH_METHOD=claude_code_cli\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")
    load_hermes_dotenv(hermes_home=home)
    assert os.getenv("HERMES_ACP_AUTH_METHOD") == "claude_code_cli"


def test_shell_exported_credentials_survive_cleanup(tmp_path, monkeypatch):
    """User-shell-exported provider credentials must NOT be scrubbed.

    ``export OPENAI_API_KEY=…`` in the shell with a ``.env`` that doesn't
    contain the key is a documented, legitimate flow (see
    test_dump_env_visibility.py). The startup cleanup is scoped to
    _PROFILE_MANAGED_ENV_KEYS (ACP routing keys) precisely so it can never
    delete shell-supplied credentials — a process cannot distinguish a
    shell export from parent-process leakage, so credential isolation is
    owned by read-time secret scoping instead.
    """
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text("SOME_OTHER_KEY=x\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:token-from-shell")
    # A profile-managed routing key inherited alongside them IS cleared.
    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("OPENAI_API_KEY") == "sk-from-shell"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-from-shell"
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "12345:token-from-shell"
    assert "HERMES_ACP_AUTH_METHOD" not in os.environ


def test_cleanup_scope_is_the_profile_managed_set():
    """Lock the invariant: the startup scrub set contains only behavioral
    ACP/routing keys — never credential-shaped keys. If this fails, someone
    widened _PROFILE_MANAGED_ENV_KEYS toward the full known-key set, which
    re-introduces the shell-export deletion bug.
    """
    from hermes_cli.env_loader import _PROFILE_MANAGED_ENV_KEYS

    for key in _PROFILE_MANAGED_ENV_KEYS:
        assert not key.endswith(("_API_KEY", "_TOKEN", "_SECRET")), (
            f"{key} looks credential-shaped; startup scrub must not "
            "cover credentials — read-time secret scoping owns those"
        )


# ---------------------------------------------------------------------------
# config.yaml terminal.* re-apply after dotenv loads (#29186 / #67323)
#
# load_hermes_dotenv loads .env with override=True, so a stale
# TERMINAL_ENV=docker in .env used to silently beat config.yaml's
# terminal.backend on every reload (gateway per-turn reload, cron standalone
# runs). The bridge re-applies config.yaml's EXPLICIT terminal keys last via
# the shared hermes_cli.config.apply_terminal_config_to_env helper.
# ---------------------------------------------------------------------------


def _seed_terminal_home(tmp_path, monkeypatch, *, config_yaml=None, env_text=None):
    home = tmp_path / "hermes"
    home.mkdir()
    if config_yaml is not None:
        (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if env_text is not None:
        (home / ".env").write_text(env_text, encoding="utf-8")
    # The bridge is scoped to the process HERMES_HOME (a different profile's
    # load must not bridge this process's config), so point the process at
    # the seeded home like a real gateway/cron process would be.
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_config_yaml_terminal_backend_overrides_stale_env(tmp_path, monkeypatch):
    """Regression for #29186: a leftover TERMINAL_ENV=docker in ~/.hermes/.env
    must not silently override the user's choice in config.yaml. config.yaml
    is the documented source of truth, so its value must win after load."""
    home = _seed_terminal_home(
        tmp_path, monkeypatch,
        config_yaml="terminal:\n  backend: local\n",
        env_text="TERMINAL_ENV=docker\n",
    )

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("TERMINAL_ENV") == "local"


def test_config_yaml_terminal_backend_overrides_stale_shell(tmp_path, monkeypatch):
    """config.yaml must also beat a stale TERMINAL_ENV exported in the shell
    (e.g. set in ~/.zshrc when the user was experimenting with docker)."""
    home = _seed_terminal_home(
        tmp_path, monkeypatch,
        config_yaml="terminal:\n  backend: local\n",
    )

    monkeypatch.setenv("TERMINAL_ENV", "docker")

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("TERMINAL_ENV") == "local"


def test_no_terminal_section_leaves_env_value_alone(tmp_path, monkeypatch):
    """When config.yaml has no terminal section, the .env value is still the
    user's active setting — the bridge must NOT clobber it with merged
    defaults."""
    home = _seed_terminal_home(
        tmp_path, monkeypatch,
        config_yaml="display:\n  streaming: true\n",
        env_text="TERMINAL_ENV=docker\n",
    )

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("TERMINAL_ENV") == "docker"


def test_config_yaml_terminal_omitted_key_does_not_clear_env(tmp_path, monkeypatch):
    """If config.yaml has a terminal block but no `backend`, the .env value
    must survive (only explicit config keys override env)."""
    home = _seed_terminal_home(
        tmp_path, monkeypatch,
        config_yaml="terminal:\n  timeout: 600\n",
        env_text="TERMINAL_ENV=docker\n",
    )

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    load_hermes_dotenv(hermes_home=home)

    assert os.getenv("TERMINAL_ENV") == "docker"
    assert os.getenv("TERMINAL_TIMEOUT") == "600"


def test_other_profile_home_does_not_bridge_process_config(tmp_path, monkeypatch):
    """Loading a DIFFERENT profile's .env must not re-bridge this process's
    config.yaml — the shared bridge reads the process-global config, so
    applying it for another home would stamp the wrong profile's terminal
    settings into the env."""
    process_home = tmp_path / "process-home"
    process_home.mkdir()
    (process_home / "config.yaml").write_text(
        "terminal:\n  backend: local\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    other_home = tmp_path / "other-profile"
    other_home.mkdir()
    (other_home / ".env").write_text("TERMINAL_ENV=docker\n", encoding="utf-8")

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    load_hermes_dotenv(hermes_home=other_home)

    # The other profile's .env value stands; the process config was not applied.
    assert os.getenv("TERMINAL_ENV") == "docker"


# ---------------------------------------------------------------------------
# ACP-hosted precedence: host-owned env beats the profile .env
#
# Under Buzz Desktop's buzz-acp harness the host owns the agent identity and
# passes it in as env: HERMES_HOME=<profile>, BUZZ_PRIVATE_KEY=<managed key>,
# BUZZ_AUTH_TAG, BUZZ_RELAY_URL, plus BUZZ_MANAGED_AGENT as the harness marker.
# A profile whose .env carries its own BUZZ_PRIVATE_KEY used to override the
# managed key on the override=True load, so the agent signed as the wrong
# identity and every relay send failed BUZZ_AUTH_TAG verification.
#
# mark_acp_hosted() snapshots the host-owned set once, before any load, and
# every later load restores it. The Buzz credential group is all-or-nothing:
# BUZZ_AUTH_TAG is an attestation bound to BUZZ_PRIVATE_KEY, so a .env that
# completes a part-supplied host identity splits it across two owners and fails
# relay verification for exactly the reason the unfixed override did.
#
# Non-ACP entrypoints, and plain editor hosts with no BUZZ_MANAGED_AGENT, keep
# the documented ".env overrides stale shell exports" rule unchanged.
# ---------------------------------------------------------------------------


def _seed_buzz_profile(tmp_path, env_text):
    home = tmp_path / "profile"
    home.mkdir()
    if env_text is not None:
        (home / ".env").write_text(env_text, encoding="utf-8")
    return home


_BUZZ_PROFILE_ENV = (
    "BUZZ_PRIVATE_KEY=profile-key\n"
    "BUZZ_RELAY_URL=ws://profile.example\n"
    "HERMES_HOME=/profile/says/elsewhere\n"
    "OPENAI_API_KEY=sk-from-profile\n"
)


def _clear_buzz_env(monkeypatch):
    for key in ("BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL", "BUZZ_API_TOKEN",
                "BUZZ_MANAGED_AGENT", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def _mark_acp_hosted(monkeypatch, env_loader):
    """Set the marker the way an ACP entrypoint does, with the process-wide globals it writes restored
    on teardown: monkeypatch.setattr records the pre-test value, so a snapshot taken here cannot leak
    into another test even though the real globals are deliberately never cleared in production."""
    monkeypatch.setattr(env_loader, "_ACP_HOSTED", False)
    monkeypatch.setattr(env_loader, "_ACP_HOST_ENV", {})
    monkeypatch.setattr(env_loader, "_ACP_RESTORE_LOGGED", False)
    env_loader.mark_acp_hosted()


def test_acp_hosted_keeps_host_owned_env_over_profile_env(tmp_path, monkeypatch):
    """Managed ACP host: BUZZ_* and HERMES_HOME passed in by the host survive the
    profile .env load, and non host-owned keys keep the documented
    .env-overrides-shell rule. The host supplied part of the Buzz identity, so
    the profile's BUZZ_RELAY_URL is dropped rather than used to complete it."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setenv("BUZZ_AUTH_TAG", "tag-from-host")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")

    monkeypatch.setattr(env_loader, "_ACP_HOSTED", False)
    assert env_loader.is_acp_hosted() is False
    _mark_acp_hosted(monkeypatch, env_loader)
    assert env_loader.is_acp_hosted() is True

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [home / ".env"]
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert os.environ["BUZZ_AUTH_TAG"] == "tag-from-host"
    assert os.environ["HERMES_HOME"] == str(home)
    # The host owns this identity; .env must not supply the relay leg of it.
    assert "BUZZ_RELAY_URL" not in os.environ
    # Not host-owned: .env still beats the stale shell export.
    assert os.environ["OPENAI_API_KEY"] == "sk-from-profile"


def test_acp_hosted_profile_env_cannot_complete_a_split_buzz_identity(tmp_path, monkeypatch):
    """BUZZ_AUTH_TAG is a NIP-OA attestation bound to the signing key, so a host
    that passes only BUZZ_PRIVATE_KEY must not end up signing with the managed
    key while presenting the profile's tag: the whole BUZZ_* group the .env
    introduced is dropped, not gap-filled."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(
        tmp_path,
        "BUZZ_PRIVATE_KEY=profile-key\n"
        "BUZZ_AUTH_TAG=profile-tag\n"
        "BUZZ_RELAY_URL=ws://profile.example\n"
        "BUZZ_API_TOKEN=profile-token\n",
    )
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert "BUZZ_AUTH_TAG" not in os.environ
    assert "BUZZ_RELAY_URL" not in os.environ
    assert "BUZZ_API_TOKEN" not in os.environ


@pytest.mark.parametrize("host_env", [
    {},                                          # host claimed nothing at all
    {"BUZZ_RELAY_URL": "ws://host.example"},     # relay only: an endpoint, not a signing claim
])
def test_acp_hosted_profile_buzz_env_intact_when_host_supplies_no_identity(
    tmp_path, monkeypatch, host_env
):
    """All-or-nothing cuts both ways: a managed host that has not claimed the
    identity leaves the profile .env supplying all of it exactly as before.

    BUZZ_RELAY_URL is a member of the group but not a trigger for it: it is a
    non-secret endpoint that a custom harness definition or an ambient shell
    export can carry alone, and treating it as a claim would delete the
    profile's key AND tag and leave the agent with nothing to sign with, a
    total outage rather than a wrong identity."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    for key, value in host_env.items():
        monkeypatch.setenv(key, value)
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "profile-key"
    # A relay the host did pass is still host-owned; it just did not claim the group.
    assert os.environ["BUZZ_RELAY_URL"] == host_env.get(
        "BUZZ_RELAY_URL", "ws://profile.example"
    )


def test_acp_hosted_only_drops_buzz_keys_the_dotenv_defined(tmp_path, monkeypatch):
    """A BUZZ_* variable set at runtime after the marker is not the profile
    completing an identity, so the restore leaves it alone: only names actually
    assigned by the loaded .env files are dropped."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, "BUZZ_RELAY_URL=ws://profile.example\n")
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    _mark_acp_hosted(monkeypatch, env_loader)
    monkeypatch.setenv("BUZZ_SESSION_ID", "set-at-runtime")

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert "BUZZ_RELAY_URL" not in os.environ
    assert os.environ["BUZZ_SESSION_ID"] == "set-at-runtime"


def test_plain_editor_acp_host_keeps_dotenv_precedence_for_buzz(tmp_path, monkeypatch):
    """Without BUZZ_MANAGED_AGENT the ACP host is a plain editor (Zed, VS Code),
    where the docs tell operators to export BUZZ_PRIVATE_KEY in the launching
    shell. Reversing precedence there would break users who never had a managed
    identity, so only HERMES_HOME is protected."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "shell-export-key")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "profile-key"
    assert os.environ["BUZZ_RELAY_URL"] == "ws://profile.example"
    assert os.environ["HERMES_HOME"] == str(home)


def test_acp_host_snapshot_cannot_latch_a_clobbered_value(tmp_path, monkeypatch):
    """The thread race in miniature: another thread's in-flight load left the
    profile value in os.environ. A snapshot re-read per load would capture THAT
    and re-assert it forever; the snapshot is taken once, by the marker, so the
    next load restores the host value.

    Also covers the repeat-load case run_agent / lazy MCP loads hit: the marker
    is process-wide, so a later load cannot re-clobber either."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    _mark_acp_hosted(monkeypatch, env_loader)

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "profile-key")  # other thread, mid-load
    load_hermes_dotenv(hermes_home=home)
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"

    load_hermes_dotenv(hermes_home=home, project_env=tmp_path / "missing.env")
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"


def test_marking_acp_hosted_twice_keeps_the_pre_load_snapshot(tmp_path, monkeypatch):
    """`hermes acp` marks TWICE: once at main's import scope, then again inside
    acp_adapter.entry._load_env(). By the second call a dotenv load has already
    run, so re-snapshotting would capture whatever is in os.environ then and pin
    it as host-owned. On any argv shape the import-time gate misses, that is the
    PROFILE's key pinned for the life of the process, silently. Arming is
    idempotent, so the latch class is gone independently of the gate."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    _mark_acp_hosted(monkeypatch, env_loader)

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "profile-key")  # an unmarked load already ran
    env_loader.mark_acp_hosted()  # entry._load_env()'s second call

    assert env_loader._ACP_HOST_ENV["BUZZ_PRIVATE_KEY"] == "managed-key"
    load_hermes_dotenv(hermes_home=home)
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"


def test_acp_env_lock_keeps_a_sibling_loader_out_of_the_load_window(tmp_path, monkeypatch):
    """An ACP process loads dotenv from background threads (entry starts
    background MCP discovery, sessions register MCP servers via asyncio.to_thread).

    What the lock actually buys is that a sibling LOADER cannot run its own
    override/delete passes inside another load's window, so pin that rather than
    the settled value the immutable snapshot already guarantees on its own: the
    sibling started mid-window must still be blocked when the window ends."""
    import threading

    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    _mark_acp_hosted(monkeypatch, env_loader)

    finished = threading.Event()
    observed: list = []  # [None] once armed, then the sibling's finished-state at window close
    errors: list[BaseException] = []

    def sibling():
        try:
            load_hermes_dotenv(hermes_home=home)
        except BaseException as exc:  # noqa: BLE001 (surfaced as a test failure below)
            errors.append(exc)
        finished.set()

    thread = threading.Thread(target=sibling)

    def during_the_window(_home):
        """Stands in for the external secret fetch, inside the outer load's window.
        Runs its body once: the sibling's own load reaches this hook too."""
        if observed:
            return
        observed.append(None)
        thread.start()
        finished.wait(timeout=1.0)
        observed[0] = finished.is_set()

    monkeypatch.setattr(env_loader, "_apply_external_secret_sources", during_the_window)

    load_hermes_dotenv(hermes_home=home)
    thread.join(timeout=30)

    assert errors == []
    assert observed == [False], "a sibling loader ran inside the load window"
    assert not thread.is_alive()
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"


def test_acp_hosted_non_dotenv_profile_sources_cannot_complete_the_identity(tmp_path, monkeypatch):
    """The user and project .env are not the profile's only supply routes.
    `.op.env` is loaded straight into os.environ and never appears in `loaded`,
    and an external secret source injects without touching a dotenv file at all.
    A host passing only BUZZ_PRIVATE_KEY while either of those supplies
    BUZZ_AUTH_TAG produces the same split identity, and fails relay
    verification for exactly the reason the unfixed override did."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, "OPENAI_API_KEY=sk-from-profile\n")
    (home / ".op.env").write_text("BUZZ_AUTH_TAG=opdotenv-tag\n", encoding="utf-8")
    _clear_buzz_env(monkeypatch)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda _home: os.environ.__setitem__("BUZZ_RELAY_URL", "ws://vault.example"),
    )
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert "BUZZ_AUTH_TAG" not in os.environ
    assert "BUZZ_RELAY_URL" not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "sk-from-profile"


def test_acp_host_key_is_ascii_sanitized_before_it_is_restored(tmp_path, monkeypatch):
    """BUZZ_PRIVATE_KEY ends in _KEY, so _sanitize_loaded_credentials strips
    non-ASCII from it. Re-installing the raw host value on restore would undo
    that sweep (and _WARNED_KEYS would suppress the second warning), shipping a
    key that cannot be sent as an HTTP header."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setattr(env_loader, "_WARNED_KEYS", set())
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1ab​cd")
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "nsec1abcd"
    assert os.environ["BUZZ_PRIVATE_KEY"].isascii()


def test_acp_host_env_opt_out_restores_dotenv_precedence(tmp_path, monkeypatch):
    """HERMES_ACP_HOST_ENV=0 is the operator kill switch: an install that hits a
    bad interaction can get the old precedence back without a downgrade."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setenv("HERMES_ACP_HOST_ENV", "0")
    _mark_acp_hosted(monkeypatch, env_loader)

    assert env_loader.is_acp_hosted() is False

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "profile-key"


def test_non_acp_profile_env_still_overrides_inherited_buzz_key(tmp_path, monkeypatch):
    """Gateway / CLI / cron are unchanged: the profile .env overrides an
    inherited BUZZ_PRIVATE_KEY exactly as before."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setattr(env_loader, "_ACP_HOSTED", False)
    monkeypatch.setattr(env_loader, "_ACP_HOST_ENV", {})

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "profile-key"
    assert os.environ["OPENAI_API_KEY"] == "sk-from-profile"


@pytest.mark.parametrize("host_value", ["", "   ", "\n", "\t "])
def test_acp_hosted_blank_host_value_is_filled_from_profile_env(tmp_path, monkeypatch, host_value):
    """A host that passes a blank BUZZ_PRIVATE_KEY has not provided a key; the
    profile .env may fill it. Blank, not merely empty: "   " and a trailing "\\n"
    are what a harness that read the key out of a file passes, both are truthy,
    and _sanitize_credential_value cannot help because whitespace is ASCII.
    Counting one as provided would pin a key that cannot sign AND claim the
    whole group with it, deleting the profile's working identity."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", host_value)
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "profile-key"
    assert os.environ["BUZZ_RELAY_URL"] == "ws://profile.example"


def test_acp_hosted_profile_without_env_keeps_host_env(tmp_path, monkeypatch):
    """Bare profile (no .env): nothing to restore, host env untouched."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, None)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setenv("BUZZ_AUTH_TAG", "tag-from-host")
    _mark_acp_hosted(monkeypatch, env_loader)

    assert load_hermes_dotenv(hermes_home=home) == []

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert os.environ["BUZZ_AUTH_TAG"] == "tag-from-host"
    assert "BUZZ_RELAY_URL" not in os.environ


def test_acp_hosted_known_key_cleanup_is_unchanged(tmp_path, monkeypatch):
    """The startup scrub of _PROFILE_MANAGED_ENV_KEYS absent from .env is not
    part of the host-owned set and behaves exactly as in non-ACP mode."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setenv("HERMES_ACP_AUTH_METHOD", "cursor_login")
    monkeypatch.setenv("COPILOT_CLI_PATH", "/usr/bin/sneaky")
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert "HERMES_ACP_AUTH_METHOD" not in os.environ
    assert "COPILOT_CLI_PATH" not in os.environ
    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"


def test_acp_hosted_managed_env_still_beats_host(tmp_path, monkeypatch):
    """The admin-managed .env keeps its top-of-stack precedence: host-owned
    keys are restored before the managed overlay, not after."""
    import hermes_cli.env_loader as env_loader
    from hermes_cli import managed_scope

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".env").write_text("BUZZ_RELAY_URL=ws://org.example\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setenv("BUZZ_RELAY_URL", "ws://host.example")
    _mark_acp_hosted(monkeypatch, env_loader)

    try:
        load_hermes_dotenv(hermes_home=home)
    finally:
        managed_scope.invalidate_managed_cache()

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"
    assert os.environ["BUZZ_RELAY_URL"] == "ws://org.example"


def test_acp_hosted_beats_an_override_existing_secret_source(tmp_path, monkeypatch):
    """A profile mapping BUZZ_PRIVATE_KEY from a vault with override_existing:
    true writes over a pre-existing env value, so the restore has to run after
    external secret sources, not only after the dotenv loads."""
    import hermes_cli.env_loader as env_loader

    home = _seed_buzz_profile(tmp_path, _BUZZ_PROFILE_ENV)
    _clear_buzz_env(monkeypatch)
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "managed-key")
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda _home: os.environ.__setitem__("BUZZ_PRIVATE_KEY", "vault-key"),
    )
    _mark_acp_hosted(monkeypatch, env_loader)

    load_hermes_dotenv(hermes_home=home)

    assert os.environ["BUZZ_PRIVATE_KEY"] == "managed-key"


def test_dotenv_key_scanner_ignores_multi_line_quoted_values(tmp_path):
    """_env_keys_defined_in_dotenv is a line scanner feeding the scrub of
    _PROFILE_MANAGED_ENV_KEYS absent from .env. A quoted value may span lines,
    and a continuation line holding an `=` used to parse as its own assignment,
    inventing key names the file never defined and hiding real ones that follow."""
    from hermes_cli.env_loader import _env_keys_defined_in_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        'BUZZ_AUTH_TAG=\'{\n'
        '  "kind": 22242,\n'
        '  "BUZZ_RELAY_URL": "ws://not-an-assignment"\n'
        '}\'\n'
        'HERMES_ACP_AUTH_METHOD=cursor_login\n'
        'BUZZ_PRIVATE_KEY="single-line"\n',
        encoding="utf-8",
    )

    assert _env_keys_defined_in_dotenv(env_file) == {
        "BUZZ_AUTH_TAG", "HERMES_ACP_AUTH_METHOD", "BUZZ_PRIVATE_KEY",
    }


def test_acp_host_owned_set_is_identity_only():
    """Lock the invariant: the host-owned set is the agent identity the host
    passes in (HERMES_HOME, BUZZ_*), never provider credentials. Widening it
    would let a stale shell export beat the .env written by `hermes setup`
    for every editor-hosted ACP user. The Buzz group is the atomic part of it:
    key, attestation and relay travel together in one signed auth event, and
    only the two signing members COUNT as the host claiming that identity."""
    from hermes_cli.env_loader import (
        _ACP_HOST_OWNED_ENV_KEYS,
        _ACP_HOST_OWNED_ENV_PREFIXES,
        _BUZZ_IDENTITY_ENV_KEYS,
        _BUZZ_IDENTITY_TRIGGER_KEYS,
    )

    assert _ACP_HOST_OWNED_ENV_KEYS == frozenset({"HERMES_HOME"})
    assert _ACP_HOST_OWNED_ENV_PREFIXES == ("BUZZ_",)
    assert _BUZZ_IDENTITY_ENV_KEYS == frozenset({
        "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL",
    })
    assert _BUZZ_IDENTITY_TRIGGER_KEYS == frozenset({"BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"})
    assert _BUZZ_IDENTITY_TRIGGER_KEYS < _BUZZ_IDENTITY_ENV_KEYS
