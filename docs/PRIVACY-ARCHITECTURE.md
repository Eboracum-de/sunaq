# Privacy architecture and trust boundary

**Reference:** `0.8.7-rc1` release candidate

The middleware deliberately separates access to the complete private corpus from
processing of already selected evidence. Privacy is therefore not defined as
"every model must run locally". The primary invariant is that the complete
Nextcloud/Elasticsearch corpus, vector index and ACL state remain inside the
operator-controlled retrieval plane.

## Private retrieval plane

The following operations are intended to remain local/private:

1. Nextcloud FullTextSearch / Elasticsearch access;
2. document chunking and embedding;
3. Qdrant vector storage and search;
4. optional Neo4j retrieval over an existing graph;
5. Cross-Encoder/TEI reranking when a local reranker is configured;
6. live Nextcloud ACL authorization.

Only document content that survived the configured local retrieval and live
authorization pipeline is eligible for transmission to a remote verifier/answer
role. Query-rewrite roles may also receive the user query and bounded shared
identity/alias context according to configuration; that context is retrieval
knowledge, not document evidence.

The same boundary applies to portable retrieval provenance. Backend queries may be recorded, but ResearchRun candidate/verifier provenance for private documents starts after live ACL. Raw pre-ACL hit counts, denied candidate identities, backend ranks and scores remain operator diagnostics rather than user-visible ResearchRun metadata.

```text
Nextcloud / Elasticsearch
          |
          v
 local embedding -> Qdrant
          |
          v
 hybrid / graph retrieval
          |
          v
    local reranker
          |
          v
 Nextcloud Live ACL
          |
---------- trust boundary ----------
          |
          v
 candidate verifier / evidence control / answer LLM
```

## Role-specific LLM routing

The current reference can route four LLM roles independently:

- `planner`: Query Rewrite/SearchSpec generation and optional bounded round control;
- `verifier`: semantic verification of ACL-authorized document candidates;
- `evidence`: evidence decisions and citation-integrity control;
- `answer`: final answer generation and streaming.

Unset role settings inherit the canonical `LLM_*` backend for compatibility. Each role can override backend, URL, model, API key, TLS verification
and trust scope through `<ROLE>_LLM_*` variables.

The `evidence` role remains available even though the **Evidence Control** stage is disabled by default. For packaged SunaQ models, opt in per selected model with `models/<profile>/profile.yaml: evidence_control.mode: review`; the evidence model then runs only after live ACL and Candidate Verification. The Candidate Verifier is a separate stage and is unaffected by this switch. The shipped Standard (Schnell) profile may additionally consume a preserved legacy `config.yaml: evidence_control` value through its explicit `legacy_config_overlay` upgrade bridge. `EVIDENCE_DECISION_MODE` is only the legacy/default fallback when no effective packaged-profile value is present.

A practical `private-retrieval` deployment keeps embedding, Qdrant, reranking
and ACL local while using a capable remote model for verifier/evidence/answer.
A `strict-local` deployment points all roles at a local provider instead.

Within the three-tier architecture, **SRC does not require zero model egress**.
Remote model roles can remain compatible with SRC when the administrator
deliberately accepts the disclosure and configured evidence caps bound what is
transmitted. The retrieval/data-plane distinction is instead: SRC has no
SunaQ-owned secondary retrieval state; ERG Internal adds local derived
retrieval/resolution state; ERG Full additionally permits external
research/resolution boundaries. Model-processing and embedding egress remain
orthogonal policy decisions.

## Remote evidence budgets

When a role endpoint is classified as remote, the provider applies hard evidence caps in
addition to the feature-specific limits:

- maximum characters per answer document: `REMOTE_LLM_MAX_CHARS_PER_DOCUMENT`;
- maximum total answer/evidence characters per call: `REMOTE_LLM_MAX_TOTAL_CHARS`;
- maximum verifier candidates: `REMOTE_VERIFIER_MAX_CANDIDATES`;
- maximum verifier characters per candidate: `REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT`;
- maximum answer documents: `REMOTE_ANSWER_MAX_DOCUMENTS`.

Loopback and private/link-local IP endpoints are classified as local by default;
public IPs and ordinary public DNS names are remote. `*_LLM_SCOPE=local|remote`
can override this classification when the administrator knows the actual trust
boundary.

These limits constrain transmitted evidence, not the user question itself. A
remote planner may receive the user's query and bounded conversation context.

## Graph extraction is different

Graph extraction can inspect much larger portions of a document than normal
answer evidence. The graph entity/relation extractors therefore remain a
separate administrative trust decision. Their backend is configured under
`graph_entity_discovery` / `graph_relation_discovery` (or the corresponding
`GRAPH_*` environment variables).

Automatic graph processing is **off by default** in the current reference configuration even when Neo4j and the
Graph Queue are available:

```yaml
graph_queue:
  enabled: true
  auto_enqueue_cited_documents: false
  worker:
    enabled: false
```

This prevents answer-cited documents from being silently sent to a remote graph
extractor. Administrators may still enqueue/process graph jobs deliberately.

## Public Web Research

Public Web Research is separate from the private corpus. The native SunaQ Web
path searches and fetches sources itself; only actually fetched content can
become evidence. Selected sources can be archived to the current user's
Nextcloud via WebDAV. Archive TLS verification has its own
`web.yaml: archive.verify_tls` setting because the archive write is a separate
HTTP client path.

ERG may alternatively delegate a **Web-only** request to a configured hosted
Research Provider such as OpenAI. In this mode the external provider owns Web
search and answer synthesis. The first delegated-research contract has no field
for Nextcloud document evidence, retrieved document text or raw chat history.
Only the effective public-Web query is transmitted; for a contextual follow-up
that query may be the standalone query produced by SunaQ's normal follow-up
rewrite. Both `outbound_query` and `pre_model_egress` policy hooks run before
the request leaves SunaQ.

**Important current limitation (October 2026):** The standalone-query rewrite
may consume prior *assistant messages*, including earlier answers grounded in
private Nextcloud documents. The delegated research adapter does not receive
the raw chat history, but the rewritten query may nevertheless incorporate
private details from those answers. Moreover, if the configured follow-up
rewrite LLM is remote, that rewrite step itself can transmit the bounded chat
history to an external LLM independently of the research adapter.

0.8.7-rc1 has a deterministic, class-aware outbound egress policy. Public research
queries use the `research_query` class and default to `guard`: known credential
formats and administrator-configured identifier regexes are redacted before the
query leaves SunaQ. This applies to Brave/SearX-style Web search, delegated
research queries and external resolver references. Resolver-local ranking
context remains local and is not redacted merely because the selected adapter is
external.

Remote planner/verifier/evidence/answer/follow-up calls use the separate
`model_processing` class and default to `audit`, while external embeddings use
`embedding` and default to `allow`. Administrators may change either class to
`guard` or `block`, but SunaQ does not destructively redact those payloads by
default because doing so can change verification, answer or vector semantics.
This policy reduces accidental disclosure risk; it does **not** claim to detect
all sensitive business facts or provide a complete DLP guarantee.

A delegated Web-only request must not be confused with a future delegated hybrid
mode. Sending internal Nextcloud evidence to a hosted Research Provider would
cross an additional trust boundary and therefore requires an explicit
data-egress grant and contract rather than reusing the public-only adapter.

The normalized delegated provenance (provider/model, provider search queries,
citations, consulted URLs and bounded usage metadata) may be stored in the
current Nextcloud ResearchRun. Raw provider responses are not persisted by this
path.

## Privacy/policy inspection hooks

The generic hook points remain the stable privacy/trust-boundary interface. 0.8.7-rc1
runs the built-in egress policy through the outbound/model hook stages before
any optional site-specific evaluator. The same hooks still cover untrusted
content entering SunaQ and persistence boundaries:

- `outbound_query` before a query is sent to Brave, SearXNG or another external
  search provider;
- `pre_fetch` before an HTTP/Playwright URL fetch;
- `post_fetch` after Web/mail content or attachments are received and before
  further processing where practical;
- `pre_persist` before SunaQ writes imported or generated artifacts to
  Nextcloud;
- `pre_model_egress` before evidence/context is sent to a remote LLM or
  embedding service.

The interface is intentionally adapter-neutral. `config.yaml:egress` controls
the built-in classes with `allow`, `audit`, `guard` or `block`; only
`research_query` is guarded by default. A deployment may additionally attach
malware scanning, URL/domain policy, ICAP, YARA, DLP/redaction or custom
inspection services. Evaluator failures propagate rather than being bypassed, so
a configured required adapter can fail closed.

The deterministic guard is not semantic confidentiality classification. Its
presence must not be represented as a general malware, DLP or data-classification
guarantee.

## Shared retrieval knowledge versus protected evidence

SunaQ intentionally permits organization-wide reuse of curated identity and alias
knowledge. If one user's work establishes a useful name form, another user's
query may benefit from that expansion. This does not transfer the source
document's read permission: any concrete document content still has to pass the
current user's live Nextcloud ACL before it is used as answer evidence.

The distinction is important for Graph-Lite: identity vocabulary and curation
state can be collaborative, while source passages, EvidenceFrames and supporting
documents remain provenance-bearing and authorization-sensitive.

## Untrusted content

Fresh installations keep Web Research, mail ingestion, chat-archive evidence
and Research-Finding persistence disabled. This makes ordinary ACL-authorized
Nextcloud documents the default evidence boundary.

Mail, web pages, archived web content and saved chats can be authored or influenced
by third parties. Their text is data, not an instruction channel to the model.
Structured verifier/Graph schemas, bounded context and provenance-specific prompts
reduce the impact of adversarial text, but SunaQ does not claim a complete prompt-
injection defense. Operators should treat automatic extraction from hostile inbound
content as lower-trust until reviewed.

## Operational principle

A deployment should make the trust decision explicit rather than infer it from
where a model happens to run. Provider health reports the effective backend,
model and local/remote scope for Query-Rewriter (`planner`), verifier, evidence and answer roles.

## Research-Finding provenance

`research_findings` persists only positive, verifier-supported document matches; rejected or uncertain document/query pairs are not stored as Findings. Shared `ResearchFinding` nodes deliberately coalesce equivalent curation work, while per-user provenance is represented by the concrete request path:

```text
CanonicalUser -> ResearchRun -> ResearchFinding -> Document
```

The ResearchRun retains the user/query/runtime provenance. Before Findings evidence is shown in SunaQ Admin or self-service curation, the supporting Document is re-authorized through the selected/current user's live Nextcloud credential. SunaQ Admin is a trusted operator surface: its administrator can choose another configured canonical-user context and is therefore not isolated by the administrator's own Nextcloud ACL. Self-service curation is separately gated and currently exposes only the authenticated user's own ResearchRuns.

Shared curation remains intentional organization-level retrieval knowledge. A decision made by one authorized curator can be reused when another authorized user later reaches the same Finding, but it never grants access to the supporting document.

RC5 additionally treats a successful Finding live-ACL denial as a narrow lifecycle signal for **uncurated user provenance**. In SunaQ Admin and self-service curation, a definitively denied numeric Nextcloud file can cause that user's `ResearchRun-[:PRODUCED]->ResearchFinding` edge to be removed. The shared Finding is garbage-collected only if it remains uncurated and no user ResearchRun references it. Curated Findings and their document-grounded observations/claims remain stored and continue to rely on live ACL for per-user evidence visibility. Operational ACL failures or ambiguous/non-Nextcloud identifiers never trigger deletion.

## Data protection and lifecycle

Technical data minimization does not by itself define legal roles or retention duties.
For deployments using remote LLM/search/hosting providers, the operator must assess the
required contractual/organizational controls for the actual data and jurisdiction.
SunaQ's role routing and evidence caps can support such a policy but do not replace it.

Deletion, archive-retention, backup/restore and key-rotation limits are documented in
`DATA-LIFECYCLE.md`; the adversary/boundary model is in `THREAT-MODEL.md`.
