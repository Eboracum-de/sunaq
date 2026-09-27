# SunaQ technical reference
## Technical documentation and command reference

**Version:** `0.8.6-rc1.2` (draft)  
**Updated:** 25 September 2026

This file is the consolidated technical reference for the current release-candidate snapshot. Internal development and migration drafts are not part of the release documentation. Where older notes conflict with the current implementation, this reference together with `config.yaml`, `models/*/profile.yaml`, `models/README.md`, `web.yaml`, `provider.env.example` and `versions.lock.yaml` describes the intended baseline.

---

# 1. Directory and process model

Default installation directory:

```text
/opt/sunaq/
```

Main processes:

| Process | Default port | Purpose |
|---|---:|---|
| `rag-api` | 8765 | Retrieval, live ACL, Web, Graph API and Admin UI |
| `rag-provider` | 8766 | OpenAI-compatible chat interface and orchestration |
| `graph-worker` | – | asynchronous GraphQueue processing |
| `mail-worker` | – | periodic recursive mail synchronization |
| `sync-worker` | – | periodic Elasticsearch → Qdrant synchronization |
| OpenWebUI | 3000 loopback | optional frontend |
| Qdrant | 6333 loopback | optional local vector store |
| Neo4j | 7687/7474 | optional local graph/browser according to deployment profile |

Reverse-proxy paths:

```text
/             user UI; OpenWebUI when installed
/rag-admin/   protected SunaQ administration
/curation/    optional Nextcloud-authenticated Findings self-service
/rag-api/     middleware/diagnostics
/auth/        Nextcloud Login Flow
/v1/          OpenAI-compatible provider
```

Without a user UI, `/` redirects to `/rag-admin/`.

### Internal API trust boundary

Port `8765` is an internal FastAPI service and is loopback-bound by the supported profiles. The current 0.8.6 line enforces explicit route zones through central FastAPI dependencies:

- `PUBLIC`: no internal machine credential; currently only `/live`;
- `INTERNAL`: requires `X-AKI-Internal-Key` / `RAG_INTERNAL_API_KEY`;
- `TRUSTED_PROVIDER`: requires the internal key plus `X-AKI-Provider-Key` / `RAG_PROVIDER_INTERNAL_KEY`;
- `USER`: trusted-provider authentication plus a usable scoped identity in multi-user modes; Nextcloud live ACL remains the final document authorization boundary;
- `ADMIN`: internal machine authentication plus SunaQ Admin Basic credentials.

The bundled provider sends both internal and provider-role credentials. Bundled nginx receives and injects only the internal credential, after its configured proxy authentication layer, so an nginx-forwarded request cannot acquire provider privileges. Client-supplied internal-key values are overwritten by nginx.

Core FastAPI routes expose their assigned zone in OpenAPI as `x-aki-security-zone`, and CI verifies the route/zone mapping. `X-RAG-User-ID` remains an identity lookup key used only after the trusted-provider boundary; it is **not** caller authentication. Direct access to port 8765 is unsupported. Native startup rejects non-loopback `RAG_API_HOST` unless `RAG_ALLOW_REMOTE_INTERNAL_API=true` is explicitly configured.

### Architecture, preset, component-profile and deployment axes

rc1.2 separates four concepts:

- **SRC / ERG** are enforced architecture/capability tiers. SRC is the narrow
  Documents-only Elasticsearch/files path with mandatory live ACL, one retrieval
  round and local/private model processing; ERG is the extension envelope.
- **core / workgroup** are shipped capability presets. `core` selects the SRC
  invariant set; `workgroup` is a conservative Elasticsearch-centric ERG
  starting point.
- **Super-Light / Standard** are component/resource profiles. Super-Light ships
  as formal SRC in rc1.2; Standard remains the ERG-compatible reference profile.
- **native / dockerized** are deployment mechanisms. The supported mappings
  remain `standard+native` and `super-light+dockerized`.

Installers accept `--preset core|workgroup` and `--preset-file FILE`.
Preset files are YAML data merged through `rag.config_preset`; they are never
sourced as shell. Explicit connection/TLS/index installer arguments are applied
after the preset and therefore win.

The existing Standard-installer option `--core` is a **legacy resource
shorthand for Qdrant + Neo4j**. It is unrelated to `--preset core` and must not
be used as terminology for the SRC boundary.

---

# 2. Installation

## 2.1 Show the installation plan

```bash
sudo ./install/install.sh --plan --full
```

## 2.2 Standard/native installer options

The public wrapper accepts `--profile standard|super-light` and `--deployment native|dockerized`. In the 0.8.6-rc1.2 line the supported and regression-tested combinations are `standard+native` and `super-light+dockerized`. Profile and deployment remain explicit, conceptually separate axes even though other combinations are not yet supported. Common connection/frontend/proxy switches use the same names in both profiles. Profile-specific resource switches remain separate because Super-Light deliberately has no Qdrant/reranker arm. On rerun, prior OpenWebUI/proxy selections are retained unless an explicit `--no-...` override is supplied. The options below belong to the standard/native profile.

| Option | Meaning |
|---|---|
| `--prefix PATH` | installation directory, default `/opt/sunaq` |
| `--user USER` | service user, default `rag` |
| `--nextcloud-url URL` | override the Nextcloud base URL in `config.yaml` |
| `--elasticsearch-url URL` | override the Elasticsearch endpoint in `config.yaml` |
| `--elasticsearch-index ID` | override the Elasticsearch index in `config.yaml` |
| `--preset core\|workgroup` | apply a shipped capability preset; explicit connection/TLS/index arguments win |
| `--preset-file FILE` | apply a site-owned safe YAML capability overlay |
| `--skip-system-packages` | do not install OS packages/Docker |
| `--with-qdrant` | install/start local Qdrant |
| `--with-neo4j` | install/start local Neo4j |
| `--core` | legacy Standard-installer resource shorthand: Qdrant + Neo4j; unrelated to the rc1.2 `core` capability preset |
| `--with-openwebui` | install/start or retain the pinned OpenWebUI build |
| `--no-openwebui` | explicitly disable/remove the OpenWebUI container; persistent volume is retained |
| `--full` | Qdrant + Neo4j + OpenWebUI |
| `--with-proxy` | explicitly enable/retain the bundled nginx proxy |
| `--no-proxy` | explicitly disable/remove the bundled nginx proxy |
| `--proxy-http-port PORT` | nginx HTTP listen port; default 80 |
| `--proxy-https-port PORT` | nginx HTTPS listen port; default 443 |
| `--no-proxy-basic-auth` | disable nginx Basic Auth gate; rate limits remain |
| `--multi-user` | explicit multi-user / credential-store mode; default |
| `--single-user` | explicit single-user mode; live ACL remains enabled |
| `--acl-off` | diagnostic mode without live ACL; not for shared protected corpora |
| `--with-systemd` | install/enable optional systemd units |
| `--no-systemd` | legacy alias: no systemd integration |
| `--no-reranker-download` | do not preload the local HF reranker |
| `--plan` | show plan only |
| `-y`, `--yes` | confirm non-interactively |

No longer bundled:

- Ollama: `--with-ollama` is rejected;
- SearXNG: `--with-searxng` is rejected.

Both can be operated externally and configured as backends.

## 2.3 Super-Light/dockerized installer options

Super-Light has a separate CLI because it configures the external Nextcloud/FullTextSearch connection while building the containerized middleware. `--nextcloud-url` and `--elasticsearch-url` are required when the installer starts the stack; they may be omitted together with `--no-start`.

| Option | Meaning |
|---|---|
| `--nextcloud-url URL` | canonical Nextcloud base URL, preferably HTTPS |
| `--elasticsearch-url URL` | existing Nextcloud FullTextSearch Elasticsearch endpoint |
| `--elasticsearch-index ID` | FullTextSearch index name; default `my_index` |
| `--preset core\|workgroup` | apply a shipped capability preset; Super-Light defaults to core/SRC |
| `--preset-file FILE` | apply a site-owned safe YAML capability overlay inside the provider container |
| `--prefix PATH` | installation directory; default `/opt/sunaq` |
| `--skip-system-packages` | do not install Docker/curl/jq/openssl |
| `--no-start` | prepare files/images but do not start the stack |
| `--with-openwebui` | start/retain the bundled OpenWebUI; fresh default off |
| `--no-openwebui` | explicitly disable/remove bundled OpenWebUI on rerun |
| `--with-proxy` | start/retain the bundled nginx TLS/auth gate; fresh default off |
| `--no-proxy` | explicitly disable/remove bundled nginx on rerun |
| `--proxy-http-port PORT` | nginx HTTP listen port; default 80 |
| `--proxy-https-port PORT` | nginx HTTPS listen port; default 443 |
| `--ca-certificate FILE` | add a private root/intermediate CA to API/provider containers; repeatable |
| `--x509-strict` | enable Python/OpenSSL `VERIFY_X509_STRICT` |
| `--no-x509-strict` | compatibility mode: normal TLS verification remains enabled, extra strict checks remain off |
| `--plan` | show the effective plan and exit |
| `-y`, `--yes` | confirm non-interactively |

Example:

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --with-proxy
```

For a host where Nextcloud/Apache already owns 80/443, use alternate internal nginx ports, for example `--proxy-http-port 81 --proxy-https-port 444`, and let Apache proxy only the SunaQ path prefixes. See `install/INSTALL.md` and `docs/BETA-OPERATIONS.md`.

## 2.4 Reference installation

Standard/native:

```bash
sudo ./install/install.sh --plan --with-openwebui --with-qdrant --with-neo4j
sudo ./install/install.sh       --with-openwebui --with-qdrant --with-neo4j
```

Super-Light/dockerized:

```bash
sudo ./install/install.sh --profile super-light --plan \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index
```

Before first start, review at least `config.yaml`, `provider.env` and `runtime.env`.

## 2.5 Maintenance mode

The RC5 line introduced an explicit operator-controlled maintenance state; rc1.1 retains it. Fresh installs and installer reruns set:

```text
RAG_MAINTENANCE_MODE=true
```

in `runtime.env` and start only the maintenance-facing provider path needed to keep the OpenAI-compatible endpoint predictable for trusted frontends while normal retrieval/API/workers are unavailable.

Use the wrapper rather than editing the environment value manually:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh status
sudo /opt/sunaq/install/maintenance-mode.sh on
sudo /opt/sunaq/install/maintenance-mode.sh off
```

`status` reports both the maintenance state and the recorded deployment mode.

While maintenance is **on**:

- the normal SunaQ API/background workers are stopped or not started;
- the provider runs the minimal `rag.maintenance_provider` implementation;
- `/live` and `/health` report maintenance state;
- `/v1/models` and `/v1/chat/completions` still require a registered provider-client Bearer key from `runtime/users.sqlite`;
- authenticated chat requests receive the configured maintenance message instead of loading retrieval, LLM, Elasticsearch, Qdrant or Neo4j paths.

This is intentionally not an unauthenticated bypass. If the provider-client registry is unavailable, the maintenance provider fails closed rather than accepting arbitrary callers.

For **Super-Light/dockerized**, `maintenance-mode.sh off` starts Neo4j and the API, plus Playwright only when it was explicitly installed with `--with-playwright`. The mail worker is not part of the baseline startup. The wrapper waits for Neo4j/schema initialization and only then recreates the normal provider. If schema/startup readiness fails, it restores `RAG_MAINTENANCE_MODE=true` and returns to the maintenance provider.

For **Standard/native**, the wrapper performs the corresponding systemd/native process switch. Systemd-managed switching requires root.

Maintenance mode is required before the retained `backup-restore.sh create` and `restore` operations and is the expected state for comparable invasive maintenance. The restore workflow deliberately leaves SunaQ in maintenance mode until health/smoke/live-ACL checks have completed; see `BETA-OPERATIONS.md` and `DATA-LIFECYCLE.md`.

---

# 3. Configuration files

## 3.1 `config.yaml`

0.8.6 changes the configuration ownership boundary substantially. `config.yaml`
remains the canonical place for **deployment/global infrastructure, security,
source access and background-worker policy**. Request-local research behaviour is
owned by the selected SunaQ model package under `models/<profile>/profile.yaml`.

For packaged SunaQ models, the following sections are intentionally **not**
inherited from global `config.yaml`:

| Profile-owned section | Current profile-local parameters |
| --- | --- |
| `search` | `es_limit`, `vector_limit`, `vector_threshold`, `rrf_k`, `rerank_candidates`, `final_limit`, `rerank_min_score` |
| `retrieval_planner` | `enabled`, `thinking`, `max_retrieval_rounds`, `max_queries_per_round`, `model`, `max_tokens`, `context_max_chars`, `max_complete_documents`, `overflow_acl_scan_limit`, verifier candidate/character/token/batch limits |
| `evidence_control` | `mode` and future evidence-review policy |
| `retrieval_signal` | unspecific-query rejection, signal floors, curve/head/tail and dominance/overlap thresholds |
| `entity_resolution` | enable/fail-open/retry/fuzzy settings and candidate/similarity thresholds |
| `graph_retrieval` | enable, graph candidate limits, weights, hydration and snippet budgets |
| `reranker` | backend/model/device/batch/length/TEI/fallback settings |
| `context_enrichment` | head/window/max-window/max-character budgets |
| `answer_context` | max documents, per-document characters and total answer-context characters |

The loader copies the global configuration, removes all nine profile-owned
sections, then installs the selected model's values. This is deliberately
stronger than a normal YAML override: changing an old global
`config.yaml: retrieval_planner` or `config.yaml: reranker` section must not
silently change every packaged SunaQ model.

Typical settings that remain global include Elasticsearch/Qdrant/Neo4j
endpoints, embeddings, ACL and identity policy, retrieval capability policy,
Nextcloud/TLS settings, source/archive policy, synchronization, GraphQueue and
graph discovery/indexing workers, credentials, mail, Admin UI and deployment
metadata.


### Elasticsearch

```yaml
elasticsearch:
  enabled: true
  url: "https://es.example:9200"
  index: "nextcloud-index"
  username: "rag-readonly"
  password_env: "ELASTICSEARCH_PASSWORD"
  verify_tls: true
  ca_file: "/etc/nextcloud-rag/es-ca.pem"
  page_size: 50
```

Store the password only in `runtime.env` or an equivalently protected secret store. For retrieval, a read-only Elasticsearch account is recommended. The middleware does not require write access to the Nextcloud FullTextSearch index for normal search.

### Embeddings

Local reference:

```yaml
embedding:
  backend: ollama
  url: "http://127.0.0.1:11434"
  model: "qwen3-embedding:4b"
  profile: custom
  document_prefix: ""
  query_prefix: "Instruct: Given a user query, retrieve relevant passages from a private document archive that answer or relate to the query\nQuery: "
  dimensions: 1024
  api_key_env: "EMBEDDING_API_KEY"
  verify_tls: true
  timeout: 300
```

The embedding layer is model-agnostic. `query_prefix` and `document_prefix` are configured explicitly and are never inferred from the model name. The reference Qwen3 configuration uses a query-only retrieval instruction. Other models may use different prefixes or none.

`dimensions` is optional and model/backend dependent. In the reference setup, `dimensions: 1024` keeps the Qdrant collection at 1024 dimensions even with the 4B embedding model. Different embedding spaces must not be mixed; changing the embedding model normally requires a separate/rebuilt vector collection.

By default the sync path adds the file basename only to the embedding input (`sync.embedding_include_basename: true`). The evidence chunk stored in Qdrant remains unchanged.

For a one-off GPU initial sync:

```bash
rag.sync --embedding-url http://GPU-HOST:11434
```

An external OpenAI-compatible embedding endpoint is supported. When external embeddings are used, document text is transmitted chunk-by-chunk to that provider.

### Qdrant

```yaml
qdrant:
  enabled: true
  url: "http://127.0.0.1:6333"
  collection: "nextcloud_rag"
```

Qdrant is a retrieval store, not an authorization authority. Keep it on loopback or a trusted network unless remote access is required. If the selected deployment/backend offers separate read-only credentials or network policy, use least privilege for query-only consumers.

### Retrieval, query rewrite and optional rounds

The normal retrieval contract is deliberately small. Round 1 rewrites the user question once into a SearchSpec:

```json
{
  "elastic_query": "+examplehost +2025 +invoice",
  "semantic_query": "invoices from examplehost in 2025",
  "entities": ["examplehost"],
  "concepts": ["invoice"],
  "constraints": [{"kind": "year", "value": "2025"}],
  "verification_requirements": [
    "The document itself is an invoice from examplehost.",
    "The relevant year is 2025."
  ]
}
```

A compact Neo4j seed/alias context is supplied before the rewrite. `elastic_query` is a human-style Nextcloud full-text expression, **not raw Elasticsearch JSON DSL**. The API parses it and deterministically constructs the Elasticsearch request body. `semantic_query` is sent only to Qdrant when the vector arm is enabled.

`entities`, `concepts`, `constraints` and `verification_requirements` are analytical side products for Graph-Lite, verifier and provenance; they do not silently rewrite `elastic_query`.

Elasticsearch retrieval can optionally apply the implemented ACL metadata
prefilter before later ranking work. Its first version uses Nextcloud
owner/direct-user/group metadata; Circle membership is not yet evaluated. If the
required identity/group context is unavailable, the prefilter is skipped rather
than denying otherwise visible evidence. It is only a retrieval optimization:
the final live WebDAV ACL remains authoritative.

Backend results are fused, deduplicated and optionally reranked. The current
normal path then applies live Nextcloud ACL and the Candidate Verifier. ACL
denials do not trigger adaptive replacement searches, so the visible candidate
window may become smaller. A fixed pre-rerank ACL pool remains a documented
future optimization and is distinct from the implemented metadata prefilter.

After Candidate Verification, SunaQ can optionally run **Evidence Control**.
The current rc1.2 shipped profiles set `evidence_control.mode: off`; this does
not disable the Candidate Verifier.

The configuration split above is part of the 0.8.6 model-package contract; see
section 3.2 for loading, precedence, custom profiles, role routing and prompts.

The current shipped budgets are:

| Profile | Verification window | Answer documents | Answer total chars |
| --- | ---: | ---: | ---: |
| Schnell / `sunaq-standard` | 10 | 10 | 25,000 |
| Gründlich / `sunaq-thorough` | 30 | 30 | 100,000 |
| Tief / `sunaq-deep` | 50 | 50 | 200,000 |

All three currently use `max_retrieval_rounds: 1`, Evidence Review off and
planner thinking off. This keeps the current shipped profile comparison primarily focused on breadth and budget.

Administrator-controlled remote hard ceilings remain separate from profile
budgets. Legacy/non-packaged compatibility requests retain the older restrictive
remote caps.

Profiles and prompt packs are read at process startup. Restart API/provider after
editing them. Installer reruns preserve an existing model-package directory as
administrator-owned configuration and add only missing shipped packages.

The SearchSpec path logs the generated `elastic_query`, the actual
Elasticsearch JSON request body and a compact hit list without document contents.


### Qdrant sync

```yaml
sync:
  chunk_size: 3000
  chunk_overlap: 400
  embedding_batch_size: 8
  max_documents: 0
  state_db: "state.sqlite"
```

Semantic corpus filtering operates on text already extracted by Nextcloud/Elasticsearch. The middleware does not decode the source binary files itself.

### Live ACL

```yaml
acl:
  enabled: true
  identity_mode: credential_store
  credential_store: "runtime/users.sqlite"
  verify_tls: true
  timeout: 15
  batch_size: 100
  prefilter:
    enabled: false
```

`credential_store` is the safe multi-user default. `acl-off` is diagnostic only. The API health payload reports `live_acl.enabled` and `identity_mode`; `enabled=true` should be part of acceptance for every shared deployment.

The live authorization implementation performs a WebDAV `SEARCH` scoped to the current user's files and checks candidate `oc:fileid` values. Checks are batched: with the default `batch_size: 100`, 50 file IDs require **one authenticated WebDAV request**, not 50 requests. This makes the ordinary bounded-candidate check comparable to a normal Nextcloud/WebDAV round trip rather than a per-document network loop.

The optional metadata prefilter runs earlier in Elasticsearch and is not an
authorization mechanism. The first implementation evaluates owner/direct-user/
group metadata only; Nextcloud Circles are not yet included. Leave it disabled
where Circle-only shares must remain discoverable. Missing user/group context
causes the prefilter to be skipped, while every final candidate still requires
the live WebDAV check.

Request-dependent `files_accesscontrol` policies can depend on source address, URL, time or user agent. Operators that rely on such rules should include their actual policy shapes in acceptance testing so the middleware request context matches the intended Nextcloud policy.

### Neo4j / Graph

```yaml
neo4j:
  enabled: true
  uri: "bolt://127.0.0.1:7687"
  username: "neo4j"
  password_env: "NEO4J_PASSWORD"
  database: "neo4j"
```

GraphQueue, Entity Discovery and Relation Discovery use separate budgets/thresholds in `config.yaml`.

Neo4j is writable because Graph-Lite stores curation/provenance and optional document observations. It should therefore be treated as sensitive infrastructure and should normally remain loopback/private-network only.

### Profile-local reranker example

The following values belong in the selected `models/<profile>/profile.yaml`, not in global `config.yaml`.

Local:

```yaml
reranker:
  backend: local
  model: "BAAI/bge-reranker-v2-m3"
  device: cpu
  max_length: 512
  batch_size: 4
```

External TEI:

```yaml
reranker:
  backend: tei
  tei_url: "http://127.0.0.1:8081"
  timeout_seconds: 30
  tei_batch_size: 32
  fallback_backend: none
```

Super-Light intentionally disables the reranker:

```yaml
reranker:
  backend: none
  fallback_backend: none
```

This does **not** disable candidate deduplication. `dedup.enabled:true` remains an independent preprocessing step and prevents PDF/ODT/copy variants or near-identical text from consuming multiple candidate slots.

Super-Light uses the same selected SunaQ profile budgets; its deployment override disables unavailable heavyweight capabilities such as local reranking/graph document retrieval rather than defining a separate six-document normal window.

### RetrievalRecord

Disabled by default. Optional configuration:

```yaml
retrieval_record:
  enabled: true
  directory: "runtime/retrieval-records"
```

Records contain structured retrieval/evidence metadata, not complete document bodies.

### Periodic ES → Qdrant synchronization

```yaml
sync_worker:
  enabled: true
  poll_interval_seconds: 300
  max_documents: 0
  enqueue_graph: false
```

The worker periodically invokes the same incremental `rag.sync` implementation used by the CLI. `max_documents: 0` is important for long-running operation; a fixed limit could permanently exclude documents later in the Elasticsearch index. Full graph enqueue remains a separate opt-in.

### Mail

```yaml
mail:
  enabled: false
  state_file: mail_state.sqlite
  poll_interval_seconds: 300
```

Accounts, servers, mailbox roots, target paths and credentials are user-specific in `runtime/users.sqlite` and are managed through Admin UI/CLI. Each configured mailbox is a **recursive root**; the worker uses IMAP `LIST` to discover selectable descendants.

---

## 3.2 SunaQ model packages

Each direct subdirectory below `models/` that contains a valid `profile.yaml`
is discovered at API/provider startup:

```text
models/
  standard/profile.yaml
  thorough/profile.yaml
  deep/profile.yaml
  my-local-profile/profile.yaml
```

The directory name is only a package location. The stable API identity is the
profile `id`; `name` is the user-visible label and `order` controls display
ordering. `enabled: false` skips a package. IDs and aliases must be unique.
Exactly one explicit `default: true` is allowed unless
`SUNAQ_DEFAULT_MODEL` selects the default; otherwise the first ordered model
becomes the default.

A model package can contain:

- the nine request-local configuration sections listed in 3.1;
- `deployment_overrides` for capability-preserving differences such as
  Super-Light disabling graph-document retrieval or a local reranker;
- `roles` for `default`, `planner`, `verifier`, `evidence` and
  `answer`, independently selecting the underlying LLM backend/model;
- `prompts` pointing to files inside that model directory;
- metadata such as `id`, `name`, `description`, `order`, `aliases`,
  `default` and `enabled`.

### Compilation and precedence

Profiles and prompt files are compiled once at process startup. Runtime requests
select an already validated compiled model and do not reread YAML/prompt files.
After editing a model package, restart API/provider before testing it.

For a packaged model the effective request-local configuration is built in this
order:

1. load the global base configuration for infrastructure/background policy;
2. remove the nine profile-owned request-local sections;
3. load those sections from the selected model package;
4. when `legacy_config_overlay: true` is explicitly set, merge matching old
   inline `config.yaml` sections over that model as an upgrade bridge;
5. apply the matching `deployment_overrides.<deployment.profile>` last;
6. compile model-specific role routing and prompt files.

The shipped Schnell profile enables `legacy_config_overlay: true` only to
preserve deliberately tuned 0.8.5 Standard installations. Fresh 0.8.6
`config.yaml` files no longer contain these request-local sections, so the
bridge is inert. Gründlich, Tief and administrator-created profiles are isolated
from legacy global tuning unless they explicitly opt into such behaviour.

If no packaged model exists at all, SunaQ falls back to the legacy single-model
compatibility path and uses global `config.yaml` settings.

### Current rc1.2 shipped profiles

| Profile | Search/final window | Verification window | Answer context |
| --- | ---: | ---: | ---: |
| Schnell / `sunaq-standard` | 10 | 10 | 10 docs / 25k chars |
| Gründlich / `sunaq-thorough` | 30 | 30 | 30 docs / 100k chars |
| Tief / `sunaq-deep` | 50 | 50 | 50 docs / 200k chars |

The profile packages differ in more than the final answer budget: they also own
Elasticsearch/vector candidate limits, verifier/context budgets,
entity-resolution breadth, graph-retrieval budgets and context-enrichment
limits. In rc1.1 all three still keep one retrieval round, Evidence Review off and
planner thinking off, so the current field comparison remains primarily a
breadth/budget experiment.

### LLM role routing inside a profile

The visible SunaQ model is independent from the underlying LLM. For example:

```yaml
roles:
  planner:
    backend: ollama
    base_url: http://127.0.0.1:11434
    model: qwen3:4b
  verifier:
    backend: openai
    base_url: https://provider.example/v1
    model: verifier-model
    api_key_env: VERIFIER_API_KEY
  answer:
    backend: openai
    base_url: https://provider.example/v1
    model: answer-model
    api_key_env: ANSWER_API_KEY
```

Missing role values inherit the established global/role environment defaults.
Secrets are referenced through environment variables; plaintext API keys do not
belong in `profile.yaml`.

### Model-specific prompts

A short prompt filename resolves below
`models/<profile>/prompts/`. The shipped core slots are `planner`,
`verifier`, `evidence` and `answer`. Prompt paths must remain inside the
model directory.

### Remote hard ceilings

Profile budgets are still bounded by administrator-controlled remote hard
ceilings. The packaged-model ceilings are separate from the older legacy caps:

```text
SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT
SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS
SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES
SUNAQ_REMOTE_HARD_VERIFIER_MAX_CHARS_PER_DOCUMENT
SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS
```

These are safety ceilings, not normal profile defaults. The shipped profile
budgets are intended to remain at or below them. Legacy/non-packaged
compatibility requests continue to use the older `REMOTE_*` limits. For upgrade
safety, an explicitly preserved `REMOTE_*` value also remains a tighter cap for
a packaged SunaQ profile; fresh 0.8.6 templates leave those legacy variables
unset.

### Administrator-created profiles

An administrator may copy an existing package, assign a new unique `id` and
`name`, tune its profile-local sections/roles/prompts and restart API/provider.
The registry and SunaQ Admin discover the new package automatically. It is not
automatically granted to existing users; per-user entitlement still has to be
enabled in SunaQ Admin.

Installer reruns treat existing model-package directories as
administrator-owned configuration and do not overwrite them; missing newly
shipped packages may be added.

---

## 3.3 `provider.env` / `runtime.env`

`runtime.env` contains deployment/global service secrets and operational flags. The maintenance-state flag introduced in the RC5 line and retained in rc1.1 is:

```bash
RAG_MAINTENANCE_MODE=true
```

Treat it as wrapper-managed state; use `install/maintenance-mode.sh` to switch modes so process/container lifecycle and readiness handling stay consistent with the flag.


Example OpenAI reference path:

```bash
LLM_BACKEND=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-5.6-luna
FOLLOWUP_MODEL=gpt-5.6-luna
EVIDENCE_MODEL=gpt-5.6-luna
ANSWER_MODEL=gpt-5.6-luna
ANSWER_THINKING=false
```

Secrets belong in `runtime.env`:

```bash
LLM_API_KEY='...'
ELASTICSEARCH_PASSWORD='...'
NEO4J_PASSWORD='...'
WEB_SEARCH_API_KEY='...'
# optional dedicated key for the Web relevance LLM:
WEB_LLM_API_KEY='...'
```

If `WEB_LLM_API_KEY` is empty, the Web relevance path may fall back to the normal `LLM_API_KEY`.

User-bound Nextcloud/IMAP secrets do **not** live in `runtime.env`. The encrypted credential store additionally uses:

```bash
RAG_CREDENTIAL_MASTER_KEY_FILE=/opt/sunaq/runtime/credential-master.key
RAG_CREDENTIAL_ENCRYPTION=required
```

The master key itself is never placed in `runtime.env`; only its path is configured there. Stage-1 encryption still leaves global provider/infrastructure secrets in the protected `0600` environment file.

The provider supports role-specific model routing. The architecture is intentionally model-agnostic: local Ollama models and hosted OpenAI-compatible providers can be mixed per role. Development testing has included Qwen3:8B via Ollama and GPT-5.6 Sol; these are validation points rather than a closed compatibility or quality list.

For native `api.openai.com` + GPT-5.6, the provider selects compatible request parameters such as `reasoning_effort` and `max_completion_tokens` and removes unsupported sampling parameters.

---

## 3.4 `web.yaml`

Current shape:

```yaml
enabled: true

search:
  provider: brave
  url: ""
  api_key_env: "WEB_SEARCH_API_KEY"
  max_results: 10
  timeout: 20
  verify_tls: true

fetch:
  timeout: 25
  verify_tls: true
  concurrency: 4
  max_redirects: 5
  max_bytes: 15000000
  max_text_chars: 300000
  allow_private: false

relevance:
  api_key_env: "WEB_LLM_API_KEY"
  verify_tls: true
  timeout: 180
  num_ctx: 16384
  num_predict: 800
  preview_chars: 4200
  min_score: 0.58
  max_sources: 5

archive:
  enabled: true
  exclude_from_internal_retrieval: true
  write_research_markdown: true
  write_text_snapshot: true
  write_raw_pdf: true
  write_raw_html: false
  write_fetch_log: true
  write_metadata_json: true
  write_rendered_pdf: true
  renderer:
    enabled: false
    url: "http://127.0.0.1:8090/render"
    timeout: 45
    verify_tls: true
    max_bytes: 52428800
    landscape: true
    prefer_css_page_size: false
    viewport:
      width: 1440
      height: 900
    persist_state: true
    cleanup:
      cookie_consent: off
      dismiss_overlays: false
      remove_overlays: false
```

### Brave

`provider: brave` with an empty `url` selects the Brave Web Search endpoint automatically. Key: `WEB_SEARCH_API_KEY`.

### SearXNG

Example:

```yaml
search:
  provider: searxng
  url: "http://127.0.0.1:8080"
  api_key_env: ""
```

SearXNG must support JSON output. The fetch/relevance/archive path is otherwise unchanged.

Keep `fetch.allow_private:false` for public Web research. The restriction applies to fetched target pages, not to a locally operated search provider.

---

# 4. Start, stop and status

```bash
cd /opt/sunaq
./start-all.sh
./status.sh
./stop-all.sh
```

`start-all.sh` starts:

- API;
- Provider;
- Graph Worker only when Neo4j + GraphQueue are enabled;
- ES → Qdrant Sync Worker only when `sync_worker.enabled=true` and Qdrant is enabled;
- Mail Worker only when both `mail.enabled=true` and `mail.worker.enabled=true`.

Individual starts:

```bash
./start-api.sh
./start-openwebui-provider.sh
./start-graph-worker.sh
./start-sync-worker.sh
./start-mail-worker.sh
```

Logs for script-based operation:

```text
log/api.log
log/provider.log
log/graph-worker.log
log/sync-worker.log
log/mail-worker.log
```

---

# 5. User commands in chat

Slash directives must appear at the **beginning** of the request. `/help` intentionally exposes only ordinary user-facing commands.

## 5.1 Quick reference

| Command | Effect |
|---|---|
| `/new` | start a fresh SunaQ conversation context |
| `/web` | public-Web research only |
| `/list` | ranked result list with relevant passage |
| `/force` | bypass the broad/unspecific early stop |
| `/use:...` | directly reuse prior sources or a uniquely named document |
| `/help` | built-in quick reference |

## 5.2 Examples

Normal hybrid search:

```text
What connection exists between Max Example and Example Holdings?
```

Result list:

```text
/list invoices from Example Ltd.
```

Web research:

```text
/web Current public information about Example Ltd.
```

Reuse prior internal sources:

```text
/use:1,2 Compare these two documents.
```

Reuse archived Web sources:

```text
/use:W1,W2 Compare these two sources.
```

Direct file:

```text
/use:"INV-EX-2025-001.odt" Summarize this invoice.
```

## 5.3 Combination rules

- `/use` may only be combined with `/new` in slash mode.
- `/web` is a separate public evidence arm.
- `/force` and `/list` may be combined where meaningful.
- Internal diagnostic/expert directives remain technically available but are not advertised in ordinary help.
- System status is not exposed through ordinary chat; administrators use Admin UI, `status.sh` or protected health endpoints.

---

# 6. Natural control instructions in a leading parenthesized block

More complex workflows can be expressed in **one leading parenthesized block**. The remaining text is the task itself.

Example:

```text
(Use document 2 and then search the Web)
Compare the statements in it with public sources.
```

The instruction compiler may choose only from a closed action set:

- reuse previous sources;
- new internal search: `auto`, `files/vector/graph` or `elastic`;
- Web on/off;
- Web in parallel or after internal evidence;
- ranked/raw list;
- `/force` equivalent;
- context reset.

Examples:

```text
(Search internally and then on the Web)
Compare internal information about Company X with the current public record.
```

```text
(Use these documents and then search the Web)
Check the people and companies mentioned in them against public sources.
```

```text
(Search documents and vector only)
Where is an unauthorized business address discussed?
```

```text
(Search only on the Web)
Michaela Example
```

### Limitation

Direct document selection **plus a simultaneous new internal search** is not supported in the current snapshot. Direct document selection + Web is supported.

A leading `/` always activates slash-directive mode; a later parenthesized expression is ordinary user text.

---

# 7. Web workflows

## 7.1 Explicit Web-only

```text
/web <research task>
```

Flow:

1. Brave/SearXNG searches.
2. Target pages are fetched.
3. Reranker/passage selection identifies a meaningful passage per page.
4. Relevance LLM evaluates each fetched source.
5. Only sources above `min_score` with `relevant=true` become evidence.
6. The answer cites `[W1]`, `[W2]`, ...
7. Optional archive is written.

Search-engine snippets are never treated as answer evidence.

## 7.2 Compare internal information with the Web

Recommended:

```text
(Search internally and then on the Web)
Find XY and compare the internal information with public sources.
```

For `web_timing=after`, the provider may derive up to three Web queries from already authorized/selected internal evidence.

This is an explicit **egress boundary**. The Web-query helper treats internal material as untrusted evidence and is instructed not to copy passwords, API keys, tokens, e-mail addresses, long random strings, internal contract/case identifiers or other unusual verbatim identifiers into public search queries unless the user explicitly asks to search for that exact value.

## 7.3 Check a specific document against the Web

```text
(Use document 2 and then search the Web)
Compare the statements with public sources.
```

No new internal search is performed. Document 2 is direct internal evidence. The Web-query helper may use public names, companies, places or relationships from that evidence as search anchors subject to the egress rules above.

## 7.4 Automatic Web fallback

Trusted frontends may allow public Web evidence when internal evidence is insufficient. Requirements:

1. `web.yaml: enabled: true`;
2. Web Research enabled in Admin UI for the canonical user;
3. the trusted integration sends:

```http
X-RAG-Web-Allowed: true
```

4. the conservative Web gate returns `use_web=true`.

An internal null result therefore does not automatically trigger a public request.

---

# 8. Web archive

Multi-user archive targets are stored per canonical user in Admin UI/`users.sqlite`. Nextcloud WebDAV remains the write-authorization authority.

Layout:

```text
<archive-root>/2026-09/09-191542-abcd/
    recherche.md
    fetch-log.jsonl
    01-source.txt
    .01-source.metadata.json
    01-source.pdf
    02-source.txt
    .02-source.metadata.json
    02-source.pdf
```

The `.txt` snapshot includes, among other fields:

- final URL;
- original URL;
- title;
- publisher;
- publication date when detected;
- retrieval timestamp;
- content type;
- SHA hash;
- search provider;
- search rank;
- relevance score/reason;
- query;
- extracted text.

`fetch-log.jsonl` writes one JSON record for every search result actually requested, including requested/final URL, HTTP status, redirect count, fetch error, content hash and the full relevance decision. Rejected research paths therefore remain auditable.

`write_metadata_json:true` creates a hidden metadata sidecar (`.NN-source.metadata.json`) for each selected source.

When `archive.renderer.enabled:true`, selected HTML sources are rendered after the synchronous text/metadata archive step through a bounded in-process background queue. The sidecar moves from `render.status=pending` to `complete` or `failed`. Renderer failures are fail-open and do not change text evidence or the already delivered answer.

The shared Playwright renderer defaults to a 1440×900 desktop viewport and A4 Landscape. Optional consent/overlay cleanup is deliberately best-effort. With `persist_state:true`, browser storage is reused per requested host so ordinary cookie choices need not be repeated every run.

The archive renderer is intended for public sources. Login walls, paywalls, CAPTCHAs and access controls are not bypassed.

`write_raw_html:true` stores the main original HTML response. This is **not** a complete WARC/browser capture with subresources. A Playwright PDF is also only a visual snapshot, not a forensic capture.

Archived `.txt` sources can be reused immediately through `/use:W1` without waiting for later Elasticsearch synchronization. Access again uses the current user's WebDAV credential.

---

# 9. Trusted clients and user identity

Provider Bearer keys identify trusted frontend/integration **clients**, not people.

```text
scoped identity = client_id::external_user_id
canonical user  = (nextcloud_server, nextcloud_login)
```

A user can reach the same canonical Nextcloud account through multiple Trusted Clients.

External OpenWebUI example:

```json
{
  "X-OpenWebUI-User-Id": "{{USER_ID}}"
}
```

Automatic Web fallback additionally:

```json
{
  "X-OpenWebUI-User-Id": "{{USER_ID}}",
  "X-RAG-Web-Allowed": "true"
}
```

Every frontend receives its own provider-client key.

### Security boundary

The frontend-supplied external user ID is intentionally trusted **only after** the provider client has authenticated. It is then scoped as `client_id::external_user_id` and used to select the server-side credential binding.

Consequently, a provider-client key must be treated as a credential for a trusted integration server. If an attacker obtains that key and can reach the provider, they may submit an external user ID corresponding to another binding inside the same client scope and thereby cause requests to use that user's stored Nextcloud credential.

Operational rules:

- keep the provider-client key on the integration server; do not expose it to browser JavaScript;
- issue a separate key per integration;
- rotate/disable a client immediately after suspected exposure;
- bind the provider to loopback/private networks where possible;
- when an external integration must reach it, use reverse-proxy source-IP/network allowlists and, where appropriate, mTLS or an equivalent second network-level control;
- do not treat an arbitrary public OpenAI-compatible client as trusted merely because it can send an `Authorization` header.

This boundary allows SunaQ to remain UI-agnostic and to be combined with other local RAG systems, agents or tools, but only when the administrator deliberately grants that integration access.

---

# 10. Administration CLI

All examples assume `/opt/sunaq`:

```bash
cd /opt/sunaq
```

## 10.1 Trusted Provider Clients

Module:

```bash
sudo -u rag ./.venv/bin/python -m rag.provider_clients ...
```

Commands:

```text
list
create <client_id> [--name NAME]
rotate <client_id> [--name NAME]
enable <client_id>
disable <client_id>
delete <client_id>
```

Examples:

```bash
sudo -u rag ./.venv/bin/python -m rag.provider_clients list
sudo -u rag ./.venv/bin/python -m rag.provider_clients create openwebui-office --name "OpenWebUI Office"
sudo -u rag ./.venv/bin/python -m rag.provider_clients rotate openwebui-office
sudo -u rag ./.venv/bin/python -m rag.provider_clients disable openwebui-office
```

Create/Rotate prints the new API key **once**; only its hash is stored.

`delete` removes the client and client-bound identity/Nextcloud-credential data. Canonical users and mail/Web settings remain when still referenced independently.

## 10.2 Canonical users

```bash
sudo -u rag ./.venv/bin/python -m rag.user_admin list
sudo -u rag ./.venv/bin/python -m rag.user_admin show <login> [--server URL]
sudo -u rag ./.venv/bin/python -m rag.user_admin reauth <login> [--server URL]
sudo -u rag ./.venv/bin/python -m rag.user_admin set-mail-password <login> [--server URL] [--account-id ID]
```

`reauth` removes Nextcloud app passwords and pending Login Flows; the canonical user and mail/Web settings remain.

### Legacy/advanced credential CLI

```bash
sudo -u rag ./.venv/bin/python -m rag.user_credentials list-users
sudo -u rag ./.venv/bin/python -m rag.user_credentials set-nextcloud <user_id> <username> --client <client> --server <url>
sudo -u rag ./.venv/bin/python -m rag.user_credentials delete-nextcloud <user_id> --client <client>
```

This CLI is intended for manual/diagnostic bindings. Nextcloud Login Flow v2 is preferred for ordinary use.

---

# 11. Sync and retrieval diagnostics

## 11.1 Elasticsearch → Qdrant sync

```bash
sudo -u rag ./.venv/bin/python -m rag.sync [options]
```

Options:

```text
-c, --config FILE
--include-path PREFIX       repeatable; overrides sync.include_paths
--exclude-path PREFIX       repeatable; overrides sync.exclude_paths
--max-documents N           0 = unlimited
--dry-run                   no writes to Qdrant/state/queue
--log-level LEVEL
--enqueue-graph             enqueue new/changed documents for Graph processing
--no-enqueue-graph          disable Graph enqueue for this run
```

Examples:

```bash
sudo -u rag ./.venv/bin/python -m rag.sync --dry-run --include-path Example
sudo -u rag ./.venv/bin/python -m rag.sync --include-path Example --max-documents 500
```

In normal operation `start-sync-worker.sh` runs this periodically. It is not a second synchronization implementation; state, chunking, embeddings and Qdrant lifecycle remain in `rag.sync`.

## 11.2 Qdrant/embedding smoke test

```bash
sudo -u rag ./.venv/bin/python -m rag.qdrant_smoke -c config.yaml
sudo -u rag ./.venv/bin/python -m rag.qdrant_smoke -c config.yaml --json
```

The probe creates an embedding and checks Qdrant/collection. Treat the embedding backend and Qdrant as separate failure domains.

## 11.3 Live-ACL smoke test

```bash
sudo -u rag ./.venv/bin/python -m rag.acl_smoke files:42040 files:66732 --user-id 'client::userid'
```

Numeric IDs are normalized automatically to `files:<id>`.

## 11.4 Manual hybrid search

```bash
sudo -u rag ./.venv/bin/python -m rag.hybrid_search \
  "unauthorized access" \
  --must business-address \
  --should registered-office \
  --phrase "prohibition of use" \
  --limit 10
```

Options:

```text
semantic                         optional semantic query
--must TERM                      repeatable
--should TERM                    repeatable
--not TERM                       repeatable
--phrase PHRASE                  repeatable
--from-date YYYY-MM-DD
--to-date YYYY-MM-DD
--limit N
--es-limit N
--vector-limit N
--threshold FLOAT
```

---

# 12. Graph administration

Main module:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph --config config.yaml <command>
```

## 12.1 Basics and inspection

```text
check
init
stats
candidates
find <query> [--limit N]
merges [query] [--limit N]
entity --entity <ENTITY_ID>
observations --entity <ENTITY_ID>
forms --entity <ENTITY_ID>
blocked-entities
```

## 12.2 Candidates/backfills

```text
refresh-candidates [--max-candidates N]
identity-backfill
curation-backfill
form-policy-backfill
```

These are conservative maintenance operations. `identity-backfill` does not perform automatic Entity merges.

## 12.3 Merge and negative identity

Preview is the default:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph merge \
  --keep <ENTITY_A> --merge <ENTITY_B>
```

Execute:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph merge \
  --keep <ENTITY_A> --merge <ENTITY_B> --alias-policy contextual --yes
```

Permanently reject a merge:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph reject-merge \
  --left <ENTITY_A> --right <ENTITY_B> --reason manual_rejection --yes
```

## 12.4 Names, aliases and policies

```bash
sudo -u rag ./.venv/bin/python -m rag.graph correct-name \
  --entity <ID> --name "Max Alexander Example" --yes

sudo -u rag ./.venv/bin/python -m rag.graph add-alias \
  --entity <ID> --alias "Max Example" --policy contextual --weight 0.95 --yes

sudo -u rag ./.venv/bin/python -m rag.graph remove-alias \
  --entity <ID> --alias "M. Example" --yes

sudo -u rag ./.venv/bin/python -m rag.graph set-form-policy \
  --entity <ID> --form "M. Example" --policy search_only --yes
```

Policies:

| Policy | Effect |
|---|---|
| `exclusive` | query + hard ingestion identity resolution |
| `contextual` | query/candidate use; no hard ingestion resolution |
| `search_only` | query expansion only |
| `document_only` | not exposed as a global resolver/search form |

## 12.5 Correct an observation / remove a non-entity

```bash
sudo -u rag ./.venv/bin/python -m rag.graph correct-observation \
  --observation <OBS_ID> --entity <TARGET_ENTITY> --reason ocr --yes

sudo -u rag ./.venv/bin/python -m rag.graph delete-entity \
  --entity <ID> --reason manual_not_an_entity --yes
```

`delete-entity` preserves the underlying observations as rejected evidence instead of erasing provenance.

## 12.6 CardDAV provenance

```text
contact-sources
contact-import-runs [--limit N]
contacts [--cloud ID] [--source-user ID] [--addressbook NAME] [--import-run ID] [--limit N]
contact-provenance-backfill [--cloud ID] [--source-user ID]
rollback-contacts [scope...] [--priority high|normal|background] [--no-relink] [--yes]
reassign-contact --contact <CONTACT_ID> --entity <ENTITY_ID> [--priority ...] [--no-relink] [--yes]
```

## 12.7 Full Graph reset

```bash
sudo -u rag ./.venv/bin/python -m rag.graph reset --yes-really-delete-all
```

This command deletes **all** Graph data and intentionally requires a dedicated confirmation switch.

---

# 13. GraphQueue and GraphWorker

Queue status:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_queue stats
sudo -u rag ./.venv/bin/python -m rag.graph_queue recent --limit 20
```

Preview a path enqueue:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_queue enqueue-path \
  Example/Investments --priority normal
```

Execute:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_queue enqueue-path \
  Example/Investments \
  --exclude-path Example/Investments/Archive \
  --priority background --yes
```

Worker:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_worker
sudo -u rag ./.venv/bin/python -m rag.graph_worker --once
sudo -u rag ./.venv/bin/python -m rag.graph_worker --once --ignore-idle
```

The worker honors configured idle-time and quiet-hour rules by default.

---

# 14. Graph indexing/rebuild

Index one document manually:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_indexer \
  --document files:66732 \
  --query "manual graph analysis"
```

Options:

```text
--document ID       repeatable; required
--query TEXT
--no-fuzzy
--no-discovery      no LLM entity discovery; relink known entities only
--no-relations      no LLM relation/claim discovery
--force             reprocess unchanged hashes
```

Replay queued evidence through Graph v3:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_rebuild --phase both
```

Options:

```text
--phase discover|relink|relations|both
--limit N
--offset N
--document ID       repeatable
--sleep SECONDS
--log-level LEVEL
--force
--force-oversize
```

Suggest Entity duplicates without modifying data:

```bash
sudo -u rag ./.venv/bin/python -m rag.identity_suggestions \
  --type Person --min-score 0.70 --limit 50
```

Scores are triage heuristics, not identity probabilities.

Diagnose query-entity resolution:

```bash
sudo -u rag ./.venv/bin/python -m rag.graph_entities \
  "What connection exists between Max Example and Example Holdings?"
```

Options: `--no-fuzzy`, `--fuzzy-threshold`, `--fuzzy-max-candidates`.

---

# 15. CardDAV sync / contact seeds

In multi-user mode, CardDAV is administered through the verified Nextcloud account. Internal `canonical_user_id` is only a join key; UI and CLI use `nextcloud_login`, and `--server` disambiguates identical logins on multiple Nextcloud instances.

The Nextcloud credential already created through Login Flow is reused.

Admin UI:

```text
SunaQ Admin -> Users -> <Nextcloud login> -> Contact DB
```

Native CLI:

```bash
sudo -u rag ./.venv/bin/python -m rag.contacts list
sudo -u rag ./.venv/bin/python -m rag.contacts status --user alice
sudo -u rag ./.venv/bin/python -m rag.contacts books --user alice
sudo -u rag ./.venv/bin/python -m rag.contacts sync --user alice
```

Dockerized Super-Light:

```bash
cd /opt/sunaq/install/super-light
./contacts.sh list
./contacts.sh status --user alice
./contacts.sh books --user alice
./contacts.sh sync --user alice
```

Sync options: `--dry-run`, `--limit`, `--force`; use `--server URL` when the same login exists on multiple Nextcloud instances.

Missing credentials, disabled user/contact source or an empty address book produce a no-op rather than a stack failure. The Admin action starts a background job and exposes progress. Available address books are discovered with display name and technical slug.

After a complete successful run, vanished CardDAV hrefs are reconciled from ContactRecords. Limited/dry-run/failed scans do not perform source deletion.

ContactRecord provenance remains externally traceable through Nextcloud instance (`cloud_id`), `source_user_id`/login, address book and vCard UID. The canonical UUID is not used as the business source identity.

The historical `python -m rag.carddav_sync` path with environment credentials remains single-user compatibility only.

---

# 16. Mail sync

The long-running scheduler is deployment-neutral: native/systemd starts `.venv/bin/python -m rag.mail_worker`; Docker starts `python -m rag.mail_worker`.

Globally, `config.yaml: mail.enabled=true` is required and the canonical user must also have an enabled mail account.

```bash
sudo -u rag ./.venv/bin/python -m rag.mail_sync
```

Filters:

```text
--user <login|canonical_user_id>
--account <account_id|name>
--mailbox <IMAP-folder>       this mailbox as recursive root
--max-messages N             limit per actually synchronized mailbox
--dry-run
```

Example:

```bash
sudo -u rag ./.venv/bin/python -m rag.mail_sync \
  --user demo-user --mailbox INBOX --max-messages 50 --dry-run
```

## Recursive mailboxes

Configured `mailboxes` are roots, not a static list. Before each account run the middleware uses IMAP `LIST` and synchronizes the root plus all selectable descendants. `\Noselect` containers are skipped while selectable children remain included. The hierarchy delimiter reported by the server is honored.

Example:

```text
<target>/<account>/INBOX/Projects/2026/<year>/<month>/...
```

For classic IMAP4rev1, non-ASCII mailbox names are handled as Modified UTF-7.

The Admin action **Test connection & discover mailboxes** displays visible mailbox names, flags and hierarchy delimiter using the same discovery path as the sync worker.

## One directory per message

New imports use the directory-per-message layout:

```text
<target>/<account>/<mailbox-hierarchy>/<year>/<month>/
  <timestamp>_<uid>_<subject>/
    mail.txt
    .mailmeta.json
    a01_<attachment>
    a02_<attachment>
    ...
    message.eml              # optional
```

`mail.txt` is the indexable normalized representation. `.mailmeta.json` contains deterministic mail/thread metadata, IMAP UID/UIDVALIDITY, import time, selected transport/authentication headers and SHA-256/byte length of the raw message retrieved from IMAP.

Attachments remain original files. New accounts do **not** store redundant `message.eml` by default; `store_eml=true` remains an explicit forensic/raw-message retention option.

Legacy flat archives remain readable through the sidecar reader but are not moved automatically. For a clean rebuild, use a new/empty target path and deliberately reset the relevant mail state.

## Follow-up synchronization to Elasticsearch/Qdrant

Mail sync may still trigger Nextcloud FullTextSearch for monthly directories that were actually written. The generic ES → Qdrant reconciliation should then be performed by the dedicated `sync_worker`, so mail and all other new/changed Nextcloud documents share one vector lifecycle.

---

# 17. Reset semantic data

```bash
./reset-rag.sh
```

shows a warning only. Execute:

```bash
./reset-rag.sh --yes
```

Deleted:

- local SQLite sync state;
- configured Qdrant collection.

**Not changed:** Elasticsearch and Nextcloud.

---

# 18. HTTP API quick reference

Internal API, normally bound to `127.0.0.1:8765`. The zone labels below are enforced by central dependencies; callers should use the provider or bundled admin/proxy paths rather than constructing internal headers manually.

```text
GET    /live                              PUBLIC
GET    /health                            INTERNAL
POST   /auth/nextcloud/start              TRUSTED_PROVIDER
POST   /auth/nextcloud/ensure             TRUSTED_PROVIDER
GET    /auth/nextcloud/status/{flow_id}   TRUSTED_PROVIDER
DELETE /auth/nextcloud/{rag_user_id}       ADMIN
POST   /web/search                         USER
POST   /web/archive/finalize               USER
POST   /query-context                      TRUSTED_PROVIDER
POST   /plan                               TRUSTED_PROVIDER
GET    /graph/stats                        ADMIN
POST   /graph/document                     ADMIN
POST   /graph/enqueue-evidence             INTERNAL
POST   /graph/research-findings            INTERNAL
GET    /graph/queue/stats                  ADMIN
GET    /graph/queue/jobs                   ADMIN
POST   /graph/index-evidence               INTERNAL
POST   /documents/resolve                  USER
POST   /elastic/search                     USER
POST   /multi-search                       USER
POST   /search                             USER
```

These endpoints are primarily internal Provider/Admin interfaces. Ordinary users talk to the OpenAI-compatible provider under `/v1/`.

---

# 19. Security and credentials

## 19.1 Encrypted `runtime/users.sqlite` store

Important tables:

```text
provider_clients
canonical_users
identity_bindings
credentials
nextcloud_login_flows
mail_accounts
user_web_settings
contact_sync_settings
web_archive_roots
store_meta
```

Credential primary keys include `rag_user_id`, `service` and `account_id`. Never update credentials by username alone with ad-hoc SQL.

Reversible values in `credentials` (`nextcloud`, `mail_imap`), Login Flow poll tokens and temporary curation-session app passwords are encrypted with **AES-256-GCM**. The stored format is versioned (`enc:v1:`). Additional Authenticated Data binds ciphertext to its semantic identity; copying ciphertext to another user/service/account therefore fails authentication.

Trusted-client keys in `provider_clients` are non-reversible and stored only as SHA-256 digests.

## 19.2 Master key and operating modes

Fresh install:

```text
runtime/credential-master.key   root:rag 0640
runtime/users.sqlite            rag:rag 0600
```

Configuration:

```bash
RAG_CREDENTIAL_MASTER_KEY_FILE=/opt/sunaq/runtime/credential-master.key
RAG_CREDENTIAL_ENCRYPTION=required
```

Modes:

- `required`: production; plaintext credential reads and missing/unsafe master key fail closed;
- `preferred`: migration/development; use an available key and allow controlled migration of legacy plaintext;
- `disabled`: diagnostic/legacy only; new secrets remain plaintext and this mode is not for production.

Back up the master key separately but together with the encrypted database. A `users.sqlite` backup without the corresponding master key cannot restore encrypted credentials.

## 19.3 Secret administration

```bash
cd /opt/sunaq
./.venv/bin/python -m rag.secret_admin status
sudo ./.venv/bin/python -m rag.secret_admin init-key --group rag
sudo -u rag ./.venv/bin/python -m rag.secret_admin migrate
sudo -u rag ./.venv/bin/python -m rag.secret_admin verify
```

`status`, `migrate` and `verify` never print secret values. `verify` exits with code 2 when plaintext remnants or decryption failures remain.

The Admin UI exposes status only under `/rag-admin/security`. Stored IMAP passwords are never returned as form values.

## 19.4 Limits of stage-1 encryption

The `rag` service account must be able to read the master key during operation. Stage 1 protects SQLite/backup/admin handling but does not defend against `root` or a fully compromised `rag` process.

Global secrets such as `LLM_API_KEY`, `WEB_SEARCH_API_KEY`, `ELASTICSEARCH_PASSWORD`, `NEO4J_PASSWORD` and `RAG_ADMIN_PASSWORD` remain in `runtime.env`. A later stage may move them behind the same secret abstraction and optionally use systemd credentials/TPM or a privileged helper on modern hosts.

## 19.5 TLS

Nextcloud TLS verification is the default. `nextcloud.verify_tls` and
`nextcloud.ca_file` are the canonical policy for Login Flow, live ACL, CardDAV,
mail WebDAV and web archive. `security.allow_insecure_nextcloud=true` is a lab
escape hatch only.

Both supported installers accept repeatable `--ca-certificate <PEM>` options.
They build a Nextcloud-specific CA bundle and point `nextcloud.ca_file` at it;
the native profile does not set global `SSL_CERT_FILE` or
`REQUESTS_CA_BUNDLE`, so unrelated public HTTPS clients retain their normal
trust store. Super-Light additionally adds supplied anchors to its container
system trust bundle. Private CAs for Elasticsearch/LLM/Web remain independently
configurable through their corresponding CA/verify settings.

## 19.6 Untrusted evidence and prompt injection

Retrieved text is data, not executable control input. The normal LLM path cannot emit arbitrary Elasticsearch DSL, Cypher, SQL or shell commands for execution. SearchSpec normalization, structured verifier output, Graph schemas/ontology, fixed budgets and live ACL substantially narrow indirect prompt-injection impact.

The main residual risks are:

- answer/evidence integrity: a model may misinterpret malicious instructions embedded in a document;
- persistent Graph/Finding pollution if manipulated text produces a formally valid but misleading observation;
- external information egress when Web-after derives public search queries from internal evidence.

Evidence-bearing prompts therefore explicitly instruct models never to follow instructions contained inside documents, mail, archived chats or Web pages.

Infrastructure least privilege remains separate from prompt handling: use read-only Elasticsearch credentials for retrieval, restrict Qdrant/Neo4j by network exposure and use backend-specific read-only credentials where supported.

---

# 20. OpenWebUI and alternative frontends

Pinned bundle version in `versions.lock.yaml`:

```text
ghcr.io/open-webui/open-webui:v0.11.0
```

The installer provides OpenWebUI and binds it loopback to the Provider. The planned restrictive "frontend-only" preconfiguration is not yet complete. Administrators should disable unrelated OpenWebUI RAG/Knowledge, tool, plugin, Web search, update and workspace capabilities when those are not intended.

OpenWebUI follow-up helper requests are suppressed provider-side and do not invoke an LLM.

OpenWebUI can also be embedded/navigated from Nextcloud without special middleware support.

SunaQ is UI-agnostic. Any integration that implements the OpenAI-compatible request contract can use the Provider **if the administrator creates a Trusted Client for it**. This makes composition with other local RAG systems, agents and research tools possible, but the Trusted Client boundary in section 9 applies: provider keys belong on trusted integration servers and should be network-restricted when exposed beyond loopback.

---

# 21. Health and troubleshooting

Overall status:

```bash
./status.sh
```

Direct endpoints:

```bash
curl -s http://127.0.0.1:8765/health | jq
curl -s http://127.0.0.1:8766/live | jq      # cheap provider liveness, no remote LLM probe
curl -s http://127.0.0.1:8766/health | jq    # explicit provider diagnostics; may probe configured LLM backends
```

## Web

If `/web` reports that no relevant fetchable source was found:

1. inspect API log, not only Provider log;
2. compare search count vs. fetch count;
3. inspect Web relevance decisions;
4. verify `WEB_SEARCH_API_KEY` and optional `WEB_LLM_API_KEY`;
5. diagnose Brave 401/429/5xx responses;
6. for fetch failures, inspect TLS, redirects, content type and SSRF/private-target rules.

The Web relevance step expects complete structured-output decisions for all loaded sources. Incomplete outputs are retried and then surfaced as explicit errors.

## Vector/Qdrant

```bash
sudo -u rag ./.venv/bin/python -m rag.qdrant_smoke --json
```

Qdrant and the embedding backend are separate failure domains. A known diagnostic rough edge is that some vector failures may still be summarized as `vector unavailable`; a true zero-hit result should not be interpreted as backend failure.

## ACL

```bash
sudo -u rag ./.venv/bin/python -m rag.acl_smoke files:<id> --user-id '<client>::<user>'
```

Do not compensate ACL denials with adaptive backfill. The current normal path filters a bounded ranking window. Acceptance tests with narrow rights should check both "no leak" and the resulting recall/result count.

For latency measurements, record the WebDAV SEARCH time separately from total answer time. With the default batch size, up to 100 unique candidate file IDs are checked in one request; benchmarking 10/50/100 candidates on the actual Nextcloud instance is more meaningful than extrapolating from per-file WebDAV operations.

---

# 22. Current feature/freeze status

**Implemented in the current 0.8.6-rc1.2 candidate:**

- common middleware core with the supported mappings `standard+native` and
  `super-light+dockerized`;
- SunaQ research profiles Schnell/Gründlich/Tief with per-user entitlement,
  request-local budgets, prompt packs and role-specific LLM routing;
- Query Rewriter/SearchSpec + Elasticsearch, optional Qdrant and Neo4j
  seed/alias expansion;
- optional Elasticsearch ACL metadata prefilter over owner/direct-user/group
  metadata, with live Nextcloud WebDAV ACL remaining the final authorization
  boundary;
- bounded Candidate Verification without adaptive post-denial backfill;
- deterministic follow-up actions and identity-scoped request progress;
- SunaQ Recherche **0.3.4** for Nextcloud 23+;
- Brave/SearXNG Web Research and optional Nextcloud-backed Mail/Web/Chat archives;
- authenticated source-capability filtering: optional sources are available only
  while the global service gate and the canonical user's gate are both enabled,
  and unavailable controls stay hidden in the bundled client;
- Graph-Lite Findings/curation, CardDAV seeds and multi-user Login Flow;
- one administrator-selected chat-archive path plus per-user enable/disable gate
  below the global `chat_archive.enabled` switch;
- HTTPS-by-default credential-bearing SunaQ Recherche transport;
- fresh-install minimal defaults: Web/Web archive, chat-archive evidence/writes,
  Research-Finding persistence and mail worker remain off until enabled;
- enforced SRC/ERG architecture tiers, including mandatory ACL/Documents-only
  SRC invariants, safe core/workgroup presets and client capability publication;
- structured JSON evidence with an immutable server-side untrusted-evidence
  guard, SSRF/internal-network Web/Playwright protection and file-backed service
  secret handling;
- maintenance/recovery tooling inherited from earlier release candidates and
  retained in the current line.

**Intentionally outside the current rc1.2 baseline:**

- authoritative server-side conversation state (rc2);
- configured policy/inspection adapters for the rc1.1 hook scaffold;
- evaluated/tuned additional profile-specific retrieval rounds (rc2);
- planner/model Thinking as a default profile differentiator;
- a generic side-effect/action execution layer;
- complete browser/WARC/WACZ capture;
- automatic global fact materialization from QueryFrames;
- `standard+dockerized` as a released deployment path.

See `models/README.md`, `SRC-ERG.md`, `KNOWN-LIMITATIONS.md` and `ROADMAP.md`.

### Policy/inspection hook scaffold

`rag/policy_hooks.py` defines the common `ALLOW | BLOCK | QUARANTINE | MODIFY`
contract and the stages `outbound_query`, `pre_fetch`, `post_fetch`,
`pre_persist` and `pre_model_egress`. The rc1.1/rc1.2 line wires these into Web
search/fetch/Playwright, Web-archive writes, IMAP message/attachment import,
Mail-archive writes and LLM/embedding backend calls.

No evaluator is installed by default, so every hook is an ALLOW-only no-op.
Model/embedding hooks are invoked at the backend boundary for both local and
remote endpoints; evaluator metadata includes backend/base URL/model so a later
policy can distinguish trust zones without duplicating endpoint classification
inside each caller.

---

# 23. Release validation

The rc1.2 candidate retains the release-tested 0.8.6-rc1/rc1.1 behavior where
compatible and adds boundary-focused regression coverage for:
JSON evidence isolation and immutable evidence guards, Web/Playwright SSRF
controls, file-backed secret loading, SRC/ERG startup/request invariants,
Documents-only deterministic SRC lookup paths and safe capability preset parsing.

The current branch is not considered release-validated merely because individual
development commits pass tests. Promotion requires the final repository CI,
manifest/hygiene checks and review state to be green on the release head, followed
by the intended installation/acceptance checks for the supported deployment path.

Registry images recorded in the Compose lock set are digest-pinned. The locally
built Playwright renderer currently uses a version-tag-pinned Microsoft base image
rather than an immutable base-image digest; this is documented deferred hardening
in `KNOWN-LIMITATIONS.md`. Secrets are not part of the package.

## Role-specific LLM routing

Canonical `LLM_*` variables remain the compatibility default. Optional role prefixes:

```text
PLANNER_LLM_*
VERIFIER_LLM_*
EVIDENCE_LLM_*
ANSWER_LLM_*
```

Each role accepts `BACKEND`, `BASE_URL`, `MODEL`, `API_KEY`, `VERIFY_TLS`, `CA_FILE` and `SCOPE` (`local|remote`). Unset values inherit the default. Provider `/health` reports effective routing and scope.

Remote evidence budgets:

```text
REMOTE_LLM_MAX_CHARS_PER_DOCUMENT
REMOTE_LLM_MAX_TOTAL_CHARS
REMOTE_VERIFIER_MAX_CANDIDATES
REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT
REMOTE_ANSWER_MAX_DOCUMENTS
```

The Graph Worker is controlled by `graph_queue.worker.enabled`; cited documents are automatically queued only when `graph_queue.auto_enqueue_cited_documents=true`.

Mail background polling is controlled independently by `mail.worker.enabled`.

Web-archive TLS verification is separate from Search/Fetch/Relevance TLS and is configured through `web.yaml: archive.verify_tls`.

Raw HTML is not archived by default (`write_raw_html: false`); text snapshots and raw PDFs remain available as provenance.

## SunaQ Recherche Findings

`POST /graph/research-findings` accepts already structured Query-Rewriter/Verifier output and starts neither a Graph Worker nor an additional LLM call. Only entries with `verification_status=match` and `relation_binding=direct` are persisted.

Configuration:

```yaml
research_findings:
  # Fresh-install default: false. Enable only after accepting the durable
  # Findings/curation lifecycle.
  enabled: false
  timeout: 5
  max_documents_per_request: 30
  curation:
    admin_user_context: true
    user_self_service: false
    session_max_seconds: 7200
```

The provenance/curation model separates the concrete research run from the shared Finding:

```text
(:CanonicalUser)-[:PERFORMED]->(:ResearchRun)-[:PRODUCED]->(:ResearchFinding)
(:ResearchFinding)-[:SUPPORTED_BY]->(:Document)
(:ResearchFinding)-[:QUERY_ENTITY {role, text, resolution}]->(:Entity)
```

`ResearchRun` stores the originating user/query/runtime provenance. A run or selected `PRODUCED` edges can be dismissed from the curation queue without deleting the shared Finding or its provenance.

The legacy-compatible `finding_id` remains deterministic from provenance, document ID and the complete canonical QueryFrame. A separate `curation_hash` covers the structured Entities, relations, constraints and concepts while excluding free-form `intent` wording. Persistence first looks for an existing Finding on the same supporting document with that curation fingerprint, allowing equivalent provider/chat runs to reuse one global curation decision without changing existing Finding IDs during upgrade.

Admin curation is user-context scoped. The SunaQ Admin selects a canonical Nextcloud user; the backend requires that the selected user actually produced the Finding and re-checks the supporting document through the user's current live Nextcloud ACL before returning Finding evidence. Unauthorized Findings are omitted from lists and counts.

The RC5 line introduced lazy cleanup at these Finding ACL-filter points; rc1.1 retains the same bounded behavior. When a successful ACL check definitively denies a numeric Nextcloud `files:<id>`, the graph layer may remove the selected/current user's `PRODUCED` edges for Findings that are still completely uncurated. The shared Finding is deleted only after its last ResearchRun reference disappears. Finding curator status/suppression, `CURATED_ENTITY` edges or any Finding-derived RelationObservation prevent deletion. ACL/backend/TLS/network/credential errors and non-numeric/non-Nextcloud identifiers never trigger cleanup. This mechanism is not called by Qdrant sync and does not replace a future explicit cross-store `purge-document` workflow.

Optional end-user curation is exposed at `/curation/`. It uses Nextcloud Login Flow v2 once per curation session and creates no separate SunaQ password. The returned app password is stored only in the encrypted `curation_sessions` table, not in the normal provider credential namespace and not in `identity_bindings`.

A curation session has a configurable **absolute** maximum age; default is 7200 seconds. Request activity does not extend expiry. The browser receives a random HttpOnly/Secure/SameSite=Strict session cookie, while SQLite stores only its hash. State-changing requests require a session-bound CSRF token.

Every request re-checks session expiry, canonical-user enablement, per-user curation permission and live Nextcloud ACL. Logout or expiry invalidates the local session before app-password revocation is attempted. Failed revocations remain `revocation_pending` and cannot authenticate. API startup invalidates all surviving curation sessions and retries revocation.

Shared Entity/Finding/RelationObservation curation records the curator actor for audit provenance. The knowledge decision remains global, while visibility of supporting evidence remains per-user and live-ACL-controlled.

---

# 24. Security and lifecycle documents

For operational and review questions also see:

- `docs/THREAT-MODEL.md` — shared-alias vs. evidence boundary, ACL ordering, prompt injection, archives and Findings;
- `docs/DATA-LIFECYCLE.md` — deletion, retention, backup/restore and master-key lifecycle;
- `docs/NEXTCLOUD-CONTEXT-CHAT.md` — neutral comparison with Nextcloud's native Context Chat architecture.

## Lexical query semantics and hybrid correction

The Elasticsearch files arm intentionally preserves Nextcloud-style lexical
syntax. Unquoted words are independent lexical anchors; a quoted multi-word
string is a phrase constraint. Consequently, `Project Alpha 42` and
`"Project Alpha 42"` can produce different candidate sets in files-only
operation. The query rewriter may normalize or add search anchors, but explicit
user-entered quoted phrases and their `+` / `-` occurrence markers are
restored deterministically before the Elasticsearch request is compiled.

This wording sensitivity is most visible in Elasticsearch-only deployments.
Richer entity resolution and alias coverage should reduce it by supplying better
canonical lexical anchors. Where ERG enables Qdrant, semantic vector retrieval
provides an independent recall signal and can compensate for lexical variation
or OCR wording differences. It does not replace explicit phrase semantics, live
Nextcloud ACL enforcement, or downstream evidence verification.

