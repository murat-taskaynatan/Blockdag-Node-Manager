#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/blockdag-node-manager}"
SERVICE_NAME="${SERVICE_NAME:-blockdag-node-manager.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DEFAULT_SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_USER="${SERVICE_USER:-$DEFAULT_SERVICE_USER}"
if [[ -z "${SERVICE_GROUP:-}" ]]; then
  SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || id -gn)"
fi
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_DIR="${ENV_DIR:-/etc/blockdag-node-manager}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/node-manager.env}"
HOST_DEFAULT="${HOST:-0.0.0.0}"
PORT_DEFAULT="${PORT:-8080}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: required command '$1' not found." >&2; exit 1; }
}

ensure_packages() {
  local packages=("$@")
  local missing=()
  if command -v apt-get >/dev/null 2>&1; then
    for pkg in "${packages[@]}"; do
      dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
      echo "Installing missing apt packages: ${missing[*]}"
      sudo apt-get update
      sudo apt-get install -y "${missing[@]}"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    for pkg in "${packages[@]}"; do
      rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
      echo "Installing missing dnf packages: ${missing[*]}"
      sudo dnf install -y "${missing[@]}"
    fi
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$SCRIPT_DIR}"

need_cmd "$PYTHON_BIN"
ensure_packages python3 python3-venv python3-pip rsync

echo "[1/6] Syncing files to $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"

echo "[2/6] Preparing Python environment"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

echo "[3/6] Writing environment file $ENV_FILE"
sudo mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<EOF
# BlockDAG Node Manager environment overrides
HOST=$HOST_DEFAULT
PORT=$PORT_DEFAULT
# BDAG_RPC_BASE=http://127.0.0.1:18545
# BDAG_RPC_USER=
# BDAG_RPC_PASS=
EOF
  sudo chmod 600 "$ENV_FILE"
fi

echo "[4/6] Installing systemd unit $SERVICE_NAME"
START_CMD="/bin/bash -lc 'source $ENV_FILE 2>/dev/null || true; HOST=\${HOST:-0.0.0.0}; PORT=\${PORT:-8080}; exec $INSTALL_DIR/.venv/bin/waitress-serve --listen=\"\${HOST}:\${PORT}\" app:app'"
sudo tee "$SYSTEMD_DIR/$SERVICE_NAME" >/dev/null <<EOF
[Unit]
Description=BlockDAG Node Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
Environment=PYTHONWARNINGS=ignore:Unverified HTTPS request
ExecStart=$START_CMD
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo "[5/6] Reloading systemd and starting service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

HOST_VALUE="$(sudo awk -F= '/^HOST=/{print $2}' "$ENV_FILE" 2>/dev/null | tail -n1)"
PORT_VALUE="$(sudo awk -F= '/^PORT=/{print $2}' "$ENV_FILE" 2>/dev/null | tail -n1)"
HOST_VALUE="${HOST_VALUE:-$HOST_DEFAULT}"
PORT_VALUE="${PORT_VALUE:-$PORT_DEFAULT}"
if [[ -z "$HOST_VALUE" || "$HOST_VALUE" == "0.0.0.0" || "$HOST_VALUE" == "::" || "$HOST_VALUE" == "[::]" ]]; then
  DISPLAY_HOST="localhost"
else
  DISPLAY_HOST="$HOST_VALUE"
fi
if [[ "$DISPLAY_HOST" == *:* && "$DISPLAY_HOST" != \[* ]]; then
  DISPLAY_HOST="[$DISPLAY_HOST]"
fi

cat <<EOF

BlockDAG Node Manager installation complete.
  - Service name: $SERVICE_NAME
  - Config file: $ENV_FILE
  - UI: http://$DISPLAY_HOST:${PORT_VALUE:-8080}/node-manager
  - Manage via: sudo systemctl {status|restart|stop} $SERVICE_NAME
  - Logs: journalctl -u $SERVICE_NAME -f

EOF
