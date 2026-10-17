# Neo4j application schema

This is the canonical internal reference for the Neo4j schema supported by SunaQ since `0.8.5-rc5`. It describes the application contract derived from the Cypher access used by the current SunaQ codebase. It is not a `SHOW SCHEMA` dump: Neo4j exposes constraints and indexes there, but not the optional property contract, secondary labels, provenance rules or compatibility fields.

## Contract rules

- A **required key** is an identifier used in `MATCH`/`MERGE` and protected by a uniqueness constraint where the label has an independent lifecycle.
- All other properties are **optional** unless a write path explicitly needs them for the operation being performed. Historic nodes and relationships may legitimately omit optional fields.
- Optional properties must be read as `properties(value)['field']` (usually with `coalesce`) so an empty or partial database does not emit `UnknownPropertyKeyWarning`.
- SunaQ creates constraints and indexes with `IF NOT EXISTS`. Repeating initialization is supported.
- Secondary labels such as `Person`, `Organization`, `OrganizationalUnit`, `Claim` and `AKIResearchFinding` refine a primary object; they are not separate identity stores.
- `AKIResearchFinding` is a retained compatibility label from the pre-SunaQ
  naming and is therefore intentionally not renamed in this schema reference.
- No synthetic nodes or dummy properties are created to register property tokens.

## Node labels and properties

“W” means SunaQ writes the field. “R” means SunaQ reads it. Unless marked required, listed fields are optional and may be absent on legacy data.

| Label | Required key / role | Optional properties used by SunaQ | Access |
|---|---|---|---|
| `Entity` | `entity_id`; shared identity node | `display_name`, `identity_key`, `entity_kind`, `identity_status`, `origin`, `display_name_source_contact_id`, `merged_into_entity_id`, `confirmation_method`, `confirmed_at`, `created_at`, `updated_at`, `merged_at`, `orphaned_at`, `orphan_reason`/`orphaned_reason`, `last_document_seen_at`, `source_finding_id` | R/W |
| `Person` | Secondary label on `Entity` | Uses the `Entity` property contract | R/W label |
| `Organization` | Secondary label on `Entity` | Uses the `Entity` property contract | R/W label |
| `OrganizationalUnit` | Secondary label on `Entity`; `unit_key` is unique | `organization_entity_id`, `level`, `short_name`, `qualified_name` | R/W |
| `EntityName` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `SearchAlias` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `EntityFormDecision` | `normalized` | `value`, `status`, `target_entity_id`, `decision_kind`, `reason`, `curator_actor`, `decided_at`, `created_at`, `updated_at` | R/W |
| `EmailAddress` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `PhoneNumber` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `PostalAddress` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `ContactRecord` | `contact_id` | `vcard_uid`, `href`, `etag`, `cloud_id`, `source_user_id`, `addressbook_href`, `addressbook_name`, `addressbook_slug`, `created_import_run_id`, `last_import_run_id`, `organization_units`, reassignment audit fields, timestamps | R/W |
| `ContactImportRun` | `run_id` | `cloud_id`, `source_user_id`, `addressbooks_json`, `status`, contact/error counters, start/finish/rollback timestamps, `rollback_deleted_contacts`, `created_at`, `updated_at` | R/W |
| `Document` | `document_id` | title/path/source URL and origin/date fields; graph hash/extractor/index timestamps; evidence counters and last-query fields; analysis summary/model/key points/actors/uncertainties; mail linkage fields; timestamps | R/W |
| `MentionName` | `normalized` | `value`, `last_seen_value`, `created_at`, `updated_at` | R/W |
| `EntityObservation` | `observation_id` | `document_id`, canonical/observed/context text, `normalized`, suggested type/kind, confidence and admission fields, candidate IDs, extractor/status/rejection fields, curator decision/actor/target/reason fields, timestamps | R/W |
| `RelationObservation` | `relation_id` | `document_id`, `predicate`, `predicate_text`, `relation_text`, `evidence_text`, confidence/stance/chunk/extractor, validity and evidence-date fields, ontology fields, curator/review fields, `source_finding_id`, timestamps | R/W |
| `Claim` | Secondary label on `RelationObservation` | Uses the `RelationObservation` contract | W label |
| `MailMessage` | `mail_key` | `message_id`, `message_date`, subject, sender/recipient/reference JSON, `placeholder`, timestamps | R/W |
| `ResearchFinding` | `finding_id` | query/evidence frame JSON and hashes, intent, entity/role/relation/concept/constraint fields, verifier/planner/software metadata, provenance code/label, source metadata, observation counters, curator status/reason/actor/timestamps, suppressed entity texts, timestamps | R/W |
| `AKIResearchFinding` | Secondary label on `ResearchFinding` | Uses the `ResearchFinding` contract | W label |
| `ResearchRun` | `run_id` | canonical user and Nextcloud context, user/retrieval query, source scopes, frame/model/software metadata, curation/dismissal fields, timestamps | R/W |
| `CanonicalUser` | `canonical_user_id` | `nextcloud_login`, `nextcloud_server`, timestamps | W |
| `RAGSchemaMarker` | Legacy only | Older releases created marker nodes with dummy properties to register tokens. 0.8.5-rc4.1 no longer creates or reads them. Existing markers are inert and may remain until an explicit housekeeping migration is justified. | Legacy |

### Document provenance fields

The document provenance contract includes `source_origin`, `source_url`, `path`, `title`, `document_date`, `source_date`, `source_date_precision`, `source_date_confidence`, `source_date_basis` and `source_date_evidence`. Retrieval/query provenance adds `last_query_id`, `last_user_query`, `last_retrieval_query` and evidence timestamps/counters.

### Identity and curation provenance

Identity/name relationships retain source fields such as `source_contact_id`, `source_document_id`, `source_merge_entity_id`, `source_curator`, `extractor`, `resolution_policy`, `reason` and timestamps. Manual Findings decisions retain `curator_actor`, decision status/reason/timestamps and the supporting Finding/Document relationships.

## Relationship types and properties

Relationship properties are optional. Endpoint keys and labels provide identity; relationship metadata provides provenance and workflow state.

| Relationship | Direction / purpose | Optional properties used |
|---|---|---|
| `HAS_NAME` | `Entity -> EntityName` | kind, preferred, active, resolution policy, normalized form, confidence, source contact/document/merge/curator, extractor, observed text, retirement/policy/merge timestamps and actors |
| `HAS_SEARCH_ALIAS` | `Entity -> SearchAlias` | kind, weight, active, resolution policy, normalized form, source contact/document/merge/curator, extractor, retirement/policy/merge timestamps and actors |
| `HAS_EMAIL`, `HAS_PHONE`, `HAS_ADDRESS` | `Entity -> value node` | active, normalized, source contact, updated timestamp |
| `DESCRIBES` | `ContactRecord -> Entity` | resolved_by, updated timestamp |
| `SUPPLIES_NAME`, `SUPPLIES_EMAIL`, `SUPPLIES_PHONE`, `SUPPLIES_ADDRESS` | Contact provenance | no required relationship properties |
| `WORKS_AT` | `Person -> Organization` | active, preferred, kind, organization units, source contact, updated timestamp |
| `WORKS_IN` | `Person -> OrganizationalUnit` | active, source contact, updated timestamp |
| `PART_OF` | `OrganizationalUnit -> Organization/Unit` | active, source contact, updated timestamp |
| `SUPPLIES_ORG_UNIT` | Contact provenance for units | unit key, level, active, updated timestamp |
| `POSSIBLE_SAME_AS` | Review-only identity-equivalence candidate | score, reason, status, suggested_by, matched forms/types, carried-from-merge ID, timestamps |
| `SAME_AS` | Curator-confirmed non-destructive identity equivalence; both Entities remain active | active, reason, decided_by, decided_at, created_at, updated_at |
| `NOT_SAME_AS` | Persisted negative identity decision | reason, decided_by, decided_at, updated_at |
| `MERGED_INTO` | Stronger technical consolidation: retired `Entity -> survivor Entity` | method, merged_at, updated_at |
| `TOUCHED` | `ContactImportRun -> ContactRecord` | first/last touched timestamps |
| `HAS_ENTITY_OBSERVATION` | `Document -> EntityObservation` | extractor, updated_at |
| `RESOLVED_TO` | `EntityObservation -> Entity` | resolved_by, merged-from ID, updated_at |
| `MENTIONS` | `Document -> Entity` | count, observed values, resolution, max score, extractor, curated flag, merged-from ID, timestamps |
| `MENTIONS_NAME` | `Document -> MentionName` | status, count, observed values, candidate IDs/scores, normalized, extractor, updated_at |
| `POSSIBLE_MATCH` | `MentionName -> Entity` | extractor, max score, merged-from ID, last-seen/updated timestamps |
| `HAS_RELATION_OBSERVATION` | `Document -> RelationObservation` | extractor, updated_at |
| `SUBJECT`, `OBJECT` | Claim endpoints | no required relationship properties |
| `REPRESENTS_MAIL`, `ATTACHMENT_OF` | `Document -> MailMessage` | role, sidecar path, updated_at |
| `REPLIES_TO` | `MailMessage -> MailMessage` | source, updated_at |
| `SUPPORTED_BY` | `ResearchFinding -> Document` | verification status, relation binding, query/seen/created timestamps |
| `PERFORMED` | `CanonicalUser -> ResearchRun` | created/last-seen timestamps |
| `PRODUCED` | `ResearchRun -> ResearchFinding` | disposition, dismissal audit fields, created/last-seen/updated timestamps |
| `QUERY_ENTITY` | `ResearchFinding -> Entity` | frame entity ID, role, text, resolution, updated_at |
| `CURATED_ENTITY` | `ResearchFinding -> Entity` | frame text, role, curator, curated/updated timestamps |
| `DERIVED_FROM_FINDING` | `RelationObservation -> ResearchFinding` | no required relationship properties |

## Constraints

All are uniqueness constraints and are created with `IF NOT EXISTS`:

- `entity_id`: `Entity.entity_id`
- `contact_id`: `ContactRecord.contact_id`
- `contact_import_run_id`: `ContactImportRun.run_id`
- `entity_name_normalized`: `EntityName.normalized`
- `search_alias_normalized`: `SearchAlias.normalized`
- `entity_form_decision_normalized`: `EntityFormDecision.normalized`
- `email_normalized`, `phone_normalized`, `address_normalized`
- `org_unit_key`: `OrganizationalUnit.unit_key`
- `document_id`: `Document.document_id`
- `mention_name_normalized`: `MentionName.normalized`
- `entity_observation_id`: `EntityObservation.observation_id`
- `relation_observation_id`: `RelationObservation.relation_id`
- `mail_message_key`: `MailMessage.mail_key`
- `research_finding_id`: `ResearchFinding.finding_id`
- `research_run_id`: `ResearchRun.run_id`
- `canonical_user_id`: `CanonicalUser.canonical_user_id`

## Indexes

All are range indexes created with `IF NOT EXISTS`:

- Entity: `entity_display_name` on `display_name`; `entity_identity_key` on `identity_key`
- OrganizationalUnit: `org_unit_display_name` on `display_name`
- ContactRecord: `contact_uid` on `vcard_uid`; `contact_cloud` on `cloud_id`; `contact_source_user` on `source_user_id`; `contact_addressbook_name` on `addressbook_name`; `contact_addressbook_slug` on `addressbook_slug`; `contact_created_import_run` on `created_import_run_id`; `contact_last_import_run` on `last_import_run_id`
- EntityObservation: `entity_observation_document` on `document_id`; `entity_observation_normalized` on `normalized`; `entity_observation_status` on `status`; `entity_observation_curator_status` on `curator_status`
- RelationObservation: `relation_observation_document` on `document_id`; `relation_observation_predicate` on `predicate`; `relation_observation_stance` on `stance`; `relation_observation_curator_status` on `curator_status`
- ResearchFinding: `research_finding_frame_hash` on `frame_hash`; `research_finding_curation_hash` on `curation_hash`; `research_finding_provenance` on `provenance_code`
- ResearchRun: `research_run_user` on `canonical_user_id`; `research_run_status` on `curation_status`

## OpenRegister external evidence layer

ERG register research keeps provider-derived register knowledge in a separate
Neo4j namespace. It deliberately does **not** add labels to the curated
`Entity` / `ResearchFinding` graph.

The external layer uses:

- `OpenRegisterRun` — one resolver research run; it carries no user identity or
  Nextcloud archive path.
- `OpenRegisterEntity` with optional `OpenRegisterOrganization` or
  `OpenRegisterPerson` — deduplicated primarily by the stable OpenRegister ID.
- `ExternalFinding:OpenRegisterFinding` — per-run provenance with
  `provenance_code=openregister`, `curation_mode=external`, rank and
  selection state (`selected`, `candidate`, `bycatch`).
- `OpenRegisterFact` — structured list/history records retained from enriched
  provider data, linked to the external entity and the originating finding.
- `OPENREGISTER_ROLE` — time-bounded register roles such as board/director or
  procuration positions. `valid_from`, `valid_to` and `current` come only
  from provider-supplied structured data.
- `OpenRegisterSourceDocument` — an archived provider-supplied source document
  represented by its digest and non-user-specific metadata. User-specific
  Nextcloud paths and URLs remain only in that user's research dossier. The
  run-specific `SUPPORTED_BY` relationship may carry the numeric
  `nextcloud_file_id` of the archived copy so the evidence can be resolved
  back to Nextcloud without putting a user-specific copy ID on the global
  hash-deduplicated document node.

Relationships are `PRODUCED_EXTERNAL`, `DESCRIBES`,
`HAS_EXTERNAL_FACT`, `DERIVED_FROM`, `OPENREGISTER_ROLE`,
`HAS_SOURCE_DOCUMENT` and `SUPPORTED_BY`.

Search candidates that were not selected are retained rather than discarded.
The first unresolved candidate is marked `candidate`; additional results are
`bycatch`. A later research run may select the same stable external entity
without changing the historic status of the earlier run.

The external graph is written fail-open after resolver processing. Its schema is
created idempotently by `rag.openregister_graph` when register research is used
and Neo4j is enabled. Ordinary SunaQ graph retrieval and Graph-Lite curation do
not query these labels, so provider-derived register observations cannot become
curated internal facts implicitly.

## Query assumptions and warning policy

Queries may assume that constrained identifiers are present on matching nodes. They must not assume that any optional field or relationship type exists anywhere in the database. Neo4j emits an `UnknownPropertyKeyWarning` when a query directly names a property token that has never existed, and an `UnknownRelationshipTypeWarning` when a read query statically names a relationship type that has never been created.

RC4 attempted to avoid property warnings by writing optional names onto `RAGSchemaMarker` nodes. That was incomplete, drifted from the real application schema and polluted the graph. 0.8.5-rc4.1 instead:

1. reads optional node and relationship fields through `properties(value)['field']`;
2. reads optional/sparse relationship types through a generic relationship variable plus `type(r)='RELATIONSHIP_TYPE'` where absence is a valid state;
3. keeps relationship creation statements explicit (for example `MERGE ...[:MERGED_INTO]...`) so write-path spelling/schema errors remain visible;
4. creates only real constraints and indexes;
5. backfills only real application data where semantics are deterministic;
6. retains missing optional fields and absent optional relationship types as missing.

The recurring `RelationObservation.relation_text` property warning and sparse-graph relationship warnings such as `MENTIONS_NAME` or `MERGED_INTO` are instances of the same general rule: absence is valid and must not require synthetic graph data merely to register a token.

> **Empty-graph note:** Until the first seed/contact import has populated Neo4j, the graph is intentionally sparse. Neo4j may therefore emit notifications or warnings about optional labels, property keys or relationship types that do not exist yet. Such messages do not by themselves indicate a failed schema initialization. Connection failures, constraint/index errors and actual Cypher execution errors are not covered by this note and should still be investigated.

## Initialization and upgrades

`GraphStore.ensure_schema()` is the canonical entry point. It:

1. creates missing base constraints/indexes;
2. lazily backfills historic global Entity form decisions using `ON CREATE SET`;
3. fills missing `Entity.entity_kind` only, without reclassifying existing values;
4. creates the Research Finding/Run/User schema;
5. backfills a missing Finding `curation_hash` without changing Finding IDs.

The standard installer waits for selected local Neo4j and, on a fresh 0.8.6 installation, runs the equivalent of:

```bash
python -m rag.graph --config /opt/sunaq/config.yaml init
```

Recognized legacy installations retain their existing installation prefix; substitute that prefix (for example `/opt/nextcloud-rag/config.yaml`) when running the command manually.

The Dockerized installer runs the same command inside the API image. API startup also attempts the idempotent migration for independently managed deployments; failure is logged and deferred because Neo4j remains a degradable backend. Installer-selected local Neo4j failure is fatal.

## Legacy and migration notes

- Historic nodes may omit any optional property.
- Existing `RAGSchemaMarker` nodes are inert compatibility debris. 0.8.5-rc4.1 does not create, query or depend on them.
- Legacy Entity observations are promoted to `EntityFormDecision` only when no newer global decision exists.
- Legacy Findings receive `curation_hash` only when the stored query frame is parseable.
- Existing constraints or indexes, including equivalent objects already present under another name, are accepted by Neo4j's `IF NOT EXISTS` semantics.
- No migration deletes Entities, Documents, observations, claims, identity forms or provenance relationships.

## Verification

The normal CI uses contract tests because it does not run a Neo4j service. The tests cover fresh, partial, already initialized and repeated initialization; required constraints/indexes; non-overwriting legacy backfills; property-safe warning regressions; and installer invocation. A deployment-level acceptance test should additionally run `python -m rag.graph ... init` twice against a Neo4j 5 Community database and inspect server notifications/logs.
