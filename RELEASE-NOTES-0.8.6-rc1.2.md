# SunaQ 0.8.6-rc1.2

0.8.6-rc1.2 is a hardening and architecture-boundary release. It deliberately
does not tune multi-round retrieval or add broader administration features.

## Main changes

- Retrieved document/Web evidence is represented as structured JSON records and
  every evidence-consuming model role receives an immutable server-side
  untrusted-evidence guard.
- Live Web retrieval resolves and validates destinations before connecting and binds each connection to the validated public IP, closing the DNS-rebinding validation/connect gap. The optional Playwright renderer no longer performs independent live navigation: it renders only the already fetched HTML snapshot with browser networking/WebSockets blocked.
- Native and Dockerized SunaQ services use file-backed credentials for routine service secrets. API/provider/mail/model/Graph credentials are removed from the normal Super-Light container environment. The optional bundled OpenWebUI remains an explicit exception because its scoped provider-client key is supplied through the upstream environment interface.
- `architecture.tier: src|erg` is enforced at startup and request boundaries.
  SRC means Documents-only Elasticsearch/files retrieval, mandatory live
  Nextcloud ACL and at most one retrieval round. LLM roles may be local/private or
  explicitly administrator-configured remote endpoints; remote evidence caps
  continue to apply. Reranker/TEI processing remains local/private in SRC.
- Super-Light is the packaged SRC baseline. The full reference configuration
  remains ERG for upgrade compatibility.
- `core` and `workgroup` capability presets are shipped as safe YAML data and
  can be selected with `--preset` or `--preset-file`.

## Compatibility

Existing installations without `architecture.tier` default to ERG. This avoids
silently disabling capabilities on upgrade. Administrators who want the strict
SRC contract should select the core preset or explicitly configure
`architecture.tier: src` and satisfy all SRC invariants.

The legacy Standard-installer `--core` flag remains a resource shorthand for
Qdrant + Neo4j and is distinct from `--preset core`.

ERG-to-SRC conversion during the RC line does not migrate historical graph data. Reset Neo4j explicitly and rebuild only trusted CardDAV/administrator seeds before using an existing ERG installation as SRC.

## Deferred

The following remain rc2 work:

- systematic multi-round retrieval evaluation/tuning;
- authoritative server-side conversation history;
- Admin/maintenance convenience features.

Before public tagging, the release still requires final CI/review and the normal
installation/acceptance checks.
