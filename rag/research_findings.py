"""Helpers for persistent positive research findings.

RC10 deliberately does *not* run another graph-extraction model.  It preserves
semantic work that already happened in the retrieval planner and candidate
verifier.  Only positive, document-grounded matches are eligible.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from rag.retrieval_planner import normalize_query_frame

# Keep the legacy provenance code stable because it participates in
# deterministic Finding IDs and therefore identifies existing graph data.
PROVENANCE_CODE = "aki_research"
PROVENANCE_LABEL = "SunaQ Recherche"


def _clean(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def canonical_query_frame(value: Any) -> dict[str, Any]:
    """Return a stable semantic frame independent of planner-local entity IDs.

    Planner entity IDs (e.g. e1/e2) are local implementation details.  Findings
    should coalesce when the same semantic frame is produced with a different
    local numbering or entity order, so entities are sorted by their textual
    role and remapped to stable q1/q2/... identifiers before hashing.
    """
    frame = normalize_query_frame(value)
    entities = list(frame.get("entities") or [])
    entities.sort(key=lambda item: (
        str(item.get("text") or "").casefold(),
        str(item.get("role") or "").casefold(),
        str(item.get("id") or "").casefold(),
    ))

    id_map: dict[str, str] = {}
    canonical_entities: list[dict[str, str]] = []
    for index, item in enumerate(entities, start=1):
        old_id = str(item.get("id") or "")
        new_id = f"q{index}"
        id_map[old_id] = new_id
        canonical_entities.append({
            "id": new_id,
            "text": _clean(item.get("text"), 300),
            "role": _clean(item.get("role"), 160),
        })

    relations: list[dict[str, str]] = []
    for item in frame.get("relations") or []:
        source = id_map.get(str(item.get("source") or ""))
        target = id_map.get(str(item.get("target") or ""))
        predicate = _clean(item.get("predicate"), 300)
        if source and target and predicate:
            relations.append({"source": source, "predicate": predicate, "target": target})
    relations.sort(key=lambda item: (item["source"], item["predicate"].casefold(), item["target"]))

    constraints = [
        {"kind": _clean(item.get("kind"), 120), "value": _clean(item.get("value"), 300)}
        for item in (frame.get("constraints") or [])
        if isinstance(item, dict) and _clean(item.get("kind"), 120) and _clean(item.get("value"), 300)
    ]
    constraints.sort(key=lambda item: (item["kind"].casefold(), item["value"].casefold()))

    concepts = sorted(
        {_clean(item, 240) for item in (frame.get("concepts") or []) if _clean(item, 240)},
        key=str.casefold,
    )

    return {
        "intent": _clean(frame.get("intent"), 240),
        "entities": canonical_entities,
        "relations": relations,
        "constraints": constraints,
        "concepts": concepts,
    }


def query_frame_has_structure(value: Any) -> bool:
    """Return true when a frame contains reusable semantics beyond a generic intent."""
    frame = canonical_query_frame(value)
    return bool(frame["entities"] or frame["relations"] or frame["constraints"] or frame["concepts"])


def query_frame_hash(value: Any) -> str:
    frame = canonical_query_frame(value)
    payload = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def curation_frame_hash(value: Any) -> str:
    """Stable curation fingerprint independent of free-form intent wording.

    The full QueryFrame remains ResearchRun provenance. Shared Finding curation
    should coalesce when the structured entities/relations/constraints/concepts
    are identical even if two provider runs phrase the intent differently.
    """
    frame = canonical_query_frame(value)
    curation_frame = {
        "entities": frame["entities"],
        "relations": frame["relations"],
        "constraints": frame["constraints"],
        "concepts": frame["concepts"],
    }
    payload = json.dumps(curation_frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def finding_id(document_id: str, query_frame: Any, *, provenance_code: str = PROVENANCE_CODE) -> str:
    """Legacy-compatible deterministic Finding ID.

    Cross-run curation coalescing uses curation_frame_hash at persistence time.
    Keeping the original ID formula avoids duplicating existing graph nodes on
    upgrade.
    """
    basis = "\0".join((
        str(provenance_code or PROVENANCE_CODE),
        str(document_id or ""),
        query_frame_hash(query_frame),
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def frame_relation_texts(value: Any) -> list[str]:
    """Render stable human-readable relation descriptors without creating fact edges."""
    frame = canonical_query_frame(value)
    by_id = {item["id"]: item["text"] for item in frame["entities"]}
    out: list[str] = []
    for relation in frame["relations"]:
        source = by_id.get(relation["source"], relation["source"])
        target = by_id.get(relation["target"], relation["target"])
        out.append(f"{source} | {relation['predicate']} | {target}"[:1000])
    return out


def _evidence_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def evidence_frame_view(value: Any) -> dict[str, Any]:
    """Return a stable, presentation-oriented view of verifier evidence."""
    frame = _evidence_mapping(value)

    def records(name: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in frame.get(name) or []:
            if isinstance(item, dict):
                out.append({str(k): _clean(v, 1000) for k, v in item.items() if _clean(v, 1000)})
            elif _clean(item, 1000):
                out.append({"value": _clean(item, 1000)})
        return out

    view = {
        "concepts": [_clean(item, 500) for item in (frame.get("concepts") or []) if _clean(item, 500)],
        "constraints": records("constraints"),
        "entities": records("entities"),
        "mentioned_entities": records("mentioned_entities"),
        "relations": records("relations"),
    }
    view["has_content"] = any(view[name] for name in view)
    return view


def evidence_entity_candidates(value: Any) -> list[dict[str, str]]:
    """Collect curator-gated Entity candidates from an EvidenceFrame."""
    frame = _evidence_mapping(value)
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(raw: Any, role: str) -> None:
        text = _clean(raw, 300)
        key = text.casefold()
        if not text or key in seen or re.fullmatch(r"[eq]\d+", text, flags=re.IGNORECASE):
            return
        seen.add(key)
        out.append({"text": text, "role": _clean(role, 160)})

    for bucket, default_role in (("entities", "Evidenz"), ("mentioned_entities", "Erwähnung")):
        for item in frame.get(bucket) or []:
            if isinstance(item, dict):
                text = next((item.get(name) for name in ("text", "display_name", "name", "value", "entity_text") if _clean(item.get(name), 300)), "")
                add(text, item.get("role") or item.get("kind") or default_role)
            else:
                add(item, default_role)

    entity_kind_cues = (
        "person", "organisation", "organization", "firma", "unternehmen",
        "gesellschaft", "behörde", "behoerde", "gericht", "entity", "entität", "entitaet",
    )
    for item in frame.get("constraints") or []:
        if isinstance(item, dict):
            kind = _clean(item.get("kind"), 160)
            if any(cue in kind.casefold() for cue in entity_kind_cues):
                add(item.get("value"), kind or "Constraint")

    # Relation sources are often actors and remain useful fallback candidates.
    # Targets are deliberately not promoted here: verifier targets frequently
    # contain clauses, objects, or grouped names. A target that was independently
    # extracted as an Entity/mention is already included by the buckets above.
    for relation in frame.get("relations") or []:
        if isinstance(relation, dict):
            add(relation.get("source") or relation.get("subject"), "Relationsquelle")
    return out


def merge_entity_candidates(*groups: Any) -> list[dict[str, str]]:
    """Merge candidate groups by normalized text while preserving first role."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if isinstance(item, dict):
                text, role = _clean(item.get("text"), 300), _clean(item.get("role"), 160)
            else:
                text, role = _clean(item, 300), ""
            # Hide target-only candidates persisted by older releases. The same
            # text remains eligible when another group supplies it as an Entity
            # or mention before this legacy Relationsziel record is encountered.
            if role.casefold() == "relationsziel":
                continue
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append({"text": text, "role": role})
    return out
