#!/usr/bin/env python3
"""Incremental evidence-document graph indexer for Graph v3 (middleware v0.7).

Graph v3 remains conservative: it grows the identity catalogue and adds document-grounded relation observations:

- evidence documents are fetched from Elasticsearch by document_id;
- a configurable LLM backend extracts only explicitly named Person/Organization observations;
- observations are validated against the actual document text;
- a separate deterministic resolver checks whether the entity is already known;
- observations pass a conservative admission gate before an unknown name may become a provisional entity;
- the expanded catalogue is then used for exact/fuzzy Document->Entity mention linking;
- a second LLM pass sees only exact resolved entities from the same chunk and may emit grounded Claim/RelationObservation records;
- these observations retain verbatim source evidence and never become unqualified Entity->Entity fact edges.

The module can also be run as a CLI for deterministic testing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
from rapidfuzz import fuzz

from rag.llm_backend import build_llm_backend
from rag.evidence_boundary import guarded_evidence_prompt, serialize_evidence_records
from rag.secret_env import secret_env
from rag.logging_utils import get_logger
from rag.mail_metadata import MailMetadataReader, message_key, reply_parent_id, representation_role
from rag.ontology import load_relation_ontology, ontology_prompt, relation_schema, validate_relation_semantics
from rag.source_date import infer_source_date

from rag.elasticsearch_client import httpx_options as elastic_httpx_options
from rag.graph import (
    BASE_DIR,
    GraphStore,
    blocked_generic_names,
    cfg_get,
    load_config,
    normalize_name,
)

EXTRACTOR_VERSION = "graph_claims_v3b3"
log = get_logger("worker")


@dataclass
class MentionCandidate:
    entity_id: str
    entity_type: str
    entity_kind: str
    display_name: str
    form_value: str
    form_type: str
    form_weight: float
    similarity: float
    score: float


@dataclass
class DetectedMention:
    observed_value: str
    normalized: str
    status: str
    count: int = 1
    candidates: list[MentionCandidate] = field(default_factory=list)

    @property
    def resolved_entity_id(self) -> str | None:
        if self.status != "resolved_exact" or len(self.candidates) != 1:
            return None
        return self.candidates[0].entity_id


def _entity_type(labels: list[str]) -> str:
    if "Person" in labels:
        return "PERSON"
    if "OrganizationalUnit" in labels:
        return "ORGANIZATIONAL_UNIT"
    if "Organization" in labels:
        return "ORGANIZATION"
    return "ENTITY"


_COURT_KIND_RE = re.compile(r"\b(?:amtsgericht|landgericht|oberlandesgericht|bundesgerichtshof|verwaltungsgericht|arbeitsgericht|sozialgericht|finanzgericht|registergericht)\b", re.IGNORECASE)
_BANK_KIND_RE = re.compile(r"\b(?:bank|sparkasse|landesbank|postbank|kreditbank)\b", re.IGNORECASE)
_LAWFIRM_KIND_RE = re.compile(r"\b(?:kanzlei|rechtsanw[aä]lte?|anwaltskanzlei|law\s+firm)\b", re.IGNORECASE)
_AUTHORITY_KIND_RE = re.compile(r"\b(?:ministerium|bundesamt|landesamt|finanzamt|beh[oö]rde|stadtverwaltung|gemeinde|kreisverwaltung)\b", re.IGNORECASE)
_ASSOCIATION_KIND_RE = re.compile(r"\b(?:e\.?v\.?|verein|verband|kammer|stiftung|genossenschaft)\b", re.IGNORECASE)
_COMPANY_KIND_RE = re.compile(r"\b(?:gmbh|ug|ag|se|kg|ohg|gbr|mbh|ltd\.?|limited|inc\.?|corp\.?)\b", re.IGNORECASE)


def _infer_entity_kind(name: str, labels: list[str] | None = None) -> str:
    labels = list(labels or [])
    if "Person" in labels:
        return "Person"
    value = str(name or "").strip()
    if _COURT_KIND_RE.search(value):
        return "Court"
    if _BANK_KIND_RE.search(value):
        return "Bank"
    if _LAWFIRM_KIND_RE.search(value):
        return "LawFirm"
    if _AUTHORITY_KIND_RE.search(value):
        return "Authority"
    if _ASSOCIATION_KIND_RE.search(value):
        return "Association"
    if _COMPANY_KIND_RE.search(value):
        return "Company"
    return "Other" if "Organization" in labels or not labels else "Other"


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize like normalize_name(), retaining a best-effort source char map."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    pieces: list[str] = []
    mapping: list[int] = []
    in_space = True
    for idx, char in enumerate(text):
        norm = normalize_name(char)
        if norm:
            for c in norm:
                pieces.append(c)
                mapping.append(idx)
            in_space = False
        else:
            if not in_space and pieces:
                pieces.append(" ")
                mapping.append(idx)
                in_space = True
    while pieces and pieces[-1] == " ":
        pieces.pop()
        mapping.pop()
    return "".join(pieces), mapping


def _original_value(text: str, charmap: list[int], start: int, end: int, fallback: str) -> str:
    if not charmap or end <= start:
        return fallback
    orig_start = charmap[min(start, len(charmap) - 1)]
    orig_end = charmap[min(end - 1, len(charmap) - 1)] + 1
    value = text[orig_start:orig_end].strip(" \t\r\n,;:()[]{}")
    return value or fallback


def _dedup_forms(forms: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_form: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for form in forms:
        normalized = str(form.get("normalized") or "").strip()
        entity_id = str(form.get("entity_id") or "")
        if len(normalized) < 3 or not entity_id:
            continue
        key = (normalized, entity_id)
        if key in seen:
            continue
        seen.add(key)
        by_form.setdefault(normalized, []).append(form)
    return by_form


def _candidate(form: dict[str, Any], similarity: float) -> MentionCandidate:
    weight = float(form.get("weight", 0.0) or 0.0)
    score = max(0.0, min(1.0, weight * similarity / 100.0))
    labels = list(form.get("labels") or [])
    display_name = str(form.get("display_name") or "")
    return MentionCandidate(
        entity_id=str(form.get("entity_id") or ""),
        entity_type=_entity_type(labels),
        entity_kind=str(form.get("entity_kind") or _infer_entity_kind(display_name, labels)),
        display_name=display_name,
        form_value=str(form.get("value") or ""),
        form_type=str(form.get("form_type") or "name"),
        form_weight=round(weight, 4),
        similarity=round(float(similarity), 2),
        score=round(score, 4),
    )


def _dedup_candidates(candidates: Iterable[MentionCandidate]) -> list[MentionCandidate]:
    best: dict[str, MentionCandidate] = {}
    for candidate in candidates:
        old = best.get(candidate.entity_id)
        if old is None or candidate.score > old.score:
            best[candidate.entity_id] = candidate
    return sorted(best.values(), key=lambda x: (x.score, x.similarity), reverse=True)


def detect_document_mentions(
    text: str,
    forms: list[dict[str, Any]],
    *,
    fuzzy: bool = True,
    fuzzy_threshold: float = 92.0,
    fuzzy_max_candidates: int = 5,
    max_fuzzy_form_tokens: int = 6,
) -> list[DetectedMention]:
    """Detect seed-backed mentions in one document.

    Fuzzy matching is deliberately anchor-based: at least one token of the known
    form must occur exactly in the document.  This catches common one-token OCR
    errors such as ``Mäx Mustermann`` while greatly reducing graph pollution.
    """
    text = str(text or "")
    normalized_text, charmap = _normalized_with_map(text)
    if not normalized_text:
        return []

    by_form = _dedup_forms(forms)
    occupied: list[tuple[int, int]] = []
    collected: dict[tuple[str, str], DetectedMention] = {}
    exact_forms_found: set[str] = set()

    # ------------------------------------------------------------
    # Exact phrase matches. A spelling can point to several entities.
    # Prefer the longest overlapping form, so e.g. "Musterhof 280 VV UG"
    # is not counted again as the shorter alias "Musterhof 280" at the
    # same character position.
    # ------------------------------------------------------------
    exact_hits: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for known_form, raw_candidates in by_form.items():
        pattern = re.compile(rf"(?<!\w){re.escape(known_form)}(?!\w)")
        for match in pattern.finditer(normalized_text):
            exact_hits.append((match.start(), match.end(), known_form, raw_candidates))

    exact_hits.sort(key=lambda x: (x[1] - x[0], max(float(c.get("weight", 0.0)) for c in x[3])), reverse=True)
    accepted_exact: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for hit in exact_hits:
        start, end, known_form, raw_candidates = hit
        if any(not (end <= s or start >= e) for s, e, _, _ in accepted_exact):
            continue
        accepted_exact.append(hit)
        exact_forms_found.add(known_form)
    accepted_exact.sort(key=lambda x: x[0])

    for start, end, known_form, raw_candidates in accepted_exact:
        candidates = _dedup_candidates(_candidate(item, 100.0) for item in raw_candidates)
        status = "resolved_exact" if len(candidates) == 1 else "ambiguous_exact"
        occupied.append((start, end))
        observed = _original_value(text, charmap, start, end, known_form)
        key = (normalize_name(observed), status)
        mention = collected.get(key)
        if mention is None:
            collected[key] = DetectedMention(
                observed_value=observed,
                normalized=normalize_name(observed) or known_form,
                status=status,
                count=1,
                candidates=candidates,
            )
        else:
            mention.count += 1
            mention.candidates = _dedup_candidates(mention.candidates + candidates)

    if not fuzzy:
        return sorted(collected.values(), key=lambda m: (m.status, m.normalized))

    # ------------------------------------------------------------
    # Conservative fuzzy pass. Tokenize normalized document once.
    # A known form is checked only around positions of exact anchor tokens.
    # ------------------------------------------------------------
    token_matches = list(re.finditer(r"\S+", normalized_text))
    tokens = [m.group(0) for m in token_matches]
    token_positions: dict[str, list[int]] = {}
    for idx, token in enumerate(tokens):
        token_positions.setdefault(token, []).append(idx)

    fuzzy_seen_spans: set[tuple[int, int, str]] = set()

    for known_form, raw_candidates in by_form.items():
        if known_form in exact_forms_found:
            continue
        known_tokens = known_form.split()
        n = len(known_tokens)
        if n < 2 or n > max_fuzzy_form_tokens:
            continue

        # Prefer informative exact anchors (long token or number).  At least one
        # must survive OCR exactly; otherwise v1 declines to guess.
        anchors = sorted(
            set(known_tokens),
            key=lambda t: (t.isdigit(), len(t)),
            reverse=True,
        )
        anchor_positions: set[int] = set()
        for anchor in anchors:
            if len(anchor) < 3 and not anchor.isdigit():
                continue
            anchor_positions.update(token_positions.get(anchor, []))

        for anchor_idx in anchor_positions:
            # The anchor may be any token inside the known form. Inspect windows
            # of the same length and +/- one token around it.
            for length in sorted(set([max(2, n - 1), n, n + 1])):
                for start_idx in range(max(0, anchor_idx - length + 1), min(anchor_idx + 1, len(tokens) - length + 1)):
                    end_idx = start_idx + length
                    if not (start_idx <= anchor_idx < end_idx):
                        continue
                    start = token_matches[start_idx].start()
                    end = token_matches[end_idx - 1].end()
                    if any(not (end <= s or start >= e) for s, e in occupied):
                        continue
                    span = normalized_text[start:end]
                    similarity = float(fuzz.ratio(span, known_form))
                    if similarity < fuzzy_threshold:
                        continue
                    span_key = (start, end, known_form)
                    if span_key in fuzzy_seen_spans:
                        continue
                    fuzzy_seen_spans.add(span_key)

                    candidates = _dedup_candidates(
                        _candidate(item, similarity) for item in raw_candidates
                    )[:fuzzy_max_candidates]
                    if not candidates:
                        continue
                    observed = _original_value(text, charmap, start, end, span)
                    norm_observed = normalize_name(observed) or span
                    key = (norm_observed, "fuzzy_candidate")
                    mention = collected.get(key)
                    if mention is None:
                        collected[key] = DetectedMention(
                            observed_value=observed,
                            normalized=norm_observed,
                            status="fuzzy_candidate",
                            count=1,
                            candidates=candidates,
                        )
                    else:
                        mention.count += 1
                        mention.candidates = _dedup_candidates(mention.candidates + candidates)

    return sorted(
        collected.values(),
        key=lambda m: (
            0 if m.status == "resolved_exact" else 1,
            -m.count,
            m.normalized,
        ),
    )


@dataclass
class EntityObservation:
    canonical_name: str
    entity_type: str
    entity_kind: str
    mention_context: str
    observed_text: str
    context_text: str
    confidence: float
    relevant_actor: bool
    count: int = 1

    @property
    def name(self) -> str:
        # Compatibility for diagnostics/rebuild summaries written against v2a.
        return self.canonical_name


@dataclass
class RelationObservation:
    subject_entity_id: str
    predicate: str
    predicate_text: str
    relation_text: str
    object_entity_id: str
    evidence_text: str
    confidence: float
    stance: str
    chunk_index: int = 0
    valid_from: str = ""
    valid_to: str = ""


def _normalize_predicate(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    return value[:64]


# Relation JSON schema is generated from ontology/relations-v1.yaml at runtime.


DEFAULT_RELATION_PROMPT = r"""Du extrahierst ausschliesslich EXPLIZIT belegte Beziehungen zwischen bereits aufgeloesten Entities.

Du bekommst eine abschliessende Ontologie erlaubter Predicates und eine Liste erlaubter Entity-IDs mit Typ/Kind. Verwende NUR diese IDs und NUR Predicates aus der Ontologie. Erfinde weder Entities noch Relationsnamen.

Eine blosse gemeinsame Erwaehnung ist KEINE Beziehung. Briefkopf, Footer, Anschrift, raeumliche Naehe, gleiche Zeile oder OCR-Nachbarschaft reichen allein nicht. Footer/Briefkopf duerfen echte explizite Angaben enthalten (z.B. „Vorstand: ...“, „Vorsitzender des Aufsichtsrats: ...“, „Amtsgericht ... / Handelsregister HRB ...“); extrahiere nur genau die dadurch ausgedrueckte Relation.

Fuer jede Beziehung:
- subject_entity_id/object_entity_id: exakt aus der erlaubten Liste.
- predicate: exakt ein Predicate aus der gelieferten Ontologie.
- predicate_text: knappe Beschreibung der im Text behaupteten Beziehung.
- relation_text: die kleinste WOERTLICHE Passage aus dem Ausschnitt, die den Beziehungsausdruck bzw. Relationshinweis selbst traegt (z.B. „Vorstand: M.Mustermann“ oder „Amtsgericht Frankfurt am Main / Handelsregister HRB 72567“). Keine Paraphrase.
- evidence_text: WOERTLICH kopierte, ausreichend grosse Passage aus dem Ausschnitt, die Subject, Object und relation_text gemeinsam belegt. Keine Paraphrase.
- stance: asserted fuer als Tatsache dargestellte Aussage; negated fuer ausdrueckliche Verneinung; alleged fuer Behauptung/Zuschreibung; questioned fuer ausdruecklich in Frage gestellte Beziehung; conditional fuer Bedingung/Hypothese; unknown nur wenn keine sichere Zuordnung moeglich ist.
- confidence bewertet nur, wie sicher die Passage genau diese Relation ausdrueckt, nicht ob sie objektiv wahr ist.
- valid_from/valid_to nur setzen, wenn der zeitliche Geltungsbereich in derselben Passage ausdruecklich erkennbar ist, sonst leere Zeichenfolge.

Keine Relation aus Weltwissen oder plausibler Schlussfolgerung ergaenzen. Wenn keine Relation der Ontologie explizit passt: KEINE Relation ausgeben. Antworte ausschliesslich im JSON-Schema."""


ENTITY_DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "type": {"type": "string", "enum": ["Person", "Organization"]},
                    "observed_text": {"type": "string"},
                    "context_text": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "relevant_actor": {"type": "boolean"},
                    "mention_context": {
                        "type": "string",
                        "enum": ["actor", "identity_metadata", "quoted", "incidental"],
                    },
                    "entity_kind": {
                        "type": "string",
                        "enum": ["Person", "Company", "Court", "Authority", "LawFirm", "Bank", "Association", "Other"],
                    },
                },
                "required": [
                    "canonical_name", "type", "observed_text", "context_text",
                    "confidence", "relevant_actor", "mention_context", "entity_kind"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entities"],
    "additionalProperties": False,
}


DOCUMENT_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
        "relevant_actors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["Person", "Organization", "Other"]},
                    "role": {"type": "string"},
                },
                "required": ["name", "type", "role"],
                "additionalProperties": False,
            },
            "maxItems": 32,
        },
        "uncertainties": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
    },
    "required": ["document_type", "summary", "key_points", "relevant_actors", "uncertainties"],
    "additionalProperties": False,
}


DEFAULT_DOCUMENT_SUMMARY_PROMPT = r"""Du erstellst eine knappe, strukturierte Dokumentanalyse als abgeleitetes Arbeitsprodukt fuer spaetere Vorgangs-/Ordneranalysen.

Die Analyse ist KEINE neue primaere Evidenz und darf keine Tatsachen erfinden. Verwende nur den bereitgestellten Dokumenttext sowie die optional mitgelieferten bereits erkannten Entities/Relationen.

- document_type: kurze sachliche Gattung, z.B. E-Mail, Vertrag, Schriftsatz, Protokoll, Rechnung, Registerauszug, Webseite, Brief.
- summary: neutrale Zusammenfassung des Dokumentinhalts, typischerweise 3-8 Saetze.
- key_points: die fuer eine spaetere Vorgangsanalyse wichtigsten Aussagen/Handlungen/Entscheidungen.
- relevant_actors: nur Personen/Organisationen, die im Dokument fuer den Vorgang eine erkennbare Rolle spielen; Footer-/Impressumsmetadaten nicht automatisch als Akteur behandeln. role ist eine knappe textnahe Rollenbeschreibung.
- uncertainties: erkennbare Unsicherheiten, OCR-Probleme, Widersprueche oder Punkte, die aus diesem Dokument allein nicht sicher entschieden werden koennen.

Bei langen Dokumenten kann der bereitgestellte Text gekuerzt sein. Dann keine Vollstaendigkeit behaupten. Keine Rechts-/Sachbewertung aus Weltwissen. Antworte ausschliesslich im JSON-Schema."""


DEFAULT_DISCOVERY_PROMPT = r"""Du extrahierst identitaetsfaehige Personen und Organisationen aus einem Dokumentausschnitt fuer einen evidenzorientierten Knowledge Graph.

Erlaubte Basistypen:
- Person: konkrete natuerliche Person mit erkennbarem Eigennamen.
- Organization: konkrete Firma, Behoerde, Kanzlei, Verein/Verband, Bank, Gericht, Stiftung oder andere benannte Organisation.

Der Graph soll NICHT jedes Named Entity sammeln. Jede Beobachtung wird deshalb zusaetzlich nach ihrem Kontext klassifiziert:
- actor: konkreter Akteur des Vorgangs/Dokuments, z.B. Partei, Gesellschaft, Gericht im Verfahren, Vertreter, Organmitglied, Vertragspartner, Absender, Empfaenger, Eigentuemmer oder Gesellschafter.
- identity_metadata: identitaetsbezogene Metadaten, die eine Entity beschreiben oder einordnen, aber keine eigene Handlung im Dokument darstellen, z.B. Vorstand/Aufsichtsrat/Registergericht im Footer oder Briefkopf.
- quoted: Entity kommt nur in einem zitierten/weitergeleiteten Fremdtext oder historischen/literarischen Beispiel vor.
- incidental: zufaellige oder sachlich nebensächliche Erwaehnung ohne Rolle fuer Vorgang oder Identitaet.

entity_kind klassifiziert nur grob und darf keine neue Tatsache erfinden:
- Person fuer Person.
- Company fuer Unternehmen/Gesellschaften.
- Court fuer Gerichte/Registergerichte.
- Authority fuer sonstige Behoerden/oeffentliche Stellen.
- LawFirm fuer Kanzleien.
- Bank fuer Banken/Sparkassen.
- Association fuer Verein, Verband, Kammer, Stiftung oder Genossenschaft.
- Other fuer sonstige Organisationen oder wenn keine engere Einordnung aus dem Namen/Text sicher ist.

Fuer jede Beobachtung:
1. observed_text ist die exakte Zeichenfolge aus dem Ausschnitt, in der der Name steht.
2. canonical_name ist derselbe Name in identitaetsgeeigneter Form. Bei Personen Anrede und akademische Titel (Herr/Herrn/Frau/Dr./Prof.) entfernen, den eigentlichen Namen NICHT korrigieren oder ergaenzen. Bei Organisationen Rechtsformen erhalten.
3. context_text ist eine kurze, WOERTLICH kopierte Passage aus dem Ausschnitt (moeglichst <= 300 Zeichen), die observed_text enthaelt und den lokalen Kontext zeigt.
4. relevant_actor=true genau dann, wenn mention_context=actor. Bei identity_metadata/quoted/incidental false.
5. mention_context und entity_kind nach obigen Regeln setzen.

NICHT als Entity-Kandidat ausgeben:
- Adressen, Strassen, Hausnummern
- URLs, Domains, E-Mail-Adressen
- Aktenzeichen, Registerzeichen, Dateinamen
- Rollenbezeichnungen ohne Eigennamen, z.B. „Beklagte zu 1.“, „Klaegerin“, „Geschaeftsfuehrer“
- Sammel-/Restbegriffe wie „u.a.“, „sonstige“, „die Beteiligten“
- Beziehungen oder Claims. Insbesondere KEINE Beteiligung, Organstellung oder Vertretungsmacht behaupten.

Wichtige Beispiele:
- „Herrn Dr. Gregor Brauer“ -> Person, Person, actor falls er am Vorgang beteiligt ist; canonical_name „Gregor Brauer“.
- „MSH Projekt UG“ -> Organization, Company.
- „Amtsgericht Frankfurt am Main“ in „Amtsgericht Frankfurt am Main / Handelsregister HRB ...“ eines Firmenfooters -> Organization, Court, identity_metadata.
- „Alexander Eichner“ in „Vorsitzender des Aufsichtsrats: Alexander Eichner“ eines Firmenfooters -> Person, identity_metadata (die spaetere Relationsextraktion darf die explizite Organrelation pruefen).
- „Kurfürstendamm 62“ -> keine Entity.
- „Beklagte zu 1.“ -> keine Entity.
- „Alexander der Große“ in historischem Beispiel/Zitat -> quoted oder weglassen.

ZUSAETZLICHE IDENTITAETSREGEL FUER ORGANISATIONSEINHEITEN:
Begriffe wie „Handelsregister“, „Insolvenzgericht“, „Grundbuchamt“, „Registergericht“, „Nachlassgericht“ oder eine Abteilung sind fuer sich allein KEINE eigenstaendige Organization. Extrahiere sie nicht ohne qualifizierenden Traeger/Ort.

Nur tatsaechlich im Ausschnitt vorhandene Namen. OCR-Fehler nicht kreativ korrigieren. Im Zweifel Kandidat weglassen oder incidental/quoted setzen. Antworte ausschliesslich im vorgegebenen JSON-Format."""


_PERSON_PREFIX_RE = re.compile(
    r"^(?:(?:herrn?|frau|fraeulein|fräulein|prof(?:essor)?\.?|dr\.?|"
    r"dipl\.?[- ]?ing\.?|rechtsanwalt(?:in)?|notar(?:in)?)\s+)+",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.(?:de|com|org|net|eu|info|biz)\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", re.IGNORECASE)
_ADDRESS_RE = re.compile(
    r"(?:str(?:asse|aße|\.)?|straße|strasse|weg|platz|allee|damm|ring|ufer|gasse|"
    r"chaussee|markt|wall|graben|hof|promenade|steig|pfad)\s+\d+[a-z]?(?:\s*[-/]\s*\d+[a-z]?)?\s*$",
    re.IGNORECASE,
)
_POSTAL_CITY_RE = re.compile(r"^\s*\d{5}\s+[A-Za-zÄÖÜäöüß]", re.IGNORECASE)
_FILE_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|odt|ods|txt|eml|msg|jpg|jpeg|png|tiff?)\s*$", re.IGNORECASE)
_REGISTER_RE = re.compile(r"^(?:hr[ab]|gnr|vr|pr|az|aktenzeichen)\s*[:.-]?\s*[a-z0-9./ -]+$", re.IGNORECASE)
_TRAILING_ET_AL_RE = re.compile(r"(?:\bu\.\s*a\.|\bu\s+a)\s*$", re.IGNORECASE)
_ROLE_ONLY_RE = re.compile(
    r"^(?:die\s+)?(?:beklagte?r?|klaeger(?:in)?|kläger(?:in)?|antragsteller(?:in)?|"
    r"antragsgegner(?:in)?|geschaeftsfuehrer(?:in)?|geschäftsführer(?:in)?|"
    r"gesellschafter(?:in)?|kaeufer(?:in)?|käufer(?:in)?|verkaeufer(?:in)?|verkäufer(?:in)?|"
    r"vertreter(?:in)?|bevollmaechtigte?r?|bevollmächtigte?r?|unterzeichner(?:in)?|zeuge|zeugin)"
    r"(?:\s+(?:zu|nr|nummer)\s*\.?\s*\d+)?\.?$",
    re.IGNORECASE,
)
_LEGAL_FORM_RE = re.compile(
    r"\b(?:gmbh|ug(?:\s*\(haftungsbeschraenkt\)|\s*\(haftungsbeschränkt\))?|ag|se|kg|ohg|gbr|kgaa|mbh|e\.?\s*v\.?)\b",
    re.IGNORECASE,
)
_ORG_CUE_RE = re.compile(
    r"\b(?:amtsgericht|landgericht|oberlandesgericht|bundesgerichtshof|verwaltungsgericht|"
    r"arbeitsgericht|finanzgericht|sozialgericht|staatsanwaltschaft|finanzamt|ministerium|"
    r"bundesamt|landesamt|universitaet|universität|hochschule|stadt|gemeinde|kammer|"
    r"stiftung|verein|kanzlei|bank|sparkasse|versicherung|polizei)\b",
    re.IGNORECASE,
)
_PERSON_CONTEXT_CUE_RE = re.compile(
    r"\b(?:herrn?|frau|dr\.?|prof\.?|geschaeftsfuehrer(?:in)?|geschäftsführer(?:in)?|"
    r"vorstand|aufsichtsrat|aufsichtsratsvorsitzende?r?|vorsitzende?r?\s+des\s+aufsichtsrats|rechtsanwalt(?:in)?|notar(?:in)?|vertreten\s+durch|bevollmaechtigt|bevollmächtigt|"
    r"gesellschafter(?:in)?|klaeger(?:in)?|kläger(?:in)?|beklagte?r?|antragsteller(?:in)?|"
    r"insolvenzverwalter(?:in)?|liquidator(?:in)?|prokurist(?:in)?|unterzeichner(?:in)?)\b",
    re.IGNORECASE,
)


def _canonical_person_name(value: str) -> str:
    value = str(value or "").strip(" \t\r\n,;:")
    previous = None
    while previous != value:
        previous = value
        value = _PERSON_PREFIX_RE.sub("", value).strip(" \t\r\n,;:")
    return value


def _hard_rejection_reason(name: str, observed_text: str) -> str | None:
    raw = str(name or observed_text or "").strip()
    norm = normalize_name(raw)
    if not raw or not norm:
        return "empty_name"
    if _URL_RE.search(raw) or _DOMAIN_RE.search(raw):
        return "url_or_domain"
    if _EMAIL_RE.search(raw):
        return "email_address"
    if _ADDRESS_RE.search(raw) or _POSTAL_CITY_RE.search(raw):
        return "postal_address"
    if _FILE_RE.search(raw):
        return "filename"
    if _REGISTER_RE.fullmatch(raw.strip()):
        return "register_or_case_identifier"
    if _ROLE_ONLY_RE.fullmatch(raw.strip()):
        return "role_without_identity"
    if _TRAILING_ET_AL_RE.search(raw) and not _LEGAL_FORM_RE.search(raw):
        return "collective_or_et_al_expression"
    if norm in {
        "sonstige", "die beteiligten", "beteiligte", "unbekannt", "unbekannte", "diverse",
        "u a", "ua", "usw", "etc", "n n", "nn",
    }:
        return "generic_or_collective_expression"
    if norm in {
        "handelsregister", "registergericht", "insolvenzgericht", "grundbuchamt",
        "nachlassgericht", "zivilabteilung", "strafabteilung",
    }:
        # These are organizational units, not globally identifiable organizations.
        # A qualified form such as "Handelsregister Amtsgericht Musterstadt" may still
        # be represented through the CardDAV parent-scoped OrganizationalUnit.
        return "unqualified_organizational_unit"
    return None

class StructuredOutputError(ValueError):
    """Structured LLM output remained invalid after a grounded retry."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        first_done_reason: Any = None,
        retry_done_reason: Any = None,
        first_chars: int = 0,
        retry_chars: int = 0,
    ):
        super().__init__(message)
        self.stage = stage
        self.first_done_reason = first_done_reason
        self.retry_done_reason = retry_done_reason
        self.first_chars = int(first_chars or 0)
        self.retry_chars = int(retry_chars or 0)


def _strip_json_payload(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if "</think>" in raw:
        raw = raw.rsplit("</think>", 1)[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Entity extractor returned no JSON object")
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Entity extractor JSON root must be an object")
    return data


def _chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    text = str(text or "")
    if not text:
        return []
    chunk_chars = max(2000, int(chunk_chars or 12000))
    overlap_chars = max(0, min(int(overlap_chars or 0), chunk_chars // 3))
    if len(text) <= chunk_chars:
        return [text]

    out: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        target = min(length, start + chunk_chars)
        end = target
        if target < length:
            floor = start + int(chunk_chars * 0.70)
            # Prefer a page/paragraph/line boundary near the target; do not
            # destroy the original text because observed_text validation relies
            # on it.
            candidates = [
                text.rfind("\n\f", floor, target),
                text.rfind("\n\n", floor, target),
                text.rfind("\n", floor, target),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end]
        if chunk.strip():
            out.append(chunk)
        if end >= length:
            break
        next_start = max(start + 1, end - overlap_chars)
        start = next_start
    return out


class GraphEvidenceIndexer:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.es_url = str(cfg_get(cfg, "elasticsearch.url", default="")).rstrip("/")
        self.es_index = str(cfg_get(cfg, "elasticsearch.index", default=""))
        self.es_timeout = float(cfg_get(cfg, "elasticsearch.timeout", default=60) or 60)
        self.es_httpx_options = elastic_httpx_options(cfg)
        self.enabled = bool(cfg_get(cfg, "graph_indexer.enabled", default=True))
        self.fuzzy = bool(cfg_get(cfg, "graph_indexer.fuzzy", default=True))
        self.fuzzy_threshold = float(cfg_get(cfg, "graph_indexer.fuzzy_threshold", default=92.0) or 92.0)
        self.fuzzy_max_candidates = int(cfg_get(cfg, "graph_indexer.fuzzy_max_candidates", default=5) or 5)
        self.max_documents = int(cfg_get(cfg, "graph_indexer.max_documents_per_call", default=8) or 8)
        self.max_content_chars = int(cfg_get(cfg, "graph_indexer.max_content_chars", default=500000) or 500000)
        # Automatic graph work deliberately skips compilation/oversize documents.
        # Explicit maintenance can bypass these limits with force_oversize=True.
        self.auto_max_text_chars = int(cfg_get(cfg, "graph_queue.auto_limits.max_text_chars", default=200000) or 0)
        self.auto_max_chunks = int(cfg_get(cfg, "graph_queue.auto_limits.max_chunks", default=60) or 0)
        self.auto_max_file_bytes = int(cfg_get(cfg, "graph_queue.auto_limits.max_file_bytes", default=10000000) or 0)
        self.blocked_generic_names = blocked_generic_names(cfg)
        # Hidden .mailmeta.json sidecars are read directly from WebDAV.  The
        # reader caches directory misses/hits so ordinary documents do not cause
        # repeated DAV lookups while a worker process is alive.
        self.mail_metadata_reader = MailMetadataReader(cfg)

        # Graph LLM entity discovery is independent from the answer provider
        # process, but uses the same backend abstraction. This keeps the worker
        # usable on local-Ollama and API-only nodes alike.
        self.discovery_enabled = bool(cfg_get(cfg, "graph_entity_discovery.enabled", default=True))
        self.discovery_backend_name = str(
            cfg_get(
                cfg,
                "graph_entity_discovery.backend",
                default=os.getenv("GRAPH_ENTITY_BACKEND") or os.getenv("LLM_BACKEND") or "ollama",
            )
            or "ollama"
        ).strip().lower()
        self.discovery_url = str(
            cfg_get(
                cfg,
                "graph_entity_discovery.url",
                default=os.getenv("GRAPH_ENTITY_URL") or os.getenv("LLM_BASE_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434",
            )
            or "http://127.0.0.1:11434"
        ).rstrip("/")
        self.discovery_model = str(
            cfg_get(
                cfg,
                "graph_entity_discovery.model",
                default=os.getenv("GRAPH_ENTITY_MODEL") or os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL") or "qwen3:8b",
            )
            or "qwen3:8b"
        )
        discovery_api_key_env = str(cfg_get(cfg, "graph_entity_discovery.api_key_env", default="") or "").strip()
        discovery_api_key = (
            secret_env(discovery_api_key_env, "") if discovery_api_key_env
            else secret_env("GRAPH_ENTITY_API_KEY", "") or secret_env("LLM_API_KEY", "")
        )
        self.discovery_api_key = discovery_api_key
        self.discovery_verify_tls = bool(cfg_get(cfg, "graph_entity_discovery.verify_tls", default=True))
        self.discovery_ca_file = str(cfg_get(cfg, "graph_entity_discovery.ca_file", default="") or "").strip() or None
        self.discovery_backend = build_llm_backend(
            self.discovery_backend_name,
            base_url=self.discovery_url,
            model=self.discovery_model,
            api_key=discovery_api_key,
            verify_tls=self.discovery_verify_tls,
            ca_file=self.discovery_ca_file,
        )
        self.discovery_timeout = float(cfg_get(cfg, "graph_entity_discovery.timeout", default=300) or 300)
        self.discovery_chunk_chars = int(cfg_get(cfg, "graph_entity_discovery.chunk_chars", default=12000) or 12000)
        self.discovery_overlap_chars = int(cfg_get(cfg, "graph_entity_discovery.overlap_chars", default=600) or 600)
        self.discovery_max_chunks = int(cfg_get(cfg, "graph_entity_discovery.max_chunks", default=0) or 0)
        self.discovery_min_confidence = float(cfg_get(cfg, "graph_entity_discovery.min_confidence", default=0.70) or 0.70)
        self.discovery_max_entities_per_chunk = int(cfg_get(cfg, "graph_entity_discovery.max_entities_per_chunk", default=40) or 40)
        self.discovery_num_ctx = int(cfg_get(cfg, "graph_entity_discovery.num_ctx", default=16384) or 16384)
        self.discovery_num_predict = int(cfg_get(cfg, "graph_entity_discovery.num_predict", default=1400) or 1400)
        self.discovery_adaptive_split_min_chars = max(2000, int(
            cfg_get(cfg, "graph_entity_discovery.adaptive_split_min_chars", default=3000) or 3000
        ))
        self.discovery_adaptive_split_max_depth = max(0, int(
            cfg_get(cfg, "graph_entity_discovery.adaptive_split_max_depth", default=2) or 0
        ))
        self.discovery_think = bool(cfg_get(cfg, "graph_entity_discovery.think", default=False))
        self.discovery_require_relevant_actor = bool(
            cfg_get(cfg, "graph_entity_discovery.require_relevant_actor", default=True)
        )
        self.discovery_person_auto_create_confidence = float(
            cfg_get(cfg, "graph_entity_discovery.person_auto_create_confidence", default=0.93) or 0.93
        )
        self.discovery_organization_auto_create_confidence = float(
            cfg_get(cfg, "graph_entity_discovery.organization_auto_create_confidence", default=0.88) or 0.88
        )
        self.discovery_person_require_context_cue = bool(
            cfg_get(cfg, "graph_entity_discovery.person_require_context_cue", default=True)
        )
        self.discovery_context_max_chars = int(
            cfg_get(cfg, "graph_entity_discovery.context_max_chars", default=500) or 500
        )
        prompt_path = str(cfg_get(cfg, "graph_entity_discovery.prompt_file", default="prompts/graph_entity_discovery.txt") or "").strip()
        path = Path(prompt_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            self.discovery_prompt = path.read_text(encoding="utf-8").strip()
        except OSError:
            self.discovery_prompt = DEFAULT_DISCOVERY_PROMPT.strip()

        # Graph v3: relation/claim discovery is a separate conservative pass.
        # It may use its own backend/model, but inherits entity-discovery settings
        # by default so local and API-only nodes need no duplicate configuration.
        self.relation_enabled = bool(cfg_get(cfg, "graph_relation_discovery.enabled", default=True))
        relation_backend_name = str(
            cfg_get(cfg, "graph_relation_discovery.backend", default=os.getenv("GRAPH_RELATION_BACKEND") or self.discovery_backend_name)
            or self.discovery_backend_name
        ).strip().lower()
        relation_url = str(
            cfg_get(cfg, "graph_relation_discovery.url", default=os.getenv("GRAPH_RELATION_URL") or self.discovery_url)
            or self.discovery_url
        ).rstrip("/")
        self.relation_model = str(
            cfg_get(cfg, "graph_relation_discovery.model", default=os.getenv("GRAPH_RELATION_MODEL") or self.discovery_model)
            or self.discovery_model
        )
        relation_api_key_env = str(cfg_get(cfg, "graph_relation_discovery.api_key_env", default="") or "").strip()
        relation_api_key = (
            secret_env(relation_api_key_env, "") if relation_api_key_env
            else secret_env("GRAPH_RELATION_API_KEY", "") or self.discovery_api_key or secret_env("LLM_API_KEY", "")
        )
        relation_verify_tls = bool(cfg_get(cfg, "graph_relation_discovery.verify_tls", default=self.discovery_verify_tls))
        relation_ca_file = str(cfg_get(cfg, "graph_relation_discovery.ca_file", default=self.discovery_ca_file or "") or "").strip() or None
        self.relation_backend = build_llm_backend(
            relation_backend_name,
            base_url=relation_url,
            model=self.relation_model,
            api_key=relation_api_key,
            verify_tls=relation_verify_tls,
            ca_file=relation_ca_file,
        )
        self.relation_timeout = float(cfg_get(cfg, "graph_relation_discovery.timeout", default=self.discovery_timeout) or self.discovery_timeout)
        self.relation_chunk_chars = int(cfg_get(cfg, "graph_relation_discovery.chunk_chars", default=self.discovery_chunk_chars) or self.discovery_chunk_chars)
        self.relation_overlap_chars = int(cfg_get(cfg, "graph_relation_discovery.overlap_chars", default=self.discovery_overlap_chars) or self.discovery_overlap_chars)
        self.relation_max_chunks = int(cfg_get(cfg, "graph_relation_discovery.max_chunks", default=0) or 0)
        self.relation_min_confidence = float(cfg_get(cfg, "graph_relation_discovery.min_confidence", default=0.72) or 0.72)
        self.relation_max_per_chunk = int(cfg_get(cfg, "graph_relation_discovery.max_relations_per_chunk", default=24) or 24)
        self.relation_evidence_max_chars = int(cfg_get(cfg, "graph_relation_discovery.evidence_max_chars", default=900) or 900)
        self.relation_num_ctx = int(cfg_get(cfg, "graph_relation_discovery.num_ctx", default=self.discovery_num_ctx) or self.discovery_num_ctx)
        self.relation_num_predict = int(cfg_get(cfg, "graph_relation_discovery.num_predict", default=1200) or 1200)
        self.relation_adaptive_split_min_chars = max(2000, int(
            cfg_get(cfg, "graph_relation_discovery.adaptive_split_min_chars", default=self.discovery_adaptive_split_min_chars)
            or self.discovery_adaptive_split_min_chars
        ))
        self.relation_adaptive_split_max_depth = max(0, int(
            cfg_get(cfg, "graph_relation_discovery.adaptive_split_max_depth", default=self.discovery_adaptive_split_max_depth)
            or 0
        ))
        self.relation_think = bool(cfg_get(cfg, "graph_relation_discovery.think", default=False))
        relation_prompt_path = str(cfg_get(cfg, "graph_relation_discovery.prompt_file", default="prompts/graph_relation_discovery.txt") or "").strip()
        relation_path = Path(relation_prompt_path)
        if not relation_path.is_absolute():
            relation_path = BASE_DIR / relation_path
        try:
            self.relation_prompt = relation_path.read_text(encoding="utf-8").strip()
        except OSError:
            self.relation_prompt = DEFAULT_RELATION_PROMPT.strip()

        self.relation_ontology = load_relation_ontology()
        self.relation_schema = relation_schema(self.relation_ontology)
        self.relation_ontology_prompt = ontology_prompt(self.relation_ontology)

        # Derived per-document summary cache. Kept out of config.yaml to avoid
        # inflating the main retrieval configuration; environment variables are
        # sufficient until the later batch/case-analysis feature gets its own UI.
        self.summary_enabled = os.getenv("GRAPH_DOCUMENT_SUMMARY_ENABLED", "true").lower() in {
            "1", "true", "yes", "on"
        }
        self.summary_model = os.getenv("GRAPH_DOCUMENT_SUMMARY_MODEL", self.relation_model)
        self.summary_timeout = float(os.getenv("GRAPH_DOCUMENT_SUMMARY_TIMEOUT", str(self.relation_timeout)))
        self.summary_max_chars = max(4000, int(os.getenv("GRAPH_DOCUMENT_SUMMARY_MAX_CHARS", "30000")))
        self.summary_num_ctx = max(4096, int(os.getenv("GRAPH_DOCUMENT_SUMMARY_NUM_CTX", str(self.relation_num_ctx))))
        self.summary_num_predict = max(300, int(os.getenv("GRAPH_DOCUMENT_SUMMARY_NUM_PREDICT", "900")))
        self.summary_think = os.getenv("GRAPH_DOCUMENT_SUMMARY_THINK", "false").lower() in {
            "1", "true", "yes", "on"
        }
        summary_prompt_path = Path(os.getenv("GRAPH_DOCUMENT_SUMMARY_PROMPT_FILE", "prompts/graph_document_summary.txt"))
        if not summary_prompt_path.is_absolute():
            summary_prompt_path = BASE_DIR / summary_prompt_path
        try:
            self.summary_prompt = summary_prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            self.summary_prompt = DEFAULT_DOCUMENT_SUMMARY_PROMPT.strip()
        self.summary_signature = "document_summary_v1:" + hashlib.sha256(
            json.dumps(
                {
                    "model": self.summary_model,
                    "prompt": hashlib.sha256(self.summary_prompt.encode("utf-8")).hexdigest()[:12],
                    "max_chars": self.summary_max_chars,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]

        self.extractor_signature = self._build_extractor_signature(
            entity_discovery=self.discovery_enabled,
            relation_discovery=self.relation_enabled,
        )

    def _build_extractor_signature(
        self,
        *,
        entity_discovery: bool,
        relation_discovery: bool,
    ) -> str:
        signature_payload = {
            "extractor": EXTRACTOR_VERSION,
            "entity_discovery": bool(entity_discovery),
            "entity_model": self.discovery_model if entity_discovery else "",
            "entity_prompt": hashlib.sha256(self.discovery_prompt.encode("utf-8")).hexdigest()[:12] if entity_discovery else "",
            "entity_min_confidence": self.discovery_min_confidence if entity_discovery else None,
            "entity_chunk_chars": self.discovery_chunk_chars if entity_discovery else None,
            "entity_overlap_chars": self.discovery_overlap_chars if entity_discovery else None,
            "relations": bool(relation_discovery),
            "relation_model": self.relation_model if relation_discovery else "",
            "relation_prompt": hashlib.sha256(self.relation_prompt.encode("utf-8")).hexdigest()[:12] if relation_discovery else "",
            "relation_ontology": self.relation_ontology.get("hash") if relation_discovery else "",
            "relation_min_confidence": self.relation_min_confidence if relation_discovery else None,
            "relation_chunk_chars": self.relation_chunk_chars if relation_discovery else None,
            "relation_overlap_chars": self.relation_overlap_chars if relation_discovery else None,
        }
        return EXTRACTOR_VERSION + ":" + hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:12]

    def _fetch_documents(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not self.es_url or not self.es_index:
            raise RuntimeError("Elasticsearch-Konfiguration fehlt")
        endpoint = f"{self.es_url}/{self.es_index}/_mget"
        response = httpx.post(
            endpoint,
            json={
                "docs": [
                    {
                        "_id": document_id,
                        "_source": [
                            "title", "content", "hash", "attachment",
                            "source", "provider", "owner", "users", "groups", "circles"
                        ],
                    }
                    for document_id in ids
                ]
            },
            timeout=self.es_timeout,
            **self.es_httpx_options,
        )
        response.raise_for_status()
        data = response.json()
        out: dict[str, dict[str, Any]] = {}
        for doc in data.get("docs", []):
            document_id = str(doc.get("_id") or "")
            if not document_id or not doc.get("found"):
                continue
            source = doc.get("_source") or {}
            out[document_id] = source
        return out

    @staticmethod
    def _content_hash(source: dict[str, Any], fallback_text: str = "") -> str:
        content = str(source.get("content") or fallback_text or "")
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    def _llm_entity_extract(self, chunk: str, *, title: str = "") -> dict[str, Any]:
        evidence = serialize_evidence_records(
            [{"title": title or "(ohne Titel)", "text": str(chunk or "")}],
            kind="graph_entity_extract",
        )
        user = (
            "EVIDENCE_JSON:\n"
            + evidence
            + "\n\nExtrahiere nur die in diesem Evidence-Record ausdrücklich "
            "genannten Personen und Organisationen."
        )
        messages = [
            {"role": "system", "content": guarded_evidence_prompt(self.discovery_prompt)},
            {"role": "user", "content": user},
        ]

        def call_backend(*, num_predict: int, retry: bool = False) -> dict[str, Any]:
            request_messages = list(messages)
            if retry:
                request_messages.append({
                    "role": "user",
                    "content": (
                        "Die vorige Ausgabe war syntaktisch kein gueltiges JSON. "
                        "Fuehre die Extraktion fuer denselben Ausschnitt erneut aus. "
                        "Antworte ausschliesslich mit EINEM vollstaendigen JSON-Objekt, "
                        "das exakt dem vorgegebenen Schema entspricht. Keine Markdown-Fences, "
                        "keine Kommentare und kein Text vor oder nach dem JSON."
                    ),
                })
            return asyncio.run(
                self.discovery_backend.complete(
                    request_messages,
                    options={
                        "temperature": 0.0,
                        "num_predict": num_predict,
                        "num_ctx": self.discovery_num_ctx,
                    },
                    think=self.discovery_think,
                    response_format=ENTITY_DISCOVERY_SCHEMA,
                    timeout=self.discovery_timeout,
                )
            )

        first = call_backend(num_predict=self.discovery_num_predict)
        first_content = str(first.get("content") or "")
        try:
            return _strip_json_payload(first_content)
        except (json.JSONDecodeError, ValueError) as first_exc:
            retry_predict = max(self.discovery_num_predict + 512, self.discovery_num_predict * 2)
            log.warning(
                "Entity discovery returned invalid JSON; retrying once title=%r "
                "done_reason=%r chars=%s error=%s",
                title, first.get("done_reason"), len(first_content), first_exc,
            )
            second = call_backend(num_predict=retry_predict, retry=True)
            second_content = str(second.get("content") or "")
            try:
                return _strip_json_payload(second_content)
            except (json.JSONDecodeError, ValueError) as second_exc:
                raise StructuredOutputError(
                    "Entity extractor returned invalid JSON twice "
                    f"(first_done_reason={first.get('done_reason')!r}, "
                    f"retry_done_reason={second.get('done_reason')!r}, "
                    f"retry_chars={len(second_content)}): {second_exc}",
                    stage="entity",
                    first_done_reason=first.get("done_reason"),
                    retry_done_reason=second.get("done_reason"),
                    first_chars=len(first_content),
                    retry_chars=len(second_content),
                ) from second_exc

    def _validated_observations(self, payload: dict[str, Any], chunk: str) -> list[EntityObservation]:
        """Ground LLM observations in the exact chunk before any graph mutation."""
        out: list[EntityObservation] = []
        chunk_norm = normalize_name(chunk)
        for item in list(payload.get("entities") or [])[: self.discovery_max_entities_per_chunk]:
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("type") or "").strip()
            if entity_type not in {"Person", "Organization"}:
                continue
            canonical = str(item.get("canonical_name") or item.get("name") or "").strip(" \t\r\n,;:")
            observed = str(item.get("observed_text") or "").strip(" \t\r\n,;:")
            context_text = str(item.get("context_text") or "").strip()
            mention_context = str(item.get("mention_context") or "incidental").strip().lower()
            if mention_context not in {"actor", "identity_metadata", "quoted", "incidental"}:
                mention_context = "incidental"
            entity_kind = str(item.get("entity_kind") or "").strip()
            allowed_kinds = {"Person", "Company", "Court", "Authority", "LawFirm", "Bank", "Association", "Other"}
            if entity_kind not in allowed_kinds:
                entity_kind = "Person" if entity_type == "Person" else _infer_entity_kind(canonical or observed, [entity_type])
            if entity_type == "Person":
                entity_kind = "Person"
            elif entity_kind == "Person":
                entity_kind = _infer_entity_kind(canonical or observed, ["Organization"])

            raw_relevant_actor = item.get("relevant_actor", mention_context == "actor")
            if isinstance(raw_relevant_actor, bool):
                relevant_actor = raw_relevant_actor
            elif isinstance(raw_relevant_actor, str):
                relevant_actor = raw_relevant_actor.strip().casefold() in {"1", "true", "yes", "ja"}
            else:
                relevant_actor = bool(raw_relevant_actor)
            # Keep the legacy boolean deterministic: only actor means actor.
            relevant_actor = bool(mention_context == "actor")
            try:
                confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                continue
            if confidence < self.discovery_min_confidence:
                continue

            if entity_type == "Person":
                canonical = _canonical_person_name(canonical or observed)
            canonical_norm = normalize_name(canonical)
            observed_norm = normalize_name(observed)
            context_norm = normalize_name(context_text)
            if len(canonical_norm) < 3 or len(observed_norm) < 3:
                continue
            if observed_norm not in chunk_norm:
                continue
            # context_text is a second grounding anchor: it must be copied from
            # the same chunk and contain the actual observation.
            if not context_norm or context_norm not in chunk_norm or observed_norm not in context_norm:
                continue
            if len(context_text) > self.discovery_context_max_chars:
                context_text = context_text[: self.discovery_context_max_chars].rstrip()
                context_norm = normalize_name(context_text)
                if observed_norm not in context_norm:
                    # Do not accidentally truncate away the grounded name.
                    context_text = observed[: self.discovery_context_max_chars]

            # Canonicalization may remove titles but must not invent a different
            # person/organization name.
            if not (
                canonical_norm in observed_norm
                or observed_norm in canonical_norm
                or fuzz.ratio(canonical_norm, observed_norm) >= 88.0
            ):
                continue
            if entity_type == "Person" and len(canonical_norm.split()) < 2:
                continue

            if entity_type == "Organization" and entity_kind == "Other":
                inferred_kind = _infer_entity_kind(canonical, ["Organization"])
                if inferred_kind != "Other":
                    entity_kind = inferred_kind

            out.append(EntityObservation(
                canonical_name=canonical,
                entity_type=entity_type,
                entity_kind=entity_kind,
                mention_context=mention_context,
                observed_text=observed,
                context_text=context_text,
                confidence=max(0.0, min(1.0, confidence)),
                relevant_actor=relevant_actor,
            ))
        return out

    def _admission_decision(self, obs: EntityObservation) -> dict[str, Any]:
        """Decide whether an observation may resolve/create an identity.

        Hard garbage/incidental observations are retained as observations but do
        not participate in identity resolution. Plausible but weak candidates may
        resolve an already-known exact identity while being forbidden from
        creating a new one.
        """
        canonical = obs.canonical_name
        canonical_norm = normalize_name(canonical or obs.observed_text)
        if canonical_norm in self.blocked_generic_names:
            return {
                "eligible": False,
                "allow_create": False,
                "reason": "blocked_generic_name",
            }
        reason = _hard_rejection_reason(canonical, obs.observed_text)
        if reason:
            return {"eligible": False, "allow_create": False, "reason": reason}

        if obs.mention_context in {"quoted", "incidental"}:
            return {
                "eligible": False,
                "allow_create": False,
                "reason": f"mention_context_{obs.mention_context}",
            }
        if obs.mention_context == "identity_metadata":
            # Metadata may resolve known identities. New provisional identities
            # need a stronger signal than ordinary actors: a full named person
            # with an explicit role cue, or a multi-token/legal-form organization
            # with an institutional cue. This admits e.g. a named board member or
            # "Amtsgericht Frankfurt am Main", while a one-token "Postbank"
            # footer observation remains unresolved until corroborated elsewhere.
            meta_norm = normalize_name(canonical)
            meta_tokens = meta_norm.split()
            if obs.entity_type == "Person":
                has_role_cue = bool(_PERSON_CONTEXT_CUE_RE.search(obs.context_text))
                allow_meta = (
                    len(meta_tokens) >= 2
                    and has_role_cue
                    and obs.confidence >= max(self.discovery_person_auto_create_confidence, 0.95)
                )
                return {
                    "eligible": True,
                    "allow_create": bool(allow_meta),
                    "reason": "identity_metadata_strong_person_role" if allow_meta else "identity_metadata_exact_resolution_only",
                }
            strong_org = bool(_LEGAL_FORM_RE.search(canonical) or _ORG_CUE_RE.search(canonical))
            allow_meta = (
                strong_org
                and (len(meta_tokens) >= 2 or bool(_LEGAL_FORM_RE.search(canonical)))
                and obs.confidence >= max(self.discovery_organization_auto_create_confidence, 0.94)
            )
            return {
                "eligible": True,
                "allow_create": bool(allow_meta),
                "reason": "identity_metadata_strong_organization" if allow_meta else "identity_metadata_exact_resolution_only",
            }
        if self.discovery_require_relevant_actor and not obs.relevant_actor:
            return {"eligible": False, "allow_create": False, "reason": "incidental_or_not_document_actor"}

        norm = normalize_name(canonical)
        tokens = norm.split()
        if obs.entity_type == "Person":
            if len(tokens) < 2 or len(tokens) > 7:
                return {"eligible": False, "allow_create": False, "reason": "implausible_person_name_shape"}
            has_context_cue = bool(_PERSON_CONTEXT_CUE_RE.search(obs.context_text) or _PERSON_CONTEXT_CUE_RE.search(obs.observed_text))
            allow_create = obs.confidence >= self.discovery_person_auto_create_confidence
            if self.discovery_person_require_context_cue and not has_context_cue:
                allow_create = False
                return {
                    "eligible": True,
                    "allow_create": False,
                    "reason": "person_needs_stronger_identity_context",
                }
            return {
                "eligible": True,
                "allow_create": bool(allow_create),
                "reason": "person_strong_context" if allow_create else "person_below_auto_create_confidence",
            }

        # Organizations are safer to create when a legal form or institutional
        # cue is present. Otherwise require a high-confidence, multi-token actor.
        strong_shape = bool(_LEGAL_FORM_RE.search(canonical) or _ORG_CUE_RE.search(canonical))
        if len(tokens) < 1 or len(tokens) > 14:
            return {"eligible": False, "allow_create": False, "reason": "implausible_organization_name_shape"}
        threshold = self.discovery_organization_auto_create_confidence
        if strong_shape and obs.confidence >= max(self.discovery_min_confidence, threshold - 0.08):
            return {"eligible": True, "allow_create": True, "reason": "organization_strong_name_shape"}
        if len(tokens) >= 2 and obs.confidence >= threshold:
            return {"eligible": True, "allow_create": True, "reason": "organization_relevant_high_confidence"}
        return {
            "eligible": True,
            "allow_create": False,
            "reason": "organization_needs_stronger_identity_signal",
        }

    def discover_entities(self, text: str, *, title: str = "") -> tuple[list[EntityObservation], list[dict[str, Any]]]:
        if not self.discovery_enabled or not text.strip():
            return [], []
        chunks = _chunk_text(text, self.discovery_chunk_chars, self.discovery_overlap_chars)
        if self.discovery_max_chunks > 0:
            chunks = chunks[: self.discovery_max_chunks]

        merged: dict[tuple[str, str], EntityObservation] = {}
        errors: list[dict[str, Any]] = []

        def merge_observations(observations: list[EntityObservation]) -> None:
            for obs in observations:
                key = (obs.entity_type, normalize_name(obs.canonical_name))
                old = merged.get(key)
                if old is None:
                    merged[key] = obs
                    continue
                old.count += 1
                if obs.confidence > old.confidence:
                    old.confidence = obs.confidence
                    old.observed_text = obs.observed_text
                    old.context_text = obs.context_text
                    old.entity_kind = obs.entity_kind
                    old.mention_context = obs.mention_context
                context_rank = {"incidental": 0, "quoted": 1, "identity_metadata": 2, "actor": 3}
                if context_rank.get(obs.mention_context, 0) > context_rank.get(old.mention_context, 0):
                    old.mention_context = obs.mention_context
                old.relevant_actor = old.mention_context == "actor"

        def split_for_retry(chunk: str) -> list[str]:
            target = max(self.discovery_adaptive_split_min_chars, len(chunk) // 2)
            overlap = min(self.discovery_overlap_chars, max(0, target // 4))
            parts = _chunk_text(chunk, target, overlap)
            return [part for part in parts if part.strip() and part != chunk]

        def process_chunk(chunk: str, *, root_index: int, depth: int, label: str) -> bool:
            try:
                payload = self._llm_entity_extract(chunk, title=title)
                observations = self._validated_observations(payload, chunk)
            except StructuredOutputError as exc:
                if depth < self.discovery_adaptive_split_max_depth and len(chunk) > self.discovery_adaptive_split_min_chars:
                    parts = split_for_retry(chunk)
                    if len(parts) >= 2:
                        log.warning(
                            "Entity discovery invalid after retry title=%r chunk=%s chars=%s "
                            "done_reason=%r retry_done_reason=%r action=adaptive_split parts=%s depth=%s",
                            title, label, len(chunk), exc.first_done_reason, exc.retry_done_reason,
                            len(parts), depth,
                        )
                        any_success = False
                        for sub_index, part in enumerate(parts, start=1):
                            any_success = process_chunk(
                                part, root_index=root_index, depth=depth + 1,
                                label=f"{label}.{sub_index}",
                            ) or any_success
                        return any_success
                errors.append({
                    "chunk": label,
                    "error": f"{type(exc).__name__}: {exc}",
                    "done_reason": exc.retry_done_reason or exc.first_done_reason,
                })
                log.warning("Entity discovery failed title=%r chunk=%s: %s", title, label, exc)
                return False
            except Exception as exc:
                errors.append({"chunk": label, "error": f"{type(exc).__name__}: {exc}"})
                log.warning("Entity discovery failed title=%r chunk=%s: %s", title, label, exc)
                return False

            merge_observations(observations)
            return True

        successful_roots = 0
        for idx, chunk in enumerate(chunks, start=1):
            if process_chunk(chunk, root_index=idx, depth=0, label=str(idx)):
                successful_roots += 1

        if chunks and successful_roots == 0:
            raise RuntimeError(
                f"Entity discovery failed for all {len(chunks)} root chunk(s): "
                + "; ".join(str(item.get("error") or "") for item in errors[:3])
            )
        return list(merged.values()), errors

    def _llm_relation_extract(
        self,
        chunk: str,
        *,
        title: str,
        allowed_entities: list[dict[str, str]],
    ) -> dict[str, Any]:
        evidence = serialize_evidence_records(
            [
                {
                    "title": title or "(ohne Titel)",
                    "text": str(chunk or ""),
                    "allowed_entities": list(allowed_entities),
                }
            ],
            kind="graph_relation_extract",
        )
        user = (
            f"VERTRAUENSWÜRDIGE RELATIONS-ONTOLOGIE:\n{self.relation_ontology_prompt}\n\n"
            "EVIDENCE_JSON:\n"
            + evidence
            + "\n\nVerwende nur entity_id-Werte aus allowed_entities. "
            "Extrahiere nur explizit durch relation_text und evidence_text belegte Beziehungen. "
            "Wenn keine Ontologie-Relation explizit passt, liefere eine leere relations-Liste."
        )
        messages = [
            {"role": "system", "content": guarded_evidence_prompt(self.relation_prompt)},
            {"role": "user", "content": user},
        ]

        def call_backend(*, num_predict: int, retry: bool = False) -> dict[str, Any]:
            retry_messages = list(messages)
            if retry:
                retry_messages.append({
                    "role": "user",
                    "content": (
                        "Die vorige Ausgabe war syntaktisch kein gueltiges JSON. "
                        "Fuehre die Extraktion fuer denselben Ausschnitt erneut aus. "
                        "Antworte ausschliesslich mit EINEM vollstaendigen JSON-Objekt, "
                        "das exakt dem vorgegebenen Schema entspricht. Keine Markdown-Fences, "
                        "keine Kommentare und kein Text vor oder nach dem JSON."
                    ),
                })
            return asyncio.run(
                self.relation_backend.complete(
                    retry_messages,
                    options={
                        "temperature": 0.0,
                        "num_predict": num_predict,
                        "num_ctx": self.relation_num_ctx,
                    },
                    think=self.relation_think,
                    response_format=self.relation_schema,
                    timeout=self.relation_timeout,
                )
            )

        first = call_backend(num_predict=self.relation_num_predict)
        try:
            return _strip_json_payload(str(first.get("content") or ""))
        except (json.JSONDecodeError, ValueError) as first_exc:
            # Structured-output support is not perfectly reliable across all
            # Ollama/model combinations. Do not repair malformed JSON locally:
            # a guessed comma/quote could silently change evidence. Retry the
            # same grounded extraction once and give it more output budget.
            retry_predict = max(self.relation_num_predict + 512, self.relation_num_predict * 2)
            log.warning(
                "Relation discovery returned invalid JSON; retrying once "
                "done_reason=%r chars=%s error=%s",
                first.get("done_reason"),
                len(str(first.get("content") or "")),
                first_exc,
            )
            second = call_backend(num_predict=retry_predict, retry=True)
            try:
                return _strip_json_payload(str(second.get("content") or ""))
            except (json.JSONDecodeError, ValueError) as second_exc:
                second_content = str(second.get("content") or "")
                raise StructuredOutputError(
                    "Relation extractor returned invalid JSON twice "
                    f"(first_done_reason={first.get('done_reason')!r}, "
                    f"retry_done_reason={second.get('done_reason')!r}, "
                    f"retry_chars={len(second_content)}): {second_exc}",
                    stage="relation",
                    first_done_reason=first.get("done_reason"),
                    retry_done_reason=second.get("done_reason"),
                    first_chars=len(str(first.get("content") or "")),
                    retry_chars=len(second_content),
                ) from second_exc

    def _llm_document_summary(
        self,
        text: str,
        *,
        title: str,
        known_entities: list[dict[str, Any]],
        known_relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        full_text = str(text or "")
        if len(full_text) <= self.summary_max_chars:
            excerpt = full_text
            text_truncated = False
        else:
            # One cheap summary call is intentionally not a full map/reduce pass.
            # Sample beginning/middle/end so long contracts or appendices do not
            # collapse to a pure first-page summary.
            head_n = int(self.summary_max_chars * 0.45)
            middle_n = int(self.summary_max_chars * 0.25)
            tail_n = self.summary_max_chars - head_n - middle_n
            middle_start = max(0, (len(full_text) - middle_n) // 2)
            excerpt = (
                "[ANFANG DES DOKUMENTS]\n" + full_text[:head_n]
                + "\n\n[MITTLERER AUSSCHNITT]\n" + full_text[middle_start:middle_start + middle_n]
                + "\n\n[ENDE DES DOKUMENTS]\n" + full_text[-tail_n:]
            )
            text_truncated = True
        evidence = serialize_evidence_records(
            [
                {
                    "title": title or "(ohne Titel)",
                    "text": excerpt,
                    "known_entities": known_entities[:40],
                    "known_relations": known_relations[:30],
                    "text_truncated": text_truncated,
                }
            ],
            kind="graph_document_summary",
        )
        user = (
            "EVIDENCE_JSON:\n"
            + evidence
            + "\n\nFasse ausschließlich die im Evidence-Record belegten Inhalte "
            "gemäß dem vorgegebenen Schema zusammen."
        )
        messages = [
            {"role": "system", "content": guarded_evidence_prompt(self.summary_prompt)},
            {"role": "user", "content": user},
        ]

        def call(*, num_predict: int, retry: bool = False) -> dict[str, Any]:
            request_messages = list(messages)
            if retry:
                request_messages.append({
                    "role": "user",
                    "content": (
                        "Die vorige Ausgabe war kein gueltiges JSON. Wiederhole dieselbe "
                        "Dokumentanalyse und liefere ausschliesslich ein vollstaendiges "
                        "JSON-Objekt gemaess Schema."
                    ),
                })
            return asyncio.run(
                self.relation_backend.complete(
                    request_messages,
                    options={
                        "temperature": 0.0,
                        "num_predict": num_predict,
                        "num_ctx": self.summary_num_ctx,
                    },
                    think=self.summary_think,
                    model=self.summary_model,
                    response_format=DOCUMENT_SUMMARY_SCHEMA,
                    timeout=self.summary_timeout,
                )
            )

        first = call(num_predict=self.summary_num_predict)
        try:
            return _strip_json_payload(str(first.get("content") or ""))
        except (json.JSONDecodeError, ValueError):
            second = call(num_predict=max(self.summary_num_predict * 2, self.summary_num_predict + 400), retry=True)
            return _strip_json_payload(str(second.get("content") or ""))

    @staticmethod
    def _validated_document_summary(payload: dict[str, Any]) -> dict[str, Any]:
        def clean_list(value: Any, *, limit: int) -> list[str]:
            out: list[str] = []
            for item in list(value or [])[:limit]:
                text = re.sub(r"\s+", " ", str(item or "")).strip()
                if text and text not in out:
                    out.append(text[:1000])
            return out

        actors: list[dict[str, str]] = []
        for item in list(payload.get("relevant_actors") or [])[:32]:
            if not isinstance(item, dict):
                continue
            name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
            role = re.sub(r"\s+", " ", str(item.get("role") or "")).strip()
            actor_type = str(item.get("type") or "Other").strip()
            if actor_type not in {"Person", "Organization", "Other"}:
                actor_type = "Other"
            if name:
                actors.append({"name": name[:300], "type": actor_type, "role": role[:500]})

        return {
            "document_type": re.sub(r"\s+", " ", str(payload.get("document_type") or "")).strip()[:160],
            "summary": str(payload.get("summary") or "").strip()[:6000],
            "key_points": clean_list(payload.get("key_points"), limit=16),
            "relevant_actors": actors,
            "uncertainties": clean_list(payload.get("uncertainties"), limit=12),
        }

    @staticmethod
    def _ontology_base_type(entity_type: str) -> str:
        value = str(entity_type or "").strip().upper()
        if value == "PERSON":
            return "Person"
        if value in {"ORGANIZATION", "ORGANIZATIONAL_UNIT"}:
            return "Organization"
        return "Entity"

    def _validated_relations(
        self,
        payload: dict[str, Any],
        chunk: str,
        *,
        allowed_entities: dict[str, dict[str, Any]],
        chunk_index: int,
    ) -> tuple[list[RelationObservation], list[dict[str, Any]]]:
        out: list[RelationObservation] = []
        rejected: list[dict[str, Any]] = []
        chunk_norm = normalize_name(chunk)
        allowed_stances = {"asserted", "negated", "alleged", "questioned", "conditional", "unknown"}
        ontology_predicates = self.relation_ontology.get("predicates") or {}

        def reject(item: dict[str, Any], reason: str) -> None:
            rejected.append({
                "kind": "rejected_relation",
                "chunk": chunk_index,
                "reason": reason,
                "subject_entity_id": str(item.get("subject_entity_id") or ""),
                "predicate": str(item.get("predicate") or ""),
                "object_entity_id": str(item.get("object_entity_id") or ""),
                "relation_text": str(item.get("relation_text") or "")[:300],
            })

        for item in list(payload.get("relations") or [])[: self.relation_max_per_chunk]:
            if not isinstance(item, dict):
                continue
            subject_id = str(item.get("subject_entity_id") or "").strip()
            object_id = str(item.get("object_entity_id") or "").strip()
            if subject_id not in allowed_entities or object_id not in allowed_entities:
                reject(item, "unknown_entity_id")
                continue
            if not subject_id or subject_id == object_id:
                reject(item, "self_or_empty_relation")
                continue

            predicate_text = str(item.get("predicate_text") or item.get("predicate") or "").strip()
            predicate = _normalize_predicate(str(item.get("predicate") or ""))
            spec = ontology_predicates.get(predicate)

            subject_meta = allowed_entities[subject_id]
            object_meta = allowed_entities[object_id]
            subject_type = self._ontology_base_type(str(subject_meta.get("entity_type") or ""))
            object_type = self._ontology_base_type(str(object_meta.get("entity_type") or ""))

            evidence_text = str(item.get("evidence_text") or "").strip()
            relation_text = str(item.get("relation_text") or "").strip()
            semantic_rejection = validate_relation_semantics(
                self.relation_ontology,
                predicate=predicate,
                subject_type=subject_type,
                subject_kind=str(subject_meta.get("entity_kind") or "Other"),
                object_type=object_type,
                object_kind=str(object_meta.get("entity_kind") or "Other"),
                relation_text=relation_text,
                evidence_text=evidence_text,
            )
            if semantic_rejection:
                reject(item, semantic_rejection)
                continue
            evidence_norm = normalize_name(evidence_text)
            relation_norm = normalize_name(relation_text)
            if len(evidence_norm) < 8 or len(relation_norm) < 3:
                reject(item, "missing_grounded_relation_or_evidence_text")
                continue
            if evidence_norm not in chunk_norm:
                reject(item, "evidence_not_in_source_chunk")
                continue
            if relation_norm not in evidence_norm or relation_norm not in chunk_norm:
                reject(item, "relation_text_not_grounded_in_evidence")
                continue

            subject_forms = set(subject_meta.get("forms") or set())
            object_forms = set(object_meta.get("forms") or set())
            if not any(form and form in evidence_norm for form in subject_forms):
                reject(item, "subject_not_in_evidence")
                continue
            if not any(form and form in evidence_norm for form in object_forms):
                reject(item, "object_not_in_evidence")
                continue
            if len(evidence_text) > self.relation_evidence_max_chars:
                reject(item, "evidence_too_long")
                continue

            try:
                confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                reject(item, "invalid_confidence")
                continue
            if confidence < self.relation_min_confidence:
                reject(item, "below_relation_confidence")
                continue
            stance = str(item.get("stance") or "unknown").strip().lower()
            if stance not in allowed_stances:
                stance = "unknown"

            out.append(RelationObservation(
                subject_entity_id=subject_id,
                predicate=predicate,
                predicate_text=predicate_text or predicate,
                relation_text=relation_text,
                object_entity_id=object_id,
                evidence_text=evidence_text,
                confidence=max(0.0, min(1.0, confidence)),
                stance=stance,
                chunk_index=chunk_index,
                valid_from=str(item.get("valid_from") or "").strip(),
                valid_to=str(item.get("valid_to") or "").strip(),
            ))
        return out, rejected

    def discover_relations(
        self,
        text: str,
        forms: list[dict[str, Any]],
        *,
        title: str = "",
    ) -> tuple[list[RelationObservation], list[dict[str, Any]]]:
        """Extract ontology-constrained grounded relations between exact entities."""
        if not self.relation_enabled or not text.strip():
            return [], []
        chunks = _chunk_text(text, self.relation_chunk_chars, self.relation_overlap_chars)
        if self.relation_max_chunks > 0:
            chunks = chunks[: self.relation_max_chunks]

        merged: dict[tuple[str, str, str, str], RelationObservation] = {}
        diagnostics: list[dict[str, Any]] = []

        def split_for_retry(chunk: str) -> list[str]:
            target = max(self.relation_adaptive_split_min_chars, len(chunk) // 2)
            overlap = min(self.relation_overlap_chars, max(0, target // 4))
            parts = _chunk_text(chunk, target, overlap)
            return [part for part in parts if part.strip() and part != chunk]

        def process_chunk(chunk: str, *, root_index: int, depth: int, label: str) -> None:
            chunk_mentions = detect_document_mentions(
                chunk,
                forms,
                fuzzy=False,
                fuzzy_threshold=self.fuzzy_threshold,
                fuzzy_max_candidates=self.fuzzy_max_candidates,
            )
            allowed: dict[str, dict[str, Any]] = {}
            for mention in chunk_mentions:
                if mention.status != "resolved_exact" or len(mention.candidates) != 1:
                    continue
                candidate = mention.candidates[0]
                entry = allowed.setdefault(candidate.entity_id, {
                    "entity_id": candidate.entity_id,
                    "display_name": candidate.display_name,
                    "entity_type": candidate.entity_type,
                    "entity_kind": candidate.entity_kind,
                    "forms": set(),
                })
                observed_norm = normalize_name(mention.observed_value)
                if observed_norm:
                    entry["forms"].add(observed_norm)
            if len(allowed) < 2:
                return

            try:
                payload = self._llm_relation_extract(
                    chunk,
                    title=title,
                    allowed_entities=[
                        {
                            "entity_id": str(v["entity_id"]),
                            "display_name": str(v.get("display_name") or ""),
                            "entity_type": self._ontology_base_type(str(v.get("entity_type") or "")),
                            "entity_kind": str(v.get("entity_kind") or "Other"),
                        }
                        for v in allowed.values()
                    ],
                )
                observations, rejections = self._validated_relations(
                    payload,
                    chunk,
                    allowed_entities=allowed,
                    chunk_index=root_index,
                )
                diagnostics.extend(rejections)
            except StructuredOutputError as exc:
                if depth < self.relation_adaptive_split_max_depth and len(chunk) > self.relation_adaptive_split_min_chars:
                    parts = split_for_retry(chunk)
                    if len(parts) >= 2:
                        log.warning(
                            "Relation discovery invalid after retry title=%r chunk=%s chars=%s "
                            "done_reason=%r retry_done_reason=%r action=adaptive_split parts=%s depth=%s",
                            title, label, len(chunk), exc.first_done_reason, exc.retry_done_reason,
                            len(parts), depth,
                        )
                        diagnostics.append({
                            "kind": "adaptive_split", "chunk": label, "parts": len(parts),
                            "done_reason": exc.retry_done_reason or exc.first_done_reason,
                        })
                        for sub_index, part in enumerate(parts, start=1):
                            process_chunk(
                                part, root_index=root_index, depth=depth + 1,
                                label=f"{label}.{sub_index}",
                            )
                        return
                diagnostics.append({
                    "kind": "extractor_error", "chunk": label,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning("Relation discovery failed title=%r chunk=%s: %s", title, label, exc)
                return
            except Exception as exc:
                diagnostics.append({
                    "kind": "extractor_error", "chunk": label,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning("Relation discovery failed title=%r chunk=%s: %s", title, label, exc)
                return

            for obs in observations:
                key = (
                    obs.subject_entity_id,
                    obs.predicate,
                    obs.object_entity_id,
                    normalize_name(obs.evidence_text),
                )
                old = merged.get(key)
                if old is None or obs.confidence > old.confidence:
                    merged[key] = obs

        for idx, chunk in enumerate(chunks, start=1):
            process_chunk(chunk, root_index=idx, depth=0, label=str(idx))
        return list(merged.values()), diagnostics

    def index_evidence(
        self,
        documents: list[dict[str, Any]],
        *,
        query_id: str = "",
        user_query: str = "",
        retrieval_query: str = "",
        evidence_action: str = "answer",
        force_reindex: bool = False,
        entity_discovery: bool | None = None,
        relation_discovery: bool | None = None,
        count_evidence: bool = True,
        force_oversize: bool = False,
        _state_signature_override: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "documents": [], "errors": []}

        dedup: dict[str, dict[str, Any]] = {}
        for item in documents[: self.max_documents]:
            document_id = str(item.get("document_id") or "").strip()
            if document_id:
                dedup.setdefault(document_id, dict(item))
        ids = list(dedup)
        if not ids:
            return {"enabled": True, "documents": [], "errors": []}

        try:
            fetched = self._fetch_documents(ids)
        except Exception as exc:
            fetched = {}
            fetch_error = f"{type(exc).__name__}: {exc}"
        else:
            fetch_error = ""

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        do_discovery = self.discovery_enabled if entity_discovery is None else bool(entity_discovery)
        do_relations = self.relation_enabled if relation_discovery is None else bool(relation_discovery)
        extractor_signature = _state_signature_override or self._build_extractor_signature(
            entity_discovery=do_discovery,
            relation_discovery=do_relations,
        )

        with GraphStore.from_config(self.cfg) as graph:
            graph.verify_connectivity()
            graph.ensure_schema()

            for document_id in ids:
                supplied = dedup[document_id]
                source = fetched.get(document_id, {})
                title = str(source.get("title") or supplied.get("title") or "")
                raw_text = str(source.get("content") or supplied.get("context_text") or supplied.get("text") or "")
                attachment = source.get("attachment") or {}
                raw_file_bytes = (
                    supplied.get("file_bytes")
                    or attachment.get("content_length")
                    or attachment.get("size")
                    or source.get("size")
                    or 0
                )
                try:
                    file_bytes = int(raw_file_bytes or 0)
                except (TypeError, ValueError):
                    file_bytes = 0
                entity_chunks = len(_chunk_text(raw_text, self.discovery_chunk_chars, self.discovery_overlap_chars)) if raw_text else 0
                relation_chunks = len(_chunk_text(raw_text, self.relation_chunk_chars, self.relation_overlap_chars)) if raw_text else 0
                estimated_chunks = max(entity_chunks, relation_chunks)
                oversize_reasons: list[str] = []
                if self.auto_max_text_chars > 0 and len(raw_text) > self.auto_max_text_chars:
                    oversize_reasons.append(f"text_chars={len(raw_text)}>{self.auto_max_text_chars}")
                if self.auto_max_chunks > 0 and estimated_chunks > self.auto_max_chunks:
                    oversize_reasons.append(f"graph_chunks={estimated_chunks}>{self.auto_max_chunks}")
                if self.auto_max_file_bytes > 0 and file_bytes > self.auto_max_file_bytes:
                    oversize_reasons.append(f"file_bytes={file_bytes}>{self.auto_max_file_bytes}")
                if oversize_reasons and not force_oversize:
                    skipped = {
                        "document_id": document_id,
                        "title": title,
                        "status": "skipped_oversize",
                        "oversize": {
                            "reasons": oversize_reasons,
                            "text_chars": len(raw_text),
                            "graph_chunks": estimated_chunks,
                            "file_bytes": file_bytes or None,
                        },
                        "discovered_entities": [],
                        "entity_resolutions": [],
                        "relations": [],
                    }
                    results.append(skipped)
                    log.warning(
                        "SKIPPED_OVERSIZE document=%s title=%r reasons=%s",
                        document_id, title, ",".join(oversize_reasons),
                    )
                    continue
                text = raw_text[: self.max_content_chars] if len(raw_text) > self.max_content_chars else raw_text
                document_date = str(attachment.get("date") or supplied.get("document_date") or "")
                inferred_source_date = infer_source_date(title=title, text=text)
                content_hash = self._content_hash(source, text)

                try:
                    mail_metadata: dict[str, Any] | None = None
                    mail_sidecar_path = ""
                    mail_role = ""
                    mail_metadata_error = ""
                    state = graph.touch_evidence_document(
                        document_id=document_id,
                        title=title,
                        path=str(supplied.get("path") or title),
                        source_url=str(supplied.get("source_url") or ""),
                        document_date=document_date,
                        source_date=inferred_source_date.value,
                        source_date_precision=inferred_source_date.precision,
                        source_date_confidence=inferred_source_date.confidence,
                        source_date_basis=inferred_source_date.basis,
                        source_date_evidence=inferred_source_date.evidence,
                        content_hash=content_hash,
                        extractor=extractor_signature,
                        query_id=query_id,
                        user_query=user_query,
                        retrieval_query=retrieval_query,
                        evidence_action=evidence_action,
                        count_evidence=count_evidence,
                    )

                    # Protocol metadata is attached independently of LLM
                    # extraction and therefore also on a cached graph document.
                    try:
                        sidecar = self.mail_metadata_reader.fetch_for_title(
                            title, rag_user_id=str(supplied.get("rag_user_id") or "") or None
                        )
                        if sidecar is not None:
                            candidate_meta, candidate_path = sidecar
                            candidate_role = representation_role(candidate_meta, title)
                            # A sidecar can share a directory with unrelated
                            # files. Only files explicitly named in its manifest
                            # are linked to the MailMessage.
                            if candidate_role:
                                headers = candidate_meta.get("headers") or {}
                                key = message_key(str(headers.get("message_id") or ""), candidate_path)
                                graph.attach_mail_metadata(
                                    document_id=document_id,
                                    mail_key=key,
                                    sidecar_path=candidate_path,
                                    role=candidate_role,
                                    metadata=candidate_meta,
                                    reply_parent_message_id=reply_parent_id(candidate_meta),
                                )
                                mail_metadata = candidate_meta
                                mail_sidecar_path = candidate_path
                                mail_role = candidate_role
                    except Exception as exc:
                        mail_metadata_error = f"{type(exc).__name__}: {exc}"
                        log.warning("Mail metadata attach failed document=%s title=%r: %s", document_id, title, exc)

                    needs_reindex = bool(state.get("needs_reindex")) or bool(force_reindex)
                    mentions: list[DetectedMention] = []
                    observations: list[EntityObservation] = []
                    observation_resolutions: list[dict[str, Any]] = []
                    discovery_errors: list[dict[str, Any]] = []
                    relation_observations: list[RelationObservation] = []
                    relation_errors: list[dict[str, Any]] = []
                    document_analysis: dict[str, Any] | None = None
                    document_analysis_error = ""

                    if needs_reindex and text:
                        if do_discovery:
                            observations, discovery_errors = self.discover_entities(text, title=title)
                            for obs in observations:
                                admission = self._admission_decision(obs)
                                resolution = graph.record_document_entity_observation(
                                    document_id=document_id,
                                    entity_type=obs.entity_type,
                                    canonical_name=obs.canonical_name,
                                    observed_text=obs.observed_text,
                                    context_text=obs.context_text,
                                    confidence=obs.confidence,
                                    relevant_actor=obs.relevant_actor,
                                    mention_context=obs.mention_context,
                                    entity_kind=obs.entity_kind,
                                    extractor=extractor_signature,
                                    eligible=bool(admission["eligible"]),
                                    allow_create=bool(admission["allow_create"]),
                                    admission_reason=str(admission["reason"]),
                                )
                                resolution["confidence"] = round(obs.confidence, 4)
                                resolution["observed_text"] = obs.observed_text
                                resolution["context_text"] = obs.context_text
                                resolution["relevant_actor"] = obs.relevant_actor
                                resolution["mention_context"] = obs.mention_context
                                resolution["entity_kind"] = obs.entity_kind
                                resolution["observation_count"] = obs.count
                                observation_resolutions.append(resolution)

                        # Refresh AFTER discovery so names introduced by this very
                        # document are immediately linkable in the same pass.
                        forms = graph.name_forms(include_inactive_names=True, purpose="ingestion")
                        mentions = detect_document_mentions(
                            text,
                            forms,
                            fuzzy=self.fuzzy,
                            fuzzy_threshold=self.fuzzy_threshold,
                            fuzzy_max_candidates=self.fuzzy_max_candidates,
                        )
                        graph.replace_document_mentions(
                            document_id=document_id,
                            mentions=[
                                {
                                    "observed_value": m.observed_value,
                                    "normalized": m.normalized,
                                    "status": m.status,
                                    "count": m.count,
                                    "candidates": [vars(c) for c in m.candidates],
                                }
                                for m in mentions
                            ],
                            content_hash=content_hash,
                            extractor=extractor_signature,
                        )

                        if do_relations:
                            relation_observations, relation_errors = self.discover_relations(
                                text, forms, title=title
                            )
                            relation_payloads: list[dict[str, Any]] = []
                            for relation in relation_observations:
                                relation_id = hashlib.sha256(
                                    (
                                        f"{document_id}\0{relation.subject_entity_id}\0{relation.predicate}\0"
                                        f"{relation.object_entity_id}\0{normalize_name(relation.evidence_text)}"
                                    ).encode("utf-8", errors="replace")
                                ).hexdigest()[:40]
                                relation_payloads.append({
                                    "relation_id": relation_id,
                                    "subject_entity_id": relation.subject_entity_id,
                                    "predicate": relation.predicate,
                                    "predicate_text": relation.predicate_text,
                                    "relation_text": relation.relation_text,
                                    "object_entity_id": relation.object_entity_id,
                                    "evidence_text": relation.evidence_text,
                                    "confidence": relation.confidence,
                                    "stance": relation.stance,
                                    "chunk_index": relation.chunk_index,
                                    "valid_from": relation.valid_from,
                                    "valid_to": relation.valid_to,
                                })
                            graph.replace_document_relation_observations(
                                document_id=document_id,
                                observations=relation_payloads,
                                extractor=extractor_signature,
                            )

                    needs_summary = bool(
                        self.summary_enabled
                        and text
                        and (
                            force_reindex
                            or str(state.get("analysis_hash") or "") != content_hash
                            or str(state.get("analysis_extractor") or "") != self.summary_signature
                        )
                    )
                    if needs_summary:
                        try:
                            doc_struct = graph.document_summary(document_id)
                            known_entities = [
                                {
                                    "name": str(m.get("display_name") or ""),
                                    "entity_id": str(m.get("entity_id") or ""),
                                    "labels": list(m.get("labels") or []),
                                }
                                for m in list(doc_struct.get("mentions") or [])
                                if str(m.get("display_name") or "").strip()
                            ]
                            known_relations = [
                                {
                                    "subject": r.subject_entity_id,
                                    "predicate": r.predicate,
                                    "object": r.object_entity_id,
                                    "stance": r.stance,
                                }
                                for r in relation_observations
                            ]
                            raw_analysis = self._llm_document_summary(
                                text,
                                title=title,
                                known_entities=known_entities,
                                known_relations=known_relations,
                            )
                            document_analysis = self._validated_document_summary(raw_analysis)
                            graph.store_document_analysis(
                                document_id=document_id,
                                content_hash=content_hash,
                                extractor=self.summary_signature,
                                model=self.summary_model,
                                document_type=document_analysis["document_type"],
                                summary=document_analysis["summary"],
                                key_points=document_analysis["key_points"],
                                relevant_actors=document_analysis["relevant_actors"],
                                uncertainties=document_analysis["uncertainties"],
                            )
                        except Exception as exc:
                            document_analysis_error = f"{type(exc).__name__}: {exc}"
                            log.warning("Document summary failed title=%r: %s", title, exc)

                    results.append({
                        "document_id": document_id,
                        "title": title,
                        "reindexed": needs_reindex,
                        "entity_discovery": do_discovery,
                        "text_chars": len(text),
                        "discovered_entities": [
                            {
                                "canonical_name": o.canonical_name,
                                "type": o.entity_type,
                                "observed_text": o.observed_text,
                                "context_text": o.context_text,
                                "confidence": round(o.confidence, 4),
                                "relevant_actor": o.relevant_actor,
                                "mention_context": o.mention_context,
                                "entity_kind": o.entity_kind,
                                "count": o.count,
                            }
                            for o in observations
                        ] if needs_reindex else None,
                        "entity_resolutions": observation_resolutions if needs_reindex else None,
                        "discovery_errors": discovery_errors if needs_reindex else None,
                        "relation_discovery": do_relations,
                        "relations": [
                            {
                                "subject_entity_id": r.subject_entity_id,
                                "predicate": r.predicate,
                                "predicate_text": r.predicate_text,
                                "relation_text": r.relation_text,
                                "object_entity_id": r.object_entity_id,
                                "evidence_text": r.evidence_text,
                                "confidence": round(r.confidence, 4),
                                "stance": r.stance,
                                "chunk_index": r.chunk_index,
                                "valid_from": r.valid_from,
                                "valid_to": r.valid_to,
                            }
                            for r in relation_observations
                        ] if needs_reindex and do_relations else None,
                        "relation_errors": relation_errors if needs_reindex and do_relations else None,
                        "mentions": len(mentions) if needs_reindex else None,
                        "resolved_mentions": sum(1 for m in mentions if m.status == "resolved_exact") if needs_reindex else None,
                        "unresolved_mentions": sum(1 for m in mentions if m.status != "resolved_exact") if needs_reindex else None,
                        "source_date": inferred_source_date.to_dict(),
                        "mail_metadata": {
                            "sidecar_path": mail_sidecar_path,
                            "role": mail_role,
                            "message_id": str(((mail_metadata or {}).get("headers") or {}).get("message_id") or ""),
                            "reply_parent": reply_parent_id(mail_metadata or {}) if mail_metadata else "",
                        } if mail_metadata else None,
                        "mail_metadata_error": mail_metadata_error or None,
                        "document_analysis": document_analysis,
                        "document_analysis_error": document_analysis_error or None,
                        "document_analysis_cached": bool(
                            self.summary_enabled
                            and not document_analysis
                            and not document_analysis_error
                            and str(state.get("analysis_hash") or "") == content_hash
                            and str(state.get("analysis_extractor") or "") == self.summary_signature
                        ),
                        "technical_document_date": document_date,
                        "source": "elasticsearch" if document_id in fetched else "provider_context",
                    })
                except Exception as exc:
                    errors.append({
                        "document_id": document_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

        if fetch_error:
            errors.append({"document_id": "_fetch", "error": fetch_error})

        return {
            "enabled": True,
            "extractor": EXTRACTOR_VERSION,
            "extractor_signature": extractor_signature,
            "entity_discovery_model": self.discovery_model if do_discovery else None,
            "relation_discovery_model": self.relation_model if do_relations else None,
            "document_summary_model": self.summary_model if self.summary_enabled else None,
            "documents": results,
            "errors": errors,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Index evidence documents into Neo4j Graph v3 with conservative entity and relation discovery")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--document", action="append", default=[], help="Elasticsearch document_id; repeatable")
    parser.add_argument("--query", default="CLI graph index")
    parser.add_argument("--no-fuzzy", action="store_true")
    parser.add_argument("--no-discovery", action="store_true", help="skip LLM entity discovery; only relink against known entities")
    parser.add_argument("--no-relations", action="store_true", help="skip LLM relation/claim discovery")
    parser.add_argument("--force", action="store_true", help="reprocess even when hash/extractor is unchanged")
    args = parser.parse_args()

    if not args.document:
        parser.error("mindestens ein --document files:... ist erforderlich")

    cfg = load_config(args.config)
    if args.no_fuzzy:
        cfg.setdefault("graph_indexer", {})["fuzzy"] = False
    indexer = GraphEvidenceIndexer(cfg)
    result = indexer.index_evidence(
        [{"document_id": document_id} for document_id in args.document],
        query_id="cli",
        user_query=args.query,
        retrieval_query=args.query,
        evidence_action="manual",
        force_reindex=args.force,
        entity_discovery=not args.no_discovery,
        relation_discovery=not args.no_relations,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
