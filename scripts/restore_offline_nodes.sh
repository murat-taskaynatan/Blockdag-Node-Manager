#!/usr/bin/env bash
set -euo pipefail

# Restore offline/stalled nodes sequentially with a cooldown between jobs.
# Usage: RESTORE_COOLDOWN_SEC=600 ./restore_offline_nodes.sh

BASE_URL="${BASE_URL:-http://localhost:8081}"
COOLDOWN_SEC="${RESTORE_COOLDOWN_SEC:-30}"

request() {
  local method="$1"
  local path="$2"
  local data="$3"
  curl -sS -X "$method" "$BASE_URL$path" -H "content-type: application/json" -d "$data" || return $?
}

ensure_clean_node() {
  local container="$1"
  if [[ -z "$container" || "$container" == "null" || "$container" == "none" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "  Docker CLI not available; skipping cleanup for ${container}"
    return 0
  fi
  echo "  Stopping container ${container} before restore..."
  docker stop "$container" >/dev/null 2>&1 || true
}

nodes_json=$(curl -sS "$BASE_URL/api/node-manager/nodes")
mapfile -t offline_nodes < <(
  printf '%s\n' "$nodes_json" | jq -r '
    .nodes[]
    | select(.status.running==false or .status.stalled==true or .status.forced_offline==true)
    | [.id, (.container // .id // "")]
    | @tsv'
)

if [[ ${#offline_nodes[@]} -eq 0 ]]; then
  echo "No offline/on-stall nodes detected."
  exit 0
fi

echo "Restoring offline nodes:"
for entry in "${offline_nodes[@]}"; do
  IFS=$'\t' read -r node container <<< "$entry"
  echo "- Starting restore for node ${node}"
  ensure_clean_node "$container"
  payload="{\"node\":\"${node}\"}"
  response=$(request POST /api/snapshots/restore "$payload")
  brief=$(echo "$response" | jq -r '.message // .error // "restore submitted"' 2>/dev/null || echo "restore submitted")
  echo "  -> ${brief}"
  if echo "$response" | jq -e '.ok' >/dev/null 2>&1; then
    echo "  Waiting for restore job to complete..."
    while true; do
      job=$(curl -sS "$BASE_URL/api/snapshots" | jq '.job')
      active=$(echo "$job" | jq -r '.active' 2>/dev/null || echo "false")
      progress=$(echo "$job" | jq -r '.progress.pct' 2>/dev/null)
      details_node=$(echo "$job" | jq -r '.details.node // empty')
      progress_text="unknown"
      if [[ "$progress" != "null" && "$progress" != "" ]]; then
        progress_int=$(printf "%.0f" "$progress")
        progress_text="${progress_int}%"
      fi
      echo "    Node ${details_node:-$node} restore progress: ${progress_text}"
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
