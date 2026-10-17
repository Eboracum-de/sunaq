"""Startup-loaded SunaQ model/profile registry.

A SunaQ model is the user-visible OpenAI-compatible model exposed by the
provider. It is deliberately distinct from the underlying LLMs used for
planner/verifier/evidence/answer roles.

Each model lives in::

    models/<directory>/profile.yaml
    models/<directory>/prompts/*

Profiles may override request-local retrieval/answer settings and LLM role
routing. Prompt files are read once at startup. Runtime requests only select an
already validated RuntimeModel; they never read YAML or prompt files.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Iterable

import yaml

from rag.architecture_policy import architecture_tier, normalize_architecture_tier


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

# Only request-local/search behaviour belongs in a SunaQ model. Background
# workers (graph queue/discovery/indexing, mail sync, credentials, TLS, etc.)
# remain global in config.yaml.
PROFILE_CONFIG_SECTIONS = frozenset(
    {
        "search",
        "retrieval_planner",
        "evidence_control",
        "retrieval_signal",
        "graph_retrieval",
        "reranker",
        "context_enrichment",
        "answer_context",
        "entity_resolution",
        "resolver_research",
        "capabilities",
        "research",
    }
)

ROLE_NAMES = frozenset({"default", "planner", "verifier", "evidence", "answer"})


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = deepcopy(value)
    return result


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _component_enabled(cfg: dict[str, Any], section: str, *, default: bool = False) -> bool:
    value = cfg.get(section) or {}
    if not isinstance(value, dict):
        return default
    return _truthy(value.get("enabled"), default)


def _clean_model_id(value: Any, *, field: str = "id") -> str:
    model_id = str(value or "").strip()
    if not _MODEL_ID_RE.fullmatch(model_id):
        raise RuntimeError(
            f"Invalid SunaQ model {field} {model_id!r}; expected letters/digits and ._-"
        )
    return model_id


def _safe_prompt_path(model_dir: Path, raw_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise RuntimeError(f"Empty prompt path in {model_dir / 'profile.yaml'}")
    relative = Path(value)
    if relative.is_absolute():
        raise RuntimeError(f"Prompt path must be relative to the model directory: {value}")
    # A short filename means models/<model>/prompts/<filename>.
    candidate = model_dir / (relative if len(relative.parts) > 1 else Path("prompts") / relative)
    root = model_dir.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Prompt path escapes model directory: {value}") from exc
    return resolved


@dataclass(frozen=True)
class RuntimeModel:
    model_id: str
    name: str
    description: str
    config: dict[str, Any]
    roles: dict[str, dict[str, Any]]
    prompts: dict[str, str]
    aliases: tuple[str, ...]
    is_default: bool
    order: int = 100
    directory: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        value = self.config.get(name) or {}
        return dict(value) if isinstance(value, dict) else {}

    def public_info(self) -> dict[str, Any]:
        return {
            "id": self.model_id,
            "object": "model",
            "created": 0,
            "owned_by": "sunaq",
            "name": self.name,
            "description": self.description,
        }


class ModelRegistry:
    def __init__(
        self,
        models: Iterable[RuntimeModel],
        default_model_id: str,
        compatibility_aliases: dict[str, str] | None = None,
    ):
        by_id: dict[str, RuntimeModel] = {}
        aliases: dict[str, str] = {}
        for model in models:
            if model.model_id in by_id:
                raise RuntimeError(f"Duplicate SunaQ model id: {model.model_id}")
            by_id[model.model_id] = model
            for alias in model.aliases:
                if alias in by_id or alias in aliases:
                    raise RuntimeError(f"Duplicate SunaQ model alias: {alias}")
                aliases[alias] = model.model_id
        collisions = sorted(set(aliases) & set(by_id))
        if collisions:
            raise RuntimeError(
                "SunaQ model alias collides with model id: " + ", ".join(collisions)
            )
        if not by_id:
            raise RuntimeError("No SunaQ models are configured")
        resolved_default = aliases.get(default_model_id, default_model_id)
        if resolved_default not in by_id:
            raise RuntimeError(f"Unknown default SunaQ model: {default_model_id}")
        for raw_alias, raw_target in (compatibility_aliases or {}).items():
            alias = _clean_model_id(raw_alias, field="compatibility alias")
            target = aliases.get(str(raw_target), str(raw_target))
            if target not in by_id:
                raise RuntimeError(
                    f"Compatibility alias {alias!r} targets unknown SunaQ model {raw_target!r}"
                )
            if alias in by_id:
                if alias != target:
                    raise RuntimeError(f"Compatibility alias collides with SunaQ model id: {alias}")
                continue
            existing = aliases.get(alias)
            if existing is not None and existing != target:
                raise RuntimeError(f"Compatibility alias collides with SunaQ model alias: {alias}")
            aliases[alias] = target

        self._models = by_id
        self._aliases = aliases
        self.default_model_id = resolved_default

    def get(self, model_id: str | None = None) -> RuntimeModel:
        requested = str(model_id or "").strip() or self.default_model_id
        requested = self._aliases.get(requested, requested)
        try:
            return self._models[requested]
        except KeyError as exc:
            raise KeyError(f"Unknown SunaQ model: {model_id}") from exc

    def contains(self, model_id: str) -> bool:
        try:
            self.get(model_id)
            return True
        except KeyError:
            return False

    def list(self) -> list[RuntimeModel]:
        return list(self._models.values())

    def public_models(self) -> list[dict[str, Any]]:
        return [model.public_info() for model in self.list()]

    def canonical_id(self, model_id: str | None) -> str:
        return self.get(model_id).model_id


def _legacy_model(base_config: dict[str, Any]) -> RuntimeModel:
    model_id = _clean_model_id(
        os.getenv("PROVIDER_MODEL_ID", "sunaq"),
        field="legacy id",
    )
    return RuntimeModel(
        model_id=model_id,
        name=os.getenv("PROVIDER_MODEL_NAME", "SunaQ"),
        description="Compatibility model using the global config.yaml settings.",
        config=deepcopy(base_config),
        roles={},
        prompts={},
        aliases=(),
        is_default=True,
        order=100,
        directory=None,
    )


def load_model_registry(
    base_config: dict[str, Any],
    *,
    models_dir: str | Path | None = None,
) -> ModelRegistry:
    root = Path(
        models_dir
        or os.getenv("SUNAQ_MODELS_DIR", "")
        or (Path(__file__).resolve().parent.parent / "models")
    )
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root

    models: list[RuntimeModel] = []
    if root.is_dir():
        for model_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name):
            profile_path = model_dir / "profile.yaml"
            if not profile_path.is_file():
                continue
            try:
                raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                raise RuntimeError(f"Cannot read SunaQ profile {profile_path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise RuntimeError(f"SunaQ profile must be a YAML mapping: {profile_path}")
            if raw.get("enabled", True) is False:
                continue

            model_id = _clean_model_id(raw.get("id") or model_dir.name)

            tiers_raw = raw.get("architecture_tiers") or []
            if isinstance(tiers_raw, str):
                tiers_raw = [tiers_raw]
            if not isinstance(tiers_raw, list):
                raise RuntimeError(f"architecture_tiers must be a list in {profile_path}")
            allowed_tiers = {
                normalize_architecture_tier(value)
                for value in tiers_raw
                if str(value or "").strip()
            }
            current_tier = architecture_tier(base_config)
            if allowed_tiers and current_tier not in allowed_tiers:
                continue

            resolver_requirement = raw.get("resolver_research") or {}
            if (
                isinstance(resolver_requirement, dict)
                and _truthy(resolver_requirement.get("enabled"), False)
                and not _component_enabled(base_config, "resolver")
            ):
                continue

            name = str(raw.get("name") or raw.get("label") or model_id).strip() or model_id
            description = str(raw.get("description") or "").strip()
            aliases_raw = raw.get("aliases") or []
            if isinstance(aliases_raw, str):
                aliases_raw = [aliases_raw]
            if not isinstance(aliases_raw, list):
                raise RuntimeError(f"aliases must be a list in {profile_path}")
            aliases = tuple(_clean_model_id(value, field="alias") for value in aliases_raw)

            # A packaged SunaQ model owns all request-local behaviour.
            # Do not silently inherit the corresponding legacy sections from
            # config.yaml; otherwise one global edit could mutate every model.
            # Global infrastructure/security/background-worker configuration is
            # still copied into the runtime model unchanged.
            model_config = deepcopy(base_config)
            for section in PROFILE_CONFIG_SECTIONS:
                model_config.pop(section, None)

            for section in PROFILE_CONFIG_SECTIONS:
                if section not in raw:
                    continue
                override = raw.get(section)
                if not isinstance(override, dict):
                    raise RuntimeError(f"{section} must be a mapping in {profile_path}")
                model_config[section] = deepcopy(override)

            # Validate model-local capability and backend choices at startup.
            # Provider-side capability checks still intersect these with global
            # configuration and per-user entitlements on every request.
            caps = model_config.get("capabilities") or {}
            research = model_config.get("research") or {}
            allowed_caps = {"documents", "mailarchive", "chatarchive", "web"}
            if set(caps) - allowed_caps or any(not isinstance(v, bool) for v in caps.values()):
                raise RuntimeError(f"Invalid capabilities in {profile_path}")
            if set(research) - {"web_backend"}:
                raise RuntimeError(f"Unknown research fields in {profile_path}")
            selected_backend = research.get("web_backend")
            if selected_backend is not None and (
                not isinstance(selected_backend, str) or
                (not selected_backend.strip()) or
                len(selected_backend) > 64
            ):
                raise RuntimeError(f"Invalid research.web_backend in {profile_path}")

            # Upgrade bridge: the shipped default profile may explicitly opt in
            # to old inline config.yaml sections. This preserves an existing
            # 0.8.5 installation's tuned Standard behaviour without letting
            # those legacy values leak into other SunaQ models. Fresh 0.8.6
            # configs contain none of these sections, so the bridge is inert.
            if bool(raw.get("legacy_config_overlay", False)):
                for section in PROFILE_CONFIG_SECTIONS:
                    legacy = base_config.get(section)
                    if not isinstance(legacy, dict):
                        continue
                    current = model_config.get(section) or {}
                    if not isinstance(current, dict):
                        current = {}
                    model_config[section] = _deep_merge(dict(current), legacy)

            deployment_profile = str(
                ((base_config.get("deployment") or {}).get("profile") or "")
            ).strip().lower()
            deployment_overrides = raw.get("deployment_overrides") or {}
            if not isinstance(deployment_overrides, dict):
                raise RuntimeError(
                    f"deployment_overrides must be a mapping in {profile_path}"
                )
            selected_override: dict[str, Any] = {}
            if deployment_profile:
                selected_override = deployment_overrides.get(deployment_profile) or {}
                # Narrow packaging-rename bridge. Installer reruns migrate
                # site-owned model profiles from super-light -> dockerized, but
                # failed/rolled-back upgrades must remain runnable while config
                # and model files momentarily carry opposite names.
                if not selected_override and deployment_profile == "dockerized":
                    selected_override = deployment_overrides.get("super-light") or {}
                elif not selected_override and deployment_profile == "super-light":
                    selected_override = deployment_overrides.get("dockerized") or {}
            if not isinstance(selected_override, dict):
                raise RuntimeError(
                    f"deployment override {deployment_profile!r} must be a mapping in {profile_path}"
                )
            for section, override in selected_override.items():
                section_name = str(section or "").strip()
                if section_name not in PROFILE_CONFIG_SECTIONS:
                    raise RuntimeError(
                        f"Unsupported deployment override section {section_name!r} in {profile_path}"
                    )
                if not isinstance(override, dict):
                    raise RuntimeError(
                        f"Deployment override {section_name} must be a mapping in {profile_path}"
                    )
                current = model_config.get(section_name) or {}
                if not isinstance(current, dict):
                    current = {}
                model_config[section_name] = _deep_merge(dict(current), override)


            roles_raw = raw.get("roles") or {}
            if not isinstance(roles_raw, dict):
                raise RuntimeError(f"roles must be a mapping in {profile_path}")
            roles: dict[str, dict[str, Any]] = {}
            for role, role_config in roles_raw.items():
                role_name = str(role or "").strip().lower()
                if role_name not in ROLE_NAMES:
                    raise RuntimeError(f"Unknown LLM role {role!r} in {profile_path}")
                if not isinstance(role_config, dict):
                    raise RuntimeError(f"Role {role_name} must be a mapping in {profile_path}")
                roles[role_name] = deepcopy(role_config)

            prompts_raw = raw.get("prompts") or {}
            if not isinstance(prompts_raw, dict):
                raise RuntimeError(f"prompts must be a mapping in {profile_path}")
            prompts: dict[str, str] = {}
            for key, prompt_path in prompts_raw.items():
                prompt_key = str(key or "").strip().lower()
                if not prompt_key:
                    raise RuntimeError(f"Empty prompt key in {profile_path}")
                path = _safe_prompt_path(model_dir, str(prompt_path))
                try:
                    prompts[prompt_key] = path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise RuntimeError(
                        f"Cannot read SunaQ prompt {prompt_key!r} from {path}: {exc}"
                    ) from exc

            models.append(
                RuntimeModel(
                    model_id=model_id,
                    name=name,
                    description=description,
                    config=model_config,
                    roles=roles,
                    prompts=prompts,
                    aliases=aliases,
                    is_default=bool(raw.get("default", False)),
                    order=int(raw.get("order", 100) or 100),
                    directory=model_dir,
                )
            )

    if not models:
        legacy = _legacy_model(base_config)
        return ModelRegistry([legacy], legacy.model_id)

    models.sort(key=lambda model: (model.order, model.name.casefold(), model.model_id))

    defaults = [model.model_id for model in models if model.is_default]
    env_default = str(os.getenv("SUNAQ_DEFAULT_MODEL", "") or "").strip()
    if env_default:
        default_id = _clean_model_id(env_default, field="default id")
    elif len(defaults) == 1:
        default_id = defaults[0]
    elif len(defaults) > 1:
        raise RuntimeError(f"Multiple default SunaQ models configured: {', '.join(defaults)}")
    else:
        default_id = models[0].model_id

    legacy_provider_id = str(os.getenv("PROVIDER_MODEL_ID", "") or "").strip()
    compatibility_aliases = (
        {legacy_provider_id: default_id}
        if legacy_provider_id
        else {}
    )
    return ModelRegistry(
        models,
        default_id,
        compatibility_aliases=compatibility_aliases,
    )
