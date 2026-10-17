from types import SimpleNamespace

import rag.embeddings as embeddings
from rag.embeddings import build_embedding_backend_from_config
from rag.sync import build_index_signature, embed_chunks_resilient


class CaptureEmbedding(embeddings.EmbeddingBackend):
    kind = "capture"

    def __post_init__(self):
        super().__post_init__()
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        dim = self.dimensions or 2
        return [[1.0] * dim for _ in texts]


def test_explicit_query_instruction_and_1024_dimensions():
    backend = build_embedding_backend_from_config({
        "embedding": {
            "backend": "ollama",
            "url": "http://127.0.0.1:11434",
            "model": "arbitrary-embedding-model",
            "profile": "custom",
            "document_prefix": "",
            "query_prefix": "Instruct: test\nQuery: ",
            "dimensions": 1024,
        }
    })
    assert backend.profile == "custom"
    assert backend.document_prefix == ""
    assert backend.query_prefix == "Instruct: test\nQuery: "
    assert backend.dimensions == 1024


def test_basename_is_embedding_only_and_payload_chunk_stays_raw():
    backend = CaptureEmbedding(
        "http://unused",
        "arbitrary-embedding-model",
        profile="custom",
        query_prefix="Instruct: test\nQuery: ",
        dimensions=4,
    )
    result = embed_chunks_resilient(
        backend,
        ["Unveraenderter Payload-Text"],
        batch_size=8,
        document_id="files:1",
        title="Rechnungen/2025/RG-examplehost-2025-04.pdf",
        overlap=400,
        include_basename=True,
    )
    assert backend.calls == [[
        "Document: RG-examplehost-2025-04.pdf\n\nUnveraenderter Payload-Text"
    ]]
    assert result[0][0] == "Unveraenderter Payload-Text"
    assert len(result[0][1]) == 4


def test_index_signature_changes_with_dimensions_and_basename_mode():
    base = dict(
        embedding_backend="ollama",
        embedding_model="arbitrary-embedding-model",
        embedding_profile="custom",
        embedding_document_prefix="",
        chunk_size=3000,
        chunk_overlap=400,
    )
    sig_1024 = build_index_signature(
        **base, embedding_dimensions=1024, embedding_include_basename=True
    )
    sig_native = build_index_signature(
        **base, embedding_dimensions=None, embedding_include_basename=True
    )
    sig_no_name = build_index_signature(
        **base, embedding_dimensions=1024, embedding_include_basename=False
    )
    assert sig_1024 != sig_native
    assert sig_1024 != sig_no_name


def test_ollama_embed_sends_dimensions(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"embeddings":[[0.1,0.2,0.3,0.4]]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[0.1, 0.2, 0.3, 0.4]]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(embeddings.httpx, "Client", FakeClient)
    backend = embeddings.OllamaEmbeddings(
        "http://ollama", "arbitrary-embedding-model", dimensions=4
    )
    vectors = backend.embed(["hello"])
    assert vectors == [[0.1, 0.2, 0.3, 0.4]]
    assert calls == [("http://ollama/api/embed", {
        "model": "arbitrary-embedding-model",
        "input": ["hello"],
        "dimensions": 4,
    })]


def test_ollama_model_tag_case_is_resolved_from_installed_tags(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = __import__('json').dumps(payload)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            assert url == "http://ollama/api/tags"
            return FakeResponse(200, {"models": [{"name": "Qwen3-Embedding:4B"}]})

        def post(self, url, json):
            calls.append((url, dict(json)))
            if json["model"] == "qwen3-embedding:4b":
                return FakeResponse(404, {"error": "model not found"})
            return FakeResponse(200, {"embeddings": [[0.1, 0.2, 0.3, 0.4]]})

    monkeypatch.setattr(embeddings.httpx, "Client", FakeClient)
    backend = embeddings.OllamaEmbeddings(
        "http://ollama", "qwen3-embedding:4b", dimensions=4
    )
    assert backend.embed(["hello"]) == [[0.1, 0.2, 0.3, 0.4]]
    assert backend.model == "Qwen3-Embedding:4B"
    assert [call[1]["model"] for call in calls] == [
        "qwen3-embedding:4b",
        "Qwen3-Embedding:4B",
    ]


def test_embedding_health_accepts_ollama_model_tag_case_difference(monkeypatch):
    import rag.api as api

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"models": [{"name": "Qwen3-Embedding:4B"}]}

    monkeypatch.setattr(api, "EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(api, "EMBEDDING_URL", "http://ollama")
    monkeypatch.setattr(api, "EMBEDDING_MODEL", "qwen3-embedding:4b")
    monkeypatch.setattr(
        api,
        "_probe_http",
        lambda *args, **kwargs: {
            "status": "ok",
            "http_status": 200,
            "latency_ms": 1.0,
            "response": FakeResponse(),
        },
    )

    result = api._health_embedding(4.0)
    assert result["status"] == "ok"
    assert result["model_present"] is True
    assert "error" not in result


def test_embedding_health_does_not_accept_different_explicit_ollama_tag(monkeypatch):
    import rag.api as api

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"models": [{"name": "Qwen3-Embedding:8B"}]}

    monkeypatch.setattr(api, "EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(api, "EMBEDDING_URL", "http://ollama")
    monkeypatch.setattr(api, "EMBEDDING_MODEL", "qwen3-embedding:4b")
    monkeypatch.setattr(
        api,
        "_probe_http",
        lambda *args, **kwargs: {
            "status": "ok",
            "http_status": 200,
            "latency_ms": 1.0,
            "response": FakeResponse(),
        },
    )

    result = api._health_embedding(4.0)
    assert result["status"] == "down"
    assert result["model_present"] is False


def test_ollama_dimension_mismatch_fails(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = '{"embeddings":[[0.1,0.2]]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[0.1, 0.2]]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr(embeddings.httpx, "Client", FakeClient)
    backend = embeddings.OllamaEmbeddings(
        "http://ollama", "arbitrary-embedding-model", dimensions=4
    )
    try:
        backend.embed(["hello"])
    except RuntimeError as exc:
        assert "dimension mismatch" in str(exc)
    else:
        raise AssertionError("dimension mismatch must fail closed")
