from pathlib import Path

import yaml

import rag.sync_worker as worker
from rag.sync_worker import load_worker_settings


def test_sync_worker_requires_both_worker_and_qdrant_enabled(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "qdrant": {"enabled": True},
        "sync_worker": {
            "enabled": True,
            "poll_interval_seconds": 10,
            "max_documents": 12,
            "enqueue_graph": True,
        },
    }))
    enabled, interval, max_documents, enqueue_graph, pause_check_documents = load_worker_settings(path)
    assert enabled is True
    assert interval == 60
    assert max_documents == 12
    assert enqueue_graph is True
    assert pause_check_documents == 25

    path.write_text(yaml.safe_dump({
        "qdrant": {"enabled": False},
        "sync_worker": {"enabled": True},
    }))
    assert load_worker_settings(path)[0] is False


def test_sync_worker_missing_runtime_control_is_paused(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "qdrant": {"enabled": True},
        "sync_worker": {
            "enabled": True,
            "poll_interval_seconds": 60,
            "max_documents": 0,
            "enqueue_graph": False,
        },
    }), encoding="utf-8")
    called = []
    monkeypatch.setattr(worker, "run_once", lambda *args, **kwargs: called.append((args, kwargs)) or 0)
    monkeypatch.setattr(worker.sys, "argv", ["sync_worker", "--config", str(path), "--once"])

    assert worker.main() == 0
    assert called == []
