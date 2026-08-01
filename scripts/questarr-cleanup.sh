#!/usr/bin/env bash
##############################################################################
# questarr-cleanup.sh — Clean up Questarr indexers/downloaders.
#
# Questarr is being retired and replaced by the RomGoGetter indexer
# (Torznab + Transmission RPC, single binary, on the LAN).
#
# Two cleanup paths:
#   1. API mode (preferred): DELETE /api/indexers/:id and /api/downloaders/:id
#      Reads every indexer, deletes everything except 'RomGoGetter' (case-insensitive).
#      Deletes any downloader of type 'qbittorrent' (or matching '<name>'-qBittorrent).
#
#   2. DB mode (fallback when Questarr is offline):
#      Locates the SQLite database (likely a PVC mount) and runs DELETE
#      statements directly. The DB schema is at server/migrations/0000_init_sqlite.sql.
#
# Usage:
#   questarr-cleanup.sh api http://192.168.168.X:3000
#   questarr-cleanup.sh db /var/lib/questarr/data/questarr.db
#   questarr-cleanup.sh discover                # print user-friendly guess
#
# Environment variables:
#   QUESTARR_API_URL    Base URL for Questarr (e.g. http://host:3000)
#   QUESTARR_DB_PATH    Filesystem path to the SQLite DB
#   KEEP_INDEXER_NAME   Indexer name to keep (default: RomGoGetter)
##############################################################################
set -euo pipefail

LOG_PREFIX="[questarr-cleanup]"

log()  { echo "${LOG_PREFIX} $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

cmd="${1:-discover}"
k3s_yaml="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

##
## 1. API mode
##
api_cleanup() {
  local base="$1"
  log "API mode: ${base}"

  # Step 1: list indexers
  log "Listing indexers..."
  local indexers
  indexers=$(curl -sS "${base}/api/indexers")
  log "raw: $(echo "$indexers" | head -c 200)..."

  local count
  count=$(echo "$indexers" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  log "Found ${count} indexers"

  local i=0
  while [ "$i" -lt "$count" ]; do
    local id name
    id=$(echo "$indexers" | python3 -c "import json,sys; print(json.load(sys.stdin)[${i}]['id'])")
    name=$(echo "$indexers" | python3 -c "import json,sys; print(json.load(sys.stdin)[${i}]['name'])")
    # Skip if name matches KEEP_INDEXER_NAME (case-insensitive)
    if [ "${name,,}" = "${KEEP_INDEXER_NAME:-romgogetter,,}" ]; then
      log "  keeping: ${name} (${id})"
      i=$((i+1))
      continue
    fi
    log "  deleting: ${name} (${id})"
    curl -sS -X DELETE "${base}/api/indexers/${id}" -o /dev/null -w "    HTTP %{http_code}\n"
    i=$((i+1))
  done

  # Step 2: list downloaders
  log "Listing downloaders..."
  local downloaders
  downloaders=$(curl -sS "${base}/api/downloaders")
  log "raw: $(echo "$downloaders" | head -c 200)..."

  count=$(echo "$downloaders" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  log "Found ${count} downloaders"

  i=0
  while [ "$i" -lt "$count" ]; do
    local id name type
    id=$(echo "$downloaders" | python3 -c "import json,sys; print(json.load(sys.stdin)[${i}]['id'])")
    name=$(echo "$downloaders" | python3 -c "import json,sys; print(json.load(sys.stdin)[${i}]['name'])")
    type=$(echo "$downloaders" | python3 -c "import json,sys; print(json.load(sys.stdin)[${i}]['type'])")
    if [ "${type}" = "qbittorrent" ] || [[ "${name,,}" == *"qbittorrent"* ]] || [[ "${name,,}" == *"qbit"* ]]; then
      log "  deleting: ${name} (${type}, ${id})"
      curl -sS -X DELETE "${base}/api/downloaders/${id}" -o /dev/null -w "    HTTP %{http_code}\n"
    else
      log "  keeping: ${name} (${type}, ${id})"
    fi
    i=$((i+1))
  done

  log "Done."
}

##
## 2. DB mode (offline)
##
db_cleanup() {
  local db="$1"
  log "DB mode: ${db}"
  [ -f "$db" ] || fail "DB not found: ${db}"

  # Backup
  cp "$db" "${db}.bak-$(date +%Y%m%d-%H%M%S)"
  log "Backup: ${db}.bak-..."

  # Get current rows
  log "Current indexers:"
  sqlite3 "$db" "SELECT id, name, url FROM indexers;"
  log "Current downloaders:"
  sqlite3 "$db" "SELECT id, name, type FROM downloaders;"

  # Delete non-RomGoGetter indexers
  log "Deleting non-RomGoGetter indexers..."
  local keep="${KEEP_INDEXER_NAME:-RomGoGetter}"
  sqlite3 "$db" "DELETE FROM indexers WHERE lower(name) NOT LIKE lower('%$keep%');"

  # Delete qBittorrent downloaders
  log "Deleting qBittorrent downloaders..."
  sqlite3 "$db" "DELETE FROM downloaders WHERE type='qbittorrent' OR lower(name) LIKE '%qbittorrent%' OR lower(name) LIKE '%qbit%';"

  log "After:"
  sqlite3 "$db" "SELECT id, name, url FROM indexers;"
  sqlite3 "$db" "SELECT id, name, type FROM downloaders;"
}

##
## 3. Discover — guess where Questarr lives (k3s pod, docker, host)
##
discover() {
  log "Discovery mode (best-effort guesses)"

  # Check k3s deployments
  if [ -f "$k3s_yaml" ] && command -v kubectl >/dev/null 2>&1; then
    log "k3s deployments:"
    KUBECONFIG="$k3s_yaml" kubectl get deploy -A 2>&1 | grep -iE "quest|torzn|prowlarr|qbittorrent|transmission|romgo" || echo "  (none)"
  fi

  # Check docker/podman containers
  if command -v docker >/dev/null 2>&1; then
    log "docker containers:"
    docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>&1 | grep -iE "quest|torzn|prowlarr|qbittorrent|transmission|romgo" || echo "  (none)"
  fi
  if command -v podman >/dev/null 2>&1; then
    log "podman containers:"
    podman ps --format '  {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>&1 | grep -iE "quest|torzn|prowlarr|qbittorrent|transmission|romgo" || echo "  (none)"
  fi

  # Check common DB paths
  log "Common SQLite DB paths:"
  for p in \
    /var/lib/questarr/data/questarr.db \
    /mnt/shared/questarr/data/questarr.db \
    /srv/questarr/data/questarr.db \
    "${HOME}/questarr/data/questarr.db"
  do
    [ -f "$p" ] && log "  found: $p"
  done

  # Look for any *.db file on the system
  log "Hunting for questarr.db anywhere on /mnt and /var:..."
  find /mnt /var /srv -maxdepth 6 -name '*.db' 2>/dev/null | head -20 || true
}

case "$cmd" in
  api)
    api_cleanup "${2:-${QUESTARR_API_URL:-}}"
    ;;
  db)
    db_cleanup "${2:-${QUESTARR_DB_PATH:-}}"
    ;;
  discover|*)
    discover
    ;;
esac