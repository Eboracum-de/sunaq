"""Machine credentials for trusted calls into the middleware API."""
from __future__ import annotations

import secrets
from typing import Mapping

from rag.secret_env import secret_env

ENV_NAME = "RAG_INTERNAL_API_KEY"
HEADER_NAME = "X-AKI-Internal-Key"
PROVIDER_ENV_NAME = "RAG_PROVIDER_INTERNAL_KEY"
PROVIDER_HEADER_NAME = "X-AKI-Provider-Key"
MIN_KEY_LENGTH = 32


def _key(name: str) -> str:
    return secret_env(name, "").strip()


def internal_api_key() -> str:
    return _key(ENV_NAME)


def provider_internal_key() -> str:
    return _key(PROVIDER_ENV_NAME)


def internal_api_configured() -> bool:
    return len(internal_api_key()) >= MIN_KEY_LENGTH


def provider_api_configured() -> bool:
    return len(provider_internal_key()) >= MIN_KEY_LENGTH


def internal_api_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the base machine-auth header used by trusted internal callers."""
    key = internal_api_key()
    if len(key) < MIN_KEY_LENGTH:
        raise RuntimeError(
            f"{ENV_NAME} is missing or too short; internal middleware calls are disabled"
        )
    headers = {HEADER_NAME: key}
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def provider_api_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return headers proving both internal-client and trusted-provider roles."""
    headers = internal_api_headers()
    key = provider_internal_key()
    if len(key) < MIN_KEY_LENGTH:
        raise RuntimeError(
            f"{PROVIDER_ENV_NAME} is missing or too short; trusted-provider calls are disabled"
        )
    headers[PROVIDER_HEADER_NAME] = key
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def _valid(name: str, supplied: str | None) -> bool:
    expected = _key(name)
    candidate = str(supplied or "").strip()
    if len(expected) < MIN_KEY_LENGTH or len(candidate) < MIN_KEY_LENGTH:
        return False
    return secrets.compare_digest(candidate, expected)


def valid_internal_api_key(supplied: str | None) -> bool:
    return _valid(ENV_NAME, supplied)


def valid_provider_internal_key(supplied: str | None) -> bool:
    return _valid(PROVIDER_ENV_NAME, supplied)
