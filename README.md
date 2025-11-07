# BlockDAG | Node Manager

BlockDAG Node Manager is a lightweight Flask application that discovers, monitors, and controls multiple BlockDAG nodes running on the local network. It exposes a single-page UI with live height charts, peer counts, container status, and quick controls for restarting Docker-based nodes.

<img width="973" height="1007" alt="image" src="https://github.com/user-attachments/assets/2117ef24-e333-449b-88e6-2df2d3e9186b" />







## Features
- Automatic discovery of local Docker containers running BlockDAG nodes.
- Real-time metrics showing local/remote height, deltas, peers, and node uptime.
- Time-series charts powered by lightweight sampling (no background daemons required).
- Dedicated backup management module with scheduled backups.
- Chart controls for sampling window and history length, with server-side buffering.
- Dynamic Flask route /api/status and chart APIs powering the frontend.
- Live log viewer with ANSI cleanup and auto-scroll to keep recent node activity visible.
- Remote-height awareness that surfaces local vs remote deltas and ETA to full sync.
- Mining state detection and health categorisation (steady, syncing, downloading, stalled, etc.).
- Safe Docker controls for starting, stopping, and restarting containers directly from the UI.
- REST API suitable for automation via `/api/node-manager/*`.

Log View
<br>
  <img width="973" height="307" alt="image" src="https://github.com/user-attachments/assets/5d6cb4b7-d64e-4a52-bf86-aa6983575eba" />


## Requirements
- Python 3.10+ with `venv`.
- `Flask >= 3.0.0`, `Requests >= 2.31.0`, and `Waitress >= 3.0.0` (install via `pip install -r requirements.txt`).
- Optional: Docker CLI for container discovery and controls.

## Quick Start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
export FLASK_APP=app.py
flask run --host=0.0.0.0 --port=8080
```

Navigate to `http://localhost:8081/` to open the UI.

## Configuration
Node definitions live in `config/nodes.json`. Each entry can specify:

```jsonc
[
  {
    "id": "primary",
    "label": "Primary Node",
    "container": "blockdag-node",
    "rpc_base": "http://127.0.0.1:18545",
    "remote_rpc_bases": [
      "https://rpc.bdagscan.com"
    ]
  }
]
```

Environment variables (`BDAG_RPC_BASE`, `BDAG_REMOTE_RPC_BASES`, etc.) are honoured and can be referenced inside the JSON using `${VAR:-default}` placeholders.

## Production Install
Use the bundled helper to deploy under `/opt/blockdag-node-manager` with systemd integration:

```bash
./install_node_manager.sh
```

The installer automatically stops and removes any previous `blockdag-node-manager.service` deployment before syncing new files, and if you run it outside a repo clone it will fetch the latest sources from GitHub automatically.

By default the service binds to `0.0.0.0:8081`; adjust `HOST`/`PORT` in `/etc/blockdag-node-manager/node-manager.env` or export them before running the installer if you need different bindings.

The script can be customised via environment variables, e.g.:

```bash
HOST=0.0.0.0 PORT=8080 INSTALL_DIR=/opt/bdag-manager ./install_node_manager.sh
```

All runtime overrides are stored in `/etc/blockdag-node-manager/node-manager.env`.

Need a zero-touch install on a fresh host? Use the remote installer:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \ | sudo bash
```

## Updating the `/opt` install to v1.4.5
After tagging and pushing `v1.4.5` (`git tag v1.4.5 && git push origin v1.4.5`), rerun the installer so `/opt/blockdag-node-manager` picks up the release:

```bash
git fetch --tags
git checkout v1.4.5
sudo ./install_node_manager.sh
```

To update a remote host directly from GitHub, point the bootstrap script at the tag:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \
| sudo REPO_REF=v1.4.5 bash
```

## Uninstall
To remove an installed service:

```bash
sudo systemctl stop blockdag-node-manager
sudo systemctl disable blockdag-node-manager
sudo rm -rf /opt/blockdag-node-manager
sudo rm -f /etc/systemd/system/blockdag-node-manager.service
sudo rm -f /etc/blockdag-node-manager/node-manager.env
sudo systemctl daemon-reload
```

Adjust the paths if you installed into a custom directory, and remove any leftover snapshot or backup directories you no longer need.


## API Overview
- `GET /api/node-manager/nodes` — summary of discovered nodes and status.
- `GET /api/node-manager/metrics?nodes=primary,foo` — chart-ready metrics for the requested nodes.
- `POST /api/node-manager/discover` — force a Docker discovery pass.
- `POST /api/control` — trigger Docker actions, e.g. `{"action":"docker_restart","node":"primary"}`.

## License
This project is released under the MIT License. See `LICENSE` for details.

![Node Manager UI](static/3d.gif)
