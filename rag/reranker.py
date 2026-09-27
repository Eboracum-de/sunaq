import os
import time
import threading
from pathlib import Path

import httpx
import yaml

from rag.logging_utils import get_logger
from rag.architecture_policy import is_src
from rag.network_policy import pinned_private_target


# ------------------------------------------------------------
# Hugging Face grundsätzlich offline für den lokalen Fallback.
# Die schweren torch/transformers-Module werden bei backend=tei
# absichtlich NICHT importiert.
# ------------------------------------------------------------

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

log = get_logger("reranker")


# ------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

with (BASE_DIR / "config.yaml").open("r") as f:
    config = yaml.safe_load(f) or {}

reranker_config = config.get("reranker", {}) or {}

MODEL_NAME = str(
    reranker_config.get("model", "BAAI/bge-reranker-v2-m3")
).strip()

BACKEND = str(reranker_config.get("backend", "none") or "none").strip().lower()
DEVICE = str(reranker_config.get("device", "cpu") or "cpu").strip()
MAX_LENGTH = int(reranker_config.get("max_length", 512) or 512)
BATCH_SIZE = int(reranker_config.get("batch_size", 4) or 4)
TEI_URL = str(reranker_config.get("tei_url", "") or "").strip().rstrip("/")
TIMEOUT_SECONDS = float(reranker_config.get("timeout_seconds", 30) or 30)
TEI_BATCH_SIZE = int(reranker_config.get("tei_batch_size", 32) or 32)
FALLBACK_BACKEND = str(
    reranker_config.get("fallback_backend", "none") or "none"
).strip().lower()
PRIVATE_NETWORK_ONLY = is_src(config)

_VALID_BACKENDS = {"none", "local", "tei"}
_VALID_FALLBACKS = {"none", "local"}

# ------------------------------------------------------------
# Text für Cross-Encoder-Reranking bauen
#
# Relevanzsignale:
# - Titel / Pfad
# - Dokumentdatum
# - bester verfügbarer Treffertext
#
# Nicht enthalten: owner/users/groups/circles, IDs, Hashes usw.
# Diese Felder dienen ACL bzw. Navigation, nicht Relevanz.
# ------------------------------------------------------------

def build_reranker_text(item: dict) -> str:

    # Beide Retrieval-Sichten sind relevant: Elasticsearch liefert meist den
    # besten lexikalischen Ausschnitt, Qdrant den semantisch ähnlichsten Chunk.
    # Die frühere "vector OR es"-Logik hat die jeweils andere Evidenz verworfen.
    es_snippet = str(item.get("es_snippet") or "").strip()
    vector_snippet = str(item.get("vector_snippet") or "").strip()
    graph_snippet = str(item.get("graph_snippet") or "").strip()
    graph_reason = str(item.get("graph_reason") or "").strip()
    graph_entities = [str(x) for x in (item.get("graph_entities") or []) if str(x).strip()]
    graph_relations = list(item.get("graph_direct_relations") or [])
    graph_indirect_chains = list(item.get("graph_indirect_chains") or [])
    legacy_snippet = str(item.get("text") or "").strip()

    path = (
        item.get("path")
        or item.get("title")
        or item.get("filename")
        or ""
    ).strip()

    source_date = str(item.get("source_date") or "").strip()
    document_date = str(item.get("document_date") or "").strip()

    content_kind = str(
        item.get("content_kind")
        or ""
    ).strip()

    parts = []

    if path:
        parts.append(
            f"Datei/Pfad: {path}"
        )

    if source_date:
        parts.append(f"Quellendatum (inhaltlich): {source_date}")
    if document_date:
        # Technical ES/Nextcloud date stays separate from source-content time.
        display_date = document_date[:10]
        parts.append(f"Technisches Dokumentdatum: {display_date}")

    if content_kind == "metadata_only":
        parts.append(
            "Dokumenttyp: nur Dateimetadaten, kein extrahierter Dokumenttext"
        )

    if es_snippet:
        parts.append(
            "Elasticsearch-Ausschnitt:\n" + es_snippet[:1400]
        )

    if (
        vector_snippet
        and vector_snippet.casefold() != es_snippet.casefold()
    ):
        parts.append(
            "Semantischer Chunk:\n" + vector_snippet[:1400]
        )

    if graph_reason or graph_entities or graph_relations or graph_indirect_chains:
        graph_bits = []
        if graph_reason:
            graph_bits.append(f"Graph-Signal: {graph_reason}")
        if graph_entities:
            graph_bits.append("Graph-Entities: " + ", ".join(graph_entities[:6]))
        for rel in graph_relations[:4]:
            src = str(rel.get("from_display_name") or rel.get("from_entity_id") or "")
            typ = str(rel.get("relation") or "")
            dst = str(rel.get("to_display_name") or rel.get("to_entity_id") or "")
            if src and typ and dst:
                graph_bits.append(f"Strukturhinweis: {src} -[{typ}]-> {dst}")
        for chain in graph_indirect_chains[:3]:
            a = str(chain.get("query_entity_a_name") or chain.get("query_entity_a_id") or "")
            c = str(chain.get("bridge_entity_name") or chain.get("bridge_entity_id") or "")
            b = str(chain.get("query_entity_b_name") or chain.get("query_entity_b_id") or "")
            if a and c and b:
                graph_bits.append(
                    f"Indirekte, dokumentgestuetzte Belegkette (NICHT direkte Beziehung): {a} -> {c} -> {b}"
                )
        if graph_bits:
            parts.append("\n".join(graph_bits))

    if (
        graph_snippet
        and graph_snippet.casefold() != es_snippet.casefold()
        and graph_snippet.casefold() != vector_snippet.casefold()
    ):
        parts.append(
            "Graph-Dokumentausschnitt:\n" + graph_snippet[:1600]
        )

    if (
        not es_snippet
        and not vector_snippet
        and not graph_snippet
        and legacy_snippet
    ):
        parts.append(
            "Text:\n" + legacy_snippet[:2200]
        )

    return "\n".join(parts).strip()


# ------------------------------------------------------------
# Reranker
# ------------------------------------------------------------

class Reranker:

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        device: str = DEVICE,
        max_length: int = MAX_LENGTH,
        batch_size: int = BATCH_SIZE,
        backend: str = BACKEND,
        tei_url: str = TEI_URL,
        timeout_seconds: float = TIMEOUT_SECONDS,
        tei_batch_size: int = TEI_BATCH_SIZE,
        fallback_backend: str = FALLBACK_BACKEND,
        private_network_only: bool = PRIVATE_NETWORK_ONLY,
    ):
        self.model_name = str(model_name).strip()
        self.device_name = str(device).strip() or "cpu"
        self.max_length = max(32, int(max_length))
        self.batch_size = max(1, int(batch_size))
        self.backend = str(backend or "none").strip().lower()
        self.tei_url = str(tei_url or "").strip().rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.tei_batch_size = max(1, int(tei_batch_size))
        self.fallback_backend = str(fallback_backend or "none").strip().lower()
        self.private_network_only = bool(private_network_only)

        if self.backend not in _VALID_BACKENDS:
            raise ValueError(
                f"Ungültiges Reranker-Backend {self.backend!r}; erlaubt: none, local, tei"
            )
        if self.fallback_backend not in _VALID_FALLBACKS:
            raise ValueError(
                "Ungültiges reranker.fallback_backend "
                f"{self.fallback_backend!r}; erlaubt: none, local"
            )
        if self.backend == "tei" and not self.tei_url:
            raise ValueError("reranker.backend=tei benötigt reranker.tei_url")

        # Local runtime is deliberately lazy.  With backend=tei, importing this
        # module must not import torch/transformers or allocate model memory.
        self.torch = None
        self.tokenizer = None
        self.model = None
        self.device = None
        self.local_load_attempted = False
        self.local_loaded = False
        self.local_load_error = None
        self.disabled_reason = None

    # --------------------------------------------------------
    # Lokales Modell laden (nur local oder expliziter Fallback)
    # --------------------------------------------------------

    def _load_local(self):
        if self.local_loaded:
            return

        if self.local_load_attempted:
            if self.local_load_error:
                raise RuntimeError(
                    "Lokaler Reranker konnte nicht geladen werden: "
                    f"{self.local_load_error}"
                )
            return

        self.local_load_attempted = True

        try:
            # Heavy imports are intentionally local to this path.
            import torch
            from transformers import AutoTokenizer, AutoModelForSequenceClassification

            self.torch = torch
            self.device = torch.device(self.device_name)

            log.info(
                "Lade lokalen Reranker: model=%s device=%s",
                self.model_name,
                self.device,
            )

            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_name,
                local_files_only=True,
            )
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                local_files_only=True,
            )
            self.model = self.model.to(self.device)
            self.model.eval()

            self.local_loaded = True
            self.local_load_error = None
            log.info("Lokaler Reranker bereit.")

        except Exception as exc:
            self.local_loaded = False
            self.local_load_error = f"{type(exc).__name__}: {exc}"
            self.model = None
            self.tokenizer = None
            raise

    def load(self):
        """Preserve the old public API while allowing an explicit disabled mode."""
        if self.backend == "none":
            self.disabled_reason = "disabled by configuration"
            log.info("Reranker deaktiviert (backend=none).")
            return
        if self.backend == "local":
            self._load_local()
        else:
            # Remote TEI is contacted lazily on the first score call.  This
            # keeps API startup independent of temporary network outages.
            log.info(
                "Reranker konfiguriert: backend=tei url=%s fallback=%s",
                self.tei_url,
                self.fallback_backend,
            )

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    def status(self):
        return {
            "backend": self.backend,
            "model": self.model_name,
            "device": self.device_name if self.backend == "local" else ("remote" if self.backend == "tei" else None),
            "tei_url": self.tei_url if self.backend == "tei" else None,
            "timeout_seconds": self.timeout_seconds,
            "tei_batch_size": self.tei_batch_size,
            "fallback_backend": self.fallback_backend,
            # Legacy status keys remain for callers that knew only the local backend.
            "loaded": self.local_loaded,
            "load_attempted": self.local_load_attempted,
            "load_error": self.local_load_error,
            "local_loaded": self.local_loaded,
            "local_load_attempted": self.local_load_attempted,
            "local_load_error": self.local_load_error,
            "disabled_reason": self.disabled_reason,
            "private_network_only": self.private_network_only,
        }

    # --------------------------------------------------------
    # TEI bewerten
    # --------------------------------------------------------

    def _score_tei(self, query: str, texts: list[str]) -> list[dict]:
        endpoint = f"{self.tei_url}/rerank"
        started = time.perf_counter()
        all_scores: list[dict] = []
        calls = 0

        try:
            for start in range(0, len(texts), self.tei_batch_size):
                batch_texts = texts[start:start + self.tei_batch_size]
                calls += 1
                target_url = endpoint
                target_headers: dict[str, str] | None = None
                target_extensions: dict[str, str] | None = None
                if self.private_network_only:
                    target_url, host_header, target_extensions = pinned_private_target(endpoint)
                    target_headers = {"Host": host_header}
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    trust_env=not self.private_network_only,
                ) as client:
                    response = client.post(
                        target_url,
                        headers=target_headers,
                        extensions=target_extensions,
                        json={
                            "query": query,
                            "texts": batch_texts,
                            "truncate": True,
                            "raw_scores": False,
                            "return_text": False,
                        },
                    )
                response.raise_for_status()
                payload = response.json()

                if not isinstance(payload, list):
                    raise RuntimeError("TEI /rerank lieferte kein JSON-Array")

                scores_by_index: dict[int, float] = {}
                for item in payload:
                    if not isinstance(item, dict):
                        raise RuntimeError("TEI /rerank lieferte einen ungültigen Eintrag")
                    try:
                        index = int(item["index"])
                        score = float(item["score"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError("TEI /rerank Eintrag ohne gültigen index/score") from exc

                    if index < 0 or index >= len(batch_texts):
                        raise RuntimeError(f"TEI /rerank lieferte ungültigen Index {index}")
                    if index in scores_by_index:
                        raise RuntimeError(f"TEI /rerank lieferte Index {index} doppelt")
                    scores_by_index[index] = score

                missing = [idx for idx in range(len(batch_texts)) if idx not in scores_by_index]
                if missing:
                    raise RuntimeError(
                        "TEI /rerank lieferte nicht für alle Kandidaten einen Score: "
                        + ",".join(str(idx) for idx in missing[:10])
                    )

                all_scores.extend(
                    {"raw_score": None, "score": scores_by_index[idx]}
                    for idx in range(len(batch_texts))
                )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.warning(
                "Reranker backend=tei failed: candidates=%d calls=%d elapsed_ms=%.0f error=%s: %s",
                len(texts),
                calls,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            raise RuntimeError(f"TEI-Reranker fehlgeschlagen: {type(exc).__name__}: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.info(
            "Reranker backend=tei candidates=%d calls=%d elapsed_ms=%.0f",
            len(texts),
            calls,
            elapsed_ms,
        )
        return all_scores

    # --------------------------------------------------------
    # Lokale Bewertung
    # --------------------------------------------------------

    def _score_local(self, query: str, texts: list[str]) -> list[dict]:
        self._load_local()
        torch = self.torch
        started = time.perf_counter()
        results = []

        try:
            for start in range(0, len(texts), self.batch_size):
                batch_texts = texts[start:start + self.batch_size]
                queries = [query for _ in batch_texts]

                inputs = self.tokenizer(
                    queries,
                    batch_texts,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                inputs = {key: value.to(self.device) for key, value in inputs.items()}

                with torch.no_grad():
                    logits = self.model(**inputs, return_dict=True).logits.view(-1).float()
                    normalized = torch.sigmoid(logits)

                for raw, score in zip(logits.tolist(), normalized.tolist()):
                    results.append({"raw_score": raw, "score": score})

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.info(
                "Reranker backend=local candidates=%d elapsed_ms=%.0f device=%s",
                len(texts),
                elapsed_ms,
                self.device_name,
            )
            return results

        except torch.cuda.OutOfMemoryError as exc:
            self.disabled_reason = "CUDA out of memory"
            try:
                torch.cuda.empty_cache()
            except Exception:
                log.debug("Failed to empty CUDA cache after reranker OOM", exc_info=True)
            raise RuntimeError("CUDA-Speicher für Reranker reicht nicht aus") from exc

    # --------------------------------------------------------
    # Query/Text-Paare bewerten
    # --------------------------------------------------------

    def score(self, query: str, texts: list[str]) -> list[dict]:
        if not texts:
            return []

        if self.backend == "none":
            raise RuntimeError("Reranker ist per Konfiguration deaktiviert")

        if self.disabled_reason:
            raise RuntimeError(f"Reranker wurde deaktiviert: {self.disabled_reason}")

        if self.backend == "local":
            return self._score_local(query, texts)

        try:
            return self._score_tei(query, texts)
        except Exception:
            if self.fallback_backend != "local":
                raise
            log.warning(
                "TEI-Reranker nicht verfügbar; explizit konfigurierter local-Fallback wird verwendet"
            )
            return self._score_local(query, texts)

    # --------------------------------------------------------
    # Bereits gefundene Dokumente neu sortieren
    # --------------------------------------------------------

    def rerank(
        self,
        query: str,
        results: list[dict],
        candidate_limit: int = 20,
        top_k: int = 8,
        min_score: float | None = None,
    ) -> list[dict]:
        if not results:
            return []

        candidates = results[:candidate_limit]
        usable_candidates = []
        texts = []

        for item in candidates:
            text = build_reranker_text(item)
            if not text:
                continue
            usable_candidates.append(item)
            texts.append(text)

        if not usable_candidates:
            return []

        scores = self.score(query, texts)
        reranked = []

        for item, score_info in zip(usable_candidates, scores):
            new_item = dict(item)
            new_item["reranker_score"] = score_info["score"]
            new_item["reranker_raw_score"] = score_info.get("raw_score")

            if min_score is not None and score_info["score"] < min_score:
                continue
            reranked.append(new_item)

        reranked.sort(
            key=lambda item: (
                item.get("reranker_score", 0.0),
                item.get("rrf", 0.0),
            ),
            reverse=True,
        )
        return reranked[:top_k]


# ------------------------------------------------------------
# Singleton
# ------------------------------------------------------------

reranker = Reranker(private_network_only=PRIVATE_NETWORK_ONLY)
_profile_rerankers: dict[tuple, Reranker] = {}
_profile_rerankers_lock = threading.Lock()


def _merged_reranker_config(config_override: dict | None) -> dict:
    if not config_override:
        return dict(reranker_config)
    merged = dict(reranker_config)
    merged.update(dict(config_override))
    return merged


def _reranker_key(cfg: dict) -> tuple:
    return (
        str(cfg.get("backend", "none") or "none").strip().lower(),
        str(cfg.get("model", MODEL_NAME) or MODEL_NAME).strip(),
        str(cfg.get("device", DEVICE) or DEVICE).strip(),
        int(cfg.get("max_length", MAX_LENGTH) or MAX_LENGTH),
        int(cfg.get("batch_size", BATCH_SIZE) or BATCH_SIZE),
        str(cfg.get("tei_url", TEI_URL) or "").strip().rstrip("/"),
        float(cfg.get("timeout_seconds", TIMEOUT_SECONDS) or TIMEOUT_SECONDS),
        int(cfg.get("tei_batch_size", TEI_BATCH_SIZE) or TEI_BATCH_SIZE),
        str(cfg.get("fallback_backend", FALLBACK_BACKEND) or "none").strip().lower(),
    )


def _reranker_for_config(config_override: dict | None) -> Reranker:
    if not config_override:
        return reranker
    cfg = _merged_reranker_config(config_override)
    key = _reranker_key(cfg)
    if key == _reranker_key(dict(reranker_config)):
        return reranker
    with _profile_rerankers_lock:
        selected = _profile_rerankers.get(key)
        if selected is None:
            selected = Reranker(
                backend=key[0],
                model_name=key[1],
                device=key[2],
                max_length=key[3],
                batch_size=key[4],
                tei_url=key[5],
                timeout_seconds=key[6],
                tei_batch_size=key[7],
                fallback_backend=key[8],
                private_network_only=PRIVATE_NETWORK_ONLY,
            )
            _profile_rerankers[key] = selected
        return selected


def load_reranker():
    reranker.load()


def get_reranker_status(config_override: dict | None = None):
    return _reranker_for_config(config_override).status()


def rerank_results(
    query: str,
    results: list[dict],
    candidate_limit: int = 20,
    top_k: int = 8,
    min_score: float | None = None,
    config_override: dict | None = None,
) -> list[dict]:
    selected = _reranker_for_config(config_override)
    return selected.rerank(
        query=query,
        results=results,
        candidate_limit=candidate_limit,
        top_k=top_k,
        min_score=min_score,
    )
