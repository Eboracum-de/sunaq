"""Qdrant/embedding smoke probe with a hidden sentinel point.

The probe deliberately writes one stable internal point.  This both verifies
embedding + Qdrant write/read and initializes an empty collection with the
correct vector dimension.  Retrieval filters internal points, so the sentinel
never becomes answer evidence.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import requests
import yaml

from rag.embeddings import build_embedding_backend_from_config
from rag.sync import ensure_qdrant_collection, qdrant_upsert

SMOKE_DOCUMENT_ID = "__rag_smoke_test__"
SMOKE_TEXT = (
    "Hallo aus Qdrant. Wenn du diese Nachricht direkt in der Vektordatenbank "
    "gefunden hast, funktionieren Embedding, Collection und Schreibzugriff. "
    "Grüße vom Nextcloud Hybrid RAG."
)
SMOKE_POINT_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, f"nextcloud-rag:{SMOKE_DOCUMENT_ID}:0")
)


class SmokeProbeError(RuntimeError):
    """Expected smoke-probe failure with a stable component/reason classification."""

    def __init__(self, component: str, reason: str, message: str):
        super().__init__(message)
        self.component = str(component)
        self.reason = str(reason)


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be a mapping")
    return cfg


def run_probe(config_path: str | Path, *, embedding_url: str | None = None) -> dict[str, Any]:
    cfg = load_config(config_path)
    if embedding_url:
        embedding_cfg = cfg.setdefault("embedding", {})
        if not isinstance(embedding_cfg, dict):
            raise RuntimeError("embedding configuration must be a mapping")
        embedding_cfg["url"] = str(embedding_url).rstrip("/")
    qcfg = cfg.get("qdrant") or {}
    qdrant_url = str(qcfg.get("url") or "http://127.0.0.1:6333").rstrip("/")
    collection = str(qcfg.get("collection") or "nextcloud_rag")

    backend = build_embedding_backend_from_config(cfg)
    try:
        vectors = backend.embed([SMOKE_TEXT])
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as exc:
        raise SmokeProbeError(
            "embedding",
            "offline",
            f"{backend.kind} embedding endpoint not reachable ({backend.base_url})",
        ) from exc
    except Exception as exc:
        raise SmokeProbeError(
            "embedding",
            "error",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if len(vectors) != 1 or not vectors[0]:
        raise SmokeProbeError(
            "embedding",
            "error",
            "embedding backend returned no vector for smoke-test text",
        )
    vector = vectors[0]

    try:
        ensure_qdrant_collection(qdrant_url, collection, len(vector))
        qdrant_upsert(
            qdrant_url,
            collection,
            [
                {
                    "id": SMOKE_POINT_ID,
                    "vector": vector,
                    "payload": {
                        "_rag_internal": True,
                        "record_type": "smoke_test",
                        "document_id": SMOKE_DOCUMENT_ID,
                        "chunk_no": 0,
                        "title": "Hallo aus Qdrant",
                        "content": SMOKE_TEXT,
                        "source": "rag-smoke-test",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ],
        )

        # Verify that the exact point can be read back.  The vector itself is not
        # needed; avoiding it keeps the response small.
        response = requests.get(
            f"{qdrant_url}/collections/{collection}/points/{SMOKE_POINT_ID}",
            params={"with_payload": "true", "with_vector": "false"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SmokeProbeError(
            "qdrant",
            "offline",
            f"Qdrant endpoint not reachable ({qdrant_url})",
        ) from exc
    data = response.json()
    result = data.get("result") or {}
    payload = result.get("payload") or {}
    if payload.get("document_id") != SMOKE_DOCUMENT_ID or not payload.get("_rag_internal"):
        raise RuntimeError("Qdrant smoke point could not be verified after upsert")

    return {
        "status": "ok",
        "qdrant_url": qdrant_url,
        "collection": collection,
        "vector_size": len(vector),
        "embedding": backend.info(),
        "point_id": SMOKE_POINT_ID,
        "message": SMOKE_TEXT,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize/probe Qdrant using the configured embedding backend")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    parser.add_argument("--embedding-url", default=None, help="One-run override for embedding.url")
    args = parser.parse_args()

    try:
        result = run_probe(args.config, embedding_url=args.embedding_url)
    except SmokeProbeError as exc:
        payload = {
            "status": "down",
            "component": exc.component,
            "reason": exc.reason,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(
                f"SMOKE_FAIL component={exc.component} reason={exc.reason} error={str(exc)}"
            )
        return 20 if exc.component == "embedding" and exc.reason == "offline" else 21
    except Exception as exc:
        payload = {
            "status": "down",
            "component": "probe",
            "reason": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(
                "SMOKE_FAIL component=probe reason=error "
                f"error={type(exc).__name__}: {exc}"
            )
        return 22

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            "Qdrant smoke probe OK: "
            f"collection={result['collection']} vector_size={result['vector_size']} "
            f"embedding={result['embedding']['backend']}:{result['embedding']['model']} "
            f"profile={result['embedding']['profile']} dimensions={result['embedding']['dimensions'] or result['vector_size']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
