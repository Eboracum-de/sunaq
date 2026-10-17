# SRC / ERG Internal / ERG Full architecture

**Status:** target architecture for the `0.8.7-rc1` development line. The
three tiers are enforced capability/trust envelopes, not separate products and
not deployment mechanisms.

SunaQ uses three primary architectural dimensions:

1. **SunaQ-owned persistent derived state / data lifecycle**
2. **External information ingress**
3. **Research/data egress to external information services**

The locality of model-based processing is orthogonal. Planner, verifier,
answerer, reranker, embeddings and graph extraction may run locally or through
an explicitly configured external provider without changing the architecture
tier.

## 1. Tier definitions

### SRC — Secure RAG Core

SRC performs runtime analysis of the authoritative internal Nextcloud
information space without creating a persistent SunaQ-derived knowledge store.

The baseline is:

- ordinary Nextcloud documents through FullTextSearch / Elasticsearch;
- mandatory live Nextcloud ACL before private document evidence is used;
- one bounded retrieval round;
- no Qdrant/vector corpus;
- no persistent document graph or Findings;
- no CardDAV-derived graph seeds;
- no Chat archive / Research dossier;
- no Mail import;
- no public Web research;
- no external resolver or Delegated Research.

Elasticsearch is persistent derived state, but it is **not lifecycle-owned by
SunaQ**. The Nextcloud deployment remains responsible for the FullTextSearch
index lifecycle.

### ERG Internal

ERG Internal is SRC plus **SunaQ-owned persistent derived information generated
exclusively from the authoritative internal information space**.

Typical optional capabilities include:

- embeddings and Qdrant/vector retrieval;
- Neo4j-derived entity, graph and relationship state;
- CardDAV contact data used as graph/resolver seeds;
- Graph-Lite / document observations;
- Research Findings and manual curation;
- local resolver adapters operating only on internal state;
- Chat / Research dossier persistence.

These capabilities create lifecycle obligations even though no external
information source is added.

ERG Internal does **not** permit Mail import, public Web acquisition, Delegated
Research or external resolver adapters.

### ERG Full

ERG Full is ERG Internal plus **external information acquisition** and the
corresponding research egress required to reach external information services.

Typical optional capabilities include:

- Mail import from external mail systems;
- public Web search/fetch and optional archival;
- external resolver capability;
- Delegated Research;
- persistence of external research artifacts.

External resolver adapters remain experimental in the 0.8.7 development line
and are not part of the public feature contract.

## 2. Component / capability matrix

Legend: **yes** = permitted/baseline capability, **optional** = permitted when
configured, **no** = outside the tier.

| Capability | SRC | ERG Internal | ERG Full | Architectural role |
|---|---:|---:|---:|---|
| Provider / API / orchestration | yes | yes | yes | runtime core |
| Elasticsearch / NC FullTextSearch | yes | yes | yes | Nextcloud-owned retrieval source |
| Live Nextcloud ACL | yes | yes | yes | mandatory authorization boundary |
| Query rewrite / planner / verifier / answer | yes | yes | yes | processing plane |
| Reranker | optional | optional | optional | processing plane |
| Embedding model | no corpus use | optional | optional | processing for derived vector state |
| Qdrant / vector retrieval | no | optional | optional | SunaQ-owned derived state |
| CardDAV-derived seeds | no | optional | optional | SunaQ-owned derived state from internal source |
| Neo4j / graph observations | no | optional | optional | SunaQ-owned derived state |
| Research Findings / curation | no | optional | optional | derived + partly non-rebuildable state |
| Local resolver | no | optional | optional | internal resolution only |
| Chat / Research dossier (0.8.7) | no | optional | optional | persistent application/research state |
| Mail import / worker | no | no | optional | external information ingress |
| Public Web search/fetch | no | no | optional | research egress + external ingress |
| Delegated Research | no | no | optional | external research boundary |
| External resolver | no | no | optional | research egress + external ingress |
| Ingress policy hooks / untrusted evidence boundary | no external ingress | internal hooks only | required boundary | external-content control |
| Research egress policy | no research egress | no research egress | required boundary | outbound connector control |
| Central Derived Reconciler | no | target | target | lifecycle target, not shipped in 0.8.7 |

Concrete databases are implementations, not tier definitions. Replacing Qdrant
or Neo4j in the future does not change the meaning of ERG Internal.

## 3. Processing locality is orthogonal

A tier describes **what information space SunaQ uses and what persistent state
it owns**, not where a model executes.

| Processing role | Local | External | Changes tier? |
|---|---:|---:|---:|
| Query rewrite | yes | yes | no |
| Planner | yes | yes | no |
| Reranker | yes | yes | no |
| Verifier / evidence control | yes | yes | no |
| Answerer | yes | yes | no |
| Embeddings | yes | yes | no |
| Graph entity/relation extraction | yes | yes | no |
| Document summarization | yes | yes | no |

External processing is an explicit administrator decision. SunaQ can enforce
whether an external endpoint is permitted and can audit that boundary, but it
does not promise that semantic redaction can preserve equivalent model quality.
If a processing role must not receive the required private context outside the
trust zone, the appropriate deployment choice is a local/internal provider for
that role.

## 4. Lifecycle ownership

| Data/state | Authority / owner | Lifecycle responsibility |
|---|---|---|
| Nextcloud files | Nextcloud | Nextcloud |
| FullTextSearch / Elasticsearch | Nextcloud deployment | Nextcloud / FTS deployment |
| Qdrant vectors/chunks | SunaQ | SunaQ |
| machine-derived graph observations | SunaQ | SunaQ |
| CardDAV-derived graph state | SunaQ derived from Nextcloud source | SunaQ |
| Findings | SunaQ | SunaQ, preserving curator decisions |
| Mail/Web research copies | SunaQ/Nextcloud archive | SunaQ archive policy |
| Conversation state | transitional in 0.8.7 | SunaQ target in 0.9 |

0.8.7 deliberately does **not** introduce a central Derived Reconciler.
Existing component-specific mechanisms remain authoritative for this release:
Qdrant stale/shrinking-document cleanup, CardDAV reconciliation, graph
rebuild/relink administration and conservative Findings cleanup.

The target architecture for a later 0.8.x or 0.9 release is a central,
source-driven **Derived Reconciler** that coordinates source changes/deletions
across SunaQ-owned derived stores. This is a target responsibility, not a 0.8.7
release contract.

## 5. ACL invariant

For private document retrieval, Nextcloud remains the authorization authority.
Elasticsearch, Qdrant and Neo4j may produce candidates, but a current live
Nextcloud ACL decision is required before source document content becomes answer
evidence.

The 0.8.7 Chat/Research dossier remains a transitional persistence/debug model.
A materialized answer is not retroactively rewritten when a source ACL later
changes. The 0.9 target is server-authoritative Conversation State, with
Nextcloud retaining at most a human-readable projection/export rather than the
authoritative internal run/decision state.

## 6. Resolver boundary

Resolution is separate from retrieval.

- SRC: resolver capability disabled.
- ERG Internal: local/internal resolver adapters may use SunaQ-owned or
  Nextcloud-derived state.
- ERG Full: external resolver capability may additionally be enabled.

Concrete external resolver adapters are implementation experiments in 0.8.7;
the stable architectural contract is the **external resolver capability**, not
a named third-party data service.

## 7. Architecture tier, capability presets and deployment

SunaQ exposes three independent installer concepts:

```text
--architecture-tier src|erg-internal|erg-full
    normative trust, lifecycle and external-ingress boundary

--preset core|workgroup|full
    shortcut for a recommended architecture tier + capability combination

--deployment standard|dockerized
    packaging/runtime mechanism
```

The architecture tier is authoritative. A tier selected without a preset is a
bare envelope: optional components remain off unless they are named explicitly
with `--with-*`. Presets are atomic convenience bundles. If an administrator
wants only part of a preset, the supported model is to omit the preset and list
the wanted positive component switches explicitly. Negative component switches
are reserved for rerun/removal operations, not fresh-install composition.

The current named presets are:

```text
core       -> src          + no SunaQ-owned persistent derived state
workgroup  -> erg-internal + Qdrant + Neo4j + internal resolver + Findings + chat archive
full       -> erg-full     + workgroup capabilities + Mail/external research capabilities
```

For SRC, the current Core preset intentionally remains minimal, so its data
capabilities are currently close to a bare `--architecture-tier src` install.
The concepts nevertheless remain separate: a future Core shortcut may select
safe operational components without changing the SRC boundary. For ERG
Internal and ERG Full, a tier-only install likewise selects only the permissible
envelope and does not imply Workgroup or Full.

A named preset may be combined with an explicit matching architecture tier, for
example `--preset workgroup --architecture-tier erg-internal`. A conflicting
combination is rejected rather than resolved by precedence. A custom preset
with an explicit `architecture.tier` is subject to the same rule.

Historical `--profile super-light` remains a compatibility alias for
`--deployment dockerized --preset core`. Existing
`architecture.tier: erg` values are normalized to `erg_full` for upgrade
safety.

Deployment does not change the architecture boundary. Both Standard and
Dockerized deployments may provision the components permitted by the selected
tier. Whether a backend is local or externally managed remains a component
configuration decision, not a deployment-tier decision.

## 8. ERG to SRC transition during the RC line

Changing an existing ERG Internal/Full installation to SRC changes the lifecycle
boundary; it does not migrate historical SunaQ-owned derived data into SRC.
Before such a downgrade, stop SunaQ and remove or archive derived state according
to site policy.

For an RC installation where the Neo4j graph is disposable, the existing
explicit destructive helper remains available:

```bash
python -m rag.graph --config /opt/sunaq/config.yaml reset --yes-really-delete-all
```

This command is intentionally destructive. Do not use it where manually curated
Graph/Findings state must be retained without first exporting or otherwise
preserving that state. Do not re-import CardDAV-derived graph seeds into SRC;
if that persistent derived resolver state is required, the correct tier is ERG
Internal.

## 9. Security consequence

The three tiers can be summarized as:

| Tier | SunaQ-owned derived-state lifecycle | External information ingress | Research/data egress |
|---|---:|---:|---:|
| SRC | no | no | no |
| ERG Internal | yes | no | no |
| ERG Full | yes | yes | yes |

External model/embedding/reranker processing is deliberately excluded from this
table because it is an orthogonal provider-locality decision.
