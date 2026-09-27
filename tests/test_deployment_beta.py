from pathlib import Path
import subprocess

import yaml

from rag.sunaq_models import load_model_registry


ROOT = Path(__file__).resolve().parent.parent


def _standard_installer_text() -> str:
    return (ROOT / "install/profiles/install-standard.sh").read_text()




def test_unified_installer_dispatches_profiles():
    installer = (ROOT / "install/install.sh").read_text()
    assert '--profile' in installer
    assert 'install-standard.sh' in installer
    assert 'install-super-light.sh' in installer
    super_impl = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert 'Host Python required:    no' in super_impl
    assert 'WITH_OPENWEBUI=0' in super_impl
    assert 'WITH_PROXY=0' in super_impl


def test_profile_installers_are_executable():
    for path in (
        ROOT / "install/profiles/install-standard.sh",
        ROOT / "install/profiles/install-super-light.sh",
    ):
        assert path.stat().st_mode & 0o111, f"{path} must be executable in the Git checkout"


def test_super_light_compose_is_legacy_compatible_shape():
    cfg = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    assert cfg["version"] == "2.4"
    services = cfg["services"]
    assert "proxy" not in services
    assert "openwebui" not in services
    assert "depends_on" not in services["provider"]
    assert services["provider"]["command"] == ["python", "-m", "rag.provider_entrypoint"]


def test_systemd_normal_services_honor_maintenance_gate():
    for name in ("rag-api", "rag-graph-worker", "rag-sync-worker", "rag-mail-worker"):
        unit = (ROOT / "install/systemd" / f"{name}.service.in").read_text()
        assert "ExecCondition=" in unit
        assert "RAG_MAINTENANCE_MODE" in unit
    provider = (ROOT / "install/systemd/rag-provider.service.in").read_text()
    assert "ExecCondition=" not in provider


def test_maintenance_mode_is_shipped_and_defaulted_on_install():
    standard = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    runtime = (ROOT / "install/runtime.env.example").read_text()
    super_runtime = (ROOT / "install/super-light/runtime.env.super-light.example").read_text()
    start_all = (ROOT / "start-all.sh").read_text()
    provider_start = (ROOT / "start-openwebui-provider.sh").read_text()
    helper = (ROOT / "install/maintenance-mode.sh").read_text()

    assert "RAG_MAINTENANCE_MODE=true" in runtime
    assert "RAG_MAINTENANCE_MODE=true" in super_runtime
    assert "RAG_MAINTENANCE_MODE=true" in standard
    assert "set_runtime_env_value RAG_MAINTENANCE_MODE true" in super_light
    assert "Maintenance mode is enabled: API/workers are intentionally not started." in start_all
    assert "rag.provider_entrypoint" in provider_start
    assert "on|off|status" in helper
    assert "Starting super-light maintenance endpoint" in super_light


def test_super_light_container_build_does_not_require_root_provider_example():
    dockerfile = (ROOT / "install/super-light/Dockerfile.provider").read_text()
    assert "COPY provider.env.example" not in dockerfile
    assert "PIP_ROOT_USER_ACTION=ignore" in dockerfile


def test_super_light_plan_reports_disk_and_keeps_openwebui_opt_in():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert "Estimated disk use:" in installer
    assert "not pulled/not started" in installer
    assert '[[ $WITH_OPENWEBUI -eq 1 ]] && SERVICES+=(openwebui)' in installer
    assert 'WITH_PLAYWRIGHT=0' in installer
    assert '--with-playwright' in installer
    assert 'compose build playwright-renderer' in installer

def test_compose_minimal_default_is_proxy_only():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    services = cfg["services"]
    default_services = [name for name, spec in services.items() if not spec.get("profiles")]
    assert default_services == ["proxy"]
    assert services["qdrant"]["profiles"] == ["qdrant"]
    assert services["neo4j"]["profiles"] == ["neo4j"]


def test_standard_full_does_not_implicitly_enable_playwright():
    installer = _standard_installer_text()
    full_line = next(line for line in installer.splitlines() if "--full)" in line)
    assert "WITH_PLAYWRIGHT" not in full_line
    assert "PLAYWRIGHT_EXPLICIT" not in full_line


def test_standard_playwright_renderer_is_optional_compose_service():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    renderer = cfg["services"]["playwright-renderer"]
    assert renderer["profiles"] == ["renderer"]
    assert renderer["build"]["context"] == "./components/playwright-renderer"
    assert renderer["ports"] == ["127.0.0.1:${PLAYWRIGHT_PORT:-8090}:8080"]
    assert "playwright_state:/state" in renderer["volumes"]
    installer = _standard_installer_text()
    assert 'archive.renderer.enabled' in installer
    assert '--with-playwright' in installer
    assert '--no-playwright' in installer
    assert "PLAYWRIGHT_EXPLICIT=1" in installer
    assert "web.setdefault('archive', {}).setdefault('renderer', {})['enabled'] = playwright_enabled" in installer
    assert '--profile renderer build playwright-renderer' in installer
    assert '--profile renderer up -d playwright-renderer' in installer
    assert 'LOCAL_PLAYWRIGHT=$PLAYWRIGHT_ENABLED' in installer


def test_standard_disabled_playwright_does_not_require_generated_seccomp_file():
    compose = (ROOT / "install/docker-compose.yml").read_text(encoding="utf-8")
    installer = _standard_installer_text()
    assert 'seccomp=${PLAYWRIGHT_SECCOMP_PROFILE:-unconfined}' in compose
    assert 'PLAYWRIGHT_SECCOMP_PROFILE="unconfined"' in installer
    assert 'PLAYWRIGHT_SECCOMP_PROFILE="./components/playwright-renderer/seccomp_profile.json"' in installer
    prepare = '"$PREFIX/install/components/playwright-renderer/prepare.sh"'
    prepare_pos = installer.index(prepare)
    runtime_compose = 'compose_cmd -f docker-compose.yml --env-file .env'
    assert prepare_pos < installer.index(runtime_compose, prepare_pos)
    assert installer.count(prepare) == 1


def test_bind_mounts_are_selinux_relabelled():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    proxy_mounts = cfg["services"]["proxy"]["volumes"]
    assert all(m.endswith(":ro,z") for m in proxy_mounts)



def test_standard_defaults_to_no_reranker_and_no_model_download():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    standard = load_model_registry(cfg).get("sunaq-standard")
    assert standard.section("reranker")["backend"] == "none"
    installer = _standard_installer_text()
    assert "DOWNLOAD_RERANKER=0" in installer
    assert "--with-reranker-download" in installer
    assert "reranker opt-in" in installer


def test_optional_external_services_are_not_bundled():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    services = cfg["services"]
    assert "searxng" not in services
    assert "valkey" not in services
    assert "ollama" not in services
    installer = _standard_installer_text()
    assert "--with-searxng was removed from the bundled stack" in installer
    assert "Bundled Ollama is not part of this release" in installer
    assert "WITH_OLLAMA" not in installer


def test_evidence_controller_is_off_by_default():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    standard = load_model_registry(cfg).get("sunaq-standard")
    assert standard.section("evidence_control")["mode"] == "off"
    env = (ROOT / "provider.env.example").read_text()
    assert "Legacy fallback only; the active SunaQ model is canonical." in env
    assert "EVIDENCE_DECISION_MODE=off" in env
    provider = (ROOT / "rag/openai_provider.py").read_text()
    assert 'PROVIDER_CONFIG.get("evidence_control")' in provider
    assert 'os.getenv("EVIDENCE_DECISION_MODE", "off")' in provider


def test_openwebui_connection_forwards_stable_user_identity():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    env = cfg["services"]["openwebui"]["environment"]
    assert env["ENABLE_FORWARD_USER_INFO_HEADERS"] == "false"
    assert "X-OpenWebUI-User-Id" in env["OPENAI_API_CONFIGS"]
    assert "{{USER_ID}}" in env["OPENAI_API_CONFIGS"]


def test_openwebui_root_assets_are_not_rate_limited():
    nginx = (ROOT / "install/nginx/nginx-openwebui.conf").read_text()
    root_block = nginx.split("location / {", 1)[1].split("}", 1)[0]
    assert "limit_req" not in root_block


def test_v1_rate_limits_restore_real_client_only_from_trusted_loopback_proxy():
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert "set_real_ip_from 127.0.0.1;" in nginx
        assert "set_real_ip_from ::1;" in nginx
        assert "real_ip_header X-Forwarded-For;" in nginx
        assert "real_ip_recursive on;" in nginx
        assert "zone=rag_v1:10m rate=10r/s" in nginx
        assert "limit_conn rag_v1_conn 16;" in nginx


def test_rag_api_explicitly_forwards_test_identity_header():
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert "proxy_set_header X-RAG-User-ID $http_x_rag_user_id;" in nginx


def test_openwebui_provider_config_is_installer_authoritative():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    env = cfg["services"]["openwebui"]["environment"]
    assert env["ENABLE_PERSISTENT_CONFIG"] == "false"
    assert env["BYPASS_MODEL_ACCESS_CONTROL"] == "true"
    assert env["ENABLE_EVALUATION_ARENA_MODELS"] == "false"
    assert env["OPENAI_API_KEY"] == "${OPENWEBUI_PROVIDER_API_KEY:-}"


def test_sync_graph_discovery_is_opt_in_by_default():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    standard = load_model_registry(cfg).get("sunaq-standard")
    assert cfg["sync"]["graph_queue"]["enabled"] is False
    assert "structural_relations" not in standard.section("graph_retrieval")
    sync_source = (ROOT / "rag/sync.py").read_text()
    assert 'cfg_get(cfg, "sync.graph_queue.enabled", default=False)' in sync_source


def test_optional_relation_date_properties_use_dynamic_access():
    graph_source = (ROOT / "rag/graph.py").read_text()
    assert "c.evidence_date_confidence AS evidence_date_confidence" not in graph_source
    assert "properties(c)['evidence_date_confidence'] AS evidence_date_confidence" in graph_source


def test_graph_answer_hook_is_cited_only_not_sync_coupled():
    provider = (ROOT / "rag/openai_provider.py").read_text()
    assert provider.count('evidence_action="answer_cited"') == 2
    assert "explicit_cited_results = _cited_results(answer, results)" in provider


def test_docker_and_ml_beta_dependencies_are_pinned():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    images = {name: spec["image"] for name, spec in cfg["services"].items()}
    assert "latest" not in "\n".join(images.values())
    assert set(images) == {"proxy", "qdrant", "neo4j", "openwebui", "playwright-renderer"}
    external_images = [
        image
        for name, image in images.items()
        if "build" not in cfg["services"][name]
    ]
    assert all("@sha256:" in image for image in external_images)
    assert images["playwright-renderer"] == "rag-playwright-renderer:0.2.3"
    renderer_dockerfile = (ROOT / "install/components/playwright-renderer/Dockerfile").read_text()
    assert "PIP_ROOT_USER_ACTION=ignore" in renderer_dockerfile
    assert "PIP_DISABLE_PIP_VERSION_CHECK=1" in renderer_dockerfile
    assert "v1.19.0@sha256:" in images["qdrant"]
    assert "5.26.29-community@sha256:" in images["neo4j"]
    assert "v0.11.4-slim@sha256:" in images["openwebui"]
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text())
    for name in ("nginx", "qdrant", "neo4j", "openwebui"):
        assert lock["docker"][name]["ref"].endswith(lock["docker"][name]["digest"])
        assert "@sha256:" in lock["docker"][name]["ref"]
    req = (ROOT / "requirements.txt").read_text()
    assert "transformers==4.57.6" in req
    installer = _standard_installer_text()
    assert "torch==2.13.0+cpu" in installer
    assert "import torch.export" in installer


def test_provider_auth_scopes_user_id_to_authenticated_client():
    provider = (ROOT / "rag/openai_provider.py").read_text()
    assert "authenticate_client(api_key)" in provider
    assert "scope_identity(client_id" in provider
    assert "authorization != f\"Bearer {PROVIDER_API_KEY}\"" not in provider


def test_beta_defaults_are_multiuser_and_tls_verified():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert cfg["acl"]["enabled"] is True
    assert cfg["acl"]["identity_mode"] == "credential_store"
    assert cfg["nextcloud"]["verify_tls"] is True
    assert cfg["nextcloud"]["ca_file"] == ""
    assert "verify_tls" not in cfg["acl"]
    assert "verify_tls" not in cfg["auth"]
    assert "verify_tls" not in cfg["carddav"]
    installer = _standard_installer_text()
    assert "MULTI_USER=1" in installer
    assert "--single-user" in installer
    assert "--acl-off" in installer


def test_installers_support_scoped_private_nextcloud_ca_without_global_env_override():
    standard = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    for installer in (standard, super_light):
        assert "--ca-certificate" in installer
        assert "nextcloud-ca-bundle.pem" in installer
    assert "REQUESTS_CA_BUNDLE=" not in standard
    assert "SSL_CERT_FILE=" not in standard


def test_fresh_baseline_keeps_optional_research_layers_disabled():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    web = yaml.safe_load((ROOT / "web.yaml").read_text())
    main = (ROOT / "clients/nextcloud/sunaq/templates/main.php").read_text()

    assert cfg["mail"]["enabled"] is False
    assert cfg["mail"]["worker"]["enabled"] is False
    assert cfg["research_findings"]["enabled"] is False
    assert cfg["chat_archive"]["enabled"] is False
    assert web["enabled"] is False
    assert web["archive"]["enabled"] is False
    assert 'value="documents" checked' in main
    assert 'value="mailarchive" checked' not in main


def test_per_user_mail_and_web_are_not_shipped_in_yaml():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    web = yaml.safe_load((ROOT / "web.yaml").read_text())
    assert "accounts" not in (cfg.get("mail") or {})
    assert "webdav" not in (cfg.get("mail") or {})
    assert "root" not in (web.get("archive") or {})


def test_runtime_does_not_start_mail_worker_when_feature_is_disabled():
    start = (ROOT / "start-all.sh").read_text()
    maintenance = (ROOT / "install/maintenance-mode.sh").read_text()
    standard = _standard_installer_text()

    assert 'mail_enabled="$("$BASE_DIR/.venv/bin/python"' in start
    assert 'if [[ "$mail_enabled" == "1" ]]' in start
    assert 'skipped (mail feature/worker disabled)' in start
    assert 'if systemctl is-enabled rag-mail-worker' in maintenance
    assert 'if mail_worker_enabled; then' in maintenance
    assert 'compose up -d neo4j playwright-renderer api mail-worker' not in maintenance
    assert 'MAIL_WORKER_ENABLED=' in standard
    assert 'systemctl disable rag-mail-worker' in standard


def test_installer_ships_per_user_mail_worker_without_global_mail_secret_envs():
    installer = _standard_installer_text()
    runtime_example = (ROOT / "install/runtime.env.example").read_text()
    assert "start-mail-worker.sh" in installer
    assert "rag-mail-worker" in installer
    assert "MAIL_WORKER_ENABLED" in installer
    assert "systemctl disable rag-mail-worker" in installer
    assert "start-sync-worker.sh" in installer
    assert "rag-sync-worker" in installer
    assert "MAIL_IMAP_USERNAME" not in installer
    assert "MAIL_IMAP_PASSWORD" not in installer
    assert "MAIL_IMAP_USERNAME" not in runtime_example
    assert "MAIL_IMAP_PASSWORD" not in runtime_example


def test_proxy_defaults_to_https_and_reserves_root_for_ui():
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert "listen 443 ssl default_server;" in nginx
        assert "return 308 https://$host$request_uri;" in nginx
        assert "location = /proxy-health" in nginx
        assert "location /rag-admin/" in nginx
        assert "location /admin/" not in nginx
    no_ui = (ROOT / "install/nginx/nginx.conf").read_text()
    assert "return 302 /rag-admin/;" in no_ui
    ui = (ROOT / "install/nginx/nginx-openwebui.conf").read_text()
    assert "proxy_pass http://127.0.0.1:3000;" in ui


def test_public_provider_api_has_dedicated_request_and_connection_limits():
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert "zone=rag_v1:10m rate=10r/s" in nginx
        assert "limit_conn_zone $binary_remote_addr zone=rag_v1_conn:10m;" in nginx
        v1 = nginx.split("location /v1/ {", 1)[1].split("}", 1)[0]
        assert "limit_req zone=rag_v1 burst=30 nodelay;" in v1
        assert "limit_req_status 429;" in v1
        assert "limit_conn rag_v1_conn 16;" in v1
        assert "limit_conn_status 429;" in v1


def test_proxy_mounts_bootstrap_tls_material():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    mounts = cfg["services"]["proxy"]["volumes"]
    assert "./nginx/tls:/etc/nginx/tls:ro,z" in mounts
    installer = _standard_installer_text()
    assert "openssl req -x509" in installer
    assert "Firewall is NOT modified" in installer


def test_elasticsearch_password_is_env_referenced_not_yaml_secret():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    es = cfg["elasticsearch"]
    assert es["password_env"] == "ELASTICSEARCH_PASSWORD"
    assert "password" not in es
    assert es["verify_tls"] is True
    runtime = (ROOT / "install/runtime.env.example").read_text()
    assert "ELASTICSEARCH_PASSWORD=" in runtime


def test_bundled_openwebui_uses_loopback_provider_without_tls_coupling():
    cfg = yaml.safe_load((ROOT / "install/docker-compose.yml").read_text())
    spec = cfg["services"]["openwebui"]
    env = spec["environment"]
    assert spec["network_mode"] == "host"
    assert "ports" not in spec
    assert "extra_hosts" not in spec
    assert env["HOST"] == "127.0.0.1"
    assert env["PORT"] == "${OPENWEBUI_PORT:-3000}"
    assert env["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8766/v1"
    # Do not weaken OpenWebUI's global outbound TLS verification merely to
    # reach the local provider.
    assert "AIOHTTP_CLIENT_SESSION_SSL" not in env


def test_rag_api_root_redirects_to_health():
    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert "location = /rag-api {" in nginx
        assert "location = /rag-api/ {" in nginx
        assert nginx.count("return 302 /rag-api/health;") >= 2


def test_openwebui_followup_helper_is_suppressed_without_llm():
    provider = (ROOT / "rag/openai_provider.py").read_text()
    assert 'if auxiliary_kind == "ui:follow_ups":' in provider
    assert "return _static_response('{\"follow_ups\":[]}'" in provider


def test_installer_waits_for_bundled_ui_and_proxy_health():
    installer = _standard_installer_text()
    status = (ROOT / "status.sh").read_text()
    smoke = (ROOT / "install/smoke-test.sh").read_text()
    assert 'Waiting for OpenWebUI on 127.0.0.1:${OPENWEBUI_PORT:-3000}' in installer
    assert 'http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health' in installer
    assert '"https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health"' in installer
    assert 'LOCAL_OPENWEBUI=0' in status
    assert 'openwebui "${OPENWEBUI_PORT:-3000}"' in status
    assert 'OpenWebUI HTTP (127.0.0.1:${OPENWEBUI_PORT:-3000})' in smoke


def test_sync_and_reset_do_not_use_qdrant_filter_delete():
    sync_source = (ROOT / "rag/sync.py").read_text()
    reset_source = (ROOT / "reset-rag.sh").read_text()
    assert 'json={"filter": {"must": [{"key": "document_id"' not in sync_source
    assert '"filter"' not in sync_source.split("def qdrant_delete_chunk_range", 1)[1].split("def qdrant_upsert", 1)[0]
    assert '/points/delete?wait=true' not in reset_source
    assert '-X DELETE' in reset_source


def test_periodic_sync_worker_wraps_existing_rag_sync():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    worker = cfg["sync_worker"]
    assert worker["enabled"] is True
    assert worker["poll_interval_seconds"] >= 60
    assert worker["max_documents"] == 0
    assert worker["enqueue_graph"] is False
    script = (ROOT / "start-sync-worker.sh").read_text()
    assert "-m rag.sync" in script
    assert "--max-documents" in script
    assert "--no-enqueue-graph" in script




def test_super_light_core_services_use_generated_non_secret_env_only():
    cfg = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    for name in ("api", "provider"):
        assert cfg["services"][name]["env_file"] == ["../../runtime.container.env"]
    assert "mail-worker" not in cfg["services"]
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert '  mail-worker:' in installer
    assert 'MAIL_WORKER_ACTIVE' in installer
    sync = (ROOT / "install/sync-container-secrets.sh").read_text()
    assert "RAG_PROVIDER_INTERNAL_KEY" in sync
    assert "*_API_KEY|*_PASSWORD|*_TOKEN|*_SECRET" in sync
    assert "runtime.container.env" in sync
    assert "service-secrets" in sync
    loader = (ROOT / "install/load-service-env.sh").read_text()
    assert "runtime.service.env" in loader
    assert "WARNING: runtime.service.env missing" in loader


def test_final_architecture_validation_runs_after_installer_overrides():
    standard = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    for installer in (standard, super_light):
        assert "--validate-only" in installer
        assert 'Validating final SRC/ERG architecture configuration' in installer
    assert "X509_STRICT_EXPLICIT=1" in standard
    assert "X509_STRICT_EXPLICIT=1" in super_light
    assert "ELASTICSEARCH_INDEX_EXPLICIT=1" in super_light


def test_rc_line_documents_destructive_erg_to_src_graph_reset():
    graph = (ROOT / "rag/graph.py").read_text()
    docs = (ROOT / "docs/SRC-ERG.md").read_text()
    assert 'sub.add_parser("reset"' in graph
    assert 'MATCH (n) DETACH DELETE n' in graph
    assert "--yes-really-delete-all" in docs

def test_release_repository_hygiene():
    assert (ROOT / "rag/version.py").read_text().strip() == 'VERSION = "0.8.6-rc1.2"'
    assert not (ROOT / "provider.env").exists()
    assert "provider.env" in (ROOT / ".gitignore").read_text().splitlines()
    assert (ROOT / "CHANGELOG.md").exists()
    assert (ROOT / "SECURITY.md").exists()
    assert (ROOT / "docs/ARCHITECTURE.md").exists()
    assert not list(ROOT.glob("MIGRATION-*.md"))
    assert not list(ROOT.glob("DESIGN-*.md"))


def test_installer_creates_local_provider_env_from_example():
    installer = _standard_installer_text()
    assert 'cp -a "$SOURCE_DIR/provider.env.example" "$PREFIX/provider.env"' in installer
    assert "provider.env.example README.md CHANGELOG.md SECURITY.md" in installer


def test_admin_navigation_uses_four_primary_areas_and_graph_subnav():
    base = (ROOT / "rag/templates/admin/base.html").read_text()
    for label in ("Übersicht", "Benutzer", "Graph", "Sicherheit"):
        assert f">{label}</a>" in base
    assert ">Identitäts-Kandidaten</a>" in base
    assert ">Beobachtungen</a>" in base
    assert ">Relationen</a>" in base
    assert ">Kontaktquellen</a>" in base
    assert ">Graph-Import / Queue</a>" in base
    assert ">/health</a>" not in base


def test_installer_never_executes_state_from_unrecognized_prefix(tmp_path):
    prefix = tmp_path / "foreign"
    state_dir = prefix / "install"
    state_dir.mkdir(parents=True)
    sentinel = tmp_path / "executed"
    (state_dir / "install-state.env").write_text(
        "DEPLOYMENT_PROFILE=standard\n"
        f"LOCAL_NEO4J=$(touch {sentinel})\n"
    )

    result = subprocess.run(
        ["bash", str(ROOT / "install/profiles/install-standard.sh"), "--plan", "--prefix", str(prefix)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "not recognized as a SunaQ installation" in result.stderr
    assert not sentinel.exists()


def test_installer_parses_recognized_state_as_data_on_rerun(tmp_path):
    prefix = tmp_path / "aki"
    (prefix / "install").mkdir(parents=True)
    (prefix / "rag").mkdir()
    (prefix / "config.yaml").write_text("{}\n")
    (prefix / ".aki-rag-installation").write_text(
        "AKI_RAG_INSTALLATION=1\nDEPLOYMENT_PROFILE=standard\nDEPLOYMENT_MODE=native\n"
    )
    (prefix / "install/install-state.env").write_text(
        "LOCAL_QDRANT=0\n"
        "LOCAL_NEO4J=1\n"
        "LOCAL_OPENWEBUI=0\n"
        "LOCAL_PROXY=1\n"
        "PROXY_BASIC_AUTH_STATE=1\n"
        "MALICIOUS=$(touch /tmp/aki-rag-state-must-not-run)\n"
    )

    result = subprocess.run(
        ["bash", str(ROOT / "install/profiles/install-standard.sh"), "--plan", "--prefix", str(prefix)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Neo4j:                   install/start" in result.stdout
    assert 'source "$PREFIX/install/install-state.env"' not in _standard_installer_text()


def test_super_light_rerun_preserves_admin_credentials_and_proxy_identity():
    script = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert 'ADMIN_PASSWORD="$(sed -n \'s/^RAG_ADMIN_PASSWORD=//p\'' in script
    assert 'if [[ -z "$ADMIN_PASSWORD" || "$ADMIN_PASSWORD" == "replace-me" ]]' in script
    assert 'printf \'%s:%s\\n\' "${ADMIN_USER:-admin}"' in script
    assert "printf 'admin:%s\\n'" not in script


def test_research_finding_runtime_recovery_retries_full_neo4j_schema_upgrade():
    source = (Path(__file__).resolve().parents[1] / "rag" / "api.py").read_text()
    marker = "if not _research_finding_schema_ready:"
    start = source.index(marker, source.index("def graph_research_findings"))
    block = source[start:start + 500]
    assert "graph.ensure_schema()" in block
    assert "graph.ensure_research_finding_schema()" not in block


def test_standard_installer_common_connection_and_proxy_options_are_supported(tmp_path):
    result = subprocess.run(
        [
            "bash", str(ROOT / "install/profiles/install-standard.sh"),
            "--plan",
            "--prefix", str(tmp_path / "fresh"),
            "--nextcloud-url", "https://cloud.example/nextcloud",
            "--elasticsearch-url", "http://10.0.0.20:9200",
            "--elasticsearch-index", "my_index",
            "--with-proxy",
            "--proxy-http-port", "81",
            "--proxy-https-port", "444",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Nextcloud URL override:   https://cloud.example/nextcloud" in result.stdout
    assert "Elasticsearch URL:       http://10.0.0.20:9200" in result.stdout
    assert "Elasticsearch index:     my_index" in result.stdout
    assert "HTTPS 444 + HTTP redirect on 81" in result.stdout


def test_standard_rerun_can_explicitly_disable_inherited_openwebui(tmp_path):
    prefix = tmp_path / "aki"
    (prefix / "install").mkdir(parents=True)
    (prefix / "rag").mkdir()
    (prefix / "config.yaml").write_text("{}\n")
    (prefix / ".aki-rag-installation").write_text(
        "AKI_RAG_INSTALLATION=1\nDEPLOYMENT_PROFILE=standard\nDEPLOYMENT_MODE=native\n"
    )
    (prefix / "install/install-state.env").write_text(
        "DEPLOYMENT_PROFILE=standard\n"
        "LOCAL_QDRANT=0\n"
        "LOCAL_NEO4J=0\n"
        "LOCAL_OPENWEBUI=1\n"
        "LOCAL_PROXY=1\n"
        "PROXY_HTTP_PORT=80\n"
        "PROXY_HTTPS_PORT=443\n"
        "PROXY_BASIC_AUTH_STATE=1\n"
    )

    inherited = subprocess.run(
        ["bash", str(ROOT / "install/profiles/install-standard.sh"), "--plan", "--prefix", str(prefix)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert inherited.returncode == 0, inherited.stderr
    assert (
        "OpenWebUI:                install/start "
        "(retained from existing install; use --no-openwebui to disable)"
    ) in inherited.stdout

    disabled = subprocess.run(
        [
            "bash", str(ROOT / "install/profiles/install-standard.sh"),
            "--plan", "--prefix", str(prefix), "--no-openwebui",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    assert "OpenWebUI:                external/skip" in disabled.stdout


def test_common_optional_frontend_proxy_switches_exist_in_both_profiles():
    standard = (ROOT / "install/profiles/install-standard.sh").read_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    common = {
        "--nextcloud-url", "--elasticsearch-url", "--elasticsearch-index",
        "--with-openwebui", "--no-openwebui", "--with-proxy", "--no-proxy",
        "--with-playwright", "--no-playwright",
        "--proxy-http-port", "--proxy-https-port",
        "--x509-strict", "--no-x509-strict", "--plan",
    }
    for option in common:
        assert option in standard, option
        assert option in super_light, option


def test_legacy_prefix_is_preferred_when_new_default_prefix_is_empty():
    standard = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    for source in (standard, super_light):
        assert '[[ ! -e "$PREFIX" ]] || [[ -d "$PREFIX" && -z "$(find "$PREFIX"' in source
        assert 'PREFIX="$LEGACY_PREFIX"' in source


def test_standard_rerun_preflight_detects_existing_and_running_services():
    installer = _standard_installer_text()
    assert '[INFO] Existing SunaQ installation detected at $PREFIX.' in installer
    assert 'for name in api provider graph-worker sync-worker mail-worker' in installer
    assert 'systemctl is-active --quiet "$unit"' in installer
    assert 'ps --services --filter status=running' in installer
    assert 'Could not inspect the existing Docker Compose stack' in installer
    assert '[WARN] Existing SunaQ services are running:' in installer
    assert 'no installation changes were made' in installer.lower()
    assert installer.index("preflight_existing_install") < installer.index("confirm_plan", installer.index("preflight_existing_install"))


def test_standard_neo4j_schema_init_runs_from_application_root_with_progress():
    installer = _standard_installer_text()
    marker = 'log "Waiting for Neo4j and applying the idempotent SunaQ schema upgrade"'
    start = installer.index(marker)
    block = installer[start:start + 2600]
    assert 'cd "$PREFIX"' in block
    assert '"$PREFIX/.venv/bin/python" -m rag.graph --config "$PREFIX/config.yaml" init' in block
    assert "Neo4j/schema initialization still waiting" in block
    assert "SunaQ schema upgrade completed after" in block


def test_standard_proxy_health_uses_configured_https_port():
    installer = _standard_installer_text()
    assert '"https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health"' in installer
    assert 'did not become healthy on HTTPS ${PROXY_HTTPS_PORT}' in installer


def test_internal_api_key_is_installer_managed_and_proxy_injected():
    installer = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    compose = (ROOT / "install/docker-compose.yml").read_text()
    for source in (installer, super_light):
        assert "RAG_INTERNAL_API_KEY" in source
        assert "RAG_PROVIDER_INTERNAL_KEY" in source
        assert "internal-auth.conf" in source
        assert 'proxy_set_header X-AKI-Internal-Key "%s";' in source
    assert "./nginx/internal-auth.conf:/etc/nginx/internal-auth.conf:ro,z" in compose

    for name in ("nginx.conf", "nginx-openwebui.conf"):
        nginx = (ROOT / "install/nginx" / name).read_text()
        assert nginx.count("include /etc/nginx/internal-auth.conf;") >= 5
        assert "X-AKI-Provider-Key" not in nginx


def test_direct_api_health_tools_send_internal_machine_key():
    status = (ROOT / "status.sh").read_text()
    smoke = (ROOT / "install/smoke-test.sh").read_text()
    for source in (status, smoke):
        assert "X-AKI-Internal-Key" in source
        assert "RAG_INTERNAL_API_KEY" in source


def test_native_api_refuses_accidental_non_loopback_bind():
    start_api = (ROOT / "start-api.sh").read_text()
    assert "RAG_ALLOW_REMOTE_INTERNAL_API" in start_api
    assert "Refusing non-loopback RAG_API_HOST" in start_api
    assert "127.0.0.1|::1|localhost" in start_api


def test_standard_rerun_repairs_missing_compose_env_before_preflight():
    installer = _standard_installer_text()
    compose_check = installer.index('if [[ ${#existing_compose[@]} -gt 0 && -f "$PREFIX/install/docker-compose.yml" ]]')
    env_repair = installer.index('Recreating missing install/.env from .env.example for rerun preflight.')
    compose_ps = installer.index('--env-file .env ps --services --filter status=running')
    assert compose_check < env_repair < compose_ps


def test_standard_rerun_repair_uses_actual_service_primary_group():
    installer = _standard_installer_text()
    assert 'env_group="$(id -gn "$RAG_USER" 2>/dev/null)"' in installer
    assert 'chown "$RAG_USER:$env_group" "$PREFIX/install/.env"' in installer


def test_standard_runtime_internal_keys_replace_placeholders():
    installer = _standard_installer_text()
    assert '[[ -n "$current" && "$current" != "replace-me" ]]' in installer
    assert '[[ -n "$RAG_INTERNAL_API_KEY" && "$RAG_INTERNAL_API_KEY" != "replace-me" ]]' in installer
    assert '[[ -n "$RAG_PROVIDER_INTERNAL_KEY" && "$RAG_PROVIDER_INTERNAL_KEY" != "replace-me" ]]' in installer



def test_native_maintenance_refreshes_generated_secret_environment():
    helper = (ROOT / "install/maintenance-mode.sh").read_text()
    enable = helper.split("enable_native() {", 1)[1].split("disable_native() {", 1)[0]
    disable = helper.split("disable_native() {", 1)[1].split("state_enabled() {", 1)[0]
    assert "sync_container_secrets" in enable
    assert "sync_container_secrets" in disable
    assert "runtime.service.env" in (ROOT / "install/load-service-env.sh").read_text()


def test_installer_config_changes_are_rollback_protected_until_validation():
    standard = _standard_installer_text()
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text()
    for installer in (standard, super_light):
        assert 'CONFIG_TX_ACTIVE=1' in installer
        assert 'trap rollback_config_transaction EXIT' in installer
        assert 'previous config.yaml/web.yaml restored' in installer
        validation = installer.index("Validating final SRC/ERG architecture configuration")
        commit = installer.index("CONFIG_TX_ACTIVE=0", validation)
        assert validation < commit



def test_super_light_preset_uses_writable_runtime_staging_not_readonly_config():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    compose = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    provider_mounts = compose["services"]["provider"]["volumes"]
    assert "../../config.yaml:/app/config.yaml:ro" in provider_mounts
    assert "--output /app/runtime/install-config.yaml" in installer
    assert 'cp "$PRESET_OUTPUT" "$PREFIX/config.yaml"' in installer
    preset_block = installer.split('log "Applying capability preset:', 1)[1].split(
        '# Explicit installer arguments remain authoritative', 1
    )[0]
    assert "--config /app/config.yaml" in preset_block
    assert "--preset /app/runtime/install-preset.yaml" in preset_block
    assert "--output /app/runtime/install-config.yaml" in preset_block



def test_runtime_templates_document_role_llm_key_fallbacks_without_legacy_single_user_credentials():
    for path in (
        ROOT / "install/runtime.env.example",
        ROOT / "install/super-light/runtime.env.super-light.example",
    ):
        text = path.read_text()
        for key in (
            "LLM_API_KEY=",
            "PLANNER_LLM_API_KEY=",
            "VERIFIER_LLM_API_KEY=",
            "EVIDENCE_LLM_API_KEY=",
            "ANSWER_LLM_API_KEY=",
        ):
            assert key in text
        assert "falls back to LLM_API_KEY" in text
        assert "NEXTCLOUD_USERNAME=" not in text
        assert "NEXTCLOUD_APP_PASSWORD=" not in text


def test_super_light_completion_message_separates_provider_routing_and_secrets():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert "Configure LLM backend/model routing in:" in installer
    assert "$PREFIX/provider.env" in installer
    assert "Keep LLM/API credentials and other secrets in:" in installer
    assert "$PREFIX/runtime.env" in installer
    assert "otherwise the role falls" in installer
    assert "back to LLM_API_KEY." in installer
    assert "Contact seeds:" not in installer


def test_provider_templates_do_not_duplicate_llm_api_secret():
    for path in (
        ROOT / "provider.env.example",
        ROOT / "install/super-light/provider.env.super-light.example",
    ):
        lines = path.read_text().splitlines()
        assert "LLM_API_KEY=" not in lines
        assert any("runtime.env" in line and "LLM_API_KEY" in line for line in lines)



def test_super_light_completion_mentions_reverse_proxy_tls_replacement():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert "$PREFIX/install/nginx/tls/server.crt" in installer
    assert "$PREFIX/install/nginx/tls/server.key" in installer
    assert "docker compose restart proxy" in installer
    assert "docker-compose restart proxy" in installer



def test_smoke_test_requires_configured_llm_roles_to_be_healthy():
    smoke = (ROOT / "install/smoke-test.sh").read_text()
    assert ".llm.status" in smoke
    assert 'ok "LLM roles reachable"' in smoke
    assert 'bad "LLM roles not fully reachable' in smoke
    assert ".llm.roles" in smoke
    assert ".maintenance // false" in smoke
    assert "LLM role health deferred while provider is in maintenance mode." in smoke



def test_super_light_playwright_is_not_in_base_compose_and_is_generated_only_when_selected():
    cfg = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    assert "playwright-renderer" not in cfg["services"]
    assert "playwright_state" not in (cfg.get("volumes") or {})

    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    assert 'if [[ $WITH_PLAYWRIGHT -eq 1 || $WITH_OPENWEBUI -eq 1 || $WITH_PROXY -eq 1 || $MAIL_WORKER_ACTIVE -eq 1 ]]; then' in installer
    assert "  playwright-renderer:" in installer
    assert "  playwright_state:" in installer



def test_super_light_final_architecture_validation_does_not_require_compose_run():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    marker = 'log "Validating final SRC/ERG architecture configuration"'
    block = installer.split(marker, 1)[1].split('CONFIG_TX_ACTIVE=0', 1)[0]
    assert "docker run --rm" in block
    assert "python -m rag.config_preset --config /app/config.yaml --validate-only" in block
    assert "compose run --rm --no-deps provider" not in block
    assert "SUNAQ_RUNTIME_IMAGE" in block



def test_super_light_mail_worker_is_generated_only_from_final_enabled_config():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text()
    base = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    assert "mail-worker" not in base["services"]
    assert "mail_worker_enabled_from_config" in installer
    assert "MAIL_WORKER_ACTIVE=0" in installer
    assert "mail_worker_enabled_from_config && MAIL_WORKER_ACTIVE=1" in installer
    validation = installer.index('log "Validating final SRC/ERG architecture configuration"')
    mail_override = installer.index('  mail-worker:')
    assert validation < mail_override
