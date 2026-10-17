# Resolver gateway prototype (legacy packaging)

> **Status:** historical prototype packaging. The resolver implementation in
> `erg_resolver/` is now installed as the regular Standard-ERG component via
> `install/components/erg-resolver/Dockerfile` and the `erg-resolver` service
> in `install/docker-compose.yml`. Do not use this directory as the canonical
> installation path for new deployments.

This directory records the earlier ERG-side experiment that introduced
reference resolution without changing the SRC request path.

## Scope

The prototype exposes a deliberately small contract:

- `GET /health`
- `GET /capabilities`
- `POST /resolve`

Adapters are optional and fail independently. The current prototype supports:

- **Neo4j** — read-only local Entity/EntityName/SearchAlias resolution with
  contact/finding provenance.
- **OpenRegister** — optional external German company resolution using the
  official Python SDK.
- **OpenPLZ** — optional external German locality/street normalization using the
  public OpenPLZ API.

The resolver does not write to Neo4j and is not part of the SRC request path.
If it is absent or unavailable, the existing SunaQ retrieval flow remains the
fallback.

External adapters are deliberately hint/shape gated. OpenRegister is queried
only for company-like references or an explicit organization/company hint.
OpenPLZ is queried only for address/location-like references. Only the
`reference.text` value is sent to an external adapter; `context` remains local
and is used only for candidate ranking.

## Build

From the root of an unpacked prototype tree:

```sh
docker build \
  -f install/resolver-prototype/Dockerfile \
  -t sunaq-resolver:prototype \
  .
```

## Start: Neo4j only

The bundled Compose file keeps the original local-only experiment:

```sh
docker compose -f install/resolver-prototype/docker-compose.yml up --build
```

The service binds to `127.0.0.1:8770` through host networking. Neo4j is
expected on localhost port 7687 by default. Its password is read from
`runtime/service-secrets/neo4j_auth`; both a raw password and Neo4j's
`neo4j/password` format are accepted.

For an already installed Standard/native SunaQ tree, a direct container run can
bind the installation's existing Neo4j secret instead of copying it into the
prototype checkout.

## Optional OpenRegister adapter

The prototype pins `openregister-sdk==3.9.0`. Store the API key in a file
outside the source tree, for example:

```sh
install -m 600 /dev/null ~/openregister-api-key
printf '%s' 'YOUR_API_KEY' > ~/openregister-api-key
```

Then start the prototype with an explicit one-file bind mount:

```sh
docker run --rm -d \
  --name sunaq-resolver-test \
  --network host \
  -e RESOLVER_NEO4J_ENABLED=true \
  -e RESOLVER_NEO4J_URI=bolt://127.0.0.1:7687 \
  -e RESOLVER_NEO4J_USERNAME=neo4j \
  -e RESOLVER_NEO4J_PASSWORD_FILE=/run/sunaq-secrets/neo4j_auth \
  -e RESOLVER_OPENREGISTER_ENABLED=true \
  -e RESOLVER_OPENREGISTER_API_KEY_FILE=/run/sunaq-secrets/openregister_api_key \
  -e RESOLVER_OPENPLZ_ENABLED=true \
  -v /opt/sunaq/runtime/service-secrets/neo4j_auth:/run/sunaq-secrets/neo4j_auth:ro \
  -v "$HOME/openregister-api-key":/run/sunaq-secrets/openregister_api_key:ro \
  sunaq-resolver:prototype
```

OpenRegister can alternatively read `OPENREGISTER_API_KEY` from the environment,
but the file-backed form is preferred for this prototype.

OpenRegister candidate search keeps `reference.context` local. The adapter
processes up to 100 provider-returned candidates by default, applies local
context ranking, and returns only the caller-requested bounded result set
(`limit_per_reference` remains capped at 20). The internal processing window
can be changed with `RESOLVER_OPENREGISTER_CANDIDATE_FETCH_LIMIT`; values are
bounded to 20..500. This setting cannot make the upstream autocomplete endpoint
return more rows than the provider/account itself supplies.

OpenPLZ needs no API key.


## Examples

Local graph resolution:

```sh
curl -s http://127.0.0.1:8770/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "references": [
      {"text": "John Doe", "context": ["Berlin", "ACME"]},
      {"text": "J. Doe", "context": []}
    ]
  }' | jq
```

Company and location resolution:

```sh
curl -s http://127.0.0.1:8770/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "references": [
      {
        "text": "Example Machinery AG",
        "context": ["Example City"],
        "hints": {"kind": "organization"}
      },
      {
        "text": "53123 Bonn",
        "context": [],
        "hints": {"kind": "location"}
      },
      {
        "text": "10117 Berlin Pariser Platz",
        "context": [],
        "hints": {"kind": "address"}
      }
    ]
  }' | jq
```

A result contains advisory candidates only. Resolution does not grant document
access, bypass Nextcloud ACLs, or constitute answer evidence.

## Prototype boundary

Do not add provider-specific logic to SRC. The intended boundary is:

```text
SunaQ normal retrieval
        |
        +-- optional ERG resolver enrichment
                |
                +-- Neo4j
                +-- OpenRegister
                +-- OpenPLZ
                +-- future CRM/case/register adapters

resolver absent/error -> normal SunaQ retrieval unchanged
```

The next experiment is to verify that heterogeneous local/company/location
references can be normalized usefully behind this one contract before any
integration into the query-rewrite path.


## Start with OpenRegister

Keep the default resolver compose file Neo4j-only. To enable OpenRegister,
store the credential in the administrator-owned `runtime.env`, not directly
under `runtime/service-secrets/`. That directory is generated and atomically
replaced by `sync-container-secrets.sh`.

Add the secret to `/opt/sunaq/runtime.env`:

```sh
OPENREGISTER_API_KEY=YOUR_API_KEY
```

Then materialize the file-backed container secret:

```sh
sudo /opt/sunaq/install/sync-container-secrets.sh /opt/sunaq
test -f /opt/sunaq/runtime/service-secrets/OPENREGISTER_API_KEY
```

Do not edit the generated file under `runtime/service-secrets/` directly.

Then start the resolver with the shipped opt-in override:

```sh
cd /opt/sunaq/install/resolver-prototype
docker compose \
  -f docker-compose.yml \
  -f docker-compose.openregister.yml \
  up -d --build
```

This mounts only the OpenRegister key file in addition to the Neo4j secret.
The `sunaq-register` model remains unavailable unless the administrator also
sets `resolver.enabled: true` in `config.yaml` and restarts the provider.
