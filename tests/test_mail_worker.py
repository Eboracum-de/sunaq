from pathlib import Path

import yaml

import rag.mail_worker as worker
from rag.runtime_control import set_worker_enabled


def _write(path: Path, data: dict):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_worker_config_requires_both_mail_and_worker_enabled(tmp_path):
    cfg = tmp_path / "config.yaml"
    _write(cfg, {"mail": {"enabled": True, "worker": {"enabled": True, "poll_interval_seconds": 123}}})
    assert worker._load_mail_worker_config(cfg) == (True, 123)
    _write(cfg, {"mail": {"enabled": True, "worker": {"enabled": False}}})
    assert worker._load_mail_worker_config(cfg)[0] is False


def test_worker_once_disabled_is_clean_noop(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write(cfg, {"mail": {"enabled": False, "worker": {"enabled": True}}})
    called = []
    monkeypatch.setattr(worker, "_run_sync_once", lambda path: called.append(path) or 0)
    assert worker.run_worker(cfg, once=True, idle_poll_seconds=1) == 0
    assert called == []


def test_worker_once_enabled_runs_same_mail_sync_path_after_runtime_release(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write(cfg, {"mail": {"enabled": True, "worker": {"enabled": True, "poll_interval_seconds": 60}}})
    set_worker_enabled("mail_worker", True, config_path=cfg)
    called = []
    monkeypatch.setattr(worker, "_run_sync_once", lambda path: called.append(path) or 0)
    assert worker.run_worker(cfg, once=True) == 0
    assert called == [cfg]
