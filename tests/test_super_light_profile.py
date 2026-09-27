from pathlib import Path
import subprocess

import pytest
import yaml

from rag.reranker import Reranker
from rag.sunaq_models import load_model_registry


ROOT = Path(__file__).resolve().parent.parent


def test_super_light_profile_disables_vector_reranker_and_graph_documents():
    cfg = yaml.safe_load((ROOT / "install/super-light/config.super-light.yaml").read_text())
    registry = load_model_registry(cfg)
    assert cfg["deployment"]["profile"] == "super-light"
    assert cfg["elasticsearch"]["enabled"] is True
    assert cfg["qdrant"]["enabled"] is False
    assert cfg["sync_worker"]["enabled"] is False
    assert cfg["neo4j"]["enabled"] is True
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
        assert model.section("graph_retrieval")["enabled"] is False
        assert model.section("reranker")["backend"] == "none"


def test_super_light_requirements_have_no_model_or_vector_client_dependencies():
    req = (ROOT / "requirements-super-light.txt").read_text().lower()
    for forbidden in ("qdrant-client", "transformers", "huggingface", "sentencepiece", "torch"):
        assert forbidden not in req
    assert "neo4j" in req


def test_super_light_web_profile_is_disabled_until_admin_opts_in():
    web = yaml.safe_load((ROOT / "install/super-light/web.super-light.yaml").read_text())
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


def test_super_light_proxy_htpasswd_is_worker_readable():
    installer = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(encoding="utf-8")
    assert 'chmod 644 "$PREFIX/install/nginx/htpasswd"' in installer
    assert 'chmod 600 "$PREFIX/install/nginx/htpasswd"' not in installer


def test_super_light_web_capability_is_globally_off_by_default():
    web = yaml.safe_load((ROOT / "install/super-light/web.super-light.yaml").read_text())
    assert web["enabled"] is False
    assert web["archive"]["enabled"] is False
    assert web["search"]["provider"] == "brave"


def test_super_light_base_compose_does_not_define_optional_frontends_or_renderer():
    compose = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    services = compose["services"]
    assert "openwebui" not in services
    assert "proxy" not in services
    assert "playwright-renderer" not in services
    assert "mail-worker" not in services
    assert {"api", "provider", "neo4j"}.issubset(services)


def test_super_light_mounts_administrator_models_read_only():
    compose = yaml.safe_load((ROOT / "install/super-light/docker-compose.yml").read_text())
    for service in ("api", "provider"):
        volumes = compose["services"][service]["volumes"]
        assert "../../models:/app/models:ro" in volumes


def test_super_light_installer_generates_optional_compose_override():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert 'docker-compose.override.yml' in installer
    assert 'if [[ $WITH_PLAYWRIGHT -eq 1 ]]' in installer
    assert 'if [[ $WITH_OPENWEBUI -eq 1 ]]' in installer
    assert 'if [[ $WITH_PROXY -eq 1 ]]' in installer
    assert '  playwright-renderer:' in installer
    assert '  mail-worker:' in installer
    assert 'MAIL_WORKER_ACTIVE' in installer
    assert 'mail_worker_enabled_from_config' in installer
    assert 'compose up -d --remove-orphans' in installer


def test_super_light_disabled_playwright_does_not_require_generated_seccomp_file():
    compose = (ROOT / "install/super-light/docker-compose.yml").read_text(encoding="utf-8")
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert 'playwright-renderer:' not in compose
    assert 'seccomp=${PLAYWRIGHT_SECCOMP_PROFILE:-unconfined}' in installer
    assert 'PLAYWRIGHT_SECCOMP_PROFILE=$([[ $WITH_PLAYWRIGHT -eq 1 ]]' in installer
    prepare = '"$PREFIX/install/components/playwright-renderer/prepare.sh"'
    first_compose = 'compose stop playwright-renderer'
    assert installer.index(prepare) < installer.index('compose build api provider')
    assert installer.index(prepare) < installer.index(first_compose)
    assert installer.count(prepare) == 1


def test_super_light_marks_dockerized_deployment_mode():
    cfg = yaml.safe_load((ROOT / "install/super-light/config.super-light.yaml").read_text())
    assert cfg["deployment"]["profile"] == "super-light"
    assert cfg["deployment"]["mode"] == "dockerized"


def test_super_light_private_ca_is_baked_into_provider_image():
    dockerfile = (ROOT / "install/super-light/Dockerfile.provider").read_text(encoding="utf-8")
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert "--ca-certificate" in installer
    assert "runtime/ca" in installer
    assert "COPY runtime/ca/" in dockerfile
    assert "update-ca-certificates" in dockerfile
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in dockerfile
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in dockerfile


def test_docker_build_context_excludes_runtime_secrets_but_keeps_ca():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "runtime/*" in ignore
    assert "!runtime/ca/" in ignore
    assert "provider.env" in ignore
    assert "runtime.env" in ignore


def test_super_light_retrieval_policy_keeps_es_required_and_document_graph_disabled():
    cfg = yaml.safe_load((ROOT / "install/super-light/config.super-light.yaml").read_text())
    policy = cfg["retrieval_policy"]
    assert policy["internal"] == {
        "files": "required",
        "vector": "disabled",
        "graph": "disabled",
    }
    assert policy["web"] == "disabled"
    assert cfg["tls"]["x509_strict"] is False


def test_public_installer_exposes_profile_and_deployment_axes():
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    assert "--deployment native|dockerized" in installer
    assert "standard:native" in installer
    assert "super-light:dockerized" in installer


def test_super_light_installer_supports_x509_compatibility_without_disabling_tls():
    installer = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(encoding="utf-8")
    assert "--x509-strict" in installer
    assert "--no-x509-strict" in installer
    assert "X509_STRICT=0" in installer
    assert "x509_strict: false" in installer
    assert "verify_tls: false" not in installer


def test_super_light_uses_profile_specific_verification_budget_without_reranker():
    cfg = yaml.safe_load((ROOT / "install/super-light/config.super-light.yaml").read_text())
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


def test_super_light_renderer_is_shared_landscape_desktop_component():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert 'context: ../components/playwright-renderer' in installer
    assert 'playwright_state:/state' in installer
    assert (ROOT / "install/components/playwright-renderer/app/renderer.py").is_file()
    web = yaml.safe_load((ROOT / "install/super-light/web.super-light.yaml").read_text())
    renderer = web["archive"]["renderer"]
    assert renderer["landscape"] is True
    assert renderer["prefer_css_page_size"] is False
    assert renderer["viewport"] == {"width": 1440, "height": 900}
    assert renderer["persist_state"] is True


def test_super_light_contact_cli_executes_in_running_api_container():
    helper = (ROOT / "install/super-light/contacts.sh").read_text(encoding="utf-8")
    seed = (ROOT / "install/super-light/seed-carddav.sh").read_text(encoding="utf-8")
    assert 'exec -T api python -m rag.contacts' in helper
    assert 'run --rm --no-deps api' not in helper
    assert './contacts.sh sync' in seed


def test_super_light_smoke_test_does_not_require_host_venv_or_qdrant():
    smoke = (ROOT / "install" / "smoke-test.sh").read_text(encoding="utf-8")
    assert 'Host Python venv not required' in smoke
    assert 'Qdrant disabled by configuration' in smoke
    assert 'Playwright renderer unavailable' in smoke
    assert 'Neo4j deferred until maintenance mode is disabled' in smoke
    assert 'Playwright renderer deferred until maintenance mode is disabled' in smoke
    assert 'MAINTENANCE_ACTIVE=1' in smoke


def test_super_light_installer_records_profile_state_for_diagnostics():
    installer = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(encoding="utf-8")
    assert 'DEPLOYMENT_PROFILE=super-light' in installer
    assert 'DEPLOYMENT_MODE=dockerized' in installer
    assert 'LOCAL_QDRANT=0' in installer
    assert 'WITH_PLAYWRIGHT=0' in installer
    assert '--with-playwright' in installer
    assert '--no-playwright' in installer
    assert 'LOCAL_PLAYWRIGHT=$WITH_PLAYWRIGHT' in installer


def test_playwright_renderer_stays_alive_in_degraded_browser_state():
    renderer = (ROOT / "install/components/playwright-renderer/app/renderer.py").read_text(encoding="utf-8")
    assert 'Chromium launch failed; renderer stays up in degraded mode' in renderer
    assert '"launch_error": BROWSER_LAUNCH_ERROR' in renderer


def test_installers_refuse_nonempty_foreign_prefixes():
    super_light = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(encoding="utf-8")
    standard = (ROOT / "install" / "profiles" / "install-standard.sh").read_text(encoding="utf-8")
    marker = "Refusing to install into non-empty directory that is not recognized as a SunaQ installation"
    assert marker in super_light
    assert marker in standard
    assert '.sunaq-installation' in super_light
    assert '.sunaq-installation' in standard
    assert '.aki-rag-installation' in super_light
    assert '.aki-rag-installation' in standard


def test_install_profile_shell_scripts_are_syntax_valid():
    for path in (
        ROOT / "install/profiles/install-super-light.sh",
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


def test_super_light_rerun_aborts_for_running_stack_but_accepts_stopped_stack():
    installer = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(encoding="utf-8")
    running = '[WARN] Existing SunaQ services are running:'
    stopped = '[INFO] Existing SunaQ stack is stopped; rerun may rebuild/start it.'
    assert running in installer
    assert stopped in installer
    block = installer[installer.index(running):installer.index(stopped)]
    assert "no installation changes were made" in block
    assert "exit 2" in block


def test_super_light_installer_supports_alternate_proxy_ports():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
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


def test_super_light_installer_rejects_invalid_proxy_ports():
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


def test_super_light_rerun_preserves_optional_services_unless_explicitly_disabled(tmp_path):
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


def test_super_light_runtime_env_appends_real_newlines_for_upgrade_keys():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert "printf '%s=%s\\n' \"$key\" \"$value\" >> \"$PREFIX/runtime.env\"" in installer
    assert "printf '%s=%s\\\\n' \"$key\" \"$value\" >> \"$PREFIX/runtime.env\"" not in installer


def test_super_light_runtime_env_repairs_buggy_literal_newline_upgrade_state():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    assert "repair_legacy_runtime_env_newline_bug" in installer
    assert "RAG_PROVIDER_INTERNAL_KEY" in installer
    assert "gsub(/\\\\nRAG_/, \"\\nRAG_\")" in installer
    assert "repair_legacy_runtime_env_newline_bug\n\n# Every install/update" in installer


def test_super_light_runtime_env_repair_preserves_unrelated_literal_newlines():
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
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    for installer in (standard, super_light):
        assert 'probe_service_url "Nextcloud" "$NEXTCLOUD_URL"' in installer
        assert 'probe_service_url "Elasticsearch" "$ELASTICSEARCH_URL"' in installer
        assert 'curl -sS --max-time 5 -o /dev/null "$url"' in installer
        assert "Installation will continue; verify the configured URL/service before using SunaQ." in installer


def test_super_light_provider_image_packages_sunaq_models():
    dockerfile = (ROOT / "install/super-light/Dockerfile.provider").read_text(encoding="utf-8")
    assert "COPY models/ /app/models/" in dockerfile
    assert (ROOT / "models" / "standard" / "profile.yaml").is_file()
    assert (ROOT / "models" / "thorough" / "profile.yaml").is_file()
    assert (ROOT / "models" / "deep" / "profile.yaml").is_file()



def test_installers_seed_preserve_and_add_new_sunaq_model_packages():
    standard = (ROOT / "install" / "profiles" / "install-standard.sh").read_text(
        encoding="utf-8"
    )
    super_light = (ROOT / "install" / "profiles" / "install-super-light.sh").read_text(
        encoding="utf-8"
    )
    for installer in (standard, super_light):
        assert 'if [[ ! -d "$PREFIX/models" ]]; then' in installer
        assert 'cp -a "$SOURCE_DIR/models" "$PREFIX/models"' in installer
        assert 'for source_model in "$SOURCE_DIR"/models/*; do' in installer
        assert 'if [[ ! -e "$PREFIX/models/$model_name" ]]; then' in installer
        assert 'cp -a "$source_model" "$PREFIX/models/$model_name"' in installer
        assert 'Added new SunaQ model package:' in installer
    assert "for item in rag prompts models ontology" not in standard
    assert "for item in rag prompts models ontology" not in super_light



def test_fresh_install_defaults_to_opt_sunaq_but_legacy_prefix_is_still_recognized():
    standard = (ROOT / "install/profiles/install-standard.sh").read_text(encoding="utf-8")
    super_light = (ROOT / "install/profiles/install-super-light.sh").read_text(encoding="utf-8")
    for installer in (standard, super_light):
        assert 'PREFIX="/opt/sunaq"' in installer
        assert 'LEGACY_PREFIX="/opt/nextcloud-rag"' in installer
        assert 'PREFIX_EXPLICIT=0' in installer
        assert 'Legacy SunaQ/AKI installation detected' in installer



def test_optional_openwebui_uses_pinned_slim_image_and_hides_arena():
    installer = (ROOT / "install/profiles/install-super-light.sh").read_text(
        encoding="utf-8"
    )
    image = (
        "ghcr.io/open-webui/open-webui:"
        "v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e"
    )
    assert f"OPENWEBUI_IMAGE={image}" in installer
    assert "${OPENWEBUI_IMAGE:-" + image + "}" in installer
    assert 'ENABLE_EVALUATION_ARENA_MODELS: "false"' in installer
