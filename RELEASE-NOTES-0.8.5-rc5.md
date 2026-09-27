# AKI RAG Middleware 0.8.5-rc5

**Date:** 22 September 2026  
**Status:** release-candidate baseline; RC5 incremental acceptance completed

## Purpose

`0.8.5-rc5` consolidates the operational and retrieval hardening developed after rc4.3. The main themes are an explicit maintenance/recovery path, a conservative ACL-aware retrieval prefilter, stronger Graph-Lite identity/Findings lifecycle handling, and a more usable Nextcloud-native chat archive.

The release keeps the central authorization invariant unchanged:

> Retrieval systems propose candidates. Nextcloud remains the final authorization authority for private document evidence.

Live Nextcloud WebDAV ACL remains mandatory before private document content can become verifier or answer evidence.

## Maintenance mode and recovery

RC5 introduces an explicit maintenance state:

```bash
sudo /opt/nextcloud-rag/install/maintenance-mode.sh status
sudo /opt/nextcloud-rag/install/maintenance-mode.sh on
sudo /opt/nextcloud-rag/install/maintenance-mode.sh off
```

Fresh installs and installer reruns enter maintenance mode first. The normal retrieval/API/workers are not exposed as ready while invasive maintenance is in progress. A minimal authenticated OpenAI-compatible provider remains available so trusted frontends receive a stable maintenance response instead of a proxy/startup failure.

On Super-Light/dockerized deployments, leaving maintenance mode starts the required services, waits for Neo4j/schema readiness, and only then recreates the normal provider. If normal startup fails, AKI returns to maintenance mode instead of leaving a partially started user-facing stack.

RC5 also adds console-first backup/restore for AKI-owned state:

```bash
sudo /opt/nextcloud-rag/install/backup-restore.sh create /srv/aki-backups
sudo /opt/nextcloud-rag/install/backup-restore.sh verify /srv/aki-backups/aki-rag-backup-...
sudo /opt/nextcloud-rag/install/backup-restore.sh restore /srv/aki-backups/aki-rag-backup-... --yes
```

The recovery set includes configuration/runtime state, AKI SQLite databases, `runtime/users.sqlite` with the matching credential master key, local CA/TLS/operator state below the installation prefix, and bundled Neo4j where selected. It deliberately excludes Nextcloud, Elasticsearch, rebuildable Qdrant, external Neo4j, OpenWebUI/Playwright state and model caches.

The Super-Light field test exercised a real `users.sqlite` loss/restore roundtrip. Without the store, provider-client authentication failed closed; the verified restore recovered the registered provider-client/user credential state and normal authenticated requests. Bundled Neo4j snapshot/restore was included in the same recovery workflow.

Legacy Docker Compose v1 compatibility was also fixed for the backup helper: it no longer uses `docker-compose run provider` on a host-networked provider service and instead uses an isolated `docker run --network none` helper.

## Retrieval and ACL behavior

RC5 adds an optional pre-rerank ACL metadata prefilter under:

```yaml
acl:
  prefilter:
    enabled: false
```

The first implementation uses Nextcloud FullTextSearch metadata for owner/direct-user/group membership. The authenticated Nextcloud UID and current groups are resolved server-side through the stored Nextcloud credential; frontend-supplied group headers are not trusted.

The prefilter is a recall/performance optimization only:

- final live WebDAV ACL remains the authorization boundary;
- indexed ACL metadata cannot grant access;
- failure of the OCS identity/group lookup skips the prefilter and falls back to the established retrieval path;
- Circles are intentionally not evaluated in RC5 v1;
- no adaptive backfill is performed after final ACL denials.

Broad/unspecific-query feedback is deferred until after live ACL, so a user with no authorized candidates receives a normal no-results outcome rather than an index-wide breadth signal.

RC5 also preserves exact duplicate variants by Nextcloud file ID when FullTextSearch exposes the same valid extracted-content hash. Live ACL checks each retained variant; an authorized duplicate may be promoted if the ranked representative is denied.

## Follow-up evidence continuity

Conversational follow-up resolution is now language-neutral. The planner decides whether recent answer context is required and emits a standalone retrieval query. A small bounded set of documents from the immediately preceding answer can be re-resolved through the current user's live ACL and current source-scope policy before joining the normal verifier pool.

This is short-lived evidence continuity, not persistent conversational entity memory.

## Graph-Lite and Findings

RC5 expands identity curation with non-destructive `SAME_AS` decisions:

- **Identisch** keeps both Entities and their source-specific ContactRecords while sharing identity/search forms;
- **Verschieden** persists `NOT_SAME_AS`;
- `MERGED_INTO` remains a separate stronger consolidation action.

Identity candidate presentation collapses transitive `SAME_AS` components and exposes CardDAV/user provenance for administrative review.

Finding lifecycle handling is tightened through lazy ACL cleanup: after a successful definitive live-ACL denial, AKI may remove only that canonical user's provenance for still-uncurated Findings and garbage-collect a shared Finding only when no ResearchRun references it. Curated/suppressed Findings, curated entity mappings and relation observations are preserved.

The direct `/use` path remains intentionally independent of Findings enrichment in RC5. Adding Candidate-Verifier-derived Findings for `/use` without affecting the direct answer path is deferred to 0.8.5.1.

## AKI Recherche and chat archive

The bundled Nextcloud app is updated to **AKI Recherche 0.2.6**.

Newly saved conversations are stored as readable Markdown under `AKI-Chats/` with hidden `.akirag.json` sidecars for machine metadata/provenance. The visible archive receives a stable Nextcloud `files:<id>`; the archive origin is registered immediately through the trusted provider/API path so `/chatarchive` scoping does not have to wait for path self-healing.

The app also records/displays effective source scopes and includes the current context-sensitivity notice.

Chat archives remain independent Nextcloud objects with their own file ID, ACL and lifecycle. Removing access to an original source document does not retroactively rewrite a saved chat containing derived text.

## Installer and upgrade behavior

Installer reruns preserve site-owned runtime/configuration state and require the existing stack to be stopped before refresh.

Important upgrade behavior:

- an existing `config.yaml` is preserved and is **not** schema-merged with newly introduced optional keys;
- administrators should compare the shipped reference/changelog after an update and add wanted options manually, for example `acl.prefilter.enabled`;
- valid existing runtime secrets and machine-role keys are preserved;
- fresh installs and reruns enter maintenance mode before normal service is reopened;
- the exact installer invocation is recorded in `install/last-install-command.sh` for reproducible reruns.

## Security and operational notes

RC5 retains the rc4.3 internal-service trust zones and separate machine credentials `RAG_INTERNAL_API_KEY` and `RAG_PROVIDER_INTERNAL_KEY`.

Dockerized/Super-Light still supplies several global service secrets through environment files. An account allowed to operate Docker/Compose can therefore render or inspect those values, for example through `docker-compose config` or container inspection. Treat Docker-daemon access as privileged/root-equivalent and do not paste full rendered Compose output into issues, chats or support logs.

Moving suitable service secrets to Docker secrets or file-mounted credentials is deferred hardening. It is not intended to protect secrets from a Docker administrator; the goal is to reduce accidental exposure during normal diagnostics.

The RC5 backup inventory can currently report a configured Super-Light path such as `/app/runtime/ca/nextcloud-ca-bundle.pem` as external even though `runtime/ca/` is explicitly included in the recovery set. This is a path-normalization warning, not loss of the bundled CA file; verify the expected file in `files.tar`.

## Known limits

The most relevant RC5 boundaries are:

- supported/tested mappings remain `standard+native` and `super-light+dockerized`;
- Circles are not part of the first ACL metadata prefilter;
- there is no unified cross-store purge/rollback transaction;
- master-key rotation remains a follow-up workflow;
- Dockerized global service-secret delivery still uses environment materialization;
- Playwright's Microsoft base image is version-tag pinned but not yet immutable-digest pinned;
- `/use` does not yet create Findings;
- chat archive creation is currently implemented by the bundled AKI Recherche app, while retrieval/source-scope handling remains provider/middleware-side.

See `docs/KNOWN-LIMITATIONS.md`, `docs/ROADMAP.md`, `docs/DATA-LIFECYCLE.md` and `SECURITY.md`.

## Validation

CI for the accepted RC5 branch covers Python compilation, shipped shell syntax and the full regression suite. The final documentation/maintenance-mode baseline passed **541 tests with one known warning**.

The rc4.3 blank-VM acceptance remains the base deployment proof for both supported mappings. RC5 incremental field acceptance additionally covered:

- two-user ACL prefilter and broad/unspecific-query behavior;
- Markdown chat archive continuation;
- maintenance-mode transitions;
- Super-Light backup creation and verification;
- real credential-store loss followed by successful restore;
- bundled Neo4j snapshot/restore participation;
- legacy Docker Compose 1.25.x backup-helper compatibility after the RC5 fix.

RC5 was therefore the private development-repository release-candidate baseline at that time. Public release/promotion remained a separate repository/release operation.
