from pathlib import Path
import shutil

import yaml

from rag.config_check import check_config
from rag.config_preset import apply_preset


ROOT = Path(__file__).resolve().parent.parent
LEGACY_MODELS_DIR = ROOT / "tests" / "__no_sunaq_models__"


def messages(cfg):
    # These small synthetic configs intentionally exercise the compatibility
    # single-model path instead of loading the repository's shipped models.
    return [
        (issue.level, issue.message)
        for issue in check_config(cfg, models_dir=LEGACY_MODELS_DIR)
    ]


def test_rejects_no_document_retrieval_arm():
    found = messages({"elasticsearch": {"enabled": False}, "qdrant": {"enabled": False}, "reranker": {"backend": "none"}})
    assert any(level == "error" and "no document retrieval arm" in message for level, message in found)


def test_hybrid_without_reranker_is_warning_not_error():
    cfg = {
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "qwen3-embedding:4b"},
        "reranker": {"backend": "none"},
    }
    found = messages(cfg)
    assert any(level == "warning" and "supported via RRF" in message for level, message in found)
    assert not any(level == "error" for level, _ in found)


def test_dockerized_profile_is_consistent():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": False},
        "graph_retrieval": {"enabled": False},
        "graph_indexer": {"enabled": False},
        "graph_entity_discovery": {"enabled": False},
        "graph_relation_discovery": {"enabled": False},
    }
    assert messages(cfg) == []


def test_dockerized_sync_state_must_use_runtime_mount():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "x"},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": True},
        "sync": {
            "state_db": "state.sqlite",
            "embedding_deferred_file": "embedding-deferred.jsonl",
            "status_file": "sync-status.json",
        },
    }
    found = messages(cfg)
    assert any(
        level == "error" and "sync.state_db must live below runtime/" in message
        for level, message in found
    )
    assert any(
        level == "error" and "sync.embedding_deferred_file must live below runtime/" in message
        for level, message in found
    )
    assert any(
        level == "error" and "sync.status_file must live below runtime/" in message
        for level, message in found
    )


def test_dockerized_legacy_top_level_state_db_is_rejected():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "x"},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": True},
        "state_db": "state.sqlite",
        "sync": {
            "embedding_deferred_file": "runtime/embedding-deferred.jsonl",
            "status_file": "runtime/sync-status.json",
        },
    }
    found = messages(cfg)
    assert any(
        level == "error"
        and "sync.state_db must live below runtime/" in message
        and "state.sqlite" in message
        for level, message in found
    )


def test_dockerized_sync_runtime_paths_are_accepted():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "x"},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": True},
        "sync": {
            "state_db": "runtime/state.sqlite",
            "embedding_deferred_file": "runtime/embedding-deferred.jsonl",
            "status_file": "runtime/sync-status.json",
        },
    }
    found = messages(cfg)
    assert not any(
        level == "error" and "must live below runtime/" in message
        for level, message in found
    )


def test_dockerized_does_not_impose_capability_blacklist():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "x"},
        "reranker": {"backend": "local"},
        "sync_worker": {"enabled": True},
    }
    found = messages(cfg)
    assert not any(level == "error" for level, _ in found)


def test_sync_worker_requires_qdrant_independent_of_deployment():
    cfg = {
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": True},
    }
    found = messages(cfg)
    assert any(
        level == "error" and "sync_worker.enabled=true requires qdrant.enabled=true" in message
        for level, message in found
    )


def test_tei_requires_url():
    found = messages({
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "tei", "tei_url": ""},
    })
    assert any(level == "error" and "requires reranker.tei_url" in message for level, message in found)


def test_research_findings_without_neo4j_is_warning():
    found = messages({
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "neo4j": {"enabled": False},
        "research_findings": {"enabled": True},
    })
    assert any(level == "warning" and "will not be persisted" in message for level, message in found)


def test_required_policy_arm_must_have_enabled_capability():
    found = messages({
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "neo4j": {"enabled": False},
        "reranker": {"backend": "none"},
        "retrieval_policy": {
            "internal": {"files": "required", "vector": "required", "graph": "disabled"}
        },
    })
    assert any(level == "error" and "requires vector" in message for level, message in found)


def test_policy_may_disable_installed_optional_capability():
    found = messages({
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": True},
        "embedding": {"backend": "ollama", "model": "x"},
        "neo4j": {"enabled": False},
        "reranker": {"backend": "local"},
        "retrieval_policy": {
            "internal": {"files": "required", "vector": "disabled", "graph": "disabled"},
            "optional_default": "include",
            "web": "explicit",
        },
    })
    assert not any(level == "error" for level, _ in found)


def test_rejects_unknown_evidence_control_mode():
    found = messages({
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "evidence_control": {"mode": "automatic"},
    })
    assert any(
        level == "error" and "evidence_control.mode must be off or review" in message
        for level, message in found
    )



def test_rejects_http_nextcloud_without_explicit_insecure_exception():
    found = messages({
        "acl": {"enabled": True, "identity_mode": "credential_store"},
        "auth": {
            "credential_store": "runtime/users.sqlite",
            "nextcloud_login_flow_enabled": True,
        },
        "nextcloud": {
            "base_url": "http://nextcloud.example.invalid",
            "verify_tls": True,
        },
        "security": {"allow_insecure_nextcloud": False},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
    })
    assert any(
        level == "error" and "nextcloud.base_url must use HTTPS" in message
        for level, message in found
    )


def test_allows_http_nextcloud_only_with_explicit_lab_exception():
    found = messages({
        "acl": {"enabled": True, "identity_mode": "credential_store"},
        "auth": {
            "credential_store": "runtime/users.sqlite",
            "nextcloud_login_flow_enabled": True,
        },
        "nextcloud": {
            "base_url": "http://nextcloud.example.invalid",
            "verify_tls": True,
        },
        "security": {"allow_insecure_nextcloud": True},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
    })
    assert not any(level == "error" and "nextcloud.base_url" in message for level, message in found)


def test_upgraded_legacy_model_packages_may_omit_new_resolver_research_section(tmp_path):
    models_dir = tmp_path / "models"
    shutil.copytree(ROOT / "models", models_dir)
    for name in ("standard", "thorough", "deep"):
        path = models_dir / name / "profile.yaml"
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile.pop("resolver_research", None)
        path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    found = [(issue.level, issue.message) for issue in check_config(cfg, models_dir=models_dir)]
    assert not any(
        level == "error" and "resolver_research" in message
        for level, message in found
    )


def test_shipped_standard_config_validates_effective_sunaq_models():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    found = [(issue.level, issue.message) for issue in check_config(cfg)]
    assert not any(level == "error" for level, _ in found)


def test_shipped_dockerized_config_validates_model_deployment_overrides():
    cfg = yaml.safe_load(
        (ROOT / "install" / "dockerized" / "config.dockerized.yaml").read_text(
            encoding="utf-8"
        )
    )
    found = [(issue.level, issue.message) for issue in check_config(cfg)]
    assert found == []


def test_web_acquisition_requires_erg_full():
    src = yaml.safe_load(
        (ROOT / "install" / "dockerized" / "config.dockerized.yaml").read_text(
            encoding="utf-8"
        )
    )
    workgroup = yaml.safe_load(
        (ROOT / "install" / "presets" / "workgroup.yaml").read_text(encoding="utf-8")
    )
    full = yaml.safe_load(
        (ROOT / "install" / "presets" / "full.yaml").read_text(encoding="utf-8")
    )
    web_cfg = {
        "enabled": False,
        "archive": {
            "enabled": False,
            "renderer": {"enabled": True},
        },
    }

    for cfg in (src, apply_preset(src, workgroup)):
        found = [
            (issue.level, issue.message)
            for issue in check_config(cfg, web_cfg=web_cfg)
        ]
        assert any(
            level == "error"
            and "web.archive.renderer.enabled=true requires architecture.tier=erg_full"
            in message
            for level, message in found
        )

    erg_full = apply_preset(src, full)
    found = [
        (issue.level, issue.message)
        for issue in check_config(erg_full, web_cfg=web_cfg)
    ]
    assert not any(
        "requires architecture.tier=erg_full" in message
        for _, message in found
    )


def test_web_archive_legacy_tls_false_is_error_when_it_is_effective():
    cfg = {
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "security": {"allow_insecure_nextcloud": False},
    }
    web_cfg = {"archive": {"verify_tls": False}}
    found = [
        (issue.level, issue.message)
        for issue in check_config(
            cfg,
            models_dir=LEGACY_MODELS_DIR,
            web_cfg=web_cfg,
        )
    ]
    assert any(
        level == "error"
        and "web.archive.verify_tls=false" in message
        and "credential-bearing Nextcloud WebDAV" in message
        for level, message in found
    )


def test_web_archive_legacy_tls_false_is_reported_as_ignored_when_canonical_tls_exists():
    cfg = {
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
        "nextcloud": {"verify_tls": True},
    }
    web_cfg = {"archive": {"verify_tls": False}}
    found = [
        (issue.level, issue.message)
        for issue in check_config(
            cfg,
            models_dir=LEGACY_MODELS_DIR,
            web_cfg=web_cfg,
        )
    ]
    assert any(
        level == "warning"
        and "deprecated compatibility setting" in message
        and "ignored" in message
        for level, message in found
    )
    assert not any(
        level == "error" and "web.archive.verify_tls=false" in message
        for level, message in found
    )


def test_web_outbound_tls_false_is_visible_as_warning():
    cfg = {
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "reranker": {"backend": "none"},
    }
    web_cfg = {
        "search": {"verify_tls": False},
        "fetch": {"verify_tls": False},
    }
    found = [
        (issue.level, issue.message)
        for issue in check_config(
            cfg,
            models_dir=LEGACY_MODELS_DIR,
            web_cfg=web_cfg,
        )
    ]
    assert any(level == "warning" and "web.search.verify_tls=false" in message for level, message in found)
    assert any(level == "warning" and "web.fetch.verify_tls=false" in message for level, message in found)


def test_dockerized_erg_internal_allows_neo4j_without_document_graph_retrieval():
    cfg = {
        "architecture": {"tier": "erg_internal"},
        "deployment": {"profile": "dockerized", "mode": "dockerized"},
        "elasticsearch": {"enabled": True},
        "qdrant": {"enabled": False},
        "neo4j": {"enabled": True},
        "reranker": {"backend": "none"},
        "sync_worker": {"enabled": False},
        "graph_retrieval": {"enabled": False},
        "graph_indexer": {"enabled": False},
        "graph_entity_discovery": {"enabled": False},
        "graph_relation_discovery": {"enabled": False},
        "research_findings": {"enabled": True},
    }
    found = messages(cfg)
    assert not any(level == "error" and "neo4j" in message.lower() for level, message in found)
    assert not any(level == "warning" and "will not be persisted" in message for level, message in found)


def test_shipped_dockerized_erg_presets_pass_full_static_config_check():
    base = yaml.safe_load(
        (ROOT / "install" / "dockerized" / "config.dockerized.yaml").read_text(
            encoding="utf-8"
        )
    )
    for name, tier in (("workgroup", "erg_internal"), ("full", "erg_full")):
        preset = yaml.safe_load(
            (ROOT / "install" / "presets" / f"{name}.yaml").read_text(encoding="utf-8")
        )
        cfg = apply_preset(base, preset)
        assert cfg["architecture"]["tier"] == tier
        assert cfg["neo4j"]["enabled"] is True
        found = [(issue.level, issue.message) for issue in check_config(cfg)]
        assert not any(level == "error" for level, _ in found), found
