# Administration

This document covers the administrator-owned runtime state introduced for the
multi-user beta. Normal administration should use the Admin UI or CLI. Direct
SQLite changes are a diagnostic/emergency tool only.

Fresh 0.8.7-rc1 installations use `/opt/sunaq` in the command examples below. A recognized legacy installation retains its existing installation prefix (for example `/opt/nextcloud-rag`); substitute that retained prefix consistently in commands and paths.

## 1. SunaQ Admin and user UI paths

The reverse proxy reserves paths by role:

```text
/             selected user UI (OpenWebUI is optional)
/rag-admin/   protected SunaQ administration
/curation/    optional Nextcloud-authenticated end-user Findings curation
/rag-api/     protected middleware API/diagnostics
/v1/          OpenAI-compatible provider (Bearer-authenticated)
/auth/        Nextcloud Login Flow endpoints
```

Without a user UI, `/` redirects to `/rag-admin/`. OpenWebUI keeps its own
`/admin/...` path because the SunaQ Admin no longer occupies `/admin/`.

## 2. Trusted frontend clients

A provider API key identifies a frontend client, not a person. Plaintext keys
are shown only when created/rotated; `runtime/users.sqlite` stores their hashes.

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.provider_clients list
sudo -u rag ./.venv/bin/python -m rag.provider_clients create office-ui --name "Office UI"
sudo -u rag ./.venv/bin/python -m rag.provider_clients rotate office-ui
sudo -u rag ./.venv/bin/python -m rag.provider_clients disable office-ui
sudo -u rag ./.venv/bin/python -m rag.provider_clients enable office-ui
sudo -u rag ./.venv/bin/python -m rag.provider_clients delete office-ui
```

`disable` is reversible and retains bindings. `rotate` changes only the key and
retains bindings. `delete` removes the client and its frontend-scoped identity
bindings, Nextcloud credentials and pending login flows. Canonical Nextcloud
users plus their Mail/Web configuration are retained because another trusted
frontend may still use them.

For an external OpenWebUI, configure a dedicated client key and set the
connection's **Additional headers (JSON)** to:

```json
{"X-OpenWebUI-User-Id":"{{USER_ID}}"}
```

This flow has been validated with more than one trusted OpenWebUI client.

## 3. Canonical users and safe credential operations

Canonical user key:

```text
(nextcloud_server, nextcloud_login)
```

Frontend identities remain scoped as:

```text
client_id::external_user_id
```

### Per-user SunaQ model access

SunaQ model entitlement is separate from Nextcloud document authorization.

**Standard (Schnell)** (`sunaq-standard`) is the safe default. Administrators may enable
**Thorough (Gründlich)** and/or **Deep (Tief)** per canonical user in **SunaQ Admin → Users** and may
choose that user's default profile. The provider's authenticated `/v1/models`
response returns only currently allowed profiles.

A user who is not configured for a stronger profile cannot select it through the
SunaQ app or a generic provider client, and follow-up logic must not recommend it.
If model entitlement changes while a SunaQ browser tab is already open, reload
the app to refresh its model selector.

The profile packages themselves live below `models/` and are loaded at
API/provider startup. Editing a profile or prompt requires a process restart.

Useful CLI commands:

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.user_admin list
sudo -u rag ./.venv/bin/python -m rag.user_admin show demo-user
sudo -u rag ./.venv/bin/python -m rag.user_admin reauth demo-user
sudo -u rag ./.venv/bin/python -m rag.user_admin set-mail-password demo-user
```

Use `--server https://cloud.example/nextcloud` if the same login exists on more
than one Nextcloud instance. `reauth` removes only the user's frontend-scoped
Nextcloud app passwords/pending flows; the internal canonical user, bindings and Mail/Web/Contact-DB
settings remain. The next frontend request starts Login Flow v2 again.

### Do not impersonate a user during first authorization

A Nextcloud administrator should **not** enter or imitate another user's identity in a frontend and then complete that user's first SunaQ/Nextcloud authorization from the administrator's browser/session. The frontend identity is part of the binding key:

```text
client_id::external_user_id -> canonical Nextcloud user
```

If the administrator initiates that first authorization under the administrator's frontend identity while authenticating to Nextcloud as another user, the other user's Nextcloud login becomes bound to the administrator's frontend ID.

The current release does not provide a supported Admin-UI/CLI operation that safely reassigns such an incorrect frontend identity binding. Repair currently requires direct manipulation of `runtime/users.sqlite`, which should be treated as an emergency procedure only.

For normal onboarding, the actual user should open the configured frontend/app under their own frontend identity and complete Nextcloud Login Flow themselves. Administrators may configure clients and user-independent settings beforehand, but should not perform the user's first identity authorization on their behalf.

The Mail password command prompts through the terminal and does not print the
secret. In the Admin UI, Mail account configuration and the IMAP credential are
separate forms. The stored credential is never returned or pre-filled; replacing
it requires an explicit credential POST.

## 3.1 Research Findings curation access

Research Findings can be curated centrally by SunaQ administrators and, optionally, by selected Nextcloud users.

```yaml
research_findings:
  curation:
    admin_user_context: true
    user_self_service: false
    session_max_seconds: 7200
```

`admin_user_context` controls whether the protected SunaQ Admin may select a canonical user and curate that user's ResearchRuns. The Admin view uses that user's existing stored Nextcloud credential for the live ACL check; selecting a user does not create a new identity binding.

`user_self_service` exposes `/curation/` without the SunaQ Admin Basic-Auth layer. It is disabled by default. Each canonical user also has a separate **Findings curation** permission in SunaQ Admin → Users; normal research access does not imply permission to modify shared Graph-Lite knowledge.

Self-service authentication uses Nextcloud Login Flow v2 once per curation session. No SunaQ user password exists. A successful flow creates a temporary Nextcloud app password which is stored only in the encrypted `curation_sessions` table.

The default absolute session lifetime is two hours. `session_max_seconds` is configurable; request activity does not extend it. Every request re-checks expiry, canonical-user enablement and the per-user curation permission.

SunaQ Admin and self-service curation also use the live-ACL check as a **lazy cleanup point** for stale uncurated Finding provenance. After a successful ACL request definitively denies a numeric Nextcloud `files:<id>`, SunaQ removes only the selected/current canonical user's `ResearchRun-[:PRODUCED]->ResearchFinding` edge for Findings that have never been curated. A shared uncurated Finding is deleted only when no ResearchRun for any user still references it. Curated Findings are retained. ACL/backend/TLS/network/credential errors never trigger this cleanup.

At logout/expiry the session is invalidated locally before Nextcloud app-password revocation is attempted. Failed revocations stay `revocation_pending` and cannot authorize requests. On every API start all surviving temporary curation sessions are invalidated and their app passwords are submitted for revocation again.

The self-service cookie is `HttpOnly`, `Secure`, `SameSite=Strict` and scoped to `/curation/`. State-changing operations additionally carry a server-side session-bound CSRF token.

## 4. Credential encryption administration

```bash
cd /opt/sunaq
./.venv/bin/python -m rag.secret_admin status
sudo ./.venv/bin/python -m rag.secret_admin init-key --group rag
sudo -u rag ./.venv/bin/python -m rag.secret_admin migrate
sudo -u rag ./.venv/bin/python -m rag.secret_admin verify
```

Production uses `RAG_CREDENTIAL_ENCRYPTION=required`. Migration may temporarily
use `preferred`. No command provides a secret-show/export operation. The Admin UI
security page at `/rag-admin/security` shows only encryption state and whether
global runtime secret variables are configured.

## 5. `runtime/users.sqlite` layout

The schema is initialized by `rag.credential_store.CredentialStore`.

### `provider_clients`

Trusted frontend registry.

```text
client_id (PK)
name
api_key_hash (UNIQUE)
enabled
created_at / updated_at / last_used_at
```

### `canonical_users`

One row per verified Nextcloud identity.

```text
canonical_user_id (PK)
nextcloud_server
nextcloud_login
enabled
findings_curation_enabled
created_at / updated_at / last_seen_at
UNIQUE(nextcloud_server, nextcloud_login)
```

### `identity_bindings`

Maps one frontend-scoped identity to a canonical user.

```text
rag_user_id (PK)              # client_id::external_user_id
canonical_user_id (FK)
created_at / updated_at
```

### `credentials`

Generic credential store used by more than one service.

```text
rag_user_id
service
account_id
server
username
secret
created_at / updated_at
PRIMARY KEY(rag_user_id, service, account_id)
```

Important `service` values:

```text
nextcloud     frontend-scoped Nextcloud app password
mail_imap     canonical-user-owned IMAP password
```

Mail secrets use `rag_user_id=canonical-user:<canonical_user_id>`; Nextcloud
credentials use the frontend-scoped `client_id::external_user_id`. **Never
update credentials by `username` alone.** A username can occur in several
services and such an UPDATE can overwrite both IMAP and Nextcloud secrets.

Reversible secrets are encrypted at rest with AES-256-GCM. The master
key is stored outside SQLite (`runtime/credential-master.key` on a standard
installation) and is normally `root:rag 0640`. Ciphertexts are bound by AEAD
additional data to their user/service/account identity.

### `nextcloud_login_flows`

Pending Nextcloud Login Flow v2 state, keyed by `flow_id` and scoped
`rag_user_id`.

### `curation_sessions`

Ephemeral self-service Findings sessions. These are deliberately separate from normal provider credentials:

```text
session_id_hash (PK)
canonical_user_id (FK)
nextcloud_server
nextcloud_login
app_password          # AES-256-GCM encrypted
csrf_token
state                 # active | revocation_pending
created_at
expires_at
last_seen_at
```

The browser receives only the random session token; SQLite stores its SHA-256 hash. The temporary Nextcloud app password is never inserted into `credentials(service='nextcloud')` and does not create an `identity_bindings` entry.

### `mail_accounts`

Per-verified-Nextcloud-user mail configuration (intern über `canonical_user_id`). The schema is 1:n even though the beta
Admin UI currently edits one account per user.

Key fields include `account_id`, `canonical_user_id`, host/port/security,
username, mailbox list, target paths, `store_eml`, `store_attachments` and
polling/import limits. The password itself is not in this table; it is in
`credentials(service='mail_imap')`.

### `user_web_settings`

Per-verified-Nextcloud-user Web Research enablement, archive enablement and target path (intern über `canonical_user_id`).

### `user_chat_settings`

Per-canonical-user chat archive enablement and target path. `enabled` is the
user-level gate below the global `config.yaml: chat_archive.enabled` switch.
Exactly one dedicated Nextcloud-relative archive path is configured for each user;
absence of a row keeps the compatibility defaults (`enabled=true`,
`target_path=SunaQ-Chats`). Changing the path does not move existing archive
files. For an older RC archive, configure `AKI-Chats` for that user or move the
files once to the selected target.

### `contact_sync_settings`

Per-user CardDAV seed configuration and last-run status. The foreign key is the
internal `canonical_user_id`, but routine administration is keyed and displayed
by `nextcloud_server + nextcloud_login`. Fields include enablement, optional
address-book include/exclude lists, last sync time/status, contacts seen/written
and the last bounded error text. No CardDAV password is stored in this table;
the sync reuses the user's Nextcloud Login-Flow credential.

### `web_archive_roots`

History of Web archive roots that must stay excluded from ordinary internal
retrieval even if an administrator later changes a user's archive target.

### `store_meta`

Schema/runtime migration markers and internal metadata.

## 5. Optional sources: global vs. per-user switches

SunaQ exposes an optional source only when its effective capability is enabled
for the authenticated user. The bundled Nextcloud client therefore does not show
disabled optional source controls at all; explicit source directives are subject
to the same provider-side policy.

Mail archive retrieval/import requires:

```text
config.yaml: mail.enabled=true
mail_accounts.enabled=true
mail account has a usable credential
canonical user enabled
```

The mail worker is not started by the fresh-install baseline. It becomes active
only after the administrator enables both `mail.enabled` and
`mail.worker.enabled` and starts/enables the worker service. Native non-systemd
`start-all.sh` does this automatically from the configuration; systemd
deployments use `systemctl enable --now rag-mail-worker`, and Dockerized deployments
use `docker compose up -d mail-worker` from `install/dockerized/`.

When active, the worker polls every enabled account across all enabled canonical
users; a broken/missing credential for one user does not intentionally redefine the
others' configuration. Configured mailbox names are recursive roots: selectable
IMAP descendants are discovered with `LIST`, and new messages use a dedicated
Nextcloud directory per mail.
After storing/testable IMAP credentials, **Verbindung testen & Mailboxen ermitteln** runs the same IMAP `LIST` parser as the importer and shows server names, flags, hierarchy delimiter and `\Noselect` state. The configured mailbox list remains a set of recursive roots.

Web Research has global policy in `web.yaml` and per-user enablement/target in
`users.sqlite`. Live `/web` is available only when both the global Web service
and that user's Web switch are enabled. `/webarchive` additionally requires
global Web-archive persistence and the user's `archive_enabled` switch. The
Admin UI warns when Mail, Web or Chat is configured per user while the
corresponding global engine is disabled.

Saved chats are still classified as `chat_archive` so they can never fall into
ordinary document retrieval. Using or writing them is disabled on fresh installs.
Effective Chat availability is:

```text
config.yaml: chat_archive.enabled=true
user_chat_settings.enabled=true
canonical user enabled
```

Enable the capability only after the operator has accepted the independent
retention/lifecycle implications. When the effective capability is false, SunaQ
Recherche hides the Chats source and keeps new conversations session-local.

Research-Finding persistence is likewise opt-in:
`config.yaml: research_findings.enabled=false` is the fresh-install default.

CardDAV contact seeds are configured under **Users → <Nextcloud login> → Kontakt-DB**.
The UI deliberately does not expose the internal UUID as an operational selector.
It uses the Nextcloud credential already stored by Login Flow. The matching CLI is:

```bash
# native
./.venv/bin/python -m rag.contacts list
./.venv/bin/python -m rag.contacts sync --user alice

# dockerized
install/dockerized/contacts.sh list
install/dockerized/contacts.sh sync --user alice
```

Add `--server URL` only if the same login exists on multiple Nextcloud instances.
Missing credential, disabled source or an empty address book is a no-op rather than
a stack failure. The Admin UI starts synchronization as a background job and shows a progress page with processed/total, written, repaired, removed and error counts. **Adressbücher ermitteln** shows both CardDAV display names and technical slugs. Full successful scans reconcile source deletions; limited/dry-run/failed scans never delete unseen contacts. `NEXTCLOUD_USERNAME` /
`NEXTCLOUD_APP_PASSWORD` remains a legacy single-user compatibility path only.


### Global identity curation vs. source provenance

Contact records remain source-specific (`cloud_id`, `source_user_id`, address book and import-run provenance), while Entity identity decisions are shared Graph-Lite curation. The **Identitäts-Kandidaten** page therefore defaults to the global queue but also offers an optional canonical-user context to keep large multi-user installations manageable. That filter selects candidates whose CardDAV provenance includes the chosen Nextcloud login and shows the relevant user/address-book sources; it is an administrative work-queue filter, not an ACL or tenant-isolation boundary.

Open `POSSIBLE_SAME_AS` rows are grouped by the transitive active `SAME_AS` component on each side. If A and C have already been confirmed as `SAME_AS`, candidate edges A↔B and C↔B appear as one identity-group candidate rather than two redundant decisions.

RC5 distinguishes four identity relationships:

- `POSSIBLE_SAME_AS`: machine-generated review candidate only;
- `SAME_AS`: curator-confirmed equivalence. Both Entities remain active with their own ContactRecords, names and source lifecycle; query/entity resolution may use forms from the equivalence component;
- `NOT_SAME_AS`: persisted negative decision so the pair is not re-suggested;
- `MERGED_INTO`: explicit stronger consolidation. One Entity becomes a tombstone and evidence/identity references are redirected to the chosen survivor.

The normal candidate queue offers **Identisch** (non-destructive `SAME_AS`) and **Verschieden**. A technical merge remains a separate operation in Entity details and should be used only when two SunaQ identity nodes are themselves redundant, not merely because two independent users/address books contain records for the same real person or organization.

This global Entity curation is separate from **Research Findings** curation. The `research_findings.curation.admin_user_context` and `user_self_service` switches govern Finding/ResearchRun review and its live-ACL user context; they do not turn the global Entity/alias/identity layer into a per-user graph. End-user self-service for `SAME_AS` / `NOT_SAME_AS` is not exposed in RC5: a future user-facing implementation must filter provenance so it cannot reveal that a contact exists only in another user's private address book.

## 6. Elasticsearch credentials

`config.yaml` contains the username and secret reference only:

```yaml
elasticsearch:
  url: "https://es.example:9200"
  index: "nextcloud"
  username: "nextcloud"
  password_env: "ELASTICSEARCH_PASSWORD"
  verify_tls: true
  ca_file: "/etc/nextcloud-rag/es-ca.pem"
```

`runtime.env` contains:

```bash
ELASTICSEARCH_PASSWORD='...'
```

The administrator decides which Elasticsearch account to use. It can be the
same account as Nextcloud FullTextSearch or a separate read-only account. The
middleware neither creates nor changes Elasticsearch users/roles.

## 6.1 Private CA / internal PKI

Do not use `verify_tls: false` as the normal solution for internally signed
Nextcloud/Elasticsearch/LLM endpoints. Trust the issuing root/intermediate CA.

For the **Dockerized deployment**, pass one or more PEM CA
certificates during installation:

```bash
sudo ./install/install.sh --deployment dockerized \
  --ca-certificate /secure/path/company-root.crt ...
```

The API/provider image receives the private anchors in addition to the public
Debian CA bundle. A rerun rebuilds the image after CA changes.

For the **Standard deployment**, install the CA into the host OS trust
store. If the Python HTTP stack uses a separate CA bundle, set
`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` in the SunaQ environment to the host's
combined system bundle. Service-specific `ca_file` options (for example
Elasticsearch) remain available when a private CA should apply only to one
backend.

The TLS certificate served by the SunaQ nginx and the trust anchors used by SunaQ
as an HTTPS client are different concerns. Replacing `server.crt/server.key`
does not automatically make the issuing CA trusted by API/provider clients.

## 7. Direct SQLite access

For diagnostics only:

```bash
sudo -u rag sqlite3 /opt/sunaq/runtime/users.sqlite
.tables
.schema credentials
```

Before changing data manually, inspect the exact schema and include all key
columns (`rag_user_id`, `service`, `account_id`) in predicates. Prefer the CLI or
Admin UI whenever an operation exists there.


## OpenWebUI helper tasks

For the beta, the provider refuses to generate OpenWebUI follow-up suggestions.
A detected `ui:follow_ups` helper request returns `{"follow_ups":[]}` without an
LLM call. This is a server-side security boundary and does not depend on an
OpenWebUI UI setting.

## Container image policy

The beta installer manages only nginx plus optional Qdrant, Neo4j and OpenWebUI. Every shipped image reference is pinned to an immutable digest in `versions.lock.yaml` and `install/.env.example`; tags are retained only for readability. Ollama and web-search infrastructure are administrator-managed external services. Change an image pin only as an explicit upgrade followed by a fresh acceptance test.

## Qdrant sync lifecycle

Normal Elasticsearch -> Qdrant synchronization does not use payload-filter deletion. Stable UUIDv5 point IDs plus the committed SQLite `chunk_count` are used to overwrite and remove chunks deterministically. When a document shrinks, new chunks are upserted first and only the obsolete tail IDs are deleted afterward. A stale document is removed by its known explicit point IDs.

Recommended existing-installation sync settings for large/heterogeneous corpora are `elasticsearch.page_size: 50` and `sync.embedding_batch_size: 8`. They affect peak memory/HTTP batch size, not semantic chunk boundaries.


## Retrieval path: Query Rewriter, SearchSpec and verifier

Canonical round/budget settings live in `config.yaml` under the legacy-named
`retrieval_planner` section; `MAX_RETRIEVAL_ROUNDS` in `provider.env` remains
only a compatibility fallback if the YAML key is absent. The section name is
retained to avoid configuration churn, but the normal path no longer uses the
historical multi-probe planner.

Current reference policy:

```yaml
retrieval_planner:
  enabled: true              # enable additional rounds after round 1
  max_retrieval_rounds: 1    # 1 = rewrite once, retrieve once
  model: ""                  # inherit LLM_MODEL
  max_tokens: 700
  context_max_chars: 12000
  max_complete_documents: 15
  overflow_acl_scan_limit: 80
  verification_candidate_limit: 6
  bounded_verification_candidate_limit: 30
  exhaustive_verification_candidate_limit: 30
  verification_max_chars_per_document: 2500
  verification_max_tokens: 900
  verification_batch_size: 6
```

Round 1 starts with one Query Rewrite. In ERG, bounded resolver/query-context
may be supplied before the rewrite when that capability is granted. The model
emits a backend-neutral `SearchSpec`: natural `semantic_query`, `entities`,
`entity_hints`, `concepts`, `concept_groups`, `constraints` and
`verification_requirements`. The current planner response contract does not
contain `elastic_query` and never emits raw Elasticsearch JSON.

The same structured intent can drive multiple backends:

```text
entities + concept_groups + safe literals
    -> deterministic Files compiler -> Elasticsearch JSON
semantic_query
    -> Qdrant, when granted and available
resolver/query context
    -> bounded identity/alias enrichment in ERG
analysis fields
    -> verifier + provenance / optional Findings
```

Required concept groups compile to MUST(OR(...)); optional groups contribute
ranking/context. This keeps semantic interpretation in the planner while
backend syntax and occurrence semantics remain deterministic middleware
responsibilities.

The resulting candidate lists are fused, deduplicated and optionally reranked.
Live Nextcloud ACL then removes unauthorized candidates. No lower-ranked
candidates are adaptively fetched/backfilled after an ACL denial. This means a
narrowly authorized user may receive fewer results even when an authorized
candidate existed just below the final ranking window.

RC5 adds an optional pre-rerank ACL-metadata prefilter. Existing installations preserve their `config.yaml` on installer reruns, so this and other newly introduced optional keys are **not auto-merged** into an older configuration; add them manually when the feature is wanted:

```yaml
acl:
  enabled: true
  prefilter:
    enabled: false
```

When enabled, v1 resolves the authenticated user's actual Nextcloud UID and
current group IDs server-side through the OCS current-user endpoint using the stored
Nextcloud app credential. It matches that UID against `owner` / `users` and the
server-derived group IDs against `groups` in Elasticsearch and Qdrant. Prefiltering
therefore does not depend on SunaQ Recherche or another frontend supplying group
headers. If the OCS identity lookup is unavailable or malformed, only the metadata
prefilter is skipped for that request and retrieval falls back to the established
unfiltered path; final live WebDAV ACL still applies. Circles are intentionally not
evaluated in v1; keep the feature disabled where Circle-only shares are relevant.

The prefilter is a recall/performance optimization only. Final live WebDAV ACL
remains mandatory and is the sole document-authorization boundary. The optional Candidate Verifier
checks only the administrator-controlled authorized-candidate window. For a
requested document type the document itself must be of that type; a bank
statement that merely mentions an invoice is not an invoice match.

Standard and Dockerized deployments use the same retrieval path. Effective Qdrant
and reranker availability is determined by architecture/capability configuration,
not deployment packaging. Verifier windows are selected by the active SunaQ
profile (Standard/Schnell 10, Thorough/Gründlich 30, Deep/Tief 50) and remain subject to administrator
hard limits.

Additional retrieval rounds remain optional. If `enabled:true` and
`max_retrieval_rounds > 1`, a later round may produce a revised SearchSpec after
seeing the bounded result picture. Every round executes the **same**
ES/Qdrant/fusion pipeline; there is no second probe language or arm-specific LLM
syntax.

### Exhaustive mode

Ordinary wording such as “Suche die Rechnungen …” is **not** exhaustive merely because documents are requested. Broader completeness handling is reserved for explicit completeness/counting intent such as `alle`, `sämtliche`, `vollständige Liste`, `wie viele` or `zähle`.

The completeness path remains bounded by `max_complete_documents` and `overflow_acl_scan_limit`; it fails conservatively rather than claiming completeness from an unbounded or partially authorized result set. No pre-ACL hit count is exposed to the user.

### RetrievalRecord

Optional query/evidence audit records can be enabled with:

```yaml
retrieval_record:
  enabled: true
  directory: "runtime/retrieval-records"
```

Records contain the original/normalized query, SearchSpec/derived QueryFrame, reviewed document metadata, verifier decisions/EvidenceFrames and provenance, but no complete document bodies and no user identity. They are intended for debugging/audit and a later **curated** graph-ingestion path; QueryFrames must never be auto-imported as facts.

### Document-grounded indirect Graph chains

Graph retrieval may return a two-hop `A -> C -> B` chain only when each hop is a document-grounded `RelationObservation`. Such results are explicitly indirect; `direct_relations` remains empty for the chain. Prompts forbid presenting `A -> C -> B` as a direct `A <-> B` relationship.

### Web Research

The public Web arm supports Brave Search and external SearXNG. Search results are discovery only: target pages are fetched, the best passage is selected, and a relevance LLM must make a complete per-source Structured-Output decision. Search snippets are never answer Evidence.

When per-user archiving is enabled, selected pages may be stored as `recherche.md`, text snapshots, raw PDFs and raw HTML main responses. The configured archive root is excluded from normal internal retrieval to prevent archived Web Evidence from reappearing as an apparently independent internal source.

## Worker switches and trust boundary

Capabilities and background workers are deliberately separate:

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

This allows manual Qdrant synchronization, prepared mail accounts and an
available Neo4j/Graph Queue without immediately starting CPU-intensive or
privacy-relevant background work.

For role-specific LLM routing and remote evidence limits, see
`PRIVACY-ARCHITECTURE.md` and `provider.env.example`.

## Administrator-managed CPU model services

A tested CPU-oriented layout keeps model services outside the middleware install
prefix, for example `/opt/sunaq-models`. The middleware does not own or
upgrade these containers. A practical reference combination is Ollama 0.24.0
with `qwen3-embedding:4b` plus a TEI CPU service running
`Alibaba-NLP/gte-multilingual-reranker-base`. Bind both services to loopback when
they run on the SunaQ VM.

For Ollama embedding-only use, keeping one loaded model resident avoids repeated
large disk reads on quiet/busy transitions:

```yaml
environment:
  OLLAMA_KEEP_ALIVE: "-1"
  OLLAMA_MAX_LOADED_MODELS: "1"
```

The initial ES→Qdrant synchronization is CPU intensive on a CPU-only VM. It can
be disabled with `sync_worker.enabled: false` and run manually at a controlled
time or on a suitable system.

## Docker persistent data

Bundled Qdrant, Neo4j and OpenWebUI use Docker named volumes rather than visible
subdirectories below `/opt/sunaq`. Inspect them with:

```bash
docker volume ls
docker volume inspect install_qdrant_data
```

The `rag` service account intentionally does not require Docker-socket access.
Membership in the `docker` group is effectively root-equivalent and should not
be granted merely to access the middleware.

## Private Elasticsearch backend network

For an Elasticsearch instance without authentication, do not expose TCP/9200 on
the ordinary LAN. A pragmatic legacy setup is a dedicated isolated VM network
with one backend NIC per VM, no gateway and a firewall rule allowing TCP/9200
only from the SunaQ VM. Elasticsearch can bind only its HTTP interface to that
backend address while keeping cluster transport local.


## FullTextSearch reconciliation helper

`helpers/fts_reconcile` contains a standalone Nextcloud maintenance app. Its `fts_reconcile:files` command is dry-run by default, `--reconcile` marks stale file-provider FTS rows for removal through the FullTextSearch status path, and `--unreconcile` removes a pending removal mark before FullTextSearch has processed it. The helper is not installed by the middleware installer.

## TLS compatibility for private PKI

Prefer `verify_tls: true`. Private CA trust and Python X.509 strictness are two
separate controls:

```yaml
tls:
  x509_strict: true
```

- `true` (default): Python 3.13 strict RFC-5280 validation.
- `false`: keep normal CA/chain/SAN/hostname/validity verification but remove
  only `VERIFY_X509_STRICT`; useful for established intranet PKIs lacking newer
  extensions such as Authority Key Identifier on leaf certificates.
- `verify_tls: false`: disables certificate verification for the affected
  backend and should remain a diagnostic/legacy exception.

For dockerized installs use `--ca-certificate` so API/provider containers receive
the private CA in their own trust store. Installing a CA only on the Docker host
does not automatically make it trusted inside containers.

## Retrieval policy administration

`retrieval_policy` controls which implemented capabilities are available, not
their algorithms or resource budgets. The normal automatic private-document path
uses `files` and, when configured, `vector`. Neo4j remains available for
entity/alias expansion independently of graph document retrieval.

```yaml
retrieval_policy:
  internal:
    files: required
    vector: optional
    graph: optional
  optional_default: include
  web: planner
```

Neo4j entity resolution/query expansion is independent of the graph *document*
retrieval arm. An ERG deployment can therefore use Neo4j entity resolution/query expansion
while `retrieval_policy.internal.graph: disabled` and
`graph_retrieval.enabled: false`.

## SunaQ Recherche 0.3.16

The Nextcloud client is under `clients/nextcloud/sunaq`. It is intentionally a
thin search frontend: Nextcloud session → server-side proxy → OpenAI-compatible
provider. It sends the current Nextcloud UID as `X-RAG-User-ID`; the provider key
never reaches browser JavaScript.

SunaQ Recherche renders a safe Markdown subset without raw HTML and adds per-user-message
controls. Saved conversations are written as readable `.md` files in the user's visible
`SunaQ-Chats/` folder; the machine state remains in hidden `.<chat-id>.sunaq.json`
sidecars. Legacy `AKI-Chats/` / `.akirag.json` archives remain readable for compatibility. New/updated Markdown archives persist `source_origin=chat_archive` plus the
stable Nextcloud `files:<id>` and register that origin immediately through the trusted
provider/API path; the existing path classifier remains a recovery fallback.

It adds per-user-message controls:

- **Erneut senden**: rerun exactly that question from that conversation point.
- **Bearbeiten & erneut senden**: load the question into the composer, edit it,
  and branch the conversation at that point before resubmitting.

The old and edited variants are never sent together as competing user turns.

### SunaQ installation/configuration

The app targets Nextcloud 23+. Install the `sunaq` directory below the Nextcloud
`apps/` tree and enable it with `occ app:enable sunaq`. Configure the middleware
base URL and provider API key under **Settings → Administration → Additional
settings**. The key is stored server-side through Nextcloud encryption and is not
exposed to browser JavaScript.

If the configured middleware URL points to an RFC1918/private address, Nextcloud may
reject it with `Host violates local access rules`; in a deliberately internal
deployment the global Nextcloud option `allow_local_remote_servers => true` permits
that server-side proxy connection. The Nextcloud host must also trust the middleware
TLS issuer. SunaQ Recherche registers the navigation through `<navigations>` and ships
a dedicated compact `img/app.svg`, so the app appears in the normal Nextcloud app
navigation rather than requiring a manual URL.

### Findings administration and Graph-Lite boundary

`ResearchFinding:AKIResearchFinding` nodes are visible in **Graph → Findings** with their query/evidence frame, verification/provenance and supporting document. The default view shows open findings grouped by unresolved entity text. Administrators can curate individual rows or use checkbox-based bulk assignment / bulk not-an-entity decisions; findings without any entity text can be suppressed as a batch without deleting provenance.

A confirmed entity decision creates document-grounded observation/mention provenance. With at least two curated entities an administrator may create a manual document-grounded `RelationObservation` claim. The current release does **not** convert a Finding or Claim into a global Entity relation or query-expansion edge automatically. See `GRAPHLIGHT-FINDINGS.md`.

Finding curation is shared work. Equivalent Findings coalesce so later authorized users can reuse existing curator decisions. Per-user provenance is represented by `CanonicalUser -> ResearchRun -> ResearchFinding`, and the Admin Findings/Observations/Relations views require a selected canonical-user context. Supporting documents are checked live with that selected user's Nextcloud credential before EvidenceFrame/document details are rendered.

SunaQ Admin remains a **trusted operator surface** rather than a personal Nextcloud-user surface: the administrator may deliberately switch canonical-user context and inspect evidence visible to that selected user. This is not a missing live-ACL check, but it also is not tenant isolation against the SunaQ administrator. Do not expose `/rag-admin/` to ordinary users. End-user `/curation/` is separately authenticated through Nextcloud Login Flow and currently limits users to their own ResearchRuns.

Personal `Mailarchiv/`, `Webarchiv/` and `SunaQ-Chats/` content is initially private because it is stored in the owning user's Nextcloud file tree. Sharing folders through normal Nextcloud shares is the supported collaboration mechanism; retrieval still performs the live ACL check for the querying user. `/chatarchive` is optional: it is useful as retained working memory, but a saved chat can contain copied/derived text whose lifecycle is independent from the original source document. See `THREAT-MODEL.md` and `DATA-LIFECYCLE.md`.


## Security/lifecycle operator notes

- `/health` exposes `live_acl.enabled` and the configured identity mode. `acl.enabled=false` is a lab/diagnostic state, not a safe shared-corpus mode.
- Mail, Web and Chat content may be untrusted/instruction-like text; do not treat successful extraction as a security trust signal.
- There is no unified cross-store document/person purge command in 0.8.5. See `DATA-LIFECYCLE.md` before defining retention/deletion procedures.
- For the engineering threat model and the shared-alias/evidence distinction, see `THREAT-MODEL.md`.
