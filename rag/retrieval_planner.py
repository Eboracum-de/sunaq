"""Pure helpers for the bounded RC8 retrieval planner.

The provider owns LLM orchestration; this module deliberately contains only
configuration and normalization logic so planner decisions remain easy to test.
The original user query is never replaced: generated probes are additions.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Iterable

from rag.search_text import normalize_query_quotes


@dataclass(frozen=True)
class RetrievalPlannerSettings:
    enabled: bool = True
    thinking: bool = False
    max_retrieval_rounds: int = 2
    max_queries_per_round: int = 3
    model: str = ""
    max_tokens: int = 700
    context_max_chars: int = 12000
    max_complete_documents: int = 15
    overflow_acl_scan_limit: int = 80
    verification_candidate_limit: int = 6
    bounded_verification_candidate_limit: int = 30
    exhaustive_verification_candidate_limit: int = 30
    verification_max_chars_per_document: int = 3500
    verification_max_tokens: int = 1400
    verification_batch_size: int = 5


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def load_retrieval_planner_settings(
    config: dict[str, Any] | None,
    *,
    env: dict[str, str] | None = None,
) -> RetrievalPlannerSettings:
    """Load RC8 settings, with provider.env only as a legacy rounds fallback."""
    cfg = dict((config or {}).get("retrieval_planner") or {})
    environment = os.environ if env is None else env
    legacy_rounds = _bounded_int(environment.get("MAX_RETRIEVAL_ROUNDS", 2), 2, 1, 8)
    max_complete_documents = _bounded_int(
        cfg.get("max_complete_documents", 15), 15, 1, 100
    )
    # The strict overflow gate must be able to observe at least one document
    # beyond the completeness capacity. Otherwise an administrator could
    # accidentally configure a scan that can never prove overflow.
    overflow_acl_scan_limit = _bounded_int(
        cfg.get("overflow_acl_scan_limit", 80), 80, 1, 500
    )
    overflow_acl_scan_limit = max(
        max_complete_documents + 1, overflow_acl_scan_limit
    )

    return RetrievalPlannerSettings(
        enabled=_truthy(cfg.get("enabled"), True),
        thinking=_truthy(cfg.get("thinking"), False),
        max_retrieval_rounds=_bounded_int(
            cfg.get("max_retrieval_rounds", legacy_rounds), legacy_rounds, 1, 8
        ),
        max_queries_per_round=_bounded_int(
            cfg.get("max_queries_per_round", 3), 3, 1, 8
        ),
        model=str(cfg.get("model") or "").strip(),
        max_tokens=_bounded_int(cfg.get("max_tokens", 700), 700, 128, 4096),
        context_max_chars=_bounded_int(
            cfg.get("context_max_chars", 12000), 12000, 2000, 100000
        ),
        max_complete_documents=max_complete_documents,
        overflow_acl_scan_limit=overflow_acl_scan_limit,
        verification_candidate_limit=_bounded_int(
            cfg.get("verification_candidate_limit", 6), 6, 1, 60
        ),
        bounded_verification_candidate_limit=_bounded_int(
            cfg.get("bounded_verification_candidate_limit", 30), 30, 1, 60
        ),
        exhaustive_verification_candidate_limit=_bounded_int(
            cfg.get("exhaustive_verification_candidate_limit", 30), 30, 1, 60
        ),
        verification_max_chars_per_document=_bounded_int(
            cfg.get("verification_max_chars_per_document", 3500), 3500, 1000, 12000
        ),
        verification_max_tokens=_bounded_int(
            cfg.get("verification_max_tokens", 1400), 1400, 256, 4096
        ),
        verification_batch_size=_bounded_int(
            cfg.get("verification_batch_size", 5), 5, 1, 10
        ),
    )


def strict_overflow_reason(
    *,
    es_total: int,
    scanned_documents: int,
    authorized_documents: int,
    max_complete_documents: int,
    scan_limit: int,
) -> str | None:
    """Classify the bounded post-ACL completeness gate without leaking counts.

    ``authorized_overflow`` is definitive. ``acl_scan_limit_reached`` is a
    conservative fail-closed result: the strict ES set is larger than the
    bounded authorization window, but proving whether enough later documents
    are visible would require more Nextcloud ACL traffic.
    """
    max_complete_documents = max(1, int(max_complete_documents))
    scan_limit = max(max_complete_documents + 1, int(scan_limit))
    authorized_documents = max(0, int(authorized_documents))
    scanned_documents = max(0, int(scanned_documents))
    es_total = max(0, int(es_total))

    if authorized_documents > max_complete_documents:
        return "authorized_overflow"
    if es_total > scanned_documents and scanned_documents >= scan_limit:
        return "acl_scan_limit_reached"
    return None


_EXHAUSTIVE_NOUNS = (
    r"rechnungen?|dokumente?|dateien?|schreiben|vertraege?|verträge?|"
    r"e-?mails?|mails?|belege?|angebote?|kontoauszuege?|kontoauszüge?|"
    r"protokolle?|bescheide?|briefe?|vermerke?|anlagen?"
)


def detect_exhaustive_intent(question: str) -> bool:
    """Detect only explicit completeness/counting requests.

    Ordinary requests such as ``Suche die Rechnungen ...`` are normal retrieval:
    they may use a small context verifier, but they do not claim completeness.
    """
    text = normalize_query_quotes(str(question or "")).strip().casefold()
    if not text:
        return False
    if re.search(r"\b(?:alle|saemtliche|sämtliche|wirklich\s+alle|vollstaendige|vollständige)\b.*\b(?:" + _EXHAUSTIVE_NOUNS + r")\b", text):
        return True
    if re.search(r"\b(?:wie\s+viele|zaehle|zähle|anzahl)\b.{0,80}\b(?:" + _EXHAUSTIVE_NOUNS + r")\b", text):
        return True
    if re.search(r"\b(?:vollstaendige|vollständige)\s+(?:liste|uebersicht|übersicht)\b", text):
        return True
    return False


def detect_bounded_document_set(question: str) -> bool:
    """Detect a concretely bounded document-set request without claiming completeness.

    A document-type noun plus an explicit language-neutral literal constraint
    (year/date/identifier) is enough to justify a larger verification window.
    This deliberately does *not* turn the request into an exhaustive search.
    """
    text = normalize_query_quotes(str(question or "")).strip().casefold()
    if not text or detect_exhaustive_intent(text):
        return False
    if not re.search(r"\b(?:" + _EXHAUSTIVE_NOUNS + r"|invoices?|documents?|files?|receipts?|offers?|statements?|minutes?)\b", text):
        return False
    constraints = extract_safe_hard_constraints(question)
    return bool(constraints.get("years") or constraints.get("dates") or constraints.get("identifiers"))


def is_strict_lexical_query(query: str) -> bool:
    """Return true only for an actually restrictive ES-style query."""
    text = normalize_query_quotes(str(query or "")).strip()
    if not text:
        return False
    must_words = re.findall(r"(?<!\S)\+[\wÄÖÜäöüß][\wÄÖÜäöüß0-9._/-]*", text)
    must_phrases = re.findall(r'(?<!\S)\+"[^"\r\n]+"', text)
    # Two independent mandatory anchors are the minimum for the overflow gate.
    return len(must_words) + len(must_phrases) >= 2



_COMPLETE_FILENAME_EXTENSIONS = (
    "pdf|odt|ods|odp|doc|docx|xls|xlsx|ppt|pptx|rtf|txt|csv|eml|msg|"
    "jpg|jpeg|png|tif|tiff|gif|webp|zip|7z|rar|xml|json|yaml|yml"
)
_COMPLETE_FILENAME_RE = re.compile(
    rf"(?:^|[\s\"'])([^\s\"'<>|]+\.(?:{_COMPLETE_FILENAME_EXTENSIONS}))(?=$|[\s,;:!?\"'])",
    re.IGNORECASE,
)


def extract_complete_filename(question: str) -> str:
    """Return an explicitly written basename with a known file extension."""
    text = normalize_query_quotes(str(question or "")).strip()
    match = _COMPLETE_FILENAME_RE.search(text)
    return match.group(1).strip().rstrip(".,;:!?") if match else ""


_STRICT_FILLER_TERMS = {
    "suche", "such", "finde", "find", "zeige", "zeig", "liste", "list",
    "prüfe", "pruefe", "prüfen", "pruefen", "analysiere", "analysieren",
    "bitte", "möchte", "moechte", "soll", "sollen",
}


def build_deterministic_strict_query(question: str, plan: Any) -> str:
    """Build a conservative mandatory ES query from the mature SearchPlan.

    This is used only as the first hard probe for exhaustive/document-set
    requests.  It deliberately reuses planner terms and resolved original
    entity mentions instead of asking the retrieval-planner LLM whether such a
    probe should exist.  Known aliases remain available to the normal hybrid
    probes; the hard gate itself stays tied to what the user actually said.
    """
    if plan is None:
        return ""

    anchors: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(value: Any, phrase: bool = False) -> None:
        text = _clean_query(value)
        if not text:
            return
        folded = text.casefold()
        if folded in _STRICT_FILLER_TERMS or folded in seen:
            return
        seen.add(folded)
        anchors.append((text, phrase or bool(re.search(r"\s", text))))

    # Explicit user control syntax remains strongest.
    for value in getattr(plan, "must", []) or []:
        add(value)
    for value in getattr(plan, "phrases", []) or []:
        add(value, phrase=True)

    # Entity-aware planning already groups aliases.  For the hard probe use
    # the original mention, not an inferred alias, to avoid broadening the
    # completeness gate beyond the user's criteria.
    groups = list(getattr(plan, "entity_groups", []) or [])
    for group in groups:
        if isinstance(group, dict):
            add(group.get("mention"), phrase=True)

    if not groups:
        for entity in getattr(plan, "entity_mentions", []) or []:
            if not isinstance(entity, dict):
                continue
            if str(entity.get("status") or "") not in {"resolved", "ambiguous", "fuzzy_candidate"}:
                continue
            add(entity.get("mention"), phrase=True)

    # Remaining lexical terms carry document type, date/year and other
    # concrete restrictions.  Query-control verbs are discarded.
    for value in getattr(plan, "should", []) or []:
        add(value)

    rendered: list[str] = []
    for text, phrase in anchors:
        escaped = text.replace('"', '\\"')
        rendered.append(f'+"{escaped}"' if phrase else f'+{escaped}')

    candidate = " ".join(rendered)
    return candidate if is_strict_lexical_query(candidate) else ""


_SAFE_YEAR_RE = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
_SAFE_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)"
)
_SAFE_IDENTIFIER_RE = re.compile(
    r"(?<![\w.-])(?=[A-Za-z0-9._/-]{5,}(?![\w.-]))"
    r"(?=[A-Za-z0-9._/-]*[A-Za-z])(?=[A-Za-z0-9._/-]*\d)"
    r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+(?![\w.-])"
)


def extract_safe_hard_constraints(question: str) -> dict[str, list[str]]:
    """Extract only language-neutral literal constraints safe for hard filtering.

    Entity names and ordinary content words are deliberately *not* returned.
    They are valuable retrieval signals but too brittle to become mandatory
    filters in natural-language enterprise search.
    """
    text = normalize_query_quotes(str(question or "")).strip()
    result: dict[str, list[str]] = {
        "filenames": [],
        "identifiers": [],
        "dates": [],
        "years": [],
    }

    filename = extract_complete_filename(text)
    if filename:
        result["filenames"].append(filename)

    def unique(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            value = str(value or "").strip()
            folded = value.casefold()
            if value and folded not in seen:
                seen.add(folded)
                out.append(value)
        return out

    result["dates"] = unique(match.group(0) for match in _SAFE_DATE_RE.finditer(text))
    result["years"] = unique(match.group(0) for match in _SAFE_YEAR_RE.finditer(text))
    result["identifiers"] = unique(
        match.group(0) for match in _SAFE_IDENTIFIER_RE.finditer(text)
        if not filename or match.group(0).casefold() not in filename.casefold()
    )
    return result


def safe_constraint_summary(question: str) -> str:
    constraints = extract_safe_hard_constraints(question)
    parts: list[str] = []
    for key in ("filenames", "identifiers", "dates", "years"):
        values = constraints.get(key) or []
        if values:
            parts.append(f"{key}=" + ", ".join(values))
    return "; ".join(parts) or "(keine sicheren harten Constraints)"

_SIMPLE_BOOLEAN_GROUP_RE = re.compile(
    r'(?<!\S)([+-])\(\s*("[^"\r\n]+"|[\wÄÖÜäöüß][\wÄÖÜäöüß0-9._/-]*)\s*\)'
)
_BOOLEAN_CONTROL_RE = re.compile(r'(?<!\S)[+-](?=\(|"|[\wÄÖÜäöüß])')


def normalize_boolean_probe_syntax(query: str) -> str:
    """Canonicalize the small Boolean subset understood by the ES planner.

    LLMs sometimes emit a search-engine-looking atom such as ``+(Rechnung)``
    even though the middleware syntax is ``+Rechnung``.  Flatten only a single
    token or quoted phrase inside the parentheses; complex groups remain
    untouched and can therefore never be mistaken for a valid strict probe.
    """
    text = normalize_query_quotes(str(query or "")).strip()
    previous = None
    while text != previous:
        previous = text
        text = _SIMPLE_BOOLEAN_GROUP_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    return re.sub(r"\s+", " ", text).strip()


def has_boolean_control_syntax(query: str) -> bool:
    """Return whether a generated probe contains + / - Boolean controls."""
    return bool(_BOOLEAN_CONTROL_RE.search(normalize_query_quotes(str(query or ""))))


def semanticize_query(query: str) -> str:
    """Remove Boolean control syntax before a query is used as semantic text."""
    text = normalize_boolean_probe_syntax(query)
    # A complex generated Boolean group is not valid strict syntax, but its
    # content is still useful as natural-language recall text.
    text = re.sub(r'(?<!\S)[+-]\(\s*([^()\r\n]+?)\s*\)', r'\1', text)
    text = re.sub(r'(?<!\S)[+-](?="|[\wÄÖÜäöüß])', "", text)
    text = text.replace('"', "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_query_frame(value: Any) -> dict[str, Any]:
    """Normalize the planner's free-form graph-light representation.

    The vocabulary is intentionally open: predicates, roles, constraint kinds
    and concepts are model/user-data derived strings, not a document ontology.
    Only the structural envelope is fixed.
    """
    if not isinstance(value, dict):
        return {"intent": "", "entities": [], "relations": [], "constraints": [], "concepts": [], "concept_groups": []}

    entities: list[dict[str, str]] = []
    known_ids: set[str] = set()
    for item in (value.get("entities") or [])[:12]:
        if not isinstance(item, dict):
            continue
        entity_id = _clean_query(item.get("id"))[:40]
        text = _clean_query(item.get("text"))[:300]
        role = _clean_query(item.get("role"))[:160]
        if not entity_id or not text or entity_id in known_ids:
            continue
        known_ids.add(entity_id)
        entities.append({"id": entity_id, "text": text, "role": role})

    relations: list[dict[str, str]] = []
    for item in (value.get("relations") or [])[:12]:
        if not isinstance(item, dict):
            continue
        source = _clean_query(item.get("source"))[:40]
        predicate = _clean_query(item.get("predicate"))[:300]
        target = _clean_query(item.get("target"))[:40]
        if not source or not predicate or not target:
            continue
        if source not in known_ids or target not in known_ids:
            continue
        relations.append({"source": source, "predicate": predicate, "target": target})

    constraints: list[dict[str, str]] = []
    for item in (value.get("constraints") or [])[:12]:
        if not isinstance(item, dict):
            continue
        kind = _clean_query(item.get("kind"))[:120]
        constraint_value = _clean_query(item.get("value"))[:300]
        if kind and constraint_value:
            constraints.append({"kind": kind, "value": constraint_value})

    concepts: list[str] = []
    seen_concepts: set[str] = set()
    for item in (value.get("concepts") or [])[:16]:
        concept = _clean_query(item)[:240]
        folded = concept.casefold()
        if concept and folded not in seen_concepts:
            seen_concepts.add(folded)
            concepts.append(concept)

    concept_groups: list[dict[str, Any]] = []
    for item in (value.get("concept_groups") or [])[:12]:
        if not isinstance(item, dict):
            continue
        terms: list[str] = []
        seen_terms: set[str] = set()
        for raw_term in (item.get("terms") or [])[:12]:
            term = _clean_query(raw_term)[:240]
            folded = term.casefold()
            if term and folded not in seen_terms:
                seen_terms.add(folded)
                terms.append(term)
        if terms:
            concept_groups.append({
                "required": bool(item.get("required", False)),
                "terms": terms,
            })

    return {
        "intent": _clean_query(value.get("intent"))[:240],
        "entities": entities,
        "relations": relations,
        "constraints": constraints,
        "concepts": concepts,
        "concept_groups": concept_groups,
    }


def _clean_query(value: Any) -> str:
    text = normalize_query_quotes(str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def normalize_generated_probes(
    original_query: str,
    generated: Iterable[dict[str, Any]],
    *,
    max_additional: int,
    retrieval_arms: set[str] | None,
    already_seen: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Validate model probes and enforce hard user arm selectors.

    The model cannot widen an explicit /files, /vector or /graph selection.  A
    vector-only request additionally rejects Boolean probe syntax: its query is
    converted to plain semantic text.
    """
    max_additional = max(0, int(max_additional))
    if max_additional == 0:
        return []

    selected_arms = set(retrieval_arms or {"files", "vector", "graph"})
    vector_only = selected_arms == {"vector"}
    seen = {_clean_query(original_query).casefold()}
    seen.update(_clean_query(value).casefold() for value in already_seen if _clean_query(value))
    result: list[dict[str, str]] = []

    for item in generated:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "semantic").strip().casefold()
        if kind not in {"strict_lexical", "lexical", "semantic"}:
            kind = "semantic"
        query = normalize_boolean_probe_syntax(_clean_query(item.get("query")))
        if not query:
            continue

        # Boolean control syntax belongs exclusively to strict_lexical.  A
        # model may label a perfectly usable +foo +bar query as semantic or
        # lexical; when a files arm exists, recover that intent instead of
        # sending Boolean punctuation through a non-strict view.  Invalid or
        # weak Boolean output is reduced to plain recall text.
        boolean_controls = has_boolean_control_syntax(query)
        if kind != "strict_lexical" and boolean_controls:
            if "files" in selected_arms and is_strict_lexical_query(query):
                kind = "strict_lexical"
            else:
                query = semanticize_query(query) or query

        # A model-generated strict_lexical probe is allowed as an additional
        # ES recall view, but never becomes a completeness claim. It must
        # contain at least two mandatory anchors; malformed/weak Boolean
        # output is downgraded to an ordinary lexical probe.
        if kind == "strict_lexical" and not is_strict_lexical_query(query):
            kind = "lexical"
            query = semanticize_query(query) or query

        if vector_only:
            query = semanticize_query(query)
            kind = "semantic"
            if not query:
                continue

        folded = query.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append({"kind": kind, "query": query})
        if len(result) >= max_additional:
            break
    return result


def original_probe(original_query: str) -> dict[str, str]:
    query = _clean_query(original_query)
    return {"kind": "original", "query": query, "semantic_query": query}
