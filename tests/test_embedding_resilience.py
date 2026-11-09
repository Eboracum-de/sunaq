import pytest

import rag.sync as sync
from rag.embeddings import EmbeddingContextLengthError


class ContextLimitedBackend:
    kind = "dummy"
    model = "dummy"

    def __init__(self, limit=1000):
        self.limit = limit
        self.calls = []

    def embed(self, texts):
        self.calls.append([len(x) for x in texts])
        if any(len(x) > self.limit for x in texts):
            raise EmbeddingContextLengthError("too long", status_code=400)
        return [[float(len(x)), 0.5] for x in texts]


def test_context_overflow_isolated_and_only_offending_chunk_split():
    backend = ContextLimitedBackend(limit=1000)
    chunks = ["A" * 2000, "B" * 500]

    result = sync.embed_chunks_resilient(
        backend,
        chunks,
        batch_size=8,
        document_id="files:1",
        title="test.pdf",
        overlap=100,
    )

    texts = [text for text, _vector in result]
    assert len(texts) > 2
    assert all(len(text) <= 1000 for text in texts)
    assert "B" * 500 in texts
    assert any(set(text) == {"A"} for text in texts)


def test_non_context_embedding_error_is_not_silently_split():
    class BrokenBackend:
        def embed(self, texts):
            raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError, match="backend unavailable"):
        sync.embed_chunks_resilient(
            BrokenBackend(),
            ["A" * 2000],
            batch_size=8,
            document_id="files:2",
            title="test.pdf",
            overlap=100,
        )


def test_sync_does_not_delete_existing_qdrant_points_before_embedding_success(monkeypatch, tmp_path):
    import sys
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "embedding": {"backend": "ollama", "model": "dummy"},
            "qdrant": {"url": "http://qdrant", "collection": "test"},
            "sync": {
                "state_db": str(tmp_path / "state.sqlite"),
                "chunk_size": 3000,
                "chunk_overlap": 400,
                "max_documents": 0,
                "graph_queue": {"enabled": False},
            },
        }),
        encoding="utf-8",
    )

    hit = {
        "_id": "files:99",
        "_source": {
            "title": "Test/document.pdf",
            "content": "A" * 3000,
            "attachment": {"content_type": "application/pdf"},
        },
    }

    class BrokenBackend:
        kind = "dummy"
        model = "dummy-model"

        def embed(self, texts):
            raise RuntimeError("embedding backend failed")

    deleted = []
    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter([hit]))
    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: {})
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: BrokenBackend())
    monkeypatch.setattr(sync, "qdrant_delete_chunk_range", lambda *a, **k: deleted.append(a))
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    with pytest.raises(RuntimeError, match="embedding backend failed"):
        sync.main()
    assert deleted == []


def test_persistent_context_overflow_is_deferred_and_sync_continues(monkeypatch, tmp_path):
    import sys
    import yaml

    queue_path = tmp_path / "embedding-deferred.jsonl"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "embedding": {"backend": "ollama", "model": "dummy"},
            "qdrant": {"url": "http://qdrant", "collection": "test"},
            "sync": {
                "state_db": str(tmp_path / "state.sqlite"),
                "embedding_deferred_file": str(queue_path),
                "chunk_size": 3000,
                "chunk_overlap": 400,
                "max_documents": 0,
                "graph_queue": {"enabled": False},
            },
        }),
        encoding="utf-8",
    )

    hit = {
        "_id": "files:199",
        "_source": {
            "title": "Test/overflow.pdf",
            "content": "A" * 3000,
            "attachment": {"content_type": "application/pdf"},
        },
    }

    class AlwaysTooLongBackend:
        kind = "dummy"
        model = "dummy-model"

        def embed(self, texts):
            raise EmbeddingContextLengthError("still too long", status_code=400)

    deleted = []
    upserted = []
    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter([hit]))
    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: {})
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: AlwaysTooLongBackend())
    monkeypatch.setattr(sync, "qdrant_delete_chunk_range", lambda *a, **k: deleted.append(a))
    monkeypatch.setattr(sync, "qdrant_upsert", lambda *a, **k: upserted.append(a))
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert deleted == []
    assert upserted == []
    lines = queue_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = __import__("json").loads(lines[0])
    assert record["document_id"] == "files:199"
    assert record["reason"] == "context_length"
    assert record["chunk_size"] == 3000
