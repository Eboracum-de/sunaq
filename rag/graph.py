#!/usr/bin/env python3
"""Neo4j support for the Nextcloud Hybrid RAG middleware.

Phase 1 deliberately concentrates on the identity graph:
- CardDAV contact records are sources, not identities.
- Person/Organization nodes receive stable UUIDs once created.
- Names are separate nodes and may point to more than one entity.
- Generated search aliases are separate from real names.
- Contact-derived values keep provenance and active/inactive history.

v0.6.5 adds identity-form semantics and generic-name admission control:
- names/aliases carry a resolution_policy (exclusive/contextual/search_only/document_only);
- query resolution may use recall-oriented forms while ingestion only hard-resolves exclusive forms;
- manual merges default inherited spellings to contextual unless explicitly promoted;
- generic institutional class names can be deterministically blocked before Entity creation.
v0.6.6a adds explicit CardDAV provenance and reversible contact-source imports:
- ContactRecord carries cloud/user/address-book/import-run provenance;
- contact imports are audited as ContactImportRun nodes;
- whole sources can be previewed and rolled back without touching manual identity decisions;
- affected documents can be queued for deterministic no-LLM relinking.
Graph v3 adds document-grounded Claim/RelationObservation nodes. These are retrieval signals with source passages, never unqualified global fact edges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from rag.research_findings import (
    PROVENANCE_CODE,
    PROVENANCE_LABEL,
    canonical_query_frame,
    curation_frame_hash,
    evidence_entity_candidates,
    merge_entity_candidates,
    finding_id as research_finding_id,
    frame_relation_texts,
    query_frame_hash,
    query_frame_has_structure,
)
from urllib.parse import unquote, urlparse

import yaml

from rag.secret_env import secret_env
try:
    from neo4j import GraphDatabase
except ImportError:  # optional when Neo4j is disabled in a lite deployment
    GraphDatabase = None
from rapidfuzz import fuzz

from rag.ontology import (
    compatible_document_predicates,
    entity_type_from_labels,
    load_relation_ontology,
    predicate_label as ontology_predicate_label,
    relation_names_with_role,
)

BASE_DIR = Path(__file__).resolve().parent.parent

_RELATION_ONTOLOGY = load_relation_ontology()
RETRIEVAL_BRIDGE_RELATIONS = relation_names_with_role(_RELATION_ONTOLOGY, "retrieval_bridge")
IDENTITY_CONTEXT_RELATIONS = relation_names_with_role(_RELATION_ONTOLOGY, "identity_context")
IDENTITY_MERGE_RELATIONS = relation_names_with_role(_RELATION_ONTOLOGY, "identity_merge")
PERSON_AFFILIATION_RELATIONS = relation_names_with_role(_RELATION_ONTOLOGY, "person_affiliation")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be a mapping")
    from rag.tls_compat import configure_tls_compat
    configure_tls_compat(cfg)
    return cfg


def cfg_get(cfg: dict[str, Any], *paths: str, default=None):
    for path in paths:
        cur: Any = cfg
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            return cur
    return default


def env_or_value(cfg: dict[str, Any], value_path: str, env_path: str, *, default: str = "") -> str:
    env_name = str(cfg_get(cfg, env_path, default="") or "").strip()
    if env_name:
        value = secret_env(env_name, "")
        if value:
            return value
    return str(cfg_get(cfg, value_path, default=default) or default)


def normalize_name(value: str) -> str:
    """Conservative text normalization for identity matching.

    We intentionally do not transliterate umlauts (Müller != Mueller) and do
    not reorder names. Punctuation is treated as whitespace so e.g. "D. Wahl"
    and "D Wahl" share a search key.
    """
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^0-9a-zäöüßà-ž]+", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


FORM_RESOLUTION_POLICIES = {"exclusive", "contextual", "search_only", "document_only"}

# Exact generic class labels that must never become standalone identities.
# Qualified names remain valid: "Finanzamt Bonn" != "Finanzamt".
DEFAULT_BLOCKED_GENERIC_NAMES = {
    "finanzamt",
    "amtsgericht",
    "landgericht",
    "oberlandesgericht",
    "verwaltungsgericht",
    "arbeitsgericht",
    "sozialgericht",
    "finanzgericht",
    "staatsanwaltschaft",
    "handelsregister",
    "registergericht",
    "insolvenzgericht",
    "grundbuchamt",
    "nachlassgericht",
    "zivilabteilung",
    "strafabteilung",
}


def blocked_generic_names(cfg: dict[str, Any] | None = None) -> set[str]:
    """Return normalized exact-name denylist for Entity admission.

    Config values extend the conservative defaults unless
    graph.entity_admission.use_default_blocked_generic_names=false.
    """
    cfg = cfg or {}
    use_defaults = bool(cfg_get(
        cfg, "graph.entity_admission.use_default_blocked_generic_names", default=True
    ))
    values: set[str] = set(DEFAULT_BLOCKED_GENERIC_NAMES if use_defaults else set())
    configured = cfg_get(cfg, "graph.entity_admission.blocked_generic_names", default=[]) or []
    if isinstance(configured, str):
        configured = [configured]
    for value in configured:
        norm = normalize_name(str(value or ""))
        if norm:
            values.add(norm)
    return values


def _form_policy(value: Any, *, default: str) -> str:
    policy = str(value or default).strip().lower()
    return policy if policy in FORM_RESOLUTION_POLICIES else default


def normalize_email(value: str) -> str:
    return str(value or "").strip().casefold()


def normalize_phone(value: str) -> str:
    """Syntax-only phone normalization.

    This does not guess a country. +49... and 0... therefore remain different
    keys. Country-aware E.164 normalization can be added later without changing
    the graph schema.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    lead_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if lead_plus else "") + digits


def normalize_address(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", value).strip()


def stable_contact_id(addressbook_href: str, vcard_uid: str, href: str) -> str:
    book_key = hashlib.sha256(addressbook_href.encode("utf-8")).hexdigest()[:12]
    uid = str(vcard_uid or "").strip()
    if not uid:
        uid = hashlib.sha256(href.encode("utf-8")).hexdigest()[:24]
    return f"carddav:{book_key}:{uid}"


def _compact_unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def generated_person_aliases(given: str, family: str, additional: str = "") -> list[dict[str, Any]]:
    """Create *search* aliases, not additional real names.

    Additional/middle given names are treated as compatible name extensions.
    This covers both vCard encodings ``N:Muster;Robert;Alexander;;`` and
    ``N:Muster;Robert Alexander;;;``.  Surname-only aliases are deliberately
    not generated because they are too ambiguous.
    """
    given = str(given or "").strip()
    family = str(family or "").strip()
    additional = str(additional or "").strip()
    aliases: list[dict[str, Any]] = []

    if given and family:
        given_parts = [p for p in given.split() if p]
        primary_given = given_parts[0] if given_parts else given
        embedded_middle = " ".join(given_parts[1:])
        middle = " ".join(x for x in (embedded_middle, additional) if x).strip()

        aliases.append({
            "value": f"{family}, {given}",
            "kind": "family_given",
            "weight": 0.90,
            "resolution_policy": "contextual",
        })
        aliases.append({
            "value": f"{primary_given[0]}. {family}",
            "kind": "initial_family",
            "weight": 0.62,
            "resolution_policy": "search_only",
        })

        if middle or primary_given != given:
            # A middle/additional given name does not make the shorter form a
            # different identity: Max Alexander Mustermann <-> Max Mustermann.
            aliases.append({
                "value": f"{primary_given} {family}",
                "kind": "without_additional_given_names",
                "weight": 0.96,
                "resolution_policy": "contextual",
            })
            if middle:
                aliases.append({
                    "value": f"{primary_given} {middle} {family}",
                    "kind": "full_with_additional",
                    "weight": 0.98,
                    "resolution_policy": "contextual",
                })
                aliases.append({
                    "value": f"{primary_given} {middle[0]}. {family}",
                    "kind": "middle_initial",
                    "weight": 0.86,
                    "resolution_policy": "search_only",
                })

    dedup: dict[str, dict[str, Any]] = {}
    for item in aliases:
        norm = normalize_name(item["value"])
        if norm:
            item = dict(item)
            item["normalized"] = norm
            old = dedup.get(norm)
            if old is None or float(item["weight"]) > float(old["weight"]):
                dedup[norm] = item
    return list(dedup.values())


LEGAL_FORM_PATTERNS = [
    # longer compound forms first
    r"gmbh\s*(?:&|und)\s*co\.?\s*kg",
    r"gmbh\s*&\s*co\.?\s*kg",
    r"ug\s*\(?haftungsbeschränkt\)?",
    r"ug\s*\(?haftungsbeschraenkt\)?",
    r"gmbh",
    r"mbh",
    r"kgaa",
    r"ag",
    r"se",
    r"kg",
    r"ohg",
    r"gbr",
    r"ug",
    r"e\.?\s*g\.?",
    r"e\.?\s*v\.?",
]


def generated_organization_aliases(name: str) -> list[dict[str, Any]]:
    """Create conservative search aliases for legal organization names.

    Real organization names remain EntityName values.  These derived forms are
    only SearchAlias nodes and therefore can carry lower retrieval weights.

    Examples:
      "Beispiel GmbH"        -> "Beispiel"
      "Musterhof 280 VV UG" -> "Musterhof 280 VV", "Musterhof 280"

    The second transformation is deliberately limited to the common ``VV``
    shelf-company marker immediately before a legal form; arbitrary trailing
    words are never removed.
    """
    raw = str(name or "").strip()
    if not raw:
        return []

    aliases: list[dict[str, Any]] = []
    current = raw
    legal_removed = False
    for pattern in LEGAL_FORM_PATTERNS:
        m = re.search(rf"(?:\s|,)+({pattern})\s*$", current, flags=re.IGNORECASE)
        if m:
            short = current[:m.start()].rstrip(" ,.-")
            if short and normalize_name(short) != normalize_name(raw):
                aliases.append({
                    "value": short,
                    "kind": "without_legal_form",
                    "weight": 0.94,
                    "resolution_policy": "contextual",
                })
                current = short
                legal_removed = True
            break

    # Common German Vorratsgesellschaft naming pattern, e.g.
    # "Musterhof 280 VV UG".  Keep this lower-weight than merely stripping UG.
    if legal_removed:
        m = re.search(r"\s+VV\s*$", current, flags=re.IGNORECASE)
        if m:
            short = current[:m.start()].rstrip(" ,.-")
            if short:
                aliases.append({
                    "value": short,
                    "kind": "without_vv_legal_suffix",
                    "weight": 0.86,
                    "resolution_policy": "contextual",
                })

    dedup: dict[str, dict[str, Any]] = {}
    full_norm = normalize_name(raw)
    for item in aliases:
        norm = normalize_name(item["value"])
        if not norm or norm == full_norm or len(norm) < 3:
            continue
        item = dict(item)
        item["normalized"] = norm
        old = dedup.get(norm)
        if old is None or float(item["weight"]) > float(old["weight"]):
            dedup[norm] = item
    return list(dedup.values())


def organization_identity_key(name: str) -> str:
    """Return a *strict* organization identity key.

    This key is deliberately much more conservative than search aliases or
    fuzzy similarity. It only removes wording that is legally/semantically
    neutral for the same German legal form. In particular, ``UG`` and
    ``UG (haftungsbeschränkt)`` share a key, while name-stem changes such as
    ``Muster Handel`` vs ``Muster Handels GmbH`` remain different.
    Punctuation differences are already neutralized by ``normalize_name``.
    """
    norm = normalize_name(name)
    if not norm:
        return ""
    norm = re.sub(r"\bug\s+haftungsbeschränkt\b", "ug", norm)
    norm = re.sub(r"\bug\s+haftungsbeschraenkt\b", "ug", norm)
    # Punctuation-only spelling variants of registered legal forms. NFKC plus
    # normalize_name turns e.G. into "e g" while eG becomes "eg".
    norm = re.sub(r"\be\s+g\b", "eg", norm)
    norm = re.sub(r"\be\s+v\b", "ev", norm)
    return re.sub(r"\s+", " ", norm).strip()


@dataclass
class Neo4jSettings:
    uri: str
    username: str
    password: str
    database: str = "neo4j"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Neo4jSettings":
        uri = str(cfg_get(cfg, "neo4j.uri", default="bolt://127.0.0.1:7687"))
        username = str(cfg_get(cfg, "neo4j.username", default="neo4j"))
        password = env_or_value(cfg, "neo4j.password", "neo4j.password_env")
        database = str(cfg_get(cfg, "neo4j.database", default="neo4j"))
        if not password:
            raise RuntimeError(
                "Neo4j-Passwort fehlt. Setze neo4j.password oder besser "
                "neo4j.password_env in config.yaml."
            )
        return cls(uri=uri, username=username, password=password, database=database)


NEO4J_BASE_CONSTRAINTS = (
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.entity_id IS UNIQUE",
    "CREATE CONSTRAINT contact_id IF NOT EXISTS FOR (n:ContactRecord) REQUIRE n.contact_id IS UNIQUE",
    "CREATE CONSTRAINT contact_import_run_id IF NOT EXISTS FOR (n:ContactImportRun) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT entity_name_normalized IF NOT EXISTS FOR (n:EntityName) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT search_alias_normalized IF NOT EXISTS FOR (n:SearchAlias) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT entity_form_decision_normalized IF NOT EXISTS FOR (n:EntityFormDecision) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT email_normalized IF NOT EXISTS FOR (n:EmailAddress) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT phone_normalized IF NOT EXISTS FOR (n:PhoneNumber) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT address_normalized IF NOT EXISTS FOR (n:PostalAddress) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT org_unit_key IF NOT EXISTS FOR (n:OrganizationalUnit) REQUIRE n.unit_key IS UNIQUE",
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (n:Document) REQUIRE n.document_id IS UNIQUE",
    "CREATE CONSTRAINT mention_name_normalized IF NOT EXISTS FOR (n:MentionName) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT entity_observation_id IF NOT EXISTS FOR (n:EntityObservation) REQUIRE n.observation_id IS UNIQUE",
    "CREATE CONSTRAINT relation_observation_id IF NOT EXISTS FOR (n:RelationObservation) REQUIRE n.relation_id IS UNIQUE",
    "CREATE CONSTRAINT mail_message_key IF NOT EXISTS FOR (n:MailMessage) REQUIRE n.mail_key IS UNIQUE",
)
NEO4J_RESEARCH_CONSTRAINTS = (
    "CREATE CONSTRAINT research_finding_id IF NOT EXISTS FOR (n:ResearchFinding) REQUIRE n.finding_id IS UNIQUE",
    "CREATE CONSTRAINT research_run_id IF NOT EXISTS FOR (n:ResearchRun) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT canonical_user_id IF NOT EXISTS FOR (n:CanonicalUser) REQUIRE n.canonical_user_id IS UNIQUE",
)
NEO4J_SCHEMA_CONSTRAINTS = NEO4J_BASE_CONSTRAINTS + NEO4J_RESEARCH_CONSTRAINTS

NEO4J_BASE_INDEXES = (
    "CREATE INDEX entity_display_name IF NOT EXISTS FOR (n:Entity) ON (n.display_name)",
    "CREATE INDEX entity_identity_key IF NOT EXISTS FOR (n:Entity) ON (n.identity_key)",
    "CREATE INDEX org_unit_display_name IF NOT EXISTS FOR (n:OrganizationalUnit) ON (n.display_name)",
    "CREATE INDEX contact_uid IF NOT EXISTS FOR (n:ContactRecord) ON (n.vcard_uid)",
    "CREATE INDEX contact_cloud IF NOT EXISTS FOR (n:ContactRecord) ON (n.cloud_id)",
    "CREATE INDEX contact_source_user IF NOT EXISTS FOR (n:ContactRecord) ON (n.source_user_id)",
    "CREATE INDEX contact_addressbook_name IF NOT EXISTS FOR (n:ContactRecord) ON (n.addressbook_name)",
    "CREATE INDEX contact_addressbook_slug IF NOT EXISTS FOR (n:ContactRecord) ON (n.addressbook_slug)",
    "CREATE INDEX contact_created_import_run IF NOT EXISTS FOR (n:ContactRecord) ON (n.created_import_run_id)",
    "CREATE INDEX contact_last_import_run IF NOT EXISTS FOR (n:ContactRecord) ON (n.last_import_run_id)",
    "CREATE INDEX entity_observation_document IF NOT EXISTS FOR (n:EntityObservation) ON (n.document_id)",
    "CREATE INDEX entity_observation_normalized IF NOT EXISTS FOR (n:EntityObservation) ON (n.normalized)",
    "CREATE INDEX entity_observation_status IF NOT EXISTS FOR (n:EntityObservation) ON (n.status)",
    "CREATE INDEX entity_observation_curator_status IF NOT EXISTS FOR (n:EntityObservation) ON (n.curator_status)",
    "CREATE INDEX relation_observation_document IF NOT EXISTS FOR (n:RelationObservation) ON (n.document_id)",
    "CREATE INDEX relation_observation_predicate IF NOT EXISTS FOR (n:RelationObservation) ON (n.predicate)",
    "CREATE INDEX relation_observation_stance IF NOT EXISTS FOR (n:RelationObservation) ON (n.stance)",
    "CREATE INDEX relation_observation_curator_status IF NOT EXISTS FOR (n:RelationObservation) ON (n.curator_status)",
)
NEO4J_RESEARCH_INDEXES = (
    "CREATE INDEX research_finding_frame_hash IF NOT EXISTS FOR (n:ResearchFinding) ON (n.frame_hash)",
    "CREATE INDEX research_finding_curation_hash IF NOT EXISTS FOR (n:ResearchFinding) ON (n.curation_hash)",
    "CREATE INDEX research_finding_provenance IF NOT EXISTS FOR (n:ResearchFinding) ON (n.provenance_code)",
    "CREATE INDEX research_run_user IF NOT EXISTS FOR (n:ResearchRun) ON (n.canonical_user_id)",
    "CREATE INDEX research_run_status IF NOT EXISTS FOR (n:ResearchRun) ON (n.curation_status)",
)
NEO4J_SCHEMA_INDEXES = NEO4J_BASE_INDEXES + NEO4J_RESEARCH_INDEXES


class GraphStore:
    def __init__(self, settings: Neo4jSettings):
        if GraphDatabase is None:
            raise RuntimeError("Neo4j support is not installed (missing 'neo4j' Python package)")
        self.settings = settings
        self.driver = GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, settings.password),
        )

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "GraphStore":
        return cls(Neo4jSettings.from_config(cfg))

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def _run(self, query: str, **params) -> list[dict[str, Any]]:
        with self.driver.session(database=self.settings.database) as session:
            result = session.run(query, **params)
            return [record.data() for record in result]

    def ensure_schema(self) -> None:
        """Create and non-destructively upgrade the complete AKI Neo4j schema.

        Constraints and indexes use IF NOT EXISTS, so this is safe for fresh,
        complete, and partially initialized databases. Optional properties are
        read through properties(node_or_relationship)[key]; no synthetic nodes
        or dummy properties are created merely to register property tokens.
        """
        for statement in NEO4J_BASE_CONSTRAINTS + NEO4J_BASE_INDEXES:
            self._run(statement)

        # Preserve the latest historic manual decision for a normalized form.
        # ON CREATE keeps subsequent explicit global decisions authoritative.
        self._run(
            """
            MATCH (o:EntityObservation)
            WHERE coalesce(properties(o)['curator_status'],'') IN
                  ['manual_not_entity','corrected_observation','research_finding_entity']
              AND coalesce(properties(o)['normalized'],'') <> ''
            WITH properties(o)['normalized'] AS normalized, o
            ORDER BY properties(o)['curator_decided_at'] DESC,
                     properties(o)['updated_at'] DESC
            WITH normalized, collect(o)[0] AS latest
            MERGE (d:EntityFormDecision {normalized:normalized})
            ON CREATE SET
                d.value=coalesce(properties(latest)['observed_text'],
                                 properties(latest)['canonical_name'],normalized),
                d.status=CASE WHEN properties(latest)['curator_status']='manual_not_entity'
                              THEN 'not_entity' ELSE 'entity' END,
                d.target_entity_id=coalesce(properties(latest)['curator_target_entity_id'],''),
                d.decision_kind='historic_observation',
                d.reason='historic_observation_backfill',
                d.decided_at=coalesce(properties(latest)['curator_decided_at'],
                                      properties(latest)['updated_at'],datetime()),
                d.created_at=datetime(),
                d.updated_at=datetime()
            """
        )

        self.ensure_entity_kind_schema()
        self.ensure_research_finding_schema()

    def ensure_entity_kind_schema(self) -> int:
        """Backfill the stable Entity.entity_kind contract without reclassifying."""
        rows = self._run(
            """
            MATCH (e:Entity)
            WHERE properties(e)['entity_kind'] IS NULL
            SET e.entity_kind = CASE WHEN e:Person THEN 'Person' ELSE '' END
            RETURN count(e) AS updated
            """
        )
        return int(rows[0].get("updated") or 0) if rows else 0

    def ensure_research_finding_schema(self) -> None:
        """Create and upgrade the lightweight Research Finding schema."""
        for statement in NEO4J_RESEARCH_CONSTRAINTS + NEO4J_RESEARCH_INDEXES:
            self._run(statement)

        # Backfill the structured curation fingerprint without changing existing
        # Finding IDs. This preserves upgrade compatibility while allowing later
        # provider runs with different free-form intent wording to reuse the
        # same shared curation object.
        legacy_rows = self._run(
            """
            MATCH (f:ResearchFinding)
            WHERE coalesce(properties(f)['curation_hash'],'')=''
              AND coalesce(properties(f)['query_frame_json'],'') <> ''
            RETURN f.finding_id AS finding_id,
                   properties(f)['query_frame_json'] AS query_frame_json
            """
        )
        for row in legacy_rows:
            try:
                parsed = json.loads(str(row.get("query_frame_json") or "{}"))
                chash = curation_frame_hash(parsed)
            except Exception:
                continue
            self._run(
                """
                MATCH (f:ResearchFinding {finding_id:$finding_id})
                SET f.curation_hash=$curation_hash, f.updated_at=datetime()
                """,
                finding_id=str(row.get("finding_id") or ""),
                curation_hash=chash,
            )


    def backfill_identity_keys(self) -> int:
        """Backfill strict organization identity keys without merging anything."""
        rows = self._run(
            """
            MATCH (e:Entity:Organization)
            RETURN properties(e)['entity_id'] AS entity_id, properties(e)['display_name'] AS display_name
            """
        )
        changed = 0
        for row in rows:
            entity_id = str(row.get("entity_id") or "")
            key = organization_identity_key(str(row.get("display_name") or ""))
            if not entity_id or not key:
                continue
            self._run(
                "MATCH (e:Entity {entity_id:$entity_id}) SET e.identity_key=$key, e.updated_at=datetime()",
                entity_id=entity_id,
                key=key,
            )
            changed += 1
        return changed

    def backfill_manual_confirmations(self) -> int:
        """Confirm survivors of manual merges created by pre-0.6.4 curation."""
        rows = self._run(
            """
            MATCH (old:Entity)-[r]->(keep:Entity)
            WHERE type(r)='MERGED_INTO'
              AND coalesce(properties(r)['method'],'')='manual_curator'
              AND coalesce(properties(keep)['identity_status'],'') <> 'merged' AND coalesce(properties(keep)['identity_status'],'') <> 'orphaned'
            SET keep.identity_status='confirmed',
                keep.confirmation_method=coalesce(properties(keep)['confirmation_method'],'manual_curator_merge'),
                keep.confirmed_at=coalesce(properties(keep)['confirmed_at'],datetime()),
                keep.updated_at=datetime()
            RETURN count(DISTINCT keep) AS count
            """
        )
        return int(rows[0].get("count") or 0) if rows else 0

    # ------------------------------------------------------------------
    # Entity resolution for CardDAV ingestion
    # ------------------------------------------------------------------

    def backfill_form_policies(self) -> dict[str, int]:
        """Assign conservative policies to legacy name/alias relationships.

        Canonical/real names are identity evidence. Derived aliases and names
        inherited through earlier manual merges are contextual by default.
        No Entity is merged or deleted.
        """
        name_rows = self._run(
            """
            MATCH (:Entity)-[r]->(:EntityName)
            WHERE type(r)='HAS_NAME'
              AND (properties(r)['resolution_policy'] IS NULL OR trim(properties(r)['resolution_policy'])='')
            SET r.resolution_policy=CASE
                WHEN coalesce(properties(r)['kind'],'') IN ['merged_name'] THEN 'contextual'
                ELSE 'exclusive' END,
                r.updated_at=datetime()
            RETURN count(r) AS count
            """
        )
        alias_rows = self._run(
            """
            MATCH (:Entity)-[r]->(:SearchAlias)
            WHERE type(r)='HAS_SEARCH_ALIAS'
              AND (properties(r)['resolution_policy'] IS NULL OR trim(properties(r)['resolution_policy'])='')
            SET r.resolution_policy=CASE
                WHEN coalesce(properties(r)['kind'],'') IN ['merged_alias','merged_display_name'] THEN 'contextual'
                ELSE 'contextual' END,
                r.updated_at=datetime()
            RETURN count(r) AS count
            """
        )
        return {
            "names_updated": int(name_rows[0]["count"] if name_rows else 0),
            "aliases_updated": int(alias_rows[0]["count"] if alias_rows else 0),
        }

    def entity_forms(self, entity_id: str) -> list[dict[str, Any]]:
        """List active and historical forms with their resolution semantics."""
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            OPTIONAL MATCH (e)-[rn]->(n:EntityName)
            WHERE type(rn)='HAS_NAME'
            WITH e, collect(CASE WHEN n IS NULL THEN null ELSE {
                form_kind:'name', value:coalesce(properties(n)['last_seen_value'],properties(n)['value']),
                normalized:n.normalized, relation_kind:coalesce(properties(rn)['kind'],'name'),
                active:coalesce(properties(rn)['active'],true), preferred:coalesce(properties(rn)['preferred'],false),
                resolution_policy:coalesce(properties(rn)['resolution_policy'],'exclusive')
            } END) AS names
            OPTIONAL MATCH (e)-[ra]->(a:SearchAlias)
            WHERE type(ra)='HAS_SEARCH_ALIAS'
            WITH e, names, collect(CASE WHEN a IS NULL THEN null ELSE {
                form_kind:'alias', value:coalesce(properties(a)['last_seen_value'],properties(a)['value']),
                normalized:a.normalized, relation_kind:coalesce(properties(ra)['kind'],'alias'),
                active:coalesce(properties(ra)['active'],true), preferred:false,
                resolution_policy:coalesce(properties(ra)['resolution_policy'],'contextual')
            } END) AS aliases
            RETURN e.display_name AS display_name, labels(e) AS labels,
                   [x IN names + aliases WHERE x IS NOT NULL] AS forms
            """,
            entity_id=entity_id,
        )
        if not rows:
            return []
        return list(rows[0].get("forms") or [])

    def set_form_policy_preview(self, entity_id: str, form: str, policy: str) -> dict[str, Any]:
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        policy = _form_policy(policy, default="contextual")
        norm = normalize_name(form)
        if not norm:
            raise ValueError("Form ist leer")
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            OPTIONAL MATCH (e)-[rn:HAS_NAME]->(n:EntityName {normalized:$normalized})
            OPTIONAL MATCH (e)-[ra:HAS_SEARCH_ALIAS]->(a:SearchAlias {normalized:$normalized})
            RETURN
              [x IN collect(DISTINCT CASE WHEN rn IS NULL THEN null ELSE {
                  form_kind:'name', value:coalesce(properties(n)['last_seen_value'],properties(n)['value']),
                  relation_kind:coalesce(properties(rn)['kind'],'name'),
                  current_policy:coalesce(properties(rn)['resolution_policy'],'exclusive'),
                  active:coalesce(properties(rn)['active'],true)
              } END) WHERE x IS NOT NULL] +
              [x IN collect(DISTINCT CASE WHEN ra IS NULL THEN null ELSE {
                  form_kind:'alias', value:coalesce(properties(a)['last_seen_value'],properties(a)['value']),
                  relation_kind:coalesce(properties(ra)['kind'],'alias'),
                  current_policy:coalesce(properties(ra)['resolution_policy'],'contextual'),
                  active:coalesce(properties(ra)['active'],true)
              } END) WHERE x IS NOT NULL] AS matches
            """,
            entity_id=entity_id, normalized=norm,
        )
        matches = list(rows[0].get("matches") or []) if rows else []
        if not matches:
            raise ValueError(f"Namensform nicht an Entity gefunden: {form}")
        return {
            "action": "set_form_policy", "entity": entity,
            "form": form, "normalized": norm, "policy": policy,
            "matches": matches,
            "semantics": {
                "exclusive": "query + ingestion exact identity resolution",
                "contextual": "query/candidate use only; never hard-resolve document ingestion",
                "search_only": "query expansion only; never ingestion identity evidence",
                "document_only": "not exposed as a global resolver/search form",
            },
            "note": "Ohne --yes wird nichts verändert.",
        }

    def set_form_policy(self, entity_id: str, form: str, policy: str) -> dict[str, Any]:
        preview = self.set_form_policy_preview(entity_id, form, policy)
        norm = str(preview["normalized"])
        policy = str(preview["policy"])
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            OPTIONAL MATCH (e)-[rn:HAS_NAME]->(:EntityName {normalized:$normalized})
            OPTIONAL MATCH (e)-[ra:HAS_SEARCH_ALIAS]->(:SearchAlias {normalized:$normalized})
            FOREACH (_ IN CASE WHEN rn IS NULL THEN [] ELSE [1] END |
                SET rn.resolution_policy=$policy, rn.policy_set_by='manual_curator',
                    rn.policy_set_at=datetime(), rn.updated_at=datetime())
            FOREACH (_ IN CASE WHEN ra IS NULL THEN [] ELSE [1] END |
                SET ra.resolution_policy=$policy, ra.policy_set_by='manual_curator',
                    ra.policy_set_at=datetime(), ra.updated_at=datetime())
            RETURN (CASE WHEN rn IS NULL THEN 0 ELSE 1 END) +
                   (CASE WHEN ra IS NULL THEN 0 ELSE 1 END) AS changed
            """,
            entity_id=entity_id, normalized=norm, policy=policy,
        )
        self.refresh_possible_same_as(entity_id)
        return {
            "status": "updated", "entity_id": entity_id,
            "form": str(preview["form"]), "normalized": norm,
            "policy": policy, "relationships_changed": int(rows[0]["changed"] if rows else 0),
        }

    def add_alias_preview(
        self, entity_id: str, alias: str, *, policy: str = "contextual", weight: float = 0.95
    ) -> dict[str, Any]:
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        if str(entity.get("identity_status") or "") in {"merged", "orphaned"}:
            raise ValueError("Alias kann nur an eine aktive Entity angelegt werden")
        alias = str(alias or "").strip()
        norm = normalize_name(alias)
        if not norm:
            raise ValueError("Alias ist leer")
        policy = _form_policy(policy, default="contextual")
        if policy == "document_only":
            raise ValueError("document_only gehört an eine konkrete Observation, nicht an einen globalen Alias")
        return {
            "action": "add_alias", "entity": entity, "alias": alias,
            "normalized": norm, "policy": policy,
            "weight": max(0.0, min(1.0, float(weight))),
            "note": "Ohne --yes wird nichts verändert.",
        }

    def add_alias(
        self, entity_id: str, alias: str, *, policy: str = "contextual", weight: float = 0.95
    ) -> dict[str, Any]:
        preview = self.add_alias_preview(entity_id, alias, policy=policy, weight=weight)
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            MERGE (a:SearchAlias {normalized:$normalized})
            ON CREATE SET a.value=$alias, a.created_at=datetime()
            SET a.last_seen_value=$alias, a.updated_at=datetime()
            MERGE (e)-[r:HAS_SEARCH_ALIAS {source_curator:'manual', normalized:$normalized}]->(a)
            SET r.kind='manual_alias', r.weight=$weight, r.active=true,
                r.resolution_policy=$policy, r.updated_at=datetime()
            """,
            entity_id=entity_id, normalized=str(preview["normalized"]),
            alias=str(preview["alias"]), weight=float(preview["weight"]),
            policy=str(preview["policy"]),
        )
        self.refresh_possible_same_as(entity_id)
        return {
            "status": "added_alias", "entity_id": entity_id,
            "alias": str(preview["alias"]), "normalized": str(preview["normalized"]),
            "policy": str(preview["policy"]), "weight": float(preview["weight"]),
        }


    def find_entities(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Find Entities by display name, real names and search aliases.

        Deliberately includes merged/orphaned tombstones so curation can recover
        historical IDs. This is an administration lookup, not query resolution.
        """
        query = str(query or "").strip()
        if not query:
            return []
        needle = query.casefold()
        normalized = normalize_name(query)
        rows = self._run(
            """
            MATCH (e:Entity)
            OPTIONAL MATCH (e)-[rn]->(n:EntityName)
            WHERE type(rn)='HAS_NAME'
            WITH e, collect(DISTINCT CASE WHEN n IS NULL THEN null ELSE {
                kind:'name', value:coalesce(properties(n)['last_seen_value'],properties(n)['value']),
                normalized:n.normalized,
                active:coalesce(properties(rn)['active'],true),
                resolution_policy:coalesce(properties(rn)['resolution_policy'],'exclusive')
            } END) AS names
            OPTIONAL MATCH (e)-[ra]->(a:SearchAlias)
            WHERE type(ra)='HAS_SEARCH_ALIAS'
            WITH e, names,
                 collect(DISTINCT CASE WHEN a IS NULL THEN null ELSE {
                    kind:'alias', value:coalesce(properties(a)['last_seen_value'],properties(a)['value']),
                    normalized:a.normalized,
                    active:coalesce(properties(ra)['active'],true),
                    resolution_policy:coalesce(properties(ra)['resolution_policy'],'contextual')
                 } END) AS aliases
            WITH e, [x IN names + aliases WHERE x IS NOT NULL] AS forms
            WHERE toLower(coalesce(e.display_name,'')) CONTAINS $needle
               OR any(x IN forms WHERE
                    toLower(coalesce(x.value,'')) CONTAINS $needle
                    OR ($normalized <> '' AND coalesce(x.normalized,'') CONTAINS $normalized))
            OPTIONAL MATCH (e)-[merge_rel]->(keep:Entity)
            WHERE type(merge_rel)='MERGED_INTO'
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   coalesce(properties(e)['origin'],'') AS origin,
                   coalesce(properties(e)['identity_status'],'') AS identity_status,
                   properties(e)['merged_into_entity_id'] AS merged_into_entity_id,
                   keep.display_name AS merged_into_display_name,
                   size([(d:Document)-[mention_rel]->(e) WHERE type(mention_rel)='MENTIONS' | d]) AS document_mentions,
                   size([(o:EntityObservation)-[resolved_rel]->(e) WHERE type(resolved_rel)='RESOLVED_TO' | o]) AS observations,
                   forms
            ORDER BY
              CASE WHEN toLower(coalesce(e.display_name,''))=$needle THEN 0 ELSE 1 END,
              e.display_name
            LIMIT $limit
            """,
            needle=needle,
            normalized=normalized,
            limit=max(1, min(int(limit), 200)),
        )
        return [dict(row) for row in rows]

    def list_merges(self, query: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        """List manual/legacy identity merges, newest first where timestamps exist."""
        needle = str(query or "").strip().casefold()
        rows = self._run(
            """
            MATCH (old:Entity)-[r]->(keep:Entity)
            WHERE type(r)='MERGED_INTO'
              AND ($needle=''
               OR toLower(coalesce(old.display_name,'')) CONTAINS $needle
               OR toLower(coalesce(keep.display_name,'')) CONTAINS $needle)
            RETURN old.entity_id AS merged_entity_id,
                   old.display_name AS merged_display_name,
                   coalesce(properties(old)['identity_status'],'') AS merged_status,
                   keep.entity_id AS survivor_entity_id,
                   keep.display_name AS survivor_display_name,
                   coalesce(properties(keep)['identity_status'],'') AS survivor_status,
                   coalesce(properties(r)['method'],'') AS method,
                   toString(properties(r)['merged_at']) AS merged_at
            ORDER BY properties(r)['merged_at'] DESC, old.display_name
            LIMIT $limit
            """,
            needle=needle,
            limit=max(1, min(int(limit), 500)),
        )
        return [dict(row) for row in rows]

    def entity_relation_observations(self, entity_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read-only relation observations involving one entity."""
        return self._run(
            """
            MATCH (c:RelationObservation)-[subject_rel]->(s:Entity)
            WHERE type(subject_rel)='SUBJECT'
            MATCH (c)-[object_rel]->(o:Entity)
            WHERE type(object_rel)='OBJECT'
            MATCH (d:Document)-[document_rel]->(c)
            WHERE type(document_rel)='HAS_RELATION_OBSERVATION'
              AND (s.entity_id=$entity_id OR o.entity_id=$entity_id)
            RETURN c.relation_id AS relation_id,
                   CASE WHEN s.entity_id=$entity_id THEN 'subject' ELSE 'object' END AS entity_role,
                   s.entity_id AS subject_entity_id,
                   s.display_name AS subject_display_name,
                   c.predicate AS predicate,
                   properties(c)['predicate_text'] AS predicate_text,
                   properties(c)['relation_text'] AS relation_text,
                   o.entity_id AS object_entity_id,
                   o.display_name AS object_display_name,
                   properties(c)['evidence_text'] AS evidence_text,
                   properties(c)['confidence'] AS confidence,
                   c.stance AS stance,
                   properties(c)['valid_from'] AS valid_from,
                   properties(c)['valid_to'] AS valid_to,
                   properties(c)['evidence_date'] AS evidence_date,
                   properties(c)['evidence_date_precision'] AS evidence_date_precision,
                   properties(c)['evidence_date_confidence'] AS evidence_date_confidence,
                   properties(c)['evidence_date_basis'] AS evidence_date_basis,
                   d.document_id AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(d)['source_url'] AS source_url
            ORDER BY coalesce(properties(c)['confidence'],0) DESC, properties(d)['document_date'] DESC, c.predicate
            LIMIT $limit
            """,
            entity_id=entity_id,
            limit=max(1, min(int(limit), 1000)),
        )

    def list_relation_observations(self, query: str = "", *, limit: int = 200) -> list[dict[str, Any]]:
        """Recent/filtered document-grounded relations for curator diagnostics."""
        needle = str(query or "").strip().casefold()
        return self._run(
            """
            MATCH (c:RelationObservation)-[subject_rel]->(s:Entity)
            WHERE type(subject_rel)='SUBJECT'
            MATCH (c)-[object_rel]->(o:Entity)
            WHERE type(object_rel)='OBJECT'
            MATCH (d:Document)-[document_rel]->(c)
            WHERE type(document_rel)='HAS_RELATION_OBSERVATION'
              AND ($needle=''
               OR toLower(coalesce(s.display_name,'')) CONTAINS $needle
               OR toLower(coalesce(o.display_name,'')) CONTAINS $needle
               OR toLower(coalesce(c.predicate,'')) CONTAINS $needle
               OR toLower(coalesce(properties(c)['predicate_text'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(c)['relation_text'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(c)['evidence_text'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(d)['title'],'')) CONTAINS $needle)
            RETURN c.relation_id AS relation_id,
                   s.entity_id AS subject_entity_id,
                   s.display_name AS subject_display_name,
                   c.predicate AS predicate,
                   properties(c)['predicate_text'] AS predicate_text,
                   properties(c)['relation_text'] AS relation_text,
                   o.entity_id AS object_entity_id,
                   o.display_name AS object_display_name,
                   properties(c)['evidence_text'] AS evidence_text,
                   properties(c)['confidence'] AS confidence,
                   c.stance AS stance,
                   properties(c)['valid_from'] AS valid_from,
                   properties(c)['valid_to'] AS valid_to,
                   properties(c)['evidence_date'] AS evidence_date,
                   properties(c)['evidence_date_precision'] AS evidence_date_precision,
                   properties(c)['evidence_date_confidence'] AS evidence_date_confidence,
                   properties(c)['evidence_date_basis'] AS evidence_date_basis,
                   d.document_id AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(d)['source_url'] AS source_url,
                   toString(properties(c)['updated_at']) AS updated_at
            ORDER BY coalesce(properties(c)['updated_at'],properties(c)['created_at']) DESC, coalesce(properties(c)['confidence'],0) DESC
            LIMIT $limit
            """,
            needle=needle,
            limit=max(1, min(int(limit), 1000)),
        )

    def entity_detail(self, entity_id: str) -> dict[str, Any]:
        """Curator-oriented detail view used by CLI and the future admin UI."""
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        merge_target = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            OPTIONAL MATCH (e)-[r]->(keep:Entity)
            WHERE type(r)='MERGED_INTO'
            RETURN keep.entity_id AS entity_id, keep.display_name AS display_name,
                   coalesce(properties(r)['method'],'') AS method, toString(properties(r)['merged_at']) AS merged_at
            LIMIT 1
            """,
            entity_id=entity_id,
        )
        merged_from = self._run(
            """
            MATCH (old:Entity)-[r]->(e:Entity {entity_id:$entity_id})
            WHERE type(r)='MERGED_INTO'
            RETURN old.entity_id AS entity_id, old.display_name AS display_name,
                   coalesce(properties(r)['method'],'') AS method, toString(properties(r)['merged_at']) AS merged_at
            ORDER BY properties(r)['merged_at'] DESC, old.display_name
            """,
            entity_id=entity_id,
        )
        same_as = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r]-(peer:Entity)
            WHERE type(r)='SAME_AS' AND coalesce(properties(r)['active'],true)=true
            RETURN peer.entity_id AS entity_id, peer.display_name AS display_name,
                   coalesce(properties(peer)['identity_status'],'') AS identity_status,
                   coalesce(properties(r)['reason'],'') AS reason,
                   toString(properties(r)['decided_at']) AS decided_at
            ORDER BY peer.display_name, peer.entity_id
            """,
            entity_id=entity_id,
        )
        return {
            "entity": entity,
            "forms": self.entity_forms(entity_id),
            "contacts": self.entity_contact_records(entity_id),
            "observations": self.entity_observations(entity_id),
            "relations": self.entity_relation_observations(entity_id),
            "merged_into": (dict(merge_target[0]) if merge_target and merge_target[0].get("entity_id") else None),
            "merged_from": [dict(row) for row in merged_from],
            "same_as": [dict(row) for row in same_as],
        }

    def remove_alias_preview(self, entity_id: str, alias: str) -> dict[str, Any]:
        """Preview removal of one global SearchAlias from exactly one Entity.

        EntityName relationships are intentionally not touched. The shared
        SearchAlias node is deleted only when no Entity references it afterwards.
        """
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        alias = str(alias or "").strip()
        normalized = normalize_name(alias)
        if not normalized:
            raise ValueError("Alias ist leer")
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_SEARCH_ALIAS]->(a:SearchAlias {normalized:$normalized})
            RETURN coalesce(properties(a)['last_seen_value'],properties(a)['value']) AS value,
                   a.normalized AS normalized,
                   coalesce(properties(r)['kind'],'alias') AS relation_kind,
                   coalesce(properties(r)['resolution_policy'],'contextual') AS resolution_policy,
                   coalesce(properties(r)['active'],true) AS active,
                   properties(r)['source_curator'] AS source_curator,
                   properties(r)['source_merge_entity_id'] AS source_merge_entity_id,
                   properties(r)['source_document_id'] AS source_document_id,
                   properties(r)['source_contact_id'] AS source_contact_id
            """,
            entity_id=entity_id,
            normalized=normalized,
        )
        if not rows:
            raise ValueError(f"Alias nicht an Entity gefunden: {alias}")
        return {
            "action": "remove_alias",
            "entity": entity,
            "alias": alias,
            "normalized": normalized,
            "relationships": [dict(row) for row in rows],
            "note": "Entfernt nur HAS_SEARCH_ALIAS an dieser Entity; EntityName bleibt unberührt. Ohne --yes keine Änderung.",
        }

    def remove_alias(self, entity_id: str, alias: str) -> dict[str, Any]:
        preview = self.remove_alias_preview(entity_id, alias)
        normalized = str(preview["normalized"])
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_SEARCH_ALIAS]->(a:SearchAlias {normalized:$normalized})
            WITH collect(r) AS rels
            FOREACH (r IN rels | DELETE r)
            RETURN size(rels) AS removed
            """,
            entity_id=entity_id,
            normalized=normalized,
        )
        orphan_rows = self._run(
            """
            MATCH (a:SearchAlias {normalized:$normalized})
            OPTIONAL MATCH (:Entity)-[r:HAS_SEARCH_ALIAS]->(a)
            WITH a, count(r) AS refs
            WHERE refs=0
            DETACH DELETE a
            RETURN count(a) AS deleted
            """,
            normalized=normalized,
        )
        self.refresh_possible_same_as(entity_id)
        return {
            "status": "removed_alias",
            "entity_id": entity_id,
            "alias": str(preview["alias"]),
            "normalized": normalized,
            "relationships_removed": int(rows[0].get("removed") or 0) if rows else 0,
            "orphan_alias_node_deleted": bool(int(orphan_rows[0].get("deleted") or 0)) if orphan_rows else False,
        }

    def _existing_entity_for_contact(self, contact_id: str) -> str | None:
        rows = self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})-[:DESCRIBES]->(e:Entity)
            RETURN e.entity_id AS entity_id
            LIMIT 1
            """,
            contact_id=contact_id,
        )
        return rows[0]["entity_id"] if rows else None

    def _entities_by_email(self, normalized: str, entity_type: str) -> list[str]:
        if not normalized:
            return []
        rows = self._run(
            """
            MATCH (e:Entity)-[r:HAS_EMAIL]->(:EmailAddress {normalized:$normalized})
            WHERE r.active = true AND $entity_type IN labels(e)
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT e.entity_id AS entity_id
            """,
            normalized=normalized,
            entity_type=entity_type,
        )
        return [r["entity_id"] for r in rows]

    def _entities_by_phone(self, normalized: str, entity_type: str) -> list[str]:
        if not normalized:
            return []
        rows = self._run(
            """
            MATCH (e:Entity)-[r:HAS_PHONE]->(:PhoneNumber {normalized:$normalized})
            WHERE r.active = true AND $entity_type IN labels(e)
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT e.entity_id AS entity_id
            """,
            normalized=normalized,
            entity_type=entity_type,
        )
        return [r["entity_id"] for r in rows]

    def _entities_by_exact_name(self, normalized: str, entity_type: str) -> list[str]:
        if not normalized:
            return []
        rows = self._run(
            """
            MATCH (e:Entity)-[:HAS_NAME]->(:EntityName {normalized:$normalized})
            WHERE $entity_type IN labels(e)
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT e.entity_id AS entity_id
            """,
            normalized=normalized,
            entity_type=entity_type,
        )
        return [r["entity_id"] for r in rows]

    def _entities_by_identity_key(self, identity_key: str, entity_type: str) -> list[str]:
        if not identity_key or entity_type != "Organization":
            return []
        rows = self._run(
            """
            MATCH (e:Entity:Organization {identity_key:$identity_key})
            WHERE coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT properties(e)['entity_id'] AS entity_id
            ORDER BY properties(e)['entity_id']
            """,
            identity_key=identity_key,
        )
        return [str(r["entity_id"]) for r in rows]

    def resolve_or_create_entity(
        self,
        *,
        contact_id: str,
        entity_type: str,
        display_name: str,
        names: list[dict[str, Any]],
        emails: list[str],
        phones: list[str],
        organization_name: str = "",
    ) -> tuple[str, str]:
        """Resolve conservatively and return (entity_id, reason)."""
        entity_type = "Organization" if entity_type == "Organization" else "Person"

        existing = self._existing_entity_for_contact(contact_id)
        if existing:
            return existing, "contact_record"

        # A strict organization identity key only neutralizes semantically
        # equivalent legal-form wording (e.g. UG vs UG (haftungsbeschränkt)).
        # It does NOT use fuzzy name similarity.
        if entity_type == "Organization":
            identity_matches = self._entities_by_identity_key(
                organization_identity_key(display_name), "Organization"
            )
            if len(identity_matches) == 1:
                return identity_matches[0], "organization_identity_key"

        # Exact e-mail and phone are strongest. Only accept a unique candidate.
        candidates: set[str] = set()
        for email in emails:
            candidates.update(self._entities_by_email(normalize_email(email), entity_type))
        if len(candidates) == 1:
            return next(iter(candidates)), "email"

        candidates = set()
        for phone in phones:
            candidates.update(self._entities_by_phone(normalize_phone(phone), entity_type))
        if len(candidates) == 1:
            return next(iter(candidates)), "phone"

        # A later CardDAV seed may confirm an Organization that was first
        # discovered in a document.  Only upgrade a UNIQUE exact-name match and
        # only when that target is still document-derived/provisional.  This
        # avoids broad name-only merging of established CardDAV identities.
        if entity_type == "Organization":
            name_candidates: set[str] = set()
            for item in names:
                name_norm = normalize_name(item.get("value", ""))
                if name_norm:
                    name_candidates.update(self._entities_by_exact_name(name_norm, entity_type))
            if len(name_candidates) == 1:
                candidate_id = next(iter(name_candidates))
                rows = self._run(
                    """
                    MATCH (e:Entity:Organization {entity_id:$entity_id})
                    RETURN coalesce(properties(e)['origin'],'') AS origin,
                           coalesce(properties(e)['identity_status'],'') AS identity_status
                    """,
                    entity_id=candidate_id,
                )
                if rows and (
                    "document" in str(rows[0].get("origin") or "")
                    or str(rows[0].get("identity_status") or "") == "provisional"
                ):
                    return candidate_id, "document_exact_name"

        # Name-only identity merges are deliberately avoided. Same-name people
        # are common enough that a unique current hit is not proof of identity.
        # For persons, name + organization is strong enough for a seed merge.
        org_norm = normalize_name(organization_name)
        if entity_type == "Person" and org_norm:
            for item in names:
                name_norm = normalize_name(item.get("value", ""))
                if not name_norm:
                    continue
                rows = self._run(
                    """
                    MATCH (e:Entity:Person)-[:HAS_NAME]->(:EntityName {normalized:$name_norm})
                    MATCH (e)-[rw:WORKS_AT]->(o:Entity:Organization)-[:HAS_NAME]->(:EntityName {normalized:$org_norm})
                    WHERE coalesce(properties(rw)['active'],true)=true
                    RETURN DISTINCT properties(e)['entity_id'] AS entity_id
                    """,
                    name_norm=name_norm,
                    org_norm=org_norm,
                )
                matches = [row["entity_id"] for row in rows]
                if len(matches) == 1:
                    return matches[0], "name_and_organization"

        entity_id = str(uuid.uuid4())
        label = entity_type
        self._run(
            f"""
            CREATE (e:Entity:{label} {{
                entity_id:$entity_id,
                display_name:$display_name,
                identity_key:$identity_key,
                entity_kind:$entity_kind,
                origin:'carddav',
                identity_status:'seeded',
                created_at:datetime(),
                updated_at:datetime()
            }})
            """,
            entity_id=entity_id,
            display_name=display_name,
            identity_key=(organization_identity_key(display_name) if entity_type == "Organization" else ""),
            entity_kind=("Person" if entity_type == "Person" else ""),
        )
        return entity_id, "created"

    def _ensure_organization_units(
        self,
        *,
        organization_entity_id: str,
        organization_name: str,
        organization_units: list[str],
        contact_id: str,
    ) -> list[str]:
        """Upsert a hierarchical organizational-unit chain.

        Unit identity is scoped by the owning organization and the full unit
        path. Therefore "Insolvenzgericht" below Amtsgericht Musterstadt is a
        different entity from an equally named unit below another court.
        """
        units = _compact_unique(organization_units)
        if not units:
            return []

        # Preserve historical nodes, but deactivate relationships supplied by
        # this contact before recreating the current snapshot.
        self._run(
            """
            MATCH (u:Entity:OrganizationalUnit)-[r:PART_OF]->()
            WHERE r.source_contact_id=$contact_id
            SET r.active=false, r.updated_at=datetime()
            """,
            contact_id=contact_id,
        )
        for rel in ("HAS_NAME", "HAS_SEARCH_ALIAS"):
            self._run(
                f"""
                MATCH (u:Entity:OrganizationalUnit)-[r:{rel}]->()
                WHERE r.source_contact_id=$contact_id
                SET r.active=false, r.updated_at=datetime()
                """,
                contact_id=contact_id,
            )
        self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})-[r:SUPPLIES_ORG_UNIT]->()
            DELETE r
            """,
            contact_id=contact_id,
        )

        ids: list[str] = []
        parent_entity_id = organization_entity_id
        path_parts: list[str] = []

        for level, unit_name in enumerate(units, start=1):
            path_parts.append(unit_name)
            normalized_path = " / ".join(normalize_name(x) for x in path_parts)
            unit_key = hashlib.sha256(
                f"{organization_entity_id}|{normalized_path}".encode("utf-8")
            ).hexdigest()
            new_entity_id = str(uuid.uuid4())
            qualified_name = " / ".join([organization_name, *path_parts])

            rows = self._run(
                """
                MATCH (org:Entity:Organization {entity_id:$organization_entity_id})
                MERGE (u:Entity:OrganizationalUnit {unit_key:$unit_key})
                ON CREATE SET
                    u.entity_id=$new_entity_id,
                    u.created_at=datetime()
                SET u.display_name=$qualified_name,
                    u.short_name=$unit_name,
                    u.qualified_name=$qualified_name,
                    u.organization_entity_id=$organization_entity_id,
                    u.entity_kind=coalesce(properties(u)['entity_kind'],''),
                    u.level=$level,
                    u.updated_at=datetime()
                RETURN properties(u)['entity_id'] AS entity_id
                """,
                organization_entity_id=organization_entity_id,
                unit_key=unit_key,
                new_entity_id=new_entity_id,
                unit_name=unit_name,
                qualified_name=qualified_name,
                level=level,
            )
            unit_entity_id = rows[0]["entity_id"]
            ids.append(unit_entity_id)

            # Organizational-unit identity is scoped by the parent organization.
            # Do not create a global EntityName such as merely "Handelsregister",
            # which would visually and semantically collapse unrelated courts.
            qualified_norm = normalize_name(qualified_name)
            self._run(
                """
                MATCH (u:Entity:OrganizationalUnit {entity_id:$unit_entity_id})
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (n:EntityName {normalized:$normalized})
                ON CREATE SET n.value=$value, n.created_at=datetime()
                SET n.last_seen_value=$value, n.updated_at=datetime()
                MERGE (u)-[r:HAS_NAME {source_contact_id:$contact_id, normalized:$normalized}]->(n)
                SET r.kind='qualified_organizational_unit', r.preferred=true, r.active=true,
                    r.resolution_policy='exclusive', r.updated_at=datetime()
                MERGE (c)-[s:SUPPLIES_ORG_UNIT {unit_key:$unit_key}]->(u)
                SET s.level=$level, s.active=true, s.updated_at=datetime()
                """,
                unit_entity_id=unit_entity_id,
                contact_id=contact_id,
                normalized=qualified_norm,
                value=qualified_name,
                unit_key=unit_key,
                level=level,
            )

            # Qualified forms are search aliases, not alternative real names.
            alias_values = [
                (f"{unit_name} {organization_name}", "unit_org", 0.96),
                (f"{organization_name} {unit_name}", "org_unit", 0.96),
                (qualified_name, "qualified_unit", 0.98),
            ]
            # Common natural shorthand for institutions such as
            # "Insolvenzgericht Musterstadt" from "Amtsgericht Musterstadt".
            org_tokens = organization_name.split(maxsplit=1)
            institutional_heads = {
                "amtsgericht", "landgericht", "oberlandesgericht",
                "verwaltungsgericht", "arbeitsgericht", "sozialgericht",
                "finanzgericht", "staatsanwaltschaft", "finanzamt",
            }
            if len(org_tokens) == 2 and normalize_name(org_tokens[0]) in institutional_heads:
                alias_values.append((f"{unit_name} {org_tokens[1]}", "unit_location", 0.94))

            seen_aliases: set[str] = set()
            for alias_value, alias_kind, alias_weight in alias_values:
                alias_norm = normalize_name(alias_value)
                if not alias_norm or alias_norm == qualified_norm or alias_norm in seen_aliases:
                    continue
                seen_aliases.add(alias_norm)
                self._run(
                    """
                    MATCH (u:Entity:OrganizationalUnit {entity_id:$unit_entity_id})
                    MERGE (a:SearchAlias {normalized:$normalized})
                    ON CREATE SET a.value=$value, a.created_at=datetime()
                    SET a.last_seen_value=$value, a.updated_at=datetime()
                    MERGE (u)-[r:HAS_SEARCH_ALIAS {source_contact_id:$contact_id, normalized:$normalized}]->(a)
                    SET r.kind=$kind, r.weight=$weight, r.active=true,
                        r.resolution_policy='contextual', r.updated_at=datetime()
                    """,
                    unit_entity_id=unit_entity_id,
                    contact_id=contact_id,
                    normalized=alias_norm,
                    value=alias_value,
                    kind=alias_kind,
                    weight=alias_weight,
                )

            self._run(
                """
                MATCH (u:Entity:OrganizationalUnit {entity_id:$unit_entity_id})
                MATCH (parent:Entity {entity_id:$parent_entity_id})
                MERGE (u)-[r:PART_OF {source_contact_id:$contact_id}]->(parent)
                SET r.active=true, r.updated_at=datetime()
                """,
                unit_entity_id=unit_entity_id,
                parent_entity_id=parent_entity_id,
                contact_id=contact_id,
            )
            parent_entity_id = unit_entity_id

        return ids

    # ------------------------------------------------------------------
    # Non-destructive duplicate / merge candidates
    # ------------------------------------------------------------------

    @staticmethod
    def _organization_core_normalized(name: str) -> str:
        """Return a conservative organization core without a trailing legal form."""
        raw = str(name or "").strip()
        if not raw:
            return ""
        aliases = generated_organization_aliases(raw)
        for item in aliases:
            if str(item.get("kind") or "") == "without_legal_form":
                return str(item.get("normalized") or normalize_name(item.get("value") or ""))
        return normalize_name(raw)

    def refresh_possible_same_as(self, entity_id: str, *, max_candidates: int = 5) -> list[dict[str, Any]]:
        """Create review-only POSSIBLE_SAME_AS edges; never merge identities.

        Candidate similarity is evaluated across *all active names and aliases*
        of both identities, not just their current display names. This matters
        after a curator merge: a retired display name is preserved as an alias
        on the survivor and must continue to participate in duplicate review.
        OrganizationalUnit is excluded because its identity is parent-scoped.
        """
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            RETURN e.display_name AS display_name, labels(e) AS labels,
                   coalesce(properties(e)['identity_status'],'') AS identity_status,
                   coalesce(e.identity_key,'') AS identity_key
            """,
            entity_id=entity_id,
        )
        if not rows:
            return []
        labels = list(rows[0].get("labels") or [])
        if str(rows[0].get("identity_status") or "") in {"merged", "orphaned"}:
            return []
        if "OrganizationalUnit" in labels:
            return []
        entity_type = "Organization" if "Organization" in labels else "Person" if "Person" in labels else ""
        if not entity_type:
            return []
        own_name = str(rows[0].get("display_name") or "").strip()
        own_norm = normalize_name(own_name)
        if not own_norm:
            return []

        # Remove only machine-generated pending suggestions involving this node;
        # curator decisions and merge-carried candidates remain untouched.
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r]-()
            WHERE type(r)='POSSIBLE_SAME_AS'
              AND coalesce(properties(r)['status'],'candidate')='candidate'
              AND coalesce(properties(r)['suggested_by'],'')='identity_similarity_v1'
            DELETE r
            """,
            entity_id=entity_id,
        )

        # SAME_AS is an equivalence relation. Candidate suppression must therefore
        # operate on the whole component, not merely on the concrete edge that a
        # curator happened to confirm. Likewise, one NOT_SAME_AS decision between
        # two components blocks all equivalent members on both sides.
        own_component_ids = set(self.same_as_component_ids(entity_id) or [entity_id])
        blocked_ids = set(own_component_ids)
        blocked_rows = self._run(
            """
            UNWIND $component_ids AS current_id
            MATCH (e:Entity {entity_id:current_id})-[r]-(x:Entity)
            WHERE type(r)='NOT_SAME_AS'
            RETURN DISTINCT x.entity_id AS entity_id
            """,
            component_ids=sorted(own_component_ids),
        )
        for blocked_row in blocked_rows:
            blocked_id = str(blocked_row.get("entity_id") or "")
            if not blocked_id:
                continue
            blocked_ids.update(self.same_as_component_ids(blocked_id) or [blocked_id])

        forms = self.name_forms(include_inactive_names=False)
        own_forms: list[dict[str, Any]] = [
            row for row in forms if str(row.get("entity_id") or "") in own_component_ids
        ]
        # A display name should remain usable even when a legacy/imported node has
        # no explicit active HAS_NAME edge.
        if not any(str(row.get("normalized") or "") == own_norm for row in own_forms):
            own_forms.append({
                "entity_id": entity_id,
                "display_name": own_name,
                "value": own_name,
                "normalized": own_norm,
                "labels": labels,
                "form_type": "display_name",
                "weight": 1.0,
            })

        grouped: dict[str, dict[str, Any]] = {}
        for row in forms:
            other_id = str(row.get("entity_id") or "")
            if not other_id or other_id == entity_id or other_id in blocked_ids:
                continue
            other_labels = list(row.get("labels") or [])
            if entity_type not in other_labels or "OrganizationalUnit" in other_labels:
                continue
            other_form = str(row.get("value") or row.get("display_name") or "").strip()
            other_norm = normalize_name(other_form)
            if not other_norm:
                continue

            best_pair: dict[str, Any] | None = None
            for own_row in own_forms:
                own_form = str(own_row.get("value") or own_row.get("display_name") or own_name).strip()
                own_form_norm = normalize_name(own_form)
                if not own_form_norm:
                    continue

                if other_norm == own_form_norm:
                    score = 1.0
                    reason = "exact_shared_form"
                else:
                    score = float(fuzz.ratio(own_form_norm, other_norm)) / 100.0
                    reason = "high_name_similarity"

                if entity_type == "Organization":
                    own_form_key = organization_identity_key(own_form)
                    other_form_key = organization_identity_key(other_form)
                    if own_form_key and other_form_key == own_form_key:
                        score = max(score, 0.995)
                        reason = "identity_key_equivalent"
                    own_core = self._organization_core_normalized(own_form)
                    other_core = self._organization_core_normalized(other_form)
                    if own_core and other_core and own_core == other_core and own_form_norm != other_norm:
                        score = max(score, 0.985)
                        reason = "legal_form_compatible"
                    threshold = 0.92
                else:
                    threshold = 0.96

                if score < threshold:
                    continue
                pair = {
                    "score": score,
                    "reason": reason,
                    "own_form": own_form,
                    "other_form": other_form,
                    "own_form_type": str(own_row.get("form_type") or "name"),
                    "other_form_type": str(row.get("form_type") or "name"),
                }
                if best_pair is None or score > float(best_pair.get("score") or 0.0):
                    best_pair = pair

            if best_pair is None:
                continue
            old = grouped.get(other_id)
            if old is None or float(best_pair["score"]) > float(old.get("score") or 0.0):
                grouped[other_id] = {
                    "entity_id": other_id,
                    "display_name": str(row.get("display_name") or other_form),
                    **best_pair,
                }

        candidates = sorted(grouped.values(), key=lambda x: (-float(x["score"]), x["display_name"]))[:max_candidates]
        for cand in candidates:
            other_id = str(cand["entity_id"])
            left_id, right_id = sorted([entity_id, other_id])
            if left_id == entity_id:
                left_form, right_form = str(cand["own_form"]), str(cand["other_form"])
                left_form_type, right_form_type = str(cand["own_form_type"]), str(cand["other_form_type"])
            else:
                left_form, right_form = str(cand["other_form"]), str(cand["own_form"])
                left_form_type, right_form_type = str(cand["other_form_type"]), str(cand["own_form_type"])
            self._run(
                """
                MATCH (a:Entity {entity_id:$left_id})
                MATCH (b:Entity {entity_id:$right_id})
                MERGE (a)-[r:POSSIBLE_SAME_AS]->(b)
                SET r.score=$score,
                    r.reason=$reason,
                    r.status='candidate',
                    r.suggested_by='identity_similarity_v1',
                    r.matched_left_form=$left_form,
                    r.matched_right_form=$right_form,
                    r.matched_left_form_type=$left_form_type,
                    r.matched_right_form_type=$right_form_type,
                    r.updated_at=datetime()
                """,
                left_id=left_id,
                right_id=right_id,
                score=float(cand["score"]),
                reason=str(cand["reason"]),
                left_form=left_form,
                right_form=right_form,
                left_form_type=left_form_type,
                right_form_type=right_form_type,
            )
        return candidates

    def refresh_all_possible_same_as(self, *, max_candidates: int = 5) -> dict[str, int]:
        """Rebuild machine-generated duplicate suggestions for all active identities.

        Manual NOT_SAME_AS decisions and merge-carried candidates are preserved.
        No LLM call and no document re-indexing is involved.
        """
        rows = self._run(
            """
            MATCH (e:Entity)
            WHERE (e:Person OR e:Organization)
              AND NOT e:OrganizationalUnit
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN properties(e)['entity_id'] AS entity_id
            ORDER BY properties(e)['entity_id']
            """
        )
        refreshed = 0
        for row in rows:
            entity_id = str(row.get("entity_id") or "")
            if not entity_id:
                continue
            self.refresh_possible_same_as(entity_id, max_candidates=max_candidates)
            refreshed += 1
        count_rows = self._run(
            """
            MATCH ()-[r:POSSIBLE_SAME_AS]->()
            WHERE coalesce(properties(r)['status'],'candidate')='candidate'
            RETURN count(r) AS count
            """
        )
        return {
            "entities_refreshed": refreshed,
            "merge_candidates": int(count_rows[0]["count"] if count_rows else 0),
        }

    # ------------------------------------------------------------------
    # Contact upsert + provenance/history
    # ------------------------------------------------------------------

    def upsert_contact(
        self,
        *,
        contact_id: str,
        vcard_uid: str,
        href: str,
        etag: str,
        addressbook_href: str,
        addressbook_name: str,
        entity_type: str,
        display_name: str,
        names: list[dict[str, Any]],
        aliases: list[dict[str, Any]],
        emails: list[str],
        phones: list[str],
        addresses: list[str],
        organization_name: str = "",
        organization_units: list[str] | None = None,
        cloud_id: str = "",
        source_user_id: str = "",
        addressbook_slug: str = "",
        import_run_id: str = "",
    ) -> dict[str, Any]:
        # A legacy ContactRecord may have blank provenance and is allowed to be
        # claimed by the first 0.6.6a sync.  Once explicit provenance exists, a
        # contact_id collision across cloud/user/addressbook is never overwritten
        # silently. This matters for shared address books in future multi-user use.
        existing_source = self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})
            RETURN coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href
            LIMIT 1
            """,
            contact_id=contact_id,
        )
        if existing_source:
            old = existing_source[0]
            conflicts = []
            for field, new_value in (
                ("cloud_id", str(cloud_id or "")),
                ("source_user_id", str(source_user_id or "")),
                ("addressbook_href", str(addressbook_href or "")),
            ):
                old_value = str(old.get(field) or "")
                if old_value and new_value and old_value != new_value:
                    conflicts.append((field, old_value, new_value))
            if conflicts:
                raise ValueError(
                    f"ContactRecord provenance collision for {contact_id}: {conflicts}"
                )

        entity_id, resolved_by = self.resolve_or_create_entity(
            contact_id=contact_id,
            entity_type=entity_type,
            display_name=display_name,
            names=names,
            emails=emails,
            phones=phones,
            organization_name=organization_name,
        )
        organization_units = _compact_unique(organization_units or [])

        self._run(
            """
            MERGE (c:ContactRecord {contact_id:$contact_id})
            ON CREATE SET c.created_at = datetime(),
                          c.created_import_run_id = CASE WHEN $import_run_id <> '' THEN $import_run_id ELSE null END
            SET c.vcard_uid=$vcard_uid,
                c.href=$href,
                c.etag=$etag,
                c.addressbook_href=$addressbook_href,
                c.addressbook_name=$addressbook_name,
                c.addressbook_slug=$addressbook_slug,
                c.cloud_id=$cloud_id,
                c.source_user_id=$source_user_id,
                c.last_import_run_id=CASE WHEN $import_run_id <> '' THEN $import_run_id ELSE properties(c)['last_import_run_id'] END,
                c.organization_units=$organization_units,
                c.updated_at=datetime(),
                c.last_seen_at=datetime()
            WITH c
            MATCH (e:Entity {entity_id:$entity_id})
            MERGE (c)-[r:DESCRIBES]->(e)
            SET r.resolved_by=$resolved_by,
                r.updated_at=datetime()
            SET e.display_name = CASE
                WHEN $display_name <> '' THEN $display_name
                ELSE e.display_name
            END,
                e.origin = CASE
                    WHEN properties(e)['origin'] IS NULL OR e.origin = '' THEN 'carddav'
                    WHEN e.origin = 'document' THEN 'carddav+document'
                    ELSE properties(e)['origin']
                END,
                e.identity_status=CASE WHEN coalesce(properties(e)['identity_status'],'')='confirmed' THEN 'confirmed' ELSE 'seeded' END,
                e.display_name_source_contact_id=CASE WHEN $display_name <> '' THEN $contact_id ELSE properties(e)['display_name_source_contact_id'] END,
                e.identity_key=CASE WHEN $entity_type='Organization' THEN $identity_key ELSE e.identity_key END,
                e.updated_at=datetime()
            """,
            contact_id=contact_id,
            vcard_uid=vcard_uid,
            href=href,
            etag=etag,
            addressbook_href=addressbook_href,
            addressbook_name=addressbook_name,
            addressbook_slug=str(addressbook_slug or ""),
            cloud_id=str(cloud_id or ""),
            source_user_id=str(source_user_id or ""),
            import_run_id=str(import_run_id or ""),
            organization_units=organization_units,
            entity_id=entity_id,
            resolved_by=resolved_by,
            display_name=display_name,
            entity_type=entity_type,
            identity_key=(organization_identity_key(display_name) if entity_type == "Organization" else ""),
        )

        if str(import_run_id or "").strip():
            self._run(
                """
                MERGE (run:ContactImportRun {run_id:$run_id})
                ON CREATE SET run.created_at=datetime(), run.status='running'
                SET run.cloud_id=$cloud_id, run.source_user_id=$source_user_id,
                    run.updated_at=datetime()
                WITH run
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (run)-[r:TOUCHED]->(c)
                ON CREATE SET r.first_touched_at=datetime()
                SET r.last_touched_at=datetime()
                """,
                run_id=str(import_run_id),
                cloud_id=str(cloud_id or ""),
                source_user_id=str(source_user_id or ""),
                contact_id=contact_id,
            )

        # Preserve previous contact-derived identity values as inactive history.
        for rel in ("HAS_NAME", "HAS_SEARCH_ALIAS", "HAS_EMAIL", "HAS_PHONE", "HAS_ADDRESS"):
            self._run(
                f"""
                MATCH (e:Entity {{entity_id:$entity_id}})-[r:{rel}]->()
                WHERE r.source_contact_id=$contact_id
                SET r.active=false, r.updated_at=datetime()
                """,
                entity_id=entity_id,
                contact_id=contact_id,
            )

        # Current source edges from ContactRecord are exact snapshots, so replace.
        for rel in ("SUPPLIES_NAME", "SUPPLIES_EMAIL", "SUPPLIES_PHONE", "SUPPLIES_ADDRESS"):
            self._run(
                f"""
                MATCH (c:ContactRecord {{contact_id:$contact_id}})-[r:{rel}]->()
                DELETE r
                """,
                contact_id=contact_id,
            )

        for idx, item in enumerate(names):
            value = str(item.get("value", "")).strip()
            normalized = normalize_name(value)
            if not normalized:
                continue
            kind = str(item.get("kind", "name"))
            preferred = bool(item.get("preferred", idx == 0))
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (n:EntityName {normalized:$normalized})
                ON CREATE SET n.value=$value, n.created_at=datetime()
                SET n.last_seen_value=$value, n.updated_at=datetime()
                MERGE (e)-[r:HAS_NAME {source_contact_id:$contact_id, normalized:$normalized}]->(n)
                SET r.kind=$kind, r.preferred=$preferred, r.active=true,
                    r.resolution_policy=$resolution_policy, r.updated_at=datetime()
                MERGE (c)-[:SUPPLIES_NAME]->(n)
                """,
                entity_id=entity_id,
                contact_id=contact_id,
                normalized=normalized,
                value=value,
                kind=kind,
                preferred=preferred,
                resolution_policy=_form_policy(item.get("resolution_policy"), default="exclusive"),
            )

        for item in aliases:
            value = str(item.get("value", "")).strip()
            normalized = normalize_name(value)
            if not normalized:
                continue
            kind = str(item.get("kind", "generated"))
            weight = float(item.get("weight", 0.5))
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MERGE (a:SearchAlias {normalized:$normalized})
                ON CREATE SET a.value=$value, a.created_at=datetime()
                SET a.last_seen_value=$value, a.updated_at=datetime()
                MERGE (e)-[r:HAS_SEARCH_ALIAS {source_contact_id:$contact_id, normalized:$normalized}]->(a)
                SET r.kind=$kind, r.weight=$weight, r.active=true,
                    r.resolution_policy=$resolution_policy, r.updated_at=datetime()
                """,
                entity_id=entity_id,
                contact_id=contact_id,
                normalized=normalized,
                value=value,
                kind=kind,
                weight=weight,
                resolution_policy=_form_policy(item.get("resolution_policy"), default="contextual"),
            )

        for email in _compact_unique(emails):
            normalized = normalize_email(email)
            if not normalized:
                continue
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (v:EmailAddress {normalized:$normalized})
                ON CREATE SET v.value=$value, v.created_at=datetime()
                SET v.last_seen_value=$value, v.updated_at=datetime()
                MERGE (e)-[r:HAS_EMAIL {source_contact_id:$contact_id, normalized:$normalized}]->(v)
                SET r.active=true, r.updated_at=datetime()
                MERGE (c)-[:SUPPLIES_EMAIL]->(v)
                """,
                entity_id=entity_id,
                contact_id=contact_id,
                normalized=normalized,
                value=email,
            )

        for phone in _compact_unique(phones):
            normalized = normalize_phone(phone)
            if not normalized:
                continue
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (v:PhoneNumber {normalized:$normalized})
                ON CREATE SET v.value=$value, v.created_at=datetime()
                SET v.last_seen_value=$value, v.updated_at=datetime()
                MERGE (e)-[r:HAS_PHONE {source_contact_id:$contact_id, normalized:$normalized}]->(v)
                SET r.active=true, r.updated_at=datetime()
                MERGE (c)-[:SUPPLIES_PHONE]->(v)
                """,
                entity_id=entity_id,
                contact_id=contact_id,
                normalized=normalized,
                value=phone,
            )

        for address in _compact_unique(addresses):
            normalized = normalize_address(address)
            if not normalized:
                continue
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MATCH (c:ContactRecord {contact_id:$contact_id})
                MERGE (v:PostalAddress {normalized:$normalized})
                ON CREATE SET v.value=$value, v.created_at=datetime()
                SET v.last_seen_value=$value, v.updated_at=datetime()
                MERGE (e)-[r:HAS_ADDRESS {source_contact_id:$contact_id, normalized:$normalized}]->(v)
                SET r.active=true, r.updated_at=datetime()
                MERGE (c)-[:SUPPLIES_ADDRESS]->(v)
                """,
                entity_id=entity_id,
                contact_id=contact_id,
                normalized=normalized,
                value=address,
            )

        # A changed CardDAV ORG must not leave the previous affiliation active.
        for rel in PERSON_AFFILIATION_RELATIONS:
            self._run(
                f"""
                MATCH (e:Entity {{entity_id:$entity_id}})-[r:{rel}]->()
                WHERE r.source_contact_id=$contact_id
                SET r.active=false, r.updated_at=datetime()
                """,
                entity_id=entity_id,
                contact_id=contact_id,
            )

        org_entity_id: str | None = entity_id if entity_type == "Organization" else None
        organization_name = str(organization_name or "").strip()
        if entity_type == "Person" and organization_name:
            org_norm = normalize_name(organization_name)
            org_matches = self._entities_by_exact_name(org_norm, "Organization")
            if len(org_matches) == 1:
                org_entity_id = org_matches[0]
            elif len(org_matches) == 0:
                org_entity_id = str(uuid.uuid4())
                self._run(
                    """
                    CREATE (o:Entity:Organization {
                        entity_id:$entity_id,
                        display_name:$display_name,
                        identity_key:$identity_key,
                        origin:'carddav',
                        identity_status:'seeded',
                        display_name_source_contact_id:$source_contact_id,
                        created_at:datetime(), updated_at:datetime()
                    })
                    MERGE (n:EntityName {normalized:$normalized})
                    ON CREATE SET n.value=$display_name, n.created_at=datetime()
                    SET n.last_seen_value=$display_name, n.updated_at=datetime()
                    WITH o, n
                    MERGE (o)-[r:HAS_NAME {source_contact_id:$source_contact_id, normalized:$normalized}]->(n)
                    SET r.kind='organization', r.preferred=true, r.active=true,
                        r.resolution_policy='exclusive', r.updated_at=datetime()
                    """,
                    entity_id=org_entity_id,
                    display_name=organization_name,
                    normalized=org_norm,
                    identity_key=organization_identity_key(organization_name),
                    source_contact_id=contact_id,
                )

            # If the organization name is ambiguous, do not guess.
            if org_entity_id:
                self._run(
                    """
                    MATCH (p:Entity:Person {entity_id:$person_id})
                    MATCH (o:Entity:Organization {entity_id:$org_id})
                    MERGE (p)-[r:WORKS_AT {source_contact_id:$contact_id}]->(o)
                    SET r.active=true,
                        r.organization_units=$organization_units,
                        r.updated_at=datetime()
                    """,
                    person_id=entity_id,
                    org_id=org_entity_id,
                    contact_id=contact_id,
                    organization_units=organization_units,
                )

        # Organization short forms (e.g. without legal form) are useful
        # even when the organization exists only through a person's ORG field.
        if org_entity_id:
            org_alias_source = display_name if entity_type == "Organization" else organization_name
            for item in generated_organization_aliases(org_alias_source):
                alias_value = str(item.get("value") or "").strip()
                alias_norm = normalize_name(alias_value)
                if not alias_norm:
                    continue
                self._run(
                    """
                    MATCH (o:Entity:Organization {entity_id:$org_id})
                    MERGE (a:SearchAlias {normalized:$normalized})
                    ON CREATE SET a.value=$value, a.created_at=datetime()
                    SET a.last_seen_value=$value, a.updated_at=datetime()
                    MERGE (o)-[r:HAS_SEARCH_ALIAS {source_contact_id:$contact_id, normalized:$normalized}]->(a)
                    SET r.kind=$kind, r.weight=$weight, r.active=true,
                        r.resolution_policy='contextual', r.updated_at=datetime()
                    """,
                    org_id=org_entity_id,
                    contact_id=contact_id,
                    normalized=alias_norm,
                    value=alias_value,
                    kind=str(item.get("kind") or "organization_short"),
                    weight=float(item.get("weight", 0.8)),
                )

        unit_entity_ids: list[str] = []
        if org_entity_id and organization_units:
            unit_org_name = display_name if entity_type == "Organization" else organization_name
            unit_entity_ids = self._ensure_organization_units(
                organization_entity_id=org_entity_id,
                organization_name=unit_org_name,
                organization_units=organization_units,
                contact_id=contact_id,
            )
            if entity_type == "Person" and unit_entity_ids:
                self._run(
                    """
                    MATCH (p:Entity:Person {entity_id:$person_id})
                    MATCH (u:Entity:OrganizationalUnit {entity_id:$unit_id})
                    MERGE (p)-[r:WORKS_IN {source_contact_id:$contact_id}]->(u)
                    SET r.active=true, r.updated_at=datetime()
                    """,
                    person_id=entity_id,
                    unit_id=unit_entity_ids[-1],
                    contact_id=contact_id,
                )

        merge_candidates = self.refresh_possible_same_as(entity_id)

        return {
            "contact_id": contact_id,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "display_name": display_name,
            "resolved_by": resolved_by,
            "organization_entity_id": org_entity_id,
            "organization_units": organization_units,
            "organization_unit_entity_ids": unit_entity_ids,
            "merge_candidates": merge_candidates,
        }

    # ------------------------------------------------------------------
    # Contact source provenance / rollback
    # ------------------------------------------------------------------

    @staticmethod
    def _contact_href_provenance(addressbook_href: str) -> tuple[str, str, str]:
        """Best-effort provenance for legacy ContactRecords.

        New imports write these fields explicitly.  This parser is only used by
        the migration/backfill command and therefore intentionally avoids any
        identity inference.
        """
        raw = str(addressbook_href or "").strip()
        if not raw:
            return "", "", ""
        parsed = urlparse(raw)
        cloud_id = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        parts = [unquote(x) for x in parsed.path.split("/") if x]
        source_user_id = ""
        try:
            idx = parts.index("users")
            if idx + 1 < len(parts):
                source_user_id = parts[idx + 1]
        except ValueError:
            pass
        slug = parts[-1] if parts else ""
        return cloud_id, source_user_id, slug

    def start_contact_import(
        self,
        run_id: str,
        *,
        cloud_id: str,
        source_user_id: str,
        addressbooks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id fehlt")
        books = [
            {
                "href": str((b or {}).get("href") or ""),
                "displayname": str((b or {}).get("displayname") or ""),
                "slug": str((b or {}).get("slug") or ""),
            }
            for b in (addressbooks or [])
        ]
        self._run(
            """
            MERGE (run:ContactImportRun {run_id:$run_id})
            ON CREATE SET run.created_at=datetime(), run.started_at=datetime()
            SET run.cloud_id=$cloud_id,
                run.source_user_id=$source_user_id,
                run.addressbooks_json=$addressbooks_json,
                run.status='running',
                run.updated_at=datetime()
            """,
            run_id=run_id,
            cloud_id=str(cloud_id or ""),
            source_user_id=str(source_user_id or ""),
            addressbooks_json=json.dumps(books, ensure_ascii=False),
        )
        return {"run_id": run_id, "cloud_id": cloud_id, "source_user_id": source_user_id, "addressbooks": books}

    def finish_contact_import(
        self,
        run_id: str,
        *,
        status: str,
        contacts_seen: int,
        contacts_written: int,
        error_count: int,
        contacts_deleted: int = 0,
    ) -> None:
        self._run(
            """
            MATCH (run:ContactImportRun {run_id:$run_id})
            SET run.status=$status,
                run.contacts_seen=$contacts_seen,
                run.contacts_written=$contacts_written,
                run.error_count=$error_count,
                run.contacts_deleted=$contacts_deleted,
                run.finished_at=datetime(),
                run.updated_at=datetime()
            """,
            run_id=str(run_id),
            status=str(status or "finished"),
            contacts_seen=int(contacts_seen),
            contacts_written=int(contacts_written),
            error_count=int(error_count),
            contacts_deleted=int(contacts_deleted),
        )

    def backfill_contact_provenance(
        self,
        *,
        default_cloud_id: str = "",
        default_source_user_id: str = "",
    ) -> dict[str, int]:
        """Fill explicit source fields on legacy ContactRecords; never changes identity."""
        rows = self._run(
            """
            MATCH (c:ContactRecord)
            RETURN c.contact_id AS contact_id,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href,
                   coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(c.addressbook_slug,'') AS addressbook_slug
            """
        )
        changed = 0
        for row in rows:
            href_cloud, href_user, href_slug = self._contact_href_provenance(str(row.get("addressbook_href") or ""))
            cloud_id = str(row.get("cloud_id") or default_cloud_id or href_cloud or "").strip()
            source_user_id = str(row.get("source_user_id") or default_source_user_id or href_user or "").strip()
            addressbook_slug = str(row.get("addressbook_slug") or href_slug or "").strip()
            if (
                cloud_id == str(row.get("cloud_id") or "")
                and source_user_id == str(row.get("source_user_id") or "")
                and addressbook_slug == str(row.get("addressbook_slug") or "")
            ):
                continue
            self._run(
                """
                MATCH (c:ContactRecord {contact_id:$contact_id})
                SET c.cloud_id=$cloud_id,
                    c.source_user_id=$source_user_id,
                    c.addressbook_slug=$addressbook_slug,
                    c.updated_at=datetime()
                """,
                contact_id=str(row.get("contact_id") or ""),
                cloud_id=cloud_id,
                source_user_id=source_user_id,
                addressbook_slug=addressbook_slug,
            )
            changed += 1
        return {"contacts_seen": len(rows), "contacts_updated": changed}

    def list_contact_sources(self) -> list[dict[str, Any]]:
        """List CardDAV sources and how strongly they overlap other sources.

        ``overlap_contact_records`` counts records whose resolved Entity is also
        described by at least one ContactRecord from another cloud/user/address
        book source.  It is diagnostic only and never triggers automatic merges.
        """
        rows = self._run(
            """
            MATCH (c:ContactRecord)
            OPTIONAL MATCH (c)-[:DESCRIBES]->(e:Entity)
            OPTIONAL MATCH (other:ContactRecord)-[:DESCRIBES]->(e)
            WHERE other.contact_id <> c.contact_id
              AND (coalesce(other.cloud_id,'') <> coalesce(c.cloud_id,'')
                   OR coalesce(other.source_user_id,'') <> coalesce(c.source_user_id,'')
                   OR coalesce(properties(other)['addressbook_href'],'') <> coalesce(properties(c)['addressbook_href'],''))
            WITH c, e, count(other) > 0 AS has_other_source
            RETURN coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(c.addressbook_name,'') AS addressbook_name,
                   coalesce(c.addressbook_slug,'') AS addressbook_slug,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href,
                   count(DISTINCT c) AS contact_records,
                   count(DISTINCT e) AS entities,
                   count(DISTINCT CASE WHEN has_other_source THEN c.contact_id ELSE null END) AS overlap_contact_records,
                   max(toString(properties(c)['last_seen_at'])) AS last_seen_at
            ORDER BY cloud_id, source_user_id, addressbook_name
            """
        )
        return [dict(row) for row in rows]

    def list_contact_import_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._run(
            """
            MATCH (run:ContactImportRun)
            OPTIONAL MATCH (run)-[:TOUCHED]->(c:ContactRecord)
            WITH run, count(DISTINCT c) AS touched_current
            RETURN run.run_id AS run_id,
                   coalesce(properties(run)['cloud_id'],'') AS cloud_id,
                   coalesce(properties(run)['source_user_id'],'') AS source_user_id,
                   coalesce(properties(run)['status'],'') AS status,
                   coalesce(properties(run)['contacts_seen'],0) AS contacts_seen,
                   coalesce(properties(run)['contacts_written'],0) AS contacts_written,
                   coalesce(properties(run)['error_count'],0) AS error_count,
                   touched_current,
                   toString(properties(run)['started_at']) AS started_at,
                   toString(properties(run)['finished_at']) AS finished_at,
                   toString(properties(run)['rolled_back_at']) AS rolled_back_at,
                   coalesce(properties(run)['rollback_deleted_contacts'],0) AS rollback_deleted_contacts
            ORDER BY properties(run)['started_at'] DESC
            LIMIT $limit
            """,
            limit=max(1, min(int(limit), 1000)),
        )
        return [dict(row) for row in rows]

    def contact_records(
        self,
        *,
        cloud_id: str = "",
        source_user_id: str = "",
        addressbook: str = "",
        import_run_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = """
            MATCH (c:ContactRecord)
            WHERE ($cloud_id='' OR coalesce(c.cloud_id,'')=$cloud_id)
              AND ($source_user_id='' OR coalesce(c.source_user_id,'')=$source_user_id)
              AND ($addressbook='' OR coalesce(c.addressbook_name,'')=$addressbook OR coalesce(c.addressbook_slug,'')=$addressbook)
              AND ($import_run_id='' OR coalesce(c.created_import_run_id,'')=$import_run_id)
            OPTIONAL MATCH (c)-[d:DESCRIBES]->(e:Entity)
            RETURN c.contact_id AS contact_id,
                   c.vcard_uid AS vcard_uid,
                   properties(c)['href'] AS href,
                   properties(c)['etag'] AS etag,
                   coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(c.addressbook_name,'') AS addressbook_name,
                   coalesce(c.addressbook_slug,'') AS addressbook_slug,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href,
                   coalesce(c.created_import_run_id,'') AS created_import_run_id,
                   coalesce(properties(c)['last_import_run_id'],'') AS last_import_run_id,
                   e.entity_id AS entity_id,
                   e.display_name AS entity_display_name,
                   labels(e) AS entity_labels,
                   coalesce(properties(d)['resolved_by'],'') AS resolved_by,
                   toString(properties(c)['created_at']) AS created_at,
                   toString(properties(c)['last_seen_at']) AS last_seen_at
            ORDER BY source_user_id, addressbook_name, entity_display_name, contact_id
        """
        params = {
            "cloud_id": str(cloud_id or ""),
            "source_user_id": str(source_user_id or ""),
            "addressbook": str(addressbook or ""),
            "import_run_id": str(import_run_id or ""),
        }
        if int(limit) > 0:
            query += "\nLIMIT $limit"
            params["limit"] = max(1, min(int(limit), 100000))
        return self._run(query, **params)

    def entity_contact_records(self, entity_id: str) -> list[dict[str, Any]]:
        return self._run(
            """
            MATCH (c:ContactRecord)-[r:DESCRIBES]->(e:Entity {entity_id:$entity_id})
            RETURN c.contact_id AS contact_id,
                   c.vcard_uid AS vcard_uid,
                   properties(c)['href'] AS href,
                   properties(c)['etag'] AS etag,
                   coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(c.addressbook_name,'') AS addressbook_name,
                   coalesce(c.addressbook_slug,'') AS addressbook_slug,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href,
                   coalesce(c.created_import_run_id,'') AS created_import_run_id,
                   coalesce(properties(c)['last_import_run_id'],'') AS last_import_run_id,
                   coalesce(properties(r)['resolved_by'],'') AS resolved_by,
                   toString(properties(c)['last_seen_at']) AS last_seen_at
            ORDER BY source_user_id, addressbook_name, contact_id
            """,
            entity_id=entity_id,
        )

    def contact_record(self, contact_id: str) -> dict[str, Any] | None:
        """Return one ContactRecord plus its currently described Entity."""
        rows = self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})
            OPTIONAL MATCH (c)-[r:DESCRIBES]->(e:Entity)
            RETURN c.contact_id AS contact_id,
                   c.vcard_uid AS vcard_uid,
                   properties(c)['href'] AS href,
                   properties(c)['etag'] AS etag,
                   coalesce(c.cloud_id,'') AS cloud_id,
                   coalesce(c.source_user_id,'') AS source_user_id,
                   coalesce(c.addressbook_name,'') AS addressbook_name,
                   coalesce(c.addressbook_slug,'') AS addressbook_slug,
                   coalesce(properties(c)['addressbook_href'],'') AS addressbook_href,
                   coalesce(c.created_import_run_id,'') AS created_import_run_id,
                   coalesce(properties(c)['last_import_run_id'],'') AS last_import_run_id,
                   e.entity_id AS entity_id,
                   e.display_name AS entity_display_name,
                   labels(e) AS entity_labels,
                   coalesce(properties(r)['resolved_by'],'') AS resolved_by,
                   toString(properties(c)['created_at']) AS created_at,
                   toString(properties(c)['last_seen_at']) AS last_seen_at
            LIMIT 1
            """,
            contact_id=str(contact_id or '').strip(),
        )
        return dict(rows[0]) if rows else None

    def _contact_affected_entity_ids(self, contact_ids: list[str]) -> list[str]:
        if not contact_ids:
            return []
        ids: set[str] = set()
        for row in self._run(
            """
            MATCH (c:ContactRecord)-[:DESCRIBES]->(e:Entity)
            WHERE c.contact_id IN $contact_ids
            RETURN DISTINCT e.entity_id AS entity_id
            """,
            contact_ids=contact_ids,
        ):
            if row.get("entity_id"):
                ids.add(str(row["entity_id"]))
        for row in self._run(
            """
            MATCH (e:Entity)-[r]-()
            WHERE r.source_contact_id IN $contact_ids
            RETURN DISTINCT e.entity_id AS entity_id
            """,
            contact_ids=contact_ids,
        ):
            if row.get("entity_id"):
                ids.add(str(row["entity_id"]))
        return sorted(ids)

    def _documents_for_affected_entities(self, entity_ids: list[str]) -> list[str]:
        if not entity_ids:
            return []
        ids: set[str] = set()
        queries = [
            """MATCH (d:Document)-[:MENTIONS]->(e:Entity) WHERE e.entity_id IN $entity_ids RETURN DISTINCT d.document_id AS document_id""",
            """MATCH (d:Document)-[r1]->(o:EntityObservation)-[r2]->(e:Entity) WHERE type(r1)='HAS_ENTITY_OBSERVATION' AND type(r2)='RESOLVED_TO' AND e.entity_id IN $entity_ids RETURN DISTINCT d.document_id AS document_id""",
            """MATCH (d:Document)-[r:MENTIONS_NAME]->(:MentionName) WHERE any(x IN coalesce(properties(r)['candidate_entity_ids'],[]) WHERE x IN $entity_ids) RETURN DISTINCT d.document_id AS document_id""",
        ]
        for query in queries:
            for row in self._run(query, entity_ids=entity_ids):
                if row.get("document_id"):
                    ids.add(str(row["document_id"]))
        return sorted(ids)

    def _remaining_contacts_for_entities(self, entity_ids: list[str]) -> dict[str, list[str]]:
        out = {entity_id: [] for entity_id in entity_ids}
        if not entity_ids:
            return out
        for row in self._run(
            """
            MATCH (c:ContactRecord)-[:DESCRIBES]->(e:Entity)
            WHERE e.entity_id IN $entity_ids
            RETURN e.entity_id AS entity_id, collect(DISTINCT c.contact_id) AS contact_ids
            """,
            entity_ids=entity_ids,
        ):
            out[str(row.get("entity_id") or "")] = [str(x) for x in (row.get("contact_ids") or []) if x]
        return out

    def remove_contact_records(self, contact_ids: list[str], *, orphaned_reason: str = "contact_source_removed") -> dict[str, Any]:
        """Remove selected CardDAV source records and only their source-derived graph material.

        Manual curator decisions and document-derived evidence are deliberately
        preserved. Entities that lose their final source of support are marked
        orphaned instead of being deleted.
        """
        ids = _compact_unique([str(x or "").strip() for x in contact_ids if str(x or "").strip()])
        if not ids:
            return {"removed_contact_records": 0, "affected_entities": [], "orphaned_entities": []}
        affected_entity_ids = self._contact_affected_entity_ids(ids)
        self._run(
            """
            MATCH ()-[r]->()
            WHERE r.source_contact_id IN $contact_ids
            DELETE r
            """,
            contact_ids=ids,
        )
        self._run(
            """
            MATCH (c:ContactRecord)
            WHERE c.contact_id IN $contact_ids
            DETACH DELETE c
            """,
            contact_ids=ids,
        )

        orphaned: list[str] = []
        for entity_id in affected_entity_ids:
            self._refresh_display_name_after_contact_removal(entity_id, ids)
            support = self._entity_support_after_contact_change(entity_id)
            if self._is_unsupported_contact_identity(support):
                self._run(
                    """
                    MATCH (e:Entity {entity_id:$entity_id})
                    SET e.identity_status='orphaned',
                        e.orphaned_reason=$orphaned_reason,
                        e.orphaned_at=datetime(),
                        e.updated_at=datetime()
                    """,
                    entity_id=entity_id,
                    orphaned_reason=str(orphaned_reason or "contact_source_removed"),
                )
                orphaned.append(entity_id)
            else:
                self.refresh_possible_same_as(entity_id)

        for label, rel_type in (
            ("EntityName", "HAS_NAME"),
            ("SearchAlias", "HAS_SEARCH_ALIAS"),
            ("EmailAddress", "HAS_EMAIL"),
            ("PhoneNumber", "HAS_PHONE"),
            ("PostalAddress", "HAS_ADDRESS"),
        ):
            self._run(
                f"""
                MATCH (v:{label})
                WHERE NOT ()-[:{rel_type}]->(v)
                DETACH DELETE v
                """
            )
        return {
            "removed_contact_records": len(ids),
            "affected_entities": affected_entity_ids,
            "orphaned_entities": orphaned,
        }

    def reconcile_contact_addressbook(
        self,
        *,
        cloud_id: str,
        source_user_id: str,
        addressbook_href: str,
        current_hrefs: list[str],
    ) -> dict[str, Any]:
        """Mirror deletions from one completely fetched CardDAV address book.

        ``current_hrefs`` comes from the CardDAV REPORT before vCard parsing, so
        malformed-but-still-present cards are never mistaken for deletions.
        """
        current = {str(x or "").strip() for x in current_hrefs if str(x or "").strip()}
        rows = self._run(
            """
            MATCH (c:ContactRecord)
            WHERE coalesce(c.cloud_id,'')=$cloud_id
              AND coalesce(c.source_user_id,'')=$source_user_id
              AND coalesce(properties(c)['addressbook_href'],'')=$addressbook_href
            RETURN c.contact_id AS contact_id, coalesce(properties(c)['href'],'') AS href
            """,
            cloud_id=str(cloud_id or ""),
            source_user_id=str(source_user_id or ""),
            addressbook_href=str(addressbook_href or ""),
        )
        stale = [str(row.get("contact_id") or "") for row in rows if str(row.get("href") or "") not in current]
        result = self.remove_contact_records(stale, orphaned_reason="carddav_source_deleted")
        return {
            "known_before": len(rows),
            "current_hrefs": len(current),
            "stale_contact_records": len(stale),
            **result,
        }

    def rollback_contacts_preview(
        self,
        *,
        cloud_id: str = "",
        source_user_id: str = "",
        addressbook: str = "",
        import_run_id: str = "",
    ) -> dict[str, Any]:
        if not any(str(x or "").strip() for x in (cloud_id, source_user_id, addressbook, import_run_id)):
            raise ValueError("Mindestens --cloud, --source-user, --addressbook oder --import-run ist erforderlich")
        records = self.contact_records(
            cloud_id=cloud_id,
            source_user_id=source_user_id,
            addressbook=addressbook,
            import_run_id=import_run_id,
            limit=0,
        )
        contact_ids = [str(r.get("contact_id") or "") for r in records if r.get("contact_id")]
        affected_entities = self._contact_affected_entity_ids(contact_ids)
        current_contacts = self._remaining_contacts_for_entities(affected_entities)
        selected = set(contact_ids)
        entity_rows = []
        for entity_id in affected_entities:
            entity = self._entity_curation_summary(entity_id) or {"entity_id": entity_id}
            remaining = [x for x in current_contacts.get(entity_id, []) if x not in selected]
            entity_rows.append({**entity, "remaining_contact_records_after": len(remaining)})
        relink_ids = self._documents_for_affected_entities(affected_entities)
        touched_by_run = 0
        if import_run_id:
            rows = self._run(
                """
                MATCH (:ContactImportRun {run_id:$run_id})-[:TOUCHED]->(c:ContactRecord)
                RETURN count(DISTINCT c) AS count
                """,
                run_id=str(import_run_id),
            )
            touched_by_run = int(rows[0].get("count") or 0) if rows else 0
        return {
            "action": "rollback_contacts",
            "scope": {
                "cloud_id": str(cloud_id or ""),
                "source_user_id": str(source_user_id or ""),
                "addressbook": str(addressbook or ""),
                "import_run_id": str(import_run_id or ""),
            },
            "contact_records_selected": len(records),
            "contacts": records,
            "affected_entities": entity_rows,
            "entities_with_other_contact_sources": sum(1 for e in entity_rows if int(e.get("remaining_contact_records_after") or 0) > 0),
            "relink_document_count": len(relink_ids),
            "relink_document_ids": relink_ids,
            "import_run_touched_current": touched_by_run,
            "note": (
                "Bei --import-run werden absichtlich nur ContactRecords entfernt, die in diesem Lauf neu angelegt wurden; "
                "bereits vorher vorhandene und nur erneut berührte Records bleiben erhalten. Manuelle Merge-/Curator-Entscheidungen bleiben bestehen."
            ),
        }

    def _refresh_display_name_after_contact_removal(self, entity_id: str, removed_contact_ids: list[str]) -> None:
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            RETURN coalesce(properties(e)['identity_status'],'') AS identity_status,
                   coalesce(properties(e)['display_name_source_contact_id'],'') AS source_contact_id
            """,
            entity_id=entity_id,
        )
        if not rows:
            return
        status = str(rows[0].get("identity_status") or "")
        source_contact_id = str(rows[0].get("source_contact_id") or "")
        if status == "confirmed" or source_contact_id not in set(removed_contact_ids):
            return
        names = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_NAME]->(n:EntityName)
            WHERE coalesce(properties(r)['active'],true)=true
            RETURN coalesce(properties(n)['last_seen_value'],properties(n)['value']) AS value,
                   coalesce(properties(r)['preferred'],false) AS preferred,
                   CASE WHEN properties(r)['source_curator']='manual' THEN 0
                        WHEN properties(r)['source_document_id'] IS NOT NULL THEN 1 ELSE 2 END AS source_rank,
                   coalesce(properties(r)['source_contact_id'],'') AS source_contact_id
            ORDER BY source_rank ASC, preferred DESC, value ASC
            """,
            entity_id=entity_id,
        )
        if not names:
            return
        chosen = str(names[0].get("value") or "").strip()
        if chosen:
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                SET e.display_name=$display_name,
                    e.display_name_source_contact_id=CASE WHEN $source_contact_id<>'' THEN $source_contact_id ELSE null END,
                    e.updated_at=datetime()
                """,
                entity_id=entity_id,
                display_name=chosen,
                source_contact_id=str(names[0].get("source_contact_id") or ""),
            )

    def _entity_support_after_contact_change(self, entity_id: str) -> dict[str, Any] | None:
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            RETURN coalesce(properties(e)['origin'],'') AS origin,
                   coalesce(properties(e)['identity_status'],'') AS identity_status,
                   size([(c:ContactRecord)-[:DESCRIBES]->(e) | c]) AS contact_records,
                   size([(o:EntityObservation)-[:RESOLVED_TO]->(e) | o]) AS document_observations,
                   size([(e)-[r]-() WHERE properties(r)['source_contact_id'] IS NOT NULL | r]) AS contact_source_relations,
                   size([(e)-[r:HAS_NAME]->() WHERE properties(r)['source_document_id'] IS NOT NULL | r]) AS document_name_relations,
                   size([(e)-[r:HAS_SEARCH_ALIAS]->() WHERE properties(r)['source_document_id'] IS NOT NULL | r]) AS document_alias_relations
            """,
            entity_id=entity_id,
        )
        return dict(rows[0]) if rows else None

    @staticmethod
    def _is_unsupported_contact_identity(support: dict[str, Any] | None) -> bool:
        if not support:
            return False
        if str(support.get("identity_status") or "") in {"confirmed", "merged"}:
            return False
        return (
            int(support.get("contact_records") or 0) == 0
            and int(support.get("contact_source_relations") or 0) == 0
            and int(support.get("document_observations") or 0) == 0
            and int(support.get("document_name_relations") or 0) == 0
            and int(support.get("document_alias_relations") or 0) == 0
        )

    def rollback_contacts(
        self,
        *,
        cloud_id: str = "",
        source_user_id: str = "",
        addressbook: str = "",
        import_run_id: str = "",
    ) -> dict[str, Any]:
        preview = self.rollback_contacts_preview(
            cloud_id=cloud_id,
            source_user_id=source_user_id,
            addressbook=addressbook,
            import_run_id=import_run_id,
        )
        contact_ids = [str(r.get("contact_id") or "") for r in preview.get("contacts", []) if r.get("contact_id")]
        affected_entity_ids = [str(e.get("entity_id") or "") for e in preview.get("affected_entities", []) if e.get("entity_id")]
        if not contact_ids:
            return {**preview, "status": "nothing_to_remove", "removed_contact_records": 0, "orphaned_entities": []}

        removal = self.remove_contact_records(contact_ids, orphaned_reason="contact_source_rollback")
        orphaned = list(removal.get("orphaned_entities") or [])

        if import_run_id:
            self._run(
                """
                MATCH (run:ContactImportRun {run_id:$run_id})
                SET run.status='rolled_back',
                    run.rolled_back_at=datetime(),
                    run.rollback_deleted_contacts=$deleted,
                    run.updated_at=datetime()
                """,
                run_id=str(import_run_id),
                deleted=len(contact_ids),
            )

        return {
            **preview,
            "status": "rolled_back",
            "removed_contact_records": len(contact_ids),
            "orphaned_entities": orphaned,
        }

    def reassign_contact_preview(self, contact_id: str, target_entity_id: str) -> dict[str, Any]:
        rows = self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})-[:DESCRIBES]->(old:Entity)
            MATCH (target:Entity {entity_id:$target_entity_id})
            RETURN old.entity_id AS old_entity_id, old.display_name AS old_display_name,
                   labels(old) AS old_labels, target.display_name AS target_display_name,
                   labels(target) AS target_labels,
                   coalesce(properties(target)['identity_status'],'') AS target_status
            """,
            contact_id=str(contact_id),
            target_entity_id=str(target_entity_id),
        )
        if not rows:
            raise ValueError("ContactRecord oder Ziel-Entity nicht gefunden")
        row = dict(rows[0])
        if str(row.get("target_status") or "") in {"merged", "orphaned"}:
            raise ValueError("Ziel-Entity ist nicht aktiv")
        old_type = "Organization" if "Organization" in (row.get("old_labels") or []) else "Person"
        target_type = "Organization" if "Organization" in (row.get("target_labels") or []) else "Person"
        if old_type != target_type:
            raise ValueError(f"Typkonflikt: ContactRecord beschreibt {old_type}, Ziel ist {target_type}")
        affected = self._documents_for_affected_entities([str(row.get("old_entity_id") or ""), str(target_entity_id)])
        return {
            "action": "reassign_contact",
            "contact_id": str(contact_id),
            "old_entity_id": row.get("old_entity_id"),
            "old_display_name": row.get("old_display_name"),
            "target_entity_id": str(target_entity_id),
            "target_display_name": row.get("target_display_name"),
            "entity_type": old_type,
            "relink_document_ids": affected,
            "note": "Nur die Entity-Zuordnung dieses ContactRecords wird geändert; die CardDAV-Quelle selbst bleibt unverändert.",
        }

    def reassign_contact(self, contact_id: str, target_entity_id: str) -> dict[str, Any]:
        preview = self.reassign_contact_preview(contact_id, target_entity_id)
        old_entity_id = str(preview.get("old_entity_id") or "")
        contact_id = str(contact_id)
        target_entity_id = str(target_entity_id)

        # Move contact-owned identity/value edges from the old identity to target.
        for rel_type in ("HAS_NAME", "HAS_SEARCH_ALIAS", "HAS_EMAIL", "HAS_PHONE", "HAS_ADDRESS"):
            self._run(
                f"""
                MATCH (old:Entity {{entity_id:$old_entity_id}})-[r:{rel_type}]->(v)
                WHERE r.source_contact_id=$contact_id
                MATCH (target:Entity {{entity_id:$target_entity_id}})
                MERGE (target)-[nr:{rel_type} {{source_contact_id:$contact_id, normalized:r.normalized}}]->(v)
                SET nr = properties(r)
                DELETE r
                """,
                old_entity_id=old_entity_id,
                target_entity_id=target_entity_id,
                contact_id=contact_id,
            )
        for rel_type in PERSON_AFFILIATION_RELATIONS:
            self._run(
                f"""
                MATCH (old:Entity {{entity_id:$old_entity_id}})-[r:{rel_type}]->(v)
                WHERE r.source_contact_id=$contact_id
                MATCH (target:Entity {{entity_id:$target_entity_id}})
                MERGE (target)-[nr:{rel_type} {{source_contact_id:$contact_id}}]->(v)
                SET nr = properties(r)
                DELETE r
                """,
                old_entity_id=old_entity_id,
                target_entity_id=target_entity_id,
                contact_id=contact_id,
            )
        self._run(
            """
            MATCH (u:Entity:OrganizationalUnit)-[r:PART_OF]->(old:Entity {entity_id:$old_entity_id})
            WHERE r.source_contact_id=$contact_id
            MATCH (target:Entity {entity_id:$target_entity_id})
            MERGE (u)-[nr:PART_OF {source_contact_id:$contact_id}]->(target)
            SET nr=properties(r)
            DELETE r
            """,
            old_entity_id=old_entity_id,
            target_entity_id=target_entity_id,
            contact_id=contact_id,
        )
        self._run(
            """
            MATCH (c:ContactRecord {contact_id:$contact_id})-[r:DESCRIBES]->(old:Entity {entity_id:$old_entity_id})
            MATCH (target:Entity {entity_id:$target_entity_id})
            DELETE r
            MERGE (c)-[nr:DESCRIBES]->(target)
            SET nr.resolved_by='manual_contact_reassignment', nr.updated_at=datetime(),
                c.curator_reassigned_at=datetime(),
                c.curator_reassigned_from_entity_id=$old_entity_id,
                c.curator_reassigned_to_entity_id=$target_entity_id,
                c.updated_at=datetime(),
                target.identity_status='confirmed',
                target.confirmation_method='manual_contact_reassignment',
                target.confirmed_at=datetime(),
                target.updated_at=datetime()
            """,
            contact_id=contact_id,
            old_entity_id=old_entity_id,
            target_entity_id=target_entity_id,
        )
        # If the old Entity's display name came from this ContactRecord, choose
        # a remaining active real name before deciding whether the identity is
        # now contact-only/orphaned.
        self._refresh_display_name_after_contact_removal(old_entity_id, [contact_id])

        # Old contact-only identity becomes inactive, but manual confirmations are preserved.
        orphaned = False
        support = self._entity_support_after_contact_change(old_entity_id)
        if self._is_unsupported_contact_identity(support):
            self._run(
                """MATCH (e:Entity {entity_id:$entity_id}) SET e.identity_status='orphaned', e.orphaned_reason='manual_contact_reassignment', e.orphaned_at=datetime(), e.updated_at=datetime()""",
                entity_id=old_entity_id,
            )
            orphaned = True
        if not orphaned:
            self.refresh_possible_same_as(old_entity_id)
        self.refresh_possible_same_as(target_entity_id)
        return {**preview, "status": "reassigned", "old_entity_orphaned": orphaned}

    # ------------------------------------------------------------------
    # Query-side identity lookup
    # ------------------------------------------------------------------

    def name_forms(
        self,
        *,
        include_inactive_names: bool = True,
        purpose: str = "query",
    ) -> list[dict[str, Any]]:
        """Return global identity/search forms with explicit resolution policy.

        purpose=query: exclusive + contextual + search_only forms are visible.
        purpose=ingestion: only exclusive forms may create hard exact identity
        resolution / Document->MENTIONS. document_only is never global.
        """
        purpose = str(purpose or "query").strip().lower()
        if purpose not in {"query", "ingestion"}:
            raise ValueError("purpose must be 'query' or 'ingestion'")
        active_clause = (
            "AND r.active = true"
            if purpose == "ingestion" else
            ("AND coalesce(properties(r)['retired_by'],'') <> 'manual_name_correction'"
             if include_inactive_names else "AND r.active = true")
        )
        policy_clause_name = (
            "AND coalesce(r.resolution_policy,'exclusive')='exclusive'"
            if purpose == "ingestion" else
            "AND coalesce(r.resolution_policy,'exclusive') <> 'document_only'"
        )
        policy_clause_alias = (
            "AND coalesce(r.resolution_policy,'contextual')='exclusive'"
            if purpose == "ingestion" else
            "AND coalesce(r.resolution_policy,'contextual') <> 'document_only'"
        )
        names = self._run(
            f"""
            MATCH (e:Entity)-[r]->(n:EntityName)
            WHERE type(r)='HAS_NAME' AND n.normalized <> '' {active_clause} {policy_clause_name}
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN n.normalized AS normalized,
                   coalesce(properties(n)['last_seen_value'], properties(n)['value']) AS value,
                   e.entity_id AS entity_id,
                   labels(e) AS labels,
                   e.display_name AS display_name,
                   coalesce(properties(e)['entity_kind'],'') AS entity_kind,
                   coalesce(properties(r)['preferred'],false) AS preferred,
                   coalesce(properties(r)['active'],true) AS active,
                   1.0 AS weight,
                   'name' AS form_type,
                   coalesce(properties(r)['resolution_policy'],'exclusive') AS resolution_policy
            """
        )
        aliases = self._run(
            f"""
            MATCH (e:Entity)-[r]->(a:SearchAlias)
            WHERE type(r)='HAS_SEARCH_ALIAS' AND coalesce(properties(r)['active'],true)=true AND a.normalized <> '' {policy_clause_alias}
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN a.normalized AS normalized,
                   coalesce(properties(a)['last_seen_value'], properties(a)['value']) AS value,
                   e.entity_id AS entity_id,
                   labels(e) AS labels,
                   e.display_name AS display_name,
                   coalesce(properties(e)['entity_kind'],'') AS entity_kind,
                   false AS preferred,
                   true AS active,
                   coalesce(properties(r)['weight'],0.5) AS weight,
                   'alias' AS form_type,
                   coalesce(properties(r)['resolution_policy'],'contextual') AS resolution_policy
            """
        )

        # Derived organization short forms are useful for user-query recall, but
        # are deliberately NOT implicit ingestion identity evidence.
        derived: list[dict[str, Any]] = []
        if purpose == "query":
            for row in names:
                labels = list(row.get("labels") or [])
                if "Organization" not in labels:
                    continue
                source_name = str(row.get("value") or row.get("display_name") or "").strip()
                if not source_name:
                    continue
                for item in generated_organization_aliases(source_name):
                    derived.append({
                        "normalized": str(item.get("normalized") or normalize_name(item.get("value") or "")),
                        "value": str(item.get("value") or ""),
                        "entity_id": row.get("entity_id"),
                        "labels": labels,
                        "display_name": row.get("display_name"),
                        "entity_kind": row.get("entity_kind"),
                        "preferred": False,
                        "active": True,
                        "weight": float(item.get("weight", 0.5)),
                        "form_type": "derived_alias",
                        "resolution_policy": "contextual",
                    })

        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for row in [*names, *aliases, *derived]:
            key = (str(row.get("entity_id") or ""), str(row.get("normalized") or ""))
            if not all(key):
                continue
            old = combined.get(key)
            if old is None or float(row.get("weight", 0.0)) > float(old.get("weight", 0.0)):
                combined[key] = row
        return list(combined.values())

    def same_as_component_ids(self, entity_id: str, *, max_hops: int = 16) -> list[str]:
        """Return the active SAME_AS equivalence component containing one Entity.

        SAME_AS is non-destructive identity equivalence: every Entity keeps its
        own lifecycle and provenance.  Traversal is bounded and implemented
        without a typed variable-length Cypher pattern so older/sparse stores
        do not emit unknown-relationship warnings before the first decision.
        """
        root = str(entity_id or "").strip()
        if not root:
            return []
        active = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            WHERE coalesce(properties(e)['identity_status'],'') <> 'merged'
              AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN e.entity_id AS entity_id
            LIMIT 1
            """,
            entity_id=root,
        )
        if not active:
            return []

        seen = {root}
        frontier = [root]
        for _ in range(max(1, min(int(max_hops or 16), 64))):
            if not frontier:
                break
            rows = self._run(
                """
                UNWIND $frontier AS current_id
                MATCH (e:Entity {entity_id:current_id})-[r]-(peer:Entity)
                WHERE type(r)='SAME_AS'
                  AND coalesce(properties(r)['active'],true)=true
                  AND coalesce(properties(peer)['identity_status'],'') <> 'merged'
                  AND coalesce(properties(peer)['identity_status'],'') <> 'orphaned'
                RETURN DISTINCT peer.entity_id AS entity_id
                """,
                frontier=frontier,
            )
            next_frontier = []
            for row in rows:
                peer_id = str(row.get("entity_id") or "").strip()
                if peer_id and peer_id not in seen:
                    seen.add(peer_id)
                    next_frontier.append(peer_id)
            frontier = next_frontier
        return sorted(seen)

    def entity_search_forms(self, entity_id: str) -> list[dict[str, Any]]:
        entity_ids = self.same_as_component_ids(entity_id)
        if not entity_ids:
            return []
        rows = self._run(
            """
            MATCH (e:Entity)
            WHERE e.entity_id IN $entity_ids
            OPTIONAL MATCH (e)-[rn:HAS_NAME]->(n:EntityName)
            WITH e, collect(CASE WHEN n IS NULL THEN null ELSE {
                value:coalesce(properties(n)['last_seen_value'],properties(n)['value']), normalized:n.normalized,
                weight:CASE WHEN coalesce(properties(rn)['preferred'],false) THEN 1.0 ELSE 0.92 END,
                kind:coalesce(properties(rn)['kind'],'name'), active:coalesce(properties(rn)['active'],true), source:'name',
                source_entity_id:e.entity_id,
                resolution_policy:coalesce(properties(rn)['resolution_policy'],'exclusive')
            } END) AS names
            OPTIONAL MATCH (e)-[ra:HAS_SEARCH_ALIAS]->(a:SearchAlias)
            WITH e, names, collect(CASE WHEN a IS NULL THEN null ELSE {
                value:coalesce(properties(a)['last_seen_value'],properties(a)['value']), normalized:a.normalized,
                weight:coalesce(properties(ra)['weight'],0.5), kind:coalesce(properties(ra)['kind'],'alias'),
                active:coalesce(properties(ra)['active'],true), source:'alias',
                source_entity_id:e.entity_id,
                resolution_policy:coalesce(properties(ra)['resolution_policy'],'contextual')
            } END) AS aliases
            RETURN [x IN names + aliases WHERE x IS NOT NULL AND coalesce(x.resolution_policy,'contextual') <> 'document_only'] AS forms
            """,
            entity_ids=entity_ids,
        )
        forms = []
        for row in rows:
            forms.extend(list(row.get("forms") or []))

        # The same spelling can occur on multiple equivalent identities. Search
        # it once using the strongest available form while retaining provenance.
        combined: dict[str, dict[str, Any]] = {}
        for form in forms:
            key = str(form.get("normalized") or normalize_name(form.get("value") or "")).strip()
            if not key:
                continue
            old = combined.get(key)
            if old is None or float(form.get("weight") or 0.0) > float(old.get("weight") or 0.0):
                combined[key] = dict(form)
        result = list(combined.values())
        result.sort(key=lambda x: (bool(x.get("active")), float(x.get("weight", 0.0))), reverse=True)
        return result


    def identity_profiles(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        """Return provenance-aware profiles for duplicate suggestions.

        This is read-only.  It deliberately exposes CardDAV-derived evidence
        without declaring any pair of entities identical.
        """
        type_clause = ""
        if entity_type == "Person":
            type_clause = "AND e:Person"
        elif entity_type == "Organization":
            type_clause = "AND e:Organization"

        return self._run(
            f"""
            MATCH (e:Entity)
            WHERE (e:Person OR e:Organization) {type_clause}
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            RETURN
                properties(e)['entity_id'] AS entity_id,
                labels(e) AS labels,
                properties(e)['display_name'] AS display_name,
                [(e)-[r:HAS_NAME]->(n:EntityName)
                    WHERE coalesce(properties(r)['active'],true)=true | {{
                        value:coalesce(properties(n)['last_seen_value'],properties(n)['value']),
                        normalized:n.normalized,
                        preferred:coalesce(properties(r)['preferred'],false),
                        kind:coalesce(properties(r)['kind'],'name')
                    }}] AS names,
                [(e)-[r:HAS_EMAIL]->(v:EmailAddress)
                    WHERE coalesce(properties(r)['active'],true)=true | v.normalized] AS emails,
                [(e)-[r:HAS_PHONE]->(v:PhoneNumber)
                    WHERE coalesce(properties(r)['active'],true)=true | v.normalized] AS phones,
                [(e)-[r:HAS_ADDRESS]->(v:PostalAddress)
                    WHERE coalesce(properties(r)['active'],true)=true | v.normalized] AS addresses,
                [(e)-[r:WORKS_AT]->(o:Entity:Organization)
                    WHERE coalesce(properties(r)['active'],true)=true | {{
                        entity_id:properties(o)['entity_id'],
                        display_name:properties(o)['display_name']
                    }}] AS organizations,
                [(c:ContactRecord)-[:DESCRIBES]->(e) | {{
                    contact_id:c.contact_id,
                    href:properties(c)['href'],
                    cloud_id:coalesce(c.cloud_id,''),
                    source_user_id:coalesce(c.source_user_id,''),
                    addressbook_name:c.addressbook_name,
                    addressbook_slug:coalesce(c.addressbook_slug,'')
                }}] AS contact_records
            ORDER BY properties(e)['display_name']
            """
        )


    def candidate_context_links(
        self,
        candidate_ids: list[str],
        context_entity_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Return conservative direct identity-graph links for disambiguation.

        This intentionally uses only seed-level structural relations. Document
        MENTIONS/Claims will be added later and must not silently influence
        identity resolution in phase 1.
        """
        candidate_ids = [str(x) for x in candidate_ids if x]
        context_entity_ids = [str(x) for x in context_entity_ids if x]
        if not candidate_ids or not context_entity_ids:
            return {}
        rows = self._run(
            """
            UNWIND $candidate_ids AS candidate_id
            MATCH (c:Entity {entity_id:candidate_id})
            UNWIND $context_ids AS context_id
            MATCH (x:Entity {entity_id:context_id})
            OPTIONAL MATCH (c)-[r]-(x)
            WHERE type(r) IN $relation_types
              AND coalesce(r.active,true)=true
            WITH candidate_id, context_id,
                 [rel IN collect(DISTINCT r) WHERE rel IS NOT NULL | type(rel)] AS rel_types
            WHERE size(rel_types) > 0
            RETURN candidate_id, context_id, rel_types
            """,
            candidate_ids=candidate_ids,
            context_ids=context_entity_ids,
            relation_types=IDENTITY_CONTEXT_RELATIONS,
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(str(row["candidate_id"]), []).append({
                "context_entity_id": str(row["context_id"]),
                "relations": list(row.get("rel_types") or []),
            })
        return out

    # ------------------------------------------------------------------
    # Graph v2a.1: document-driven entity discovery and conservative admission
    # ------------------------------------------------------------------

    def _entities_by_exact_form(
        self, normalized: str, entity_type: str, *, purpose: str = "ingestion"
    ) -> list[dict[str, Any]]:
        """Return exact forms eligible for the requested resolver purpose.

        Ingestion is intentionally strict: only relationships explicitly marked
        exclusive are identity evidence. Contextual/search-only aliases remain
        available to the user-query resolver through name_forms(purpose='query').
        """
        if not normalized:
            return []
        entity_type = "Organization" if entity_type == "Organization" else "Person"
        purpose = str(purpose or "ingestion").strip().lower()
        if purpose == "ingestion":
            name_policy = "coalesce(rn.resolution_policy,'exclusive')='exclusive'"
            alias_policy = "coalesce(ra.resolution_policy,'contextual')='exclusive'"
        else:
            name_policy = "coalesce(rn.resolution_policy,'exclusive') <> 'document_only'"
            alias_policy = "coalesce(ra.resolution_policy,'contextual') <> 'document_only'"
        rows = self._run(
            f"""
            MATCH (e:Entity)
            WHERE $entity_type IN labels(e)
              AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            OPTIONAL MATCH (e)-[rn:HAS_NAME]->(n:EntityName {{normalized:$normalized}})
            OPTIONAL MATCH (e)-[ra:HAS_SEARCH_ALIAS]->(a:SearchAlias {{normalized:$normalized}})
            WITH e, n, rn, a, ra
            WHERE (n IS NOT NULL AND coalesce(properties(rn)['active'],true)=true AND {name_policy})
               OR (a IS NOT NULL AND coalesce(properties(ra)['active'],true)=true AND {alias_policy})
            RETURN DISTINCT e.entity_id AS entity_id,
                   e.display_name AS display_name, labels(e) AS labels,
                   coalesce(properties(e)['origin'],'') AS origin,
                   coalesce(properties(e)['identity_status'],'') AS identity_status
            ORDER BY e.display_name, e.entity_id
            """,
            normalized=normalized, entity_type=entity_type,
        )
        return rows

    def record_document_entity_observation(
        self,
        *,
        document_id: str,
        entity_type: str,
        canonical_name: str,
        observed_text: str,
        context_text: str,
        confidence: float,
        relevant_actor: bool,
        mention_context: str = "actor",
        entity_kind: str = "Other",
        extractor: str,
        eligible: bool,
        allow_create: bool,
        admission_reason: str,
    ) -> dict[str, Any]:
        """Persist one grounded observation, then resolve/create conservatively.

        The observation is the durable first-class result of the LLM pass. It is
        deliberately distinct from an Entity. Hard-rejected/incidental strings
        remain inspectable as observations but can never become identities.
        Plausible weak observations may resolve an already-known exact identity
        while still being barred from creating a new one.
        """
        entity_type = "Organization" if entity_type == "Organization" else "Person"
        clean_name = str(canonical_name or observed_text or "").strip()
        normalized = normalize_name(clean_name)
        observation_id = hashlib.sha256(
            f"{document_id}\0{entity_type}\0{normalized}".encode("utf-8", errors="replace")
        ).hexdigest()[:40]

        # Record the grounded observation before any identity decision. This is
        # useful for later curation and for learning when a stronger observation
        # of the same identity appears in another document.
        self._run(
            """
            MATCH (d:Document {document_id:$document_id})
            MERGE (o:EntityObservation {observation_id:$observation_id})
            ON CREATE SET o.created_at=datetime(), o.first_seen_at=datetime(),
                          o.curator_status='',
                          o.curator_target_entity_id='',
                          o.curator_reason=''
            SET o.document_id=$document_id,
                o.canonical_name=$canonical_name,
                o.observed_text=$observed_text,
                o.context_text=$context_text,
                o.normalized=$normalized,
                o.suggested_type=$entity_type,
                o.confidence=$confidence,
                o.relevant_actor=$relevant_actor,
                o.mention_context=$mention_context,
                o.entity_kind=$entity_kind,
                o.eligible=$eligible,
                o.allow_create=$allow_create,
                o.admission_reason=$admission_reason,
                o.extractor=$extractor,
                o.last_seen_at=datetime(),
                o.updated_at=datetime()
            MERGE (d)-[r:HAS_ENTITY_OBSERVATION]->(o)
            SET r.extractor=$extractor, r.updated_at=datetime()
            """,
            document_id=document_id,
            observation_id=observation_id,
            canonical_name=clean_name,
            observed_text=str(observed_text or clean_name),
            context_text=str(context_text or observed_text or clean_name)[:1000],
            normalized=normalized,
            entity_type=entity_type,
            confidence=float(confidence),
            relevant_actor=bool(relevant_actor),
            mention_context=str(mention_context or "incidental"),
            entity_kind=str(entity_kind or ("Person" if entity_type == "Person" else "Other")),
            eligible=bool(eligible),
            allow_create=bool(allow_create),
            admission_reason=str(admission_reason or ""),
            extractor=extractor,
        )

        # Manual curation has precedence over repeated LLM extraction. A rebuild
        # may revisit the same document, but it must not silently undo an
        # explicit human decision about this grounded observation.
        manual_rows = self._run(
            """
            MATCH (o:EntityObservation {observation_id:$id})
            RETURN coalesce(properties(o)['curator_status'],'') AS curator_status,
                   coalesce(properties(o)['curator_target_entity_id'],'') AS target_entity_id,
                   coalesce(properties(o)['curator_reason'],'') AS curator_reason
            """,
            id=observation_id,
        )
        manual = dict(manual_rows[0]) if manual_rows else {}
        curator_status = str(manual.get("curator_status") or "")
        if curator_status == "manual_not_entity":
            self._run(
                """
                MATCH (o:EntityObservation {observation_id:$id})
                OPTIONAL MATCH (o)-[r:RESOLVED_TO]->()
                DELETE r
                SET o.status='rejected',
                    o.rejection_reason='manual_not_an_entity',
                    o.candidate_entity_ids=[],
                    o.updated_at=datetime()
                """,
                id=observation_id,
            )
            return {
                "status": "rejected",
                "reason": "manual_not_an_entity",
                "observation_id": observation_id,
                "name": clean_name,
                "normalized": normalized,
                "entity_type": entity_type,
            }
        if curator_status in {"corrected_observation", "research_finding_entity"}:
            target_id = str(manual.get("target_entity_id") or "")
            target_rows = self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                WHERE coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
                RETURN e.display_name AS display_name, labels(e) AS labels
                """,
                entity_id=target_id,
            ) if target_id else []
            if target_rows:
                self._run(
                    """
                    MATCH (o:EntityObservation {observation_id:$observation_id})
                    MATCH (e:Entity {entity_id:$entity_id})
                    OPTIONAL MATCH (o)-[old:RESOLVED_TO]->()
                    DELETE old
                    MERGE (o)-[r:RESOLVED_TO]->(e)
                    SET r.resolved_by='manual_observation_correction',
                        r.updated_at=datetime(),
                        o.status='corrected', o.rejection_reason=null,
                        o.candidate_entity_ids=[$entity_id], o.updated_at=datetime()
                    """,
                    observation_id=observation_id,
                    entity_id=target_id,
                )
                return {
                    "status": "corrected",
                    "reason": str(manual.get("curator_reason") or "manual_observation_correction"),
                    "observation_id": observation_id,
                    "entity_id": target_id,
                    "display_name": str(target_rows[0].get("display_name") or clean_name),
                    "name": clean_name,
                    "normalized": normalized,
                    "entity_type": entity_type,
                }

        if not normalized:
            self._run(
                "MATCH (o:EntityObservation {observation_id:$id}) SET o.status='rejected', o.rejection_reason='empty_name', o.updated_at=datetime()",
                id=observation_id,
            )
            return {
                "status": "rejected", "reason": "empty_name", "observation_id": observation_id,
                "name": clean_name, "entity_type": entity_type,
            }

        if not eligible:
            self._run(
                """
                MATCH (o:EntityObservation {observation_id:$id})
                OPTIONAL MATCH (o)-[r:RESOLVED_TO]->()
                DELETE r
                SET o.status='rejected', o.rejection_reason=$reason,
                    o.candidate_entity_ids=[], o.updated_at=datetime()
                """,
                id=observation_id,
                reason=str(admission_reason or "not_eligible"),
            )
            return {
                "status": "rejected",
                "reason": str(admission_reason or "not_eligible"),
                "observation_id": observation_id,
                "name": clean_name,
                "normalized": normalized,
                "entity_type": entity_type,
            }

        if entity_type == "Organization":
            identity_key = organization_identity_key(clean_name)
            identity_ids = self._entities_by_identity_key(identity_key, "Organization")
            if len(identity_ids) == 1:
                entity_id = identity_ids[0]
                rows = self._run(
                    "MATCH (e:Entity {entity_id:$id}) RETURN e.display_name AS display_name",
                    id=entity_id,
                )
                self._run(
                    """
                    MATCH (o:EntityObservation {observation_id:$observation_id})
                    MATCH (e:Entity {entity_id:$entity_id})
                    OPTIONAL MATCH (o)-[old:RESOLVED_TO]->()
                    DELETE old
                    MERGE (o)-[r:RESOLVED_TO]->(e)
                    SET r.resolved_by='organization_identity_key', r.updated_at=datetime(),
                        o.status='resolved_existing', o.rejection_reason=null,
                        o.candidate_entity_ids=[$entity_id], o.updated_at=datetime(),
                        e.last_document_seen_at=datetime(), e.updated_at=datetime()
                    """,
                    observation_id=observation_id,
                    entity_id=entity_id,
                )
                return {
                    "status": "resolved_existing",
                    "reason": "organization_identity_key",
                    "observation_id": observation_id,
                    "entity_id": entity_id,
                    "display_name": str(rows[0].get("display_name") if rows else clean_name),
                    "name": clean_name,
                    "normalized": normalized,
                    "identity_key": identity_key,
                    "entity_type": entity_type,
                }
            if len(identity_ids) > 1:
                self._run(
                    """
                    MATCH (o:EntityObservation {observation_id:$id})
                    OPTIONAL MATCH (o)-[r:RESOLVED_TO]->()
                    DELETE r
                    SET o.status='ambiguous', o.rejection_reason=null,
                        o.candidate_entity_ids=$candidate_ids, o.updated_at=datetime()
                    """,
                    id=observation_id,
                    candidate_ids=identity_ids,
                )
                return {
                    "status": "ambiguous",
                    "reason": "multiple_organization_identity_key_matches",
                    "observation_id": observation_id,
                    "name": clean_name,
                    "normalized": normalized,
                    "identity_key": identity_key,
                    "entity_type": entity_type,
                    "candidate_entity_ids": identity_ids,
                }

        matches = self._entities_by_exact_form(normalized, entity_type)
        if len(matches) == 1:
            entity_id = str(matches[0]["entity_id"])
            self._run(
                """
                MATCH (o:EntityObservation {observation_id:$observation_id})
                MATCH (e:Entity {entity_id:$entity_id})
                OPTIONAL MATCH (o)-[old:RESOLVED_TO]->()
                DELETE old
                MERGE (o)-[r:RESOLVED_TO]->(e)
                SET r.resolved_by='exact_name_or_alias', r.updated_at=datetime(),
                    o.status='resolved_existing', o.rejection_reason=null,
                    o.candidate_entity_ids=[$entity_id], o.updated_at=datetime(),
                    e.last_document_seen_at=datetime(), e.updated_at=datetime()
                """,
                observation_id=observation_id,
                entity_id=entity_id,
            )
            return {
                "status": "resolved_existing",
                "reason": "exact_name_or_alias",
                "observation_id": observation_id,
                "entity_id": entity_id,
                "display_name": str(matches[0].get("display_name") or clean_name),
                "name": clean_name,
                "normalized": normalized,
                "entity_type": entity_type,
            }

        if len(matches) > 1:
            candidate_ids = [str(row.get("entity_id") or "") for row in matches if row.get("entity_id")]
            self._run(
                """
                MATCH (o:EntityObservation {observation_id:$id})
                OPTIONAL MATCH (o)-[r:RESOLVED_TO]->()
                DELETE r
                SET o.status='ambiguous', o.rejection_reason=null,
                    o.candidate_entity_ids=$candidate_ids, o.updated_at=datetime()
                """,
                id=observation_id,
                candidate_ids=candidate_ids,
            )
            return {
                "status": "ambiguous",
                "reason": "multiple_exact_name_or_alias_matches",
                "observation_id": observation_id,
                "name": clean_name,
                "normalized": normalized,
                "entity_type": entity_type,
                "candidate_entity_ids": candidate_ids,
                "candidate_display_names": [str(row.get("display_name") or "") for row in matches],
            }

        if not allow_create:
            self._run(
                """
                MATCH (o:EntityObservation {observation_id:$id})
                OPTIONAL MATCH (o)-[r:RESOLVED_TO]->()
                DELETE r
                SET o.status='unresolved', o.rejection_reason=null,
                    o.candidate_entity_ids=[], o.updated_at=datetime()
                """,
                id=observation_id,
            )
            return {
                "status": "unresolved",
                "reason": str(admission_reason or "insufficient_identity_signal"),
                "observation_id": observation_id,
                "name": clean_name,
                "normalized": normalized,
                "entity_type": entity_type,
            }

        entity_id = str(uuid.uuid4())
        label = entity_type
        self._run(
            f"""
            CREATE (e:Entity:{label} {{
                entity_id:$entity_id,
                display_name:$display_name,
                identity_key:$identity_key,
                origin:'document',
                identity_status:'provisional',
                entity_kind:$entity_kind,
                first_source_document_id:$document_id,
                first_document_seen_at:datetime(),
                last_document_seen_at:datetime(),
                document_evidence_count:1,
                created_at:datetime(),
                updated_at:datetime()
            }})
            MERGE (n:EntityName {{normalized:$normalized}})
            ON CREATE SET n.value=$display_name, n.created_at=datetime()
            SET n.last_seen_value=$display_name, n.updated_at=datetime()
            MERGE (e)-[rn:HAS_NAME {{source_document_id:$document_id, normalized:$normalized}}]->(n)
            SET rn.kind='document_discovered', rn.preferred=true, rn.active=true,
                rn.resolution_policy='exclusive', rn.extractor=$extractor, rn.confidence=$confidence,
                rn.observed_text=$observed_text, rn.updated_at=datetime()
            WITH e
            MATCH (o:EntityObservation {{observation_id:$observation_id}})
            MERGE (o)-[rr:RESOLVED_TO]->(e)
            SET rr.resolved_by='created_provisional', rr.updated_at=datetime(),
                o.status='created_provisional', o.rejection_reason=null,
                o.candidate_entity_ids=[$entity_id], o.updated_at=datetime()
            """,
            entity_id=entity_id,
            display_name=clean_name,
            normalized=normalized,
            document_id=document_id,
            extractor=extractor,
            confidence=float(confidence),
            observed_text=str(observed_text or clean_name),
            observation_id=observation_id,
            identity_key=(organization_identity_key(clean_name) if entity_type == "Organization" else ""),
            entity_kind=str(entity_kind or ("Person" if entity_type == "Person" else "Other")),
        )

        if entity_type == "Organization":
            for alias in generated_organization_aliases(clean_name):
                alias_value = str(alias.get("value") or "").strip()
                alias_norm = str(alias.get("normalized") or normalize_name(alias_value)).strip()
                if not alias_norm or alias_norm == normalized:
                    continue
                self._run(
                    """
                    MATCH (e:Entity:Organization {entity_id:$entity_id})
                    MERGE (a:SearchAlias {normalized:$normalized})
                    ON CREATE SET a.value=$value, a.created_at=datetime()
                    SET a.last_seen_value=$value, a.updated_at=datetime()
                    MERGE (e)-[r:HAS_SEARCH_ALIAS {source_document_id:$document_id, normalized:$normalized}]->(a)
                    SET r.kind=$kind, r.weight=$weight, r.active=true,
                        r.resolution_policy='contextual', r.extractor=$extractor, r.updated_at=datetime()
                    """,
                    entity_id=entity_id,
                    document_id=document_id,
                    normalized=alias_norm,
                    value=alias_value,
                    kind=str(alias.get("kind") or "document_derived_alias"),
                    weight=float(alias.get("weight") or 0.5),
                    extractor=extractor,
                )

        # Earlier weak observations of exactly this same type/name can now be
        # resolved without another LLM call. MENTIONS are rebuilt by the cheap
        # relink phase, so this does not synthesize semantic evidence.
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            MATCH (o:EntityObservation {normalized:$normalized, suggested_type:$entity_type})
            WHERE o.status='unresolved' AND coalesce(properties(o)['eligible'],false)=true
            MERGE (o)-[r:RESOLVED_TO]->(e)
            SET r.resolved_by='later_exact_entity', r.updated_at=datetime(),
                o.status='resolved_existing', o.candidate_entity_ids=[$entity_id],
                o.updated_at=datetime()
            """,
            entity_id=entity_id,
            normalized=normalized,
            entity_type=entity_type,
        )

        merge_candidates = self.refresh_possible_same_as(entity_id)

        return {
            "status": "created_provisional",
            "reason": str(admission_reason or "new_document_entity"),
            "observation_id": observation_id,
            "entity_id": entity_id,
            "display_name": clean_name,
            "name": clean_name,
            "normalized": normalized,
            "entity_type": entity_type,
            "merge_candidates": merge_candidates,
        }

    def resolve_or_create_document_entity(
        self,
        *,
        document_id: str,
        entity_type: str,
        name: str,
        observed_text: str,
        confidence: float,
        extractor: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for v2a callers; v2a.1 callers use observations."""
        return self.record_document_entity_observation(
            document_id=document_id,
            entity_type=entity_type,
            canonical_name=name,
            observed_text=observed_text,
            context_text=observed_text,
            confidence=confidence,
            relevant_actor=True,
            mention_context="actor",
            entity_kind=("Person" if entity_type == "Person" else "Other"),
            extractor=extractor,
            eligible=True,
            allow_create=True,
            admission_reason="legacy_v2a_compatibility",
        )

    # ------------------------------------------------------------------
    # Incremental evidence-document graph
    # ------------------------------------------------------------------

    def touch_evidence_document(
        self,
        *,
        document_id: str,
        title: str,
        path: str,
        source_url: str,
        document_date: str,
        source_date: str = "",
        source_date_precision: str = "",
        source_date_confidence: float = 0.0,
        source_date_basis: str = "",
        source_date_evidence: str = "",
        content_hash: str = "",
        extractor: str,
        query_id: str = "",
        user_query: str = "",
        retrieval_query: str = "",
        evidence_action: str = "answer",
        count_evidence: bool = True,
    ) -> dict[str, Any]:
        """Upsert document metadata and register one evidence use.

        Mention extraction is only required when the document text hash or the
        extractor version changed. Normal queue calls increment evidence_count;
        maintenance/rebuild calls may disable that increment.
        """
        rows = self._run(
            """
            MERGE (d:Document {document_id:$document_id})
            ON CREATE SET
                d.created_at=datetime(),
                d.first_seen_at=datetime(),
                d.first_evidence_at=datetime(),
                d.evidence_count=0
            WITH d,
                 properties(d)['graph_hash'] AS previous_hash,
                 properties(d)['graph_extractor'] AS previous_extractor
            SET d.title=$title,
                d.path=$path,
                d.source_url=$source_url,
                d.document_date=$document_date,
                d.source_date=$source_date,
                d.source_date_precision=$source_date_precision,
                d.source_date_confidence=$source_date_confidence,
                d.source_date_basis=$source_date_basis,
                d.source_date_evidence=$source_date_evidence,
                d.last_seen_at=datetime(),
                d.last_evidence_at=datetime(),
                d.evidence_count=coalesce(properties(d)['evidence_count'],0)+$evidence_increment,
                d.last_query_id=$query_id,
                d.last_user_query=$user_query,
                d.last_retrieval_query=$retrieval_query,
                d.last_evidence_action=$evidence_action,
                d.updated_at=datetime()
            RETURN previous_hash, previous_extractor,
                   properties(d)['evidence_count'] AS evidence_count,
                   properties(d)['analysis_hash'] AS analysis_hash,
                   properties(d)['analysis_extractor'] AS analysis_extractor
            """,
            document_id=document_id,
            title=title,
            path=path,
            source_url=source_url,
            document_date=document_date,
            source_date=source_date,
            source_date_precision=source_date_precision,
            source_date_confidence=float(source_date_confidence or 0.0),
            source_date_basis=source_date_basis,
            source_date_evidence=source_date_evidence[:500],
            query_id=query_id,
            user_query=user_query,
            retrieval_query=retrieval_query,
            evidence_action=evidence_action,
            evidence_increment=1 if count_evidence else 0,
        )
        row = rows[0] if rows else {}
        return {
            "previous_hash": row.get("previous_hash"),
            "previous_extractor": row.get("previous_extractor"),
            "evidence_count": int(row.get("evidence_count") or 0),
            "analysis_hash": str(row.get("analysis_hash") or ""),
            "analysis_extractor": str(row.get("analysis_extractor") or ""),
            "needs_reindex": (
                str(row.get("previous_hash") or "") != str(content_hash or "")
                or str(row.get("previous_extractor") or "") != str(extractor or "")
            ),
        }

    def attach_mail_metadata(
        self,
        *,
        document_id: str,
        mail_key: str,
        sidecar_path: str,
        role: str,
        metadata: dict[str, Any],
        reply_parent_message_id: str = "",
    ) -> None:
        """Attach deterministic RFC mail/thread metadata to an indexed document.

        This is deliberately separate from LLM-extracted relation observations:
        Message-ID / In-Reply-To / References are protocol metadata, not claims.
        Attachments use ATTACHMENT_OF; mail representations use REPRESENTS_MAIL.
        """
        headers = metadata.get("headers") or {}
        message_id = str(headers.get("message_id") or "").strip()
        message_date = str(headers.get("date_iso") or "").strip()
        subject = str(headers.get("subject") or "").strip()
        from_json = json.dumps(headers.get("from") or [], ensure_ascii=False)
        to_json = json.dumps(headers.get("to") or [], ensure_ascii=False)
        cc_json = json.dumps(headers.get("cc") or [], ensure_ascii=False)
        references_json = json.dumps(headers.get("references") or [], ensure_ascii=False)
        in_reply_to_json = json.dumps(headers.get("in_reply_to") or [], ensure_ascii=False)

        rel_type = "ATTACHMENT_OF" if role == "attachment" else "REPRESENTS_MAIL"
        # Relationship type cannot be parameterized in Cypher; the value comes
        # only from the fixed deterministic branch above.
        self._run(
            f"""
            MATCH (d:Document {{document_id:$document_id}})
            MERGE (m:MailMessage {{mail_key:$mail_key}})
            ON CREATE SET m.created_at=datetime()
            SET m.message_id=$message_id,
                m.message_date=$message_date,
                m.subject=$subject,
                m.from_json=$from_json,
                m.to_json=$to_json,
                m.cc_json=$cc_json,
                m.references_json=$references_json,
                m.in_reply_to_json=$in_reply_to_json,
                m.placeholder=false,
                m.updated_at=datetime()
            SET d.mail_sidecar_path=$sidecar_path,
                d.mail_message_key=$mail_key,
                d.mail_message_id=$message_id,
                d.mail_representation_role=$role
            WITH d,m
            OPTIONAL MATCH (d)-[old:REPRESENTS_MAIL|ATTACHMENT_OF]->(other:MailMessage)
            WHERE other.mail_key <> $mail_key
            DELETE old
            MERGE (d)-[r:{rel_type}]->(m)
            SET r.role=$role, r.sidecar_path=$sidecar_path, r.updated_at=datetime()
            """,
            document_id=document_id,
            mail_key=mail_key,
            sidecar_path=str(sidecar_path or "")[:2000],
            role=str(role or "related")[:64],
            message_id=message_id[:1000],
            message_date=message_date[:128],
            subject=subject[:2000],
            from_json=from_json[:12000],
            to_json=to_json[:12000],
            cc_json=cc_json[:12000],
            references_json=references_json[:24000],
            in_reply_to_json=in_reply_to_json[:12000],
        )

        # Replace the deterministic direct-parent edge if the sidecar changes.
        self._run(
            """
            MATCH (m:MailMessage {mail_key:$mail_key})-[r:REPLIES_TO]->()
            DELETE r
            """,
            mail_key=mail_key,
        )
        parent_id = str(reply_parent_message_id or "").strip()
        if parent_id:
            parent_key = "message-id:" + parent_id
            self._run(
                """
                MATCH (m:MailMessage {mail_key:$mail_key})
                MERGE (p:MailMessage {mail_key:$parent_key})
                ON CREATE SET p.created_at=datetime(), p.placeholder=true
                SET p.message_id=CASE WHEN coalesce(properties(p)['message_id'],'')='' THEN $parent_id ELSE properties(p)['message_id'] END,
                    p.updated_at=datetime()
                MERGE (m)-[r:REPLIES_TO]->(p)
                SET r.source='mail_header', r.updated_at=datetime()
                """,
                mail_key=mail_key,
                parent_key=parent_key,
                parent_id=parent_id[:1000],
            )

    def store_document_analysis(
        self,
        *,
        document_id: str,
        content_hash: str,
        extractor: str,
        model: str,
        document_type: str,
        summary: str,
        key_points: list[str],
        relevant_actors: list[dict[str, Any]],
        uncertainties: list[str],
    ) -> None:
        """Persist a derived per-document analysis on the Document node."""
        self._run(
            """
            MATCH (d:Document {document_id:$document_id})
            SET d.analysis_hash=$content_hash,
                d.analysis_extractor=$extractor,
                d.analysis_model=$model,
                d.analysis_document_type=$document_type,
                d.analysis_summary=$summary,
                d.analysis_key_points=$key_points,
                d.analysis_relevant_actors_json=$relevant_actors_json,
                d.analysis_uncertainties=$uncertainties,
                d.analysis_updated_at=datetime()
            """,
            document_id=document_id,
            content_hash=content_hash,
            extractor=extractor,
            model=model,
            document_type=document_type[:160],
            summary=summary[:6000],
            key_points=[str(x)[:1000] for x in key_points[:16]],
            relevant_actors_json=json.dumps(relevant_actors[:32], ensure_ascii=False),
            uncertainties=[str(x)[:1000] for x in uncertainties[:12]],
        )

    def replace_document_mentions(
        self,
        *,
        document_id: str,
        mentions: list[dict[str, Any]],
        content_hash: str,
        extractor: str,
    ) -> None:
        """Replace derived mention edges for one document.

        Exact unique mentions become one MENTIONS edge per Document/Entity pair;
        repeated occurrences are represented by ``mention_count``. Ambiguous or
        fuzzy forms stay explicit as MENTIONS_NAME and POSSIBLE_MATCH candidates.
        Previous extractor versions are intentionally removed on every relink.
        """
        # MENTIONS / MENTIONS_NAME are derived document edges, not durable
        # identity decisions.  Replace *all* previous derived edges for this
        # document, regardless of extractor version.  Filtering by the current
        # extractor caused parallel edges to accumulate on extractor upgrades
        # (for example v2a2 + v2a3 for the same Document -> Entity pair).
        # Manual curation is preserved separately on EntityObservation and is
        # overlaid again below during relink.
        self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[r:MENTIONS]->()
            DELETE r
            """,
            document_id=document_id,
        )
        self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[r:MENTIONS_NAME]->()
            DELETE r
            """,
            document_id=document_id,
        )

        resolved_by_entity: dict[str, dict[str, Any]] = {}
        unresolved_mentions: list[dict[str, Any]] = []

        # Overlay manual, document-scoped curation decisions. This is crucial:
        # an OCR error such as "S G Consulting UG" -> "S K Consulting UG"
        # must not become a global alias, but a later relink of the same document
        # must still honor the human correction.
        manual_rows = self._run(
            """
            MATCH (o:EntityObservation {document_id:$document_id})
            WHERE coalesce(properties(o)['curator_status'],'') IN ['manual_not_entity','corrected_observation','research_finding_entity']
            RETURN o.observation_id AS observation_id,
                   coalesce(properties(o)['curator_status'],'') AS curator_status,
                   coalesce(properties(o)['curator_target_entity_id'],'') AS target_entity_id,
                   coalesce(properties(o)['observed_text'],'') AS observed_text,
                   coalesce(o.normalized,'') AS normalized
            """,
            document_id=document_id,
        )
        manual_by_form: dict[str, dict[str, Any]] = {}
        for row in manual_rows:
            for key in (normalize_name(str(row.get("observed_text") or "")), str(row.get("normalized") or "").strip()):
                if key:
                    manual_by_form[key] = dict(row)

        for mention in mentions:
            observed_value = str(mention.get("observed_value") or "").strip()
            normalized = str(mention.get("normalized") or normalize_name(observed_value)).strip()
            status = str(mention.get("status") or "")
            count = max(1, int(mention.get("count") or 1))
            candidates = list(mention.get("candidates") or [])
            if not normalized:
                continue

            manual = manual_by_form.get(normalize_name(observed_value)) or manual_by_form.get(normalized)
            if manual:
                curator_status = str(manual.get("curator_status") or "")
                if curator_status == "manual_not_entity":
                    # Deliberately no MENTIONS or MENTIONS_NAME edge: this form
                    # was manually classified as not an entity in this document.
                    continue
                if curator_status == "corrected_observation":
                    target_id = str(manual.get("target_entity_id") or "")
                    target_rows = self._run(
                        """
                        MATCH (e:Entity {entity_id:$entity_id})
                        WHERE coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
                        RETURN e.entity_id AS entity_id
                        """,
                        entity_id=target_id,
                    ) if target_id else []
                    if target_rows:
                        status = "resolved_exact"
                        candidates = [{
                            "entity_id": target_id,
                            "score": 1.0,
                            "form_weight": 1.0,
                        }]

            if status == "resolved_exact" and len(candidates) == 1:
                candidate = candidates[0]
                entity_id = str(candidate.get("entity_id") or "")
                if not entity_id:
                    continue
                bucket = resolved_by_entity.setdefault(entity_id, {
                    "count": 0,
                    "observed_values": [],
                    "max_score": 0.0,
                })
                bucket["count"] += count
                if observed_value and observed_value not in bucket["observed_values"]:
                    bucket["observed_values"].append(observed_value)
                bucket["max_score"] = max(
                    float(bucket["max_score"]),
                    float(candidate.get("score") or candidate.get("form_weight") or 1.0),
                )
            else:
                unresolved_mentions.append({
                    "observed_value": observed_value,
                    "normalized": normalized,
                    "status": status,
                    "count": count,
                    "candidates": candidates,
                })

        for entity_id, bucket in resolved_by_entity.items():
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})
                MATCH (e:Entity {entity_id:$entity_id})
                MERGE (d)-[r:MENTIONS {extractor:$extractor}]->(e)
                SET r.mention_count=$mention_count,
                    r.observed_values=$observed_values,
                    r.resolution='exact_unique',
                    r.max_score=$max_score,
                    r.updated_at=datetime()
                """,
                document_id=document_id,
                entity_id=entity_id,
                extractor=extractor,
                mention_count=int(bucket["count"]),
                observed_values=list(bucket["observed_values"]),
                max_score=float(bucket["max_score"]),
            )

        for mention in unresolved_mentions:
            observed_value = str(mention["observed_value"])
            normalized = str(mention["normalized"])
            status = str(mention["status"])
            count = int(mention["count"])
            candidates = list(mention["candidates"])
            candidate_ids = [
                str(c.get("entity_id") or "")
                for c in candidates
                if str(c.get("entity_id") or "")
            ]
            candidate_scores = [
                float(c.get("score") or 0.0)
                for c in candidates
                if str(c.get("entity_id") or "")
            ]
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})
                MERGE (m:MentionName {normalized:$normalized})
                ON CREATE SET m.created_at=datetime(), m.value=$observed_value
                SET m.last_seen_value=$observed_value,
                    m.updated_at=datetime()
                MERGE (d)-[r:MENTIONS_NAME {extractor:$extractor, normalized:$normalized}]->(m)
                SET r.mention_count=$mention_count,
                    r.observed_values=$observed_values,
                    r.status=$status,
                    r.candidate_entity_ids=$candidate_entity_ids,
                    r.candidate_scores=$candidate_scores,
                    r.updated_at=datetime()
                """,
                document_id=document_id,
                normalized=normalized,
                observed_value=observed_value,
                extractor=extractor,
                mention_count=count,
                observed_values=[observed_value] if observed_value else [],
                status=status,
                candidate_entity_ids=candidate_ids,
                candidate_scores=candidate_scores,
            )

            # Candidate edges are global spelling hypotheses, not identity
            # assertions. They are deliberately named POSSIBLE_MATCH.
            for candidate in candidates:
                entity_id = str(candidate.get("entity_id") or "")
                if not entity_id:
                    continue
                self._run(
                    """
                    MATCH (m:MentionName {normalized:$normalized})
                    MATCH (e:Entity {entity_id:$entity_id})
                    MERGE (m)-[r:POSSIBLE_MATCH {extractor:$extractor}]->(e)
                    SET r.max_score = CASE
                        WHEN properties(r)['max_score'] IS NULL OR properties(r)['max_score'] < $score THEN $score
                        ELSE properties(r)['max_score']
                    END,
                        r.last_seen_at=datetime()
                    """,
                    normalized=normalized,
                    entity_id=entity_id,
                    extractor=extractor,
                    score=float(candidate.get("score") or 0.0),
                )

        # Research-finding curation is a durable human decision. Restore the
        # document-level MENTIONS edge even when a later extractor version no
        # longer emits the same surface form.
        for manual in manual_rows:
            if str(manual.get("curator_status") or "") != "research_finding_entity":
                continue
            target_id = str(manual.get("target_entity_id") or "")
            if not target_id:
                continue
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})
                MATCH (target:Entity {entity_id:$entity_id})
                WHERE coalesce(properties(target)['identity_status'],'') NOT IN ['merged','orphaned']
                MERGE (d)-[m:MENTIONS]->(target)
                SET m.research_finding_curated=true,
                    m.last_seen_at=datetime(), m.updated_at=datetime()
                """,
                document_id=document_id,
                entity_id=target_id,
            )

        self._run(
            """
            MATCH (d:Document {document_id:$document_id})
            SET d.graph_hash=$content_hash,
                d.graph_extractor=$extractor,
                d.last_graph_indexed_at=datetime(),
                d.updated_at=datetime()
            """,
            document_id=document_id,
            content_hash=content_hash,
            extractor=extractor,
        )

    def replace_document_relation_observations(
        self,
        *,
        document_id: str,
        observations: list[dict[str, Any]],
        extractor: str,
    ) -> None:
        """Replace document-grounded relation observations.

        RelationObservation/Claim nodes are deliberately *not* durable fact
        edges between entities. Every node belongs to one source document and
        carries an evidence passage. Re-indexing therefore replaces this
        derived layer wholesale, just like MENTIONS edges.
        """
        self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[:HAS_RELATION_OBSERVATION]->(c:RelationObservation)
            WHERE coalesce(properties(c)['curator_status'],'') <> 'manual_claim'
              AND coalesce(properties(c)['extractor'],'') <> 'research_finding_curator'
            DETACH DELETE c
            """,
            document_id=document_id,
        )

        for item in observations:
            relation_id = str(item.get("relation_id") or "").strip()
            subject_id = str(item.get("subject_entity_id") or "").strip()
            object_id = str(item.get("object_entity_id") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            evidence_text = str(item.get("evidence_text") or "").strip()
            if not relation_id or not subject_id or not object_id or not predicate or not evidence_text:
                continue
            if subject_id == object_id:
                continue
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})
                MATCH (s:Entity {entity_id:$subject_id})
                MATCH (o:Entity {entity_id:$object_id})
                CREATE (c:Claim:RelationObservation {relation_id:$relation_id})
                SET c.document_id=$document_id,
                    c.predicate=$predicate,
                    c.predicate_text=$predicate_text,
                    c.relation_text=$relation_text,
                    c.evidence_text=$evidence_text,
                    c.confidence=$confidence,
                    c.stance=$stance,
                    c.chunk_index=$chunk_index,
                    c.extractor=$extractor,
                    c.valid_from=$valid_from,
                    c.valid_to=$valid_to,
                    c.evidence_date=coalesce(properties(d)['source_date'],''),
                    c.evidence_date_precision=coalesce(properties(d)['source_date_precision'],''),
                    c.evidence_date_confidence=coalesce(properties(d)['source_date_confidence'],0.0),
                    c.evidence_date_basis=coalesce(properties(d)['source_date_basis'],''),
                    c.created_at=datetime(),
                    c.updated_at=datetime()
                CREATE (d)-[:HAS_RELATION_OBSERVATION {extractor:$extractor}]->(c)
                CREATE (c)-[:SUBJECT]->(s)
                CREATE (c)-[:OBJECT]->(o)
                """,
                document_id=document_id,
                relation_id=relation_id,
                subject_id=subject_id,
                object_id=object_id,
                predicate=predicate,
                predicate_text=str(item.get("predicate_text") or predicate),
                relation_text=str(item.get("relation_text") or "")[:1000],
                evidence_text=evidence_text[:2000],
                confidence=float(item.get("confidence") or 0.0),
                stance=str(item.get("stance") or "asserted"),
                chunk_index=int(item.get("chunk_index") or 0),
                extractor=extractor,
                valid_from=str(item.get("valid_from") or ""),
                valid_to=str(item.get("valid_to") or ""),
            )

    def document_relation_observations(self, document_id: str) -> list[dict[str, Any]]:
        """Return grounded relation observations for diagnostics."""
        return self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[:HAS_RELATION_OBSERVATION]->(c:RelationObservation)
            MATCH (c)-[:SUBJECT]->(s:Entity)
            MATCH (c)-[:OBJECT]->(o:Entity)
            RETURN c.relation_id AS relation_id,
                   s.entity_id AS subject_entity_id,
                   s.display_name AS subject_display_name,
                   c.predicate AS predicate,
                   properties(c)['predicate_text'] AS predicate_text,
                   properties(c)['relation_text'] AS relation_text,
                   o.entity_id AS object_entity_id,
                   o.display_name AS object_display_name,
                   properties(c)['evidence_text'] AS evidence_text,
                   properties(c)['confidence'] AS confidence,
                   c.stance AS stance,
                   properties(c)['chunk_index'] AS chunk_index,
                   properties(c)['valid_from'] AS valid_from,
                   properties(c)['valid_to'] AS valid_to,
                   properties(c)['evidence_date'] AS evidence_date,
                   properties(c)['evidence_date_precision'] AS evidence_date_precision,
                   properties(c)['evidence_date_confidence'] AS evidence_date_confidence,
                   properties(c)['evidence_date_basis'] AS evidence_date_basis,
                   properties(c)['extractor'] AS extractor
            ORDER BY coalesce(properties(c)['confidence'],0) DESC, properties(c)['chunk_index'], c.predicate
            """,
            document_id=document_id,
        )

    def retrieve_documents_for_entities(
        self,
        entity_ids: list[str],
        *,
        limit: int = 30,
        structural_relations: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return conservative document candidates for resolved query entities.

        v1 deliberately treats the graph as a *retrieval* signal, not as answer
        evidence.  Only Document nodes already created by the evidence indexer
        are returned.  Direct identity/organization edges are reported as
        structure and may broaden the document neighborhood, but are never
        converted into synthetic answer documents.
        """
        ids: list[str] = []
        seen: set[str] = set()
        for value in entity_ids:
            value = str(value or "").strip()
            if value and value not in seen:
                seen.add(value)
                ids.append(value)

        limit = max(1, int(limit or 30))
        relation_types = [
            str(x).strip()
            for x in (structural_relations or RETRIEVAL_BRIDGE_RELATIONS)
            if str(x).strip()
        ]

        if not ids:
            return {
                "mode": "none",
                "entity_ids": [],
                "documents": [],
                "direct_relations": [],
            }

        direct_relations: list[dict[str, Any]] = []
        if len(ids) >= 2 and relation_types:
            direct_relations = self._run(
                """
                MATCH (a:Entity)-[r]->(b:Entity)
                WHERE a.entity_id IN $entity_ids
                  AND b.entity_id IN $entity_ids
                  AND a.entity_id <> b.entity_id
                  AND type(r) IN $relation_types
                  AND coalesce(r.active,true)=true
                RETURN DISTINCT
                    a.entity_id AS from_entity_id,
                    a.display_name AS from_display_name,
                    type(r) AS relation,
                    b.entity_id AS to_entity_id,
                    b.display_name AS to_display_name
                ORDER BY relation, from_display_name, to_display_name
                """,
                entity_ids=ids,
                relation_types=relation_types,
            )

        # Graph v3 strongest signal: a source document contains an explicit,
        # grounded relation observation whose subject and object are both
        # resolved query entities. The relation node is *not* treated as a
        # global fact; it merely promotes the supporting document.
        if len(ids) >= 2:
            relation_rows = self._run(
                """
                MATCH (d:Document)-[:HAS_RELATION_OBSERVATION]->(c:RelationObservation)
                MATCH (c)-[:SUBJECT]->(s:Entity)
                MATCH (c)-[:OBJECT]->(o:Entity)
                WHERE s.entity_id IN $entity_ids
                  AND o.entity_id IN $entity_ids
                  AND s.entity_id <> o.entity_id
                  AND coalesce(properties(c)['extractor'],'') <> 'research_finding_curator'
                WITH d,
                     collect({
                       relation_id:c.relation_id,
                       subject_entity_id:s.entity_id,
                       subject_display_name:s.display_name,
                       predicate:c.predicate,
                       predicate_text:properties(c)['predicate_text'],
                       object_entity_id:o.entity_id,
                       object_display_name:o.display_name,
                       evidence_text:properties(c)['evidence_text'],
                       confidence:properties(c)['confidence'],
                       stance:c.stance,
                       chunk_index:properties(c)['chunk_index'],
                       evidence_date:properties(c)['evidence_date'],
                       evidence_date_precision:properties(c)['evidence_date_precision'],
                       evidence_date_confidence:properties(c)['evidence_date_confidence']
                     }) AS relation_observations,
                     collect(DISTINCT s.entity_id) + collect(DISTINCT o.entity_id) AS raw_entity_ids,
                     max(coalesce(properties(c)['confidence'],0.0)) AS max_confidence
                UNWIND raw_entity_ids AS raw_entity_id
                WITH d, relation_observations, max_confidence, collect(DISTINCT raw_entity_id) AS matched_entity_ids
                RETURN properties(d) AS document,
                       relation_observations,
                       matched_entity_ids,
                       size(matched_entity_ids) AS matched_entity_count,
                       max_confidence
                ORDER BY matched_entity_count DESC,
                         max_confidence DESC,
                         size(relation_observations) DESC,
                         coalesce(properties(d)['last_graph_indexed_at'],properties(d)['last_seen_at'],properties(d)['created_at']) DESC
                LIMIT $limit
                """,
                entity_ids=ids,
                limit=limit,
            )
            if relation_rows:
                documents: list[dict[str, Any]] = []
                for rank, row in enumerate(relation_rows, start=1):
                    doc = dict(row.get("document") or {})
                    observations = list(row.get("relation_observations") or [])
                    evidence = []
                    matched_names: list[str] = []
                    for observation in observations:
                        value = str((observation or {}).get("evidence_text") or "").strip()
                        if value and value not in evidence:
                            evidence.append(value)
                        for key in ("subject_display_name", "object_display_name"):
                            name = str((observation or {}).get(key) or "").strip()
                            if name and name not in matched_names:
                                matched_names.append(name)
                    doc.update({
                        "rank": rank,
                        "graph_score": round(
                            1.25
                            + min(0.20, 0.05 * max(0, int(row.get("matched_entity_count") or 0) - 2))
                            + min(0.15, 0.15 * float(row.get("max_confidence") or 0.0)),
                            4,
                        ),
                        "graph_reason": "explicit_relation_observation",
                        "graph_matched_entity_ids": list(row.get("matched_entity_ids") or []),
                        "graph_matched_entity_names": matched_names,
                        "graph_matched_entity_count": int(row.get("matched_entity_count") or 0),
                        "graph_relation_observations": observations,
                        "graph_relation_evidence": evidence[:4],
                    })
                    documents.append(doc)
                return {
                    "mode": "explicit_relation_observation",
                    "entity_ids": ids,
                    "documents": documents,
                    "direct_relations": direct_relations,
                }

        # Previous strongest signal: one already-indexed evidence document
        # contains all resolved query entities as exact, unique MENTIONS edges.
        if len(ids) >= 2:
            rows = self._run(
                """
                MATCH (d:Document)-[m:MENTIONS]->(e:Entity)
                WHERE e.entity_id IN $entity_ids
                WITH d,
                     collect(DISTINCT e.entity_id) AS matched_entity_ids,
                     collect(DISTINCT e.display_name) AS matched_entity_names,
                     sum(coalesce(properties(m)['mention_count'],1)) AS mention_total
                WHERE size(matched_entity_ids) = size($entity_ids)
                RETURN properties(d) AS document,
                       matched_entity_ids,
                       matched_entity_names,
                       size(matched_entity_ids) AS matched_entity_count,
                       mention_total
                ORDER BY mention_total DESC,
                         coalesce(properties(d)['last_graph_indexed_at'],properties(d)['last_seen_at'],properties(d)['created_at']) DESC
                LIMIT $limit
                """,
                entity_ids=ids,
                limit=limit,
            )
            if rows:
                documents: list[dict[str, Any]] = []
                for rank, row in enumerate(rows, start=1):
                    doc = dict(row.get("document") or {})
                    doc.update({
                        "rank": rank,
                        "graph_score": round(
                            1.0 + min(0.20, 0.02 * float(row.get("mention_total") or 0)),
                            4,
                        ),
                        "graph_reason": "all_query_entities",
                        "graph_matched_entity_ids": list(row.get("matched_entity_ids") or []),
                        "graph_matched_entity_names": list(row.get("matched_entity_names") or []),
                        "graph_matched_entity_count": int(row.get("matched_entity_count") or 0),
                        "graph_mention_total": int(row.get("mention_total") or 0),
                    })
                    documents.append(doc)
                return {
                    "mode": "all_query_entities",
                    "entity_ids": ids,
                    "documents": documents,
                    "direct_relations": direct_relations,
                }

            # If the identity seed itself knows a direct structural relation,
            # expose the already-indexed document neighborhood of both endpoints
            # as a weaker fallback.  The relation is not asserted to the answer
            # model; it merely helps retrieve documents that may support it.
            if direct_relations:
                rows = self._run(
                    """
                    MATCH (d:Document)-[m:MENTIONS]->(e:Entity)
                    WHERE e.entity_id IN $entity_ids
                    WITH d,
                         collect(DISTINCT e.entity_id) AS matched_entity_ids,
                         collect(DISTINCT e.display_name) AS matched_entity_names,
                         sum(coalesce(properties(m)['mention_count'],1)) AS mention_total
                    RETURN properties(d) AS document,
                           matched_entity_ids,
                           matched_entity_names,
                           size(matched_entity_ids) AS matched_entity_count,
                           mention_total
                    ORDER BY matched_entity_count DESC,
                             mention_total DESC,
                             coalesce(properties(d)['last_graph_indexed_at'],properties(d)['last_seen_at'],properties(d)['created_at']) DESC
                    LIMIT $limit
                    """,
                    entity_ids=ids,
                    limit=limit,
                )
                documents = []
                for rank, row in enumerate(rows, start=1):
                    matched_count = int(row.get("matched_entity_count") or 0)
                    doc = dict(row.get("document") or {})
                    doc.update({
                        "rank": rank,
                        "graph_score": round(
                            0.62
                            + 0.16 * max(0, matched_count - 1)
                            + min(0.12, 0.015 * float(row.get("mention_total") or 0)),
                            4,
                        ),
                        "graph_reason": "direct_relation_context",
                        "graph_matched_entity_ids": list(row.get("matched_entity_ids") or []),
                        "graph_matched_entity_names": list(row.get("matched_entity_names") or []),
                        "graph_matched_entity_count": matched_count,
                        "graph_mention_total": int(row.get("mention_total") or 0),
                    })
                    documents.append(doc)
                return {
                    "mode": "direct_relation_context",
                    "entity_ids": ids,
                    "documents": documents,
                    "direct_relations": direct_relations,
                }

            # RC8: allow a two-hop bridge only when *both* hops are grounded in
            # RelationObservation nodes that each have a source Document.  This
            # is retrieval context, never a synthetic direct A<->B relation.
            chain_rows = self._run(
                """
                MATCH (d1:Document)-[:HAS_RELATION_OBSERVATION]->(r1:RelationObservation)
                MATCH (r1)-[:SUBJECT|OBJECT]-(a:Entity)
                MATCH (r1)-[:SUBJECT|OBJECT]-(c:Entity)
                WHERE a.entity_id IN $entity_ids
                  AND c.entity_id <> a.entity_id
                  AND NOT c.entity_id IN $entity_ids
                MATCH (d2:Document)-[:HAS_RELATION_OBSERVATION]->(r2:RelationObservation)
                MATCH (r2)-[:SUBJECT|OBJECT]-(c)
                MATCH (r2)-[:SUBJECT|OBJECT]-(b:Entity)
                WHERE b.entity_id IN $entity_ids
                  AND b.entity_id <> a.entity_id
                  AND b.entity_id <> c.entity_id
                  AND r1.relation_id <> r2.relation_id
                MATCH (r1)-[:SUBJECT]->(s1:Entity)
                MATCH (r1)-[:OBJECT]->(o1:Entity)
                MATCH (r2)-[:SUBJECT]->(s2:Entity)
                MATCH (r2)-[:OBJECT]->(o2:Entity)
                RETURN DISTINCT
                    properties(d1) AS document1,
                    properties(d2) AS document2,
                    a.entity_id AS query_entity_a_id,
                    a.display_name AS query_entity_a_name,
                    b.entity_id AS query_entity_b_id,
                    b.display_name AS query_entity_b_name,
                    c.entity_id AS bridge_entity_id,
                    c.display_name AS bridge_entity_name,
                    {
                      relation_id:r1.relation_id,
                      subject_entity_id:s1.entity_id,
                      subject_display_name:s1.display_name,
                      predicate:r1.predicate,
                      predicate_text:properties(r1)['predicate_text'],
                      object_entity_id:o1.entity_id,
                      object_display_name:o1.display_name,
                      evidence_text:properties(r1)['evidence_text'],
                      confidence:properties(r1)['confidence'],
                      stance:r1.stance
                    } AS hop1,
                    {
                      relation_id:r2.relation_id,
                      subject_entity_id:s2.entity_id,
                      subject_display_name:s2.display_name,
                      predicate:r2.predicate,
                      predicate_text:properties(r2)['predicate_text'],
                      object_entity_id:o2.entity_id,
                      object_display_name:o2.display_name,
                      evidence_text:properties(r2)['evidence_text'],
                      confidence:properties(r2)['confidence'],
                      stance:r2.stance
                    } AS hop2
                ORDER BY coalesce(properties(r1)['confidence'],0.0) + coalesce(properties(r2)['confidence'],0.0) DESC,
                         bridge_entity_name
                LIMIT $chain_limit
                """,
                entity_ids=ids,
                chain_limit=min(8, limit),
            )
            if chain_rows:
                documents_by_id: dict[str, dict[str, Any]] = {}
                chains: list[dict[str, Any]] = []
                for chain_index, row in enumerate(chain_rows, start=1):
                    chain = {
                        "chain_index": chain_index,
                        "kind": "indirect_two_hop",
                        "query_entity_a_id": row.get("query_entity_a_id"),
                        "query_entity_a_name": row.get("query_entity_a_name"),
                        "bridge_entity_id": row.get("bridge_entity_id"),
                        "bridge_entity_name": row.get("bridge_entity_name"),
                        "query_entity_b_id": row.get("query_entity_b_id"),
                        "query_entity_b_name": row.get("query_entity_b_name"),
                        "hop1": dict(row.get("hop1") or {}),
                        "hop2": dict(row.get("hop2") or {}),
                        "direct_relation": False,
                    }
                    chains.append(chain)
                    for doc_key, hop_key in (("document1", "hop1"), ("document2", "hop2")):
                        doc = dict(row.get(doc_key) or {})
                        document_id = str(doc.get("document_id") or "").strip()
                        if not document_id:
                            continue
                        existing = documents_by_id.setdefault(document_id, doc)
                        existing.setdefault("graph_relation_evidence", [])
                        evidence = str((row.get(hop_key) or {}).get("evidence_text") or "").strip()
                        if evidence and evidence not in existing["graph_relation_evidence"]:
                            existing["graph_relation_evidence"].append(evidence)
                        existing.setdefault("graph_indirect_chains", [])
                        if chain not in existing["graph_indirect_chains"]:
                            existing["graph_indirect_chains"].append(chain)

                documents: list[dict[str, Any]] = []
                for rank, doc in enumerate(documents_by_id.values(), start=1):
                    doc.update({
                        "rank": rank,
                        "graph_score": round(max(0.50, 0.66 - 0.01 * (rank - 1)), 4),
                        "graph_reason": "indirect_relation_chain",
                    })
                    documents.append(doc)
                    if len(documents) >= limit:
                        break
                return {
                    "mode": "indirect_relation_chain",
                    "entity_ids": ids,
                    "documents": documents,
                    # Deliberately empty: A-C-B is not A-B.
                    "direct_relations": [],
                    "indirect_relation_chains": chains,
                }

            return {
                "mode": "no_pair_signal",
                "entity_ids": ids,
                "documents": [],
                "direct_relations": [],
                "indirect_relation_chains": [],
            }

        # One resolved entity is intentionally NOT a graph retrieval arm.
        # Mere graph presence is a coverage artifact, not relevance evidence;
        # ordinary name/text lookup belongs to Elasticsearch/Qdrant. The graph
        # becomes a retrieval signal only when query structure (co-mentions or
        # direct entity relations) adds information beyond full text.
        return {
            "mode": "no_single_entity_signal",
            "entity_ids": ids,
            "documents": [],
            "direct_relations": [],
        }

    def _entity_form_decision(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_name(text)
        if not normalized:
            return None
        rows = self._run(
            """
            MATCH (d:EntityFormDecision {normalized:$normalized})
            RETURN d.normalized AS normalized,
                   coalesce(properties(d)['value'],'') AS value,
                   coalesce(properties(d)['status'],'') AS status,
                   coalesce(properties(d)['target_entity_id'],'') AS target_entity_id,
                   coalesce(properties(d)['decision_kind'],'') AS decision_kind,
                   coalesce(properties(d)['reason'],'') AS reason,
                   toString(properties(d)['decided_at']) AS decided_at
            LIMIT 1
            """,
            normalized=normalized,
        )
        if rows:
            return dict(rows[0])

        # Deployments that have not restarted since the global decision model
        # was introduced may not have run ensure_schema's bulk backfill yet.
        # Promote the latest historic manual decision lazily so it applies to
        # every Finding immediately, not only to its originating Finding.
        historic_rows = self._run(
            """
            MATCH (o:EntityObservation {normalized:$normalized})
            WHERE coalesce(properties(o)['curator_status'],'') IN
                  ['manual_not_entity','corrected_observation','research_finding_entity']
            WITH o
            ORDER BY coalesce(
                properties(o)['curator_decided_at'],
                properties(o)['updated_at'],
                properties(o)['created_at']
            ) DESC
            RETURN coalesce(properties(o)['observed_text'],
                            properties(o)['canonical_name'],
                            $value) AS value,
                   coalesce(properties(o)['curator_status'],'') AS curator_status,
                   coalesce(properties(o)['curator_target_entity_id'],'') AS target_entity_id,
                   coalesce(properties(o)['curator_reason'],'') AS reason,
                   toString(coalesce(
                       properties(o)['curator_decided_at'],
                       properties(o)['updated_at'],
                       properties(o)['created_at']
                   )) AS decided_at
            LIMIT 1
            """,
            normalized=normalized,
            value=str(text or "").strip(),
        )
        if not historic_rows:
            return None

        historic = dict(historic_rows[0])
        curator_status = str(historic.get("curator_status") or "")
        status = "not_entity" if curator_status == "manual_not_entity" else "entity"
        target_entity_id = str(historic.get("target_entity_id") or "")
        self._set_entity_form_decision(
            str(historic.get("value") or text),
            status=status,
            target_entity_id=target_entity_id,
            reason=str(historic.get("reason") or "historic_observation_backfill"),
            curator_actor="historic_observation_backfill",
            decision_kind="historic_observation",
        )
        return {
            "normalized": normalized,
            "value": str(historic.get("value") or text),
            "status": status,
            "target_entity_id": target_entity_id,
            "decision_kind": "historic_observation",
            "reason": str(historic.get("reason") or "historic_observation_backfill"),
            "decided_at": str(historic.get("decided_at") or ""),
        }

    def _set_entity_form_decision(
        self,
        text: str,
        *,
        status: str,
        target_entity_id: str = "",
        reason: str = "",
        curator_actor: str = "manual_admin",
        decision_kind: str = "",
    ) -> None:
        normalized = normalize_name(text)
        if not normalized:
            return
        if status not in {"entity", "not_entity"}:
            raise ValueError("Ungültiger globaler Entity-Formstatus")
        self._run(
            """
            MERGE (d:EntityFormDecision {normalized:$normalized})
            ON CREATE SET d.created_at=datetime()
            SET d.value=$value,
                d.status=$status,
                d.target_entity_id=$target_entity_id,
                d.decision_kind=$decision_kind,
                d.reason=$reason,
                d.curator_actor=$curator_actor,
                d.decided_at=datetime(),
                d.updated_at=datetime()
            """,
            normalized=normalized,
            value=str(text or "").strip(),
            status=status,
            target_entity_id=str(target_entity_id or ""),
            reason=str(reason or "")[:1000],
            curator_actor=str(curator_actor or "manual_admin")[:300],
            decision_kind=str(decision_kind or "")[:100],
        )

    def _remove_manual_alias_form(self, entity_id: str, text: str) -> None:
        normalized = normalize_name(text)
        if not entity_id or not normalized:
            return
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_SEARCH_ALIAS]->(a:SearchAlias {normalized:$normalized})
            WHERE coalesce(properties(r)['source_curator'],'')='manual'
            DELETE r
            WITH a
            OPTIONAL MATCH (:Entity)-[remaining:HAS_SEARCH_ALIAS]->(a)
            WITH a, count(remaining) AS refs
            WHERE refs=0
            DELETE a
            """,
            entity_id=entity_id,
            normalized=normalized,
        )
        self.refresh_possible_same_as(entity_id)

    def _resolve_existing_query_entity(self, text: str) -> dict[str, Any] | None:
        """Resolve one query-frame entity only when an exact global form is unique.

        Exclusive names and unique contextual/search aliases are valid defaults
        for Finding curation. A global not-entity decision always wins until a
        curator explicitly assigns or creates an Entity for that form.
        """
        normalized = normalize_name(text)
        if not normalized:
            return None
        decision = self._entity_form_decision(text)
        if decision and str(decision.get("status") or "") == "not_entity":
            return None
        if decision and str(decision.get("status") or "") == "entity":
            target_id = str(decision.get("target_entity_id") or "")
            target_rows = self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                WHERE coalesce(properties(e)['identity_status'],'') <> 'merged'
                  AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
                RETURN e.entity_id AS entity_id,
                       e.display_name AS display_name,
                       labels(e) AS labels,
                       'curated' AS match_kind
                LIMIT 1
                """,
                entity_id=target_id,
            ) if target_id else []
            if target_rows:
                return target_rows[0]
        rows = self._run(
            """
            MATCH (e:Entity)
            WHERE coalesce(properties(e)['identity_status'],'') <> 'merged'
              AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
            OPTIONAL MATCH (e)-[rn:HAS_NAME]->(n:EntityName {normalized:$normalized})
            OPTIONAL MATCH (e)-[ra:HAS_SEARCH_ALIAS]->(a:SearchAlias {normalized:$normalized})
            WITH e, n, rn, a, ra
            WHERE (n IS NOT NULL
                   AND coalesce(properties(rn)['active'],true)=true
                   AND coalesce(properties(rn)['resolution_policy'],'exclusive') <> 'document_only')
               OR (a IS NOT NULL
                   AND coalesce(properties(ra)['active'],true)=true
                   AND coalesce(properties(ra)['resolution_policy'],'contextual') <> 'document_only')
            WITH e, max(CASE WHEN n IS NOT NULL THEN 2 ELSE 1 END) AS match_rank
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   CASE WHEN match_rank=2 THEN 'name' ELSE 'alias' END AS match_kind
            ORDER BY e.display_name, e.entity_id
            """,
            normalized=normalized,
        )
        return rows[0] if len(rows) == 1 else None
    def store_research_findings(
        self,
        *,
        query_id: str,
        query_frame: dict[str, Any],
        documents: list[dict[str, Any]],
        canonical_user_id: str = "",
        nextcloud_login: str = "",
        nextcloud_server: str = "",
        user_query: str = "",
        retrieval_query: str = "",
        source_scopes: list[str] | None = None,
        provenance_code: str = PROVENANCE_CODE,
        provenance_label: str = PROVENANCE_LABEL,
        software_version: str = "",
        planner_model: str = "",
        verifier_model: str = "",
    ) -> dict[str, Any]:
        """Persist positive planner/verifier work without another extraction pass.

        Only verifier matches with direct document binding are accepted. The
        query frame remains a provenance-bearing observation; its relations are
        deliberately *not* promoted to global Entity->Entity fact edges. Existing
        graph entities are linked only on unique exact query-form resolution.
        Document/finding upserts and query-entity links are batched so the feature
        remains cheap enough for the super-light deployment profile.
        """
        frame = canonical_query_frame(query_frame)
        if not query_frame_has_structure(frame):
            return {"stored": 0, "skipped": len(documents), "reason": "empty_query_frame"}

        frame_hash = query_frame_hash(frame)
        shared_curation_hash = curation_frame_hash(frame)
        frame_json = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        query_entity_candidates = [
            {"text": str(item.get("text") or ""), "role": str(item.get("role") or "")}
            for item in frame.get("entities") or []
        ]
        relation_texts = frame_relation_texts(frame)
        constraints_json = json.dumps(frame.get("constraints") or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        concepts = [str(item) for item in frame.get("concepts") or []]

        resolved_entities: dict[str, dict[str, Any]] = {}
        for item in frame.get("entities") or []:
            resolved = self._resolve_existing_query_entity(str(item.get("text") or ""))
            if resolved:
                resolved_entities[str(item.get("id") or "")] = resolved

        rows: list[dict[str, Any]] = []
        skipped = 0
        for item in documents:
            if not isinstance(item, dict):
                skipped += 1
                continue
            document_id = str(item.get("document_id") or "").strip()
            verification_status = str(item.get("verification_status") or "").strip().lower()
            relation_binding = str(item.get("relation_binding") or "").strip().lower()
            if not document_id or verification_status != "match" or relation_binding != "direct":
                skipped += 1
                continue

            evidence_frame = item.get("evidence_frame") if isinstance(item.get("evidence_frame"), dict) else {}
            evidence_json = json.dumps(evidence_frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            finding_candidates = merge_entity_candidates(
                query_entity_candidates,
                evidence_entity_candidates(evidence_frame),
            )
            candidate_finding_id = research_finding_id(
                document_id, frame, provenance_code=provenance_code
            )
            existing = self._run(
                """
                MATCH (f:ResearchFinding)-[:SUPPORTED_BY]->(d:Document {document_id:$document_id})
                WHERE coalesce(properties(f)['curation_hash'],'')=$curation_hash
                   OR (coalesce(properties(f)['curation_hash'],'')='' AND f.frame_hash=$frame_hash)
                RETURN f.finding_id AS finding_id
                ORDER BY properties(f)['created_at'] ASC
                LIMIT 1
                """,
                document_id=document_id,
                curation_hash=shared_curation_hash,
                frame_hash=frame_hash,
            )
            if existing and str(existing[0].get("finding_id") or ""):
                candidate_finding_id = str(existing[0]["finding_id"])
            rows.append({
                "document_id": document_id,
                "title": str(item.get("title") or "")[:1000],
                "path": str(item.get("path") or "")[:4000],
                "source_url": str(item.get("source_url") or "")[:4000],
                "document_date": str(item.get("document_date") or "")[:100],
                "source_origin": str(item.get("source_origin") or "")[:120],
                "finding_id": candidate_finding_id,
                "evidence_frame_json": evidence_json,
                "evidence_frame_hash": hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
                "entity_texts": [candidate["text"] for candidate in finding_candidates],
                "entity_roles": [candidate["role"] for candidate in finding_candidates],
            })

        if not rows:
            return {
                "stored": 0,
                "skipped": skipped,
                "frame_hash": frame_hash,
                "finding_ids": [],
                "resolved_query_entities": len(resolved_entities),
                "provenance": str(provenance_label or PROVENANCE_LABEL),
            }

        run_id = str(query_id or "").strip()
        if run_id:
            self._run(
                """
                MERGE (run:ResearchRun {run_id:$run_id})
                ON CREATE SET run.created_at=datetime(), run.curation_status='open'
                SET run.canonical_user_id=$canonical_user_id,
                    run.nextcloud_login=$nextcloud_login,
                    run.nextcloud_server=$nextcloud_server,
                    run.user_query=$user_query,
                    run.retrieval_query=$retrieval_query,
                    run.source_scopes=$source_scopes,
                    run.frame_hash=$frame_hash,
                    run.software_version=$software_version,
                    run.planner_model=$planner_model,
                    run.verifier_model=$verifier_model,
                    run.last_seen_at=datetime(),
                    run.updated_at=datetime()
                FOREACH (_ IN CASE WHEN $canonical_user_id <> '' THEN [1] ELSE [] END |
                    MERGE (u:CanonicalUser {canonical_user_id:$canonical_user_id})
                    ON CREATE SET u.created_at=datetime()
                    SET u.nextcloud_login=$nextcloud_login,
                        u.nextcloud_server=$nextcloud_server,
                        u.updated_at=datetime()
                    MERGE (u)-[p:PERFORMED]->(run)
                    ON CREATE SET p.created_at=datetime()
                    SET p.last_seen_at=datetime()
                )
                """,
                run_id=run_id,
                canonical_user_id=str(canonical_user_id or "")[:200],
                nextcloud_login=str(nextcloud_login or "")[:300],
                nextcloud_server=str(nextcloud_server or "")[:2000],
                user_query=str(user_query or "")[:4000],
                retrieval_query=str(retrieval_query or "")[:4000],
                source_scopes=sorted({str(x).strip() for x in (source_scopes or []) if str(x).strip()}),
                frame_hash=frame_hash,
                software_version=str(software_version or "")[:120],
                planner_model=str(planner_model or "")[:300],
                verifier_model=str(verifier_model or "")[:300],
            )

        self._run(
            """
            UNWIND $rows AS row
            MERGE (d:Document {document_id:row.document_id})
            ON CREATE SET d.created_at=datetime(), d.first_seen_at=datetime()
            SET d.title=row.title,
                d.path=row.path,
                d.source_url=row.source_url,
                d.document_date=row.document_date,
                d.source_origin=row.source_origin,
                d.last_seen_at=datetime(),
                d.updated_at=datetime()
            MERGE (f:ResearchFinding:AKIResearchFinding {finding_id:row.finding_id})
            ON CREATE SET f.created_at=datetime(),
                          f.first_seen_at=datetime(),
                          f.observation_count=0
            SET f.provenance_code=$provenance_code,
                f.provenance_label=$provenance_label,
                f.frame_hash=$frame_hash,
                f.curation_hash=$curation_hash,
                f.query_frame_json=$query_frame_json,
                f.intent=$intent,
                f.entity_texts=row.entity_texts,
                f.entity_roles=row.entity_roles,
                f.relation_texts=$relation_texts,
                f.constraints_json=$constraints_json,
                f.concepts=$concepts,
                f.evidence_frame_json=row.evidence_frame_json,
                f.evidence_frame_hash=row.evidence_frame_hash,
                f.verification_status='match',
                f.relation_binding='direct',
                f.last_query_id=$query_id,
                f.software_version=$software_version,
                f.planner_model=$planner_model,
                f.verifier_model=$verifier_model,
                f.last_seen_at=datetime(),
                f.observation_count=coalesce(properties(f)['observation_count'],0)+1,
                f.updated_at=datetime()
            MERGE (f)-[r:SUPPORTED_BY]->(d)
            ON CREATE SET r.created_at=datetime()
            SET r.verification_status='match',
                r.relation_binding='direct',
                r.last_query_id=$query_id,
                r.last_seen_at=datetime()
            WITH f
            OPTIONAL MATCH (run:ResearchRun {run_id:$query_id})
            FOREACH (_ IN CASE WHEN run IS NULL THEN [] ELSE [1] END |
                MERGE (run)-[p:PRODUCED]->(f)
                ON CREATE SET p.created_at=datetime(), p.disposition='pending'
                SET p.last_seen_at=datetime()
            )
            """,
            rows=rows,
            provenance_code=str(provenance_code or PROVENANCE_CODE)[:120],
            provenance_label=str(provenance_label or PROVENANCE_LABEL)[:240],
            frame_hash=frame_hash,
            curation_hash=shared_curation_hash,
            query_frame_json=frame_json,
            intent=str(frame.get("intent") or "")[:240],
            relation_texts=relation_texts,
            constraints_json=constraints_json,
            concepts=concepts,
            query_id=str(query_id or "")[:200],
            software_version=str(software_version or "")[:120],
            planner_model=str(planner_model or "")[:300],
            verifier_model=str(verifier_model or "")[:300],
        )

        links: list[dict[str, str]] = []
        for row in rows:
            for entity in frame.get("entities") or []:
                frame_entity_id = str(entity.get("id") or "")
                resolved = resolved_entities.get(frame_entity_id)
                entity_id = str((resolved or {}).get("entity_id") or "")
                if not entity_id:
                    continue
                links.append({
                    "finding_id": str(row["finding_id"]),
                    "entity_id": entity_id,
                    "frame_entity_id": frame_entity_id,
                    "text": str(entity.get("text") or "")[:300],
                    "role": str(entity.get("role") or "")[:160],
                })

        if links:
            self._run(
                """
                UNWIND $links AS link
                MATCH (f:ResearchFinding {finding_id:link.finding_id})
                MATCH (e:Entity {entity_id:link.entity_id})
                MERGE (f)-[r:QUERY_ENTITY {frame_entity_id:link.frame_entity_id}]->(e)
                SET r.text=link.text,
                    r.role=link.role,
                    r.resolution='unique_exact_query_form',
                    r.updated_at=datetime()
                """,
                links=links,
            )

        return {
            "stored": len(rows),
            "skipped": skipped,
            "frame_hash": frame_hash,
            "finding_ids": [str(row["finding_id"]) for row in rows],
            "resolved_query_entities": len(resolved_entities),
            "provenance": str(provenance_label or PROVENANCE_LABEL),
        }

    def list_research_runs(
        self,
        *,
        canonical_user_id: str = "",
        query: str = "",
        state: str = "open",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List research runs as the primary curation work queue.

        Finding curation remains global. Run/edge disposition only controls
        whether one concrete research run remains in a user's working queue.
        """
        rows = self._run(
            """
            MATCH (run:ResearchRun)
            WHERE ($canonical_user_id='' OR run.canonical_user_id=$canonical_user_id)
              AND ($needle='' OR toLower(coalesce(properties(run)['user_query'],'')) CONTAINS $needle
                               OR toLower(coalesce(properties(run)['retrieval_query'],'')) CONTAINS $needle)
            OPTIONAL MATCH (run)-[p:PRODUCED]->(f:ResearchFinding)
            RETURN properties(run) AS run,
                   collect({finding_id:f.finding_id, disposition:coalesce(properties(p)['disposition'],'pending')}) AS produced
            ORDER BY properties(run)['created_at'] DESC
            LIMIT $limit
            """,
            canonical_user_id=str(canonical_user_id or ""),
            needle=str(query or "").strip().casefold(),
            limit=max(1, min(int(limit), 2000)),
        )
        produced_ids = list(dict.fromkeys(
            str(edge.get("finding_id") or "")
            for row in rows
            for edge in (row.get("produced") or [])
            if str((edge or {}).get("finding_id") or "")
        ))
        findings = {
            str(item.get("finding_id") or ""): item
            for item in self.list_research_findings(finding_ids=produced_ids)
        }
        out: list[dict[str, Any]] = []
        for row in rows:
            run = dict(row.get("run") or {})
            produced = [dict(x or {}) for x in (row.get("produced") or []) if (x or {}).get("finding_id")]
            visible = []
            open_count = 0
            resolved_count = 0
            dismissed_count = 0
            review_count = 0
            for edge in produced:
                finding = findings.get(str(edge.get("finding_id") or ""))
                if finding is None:
                    continue
                item = {**finding, "run_disposition": str(edge.get("disposition") or "pending")}
                visible.append(item)
                if item["run_disposition"] == "dismissed":
                    dismissed_count += 1
                    continue
                graph_state = str(item.get("graph_state") or "open")
                if graph_state in {"open", "no_entity"}:
                    open_count += 1
                elif graph_state == "review_required":
                    review_count += 1
                else:
                    resolved_count += 1
            effective = "dismissed" if str(run.get("curation_status") or "") == "dismissed" else (
                "open" if (open_count or review_count) else "completed"
            )
            run.update({
                "effective_status": effective,
                "finding_count": len(visible),
                "open_count": open_count,
                "resolved_count": resolved_count,
                "review_count": review_count,
                "dismissed_count": dismissed_count,
                "findings": visible,
            })
            if state in {"", "all"} or effective == state:
                out.append(run)
        return out

    def research_finding_observed_by_user(self, canonical_user_id: str, finding_id: str) -> bool:
        rows = self._run(
            """
            MATCH (run:ResearchRun {canonical_user_id:$canonical_user_id})-[:PRODUCED]->(f:ResearchFinding {finding_id:$finding_id})
            RETURN count(f) AS count
            """,
            canonical_user_id=str(canonical_user_id or ""),
            finding_id=str(finding_id or ""),
        )
        return bool(int(rows[0].get("count") or 0)) if rows else False

    def purge_denied_uncurated_research_findings_for_user(
        self,
        canonical_user_id: str,
        finding_ids: list[str],
    ) -> dict[str, int]:
        """Remove only uncurated user provenance after a definitive live-ACL denial.

        Curated Findings are preserved. A Finding is eligible only when it has
        no per-Finding curator status/suppression, no CURATED_ENTITY edge and no
        RelationObservation derived from it. User-specific PRODUCED edges are
        removed first; the shared Finding node is garbage-collected only when no
        ResearchRun for any user still references it.
        """
        user_id = str(canonical_user_id or "").strip()
        ids = list(dict.fromkeys(
            str(value or "").strip()
            for value in (finding_ids or [])
            if str(value or "").strip()
        ))
        if not user_id or not ids:
            return {
                "requested": len(ids),
                "detached_edges": 0,
                "detached_findings": 0,
                "deleted_findings": 0,
            }

        eligible_rows = self._run(
            """
            MATCH (run:ResearchRun {canonical_user_id:$canonical_user_id})-[p:PRODUCED]->(f:ResearchFinding)
            WHERE f.finding_id IN $finding_ids
              AND coalesce(properties(f)['curator_status'],'')=''
              AND size(coalesce(properties(f)['suppressed_entity_texts'],[]))=0
              AND NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }
              AND NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }
            RETURN count(p) AS edge_count, collect(DISTINCT f.finding_id) AS finding_ids
            """,
            canonical_user_id=user_id,
            finding_ids=ids,
        )
        eligible = dict(eligible_rows[0]) if eligible_rows else {}
        eligible_ids = [
            str(value or "").strip()
            for value in (eligible.get("finding_ids") or [])
            if str(value or "").strip()
        ]
        detached_edges = int(eligible.get("edge_count") or 0)
        if eligible_ids:
            self._run(
                """
                MATCH (run:ResearchRun {canonical_user_id:$canonical_user_id})-[p:PRODUCED]->(f:ResearchFinding)
                WHERE f.finding_id IN $finding_ids
                  AND coalesce(properties(f)['curator_status'],'')=''
                  AND size(coalesce(properties(f)['suppressed_entity_texts'],[]))=0
                  AND NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }
                  AND NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }
                DELETE p
                """,
                canonical_user_id=user_id,
                finding_ids=eligible_ids,
            )

        orphan_rows = self._run(
            """
            MATCH (f:ResearchFinding)
            WHERE f.finding_id IN $finding_ids
              AND coalesce(properties(f)['curator_status'],'')=''
              AND size(coalesce(properties(f)['suppressed_entity_texts'],[]))=0
              AND NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }
              AND NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }
              AND NOT EXISTS { MATCH (:ResearchRun)-[:PRODUCED]->(f) }
            RETURN f.finding_id AS finding_id
            """,
            finding_ids=eligible_ids,
        ) if eligible_ids else []
        orphan_ids = [
            str(row.get("finding_id") or "").strip()
            for row in orphan_rows
            if str(row.get("finding_id") or "").strip()
        ]
        if orphan_ids:
            self._run(
                """
                MATCH (f:ResearchFinding)
                WHERE f.finding_id IN $finding_ids
                  AND coalesce(properties(f)['curator_status'],'')=''
                  AND size(coalesce(properties(f)['suppressed_entity_texts'],[]))=0
                  AND NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }
                  AND NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }
                  AND NOT EXISTS { MATCH (:ResearchRun)-[:PRODUCED]->(f) }
                DETACH DELETE f
                """,
                finding_ids=orphan_ids,
            )
        return {
            "requested": len(ids),
            "detached_edges": detached_edges,
            "detached_findings": len(eligible_ids),
            "deleted_findings": len(orphan_ids),
        }

    def research_run_detail(self, run_id: str) -> dict[str, Any] | None:
        runs = self._run(
            """
            MATCH (run:ResearchRun {run_id:$run_id})
            OPTIONAL MATCH (run)-[p:PRODUCED]->(f:ResearchFinding)
            RETURN properties(run) AS run,
                   collect({finding_id:f.finding_id, disposition:coalesce(properties(p)['disposition'],'pending')}) AS produced
            """,
            run_id=str(run_id or ""),
        )
        if not runs:
            return None
        run = dict(runs[0].get("run") or {})
        produced_ids = [
            str((edge or {}).get("finding_id") or "")
            for edge in (runs[0].get("produced") or [])
            if str((edge or {}).get("finding_id") or "")
        ]
        finding_map = {
            str(item.get("finding_id") or ""): item
            for item in self.list_research_findings(finding_ids=produced_ids)
        }
        findings = []
        for edge in runs[0].get("produced") or []:
            fid = str((edge or {}).get("finding_id") or "")
            item = finding_map.get(fid)
            if item:
                findings.append({**item, "run_disposition": str((edge or {}).get("disposition") or "pending")})
        run["findings"] = findings
        return run

    def dismiss_research_run(self, run_id: str, *, actor: str = "admin", reason: str = "") -> dict[str, Any]:
        rows = self._run(
            """
            MATCH (run:ResearchRun {run_id:$run_id})
            SET run.curation_status='dismissed',
                run.dismissed_at=datetime(),
                run.dismissed_by=$actor,
                run.dismiss_reason=$reason,
                run.updated_at=datetime()
            WITH run
            OPTIONAL MATCH (run)-[p:PRODUCED]->(:ResearchFinding)
            WHERE coalesce(properties(p)['disposition'],'pending')='pending'
            SET p.disposition='dismissed', p.dismissed_at=datetime(), p.dismissed_by=$actor
            RETURN run.run_id AS run_id, count(p) AS findings_dismissed
            """,
            run_id=str(run_id or ""),
            actor=str(actor or "admin")[:300],
            reason=str(reason or "")[:1000],
        )
        if not rows:
            raise ValueError("Research run not found")
        return dict(rows[0])

    def set_research_run_finding_disposition(
        self,
        run_id: str,
        finding_ids: list[str],
        *,
        disposition: str,
        actor: str = "admin",
    ) -> dict[str, Any]:
        clean = str(disposition or "").strip().casefold()
        if clean not in {"pending", "dismissed"}:
            raise ValueError("invalid research-run finding disposition")
        ids = list(dict.fromkeys(str(x or "").strip() for x in finding_ids if str(x or "").strip()))
        rows = self._run(
            """
            MATCH (run:ResearchRun {run_id:$run_id})-[p:PRODUCED]->(f:ResearchFinding)
            WHERE f.finding_id IN $finding_ids
            SET p.disposition=$disposition,
                p.updated_at=datetime(),
                p.dismissed_at=CASE WHEN $disposition='dismissed' THEN datetime() ELSE null END,
                p.dismissed_by=CASE WHEN $disposition='dismissed' THEN $actor ELSE '' END
            RETURN count(p) AS updated
            """,
            run_id=str(run_id or ""),
            finding_ids=ids,
            disposition=clean,
            actor=str(actor or "admin")[:300],
        )
        return {"updated": int(rows[0].get("updated") or 0) if rows else 0, "requested": len(ids)}

    def list_research_findings(
        self,
        *,
        query: str = "",
        limit: int = 200,
        finding_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List persisted positive verifier findings for the admin UI.

        When finding_ids is supplied, fetch exactly that bounded caller-owned
        set rather than applying the global newest-findings window.
        """
        needle = str(query or "").strip().casefold()
        targeted = finding_ids is not None
        ids = list(dict.fromkeys(
            str(value or "").strip()
            for value in (finding_ids or [])
            if str(value or "").strip()
        ))
        if targeted and not ids:
            return []
        row_limit = len(ids) if targeted else max(1, min(int(limit), 2000))
        rows = self._run(
            """
            MATCH (f:ResearchFinding)-[s:SUPPORTED_BY]->(d:Document)
            OPTIONAL MATCH (f)-[qe:QUERY_ENTITY]->(e:Entity)
            WITH f, s, d, collect({
                text: properties(qe)['text'], role: properties(qe)['role'], entity_id: e.entity_id,
                display_name: e.display_name
            }) AS resolved_entities
            OPTIONAL MATCH (f)-[ce:CURATED_ENTITY]->(curated:Entity)
            WITH f, s, d, resolved_entities, collect({
                text: properties(ce)['frame_text'], role: properties(ce)['role'], entity_id: curated.entity_id,
                display_name: curated.display_name, labels: labels(curated)
            }) AS curated_entities
            OPTIONAL MATCH (claim:RelationObservation)-[:DERIVED_FROM_FINDING]->(f)
            WITH f, s, d, resolved_entities, curated_entities,
                 count(CASE WHEN coalesce(properties(claim)['curator_status'],'') = 'manual_claim' THEN claim END) AS claim_count,
                 count(CASE WHEN coalesce(properties(claim)['curator_status'],'') = 'review_required' THEN claim END) AS review_count
            WHERE (size($finding_ids)=0 OR f.finding_id IN $finding_ids)
              AND ($needle=''
               OR toLower(coalesce(properties(d)['title'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(d)['path'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(f)['intent'],'')) CONTAINS $needle
               OR toLower(coalesce(properties(f)['constraints_json'],'')) CONTAINS $needle
               OR any(x IN coalesce(properties(f)['entity_texts'],[]) WHERE toLower(x) CONTAINS $needle)
               OR any(x IN coalesce(properties(f)['concepts'],[]) WHERE toLower(x) CONTAINS $needle)
               OR any(x IN resolved_entities WHERE toLower(coalesce(x.display_name,'')) CONTAINS $needle)
               OR any(x IN curated_entities WHERE toLower(coalesce(x.display_name,'')) CONTAINS $needle))
            RETURN f.finding_id AS finding_id,
                   d.document_id AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(d)['source_url'] AS source_url,
                   properties(d)['document_date'] AS document_date,
                   properties(f)['intent'] AS intent,
                   properties(f)['entity_texts'] AS entity_texts,
                   properties(f)['entity_roles'] AS entity_roles,
                   properties(f)['concepts'] AS concepts,
                   properties(f)['constraints_json'] AS constraints_json,
                   properties(f)['verification_status'] AS verification_status,
                   properties(f)['relation_binding'] AS relation_binding,
                   properties(f)['planner_model'] AS planner_model,
                   properties(f)['verifier_model'] AS verifier_model,
                   properties(f)['software_version'] AS software_version,
                   properties(f)['curator_status'] AS curator_status,
                   properties(f)['curator_reason'] AS curator_reason,
                   coalesce(properties(f)['suppressed_entity_texts'],[]) AS suppressed_entity_texts,
                   properties(f)['observation_count'] AS observation_count,
                   properties(f)['last_seen_at'] AS last_seen_at,
                   resolved_entities, curated_entities, claim_count, review_count
            ORDER BY properties(f)['last_seen_at'] DESC
            LIMIT $limit
            """,
            needle=needle,
            finding_ids=ids,
            limit=row_limit,
        )
        global_rejections = {
            str(row.get("normalized") or "")
            for row in self._run(
                """
                MATCH (d:EntityFormDecision)
                WHERE coalesce(properties(d)['status'],'')='not_entity'
                RETURN d.normalized AS normalized
                """
            )
            if str(row.get("normalized") or "")
        }
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            visible_candidates = merge_entity_candidates([
                {"text": str(text or "").strip(), "role": str(role or "")}
                for text, role in zip(
                    item.get("entity_texts") or [],
                    list(item.get("entity_roles") or [])
                    + [""] * len(item.get("entity_texts") or []),
                )
                if str(text or "").strip()
            ])
            entity_texts = [candidate["text"] for candidate in visible_candidates]
            item["entity_texts"] = entity_texts
            item["entity_roles"] = [candidate["role"] for candidate in visible_candidates]
            curated = [x for x in (item.get("curated_entities") or []) if x and x.get("entity_id")]
            suppressed = {
                normalize_name(str(x))
                for x in (item.get("suppressed_entity_texts") or [])
                if normalize_name(str(x))
            }
            suppressed.update(
                normalize_name(text) for text in entity_texts
                if normalize_name(text) in global_rejections
            )
            decided = {
                normalize_name(str(x.get("text") or "")) for x in curated
                if normalize_name(str(x.get("text") or ""))
            } | suppressed
            claim_count = int(item.get("claim_count") or 0)
            review_count = int(item.get("review_count") or 0)
            curator_status = str(item.get("curator_status") or "")
            if curator_status == "suppressed":
                graph_state = "suppressed"
            elif review_count:
                graph_state = "review_required"
            elif not entity_texts:
                graph_state = "no_entity"
            elif claim_count:
                graph_state = "claimed"
            elif curator_status == "mentions_only":
                graph_state = "mentions_only"
            elif all(normalize_name(text) in decided for text in entity_texts):
                graph_state = "entities_resolved"
            else:
                graph_state = "open"
            item["graph_state"] = graph_state
            item["entity_decisions"] = len(decided)
            item["entity_total"] = len(entity_texts)
            out.append(item)
        return out

    def research_finding_detail(self, finding_id: str) -> dict[str, Any] | None:
        rows = self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})-[s:SUPPORTED_BY]->(d:Document)
            OPTIONAL MATCH (f)-[qe:QUERY_ENTITY]->(e:Entity)
            WITH f, s, d, collect({
                frame_entity_id: properties(qe)['frame_entity_id'], role: properties(qe)['role'], text: properties(qe)['text'],
                entity_id: e.entity_id, display_name: e.display_name, labels: labels(e)
            }) AS resolved_entities
            OPTIONAL MATCH (f)-[ce:CURATED_ENTITY]->(curated:Entity)
            WITH f, s, d, resolved_entities, collect({
                text: properties(ce)['frame_text'], role: properties(ce)['role'], entity_id: curated.entity_id,
                display_name: curated.display_name, labels: labels(curated),
                entity_kind: coalesce(properties(curated)['entity_kind'],''),
                curated_at: properties(ce)['curated_at']
            }) AS curated_entities
            OPTIONAL MATCH (claim:RelationObservation)-[:DERIVED_FROM_FINDING]->(f)
            OPTIONAL MATCH (claim)-[:SUBJECT]->(subject:Entity)
            OPTIONAL MATCH (claim)-[:OBJECT]->(object:Entity)
            WITH f, s, d, resolved_entities, curated_entities, collect(CASE WHEN claim IS NULL THEN null ELSE {
                relation_id: properties(claim)['relation_id'],
                predicate: properties(claim)['predicate'],
                predicate_text: properties(claim)['predicate_text'],
                relation_text: properties(claim)['relation_text'],
                evidence_text: properties(claim)['evidence_text'],
                curator_note: properties(claim)['curator_note'],
                curator_status: properties(claim)['curator_status'],
                review_reason: properties(claim)['review_reason'],
                subject_entity_id: subject.entity_id,
                subject_name: subject.display_name,
                object_entity_id: object.entity_id,
                object_name: object.display_name
            } END) AS claims
            RETURN properties(f) AS finding, properties(s) AS support,
                   properties(d) AS document, resolved_entities, curated_entities,
                   [x IN claims WHERE x IS NOT NULL] AS claims
            LIMIT 1
            """,
            finding_id=str(finding_id or "").strip(),
        )
        if not rows:
            return None
        detail = dict(rows[0])
        finding = dict(detail.get("finding") or {})
        visible_candidates = merge_entity_candidates([
            {"text": str(text or "").strip(), "role": str(role or "")}
            for text, role in zip(
                finding.get("entity_texts") or [],
                list(finding.get("entity_roles") or [])
                + [""] * len(finding.get("entity_texts") or []),
            )
            if str(text or "").strip()
        ])
        finding["entity_texts"] = [candidate["text"] for candidate in visible_candidates]
        finding["entity_roles"] = [candidate["role"] for candidate in visible_candidates]
        detail["finding"] = finding
        return detail

    def curate_research_finding(
        self, finding_id: str, *, status: str, reason: str = "", curator_actor: str = "manual_admin"
    ) -> dict[str, Any]:
        """Set curator workflow state without deleting finding provenance."""
        clean_status = str(status or "").strip().casefold()
        if clean_status not in {"", "suppressed", "mentions_only"}:
            raise ValueError(f"Ungültiger Finding-Status: {status}")

        if clean_status == "mentions_only":
            detail = self.research_finding_detail(finding_id)
            if detail is None:
                raise ValueError(f"Finding nicht gefunden: {finding_id}")
            finding = dict(detail.get("finding") or {})
            entity_texts = [
                str(x).strip()
                for x in (finding.get("entity_texts") or [])
                if str(x or "").strip()
            ]
            if not entity_texts:
                raise ValueError("Finding ohne Entity kann nicht als 'Nur Mentions' abgeschlossen werden")
            curated = {
                str(x.get("text") or "").casefold()
                for x in (detail.get("curated_entities") or [])
                if x and x.get("entity_id")
            }
            suppressed = {
                str(x).casefold()
                for x in (finding.get("suppressed_entity_texts") or [])
                if str(x or "").strip()
            }
            if any(text.casefold() not in curated | suppressed for text in entity_texts):
                raise ValueError("Vor 'Nur Mentions' müssen alle Entity-Entscheidungen abgeschlossen sein")
            active_claims = [
                x
                for x in (detail.get("claims") or [])
                if str((x or {}).get("curator_status") or "") in {"manual_claim", "review_required"}
            ]
            if active_claims:
                raise ValueError("Finding mit aktivem oder prüfpflichtigem Claim kann nicht als 'Nur Mentions' abgeschlossen werden")

        rows = self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})
            SET f.curator_status=$status,
                f.curator_reason=$reason,
                f.curator_actor=$curator_actor,
                f.curator_decided_at=datetime(),
                f.updated_at=datetime()
            RETURN f.finding_id AS finding_id,
                   properties(f)['curator_status'] AS curator_status,
                   properties(f)['curator_reason'] AS curator_reason
            """,
            finding_id=str(finding_id or "").strip(),
            status=clean_status,
            reason=str(reason or "").strip()[:1000],
            curator_actor=str(curator_actor or "manual_admin")[:300],
        )
        if not rows:
            raise ValueError(f"Finding nicht gefunden: {finding_id}")
        return dict(rows[0])

    def suppress_research_findings_without_entities(self) -> dict[str, Any]:
        """Suppress open findings whose evidence frame contains no entity text.

        Provenance is retained; this only removes obvious non-graph findings
        from the curator work queue.
        """
        rows = self._run(
            """
            MATCH (f:ResearchFinding)
            WHERE size(coalesce(properties(f)['entity_texts'],[])) = 0
              AND coalesce(properties(f)['curator_status'],'') <> 'suppressed'
            SET f.curator_status='suppressed',
                f.curator_reason='bulk_no_entity_cleanup',
                f.curator_decided_at=datetime(),
                f.updated_at=datetime()
            RETURN count(f) AS updated
            """
        )
        return {"action": "suppress_research_findings_without_entities", "updated": int((rows[0] if rows else {}).get("updated") or 0)}

    def research_finding_entity_options(self, finding_id: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Return per-text Entity Resolution options for one ResearchFinding."""
        detail = self.research_finding_detail(finding_id)
        if detail is None:
            raise ValueError(f"Finding nicht gefunden: {finding_id}")
        finding = dict(detail.get("finding") or {})
        stored_candidates = [
            {"text": str(text or "").strip(), "role": str(role or "")}
            for text, role in zip(
                finding.get("entity_texts") or [],
                list(finding.get("entity_roles") or []) + [""] * len(finding.get("entity_texts") or []),
            )
            if str(text or "").strip()
        ]
        candidates = merge_entity_candidates(
            stored_candidates,
            evidence_entity_candidates(finding.get("evidence_frame_json") or ""),
        )
        texts = [item["text"] for item in candidates]
        roles = [item["role"] for item in candidates]
        suppressed = {str(x).casefold() for x in (finding.get("suppressed_entity_texts") or [])}
        curated_by_text = {
            str(x.get("text") or "").casefold(): x
            for x in (detail.get("curated_entities") or [])
            if x and x.get("entity_id")
        }
        auto_by_text = {
            str(x.get("text") or "").casefold(): x
            for x in (detail.get("resolved_entities") or [])
            if x and x.get("entity_id")
        }
        out: list[dict[str, Any]] = []
        for index, text in enumerate(texts):
            key = text.casefold()
            decision = self._entity_form_decision(text)
            globally_suppressed = bool(
                decision and str(decision.get("status") or "") == "not_entity"
            )
            if key not in auto_by_text and not globally_suppressed:
                exact = self._resolve_existing_query_entity(text)
                if exact:
                    auto_by_text[key] = {
                        **dict(exact),
                        "text": text,
                        "role": roles[index] if index < len(roles) else "",
                    }
            suggestions = (
                [] if globally_suppressed
                else self.find_entities(text, limit=max(1, min(int(limit), 20)))
            )
            out.append({
                "text": text,
                "role": roles[index] if index < len(roles) else "",
                "suppressed": key in suppressed or globally_suppressed,
                "suppression_scope": (
                    "global" if globally_suppressed
                    else ("finding" if key in suppressed else "")
                ),
                "global_decision": decision,
                "curated": curated_by_text.get(key),
                "auto_resolved": auto_by_text.get(key),
                "suggestions": suggestions,
            })
        return out

    def research_finding_claim_options(self, finding_id: str) -> list[dict[str, Any]]:
        """Return ontology-admitted predicates for each ordered curated Entity pair."""
        detail = self.research_finding_detail(finding_id)
        if detail is None:
            raise ValueError(f"Finding nicht gefunden: {finding_id}")
        curated = [
            dict(item)
            for item in (detail.get("curated_entities") or [])
            if item and item.get("entity_id")
        ]
        out: list[dict[str, Any]] = []
        for subject in curated:
            subject_id = str(subject.get("entity_id") or "")
            subject_type = entity_type_from_labels(subject.get("labels"))
            subject_kind = str(subject.get("entity_kind") or subject_type)
            for obj in curated:
                object_id = str(obj.get("entity_id") or "")
                if not subject_id or not object_id or subject_id == object_id:
                    continue
                object_type = entity_type_from_labels(obj.get("labels"))
                object_kind = str(obj.get("entity_kind") or object_type)
                predicates = compatible_document_predicates(
                    _RELATION_ONTOLOGY,
                    subject_type=subject_type,
                    subject_kind=subject_kind,
                    object_type=object_type,
                    object_kind=object_kind,
                )
                if not predicates:
                    continue
                out.append({
                    "subject": {
                        "entity_id": subject_id,
                        "display_name": str(subject.get("display_name") or ""),
                        "entity_type": subject_type,
                        "entity_kind": subject_kind,
                    },
                    "object": {
                        "entity_id": object_id,
                        "display_name": str(obj.get("display_name") or ""),
                        "entity_type": object_type,
                        "entity_kind": object_kind,
                    },
                    "predicates": [
                        {
                            "id": name,
                            "label": ontology_predicate_label(name, spec, language="de"),
                            "description": str(spec.get("description") or ""),
                        }
                        for name, spec in predicates.items()
                    ],
                })
        return out

    def _create_manual_research_entity(self, *, display_name: str, entity_type: str, finding_id: str) -> dict[str, Any]:
        name = re.sub(r"\s+", " ", str(display_name or "")).strip()
        if not normalize_name(name):
            raise ValueError("Entity-Name ist leer")
        if entity_type not in {"Person", "Organization"}:
            raise ValueError("Entity-Typ muss Person oder Organization sein")
        entity_id = str(uuid.uuid4())
        identity_key = organization_identity_key(name) if entity_type == "Organization" else ""
        self._run(
            f"""
            CREATE (e:Entity:{entity_type} {{
                entity_id:$entity_id,
                display_name:$display_name,
                identity_key:$identity_key,
                entity_kind:$entity_type,
                origin:'research_finding_curator',
                identity_status:'confirmed',
                confirmation_method:'manual_curator',
                source_finding_id:$finding_id,
                confirmed_at:datetime(),
                created_at:datetime(),
                updated_at:datetime()
            }})
            MERGE (n:EntityName {{normalized:$normalized}})
            ON CREATE SET n.value=$display_name, n.created_at=datetime()
            SET n.last_seen_value=$display_name, n.updated_at=datetime()
            MERGE (e)-[r:HAS_NAME {{source_curator:'manual', normalized:$normalized}}]->(n)
            SET r.kind='manual_canonical', r.preferred=true, r.active=true,
                r.resolution_policy='exclusive', r.reason='research_finding_curator',
                r.updated_at=datetime()
            """,
            entity_id=entity_id,
            display_name=name,
            normalized=normalize_name(name),
            identity_key=identity_key,
            entity_type=entity_type,
            finding_id=str(finding_id or ""),
        )
        candidates = self.refresh_possible_same_as(entity_id)
        return {
            "entity_id": entity_id,
            "display_name": name,
            "entity_type": entity_type,
            "merge_candidates": candidates,
        }

    def curate_research_finding_entity_preview(
        self,
        finding_id: str,
        *,
        entity_text: str,
        action: str,
        target_entity_id: str = "",
        new_name: str = "",
        entity_type: str = "",
        reason: str = "",
        curator_actor: str = "manual_admin",
    ) -> dict[str, Any]:
        detail = self.research_finding_detail(finding_id)
        if detail is None:
            raise ValueError(f"Finding nicht gefunden: {finding_id}")
        finding = dict(detail.get("finding") or {})
        candidates = merge_entity_candidates(
            [
                {"text": str(text or "").strip(), "role": str(role or "")}
                for text, role in zip(
                    finding.get("entity_texts") or [],
                    list(finding.get("entity_roles") or []) + [""] * len(finding.get("entity_texts") or []),
                )
                if str(text or "").strip()
            ],
            evidence_entity_candidates(finding.get("evidence_frame_json") or ""),
        )
        texts = [item["text"] for item in candidates]
        text = str(entity_text or "").strip()
        if text not in texts:
            raise ValueError("Entity-Text gehört nicht zu diesem Finding")
        clean_action = str(action or "").strip().casefold()
        if clean_action not in {"assign", "assign_alias", "create", "suppress"}:
            raise ValueError(f"Ungültige Entity-Aktion: {action}")
        preview: dict[str, Any] = {
            "action": clean_action,
            "finding_id": finding_id,
            "document_id": str((detail.get("document") or {}).get("document_id") or ""),
            "entity_text": text,
            "reason": str(reason or "").strip(),
            "effect": "Entity-Resolution + dokumentgebundene MENTION-Provenienz; keine globale Faktenkante.",
        }
        if clean_action in {"assign", "assign_alias"}:
            target = self._entity_curation_summary(str(target_entity_id or "").strip())
            if target is None:
                raise ValueError("Ziel-Entity nicht gefunden")
            if str(target.get("identity_status") or "") in {"merged", "orphaned"}:
                raise ValueError("Ziel-Entity ist nicht aktiv")
            preview["target"] = target
            if clean_action == "assign_alias":
                preview["alias"] = text
                preview["alias_policy"] = "contextual"
                preview["effect"] = (
                    "Globale kontextuelle Aliasform anlegen und dieses Finding "
                    "dokumentgebunden der Entity zuordnen."
                )
        elif clean_action == "create":
            name = re.sub(r"\s+", " ", str(new_name or text)).strip()
            if not normalize_name(name):
                raise ValueError("Neuer Entity-Name ist leer")
            if entity_type not in {"Person", "Organization"}:
                raise ValueError("Bitte Entity-Typ Person oder Organization wählen")
            preview["new_entity"] = {"display_name": name, "entity_type": entity_type}
        else:
            preview["effect"] = (
                "Die normalisierte Form wird global als 'keine Entity' markiert; "
                "künftige Findings übernehmen diese Entscheidung. Provenienz bleibt erhalten."
            )
        preview["note"] = "Preview; keine Änderung."
        return preview

    def _cleanup_document_mention_if_unsupported(self, document_id: str, entity_id: str, observation_id: str) -> None:
        if not document_id or not entity_id:
            return
        rows = self._run(
            """
            MATCH (d:Document {document_id:$document_id})
            MATCH (e:Entity {entity_id:$entity_id})
            OPTIONAL MATCH (other:EntityObservation {document_id:$document_id})-[:RESOLVED_TO]->(e)
            WHERE other.observation_id <> $observation_id
              AND coalesce(properties(other)['curator_status'],'') <> 'manual_not_entity'
            RETURN count(other) AS support_count
            """,
            document_id=document_id,
            entity_id=entity_id,
            observation_id=observation_id,
        )
        unsupported = (not rows) or int(rows[0].get("support_count") or 0) == 0
        if unsupported:
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})-[r:MENTIONS]->(e:Entity {entity_id:$entity_id})
                DELETE r
                """,
                document_id=document_id,
                entity_id=entity_id,
            )

    def _mark_research_finding_claims_for_review(self, finding_id: str, *, reason: str) -> None:
        self._run(
            """
            MATCH (c:RelationObservation)-[:DERIVED_FROM_FINDING]->(f:ResearchFinding {finding_id:$finding_id})
            WHERE coalesce(properties(c)['curator_status'],'') = 'manual_claim'
            SET c.curator_status='review_required',
                c.review_reason=$reason,
                c.review_required_at=datetime(),
                c.updated_at=datetime()
            """,
            finding_id=finding_id,
            reason=str(reason or "finding_entity_changed")[:500],
        )

    def curate_research_finding_entity(
        self,
        finding_id: str,
        *,
        entity_text: str,
        action: str,
        target_entity_id: str = "",
        new_name: str = "",
        entity_type: str = "",
        reason: str = "",
        curator_actor: str = "manual_admin",
    ) -> dict[str, Any]:
        preview = self.curate_research_finding_entity_preview(
            finding_id,
            entity_text=entity_text,
            action=action,
            target_entity_id=target_entity_id,
            new_name=new_name,
            entity_type=entity_type,
            reason=reason,
        )
        detail = self.research_finding_detail(finding_id) or {}
        document_id = str((detail.get("document") or {}).get("document_id") or "")
        finding = dict(detail.get("finding") or {})
        text = str(entity_text or "").strip()
        clean_action = str(action or "").strip().casefold()
        prior_form_decision = self._entity_form_decision(text)
        prior_alias_target = (
            str((prior_form_decision or {}).get("target_entity_id") or "")
            if str((prior_form_decision or {}).get("decision_kind") or "") == "alias"
            else ""
        )
        finding_candidates = merge_entity_candidates(
            [
                {"text": str(text or "").strip(), "role": str(role or "")}
                for text, role in zip(
                    finding.get("entity_texts") or [],
                    list(finding.get("entity_roles") or []) + [""] * len(finding.get("entity_texts") or []),
                )
                if str(text or "").strip()
            ],
            evidence_entity_candidates(finding.get("evidence_frame_json") or ""),
        )
        self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})
            SET f.entity_texts=$entity_texts, f.entity_roles=$entity_roles, f.updated_at=datetime()
            """,
            finding_id=finding_id,
            entity_texts=[item["text"] for item in finding_candidates],
            entity_roles=[item["role"] for item in finding_candidates],
        )
        finding["entity_texts"] = [item["text"] for item in finding_candidates]
        finding["entity_roles"] = [item["role"] for item in finding_candidates]
        observation_id = hashlib.sha256(
            f"research_finding\0{finding_id}\0{text.casefold()}".encode("utf-8", errors="replace")
        ).hexdigest()[:40]

        prior = self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})
            OPTIONAL MATCH (f)-[ce:CURATED_ENTITY {frame_text:$entity_text}]->(old:Entity)
            RETURN old.entity_id AS old_entity_id
            """,
            finding_id=finding_id,
            entity_text=text,
        )
        old_entity_id = str(prior[0].get("old_entity_id") or "") if prior else ""

        if clean_action == "suppress":
            self._run(
                """
                MATCH (f:ResearchFinding {finding_id:$finding_id})-[s:SUPPORTED_BY]->(d:Document)
                OPTIONAL MATCH (f)-[ce:CURATED_ENTITY {frame_text:$entity_text}]->()
                DELETE ce
                SET f.suppressed_entity_texts =
                    [x IN coalesce(properties(f)['suppressed_entity_texts'],[]) WHERE toLower(x) <> toLower($entity_text)] + [$entity_text],
                    f.curator_status='',
                    f.curator_reason='',
                    f.updated_at=datetime()
                MERGE (o:EntityObservation {observation_id:$observation_id})
                ON CREATE SET o.created_at=datetime(), o.first_seen_at=datetime()
                SET o.document_id=d.document_id,
                    o.canonical_name=$entity_text,
                    o.observed_text=$entity_text,
                    o.context_text=$context_text,
                    o.normalized=$normalized,
                    o.extractor='research_finding_curator',
                    o.status='rejected',
                    o.rejection_reason='manual_not_an_entity',
                    o.curator_status='manual_not_entity',
                    o.curator_reason=$reason,
                    o.curator_decided_at=datetime(),
                    o.candidate_entity_ids=[],
                    o.updated_at=datetime()
                MERGE (d)-[hr:HAS_ENTITY_OBSERVATION]->(o)
                SET hr.extractor='research_finding_curator', hr.updated_at=datetime()
                WITH o
                OPTIONAL MATCH (o)-[rr:RESOLVED_TO]->()
                DELETE rr
                """,
                finding_id=finding_id,
                entity_text=text,
                observation_id=observation_id,
                context_text=str(finding.get("intent") or "")[:1000],
                normalized=normalize_name(text),
                reason=str(reason or "research_finding_not_entity")[:1000],
                curator_actor=str(curator_actor or "manual_admin")[:300],
            )
            if prior_alias_target:
                self._remove_manual_alias_form(prior_alias_target, text)
            self._set_entity_form_decision(
                text,
                status="not_entity",
                reason=str(reason or "research_finding_not_entity"),
                curator_actor=curator_actor,
                decision_kind="not_entity",
            )
            if old_entity_id:
                self._mark_research_finding_claims_for_review(finding_id, reason="finding_entity_suppressed")
            self._cleanup_document_mention_if_unsupported(document_id, old_entity_id, observation_id)
            return {
                **preview,
                "status": "suppressed_entity",
                "observation_id": observation_id,
                "suppression_scope": "global_form",
            }

        if clean_action == "create":
            if prior_alias_target:
                self._remove_manual_alias_form(prior_alias_target, text)
            created = self._create_manual_research_entity(
                display_name=str(new_name or text),
                entity_type=entity_type,
                finding_id=finding_id,
            )
            target_entity_id = str(created["entity_id"])
        elif clean_action == "assign_alias":
            if prior_alias_target and prior_alias_target != str(target_entity_id or "").strip():
                self._remove_manual_alias_form(prior_alias_target, text)
            self.add_alias(
                str(target_entity_id or "").strip(),
                text,
                policy="contextual",
                weight=0.95,
            )
        if clean_action == "assign" and prior_alias_target:
            self._remove_manual_alias_form(prior_alias_target, text)
        target = self._entity_curation_summary(str(target_entity_id or "").strip())
        if target is None:
            raise ValueError("Ziel-Entity nicht gefunden")
        labels = set(target.get("labels") or [])
        suggested_type = "Person" if "Person" in labels else "Organization" if "Organization" in labels else "Entity"
        role = ""
        texts = [str(x) for x in (finding.get("entity_texts") or [])]
        roles = [str(x) for x in (finding.get("entity_roles") or [])]
        for idx, value in enumerate(texts):
            if value == text and idx < len(roles):
                role = roles[idx]
                break

        self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})-[s:SUPPORTED_BY]->(d:Document)
            MATCH (target:Entity {entity_id:$target_entity_id})
            OPTIONAL MATCH (f)-[old:CURATED_ENTITY {frame_text:$entity_text}]->()
            DELETE old
            MERGE (f)-[ce:CURATED_ENTITY {frame_text:$entity_text}]->(target)
            SET ce.role=$role,
                ce.curated_by=$curator_actor,
                ce.curated_at=datetime(),
                ce.updated_at=datetime(),
                f.suppressed_entity_texts = [x IN coalesce(properties(f)['suppressed_entity_texts'],[]) WHERE toLower(x) <> toLower($entity_text)],
                f.curator_status='',
                f.curator_reason='',
                f.updated_at=datetime()
            MERGE (o:EntityObservation {observation_id:$observation_id})
            ON CREATE SET o.created_at=datetime(), o.first_seen_at=datetime()
            SET o.document_id=d.document_id,
                o.canonical_name=$entity_text,
                o.observed_text=$entity_text,
                o.context_text=$context_text,
                o.normalized=$normalized,
                o.suggested_type=$suggested_type,
                o.entity_kind=$suggested_type,
                o.confidence=1.0,
                o.relevant_actor=true,
                o.mention_context='research_finding',
                o.eligible=true,
                o.allow_create=false,
                o.admission_reason='manual_research_finding_resolution',
                o.extractor='research_finding_curator',
                o.status='corrected',
                o.rejection_reason=null,
                o.curator_status='research_finding_entity',
                o.curator_target_entity_id=target.entity_id,
                o.curator_reason=$reason,
                o.curator_actor=$curator_actor,
                o.curator_decided_at=datetime(),
                o.candidate_entity_ids=[target.entity_id],
                o.updated_at=datetime()
            MERGE (d)-[hr:HAS_ENTITY_OBSERVATION]->(o)
            SET hr.extractor='research_finding_curator', hr.updated_at=datetime()
            WITH d, o, target
            OPTIONAL MATCH (o)-[rr:RESOLVED_TO]->()
            DELETE rr
            MERGE (o)-[resolved:RESOLVED_TO]->(target)
            SET resolved.resolved_by='manual_research_finding', resolved.updated_at=datetime()
            MERGE (d)-[m:MENTIONS]->(target)
            SET m.research_finding_curated=true,
                m.last_seen_at=datetime(), m.updated_at=datetime()
            """,
            finding_id=finding_id,
            entity_text=text,
            target_entity_id=str(target_entity_id),
            role=role,
            observation_id=observation_id,
            context_text=str(finding.get("intent") or "")[:1000],
            normalized=normalize_name(text),
            suggested_type=suggested_type,
            reason=str(reason or "manual_research_finding_resolution")[:1000],
            curator_actor=str(curator_actor or "manual_admin")[:300],
        )
        self._set_entity_form_decision(
            text,
            status="entity",
            target_entity_id=str(target_entity_id),
            reason=str(reason or (
                "manual_alias_assignment" if clean_action == "assign_alias"
                else "manual_research_finding_resolution"
            )),
            curator_actor=curator_actor,
            decision_kind=(
                "alias" if clean_action == "assign_alias"
                else ("created" if clean_action == "create" else "assignment")
            ),
        )
        if old_entity_id and old_entity_id != str(target_entity_id):
            self._mark_research_finding_claims_for_review(finding_id, reason="finding_entity_reassigned")
            self._cleanup_document_mention_if_unsupported(document_id, old_entity_id, observation_id)
        return {
            **preview,
            "status": (
                "resolved_entity_with_alias" if clean_action == "assign_alias"
                else "resolved_entity"
            ),
            "observation_id": observation_id,
            "target_entity_id": str(target_entity_id),
            "target_display_name": str(target.get("display_name") or ""),
        }

    def accept_research_finding_entity_defaults(
        self,
        finding_id: str,
        *,
        curator_actor: str = "manual_admin",
    ) -> dict[str, Any]:
        """Apply every pending unique exact-name or alias default in one action."""
        applied: list[dict[str, str]] = []
        for option in self.research_finding_entity_options(finding_id, limit=1):
            target = option.get("auto_resolved")
            if option.get("curated") or option.get("suppressed") or not target:
                continue
            result = self.curate_research_finding_entity(
                finding_id,
                entity_text=str(option.get("text") or ""),
                action=(
                    "assign_alias"
                    if str(target.get("match_kind") or "") == "alias"
                    else "assign"
                ),
                target_entity_id=str(target.get("entity_id") or ""),
                reason=(
                    "unique_alias_default"
                    if str(target.get("match_kind") or "") == "alias"
                    else "unique_exact_name_default"
                ),
                curator_actor=curator_actor,
            )
            applied.append({
                "text": str(option.get("text") or ""),
                "entity_id": str(result.get("target_entity_id") or ""),
                "display_name": str(result.get("target_display_name") or ""),
                "match_kind": str(target.get("match_kind") or "name"),
            })
        return {
            "status": "entity_defaults_applied",
            "finding_id": finding_id,
            "applied_count": len(applied),
            "applied": applied,
        }

    def research_finding_claim_preview(
        self,
        finding_id: str,
        *,
        subject_entity_id: str,
        predicate_id: str,
        object_entity_id: str,
        predicate_label: str = "",
        claim_text: str = "",
    ) -> dict[str, Any]:
        """Validate one manual claim against the versioned relation ontology."""
        predicate = str(predicate_id or "").strip().replace("-", "_").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", predicate):
            raise ValueError("Predicate-ID ist ungültig")
        if subject_entity_id == object_entity_id:
            raise ValueError("Subject und Object müssen verschieden sein")
        rows = self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})-[s:SUPPORTED_BY]->(d:Document)
            MATCH (f)-[:CURATED_ENTITY]->(subject:Entity {entity_id:$subject_entity_id})
            MATCH (f)-[:CURATED_ENTITY]->(object:Entity {entity_id:$object_entity_id})
            RETURN d.document_id AS document_id,
                   subject.display_name AS subject_name,
                   labels(subject) AS subject_labels,
                   coalesce(properties(subject)['entity_kind'],'') AS subject_kind,
                   object.display_name AS object_name,
                   labels(object) AS object_labels,
                   coalesce(properties(object)['entity_kind'],'') AS object_kind,
                   properties(f)['intent'] AS intent
            LIMIT 1
            """,
            finding_id=finding_id,
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
        )
        if not rows:
            raise ValueError("Subject/Object müssen für dieses Finding kuratierte Entities sein")
        row = dict(rows[0])
        subject_type = entity_type_from_labels(row.get("subject_labels"))
        object_type = entity_type_from_labels(row.get("object_labels"))
        subject_kind = str(row.get("subject_kind") or subject_type)
        object_kind = str(row.get("object_kind") or object_type)
        compatible = compatible_document_predicates(
            _RELATION_ONTOLOGY,
            subject_type=subject_type,
            subject_kind=subject_kind,
            object_type=object_type,
            object_kind=object_kind,
        )
        spec = compatible.get(predicate)
        if spec is None:
            known = (_RELATION_ONTOLOGY.get("predicates") or {}).get(predicate)
            if known is None:
                raise ValueError(f"Predicate {predicate} ist nicht Teil der Relation-Ontologie")
            raise ValueError(
                f"Predicate {predicate} ist für {subject_type}/{subject_kind} -> "
                f"{object_type}/{object_kind} nicht zugelassen"
            )
        canonical_label = ontology_predicate_label(predicate, spec, language="de")
        relation_id = hashlib.sha256(
            f"research_finding_claim\0{finding_id}\0{subject_entity_id}\0{predicate}\0{object_entity_id}".encode("utf-8")
        ).hexdigest()[:40]
        return {
            "action": "create_research_claim",
            "finding_id": finding_id,
            "document_id": row.get("document_id"),
            "relation_id": relation_id,
            "subject": {
                "entity_id": subject_entity_id,
                "display_name": row.get("subject_name"),
                "entity_type": subject_type,
                "entity_kind": subject_kind,
            },
            "predicate_id": predicate,
            "predicate_label": canonical_label,
            "object": {
                "entity_id": object_entity_id,
                "display_name": row.get("object_name"),
                "entity_type": object_type,
                "entity_kind": object_kind,
            },
            "claim_text": str(claim_text or "").strip(),
            "ontology": {
                "name": str(_RELATION_ONTOLOGY.get("name") or ""),
                "version": _RELATION_ONTOLOGY.get("version"),
                "hash": str(_RELATION_ONTOLOGY.get("hash") or ""),
            },
            "effect": "Erzeugt eine dokumentgebundene RelationObservation/Claim mit DERIVED_FROM_FINDING; noch keine globale Faktenkante.",
            "note": "Preview; keine Änderung.",
        }

    def create_research_finding_claim(
        self,
        finding_id: str,
        *,
        subject_entity_id: str,
        predicate_id: str,
        object_entity_id: str,
        predicate_label: str = "",
        claim_text: str = "",
        curator_actor: str = "manual_admin",
    ) -> dict[str, Any]:
        preview = self.research_finding_claim_preview(
            finding_id,
            subject_entity_id=subject_entity_id,
            predicate_id=predicate_id,
            object_entity_id=object_entity_id,
            predicate_label=predicate_label,
            claim_text=claim_text,
        )
        detail = self.research_finding_detail(finding_id) or {}
        finding = dict(detail.get("finding") or {})
        relation_id = str(preview["relation_id"])
        predicate = str(preview["predicate_id"])
        canonical_label = str(preview["predicate_label"])
        relation_text = (
            f"{preview['subject']['display_name']} — {canonical_label} → "
            f"{preview['object']['display_name']}"
        )
        curator_note = str(claim_text or "").strip()[:1000]
        evidence_frame = str(finding.get("evidence_frame_json") or "").strip()
        evidence_summary = evidence_frame[:2000] if evidence_frame else relation_text[:2000]
        ontology = dict(preview.get("ontology") or {})
        self._run(
            """
            MATCH (f:ResearchFinding {finding_id:$finding_id})-[support:SUPPORTED_BY]->(d:Document)
            MATCH (f)-[:CURATED_ENTITY]->(subject:Entity {entity_id:$subject_entity_id})
            MATCH (f)-[:CURATED_ENTITY]->(object:Entity {entity_id:$object_entity_id})
            MERGE (c:Claim:RelationObservation {relation_id:$relation_id})
            ON CREATE SET c.created_at=datetime()
            SET c.document_id=d.document_id,
                c.predicate=$predicate,
                c.predicate_text=$predicate_label,
                c.relation_text=$relation_text,
                c.evidence_text=$evidence_text,
                c.curator_note=$curator_note,
                c.confidence=1.0,
                c.stance='asserted',
                c.chunk_index=0,
                c.extractor='research_finding_curator',
                c.curator_status='manual_claim',
                c.curator_reason='ontology_curated',
                c.curator_actor=$curator_actor,
                c.review_reason=null,
                c.review_required_at=null,
                c.source_finding_id=$finding_id,
                c.ontology_name=$ontology_name,
                c.ontology_version=$ontology_version,
                c.ontology_hash=$ontology_hash,
                c.evidence_date=coalesce(properties(d)['source_date'],''),
                c.evidence_date_precision=coalesce(properties(d)['source_date_precision'],''),
                c.updated_at=datetime()
            MERGE (d)-[dr:HAS_RELATION_OBSERVATION]->(c)
            SET dr.extractor='research_finding_curator', dr.updated_at=datetime()
            MERGE (c)-[:SUBJECT]->(subject)
            MERGE (c)-[:OBJECT]->(object)
            MERGE (c)-[:DERIVED_FROM_FINDING]->(f)
            SET f.curator_status='',
                f.curator_reason='',
                f.updated_at=datetime()
            """,
            finding_id=finding_id,
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
            relation_id=relation_id,
            predicate=predicate,
            predicate_label=canonical_label[:300],
            relation_text=relation_text[:1000],
            evidence_text=evidence_summary,
            curator_note=curator_note,
            curator_actor=str(curator_actor or "manual_admin")[:300],
            ontology_name=str(ontology.get("name") or "")[:200],
            ontology_version=ontology.get("version"),
            ontology_hash=str(ontology.get("hash") or "")[:100],
        )
        return {**preview, "status": "claim_created", "relation_id": relation_id}

    def review_research_finding_claim(
        self,
        finding_id: str,
        *,
        relation_id: str,
        action: str,
        reason: str = "",
        curator_actor: str = "manual_admin",
    ) -> dict[str, Any]:
        """Review or withdraw a manual finding claim without deleting provenance."""
        detail = self.research_finding_detail(finding_id)
        if detail is None:
            raise ValueError(f"Finding nicht gefunden: {finding_id}")
        claim = next(
            (
                dict(item)
                for item in (detail.get("claims") or [])
                if str((item or {}).get("relation_id") or "") == str(relation_id or "")
            ),
            None,
        )
        if claim is None:
            raise ValueError("Claim nicht gefunden")

        current_status = str(claim.get("curator_status") or "")
        clean_action = str(action or "").strip().casefold()
        if clean_action == "confirm":
            if current_status != "review_required":
                raise ValueError("Nur ein prüfpflichtiger Claim kann erneut bestätigt werden")
            self.research_finding_claim_preview(
                finding_id,
                subject_entity_id=str(claim.get("subject_entity_id") or ""),
                predicate_id=str(claim.get("predicate") or ""),
                object_entity_id=str(claim.get("object_entity_id") or ""),
            )
            new_status = "manual_claim"
            curator_reason = str(reason or "review_confirmed")[:1000]
        elif clean_action == "dismiss":
            if current_status != "review_required":
                raise ValueError("Nur ein prüfpflichtiger Claim kann als überholt markiert werden")
            new_status = "superseded"
            curator_reason = str(reason or "review_superseded")[:1000]
        elif clean_action == "withdraw":
            if current_status not in {"manual_claim", "review_required"}:
                raise ValueError("Nur ein aktiver oder prüfpflichtiger Claim kann zurückgenommen werden")
            new_status = "withdrawn"
            curator_reason = str(reason or "manually_withdrawn")[:1000]
        else:
            raise ValueError("Ungültige Claim-Aktion")

        rows = self._run(
            """
            MATCH (c:RelationObservation {relation_id:$relation_id})-[:DERIVED_FROM_FINDING]->(f:ResearchFinding {finding_id:$finding_id})
            SET c.curator_status=$status,
                c.curator_reason=$reason,
                c.curator_actor=$curator_actor,
                c.review_reason=null,
                c.review_required_at=null,
                c.updated_at=datetime(),
                f.updated_at=datetime()
            RETURN c.relation_id AS relation_id, properties(c)['curator_status'] AS curator_status
            """,
            finding_id=finding_id,
            relation_id=str(relation_id or ""),
            status=new_status,
            reason=curator_reason,
            curator_actor=str(curator_actor or "manual_admin")[:300],
        )
        if not rows:
            raise ValueError("Claim nicht gefunden")
        return {
            "action": clean_action,
            "finding_id": finding_id,
            "relation_id": str(relation_id or ""),
            "curator_status": new_status,
        }

    def document_research_findings(self, document_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._run(
            """
            MATCH (f:ResearchFinding)-[r:SUPPORTED_BY]->(d:Document {document_id:$document_id})
            OPTIONAL MATCH (f)-[qe:QUERY_ENTITY]->(e:Entity)
            WITH f, r, collect({
                frame_entity_id: properties(qe)['frame_entity_id'],
                role: properties(qe)['role'],
                text: properties(qe)['text'],
                entity_id: e.entity_id,
                display_name: e.display_name
            }) AS resolved_entities
            RETURN properties(f) AS finding,
                   properties(r) AS support,
                   resolved_entities
            ORDER BY properties(f)['last_seen_at'] DESC
            LIMIT $limit
            """,
            document_id=document_id,
            limit=max(1, int(limit or 50)),
        )

    def document_summary(self, document_id: str) -> dict[str, Any]:
        docs = self._run(
            """
            MATCH (d:Document {document_id:$document_id})
            RETURN properties(d) AS document
            """,
            document_id=document_id,
        )
        if not docs:
            return {"document_id": document_id, "found": False, "mentions": [], "mention_names": []}

        mentions = self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[r:MENTIONS]->(e:Entity)
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   properties(r)['mention_count'] AS mention_count,
                   properties(r)['observed_values'] AS observed_values,
                   properties(r)['resolution'] AS resolution,
                   properties(r)['max_score'] AS max_score
            ORDER BY coalesce(properties(r)['mention_count'],0) DESC, e.display_name
            """,
            document_id=document_id,
        )
        mention_names = self._run(
            """
            MATCH (d:Document {document_id:$document_id})-[r:MENTIONS_NAME]->(m:MentionName)
            RETURN m.normalized AS normalized,
                   coalesce(properties(m)['last_seen_value'],properties(m)['value']) AS value,
                   properties(r)['status'] AS status,
                   properties(r)['mention_count'] AS mention_count,
                   properties(r)['observed_values'] AS observed_values,
                   properties(r)['candidate_entity_ids'] AS candidate_entity_ids,
                   properties(r)['candidate_scores'] AS candidate_scores
            ORDER BY coalesce(properties(r)['mention_count'],0) DESC, m.normalized
            """,
            document_id=document_id,
        )
        relation_observations = self.document_relation_observations(document_id)
        research_findings = self.document_research_findings(document_id)
        return {
            "document_id": document_id,
            "found": True,
            "document": docs[0].get("document") or {},
            "mentions": mentions,
            "mention_names": mention_names,
            "relation_observations": relation_observations,
            "research_findings": research_findings,
        }

    def _entity_curation_summary(self, entity_id: str) -> dict[str, Any] | None:
        rows = self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   coalesce(properties(e)['origin'],'') AS origin,
                   coalesce(properties(e)['identity_status'],'') AS identity_status,
                   coalesce(e.identity_key,'') AS identity_key,
                   size([(d:Document)-[:MENTIONS]->(e) | d]) AS document_mentions,
                   size([(o:EntityObservation)-[:RESOLVED_TO]->(e) | o]) AS observations,
                   size([(c:ContactRecord)-[:DESCRIBES]->(e) | c]) AS contact_records,
                   size([(e)-[:HAS_NAME]->(:EntityName) | 1]) AS names,
                   size([(e)-[:HAS_SEARCH_ALIAS]->(:SearchAlias) | 1]) AS aliases
            """,
            entity_id=entity_id,
        )
        return dict(rows[0]) if rows else None

    def entity_observations(self, entity_id: str) -> list[dict[str, Any]]:
        """List grounded observations currently or manually associated with an entity."""
        return self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            MATCH (o:EntityObservation)
            WHERE (o)-[:RESOLVED_TO]->(e)
               OR o.curator_target_entity_id=$entity_id
            OPTIONAL MATCH (d:Document)-[document_observation]->(o)
            WHERE type(document_observation)='HAS_ENTITY_OBSERVATION'
            RETURN o.observation_id AS observation_id,
                   properties(o)['document_id'] AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(o)['observed_text'] AS observed_text,
                   properties(o)['canonical_name'] AS canonical_name,
                   properties(o)['normalized'] AS normalized,
                   properties(o)['suggested_type'] AS suggested_type,
                   properties(o)['entity_kind'] AS entity_kind,
                   properties(o)['mention_context'] AS mention_context,
                   properties(o)['relevant_actor'] AS relevant_actor,
                   properties(o)['admission_reason'] AS admission_reason,
                   properties(o)['status'] AS status,
                   properties(o)['extractor'] AS extractor,
                   properties(o)['curator_status'] AS curator_status,
                   properties(o)['curator_reason'] AS curator_reason,
                   properties(o)['curator_target_entity_id'] AS curator_target_entity_id
            ORDER BY document_id, observation_id
            """,
            entity_id=entity_id,
        )

    def list_observations(
        self,
        *,
        query: str = "",
        status: str = "needs_review",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Curator-oriented observation list for the browser UI.

        needs_review is a derived work-queue state: only unresolved,
        ambiguous, or provisional automatic observations without a human
        decision are included. Other values match extractor status or
        curator_status exactly. This is read-only and does not alter resolution.
        """
        needle = str(query or "").strip().casefold()
        status = str(status or "needs_review").strip()
        rows = self._run(
            """
            MATCH (o:EntityObservation)
            OPTIONAL MATCH (d:Document)-[document_observation]->(o)
            WHERE type(document_observation)='HAS_ENTITY_OBSERVATION'
            OPTIONAL MATCH (o)-[:RESOLVED_TO]->(e:Entity)
            WHERE (
                    $status='all'
                    OR (
                        $status='needs_review'
                        AND coalesce(properties(o)['curator_status'],'')=''
                        AND coalesce(properties(o)['status'],'') IN ['created_provisional','ambiguous','unresolved']
                    )
                    OR (
                        $status <> 'all' AND $status <> 'needs_review'
                        AND (
                            coalesce(properties(o)['status'],'')=$status
                            OR coalesce(properties(o)['curator_status'],'')=$status
                        )
                    )
                  )
              AND ($needle=''
                   OR toLower(coalesce(properties(o)['observed_text'],'')) CONTAINS $needle
                   OR toLower(coalesce(properties(o)['canonical_name'],'')) CONTAINS $needle
                   OR toLower(coalesce(properties(d)['title'],'')) CONTAINS $needle
                   OR toLower(coalesce(properties(d)['path'],'')) CONTAINS $needle
                   OR toLower(coalesce(e.display_name,'')) CONTAINS $needle)
            RETURN o.observation_id AS observation_id,
                   properties(o)['document_id'] AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(o)['observed_text'] AS observed_text,
                   properties(o)['canonical_name'] AS canonical_name,
                   properties(o)['normalized'] AS normalized,
                   properties(o)['suggested_type'] AS suggested_type,
                   properties(o)['entity_kind'] AS entity_kind,
                   properties(o)['mention_context'] AS mention_context,
                   properties(o)['relevant_actor'] AS relevant_actor,
                   properties(o)['admission_reason'] AS admission_reason,
                   properties(o)['status'] AS status,
                   properties(o)['extractor'] AS extractor,
                   properties(o)['curator_status'] AS curator_status,
                   properties(o)['curator_reason'] AS curator_reason,
                   properties(o)['curator_target_entity_id'] AS curator_target_entity_id,
                   e.entity_id AS resolved_entity_id,
                   e.display_name AS resolved_entity_name
            ORDER BY coalesce(properties(d)['path'],properties(d)['title'],properties(o)['document_id']), o.observation_id
            LIMIT $limit
            """,
            needle=needle,
            status=status,
            limit=max(1, min(int(limit), 2000)),
        )
        status_labels = {
            "resolved_existing": "automatisch eindeutig",
            "created_provisional": "provisional erzeugt",
            "ambiguous": "mehrdeutig",
            "unresolved": "nicht aufgelöst",
            "rejected": "verworfen",
            "corrected": "manuell aufgelöst",
        }
        curator_labels = {
            "research_finding_entity": "im Finding bestätigt",
            "corrected_observation": "manuell korrigiert",
            "manual_not_entity": "manuell Non-Entity",
        }
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            extractor_status = str(item.get("status") or "")
            curator_status = str(item.get("curator_status") or "")
            item["needs_review"] = (
                not curator_status
                and extractor_status in {"created_provisional", "ambiguous", "unresolved"}
            )
            if status == "needs_review" and not item["needs_review"]:
                continue
            if (
                status not in {"all", "needs_review"}
                and status not in {extractor_status, curator_status}
            ):
                continue
            item["status_label"] = status_labels.get(extractor_status, extractor_status or "—")
            item["curator_status_label"] = curator_labels.get(curator_status, curator_status or "—")
            out.append(item)
        return out

    def observation_detail(self, observation_id: str) -> dict[str, Any] | None:
        """Public read-only wrapper used by GraphCurator/admin UI."""
        row = self._observation_curation_summary(observation_id)
        if row is None:
            return None
        extractor_status = str(row.get("status") or "")
        curator_status = str(row.get("curator_status") or "")
        row["needs_review"] = (
            not curator_status
            and extractor_status in {"created_provisional", "ambiguous", "unresolved"}
        )
        row["status_label"] = {
            "resolved_existing": "automatisch eindeutig",
            "created_provisional": "provisional erzeugt",
            "ambiguous": "mehrdeutig",
            "unresolved": "nicht aufgelöst",
            "rejected": "verworfen",
            "corrected": "manuell aufgelöst",
        }.get(extractor_status, extractor_status or "—")
        row["curator_status_label"] = {
            "research_finding_entity": "im Finding bestätigt",
            "corrected_observation": "manuell korrigiert",
            "manual_not_entity": "manuell Non-Entity",
        }.get(curator_status, curator_status or "—")
        return row

    def _observation_curation_summary(self, observation_id: str) -> dict[str, Any] | None:
        rows = self._run(
            """
            MATCH (o:EntityObservation {observation_id:$observation_id})
            OPTIONAL MATCH (d:Document)-[document_observation]->(o)
            WHERE type(document_observation)='HAS_ENTITY_OBSERVATION'
            OPTIONAL MATCH (o)-[:RESOLVED_TO]->(e:Entity)
            RETURN o.observation_id AS observation_id,
                   properties(o)['document_id'] AS document_id,
                   properties(d)['title'] AS document_title,
                   properties(d)['path'] AS document_path,
                   properties(o)['observed_text'] AS observed_text,
                   properties(o)['canonical_name'] AS canonical_name,
                   properties(o)['normalized'] AS normalized,
                   properties(o)['suggested_type'] AS suggested_type,
                   properties(o)['entity_kind'] AS entity_kind,
                   properties(o)['mention_context'] AS mention_context,
                   properties(o)['relevant_actor'] AS relevant_actor,
                   properties(o)['admission_reason'] AS admission_reason,
                   properties(o)['status'] AS status,
                   properties(o)['extractor'] AS extractor,
                   properties(o)['curator_status'] AS curator_status,
                   properties(o)['curator_reason'] AS curator_reason,
                   properties(o)['curator_target_entity_id'] AS curator_target_entity_id,
                   e.entity_id AS resolved_entity_id,
                   e.display_name AS resolved_entity_name
            LIMIT 1
            """,
            observation_id=observation_id,
        )
        return dict(rows[0]) if rows else None

    def correct_name_preview(self, entity_id: str, new_name: str, *, reason: str = "manual_name_correction") -> dict[str, Any]:
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        if str(entity.get("identity_status") or "") in {"merged", "orphaned"}:
            raise ValueError("Gemergte/verwaiste Entity kann nicht direkt umbenannt werden")
        new_name = str(new_name or "").strip()
        if not normalize_name(new_name):
            raise ValueError("Neuer Name ist leer")
        return {
            "action": "correct_name",
            "entity": entity,
            "new_name": new_name,
            "new_normalized": normalize_name(new_name),
            "reason": str(reason or "manual_name_correction"),
            "will_confirm_entity": True,
            "old_display_name_will_not_become_global_alias": True,
            "note": "Ohne --yes wird nichts verändert.",
        }

    def correct_name(self, entity_id: str, new_name: str, *, reason: str = "manual_name_correction") -> dict[str, Any]:
        preview = self.correct_name_preview(entity_id, new_name, reason=reason)
        entity = preview["entity"]
        old_name = str(entity.get("display_name") or "").strip()
        old_norm = normalize_name(old_name)
        new_name = str(preview["new_name"])
        new_norm = str(preview["new_normalized"])
        labels = set(entity.get("labels") or [])
        entity_type = "Organization" if "Organization" in labels else "Person" if "Person" in labels else ""
        if not entity_type:
            raise ValueError("Nur Person/Organization kann mit correct-name kuratiert werden")

        # The old display spelling is explicitly being corrected, not promoted
        # to a global alias. Retire matching name relations and document-derived
        # aliases, while keeping their nodes/provenance available for audit.
        if old_norm and old_norm != new_norm:
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_NAME]->(n:EntityName {normalized:$old_norm})
                SET r.active=false, r.preferred=false,
                    r.retired_by='manual_name_correction', r.retired_at=datetime(), r.updated_at=datetime()
                """,
                entity_id=entity_id,
                old_norm=old_norm,
            )
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})-[r:HAS_SEARCH_ALIAS]->(:SearchAlias)
                WHERE properties(r)['source_document_id'] IS NOT NULL
                SET r.active=false, r.retired_by='manual_name_correction',
                    r.retired_at=datetime(), r.updated_at=datetime()
                """,
                entity_id=entity_id,
            )

        identity_key = organization_identity_key(new_name) if entity_type == "Organization" else ""
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            MERGE (n:EntityName {normalized:$normalized})
            ON CREATE SET n.value=$new_name, n.created_at=datetime()
            SET n.last_seen_value=$new_name, n.updated_at=datetime()
            MERGE (e)-[r:HAS_NAME {source_curator:'manual', normalized:$normalized}]->(n)
            SET r.kind='manual_canonical', r.preferred=true, r.active=true,
                r.resolution_policy='exclusive', r.reason=$reason, r.updated_at=datetime()
            SET e.display_name=$new_name,
                e.identity_status='confirmed',
                e.confirmation_method='manual_curator',
                e.confirmed_at=datetime(),
                e.identity_key=CASE WHEN $identity_key <> '' THEN $identity_key ELSE e.identity_key END,
                e.updated_at=datetime()
            """,
            entity_id=entity_id,
            normalized=new_norm,
            new_name=new_name,
            reason=str(reason or "manual_name_correction"),
            identity_key=identity_key,
        )

        # Recreate only safe aliases from the corrected canonical name.
        alias_specs: list[dict[str, Any]] = []
        if entity_type == "Organization":
            alias_specs.extend(generated_organization_aliases(new_name))
        if "²" in new_name:
            alias_specs.append({"value": new_name.replace("²", "^2"), "kind": "superscript_ascii", "weight": 0.96})
        seen_aliases: set[str] = set()
        for item in alias_specs:
            value = str(item.get("value") or "").strip()
            norm = str(item.get("normalized") or normalize_name(value)).strip()
            if not norm or norm == new_norm or norm in seen_aliases:
                continue
            seen_aliases.add(norm)
            self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                MERGE (a:SearchAlias {normalized:$normalized})
                ON CREATE SET a.value=$value, a.created_at=datetime()
                SET a.last_seen_value=$value, a.updated_at=datetime()
                MERGE (e)-[r:HAS_SEARCH_ALIAS {source_curator:'manual', normalized:$normalized}]->(a)
                SET r.kind=$kind, r.weight=$weight, r.active=true,
                        r.resolution_policy='contextual', r.updated_at=datetime()
                """,
                entity_id=entity_id,
                normalized=norm,
                value=value,
                kind=str(item.get("kind") or "manual_alias"),
                weight=float(item.get("weight") or 0.9),
            )

        candidates = self.refresh_possible_same_as(entity_id)
        return {
            "status": "corrected_name",
            "entity_id": entity_id,
            "old_display_name": old_name,
            "display_name": new_name,
            "identity_status": "confirmed",
            "reason": str(reason or "manual_name_correction"),
            "merge_candidates": candidates,
        }

    def correct_observation_preview(self, observation_id: str, target_entity_id: str, *, reason: str = "ocr") -> dict[str, Any]:
        observation = self._observation_curation_summary(observation_id)
        target = self._entity_curation_summary(target_entity_id)
        if observation is None:
            raise ValueError(f"Observation nicht gefunden: {observation_id}")
        if target is None:
            raise ValueError(f"Ziel-Entity nicht gefunden: {target_entity_id}")
        if str(target.get("identity_status") or "") in {"merged", "orphaned"}:
            raise ValueError("Ziel-Entity ist nicht aktiv")
        labels = set(target.get("labels") or [])
        expected = str(observation.get("suggested_type") or "")
        if expected and expected not in labels:
            raise ValueError(f"Typkonflikt: Observation={expected}, Ziel={labels}")
        return {
            "action": "correct_observation",
            "observation": observation,
            "target": target,
            "reason": str(reason or "ocr"),
            "scope": "document_observation_only",
            "will_not_create_global_alias": True,
            "note": "Ohne --yes wird nichts verändert.",
        }

    def correct_observation(self, observation_id: str, target_entity_id: str, *, reason: str = "ocr") -> dict[str, Any]:
        preview = self.correct_observation_preview(observation_id, target_entity_id, reason=reason)
        observation = preview["observation"]
        old_entity_id = str(observation.get("resolved_entity_id") or "")
        document_id = str(observation.get("document_id") or "")
        observed_text = str(observation.get("observed_text") or observation.get("canonical_name") or "").strip()
        observation_extractor = str(observation.get("extractor") or "manual_curator")

        self._run(
            """
            MATCH (o:EntityObservation {observation_id:$observation_id})
            MATCH (target:Entity {entity_id:$target_entity_id})
            OPTIONAL MATCH (o)-[old:RESOLVED_TO]->()
            DELETE old
            MERGE (o)-[r:RESOLVED_TO]->(target)
            SET r.resolved_by='manual_observation_correction', r.updated_at=datetime(),
                o.status='corrected', o.rejection_reason=null,
                o.curator_status='corrected_observation',
                o.curator_target_entity_id=$target_entity_id,
                o.curator_reason=$reason,
                o.curator_decided_at=datetime(),
                o.candidate_entity_ids=[$target_entity_id],
                o.updated_at=datetime()
            """,
            observation_id=observation_id,
            target_entity_id=target_entity_id,
            reason=str(reason or "ocr"),
        )

        # Make the current graph immediately consistent. Future relink passes
        # are protected by the manual overlay in replace_document_mentions().
        if document_id:
            if old_entity_id and old_entity_id != target_entity_id:
                still_supported = self._run(
                    """
                    MATCH (o:EntityObservation {document_id:$document_id})-[:RESOLVED_TO]->(e:Entity {entity_id:$old_entity_id})
                    WHERE o.observation_id <> $observation_id
                      AND coalesce(properties(o)['curator_status'],'') <> 'manual_not_entity'
                    RETURN count(o) > 0 AS supported
                    """,
                    document_id=document_id,
                    old_entity_id=old_entity_id,
                    observation_id=observation_id,
                )
                if not (still_supported and bool(still_supported[0].get("supported"))):
                    self._run(
                        """
                        MATCH (d:Document {document_id:$document_id})-[r:MENTIONS]->(e:Entity {entity_id:$old_entity_id})
                        DELETE r
                        """,
                        document_id=document_id,
                        old_entity_id=old_entity_id,
                    )
            self._run(
                """
                MATCH (d:Document {document_id:$document_id})
                MATCH (e:Entity {entity_id:$target_entity_id})
                OPTIONAL MATCH (d)-[existing:MENTIONS]->(e)
                WITH d,e,existing
                FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END |
                    CREATE (d)-[r:MENTIONS {extractor:$observation_extractor}]->(e)
                    SET r.mention_count=1,
                        r.observed_values=CASE WHEN $observed_text='' THEN [] ELSE [$observed_text] END,
                        r.resolution='manual_observation_correction',
                        r.max_score=1.0, r.updated_at=datetime()
                )
                FOREACH (_ IN CASE WHEN existing IS NULL THEN [] ELSE [1] END |
                    SET existing.observed_values=CASE
                            WHEN $observed_text='' OR $observed_text IN coalesce(properties(existing)['observed_values'],[]) THEN coalesce(properties(existing)['observed_values'],[])
                            ELSE coalesce(properties(existing)['observed_values'],[]) + [$observed_text] END,
                        existing.resolution='manual_observation_correction',
                        existing.max_score=CASE WHEN coalesce(properties(existing)['max_score'],0.0) < 1.0 THEN 1.0 ELSE properties(existing)['max_score'] END,
                        existing.updated_at=datetime()
                )
                """,
                document_id=document_id,
                target_entity_id=target_entity_id,
                observed_text=observed_text,
                observation_extractor=observation_extractor,
            )

        orphaned = False
        if old_entity_id and old_entity_id != target_entity_id:
            rows = self._run(
                """
                MATCH (e:Entity {entity_id:$entity_id})
                WHERE coalesce(properties(e)['identity_status'],'')='provisional'
                OPTIONAL MATCH (d:Document)-[m:MENTIONS]->(e)
                WITH e, count(m) AS mentions
                OPTIONAL MATCH (o:EntityObservation)-[r:RESOLVED_TO]->(e)
                WITH e, mentions, count(r) AS observations
                OPTIONAL MATCH (c:ContactRecord)-[x:DESCRIBES]->(e)
                WITH e, mentions, observations, count(x) AS contacts
                WHERE mentions=0 AND observations=0 AND contacts=0
                SET e.identity_status='orphaned', e.orphan_reason='observation_corrected',
                    e.orphaned_at=datetime(), e.updated_at=datetime()
                RETURN count(e) AS count
                """,
                entity_id=old_entity_id,
            )
            orphaned = bool(rows and int(rows[0].get("count") or 0) > 0)

        return {
            "status": "corrected_observation",
            "observation_id": observation_id,
            "document_id": document_id,
            "observed_text": observed_text,
            "old_entity_id": old_entity_id or None,
            "target_entity_id": target_entity_id,
            "old_entity_orphaned": orphaned,
            "reason": str(reason or "ocr"),
        }

    def delete_entity_preview(self, entity_id: str, *, reason: str = "manual_not_an_entity") -> dict[str, Any]:
        entity = self._entity_curation_summary(entity_id)
        if entity is None:
            raise ValueError(f"Entity nicht gefunden: {entity_id}")
        if str(entity.get("identity_status") or "") == "merged":
            raise ValueError("Gemergte Tombstone-Entity nicht mit delete-entity entfernen")
        if int(entity.get("contact_records") or 0) > 0:
            raise ValueError("delete-entity verweigert CardDAV-seeded Entity mit ContactRecord; erst Quelle/Identität prüfen")
        observations = self.entity_observations(entity_id)
        return {
            "action": "delete_entity",
            "entity": entity,
            "observations": observations,
            "reason": str(reason or "manual_not_an_entity"),
            "will_delete_entity_node": True,
            "will_preserve_observations_as_rejected": True,
            "note": "Ohne --yes wird nichts verändert.",
        }

    def delete_entity(self, entity_id: str, *, reason: str = "manual_not_an_entity") -> dict[str, Any]:
        preview = self.delete_entity_preview(entity_id, reason=reason)
        entity = preview["entity"]
        observations = list(preview.get("observations") or [])

        # Preserve grounded observations as negative human decisions. This makes
        # rebuilds of the same documents idempotent without keeping a bogus
        # Entity as a retrieval fast track.
        self._run(
            """
            MATCH (o:EntityObservation)-[r:RESOLVED_TO]->(e:Entity {entity_id:$entity_id})
            DELETE r
            SET o.status='rejected',
                o.rejection_reason='manual_not_an_entity',
                o.curator_status='manual_not_entity',
                o.curator_reason=$reason,
                o.curator_decided_at=datetime(),
                o.curator_target_entity_id=null,
                o.candidate_entity_ids=[],
                o.updated_at=datetime()
            """,
            entity_id=entity_id,
            reason=str(reason or "manual_not_an_entity"),
        )

        # Relation observations require two valid resolved endpoints. If a
        # curator decides that this node was not an entity, observations using
        # it are invalid derived data and must be rebuilt from the document.
        self._run(
            """
            MATCH (c:RelationObservation)-[:SUBJECT]->(e:Entity {entity_id:$entity_id})
            DETACH DELETE c
            """,
            entity_id=entity_id,
        )
        self._run(
            """
            MATCH (c:RelationObservation)-[:OBJECT]->(e:Entity {entity_id:$entity_id})
            DETACH DELETE c
            """,
            entity_id=entity_id,
        )
        self._run(
            """
            MATCH (e:Entity {entity_id:$entity_id})
            DETACH DELETE e
            """,
            entity_id=entity_id,
        )

        # Shared value nodes are deleted only when no active graph object refers
        # to them anymore; this cannot remove another identity's data.
        for label, rel_type in (
            ("EntityName", "HAS_NAME"),
            ("SearchAlias", "HAS_SEARCH_ALIAS"),
            ("EmailAddress", "HAS_EMAIL"),
            ("PhoneNumber", "HAS_PHONE"),
            ("PostalAddress", "HAS_ADDRESS"),
        ):
            self._run(
                f"""
                MATCH (v:{label})
                WHERE NOT ()-[:{rel_type}]->(v)
                DETACH DELETE v
                """
            )

        return {
            "status": "deleted_non_entity",
            "entity_id": entity_id,
            "display_name": entity.get("display_name"),
            "rejected_observations": len(observations),
            "reason": str(reason or "manual_not_an_entity"),
        }

    def merge_entities_preview(
        self, keep_entity_id: str, merge_entity_id: str, *, alias_policy: str = "contextual"
    ) -> dict[str, Any]:
        """Validate and preview a manual identity merge without changing data."""
        if keep_entity_id == merge_entity_id:
            raise ValueError("keep und merge dürfen nicht dieselbe Entity sein")
        keep = self._entity_curation_summary(keep_entity_id)
        merge = self._entity_curation_summary(merge_entity_id)
        if keep is None:
            raise ValueError(f"KEEP-Entity nicht gefunden: {keep_entity_id}")
        if merge is None:
            raise ValueError(f"MERGE-Entity nicht gefunden: {merge_entity_id}")
        keep_labels = set(keep.get("labels") or [])
        merge_labels = set(merge.get("labels") or [])
        keep_type = "Organization" if "Organization" in keep_labels else "Person" if "Person" in keep_labels else ""
        merge_type = "Organization" if "Organization" in merge_labels else "Person" if "Person" in merge_labels else ""
        if not keep_type or keep_type != merge_type:
            raise ValueError("Nur zwei Person-Entities oder zwei Organization-Entities können zusammengeführt werden")
        if str(keep.get("identity_status") or "") in {"merged", "orphaned"}:
            raise ValueError("KEEP-Entity ist nicht aktiv (merged/orphaned)")
        if str(merge.get("identity_status") or "") in {"merged", "orphaned"}:
            raise ValueError("MERGE-Entity ist nicht aktiv (merged/orphaned)")
        alias_policy = _form_policy(alias_policy, default="contextual")
        if alias_policy == "document_only":
            # A true identity merge can preserve document-only spellings for audit,
            # but they must not become global resolver forms.
            pass
        return {
            "action": "merge",
            "entity_type": keep_type,
            "keep": keep,
            "merge": merge,
            "will_preserve_tombstone": True,
            "inherited_form_policy": alias_policy,
            "note": "Ohne --yes wird nichts verändert.",
        }

    def merge_entities(
        self, keep_entity_id: str, merge_entity_id: str, *, alias_policy: str = "contextual"
    ) -> dict[str, Any]:
        """Manually merge two identities while preserving the retired node.

        The merged node becomes a tombstone. Evidence/provenance is not thrown
        away: document mentions and resolution links are redirected, while old
        source edges remain inspectable on the tombstone where useful.
        """
        preview = self.merge_entities_preview(keep_entity_id, merge_entity_id, alias_policy=alias_policy)
        alias_policy = str(preview["inherited_form_policy"])
        keep_type = str(preview["entity_type"])
        merge_name = str(preview["merge"].get("display_name") or "").strip()

        # Keep third-party review hints before retiring the old node. A curator
        # merge changes identity, not the fact that an unresolved duplicate
        # suspicion existed. These are reattached to the survivor below.
        carried_candidates = self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r:POSSIBLE_SAME_AS]-(x:Entity)
            WHERE x.entity_id <> $keep_id
              AND coalesce(properties(r)['status'],'candidate')='candidate'
              AND coalesce(properties(x)['identity_status'],'') <> 'merged' AND coalesce(properties(x)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT x.entity_id AS entity_id,
                   coalesce(properties(r)['score'],0.0) AS score,
                   coalesce(properties(r)['reason'],'carried_candidate') AS reason,
                   properties(r)['matched_left_form'] AS matched_left_form,
                   properties(r)['matched_right_form'] AS matched_right_form
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )
        carried_equivalents = self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r]-(x:Entity)
            WHERE type(r)='SAME_AS'
              AND x.entity_id <> $keep_id
              AND coalesce(properties(r)['active'],true)=true
              AND coalesce(properties(x)['identity_status'],'') <> 'merged'
              AND coalesce(properties(x)['identity_status'],'') <> 'orphaned'
            RETURN DISTINCT x.entity_id AS entity_id,
                   coalesce(properties(r)['reason'],'carried_from_merge') AS reason
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )


        # Preserve every real name and search alias on the survivor as a
        # manual-merge form. Original source relationships remain on the
        # tombstone for provenance/history.
        self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r:HAS_NAME]->(n:EntityName)
            MATCH (keep:Entity {entity_id:$keep_id})
            MERGE (keep)-[nr:HAS_NAME {source_merge_entity_id:$merge_id, normalized:n.normalized}]->(n)
            SET nr.kind='merged_name', nr.preferred=false, nr.active=true,
                nr.resolution_policy=$alias_policy, nr.merged_at=datetime(), nr.updated_at=datetime()
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
            alias_policy=alias_policy,
        )
        self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r:HAS_SEARCH_ALIAS]->(a:SearchAlias)
            MATCH (keep:Entity {entity_id:$keep_id})
            MERGE (keep)-[nr:HAS_SEARCH_ALIAS {source_merge_entity_id:$merge_id, normalized:a.normalized}]->(a)
            SET nr.kind='merged_alias', nr.weight=CASE WHEN coalesce(properties(r)['weight'],0.5) >= 0.92 THEN coalesce(properties(r)['weight'],0.5) ELSE 0.92 END, nr.active=true,
                nr.resolution_policy=$alias_policy, nr.merged_at=datetime(), nr.updated_at=datetime()
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
            alias_policy=alias_policy,
        )
        if merge_name:
            alias_norm = normalize_name(merge_name)
            self._run(
                """
                MATCH (keep:Entity {entity_id:$keep_id})
                MERGE (a:SearchAlias {normalized:$normalized})
                ON CREATE SET a.value=$value, a.created_at=datetime()
                SET a.last_seen_value=$value, a.updated_at=datetime()
                MERGE (keep)-[r:HAS_SEARCH_ALIAS {source_merge_entity_id:$merge_id, normalized:$normalized}]->(a)
                SET r.kind='merged_display_name', r.weight=0.98, r.active=true,
                    r.resolution_policy=$alias_policy, r.merged_at=datetime(), r.updated_at=datetime()
                """,
                keep_id=keep_entity_id,
                merge_id=merge_entity_id,
                normalized=alias_norm,
                value=merge_name,
                alias_policy=alias_policy,
            )

        # Consolidated identity/contact signals on the survivor. Source-specific
        # relationships on the tombstone remain as audit trail.
        for rel_type, label in (
            ("HAS_EMAIL", "EmailAddress"),
            ("HAS_PHONE", "PhoneNumber"),
            ("HAS_ADDRESS", "PostalAddress"),
        ):
            self._run(
                f"""
                MATCH (old:Entity {{entity_id:$merge_id}})-[r:{rel_type}]->(v:{label})
                MATCH (keep:Entity {{entity_id:$keep_id}})
                MERGE (keep)-[nr:{rel_type} {{source_merge_entity_id:$merge_id}}]->(v)
                SET nr.active=true, nr.merged_at=datetime(), nr.updated_at=datetime()
                """,
                keep_id=keep_entity_id,
                merge_id=merge_entity_id,
            )

        # Re-point CardDAV records. A ContactRecord should describe the surviving
        # identity only; its supplied value nodes remain unchanged.
        self._run(
            """
            MATCH (c:ContactRecord)-[r:DESCRIBES]->(old:Entity {entity_id:$merge_id})
            MATCH (keep:Entity {entity_id:$keep_id})
            MERGE (c)-[nr:DESCRIBES]->(keep)
            SET nr += properties(r), nr.resolved_by='manual_merge', nr.updated_at=datetime()
            DELETE r
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )

        # Re-point grounded observations and make their candidate pointer current.
        self._run(
            """
            MATCH (o:EntityObservation)-[r:RESOLVED_TO]->(old:Entity {entity_id:$merge_id})
            MATCH (keep:Entity {entity_id:$keep_id})
            MERGE (o)-[nr:RESOLVED_TO]->(keep)
            SET nr.resolved_by='manual_merge', nr.merged_from_entity_id=$merge_id,
                nr.updated_at=datetime(),
                o.status=CASE WHEN coalesce(properties(o)['curator_status'],'')='corrected_observation' THEN 'corrected' ELSE 'resolved_existing' END,
                o.candidate_entity_ids=[$keep_id],
                o.curator_target_entity_id=CASE
                    WHEN coalesce(properties(o)['curator_status'],'')='corrected_observation' THEN $keep_id
                    ELSE properties(o)['curator_target_entity_id'] END,
                o.updated_at=datetime()
            DELETE r
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )

        # Consolidate Document->MENTIONS. If both identities were mentioned in
        # the same document, keep one edge and combine counts/observed spellings.
        self._run(
            """
            MATCH (d:Document)-[src:MENTIONS]->(old:Entity {entity_id:$merge_id})
            MATCH (keep:Entity {entity_id:$keep_id})
            OPTIONAL MATCH (d)-[dst:MENTIONS]->(keep)
            WITH d, src, dst, keep
            FOREACH (_ IN CASE WHEN dst IS NULL THEN [1] ELSE [] END |
                CREATE (d)-[nr:MENTIONS]->(keep)
                SET nr = properties(src),
                    nr.resolution='manual_merge',
                    nr.merged_from_entity_id=$merge_id,
                    nr.updated_at=datetime()
            )
            FOREACH (_ IN CASE WHEN dst IS NULL THEN [] ELSE [1] END |
                SET dst.mention_count=coalesce(properties(dst)['mention_count'],0)+coalesce(properties(src)['mention_count'],0),
                    dst.observed_values=reduce(acc=[], x IN coalesce(properties(dst)['observed_values'],[])+coalesce(properties(src)['observed_values'],[]) |
                        CASE WHEN x IN acc THEN acc ELSE acc + [x] END),
                    dst.max_score=CASE
                        WHEN coalesce(properties(dst)['max_score'],0.0) >= coalesce(properties(src)['max_score'],0.0) THEN coalesce(properties(dst)['max_score'],0.0)
                        ELSE coalesce(properties(src)['max_score'],0.0)
                    END,
                    dst.resolution='manual_merge',
                    dst.updated_at=datetime()
            )
            DELETE src
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )

        # RelationObservation endpoints follow a curator-confirmed identity
        # merge. The Claim node remains document-scoped and its evidence text is
        # unchanged; only the resolved identity endpoint is redirected.
        for rel_type in ("SUBJECT", "OBJECT"):
            self._run(
                f"""
                MATCH (c:RelationObservation)-[r:{rel_type}]->(old:Entity {{entity_id:$merge_id}})
                MATCH (keep:Entity {{entity_id:$keep_id}})
                MERGE (c)-[nr:{rel_type}]->(keep)
                SET nr.merged_from_entity_id=$merge_id, nr.updated_at=datetime()
                DELETE r
                """,
                keep_id=keep_entity_id,
                merge_id=merge_entity_id,
            )
        # A relation between two nodes that were just confirmed as the same
        # identity is no longer a meaningful binary relation. Drop it; a later
        # document rebuild may derive a corrected observation if appropriate.
        self._run(
            """
            MATCH (c:RelationObservation)-[:SUBJECT]->(e:Entity)
            MATCH (c)-[:OBJECT]->(e)
            DETACH DELETE c
            """
        )

        # Mention-name candidate links should no longer point at the retired id.
        self._run(
            """
            MATCH (m:MentionName)-[r:POSSIBLE_MATCH]->(old:Entity {entity_id:$merge_id})
            MATCH (keep:Entity {entity_id:$keep_id})
            MERGE (m)-[nr:POSSIBLE_MATCH]->(keep)
            SET nr += properties(r), nr.merged_from_entity_id=$merge_id, nr.updated_at=datetime()
            DELETE r
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )

        # Move the small set of structural identity relations used by the graph.
        for rel_type in IDENTITY_MERGE_RELATIONS:
            self._run(
                f"""
                MATCH (old:Entity {{entity_id:$merge_id}})-[r:{rel_type}]->(x:Entity)
                WHERE x.entity_id <> $keep_id
                MATCH (keep:Entity {{entity_id:$keep_id}})
                MERGE (keep)-[nr:{rel_type} {{source_merge_entity_id:$merge_id}}]->(x)
                SET nr += properties(r), nr.active=coalesce(r.active,true),
                    nr.merged_from_entity_id=$merge_id, nr.updated_at=datetime()
                DELETE r
                """,
                keep_id=keep_entity_id,
                merge_id=merge_entity_id,
            )
            self._run(
                f"""
                MATCH (x:Entity)-[r:{rel_type}]->(old:Entity {{entity_id:$merge_id}})
                WHERE x.entity_id <> $keep_id
                MATCH (keep:Entity {{entity_id:$keep_id}})
                MERGE (x)-[nr:{rel_type} {{source_merge_entity_id:$merge_id}}]->(keep)
                SET nr += properties(r), nr.active=coalesce(r.active,true),
                    nr.merged_from_entity_id=$merge_id, nr.updated_at=datetime()
                DELETE r
                """,
                keep_id=keep_entity_id,
                merge_id=merge_entity_id,
            )

        # Preserve explicit negative identity decisions previously attached to
        # the retired entity, except of course against the chosen survivor.
        not_same = self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r]-(x:Entity)
            WHERE type(r) = 'NOT_SAME_AS' AND x.entity_id <> $keep_id
            RETURN DISTINCT x.entity_id AS entity_id,
                   coalesce(r.reason,'manual_rejection') AS reason
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
        )
        for row in not_same:
            other_id = str(row.get("entity_id") or "")
            if other_id:
                self.reject_merge_candidate(
                    keep_entity_id,
                    other_id,
                    reason=str(row.get("reason") or "carried_from_merged_entity"),
                )

        # Preserve non-destructive identity equivalence when one member is later
        # explicitly consolidated.  A contradictory NOT_SAME_AS on the survivor
        # wins and prevents carrying that equivalence.
        for row in carried_equivalents:
            other_id = str(row.get("entity_id") or "")
            if not other_id or other_id == keep_entity_id:
                continue
            blocked = self._run(
                """
                MATCH (a:Entity {entity_id:$keep_id})
                MATCH (b:Entity {entity_id:$other_id})
                OPTIONAL MATCH (a)-[n]-(b)
                WHERE type(n)='NOT_SAME_AS'
                RETURN n IS NOT NULL AS blocked
                """,
                keep_id=keep_entity_id,
                other_id=other_id,
            )
            if blocked and bool(blocked[0].get("blocked")):
                continue
            a_id, b_id = sorted([keep_entity_id, other_id])
            self._run(
                """
                MATCH (a:Entity {entity_id:$a_id})
                MATCH (b:Entity {entity_id:$b_id})
                MERGE (a)-[r:SAME_AS]->(b)
                ON CREATE SET r.created_at=datetime()
                SET r.active=true,
                    r.reason=$reason,
                    r.decided_by='manual_merge_carry',
                    r.source_merge_entity_id=$merge_id,
                    r.updated_at=datetime()
                """,
                a_id=a_id,
                b_id=b_id,
                reason=str(row.get("reason") or "carried_from_merge"),
                merge_id=merge_entity_id,
            )
        self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r]-()
            WHERE type(r)='SAME_AS'
            DELETE r
            """,
            merge_id=merge_entity_id,
        )

        # Remove stale candidate edges involving the retired entity, record the
        # irreversible semantic decision as a reversible tombstone redirect.
        self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r:POSSIBLE_SAME_AS]-()
            DELETE r
            """,
            merge_id=merge_entity_id,
        )
        self._run(
            """
            MATCH (old:Entity {entity_id:$merge_id})-[r]-()
            WHERE type(r) = 'NOT_SAME_AS'
            DELETE r
            """,
            merge_id=merge_entity_id,
        )
        keep_key = organization_identity_key(str(preview["keep"].get("display_name") or "")) if keep_type == "Organization" else ""
        self._run(
            """
            MATCH (keep:Entity {entity_id:$keep_id})
            MATCH (old:Entity {entity_id:$merge_id})
            MERGE (old)-[r:MERGED_INTO]->(keep)
            SET r.merged_at=datetime(), r.method='manual_curator', r.updated_at=datetime(),
                old.identity_status='merged', old.merged_into_entity_id=$keep_id,
                old.merged_at=datetime(), old.updated_at=datetime(),
                keep.identity_status='confirmed',
                keep.confirmation_method='manual_curator_merge',
                keep.confirmed_at=datetime(),
                keep.identity_key=CASE WHEN $keep_key <> '' THEN $keep_key ELSE keep.identity_key END,
                keep.updated_at=datetime()
            """,
            keep_id=keep_entity_id,
            merge_id=merge_entity_id,
            keep_key=keep_key,
        )
        new_candidates = self.refresh_possible_same_as(keep_entity_id)

        # Reattach unresolved third-party candidates from the retired identity.
        # They are tagged as merge-carried so a later similarity refresh does not
        # silently erase an outstanding curator decision. Explicit NOT_SAME_AS
        # always wins.
        carried_count = 0
        for row in carried_candidates:
            other_id = str(row.get("entity_id") or "")
            if not other_id or other_id == keep_entity_id:
                continue
            blocked = self._run(
                """
                MATCH (a:Entity {entity_id:$keep_id})
                MATCH (b:Entity {entity_id:$other_id})
                OPTIONAL MATCH (a)-[n]-(b)
                WHERE type(n) = 'NOT_SAME_AS'
                RETURN n IS NOT NULL AS blocked
                """,
                keep_id=keep_entity_id,
                other_id=other_id,
            )
            if blocked and bool(blocked[0].get("blocked")):
                continue
            left_id, right_id = sorted([keep_entity_id, other_id])
            self._run(
                """
                MATCH (a:Entity {entity_id:$left_id})
                MATCH (b:Entity {entity_id:$right_id})
                MERGE (a)-[r:POSSIBLE_SAME_AS]->(b)
                ON CREATE SET r.created_at=datetime()
                SET r.score=CASE
                        WHEN coalesce(properties(r)['score'],0.0) >= $score THEN coalesce(properties(r)['score'],0.0)
                        ELSE $score END,
                    r.reason=CASE
                        WHEN coalesce(properties(r)['score'],0.0) >= $score AND properties(r)['reason'] IS NOT NULL THEN properties(r)['reason']
                        ELSE $reason END,
                    r.status='candidate',
                    r.suggested_by=CASE
                        WHEN coalesce(properties(r)['suggested_by'],'')='identity_similarity_v1' THEN properties(r)['suggested_by']
                        ELSE 'manual_merge_carry' END,
                    r.carried_from_merge_entity_id=$merge_id,
                    r.updated_at=datetime()
                """,
                left_id=left_id,
                right_id=right_id,
                score=float(row.get("score") or 0.0),
                reason=str(row.get("reason") or "carried_candidate"),
                merge_id=merge_entity_id,
            )
            carried_count += 1

        return {
            "status": "merged",
            "keep_entity_id": keep_entity_id,
            "merged_entity_id": merge_entity_id,
            "keep_name": preview["keep"].get("display_name"),
            "merged_name": preview["merge"].get("display_name"),
            "inherited_form_policy": alias_policy,
            "new_merge_candidates": new_candidates,
            "carried_merge_candidates": carried_count,
        }

    def confirm_same_as_preview(
        self, left_entity_id: str, right_entity_id: str, *, reason: str = "manual_identity_confirmation"
    ) -> dict[str, Any]:
        """Validate a non-destructive identity-equivalence decision."""
        if left_entity_id == right_entity_id:
            raise ValueError("Eine Entity ist bereits mit sich selbst identisch")
        left = self._entity_curation_summary(left_entity_id)
        right = self._entity_curation_summary(right_entity_id)
        if left is None or right is None:
            raise ValueError("Mindestens eine Entity wurde nicht gefunden")
        for label, entity in (("A", left), ("B", right)):
            if str(entity.get("identity_status") or "") in {"merged", "orphaned"}:
                raise ValueError(f"Entity {label} ist nicht aktiv (merged/orphaned)")
        left_labels = set(left.get("labels") or [])
        right_labels = set(right.get("labels") or [])
        left_type = "Organization" if "Organization" in left_labels else "Person" if "Person" in left_labels else ""
        right_type = "Organization" if "Organization" in right_labels else "Person" if "Person" in right_labels else ""
        if not left_type or left_type != right_type or "OrganizationalUnit" in left_labels or "OrganizationalUnit" in right_labels:
            raise ValueError("SAME_AS ist nur zwischen zwei aktiven Personen oder zwei aktiven Organisationen zulässig")
        return {
            "action": "same_as",
            "entity_type": left_type,
            "left": left,
            "right": right,
            "reason": str(reason or "manual_identity_confirmation"),
            "note": (
                "Beide Entities bleiben aktiv und behalten eigene ContactRecords, Namen und Provenienz. "
                "Es wird kein Survivor gewählt und nichts umgehängt."
            ),
        }

    def confirm_same_as(
        self, left_entity_id: str, right_entity_id: str, *, reason: str = "manual_identity_confirmation"
    ) -> dict[str, Any]:
        """Persist manual identity equivalence without merging either Entity."""
        preview = self.confirm_same_as_preview(left_entity_id, right_entity_id, reason=reason)
        a_id, b_id = sorted([left_entity_id, right_entity_id])
        self._run(
            """
            MATCH (a:Entity {entity_id:$a_id})-[r]-(b:Entity {entity_id:$b_id})
            WHERE type(r) IN ['POSSIBLE_SAME_AS','NOT_SAME_AS']
            DELETE r
            """,
            a_id=a_id,
            b_id=b_id,
        )
        self._run(
            """
            MATCH (a:Entity {entity_id:$a_id})
            MATCH (b:Entity {entity_id:$b_id})
            MERGE (a)-[r:SAME_AS]->(b)
            ON CREATE SET r.created_at=datetime()
            SET r.active=true,
                r.reason=$reason,
                r.decided_by='manual_curator',
                r.decided_at=datetime(),
                r.updated_at=datetime()
            """,
            a_id=a_id,
            b_id=b_id,
            reason=str(reason or "manual_identity_confirmation"),
        )
        # Rebuild only machine suggestions around the pair. SAME_AS itself is
        # a blocking curator decision and is never replaced by this refresh.
        self.refresh_possible_same_as(left_entity_id)
        self.refresh_possible_same_as(right_entity_id)
        return {
            "status": "same_as",
            "left_entity_id": left_entity_id,
            "left_name": preview["left"].get("display_name"),
            "right_entity_id": right_entity_id,
            "right_name": preview["right"].get("display_name"),
            "reason": str(reason or "manual_identity_confirmation"),
        }

    def reject_merge_candidate(self, left_entity_id: str, right_entity_id: str, *, reason: str = "manual_rejection") -> dict[str, Any]:
        """Persist a negative identity decision so similarity cannot re-suggest it."""
        if left_entity_id == right_entity_id:
            raise ValueError("Eine Entity kann nicht als verschieden von sich selbst markiert werden")
        left = self._entity_curation_summary(left_entity_id)
        right = self._entity_curation_summary(right_entity_id)
        if left is None or right is None:
            raise ValueError("Mindestens eine Entity wurde nicht gefunden")
        a_id, b_id = sorted([left_entity_id, right_entity_id])
        self._run(
            """
            MATCH (a:Entity {entity_id:$a_id})-[c]-(b:Entity {entity_id:$b_id})
            WHERE type(c) IN ['POSSIBLE_SAME_AS','SAME_AS']
            DELETE c
            """,
            a_id=a_id,
            b_id=b_id,
        )
        self._run(
            """
            MATCH (a:Entity {entity_id:$a_id})
            MATCH (b:Entity {entity_id:$b_id})
            MERGE (a)-[r:NOT_SAME_AS]->(b)
            SET r.reason=$reason, r.decided_by='manual_curator',
                r.decided_at=datetime(), r.updated_at=datetime()
            """,
            a_id=a_id,
            b_id=b_id,
            reason=str(reason or "manual_rejection"),
        )
        return {
            "status": "not_same_as",
            "left_entity_id": left_entity_id,
            "left_name": left.get("display_name"),
            "right_entity_id": right_entity_id,
            "right_name": right.get("display_name"),
            "reason": str(reason or "manual_rejection"),
        }

    def list_merge_candidates(self, *, source_user_id: str = "") -> list[dict[str, Any]]:
        """List open identity candidates grouped by active SAME_AS components.

        SAME_AS is an equivalence relation even though we persist only the
        curator-confirmed edges.  The review queue therefore collapses all raw
        POSSIBLE_SAME_AS edges between the same two equivalence components into
        one actionable row.  ContactRecord provenance is attached for
        administration and optional per-user filtering.
        """
        raw = self._run(
            """
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE type(r)='POSSIBLE_SAME_AS'
              AND coalesce(properties(r)['status'],'candidate')='candidate'
              AND coalesce(properties(a)['identity_status'],'') <> 'merged' AND coalesce(properties(a)['identity_status'],'') <> 'orphaned'
              AND coalesce(properties(b)['identity_status'],'') <> 'merged' AND coalesce(properties(b)['identity_status'],'') <> 'orphaned'
            RETURN a.entity_id AS left_entity_id,
                   a.display_name AS left_name,
                   labels(a) AS left_labels,
                   b.entity_id AS right_entity_id,
                   b.display_name AS right_name,
                   labels(b) AS right_labels,
                   properties(r)['score'] AS score,
                   properties(r)['reason'] AS reason,
                   properties(r)['status'] AS status,
                   properties(r)['suggested_by'] AS suggested_by,
                   properties(r)['matched_left_form'] AS matched_left_form,
                   properties(r)['matched_right_form'] AS matched_right_form,
                   properties(r)['carried_from_merge_entity_id'] AS carried_from_merge_entity_id
            """
        )
        if not raw:
            return []

        identity_decisions = self._run(
            """
            MATCH (a:Entity)-[r]-(b:Entity)
            WHERE type(r) IN ['SAME_AS','NOT_SAME_AS']
              AND (type(r) <> 'SAME_AS' OR coalesce(properties(r)['active'],true)=true)
              AND coalesce(properties(a)['identity_status'],'') <> 'merged'
              AND coalesce(properties(a)['identity_status'],'') <> 'orphaned'
              AND coalesce(properties(b)['identity_status'],'') <> 'merged'
              AND coalesce(properties(b)['identity_status'],'') <> 'orphaned'
            RETURN a.entity_id AS left_entity_id, b.entity_id AS right_entity_id,
                   type(r) AS relation_type
            """
        )

        parent: dict[str, str] = {}

        def find(entity_id: str) -> str:
            parent.setdefault(entity_id, entity_id)
            while parent[entity_id] != entity_id:
                parent[entity_id] = parent[parent[entity_id]]
                entity_id = parent[entity_id]
            return entity_id

        def union(left_id: str, right_id: str) -> None:
            left_root = find(left_id)
            right_root = find(right_id)
            if left_root == right_root:
                return
            keep, merge = sorted((left_root, right_root))
            parent[merge] = keep

        for row in raw:
            find(str(row.get("left_entity_id") or ""))
            find(str(row.get("right_entity_id") or ""))
        for row in identity_decisions:
            if str(row.get("relation_type") or "") != "SAME_AS":
                continue
            left_id = str(row.get("left_entity_id") or "")
            right_id = str(row.get("right_entity_id") or "")
            if left_id and right_id:
                union(left_id, right_id)

        blocked_component_pairs: set[tuple[str, str]] = set()
        for row in identity_decisions:
            if str(row.get("relation_type") or "") != "NOT_SAME_AS":
                continue
            left_id = str(row.get("left_entity_id") or "")
            right_id = str(row.get("right_entity_id") or "")
            if left_id and right_id:
                left_root, right_root = find(left_id), find(right_id)
                if left_root != right_root:
                    blocked_component_pairs.add(tuple(sorted((left_root, right_root))))

        entity_ids = sorted(entity_id for entity_id in parent if entity_id)
        metadata_rows = self._run(
            """
            MATCH (e:Entity)
            WHERE e.entity_id IN $entity_ids
            OPTIONAL MATCH (c:ContactRecord)-[cr]->(e)
            WHERE type(cr)='DESCRIBES'
            WITH e, [x IN collect(CASE WHEN c IS NULL THEN null ELSE {
                contact_id:c.contact_id,
                cloud_id:coalesce(c.cloud_id,''),
                source_user_id:coalesce(c.source_user_id,''),
                addressbook_name:coalesce(c.addressbook_name,''),
                addressbook_slug:coalesce(c.addressbook_slug,'')
            } END) WHERE x IS NOT NULL] AS sources
            RETURN e.entity_id AS entity_id,
                   e.display_name AS display_name,
                   labels(e) AS labels,
                   sources
            """,
            entity_ids=entity_ids,
        )
        metadata = {
            str(row.get("entity_id") or ""): dict(row)
            for row in metadata_rows
            if str(row.get("entity_id") or "")
        }

        components: dict[str, list[str]] = {}
        for entity_id in entity_ids:
            components.setdefault(find(entity_id), []).append(entity_id)

        def component_summary(root: str) -> dict[str, Any]:
            ids = sorted(components.get(root) or [root])
            names: list[str] = []
            sources: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            seen_sources: set[tuple[str, str, str, str]] = set()
            for entity_id in ids:
                meta = metadata.get(entity_id) or {}
                name = str(meta.get("display_name") or "").strip()
                if name and name.casefold() not in seen_names:
                    seen_names.add(name.casefold())
                    names.append(name)
                for source in meta.get("sources") or []:
                    source = dict(source or {})
                    key = (
                        str(source.get("source_user_id") or ""),
                        str(source.get("addressbook_name") or ""),
                        str(source.get("cloud_id") or ""),
                        str(source.get("contact_id") or ""),
                    )
                    if key in seen_sources:
                        continue
                    seen_sources.add(key)
                    sources.append(source)
            sources.sort(key=lambda item: (
                str(item.get("source_user_id") or "").casefold(),
                str(item.get("addressbook_name") or "").casefold(),
                str(item.get("contact_id") or ""),
            ))
            return {
                "component_ids": ids,
                "component_names": names,
                "sources": sources,
                "source_users": sorted({
                    str(item.get("source_user_id") or "")
                    for item in sources
                    if str(item.get("source_user_id") or "")
                }, key=str.casefold),
            }

        component_cache = {
            root: component_summary(root)
            for root in components
        }
        selected_user = str(source_user_id or "").strip().casefold()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}

        for raw_row in raw:
            row = dict(raw_row)
            raw_left_id = str(row.get("left_entity_id") or "")
            raw_right_id = str(row.get("right_entity_id") or "")
            if not raw_left_id or not raw_right_id:
                continue
            raw_left_root = find(raw_left_id)
            raw_right_root = find(raw_right_id)
            if raw_left_root == raw_right_root:
                continue

            key = tuple(sorted((raw_left_root, raw_right_root)))
            if key in blocked_component_pairs:
                continue
            swapped = raw_left_root != key[0]
            if swapped:
                row["left_entity_id"], row["right_entity_id"] = raw_right_id, raw_left_id
                row["left_name"], row["right_name"] = row.get("right_name"), row.get("left_name")
                row["left_labels"], row["right_labels"] = row.get("right_labels"), row.get("left_labels")
                row["matched_left_form"], row["matched_right_form"] = (
                    row.get("matched_right_form"), row.get("matched_left_form")
                )

            left_summary = component_cache.get(key[0]) or component_summary(key[0])
            right_summary = component_cache.get(key[1]) or component_summary(key[1])
            visible_users = {
                str(value).casefold()
                for value in (left_summary["source_users"] + right_summary["source_users"])
                if str(value).strip()
            }
            if selected_user and selected_user not in visible_users:
                continue

            row.update({
                "left_component_ids": left_summary["component_ids"],
                "left_component_names": left_summary["component_names"],
                "left_sources": left_summary["sources"],
                "right_component_ids": right_summary["component_ids"],
                "right_component_names": right_summary["component_names"],
                "right_sources": right_summary["sources"],
            })
            previous = grouped.get(key)
            if previous is None or float(row.get("score") or 0.0) > float(previous.get("score") or 0.0):
                grouped[key] = row

        return sorted(
            grouped.values(),
            key=lambda row: (
                -float(row.get("score") or 0.0),
                str(row.get("left_name") or "").casefold(),
                str(row.get("right_name") or "").casefold(),
            ),
        )

    def stats(self) -> dict[str, int]:
        rows = self._run(
            """
            MATCH (n)
            RETURN count(n) AS nodes
            """
        )
        rels = self._run("MATCH ()-[r]->() RETURN count(r) AS relationships")
        persons = self._run("MATCH (n:Person) RETURN count(n) AS count")
        orgs = self._run("MATCH (n:Organization) RETURN count(n) AS count")
        units = self._run("MATCH (n:OrganizationalUnit) RETURN count(n) AS count")
        contacts = self._run("MATCH (n:ContactRecord) RETURN count(n) AS count")
        contact_import_runs = self._run("MATCH (n:ContactImportRun) RETURN count(n) AS count")
        documents = self._run("MATCH (n:Document) RETURN count(n) AS count")
        mention_names = self._run("MATCH (n:MentionName) RETURN count(n) AS count")
        document_mentions = self._run("MATCH (:Document)-[r]->(:Entity) WHERE type(r)='MENTIONS' RETURN count(r) AS count")
        document_name_mentions = self._run("MATCH (:Document)-[r]->(:MentionName) WHERE type(r)='MENTIONS_NAME' RETURN count(r) AS count")
        provisional_entities = self._run("MATCH (e:Entity) WHERE coalesce(e.identity_status,'')='provisional' RETURN count(e) AS count")
        document_origin_entities = self._run("MATCH (e:Entity) WHERE coalesce(e.origin,'') CONTAINS 'document' RETURN count(e) AS count")
        entity_observations = self._run("MATCH (o:EntityObservation) RETURN count(o) AS count")
        relation_observations = self._run("MATCH (o:RelationObservation) RETURN count(o) AS count")
        research_findings = self._run("MATCH (f:ResearchFinding) RETURN count(f) AS count")
        mail_messages = self._run("MATCH (m:MailMessage) RETURN count(m) AS count")
        mail_replies = self._run("MATCH (:MailMessage)-[r]->(:MailMessage) WHERE type(r)='REPLIES_TO' RETURN count(r) AS count")
        mail_representations = self._run("MATCH (:Document)-[r]->(:MailMessage) WHERE type(r)='REPRESENTS_MAIL' RETURN count(r) AS count")
        mail_attachments = self._run("MATCH (:Document)-[r]->(:MailMessage) WHERE type(r)='ATTACHMENT_OF' RETURN count(r) AS count")
        rejected_observations = self._run("MATCH (o:EntityObservation {status:'rejected'}) RETURN count(o) AS count")
        unresolved_observations = self._run("MATCH (o:EntityObservation {status:'unresolved'}) RETURN count(o) AS count")
        merge_candidates = len(self.list_merge_candidates())
        merged_entities = self._run("MATCH (e:Entity {identity_status:'merged'}) RETURN count(e) AS count")
        confirmed_entities = self._run("MATCH (e:Entity {identity_status:'confirmed'}) RETURN count(e) AS count")
        orphaned_entities = self._run("MATCH (e:Entity {identity_status:'orphaned'}) RETURN count(e) AS count")
        manual_non_entity_observations = self._run("MATCH (o:EntityObservation {curator_status:'manual_not_entity'}) RETURN count(o) AS count")
        corrected_observations = self._run("MATCH (o:EntityObservation {curator_status:'corrected_observation'}) RETURN count(o) AS count")
        not_same_as = self._run("MATCH ()-[r]->() WHERE type(r) = 'NOT_SAME_AS' RETURN count(r) AS count")
        same_as = self._run("MATCH ()-[r]->() WHERE type(r) = 'SAME_AS' RETURN count(r) AS count")
        return {
            "nodes": int(rows[0]["nodes"] if rows else 0),
            "relationships": int(rels[0]["relationships"] if rels else 0),
            "persons": int(persons[0]["count"] if persons else 0),
            "organizations": int(orgs[0]["count"] if orgs else 0),
            "organizational_units": int(units[0]["count"] if units else 0),
            "contact_records": int(contacts[0]["count"] if contacts else 0),
            "contact_import_runs": int(contact_import_runs[0]["count"] if contact_import_runs else 0),
            "documents": int(documents[0]["count"] if documents else 0),
            "mention_names": int(mention_names[0]["count"] if mention_names else 0),
            "document_mentions": int(document_mentions[0]["count"] if document_mentions else 0),
            "document_name_mentions": int(document_name_mentions[0]["count"] if document_name_mentions else 0),
            "provisional_entities": int(provisional_entities[0]["count"] if provisional_entities else 0),
            "document_origin_entities": int(document_origin_entities[0]["count"] if document_origin_entities else 0),
            "entity_observations": int(entity_observations[0]["count"] if entity_observations else 0),
            "relation_observations": int(relation_observations[0]["count"] if relation_observations else 0),
            "research_findings": int(research_findings[0]["count"] if research_findings else 0),
            "mail_messages": int(mail_messages[0]["count"] if mail_messages else 0),
            "mail_reply_relations": int(mail_replies[0]["count"] if mail_replies else 0),
            "mail_representations": int(mail_representations[0]["count"] if mail_representations else 0),
            "mail_attachments": int(mail_attachments[0]["count"] if mail_attachments else 0),
            "rejected_entity_observations": int(rejected_observations[0]["count"] if rejected_observations else 0),
            "unresolved_entity_observations": int(unresolved_observations[0]["count"] if unresolved_observations else 0),
            "merge_candidates": int(merge_candidates),
            "merged_entities": int(merged_entities[0]["count"] if merged_entities else 0),
            "confirmed_entities": int(confirmed_entities[0]["count"] if confirmed_entities else 0),
            "orphaned_entities": int(orphaned_entities[0]["count"] if orphaned_entities else 0),
            "manual_non_entity_observations": int(manual_non_entity_observations[0]["count"] if manual_non_entity_observations else 0),
            "corrected_observations": int(corrected_observations[0]["count"] if corrected_observations else 0),
            "not_same_as_decisions": int(not_same_as[0]["count"] if not_same_as else 0),
            "same_as_decisions": int(same_as[0]["count"] if same_as else 0),
        }


def _queue_contact_relink(
    cfg: dict[str, Any],
    document_ids: list[str],
    *,
    reason: str,
    priority: str = "high",
) -> dict[str, Any]:
    """Queue deterministic re-link jobs: Elasticsearch text, no entity-discovery LLM."""
    ids = [str(x) for x in document_ids if str(x or "").strip()]
    if not ids:
        return {"queued": 0, "coalesced": 0, "document_count": 0}
    from rag.graph_queue import GraphQueue

    queue = GraphQueue(cfg)
    result = queue.enqueue_evidence({
        "query_id": reason,
        "user_query": "",
        "retrieval_query": "",
        "evidence_action": reason,
        "queue_priority": priority,
        "entity_discovery": False,
        "force_reindex": True,
        "count_evidence": False,
        "documents": [{"document_id": document_id} for document_id in ids],
    })
    return {"document_count": len(ids), **result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Neo4j identity graph administration")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Verify Neo4j connectivity")
    sub.add_parser("init", help="Create constraints and indexes")
    sub.add_parser("stats", help="Show graph statistics")
    sub.add_parser("candidates", help="List review-only POSSIBLE_SAME_AS merge candidates")
    find_parser = sub.add_parser("find", help="Find Entities by display name, names and aliases; includes merged tombstones")
    find_parser.add_argument("query")
    find_parser.add_argument("--limit", type=int, default=50)
    merges_parser = sub.add_parser("merges", help="List historical MERGED_INTO decisions")
    merges_parser.add_argument("query", nargs="?", default="")
    merges_parser.add_argument("--limit", type=int, default=100)
    entity_parser = sub.add_parser("entity", help="Show one Entity with forms, observations and merge history")
    entity_parser.add_argument("--entity", required=True, dest="entity_id")
    refresh_parser = sub.add_parser("refresh-candidates", help="Recompute similarity candidates from all active names/aliases; no LLM")
    refresh_parser.add_argument("--max-candidates", type=int, default=5, dest="max_candidates")
    sub.add_parser("identity-backfill", help="Backfill strict organization identity keys; never merges entities")
    sub.add_parser("curation-backfill", help="Mark survivors of earlier manual merges as confirmed; no LLM")
    sub.add_parser("form-policy-backfill", help="Assign conservative resolution_policy to legacy names/aliases; no LLM")
    merge_parser = sub.add_parser("merge", help="Manually merge two identities; preview unless --yes is supplied")
    merge_parser.add_argument("--keep", required=True, dest="keep_entity_id", help="Entity ID that survives")
    merge_parser.add_argument("--merge", required=True, dest="merge_entity_id", help="Entity ID retired into --keep")
    merge_parser.add_argument("--alias-policy", choices=sorted(FORM_RESOLUTION_POLICIES), default="contextual",
                              help="Policy inherited by spellings from the retired Entity; default contextual")
    merge_parser.add_argument("--yes", action="store_true", help="Actually perform the merge")
    reject_parser = sub.add_parser("reject-merge", help="Persist NOT_SAME_AS so a candidate is not suggested again")
    reject_parser.add_argument("--left", required=True, dest="left_entity_id")
    reject_parser.add_argument("--right", required=True, dest="right_entity_id")
    reject_parser.add_argument("--reason", default="manual_rejection")
    reject_parser.add_argument("--yes", action="store_true", help="Actually persist the negative identity decision")
    obs_parser = sub.add_parser("observations", help="List grounded observations for one Entity")
    obs_parser.add_argument("--entity", required=True, dest="entity_id")
    cname_parser = sub.add_parser("correct-name", help="Correct the canonical display name; preview unless --yes")
    cname_parser.add_argument("--entity", required=True, dest="entity_id")
    cname_parser.add_argument("--name", required=True, dest="new_name")
    cname_parser.add_argument("--reason", default="manual_name_correction")
    cname_parser.add_argument("--yes", action="store_true")
    cobs_parser = sub.add_parser("correct-observation", help="Redirect one document/OCR observation to the correct Entity")
    cobs_parser.add_argument("--observation", required=True, dest="observation_id")
    cobs_parser.add_argument("--entity", required=True, dest="target_entity_id")
    cobs_parser.add_argument("--reason", default="ocr")
    cobs_parser.add_argument("--yes", action="store_true")
    del_parser = sub.add_parser("delete-entity", help="Delete an obvious non-entity; preserve observations as rejected")
    del_parser.add_argument("--entity", required=True, dest="entity_id")
    del_parser.add_argument("--reason", default="manual_not_an_entity")
    del_parser.add_argument("--yes", action="store_true")
    forms_parser = sub.add_parser("forms", help="List names/aliases and their resolution_policy for one Entity")
    forms_parser.add_argument("--entity", required=True, dest="entity_id")
    policy_parser = sub.add_parser("set-form-policy", help="Set exclusive/contextual/search_only/document_only for one Entity form")
    policy_parser.add_argument("--entity", required=True, dest="entity_id")
    policy_parser.add_argument("--form", required=True)
    policy_parser.add_argument("--policy", required=True, choices=sorted(FORM_RESOLUTION_POLICIES))
    policy_parser.add_argument("--yes", action="store_true")
    alias_parser = sub.add_parser("add-alias", help="Add a manually curated global alias with explicit policy")
    alias_parser.add_argument("--entity", required=True, dest="entity_id")
    alias_parser.add_argument("--alias", required=True)
    alias_parser.add_argument("--policy", choices=["exclusive", "contextual", "search_only"], default="contextual")
    alias_parser.add_argument("--weight", type=float, default=0.95)
    alias_parser.add_argument("--yes", action="store_true")
    remove_alias_parser = sub.add_parser("remove-alias", help="Remove one SearchAlias from exactly one Entity; preview unless --yes")
    remove_alias_parser.add_argument("--entity", required=True, dest="entity_id")
    remove_alias_parser.add_argument("--alias", required=True)
    remove_alias_parser.add_argument("--yes", action="store_true")
    blocked_parser = sub.add_parser("blocked-entities", help="List active Entities whose display name exactly matches the configured generic denylist")
    sub.add_parser("contact-sources", help="List CardDAV provenance grouped by cloud/user/address book")
    cir_parser = sub.add_parser("contact-import-runs", help="List CardDAV import runs")
    cir_parser.add_argument("--limit", type=int, default=100)
    contacts_parser = sub.add_parser("contacts", help="List ContactRecords by provenance scope")
    contacts_parser.add_argument("--cloud", default="", dest="cloud_id")
    contacts_parser.add_argument("--source-user", default="", dest="source_user_id")
    contacts_parser.add_argument("--addressbook", default="")
    contacts_parser.add_argument("--import-run", default="", dest="import_run_id")
    contacts_parser.add_argument("--limit", type=int, default=500)
    cpb_parser = sub.add_parser("contact-provenance-backfill", help="Backfill cloud/user/addressbook_slug on legacy ContactRecords; no identity changes")
    cpb_parser.add_argument("--cloud", default="", dest="cloud_id")
    cpb_parser.add_argument("--source-user", default="", dest="source_user_id")
    rollback_parser = sub.add_parser("rollback-contacts", help="Remove a CardDAV source/import scope; preview unless --yes")
    rollback_parser.add_argument("--cloud", default="", dest="cloud_id")
    rollback_parser.add_argument("--source-user", default="", dest="source_user_id")
    rollback_parser.add_argument("--addressbook", default="")
    rollback_parser.add_argument("--import-run", default="", dest="import_run_id")
    rollback_parser.add_argument("--priority", choices=["high", "normal", "background"], default="high")
    rollback_parser.add_argument("--no-relink", action="store_true", help="Do not queue deterministic no-LLM document relinking")
    rollback_parser.add_argument("--yes", action="store_true")
    reassign_parser = sub.add_parser("reassign-contact", help="Move one ContactRecord to another same-type Entity; preview unless --yes")
    reassign_parser.add_argument("--contact", required=True, dest="contact_id")
    reassign_parser.add_argument("--entity", required=True, dest="target_entity_id")
    reassign_parser.add_argument("--priority", choices=["high", "normal", "background"], default="high")
    reassign_parser.add_argument("--no-relink", action="store_true")
    reassign_parser.add_argument("--yes", action="store_true")
    reset_parser = sub.add_parser("reset", help="DELETE ALL graph data; explicit confirmation required")
    reset_parser.add_argument("--yes-really-delete-all", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    with GraphStore.from_config(cfg) as graph:
        if args.command == "check":
            graph.verify_connectivity()
            print("Neo4j: Verbindung OK")
        elif args.command == "init":
            graph.verify_connectivity()
            graph.ensure_schema()
            print("Neo4j: Schema/Constraints OK")
        elif args.command == "stats":
            print(json.dumps(graph.stats(), ensure_ascii=False, indent=2))
        elif args.command == "candidates":
            print(json.dumps(graph.list_merge_candidates(), ensure_ascii=False, indent=2))
        elif args.command == "find":
            print(json.dumps(graph.find_entities(args.query, limit=args.limit), ensure_ascii=False, indent=2))
        elif args.command == "merges":
            print(json.dumps(graph.list_merges(args.query, limit=args.limit), ensure_ascii=False, indent=2))
        elif args.command == "entity":
            print(json.dumps(graph.entity_detail(args.entity_id), ensure_ascii=False, indent=2))
        elif args.command == "contact-sources":
            print(json.dumps(graph.list_contact_sources(), ensure_ascii=False, indent=2))
        elif args.command == "contact-import-runs":
            print(json.dumps(graph.list_contact_import_runs(limit=args.limit), ensure_ascii=False, indent=2))
        elif args.command == "contacts":
            print(json.dumps(graph.contact_records(
                cloud_id=args.cloud_id, source_user_id=args.source_user_id,
                addressbook=args.addressbook, import_run_id=args.import_run_id, limit=args.limit
            ), ensure_ascii=False, indent=2))
        elif args.command == "contact-provenance-backfill":
            default_cloud = str(args.cloud_id or cfg_get(cfg, "carddav.cloud_id", "nextcloud.cloud_id", default="") or "")
            if not default_cloud:
                base = str(cfg_get(cfg, "nextcloud.base_url", default="") or "").strip()
                parsed = urlparse(base)
                if parsed.scheme and parsed.netloc:
                    default_cloud = f"{parsed.scheme}://{parsed.netloc}"
            default_user = str(args.source_user_id or cfg_get(cfg, "carddav.user_id", "carddav.username", default="") or "")
            print(json.dumps({"status":"ok", **graph.backfill_contact_provenance(
                default_cloud_id=default_cloud, default_source_user_id=default_user
            )}, ensure_ascii=False, indent=2))
        elif args.command == "rollback-contacts":
            preview = graph.rollback_contacts_preview(
                cloud_id=args.cloud_id, source_user_id=args.source_user_id,
                addressbook=args.addressbook, import_run_id=args.import_run_id
            )
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                result = graph.rollback_contacts(
                    cloud_id=args.cloud_id, source_user_id=args.source_user_id,
                    addressbook=args.addressbook, import_run_id=args.import_run_id
                )
                if not args.no_relink:
                    result["relink_queue"] = _queue_contact_relink(
                        cfg, list(result.get("relink_document_ids") or []),
                        reason="contact_rollback_relink", priority=args.priority
                    )
                print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "reassign-contact":
            preview = graph.reassign_contact_preview(args.contact_id, args.target_entity_id)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                result = graph.reassign_contact(args.contact_id, args.target_entity_id)
                if not args.no_relink:
                    result["relink_queue"] = _queue_contact_relink(
                        cfg, list(result.get("relink_document_ids") or []),
                        reason="contact_reassignment_relink", priority=args.priority
                    )
                print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "refresh-candidates":
            print(json.dumps(
                graph.refresh_all_possible_same_as(max_candidates=max(1, int(args.max_candidates))),
                ensure_ascii=False, indent=2
            ))
        elif args.command == "identity-backfill":
            count = graph.backfill_identity_keys()
            print(json.dumps({"status": "ok", "organizations_updated": count}, ensure_ascii=False, indent=2))
        elif args.command == "curation-backfill":
            count = graph.backfill_manual_confirmations()
            print(json.dumps({"status": "ok", "manual_merge_survivors_confirmed": count}, ensure_ascii=False, indent=2))
        elif args.command == "form-policy-backfill":
            print(json.dumps({"status": "ok", **graph.backfill_form_policies()}, ensure_ascii=False, indent=2))
        elif args.command == "merge":
            preview = graph.merge_entities_preview(args.keep_entity_id, args.merge_entity_id, alias_policy=args.alias_policy)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(
                    graph.merge_entities(args.keep_entity_id, args.merge_entity_id, alias_policy=args.alias_policy),
                    ensure_ascii=False, indent=2
                ))
        elif args.command == "reject-merge":
            left = graph._entity_curation_summary(args.left_entity_id)
            right = graph._entity_curation_summary(args.right_entity_id)
            if left is None or right is None:
                raise SystemExit("Mindestens eine Entity wurde nicht gefunden")
            if not args.yes:
                print(json.dumps({
                    "action": "reject_merge",
                    "left": left,
                    "right": right,
                    "reason": args.reason,
                    "note": "Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.",
                }, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(
                    graph.reject_merge_candidate(args.left_entity_id, args.right_entity_id, reason=args.reason),
                    ensure_ascii=False, indent=2
                ))
        elif args.command == "observations":
            print(json.dumps(graph.entity_observations(args.entity_id), ensure_ascii=False, indent=2))
        elif args.command == "correct-name":
            preview = graph.correct_name_preview(args.entity_id, args.new_name, reason=args.reason)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.correct_name(args.entity_id, args.new_name, reason=args.reason), ensure_ascii=False, indent=2))
        elif args.command == "correct-observation":
            preview = graph.correct_observation_preview(args.observation_id, args.target_entity_id, reason=args.reason)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.correct_observation(args.observation_id, args.target_entity_id, reason=args.reason), ensure_ascii=False, indent=2))
        elif args.command == "forms":
            print(json.dumps(graph.entity_forms(args.entity_id), ensure_ascii=False, indent=2))
        elif args.command == "set-form-policy":
            preview = graph.set_form_policy_preview(args.entity_id, args.form, args.policy)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.set_form_policy(args.entity_id, args.form, args.policy), ensure_ascii=False, indent=2))
        elif args.command == "add-alias":
            preview = graph.add_alias_preview(args.entity_id, args.alias, policy=args.policy, weight=args.weight)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.add_alias(args.entity_id, args.alias, policy=args.policy, weight=args.weight), ensure_ascii=False, indent=2))
        elif args.command == "remove-alias":
            preview = graph.remove_alias_preview(args.entity_id, args.alias)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.remove_alias(args.entity_id, args.alias), ensure_ascii=False, indent=2))
        elif args.command == "blocked-entities":
            blocked = sorted(blocked_generic_names(cfg))
            rows = graph._run(
                """
                MATCH (e:Entity)
                WHERE (e:Person OR e:Organization)
                  AND coalesce(properties(e)['identity_status'],'') <> 'merged' AND coalesce(properties(e)['identity_status'],'') <> 'orphaned'
                RETURN properties(e)['entity_id'] AS entity_id, properties(e)['display_name'] AS display_name, labels(e) AS labels
                ORDER BY properties(e)['display_name']
                """
            )
            matches = [row for row in rows if normalize_name(str(row.get("display_name") or "")) in set(blocked)]
            print(json.dumps({"blocked_names": blocked, "entities": matches}, ensure_ascii=False, indent=2))
        elif args.command == "delete-entity":
            preview = graph.delete_entity_preview(args.entity_id, reason=args.reason)
            if not args.yes:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                print("Keine Änderung. Zum Ausführen denselben Befehl mit --yes wiederholen.")
            else:
                print(json.dumps(graph.delete_entity(args.entity_id, reason=args.reason), ensure_ascii=False, indent=2))
        elif args.command == "reset":
            if not args.yes_really_delete_all:
                raise SystemExit(
                    "Abbruch: reset löscht ALLE Neo4j-Daten. Wiederhole mit "
                    "--yes-really-delete-all, wenn das wirklich beabsichtigt ist."
                )
            graph.verify_connectivity()
            graph._run("MATCH (n) DETACH DELETE n")
            graph.ensure_schema()
            print("Neo4j: alle Daten gelöscht; leeres Schema neu angelegt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
