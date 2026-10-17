import os

import yaml

from rag import openai_provider as provider


def test_nextcloud_base_url_comes_from_config_when_env_missing(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"nextcloud": {"base_url": "https://cloud.example/"}}))
    monkeypatch.delenv("NEXTCLOUD_BASE_URL", raising=False)
    monkeypatch.setenv("RAG_CONFIG_FILE", str(config))
    assert provider._provider_config_nextcloud_base_url() == "https://cloud.example"


def test_nextcloud_base_url_env_override_wins(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"nextcloud": {"base_url": "https://config.example"}}))
    monkeypatch.setenv("RAG_CONFIG_FILE", str(config))
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://override.example/")
    assert provider._provider_config_nextcloud_base_url() == "https://override.example"


def _source_result(file_id="73", path="Test.pdf"):
    return provider.SearchResult(
        index=1,
        title=path.rsplit("/", 1)[-1],
        text="",
        raw={
            "path": path,
            "document_id": f"files:{file_id}",
            "nextcloud_openfile_id": file_id,
        },
    )


def test_nextcloud_source_url_uses_legacy_route_before_nc26(monkeypatch):
    monkeypatch.setattr(provider, "NEXTCLOUD_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(provider, "_nextcloud_major_version", lambda: 23)
    url = provider._nextcloud_source_url(_source_result(path="Folder/Test.pdf"))
    assert url == (
        "https://cloud.example/index.php/apps/files/"
        "?dir=%2FFolder&openfile=73"
    )


def test_nextcloud_source_url_uses_modern_route_from_nc26(monkeypatch):
    monkeypatch.setattr(provider, "NEXTCLOUD_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(provider, "_nextcloud_major_version", lambda: 34)
    url = provider._nextcloud_source_url(_source_result())
    assert url == (
        "https://cloud.example/apps/files/files/73"
        "?dir=%2F&editing=false&openfile=true"
    )


def test_document_id_from_url_accepts_legacy_and_modern_nextcloud_links():
    assert provider._document_id_from_url(
        "https://cloud.example/index.php/apps/files/?dir=%2F&openfile=73"
    ) == "files:73"
    assert provider._document_id_from_url(
        "https://cloud.example/apps/files/files/73?dir=/&editing=false&openfile=true"
    ) == "files:73"
    assert provider._document_id_from_url(
        "https://cloud.example/index.php/apps/files/files/73?dir=/&openfile=true"
    ) == "files:73"

def test_nextcloud_source_url_switches_at_nc26(monkeypatch):
    monkeypatch.setattr(provider, "NEXTCLOUD_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(provider, "_nextcloud_major_version", lambda: 26)
    assert "/apps/files/files/73?" in provider._nextcloud_source_url(_source_result())
