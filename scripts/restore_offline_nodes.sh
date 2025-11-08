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
  payload="{\"node\":\"${node}\"}"
  response=$(request POST /api/snapshots/restore "$payload")
  echo "  -> $response"
  if echo "$response" | jq -e '.ok' >/dev/null 2>&1; then
    echo "  Waiting for restore job to complete..."
    while true; do
      job=$(curl -sS "$BASE_URL/api/snapshots" | jq -r '.job')
      active=$(echo "$job" | jq -r '.active' 2>/dev/null || echo "false")
      if [[ "$active" != "true" ]]; then
        break
      fi
      sleep 5
    done
  else
    echo "  Restore request failed; skipping wait."
  fi
  echo "  Cooling down for ${COOLDOWN_SEC}s before next restore..."
  sleep "$COOLDOWN_SEC"
done
