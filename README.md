# SunaQ

**Evidence-first AI gateway for existing Nextcloud deployments.**

> **Keep your Nextcloud as is. Add modern AI around it.**

Add modern AI-assisted research and RAG to your established Nextcloud document estates — without replacing Nextcloud as the document store, rebuilding its permission model or requiring full-corpus vectorization. Full-corpus vectorization, graph building and other research features are optional, not mandatory.

SunaQ reuses existing Nextcloud FullTextSearch / Elasticsearch infrastructure and keeps Nextcloud as the final authorization authority for private document evidence. Start with an Elasticsearch-centric core deployment (SRC) and add semantic retrieval, reranking, Graph-Lite or additional research sources only where they provide value.

**Super-Light can typically be up and running in under 10 minutes** when the required Nextcloud, Elasticsearch and model endpoints are already available. Actual installation time depends on network speed, host performance, image/package downloads and site configuration.

[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)
[![Status: Public Beta](https://img.shields.io/badge/status-public%20beta-orange.svg)](CHANGELOG.md)

Behind an OpenAI-compatible provider interface, SunaQ combines FullTextSearch / Elasticsearch with optional semantic retrieval, graph signals, mail ingestion, web research and archived chats. SunaQ owns retrieval, ACL enforcement, evidence preparation and provenance; the downstream model/backend remains replaceable.

The central security invariant is deliberately simple:

> **Retrieval systems propose candidates. Nextcloud remains the final authorization authority.**

Every document candidate is checked live against Nextcloud for the authenticated user before it can become answer evidence. If an otherwise relevant document is not authorized, it is removed rather than replaced by a weaker result merely to fill the context window.

> **Project status:** `0.8.6-rc1.2` is the current development release-candidate, adding enforced SRC/ERG boundaries and hardening. See `CHANGELOG.md`, `models/README.md`, `docs/BETA-OPERATIONS.md` and `docs/KNOWN-LIMITATIONS.md`.


## Why this project exists

Many organizations already have a useful Nextcloud document estate, working permissions and a FullTextSearch / Elasticsearch index. Replacing that environment merely to add AI-assisted research is often unnecessary, expensive or undesirable.

SunaQ follows a brownfield integration approach: keep the existing document store, search infrastructure and permission model in place, and add modern AI capabilities around them. Operational complexity and external data exposure remain under administrator control.

The project focuses on eight practical goals:

- **Existing-estate / legacy-friendly deployment.** The Super-Light profile reuses an existing Nextcloud FullTextSearch / Elasticsearch index and is tested on an openSUSE Leap 15.3 host. The bundled SunaQ Recherche UI targets Nextcloud 23+.
- **No document migration.** Nextcloud remains the document store and authorization authority; SunaQ does not require a separate AI knowledge base to become the system of record.
- **No mandatory full-corpus vectorization.** Super-Light can remain Elasticsearch-centric indefinitely. Qdrant and embeddings are optional scale-up components rather than prerequisites for first use.
- **UI agnostic.** The middleware exposes an OpenAI-compatible provider path. The included SunaQ Recherche app is a slim Nextcloud-native UI; external OpenWebUI deployments can use the same middleware.
- **Small local footprint.** Current Super-Light runs without Qdrant and without a local reranker. On the current acceptance VM, the local middleware services used about **1.7 GiB RAM at idle** while Nextcloud, Elasticsearch and the LLM were external. This is an observed test point, not a guaranteed ceiling; 4 GiB remains the practical VM minimum when Chromium-based web archiving is enabled.
- **Optional research sources.** The fresh-install baseline is ordinary Nextcloud documents only. Imported mail, archived web evidence, saved SunaQ chats and live public-web research are separate administrator-enabled extensions.
- **Scale up without changing the core.** The same codebase can add Qdrant semantic retrieval, a reranker and richer Neo4j/Graph-Lite functionality when resources and use cases justify them.
- **Data minimization by design.** Retrieval, indexing, embeddings and ACL checks can remain local. LLM roles are independently configurable and may be local or remote. A fully local deployment is possible when local model and web-search choices are used.

## Default architecture boundary

In `0.8.6-rc1.2`, a fresh Super-Light installation is the formal
**Secure RAG Core (SRC)** package:

```text
Nextcloud documents
       |
FullTextSearch / Elasticsearch (files only)
       |
query rewrite / one bounded retrieval round
       |
LIVE NEXTCLOUD ACL
       |
local or administrator-approved LLM roles
```

SRC is enforced rather than descriptive. Live ACL must be enabled; document
evidence is Documents-only; vector/Qdrant, document-graph retrieval, Mail,
Web/Web archive, Chat archive and Research Findings are unavailable. Model
roles may be local or remote by explicit administrator choice; remote evidence
caps still apply. Reranker processing remains inside the local/private trust
boundary. Neo4j may
still provide administrator-owned seed/alias context without becoming a document
evidence arm.

**ERG — Eboracum Research Gate** is the opt-in extension envelope for additional
sources, derived stores, broader external egress surfaces and more complex
research functions. Remote LLM use alone does not require ERG. Enabling an
ERG-only capability is therefore an explicit architecture
decision rather than an accidental drift away from SRC.

Shipped YAML overlays `core` and `workgroup` can be selected with
`--preset core|workgroup` or a site-specific `--preset-file FILE`.
Existing installations without an `architecture.tier` remain ERG-compatible
for upgrade safety; they are not retroactively labelled SRC.

### Prompt-injection boundary in the default profile

SunaQ does not treat document text as executable control input. In the default
one-round profiles, query rewriting happens before corpus evidence is supplied to
the planner. The planner emits a bounded `SearchSpec`; middleware code, not the
model, compiles backend requests. ACL prefilter identity/groups are resolved
server-side from Nextcloud, and every final private-document candidate is checked
again through live Nextcloud WebDAV ACL before verifier or answer use.

An authorized document can still contain adversarial instructions and influence a
Verifier or answer model. In the default profile this is primarily an
**evidence-integrity / answer-quality risk**, not an authorization or arbitrary
code-execution path: model output cannot grant file access, change the current
Nextcloud identity, execute raw Elasticsearch DSL or invoke arbitrary write
operations.

Optional multi-round retrieval, Web/Mail/Chat evidence and
Graph/Findings persistence do not replace the deterministic application control flow with model-controlled execution, but they do enlarge the integrity surface and therefore remain administrator-controlled features.

## Architecture at a glance

```text
                 SunaQ Recherche / OpenWebUI / API client
                                |
                       OpenAI-compatible provider
                                |
                     Query rewrite / SearchSpec
                                |
                +---------------+---------------+
                |                               |
        internal document path              live web path
                |                               |
       Neo4j seed/alias context           search provider
                |                               |
       +--------+---------+               fetch + relevance
       |                  |                    gate
 Elasticsearch         Qdrant                   |
  required arm         optional                 |
       +--------+---------+                     |
                |                               |
          fusion / dedup                        |
                |                               |
        optional reranker                       |
                |                               |
        LIVE NEXTCLOUD ACL                      |
                |                               |
       optional verifier                        |
                +---------------+---------------+
                                |
                  reasoning / answer backend
                                |
              answer / structured outcome + sources
```

Elasticsearch, Qdrant and Neo4j are retrieval systems, not authorization systems. The live Nextcloud ACL check is intentionally downstream of candidate retrieval and upstream of document evidence sent to verifier or answer roles.

## Architecture tiers: SRC and ERG

SunaQ distinguishes two enforced capability envelopes:

- **SRC — Secure RAG Core:** ordinary Nextcloud documents, Elasticsearch
  `files` retrieval, mandatory live Nextcloud ACL, one retrieval round and
  local or administrator-approved remote model processing. ERG-only sources and derived document stores
  are rejected at startup/request boundaries.
- **ERG — Eboracum Research Gate:** the extension space for additional sources,
  vector retrieval, Findings/Graph-Lite, document graph
  processing and future multi-round research. ERG is a menu; it does not imply
  that every extension is enabled.

The architecture tier is independent of **Standard / Super-Light** deployment
and **Schnell / Gründlich / Tief** research models. Super-Light ships as SRC in
rc1.2; the full reference configuration remains ERG for compatibility with
existing installations. See [SRC and ERG architecture](docs/SRC-ERG.md).

## Deployment profiles

| Profile | Intended use | Local components | External dependencies |
| --- | --- | --- | --- |
| **Super-Light** | lightweight, Elasticsearch-centric component profile | API, provider, Neo4j seed/alias context; optional Playwright/nginx | Nextcloud, FullTextSearch/Elasticsearch, LLM |
| **Standard** | larger/hybrid retrieval installations | native middleware plus optional Qdrant, reranker, Neo4j, OpenWebUI | Nextcloud, Elasticsearch; model backends as configured |

Profile and deployment mechanism are conceptually separate axes. For the 0.8.6-rc1.2 candidate, the regression-tested and supported mappings remain:

- `super-light + dockerized`
- `standard + native`

Other combinations are not yet supported. Super-Light is a profile of the same middleware, not a separate fork; `dockerized` is the deployment mechanism that currently provides the tested legacy-host compatibility.

## Research sources

The middleware keeps source selection separate from retrieval-engine selection.

- `/documents` — ordinary Nextcloud documents
- `/mailarchive` — imported mail and attachments
- `/webarchive` — archived web-research evidence
- `/chatarchive` — saved SunaQ conversations
- live `/web` research — current public web evidence, separately fetched and checked

Archive origins are tracked by stable Nextcloud file IDs and mirrored into retrieval indexes so source scopes can be applied before candidate limits.

Without an explicit source directive, SunaQ searches ordinary Nextcloud documents only. Mail, web archives, saved chat archives and live Web research are optional capabilities. The provider enforces the effective capability for the authenticated user: optional sources are usable only while the corresponding global service gate and per-user gate are enabled. Explicit `/mailarchive`, `/webarchive`, `/chatarchive` or `/web` directives cannot bypass that policy. The bundled SunaQ Recherche client renders these optional source controls fail-closed: they start hidden and are shown only after the authenticated capability response confirms availability. **Documents** remain the always-visible baseline source.

Archive scopes are optional. In particular, saved chats are useful as shared/flat-hierarchy working memory, but they are deliberate retained copies: a saved conversation can contain text derived from another document and then has its own Nextcloud file ID, ACL and lifecycle. In SunaQ Recherche 0.3.4 chat archiving is enabled only when both the global `chat_archive.enabled` gate and the canonical user's chat-archive switch are enabled. Otherwise new chats remain session-local, the Chats source control is not shown and explicit `/chatarchive` retrieval is rejected. Manual deletion of a managed Markdown chat removes its hidden metadata sidecar in the same Nextcloud file-operation path, with list/load self-healing for older orphan sidecars.

## Privacy and trust boundaries

A private document corpus does not need to be exposed wholesale to an external LLM provider. In the reference architecture:

- Nextcloud/Elasticsearch retrieval stays inside the administrator-controlled environment.
- Qdrant and embeddings may remain local.
- ACL authorization is checked live against Nextcloud.
- planner, verifier, evidence-control and answer roles are independently configurable.
- remote roles have bounded document/count/character budgets.
- full-document graph extraction is a separate opt-in trust decision and is disabled by default in the reference configuration.

Those controls reduce disclosure; they do not make remotely transmitted evidence non-sensitive. Administrators remain responsible for deciding which roles may use remote model providers.

The minimal profile also reduces prompt-injection exposure by keeping Web, mail
ingestion and chat evidence outside the default retrieval path. Query rewriting
interprets the user's request before corpus evidence is loaded, but it is not a
general prompt-injection sandbox: an authorized document can still contain
adversarial text that reaches later verifier/answer stages. Corpus evidence must
therefore continue to be treated as untrusted data.

SunaQ also distinguishes **shared retrieval knowledge** from **document evidence**. Curated names/aliases and shared Finding decisions may be reused across users so that the organization benefits from prior curation. That reuse does not grant access to the document that originally motivated the knowledge: document text still needs the current user's live Nextcloud authorization before it becomes answer evidence.

See `docs/PRIVACY-ARCHITECTURE.md`, `docs/THREAT-MODEL.md` and `SECURITY.md` for details.

## Compatibility and tested baseline

The project intentionally keeps support for older installations in scope rather than requiring a current Linux/Python stack everywhere.

Current 0.8.6-rc1.2 reference points:

- **SunaQ Recherche 0.3.4:** Nextcloud 23+
- **Super-Light acceptance host:** openSUSE Leap 15.3
- **Document retrieval:** existing Nextcloud FullTextSearch / Elasticsearch
- **Internal PKI:** supported, including compatibility mode for older private certificate chains without disabling ordinary TLS verification
- **Answer provider:** OpenAI-compatible; both SRC and ERG may use local or administrator-approved remote model roles

Compatibility statements describe the current tested/project target, not a promise that every combination of Nextcloud, Elasticsearch, proxy and model backend is regression-tested.

For current Nextcloud deployments, the native baseline to evaluate is Nextcloud Context Chat. SunaQ is not intended to out-feature that supported ecosystem; it addresses a different operating model: reuse of an existing FullTextSearch estate, a replaceable OpenAI-compatible provider boundary, optional Graph-Lite and live Nextcloud authorization of concrete document candidates. See `docs/NEXTCLOUD-CONTEXT-CHAT.md` for the neutral comparison and current caveats.

## Quick start: Super-Light

Inspect the installation plan before changing the host:

```bash
sudo ./install/install.sh \
  --profile super-light \
  --deployment dockerized \
  --preset core \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --plan
```

Then install with the same arguments, removing `--plan` and adding only the components you want. Although Super-Light already ships from the SRC baseline, keeping `--preset core` explicit makes the saved installation command a reproducible declaration of the architecture contract rather than relying on defaults. For an internal PKI, use repeatable `--ca-certificate FILE` arguments rather than disabling TLS verification. Fresh installs and installer reruns enter an explicit **maintenance mode** first: the OpenAI-compatible provider remains reachable and authenticates trusted client keys, but returns a maintenance message without loading the normal SunaQ/LLM pipeline. After configuration and checks, use `sudo /opt/sunaq/install/maintenance-mode.sh off` on a fresh 0.8.6 installation to start normal operation. Recognized legacy installations keep their existing prefix (for example `/opt/nextcloud-rag`) rather than being moved.

Detailed installation and acceptance steps are in `install/INSTALL.md` and `docs/BETA-OPERATIONS.md`.

## Front ends

### SunaQ Recherche

The included `clients/nextcloud/sunaq/` app is a slim Nextcloud-native research UI. It targets Nextcloud 23+, proxies server-side to the middleware, keeps the provider key out of browser JavaScript, exposes the user's allowed SunaQ profiles, shows request progress/follow-up actions and stores saved conversations per user in Nextcloud.

### OpenWebUI & other Frontends

OpenWebUI can be used as an external client through the provider interface, the installer offers the option to include OpenWebUI in the installation process. The middleware does not depend on OpenWebUI-specific retrieval or knowledge features, so the UI of your choice may be used, as long as it supports sending an appropriate client identification in the request header.

When SunaQ is selected as the model, client-side Knowledge/RAG/File-Context
injection should be disabled: SunaQ is intended to remain the retrieval and
evidence authority. Replayed client `user`/`assistant` history is still used for bounded follow-up
resolution and is therefore not authoritative provenance. Authoritative
server-side conversation state is deferred to rc2.

The same OpenAI-compatible boundary can be used by other local frontends, RAG systems, agents or research tools when an administrator deliberately registers them as trusted clients. A trusted-client key is an integration-server credential: keep it server-side and restrict externally reachable provider endpoints by network policy/reverse-proxy allowlists or equivalent controls where practical.

The API/provider boundary is intentional: front-end choice should not define the retrieval architecture.

## Optional scale-up path

An SRC deployment can remain Elasticsearch-centric indefinitely. Where the workload justifies additional capabilities, the same middleware can move into the ERG envelope and add:

- **Qdrant** for semantic/vector retrieval,
- a **cross-encoder reranker**,
- **Neo4j** beyond seed/alias expansion,
- curated **Graph-Lite** entities, mentions and relation observations,
- local model services for a completely self-hosted processing path.

The graph layer is deliberately conservative: retrieved or LLM-derived observations are not automatically promoted to global facts merely because they were extracted.

## Documentation

- `install/INSTALL.md` — fresh-machine installation
- `docs/BETA-OPERATIONS.md` — beta runbook and acceptance checklist
- `docs/ARCHITECTURE.md` — architecture and trust model
- `docs/PRIVACY-ARCHITECTURE.md` — local/remote processing boundaries
- `docs/THREAT-MODEL.md` — adversaries, shared retrieval knowledge, ACL and archive boundaries
- `docs/DATA-LIFECYCLE.md` — deletion, backup/restore and derived-store lifecycle
- `docs/NEXTCLOUD-CONTEXT-CHAT.md` — relationship to Nextcloud's native Context Chat architecture
- `docs/TECHNICAL-REFERENCE.md` — detailed configuration and APIs
- `docs/ADMINISTRATION.md` — user, credential, mail, web and graph administration
- `docs/GRAPHLIGHT-FINDINGS.md` — Findings curation and Graph-Lite safety boundary
- `docs/KNOWN-LIMITATIONS.md` — known limitations and deferred polish
- `docs/ROADMAP.md` — implemented 0.8.6 direction and explicitly deferred follow-up work
- `docs/DEVELOPMENT.md` — repository layout and test baseline
- `SECURITY.md` — security model and vulnerability reporting
- `RELEASE-NOTES-0.8.6-rc1.2.md` — current development release-candidate notes
- `RELEASE-NOTES-0.8.6-rc1.1.md` — current public release-candidate notes
- `RELEASE-NOTES-0.8.5-rc5.1.md` — earlier public-beta release notes
- `RELEASE-NOTES-0.8.5-rc5.md` — preceding release-candidate notes
- `CHANGELOG.md` — detailed development/change history
- `CONTRIBUTING.md` — contribution and licensing policy
- `CLA.md` / `docs/CLA-PROCESS.md` — contributor rights without copyright assignment
- `CODE_OF_CONDUCT.md` — community conduct expectations
- `RELEASE-NOTES-0.8.5-rc4.3.md` — preceding accepted/public release-candidate notes
- `RELEASE-NOTES-0.8.5-rc4.2.md` — previous hotfix release notes
- `RELEASE-NOTES-0.8.5-rc4.1.md` — previous hotfix release notes
- `RELEASE-NOTES-0.8.5-rc4.md` — previous release-candidate notes
- `RELEASE-NOTES-0.8.5-rc3.md` — first public release notes

## Development and tests

```bash
python -m pytest
```

Before a release candidate is tagged, the project also performs syntax/configuration checks, blank-VM installation/acceptance for the intended profile, manifest regeneration and a final scan for runtime state and secrets.

Runtime databases, environment secrets, TLS material and local deployment state must never be committed.

## License and commercial licensing

The public source is licensed under **GNU AGPL-3.0-only** unless a file states otherwise. See `LICENSE` and `COPYRIGHT`.

Eboracum GmbH intends to preserve the option of offering the same code under separate commercial/proprietary terms for organizations that require an alternative license. This does **not** withdraw or reduce the rights already granted for AGPL releases.

Because dual licensing requires a clean rights chain, non-trivial copyrightable contributions require the project CLA before merge. **Contributors retain ownership**; the CLA is an additional non-exclusive grant and commits accepted contributions to continued public AGPL availability.

See `CONTRIBUTING.md`, `CLA.md` and `COMMERCIAL-LICENSING.md`.

## Project status and maintenance

Public release does not imply an SLA or guaranteed support lifetime. Questions about the project can be sent to `rag@eboracum.de`.

Should active development end, the preferred lifecycle is to mark the project as maintained only for critical fixes and eventually archive the repository rather than erase the public history. Forks remain part of the freedoms provided by the AGPL.

See `docs/PROJECT-GOVERNANCE.md`.

## Renaming

Releases up to and including `0.8.5-rc5.1` were published under the
`aki-rag-middleware` name. To avoid confusion with the Berlin-based
aki.io GmbH, we decided to rename the project to **SunaQ**. We hope that the new
name does not conflict with any existing project, product or company in this
field.

As part of the rename, current user-facing names and new project-local
identifiers no longer use `AKI` or `Nextcloud` where those terms are not
required for compatibility. For example, fresh installations now default to
`/opt/sunaq` instead of `/opt/nextcloud-rag`. References to Nextcloud remain
where necessary to describe compatibility with the Nextcloud platform.

Some textual, configuration, protocol, archive and compatibility references to
the previous names intentionally remain so existing installations can continue
to work and upgrade safely.

Please accept our apologies for any inconvenience caused by this decision.

## Trademark notice

SunaQ is an independent project and is not affiliated with, sponsored by, or endorsed by Nextcloud GmbH or aki.io GmbH. Historical repository and compatibility identifiers may still use the earlier AKI naming during the 0.8.6 transition. “Nextcloud” is used descriptively to identify compatibility with the Nextcloud software platform. Nextcloud and related marks are trademarks of Nextcloud GmbH. References to aki.io are solely for identification and do not imply any affiliation, sponsorship, or endorsement.

See `TRADEMARKS.md`.

### Elasticsearch-only query wording

Lexical retrieval remains sensitive to query form. For example,
`Project Alpha 42` and `"Project Alpha 42"` are intentionally different
searches: the quoted form keeps the words together as one phrase. Better
entity/alias resolution should reduce this sensitivity; ERG deployments can
additionally use Qdrant semantic retrieval as a complementary recall signal.

