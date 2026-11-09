import sys
from pathlib import Path

import yaml

import rag.sync as sync


class DummyEmbeddingBackend:
    kind = "dummy"
    model = "dummy-model"

    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


def _hit(doc_id, title, content="text"):
    return {
        "_id": doc_id,
        "_source": {
            "title": title,
            "content": content,
            "attachment": {"content_type": "text/plain"},
        },
    }


def test_scoped_sync_checks_scope_before_semantic_filter(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "embedding": {"backend": "ollama", "model": "dummy"},
            "sync": {
                "state_db": str(tmp_path / "state.sqlite"),
                "max_documents": 0,
                "graph_queue": {"enabled": False},
            },
        }),
        encoding="utf-8",
    )

    hits = [
        _hit("files:1", "Other/ignored.eml"),
        _hit("files:2", "Wanted/document.txt"),
    ]
    monkeypatch.setattr(sync, "es_scroll", lambda *args, **kwargs: iter(hits))
    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: {})
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())

    semantic_titles = []
    original_semantic = sync.semantic_document_allowed

    def semantic_spy(cfg, hit):
        semantic_titles.append(hit["_source"]["title"])
        return original_semantic(cfg, hit)

    monkeypatch.setattr(sync, "semantic_document_allowed", semantic_spy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync", "--config", str(cfg_path), "--include-path", "Wanted", "--dry-run", "--max-documents", "0"],
    )

    assert sync.main() == 0
    assert semantic_titles == ["Wanted/document.txt"]


def test_scope_path_normalization_accepts_leading_slash():
    assert sync.path_matches_scope("Wanted/Sub/document.pdf", ["/Wanted/Sub/"], []) is True
    assert sync.path_matches_scope("Wanted/Else/document.pdf", ["/Wanted/Sub/"], []) is False
