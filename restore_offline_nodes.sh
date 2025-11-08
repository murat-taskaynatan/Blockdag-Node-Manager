#!/usr/bin/env bash
set -euo pipefail

# Restore offline/stalled nodes sequentially with a cooldown between jobs.
# Usage: RESTORE_COOLDOWN_SEC=600 ./restore_offline_nodes.sh

BASE_URL="${BASE_URL:-http://localhost:8081}"
COOLDOWN_SEC="${RESTORE_COOLDOWN_SEC:-90}"

request() {
  local method="$1"
  local path="$2"
  local data="$3"
  curl -sS -X "$method" "$BASE_URL$path" -H "content-type: application/json" -d "$data" || return $?
}

nodes_json=$(curl -sS "$BASE_URL/api/node-manager/nodes")
offline_ids=$(printf '%s\n' "$nodes_json" | jq -r '.nodes[] | select(.status.running==false or .status.stalled==true) | .id')

if [[ -z "$offline_ids" ]]; then
  echo "No offline/on-stall nodes detected."
  exit 0
fi

echo "Restoring offline nodes:"
for node in $offline_ids; do
  echo "- Starting restore for node ${node}"
  response=$(request POST /api/snapshots/restore "{\"node\":\"${node}\"}")
  echo "  -> $response"
  echo "  Cooling down for ${COOLDOWN_SEC}s before next restore..."
  sleep "$COOLDOWN_SEC"
done
