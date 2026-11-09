import hashlib
import json
import sys
from types import SimpleNamespace

import yaml

import rag.sync as sync


class DummyEmbeddingBackend:
    kind = "dummy"
    model = "dummy-model"
    profile = "plain"
    document_prefix = ""

    def embed(self, texts):
        return [[float(len(text)), 0.25] for text in texts]


def _response(status=200, text='{"status":"ok"}'):
    return SimpleNamespace(status_code=status, ok=200 <= status < 300, text=text)


def _hit(doc_id="files:1", title="Docs/test.txt", content="abcdefghij"):
    return {
        "_id": doc_id,
        "_source": {
            "title": title,
            "content": content,
            "hash": f"hash-{content}",
            "attachment": {"content_type": "text/plain"},
        },
    }


def _config(tmp_path, **sync_overrides):
    data = {
        "embedding": {"backend": "ollama", "model": "dummy"},
        "qdrant": {"url": "http://qdrant", "collection": "test"},
        "sync": {
            "state_db": str(tmp_path / "state.sqlite"),
            "status_file": str(tmp_path / "sync-status.json"),
            "chunk_size": 5,
            "chunk_overlap": 0,
            "embedding_batch_size": 2,
            "max_documents": 0,
            "graph_queue": {"enabled": False},
        },
    }
    data["sync"].update(sync_overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _decision_signature(index_signature="sig", document_filter=None):
    payload = {
        "schema": "sunaq-es-inventory-v1",
        "index_signature": index_signature,
        "document_filter": document_filter or {},
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _inventory_record(digest, *, disposition="vectorized", index_signature="sig", chunk_count=2):
    return {
        "content_hash": digest,
        "disposition": disposition,
        "index_signature": index_signature,
        "decision_signature": _decision_signature(index_signature),
        "chunk_count": chunk_count,
    }


def test_explicit_chunk_delete_uses_point_ids_not_filter(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _response()

    monkeypatch.setattr(sync.requests, "post", fake_post)
    deleted = sync.qdrant_delete_chunk_range(
        "http://qdrant", "test", "files:42", 2, 5, batch_size=2
    )

    assert deleted == 3
    assert len(calls) == 2
    payloads = [call[1]["json"] for call in calls]
    assert all("points" in payload for payload in payloads)
    assert all("filter" not in payload for payload in payloads)
    assert payloads[0]["points"] == [
        sync.point_id("files:42", 2),
        sync.point_id("files:42", 3),
    ]
    assert payloads[1]["points"] == [sync.point_id("files:42", 4)]


def test_new_document_upserts_without_delete(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    hit = _hit(content="abcdefghij")
    events = []
    inventory = {}

    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter([hit]))
    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(
        sync,
        "qdrant_inventory_upsert_many",
        lambda _url, _collection, records, **kwargs: inventory.update({
            doc: {
                "content_hash": digest,
                "disposition": disposition,
                "index_signature": index_sig,
                "decision_signature": decision_sig,
                "chunk_count": chunks,
            }
            for doc, digest, disposition, index_sig, decision_sig, chunks in records
        }) or len(list(inventory)),
    )
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(sync, "ensure_qdrant_collection", lambda *a, **k: events.append(("ensure",)))
    monkeypatch.setattr(sync, "qdrant_upsert", lambda *a, **k: events.append(("upsert", a[2])))
    monkeypatch.setattr(sync, "qdrant_delete_chunk_range", lambda *a, **k: events.append(("delete", a[3], a[4])))
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert [event[0] for event in events].count("upsert") == 1
    assert not any(event[0] == "delete" for event in events)

    status = json.loads((tmp_path / "sync-status.json").read_text(encoding="utf-8"))
    assert status["complete_full_scan"] is True
    assert status["scan"]["scanned"] == 1
    assert status["scan"]["eligible_seen"] == 1
    assert status["state"]["vectorized_documents"] == 1
    assert status["state"]["chunks"] == 2


def test_shrinking_document_upserts_first_then_deletes_only_tail(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    hit = _hit(content="abcdefghi")
    events = []
    inventory = {"files:1": _inventory_record("old-hash", chunk_count=4)}

    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(sync, "qdrant_inventory_upsert_many", lambda *a, **k: 1)
    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter([hit]))
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(sync, "build_index_signature", lambda **kwargs: "sig")
    monkeypatch.setattr(sync, "ensure_qdrant_collection", lambda *a, **k: None)
    monkeypatch.setattr(sync, "qdrant_upsert", lambda *a, **k: events.append(("upsert",)))
    monkeypatch.setattr(
        sync,
        "qdrant_delete_chunk_range",
        lambda _url, _collection, doc, start, end, **kwargs: events.append(("delete", doc, start, end)),
    )
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert events[0][0] == "upsert"
    assert events[-1] == ("delete", "files:1", 2, 4)


def test_stale_document_deletes_qdrant_inventory_ids(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    inventory = {"files:9": _inventory_record("hash", chunk_count=3)}
    deleted = []
    inventory_deleted = []

    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter([]))
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(
        sync,
        "qdrant_delete_chunk_range",
        lambda _url, _collection, doc, start, end, **kwargs: deleted.append((doc, start, end)),
    )
    monkeypatch.setattr(
        sync,
        "qdrant_inventory_delete",
        lambda _url, _collection, doc: inventory_deleted.append(doc),
    )
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert deleted == [("files:9", 0, 3)]
    assert inventory_deleted == ["files:9"]


def test_runtime_pause_stops_at_batch_boundary_without_stale_cleanup(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    inventory = {
        f"files:{number}": _inventory_record("hash-abcdefghij")
        for number in range(1, 61)
    }
    inventory["files:999"] = _inventory_record("stale-hash", chunk_count=1)

    hits = [
        _hit(doc_id=f"files:{number}", title=f"Docs/test-{number}.txt", content="abcdefghij")
        for number in range(1, 61)
    ]
    deleted = []
    inventory_deleted = []

    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(sync, "es_scroll", lambda *a, **k: iter(hits))
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(sync, "build_index_signature", lambda **kwargs: "sig")
    monkeypatch.setattr(sync, "maybe_register_path_origin", lambda *a, **k: "internal")
    monkeypatch.setattr(sync, "runtime_worker_enabled", lambda *a, **k: False)
    monkeypatch.setattr(
        sync,
        "qdrant_delete_chunk_range",
        lambda _url, _collection, doc, start, end, **kwargs: deleted.append((doc, start, end)),
    )
    monkeypatch.setattr(
        sync,
        "qdrant_inventory_delete",
        lambda _url, _collection, doc: inventory_deleted.append(doc),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync",
            "--config",
            str(cfg_path),
            "--max-documents",
            "0",
            "--runtime-control-worker",
            "sync_worker",
            "--pause-check-documents",
            "25",
        ],
    )

    assert sync.main() == 0
    assert deleted == []
    assert inventory_deleted == []

    status = json.loads((tmp_path / "sync-status.json").read_text(encoding="utf-8"))
    assert status["runtime_paused"] is True
    assert status["complete_full_scan"] is False
    assert status["scan"]["scanned"] == 25



def test_legacy_dockerized_top_level_state_db_uses_runtime_mount():
    cfg = {
        "deployment": {"mode": "dockerized"},
        "state_db": "state.sqlite",
    }
    assert sync.resolve_sync_state_path(cfg) == "runtime/state.sqlite"


def test_standard_legacy_top_level_state_db_remains_compatible():
    cfg = {
        "deployment": {"mode": "native"},
        "state_db": "state.sqlite",
    }
    assert sync.resolve_sync_state_path(cfg) == "state.sqlite"


def test_canonical_sync_state_db_wins_over_legacy_value():
    cfg = {
        "deployment": {"mode": "dockerized"},
        "state_db": "state.sqlite",
        "sync": {"state_db": "runtime/custom-state.sqlite"},
    }
    assert sync.resolve_sync_state_path(cfg) == "runtime/custom-state.sqlite"


def test_es_scroll_uses_light_source_and_hydrates_only_selected_documents(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
            self.ok = True
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    pages = [
        {
            "_scroll_id": "scroll-1",
            "hits": {
                "hits": [
                    {"_id": "files:1", "_source": {"title": "Docs/one.txt", "hash": "same"}},
                    {"_id": "files:2", "_source": {"title": "Docs/two.txt", "hash": "changed"}},
                ]
            },
        },
        {"_scroll_id": "scroll-1", "hits": {"hits": []}},
    ]

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/my_index/_search"):
            return FakeResponse(pages[0])
        if url.endswith("/my_index/_mget"):
            assert kwargs["json"]["ids"] == ["files:2"]
            return FakeResponse({
                "docs": [{
                    "_id": "files:2",
                    "found": True,
                    "_source": {
                        "title": "Docs/two.txt",
                        "hash": "changed",
                        "content": "full document text",
                    },
                }]
            })
        if url.endswith("/_search/scroll"):
            return FakeResponse(pages[1])
        raise AssertionError(url)

    monkeypatch.setattr(sync.requests, "post", fake_post)
    monkeypatch.setattr(sync.requests, "delete", lambda *a, **k: FakeResponse({}))

    hits = list(sync.es_scroll(
        "http://es",
        "my_index",
        50,
        "5m",
        {},
        full_source_predicate=lambda hit: hit["_id"] == "files:2",
        inventory_only=True,
    ))

    initial_body = calls[0][1]["json"]
    assert initial_body["_source"] == ["hash"]
    assert initial_body["version"] is True
    assert [hit["_id"] for hit in hits] == ["files:1", "files:2"]
    assert "content" not in hits[0]["_source"]
    assert hits[1]["_source"]["content"] == "full document text"
    mget_call = next(kwargs for url, kwargs in calls if url.endswith("/_mget"))
    assert mget_call["json"] == {"ids": ["files:2"]}
    assert sum(1 for url, _ in calls if url.endswith("/_mget")) == 1


def test_hashless_document_uses_elasticsearch_version_as_fingerprint():
    hit = {
        "_id": "files:empty",
        "_version": 17,
        "_source": {"title": "Docs/empty.txt"},
    }
    assert sync.content_hash(hit) == "es-version:17"


def test_qdrant_inventory_scroll_projects_metadata_without_vectors(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    monkeypatch.setattr(sync, "qdrant_collection_exists", lambda *a, **k: True)

    pages = [
        {
            "result": {
                "points": [
                    {
                        "id": "x",
                        "payload": {
                            "document_id": "files:1",
                            "content_hash": "hash-1",
                            "disposition": "vectorized",
                            "index_signature": "sig",
                            "decision_signature": "decision",
                            "chunk_count": 3,
                        },
                    }
                ],
                "next_page_offset": "next",
            }
        },
        {"result": {"points": [], "next_page_offset": None}},
    ]

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(sync.requests, "post", fake_post)
    inventory = sync.qdrant_load_inventory("http://qdrant", "test__sync_inventory")

    assert inventory["files:1"]["content_hash"] == "hash-1"
    assert inventory["files:1"]["chunk_count"] == 3
    assert calls[0][1]["json"]["with_vector"] is False
    assert "content_hash" in calls[0][1]["json"]["with_payload"]["include"]


def test_full_run_uses_qdrant_inventory_without_hydration(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    light_hit = {"_id": "files:1", "_source": {"hash": "hash-abcdefghij"}}
    predicate_results = []
    inventory = {
        "files:1": _inventory_record("hash-abcdefghij", index_signature="sig", chunk_count=2)
    }

    def fake_scroll(*args, **kwargs):
        predicate = kwargs["full_source_predicate"]
        predicate_results.append(predicate(light_hit))
        yield light_hit

    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(sync, "es_scroll", fake_scroll)
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(sync, "build_index_signature", lambda **kwargs: "sig")
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert predicate_results == [False]

    status = json.loads((tmp_path / "sync-status.json").read_text(encoding="utf-8"))
    assert status["scan"]["unchanged"] == 1
    assert status["scan"]["full_source_fetched"] == 0


def test_changed_document_is_hydrated_and_qdrant_inventory_updated(monkeypatch, tmp_path):
    cfg_path = _config(tmp_path)
    inventory = {"files:1": _inventory_record("old-hash", index_signature="sig", chunk_count=2)}
    light_hit = {"_id": "files:1", "_source": {"hash": "hash-abcdefghij"}}
    full_hit = _hit(content="abcdefghij")
    written = []

    def fake_scroll(*args, **kwargs):
        predicate = kwargs["full_source_predicate"]
        assert predicate(light_hit) is True
        yield full_hit

    monkeypatch.setattr(sync, "qdrant_load_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(sync, "qdrant_inventory_upsert_many", lambda _u, _c, records, **k: written.extend(records) or len(records))
    monkeypatch.setattr(sync, "es_scroll", fake_scroll)
    monkeypatch.setattr(sync, "build_embedding_backend_from_config", lambda cfg: DummyEmbeddingBackend())
    monkeypatch.setattr(sync, "build_index_signature", lambda **kwargs: "sig")
    monkeypatch.setattr(sync, "ensure_qdrant_collection", lambda *a, **k: None)
    monkeypatch.setattr(sync, "qdrant_upsert", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(cfg_path), "--max-documents", "0"])

    assert sync.main() == 0
    assert written[-1][0] == "files:1"
    assert written[-1][1] == "hash-abcdefghij"
    assert written[-1][2] == "vectorized"

    status = json.loads((tmp_path / "sync-status.json").read_text(encoding="utf-8"))
    assert status["scan"]["updated"] == 1
    assert status["scan"]["full_source_fetched"] == 1
