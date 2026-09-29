from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_standard_installer_waits_for_neo4j_and_runs_full_schema_upgrade():
    script = (ROOT / "install/profiles/install-standard.sh").read_text()
    start = script.index('compose_cmd -f docker-compose.yml --env-file .env "${LOCAL_PROFILE_ARGS[@]}" up -d')
    init = script.index('"$PREFIX/.venv/bin/python" -m rag.graph --config "$PREFIX/config.yaml" init')
    assert init > start
    assert 'if [[ $WITH_NEO4J -eq 1 ]]' in script[start:init]
    assert 'NEO4J_PASSWORD="$NEO4J_PASSWORD"' in script[start:init + 500]
    assert "NEO4J_SCHEMA_READY" in script[start:init + 1000]
    assert "exit 1" in script[init:init + 1200]


def test_super_light_install_stays_provider_only_until_maintenance_is_disabled():
    script = (ROOT / "install/profiles/install-super-light.sh").read_text()
    compose = (ROOT / "install/super-light/docker-compose.yml").read_text()
    maintenance = (ROOT / "install/maintenance-mode.sh").read_text()

    assert "set_runtime_env_value RAG_MAINTENANCE_MODE true" in script
    assert 'SERVICES=(provider)' in script
    assert 'SERVICES=(api provider mail-worker neo4j playwright-renderer)' not in script
    assert "compose exec -T api python -m rag.graph --config /app/config.yaml init" not in script
    assert 'command: ["python", "-m", "rag.provider_entrypoint"]' in compose
    assert 'local services=(neo4j api)' in maintenance
    assert 'if state_enabled LOCAL_PLAYWRIGHT' in maintenance
    assert 'services=(neo4j playwright-renderer api)' in maintenance
    assert "compose up -d neo4j playwright-renderer api mail-worker" not in maintenance
    assert "compose exec -T api python -m rag.graph --config /app/config.yaml init" in maintenance
    assert "Neo4j/schema: waiting" in maintenance
    assert "returning to maintenance mode" in maintenance
    assert "compose up -d --no-deps --force-recreate provider" in maintenance


def test_api_startup_has_nonfatal_schema_upgrade_fallback():
    api = (ROOT / "rag/api.py").read_text()
    assert "def _initialize_neo4j_schema()" in api
    assert "graph.ensure_schema()" in api
    assert "_initialize_neo4j_schema()" in api
    assert "Neo4j schema initialization deferred" in api


def test_standard_installer_config_preset_uses_installed_tree_on_pythonpath():
    script = (ROOT / "install/profiles/install-standard.sh").read_text()
    invocation = 'run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m rag.config_preset'
    assert script.count(invocation) == 2
