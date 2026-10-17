# SunaQ models

A **SunaQ model** is the user-visible research/work profile exposed through the
OpenAI-compatible `/v1/models` endpoint. It is deliberately distinct from the
underlying LLM.

Each direct subdirectory is one selectable model:

```text
models/
  standard/   # documentation name: Standard; current German label: Schnell
    profile.yaml
    prompts/
      retrieval_planner.txt
      candidate_verifier.txt
      evidence_decision.txt
      rag_answer.txt
  thorough/   # documentation name: Thorough; current German label: Gründlich
    profile.yaml
    prompts/
      ...
  deep/       # documentation name: Deep; current German label: Tief
    profile.yaml
    prompts/
      ...
```

Profiles and all referenced prompt files are read and validated once when the
API/provider process starts. Requests only select the compiled runtime model;
no YAML or prompt file is read on the request path.

## Ownership boundary

For packaged SunaQ models, request-local research behaviour belongs in
`models/<model>/profile.yaml`, not in `config.yaml`.

The profile sections are:

- `search`
- `retrieval_planner`
- `evidence_control`
- `retrieval_signal`
- `entity_resolution`
- `graph_retrieval`
- `reranker`
- `context_enrichment`
- `answer_context`

Global infrastructure/security/background policy remains in `config.yaml`.
Examples include Elasticsearch/Qdrant/Neo4j endpoints, embeddings, ACL,
credentials, source configuration, graph queue/discovery workers, synchronization
and archive engines.

## Budget profiles

The initial 0.8.6 profile experiment deliberately isolates **budget size** from
extra retrieval rounds and model reasoning. All shipped profiles currently use
one retrieval round, Evidence Review is off, and planner thinking remains off.

| Model | Verification window | Answer documents | Per-document answer chars | Total answer chars |
| --- | ---: | ---: | ---: | ---: |
| `sunaq-standard` | 10 | 10 | 2,500 | 25,000 |
| `sunaq-thorough` | 30 | 30 | 4,000 | 100,000 |
| `sunaq-deep` | 50 | 50 | 6,000 | 200,000 |

For remote LLM roles these profile budgets remain subject to separate
administrator-controlled `SUNAQ_REMOTE_HARD_*` ceilings. The shipped ceilings are
intentionally at least as high as the shipped profile budgets so they remain a
safety boundary rather than silently collapsing Standard (Schnell), Thorough (Gründlich) and Deep (Tief) back
to the same window. Preserved 0.8.5 `REMOTE_*` values are treated as explicit
tighter caps for upgrade safety; review, remove or raise those legacy overrides
only when the larger profile budget is intentionally allowed to leave the host.

This staging is intentional: first validate budget behaviour, then evaluate
additional retrieval rounds, and only afterwards experiment with model
thinking/reasoning.

The registry intentionally strips legacy copies of the request-local sections
from `config.yaml` before compiling a packaged model. This prevents one global
setting from silently changing every selectable SunaQ model. If no packaged
models exist, the legacy single-model compatibility path still uses
`config.yaml`.

## Upgrade behaviour and local ownership

The installer seeds `models/` when it is missing, including an upgrade from a
0.8.5 installation. After that, `models/` is treated as administrator-owned
configuration and is not overwritten by installer reruns. Local LLM routing and
prompt engineering therefore survive updates.

The shipped `sunaq-standard` profile has `legacy_config_overlay: true` as a
0.8.5 compatibility bridge. If an upgraded installation still contains the old
request-local sections in `config.yaml`, those values override the
corresponding Standard profile values only. Other SunaQ models are unaffected.
Fresh 0.8.7-rc1 configurations contain no such legacy sections, so the bridge is
inert.

Once an upgraded installation has intentionally transferred its tuning into the
Standard model package, administrators may remove the old request-local
sections from `config.yaml` (or disable the bridge in the profile).

## Deployment-specific overrides

A model may carry small capability-preserving overrides for a deployment
profile. These are still part of the SunaQ model package; they do not move
request-local policy back into `config.yaml`.

Example:

```yaml
deployment_overrides:
  dockerized:
    retrieval_planner:
      verification_candidate_limit: 10
    graph_retrieval:
      enabled: false
    reranker:
      backend: none
```

Deployment-specific overrides may tune packaging-sensitive defaults without
conflating deployment with the architecture tier. The shipped profiles use the
`dockerized` override only for budget-preserving settings; SRC/ERG policy gates
still determine the effective retrieval arms. Deployment overrides are merged
over the model's own section values during startup compilation.

## Research backends per SunaQ model

A user-selectable SunaQ model is the unit for both research budgets and
**authorized research capabilities**. The global administrator-owned registry
defines available backend IDs, URLs, credentials and hard limits. The model
selects one registered backend for Web research; it cannot construct, enable or
change a backend. A user's allowed model list remains a separate admin grant.

Two kinds must stay separate:

| Kind | Candidate adapters | Result ownership |
| --- | --- | --- |
| Web Retrieval | Brave Search, future Brave LLM Context | Web evidence; SunaQ verifies and produces the answer |
| Delegated Research | OpenAI Web Search, future Gemini or Brave Answers | External service produces answer and citations; SunaQ controls provenance, egress and chat persistence |

The current packaging is turn-oriented rather than hiding provider
switches below one generic Web button:

- normal SunaQ models (for example Standard/Thorough/Deep (Schnell/Gründlich/Tief)) may expose native Web
  Retrieval such as Brave Search alongside internal sources;
- Delegated Research should normally be exposed as a separate user-visible
  SunaQ model (for example "OpenAI Research"), making cost and external
  processing an explicit turn-level choice;

The Nextcloud client keeps the same conversation id and sends the accumulated
message history with whichever SunaQ model is selected for the next turn.
Users can therefore move between normal SunaQ models and Delegated Research
without starting a new chat. The selected model determines
the current turn's capability envelope; it does not own the conversation.

Profile fragment (supported by the current registry):

~~~yaml
capabilities:
  documents: true
  web: true
research:
  web_backend: openai-research
~~~

'config.yaml' would register 'openai-research' separately. The model only
references that ID; the effective grant is intersected with architecture tier,
global configuration and per-user permission. If the ID is invalid, absent,
or disabled, the request must fail closed rather than silently selecting an
alternative provider. No provider-specific API keys belong in profile YAML.

**Current status:** Profile 'research.web_backend' selects a named entry from the
administrator-owned 'config.yaml:research_backends' registry. Implemented
adapters are 'brave_search' (existing native Web pipeline configured in
web.yaml) and 'openai' (hosted Web Search). Model packages without an explicit
binding retain the global 'delegated_research' or
'SUNAQ_RESEARCH_PROVIDER' choice for upgrade compatibility.

A model can set 'capabilities.web: false' or 'research.web_backend: none' to
disable Web. Unknown/disabled backends are rejected, never silently replaced.
The Nextcloud 0.3.16 UI updates visible source selectors on model changes.
Existing clients remain server-authorized but may show outdated selectors.

**Current egress status:** The shared outbound policy is implemented with
`research_query`, `model_processing` and `embedding` classes. Research
queries default to deterministic `guard`; model processing defaults to
non-destructive `audit`; embeddings default to `allow`. Administrators may
set any class to `allow`, `audit`, `guard` or `block`.

**Not yet implemented:** Gemini, Brave LLM Context, Brave Answers and combined
Documents+Web through a delegated Research Provider. Combined requests for a
delegated backend still fail explicitly rather than silently switching engines.
The preferred UX is to use native Brave Web Retrieval in normal models and
switch to a dedicated Delegated Research model for a separate turn in the same
chat.

See docs/RETRIEVAL-ARCHITECTURE.md sections 3.5-3.9.

## LLM roles

`roles` independently routes the `planner`, `verifier`, `evidence` and
`answer` roles to underlying LLMs. Missing role values inherit the established
global/role environment configuration, so profiles can share an LLM or mix
local/remote LLMs.

Secrets are referenced through `api_key_env`; plaintext API keys are rejected.

Example:

```yaml
roles:
  planner:
    model: qwen3:4b
  verifier:
    backend: openai
    base_url: https://api.example/v1
    model: strong-model
    api_key_env: VERIFIER_API_KEY
  answer:
    backend: openai
    base_url: https://api.example/v1
    model: strong-model
    api_key_env: ANSWER_API_KEY
```

## Model-specific prompts

`prompts` maps logical prompt slots to files within the model directory. A
simple filename resolves below `models/<model>/prompts/`.

The core research slots currently packaged per model are:

- `query_rewriter` -> backend-neutral SearchSpec generation;
- `planner` -> retrieval/query planning;
- `verifier` -> candidate document verification;
- `evidence` -> evidence-control decision;
- `answer` -> final document-grounded answer.

Other provider prompts (natural-command parsing, web gating, follow-up rewrite,
etc.) remain common structural behaviour unless a model explicitly overrides the
corresponding prompt slot. This keeps responsiveness, client behaviour and
workflow semantics consistent across models while still allowing prompt
engineering for the research roles.

## Shipped profiles

The shipped user-facing order is deterministic:

1. **Standard (Schnell)** (`sunaq-standard`, order 10)
2. **Thorough (Gründlich)** (`sunaq-thorough`, order 20)
3. **Deep (Tief)** (`sunaq-deep`, order 30)

The internal IDs remain stable even if a display label changes. The legacy
provider model ID `nextcloud-hybrid-rag` remains an alias of
`sunaq-standard`.

For the current 0.8.7-rc1 release candidate, all three profiles deliberately use
**one retrieval round**, `evidence_control.mode: off` and planner
`thinking: false`. The intended difference is budget only:

| Display name | Model ID | Candidate/verifier window | Answer context |
| --- | --- | ---: | ---: |
| Standard (Schnell) | `sunaq-standard` | 10 | 10 docs / 25k chars |
| Thorough (Gründlich) | `sunaq-thorough` | 30 | 30 docs / 100k chars |
| Deep (Tief) | `sunaq-deep` | 50 | 50 docs / 200k chars |

This isolation is intentional. Additional retrieval rounds and model thinking
are separate experiments and are not part of rc1's shipped behaviour.

A near-capacity run can warn that the current ranked candidate window is almost
exhausted without claiming that additional ACL-visible hits are known to exist.
A hard limit warning is emitted only when known candidates were not checked.

## Per-user access

`sunaq-standard` / **Standard (Schnell)** is the safe default entitlement. Thorough (Gründlich) and
Deep (Tief) are opt-in per canonical user through SunaQ Admin. The authenticated
`/v1/models` response exposes only models currently allowed for that user.

Follow-up actions may suggest rerunning the same query with a stronger model,
but only when a strictly stronger **allowed** profile exists. A profile must
never suggest itself as an upgrade.

## Runtime and upgrade notes

The model registry and prompt files are loaded at API/provider process startup.
After changing a profile package, restart the normal API/provider processes
before testing it. In the Dockerized deployment, `models/` is mounted read-only
into both API and provider containers, so the normal Maintenance ON/OFF
recreate cycle reloads administrator-owned profile and model-specific prompt
changes; an image rebuild is not required for those files.

Installer reruns preserve existing model-package directories as
administrator-owned configuration and add only newly shipped package
directories. This prevents silent overwrites of local prompt/profile tuning,
but it also means an upgraded installation can intentionally retain an older
Standard/Thorough package. For release acceptance, use a fresh installation or
explicitly refresh the shipped model packages after reviewing local changes.
A future installer option may make that refresh operation explicit; no such
switch is part of rc1 yet.
