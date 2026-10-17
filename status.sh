#!/usr/bin/env bash
set -u
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"
set -a
[[ -f provider.env ]] && source provider.env
[[ -f runtime.env ]] && source runtime.env
set +a
RUN_DIR="$BASE_DIR/run"
LOCAL_QDRANT=0
LOCAL_PROXY=0
LOCAL_RESOLVER=0
LOCAL_OPENWEBUI=0
LOCAL_PLAYWRIGHT=0
ARCHITECTURE_TIER=""
DEPLOYMENT_MODE=""
[[ -f install/install-state.env ]] && source install/install-state.env
if [[ -z "${DEPLOYMENT_MODE:-}" && -f .sunaq-installation ]]; then
  DEPLOYMENT_MODE="$(sed -n 's/^DEPLOYMENT_MODE=//p' .sunaq-installation | tail -1)"
fi
[[ "$DEPLOYMENT_MODE" == "dockerized" ]] || DEPLOYMENT_MODE="native"
DISPLAY_DEPLOYMENT="$DEPLOYMENT_MODE"
[[ "$DISPLAY_DEPLOYMENT" == "native" ]] && DISPLAY_DEPLOYMENT="standard"
printf '%-14s %s\n' deployment "$DISPLAY_DEPLOYMENT"

if [[ -z "$ARCHITECTURE_TIER" && -f config.yaml ]]; then
  ARCHITECTURE_TIER="$(awk '
    /^architecture:[[:space:]]*$/ { in_section=1; next }
    in_section && /^[^[:space:]#]/ { exit }
    in_section && /^  tier:[[:space:]]*/ {
      value=$0
      sub(/^  tier:[[:space:]]*/, "", value)
      sub(/[[:space:]]+#.*$/, "", value)
      gsub(/[[:space:]"]/, "", value)
      print value
      exit
    }
  ' config.yaml)"
fi
[[ "$ARCHITECTURE_TIER" == "erg" ]] && ARCHITECTURE_TIER="erg_full"
[[ -n "$ARCHITECTURE_TIER" ]] && printf '%-14s %s\n' architecture "$ARCHITECTURE_TIER"

proc_status() {
  local name="$1" pidfile="$RUN_DIR/$1.pid"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%-14s RUNNING pid=%s\n' "$name" "$pid"
      return
    fi
  fi
  printf '%-14s STOPPED\n' "$name"
}

dockerized_compose() {
  (
    cd "$BASE_DIR/install/dockerized"
    if docker compose version >/dev/null 2>&1; then
      docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
      docker-compose "$@"
    else
      return 127
    fi
  )
}

docker_service_status() {
  local label="$1" service="$2" cid=""
  cid="$(dockerized_compose ps -q "$service" 2>/dev/null | head -1 || true)"
  if [[ -n "$cid" ]] && docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null | grep -qx true; then
    printf '%-14s RUNNING container=%s\n' "$label" "${cid:0:12}"
  else
    printf '%-14s STOPPED\n' "$label"
  fi
}

docker_service_defined() {
  dockerized_compose config --services 2>/dev/null | grep -qx "$1"
}

if [[ "$DEPLOYMENT_MODE" == "dockerized" ]]; then
  docker_service_status api api
  docker_service_status provider provider
  [[ "${LOCAL_QDRANT:-0}" == "1" ]] && docker_service_status qdrant qdrant
  [[ "${LOCAL_NEO4J:-0}" == "1" ]] && docker_service_status neo4j neo4j
  docker_service_defined graph-worker && docker_service_status graph-worker graph-worker
  docker_service_defined sync-worker && docker_service_status sync-worker sync-worker
  [[ "${LOCAL_RESOLVER:-0}" == "1" ]] && docker_service_status erg-resolver erg-resolver
  [[ "${LOCAL_PLAYWRIGHT:-0}" == "1" ]] && docker_service_status playwright playwright-renderer
  [[ "${LOCAL_OPENWEBUI:-0}" == "1" ]] && docker_service_status openwebui openwebui
  [[ "${LOCAL_PROXY:-0}" == "1" ]] && docker_service_status proxy proxy
  docker_service_defined mail-worker && docker_service_status mail-worker mail-worker
else
  proc_status api
  proc_status provider
  proc_status graph-worker
  proc_status sync-worker
  proc_status mail-worker
fi

maintenance_value="${RAG_MAINTENANCE_MODE:-false}"
maintenance_value="$(printf '%s' "$maintenance_value" | tr '[:upper:]' '[:lower:]')"
case "$maintenance_value" in
  1|true|yes|on) printf '%-14s ENABLED\n' maintenance ;;
  *) printf '%-14s disabled\n' maintenance ;;
esac

printf '\nEndpoints:\n'
api_probe_host="${RAG_API_HOST:-127.0.0.1}"
[[ "$api_probe_host" == "0.0.0.0" || "$api_probe_host" == "::" ]] && api_probe_host="127.0.0.1"
provider_probe_host="${PROVIDER_HOST:-127.0.0.1}"
[[ "$provider_probe_host" == "0.0.0.0" || "$provider_probe_host" == "::" ]] && provider_probe_host="127.0.0.1"

api_auth_args=()
[[ -n "${RAG_INTERNAL_API_KEY:-}" ]] && api_auth_args=(-H "X-AKI-Internal-Key: ${RAG_INTERNAL_API_KEY}")
if curl -fsS --max-time 3 "${api_auth_args[@]}" "http://${api_probe_host}:${RAG_API_PORT:-8765}/health" >/dev/null 2>&1; then
  printf '%-14s OK http://%s:%s/health\n' api "$api_probe_host" "${RAG_API_PORT:-8765}"
else
  printf '%-14s DOWN http://%s:%s/health\n' api "$api_probe_host" "${RAG_API_PORT:-8765}"
fi
if curl -fsS --max-time 3 "http://${provider_probe_host}:${PROVIDER_PORT:-8766}/live" >/dev/null 2>&1; then
  printf '%-14s OK http://%s:%s/live\n' provider "$provider_probe_host" "${PROVIDER_PORT:-8766}"
else
  printf '%-14s DOWN http://%s:%s/live\n' provider "$provider_probe_host" "${PROVIDER_PORT:-8766}"
fi


if [[ ${LOCAL_QDRANT:-0} -eq 1 ]]; then
  if curl -fsS --max-time 3 "http://127.0.0.1:6333/collections" >/dev/null 2>&1; then
    printf '%-14s OK http://127.0.0.1:6333/collections\n' qdrant
  else
    printf '%-14s DOWN http://127.0.0.1:6333/collections\n' qdrant
  fi
fi

if [[ ${LOCAL_RESOLVER:-0} -eq 1 ]]; then
  if curl -fsS --max-time 3 "http://127.0.0.1:8770/health" >/dev/null 2>&1; then
    printf '%-14s OK http://127.0.0.1:8770/health\n' erg-resolver
  else
    printf '%-14s DOWN http://127.0.0.1:8770/health\n' erg-resolver
  fi
fi

if [[ ${LOCAL_PLAYWRIGHT:-0} -eq 1 ]]; then
  if curl -fsS --max-time 3 "http://127.0.0.1:${PLAYWRIGHT_PORT:-8090}/live" >/dev/null 2>&1; then
    printf '%-14s OK http://127.0.0.1:%s/live\n' playwright "${PLAYWRIGHT_PORT:-8090}"
  else
    printf '%-14s DOWN http://127.0.0.1:%s/live\n' playwright "${PLAYWRIGHT_PORT:-8090}"
  fi
fi

if [[ ${LOCAL_OPENWEBUI:-0} -eq 1 ]]; then
  if curl -fsS --max-time 3 "http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health" >/dev/null 2>&1; then
    printf '%-14s OK http://127.0.0.1:%s/health
' openwebui "${OPENWEBUI_PORT:-3000}"
  else
    printf '%-14s DOWN http://127.0.0.1:%s/health
' openwebui "${OPENWEBUI_PORT:-3000}"
  fi
fi

if [[ ${LOCAL_PROXY:-0} -eq 1 ]]; then
  PROXY_HTTPS_PORT="${PROXY_HTTPS_PORT:-443}"
  if curl -kfsS --max-time 3 "https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health" >/dev/null 2>&1; then
    printf '%-14s OK https://127.0.0.1:%s/proxy-health\n' proxy "$PROXY_HTTPS_PORT"
  else
    printf '%-14s DOWN https://127.0.0.1:%s/proxy-health\n' proxy "$PROXY_HTTPS_PORT"
  fi
fi
