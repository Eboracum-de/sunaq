# Graph-Lite Findings curation

**Reference:** `0.8.7-rc1`

Graph-Lite turns selected, verified research findings into **curated, document-grounded graph observations**. It is deliberately conservative: a ResearchFinding is provenance-bearing evidence that a query frame matched a document; it is not automatically a global fact.

Graph-Lite Findings are **optional**. Normal document retrieval, live ACL enforcement, verification and answer generation do not depend on Findings being curated or even enabled. A well-curated graph can improve entity resolution and retrieval efficiency and can support richer searches over recognized relationships, but it is an enrichment layer rather than a prerequisite for ordinary operation.

This makes Findings part of an optional learning/curation loop:

```text
ordinary search -> verified evidence -> optional Finding
                                  -> optional curation
                                  -> improved shared entity/relation knowledge
```

Deployments that do not need shared graph learning, or that prefer a smaller persistence/security surface, can disable Research Findings and continue to use the normal SunaQ path. Disabling Findings does not disable live ACL, Elasticsearch retrieval, optional Qdrant retrieval, verification or answer generation.

## Curation model

The manual workflow is:

1. **Entity resolution** — map each finding entity text to an existing Entity, create a corrected Person/Organization, or mark the normalized form globally as not-an-entity. Candidates include structured QueryFrame entities, named EvidenceFrame entities/mentions, entity-like constraints and relation **sources**. A verifier relation target is not promoted solely because it is a target; it remains eligible when independently extracted as an Entity or mention.
2. **Reusable form decision** — a global not-an-entity decision prevents the same normalized form from returning as a task in later Findings. A later explicit assignment or Entity creation reverses that decision without deleting the historic document observations.
3. **Name and alias defaults** — unique exact canonical names and unique curated contextual/search aliases are presented as defaults and can be stored together. Similar known Entities remain suggestions; choosing “Als Alias anlegen” creates the established contextual `SearchAlias` and assigns the current Finding.
4. **Document-grounded mention** — a confirmed mapping creates/updates an `EntityObservation`, `RESOLVED_TO` and `Document-[:MENTIONS]->Entity` provenance.
5. **Optional claim** — with at least two curated Entities, an administrator chooses Entity A, an ontology predicate and Entity B in one directed claim builder. The UI filters predicates to combinations admitted by the versioned relation ontology; the server validates the selection again. Predicate IDs are canonical uppercase IDs; curator-facing labels come from the ontology.
6. **RelationObservation** — the claim stays bound to its source Document and carries `DERIVED_FROM_FINDING` provenance.
7. **No automatic canonical fact edge** — the current release does not silently convert the claim into a global `Entity -> Entity` truth or query-expansion edge.
8. **Reversible claim decision** — an active manual claim may be withdrawn. The RelationObservation and its provenance remain stored with status `withdrawn`, but it no longer counts as an active claim.
9. **Mentions without a claim** — every confirmed Entity mapping already creates the document-grounded Mention. Once all Entity candidates are decided, the Finding may be closed as `mentions_only` without asserting a relation.

## Findings and ResearchRun states

A `ResearchFinding` is the shared, deduplicated result object. Existing Finding IDs keep their upgrade-compatible full QueryFrame formula, while persistence also maintains a structured `curation_hash` over Entities, relations, constraints and concepts. The free-form intent text is deliberately excluded from that curation fingerprint. Equivalent provider runs can therefore point to the same Finding and reuse the same global curation decision even when the model phrases the intent differently.

A `ResearchRun` is the per-query work/provenance object:

```text
CanonicalUser
     |
  PERFORMED
     v
ResearchRun
     |
  PRODUCED
     v
ResearchFinding ---- SUPPORTED_BY ----> Document
```

The ResearchRun stores the original user query, retrieval query, user identity and runtime provenance. A second query, another chat or another provider run may produce the same ResearchFinding without creating a second curation task.

Finding states include:

- `open` — at least one entity text still needs a global decision;
- `entities_resolved` — every entity text was curated or explicitly marked not-an-entity;
- `claimed` — at least one active manual document-grounded claim exists;
- `mentions_only` — entity decisions are complete and the curator deliberately recorded no semantic claim;
- `review_required` — a previously curated claim must be reviewed because a dependent Entity decision changed;
- `no_entity` — the Finding contains no entity text;
- `suppressed` — legacy/global Finding suppression. Normal queue cleanup now uses ResearchRun disposition instead.

ResearchRun queue states are separate:

- `open` — at least one currently visible produced Finding still needs work;
- `completed` — all currently visible Findings are already globally resolved or locally dismissed;
- `dismissed` — the curator deliberately removed this research run from the working queue.

The `ResearchRun-[:PRODUCED]->ResearchFinding` relationship also carries a per-run disposition. Dismissing a Finding there means only “do not ask me to curate this Finding for this research run”. It does **not** mark the shared Finding false, delete provenance or prevent another run from presenting the same still-open Finding.

## Research-centred curation workflow

The Admin landing page is a list of ResearchRuns with open, currently ACL-visible Findings. The administrator first selects a canonical Nextcloud user. SunaQ then:

1. loads ResearchRuns performed by that user;
2. collects their supporting document IDs;
3. re-checks those documents through the user's current Nextcloud WebDAV credential;
4. removes unauthorized Findings before calculating counts or rendering query/evidence data;
5. lets the curator open one ResearchRun and work through its remaining Findings.

A global Finding decision is reused automatically wherever that Finding appears later. Entity grouping and bulk work may still be useful inside a ResearchRun, but the primary human work unit is the originating research rather than a corpus-wide entity inbox.

A whole ResearchRun, or selected Findings only within that run, may be dismissed from the queue. This is queue state, not a graph truth decision.

## Lazy ACL self-cleanup

RC5 adds a deliberately narrow self-cleanup path to the same Finding ACL checks used by SunaQ Admin and `/curation/`.

When a live Nextcloud ACL request completes successfully and a numeric supporting `files:<id>` is not returned for the selected/current canonical user, SunaQ may remove that user's `ResearchRun-[:PRODUCED]->ResearchFinding` provenance **only while the Finding is still uncurated**. If no ResearchRun for any user references that uncurated Finding afterwards, the shared Finding node is garbage-collected.

The lazy cleanup never deletes curated Finding knowledge. A Finding is preserved when any of the following applies:

- a Finding-level curator status or suppression exists;
- at least one `CURATED_ENTITY` relationship exists;
- any `RelationObservation-[:DERIVED_FROM_FINDING]->ResearchFinding` provenance exists.

The cleanup is also deliberately non-destructive on uncertainty. Timeout, TLS/network failure, Nextcloud backend error, rejected/invalid credentials, disabled live ACL or a source that is not a numeric Nextcloud `files:<id>` cause fail-closed visibility but **no deletion**.

This is lazy cleanup, not a periodic reconciler and not a general document purge. It currently runs when Findings are ACL-filtered for SunaQ Admin or self-service curation. Qdrant synchronization does not trigger Neo4j deletion.

## Durable manual state

Manual Graph-Lite decisions must survive later machine re-indexing:

- Finding Entity actions write immediately; the former mandatory JSON preview is available only through the optional “Details ansehen” action;
- unique name/alias defaults can be accepted in one bulk action and remain individually reversible;
- normalized global not-an-entity decisions are stored as `EntityFormDecision` records and are reused across Findings; historic manual observations are promoted lazily when a deployment has not yet run the bulk schema backfill;
- finding-derived curated `MENTIONS` are restored after document relinking;
- manual finding claims are excluded from replacement of machine-derived relation observations;
- reassigning or suppressing a curated entity marks dependent manual claims `review_required` rather than leaving stale endpoints active.

## Evidence rule

A manual claim should use persisted evidence from the finding/EvidenceFrame when available. The user's search/query intent is not treated as document evidence merely because it led to the finding.

## Relation ontology

Manual claims use the same versioned relation ontology as automatic document-relation extraction: `ontology/relations-v1.yaml`.

- the EvidenceFrame is rendered as readable concepts, criteria, mentions and observed relationships, with raw JSON available only as an expandable diagnostic;
- entity candidates that a curator marked as not-an-entity are removed from the curated EvidenceFrame view, together with relation suggestions that use them as an endpoint; the immutable raw JSON still retains the original verifier output for audit provenance;
- free-form verifier predicates remain labelled observations and are never promoted automatically;
- relation targets remain visible in the EvidenceFrame but are not promoted solely from their target role into Entity curation; independently extracted Entities or mentions remain candidates;
- free-form predicates are rejected server-side;
- predicate IDs are canonical uppercase IDs such as `SHAREHOLDER_OF`, `DIRECTOR_OF` and `REGISTERED_AT`;
- the Admin UI shows localized display labels from the ontology rather than asking the curator to invent a predicate name;
- only predicates whose document source policy and subject/object type/kind signatures fit the selected Entity pair are offered;
- a human curator may still add an explanatory note, but that note does not change the machine-readable predicate;
- if no ontology predicate fits the evidence, the finding can be closed as `mentions_only` rather than inventing a new relation.

If a recurring relation is genuinely missing, extend and version the ontology deliberately instead of creating one-off predicates in the Admin UI.

## User provenance and ACL boundary

User provenance is persisted through ResearchRuns rather than by copying a user identifier directly onto the shared Finding:

```text
CanonicalUser A --PERFORMED--> Run A1 --PRODUCED--\
CanonicalUser A --PERFORMED--> Run A2 --PRODUCED----> ResearchFinding F17
CanonicalUser B --PERFORMED--> Run B1 --PRODUCED--/          |
                                                        SUPPORTED_BY
                                                             |
                                                          Document
```

This records not only who observed a Finding but also through which concrete research request it was produced.

Neither `PERFORMED` nor `PRODUCED` grants access to the supporting document. Every Admin or end-user Finding view re-checks the supporting Document through live Nextcloud ACL in the selected/current user's context before exposing document title/path, EvidenceFrame, QueryFrame-derived work details or claims. Unauthorized Findings are omitted rather than reported as hidden counts.

The Admin **Observations** and **Relations** views use the same document boundary. An administrator must select a canonical Nextcloud user; rows are then included only when that user's current credential passes live ACL for the supporting Document. The views fail closed when the user, credential, ACL service or document binding is missing.

This is user-scoped evidence filtering inside a trusted administration surface, not isolation from the SunaQ administrator. The administrator can switch the selected canonical user and therefore inspect evidence visible to that account. Ordinary users should never receive SunaQ Admin credentials.

The Observations page is a work queue by default. Its `needs_review` state includes only unresolved, ambiguous or provisional automatic observations without a curator decision. Manually confirmed/corrected observations and `manual_not_entity` decisions remain auditable through their explicit filters but no longer reappear in the default queue. The selected state is enforced both in the Neo4j query and defensively on the returned rows.

Global curation remains shared knowledge. If one authorized curator maps an entity or creates a document-grounded claim, a later authorized user reaching the same Finding benefits from that decision without repeating it. Curator identity is retained on the shared decision for audit provenance.

### Optional end-user self-service

End-user curation is disabled by default and has two gates:

```yaml
research_findings:
  curation:
    admin_user_context: true
    user_self_service: false
    session_max_seconds: 7200
```

The SunaQ administrator can additionally enable or disable Findings curation per canonical user.

When self-service is enabled, `/curation/` uses Nextcloud Login Flow v2. It does not create a separate SunaQ password and does not reuse/store the resulting app password as a normal provider credential. The app password belongs only to an encrypted temporary curation session. The login identity is supplied by Nextcloud after the flow completes; no username is trusted from a curation form. The current self-service view exposes only ResearchRuns belonging to that canonical user.

The session has a hard absolute lifetime; the default is 7200 seconds (two hours). Activity updates diagnostics only and never extends `expires_at`. Every curation request verifies that the session is active, unexpired, the canonical user is still enabled and the per-user curation permission is still present.

Logout or expiry invalidates the local session before attempting to revoke the temporary app password at Nextcloud. Failed revocation remains `revocation_pending` and is never accepted as an active session. API startup invalidates all surviving curation sessions and retries revocation so an API restart does not silently resurrect a browser session.

Curated Graph-Lite claims are not automatically used for query expansion in the current release. Before a future release uses document-grounded claims as answer/retrieval evidence, that path must preserve supporting-document ACL semantics. Shared identity/alias vocabulary may be reused more broadly as retrieval knowledge; see `THREAT-MODEL.md`.

## Deliberate non-goals in the current release

- automatic promotion of ResearchFindings to global facts;
- automatic use of curated claims for query expansion;
- LLM claim distillation into canonical relations without curator review;
- free-form or one-off predicates created ad hoc in the Admin UI;
- treating mere co-occurrence as a semantic relation.
