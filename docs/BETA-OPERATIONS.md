# Beta operations runbook

**Reference:** `0.8.6-rc1`  
**Target:** controlled beta deployment behind an administrator-managed network boundary

This document is the short operational path for the current beta candidate. For
full configuration details see `TECHNICAL-REFERENCE.md`; for identity/credential
administration see `ADMINISTRATION.md`.

## 1. Supported deployment matrix

0.8.6 keeps functional profile and deployment mechanism separate, but only two
combinations are regression-tested and accepted for the beta:

| Functional profile | Deployment | Status |
| --- | --- | --- |
| `standard` | `native` | supported/tested |
| `super-light` | `dockerized` | supported/tested |
| `standard` | `dockerized` | not a 0.8.6-rc1 supported mapping |
| `super-light` | `native` | not a 0.8.6-rc1 supported mapping |

Super-Light is one configuration of the same middleware, not a fork. Nextcloud,
its FullTextSearch Elasticsearch and the LLM endpoint are administrator-managed
dependencies outside the SunaQ stack; they may be on separate systems or, where
ports/resources permit, on the same host. Locally the minimal SunaQ stack runs API, provider and Neo4j seed/alias support;
bundled nginx is optional. Qdrant, the local reranker, Web Research, Research
Findings, the mail worker and Playwright are disabled by default. Install the
renderer only when required with `--with-playwright`.

## 2. Super-Light installation

Inspect the plan first. For rc1.2 acceptance, keep the architecture choice
explicit in the recorded command even where Super-Light would select the same
packaged defaults: `--profile super-light --deployment dockerized --preset core`
means the Dockerized Super-Light deployment of the formal SRC contract.

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --nextcloud-url https://cloud.example/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --with-proxy \
  --plan
```

For an internal PKI, both supported profiles accept one
`--ca-certificate FILE` for each root/intermediate PEM certificate. The
installer builds a Nextcloud-specific CA bundle and configures
`nextcloud.ca_file`; the native profile does not replace the global Python CA
bundle, so public OpenAI/Hugging Face trust remains unchanged. If the chain is
trusted but Python 3.13 rejects an older certificate only because strict
RFC-5280 checks require an Authority Key Identifier, additionally use
`--no-x509-strict`:

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --nextcloud-url https://cloud.example/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --ca-certificate /secure/Company_Root_CA.crt \
  --no-x509-strict \
  --with-proxy \
  -y
```

`--no-x509-strict` does **not** disable normal CA-chain, hostname/SAN, signature
or validity checks. Do not replace it with `verify_tls:false` in normal operation.

The CA option covers SunaQ -> Nextcloud traffic. For the reverse direction
(Nextcloud SunaQ Recherche app -> SunaQ HTTPS endpoint), an internal SunaQ server CA
must also be imported into Nextcloud's own certificate store; a successful
host-shell `curl` is not sufficient evidence for Nextcloud's HTTP client.

### Same host as Nextcloud/Apache

When Super-Light runs on the same host as Nextcloud and Apache already owns 80/443, keep Apache public and move the bundled SunaQ nginx to internal ports such as 81/444:

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --nextcloud-url https://cloud.example/nextcloud \
  --elasticsearch-url http://127.0.0.1:9200 \
  --elasticsearch-index my_index \
  --with-proxy \
  --proxy-http-port 81 \
  --proxy-https-port 444 \
  -y
```

Apache should then proxy only `/v1/`, `/auth/nextcloud/`, `/rag-admin/`, `/rag-api/` and `/curation/` to `https://127.0.0.1:444`. Leave `/` with Nextcloud. Keep the internal SunaQ ports blocked from untrusted networks; the installer-generated nginx certificate is self-signed unless replaced. A complete Apache `ProxyPass` example and the backend-TLS notes are in `install/INSTALL.md`.


### Reruns and recorded installation command

The installer also refuses a non-empty `--prefix` that is not recognized as an SunaQ installation. This is a safety boundary because profile refreshes replace selected top-level paths such as `rag/`, `docs/` and `clients/`. A typo such as pointing `--prefix` at an unrelated application directory must therefore fail before any files are changed. Fresh 0.8.6 installs should use the default dedicated `/opt/sunaq` path. Recognized older installations keep their existing prefix and legacy marker compatibility; new installs write `.sunaq-installation` plus installer state for future reruns.


After installation, keep `/opt/sunaq/install/last-install-command.sh` with the
host's operational records. Prefer retaining the explicit `--profile`,
`--deployment` and `--preset` switches even when they match defaults; the
recorded command then documents both deployment topology and architecture tier. The installer writes the exact shell-escaped wrapper command
used on the last run so later maintenance does not depend on reconstructing profile,
URLs, CA files or optional component switches from memory.

Before a rerun, review the stored command and run the equivalent command with `--plan`
first. The Super-Light installer performs an early preflight for required source paths,
the installation prefix, CA files and Docker availability. If an existing stack is detected, the preflight inspects its Compose services. **Any
running SunaQ service is a hard preflight error** and no installation changes are made;
stop the complete Super-Light Compose stack before a rerun. A recognized installation
whose stack is fully stopped is allowed through the repair/rerun path. Missing input
files or an unreachable installed Docker daemon also fail before the source tree is
replaced.

A rerun preserves the site's existing `config.yaml`; it does **not** merge newly introduced optional configuration keys into that file automatically. After updating to a newer RC, compare the shipped reference configuration/changelog and add desired new options manually. For RC5 this includes, for example, `acl.prefilter.enabled` (off by default when absent/preserved).

## 3. First post-install configuration

Fresh installs use `/opt/sunaq`. If this is an upgrade of a recognized legacy
installation, substitute its retained prefix in the commands below.


The installer prints the SunaQ Admin credential and provider API key and records the local runtime values in `/opt/sunaq/runtime.env`. Treat that file as a secret. Fresh installs and reruns enter maintenance mode: the provider authenticates trusted client keys but returns only the maintenance response, while normal API/background workers remain stopped. Configure the actual LLM and optional Web Search credentials before user acceptance, then leave maintenance mode with:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh status
sudo /opt/sunaq/install/maintenance-mode.sh off
```

Use `maintenance-mode.sh on` again before key rotation, restore work or comparable maintenance.

Useful Super-Light commands:

```bash
cd /opt/sunaq/install/super-light
./status-super-light.sh
docker-compose ps
docker-compose logs --tail=100 api provider
docker-compose restart api provider
docker-compose logs -f mail-worker
```

Do not edit credential rows in `runtime/users.sqlite` with ad-hoc SQL. Use the
Admin UI or supplied CLIs.

### 3.1 Backup and restore

RC5 provides the console-first recovery tool `install/backup-restore.sh`. Create and restore operations require SunaQ maintenance mode; `verify` is read-only. Use a backup target **outside** the installation prefix:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh on
sudo /opt/sunaq/install/backup-restore.sh create /srv/aki-backups
```

`create` writes a timestamped directory such as `aki-rag-backup-20260922-123702Z` and verifies it before publishing it. The recovery set contains SunaQ-owned configuration/runtime state, SQLite state including `runtime/users.sqlite`, the matching credential master key, private CA/TLS/operator state below the installation prefix and bundled Neo4j when selected. It deliberately does not back up Nextcloud, Elasticsearch, rebuildable Qdrant, external Neo4j, OpenWebUI/Playwright state or model caches.

Treat the recovery directory as a secret: it contains service credentials and the credential master key. Store it with restrictive permissions and, where appropriate, encrypted/off-host.

Verify an existing recovery set independently with:

```bash
sudo /opt/sunaq/install/backup-restore.sh verify \
  /srv/aki-backups/aki-rag-backup-YYYYMMDD-HHMMSSZ
```

Restore only after verifying the selected set and while maintenance mode is active:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh on
sudo /opt/sunaq/install/backup-restore.sh restore \
  /srv/aki-backups/aki-rag-backup-YYYYMMDD-HHMMSSZ --yes
```

Restore is intentionally conservative: the supported deployment profile/mode and installation prefix must match the recovery set. Existing SQLite main/WAL/SHM state covered by the set is replaced coherently; bundled Neo4j is restored when included. A successful restore **leaves SunaQ in maintenance mode**. Run smoke/health/live-ACL checks and at least one authenticated document query before returning to normal service:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh off
```

The RC5 Super-Light acceptance test exercised a real `users.sqlite` loss: the provider failed closed with an invalid-client 401, then the verified restore recovered the registered provider-client/user credential state and normal authenticated requests. The same recovery set included the bundled Neo4j snapshot/restore step.

On Super-Light, a warning that `/app/runtime/ca/...` is an external `ca_file` can currently be path-normalization noise: `runtime/ca/` is explicitly included in the recovery set. Confirm the expected CA file is present in `files.tar`; genuinely external CA paths remain operator-owned dependencies.

For cross-system recovery order, key/master-key pairing and deletion/lifecycle scope, see `DATA-LIFECYCLE.md`.

## 4. SunaQ Recherche 0.3.2

SunaQ Recherche is the preferred slim Nextcloud UI for this beta. It targets Nextcloud 23+.
Install the `sunaq` app in Nextcloud, enable it, then configure **SunaQ URL**
and **Provider API key** under **Settings → Administration → Additional settings**.

The app proxies server-side and sends the current Nextcloud UID; the provider key
never reaches browser JavaScript. Credential-bearing app requests require HTTPS by
default. An administrator can explicitly enable insecure HTTP for controlled lab/test
networks, but that opt-in sends the provider key and request content without transport
encryption and is not a normal deployment mode. Each user completes Nextcloud Login
Flow once so the middleware can perform live ACL checks with that user's current
credential.

SunaQ Admin configures exactly one chat-archive target path per canonical user
(default `SunaQ-Chats`). The global `chat_archive.enabled` capability controls
both new writes by the bundled app and retrieval through `/chatarchive`; with the
capability off, a chat remains only in the current browser session unless the user
copies it elsewhere manually. Changing the path does not copy or move existing
files. For an older RC archive, either leave that user on `AKI-Chats` or move the
archive once into the selected path. Do not maintain two simultaneously active
chat archives for one user. Deleting a managed visible Markdown chat in Nextcloud
also deletes its hidden SunaQ metadata sidecar; opening/listing chats additionally
prunes older orphan sidecars.

If the middleware URL is an RFC1918/private address, Nextcloud can reject the
server-side request with `Host violates local access rules`. For a deliberately
internal deployment set the global Nextcloud option `allow_local_remote_servers`
to true and make sure the Nextcloud host trusts the middleware TLS issuer.

### Public `/v1/` pressure controls

The OpenAI-compatible provider surface remains externally reachable through the
reverse proxy so a separate trusted client such as OpenWebUI can connect. Provider
routes are nevertheless authenticated with a high-entropy registered Bearer client
key before user identity or retrieval work is accepted.

Bundled nginx applies a dedicated per-source-IP limit of 10 requests/s with a
burst allowance and a maximum of 16 concurrent `/v1/` connections; excess requests
receive HTTP 429. Persistent client/user lockouts after failed authentication are
intentionally not used because an unauthenticated attacker could weaponize them to
lock out a known legitimate client.

When bundled nginx sits behind another reverse proxy, the rate-limit key must
resolve to the real client address. The shipped configuration trusts
`X-Forwarded-For` only from loopback (`127.0.0.1` / `::1`), covering the
documented same-host Apache setup. For a remote load balancer, add only that
balancer's exact address/network via `set_real_ip_from`; never trust forwarded
client-address headers from arbitrary peers.

These controls bound application pressure, not volumetric DDoS. Keep the provider
itself on loopback/private service networking and expose only the reverse proxy.
Internet-facing sites that need protection against link or host saturation require
upstream firewall/load-balancer/provider DDoS controls.

## 5. Contact seeds / Graph-Lite

After a user has completed Login Flow, open:

```text
SunaQ Admin → Users → <Nextcloud login> → Kontakt-DB
```

Enable/configure the source and choose **Jetzt synchronisieren**. CardDAV uses the
already stored Nextcloud credential; no second CardDAV password is needed. The
operational identity is the human Nextcloud login; `canonical_user_id` remains an
internal join key.

CLI equivalent:

```bash
cd /opt/sunaq/install/super-light
./contacts.sh list
./contacts.sh status --user alice
./contacts.sh books --user alice
./contacts.sh sync --user alice
```

Use `--server URL` only when the same login exists on multiple Nextcloud servers.
Missing credential, disabled seed source and empty address book are clean no-ops.

The Admin UI starts the contact import as a background job and shows a progress bar with processed/total, written, repaired, removed and error counts. **Adressbücher ermitteln** lists the CardDAV display name and technical slug accepted by include/exclude filters. A full successful scan also removes ContactRecords whose CardDAV href disappeared; parse failures are still counted as seen and are not mistaken for deletions.

## 6. Retrieval behavior in Super-Light

Super-Light uses Elasticsearch as the required document arm and Neo4j for
entity/alias expansion and lightweight research findings. Qdrant and the local
reranker are disabled.

Every normal request first produces one small SearchSpec. In
Super-Light its lexical fields are compiled to Elasticsearch; `semantic_query`
is retained for portability but is not executed because Qdrant is disabled.
Neo4j may add known seed/alias forms before the Elasticsearch request. Normal INFO logging records only bounded control/count metadata for this path;
query text and Elasticsearch request bodies are not emitted at INFO. The shipped rc1 profiles deliberately use one retrieval round. Additional rounds
are deferred until the budget-only profile comparison has been accepted.

The absence of a reranker does **not** disable deduplication. Near-identical text
and common PDF/ODT/copy variants are collapsed before the final candidate path.
The normal verification/answer budget now comes from the selected SunaQ profile:
Schnell 10, Gründlich 30 and Tief 50 candidates/documents, subject to deployment
capabilities and administrator hard ceilings.

Live Nextcloud ACL remains mandatory. Unauthorized results are removed and do not
trigger adaptive retrieval/backfill merely to fill the context. In the current
normal path ACL follows the bounded ranking decision, so a narrow-rights user may
receive fewer results; this is an explicit acceptance-test case rather than a hidden
failure.

If Elasticsearch is unavailable, the API returns service-unavailable semantics and
the UI should show `Dokumentensuche derzeit nicht verfügbar` rather than exposing a
generic 500/502 as the user-facing result.

## 7. Web Research and archive

The shared Playwright renderer uses a 1440×900 desktop viewport and A4 Landscape
PDF. Rendering is archival enrichment scheduled after the synchronous evidence/archive write, so slow Chromium rendering does not block the user answer. Cookie/harmless-overlay handling is bounded best effort; ordinary browser
storage can persist per requested host so a consent choice may survive future
captures. The archive deliberately does not bypass login walls, paywalls,
CAPTCHAs or access restrictions.

Per-source metadata JSON is stored as a hidden dotfile, for example:

```text
.01-source.metadata.json
```

The PDF is a readable research snapshot, not WARC/WACZ or a forensic browser
archive. Raw HTML, when enabled, is only the fetched main response and does not
bundle every subresource.

## 8. Acceptance checklist before opening the beta

Use at least one real account with ordinary documents and one second account with
different ACLs.

1. `status-super-light.sh` reports API/provider/Neo4j ready. If Playwright was explicitly installed with `--with-playwright`, verify the renderer is ready as well.
2. SunaQ appears in Nextcloud navigation and opens without manual URL entry.
3. User 1 completes Login Flow and can query an authorized document.
4. User 2 cannot receive evidence for a document they cannot access.
5. Kontakt-DB sync succeeds for one user; Neo4j shows ContactRecords/provenance.
6. Review one identity candidate: **Identisch** must create non-destructive `SAME_AS` with both Entities still active; **Verschieden** must create `NOT_SAME_AS`. Use technical merge separately only for a true redundant SunaQ Entity.
7. With two files that have the same valid extracted-content hash, verify they consume one duplicate group while live ACL still checks both file IDs and can promote the authorized copy if the ranked representative is denied.
8. A normal document question works with the Super-Light 10-candidate verifier window.
9. **If Web Research/Web archive is enabled** (and Playwright is installed when rendered PDFs are required), run one Web Research request and verify the selected source archive/metadata; with Playwright enabled, also verify the desktop/Landscape PDF.
10. Stop Elasticsearch temporarily and verify the friendly unavailable response.
11. Restore Elasticsearch and verify retrieval recovers without state repair.
12. Verify that `/health` reports `live_acl.enabled=true` on a shared beta instance.
13. If shared alias/Graph-Lite is enabled, verify that User 2 may benefit from a curated alias without receiving the protected source document as evidence.
14. If `/chatarchive` is enabled, verify that the saved chat obeys the ACL of its own Nextcloud archive file and document its independent retention semantics.
15. Run `docker-compose down` / `docker-compose up -d` and repeat one document query. **If Web Research is enabled**, repeat one Web query as a separate optional-capability acceptance check.

The rc4.3 blank-VM pass completed for both supported mappings. That historical Super-Light/dockerized pass used Playwright by default; rc1.1 changes the fresh-install baseline so Playwright is now explicit `--with-playwright`. Standard/native passed installation, document search and SunaQ Admin checks; when selected with `--with-playwright`, the renderer was built and started automatically. The earlier Leap 15.3 beta host additionally exercised CardDAV import/reconciliation, Web Research archive creation, IMAP→WebDAV mail import with attachments/OCR and the long-running Docker mail worker. Rerun this acceptance checklist before production rollout. RC5 incremental field acceptance additionally covers the ACL prefilter/two-user unspecific behavior, Markdown chat continuation and the Super-Light backup/restore roundtrip described above.

## 9. Resource reference

Observed on the Super-Light acceptance VM:

```text
assigned RAM:      ~3.9 GiB
idle RAM in use:   ~1.7 GiB / 44%
CPU idle:          ~99%
load average:      ~0.09 / 0.09 / 0.08
swap:              none
```

This is an observed test point, not a guaranteed ceiling. 4 GiB is a practical
minimum for the tested profile; 4–8 GiB is recommended when Chromium/Web Research
will be used. Nextcloud, Elasticsearch and the LLM are external in this figure.

## 10. Beta freeze

0.8.6-rc1 is the current deployment/operations release-candidate baseline.
Expected follow-up work before broader feature expansion is security/curation
hardening, documentation consistency and adversarial code-vs-docs tests (ACL,
aliases, Findings, archive boundaries and untrusted content). A change that alters
the trust model, live ACL invariant, credential ownership, deployment axes or
evidence pipeline should be treated as an architectural change and not slipped into
a retrieval-quality patch.


## Source-origin mirror and reconcile

Archive scopes (`/mailarchive`, `/webarchive`, `/chatarchive`) filter on mirrored `source_origin` values before the Elasticsearch/Qdrant candidate windows. The middleware registry keyed by Nextcloud `files:<id>` is the durable source of truth for special archive origins. Normal retrieval performs a lightweight id-based mirror repair whenever the registry changes; this avoids a full index scan in Super-Light.

A full Elasticsearch reset/reindex is explicit administrator work. Afterwards run:

```bash
cd /opt/sunaq/install/super-light
docker-compose exec api python -m rag.source_registry reconcile
```

For native/standard deployments run the equivalent module with the installed venv. Reconcile prefers `.mailmeta.json` membership data for mail recovery, then falls back to the normalized `CONTENT-KIND: EMAIL` marker and configured archive roots.

## TLS strict mode

Normal TLS verification is enabled by default. `tls.x509_strict` defaults to `false` for compatibility with older private PKIs; this only disables the additional Python/OpenSSL `VERIFY_X509_STRICT` flag. Use `--x509-strict` to opt into strict RFC-5280 checks. Do not use `verify_tls: false` as a substitute.

## Query formulation in Elasticsearch-only operation

In an Elasticsearch-only deployment, retrieval quality is more sensitive to the
lexical form of the user's query than in a hybrid setup. For example,
`Project Alpha 42` and `"Project Alpha 42"` are intentionally not equivalent:
the first allows the rewriter to use the individual lexical anchors, while the
quoted form requests the multi-word string as one phrase. Quoting can therefore
materially narrow the candidate set when names, project labels, identifiers or
OCR-heavy documents contain otherwise common tokens.

This is expected behaviour for a lexical retrieval arm rather than an ACL or LLM
failure. The effect should become less pronounced as entity resolution and alias
coverage improve, because better-resolved entities provide stronger canonical
search anchors. In ERG/hybrid deployments, an additional semantic vector arm
such as Qdrant can further reduce dependence on exact lexical wording by
providing semantically similar candidates as a complementary recall signal.
Vector retrieval remains complementary: live Nextcloud ACL and downstream
verification/evidence handling remain authoritative boundaries, and explicit
quoted lexical constraints are preserved when the user supplies them.

