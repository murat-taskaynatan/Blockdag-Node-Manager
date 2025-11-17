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
SERVICE_CPU_AFFINITY="${SERVICE_CPU_AFFINITY:-0}"
SERVICE_CPU_WEIGHT="${SERVICE_CPU_WEIGHT:-900}"
SNAPSHOT_DIR_DEFAULT="${SNAPSHOT_DIR_DEFAULT:-/opt/backups}"

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
REPO_REF="${REPO_REF:-${REPO_BRANCH:-v1.6.1}}"

need_cmd "$PYTHON_BIN"
ensure_packages python3 python3-venv python3-pip rsync fio

TEMP_DIR=""
cleanup() {
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

ensure_nginx_rate_limit() {
  local site_conf="/etc/nginx/sites-enabled/node_manager"
  if [[ ! -f "$site_conf" ]]; then
    return
  fi
  if grep -q "node_mgr_limit" "$site_conf"; then
    return
  fi
  sudo python3 <<'PY'
from pathlib import Path
conf = Path("/etc/nginx/sites-enabled/node_manager")
try:
    text = conf.read_text()
except FileNotFoundError:
    raise SystemExit("node_manager site config missing")
if "limit_req_zone $binary_remote_addr zone=node_mgr_limit" not in text:
    text = text.replace(
        "upstream node_manager_backend {",
        "limit_req_zone $binary_remote_addr zone=node_mgr_limit:10m rate=10r/s;\n\nupstream node_manager_backend {",
        1,
    )
if "limit_req zone=node_mgr_limit" not in text:
    text = text.replace(
        "location / {",
        "location / {\n        limit_req zone=node_mgr_limit burst=20 nodelay;\n",
        1,
    )
conf.write_text(text)
PY
  sudo nginx -t >/dev/null && sudo systemctl reload nginx >/dev/null
}

if [[ ! -f "$SOURCE_DIR/app.py" ]]; then
  need_cmd git
  TEMP_DIR="$(mktemp -d)"
  trap cleanup EXIT
echo "[0/10] Source tree missing; cloning $REPO_URL (ref $REPO_REF)"
  git clone --depth 1 --branch "$REPO_REF" --single-branch "$REPO_URL" "$TEMP_DIR/repo"
  SOURCE_DIR="$TEMP_DIR/repo"
fi

echo "[1/10] Removing any existing installation of $SERVICE_NAME (if present)"
if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE_NAME}"; then
  sudo systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
fi
sudo rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
sudo rm -f "$ENV_FILE"
if [[ -d "$INSTALL_DIR" ]]; then
  sudo rm -rf "$INSTALL_DIR"
fi
sudo systemctl daemon-reload

echo "[2/10] Syncing files to $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"
# Ensure service account owns the synced tree (covers data/ after upgrades)
sudo chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
# Guarantee runtime data directory exists and is writable
sudo mkdir -p "$INSTALL_DIR/data"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR/data"
sudo mkdir -p "$SNAPSHOT_DIR_DEFAULT"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$SNAPSHOT_DIR_DEFAULT"

echo "[3/10] Preparing Python environment"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

echo "[4/10] Writing environment file $ENV_FILE"
sudo mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<EOF
# BlockDAG Node Manager environment overrides
HOST=$HOST_DEFAULT
PORT=$PORT_DEFAULT
# Remote login gate: set to 1 with credentials to require sign-in
BDAG_LOGIN_ENABLED=0
# BDAG_LOGIN_USER=
# BDAG_LOGIN_PASS=
# BDAG_RPC_BASE=http://127.0.0.1:18545
# BDAG_RPC_USER=
# BDAG_RPC_PASS=
EOF
fi
if ! sudo grep -q "^BDAG_CPU_TEMP_PATH=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
# Default CPU temperature path (Settings tab updates this value)
BDAG_CPU_TEMP_PATH=/mnt/hgfs/vmshared/cpu_temp.txt
EOF
fi
if ! sudo grep -q "^BDAG_SAMPLE_SEC=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
# Cache duration for node metrics (seconds)
BDAG_SAMPLE_SEC=30
EOF
fi
if ! sudo grep -q "^WAITRESS_THREADS=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
# Waitress tuning
WAITRESS_THREADS=24
WAITRESS_BACKLOG=256
WAITRESS_CONNECTION_LIMIT=0
EOF
fi
if ! sudo grep -q "^BDAG_SNAPSHOT_LIGHT_MODE=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
# Snapshot/restore tuning
BDAG_SNAPSHOT_LIGHT_MODE=1
EOF
fi
if ! sudo grep -q "^BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
# Liveness auto-recover tuning
BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=240
EOF
fi
if ! sudo grep -q "^BDAG_LIVENESS_MAX_RESTARTS=" "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'EOF'
BDAG_LIVENESS_MAX_RESTARTS=2
EOF
fi
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$ENV_FILE"
sudo chmod 640 "$ENV_FILE"

CPU_DROPIN_DIR="$SYSTEMD_DIR/${SERVICE_NAME}.d"
CPU_OVERRIDE="$CPU_DROPIN_DIR/cpu.conf"
echo "[5/10] Writing CPU affinity/weight override at $CPU_OVERRIDE"
sudo mkdir -p "$CPU_DROPIN_DIR"
if [[ ! -f "$CPU_OVERRIDE" ]]; then
  sudo tee "$CPU_OVERRIDE" >/dev/null <<EOF
[Service]
CPUAffinity=$SERVICE_CPU_AFFINITY
CPUWeight=$SERVICE_CPU_WEIGHT
EOF
else
  echo " - existing override detected; leaving $CPU_OVERRIDE unchanged"
fi

DOCKER_GROUP_NOTICE=0
echo "[6/10] Ensuring Docker access for service user $SERVICE_USER"
if command -v docker >/dev/null 2>&1; then
  if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "docker"; then
    echo " - $SERVICE_USER already belongs to docker group"
  else
    if getent group docker >/dev/null 2>&1; then
      echo " - Adding $SERVICE_USER to docker group"
      sudo usermod -aG docker "$SERVICE_USER"
      DOCKER_GROUP_NOTICE=1
    else
      echo " - Warning: docker group not found; skipping group membership adjustment"
      DOCKER_GROUP_NOTICE=2
    fi
  fi
else
  echo " - Docker binary not found; skipping access check"
fi

echo "[7/10] Writing launch helper $INSTALL_DIR/run_node_manager.sh"
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
WAITRESS_THREADS="${WAITRESS_THREADS:-12}"
WAITRESS_BACKLOG="${WAITRESS_BACKLOG:-256}"
WAITRESS_CONNECTION_LIMIT="${WAITRESS_CONNECTION_LIMIT:-0}"
cd "$INSTALL_DIR"
ARGS=("--listen=${HOST}:${PORT}" "--threads=${WAITRESS_THREADS}" "--backlog=${WAITRESS_BACKLOG}")
if [[ -n "$WAITRESS_CONNECTION_LIMIT" && "$WAITRESS_CONNECTION_LIMIT" != "0" ]]; then
  ARGS+=("--connection-limit=${WAITRESS_CONNECTION_LIMIT}")
fi
exec "$VENV_BIN/waitress-serve" "${ARGS[@]}" app:app
EOF
sudo sed -i \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  "$RUNNER"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$RUNNER"
sudo chmod 750 "$RUNNER"

echo "[8/10] Installing systemd unit $SERVICE_NAME"
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

echo "[9/10] Reloading systemd and starting service"
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

echo "[10/10] Installation summary"
cat <<EOF
BlockDAG Node Manager installation complete.
  - Service name: $SERVICE_NAME
  - Config file: $ENV_FILE
  - UI: http://$DISPLAY_HOST:${PORT_VALUE:-8081}/
  - Manage via: sudo systemctl {status|restart|stop} $SERVICE_NAME
  - Logs: journalctl -u $SERVICE_NAME -f

EOF

ensure_nginx_rate_limit

echo "[10/9] Restarting $SERVICE_NAME to load the new install"
sudo systemctl restart "$SERVICE_NAME"

if [[ "$DOCKER_GROUP_NOTICE" -eq 1 ]]; then
  cat <<EOF
Additional action required:
  - Added $SERVICE_USER to docker group. Log out and back in (or restart the BlockDAG Node Manager service) so the new permissions take effect.

EOF
elif [[ "$DOCKER_GROUP_NOTICE" -eq 2 ]]; then
  cat <<EOF
Warning:
  - docker group not found; ensure Docker is installed and the service user can access /var/run/docker.sock before running discovery.

EOF
fi
