"""Small structured retrieval-intent contract shared by provider and API.

The planner describes intent; it does not own Elasticsearch syntax. A SearchSpec
therefore carries entities, concept alternative groups, constraints and one
natural semantic query. Backend-specific files queries are compiled
deterministically by the middleware. ``elastic_query`` remains accepted only
as a backwards-compatibility field for older callers.
"""
from __future__ import annotations

import re
from typing import Any


def _clean_text(value: Any, *, max_len: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len]


def _clean_list(value: Any, *, max_items: int = 24) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        folded = text.casefold()
        if not text or folded in seen:
            continue
        seen.add(folded)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _clean_concept_groups(value: Any, *, max_items: int = 12) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[bool, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        terms = _clean_list(item.get("terms"), max_items=12)
        if not terms:
            continue
        required = bool(item.get("required", False))
        key = (required, tuple(term.casefold() for term in terms))
        if key in seen:
            continue
        seen.add(key)
        out.append({"required": required, "terms": terms})
        if len(out) >= max_items:
            break
    return out


def _clean_entity_hints(value: Any, *, max_items: int = 16) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    allowed = {"person", "organization", "location", "address"}
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        kind = _clean_text(item.get("kind"), max_len=32).casefold()
        key = (text.casefold(), kind)
        if not text or kind not in allowed or key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "kind": kind})
        if len(out) >= max_items:
            break
    return out



def _clean_constraints(value: Any, *, max_items: int = 12) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = _clean_text(item.get("kind"), max_len=120)
        val = _clean_text(item.get("value"), max_len=300)
        key = (kind.casefold(), val.casefold())
        if not kind or not val or key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "value": val})
        if len(out) >= max_items:
            break
    return out


def normalize_search_spec(value: Any, *, original_query: str = "") -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    raw_semantic_query = raw.get("semantic_query")
    semantic_query = (
        _clean_text(raw_semantic_query, max_len=1200)
        if isinstance(raw_semantic_query, str)
        else ""
    )
    if not semantic_query:
        semantic_query = _clean_text(original_query, max_len=1200)
    raw_elastic_query = raw.get("elastic_query")
    elastic_query = (
        _clean_text(raw_elastic_query, max_len=1200)
        if isinstance(raw_elastic_query, str)
        else ""
    )

    return {
        "elastic_query": elastic_query,
        "semantic_query": semantic_query,
        # Structured retrieval intent. The files compiler consumes entities,
        # concept_groups and safe literal constraints deterministically.
        "entities": _clean_list(raw.get("entities"), max_items=16),
        "entity_hints": _clean_entity_hints(raw.get("entity_hints"), max_items=16),
        "concepts": _clean_list(raw.get("concepts"), max_items=16),
        "concept_groups": _clean_concept_groups(raw.get("concept_groups")),
        "constraints": _clean_constraints(raw.get("constraints")),
        "verification_requirements": _clean_list(raw.get("verification_requirements"), max_items=16),
        # Legacy structured lexical fields remain accepted for old callers and
        # diagnostics, but rc3's query rewriter no longer emits them.
        "must": _clean_list(raw.get("must")),
        "should": _clean_list(raw.get("should")),
        "must_not": _clean_list(raw.get("must_not")),
        "phrases": _clean_list(raw.get("phrases")),
        "must_not_phrases": _clean_list(raw.get("must_not_phrases")),
    }


def has_lexical_terms(spec: dict[str, Any] | None) -> bool:
    value = spec or {}
    return (
        bool(str(value.get("elastic_query") or "").strip())
        or bool(value.get("entities"))
        or bool(value.get("concept_groups"))
        or any(value.get(field) for field in ("must", "should", "phrases", "must_not", "must_not_phrases"))
    )


def positive_lexical_terms(spec: dict[str, Any] | None) -> bool:
    value = spec or {}
    if str(value.get("elastic_query") or "").strip():
        return True
    if value.get("entities") or value.get("concept_groups"):
        return True
    return any(value.get(field) for field in ("must", "should", "phrases"))


def query_frame_from_search_spec(spec: dict[str, Any] | None, *, intent: str = "") -> dict[str, Any]:
    value = spec or {}
    entities = [
        {"id": f"q{index}", "text": text, "role": ""}
        for index, text in enumerate(value.get("entities") or [], start=1)
        if str(text or "").strip()
    ]
    return {
        "intent": _clean_text(intent, max_len=500),
        "entities": entities,
        "relations": [],
        "constraints": list(value.get("constraints") or []),
        "concepts": list(value.get("concepts") or []),
        "concept_groups": list(value.get("concept_groups") or []),
    }


def fingerprint(spec: dict[str, Any] | None) -> tuple:
    value = spec or {}
    return (
        str(value.get("elastic_query") or ""),
        str(value.get("semantic_query") or ""),
        tuple(value.get("entities") or []),
        tuple(
            (str(item.get("text") or ""), str(item.get("kind") or ""))
            for item in (value.get("entity_hints") or [])
            if isinstance(item, dict)
        ),
        tuple(value.get("concepts") or []),
        tuple(
            (
                bool(item.get("required")),
                tuple(str(term) for term in (item.get("terms") or [])),
            )
            for item in (value.get("concept_groups") or [])
            if isinstance(item, dict)
        ),
        tuple((str(item.get("kind") or ""), str(item.get("value") or "")) for item in (value.get("constraints") or []) if isinstance(item, dict)),
        tuple(value.get("verification_requirements") or []),
    )
