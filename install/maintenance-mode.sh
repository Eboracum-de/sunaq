#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="$BASE_DIR/runtime.env"
STATE_FILE="$BASE_DIR/install/install-state.env"

usage() {
  echo "Usage: $0 on|off|status" >&2
  exit 2
}

[[ -f "$RUNTIME_ENV" ]] || { echo "runtime.env not found: $RUNTIME_ENV" >&2; exit 1; }

set_mode() {
  local value="$1"
  if grep -q '^RAG_MAINTENANCE_MODE=' "$RUNTIME_ENV"; then
    sed -i "s/^RAG_MAINTENANCE_MODE=.*/RAG_MAINTENANCE_MODE=$value/" "$RUNTIME_ENV"
  else
    printf '\nRAG_MAINTENANCE_MODE=%s\n' "$value" >> "$RUNTIME_ENV"
  fi
  chmod 600 "$RUNTIME_ENV"
}

current_mode() {
  local value
  value="$(sed -n 's/^RAG_MAINTENANCE_MODE=//p' "$RUNTIME_ENV" | tail -1)"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|on) echo on ;;
    *) echo off ;;
  esac
}

deployment_mode="native"
if [[ -f "$STATE_FILE" ]]; then
  value="$(sed -n 's/^DEPLOYMENT_MODE=//p' "$STATE_FILE" | tail -1)"
  [[ "$value" == "dockerized" || "$value" == "native" ]] && deployment_mode="$value"
elif [[ -f "$BASE_DIR/.sunaq-installation" || -f "$BASE_DIR/.aki-rag-installation" ]]; then
  marker="$BASE_DIR/.sunaq-installation"
  [[ -f "$marker" ]] || marker="$BASE_DIR/.aki-rag-installation"
  value="$(sed -n 's/^DEPLOYMENT_MODE=//p' "$marker" | tail -1)"
  [[ "$value" == "dockerized" || "$value" == "native" ]] && deployment_mode="$value"
fi

sync_container_secrets() {
  # Both deployment modes consume generated secret state in rc1.2:
  # Docker uses runtime.container.env + mounted files, native launchers source
  # runtime.service.env. Refresh before every service transition/rotation.
  bash "$BASE_DIR/install/sync-container-secrets.sh" "$BASE_DIR" >/dev/null
}

compose() {
  (
    cd "$BASE_DIR/install/dockerized"
    if docker compose version >/dev/null 2>&1; then
      docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
      docker-compose "$@"
    else
      echo "Docker Compose not found." >&2
      exit 1
    fi
  )
}

native_compose() {
  (
    cd "$BASE_DIR/install"
    if docker compose version >/dev/null 2>&1; then
      docker compose -f docker-compose.yml --env-file .env "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
      docker-compose -f docker-compose.yml --env-file .env "$@"
    else
      echo "Docker Compose not found." >&2
      exit 1
    fi
  )
}

refresh_native_secret_consumers() {
  # sync-container-secrets replaces service-secrets atomically. Containers that
  # bind-mount that directory must be recreated, not merely restarted, or their
  # mount remains attached to the old directory inode.
  if state_enabled LOCAL_RESOLVER; then
    native_compose --profile resolver up -d --no-deps --force-recreate erg-resolver
  fi
}

native_systemd_available() {
  command -v systemctl >/dev/null 2>&1 && [[ -f /etc/systemd/system/rag-provider.service ]]
}

native_require_root() {
  if native_systemd_available && [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Systemd-managed maintenance switching requires root; rerun with sudo." >&2
    exit 1
  fi
}

enable_native() {
  native_require_root
  sync_container_secrets
  refresh_native_secret_consumers
  if native_systemd_available; then
    systemctl stop rag-api rag-graph-worker rag-sync-worker rag-mail-worker 2>/dev/null || true
    systemctl restart rag-provider
  else
    "$BASE_DIR/stop-all.sh" --native-only || true
    "$BASE_DIR/start-all.sh"
  fi
}

disable_native() {
  native_require_root
  sync_container_secrets
  refresh_native_secret_consumers
  if native_systemd_available; then
    systemctl start rag-api
    systemctl restart rag-provider
    systemctl start rag-sync-worker 2>/dev/null || true
    if systemctl is-enabled rag-mail-worker >/dev/null 2>&1; then
      systemctl start rag-mail-worker
    fi
    if systemctl is-enabled rag-graph-worker >/dev/null 2>&1; then
      systemctl start rag-graph-worker
    fi
  else
    "$BASE_DIR/stop-all.sh" --native-only || true
    "$BASE_DIR/start-all.sh"
  fi
}

state_enabled() {
  local key="$1"
  [[ -f "$STATE_FILE" ]] || return 1
  [[ "$(sed -n "s/^${key}=//p" "$STATE_FILE" | tail -1)" == "1" ]]
}

docker_service_defined() {
  compose config --services 2>/dev/null | grep -qx "$1"
}

mail_worker_enabled() {
  [[ -f "$BASE_DIR/config.yaml" ]] || return 1
  awk '
    /^mail:[[:space:]]*$/ { in_mail=1; next }
    in_mail && /^[^[:space:]#]/ { exit }
    in_mail && /^  enabled:[[:space:]]*/ {
      value=$0
      sub(/^  enabled:[[:space:]]*/, "", value)
      sub(/[[:space:]]+#.*$/, "", value)
      gsub(/[[:space:]"]/, "", value)
      mail=(tolower(value) ~ /^(1|true|yes|on)$/)
    }
    in_mail && /^  worker:[[:space:]]*$/ { in_worker=1; next }
    in_mail && in_worker && /^    enabled:[[:space:]]*/ {
      value=$0
      sub(/^    enabled:[[:space:]]*/, "", value)
      sub(/[[:space:]]+#.*$/, "", value)
      gsub(/[[:space:]"]/, "", value)
      worker=(tolower(value) ~ /^(1|true|yes|on)$/)
    }
    END { exit !(mail && worker) }
  ' "$BASE_DIR/config.yaml"
}

stop_dockerized_normal_services() {
  # Maintenance mode owns the runtime/service boundary, not the long-lived
  # frontend/proxy boundary. Stop every normal runtime service that can be
  # defined for this installation; missing/stopped services are harmless.
  local services=(api)
  state_enabled LOCAL_QDRANT && services+=(qdrant)
  state_enabled LOCAL_RESOLVER && services+=(erg-resolver)
  state_enabled LOCAL_NEO4J && services+=(neo4j)
  state_enabled LOCAL_PLAYWRIGHT && services+=(playwright-renderer)
  docker_service_defined graph-worker && services+=(graph-worker)
  docker_service_defined sync-worker && services+=(sync-worker)
  docker_service_defined mail-worker && services+=(mail-worker)
  compose stop "${services[@]}" >/dev/null 2>&1 || true
}

enable_dockerized() {
  stop_dockerized_normal_services
  # Materialize the just-updated runtime.env before start-all.sh sources
  # runtime.service.env and decides which desired state to start.
  sync_container_secrets
  # start-all.sh is the single desired-state dispatcher. With
  # RAG_MAINTENANCE_MODE=true it converges even a completely stopped stack to
  # the maintenance state, including retained proxy/OpenWebUI frontends.
  "$BASE_DIR/start-all.sh"
}

disable_dockerized() {
  # Materialize RAG_MAINTENANCE_MODE=false before start-all.sh reads
  # runtime.service.env.
  sync_container_secrets
  # Likewise, leaving maintenance must converge from any prior state
  # (maintenance, partially running, or fully stopped) to normal operation.
  # start-all.sh knows the complete configured Dockerized service set.
  "$BASE_DIR/start-all.sh"
}

case "${1:-}" in
  status)
    public_deployment="$deployment_mode"
    [[ "$public_deployment" == "native" ]] && public_deployment="standard"
    echo "maintenance=$(current_mode) deployment=$public_deployment"
    ;;
  on)
    set_mode true
    if [[ "$deployment_mode" == "dockerized" ]]; then enable_dockerized; else enable_native; fi
    echo "SunaQ maintenance mode enabled."
    ;;
  off)
    set_mode false
    if { [[ "$deployment_mode" == "dockerized" ]] && disable_dockerized; } || { [[ "$deployment_mode" != "dockerized" ]] && disable_native; }; then
      echo "SunaQ maintenance mode disabled; normal services started."
    else
      rc=$?
      echo "Normal startup failed; restoring maintenance mode." >&2
      set_mode true
      if [[ "$deployment_mode" == "dockerized" ]]; then
        enable_dockerized || true
      else
        enable_native || true
      fi
      exit "$rc"
    fi
    ;;
  *)
    usage
    ;;
esac
