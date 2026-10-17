from __future__ import annotations

from pathlib import Path
import subprocess


def test_backup_restore_shell_is_syntax_valid() -> None:
    script = Path("install/backup-restore.sh")
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_backup_restore_contract_is_conservative() -> None:
    text = Path("install/backup-restore.sh").read_text(encoding="utf-8")
    assert "require_maintenance" in text
    assert "restore BACKUP --yes" in text
    assert "QDRANT_INCLUDED=0" in text
    assert "QDRANT_SYNC_STATE_INCLUDED=0" in text
    assert "drop_qdrant_sync_state" in text
    assert "normalize_restored_ownership" in text
    assert "neo4j-data.tar" in text
    assert "NEXTCLOUD_INCLUDED=0" in text
    assert "ELASTICSEARCH_INCLUDED=0" in text
    assert ".sunaq-installation" in text
    assert "sunaq-backup-$timestamp" in text
    assert "maintenance-mode.sh\" on" in text


def test_dockerized_backup_helper_avoids_compose_run_host_network_bug() -> None:
    text = Path("install/backup-restore.sh").read_text(encoding="utf-8")
    helper = text[text.index("provider_image_id()"):text.index("current_version()")]
    assert "dockerized_backup_helper" in helper
    assert "docker run --rm --network none --user 0:0" in helper
    assert "run --rm -T --no-deps" not in helper


def test_backup_inventory_contract_covers_current_site_state() -> None:
    text = Path("rag/backup_admin.py").read_text(encoding="utf-8")
    for marker in (
        '"models"',
        '"install/dockerized/.env"',
        '"install/dockerized/docker-compose.override.yml"',
        '"install/nginx/internal-auth.conf"',
        '"runtime/worker-control.json"',
        '"runtime/retrieval-records"',
        '"mail_state"',
        '"qdrant_sync_state"',
    ):
        assert marker in text
