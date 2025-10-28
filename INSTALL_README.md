# BlockDAG Node Manager Installation Guide

This guide explains how to deploy the BlockDAG Node Manager UI as a system service using the bundled helper script `install_node_manager.sh`.

## 1. Prerequisites

- Ubuntu/Debian or RHEL/Fedora style host with `sudo`
- Python 3.9+ with `venv`
- `git`, `rsync`, `systemctl`, and Docker (optional but recommended for discovery/control)

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

Afterwards, visit `http://<host>:8081/` (defaults to all interfaces on port `8081`).

## 3. Install Directly From GitHub

Need to bootstrap a host without cloning this repository first? Use the fetch-and-install helper:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \
  -o install_nm_from_github.sh && chmod +x install_nm_from_github.sh && sudo ./install_nm_from_github.sh
```

Customize with the same environment variables (`REPO_REF`, `INSTALL_DIR`, etc.) as the local installer.

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
