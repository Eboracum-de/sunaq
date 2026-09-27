"""Shared Elasticsearch connection/authentication helpers.

The middleware intentionally keeps Elasticsearch credentials out of config.yaml.
The administrator selects the username and an environment-variable name there;
the secret itself is supplied through runtime.env/systemd/container environment.
Legacy ``user``/``password`` keys remain accepted for compatibility.
"""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

from rag.secret_env import secret_env


def _section(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(cfg.get("elasticsearch") or cfg.get("es") or {})


def elastic_credentials(cfg: dict[str, Any]) -> tuple[str, str] | None:
    es = _section(cfg)
    username = str(es.get("username") or es.get("user") or "").strip()
    password_env = str(es.get("password_env") or "ELASTICSEARCH_PASSWORD").strip()
    password = ""
    if password_env:
        password = secret_env(password_env, "")
    if not password:
        # Compatibility only; new installations should not put secrets in YAML.
        password = str(es.get("password") or "")

    if not username and not password:
        return None
    if not username:
        raise RuntimeError("Elasticsearch password is configured but username is empty")
    if not password:
        source = password_env or "elasticsearch.password"
        raise RuntimeError(f"Elasticsearch username is configured but password is missing ({source})")
    return username, password


def elastic_verify(cfg: dict[str, Any], *, for_httpx: bool = False):
    es = _section(cfg)
    verify_tls = bool(es.get("verify_tls", True))
    if not verify_tls:
        return False
    ca_file = str(es.get("ca_file") or "").strip()
    if not ca_file:
        return True
    path = Path(ca_file)
    if not path.exists():
        raise RuntimeError(f"Elasticsearch CA file does not exist: {ca_file}")
    if for_httpx:
        return ssl.create_default_context(cafile=str(path))
    return str(path)


def requests_options(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"verify": elastic_verify(cfg, for_httpx=False)}
    auth = elastic_credentials(cfg)
    if auth:
        out["auth"] = auth
    return out


def httpx_options(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"verify": elastic_verify(cfg, for_httpx=True)}
    auth = elastic_credentials(cfg)
    if auth:
        out["auth"] = auth
    return out
