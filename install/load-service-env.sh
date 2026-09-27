#!/usr/bin/env bash
# Source the generated non-secret runtime environment plus *_FILE secret paths.
# This file is intended to be sourced by SunaQ native launch scripts.

SUNAQ_ENV_DIR="${1:-$(pwd)}"

if [[ -f "$SUNAQ_ENV_DIR/runtime.service.env" ]]; then
  set -a
  source "$SUNAQ_ENV_DIR/runtime.service.env"
  set +a
  return 0 2>/dev/null || exit 0
fi

# Upgrade fallback only. A rerun of the rc1.2 installer generates
# runtime.service.env and removes the need to source plaintext compatibility
# files during normal service startup.
echo "WARNING: runtime.service.env missing; using legacy plaintext environment. Rerun the installer." >&2
set -a
[[ -f "$SUNAQ_ENV_DIR/provider.env" ]] && source "$SUNAQ_ENV_DIR/provider.env"
[[ -f "$SUNAQ_ENV_DIR/runtime.env" ]] && source "$SUNAQ_ENV_DIR/runtime.env"
set +a
