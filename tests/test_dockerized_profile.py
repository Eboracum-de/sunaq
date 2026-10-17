from pathlib import Path
import subprocess

import pytest
import yaml

from rag.reranker import Reranker
from rag.sunaq_models import load_model_registry


ROOT = Path(__file__).resolve().parent.parent


def test_dockerized_base_defaults_do_not_override_model_capabilities():
    cfg = yaml.safe_load((ROOT / "install/dockerized/config.dockerized.yaml").read_text())
    registry = load_model_registry(cfg)
    assert cfg["deployment"]["profile"] == "dockerized"
    assert cfg["elasticsearch"]["enabled"] is True
    assert cfg["qdrant"]["enabled"] is False
    assert cfg["sync_worker"]["enabled"] is False
    assert cfg["neo4j"]["enabled"] is False
    assert cfg["graph_queue"]["enabled"] is False
    assert cfg["graph_queue"]["worker"]["enabled"] is False
    assert cfg["graph_indexer"]["enabled"] is False
    assert cfg["graph_entity_discovery"]["enabled"] is False
    assert cfg["graph_relation_discovery"]["enabled"] is False
    assert cfg["research_findings"]["enabled"] is False
    assert cfg["chat_archive"]["enabled"] is False
    assert cfg["mail"]["enabled"] is False
    assert cfg["mail"]["worker"]["enabled"] is False

    for model in registry.list():
        assert model.section("entity_resolution")["enabled"] is True
        # Deployment no longer disables graph/model capabilities. SRC request
        # policy still restricts effective retrieval arms to files.
        assert model.section("graph_retrieval")["enabled"] is True
        assert model.section("reranker")["backend"] == "none"


def test_mail_sync_is_erg_full_only():
    core = yaml.safe_load((ROOT / "install/presets/core.yaml").read_text())
    workgroup = yaml.safe_load((ROOT / "install/presets/workgroup.yaml").read_text())
    full = yaml.safe_load((ROOT / "install/presets/full.yaml").read_text())

    for preset in (core, workgroup):
        assert preset["mail"]["enabled"] is False
        assert preset["mail"]["worker"]["enabled"] is False
        assert preset["mail_metadata"]["enabled"] is False

    assert full["mail"]["enabled"] is True
    assert full["mail"]["worker"]["enabled"] is True
    assert full["mail_metadata"]["enabled"] is True


def test_provider_templates_default_to_local_llm_and_keep_model_settings_in_profiles():
    for relative in (
        "provider.env.example",
        "install/dockerized/provider.env.dockerized.example",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "LLM_BACKEND=ollama" in text
        assert "LLM_BASE_URL=http://127.0.0.1:11434" in text
        assert "LLM_MODEL=qwen3:8b" in text
        assert "PROVIDER_MODEL_ID=sunaq" in text
        assert "PROVIDER_MODEL_ID=nextcloud-hybrid-rag" not in text
        lines = set(text.splitlines())
        assert not any(line.startswith("CONTEXT_MAX_CHARS=") for line in lines)
        assert not any(line.startswith("RAG_ANSWER_PROMPT_FILE=") for line in lines)
        assert not any(line.startswith("MAX_RETRIEVAL_ROUNDS=") for line in lines)
        assert "models/*/profile.yaml" in text


def test_web_templates_use_sunaq_user_agent():
    for relative in ("web.yaml", "install/dockerized/web.dockerized.yaml"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "SunaQ-WebEvidence/0.8.7" in text
        assert "AKI-RAG-WebEvidence" not in text


def test_dockerized_requirements_include_data_clients_but_not_heavy_local_ml_stack():
    req = (ROOT / "requirements-dockerized.txt").read_text().lower()
    assert "qdrant-client==1.19.0" in req
    assert "neo4j==5.28.4" in req
    for forbidden in ("transformers", "huggingface", "sentencepiece", "torch"):
        assert forbidden not in req


def test_dockerized_web_profile_is_disabled_until_admin_opts_in():
    web = yaml.safe_load((ROOT / "install/dockerized/web.dockerized.yaml").read_text())
    renderer = web["archive"]["renderer"]
    assert web["enabled"] is False
    assert web["archive"]["enabled"] is False
    assert renderer["enabled"] is False
    assert renderer["url"] == "http://127.0.0.1:8090/render"
    assert renderer["cleanup"]["cookie_consent"] == "off"
    assert renderer["cleanup"]["dismiss_overlays"] is False
    assert renderer["cleanup"]["remove_overlays"] is False


def test_reranker_none_is_explicitly_disabled():
    reranker = Reranker(backend="none")
    reranker.load()
    status = reranker.status()
    assert status["backend"] == "none"
    assert status["disabled_reason"] == "disabled by configuration"
    with pytest.raises(RuntimeError, match="deaktiviert"):
        reranker.score("query", ["document"])


def test_search_skips_reranker_when_disabled(monkeypatch):
    from types import SimpleNamespace
    import rag.search as search

    monkeypatch.setattr(search, "RERANKER_ENABLED", False)
    monkeypatch.setattr(search, "ELASTICSEARCH_ENABLED", True)
    monkeypatch.setattr(search, "RETRIEVAL_SIGNAL_ENABLED", False)
    monkeypatch.setattr(
        search,
        "prepare_entity_context",
        lambda question: {"enabled": False, "entities": [], "elastic_phrase_expansion": [], "error": None},
    )
    monkeypatch.setattr(
        search,
        "create_plan",
        lambda *a, **k: SimpleNamespace(must=["examplehost"], phrases=[], entity_should_phrases=[]),
    )

    def fake_es(question, plan, diagnostics):
        diagnostics.update({"total_hits": 2, "total_relation": "eq", "available": True})
        return [
            {"document_id": "files:1", "title": "a.pdf", "path": "a.pdf", "score": 10.0, "text": "a", "snippet": "a"},
            {"document_id": "files:2", "title": "b.pdf", "path": "b.pdf", "score": 9.0, "text": "b", "snippet": "b"},
        ]

    monkeypatch.setattr(search, "elastic_search", fake_es)
    monkeypatch.setattr(
        search,
        "rerank_results",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reranker must not run")),
    )

    payload = search.perform_search(
        "examplehost 2025",
        limit=2,
        retrieval_arms={"files"},
        force_unspecific=True,
    )

    assert payload["retrieval_mode"] == "rrf_no_reranker"
    assert payload["reranker_used"] is False
    assert payload["reranker_error"] is None
    assert [item["document_id"] for item in payload["results"]] == ["files:1", "files:2"]


def test_dockerized_proxy_htpasswd_is_worker_readable():
    installer = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")
    assert 'chmod 644 "$PREFIX/install/nginx/htpasswd"' in installer
    assert 'chmod 600 "$PREFIX/install/nginx/htpasswd"' not in installer


def test_dockerized_web_capability_is_globally_off_by_default():
    web = yaml.safe_load((ROOT / "install/dockerized/web.dockerized.yaml").read_text())
    assert web["enabled"] is False
    assert web["archive"]["enabled"] is False
    assert web["search"]["provider"] == "brave"


def test_dockerized_base_compose_does_not_define_optional_frontends_or_renderer():
    compose = yaml.safe_load((ROOT / "install/dockerized/docker-compose.yml").read_text())
    services = compose["services"]
    assert "openwebui" not in services
    assert "proxy" not in services
    assert "playwright-renderer" not in services
    assert "mail-worker" not in services
    assert set(services) == {"api", "provider"}


def test_dockerized_installer_has_no_obsolete_model_packaging_hook():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "rag.model_packaging" not in installer
    assert "Synchronizing bundled SunaQ models with architecture tier" not in installer


def test_dockerized_runtime_images_use_current_rc1_tag():
    compose = yaml.safe_load((ROOT / "install/dockerized/docker-compose.yml").read_text())
    assert compose["services"]["api"]["image"] == "sunaq:0.8.7-rc1"
    assert compose["services"]["provider"]["image"] == "sunaq:0.8.7-rc1"

    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "sunaq:0.8.6-rc1.2" not in installer
    assert 'image: sunaq:0.8.7-rc1' in installer
    assert 'image: sunaq-erg-resolver:0.8.7-rc1' in installer


def test_dockerized_mounts_administrator_models_read_only():
    compose = yaml.safe_load((ROOT / "install/dockerized/docker-compose.yml").read_text())
    for service in ("api", "provider"):
        volumes = compose["services"][service]["volumes"]
        assert "../../models:/app/models:ro,z" in volumes


def test_dockerized_installer_generates_optional_compose_override():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert 'docker-compose.override.yml' in installer
    assert 'if [[ $WITH_PLAYWRIGHT -eq 1 ]]' in installer
    assert 'if [[ $WITH_OPENWEBUI -eq 1 ]]' in installer
    assert 'if [[ $WITH_PROXY -eq 1 ]]' in installer
    assert '  playwright-renderer:' in installer
    assert '  mail-worker:' in installer
    assert '  erg-resolver:' in installer
    assert '  neo4j:' in installer
    assert 'NEO4J_ACTIVE' in installer
    assert 'neo4j_enabled_from_config' in installer
    assert 'RESOLVER_ACTIVE' in installer
    assert 'MAIL_WORKER_ACTIVE' in installer
    assert 'mail_worker_enabled_from_config' in installer
    assert 'compose up -d --remove-orphans' in installer


def test_dockerized_disabled_playwright_does_not_require_generated_seccomp_file():
    compose = (ROOT / "install/dockerized/docker-compose.yml").read_text(encoding="utf-8")
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert 'playwright-renderer:' not in compose
    assert 'seccomp=${PLAYWRIGHT_SECCOMP_PROFILE:-unconfined}' in installer
    assert 'PLAYWRIGHT_SECCOMP_PROFILE=$([[ $WITH_PLAYWRIGHT -eq 1 ]]' in installer
    prepare = '"$PREFIX/install/components/playwright-renderer/prepare.sh"'
    first_compose = 'compose stop playwright-renderer'
    assert installer.index(prepare) < installer.index('compose build api provider')
    assert installer.index(prepare) < installer.index(first_compose)
    assert installer.count(prepare) == 1


def test_dockerized_marks_dockerized_deployment_mode():
    cfg = yaml.safe_load((ROOT / "install/dockerized/config.dockerized.yaml").read_text())
    assert cfg["deployment"]["profile"] == "dockerized"
    assert cfg["deployment"]["mode"] == "dockerized"


def test_dockerized_private_ca_is_baked_into_provider_image():
    dockerfile = (ROOT / "install/dockerized/Dockerfile.provider").read_text(encoding="utf-8")
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "--ca-certificate" in installer
    assert "runtime/ca" in installer
    assert "COPY runtime/ca/" in dockerfile
    assert "update-ca-certificates" in dockerfile
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in dockerfile
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in dockerfile


def test_dockerized_nextcloud_ca_uses_augmented_system_bundle():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "install/dockerized/Dockerfile.provider").read_text(encoding="utf-8")

    assert 'NEXTCLOUD_CA_FILE="/etc/ssl/certs/ca-certificates.crt"' in installer
    assert 'NEXTCLOUD_CA_FILE="/app/runtime/ca/nextcloud-ca-bundle.pem"' not in installer
    assert "COPY runtime/ca/ /usr/local/share/ca-certificates/rag-custom/" in dockerfile
    assert "RUN update-ca-certificates" in dockerfile


def test_docker_build_context_excludes_runtime_secrets_but_keeps_ca():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "runtime/*" in ignore
    assert "!runtime/ca/" in ignore
    assert "provider.env" in ignore
    assert "runtime.env" in ignore


def test_dockerized_retrieval_policy_keeps_es_required_and_document_graph_disabled():
    cfg = yaml.safe_load((ROOT / "install/dockerized/config.dockerized.yaml").read_text())
    policy = cfg["retrieval_policy"]
    assert policy["internal"] == {
        "files": "required",
        "vector": "disabled",
        "graph": "disabled",
    }
    assert policy["web"] == "disabled"
    assert cfg["tls"]["x509_strict"] is False


def test_public_installer_exposes_orthogonal_architecture_and_deployment_axes():
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    assert "--architecture-tier src|erg-internal|erg-full" in installer
    assert "--preset core|workgroup|full" in installer
    assert "--deployment standard|dockerized" in installer
    assert "compatibility alias for --deployment dockerized --preset core" in installer
    assert 'exec "$SCRIPT_DIR/profiles/install-standard.sh"' in installer
    assert 'exec "$SCRIPT_DIR/profiles/install-dockerized.sh"' in installer


def test_dockerized_installer_supports_x509_compatibility_without_disabling_tls():
    installer = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")
    assert "--x509-strict" in installer
    assert "--no-x509-strict" in installer
    assert "X509_STRICT=0" in installer
    assert "x509_strict: false" in installer
    assert "verify_tls: false" not in installer


def test_dockerized_uses_profile_specific_verification_budget_without_reranker():
    cfg = yaml.safe_load((ROOT / "install/dockerized/config.dockerized.yaml").read_text())
    registry = load_model_registry(cfg)
    standard = registry.get("sunaq-standard")
    thorough = registry.get("sunaq-thorough")
    deep = registry.get("sunaq-deep")

    assert standard.section("retrieval_planner")["verification_candidate_limit"] == 10
    assert thorough.section("retrieval_planner")["verification_candidate_limit"] == 30
    assert deep.section("retrieval_planner")["verification_candidate_limit"] == 50
    assert standard.section("reranker")["backend"] == "none"
    assert thorough.section("reranker")["backend"] == "none"
    assert deep.section("reranker")["backend"] == "none"


def test_dockerized_renderer_is_shared_landscape_desktop_component():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert 'context: ../components/playwright-renderer' in installer
    assert 'playwright_state:/state' in installer
    assert (ROOT / "install/components/playwright-renderer/app/renderer.py").is_file()
    web = yaml.safe_load((ROOT / "install/dockerized/web.dockerized.yaml").read_text())
    renderer = web["archive"]["renderer"]
    assert renderer["landscape"] is True
    assert renderer["prefer_css_page_size"] is False
    assert renderer["viewport"] == {"width": 1440, "height": 900}
    assert renderer["persist_state"] is True


def test_src_admin_hides_and_blocks_erg_only_capabilities():
    admin = (ROOT / "rag/admin_ui.py").read_text(encoding="utf-8")
    base = (ROOT / "rag/templates/admin/base.html").read_text(encoding="utf-8")
    overview = (ROOT / "rag/templates/admin/overview.html").read_text(encoding="utf-8")
    user = (ROOT / "rag/templates/admin/user.html").read_text(encoding="utf-8")
    users = (ROOT / "rag/templates/admin/users.html").read_text(encoding="utf-8")

    assert "architecture_tier = effective_architecture_tier(cfg)" in admin
    assert 'src_mode = architecture_tier == "src"' in admin
    assert "chat_archive_available = not src_mode" in admin
    assert 'mail_available = architecture_tier == "erg_full"' in admin
    assert 'web_research_available = architecture_tier == "erg_full"' in admin
    assert 'graph_enabled = (not src_mode) and bool(cfg_get(cfg, "neo4j.enabled", default=False))' in admin
    assert 'findings_available = graph_enabled and bool(cfg_get(cfg, "research_findings.enabled", default=False))' in admin
    assert "contacts_available = graph_enabled" in admin
    assert "_require_admin_path_capability(request)" in admin
    for message in (
        "Graph capability is not available",
        "Chat archive capability is not available",
        "Research Findings capability is not available",
        "Contact seed capability is not available",
        "Web Research capability is not available",
        "Mail capability is not available",
    ):
        assert message in admin

    overview_block = admin[admin.index("async def admin_overview"):admin.index("def _acl_filter_findings_for_canonical_user")]
    assert overview_block.index("if graph_enabled:") < overview_block.index("GraphCurator.from_config(cfg)")

    assert "{% if graph_enabled %}" in base
    assert "Graph</a>{% endif %}" in base
    assert "{% if graph_area and graph_enabled %}" in base
    assert overview.count("{% if graph_enabled %}") >= 2
    assert "{% if mail_available %}" in overview
    assert "{% if web_research_available %}" in overview
    assert "{% if vector_enabled %}" in overview

    assert "{% if chat_archive_available %}" in user
    assert "{% if findings_available %}" in user
    assert "{% if contacts_available %}" in user
    assert "{% if web_research_available %}" in user
    assert "{% if mail_available %}" in user

    assert "{% if mail_available %}<th>Mail</th>{% endif %}" in users
    assert "{% if web_research_available %}<th>Web</th>{% endif %}" in users
    assert "{% if contacts_available %}<th>Kontakt-DB</th>{% endif %}" in users






def test_dockerized_image_does_not_bake_site_config_files():
    dockerfile = (ROOT / "install/dockerized/Dockerfile.provider").read_text(encoding="utf-8")
    assert "COPY config.yaml /app/config.yaml" not in dockerfile
    assert "COPY web.yaml /app/web.yaml" not in dockerfile
    assert "Site configuration is runtime state" in dockerfile

def test_installers_run_centralized_config_upgrade_migrations():
    dockerized = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    standard = (ROOT / "install/profiles/install-standard.sh").read_text(encoding="utf-8")

    assert "python -m rag.config_upgrade" in dockerized
    assert "--deployment dockerized" in dockerized
    assert "install-config-upgraded.yaml" in dockerized

    assert "-m rag.config_upgrade" in standard
    assert "--deployment native" in standard


def test_dockerized_installer_validates_file_bind_mount_types_before_compose():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert 'require_regular_file_path "$PREFIX/config.yaml" "SunaQ config"' in installer
    assert 'require_regular_file_path "$PREFIX/web.yaml" "SunaQ web config"' in installer
    assert '"$SOURCE_DIR/install/dockerized/web.dockerized.yaml"' in installer
    assert 'SunaQ models path must be a directory' in installer
    assert 'SunaQ runtime path must be a directory' in installer

def test_dockerized_contact_cli_reports_erg_internal_requirement():
    helper = (ROOT / "install/dockerized/contacts.sh").read_text(encoding="utf-8")
    seed = (ROOT / "install/dockerized/seed-carddav.sh").read_text(encoding="utf-8")
    assert "ERG Internal capability" in helper
    assert "--preset workgroup" in helper
    assert 'exec "$(dirname "$0")/contacts.sh" "$@"' in seed


def test_dockerized_smoke_test_does_not_require_host_venv_or_qdrant():
    smoke = (ROOT / "install" / "smoke-test.sh").read_text(encoding="utf-8")
    assert 'Host Python venv not required' in smoke
    assert 'Qdrant disabled by configuration' in smoke
    assert 'Playwright renderer unavailable' in smoke
    assert 'Local Neo4j not selected' in smoke
    assert 'Playwright renderer deferred until maintenance mode is disabled' in smoke
    assert 'MAINTENANCE_ACTIVE=1' in smoke

def test_smoke_qdrant_probe_uses_deployment_neutral_config_path():
    smoke = (ROOT / "install" / "smoke-test.sh").read_text(encoding="utf-8")
    assert "run_smoke_python -m rag.qdrant_smoke --config config.yaml" in smoke
    assert "run_smoke_python -m rag.qdrant_smoke --config /app/config.yaml" not in smoke



def test_dockerized_installer_accepts_all_architecture_presets():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "--preset core|workgroup|full" in installer
    assert "core|workgroup|full" in installer
    assert 'src|erg_internal|erg_full' in installer
    assert '[[ "$EFFECTIVE_ARCHITECTURE_TIER" == "src" ]]' not in installer
    assert "Use the Standard profile for ERG Internal/Full" not in installer


def test_dockerized_plan_accepts_explicit_openai_llm_selection():
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "install/install.sh"),
            "--deployment",
            "dockerized",
            "--preset",
            "full",
            "--llm",
            "openai",
            "--plan",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dockerized_alias_remains_dockerized_core_only():
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "install/install.sh"),
            "--profile",
            "super-light",
            "--preset",
            "workgroup",
            "--plan",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "super-light means dockerized + core/SRC" in result.stderr


@pytest.mark.parametrize("preset", ["core", "workgroup", "full"])
def test_dockerized_plan_accepts_each_architecture_preset(preset):
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "install/install.sh"),
            "--deployment",
            "dockerized",
            "--preset",
            preset,
            "--plan",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Deployment mode:         dockerized" in result.stdout
    assert f"Capability preset:       {preset}" in result.stdout


def test_dockerized_maintenance_transitions_reuse_start_all_desired_state():
    maintenance = (ROOT / "install/maintenance-mode.sh").read_text(encoding="utf-8")
    assert "stop_dockerized_normal_services" in maintenance
    assert maintenance.count('"$BASE_DIR/start-all.sh"') >= 2
    assert maintenance.count("sync_container_secrets") >= 3
    assert "compose up -d --no-deps --force-recreate provider" not in maintenance


def test_dockerized_diagnostics_use_common_status_and_recorded_proxy_ports():
    smoke = (ROOT / "install/smoke-test.sh").read_text(encoding="utf-8")
    status = (ROOT / "status.sh").read_text(encoding="utf-8")
    legacy = (ROOT / "install/dockerized/status-dockerized.sh").read_text(encoding="utf-8")

    assert 'PROXY_HTTP_PORT="${PROXY_HTTP_PORT:-80}"' in smoke
    assert 'PROXY_HTTPS_PORT="${PROXY_HTTPS_PORT:-443}"' in smoke
    assert '"https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health"' in smoke
    assert '"http://127.0.0.1:${PROXY_HTTP_PORT}/"' in smoke

    assert '[[ -f install/install-state.env ]] && source install/install-state.env' in status
    assert 'PROXY_HTTPS_PORT="${PROXY_HTTPS_PORT:-443}"' in status
    assert '"https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health"' in status
    assert "LOCAL_NEO4J" in status
    assert "docker_service_status neo4j neo4j" in status
    assert "127.0.0.1:8770/health" in status
    assert 'exec "$PREFIX/status.sh" "$@"' in legacy


def test_dockerized_installer_records_profile_state_for_diagnostics():
    installer = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")
    assert 'DEPLOYMENT_PROFILE=dockerized' in installer
    assert 'DEPLOYMENT_MODE=dockerized' in installer
    assert 'LOCAL_QDRANT=$WITH_QDRANT' in installer
    assert '--with-qdrant' in installer
    assert '--no-qdrant' in installer
    assert 'LOCAL_RESOLVER=0' in installer
    assert 'WITH_PLAYWRIGHT=0' in installer
    assert '--with-playwright' in installer
    assert '--no-playwright' in installer
    assert 'LOCAL_PLAYWRIGHT=$WITH_PLAYWRIGHT' in installer


def test_playwright_renderer_stays_alive_in_degraded_browser_state():
    renderer = (ROOT / "install/components/playwright-renderer/app/renderer.py").read_text(encoding="utf-8")
    assert 'Chromium launch failed; renderer stays up in degraded mode' in renderer
    assert '"launch_error": BROWSER_LAUNCH_ERROR' in renderer


def test_installers_refuse_nonempty_foreign_prefixes():
    dockerized = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")
    standard = (ROOT / "install" / "profiles" / "install-standard.sh").read_text(encoding="utf-8")
    marker = "Refusing to install into non-empty directory that is not recognized as a SunaQ installation"
    assert marker in dockerized
    assert marker in standard
    assert '.sunaq-installation' in dockerized
    assert '.sunaq-installation' in standard
    assert '.aki-rag-installation' in dockerized
    assert '.aki-rag-installation' in standard


def test_install_profile_shell_scripts_are_syntax_valid():
    for path in (
        ROOT / "install/profiles/install-dockerized.sh",
        ROOT / "install/profiles/install-standard.sh",
        ROOT / "install/install.sh",
    ):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_dockerized_rerun_aborts_for_running_stack_but_accepts_stopped_stack():
    installer = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")
    running = '[WARN] Existing SunaQ services are running:'
    stopped = '[INFO] Existing SunaQ stack is stopped; rerun may rebuild/start it.'
    assert running in installer
    assert stopped in installer
    block = installer[installer.index(running):installer.index(stopped)]
    assert "no installation changes were made" in block
    assert "exit 2" in block


def test_dockerized_installer_supports_alternate_proxy_ports():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "--proxy-http-port" in installer
    assert "--proxy-https-port" in installer
    assert 'listen ${PROXY_HTTP_PORT} default_server' in installer
    assert 'listen ${PROXY_HTTPS_PORT} ssl default_server' in installer

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "install/install.sh"),
            "--profile",
            "super-light",
            "--plan",
            "--with-proxy",
            "--proxy-http-port",
            "81",
            "--proxy-https-port",
            "444",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Reverse proxy:           bundled/start on 81/444" in result.stdout


def test_dockerized_installer_rejects_invalid_proxy_ports():
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "install/install.sh"),
            "--profile",
            "super-light",
            "--plan",
            "--proxy-http-port",
            "0",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--proxy-http-port must be an integer from 1 to 65535" in result.stderr


def test_dockerized_rerun_preserves_optional_services_unless_explicitly_disabled(tmp_path):
    prefix = tmp_path / "aki"
    (prefix / "install").mkdir(parents=True)
    (prefix / "rag").mkdir()
    (prefix / "config.yaml").write_text("{}\n")
    (prefix / ".aki-rag-installation").write_text(
        "AKI_RAG_INSTALLATION=1\nDEPLOYMENT_PROFILE=super-light\nDEPLOYMENT_MODE=dockerized\n"
    )
    (prefix / "install/install-state.env").write_text(
        "DEPLOYMENT_PROFILE=super-light\n"
        "LOCAL_OPENWEBUI=1\n"
        "LOCAL_PROXY=1\n"
        "PROXY_HTTP_PORT=81\n"
        "PROXY_HTTPS_PORT=444\n"
    )

    inherited = subprocess.run(
        [
            "bash", str(ROOT / "install/install.sh"), "--profile", "super-light",
            "--plan", "--prefix", str(prefix),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert inherited.returncode == 0, inherited.stderr
    assert (
        "OpenWebUI:               pull/start "
        "(retained from existing install; use --no-openwebui to disable)"
    ) in inherited.stdout
    assert (
        "Reverse proxy:           bundled/start on 81/444 "
        "(retained from existing install; use --no-proxy to disable)"
    ) in inherited.stdout

    disabled = subprocess.run(
        [
            "bash", str(ROOT / "install/install.sh"), "--profile", "super-light",
            "--plan", "--prefix", str(prefix), "--no-openwebui", "--no-proxy",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    assert "OpenWebUI:               not pulled/not started" in disabled.stdout
    assert "Reverse proxy:           disabled" in disabled.stdout

    explicit_ports = subprocess.run(
        [
            "bash", str(ROOT / "install/install.sh"), "--profile", "super-light",
            "--plan", "--prefix", str(prefix), "--with-proxy",
            "--proxy-http-port", "82", "--proxy-https-port", "445",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert explicit_ports.returncode == 0, explicit_ports.stderr
    assert "Reverse proxy:           bundled/start on 82/445" in explicit_ports.stdout


def test_dockerized_runtime_env_appends_real_newlines_for_upgrade_keys():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "printf '%s=%s\\n' \"$key\" \"$value\" >> \"$PREFIX/runtime.env\"" in installer
    assert "printf '%s=%s\\\\n' \"$key\" \"$value\" >> \"$PREFIX/runtime.env\"" not in installer


def test_dockerized_runtime_env_repairs_buggy_literal_newline_upgrade_state():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert "repair_legacy_runtime_env_newline_bug" in installer
    assert "RAG_PROVIDER_INTERNAL_KEY" in installer
    assert "gsub(/\\\\nRAG_/, \"\\nRAG_\")" in installer
    assert "repair_legacy_runtime_env_newline_bug\n\n# Every install/update" in installer


def test_dockerized_runtime_env_repair_preserves_unrelated_literal_newlines():
    fixture = "CUSTOM_VALUE=a\\nb\\nRAG_INTERNAL_API_KEY=replace-me\nOTHER_VALUE=x\\ny\n"
    program = r'{ if ($0 ~ /\\nRAG_/) gsub(/\\nRAG_/, "\nRAG_"); print }'
    result = subprocess.run(
        ["awk", program],
        input=fixture,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == (
        "CUSTOM_VALUE=a\\nb\n"
        "RAG_INTERNAL_API_KEY=replace-me\n"
        "OTHER_VALUE=x\\ny\n"
    )


def test_installers_warn_on_unreachable_configured_services_without_failing_install():
    standard = (ROOT / "install/profiles/install-standard.sh").read_text(encoding="utf-8")
    dockerized = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    for installer in (standard, dockerized):
        assert 'probe_service_url "Nextcloud" "$NEXTCLOUD_URL"' in installer
        assert 'probe_service_url "Elasticsearch" "$ELASTICSEARCH_URL"' in installer
        assert 'curl -sS --max-time 5 -o /dev/null "$url"' in installer
        assert "Installation will continue; verify the configured URL/service before using SunaQ." in installer


def test_dockerized_provider_image_packages_sunaq_models():
    dockerfile = (ROOT / "install/dockerized/Dockerfile.provider").read_text(encoding="utf-8")
    assert "COPY models/ /app/models/" in dockerfile
    assert (ROOT / "models" / "standard" / "profile.yaml").is_file()
    assert (ROOT / "models" / "thorough" / "profile.yaml").is_file()
    assert (ROOT / "models" / "deep" / "profile.yaml").is_file()



def test_installers_seed_preserve_and_add_only_enabled_sunaq_model_packages():
    standard = (ROOT / "install" / "profiles" / "install-standard.sh").read_text(
        encoding="utf-8"
    )
    dockerized = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(
        encoding="utf-8"
    )
    for installer in (standard, dockerized):
        assert 'model_package_enabled()' in installer
        assert 'mkdir -p "$PREFIX/models"' in installer
        assert 'for source_model in "$SOURCE_DIR"/models/*; do' in installer
        assert 'if ! model_package_enabled "$source_model"; then' in installer
        assert 'Skipping disabled SunaQ model package:' in installer
        assert 'if [[ ! -e "$PREFIX/models/$model_name" ]]; then' in installer
        assert 'cp -a "$source_model" "$PREFIX/models/$model_name"' in installer
        assert 'Added new SunaQ model package:' in installer
    assert "for item in rag prompts models ontology" not in standard
    assert "for item in rag prompts models ontology" not in dockerized



def test_fresh_install_defaults_to_opt_sunaq_but_legacy_prefix_is_still_recognized():
    standard = (ROOT / "install/profiles/install-standard.sh").read_text(encoding="utf-8")
    dockerized = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    for installer in (standard, dockerized):
        assert 'PREFIX="/opt/sunaq"' in installer
        assert 'LEGACY_PREFIX="/opt/nextcloud-rag"' in installer
        assert 'PREFIX_EXPLICIT=0' in installer
        assert 'Legacy SunaQ/AKI installation detected' in installer



def test_optional_openwebui_uses_pinned_slim_image_and_hides_arena():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(
        encoding="utf-8"
    )
    image = (
        "ghcr.io/open-webui/open-webui:"
        "v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e"
    )
    assert f"OPENWEBUI_IMAGE={image}" in installer
    assert "${OPENWEBUI_IMAGE:-" + image + "}" in installer
    assert 'ENABLE_EVALUATION_ARENA_MODELS: "false"' in installer


def test_dockerized_erg_presets_enable_local_neo4j_without_document_graph():
    core = yaml.safe_load((ROOT / "install/presets/core.yaml").read_text())
    workgroup = yaml.safe_load((ROOT / "install/presets/workgroup.yaml").read_text())
    full = yaml.safe_load((ROOT / "install/presets/full.yaml").read_text())

    assert core["architecture"]["tier"] == "src"
    assert core["neo4j"]["enabled"] is False
    assert workgroup["architecture"]["tier"] == "erg_internal"
    assert workgroup["qdrant"]["enabled"] is True
    assert workgroup["sync_worker"]["enabled"] is True
    assert workgroup["neo4j"]["enabled"] is True
    assert full["architecture"]["tier"] == "erg_full"
    assert full["qdrant"]["enabled"] is True
    assert full["sync_worker"]["enabled"] is True
    assert full["neo4j"]["enabled"] is True

    # ERG Internal and Full differ only in external acquisition capabilities;
    # their ordinary internal document policy stays identical.
    assert workgroup["retrieval_policy"]["internal"] == full["retrieval_policy"]["internal"]

    # Neo4j here is local entity/contact/resolver state. The dockerized
    # footprint still does not enable document-graph retrieval/extraction.
    for preset in (workgroup, full):
        assert preset["retrieval_policy"]["internal"]["graph"] == "disabled"
        assert preset["graph_queue"]["enabled"] is False
        assert preset["graph_indexer"]["enabled"] is False
        assert preset["graph_entity_discovery"]["enabled"] is False
        assert preset["graph_relation_discovery"]["enabled"] is False


def test_dockerized_neo4j_uses_persistent_volumes_and_file_backed_secret():
    installer = (ROOT / "install/profiles/install-dockerized.sh").read_text(encoding="utf-8")
    assert 'NEO4J_AUTH_FILE: "/run/secrets/neo4j_auth"' in installer
    assert "../../runtime/service-secrets/neo4j_auth:/run/secrets/neo4j_auth:ro,z" in installer
    assert "neo4j_data:/data" in installer
    assert "neo4j_logs:/logs" in installer
    assert 'compose pull neo4j' in installer
    assert 'LOCAL_NEO4J=$NEO4J_ACTIVE' in installer
    assert 'RESOLVER_NEO4J_ENABLED=$([[ $NEO4J_ACTIVE -eq 1 ]]' in installer


def test_release_identity_hygiene():
    current_version = (ROOT / "rag/version.py").read_text(encoding="utf-8").strip()
    app_info = (ROOT / "clients/nextcloud/sunaq/appinfo/info.xml").read_text(encoding="utf-8")
    assert current_version == 'VERSION = "0.8.7-rc1"'
    assert "<version>0.3.16</version>" in app_info

    forbidden_by_file = {
        "README.md": (
            "In the RC2 architecture",
            "current RC2 Dockerized footprint",
            "SunaQ Recherche 0.3.4",
            "fresh 0.8.6 installation",
            "server-side conversation state is deferred to rc2",
        ),
        "provider.env.example": (
            "0.8.7-rc2",
            "RC3",
            "Fresh 0.8.6 installs",
        ),
        "install/dockerized/provider.env.dockerized.example": (
            "0.8.7-rc2",
            "RC3",
            "Fresh 0.8.6 installs",
        ),
        "install/INSTALL.md": (
            "fresh 0.8.6 installation",
        ),
        "models/README.md": (
            "RC2 target",
            "recommended RC2 packaging",
            "RC2 profile fragment",
            "**RC2 status:**",
            "**RC2 egress status:**",
            "Nextcloud 0.3.14 UI",
            "current 0.8.6-rc1 experiment",
            "deployment_overrides:\\n  super-light:",
        ),
        "docs/ADMINISTRATION.md": (
            "Fresh 0.8.6 installations",
            "install/super-light/",
            "dockerized super-light",
            "Super-Light uses the exact same path",
            "SunaQ Recherche 0.3.0",
        ),
        "docs/BETA-OPERATIONS.md": (
            "## 4. SunaQ Recherche 0.3.2",
        ),
        "docs/KNOWN-LIMITATIONS.md": (
            "Recherche 0.3.4",
            "Super-Light intentionally relies",
            "Bundled nginx and OpenWebUI are opt-in in Super-Light",
            "Super-Light has no local reranker",
            "required Super-Light document arm",
        ),
        "docs/ROADMAP.md": (
            "SunaQ Recherche 0.3.0 with",
            "Super-Light as the Elasticsearch-centric deployment profile",
        ),
        "docs/TECHNICAL-REFERENCE.md": (
            "current rc2",
            "standard+native",
            "super-light+dockerized",
            "SunaQ Recherche **0.3.4**",
        ),
        "clients/nextcloud/sunaq/README.md": (
            "\\n- 0.3.16:",
        ),
    }
    for relative, forbidden_strings in forbidden_by_file.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for forbidden in forbidden_strings:
            assert forbidden not in text, f"{relative}: stale release marker {forbidden!r}"
