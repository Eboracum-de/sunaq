import asyncio

from rag.resolver_client import (
    ResolverSettings,
    build_resolve_request,
    entity_context_from_resolver,
    load_resolver_settings,
    merge_entity_context,
    resolve_search_spec,
)


def test_resolver_request_sends_entity_reference_and_keeps_context_structured():
    payload = build_resolve_request({
        "entities": ["EXAMPLE MACHINERY AG"],
        "constraints": [{"kind": "location", "value": "Todtnau"}],
    })

    assert payload["references"] == [{
        "text": "EXAMPLE MACHINERY AG",
        "context": ["Todtnau"],
        "hints": {"kind": "organization"},
    }]
    assert payload["limit_per_reference"] == 8


def test_resolver_request_preserves_model_entity_hint_and_source_filter():
    payload = build_resolve_request(
        {
            "entities": ["John Doe"],
            "entity_hints": [{"text": "John Doe", "kind": "person"}],
            "constraints": [{"kind": "location", "value": "Berlin"}],
        },
        sources=["openregister"],
    )
    assert payload["references"] == [{
        "text": "John Doe",
        "context": ["Berlin"],
        "hints": {"kind": "person"},
    }]
    assert payload["sources"] == ["openregister"]


def test_external_resolver_guard_changes_only_reference_text_not_local_context(monkeypatch):
    import rag.resolver_client as resolver_client

    seen = []

    def fake_policy(stage, *, content=None, metadata=None):
        seen.append((stage, content, dict(metadata or {})))
        return "[REDACTED]" if content == "Secret GmbH" else content

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            assert json["references"][0]["text"] == "[REDACTED]"
            assert json["references"][0]["context"] == ["Karben", "DL-2026-4711"]
            return Response()

    monkeypatch.setattr(resolver_client, "apply_policy_hook", fake_policy)
    monkeypatch.setattr(resolver_client.httpx, "AsyncClient", lambda **kwargs: Client())

    outcome = asyncio.run(resolve_search_spec(
        ResolverSettings(True, "http://127.0.0.1:8770", 5),
        {
            "entities": ["Secret GmbH"],
            "constraints": [
                {"kind": "location", "value": "Karben"},
                {"kind": "reference", "value": "DL-2026-4711"},
            ],
        },
        sources=["openregister"],
    ))

    assert outcome.error == ""
    assert outcome.request["references"][0]["text"] == "[REDACTED]"
    assert outcome.request["references"][0]["context"] == ["Karben", "DL-2026-4711"]
    assert seen[0][2]["external"] is True


def test_resolved_gateway_candidate_becomes_query_expansion():
    context = entity_context_from_resolver({
        "results": [{
            "reference": "EXAMPLE MACHINERY AG",
            "status": "resolved",
            "candidates": [{
                "source": "openregister",
                "candidate_id": "DE-TEST-1",
                "canonical": "EXAMPLE MACHINERY AG",
                "kind": "Organization",
                "aliases": ["Example Machinery"],
                "confidence": 0.99,
            }],
        }],
    })

    assert context["entities"][0]["status"] == "resolved"
    assert context["entities"][0]["entity_id"] == "openregister:DE-TEST-1"
    assert {item["value"] for item in context["elastic_phrase_expansion"]} == {
        "EXAMPLE MACHINERY AG",
        "Example Machinery",
    }


def test_ambiguous_gateway_output_never_silently_expands_query():
    context = entity_context_from_resolver({
        "results": [{
            "reference": "Example AG",
            "status": "candidates",
            "candidates": [
                {
                    "source": "openregister",
                    "candidate_id": "1",
                    "canonical": "Example AG",
                    "kind": "Organization",
                    "confidence": 0.91,
                },
                {
                    "source": "openregister",
                    "candidate_id": "2",
                    "canonical": "Example Holding AG",
                    "kind": "Organization",
                    "confidence": 0.89,
                },
            ],
        }],
    })

    assert context["entities"][0]["status"] == "ambiguous"
    assert context["elastic_phrase_expansion"] == []


def test_resolver_context_merges_with_existing_neo4j_seed_context():
    base = {
        "enabled": True,
        "entities": [{
            "mention": "John Doe",
            "status": "resolved",
            "entity_id": "neo4j-1",
        }],
        "elastic_phrase_expansion": [{
            "entity_id": "neo4j-1",
            "value": "Robert Alexander Wilke",
        }],
        "error": None,
    }
    external = {
        "enabled": True,
        "entities": [{
            "mention": "EXAMPLE MACHINERY AG",
            "status": "resolved",
            "entity_id": "openregister:1",
        }],
        "elastic_phrase_expansion": [{
            "entity_id": "openregister:1",
            "value": "EXAMPLE MACHINERY AG",
        }],
        "error": None,
    }

    merged = merge_entity_context(base, external)
    assert len(merged["entities"]) == 2
    assert len(merged["elastic_phrase_expansion"]) == 2


def test_resolver_gateway_is_disabled_in_src_even_if_requested(monkeypatch):
    monkeypatch.setenv("RESOLVER_GATEWAY_ENABLED", "true")
    settings = load_resolver_settings({
        "architecture": {"tier": "src"},
        "resolver": {"enabled": True, "url": "http://127.0.0.1:8770"},
    })
    assert settings.enabled is False


def test_existing_local_resolution_wins_over_external_duplicate():
    base = {
        "enabled": True,
        "entities": [{
            "mention": "Example AG",
            "status": "resolved",
            "entity_id": "neo4j-local",
        }],
        "elastic_phrase_expansion": [{
            "entity_id": "neo4j-local",
            "value": "Example Aktiengesellschaft",
        }],
        "error": None,
    }
    external = {
        "enabled": True,
        "entities": [{
            "mention": "Example AG",
            "status": "resolved",
            "entity_id": "openregister:external",
        }],
        "elastic_phrase_expansion": [{
            "entity_id": "openregister:external",
            "value": "Example AG",
        }],
        "error": None,
    }

    merged = merge_entity_context(base, external)
    assert [item["entity_id"] for item in merged["entities"]] == ["neo4j-local"]
    assert {item["entity_id"] for item in merged["elastic_phrase_expansion"]} == {"neo4j-local"}


def test_external_resolution_can_replace_unresolved_local_mention():
    base = {
        "enabled": True,
        "entities": [{
            "mention": "Example AG",
            "status": "unresolved",
            "entity_id": "",
        }],
        "elastic_phrase_expansion": [],
        "error": None,
    }
    external = {
        "enabled": True,
        "entities": [{
            "mention": "Example AG",
            "status": "resolved",
            "entity_id": "openregister:external",
        }],
        "elastic_phrase_expansion": [{
            "entity_id": "openregister:external",
            "value": "Example AG",
        }],
        "error": None,
    }

    merged = merge_entity_context(base, external)
    assert [item["entity_id"] for item in merged["entities"]] == ["openregister:external"]
    assert [item["entity_id"] for item in merged["elastic_phrase_expansion"]] == ["openregister:external"]


def test_search_spec_normalizes_entity_hints_for_register_model():
    from rag.search_spec import normalize_search_spec

    spec = normalize_search_spec({
        "entities": ["John Doe", "Example Capital GmbH"],
        "entity_hints": [
            {"text": "John Doe", "kind": "person"},
            {"text": "Example Capital GmbH", "kind": "organization"},
            {"text": "ignored", "kind": "unknown"},
        ],
    })
    assert spec["entity_hints"] == [
        {"text": "John Doe", "kind": "person"},
        {"text": "Example Capital GmbH", "kind": "organization"},
    ]

