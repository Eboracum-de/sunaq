from types import SimpleNamespace

import yaml

from rag.architecture_policy import (
    ERG,
    SRC,
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
    cfg["retrieval_policy"]["web"] = "planner"
    cfg["research_findings"]["enabled"] = True
    errors = validate_architecture_config(cfg)
    assert "SRC requires qdrant.enabled=false" in errors
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


def test_src_blocks_erg_api_surfaces_but_keeps_seed_graph_diagnostics():
    cfg = _src_config()
    for path in (
        "/web/search",
        "/source-origin/register-chat",
        "/graph/document",
        "/graph/research-findings",
        "/graph/queue/stats",
    ):
        assert api_path_blocked(cfg, path), path
    assert not api_path_blocked(cfg, "/graph/stats")
    assert not api_path_blocked(cfg, "/search")


def test_src_allows_remote_llm_roles_but_rejects_remote_tei():
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

    assert validate_runtime_model(cfg, Model())


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


def test_erg_does_not_apply_src_restrictions():
    cfg = {"architecture": {"tier": "erg"}}
    assert validate_architecture_config(cfg) == []
    assert request_capability_error(
        cfg,
        source_scopes={"mailarchive", "chatarchive"},
        retrieval_arms={"vector", "graph"},
        web_requested=True,
    ) is None


def test_core_preset_is_valid_src_overlay():
    preset = yaml.safe_load(
        open("install/presets/core.yaml", encoding="utf-8")
    )
    merged = apply_preset({"acl": {"enabled": True}}, preset)
    assert architecture_tier(merged) == SRC
    assert validate_architecture_config(merged) == []


def test_super_light_template_is_formal_src():
    cfg = yaml.safe_load(
        open("install/super-light/config.super-light.yaml", encoding="utf-8")
    )
    assert architecture_tier(cfg) == SRC
    assert validate_architecture_config(cfg) == []
