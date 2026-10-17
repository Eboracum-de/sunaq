from pathlib import Path

import pytest
import yaml

import rag.mail_worker as mail_worker
from rag.runtime_control import set_worker_enabled, snapshot, worker_enabled


def test_runtime_worker_control_defaults_to_paused_when_file_is_absent(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n", encoding="utf-8")
    assert worker_enabled("sync_worker", config_path=cfg) is False
    assert worker_enabled("mail_worker", config_path=cfg) is False


def test_runtime_worker_control_persists_independent_worker_switches(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n", encoding="utf-8")

    set_worker_enabled("sync_worker", False, config_path=cfg)
    assert snapshot(config_path=cfg) == {
        "mail_worker": False,
        "sync_worker": False,
    }

    set_worker_enabled("mail_worker", False, config_path=cfg)
    assert snapshot(config_path=cfg) == {
        "mail_worker": False,
        "sync_worker": False,
    }

    set_worker_enabled("sync_worker", True, config_path=cfg)
    assert snapshot(config_path=cfg)["sync_worker"] is True
    assert (tmp_path / "runtime" / "worker-control.json").stat().st_mode & 0o777 == 0o600


def test_unknown_runtime_worker_is_rejected(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        set_worker_enabled("graph_worker", True, config_path=cfg)


def test_mail_worker_runtime_pause_blocks_sync_without_changing_capability_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "mail": {
                "enabled": True,
                "worker": {"enabled": True, "poll_interval_seconds": 60},
            }
        }),
        encoding="utf-8",
    )
    set_worker_enabled("mail_worker", False, config_path=cfg)
    called = []
    monkeypatch.setattr(mail_worker, "_run_sync_once", lambda path: called.append(path) or 0)

    assert mail_worker.run_worker(cfg, once=True, idle_poll_seconds=1) == 0
    assert called == []
