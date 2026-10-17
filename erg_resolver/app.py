"""Small standalone reference-resolution gateway prototype.

This service deliberately lives outside the SRC request path.  It normalizes
heterogeneous resolution resources behind one HTTP contract.  The first
adapter is read-only Neo4j access to the existing contact/entity/finding graph.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from fastapi import FastAPI
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

log = logging.getLogger("sunaq.resolver")


def normalize_reference(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^0-9a-zäöüßà-ž]+", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def read_password_file(path: str, username: str = "neo4j") -> str:
    """Read either a raw password or Neo4j's conventional user/password file."""
    value = Path(path).read_text(encoding="utf-8").strip()
    prefix = f"{username}/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    if not value:
        raise RuntimeError("Neo4j password file is empty")
    return value


@dataclass(frozen=True)
class Neo4jResolverSettings:
    uri: str = "bolt://127.0.0.1:7687"
    username: str = "neo4j"
    password_file: str = "/run/sunaq-secrets/neo4j_auth"
    database: str = "neo4j"

    @classmethod
    def from_env(cls) -> "Neo4jResolverSettings":
        return cls(
            uri=os.getenv("RESOLVER_NEO4J_URI", cls.uri),
            username=os.getenv("RESOLVER_NEO4J_USERNAME", cls.username),
            password_file=os.getenv("RESOLVER_NEO4J_PASSWORD_FILE", cls.password_file),
            database=os.getenv("RESOLVER_NEO4J_DATABASE", cls.database),
        )


class ReferenceInput(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    context: list[str] = Field(default_factory=list)
    hints: dict[str, str] = Field(default_factory=dict)


class ResolveRequest(BaseModel):
    references: list[ReferenceInput] = Field(min_length=1, max_length=20)
    limit_per_reference: int = Field(default=8, ge=1, le=20)
    sources: list[str] = Field(default_factory=list, max_length=8)


class EnrichRequest(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=300)
    kind: str = Field(min_length=1, max_length=64)
    include_holdings: bool = False


class ResearchArtifact(BaseModel):
    mode: Literal["inline_text", "remote"]
    file_name: str = Field(min_length=1, max_length=240)
    content_type: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=2_000_000)
    url: str = Field(default="", max_length=4096)
    include_in_summary: bool = False
    summary_markdown: str = Field(default="", max_length=200_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnrichResponse(BaseModel):
    source: str
    candidate_id: str
    kind: str
    details: dict[str, Any]
    artifacts: list[ResearchArtifact] = Field(default_factory=list, max_length=32)


@dataclass(frozen=True)
class AdapterEnrichment:
    details: dict[str, Any]
    artifacts: list[dict[str, Any]]


class Candidate(BaseModel):
    source: str
    candidate_id: str
    canonical: str
    kind: str = ""
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    match: str
    confidence: float | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class ReferenceResult(BaseModel):
    reference: str
    status: str
    candidates: list[Candidate] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ResolveResponse(BaseModel):
    results: list[ReferenceResult]


class ResolverAdapter(Protocol):
    name: str

    def health(self) -> dict[str, Any]: ...
    def resolve(self, reference: ReferenceInput, *, limit: int) -> list[dict[str, Any]]: ...


class Neo4jResolver:
    name = "neo4j"

    def __init__(self, settings: Neo4jResolverSettings):
        self.settings = settings
        password = read_password_file(settings.password_file, settings.username)
        self.driver = GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, password),
        )

    def close(self) -> None:
        self.driver.close()

    def health(self) -> dict[str, Any]:
        self.driver.verify_connectivity()
        return {"status": "ok", "uri": self.settings.uri, "database": self.settings.database}

    def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self.driver.session(database=self.settings.database) as session:
            return [record.data() for record in session.run(query, **params)]

    @staticmethod
    def _kind(labels: list[str]) -> str:
        preferred = ("Person", "Organization", "OrganizationalUnit")
        for label in preferred:
            if label in labels:
                return label
        return next((label for label in labels if label != "Entity"), "")

    @staticmethod
    def _match_score(forms: list[dict[str, Any]], normalized: str) -> tuple[str, float]:
        best_kind = "prefix"
        best_score = 0.70
        for form in forms:
            form_norm = str(form.get("normalized") or "")
            rel_type = str(form.get("rel_type") or "")
            if form_norm == normalized:
                if rel_type == "HAS_NAME":
                    return "exact_name", 0.98
                best_kind, best_score = "exact_alias", max(best_score, 0.93)
            elif form_norm.startswith(normalized) or normalized.startswith(form_norm):
                if rel_type == "HAS_NAME" and best_score < 0.82:
                    best_kind, best_score = "prefix_name", 0.82
                elif best_score < 0.78:
                    best_kind, best_score = "prefix_alias", 0.78
        return best_kind, best_score

    def resolve(self, reference: ReferenceInput, *, limit: int) -> list[dict[str, Any]]:
        normalized = normalize_reference(reference.text)
        if not normalized:
            return []

        rows = self._run(
            """
            MATCH (e:Entity)
            WHERE coalesce(properties(e)['identity_status'],'') <> 'merged'
              AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            OPTIONAL MATCH (e)-[rf]->(form)
            WHERE type(rf) IN ['HAS_NAME','HAS_SEARCH_ALIAS']
              AND (form:EntityName OR form:SearchAlias)
              AND coalesce(properties(rf)['active'],true)=true
              AND coalesce(properties(rf)['resolution_policy'],'contextual') <> 'document_only'
            WITH e, collect(DISTINCT CASE WHEN form IS NULL THEN NULL ELSE {
                value: coalesce(properties(form)['value'], properties(form)['last_seen_value'], properties(form)['normalized']),
                normalized: properties(form)['normalized'],
                rel_type: type(rf),
                policy: coalesce(properties(rf)['resolution_policy'],'')
            } END) AS forms
            WHERE any(x IN forms WHERE
                x.normalized=$normalized
                OR x.normalized STARTS WITH $normalized
                OR $normalized STARTS WITH x.normalized)
            OPTIONAL MATCH (e)-[ar:HAS_ADDRESS]->(addr:PostalAddress)
            WHERE coalesce(properties(ar)['active'],true)=true
            WITH e, forms, collect(DISTINCT coalesce(properties(addr)['value'], properties(addr)['normalized'])) AS addresses
            OPTIONAL MATCH (e)-[wr:WORKS_AT]->(org:Entity)
            WHERE coalesce(properties(wr)['active'],true)=true
            WITH e, forms, addresses, collect(DISTINCT org.display_name) AS organizations
            OPTIONAL MATCH (c:ContactRecord)-[:DESCRIBES]->(e)
            WITH e, forms, addresses, organizations, count(DISTINCT c) AS contact_count
            OPTIONAL MATCH (f:ResearchFinding)-[:QUERY_ENTITY]->(e)
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   forms,
                   addresses,
                   organizations,
                   contact_count,
                   count(DISTINCT f) AS finding_count
            LIMIT $candidate_limit
            """,
            normalized=normalized,
            candidate_limit=max(limit * 4, 20),
        )

        context_terms = [normalize_reference(item) for item in reference.context if normalize_reference(item)]
        candidates: list[dict[str, Any]] = []
        for row in rows:
            forms = [item for item in (row.get("forms") or []) if isinstance(item, dict)]
            match_kind, score = self._match_score(forms, normalized)
            names = [
                str(item.get("value") or "").strip()
                for item in forms
                if str(item.get("rel_type") or "") == "HAS_NAME" and str(item.get("value") or "").strip()
            ]
            aliases = [
                str(item.get("value") or "").strip()
                for item in forms
                if str(item.get("rel_type") or "") == "HAS_SEARCH_ALIAS" and str(item.get("value") or "").strip()
            ]
            addresses = [str(v) for v in (row.get("addresses") or []) if str(v or "").strip()]
            organizations = [str(v) for v in (row.get("organizations") or []) if str(v or "").strip()]
            haystack = normalize_reference(" ".join(
                [str(row.get("display_name") or "")] + names + aliases + addresses + organizations
            ))
            context_hits = [term for term in context_terms if term and term in haystack]
            score = min(0.99, score + min(0.09, 0.03 * len(context_hits)))
            canonical = str(row.get("display_name") or "").strip() or (names[0] if names else reference.text)

            candidates.append({
                "source": self.name,
                "candidate_id": str(row.get("entity_id") or ""),
                "canonical": canonical,
                "kind": self._kind(list(row.get("labels") or [])),
                "aliases": sorted(set(names + aliases) - {canonical}, key=str.casefold),
                "attributes": {
                    "addresses": addresses,
                    "organizations": organizations,
                    "context_hits": context_hits,
                },
                "match": match_kind,
                "confidence": round(score, 3),
                "provenance": {
                    "contact_records": int(row.get("contact_count") or 0),
                    "research_findings": int(row.get("finding_count") or 0),
                },
            })

        candidates.sort(key=lambda item: (
            -(item.get("confidence") or 0.0),
            str(item.get("canonical") or "").casefold(),
        ))
        return candidates[:limit]



def _object_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, dict) else {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump()
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _kind_hint(reference: ReferenceInput) -> str:
    return normalize_reference(reference.hints.get("kind") or reference.hints.get("type") or "")


@dataclass(frozen=True)
class OpenRegisterSettings:
    api_key_file: str = ""
    timeout: float = 5.0
    candidate_fetch_limit: int = 100

    @classmethod
    def from_env(cls) -> "OpenRegisterSettings":
        return cls(
            api_key_file=os.getenv("RESOLVER_OPENREGISTER_API_KEY_FILE", "").strip(),
            timeout=max(0.5, float(os.getenv("RESOLVER_OPENREGISTER_TIMEOUT", "5"))),
            candidate_fetch_limit=max(
                20,
                min(
                    500,
                    int(os.getenv("RESOLVER_OPENREGISTER_CANDIDATE_FETCH_LIMIT", "100")),
                ),
            ),
        )

    def api_key(self) -> str:
        if self.api_key_file:
            value = Path(self.api_key_file).read_text(encoding="utf-8").strip()
        else:
            value = os.getenv("OPENREGISTER_API_KEY", "").strip()
        if not value:
            raise RuntimeError("OpenRegister API key is empty")
        return value


class OpenRegisterResolver:
    """Optional external German company/person resolver.

    Only the reference text is sent to OpenRegister during candidate search.
    Context stays local and is used solely to rank returned candidates, reducing
    accidental data egress. Stable OpenRegister ids can then be enriched through
    the dedicated /enrich contract.
    """

    name = "openregister"
    _COMPANY_ID_RE = re.compile(r"^DE-[A-Z0-9][A-Z0-9-]{4,}$", re.IGNORECASE)
    _LEGAL_FORM_RE = re.compile(
        r"\b(?:gmbh|ag|ug|kg|ohg|gbr|se|eg|kgaa|gmbh\s*&\s*co\.?\s*kg)\b",
        re.IGNORECASE,
    )
    _PERSON_HINTS = {"person", "natural_person", "natural person", "personne", "personen"}
    _ORG_HINTS = {"organization", "organisation", "company", "firma", "unternehmen"}

    def __init__(self, settings: OpenRegisterSettings, client: Any | None = None):
        self.settings = settings
        if client is not None:
            self.client = client
            return
        try:
            from openregister import Openregister
        except ImportError as exc:
            raise RuntimeError("openregister-sdk is not installed") from exc
        self.client = Openregister(
            api_key=settings.api_key(),
            timeout=settings.timeout,
            max_retries=0,
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def health(self) -> dict[str, Any]:
        # Do not spend API credits or leak data from a health check.
        return {"status": "ok", "mode": "configured", "external": True}

    def _reference_kind(self, reference: ReferenceInput) -> str:
        hint = _kind_hint(reference)
        if hint in self._PERSON_HINTS:
            return "person"
        if hint in self._ORG_HINTS:
            return "organization"
        text = reference.text.strip()
        if self._COMPANY_ID_RE.fullmatch(text) or self._LEGAL_FORM_RE.search(text):
            return "organization"
        return ""

    @staticmethod
    def _context_terms(reference: ReferenceInput) -> list[str]:
        return [
            normalize_reference(item)
            for item in reference.context
            if normalize_reference(item)
        ]

    @staticmethod
    def _external_search_reference(reference: ReferenceInput) -> ReferenceInput:
        """Keep locally parsed constraints out of the external search term.

        Query rewriters may occasionally repeat a location/address constraint at
        the end of the entity text (for example "FLG Automation in Karben").
        Because that same value is already present in reference.context, remove
        only an exact trailing context value (optionally preceded by a common
        separator/preposition). Never strip context from the middle of a name.
        """
        text = reference.text.strip()
        for raw_context in sorted(
            (str(value or "").strip() for value in reference.context),
            key=len,
            reverse=True,
        ):
            if not raw_context:
                continue
            escaped = re.escape(raw_context)
            patterns = (
                rf"\s+(?:in|aus|bei|nahe|near)\s+{escaped}\s*$",
                rf"\s*[,;|–—-]\s*{escaped}\s*$",
                rf"\s+{escaped}\s*$",
            )
            for pattern in patterns:
                cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" ,;|-–—")
                if cleaned and cleaned != text:
                    return reference.model_copy(update={"text": cleaned})
        return reference

    @staticmethod
    def _candidate_from_company(
        raw: Any,
        *,
        reference: ReferenceInput,
        match_override: str = "",
        score_override: float | None = None,
    ) -> dict[str, Any] | None:
        data = _object_dict(raw)
        company_id = str(data.get("company_id") or data.get("id") or "").strip()

        name_value = data.get("name")
        name_obj = _object_dict(name_value)
        if name_obj:
            name = str(name_obj.get("name") or "").strip()
        else:
            name = str(name_value or "").strip()

        register = _object_dict(data.get("company_register") or data.get("register") or {})
        address = _object_dict(data.get("address") or data.get("registered_address") or {})
        purpose_obj = _object_dict(data.get("purpose") or {})
        formatted_address = str(
            address.get("formatted_value")
            or address.get("formatted")
            or address.get("value")
            or ""
        ).strip()
        if not company_id or not name:
            return None

        canonical_norm = normalize_reference(name)
        reference_norm = normalize_reference(reference.text)
        if match_override:
            match = match_override
            score = float(score_override or 0.0)
        elif canonical_norm == reference_norm:
            match, score = "exact_name", 0.98
        elif canonical_norm.startswith(reference_norm) or reference_norm.startswith(canonical_norm):
            match, score = "prefix_name", 0.84
        else:
            match, score = "autocomplete", 0.76

        purpose_text = str(
            purpose_obj.get("purpose")
            or data.get("purpose_text")
            or (data.get("purpose") if isinstance(data.get("purpose"), str) else "")
            or ""
        ).strip()
        register_court = str(data.get("register_court") or register.get("register_court") or "").strip()
        register_number = str(data.get("register_number") or register.get("register_number") or "").strip()
        legal_form = str(
            data.get("legal_form")
            or name_obj.get("legal_form")
            or ""
        ).strip()
        active = data.get("active")
        if active is None and str(data.get("status") or "").strip():
            active = str(data.get("status") or "").strip().casefold() == "active"

        context_terms = OpenRegisterResolver._context_terms(reference)
        local_haystack = normalize_reference(" ".join([
            name,
            formatted_address,
            register_court,
            purpose_text,
        ]))
        context_hits = [term for term in context_terms if term in local_haystack]
        score = min(0.99, score + min(0.06, 0.02 * len(context_hits)))

        return {
            "source": "openregister",
            "candidate_id": company_id,
            "canonical": name,
            "kind": "Organization",
            "aliases": [],
            "attributes": {
                "active": active,
                "legal_form": legal_form,
                "register_court": register_court,
                "register_number": register_number,
                "address": formatted_address,
                "context_hits": context_hits,
            },
            "match": match,
            "confidence": round(score, 3),
            "provenance": {
                "registry": "OpenRegister",
                "external": True,
                "outbound_query": reference.text.strip(),
                "context_sent_external": False,
            },
        }

    @staticmethod
    def _candidate_from_person(raw: Any, *, reference: ReferenceInput) -> dict[str, Any] | None:
        data = _object_dict(raw)
        person_id = str(data.get("id") or data.get("person_id") or "").strip()
        name = str(data.get("name") or "").strip()
        if not person_id or not name:
            return None

        city = str(data.get("city") or "").strip()
        date_of_birth = str(data.get("date_of_birth") or "").strip()
        reference_norm = normalize_reference(reference.text)
        candidate_norm = normalize_reference(name)
        if candidate_norm == reference_norm:
            match, score = "exact_name", 0.90
        elif candidate_norm.startswith(reference_norm) or reference_norm.startswith(candidate_norm):
            match, score = "prefix_name", 0.80
        else:
            match, score = "search", 0.72

        context_terms = OpenRegisterResolver._context_terms(reference)
        local_haystack = normalize_reference(" ".join([name, city, date_of_birth]))
        context_hits = [term for term in context_terms if term in local_haystack]
        score = min(0.99, score + min(0.08, 0.06 * len(context_hits)))

        return {
            "source": "openregister",
            "candidate_id": person_id,
            "canonical": name,
            "kind": "Person",
            "aliases": [],
            "attributes": {
                "active": data.get("active"),
                "city": city,
                "date_of_birth": date_of_birth,
                "context_hits": context_hits,
            },
            "match": match,
            "confidence": round(score, 3),
            "provenance": {
                "registry": "OpenRegister",
                "external": True,
                "outbound_query": reference.text.strip(),
                "context_sent_external": False,
            },
        }

    @staticmethod
    def _sanitize_company_details(raw: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Preserve provider data while separating short-lived transport URLs."""
        data = _object_dict(raw)
        sources = [
            _object_dict(item)
            for item in (data.pop("sources", None) or [])
            if _object_dict(item)
        ]
        source_documents = [
            {"url": str(item.get("document_url") or "").strip()}
            for item in sources
            if str(item.get("document_url") or "").strip().startswith("https://")
        ]

        name_obj = _object_dict(data.get("name") or {})
        purpose_obj = _object_dict(data.get("purpose") or {})
        if not str(data.get("id") or "").strip():
            data["id"] = str(data.get("company_id") or "")
        if name_obj:
            data["name_record"] = name_obj
            data["name"] = str(name_obj.get("name") or "")
        if purpose_obj:
            data["purpose_record"] = purpose_obj
            data["purpose"] = str(purpose_obj.get("purpose") or "")
        data["register_documents_available"] = len(source_documents)
        return data, source_documents

    @staticmethod
    def _portable_fact_rows(details: dict[str, Any]) -> list[tuple[str, str]]:
        """Flatten structured provider data into a portable path/value table."""
        rows: list[tuple[str, str]] = []

        def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key in sorted(value, key=lambda item: str(item).casefold()):
                    child = f"{path}.{key}" if path else str(key)
                    walk(value[key], child)
                return
            if isinstance(value, list):
                for index, item in enumerate(value, start=1):
                    child = f"{path}[{index}]" if path else f"[{index}]"
                    walk(item, child)
                return
            if value is None:
                return
            rendered = str(value).strip()
            if path and rendered:
                rows.append((path, rendered))

        walk(details, "")
        return rows

    @classmethod
    def _research_artifacts(
        cls,
        details: dict[str, Any],
        source_documents: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []

        details_text = json.dumps(
            details,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ) + "\n"
        artifacts.append({
            "mode": "inline_text",
            "file_name": "details.json",
            "content_type": "application/json; charset=utf-8",
            "title": "Strukturierter Provider-Snapshot",
            "content": details_text,
            "metadata": {
                "artifact_role": "provider_snapshot",
                "source": "OpenRegister",
            },
        })

        rows = cls._portable_fact_rows(details)
        display_name = str(details.get("name") or "").strip()
        if not display_name:
            display_name = " ".join(
                part
                for part in (
                    str(details.get("first_name") or "").strip(),
                    str(details.get("last_name") or "").strip(),
                )
                if part
            ).strip()
        external_id = str(details.get("id") or details.get("company_id") or "").strip()

        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer)
        writer.writerow(["source", "entity", "external_id", "path", "value"])
        writer.writerows(
            ("OpenRegister", display_name, external_id, path, value)
            for path, value in rows
        )
        csv_text = csv_buffer.getvalue()

        heading = "## Strukturierte Registerdaten"
        if display_name:
            heading += " – " + display_name.replace("\n", " ").replace("|", "\\|")
        markdown_lines = [
            heading,
            "",
            "| Feld | Wert |",
            "|---|---|",
        ]
        max_summary_rows = 160
        summary_rows = [
            (path, value)
            for path, value in rows
            if not any(
                token in path.casefold()
                for token in ("date_of_birth", "birth_date", "birthdate", ".age")
            )
        ]
        for path, value in summary_rows[:max_summary_rows]:
            safe_path = path.replace("|", "\\|")
            safe_value = value.replace("\n", " ").replace("|", "\\|")
            markdown_lines.append(f"| {safe_path} | {safe_value} |")
        if len(summary_rows) > max_summary_rows:
            markdown_lines.extend([
                "",
                (
                    f"_Die Tabelle ist auf {max_summary_rows} Zeilen gekürzt; "
                    f"die vollständige portable Projektion steht in facts.csv "
                    f"({len(rows)} Zeilen)._"
                ),
            ])

        artifacts.append({
            "mode": "inline_text",
            "file_name": "facts.csv",
            "content_type": "text/csv; charset=utf-8",
            "title": "Portable strukturierte Fakten",
            "content": csv_text,
            "include_in_summary": True,
            "summary_markdown": "\n".join(markdown_lines).rstrip() + "\n",
            "metadata": {
                "artifact_role": "portable_facts",
                "source": "OpenRegister",
                "row_count": len(rows),
            },
        })

        for index, source in enumerate(source_documents, start=1):
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            artifacts.append({
                "mode": "remote",
                "file_name": f"openregister-source-{index:02d}.pdf",
                "content_type": "application/pdf",
                "title": f"OpenRegister-Quelldokument {index}",
                "url": url,
                "metadata": {
                    "artifact_role": "primary_source",
                    "source": "OpenRegister",
                    "retrieval_mode": "supplied_source_document",
                    "realtime": False,
                },
            })
        return artifacts

    @staticmethod
    def _sanitize_person_details(raw: Any) -> dict[str, Any]:
        data = _object_dict(raw)
        return {
            "id": str(data.get("id") or ""),
            "first_name": str(data.get("first_name") or ""),
            "last_name": str(data.get("last_name") or ""),
            "city": str(data.get("city") or ""),
            "date_of_birth": str(data.get("date_of_birth") or ""),
            "age": data.get("age"),
            "management_positions": [
                _object_dict(item) for item in (data.get("management_positions") or [])
            ],
        }

    def resolve(self, reference: ReferenceInput, *, limit: int) -> list[dict[str, Any]]:
        kind = self._reference_kind(reference)
        if not kind:
            return []

        search_reference = self._external_search_reference(reference)
        text = search_reference.text.strip()
        candidates: list[dict[str, Any]] = []
        if kind == "person":
            response = self.client.search.find_person_v1(query={"value": text})
            for item in list(getattr(response, "results", []) or [])[
                : self.settings.candidate_fetch_limit
            ]:
                candidate = self._candidate_from_person(item, reference=search_reference)
                if candidate:
                    candidates.append(candidate)
        elif self._COMPANY_ID_RE.fullmatch(text):
            response = self.client.company.get_details_v1(company_id=text)
            candidate = self._candidate_from_company(
                response,
                reference=search_reference,
                match_override="exact_register_id",
                score_override=0.99,
            )
            if candidate:
                candidates.append(candidate)
        else:
            # Use the company-search endpoint rather than autocomplete for
            # resolver queries. Resolver references may be incomplete name
            # anchors ("Kurfürstendamm"), while autocomplete is optimized for
            # type-ahead of known company names. Keep local constraints such as
            # city out of the provider request and apply them only in ranking.
            response = self.client.search.find_companies_v1(
                query={"value": text},
                pagination={
                    "page": 1,
                    "per_page": self.settings.candidate_fetch_limit,
                },
            )
            for item in list(getattr(response, "results", []) or [])[
                : self.settings.candidate_fetch_limit
            ]:
                candidate = self._candidate_from_company(item, reference=search_reference)
                if candidate:
                    candidates.append(candidate)

        candidates.sort(key=lambda item: (
            -(item.get("confidence") or 0.0),
            str(item.get("canonical") or "").casefold(),
        ))
        return candidates[:limit]

    def enrich(
        self,
        *,
        candidate_id: str,
        kind: str,
        include_holdings: bool = False,
    ) -> dict[str, Any] | AdapterEnrichment:
        kind_norm = normalize_reference(kind)
        if kind_norm in {"person", "natural person", "natural_person"}:
            details = self._sanitize_person_details(
                self.client.person.get_details_v1(candidate_id)
            )
            if include_holdings:
                holdings = _object_dict(self.client.person.get_holdings_v1(candidate_id))
                details["holdings"] = [
                    _object_dict(item) for item in (holdings.get("holdings") or [])
                ]
            return AdapterEnrichment(
                details=details,
                artifacts=self._research_artifacts(details, []),
            )
        if kind_norm in {"organization", "organisation", "company", "firma", "unternehmen"}:
            details, source_documents = self._sanitize_company_details(
                self.client.company.get_details_v1(company_id=candidate_id)
            )
            return AdapterEnrichment(
                details=details,
                artifacts=self._research_artifacts(details, source_documents),
            )
        raise ValueError(f"unsupported OpenRegister enrichment kind: {kind}")



@dataclass(frozen=True)
class OpenPlzSettings:
    base_url: str = "https://openplzapi.org"
    timeout: float = 4.0

    @classmethod
    def from_env(cls) -> "OpenPlzSettings":
        return cls(
            base_url=os.getenv("RESOLVER_OPENPLZ_BASE_URL", cls.base_url).rstrip("/"),
            timeout=max(0.5, float(os.getenv("RESOLVER_OPENPLZ_TIMEOUT", "4"))),
        )


class OpenPlzResolver:
    """Optional external German postal/locality resolver."""

    name = "openplz"
    _POSTAL_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
    _STREET_RE = re.compile(
        r"\b(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|markt)\b",
        re.IGNORECASE,
    )

    def __init__(self, settings: OpenPlzSettings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def health(self) -> dict[str, Any]:
        # Avoid external requests solely for health reporting.
        return {"status": "ok", "mode": "configured", "external": True}

    def _accepts(self, reference: ReferenceInput) -> bool:
        hint = _kind_hint(reference)
        if hint:
            return hint in {
                "location", "address", "postal address", "postaladdress",
                "street", "locality", "ort", "adresse",
            }
        text = reference.text.strip()
        return bool(self._POSTAL_RE.search(text) or self._STREET_RE.search(text))

    @staticmethod
    def _context_hits(reference: ReferenceInput, haystack: str) -> list[str]:
        normalized = normalize_reference(haystack)
        return [
            term for term in (normalize_reference(item) for item in reference.context)
            if term and term in normalized
        ]

    def _locality_candidates(
        self,
        reference: ReferenceInput,
        *,
        limit: int,
        postal_code: str,
    ) -> list[dict[str, Any]]:
        remainder = re.sub(rf"(?<!\d){re.escape(postal_code)}(?!\d)", " ", reference.text)
        locality_name = re.sub(r"\s+", " ", remainder).strip(" ,")
        params: dict[str, Any] = {"postalCode": postal_code, "pageSize": min(50, max(limit * 2, 10))}
        if locality_name:
            params["name"] = locality_name
        response = self.client.get("/de/Localities", params=params)
        response.raise_for_status()
        rows = response.json()
        candidates: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            postal = str(row.get("postalCode") or "").strip()
            if not name or not postal:
                continue
            exact_name = bool(locality_name) and normalize_reference(name) == normalize_reference(locality_name)
            exact_postal = postal == postal_code
            score = 0.98 if exact_name and exact_postal else (0.92 if exact_postal else 0.80)
            context_hits = self._context_hits(reference, f"{postal} {name}")
            score = min(0.99, score + min(0.04, 0.02 * len(context_hits)))
            candidates.append({
                "source": self.name,
                "candidate_id": f"openplz:de:locality:{postal}:{normalize_reference(name)}",
                "canonical": f"{postal} {name}",
                "kind": "Locality",
                "aliases": [],
                "attributes": {
                    "postal_code": postal,
                    "locality": name,
                    "municipality": _object_dict(row.get("municipality")),
                    "federal_state": _object_dict(row.get("federalState")),
                    "context_hits": context_hits,
                },
                "match": "exact_postal_locality" if exact_name and exact_postal else "postal_locality",
                "confidence": round(score, 3),
                "provenance": {"registry": "OpenPLZ", "external": True},
            })
        return candidates[:limit]

    def _street_candidates(self, reference: ReferenceInput, *, limit: int) -> list[dict[str, Any]]:
        response = self.client.get(
            "/de/FullTextSearch",
            params={"searchTerm": reference.text.strip(), "pageSize": min(50, max(limit * 2, 10))},
        )
        response.raise_for_status()
        rows = response.json()
        query_norm = normalize_reference(reference.text)
        candidates: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            street = str(row.get("name") or "").strip()
            locality = str(row.get("locality") or "").strip()
            postal = str(row.get("postalCode") or "").strip()
            if not street or not locality or not postal:
                continue
            canonical = f"{street}, {postal} {locality}"
            candidate_norm = normalize_reference(canonical)
            query_terms = [term for term in query_norm.split() if term]
            matched_terms = sum(1 for term in query_terms if term in candidate_norm)
            ratio = matched_terms / max(1, len(query_terms))
            score = 0.90 if ratio >= 0.75 else (0.82 if ratio >= 0.5 else 0.72)
            context_hits = self._context_hits(reference, canonical)
            score = min(0.96, score + min(0.04, 0.02 * len(context_hits)))
            candidates.append({
                "source": self.name,
                "candidate_id": f"openplz:de:street:{postal}:{normalize_reference(locality)}:{normalize_reference(street)}",
                "canonical": canonical,
                "kind": "Street",
                "aliases": [],
                "attributes": {
                    "postal_code": postal,
                    "locality": locality,
                    "street": street,
                    "borough": row.get("borough"),
                    "suburb": row.get("suburb"),
                    "municipality": _object_dict(row.get("municipality")),
                    "federal_state": _object_dict(row.get("federalState")),
                    "house_number_verified": False,
                    "context_hits": context_hits,
                },
                "match": "postal_street",
                "confidence": round(score, 3),
                "provenance": {"registry": "OpenPLZ", "external": True},
            })
        candidates.sort(key=lambda item: (
            -(item.get("confidence") or 0.0),
            str(item.get("canonical") or "").casefold(),
        ))
        return candidates[:limit]

    def resolve(self, reference: ReferenceInput, *, limit: int) -> list[dict[str, Any]]:
        if not self._accepts(reference):
            return []
        postal_match = self._POSTAL_RE.search(reference.text)
        has_street_signal = bool(self._STREET_RE.search(reference.text))
        if postal_match and not has_street_signal:
            return self._locality_candidates(reference, limit=limit, postal_code=postal_match.group(1))
        return self._street_candidates(reference, limit=limit)


class ResolverGateway:
    def __init__(self, adapters: list[ResolverAdapter], startup_errors: dict[str, str] | None = None):
        self.adapters = adapters
        self.startup_errors = dict(startup_errors or {})

    def close(self) -> None:
        for adapter in self.adapters:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def capabilities(self) -> dict[str, Any]:
        return {
            "service": "sunaq-erg-resolver",
            "capabilities": ["resolve_reference", "enrich_entity"],
            "adapters": [adapter.name for adapter in self.adapters],
            "unavailable_adapters": sorted(self.startup_errors),
        }

    def health(self) -> dict[str, Any]:
        adapters: dict[str, Any] = {}
        overall = "degraded" if self.startup_errors else "ok"
        for name, error_type in self.startup_errors.items():
            adapters[name] = {"status": "error", "error": error_type}
        for adapter in self.adapters:
            try:
                adapters[adapter.name] = adapter.health()
            except Exception as exc:  # local diagnostic endpoint; do not expose secret values
                overall = "degraded"
                adapters[adapter.name] = {"status": "error", "error": type(exc).__name__}
        return {"status": overall, "adapters": adapters}

    def resolve(self, request: ResolveRequest) -> ResolveResponse:
        results: list[ReferenceResult] = []
        allowed_sources = {
            str(value or "").strip().casefold()
            for value in request.sources
            if str(value or "").strip()
        }
        for reference in request.references:
            merged: list[Candidate] = []
            errors: list[str] = []
            for adapter in self.adapters:
                if allowed_sources and adapter.name.casefold() not in allowed_sources:
                    continue
                try:
                    merged.extend(Candidate.model_validate(item) for item in adapter.resolve(
                        reference, limit=request.limit_per_reference
                    ))
                except Exception as exc:
                    log.exception("resolver adapter %s failed", adapter.name)
                    errors.append(f"{adapter.name}:{type(exc).__name__}")

            merged.sort(key=lambda item: (
                -(item.confidence or 0.0),
                item.canonical.casefold(),
                item.source.casefold(),
            ))
            merged = merged[:request.limit_per_reference]
            if not merged:
                status = "error" if errors else "unresolved"
            elif len(merged) == 1:
                # A single weak candidate is still only a candidate. Do not
                # silently promote it merely because no competing result was
                # returned, and do not enter the multi-candidate margin logic.
                status = (
                    "resolved"
                    if (merged[0].confidence or 0.0) >= 0.90
                    else "candidates"
                )
            else:
                top_context = list((merged[0].attributes or {}).get("context_hits") or [])
                competing_context = any(
                    list((candidate.attributes or {}).get("context_hits") or [])
                    for candidate in merged[1:]
                )
                top_confidence = merged[0].confidence or 0.0
                second_confidence = merged[1].confidence or 0.0
                strong_exact_margin = (
                    str(merged[0].match or "").strip().casefold() == "exact_name"
                    and top_confidence >= 0.95
                    and (top_confidence - second_confidence) >= 0.10
                )
                unique_context_match = (
                    top_confidence >= 0.90
                    and bool(top_context)
                    and not competing_context
                )
                if strong_exact_margin or unique_context_match:
                    # Exact-name separation or unique local-only context may
                    # disambiguate without inventing aliases or exporting the
                    # context to the external registry.
                    status = "resolved"
                else:
                    status = "candidates"
            results.append(ReferenceResult(
                reference=reference.text,
                status=status,
                candidates=merged,
                errors=errors,
            ))
        return ResolveResponse(results=results)

    def enrich(self, request: EnrichRequest) -> EnrichResponse:
        source = request.source.strip().casefold()
        for adapter in self.adapters:
            if adapter.name.casefold() != source:
                continue
            method = getattr(adapter, "enrich", None)
            if not callable(method):
                raise ValueError(f"resolver adapter {adapter.name} does not support enrichment")
            value = method(
                candidate_id=request.candidate_id,
                kind=request.kind,
                include_holdings=request.include_holdings,
            )
            if isinstance(value, AdapterEnrichment):
                details = value.details
                artifacts = value.artifacts
            else:
                details = value if isinstance(value, dict) else {}
                artifacts = []
            return EnrichResponse(
                source=adapter.name,
                candidate_id=request.candidate_id,
                kind=request.kind,
                details=details,
                artifacts=artifacts,
            )
        raise ValueError(f"resolver adapter is not available: {request.source}")



def _env_enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().casefold() in {"1", "true", "yes", "on"}


def gateway_from_env() -> ResolverGateway:
    adapters: list[ResolverAdapter] = []
    startup_errors: dict[str, str] = {}

    def add_optional(name: str, factory: Any) -> None:
        try:
            adapters.append(factory())
        except Exception as exc:
            # Resolver enrichment is optional. One broken adapter must never
            # make healthy local/other adapters unavailable.
            log.exception("resolver adapter %s failed to initialize", name)
            startup_errors[name] = type(exc).__name__

    if _env_enabled("RESOLVER_NEO4J_ENABLED", "true"):
        add_optional("neo4j", lambda: Neo4jResolver(Neo4jResolverSettings.from_env()))
    if _env_enabled("RESOLVER_OPENREGISTER_ENABLED"):
        add_optional("openregister", lambda: OpenRegisterResolver(OpenRegisterSettings.from_env()))
    if _env_enabled("RESOLVER_OPENPLZ_ENABLED"):
        add_optional("openplz", lambda: OpenPlzResolver(OpenPlzSettings.from_env()))
    return ResolverGateway(adapters, startup_errors=startup_errors)


app = FastAPI(title="SunaQ ERG Resolver", version="0.8.7-rc2")
_gateway: ResolverGateway | None = None


def get_gateway() -> ResolverGateway:
    global _gateway
    if _gateway is None:
        _gateway = gateway_from_env()
    return _gateway


@app.get("/health")
async def health() -> dict[str, Any]:
    return await asyncio.to_thread(get_gateway().health)


@app.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    return get_gateway().capabilities()


@app.post("/resolve", response_model=ResolveResponse)
async def resolve(request: ResolveRequest) -> ResolveResponse:
    return await asyncio.to_thread(get_gateway().resolve, request)


@app.post("/enrich", response_model=EnrichResponse)
async def enrich(request: EnrichRequest) -> EnrichResponse:
    return await asyncio.to_thread(get_gateway().enrich, request)
