from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest
import yaml
from fastapi import HTTPException

import rag.maintenance_provider as maintenance


def _registry(tmp_path, api_key: str = "test-provider-key-" + "x" * 32):
    db = tmp_path / "users.sqlite"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE provider_clients ("
            "client_id TEXT PRIMARY KEY, api_key_hash TEXT NOT NULL UNIQUE, "
            "enabled INTEGER NOT NULL)"
        )
        con.execute(
            "INSERT INTO provider_clients(client_id,api_key_hash,enabled) VALUES(?,?,1)",
            ("frontend-a", hashlib.sha256(api_key.encode("utf-8")).hexdigest()),
        )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"auth": {"credential_store": str(db)}}), encoding="utf-8")
    return cfg, api_key


def test_maintenance_provider_authenticates_registered_client_without_normal_provider(monkeypatch, tmp_path):
    cfg, api_key = _registry(tmp_path)
    monkeypatch.setenv("RAG_CONFIG_FILE", str(cfg))
    assert maintenance._authenticate_client("Bearer " + api_key) == "frontend-a"
    with pytest.raises(HTTPException) as exc:
        maintenance._authenticate_client("Bearer wrong-key")
    assert exc.value.status_code == 401


def test_maintenance_provider_registry_access_is_read_only_and_missing_store_fails_closed(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"auth": {"credential_store": str(tmp_path / "missing.sqlite")}}), encoding="utf-8")
    monkeypatch.setenv("RAG_CONFIG_FILE", str(cfg))
    with pytest.raises(HTTPException) as exc:
        maintenance._authenticate_client("Bearer " + "x" * 32)
    assert exc.value.status_code == 503
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.asyncio
async def test_maintenance_chat_returns_stable_message_after_auth(monkeypatch, tmp_path):
    cfg, api_key = _registry(tmp_path)
    monkeypatch.setenv("RAG_CONFIG_FILE", str(cfg))
    body = maintenance.ChatCompletionRequest(
        model="nextcloud-hybrid-rag",
        messages=[{"role": "user", "content": "Suche Dokumente"}],
        stream=False,
    )
    result = await maintenance.chat_completions(body, authorization="Bearer " + api_key)
    assert result["choices"][0]["message"]["content"] == maintenance.MAINTENANCE_MESSAGE
    assert "Maintenance-Modus" in maintenance.MAINTENANCE_MESSAGE


@pytest.mark.asyncio
async def test_maintenance_health_is_explicit():
    result = await maintenance.health()
    assert result["status"] == "maintenance"
    assert result["maintenance"] is True


@pytest.mark.asyncio
async def test_maintenance_model_probe_reports_state_and_version(monkeypatch, tmp_path):
    cfg, api_key = _registry(tmp_path)
    monkeypatch.setenv("RAG_CONFIG_FILE", str(cfg))
    result = await maintenance.list_models(authorization="Bearer " + api_key)
    assert result["status"] == "maintenance"
    assert result["maintenance"] is True
    assert result["version"] == maintenance.VERSION
    assert len(result["data"]) == 1


def test_nginx_admin_returns_explicit_maintenance_status_when_api_is_down():
    root = Path(__file__).resolve().parents[1]
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        text = (root / "install" / "nginx" / name).read_text(encoding="utf-8")
        assert "proxy_intercept_errors on;" in text
        assert "error_page 502 504 = /rag-admin-unavailable;" in text
        assert "error_page 502 503 504 = /rag-admin-unavailable;" not in text
        assert "RAG-Admin derzeit nicht verfügbar" in text
