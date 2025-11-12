#!/bin/sh
set -e

# Prefer a bundled binary when present; fall back to whatever nodeworker ships.
if [ -x /opt/bdag/bdag ]; then
  BIN=/opt/bdag/bdag
elif [ -n "${NODE_BINARY:-}" ] && [ -x "${NODE_BINARY:-}" ]; then
  BIN="${NODE_BINARY:-}"
else
  BIN=/usr/local/bin/bdag
fi

echo "Using node binary: $BIN"

exec nodeworker \
  --health.liveness-timeout="${HEALTH_LIVENESS_TIMEOUT:-5m}" \
  --node-binary="$BIN" \
  --node-args="${NODE_ARGS:-}" \
  --rpc-url="${RPC_URL:-}" \
  --contract-address="${CONTRACT_ADDRESS:-}" \
  --rollout-window="${ROLLOUT_WINDOW:-}" \
  --persist-root="${PERSIST_ROOT:-}" \
  --health-min-peers="${HEALTH_MIN_PEERS:-}" \
  --contract-deploy-block="${CONTRACT_DEPLOY_BLOCK:-}"
