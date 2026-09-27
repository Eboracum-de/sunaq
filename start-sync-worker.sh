#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source "$(pwd)/install/load-service-env.sh" "$(pwd)"

read_worker_config() {
  ./.venv/bin/python - <<'PY'
import yaml
try:
    with open('config.yaml', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    worker = cfg.get('sync_worker') or {}
    qdrant = cfg.get('qdrant') or {}
    enabled = bool(worker.get('enabled', True)) and bool(qdrant.get('enabled', True))
    interval = max(60, int(worker.get('poll_interval_seconds') or 300))
    max_documents = max(0, int(worker.get('max_documents') or 0))
    enqueue_graph = bool(worker.get('enqueue_graph', False))
    print('1' if enabled else '0')
    print(interval)
    print(max_documents)
    print('1' if enqueue_graph else '0')
except Exception:
    print('0')
    print(300)
    print(0)
    print('0')
PY
}

while true; do
  mapfile -t values < <(read_worker_config)
  enabled="${values[0]:-0}"
  interval="${values[1]:-300}"
  max_documents="${values[2]:-0}"
  enqueue_graph="${values[3]:-0}"

  if [[ "$enabled" == "1" ]]; then
    args=(--config config.yaml --max-documents "$max_documents")
    if [[ "$enqueue_graph" == "1" ]]; then
      args+=(--enqueue-graph)
    else
      args+=(--no-enqueue-graph)
    fi
    if ! ./.venv/bin/python -m rag.sync "${args[@]}"; then
      echo "$(date -Is) ES->Qdrant sync run failed; retrying after interval" >&2
    fi
  else
    echo "$(date -Is) sync_worker/qdrant disabled; no ES->Qdrant sync run" >&2
  fi
  sleep "$interval"
done
