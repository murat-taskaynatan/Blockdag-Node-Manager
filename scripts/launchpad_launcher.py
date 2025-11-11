import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LAUNCHPAD_REPO = "https://github.com/BlockdagNetworkLabs/blockdag-scripts.git"


class LaunchError(RuntimeError):
    """Raised when the launch process fails."""


def _simplify_launch_error(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    conflict = re.search(r'container name "([^"]+)" is already in use', text, re.IGNORECASE)
    if conflict:
        name = conflict.group(1)
        return f"Container '{name}' already exists. Remove or rename the existing container before launching."
    safe_dir = re.search(r"git config --global --add safe\.directory\s+(\S+)", text, re.IGNORECASE)
    if safe_dir:
        repo_path = safe_dir.group(1)
        return (
            "Git needs to trust the scripts directory. "
            f"Run `git config --global --add safe.directory {repo_path}` once and retry."
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[-1]


def _run_command(cmd, cwd=None, env=None):
    process = subprocess.run(cmd, cwd=cwd, env=env or os.environ, capture_output=True, text=True)
    if process.returncode != 0:
        stderr = (process.stderr or "").strip()
        stdout = (process.stdout or "").strip()
        friendly = _simplify_launch_error(stderr or stdout)
        if friendly:
            raise LaunchError(friendly)
        message = stderr.splitlines()[-1] if stderr else stdout.splitlines()[-1] if stdout else "command failed"
        raise LaunchError(f"Command {' '.join(cmd)} failed: {message}")
    return process.stdout.strip()


def _ensure_git_safe_directory(path: Path) -> None:
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    try:
        existing = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            capture_output=True,
            text=True,
            check=False,
        )
        entries = [line.strip() for line in (existing.stdout or "").splitlines() if line.strip()]
        if resolved in entries:
            return
    except Exception:
        pass
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", resolved],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass


def _sanitize_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "", label.lower())
    return slug or "node"


def _coerce_port(value, default: int) -> int:
    try:
        port = int(str(value).strip())
        if port > 0:
            return port
    except (TypeError, ValueError, AttributeError):
        pass
    return default


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


def _existing_node_ports(peer_internal_hint: Optional[int]) -> Tuple[Dict[str, List[int]], Optional[int]]:
    ports = {"p2p": [], "rpc": [], "ws": [], "peer": []}
    detected_peer_internal: Optional[int] = None
    fallback_candidates: List[Tuple[int, int]] = []
    try:
        names = _run_command(["docker", "ps", "--format", "{{.Names}}"])
    except LaunchError:
        return ports, None
    for name in names.splitlines():
        name = name.strip()
        if not name:
            continue
        try:
            inspect_raw = _run_command(["docker", "inspect", name])
        except LaunchError:
            continue
        try:
            info = json.loads(inspect_raw)[0]
        except Exception:
            continue
        port_map = (
            info.get("NetworkSettings", {}).get("Ports")
            or info.get("HostConfig", {}).get("PortBindings")
            or {}
        )
        if not isinstance(port_map, dict):
            continue
        container_fallbacks: List[Tuple[int, int]] = []
        matched_hint = False
        looks_like_blockdag = False
        for port_key, bindings in port_map.items():
            if not isinstance(port_key, str) or "/tcp" not in port_key:
                continue
            container_port_raw = port_key.split("/")[0]
            if not container_port_raw.isdigit():
                continue
            container_port = int(container_port_raw)
            binding = bindings[0] if isinstance(bindings, list) and bindings else None
            host_port = None
            if isinstance(binding, dict):
                host_raw = binding.get("HostPort")
                if host_raw and str(host_raw).isdigit():
                    host_port = int(host_raw)
            if host_port is None:
                continue
            if container_port == 38131:
                ports["p2p"].append(host_port)
                looks_like_blockdag = True
                continue
            if container_port == 18545:
                ports["rpc"].append(host_port)
                looks_like_blockdag = True
                continue
            if container_port == 18546:
                ports["ws"].append(host_port)
                looks_like_blockdag = True
                continue
            if peer_internal_hint and container_port == peer_internal_hint:
                ports["peer"].append(host_port)
                matched_hint = True
                detected_peer_internal = container_port
            else:
                container_fallbacks.append((container_port, host_port))
        if not matched_hint and looks_like_blockdag and container_fallbacks:
            fallback_candidates.extend(container_fallbacks)
    if not ports["peer"] and fallback_candidates:
        fallback_candidates.sort(key=lambda item: item[0])
        detected_peer_internal = fallback_candidates[0][0]
        ports["peer"] = [host for port, host in fallback_candidates if port == detected_peer_internal]
    return ports, detected_peer_internal


def _prepare_ports(config: Dict) -> Tuple[int, int, int, int, int]:
    base_p2p = _coerce_port(config.get("p2pPort"), 38130)
    base_rpc = _coerce_port(config.get("rpcPort"), 18545)
    base_ws = _coerce_port(config.get("wsPort"), base_rpc + 1)
    peer_internal_hint = _coerce_port(config.get("peerPort"), 18150)
    existing, detected_peer_internal = _existing_node_ports(peer_internal_hint)
    peer_internal = detected_peer_internal or peer_internal_hint or 18150
    base_peer = min(existing["peer"]) if existing["peer"] else peer_internal
    external_override = config.get("externalP2PPort")
    peer_external_override = config.get("externalPeerPort")
    used = _collect_used_ports()
    if config.get("autoPorts"):
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
        override = _coerce_port(external_override, base_p2p)
        manual_ws = _coerce_port(config.get("wsPort"), base_ws)
        manual_peer = _coerce_port(peer_external_override, base_peer)
        if override in used or base_rpc in used or manual_ws in used or manual_peer in used:
            raise LaunchError("Selected ports are already in use")
        p2p = override
        rpc = base_rpc
        ws = manual_ws
        peer = manual_peer
    return p2p, rpc, ws, peer, peer_internal


def _render_compose(source: Path, target: Path, label: str, p2p: int, rpc: int, ws: int, peer: int, peer_internal: int):
    text = source.read_text()
    text = text.replace("blockdag-testnet-network", label)
    text = text.replace('- "38131:38131"', f'- "{p2p}:{p2p}"', 1)
    text = text.replace('- "18545:18545"', f'- "{rpc}:{rpc}"', 1)
    text = text.replace('- "18546:18546"', f'- "{ws}:{ws}"', 1)
    text = text.replace('- "18150:18150"', f'- "{peer}:{peer_internal}"', 1)
    text = text.replace("--rpclisten=0.0.0.0:38131", f"--rpclisten=0.0.0.0:{p2p}")
    text = text.replace("--http.port=18545", f"--http.port={rpc}")
    text = text.replace("--ws.port=18546", f"--ws.port={ws}")
    text = text.replace("ws://127.0.0.1:18546", f"ws://127.0.0.1:{ws}")
    target.write_text(text)


def preview_ports(payload: Dict) -> Dict[str, int]:
    """Return the resolved port mappings without starting any containers."""
    p2p_port, rpc_port, ws_port, peer_port, peer_internal = _prepare_ports(payload)
    return {
        "p2pPort": p2p_port,
        "rpcPort": rpc_port,
        "wsPort": ws_port,
        "peerPort": peer_port,
        "peerPortInternal": peer_internal,
    }


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
    git_dir = scripts_dir / ".git"
    if scripts_dir.exists():
        if not git_dir.exists():
            raise LaunchError("Existing blockdag-scripts directory is not a git repo; remove it and retry")
        _ensure_git_safe_directory(scripts_dir)
        _run_command(["git", "-C", str(scripts_dir), "pull"])
    else:
        _ensure_git_safe_directory(scripts_dir)
        _run_command(["git", "clone", "--depth", "1", LAUNCHPAD_REPO, str(scripts_dir)])
    env_path = scripts_dir / ".env"
    env_path.write_text(f"PUB_ETH_ADDR={wallet}\n", encoding="utf-8")
    (scripts_dir / "wallet.txt").write_text(wallet + "\n", encoding="utf-8")
    p2p_port, rpc_port, ws_port, peer_port, peer_internal = _prepare_ports(payload)
    compose_src = scripts_dir / "docker-compose.yml"
    if not compose_src.exists():
        raise LaunchError("docker-compose template not found")
    compose_target = scripts_dir / f"docker-compose-{label}.yml"
    _render_compose(compose_src, compose_target, label, p2p_port, rpc_port, ws_port, peer_port, peer_internal)
    project_name = label
    env = {**os.environ, "MINING_ADDRESS": wallet}
    output = _run_command(
        ["docker", "compose", "-p", project_name, "-f", str(compose_target), "up", "-d"],
        cwd=str(scripts_dir),
        env=env,
    )
    return {
        "label": label,
        "p2pPort": p2p_port,
        "rpcPort": rpc_port,
        "wsPort": ws_port,
        "peerPort": peer_port,
        "peerPortInternal": peer_internal,
        "dockerOutput": output,
    }
