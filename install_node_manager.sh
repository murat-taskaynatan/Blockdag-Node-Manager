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
PORT_DEFAULT="${PORT:-8081}"

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
REPO_URL="${REPO_URL:-https://github.com/murat-taskaynatan/Blockdag-Node-Manager.git}"
REPO_REF="${REPO_REF:-${REPO_BRANCH:-main}}"

need_cmd "$PYTHON_BIN"
ensure_packages python3 python3-venv python3-pip rsync fio

TEMP_DIR=""
cleanup() {
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

if [[ ! -f "$SOURCE_DIR/app.py" ]]; then
  need_cmd git
  TEMP_DIR="$(mktemp -d)"
  trap cleanup EXIT
  echo "[0/8] Source tree missing; cloning $REPO_URL (ref $REPO_REF)"
  git clone --depth 1 --branch "$REPO_REF" --single-branch "$REPO_URL" "$TEMP_DIR/repo"
  SOURCE_DIR="$TEMP_DIR/repo"
fi

echo "[1/8] Removing any existing installation of $SERVICE_NAME (if present)"
if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
  sudo systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
fi
sudo rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
sudo rm -f "$ENV_FILE"
if [[ -d "$INSTALL_DIR" ]]; then
  sudo rm -rf "$INSTALL_DIR"
fi
sudo systemctl daemon-reload

echo "[2/8] Syncing files to $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"

echo "[3/8] Preparing Python environment"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

echo "[4/8] Writing environment file $ENV_FILE"
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
fi
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$ENV_FILE"
sudo chmod 640 "$ENV_FILE"

echo "[5/8] Writing launch helper $INSTALL_DIR/run_node_manager.sh"
RUNNER="$INSTALL_DIR/run_node_manager.sh"
sudo tee "$RUNNER" >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ENV_FILE="__ENV_FILE__"
INSTALL_DIR="__INSTALL_DIR__"
VENV_BIN="$INSTALL_DIR/.venv/bin"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8081}"
cd "$INSTALL_DIR"
exec "$VENV_BIN/waitress-serve" --listen="${HOST}:${PORT}" app:app
EOF
sudo sed -i \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  "$RUNNER"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$RUNNER"
sudo chmod 750 "$RUNNER"

echo "[6/8] Installing systemd unit $SERVICE_NAME"
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
Environment="PYTHONWARNINGS=ignore:Unverified HTTPS request"
ExecStart=$RUNNER
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo "[7/8] Reloading systemd and starting service"
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
[8/8] Installation summary:
BlockDAG Node Manager installation complete.
  - Service name: $SERVICE_NAME
  - Config file: $ENV_FILE
  - UI: http://$DISPLAY_HOST:${PORT_VALUE:-8081}/
  - Manage via: sudo systemctl {status|restart|stop} $SERVICE_NAME
  - Logs: journalctl -u $SERVICE_NAME -f

EOF
