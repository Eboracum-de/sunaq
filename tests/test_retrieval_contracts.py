import pytest

from rag.retrieval.contracts import (
    ArtifactMode,
    AuthorizedDocumentEvidence,
    CapabilityGrant,
    ErgRetriever,
    ErgRetrievalRequest,
    ExternalEvidence,
    EffectiveRetrievalPlan,
    PrivateCandidate,
    ResearchArtifact,
    ResearchGoal,
    ResolutionBatch,
    ResolutionRequest,
    ResolutionResult,
    Resolver,
    ResolverCandidate,
    ResolverGrant,
    ResolverReference,
    RetrievalBatch,
    RetrievalBudgetGrant,
    RetrievalDecisionRecord,
    RetrievalPlannerProposal,
    RetrievalPolicyResolution,
    RetrievalIdentity,
    SrcRetriever,
    SrcRetrievalRequest,
)


def test_retrieval_decision_record_is_structured_and_serializable():
    record = RetrievalDecisionRecord(
        query_id="req-decision-1",
        original_query='Suche "Musterweg 280"',
        normalized_query='Suche "Musterweg 280"',
        planner=RetrievalPlannerProposal(
            intent="find evidence",
            elastic_query='+"Musterweg 280"',
            semantic_query="Musterweg 280",
            entities=("Musterweg 280",),
            concepts=("Adresse",),
        ),
        policy=RetrievalPolicyResolution(
            architecture_tier="SRC",
            model_id="sunaq-standard",
            retrieval_arms=("files",),
            source_scopes=("documents",),
            reason_codes=("SRC_TIER", "quoted_phrase"),
        ),
        effective_plan=EffectiveRetrievalPlan(
            retrieval_arms=("files",),
            source_scopes=("documents",),
            elastic_query='+"Musterweg 280"',
            semantic_query="Musterweg 280",
            max_rounds=1,
            max_queries_per_round=3,
            verification_candidate_limit=10,
            max_complete_documents=15,
        ),
        created_at="2026-10-05T10:00:00+02:00",
    )

    payload = record.to_dict()
    assert payload["schema"] == "sunaq.retrieval_decision.v2"
    assert payload["policy"]["architecture_tier"] == "src"
    assert payload["policy"]["reason_codes"] == ("src_tier", "quoted_phrase")
    assert payload["effective_plan"]["retrieval_arms"] == ("files",)
    assert "reasoning" not in payload
    assert "credentials" not in payload


def test_retrieval_decision_record_rejects_invalid_authority_fields():
    with pytest.raises(ValueError, match="architecture tier"):
        RetrievalPolicyResolution(
            architecture_tier="unknown",
            model_id="sunaq-standard",
        )

    with pytest.raises(ValueError, match="max_rounds"):
        EffectiveRetrievalPlan(
            max_rounds=9,
            max_queries_per_round=1,
            verification_candidate_limit=1,
            max_complete_documents=1,
        )


def test_src_request_has_no_erg_capability_or_transport_controls():
    request = SrcRetrievalRequest(
        query="Project Alpha",
        identity=RetrievalIdentity(user_id="tenant::alice"),
        budget=RetrievalBudgetGrant(max_results=12),
    )

    assert not hasattr(request, "capabilities")
    assert not hasattr(request, "retrieval_arms")
    assert not hasattr(request, "endpoint")
    assert not hasattr(request, "credentials")

    with pytest.raises(ValueError, match="one round"):
        SrcRetrievalRequest(
            query="Project Alpha",
            identity=RetrievalIdentity(user_id="tenant::alice"),
            budget=RetrievalBudgetGrant(max_results=12, max_rounds=2),
        )


def test_retrieval_budget_grant_enforces_hard_caps():
    with pytest.raises(ValueError, match="max_results"):
        RetrievalBudgetGrant(max_results=0)
    with pytest.raises(ValueError, match="max_results"):
        RetrievalBudgetGrant(max_results=101)
    with pytest.raises(ValueError, match="max_rounds"):
        RetrievalBudgetGrant(max_results=1, max_rounds=9)
    with pytest.raises(ValueError, match="max_evidence_chars"):
        RetrievalBudgetGrant(max_results=1, max_evidence_chars=2_000_001)


def test_capability_grant_rejects_unknown_capabilities():
    grant = CapabilityGrant(
        frozenset({" OpenRegister-Research ", "WEB"})
    )
    assert grant.capabilities == frozenset({"openregister-research", "web"})
    assert grant.allows("WEB")

    with pytest.raises(ValueError, match="unknown ERG capabilities"):
        CapabilityGrant(frozenset({"web", "arbitrary-cypher"}))


def test_erg_request_requires_code_issued_capability_grant():
    identity = RetrievalIdentity(user_id="tenant::alice")
    budget = RetrievalBudgetGrant(max_results=5)
    grant = CapabilityGrant(frozenset({"openregister-research", "web"}))

    request = ErgRetrievalRequest(
        query="register research",
        identity=identity,
        budget=budget,
        grant=grant,
    )
    assert request.capabilities == frozenset({"openregister-research", "web"})
    assert not hasattr(request, "context")


def test_private_candidate_is_not_evidence_and_has_no_text_channel():
    candidate = PrivateCandidate(
        document_id="files:42",
        source="vector",
        rank=1,
        score=0.87,
        candidate_kind="semantic",
        trace_id="req-1",
    )
    batch = RetrievalBatch(private_candidates=(candidate,))

    assert not hasattr(candidate, "text")
    assert not hasattr(candidate, "metadata")
    assert batch.evidence == ()
    assert batch.private_candidates == (candidate,)


def test_authorized_and_external_evidence_are_separate_trust_classes():
    document = AuthorizedDocumentEvidence(
        index=2,
        title="Board.pdf",
        text="authorized text",
        document_id="files:7",
    )
    external = ExternalEvidence(
        index=1,
        title="Public source",
        text="external text",
        source="web",
        evidence_id="https://example.invalid/source",
    )
    batch = RetrievalBatch(
        document_evidence=(document,),
        external_evidence=(external,),
    )

    assert document.evidence_id == "files:7"
    assert batch.document_evidence == (document,)
    assert batch.external_evidence == (external,)
    assert batch.evidence == (external, document)

    with pytest.raises(ValueError, match="globally unique"):
        RetrievalBatch(
            document_evidence=(
                AuthorizedDocumentEvidence(
                    index=1,
                    title="Document",
                    text="text",
                    document_id="files:8",
                ),
            ),
            external_evidence=(
                ExternalEvidence(
                    index=1,
                    title="External",
                    text="text",
                    source="web",
                ),
            ),
        )

    with pytest.raises(ValueError, match="document_id"):
        AuthorizedDocumentEvidence(
            index=1,
            title="Missing id",
            text="text",
            document_id="",
        )


def test_retrieval_batch_enforces_issued_result_and_character_budget():
    batch = RetrievalBatch(
        document_evidence=(
            AuthorizedDocumentEvidence(
                index=1,
                title="Document",
                text="123456",
                document_id="files:9",
            ),
        ),
    )
    batch.validate_budget(RetrievalBudgetGrant(max_results=1, max_evidence_chars=6))

    with pytest.raises(ValueError, match="max_results"):
        batch.validate_budget(RetrievalBudgetGrant(max_results=1, max_evidence_chars=6))
        RetrievalBatch(
            document_evidence=batch.document_evidence,
            private_candidates=(
                PrivateCandidate(document_id="files:10", source="vector"),
            ),
        ).validate_budget(RetrievalBudgetGrant(max_results=1))

    with pytest.raises(ValueError, match="max_evidence_chars"):
        batch.validate_budget(RetrievalBudgetGrant(max_results=1, max_evidence_chars=5))


def test_erg_request_rejects_unbounded_or_provider_specific_constraints():
    identity = RetrievalIdentity(user_id="tenant::alice")
    budget = RetrievalBudgetGrant(max_results=5)
    grant = CapabilityGrant(frozenset({"web"}))

    request = ErgRetrievalRequest(
        query="public research",
        identity=identity,
        budget=budget,
        grant=grant,
        constraints=((" Jurisdiction ", "DE"),),
    )
    assert request.constraints == (("jurisdiction", "DE"),)

    with pytest.raises(ValueError, match="unknown ERG research constraint"):
        ErgRetrievalRequest(
            query="public research",
            identity=identity,
            budget=budget,
            grant=grant,
            constraints=(("cypher", "MATCH (n) RETURN n"),),
        )

    with pytest.raises(ValueError, match="constraint value"):
        ErgRetrievalRequest(
            query="public research",
            identity=identity,
            budget=budget,
            grant=grant,
            constraints=(("source", "x" * 513),),
        )


def test_resolver_context_and_research_terms_are_bounded():
    with pytest.raises(ValueError, match="context item"):
        ResolverReference(text="Example AG", context=("x" * 501,))

    with pytest.raises(ValueError, match="term exceeds"):
        ResearchGoal(name="ownership", terms=("x" * 201,))


def test_research_artifact_contract_separates_inline_and_remote_material():
    inline = ResearchArtifact(
        source="openregister",
        external_id="DE-1",
        file_name="details.json",
        content_type="application/json",
        mode=ArtifactMode.INLINE_TEXT,
        content='{"name":"Example AG"}',
    )
    assert inline.url == ""

    remote = ResearchArtifact(
        source="openregister",
        external_id="DE-1",
        file_name="register.pdf",
        content_type="application/pdf",
        mode=ArtifactMode.REMOTE,
        url="https://register.example/document.pdf",
    )
    assert remote.content == ""

    with pytest.raises(ValueError, match="HTTPS"):
        ResearchArtifact(
            source="openregister",
            external_id="DE-1",
            file_name="register.pdf",
            content_type="application/pdf",
            mode=ArtifactMode.REMOTE,
            url="http://127.0.0.1/private",
        )
    with pytest.raises(ValueError, match="path-free"):
        ResearchArtifact(
            source="openregister",
            external_id="DE-1",
            file_name="../../secret",
            content_type="application/json",
            mode=ArtifactMode.INLINE_TEXT,
            content="{}",
        )


def test_resolution_contract_uses_known_adapter_grant_and_keeps_research_out():
    reference = ResolverReference(text="Example AG")
    candidate = ResolverCandidate(
        adapter="openregister",
        candidate_id="DE-1",
        canonical_name="Example Aktiengesellschaft",
        confidence=0.94,
    )
    result = ResolutionResult(
        reference=reference,
        status="RESOLVED",
        candidates=(candidate,),
    )
    request = ResolutionRequest(
        references=(reference,),
        grant=ResolverGrant(frozenset({"neo4j", "openregister"})),
    )

    assert result.status == "resolved"
    assert request.allowed_adapters == frozenset({"neo4j", "openregister"})
    assert not hasattr(candidate, "artifacts")

    with pytest.raises(ValueError, match="unknown resolver adapters"):
        ResolverGrant(frozenset({"neo4j", "shell"}))


def test_retrieval_identity_preserves_actual_group_boundaries():
    identity = RetrievalIdentity(
        user_id="tenant::alice",
        groups=("finance", "board"),
    )
    assert identity.groups == ("finance", "board")


@pytest.mark.asyncio
async def test_protocols_are_structural_and_transport_neutral():
    class ResolverImpl:
        async def resolve(self, request):
            return ResolutionBatch()

    class SrcImpl:
        async def retrieve(self, request):
            return RetrievalBatch()

    class ErgImpl:
        async def retrieve(self, request):
            return RetrievalBatch()

    resolver = ResolverImpl()
    src = SrcImpl()
    erg = ErgImpl()

    assert isinstance(resolver, Resolver)
    assert isinstance(src, SrcRetriever)
    assert isinstance(erg, ErgRetriever)
    assert await resolver.resolve(
        ResolutionRequest(
            references=(ResolverReference(text="Example AG"),),
            grant=ResolverGrant(frozenset({"neo4j"})),
        )
    ) == ResolutionBatch()
