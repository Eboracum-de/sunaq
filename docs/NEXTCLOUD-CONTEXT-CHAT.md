# Relationship to Nextcloud Context Chat

**Reference:** `0.8.7-rc1`  
**External facts checked:** 2026-09-19

Nextcloud Context Chat is the most relevant comparison for SunaQ because it is Nextcloud's own native RAG/search assistant solution. This document is not a feature-ranking or a claim that one architecture is universally better. The two projects optimize for different operating assumptions.

Official references:

- Nextcloud Administration Manual: https://docs.nextcloud.com/server/stable/admin_manual/ai/app_context_chat.html
- Nextcloud File Access Control documentation: https://docs.nextcloud.com/server/stable/admin_manual/file_workflows/access_control.html
- Context Chat content-provider documentation: https://github.com/nextcloud/context_chat/blob/main/doc/Implementing_a_provider.md
- Context Chat Backend: https://github.com/nextcloud/context_chat_backend

## 1. Context Chat

The current Nextcloud Administration Manual describes Context Chat as an ensemble of:

- the PHP `context_chat` app; and
- the Python `context_chat_backend` ExternalApp.

The current manual lists Nextcloud 32 as the minimum server version and supports on-premises model operation, including x86-64 CPU and supported GPU paths.

Context Chat is therefore the natural first choice to evaluate for a current Nextcloud deployment that wants the supported native Assistant integration.

## 2. Different authorization strategy

SunaQ deliberately does not copy Nextcloud ACL state into its retrieval stores as the final authorization source.

Its core model is:

```text
global candidate discovery
        |
        v
live Nextcloud check for the current user and concrete file
        |
        v
authorized document evidence
```

Context Chat's provider model indexes submitted content together with user/access information and exposes operations for updating/removing access in its knowledge base. As of Nextcloud 32 the older internal provider API documented in `Implementing_a_provider.md` has been superseded by the OCP API, but the architectural distinction remains relevant: content is ingested into Context Chat's own knowledge base with its access scope.

These are different trade-offs:

- replicated/index-time access scope can make retrieval efficient but requires synchronization of authorization state;
- SunaQ's live post-retrieval authorization avoids treating a copied ACL as authoritative, at the cost of live checks and a bounded post-filtering window.

## 3. File Access Control and live WebDAV authorization

Nextcloud's current administration manual explicitly states that Context Chat does **not** follow rules from the `files_accesscontrol` app and may return indexed information for a file that those rules deny while the file remains visible to that user in Files.

SunaQ uses a different authorization model rather than claiming a generally stronger one. Each concrete candidate is checked through Nextcloud WebDAV with the current user's own Nextcloud credential. This deliberately reuses the same access path used by ordinary WebDAV clients and mounted/synchronized Nextcloud file access instead of maintaining an independent authorization copy inside the RAG index.

The practical consequence is architectural: SunaQ attempts to inherit the security decisions of the already-operated Nextcloud/WebDAV environment at request time. Deployments using unusual request-dependent `files_accesscontrol` rules should nevertheless include those rules in their acceptance test, because such policies can depend on request properties such as source address, URL, time or user agent.

The live check is batched. With the current default `acl.batch_size: 100`, a set of 50 candidate file IDs is checked in a single WebDAV `SEARCH` request, not 50 individual requests. The cost is therefore normally one authenticated Nextcloud round trip per bounded candidate batch.

## 4. Existing-index and independently operated middleware model

SunaQ's design center is a deployment that already has a useful Nextcloud FullTextSearch/Elasticsearch corpus and does not want to build a second authoritative document repository merely to add RAG.

That leads to several deliberate choices:

- reuse the existing Elasticsearch candidate source;
- keep Qdrant optional;
- keep Neo4j optional/Graph-Lite;
- run the provider/retrieval path as independent long-running middleware rather than as a Nextcloud application task;
- keep the frontend replaceable through an OpenAI-compatible provider boundary;
- allow model roles to be local or remote independently;
- retain Nextcloud as the final document authorization authority.

### Request latency and background processing

The two systems also have different process models. Context Chat uses Nextcloud's task/background infrastructure; the current administration manual recommends background workers for faster pickup of AI tasks. Nextcloud's general background-job subsystem can be operated through AJAX, Webcron, cron or continuous workers depending on version and deployment.

SunaQ's normal query path is served directly by its own long-running API/provider processes. It is therefore not coupled to Nextcloud's background-job scheduler for ordinary query execution. This does not imply that one system is inherently faster: total response time still depends on retrieval stores, model backend, model size, hardware, network latency and enabled verification/research stages.

### Model choice

SunaQ is deliberately model-agnostic at the provider boundary. Planner, verifier, evidence-control and answer roles can use different OpenAI-compatible or locally hosted model backends, and administrators decide whether each role remains local or is sent to a commercial provider. Answer quality therefore depends materially on the selected model.

Development testing has included Qwen3:8B through Ollama and GPT-5.6 Sol; these are test points, not a closed compatibility list or a quality guarantee.

### Frontend and composition

SunaQ is also UI-agnostic. The bundled SunaQ Recherche app is one client, while a separately operated OpenWebUI or another OpenAI-compatible frontend can use the same provider. The boundary also makes it possible to compose SunaQ with other local RAG systems, agents or research tools when an administrator deliberately grants access.

That flexibility moves trust to the provider-client boundary. A trusted client API key authenticates the integration server, not the person using its UI; the integration supplies the external user identity that is mapped to the corresponding server-side Nextcloud credential. Client keys must therefore remain server-side. For exposed integrations, reverse-proxy network allowlists, mTLS or equivalent controls are useful defense in depth in addition to the provider key.

This makes SunaQ relevant to existing/heterogeneous installations and operators who accept more hands-on administration in exchange for control over retrieval, integration and model placement.

It does **not** make SunaQ a substitute for the broader maturity, native UI integration, support path or ecosystem of Context Chat.

## 5. Shared knowledge versus evidence

SunaQ intentionally allows organization-wide reuse of curated identity/alias knowledge and shared Finding curation. The benefit is that users do not repeat the same Graph-Lite work.

That shared state is retrieval/curation knowledge, not a grant to read its supporting source.

A user can benefit from an alias first established by another user's work; any document subsequently used as answer evidence must still pass the current user's live Nextcloud ACL.

## 6. Scope comparison

| Topic | Nextcloud Context Chat | SunaQ |
|---|---|---|
| Primary position | native Nextcloud Assistant/RAG stack | independent middleware for existing Nextcloud/FTS estates |
| Current minimum Nextcloud in official docs | 32 | SunaQ app/docs target 23+; deployment compatibility must be acceptance-tested per installation |
| Retrieval store | Context Chat backend knowledge/index stack | existing Elasticsearch first; optional Qdrant/Neo4j |
| Authorization model | access scope represented in Context Chat content/index lifecycle | current Nextcloud visibility checked live for concrete file candidates |
| Frontend | native Assistant integration | SunaQ Recherche or separate OpenAI-compatible frontends; composable with other tools when explicitly trusted |
| Request process | Nextcloud task/background infrastructure; background workers recommended for faster pickup | independent long-running API/provider request path |
| Model placement | configurable Nextcloud task-processing providers | role-specific, model-agnostic local/remote backends |
| Graph-Lite curation | not an SunaQ-equivalent design goal | explicit optional SunaQ subsystem |
| Operational maturity | official Nextcloud project/ecosystem | release-candidate project with limited external usage |

The table describes project shape, not a quality score.

## 7. When the distinction matters

Context Chat is the obvious baseline when:

- the deployment is on a supported current Nextcloud release;
- native Assistant integration and upstream support matter most;
- operating the official Context Chat backend is acceptable.

SunaQ's architecture is specifically interesting when:

- an existing FullTextSearch estate should remain the retrieval substrate;
- authorization should be checked against Nextcloud at request time rather than treated as a copied ACL;
- local/private model placement, free model choice and per-role routing are important;
- a replaceable OpenAI-compatible frontend boundary or integration with other local tools is desired;
- ordinary request handling should remain independent from the Nextcloud background-job scheduler;
- Graph-Lite/shared research curation is useful.

Operators should evaluate both against their own security model, supported Nextcloud version and operational capacity.
