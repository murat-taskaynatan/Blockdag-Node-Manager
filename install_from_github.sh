#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/murat-taskaynatan/Blockdag-Node-Manager.git}"
REPO_REF="${REPO_REF:-${REPO_BRANCH:-main}}"

: "${INSTALL_DIR:=/opt/blockdag-node-manager}"
: "${SERVICE_NAME:=blockdag-node-manager.service}"
: "${SYSTEMD_DIR:=/etc/systemd/system}"
: "${ENV_DIR:=/etc/blockdag-node-manager}"
: "${ENV_FILE:=$ENV_DIR/node-manager.env}"
: "${PYTHON_BIN:=python3}"
: "${HOST:=0.0.0.0}"
: "${PORT:=8080}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: required command '$1' not found." >&2; exit 1; }
}

cleanup() {
  rm -rf "$TEMP_ROOT"
}

need_cmd git
need_cmd "$PYTHON_BIN"

TEMP_ROOT="$(mktemp -d)"
trap cleanup EXIT

echo "[1/4] Cloning $REPO_URL (ref $REPO_REF)"
git clone --depth 1 --branch "$REPO_REF" --single-branch "$REPO_URL" "$TEMP_ROOT/repo"

echo "[2/4] Running installer from cloned repository"
SOURCE_DIR="$TEMP_ROOT/repo" \
INSTALL_DIR="$INSTALL_DIR" \
SERVICE_NAME="$SERVICE_NAME" \
SYSTEMD_DIR="$SYSTEMD_DIR" \
ENV_DIR="$ENV_DIR" \
ENV_FILE="$ENV_FILE" \
PYTHON_BIN="$PYTHON_BIN" \
HOST="$HOST" \
PORT="$PORT" \
bash "$TEMP_ROOT/repo/install_node_manager.sh"

echo "[3/4] Cleaning temporary checkout"
cleanup
trap - EXIT

cat <<EOF
[4/4] Installation finished!
  - Deployed from $REPO_URL@$REPO_REF
  - Service name: $SERVICE_NAME
  - Config file: $ENV_FILE
EOF
