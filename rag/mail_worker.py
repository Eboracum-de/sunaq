"""Long-running scheduler for the per-user IMAP -> Nextcloud mail sync.

The worker deliberately owns only scheduling/lifecycle. One import pass remains
implemented by :mod:`rag.mail_sync`, so native/systemd and Docker deployments
execute the same sync code path.

The config file is re-read before every iteration. This lets administrators
change ``mail.enabled``, ``mail.worker.enabled`` or the poll interval without
restarting the worker process.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

from rag.logging_utils import get_logger
from rag.runtime_control import worker_enabled as runtime_worker_enabled


log = get_logger("mail-worker")
DEFAULT_POLL_SECONDS = 300
DEFAULT_IDLE_POLL_SECONDS = 15
MIN_POLL_SECONDS = 60


def _load_mail_worker_config(config_path: Path) -> tuple[bool, int]:
    """Return ``(enabled, poll_seconds)`` from the current YAML file.

    ``enabled`` requires both the global mail feature and the worker switch.
    Invalid/missing intervals fall back to 300 seconds and are always clamped
    to at least 60 seconds so a bad config cannot create a busy loop.
    """
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    mail_cfg = cfg.get("mail") or {}
    worker_cfg = mail_cfg.get("worker") or {}
    enabled = bool(mail_cfg.get("enabled", False)) and bool(worker_cfg.get("enabled", False))
    raw_interval = worker_cfg.get("poll_interval_seconds") or mail_cfg.get("poll_interval_seconds") or DEFAULT_POLL_SECONDS
    try:
        interval = max(MIN_POLL_SECONDS, int(raw_interval))
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_SECONDS
    return enabled, interval


def _run_sync_once(config_path: Path) -> int:
    """Run one mail-sync pass in an isolated child process."""
    argv = [sys.executable, "-m", "rag.mail_sync", "--config", str(config_path)]
    completed = subprocess.run(argv, check=False)
    return int(completed.returncode)


def run_worker(
    config_path: Path,
    *,
    once: bool = False,
    idle_poll_seconds: int = DEFAULT_IDLE_POLL_SECONDS,
) -> int:
    """Run the scheduler loop.

    Disabled mail polling keeps the process alive and re-checks the config.
    That behaviour is important for systemd and Docker: an administrator may
    enable or disable polling without restarting the service/container.
    """
    idle_poll_seconds = max(1, int(idle_poll_seconds))
    last_state: bool | None = None

    while True:
        try:
            configured, poll_seconds = _load_mail_worker_config(config_path)
            runtime_allowed = runtime_worker_enabled("mail_worker", config_path=config_path, default=False)
            enabled = configured and runtime_allowed
        except Exception as exc:
            log.error("mail worker config konnte nicht gelesen werden (%s): %s", config_path, exc)
            if once:
                return 1
            time.sleep(idle_poll_seconds)
            continue

        if not enabled:
            if last_state is not False:
                reason = "runtime execution paused by administrator" if configured and not runtime_allowed else "mail feature/worker disabled"
                log.info(
                    "%s; Prozess bleibt aktiv und prüft Status alle %ss",
                    reason,
                    idle_poll_seconds,
                )
            last_state = False
            if once:
                return 0
            time.sleep(idle_poll_seconds)
            continue

        if last_state is not True:
            log.info("mail worker aktiviert; poll_interval_seconds=%s", poll_seconds)
        last_state = True

        rc = _run_sync_once(config_path)
        if rc != 0:
            log.warning("mail sync run fehlgeschlagen rc=%s; nächster Versuch in %ss", rc, poll_seconds)
        else:
            log.info("mail sync run beendet; nächster Lauf in %ss", poll_seconds)

        if once:
            return rc
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Langlaufender Scheduler für den benutzerbezogenen IMAP-Mailimport")
    parser.add_argument("--config", default=os.getenv("RAG_CONFIG", "config.yaml"))
    parser.add_argument("--once", action="store_true", help="Konfiguration prüfen und höchstens einen Sync-Lauf ausführen")
    parser.add_argument(
        "--idle-poll-seconds",
        type=int,
        default=DEFAULT_IDLE_POLL_SECONDS,
        help="Prüfintervall bei deaktiviertem Worker oder unlesbarer Konfiguration (Default: 15)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(
        getattr(logging, os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    )

    return run_worker(
        Path(args.config).resolve(),
        once=bool(args.once),
        idle_poll_seconds=int(args.idle_poll_seconds),
    )


if __name__ == "__main__":
    raise SystemExit(main())
