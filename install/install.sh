#!/usr/bin/env bash
set -euo pipefail

# Single public installer entry point.
#
# Architecture boundary, capability preset and deployment mechanism are independent axes:
#   --architecture-tier src|erg-internal|erg-full -> normative trust/lifecycle boundary
#   --preset core|workgroup|full                   -> recommended capability combination
#   --deployment standard|dockerized               -> host/native vs containerized runtime
#
# Historical --profile switches remain compatibility aliases only.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf -v SUNAQ_INSTALL_INVOCATION '%q ' "$0" "$@"
SUNAQ_INSTALL_INVOCATION="${SUNAQ_INSTALL_INVOCATION% }"
AKI_INSTALL_INVOCATION="$SUNAQ_INSTALL_INVOCATION"
export SUNAQ_INSTALL_INVOCATION AKI_INSTALL_INVOCATION

PROFILE=""
PROFILE_EXPLICIT=0
DEPLOYMENT=""
DEPLOYMENT_EXPLICIT=0
PRESET_NAME=""
PRESET_FILE_EXPLICIT=0
ARCHITECTURE_TIER=""
ARCHITECTURE_TIER_EXPLICIT=0
FORWARD=()

normalize_architecture_tier() {
  local value="${1,,}"
  case "$value" in
    src) printf 'src' ;;
    erg-internal|erg_internal) printf 'erg_internal' ;;
    erg-full|erg_full|erg) printf 'erg_full' ;;
    *) return 1 ;;
  esac
}

usage() {
  cat <<'USAGE'
Usage: sudo ./install/install.sh [--deployment MODE] [--architecture-tier TIER] [--preset PRESET] [options]

Preferred independent axes:
  --architecture-tier src|erg-internal|erg-full
                                normative architecture/trust boundary
  --preset core|workgroup|full
                                shortcut for a recommended tier + capability set
  --deployment standard|dockerized
                                deployment mechanism (default: standard)
  --llm local|openai            canonical LLM routing (fresh default: local)

Compatibility aliases:
  --profile standard            alias for --deployment standard
  --profile super-light         alias for --deployment dockerized --preset core
  --super-light                 same compatibility alias as above
  --deployment native           legacy alias for --deployment standard

Examples:
  sudo ./install/install.sh --deployment standard --preset core --plan
  sudo ./install/install.sh --deployment standard --preset workgroup --plan
  sudo ./install/install.sh --deployment standard --architecture-tier erg-internal --with-neo4j --plan
  sudo ./install/install.sh --deployment dockerized --preset core --plan \
    --nextcloud-url https://cloud.example.org/nextcloud \
    --elasticsearch-url http://127.0.0.1:9200
  sudo ./install/install.sh --deployment dockerized --preset full --plan \
    --nextcloud-url https://cloud.example.org/nextcloud \
    --elasticsearch-url http://127.0.0.1:9200

For deployment-specific options:
  ./install/install.sh --deployment standard --help
  ./install/install.sh --deployment dockerized --help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo "--profile requires a value" >&2; exit 2; }
      PROFILE="$2"; PROFILE_EXPLICIT=1; shift 2 ;;
    --profile=*)
      PROFILE="${1#*=}"; PROFILE_EXPLICIT=1; shift ;;
    --super-light)
      PROFILE="super-light"; PROFILE_EXPLICIT=1; shift ;;
    --deployment)
      [[ $# -ge 2 ]] || { echo "--deployment requires a value" >&2; exit 2; }
      DEPLOYMENT="$2"; DEPLOYMENT_EXPLICIT=1; shift 2 ;;
    --deployment=*)
      DEPLOYMENT="${1#*=}"; DEPLOYMENT_EXPLICIT=1; shift ;;
    --architecture-tier|--architecture_tier)
      [[ $# -ge 2 ]] || { echo "--architecture-tier requires src, erg-internal or erg-full" >&2; exit 2; }
      ARCHITECTURE_TIER="$(normalize_architecture_tier "$2")" || { echo "Unknown architecture tier: $2" >&2; exit 2; }
      ARCHITECTURE_TIER_EXPLICIT=1
      FORWARD+=(--architecture-tier "$ARCHITECTURE_TIER")
      shift 2 ;;
    --architecture-tier=*|--architecture_tier=*)
      raw_tier="${1#*=}"
      ARCHITECTURE_TIER="$(normalize_architecture_tier "$raw_tier")" || { echo "Unknown architecture tier: $raw_tier" >&2; exit 2; }
      ARCHITECTURE_TIER_EXPLICIT=1
      FORWARD+=(--architecture-tier "$ARCHITECTURE_TIER")
      shift ;;
    --preset)
      [[ $# -ge 2 ]] || { echo "--preset requires core, workgroup or full" >&2; exit 2; }
      PRESET_NAME="$2"
      FORWARD+=("$1" "$2")
      shift 2 ;;
    --preset=*)
      PRESET_NAME="${1#*=}"
      FORWARD+=(--preset "$PRESET_NAME")
      shift ;;
    --preset-file)
      [[ $# -ge 2 ]] || { echo "--preset-file requires a file" >&2; exit 2; }
      PRESET_FILE_EXPLICIT=1
      FORWARD+=("$1" "$2")
      shift 2 ;;
    --preset-file=*)
      PRESET_FILE_EXPLICIT=1
      FORWARD+=(--preset-file "${1#*=}")
      shift ;;
    --profiles)
      printf 'standard (compatibility alias)\nsuper-light (compatibility alias: dockerized + core)\n'
      exit 0 ;;
    -h|--help)
      if [[ $PROFILE_EXPLICIT -eq 0 && $DEPLOYMENT_EXPLICIT -eq 0 ]]; then
        usage
        exit 0
      fi
      FORWARD+=("$1"); shift ;;
    *)
      FORWARD+=("$1"); shift ;;
  esac
done

case "$PROFILE" in
  "") ;;
  standard|default) PROFILE="standard" ;;
  super-light|superlight) PROFILE="super-light" ;;
  *)
    echo "Unknown compatibility profile: $PROFILE" >&2
    usage >&2
    exit 2 ;;
esac

case "$PRESET_NAME" in
  ""|core|workgroup|full) ;;
  *) echo "Unknown preset: $PRESET_NAME" >&2; exit 2 ;;
esac

if [[ -z "$DEPLOYMENT" ]]; then
  if [[ "$PROFILE" == "super-light" ]]; then
    DEPLOYMENT="dockerized"
  else
    DEPLOYMENT="standard"
  fi
fi
case "$DEPLOYMENT" in
  standard|native) DEPLOYMENT="standard" ;;
  dockerized) ;;
  *) echo "Unknown deployment mode: $DEPLOYMENT" >&2; exit 2 ;;
esac

# Compatibility aliases intentionally preserve their historical meaning.
# New installations should use independent --deployment/--architecture-tier/--preset axes.
if [[ "$PROFILE" == "standard" && "$DEPLOYMENT" != "standard" ]]; then
  echo "--profile standard is a compatibility alias for --deployment standard; do not combine it with --deployment dockerized." >&2
  exit 2
fi
if [[ "$PROFILE" == "super-light" ]]; then
  if [[ "$DEPLOYMENT" != "dockerized" ]]; then
    echo "--profile super-light is a compatibility alias for --deployment dockerized --preset core." >&2
    exit 2
  fi
  if [[ $PRESET_FILE_EXPLICIT -eq 1 ]]; then
    echo "--profile super-light fixes the legacy alias to the core/SRC preset; use --deployment dockerized --preset-file ... for a custom capability preset." >&2
    exit 2
  fi
  if [[ -n "$PRESET_NAME" && "$PRESET_NAME" != "core" ]]; then
    echo "--profile super-light means dockerized + core/SRC. Use --deployment dockerized --preset $PRESET_NAME instead." >&2
    exit 2
  fi
  if [[ -z "$PRESET_NAME" ]]; then
    PRESET_NAME="core"
    FORWARD+=(--preset core)
  fi
fi

if [[ $ARCHITECTURE_TIER_EXPLICIT -eq 1 && -n "$PRESET_NAME" ]]; then
  case "$PRESET_NAME" in
    core) preset_tier="src" ;;
    workgroup) preset_tier="erg_internal" ;;
    full) preset_tier="erg_full" ;;
    *) preset_tier="" ;;
  esac
  if [[ -n "$preset_tier" && "$preset_tier" != "$ARCHITECTURE_TIER" ]]; then
    echo "Preset '$PRESET_NAME' selects architecture tier '$preset_tier', but --architecture-tier '$ARCHITECTURE_TIER' was requested." >&2
    echo "Use a matching tier or configure capabilities without that preset." >&2
    exit 2
  fi
fi

case "$DEPLOYMENT" in
  standard)
    exec "$SCRIPT_DIR/profiles/install-standard.sh" "${FORWARD[@]}"
    ;;
  dockerized)
    exec "$SCRIPT_DIR/profiles/install-dockerized.sh" "${FORWARD[@]}"
    ;;
esac
