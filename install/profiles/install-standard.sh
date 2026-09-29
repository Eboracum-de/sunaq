#!/usr/bin/env bash
set -euo pipefail

# Bootstrap a blank Linux VM into a usable SunaQ / Eboracum Research Gateway node.
# 0.8.6-rc1.2 standard profile implementation.

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFIX="/opt/sunaq"
LEGACY_PREFIX="/opt/nextcloud-rag"
PREFIX_EXPLICIT=0
RAG_USER="rag"
RAG_GROUP="rag"
NEXTCLOUD_URL=""
CA_CERTIFICATES=()
ELASTICSEARCH_URL=""
ELASTICSEARCH_INDEX=""
INSTALL_SYSTEM_PACKAGES=1
WITH_QDRANT=0
WITH_NEO4J=0
WITH_OPENWEBUI=0
OPENWEBUI_EXPLICIT=0
WITH_PLAYWRIGHT=0
PLAYWRIGHT_EXPLICIT=0
OPENWEBUI_FROM_STATE=0
WITH_PROXY=1
PROXY_EXPLICIT=0
PROXY_FROM_STATE=0
PROXY_HTTP_PORT=80
PROXY_HTTPS_PORT=443
PROXY_HTTP_PORT_EXPLICIT=0
PROXY_HTTPS_PORT_EXPLICIT=0
PROXY_BASIC_AUTH=1
MULTI_USER=1
ACL_OFF=0
ACL_MODE_EXPLICIT=0
WITH_SYSTEMD=0
DOWNLOAD_RERANKER=0
ASSUME_YES=0
PLAN_ONLY=0
X509_STRICT=0
X509_STRICT_EXPLICIT=0
PRESET_FILE=""
PRESET_NAME=""

usage() {
  cat <<USAGE
Usage: $0 [options]

Options:
  --prefix PATH             Install directory (default: /opt/sunaq)
  --user USER               Service user (default: rag)
  --nextcloud-url URL       Override Nextcloud base URL in config.yaml
  --ca-certificate FILE     Trust one private CA certificate for Nextcloud; repeatable
  --elasticsearch-url URL   Override Elasticsearch URL in config.yaml
  --elasticsearch-index ID  Override Elasticsearch index in config.yaml
  --preset core|workgroup   Apply a shipped capability preset
  --preset-file FILE        Apply a safe YAML capability overlay
  --skip-system-packages    Do not install OS packages/Docker
  --with-qdrant             Install/start a local Qdrant container
  --with-neo4j              Install/start a local Neo4j container
  --core                    Local data stack: Qdrant + Neo4j
  --with-openwebui          Start/retain bundled OpenWebUI
  --no-openwebui            Disable/remove bundled OpenWebUI on this host
  --with-playwright         Enable/build/start Playwright Web-archive renderer
  --no-playwright           Disable/remove Playwright Web-archive renderer
  --with-proxy              Start/retain bundled nginx reverse proxy
  --no-proxy                Disable/remove bundled nginx reverse proxy
  --proxy-http-port PORT     nginx HTTP listen port (default: 80)
  --proxy-https-port PORT    nginx HTTPS listen port (default: 443)
  --no-proxy-basic-auth     Disable nginx Basic Auth gate; rate limits remain active
  --multi-user             Explicitly select the default multi-user credential_store mode
  --single-user            Explicit one-user mode; live ACL remains enabled
  --acl-off                Diagnostic only: disable live document ACL explicitly
  --full                   Full bundled stack: Qdrant + Neo4j + OpenWebUI
  --with-systemd           Also install/enable optional systemd services
  --no-systemd             Legacy alias; keep systemd integration disabled
  --with-reranker-download Pre-download local Hugging Face reranker model (opt-in)
  --no-reranker-download   Keep reranker model download disabled (default)
  --x509-strict            Enable Python/OpenSSL VERIFY_X509_STRICT (default: off)
  --no-x509-strict         Compatibility alias; keep strict mode disabled
  --plan                    Show the installation plan and exit without changes
  -y, --yes                Assume yes; run non-interactively without confirmation
  -h, --help               Show this help

Examples:
  sudo ./install/install.sh                 # minimal: proxy + middleware, external Nextcloud/Ollama
  sudo ./install/install.sh --core
  sudo ./install/install.sh --full
  sudo ./install/install.sh --single-user
  sudo ./install/install.sh --plan --full
  sudo ./install/install.sh -y --with-qdrant --with-neo4j --with-openwebui
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; PREFIX_EXPLICIT=1; shift 2 ;;
    --user) RAG_USER="$2"; RAG_GROUP="$2"; shift 2 ;;
    --nextcloud-url) [[ $# -ge 2 ]] || { echo "--nextcloud-url requires a URL" >&2; exit 2; }; NEXTCLOUD_URL="$2"; shift 2 ;;
    --ca-certificate) [[ $# -ge 2 ]] || { echo "--ca-certificate requires a file" >&2; exit 2; }; CA_CERTIFICATES+=("$2"); shift 2 ;;
    --elasticsearch-url) [[ $# -ge 2 ]] || { echo "--elasticsearch-url requires a URL" >&2; exit 2; }; ELASTICSEARCH_URL="$2"; shift 2 ;;
    --elasticsearch-index) [[ $# -ge 2 ]] || { echo "--elasticsearch-index requires an index" >&2; exit 2; }; ELASTICSEARCH_INDEX="$2"; shift 2 ;;
    --preset)
      [[ $# -ge 2 ]] || { echo "--preset requires core or workgroup" >&2; exit 2; }
      case "$2" in core|workgroup) PRESET_NAME="$2"; PRESET_FILE="$SOURCE_DIR/install/presets/$2.yaml" ;; *) echo "Unknown preset: $2" >&2; exit 2 ;; esac
      shift 2 ;;
    --preset-file)
      [[ $# -ge 2 ]] || { echo "--preset-file requires a readable YAML file" >&2; exit 2; }
      PRESET_NAME="custom"; PRESET_FILE="$2"; shift 2 ;;
    --skip-system-packages) INSTALL_SYSTEM_PACKAGES=0; shift ;;
    --with-qdrant) WITH_QDRANT=1; shift ;;
    --with-neo4j) WITH_NEO4J=1; shift ;;
    --core) WITH_QDRANT=1; WITH_NEO4J=1; shift ;;
    --with-ollama|--ollama-gpu|--ollama-models) echo "Bundled Ollama is not part of this release; install/manage Ollama or another compatible backend separately and configure its URL in config.yaml/provider.env." >&2; exit 2 ;;
    --with-openwebui) WITH_OPENWEBUI=1; OPENWEBUI_EXPLICIT=1; shift ;;
    --no-openwebui) WITH_OPENWEBUI=0; OPENWEBUI_EXPLICIT=1; shift ;;
    --with-playwright) WITH_PLAYWRIGHT=1; PLAYWRIGHT_EXPLICIT=1; shift ;;
    --no-playwright) WITH_PLAYWRIGHT=0; PLAYWRIGHT_EXPLICIT=1; shift ;;
    --with-searxng) echo "--with-searxng was removed from the bundled stack; install/configure SearXNG externally and point web.yaml to it." >&2; exit 2 ;;
    --with-proxy) WITH_PROXY=1; PROXY_EXPLICIT=1; shift ;;
    --no-proxy) WITH_PROXY=0; PROXY_EXPLICIT=1; shift ;;
    --proxy-http-port) [[ $# -ge 2 ]] || { echo "--proxy-http-port requires a port" >&2; exit 2; }; PROXY_HTTP_PORT="$2"; PROXY_HTTP_PORT_EXPLICIT=1; shift 2 ;;
    --proxy-https-port) [[ $# -ge 2 ]] || { echo "--proxy-https-port requires a port" >&2; exit 2; }; PROXY_HTTPS_PORT="$2"; PROXY_HTTPS_PORT_EXPLICIT=1; shift 2 ;;
    --no-proxy-basic-auth) PROXY_BASIC_AUTH=0; shift ;;
    --multi-user) MULTI_USER=1; ACL_OFF=0; ACL_MODE_EXPLICIT=1; shift ;;
    --single-user) MULTI_USER=0; ACL_OFF=0; ACL_MODE_EXPLICIT=1; shift ;;
    --acl-off) MULTI_USER=0; ACL_OFF=1; ACL_MODE_EXPLICIT=1; shift ;;
    --full) WITH_QDRANT=1; WITH_NEO4J=1; WITH_OPENWEBUI=1; shift ;;
    --with-systemd) WITH_SYSTEMD=1; shift ;;
    --no-systemd) WITH_SYSTEMD=0; shift ;;
    --with-reranker-download) DOWNLOAD_RERANKER=1; shift ;;
    --no-reranker-download) DOWNLOAD_RERANKER=0; shift ;;
    --x509-strict) X509_STRICT=1; X509_STRICT_EXPLICIT=1; shift ;;
    --no-x509-strict) X509_STRICT=0; X509_STRICT_EXPLICIT=1; shift ;;
    --plan) PLAN_ONLY=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$PRESET_FILE" && ! -r "$PRESET_FILE" ]]; then
  echo "Preset file is not readable: $PRESET_FILE" >&2
  exit 2
fi
if [[ -n "$PRESET_FILE" ]]; then
  PRESET_FILE="$(cd "$(dirname "$PRESET_FILE")" && pwd)/$(basename "$PRESET_FILE")"
fi

# Fresh installs use /opt/sunaq. Existing 0.8.5 installations remain in place
# unless --prefix is explicitly supplied.
if [[ $PREFIX_EXPLICIT -eq 0 && -d "$LEGACY_PREFIX" ]] && { [[ ! -e "$PREFIX" ]] || [[ -d "$PREFIX" && -z "$(find "$PREFIX" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; }; then
  if [[ -f "$LEGACY_PREFIX/.sunaq-installation" || -f "$LEGACY_PREFIX/.aki-rag-installation" || ( -d "$LEGACY_PREFIX/rag" && -f "$LEGACY_PREFIX/config.yaml" && -d "$LEGACY_PREFIX/install" ) ]]; then
    echo "[INFO] Legacy SunaQ/AKI installation detected at $LEGACY_PREFIX; continuing in place."
    PREFIX="$LEGACY_PREFIX"
  fi
fi

# Validate an existing prefix before reading any state from it. The state file is
# data written by AKI, never shell code: an operator may pass --prefix while
# running this installer as root.
PREFIX_RECOGNIZED_SUNAQ=0
validate_install_prefix() {
  if [[ -e "$PREFIX" && ! -d "$PREFIX" ]]; then
    echo "Install prefix exists but is not a directory: $PREFIX" >&2
    exit 2
  fi
  if [[ -d "$PREFIX" ]] && [[ -n "$(find "$PREFIX" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    local source_is_prefix=0
    local recognized_sunaq=0
    [[ "$(readlink -f "$SOURCE_DIR")" == "$(readlink -f "$PREFIX")" ]] && source_is_prefix=1
    [[ -f "$PREFIX/.sunaq-installation" || -f "$PREFIX/.aki-rag-installation" ]] && recognized_sunaq=1
    if [[ -d "$PREFIX/rag" && -f "$PREFIX/config.yaml" && -d "$PREFIX/install" ]]; then
      recognized_sunaq=1
    fi
    if [[ $source_is_prefix -ne 1 && $recognized_sunaq -ne 1 ]]; then
      echo "Refusing to install into non-empty directory that is not recognized as a SunaQ installation: $PREFIX" >&2
      echo "Choose a dedicated --prefix (recommended: /opt/sunaq). Existing files were not modified." >&2
      exit 2
    fi
    PREFIX_RECOGNIZED_SUNAQ=1
  fi
}

read_state_bool() {
  local key="$1"
  local value="$2"
  case "$value" in
    0|1) printf '%s' "$value" ;;
    *)
      echo "Invalid boolean value in install-state.env for $key: $value" >&2
      exit 2
      ;;
  esac
}

load_install_state() {
  local state_file="$PREFIX/install/install-state.env"
  [[ $PREFIX_RECOGNIZED_SUNAQ -eq 1 && -f "$state_file" ]] || return 0

  local state_local_qdrant=0
  local state_local_neo4j=0
  local state_local_openwebui=0
  local state_local_playwright=0
  local state_local_proxy=1
  local state_proxy_http_port=80
  local state_proxy_https_port=443
  local state_proxy_basic_auth=1
  local key value

  while IFS='=' read -r key value || [[ -n "$key$value" ]]; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      LOCAL_QDRANT) state_local_qdrant="$(read_state_bool "$key" "$value")" ;;
      LOCAL_NEO4J) state_local_neo4j="$(read_state_bool "$key" "$value")" ;;
      LOCAL_OPENWEBUI) state_local_openwebui="$(read_state_bool "$key" "$value")" ;;
      LOCAL_PLAYWRIGHT) state_local_playwright="$(read_state_bool "$key" "$value")" ;;
      LOCAL_PROXY) state_local_proxy="$(read_state_bool "$key" "$value")" ;;
      PROXY_HTTP_PORT) state_proxy_http_port="$value" ;;
      PROXY_HTTPS_PORT) state_proxy_https_port="$value" ;;
      PROXY_BASIC_AUTH_STATE) state_proxy_basic_auth="$(read_state_bool "$key" "$value")" ;;
      DEPLOYMENT_PROFILE)
        case "$value" in standard|super-light) ;; *)
          echo "Invalid DEPLOYMENT_PROFILE in install-state.env: $value" >&2
          exit 2
        esac
        ;;
      *) : ;; # Ignore unknown keys for forward compatibility; never evaluate them.
    esac
  done < "$state_file"

  # Reruns are additive: previously installed local components stay selected
  # unless an explicit removal mechanism is added in a later release.
  [[ $WITH_QDRANT -eq 0 && $state_local_qdrant -eq 1 ]] && WITH_QDRANT=1
  [[ $WITH_NEO4J -eq 0 && $state_local_neo4j -eq 1 ]] && WITH_NEO4J=1
  if [[ $OPENWEBUI_EXPLICIT -eq 0 ]]; then
    WITH_OPENWEBUI=$state_local_openwebui
    [[ $state_local_openwebui -eq 1 ]] && OPENWEBUI_FROM_STATE=1
  fi
  if [[ $PLAYWRIGHT_EXPLICIT -eq 0 ]]; then
    WITH_PLAYWRIGHT=$state_local_playwright
  fi
  if [[ $PROXY_EXPLICIT -eq 0 ]]; then
    WITH_PROXY=$state_local_proxy
    PROXY_FROM_STATE=1
  fi
  [[ $PROXY_HTTP_PORT_EXPLICIT -eq 0 ]] && PROXY_HTTP_PORT=$state_proxy_http_port
  [[ $PROXY_HTTPS_PORT_EXPLICIT -eq 0 ]] && PROXY_HTTPS_PORT=$state_proxy_https_port
  [[ $PROXY_BASIC_AUTH -eq 1 && $state_proxy_basic_auth -eq 0 ]] && PROXY_BASIC_AUTH=0
  return 0
}

validate_install_prefix
load_install_state

validate_port() {
  local name="$1" value="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
    echo "$name must be an integer from 1 to 65535: $value" >&2
    exit 2
  fi
}
validate_port --proxy-http-port "$PROXY_HTTP_PORT"
validate_port --proxy-https-port "$PROXY_HTTPS_PORT"
if [[ "$PROXY_HTTP_PORT" == "$PROXY_HTTPS_PORT" ]]; then
  echo "--proxy-http-port and --proxy-https-port must be different" >&2
  exit 2
fi

for ca_source in "${CA_CERTIFICATES[@]}"; do
  [[ -r "$ca_source" ]] || {
    echo "CA certificate is not readable: $ca_source" >&2
    exit 2
  }
  cert_count="$(grep -c -- '-----BEGIN CERTIFICATE-----' "$ca_source" || true)"
  [[ "$cert_count" -eq 1 ]] || {
    echo "--ca-certificate expects exactly one PEM certificate per file: $ca_source" >&2
    echo "Repeat --ca-certificate for root/intermediate certificates." >&2
    exit 2
  }
  if command -v openssl >/dev/null 2>&1; then
    openssl x509 -in "$ca_source" -noout >/dev/null 2>&1 || {
      echo "Invalid PEM X.509 certificate: $ca_source" >&2
      exit 2
    }
  fi
done

log() { printf '\n==> %s\n' "$*"; }

probe_service_url() {
  local label="$1" url="$2"
  [[ -n "$url" ]] || return 0
  if ! command -v curl >/dev/null 2>&1; then
    echo "[INFO] $label reachability check skipped: curl is not available yet."
    return 0
  fi
  if curl -sS --max-time 5 -o /dev/null "$url"; then
    echo "[CHECK] $label endpoint reachable."
  else
    echo "[WARN] $label endpoint is not reachable with current host curl/TLS trust." >&2
    echo "       Installation will continue; verify the configured URL/service before using SunaQ." >&2
  fi
}

probe_configured_services() {
  probe_service_url "Nextcloud" "$NEXTCLOUD_URL"
  probe_service_url "Elasticsearch" "$ELASTICSEARCH_URL"
}

print_plan() {
  # Conservative component-wise estimate.  Indexed/user data is extra.
  local disk_low=4
  local disk_high=7
  [[ $WITH_QDRANT -eq 1 ]] && { disk_low=$((disk_low + 0)); disk_high=$((disk_high + 1)); }
  [[ $WITH_NEO4J -eq 1 ]] && { disk_low=$((disk_low + 1)); disk_high=$((disk_high + 2)); }
  [[ $WITH_OPENWEBUI -eq 1 ]] && { disk_low=$((disk_low + 6)); disk_high=$((disk_high + 8)); }
  [[ $WITH_PLAYWRIGHT -eq 1 ]] && { disk_low=$((disk_low + 1)); disk_high=$((disk_high + 2)); }
  if [[ $DOWNLOAD_RERANKER -eq 0 ]]; then
    disk_low=$(( disk_low > 2 ? disk_low - 2 : 1 ))
    disk_high=$(( disk_high > 3 ? disk_high - 3 : 2 ))
  fi

  local docker_needed=0
  if [[ $WITH_PROXY -eq 1 || $WITH_QDRANT -eq 1 || $WITH_NEO4J -eq 1 || $WITH_OPENWEBUI -eq 1 || $WITH_PLAYWRIGHT -eq 1 ]]; then
    docker_needed=1
  fi

  cat <<PLAN

SunaQ / Eboracum Research Gateway installation plan
--------------------------------------
Install prefix:          $PREFIX
Service user:            $RAG_USER
System packages:         $([[ $INSTALL_SYSTEM_PACKAGES -eq 1 ]] && echo install/update || echo leave unchanged)
Docker/Compose:          $([[ $docker_needed -eq 1 ]] && echo required || echo not required by selected components)
Reverse proxy/nginx:     $([[ $WITH_PROXY -eq 1 ]] && echo "install/start; HTTPS ${PROXY_HTTPS_PORT} + HTTP redirect on ${PROXY_HTTP_PORT}$([[ $PROXY_FROM_STATE -eq 1 ]] && echo ' (retained from existing install)')" || echo skip)
Proxy Basic Auth gate:    $([[ $WITH_PROXY -eq 1 && $PROXY_BASIC_AUTH -eq 1 ]] && echo enabled || echo disabled)
Qdrant:                  $([[ $WITH_QDRANT -eq 1 ]] && echo install/start || echo disabled/external)
Neo4j:                   $([[ $WITH_NEO4J -eq 1 ]] && echo install/start || echo disabled/external)
Python venv/dependencies: install/update
Reranker model cache:     $([[ $DOWNLOAD_RERANKER -eq 1 ]] && echo "pre-download requested (used only for backend=local)" || echo "skip (default; reranker opt-in)")
LLM/embedding backend:    external/admin-managed (Ollama or compatible service)
Nextcloud URL override:   ${NEXTCLOUD_URL:-<preserve/configure in config.yaml>}
Nextcloud private CA:      ${#CA_CERTIFICATES[@]} certificate(s) supplied on this run
Elasticsearch URL:       ${ELASTICSEARCH_URL:-<preserve/configure in config.yaml>}
Elasticsearch index:     ${ELASTICSEARCH_INDEX:-<preserve/configure in config.yaml>}
Capability preset:       ${PRESET_NAME:-<none; preserve site configuration>}
OpenWebUI:                $([[ $WITH_OPENWEBUI -eq 1 ]] && echo "install/start$([[ $OPENWEBUI_FROM_STATE -eq 1 ]] && echo ' (retained from existing install; use --no-openwebui to disable)')" || echo external/skip)
Playwright renderer:      $([[ $PLAYWRIGHT_EXPLICIT -eq 1 ]] && ([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo enable/build/start || echo disable/remove) || ([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo "retain enabled state" || echo "preserve web.yaml state"))
Web search service:       external/admin-managed (not bundled)
ACL mode:                 $([[ $ACL_OFF -eq 1 ]] && echo ACL-OFF-DIAGNOSTIC || ([[ $MULTI_USER -eq 1 ]] && echo multi-user credential_store || echo single_user-live-ACL))
Systemd units:            $([[ $WITH_SYSTEMD -eq 1 ]] && echo optional install/enable || echo skip \(start scripts are default\))
Python X.509 strict:      $([[ $X509_STRICT -eq 1 ]] && echo enabled || echo disabled-compatibility-mode)
Estimated disk use:       roughly ${disk_low}-${disk_high} GiB before indexed/user data

The default install is intentionally minimal: middleware + nginx only. Nextcloud
and the LLM/embedding backend are expected to be reachable after site configuration.
Use --core for Qdrant + Neo4j or --full for all bundled optional components. Backend containers
bind to 127.0.0.1; nginx is the only externally reachable interface.
A self-signed bootstrap certificate is generated for HTTPS and may be replaced by
the administrator after installation.
PLAN
}
confirm_plan() {
  print_plan
  if [[ $PLAN_ONLY -eq 1 ]]; then
    exit 0
  fi
  if [[ $ASSUME_YES -eq 1 ]]; then
    log "Non-interactive mode enabled (-y/--yes); proceeding"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "Refusing to modify the system without confirmation on non-interactive stdin." >&2
    echo "Review with --plan, then rerun with -y/--yes for unattended installation." >&2
    exit 1
  fi
  printf '\nProceed with this installation? [y/N] '
  local answer
  read -r answer
  case "$answer" in
    y|Y|yes|YES|Yes) ;;
    *) echo "Installation cancelled."; exit 0 ;;
  esac
}

preflight_existing_install() {
  [[ $PREFIX_RECOGNIZED_SUNAQ -eq 1 ]] || return 0

  echo "[INFO] Existing SunaQ installation detected at $PREFIX."

  local running=()
  local name pid pidfile unit
  for name in api provider graph-worker sync-worker mail-worker; do
    pidfile="$PREFIX/run/$name.pid"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        running+=("$name(pid=$pid)")
      fi
    fi
  done

  if command -v systemctl >/dev/null 2>&1; then
    for unit in rag-api.service rag-provider.service rag-graph-worker.service rag-sync-worker.service rag-mail-worker.service; do
      if systemctl is-active --quiet "$unit" 2>/dev/null; then
        running+=("$unit")
      fi
    done
  fi

  local docker_selected=0
  [[ $WITH_PROXY -eq 1 || $WITH_QDRANT -eq 1 || $WITH_NEO4J -eq 1 || $WITH_OPENWEBUI -eq 1 || $WITH_PLAYWRIGHT -eq 1 ]] && docker_selected=1

  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      local existing_compose=()
      if docker compose version >/dev/null 2>&1; then
        existing_compose=(docker compose)
      elif command -v docker-compose >/dev/null 2>&1; then
        existing_compose=(docker-compose)
      fi
      if [[ ${#existing_compose[@]} -gt 0 && -f "$PREFIX/install/docker-compose.yml" ]]; then
        if [[ ! -f "$PREFIX/install/.env" ]]; then
          if [[ -f "$PREFIX/install/.env.example" ]]; then
            echo "[INFO] Recreating missing install/.env from .env.example for rerun preflight."
            local env_group
            if ! env_group="$(id -gn "$RAG_USER" 2>/dev/null)"; then
              echo "[WARN] Cannot resolve primary group for existing service user $RAG_USER; rerun preflight cannot repair install/.env safely." >&2
              exit 2
            fi
            cp "$PREFIX/install/.env.example" "$PREFIX/install/.env"
            chmod 600 "$PREFIX/install/.env"
            chown "$RAG_USER:$env_group" "$PREFIX/install/.env"
          else
            echo "[WARN] Existing Docker Compose installation has no install/.env or .env.example; cannot verify that the stack is stopped." >&2
            exit 2
          fi
        fi
        local docker_running
        if ! docker_running="$(
          cd "$PREFIX/install" &&
          "${existing_compose[@]}" -f docker-compose.yml --env-file .env ps --services --filter status=running 2>/dev/null
        )"; then
          echo "[WARN] Could not inspect the existing Docker Compose stack; cannot verify that it is stopped." >&2
          echo "No installation changes were made." >&2
          exit 2
        fi
        if [[ -n "$docker_running" ]]; then
          while IFS= read -r name; do
            [[ -n "$name" ]] && running+=("docker:$name")
          done <<< "$docker_running"
        fi
      elif [[ $docker_selected -eq 1 ]]; then
        echo "[WARN] Existing installation uses local Docker components, but Docker Compose is unavailable; cannot verify that the stack is stopped." >&2
        echo "No installation changes were made." >&2
        exit 2
      fi
    elif [[ $docker_selected -eq 1 ]]; then
      echo "[WARN] Existing installation uses local Docker components, but the Docker daemon is not reachable; cannot verify that the stack is stopped." >&2
      echo "No installation changes were made." >&2
      exit 2
    fi
  elif [[ $docker_selected -eq 1 ]]; then
    echo "[WARN] Existing installation uses local Docker components, but Docker is unavailable; cannot verify that the stack is stopped." >&2
    echo "No installation changes were made." >&2
    exit 2
  fi

  if [[ ${#running[@]} -gt 0 ]]; then
    echo "[WARN] Existing SunaQ services are running: ${running[*]}" >&2
    echo "Stop the existing middleware and local containers before rerunning the installer; no installation changes were made." >&2
    echo "Typical first step: $PREFIX/stop-all.sh" >&2
    exit 2
  fi

  echo "[INFO] Existing SunaQ installation is stopped; rerun may update it."
}

if [[ $PLAN_ONLY -eq 1 ]]; then
  print_plan
  exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "This bootstrap currently expects root (use sudo)." >&2
  exit 1
fi

preflight_existing_install
confirm_plan

detect_python() {
  local candidate
  PYTHON_BIN=""
  # Prefer the distribution's default python3 so its stdlib/venv packages match.
  for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
      then
        PYTHON_BIN="$(command -v "$candidate")"
        return 0
      fi
    fi
  done
  return 1
}

select_python() {
  if detect_python; then
    return 0
  fi
  echo "Python >=3.10 is required." >&2
  exit 1
}

docker_needed() {
  [[ $WITH_PROXY -eq 1 || $WITH_QDRANT -eq 1 || $WITH_NEO4J -eq 1 || $WITH_OPENWEBUI -eq 1 || $WITH_PLAYWRIGHT -eq 1 ]]
}

install_system_packages() {
  log "Installing system packages"
  local have_python=0
  if detect_python; then
    have_python=1
    log "Keeping existing Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl jq git openssl python3-venv
    [[ $have_python -eq 0 ]] && DEBIAN_FRONTEND=noninteractive apt-get install -y python3
    if docker_needed; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
      apt-get install -y docker-compose-v2 2>/dev/null \
        || apt-get install -y docker-compose-plugin 2>/dev/null \
        || apt-get install -y docker-compose
    fi
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive refresh
    zypper --non-interactive install ca-certificates curl jq git openssl
    [[ $have_python -eq 0 ]] && zypper --non-interactive install python3
    if docker_needed; then
      zypper --non-interactive install docker docker-compose
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl jq git openssl
    [[ $have_python -eq 0 ]] && dnf install -y python3
    if docker_needed; then
      dnf install -y docker docker-compose-plugin
    fi
  else
    echo "Unsupported package manager. Install Python >=3.10 and required local-service dependencies first." >&2
    exit 1
  fi
  if docker_needed; then
    systemctl enable --now docker
  fi
}
ensure_venv_support() {
  # Verify the exact selected interpreter, not merely the presence of a distro
  # package. This catches systems where Python is present but venv/ensurepip is
  # split out by the distribution.
  local probe
  probe="$(mktemp -d)"
  if "$PYTHON_BIN" -m venv "$probe/venv" >/dev/null 2>&1; then
    rm -rf "$probe"
    return 0
  fi
  rm -rf "$probe"

  log "Python venv support is missing; installing the distro helper package"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip
  elif command -v zypper >/dev/null 2>&1; then
    local py_suffix
    py_suffix="$($PYTHON_BIN - <<'PY'
import sys
print(f"{sys.version_info.major}{sys.version_info.minor}")
PY
)"
    # openSUSE commonly uses versioned pip packages (e.g. python313-pip).
    # Try that first, then the generic capability/package name.
    zypper --non-interactive install "python${py_suffix}-pip" \
      || zypper --non-interactive install python3-pip \
      || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-pip
  fi

  probe="$(mktemp -d)"
  if ! "$PYTHON_BIN" -m venv "$probe/venv" >/dev/null 2>&1; then
    rm -rf "$probe"
    echo "Selected Python ($PYTHON_BIN) cannot create a venv with pip." >&2
    echo "Install venv/ensurepip support for this exact Python version and retry." >&2
    exit 1
  fi
  rm -rf "$probe"
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "Docker Compose not found." >&2
    exit 1
  fi
}

random_secret() {
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

if [[ $INSTALL_SYSTEM_PACKAGES -eq 1 ]]; then
  install_system_packages
fi
probe_configured_services
select_python
ensure_venv_support
log "Using Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

if ! id "$RAG_USER" >/dev/null 2>&1; then
  log "Creating service user $RAG_USER"
  useradd --system --create-home --home-dir "/var/lib/${RAG_USER}" --shell /bin/bash "$RAG_USER"
fi
RAG_GROUP="$(id -gn "$RAG_USER")"
RAG_HOME="$(getent passwd "$RAG_USER" | cut -d: -f6)"

run_as_rag() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$RAG_USER" -- env HOME="$RAG_HOME" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$RAG_USER" -H "$@"
  else
    echo "Neither runuser nor sudo is available to execute commands as $RAG_USER." >&2
    exit 1
  fi
}

log "Installing source tree into $PREFIX"
mkdir -p "$PREFIX"
FRESH_CONFIG=0
[[ ! -e "$PREFIX/config.yaml" ]] && FRESH_CONFIG=1
# install/.env is admin-owned site state (image pins, local-service secrets).
# Preserve it while refreshing the shipped install templates.
INSTALL_ENV_BACKUP=""
INSTALL_TLS_BACKUP=""
if [[ -f "$PREFIX/install/.env" ]]; then
  INSTALL_ENV_BACKUP="$(mktemp)"
  cp -a "$PREFIX/install/.env" "$INSTALL_ENV_BACKUP"
fi
# TLS material is administrator-owned site state as well. Preserve a replaced
# certificate/key across installer reruns.
if [[ -d "$PREFIX/install/nginx/tls" ]]; then
  INSTALL_TLS_BACKUP="$(mktemp -d)"
  cp -a "$PREFIX/install/nginx/tls/." "$INSTALL_TLS_BACKUP/" 2>/dev/null || true
fi
# Preserve local runtime/config files on reruns; copy source files around them.
for item in rag prompts ontology install docs clients requirements.txt versions.lock.yaml pyproject.toml reset-rag.sh start-api.sh start-openwebui-provider.sh start-graph-worker.sh start-sync-worker.sh start-mail-worker.sh start-all.sh stop-all.sh status.sh provider.env.example README.md CHANGELOG.md SECURITY.md; do
  rm -rf "$PREFIX/$item"
  cp -a "$SOURCE_DIR/$item" "$PREFIX/$item"
done
# SunaQ model packages are administrator-owned configuration after first
# installation. Seed the directory once, then add only newly shipped package
# directories on upgrades. Existing packages are never overwritten, preserving
# local profile/LLM/prompt tuning while allowing new bundled profiles to appear.
if [[ ! -d "$PREFIX/models" ]]; then
  cp -a "$SOURCE_DIR/models" "$PREFIX/models"
else
  for source_model in "$SOURCE_DIR"/models/*; do
    [[ -d "$source_model" ]] || continue
    model_name="$(basename "$source_model")"
    if [[ ! -e "$PREFIX/models/$model_name" ]]; then
      cp -a "$source_model" "$PREFIX/models/$model_name"
      log "Added new SunaQ model package: $model_name"
    fi
  done
fi
chmod 0755 "$PREFIX/install/maintenance-mode.sh"
if [[ -n "$INSTALL_ENV_BACKUP" ]]; then
  mv "$INSTALL_ENV_BACKUP" "$PREFIX/install/.env"
fi
if [[ -n "$INSTALL_TLS_BACKUP" ]]; then
  mkdir -p "$PREFIX/install/nginx/tls"
  cp -a "$INSTALL_TLS_BACKUP/." "$PREFIX/install/nginx/tls/" 2>/dev/null || true
  rm -rf "$INSTALL_TLS_BACKUP"
fi
for item in config.yaml web.yaml; do
  if [[ ! -e "$PREFIX/$item" ]]; then
    cp -a "$SOURCE_DIR/$item" "$PREFIX/$item"
  fi
done
if [[ ! -e "$PREFIX/provider.env" ]]; then
  cp -a "$SOURCE_DIR/provider.env.example" "$PREFIX/provider.env"
fi
cat > "$PREFIX/.sunaq-installation" <<MARKER
SUNAQ_INSTALLATION=1
DEPLOYMENT_PROFILE=standard
DEPLOYMENT_MODE=native
MARKER
chmod 0644 "$PREFIX/.sunaq-installation"
mkdir -p "$PREFIX/runtime" "$PREFIX/runtime/ca"
chmod 700 "$PREFIX/runtime"
NEXTCLOUD_CA_FILE=""
if [[ ${#CA_CERTIFICATES[@]} -gt 0 ]]; then
  log "Installing private CA bundle for Nextcloud TLS"
  rm -f "$PREFIX/runtime/ca"/nextcloud-installer-*.crt
  ca_index=0
  for ca_source in "${CA_CERTIFICATES[@]}"; do
    ca_index=$((ca_index + 1))
    printf -v ca_name 'nextcloud-installer-%02d.crt' "$ca_index"
    cp "$ca_source" "$PREFIX/runtime/ca/$ca_name"
    chmod 0644 "$PREFIX/runtime/ca/$ca_name"
  done
  NEXTCLOUD_CA_FILE="$PREFIX/runtime/ca/nextcloud-ca-bundle.pem"
  cat "$PREFIX/runtime/ca"/nextcloud-installer-*.crt > "$NEXTCLOUD_CA_FILE"
  chmod 0644 "$NEXTCLOUD_CA_FILE"
fi
chown -R "$RAG_USER:$RAG_GROUP" "$PREFIX"

log "Creating Python virtual environment"
if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
  run_as_rag "$PYTHON_BIN" -m venv "$PREFIX/.venv"
fi
run_as_rag "$PREFIX/.venv/bin/python" -m pip install --upgrade pip wheel
# Reranker is CPU by design in the canonical config; a CPU wheel is much more
# portable than implicitly selecting a CUDA build on arbitrary hosts.
run_as_rag "$PREFIX/.venv/bin/python" -m pip install \
  --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.13.0+cpu'
run_as_rag "$PREFIX/.venv/bin/python" -m pip install -r "$PREFIX/requirements.txt"

CONFIG_TX_DIR="$(mktemp -d "$PREFIX/runtime/.installer-config.XXXXXX")"
cp -p "$PREFIX/config.yaml" "$CONFIG_TX_DIR/config.yaml"
if [[ -f "$PREFIX/web.yaml" ]]; then
  cp -p "$PREFIX/web.yaml" "$CONFIG_TX_DIR/web.yaml"
else
  : > "$CONFIG_TX_DIR/web.absent"
fi
CONFIG_TX_ACTIVE=1
rollback_config_transaction() {
  local rc=$?
  set +e
  if [[ "${CONFIG_TX_ACTIVE:-0}" -eq 1 && -d "${CONFIG_TX_DIR:-}" ]]; then
    cp -p "$CONFIG_TX_DIR/config.yaml" "$PREFIX/config.yaml"
    if [[ -f "$CONFIG_TX_DIR/web.yaml" ]]; then
      cp -p "$CONFIG_TX_DIR/web.yaml" "$PREFIX/web.yaml"
    elif [[ -f "$CONFIG_TX_DIR/web.absent" ]]; then
      rm -f "$PREFIX/web.yaml"
    fi
    echo "Installer configuration validation failed; previous config.yaml/web.yaml restored." >&2
  fi
  rm -rf "${CONFIG_TX_DIR:-}"
  exit "$rc"
}
trap rollback_config_transaction EXIT

if [[ -n "$PRESET_FILE" ]]; then
  log "Applying capability preset: ${PRESET_NAME:-custom}"
  PRESET_STAGE="$PREFIX/runtime/install-preset.yaml"
  cp "$PRESET_FILE" "$PRESET_STAGE"
  chmod 0640 "$PRESET_STAGE"
  chown "$RAG_USER:$RAG_GROUP" "$PRESET_STAGE"
  run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m rag.config_preset \
    --config "$PREFIX/config.yaml" \
    --preset "$PRESET_STAGE"
  rm -f "$PRESET_STAGE"
fi

log "Validating pinned ML runtime"
run_as_rag "$PREFIX/.venv/bin/python" - <<'PYML'
import torch
if torch.__version__ != "2.13.0+cpu":
    raise SystemExit(f"unexpected torch version: {torch.__version__}")
import torch.export
from transformers import AutoModelForSequenceClassification
print("ML runtime OK: torch", torch.__version__)
PYML

NEO4J_PASSWORD="$(random_secret)"
ADMIN_USER="admin"
ADMIN_PASSWORD="$(random_secret)"
PROVIDER_API_KEY="$(random_secret)"
RAG_INTERNAL_API_KEY="$(random_secret)"
RAG_PROVIDER_INTERNAL_KEY="$(random_secret)"

if [[ ! -f "$PREFIX/runtime.env" ]]; then
  log "Creating runtime.env"
  cat > "$PREFIX/runtime.env" <<RUNTIME
NEO4J_PASSWORD=$NEO4J_PASSWORD
RAG_ADMIN_USER=admin
RAG_ADMIN_PASSWORD=$ADMIN_PASSWORD
PROVIDER_API_KEY=$PROVIDER_API_KEY
RAG_INTERNAL_API_KEY=$RAG_INTERNAL_API_KEY
RAG_PROVIDER_INTERNAL_KEY=$RAG_PROVIDER_INTERNAL_KEY
RAG_MAINTENANCE_MODE=true
LLM_API_KEY=
EMBEDDING_API_KEY=
GRAPH_ENTITY_API_KEY=
GRAPH_RELATION_API_KEY=
WEB_SEARCH_API_KEY=
WEB_LLM_API_KEY=
NEXTCLOUD_USERNAME=
NEXTCLOUD_APP_PASSWORD=
ELASTICSEARCH_PASSWORD=
RAG_CREDENTIAL_MASTER_KEY_FILE=$PREFIX/runtime/credential-master.key
RAG_CREDENTIAL_ENCRYPTION=required
RUNTIME
  chmod 600 "$PREFIX/runtime.env"
  chown "$RAG_USER:$RAG_GROUP" "$PREFIX/runtime.env"
else
  # Reuse existing secrets on idempotent reruns.
  NEO4J_PASSWORD="$(grep '^NEO4J_PASSWORD=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
  ADMIN_USER="$(grep '^RAG_ADMIN_USER=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
  ADMIN_PASSWORD="$(grep '^RAG_ADMIN_PASSWORD=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
  PROVIDER_API_KEY="$(grep '^PROVIDER_API_KEY=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
  RAG_INTERNAL_API_KEY="$(grep '^RAG_INTERNAL_API_KEY=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
  RAG_PROVIDER_INTERNAL_KEY="$(grep '^RAG_PROVIDER_INTERNAL_KEY=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
fi

ensure_runtime_key() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$PREFIX/runtime.env" 2>/dev/null; then
    local current
    current="$(grep "^${key}=" "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
    [[ -n "$current" && "$current" != "replace-me" ]] || sed -i "s|^${key}=.*|${key}=${value}|" "$PREFIX/runtime.env"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$PREFIX/runtime.env"
  fi
}
[[ -n "$NEO4J_PASSWORD" ]] || NEO4J_PASSWORD="$(random_secret)"
[[ -n "$ADMIN_USER" ]] || ADMIN_USER="admin"
[[ -n "$ADMIN_PASSWORD" ]] || ADMIN_PASSWORD="$(random_secret)"
[[ -n "$PROVIDER_API_KEY" ]] || PROVIDER_API_KEY="$(random_secret)"
[[ -n "$RAG_INTERNAL_API_KEY" && "$RAG_INTERNAL_API_KEY" != "replace-me" ]] || RAG_INTERNAL_API_KEY="$(random_secret)"
[[ -n "$RAG_PROVIDER_INTERNAL_KEY" && "$RAG_PROVIDER_INTERNAL_KEY" != "replace-me" ]] || RAG_PROVIDER_INTERNAL_KEY="$(random_secret)"
ensure_runtime_key NEO4J_PASSWORD "$NEO4J_PASSWORD"
ensure_runtime_key RAG_ADMIN_USER "$ADMIN_USER"
ensure_runtime_key RAG_ADMIN_PASSWORD "$ADMIN_PASSWORD"
ensure_runtime_key PROVIDER_API_KEY "$PROVIDER_API_KEY"
ensure_runtime_key RAG_INTERNAL_API_KEY "$RAG_INTERNAL_API_KEY"
ensure_runtime_key RAG_PROVIDER_INTERNAL_KEY "$RAG_PROVIDER_INTERNAL_KEY"
ensure_runtime_key ELASTICSEARCH_PASSWORD ""
if grep -q '^RAG_MAINTENANCE_MODE=' "$PREFIX/runtime.env" 2>/dev/null; then
  sed -i 's/^RAG_MAINTENANCE_MODE=.*/RAG_MAINTENANCE_MODE=true/' "$PREFIX/runtime.env"
else
  printf '\nRAG_MAINTENANCE_MODE=true\n' >> "$PREFIX/runtime.env"
fi
chmod 600 "$PREFIX/runtime.env"
chown "$RAG_USER:$RAG_GROUP" "$PREFIX/runtime.env"

# Stage-1 encrypted credential store.  The key is outside SQLite and is managed
# by root while remaining readable by the service group. Existing installations
# are migrated in-place below before normal services start.
CREDENTIAL_MASTER_KEY_FILE="$(grep '^RAG_CREDENTIAL_MASTER_KEY_FILE=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
[[ -n "$CREDENTIAL_MASTER_KEY_FILE" ]] || CREDENTIAL_MASTER_KEY_FILE="$PREFIX/runtime/credential-master.key"
CREDENTIAL_ENCRYPTION="$(grep '^RAG_CREDENTIAL_ENCRYPTION=' "$PREFIX/runtime.env" | head -1 | cut -d= -f2- || true)"
[[ -n "$CREDENTIAL_ENCRYPTION" ]] || CREDENTIAL_ENCRYPTION="required"
ensure_runtime_key RAG_CREDENTIAL_MASTER_KEY_FILE "$CREDENTIAL_MASTER_KEY_FILE"
ensure_runtime_key RAG_CREDENTIAL_ENCRYPTION "$CREDENTIAL_ENCRYPTION"
export RAG_CREDENTIAL_MASTER_KEY_FILE="$CREDENTIAL_MASTER_KEY_FILE"
export RAG_CREDENTIAL_ENCRYPTION="$CREDENTIAL_ENCRYPTION"
if [[ ! -f "$CREDENTIAL_MASTER_KEY_FILE" ]]; then
  log "Creating encrypted credential-store master key"
  env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m rag.secret_admin init-key --path "$CREDENTIAL_MASTER_KEY_FILE" --group "$RAG_GROUP"
fi
chmod 640 "$CREDENTIAL_MASTER_KEY_FILE"
chown root:"$RAG_GROUP" "$CREDENTIAL_MASTER_KEY_FILE"

# The Bearer authenticates a specific trusted frontend client. The
# previous release used one shared provider key for every frontend, so on first
# migration to the client registry we rotate that key to invalidate copies.
PROVIDER_CLIENT_ID="default-client"
PROVIDER_CLIENT_NAME="Default trusted frontend"
if [[ $WITH_OPENWEBUI -eq 1 ]]; then
  PROVIDER_CLIENT_ID="openwebui-local"
  PROVIDER_CLIENT_NAME="Local OpenWebUI"
fi
CLIENT_AUTH_STATE="$(run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" - "$PREFIX/runtime/users.sqlite" "$PROVIDER_API_KEY" "$PROVIDER_CLIENT_ID" <<'PYCLIENTSTATE'
import sys
from rag.credential_store import CredentialStore
store = CredentialStore(sys.argv[1])
client = store.authenticate_client(sys.argv[2], touch=False) if sys.argv[2] else None
print(store.client_count())
print(client.client_id if client else "")
PYCLIENTSTATE
)"
CLIENT_COUNT="$(printf '%s\n' "$CLIENT_AUTH_STATE" | sed -n '1p')"
CURRENT_CLIENT_ID="$(printf '%s\n' "$CLIENT_AUTH_STATE" | sed -n '2p')"
if [[ "$CLIENT_COUNT" == "0" || "$CURRENT_CLIENT_ID" != "$PROVIDER_CLIENT_ID" ]]; then
  PROVIDER_API_KEY="$(random_secret)"
  sed -i "s|^PROVIDER_API_KEY=.*|PROVIDER_API_KEY=$PROVIDER_API_KEY|" "$PREFIX/runtime.env"
  log "Registering trusted provider client: $PROVIDER_CLIENT_ID (rotated client key)"
  run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" - "$PREFIX/runtime/users.sqlite" "$PROVIDER_CLIENT_ID" "$PROVIDER_CLIENT_NAME" "$PROVIDER_API_KEY" <<'PYCLIENT'
import sys
from rag.credential_store import CredentialStore
store = CredentialStore(sys.argv[1])
store.register_client(sys.argv[2], sys.argv[4], name=sys.argv[3], replace=True)
changed = store.migrate_legacy_identities(sys.argv[2])
print(f"trusted client registered; legacy bindings scoped={changed}")
PYCLIENT
else
  # Still run the one-shot identity migration in case a client was provisioned
  # manually before upgrading the credential-store schema.
  run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" - "$PREFIX/runtime/users.sqlite" "$PROVIDER_CLIENT_ID" <<'PYCLIENTMIG'
import sys
from rag.credential_store import CredentialStore
changed = CredentialStore(sys.argv[1]).migrate_legacy_identities(sys.argv[2])
if changed:
    print(f"legacy bindings scoped={changed}")
PYCLIENTMIG
fi

log "Migrating reversible credentials to encrypted storage"
run_as_rag env PYTHONPATH="$PREFIX" RAG_CREDENTIAL_MASTER_KEY_FILE="$CREDENTIAL_MASTER_KEY_FILE" RAG_CREDENTIAL_ENCRYPTION="$CREDENTIAL_ENCRYPTION" \
  "$PREFIX/.venv/bin/python" -m rag.secret_admin --store "$PREFIX/runtime/users.sqlite" migrate
run_as_rag env PYTHONPATH="$PREFIX" RAG_CREDENTIAL_MASTER_KEY_FILE="$CREDENTIAL_MASTER_KEY_FILE" RAG_CREDENTIAL_ENCRYPTION="$CREDENTIAL_ENCRYPTION" \
  "$PREFIX/.venv/bin/python" -m rag.secret_admin --store "$PREFIX/runtime/users.sqlite" verify
chmod 600 "$PREFIX/runtime.env" "$PREFIX/runtime/users.sqlite"
chown "$RAG_USER:$RAG_GROUP" "$PREFIX/runtime.env" "$PREFIX/runtime/users.sqlite"

log "Synchronizing file-backed service secrets"
bash "$PREFIX/install/sync-container-secrets.sh" "$PREFIX"

ensure_env_key() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local current
    current="$(grep "^${key}=" "$file" | head -1 | cut -d= -f2- || true)"
    if [[ -z "$current" || "$current" == "replace-me" ]]; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    fi
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [[ ! -f "$PREFIX/install/.env" ]]; then
  cp "$PREFIX/install/.env.example" "$PREFIX/install/.env"
fi
# Upgrade only our former moving defaults. Explicit admin-selected image tags
# remain untouched.
upgrade_image_pin() {
  local key="$1" old="$2" new="$3"
  if grep -qx "${key}=${old}" "$PREFIX/install/.env" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${new}|" "$PREFIX/install/.env"
  fi
}
upgrade_image_pin NGINX_IMAGE 'nginx:alpine' 'nginx:1.30.4-alpine3.24'
upgrade_image_pin NGINX_IMAGE 'nginx:1.30.4-alpine3.24' 'nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
upgrade_image_pin QDRANT_IMAGE 'qdrant/qdrant:latest' 'qdrant/qdrant:v1.19.0@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc'
upgrade_image_pin QDRANT_IMAGE 'qdrant/qdrant:v1.19.0' 'qdrant/qdrant:v1.19.0@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc'
upgrade_image_pin NEO4J_IMAGE 'neo4j:5-community' 'neo4j:5.26.29-community@sha256:d9dd3dc7d1c78fa959191ff02dbdcbefadceaf83eee23428fb92a58cac8ad3fe'
upgrade_image_pin NEO4J_IMAGE 'neo4j:5.26.30-community' 'neo4j:5.26.29-community@sha256:d9dd3dc7d1c78fa959191ff02dbdcbefadceaf83eee23428fb92a58cac8ad3fe'
upgrade_image_pin NEO4J_IMAGE 'neo4j:5.26.29-community' 'neo4j:5.26.29-community@sha256:d9dd3dc7d1c78fa959191ff02dbdcbefadceaf83eee23428fb92a58cac8ad3fe'
upgrade_image_pin OPENWEBUI_IMAGE 'ghcr.io/open-webui/open-webui:v0.11.1' 'ghcr.io/open-webui/open-webui:v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e'
upgrade_image_pin OPENWEBUI_IMAGE 'ghcr.io/open-webui/open-webui:v0.11.0' 'ghcr.io/open-webui/open-webui:v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e'
upgrade_image_pin OPENWEBUI_IMAGE 'ghcr.io/open-webui/open-webui:v0.11.4-slim' 'ghcr.io/open-webui/open-webui:v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e'
# Neo4j's password is shared with runtime.env and must stay consistent.
if grep -q '^NEO4J_PASSWORD=' "$PREFIX/install/.env"; then
  sed -i "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=$NEO4J_PASSWORD|" "$PREFIX/install/.env"
else
  printf '\nNEO4J_PASSWORD=%s\n' "$NEO4J_PASSWORD" >> "$PREFIX/install/.env"
fi
# Seed a fresh OpenWebUI connection without exposing the rest of runtime.env.
# Keep this synchronized with the canonical PROVIDER_API_KEY in runtime.env.
if grep -q '^OPENWEBUI_PROVIDER_API_KEY=' "$PREFIX/install/.env"; then
  sed -i "s|^OPENWEBUI_PROVIDER_API_KEY=.*|OPENWEBUI_PROVIDER_API_KEY=$PROVIDER_API_KEY|" "$PREFIX/install/.env"
else
  printf '\nOPENWEBUI_PROVIDER_API_KEY=%s\n' "$PROVIDER_API_KEY" >> "$PREFIX/install/.env"
fi
chmod 600 "$PREFIX/install/.env"
chown "$RAG_USER:$RAG_GROUP" "$PREFIX/install/.env"

# First-install component capability defaults. Existing site configuration is
# preserved on reruns, but a fresh VM should not probe services the admin did
# not select.
run_as_rag "$PREFIX/.venv/bin/python" - "$PREFIX/config.yaml" "$PREFIX/web.yaml" "$WITH_QDRANT" "$WITH_NEO4J" "$MULTI_USER" "$ACL_OFF" "$ACL_MODE_EXPLICIT" "$FRESH_CONFIG" "$X509_STRICT" "$NEXTCLOUD_URL" "$NEXTCLOUD_CA_FILE" "$ELASTICSEARCH_URL" "$ELASTICSEARCH_INDEX" "$PLAYWRIGHT_EXPLICIT" "$WITH_PLAYWRIGHT" "$X509_STRICT_EXPLICIT" "$PRESET_NAME" <<'PYCFG'
import sys, yaml
from pathlib import Path
config_path, web_path = Path(sys.argv[1]), Path(sys.argv[2])
with config_path.open(encoding='utf-8') as f: cfg=yaml.safe_load(f) or {}
fresh = bool(int(sys.argv[8]))
x509_strict = bool(int(sys.argv[9]))
nextcloud_url, nextcloud_ca_file, elasticsearch_url, elasticsearch_index = sys.argv[10:14]
playwright_explicit = bool(int(sys.argv[14]))
playwright_enabled = bool(int(sys.argv[15]))
x509_explicit = bool(int(sys.argv[16]))
preset_name = str(sys.argv[17] or "")
if x509_explicit or (fresh and not preset_name):
    cfg.setdefault('tls', {})['x509_strict'] = x509_strict
is_src = str((cfg.get('architecture') or {}).get('tier') or 'erg').strip().casefold() == 'src'
if nextcloud_url:
    cfg.setdefault('nextcloud', {})['base_url'] = nextcloud_url
if nextcloud_ca_file:
    nextcloud = cfg.setdefault('nextcloud', {})
    nextcloud['verify_tls'] = True
    nextcloud['ca_file'] = nextcloud_ca_file
if elasticsearch_url:
    cfg.setdefault('elasticsearch', {})['url'] = elasticsearch_url
if elasticsearch_index:
    cfg.setdefault('elasticsearch', {})['index'] = elasticsearch_index
if fresh or bool(int(sys.argv[3])):
    cfg.setdefault('qdrant', {})['enabled'] = bool(int(sys.argv[3]))
if fresh or bool(int(sys.argv[4])):
    cfg.setdefault('neo4j', {})['enabled'] = bool(int(sys.argv[4]))
if fresh and not bool(int(sys.argv[4])):
    cfg.setdefault('graph_retrieval', {})['enabled'] = False
    cfg.setdefault('graph_queue', {})['enabled'] = False
    cfg.setdefault('sync', {}).setdefault('graph_queue', {})['enabled'] = False
elif bool(int(sys.argv[4])):
    if is_src:
        # SRC may keep Neo4j only as a clean seed/alias store. Document graph
        # retrieval and graph-derived persistence remain disabled.
        cfg.setdefault('graph_retrieval', {})['enabled'] = False
        cfg.setdefault('graph_queue', {})['enabled'] = False
        cfg.setdefault('sync', {}).setdefault('graph_queue', {})['enabled'] = False
    else:
        # ERG may use demand-driven document graph evidence while bulk graph
        # extraction during sync stays opt-in.
        cfg.setdefault('graph_retrieval', {})['enabled'] = True
        cfg.setdefault('graph_queue', {})['enabled'] = True
        if fresh:
            cfg.setdefault('sync', {}).setdefault('graph_queue', {})['enabled'] = False
# Safe mode is the fresh-install default. Existing site choices are preserved
# unless the administrator explicitly passes an ACL-mode flag.
if fresh or bool(int(sys.argv[7])):
    acl = cfg.setdefault('acl', {})
    if bool(int(sys.argv[6])):
        acl['enabled'] = False
        acl['identity_mode'] = 'single_user'
    elif bool(int(sys.argv[5])):
        acl['enabled'] = True
        acl['identity_mode'] = 'credential_store'
        acl['credential_store'] = 'runtime/users.sqlite'
        cfg.setdefault('auth', {})['credential_store'] = 'runtime/users.sqlite'
        cfg['auth']['nextcloud_login_flow_enabled'] = True
    else:
        acl['enabled'] = True
        acl['identity_mode'] = 'single_user'
with config_path.open('w', encoding='utf-8') as f: yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
with web_path.open(encoding='utf-8') as f: web=yaml.safe_load(f) or {}
if playwright_explicit:
    web.setdefault('archive', {}).setdefault('renderer', {})['enabled'] = playwright_enabled
with web_path.open('w', encoding='utf-8') as f: yaml.safe_dump(web, f, sort_keys=False, allow_unicode=True)
PYCFG

log "Validating final SRC/ERG architecture configuration"
if ! run_as_rag env PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m rag.config_preset \
  --config "$PREFIX/config.yaml" --validate-only; then
  exit 2
fi
CONFIG_TX_ACTIVE=0
rm -rf "$CONFIG_TX_DIR"
trap - EXIT

# Bootstrap TLS: encrypt external traffic by default even before the site
# administrator installs a trusted certificate. Browsers will warn about this
# self-signed certificate; replacing server.crt/server.key is supported and
# preserved across installer reruns.
TLS_DIR="$PREFIX/install/nginx/tls"
mkdir -p "$TLS_DIR"
if [[ ! -s "$TLS_DIR/server.crt" || ! -s "$TLS_DIR/server.key" ]]; then
  log "Generating self-signed nginx bootstrap certificate"
  CERT_CN="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo nextcloud-rag)"
  [[ -n "$CERT_CN" ]] || CERT_CN="nextcloud-rag"
  SAN="DNS:${CERT_CN},DNS:localhost,IP:127.0.0.1"
  SHORT_HOST="$(hostname 2>/dev/null || true)"
  if [[ -n "$SHORT_HOST" && "$SHORT_HOST" != "$CERT_CN" ]]; then
    SAN="${SAN},DNS:${SHORT_HOST}"
  fi
  for ip in $(hostname -I 2>/dev/null || true); do
    [[ "$ip" == *:* ]] && continue  # keep bootstrap generation IPv4-simple
    SAN="${SAN},IP:${ip}"
  done
  if ! openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
      -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
      -subj "/CN=${CERT_CN}" -addext "subjectAltName=${SAN}" >/dev/null 2>&1; then
    # Compatibility fallback for older OpenSSL builds without req -addext.
    OPENSSL_CFG="$(mktemp)"
    cat > "$OPENSSL_CFG" <<EOFSSL
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = ${CERT_CN}
[v3]
subjectAltName = ${SAN}
EOFSSL
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
      -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
      -config "$OPENSSL_CFG" >/dev/null 2>&1
    rm -f "$OPENSSL_CFG"
  fi
fi
chmod 600 "$TLS_DIR/server.key"
chmod 644 "$TLS_DIR/server.crt"

# nginx Basic auth is deliberately limited to admin/auth/API locations. OpenWebUI
# keeps its own Bearer/session auth; layering Basic on / would break that header.
if [[ -z "$ADMIN_PASSWORD" ]]; then ADMIN_PASSWORD="$(random_secret)"; fi
HTPASS_HASH="$($PYTHON_BIN - "$ADMIN_PASSWORD" <<'PYHT'
import base64, hashlib, sys
print('{SHA}' + base64.b64encode(hashlib.sha1(sys.argv[1].encode()).digest()).decode())
PYHT
)"
printf '%s:%s\n' "${ADMIN_USER:-admin}" "$HTPASS_HASH" > "$PREFIX/install/nginx/htpasswd"
chmod 644 "$PREFIX/install/nginx/htpasswd"
printf 'proxy_set_header X-AKI-Internal-Key "%s";\n' "$RAG_INTERNAL_API_KEY" > "$PREFIX/install/nginx/internal-auth.conf"
chmod 600 "$PREFIX/install/nginx/internal-auth.conf"
if [[ $WITH_OPENWEBUI -eq 1 ]]; then
  cp "$PREFIX/install/nginx/nginx-openwebui.conf" "$PREFIX/install/nginx/generated.conf"
else
  cp "$PREFIX/install/nginx/nginx.conf" "$PREFIX/install/nginx/generated.conf"
fi
sed -i \
  -e "s/listen 80 default_server;/listen ${PROXY_HTTP_PORT} default_server;/" \
  -e "s/listen 443 ssl default_server;/listen ${PROXY_HTTPS_PORT} ssl default_server;/" \
  "$PREFIX/install/nginx/generated.conf"
if [[ "$PROXY_HTTPS_PORT" != "443" ]]; then
  sed -i 's|return 308 https://$host$request_uri;|return 308 https://$host:'"${PROXY_HTTPS_PORT}"'$request_uri;|' \
    "$PREFIX/install/nginx/generated.conf"
fi
chmod 644 "$PREFIX/install/nginx/generated.conf"
chmod 755 "$PREFIX" "$PREFIX/install" "$PREFIX/install/nginx"
if [[ $PROXY_BASIC_AUTH -eq 0 ]]; then
  sed -i '/^[[:space:]]*auth_basic /d; /^[[:space:]]*auth_basic_user_file /d' "$PREFIX/install/nginx/generated.conf"
fi

PLAYWRIGHT_ENABLED="$(
  run_as_rag "$PREFIX/.venv/bin/python" - "$PREFIX/web.yaml" <<'PYRENDER'
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
with path.open(encoding="utf-8") as handle:
    cfg = yaml.safe_load(handle) or {}
value = (((cfg.get("archive") or {}).get("renderer") or {}).get("enabled", False))
if isinstance(value, bool):
    enabled = value
else:
    enabled = str(value).strip().casefold() in {"1", "true", "yes", "on"}
print("1" if enabled else "0")
PYRENDER
)"
case "$PLAYWRIGHT_ENABLED" in 0|1) ;; *)
  echo "Could not determine archive.renderer.enabled from $PREFIX/web.yaml" >&2
  exit 1
esac

if [[ "$PLAYWRIGHT_ENABLED" -eq 1 ]]; then
  PLAYWRIGHT_SECCOMP_PROFILE="./components/playwright-renderer/seccomp_profile.json"
  log "Preparing Playwright Chromium seccomp profile"
  "$PREFIX/install/components/playwright-renderer/prepare.sh"
else
  PLAYWRIGHT_SECCOMP_PROFILE="unconfined"
fi
if grep -q '^PLAYWRIGHT_SECCOMP_PROFILE=' "$PREFIX/install/.env"; then
  sed -i "s|^PLAYWRIGHT_SECCOMP_PROFILE=.*|PLAYWRIGHT_SECCOMP_PROFILE=$PLAYWRIGHT_SECCOMP_PROFILE|" "$PREFIX/install/.env"
else
  printf '\nPLAYWRIGHT_SECCOMP_PROFILE=%s\n' "$PLAYWRIGHT_SECCOMP_PROFILE" >> "$PREFIX/install/.env"
fi

cd "$PREFIX/install"
# Explicit negative switches win over retained rerun state. Remove only the
# container; persistent volumes are preserved for a later re-enable.
if [[ $OPENWEBUI_EXPLICIT -eq 1 && $WITH_OPENWEBUI -eq 0 ]]; then
  compose_cmd -f docker-compose.yml --env-file .env --profile ui stop openwebui >/dev/null 2>&1 || true
  compose_cmd -f docker-compose.yml --env-file .env --profile ui rm -f openwebui >/dev/null 2>&1 || true
fi
if [[ $PROXY_EXPLICIT -eq 1 && $WITH_PROXY -eq 0 ]]; then
  compose_cmd -f docker-compose.yml --env-file .env stop proxy >/dev/null 2>&1 || true
  compose_cmd -f docker-compose.yml --env-file .env rm -f proxy >/dev/null 2>&1 || true
fi
if [[ "$PLAYWRIGHT_ENABLED" -eq 0 ]]; then
  compose_cmd -f docker-compose.yml --env-file .env --profile renderer stop playwright-renderer >/dev/null 2>&1 || true
  compose_cmd -f docker-compose.yml --env-file .env --profile renderer rm -f playwright-renderer >/dev/null 2>&1 || true
fi

LOCAL_SERVICES=()
LOCAL_PROFILE_ARGS=()
if [[ $WITH_QDRANT -eq 1 ]]; then
  LOCAL_SERVICES+=(qdrant)
  LOCAL_PROFILE_ARGS+=(--profile qdrant)
fi
if [[ $WITH_NEO4J -eq 1 ]]; then
  LOCAL_SERVICES+=(neo4j)
  LOCAL_PROFILE_ARGS+=(--profile neo4j)
fi
if [[ ${#LOCAL_SERVICES[@]} -gt 0 ]]; then
  log "Starting local data services: ${LOCAL_SERVICES[*]}"
  compose_cmd -f docker-compose.yml --env-file .env "${LOCAL_PROFILE_ARGS[@]}" up -d "${LOCAL_SERVICES[@]}"
fi

if [[ "$PLAYWRIGHT_ENABLED" -eq 1 ]]; then
  log "Building and starting Playwright renderer for Web archive"
  compose_cmd -f docker-compose.yml --env-file .env --profile renderer build playwright-renderer
  compose_cmd -f docker-compose.yml --env-file .env --profile renderer up -d playwright-renderer
  PLAYWRIGHT_READY=0
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${PLAYWRIGHT_PORT:-8090}/live" >/dev/null 2>&1; then
      PLAYWRIGHT_READY=1
      break
    fi
    sleep 2
  done
  if [[ $PLAYWRIGHT_READY -ne 1 ]]; then
    echo "Playwright renderer did not become reachable on 127.0.0.1:${PLAYWRIGHT_PORT:-8090}." >&2
    compose_cmd -f docker-compose.yml --env-file .env --profile renderer logs --tail=120 playwright-renderer >&2 || true
    exit 1
  fi
fi

if [[ $WITH_NEO4J -eq 1 ]]; then
  log "Waiting for Neo4j and applying the idempotent SunaQ schema upgrade"
  NEO4J_SCHEMA_READY=0
  NEO4J_SCHEMA_ATTEMPTS=90
  NEO4J_SCHEMA_STARTED_AT=$(date +%s)
  NEO4J_SCHEMA_LAST_NOTICE=0
  for attempt in $(seq 1 "$NEO4J_SCHEMA_ATTEMPTS"); do
    # The installer currently runs from $PREFIX/install for Compose. Python
    # modules live one directory above, so run schema initialization with the
    # application root as cwd instead of relying on an incidental PYTHONPATH.
    if (
      cd "$PREFIX"
      run_as_rag env NEO4J_PASSWORD="$NEO4J_PASSWORD" \
        "$PREFIX/.venv/bin/python" -m rag.graph --config "$PREFIX/config.yaml" init
    ) >/dev/null 2>&1; then
      NEO4J_SCHEMA_READY=1
      elapsed=$(( $(date +%s) - NEO4J_SCHEMA_STARTED_AT ))
      echo "[INFO] Neo4j is ready; SunaQ schema upgrade completed after ${elapsed}s."
      break
    fi
    elapsed=$(( $(date +%s) - NEO4J_SCHEMA_STARTED_AT ))
    if (( elapsed - NEO4J_SCHEMA_LAST_NOTICE >= 10 )); then
      echo "[INFO] Neo4j/schema initialization still waiting (${elapsed}s; attempt ${attempt}/${NEO4J_SCHEMA_ATTEMPTS}) ..."
      NEO4J_SCHEMA_LAST_NOTICE=$elapsed
    fi
    sleep 2
  done
  if [[ $NEO4J_SCHEMA_READY -ne 1 ]]; then
    echo "Neo4j did not become ready or the SunaQ schema upgrade failed." >&2
    (
      cd "$PREFIX"
      run_as_rag env NEO4J_PASSWORD="$NEO4J_PASSWORD" \
        "$PREFIX/.venv/bin/python" -m rag.graph --config "$PREFIX/config.yaml" init
    ) >&2 || true
    compose_cmd -f docker-compose.yml --env-file .env --profile neo4j logs --tail=120 neo4j >&2 || true
    exit 1
  fi
fi



if [[ $WITH_OPENWEBUI -eq 1 ]]; then
  log "Starting OpenWebUI"
  compose_cmd -f docker-compose.yml --env-file .env --profile ui up -d openwebui
  # Fail early if Compose did not inject the provider key we just synchronized.
  # ENABLE_PERSISTENT_CONFIG=false makes this container environment the source
  # of truth for the provider connection, avoiding stale DB ConfigVar values.
  OPENWEBUI_CONTAINER_ID="$(compose_cmd -f docker-compose.yml --env-file .env --profile ui ps -q openwebui)"
  OPENWEBUI_EFFECTIVE_KEY="$(docker inspect "$OPENWEBUI_CONTAINER_ID" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^OPENAI_API_KEY=//p' | head -1)"
  if [[ -z "$OPENWEBUI_EFFECTIVE_KEY" || "$OPENWEBUI_EFFECTIVE_KEY" != "$PROVIDER_API_KEY" ]]; then
    echo "OpenWebUI provider key injection failed; refusing to leave a broken frontend configuration." >&2
    exit 1
  fi
  log "Waiting for OpenWebUI on 127.0.0.1:${OPENWEBUI_PORT:-3000} (first-start migrations can take several minutes)"
  OPENWEBUI_READY=0
  OPENWEBUI_WAIT_SECONDS=900
  OPENWEBUI_STARTED_AT=$(date +%s)
  OPENWEBUI_LAST_NOTICE=0
  while (( $(date +%s) - OPENWEBUI_STARTED_AT < OPENWEBUI_WAIT_SECONDS )); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health" >/dev/null 2>&1; then
      OPENWEBUI_READY=1
      break
    fi
    state="$(docker inspect "$OPENWEBUI_CONTAINER_ID" --format '{{.State.Status}}' 2>/dev/null || true)"
    if [[ "$state" == "exited" || "$state" == "dead" ]]; then
      echo "OpenWebUI container stopped during initialization (state=$state)." >&2
      compose_cmd -f docker-compose.yml --env-file .env --profile ui logs --tail=120 openwebui >&2 || true
      exit 1
    fi
    elapsed=$(( $(date +%s) - OPENWEBUI_STARTED_AT ))
    if (( elapsed - OPENWEBUI_LAST_NOTICE >= 30 )); then
      echo "[INFO] OpenWebUI is still initializing (${elapsed}s; database migrations may still be running) ..."
      OPENWEBUI_LAST_NOTICE=$elapsed
    fi
    sleep 2
  done
  if [[ $OPENWEBUI_READY -ne 1 ]]; then
    echo "OpenWebUI did not become healthy within ${OPENWEBUI_WAIT_SECONDS}s on 127.0.0.1:${OPENWEBUI_PORT:-3000}." >&2
    compose_cmd -f docker-compose.yml --env-file .env --profile ui logs --tail=120 openwebui >&2 || true
    exit 1
  fi
fi

if [[ $WITH_PROXY -eq 1 ]]; then
  log "Starting nginx reverse proxy on HTTPS ${PROXY_HTTPS_PORT} (HTTP ${PROXY_HTTP_PORT} redirects)"
  compose_cmd -f docker-compose.yml --env-file .env up -d proxy
  PROXY_READY=0
  for _ in $(seq 1 30); do
    if curl -kfsS --max-time 3 "https://127.0.0.1:${PROXY_HTTPS_PORT}/proxy-health" >/dev/null 2>&1; then
      PROXY_READY=1
      break
    fi
    sleep 1
  done
  if [[ $PROXY_READY -ne 1 ]]; then
    echo "nginx reverse proxy did not become healthy on HTTPS ${PROXY_HTTPS_PORT}." >&2
    compose_cmd -f docker-compose.yml --env-file .env logs --tail=80 proxy >&2 || true
    exit 1
  fi
fi

cat > "$PREFIX/install/install-state.env" <<STATE
LOCAL_QDRANT=$WITH_QDRANT
LOCAL_NEO4J=$WITH_NEO4J
LOCAL_OPENWEBUI=$WITH_OPENWEBUI
LOCAL_PLAYWRIGHT=$PLAYWRIGHT_ENABLED
LOCAL_PROXY=$WITH_PROXY
PROXY_HTTP_PORT=$PROXY_HTTP_PORT
PROXY_HTTPS_PORT=$PROXY_HTTPS_PORT
PROXY_BASIC_AUTH_STATE=$PROXY_BASIC_AUTH
MULTI_USER=$MULTI_USER
ACL_OFF=$ACL_OFF
STATE
chown "$RAG_USER:$RAG_GROUP" "$PREFIX/install/install-state.env"

if [[ $DOWNLOAD_RERANKER -eq 1 ]]; then
  mapfile -t RERANK_CFG < <($PREFIX/.venv/bin/python - <<PY
import yaml
with open('$PREFIX/config.yaml', encoding='utf-8') as f:
    cfg=yaml.safe_load(f) or {}
r=cfg.get('reranker') or {}
print(str(r.get('backend') or 'none').strip().lower())
print(str(r.get('model') or 'BAAI/bge-reranker-v2-m3'))
PY
)
  RERANK_BACKEND="${RERANK_CFG[0]:-none}"
  RERANK_MODEL="${RERANK_CFG[1]:-BAAI/bge-reranker-v2-m3}"
  if [[ "$RERANK_BACKEND" == "local" ]]; then
    log "Pre-downloading local reranker model: $RERANK_MODEL"
    run_as_rag env \
      HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 RERANK_MODEL="$RERANK_MODEL" \
      "$PREFIX/.venv/bin/python" - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(repo_id=os.environ["RERANK_MODEL"])
PY
  else
    log "Reranker backend is '$RERANK_BACKEND'; skipping local Hugging Face model download"
  fi
fi

MAIL_WORKER_ENABLED="$("$PREFIX/.venv/bin/python" - <<PY
import yaml
with open("$PREFIX/config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
mail = cfg.get("mail") or {}
worker = mail.get("worker") or {}
print("1" if bool(mail.get("enabled", False)) and bool(worker.get("enabled", False)) else "0")
PY
)"

if [[ $WITH_SYSTEMD -eq 1 ]]; then
  log "Installing systemd services"
  for name in rag-api rag-provider rag-graph-worker rag-sync-worker rag-mail-worker; do
    src="$PREFIX/install/systemd/${name}.service.in"
    dst="/etc/systemd/system/${name}.service"
    sed \
      -e "s|@RAG_DIR@|$PREFIX|g" \
      -e "s|@RAG_USER@|$RAG_USER|g" \
      -e "s|@RAG_GROUP@|$RAG_GROUP|g" \
      "$src" > "$dst"
  done
  systemctl daemon-reload
  systemctl enable rag-api rag-provider rag-sync-worker
  if [[ "$MAIL_WORKER_ENABLED" -eq 1 ]]; then
    systemctl enable rag-mail-worker
  else
    systemctl disable rag-mail-worker >/dev/null 2>&1 || true
  fi
  if [[ $WITH_NEO4J -eq 1 ]]; then systemctl enable rag-graph-worker; fi
  # Normal services remain stopped until maintenance mode is disabled.
fi

log "Starting maintenance provider"
if [[ $WITH_SYSTEMD -eq 1 ]]; then
  systemctl start rag-provider
else
  run_as_rag "$PREFIX/start-all.sh"
fi

log "Bootstrap complete"
cat <<SUMMARY

Installed at: $PREFIX
Qdrant:      $([[ $WITH_QDRANT -eq 1 ]] && echo local/running || echo external/not installed by this run)
Neo4j:       $([[ $WITH_NEO4J -eq 1 ]] && echo local/running || echo external/not installed by this run)
LLM backend: external/admin-managed
OpenWebUI:   $([[ $WITH_OPENWEBUI -eq 1 ]] && echo local/running || echo external/not installed by this run)
Playwright:  $([[ $PLAYWRIGHT_ENABLED -eq 1 ]] && echo local/running || echo disabled/not installed by this run)
Web search:   external/admin-managed; configure web.yaml when needed
Proxy:       $([[ $WITH_PROXY -eq 1 ]] && echo https://HOST/ \(self-signed bootstrap TLS\) || echo skipped)
Proxy gate:  $([[ $WITH_PROXY -eq 1 && $PROXY_BASIC_AUTH -eq 1 ]] && echo Basic-Auth + rate-limit || echo rate-limit/no Basic-Auth)
ACL mode:    $([[ $ACL_OFF -eq 1 ]] && echo ACL-OFF-DIAGNOSTIC || ([[ $MULTI_USER -eq 1 ]] && echo credential_store || echo single_user-live-ACL))
Provider client: $PROVIDER_CLIENT_ID
Systemd:      $([[ $WITH_SYSTEMD -eq 1 ]] && echo 'units installed; provider running in maintenance mode' || echo skipped)

Generated credentials (also in $PREFIX/runtime.env):
  Admin user:       ${ADMIN_USER}
  Admin password:   ${ADMIN_PASSWORD}
  Provider API key: ${PROVIDER_API_KEY}

Next steps:
  1. Edit $PREFIX/config.yaml (Elasticsearch index, Nextcloud URL, TLS).
  2. Edit $PREFIX/provider.env for local vs. API LLM and model names.
  3. Multi-user + live ACL is the default. Users bind through /auth/nextcloud/ensure.
     --single-user requires NEXTCLOUD_USERNAME/NEXTCLOUD_APP_PASSWORD in runtime.env.
     Additional frontends need their own key: python -m rag.provider_clients create CLIENT_ID.
  4. Optional public web search is external/admin-managed; configure web.yaml and WEB_SEARCH_API_KEY when needed.
     Per-user CardDAV seeds use the Login-Flow credential and are managed under RAG Admin -> Users -> Kontakt-DB.
  5. The provider is now in maintenance mode; normal API/workers are not started.
  6. External entry point: https://<server-ip>/ (RAG admin: /rag-admin/, health: /rag-api/health).
     HTTP port 80 redirects to HTTPS. The bootstrap certificate is self-signed;
     replace install/nginx/tls/server.crt and server.key with site certificates as desired.
  7. Run preflight/smoke checks, then leave maintenance mode:
     sudo $PREFIX/install/maintenance-mode.sh off
  8. Check: $PREFIX/status.sh
 10. Firewall is NOT modified by this installer. Open inbound TCP 443 for HTTPS
     and TCP 80 if you want the HTTP-to-HTTPS redirect reachable externally.

Global runtime secrets generated during first install are in $PREFIX/runtime.env (chmod 600).
Per-user reversible credentials are encrypted in runtime/users.sqlite; master key: $CREDENTIAL_MASTER_KEY_FILE (root:$RAG_GROUP 0640).
Docker images and critical ML dependencies are pinned for this beta release.
Change pins deliberately and retest before upgrading them.
SUMMARY
