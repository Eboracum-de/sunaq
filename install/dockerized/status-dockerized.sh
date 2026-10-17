#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper only.  Operational status is deployment-independent and
# lives in the common top-level status.sh entrypoint.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec "$PREFIX/status.sh" "$@"
