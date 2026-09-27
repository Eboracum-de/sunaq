# SunaQ architecture
## Architecture and design baseline 0.8.6-rc1.2

**Updated:** 24 September 2026  
**Status:** Release Candidate  
**Reference version:** `0.8.6-rc1.2`

---

## 1. Overview

SunaQ connects an existing Nextcloud document estate to several retrieval paths and a replaceable reasoning/answer backend. It is not a document store and does not maintain an independent authorization database. The retrieval/evidence layer is the core: SunaQ decides what may be searched, what evidence is ACL-visible, how candidates are verified and what context is passed downstream.

The core authorization rule is:

> **Retrieval systems propose candidates. Nextcloud remains the final authorization authority for private document evidence.**

Elasticsearch, Qdrant and Neo4j may contribute retrieval signals or candidate documents. Before private document content becomes verifier or answer evidence, the concrete file candidate is checked live through Nextcloud for the current user.

If live authorization removes a candidate, the normal path does not adaptively fetch lower-ranked documents merely to fill the context window.

The reference architecture can combine:

- Nextcloud FullTextSearch / Elasticsearch for lexical retrieval;
- optional Qdrant for semantic retrieval;
- Neo4j for seed/alias/entity context and optional Graph-Lite functions;
- result fusion and deduplication;
- an optional local or external reranker;
- live Nextcloud WebDAV authorization;
- a compact document-grounded Candidate Verifier;
- one or more configurable LLM roles;
- optional public-Web research through Brave Search or SearXNG;
- optional Nextcloud-backed Web, Mail and Chat archives.

The model layers are independently configurable. Embedding models, rerankers and planner/verifier/evidence/answer roles can be local or external according to administrator policy. In 0.8.6, user-visible SunaQ profiles select request-local research budgets and role routing without making the underlying LLM itself the product-level model.

### 1.1 SRC and ERG capability layers

The codebase enforces two architecture tiers:

```text
A — Secure RAG Core (SRC)
    Nextcloud documents
            ↓
    FullTextSearch / Elasticsearch (files only)
            ↓
       live Nextcloud ACL (mandatory)
            ↓
 local or administrator-approved LLM roles
    one retrieval round

B — Eboracum Research Gate (ERG)
    SRC foundation + selected extensions
            ↓
    Mail / Web / Chat archives
    broader external egress surfaces
    live Web research / Playwright
    Qdrant / semantic retrieval
    Findings / Graph-Lite / document graph
    additional retrieval rounds
```

SRC is a runtime contract in rc1.2, not a marketing label. Incompatible
configuration fails validation; omitted retrieval arms are normalized to
`files`; deterministic document selectors are filtered to ordinary Documents;
and ERG-only internal endpoints are unavailable. Neo4j may still provide
administrator-owned seed/alias context without becoming a document-evidence arm.
Remote LLM routing is an administrator-controlled egress choice within either
tier; SRC does not require zero model egress, and remote evidence caps remain
mandatory whenever a role endpoint is classified as remote.

ERG remains a menu rather than a monolithic advanced mode. Enabling an ERG
capability can add retained state, untrusted input, external egress,
latency/resource cost or lifecycle obligations.

Super-Light ships as formal SRC in rc1.2. The full reference configuration is
ERG for upgrade compatibility with installations that already used optional
capabilities. See [SRC-ERG.md](SRC-ERG.md).

### 1.2 Policy/inspection boundaries

rc1.1 now wires a small, shared hook contract into the boundaries where SunaQ
sends data outward, fetches untrusted content or writes imported/generated
content back to Nextcloud. The stages are `outbound_query`, `pre_fetch`,
`post_fetch`, `pre_persist` and `pre_model_egress`.

The shipped evaluator remains deliberately a no-op: every stage returns
`ALLOW`, so the hooks do not claim malware, DLP or reputation filtering.
rc1.2 adds non-optional application-level SSRF/network destination enforcement
for Web and Playwright independently of this hook scaffold. Concrete scanner/DLP
adapters remain optional future extensions. Evaluator failures are not swallowed,
allowing a configured required adapter to fail closed.

---

## 2. Scope and operating assumptions

The architecture is designed for installations that already have a useful Nextcloud document corpus and, in many cases, an existing FullTextSearch/Elasticsearch deployment.

Typical retrieval challenges include:

- alternate spellings and abbreviations;
- OCR errors;
- incomplete or colloquial user questions;
- relationships that cannot be expressed by one keyword;
- semantically similar but factually irrelevant documents;
- multiple versions or contradictory document states;
- changing Nextcloud permissions.

The middleware translates the user request into a constrained `SearchSpec`. The model may produce a human-style Nextcloud full-text expression and a semantic query, while the middleware itself constructs backend requests.

The model does not emit raw Elasticsearch JSON DSL for execution.

---

## 3. Design principles

### 3.1 Retrieval and authorization are separate concerns

Search/index systems may have stale, broader or differently synchronized knowledge than the current Nextcloud file tree. They are therefore candidate sources, not permission authorities.

### 3.2 Document evidence remains document-grounded

A semantic match or graph relation signal is not sufficient evidence by itself. Where enabled, the Candidate Verifier checks whether the candidate document directly supports the information need.

### 3.3 Missing evidence is represented explicitly

When authorized, relevant evidence is not available, the system may return fewer results or an insufficient-evidence response. It does not intentionally replace missing evidence with weaker documents simply to maintain a result count.

### 3.4 Models and providers are replaceable components

Answer LLM, planner/verifier roles, embedding backend, reranker and Web search provider are independently configurable.

Changing an answer model does not require re-indexing the vector corpus. Changing the embedding model normally does, because vector spaces from different embedding models must not be mixed.

### 3.5 Public Web research is a separate evidence path

Public Web discovery, fetch, passage selection, relevance review and archiving are handled separately from private document retrieval.

### 3.6 SunaQ profiles are research policies, not LLM identities

The OpenAI-compatible `/v1/models` endpoint exposes SunaQ research profiles rather
than raw backend-model names. The shipped rc1 profiles are **Schnell**,
**Gründlich** and **Tief**.

All three currently use one retrieval round, Evidence Review off and planner
thinking off. Their intentional difference is the amount of candidate
verification and answer context they may consume. This keeps the first profile
experiment attributable to budget rather than mixing budget, extra retrieval
rounds and model reasoning.

Model access is server-side and per canonical user. Schnell is the default;
stronger profiles are opt-in. A follow-up action may recommend a stronger model
only when that model is actually allowed for the current user.

### 3.7 SunaQ controls evidence; downstream systems consume it

The current implementation supports Ollama and OpenAI-compatible LLM backends for
planner/verifier/evidence/answer roles. Architecturally, the downstream endpoint
is replaceable: an agent or workflow system can be used when it presents a
compatible contract.

The security boundary remains on the SunaQ side. A downstream system should not
bypass SunaQ to query Nextcloud, Elasticsearch or Qdrant directly. For future
agentic retrieval loops, the intended pattern is:

```text
backend detects evidence gap
          |
          v
structured request for another search
          |
          v
SunaQ validates scope / ACL / budget
          |
          v
SunaQ executes retrieval and returns new evidence
```

External side effects such as sending mail are a separate future action layer and
should require explicit capability/policy/approval checks rather than being an
implicit property of an answer backend.

### 3.8 Graph-Lite is optional enrichment

Graph-Lite and Research Findings are not prerequisites for normal document search.

Ordinary Elasticsearch/Qdrant retrieval, live ACL, verifier and answer generation continue to operate when Findings are disabled or left uncurated.

Curated identity and relation knowledge can improve query expansion, entity resolution and later graph-assisted searches, but this is an optional learning/curation loop rather than a mandatory runtime dependency.

### 3.9 Model output may influence semantics, not authorization

LLMs participate in bounded semantic decisions such as query rewriting,
candidate verification, evidence selection and answer generation. Their output
therefore can influence **which authorized evidence is considered relevant** and
what answer is produced.

LLM output is not treated as executable middleware control:

- the planner emits a validated/normalized SearchSpec rather than raw
  Elasticsearch JSON DSL;
- retrieval arms, source capabilities, budgets and model entitlements are
  application policy;
- ACL-prefilter identity and groups come from authenticated Nextcloud OCS data,
  not from the query or model;
- the final live WebDAV ACL is deterministic and independent of model output;
- normal retrieval has no generic tool/action interface and no arbitrary
  filesystem, shell, database or Nextcloud write capability.

This means prompt injection is not absent, but its consequence is deliberately
bounded. In layer A it is principally an evidence-integrity/answer-quality risk.
Persistence/egress risks become materially larger when optional ERG capabilities
such as Web/Mail/Chat ingestion, Graph/Findings persistence or other external
boundaries are enabled.

---

## 4. High-level architecture

The policy/inspection stages wired in rc1.1 are shown in brackets. The shipped
evaluator returns `ALLOW` for all stages; concrete adapters remain deferred.

```text
                 SunaQ Recherche / OpenWebUI / trusted API client
                                   |
                         OpenAI-compatible provider
                                   |
                         Query rewrite / SearchSpec
                                   |
                 +-----------------+-----------------+
                 |                                   |
         private/internal path                  public Web path
                 |                                   |
        Neo4j seed/alias context          [outbound_query policy]
                 |                                   |
        +--------+---------+                    Brave / SearXNG
        |                  |                         |
 Elasticsearch         Qdrant                   URL discovery
  required arm         optional                     |
        |                  |                  [pre_fetch policy]
        +--------+---------+                         |
                 |                              HTTP/Playwright
          fusion / dedup                            fetch
                 |                                   |
        optional reranker                    [post_fetch inspect]
                 |                                   |
        LIVE NEXTCLOUD ACL                    passage selection
                 |                                   |
       optional Candidate Verifier             relevance gate
                 |                                   |
                 +-----------------+-----------------+
                                   |
                         bounded evidence/context
                                   |
                        [pre_model_egress]
                                   |
                       reasoning/answer backend
                                   |
                 outcome + sources + provenance
                                   |
                    optional archive/import write
                                   |
                          [pre_persist]
                                   |
                              Nextcloud
```

---

## 5. Component responsibilities

| Component | Responsibility | Authorizes private document access? |
|---|---|---:|
| Query Rewriter | creates SearchSpec and lightweight analysis fields | No |
| Neo4j | seed/alias/entity context; optional graph retrieval and curation | No |
| Elasticsearch | lexical candidate discovery | No |
| Qdrant | semantic candidate discovery | No |
| Dedup/RRF/Reranker | candidate combination and ordering | No |
| Nextcloud WebDAV ACL | current-user visibility of concrete files | **Yes** |
| Candidate Verifier | document-level relevance and relation binding | No |
| Answer model | answer generation from authorized evidence | No |
| Brave/SearXNG | public URL discovery | N/A |
| Web relevance gate | source relevance after actual fetch | N/A |
| Planned policy/inspection hooks (rc1.2) | optional outbound-query, pre-fetch, post-fetch, pre-persist and remote-model-egress policy/inspection | No |

---

## 6. Internal retrieval path

### 6.1 Normal path

Each retrieval round uses the same interface:

1. A compact Neo4j seed/alias context may be loaded before rewrite.
2. The user question is rewritten into one `SearchSpec`.
3. `elastic_query` is parsed and compiled deterministically into Elasticsearch JSON.
4. `semantic_query` is sent to Qdrant when enabled.
5. Candidate lists are fused and deduplicated.
6. An optional reranker may reorder the bounded candidate set.
7. Live Nextcloud ACL removes unauthorized files.
8. The Candidate Verifier may check direct document support.
9. The answer model receives only selected evidence.

Example:

```text
User:
Find invoices from Example Ltd. from 2025

SearchSpec:
  elastic_query:  +Example +2025 +invoice
  semantic_query: invoices from Example Ltd. from 2025
  entities:       Example Ltd.
  concepts:       invoice
  constraints:    year=2025
```

0.8.6 request budgets come from the selected SunaQ profile. The shipped rc1
profiles use one retrieval round and candidate/answer windows of 10, 30 and 50
documents for Schnell, Gründlich and Tief respectively. See
`models/README.md` for the exact profile budgets and administrator hard caps.

`max_retrieval_rounds: 1` means one rewrite followed by one retrieval run.

If more rounds are configured, a later round may produce a revised SearchSpec from the bounded visible result picture. It still uses the same retrieval pipeline and configured backend policy.

### 6.2 Explicit retrieval directives

Supported specialist/diagnostic directives can override the normal arm selection:

- `/files` — Elasticsearch only, still using structured rewrite;
- `/vector` — Qdrant only;
- `/graph` — explicit graph document-retrieval path where enabled;
- combinations such as `/files /vector`;
- `/elastic` — direct Nextcloud/Elasticsearch full-text mode without rewrite/vector/fusion/reranker.

Neo4j seed/alias expansion is independent from the optional graph document-retrieval arm.

---

## 7. SearchSpec, QueryFrame, EvidenceFrame and RetrievalRecord

### 7.1 SearchSpec

The SearchSpec is the executable retrieval description.

Typical fields include:

```json
{
  "elastic_query": "+Example +2025 +invoice",
  "semantic_query": "invoices from Example Ltd. in 2025",
  "entities": ["Example Ltd."],
  "concepts": ["invoice"],
  "constraints": [{"kind": "year", "value": "2025"}],
  "verification_requirements": [
    "The document itself is an invoice from Example Ltd."
  ]
}
```

### 7.2 QueryFrame

For verifier/provenance/Findings compatibility, analysis fields can be represented as a QueryFrame.

A QueryFrame is a **search hypothesis**, not evidence and not a fact.

### 7.3 EvidenceFrame

The Candidate Verifier derives an EvidenceFrame only from the candidate document.

Important relation bindings include:

- `direct` — the document itself supports the requested relationship/object;
- `reference_only` — the requested subject is only mentioned or referenced;
- `contradicted` — the document supports a materially different relationship;
- `unclear` — the document does not allow a reliable decision.

### 7.4 RetrievalRecord

Optional retrieval/evidence audit records:

```yaml
retrieval_record:
  enabled: true
  directory: "runtime/retrieval-records"
```

They store structured query, SearchSpec/QueryFrame, document references, verifier metadata and EvidenceFrames, but not complete document bodies.

A QueryFrame must not be imported automatically as a graph fact.

---

## 8. Reranking and deduplication

Supported reranker modes include a local cross-encoder and an external TEI endpoint.

Local example:

```yaml
reranker:
  backend: local
  model: BAAI/bge-reranker-v2-m3
  device: cpu
```

External TEI example:

```yaml
reranker:
  backend: tei
  tei_url: "http://127.0.0.1:8081"
  fallback_backend: none
```

Super-Light can operate without a reranker. Deduplication remains a separate preprocessing step.

---

## 9. Live ACL and bounded post-filtering

The current normal order is:

```text
retrieve -> fuse/deduplicate -> optional rerank -> bounded candidates
        -> live Nextcloud ACL -> verifier/answer
```

Example:

```text
Candidates:      [A, B, C, D, E]
Ranking:         [C, A, E, B, D]
ACL authorized:  [C, E]
Answer evidence: [C, E]
```

The middleware does not then fetch F, G or H merely to restore the original count.

This has two operational consequences:

- no replicated ACL shadow is required in Elasticsearch/Qdrant/Neo4j;
- users with narrow permissions may receive fewer results than a user with broader rights.

A possible future optimization is a fixed-size ACL candidate pool before an expensive reranker. Such a pool would remain bounded and non-adaptive.

### 9.1 WebDAV request cost

Live ACL checks are batched. With the default:

```yaml
acl:
  batch_size: 100
```

up to 100 unique file IDs are checked in one authenticated WebDAV `SEARCH` request.

The practical cost should be measured on the actual Nextcloud deployment. It is not a per-document HTTP request loop.

---

## 10. Graph and Graph-Lite

Neo4j is used for identity/alias context, provenance and optional graph-assisted retrieval and curation.

It is not an authorization store and is not treated as a universal fact database.

### 10.1 CardDAV seeds and provenance

CardDAV contacts can seed known Persons/Organizations. Document processing can add observations and relation evidence while retaining provenance.

### 10.2 Name and alias policies

Entity forms can carry a `resolution_policy`:

- `exclusive` — query use plus hard ingestion identity resolution;
- `contextual` — query/candidate use but no hard ingestion resolution;
- `search_only` — query expansion only;
- `document_only` — not exposed as a global resolver/search form.

This allows uncertain OCR/name variants to be useful without automatically treating them as canonical identity.

### 10.3 Shared retrieval knowledge

Curated names and aliases can be reused across users.

This reuse is retrieval knowledge, not access to the source document that originally motivated the alias. Document evidence still requires current-user live ACL.

### 10.4 Graph curation operations

Administrative tooling supports, among other operations:

- merge proposals;
- manual merge with preview;
- persistent `NOT_SAME_AS`;
- alias and policy maintenance;
- name correction;
- reassignment of observations or CardDAV records;
- provenance and import-run inspection.

### 10.5 Indirect relations

A document-grounded chain:

```text
A -> C -> B
```

may be represented as an indirect connection only when both hops have supporting provenance.

It must not be transformed into a direct `A <-> B` relation.

### 10.6 Research Findings

Research Findings are positive, document-bound verifier observations.

A deterministic `finding_id` combines provenance, supporting document and canonical QueryFrame so repeated equivalent research can coalesce.

Findings are an **optional learning layer**. The normal SunaQ pipeline does not depend on their curation.

A typical enrichment loop is:

```text
query
 -> SearchSpec
 -> retrieval
 -> live ACL
 -> verifier
 -> answer
 -> optional ResearchFinding
 -> optional curator decisions
 -> improved shared graph knowledge
```

A well-curated graph can improve entity resolution and may support searches across recognized relationships. Leaving Findings uncurated or disabling `research_findings.enabled` does not disable normal retrieval or answering.

Direct `/use` selection remains a retrieval bypass: the user chooses already-resolved documents and the answer path does not rerank or discard them. When Research Findings are enabled, SunaQ now runs a **side pipeline** only for enrichment: it derives a structured QueryFrame from the user's `/use` task, verifies copies of the live-ACL-authorized selected documents, and persists only verifier `match` + `direct` observations. Verifier output cannot remove or reorder the explicit answer context.

Curated claims remain document-grounded `RelationObservation` records. The current release does not automatically promote them into global fact edges or query-expansion relations.

User/query provenance is represented through a per-request `ResearchRun` while the Finding remains shared:

```text
CanonicalUser --PERFORMED--> ResearchRun --PRODUCED--> ResearchFinding
                                                        |
                                                  SUPPORTED_BY
                                                        |
                                                     Document
```

The ResearchRun stores the original user query and retrieval/runtime provenance. Equivalent runs may therefore converge on the same globally curated Finding. Run-level dismissal controls the work queue only; it does not alter the shared Finding.

Both the administrator user-context view and optional end-user self-service re-check the supporting document through live Nextcloud ACL before exposing Finding evidence. End-user curation uses a separate, short-lived Nextcloud Login Flow session rather than a persistent SunaQ password or the ordinary provider credential. Self-service is disabled by default and can be gated per canonical user.

---

## 11. Public Web research

### 11.1 Discovery

Supported discovery backends include:

- Brave Search API;
- externally operated SearXNG.

Search snippets are discovery metadata and are not answer evidence.

### 11.2 Evidence pipeline

Current rc1.2 execution is shown together with the policy-hook insertion points
points:

```text
derived/public search query
   -> [outbound_query]
   -> Search provider
   -> URL list
   -> [pre_fetch]
   -> HTTP/Playwright fetch
   -> [post_fetch]
   -> text/PDF extraction
   -> passage selection
   -> relevance review
   -> bounded Web evidence
   -> [pre_model_egress when the target model is remote]
   -> answer citations [W1], [W2], ...
```

The bracketed policy/inspection stages are real rc1.1 hook points, but the
shipped evaluator is ALLOW-only and therefore not a filtering guarantee.

### 11.3 Explicit, mixed and fallback use

Web research can be:

- explicit Web-only: `/web ...`;
- part of a mixed workflow;
- used after selected internal evidence;
- allowed as a controlled fallback when the trusted client sets `X-RAG-Web-Allowed: true` and per-user Web research is enabled.

A conservative Web gate decides whether automatic Web use is appropriate.

### 11.4 Web-after egress boundary

When Web queries are derived from private internal evidence, the query itself becomes data sent to an external search provider.

The Web-after prompt therefore treats internal evidence as untrusted input and avoids transferring secret-like strings, e-mail addresses, API keys, tokens, internal identifiers or unusual verbatim text unless the user explicitly asks to search for that exact value.

### 11.5 Web archive

Selected Web research can be archived through the user's Nextcloud WebDAV credential:

```text
<archive-root>/YYYY-MM/DD-HHMMSS-xxxx/
    recherche.md
    fetch-log.jsonl
    01-source.txt
    .01-source.metadata.json
    01-source.pdf
    01-source.html       # optional
    ...
```

Archive roots are excluded from ordinary internal retrieval so archived public material does not later appear as an independent private source.

Playwright rendering is optional and produces a readable research snapshot, not a complete WARC/WACZ forensic capture.

Web-archive writes pass the rc1.1 `pre_persist` hook; rc1.2 should attach configurable policy adapters
after content inspection and before SunaQ writes the archive artifact into
Nextcloud. This allows a deployment to attach malware/content/DLP policy without
coupling the archive implementation to one scanner.

---

## 12. Privacy and processing boundaries

### 12.1 Local embeddings

With local embedding infrastructure:

- document text can remain inside the administrator-controlled environment;
- Qdrant can remain local;
- only selected authorized evidence needs to be transmitted to a remote answer/verifier model when remote roles are configured.

### 12.2 External embeddings

Using an external embedding provider transmits every indexed text chunk to that provider.

For a full index this can approach disclosure of the complete indexable corpus and should be treated as a different trust boundary from selective answer generation.

### 12.3 Role-specific LLM routing

Planner, verifier, evidence-control and answer roles can inherit one default model/backend or use separate model configurations.

Remote roles have explicit document/count/character budgets.

### 12.4 Credential storage

User-bound reversible Nextcloud and IMAP credentials, and Login Flow poll tokens, are encrypted with AES-256-GCM in the credential store.

The master key remains outside SQLite.

This protects stored database material from casual/plaintext disclosure but is not intended to protect secrets from `root` or a fully compromised middleware process.

### 12.5 Untrusted content and prompt injection

Documents, mail, Web pages and saved chats may contain text phrased as model
instructions. SunaQ treats that text as **untrusted evidence**, not as middleware
policy.

The important distinction is between *semantic influence* and *control-plane
authority*:

| Stage | Can untrusted document text influence it? | Security consequence |
|---|---|---|
| First-round query rewrite in shipped profiles | **No** — it runs before corpus evidence is supplied | no corpus-driven planner injection |
| Elasticsearch/Qdrant request construction | Indirectly through validated SearchSpec terms | retrieval quality/recall; no raw DSL/tool execution |
| ACL metadata prefilter | **No** for identity/groups | optimization only; live ACL still final |
| Live Nextcloud ACL | **No** | deterministic authorization boundary |
| Candidate Verifier | **Yes**, after live ACL | a visible malicious document may be misclassified |
| Answer model | **Yes**, after live ACL | misleading/incorrect answer or social-engineering text |
| Later retrieval rounds, when enabled | **Yes** — ACL-visible snippets can feed the next rewrite | bounded search steering; final ACL still applies |
| Research Findings / graph extraction, when enabled | **Yes** | persistent knowledge pollution is possible |
| Web-after queries, when enabled | **Yes** | possible query-egress manipulation; separate policy boundary |

The shipped 0.8.6-rc1.2 profiles use one retrieval round. Corpus text therefore
does not feed back into their query rewriter during the same request. A future
multi-round profile intentionally changes that assumption and must be evaluated
as a larger prompt-injection surface.

The verifier and answer model still receive authorized document text. Prompt
injection can therefore cause a wrong relevance decision or answer even in the
minimal profile. What it cannot do through the current RAG contract is grant
itself access to another Nextcloud file, change the authenticated identity,
override the live ACL, emit raw Elasticsearch DSL for execution, or invoke an
arbitrary side-effecting tool.

There are two bounded persistence cases worth separating from retrieval itself:

1. the bundled UI may save the generated conversation as a new Nextcloud chat
   file, but only while the provider advertises `chat_archive.enabled=true`.
   The file has its own Nextcloud ACL/lifecycle; when the capability is disabled
   the app does not write new archives and `/chatarchive` retrieval is rejected.
   Managed Markdown deletion also removes its hidden metadata sidecar, with
   list/load orphan pruning as a repair fallback;
2. optional Findings/Graph enrichment can persist model-derived observations,
   which is why those capabilities are disabled in the fresh-install baseline.

Accordingly, the main residual risks in layer A are evidence integrity, answer
quality and user-facing social engineering. Persistent graph/finding pollution,
public-Web query egress and broader untrusted-content ingestion belong to the
explicitly enabled ERG capability layer.

See `THREAT-MODEL.md` for the security analysis.

---

## 13. Mail integration

Mail synchronization is configured per canonical Nextcloud user.

Configured IMAP mailbox names are recursive roots. Selectable descendants are discovered through IMAP `LIST`, and the hierarchy is mirrored into Nextcloud.

The rc1.1 inspection boundary for imported mail is:

```text
IMAP message / attachment
        |
   receive bytes/text
        |
   [post_fetch inspect]
        |
 normalize / extract metadata
        |
    [pre_persist]
        |
  Nextcloud Mail archive
```

For attachments, a deployment may choose to inspect the received binary before
parser/OCR/rendering where the configured adapter supports that workflow. The shipped evaluator is ALLOW-only, so these hooks are not generic malware
scanning in rc1.1.

New messages use a directory-per-message layout:

```text
<target>/<account>/<mailbox-hierarchy>/<YYYY>/<MM>/
  <timestamp>_<uid>_<subject>/
    mail.txt
    .mailmeta.json
    a01_<attachment>
    ...
    message.eml          # optional
```

`mail.txt` is the indexable normalized representation. `.mailmeta.json` carries deterministic metadata for mail/thread processing.

Legacy flat archives remain readable and are not moved automatically.

---

## 14. Frontends and provider boundary

SunaQ Recherche is the bundled Nextcloud-native frontend.

OpenWebUI or another OpenAI-compatible integration can use the same provider when registered as a Trusted Client.

The frontend boundary is intentionally independent from retrieval implementation:

```text
frontend/integration
      |
trusted client key
      |
OpenAI-compatible provider
      |
retrieval/orchestration
```

A Trusted Client key authenticates the integration server, not the human user. The external user identifier supplied by that integration is scoped as:

```text
client_id::external_user_id
```

and selects the corresponding server-side Nextcloud credential binding.

Provider keys therefore belong only on trusted integration servers. Externally reachable provider endpoints should be restricted with network policy, reverse-proxy allowlists, mTLS or equivalent controls where appropriate.

This boundary also allows SunaQ to be composed with other local reasoning systems, agents or research tools when the administrator explicitly permits it.

---

## 15. Process model and latency

The normal SunaQ request path is served by long-running API/provider processes and is independent from Nextcloud's background-job scheduler.

Total latency depends on:

- query rewrite;
- Elasticsearch/Qdrant response time;
- hydration/fusion/dedup/reranking;
- live WebDAV ACL;
- verifier;
- answer model;
- public Web fetch/relevance stages when enabled.

One observed development run with GPT-5.6 Luna was approximately:

```text
Query Rewrite            ~4 s
Retrieval/Rerank/ACL     ~9-10 s
Verifier                 ~5 s
Answer                   ~3 s
Total                    ~24 s
```

This is an observation from one environment, not a performance guarantee.

Latency measurements should separate live ACL time from the rest of retrieval. Because ACL uses batched WebDAV SEARCH, benchmarking 10/50/100 candidates on the target Nextcloud instance is more useful than assuming linear per-document cost.

---

## 16. Background workers and deployment profiles

Capabilities and workers are configured separately.

Example:

```yaml
qdrant:
  enabled: true
sync_worker:
  enabled: false

mail:
  enabled: true
  worker:
    enabled: false

graph_queue:
  enabled: true
  auto_enqueue_cited_documents: false
  worker:
    enabled: false
```

This permits a component to be configured without automatically starting periodic CPU-intensive or privacy-sensitive processing.

The current release line tests:

- `standard + native`;
- `super-light + dockerized`.

These are combinations of two independent axes:

- **functional profile** — enabled retrieval/graph/UI capabilities;
- **deployment mode** — native or containerized service operation.

Super-Light uses the same middleware core and can remain Elasticsearch-centric without Qdrant or a local reranker.

---

## 17. Error and uncertainty handling

The middleware uses explicit states for incomplete or uncertain processing:

- invalid/incomplete structured model output is retried or reported as failure;
- ACL denial removes evidence and does not trigger adaptive refill;
- indirect graph chains remain labeled indirect;
- search-engine snippets are not evidence;
- unfetchable Web pages are not evidence;
- mere mention can be classified `reference_only`;
- missing evidence is phrased as absence from the retrieved/available sources rather than global nonexistence.

---

## 18. Backup and recovery boundary

RC5 adds recovery for **SunaQ-owned operational state**, not a transaction across the complete Nextcloud/SunaQ estate. The console recovery set groups state that must remain coherent: configuration/runtime state, SunaQ SQLite databases, `runtime/users.sqlite` with its matching credential master key, local private CA/TLS/operator files below the installation prefix and bundled Neo4j where selected.

The boundary is intentional:

- **Nextcloud** remains the authoritative source/document/ACL platform and uses its own backup process;
- **Elasticsearch/FullTextSearch** remains source-platform derived state and is restored/rebuilt separately;
- **Qdrant** is rebuildable derived state and is excluded from the first recovery format;
- **external Neo4j** is operator-managed and is not copied into the SunaQ recovery set;
- **bundled Neo4j** is included because Graph-Lite/manual curation may contain non-reconstructible human work;
- **credential SQLite + master key** are recovered as one unit because separating versions can make encrypted secrets unusable.

Create/restore operations run behind the explicit maintenance gate. Restore verifies checksums, SQLite integrity and credential decryption before replacing state and deliberately leaves the service in maintenance mode afterwards. Operational command sequences are documented in `BETA-OPERATIONS.md`; cross-system restore ordering and lifecycle semantics are in `DATA-LIFECYCLE.md`.

This is recovery, not a unified purge/rollback transaction: SunaQ does not atomically restore or delete Nextcloud, Elasticsearch, Qdrant and all derived stores together.

---

## 19. Current release-candidate boundaries

`0.8.6-rc1` is the current release-candidate baseline.

Known limits include:

- only `standard+native` and `super-light+dockerized` are released/tested deployment combinations;
- no unified cross-store purge/restore transaction;
- no complete prompt-injection defense;
- Web snapshots are research artifacts, not full WARC/WACZ captures;
- ResearchRun provenance and user-scoped live-ACL curation are implemented, but curation still writes shared Graph-Lite knowledge and should therefore be granted deliberately;
- no automatic conversion of QueryFrames or Finding claims into global facts.

See `KNOWN-LIMITATIONS.md`, `THREAT-MODEL.md`, `DATA-LIFECYCLE.md` and `GRAPHLIGHT-FINDINGS.md` for details.

---

## 20. Responsibility separation

The current middleware separates four responsibilities:

1. **candidate retrieval** — Elasticsearch, optional Qdrant, optional Graph/Web paths;
2. **private-document authorization** — Nextcloud live ACL;
3. **evidence review** — Candidate Verifier and Web relevance checks;
4. **reasoning/outcome generation** — the configured downstream answer backend.

This separation defines component boundaries and failure handling. It also permits individual retrieval/model components to be replaced without changing the live authorization rule.

It is an architectural choice with explicit trade-offs rather than a claim that the same decomposition is required for every RAG deployment.
