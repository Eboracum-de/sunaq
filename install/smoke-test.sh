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
LOCAL_RESOLVER=0
LOCAL_PLAYWRIGHT=0
LOCAL_OPENWEBUI=0
LOCAL_PROXY=0
MULTI_USER=0
DEPLOYMENT_PROFILE=""
DEPLOYMENT_MODE=""
ARCHITECTURE_TIER=""
if [[ -f "$PREFIX/install/install-state.env" ]]; then
  source "$PREFIX/install/install-state.env"
fi
PROXY_HTTP_PORT="${PROXY_HTTP_PORT:-80}"
PROXY_HTTPS_PORT="${PROXY_HTTPS_PORT:-443}"

set -a
[[ -f "$PREFIX/provider.env" ]] && source "$PREFIX/provider.env"
[[ -f "$PREFIX/runtime.env" ]] && source "$PREFIX/runtime.env"
set +a

# Older install-state files predate profile/mode markers. Fall back to the
# simple scalar deployment block in config.yaml so a Dockerized installation
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

CONFIG_ARCHITECTURE_TIER=""
if [[ -f "$PREFIX/config.yaml" ]]; then
  [[ -n "$DEPLOYMENT_PROFILE" ]] || DEPLOYMENT_PROFILE="$(yaml_block_scalar deployment profile "$PREFIX/config.yaml")"
  [[ -n "$DEPLOYMENT_MODE" ]] || DEPLOYMENT_MODE="$(yaml_block_scalar deployment mode "$PREFIX/config.yaml")"
  CONFIG_ARCHITECTURE_TIER="$(yaml_block_scalar architecture tier "$PREFIX/config.yaml")"
  [[ "$CONFIG_ARCHITECTURE_TIER" == "erg" ]] && CONFIG_ARCHITECTURE_TIER="erg_full"
  QDRANT_ENABLED="$(yaml_block_scalar qdrant enabled "$PREFIX/config.yaml")"
else
  QDRANT_ENABLED=""
fi

SUNAQ_RUNTIME_IMAGE=""
if [[ "$DEPLOYMENT_MODE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "super-light" ]]; then
  SUNAQ_RUNTIME_IMAGE="$(
    awk '/^[[:space:]]*image:[[:space:]]+sunaq:/{print $2; exit}'       "$PREFIX/install/dockerized/docker-compose.yml" 2>/dev/null || true
  )"
fi

run_smoke_python() {
  if [[ -x "$PY" ]]; then
    (cd "$PREFIX" && run_as_owner "$PY" "$@")
    return
  fi
  if [[ "$DEPLOYMENT_MODE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "super-light" ]]; then
    [[ -n "$SUNAQ_RUNTIME_IMAGE" ]] || {
      echo "Dockerized runtime image could not be determined." >&2
      return 127
    }
    [[ -f "$PREFIX/runtime.container.env" ]] || {
      echo "Dockerized runtime.container.env is missing; file-backed service credentials are unavailable." >&2
      return 127
    }
    docker run --rm -i --network host       --env-file "$PREFIX/runtime.container.env"       -v "$PREFIX/config.yaml:/app/config.yaml:ro,z"       -v "$PREFIX/runtime:/app/runtime:ro,z"       -v "$PREFIX/runtime/service-secrets:/run/sunaq-secrets:ro,z"       -w /app       "$SUNAQ_RUNTIME_IMAGE"       python "$@"
    return
  fi
  echo "No Python runtime available for smoke probe." >&2
  return 127
}

ok() { echo "[ OK ] $*"; }
warn() { echo "[WARN] $*"; }
bad() { echo "[FAIL] $*"; FAIL=1; }

MAINTENANCE_ACTIVE=0
case "$(printf '%s' "${RAG_MAINTENANCE_MODE:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) MAINTENANCE_ACTIVE=1 ;;
esac

if [[ -z "$CONFIG_ARCHITECTURE_TIER" ]]; then
  bad "architecture.tier missing from authoritative config.yaml${ARCHITECTURE_TIER:+ (install-state reports $ARCHITECTURE_TIER)}"
elif [[ -n "$ARCHITECTURE_TIER" && "$ARCHITECTURE_TIER" != "$CONFIG_ARCHITECTURE_TIER" ]]; then
  bad "Architecture tier mismatch: config.yaml=$CONFIG_ARCHITECTURE_TIER install-state=$ARCHITECTURE_TIER"
else
  ARCHITECTURE_TIER="$CONFIG_ARCHITECTURE_TIER"
  ok "Architecture tier: $ARCHITECTURE_TIER"
fi

if [[ -x "$PY" ]]; then
  ok "Python venv"
elif [[ "$DEPLOYMENT_MODE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "super-light" ]]; then
  ok "Host Python venv not required ($DEPLOYMENT_PROFILE/$DEPLOYMENT_MODE)"
else
  bad "Python venv missing"
fi
if [[ -f "$PREFIX/config.yaml" ]]; then
  echo "[INFO] Checking static SunaQ configuration ..."
  if [[ -x "$PY" ]]; then
    if (cd "$PREFIX" && run_as_owner "$PY" -m rag.config_check --config config.yaml); then
      ok "Configuration check"
    else
      bad "Configuration check"
    fi
  elif [[ "$DEPLOYMENT_MODE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "dockerized" || "$DEPLOYMENT_PROFILE" == "super-light" ]]; then
    if [[ -n "$SUNAQ_RUNTIME_IMAGE" ]] && command -v docker >/dev/null 2>&1; then
      if docker run --rm         -v "$PREFIX/config.yaml:/app/config.yaml:ro,z"         -v "$PREFIX/web.yaml:/app/web.yaml:ro,z"         -v "$PREFIX/runtime:/app/runtime:ro,z"         "$SUNAQ_RUNTIME_IMAGE"         python -m rag.config_check --config /app/config.yaml --web-config /app/web.yaml; then
        ok "Configuration check"
      else
        bad "Configuration check"
      fi
    else
      bad "Configuration check unavailable (runtime image not found)"
    fi
  else
    bad "Configuration check unavailable"
  fi
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

if [[ -f "$PREFIX/config.yaml" ]]; then
  echo "[INFO] Testing configured Elasticsearch search endpoint ..."
  if ES_TARGET="$(run_smoke_python - <<'PY'
import httpx
import yaml
from rag.elasticsearch_client import httpx_options
from rag.secret_env import secret_env

with open("config.yaml", encoding="utf-8") as handle:
    cfg = yaml.safe_load(handle) or {}
es = cfg.get("elasticsearch") or cfg.get("es") or {}
raw_enabled = es.get("enabled", True)
enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).strip().casefold() in {"1", "true", "yes", "on"}
if not enabled:
    print("DISABLED")
    raise SystemExit(0)

url = str(es.get("url") or "http://127.0.0.1:9200").rstrip("/")
index = str(es.get("index") or "").strip()
if not index:
    raise RuntimeError("elasticsearch.index is empty")

username = str(es.get("username") or es.get("user") or "").strip()
password_env = str(es.get("password_env") or "ELASTICSEARCH_PASSWORD").strip()
password_present = bool(secret_env(password_env, "").strip()) if password_env else bool(str(es.get("password") or "").strip())
timeout = max(1.0, min(float(es.get("timeout") or 10.0), 15.0))

with httpx.Client(timeout=timeout, **httpx_options(cfg)) as client:
    response = client.post(
        f"{url}/{index}/_search",
        json={"size": 0, "track_total_hits": False, "query": {"match_all": {}}},
    )
    if response.status_code in {401, 403}:
        raise RuntimeError(
            "Elasticsearch authentication rejected "
            f"(HTTP {response.status_code}; username={'set' if username else 'empty'}; "
            f"{password_env or 'password'}={'present' if password_present else 'empty'})"
        )
    response.raise_for_status()

print(f"{url}/{index}")
PY
  )"; then
    if [[ "$ES_TARGET" == "DISABLED" ]]; then
      ok "Elasticsearch disabled by configuration"
    else
      ok "Elasticsearch authenticated search ($ES_TARGET)"
    fi
  else
    bad "Elasticsearch configured/authenticated search probe"
  fi
fi

QDRANT_URL="http://127.0.0.1:6333"
QDRANT_COLLECTION="nextcloud_rag"
if [[ -f "$PREFIX/config.yaml" ]]; then
  mapfile -t QDRANT_CFG < <(run_smoke_python - <<'PY'
import yaml
with open("config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
q = cfg.get("qdrant") or {}
print(str(q.get("url") or "http://127.0.0.1:6333").rstrip("/"))
print(str(q.get("collection") or "nextcloud_rag"))
PY
)
  [[ ${#QDRANT_CFG[@]} -ge 1 ]] && QDRANT_URL="${QDRANT_CFG[0]}"
  [[ ${#QDRANT_CFG[@]} -ge 2 ]] && QDRANT_COLLECTION="${QDRANT_CFG[1]}"
fi

if [[ "$QDRANT_ENABLED" == "false" ]]; then
  ok "Qdrant disabled by configuration"
elif curl -fsS "$QDRANT_URL/collections" >/dev/null 2>&1; then
  ok "Qdrant HTTP ($QDRANT_URL)"
  echo "[INFO] Testing configured embedding backend and Qdrant write/read ..."
  qdrant_smoke_output="$(run_smoke_python -m rag.qdrant_smoke --config config.yaml 2>&1)"
  qdrant_smoke_rc=$?
  if [[ $qdrant_smoke_rc -eq 0 ]]; then
    ok "Embedding + Qdrant smoke point ($QDRANT_COLLECTION)"
  elif [[ $qdrant_smoke_rc -eq 20 ]]; then
    bad "Embedding offline"
    [[ -n "$qdrant_smoke_output" ]] && printf '       %s\n' "$qdrant_smoke_output"
  else
    bad "Embedding/Qdrant smoke probe"
    [[ -n "$qdrant_smoke_output" ]] && printf '       %s\n' "$qdrant_smoke_output"
  fi
elif [[ "$DEPLOYMENT_MODE" == "dockerized" && $MAINTENANCE_ACTIVE -eq 1 && $LOCAL_QDRANT -eq 1 ]]; then
  ok "Qdrant deferred until maintenance mode is disabled"
else
  [[ $LOCAL_QDRANT -eq 1 ]] && bad "Qdrant HTTP ($QDRANT_URL)" || echo "[INFO] Qdrant not selected/reachable at configured URL: $QDRANT_URL"
fi

if [[ "$ARCHITECTURE_TIER" == "src" ]]; then
  ok "Neo4j/graph disabled by SRC architecture"
elif curl -fsS http://127.0.0.1:7474 >/dev/null 2>&1; then
  ok "Neo4j HTTP"
elif [[ "$DEPLOYMENT_MODE" == "dockerized" && $MAINTENANCE_ACTIVE -eq 1 && $LOCAL_NEO4J -eq 1 ]]; then
  ok "Neo4j deferred until maintenance mode is disabled"
else
  [[ $LOCAL_NEO4J -eq 1 ]] && bad "Neo4j HTTP" || echo "[INFO] Local Neo4j not selected; configure a remote endpoint if graph/entity capabilities are required."
fi

if [[ $LOCAL_RESOLVER -eq 1 ]]; then
  resolver_health="$(curl -fsS http://127.0.0.1:8770/health 2>/dev/null || true)"
  resolver_caps="$(curl -fsS http://127.0.0.1:8770/capabilities 2>/dev/null || true)"
  if printf '%s' "$resolver_health" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    if printf '%s' "$resolver_caps" | grep -Eq '"adapters"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]'; then
      warn "ERG resolver healthy but no adapters are enabled"
    else
      ok "ERG resolver health"
    fi
    [[ -n "$resolver_caps" ]] && printf '[INFO] Resolver capabilities: %s\n' "$resolver_caps"
  elif [[ "$DEPLOYMENT_MODE" == "dockerized" && $MAINTENANCE_ACTIVE -eq 1 ]]; then
    ok "ERG resolver deferred until maintenance mode is disabled"
  elif [[ -n "$resolver_health" ]]; then
    bad "ERG resolver degraded"
    printf '       %s\n' "$resolver_health"
  else
    bad "ERG resolver unavailable (127.0.0.1:8770)"
  fi
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
  if curl -kfsS "https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health" >/dev/null 2>&1; then
    ok "nginx reverse proxy HTTPS (127.0.0.1:${PROXY_HTTPS_PORT})"
  else
    bad "nginx reverse proxy HTTPS (127.0.0.1:${PROXY_HTTPS_PORT})"
  fi
  if curl -sSI "http://127.0.0.1:${PROXY_HTTP_PORT}/" 2>/dev/null | head -1 | grep -Eq ' 30[1278] '; then
    ok "nginx HTTP -> HTTPS redirect (127.0.0.1:${PROXY_HTTP_PORT})"
  else
    bad "nginx HTTP -> HTTPS redirect (127.0.0.1:${PROXY_HTTP_PORT})"
  fi
fi

api_probe_host="${RAG_API_HOST:-127.0.0.1}"
[[ "$api_probe_host" == "0.0.0.0" || "$api_probe_host" == "::" ]] && api_probe_host="127.0.0.1"
provider_probe_host="${PROVIDER_HOST:-0.0.0.0}"
[[ "$provider_probe_host" == "0.0.0.0" || "$provider_probe_host" == "::" ]] && provider_probe_host="127.0.0.1"

api_auth_args=()
[[ -n "${RAG_INTERNAL_API_KEY:-}" ]] && api_auth_args=(-H "X-AKI-Internal-Key: ${RAG_INTERNAL_API_KEY}")
api_health_json="$(curl -fsS "${api_auth_args[@]}" "http://${api_probe_host}:${RAG_API_PORT:-8765}/health" 2>/dev/null || true)"
if [[ -n "$api_health_json" ]]; then
  ok "RAG API health"
  if command -v jq >/dev/null 2>&1; then
    files_status="$(printf '%s' "$api_health_json" | jq -r '.retrieval_arms.files.status // "unknown"' 2>/dev/null || echo unknown)"
    case "$files_status" in
      ok) ok "Elasticsearch retrieval arm via API health" ;;
      disabled) ok "Elasticsearch retrieval arm disabled" ;;
      *)
        bad "Elasticsearch retrieval arm not healthy (status=$files_status)"
        printf '%s' "$api_health_json" | jq -c '.retrieval_arms.files // {}' 2>/dev/null | sed 's/^/       /' || true
        ;;
    esac
  else
    warn "jq unavailable; Elasticsearch arm status could not be evaluated from RAG API /health"
  fi
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
