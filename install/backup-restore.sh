#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="$BASE_DIR/runtime.env"
STATE_FILE="$BASE_DIR/install/install-state.env"
MARKER_FILE="$BASE_DIR/.sunaq-installation"
LEGACY_MARKER_FILE="$BASE_DIR/.aki-rag-installation"
FORMAT="aki-rag-backup-v1"
if [[ ! -f "$MARKER_FILE" && -f "$LEGACY_MARKER_FILE" ]]; then
  MARKER_FILE="$LEGACY_MARKER_FILE"
fi

usage() {
  cat >&2 <<'USAGE'
Usage:
  backup-restore.sh create TARGET
  backup-restore.sh verify BACKUP
  backup-restore.sh restore BACKUP --yes

TARGET is a destination directory. create writes a timestamped recovery set below
it. create/restore require root and SunaQ maintenance mode. verify is read-only.

Scope:
  included: SunaQ config/runtime secrets, administrator-owned model profiles,
            credential master key, non-Qdrant SunaQ SQLite state,
            private CA/TLS/operator state, bundled Neo4j when selected
  excluded: Nextcloud, Elasticsearch, Qdrant and its rebuildable sync state,
            external Neo4j, OpenWebUI/Playwright state, model caches
USAGE
  exit 2
}

env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -1
}

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "This operation must run as root (use sudo)." >&2
    exit 1
  fi
}

require_maintenance() {
  [[ -f "$RUNTIME_ENV" ]] || {
    echo "runtime.env not found: $RUNTIME_ENV" >&2
    exit 1
  }
  local value
  value="$(env_value "$RUNTIME_ENV" RAG_MAINTENANCE_MODE)"
  if ! is_true "$value"; then
    echo "SunaQ maintenance mode is required." >&2
    echo "Run: sudo $BASE_DIR/install/maintenance-mode.sh on" >&2
    exit 1
  fi
}

set_runtime_maintenance_true() {
  if grep -q '^RAG_MAINTENANCE_MODE=' "$RUNTIME_ENV"; then
    sed -i 's/^RAG_MAINTENANCE_MODE=.*/RAG_MAINTENANCE_MODE=true/' "$RUNTIME_ENV"
  else
    printf '\nRAG_MAINTENANCE_MODE=true\n' >> "$RUNTIME_ENV"
  fi
  chmod 600 "$RUNTIME_ENV"
}

deployment_profile="standard"
deployment_mode="native"
local_neo4j="0"
if [[ -f "$STATE_FILE" ]]; then
  value="$(env_value "$STATE_FILE" DEPLOYMENT_PROFILE)"
  [[ -n "$value" ]] && deployment_profile="$value"
  value="$(env_value "$STATE_FILE" DEPLOYMENT_MODE)"
  [[ -n "$value" ]] && deployment_mode="$value"
  value="$(env_value "$STATE_FILE" LOCAL_NEO4J)"
  [[ -n "$value" ]] && local_neo4j="$value"
fi
if [[ -f "$MARKER_FILE" ]]; then
  value="$(env_value "$MARKER_FILE" DEPLOYMENT_PROFILE)"
  [[ -n "$value" ]] && deployment_profile="$value"
  value="$(env_value "$MARKER_FILE" DEPLOYMENT_MODE)"
  [[ -n "$value" ]] && deployment_mode="$value"
fi
if [[ "$deployment_mode" != "dockerized" && "$deployment_profile" == "super-light" ]]; then
  deployment_mode="dockerized"
fi

compose_bin=()
compose_detect() {
  if [[ ${#compose_bin[@]} -gt 0 ]]; then
    return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    compose_bin=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    compose_bin=(docker-compose)
  else
    echo "Docker Compose not found." >&2
    return 1
  fi
}

compose_neo4j() {
  compose_detect
  if [[ "$deployment_mode" == "dockerized" ]]; then
    (
      cd "$BASE_DIR/install/dockerized"
      "${compose_bin[@]}" "$@"
    )
  else
    (
      cd "$BASE_DIR/install"
      "${compose_bin[@]}" -f docker-compose.yml --env-file .env --profile neo4j "$@"
    )
  fi
}

provider_image_id() {
  compose_detect
  local container_id image_id
  container_id="$(
    cd "$BASE_DIR/install/dockerized"
    "${compose_bin[@]}" ps -q provider | head -1
  )"
  [[ -n "$container_id" ]] || {
    echo "Provider container not found; enter SunaQ maintenance mode before backup/restore." >&2
    return 1
  }
  image_id="$(docker inspect --format '{{.Image}}' "$container_id" 2>/dev/null || true)"
  [[ -n "$image_id" ]] || {
    echo "Could not determine the provider image for backup helper execution." >&2
    return 1
  }
  printf '%s\n' "$image_id"
}

dockerized_backup_helper() {
  local mount_source="$1" mount_target="$2"
  shift 2
  local image_id
  image_id="$(provider_image_id)" || return $?

  # Do not use `docker-compose run provider` here.  Legacy docker-compose v1
  # injects links to running project containers for `run`; that is incompatible
  # with the provider's host network mode and fails before backup_admin starts.
  # backup_admin is a local filesystem/SQLite verifier and needs no network.
  docker run --rm --network none --user 0:0 \
    --env-file "$BASE_DIR/provider.env" \
    --env-file "$BASE_DIR/runtime.env" \
    -v "$BASE_DIR/config.yaml:/app/config.yaml:ro,z" \
    -v "$BASE_DIR/web.yaml:/app/web.yaml:ro,z" \
    -v "$BASE_DIR/runtime:/app/runtime:rw,z" \
    -v "$BASE_DIR/rag:/app/rag:ro,z" \
    -v "$mount_source:$mount_target:rw,z" \
    "$image_id" "$@"
}

helper_root() {
  local command="$1"
  local extra=(--source-prefix "$BASE_DIR")
  if [[ "$deployment_mode" == "dockerized" ]]; then
    dockerized_backup_helper "$BASE_DIR" /host-root \
      python -m rag.backup_admin "$command" --root /host-root "${extra[@]}"
  else
    [[ -x "$BASE_DIR/.venv/bin/python" ]] || {
      echo "SunaQ virtualenv Python missing: $BASE_DIR/.venv/bin/python" >&2
      exit 1
    }
    (
      cd "$BASE_DIR"
      "$BASE_DIR/.venv/bin/python" -m rag.backup_admin "$command" --root "$BASE_DIR" "${extra[@]}"
    )
  fi
}

helper_verify_tree() {
  local tree="$1" source_prefix="$2"
  if [[ "$deployment_mode" == "dockerized" ]]; then
    dockerized_backup_helper "$tree" /verify-root \
      python -m rag.backup_admin verify-root --root /verify-root \
        --source-prefix "$source_prefix"
  else
    (
      cd "$BASE_DIR"
      "$BASE_DIR/.venv/bin/python" -m rag.backup_admin verify-root --root "$tree" \
        --source-prefix "$source_prefix"
    )
  fi
}

current_version() {
  sed -nE 's/^VERSION[[:space:]]*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/p' \
    "$BASE_DIR/rag/version.py" | head -1
}

metadata_value() {
  local backup="$1" key="$2"
  sed -n "s/^${key}=//p" "$backup/METADATA.txt" | tail -1
}

verify_checksums() {
  local backup="$1"
  [[ -f "$backup/MANIFEST.sha256" ]] || {
    echo "Missing MANIFEST.sha256 in $backup" >&2
    return 1
  }
  (
    cd "$backup"
    sha256sum -c MANIFEST.sha256
  )
}

check_tar_paths() {
  local archive="$1" member
  while IFS= read -r member; do
    case "$member" in
      /*|../*|*/../*|*/..)
        echo "Unsafe path in archive $archive: $member" >&2
        return 1 ;;
    esac
  done < <(tar -tf "$archive")
  if tar -tvf "$archive" | awk '
      substr($0, 1, 1) != "-" && substr($0, 1, 1) != "d" { bad=1 }
      END { exit(bad ? 0 : 1) }
    '; then
    echo "Archive contains symlinks, hardlinks or special files and is not accepted: $archive" >&2
    return 1
  fi
}

verify_backup() {
  local backup="$1"
  [[ -d "$backup" ]] || { echo "Backup directory not found: $backup" >&2; return 1; }
  [[ -f "$backup/METADATA.txt" ]] || { echo "Missing METADATA.txt in $backup" >&2; return 1; }
  [[ "$(metadata_value "$backup" FORMAT)" == "$FORMAT" ]] || {
    echo "Unsupported backup format in $backup" >&2
    return 1
  }
  [[ -f "$backup/files.tar" ]] || { echo "Missing files.tar in $backup" >&2; return 1; }

  verify_checksums "$backup"
  check_tar_paths "$backup/files.tar"

  local tmp source_prefix rc
  tmp="$(mktemp -d)"
  chmod 700 "$tmp"
  source_prefix="$(metadata_value "$backup" SOURCE_PREFIX)"
  [[ -n "$source_prefix" ]] || source_prefix="$BASE_DIR"
  rc=0
  tar -xpf "$backup/files.tar" -C "$tmp" || rc=$?
  if [[ $rc -eq 0 ]]; then
    helper_verify_tree "$tmp" "$source_prefix" || rc=$?
  fi
  rm -rf "$tmp"
  [[ $rc -eq 0 ]] || return "$rc"

  if [[ -f "$backup/volumes/neo4j-data.tar" ]]; then
    check_tar_paths "$backup/volumes/neo4j-data.tar"
  fi
  echo "Backup verification OK: $backup"
}

snapshot_neo4j() {
  local destination="$1"
  mkdir -p "$(dirname "$destination")"
  local was_running=0
  if compose_neo4j ps --services --filter status=running 2>/dev/null | grep -qx neo4j; then
    was_running=1
  fi

  if ! compose_neo4j stop neo4j >/dev/null 2>&1; then
    echo "Could not stop bundled Neo4j; refusing an inconsistent snapshot." >&2
    return 1
  fi
  if ! compose_neo4j run --rm -T --no-deps --entrypoint sh neo4j \
      -c 'tar -C /data -cf - .' > "$destination"; then
    [[ $was_running -eq 1 ]] && compose_neo4j start neo4j >/dev/null 2>&1 || true
    echo "Neo4j snapshot failed." >&2
    return 1
  fi
  chmod 600 "$destination"
  if [[ "$was_running" -eq 1 ]]; then
    compose_neo4j start neo4j >/dev/null
  fi
  return 0
}

normalize_restored_ownership() {
  local uid="$1" gid="$2"
  [[ "$deployment_mode" != "dockerized" ]] || return 0
  [[ -n "$uid" && -n "$gid" ]] || return 0

  local path
  for path in config.yaml web.yaml provider.env runtime.env .sunaq-installation .aki-rag-installation; do
    [[ -e "$BASE_DIR/$path" ]] && chown "$uid:$gid" "$BASE_DIR/$path"
  done
  [[ -d "$BASE_DIR/models" ]] && chown -R "$uid:$gid" "$BASE_DIR/models"
  [[ -d "$BASE_DIR/runtime" ]] && chown -R "$uid:$gid" "$BASE_DIR/runtime"
  [[ -f "$BASE_DIR/install/.env" ]] && chown "$uid:$gid" "$BASE_DIR/install/.env"
  [[ -f "$BASE_DIR/install/install-state.env" ]] && chown "$uid:$gid" "$BASE_DIR/install/install-state.env"

  while IFS= read -r path; do
    chown "$uid:$gid" "$path"
  done < <(find "$BASE_DIR" -maxdepth 1 -type f \( -name '*.sqlite' -o -name '*.sqlite-wal' -o -name '*.sqlite-shm' \) -print)

  local key
  key="$(env_value "$RUNTIME_ENV" RAG_CREDENTIAL_MASTER_KEY_FILE)"
  [[ -n "$key" ]] || key="runtime/credential-master.key"
  [[ "$key" == /* ]] || key="$BASE_DIR/$key"
  case "$(realpath -m "$key")" in
    "$BASE_DIR"/*)
      if [[ -f "$key" ]]; then
        chown "root:$gid" "$key"
        chmod 0640 "$key"
      fi
      ;;
  esac
}

drop_qdrant_sync_state() {
  command -v jq >/dev/null 2>&1 || {
    echo "jq is required to normalize Qdrant restore state." >&2
    return 1
  }
  local inventory_json relative
  inventory_json="$(helper_root inventory)"
  relative="$(jq -r '.qdrant_sync_state.relative // empty' <<<"$inventory_json")"

  local candidates=("state.sqlite" "runtime/state.sqlite")
  [[ -n "$relative" ]] && candidates+=("$relative")
  local item
  for item in "${candidates[@]}"; do
    case "$item" in
      ""|/*|../*|*/../*|*/..) continue ;;
    esac
    rm -f "$BASE_DIR/$item" "$BASE_DIR/$item-wal" "$BASE_DIR/$item-shm"
  done
}

restore_neo4j() {
  local source="$1"
  local was_running=0
  if compose_neo4j ps --services --filter status=running 2>/dev/null | grep -qx neo4j; then
    was_running=1
  fi

  if ! compose_neo4j stop neo4j >/dev/null 2>&1; then
    echo "Could not stop bundled Neo4j; refusing to restore its volume." >&2
    return 1
  fi
  if ! compose_neo4j run --rm -T --no-deps --entrypoint sh neo4j \
      -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true; tar -C /data -xf -' \
      < "$source"; then
    [[ $was_running -eq 1 ]] && compose_neo4j start neo4j >/dev/null 2>&1 || true
    echo "Neo4j restore failed; the local Neo4j volume may now be partial. Restore the verified snapshot again before leaving maintenance mode." >&2
    return 1
  fi
  if [[ "$was_running" -eq 1 ]]; then
    compose_neo4j start neo4j >/dev/null
  fi
  return 0
}

create_backup() {
  local target="$1"
  require_root
  require_maintenance
  command -v jq >/dev/null 2>&1 || { echo "jq is required." >&2; exit 1; }
  command -v tar >/dev/null 2>&1 || { echo "tar is required." >&2; exit 1; }
  command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum is required." >&2; exit 1; }
  command -v realpath >/dev/null 2>&1 || { echo "realpath is required." >&2; exit 1; }

  local target_abs base_abs
  target_abs="$(realpath -m "$target")"
  base_abs="$(realpath -m "$BASE_DIR")"
  case "$target_abs" in
    "$base_abs"|"$base_abs"/*)
      echo "Backup target must be outside the SunaQ installation prefix: $BASE_DIR" >&2
      exit 1 ;;
  esac
  mkdir -p "$target_abs"
  chmod 700 "$target_abs"

  helper_root checkpoint-root >/dev/null
  local inventory_json
  inventory_json="$(helper_root inventory)"
  if [[ "$(jq '.blocking_external_paths | length' <<<"$inventory_json")" -ne 0 ]]; then
    echo "Automatic recovery cannot safely include required state outside $BASE_DIR:" >&2
    jq -r '.blocking_external_paths[] | "  \(.kind): \(.path)"' <<<"$inventory_json" >&2
    exit 1
  fi
  if [[ "$(jq '.external_paths | length' <<<"$inventory_json")" -ne 0 ]]; then
    echo "[WARN] SunaQ-referenced paths outside the installation prefix are not included:" >&2
    jq -r '.external_paths[] | "  \(.kind): \(.path)"' <<<"$inventory_json" >&2
  fi

  local timestamp name partial final
  timestamp="$(date -u +%Y%m%d-%H%M%SZ)"
  name="sunaq-backup-$timestamp"
  partial="$target_abs/.${name}.partial"
  final="$target_abs/$name"
  rm -rf "$partial"
  mkdir -p "$partial"
  chmod 700 "$partial"

  local list_file
  list_file="$(mktemp)"
  trap 'rm -f "${list_file:-}"; rm -rf "${partial:-}"' EXIT
  jq -r '.archive_paths[]' <<<"$inventory_json" > "$list_file"
  (
    cd "$BASE_DIR"
    tar -cpf "$partial/files.tar" --verbatim-files-from -T "$list_file"
  )
  chmod 600 "$partial/files.tar"

  local neo4j_included=0
  if is_true "$local_neo4j"; then
    echo "Creating bundled Neo4j snapshot ..."
    snapshot_neo4j "$partial/volumes/neo4j-data.tar"
    neo4j_included=1
  else
    echo "[WARN] Bundled Neo4j is not selected. External Neo4j, if used, is not part of this recovery set." >&2
  fi

  cat > "$partial/METADATA.txt" <<EOF
FORMAT=$FORMAT
CREATED_AT_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SUNAQ_VERSION=$(current_version)
AKI_VERSION=$(current_version)
SOURCE_PREFIX=$BASE_DIR
DEPLOYMENT_PROFILE=$deployment_profile
DEPLOYMENT_MODE=$deployment_mode
NEO4J_INCLUDED=$neo4j_included
QDRANT_INCLUDED=0
QDRANT_SYNC_STATE_INCLUDED=0
NEXTCLOUD_INCLUDED=0
ELASTICSEARCH_INCLUDED=0
EOF
  chmod 600 "$partial/METADATA.txt"

  (
    cd "$partial"
    {
      sha256sum METADATA.txt files.tar
      [[ -f volumes/neo4j-data.tar ]] && sha256sum volumes/neo4j-data.tar
    } > MANIFEST.sha256
  )
  chmod 600 "$partial/MANIFEST.sha256"

  verify_backup "$partial" >/dev/null
  mv "$partial" "$final"
  rm -f "$list_file"
  partial=""
  list_file=""
  trap - EXIT

  echo "Backup created and verified: $final"
  echo "Contains sensitive secrets and the credential master key; keep it protected."
}

restore_backup() {
  local backup="$1" confirmation="${2:-}"
  require_root
  require_maintenance
  [[ "$confirmation" == "--yes" ]] || {
    echo "Restore is destructive. Re-run with --yes after verifying the selected recovery set." >&2
    exit 2
  }

  backup="$(realpath -m "$backup")"
  verify_backup "$backup"

  local backup_profile backup_mode backup_prefix target_runtime_uid target_runtime_gid
  target_runtime_uid="$(stat -c '%u' "$BASE_DIR/runtime" 2>/dev/null || true)"
  target_runtime_gid="$(stat -c '%g' "$BASE_DIR/runtime" 2>/dev/null || true)"
  backup_profile="$(metadata_value "$backup" DEPLOYMENT_PROFILE)"
  backup_mode="$(metadata_value "$backup" DEPLOYMENT_MODE)"
  backup_prefix="$(metadata_value "$backup" SOURCE_PREFIX)"
  if [[ -n "$backup_profile" && "$backup_profile" != "$deployment_profile" ]]; then
    echo "Backup profile $backup_profile does not match this installation ($deployment_profile)." >&2
    exit 1
  fi
  if [[ -n "$backup_mode" && "$backup_mode" != "$deployment_mode" ]]; then
    echo "Backup deployment mode $backup_mode does not match this installation ($deployment_mode)." >&2
    exit 1
  fi
  if [[ -n "$backup_prefix" && "$(realpath -m "$backup_prefix")" != "$(realpath -m "$BASE_DIR")" ]]; then
    echo "Backup source prefix $backup_prefix differs from this installation prefix $BASE_DIR." >&2
    echo "RC5 restore intentionally does not rewrite absolute deployment paths; prepare the same prefix first." >&2
    exit 1
  fi

  local source_version now_version
  source_version="$(metadata_value "$backup" SUNAQ_VERSION)"
  [[ -n "$source_version" ]] || source_version="$(metadata_value "$backup" AKI_VERSION)"
  now_version="$(current_version)"
  if [[ -n "$source_version" && -n "$now_version" && "$source_version" != "$now_version" ]]; then
    echo "[WARN] Backup version $source_version differs from installed code $now_version." >&2
    echo "[WARN] Restore will continue; remain in maintenance mode until schema/smoke checks pass." >&2
  fi

  # Prevent stale WAL/SHM sidecars from being paired with restored main DBs.
  local member
  while IFS= read -r member; do
    case "$member" in
      *.sqlite)
        rm -f "$BASE_DIR/$member" "$BASE_DIR/$member-wal" "$BASE_DIR/$member-shm" ;;
    esac
  done < <(tar -tf "$backup/files.tar")

  tar -xpf "$backup/files.tar" -C "$BASE_DIR"
  set_runtime_maintenance_true
  normalize_restored_ownership "$target_runtime_uid" "$target_runtime_gid"

  if ! is_true "$(metadata_value "$backup" QDRANT_INCLUDED)"; then
    # Qdrant vectors are derived and not part of the recovery set. Drop any
    # restored legacy sync database so the next rebuild cannot falsely classify
    # documents as unchanged against a missing/different vector collection.
    drop_qdrant_sync_state
  fi

  if is_true "$(metadata_value "$backup" NEO4J_INCLUDED)"; then
    [[ -f "$backup/volumes/neo4j-data.tar" ]] || {
      echo "Backup metadata requires Neo4j, but the volume archive is missing." >&2
      exit 1
    }
    if ! is_true "$local_neo4j"; then
      echo "Backup contains bundled Neo4j but this installation is not configured for bundled Neo4j." >&2
      echo "Refusing to write the snapshot into an unknown deployment." >&2
      exit 1
    fi
    echo "Restoring bundled Neo4j snapshot ..."
    restore_neo4j "$backup/volumes/neo4j-data.tar"
  fi

  helper_root verify-root >/dev/null
  "$BASE_DIR/install/maintenance-mode.sh" on >/dev/null

  echo "Restore completed and local recovery checks passed."
  echo "SunaQ remains in maintenance mode."
  if ! is_true "$(metadata_value "$backup" QDRANT_INCLUDED)"; then
    echo "Qdrant is not part of this recovery set; its sync state was reset."
    echo "Restore/reset the Qdrant collection as appropriate and run a full ES -> Qdrant sync before leaving maintenance."
  fi
  echo "Run deployment smoke/ACL checks, then: sudo $BASE_DIR/install/maintenance-mode.sh off"
}

case "${1:-}" in
  create)
    [[ $# -eq 2 ]] || usage
    create_backup "$2"
    ;;
  verify)
    [[ $# -eq 2 ]] || usage
    verify_backup "$(realpath -m "$2")"
    ;;
  restore)
    [[ $# -eq 3 ]] || usage
    restore_backup "$2" "$3"
    ;;
  *)
    usage
    ;;
esac
