# BlockDAG Node Manager Installation Guide

This guide explains how to deploy the BlockDAG Node Manager UI as a system service using the bundled helper script `install_node_manager.sh`.

## 1. Prerequisites

- Ubuntu/Debian or RHEL/Fedora style host with `sudo`
- Python 3.9+ with `venv`
- `git`, `rsync`, `systemctl`, and Docker (optional but recommended for discovery/control)
- `fio` (used by Overclock → Verify). The installer auto‑installs `fio` when using apt/dnf; the app also attempts on‑demand install when you click Verify.

## 2. Quick Install

Clone the repository and run the installer:

```bash
git clone https://github.com/murat-taskaynatan/Blockdag-Node-Manager.git
cd Blockdag-Node-Manager
sudo ./install_node_manager.sh
```

The script will:

1. Sync the repository contents into `/opt/blockdag-node-manager`.
2. Create a Python virtual environment in `/opt/blockdag-node-manager/.venv`.
3. Install dependencies from `requirements.txt` (falls back to `flask`, `requests`, `waitress` if missing).
4. Write `/etc/blockdag-node-manager/node-manager.env` with overridable environment variables.
5. Register and start the `blockdag-node-manager.service` systemd unit.
6. Ensure `fio` is installed for the Overclock verification benchmark (apt/dnf systems).

If a previous `blockdag-node-manager.service` exists, the installer stops, disables, and removes it before reinstalling. Running the script outside a repo clone automatically pulls the latest sources from GitHub.

Afterwards, visit `http://<host>:8081/` (defaults to all interfaces on port `8081`).

## 3. Install Directly From GitHub

Need to bootstrap a host without cloning this repository first? Use the fetch-and-install helper:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \
  -o install_nm_from_github.sh && chmod +x install_nm_from_github.sh && sudo ./install_nm_from_github.sh
```

Customize with the same environment variables (`REPO_REF`, `INSTALL_DIR`, etc.) as the local installer.
The fetch-and-install helper also seeds the Node Manager nginx vhost with the built-in rate limit so new installations throttle abusive probes automatically.

### Updating `/opt` after tagging v1.4.9

Once the release is tagged and pushed (`git tag v1.4.9 && git push origin v1.4.9`), refresh the managed install:

```bash
git fetch --tags
git checkout v1.4.9
sudo ./install_node_manager.sh
```

Need to upgrade a host without cloning the repo first? Run:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \
  | sudo REPO_REF=v1.4.9 bash
```

## 4. Customising the Install

Set environment variables before running the installer:

| Variable      | Description                              | Default                               |
|---------------|------------------------------------------|---------------------------------------|
| `INSTALL_DIR` | Deploy location for the app              | `/opt/blockdag-node-manager`          |
| `SERVICE_NAME`| Systemd unit filename                    | `blockdag-node-manager.service`       |
| `SERVICE_USER`/`SERVICE_GROUP` | Service owner/group    | detected from `SUDO_USER`             |
| `HOST`        | Listen address for waitress              | `0.0.0.0`                             |
| `PORT`        | Listen port for waitress                 | `8081`                                |
| `REPO_URL` / `REPO_REF` | Alternate source/ref         | this repository / `main`              |

Example:

```bash
sudo INSTALL_DIR=/srv/bdag-manager \
     SERVICE_USER=flask \
     HOST=127.0.0.1 PORT=5000 \
     ./install_node_manager.sh
```

## 5. Runtime Configuration

Edit `/etc/blockdag-node-manager/node-manager.env` to override RPC settings or binding options:

```
HOST=0.0.0.0
PORT=8081
BDAG_RPC_BASE=http://127.0.0.1:18545
BDAG_RPC_USER=
BDAG_RPC_PASS=
```

Restart the service after changes:

```bash
sudo systemctl restart blockdag-node-manager.service
```

The settings UI includes a memory-pressure auto-restart toggle/threshold that restarts every node one by one (with a 60 s gap) when overall memory usage exceeds the configured percent. Adjust `auto_restart_mem_threshold` via the form and keep the toggle enabled to use this safeguard.

Tweak Waitress concurrency with `WAITRESS_THREADS`, `WAITRESS_BACKLOG`, and `WAITRESS_CONNECTION_LIMIT` in the same env file (defaults are 12 threads, 256 backlog slots, and no connection cap).

The installer also seeds the liveness watchdog to act faster by default:

```
BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=240
BDAG_LIVENESS_MAX_RESTARTS=3
```

Change those values if your nodes need a longer (or shorter) window between restart attempts before the manager escalates to a snapshot restore.

### Remote login toggle

The installer now seeds `BDAG_LOGIN_ENABLED=0` so the login gate is off by default (matching the remote bootstrap experience). To require authentication, flip the flag and supply credentials:

```
BDAG_LOGIN_ENABLED=1
BDAG_LOGIN_USER=manager
BDAG_LOGIN_PASS=changeme
```

Leave `BDAG_LOGIN_ENABLED=0` or omit credentials to keep the UI fully open.

The CPU temperature path is seeded to `/mnt/hgfs/vmshared/cpu_temp.txt` via `BDAG_CPU_TEMP_PATH` (see the env template above); change it in the Settings tab if you need another file, and that selection is saved back to `config/settings.json`.

## 6. Managing the Service

```bash
sudo systemctl status blockdag-node-manager.service   # check status
sudo systemctl restart blockdag-node-manager.service  # restart
sudo systemctl stop blockdag-node-manager.service     # stop
journalctl -u blockdag-node-manager.service -f        # tail logs
```

## 7. Uninstall

```bash
sudo systemctl disable --now blockdag-node-manager.service
sudo rm -f /etc/systemd/system/blockdag-node-manager.service
sudo rm -rf /opt/blockdag-node-manager
sudo rm -rf /etc/blockdag-node-manager
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

## 8. Support

- Open issues at <https://github.com/murat-taskaynatan/Blockdag-Node-Manager/issues>
- Include `journalctl -u blockdag-node-manager.service` logs and `node-manager.env` overrides when reporting problems.
