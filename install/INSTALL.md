# Installation – 0.8.6-rc1.2 (draft)

All deployment variants use the single public entry point `install/install.sh`. Select `--profile standard` (default) or `--profile super-light`. The super-light profile is containerized and therefore does not require Python >=3.10 on the host; it is intended for older/smaller systems such as Leap 15.3.

Functional profile and deployment mechanism are conceptually separate. In the 0.8.6-rc1.2 candidate the supported mappings remain `standard -> native` and `super-light -> dockerized`; the latter is not a fork of the middleware.

Fresh 0.8.6 installations default to **`/opt/sunaq`**. A recognized existing
installation is deliberately kept at its current prefix (for example
`/opt/sunaq`); the installer does not move site-owned runtime state.

0.8.6-rc1.2 retains the rc1/rc1.1 research/client baseline and adds enforced
SRC/ERG architecture tiers, structured untrusted-evidence handling, Web/Playwright
SSRF hardening and file-backed service-secret delivery. Super-Light is the
packaged SRC baseline; Standard remains the ERG-compatible reference profile.
The security and user-configuration model remains:

- multi-user + live Nextcloud ACL is the safe installation default;
- Nextcloud TLS verification is on by default;
- an unsafe ACL/TLS configuration fails at API startup instead of silently
  degrading;
- a verified Nextcloud account becomes the canonical user identity;
- mail accounts, Web-research destinations and CardDAV seed settings are admin-managed per verified Nextcloud
  user in `runtime/users.sqlite`, not in `config.yaml`;
- the mail data model is already 1:n, while the Admin UI currently
  exposes one mail account per user;
- Nextcloud remains the authorization authority for every document read and for
  per-user WebDAV writes.

This is a release candidate for beta acceptance, not a production-hardened
release. App passwords are encrypted inside the SQLite credential store and protected by a separate root-managed master key; TLS/firewall/backup policy remains an administrator responsibility.

## 1. Recommended fresh-VM test

First inspect the plan:

```bash
sudo ./install/install.sh --plan --with-openwebui --with-qdrant --with-neo4j
```

For the tested topology with external Nextcloud and an administrator-managed LLM/embedding backend:

```bash
sudo ./install/install.sh --with-openwebui --with-qdrant --with-neo4j
```

`--multi-user` is no longer required; it is the default. `--full` now installs
all components still bundled by the middleware:

```bash
sudo ./install/install.sh --full
```

Ollama is deliberately not installed or managed by this release. The administrator
provides an Ollama or other compatible LLM/embedding backend separately and
configures its URL/model names in `config.yaml` / `provider.env`.

For a CPU-only local embedding/reranker helper stack, this release includes
`install/docker-compose.ollama.example.yml`. A convenient separate location is:

```bash
sudo mkdir -p /opt/rag-helper
sudo cp install/docker-compose.ollama.example.yml /opt/rag-helper/docker-compose.yml
cd /opt/rag-helper
sudo docker compose up -d
sudo docker exec ollama ollama pull qwen3-embedding:4b
```

The current reference keeps Qdrant at 1024 dimensions (`embedding.dimensions: 1024`). Prefixes are explicit configuration (`embedding.query_prefix` / `embedding.document_prefix`); the runtime does not infer them from the model name. For a one-time GPU-assisted initial sync, leave the persistent CPU URL in `config.yaml` and pass `--embedding-url http://GPU-HOST:11434` to `rag.sync`.

The example keeps Ollama and TEI bound to `127.0.0.1`, pins Ollama to `0.24.0`,
and uses `Alibaba-NLP/gte-multilingual-reranker-base` through the TEI CPU image.

Reranking is **disabled by default** in both reference profiles:

```yaml
reranker:
  backend: none
```

Enable `backend: tei` or `backend: local` only for comparative testing. A
Standard install does not pre-download the local Hugging Face reranker model
unless `--with-reranker-download` is supplied. TEI is an external/local-helper
service and does not require that model cache in the middleware venv.

The fresh-install default prefix is `/opt/sunaq` and the service user is `rag`. Recognized legacy prefixes are preserved on rerun.

## 2. Installation modes

### Architecture presets

Super-Light starts from the formal SRC configuration. For fresh Super-Light
acceptance and reproducible operational records, the recommended command still
spells this out as `--profile super-light --deployment dockerized --preset core`.
The first two switches select the tested deployment mapping; `--preset core`
selects and validates the formal SRC capability contract. This remains useful
even though those choices match the packaged Super-Light defaults, because
`install/last-install-command.sh` then records the intended architecture
explicitly.

Both supported installers also accept:

```text
--preset core
--preset workgroup
--preset-file /path/to/site-preset.yaml
```

Preset files are parsed as YAML data through `rag.config_preset`; they are not
sourced as shell. The merge is validated against SRC/ERG invariants. Explicit
installer connection, TLS and Elasticsearch-index arguments remain authoritative
over preset values. Existing installations receive no preset change unless the
operator explicitly selects one.

When an existing **ERG** RC installation is deliberately converted to
`--preset core`, historical Neo4j contents are **not migrated**. Stop the
installation, reset the graph with `python -m rag.graph --config
/opt/sunaq/config.yaml reset --yes-really-delete-all`, then re-import only the
wanted CardDAV/administrator seeds before normal SRC use. This destructive reset
is intentional during the RC line and avoids carrying document-derived ERG
entities into SRC query expansion.


```text
(default)      multi-user credential_store + live ACL
--single-user  explicit one-user mode; live ACL remains enabled
--acl-off      explicit diagnostic mode; never use for a shared document set
```

Common connection/frontend/proxy switches now use the same names in Standard and Super-Light. On a recognized Standard rerun, the installer reports the existing SunaQ installation and verifies that native SunaQ processes, systemd units and local Docker Compose services are stopped before modifying files. If the running state cannot be determined reliably, the rerun aborts without changes. On rerun, an existing OpenWebUI/proxy selection is retained unless explicitly overridden with `--no-openwebui` / `--no-proxy`. Standard also accepts `--nextcloud-url`, `--elasticsearch-url`, `--elasticsearch-index`, `--proxy-http-port` and `--proxy-https-port`, so deployment scripts do not need profile-specific spellings.

Other useful flags:

```text
--with-qdrant       local Qdrant
--with-neo4j        local Neo4j
--core              legacy resource shorthand: Qdrant + Neo4j (not SRC)
--preset core|workgroup
                    apply a shipped capability preset
--preset-file FILE  apply a safe site-owned YAML capability overlay
--with-openwebui    bundled OpenWebUI
--full              Qdrant + Neo4j + OpenWebUI
--with-systemd      install/enable optional middleware units, do not start yet
--no-proxy          no bundled nginx
--plan              show plan only
-y                  non-interactive after plan review
```

Fresh installs and installer reruns deliberately return to **maintenance mode**. The OpenAI-compatible provider is started in a minimal maintenance implementation that still validates registered provider-client Bearer keys, but does not load the normal SunaQ/LLM pipeline.

For **Super-Light**, the initial maintenance start intentionally brings up only
the provider plus optional OpenWebUI/nginx. Leaving maintenance starts Neo4j and
the normal API; Playwright starts only when explicitly installed with
`--with-playwright`, and the mail worker is not started unless the administrator
has enabled and started that optional ingestion path. A smoke test reports only
selected local components.

Current rc1.2 operational note: the Neo4j image can still be first pulled at
`maintenance-mode.sh off` rather than during the installer preparation phase.
That is functional but not the intended long-term UX; a future installer cleanup
should pull every selected runtime image before the maintenance hand-off so
leaving maintenance is a fast, deterministic state transition.

Selected infrastructure containers may already be prepared or running in the
Standard profile; they do not receive user SunaQ traffic while the provider gate
remains in maintenance mode.

All Docker images shipped by the installer are immutable digest-qualified pins.
Human-readable tags remain in the image reference for clarity, but Docker resolves
the exact digest recorded in `versions.lock.yaml`. Image upgrades are therefore
explicit release changes, not side effects of a mutable tag.

## 3. Mandatory site configuration before first normal start

Edit `/opt/sunaq/config.yaml`. During this phase the provider returns only `SunaQ ist im Maintenance-Modus. Bitte versuchen Sie es später erneut.` after successful provider-key authentication.

The operator switch uses the installation prefix reported by the installer plan. For a fresh 0.8.6 installation:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh status
sudo /opt/sunaq/install/maintenance-mode.sh on
sudo /opt/sunaq/install/maintenance-mode.sh off
```

A recognized legacy installation keeps its retained prefix; for example:

```bash
sudo /opt/nextcloud-rag/install/maintenance-mode.sh status
sudo /opt/nextcloud-rag/install/maintenance-mode.sh on
sudo /opt/nextcloud-rag/install/maintenance-mode.sh off
```

`on` stops the normal API/background workers and restarts the minimal provider. `off` restarts the normal provider and normal services. This is also the intended envelope for master-key rotation, restore checks and other operations that must not race normal credential/SunaQ traffic.

Edit `/opt/sunaq/config.yaml`.

At minimum check:

```yaml
nextcloud:
  base_url: "https://cloud.example.org/nextcloud"
  verify_tls: true
  ca_file: ""

elasticsearch:
  url: "http://127.0.0.1:9200"
  index: "<your-nextcloud-fulltextsearch-index>"
  username: ""
  password_env: "ELASTICSEARCH_PASSWORD"
  verify_tls: true
  ca_file: ""
```

If Elasticsearch requires authentication, set the administrator-selected user
name in `config.yaml` and place only the password in chmod-0600 `runtime.env`:

```bash
ELASTICSEARCH_PASSWORD='...'
```

The Elasticsearch account may be the same account used by Nextcloud
FullTextSearch, or a dedicated read-only account provisioned by the
administrator. The middleware does not create Elasticsearch users or roles. For
remote HTTPS Elasticsearch, `ca_file` may point to the private CA bundle.

Use the **canonical HTTPS URL** of Nextcloud. The Login Flow deliberately does
not follow HTTP-to-HTTPS redirects. The beta defaults are:

```yaml
security:
  allow_insecure_nextcloud: false

acl:
  enabled: true
  identity_mode: credential_store
  credential_store: runtime/users.sqlite

auth:
  credential_store: runtime/users.sqlite
  nextcloud_login_flow_enabled: true
```

For a private Nextcloud CA, configure a trusted CA rather than disabling TLS
verification. Both supported installer profiles accept repeatable
`--ca-certificate FILE` options and build a Nextcloud-specific CA bundle. In a
native Standard install this becomes
`/opt/sunaq/runtime/ca/nextcloud-ca-bundle.pem` and is written to
`nextcloud.ca_file`. This deliberately avoids global
`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` overrides, which can otherwise replace
the public trust used for OpenAI, Hugging Face and unrelated HTTPS endpoints.

`security.allow_insecure_nextcloud=true` exists only as an explicit lab escape
hatch.

Edit `/opt/sunaq/provider.env` for the default backend connection and review `models/` for the user-visible SunaQ profiles. The shipped profiles route planner/verifier/evidence/answer roles through the configured backend unless a profile supplies an explicit role override. Keep API keys in root-protected runtime provisioning state; rc1.2 materializes file-backed runtime secrets for supported services. Embeddings remain independently configured in `config.yaml`.

## 4. Start and smoke test

```bash
sudo -u rag /opt/sunaq/start-all.sh
/opt/sunaq/status.sh
/opt/sunaq/install/smoke-test.sh /opt/sunaq
```

If systemd units were installed, start them only after site configuration is
complete.

The default nginx topology is:

```text
client -> nginx :443 (TLS)
HTTP :80 -> 308 HTTPS redirect
             |-- /rag-admin/ -> SunaQ Admin UI 127.0.0.1:8765
             |-- /rag-api/ -> SunaQ API 127.0.0.1:8765
             |-- /auth/    -> Nextcloud Login Flow endpoints
             |-- /v1/      -> OpenAI-compatible provider 127.0.0.1:8766
             `-- /          -> selected user UI; without UI -> redirect to /rag-admin/
```

Optional Qdrant and Neo4j bind to loopback. The FastAPI middleware on `127.0.0.1:8765` is additionally protected by an installer-managed `RAG_INTERNAL_API_KEY`. The provider sends this machine credential on internal calls; nginx injects it only on protected API/auth proxy locations. The bundled OpenWebUI uses host networking but explicitly binds its own server to `127.0.0.1:3000`; it reaches the provider directly at `http://127.0.0.1:8766/v1`. This internal loopback hop is intentionally independent of nginx TLS/certificate replacement.

The installer generates a self-signed nginx bootstrap certificate with SANs for
the current hostname, localhost and detected IPv4 addresses. Browsers will warn
until a trusted certificate is installed. This is deliberate: external traffic
is encrypted by default, while certificate trust remains an administrator/site
PKI decision. The installer does **not** modify the host firewall; allow inbound
TCP 443 and, if the redirect should be reachable, TCP 80.


### Bundled OpenWebUI and HTTPS

Existing Apache/reverse proxy deployments should preferably keep the bundled nginx on alternate loopback/internal ports and proxy the public paths through it. This preserves the tested TLS/rate-limit/authentication rules and RC4.3 security-zone handling. A deployment using `--no-proxy` may proxy directly to 8765 only as an advanced configuration: the external proxy must implement equivalent client authentication and may inject **only** the general `X-AKI-Internal-Key` value from `RAG_INTERNAL_API_KEY` for INTERNAL/ADMIN access. Do not give a generic reverse proxy `RAG_PROVIDER_INTERNAL_KEY`; TRUSTED_PROVIDER/USER calls should originate from the SunaQ provider. Never expose 8765/8766 directly to an untrusted network.

#### Same host as Nextcloud/Apache

If Nextcloud's Apache already owns host ports 80/443, keep Apache as the public entry point and move only the bundled SunaQ nginx to unused internal host ports. Super-Light supports this directly:

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://127.0.0.1:9200 \
  --elasticsearch-index my_index \
  --with-proxy \
  --proxy-http-port 81 \
  --proxy-https-port 444
```

The selected ports are part of the recorded `install/last-install-command.sh`, so an installer rerun reproduces the same topology. The SunaQ nginx still uses its normal TLS/Auth/rate-limit configuration; only the listen ports change. Keep 81/444 blocked from untrusted networks when they are used only as an Apache backend.

The general machine credential injected by SunaQ nginx authenticates nginx to the INTERNAL service plane; it is **not** an end-user/admin login and it does not grant TRUSTED_PROVIDER/USER privileges. The provider-role secret remains confined to the API/provider runtime. Do not use `--no-proxy-basic-auth` unless an upstream proxy already provides equivalent authentication before requests reach SunaQ nginx.

In the Nextcloud HTTPS virtual host, proxy only the SunaQ path prefixes to nginx on loopback. Do **not** proxy `/`, because that would replace the Nextcloud application root. A minimal Apache example is:

```apache
ProxyPreserveHost On
SSLProxyEngine On

# The installer bootstrap certificate is self-signed. Either trust/replace it,
# or limit disabled backend verification to this loopback proxy target.
<Proxy "https://127.0.0.1:444/*">
    SSLProxyVerify none
    SSLProxyCheckPeerName off
</Proxy>

RedirectMatch 302 ^/rag-admin$ /rag-admin/
RedirectMatch 302 ^/rag-api$ /rag-api/
RedirectMatch 302 ^/curation$ /curation/

ProxyPass        /v1/             https://127.0.0.1:444/v1/
ProxyPassReverse /v1/             https://127.0.0.1:444/v1/

ProxyPass        /auth/nextcloud/ https://127.0.0.1:444/auth/nextcloud/
ProxyPassReverse /auth/nextcloud/ https://127.0.0.1:444/auth/nextcloud/

ProxyPass        /rag-admin/      https://127.0.0.1:444/rag-admin/
ProxyPassReverse /rag-admin/      https://127.0.0.1:444/rag-admin/

ProxyPass        /rag-api/        https://127.0.0.1:444/rag-api/
ProxyPassReverse /rag-api/        https://127.0.0.1:444/rag-api/

ProxyPass        /curation/       https://127.0.0.1:444/curation/
ProxyPassReverse /curation/       https://127.0.0.1:444/curation/
```

This requires Apache's proxy/proxy_http and SSL proxy support. Prefer trusting or replacing the nginx backend certificate where practical; `SSLProxyVerify none` above is appropriate only for the explicitly scoped loopback backend and must not be generalized to unrelated HTTPS proxies.

Configure SunaQ with the **public Apache URL** (for example `https://cloud.example.org`), not `https://127.0.0.1:444`. Apache then forwards only the SunaQ path prefixes internally. If bundled OpenWebUI is enabled, its `/` route cannot share the same Nextcloud virtual-host root; expose OpenWebUI through a separate hostname/vhost or keep it local.


External clients always use nginx HTTPS (`https://HOST/v1`). The bundled
OpenWebUI is different: because it runs on the same Linux host, the bundled configuration uses host
networking with `HOST=127.0.0.1`, `PORT=3000` and connects directly to
`http://127.0.0.1:8766/v1`. This avoids an HTTP->HTTPS redirect inside the
container and avoids globally disabling OpenWebUI TLS verification. Replacing
the nginx bootstrap certificate therefore does not require changing the bundled
OpenWebUI provider connection.

`https://HOST/rag-api` and `https://HOST/rag-api/` redirect to the protected
`/rag-api/health` endpoint for a useful browser/diagnostic landing point.

OpenWebUI follow-up-question helper calls are suppressed server-side in this
beta and return an empty `follow_ups` list without invoking an LLM. This closes
a secondary disclosure path discovered during the two-user ACL test.

## 5. Trusted frontend and canonical user model

A provider Bearer identifies a **trusted frontend client**. The frontend's user
ID is scoped as:

```text
client_id::external_user_id
```

After successful Nextcloud Login Flow v2 the scoped frontend identity is bound
to the canonical user:

```text
(nextcloud_server, nextcloud_login)
```

Consequently the same verified Nextcloud user may arrive from several trusted
frontends and still receives the same admin-managed mail/Web configuration;
each frontend binding nevertheless retains its own Nextcloud app password.

Create another trusted frontend with its own key:

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.provider_clients \
  create openwebui-office --name "OpenWebUI Office"
```

Never copy one frontend's Bearer to an unrelated frontend.

For an existing/external OpenWebUI, no Compose modification is required when
the connection editor offers **Additional headers (JSON)**. Configure the
connection to `/v1` with its own trusted-client key and add:

```json
{
  "X-OpenWebUI-User-Id": "{{USER_ID}}"
}
```

This path has been validated on a blank VM: the external
OpenWebUI supplied its stable user ID, triggered Nextcloud Login Flow v2,
completed authentication, returned an answer and created the canonical user in
the SunaQ Admin UI.

## 6. First-user onboarding

In the bundled OpenWebUI, a signed-in user sends a stable opaque user ID to the
provider. If no Nextcloud credential is bound yet, the provider starts or reuses
a Login Flow v2. The user follows the Nextcloud URL, authorizes the app password,
and the next request completes the binding.

Relevant endpoints:

```text
POST /auth/nextcloud/start
POST /auth/nextcloud/ensure
GET  /auth/nextcloud/status/<flow_id>
```

After successful verification, the account appears in:

```text
/rag-admin/users
```

The Admin UI never displays stored app passwords.

## 7. Per-user Web research

Search/fetch policy remains global in `web.yaml`. A user-specific Web setting is
created by the administrator under `/rag-admin/users/<canonical-id>` and contains:

```text
enabled
archive_enabled
Nextcloud target_path
```

No per-user target directory lives in `web.yaml` in the multi-user beta.
The installer no longer bundles a search engine. To enable public Web research,
the administrator configures a supported external provider in `web.yaml`: Brave
or a separately operated SearXNG endpoint. Enabling the global arm does not
automatically enable it for every canonical user.

For Brave, keep the secret out of `web.yaml`:

```bash
WEB_SEARCH_API_KEY='...'
```

The web relevance model may use a dedicated `WEB_LLM_API_KEY`; when omitted, the current provider configuration can fall back to the normal `LLM_API_KEY`. Search snippets are discovery metadata only: the middleware fetches the target pages and admits only fetched, relevance-checked content as Evidence.

When archiving is enabled, the middleware writes through WebDAV with that
canonical user's current Nextcloud credential. Therefore an administrator may
choose the desired destination, but **Nextcloud ACL ultimately decides whether
the user can write there**.

Use a dedicated archive folder. Every configured Web archive root is excluded
from ordinary internal retrieval (and retained in an exclusion history when the
target changes), so a mixed business-document/Web-archive folder would hide the
other documents below that root as well.

Rendered-PDF enrichment uses the local Playwright renderer configured under
`archive.renderer` in `web.yaml`. In the Standard profile the renderer is an
optional Compose service. Use `--with-playwright` to set
`archive.renderer.enabled: true`, prepare/build the renderer, start it on
`127.0.0.1:8090` and record `LOCAL_PLAYWRIGHT=1`. Use `--no-playwright` to
set the renderer disabled and remove the local container. If neither switch is
passed on a rerun, the existing `web.yaml` setting remains authoritative.
Super-Light now uses the same explicit lifecycle: fresh installs do not build or
start the renderer. Pass `--with-playwright` to install/retain it or
`--no-playwright` to stop/remove the local renderer container. Reruns without
either switch preserve the recorded installation state.

## 7a. Periodic Elasticsearch -> Qdrant sync

The installable snapshot includes `start-sync-worker.sh`. It periodically invokes the existing state-aware `rag.sync` implementation; it is not a second indexer. Default policy:

```yaml
sync_worker:
  enabled: true
  poll_interval_seconds: 300
  max_documents: 0
  enqueue_graph: false
```

`start-all.sh` starts the worker when both `sync_worker.enabled` and `qdrant.enabled` are true. With systemd installation the corresponding unit is `rag-sync-worker.service`. Keep the legacy mail-specific Qdrant post-hook disabled in normal operation to avoid redundant runs.

## 8. Per-user Mail sync

`config.yaml` contains only the global mail worker policy:

```yaml
mail:
  enabled: false
  state_file: mail_state.sqlite
  poll_interval_seconds: 300
  worker:
    enabled: false
```

There are no IMAP usernames/passwords or per-user target paths in
`config.yaml`. Configure the first account for a verified user through the Admin
UI. The current Admin UI exposes one account per user, but the underlying
`mail_accounts` schema is already 1:n so a later multi-mailbox/account UI does
not require a user-model migration.

After saving the IMAP credential, **Verbindung testen & Mailboxen ermitteln** can list the server-visible mailbox names, flags, hierarchy delimiter and selectability before choosing recursive roots.

A mail account contains, among other things:

```text
IMAP host/port/security
IMAP username + secret
mailbox root list (subfolders are synchronized recursively)
import limit / not-before date
Nextcloud target path
optional EML target path
```

The IMAP secret is held via the shared `CredentialStore` abstraction. In `0.8.3-rc6`
it is AES-256-GCM encrypted inside `runtime/users.sqlite`, using a master key
outside the database; it is never placed in `config.yaml`.

To activate polling globally after at least one account has been configured,
enable both the mail capability and its periodic worker:

```yaml
mail:
  enabled: true
  worker:
    enabled: true
```

Then restart/start the worker. Native/systemd and Docker both run the same `rag.mail_worker` Python scheduler. It re-reads `mail.enabled`, `mail.worker.enabled` and the poll interval between runs; a disabled worker remains idle instead of exiting. Each configured mailbox is treated as a recursive
root; selectable IMAP descendants are discovered with `LIST`. New messages are
stored one-directory-per-mail with `mail.txt`, `.mailmeta.json` and attachments. New accounts default to **no raw `message.eml`**; enable `store_eml` explicitly when raw-message retention is required. The sidecar keeps raw-message SHA-256/size, UID/UIDVALIDITY, import timestamp and selected technical headers. Existing legacy flat archives remain readable but are not
automatically moved.

For each account the worker obtains the canonical user's current Nextcloud app
password and writes with that identity. A user whose canonical SunaQ account is
disabled or whose Nextcloud credential is absent is skipped.

Manual diagnostics:

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.mail_sync --config config.yaml --dry-run
sudo -u rag ./.venv/bin/python -m rag.mail_sync --config config.yaml --user <nextcloud-login>
```

## 9. Per-user CardDAV contact seeds

Contact seed administration is keyed by the verified Nextcloud account. The
internal `canonical_user_id` remains an implementation key and is not required
for normal operation. After a user has completed Login Flow, SunaQ Admin → Users
→ Kontakt-DB can synchronize the selected CardDAV address books with the stored
Nextcloud credential. No separate CardDAV password needs to be configured.

Dockerized super-light CLI:

```bash
cd /opt/sunaq/install/super-light
./contacts.sh list
./contacts.sh status --user <nextcloud-login>
./contacts.sh books --user <nextcloud-login>
./contacts.sh sync --user <nextcloud-login>
```

Native equivalent:

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.contacts sync --user <nextcloud-login>
```

Use `--server URL` only when the same login exists on multiple Nextcloud
instances. A disabled user/source, missing Nextcloud credential or empty address
book is a clean no-op. The Admin-UI synchronization is synchronous in 0.8.4;
there is no progress bar yet, but progress is logged and final counters/status are
persisted. The legacy global `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_PASSWORD`
CardDAV path is compatibility-only.

## 10. Live ACL and no-backfill rule

Internal retrieval may use Elasticsearch, Qdrant and Neo4j to produce
candidates. After fusion/reranking, the final candidates are checked live
against Nextcloud with the current user's app password. Invisible documents are
removed and **not replaced by lower-ranked documents**. Results that cannot be
tied to a Nextcloud file ID are denied when ACL is enabled.

Disabling a canonical user in the Admin UI blocks its live-ACL use and stops
mail/Web activity for that user; configuration is retained for controlled
re-enable/re-auth rather than deleted automatically.

## 11. Graph and normal sync

When local Neo4j is selected, both installation profiles wait for the database and run the idempotent SunaQ schema initialization. The same step runs on an installer rerun, so an existing database receives missing non-destructive constraints, indexes and deterministic backfills; a new database is not required. The API also attempts this migration at startup for deployments managed outside the installer. See [the canonical Neo4j schema reference](../docs/NEO4J-SCHEMA.md).

With Neo4j selected, graph retrieval and the asynchronous graph worker are
available. Expensive whole-corpus graph extraction remains off during normal
sync:

```yaml
sync:
  graph_queue:
    enabled: false
```

Explicit bulk graphing:

```bash
cd /opt/sunaq
sudo -u rag ./.venv/bin/python -m rag.sync --enqueue-graph
```

Answer-cited documents may still be queued on demand. Graph failure is graceful
degradation and never bypasses document ACL.

## 12. Security notes

- Multi-user + live ACL is default; unsafe modes require an explicit flag.
- Nextcloud TLS certificate checking is centralized under `nextcloud.verify_tls` / `nextcloud.ca_file` and applies to Login Flow, live ACL, CardDAV, mail WebDAV and web archive.
- Security-critical config is validated on API startup.
- Trusted-client API keys are stored hashed in `users.sqlite`; the bundled
  frontend's plaintext key remains in chmod-0600 `runtime.env`.
- Nextcloud and IMAP app passwords are AES-256-GCM encrypted in
  `runtime/users.sqlite`; the separate master key must be backed up securely.
- nginx Basic Auth protects admin/auth/internal API locations by default; the
  OpenWebUI and `/v1/` paths use their own session/Bearer authentication.
- nginx serves HTTPS on 443 by default with a generated self-signed bootstrap certificate;
  HTTP 80 redirects to HTTPS. Replace `install/nginx/tls/server.crt` and
  `server.key` with site certificates when available. The installer preserves them on reruns.
  If nginx is already running when the files are replaced, reload/restart nginx before
  testing the Nextcloud app; the current installer does not automatically reload nginx
  after an out-of-band certificate replacement.
- Firewall policy, secret backups and host hardening remain administrator responsibilities.


## Docker 29 / Leap 16 VM storage troubleshooting

On some affected Leap 16 guests running on older virtualized/Btrfs storage stacks,
Docker/containerd image extraction may fail with errors such as `invalid tar header`
or `crc32 mismatch` even though a fresh guest Btrfs scrub is clean. In the tested
environment, configuring the classic Docker storage driver resolved the issue:

```json
{
  "storage-driver": "overlay2"
}
```

Place this in `/etc/docker/daemon.json` **before the first image pull**, restart
Docker, and verify with `docker info | grep 'Storage Driver'`. Treat this as a
platform-specific workaround, not a universal default.

## 12. Blank-VM acceptance order

For the intended external Nextcloud/Ollama test:

```bash
sudo ./install/install.sh --with-openwebui --with-qdrant --with-neo4j
```

Then:

1. set canonical Nextcloud HTTPS URL and Elasticsearch index;
2. set LLM/embedding endpoint as required;
3. while maintenance mode is still enabled, run configuration/preflight checks;
4. leave maintenance mode with `sudo /opt/sunaq/install/maintenance-mode.sh off`;
5. verify `/rag-api/health` and `/rag-admin/`;
6. install/open SunaQ Recherche (or another trusted frontend), complete Nextcloud Login Flow for two users and verify ACL separation;
7. synchronize one user's Kontakt-DB seed from the Admin UI and verify Neo4j source/provenance;
8. add a second trusted frontend and verify its identity binding is isolated;
9. configure one user's Web target, archive a public page and verify landscape/desktop PDF plus hidden metadata;
10. configure one IMAP account if mail is in scope, run a dry-run and then an actual incremental sync;
11. stop Elasticsearch temporarily and verify the user receives a service-unavailable message rather than a generic 500/502;
12. verify normal document sync leaves bulk graph enqueue disabled and backend degradation never turns into an ACL fail-open.

Blank-VM acceptance for `0.8.5-rc4.3` has been completed for both supported mappings. Super-Light/dockerized installed successfully and passed document search plus SunaQ Admin checks. Standard/native also completed installation and passed document search plus SunaQ Admin checks, including automatic startup of the optional Playwright renderer when selected with `--with-playwright`. Repeat the same checklist on the intended deployment host before exposing it to users.


## 13. Administration reference

See `docs/ADMINISTRATION.md` for trusted-client lifecycle, canonical users,
mail/Web/Kontakt-DB configuration and the `runtime/users.sqlite` schema. For the
short beta runbook use `docs/BETA-OPERATIONS.md`; deferred polish and known limits
are tracked in `docs/KNOWN-LIMITATIONS.md`. Direct SQL edits
are diagnostic/emergency operations only; normal administration should use the
CLI or Admin UI.

## Retrieval-planner / sync baseline

The reference config uses an Elasticsearch scroll page of `50` and embedding work batch of `8` to limit peak memory on heterogeneous corpora. Qdrant lifecycle cleanup uses deterministic explicit point IDs rather than payload-filter deletion.


## Retrieval-planner acceptance checks

After the normal health/smoke checks, verify the provider health endpoint and
confirm the `retrieval_planner` block reports the intended YAML values.

Recommended functional probes:

1. Normal short query: verify ES and vector retrieval remain available.
2. `/files <query>`: verify only the Files arm is used.
3. `/vector <query>`: verify the provider does not send Boolean `+term` syntax as
   vector embedding text.
4. Exhaustive query such as `Suche mir Rechnungen von A an B aus 2025`: inspect
   provider logs for the initial planner probes. If a strict probe is emitted,
   verify the strict ACL gate runs only in round 1.
5. A deliberately broad exhaustive query should return a polite request to
   narrow the criteria rather than silently truncating a larger visible set.

The neutral release build can validate Python logic and mocked Graph shaping but
cannot substitute for a runtime test against the deployment's actual Neo4j,
Qdrant, embedding backend and Nextcloud Live-ACL.


### Encrypted credential store

Fresh installs create `runtime/credential-master.key` as `root:rag 0640`, add
`RAG_CREDENTIAL_MASTER_KEY_FILE` and `RAG_CREDENTIAL_ENCRYPTION=required` to
`runtime.env`, and verify the encrypted credential store. `0.8.6-rc1.2` is the current development release-candidate baseline; no upgrade path from unpublished internal snapshots is documented or supported.

The master key must be backed up separately. The Admin UI can report encryption
status and replace IMAP credentials, but does not reveal stored secrets or create
the root-managed master key.

## Profile vs deployment mode

0.8.4 exposes profile and deployment as separate concepts. The tested combinations
in this release are:

```text
profile=standard    deployment=native
profile=super-light deployment=dockerized
```

The public installer accepts both axes explicitly, for example:

```bash
sudo ./install/install.sh --profile super-light --deployment dockerized --plan ...
```

Other combinations are rejected rather than silently approximated. This keeps
one middleware codebase while leaving room for a later `standard + dockerized`
deployment without creating a fork.

### Reproducible installer reruns

The installer also refuses a non-empty `--prefix` that is not recognized as a SunaQ installation. This is a safety boundary because profile refreshes replace selected top-level paths such as `rag/`, `docs/` and `clients/`. A typo such as pointing `--prefix` at an unrelated application directory must therefore fail before any files are changed. Fresh installs should use a dedicated empty path; current installations carry `.aki-rag-installation` plus installer state for future reruns.


Keep the exact installation command used for a host. The Super-Light installer now
records the wrapper invocation in:

```text
/opt/sunaq/install/last-install-command.sh
```

The file is mode `0600` because the command may contain internal URLs and certificate
paths. It contains no generated passwords or API keys. Review it before executing it
again, especially after moving certificates or changing external service addresses.

On a rerun the installer performs an early preflight before modifying the installation:
it checks the installer source tree, the existing prefix, supplied CA file paths/PEM
shape and an available Docker daemon when Docker is already installed. For an existing
Super-Light installation, any running SunaQ service is a hard preflight error: stop
the stack before rerunning the installer so no refresh is attempted against live
containers. A recognized but fully stopped stack is informational and remains the
normal repair/rerun path. An unreachable Docker daemon or missing required input file
is also a hard error.

Use `--plan` first when changing profile options, URLs, CA paths or optional services.
Site-owned `config.yaml`, `provider.env`, `runtime.env`, runtime databases, credential
master key and generated TLS material remain preserved on normal reruns.
Preservation is intentional and is not a schema merge: newly introduced optional `config.yaml` keys are not inserted into an existing site configuration automatically. Review the release changelog/reference profile after an update and add wanted settings manually (for example RC5 `acl.prefilter.enabled`).

### Dockerized super-light with private CA

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --with-proxy \
  --ca-certificate /etc/pki/trust/anchors/Company_Root_CA.crt \
  --nextcloud-url https://cloud.internal.example/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index
```

Repeat `--ca-certificate` for separate root/intermediate PEM certificates. The
files are added to the container system CA bundle in addition to public CAs. The
installer validates each supplied path and PEM before copying it; correct a typo
and rerun the idempotent installer.

Normal certificate verification is enabled by default, including CA/trust-chain,
SAN/hostname, signature and validity checks. The additional Python/OpenSSL
`VERIFY_X509_STRICT` RFC-5280 checks are **disabled by default** because older
private PKIs may omit extensions such as Authority Key Identifier while still
being valid for the site's configured trust policy. To enable the stricter mode
explicitly, use:

```bash
--x509-strict
```

This maps to:

```yaml
tls:
  x509_strict: true
```

The compatibility alias `--no-x509-strict` is still accepted. Do not substitute
`verify_tls: false` unless deliberately diagnosing a TLS issue; that disables the
actual certificate verification rather than only the additional strict flag.

These settings cover the direction **SunaQ middleware -> Nextcloud**. If the
Nextcloud-hosted SunaQ Recherche app connects to an SunaQ HTTPS endpoint signed by an
internal CA, Nextcloud itself must also trust that CA in its own certificate
store. A host-level `curl` succeeding does not prove that Nextcloud's outbound
HTTP client trusts the same certificate chain.

### Native standard deployment and private CAs

The native profile accepts the same repeatable `--ca-certificate FILE` option.
The installer validates each PEM, copies the supplied root/intermediate
certificates into `runtime/ca/`, builds
`runtime/ca/nextcloud-ca-bundle.pem` and writes that path to
`nextcloud.ca_file`. This trust bundle is used only for middleware connections
to Nextcloud (Login Flow, live ACL, CardDAV, mail WebDAV and web archive); it does
not replace the public CA bundle used for OpenAI, Hugging Face or unrelated web
requests.

Example:

```bash
sudo ./install/install.sh \
  --profile standard \
  --deployment native \
  --nextcloud-url https://cloud.internal.example/nextcloud \
  --ca-certificate /secure/Company_Root_CA.crt \
  --with-systemd \
  -y
```

Manual configuration remains possible:

```yaml
nextcloud:
  base_url: https://cloud.internal.example/nextcloud
  verify_tls: true
  ca_file: /opt/sunaq/runtime/ca/nextcloud-ca-bundle.pem
```

Legacy `auth.verify_tls`, `acl.verify_tls`, `acl.ca_file` and
`carddav.verify_tls` values are still read as upgrade fallbacks when the
canonical `nextcloud` TLS keys are absent.

The `tls.x509_strict` setting applies to native middleware processes and
standalone Nextcloud workers as well.

## Retrieval policy

Capability installation and retrieval strategy are separate. Example standard
policy:

```yaml
retrieval_policy:
  internal:
    files: required
    vector: optional
    graph: optional
  optional_default: include
  web: planner
```

`required` always runs during normal retrieval, `optional` may be omitted by the
query planner for an unambiguous task, and `disabled` is not selected by the
planner. If the planner fails, `optional_default` decides deterministically
whether optional arms are included. Round counts and candidate limits remain
hard administrator budgets under `retrieval_planner` / `search`.

Web modes are `disabled`, `explicit` (`/web` or explicit natural instruction
only), and `planner` (client-granted automatic fallback may be used).
