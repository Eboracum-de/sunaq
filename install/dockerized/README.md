# Dockerized deployment

**Reference:** `0.8.7-rc1`

Dockerized is a packaging/deployment mechanism of the common SunaQ middleware.
It is not a separate architecture tier and is not a reduced fork. Select the
information/data boundary independently with
`--architecture-tier src|erg-internal|erg-full`, or use the shipped
`core|workgroup|full` presets as recommended capability bundles.

The historical `--profile super-light` switch remains only as a compatibility
alias for `--deployment dockerized --preset core`.

## Intended use

Dockerized is aimed at legacy-friendly or dedicated Linux hosts where SunaQ
should run without a host Python runtime while reusing an existing Nextcloud
FullTextSearch/Elasticsearch estate. Nextcloud and Elasticsearch remain
administrator-managed external dependencies.

The Compose files retain the legacy-compatible format required by older tested
Docker Compose installations. OpenWebUI, nginx and Playwright are optional.

## Architecture and capability presets

| Preset | Tier | Baseline capability intent |
| --- | --- | --- |
| `core` | SRC | Documents / Elasticsearch + mandatory live Nextcloud ACL; no SunaQ-owned persistent derived knowledge store |
| `workgroup` | ERG Internal | SRC plus permitted internal derived state such as local Qdrant, Neo4j/resolver and Findings; no external information acquisition |
| `full` | ERG Full | ERG Internal plus explicitly configured external research/resolution capabilities |

Deployment packaging remains orthogonal. Dockerized can provision the same
permitted local Qdrant, Neo4j, resolver and worker capabilities as Standard
where implemented. The selected architecture tier remains the authority for
which capabilities may actually be enabled.

Fresh installs seed Sync and Mail worker runtime gates as paused. Starting the
worker container does not authorize it to perform work until the administrator
releases the corresponding runtime gate.

## Installation

Inspect the plan first:

```bash
sudo ./install/install.sh \
  --deployment dockerized \
  --architecture-tier src \
  --preset core \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --with-proxy \
  --plan
```

Install:

```bash
sudo ./install/install.sh \
  --deployment dockerized \
  --architecture-tier src \
  --preset core \
  --nextcloud-url https://cloud.example.org/nextcloud \
  --elasticsearch-url http://10.0.0.20:9200 \
  --elasticsearch-index my_index \
  --with-proxy \
  -y
```

For ERG Internal or ERG Full, select the matching tier/preset or build a custom
combination with the positive `--with-*` component switches. Preset and
deployment are intentionally separate axes.

Playwright is not installed by default. Add `--with-playwright` only when the
deployment needs rendered public-Web snapshots.

## TLS and private CAs

Normal certificate and hostname verification remains enabled. Additional Python /
OpenSSL `VERIFY_X509_STRICT` checking is off by default for compatibility with
established private PKIs; enable it explicitly with `--x509-strict` when the
site PKI satisfies those stricter checks.

Private trust anchors can be supplied repeatedly:

```bash
sudo ./install/install.sh \
  --deployment dockerized \
  --preset core \
  --ca-certificate /path/Company_Root_CA.crt \
  ...
```

For intentionally HTTP-only lab Nextcloud instances, use
`--allow-insecure-nextcloud`. Do not use that exception for normal deployments.

## Runtime and maintenance

The common operator entrypoints are installed at the deployment root:

```bash
/opt/sunaq/start-all.sh
/opt/sunaq/stop-all.sh
/opt/sunaq/status.sh
/opt/sunaq/install/maintenance-mode.sh on|off|status
/opt/sunaq/install/smoke-test.sh /opt/sunaq
```

Fresh installs and installer reruns enter maintenance mode. Leaving maintenance
converges the configured Dockerized service set and waits for required selected
dependencies before exposing the normal provider.

A fresh ERG installation can take noticeably longer on the first Neo4j database
initialization. An immediate smoke/status run may therefore report Neo4j
unavailable while the container is still starting; an unchanged repeat run must
be green before acceptance.

## Models and retrieval

The user-visible Standard / Thorough / Deep packages (current German labels: Schnell / Gründlich / Tief) are shared with Standard.
Their model/profile configuration lives below `models/`, which is mounted
read-only into the runtime containers and is treated as administrator-owned
configuration after installation.

The shipped 0.8.7-rc1 profiles use one retrieval round and no reranker by
default. Vector retrieval is available only when Qdrant is both permitted by the
selected architecture/capability policy and actually configured/available.

Live Nextcloud authorization remains mandatory for private document evidence in
all supported shared-corpus deployments.

## Contacts / CardDAV

When the resolver/graph capability is enabled, contact administration can use
the helper wrappers under the Dockerized install directory:

```bash
cd /opt/sunaq/install/dockerized
./contacts.sh list
./contacts.sh status --user alice
./contacts.sh books --user alice
./contacts.sh sync --user alice
```

The current user's stored Nextcloud Login-Flow credential is reused. Contact
identity/alias state does not itself grant access to supporting documents.

## Backup and restore

Use the common recovery utility while maintenance mode is enabled:

```bash
sudo /opt/sunaq/install/maintenance-mode.sh on
sudo /opt/sunaq/install/backup-restore.sh create /srv/sunaq-backups
```

The SunaQ recovery set includes administrator-owned configuration/model state,
credentials plus matching master key, current Dockerized site state and bundled
Neo4j when selected. Nextcloud, Elasticsearch, external Neo4j, OpenWebUI /
Playwright state and Qdrant vectors remain outside that recovery set.

Because Qdrant is rebuildable derived state, its incremental sync database is
also excluded/reset on restore. After disaster recovery, rebuild Qdrant from
Elasticsearch before leaving maintenance mode.

See `docs/DATA-LIFECYCLE.md` and `docs/BETA-OPERATIONS.md` for the recovery
contract and acceptance order.

## Legacy compatibility

Existing installations below `/opt/nextcloud-rag` are supported in place.
Legacy `.aki-rag-installation`, `install/super-light` state and
`--profile super-light` are recognized only for migration/compatibility.
Fresh installations use `/opt/sunaq`, `.sunaq-installation` and
`install/dockerized`.
