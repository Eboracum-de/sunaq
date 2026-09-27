# Security

## Security model

SunaQ treats Elasticsearch, Qdrant and Neo4j as candidate-retrieval systems. They are not authorization authorities. Before document evidence is exposed to a verifier or answer model, access is checked live against Nextcloud for the authenticated user.

The graph may contain **shared retrieval knowledge** such as curated names/aliases and shared Finding decisions. Reusing that work across users is intentional and does not grant access to the supporting source document. Source passages and document evidence remain authorization-sensitive. See `docs/THREAT-MODEL.md` for the detailed boundary.

Reversible per-user Nextcloud and IMAP credentials, together with pending Nextcloud Login Flow poll tokens, are encrypted at rest in `runtime/users.sqlite` using AES-256-GCM. The credential master key is stored separately and production mode is fail-closed when required encryption cannot be satisfied.

Global service secrets are provisioned from root-protected runtime state and rc1.2 delivers routine middleware/container credentials through file-backed secret material where practical. Keep `runtime.env`, generated service-secret files, live `provider.env`, `runtime/`, TLS private keys and backups out of source control.

File-backed delivery removes routine SunaQ service credentials from the API/provider/mail container environment; it does not make Docker access unprivileged. The optional bundled OpenWebUI currently receives its scoped provider-client credential through the upstream environment interface, so that UI credential remains visible in its Compose/container configuration when OpenWebUI is enabled. A Docker-daemon/root-equivalent operator can also inspect mounted secret files and container state. Do not publish `runtime/`, local Compose `.env` files, rendered diagnostics or backup material that may contain credentials.

The FastAPI middleware on port 8765 is an **internal service boundary**. RC4.3 uses two installer-managed machine credentials: `RAG_INTERNAL_API_KEY` proves membership in the internal service plane, while `RAG_PROVIDER_INTERNAL_KEY` proves the narrower trusted-provider role. The provider supplies both on provider-originated middleware calls. Bundled nginx receives only the internal key and therefore cannot impersonate the provider merely by forwarding a request. These machine credentials do not replace provider Bearer authentication, SunaQ Admin authentication, Nextcloud live ACL or curation-session authentication.

Direct exposure of port 8765 is unsupported. Native startup refuses a non-loopback `RAG_API_HOST` unless `RAG_ALLOW_REMOTE_INTERNAL_API=true` is deliberately set. An alternative reverse proxy that bypasses bundled nginx must both authenticate its clients and supply the internal machine credential to protected middleware routes.

## Deployment expectations

- Use HTTPS for Nextcloud and verify TLS certificates.
- Keep SunaQ Admin behind authentication and an administrator-controlled reverse proxy/network boundary.
- Keep port 8765 loopback-only. Do not rely on `X-RAG-User-ID` as caller authentication. Internal service calls require `RAG_INTERNAL_API_KEY`; provider/user routes additionally require the separate `RAG_PROVIDER_INTERNAL_KEY`.
- If nginx Basic Auth is intentionally disabled, provide equivalent upstream authentication before any proxy is allowed to inject the internal machine credential.
- Security zones are enforced centrally as `PUBLIC`, `TRUSTED_PROVIDER`, `INTERNAL`, `ADMIN` and `USER`; core FastAPI routes publish the assigned zone in OpenAPI as `x-aki-security-zone`.
- Protect `runtime.env`, `runtime/users.sqlite` and the credential master key with restrictive ownership and permissions.
- Back up the credential master key separately from, but together with, the encrypted credential database.
- Do not manipulate credential rows directly with ad-hoc SQL. Use the Admin UI or supplied CLI commands.
- Treat Elasticsearch/Qdrant/Neo4j as sensitive infrastructure even though they do not authorize access.
- Treat `acl.enabled=false` / `--acl-off` as a diagnostic state only; verify `live_acl.enabled=true` in health before opening a shared corpus.
- Decide deliberately whether optional `/chatarchive` retention is compatible with the deployment's revocation/retention policy. A saved chat is a new Nextcloud object with its own lifecycle.

## Reporting a vulnerability

Do **not** publish credentials, private document contents, exploit details or sensitive deployment information in a public issue.

Use GitHub private vulnerability reporting when available. Security questions or reports that should not be public can also be sent to `rag@eboracum.de`; do not include secrets in public issues.

## LLM trust boundary

The complete private corpus is expected to remain in the local/private retrieval plane. Elasticsearch/Nextcloud access, embeddings, Qdrant and live ACL checks do not need to be exposed to an external LLM provider.

Planner, verifier, evidence-control and answer roles can be configured independently. Formal SRC permits local/private model roles and explicitly administrator-configured remote model roles; remote evidence remains subject to document/count/character caps. Those limits reduce exposure but do not make transmitted evidence non-sensitive. The reranker/TEI path remains local/private in SRC because reranking occurs before the final live Nextcloud ACL decision.

Graph entity/relation extraction is a separate trust decision because it may process larger document portions. Automatic graph-worker startup and automatic enqueue of cited documents are disabled by default in the reference configuration.

Web archive writes have independent TLS verification settings. Disabling TLS verification is a diagnostic exception and should not be a production default.

Incoming documents, mail, public web pages and saved chats are untrusted content even when successfully retrieved. rc1.2 serializes retrieved evidence as server-generated JSON records and appends an immutable Python-side evidence guard to evidence-consuming model roles. This reduces delimiter spoofing and removable-prompt risk but is not a guarantee that an LLM cannot be influenced by adversarial content. Do not treat model extraction as a trust signal.

Deletion and backup are separate from authorization. The current release does not provide a single cross-store purge/restore transaction; see `docs/DATA-LIFECYCLE.md`.
