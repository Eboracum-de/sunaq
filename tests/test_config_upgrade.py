from pathlib import Path

import yaml

from rag.config_upgrade import upgrade_file, upgrade_text


def test_upgrade_materializes_safe_sync_defaults_without_enabling_features():
    source = """# keep this comment
deployment:
  mode: dockerized
qdrant:
  enabled: true
sync_worker:
  enabled: true
  poll_interval_seconds: 300
sync:
  chunk_size: 3000
"""
    rendered, changes = upgrade_text(source, deployment_mode="dockerized")
    cfg = yaml.safe_load(rendered)

    assert "# keep this comment" in rendered
    assert cfg["sync_worker"]["pause_check_documents"] == 25
    assert cfg["sync"]["state_db"] == "runtime/state.sqlite"
    assert cfg["sync"]["embedding_deferred_file"] == "runtime/embedding-deferred.jsonl"
    assert cfg["sync"]["status_file"] == "runtime/sync-status.json"
    assert cfg["dossier_writer"]["enabled"] is True
    assert cfg["dossier_writer"]["database"] == "runtime/dossier_queue.sqlite"
    assert cfg["dossier_writer"]["max_attempts"] == 0
    assert "pause_check_documents" in " ".join(changes)
    assert any("dossier_writer.database" in item for item in changes)


def test_upgrade_migrates_legacy_top_level_state_db_for_dockerized():
    source = """deployment:
  mode: dockerized
state_db: state.sqlite
sync_worker:
  enabled: true
sync:
  chunk_size: 3000
"""
    rendered, changes = upgrade_text(source)
    cfg = yaml.safe_load(rendered)

    assert "state_db" not in {key for key in cfg if key != "sync"}
    assert cfg["sync"]["state_db"] == "runtime/state.sqlite"
    assert any("migrated legacy state_db" in item for item in changes)


def test_upgrade_preserves_custom_legacy_state_path_on_standard():
    source = """deployment:
  mode: native
state_db: /srv/sunaq/custom-state.sqlite
sync_worker:
  enabled: true
sync:
  chunk_size: 3000
"""
    rendered, _ = upgrade_text(source)
    cfg = yaml.safe_load(rendered)

    assert "state_db" not in cfg
    assert cfg["sync"]["state_db"] == "/srv/sunaq/custom-state.sqlite"


def test_upgrade_does_not_override_explicit_operator_values():
    source = """deployment:
  mode: dockerized
dossier_writer:
  enabled: false
  database: runtime/site-dossier.sqlite
  poll_seconds: 1.5
  retry_seconds: 5
  max_retry_seconds: 120
  max_attempts: 7
  completed_retention_jobs: 25
sync_worker:
  enabled: true
  pause_check_documents: 50
sync:
  state_db: runtime/site-state.sqlite
  embedding_deferred_file: runtime/site-deferred.jsonl
  status_file: runtime/site-status.json
"""
    rendered, changes = upgrade_text(source)
    cfg = yaml.safe_load(rendered)

    assert changes == []
    assert cfg["sync_worker"]["pause_check_documents"] == 50
    assert cfg["sync"]["state_db"] == "runtime/site-state.sqlite"
    assert cfg["sync"]["status_file"] == "runtime/site-status.json"
    assert cfg["dossier_writer"]["enabled"] is False
    assert cfg["dossier_writer"]["database"] == "runtime/site-dossier.sqlite"
    assert cfg["dossier_writer"]["max_attempts"] == 7


def test_upgrade_file_supports_staged_output(tmp_path: Path):
    source = tmp_path / "config.yaml"
    target = tmp_path / "upgraded.yaml"
    source.write_text(
        "deployment:\n  mode: dockerized\nsync_worker:\n  enabled: true\nsync:\n  chunk_size: 3000\n",
        encoding="utf-8",
    )
    changes = upgrade_file(source, output_path=target, deployment_mode="dockerized")

    assert changes
    assert "pause_check_documents: 25" not in source.read_text(encoding="utf-8")
    assert "pause_check_documents: 25" in target.read_text(encoding="utf-8")
