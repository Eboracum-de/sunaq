from types import SimpleNamespace

import yaml

from rag.architecture_policy import (
    ERG,
    ERG_FULL,
    ERG_INTERNAL,
    SRC,
    allowed_resolver_sources,
    allows_external_research,
    api_path_blocked,
    architecture_tier,
    effective_retrieval_arms,
    request_capability_error,
    validate_architecture_config,
    validate_role_backends,
    validate_runtime_model,
)
from rag.config_preset import apply_preset


def _src_config():
    return {
        "architecture": {"tier": "src"},
        "acl": {"enabled": True},
        "retrieval_policy": {
            "internal": {
                "files": "required",
                "vector": "disabled",
                "graph": "disabled",
            },
            "web": "disabled",
        },
        "qdrant": {"enabled": False},
        "neo4j": {"enabled": False},
        "resolver": {"enabled": False},
        "sync_worker": {"enabled": False},
        "sync": {"graph_queue": {"enabled": False}},
        "graph_queue": {
            "enabled": False,
            "auto_enqueue_cited_documents": False,
            "worker": {"enabled": False},
        },
        "research_findings": {"enabled": False},
        "graph_indexer": {"enabled": False},
        "graph_entity_discovery": {"enabled": False},
        "graph_relation_discovery": {"enabled": False},
        "mail_metadata": {"enabled": False},
        "mail": {"enabled": False, "worker": {"enabled": False}},
        "chat_archive": {"enabled": False},
    }


def test_architecture_tier_defaults_to_erg_for_upgrade_compatibility():
    assert architecture_tier({}) == ERG


def test_src_reference_capability_set_is_valid():
    assert architecture_tier(_src_config()) == SRC
    assert validate_architecture_config(_src_config()) == []


def test_src_requires_live_acl():
    cfg = _src_config()
    cfg["acl"]["enabled"] = False
    assert "SRC requires acl.enabled=true" in validate_architecture_config(cfg)


def test_src_rejects_erg_capabilities():
    cfg = _src_config()
    cfg["qdrant"]["enabled"] = True
    cfg["neo4j"]["enabled"] = True
    cfg["resolver"]["enabled"] = True
    cfg["retrieval_policy"]["web"] = "planner"
    cfg["research_findings"]["enabled"] = True
    errors = validate_architecture_config(cfg)
    assert "SRC requires qdrant.enabled=false" in errors
    assert "SRC requires neo4j.enabled=false" in errors
    assert "SRC requires resolver.enabled=false" in errors
    assert "SRC requires retrieval_policy.web=disabled" in errors
    assert "SRC requires research_findings.enabled=false" in errors


def test_src_request_gate_and_implicit_arms_are_files_only():
    cfg = _src_config()
    assert request_capability_error(
        cfg,
        source_scopes={"documents"},
        retrieval_arms={"files"},
    ) is None
    assert effective_retrieval_arms(cfg, None) == ["files"]
    assert effective_retrieval_arms(cfg, []) == ["files"]
    assert "mailarchive" in request_capability_error(
        cfg, source_scopes={"documents", "mailarchive"}
    )
    assert "vector" in request_capability_error(cfg, retrieval_arms={"vector"})


def test_src_blocks_all_erg_graph_and_web_api_surfaces():
    cfg = _src_config()
    for path in (
        "/web/search",
        "/source-origin/register-chat",
        "/graph/document",
        "/graph/research-findings",
        "/graph/queue/stats",
        "/graph/stats",
        "/query-context",
    ):
        assert api_path_blocked(cfg, path), path
    assert not api_path_blocked(cfg, "/search")


def test_src_allows_remote_llm_roles_and_remote_tei():
    cfg = _src_config()
    assert validate_role_backends(
        cfg,
        {
            "answer": SimpleNamespace(
                remote=True,
                base_url="https://example.invalid/v1",
            ),
            "verifier": SimpleNamespace(
                remote=True,
                base_url="https://api.openai.com/v1",
            ),
        },
    ) == []
    assert validate_role_backends(
        cfg,
        {
            "answer": SimpleNamespace(
                remote=False,
                base_url="http://10.0.0.5:11434",
            )
        },
    ) == []

    class Model:
        model_id = "remote-tei"

        def section(self, name):
            if name == "retrieval_planner":
                return {"max_retrieval_rounds": 1}
            if name == "reranker":
                return {
                    "backend": "tei",
                    "tei_url": "https://reranker.example.invalid",
                }
            return {}

    assert validate_runtime_model(cfg, Model()) == []


def test_src_rejects_multiple_retrieval_rounds():
    cfg = _src_config()

    class Model:
        model_id = "multi"

        def section(self, name):
            if name == "retrieval_planner":
                return {"max_retrieval_rounds": 2}
            return {}

    assert "one retrieval round" in validate_runtime_model(cfg, Model())[0]


def test_src_validates_effective_retrieval_rounds_from_environment(monkeypatch):
    cfg = _src_config()

    class Model:
        model_id = "legacy-rounds"
        config = {"retrieval_planner": {}}

        def section(self, name):
            return self.config.get(name, {})

    monkeypatch.setenv("MAX_RETRIEVAL_ROUNDS", "2")
    assert "at most one retrieval round" in validate_runtime_model(cfg, Model())[0]

    Model.config = {"retrieval_planner": {"max_retrieval_rounds": 1}}
    assert validate_runtime_model(cfg, Model()) == []


def test_legacy_erg_alias_normalizes_to_erg_full():
    cfg = {"architecture": {"tier": "erg"}}
    assert architecture_tier(cfg) == ERG_FULL
    assert validate_architecture_config(cfg) == []
    assert allows_external_research(cfg) is True
    assert request_capability_error(
        cfg,
        source_scopes={"mailarchive", "chatarchive"},
        retrieval_arms={"vector", "graph"},
        web_requested=True,
    ) is None


def test_erg_internal_allows_local_derived_state_but_blocks_external_research():
    cfg = {
        "architecture": {"tier": "erg_internal"},
        "retrieval_policy": {
            "internal": {"files": "required", "vector": "optional", "graph": "optional"},
            "web": "disabled",
        },
        "qdrant": {"enabled": True},
        "neo4j": {"enabled": True},
        "resolver": {"enabled": True},
    }
    assert architecture_tier(cfg) == ERG_INTERNAL
    assert validate_architecture_config(cfg) == []
    assert allows_external_research(cfg) is False
    assert request_capability_error(
        cfg,
        retrieval_arms={"vector", "graph"},
        web_requested=False,
    ) is None
    assert "ERG Internal" in request_capability_error(cfg, web_requested=True)
    assert allowed_resolver_sources(
        cfg, ["neo4j", "openregister", "openplz"]
    ) == ("neo4j",)
    assert api_path_blocked(cfg, "/web/search")
    assert not api_path_blocked(cfg, "/graph/stats")


def test_erg_internal_rejects_enabled_web_policy():
    cfg = {
        "architecture": {"tier": "erg_internal"},
        "retrieval_policy": {"web": "explicit"},
    }
    assert "ERG Internal requires retrieval_policy.web=disabled" in validate_architecture_config(cfg)


def test_erg_internal_rejects_external_acquisition_capabilities():
    cfg = {
        "architecture": {"tier": "erg_internal"},
        "retrieval_policy": {"web": "disabled"},
        "mail": {"enabled": True, "worker": {"enabled": True}},
        "delegated_research": {"enabled": True},
    }
    errors = validate_architecture_config(cfg)
    assert "ERG Internal requires mail.enabled=false" in errors
    assert "ERG Internal requires mail.worker.enabled=false" in errors
    assert "ERG Internal requires delegated_research.enabled=false" in errors


def test_erg_full_allows_external_resolver_sources():
    cfg = {"architecture": {"tier": "erg_full"}}
    assert architecture_tier(cfg) == ERG_FULL
    assert allowed_resolver_sources(
        cfg, ["neo4j", "openregister", "openplz"]
    ) == ("neo4j", "openplz", "openregister")


def test_erg_internal_rejects_model_bound_external_adapters():
    cfg = {"architecture": {"tier": "erg_internal"}}

    class Model:
        model_id = "external-internal"

        def section(self, name):
            if name == "capabilities":
                return {"web": True}
            if name == "research":
                return {"web_backend": "brave-search"}
            if name == "resolver_research":
                return {"sources": ["neo4j", "openregister"]}
            return {}

    errors = validate_runtime_model(cfg, Model())
    assert any("Web capability" in item for item in errors)
    assert any("external Web backends" in item for item in errors)
    assert any("openregister" in item for item in errors)


def test_core_preset_is_valid_src_overlay():
    preset = yaml.safe_load(
        open("install/presets/core.yaml", encoding="utf-8")
    )
    merged = apply_preset({"acl": {"enabled": True}}, preset)
    assert architecture_tier(merged) == SRC
    assert validate_architecture_config(merged) == []


def test_workgroup_and_full_presets_map_to_new_erg_tiers_with_qdrant_bundle():
    workgroup = yaml.safe_load(open("install/presets/workgroup.yaml", encoding="utf-8"))
    full = yaml.safe_load(open("install/presets/full.yaml", encoding="utf-8"))

    for preset, tier in ((workgroup, ERG_INTERNAL), (full, ERG_FULL)):
        # Named ERG presets are atomic recommended bundles. They include the
        # vector capability independently of Standard vs Dockerized packaging.
        standard_cfg = apply_preset(
            {"acl": {"enabled": True}, "qdrant": {"enabled": True}},
            preset,
        )
        dockerized_cfg = apply_preset(
            {"acl": {"enabled": True}, "qdrant": {"enabled": False}},
            preset,
        )

        for cfg in (standard_cfg, dockerized_cfg):
            assert architecture_tier(cfg) == tier
            assert validate_architecture_config(cfg) == []
            assert cfg["retrieval_policy"]["internal"] == {
                "files": "required",
                "vector": "optional",
                "graph": "disabled",
            }
            assert cfg["qdrant"]["enabled"] is True
            assert cfg["sync_worker"]["enabled"] is True

        assert preset["qdrant"]["enabled"] is True

    assert workgroup["mail"]["enabled"] is False
    assert workgroup["mail"]["worker"]["enabled"] is False
    assert full["mail"]["enabled"] is True
    assert full["mail"]["worker"]["enabled"] is True


def test_dockerized_template_is_formal_src():
    cfg = yaml.safe_load(
        open("install/dockerized/config.dockerized.yaml", encoding="utf-8")
    )
    assert architecture_tier(cfg) == SRC
    assert cfg["neo4j"]["enabled"] is False
    assert validate_architecture_config(cfg) == []
