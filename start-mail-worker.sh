#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/install/load-service-env.sh" "$BASE_DIR"

if [[ -n "${RAG_PYTHON:-}" ]]; then
  PYTHON_BIN="$RAG_PYTHON"
elif [[ -x "$BASE_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$BASE_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "No Python interpreter found (set RAG_PYTHON or install python3)." >&2
  exit 127
fi

exec "$PYTHON_BIN" -m rag.mail_worker --config "${RAG_CONFIG:-$BASE_DIR/config.yaml}"
