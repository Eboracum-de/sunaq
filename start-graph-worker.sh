#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

source "$(pwd)/install/load-service-env.sh" "$(pwd)"

exec ./.venv/bin/python -m rag.graph_worker "$@"
