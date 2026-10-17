from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from rag.dossier_outbox import DossierOutbox
from rag.research_archive import ResearchRun


def _cfg(path):
    return {
        "dossier_writer": {
            "enabled": True,
            "database": str(path),
            "poll_seconds": 0.1,
            "retry_seconds": 0.5,
            "max_retry_seconds": 1.0,
            "max_attempts": 3,
        }
    }


def _run(name: str = "run-1") -> ResearchRun:
    return ResearchRun(
        conversation_id="chat-abcdefgh",
        run_id=name,
        dossier_path="SunaQ-Chats/chat-abcdefgh",
        run_path=f"SunaQ-Chats/chat-abcdefgh/runs/{name}",
        scoped_user_id="client::alice",
        created_at="2026-10-11T12:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_dossier_outbox_delivers_serialized_operation(tmp_path):
    queue = DossierOutbox(_cfg(tmp_path / "dossier.sqlite"))
    run = _run()
    queue.enqueue(
        "write_documents",
        run,
        label="documents",
        payload={"documents": [{"document_id": "files:42"}]},
    )

    calls = []

    class Archive:
        async def write_documents(self, received_run, *, documents):
            calls.append((received_run, documents))

    assert await queue.deliver_one(Archive()) is True
    assert calls[0][0].run_path == run.run_path
    assert calls[0][1] == [{"document_id": "files:42"}]
    assert queue.stats()["completed"] == 1


def test_dossier_outbox_recovers_running_job_after_restart(tmp_path):
    path = tmp_path / "dossier.sqlite"
    queue = DossierOutbox(_cfg(path))
    job_id = queue.enqueue(
        "write_documents",
        _run(),
        payload={"documents": []},
    )
    assert job_id is not None

    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE dossier_jobs SET status='running', attempts=1 WHERE id=?",
            (job_id,),
        )

    recovered = DossierOutbox(_cfg(path))
    assert recovered.stats()["pending"] == 1
    assert recovered.stats()["running"] == 0


def test_dossier_outbox_preserves_order_inside_one_run(tmp_path):
    queue = DossierOutbox(_cfg(tmp_path / "dossier.sqlite"))
    run = _run()
    first = queue.enqueue(
        "persist_run_start",
        run,
        payload={"request_id": "q1", "question": "x", "model_id": "sunaq-standard"},
    )
    second = queue.enqueue(
        "write_documents",
        run,
        payload={"documents": []},
    )
    assert first and second and first < second

    row = queue._claim_next()
    assert row is not None
    assert int(row["id"]) == first

    # While the first job is running, the later job in the same dossier must
    # not be claimed by another worker/process.
    assert queue._claim_next() is None


def test_dossier_outbox_serializes_different_runs_of_same_conversation(tmp_path):
    queue = DossierOutbox(_cfg(tmp_path / "dossier.sqlite"))
    first_run = _run("run-a")
    second_run = _run("run-b")
    first = queue.enqueue(
        "persist_run_start",
        first_run,
        payload={"request_id": "q1", "question": "x", "model_id": "sunaq-standard"},
    )
    second = queue.enqueue(
        "persist_run_start",
        second_run,
        payload={"request_id": "q2", "question": "y", "model_id": "sunaq-standard"},
    )
    assert first and second

    claimed = queue._claim_next()
    assert claimed is not None and int(claimed["id"]) == first
    assert queue._claim_next() is None


def test_dossier_outbox_allows_other_dossier_while_first_is_blocked(tmp_path):
    queue = DossierOutbox(_cfg(tmp_path / "dossier.sqlite"))
    first = queue.enqueue(
        "persist_run_start",
        _run("run-a"),
        payload={"request_id": "q1", "question": "x", "model_id": "sunaq-standard"},
    )
    second_run = ResearchRun(
        conversation_id="chat-ijklmnop",
        run_id="run-b",
        dossier_path="SunaQ-Chats/chat-ijklmnop",
        run_path="SunaQ-Chats/chat-ijklmnop/runs/run-b",
        scoped_user_id="client::bob",
        created_at="2026-10-11T12:00:01+00:00",
    )
    second = queue.enqueue(
        "persist_run_start",
        second_run,
        payload={"request_id": "q2", "question": "y", "model_id": "sunaq-standard"},
    )
    assert first and second

    row1 = queue._claim_next()
    row2 = queue._claim_next()
    assert row1 is not None and int(row1["id"]) == first
    assert row2 is not None and int(row2["id"]) == second


@pytest.mark.asyncio
async def test_dossier_outbox_prunes_completed_history(tmp_path):
    cfg = _cfg(tmp_path / "dossier.sqlite")
    cfg["dossier_writer"]["completed_retention_jobs"] = 2
    queue = DossierOutbox(cfg)

    class Archive:
        async def write_documents(self, received_run, *, documents):
            return None

    for index in range(4):
        run = ResearchRun(
            conversation_id=f"chat-prune{index:02d}xxxx",
            run_id=f"run-{index}",
            dossier_path=f"SunaQ-Chats/chat-prune{index:02d}xxxx",
            run_path=f"SunaQ-Chats/chat-prune{index:02d}xxxx/runs/run-{index}",
            scoped_user_id="client::alice",
            created_at="2026-10-11T12:00:00+00:00",
        )
        queue.enqueue("write_documents", run, payload={"documents": []})
        assert await queue.deliver_one(Archive()) is True

    assert queue.stats()["completed"] == 2


@pytest.mark.asyncio
async def test_dossier_outbox_rejects_tampered_operation(tmp_path):
    path = tmp_path / "dossier.sqlite"
    queue = DossierOutbox(_cfg(path))
    job_id = queue.enqueue("write_documents", _run(), payload={"documents": []})
    assert job_id is not None
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE dossier_jobs SET operation='_credential' WHERE id=?",
            (job_id,),
        )

    class Archive:
        pass

    assert await queue.deliver_one(Archive()) is True
    assert queue.stats()["dead"] == 1


@pytest.mark.asyncio
async def test_dossier_outbox_rejects_tampered_run_path(tmp_path):
    path = tmp_path / "dossier.sqlite"
    queue = DossierOutbox(_cfg(path))
    job_id = queue.enqueue("write_documents", _run(), payload={"documents": []})
    assert job_id is not None

    with sqlite3.connect(path) as con:
        row = con.execute(
            "SELECT run_json FROM dossier_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["run_path"] = "../escape"
        con.execute(
            "UPDATE dossier_jobs SET run_json=? WHERE id=?",
            (json.dumps(payload), job_id),
        )

    class Archive:
        pass

    assert await queue.deliver_one(Archive()) is True
    assert queue.stats()["dead"] == 1


@pytest.mark.asyncio
async def test_dossier_outbox_retries_transient_http_failure(tmp_path):
    path = tmp_path / "dossier.sqlite"
    queue = DossierOutbox(_cfg(path))
    queue.enqueue("write_documents", _run(), payload={"documents": []})

    class Archive:
        async def write_documents(self, received_run, *, documents):
            request = httpx.Request("PUT", "https://nextcloud.example/dav/file")
            response = httpx.Response(423, request=request)
            raise httpx.HTTPStatusError(
                "locked",
                request=request,
                response=response,
            )

    assert await queue.deliver_one(Archive()) is True
    assert queue.stats()["pending"] == 1
    assert queue.stats()["dead"] == 0
