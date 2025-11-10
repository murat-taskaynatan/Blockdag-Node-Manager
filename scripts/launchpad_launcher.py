import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

LAUNCHPAD_REPO = "https://github.com/BlockdagNetworkLabs/blockdag-scripts.git"


class LaunchError(RuntimeError):
    """Raised when the launch process fails."""


def _run_command(cmd, cwd=None, env=None):
    process = subprocess.run(cmd, cwd=cwd, env=env or os.environ, capture_output=True, text=True)
    if process.returncode != 0:
        raise LaunchError(f"Command {' '.join(cmd)} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def _sanitize_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "", label.lower())
    return slug or "node"


def _collect_used_ports() -> set[int]:
    ports = set()
    try:
        output = _run_command(["docker", "ps", "--format", "{{.Ports}}"])
    except LaunchError:
        return ports
    for line in output.splitlines():
        for part in line.split(","):
            host_part = part.strip()
            if "->" not in host_part or ":" not in host_part:
                continue
            host = host_part.split(":")[1].split("->")[0]
            host = host.split("/")[0]
            if not host:
                continue
            try:
                ports.add(int(host))
            except ValueError:
                continue
    return ports


def _find_available_port(used: set[int], start: int) -> int:
    port = start
    while port in used:
        port += 1
    used.add(port)
    return port


def _existing_node_ports() -> Dict[str, List[int]]:
    ports = {"p2p": [], "rpc": [], "ws": [], "peer": []}
    try:
        names = _run_command(["docker", "ps", "--format", "{{.Names}}"])
    except LaunchError:
        return ports
    for name in names.splitlines():
        if not name.startswith("blockdag-node-"):
            continue
        for target, key in ((38131, "p2p"), (18545, "rpc"), (18546, "ws"), (18150, "peer")):
            try:
                mapping = _run_command(["docker", "port", name, f"{target}/tcp"])
            except LaunchError:
                continue
            host = mapping.split(":")[-1].split("/")[0].strip()
            if host.isdigit():
                ports[key].append(int(host))
    return ports


def _prepare_ports(config: Dict) -> Tuple[int, int, int, int]:
    base_p2p = int(config.get("p2pPort") or 38130)
    base_rpc = int(config.get("rpcPort") or 18545)
    base_ws = base_rpc + 1
    base_peer = 18150
    used = _collect_used_ports()
    if config.get("autoPorts"):
        existing = _existing_node_ports()
        start_p2p = max(existing["p2p"]) + 1 if existing["p2p"] else base_p2p
        start_rpc = max(existing["rpc"]) + 1 if existing["rpc"] else base_rpc
        start_ws = max(existing["ws"]) + 1 if existing["ws"] else base_ws
        start_peer = max(existing["peer"]) + 1 if existing["peer"] else base_peer
        p2p = _find_available_port(used, start_p2p)
        rpc = _find_available_port(used, start_rpc)
        while (rpc + 1) in used:
            rpc = _find_available_port(used, rpc + 1)
        ws = _find_available_port(used, start_ws)
        peer = _find_available_port(used, start_peer)
    else:
        if base_p2p in used or base_rpc in used or base_peer in used:
            raise LaunchError("Selected ports are already in use")
        p2p = base_p2p
        rpc = base_rpc
        ws = base_ws
        peer = base_peer
    return p2p, rpc, ws, peer


def _render_compose(source: Path, target: Path, label: str, p2p: int, rpc: int, ws: int, peer: int):
    text = source.read_text()
    text = text.replace("blockdag-testnet-network", f"blockdag-node-{label}")
    text = text.replace('- "38131:38131"', f'- "{p2p}:{p2p}"', 1)
    text = text.replace('- "18545:18545"', f'- "{rpc}:{rpc}"', 1)
    text = text.replace('- "18546:18546"', f'- "{ws}:{ws}"', 1)
    text = text.replace('- "18150:18150"', f'- "{peer}:18150"', 1)
    text = text.replace("--rpclisten=0.0.0.0:38131", f"--rpclisten=0.0.0.0:{p2p}")
    text = text.replace("--http.port=18545", f"--http.port={rpc}")
    text = text.replace("--ws.port=18546", f"--ws.port={ws}")
    text = text.replace("ws://127.0.0.1:18546", f"ws://127.0.0.1:{ws}")
    target.write_text(text)


def launch_node(payload: Dict) -> Dict:
    label = _sanitize_label(payload.get("label") or "node")
    wallet = (payload.get("walletAddress") or "").strip()
    if not wallet:
        raise LaunchError("Wallet address is required")
    install_path = Path(payload.get("installPath") or "").expanduser()
    if not install_path:
        raise LaunchError("Installation path is required")
    install_path.mkdir(parents=True, exist_ok=True)
    scripts_dir = install_path / "blockdag-scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)
    _run_command(["git", "clone", "--depth", "1", LAUNCHPAD_REPO, str(scripts_dir)])
    env_path = scripts_dir / ".env"
    env_path.write_text(f"PUB_ETH_ADDR={wallet}\n", encoding="utf-8")
    (scripts_dir / "wallet.txt").write_text(wallet + "\n", encoding="utf-8")
    p2p_port, rpc_port, ws_port, peer_port = _prepare_ports(payload)
    compose_src = scripts_dir / "docker-compose.yml"
    if not compose_src.exists():
        raise LaunchError("docker-compose template not found")
    compose_target = scripts_dir / f"docker-compose-{label}.yml"
    _render_compose(compose_src, compose_target, label, p2p_port, rpc_port, ws_port, peer_port)
    project_name = f"blockdag-node-{label}"
    env = {**os.environ, "MINING_ADDRESS": wallet}
    output = _run_command(
        ["docker", "compose", "-p", project_name, "-f", str(compose_target), "up", "-d"],
        cwd=str(scripts_dir),
        env=env,
    )
    return {"label": label, "p2pPort": p2p_port, "rpcPort": rpc_port, "dockerOutput": output}
