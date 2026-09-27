"""Safe plain-text SunaQ capability preset application.

Presets are YAML mappings merged into config.yaml. They are configuration only:
no shell evaluation, templating, command substitution, environment mutation or
secret interpolation is performed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from rag.architecture_policy import validate_architecture_config


FORBIDDEN_TOP_LEVEL = frozenset({
    "secrets",
    "credentials",
})


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return value


def apply_preset(
    config: dict[str, Any],
    preset: dict[str, Any],
) -> dict[str, Any]:
    forbidden = sorted(set(preset) & FORBIDDEN_TOP_LEVEL)
    if forbidden:
        raise ValueError(
            "preset contains forbidden secret-bearing top-level keys: "
            + ", ".join(forbidden)
        )
    merged = _merge(config, preset)
    errors = validate_architecture_config(merged)
    if errors:
        raise ValueError("invalid architecture preset: " + "; ".join(errors))
    return merged


def apply_preset_file(
    config_path: str | Path,
    preset_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> Path:
    config_file = Path(config_path)
    target = Path(output_path) if output_path is not None else config_file
    merged = apply_preset(
        load_yaml_mapping(config_file),
        load_yaml_mapping(preset_path),
    )
    rendered = yaml.safe_dump(
        merged,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    target.write_text(rendered, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a safe YAML capability preset to SunaQ config.yaml."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--preset")
    parser.add_argument("--output")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the current config against architecture-tier invariants.",
    )
    args = parser.parse_args()
    if args.validate_only:
        cfg = load_yaml_mapping(args.config)
        errors = validate_architecture_config(cfg)
        if errors:
            raise SystemExit(
                "invalid architecture configuration: " + "; ".join(errors)
            )
        print(args.config)
        return 0
    if not args.preset:
        parser.error("--preset is required unless --validate-only is used")
    target = apply_preset_file(
        args.config,
        args.preset,
        output_path=args.output,
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
