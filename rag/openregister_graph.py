"""Persist OpenRegister research as a separate external Neo4j layer.

This module deliberately does not write :Entity or :ResearchFinding nodes.
OpenRegister material is provider-derived external evidence, not curated SunaQ
knowledge. Stable provider IDs deduplicate external entities, while per-run
findings preserve whether a hit was selected, a candidate, or incidental
bycatch.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError:  # optional in deployments without Neo4j
    GraphDatabase = None

from rag.secret_env import secret_env


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _neo4j_connection(cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    neo = cfg.get("neo4j") or {}
    if not _truthy(neo.get("enabled")):
        raise RuntimeError("Neo4j is disabled")
    uri = str(neo.get("uri") or "bolt://127.0.0.1:7687")
    username = str(neo.get("username") or "neo4j")
    env_name = str(neo.get("password_env") or "").strip()
    password = secret_env(env_name, "") if env_name else ""
    if not password:
        password = str(neo.get("password") or "")
    if not password:
        raise RuntimeError("Neo4j password is not configured")
    database = str(neo.get("database") or "neo4j")
    return uri, username, password, database


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _clean(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _hash(*parts: Any) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _kind(value: Any) -> str:
    folded = _clean(value, 80).casefold()
    if folded in {"organization", "organisation", "company", "firma", "unternehmen", "legal_person"}:
        return "Organization"
    if folded in {"person", "natural_person", "natural person"}:
        return "Person"
    return _clean(value, 80) or "Entity"


def _display_name(details: dict[str, Any], fallback: str = "") -> str:
    raw = details.get("name")
    if isinstance(raw, dict):
        return _clean(raw.get("name") or fallback, 300)
    if raw:
        return _clean(raw, 300)
    first = _clean(details.get("first_name"), 150)
    last = _clean(details.get("last_name"), 150)
    return _clean(" ".join(part for part in (first, last) if part) or fallback, 300)


def _candidate_state(result_status: str, rank: int) -> str:
    if result_status.casefold() == "resolved" and rank == 1:
        return "selected"
    if rank == 1:
        return "candidate"
    return "bycatch"


def _candidate_label(state: str) -> str:
    return {
        "selected": "Ausgewählt",
        "candidate": "Kandidat",
        "bycatch": "Beifang",
    }.get(str(state or "").strip().casefold(), str(state or ""))


class OpenRegisterGraphStore:
    """Idempotent writer for the external OpenRegister graph namespace."""

    SCHEMA = (
        "CREATE CONSTRAINT openregister_run_id IF NOT EXISTS "
        "FOR (n:OpenRegisterRun) REQUIRE n.run_id IS UNIQUE",
        "CREATE CONSTRAINT openregister_entity_key IF NOT EXISTS "
        "FOR (n:OpenRegisterEntity) REQUIRE n.external_key IS UNIQUE",
        "CREATE CONSTRAINT openregister_finding_id IF NOT EXISTS "
        "FOR (n:OpenRegisterFinding) REQUIRE n.finding_id IS UNIQUE",
        "CREATE CONSTRAINT openregister_fact_id IF NOT EXISTS "
        "FOR (n:OpenRegisterFact) REQUIRE n.fact_id IS UNIQUE",
        "CREATE CONSTRAINT openregister_source_id IF NOT EXISTS "
        "FOR (n:OpenRegisterSourceDocument) REQUIRE n.source_id IS UNIQUE",
        "CREATE INDEX openregister_entity_external_id IF NOT EXISTS "
        "FOR (n:OpenRegisterEntity) ON (n.external_id)",
        "CREATE INDEX openregister_entity_name IF NOT EXISTS "
        "FOR (n:OpenRegisterEntity) ON (n.canonical_name)",
        "CREATE INDEX openregister_finding_state IF NOT EXISTS "
        "FOR (n:OpenRegisterFinding) ON (n.selection_status)",
    )

    def __init__(self, cfg: dict[str, Any], *, driver: Any | None = None):
        neo = cfg.get("neo4j") or {}
        if not _truthy(neo.get("enabled")):
            raise RuntimeError("Neo4j is disabled")
        if driver is not None:
            self.driver = driver
            self.database = str(neo.get("database") or "neo4j")
            return
        if GraphDatabase is None:
            raise RuntimeError("Neo4j support is not installed")
        uri, username, password, database = _neo4j_connection(cfg)
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database

    def close(self) -> None:
        close = getattr(self.driver, "close", None)
        if callable(close):
            close()

    def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self.driver.session(database=self.database) as session:
            return [record.data() for record in session.run(query, **params)]

    def ensure_schema(self) -> None:
        for statement in self.SCHEMA:
            self._run(statement)

    @staticmethod
    def _entity_key(candidate_id: str, *, kind: str = "", name: str = "", payload: Any = None) -> str:
        stable = _clean(candidate_id, 300)
        if stable:
            return f"openregister:{stable}"
        derived = _hash(_kind(kind), _clean(name, 300).casefold(), _json(payload or {}))
        return f"openregister:derived:{derived[:40]}"

    def _upsert_entity(
        self,
        *,
        candidate_id: str,
        kind: str,
        name: str,
        details: dict[str, Any] | None = None,
        authoritative_details: bool = False,
    ) -> str:
        details = dict(details or {})
        external_key = self._entity_key(candidate_id, kind=kind, name=name, payload=details)
        canonical = _display_name(details, name) or _clean(name, 300)
        status = _clean(details.get("status"), 80)
        if not status and details.get("active") is not None:
            status = "active" if bool(details.get("active")) else "inactive"
        legal_form_raw = details.get("legal_form")
        if isinstance(legal_form_raw, dict):
            legal_form = _clean(
                legal_form_raw.get("name")
                or legal_form_raw.get("code")
                or legal_form_raw.get("value"),
                120,
            )
        else:
            legal_form = _clean(legal_form_raw, 120)
        register = details.get("company_register") or details.get("register") or {}
        register = register if isinstance(register, dict) else {}
        address_value = details.get("address") or {}
        address = address_value if isinstance(address_value, dict) else {}
        formatted_address = _clean(
            address.get("formatted_value")
            or address.get("formatted")
            or address.get("value")
            or (address_value if isinstance(address_value, str) else ""),
            500,
        )
        self._run(
            """
            MERGE (e:OpenRegisterEntity {external_key:$external_key})
            ON CREATE SET e.created_at=datetime()
            SET e.source='openregister',
                e.external_id=CASE WHEN $external_id<>'' THEN $external_id ELSE e.external_id END,
                e.kind=CASE WHEN $kind<>'' THEN $kind ELSE e.kind END,
                e.canonical_name=CASE WHEN $canonical_name<>'' THEN $canonical_name ELSE e.canonical_name END,
                e.status=CASE WHEN $status<>'' THEN $status ELSE e.status END,
                e.legal_form=CASE WHEN $legal_form<>'' THEN $legal_form ELSE e.legal_form END,
                e.register_court=CASE WHEN $register_court<>'' THEN $register_court ELSE e.register_court END,
                e.register_number=CASE WHEN $register_number<>'' THEN $register_number ELSE e.register_number END,
                e.address=CASE WHEN $address<>'' THEN $address ELSE e.address END,
                e.details_json=CASE
                    WHEN $authoritative_details OR coalesce(e.details_json,'')=''
                    THEN $details_json
                    ELSE e.details_json
                END,
                e.last_seen_at=datetime()
            """,
            external_key=external_key,
            external_id=_clean(candidate_id, 300),
            kind=_kind(kind),
            canonical_name=canonical,
            status=status,
            legal_form=legal_form,
            register_court=_clean(register.get("register_court") or details.get("register_court"), 200),
            register_number=_clean(register.get("register_number") or details.get("register_number"), 120),
            address=formatted_address,
            details_json=_json(details) if details else "",
            authoritative_details=bool(authoritative_details),
        )
        label = _kind(kind)
        if label == "Organization":
            self._run(
                "MATCH (e:OpenRegisterEntity {external_key:$key}) SET e:OpenRegisterOrganization",
                key=external_key,
            )
        elif label == "Person":
            self._run(
                "MATCH (e:OpenRegisterEntity {external_key:$key}) SET e:OpenRegisterPerson",
                key=external_key,
            )
        return external_key

    def _finding(
        self,
        *,
        run_id: str,
        reference: str,
        result_status: str,
        selection_status: str,
        rank: int,
        candidate: dict[str, Any],
        entity_key: str,
    ) -> str:
        candidate_id = _clean(candidate.get("candidate_id"), 300)
        finding_id = _hash("openregister", run_id, reference, candidate_id, rank)
        self._run(
            """
            MERGE (f:ExternalFinding:OpenRegisterFinding {finding_id:$finding_id})
            ON CREATE SET f.created_at=datetime()
            SET f.provenance_code='openregister',
                f.provenance_label='OpenRegister',
                f.curation_mode='external',
                f.finding_type='resolver_candidate',
                f.reference=$reference,
                f.resolver_status=$resolver_status,
                f.selection_status=$selection_status,
                f.selection_label=$selection_label,
                f.rank=$rank,
                f.candidate_id=$candidate_id,
                f.canonical_name=$canonical_name,
                f.kind=$kind,
                f.match=$match,
                f.confidence=$confidence,
                f.payload_json=$payload_json,
                f.last_seen_at=datetime()
            WITH f
            MATCH (r:OpenRegisterRun {run_id:$run_id})
            MERGE (r)-[:PRODUCED_EXTERNAL]->(f)
            WITH f
            MATCH (e:OpenRegisterEntity {external_key:$entity_key})
            MERGE (f)-[:DESCRIBES]->(e)
            """,
            finding_id=finding_id,
            run_id=run_id,
            entity_key=entity_key,
            reference=_clean(reference, 500),
            resolver_status=_clean(result_status, 80),
            selection_status=selection_status,
            selection_label=_candidate_label(selection_status),
            rank=int(rank),
            candidate_id=candidate_id,
            canonical_name=_clean(candidate.get("canonical"), 300),
            kind=_kind(candidate.get("kind")),
            match=_clean(candidate.get("match"), 100),
            confidence=float(candidate.get("confidence") or 0.0),
            payload_json=_json(candidate),
        )
        return finding_id

    def _fact(self, *, entity_key: str, finding_id: str, field: str, payload: Any) -> None:
        fact_json = _json(payload)
        fact_id = _hash("openregister_fact", entity_key, field, fact_json)
        self._run(
            """
            MERGE (fact:OpenRegisterFact {fact_id:$fact_id})
            ON CREATE SET fact.created_at=datetime()
            SET fact.source='openregister',
                fact.fact_type=$fact_type,
                fact.payload_json=$payload_json,
                fact.last_seen_at=datetime()
            WITH fact
            MATCH (e:OpenRegisterEntity {external_key:$entity_key})
            MERGE (e)-[:HAS_EXTERNAL_FACT]->(fact)
            WITH fact
            MATCH (f:OpenRegisterFinding {finding_id:$finding_id})
            MERGE (fact)-[:DERIVED_FROM]->(f)
            """,
            fact_id=fact_id,
            fact_type=_clean(field, 160),
            payload_json=fact_json,
            entity_key=entity_key,
            finding_id=finding_id,
        )

    def _role(
        self,
        *,
        subject_key: str,
        organization_key: str,
        finding_id: str,
        role: str,
        start_date: Any = None,
        end_date: Any = None,
        authority: Any = None,
        payload: Any = None,
    ) -> None:
        role_id = _hash(
            "openregister_role",
            subject_key,
            organization_key,
            role,
            start_date,
            end_date,
            _json(payload or {}),
        )
        self._run(
            """
            MATCH (subject:OpenRegisterEntity {external_key:$subject_key})
            MATCH (org:OpenRegisterEntity {external_key:$organization_key})
            MERGE (subject)-[rel:OPENREGISTER_ROLE {role_id:$role_id}]->(org)
            ON CREATE SET rel.created_at=datetime()
            SET rel.source='openregister',
                rel.role=$role,
                rel.valid_from=$valid_from,
                rel.valid_to=$valid_to,
                rel.current=$current,
                rel.authority=$authority,
                rel.provenance_finding_id=$finding_id,
                rel.payload_json=$payload_json,
                rel.last_seen_at=datetime()
            """,
            subject_key=subject_key,
            organization_key=organization_key,
            role_id=role_id,
            role=_clean(role, 160),
            valid_from=_clean(start_date, 80),
            valid_to=_clean(end_date, 80),
            current=not bool(_clean(end_date, 80)),
            authority=_clean(authority, 1000),
            finding_id=finding_id,
            payload_json=_json(payload or {}),
        )

    def _company_roles(
        self,
        *,
        organization_key: str,
        finding_id: str,
        details: dict[str, Any],
    ) -> None:
        buckets: list[Any] = []
        for field, value in details.items():
            if not isinstance(value, list):
                continue
            # Preserve semantic role edges independent of the provider's exact
            # history field name. Only records carrying an explicit role are
            # eligible; all other list structures remain OpenRegisterFact nodes.
            for item in value:
                if isinstance(item, dict) and _clean(item.get("role"), 160):
                    buckets.append(item)
        seen: set[str] = set()
        for raw in buckets:
            item = raw if isinstance(raw, dict) else {}
            if not item:
                continue
            payload_key = _json(item)
            if payload_key in seen:
                continue
            seen.add(payload_key)
            natural = item.get("natural_person") if isinstance(item.get("natural_person"), dict) else {}
            legal = item.get("legal_person") if isinstance(item.get("legal_person"), dict) else {}
            type_hint = _clean(item.get("type"), 80).casefold()
            is_person = bool(natural) or "natural" in type_hint or type_hint == "person"
            actor_kind = "Person" if is_person else "Organization"
            name = _clean(item.get("name"), 300)
            if not name and natural:
                name = _clean(
                    " ".join(
                        x for x in (
                            _clean(natural.get("first_name"), 150),
                            _clean(natural.get("last_name"), 150),
                        ) if x
                    ),
                    300,
                )
            if not name and legal:
                name = _clean(legal.get("name"), 300)
            actor_details = {
                "name": name,
                "city": (natural or legal).get("city"),
                "date_of_birth": natural.get("date_of_birth") if natural else None,
            }
            actor_key = self._upsert_entity(
                candidate_id=_clean(item.get("id"), 300),
                kind=actor_kind,
                name=name,
                details=actor_details,
                authoritative_details=True,
            )
            self._role(
                subject_key=actor_key,
                organization_key=organization_key,
                finding_id=finding_id,
                role=_clean(item.get("role"), 160),
                start_date=item.get("start_date"),
                end_date=item.get("end_date"),
                authority=item.get("authority"),
                payload=item,
            )

    def _person_positions(
        self,
        *,
        person_key: str,
        finding_id: str,
        details: dict[str, Any],
    ) -> None:
        positions = details.get("management_positions") or []
        if not isinstance(positions, list):
            return
        for raw in positions:
            item = raw if isinstance(raw, dict) else {}
            if not item:
                continue
            company_name = _clean(item.get("company_name") or item.get("name"), 300)
            organization_key = self._upsert_entity(
                candidate_id=_clean(item.get("register_id") or item.get("company_id"), 300),
                kind="Organization",
                name=company_name,
                details={"name": company_name},
            )
            self._role(
                subject_key=person_key,
                organization_key=organization_key,
                finding_id=finding_id,
                role=_clean(item.get("role"), 160),
                start_date=item.get("start_date"),
                end_date=item.get("end_date"),
                authority=item.get("authority"),
                payload=item,
            )

    def persist(
        self,
        *,
        run_id: str,
        resolver_response: dict[str, Any] | None,
        enrichments: list[dict[str, Any]] | None = None,
        documents: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        self.ensure_schema()
        run_key = _clean(run_id, 300)
        self._run(
            """
            MERGE (r:OpenRegisterRun {run_id:$run_id})
            ON CREATE SET r.created_at=datetime()
            SET r.source='openregister',
                r.last_seen_at=datetime()
            """,
            run_id=run_key,
        )

        finding_by_candidate: dict[str, str] = {}
        entity_by_candidate: dict[str, str] = {}
        entity_by_name: dict[str, str] = {}
        counts = {"entities": 0, "findings": 0, "facts": 0, "documents": 0}

        for result in (resolver_response or {}).get("results") or []:
            if not isinstance(result, dict):
                continue
            reference = _clean(result.get("reference"), 500)
            result_status = _clean(result.get("status"), 80)
            for rank, candidate in enumerate(
                [c for c in (result.get("candidates") or []) if isinstance(c, dict)],
                start=1,
            ):
                candidate_id = _clean(candidate.get("candidate_id"), 300)
                name = _clean(candidate.get("canonical"), 300)
                key = self._upsert_entity(
                    candidate_id=candidate_id,
                    kind=_kind(candidate.get("kind")),
                    name=name,
                    details={
                        "name": name,
                        **(candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}),
                    },
                )
                state = _candidate_state(result_status, rank)
                finding_id = self._finding(
                    run_id=run_key,
                    reference=reference,
                    result_status=result_status,
                    selection_status=state,
                    rank=rank,
                    candidate=candidate,
                    entity_key=key,
                )
                finding_by_candidate[candidate_id] = finding_id
                entity_by_candidate[candidate_id] = key
                if name:
                    entity_by_name[name.casefold()] = key
                counts["entities"] += 1
                counts["findings"] += 1

        for enrichment in enrichments or []:
            if not isinstance(enrichment, dict):
                continue
            candidate = enrichment.get("candidate") if isinstance(enrichment.get("candidate"), dict) else {}
            response = enrichment.get("response") if isinstance(enrichment.get("response"), dict) else {}
            details = response.get("details") if isinstance(response.get("details"), dict) else {}
            if not details:
                continue
            candidate_id = _clean(candidate.get("candidate_id") or response.get("candidate_id"), 300)
            name = _clean(candidate.get("canonical"), 300) or _display_name(details)
            kind = _kind(candidate.get("kind") or response.get("kind"))
            entity_key = self._upsert_entity(
                candidate_id=candidate_id,
                kind=kind,
                name=name,
                details=details,
                authoritative_details=True,
            )
            entity_by_candidate[candidate_id] = entity_key
            if name:
                entity_by_name[name.casefold()] = entity_key
            finding_id = finding_by_candidate.get(candidate_id)
            if not finding_id:
                finding_id = _hash("openregister", run_key, candidate_id, "enrichment")
                self._run(
                    """
                    MERGE (f:ExternalFinding:OpenRegisterFinding {finding_id:$finding_id})
                    ON CREATE SET f.created_at=datetime()
                    SET f.provenance_code='openregister',
                        f.provenance_label='OpenRegister',
                        f.curation_mode='external',
                        f.finding_type='entity_enrichment',
                        f.selection_status='selected',
                        f.candidate_id=$candidate_id,
                        f.canonical_name=$canonical_name,
                        f.kind=$kind,
                        f.details_json=$details_json,
                        f.last_seen_at=datetime()
                    WITH f
                    MATCH (r:OpenRegisterRun {run_id:$run_id})
                    MERGE (r)-[:PRODUCED_EXTERNAL]->(f)
                    WITH f
                    MATCH (e:OpenRegisterEntity {external_key:$entity_key})
                    MERGE (f)-[:DESCRIBES]->(e)
                    """,
                    finding_id=finding_id,
                    candidate_id=candidate_id,
                    canonical_name=name,
                    kind=kind,
                    details_json=_json(details),
                    run_id=run_key,
                    entity_key=entity_key,
                )
                finding_by_candidate[candidate_id] = finding_id
                counts["findings"] += 1
            else:
                self._run(
                    """
                    MATCH (f:OpenRegisterFinding {finding_id:$finding_id})
                    SET f.enriched=true,
                        f.finding_type='entity_enrichment',
                        f.details_json=$details_json,
                        f.last_seen_at=datetime()
                    """,
                    finding_id=finding_id,
                    details_json=_json(details),
                )

            for field, value in details.items():
                if not isinstance(value, list):
                    continue
                for item in value:
                    self._fact(
                        entity_key=entity_key,
                        finding_id=finding_id,
                        field=field,
                        payload=item,
                    )
                    counts["facts"] += 1

            if kind == "Organization":
                self._company_roles(
                    organization_key=entity_key,
                    finding_id=finding_id,
                    details=details,
                )
            elif kind == "Person":
                self._person_positions(
                    person_key=entity_key,
                    finding_id=finding_id,
                    details=details,
                )

        for record in documents or []:
            if not isinstance(record, dict) or _clean(record.get("status"), 80) != "archived":
                continue
            response = record.get("response") if isinstance(record.get("response"), dict) else {}
            candidate_id = _clean(record.get("candidate_id"), 300)
            entity_key = entity_by_candidate.get(candidate_id)
            if not entity_key:
                entity_key = entity_by_name.get(_clean(record.get("entity"), 300).casefold())
            if not entity_key:
                continue
            source_id = _clean(response.get("sha256"), 128) or _hash(
                "openregister_source",
                response.get("path"),
                candidate_id,
            )
            self._run(
                """
                MERGE (d:OpenRegisterSourceDocument {source_id:$source_id})
                ON CREATE SET d.created_at=datetime()
                SET d.source='openregister',
                    d.candidate_id=$candidate_id,
                    d.category=$category,
                    d.file_name=$file_name,
                    d.sha256=$sha256,
                    d.archived=true,
                    d.last_seen_at=datetime()
                WITH d
                MATCH (e:OpenRegisterEntity {external_key:$entity_key})
                MERGE (e)-[:HAS_SOURCE_DOCUMENT]->(d)
                """,
                source_id=source_id,
                candidate_id=candidate_id,
                category=_clean(record.get("category"), 120),
                file_name=_clean(response.get("file_name"), 300),
                sha256=_clean(response.get("sha256"), 128),
                entity_key=entity_key,
            )
            finding_id = finding_by_candidate.get(candidate_id)
            if finding_id:
                self._run(
                    """
                    MATCH (f:OpenRegisterFinding {finding_id:$finding_id})
                    MATCH (d:OpenRegisterSourceDocument {source_id:$source_id})
                    MERGE (f)-[support:SUPPORTED_BY]->(d)
                    SET support.nextcloud_file_id=$nextcloud_file_id,
                        support.archived=true,
                        support.last_seen_at=datetime()
                    """,
                    finding_id=finding_id,
                    source_id=source_id,
                    nextcloud_file_id=_clean(response.get("file_id"), 120),
                )
            counts["documents"] += 1
        return counts


def persist_openregister_research(
    cfg: dict[str, Any],
    *,
    run_id: str,
    resolver_response: dict[str, Any] | None,
    enrichments: list[dict[str, Any]] | None = None,
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Fail-open caller owns exception handling; this helper always closes the driver."""
    store = OpenRegisterGraphStore(cfg)
    try:
        return store.persist(
            run_id=run_id,
            resolver_response=resolver_response,
            enrichments=enrichments,
            documents=documents,
        )
    finally:
        store.close()
