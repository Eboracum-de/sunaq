import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from rag.research_archive import ResearchArchive


def _archive_stub():
    archive = object.__new__(ResearchArchive)
    return archive


def test_research_archive_accepts_normal_uuid_conversation_key():
    value = "550e8400-e29b-41d4-a716-446655440000"
    assert ResearchArchive._conversation_key(value) == value


@pytest.mark.parametrize("value", [
    "",
    "../escape",
    "contains/slash",
    "x" * 65,
])
def test_research_archive_rejects_unsafe_conversation_key(value):
    with pytest.raises(ValueError):
        ResearchArchive._conversation_key(value)


def test_research_run_id_is_filesystem_safe():
    value = ResearchArchive._run_id("chatcmpl-test/unsafe:value")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", value)
    assert "chatcmpl-test-unsafe-value" in value

def test_research_dossier_has_bounded_web_evidence_record():
    root = Path(__file__).resolve().parent.parent
    archive_source = (root / "rag" / "research_archive.py").read_text(encoding="utf-8")
    provider_source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")

    assert "async def write_web_evidence(" in archive_source
    assert '"schema": "sunaq.web_evidence.v1"' in archive_source
    assert 'target = f"{web_path}/evidence.json"' in archive_source
    assert '"type": "web.evidence"' in archive_source
    assert "evidence_text" in archive_source
    assert "[:16000]" in archive_source

    assert provider_source.count('"write_web_evidence"') >= 2
    assert '"web_evidence_dossier"' in provider_source
    assert "_enqueue_dossier_write" in provider_source


def test_research_run_reservation_is_local_and_persistence_preserves_existing_trace():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "research_archive.py").read_text(encoding="utf-8")
    reserve = source.split("def reserve_run(", 1)[1].split("async def persist_run_start(", 1)[0]
    persist = source.split("async def persist_run_start(", 1)[1].split("async def start_run(", 1)[0]
    compatibility = source.split("async def start_run(", 1)[1].split("async def write_resolver(", 1)[0]

    assert "httpx.AsyncClient" not in reserve
    assert "return ResearchRun(" in reserve
    assert "created_at=datetime.now(timezone.utc).isoformat()" in reserve

    assert "await self._ensure_run_dir(" in persist
    assert "await self._dav_request(" in persist
    assert "started_line + existing" in persist
    assert "await asyncio.gather(" not in persist
    assert persist.count("await self._put(") >= 2

    assert "self.reserve_run(" in compatibility
    assert "await self.persist_run_start(" in compatibility

    helper = source.split("async def _ensure_run_dir(", 1)[1].split("async def _put(", 1)[0]
    assert 'runs_path = f"{dossier}/runs"' in helper
    assert "if response.status_code == 409:" in helper


@pytest.mark.asyncio
async def test_research_archive_retries_transient_dav_423(monkeypatch):
    archive = _archive_stub()
    archive.dav_base = "https://nextcloud.example/remote.php/dav/files"
    credential = SimpleNamespace(username="alice", password="secret")
    sleeps = []

    class LockingClient:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, **kwargs):
            self.calls += 1
            status = 423 if self.calls < 3 else 201
            return httpx.Response(status, request=httpx.Request(method, url))

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("rag.nextcloud_writer.asyncio.sleep", fake_sleep)
    client = LockingClient()
    response = await archive._dav_request(
        client,
        "MKCOL",
        credential,
        "SunaQ-Chats/chat-test/runs",
    )

    assert response.status_code == 201
    assert client.calls == 3
    assert sleeps == [0.15, 0.35]


def test_run_root_writers_materialize_reserved_run_before_put():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "research_archive.py").read_text(encoding="utf-8")

    decision = source.split("async def write_retrieval_decision(", 1)[1].split(
        "async def write_retrieval_execution(", 1
    )[0]
    execution = source.split("async def write_retrieval_execution(", 1)[1].split(
        "async def write_external_source(", 1
    )[0]
    markdown = source.split("async def write_research_markdown(", 1)[1].split(
        "async def write_web_evidence(", 1
    )[0]
    documents = source.split("async def write_documents(", 1)[1]

    for writer in (decision, execution, markdown, documents):
        ensure = writer.index("await self._ensure_run_dir(")
        put = writer.index("await self._put(")
        assert ensure < put


def test_provider_queues_provenance_after_research_run_start():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")

    run_start = source.index('"research_run_start"')
    for label in (
        '"retrieval_decision_dossier"',
        '"retrieval_execution_dossier"',
        '"research_documents_manifest"',
        '"register_research_markdown"',
    ):
        assert source.index(label) > run_start

    assert "await _research_archive().write_retrieval_decision(" not in source
    assert "await _research_archive().write_documents(" not in source
    assert "await _research_archive().write_research_markdown(" not in source


def test_web_archive_finalize_is_a_post_response_barrier():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")

    helper = source.split("def _add_post_response_barrier(", 1)[1].split(
        "async def _web_finalize_archive(", 1
    )[0]
    assert "background_tasks.tasks.insert(" in helper
    assert "0," in helper
    assert "BackgroundTask(" in helper
    assert "_run_post_response_task" in helper

    for label in (
        '"web_archive_finalize"',
        '"automatic_web_archive_finalize"',
        '"automatic_web_archive_finalize_stream"',
    ):
        position = source.index(label)
        prefix = source[max(0, position - 300):position]
        assert "_add_post_response_barrier(" in prefix


def test_research_archive_uses_decision_v2_and_post_acl_execution_contract():
    root = Path(__file__).resolve().parent.parent
    archive_source = (root / "rag" / "research_archive.py").read_text(encoding="utf-8")
    provider_source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")

    assert 'retrieval-decision.v2.json' in archive_source
    assert '"sunaq.retrieval_decision.v2"' in archive_source
    assert 'retrieval-execution.v1.json' in archive_source
    assert '"sunaq.retrieval_execution.v1"' in archive_source
    assert '"post_live_acl"' in archive_source

    assert "def _authorized_document_records(" in provider_source
    assert '"authorized_rank"' in provider_source
    assert '"elasticsearch_rank"' not in provider_source.split(
        "def _authorized_document_records(", 1
    )[1].split("def _authorized_verifier_records(", 1)[0]
    assert '"rrf_rank"' not in provider_source.split(
        "def _authorized_document_records(", 1
    )[1].split("def _authorized_verifier_records(", 1)[0]



def test_deferred_web_archive_finalization_waits_for_its_run_not_fixed_30_seconds():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "web_research.py").read_text(encoding="utf-8")
    assert "self._archive_ready: dict[str, asyncio.Event] = {}" in source
    assert "self._archive_ready.setdefault(directory, asyncio.Event())" in source
    assert "await asyncio.wait_for(" in source
    assert "readiness_deadline = time.monotonic() + self.finalize_wait_timeout" in source
    assert "timeout=max(0.1, readiness_deadline - time.monotonic())" in source
    assert "store.mark_archive_ready(str(plan.get(\"run_path\") or \"\"))" in source
    assert "min(max(self.timeout, 1.0), 30.0)" not in source


def test_provider_commits_research_run_to_durable_outbox_before_response():
    root = Path(__file__).resolve().parent.parent
    source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    marker = "Research dossier run reserved:"
    start = source.index("research_run_reserved_at = time.perf_counter()")
    end = source.index("options, think, logged_parameters", start)
    block = source[start:end]

    assert "archive.reserve_run(" in block
    assert "_enqueue_dossier_write(" in block
    assert '"persist_run_start"' in block
    assert '"research_run_start"' in block
    assert "archive.persist_run_start" not in block
    assert "research_run_reserve_ms" in block
    assert marker in block
