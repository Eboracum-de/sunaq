# SunaQ 0.8.8-rc1

Release date: 2026-11-09

SunaQ 0.8.8-rc1 is a retrieval-quality, synchronization and upgrade-hardening
release candidate. It keeps the 0.8.7 SRC / ERG Internal / ERG Full architecture
and the existing chat-archive model, while bringing the query/rewrite contract
closer to SunaQ Find, reducing unnecessary Elasticsearch/Qdrant work and
hardening installer reruns on existing installations.

This remains a release candidate for technically experienced operators.

## Query rewriting and retrieval

0.8.8 consolidates the newer structured query contract and deterministic
Elasticsearch compilation work used during the 0.8.8 development cycle. The
middleware keeps the model responsible for intent description while application
code owns the final Elasticsearch request shape. This reduces accidental
renumbering/interpretation drift and keeps lexical query construction auditable.

The release also retains the existing one-round retrieval and live Nextcloud ACL
boundary. Retrieval systems may propose candidates; Nextcloud remains the final
authorization authority before private document evidence reaches verifier or
answer stages.

## Elasticsearch → Qdrant synchronization

The vector sync path has been reworked around a compact reconciliation
inventory:

- Elasticsearch is the desired-state source.
- A companion Qdrant inventory collection stores one compact metadata record per
  Elasticsearch document.
- Normal synchronization no longer uses `state.sqlite` as its reconciliation
  authority.
- Complete unchanged runs compare lightweight Elasticsearch `_id` plus
  `hash`/document-version metadata with the Qdrant inventory.
- Full extracted document text is hydrated through batched `_mget` only for
  true deltas.
- Empty and semantically excluded records remain represented in the inventory,
  so they do not need to be reloaded on every run.
- Missing Elasticsearch IDs remove stale vector chunks and inventory records.
- Inventory writes for empty/excluded records are batched.

A legacy `state.sqlite` may still be used once as an upgrade seed for existing
vectorized records; it is not the normal runtime source of truth afterwards.

On the acceptance installation used during rc1 preparation, an unchanged corpus
of about 15,000 Elasticsearch documents completed a full reconciliation in
roughly three seconds with `full_source_fetched=0`. This is an observed test
result, not a general performance guarantee.

Synchronization now emits a compact periodic progress heartbeat (default every
30 seconds) and an explicit stale-cleanup phase message. Per-document semantic
filter noise remains at DEBUG level.

The bundled Qdrant server image is pinned to the validated 1.19.2 image digest.

## Installer and upgrade hardening

Installer reruns continue to preserve administrator-owned configuration and now
also repair incomplete legacy research-backend declarations. For example, an
existing:

```yaml
research_backends:
  brave-search:
    enabled: true
```

is non-destructively completed with the required backend kind/adapter fields.
Explicit administrator choices are not overwritten.

Neo4j schema initialization retries are bounded per attempt and surface the last
error lines instead of appearing to hang silently for many minutes.

The existing legacy installation-prefix behavior is unchanged: recognized
installations under `/opt/nextcloud-rag` may continue in place rather than
being forcibly moved to `/opt/sunaq`.

## Operations and administration

The Vector Admin view reports last-run duration and reconciliation counters,
including scanned, full-source-fetched, new, updated, unchanged, deleted,
excluded and deferred documents.

The background synchronization interval remains conservative, while unchanged
full-corpus checks are now cheap enough that periodic reconciliation does not
require transferring the complete extracted corpus.

## Compatibility and deferred work

0.8.8-rc1 deliberately does **not** introduce the planned server-authoritative
chat-history redesign. The existing Nextcloud chat archive remains the active
implementation for this release candidate.

The planned 0.9 line will move canonical conversation history into a
server-side SunaQ chat store, allowing frontends to send a stable chat ID plus a
new turn while SunaQ reconstructs trusted context itself. Nextcloud chat files
can then become derived human-readable exports rather than the primary
conversation state.

The bundled Nextcloud app retains its independent application versioning and is shipped as SunaQ Recherche 0.3.21 in this tree. The 0.8.8-rc1 release focus is middleware/query/synchronization hardening rather than a new Nextcloud client architecture.
