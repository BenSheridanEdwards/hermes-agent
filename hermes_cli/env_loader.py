"""Helpers for loading Hermes .env files consistently across entrypoints."""

from __future__ import annotations

import codecs
import io
import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from utils import atomic_replace, fast_safe_load

logger = logging.getLogger(__name__)

# The ONLY env vars sanitized on load: credentials must be pure ASCII (they become HTTP header values);
# arbitrary user env vars are never silently altered.
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")

# Once-per-process guards: load_hermes_dotenv() runs repeatedly (user + project env, gateway hot-reload,
# lazy imports mid-turn, tests) so warnings/logs fire once per key/path/home.
_WARNED_KEYS: set[str] = set()          # credential names already given the non-ASCII warning
_WARNED_UTF32_PATHS: set[str] = set()   # .env paths already given the UTF-32 refuse-to-mangle warning
_SCOPED_SKIP_LOGGED: set[str] = set()   # routed profile homes whose multiplex dotenv skip was logged

# env-var name → source label ("bitwarden", …) for externally injected credentials; setup / `hermes
# model` tell users WHERE a key came from when .env lacks it.
_SECRET_SOURCES: dict[str, str] = {}
# Immutable per-home snapshots: os.environ is shared across profiles and a later home's apply may overwrite it.
_SECRET_SOURCE_VALUES_BY_HOME: dict[str, dict[str, str]] = {}
# HERMES_HOME paths already pulled external secrets for: load_hermes_dotenv() runs at import time from
# several hot modules, so without this the Bitwarden status line prints 3-5x per startup and the config
# re-parse + ASCII sweep re-run each time (Bitwarden's own cache only saves the network call).
_APPLIED_HOMES: set[str] = set()
_SECRET_SOURCE_CACHE_LOCK = threading.RLock()

# Behavioral routing keys a parent Hermes process injects into child env that silently redirect a profile
# onto the wrong provider path; these — and ONLY these — are scrubbed at startup when absent from the
# profile's .env. Credentials are excluded: shell exports are a documented way to supply them, and
# read-time secret-scope checks (agent/secret_scope.py) own cross-profile credential isolation.
_PROFILE_MANAGED_ENV_KEYS: frozenset[str] = frozenset({
    "HERMES_ACP_AUTH_METHOD", "HERMES_ACP_AUTO_APPROVE", "HERMES_COPILOT_ACP_COMMAND",
    "HERMES_COPILOT_ACP_ARGS", "COPILOT_CLI_PATH", "COPILOT_ACP_BASE_URL",
})

# ACP hosting (``hermes acp`` / ``hermes-acp`` under an editor or agent harness such as Buzz Desktop):
# the host process owns the agent's identity and injects it as env (``HERMES_HOME=<profile>``,
# ``BUZZ_PRIVATE_KEY=<managed key>``, ``BUZZ_AUTH_TAG``, ``BUZZ_RELAY_URL``). Every gateway-plugin profile
# also carries its own ``BUZZ_PRIVATE_KEY`` in ``.env``, so the override=True load below used to replace
# the managed key, the agent signed as the wrong identity and every relay send failed auth-tag
# verification. While ACP-hosted these keys keep the value the host passed in.
#
# Deliberately a narrow, documented set rather than a blanket override=False: editor hosts (Zed, VS Code)
# hand Hermes the user's login-shell env, so "host wins for everything" would let a stale
# ``OPENAI_API_KEY`` export beat the ``.env`` written by ``hermes setup``, the exact thing override=True
# exists to prevent. The managed-scope ``.env`` (admin lockdown) still beats the host. A blank host value
# (empty, or whitespace only) counts as "not provided".
#
# ``HERMES_HOME`` is protected from ``.env`` ONLY. It is NOT "whatever the host passed": on the
# ``hermes acp`` entrypoint ``main._apply_profile_override()`` runs before the marker and may already have
# rewritten it from the sticky ``active_profile`` when the host pointed at the hermes root rather than at a
# ``profiles/<name>`` dir. The snapshot pins whatever the profile router resolved, which is the value the
# rest of the process is already using.
#
# ``BUZZ_*`` is additionally gated on ``BUZZ_MANAGED_AGENT`` (set only by Buzz Desktop's buzz-acp harness,
# the same signal ``tools/environments/local_env_policy.py`` uses). Without it the ACP host is a plain
# editor, where ``website/docs/user-guide/features/acp.md`` documents ``export BUZZ_PRIVATE_KEY=...`` in
# the launching shell as the supported flow; reversing precedence there would break users who never had a
# managed identity.
_ACP_HOST_OWNED_ENV_KEYS: frozenset[str] = frozenset({"HERMES_HOME"})
_ACP_HOST_OWNED_ENV_PREFIXES: tuple[str, ...] = ("BUZZ_",)
# One Buzz identity, not three independent values: ``BUZZ_AUTH_TAG`` is a NIP-OA owner attestation bound to
# the signing key and ``BUZZ_RELAY_URL`` is a tag in the same kind-22242 auth event that
# ``BUZZ_PRIVATE_KEY`` signs (plugins/platforms/buzz/nostr_auth.py). Letting ``.env`` fill the members the
# host left unset would pair the managed key with the profile's attestation and fail relay verification for
# exactly the reason the unfixed override did. So the group is all-or-nothing: once the host claims the
# identity, the profile may not complete the three names in ``_BUZZ_IDENTITY_ENV_KEYS`` from any of its four
# supply routes: ``.env``, the project ``.env``, ``.op.env``, an external secret source.
_BUZZ_IDENTITY_ENV_KEYS: frozenset[str] = frozenset({
    "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL",
})
# What the restore DELETES when the host claims the identity: the identity, not the ``BUZZ_`` namespace.
# The namespace is about thirty names and the rest of it is plugin configuration ``hermes setup`` writes
# into the profile's own ``.env`` (``BUZZ_CLI_PATH``, ``BUZZ_CHANNELS``, ``BUZZ_HOME_CHANNEL``,
# ``BUZZ_ALLOWED_USERS``, ``BUZZ_POLL_INTERVAL`` …). Dropping those leaves a managed agent that signs
# correctly and cannot send: ``plugins/platforms/buzz/adapter.py`` fails ``_standalone_send`` on a missing
# CLI path or target channel and watches nothing without channels. ``BUZZ_CREDENTIALS_FILE`` is the one
# non-identity name in the set, because a credentials record is itself an identity source
# (``adapter.py:_resolve_credentials_data``).
_BUZZ_IDENTITY_DROP_KEYS: frozenset[str] = _BUZZ_IDENTITY_ENV_KEYS | {"BUZZ_CREDENTIALS_FILE"}
# What COUNTS as the host claiming that identity. Only the signing credentials: ``BUZZ_RELAY_URL`` is a
# non-secret endpoint that a custom harness or an ambient shell export can carry on its own, and treating
# it as a claim would delete a working profile identity and replace it with nothing (see
# ``_host_owns_buzz_identity``). It stays a member of the group, so a claiming host still owns it.
_BUZZ_IDENTITY_TRIGGER_KEYS: frozenset[str] = frozenset({"BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"})
_ACP_HOSTED = False  # set once per process by mark_acp_hosted(); never cleared outside tests
# Host-owned values captured ONCE, by mark_acp_hosted(), before any dotenv load. Deliberately not
# re-read from os.environ per load: load_hermes_dotenv() runs on background threads in an ACP process
# (acp_adapter.entry starts background MCP discovery, ACP registers session MCP servers via
# asyncio.to_thread), and a per-load snapshot taken inside another thread's load window would capture the
# profile's value and then re-assert THAT forever. An immutable snapshot cannot latch.
_ACP_HOST_ENV: dict[str, str] = {}
# Serializes the dotenv load + host-env restore window while ACP-hosted so a concurrent LOADER cannot
# observe the .env value between the override load and the restore, and two loads cannot interleave their
# override/delete passes. It buys nothing for non-loading readers, which take no lock and read os.environ
# directly; that window is closed instead by restoring after each profile source, not by this.
_ACP_ENV_LOCK = threading.RLock()
_ACP_RESTORE_LOGGED = False  # once-per-process guard, like _WARNED_KEYS / _SCOPED_SKIP_LOGGED
_ACP_KEYLESS_CLAIM_LOGGED = False  # ditto, for the tag-without-key warning
_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def mark_acp_hosted(enabled: bool = True) -> None:
    """Flag this process as ACP-hosted and snapshot the host-owned env (``_ACP_HOST_OWNED_ENV_KEYS`` /
    ``_ACP_HOST_OWNED_ENV_PREFIXES``) so every later :func:`load_hermes_dotenv` keeps it over the profile's
    ``.env``. ``HERMES_HOME`` is protected from ``.env`` only, not from the profile router that already ran.

    Called by ``acp_adapter.entry`` before its first load and by ``hermes_cli.main`` when the subcommand is
    ``acp`` (its import-time load runs before the subcommand dispatches). Process-wide on purpose:
    ``run_agent`` and lazy MCP loads call :func:`load_hermes_dotenv` again later in the same process and
    would otherwise re-clobber the host's values.

    ``HERMES_ACP_HOST_ENV=0`` (also ``false``/``no``/``off``) is an operator kill switch that restores the
    pre-fix precedence without a downgrade.

    Arming is IDEMPOTENT. ``hermes acp`` marks twice (here at import scope, then again inside
    ``acp_adapter.entry._load_env()``) and by the second call a dotenv load has already run. Re-snapshotting
    then would capture whatever ``os.environ`` holds at that point and pin it as host-owned, which on any
    argv shape the import-time gate misses means pinning the PROFILE's key forever. Keeping the first
    snapshot removes that latch class outright, independently of what the gate gets right."""
    global _ACP_HOSTED, _ACP_HOST_ENV
    if enabled and os.environ.get("HERMES_ACP_HOST_ENV", "").strip().lower() in _OFF_VALUES:
        logger.debug("acp: HERMES_ACP_HOST_ENV opt-out set, profile .env keeps its usual precedence")
        enabled = False
    if enabled and _ACP_HOSTED:
        return  # already armed: the first snapshot is the only pre-load one there is
    _ACP_HOSTED = bool(enabled)
    _ACP_HOST_ENV = _snapshot_acp_host_env() if _ACP_HOSTED else {}


def is_acp_hosted() -> bool:
    """True once :func:`mark_acp_hosted` ran in this process."""
    return _ACP_HOSTED


def acp_host_owns_buzz_identity() -> bool:
    """THE rule, asked as one question: an ACP host in this process supplied a signing member of the Buzz
    identity group, so it owns the whole group and no other principal may complete it.

    Every enforcement point in the codebase derives from this predicate rather than re-deriving its own
    version of "is the host in charge here": the env restore (:func:`_restore_acp_host_env`), the managed
    overlay (:func:`_settle_buzz_identity_after_managed_env`), the Buzz plugin's unscoped disk fallback and
    its credentials-record fallback (``plugins/platforms/buzz/adapter.py``). Four rounds of review found the
    same mismatch through four different routes because each route carried its own condition; one predicate
    is what stops the fifth."""
    return _ACP_HOSTED and _host_owns_buzz_identity()


def acp_host_owns_buzz_identity_key(name: str) -> bool:
    """:func:`acp_host_owns_buzz_identity` narrowed to one name: ``name`` is in the claimed group, so the
    restore has already deleted the profile's value for it from ``os.environ``.

    For consumers that can reach the profile's values by some route other than ``os.environ``. The Buzz
    plugin's unscoped fallback is one: ``build_profile_secret_scope`` re-reads ``<home>/.env`` off disk, so
    without this it hands back the very names the restore dropped and pairs the host's managed key with the
    profile owner's attestation again. An env-level rule cannot see a file read, so the rule has to be
    askable."""
    return name in _BUZZ_IDENTITY_DROP_KEYS and acp_host_owns_buzz_identity()


def _is_acp_host_owned_env_key(name: str) -> bool:
    """``name`` is host-owned in THIS process: ``HERMES_HOME`` always, ``BUZZ_*`` only for a Buzz-managed
    agent (see ``BUZZ_MANAGED_AGENT`` in the module comment above)."""
    if name in _ACP_HOST_OWNED_ENV_KEYS:
        return True
    return name.startswith(_ACP_HOST_OWNED_ENV_PREFIXES) and bool(os.environ.get("BUZZ_MANAGED_AGENT"))


def _env_provides(name: str) -> bool:
    """THE definition of "``name`` was supplied" for the whole ACP identity rule: present in
    ``os.environ`` AND non-blank.

    One function because two of them disagreed and that disagreement was a hole. :func:`_snapshot_acp_host_env`
    read "provided" as non-blank and :func:`_buzz_env_names` read it as ``in os.environ``, so a host exporting
    a blank ``BUZZ_AUTH_TAG`` beside a real ``BUZZ_PRIVATE_KEY`` was simultaneously not the owner of that name
    (kept out of the snapshot) and the reason the drop rule spared it (counted into the pre-load baseline).
    The profile's tag then survived the load beside the managed key: the exact mismatch this whole path
    exists to prevent, reached without a credentials file, a vault or a ``.op.env``.

    Blank, not merely empty: ``"   "`` and a trailing ``"\\n"`` are the classic artifact of a harness that
    reads a value out of a file and exports it unconditionally, and both are truthy."""
    return bool(os.environ.get(name, "").strip())


def _snapshot_acp_host_env() -> dict[str, str]:
    """Host-owned keys currently in ``os.environ`` with a non-blank value (empty means "not provided").

    Blank, not just empty: ``"   "`` and a trailing ``"\\n"`` are the classic artifact of a harness that
    reads a key out of a file, and both are truthy. Treating one as provided would pin an unusable key AND
    claim the whole Buzz group with it, deleting the profile's working identity for a value that cannot
    sign. ``_sanitize_credential_value`` does not catch it: whitespace is ASCII. The test is
    :func:`_env_provides`, shared with :func:`_buzz_env_names` so the two cannot drift apart again.

    Values are ASCII-sanitized here, not on restore: ``BUZZ_PRIVATE_KEY`` ends in ``_KEY`` and so falls
    under ``_sanitize_loaded_credentials``, and re-installing the raw host value afterwards would undo that
    sweep and ship a header-invalid key. Sanitizing at capture also means the warning names the host's
    value, before ``_WARNED_KEYS`` can be spent on the profile's."""
    if not _ACP_HOSTED:
        return {}
    return {
        k: _sanitize_credential_value(k, os.environ[k])
        for k in list(os.environ)
        if _env_provides(k) and _is_acp_host_owned_env_key(k)
    }


def _host_owns_buzz_identity() -> bool:
    """The host claimed the Buzz identity, i.e. supplied a SIGNING member of the group
    (``_BUZZ_IDENTITY_TRIGGER_KEYS``).

    ``BUZZ_RELAY_URL`` is a member of the group but not a trigger for it: it is a non-secret endpoint, and
    a harness whose env carries only a relay URL (a custom managed-agent definition, or an ambient
    ``export BUZZ_RELAY_URL`` in the shell that launched Desktop) has claimed no identity. Triggering on it
    would delete the profile's key AND tag and leave nothing behind, turning a wrong-identity risk into a
    total outage."""
    return any(key in _ACP_HOST_ENV for key in _BUZZ_IDENTITY_TRIGGER_KEYS)


def _restore_acp_host_env(buzz_before_load: frozenset[str]) -> None:
    """Drop every ``_BUZZ_IDENTITY_DROP_KEYS`` name that APPEARED DURING the load and that the host did
    not pass (a split identity fails relay auth), and THEN re-assert the snapshot. In that order: the
    restore is a sequence of ``os.environ`` writes with no lock over the readers, so its intermediate
    states are as observable as its result. See the comment on the two loops below.

    The drop is the identity group, NOT the ``BUZZ_`` prefix. The prefix is what the host OWNS, so a
    managed harness passing ``BUZZ_CHANNELS`` still beats the profile's; but deleting the prefix took the
    profile's Buzz plugin configuration with the identity and left an agent that signs correctly and
    cannot send. See ``_BUZZ_IDENTITY_DROP_KEYS``.

    "Appeared during the load", not "assigned by the loaded ``.env`` files": the loaded-path list covers
    only the user and project ``.env``, while ``.op.env`` and every external secret source inject straight
    into ``os.environ`` from the same profile. A host passing only the private key while the profile's
    ``.op.env`` or vault mapping supplies ``BUZZ_AUTH_TAG`` produces exactly the split identity this whole
    path exists to prevent. ``buzz_before_load`` is the pre-load baseline, taken by
    :func:`load_hermes_dotenv` before anything in the load runs, and diffing against it covers all four
    supply routes in one rule while keeping the runtime carve-out: a ``BUZZ_RELAY_URL`` set before the
    load is not the profile completing an identity, so it survives.

    Logs key names, never values, once per process."""
    global _ACP_RESTORE_LOGGED
    snapshot = _ACP_HOST_ENV
    if not snapshot:
        return
    # DROP FIRST, RE-ASSERT SECOND, and that order is load bearing rather than cosmetic. The two loops
    # touch disjoint names (``dropped`` excludes everything in ``snapshot`` by construction), so the end
    # state is identical either way and only the INTERMEDIATE state differs. Re-asserting first published
    # the host's managed key beside the profile owner's attestation for the length of a dict scan: the
    # exact pairing this module exists to refuse, visible to every non-loading reader, which take no lock
    # and read ``os.environ`` directly. That is reachable by the real consumer and not only by
    # instrumentation, because ``load_hermes_dotenv`` runs on background threads in an ACP process (see
    # ``_ACP_HOST_ENV`` above) while the main thread resolves the identity pair for a Buzz send.
    # Dropping first makes the intermediate state the profile's key with no attestation, or its mirror,
    # which an owner-gated relay REFUSES rather than accepts. Pinned by
    # test_acp_restore_never_shows_a_reader_a_split_identity.
    dropped: list[str] = []
    if _host_owns_buzz_identity():
        dropped = sorted(
            k for k in os.environ
            if k in _BUZZ_IDENTITY_DROP_KEYS
            and k not in snapshot
            and k not in buzz_before_load
        )
        for key in dropped:
            del os.environ[key]

    replaced = [k for k, v in snapshot.items() if os.environ.get(k) != v]
    for key in replaced:
        os.environ[key] = snapshot[key]
    if _host_owns_buzz_identity():
        _warn_if_host_claim_has_no_key()

    if (replaced or dropped) and not _ACP_RESTORE_LOGGED:
        _ACP_RESTORE_LOGGED = True
        logger.debug("acp: host-owned env kept over profile .env for %s%s",
                     ", ".join(sorted(replaced)) or "(none)",
                     f"; dropped profile-supplied Buzz keys {', '.join(dropped)}" if dropped else "")


def _warn_if_host_claim_has_no_key() -> None:
    """A host that supplies ``BUZZ_AUTH_TAG`` and no ``BUZZ_PRIVATE_KEY`` claims the identity and cannot
    sign it. Failing closed is right, an attestation belongs to one key; failing SILENTLY is not. The
    profile's key has just been deleted, and the only thing the operator sees downstream is Buzz's generic
    "must be configured" error with nothing pointing at the cause. Once per process, names only."""
    global _ACP_KEYLESS_CLAIM_LOGGED
    if _ACP_KEYLESS_CLAIM_LOGGED or "BUZZ_PRIVATE_KEY" in _ACP_HOST_ENV:
        return
    _ACP_KEYLESS_CLAIM_LOGGED = True
    logger.warning(
        "acp: the ACP host claimed the Buzz identity with BUZZ_AUTH_TAG but supplied no "
        "BUZZ_PRIVATE_KEY, so the profile's key was dropped and not replaced. Buzz sends will fail "
        "until the host passes the signing key that owns that attestation, or you unset BUZZ_AUTH_TAG "
        "on the host (HERMES_ACP_HOST_ENV=0 restores the profile .env precedence entirely)."
    )


def _buzz_env_names() -> frozenset[str]:
    """Identity-group names the environment PROVIDES before the load; the baseline
    :func:`_restore_acp_host_env` diffs against. Scoped to ``_BUZZ_IDENTITY_DROP_KEYS`` because those are the
    only names the restore can delete, so those are the only names a baseline has to protect.

    "Provides" is :func:`_env_provides`, the same test :func:`_snapshot_acp_host_env` applies, and they have
    to agree: a name in the baseline is one the drop rule will spare, and a name in the snapshot is one the
    host owns. Reading a blank value as present here while the snapshot read it as absent meant a host
    exporting ``BUZZ_AUTH_TAG=""`` beside a real managed key both disclaimed that name and shielded the
    profile's replacement for it, so the ``override=True`` load handed the managed key the profile owner's
    attestation."""
    return frozenset(k for k in _BUZZ_IDENTITY_DROP_KEYS if _env_provides(k))


def _env_keys_defined_in_dotenv(path: Path) -> set[str]:
    """KEY names assigned in a dotenv file (including empty ``KEY=``). A fast line scanner (works in early
    bootstrap without python-dotenv); decode errors fall back to latin-1 like ``_load_dotenv_with_fallback``.

    Quoted values may span lines (``BUZZ_AUTH_TAG='{\\n  "ok": 1\\n}'``), and a continuation line holding an
    ``=`` used to parse as its own assignment, inventing key names that were never defined. Track the open
    quote and skip the body.

    A quote is open only when it is never CLOSED in the rest of the value, not when the value fails to END
    with it: ``KEY="v" # note`` is a terminated value with an inline comment, the shape ``hermes setup``
    writes, and testing ``endswith`` there swallowed every following line until the next quote character.
    That deletes keys the file genuinely defines, via ``_clear_known_keys_missing_from_dotenv``, on every
    ordinary run rather than only under an ACP host."""
    keys: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            text = path.read_text(encoding="latin-1", errors="replace")
        except Exception:
            return keys
    open_quote = ""
    for raw_line in text.splitlines():
        if open_quote:  # inside a multi-line quoted value: not assignments, whatever they contain
            if open_quote in raw_line:
                open_quote = ""
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not key:
            continue
        keys.add(key)
        value = value.lstrip()
        quote = value[:1]
        if quote in ("'", '"') and quote not in value[1:]:
            open_quote = quote
    return keys


def _clear_known_keys_missing_from_dotenv(path: Path) -> None:
    """After ``.env`` loaded with override, delete inherited ``_PROFILE_MANAGED_ENV_KEYS`` it does not
    define. Deliberately NARROW: only keys that change *which provider path* is used.

    Does **not** run when the ``.env`` file does not exist (bare-profile case, which follows ``#66930`` /
    ``#67027`` semantics).
    """
    if not path.exists():
        return
    defined = _env_keys_defined_in_dotenv(path)
    for key in _PROFILE_MANAGED_ENV_KEYS:
        if key not in defined and key in os.environ:
            del os.environ[key]


def get_secret_source(env_var: str) -> str | None:
    """Source label that supplied ``env_var`` (``"bitwarden"`` …), None for .env/shell keys. Metadata only —
    never authorization to persist the raw value."""
    return _SECRET_SOURCES.get(env_var)


def get_secret_source_values(hermes_home: str | os.PathLike) -> dict[str, str]:
    """Return the external-secret value snapshot for ``hermes_home``."""
    return dict(_SECRET_SOURCE_VALUES_BY_HOME.get(str(Path(hermes_home).resolve()), {}))


def hydrate_profile_secret_sources(hermes_home: str | os.PathLike) -> dict[str, str]:
    """Resolve one profile's configured sources without mutating ``os.environ``: multiplex gateways route
    turns to profiles that never ran the process-global dotenv path, so resolve against a private mapping
    seeded from that ``.env`` and record the per-home snapshot for ``build_profile_secret_scope()``.
    Fail-open / once-per-home like ``_apply_external_secret_sources``; never returns plaintext .env entries."""
    with _SECRET_SOURCE_CACHE_LOCK:
        return _hydrate_profile_secret_sources(Path(hermes_home))


def _hydrate_profile_secret_sources(home: Path) -> dict[str, str]:
    """Locked implementation for :func:`hydrate_profile_secret_sources`."""
    home_key = str(home.resolve())
    if home_key in _APPLIED_HOMES:
        return get_secret_source_values(home)

    try:
        cfg = _load_secrets_config(home)
    except Exception:  # noqa: BLE001 — external sources must not block routing
        return {}
    if not cfg:
        return {}

    try:
        from agent.secret_scope import _is_global_env, load_env_file
        from agent.secret_sources.registry import apply_all

        local_env = {name: value for name, value in os.environ.items() if _is_global_env(name)}
        local_env.update(load_env_file(home / ".env"))
        # Mirror load_hermes_dotenv()'s .op.env bootstrap (1Password token lives in gitignored .op.env)
        # or cold profiles fail 1Password hydration. .env wins.
        # Without seeding it here a cold profile configured for the supported .op.env flow fails 1Password
        # hydration (sweeper review on #74549). .env values win — never override an existing key.
        op_env = home / ".op.env"
        if op_env.exists():
            for _name, _value in load_env_file(op_env).items():
                local_env.setdefault(_name, _value)
        local_env["HERMES_HOME"] = str(home)
        report = apply_all(cfg, home, environ=local_env)
    except Exception:  # noqa: BLE001 — preserve fail-open startup behavior
        return {}

    if not report.sources:
        return {}

    _APPLIED_HOMES.add(home_key)
    values: dict[str, str] = {}
    for name, applied in report.provenance.items():
        value = local_env.get(name)
        if value is None:
            continue
        _SECRET_SOURCES[name] = applied.source
        values[name] = value
    if values:
        _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values
    return dict(values)


def reset_secret_source_cache() -> None:
    """Forget applied homes so the next load re-pulls (tests, long-running processes after config edits)."""
    _APPLIED_HOMES.clear()
    _SECRET_SOURCES.clear()
    _SECRET_SOURCE_VALUES_BY_HOME.clear()


def format_secret_source_suffix(env_var: str) -> str:
    """``" (from Bitwarden)"``-style suffix; ``""`` for .env/shell keys (only external sources are named)."""
    source = get_secret_source(env_var)
    if not source:
        return ""
    if source == "bitwarden":
        return " (from Bitwarden)"
    # Registry label (e.g. "1Password"); raw name for unknown sources (uninstalled plugin, tests).
    try:
        from agent.secret_sources.registry import get_source

        registered = get_source(source)
        if registered is not None and registered.label:
            return f" (from {registered.label})"
    except Exception:  # noqa: BLE001 — label lookup must never raise
        pass
    return f" (from {source})"


def _format_offending_chars(value: str, limit: int = 3) -> str:
    """Compact ``U+XXXX ('c'), ...`` summary of non-ASCII codepoints."""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f"U+{ord(ch):04X}"
            if ch.isprintable():
                label += f" ({ch!r})"
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ", ".join(seen)


def _sanitize_loaded_credentials() -> None:
    """Strip non-ASCII from credential env vars (``_CREDENTIAL_SUFFIXES``) so the codebase never sees them.

    Emits a one-line warning to stderr when characters are stripped. Silent stripping would mask copy-paste
    corruption (Unicode lookalike glyphs from PDFs / rich-text editors, ZWSP from web pages) as opaque
    provider-side "invalid API key" errors (see #6843).
    """
    for key, value in list(os.environ.items()):
        cleaned = _sanitize_credential_value(key, value)
        if cleaned != value:
            os.environ[key] = cleaned


def _sanitize_credential_value(key: str, value: str) -> str:
    """ASCII-clean ONE credential value (warning once per key); any other name or an already-ASCII value is
    returned untouched. Shared by :func:`_sanitize_loaded_credentials` and the ACP host-env snapshot, which
    must clean the value it will later re-assert rather than re-installing the raw one."""
    if not any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES) or value.isascii():
        return value
    cleaned = value.encode("ascii", errors="ignore").decode("ascii")
    if key in _WARNED_KEYS:
        return cleaned
    _WARNED_KEYS.add(key)
    stripped = len(value) - len(cleaned)
    detail = _format_offending_chars(value) or "non-printable"
    print(f"  Warning: {key} contained {stripped} non-ASCII character"
          f"{'s' if stripped != 1 else ''} ({detail}) — stripped so the "
          f"key can be sent as an HTTP header.", file=sys.stderr)
    print(
        "  This usually means the key was copy-pasted from a PDF, "
        "rich-text editor, or web page that substituted lookalike\n"
        "  Unicode glyphs for ASCII letters. If authentication fails "
        "(e.g. \"API key not valid\"), re-copy the key from the\n"
        "  provider's dashboard and run `hermes setup` (or edit the "
        ".env file in a plain-text editor).",
        file=sys.stderr,
    )
    return cleaned


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        # utf-8-sig strips a leading BOM (PowerShell 5.1 / Notepad); plain utf-8 would keep U+FEFF on the
        # first key name and silently drop it from os.environ under its canonical name.
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = path.read_bytes()  # strip the BOM by hand: utf-8-sig can't once we decode latin-1
        if raw.startswith(codecs.BOM_UTF8):
            raw = raw[len(codecs.BOM_UTF8) :]
        load_dotenv(stream=io.StringIO(raw.decode("latin-1")), override=override)
    _sanitize_loaded_credentials()  # httpx encodes headers as ASCII


def _sanitize_env_file_if_needed(path: Path) -> None:
    """Pre-sanitize a .env file before python-dotenv reads it. Sniffs a leading BOM *before* any text
    decode: UTF-16 (Notepad "Unicode") is rewritten as clean UTF-8; UTF-32 is refused (left untouched) so
    we never fall through to the errors=replace corruption path."""
    if not path.exists():
        return
    try:
        from hermes_cli.config import _sanitize_env_lines
    except ImportError:
        return  # early bootstrap — config module not available yet

    try:
        raw = path.read_bytes()
    except Exception:
        return

    # ORDER MATTERS: BOM_UTF32_LE (FF FE 00 00) startswith BOM_UTF16_LE (FF FE); UTF-16 first would mangle it.
    force_utf8_rewrite = False
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        # Lazy import keeps the module import block identical to #65124's codecs/io additions so the two PRs
        # auto-merge either order.
        path_key = str(path.resolve())
        if path_key not in _WARNED_UTF32_PATHS:
            _WARNED_UTF32_PATHS.add(path_key)
            logger.warning("Skipping .env sanitize for %s: UTF-32 BOM detected; "
                           "leaving file untouched to avoid corruption", path)
        return
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        # "utf-16" uses the BOM for endianness and strips it; newline=None matches open()'s universal
        # newlines (not splitlines()'s extra boundaries like U+2028) so sanitize sees the same lines.
        try:
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-16", newline=None) as f:
                original = f.readlines()
        except UnicodeDecodeError:
            return
        force_utf8_rewrite = True  # always rewrite UTF-16 as UTF-8 so the dotenv load sees a canonical file
    else:
        # utf-8-sig strips a UTF-8 BOM; errors=replace so embedded NULs can be stripped below.
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                original = f.readlines()
        except Exception:
            return
        # errors=replace turns undecodable leading bytes into U+FFFD; persisting would glue them onto
        # the first key name permanently — leave the file untouched instead.
        if original and original[0].startswith("\ufffd"):
            return

    try:
        # Strip NULs (os.environ raises ValueError on them); also repairs BOM-less UTF-16 (NUL-padded ASCII).
        stripped = [line.replace("\x00", "") for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original or force_utf8_rewrite:
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".env_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # best-effort — don't block gateway startup


def load_hermes_dotenv(
    *,
    hermes_home: str | os.PathLike | None = None,
    project_env: str | os.PathLike | None = None,
    load_external_secrets: bool = True,
) -> list[Path]:
    """Load Hermes env files: ``~/.hermes/.env`` overrides stale shell exports; project ``.env`` is a dev
    fallback that only fills gaps when the user env exists (and overrides shell vars when it does not).

    Exception: once :func:`mark_acp_hosted` ran, host-owned keys (``HERMES_HOME``, and ``BUZZ_*`` for a
    Buzz-managed agent) keep the value captured by the marker, and no profile source (``.env``, the project
    ``.env``, ``.op.env``, an external secret source) may complete a Buzz identity the host part-supplied.
    Under that marker the whole load is serialized: an ACP process loads dotenv from background threads
    (MCP discovery, session server registration) and an unserialized loader would otherwise be visible to
    sibling LOADERS between the override load and the restore. Non-loading readers are covered by restoring
    the host identity as soon as the user ``.env`` has been read, not by the lock."""
    if not _ACP_HOSTED:
        return _load_hermes_dotenv(
            hermes_home=hermes_home, project_env=project_env,
            load_external_secrets=load_external_secrets,
        )
    with _ACP_ENV_LOCK:
        return _load_hermes_dotenv(
            hermes_home=hermes_home, project_env=project_env,
            load_external_secrets=load_external_secrets,
        )


def _load_hermes_dotenv(
    *,
    hermes_home: str | os.PathLike | None = None,
    project_env: str | os.PathLike | None = None,
    load_external_secrets: bool = True,
) -> list[Path]:
    """Body of :func:`load_hermes_dotenv`; call that instead (it owns the ACP serialization)."""
    home_path = Path(hermes_home or os.getenv("HERMES_HOME", Path.home() / ".hermes"))

    # Multiplex gateway: while a routed profile-home override is active, copying that profile's .env
    # into os.environ would expose its credentials to sibling turns and every spawned child. Unscoped
    # startup loads keep the normal path; external sources still refresh against the profile mapping.
    from agent.secret_scope import is_multiplex_active
    from hermes_constants import get_hermes_home_override

    if is_multiplex_active() and get_hermes_home_override() is not None:
        home_key = str(home_path.resolve())
        if home_key not in _SCOPED_SKIP_LOGGED:
            _SCOPED_SKIP_LOGGED.add(home_key)
            logger.debug("multiplex: skipping process-global dotenv load for routed "
                         "profile home %s (credentials resolve via the profile scope)", home_path)
        if load_external_secrets:
            from hermes_cli import _early_recovery

            if not _early_recovery._should_skip_external_secret_sources():
                hydrate_profile_secret_sources(home_path)
        return []

    loaded: list[Path] = []
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None
    # Baseline for the all-or-nothing Buzz group: names present BEFORE anything in this load ran. Taken
    # here, not inside the restore, because by then the profile's names are indistinguishable from the
    # host's. See _restore_acp_host_env.
    buzz_before_load = _buzz_env_names() if _ACP_HOSTED else frozenset()

    if user_env.exists():  # normalize formatting / strip NULs before parsing
        _sanitize_env_file_if_needed(user_env)
    if project_env_path and project_env_path.exists():
        _sanitize_env_file_if_needed(project_env_path)

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)
        _clear_known_keys_missing_from_dotenv(user_env)  # mirrors reload_env(): inherited keys must not leak
    # Restore IMMEDIATELY, not only at the end of the load. The lock below serializes loaders, but every
    # consumer of the identity reads os.environ directly and takes no lock: the Buzz plugin signing a
    # kind-22242 event, _sanitize_subprocess_env building a terminal child's env, hermes_subprocess_env.
    # Between this override=True load and the tail restore sit the project .env, the config re-parse and
    # a possible Bitwarden/1Password network round trip, and a reader landing in that window would see
    # the profile's key. Closing the window at its source costs one dict pass.
    #
    # OUTSIDE the `if user_env.exists()` block on purpose: a bare profile home with no .env is a supported
    # shape (#66930 / #67027 semantics, see _clear_known_keys_missing_from_dotenv), and main.py always
    # passes project_env=PROJECT_ROOT/".env", which then loads with override=not loaded, i.e. True. Gating
    # this call on the user .env left that case with no early restore at all.
    _restore_acp_host_env(buzz_before_load)

    # .op.env AFTER .env so .env wins, but the bootstrap OP_SERVICE_ACCOUNT_TOKEN reaches
    # apply_onepassword_secrets() even in cron with no shell state; gitignored so the token never enters
    # the committed .env. override=False lets a systemd `EnvironmentFile=-…/.op.env` token win.
    op_env = home_path / ".op.env"
    if op_env.exists() and not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        _load_dotenv_with_fallback(op_env, override=False)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    # Second restore, before the vault round trip. .op.env and the project .env are two more profile
    # sources that can put an identity member into os.environ, and _apply_external_secret_sources below is
    # the long call in this function: a Bitwarden or 1Password fetch is a network round trip, and that is
    # the widest window a lock-free reader can land in. Same helper, idempotent, no-op when the host
    # supplied nothing.
    _restore_acp_host_env(buzz_before_load)

    # External sources are skipped for the updater (dotenv + managed env still load): ``update`` must not
    # import optional secret-manager libs (Bitwarden → cryptography → _rust.pyd) into the process replacing
    # that env on Windows, and a fresh retry after a deferred dependency install would otherwise make the
    # self-lock preflight exit 2 again.
    from hermes_cli import _early_recovery

    # External secret sources are skipped in two updater situations: 1. ``load_external_secrets=False`` —
    # the caller is an ``update`` invocation that must not import optional secret-manager libraries
    # (Bitwarden → cryptography → ``_rust.pyd``) into the process that replaces that same environment on
    # Windows (#73381, #86735). 2. A fresh ``hermes update`` retry just completed a deferred dependency
    # install before importing this module. Do not remap native secret-source dependencies in that same
    # updater process or the self-lock preflight will recreate the marker and exit 2 again. Dotenv and
    # managed env still load in both cases; only external source resolution is unnecessary for the updater.
    if load_external_secrets and not _early_recovery._should_skip_external_secret_sources():
        _apply_external_secret_sources(home_path)

    # Host-owned keys win over user/project .env while ACP-hosted (see _ACP_HOST_OWNED_ENV_KEYS). Placed
    # AFTER the external secret sources (a profile mapping BUZZ_PRIVATE_KEY from a vault with
    # ``override_existing: true`` would otherwise clobber the host identity right after the restore,
    # agent/secret_sources/registry.py) and BEFORE the managed overlay, so an admin-managed .env keeps
    # its documented top-of-stack precedence. The belt to the braces above: this is the call that sees the
    # project .env, .op.env and the external secret sources.
    _restore_acp_host_env(buzz_before_load)
    _apply_managed_env()

    # config.yaml owns terminal.*, but the override=True loads above let a stale TERMINAL_ENV=docker in
    # ~/.hermes/.env win on every reload and flip the backend mid-session in long-lived processes.
    # Re-apply the explicit terminal keys LAST, after the managed overlay, so the merged config lands.
    # config.yaml is the documented source of truth for terminal.* settings, but the dotenv loads above run
    # with override=True — so a stale TERMINAL_ENV=docker left in ~/.hermes/.env (e.g. written by an older
    # `hermes setup` before the user switched terminal.backend in config.yaml) silently wins again on every
    # reload. Startup launchers bridge config→env once, but long-lived processes (gateway per-turn reload,
    # cron standalone runs) call load_hermes_dotenv() repeatedly and used to flip the effective backend back
    # to the stale .env value mid-session (#29186, #67323).
    _reapply_terminal_config_bridge(home_path)

    return loaded


def _reapply_terminal_config_bridge(home_path: Path) -> None:
    """Re-assert config.yaml's explicit ``terminal.*`` keys over reloaded .env via the single shared bridge
    ``apply_terminal_config_to_env`` (also used by terminal_tool and the TUI/dashboard launchers) so the
    semantics can't drift between sites."""
    try:
        if Path(home_path).resolve() != _process_hermes_home().resolve():
            return
        from hermes_cli.config import apply_terminal_config_to_env

        apply_terminal_config_to_env(env=None)
    except Exception:  # noqa: BLE001 — early bootstrap / malformed config
        pass


def _apply_managed_env() -> None:
    """Apply the managed-scope .env last, with override, so it beats user/shell. Does NOT stop the agent
    from later mutating os.environ (v1 relies on filesystem permissions). Fail-open: never blocks startup.

    The admin overlay outranks the ACP host, and that stays true, but when it claims the Buzz identity it
    claims the whole group: see :func:`_settle_buzz_identity_after_managed_env`. This is the last writer of
    ``BUZZ_*`` in the load, so it is the last place the all-or-nothing rule has to hold."""
    try:
        from hermes_cli import managed_scope

        managed_dir = managed_scope.get_managed_dir()
    except Exception:  # noqa: BLE001 — managed scope must never block startup
        return
    if managed_dir is None:
        return
    managed_env = managed_dir / ".env"
    if not managed_env.exists():
        return
    _sanitize_env_file_if_needed(managed_env)
    managed_names = _env_keys_defined_in_dotenv(managed_env) if acp_host_owns_buzz_identity() else set()
    # Settle BEFORE the load as well as after. The overlay claims the group by NAME, and the names it
    # claims are known from the file scan before a single value is read, so the members it does not
    # define can go first and the mixed state never exists. Settling only afterwards left the host's key
    # beside the admin's attestation for a whole dotenv read and parse, which is a far wider window than
    # the one inside _restore_acp_host_env and reachable by the same non-loading readers.
    # The second call is not redundant: _env_keys_defined_in_dotenv is a line scanner, and any name it
    # under-reports is a name the load itself would re-introduce after the pre-clear.
    _settle_buzz_identity_after_managed_env(managed_names)
    _load_dotenv_with_fallback(managed_env, override=True)
    _settle_buzz_identity_after_managed_env(managed_names)


def _settle_buzz_identity_after_managed_env(managed_names: set[str]) -> None:
    """Apply the all-or-nothing identity rule to the managed overlay, the one writer of ``BUZZ_*`` that runs
    AFTER the final :func:`_restore_acp_host_env`.

    ``/etc/hermes/.env`` beats the host by design (admin lockdown, root-owned, documented top of stack) and
    that precedence is unchanged. What changes is the granularity. The overlay used to win one NAME at a
    time, so a managed file carrying only ``BUZZ_AUTH_TAG`` left the host's managed key signing the admin's
    attestation: the same split identity this module refuses from the profile, reached by the one writer
    positioned after every restore. The admin is a third principal, and the rule does not care which two
    principals are mixed.

    An overlay that defines a SIGNING member (``_BUZZ_IDENTITY_TRIGGER_KEYS``) has claimed the identity, so
    it owns the group and the members it did not define are removed. An overlay that defines none has
    claimed nothing, and keeps its ordinary per-name precedence: ``BUZZ_RELAY_URL`` alone is an org relay
    pointed at the host's own key, which is exactly the admin lockdown this overlay is for, and
    ``_host_owns_buzz_identity`` refuses to read a bare relay URL as a claim for the same reason everywhere
    else.

    Gated on :func:`acp_host_owns_buzz_identity` via the caller: with no host claim there is only one
    principal supplying the identity and the overlay is ordinary admin precedence over the profile owner's
    own files, which a single-profile machine legitimately uses to centralize the attestation."""
    if not managed_names & _BUZZ_IDENTITY_TRIGGER_KEYS:
        return
    for key in sorted(_BUZZ_IDENTITY_DROP_KEYS - managed_names):
        os.environ.pop(key, None)


def _apply_external_secret_sources(home_path: Path) -> None:
    """Pull secrets from every enabled external source into env — AFTER dotenv (sources need .env bootstrap
    tokens), BEFORE Hermes reads credentials; failures never block startup. Precedence/conflicts/provenance
    live in ``registry.apply_all``; this wrapper owns the once-per-home guard, the post-apply ASCII sweep,
    the ``_SECRET_SOURCES`` map and status lines."""
    home_key = str(Path(home_path).resolve())
    if home_key in _APPLIED_HOMES:
        return

    # Neither early return marks the home applied: a malformed config.yaml would otherwise permanently
    # disable secret loading for this process, and an unmarked home picks up a config change on the next
    # load (the re-parse is a cheap fast_safe_load).
    try:
        cfg = _load_secrets_config(home_path)
    except Exception:  # noqa: BLE001 — config errors must not block startup
        # See #40597.
        return
    if not cfg:
        return

    # Defer the registry import until a source is enabled — bitwarden eagerly loads cryptography._rust.pyd,
    # which makes the Windows updater self-lock before its preflight. Detect by *shape* (dict with enabled
    # flag), not names, so plugin/test sources pass and a plain dict entry never forces the crypto load.
    any_enabled = any(isinstance(v, dict) and v.get("enabled") is True for v in cfg.values())
    if not any_enabled:
        return

    try:
        from agent.secret_sources.registry import apply_all
    except ImportError:
        return

    try:
        report = apply_all(cfg, home_path)
    except Exception:  # noqa: BLE001 — belt-and-braces; apply_all shouldn't raise
        return

    if not report.sources:  # no source enabled: keep retrying cheaply so flipping one on takes effect
        return

    # A real fetch attempt happened (success OR error): mark the home so the 3-5 import-time calls per
    # startup don't re-fetch / re-print (error retries are opt-in via reset_secret_source_cache()).
    # Marking AFTER the attempt keeps the earlier failure paths retryable.
    _APPLIED_HOMES.add(home_key)

    # A real fetch attempt happened (success OR error). Mark the home now so the 3-5 import-time
    # load_hermes_dotenv() calls per startup don't re-fetch / re-print — error retries within one process
    # are opt-in via reset_secret_source_cache(). Marking AFTER the attempt (not before, see #40597) is what
    # lets the earlier failure paths stay retryable.
    if report.applied_any:
        _sanitize_loaded_credentials()  # vault values carry the same copy-paste corruption risk as .env
        # Re-run the ASCII sanitization pass: vault values are user-supplied and might have the same
        # copy-paste corruption as a manually edited .env (see #6843).
        values: dict[str, str] = {}
        for name, applied in report.provenance.items():
            _SECRET_SOURCES[name] = applied.source
            if name in os.environ:
                values[name] = os.environ[name]
        _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values

    for src in report.sources:
        if src.applied:
            print(f"  {src.label}: applied {len(src.applied)} "
                  f"secret{'s' if len(src.applied) != 1 else ''}", file=sys.stderr)
        if src.result.error:
            print(f"  {src.label}: {src.result.error}", file=sys.stderr)
            hint = _remediation_hint(src.name, src.result.error_kind, cfg, scope=home_key)
            if hint:
                print(f"  {src.label}: → {hint}", file=sys.stderr)
        for warn in src.result.warnings:
            print(f"  {src.label}: {warn}", file=sys.stderr)
    for conflict in report.conflicts:
        print(f"  Secret sources: {conflict}", file=sys.stderr)


def _remediation_hint(source_name: str, error_kind, secrets_cfg: dict, *, scope: str | None = None) -> str:
    """The failed source's one-line fix-it hint; a plugin remediation() could raise and startup must not."""
    try:
        from agent.secret_sources.registry import get_source

        source = get_source(source_name, scope=scope)
        if source is None:
            return ""
        src_cfg = secrets_cfg.get(source_name)
        src_cfg = src_cfg if isinstance(src_cfg, dict) else {}
        return str(source.remediation(error_kind, src_cfg) or "").strip()
    except Exception:  # noqa: BLE001 — hints must never block startup
        return ""


def _load_secrets_config(home_path: Path) -> dict:
    """Read just the ``secrets:`` section of config.yaml, isolated so a malformed config can't break dotenv."""
    config_path = home_path / "config.yaml"
    if not config_path.exists():
        return {}
    # Prefer the shared raw-config cache: this is the first config.yaml read of a normal startup, so
    # populating it lets main.py's early bridge and hermes_logging reuse one parse instead of 3-4.
    if home_path == _process_hermes_home():
        try:
            from hermes_cli.config import read_raw_config

            data = read_raw_config() or {}
            return data.get("secrets") or {}
        except Exception:
            pass
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = fast_safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data.get("secrets") or {}


def _process_hermes_home() -> Path:
    """The HERMES_HOME the running process was launched under.

    Must be the *true* process home, ignoring any context-local
    ``set_hermes_home_override`` a per-request task has installed. Both
    callers depend on that:

    * ``_reapply_terminal_config_bridge`` guards "only re-bridge config into
      the shared ``os.environ`` when THIS load is for the process's own
      profile". If this followed the task override, a per-turn handler scoped
      to a *secondary* profile (the multiplex dashboard serving every profile
      from one process) would satisfy the guard and bridge that profile's
      ``terminal.backend`` into the shared env — e.g. a ``local`` sibling
      profile clobbering the launch profile's ``TERMINAL_ENV=ssh`` while
      leaving its ``TERMINAL_SSH_*`` untouched, so the session silently runs
      commands locally (cross-profile terminal-backend leak).
    * ``_load_secrets_config`` uses it to decide whether the shared
      (mtime,size)-keyed config cache is safe to reuse; under an override it
      must fall through to an isolated parse of the scoped profile.

    ``hermes_constants.get_process_hermes_home()`` is the override-immune
    resolver built for exactly this; delegate to it.
    """
    try:
        from hermes_constants import get_process_hermes_home

        return get_process_hermes_home()
    except Exception:
        return Path.home() / ".hermes"
