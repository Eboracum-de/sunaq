"""Backup/restore support helpers for the console-first SunaQ recovery workflow.

The host-facing orchestration lives in ``install/backup-restore.sh`` so the
Dockerized deployment does not acquire a host-Python dependency. This module is
run either by the Standard virtualenv or inside the existing provider image.

It deliberately owns SunaQ file/SQLite state. Docker volume snapshots are
orchestrated by the shell helper because the provider container has no Docker
socket access. Qdrant vector data and its rebuildable sync state are deliberately
outside the recovery set.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import yaml

from rag.credential_store import CredentialStore
from rag.version import VERSION


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    return value if isinstance(value, dict) else {}


def _cfg_get(cfg: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = cfg
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _resolve(
    root: Path,
    raw: str | Path,
    *,
    source_prefix: Path | None = None,
) -> tuple[Path, str | None]:
    root = root.resolve()
    path = Path(str(raw or "").strip() or ".")
    source = (source_prefix or root).resolve()

    if path.is_absolute():
        absolute = path.resolve(strict=False)
        try:
            relative_path = absolute.relative_to(source)
        except ValueError:
            return absolute, None
        candidate = (root / relative_path).resolve(strict=False)
    else:
        candidate = (root / path).resolve(strict=False)

    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        relative = None
    return candidate, relative


def _resolve_ca_reference(
    root: Path,
    raw: str | Path,
    *,
    source_prefix: Path | None = None,
) -> tuple[Path, str | None]:
    """Resolve configured CA files, including the Dockerized /app mount alias.

    Dockerized stores container-visible CA paths such as
    /app/runtime/ca/nextcloud-ca-bundle.pem in config.yaml while backup
    inventory runs against the host installation prefix. Treat only existing
    files below that known runtime/ca alias as SunaQ-owned; genuinely external
    absolute CA paths remain external.
    """
    path, relative = _resolve(root, raw, source_prefix=source_prefix)
    if relative is not None:
        return path, relative

    raw_path = Path(str(raw or "").strip() or ".")
    if not raw_path.is_absolute():
        return path, relative
    try:
        app_relative = raw_path.relative_to("/app")
    except ValueError:
        return path, relative
    if tuple(app_relative.parts[:2]) != ("runtime", "ca"):
        return path, relative

    root_resolved = root.resolve()
    ca_root = (root_resolved / "runtime" / "ca").resolve(strict=False)
    candidate = (root_resolved / app_relative).resolve(strict=False)
    try:
        candidate.relative_to(ca_root)
        candidate_relative = candidate.relative_to(root_resolved).as_posix()
    except ValueError:
        return path, relative
    if candidate.is_file():
        return candidate, candidate_relative
    return path, relative


def inventory(
    root: str | Path,
    *,
    source_prefix: str | Path | None = None,
) -> dict[str, Any]:
    """Return the SunaQ-owned file/SQLite state relevant to a recovery set."""

    root_path = Path(root).resolve()
    source_path = Path(source_prefix).resolve() if source_prefix else root_path
    cfg = _read_yaml(root_path / "config.yaml")
    runtime_env = _read_env(root_path / "runtime.env")
    provider_env = _read_env(root_path / "provider.env")

    credential_raw = str(
        _cfg_get(cfg, "auth", "credential_store", default="")
        or _cfg_get(cfg, "acl", "credential_store", default="")
        or "runtime/users.sqlite"
    )
    master_key_raw = str(
        runtime_env.get("RAG_CREDENTIAL_MASTER_KEY_FILE")
        or "runtime/credential-master.key"
    )
    sync_state_raw = str(
        _cfg_get(cfg, "sync", "state_db", default="")
        or _cfg_get(cfg, "state_db", default="")
        or "runtime/state.sqlite"
    )
    sync_state_path, sync_state_relative = _resolve(
        root_path, sync_state_raw, source_prefix=source_path
    )
    # Qdrant itself is deliberately not part of the recovery set. Do not archive
    # the associated incremental state either: restoring it without the matching
    # collection can make a rebuild incorrectly classify documents as unchanged.
    qdrant_state_paths = {
        str(sync_state_path),
        str((root_path / "state.sqlite").resolve(strict=False)),
        str((root_path / "runtime" / "state.sqlite").resolve(strict=False)),
    }

    critical_sqlite_raw: list[tuple[str, str]] = [
        ("source_registry", str(_cfg_get(cfg, "source_registry", "path", default="") or "runtime/source_registry.sqlite")),
        ("graph_queue", str(_cfg_get(cfg, "graph_queue", "database", default="") or "runtime/graph_queue.sqlite")),
        ("mail_state", str(_cfg_get(cfg, "mail", "state_file", default="") or "runtime/mail_state.sqlite")),
        ("dossier_writer", str(_cfg_get(cfg, "dossier_writer", "database", default="") or "runtime/dossier_queue.sqlite")),
    ]
    research_log_enabled = str(provider_env.get("RESEARCH_LOG_ENABLED", "true")).strip().lower()
    if research_log_enabled not in {"0", "false", "no", "off"}:
        critical_sqlite_raw.append(
            ("research_log", str(provider_env.get("RESEARCH_LOG_DB") or "research.sqlite"))
        )

    sqlite_raw: list[str] = [credential_raw] + [raw for _, raw in critical_sqlite_raw]
    for candidate in sorted(root_path.glob("*.sqlite")):
        sqlite_raw.append(str(candidate))
    runtime_dir = root_path / "runtime"
    if runtime_dir.is_dir():
        for candidate in sorted(runtime_dir.glob("*.sqlite")):
            sqlite_raw.append(str(candidate))

    sqlite_relative: list[str] = []
    external_paths: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in sqlite_raw:
        path, relative = _resolve(root_path, raw, source_prefix=source_path)
        key = str(path)
        if key in seen or key in qdrant_state_paths:
            continue
        seen.add(key)
        if relative is None:
            external_paths.append({"kind": "sqlite", "path": str(path)})
            continue
        if path.is_file():
            sqlite_relative.append(relative)

    credential_path, credential_relative = _resolve(root_path, credential_raw, source_prefix=source_path)
    master_key_path, master_key_relative = _resolve(root_path, master_key_raw, source_prefix=source_path)

    blocking_external_paths: list[dict[str, str]] = []
    if credential_relative is None:
        blocking_external_paths.append(
            {"kind": "credential_store", "path": str(credential_path)}
        )
    if master_key_relative is None:
        blocking_external_paths.append(
            {"kind": "credential_master_key", "path": str(master_key_path)}
        )
    for kind, raw in critical_sqlite_raw:
        path, relative = _resolve(root_path, raw, source_prefix=source_path)
        if relative is None:
            blocking_external_paths.append({"kind": kind, "path": str(path)})

    # Preserve explicitly configured private CA files and operational/audit
    # paths when they live below the SunaQ prefix. Administrator-managed paths
    # outside the prefix are reported so the operator can back them up separately.
    referenced_files_relative: list[str] = []
    ca_raw: list[str] = []

    def collect_ca_files(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().lower() == "ca_file":
                    text = str(child or "").strip()
                    if text:
                        ca_raw.append(text)
                collect_ca_files(child)
        elif isinstance(value, list):
            for child in value:
                collect_ca_files(child)

    collect_ca_files(cfg)
    for env in (runtime_env, provider_env):
        for key, value in env.items():
            if key.upper().endswith("_CA_FILE") and str(value or "").strip():
                ca_raw.append(str(value).strip())

    ca_seen: set[str] = set()
    for raw in ca_raw:
        path, relative = _resolve_ca_reference(root_path, raw, source_prefix=source_path)
        key = str(path)
        if key in ca_seen:
            continue
        ca_seen.add(key)
        if relative is None:
            external_paths.append({"kind": "ca_file", "path": str(path)})
        elif path.is_file():
            referenced_files_relative.append(relative)

    worker_control_raw = str(
        runtime_env.get("RAG_WORKER_CONTROL_FILE")
        or provider_env.get("RAG_WORKER_CONTROL_FILE")
        or "runtime/worker-control.json"
    )
    retrieval_records_raw = str(
        _cfg_get(cfg, "retrieval_record", "directory", default="")
        or "runtime/retrieval-records"
    )
    for kind, raw in (
        ("worker_control", worker_control_raw),
        ("retrieval_records", retrieval_records_raw),
    ):
        path, relative = _resolve(root_path, raw, source_prefix=source_path)
        if relative is None:
            external_paths.append({"kind": kind, "path": str(path)})
        elif path.exists():
            referenced_files_relative.append(relative)

    archive_candidates = [
        ".sunaq-installation",
        ".aki-rag-installation",
        "config.yaml",
        "web.yaml",
        "provider.env",
        "runtime.env",
        "versions.lock.yaml",
        "models",
        "install/.env",
        "install/dockerized/.env",
        "install/dockerized/docker-compose.override.yml",
        "install/super-light/.env",
        "install/super-light/docker-compose.override.yml",
        "install/install-state.env",
        "install/last-install-command.sh",
        "install/nginx/htpasswd",
        "install/nginx/internal-auth.conf",
        "install/nginx/generated.conf",
        "runtime/ca",
        "runtime/worker-control.json",
        "runtime/retrieval-records",
        "install/nginx/tls",
    ]
    archive_paths: list[str] = []
    for relative in archive_candidates + referenced_files_relative + sqlite_relative:
        if relative and relative not in archive_paths and (root_path / relative).exists():
            archive_paths.append(relative)

    if master_key_relative and master_key_path.exists() and master_key_relative not in archive_paths:
        archive_paths.append(master_key_relative)

    # WAL/SHM sidecars are normally truncated before the archive is created,
    # but include any survivors so a checkpointed database is not separated
    # from state SQLite still considers part of the same snapshot.
    for relative in list(sqlite_relative):
        for suffix in ("-wal", "-shm"):
            sidecar = root_path / (relative + suffix)
            if sidecar.exists():
                archive_paths.append(relative + suffix)

    return {
        "format": "sunaq-inventory-v2",
        "sunaq_version": VERSION,
        "aki_version": VERSION,
        "root": str(root_path),
        "source_prefix": str(source_path),
        "credential_store": {
            "path": str(credential_path),
            "relative": credential_relative,
            "exists": credential_path.is_file(),
        },
        "credential_master_key": {
            "path": str(master_key_path),
            "relative": master_key_relative,
            "exists": master_key_path.is_file(),
        },
        "qdrant_sync_state": {
            "path": str(sync_state_path),
            "relative": sync_state_relative,
            "exists": sync_state_path.is_file(),
            "included": False,
        },
        "sqlite": sorted(sqlite_relative),
        "archive_paths": archive_paths,
        "external_paths": external_paths,
        "blocking_external_paths": blocking_external_paths,
    }


def _sqlite_quick_check(path: Path, *, checkpoint: bool) -> str:
    if checkpoint:
        connection = sqlite3.connect(path, timeout=30)
    else:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        if checkpoint:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = connection.execute("PRAGMA quick_check").fetchone()
        result = str(row[0] if row else "")
        if result.lower() != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {path}: {result or 'no result'}")
        return result
    finally:
        connection.close()


def checkpoint_root(
    root: str | Path,
    *,
    source_prefix: str | Path | None = None,
) -> dict[str, Any]:
    """Checkpoint and integrity-check all discovered SunaQ SQLite databases."""

    root_path = Path(root).resolve()
    state = inventory(root_path, source_prefix=source_prefix)
    checked: list[str] = []
    for relative in state["sqlite"]:
        path = root_path / relative
        _sqlite_quick_check(path, checkpoint=True)
        checked.append(relative)
    return {"ok": True, "sqlite_checked": checked}


def verify_root(
    root: str | Path,
    *,
    source_prefix: str | Path | None = None,
) -> dict[str, Any]:
    """Verify an extracted recovery tree, including credential decryption."""

    root_path = Path(root).resolve()
    state = inventory(root_path, source_prefix=source_prefix)
    errors: list[str] = []
    checked: list[str] = []

    for item in state["blocking_external_paths"]:
        errors.append(
            f"{item['kind']} points outside the recovery root: {item['path']}"
        )

    for relative in state["sqlite"]:
        path = root_path / relative
        try:
            _sqlite_quick_check(path, checkpoint=False)
            checked.append(relative)
        except Exception as exc:
            errors.append(str(exc))

    credential = dict(state["credential_store"])
    master_key = dict(state["credential_master_key"])
    if not credential.get("exists"):
        errors.append("credential store is missing from recovery state")
    if not master_key.get("exists"):
        errors.append("credential master key is missing from recovery state")

    credential_report: dict[str, Any] | None = None
    if not errors and credential.get("relative") and master_key.get("relative"):
        runtime_env = _read_env(root_path / "runtime.env")
        mode = str(runtime_env.get("RAG_CREDENTIAL_ENCRYPTION") or "required").strip().lower()
        old_key = os.environ.get("RAG_CREDENTIAL_MASTER_KEY_FILE")
        old_mode = os.environ.get("RAG_CREDENTIAL_ENCRYPTION")
        os.environ["RAG_CREDENTIAL_MASTER_KEY_FILE"] = str(
            root_path / str(master_key["relative"])
        )
        os.environ["RAG_CREDENTIAL_ENCRYPTION"] = mode
        try:
            store = CredentialStore(root_path / str(credential["relative"]))
            credential_report = store.verify_secret_encryption()
            if not credential_report.get("ok"):
                for error in credential_report.get("errors") or []:
                    errors.append(f"credential verification: {error}")
        except Exception as exc:
            errors.append(f"credential verification failed: {exc}")
        finally:
            if old_key is None:
                os.environ.pop("RAG_CREDENTIAL_MASTER_KEY_FILE", None)
            else:
                os.environ["RAG_CREDENTIAL_MASTER_KEY_FILE"] = old_key
            if old_mode is None:
                os.environ.pop("RAG_CREDENTIAL_ENCRYPTION", None)
            else:
                os.environ["RAG_CREDENTIAL_ENCRYPTION"] = old_mode

    return {
        "ok": not errors,
        "aki_version": VERSION,
        "sqlite_checked": checked,
        "credential_report": credential_report,
        "errors": errors,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="SunaQ backup/restore helper")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "checkpoint-root", "verify-root"):
        command = sub.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--source-prefix")

    args = parser.parse_args()
    if args.command == "inventory":
        result = inventory(args.root, source_prefix=args.source_prefix)
    elif args.command == "checkpoint-root":
        result = checkpoint_root(args.root, source_prefix=args.source_prefix)
    else:
        result = verify_root(args.root, source_prefix=args.source_prefix)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(_main())
