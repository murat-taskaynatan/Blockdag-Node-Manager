import grp
import json
import os
import pwd
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LAUNCHPAD_REPO = os.getenv("BDAG_LAUNCHPAD_REPO", "https://github.com/BlockdagNetworkLabs/blockdag-scripts.git")
LAUNCHPAD_DEFAULT_IMAGE = os.getenv("BDAG_LAUNCHPAD_IMAGE", "blockdagnetwork/awakening:v0.0.2")
HELPER_TEMPLATE = Path(__file__).resolve().parent / "launchpad_entrypoint.sh"
NETWORK_KEY_NAMES = ("network.key",)
BDAGETH_KEY_GLOBS = ("**/bdageth/keystore/*",)


class LaunchError(RuntimeError):
    """Raised when the launch process fails."""


def _current_identity() -> Tuple[Optional[str], Optional[str]]:
    user = os.getenv("SUDO_USER") or os.getenv("USER")
    group = os.getenv("SUDO_GID")
    try:
        uid = os.getuid()
        if not user:
            user = pwd.getpwuid(uid).pw_name
        gid = os.getgid()
        if not group:
            group = grp.getgrgid(gid).gr_name
    except Exception:
        pass
    if isinstance(group, str) and group.isdigit():
        try:
            group = grp.getgrgid(int(group)).gr_name
        except Exception:
            pass
    if group is None and user:
        group = user
    return user, group


def _reset_launchpad_identity(scripts_dir: Path) -> None:
    """Remove peer identity files so each launchpad node gets a unique ID."""
    if not scripts_dir:
        return
    data_root = scripts_dir / "bin"
    if not data_root.exists():
        return

    # Strip any existing libp2p identity files across all node-label subdirs (bin/<label>/bdag/data/**/network.key).
    try:
        for key_path in data_root.rglob("network.key"):
            if key_path.is_file():
                try:
                    key_path.unlink(missing_ok=True)
                except Exception:
                    continue
    except Exception:
        pass

    # Drop bundled bdageth keystores so each launch is clean unless user overwrote them.
    try:
        for key_path in data_root.rglob("bdageth/keystore/*"):
            if key_path.is_file():
                try:
                    key_path.unlink(missing_ok=True)
                except Exception:
                    continue
    except Exception:
        pass


def _rewrite_compose_image(compose_path: Path, image: str) -> None:
    """Ensure compose template uses the desired image."""
    if not compose_path or not compose_path.exists():
        return
    try:
        text = compose_path.read_text()
    except Exception:
        return
    pattern = re.compile(r"image:\s+blockdagnetwork/awakening:[^\s]+", re.IGNORECASE)
    image_line = f"image: {image}"
    if pattern.search(text):
        text = pattern.sub(image_line, text, count=1)
    elif "blockdagnetwork/awakening" in text:
        text = text.replace("blockdagnetwork/awakening", image, 1)
    else:
        return
    try:
        compose_path.write_text(text)
    except Exception:
        pass

def _ensure_install_path_ready(path: Path) -> None:
    if not path:
        return
    resolved = path
    try:
        resolved = path.expanduser()
    except Exception:
        resolved = path
    user, group = _current_identity()
    if not resolved.exists():
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            try:
                subprocess.run(["sudo", "mkdir", "-p", str(resolved)], capture_output=True, text=True, check=False)
            except Exception:
                pass
        if not user:
            return
    group = group or user
    try:
        subprocess.run(
            ["sudo", "chown", "-R", f"{user}:{group}", str(resolved)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass


def _list_blockdag_networks() -> List[str]:
    try:
        output = _run_command(["docker", "network", "ls", "--format", "{{.Name}}"])
    except LaunchError:
        return []
    names = []
    for line in output.splitlines():
        name = line.strip()
        if not name:
            continue
        if "blockdag" not in name.lower():
            continue
        names.append(name)
    return names


def _prune_orphan_blockdag_networks() -> List[str]:
    removed: List[str] = []
    for name in _list_blockdag_networks():
        try:
            inspect_raw = _run_command(["docker", "network", "inspect", name])
        except LaunchError:
            continue
        try:
            info = json.loads(inspect_raw)
        except json.JSONDecodeError:
            continue
        details = info[0] if info else {}
        containers = details.get("Containers") or {}
        if containers:
            continue
        try:
            _run_command(["docker", "network", "rm", name])
            removed.append(name)
        except LaunchError:
            continue
    return removed


def _simplify_launch_error(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
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
    busy_line = next(
        (line for line in lines if re.search(r"(address already in use|port is already allocated)", line, re.IGNORECASE)),
        None,
    )
    if busy_line:
        port_match = re.search(r":(\d{2,5})\b", busy_line)
        if not port_match:
            port_match = re.search(r"\bport\s+(\d{2,5})\b", busy_line, re.IGNORECASE)
        port_detail = f"Port {port_match.group(1)} " if port_match else "One of the requested ports "
        return (
            f"{port_detail}is already in use. "
            "Adjust the Launch Pad port settings or let the manager auto-calculate ports."
        )
    subnet_error = next(
        (
            line
            for line in lines
            if "predefined address pools" in line.lower() and "fully subnetted" in line.lower()
        ),
        None,
    )
    if subnet_error:
        return (
            "Docker cannot allocate another network for Launch Pad (all predefined address pools are exhausted). "
            "Remove unused Docker networks (for example, run `docker network prune` or delete old blockdag-testnet-network-* networks) "
            "and retry the launch."
        )
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


def _node_paths(base_dir: Path, label: str) -> Tuple[Path, Path]:
    """Return host data/log paths for a launchpad node, namespaced by label."""
    data_dir = base_dir / "bin" / label / "bdag" / "data"
    logs_dir = base_dir / "bin" / label / "bdag" / "logs"
    return data_dir, logs_dir


def _infer_node_number_from_path(path: Path) -> Optional[int]:
    """Guess the node number by looking at the install path or sibling nodes."""
    last = path.name
    match = re.search(r"(\d+)$", last)
    if match:
        return int(match.group(1))
    parent = path.parent
    numbers: List[int] = []
    for entry in parent.iterdir() if parent.exists() else []:
        if not entry.is_dir():
            continue
        m = re.search(r"(\d+)$", entry.name)
        if m:
            try:
                numbers.append(int(m.group(1)))
            except ValueError:
                continue
    return max(numbers) + 1 if numbers else None


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


def _port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
    except OSError:
        return True
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return False


def _find_available_port(used: set[int], start: int) -> int:
    port = start
    while port in used or _port_in_use(port):
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


def _prepare_ports(config: Dict, node_number: Optional[int] = None) -> Tuple[int, int, int, int, int]:
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
    if node_number:
        base_p2p = 38130 + max(0, node_number - 1)
        base_rpc = 18544 + 2 * max(0, node_number - 1)
        base_ws = base_rpc + 1
        base_peer = 18174 + max(0, node_number - 1)
        p2p = _find_available_port(used, base_p2p)
        rpc = _find_available_port(used, base_rpc)
        ws = _find_available_port(used, base_ws)
        peer = _find_available_port(used, base_peer)
        if rpc != base_rpc and rpc + 1 not in used:
            ws = rpc + 1
        peer_internal = base_peer
    elif config.get("autoPorts"):
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
    elif node_number:
        base_p2p = 38130 + max(0, node_number - 1)
        base_rpc = 18544 + 2 * max(0, node_number - 1)
        base_ws = base_rpc + 1
        base_peer = 18174 + max(0, node_number - 1)
        p2p = _find_available_port(used, base_p2p)
        rpc = _find_available_port(used, base_rpc)
        ws = _find_available_port(used, base_ws)
        peer = _find_available_port(used, base_peer)
        if rpc != base_rpc and rpc + 1 not in used:
            ws = rpc + 1
    else:
        override = _coerce_port(external_override, base_p2p)
        manual_ws = _coerce_port(config.get("wsPort"), base_ws)
        manual_peer = _coerce_port(peer_external_override, base_peer)
        conflict_ports = [
            port
            for port in (override, base_rpc, manual_ws, manual_peer)
            if port in used or _port_in_use(port)
        ]
        if conflict_ports:
            raise LaunchError("Selected ports are already in use")
        p2p = override
        rpc = base_rpc
        ws = manual_ws
        peer = manual_peer
    return p2p, rpc, ws, peer, peer_internal


def _deploy_helper_entrypoint(scripts_dir: Path, label: str) -> Path:
    if not HELPER_TEMPLATE.exists():
        raise LaunchError("Launchpad helper entrypoint template is missing; reinstall the node manager.")
    helper_name = f"entrypoint-{label}.sh"
    target = scripts_dir / helper_name
    target.write_text(HELPER_TEMPLATE.read_text(), encoding="utf-8")
    try:
        target.chmod(0o755)
    except Exception:
        pass
    return target


def _render_compose(
    source: Path,
    target: Path,
    label: str,
    p2p: int,
    rpc: int,
    ws: int,
    peer: int,
    peer_internal: int,
    data_dir: Path,
    logs_dir: Path,
    mining_address: str,
    helper_mount: Optional[str] = None,
):
    text = source.read_text()
    text = text.replace("blockdag-testnet-network", label)
    text = text.replace('- "38131:38131"', f'- "{p2p}:{p2p}"', 1)
    text = text.replace('- "18545:18545"', f'- "{rpc}:{rpc}"', 1)
    text = text.replace('- "18546:18546"', f'- "{ws}:{ws}"', 1)
    text = text.replace('- "18150:18150"', f'- "{peer}:{peer_internal}"', 1)
    text = re.sub(r"--rpclisten=0\.0\.0\.0:\d+", f"--rpclisten=0.0.0.0:{p2p}", text, count=1)
    text = re.sub(r"--http\.port=\d+", f"--http.port={rpc}", text, count=1)
    text = re.sub(r"--ws\.port=\d+", f"--ws.port={ws}", text, count=1)
    text = re.sub(r"ws://127\.0\.0\.1:\d+", f"ws://127.0.0.1:{ws}", text, count=1)
    text = text.replace("./bin/bdag/data:/bdag/data", f"./{data_dir.relative_to(source.parent)}:/bdag/data")
    text = text.replace("./bin/bdag/logs:/bdag/logs", f"./{logs_dir.relative_to(source.parent)}:/bdag/logs")
    if "--health=0.0.0.0:6061" not in text:
        text = text.replace(
            "--walletpass=test ",
            "--walletpass=test --health=0.0.0.0:6061 ",
            1,
        )
    if "${MINING_ADDRESS}" in text:
        text = text.replace("${MINING_ADDRESS}", mining_address)
    else:
        # Force mining address even if template had an empty placeholder.
        text = re.sub(r"--miningaddr=\S*", f"--miningaddr={mining_address}", text)
    image_line = f"image: {LAUNCHPAD_DEFAULT_IMAGE}"
    pattern = re.compile(r"image:\s+blockdagnetwork/awakening:[^\s]+", re.IGNORECASE)
    if pattern.search(text):
        text = pattern.sub(image_line, text, count=1)
    elif "blockdagnetwork/awakening" in text:
        text = text.replace("blockdagnetwork/awakening", LAUNCHPAD_DEFAULT_IMAGE, 1)
    text = text.replace(
        "      HEALTH_MIN_PEERS: 1\n",
        f"      HEALTH_MIN_PEERS: 1\n      PEER_PORT_INTERNAL: {peer_internal}\n",
        1,
    )
    if helper_mount:
        container_line = f"    container_name: {label}\n"
        replacement = container_line + '    entrypoint: ["/custom-entrypoint.sh"]\n'
        if container_line not in text:
            raise LaunchError("Failed to inject helper entrypoint into docker-compose template")
        text = text.replace(container_line, replacement, 1)
        volume_anchor = f"      - ./{logs_dir.relative_to(source.parent)}:/bdag/logs"
        if volume_anchor not in text:
            raise LaunchError("Failed to inject helper entrypoint mount into docker-compose template")
        text = text.replace(volume_anchor, volume_anchor + f"\n      - {helper_mount}", 1)
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
    _ensure_install_path_ready(install_path)
    install_path.mkdir(parents=True, exist_ok=True)
    scripts_dir = install_path / "blockdag-scripts"
    git_dir = scripts_dir / ".git"
    pruned_networks = _prune_orphan_blockdag_networks()
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
    compose_src = scripts_dir / "docker-compose.yml"
    if not compose_src.exists():
        # Restore the template from git to avoid stale/missing compose files on first pulls.
        try:
            _run_command(["git", "-C", str(scripts_dir), "checkout", "--", "docker-compose.yml"])
        except LaunchError:
            pass
    if not compose_src.exists():
        raise LaunchError("docker-compose template not found; try recloning blockdag-scripts")
    _rewrite_compose_image(compose_src, LAUNCHPAD_DEFAULT_IMAGE)
    node_number = _infer_node_number_from_path(install_path) or 1
    p2p_port, rpc_port, ws_port, peer_port, peer_internal = _prepare_ports(payload, node_number=node_number)
    compose_target = scripts_dir / f"docker-compose-{label}.yml"
    helper_script = _deploy_helper_entrypoint(scripts_dir, label)
    helper_mount = f"./{helper_script.name}:/custom-entrypoint.sh:ro"
    data_dir, logs_dir = _node_paths(scripts_dir, label)
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    _reset_launchpad_identity(data_dir)
    _render_compose(
        compose_src,
        compose_target,
        label,
        p2p_port,
        rpc_port,
        ws_port,
        peer_port,
        peer_internal,
        data_dir,
        logs_dir,
        wallet,
        helper_mount,
    )
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
        "prunedNetworks": pruned_networks,
    }
