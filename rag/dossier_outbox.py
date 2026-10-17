"""Durable outbox for per-conversation ResearchRun persistence.

The request path commits logical dossier writes to SQLite. A single provider-side
worker delivers them to ResearchArchive/Nextcloud WebDAV in order. This keeps
transient DAV locks and process restarts out of retrieval orchestration while
preserving per-conversation write ordering.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from rag.logging_utils import get_logger
from rag.research_archive import ResearchArchive, ResearchRun

log = get_logger("provider")

_BASE_DIR = Path(__file__).resolve().parent.parent
_ALLOWED_OPERATIONS = {
    "persist_run_start",
    "write_retrieval_decision",
    "write_retrieval_execution",
    "write_documents",
    "write_web_evidence",
    "write_delegated_research",
    "write_resolver",
    "write_research_markdown",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat(timespec="milliseconds")


class DossierOutbox:
    """SQLite-backed ordered delivery queue for ResearchArchive metadata writes."""

    def __init__(self, cfg: dict[str, Any]):
        section = dict(cfg.get("dossier_writer") or {})
        raw_path = str(section.get("database") or "runtime/dossier_queue.sqlite")
        path = Path(raw_path)
        if not path.is_absolute():
            path = _BASE_DIR / path
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.enabled = bool(section.get("enabled", True))
        self.poll_seconds = max(0.1, float(section.get("poll_seconds") or 0.5))
        self.retry_seconds = max(0.5, float(section.get("retry_seconds") or 2.0))
        self.max_retry_seconds = max(
            self.retry_seconds,
            float(section.get("max_retry_seconds") or 60.0),
        )
        max_attempts = section.get("max_attempts", 0)
        self.max_attempts = max(0, int(max_attempts))
        completed_retention = section.get("completed_retention_jobs", 1000)
        self.completed_retention_jobs = max(0, int(completed_retention))
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS dossier_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    dossier_key TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_dossier_jobs_delivery
                    ON dossier_jobs(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_dossier_jobs_dossier
                    ON dossier_jobs(dossier_key, id);
                """
            )
            # A process can die after claiming a job but before completing it.
            # Re-queue such jobs on startup; the archive writes are idempotent
            # at the file level and trace duplication is preferable to loss.
            now = _iso()
            con.execute(
                """
                UPDATE dossier_jobs
                SET status='pending', updated_at=?, next_attempt_at=?,
                    started_at=NULL,
                    last_error=CASE
                        WHEN last_error IS NULL OR last_error='' THEN 'recovered_after_restart'
                        ELSE last_error || '; recovered_after_restart'
                    END
                WHERE status='running'
                """,
                (now, now),
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def enqueue(
        self,
        operation: str,
        run: ResearchRun,
        *,
        label: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        if not self.enabled:
            return None
        operation = str(operation or "").strip()
        if operation not in _ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported dossier outbox operation: {operation!r}")

        encoded_payload = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if len(encoded_payload.encode("utf-8")) > 1_500_000:
            raise ValueError("dossier outbox payload exceeds hard size limit")

        run_payload = asdict(run)
        now = _iso()
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO dossier_jobs(
                    status, created_at, updated_at, next_attempt_at,
                    dossier_key, conversation_id, run_id, run_json,
                    operation, payload_json, label
                ) VALUES('pending',?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    now,
                    now,
                    str(run.dossier_path),
                    str(run.conversation_id),
                    str(run.run_id),
                    json.dumps(run_payload, ensure_ascii=False, sort_keys=True),
                    operation,
                    encoded_payload,
                    str(label or operation)[:160],
                ),
            )
            job_id = int(cur.lastrowid)
        log.info(
            "Dossier outbox queued: job=%d run=%s operation=%s",
            job_id,
            run.run_id,
            operation,
        )
        return job_id

    def stats(self) -> dict[str, int]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n FROM dossier_jobs GROUP BY status"
            ).fetchall()
        result = {"pending": 0, "running": 0, "completed": 0, "dead": 0}
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        return result

    def _claim_next(self) -> sqlite3.Row | None:
        now = _iso()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT j.*
                FROM dossier_jobs AS j
                WHERE j.status='pending'
                  AND j.next_attempt_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dossier_jobs AS earlier
                      WHERE earlier.dossier_key=j.dossier_key
                        AND earlier.id < j.id
                        AND earlier.status IN ('pending','running')
                  )
                ORDER BY j.id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                con.commit()
                return None
            con.execute(
                """
                UPDATE dossier_jobs
                SET status='running', started_at=?, updated_at=?,
                    attempts=attempts+1
                WHERE id=? AND status='pending'
                """,
                (now, now, int(row["id"])),
            )
            con.commit()
            return con.execute(
                "SELECT * FROM dossier_jobs WHERE id=?",
                (int(row["id"]),),
            ).fetchone()

    def _complete(self, job_id: int) -> None:
        now = _iso()
        with self._connect() as con:
            con.execute(
                """
                UPDATE dossier_jobs
                SET status='completed', updated_at=?, finished_at=?,
                    last_error=NULL
                WHERE id=?
                """,
                (now, now, int(job_id)),
            )
            if self.completed_retention_jobs == 0:
                con.execute("DELETE FROM dossier_jobs WHERE status='completed'")
            else:
                con.execute(
                    """
                    DELETE FROM dossier_jobs
                    WHERE status='completed'
                      AND id NOT IN (
                          SELECT id
                          FROM dossier_jobs
                          WHERE status='completed'
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (self.completed_retention_jobs,),
                )

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            response = getattr(exc, "response", None)
            status = int(getattr(response, "status_code", 0) or 0)
            return status in {408, 409, 423, 425, 429, 500, 502, 503, 504}
        return False

    def _retry_or_dead(self, row: sqlite3.Row, exc: Exception) -> str:
        attempts = int(row["attempts"] or 0)
        now_dt = _utcnow()
        error = f"{type(exc).__name__}: {exc}"[:4000]
        if (
            not self._retryable(exc)
            or (self.max_attempts > 0 and attempts >= self.max_attempts)
        ):
            with self._connect() as con:
                con.execute(
                    """
                    UPDATE dossier_jobs
                    SET status='dead', updated_at=?, finished_at=?, last_error=?
                    WHERE id=?
                    """,
                    (_iso(now_dt), _iso(now_dt), error, int(row["id"])),
                )
            return "dead"

        delay = min(
            self.max_retry_seconds,
            self.retry_seconds * (2 ** max(0, min(attempts - 1, 8))),
        )
        next_attempt = now_dt + timedelta(seconds=delay)
        with self._connect() as con:
            con.execute(
                """
                UPDATE dossier_jobs
                SET status='pending', updated_at=?, next_attempt_at=?,
                    started_at=NULL, last_error=?
                WHERE id=?
                """,
                (_iso(now_dt), _iso(next_attempt), error, int(row["id"])),
            )
        return f"retry:{delay:.1f}"

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> ResearchRun:
        value = json.loads(str(row["run_json"] or "{}"))
        conversation_id = ResearchArchive._conversation_key(
            str(value.get("conversation_id") or "")
        )
        run_id = str(value.get("run_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
            raise ValueError("invalid ResearchRun id in dossier outbox")

        dossier_path = str(value.get("dossier_path") or "").strip(" /")
        run_path = str(value.get("run_path") or "").strip(" /")
        if (
            not dossier_path
            or any(part in {"", ".", ".."} for part in dossier_path.split("/"))
            or dossier_path.split("/")[-1] != conversation_id
        ):
            raise ValueError("invalid conversation dossier path in dossier outbox")
        expected_run_path = f"{dossier_path}/runs/{run_id}"
        if run_path != expected_run_path:
            raise ValueError("invalid ResearchRun path in dossier outbox")

        return ResearchRun(
            conversation_id=conversation_id,
            run_id=run_id,
            dossier_path=dossier_path,
            run_path=run_path,
            scoped_user_id=str(value.get("scoped_user_id") or ""),
            created_at=str(value.get("created_at") or ""),
        )

    async def deliver_one(self, archive: ResearchArchive) -> bool:
        row = self._claim_next()
        if row is None:
            return False

        job_id = int(row["id"])
        operation = str(row["operation"])
        queued_run_id = str(row["run_id"] or "")
        try:
            if operation not in _ALLOWED_OPERATIONS:
                raise ValueError(
                    f"unsupported dossier outbox operation: {operation!r}"
                )
            run = self._decode_run(row)
            payload = json.loads(str(row["payload_json"] or "{}"))
            if not isinstance(payload, dict):
                raise ValueError("invalid dossier outbox payload")
            method = getattr(archive, operation)
            await method(run, **payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = self._retry_or_dead(row, exc)
            log.warning(
                "Dossier writer failed: job=%d run=%s operation=%s outcome=%s error=%s: %s",
                job_id,
                queued_run_id,
                operation,
                outcome,
                type(exc).__name__,
                exc,
            )
            return True

        self._complete(job_id)
        log.info(
            "Dossier writer completed: job=%d run=%s operation=%s attempts=%d",
            job_id,
            run.run_id,
            operation,
            int(row["attempts"] or 0),
        )
        return True

    async def run(self, archive: ResearchArchive) -> None:
        """Drain forever. Cancellation is the provider shutdown signal."""
        log.info("Dossier writer started: database=%s stats=%s", self.path, self.stats())
        while True:
            delivered = await self.deliver_one(archive)
            if not delivered:
                await asyncio.sleep(self.poll_seconds)
