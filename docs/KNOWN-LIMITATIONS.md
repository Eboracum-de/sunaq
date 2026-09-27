# Known limitations

**Reference:** `0.8.6-rc1.2`

This file records current limits so that beta expectations match the code. Items
listed here are not necessarily defects; several are deliberate scope boundaries.

## 0.8.6 profile/client limitations

- SunaQ model packages are loaded at API/provider startup. Editing
  `models/<profile>/profile.yaml` or its prompt files requires a process restart.
- Installer reruns preserve existing model-package directories as
  administrator-owned configuration. An upgrade can therefore intentionally keep
  an older Standard/Thorough package; use a fresh installation for clean rc1
  acceptance or explicitly refresh reviewed profile files.
- The bundled SunaQ app loads the authenticated model list when the app starts.
  If an administrator changes model entitlements while the tab is open, reload
  the app to refresh the selector.
- Follow-up suggestions are intentionally transient and are not persisted in the
  chat archive. They may depend on current model entitlements and retrieval state.
- Generic OpenAI-compatible clients do not currently receive SunaQ's pre-answer
  polling status channel. The bundled SunaQ app does. A portable streaming
  `reasoning_content` bridge is deferred because it would require restructuring
  the current SSE orchestration.
- The provider's implicit source default is ordinary documents only. In SunaQ
  Recherche 0.3.4, optional Mail/Webarchive/Chat/Web controls start hidden and
  are shown only after the authenticated capability response confirms the global
  and per-user gates. Explicit Slash-Directives are subject to the same server-side
  policy; they do not bypass an administrator-disabled source.
- Completeness remains bounded. A near-capacity warning means the ranked profile
  window is almost exhausted; it does not claim that additional ACL-visible
  documents are known to exist outside the window.

## Installation and deployment

- Only `standard + native` and `super-light + dockerized` are supported/tested
  deployment mappings in the 0.8.6-rc1.2 candidate.
- Installer/rerun preflight validates the install source, non-empty install
  prefix, CA files and Docker availability before destructive refresh steps, and
  refuses a running existing SunaQ stack. Explicitly supplied Nextcloud and
  Elasticsearch URLs receive a best-effort, non-fatal host-`curl` reachability/TLS
  probe after prerequisites are available. This is an early typo/connectivity
  diagnostic only and cannot prove that an authenticated endpoint, model backend
  or later runtime path will remain usable; use the smoke/acceptance checks and
  archive the generated `install/last-install-command.sh` for reproducible reruns.
- Super-Light intentionally relies on external Nextcloud, Elasticsearch and LLM
  services. Their availability and backup are outside the local Compose stack.
- rc1.2 uses file-backed runtime secret material for SunaQ API/provider/mail/Graph/model service credentials, so those values no longer need to appear in the normal container environment. The optional bundled OpenWebUI still receives its scoped provider-client key through its upstream-supported environment interface; `docker compose config` can therefore expose that one optional UI credential when OpenWebUI is enabled. Docker-daemon access remains root-equivalent, and `runtime.env`, generated secret files, local Compose `.env` files and backups remain sensitive.
- Bundled nginx and OpenWebUI are opt-in in Super-Light. SunaQ Recherche is the
  reference slim Nextcloud UI for the current beta.
- Super-Light prepares the SunaQ application/provider image during installation,
  but the selected Neo4j image may still be pulled only when maintenance mode is
  disabled for the first normal start. This can make `maintenance-mode.sh off`
  perform an unexpected network download; pulling selected runtime images during
  installation is deferred installer polish.
- The bundled nginx starts with a generated bootstrap certificate. Replacing
  `install/nginx/tls/server.crt` and `server.key` after nginx has already
  started currently requires an explicit nginx reload/restart before the new
  certificate is served. Pre-start site-certificate provisioning/reload handling
  is deferred installer polish.
- The locally built Playwright renderer pins Playwright/Python package versions and the
  Microsoft base-image tag (`v1.62.0-noble`), but the base image is not yet pinned by
  immutable digest. Digest pinning is deferred dependency hardening.

## Contact seeds and Admin UI

- The internal `canonical_user_id` remains part of the database model, but normal
  UI/CLI administration is keyed by Nextcloud server/login. Scripts should avoid
  exposing UUIDs as operator input unless doing low-level diagnostics.
- Legacy global `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_PASSWORD` CardDAV settings
  remain only for compatibility. Multi-user seed sync should reuse Login-Flow
  credentials.

## Retrieval and completeness

- SunaQ retrieval is bounded. A request is not globally exhaustive merely because it
  asks for documents. Completeness/counting intent has separate limits and must
  fail conservatively when those limits prevent a defensible complete result.
- Normal verifier/answer windows are selected by the SunaQ profile rather than by
  one deployment-global default: Schnell 10, Gründlich 30, Tief 50. Deployment
  and administrator hard caps can still reduce unavailable capabilities.
- Super-Light has no local reranker. Deduplication is independent and remains
  active, but Elasticsearch ranking plus verifier behavior can still be less
  precise than a well-tuned reranked standard deployment on difficult corpora.
- Exact duplicate grouping can use Nextcloud FullTextSearch's valid 32-hex
  `hash` (MD5 of extracted FullTextSearch content, not a raw-file byte hash) as
  the primary exact-content signal. Distinct Nextcloud file IDs remain separate ACL
  variants; an authorized identical copy may be promoted when the ranked variant is
  denied. Near-text/OCR and same-stem format variants remain secondary signals.
- Conversational reference resolution is LLM-based and language-neutral rather than
  gated by a German keyword list. When prior chat is actually required, up to three
  documents from the immediately preceding answer context are re-resolved through
  the current user's live ACL and current source-scope policy before joining the
  normal verifier pool. This is short-lived turn continuity, not persistent entity
  memory; structured conversation entity state remains a later enhancement.
- Query rewriting is intentionally conservative. The model emits a small SearchSpec with a Nextcloud-compatible `elastic_query` and a natural
  `semantic_query`; it never emits raw Elasticsearch JSON DSL. Grammatical normalization is allowed,
  but factual synonyms must not be invented and explicit names/identifiers/years must
  not be silently discarded. Additional retrieval rounds are optional and remain
  bounded by administrator configuration.
- ACL-denied documents are removed without adaptive backfill of lower-ranked candidates.
  This can yield less evidence even when an authorized document existed below the
  bounded final window. The current order is retrieval/fusion, optional reranking,
  then live ACL. A fixed bounded pre-rerank ACL pool is a possible future
  optimization, but "keep fetching until N authorized results exist" is not part of
  the design because it creates variable work and another inference/timing surface.
- The optional ACL metadata prefilter evaluates owner/direct-user/group
  metadata. Nextcloud Circles are not considered in this first version; Circle
  support may be added in a later update. Leave the prefilter disabled where
  Circle-only shares must remain discoverable.
- Shared Neo4j names/aliases can influence retrieval across users by design. They are
  retrieval knowledge, not answer evidence. The current search API still exposes
  fairly rich entity-resolution diagnostics (matched forms/candidates/search forms);
  this diagnostic surface should be minimized or gated before it is treated as a
  normal end-user contract.

## Web Research and archive

- Cookie/overlay cleanup is best effort. Persistent per-host browser state reduces
  repeated consent prompts but does not guarantee that every CMP will disappear.
- Login walls, paywalls, CAPTCHAs and access controls are not bypassed or removed.
- A Playwright PDF is a readable snapshot, not a complete WARC/WACZ/forensic
  archive. Dynamic/video-heavy content can still render incompletely.
- Raw HTML, when enabled, contains the fetched main response rather than a package
  of every referenced resource.
- Background PDF rendering uses an in-process bounded task queue. If the API container is restarted while a render is pending, that render job is not durable and its sidecar may remain `pending`; text evidence and the answer are unaffected.
- Playwright PDF rendering is backgrounded, but the WebDAV archive write that creates the run directory, text snapshots, metadata/fetch-log material and initial `recherche.md` is still synchronous. On higher-latency Nextcloud/WebDAV paths this archive phase can dominate Web Research response time even when search/fetch/relevance are fast. This is a performance limitation, not an evidence or renderer failure.
- Web pages, incoming mail and saved chats can contain adversarial or instruction-like
  text. Structured verifier/Graph schemas and evidence separation reduce risk, but
  rc1.2 isolates retrieved content in server-generated JSON records and adds an immutable evidence guard, but no model-level mechanism can guarantee complete prompt-injection immunity. See `THREAT-MODEL.md`.

## SunaQ Recherche

- SunaQ Recherche 0.3.4 targets Nextcloud 23+. Each canonical user has exactly one configured Nextcloud chat-archive path and a per-user enable/disable gate below the global `chat_archive.enabled` switch; the default path is `SunaQ-Chats/`. SunaQ does not simultaneously scan an old `AKI-Chats/` folder and the new path. Operators upgrading an older RC can either configure that user to keep using `AKI-Chats/` or move the archive files once into the selected path. Path changes do not move files automatically. Legacy `.akirag.json` / HTML records remain readable within the selected path. Chat archives are a separate, optional `/chatarchive` source scope, not automatically trusted as primary document evidence. A saved chat is a new Nextcloud file with its own ACL/lifecycle; revoking the original source document does not automatically erase text already copied into the chat. Strict revocation deployments should leave chat archive disabled or define a retention/purge process.
- The app is deliberately thin. Advanced provider diagnostics and administration
  remain in SunaQ Admin rather than being duplicated in SunaQ Recherche.

## Graph
- rc1.2 provides no provenance-aware migration of a historical ERG Neo4j graph into SRC. During the RC line, converting an existing ERG installation to SRC requires an explicit full Neo4j reset and re-import of trusted CardDAV/administrator seeds. This avoids allowing older document-derived identities to influence SRC query expansion.

- CardDAV seeds and SunaQ Research Findings are lightweight graph inputs. Full
  document graph extraction remains comparatively expensive and opt-in.
- SunaQ stores only positive, direct, verifier-supported findings; it
  does not turn query hypotheses into global facts automatically. Research Findings are admin-visible and may be manually curated into document-grounded entity mentions and claims. They are still not automatically promoted into global facts or retrieval/query expansion.
- Equivalent Findings remain shared/deduplicated curation objects, while per-user
  observation provenance is represented through
  `CanonicalUser -> ResearchRun -> ResearchFinding`. Findings, Observations and
  Relations in SunaQ Admin require a selected canonical-user context and are filtered
  fail-closed through that user's current Nextcloud live ACL before evidence is
  rendered. **SunaQ Admin itself is nevertheless a trusted operator surface, not a
  personal Nextcloud-user surface:** an authenticated SunaQ administrator may select
  another configured user's context and thereby inspect evidence that *that selected
  user* may currently access. Do not expose SunaQ Admin to ordinary users or treat the
  administrator's own Nextcloud ACL as an isolation boundary.
- Optional self-service curation is narrower: a user sees only ResearchRuns produced
  for that canonical user and only Findings whose supporting document still passes
  that user's temporary Login-Flow credential. Shared Entity/Finding/Claim decisions
  can still affect later users because curation knowledge is intentionally global.
- Graph extraction worker startup and automatic enqueue of cited documents are off
  by default in the reference configuration.

## Mail

- The schema supports multiple mail accounts per canonical user, but the beta Admin
  UI is still oriented around the common one-account-per-user workflow.
- Mail credentials and Nextcloud credentials share the generic CredentialStore but
  are different services. Direct SQL updates by username are unsafe and unsupported.
- Raw EML is off by default for newly created accounts. The normalized mail, attachments, provenance headers and raw-message SHA-256 are retained; enable EML explicitly when forensic/raw-message retention is required.

## Operations

- There is no unified cross-store `purge-document` / data-subject workflow that
  proves deletion across Elasticsearch, Qdrant, Neo4j, optional RetrievalRecords
  and retained archive derivatives. The current implementation does perform lazy Neo4j self-cleanup when
  a successful live-ACL check definitively denies a user's numeric Nextcloud file:
  only that user's provenance edges for still-uncurated ResearchFindings are
  removed, and globally orphaned uncurated Findings are garbage-collected.
  Curated Findings are preserved, and ACL/backend/credential errors never trigger
  deletion. See `DATA-LIFECYCLE.md`.
- The current console recovery workflow covers SunaQ-owned configuration, SQLite,
  credential/master-key state and bundled Neo4j. It does not back up Nextcloud,
  Elasticsearch, Qdrant, external Neo4j, OpenWebUI/Playwright state or model
  caches. Restore currently requires the same supported deployment profile/mode
  and installation prefix. Master-key rotation remains a separate follow-up.
- Global service secrets such as provider/backend API keys still live in protected
  environment files rather than a dedicated external secret manager.
- The measured 4 GiB Super-Light success point is not a hard upper bound. Chromium
  produces transient memory peaks; capacity should be verified under the intended
  Web Research workload.
- Elasticsearch unavailability is translated to a friendly service-unavailable
  response, but the middleware cannot answer private document questions while its
  required Super-Light document arm is down.

### Verifier/Answer budgets

- The verifier and answer model may intentionally be different backends, so their
  remote limits remain separate in the current release candidate. The current configuration still has
  overlapping candidate/document caps (`bounded_verification_candidate_limit`,
  `REMOTE_VERIFIER_MAX_CANDIDATES`, `REMOTE_ANSWER_MAX_DOCUMENTS`, character
  budgets). These should be consolidated behind a small set of base values with
  empty per-role overrides inheriting the base value in a later cleanup.
- A verifier batch is not a retrieval round. If one retrieval round yields more
  candidates than one verifier batch, batching should consume the existing
  candidate pool before a new query rewrite/retrieval round is started.

## Identity administration

- Open identity candidates are grouped by transitive active `SAME_AS` component,
  so several source-specific ContactRecords for one confirmed identity do not
  create a combinatorial review queue. SunaQ Admin can filter the queue by canonical
  Nextcloud user and shows CardDAV user/address-book provenance for both sides.
  The filter is an administrative work-queue view, not an ACL boundary.
- End-user `/curation/` currently covers Research Findings only. Self-service
  `SAME_AS` / `NOT_SAME_AS` identity decisions are not yet exposed because a
  user-facing implementation must avoid revealing ContactRecords that exist only
  in another user's private address book.
- Nextcloud Login Flow must be completed by the actual target user. Nextcloud impersonation/"Nachahmen" does not safely pre-create another user's app password and can bind an external client identity to the impersonator's Nextcloud account. Revoke erroneous Nextcloud app passwords and remove the corresponding binding before reuse.
- The current Admin UI does not yet expose a dedicated per-binding delete button in SunaQ Admin.

## Mail backfill cutoff changes

The current mail state records UID cursors but not the effective `mail.not_before` value used when a historical backfill completed. If an account is first backfilled with a later cutoff (for example 2024) and the administrator later moves `not_before` earlier (for example 2020), the stored `backfill_before_uid=1` may keep the newly eligible older messages closed. Until the state schema is extended, the affected mailbox backfill cursor must be reopened manually after such a cutoff expansion.
