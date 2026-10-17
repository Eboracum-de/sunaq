#!/usr/bin/env python3
"""Persistent SQLite queue for asynchronous evidence graph construction.

The queue deliberately stores one document per job.  A chat request therefore
only performs a small SQLite INSERT and can continue to the answer model.  A
separate ``rag.graph_worker`` process consumes jobs when the RAG service has
been idle long enough.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from rag.graph import BASE_DIR, cfg_get, load_config
from rag.elasticsearch_client import requests_options as elastic_requests_options


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _scope_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def _path_matches(title: str, include_paths: Iterable[str], exclude_paths: Iterable[str] = ()) -> bool:
    title = _scope_path(title)
    includes = [_scope_path(x) for x in include_paths if _scope_path(x)]
    excludes = [_scope_path(x) for x in exclude_paths if _scope_path(x)]
    def under(path: str, root: str) -> bool:
        return path == root or path.startswith(root + "/")
    if includes and not any(under(title, root) for root in includes):
        return False
    if any(under(title, root) for root in excludes):
        return False
    return True


def _es_scoped_documents(
    cfg: dict[str, Any], include_paths: list[str], exclude_paths: list[str], *, limit: int = 0
) -> list[dict[str, Any]]:
    """Lightweight ES traversal; fetches only title and filters paths locally.

    Local filtering deliberately avoids assumptions about the Nextcloud title
    mapping (keyword/analyzed/path_hierarchy). It is a maintenance command, not
    an interactive retrieval path.
    """
    es_url = str(cfg_get(cfg, "elasticsearch.url", "es.url", default="")).rstrip("/")
    index = str(cfg_get(cfg, "elasticsearch.index", "es.index", default=""))
    if not es_url or not index:
        raise RuntimeError("Elasticsearch-Konfiguration fehlt")
    es_request_options = elastic_requests_options(cfg)
    page_size = int(cfg_get(cfg, "elasticsearch.page_size", "es.page_size", default=500) or 500)
    scroll = str(cfg_get(cfg, "elasticsearch.scroll", "es.scroll", default="5m") or "5m")
    r = requests.post(
        f"{es_url}/{index}/_search", params={"scroll": scroll},
        json={"size": page_size, "sort": ["_doc"], "_source": ["title"]},
        timeout=120, **es_request_options,
    )
    r.raise_for_status()
    data = r.json()
    scroll_id = data.get("_scroll_id")
    out: list[dict[str, Any]] = []
    try:
        while True:
            hits = list((data.get("hits") or {}).get("hits") or [])
            if not hits:
                break
            for hit in hits:
                document_id = str(hit.get("_id") or "").strip()
                title = str((hit.get("_source") or {}).get("title") or "").strip()
                if not document_id or not _path_matches(title, include_paths, exclude_paths):
                    continue
                out.append({"document_id": document_id, "title": title, "path": title})
                if limit > 0 and len(out) >= limit:
                    return out
            r = requests.post(
                f"{es_url}/_search/scroll", json={"scroll": scroll, "scroll_id": scroll_id},
                timeout=120, **es_request_options,
            )
            r.raise_for_status()
            data = r.json()
            scroll_id = data.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            try:
                requests.delete(
                    f"{es_url}/_search/scroll", json={"scroll_id": [scroll_id]},
                    timeout=30, **es_request_options,
                )
            except requests.RequestException:
                pass
    return out


class GraphQueue:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.enabled = bool(cfg_get(cfg, "graph_queue.enabled", default=False))
        raw_path = str(cfg_get(cfg, "graph_queue.database", default="graph_queue.sqlite") or "graph_queue.sqlite")
        path = Path(raw_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS graph_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    document_id TEXT NOT NULL,
                    query_id TEXT,
                    evidence_action TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    last_error TEXT,
                    coalesced_count INTEGER NOT NULL DEFAULT 0,
                    coalesced_into INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_graph_jobs_status_created
                    ON graph_jobs(status, priority, id);
                CREATE INDEX IF NOT EXISTS idx_graph_jobs_document
                    ON graph_jobs(document_id);

                CREATE TABLE IF NOT EXISTS graph_queue_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Lightweight in-place migration for queues created by 0.5.4.
            columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(graph_jobs)")}
            if "coalesced_count" not in columns:
                con.execute(
                    "ALTER TABLE graph_jobs ADD COLUMN coalesced_count INTEGER NOT NULL DEFAULT 0"
                )
            if "coalesced_into" not in columns:
                con.execute(
                    "ALTER TABLE graph_jobs ADD COLUMN coalesced_into INTEGER"
                )
            self._coalesce_existing_active_jobs(con)

    def _coalesce_existing_active_jobs(self, con: sqlite3.Connection) -> int:
        """Collapse pre-0.5.7 duplicate pending/running work without deleting rows.

        The running job wins; otherwise the oldest pending job wins.  Duplicate
        rows are retained with status='coalesced' for auditability.
        """
        groups = con.execute(
            """
            SELECT document_id
            FROM graph_jobs
            WHERE status IN ('pending','running')
            GROUP BY document_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        changed = 0
        now = _utcnow()
        for group in groups:
            document_id = str(group["document_id"])
            rows = con.execute(
                """
                SELECT id, status, coalesced_count
                FROM graph_jobs
                WHERE document_id=? AND status IN ('pending','running')
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id ASC
                """,
                (document_id,),
            ).fetchall()
            if len(rows) < 2:
                continue
            keeper = rows[0]
            keeper_id = int(keeper["id"])
            extra = 0
            for row in rows[1:]:
                extra += 1 + int(row["coalesced_count"] or 0)
                con.execute(
                    """
                    UPDATE graph_jobs
                    SET status='coalesced', coalesced_into=?,
                        finished_at=COALESCE(finished_at, ?), updated_at=?
                    WHERE id=?
                    """,
                    (keeper_id, now, now, int(row["id"])),
                )
                changed += 1
            if extra:
                con.execute(
                    """
                    UPDATE graph_jobs
                    SET coalesced_count=coalesced_count+?, updated_at=?
                    WHERE id=?
                    """,
                    (extra, now, keeper_id),
                )
        return changed

    def mark_activity(self, kind: str = "search") -> None:
        if not self.enabled:
            return
        now = _utcnow()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO graph_queue_meta(key, value) VALUES('last_activity', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now,),
            )
            con.execute(
                """
                INSERT INTO graph_queue_meta(key, value) VALUES('last_activity_kind', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(kind or "search"),),
            )

    def last_activity(self) -> datetime | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM graph_queue_meta WHERE key='last_activity'"
            ).fetchone()
        return _parse_ts(row["value"] if row else None)

    def idle_seconds(self) -> float:
        last = self.last_activity()
        if last is None:
            return 10**9
        return max(0.0, (datetime.now(timezone.utc) - last).total_seconds())

    def enqueue_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Split one evidence event into one durable job per document.

        A document that is already pending/running is not queued a second time.
        Instead ``coalesced_count`` is incremented on the active job.  This keeps
        graph construction work bounded while preserving how many duplicate
        evidence events arrived during that active job.
        """
        if not self.enabled:
            return {
                "enabled": False,
                "queued": 0,
                "coalesced": 0,
                "job_ids": [],
                "coalesced_job_ids": [],
            }

        documents = list(payload.get("documents") or [])
        query_id = str(payload.get("query_id") or "")
        action = str(payload.get("evidence_action") or "answer")
        raw_priority = payload.get("queue_priority", payload.get("priority"))
        if isinstance(raw_priority, str) and raw_priority.strip().lower() in {"high", "normal", "background"}:
            name = raw_priority.strip().lower()
            defaults = {"high": 50, "normal": 100, "background": 200}
            priority = int(cfg_get(self.cfg, f"graph_queue.priorities.{name}", default=defaults[name]) or defaults[name])
        elif raw_priority is not None:
            priority = int(raw_priority)
        else:
            priority = int(cfg_get(self.cfg, "graph_queue.priority", default=100) or 100)
        now = _utcnow()
        job_ids: list[int] = []
        coalesced_job_ids: list[int] = []

        with self._connect() as con:
            # Serialize enqueue operations so two simultaneous API requests do
            # not both miss the active-job check.
            con.execute("BEGIN IMMEDIATE")
            for doc in documents:
                document_id = str((doc or {}).get("document_id") or "").strip()
                if not document_id:
                    continue
                job_payload = {
                    "query_id": query_id,
                    "user_query": str(payload.get("user_query") or ""),
                    "retrieval_query": str(payload.get("retrieval_query") or ""),
                    "evidence_action": action,
                    "entity_discovery": payload.get("entity_discovery"),
                    "relation_discovery": payload.get("relation_discovery"),
                    "force_reindex": bool(payload.get("force_reindex", False)),
                    "count_evidence": bool(payload.get("count_evidence", True)),
                    "documents": [dict(doc)],
                }

                existing = con.execute(
                    """
                    SELECT id, status, priority, payload_json
                    FROM graph_jobs
                    WHERE document_id=? AND status IN ('pending','running')
                    ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id ASC
                    LIMIT 1
                    """,
                    (document_id,),
                ).fetchone()

                if existing is not None:
                    existing_id = int(existing["id"])
                    try:
                        old_payload = json.loads(str(existing["payload_json"] or "{}"))
                    except Exception:
                        old_payload = {}
                    # A later high-priority real evidence event must be allowed to
                    # promote a previously queued background/path job; otherwise
                    # count_evidence/query provenance would be lost by coalescing.
                    promote_payload = (
                        priority < int(existing["priority"] or priority)
                        or (bool(job_payload.get("count_evidence", True)) and not bool(old_payload.get("count_evidence", True)))
                    )
                    if promote_payload:
                        con.execute(
                            """
                            UPDATE graph_jobs
                            SET coalesced_count=coalesced_count+1, updated_at=?,
                                priority=MIN(priority, ?), query_id=?, evidence_action=?,
                                payload_json=?
                            WHERE id=?
                            """,
                            (now, priority, query_id, action, json.dumps(job_payload, ensure_ascii=False), existing_id),
                        )
                    else:
                        con.execute(
                            """
                            UPDATE graph_jobs
                            SET coalesced_count=coalesced_count+1,
                                updated_at=?, priority=MIN(priority, ?)
                            WHERE id=?
                            """,
                            (now, priority, existing_id),
                        )
                    coalesced_job_ids.append(existing_id)
                    continue

                cur = con.execute(
                    """
                    INSERT INTO graph_jobs(
                        status, priority, created_at, updated_at,
                        document_id, query_id, evidence_action, payload_json,
                        coalesced_count
                    ) VALUES('pending', ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        priority,
                        now,
                        now,
                        document_id,
                        query_id,
                        action,
                        json.dumps(job_payload, ensure_ascii=False),
                    ),
                )
                job_ids.append(int(cur.lastrowid))
            con.commit()

        return {
            "enabled": True,
            "queued": len(job_ids),
            "coalesced": len(coalesced_job_ids),
            "job_ids": job_ids,
            "coalesced_job_ids": coalesced_job_ids,
            "database": str(self.path),
        }

    def recover_stale_running(self, stale_seconds: int = 3600) -> int:
        """Return jobs left 'running' by a crashed worker to pending."""
        cutoff = datetime.now(timezone.utc).timestamp() - max(60, int(stale_seconds))
        recovered = 0
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, started_at FROM graph_jobs WHERE status='running'"
            ).fetchall()
            for row in rows:
                started = _parse_ts(row["started_at"])
                if started is None or started.timestamp() < cutoff:
                    con.execute(
                        """
                        UPDATE graph_jobs
                        SET status='pending', updated_at=?, last_error=COALESCE(last_error,'') || ' [stale worker recovered]'
                        WHERE id=?
                        """,
                        (_utcnow(), int(row["id"])),
                    )
                    recovered += 1
        return recovered

    def claim_next(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT * FROM graph_jobs
                WHERE status='pending'
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                con.commit()
                return None
            now = _utcnow()
            con.execute(
                """
                UPDATE graph_jobs
                SET status='running', started_at=?, updated_at=?, attempts=attempts+1
                WHERE id=? AND status='pending'
                """,
                (now, now, int(row["id"])),
            )
            con.commit()
            out = dict(row)
            out["status"] = "running"
            out["attempts"] = int(out.get("attempts") or 0) + 1
            out["payload"] = json.loads(str(out.pop("payload_json")))
            return out
        finally:
            con.close()

    def finish(self, job_id: int, result: dict[str, Any]) -> None:
        errors = list(result.get("errors") or [])
        docs = list(result.get("documents") or [])
        if docs and all(str(doc.get("status") or "") == "skipped_oversize" for doc in docs):
            status = "skipped_oversize"
        else:
            status = "done_with_errors" if errors else "done"
        with self._connect() as con:
            con.execute(
                """
                UPDATE graph_jobs
                SET status=?, result_json=?, last_error=?, finished_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(errors, ensure_ascii=False) if errors else None,
                    _utcnow(),
                    _utcnow(),
                    int(job_id),
                ),
            )

    def fail(self, job_id: int, error: str, *, retry: bool = True) -> None:
        status = "pending" if retry else "error"
        with self._connect() as con:
            con.execute(
                """
                UPDATE graph_jobs
                SET status=?, last_error=?, updated_at=?,
                    finished_at=CASE WHEN ?='error' THEN ? ELSE finished_at END
                WHERE id=?
                """,
                (status, str(error), _utcnow(), status, _utcnow(), int(job_id)),
            )


    def worker_heartbeat(
        self,
        *,
        state: str,
        pid: int | None = None,
        host: str | None = None,
        instance_id: str | None = None,
        job_id: int | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist one lightweight worker heartbeat in queue metadata."""
        payload = {
            "heartbeat_at": _utcnow(),
            "state": str(state or "unknown"),
            "pid": int(pid) if pid is not None else None,
            "host": str(host or "") or None,
            "instance_id": str(instance_id or "") or None,
            "job_id": int(job_id) if job_id is not None else None,
            "started_at": str(started_at or "") or None,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO graph_queue_meta(key,value) VALUES('worker_heartbeat_json',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (raw,),
            )
        return payload

    def worker_status(self) -> dict[str, Any]:
        """Return worker liveness derived from its periodic SQLite heartbeat."""
        stale_seconds = float(
            cfg_get(self.cfg, "graph_queue.worker.heartbeat_stale_seconds", default=90) or 90
        )
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM graph_queue_meta WHERE key='worker_heartbeat_json'"
            ).fetchone()
        if not row:
            return {
                "status": "unknown" if self.enabled else "disabled",
                "alive": False,
                "heartbeat_at": None,
                "heartbeat_age_seconds": None,
                "stale_after_seconds": stale_seconds,
                "state": None,
                "pid": None,
                "host": None,
                "instance_id": None,
                "job_id": None,
            }
        try:
            payload = json.loads(str(row["value"]))
        except Exception:
            payload = {}
        heartbeat = _parse_ts(payload.get("heartbeat_at"))
        age = None
        if heartbeat is not None:
            age = max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
        state = str(payload.get("state") or "unknown")
        alive = bool(
            self.enabled
            and state != "stopped"
            and age is not None
            and age <= stale_seconds
        )
        if not self.enabled:
            status = "disabled"
        elif state == "stopped":
            status = "stopped"
        elif heartbeat is None:
            status = "unknown"
        elif alive:
            status = "ok"
        else:
            status = "stale"
        return {
            "status": status,
            "alive": alive,
            "heartbeat_at": payload.get("heartbeat_at"),
            "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "stale_after_seconds": stale_seconds,
            "state": state,
            "pid": payload.get("pid"),
            "host": payload.get("host"),
            "instance_id": payload.get("instance_id"),
            "job_id": payload.get("job_id"),
            "started_at": payload.get("started_at"),
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n FROM graph_jobs GROUP BY status"
            ).fetchall()
            total = con.execute("SELECT COUNT(*) AS n FROM graph_jobs").fetchone()["n"]
            oldest = con.execute(
                "SELECT created_at FROM graph_jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            last_kind = con.execute(
                "SELECT value FROM graph_queue_meta WHERE key='last_activity_kind'"
            ).fetchone()
            coalesced_total = con.execute(
                "SELECT COALESCE(SUM(coalesced_count),0) AS n FROM graph_jobs"
            ).fetchone()["n"]
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        last = self.last_activity()
        return {
            "enabled": self.enabled,
            "database": str(self.path),
            "total": int(total),
            "counts": counts,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "last_activity": last.isoformat() if last else None,
            "last_activity_kind": last_kind["value"] if last_kind else None,
            "idle_seconds": round(self.idle_seconds(), 1),
            "oldest_pending": oldest["created_at"] if oldest else None,
            "coalesced_total": int(coalesced_total or 0),
            "worker": self.worker_status(),
        }

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, status, priority, created_at, started_at, finished_at,
                       attempts, document_id, query_id, evidence_action, last_error,
                       coalesced_count, coalesced_into
                FROM graph_jobs
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def preview_path(
        self,
        include_paths: list[str],
        exclude_paths: list[str] | None = None,
        *,
        priority: str = "normal",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Preview path-scoped graph ingestion without mutating the queue."""
        include_paths = [str(x).strip() for x in include_paths if str(x or '').strip()]
        exclude_paths = [str(x).strip() for x in (exclude_paths or []) if str(x or '').strip()]
        if not include_paths:
            raise ValueError("Mindestens ein Include-Pfad ist erforderlich")
        if priority not in {"high", "normal", "background"}:
            raise ValueError(f"Ungültige Priorität: {priority}")
        docs = _es_scoped_documents(
            self.cfg, include_paths, exclude_paths, limit=max(0, int(limit))
        )
        return {
            "action": "enqueue_path",
            "paths": include_paths,
            "exclude_paths": exclude_paths,
            "priority": priority,
            "documents_found": len(docs),
            "sample": docs[:20],
            "queue_database": str(self.path),
            "_documents": docs,
        }

    def enqueue_path(
        self,
        include_paths: list[str],
        exclude_paths: list[str] | None = None,
        *,
        priority: str = "normal",
        limit: int = 0,
    ) -> dict[str, Any]:
        """Queue all ES documents in the given path scope."""
        preview = self.preview_path(
            include_paths, exclude_paths, priority=priority, limit=limit
        )
        docs = list(preview.pop("_documents", []))
        result = self.enqueue_evidence({
            "query_id": "manual-path",
            "user_query": "",
            "retrieval_query": "",
            "evidence_action": "manual_path",
            "queue_priority": priority,
            "entity_discovery": True,
            "relation_discovery": True,
            "force_reindex": False,
            "count_evidence": False,
            "documents": docs,
        })
        return {**preview, **result}

def main() -> int:
    parser = argparse.ArgumentParser(description="Graph queue administration")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats", help="Show queue statistics")
    recent = sub.add_parser("recent", help="Show recent queue jobs")
    recent.add_argument("--limit", type=int, default=20)
    ep = sub.add_parser("enqueue-path", help="Queue all Elasticsearch documents below one or more Nextcloud paths")
    ep.add_argument("path", nargs="+", help="Path prefix, e.g. Nordstern/Beteiligungen")
    ep.add_argument("--exclude-path", action="append", default=[])
    ep.add_argument("--priority", choices=["high", "normal", "background"], default="normal")
    ep.add_argument("--limit", type=int, default=0, help="At most N matching documents; 0=all")
    ep.add_argument("--yes", action="store_true", help="Actually enqueue; otherwise preview only")
    args = parser.parse_args()

    cfg = load_config(args.config)
    queue = GraphQueue(cfg)
    if args.command == "stats":
        print(json.dumps(queue.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "recent":
        print(json.dumps(queue.recent_jobs(args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.command == "enqueue-path":
        if not args.yes:
            preview = queue.preview_path(
                list(args.path), list(args.exclude_path),
                priority=args.priority, limit=max(0, int(args.limit)),
            )
            preview.pop("_documents", None)
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            print("Keine Queue-Änderung. Zum Einreihen denselben Befehl mit --yes wiederholen.")
            return 0
        print(json.dumps(queue.enqueue_path(
            list(args.path), list(args.exclude_path),
            priority=args.priority, limit=max(0, int(args.limit)),
        ), ensure_ascii=False, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
