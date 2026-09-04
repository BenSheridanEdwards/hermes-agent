"""Conservative validators for values crossing metadata boundaries."""

from __future__ import annotations

import re
from typing import Pattern


_SENSITIVE_MARKER = re.compile(
    r"(?:authorization|bearer|cookie|token|secret|password|api[-_ ]?key)",
    re.IGNORECASE,
)
_JWT_LIKE = re.compile(
    r"(?:^|[:= /])"
    r"[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,}){2,}"
    r"(?:$|[:= /])"
)
_PREFIXED_CREDENTIAL = re.compile(
    r"(?:^|[:= /])"
    r"(?:sk(?:-[A-Za-z0-9_-]+){1,}|(?:gh[opsu]|github_pat|xox[a-z])[_-][A-Za-z0-9_-]{8,})"
    r"(?:$|[:= /])",
    re.IGNORECASE,
)
_OPAQUE_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{32,}")
_CREDENTIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


def is_safe_metadata_value(
    value: object,
    *,
    syntax: Pattern[str],
    max_length: int = 512,
) -> bool:
    """Accept bounded metadata syntax only when it is not credential-shaped."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or not value.isascii()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        or not syntax.fullmatch(value)
        or "@" in value
        or value.startswith(("{", "["))
        or _SENSITIVE_MARKER.search(value)
        or _JWT_LIKE.search(value)
        or _PREFIXED_CREDENTIAL.search(value)
    ):
        return False

    candidates = re.split(r"[:= /]", value)
    return not any(_OPAQUE_CREDENTIAL.fullmatch(candidate) for candidate in candidates)


def safe_metadata_credential_id(value: object) -> str | None:
    """Return a public-safe pool entry ID, otherwise fail closed."""
    if isinstance(value, str) and is_safe_metadata_value(
        value, syntax=_CREDENTIAL_ID, max_length=256
    ):
        return value
    return None
