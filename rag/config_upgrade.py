"""Non-destructive SunaQ config upgrade migrations.

This module materializes safe operational defaults required by newer runtime
code while preserving administrator policy choices. It deliberately does not
enable optional capabilities.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"{section}:" and not line.startswith((" ", "\t")):
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith((" ", "\t")):
            end = index
            break
    return start, end


def _ensure_section(lines: list[str], section: str) -> tuple[int, int]:
    bounds = _section_bounds(lines, section)
    if bounds is not None:
        return bounds
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{section}:")
    return len(lines) - 1, len(lines)


def _nested_key_line(lines: list[str], section: str, key: str) -> int | None:
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return None
    start, end = bounds
    prefix = f"  {key}:"
    for index in range(start + 1, end):
        if lines[index].startswith(prefix):
            return index
    return None


def _ensure_nested_key(
    lines: list[str],
    section: str,
    key: str,
    rendered_value: str,
) -> bool:
    if _nested_key_line(lines, section, key) is not None:
        return False
    start, end = _ensure_section(lines, section)
    # Recompute because creating the section may have changed list length.
    start, end = _section_bounds(lines, section) or (start, end)
    lines.insert(end, f"  {key}: {rendered_value}")
    return True


def _replace_nested_exact(
    lines: list[str],
    section: str,
    key: str,
    old_values: set[str],
    rendered_value: str,
) -> bool:
    index = _nested_key_line(lines, section, key)
    if index is None:
        return False
    raw = lines[index].split(":", 1)[1].strip().strip("\"'")
    if raw not in old_values:
        return False
    lines[index] = f"  {key}: {rendered_value}"
    return True


def _remove_top_level_key(lines: list[str], key: str) -> str | None:
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            value = line.split(":", 1)[1].strip().strip("\"'")
            del lines[index]
            return value
    return None


def upgrade_text(text: str, *, deployment_mode: str | None = None) -> tuple[str, list[str]]:
    original_had_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    parsed = yaml.safe_load(text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("config root must be a mapping")

    mode = str(
        deployment_mode
        or ((parsed.get("deployment") or {}).get("mode"))
        or "native"
    ).strip().lower()
    changes: list[str] = []

    dossier_writer = parsed.get("dossier_writer")
    if "dossier_writer" not in parsed or isinstance(dossier_writer, dict):
        dossier_defaults = {
            "enabled": "true",
            "database": "runtime/dossier_queue.sqlite",
            "poll_seconds": "0.5",
            "retry_seconds": "2",
            "max_retry_seconds": "60",
            "max_attempts": "0",
            "completed_retention_jobs": "1000",
        }
        present = dossier_writer if isinstance(dossier_writer, dict) else {}
        for key, rendered in dossier_defaults.items():
            if key not in present and _ensure_nested_key(
                lines, "dossier_writer", key, rendered
            ):
                changes.append(f"dossier_writer.{key}={rendered}")

    sync_worker = parsed.get("sync_worker")
    if isinstance(sync_worker, dict) and "pause_check_documents" not in sync_worker:
        if _ensure_nested_key(lines, "sync_worker", "pause_check_documents", "25"):
            changes.append("sync_worker.pause_check_documents=25")

    sync_cfg = parsed.get("sync")
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}

    legacy_state = parsed.get("state_db")
    if "state_db" not in sync_cfg:
        if legacy_state not in (None, ""):
            value = str(legacy_state)
            if mode == "dockerized" and value.strip() == "state.sqlite":
                value = "runtime/state.sqlite"
            _ensure_nested_key(lines, "sync", "state_db", value)
            changes.append(f"sync.state_db={value} (migrated legacy state_db)")
            removed = _remove_top_level_key(lines, "state_db")
            if removed is not None:
                changes.append("removed deprecated top-level state_db")
        else:
            _ensure_nested_key(lines, "sync", "state_db", "runtime/state.sqlite")
            changes.append("sync.state_db=runtime/state.sqlite")
    elif mode == "dockerized" and _replace_nested_exact(
        lines, "sync", "state_db", {"state.sqlite"}, "runtime/state.sqlite"
    ):
        changes.append("sync.state_db=runtime/state.sqlite (dockerized persistence fix)")

    if "embedding_deferred_file" not in sync_cfg:
        if _ensure_nested_key(
            lines,
            "sync",
            "embedding_deferred_file",
            "runtime/embedding-deferred.jsonl",
        ):
            changes.append(
                "sync.embedding_deferred_file=runtime/embedding-deferred.jsonl"
            )

    if "status_file" not in sync_cfg:
        if _ensure_nested_key(
            lines,
            "sync",
            "status_file",
            "runtime/sync-status.json",
        ):
            changes.append("sync.status_file=runtime/sync-status.json")

    rendered = "\n".join(lines)
    if original_had_trailing_newline or rendered:
        rendered += "\n"

    # Validate the migrated result before returning it.
    validated = yaml.safe_load(rendered) or {}
    if not isinstance(validated, dict):
        raise ValueError("migrated config root must remain a mapping")
    return rendered, changes


def upgrade_file(
    config_path: str | Path,
    *,
    output_path: str | Path | None = None,
    deployment_mode: str | None = None,
) -> list[str]:
    source = Path(config_path)
    text = source.read_text(encoding="utf-8")
    rendered, changes = upgrade_text(text, deployment_mode=deployment_mode)
    target = Path(output_path) if output_path is not None else source
    target.write_text(rendered, encoding="utf-8")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply safe SunaQ config upgrade migrations"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    parser.add_argument("--deployment", choices=("native", "dockerized"))
    args = parser.parse_args()

    changes = upgrade_file(
        args.config,
        output_path=args.output,
        deployment_mode=args.deployment,
    )
    if changes:
        for change in changes:
            print(f"[MIGRATE] {change}")
    else:
        print("[MIGRATE] config.yaml already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
