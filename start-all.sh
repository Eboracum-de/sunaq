#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

source "$BASE_DIR/install/load-service-env.sh" "$BASE_DIR"

DEPLOYMENT_MODE=""
LOCAL_QDRANT=0
LOCAL_NEO4J=0
LOCAL_RESOLVER=0
LOCAL_PLAYWRIGHT=0
LOCAL_OPENWEBUI=0
LOCAL_PROXY=0
STATE_FILE="$BASE_DIR/install/install-state.env"
if [[ -f "$STATE_FILE" ]]; then
  source "$STATE_FILE"
fi
if [[ -z "${DEPLOYMENT_MODE:-}" && -f "$BASE_DIR/.sunaq-installation" ]]; then
  DEPLOYMENT_MODE="$(sed -n 's/^DEPLOYMENT_MODE=//p' "$BASE_DIR/.sunaq-installation" | tail -1)"
fi
[[ "$DEPLOYMENT_MODE" == "dockerized" ]] || DEPLOYMENT_MODE="native"

compose_dockerized() {
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

config_enabled() {
  local section="$1" nested="${2:-}"
  [[ -f "$BASE_DIR/config.yaml" ]] || return 1
  if [[ -z "$nested" ]]; then
    awk -v section="$section" '
      $0 ~ "^" section ":[[:space:]]*$" { in_section=1; next }
      in_section && /^[^[:space:]#]/ { exit }
      in_section && /^  enabled:[[:space:]]*/ {
        value=$0; sub(/^  enabled:[[:space:]]*/, "", value)
        sub(/[[:space:]]+#.*$/, "", value); gsub(/[[:space:]"]/, "", value)
        exit !(tolower(value) ~ /^(1|true|yes|on)$/)
      }
      END { if (!in_section) exit 1 }
    ' "$BASE_DIR/config.yaml"
  else
    awk -v section="$section" -v nested="$nested" '
      $0 ~ "^" section ":[[:space:]]*$" { in_section=1; next }
      in_section && /^[^[:space:]#]/ { exit }
      in_section && $0 ~ "^  " nested ":[[:space:]]*$" { in_nested=1; next }
      in_section && in_nested && /^    enabled:[[:space:]]*/ {
        value=$0; sub(/^    enabled:[[:space:]]*/, "", value)
        sub(/[[:space:]]+#.*$/, "", value); gsub(/[[:space:]"]/, "", value)
        exit !(tolower(value) ~ /^(1|true|yes|on)$/)
      }
      END { if (!in_section || !in_nested) exit 1 }
    ' "$BASE_DIR/config.yaml"
  fi
}

start_dockerized() {
  command -v docker >/dev/null 2>&1 || { echo "docker is required for this deployment." >&2; exit 1; }
  bash "$BASE_DIR/install/sync-container-secrets.sh" "$BASE_DIR" >/dev/null

  local maintenance="${RAG_MAINTENANCE_MODE:-false}"
  maintenance="$(printf '%s' "$maintenance" | tr '[:upper:]' '[:lower:]')"
  local maintenance_on=0
  case "$maintenance" in 1|true|yes|on) maintenance_on=1 ;; esac

  local services=(provider)
  if [[ $maintenance_on -eq 0 ]]; then
    services=(api provider)
    [[ "${LOCAL_QDRANT:-0}" == "1" ]] && services+=(qdrant)
    [[ "${LOCAL_NEO4J:-0}" == "1" ]] && services+=(neo4j)
    [[ "${LOCAL_RESOLVER:-0}" == "1" ]] && services+=(erg-resolver)
    [[ "${LOCAL_PLAYWRIGHT:-0}" == "1" ]] && services+=(playwright-renderer)
    if compose_dockerized config --services 2>/dev/null | grep -qx graph-worker; then
      services+=(graph-worker)
    fi
    if compose_dockerized config --services 2>/dev/null | grep -qx sync-worker; then
      services+=(sync-worker)
    fi
    if config_enabled mail worker; then
      services+=(mail-worker)
    fi
  fi
  [[ "${LOCAL_OPENWEBUI:-0}" == "1" ]] && services+=(openwebui)
  [[ "${LOCAL_PROXY:-0}" == "1" ]] && services+=(proxy)

  compose_dockerized up -d --force-recreate --remove-orphans "${services[@]}"
  printf 'Dockerized SunaQ started (%s mode).\n' "$([[ $maintenance_on -eq 1 ]] && echo maintenance || echo normal)"
  printf 'Use ./status.sh for health/status.\n'
}

if [[ "$DEPLOYMENT_MODE" == "dockerized" ]]; then
  start_dockerized
  exit 0
fi

# If invoked as root, drop privileges to the owner of the install tree.
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  owner="$(stat -c '%U' "$BASE_DIR")"
  if [[ -n "$owner" && "$owner" != "root" ]]; then
    if command -v runuser >/dev/null 2>&1; then
      exec runuser -u "$owner" -- "$0" "$@"
    elif command -v sudo >/dev/null 2>&1; then
      exec sudo -u "$owner" -H "$0" "$@"
    fi
  fi
fi

RUN_DIR="$BASE_DIR/run"
LOG_DIR="$BASE_DIR/log"
mkdir -p "$RUN_DIR" "$LOG_DIR"

start_one() {
  local name="$1" script="$2" pidfile="$RUN_DIR/$1.pid" logfile="$LOG_DIR/$1.log"
  if [[ -f "$pidfile" ]]; then
    local oldpid
    oldpid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
      printf '%-14s already running pid=%s\n' "$name" "$oldpid"
      return 0
    fi
    rm -f "$pidfile"
  fi
  nohup /bin/bash "$BASE_DIR/$script" >>"$logfile" 2>&1 &
  local pid=$!
  echo "$pid" > "$pidfile"
  sleep 0.25
  if kill -0 "$pid" 2>/dev/null; then
    printf '%-14s started pid=%s log=%s\n' "$name" "$pid" "$logfile"
  else
    printf '%-14s failed; see %s\n' "$name" "$logfile" >&2
    rm -f "$pidfile"
    return 1
  fi
}

maintenance_mode="${RAG_MAINTENANCE_MODE:-false}"
maintenance_mode="$(printf '%s' "$maintenance_mode" | tr '[:upper:]' '[:lower:]')"
if [[ "$maintenance_mode" == "1" || "$maintenance_mode" == "true" || "$maintenance_mode" == "yes" || "$maintenance_mode" == "on" ]]; then
  start_one provider start-openwebui-provider.sh
  printf '\nMaintenance mode is enabled: API/workers are intentionally not started.\n'
  printf 'Set RAG_MAINTENANCE_MODE=false in runtime.env, then run ./start-all.sh for normal operation.\n'
  exit 0
fi

start_one api start-api.sh
start_one provider start-openwebui-provider.sh

graph_enabled="$($BASE_DIR/.venv/bin/python - <<'PYCFG'
import yaml
try:
    with open('config.yaml', encoding='utf-8') as f: cfg=yaml.safe_load(f) or {}
    gq = cfg.get('graph_queue') or {}
    worker = gq.get('worker') or {}
    print('1' if bool((cfg.get('neo4j') or {}).get('enabled', True)) and bool(gq.get('enabled', False)) and bool(worker.get('enabled', False)) else '0')
except Exception:
    print('0')
PYCFG
)"
if [[ "$graph_enabled" == "1" ]]; then
  start_one graph-worker start-graph-worker.sh
else
  printf '%-14s skipped (Neo4j/graph worker disabled)\n' graph-worker
fi

sync_enabled="$($BASE_DIR/.venv/bin/python - <<'PYCFG'
import yaml
try:
    with open('config.yaml', encoding='utf-8') as f: cfg=yaml.safe_load(f) or {}
    worker = cfg.get('sync_worker') or {}
    qdrant = cfg.get('qdrant') or {}
    print('1' if bool(worker.get('enabled', True)) and bool(qdrant.get('enabled', True)) else '0')
except Exception:
    print('0')
PYCFG
)"
if [[ "$sync_enabled" == "1" ]]; then
  start_one sync-worker start-sync-worker.sh
else
  printf '%-14s skipped (sync worker/Qdrant disabled)\n' sync-worker
fi

mail_enabled="$("$BASE_DIR/.venv/bin/python" - <<'PYCFG'
import yaml
try:
    with open('config.yaml', encoding='utf-8') as f: cfg=yaml.safe_load(f) or {}
    mail = cfg.get('mail') or {}
    worker = mail.get('worker') or {}
    print('1' if bool(mail.get('enabled', False)) and bool(worker.get('enabled', False)) else '0')
except Exception:
    print('0')
PYCFG
)"
if [[ "$mail_enabled" == "1" ]]; then
  start_one mail-worker start-mail-worker.sh
else
  printf '%-14s skipped (mail feature/worker disabled)\n' mail-worker
fi

printf '\nUse ./status.sh for health/status.\n'
