#!/usr/bin/env bash
set -euo pipefail
ENV_FILE="/etc/blockdag-node-manager/node-manager.env"
INSTALL_DIR="/opt/blockdag-node-manager"
VENV_BIN="$INSTALL_DIR/.venv/bin"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8081}"
WAITRESS_THREADS="${WAITRESS_THREADS:-24}"
WAITRESS_BACKLOG="${WAITRESS_BACKLOG:-512}"
WAITRESS_CONNECTION_LIMIT="${WAITRESS_CONNECTION_LIMIT:-512}"
cd "$INSTALL_DIR"
ARGS=("--listen=${HOST}:${PORT}" "--threads=${WAITRESS_THREADS}" "--backlog=${WAITRESS_BACKLOG}")
if [[ -n "$WAITRESS_CONNECTION_LIMIT" && "$WAITRESS_CONNECTION_LIMIT" != "0" ]]; then
  ARGS+=("--connection-limit=${WAITRESS_CONNECTION_LIMIT}")
fi
exec "$VENV_BIN/waitress-serve" "${ARGS[@]}" app:app
