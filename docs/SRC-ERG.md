# SRC and ERG architecture

**Status:** enforced architecture tiers in `0.8.6-rc1.2`.

SunaQ is the product. **Secure RAG Core (SRC)** and **Eboracum Research Gate
(ERG)** are capability/trust envelopes, not separate products and not deployment
mechanisms.

## Secure RAG Core (SRC)

SRC is the deliberately narrow baseline:

- ordinary Nextcloud documents are the only evidence source;
- FullTextSearch / Elasticsearch `files` is the only document retrieval arm;
- live Nextcloud ACL is mandatory and remains the final authorization boundary;
- model roles may be local or remote; remote model egress is an explicit
  administrator decision and remains subject to the configured hard evidence caps;
- retrieval is limited to one round;
- Qdrant/vector retrieval, document-graph retrieval, Mail, Web/Web archive,
  Chat archive, Research Findings and document graphization are disabled;
- an administrator-owned Neo4j seed/alias store may still exist for identity
  normalization/query expansion, but document graph evidence and graph-derived
  persistence are outside SRC.

These rules are runtime invariants. Setting `architecture.tier: src` while
enabling an incompatible capability fails validation rather than silently
creating an SRC-like hybrid. Omitted retrieval arms are normalized to `files`
inside SRC, deterministic filename/document-ID paths remain Documents-only, and
ERG-only middleware surfaces are unavailable.

Remote LLM routing is not itself an ERG capability. SRC prefers a private/local
processing path, but an administrator may deliberately configure remote
planner/verifier/evidence/answer roles and thereby accept model data egress.
Remote-role document/count/character caps continue to apply.

## Eboracum Research Gate (ERG)

ERG is the extension envelope. It may add, individually and deliberately:

- broader external egress surfaces such as Web search, external embeddings or
  additional externally hosted processing;
- live Web research and Web archiving;
- Mail and Chat archive evidence;
- Qdrant/vector retrieval and external embeddings;
- Research Findings / Graph-Lite or fuller graph processing;
- additional retrieval rounds;
- external user interfaces and other trust boundaries.

ERG is a menu, not a requirement to enable all features.

## Packaging and presets

Deployment profile and architecture tier remain separate axes.

- **Super-Light** is packaged as the formal SRC baseline in rc1.2.
- The full reference `config.yaml` remains ERG for upgrade compatibility with
  existing installations that already used optional capabilities.
- Installations predating `architecture.tier` default to ERG rather than being
  retroactively labelled SRC.

Two safe YAML overlays are shipped:

- `install/presets/core.yaml` — enforced SRC;
- `install/presets/workgroup.yaml` — conservative Elasticsearch-centric ERG
  baseline with optional working-group capabilities, while Qdrant and full
  document graph processing remain off.

Installers accept `--preset core|workgroup` and `--preset-file FILE`. Preset
files are parsed as YAML data only; they are never sourced or evaluated as shell
code. Explicit installer URL/TLS/index arguments remain authoritative over the
preset. The same merge path is also available directly through
`python -m rag.config_preset`.

For a fresh Dockerized Super-Light/SRC installation, the recommended operational
form is therefore explicit:

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

Remove `--plan` for the actual installation. Super-Light would select the same
SRC baseline without an explicit preset, but recording all three switches makes
the deployment mechanism and capability boundary reproducible and reviewable.


### ERG-to-SRC transition during the RC line

rc1.2 does not migrate historical ERG graph contents into SRC. Before converting
an existing RC installation that has used document-derived Graph/Findings data,
stop SunaQ and reset Neo4j explicitly:

```bash
python -m rag.graph --config /opt/sunaq/config.yaml reset --yes-really-delete-all
```

For a Dockerized Super-Light installation run the same command inside the
provider/API image with the normal mounted config and secret files. After the
reset, re-import only the wanted CardDAV/administrator seed sources. This is a
deliberately destructive RC transition; no legacy graph-provenance migration is
provided.

## Security boundaries added in rc1.2

rc1.2 also hardens boundaries that apply independently of architecture naming:

- retrieved evidence is serialized into server-generated JSON records;
- an immutable Python-side untrusted-evidence guard is appended to all model
  roles that consume retrieved evidence;
- public Web retrieval blocks local/private/link-local/reserved destinations
  and revalidates redirect targets; the Playwright renderer applies the same
  public-network principle to browser requests;
- service credentials can be supplied through file-backed secret material
  rather than ordinary container environment variables.

The optional policy-hook framework remains a separate extension point. Core SSRF
protection and SRC invariants do not depend on an installed policy evaluator.

## Deferred to rc2

The following are deliberately **not** part of rc1.2:

- improved/evaluated multi-round retrieval;
- authoritative server-side conversation history;
- broader administration/maintenance UX;
- experimental additional retrieval sources.

See [Roadmap](ROADMAP.md).
