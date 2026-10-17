from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_exposes_worker_runtime_controls_without_host_service_privileges():
    admin = (ROOT / "rag/admin_ui.py").read_text(encoding="utf-8")
    overview = (ROOT / "rag/templates/admin/overview.html").read_text(encoding="utf-8")
    vector = (ROOT / "rag/templates/admin/vector.html").read_text(encoding="utf-8")

    assert '@router.post("/operations/workers/{worker_name}"' in admin
    assert "set_runtime_worker_enabled(" in admin
    assert "docker.sock" not in admin
    assert "systemctl" not in admin
    assert "admin_worker_control" in overview
    assert "admin_worker_control" in vector


def test_admin_has_read_only_system_diagnostics_and_vector_views():
    admin = (ROOT / "rag/admin_ui.py").read_text(encoding="utf-8")
    base = (ROOT / "rag/templates/admin/base.html").read_text(encoding="utf-8")
    system = (ROOT / "rag/templates/admin/system.html").read_text(encoding="utf-8")
    vector = (ROOT / "rag/templates/admin/vector.html").read_text(encoding="utf-8")

    assert '@router.get("/system"' in admin
    assert '@router.get("/vector"' in admin
    assert 'f"http://127.0.0.1:{provider_port}/live"' in admin
    assert "detail_timeout" in admin
    assert "admin_system" in base
    assert "admin_vector" in base
    assert "Retrieval-Arme" in system
    assert "LLM-Rollen" in system
    assert "Provider /live" in system
    assert "provider_live_probe" in system
    assert "Qdrant" in vector
    assert "read-only" in vector.lower()
    assert "Elasticsearch" in vector
    assert "Vektorisiert" in vector
    assert "Für Semantik zugelassen" in vector
    assert "HNSW-indexierte Vektoren" in vector
    assert "_elasticsearch_document_count" in admin
    assert "_sync_status_snapshot" in admin


def test_installers_seed_missing_workers_paused_but_do_not_overwrite_runtime_control():
    standard = (ROOT / "install/profiles/install-standard.sh").read_text(encoding="utf-8")
    dockerized = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    for installer in (standard, dockerized):
        assert 'worker-control.json' in installer
        assert '"sync_worker": false' in installer
        assert '"mail_worker": false' in installer
        assert '! -f "$PREFIX/runtime/worker-control.json"' in installer


def test_native_sync_launcher_uses_same_runtime_control_aware_worker_as_dockerized():
    launcher = (ROOT / "start-sync-worker.sh").read_text(encoding="utf-8")
    dockerized = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "-m rag.sync_worker" in launcher
    assert 'command: ["python", "-m", "rag.sync_worker"' in dockerized
