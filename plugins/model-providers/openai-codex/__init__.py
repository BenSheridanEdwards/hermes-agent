"""OpenAI Codex (Responses API) provider profile."""

import re
from collections.abc import Callable

from providers import register_provider
from providers.base import ProviderProfile


_SENSITIVE_VALUE = re.compile(
    r"(?:authorization|bearer|cookie|token|secret|password|api[-_ ]?key)",
    re.IGNORECASE,
)
_IDENTIFIER_VALUE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_INTEGER_VALUE = re.compile(r"(?:0|[1-9][0-9]{0,18})")
_DECIMAL_VALUE = re.compile(r"(?:0|[1-9][0-9]{0,18})(?:\.[0-9]{1,6})?")
_LIMIT_NAME_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:/ -]{0,127}")


def _contains_sensitive_value(value: str) -> bool:
    return bool(
        "@" in value
        or value.startswith(("{", "["))
        or _SENSITIVE_VALUE.search(value)
    )


def _identifier_value(value: str) -> bool:
    return bool(
        _IDENTIFIER_VALUE.fullmatch(value)
        and not _contains_sensitive_value(value)
    )


def _integer_value(value: str) -> bool:
    return bool(_INTEGER_VALUE.fullmatch(value))


def _decimal_value(value: str) -> bool:
    return bool(_DECIMAL_VALUE.fullmatch(value))


def _percentage_value(value: str) -> bool:
    return bool(_DECIMAL_VALUE.fullmatch(value) and float(value) <= 100)


def _boolean_value(value: str) -> bool:
    return value in {"true", "false"}


def _limit_name_value(value: str) -> bool:
    return bool(
        _LIMIT_NAME_VALUE.fullmatch(value)
        and not _contains_sensitive_value(value)
    )


def _tier_validators(prefix: str) -> dict[str, Callable[[str], bool]]:
    validators: dict[str, Callable[[str], bool]] = {}
    for tier in ("primary", "secondary"):
        tier_prefix = f"{prefix}-{tier}"
        validators.update(
            {
                f"{tier_prefix}-allowed": _boolean_value,
                f"{tier_prefix}-limit-reached": _boolean_value,
                f"{tier_prefix}-used-percent": _percentage_value,
                f"{tier_prefix}-window-minutes": _integer_value,
                f"{tier_prefix}-reset-after-seconds": _integer_value,
                f"{tier_prefix}-reset-at": _integer_value,
                f"{tier_prefix}-over-secondary-limit-percent": _decimal_value,
            }
        )
    return validators


_CODEX_HEADER_VALIDATORS = {
    "retry-after": _integer_value,
    "x-codex-active-limit": _identifier_value,
    "x-codex-credits-balance": _decimal_value,
    "x-codex-credits-has-credits": _boolean_value,
    "x-codex-credits-unlimited": _boolean_value,
    "x-codex-plan-type": _identifier_value,
    **_tier_validators("x-codex"),
}


def _named_limit_validators(
    namespace: str,
) -> dict[str, Callable[[str], bool]]:
    prefix = f"x-codex-{namespace}"
    return {
        f"{prefix}-limit-name": _limit_name_value,
        **_tier_validators(prefix),
    }


class OpenAICodexProfile(ProviderProfile):
    """Project only the active named Codex limit, never arbitrary namespaces."""

    def filter_observed_response_headers(self, headers):
        projected = super().filter_observed_response_headers(headers)
        active_limit = projected.get("x-codex-active-limit", "").strip().lower()
        if not active_limit:
            return projected

        namespace = active_limit.removeprefix("codex_").replace("_", "-")
        if not namespace:
            return projected
        dynamic_validators = _named_limit_validators(namespace)
        strict_profile = ProviderProfile(
            name=self.name,
            observed_response_header_names=(
                *self.observed_response_header_names,
                *dynamic_validators,
            ),
            observed_response_header_validators={
                **self.observed_response_header_validators,
                **dynamic_validators,
            },
        )
        return strict_profile.filter_observed_response_headers(headers)


openai_codex = OpenAICodexProfile(
    name="openai-codex",
    aliases=("codex", "openai_codex"),
    api_mode="codex_responses",
    env_vars=(),  # OAuth external — no API key
    base_url="https://chatgpt.com/backend-api/codex",
    auth_type="oauth_external",
    observed_response_header_names=(
        "retry-after",
        "x-codex-active-limit",
        "x-codex-credits-balance",
        "x-codex-credits-has-credits",
        "x-codex-credits-unlimited",
        "x-codex-plan-type",
        "x-codex-primary-allowed",
        "x-codex-primary-limit-reached",
        "x-codex-primary-over-secondary-limit-percent",
        "x-codex-primary-reset-after-seconds",
        "x-codex-primary-reset-at",
        "x-codex-primary-used-percent",
        "x-codex-primary-window-minutes",
        "x-codex-secondary-allowed",
        "x-codex-secondary-limit-reached",
        "x-codex-secondary-reset-after-seconds",
        "x-codex-secondary-reset-at",
        "x-codex-secondary-used-percent",
        "x-codex-secondary-window-minutes",
    ),
    observed_response_header_validators=_CODEX_HEADER_VALIDATORS,
)

register_provider(openai_codex)
