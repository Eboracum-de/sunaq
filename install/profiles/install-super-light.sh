#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFIX="/opt/sunaq"
LEGACY_PREFIX="/opt/nextcloud-rag"
PREFIX_EXPLICIT=0
NEXTCLOUD_URL=""
ELASTICSEARCH_URL=""
ELASTICSEARCH_INDEX="my_index"
ELASTICSEARCH_INDEX_EXPLICIT=0
INSTALL_SYSTEM_PACKAGES=1
ASSUME_YES=0
PLAN_ONLY=0
START_STACK=1
WITH_OPENWEBUI=0
OPENWEBUI_EXPLICIT=0
OPENWEBUI_FROM_STATE=0
WITH_PROXY=0
PROXY_EXPLICIT=0
WITH_PLAYWRIGHT=0
PLAYWRIGHT_EXPLICIT=0
PLAYWRIGHT_FROM_STATE=0
PROXY_FROM_STATE=0
PROXY_HTTP_PORT=80
PROXY_HTTPS_PORT=443
PROXY_HTTP_PORT_EXPLICIT=0
PROXY_HTTPS_PORT_EXPLICIT=0
CA_CERTIFICATES=()
X509_STRICT=0
X509_STRICT_EXPLICIT=0
PRESET_FILE=""
PRESET_NAME=""

usage() {
  cat <<'USAGE'
Usage: sudo ./install/install.sh --profile super-light [options]

Installs the SunaQ 0.8.6 super-light installation profile as a Docker-hosted deployment.  Host Python
is not used by the middleware and may be older than Python 3.10 (e.g. Leap 15.3).

Required for a started installation:
  --nextcloud-url URL       Nextcloud base URL, preferably HTTPS
  --elasticsearch-url URL   Existing Nextcloud FullTextSearch Elasticsearch URL

Options:
  --elasticsearch-index ID  Elasticsearch index (default: my_index)
  --preset core|workgroup   Apply a shipped capability preset
  --preset-file FILE        Apply a safe YAML capability overlay
  --prefix PATH             Install prefix (default: /opt/sunaq)
  --skip-system-packages    Do not install Docker/curl/jq/openssl
  --no-start                Prepare files/images but do not start the stack
  --with-openwebui          Start/retain bundled OpenWebUI (fresh default: off)
  --no-openwebui            Disable/remove bundled OpenWebUI on this host
  --with-proxy              Start/retain bundled nginx (fresh default: off)
  --no-proxy                Disable/remove bundled nginx
  --with-playwright          Build/install local Playwright renderer (fresh default: off)
  --no-playwright            Disable/remove local Playwright renderer
  --proxy-http-port PORT     nginx HTTP listen port (default: 80)
  --proxy-https-port PORT    nginx HTTPS listen port (default: 443)
  --ca-certificate FILE     Trust one private CA certificate inside API/provider containers; repeatable
  --x509-strict             Enable Python/OpenSSL VERIFY_X509_STRICT (default: off)
  --no-x509-strict          Compatibility alias; keep strict mode disabled
  --plan                    Show the intended deployment and exit
  -y, --yes                 Non-interactive confirmation
  -h, --help                Show help

Example:
  sudo ./install/install.sh --profile super-light \
    --nextcloud-url https://cloud.example.org \
    --elasticsearch-url http://10.0.0.20:9200 \
    --elasticsearch-index my_index
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; PREFIX_EXPLICIT=1; shift 2 ;;
    --nextcloud-url) NEXTCLOUD_URL="$2"; shift 2 ;;
    --elasticsearch-url) ELASTICSEARCH_URL="$2"; shift 2 ;;
    --elasticsearch-index) ELASTICSEARCH_INDEX="$2"; ELASTICSEARCH_INDEX_EXPLICIT=1; shift 2 ;;
    --preset)
      [[ $# -ge 2 ]] || { echo "--preset requires core or workgroup" >&2; exit 2; }
      case "$2" in core|workgroup) PRESET_NAME="$2"; PRESET_FILE="$SOURCE_DIR/install/presets/$2.yaml" ;; *) echo "Unknown preset: $2" >&2; exit 2 ;; esac
      shift 2 ;;
    --preset-file)
      [[ $# -ge 2 ]] || { echo "--preset-file requires a readable YAML file" >&2; exit 2; }
      PRESET_NAME="custom"; PRESET_FILE="$2"; shift 2 ;;
    --skip-system-packages) INSTALL_SYSTEM_PACKAGES=0; shift ;;
    --no-start) START_STACK=0; shift ;;
    --with-openwebui) WITH_OPENWEBUI=1; OPENWEBUI_EXPLICIT=1; shift ;;
    --no-openwebui) WITH_OPENWEBUI=0; OPENWEBUI_EXPLICIT=1; shift ;;
    --with-proxy) WITH_PROXY=1; PROXY_EXPLICIT=1; shift ;;
    --no-proxy) WITH_PROXY=0; PROXY_EXPLICIT=1; shift ;;
    --with-playwright) WITH_PLAYWRIGHT=1; PLAYWRIGHT_EXPLICIT=1; shift ;;
    --no-playwright) WITH_PLAYWRIGHT=0; PLAYWRIGHT_EXPLICIT=1; shift ;;
    --proxy-http-port) [[ $# -ge 2 ]] || { echo "--proxy-http-port requires a port" >&2; exit 2; }; PROXY_HTTP_PORT="$2"; PROXY_HTTP_PORT_EXPLICIT=1; shift 2 ;;
    --proxy-https-port) [[ $# -ge 2 ]] || { echo "--proxy-https-port requires a port" >&2; exit 2; }; PROXY_HTTPS_PORT="$2"; PROXY_HTTPS_PORT_EXPLICIT=1; shift 2 ;;
    --ca-certificate) [[ $# -ge 2 ]] || { echo "--ca-certificate requires a file" >&2; exit 2; }; CA_CERTIFICATES+=("$2"); shift 2 ;;
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
# unless the administrator explicitly supplies --prefix; moving a live install
# implicitly would be substantially more disruptive than retaining its path.
if [[ $PREFIX_EXPLICIT -eq 0 && -d "$LEGACY_PREFIX" ]] && { [[ ! -e "$PREFIX" ]] || [[ -d "$PREFIX" && -z "$(find "$PREFIX" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; }; then
  if [[ -f "$LEGACY_PREFIX/.sunaq-installation" || -f "$LEGACY_PREFIX/.aki-rag-installation" || ( -d "$LEGACY_PREFIX/rag" && -f "$LEGACY_PREFIX/config.yaml" && -d "$LEGACY_PREFIX/install" ) ]]; then
    echo "[INFO] Legacy SunaQ/AKI installation detected at $LEGACY_PREFIX; continuing in place."
    PREFIX="$LEGACY_PREFIX"
  fi
fi

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

read_state_bool() {
  local key="$1" value="$2"
  case "$value" in
    0|1) printf '%s' "$value" ;;
    *) echo "Invalid boolean value in install-state.env for $key: $value" >&2; exit 2 ;;
  esac
}

load_install_state() {
  local state_file="$PREFIX/install/install-state.env"
  [[ -f "$state_file" ]] || return 0
  local recognized=0
  [[ -f "$PREFIX/.sunaq-installation" || -f "$PREFIX/.aki-rag-installation" ]] && recognized=1
  [[ -d "$PREFIX/rag" && -f "$PREFIX/config.yaml" && -d "$PREFIX/install" ]] && recognized=1
  [[ $recognized -eq 1 ]] || return 0

  local state_local_openwebui=0 state_local_proxy=0 state_local_playwright=0
  local state_proxy_http_port=80 state_proxy_https_port=443
  local key value
  while IFS='=' read -r key value || [[ -n "$key$value" ]]; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      LOCAL_OPENWEBUI) state_local_openwebui="$(read_state_bool "$key" "$value")" ;;
      LOCAL_PROXY) state_local_proxy="$(read_state_bool "$key" "$value")" ;;
      LOCAL_PLAYWRIGHT) state_local_playwright="$(read_state_bool "$key" "$value")" ;;
      PROXY_HTTP_PORT) state_proxy_http_port="$value" ;;
      PROXY_HTTPS_PORT) state_proxy_https_port="$value" ;;
      *) : ;;
    esac
  done < "$state_file"

  if [[ $OPENWEBUI_EXPLICIT -eq 0 ]]; then
    WITH_OPENWEBUI=$state_local_openwebui
    [[ $state_local_openwebui -eq 1 ]] && OPENWEBUI_FROM_STATE=1
  fi
  if [[ $PROXY_EXPLICIT -eq 0 ]]; then
    WITH_PROXY=$state_local_proxy
    [[ $state_local_proxy -eq 1 ]] && PROXY_FROM_STATE=1
  fi
  if [[ $PLAYWRIGHT_EXPLICIT -eq 0 ]]; then
    WITH_PLAYWRIGHT=$state_local_playwright
    [[ $state_local_playwright -eq 1 ]] && PLAYWRIGHT_FROM_STATE=1
  fi
  [[ $PROXY_HTTP_PORT_EXPLICIT -eq 0 ]] && PROXY_HTTP_PORT=$state_proxy_http_port
  [[ $PROXY_HTTPS_PORT_EXPLICIT -eq 0 ]] && PROXY_HTTPS_PORT=$state_proxy_https_port
  return 0
}

load_install_state

validate_port() {
  local name="$1"
  local value="$2"
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

print_plan() {
  # Conservative image/layer estimate only. Neo4j data, web archive and other
  # site/user data are additional. OpenWebUI is deliberately expensive and is
  # counted only when explicitly requested.
  local disk_low=2
  local disk_high=4
  if [[ $WITH_OPENWEBUI -eq 1 ]]; then
    disk_low=$((disk_low + 6))
    disk_high=$((disk_high + 8))
  fi

  cat <<PLAN
SunaQ / Eboracum Research Gateway 0.8.6-rc1.2 - super-light installation profile
----------------------------------------------
Install prefix:          $PREFIX
Deployment mode:         dockerized
Host Python required:    no
Document retrieval:      Elasticsearch only
Neo4j:                   local, seeds/entity aliases/query expansion only
Graph document arm:      disabled
Graph extraction worker: disabled
Qdrant/embeddings:       disabled / not installed
Reranker/TEI:            disabled / not installed
Playwright archive:      $([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo "install/start$([[ $PLAYWRIGHT_FROM_STATE -eq 1 ]] && echo ' (retained from existing install; use --no-playwright to disable)')" || echo disabled/not installed)
OpenWebUI:               $([[ $WITH_OPENWEBUI -eq 1 ]] && echo "pull/start$([[ $OPENWEBUI_FROM_STATE -eq 1 ]] && echo ' (retained from existing install; use --no-openwebui to disable)')" || echo not pulled/not started)
Reverse proxy:           $([[ $WITH_PROXY -eq 1 ]] && echo "bundled/start on ${PROXY_HTTP_PORT}/${PROXY_HTTPS_PORT}$([[ $PROXY_FROM_STATE -eq 1 ]] && echo ' (retained from existing install; use --no-proxy to disable)')" || echo disabled)
Nextcloud URL:           ${NEXTCLOUD_URL:-<required before start>}
Elasticsearch URL:       ${ELASTICSEARCH_URL:-<required before start>}
Elasticsearch index:     $ELASTICSEARCH_INDEX
Capability preset:       ${PRESET_NAME:-core (Super-Light packaged default)}
RAM target:              4 GiB practical minimum, 4-8 GiB recommended; Neo4j 256m pagecache / 512m max heap
Private CA trust:        ${#CA_CERTIFICATES[@]} certificate(s) supplied on this run
Python X.509 strict:      $([[ $X509_STRICT -eq 1 ]] && echo enabled || echo disabled-compatibility-mode)
Estimated disk use:      roughly ${disk_low}-${disk_high} GiB before site/user data
PLAN
}

if [[ $PLAN_ONLY -eq 1 ]]; then
  print_plan
  exit 0
fi
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer as root (sudo)." >&2
  exit 1
fi
if [[ $START_STACK -eq 1 && ( -z "$NEXTCLOUD_URL" || -z "$ELASTICSEARCH_URL" ) ]]; then
  echo "--nextcloud-url and --elasticsearch-url are required unless --no-start is used." >&2
  exit 2
fi

preflight() {
  local required
  for required in     "$SOURCE_DIR/rag"     "$SOURCE_DIR/install/super-light/docker-compose.yml"     "$SOURCE_DIR/install/super-light/config.super-light.yaml"     "$SOURCE_DIR/install/super-light/provider.env.super-light.example"     "$SOURCE_DIR/install/super-light/runtime.env.super-light.example"     "$SOURCE_DIR/install/components/playwright-renderer/prepare.sh"; do
    [[ -e "$required" ]] || {
      echo "Installer source is incomplete; required path missing: $required" >&2
      exit 2
    }
  done

  if [[ -e "$PREFIX" && ! -d "$PREFIX" ]]; then
    echo "Install prefix exists but is not a directory: $PREFIX" >&2
    exit 2
  fi
  if [[ -d "$PREFIX" && ! -w "$PREFIX" ]]; then
    echo "Existing install prefix is not writable: $PREFIX" >&2
    exit 2
  fi

  # Never adopt an unrelated non-empty directory implicitly. The installer
  # refreshes selected top-level paths with rm -rf/cp, so a typo in --prefix
  # must fail before any host state is modified.
  if [[ -d "$PREFIX" ]] && [[ -n "$(find "$PREFIX" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    source_is_prefix=0
    [[ "$(readlink -f "$SOURCE_DIR")" == "$(readlink -f "$PREFIX")" ]] && source_is_prefix=1
    recognized_sunaq=0
    [[ -f "$PREFIX/.sunaq-installation" || -f "$PREFIX/.aki-rag-installation" ]] && recognized_sunaq=1
    if [[ -f "$PREFIX/install/install-state.env" ]] && grep -Eq '^DEPLOYMENT_PROFILE=(super-light|standard)$' "$PREFIX/install/install-state.env"; then
      recognized_sunaq=1
    fi
    if [[ -d "$PREFIX/rag" && -f "$PREFIX/config.yaml" && -d "$PREFIX/install" ]]; then
      recognized_sunaq=1
    fi
    if [[ $source_is_prefix -ne 1 && $recognized_sunaq -ne 1 ]]; then
      echo "Refusing to install into non-empty directory that is not recognized as a SunaQ installation: $PREFIX" >&2
      echo "Choose a dedicated --prefix (recommended: /opt/sunaq). Existing files were not modified." >&2
      exit 2
    fi
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

  if [[ -f "$PREFIX/install/super-light/docker-compose.yml" ]]; then
    echo "[INFO] Existing Super-Light installation detected at $PREFIX."
    if command -v docker >/dev/null 2>&1; then
      if ! docker info >/dev/null 2>&1; then
        echo "Docker is installed but the daemon is not reachable; rerun would not be able to rebuild/start the existing stack." >&2
        exit 2
      fi
      existing_compose=()
      if docker compose version >/dev/null 2>&1; then
        existing_compose=(docker compose)
      elif command -v docker-compose >/dev/null 2>&1; then
        existing_compose=(docker-compose)
      fi
      if [[ ${#existing_compose[@]} -gt 0 ]]; then
        existing_running="$(
          cd "$PREFIX/install/super-light" &&
          "${existing_compose[@]}" ps --services --filter status=running 2>/dev/null || true
        )"
        if [[ -n "$existing_running" ]]; then
          echo "[WARN] Existing SunaQ services are running: $(printf '%s' "$existing_running" | tr '\n' ' ')" >&2
          echo "Stop the existing stack before rerunning the installer; no installation changes were made." >&2
          exit 2
        else
          echo "[INFO] Existing SunaQ stack is stopped; rerun may rebuild/start it."
        fi
      else
        echo "[WARN] Existing installation found but Docker Compose is not currently available; package installation may repair this."
      fi
    else
      echo "[WARN] Existing installation found but Docker is not currently available; package installation may repair this."
    fi
  fi
}

preflight
print_plan
if [[ $ASSUME_YES -ne 1 ]]; then
  if [[ ! -t 0 ]]; then
    echo "Non-interactive stdin: rerun with -y after reviewing --plan." >&2
    exit 1
  fi
  printf '\nProceed? [y/N] '
  read -r answer
  case "$answer" in y|Y|yes|YES|Yes) ;; *) echo "Cancelled."; exit 0 ;; esac
fi

install_packages() {
  [[ $INSTALL_SYSTEM_PACKAGES -eq 1 ]] || return 0
  log "Installing host prerequisites (no middleware Python packages)"
  if command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive refresh
    zypper --non-interactive install ca-certificates curl jq openssl docker docker-compose
    systemctl enable --now docker
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl jq openssl docker.io
    apt-get install -y docker-compose-v2 2>/dev/null || apt-get install -y docker-compose-plugin 2>/dev/null || apt-get install -y docker-compose
    systemctl enable --now docker
  else
    echo "Unsupported package manager. Install Docker, Docker Compose, curl, jq and openssl manually." >&2
    exit 1
  fi
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "Docker Compose not found." >&2
    exit 1
  fi
}

mail_worker_enabled_from_config() {
  [[ -f "$PREFIX/config.yaml" ]] || return 1
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
  ' "$PREFIX/config.yaml"
}

random_secret() { openssl rand -hex 32; }
sed_repl() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }

install_packages
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

probe_configured_services

log "Installing source tree"
mkdir -p "$PREFIX"
# This profile is intended for a dedicated node. Preserve site-owned runtime/config
# files on rerun while refreshing application/templates.  When the installer is
# rerun from the installed tree itself, do not delete its own source files.
if [[ "$(readlink -f "$SOURCE_DIR")" != "$(readlink -f "$PREFIX")" ]]; then
  for item in rag prompts ontology docs clients requirements.txt requirements-super-light.txt versions.lock.yaml pyproject.toml README.md CHANGELOG.md SECURITY.md .dockerignore; do
    rm -rf "$PREFIX/$item"
    cp -a "$SOURCE_DIR/$item" "$PREFIX/$item"
  done
  # Merge installer templates instead of replacing install/ wholesale: TLS material,
  # generated.conf and the local super-light .env are site state and survive reruns.
  mkdir -p "$PREFIX/install"
  cp -a "$SOURCE_DIR/install/." "$PREFIX/install/"
fi
# Seed SunaQ models once, then preserve each installed package as site
# configuration. On upgrades, copy only newly shipped package directories so
# new bundled profiles become available without overwriting local tuning.
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
mkdir -p "$PREFIX/runtime" "$PREFIX/runtime/ca"
touch "$PREFIX/runtime/ca/.keep"
# Keep private trust anchors as site-owned runtime state.  The Docker build only
# consumes runtime/ca; other runtime secrets are excluded via .dockerignore.
NEXTCLOUD_CA_FILE=""
if [[ ${#CA_CERTIFICATES[@]} -gt 0 ]]; then
  log "Installing private CA trust anchors for containerized middleware"
  rm -f "$PREFIX/runtime/ca"/installer-*.crt
  ca_index=0
  for ca_source in "${CA_CERTIFICATES[@]}"; do
    ca_index=$((ca_index + 1))
    printf -v ca_name 'installer-%02d.crt' "$ca_index"
    cp "$ca_source" "$PREFIX/runtime/ca/$ca_name"
    chmod 0644 "$PREFIX/runtime/ca/$ca_name"
  done
  cat "$PREFIX/runtime/ca"/installer-*.crt > "$PREFIX/runtime/ca/nextcloud-ca-bundle.pem"
  chmod 0644 "$PREFIX/runtime/ca/nextcloud-ca-bundle.pem"
  NEXTCLOUD_CA_FILE="/app/runtime/ca/nextcloud-ca-bundle.pem"
fi

if [[ ! -f "$PREFIX/config.yaml" ]]; then
  cp "$PREFIX/install/super-light/config.super-light.yaml" "$PREFIX/config.yaml"
fi
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
if [[ $X509_STRICT -eq 1 ]]; then
  sed -i "/^tls:/,/^[^[:space:]]/ s/^  x509_strict:.*/  x509_strict: true/" "$PREFIX/config.yaml"
else
  sed -i "/^tls:/,/^[^[:space:]]/ s/^  x509_strict:.*/  x509_strict: false/" "$PREFIX/config.yaml"
fi
if [[ ! -f "$PREFIX/web.yaml" ]]; then
  cp "$PREFIX/install/super-light/web.super-light.yaml" "$PREFIX/web.yaml"
fi
# Renderer installation state is explicit. Fresh installs stay off; reruns
# preserve the recorded state unless --with/--no-playwright is supplied.
if [[ $WITH_PLAYWRIGHT -eq 1 ]]; then
  sed -i "/^  renderer:/,/^  timeout:/ s/^    enabled:.*/    enabled: true/" "$PREFIX/web.yaml"
else
  sed -i "/^  renderer:/,/^  timeout:/ s/^    enabled:.*/    enabled: false/" "$PREFIX/web.yaml"
fi
if [[ ! -f "$PREFIX/provider.env" ]]; then
  cp "$PREFIX/install/super-light/provider.env.super-light.example" "$PREFIX/provider.env"
fi
if [[ ! -f "$PREFIX/runtime.env" ]]; then
  cp "$PREFIX/install/super-light/runtime.env.super-light.example" "$PREFIX/runtime.env"
fi

if [[ -n "$NEXTCLOUD_URL" ]]; then
  esc="$(sed_repl "${NEXTCLOUD_URL%/}")"
  sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  base_url:.*|  base_url: ${esc}/|" "$PREFIX/config.yaml"
fi
if [[ -n "$NEXTCLOUD_CA_FILE" ]]; then
  esc="$(sed_repl "$NEXTCLOUD_CA_FILE")"
  sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  verify_tls:.*|  verify_tls: true|" "$PREFIX/config.yaml"
  sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  ca_file:.*|  ca_file: ${esc}|" "$PREFIX/config.yaml"
fi
if [[ -n "$ELASTICSEARCH_URL" ]]; then
  esc="$(sed_repl "${ELASTICSEARCH_URL%/}")"
  sed -i "/^elasticsearch:/,/^[^[:space:]]/ s|^  url:.*|  url: ${esc}|" "$PREFIX/config.yaml"
fi
idx="$(sed_repl "$ELASTICSEARCH_INDEX")"
sed -i "/^elasticsearch:/,/^[^[:space:]]/ s|^  index:.*|  index: ${idx}|" "$PREFIX/config.yaml"

set_runtime_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$PREFIX/runtime.env"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$PREFIX/runtime.env"
  else
    printf '%s=%s\n' "$key" "$value" >> "$PREFIX/runtime.env"
  fi
}

repair_legacy_runtime_env_newline_bug() {
  # One buggy RC5 rerun wrote literal "\\n" separators while appending
  # newly introduced RAG_* keys to an older runtime.env. Repair only that
  # recognizable upgrade artifact before reading the service keys.
  if grep -Eq '\\n(RAG_MAINTENANCE_MODE|RAG_INTERNAL_API_KEY|RAG_PROVIDER_INTERNAL_KEY|RAG_ADMIN_USER|RAG_ADMIN_PASSWORD)=' "$PREFIX/runtime.env"; then
    local tmp
    tmp="$(mktemp)"
    awk '{ if ($0 ~ /\\nRAG_/) gsub(/\\nRAG_/, "\nRAG_"); print }' "$PREFIX/runtime.env" > "$tmp"
    cat "$tmp" > "$PREFIX/runtime.env"
    rm -f "$tmp"
  fi
}

repair_legacy_runtime_env_newline_bug

# Every install/update returns to the explicit maintenance gate first.
set_runtime_env_value RAG_MAINTENANCE_MODE true

NEO4J_PASSWORD="$(sed -n 's/^NEO4J_PASSWORD=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$NEO4J_PASSWORD" || "$NEO4J_PASSWORD" == "replace-me" ]]; then
  NEO4J_PASSWORD="$(random_secret)"
  set_runtime_env_value NEO4J_PASSWORD "$NEO4J_PASSWORD"
fi
PROVIDER_API_KEY="$(sed -n 's/^PROVIDER_API_KEY=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$PROVIDER_API_KEY" || "$PROVIDER_API_KEY" == "replace-me" ]]; then
  PROVIDER_API_KEY="$(random_secret)"
  set_runtime_env_value PROVIDER_API_KEY "$PROVIDER_API_KEY"
fi
RAG_INTERNAL_API_KEY="$(sed -n 's/^RAG_INTERNAL_API_KEY=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$RAG_INTERNAL_API_KEY" || "$RAG_INTERNAL_API_KEY" == "replace-me" ]]; then
  RAG_INTERNAL_API_KEY="$(random_secret)"
  set_runtime_env_value RAG_INTERNAL_API_KEY "$RAG_INTERNAL_API_KEY"
fi
RAG_PROVIDER_INTERNAL_KEY="$(sed -n 's/^RAG_PROVIDER_INTERNAL_KEY=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$RAG_PROVIDER_INTERNAL_KEY" || "$RAG_PROVIDER_INTERNAL_KEY" == "replace-me" ]]; then
  RAG_PROVIDER_INTERNAL_KEY="$(random_secret)"
  set_runtime_env_value RAG_PROVIDER_INTERNAL_KEY "$RAG_PROVIDER_INTERNAL_KEY"
fi
ADMIN_USER="$(sed -n 's/^RAG_ADMIN_USER=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$ADMIN_USER" || "$ADMIN_USER" == "replace-me" ]]; then
  ADMIN_USER="admin"
  set_runtime_env_value RAG_ADMIN_USER "$ADMIN_USER"
fi
ADMIN_PASSWORD="$(sed -n 's/^RAG_ADMIN_PASSWORD=//p' "$PREFIX/runtime.env" | head -1)"
if [[ -z "$ADMIN_PASSWORD" || "$ADMIN_PASSWORD" == "replace-me" ]]; then
  ADMIN_PASSWORD="$(random_secret)"
  set_runtime_env_value RAG_ADMIN_PASSWORD "$ADMIN_PASSWORD"
fi
chmod 600 "$PREFIX/runtime.env" "$PREFIX/provider.env"

if [[ ! -s "$PREFIX/runtime/credential-master.key" ]]; then
  openssl rand -base64 32 > "$PREFIX/runtime/credential-master.key"
fi
chown -R 10001:10001 "$PREFIX/runtime"
chmod 700 "$PREFIX/runtime"
chmod 600 "$PREFIX/runtime/credential-master.key"

log "Synchronizing file-backed container secrets"
bash "$PREFIX/install/sync-container-secrets.sh" "$PREFIX"

cat > "$PREFIX/install/super-light/.env" <<ENV
NEO4J_IMAGE=neo4j:5.26.29-community@sha256:d9dd3dc7d1c78fa959191ff02dbdcbefadceaf83eee23428fb92a58cac8ad3fe
OPENWEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e
NGINX_IMAGE=nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46
OPENWEBUI_PROVIDER_API_KEY=${PROVIDER_API_KEY}
NEO4J_HTTP_PORT=7474
NEO4J_BOLT_PORT=7687
NEO4J_HEAP_INITIAL=256m
NEO4J_HEAP_MAX=512m
NEO4J_PAGECACHE=256m
PLAYWRIGHT_PORT=8090
PLAYWRIGHT_SECCOMP_PROFILE=$([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo "../components/playwright-renderer/seccomp_profile.json" || echo "unconfined")
OPENWEBUI_PORT=3000
ENV
chmod 600 "$PREFIX/install/super-light/.env"

# Machine-readable installer state for diagnostics/smoke tests. Site-owned
# runtime/configuration remains authoritative; this file only records what this
# installer selected on the current host.
cat > "$PREFIX/install/install-state.env" <<ENVSTATE
DEPLOYMENT_PROFILE=super-light
DEPLOYMENT_MODE=dockerized
LOCAL_QDRANT=0
LOCAL_NEO4J=1
LOCAL_PLAYWRIGHT=$WITH_PLAYWRIGHT
LOCAL_OPENWEBUI=$WITH_OPENWEBUI
LOCAL_PROXY=$WITH_PROXY
PROXY_HTTP_PORT=$PROXY_HTTP_PORT
PROXY_HTTPS_PORT=$PROXY_HTTPS_PORT
MULTI_USER=1
ENVSTATE
chmod 0644 "$PREFIX/install/install-state.env"

cat > "$PREFIX/.sunaq-installation" <<MARKER
SUNAQ_INSTALLATION=1
DEPLOYMENT_PROFILE=super-light
DEPLOYMENT_MODE=dockerized
MARKER
chmod 0644 "$PREFIX/.sunaq-installation"

# Keep the exact wrapper invocation used for this deployment/rerun. This file is
# root-owned operational metadata; it contains no generated secrets, but may
# contain internal URLs and certificate paths and therefore is not world-readable.
INSTALL_INVOCATION="${SUNAQ_INSTALL_INVOCATION:-${AKI_INSTALL_INVOCATION:-}}"
if [[ -z "$INSTALL_INVOCATION" ]]; then
  printf -v INSTALL_INVOCATION '%q ' "$0" "$@"
  INSTALL_INVOCATION="${INSTALL_INVOCATION% }"
fi
cat > "$PREFIX/install/last-install-command.sh" <<EOF
#!/usr/bin/env bash
# Last installer invocation recorded for reproducible reruns.
$INSTALL_INVOCATION
EOF
chmod 0600 "$PREFIX/install/last-install-command.sh"

if [[ $WITH_PROXY -eq 1 ]]; then
  log "Preparing nginx TLS/admin gate"
  TLS_DIR="$PREFIX/install/nginx/tls"
  mkdir -p "$TLS_DIR"
  if [[ ! -s "$TLS_DIR/server.crt" || ! -s "$TLS_DIR/server.key" ]]; then
    CERT_CN="$(hostname -f 2>/dev/null || hostname)"
    OPENSSL_CFG="$(mktemp)"
    cat > "$OPENSSL_CFG" <<EOFSSL
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = ${CERT_CN}
[v3]
subjectAltName = DNS:${CERT_CN},DNS:localhost,IP:127.0.0.1
EOFSSL
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
      -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" -config "$OPENSSL_CFG" >/dev/null 2>&1
    rm -f "$OPENSSL_CFG"
  fi
  chmod 600 "$TLS_DIR/server.key"
  chmod 644 "$TLS_DIR/server.crt"
  printf '%s:%s\n' "${ADMIN_USER:-admin}" "$(openssl passwd -apr1 "$ADMIN_PASSWORD")" > "$PREFIX/install/nginx/htpasswd"
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

fi


# Remove any stale generated override before one-off build/preset operations.
# The final override is generated only after the resulting architecture/config
# has been validated, so optional services follow the effective capabilities.
OVERRIDE="$PREFIX/install/super-light/docker-compose.override.yml"
rm -f "$OVERRIDE"

if [[ $WITH_PLAYWRIGHT -eq 1 ]]; then
  log "Preparing Playwright Chromium seccomp profile"
  "$PREFIX/install/components/playwright-renderer/prepare.sh"
fi

cd "$PREFIX/install/super-light"
if [[ $WITH_PLAYWRIGHT -eq 0 ]]; then
  compose stop playwright-renderer >/dev/null 2>&1 || true
  compose rm -f playwright-renderer >/dev/null 2>&1 || true
fi
log "Building super-light API/provider images"
compose build api provider

if [[ -n "$PRESET_FILE" ]]; then
  log "Applying capability preset: ${PRESET_NAME:-custom}"
  PRESET_STAGE="$PREFIX/runtime/install-preset.yaml"
  PRESET_OUTPUT="$PREFIX/runtime/install-config.yaml"
  cp "$PRESET_FILE" "$PRESET_STAGE"
  chmod 0644 "$PRESET_STAGE"
  rm -f "$PRESET_OUTPUT"
  # config.yaml is deliberately mounted read-only in runtime containers. Apply
  # the preset to a writable runtime staging file, then install it host-side.
  compose run --rm --no-deps provider python -m rag.config_preset \
    --config /app/config.yaml \
    --preset /app/runtime/install-preset.yaml \
    --output /app/runtime/install-config.yaml
  [[ -s "$PRESET_OUTPUT" ]] || {
    echo "Preset application did not produce a staged configuration: $PRESET_OUTPUT" >&2
    exit 2
  }
  cp "$PRESET_OUTPUT" "$PREFIX/config.yaml"
  rm -f "$PRESET_STAGE" "$PRESET_OUTPUT"

  # Explicit installer arguments remain authoritative over preset values.
  if [[ $X509_STRICT_EXPLICIT -eq 1 ]]; then
    if [[ $X509_STRICT -eq 1 ]]; then
      sed -i "/^tls:/,/^[^[:space:]]/ s/^  x509_strict:.*/  x509_strict: true/" "$PREFIX/config.yaml"
    else
      sed -i "/^tls:/,/^[^[:space:]]/ s/^  x509_strict:.*/  x509_strict: false/" "$PREFIX/config.yaml"
    fi
  fi
  if [[ -n "$NEXTCLOUD_URL" ]]; then
    esc="$(sed_repl "${NEXTCLOUD_URL%/}")"
    sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  base_url:.*|  base_url: ${esc}/|" "$PREFIX/config.yaml"
  fi
  if [[ -n "$NEXTCLOUD_CA_FILE" ]]; then
    esc="$(sed_repl "$NEXTCLOUD_CA_FILE")"
    sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  verify_tls:.*|  verify_tls: true|" "$PREFIX/config.yaml"
    sed -i "/^nextcloud:/,/^[^[:space:]]/ s|^  ca_file:.*|  ca_file: ${esc}|" "$PREFIX/config.yaml"
  fi
  if [[ -n "$ELASTICSEARCH_URL" ]]; then
    esc="$(sed_repl "${ELASTICSEARCH_URL%/}")"
    sed -i "/^elasticsearch:/,/^[^[:space:]]/ s|^  url:.*|  url: ${esc}|" "$PREFIX/config.yaml"
  fi
  if [[ $ELASTICSEARCH_INDEX_EXPLICIT -eq 1 ]]; then
    idx="$(sed_repl "$ELASTICSEARCH_INDEX")"
    sed -i "/^elasticsearch:/,/^[^[:space:]]/ s|^  index:.*|  index: ${idx}|" "$PREFIX/config.yaml"
  fi
fi

log "Validating final SRC/ERG architecture configuration"
# This is a pure config/policy check and does not require Compose orchestration.
# In particular, avoid a second one-off `docker-compose run`: legacy
# docker-compose 1.25.x can crash while reconciling one-off containers/orphans
# after a rerun changed the service set (for example when disabling Playwright).
SUNAQ_RUNTIME_IMAGE="$(
  awk '/^[[:space:]]*image:[[:space:]]+sunaq:/{print $2; exit}' \
    "$PREFIX/install/super-light/docker-compose.yml"
)"
[[ -n "$SUNAQ_RUNTIME_IMAGE" ]] || {
  echo "Could not determine SunaQ runtime image for final configuration validation." >&2
  exit 2
}
if ! docker run --rm \
  -v "$PREFIX/config.yaml:/app/config.yaml:ro" \
  "$SUNAQ_RUNTIME_IMAGE" \
  python -m rag.config_preset --config /app/config.yaml --validate-only; then
  exit 2
fi

# Generate the effective optional-service override from the validated final
# configuration. Core/SRC therefore has no Mail worker service at all, while
# ERG can expose it only when mail and its worker are explicitly enabled.
MAIL_WORKER_ACTIVE=0
mail_worker_enabled_from_config && MAIL_WORKER_ACTIVE=1

OVERRIDE_TMP="$OVERRIDE.tmp"
rm -f "$OVERRIDE_TMP"
if [[ $WITH_PLAYWRIGHT -eq 1 || $WITH_OPENWEBUI -eq 1 || $WITH_PROXY -eq 1 || $MAIL_WORKER_ACTIVE -eq 1 ]]; then
  cat > "$OVERRIDE_TMP" <<'EOFOVR'
version: "2.4"
services:
EOFOVR
  if [[ $WITH_PLAYWRIGHT -eq 1 ]]; then
    cat >> "$OVERRIDE_TMP" <<'EOFOVR'
  playwright-renderer:
    build:
      context: ../components/playwright-renderer
    image: rag-playwright-renderer:0.2.3
    restart: unless-stopped
    init: true
    ipc: host
    ports:
      - "127.0.0.1:${PLAYWRIGHT_PORT:-8090}:8080"
    environment:
      RENDER_NAV_TIMEOUT_MS: "30000"
      RENDER_POSTLOAD_WAIT_MS: "750"
      RENDER_MAX_PDF_BYTES: "52428800"
      RENDER_ALLOWED_PORTS: "80,443"
      RENDER_MAX_CONCURRENCY: "1"
      RENDER_STATE_DIR: "/state"
    volumes:
      - playwright_state:/state
    read_only: true
    tmpfs:
      - /tmp:size=512m,mode=1777
      - /home/ragpw:size=256m,uid=10001,gid=10001,mode=0700
    pids_limit: 512
    security_opt:
      - no-new-privileges:true
      - "seccomp=${PLAYWRIGHT_SECCOMP_PROFILE:-unconfined}"
EOFOVR
  fi
  if [[ $WITH_OPENWEBUI -eq 1 ]]; then
    cat >> "$OVERRIDE_TMP" <<'EOFOVR'
  openwebui:
    image: ${OPENWEBUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.11.4-slim@sha256:0487ad4a5a4b986062dedace806c3ef1e88fec38c10d1e64d6a5501c66671e5e}
    restart: unless-stopped
    network_mode: host
    environment:
      HOST: "127.0.0.1"
      PORT: "${OPENWEBUI_PORT:-3000}"
      ENABLE_OLLAMA_API: "false"
      ENABLE_OPENAI_API: "true"
      ENABLE_FORWARD_USER_INFO_HEADERS: "false"
      ENABLE_PERSISTENT_CONFIG: "false"
      BYPASS_MODEL_ACCESS_CONTROL: "true"
      ENABLE_EVALUATION_ARENA_MODELS: "false"
      OPENAI_API_BASE_URL: "http://127.0.0.1:8766/v1"
      OPENAI_API_KEY: "${OPENWEBUI_PROVIDER_API_KEY:-}"
      OPENAI_API_CONFIGS: '{"0":{"enable":true,"headers":{"X-OpenWebUI-User-Id":"{{USER_ID}}"}}}'
    volumes:
      - openwebui_data:/app/backend/data
    depends_on:
      - provider
EOFOVR
  fi
  if [[ $WITH_PROXY -eq 1 ]]; then
    cat >> "$OVERRIDE_TMP" <<'EOFOVR'
  proxy:
    image: ${NGINX_IMAGE:-nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46}
    restart: unless-stopped
    network_mode: host
    volumes:
      - ../nginx/generated.conf:/etc/nginx/nginx.conf:ro
      - ../nginx/htpasswd:/etc/nginx/htpasswd:ro
      - ../nginx/internal-auth.conf:/etc/nginx/internal-auth.conf:ro
      - ../nginx/tls:/etc/nginx/tls:ro
    depends_on:
      - provider
EOFOVR
  fi
  if [[ $MAIL_WORKER_ACTIVE -eq 1 ]]; then
    cat >> "$OVERRIDE_TMP" <<'EOFOVR'
  mail-worker:
    image: sunaq:0.8.6-rc1.2
    restart: unless-stopped
    network_mode: host
    env_file:
      - ../../runtime.container.env
    command: ["python", "-m", "rag.mail_worker", "--config", "/app/config.yaml"]
    volumes:
      - ../../config.yaml:/app/config.yaml:ro
      - ../../web.yaml:/app/web.yaml:ro
      - ../../runtime:/app/runtime
      - ../../runtime/service-secrets:/run/sunaq-secrets:ro
    depends_on:
      - api
EOFOVR
  fi
  if [[ $WITH_PLAYWRIGHT -eq 1 || $WITH_OPENWEBUI -eq 1 ]]; then
    cat >> "$OVERRIDE_TMP" <<'EOFOVR'
volumes:
EOFOVR
    [[ $WITH_PLAYWRIGHT -eq 1 ]] && echo "  playwright_state:" >> "$OVERRIDE_TMP"
    [[ $WITH_OPENWEBUI -eq 1 ]] && echo "  openwebui_data:" >> "$OVERRIDE_TMP"
  fi
  mv "$OVERRIDE_TMP" "$OVERRIDE"
else
  rm -f "$OVERRIDE"
fi
CONFIG_TX_ACTIVE=0
rm -rf "$CONFIG_TX_DIR"
trap - EXIT

if [[ $WITH_PLAYWRIGHT -eq 1 ]]; then
  log "Building Playwright renderer image"
  compose build playwright-renderer
fi


log "Registering default trusted provider client"
compose run --rm --no-deps provider python - <<'PYCLIENT'
from rag.credential_store import CredentialStore
from rag.secret_env import secret_env
key = secret_env("PROVIDER_API_KEY", "").strip()
if len(key) < 24:
    raise SystemExit("PROVIDER_API_KEY missing/too short")
store = CredentialStore("runtime/users.sqlite")
store.register_client("default-client", key, name="Default trusted frontend", replace=True)
print("trusted provider client registered: default-client")
PYCLIENT

if [[ $START_STACK -eq 0 ]]; then
  log "Prepared in maintenance mode. Edit $PREFIX/config.yaml, provider.env and runtime.env, then start the maintenance provider with:"
  echo "  cd $PREFIX/install/super-light && docker-compose up -d provider"
  exit 0
fi

log "Starting super-light maintenance endpoint"
SERVICES=(provider)
[[ $WITH_OPENWEBUI -eq 1 ]] && SERVICES+=(openwebui)
[[ $WITH_PROXY -eq 1 ]] && SERVICES+=(proxy)
compose up -d --remove-orphans "${SERVICES[@]}"

log "Waiting for maintenance provider"
PROVIDER_READY=0
for attempt in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8766/live >/dev/null 2>&1; then
    PROVIDER_READY=1
    break
  fi
  if [[ $attempt -eq 1 || $((attempt % 10)) -eq 0 ]]; then
    printf '  Maintenance provider: waiting (attempt %d/60)\n' "$attempt"
  fi
  sleep 2
done
if [[ $PROVIDER_READY -ne 1 ]]; then
  echo "Maintenance provider did not become live" >&2
  compose logs --tail=100 provider >&2 || true
  exit 1
fi
printf '  Maintenance provider: ready\n'

cat <<DONE

Super-light installation profile installed.

Local services:
  API:        stopped until maintenance mode is disabled
  Provider:   http://127.0.0.1:8766 (maintenance)
  Neo4j:      prepared; normal stack start after configuration
  Playwright: $([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo "prepared; normal stack start after configuration" || echo "disabled/not installed")
  OpenWebUI:  $([[ $WITH_OPENWEBUI -eq 1 ]] && echo http://127.0.0.1:3000 || echo disabled)
  nginx:      $([[ $WITH_PROXY -eq 1 ]] && echo "enabled on ${PROXY_HTTP_PORT}/${PROXY_HTTPS_PORT}" || echo disabled)
  CA trust:   $([[ -d "$PREFIX/runtime/ca" ]] && find "$PREFIX/runtime/ca" -maxdepth 1 -name "*.crt" -type f 2>/dev/null | wc -l || echo 0) private certificate(s) baked into API/provider image
  X509 strict: $([[ $X509_STRICT -eq 1 ]] && echo enabled || echo disabled-compatibility-mode)

Generated credentials (store them now; both are also in $PREFIX/runtime.env):
  Admin user:       ${ADMIN_USER}
  Admin password:   ${ADMIN_PASSWORD}
  Provider API key: ${PROVIDER_API_KEY}

Before real use:
  Configure LLM backend/model routing in:
    $PREFIX/provider.env
  Keep LLM/API credentials and other secrets in:
    $PREFIX/runtime.env

  Answer and retrieval quality depend materially on the selected LLMs and
  available compute. Undersized local models can make incorrect relevance,
  entity-resolution or evidence decisions. Validate the selected models on
  representative queries and documents before production use.

  LLM_API_KEY is the default LLM credential. Optional role-specific keys
  (PLANNER_LLM_API_KEY, VERIFIER_LLM_API_KEY, EVIDENCE_LLM_API_KEY,
  ANSWER_LLM_API_KEY) override it only when set; otherwise the role falls
  back to LLM_API_KEY.

  On an installed Dockerized system, changes to provider.env or runtime.env
  require environment rematerialization and container recreation. Do not use
  docker-compose restart for these changes; use maintenance-mode.sh on followed
  by maintenance-mode.sh off.

Review infrastructure/security settings in:
  $PREFIX/config.yaml
and optional Web Research settings only when enabled:
  $PREFIX/web.yaml

If the bundled reverse proxy is enabled, replace the generated bootstrap TLS
credentials with the server certificate and private key for this host:
  $PREFIX/install/nginx/tls/server.crt
  $PREFIX/install/nginx/tls/server.key
Then restart the proxy service from $PREFIX/install/super-light with the Compose
command available on the host (docker compose restart proxy or
docker-compose restart proxy).

If you intend to use the SunaQ Nextcloud app, copy:
  $PREFIX/clients/nextcloud/sunaq
to the appropriate Nextcloud app directory (usually <nextcloud-root>/apps/sunaq),
then enable it from the Nextcloud root as the web-server user:
  sudo -u <web-user> php occ app:enable sunaq

In Nextcloud, open Settings -> Administration -> Additional settings and enter:
  SunaQ URL:  the public SunaQ base URL (without /v1)
  API-Key:    the Provider API key generated by this installer

If you use OpenWebUI or another OpenAI-compatible client, configure SunaQ as an
additional provider using the SunaQ /v1 endpoint and a Trusted Client API key.
Make sure the client forwards a stable user identity in the supported header
(for example X-OpenWebUI-User-Id for OpenWebUI). See the trusted-client and
alternative-frontend sections in docs/TECHNICAL-REFERENCE.md for details.

After configuration/verification, leave maintenance mode and start the normal stack:
  sudo $PREFIX/install/maintenance-mode.sh off

Then run the bundled smoke test:
  sudo $PREFIX/install/smoke-test.sh $PREFIX

For a Super-Light service overview, you can also run:
  $PREFIX/install/super-light/status-super-light.sh
DONE
