#!/usr/bin/env bash
set -euo pipefail
cat >&2 <<'MSG'
Dockerized core/SRC and has no Neo4j/CardDAV
resolver state. Contact seed synchronization is an ERG Internal capability.
Use the standard installer with --preset workgroup (or --preset full) for contacts.
MSG
exit 2
