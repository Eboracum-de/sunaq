#!/usr/bin/env bash
set -u
PREFIX="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$PREFIX/.venv/bin/python"
FAIL=0

OWNER="$(stat -c '%U' "$PREFIX" 2>/dev/null || id -un)"
OWNER_HOME="$(getent passwd "$OWNER" 2>/dev/null | cut -d: -f6 || true)"

run_as_owner() {
  if [[ ${EUID:-$(id -u)} -eq 0 && -n "$OWNER" && "$OWNER" != "root" ]]; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$OWNER" -m -- env HOME="${OWNER_HOME:-/tmp}" "$@"
      return
    elif command -v sudo >/dev/null 2>&1; then
      sudo -u "$OWNER" -H -- "$@"
      return
    fi
  fi
  "$@"
}

LOCAL_QDRANT=0
LOCAL_NEO4J=0
LOCAL_PLAYWRIGHT=0
LOCAL_OPENWEBUI=0
LOCAL_PROXY=0
MULTI_USER=0
DEPLOYMENT_PROFILE=""
DEPLOYMENT_MODE=""
if [[ -f "$PREFIX/install/install-state.env" ]]; then
  source "$PREFIX/install/install-state.env"
fi

set -a
[[ -f "$PREFIX/provider.env" ]] && source "$PREFIX/provider.env"
[[ -f "$PREFIX/runtime.env" ]] && source "$PREFIX/runtime.env"
set +a

# Older install-state files predate profile/mode markers. Fall back to the
# simple scalar deployment block in config.yaml so a Super-Light installation
# is never mistaken for a native host-Python deployment.
yaml_block_scalar() {
  local section="$1" key="$2" file="$3"
  awk -v section="$section" -v key="$key" '
    $0 ~ "^" section ":[[:space:]]*$" { in_section=1; next }
    in_section && $0 ~ "^[^[:space:]#]" { exit }
    in_section {
      line=$0
      sub(/^[[:space:]]+/, "", line)
      if (line ~ "^" key ":[[:space:]]*") {
        sub("^" key ":[[:space:]]*", "", line)
        gsub(/^[[:space:]"]+|[[:space:]"]+$/, "", line)
        print line
        exit
      }
    }
  ' "$file"
}

if [[ -f "$PREFIX/config.yaml" ]]; then
  [[ -n "$DEPLOYMENT_PROFILE" ]] || DEPLOYMENT_PROFILE="$(yaml_block_scalar deployment profile "$PREFIX/config.yaml")"
  [[ -n "$DEPLOYMENT_MODE" ]] || DEPLOYMENT_MODE="$(yaml_block_scalar deployment mode "$PREFIX/config.yaml")"
  QDRANT_ENABLED="$(yaml_block_scalar qdrant enabled "$PREFIX/config.yaml")"
else
  QDRANT_ENABLED=""
fi

ok() { echo "[ OK ] $*"; }
warn() { echo "[WARN] $*"; }
bad() { echo "[FAIL] $*"; FAIL=1; }

MAINTENANCE_ACTIVE=0
case "$(printf '%s' "${RAG_MAINTENANCE_MODE:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) MAINTENANCE_ACTIVE=1 ;;
esac

if [[ -x "$PY" ]]; then
  ok "Python venv"
elif [[ "$DEPLOYMENT_MODE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "super-light" ]]; then
  ok "Host Python venv not required ($DEPLOYMENT_PROFILE/$DEPLOYMENT_MODE)"
else
  bad "Python venv missing"
fi
if [[ -x "$PY" ]]; then
  echo "[INFO] Checking lightweight Python imports ..."
  run_as_owner "$PY" - <<'PY' >/dev/null 2>&1 && ok "Python core imports" || bad "Python core imports"
import fastapi, httpx, requests, yaml, neo4j, qdrant_client, rapidfuzz, vobject
PY

  echo "[INFO] Checking ML imports (torch/transformers can take a while on a cold filesystem) ..."
  ml_tmp="$(mktemp)"
  ( run_as_owner "$PY" - <<'PY' >"$ml_tmp" 2>&1
import transformers, torch
assert torch.__version__ == "2.13.0+cpu", torch.__version__
assert transformers.__version__ == "4.57.6", transformers.__version__
import torch.export
from transformers import AutoModelForSequenceClassification
PY
  ) &
  ml_pid=$!
  ml_elapsed=0
  while kill -0 "$ml_pid" 2>/dev/null; do
    sleep 10
    ml_elapsed=$((ml_elapsed + 10))
    if kill -0 "$ml_pid" 2>/dev/null; then
      echo "[INFO] ML imports still running (${ml_elapsed}s) ..."
    fi
  done
  if wait "$ml_pid"; then
    ok "Python ML imports"
  else
    cat "$ml_tmp" >&2
    bad "Python ML imports"
  fi
  rm -f "$ml_tmp"

  (cd "$PREFIX" && run_as_owner "$PY" -m compileall -q rag) && ok "rag compileall" || bad "rag compileall"
  (cd "$PREFIX" && run_as_owner "$PY" - <<'PY' >/dev/null 2>&1) && ok "RAG application imports" || bad "RAG application imports"
import rag.api
import rag.openai_provider
PY

  if [[ -f "$PREFIX/runtime/users.sqlite" ]]; then
    if (cd "$PREFIX" && run_as_owner "$PY" -c 'from rag.credential_store import CredentialStore; assert CredentialStore("runtime/users.sqlite").client_count() >= 1' >/dev/null 2>&1); then
      ok "Trusted provider client registry"
    else
      bad "Trusted provider client registry missing/empty"
    fi
  fi

  for db in graph_queue.sqlite research.sqlite runtime/users.sqlite; do
    if [[ -e "$PREFIX/$db" ]]; then
      if run_as_owner test -w "$PREFIX/$db"; then
        ok "Runtime DB writable ($db)"
      else
        bad "Runtime DB not writable by $OWNER ($db)"
      fi
    fi
  done
fi

QDRANT_URL="http://127.0.0.1:6333"
QDRANT_COLLECTION="nextcloud_rag"
if [[ -x "$PY" && -f "$PREFIX/config.yaml" ]]; then
  mapfile -t QDRANT_CFG < <(cd "$PREFIX" && run_as_owner "$PY" - <<'PY'
import yaml
with open('config.yaml', encoding='utf-8') as f:
    cfg = yaml.safe_load(f) or {}
q = cfg.get('qdrant') or {}
print(str(q.get('url') or 'http://127.0.0.1:6333').rstrip('/'))
print(str(q.get('collection') or 'nextcloud_rag'))
PY
)
  [[ ${#QDRANT_CFG[@]} -ge 1 ]] && QDRANT_URL="${QDRANT_CFG[0]}"
  [[ ${#QDRANT_CFG[@]} -ge 2 ]] && QDRANT_COLLECTION="${QDRANT_CFG[1]}"
fi

if [[ "$QDRANT_ENABLED" == "false" ]]; then
  ok "Qdrant disabled by configuration"
elif curl -fsS "$QDRANT_URL/collections" >/dev/null 2>&1; then
  ok "Qdrant HTTP ($QDRANT_URL)"
  if [[ -x "$PY" ]]; then
    echo "[INFO] Testing configured embedding backend and Qdrant write/read ..."
    if (cd "$PREFIX" && run_as_owner "$PY" -m rag.qdrant_smoke --config config.yaml); then
      ok "Embedding + Qdrant smoke point ($QDRANT_COLLECTION)"
    else
      bad "Embedding/Qdrant smoke probe"
    fi
  fi
else
  [[ $LOCAL_QDRANT -eq 1 ]] && bad "Qdrant HTTP ($QDRANT_URL)" || echo "[INFO] Qdrant not selected/reachable at configured URL: $QDRANT_URL"
fi

if curl -fsS http://127.0.0.1:7474 >/dev/null 2>&1; then
  ok "Neo4j HTTP"
elif [[ "$DEPLOYMENT_MODE" == "dockerized" && $MAINTENANCE_ACTIVE -eq 1 && $LOCAL_NEO4J -eq 1 ]]; then
  ok "Neo4j deferred until maintenance mode is disabled"
else
  [[ $LOCAL_NEO4J -eq 1 ]] && bad "Neo4j HTTP" || echo "[INFO] Local Neo4j not selected; configure a remote endpoint if graph retrieval is required."
fi

if [[ $LOCAL_PLAYWRIGHT -eq 1 ]]; then
  playwright_live="$(curl -fsS "http://127.0.0.1:${PLAYWRIGHT_PORT:-8090}/live" 2>/dev/null || true)"
  if printf '%s' "$playwright_live" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "Playwright renderer"
  elif [[ -n "$playwright_live" ]]; then
    warn "Playwright renderer is running but browser launch is degraded"
  elif [[ "$DEPLOYMENT_MODE" == "dockerized" && $MAINTENANCE_ACTIVE -eq 1 ]]; then
    ok "Playwright renderer deferred until maintenance mode is disabled"
  else
    warn "Playwright renderer unavailable; Web research remains usable but rendered-PDF archival is disabled"
  fi
fi

if [[ $LOCAL_OPENWEBUI -eq 1 ]]; then
  if curl -fsS "http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health" >/dev/null 2>&1; then
    ok "OpenWebUI HTTP (127.0.0.1:${OPENWEBUI_PORT:-3000})"
  else
    bad "OpenWebUI HTTP (127.0.0.1:${OPENWEBUI_PORT:-3000})"
  fi
fi


if [[ $LOCAL_PROXY -eq 1 ]]; then
  if curl -kfsS https://127.0.0.1/proxy-health >/dev/null 2>&1; then
    ok "nginx reverse proxy HTTPS"
  else
    bad "nginx reverse proxy HTTPS"
  fi
  if curl -sSI http://127.0.0.1/ 2>/dev/null | head -1 | grep -Eq ' 30[1278] '; then
    ok "nginx HTTP -> HTTPS redirect"
  else
    bad "nginx HTTP -> HTTPS redirect"
  fi
fi

api_probe_host="${RAG_API_HOST:-127.0.0.1}"
[[ "$api_probe_host" == "0.0.0.0" || "$api_probe_host" == "::" ]] && api_probe_host="127.0.0.1"
provider_probe_host="${PROVIDER_HOST:-0.0.0.0}"
[[ "$provider_probe_host" == "0.0.0.0" || "$provider_probe_host" == "::" ]] && provider_probe_host="127.0.0.1"

api_auth_args=()
[[ -n "${RAG_INTERNAL_API_KEY:-}" ]] && api_auth_args=(-H "X-AKI-Internal-Key: ${RAG_INTERNAL_API_KEY}")
if curl -fsS "${api_auth_args[@]}" "http://${api_probe_host}:${RAG_API_PORT:-8765}/health" >/dev/null 2>&1; then
  ok "RAG API health"
else
  echo "[INFO] RAG API not running yet."
fi
provider_health_json="$(curl -fsS "http://${provider_probe_host}:${PROVIDER_PORT:-8766}/health" 2>/dev/null || true)"
if [[ -n "$provider_health_json" ]]; then
  ok "Provider health"
  if command -v jq >/dev/null 2>&1; then
    provider_maintenance="$(printf '%s' "$provider_health_json" | jq -r '(.maintenance // false) or (.status == "maintenance")' 2>/dev/null || echo false)"
    if [[ "$provider_maintenance" == "true" ]]; then
      echo "[INFO] LLM role health deferred while provider is in maintenance mode."
    else
      llm_status="$(printf '%s' "$provider_health_json" | jq -r '.llm.status // "unknown"' 2>/dev/null || echo unknown)"
      if [[ "$llm_status" == "ok" ]]; then
        ok "LLM roles reachable"
      else
        bad "LLM roles not fully reachable (status=$llm_status)"
        printf '%s' "$provider_health_json" | jq -r '
          (.llm.roles // {}) | to_entries[] |
          "       \(.key): status=\(.value.status // "unknown") backend=\(.value.backend // "—") model=\(.value.model // "—")" +
          (if .value.error then " error=\(.value.error)" else "" end)
        ' 2>/dev/null || true
      fi
    fi
  else
    warn "jq unavailable; LLM role health could not be evaluated from provider /health"
  fi
else
  echo "[INFO] Provider not running yet."
fi

exit "$FAIL"
