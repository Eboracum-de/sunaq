"""Minimal OpenAI-compatible provider used while SunaQ is in maintenance mode.

The maintenance provider deliberately does not import the normal provider, SunaQ API,
LLM backends, Elasticsearch, Qdrant or Neo4j. It only authenticates the registered
provider-client Bearer key and returns a stable maintenance response.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, AsyncIterator

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from rag.version import VERSION


MODEL_ID = os.getenv("PROVIDER_MODEL_ID", "nextcloud-hybrid-rag")
MODEL_NAME = os.getenv("PROVIDER_MODEL_NAME", "SunaQ")
MAINTENANCE_MESSAGE = os.getenv(
    "RAG_MAINTENANCE_MESSAGE",
    "SunaQ ist im Maintenance-Modus. Bitte versuchen Sie es später erneut.",
).strip() or "RAG ist im Maintenance-Modus. Bitte versuchen Sie es später erneut."

app = FastAPI(title="SunaQ Maintenance Provider", version=VERSION)


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_ID
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None


def _credential_store_path() -> Path:
    config_path = Path(os.getenv("RAG_CONFIG_FILE", "config.yaml"))
    try:
        with config_path.open(encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle) or {}
        raw = str(((cfg.get("auth") or {}).get("credential_store") or "runtime/users.sqlite")).strip()
    except Exception:
        raw = "runtime/users.sqlite"
    return Path(raw or "runtime/users.sqlite")


def _authenticate_client(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing provider client API key")
    api_key = authorization[7:].strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing provider client API key")

    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    path = _credential_store_path()
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail="Provider client registry unavailable during maintenance",
        )
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT client_id FROM provider_clients WHERE api_key_hash=? AND enabled=1",
                (key_hash,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="Provider client registry unavailable during maintenance",
        ) from exc

    if row is None:
        raise HTTPException(status_code=401, detail="Invalid provider client API key")
    return str(row[0])


def _completion_payload(completion_id: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": MAINTENANCE_MESSAGE},
                "finish_reason": "stop",
            }
        ],
    }


def _sse(completion_id: str, content: str = "", finish_reason: str | None = None) -> str:
    delta: dict[str, Any] = {}
    if content:
        delta["content"] = content
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/live")
async def live() -> dict[str, Any]:
    return {"status": "maintenance", "version": VERSION, "model": MODEL_ID}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "maintenance",
        "maintenance": True,
        "version": VERSION,
        "model": MODEL_ID,
    }


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _authenticate_client(authorization)
    return {
        "object": "list",
        "status": "maintenance",
        "maintenance": True,
        "version": VERSION,
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "sunaq",
                "name": MODEL_NAME + " (Maintenance)",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
) -> Any:
    _authenticate_client(authorization)
    completion_id = "chatcmpl-maint-" + uuid.uuid4().hex

    if not body.stream:
        return _completion_payload(completion_id)

    async def generator() -> AsyncIterator[str]:
        initial = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        yield _sse(completion_id, MAINTENANCE_MESSAGE)
        yield _sse(completion_id, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")
