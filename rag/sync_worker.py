"""Periodic Elasticsearch -> Qdrant synchronization worker.

Thin process wrapper around the canonical rag.sync CLI. It re-reads worker
settings before each run so Standard and Dockerized deployments use the same
synchronization semantics.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from rag.logging_utils import get_logger
from rag.runtime_control import worker_enabled as runtime_worker_enabled

log = get_logger("sync-worker")
DEFAULT_IDLE_POLL_SECONDS = 15


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def load_worker_settings(config_path: str | Path) -> tuple[bool, int, int, bool, int]:
    with Path(config_path).open(encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    worker = cfg.get("sync_worker") or {}
    qdrant = cfg.get("qdrant") or {}
    enabled = _truthy(worker.get("enabled"), True) and _truthy(qdrant.get("enabled"), True)
    interval = max(60, int(worker.get("poll_interval_seconds") or 300))
    max_documents = max(0, int(worker.get("max_documents") or 0))
    enqueue_graph = _truthy(worker.get("enqueue_graph"), False)
    pause_check_documents = max(1, int(worker.get("pause_check_documents") or 25))
    return enabled, interval, max_documents, enqueue_graph, pause_check_documents


def run_once(
    config_path: str | Path,
    *,
    max_documents: int,
    enqueue_graph: bool,
    pause_check_documents: int,
) -> int:
    command = [
        sys.executable,
        "-m",
        "rag.sync",
        "--config",
        str(config_path),
        "--max-documents",
        str(max_documents),
        "--runtime-control-worker",
        "sync_worker",
        "--pause-check-documents",
        str(max(1, int(pause_check_documents))),
        "--enqueue-graph" if enqueue_graph else "--no-enqueue-graph",
    ]
    return int(subprocess.run(command, check=False).returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Periodic SunaQ Elasticsearch -> Qdrant sync worker")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    last_state: tuple[bool, bool] | None = None
    while True:
        configured, interval, max_documents, enqueue_graph, pause_check_documents = load_worker_settings(args.config)
        runtime_allowed = runtime_worker_enabled("sync_worker", config_path=args.config, default=False)
        state = (configured, runtime_allowed)
        if state != last_state:
            if not configured:
                log.info("sync_worker/qdrant capability disabled in config")
            elif not runtime_allowed:
                log.info("sync worker runtime execution paused by administrator")
            else:
                log.info(
                    "sync worker runtime execution enabled; poll_interval_seconds=%s max_documents=%s pause_check_documents=%s",
                    interval,
                    max_documents,
                    pause_check_documents,
                )
            last_state = state

        if configured and runtime_allowed:
            code = run_once(
                args.config,
                max_documents=max_documents,
                enqueue_graph=enqueue_graph,
                pause_check_documents=pause_check_documents,
            )
            if code:
                log.error("ES->Qdrant sync run failed exit=%s; retrying after interval", code)
            runtime_allowed_after_run = runtime_worker_enabled(
                "sync_worker",
                config_path=args.config,
                default=False,
            )
            sleep_seconds = interval if runtime_allowed_after_run else DEFAULT_IDLE_POLL_SECONDS
        else:
            sleep_seconds = DEFAULT_IDLE_POLL_SECONDS

        if args.once:
            return 0
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
