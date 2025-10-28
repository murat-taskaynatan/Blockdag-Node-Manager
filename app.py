import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from collections import OrderedDict, deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, render_template, request


# ---------------------------------------------------------------------------
# Flask application setup
# ---------------------------------------------------------------------------
APP_START = time.time()
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

_log_level_name = (os.getenv("BDAG_LOG_LEVEL", "INFO") or "INFO").strip().upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
app.logger.setLevel(_log_level)

try:
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Runtime constants
# ---------------------------------------------------------------------------
SAMPLE_SEC = max(1, int(os.getenv("BDAG_SAMPLE_SEC", "5") or "5"))
WINDOW = max(12, int(os.getenv("BDAG_WINDOW", "240") or "240"))

DOCKER_BIN = shutil.which("docker")


# ---------------------------------------------------------------------------
# Remote RPC defaults
# ---------------------------------------------------------------------------
def _normalize_remote_url(url: Optional[str]) -> str:
    text = (url or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/")


PRIMARY_REMOTE_RPC_BASE = _normalize_remote_url("http://13.245.135.249:18545")
LEGACY_REMOTE_RPC_BASES = [
    _normalize_remote_url("https://rpc.awakening.bdagscan.com"),
]

DEFAULT_REMOTE_RPC_BASES = [
    PRIMARY_REMOTE_RPC_BASE,
    _normalize_remote_url("https://rpc.bdagscan.com"),
    *[base for base in LEGACY_REMOTE_RPC_BASES if base != PRIMARY_REMOTE_RPC_BASE],
]


def _parse_remote_rpc_bases(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    else:
        items = [part.strip() for part in str(raw).split(",")]
    bases: List[str] = []
    for item in items:
        normalized = _normalize_remote_url(item)
        if normalized and normalized not in bases:
            bases.append(normalized)
    return bases


ENV_REMOTE_RPC_BASES = _parse_remote_rpc_bases(
    os.getenv("BDAG_REMOTE_RPC_BASES", os.getenv("BDAG_REMOTE_RPC_BASE"))
)
DEFAULT_REMOTE_BASES = ENV_REMOTE_RPC_BASES or DEFAULT_REMOTE_RPC_BASES[:]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(:-([^}]*))?\}")


def _expand_env_placeholders(value: str) -> str:
    if not isinstance(value, str):
        return value

    def repl(match: re.Match) -> str:
        var = match.group(1)
        default = match.group(3)
        current = os.getenv(var)
        if current is None or current == "":
            return default or ""
        return current

    return _ENV_VAR_PATTERN.sub(repl, value)


def _normalize_rpc_endpoint(endpoint: Optional[str]) -> str:
    text = _expand_env_placeholders(endpoint or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/") or "http://127.0.0.1:18545"


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _slugify(name: str) -> str:
    text = (name or "").strip().lower()
    if not text:
        return "node"
    allowed = []
    for ch in text:
        if ch.isalnum():
            allowed.append(ch)
        elif ch in {"-", "_"}:
            allowed.append(ch)
        elif ch.isspace():
            allowed.append("-")
    slug = "".join(allowed).strip("-_")
    return slug or "node"


def _parse_docker_env(env_list: Iterable[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for entry in env_list or []:
        if not isinstance(entry, str):
            continue
        if "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _extract_rpc_credentials(env: Dict[str, str]) -> Tuple[str, str]:
    user = (
        env.get("BDAG_RPC_USER")
        or env.get("RPC_USER")
        or env.get("BDAG_RPC_USERNAME")
        or env.get("RPC_USERNAME")
        or ""
    )
    password = (
        env.get("BDAG_RPC_PASS")
        or env.get("BDAG_RPC_PASSWORD")
        or env.get("RPC_PASS")
        or env.get("RPC_PASSWORD")
        or ""
    )
    return user.strip(), password.strip()


def _resolve_container_rpc_base(info: dict, env: Dict[str, str]) -> Optional[str]:
    for key in (
        "BDAG_RPC_BASE",
        "BDAG_RPC_URL",
        "RPC_BASE",
        "RPC_URL",
        "RPC_ENDPOINT",
    ):
        value = env.get(key)
        if not value:
            continue
        normalized = _normalize_rpc_endpoint(value)
        try:
            parsed = urlparse(normalized)
        except Exception:
            parsed = None
        scheme = (parsed.scheme if parsed else "").lower()
        host = (parsed.hostname if parsed else "") or ""
        if scheme not in {"http", "https"}:
            # skip websocket or other schemes; fall back to port mapping
            continue
        if host in {"127.0.0.1", "localhost"}:
            # inside-container loopback; prefer mapped host port resolution
            continue
        return normalized
    ports = info.get("NetworkSettings", {}).get("Ports") or {}
    if not ports:
        ports = info.get("HostConfig", {}).get("PortBindings") or {}
    for port_key, bindings in ports.items():
        if not isinstance(port_key, str) or "/tcp" not in port_key:
            continue
        container_port = port_key.split("/")[0]
        if container_port not in {"18545", "8545", "4545"}:
            continue
        binding = bindings[0] if isinstance(bindings, list) and bindings else None
        if not isinstance(binding, dict):
            continue
        host_ip = binding.get("HostIp") or "127.0.0.1"
        host_port = binding.get("HostPort")
        if not host_port:
            continue
        if host_ip in {"0.0.0.0", "::", ""}:
            host_ip = "127.0.0.1"
        return f"http://{host_ip}:{host_port}"
    return None


def _list_docker_containers() -> List[str]:
    if not DOCKER_BIN:
        return []
    try:
        out = subprocess.check_output(
            [DOCKER_BIN, "ps", "-a", "--format", "{{.Names}}"],
            text=True,
            timeout=5,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _discover_docker_nodes() -> List[dict]:
    nodes: List[dict] = []
    if not DOCKER_BIN:
        return nodes
    for name in _list_docker_containers():
        try:
            inspect = subprocess.check_output(
                [DOCKER_BIN, "inspect", name],
                text=True,
                timeout=5,
            )
            data = json.loads(inspect)[0]
        except Exception:
            continue
        env = _parse_docker_env(data.get("Config", {}).get("Env") or [])
        rpc_base = _resolve_container_rpc_base(data, env)
        rpc_user, rpc_pass = _extract_rpc_credentials(env)
        remote_bases = _parse_remote_rpc_bases(
            env.get("BDAG_REMOTE_RPC_BASES")
            or env.get("REMOTE_RPC_BASES")
            or env.get("BDAG_REMOTE_RPC_BASE")
        )
        label = (
            env.get("BDAG_NODE_LABEL")
            or env.get("NODE_LABEL")
            or data.get("Name", "").lstrip("/")
            or name
        )
        nodes.append(
            {
                "id": _slugify(name),
                "label": label,
                "container": name,
                "rpc_base": rpc_base,
                "rpc_user": rpc_user,
                "rpc_pass": rpc_pass,
                "remote_rpc_bases": remote_bases,
            }
        )
    return nodes


# ---------------------------------------------------------------------------
# Node configuration & state
# ---------------------------------------------------------------------------
DEFAULT_NODE_SETTINGS = {
    "id": "primary",
    "label": "Primary Node",
    "rpc_base": os.getenv("BDAG_RPC_BASE", "http://127.0.0.1:18545"),
    "rpc_user": os.getenv("BDAG_RPC_USER", ""),
    "rpc_pass": os.getenv("BDAG_RPC_PASS", ""),
    "rpc_timeout": float(os.getenv("BDAG_RPC_TIMEOUT", "2.5") or "2.5"),
    "rpc_verify": _coerce_bool(os.getenv("BDAG_RPC_VERIFY"), False),
    "remote_rpc_bases": DEFAULT_REMOTE_BASES,
    "remote_rpc_method": os.getenv("BDAG_REMOTE_RPC_METHOD", "eth_blockNumber") or "eth_blockNumber",
    "remote_rpc_timeout": float(os.getenv("BDAG_REMOTE_RPC_TIMEOUT", "2.5") or "2.5"),
    "remote_rpc_verify": _coerce_bool(os.getenv("BDAG_REMOTE_RPC_VERIFY"), False),
    "container": os.getenv("BDAG_NODE_CONTAINER", "").strip(),
}

NODE_CONFIG_PATH = Path(
    os.getenv("BDAG_NODE_CONFIG_PATH")
    or (Path(__file__).resolve().parent / "config" / "nodes.json")
)


class NodeContext:
    """Per-node runtime state, metrics, and configuration."""

    def __init__(self, config: dict, *, auto_discovered: bool = False):
        merged = dict(DEFAULT_NODE_SETTINGS)
        if config:
            merged.update({k: v for k, v in config.items() if v is not None})

        self.id = str(merged.get("id") or "node").strip() or "node"
        self.label = str(merged.get("label") or self.id).strip() or self.id
        self.container = str(merged.get("container") or "").strip()

        self.rpc_base = _normalize_rpc_endpoint(merged.get("rpc_base"))
        self.rpc_user = str(merged.get("rpc_user") or "").strip()
        self.rpc_pass = str(merged.get("rpc_pass") or "").strip()
        self.rpc_timeout = float(merged.get("rpc_timeout", DEFAULT_NODE_SETTINGS["rpc_timeout"]))
        self.rpc_verify = _coerce_bool(merged.get("rpc_verify", DEFAULT_NODE_SETTINGS["rpc_verify"]))

        remote_candidates = merged.get("remote_rpc_bases")
        if not remote_candidates and merged.get("remote_rpc_base"):
            remote_candidates = [merged.get("remote_rpc_base")]
        remote_bases = _parse_remote_rpc_bases(remote_candidates)
        self.remote_rpc_bases = remote_bases or DEFAULT_NODE_SETTINGS["remote_rpc_bases"][:]
        self.remote_rpc_method = merged.get("remote_rpc_method") or DEFAULT_NODE_SETTINGS["remote_rpc_method"]
        self.remote_rpc_timeout = float(
            merged.get("remote_rpc_timeout", DEFAULT_NODE_SETTINGS["remote_rpc_timeout"])
        )
        self.remote_rpc_verify = _coerce_bool(
            merged.get("remote_rpc_verify", DEFAULT_NODE_SETTINGS["remote_rpc_verify"])
        )

        self.lock = threading.RLock()
        self.height_series: deque = deque(maxlen=WINDOW)
        self.remote_series: deque = deque(maxlen=WINDOW)
        self.peers_series: deque = deque(maxlen=WINDOW)
        self.last_metrics: Optional[dict] = None
        self.last_sample_ts: float = 0.0
        self.running: bool = False
        self.auto_discovered = auto_discovered

    def update_from_metadata(self, meta: dict) -> bool:
        changed = False
        if not meta:
            return changed
        label = meta.get("label")
        if label and label != self.label:
            self.label = label
            changed = True
        container = meta.get("container")
        if container and container != self.container:
            self.container = container
            changed = True
        rpc_base = meta.get("rpc_base")
        if rpc_base:
            normalized = _normalize_rpc_endpoint(rpc_base)
            if normalized != self.rpc_base:
                self.rpc_base = normalized
                changed = True
        user = meta.get("rpc_user")
        if user is not None and user != self.rpc_user:
            self.rpc_user = user
            changed = True
        password = meta.get("rpc_pass")
        if password is not None and password != self.rpc_pass:
            self.rpc_pass = password
            changed = True
        remote_candidates = meta.get("remote_rpc_bases") or meta.get("remote_rpc_base")
        if remote_candidates:
            bases = _parse_remote_rpc_bases(remote_candidates)
            if bases and bases != self.remote_rpc_bases:
                self.remote_rpc_bases = bases
                changed = True
        return changed

    def to_metadata(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "container": self.container,
            "rpc_base": self.rpc_base,
            "remote_rpc_bases": self.remote_rpc_bases[:],
            "auto_discovered": self.auto_discovered,
        }

    def _empty_metrics(self) -> dict:
        return {
            "local_height": 0,
            "remote_height": 0,
            "height_delta": 0,
            "peers": 0,
            "running": self.running,
            "uptime_seconds": None,
            "last_updated": int(time.time() * 1000),
        }

    def sample(self, *, force: bool = False) -> dict:
        now = time.time()
        interval = max(SAMPLE_SEC, 1)
        with self.lock:
            if (
                not force
                and self.last_sample_ts
                and (now - self.last_sample_ts) < interval
                and self.last_metrics is not None
            ):
                return dict(self.last_metrics)
        metrics, remote_series_value = _collect_node_metrics(self)
        with self.lock:
            self.last_sample_ts = now
            self.running = metrics["running"]
            self.last_metrics = dict(metrics)
            ts = metrics["last_updated"]
            self.height_series.append((ts, metrics["local_height"]))
            self.remote_series.append((ts, remote_series_value))
            self.peers_series.append((ts, metrics["peers"]))
            return dict(self.last_metrics)

    def snapshot(self, *, include_series: bool = False) -> dict:
        with self.lock:
            metrics = dict(self.last_metrics or self._empty_metrics())
            metrics.setdefault("running", self.running)
            if include_series:
                labels = [ts for ts, _ in self.height_series]
                local = [int(val) if val is not None else 0 for _, val in self.height_series]
                remote_lookup = {ts: val for ts, val in self.remote_series}
                remote = [
                    remote_lookup.get(ts) if remote_lookup.get(ts) is not None else None for ts in labels
                ]
                metrics["labels"] = labels
                metrics["local"] = local
                metrics["remote"] = remote
        return metrics


def _load_node_configs() -> tuple[List[dict], bool]:
    has_file = NODE_CONFIG_PATH.exists()
    if not has_file:
        return [], False
    try:
        content = NODE_CONFIG_PATH.read_text()
    except Exception as exc:
        try:
            app.logger.warning("Failed to read node config %s: %s", NODE_CONFIG_PATH, exc)
        except Exception:
            pass
        return [], False
    stripped = content.strip()
    if not stripped:
        return [], True
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "nodes" in parsed:
            parsed = parsed["nodes"]
        if isinstance(parsed, list):
            return [entry for entry in parsed if isinstance(entry, dict)], True
    except Exception as exc:
        try:
            app.logger.warning("Failed to parse node config %s: %s", NODE_CONFIG_PATH, exc)
        except Exception:
            pass
        return [], False
    return [], True


def _ensure_unique_id(base_id: str, existing: Iterable[str]) -> str:
    candidate = base_id
    counter = 2
    while candidate in existing:
        candidate = f"{base_id}-{counter}"
        counter += 1
    return candidate


def _initialise_nodes() -> "OrderedDict[str, NodeContext]":
    nodes = OrderedDict()
    configs, explicit = _load_node_configs()
    if not configs and not explicit:
        configs = [dict(DEFAULT_NODE_SETTINGS)]
    for entry in configs:
        entry = dict(entry)
        base_id = entry.get("id") or entry.get("container") or DEFAULT_NODE_SETTINGS["id"]
        base_id = _slugify(str(base_id))
        entry["id"] = _ensure_unique_id(base_id, nodes.keys())
        try:
            ctx = NodeContext(entry)
        except Exception as exc:
            try:
                app.logger.warning("Skipping invalid node config %s: %s", entry, exc)
            except Exception:
                pass
            continue
        nodes[ctx.id] = ctx
    return nodes


NODES: "OrderedDict[str, NodeContext]" = _initialise_nodes()


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------
LOCAL_HEIGHT_METHODS = ["dag_blockNumber", "bdag_blockNumber", "eth_blockNumber", "getblockcount"]
PEER_COUNT_METHODS = ["net_peerCount", "peer_count"]


def _rpc_call(base: str, method: str, params: Optional[list], *, timeout: float, auth: Optional[Tuple[str, str]], verify: bool):
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10_000, "method": method, "params": params or []}
    response = requests.post(base, json=payload, timeout=timeout, auth=auth, verify=verify)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def _parse_height_value(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("0x"):
            try:
                return int(text, 16)
            except Exception:
                return None
        try:
            return int(text)
        except Exception:
            return None
    try:
        return int(value)
    except Exception:
        return None


def _fetch_local_height(ctx: NodeContext) -> Optional[int]:
    auth = (ctx.rpc_user, ctx.rpc_pass) if (ctx.rpc_user or ctx.rpc_pass) else None
    for method in LOCAL_HEIGHT_METHODS:
        try:
            result = _rpc_call(
                ctx.rpc_base,
                method,
                [],
                timeout=ctx.rpc_timeout,
                auth=auth,
                verify=ctx.rpc_verify,
            )
            height = _parse_height_value(result)
            if height is not None:
                return height
        except Exception:
            continue
    return None


def _fetch_peer_count(ctx: NodeContext) -> Optional[int]:
    auth = (ctx.rpc_user, ctx.rpc_pass) if (ctx.rpc_user or ctx.rpc_pass) else None
    for method in PEER_COUNT_METHODS:
        try:
            result = _rpc_call(
                ctx.rpc_base,
                method,
                [],
                timeout=ctx.rpc_timeout,
                auth=auth,
                verify=ctx.rpc_verify,
            )
            peers = _parse_height_value(result)
            if peers is not None:
                return peers
        except Exception:
            continue
    try:
        info = _rpc_call(
            ctx.rpc_base,
            "bdag_getPeerInfo",
            [],
            timeout=ctx.rpc_timeout,
            auth=auth,
            verify=ctx.rpc_verify,
        )
        if isinstance(info, list):
            return len(info)
        if isinstance(info, dict):
            peers_field = info.get("peers")
            if isinstance(peers_field, list):
                return len(peers_field)
            for key in ("count", "activeCount", "total", "numPeers"):
                if key in info:
                    try:
                        return int(info[key])
                    except Exception:
                        continue
    except Exception:
        pass
    return None


def _fetch_remote_height(ctx: NodeContext) -> Optional[int]:
    if not ctx.remote_rpc_bases:
        return None
    for base in ctx.remote_rpc_bases:
        try:
            result = _rpc_call(
                base,
                ctx.remote_rpc_method,
                [],
                timeout=ctx.remote_rpc_timeout,
                auth=None,
                verify=ctx.remote_rpc_verify,
            )
            height = _parse_height_value(result)
            if height is not None:
                return height
        except Exception:
            continue
    return None


def _parse_docker_timestamp(value: str) -> Optional[float]:
    raw = (value or "").strip()
    if not raw or raw == "0001-01-01T00:00:00Z":
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        main, rest = raw.split(".", 1)
        tz_sign = "+" if "+" in rest else "-" if "-" in rest else None
        if tz_sign:
            frac, tz = rest.split(tz_sign, 1)
            tz = tz_sign + tz
        else:
            frac, tz = rest, ""
        digits = "".join(ch for ch in frac if ch.isdigit())
        digits = (digits + "000000")[:6]
        raw = f"{main}.{digits}{tz}"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _container_state(name: str) -> Tuple[bool, bool, Optional[float]]:
    if not name or not DOCKER_BIN:
        return False, False, None
    try:
        out = subprocess.check_output(
            [DOCKER_BIN, "inspect", "-f", "{{.State.Running}}|{{.State.StartedAt}}", name],
            text=True,
            timeout=5,
        )
    except subprocess.CalledProcessError:
        return False, False, None
    except Exception:
        return False, False, None
    raw = (out or "").strip()
    if not raw:
        return False, False, None
    if "|" in raw:
        running_text, started_text = raw.split("|", 1)
    else:
        running_text, started_text = raw, ""
    running_text = running_text.strip().lower()
    exists = running_text in {"true", "false"}
    running = running_text == "true"
    started_ts = _parse_docker_timestamp(started_text)
    if not running:
        started_ts = None
    if not exists:
        return False, False, None
    return True, running, started_ts


def _collect_node_metrics(ctx: NodeContext) -> Tuple[dict, Optional[int]]:
    now_ms = int(time.time() * 1000)
    try:
        local_height = _fetch_local_height(ctx)
    except Exception:
        local_height = None
    try:
        remote_height = _fetch_remote_height(ctx)
    except Exception:
        remote_height = None
    try:
        peers = _fetch_peer_count(ctx)
    except Exception:
        peers = None
    exists, running, started_ts = _container_state(ctx.container)
    uptime_seconds: Optional[int] = None
    if exists and running and started_ts is not None:
        now_sec = now_ms / 1000
        uptime_seconds = max(0, int(now_sec - started_ts))
    local_val = int(local_height) if isinstance(local_height, int) and local_height >= 0 else 0
    remote_val = int(remote_height) if isinstance(remote_height, int) and remote_height >= 0 else None
    peers_val = int(peers) if isinstance(peers, int) and peers >= 0 else 0
    remote_display = remote_val if remote_val is not None else local_val
    effective_running = running if exists else bool(not ctx.container)
    metrics = {
        "local_height": local_val,
        "remote_height": remote_display,
        "height_delta": int(remote_display - local_val),
        "peers": peers_val,
        "running": effective_running,
        "uptime_seconds": uptime_seconds,
        "last_updated": now_ms,
    }
    return metrics, remote_val


# ---------------------------------------------------------------------------
# Fleet management & discovery
# ---------------------------------------------------------------------------
_discovery_lock = threading.Lock()


def refresh_discovered_nodes() -> Tuple[List[str], List[str], List[str]]:
    added: List[str] = []
    removed: List[str] = []
    updated: List[str] = []
    if not DOCKER_BIN:
        return added, removed, updated
    with _discovery_lock:
        discovered = _discover_docker_nodes()
        seen_containers = {entry.get("container") for entry in discovered if entry.get("container")}

        for entry in discovered:
            container = entry.get("container")
            if not container:
                continue
            target_ctx: Optional[NodeContext] = None
            for ctx in NODES.values():
                if ctx.container == container:
                    target_ctx = ctx
                    break
            if target_ctx:
                if entry.get("rpc_base") and target_ctx.update_from_metadata(entry):
                    updated.append(target_ctx.id)
                continue
            if not entry.get("rpc_base"):
                # don't create new nodes without rpc info (likely stopped before ever running)
                continue
            new_id = entry.get("id") or _slugify(container)
            new_id = _ensure_unique_id(new_id, NODES.keys())
            entry = dict(entry)
            entry["id"] = new_id
            ctx = NodeContext(entry, auto_discovered=True)
            NODES[ctx.id] = ctx
            added.append(ctx.id)

        for node_id, ctx in list(NODES.items()):
            if ctx.auto_discovered and ctx.container and ctx.container not in seen_containers:
                removed.append(node_id)
                NODES.pop(node_id, None)

    return added, removed, updated


def _fleet_summary(nodes: List[dict]) -> dict:
    count = len(nodes)
    running = sum(1 for node in nodes if node.get("status", {}).get("running"))
    offline = max(count - running, 0)
    local_heights = [
        node.get("status", {}).get("local_height") or 0 for node in nodes
    ]
    remote_heights = [
        node.get("status", {}).get("remote_height") or 0 for node in nodes
    ]
    summary = {
        "count": count,
        "running": running,
        "offline": offline,
        "max_local_height": max(local_heights) if local_heights else 0,
        "max_remote_height": max(remote_heights) if remote_heights else 0,
        "timestamp": time.time(),
    }
    return summary


def _resolve_node(node_id: Optional[str]) -> NodeContext:
    if not NODES:
        abort(404, description="no nodes configured")
    if node_id:
        ctx = NODES.get(node_id)
        if not ctx:
            abort(404, description=f"node '{node_id}' not found")
        return ctx
    return next(iter(NODES.values()))


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
APP_VERSION = os.getenv("BDAG_MANAGER_VERSION", "v1.0.0").strip() or "v1.0.0"


@app.route("/healthz")
def healthz():
    return "ok\n", 200, {"content-type": "text/plain; charset=utf-8"}


@app.route("/")
@app.route("/node-manager")
def node_manager_view():
    return render_template("node_manager.html", app_version=APP_VERSION, app_version_display=APP_VERSION)


@app.route("/api/node-manager/nodes")
def api_node_manager_nodes():
    nodes_payload = []
    for ctx in NODES.values():
        ctx.sample()
        nodes_payload.append(
            {
                "id": ctx.id,
                "label": ctx.label,
                "container": ctx.container,
                "auto_discovered": bool(ctx.auto_discovered),
                "status": ctx.snapshot(include_series=False),
            }
        )
    summary = _fleet_summary(nodes_payload) if nodes_payload else {
        "count": 0,
        "running": 0,
        "offline": 0,
        "max_local_height": 0,
        "max_remote_height": 0,
        "timestamp": time.time(),
    }
    return jsonify({"nodes": nodes_payload, "summary": summary})


@app.route("/api/node-manager/metrics")
def api_node_manager_metrics():
    nodes_param = request.args.get("nodes", "")
    if nodes_param:
        node_ids = [item.strip() for item in nodes_param.split(",") if item.strip()]
    else:
        node_ids = list(NODES.keys())
    response = {}
    for node_id in node_ids:
        ctx = NODES.get(node_id)
        if not ctx:
            continue
        ctx.sample(force=True)
        response[ctx.id] = ctx.snapshot(include_series=True)
    return jsonify({"nodes": response, "timestamp": time.time()})


@app.route("/api/node-manager/discover", methods=["POST"])
def api_node_manager_discover():
    added, removed, updated = refresh_discovered_nodes()
    return jsonify(
        {
            "ok": True,
            "added": added,
            "removed": removed,
            "updated": updated,
            "count": len(NODES),
        }
    )


def docker_action(container: str, action: str) -> dict:
    if not container:
        return {"ok": False, "error": "missing container name"}
    if not DOCKER_BIN:
        return {"ok": False, "error": "docker binary not available"}
    try:
        proc = subprocess.run(
            [DOCKER_BIN, action, container],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return {"ok": True, "output": output}
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        return {"ok": False, "error": message}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.route("/api/control", methods=["POST"])
def api_control():
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()
    node_id = body.get("node")
    ctx = None
    if node_id:
        ctx = NODES.get(str(node_id))
        if ctx is None:
            abort(404, description=f"node '{node_id}' not found")
    target = ctx or _resolve_node(None)
    container = (body.get("container") or body.get("name") or target.container or "").strip()

    mapping = {
        "docker_start": "start",
        "docker_stop": "stop",
        "docker_restart": "restart",
    }
    if action in mapping:
        result = docker_action(container, mapping[action])
        result["node"] = target.id
        return jsonify(result), (200 if result.get("ok") else 400)

    return jsonify({"ok": False, "error": f"unsupported action '{action}'"}), 400


# ---------------------------------------------------------------------------
# Application metadata
# ---------------------------------------------------------------------------
@app.route("/api/info")
def api_info():
    return jsonify(
        {
            "version": APP_VERSION,
            "start_time": APP_START,
            "node_count": len(NODES),
        }
    )


# ---------------------------------------------------------------------------
# Ensure at least one discovery pass on startup
# ---------------------------------------------------------------------------
try:
    refresh_discovered_nodes()
except Exception:
    pass
