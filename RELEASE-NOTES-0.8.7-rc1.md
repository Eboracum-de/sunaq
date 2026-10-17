# SunaQ 0.8.7-rc1

Release date: 2026-10-17

SunaQ 0.8.7-rc1 is a retrieval-architecture, administration and operations
release candidate. It builds on the SRC/ERG security boundary introduced in
0.8.6, makes the ERG trust envelope more explicit, improves structured lexical
retrieval, separates deployment from capability policy and consolidates
research provenance around per-conversation dossiers.

This remains an early release candidate for technically experienced operators.
It is intended as a feature-freeze baseline, not as a declaration of broad
production compatibility.

## Architecture and administration

### SRC, ERG Internal and ERG Full

The existing SRC/ERG architecture is refined into three administrative trust
and lifecycle envelopes:

- **SRC — Secure RAG Core** keeps the private document path deliberately narrow:
  Nextcloud Documents → FullTextSearch/Elasticsearch → live Nextcloud ACL →
  model processing. SunaQ does not create an additional full-corpus persistent
  derived knowledge store in SRC. The Nextcloud deployment owns the lifecycle
  of its FullTextSearch/Elasticsearch index; SunaQ treats that index as derived
  retrieval state and never as an authorization authority.
- **ERG Internal** adds locally controlled SunaQ-derived state and services,
  such as Qdrant vector retrieval, Neo4j entity/alias/graph state, Research
  Findings and the local resolver gateway. This introduces additional
  lifecycle, synchronization and provenance obligations, but does not require
  public or third-party research gateways.
- **ERG Full** additionally permits externally connected information/research
  capabilities such as Mail ingestion, Web search/retrieval, external resolver
  adapters and delegated research. The shipped `workgroup` preset deliberately
  excludes Mail; external information acquisition belongs to ERG Full.

The distinction is primarily administrative: ERG Full introduces provider,
egress and external-trust questions that do not arise from an ERG Internal
deployment using only operator-controlled infrastructure.

### Deployment, tier and preset are separate axes

0.8.7-rc1 separates concepts that earlier release candidates tended to conflate:

- `--deployment standard|dockerized` selects packaging and process/container
  layout;
- `--architecture-tier src|erg-internal|erg-full` selects the enforced
  information/data envelope;
- `--preset core|workgroup|full` selects the recommended capability bundle
  for SRC, ERG Internal or ERG Full.

A deployment mechanism does not imply an architecture tier. Dockerized is not
synonymous with SRC, and Standard is not synonymous with ERG.

The historical `--profile super-light` option remains a compatibility alias
for `--deployment dockerized --preset core`. Existing
`.aki-rag-installation` and `install/super-light` state are recognized only
for upgrade/migration compatibility.

### Data egress is orthogonal to the retrieval tier

SRC, ERG Internal and ERG Full describe retrieval/source capability boundaries;
they do **not** decide whether model processing is local or remote.

An administrator may deliberately use an external planner, verifier or answer
LLM even with SRC. Conversely, an ERG deployment can keep model processing
local. Egress policy is therefore a separate control dimension.

0.8.7 adds a deterministic, class-aware **Data Egress Guard**. External
research queries, model-processing calls and external embeddings are separate
policy classes and can independently be configured as allowed, audited,
guarded or blocked. The deterministic guard reduces accidental disclosure of
credential/identifier-like material; it is deliberately not presented as a
semantic DLP classifier.

### Clearer internal contracts

Provider/orchestrator, resolver, SRC retriever and ERG retriever
responsibilities are more explicitly separated. These boundaries reduce
coupling in the historical provider/middleware monolith and prepare further
modularization without changing the public OpenAI-compatible surface.

## Retrieval

### Structured query planning without giving the LLM backend control

SunaQ preserves the deterministic backend boundary already established in
0.8.6: an LLM is never allowed to provide executable Elasticsearch JSON DSL.
Trusted middleware code owns the Elasticsearch request structure.

0.8.7 improves the structured representation between natural-language planning
and backend compilation. Required concepts can be represented as alternative
groups, for example:

```text
MUST (CV OR résumé OR "curriculum vitae" OR "professional background")
AND
MUST ("Jane Doe")
```

This allows synonymous or equivalent lexical forms to improve recall without
turning every alternative into a separate mandatory term and without weakening
a required concept into an optional hint.

Entity handling is richer as well. The planner may propose bounded lexical
variants and distinguish a lexically required target from contextual entity
information. Trusted code compiles those hints into required or optional
groups; the planner still cannot emit executable backend syntax. The primary
user form remains preserved when variants are added. Orthographically distinct
variants are retained as distinct lexical alternatives rather than being
collapsed merely because Unicode case-folding treats them as equivalent; this
matters for forms such as `Musterstrasse` and `Musterstraße`.

The structured planner does not replace explicit user search syntax. Expert
lexical expressions such as `+required -excluded optional` and quoted phrases
remain available and are parsed by trusted middleware rather than interpreted
as raw Elasticsearch DSL. Explicit occurrence markers and user-entered phrase
constraints therefore survive planner/rewrite processing.

The normal path is therefore:

```text
user question
    ↓
bounded SearchSpec / structured intent
    ↓
deterministic trusted compiler
    ↓
Elasticsearch / FullTextSearch request
```

Explicit names, identifiers, years, quoted phrases, filenames and required
concept groups receive stronger deterministic preservation so that rewriting is
less likely to discard the distinguishing part of a request.

### Retrieval quality and authorization

- Elasticsearch lexical compilation/search strategy is refined for entity +
  concept queries, identifiers, phrases and document-type/concept constraints.
- SearchSpec transport preserves `concept_groups` and `entity_hints`
  through the Provider → API boundary.
- The historical normal short-query Fast Path is removed from SearchSpec
  execution.
- Qdrant/vector retrieval remains an optional recall arm in ERG, not a required
  repair layer for lexical retrieval.
- ACL metadata prefiltering remains a bounded optimization only.
- Every private document that becomes verifier or answer evidence still passes
  the authoritative live Nextcloud ACL.
- Provenance written after the ACL boundary is projected to authorized document
  identities/ranks; denied candidate identities and pre-ACL backend ranking
  details are not exported into portable ResearchRun records.

### Recall, verification and orientation evidence

0.8.7 makes the boundary between broad recall and evidentiary acceptance more
explicit. Retrieval is allowed to be recall-oriented; the Candidate Verifier is
not. Candidates are classified as `match`, `uncertain` or `reject`, with a
separate document-binding classification. A document that merely references
the requested subject/relation cannot become a confirmed match, and explicit
safe constraints such as requested years are audited after model review.

When there is no confirmed match but one or more `uncertain` candidates remain,
SunaQ may use them only as **clearly marked orientation evidence** under a
dedicated answer guard. They are not silently upgraded into the requested
document/result. This is intended for cases such as "no verified land-register extract (Grundbuchauszug), but these documents contain bounded clues that may help narrow
the next search."

Research Findings follow the stricter side of that boundary: only final
Candidate-Verifier `match` results with `direct` document binding are eligible
for automatic positive Finding persistence. `uncertain`, `reject` and
reference-only candidates do not become graph facts.

### Standard (Schnell), Thorough (Gründlich) and Deep (Tief)

The current rc1 baseline deliberately keeps all three shipped research profiles
at **one retrieval round**. Their main difference is budget:

| Profile | Verifier/candidate window | Answer context |
| --- | ---: | ---: |
| Standard (Schnell) | 10 | 10 documents / 25k chars |
| Thorough (Gründlich) | 30 | 30 documents / 100k chars |
| Deep (Tief) | 50 | 50 documents / 200k chars |

Additional retrieval rounds and planner/model-thinking experiments are deferred
until the first-pass retrieval architecture has more field data.

The richer SearchSpec/Concept-Group representation is the intended foundation
for later controlled multi-round retrieval, but this release does not claim
that behavior as complete.

## Resolver and research capabilities

The ERG resolver is a first-class architectural boundary rather than an
incidental helper inside the provider.

Resolution resources answer questions such as "which person/company/register
record is meant?" and can use locally controlled or externally connected
adapters according to the configured ERG tier and egress policy. Resolver
output can improve query preparation without automatically becoming answer
evidence.

This separation matters for contact data, company-register identifiers,
postal/locality resolution and external register services: identity/reference
resolution and evidence retrieval are related but distinct operations.

## Research dossiers and provenance

Research archiving is organized around a per-conversation dossier with per-run
provenance rather than a separate user-facing Web-archive lifecycle.

Web Research evidence and run artifacts are stored with the corresponding chat
research dossier. Archive roots remain excluded from implicit primary-document
retrieval; reusing prior research as evidence is explicit.

ResearchRun persistence and portable retrieval provenance are hardened:

- small ResearchRun metadata/provenance writes are first committed to a durable
  SQLite outbox (`runtime/dossier_queue.sqlite`) before the user-visible answer
  completes;
- one provider-side dossier writer drains that outbox in conversation order and
  re-queues interrupted in-flight jobs after a provider restart;
- ResearchArchive and Web snapshot writers share one Nextcloud DAV transport
  with bounded retry/backoff for transient `423 Locked` responses, avoiding the
  earlier race between sibling WebDAV writers;
- run reservation/materialization ordering is fixed so provenance writers can
  safely materialize a reserved run;
- retrieval strategy and actual execution are separated into
  `retrieval-decision.v2.json` and `retrieval-execution.v1.json`;
- private candidate provenance starts on the post-live-ACL side of the boundary;
- `documents.json` uses the same authorized-rank projection;
- verifier decisions are persisted only for ACL-authorized document IDs.

Portable records may retain executed queries, but raw hit counts, denied
candidate identities, pre-ACL Elasticsearch/RRF/vector ranks and backend scores
are excluded.

## Web Retrieval and Delegated Research

Web Retrieval and Delegated Research remain different backend classes:

- **Web Retrieval** returns public candidate sources/evidence; SunaQ owns
  verification and answer synthesis.
- **Delegated Research** allows an external research provider to own public-Web
  search and answer synthesis and return normalized citations/provenance.

Public research queries, remote model-processing calls and external embeddings
use separate egress-policy classes. Choosing a Web/research model does not let a
planner invent another provider, endpoint or credential.

The current guard is not a complete confidentiality/DLP system. In particular,
a contextual follow-up rewrite can incorporate facts from earlier private
answers if an administrator routes that processing step to a remote model.

## Deployment and operations

### Installer and upgrade semantics

Installer composition is stricter and more predictable than in 0.8.6:

- selecting only an architecture tier yields the bare tier rather than silently
  installing an implied bundle;
- `core`, `workgroup` and `full` are atomic named presets;
- positive `--with-*` switches build explicit custom combinations;
- negative component switches are rerun/removal operations and are not used to
  mutate a named fresh-install preset;
- invalid tier/capability combinations fail early, for example Web/Playwright
  acquisition outside ERG Full.

Installer reruns also apply a centralized **non-destructive config upgrade**
pass. It materializes newly required operational defaults such as persistent
sync state/status paths, the durable dossier-writer configuration and
`sync_worker.pause_check_documents`, while preserving explicit administrator
values and optional capability/policy choices. Manual deployments receive
configuration diagnostics instead of silently depending on missing defaults.

### Runtime worker gates

Sync and Mail workers now have deployment-neutral runtime gates. Fresh installs
seed both as paused, and missing runtime-control state fails closed to paused. A
process/container may therefore be running while its work gate remains closed.

SunaQ Admin can release or pause these workers without rewriting
`config.yaml` and without being granted Docker/systemd privileges. Vector sync
checks the runtime gate cooperatively at bounded document intervals (default:
25 documents). If a traversal is paused, it is **not** treated as a complete
corpus pass and stale-vector deletion is skipped for that interrupted run.

### Vector overview and Qdrant sync

SunaQ Admin adds a document-centric Vector/Qdrant overview showing, among other
things:

- Elasticsearch document count;
- vectorized document count;
- Qdrant point/chunk count;
- last-sync exclusions and deferred embedding work.

Qdrant synchronization uses the text already extracted/indexed by
Nextcloud FullTextSearch/Elasticsearch. SunaQ does not independently download
and decode the complete Nextcloud file corpus for vectorization.

Nextcloud remains the source of record and the live authorization authority.

The protected Admin **System** view also adds deployment-neutral diagnostics for
API/provider liveness and model-role health. Provider liveness uses the cheap
`/live` probe while the more expensive detailed health checks remain separate;
role probes run concurrently to avoid serial timeout amplification. Host-level
maintenance switching and service-log control intentionally remain operator
functions rather than granting the Admin process Docker/systemd privileges.

### Common operator entrypoints

Both deployment mechanisms use the same top-level operational entrypoints:

- `start-all.sh`
- `stop-all.sh`
- `status.sh`
- `install/maintenance-mode.sh`
- `install/smoke-test.sh`

Fresh installs and installer reruns enter maintenance mode before normal
services are exposed.

A fresh ERG installation may take noticeably longer on the first Neo4j database
initialization. An immediate smoke/status run can therefore report Neo4j as not
yet ready while its container is still progressing. An unchanged repeat run
must become green; persistent failure is not considered normal first-start
behavior.

### Legacy FullTextSearch estates

The maintenance-only `fts_reconcile` helper addresses common drift in long-lived
Nextcloud FullTextSearch installations:

- stale FullTextSearch bookkeeping for files that no longer exist;
- current Files/FTS records whose Elasticsearch `files:<fileid>` document is
  missing;
- PDFs missing from Elasticsearch or lacking sufficient extracted text and
  requiring the site's existing OCR repair workflow.

The helper diagnoses/requeues through normal Nextcloud/FullTextSearch mechanisms;
it does not write reconstructed document content directly into Elasticsearch.
Where an existing site OCR workflow is driven by a Nextcloud System Tag such as
`OCR`, reconciliation can assign that already configured tag through the normal
Nextcloud System Tags API and leave OCR/reindex execution to the site's existing
workflow.

## SunaQ Recherche 0.3.16 / Nextcloud integration

The bundled Nextcloud app is **SunaQ Recherche 0.3.16** and targets Nextcloud
23+.

The client includes:

- SSE streaming with the JSON path retained as compatibility fallback;
- capability-aware source selection and request/retrieval progress;
- per-user Chat archive handling;
- Files integration for **"Dateinamen mit SunaQ vorschlagen"**.

The filename action uses the existing explicit-document/live-ACL path to obtain
a proposed filename. The user can edit the proposal, and only explicit
confirmation invokes a normal Nextcloud rename.

0.3.16 keeps the NC23-25 legacy Files-action path and adds compatibility
adapters for the NC26-32 and NC33+ action generations. The modern action path
has been field-tested on NC34.

Dot-prefixed machine metadata/sidecars are excluded from normal RAG evidence,
Qdrant synchronization and GraphQueue processing.

## Backup and restore

The console recovery utility has been aligned with the 0.8.7 state model.

The recovery set includes, where present:

- SunaQ configuration/runtime/provider state;
- administrator-owned `models/` profiles and prompts;
- `runtime/users.sqlite` plus its matching credential master key;
- Source Registry, Graph Queue, Mail and Research SQLite state;
- worker-control state and optional local RetrievalRecords;
- current Dockerized installer state;
- nginx TLS/auth/operator state;
- bundled Neo4j including manual curation/Findings state.

Configured non-rebuildable SQLite state outside the installation prefix is a
blocking condition rather than a silent omission.

Nextcloud, Elasticsearch, external Neo4j, OpenWebUI/Playwright state and Qdrant
vectors are deliberately outside the SunaQ recovery set.

Because Qdrant is rebuildable derived state, its incremental sync database is
also excluded/reset on restore. This prevents a disaster recovery from pairing
an empty/different Qdrant collection with stale `unchanged` synchronization
state. After such a restore, rebuild Qdrant from Elasticsearch before returning
the vector worker to service.

Standard restore normalizes restored service-owned files to the UID/GID of the
prepared target installation while retaining the credential master key as a
root-owned group-readable secret.

## Security and privacy notes

The central private-document invariant is unchanged:

> Retrieval systems propose candidates. Nextcloud remains the final
> authorization authority.

0.8.7 strengthens the surrounding defense-in-depth model:

- model-generated search intent remains separated from executable backend query
  languages;
- required lexical alternatives are compiled by trusted code rather than
  delegated to arbitrary model syntax;
- egress decisions are explicit and orthogonal to the retrieval tier;
- external resolver/research capabilities are separately gated;
- private ResearchRun provenance is projected from the authorized side of the
  live-ACL boundary;
- structured-evidence and untrusted-evidence guards continue to treat retrieved
  content as data rather than executable instructions;
- Web/fetch paths retain SSRF/internal-destination protections.

## Compatibility and known limitations

- This is still an early release candidate. The tested combinations do not
  constitute a complete Nextcloud/Elasticsearch/model compatibility matrix.
- Existing installations below paths such as `/opt/nextcloud-rag` remain
  supported in place; fresh installations default to `/opt/sunaq`.
- The legacy provider model ID `nextcloud-hybrid-rag` remains an alias of
  `sunaq-standard`.
- Qdrant remains rebuildable derived state; Neo4j/local resolver state has its
  own import/curation lifecycle.
- Live ACL protects current access to private document evidence, but it does not
  automatically erase already persisted chat/archive copies or every derived
  observation after source revocation.
- Server-authoritative Conversation State is **not** part of the 0.8.7-rc1
  contract. Current client history is bounded follow-up context rather than
  authoritative conversation provenance.
- A central Derived Reconciler across all derived stores is target architecture,
  not implemented rc1 behavior.
- Zero-downtime credential-master-key rotation is not yet documented/supported.

## Field validation for this release candidate

Before publication, the release head has been exercised through the intended
deployment/update paths, including:

- fresh Dockerized installation on a clean VM;
- in-place Standard upgrade on an existing historical
  `/opt/nextcloud-rag` installation;
- smoke checks for the selected Elasticsearch/Qdrant/Neo4j/resolver/
  Playwright/API/provider/LLM-role stack;
- local Qwen3:8B operation as well as remote-model operation;
- modern Files context-menu/rename behavior on NC34;
- targeted recovery with a deliberately removed `runtime/users.sqlite`
  restored from a verified SunaQ recovery set.

A first immediate Neo4j smoke check on the fresh ERG installation was allowed to
fail during initial database startup; the unchanged follow-up run was green.

## Deliberately deferred

The following remain follow-up work rather than rc1 release blockers:

- server-authoritative Conversation State;
- central reconciliation across all derived stores;
- additional retrieval rounds for Thorough (Gründlich) / Deep (Tief);
- broader DLP/malware/reputation adapters beyond the deterministic hooks and
  current SSRF protections;
- complete automatic revocation propagation into retained chat/archive copies;
- zero-downtime credential-master-key rotation.

For operational constraints and security boundaries, read
`docs/KNOWN-LIMITATIONS.md`, `docs/THREAT-MODEL.md`,
`docs/DATA-LIFECYCLE.md`, `docs/SRC-ERG.md` and
`docs/BETA-OPERATIONS.md` before production use.
