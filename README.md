# BlockDAG Node Manager

BlockDAG Node Manager is a lightweight Flask application that discovers, monitors, and controls BlockDAG nodes running on the same host. It exposes a single-page UI with live height charts, peer counts, container status, and quick controls for restarting Docker-based nodes.

<img width="975" height="961" alt="image" src="https://github.com/user-attachments/assets/9959bfd1-7b8b-4837-9b67-e28e6e4cdea0" />




## Features
- Automatic discovery of local Docker containers running BlockDAG nodes.
- Real-time metrics showing local/remote height, deltas, peers, and node uptime.
- Time-series charts powered by lightweight sampling (no background daemons required).
- Safe Docker controls for starting, stopping, and restarting containers directly from the UI.
- REST API suitable for automation via `/api/node-manager/*`.

## Requirements
- Python 3.9+ with `venv`.
- `requests`, `flask`, and `waitress` Python packages (install via `pip install -r requirements.txt`).
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

Navigate to `http://localhost:8080/` to open the UI.

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

It downloads the installer, makes it executable, (optionally) honours overrides like `REPO_REF`, then runs the same deployment flow without requiring a local checkout.

## API Overview
- `GET /api/node-manager/nodes` — summary of discovered nodes and status.
- `GET /api/node-manager/metrics?nodes=primary,foo` — chart-ready metrics for the requested nodes.
- `POST /api/node-manager/discover` — force a Docker discovery pass.
- `POST /api/control` — trigger Docker actions, e.g. `{"action":"docker_restart","node":"primary"}`.

## License
This project is released under the MIT License. See `LICENSE` for details.

![Node Manager UI](static/3d.gif)
