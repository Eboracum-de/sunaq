"""Configurable embedding backends for Nextcloud Hybrid RAG.

Supported backends:
- ollama: native Ollama /api/embed (with legacy /api/embeddings fallback)
- openai: OpenAI-compatible /v1/embeddings API

The embedding backend is deliberately independent from the answer LLM.  This
allows a node to run fully locally, fully against API services, or in a mixed
configuration.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from typing import Any

from rag.secret_env import secret_env

import httpx

from rag.policy_hooks import PRE_MODEL_EGRESS, apply_policy_hook


class EmbeddingContextLengthError(RuntimeError):
    """Embedding backend rejected an input because it exceeds model context."""

    def __init__(self, message: str, *, status_code: int | None = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _is_context_length_response(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    text = str(response.text or "").casefold()
    needles = (
        "input length exceeds the context length",
        "context length",
        "context window",
        "too many tokens",
        "maximum context",
    )
    return any(needle in text for needle in needles)


def _verify_value(verify_tls: bool, ca_file: str | None) -> bool | ssl.SSLContext:
    if not verify_tls:
        return False
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return True


@dataclass
class EmbeddingBackend:
    base_url: str
    model: str
    api_key: str = ""
    verify_tls: bool = True
    ca_file: str | None = None
    timeout: float = 300.0
    profile: str = "plain"
    document_prefix: str = ""
    query_prefix: str = ""
    dimensions: int | None = None

    kind = "base"

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).rstrip("/")
        self.model = str(self.model)
        self.api_key = str(self.api_key or "")
        self.profile = str(self.profile or "plain").strip().lower() or "plain"
        self.document_prefix = str(self.document_prefix or "")
        self.query_prefix = str(self.query_prefix or "")
        if self.dimensions in (None, "", 0, "0"):
            self.dimensions = None
        else:
            self.dimensions = int(self.dimensions)
            if self.dimensions <= 0:
                raise RuntimeError("embedding.dimensions must be a positive integer")
        self._verify = _verify_value(self.verify_tls, self.ca_file)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _policy_texts(self, texts: list[str]) -> list[str]:
        checked = apply_policy_hook(
            PRE_MODEL_EGRESS,
            content=texts,
            metadata={
                "kind": "embedding",
                "backend": self.kind,
                "base_url": self.base_url,
                "model": self.model,
            },
        )
        if not isinstance(checked, list) or any(
            not isinstance(item, str) for item in checked
        ):
            raise TypeError("pre_model_egress policy hook must return a string list")
        return checked

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed corpus passages using the configured retrieval document role."""
        if not self.document_prefix:
            return self.embed(texts)
        return self.embed([f"{self.document_prefix}{text}" for text in texts])

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed search queries using the configured retrieval query role."""
        if not self.query_prefix:
            return self.embed(texts)
        return self.embed([f"{self.query_prefix}{text}" for text in texts])

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_queries([text])
        if len(vectors) != 1:
            raise RuntimeError(f"Embedding count mismatch for query: {len(vectors)} != 1")
        return vectors[0]

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "profile": self.profile,
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
            "dimensions": self.dimensions,
            "authenticated": bool(self.api_key),
            "verify_tls": bool(self.verify_tls),
            "ca_file": bool(self.ca_file),
        }


class OllamaEmbeddings(EmbeddingBackend):
    kind = "ollama"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        texts = self._policy_texts(texts)
        with httpx.Client(
            timeout=self.timeout,
            headers=self.headers(),
            verify=self._verify,
        ) as client:
            payload: dict[str, Any] = {"model": self.model, "input": texts}
            if self.dimensions is not None:
                payload["dimensions"] = self.dimensions
            response = client.post(
                f"{self.base_url}/api/embed",
                json=payload,
            )
            if response.status_code == 404:
                if self.dimensions is not None:
                    raise RuntimeError(
                        "Configured embedding.dimensions requires Ollama /api/embed; "
                        "the legacy /api/embeddings endpoint cannot honor it"
                    )
                vectors: list[list[float]] = []
                for text in texts:
                    legacy = client.post(
                        f"{self.base_url}/api/embeddings",
                        json={"model": self.model, "prompt": text},
                    )
                    if _is_context_length_response(legacy):
                        raise EmbeddingContextLengthError(
                            "Ollama legacy embedding input exceeds model context",
                            status_code=legacy.status_code,
                            response_text=legacy.text[:1000],
                        )
                    legacy.raise_for_status()
                    vector = legacy.json().get("embedding")
                    if not isinstance(vector, list):
                        raise RuntimeError(
                            "Unexpected Ollama legacy embedding response: "
                            f"{legacy.text[:500]}"
                        )
                    vectors.append(vector)
                return vectors

            if _is_context_length_response(response):
                raise EmbeddingContextLengthError(
                    "Ollama embedding input exceeds model context",
                    status_code=response.status_code,
                    response_text=response.text[:1000],
                )
            response.raise_for_status()
            vectors = response.json().get("embeddings")
            if not isinstance(vectors, list):
                raise RuntimeError(
                    f"Unexpected Ollama embed response: {response.text[:500]}"
                )
            if self.dimensions is not None and any(
                not isinstance(vector, list) or len(vector) != self.dimensions
                for vector in vectors
            ):
                lengths = [len(v) if isinstance(v, list) else None for v in vectors]
                raise RuntimeError(
                    f"Embedding dimension mismatch: requested {self.dimensions}, got {lengths[:5]}"
                )
            return vectors


class OpenAICompatibleEmbeddings(EmbeddingBackend):
    kind = "openai"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        texts = self._policy_texts(texts)
        with httpx.Client(
            timeout=self.timeout,
            headers=self.headers(),
            verify=self._verify,
        ) as client:
            payload: dict[str, Any] = {"model": self.model, "input": texts}
            if self.dimensions is not None:
                payload["dimensions"] = self.dimensions
            response = client.post(
                f"{self.base_url}/embeddings",
                json=payload,
            )
            if _is_context_length_response(response):
                raise EmbeddingContextLengthError(
                    "Embedding input exceeds model context",
                    status_code=response.status_code,
                    response_text=response.text[:1000],
                )
            response.raise_for_status()
            data = response.json().get("data")

        if not isinstance(data, list):
            raise RuntimeError(
                f"Unexpected OpenAI-compatible embedding response: {response.text[:500]}"
            )

        indexed: list[tuple[int, list[float]]] = []
        for fallback_index, item in enumerate(data):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise RuntimeError(
                    f"Unexpected OpenAI-compatible embedding item: {item!r}"
                )
            try:
                index = int(item.get("index", fallback_index))
            except (TypeError, ValueError):
                index = fallback_index
            indexed.append((index, item["embedding"]))

        indexed.sort(key=lambda pair: pair[0])
        vectors = [vector for _, vector in indexed]
        if self.dimensions is not None and any(len(vector) != self.dimensions for vector in vectors):
            lengths = [len(vector) for vector in vectors]
            raise RuntimeError(
                f"Embedding dimension mismatch: requested {self.dimensions}, got {lengths[:5]}"
            )
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embedding count mismatch: {len(vectors)} != {len(texts)}"
            )
        return vectors


def build_embedding_backend(
    backend: str,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    verify_tls: bool = True,
    ca_file: str | None = None,
    timeout: float = 300.0,
    profile: str = "plain",
    document_prefix: str = "",
    query_prefix: str = "",
    dimensions: int | None = None,
) -> EmbeddingBackend:
    name = str(backend or "ollama").strip().lower()
    kwargs = {
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "verify_tls": verify_tls,
        "ca_file": ca_file,
        "timeout": timeout,
        "profile": profile,
        "document_prefix": document_prefix,
        "query_prefix": query_prefix,
        "dimensions": dimensions,
    }
    if name in {"ollama", "native_ollama"}:
        return OllamaEmbeddings(**kwargs)
    if name in {"openai", "openai_compatible", "openai-compatible"}:
        return OpenAICompatibleEmbeddings(**kwargs)
    raise RuntimeError(
        "embedding.backend muss 'ollama' oder 'openai' sein "
        f"(erhalten: {backend!r})"
    )


def build_embedding_backend_from_config(config: dict[str, Any]) -> EmbeddingBackend:
    """Create an embedding backend from the canonical ``embedding`` section.

    ``api_key_env`` is preferred so secrets do not have to live in config.yaml.
    ``api_key`` exists mainly for controlled test environments.
    """
    section = config.get("embedding", {}) or {}
    backend = str(section.get("backend", "ollama")).strip().lower()
    base_url = str(
        section.get("url")
        or (config.get("ollama", {}) or {}).get("url")
        or "http://127.0.0.1:11434"
    ).rstrip("/")
    model = str(
        section.get("model")
        or (config.get("ollama", {}) or {}).get("embedding_model")
        or "qwen3-embedding:4b"
    )
    # Embedding role formatting is explicit and model-agnostic.  ``profile``
    # is diagnostic metadata only; it never selects prefixes or behavior.
    requested_profile = str(section.get("profile") or "").strip().lower()
    document_prefix = str(section.get("document_prefix") or "")
    query_prefix = str(section.get("query_prefix") or "")
    profile = requested_profile or ("custom" if (document_prefix or query_prefix) else "plain")

    env_name = str(section.get("api_key_env") or "").strip()
    api_key = secret_env(env_name, "") if env_name else str(section.get("api_key") or "")
    verify_tls = bool(section.get("verify_tls", True))
    ca_file = str(section.get("ca_file") or "").strip() or None
    timeout = float(section.get("timeout", 300.0))
    raw_dimensions = section.get("dimensions")
    dimensions = None if raw_dimensions in (None, "", 0, "0") else int(raw_dimensions)
    return build_embedding_backend(
        backend,
        base_url=base_url,
        model=model,
        api_key=api_key,
        verify_tls=verify_tls,
        ca_file=ca_file,
        timeout=timeout,
        profile=profile,
        document_prefix=document_prefix,
        query_prefix=query_prefix,
        dimensions=dimensions,
    )
