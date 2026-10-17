# Threat model and security boundaries

**Reference:** `0.8.7-rc1` development line

This document states the security assumptions of SunaQ as they exist in the current release-candidate line. It separates **retrieval knowledge**, **authorization**, and **answer evidence** because these have deliberately different sharing rules.

It is an engineering threat model, not a certification or a claim that every deployment is secure by construction.

## 1. Security invariants

1. **Nextcloud remains the authorization authority for document evidence.** Elasticsearch, Qdrant and Neo4j may propose candidates, but they do not grant access to a Nextcloud file.
2. **A document must pass the live Nextcloud ACL before its content may become verifier/answer evidence for the current user.**
3. **ACL denial does not trigger adaptive backfill.** The current normal path ranks a bounded candidate set first, applies live ACL afterwards, removes denied results and returns fewer results if necessary.
4. **Shared retrieval knowledge is not the same thing as shared evidence.** Names, aliases and curated identity work may improve retrieval for other users; this does not grant access to the source document that originally motivated that knowledge.
5. **Graph observations remain provenance-bearing.** Document-grounded claims are not silently promoted to unqualified global truth edges.
6. **ACL uncertainty must never become destructive.** Lazy Finding cleanup is permitted only after a successful live-ACL request definitively denies a numeric Nextcloud file; backend, network, TLS, credential or configuration failures do not authorize deletion.
7. **Portable retrieval provenance starts after live ACL for private candidates.** ResearchRun execution records must not expose raw pre-ACL hit counts, denied candidate identities, backend ranks or scores.
8. **Remote model roles are explicit trust-boundary choices.** Administrators decide which roles may send which bounded context to a remote provider.

## 2. Assets and trust zones

| Asset / component | Security role |
|---|---|
| Nextcloud files and shares | source content and current document authorization |
| Nextcloud WebDAV | live authorization/existence check |
| Elasticsearch | candidate discovery; not an authorization store |
| Qdrant | optional semantic candidate discovery; not an authorization store |
| Neo4j | identity/alias context, graph retrieval signals and curation provenance; not an authorization store |
| `runtime/users.sqlite` + credential master key | user bindings and reversible service credentials |
| LLM providers | potentially external processors of the bounded prompts/evidence sent to their configured roles |
| Mail/Web/Chat archives | optional Nextcloud-backed content sources with their own file IDs and ACLs |

## 3. Adversaries and failure modes considered

The design assumes that a deployment may contain:

- users who legitimately access the same RAG service but have different Nextcloud permissions;
- stale or over-broad search/index data;
- malformed or misleading documents;
- incoming mail or public web pages authored by an untrusted third party;
- a remote LLM provider that should receive no more data than its configured role requires;
- administrator mistakes, including accidental use of diagnostic modes.

A fully compromised host/root account, a compromised Nextcloud administrator, or a malicious model provider receiving data explicitly routed to it are outside the guarantees of the middleware itself.

A registered provider-client key is also a security boundary. It authenticates a **trusted integration/frontend server**, not an end user. Within that client scope, the frontend-supplied external user ID selects the corresponding server-side Nextcloud credential binding. A party that obtains a trusted client key and can reach the provider may therefore impersonate users already bound inside that client scope. Client keys must stay server-side, should be rotated if exposed, and may be additionally protected with reverse-proxy source-network allowlists or equivalent controls.

## 4. Shared alias and identity knowledge

SunaQ deliberately treats a curated/shared identity lexicon differently from document evidence.

Example:

```text
User B establishes:
"NewCo" -> "NexCo Ltd."

User A later searches:
"NewCo invoice"

The shared alias may expand A's retrieval.
Only documents that pass A's live Nextcloud ACL may become A's evidence.
```

This reuse is intentional. Without it, every user would repeatedly perform the same entity/alias curation and Graph-Lite would provide little organization-wide benefit.

The boundary is:

- **allowed as shared retrieval knowledge:** curated names/aliases, conservative deterministic name variants and other identity-resolution aids;
- **not implied by that knowledge:** access to the source document, source passage, query history or other protected provenance;
- **answer evidence:** still requires an ACL-authorized supporting document for the current user.

### Current limitation: diagnostic detail

The current search API can return entity-resolution diagnostics such as matched forms, candidate identities and search forms. Those fields are useful during development but are a broader information surface than the final document evidence. They must not be treated as proof of a document fact. A future hardening step should minimize or gate user-visible identity diagnostics while preserving shared alias reuse.

### Integrity risk

Shared retrieval knowledge can affect other users' retrieval even when it was first learned from one user's material. Therefore automatic document discovery must remain conservative. Manual curation, deterministic transformations and provenance are stronger inputs than speculative model-generated aliases. A poisoned or badly extracted alias is primarily an **integrity/retrieval-quality** risk even when the source document itself remains ACL-protected.

## 5. Findings and shared curation

A `ResearchFinding` is document-bound and globally deduplicated by supporting document plus canonical query-frame semantics. User provenance is recorded through `ResearchRun`:

```text
CanonicalUser --PERFORMED--> ResearchRun --PRODUCED--> ResearchFinding
                                                        |
                                                  SUPPORTED_BY
                                                        |
                                                     Document
```

This separates three security concepts:

- **observation/work provenance** belongs to the user-specific ResearchRun;
- **document visibility** remains governed by current Nextcloud live ACL;
- **curation state** is shared Graph-Lite knowledge.

Neither a ResearchRun nor a previous successful observation grants continued access to the source. Before the Admin user-context view or end-user self-service exposes a Finding, the supporting Document is checked again with the selected/current user's Nextcloud credential. Unauthorized Findings are omitted from lists and counts rather than rendered as inaccessible placeholders.

For still-uncurated Findings, RC5 can use that same **successful** ACL denial as a lazy cleanup signal. Only the denied user's `PRODUCED` provenance is removed; a shared Finding is deleted only when it is uncurated and no ResearchRun for any user still references it. Finding-level curator state/suppression, `CURATED_ENTITY` mappings and any Finding-derived RelationObservation prevent this lazy deletion. ACL errors or unavailable credentials remain fail-closed for visibility but non-destructive for stored Graph state.

SunaQ Admin is a trusted operator surface. Its Basic-Auth administrator is not mapped to a personal Nextcloud ACL; instead the administrator explicitly selects a canonical-user context, and Evidence is authorized with that selected user's stored Nextcloud credential. This prevents cross-context leakage inside a selected view, but it is **not** a tenant-isolation guarantee against the SunaQ administrator, who can deliberately switch to another configured user. Ordinary users must use normal research frontends or the separately gated self-service curation surface.

A shared curation decision can affect later retrieval/entity resolution for other users. Curation is therefore a write privilege on shared retrieval knowledge, distinct from ordinary research permission. Self-service is disabled by default and can be enabled per canonical user. Curator identity is stored with shared decisions for audit provenance.

### Ephemeral self-service credential

The optional `/curation/` surface does not create a SunaQ password and does not persist its Nextcloud app password in the ordinary provider credential namespace. A Nextcloud Login Flow creates an encrypted temporary `curation_sessions` credential with a hard absolute expiry (default two hours).

The browser receives only a random session token; the database stores its hash. State-changing operations require a session-bound CSRF token. The cookie is HttpOnly, Secure, SameSite=Strict and scoped to the curation path.

Every request rejects expired/inactive sessions and re-checks the canonical-user and curation-enable flags. Logout/expiry marks the local session unusable before attempting Nextcloud app-password revocation. Failed revocations remain `revocation_pending` and cannot authenticate. API startup invalidates all surviving curation sessions and retries their Nextcloud revocation.

This startup cleanup is defense in depth, not the primary expiry mechanism: the request path enforces the absolute expiry even if no cleanup worker runs.

## 6. ACL ordering and no-backfill rule

The current normal path is intentionally:

```text
retrieve (optional ES ACL metadata prefilter)
        -> fuse/deduplicate -> optional rerank -> bounded final candidates
        -> live Nextcloud ACL -> verifier/answer
```

Denied results are removed and lower-ranked results are not adaptively fetched to refill the window. This has two deliberate properties:

- the final authorization decision does not depend on a replicated ACL shadow;
- an ACL denial does not cause a variable number of additional searches/checks that could itself become an inference/timing channel.

The optional Elasticsearch ACL metadata prefilter is **implemented** and can
reduce irrelevant candidate work before later ranking. Its first version uses
the Nextcloud FullTextSearch owner/direct-user/group metadata. It does not yet
evaluate Nextcloud Circles; deployments that depend on Circle-only visibility
should leave the prefilter disabled until Circle support exists.

The prefilter is a retrieval optimization, never an authorization decision.
If the required user/group context is unavailable, SunaQ skips the prefilter and
falls back to the existing unfiltered retrieval path. Stale or incomplete index
metadata may affect recall, but a candidate still must pass the final live
WebDAV authorization before its content can become verifier or answer evidence.

The remaining trade-off is recall: a user with narrow rights may receive only a
few results when lower-ranked authorized candidates fall outside the bounded
window. A fixed-size, predeclared pre-rerank ACL candidate pool remains a
possible future optimization. The design still does not call for adaptive
"keep fetching until N authorized results exist" behavior.

### Post-ACL provenance rule

The same no-leak principle applies to stored diagnostics that travel with a user's ResearchRun. `retrieval-execution.v1.json` may record executed backend queries, but candidate identity and verifier decisions are projected only from the post-live-ACL result set. Ranks are expressed within that authorized set. Raw Elasticsearch/Qdrant hit counts, denied candidate identities, pre-ACL ES/RRF/vector ranks and backend scores are deliberately excluded. Richer pre-ACL diagnostics belong only in protected operator logs.

## 7. Untrusted content and prompt injection

Content is not trustworthy merely because it is indexed.

Particularly untrusted inputs include:

- incoming email;
- live public web pages;
- archived web pages;
- saved chats that may contain quoted external or model-generated text;
- documents supplied by external parties.

A passage such as "ignore previous instructions" is document content, not an instruction to SunaQ.

SunaQ is not a general tool-using agent. Model output is not executed as Elasticsearch JSON DSL, Cypher, SQL or shell code. Elasticsearch and Qdrant are candidate-retrieval backends; the normal LLM rewrite produces backend-neutral structured SearchSpec intent, deterministic middleware compiles backend requests, and live Nextcloud authorization is independent of document instructions. For that reason, indirect prompt injection in ordinary document retrieval is primarily an **evidence-integrity / retrieval-quality risk**, not an arbitrary-code-execution or ACL-bypass path.

The more relevant boundaries are persistence and egress:

- a manipulated document may try to bias verifier or Graph extraction output; such output must remain structured, document-grounded and provenance-bearing rather than becoming an unqualified global fact;
- Web-after workflows may derive public search queries from already authorized internal evidence. This is an intentional external egress boundary and must not be treated like a purely local retrieval step. Search-query derivation should avoid leaking unusual internal identifiers or verbatim secret-like strings unless the user explicitly requested them.

Structured verifier output, Graph schemas/ontology, bounded context and provenance-specific prompts reduce these risks but do not make untrusted content trustworthy.

### Policy and inspection hooks

rc1.1 exposes small, modular policy/inspection hook points at trust-boundary
transitions rather than embedding one mandatory scanner:

- `outbound_query` before a search query is sent to Brave, SearXNG or another
  external search provider;
- `pre_fetch` before HTTP/Playwright fetch, for URL/domain/scheme/network policy
  and future reputation checks;
- `post_fetch` after content arrives but before it is trusted for further
  processing, so downloaded pages/files and mail attachments can be inspected;
- `pre_persist` before SunaQ writes imported or generated content into
  Nextcloud;
- `pre_model_egress` before bounded evidence/context is sent to an external
  model or embedding service.

The intended contract is deliberately small: `ALLOW`, `BLOCK`, `QUARANTINE`
or `MODIFY`, with reason/provenance metadata and optional transformed content.
Potential adapters include malware scanners, ICAP, YARA, DLP/redaction or
site-specific policy services; no particular scanner is implied by the
architecture.

The rc1.1 runtime installs no evaluator, so all stages are no-op `ALLOW`
decisions. Evaluator exceptions are not swallowed, allowing a future required
scanner to fail closed. Concrete adapters and administrator configuration remain
deferred; the hook scaffold itself is not a claim that rc1.1 scans imported
mail, fetched Web content or model egress.

As infrastructure hardening, operators should also apply least privilege independently of prompt handling: use a read-only Elasticsearch account for retrieval where supported, keep Qdrant/Neo4j on trusted networks or loopback unless remote access is required, and use read-only credentials or network policy for retrieval-only services where the selected backend supports them. These are deployment controls rather than application-level prompt defenses.

## 8. Archive scopes

`/mailarchive`, `/webarchive` and `/chatarchive` are separate optional source scopes. Fresh installs expose ordinary documents only; Web Research, mail ingestion and chat-archive evidence require deliberate administrator activation.

### Chat archive

The chat archive is useful as organizational memory, especially in flat hierarchies where prior research would otherwise be difficult to rediscover. It is nevertheless a deliberate duplication boundary: a saved conversation may contain excerpts or summaries derived from another document and is stored as a new Nextcloud file with its own file ID and ACL.

Consequences:

- live ACL still applies to the **chat archive file**;
- revoking/deleting the original source document does not automatically erase information already copied into a saved conversation;
- strict deployments that require original-document revocation to remove every derived conversational copy should leave `/chatarchive` disabled or establish a separate retention/purge process.

This is a configurable product trade-off, not a reason to disable chat archive for every deployment.

The same general principle applies to intentionally archived public web evidence: an archive is a retained copy with its own lifecycle.

## 9. Diagnostic ACL-off mode

`--acl-off` / `acl.enabled=false` is a diagnostic/lab mode and is not appropriate for a shared protected corpus.

The middleware `/health` payload already reports `live_acl.enabled` and the configured identity mode. A future UI hardening step should make an ACL-disabled state visually prominent in human-facing status/admin views so that a technically healthy deployment cannot be mistaken for an authorization-safe one.

## 10. Security tests expected before production use

A production acceptance test should use at least two Nextcloud users with deliberately different rights and verify:

1. a denied file never reaches verifier/answer evidence;
2. stale Elasticsearch/Qdrant entries do not bypass Nextcloud existence/ACL checks;
3. a shared alias can improve retrieval for another user without exposing its protected source document;
4. Graph retrieval cannot turn a protected supporting document into answer evidence;
5. archived chat/mail/web files obey the ACL of their archive file;
6. Finding evidence is not shown to an admin or end-user curator unless the selected/current user produced that Finding and the supporting file currently passes live ACL;
7. a definitive successful ACL denial may self-clean only uncurated provenance for that user, while curated/shared Findings remain intact;
8. ACL/backend/TLS/network/credential failures never trigger Finding deletion;
9. expired or revoked self-service curation sessions fail closed, and an API restart leaves no surviving active curation session;
10. `acl.enabled=false` is clearly detectable operationally.

See also `PRIVACY-ARCHITECTURE.md`, `GRAPHLIGHT-FINDINGS.md`, `KNOWN-LIMITATIONS.md` and `SECURITY.md`.
