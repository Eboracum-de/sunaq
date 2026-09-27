"""Read secrets from mounted files with environment compatibility fallback.

For secret NAME, NAME_FILE takes precedence when set. This supports Docker/
Compose secrets and systemd credentials without requiring secret values in the
process/container configuration. Direct NAME remains accepted for existing
native installations and upgrades.
"""
from __future__ import annotations

import os
from pathlib import Path


def secret_env(name: str, default: str = "") -> str:
    key = str(name or "").strip()
    if not key:
        raise ValueError("secret environment name is required")

    file_name = str(os.getenv(f"{key}_FILE", "") or "").strip()
    if file_name:
        path = Path(file_name)
        try:
            # Secret files conventionally end in one newline. Preserve all
            # other characters because API keys/passwords may contain spaces.
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError(
                f"{key}_FILE is configured but cannot be read: {path}"
            ) from exc

    value = os.getenv(key)
    return default if value is None else str(value)


def secret_configured(name: str) -> bool:
    return bool(secret_env(name, "").strip())
