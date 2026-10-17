#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$BASE_DIR/run"

DEPLOYMENT_MODE=""
STATE_FILE="$BASE_DIR/install/install-state.env"
if [[ -f "$STATE_FILE" ]]; then
  DEPLOYMENT_MODE="$(sed -n 's/^DEPLOYMENT_MODE=//p' "$STATE_FILE" | tail -1)"
fi
if [[ -z "$DEPLOYMENT_MODE" && -f "$BASE_DIR/.sunaq-installation" ]]; then
  DEPLOYMENT_MODE="$(sed -n 's/^DEPLOYMENT_MODE=//p' "$BASE_DIR/.sunaq-installation" | tail -1)"
fi
[[ "$DEPLOYMENT_MODE" == "dockerized" ]] || DEPLOYMENT_MODE="native"

NATIVE_ONLY=0
case "${1:-}" in
  "") ;;
  --native-only) NATIVE_ONLY=1 ;;
  *)
    echo "Usage: $0 [--native-only]" >&2
    exit 2
    ;;
esac

stop_one() {
  local name="$1" pidfile="$RUN_DIR/$1.pid"
  if [[ ! -f "$pidfile" ]]; then
    printf '%-14s not running (no pidfile)\n' "$name"
    return 0
  fi
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    printf '%-14s not running\n' "$name"
    rm -f "$pidfile"
    return 0
  fi
  kill "$pid"
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$name did not stop after 6s; sending KILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  printf '%-14s stopped\n' "$name"
}

stop_dockerized_stack() {
  [[ -f "$BASE_DIR/install/dockerized/docker-compose.yml" ]] || {
    echo "Dockerized compose file not found: $BASE_DIR/install/dockerized/docker-compose.yml" >&2
    return 1
  }
  local compose=()
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  else
    echo "Docker Compose not available; Dockerized SunaQ was not stopped." >&2
    return 1
  fi
  (
    cd "$BASE_DIR/install/dockerized"
    "${compose[@]}" down
  )
  printf '%-14s stopped\n' docker-stack
}


if [[ "$DEPLOYMENT_MODE" == "dockerized" && $NATIVE_ONLY -eq 0 ]]; then
  stop_dockerized_stack
  exit 0
fi

stop_one mail-worker
stop_one sync-worker
stop_one graph-worker
stop_one provider
stop_one api

if [[ $NATIVE_ONLY -eq 1 ]]; then
  exit 0
fi

stop_compose_stack() {
  local compose_file="$BASE_DIR/install/docker-compose.yml"
  [[ -f "$compose_file" ]] || return 0

  local compose=()
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  else
    echo "Docker Compose not available; local container services were not stopped." >&2
    return 0
  fi

  local compose_args=(-f docker-compose.yml)
  [[ -f "$BASE_DIR/install/.env" ]] && compose_args+=(--env-file .env)

  # Services behind Compose profiles are invisible to a plain "compose down".
  # Activate every bundled profile so stop-all really stops the whole Standard
  # installation. Named volumes are deliberately preserved.
  (
    cd "$BASE_DIR/install"
    "${compose[@]}" "${compose_args[@]}" \
      --profile qdrant \
      --profile neo4j \
      --profile resolver \
      --profile renderer \
      --profile ui \
      down
  )
  printf '%-14s stopped\n' docker-stack
}

stop_compose_stack

