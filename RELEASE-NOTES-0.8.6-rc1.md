# Release notes – 0.8.6-rc1

**Status:** release candidate  
**Date:** 24 September 2026  
**Public predecessor:** `0.8.5-rc5.1`

0.8.6-rc1 introduces SunaQ as the user-facing research layer and, more
importantly, separates the SunaQ research profile from the underlying LLM.
The release keeps the established live-Nextcloud-ACL security boundary while
making research depth selectable per user.

## Highlights

### SunaQ model profiles

Three shipped profiles are exposed through the authenticated OpenAI-compatible
`/v1/models` endpoint:

| Display name | Model ID | Verification window | Answer context |
| --- | --- | ---: | ---: |
| Standard (Schnell) | `sunaq-standard` | 10 | 10 documents / 25k chars |
| Thorough (Gründlich) | `sunaq-thorough` | 30 | 30 documents / 100k chars |
| Deep (Tief) | `sunaq-deep` | 50 | 50 documents / 200k chars |

The current rc1 experiment deliberately changes **budget only**. All three
profiles use one retrieval round, Evidence Review is off and planner thinking is
off. Additional retrieval rounds and model reasoning are separate future
experiments.

Standard (Schnell) is the default entitlement. Thorough (Gründlich) and Deep (Tief) are enabled per user in
SunaQ Admin. The provider exposes only models currently allowed for the
authenticated user.

### Request-local model/runtime configuration

Profile packages under `models/` now own request-local settings for search,
retrieval planning, evidence control, retrieval signals, entity resolution,
graph retrieval, reranking, context enrichment and answer-context budgets.

Profiles can route planner/verifier/evidence/answer roles independently to
configured Ollama or OpenAI-compatible backends without mutating process-global
environment state.

### Retrieval completeness signalling

Candidate verification now distinguishes:

- a **hard limit**: known ranked candidates were not checked;
- a **near-capacity warning**: all currently ranked candidates were checked but
  the profile window is almost exhausted, so additional relevant documents
  outside the window cannot be excluded.

Verifier progress can expose the current batch, for example
`Prüfe Dokumente 11–15 von 48 …`.

### Follow-up actions

The provider can return deterministic structured follow-up actions. The SunaQ
Nextcloud client renders them as compact action buttons.

Examples:

- `Retry with Deep` (`Mit Tief erneut suchen`) only when a strictly stronger profile is currently
  allowed for the user;
- `Anfrage präzisieren` for broad/unspecific or capacity-limited searches;
- evidence-control clarification options where that mode is used.

Suggestions are live UI actions and are not persisted in archived chats.

### SunaQ Nextcloud client 0.3.0

The app moved to `clients/nextcloud/sunaq` with app id `sunaq` and visible
name **SunaQ Recherche**.

The UI now provides:

- compact chat-style composer;
- model selector inside the composer;
- source chips instead of a checkbox fieldset;
- collapsible research-history sidebar;
- request progress polling;
- provider follow-up actions;
- Markdown chat archives under `SunaQ-Chats/`.

Legacy `AKI-Chats`, `.akirag.json` and old Nextcloud app configuration remain
readable as compatibility input.

### Source defaults

The provider's client-neutral implicit source pool is now **ordinary documents
only**. Archive sources are explicit opt-ins:

- `/documents`
- `/mailarchive`
- `/webarchive`
- `/chatarchive`
- `/web`

The SunaQ Nextcloud client intentionally starts with Documents + Mail selected
and sends those scopes explicitly. A generic OpenAI-compatible client such as
OpenWebUI that sends no source directive searches ordinary documents only.

### Fresh-install and Super-Light behaviour

Fresh 0.8.6 installations default to `/opt/sunaq`. Recognized legacy
installations remain at their existing prefix (for example
`/opt/nextcloud-rag`) and are not moved automatically.

Super-Light remains Elasticsearch-centric: Qdrant and the local reranker are
disabled, while Neo4j is retained for seed/alias/Graph-Lite support.

Fresh installs enter maintenance mode first. The maintenance provider (plus
optional nginx/OpenWebUI) starts immediately; Neo4j, Playwright, API and
mail-worker are started when maintenance mode is disabled. The smoke test now
recognizes those services as intentionally deferred while maintenance mode is
active.

### Optional OpenWebUI

The bundled optional OpenWebUI uses the pinned `v0.11.4-slim` image and has
Arena models disabled. SunaQ remains a normal OpenAI-compatible provider; no
OpenWebUI-specific retrieval path is required.

New SunaQ responses no longer emit legacy `<!--rag-source:...-->` comments.
Legacy history parsing remains for compatibility.

## Upgrade note: model packages

Installer reruns preserve an existing `models/<profile>` directory as
administrator-owned configuration and only add missing newly shipped profile
directories. This avoids overwriting local prompt/profile tuning, but an upgraded
installation can therefore retain an older Standard/Thorough package.

For rc1 acceptance, prefer a fresh installation or explicitly refresh shipped
model packages after reviewing local modifications. Model packages are loaded at
process startup, so restart API/provider after changing them.

Existing `provider.env` files are also preserved. If an upgraded 0.8.5
installation explicitly contains legacy `REMOTE_*` data-exposure caps, 0.8.6
continues to enforce them as tighter limits for packaged SunaQ profiles. Fresh
0.8.6 templates instead expose `SUNAQ_REMOTE_HARD_*` ceilings and leave the
legacy variables unset. Administrators who want Thorough (Gründlich) / Deep (Tief) to use their
larger remote budgets after an upgrade must review and deliberately remove or
raise the preserved legacy caps.

## Security invariants retained

- Nextcloud remains the final authorization authority for private document
  evidence.
- Retrieval/index systems do not grant access.
- Unauthorized candidates are removed before verifier/answer evidence use.
- Model entitlement is server-side and per canonical user.
- Provider/client credentials remain server-side.
- Chat archives are separate Nextcloud files with their own ACL/lifecycle.

## Validation status

The rc1 release candidate has completed the planned pre-public validation:

- GitHub CI is green on the release-freeze branch;
- a fresh Super-Light installation was exercised against a real Nextcloud /
  Elasticsearch estate;
- Standard (Schnell), Thorough (Gründlich) and Deep (Tief) were exercised with real data, including the
  bundled SunaQ Recherche client;
- the final CodeRabbit review findings were verified against current code and
  all review threads are resolved;
- a final repository hygiene/secret scan found no committed runtime state,
  private keys, credentials, databases or logs.

The release is feature-frozen. Further retrieval-depth experiments, removal of
the legacy Exhaustive Mode and remaining schema/compatibility renames are
post-rc1 work rather than publication blockers.
