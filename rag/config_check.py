"""Static configuration consistency checks for SunaQ / ERG.

This checker deliberately avoids network calls and secret-store writes. It
validates global infrastructure/capability settings together with the effective
startup-loaded SunaQ model packages.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rag.architecture_policy import ERG_FULL, architecture_tier, validate_runtime_model
from rag.config_security import validate_static_security_config
from rag.egress_policy import load_egress_policy
from rag.sunaq_models import PROFILE_CONFIG_SECTIONS, load_model_registry


@dataclass(frozen=True)
class ConfigIssue:
    level: str  # "error" or "warning"
    message: str


def _get(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _reranker_backend(cfg: dict[str, Any]) -> str:
    return str(_get(cfg, "reranker.backend", "none") or "none").strip().lower()


def _legacy_model_sections_present(cfg: dict[str, Any]) -> bool:
    return any(section in cfg for section in PROFILE_CONFIG_SECTIONS)


def _effective_model_configs(
    cfg: dict[str, Any],
    *,
    models_dir: str | Path | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], bool, list[Any]]:
    """Return the same effective models the runtime will load.

    Packaged models win even when an upgraded installation still contains old
    request-local sections in config.yaml. Only a deployment without packaged
    models falls back to the legacy single-model config path.
    """
    registry = load_model_registry(cfg, models_dir=models_dir)
    runtime_models = registry.list()
    models = [(model.model_id, model.config) for model in runtime_models]
    legacy = all(model.directory is None for model in runtime_models)
    return models, legacy, runtime_models


def check_config(
    cfg: dict[str, Any],
    *,
    models_dir: str | Path | None = None,
    web_cfg: dict[str, Any] | None = None,
) -> list[ConfigIssue]:
    """Return static configuration errors and warnings without external probes."""
    issues: list[ConfigIssue] = []

    es_enabled = _truthy(_get(cfg, "elasticsearch.enabled", True), True)
    qdrant_enabled = _truthy(_get(cfg, "qdrant.enabled", True), True)
    sync_enabled = _truthy(_get(cfg, "sync_worker.enabled", False), False)
    profile = str(_get(cfg, "deployment.profile", "default") or "default").strip().lower()
    neo4j_enabled = _truthy(_get(cfg, "neo4j.enabled", False), False)
    research_findings_enabled = _truthy(
        _get(cfg, "research_findings.enabled", False), False
    )
    deployment_mode = str(
        _get(cfg, "deployment.mode", "native") or "native"
    ).strip().lower()

    for message in validate_static_security_config(cfg, require_complete=False):
        issues.append(ConfigIssue("error", message))

    if web_cfg is not None:
        if not isinstance(web_cfg, dict):
            issues.append(ConfigIssue("error", "web configuration root must be a mapping"))
        else:
            tier = architecture_tier(cfg)
            if tier != ERG_FULL:
                external_web_flags = (
                    ("web.enabled", _truthy(_get(web_cfg, "enabled", False), False)),
                    (
                        "web.archive.enabled",
                        _truthy(_get(web_cfg, "archive.enabled", False), False),
                    ),
                    (
                        "web.archive.renderer.enabled",
                        _truthy(
                            _get(web_cfg, "archive.renderer.enabled", False),
                            False,
                        ),
                    ),
                )
                for label, enabled in external_web_flags:
                    if enabled:
                        issues.append(
                            ConfigIssue(
                                "error",
                                f"{label}=true requires architecture.tier=erg_full; "
                                f"current tier is {tier}",
                            )
                        )
            allow_insecure_nextcloud = _truthy(
                _get(cfg, "security.allow_insecure_nextcloud", False), False
            )
            nextcloud_cfg = _get(cfg, "nextcloud", {}) or {}
            archive_cfg = _get(web_cfg, "archive", {}) or {}
            canonical_nextcloud_tls = bool(
                str(nextcloud_cfg.get("ca_file") or "").strip()
                or "verify_tls" in nextcloud_cfg
            )
            if "verify_tls" in archive_cfg:
                legacy_verify = _truthy(archive_cfg.get("verify_tls"), True)
                if canonical_nextcloud_tls:
                    issues.append(
                        ConfigIssue(
                            "warning",
                            "web.archive.verify_tls is a deprecated compatibility setting and "
                            "is ignored because config.yaml nextcloud.ca_file/verify_tls is authoritative",
                        )
                    )
                elif not legacy_verify and not allow_insecure_nextcloud:
                    issues.append(
                        ConfigIssue(
                            "error",
                            "web.archive.verify_tls=false would disable TLS verification for "
                            "credential-bearing Nextcloud WebDAV; configure nextcloud.ca_file/"
                            "nextcloud.verify_tls or set security.allow_insecure_nextcloud=true "
                            "only for an explicit lab exception",
                        )
                    )
                elif not legacy_verify:
                    issues.append(
                        ConfigIssue(
                            "warning",
                            "web.archive.verify_tls=false is an explicit insecure Nextcloud "
                            "compatibility mode",
                        )
                    )

            legacy_archive_ca = str(archive_cfg.get("ca_file") or "").strip()
            if legacy_archive_ca and canonical_nextcloud_tls:
                issues.append(
                    ConfigIssue(
                        "warning",
                        "web.archive.ca_file is deprecated and ignored because config.yaml "
                        "nextcloud.ca_file/verify_tls is authoritative",
                    )
                )

            for path in (
                "search.verify_tls",
                "fetch.verify_tls",
                "relevance.verify_tls",
                "archive.renderer.verify_tls",
            ):
                value = _get(web_cfg, path, None)
                if value is not None and not _truthy(value, True):
                    issues.append(
                        ConfigIssue(
                            "warning",
                            f"web.{path}=false disables TLS certificate verification for that "
                            "outbound Web/LLM/renderer connection",
                        )
                    )

    try:
        load_egress_policy(cfg)
    except (TypeError, ValueError) as exc:
        issues.append(ConfigIssue("error", f"invalid egress policy: {exc}"))

    try:
        model_configs, legacy_model_config, runtime_models = _effective_model_configs(
            cfg, models_dir=models_dir
        )
    except Exception as exc:
        issues.append(
            ConfigIssue(
                "error",
                f"SunaQ model configuration is invalid: {type(exc).__name__}: {exc}",
            )
        )
        model_configs = [("fallback", cfg)]
        legacy_model_config = True
        runtime_models = []

    for model in runtime_models:
        for message in validate_runtime_model(cfg, model):
            issues.append(ConfigIssue("error", message))

    if not legacy_model_config:
        # capabilities/research/resolver_research are optional RC2 extensions.
        # Upgraded 0.8.6 installations keep administrator-owned model packages,
        # so newly introduced sections must remain safely defaultable instead of
        # turning an otherwise valid legacy profile into a startup error.
        required_profile_sections = PROFILE_CONFIG_SECTIONS - {"capabilities", "research", "resolver_research"}
        for model_id, model_cfg in model_configs:
            missing = sorted(
                section
                for section in required_profile_sections
                if not isinstance(model_cfg.get(section), dict)
            )
            if missing:
                issues.append(
                    ConfigIssue(
                        "error",
                        f"SunaQ model {model_id} is missing request-local sections: "
                        + ", ".join(missing),
                    )
                )

    rerankers = {
        model_id: _reranker_backend(model_cfg)
        for model_id, model_cfg in model_configs
    }
    evidence_modes = {
        model_id: str(
            _get(model_cfg, "evidence_control.mode", "off") or "off"
        ).strip().lower()
        for model_id, model_cfg in model_configs
    }
    graph_model_enabled = {
        model_id: _truthy(
            _get(model_cfg, "graph_retrieval.enabled", True),
            True,
        )
        for model_id, model_cfg in model_configs
    }
    graph_doc_enabled = neo4j_enabled and any(graph_model_enabled.values())

    policy_modes = {
        arm: str(
            _get(cfg, f"retrieval_policy.internal.{arm}", default) or default
        ).strip().lower()
        for arm, default in (
            ("files", "optional"),
            ("vector", "optional"),
            ("graph", "optional"),
        )
    }
    optional_default = str(
        _get(cfg, "retrieval_policy.optional_default", "include") or "include"
    ).strip().lower()
    web_policy = str(
        _get(cfg, "retrieval_policy.web", "planner") or "planner"
    ).strip().lower()

    if not es_enabled and not qdrant_enabled:
        issues.append(
            ConfigIssue(
                "error",
                "no document retrieval arm is enabled (Elasticsearch and Qdrant are both disabled)",
            )
        )

    for model_id, reranker in rerankers.items():
        if reranker not in {"none", "local", "tei"}:
            issues.append(
                ConfigIssue(
                    "error",
                    f"unknown reranker.backend={reranker!r} in SunaQ model {model_id}; allowed: none, local, tei",
                )
            )

    if deployment_mode not in {"native", "dockerized"}:
        issues.append(
            ConfigIssue(
                "error",
                f"unknown deployment.mode={deployment_mode!r}; allowed: native, dockerized",
            )
        )

    if deployment_mode == "dockerized" and sync_enabled:
        effective_state_db = _get(cfg, "sync.state_db", None)
        if effective_state_db in (None, ""):
            effective_state_db = _get(cfg, "state_db", "runtime/state.sqlite")
        docker_runtime_paths = (
            ("sync.state_db", str(effective_state_db or "").strip()),
            (
                "sync.embedding_deferred_file",
                str(_get(cfg, "sync.embedding_deferred_file", "runtime/embedding-deferred.jsonl") or "").strip(),
            ),
            (
                "sync.status_file",
                str(_get(cfg, "sync.status_file", "runtime/sync-status.json") or "").strip(),
            ),
        )
        for label, raw_path in docker_runtime_paths:
            normalized = raw_path.replace("\\", "/")
            if not (
                normalized.startswith("runtime/")
                or normalized.startswith("/app/runtime/")
            ):
                issues.append(
                    ConfigIssue(
                        "error",
                        f"{label} must live below runtime/ in dockerized deployment; "
                        f"configured path {raw_path!r} would not use the persistent /app/runtime mount",
                    )
                )

    for model_id, evidence_control_mode in evidence_modes.items():
        if evidence_control_mode not in {"off", "review"}:
            issues.append(
                ConfigIssue(
                    "error",
                    f"evidence_control.mode must be off or review (SunaQ model {model_id})",
                )
            )

    for arm, mode in policy_modes.items():
        if mode not in {"required", "optional", "disabled"}:
            issues.append(
                ConfigIssue(
                    "error",
                    f"retrieval_policy.internal.{arm} must be required, optional or disabled",
                )
            )
    if optional_default not in {"include", "exclude"}:
        issues.append(
            ConfigIssue(
                "error",
                "retrieval_policy.optional_default must be include or exclude",
            )
        )
    if web_policy not in {"disabled", "explicit", "planner"}:
        issues.append(
            ConfigIssue(
                "error",
                "retrieval_policy.web must be disabled, explicit or planner",
            )
        )

    capability_enabled = {
        "files": es_enabled,
        "vector": qdrant_enabled,
        "graph": graph_doc_enabled,
    }
    for arm, mode in policy_modes.items():
        if mode == "required" and not capability_enabled[arm]:
            issues.append(
                ConfigIssue(
                    "error",
                    f"retrieval_policy requires {arm}, but its backend/capability is disabled",
                )
            )
    if all(
        mode == "disabled" or not capability_enabled[arm]
        for arm, mode in policy_modes.items()
    ):
        issues.append(
            ConfigIssue(
                "error",
                "retrieval_policy leaves no default internal document retrieval arm",
            )
        )

    if sync_enabled and not qdrant_enabled:
        issues.append(
            ConfigIssue(
                "error",
                "sync_worker.enabled=true requires qdrant.enabled=true",
            )
        )

    if sync_enabled:
        missing_operational_defaults: list[str] = []
        if _get(cfg, "sync_worker.pause_check_documents", None) in (None, ""):
            missing_operational_defaults.append("sync_worker.pause_check_documents")
        if _get(cfg, "sync.status_file", None) in (None, ""):
            missing_operational_defaults.append("sync.status_file")
        if _get(cfg, "sync.embedding_deferred_file", None) in (None, ""):
            missing_operational_defaults.append("sync.embedding_deferred_file")
        if (
            _get(cfg, "sync.state_db", None) in (None, "")
            and _get(cfg, "state_db", None) in (None, "")
        ):
            missing_operational_defaults.append("sync.state_db")
        if missing_operational_defaults:
            issues.append(
                ConfigIssue(
                    "warning",
                    "sync configuration relies on runtime defaults for: "
                    + ", ".join(missing_operational_defaults)
                    + "; rerun the installer/config upgrade or materialize these keys explicitly",
                )
            )

    if qdrant_enabled:
        embedding_backend = str(_get(cfg, "embedding.backend", "") or "").strip()
        embedding_model = str(_get(cfg, "embedding.model", "") or "").strip()
        if not embedding_backend or not embedding_model:
            issues.append(
                ConfigIssue(
                    "error",
                    "Qdrant retrieval requires embedding.backend and embedding.model",
                )
            )
        if es_enabled and any(value == "none" for value in rerankers.values()):
            issues.append(
                ConfigIssue(
                    "warning",
                    "Elasticsearch + Qdrant without a reranker is supported via RRF, but final ranking quality may be lower",
                )
            )

    if sync_enabled and not qdrant_enabled:
        issues.append(
            ConfigIssue(
                "warning",
                "sync_worker.enabled=true while Qdrant is disabled; the document embedding sync is normally unnecessary in this profile",
            )
        )

    if research_findings_enabled and not neo4j_enabled:
        issues.append(
            ConfigIssue(
                "warning",
                "research_findings.enabled=true while neo4j.enabled=false; SunaQ research findings will not be persisted",
            )
        )

    for model_id, reranker in rerankers.items():
        if reranker == "tei" and not str(
            _get(
                dict(model_configs)[model_id],
                "reranker.tei_url",
                "",
            )
            or ""
        ).strip():
            issues.append(
                ConfigIssue(
                    "error",
                    f"reranker.backend=tei requires reranker.tei_url (SunaQ model {model_id})",
                )
            )

    if profile == "dockerized" and deployment_mode != "dockerized":
        issues.append(
            ConfigIssue(
                "error",
                "deployment.profile=dockerized requires deployment.mode=dockerized",
            )
        )

    return issues


def load_config_file(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a mapping")
    return cfg


def load_optional_config_file(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    cfg_path = Path(path)
    if not cfg_path.exists():
        return None
    with cfg_path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{cfg_path} root must be a mapping")
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check SunaQ / ERG configuration consistency without network access"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--web-config",
        default="web.yaml",
        help="optional web configuration file (default: web.yaml if present)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config_file(args.config)
        web_cfg = load_optional_config_file(args.web_config)
    except Exception as exc:
        print(f"ERROR: cannot load configuration: {exc}")
        return 2

    issues = check_config(cfg, web_cfg=web_cfg)
    profile = str(_get(cfg, "deployment.profile", "default") or "default")
    print(f"Config: {args.config}")
    if web_cfg is not None:
        print(f"Web config: {args.web_config}")
    print(f"Profile: {profile}")

    if not issues:
        print("OK: no static configuration issues found")
        return 0

    for issue in issues:
        print(f"{issue.level.upper()}: {issue.message}")

    return 2 if any(issue.level == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
