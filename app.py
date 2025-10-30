import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, getcontext
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

getcontext().prec = 50


# ---------------------------------------------------------------------------
# Runtime constants
# ---------------------------------------------------------------------------
SAMPLE_SEC = max(1, int(os.getenv("BDAG_SAMPLE_SEC", "5") or "5"))
WINDOW = max(12, int(os.getenv("BDAG_WINDOW", "240") or "240"))

_docker_override = os.getenv("BDAG_DOCKER_BIN", "").strip()
DOCKER_BIN = shutil.which("docker")
if _docker_override:
    override_path = Path(_docker_override).expanduser()
    if override_path.exists() and os.access(override_path, os.X_OK):
        DOCKER_BIN = str(override_path)
    else:
        candidate = shutil.which(_docker_override)
        if candidate:
            DOCKER_BIN = candidate
if not DOCKER_BIN:
    for candidate in ("/usr/bin/docker", "/usr/local/bin/docker", "/bin/docker", "/snap/bin/docker"):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            DOCKER_BIN = candidate
            break

LOG_ERROR_THRESHOLD = max(1, int(os.getenv("BDAG_LOG_ERROR_THRESHOLD", "10") or "10"))
LOG_ERROR_CHECK_SEC = max(1.0, float(os.getenv("BDAG_LOG_ERROR_CHECK_SEC", "15") or "15"))
LOG_ERROR_RESTART_COOLDOWN_SEC = max(
    30.0,
    float(
        os.getenv("BDAG_LOG_ERROR_COOLDOWN_SEC", os.getenv("BDAG_LOG_ERROR_RESTART_COOLDOWN_SEC", "300"))
        or 300
    ),
)
LOG_ERROR_TAIL = max(10, min(int(os.getenv("BDAG_LOG_ERROR_TAIL", "80") or "80"), 200))
LOG_ERROR_PATTERN = re.compile(r"\berror\b", re.IGNORECASE)

LIVENESS_RECOVER_COOLDOWN_SEC = max(
    60.0, float(os.getenv("BDAG_LIVENESS_RECOVER_COOLDOWN_SEC", "900") or "900")
)
_liveness_patterns_raw = [
    part.strip().lower()
    for part in str(os.getenv("BDAG_LIVENESS_RECOVER_PATTERNS", "") or "").split(",")
    if part.strip()
]
if _liveness_patterns_raw:
    LIVENESS_FAILSAFE_PATTERNS = tuple(dict.fromkeys(_liveness_patterns_raw))
else:
    LIVENESS_FAILSAFE_PATTERNS = (
        "liveness probe exceeded timeout",
        "liveness probe failed",
        "forcing shutdown url=http://127.0.0.1:6061/healthz",
    )

LOG_CACHE_SEC = max(1.0, float(os.getenv("BDAG_LOG_CACHE_SEC", "2") or "2"))
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_POLICY_LOCK = threading.Lock()
_LOG_POLICY_STATE: Dict[str, Dict[str, object]] = {}
_RECENT_LOGS_CACHE: Dict[Tuple[str, int], Dict[str, object]] = {}


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

WEI_PER_BDAG = Decimal("1000000000000000000")
WALLET_BALANCE_CACHE_SEC = max(0.0, float(os.getenv("BDAG_BALANCE_CACHE_SEC", "60") or "60"))
_wallet_address_cache: Dict[str, object] = {"path": None, "mtime": 0.0, "address": None}
_wallet_balance_cache: Dict[str, object] = {"ts": 0.0, "data": None}



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


DEFAULT_RPC_FALLBACK = "https://rpc.awakening.bdagscan.com"


def _normalize_rpc_endpoint(endpoint: Optional[str]) -> str:
    text = _expand_env_placeholders(endpoint or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/") or DEFAULT_RPC_FALLBACK


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



SETTINGS_PATH = Path(__file__).resolve().parent / "config" / "settings.json"
SNAPSHOT_MAX_DEFAULT = max(0, int(os.getenv("BDAG_SNAPSHOT_MAX", "0") or 0))
SNAPSHOT_MAX = SNAPSHOT_MAX_DEFAULT

DEFAULT_SETTINGS: Dict[str, object] = {
    "liveness_auto_recover": False,
    "auto_restart_on_error": False,
    "display_wallet_balance": _coerce_bool(os.getenv("BDAG_WALLET_DISPLAY", "0"), False),
    "snapshot_max": SNAPSHOT_MAX_DEFAULT,
    "wallet_address": str(os.getenv("BDAG_WALLET_ADDRESS", "")).strip(),
}
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE: Dict[str, object] = {}


def _coerce_setting(key: str, value):
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, bool):
        return _coerce_bool(value, default)
    if isinstance(default, int):
        try:
            coerced = int(value)
        except Exception:
            return default
        return max(0, coerced)
    if isinstance(default, str):
        return str(value or "").strip()
    return value if value is not None else default


def _apply_runtime_settings(settings: Dict[str, object]) -> None:
    global SNAPSHOT_MAX
    snapshot_limit = settings.get("snapshot_max")
    if isinstance(snapshot_limit, int) and snapshot_limit >= 0:
        SNAPSHOT_MAX = snapshot_limit
    else:
        SNAPSHOT_MAX = SNAPSHOT_MAX_DEFAULT
    override_address = str(settings.get("wallet_address") or "").strip()
    if override_address:
        _wallet_address_cache.update({"path": "settings", "mtime": time.time(), "address": override_address})
    elif _wallet_address_cache.get("path") == "settings":
        _wallet_address_cache.update({"path": None, "mtime": 0.0, "address": None})
    _wallet_balance_cache.update({"ts": 0.0, "data": None})


def _load_settings_file() -> Dict[str, bool]:
    merged = DEFAULT_SETTINGS.copy()
    if SETTINGS_PATH.exists():
        try:
            with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            app.logger.warning("failed to read settings file %s: %s", SETTINGS_PATH, exc)
            payload = {}
        if isinstance(payload, dict):
            for key, default in DEFAULT_SETTINGS.items():
                if key in payload:
                    merged[key] = _coerce_setting(key, payload[key])
    _apply_runtime_settings(merged)
    return merged


def get_settings() -> Dict[str, object]:
    with _SETTINGS_LOCK:
        if not _SETTINGS_CACHE:
            _SETTINGS_CACHE.update(_load_settings_file())
        return dict(_SETTINGS_CACHE)


def update_settings(updates: Dict[str, object]) -> Dict[str, object]:
    filtered: Dict[str, object] = {}
    for key, default in DEFAULT_SETTINGS.items():
        if key in updates:
            filtered[key] = _coerce_setting(key, updates[key])
    if not filtered:
        return get_settings()
    with _SETTINGS_LOCK:
        if not _SETTINGS_CACHE:
            _SETTINGS_CACHE.update(_load_settings_file())
        _SETTINGS_CACHE.update(filtered)
        _apply_runtime_settings(_SETTINGS_CACHE)
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with SETTINGS_PATH.open("w", encoding="utf-8") as handle:
                json.dump(_SETTINGS_CACHE, handle, indent=2, sort_keys=True)
        except Exception as exc:
            app.logger.error("failed to persist settings file %s: %s", SETTINGS_PATH, exc)
        return dict(_SETTINGS_CACHE)



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
    normalized = None
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


def _wallet_candidate_paths() -> List[Path]:
    candidates: List[Path] = []
    env_path = os.getenv("BDAG_WALLET_FILE")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            Path.home() / "blockdag-scripts" / "wallet.txt",
            Path("/home/node/blockdag/blockdag-scripts/wallet.txt"),
            Path("/home/node/wallet.txt"),
            Path(__file__).resolve().parent.parent / "wallet.txt",
        ]
    )
    seen = set()
    unique: List[Path] = []
    for path in candidates:
        if not path:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


# ---------------------------------------------------------------------------
# Snapshot management (adapted from dashboard backup module)
# ---------------------------------------------------------------------------


def _expanded_path(raw: Optional[str], default: str) -> Path:
    base = raw if raw else default
    try:
        return Path(base).expanduser().resolve()
    except Exception:
        return Path(base).expanduser()


def _normalize_path(value) -> Optional[Path]:
    if value is None:
        return None
    try:
        path = Path(value).expanduser()
    except Exception:
        return None
    try:
        return path.resolve()
    except Exception:
        return path


SNAPSHOT_DATA_DIR = _expanded_path(
    os.getenv("BDAG_SNAPSHOT_DATA_DIR"),
    "/home/node/blockdag/blockdag-scripts/bin/bdag/data",
)
SNAPSHOT_DIR = _expanded_path(os.getenv("BDAG_SNAPSHOT_DIR"), os.path.expanduser("~/backups"))
SNAPSHOT_PREFIX = (os.getenv("BDAG_SNAPSHOT_PREFIX", "bdag.chaindata") or "bdag.chaindata").strip() or "bdag.chaindata"
SNAPSHOT_SUFFIX = (os.getenv("BDAG_SNAPSHOT_SUFFIX", ".tar") or ".tar").strip()
LEGACY_SNAPSHOT_PATTERNS = [
    "blockdag-chaindata-*.tar.gz",
    f"{SNAPSHOT_PREFIX}-*.tar.gz",
]

_SNAPSHOT_DIR_LOCK = threading.Lock()
_SNAPSHOT_JOB_LOCK = threading.Lock()
_SNAPSHOT_JOB_STATE: Dict[str, object] = {
    "active": False,
    "status": "idle",
    "message": "",
    "details": {},
    "started": None,
    "ended": None,
}


def _estimate_dir_size_bytes(directory: Optional[Path]) -> int:
    normalized = _normalize_path(directory)
    if not normalized or not normalized.exists():
        return 0
    size: Optional[int] = None
    try:
        out = subprocess.check_output(
            ["du", "-sb", str(normalized)],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        parts = out.strip().split()
        if parts:
            size = max(int(parts[0]), 0)
    except Exception:
        size = None
    if size and size > 0:
        return size
    total = 0
    try:
        for root, _, files in os.walk(normalized):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except Exception:
                    continue
    except Exception:
        total = max(total, 0)
    if total > 0:
        return total
    if DOCKER_BIN:
        try:
            out = subprocess.check_output(
                [
                    DOCKER_BIN,
                    "run",
                    "--rm",
                    "--init",
                    "-u",
                    "0",
                    "-v",
                    f"{normalized}:/data:ro",
                    "busybox",
                    "du",
                    "-sb",
                    "/data",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=300,
            )
            parts = out.strip().split()
            if parts:
                docker_size = max(int(parts[0]), 0)
                if docker_size > 0:
                    return docker_size
        except Exception:
            pass
    return total


def _snapshot_progress_update(payload: Optional[Dict[str, object]]) -> None:
    with _SNAPSHOT_JOB_LOCK:
        if not payload:
            _SNAPSHOT_JOB_STATE.pop("progress", None)
        else:
            progress = dict(_SNAPSHOT_JOB_STATE.get("progress") or {})
            progress.update(payload)
            _SNAPSHOT_JOB_STATE["progress"] = progress


def _run_command_with_progress(command: List[str], dest_path: Path, *, total_bytes: int, started: float) -> Tuple[str, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    last_emit = 0.0
    try:
        while True:
            retcode = process.poll()
            now = time.time()
            emit = now - last_emit >= 0.8 or retcode is not None
            bytes_written = 0
            if dest_path.exists():
                try:
                    bytes_written = max(dest_path.stat().st_size, 0)
                except Exception:
                    bytes_written = 0
            elapsed = max(now - started, 0.0)
            pct = None
            speed = 0.0
            eta = None
            if total_bytes > 0:
                pct = max(0.0, min(100.0, (bytes_written / total_bytes) * 100.0))
                if elapsed > 0:
                    speed = max(bytes_written / elapsed, 0.0)
                    remaining = max(total_bytes - bytes_written, 0)
                    if speed > 0:
                        eta = remaining / speed
            elif elapsed > 0:
                speed = max(bytes_written / elapsed, 0.0)
            if emit:
                _snapshot_progress_update(
                    {
                        "bytes_written": bytes_written,
                        "total_bytes": total_bytes,
                        "pct": pct,
                        "speed_bytes": speed,
                        "eta_seconds": eta,
                        "updated": now,
                        "path": str(dest_path),
                        "started": started,
                    }
                )
                last_emit = now
            if retcode is not None:
                stdout, stderr = process.communicate()
                if retcode != 0:
                    raise RuntimeError(stderr or stdout or f"Command exited with status {retcode}")
                final_now = time.time()
                final_bytes = 0
                if dest_path.exists():
                    try:
                        final_bytes = max(dest_path.stat().st_size, 0)
                    except Exception:
                        final_bytes = bytes_written
                final_pct = 100.0 if total_bytes > 0 else pct
                final_speed = speed
                if elapsed > 0 and final_bytes > 0:
                    final_speed = max(final_bytes / max(final_now - started, elapsed, 1e-6), 0.0)
                _snapshot_progress_update(
                    {
                        "bytes_written": final_bytes,
                        "total_bytes": total_bytes,
                        "pct": final_pct,
                        "speed_bytes": final_speed,
                        "eta_seconds": 0.0 if total_bytes > 0 else None,
                        "updated": final_now,
                        "path": str(dest_path),
                        "started": started,
                    }
                )
                return stdout or "", stderr or ""
            time.sleep(1.0)
    finally:
        try:
            process.stdout and process.stdout.close()
        except Exception:
            pass
        try:
            process.stderr and process.stderr.close()
        except Exception:
            pass


def _parse_snapshot_height(name: str) -> Optional[int]:
    if not name:
        return None
    match = re.search(r"\.(\d+)\.tar(?:\.gz)?$", name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _select_snapshot_for_restore() -> Optional[Path]:
    directory = _ensure_snapshot_dir()
    snapshots = list_snapshots()
    directory = _ensure_snapshot_dir()
    best_path: Optional[Path] = None
    best_height = -1
    fallback: Optional[Path] = None
    for entry in snapshots:
        name = entry.get("name")
        if not name:
            continue
        path = directory / name
        if not path.exists():
            continue
        height = _parse_snapshot_height(name)
        if height is not None and height > best_height:
            best_height = height
            best_path = path
        if fallback is None:
            fallback = path
    return best_path or fallback


def _stop_container(name: Optional[str], timeout: int = 90) -> bool:
    if not name or not DOCKER_BIN:
        return False
    exists, running, _ = _container_state(name)
    if not exists or not running:
        return False
    try:
        subprocess.run(
            [DOCKER_BIN, "stop", "-t", str(timeout), name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or str(exc)).strip())


def _start_container(name: Optional[str]) -> bool:
    if not name or not DOCKER_BIN:
        return False
    exists, running, _ = _container_state(name)
    if not exists:
        return False
    if running:
        return True
    try:
        subprocess.run(
            [DOCKER_BIN, "start", name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or str(exc)).strip())


def _collect_home_dirs(primary_home: Optional[Path] = None) -> List[Path]:
    homes: List[Path] = []
    seen: set[str] = set()
    candidates: List[Optional[Path]] = [primary_home]
    try:
        env_home = Path(os.getenv("HOME")) if os.getenv("HOME") else None
    except Exception:
        env_home = None
    if env_home:
        candidates.append(env_home)
    default_home = Path.home()
    candidates.append(default_home)
    homes_root = Path("/home")
    candidates.append(homes_root)
    try:
        if homes_root.exists():
            for entry in homes_root.iterdir():
                if entry.is_dir():
                    candidates.append(entry)
    except Exception:
        pass
    media_root = Path("/media")
    candidates.append(media_root)
    try:
        if media_root.exists():
            for entry in media_root.iterdir():
                if entry.is_dir():
                    candidates.append(entry)
                    try:
                        for sub in entry.iterdir():
                            if sub.is_dir():
                                candidates.append(sub)
                    except Exception:
                        continue
    except Exception:
        pass
    for candidate in candidates:
        normalized = _normalize_path(candidate)
        if not normalized or not normalized.exists():
            continue
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        homes.append(normalized)
    return homes


def _candidate_snapshot_dirs() -> List[Path]:
    candidates: List[Path] = []
    seen: set[str] = set()

    def enqueue(path: Optional[Path]) -> None:
        normalized = _normalize_path(path)
        if not normalized:
            return
        key = str(normalized)
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    enqueue(SNAPSHOT_DIR)
    enqueue(SNAPSHOT_DIR.parent if SNAPSHOT_DIR else None)
    home_dirs = _collect_home_dirs(Path.home())
    for home_dir in home_dirs:
        enqueue(home_dir / "backups")
        enqueue(home_dir / "blockdag-scripts" / "backups")
        enqueue(home_dir / "blockdag" / "backups")
    media_root = Path("/media")
    try:
        if media_root.exists():
            enqueue(media_root / "backups")
            for entry in media_root.iterdir():
                if not entry.is_dir():
                    continue
                enqueue(entry / "backups")
                enqueue(entry / "blockdag" / "backups")
                enqueue(entry / "blockdag-scripts" / "backups")
                try:
                    for sub in entry.iterdir():
                        if not sub.is_dir():
                            continue
                        enqueue(sub / "backups")
                except Exception:
                    continue
    except Exception:
        pass
    return candidates


def _snapshot_patterns() -> List[str]:
    patterns = [f"{SNAPSHOT_PREFIX}*.tar"]
    patterns.extend(LEGACY_SNAPSHOT_PATTERNS)
    return patterns


def _ensure_snapshot_dir() -> Path:
    with _SNAPSHOT_DIR_LOCK:
        directory = SNAPSHOT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory


def list_snapshots() -> List[dict]:
    directory = _ensure_snapshot_dir()
    patterns = _snapshot_patterns()
    files: List[Path] = []
    for pattern in patterns:
        try:
            files.extend(directory.glob(pattern))
        except Exception:
            continue
    if not files:
        for candidate in _candidate_snapshot_dirs():
            if candidate == directory:
                continue
            tmp: List[Path] = []
            for pattern in patterns:
                try:
                    tmp.extend(candidate.glob(pattern))
                except Exception:
                    continue
            if tmp:
                if _update_snapshot_dir(candidate):
                    directory = candidate
                files = tmp
                break
    files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    snapshots: List[dict] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshots.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return snapshots


def _prune_snapshots() -> None:
    if SNAPSHOT_MAX <= 0:
        return
    snapshots = list_snapshots()
    for entry in snapshots[SNAPSHOT_MAX:]:
        name = entry.get("name")
        if not name:
            continue
        target = _normalize_path(SNAPSHOT_DIR / name)
        if not target or not target.exists():
            continue
        try:
            target.unlink()
        except Exception:
            app.logger.warning("Failed to prune snapshot %s", name, exc_info=True)


def _scan_snapshot_locations() -> List[dict]:
    results: List[dict] = []
    patterns = _snapshot_patterns()
    for directory in _candidate_snapshot_dirs():
        if not directory.exists() or not directory.is_dir():
            continue
        entries: List[Path] = []
        for pattern in patterns:
            try:
                entries.extend(directory.glob(pattern))
            except Exception:
                continue
        if not entries:
            continue
        count = 0
        latest_mtime = 0.0
        latest_name = ""
        total_size = 0
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            count += 1
            total_size += stat.st_size
            if stat.st_mtime > latest_mtime:
                latest_mtime = stat.st_mtime
                latest_name = entry.name
        if count == 0:
            continue
        try:
            latest_iso = datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()
        except Exception:
            latest_iso = None
        results.append(
            {
                "path": str(directory),
                "count": count,
                "latest": latest_iso,
                "latest_name": latest_name,
                "total_size": total_size,
                "latest_ts": latest_mtime,
            }
        )
    results.sort(key=lambda item: item.get("latest_ts", 0.0), reverse=True)
    for item in results:
        item.pop("latest_ts", None)
    return results


def _snapshot_job_snapshot() -> Dict[str, object]:
    with _SNAPSHOT_JOB_LOCK:
        progress = _SNAPSHOT_JOB_STATE.get("progress")
        progress_copy = dict(progress) if isinstance(progress, dict) else None
        return {
            "active": bool(_SNAPSHOT_JOB_STATE.get("active")),
            "status": _SNAPSHOT_JOB_STATE.get("status"),
            "message": _SNAPSHOT_JOB_STATE.get("message"),
            "details": _SNAPSHOT_JOB_STATE.get("details", {}) or {},
            "started": _SNAPSHOT_JOB_STATE.get("started"),
            "ended": _SNAPSHOT_JOB_STATE.get("ended"),
            "warnings": _SNAPSHOT_JOB_STATE.get("warnings", []) or [],
            "progress": progress_copy,
        }


def _update_snapshot_dir(new_dir: Path) -> bool:
    normalized = _normalize_path(new_dir)
    if not normalized:
        return False
    with _SNAPSHOT_DIR_LOCK:
        global SNAPSHOT_DIR
        if SNAPSHOT_DIR == normalized:
            return False
        SNAPSHOT_DIR = normalized
    return True


def _run_snapshot_job(details: Dict[str, object]) -> None:
    dest_name: Optional[str] = None
    dest_path: Optional[Path] = None
    container = (details or {}).get("container") if details else None
    restart_required = False
    try:
        locations = _scan_snapshot_locations()
        if locations:
            primary_path = locations[0].get("path")
            if primary_path:
                _update_snapshot_dir(Path(primary_path))
        directory = _ensure_snapshot_dir()
        data_dir = _normalize_path(details.get("data_dir")) if details else None
        if not data_dir or not data_dir.exists() or not data_dir.is_dir():
            data_dir = _normalize_path(SNAPSHOT_DATA_DIR)
        if not data_dir or not data_dir.exists() or not data_dir.is_dir():
            raise RuntimeError(f"Snapshot data directory not found: {data_dir}")
        if container and DOCKER_BIN:
            try:
                restart_required = _stop_container(container)
            except Exception as exc:
                raise RuntimeError(f"Failed to stop container {container}: {exc}")
        total_bytes = _estimate_dir_size_bytes(data_dir)
        details.setdefault("total_bytes", total_bytes)
        start_time = time.time()
        timestamp = datetime.utcnow().strftime("%Y%m%d.%H%M%S")
        height = details.get("height")
        height_text = f".{height}" if height else ""
        dest_name = f"{SNAPSHOT_PREFIX}.{timestamp}{height_text}{SNAPSHOT_SUFFIX}"
        dest_path = directory / dest_name
        _snapshot_progress_update(
            {
                "bytes_written": 0,
                "total_bytes": total_bytes,
                "pct": 0.0 if total_bytes else None,
                "speed_bytes": 0.0,
                "eta_seconds": None,
                "updated": start_time,
                "started": start_time,
                "path": str(dest_path),
            }
        )
        if DOCKER_BIN:
            command = [
                DOCKER_BIN,
                "run",
                "--rm",
                "--init",
                "-u",
                "0",
                "-v",
                f"{data_dir}:/data:ro",
                "-v",
                f"{directory}:/backup",
                "busybox",
                "tar",
                "-cf",
                f"/backup/{dest_name}",
                "-C",
                "/data",
                ".",
            ]
            try:
                _run_command_with_progress(command, dest_path, total_bytes=total_bytes, started=start_time)
            except RuntimeError as exc:
                raise RuntimeError(str(exc))
        else:
            parent = data_dir.parent
            arcname = data_dir.name
            command = [
                "tar",
                "--warning=no-file-changed",
                "--ignore-failed-read",
                "-cf",
                str(dest_path),
                "-C",
                str(parent),
                arcname,
            ]
            try:
                _run_command_with_progress(command, dest_path, total_bytes=total_bytes, started=start_time)
            except RuntimeError as exc:
                err_text = str(exc).lower()
                if "unrecognized option" in err_text or "illegal option" in err_text:
                    if dest_path.exists():
                        try:
                            dest_path.unlink()
                        except Exception:
                            pass
                    restart_time = time.time()
                    _snapshot_progress_update(
                        {
                            "bytes_written": 0,
                            "total_bytes": total_bytes,
                            "pct": 0.0 if total_bytes else None,
                            "speed_bytes": 0.0,
                            "eta_seconds": None,
                            "updated": restart_time,
                            "started": restart_time,
                            "path": str(dest_path),
                        }
                    )
                    fallback_command = [
                        "tar",
                        "-cf",
                        str(dest_path),
                        "-C",
                        str(parent),
                        arcname,
                    ]
                    try:
                        _run_command_with_progress(
                            fallback_command, dest_path, total_bytes=total_bytes, started=restart_time
                        )
                    except RuntimeError as fallback_exc:
                        raise RuntimeError(str(fallback_exc))
                else:
                    raise
        message = f"Snapshot saved as {dest_name}"
        status = "completed"
        if SNAPSHOT_MAX > 0:
            _prune_snapshots()
    except Exception as exc:
        status = "error"
        message = f"Snapshot failed: {exc}"
        if dest_path and dest_path.exists():
            try:
                dest_path.unlink(missing_ok=True)
            except Exception:
                pass
    finally:
        if restart_required:
            try:
                _start_container(container)
                details.setdefault("restart", True)
            except Exception as exc:
                with _SNAPSHOT_JOB_LOCK:
                    _SNAPSHOT_JOB_STATE.setdefault("warnings", []).append(str(exc))
        elif container:
            details.setdefault("restart", False)
        _snapshot_progress_update(None)
        with _SNAPSHOT_JOB_LOCK:
            _SNAPSHOT_JOB_STATE.update(
                {
                    "active": False,
                    "status": status,
                    "message": message,
                    "details": {**(details or {}), "path": dest_name},
                    "ended": time.time(),
                }
            )


def _start_snapshot_job(node_id: Optional[str]) -> Tuple[bool, str, Dict[str, object]]:
    details: Dict[str, object] = {}
    target_ctx: Optional["NodeContext"] = None
    if node_id:
        try:
            target_ctx = _resolve_node(node_id)
        except Exception:
            target_ctx = None
    if target_ctx:
        try:
            target_ctx.sample(force=True)
        except Exception:
            pass
        details["node"] = target_ctx.id
        if target_ctx.container:
            details["container"] = target_ctx.container
        if target_ctx.chain_data_dir:
            details["data_dir"] = str(target_ctx.chain_data_dir)
        if target_ctx.label:
            details["label"] = target_ctx.label
        last_metrics = target_ctx.last_metrics or {}
        height = last_metrics.get("local_height")
        if isinstance(height, int) and height >= 0:
            details["height"] = height
    details["mode"] = "snapshot"
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_STATE.get("active"):
            return False, "Snapshot already in progress", _snapshot_job_snapshot()
        _SNAPSHOT_JOB_STATE.pop("progress", None)
        _SNAPSHOT_JOB_STATE.update(
            {
                "active": True,
                "status": "running",
                "message": "Snapshot job running…",
                "details": details,
                "started": time.time(),
                "ended": None,
                "warnings": [],
            }
        )
    thread = threading.Thread(target=_run_snapshot_job, args=(details,), daemon=True)
    thread.start()
    label = details.get("label") or details.get("node")
    message = f"Snapshot started for {label}" if label else "Snapshot started"
    return True, message, _snapshot_job_snapshot()


def _run_restore_job(details: Dict[str, object]) -> None:
    container = (details or {}).get("container") if details else None
    restart_required = False
    snapshot_path: Optional[Path] = None
    data_dir = _normalize_path(details.get("data_dir")) if details else None
    backup_dir: Optional[Path] = None
    try:
        directory = _ensure_snapshot_dir()
        snapshot_name = (details or {}).get("snapshot") if details else None
        if snapshot_name:
            snapshot_path = _normalize_path(directory / snapshot_name)
        else:
            snapshot_path = _select_snapshot_for_restore()
            if snapshot_path:
                details["snapshot"] = snapshot_path.name
        if not snapshot_path or not snapshot_path.exists():
            raise RuntimeError("No snapshots available to restore.")
        if not data_dir:
            data_dir = _normalize_path(SNAPSHOT_DATA_DIR)
        if not data_dir:
            raise RuntimeError("Restore data directory not configured.")
        if container and DOCKER_BIN:
            try:
                restart_required = _stop_container(container)
            except Exception as exc:
                raise RuntimeError(f"Failed to stop container {container}: {exc}")
        parent_dir = data_dir.parent
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d.%H%M%S")
        backup_name = f"{data_dir.name}.pre-restore.{timestamp}"
        if DOCKER_BIN:
            script = (
                "set -e\n"
                "cd /volume\n"
                f"if [ -d '{data_dir.name}' ]; then mv '{data_dir.name}' '{backup_name}'; fi\n"
                f"mkdir -p '{data_dir.name}'\n"
                f"tar -xf '/backup/{snapshot_path.name}' -C '{data_dir.name}'\n"
            )
            command = [
                DOCKER_BIN,
                "run",
                "--rm",
                "--init",
                "-u",
                "0",
                "-v",
                f"{parent_dir}:/volume",
                "-v",
                f"{snapshot_path.parent}:/backup:ro",
                "busybox",
                "sh",
                "-c",
                script,
            ]
            try:
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr or exc.stdout or str(exc))
        else:
            temp_dir = parent_dir / f"restore-{timestamp}"
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            command = ["tar", "-xf", str(snapshot_path), "-C", str(temp_dir)]
            try:
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr or exc.stdout or str(exc))
            if data_dir.exists():
                backup_dir = parent_dir / backup_name
                shutil.move(str(data_dir), str(backup_dir))
            shutil.move(str(temp_dir), str(data_dir))
        backup_dir = parent_dir / backup_name if (parent_dir / backup_name).exists() else backup_dir
        if backup_dir and backup_dir.exists():
            details["backup"] = str(backup_dir)
        message = f"Snapshot {snapshot_path.name} restored"
        label = details.get("label") or details.get("node")
        if label:
            message = f"Snapshot {snapshot_path.name} restored to {label}"
        status = "completed"
    except Exception as exc:
        status = "error"
        message = f"Snapshot restore failed: {exc}"
        if DOCKER_BIN and data_dir:
            try:
                revert_script = (
                    "cd /volume\n"
                    f"if [ -d '{backup_name}' ] && [ ! -d '{data_dir.name}' ]; then mv '{backup_name}' '{data_dir.name}'; fi\n"
                )
                subprocess.run(
                    [
                        DOCKER_BIN,
                        "run",
                        "--rm",
                        "--init",
                        "-u",
                        "0",
                        "-v",
                        f"{parent_dir}:/volume",
                        "busybox",
                        "sh",
                        "-c",
                        revert_script,
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception:
                pass
        elif backup_dir and data_dir and not data_dir.exists():
            try:
                shutil.move(str(backup_dir), str(data_dir))
            except Exception:
                pass
    finally:
        if restart_required:
            try:
                _start_container(container)
                details.setdefault("restart", True)
            except Exception as exc:
                with _SNAPSHOT_JOB_LOCK:
                    _SNAPSHOT_JOB_STATE.setdefault("warnings", []).append(str(exc))
        elif container:
            details.setdefault("restart", False)
        _snapshot_progress_update(None)
        with _SNAPSHOT_JOB_LOCK:
            _SNAPSHOT_JOB_STATE.update(
                {
                    "active": False,
                    "status": status,
                    "message": message,
                    "details": {**(details or {}), "path": details.get("snapshot")},
                    "ended": time.time(),
                }
            )


def _start_restore_job(node_id: Optional[str]) -> Tuple[bool, str, Dict[str, object]]:
    details: Dict[str, object] = {}
    target_ctx: Optional["NodeContext"]
    if node_id:
        target_ctx = _resolve_node(node_id)
    else:
        target_ctx = _resolve_node(None)
    if target_ctx:
        details["node"] = target_ctx.id
        if target_ctx.container:
            details["container"] = target_ctx.container
        if target_ctx.chain_data_dir:
            details["data_dir"] = str(target_ctx.chain_data_dir)
        if target_ctx.label:
            details["label"] = target_ctx.label
    snapshot_path = _select_snapshot_for_restore()
    if not snapshot_path or not snapshot_path.exists():
        return False, "No snapshots available to restore.", _snapshot_job_snapshot()
    details["snapshot"] = snapshot_path.name
    details["mode"] = "restore"
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_STATE.get("active"):
            return False, "Snapshot already in progress", _snapshot_job_snapshot()
        _SNAPSHOT_JOB_STATE.pop("progress", None)
        _SNAPSHOT_JOB_STATE.update(
            {
                "active": True,
                "status": "running",
                "message": "Snapshot restore running…",
                "details": details,
                "started": time.time(),
                "ended": None,
                "warnings": [],
            }
        )
    thread = threading.Thread(target=_run_restore_job, args=(details,), daemon=True)
    thread.start()
    label = details.get("label") or details.get("node")
    message = f"Snapshot restore started for {label}" if label else "Snapshot restore started"
    return True, message, _snapshot_job_snapshot()


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
        chain_data_dir = None
        for mount in data.get("Mounts") or []:
            if mount.get("Destination") == "/bdag/data" and mount.get("Source"):
                try:
                    chain_data_dir = str(_normalize_path(mount.get("Source")))
                except Exception:
                    chain_data_dir = mount.get("Source")
                break
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
                "chain_data_dir": chain_data_dir,
            }
        )
    return nodes


# ---------------------------------------------------------------------------
# Node configuration & state
# ---------------------------------------------------------------------------
DEFAULT_NODE_SETTINGS = {
    "id": "primary",
    "label": "Primary Node",
    "rpc_base": os.getenv("BDAG_RPC_BASE", DEFAULT_RPC_FALLBACK),
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

BALANCE_RPC_BASE = _normalize_rpc_endpoint(
    os.getenv("BDAG_BALANCE_RPC_BASE")
    or os.getenv("BDAG_RPC_BASE")
    or DEFAULT_NODE_SETTINGS["rpc_base"]
)
BALANCE_RPC_USER = os.getenv("BDAG_BALANCE_RPC_USER", DEFAULT_NODE_SETTINGS["rpc_user"])
BALANCE_RPC_PASS = os.getenv("BDAG_BALANCE_RPC_PASS", DEFAULT_NODE_SETTINGS["rpc_pass"])
BALANCE_RPC_TIMEOUT = float(os.getenv("BDAG_BALANCE_RPC_TIMEOUT", "6") or "6")
BALANCE_RPC_VERIFY = _coerce_bool(os.getenv("BDAG_BALANCE_RPC_VERIFY"), DEFAULT_NODE_SETTINGS["rpc_verify"])


def _get_wallet_address() -> Tuple[Optional[str], Optional[str]]:
    override = str(get_settings().get("wallet_address") or "").strip()
    if override:
        cached_address = _wallet_address_cache.get("address")
        cached_path = _wallet_address_cache.get("path")
        if cached_path != "settings" or cached_address != override:
            _wallet_address_cache.update({"path": "settings", "mtime": 0.0, "address": override})
        return override, "settings"
    for path in _wallet_candidate_paths():
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if stat.st_size <= 0:
            continue
        cached_path = _wallet_address_cache.get("path")
        cached_mtime = _wallet_address_cache.get("mtime") or 0.0
        if cached_path == str(path) and cached_mtime == stat.st_mtime:
            return _wallet_address_cache.get("address"), str(path)
        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = [line.strip() for line in fh if line.strip()]
        except Exception:
            continue
        if not lines:
            continue
        address = lines[-1]
        _wallet_address_cache.update({"path": str(path), "mtime": stat.st_mtime, "address": address})
        return address, str(path)
    _wallet_address_cache.update({"path": None, "mtime": 0.0, "address": None})
    return None, None


def _format_balance_decimal(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_UP)
    return f"{normalized:,.2f}"


def _fetch_wallet_balance(address: str) -> dict:
    if not BALANCE_RPC_BASE:
        raise RuntimeError("RPC endpoint not configured")
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": int(time.time()),
    }
    auth = None
    if BALANCE_RPC_USER or BALANCE_RPC_PASS:
        auth = (BALANCE_RPC_USER or "", BALANCE_RPC_PASS or "")
    response = requests.post(
        BALANCE_RPC_BASE,
        json=payload,
        timeout=BALANCE_RPC_TIMEOUT,
        auth=auth,
        verify=BALANCE_RPC_VERIFY,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    result = data.get("result")
    if not isinstance(result, str):
        raise RuntimeError("unexpected balance response")
    balance_wei = int(result, 16)
    balance_decimal = Decimal(balance_wei) / WEI_PER_BDAG
    formatted = _format_balance_decimal(balance_decimal)
    return {
        "address": address,
        "balance_wei": balance_wei,
        "balance_bdag": str(balance_decimal),
        "balance_formatted": f"{formatted} BDAG",
        "rpc": BALANCE_RPC_BASE,
    }


def _get_wallet_overview() -> dict:
    address, source = _get_wallet_address()
    if not address:
        return {"error": "wallet not found"}
    now = time.time()
    cached = _wallet_balance_cache.get("data")
    cached_ts = float(_wallet_balance_cache.get("ts") or 0.0)
    if (
        cached
        and isinstance(cached, dict)
        and cached.get("address") == address
        and now - cached_ts < WALLET_BALANCE_CACHE_SEC
    ):
        return cached
    try:
        info = _fetch_wallet_balance(address)
    except Exception as exc:
        info = {"address": address, "error": str(exc)}
    info["source"] = source
    info["timestamp"] = time.time()
    info["short"] = info.get("balance_formatted", "—")
    _wallet_balance_cache["data"] = info
    _wallet_balance_cache["ts"] = info["timestamp"]
    return info

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

        chain_data_dir = merged.get("chain_data_dir") or merged.get("chaindata_dir")
        self.chain_data_dir: Optional[Path]
        if chain_data_dir:
            self.chain_data_dir = _normalize_path(chain_data_dir)
        else:
            self.chain_data_dir = None

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
        chain_dir = meta.get("chain_data_dir") or meta.get("chaindata_dir")
        if chain_dir:
            normalized = _normalize_path(chain_dir)
            if normalized and normalized != self.chain_data_dir:
                self.chain_data_dir = normalized
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
            "chain_data_dir": str(self.chain_data_dir) if self.chain_data_dir else None,
        }

    def _empty_metrics(self) -> dict:
        return {
            "local_height": 0,
            "remote_height": 0,
            "height_delta": 0,
            "peers": 0,
            "running": self.running,
            "container_running": False,
            "container_exists": False,
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


def _purge_policy_state(container: Optional[str]) -> None:
    if not container:
        return
    with _LOG_POLICY_LOCK:
        _LOG_POLICY_STATE.pop(container, None)
        keys = [key for key in _RECENT_LOGS_CACHE.keys() if key[0] == container]
        for key in keys:
            _RECENT_LOGS_CACHE.pop(key, None)


def _get_recent_logs(limit: int, container: str) -> List[str]:
    if not container or not DOCKER_BIN:
        return []
    try:
        limit_int = max(1, min(int(limit), 200))
    except Exception:
        limit_int = LOG_ERROR_TAIL
    key = (container, limit_int)
    now = time.time()
    with _LOG_POLICY_LOCK:
        cached = _RECENT_LOGS_CACHE.get(key)
        cached_ts = float(cached.get("ts", 0.0)) if cached else 0.0
        if cached and now - cached_ts < LOG_CACHE_SEC and isinstance(cached.get("lines"), list):
            return list(cached["lines"])
    try:
        out = subprocess.check_output(
            [DOCKER_BIN, "logs", "--tail", str(limit_int), "--timestamps", container],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        raw_lines = [line.rstrip() for line in out.splitlines() if line.strip()]
        lines = [ANSI_ESCAPE_RE.sub("", line) for line in raw_lines]
    except Exception:
        lines = []
    with _LOG_POLICY_LOCK:
        _RECENT_LOGS_CACHE[key] = {"ts": now, "lines": lines}
    return list(lines)


def _restart_container_for_policy(ctx: "NodeContext", reason: str) -> bool:
    if not ctx or not ctx.container:
        return False
    result = docker_action(ctx.container, "restart")
    if result.get("ok"):
        try:
            app.logger.warning(
                "Auto restart triggered for node %s (%s): %s",
                ctx.id,
                ctx.container,
                reason,
            )
        except Exception:
            pass
        return True
    error_message = result.get("error") or result.get("output") or "unknown error"
    try:
        app.logger.error(
            "Auto restart failed for node %s (%s): %s",
            ctx.id,
            ctx.container,
            error_message,
        )
    except Exception:
        pass
    return False


def _apply_node_policies(ctx: "NodeContext", settings: Dict[str, bool]) -> None:
    if not ctx or not ctx.container or not DOCKER_BIN:
        return
    if not ctx.running:
        return
    enable_error_restart = bool(settings.get("auto_restart_on_error"))
    enable_liveness = bool(settings.get("liveness_auto_recover"))
    if not (enable_error_restart or enable_liveness):
        return
    now = time.time()
    with _LOG_POLICY_LOCK:
        state = _LOG_POLICY_STATE.setdefault(
            ctx.container,
            {"last_check": 0.0, "error_streak": 0, "last_restart": 0.0, "last_liveness": 0.0},
        )
        last_check = float(state.get("last_check", 0.0))
        if now - last_check < LOG_ERROR_CHECK_SEC:
            return
        state["last_check"] = now
    lines = _get_recent_logs(LOG_ERROR_TAIL, ctx.container)
    if not lines:
        with _LOG_POLICY_LOCK:
            state["error_streak"] = 0
        return
    if enable_liveness:
        for raw_line in reversed(lines):
            text_lower = str(raw_line).strip().lower()
            if any(pattern in text_lower for pattern in LIVENESS_FAILSAFE_PATTERNS):
                with _LOG_POLICY_LOCK:
                    last_liveness = float(state.get("last_liveness", 0.0))
                    last_restart = float(state.get("last_restart", 0.0))
                    if now - last_liveness < LIVENESS_RECOVER_COOLDOWN_SEC:
                        state["error_streak"] = 0
                        return
                    state["last_liveness"] = now
                if _restart_container_for_policy(ctx, "liveness probe failure detected in logs"):
                    with _LOG_POLICY_LOCK:
                        state["last_restart"] = now
                        state["error_streak"] = 0
                return
    if not enable_error_restart:
        return
    streak = 0
    for raw_line in reversed(lines):
        text = str(raw_line)
        if LOG_ERROR_PATTERN.search(text):
            streak += 1
        else:
            break
    with _LOG_POLICY_LOCK:
        state["error_streak"] = streak
        last_restart = float(state.get("last_restart", 0.0))
    if streak < LOG_ERROR_THRESHOLD:
        return
    if now - last_restart < LOG_ERROR_RESTART_COOLDOWN_SEC:
        return
    if _restart_container_for_policy(ctx, f"{streak} consecutive error log lines"):
        with _LOG_POLICY_LOCK:
            state["last_restart"] = time.time()
            state["error_streak"] = 0


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
    def _try_peer_methods(methods: Iterable[str]) -> Optional[int]:
        for method in methods:
            try:
                result = _rpc_call(
                    ctx.rpc_base,
                    method,
                    [],
                    timeout=ctx.rpc_timeout,
                    auth=auth,
                    verify=ctx.rpc_verify,
                )
                value = _parse_height_value(result)
                if value is not None:
                    return value
            except Exception:
                continue
        return None

    base_count = _try_peer_methods(PEER_COUNT_METHODS)
    if isinstance(base_count, int) and base_count > 0:
        return base_count

    try:
        info = _rpc_call(
            ctx.rpc_base,
            "bdag_getPeerInfo",
            [],
            timeout=ctx.rpc_timeout,
            auth=auth,
            verify=ctx.rpc_verify,
        )
    except Exception:
        info = None

    if info is not None:
        peer_list: List[object] = []
        count_candidates: List[object] = []
        if isinstance(info, list):
            peer_list = info
        elif isinstance(info, dict):
            for key in ("active", "activeCount", "connected", "connections", "count", "numPeers", "total", "peersCount"):
                if key in info:
                    count_candidates.append(info.get(key))
            peers_field = info.get("peers")
            if isinstance(peers_field, list):
                peer_list = peers_field
            else:
                peer_list = [info]

        for candidate in count_candidates:
            cand_val = _parse_height_value(candidate)
            if cand_val is not None and cand_val >= 0:
                return cand_val

        if peer_list:
            active = 0
            statuses = {"true", "1", "connected", "active", "running", "online", "up", "ok"}
            for peer in peer_list:
                if isinstance(peer, dict):
                    flags = (
                        peer.get("active"),
                        peer.get("state"),
                        peer.get("connected"),
                        peer.get("isActive"),
                        peer.get("is_connected"),
                        peer.get("status"),
                    )
                    counted = False
                    for flag in flags:
                        if isinstance(flag, bool):
                            if flag:
                                active += 1
                                counted = True
                                break
                        elif isinstance(flag, (int, float)):
                            if flag > 0:
                                active += 1
                                counted = True
                                break
                        elif isinstance(flag, str):
                            if flag.strip().lower() in statuses:
                                active += 1
                                counted = True
                                break
                    if not counted:
                        active += 1
                else:
                    active += 1
            if active <= 0:
                active = len(peer_list)
            if active >= 0:
                return int(active)

    try:
        result = _rpc_call(
            ctx.rpc_base,
            "getconnectioncount",
            [],
            timeout=ctx.rpc_timeout,
            auth=auth,
            verify=ctx.rpc_verify,
        )
        count = _parse_height_value(result)
        if count is not None and count > 0:
            return count
    except Exception:
        pass

    if isinstance(base_count, int) and base_count >= 0:
        return base_count
    return 0


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
    has_activity = local_val > 0 or peers_val > 0
    effective_running = running if exists else has_activity
    metrics = {
        "local_height": local_val,
        "remote_height": remote_display,
        "height_delta": int(remote_display - local_val),
        "peers": peers_val,
        "running": effective_running,
        "container_running": bool(running),
        "container_exists": bool(exists),
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
    removed_containers: List[str] = []
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
                removed_containers.append(ctx.container)
                NODES.pop(node_id, None)

    for container in removed_containers:
        _purge_policy_state(container)

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
    settings = get_settings()
    wallet_enabled = bool(settings.get("display_wallet_balance"))
    summary["wallet_enabled"] = wallet_enabled
    summary["settings"] = settings
    if wallet_enabled:
        try:
            summary["wallet"] = _get_wallet_overview()
        except Exception as exc:
            summary["wallet"] = {"error": str(exc)}
    else:
        summary["wallet"] = None
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
    settings = get_settings()
    for ctx in NODES.values():
        ctx.sample()
        _apply_node_policies(ctx, settings)
        nodes_payload.append(
            {
                "id": ctx.id,
                "label": ctx.label,
                "container": ctx.container,
                "auto_discovered": bool(ctx.auto_discovered),
                "status": ctx.snapshot(include_series=False),
            }
        )
    if nodes_payload:
        summary = _fleet_summary(nodes_payload)
    else:
        summary = _fleet_summary([])
    return jsonify({"nodes": nodes_payload, "summary": summary})


@app.route("/api/node-manager/metrics")
def api_node_manager_metrics():
    nodes_param = request.args.get("nodes", "")
    if nodes_param:
        node_ids = [item.strip() for item in nodes_param.split(",") if item.strip()]
    else:
        node_ids = list(NODES.keys())
    response = {}
    settings = get_settings()
    for node_id in node_ids:
        ctx = NODES.get(node_id)
        if not ctx:
            continue
        ctx.sample(force=True)
        _apply_node_policies(ctx, settings)
        response[ctx.id] = ctx.snapshot(include_series=True)
    return jsonify({"nodes": response, "timestamp": time.time()})



@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"settings": get_settings()})
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    updated = update_settings(body)
    return jsonify({"ok": True, "settings": updated})


@app.route("/api/snapshots", methods=["GET"])
def api_snapshots():
    snapshots = list_snapshots()
    job = _snapshot_job_snapshot()
    locations = _scan_snapshot_locations()
    response: Dict[str, object] = {
        "snapshots": snapshots,
        "job": job,
        "directory": str(SNAPSHOT_DIR),
        "locations": locations,
    }
    message = job.get("message") if isinstance(job, dict) else None
    if message:
        status = job.get("status") if isinstance(job, dict) else None
        active = bool(job.get("active")) if isinstance(job, dict) else False
        if active:
            level = "warn"
        elif status == "completed":
            level = "ok"
        elif status == "error":
            level = "error"
        else:
            level = "warn"
        response["status"] = {"text": message, "level": level}
    return jsonify(response)


@app.route("/api/snapshots/create", methods=["POST"])
def api_snapshots_create():
    body = request.get_json(silent=True) or {}
    node_id = body.get("node")
    ok, message, job = _start_snapshot_job(str(node_id) if node_id else None)
    if not ok:
        return jsonify({"ok": False, "error": message, "job": job}), 409
    return jsonify({"ok": True, "message": message, "job": job})


@app.route("/api/snapshots/scan", methods=["POST"])
def api_snapshots_scan():
    locations = _scan_snapshot_locations()
    message: str
    if locations:
        selected_path = locations[0].get("path")
        updated = _update_snapshot_dir(Path(selected_path)) if selected_path else False
        if updated:
            message = f"Snapshot directory set to {selected_path}"
        else:
            message = f"Using snapshot directory {selected_path}"
    else:
        _ensure_snapshot_dir()
        message = "No snapshot directories found. Created default location."
    return jsonify(
        {
            "ok": True,
            "message": message,
            "directory": str(SNAPSHOT_DIR),
            "locations": locations,
            "snapshots": list_snapshots(),
        }
    )


@app.route("/api/snapshots/restore", methods=["POST"])
def api_snapshots_restore():
    body = request.get_json(silent=True) or {}
    node_id = body.get("node")
    ok, message, job = _start_restore_job(str(node_id) if node_id else None)
    if not ok:
        status = 409 if job and job.get("active") else 400
        return jsonify({"ok": False, "error": message, "job": job}), status
    return jsonify({"ok": True, "message": message, "job": job})


@app.route("/api/snapshots/delete", methods=["POST"])
def api_snapshots_delete():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "missing snapshot name"}), 400
    job = _snapshot_job_snapshot()
    if job.get("active"):
        return jsonify({"ok": False, "error": "snapshot job in progress"}), 409
    directory = _ensure_snapshot_dir()
    target = directory / name
    normalized_target = _normalize_path(target)
    normalized_dir = _normalize_path(directory)
    if (
        not normalized_target
        or not normalized_dir
        or normalized_target.parent != normalized_dir
        or not normalized_target.exists()
    ):
        return jsonify({"ok": False, "error": f"snapshot '{name}' not found"}), 404
    try:
        normalized_target.unlink()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {
            "ok": True,
            "message": f"Deleted {name}",
            "directory": str(SNAPSHOT_DIR),
            "snapshots": list_snapshots(),
        }
    )


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
