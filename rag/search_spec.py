"""Backend-neutral SearchSpec contract shared by provider and retrieval API.

The LLM describes intent; it never owns Elasticsearch syntax. 0.8.8 aligns the
contract with SunaQ Find while retaining the legacy 0.8.7 fields for stored
records and external callers.
"""
from __future__ import annotations

import re
from typing import Any


def _clean_text(value: Any, *, max_len: int = 300) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
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


def _clean_enum(value: Any, allowed: set[str], default: str = "unknown") -> str:
    text = _clean_text(value, max_len=64).casefold()
    return text if text in allowed else default


def _clean_concept_groups(value: Any, *, max_items: int = 12) -> list[dict[str, Any]]:
    """Normalize staged lexical alternatives.

    New planners emit primary_terms + expansion_terms. Legacy terms-only groups
    remain accepted: the first legacy term is primary and the remaining terms
    are expansion terms. The terms field is always retained as the deduplicated
    union for 0.8.7-compatible consumers.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen_groups: set[tuple[bool, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        primary = _clean_list(item.get("primary_terms"), max_items=6)
        expansion = _clean_list(item.get("expansion_terms"), max_items=12)
        legacy = _clean_list(item.get("terms"), max_items=12)

        if not primary and legacy:
            primary = [legacy[0]]
            expansion = _clean_list(expansion + legacy[1:], max_items=12)

        terms = _clean_list(primary + expansion, max_items=18)
        if not terms:
            continue

        primary_keys = {term.casefold() for term in primary}
        expansion = [term for term in expansion if term.casefold() not in primary_keys]
        terms = _clean_list(primary + expansion, max_items=18)

        required = bool(item.get("required", False))
        key = (required, tuple(term.casefold() for term in terms))
        if key in seen_groups:
            continue
        seen_groups.add(key)
        out.append({
            "required": required,
            "primary_terms": primary,
            "expansion_terms": expansion,
            "terms": terms,
        })
        if len(out) >= max_items:
            break
    return out


def _promote_canonical_concepts(
    groups: list[dict[str, Any]],
    concepts: list[str],
) -> list[dict[str, Any]]:
    """Keep exact canonical concept forms in the primary retrieval stage."""
    if not groups or not concepts:
        return groups
    canonical = {text.casefold(): text for text in concepts if text}
    promoted: list[dict[str, Any]] = []
    for source in groups:
        group = dict(source)
        primary = list(group.get("primary_terms") or [])
        expansion = list(group.get("expansion_terms") or [])
        primary_seen = {str(term).casefold() for term in primary}

        for term in group.get("terms") or []:
            folded = str(term).strip().casefold()
            if folded in canonical and folded not in primary_seen:
                primary.append(canonical[folded])
                primary_seen.add(folded)

        expansion = [
            str(term)
            for term in expansion
            if str(term).strip() and str(term).casefold() not in primary_seen
        ]
        group["primary_terms"] = _clean_list(primary, max_items=6)
        group["expansion_terms"] = _clean_list(expansion, max_items=12)
        group["terms"] = _clean_list(
            group["primary_terms"] + group["expansion_terms"],
            max_items=18,
        )
        promoted.append(group)
    return promoted


def _clean_term_interpretations(
    value: Any,
    *,
    original_query: str,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = {
        "probable_typo",
        "normalization",
        "preserved",
        "ambiguous",
        "unrecognized",
    }
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        original = _clean_text(item.get("original"))
        status = _clean_text(item.get("status"), max_len=32).casefold()
        alternatives = _clean_list(item.get("alternatives"), max_items=4)
        key = (original.casefold(), status)
        if (
            not original
            or status not in allowed
            or key in seen
            or (original_query and original not in original_query)
        ):
            continue
        seen.add(key)
        out.append({
            "original": original,
            "status": status,
            "alternatives": alternatives,
        })
        if len(out) >= max_items:
            break
    return out


def _clean_alternative_interpretations(
    value: Any,
    *,
    max_items: int = 2,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        meaning = _clean_text(item.get("meaning"))
        query = _clean_text(item.get("query"), max_len=1200)
        key = (meaning.casefold(), query.casefold())
        if not meaning or not query or key in seen:
            continue
        seen.add(key)
        out.append({"meaning": meaning, "query": query})
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
    semantic_query = (
        _clean_text(raw.get("semantic_query"), max_len=1200)
        if isinstance(raw.get("semantic_query"), str)
        else ""
    )
    if not semantic_query:
        semantic_query = _clean_text(original_query, max_len=1200)

    elastic_query = (
        _clean_text(raw.get("elastic_query"), max_len=1200)
        if isinstance(raw.get("elastic_query"), str)
        else ""
    )
    lexical_query = (
        _clean_text(raw.get("lexical_query"), max_len=1200)
        if isinstance(raw.get("lexical_query"), str)
        else ""
    )
    if not lexical_query:
        lexical_query = _clean_text(original_query, max_len=1200)

    concepts = _clean_list(raw.get("concepts"), max_items=16)
    concept_groups = _promote_canonical_concepts(
        _clean_concept_groups(raw.get("concept_groups")),
        concepts,
    )

    return {
        "retrieval_intent": _clean_enum(
            raw.get("retrieval_intent"),
            {"direct_lexical", "planned_semantic", "ambiguous"},
        ),
        "lexical_sufficiency": _clean_enum(
            raw.get("lexical_sufficiency"),
            {"high", "medium", "low"},
        ),
        "lexical_query": lexical_query,
        "term_interpretations": _clean_term_interpretations(
            raw.get("term_interpretations"),
            original_query=original_query,
        ),
        "elastic_query": elastic_query,
        "semantic_query": semantic_query,
        "alternative_interpretations": _clean_alternative_interpretations(
            raw.get("alternative_interpretations")
        ),
        "entities": _clean_list(raw.get("entities"), max_items=16),
        "entity_hints": _clean_entity_hints(raw.get("entity_hints"), max_items=16),
        "concepts": concepts,
        "concept_groups": concept_groups,
        "constraints": _clean_constraints(raw.get("constraints")),
        "verification_requirements": _clean_list(
            raw.get("verification_requirements"), max_items=16
        ),
        "answer_requirements": _clean_list(
            raw.get("answer_requirements"), max_items=16
        ),
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
        str(value.get("retrieval_intent") or ""),
        str(value.get("lexical_sufficiency") or ""),
        str(value.get("lexical_query") or ""),
        tuple(
            (
                str(item.get("original") or ""),
                str(item.get("status") or ""),
                tuple(str(v) for v in (item.get("alternatives") or [])),
            )
            for item in (value.get("term_interpretations") or [])
            if isinstance(item, dict)
        ),
        str(value.get("elastic_query") or ""),
        str(value.get("semantic_query") or ""),
        tuple(
            (str(item.get("meaning") or ""), str(item.get("query") or ""))
            for item in (value.get("alternative_interpretations") or [])
            if isinstance(item, dict)
        ),
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
                tuple(str(term) for term in (item.get("primary_terms") or [])),
                tuple(str(term) for term in (item.get("expansion_terms") or [])),
            )
            for item in (value.get("concept_groups") or [])
            if isinstance(item, dict)
        ),
        tuple(
            (str(item.get("kind") or ""), str(item.get("value") or ""))
            for item in (value.get("constraints") or [])
            if isinstance(item, dict)
        ),
        tuple(value.get("verification_requirements") or []),
        tuple(value.get("answer_requirements") or []),
    )
