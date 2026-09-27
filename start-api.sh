#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

source "$(pwd)/install/load-service-env.sh" "$(pwd)"

RAG_API_BIND="${RAG_API_HOST:-127.0.0.1}"
case "$RAG_API_BIND" in
  127.0.0.1|::1|localhost) ;;
  *)
    case "${RAG_ALLOW_REMOTE_INTERNAL_API:-false}" in
      1|true|TRUE|yes|YES|on|ON) ;;
      *)
        echo "Refusing non-loopback RAG_API_HOST=$RAG_API_BIND without RAG_ALLOW_REMOTE_INTERNAL_API=true." >&2
        echo "The middleware API is an internal trust boundary; expose /v1 or a protected reverse-proxy path instead." >&2
        exit 2
        ;;
    esac
    ;;
esac

exec ./.venv/bin/python -m uvicorn rag.api:app \
  --host "$RAG_API_BIND" \
  --port "${RAG_API_PORT:-8765}"
