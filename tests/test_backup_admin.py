from __future__ import annotations

import sqlite3
from pathlib import Path

from rag.backup_admin import checkpoint_root, inventory, verify_root
from rag.credential_store import CredentialStore
from rag.secret_crypto import generate_master_key


def _sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO sample(value) VALUES ('ok')")


def _write_config(root: Path) -> None:
    (root / "config.yaml").write_text(
        """
auth:
  credential_store: runtime/users.sqlite
source_registry:
  path: runtime/source_registry.sqlite
graph_queue:
  database: runtime/graph_queue.sqlite
""".lstrip(),
        encoding="utf-8",
    )
    (root / "provider.env").write_text(
        "RESEARCH_LOG_ENABLED=true\nRESEARCH_LOG_DB=research.sqlite\n",
        encoding="utf-8",
    )


def test_inventory_collects_operational_state_and_sqlite(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    _write_config(root)
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n"
        "RAG_CREDENTIAL_ENCRYPTION=required\n",
        encoding="utf-8",
    )
    key = root / "runtime/credential-master.key"
    generate_master_key(key)
    monkeypatch.setenv("RAG_CREDENTIAL_MASTER_KEY_FILE", str(key))
    monkeypatch.setenv("RAG_CREDENTIAL_ENCRYPTION", "required")
    CredentialStore(root / "runtime/users.sqlite").set_credential(
        "client::alice", "nextcloud", "alice", "secret"
    )
    _sqlite(root / "runtime/source_registry.sqlite")
    _sqlite(root / "runtime/graph_queue.sqlite")
    _sqlite(root / "research.sqlite")

    state = inventory(root)

    assert state["blocking_external_paths"] == []
    assert state["credential_store"]["relative"] == "runtime/users.sqlite"
    assert state["credential_master_key"]["relative"] == "runtime/credential-master.key"
    assert set(state["sqlite"]) >= {
        "runtime/users.sqlite",
        "runtime/source_registry.sqlite",
        "runtime/graph_queue.sqlite",
        "research.sqlite",
    }
    assert "runtime/credential-master.key" in state["archive_paths"]


def test_absolute_paths_are_remapped_when_verifying_extracted_backup(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    _write_config(root)
    source_prefix = Path("/opt/nextcloud-rag")
    (root / "runtime.env").write_text(
        f"RAG_CREDENTIAL_MASTER_KEY_FILE={source_prefix}/runtime/credential-master.key\n"
        "RAG_CREDENTIAL_ENCRYPTION=required\n",
        encoding="utf-8",
    )
    key = root / "runtime/credential-master.key"
    generate_master_key(key)
    monkeypatch.setenv("RAG_CREDENTIAL_MASTER_KEY_FILE", str(key))
    monkeypatch.setenv("RAG_CREDENTIAL_ENCRYPTION", "required")
    CredentialStore(root / "runtime/users.sqlite").set_credential(
        "client::alice", "nextcloud", "alice", "secret"
    )

    report = verify_root(root, source_prefix=source_prefix)

    assert report["ok"] is True
    assert report["credential_report"]["ok"] is True


def test_verify_root_detects_wrong_credential_key(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    _write_config(root)
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n"
        "RAG_CREDENTIAL_ENCRYPTION=required\n",
        encoding="utf-8",
    )
    key = root / "runtime/credential-master.key"
    generate_master_key(key)
    monkeypatch.setenv("RAG_CREDENTIAL_MASTER_KEY_FILE", str(key))
    monkeypatch.setenv("RAG_CREDENTIAL_ENCRYPTION", "required")
    CredentialStore(root / "runtime/users.sqlite").set_credential(
        "client::alice", "nextcloud", "alice", "secret"
    )

    generate_master_key(key, force=True)
    report = verify_root(root)

    assert report["ok"] is False
    assert any("credential verification" in error for error in report["errors"])


def test_checkpoint_root_checkpoints_discovered_wal_databases(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    _write_config(root)
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    db = root / "runtime/source_registry.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO sample(value) VALUES ('ok')")

    report = checkpoint_root(root)

    assert report["ok"] is True
    assert "runtime/source_registry.sqlite" in report["sqlite_checked"]


def test_inventory_flags_required_state_outside_install_prefix(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    _write_config(root)
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=/etc/aki/credential-master.key\n",
        encoding="utf-8",
    )
    _sqlite(root / "runtime/users.sqlite")

    state = inventory(root)

    assert state["blocking_external_paths"] == [
        {"kind": "credential_master_key", "path": "/etc/aki/credential-master.key"}
    ]


def test_inventory_maps_super_light_container_ca_path_to_host_runtime(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime/ca").mkdir(parents=True)
    (root / "config.yaml").write_text(
        "nextcloud:\n  ca_file: /app/runtime/ca/nextcloud-ca-bundle.pem\n"
        "auth:\n  credential_store: runtime/users.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    (root / "runtime/ca/nextcloud-ca-bundle.pem").write_text("test-ca", encoding="utf-8")
    _sqlite(root / "runtime/users.sqlite")

    state = inventory(root, source_prefix=Path("/opt/nextcloud-rag"))

    assert "runtime/ca/nextcloud-ca-bundle.pem" in state["archive_paths"]
    assert {"kind": "ca_file", "path": "/app/runtime/ca/nextcloud-ca-bundle.pem"} not in state["external_paths"]


def test_inventory_includes_internal_ca_and_reports_external_ca(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime/ca").mkdir(parents=True)
    (root / "runtime").mkdir(exist_ok=True)
    (root / "config.yaml").write_text(
        "nextcloud:\n  ca_file: runtime/ca/private.pem\n"
        "elasticsearch:\n  ca_file: /etc/ssl/private/external-ca.pem\n"
        "auth:\n  credential_store: runtime/users.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    (root / "runtime/ca/private.pem").write_text("test-ca", encoding="utf-8")
    _sqlite(root / "runtime/users.sqlite")

    state = inventory(root)

    assert "runtime/ca/private.pem" in state["archive_paths"]
    assert {"kind": "ca_file", "path": "/etc/ssl/private/external-ca.pem"} in state["external_paths"]


def test_inventory_rejects_super_light_ca_alias_escape(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime/ca").mkdir(parents=True)
    (root / "config.yaml").write_text(
        "nextcloud:\n  ca_file: /app/runtime/ca/../../secret.pem\n"
        "auth:\n  credential_store: runtime/users.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    (root / "secret.pem").write_text("must-not-be-archived-as-ca", encoding="utf-8")
    _sqlite(root / "runtime/users.sqlite")

    state = inventory(root, source_prefix=Path("/opt/nextcloud-rag"))

    assert "secret.pem" not in state["archive_paths"]
    assert {"kind": "ca_file", "path": "/app/secret.pem"} in state["external_paths"]


def test_inventory_includes_current_operator_owned_state(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    (root / "models/standard").mkdir(parents=True)
    (root / "install/dockerized").mkdir(parents=True)
    (root / "install/nginx").mkdir(parents=True)
    (root / "runtime/retrieval-records").mkdir(parents=True)
    _write_config(root)
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    (root / ".sunaq-installation").write_text("SUNAQ_INSTALLATION=1\n", encoding="utf-8")
    (root / "models/standard/profile.yaml").write_text("description: custom\n", encoding="utf-8")
    (root / "install/dockerized/.env").write_text("LOCAL=1\n", encoding="utf-8")
    (root / "install/dockerized/docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "install/nginx/internal-auth.conf").write_text("proxy_set_header X-AKI-Internal-Key test;\n", encoding="utf-8")
    (root / "runtime/worker-control.json").write_text('{"version":1,"workers":{"sync_worker":true}}\n', encoding="utf-8")
    (root / "runtime/retrieval-records/test.json").write_text("{}\n", encoding="utf-8")

    state = inventory(root)

    for expected in (
        ".sunaq-installation",
        "models",
        "install/dockerized/.env",
        "install/dockerized/docker-compose.override.yml",
        "install/nginx/internal-auth.conf",
        "runtime/worker-control.json",
        "runtime/retrieval-records",
    ):
        assert expected in state["archive_paths"]


def test_inventory_excludes_qdrant_sync_state_and_includes_configured_mail_state(
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    (root / "data").mkdir()
    (root / "config.yaml").write_text(
        "auth:\n"
        "  credential_store: runtime/users.sqlite\n"
        "sync:\n"
        "  state_db: runtime/state.sqlite\n"
        "mail:\n"
        "  state_file: data/mail.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    _sqlite(root / "runtime/users.sqlite")
    _sqlite(root / "runtime/state.sqlite")
    _sqlite(root / "data/mail.sqlite")

    state = inventory(root)

    assert state["qdrant_sync_state"]["relative"] == "runtime/state.sqlite"
    assert state["qdrant_sync_state"]["included"] is False
    assert "runtime/state.sqlite" not in state["sqlite"]
    assert "runtime/state.sqlite" not in state["archive_paths"]
    assert "data/mail.sqlite" in state["sqlite"]
    assert "data/mail.sqlite" in state["archive_paths"]


def test_inventory_blocks_external_non_rebuildable_sqlite_state(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    (root / "config.yaml").write_text(
        "auth:\n"
        "  credential_store: runtime/users.sqlite\n"
        "mail:\n"
        "  state_file: /srv/sunaq-state/mail.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    _sqlite(root / "runtime/users.sqlite")

    state = inventory(root)

    assert {"kind": "mail_state", "path": "/srv/sunaq-state/mail.sqlite"} in state["blocking_external_paths"]


def test_inventory_includes_dossier_outbox_and_blocks_external_path(tmp_path: Path) -> None:
    root = tmp_path
    (root / "runtime").mkdir()
    (root / "config.yaml").write_text(
        "auth:\n"
        "  credential_store: runtime/users.sqlite\n"
        "dossier_writer:\n"
        "  database: runtime/dossier_queue.sqlite\n",
        encoding="utf-8",
    )
    (root / "runtime.env").write_text(
        "RAG_CREDENTIAL_MASTER_KEY_FILE=runtime/credential-master.key\n",
        encoding="utf-8",
    )
    _sqlite(root / "runtime/users.sqlite")
    _sqlite(root / "runtime/dossier_queue.sqlite")

    state = inventory(root)
    assert "runtime/dossier_queue.sqlite" in state["sqlite"]
    assert "runtime/dossier_queue.sqlite" in state["archive_paths"]

    (root / "config.yaml").write_text(
        "auth:\n"
        "  credential_store: runtime/users.sqlite\n"
        "dossier_writer:\n"
        "  database: /srv/sunaq-state/dossier.sqlite\n",
        encoding="utf-8",
    )
    state = inventory(root)
    assert {
        "kind": "dossier_writer",
        "path": "/srv/sunaq-state/dossier.sqlite",
    } in state["blocking_external_paths"]
