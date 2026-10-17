from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stop_all_stops_all_standard_compose_profiles_without_deleting_volumes():
    script = (ROOT / "stop-all.sh").read_text(encoding="utf-8")

    assert "stop_one mail-worker" in script
    assert "stop_one provider" in script
    assert "stop_one api" in script
    assert "stop_compose_stack" in script
    assert "--profile qdrant" in script
    assert "--profile neo4j" in script
    assert "--profile renderer" in script
    assert "--profile ui" in script
    assert "down" in script
    assert "down -v" not in script
    assert "down --volumes" not in script


def test_stop_all_native_only_skips_compose_shutdown():
    script = (ROOT / "stop-all.sh").read_text(encoding="utf-8")

    assert "--native-only" in script
    native_guard = script.index("if [[ $NATIVE_ONLY -eq 1 ]]")
    compose_function = script.index("stop_compose_stack()")
    assert native_guard < compose_function


def test_maintenance_switches_only_native_processes():
    script = (ROOT / "install" / "maintenance-mode.sh").read_text(encoding="utf-8")

    assert script.count('"$BASE_DIR/stop-all.sh" --native-only || true') == 2
    assert '"$BASE_DIR/stop-all.sh" || true' not in script


def test_stop_all_dispatches_dockerized_before_native_pid_stops():
    script = (ROOT / "stop-all.sh").read_text(encoding="utf-8")

    dispatch = script.index('if [[ "$DEPLOYMENT_MODE" == "dockerized" && $NATIVE_ONLY -eq 0 ]]')
    native_stop = script.index("stop_one mail-worker")
    assert dispatch < native_stop
    assert '$BASE_DIR/install/dockerized/docker-compose.yml' in script
    assert '"${compose[@]}" down' in script
    assert "down -v" not in script
    assert "down --volumes" not in script


def test_common_operator_entrypoints_are_dockerized_aware():
    start = (ROOT / "start-all.sh").read_text(encoding="utf-8")
    status = (ROOT / "status.sh").read_text(encoding="utf-8")
    maintenance = (ROOT / "install" / "maintenance-mode.sh").read_text(encoding="utf-8")

    assert 'if [[ "$DEPLOYMENT_MODE" == "dockerized" ]]' in start
    assert '$BASE_DIR/install/dockerized' in start
    assert 'install/sync-container-secrets.sh' in start
    assert 'compose_dockerized up -d --force-recreate --remove-orphans' in start
    assert 'compose_dockerized config --services' in start
    assert 'dockerized_compose config --services' not in start
    assert 'RAG_MAINTENANCE_MODE' in start
    assert 'LOCAL_QDRANT' in start
    assert 'LOCAL_NEO4J' in start
    assert 'LOCAL_RESOLVER' in start
    assert 'services+=(qdrant)' in start
    assert 'services+=(graph-worker)' in start
    assert 'services+=(sync-worker)' in start
    assert 'LOCAL_PLAYWRIGHT' in start

    assert 'DISPLAY_DEPLOYMENT="$DEPLOYMENT_MODE"' in status
    assert '[[ "$DISPLAY_DEPLOYMENT" == "native" ]] && DISPLAY_DEPLOYMENT="standard"' in status
    assert '$BASE_DIR/install/dockerized' in status
    assert 'docker_service_status api api' in status
    assert 'docker_service_status provider provider' in status
    assert 'docker_service_status qdrant qdrant' in status
    assert 'docker_service_status graph-worker graph-worker' in status
    assert 'docker_service_status sync-worker sync-worker' in status
    assert "docker inspect -f '{{.State.Running}}'" in status

    assert '$BASE_DIR/install/dockerized' in maintenance
    assert 'enable_dockerized' in maintenance
    assert 'disable_dockerized' in maintenance
    assert 'state_enabled LOCAL_QDRANT' in maintenance
    assert 'docker_service_defined graph-worker' in maintenance
    assert 'docker_service_defined sync-worker' in maintenance
    assert '[[ "$public_deployment" == "native" ]] && public_deployment="standard"' in maintenance


def test_dockerized_installer_packages_common_operator_entrypoints():
    installer = (ROOT / "install" / "profiles" / "install-dockerized.sh").read_text(encoding="utf-8")

    copy_loop = installer.split("for item in ", 1)[1].split("; do", 1)[0]
    assert "start-all.sh" in copy_loop
    assert "stop-all.sh" in copy_loop
    assert "status.sh" in copy_loop
    assert "for operator_script in start-all.sh stop-all.sh status.sh" in installer
    assert 'chmod 0755 "$PREFIX/$operator_script"' in installer


def test_legacy_dockerized_status_is_only_a_common_status_wrapper():
    legacy = (ROOT / "install" / "dockerized" / "status-dockerized.sh").read_text(encoding="utf-8")

    assert 'exec "$PREFIX/status.sh" "$@"' in legacy
    assert "docker compose" not in legacy
    assert "probe '" not in legacy
