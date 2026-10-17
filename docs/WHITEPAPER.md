# SunaQ design principles and trust envelopes

**Status:** architectural whitepaper for the 0.8.7-rc1 release-candidate line  
**Scope:** design intent, trust boundaries and system responsibilities

Normative and operational detail lives in [SRC / ERG](SRC-ERG.md),
[Architecture](ARCHITECTURE.md), [Retrieval Architecture](RETRIEVAL-ARCHITECTURE.md),
[Technical Reference](TECHNICAL-REFERENCE.md) and [Threat Model](THREAT-MODEL.md).

SunaQ is an evidence-first AI gateway for existing Nextcloud installations. Its
purpose is not to replace Nextcloud, copy an organization into a new AI-native
knowledge base, or make an LLM the owner of retrieval logic. It adds research,
retrieval and reasoning capabilities around an existing document estate while
preserving explicit authority, lifecycle and trust boundaries.

The architectural question behind SunaQ is not “how do we put all organizational
knowledge into an AI system?” It is:

> How do we give modern reasoning systems useful access to an existing
> information estate without silently replacing the systems that already own
> documents, authorization and retention?

That distinction drives the architecture.

---

## 1. Brownfield first

Many organizations already have three valuable assets: a document system of
record, a working permission model and a lexical search index that reflects
years of operational use.

For SunaQ, these are not legacy obstacles to be hidden behind a new database.
They are the starting point.

Nextcloud remains the authoritative document store. Its live access decision
remains authoritative for private document evidence. Existing
FullTextSearch/Elasticsearch infrastructure is reused where practical. Optional
derived stores such as Qdrant or Neo4j are introduced only when they provide
clear additional value.

SunaQ therefore follows a brownfield integration principle:

> Reuse authoritative systems; add derived capabilities around them; never
> confuse a retrieval index with an authority boundary.

---

## 2. Retrieval and authorization are different problems

Search systems answer: “Which documents might be relevant?”

Authorization answers: “Which of those documents may this user access now?”

SunaQ keeps these questions separate. Elasticsearch, Qdrant and Neo4j may
propose private document candidates. They do not grant access. For normal
private document evidence, a candidate must pass the current Nextcloud live ACL
before its content can become verifier evidence, answer context or portable
user-visible execution provenance.

This produces the core security invariant:

> **Retrieval systems propose candidates. Nextcloud remains the final
> authorization authority for private document evidence.**

A stale Elasticsearch entry may reduce retrieval quality, but it must not
become an authorization decision. A stale vector or graph entry may propose a
document, but it must not make that document visible.

The same distinction shapes diagnostics. Pre-ACL candidate counts, scores and
ranks can reveal hidden repository state. They may exist in protected operator
logs, but portable ResearchRun provenance begins at the post-ACL boundary.

---

## 3. Three trust envelopes, one product

SunaQ is the product. SRC, ERG Internal and ERG Full are data/retrieval trust
envelopes, not separate products and not deployment mechanisms.

| Envelope | Private retrieval/data plane | External research plane | Main additional obligation |
| --- | --- | --- | --- |
| **SRC** | Nextcloud Documents + Elasticsearch/FullTextSearch | none | keep the authoritative Nextcloud/FTS lifecycle healthy |
| **ERG Internal** | SRC + locally controlled derived state/capabilities | none | lifecycle/provenance for SunaQ-owned derived state |
| **ERG Full** | ERG Internal | explicitly granted external research/resolution | egress, external-provider, Web and provenance policy |

The envelope does not decide whether planner, verifier or answer models are
local or remote. Model-processing egress is a separate administrative decision.
A deployment can therefore use strict SRC retrieval with a deliberately remote
answer model, or ERG Internal with entirely local models.

Likewise, the envelope is independent of packaging. **standard** and
**dockerized** describe deployment. **core**, **workgroup** and **full**
describe capability policy.

### 3.1 SRC — a first-class retrieval architecture

Secure RAG Core is intentionally narrow:

- ordinary Nextcloud Documents only;
- Elasticsearch / Nextcloud FullTextSearch as the only document retrieval arm;
- mandatory live Nextcloud ACL;
- one bounded retrieval round;
- no SunaQ-owned vector/document-graph retrieval state;
- no Mail/Chat/Web research plane;
- no external resolver/research adapters.

SRC should not be understood as “ERG minus the useful parts”. It is intended to
be a strong standalone retrieval system for document estates with useful
lexical anchors: names, organizations, document types, project names, reference
numbers, dates, contractual terms and similar business vocabulary.

The engineering goal is:

> **Make lexical retrieval semantically well planned before adding semantic
> retrieval as a repair mechanism.**

A vector store can improve recall, but it should not be required to compensate
for a planner/compiler pipeline that discarded the user's explicit concepts.

### 3.2 ERG Internal — local derived intelligence

ERG Internal permits locally controlled SunaQ-derived state and capabilities,
for example:

- Qdrant/vector retrieval when infrastructure is available;
- Neo4j identity/alias resolution;
- Graph-Lite / Research Findings;
- local resolver adapters;
- Mail and Chat archive evidence stored in the operator-controlled estate.

These features can materially improve recall, identity resolution and reusable
organizational research. They also create lifecycle obligations that SRC avoids:
derived data can become stale, graph knowledge needs provenance, and deletes or
permission changes may need propagation beyond the source system.

ERG Internal therefore adds capability without changing the authority rule:
private document content still requires live Nextcloud authorization.

### 3.3 ERG Full — explicit external research

ERG Full additionally permits explicitly configured external research and
resolution boundaries, such as public Web retrieval, Delegated Research or
external register/locality adapters.

This introduces a different class of risk: query or context egress, provider
retention and availability, untrusted public evidence, SSRF/redirect handling,
cost/rate limits and citation/provenance normalization across providers.

External capabilities are not selected by arbitrary model output. Code,
administrator configuration, architecture tier and model-package policy
intersect to produce a concrete capability grant.

---

## 4. The planner describes intent; it does not own backend syntax

One of the most important SunaQ design rules is:

> **The planner describes retrieval intent. It never owns Elasticsearch,
> Qdrant, Cypher, SQL or provider-specific query syntax.**

The planner may identify entities, concepts, alternative lexical realizations
of the same concept, explicit constraints, a natural semantic query and
verification requirements.

For example, “Jane Doe CV” may become semantic intent equivalent
to:

    entity:
      Jane Doe

    required concept:
      CV

    alternative lexical realizations:
      CV
      résumé
      curriculum vitae
      professional background

The deterministic Files compiler can then produce logic equivalent to:

    MUST "Jane Doe"
    MUST (
        CV
        OR résumé
        OR "curriculum vitae"
        OR "professional background"
    )

The distinction matters for security, portability and debuggability. A model
cannot smuggle arbitrary backend syntax into the retrieval engine; different
backends may compile the same intent differently; query behavior can be tested
deterministically; and planner quality can be diagnosed separately from
compiler quality.

---

## 5. Required concepts, alternatives and evidence verification

Business search often contains several kinds of lexical information that should
not be conflated.

An entity such as Example GmbH may be required. A document type such as
Rechnung may also be required. Terms such as Rechnung and Invoice may be
different lexical forms of the same semantic criterion.

The structured retrieval plan can therefore express concept groups:

    {
      "entities": ["examplehost"],
      "concepts": ["Rechnung"],
      "concept_groups": [
        {
          "required": true,
          "terms": ["Rechnung", "Invoice"]
        }
      ],
      "constraints": [
        {"kind": "year", "value": "2025"}
      ]
    }

The compiler owns occurrence semantics such as MUST(OR(...)). The planner owns
the semantic judgment that the alternatives belong to the same criterion.

Retrieval remains recall-oriented. A lexical alternative can still match an
incidental occurrence in a large document. SunaQ therefore separates candidate
retrieval from Candidate Verification. The verifier operates only on
ACL-authorized candidates and checks whether the document itself supports the
requested subject or relationship rather than merely mentioning a matching
token.

The verifier is a precision layer, not an authorization layer.

---

## 6. Vectors complement lexical retrieval; they do not define correctness

Qdrant/vector retrieval is valuable where lexical wording diverges from user
intent, OCR is noisy, terminology varies, or the relevant concept is not
expressed through stable business anchors.

ERG can therefore combine lexical and semantic signals. The intended
relationship is:

    strong lexical retrieval
            +
    optional semantic recall
            +
    optional graph/resolver signals
            ↓
    fusion / deduplication
            ↓
    live ACL
            ↓
    verification

It is not “weak lexical retrieval followed by a vector store that repairs the
query”.

This matters operationally because full-corpus vectorization has lifecycle,
resource and model-version costs. Organizations should be able to obtain useful
AI-assisted research from their existing lexical estate before committing to a
second full derived corpus.

---

## 7. Code grants capabilities; models propose work

SunaQ deliberately avoids model-controlled infrastructure selection.

A model may propose structured intent, references to resolve, a semantic search
phrase or the use of an already configured capability. A model may not invent a
new endpoint, enable a disabled retrieval arm, raise a hard budget, obtain
credentials, bypass the current architecture tier, execute arbitrary backend
query languages, make a private candidate authorized or persist arbitrary
derived state.

The effective operation is the intersection of:

    user request
    ∩ model/profile policy
    ∩ architecture tier
    ∩ administrator configuration
    ∩ current user capability
    ∩ hard runtime limits

The result is a bounded execution plan.

---

## 8. Provenance follows trust boundaries

SunaQ distinguishes decision provenance from execution provenance.

### 8.1 Retrieval Decision

**retrieval-decision.v2.json** records what was understood and allowed before
retrieval:

- original and normalized query;
- structured planner output;
- entities, concepts, concept groups and constraints;
- verification requirements;
- architecture/profile policy resolution;
- granted retrieval/source/resolver capabilities;
- bounded effective budgets.

It intentionally does not contain backend query syntax. Backend syntax belongs
to execution.

### 8.2 Retrieval Execution

**retrieval-execution.v1.json** records what was actually executed, but portable
execution provenance begins only after the live Nextcloud ACL boundary for
private candidates.

It may contain:

- the actual executed backend queries;
- ACL-authorized candidate documents;
- ranks expressed within the authorized candidate set;
- verifier decisions only for those authorized candidates;
- the final selected documents.

It must not contain portable pre-ACL repository telemetry such as raw
Elasticsearch hit counts, identities of denied candidates, pre-ACL
ES/RRF/vector ranks or scores whose gaps could reveal hidden candidates.

Server-side operator logs may retain richer diagnostic information under the
deployment's normal log-access policy. The ResearchRun is designed to be safe
portable provenance for the user-visible research process.

### 8.3 Selected evidence

**documents.json** describes the authorized documents that actually reached the
answer context. It follows the same post-ACL rank rule and does not copy
pre-ACL backend ranks or scores.

The audit chain is therefore:

    retrieval-decision.v2.json
        what was understood / granted
              ↓
    retrieval-execution.v1.json
        what was executed / authorized / verified
              ↓
    documents.json
        what actually reached the answer model

---

## 9. Lifecycle is part of architecture

Derived data is not free merely because it is rebuildable.

SRC has the smallest SunaQ-owned lifecycle surface. It deliberately relies on
the Nextcloud deployment to keep the FullTextSearch index aligned with the
authoritative document lifecycle:

> SRC does not own the lifecycle of the Nextcloud Full-Text Search index. The
> organization/Nextcloud deployment is responsible for ensuring that the
> Elasticsearch index follows the authoritative Nextcloud data-retention and
> deletion lifecycle. SRC treats Elasticsearch as a derived retrieval source
> and uses live Nextcloud authorization to prevent stale index state from
> becoming an authorization decision.

ERG Internal adds SunaQ-owned derived vector/graph/archive state and therefore
adds explicit deletion, rebuild, backup and provenance obligations. ERG Full
additionally adds external providers whose lifecycle is not controlled by the
SunaQ operator.

The architectural tier is therefore also a statement about how much derived
state and external dependency the operator deliberately accepts.

---

## 10. Conversation and archive state are separate from retrieval authority

A saved chat or ResearchRun is a new stored object with its own lifecycle and
ACL. It may contain summaries, citations or Web Evidence derived from other
sources. Revoking access to an original source document does not retroactively
rewrite every independently stored derivative unless an explicit retention or
purge process does so.

For that reason, archive capabilities are optional and belong outside SRC.

The same principle applies to Research Findings and curated graph knowledge:
shared retrieval knowledge may improve future searches, but it never grants
access to the original supporting document.

---

## 11. Modular first, distributed second

SunaQ is progressively separating the Provider/Orchestrator, Resolver, SRC
Retriever and ERG Retriever. The purpose is clear responsibility and testable
contracts, not service proliferation.

The preferred evolution is to define typed trust/capability contracts first,
move backend-specific behavior behind those contracts, test semantic and
security parity, and distribute components only where operationally useful.

A separate service is justified by trust, lifecycle, scaling or deployment
needs—not merely because a module can technically be placed behind HTTP.

---

## 12. Non-goals

SunaQ is not intended to become:

- a replacement document management system;
- an independent permission authority;
- an unconstrained autonomous agent platform;
- a generic arbitrary-action execution engine;
- a system where an LLM can directly author backend query languages;
- a requirement to vectorize the entire corpus before useful retrieval is
  possible;
- a hidden knowledge base whose provenance cannot be traced back to source or
  research artifacts.

These non-goals are as important as the feature list because they keep the
security and operational boundary understandable.

---

## 13. Practical design test

When considering a new feature, the architecture should be challenged with a
small set of questions:

1. Who is authoritative for the underlying data?
2. Who authorizes access at the moment evidence is used?
3. Does this feature create new derived state?
4. What is its deletion and backup lifecycle?
5. Can a model only propose the operation, or can it accidentally grant the
   capability?
6. Is backend syntax compiled deterministically from structured intent?
7. Does persisted provenance begin at the correct trust boundary?
8. Can SRC remain useful without this feature?
9. If the feature is external, what exactly leaves the private plane?
10. Can an operator explain the failure mode from the stored decision and
    execution records?

A feature that cannot answer these questions cleanly probably belongs behind a
stronger boundary—or needs a simpler design.

---

## 14. Summary

SunaQ's core architectural position can be condensed to five statements:

1. **Keep Nextcloud authoritative for documents and access.**
2. **Treat retrieval indexes as candidate generators, never authorization
   systems.**
3. **Let models describe semantic intent; let deterministic code compile and
   grant execution.**
4. **Make SRC a strong lexical-first system; add ERG capabilities because they
   add value, not because they are required to repair the core.**
5. **Persist provenance at the same trust boundaries that govern evidence.**

SRC, ERG Internal and ERG Full are different answers to the same operational
question: how much derived state and external research capability should this
deployment deliberately own?

The narrowest answer should remain useful. The broader answers should remain
explicit.
