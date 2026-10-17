from rag import nextcloud_links
from rag import openai_provider as provider
from rag import search


def test_build_nextcloud_source_url_legacy_and_modern():
    assert nextcloud_links.build_nextcloud_source_url(
        "http://cloud.example:8080",
        "/Documents",
        "10",
        major_version=23,
    ) == (
        "http://cloud.example:8080/index.php/apps/files/"
        "?dir=%2FDocuments&openfile=10"
    )
    assert nextcloud_links.build_nextcloud_source_url(
        "http://cloud.example:8080",
        "/Documents",
        "10",
        major_version=34,
    ) == (
        "http://cloud.example:8080/apps/files/files/10"
        "?dir=%2FDocuments&editing=false&openfile=true"
    )


def test_search_source_url_uses_detected_nextcloud_version(monkeypatch):
    monkeypatch.setattr(search, "NEXTCLOUD_BASE_URL", "http://cloud.example:8080")
    monkeypatch.setattr(search._NEXTCLOUD_VERSION_PROBE, "major_version", lambda: 34)
    assert search.nextcloud_source_url("/", 73) == (
        "http://cloud.example:8080/apps/files/files/73"
        "?dir=%2F&editing=false&openfile=true"
    )


def test_provider_rebuilds_stale_legacy_api_source_url(monkeypatch):
    monkeypatch.setattr(provider, "NEXTCLOUD_BASE_URL", "http://cloud.example:8080")
    monkeypatch.setattr(provider, "_nextcloud_major_version", lambda: 34)
    result = provider.SearchResult(
        index=1,
        title="Reasons to use Nextcloud.pdf",
        text="",
        raw={
            "path": "Reasons to use Nextcloud.pdf",
            "document_id": "files:73",
            "nextcloud_openfile_id": "73",
            "source_url": (
                "http://cloud.example:8080/index.php/apps/files/"
                "?dir=%2F&openfile=73"
            ),
        },
    )
    assert provider._nextcloud_source_url(result) == (
        "http://cloud.example:8080/apps/files/files/73"
        "?dir=%2F&editing=false&openfile=true"
    )


def test_document_id_parser_accepts_both_link_generations():
    assert nextcloud_links.document_id_from_nextcloud_url(
        "http://cloud.example:8080/index.php/apps/files/?dir=%2F&openfile=73"
    ) == "files:73"
    assert nextcloud_links.document_id_from_nextcloud_url(
        "http://cloud.example:8080/apps/files/files/73?dir=%2F&openfile=true"
    ) == "files:73"
