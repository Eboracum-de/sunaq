"""Small deployment-neutral runtime control plane for worker execution.

The immutable/configured capability remains in config.yaml.  This file only
controls whether an already configured background worker may perform work.
It lives under runtime/ so both native and Dockerized deployments can update it
without granting the Admin UI access to host service managers or Docker.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
KNOWN_WORKERS = {"sync_worker", "mail_worker"}


def control_path(config_path: str | Path | None = None) -> Path:
    explicit = str(os.getenv("RAG_WORKER_CONTROL_FILE", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if config_path is not None:
        return Path(config_path).resolve().parent / "runtime" / "worker-control.json"
    return Path("runtime/worker-control.json").resolve()


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def worker_enabled(
    worker: str,
    *,
    config_path: str | Path | None = None,
    default: bool = False,
) -> bool:
    if worker not in KNOWN_WORKERS:
        raise ValueError(f"unknown worker: {worker}")
    data = _read(control_path(config_path))
    workers = data.get("workers")
    if not isinstance(workers, dict) or worker not in workers:
        return bool(default)
    return bool(workers.get(worker))


def snapshot(
    *,
    config_path: str | Path | None = None,
    defaults: dict[str, bool] | None = None,
) -> dict[str, bool]:
    defaults = dict(defaults or {})
    return {
        name: worker_enabled(
            name,
            config_path=config_path,
            default=bool(defaults.get(name, False)),
        )
        for name in sorted(KNOWN_WORKERS)
    }


def set_worker_enabled(
    worker: str,
    enabled: bool,
    *,
    config_path: str | Path | None = None,
) -> Path:
    if worker not in KNOWN_WORKERS:
        raise ValueError(f"unknown worker: {worker}")
    path = control_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read(path)
    workers = data.get("workers")
    if not isinstance(workers, dict):
        workers = {}
    workers[worker] = bool(enabled)
    data = {
        "version": SCHEMA_VERSION,
        "workers": workers,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
