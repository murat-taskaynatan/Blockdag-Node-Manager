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

### Remote login toggle

Authentication is disabled unless you explicitly set it in `/etc/blockdag-node-manager/node-manager.env` (the installer now writes `BDAG_LOGIN_ENABLED=0` by default). To require login, update the file so the gate is enabled and include credentials:

```ini
BDAG_LOGIN_ENABLED=1
BDAG_LOGIN_USER=manager
BDAG_LOGIN_PASS=changeme
```

`BDAG_LOGIN_ENABLED=0` or missing credentials keeps the login page hidden, which matches the experience after a clean remote install.

Waitress concurrency is tunable through `/etc/blockdag-node-manager/node-manager.env` via `WAITRESS_THREADS`, `WAITRESS_BACKLOG`, and `WAITRESS_CONNECTION_LIMIT`, which default to `12`, `256`, and `0` (unlimited) so the proxy can hold more simultaneous connections without overflowing the queue.

SSL is handled by nginx on the host. The installer drops a node_manager vhost in /etc/nginx/sites-available/ (symlinked into sites-enabled). To serve the login page over HTTPS:

1. Provision a cert (e.g., via Let’s Encrypt certbot certonly --nginx -d your.domain or copy an existing fullchain.pem/privkey.pem pair into /etc/letsencrypt/live/...).
2. Edit /etc/nginx/sites-available/node_manager so it has a server block listening on 443 with ssl_certificate / ssl_certificate_key pointing at the cert files. Keep the existing proxy_pass http://node_manager_backend stanza so nginx still forwards to Waitress on port 8081.
3. Optionally add an HTTP → HTTPS redirect block (listen 80; return 301 https://$host$request_uri;) so users always land on TLS.
4. Run sudo nginx -t and sudo systemctl reload nginx.
   
Once nginx terminates TLS, the Node Manager login page (and all other routes) are served at your domain. Because the app doesn’t need to know about TLS, no extra Flask settings are required—the cookie/session code works the same whether nginx connects via plain HTTP or HTTPS on the front end.

Liveness auto-recovery now seeds two env overrides on fresh installs: `BDAG_LIVENESS_RECOVER_COOLDOWN_SEC=240` to cap the waiting period between liveness interventions at four minutes, and `BDAG_LIVENESS_MAX_RESTARTS=3` so the guard escalates to a snapshot restore sooner when repeated restarts fail. Adjust those values in `node-manager.env` if your fleet needs a different cadence.

The settings form also exposes a memory-pressure auto-restart: enable the toggle and enter a percent value (e.g., `90`) so the manager will restart every discovered node sequentially (60 s between restarts) when host memory usage climbs above that threshold. Use it as a safety valve when the OS starts to swap.

The CPU temperature path also defaults to the shared-VM file (`/mnt/hgfs/vmshared/cpu_temp.txt`) via `BDAG_CPU_TEMP_PATH` and is only changed when you update the field inside the Settings tab—those edits are persisted in `config/settings.json`, so leave the dropdown blank if you want to keep the shared file in play.

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

After each `git push origin main`, run `./scripts/sync_opt_install.sh` from this repo. It updates the local clone, reruns `install_node_manager.sh`, and restarts the service so `/opt/blockdag-node-manager` always mirrors the latest `main` build.

### Restore offline nodes sequentially

When multiple nodes are offline or stalled, use `./scripts/restore_offline_nodes.sh` to trigger a restore job for each node one by one with a cooldown between jobs (`RESTORE_COOLDOWN_SEC`, default 90 s). The script calls `/api/snapshots/restore` for every node that reports `running==false` or `stalled==true`, then polls `/api/snapshots` until each job completes—showing the active node name and progress percentage before moving on. Export `BASE_URL` if the manager is bound to a non-default host/port.

Need a zero-touch install on a fresh host? Use the remote installer:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \ | sudo bash
```

The remote installer also inserts the same nginx rate limit by default now, so repeated scans are throttled before they reach the manager—no extra configuration required.


To update a remote host directly from GitHub, point the bootstrap script at the tag:

```bash
curl -fsSL https://raw.githubusercontent.com/murat-taskaynatan/Blockdag-Node-Manager/main/install_nm_from_github.sh \
| sudo REPO_REF=v1.5.1 bash
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
