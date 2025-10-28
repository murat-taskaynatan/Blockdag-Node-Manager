#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/murat-taskaynatan/Blockdag-Node-Manager.git}"
REPO_REF="${REPO_REF:-${REPO_BRANCH:-main}}"
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

cleanup() {
  rm -rf "$TEMP_ROOT"
}

need_cmd git
need_cmd "$PYTHON_BIN"
ensure_packages python3 python3-venv python3-pip rsync

TEMP_ROOT="$(mktemp -d)"
trap cleanup EXIT

echo "[1/6] Cloning $REPO_URL (ref $REPO_REF)"
git clone --depth 1 --branch "$REPO_REF" --single-branch "$REPO_URL" "$TEMP_ROOT/repo"

echo "[2/6] Syncing files to $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  "$TEMP_ROOT/repo/" "$INSTALL_DIR/"

echo "[3/6] Preparing Python environment"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

echo "[4/6] Writing environment file $ENV_FILE"
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

echo "[5/6] Installing systemd unit $SERVICE_NAME"
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

echo "[6/6] Reloading systemd and starting service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

cat <<EOF

BlockDAG Node Manager installation complete.
  - Service name: $SERVICE_NAME
  - Config file: $ENV_FILE
  - Manage via: sudo systemctl {status|restart|stop} $SERVICE_NAME
  - Logs: journalctl -u $SERVICE_NAME -f

EOF
