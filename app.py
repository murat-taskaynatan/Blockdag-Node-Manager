import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import pwd
import grp
import string
import shlex
import hmac
import itertools
import stat
import stat
import queue
import threading
import socket
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, getcontext
from collections import OrderedDict, deque
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple, Pattern
from urllib.parse import urlparse

import psutil
import requests
from flask import Flask, abort, jsonify, render_template, request, g
from flask import Response, send_from_directory, session, redirect, url_for
from scripts.launchpad_launcher import LaunchError, launch_node, preview_ports


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

def _parse_bool_env(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


_ENV_LOGIN_USER = os.getenv("BDAG_LOGIN_USER", "").strip()
_ENV_LOGIN_PASS = os.getenv("BDAG_LOGIN_PASS", "").strip()
_login_override = _parse_bool_env(os.getenv("BDAG_LOGIN_ENABLED"))
_default_login_enabled = bool(_ENV_LOGIN_USER and _ENV_LOGIN_PASS)
if _login_override is None:
    _initial_login_enabled = _default_login_enabled
else:
    _initial_login_enabled = _default_login_enabled and _login_override

# Runtime login state (may be overridden via settings)
LOGIN_USER = _ENV_LOGIN_USER
LOGIN_PASS = _ENV_LOGIN_PASS
LOGIN_ENABLED = _initial_login_enabled
SESSION_SECRET = os.getenv("BDAG_SESSION_SECRET")
if not SESSION_SECRET:
    SESSION_SECRET = os.urandom(32).hex()
app.secret_key = SESSION_SECRET


@app.context_processor
def inject_login_state():
    return {
        "is_authenticated": bool(session.get("authenticated")),
        "login_enabled": LOGIN_ENABLED,
    }

try:
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
except Exception:
    pass

getcontext().prec = 50


@app.before_request
def _record_request_start() -> None:
    g._bdag_request_started = time.time()


@app.after_request
def _log_request_timing(response: Response) -> Response:
    try:
        started = getattr(g, "_bdag_request_started", None)
        path = request.path or ""
        if started is None or path.startswith("/static/"):
            return response
        duration = max(time.time() - started, 0.0)
        query = request.query_string.decode("utf-8", errors="ignore")
        remote = request.headers.get("X-Forwarded-For", request.remote_addr or "-")
        app.logger.info(
            "HTTP %s %s%s -> %s in %.3fs (remote=%s)",
            request.method,
            path,
            f"?{query}" if query else "",
            response.status_code,
            duration,
            remote,
        )
    except Exception:
        pass
    return response


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

LOG_ERROR_CHECK_SEC = max(1.0, float(os.getenv("BDAG_LOG_ERROR_CHECK_SEC", "15") or "15"))
LOG_ERROR_RESTART_COOLDOWN_SEC = max(
    30.0,
    float(
        os.getenv("BDAG_LOG_ERROR_COOLDOWN_SEC", os.getenv("BDAG_LOG_ERROR_RESTART_COOLDOWN_SEC", "300"))
        or 300
    ),
)
AUTO_RESTART_INTERVAL_SEC = LOG_ERROR_RESTART_COOLDOWN_SEC
LOG_ERROR_TAIL = max(10, min(int(os.getenv("BDAG_LOG_ERROR_TAIL", "80") or "80"), 200))
PRE_RESTORE_BACKUP_RETENTION = max(0, int(os.getenv("BDAG_PRE_RESTORE_BACKUPS", "0") or "0"))

AUTO_SNAPSHOT_MIN_INTERVAL_SEC = max(
    900.0, float(os.getenv("BDAG_AUTO_SNAPSHOT_MIN_SEC", "3600") or 3600.0)
)
AUTO_SNAPSHOT_RETRY_SEC = max(
    300.0, float(os.getenv("BDAG_AUTO_SNAPSHOT_RETRY_SEC", "900") or 900.0)
)
_AUTO_SNAPSHOT_LOCK = threading.Lock()
_AUTO_SNAPSHOT_EVENT = threading.Event()
_AUTO_SNAPSHOT_STATE: Dict[str, object] = {
    "enabled": False,
    "interval": 0.0,
    "next_run": 0.0,
    "last_run": 0.0,
    "last_result": None,
}

LIVENESS_RECOVER_COOLDOWN_SEC = max(
    60.0, float(os.getenv("BDAG_LIVENESS_RECOVER_COOLDOWN_SEC", "900") or "900")
)
LIVENESS_MAX_RESTARTS = max(1, int(os.getenv("BDAG_LIVENESS_MAX_RESTARTS", "2") or "2"))
LIVENESS_SNAPSHOT_GRACE_SEC = max(
    0.0, float(os.getenv("BDAG_LIVENESS_SNAPSHOT_GRACE_SEC", "900") or "900")
)
LIVENESS_RESUME_MAX_DELTA = max(
    0.0, float(os.getenv("BDAG_LIVENESS_RESUME_MAX_DELTA", "25") or "25")
)
LIVENESS_STABLE_SEC = max(
    30.0, float(os.getenv("BDAG_LIVENESS_STABLE_SEC", "300") or "300")
)
_liveness_patterns_raw = [
    part.strip().lower()
    for part in str(os.getenv("BDAG_LIVENESS_RECOVER_PATTERNS", "") or "").split(",")
    if part.strip()
]
if _liveness_patterns_raw:
    LIVENESS_FAILSAFE_RECOVERY_PATTERNS = tuple(dict.fromkeys(_liveness_patterns_raw))
else:
    LIVENESS_FAILSAFE_RECOVERY_PATTERNS = (
        "chain db: need to thoroughly clean up old data",
        "bdag chain env error",
        "can't find cur block state",
        "illegal withdrawal at block",
        "illegal withdrawal at block:difflayer, you can cleanup your block data base by '--cleanup'",
        "the dag data was damaged (can't find tip",
        "unknown to the objstorage provider",
        "unclean shutdown detected",
    )

LIVENESS_FAILSAFE_RESTART_PATTERNS = (
    "node never became ready",
    "worker stopped",
    "liveness probe exceeded timeout",
    "liveness probe failed",
    "forcing shutdown url=http://127.0.0.1:6061/healthz",
    "watchexecuted: dial ws failed",
    "block chain is shutdown",
    "shutdown complete",
)
LIVENESS_FAILSAFE_PATTERNS = tuple(
    dict.fromkeys(LIVENESS_FAILSAFE_RESTART_PATTERNS + LIVENESS_FAILSAFE_RECOVERY_PATTERNS)
)

def _is_importing_reason(reason: Optional[str]) -> bool:
    if not reason:
        return False
    normalized = str(reason or "").strip().lower()
    return normalized.startswith("importing blocks") or normalized.startswith("downloading blocks")

_DEFAULT_LOG_CRITICAL_ERROR_PATTERNS = (
    "the dag data was damaged",
    "can't find tip",
    "chain db: need to thoroughly clean up old data",
    "liveness probe exceeded timeout; forcing shutdown",
    "illegal withdrawal at block",
    "cleanup your block data base by '--cleanup' to start liveness error recovery",
    "unknown to the objstorage provider",
    "unclean shutdown detected",
)
_dag_log_critical_patterns_raw: List[str] = []
for _env_name in ("DAG_LOG_CRITICAL_PATTERNS", "BDAG_LOG_CRITICAL_PATTERNS"):
    _env_value = os.getenv(_env_name)
    if _env_value:
        _dag_log_critical_patterns_raw.extend(
            part.strip().lower()
            for part in str(_env_value or "").split(",")
            if part.strip()
        )
if _dag_log_critical_patterns_raw:
    DAG_LOG_CRITICAL_PATTERNS = tuple(dict.fromkeys(_dag_log_critical_patterns_raw))
else:
    DAG_LOG_CRITICAL_PATTERNS = ()
if DAG_LOG_CRITICAL_PATTERNS:
    LOG_CRITICAL_ERROR_PATTERNS = tuple(
        dict.fromkeys(_DEFAULT_LOG_CRITICAL_ERROR_PATTERNS + DAG_LOG_CRITICAL_PATTERNS)
    )
else:
    LOG_CRITICAL_ERROR_PATTERNS = _DEFAULT_LOG_CRITICAL_ERROR_PATTERNS

LOG_CACHE_SEC = max(1.0, float(os.getenv("BDAG_LOG_CACHE_SEC", "2") or "2"))
LOG_REFRESH_INTERVAL_SEC = max(
    LOG_CACHE_SEC, float(os.getenv("BDAG_LOG_REFRESH_SEC", "5") or "5")
)
LOG_REFRESH_WAIT_SEC = max(0.2, min(LOG_REFRESH_INTERVAL_SEC, 2.0))
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_POLICY_LOCK = threading.Lock()
_LOG_POLICY_STATE: Dict[str, Dict[str, object]] = {}
_RECENT_LOGS_CACHE: Dict[Tuple[str, int], Dict[str, object]] = {}
_PEER_PORT_CACHE: Dict[str, Tuple[Optional[int], Optional[int], float]] = {}
_PEER_PORT_CACHE_LOCK = threading.Lock()
_PEER_PORT_CACHE_TTL = 60.0
_LOG_REFRESH_EVENT = threading.Event()
POLICY_WORKER_INTERVAL_SEC = max(
    5.0, float(os.getenv("BDAG_POLICY_WORKER_INTERVAL_SEC") or LOG_ERROR_CHECK_SEC)
)
_POLICY_EVENT = threading.Event()

MEMORY_RESTART_INTERVAL_SEC = max(
    15.0, float(os.getenv("BDAG_MEMORY_RESTART_INTERVAL_SEC", "30") or "30")
)
MEMORY_RESTART_COOLDOWN_SEC = max(
    10.0, float(os.getenv("BDAG_MEMORY_RESTART_COOLDOWN_SEC", "60") or "60")
)
_MEMORY_RESTART_LOCK = threading.Lock()
_MEMORY_RESTART_ACTIVE = False

# Overclock logs buffer (UI tailing)
_OVERCLOCK_LOGS: deque[str] = deque(maxlen=500)
_OVERCLOCK_LOGS_LOCK = threading.Lock()

_AUTOMATION_LOG: deque[Dict[str, object]] = deque(maxlen=200)
_AUTOMATION_LOG_LOCK = threading.Lock()
_AUTOMATION_SEQ = itertools.count(1)

_DOCKER_HEALTH_LOCK = threading.Lock()
_DOCKER_HEALTH: Dict[str, object] = {
    "available": bool(DOCKER_BIN),
    "docker_bin": DOCKER_BIN or "",
    "last_error": None,
    "last_checked": 0.0,
    "last_success": 0.0,
}
if not DOCKER_BIN:
    _DOCKER_HEALTH["last_error"] = "docker binary not found; install docker or set BDAG_DOCKER_BIN"


def _oc_log(message: str) -> None:
    try:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    except Exception:
        line = message
    with _OVERCLOCK_LOGS_LOCK:
        _OVERCLOCK_LOGS.append(line)


def _automation_event(
    kind: str,
    message: str,
    *,
    node: Optional[str] = None,
    container: Optional[str] = None,
    status: Optional[str] = None,
    metadata: Optional[Dict[str, object]] = None,
) -> None:
    timestamp = time.time()
    ts_iso = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    meta_copy: Dict[str, object] = {}
    if isinstance(metadata, dict):
        meta_copy = {k: v for k, v in metadata.items() if not isinstance(v, (set, bytes))}
    entry = {
        "id": next(_AUTOMATION_SEQ),
        "ts": timestamp,
        "ts_iso": ts_iso,
        "kind": kind,
        "message": str(message or "").strip() or kind,
        "node": node,
        "container": container,
        "status": status,
        "meta": meta_copy,
    }
    with _AUTOMATION_LOG_LOCK:
        _AUTOMATION_LOG.appendleft(entry)


def _automation_log_snapshot(limit: Optional[int] = None) -> List[Dict[str, object]]:
    with _AUTOMATION_LOG_LOCK:
        entries = list(_AUTOMATION_LOG)
    if limit is not None:
        try:
            safe_limit = max(1, int(limit))
        except Exception:
            safe_limit = 50
        entries = entries[:safe_limit]
    return [dict(item) for item in entries]


def _service_username() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return os.getenv("USER") or "node"


def _user_in_group(user: str, group: str) -> bool:
    if not user or user == "root":
        return True
    try:
        user_entry = pwd.getpwnam(user)
    except KeyError:
        return False
    try:
        target_group = grp.getgrnam(group)
    except KeyError:
        return True
    if target_group.gr_gid == user_entry.pw_gid or user in target_group.gr_mem:
        return True
    try:
        members = os.getgrouplist(user, user_entry.pw_gid)  # type: ignore[attr-defined]
        if target_group.gr_gid in members:
            return True
    except Exception:
        pass
    try:
        return target_group.gr_gid in os.getgroups()
    except Exception:
        pass
    try:
        for entry in grp.getgrall():
            if entry.gr_gid == target_group.gr_gid and user in entry.gr_mem:
                return True
    except Exception:
        pass
    return False


def _ensure_docker_group_membership() -> None:
    user = _service_username()
    if not user or user == "root":
        return
    if _user_in_group(user, "docker"):
        return
    if os.geteuid() == 0:
        try:
            subprocess.run(
                ["usermod", "-aG", "docker", user],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            app.logger.info(
                "Added service user '%s' to docker group; restart the node manager service to apply permissions",
                user,
            )
        except Exception as exc:
            app.logger.warning("Failed to add user '%s' to docker group: %s", user, exc)
    else:
        try:
            app.logger.warning(
                "Service user '%s' is not a member of the docker group. Run `sudo usermod -aG docker %s` and restart.",
                user,
                user,
            )
        except Exception:
            pass


def _docker_health_record(success: bool, message: Optional[str] = None) -> None:
    now = time.time()
    with _DOCKER_HEALTH_LOCK:
        previous_error = _DOCKER_HEALTH.get("last_error")
        if success:
            _DOCKER_HEALTH["available"] = True
            _DOCKER_HEALTH["last_checked"] = now
            _DOCKER_HEALTH["last_success"] = now
            if message:
                _DOCKER_HEALTH["last_error"] = message
            else:
                _DOCKER_HEALTH["last_error"] = None
        else:
            _DOCKER_HEALTH["available"] = False
            _DOCKER_HEALTH["last_checked"] = now
            if message:
                _DOCKER_HEALTH["last_error"] = message
    if not success and message and message != previous_error:
        try:
            app.logger.warning("Docker access issue: %s", message)
        except Exception:
            pass


def _docker_health_snapshot() -> Dict[str, object]:
    with _DOCKER_HEALTH_LOCK:
        return dict(_DOCKER_HEALTH)


def _normalize_docker_error(message: str) -> str:
    if not message:
        return "docker command failed"
    text = message.strip()
    lowered = text.lower()
    if "permission denied" in lowered and "/var/run/docker.sock" in lowered:
        user = _service_username()
        return f"permission denied accessing Docker socket; add user '{user}' to the 'docker' group and restart the node manager service"
    return text


def _auto_detect_data_dir(preferred: Optional[str] = None) -> Optional[Path]:
    """Best-effort detection of the node data directory.
    Order:
      1) Preferred path if it exists
      2) Any discovered node context chain_data_dir that exists
      3) Common locations and known symlinks
    """
    # 1) Preferred
    try:
        if preferred:
            p = _normalize_path(preferred)
            if p and p.exists() and p.is_dir():
                return p
    except Exception:
        pass
    # 2) Discovered nodes
    try:
        for ctx in NODES.values():
            candidate = ctx.chain_data_dir
            if candidate and Path(candidate).exists():
                return Path(candidate)
    except Exception:
        pass
    # 3) Common paths
    candidates = [
        Path.home() / "blockdag",
        Path("/home/node/blockdag"),
        Path("/media/node/nvme1/blockdag-data"),
        Path("/media/node/nvme1/blockdag"),
        Path("/media/nvme1/blockdag-data"),
        Path("/media/nvme1/blockdag"),
        Path.home() / "blockdag-scripts" / "bin" / "bdag" / "data",
    ]
    # If /home/node/blockdag symlink exists, resolve
    try:
        link = Path("/home/node/blockdag")
        if link.exists():
            candidates.insert(0, link.resolve())
    except Exception:
        pass
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Remote RPC defaults
# ---------------------------------------------------------------------------
def _normalize_remote_url(url: Optional[str]) -> str:
    text = (url or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/")


PRIMARY_REMOTE_RPC_BASE = _normalize_remote_url("https://rpc.awakening.bdagscan.com")
LEGACY_REMOTE_RPC_BASES = [
    _normalize_remote_url("http://13.245.135.249:18545"),
    _normalize_remote_url("https://rpc.bdagscan.com"),
    _normalize_remote_url("https://relay.awakening.bdagscan.com"),
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


_REMOTE_RPC_OVERRIDE_DEFINED = (
    os.getenv("BDAG_REMOTE_RPC_BASES") is not None or os.getenv("BDAG_REMOTE_RPC_BASE") is not None
)
ENV_REMOTE_RPC_BASES = _parse_remote_rpc_bases(
    os.getenv("BDAG_REMOTE_RPC_BASES", os.getenv("BDAG_REMOTE_RPC_BASE"))
)
DEFAULT_REMOTE_BASES = ENV_REMOTE_RPC_BASES or DEFAULT_REMOTE_RPC_BASES[:]

_REMOTE_HEIGHT_CACHE: Dict[Tuple[str, str], Dict[str, object]] = {}
_REMOTE_HEIGHT_CACHE_LOCK = threading.Lock()
_REMOTE_HEIGHT_CACHE_TTL_SEC = max(
    1.0,
    float(os.getenv("BDAG_REMOTE_RPC_CACHE_SEC", "5") or "5"),
)
_REMOTE_HEIGHT_CACHE_MAX_AGE_SEC = max(
    _REMOTE_HEIGHT_CACHE_TTL_SEC * 6,
    _REMOTE_HEIGHT_CACHE_TTL_SEC + 5.0,
)

WEI_PER_BDAG = Decimal("1000000000000000000")
WALLET_BALANCE_CACHE_SEC = max(0.0, float(os.getenv("BDAG_BALANCE_CACHE_SEC", "120") or "120"))
_wallet_address_cache: Dict[str, object] = {"path": None, "mtime": 0.0, "address": None}
_wallet_balance_cache: Dict[str, object] = {"ts": 0.0, "data": None}
_WALLET_BALANCE_HISTORY: deque[Dict[str, object]] = deque(maxlen=120)
_WALLET_REFRESH_EVENT = threading.Event()
_WALLET_REFRESH_LOCK = threading.Lock()
_wallet_refresh_pending = False
WALLET_HISTORY_PATH = (Path(__file__).resolve().parent / "data" / "wallet_history.json")
WALLET_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_wallet_history() -> None:
    if not WALLET_HISTORY_PATH.exists():
        return
    try:
        raw = WALLET_HISTORY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return
    if not isinstance(data, list):
        return
    _WALLET_BALANCE_HISTORY.clear()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp")
        balance = entry.get("balance")
        formatted = entry.get("formatted")
        try:
            ts_val = float(ts)
            bal_val = float(balance)
        except (TypeError, ValueError):
            continue
        _WALLET_BALANCE_HISTORY.append(
            {
                "timestamp": ts_val,
                "balance": bal_val,
                "formatted": formatted,
            }
        )


def _persist_wallet_history() -> None:
    try:
        payload = list(_WALLET_BALANCE_HISTORY)
        temp_path = WALLET_HISTORY_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(WALLET_HISTORY_PATH)
    except Exception:
        pass


def _schedule_wallet_refresh(*, force: bool = False) -> None:
    global _wallet_refresh_pending
    with _WALLET_REFRESH_LOCK:
        if _wallet_refresh_pending and not force:
            return
        _wallet_refresh_pending = True
    _WALLET_REFRESH_EVENT.set()


_load_wallet_history()

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


DEFAULT_RPC_FALLBACK = "https://relay.awakening.bdagscan.com"


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


def _coerce_port(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        port = int(value)
        return port if port > 0 else None
    except Exception:
        return None



def _detect_primary_ip() -> Optional[str]:
    targets = [("8.8.8.8", 80), ("1.1.1.1", 80)]
    for host, port in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, port))
                addr = sock.getsockname()[0]
                if addr:
                    return addr
        except Exception:
            continue
    try:
        hostname = socket.gethostname()
        if hostname:
            return socket.gethostbyname(hostname)
    except Exception:
        pass
    return None


SETTINGS_PATH = Path(__file__).resolve().parent / "config" / "settings.json"
SNAPSHOT_MAX_DEFAULT = max(0, int(os.getenv("BDAG_SNAPSHOT_MAX", "0") or 0))
SNAPSHOT_MAX = SNAPSHOT_MAX_DEFAULT
SNAPSHOT_DIR_DEFAULT_PATH = "/opt/backups"

DEFAULT_SETTINGS: Dict[str, object] = {
    "liveness_auto_recover": False,
    "auto_restart_on_error": False,
    "auto_restart_enabled": False,
    "auto_restart_hours": 0,
    "auto_restart_mem_enabled": False,
    "auto_restart_mem_threshold": 0,
    "auto_snapshot_enabled": False,
    "auto_snapshot_hours": 0,
    "display_wallet_balance": _coerce_bool(os.getenv("BDAG_WALLET_DISPLAY", "0"), False),
    "snapshot_max": SNAPSHOT_MAX_DEFAULT,
    "snapshot_dir": os.getenv("BDAG_SNAPSHOT_DIR", SNAPSHOT_DIR_DEFAULT_PATH),
    "cpu_temp_path": "/mnt/hgfs/vmshared/cpu_temp.txt",
    "wallet_address": "",
    # Overclock preferences (persist UI selections)
    "overclock_data_path": "/home/node/blockdag",
    "overclock_cpu": False,
    "overclock_nvme_latency": False,
    "overclock_scheduler": False,
    "overclock_remount": False,
    "overclock_vm_mode": False,
    "overclock_overlay_bdagchain": False,
    "overclock_overlay_bdageth": False,
    "login_gate_enabled": _initial_login_enabled,
    "login_user": _ENV_LOGIN_USER,
    "login_pass": _ENV_LOGIN_PASS,
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
    global AUTO_RESTART_INTERVAL_SEC
    global _CUSTOM_TEMP_PATH
    global SNAPSHOT_DIR
    global LOGIN_USER
    global LOGIN_PASS
    global LOGIN_ENABLED
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
    interval_hours = settings.get("auto_restart_hours")
    if isinstance(interval_hours, int) and interval_hours > 0:
        AUTO_RESTART_INTERVAL_SEC = max(300.0, float(interval_hours) * 3600.0)
    else:
        AUTO_RESTART_INTERVAL_SEC = LOG_ERROR_RESTART_COOLDOWN_SEC
    _configure_auto_snapshot(settings)
    path_value = str(settings.get("cpu_temp_path") or "").strip()
    if not path_value:
        path_value = os.getenv("BDAG_CPU_TEMP_PATH", "").strip()
    if not path_value:
        path_value = str(DEFAULT_SETTINGS.get("cpu_temp_path", "")).strip()
    _CUSTOM_TEMP_PATH = _normalize_path(path_value)

    requested_snapshot_dir = str(settings.get("snapshot_dir") or "").strip()
    env_snapshot_dir = os.getenv("BDAG_SNAPSHOT_DIR", "").strip()
    effective_snapshot_dir = requested_snapshot_dir or env_snapshot_dir or SNAPSHOT_DIR_DEFAULT_PATH
    SNAPSHOT_DIR = _expanded_path(effective_snapshot_dir, _SNAPSHOT_DIR_FALLBACK)
    try:
        _ensure_directory_rw(SNAPSHOT_DIR, create=True)
    except Exception:
        pass

    login_user = str(settings.get("login_user") or "").strip() or _ENV_LOGIN_USER
    login_pass = str(settings.get("login_pass") or "").strip() or _ENV_LOGIN_PASS
    gate_enabled = bool(settings.get("login_gate_enabled"))
    LOGIN_USER = login_user
    LOGIN_PASS = login_pass
    LOGIN_ENABLED = bool(gate_enabled and login_user and login_pass)


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
            # One-time migration: ensure Overclock toggles default to off
            migration_flag = payload.get("_overclock_defaults_migrated")
            if not migration_flag:
                merged["overclock_cpu"] = False
                merged["overclock_nvme_latency"] = False
                merged["overclock_scheduler"] = False
                merged["overclock_remount"] = False
                # Persist migration so we don't overwrite future changes
                try:
                    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                    temp = dict(merged)
                    temp["_overclock_defaults_migrated"] = True
                    with SETTINGS_PATH.open("w", encoding="utf-8") as handle:
                        json.dump(temp, handle, indent=2, sort_keys=True)
                except Exception as exc:
                    app.logger.warning("failed to persist overclock default migration: %s", exc)
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
        prev_liveness = bool(_SETTINGS_CACHE.get("liveness_auto_recover"))
        _SETTINGS_CACHE.update(filtered)
        _apply_runtime_settings(_SETTINGS_CACHE)
        new_liveness = bool(_SETTINGS_CACHE.get("liveness_auto_recover"))
        cleared_restores = 0
        if prev_liveness and not new_liveness:
            cleared_restores = _clear_liveness_restore_queue()
            if cleared_restores:
                try:
                    app.logger.info(
                        "Cleared %s pending liveness restore job(s) after disabling auto-recover.",
                        cleared_restores,
                    )
                except Exception:
                    pass
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with SETTINGS_PATH.open("w", encoding="utf-8") as handle:
                json.dump(_SETTINGS_CACHE, handle, indent=2, sort_keys=True)
        except Exception as exc:
            app.logger.error("failed to persist settings file %s: %s", SETTINGS_PATH, exc)
        _POLICY_EVENT.set()
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

    def _parse_flag(tokens: List[str], flag: str) -> Optional[str]:
        pref = f"{flag}="
        for idx, token in enumerate(tokens):
            if token.startswith(pref):
                return token[len(pref) :]
            if token == flag and idx + 1 < len(tokens):
                return tokens[idx + 1]
        return None

    if (not user or not password) and env.get("NODE_ARGS"):
        try:
            tokens = shlex.split(env.get("NODE_ARGS", ""))
        except ValueError:
            tokens = env.get("NODE_ARGS", "").split()
        if not user:
            candidate = _parse_flag(tokens, "--rpcuser")
            if candidate:
                user = candidate
        if not password:
            candidate = _parse_flag(tokens, "--rpcpass")
            if candidate:
                password = candidate

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
    candidates: Dict[str, str] = {}
    fallback_url: Optional[str] = None
    preferred_ports = ("38155", "38131", "18593", "18545", "8545", "4545")
    for port_key, bindings in ports.items():
        if not isinstance(port_key, str) or "/tcp" not in port_key:
            continue
        container_port = port_key.split("/")[0]
        binding = bindings[0] if isinstance(bindings, list) and bindings else None
        if not isinstance(binding, dict):
            continue
        host_ip = binding.get("HostIp") or "127.0.0.1"
        host_port = binding.get("HostPort")
        if not host_port:
            continue
        if host_ip in {"0.0.0.0", "::", ""}:
            host_ip = "127.0.0.1"
        url = f"http://{host_ip}:{host_port}"
        candidates.setdefault(container_port, url)
        if fallback_url is None:
            fallback_url = url
        if container_port in {"38155", "38131"}:
            return url
    for preferred in preferred_ports:
        if preferred in candidates:
            return candidates[preferred]
    return fallback_url


def _extract_peer_ports_from_inspect(info: dict) -> Tuple[Optional[int], Optional[int]]:
    ports = info.get("NetworkSettings", {}).get("Ports") or info.get("HostConfig", {}).get("PortBindings") or {}
    if not isinstance(ports, dict):
        return None, None
    preferred_internal = {"18150"}
    fallback_internal: Optional[int] = None
    fallback_external: Optional[int] = None
    for port_key, bindings in ports.items():
        if not isinstance(port_key, str) or "/tcp" not in port_key:
            continue
        container_port = port_key.split("/")[0]
        if not container_port.isdigit():
            continue
        candidate_internal = int(container_port)
        binding = bindings[0] if isinstance(bindings, list) and bindings else None
        host_port = None
        if isinstance(binding, dict):
            host_port = binding.get("HostPort")
        candidate_external = int(host_port) if host_port and str(host_port).isdigit() else None
        if container_port in preferred_internal:
            return candidate_internal, candidate_external
        if fallback_internal is None:
            fallback_internal = candidate_internal
            fallback_external = candidate_external
    return fallback_internal, fallback_external


def _lookup_peer_ports_from_container(container: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not container or not DOCKER_BIN:
        return None, None
    now = time.time()
    with _PEER_PORT_CACHE_LOCK:
        cached = _PEER_PORT_CACHE.get(container)
        if cached and (now - cached[2]) < _PEER_PORT_CACHE_TTL:
            return cached[0], cached[1]
    try:
        inspect = subprocess.check_output(
            [DOCKER_BIN, "inspect", container],
            text=True,
            timeout=3,
        )
        data = json.loads(inspect)[0]
    except Exception:
        return None, None
    internal, external = _extract_peer_ports_from_inspect(data)
    with _PEER_PORT_CACHE_LOCK:
        _PEER_PORT_CACHE[container] = (internal, external, now)
    return internal, external


_BLOCKDAG_DIR_NAME = "blockdag-scripts"
_WALLET_FILE_NAME = "wallet.txt"
_ENV_FILE_PATTERNS: Tuple[str, ...] = ("*.env", "*.env.*")
_ENV_SKIP_SUFFIXES: Tuple[str, ...] = (".example", ".sample", ".template", ".dist")
_WALLET_SCAN_MAX_DEPTH = 4
_BLOCKDAG_SCAN_CACHE_TTL = 60.0
_BLOCKDAG_SCAN_CACHE: Dict[str, Tuple[float, List[Path]]] = {}


def _append_candidate(collection: List[Path], seen: Set[str], candidate) -> None:
    if not candidate:
        return
    try:
        path = candidate if isinstance(candidate, Path) else Path(candidate)
        path = path.expanduser()
    except Exception:
        return
    key = str(path)
    if key in seen:
        return
    seen.add(key)
    collection.append(path)


def _wallet_search_roots() -> List[Path]:
    roots: List[Path] = []
    for raw in (Path("/home"), Path("/root"), Path("/media")):
        try:
            if raw.exists() and raw.is_dir():
                roots.append(raw)
        except Exception:
            continue
    return roots


def _scan_blockdag_dirs(root: Path, max_depth: int = _WALLET_SCAN_MAX_DEPTH) -> List[Path]:
    if max_depth < 0:
        return []
    results: List[Path] = []
    queue: deque[Tuple[Path, int]] = deque()
    queue.append((root, 0))
    visited: Set[str] = set()
    while queue:
        current, depth = queue.popleft()
        try:
            current_key = str(current.resolve())
        except Exception:
            current_key = str(current)
        if current_key in visited:
            continue
        visited.add(current_key)
        try:
            if not current.exists() or not current.is_dir():
                continue
        except Exception:
            continue
        if current.name == _BLOCKDAG_DIR_NAME:
            results.append(current)
            continue
        if depth >= max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except Exception:
                continue
            if entry.is_symlink():
                continue
            queue.append((entry, depth + 1))
    return results


def _discover_blockdag_script_dirs() -> List[Path]:
    now = time.time()
    results: List[Path] = []
    seen: Set[str] = set()
    for root in _wallet_search_roots():
        cache_key = str(root)
        cached = _BLOCKDAG_SCAN_CACHE.get(cache_key)
        if cached and now - cached[0] < _BLOCKDAG_SCAN_CACHE_TTL:
            dirs = cached[1]
        else:
            dirs = _scan_blockdag_dirs(root)
            _BLOCKDAG_SCAN_CACHE[cache_key] = (now, dirs)
        for directory in dirs:
            dir_key = str(directory)
            if dir_key in seen:
                continue
            seen.add(dir_key)
            results.append(directory)
    return results


def _wallet_candidate_paths() -> List[Path]:
    candidates: List[Path] = []
    seen: Set[str] = set()
    env_path = os.getenv("BDAG_WALLET_FILE")
    if env_path:
        _append_candidate(candidates, seen, env_path)
    for script_dir in _discover_blockdag_script_dirs():
        _append_candidate(candidates, seen, script_dir / _WALLET_FILE_NAME)
    return candidates


_WALLET_ENV_KEYS = (
    "PUB_ETH_ADDR",
    "BDAG_WALLET_ADDRESS",
    "ETH_ADDRESS",
)


def _wallet_env_candidate_paths() -> List[Path]:
    candidates: List[Path] = []
    seen: Set[str] = set()
    env_path = os.getenv("BDAG_ENV_FILE")
    if env_path:
        _append_candidate(candidates, seen, env_path)
    for script_dir in _discover_blockdag_script_dirs():
        _append_candidate(candidates, seen, script_dir / ".env")
        for pattern in _ENV_FILE_PATTERNS:
            try:
                for match in script_dir.glob(pattern):
                    name_lower = match.name.lower()
                    if any(name_lower.endswith(suffix) for suffix in _ENV_SKIP_SUFFIXES):
                        continue
                    _append_candidate(candidates, seen, match)
            except Exception:
                continue
    return candidates


def _wallet_from_env_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key.upper() not in _WALLET_ENV_KEYS:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


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


def _ensure_directory_rw(path: Optional[Path], *, create: bool) -> None:
    normalized = _normalize_path(path)
    if not normalized:
        return
    if create:
        try:
            normalized.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
    if not normalized.exists():
        return
    user = _service_username()
    if os.access(normalized, os.R_OK | os.W_OK | os.X_OK):
        return
    setfacl_bin = shutil.which("setfacl")
    if setfacl_bin:
        try:
            subprocess.run(
                [setfacl_bin, "-m", f"u:{user}:rwX", str(normalized)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if os.access(normalized, os.R_OK | os.W_OK | os.X_OK):
                return
        except Exception:
            pass
    if os.geteuid() == 0:
        try:
            current_mode = stat.S_IMODE(os.stat(normalized).st_mode)
            os.chmod(normalized, current_mode | 0o770)
        except Exception:
            pass
    chown_hint = None
    chown_error = None
    if not os.access(normalized, os.R_OK | os.W_OK | os.X_OK):
        user = _service_username()
        try:
            group = grp.getgrgid(os.getegid()).gr_name
        except Exception:
            group = user
        chown_cmd = ["chown", "-R", f"{user}:{group}", str(normalized)]
        if os.geteuid() != 0:
            chown_cmd = ["sudo", "-n", *chown_cmd]
        try:
            result = subprocess.run(
                chown_cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0 and os.geteuid() != 0:
                chown_hint = f"sudo chown -R {user}:{group} {normalized}"
                chown_error = (result.stderr or result.stdout or "").strip()
        except Exception:
            pass
        if os.access(normalized, os.R_OK | os.W_OK | os.X_OK):
            return
    if not os.access(normalized, os.R_OK | os.W_OK | os.X_OK):
        try:
            app.logger.warning(
                "Insufficient permissions on %s for user %s; snapshot and restore operations may fail%s%s",
                normalized,
                _service_username(),
                f". Try {chown_hint}" if chown_hint else "",
                f" (detail: {chown_error})" if chown_error else "",
            )
        except Exception:
            pass


def _preferred_path(candidates: Iterable[str], fallback: str) -> str:
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
        except Exception:
            continue
        parent = path.parent if path.parent != path else path
        if path.exists() or parent.exists():
            return str(path)
    return fallback


_SNAPSHOT_DATA_FALLBACK = _preferred_path(
    [
        "/media/node/nvme1/blockdag-data",
        "/media/node/nvme1/blockdag",
        "/media/nvme1/blockdag-data",
        "/media/nvme1/blockdag",
        "/home/node/blockdag",
        "/home/node/blockdag/blockdag-scripts/bin/bdag/data",
    ],
    "/home/node/blockdag/blockdag-scripts/bin/bdag/data",
)
SNAPSHOT_DATA_DIR = _expanded_path(os.getenv("BDAG_SNAPSHOT_DATA_DIR"), _SNAPSHOT_DATA_FALLBACK)
_SNAPSHOT_DIR_FALLBACK = SNAPSHOT_DIR_DEFAULT_PATH
SNAPSHOT_DIR = _expanded_path(os.getenv("BDAG_SNAPSHOT_DIR"), _SNAPSHOT_DIR_FALLBACK)
try:
    _ensure_directory_rw(SNAPSHOT_DIR, create=True)
except Exception:
    pass
try:
    _ensure_directory_rw(SNAPSHOT_DATA_DIR, create=False)
except Exception:
    pass
_CUSTOM_TEMP_PATH = _normalize_path(os.getenv("BDAG_CPU_TEMP_PATH") or "")
_SENSOR_PACKAGES = ("lm-sensors",)
_CUSTOM_TEMP_PATH = _normalize_path(os.getenv("BDAG_CPU_TEMP_PATH") or "")
SNAPSHOT_PREFIX = (os.getenv("BDAG_SNAPSHOT_PREFIX", "bdag.chaindata") or "bdag.chaindata").strip() or "bdag.chaindata"
SNAPSHOT_SUFFIX = (os.getenv("BDAG_SNAPSHOT_SUFFIX", ".tar") or ".tar").strip()
_snapshot_stage_env = _parse_bool_env(os.getenv("BDAG_SNAPSHOT_STAGE_COPY"))
if _snapshot_stage_env is None:
    SNAPSHOT_STAGE_COPY = True
else:
    SNAPSHOT_STAGE_COPY = _snapshot_stage_env
_SNAPSHOT_STAGE_PARENT = _normalize_path(os.getenv("BDAG_SNAPSHOT_STAGE_PARENT") or "")
if _SNAPSHOT_STAGE_PARENT:
    try:
        _SNAPSHOT_STAGE_PARENT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
SNAPSHOT_LIGHT_MODE = _coerce_bool(os.getenv("BDAG_SNAPSHOT_LIGHT_MODE"), False)
SNAPSHOT_HEALTH_ENABLED = _coerce_bool(os.getenv("BDAG_SNAPSHOT_HEALTH_ENABLED", "1"), True)
SNAPSHOT_HEALTH_MAX_HEIGHT_DELTA = max(
    0, int(os.getenv("BDAG_SNAPSHOT_MAX_HEIGHT_DELTA", os.getenv("BDAG_SNAPSHOT_MAX_DELTA", "3")) or "3")
)
SNAPSHOT_HEALTH_MIN_SYNC_PROGRESS = max(
    0.0, min(100.0, float(os.getenv("BDAG_SNAPSHOT_MIN_SYNC_PROGRESS", "99.0") or "99.0"))
)
SNAPSHOT_HEALTH_MIN_UPTIME_SEC = max(0, int(os.getenv("BDAG_SNAPSHOT_MIN_UPTIME_SEC", "120") or "120"))
SNAPSHOT_HEALTH_MIN_PEERS = max(0, int(os.getenv("BDAG_SNAPSHOT_MIN_PEERS", "1") or "1"))
SNAPSHOT_HEALTH_LOG_TAIL = max(50, min(int(os.getenv("BDAG_SNAPSHOT_LOG_TAIL", "200") or "200"), 1000))
_SNAPSHOT_IDENTITY_RELATIVE_PATHS: Tuple[Path, ...] = (
    Path("network.key"),
    Path("testnet") / "network.key",
    Path("data") / "network.key",
    Path("data") / "testnet" / "network.key",
)
_snapshot_guard_patterns_env = [
    part.strip()
    for part in str(os.getenv("BDAG_SNAPSHOT_LOG_GUARD_PATTERNS") or "").split(",")
    if part.strip()
]
if _snapshot_guard_patterns_env:
    _snapshot_guard_patterns = _snapshot_guard_patterns_env
else:
    _snapshot_guard_patterns = [
        "unclean shutdown",
        "head state missing",
        "snapshot journal",
        "zero state root",
        "truncating freezer table",
        "repairing freezer",
        "panic:",
        "fatal error",
    ]
SNAPSHOT_HEALTH_LOG_GUARD_PATTERNS: Tuple[Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in _snapshot_guard_patterns
)
LEGACY_SNAPSHOT_PATTERNS = [
    "blockdag-chaindata-*.tar.gz",
    f"{SNAPSHOT_PREFIX}-*.tar.gz",
]

_CORRUPTION_RESTORE_KEYWORDS: Tuple[str, ...] = (
    "chain db: need to thoroughly clean up old data",
    "the dag data was damaged",
    "can't find tip",
    "bdag chain env error",
    "can't find cur block state",
    "illegal withdrawal at block",
    "cleanup your block data base by '--cleanup'",
    "unknown to the objstorage provider",
    "unclean shutdown detected",
)

def _should_trigger_corruption_restore(reason: Optional[str]) -> bool:
    if not reason:
        return False
    text = str(reason).strip().lower()
    if not text:
        return False
    return any(keyword in text for keyword in _CORRUPTION_RESTORE_KEYWORDS)

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
_PENDING_RESTORE_QUEUE: Deque[Dict[str, Optional[str]]] = deque()
_PENDING_RESTORE_LOCK = threading.Lock()


def _queue_pending_restore(node_id: Optional[str], trigger: Optional[str]) -> None:
    if not node_id:
        return
    with _PENDING_RESTORE_LOCK:
        if any(entry.get("node") == node_id for entry in _PENDING_RESTORE_QUEUE):
            return
        _PENDING_RESTORE_QUEUE.append({"node": node_id, "trigger": trigger})


def _dispatch_pending_restore() -> None:
    entry: Optional[Dict[str, Optional[str]]] = None
    with _PENDING_RESTORE_LOCK:
        if not _PENDING_RESTORE_QUEUE:
            return
        entry = _PENDING_RESTORE_QUEUE.popleft()
    if not entry:
        return
    node_id = entry.get("node")
    trigger = entry.get("trigger")
    ok, message, _ = _start_restore_job(node_id, trigger=trigger)
    if not ok and message and "already in progress" in message.lower():
        _queue_pending_restore(node_id, trigger)


def _clear_liveness_restore_queue() -> int:
    """Drop pending restores that were queued by the liveness policy."""
    cleared = 0
    with _PENDING_RESTORE_LOCK:
        if not _PENDING_RESTORE_QUEUE:
            return 0
        kept: Deque[Dict[str, Optional[str]]] = deque()
        while _PENDING_RESTORE_QUEUE:
            entry = _PENDING_RESTORE_QUEUE.popleft()
            trigger = str(entry.get("trigger") or "").strip().lower()
            if trigger == "liveness":
                cleared += 1
                continue
            kept.append(entry)
        if kept:
            _PENDING_RESTORE_QUEUE.extend(kept)
    return cleared


def _estimate_dir_size_bytes(directory: Optional[Path]) -> int:
    normalized = _normalize_path(directory)
    if not normalized or not normalized.exists():
        return 0

    exclude_dirs = {"overlay-backup"}

    def _path_size(path: Path, *, prune: Optional[Set[str]] = None) -> int:
        if not path.exists():
            return 0
        if prune is None:
            try:
                out = subprocess.check_output(
                    ["du", "-sb", str(path)],
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                parts = out.strip().split()
                if parts:
                    size_val = max(int(parts[0]), 0)
                    if size_val > 0:
                        return size_val
            except Exception:
                pass
        total_size = 0
        try:
            for root, dirs, files in os.walk(path):
                if prune:
                    dirs[:] = [d for d in dirs if d not in prune]
                for name in files:
                    try:
                        total_size += (Path(root) / name).stat().st_size
                    except Exception:
                        continue
        except Exception:
            pass
        if total_size > 0:
            return total_size
        if DOCKER_BIN and prune is None:
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
                        f"{path}:/data:ro",
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
        return max(total_size, 0)

    base_size = _path_size(normalized, prune=None)
    if base_size > 0:
        for name in exclude_dirs:
            sub_path = normalized / name
            if sub_path.exists():
                base_size -= _path_size(sub_path, prune=None)
        return max(base_size, 0)

    # Fallback: direct walk while pruning excluded directories
    return _path_size(normalized, prune=exclude_dirs)


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


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    name = member.name or ""
    if name.startswith("/") or name.startswith("\\"):
        raise RuntimeError(f"Snapshot contains unsafe path entry: {name}")
    parts = Path(name).parts
    if any(part == ".." for part in parts):
        raise RuntimeError(f"Snapshot contains unsafe path traversal: {name}")


def _extract_snapshot_contents(
    snapshot_path: Path,
    target_dir: Path,
    *,
    total_bytes: int,
    started: float,
) -> None:
    try:
        tar = tarfile.open(snapshot_path, "r:*")
    except tarfile.TarError as exc:
        raise RuntimeError(f"Failed to open snapshot archive: {exc}") from exc
    try:
        members = tar.getmembers()
        member_total = sum(max(member.size, 0) for member in members if member.isfile() or member.islnk() or member.issym())
        total = member_total or total_bytes
        processed = 0
        last_emit = 0.0
        for member in members:
            _validate_tar_member(member)
            tar.extract(member, path=target_dir)
            if member.isfile() or member.islnk() or member.issym():
                processed += max(member.size, 0)
            now = time.time()
            if now - last_emit >= 0.4:
                elapsed = max(now - started, 0.0)
                pct = None
                eta = None
                if total > 0:
                    pct = max(0.0, min(100.0, (processed / total) * 100.0))
                if elapsed > 0 and total > 0 and processed > 0:
                    remaining = max(total - processed, 0)
                    if remaining > 0:
                        speed = processed / max(elapsed, 1e-6)
                        if speed > 0:
                            eta = remaining / speed
                _snapshot_progress_update(
                    {
                        "bytes_written": processed,
                        "total_bytes": total,
                        "pct": pct,
                                                "eta_seconds": eta,
                        "updated": now,
                        "path": str(snapshot_path),
                        "started": started,
                    }
                )
                last_emit = now
        final_now = time.time()
        final_total = total or processed
        _snapshot_progress_update(
            {
                "bytes_written": processed,
                "total_bytes": final_total,
                "pct": 100.0 if final_total else None,
                                "eta_seconds": 0.0 if final_total else None,
                "updated": final_now,
                "path": str(snapshot_path),
                "started": started,
            }
        )
    finally:
        tar.close()


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for entry in os.scandir(path):
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    total += entry_path.stat().st_size
                elif entry.is_dir():
                    total += _directory_size(entry_path)
            except FileNotFoundError:
                continue
    except (FileNotFoundError, PermissionError):
        return total
    return total


def _restore_via_docker(
    snapshot_path: Path,
    data_dir: Path,
    backup_name: str,
    *,
    expected_total: int,
    archive_bytes: int,
    started: float,
) -> None:
    parent_dir = data_dir.parent
    service_uid = os.getuid()
    service_gid = os.getgid()
    script = (
        "set -e\n"
        "cd /volume\n"
        f"if [ -d '{data_dir.name}' ]; then mv '{data_dir.name}' '{backup_name}'; fi\n"
        f"mkdir -p '{data_dir.name}'\n"
        "progress() {\n"
        "  pid=\"$1\"\n"
        "  while kill -0 \"$pid\" 2>/dev/null; do\n"
        f"    sz=$(du -s '{data_dir.name}' 2>/dev/null | awk '{{print $1}}')\n"
        "    sz=${sz:-0}\n"
        "    echo \"PROGRESS $sz\"\n"
        "    sleep 1\n"
        "  done\n"
        "}\n"
        f"tar -xf '/backup/{snapshot_path.name}' -C '{data_dir.name}' &\n"
        "tar_pid=$!\n"
        "progress \"$tar_pid\" &\n"
        "progress_pid=$!\n"
        "set +e\n"
        "wait \"$tar_pid\"\n"
        "tar_status=$?\n"
        "kill \"$progress_pid\" 2>/dev/null || true\n"
        "wait \"$progress_pid\" 2>/dev/null || true\n"
        "set -e\n"
        "echo \"PROGRESS_DONE\"\n"
        "if [ $tar_status -ne 0 ]; then exit $tar_status; fi\n"
        f"if [ -d '{data_dir.name}/data/testnet' ] && [ ! -e '{data_dir.name}/testnet' ]; then \n"
        f"  cd '{data_dir.name}'\n"
        "  for entry in data/*; do [ -e \"$entry\" ] || break; mv \"$entry\" .; done\n"
        "  rmdir data 2>/dev/null || rm -rf data\n"
        "  cd /volume\n"
        "fi\n"
        f"chown -R {service_uid}:{service_gid} '{data_dir.name}'\n"
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
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    bytes_written = 0
    total_hint = float(max(expected_total, archive_bytes))
    def _next_total_hint(current: float, observed: int) -> float:
        """Provide a forward-looking upper bound so ETA does not collapse prematurely."""
        observed = float(max(observed, 0))
        baseline = current if current and current > 0 else 0.0
        # Always stay at least a little ahead of the observed usage so remaining>0 while extracting.
        growth = max(observed * 0.02, 256 * 1024 * 1024)  # min 256MB or 2%
        candidate = observed + growth
        if baseline <= 0:
            return candidate
        return max(baseline, candidate)
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            line = line.strip()
            if line.startswith("PROGRESS_DONE"):
                continue
            if line.startswith("PROGRESS"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        kilobytes = int(parts[1])
                        bytes_written = max(kilobytes, 0) * 1024
                    except ValueError:
                        continue
                    now = time.time()
                    if bytes_written > total_hint:
                        total_hint = _next_total_hint(total_hint, bytes_written)
                    total = max(total_hint, archive_bytes)
                    if total <= 0:
                        total = max(bytes_written, archive_bytes, 1)
                    pct = None
                    eta = None
                    if total > 0:
                        pct = max(0.0, min(100.0, (bytes_written / total) * 100.0))
                    elapsed = max(now - started, 0.0)
                    if elapsed > 0 and total > 0 and bytes_written > 0:
                        remaining = max(total - bytes_written, 0)
                        if remaining > 0:
                            eta = remaining / max(bytes_written / elapsed, 1e-6)
                    _snapshot_progress_update(
                        {
                            "bytes_written": bytes_written,
                            "total_bytes": total,
                            "pct": pct,
                            "eta_seconds": eta,
                            "updated": now,
                            "path": str(snapshot_path),
                            "started": started,
                        }
                    )
        process.wait()
    finally:
        try:
            process.stdout and process.stdout.close()
        except Exception:
            pass
        try:
            process.stderr and process.stderr.close()
        except Exception:
            pass
    final_now = time.time()
    if process.returncode not in (0, None):
        stdout, stderr = process.communicate()
        raise RuntimeError(stderr or stdout or f"Command exited with status {process.returncode}")
    final_bytes = max(bytes_written, _directory_size(data_dir))
    final_total = max(total_hint, final_bytes, archive_bytes)
    _snapshot_progress_update(
        {
            "bytes_written": final_bytes,
            "total_bytes": final_total,
            "pct": 100.0 if final_total else None,
            "eta_seconds": 0.0 if final_total else None,
            "updated": final_now,
            "path": str(snapshot_path),
            "started": started,
        }
    )


def _prune_pre_restore_backups(data_dir: Path, retain: Optional[int] = None) -> None:
    """Limit the number of pre-restore directories kept next to the live data."""
    if not data_dir:
        return
    try:
        parent_dir = data_dir.parent
    except Exception:
        return
    if not parent_dir or not parent_dir.exists():
        return
    if retain is None:
        retain = PRE_RESTORE_BACKUP_RETENTION
    if retain < 0:
        retain = 0
    prefix = f"{data_dir.name}.pre-restore."
    pattern = re.compile(rf"^{re.escape(data_dir.name)}\.pre-restore\.(\d{{8}}\.\d{{6}})$")
    candidates: List[Tuple[float, Path]] = []
    try:
        for child in parent_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if not name.startswith(prefix):
                continue
            score = 0.0
            match = pattern.match(name)
            if match:
                try:
                    score = datetime.strptime(match.group(1), "%Y%m%d.%H%M%S").timestamp()
                except ValueError:
                    score = 0.0
            if not score:
                try:
                    score = child.stat().st_mtime
                except OSError:
                    score = 0.0
            candidates.append((score, child))
    except Exception as exc:
        app.logger.warning("Failed to enumerate pre-restore backups in %s: %s", parent_dir, exc)
        return
    if not candidates:
        return
    candidates.sort(key=lambda item: item[0], reverse=True)
    keep: Set[Path] = set()
    for _, path in candidates[:retain]:
        keep.add(path)
    for _, path in candidates[retain:]:
        if path in keep:
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            continue
        except Exception as exc:
            app.logger.warning("Failed to prune pre-restore backup %s: %s", path, exc)


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


def _start_container(name: Optional[str], retries: int = 3, retry_delay: float = 5.0) -> bool:
    if not name or not DOCKER_BIN:
        return False
    exists, running, _ = _container_state(name)
    if not exists:
        return False
    if running:
        return True
    attempts = max(1, int(retries))
    delay = max(0.0, float(retry_delay))
    last_error: Optional[str] = None
    for attempt in range(1, attempts + 1):
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
            err_text = (exc.stderr or exc.stdout or str(exc)).strip()
            last_error = err_text
            if attempt >= attempts:
                break
            lowered = err_text.lower()
            if (
                "address already in use" in lowered
                or "bind host port" in lowered
                or "failed to set up container networking" in lowered
            ):
                time.sleep(delay or 1.0)
                delay = min(delay * 2 if delay else 2.0, 30.0)
                continue
            raise RuntimeError(err_text)
    raise RuntimeError(last_error or "Failed to start container.")


def _ensure_clean_container(name: Optional[str]) -> None:
    """Stop the container if it is running so restores can safely reuse it."""
    if not name or not DOCKER_BIN:
        return
    exists, running, _ = _container_state(name)
    if not exists:
        return
    if running:
        _stop_container(name, timeout=45)


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
    try:
        node_home = Path("/home/node")
        if node_home.exists():
            for entry in node_home.iterdir():
                if entry.is_dir() and re.search(r"blockdag[-_]?node", entry.name, re.IGNORECASE):
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


def _snapshot_patterns() -> List[str]:
    patterns = [f"{SNAPSHOT_PREFIX}*.tar"]
    patterns.extend(LEGACY_SNAPSHOT_PATTERNS)
    return patterns


def _ensure_snapshot_dir() -> Path:
    with _SNAPSHOT_DIR_LOCK:
        directory = SNAPSHOT_DIR
        _ensure_directory_rw(directory, create=True)
        return directory

def _ensure_sensor_packages() -> None:
    if shutil.which("sensors"):
        return
    apt = shutil.which("apt-get")
    if not apt:
        return
    try:
        subprocess.run([apt, "update"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass
    try:
        subprocess.run(
            [apt, "install", "-y"] + list(_SENSOR_PACKAGES),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        pass


def list_snapshots() -> List[dict]:
    directory = _ensure_snapshot_dir()
    patterns = _snapshot_patterns()
    files: List[Path] = []
    for pattern in patterns:
        try:
            files.extend(directory.glob(pattern))
        except Exception:
            continue
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


def _stage_snapshot_source(data_dir: Path, snapshot_dir: Path) -> Path:
    base_dir = _SNAPSHOT_STAGE_PARENT or snapshot_dir
    staging_root = base_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="stage-", dir=str(staging_root)))
    source = str(data_dir)
    target = str(staging_dir)
    if not source.endswith("/"):
        source = source + "/"
    if not target.endswith("/"):
        target = target + "/"
    rsync_cmd = [
        "rsync",
        "-a",
        "--delete",
        "--numeric-ids",
        "--inplace",
        "--exclude",
        "overlay-backup",
        "--exclude",
        "overlay-backup/*",
        source,
        target,
    ]
    result = subprocess.run(
        rsync_cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 24):
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass
        message = result.stderr or result.stdout or f"rsync exited with status {result.returncode}"
        raise RuntimeError(message)
    if result.returncode == 24:
        warning = result.stderr or result.stdout or "rsync reported vanished files during snapshot staging"
        try:
            app.logger.warning(warning.strip())
        except Exception:
            pass
    try:
        app.logger.info("Snapshot staging complete at %s", staging_dir)
    except Exception:
        pass
    return staging_dir


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


def _snapshot_health_check(
    ctx: Optional["NodeContext"], *, mode: str = "snapshot"
) -> Tuple[bool, str, Dict[str, object]]:
    """Ensure the node is in a safe state before snapshotting/restoring."""
    mode_name = mode or "snapshot"
    info: Dict[str, object] = {
        "enabled": SNAPSHOT_HEALTH_ENABLED,
        "mode": mode_name,
        "checked_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "thresholds": {
            "max_height_delta": SNAPSHOT_HEALTH_MAX_HEIGHT_DELTA,
            "min_sync_progress": SNAPSHOT_HEALTH_MIN_SYNC_PROGRESS,
            "min_uptime_sec": SNAPSHOT_HEALTH_MIN_UPTIME_SEC,
            "min_peers": SNAPSHOT_HEALTH_MIN_PEERS,
            "log_tail": SNAPSHOT_HEALTH_LOG_TAIL,
        },
    }
    if not SNAPSHOT_HEALTH_ENABLED:
        info["skipped"] = "disabled"
        return True, "Snapshot health guard disabled", info
    if ctx is None:
        info["skipped"] = "no-context"
        return True, "No node context available", info
    try:
        metrics = ctx.sample(force=True)
    except Exception as exc:
        info["error"] = str(exc)
        return False, f"Failed to collect node metrics: {exc}", info
    info["metrics"] = metrics
    enforce_running = mode_name != "restore"
    if enforce_running and not metrics.get("container_running"):
        reason = f"Container {ctx.container or ctx.id} is not running"
        info["failure"] = "container-stopped"
        info["result"] = reason
        return False, reason, info
    if enforce_running and not metrics.get("running"):
        reason = f"Node {ctx.id} does not report a running state"
        info["failure"] = "node-not-running"
        info["result"] = reason
        return False, reason, info
    uptime = metrics.get("uptime_seconds")
    if enforce_running and isinstance(uptime, (int, float)) and uptime < SNAPSHOT_HEALTH_MIN_UPTIME_SEC:
        reason = (
            f"Node uptime {int(uptime)}s is below required "
            f"{SNAPSHOT_HEALTH_MIN_UPTIME_SEC}s for a clean snapshot window"
        )
        info["failure"] = "uptime-too-low"
        info["result"] = reason
        wait_seconds = max(0, int(SNAPSHOT_HEALTH_MIN_UPTIME_SEC - uptime))
        info["wait_seconds"] = wait_seconds
        info["next_allowed_unix"] = time.time() + wait_seconds
        return False, reason, info
    enforce_sync = mode_name != "restore"
    progress = metrics.get("sync_progress")
    try:
        progress_val = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress_val = None
    if enforce_sync and progress_val is not None and progress_val < SNAPSHOT_HEALTH_MIN_SYNC_PROGRESS:
        reason = (
            f"Node sync progress {progress_val:.2f}% is below the "
            f"{SNAPSHOT_HEALTH_MIN_SYNC_PROGRESS}% guardrail"
        )
        info["failure"] = "sync-progress"
        info["result"] = reason
        return False, reason, info
    suspicious: List[str] = []
    if ctx.container:
        try:
            tail = _get_recent_logs(SNAPSHOT_HEALTH_LOG_TAIL, ctx.container)
        except Exception as exc:
            tail = []
            info["log_error"] = str(exc)
        info["log_lines_checked"] = len(tail or [])
        if tail:
            for raw in tail:
                text = str(raw)
                lowered = text.lower()
                if any(pattern.search(lowered) for pattern in SNAPSHOT_HEALTH_LOG_GUARD_PATTERNS):
                    suspicious.append(text.strip())
            if suspicious and enforce_running:
                info["log_hits"] = suspicious[:10]
                info["failure"] = "recent-log-warning"
                message = "Recent logs show repair/corruption activity; snapshot aborted"
                info["result"] = message
                return False, message, info
            if suspicious and not enforce_running:
                info["log_hits"] = suspicious[:10]
    success_message = "Node passed snapshot health checks"
    info["result"] = success_message
    return True, success_message, info


def _configure_auto_snapshot(settings: Dict[str, object]) -> None:
    enabled = bool(settings.get("auto_snapshot_enabled"))
    raw_hours = settings.get("auto_snapshot_hours")
    try:
        hours_value = float(raw_hours)
    except (TypeError, ValueError):
        hours_value = 0.0
    interval_sec = max(0.0, hours_value * 3600.0)
    if enabled and interval_sec > 0.0:
        interval_sec = max(interval_sec, AUTO_SNAPSHOT_MIN_INTERVAL_SEC)
    else:
        enabled = False
        interval_sec = 0.0
    now = time.time()
    with _AUTO_SNAPSHOT_LOCK:
        _AUTO_SNAPSHOT_STATE["enabled"] = enabled
        _AUTO_SNAPSHOT_STATE["interval"] = interval_sec
        if not enabled or interval_sec <= 0.0:
            _AUTO_SNAPSHOT_STATE["next_run"] = 0.0
        else:
            last_run = float(_AUTO_SNAPSHOT_STATE.get("last_run") or 0.0)
            next_run = float(_AUTO_SNAPSHOT_STATE.get("next_run") or 0.0)
            if last_run > 0.0:
                candidate = last_run + interval_sec
                if candidate < now + 60.0:
                    candidate = now + 60.0
                next_run = candidate
            elif next_run <= 0.0 or next_run < now:
                next_run = now + min(interval_sec, 300.0)
            _AUTO_SNAPSHOT_STATE["next_run"] = next_run
    _AUTO_SNAPSHOT_EVENT.set()


def _auto_snapshot_mark_result(status: str) -> None:
    now = time.time()
    with _AUTO_SNAPSHOT_LOCK:
        _AUTO_SNAPSHOT_STATE["last_result"] = status
        enabled = bool(_AUTO_SNAPSHOT_STATE.get("enabled"))
        interval = float(_AUTO_SNAPSHOT_STATE.get("interval") or 0.0)
        if status == "completed":
            _AUTO_SNAPSHOT_STATE["last_run"] = now
            if enabled and interval > 0.0:
                _AUTO_SNAPSHOT_STATE["next_run"] = now + interval
            else:
                _AUTO_SNAPSHOT_STATE["next_run"] = 0.0
        else:
            if enabled and interval > 0.0:
                _AUTO_SNAPSHOT_STATE["next_run"] = now + max(AUTO_SNAPSHOT_RETRY_SEC, 120.0)
            else:
                _AUTO_SNAPSHOT_STATE["next_run"] = 0.0
    job_snapshot = _snapshot_job_snapshot()
    details = job_snapshot.get("details") if isinstance(job_snapshot, dict) else {}
    if not isinstance(details, dict):
        details = {}
    node_id = details.get("node")
    container = details.get("container")
    label = details.get("label") or node_id
    message = job_snapshot.get("message") if isinstance(job_snapshot, dict) else None
    summary = message or (f"Auto snapshot {status}" if label else "Auto snapshot update")
    _automation_event(
        "auto_snapshot",
        summary,
        node=node_id,
        container=container,
        status=status,
        metadata={
            "node_label": label,
            "path": details.get("path"),
        },
    )
    _AUTO_SNAPSHOT_EVENT.set()


def _select_best_snapshot_node() -> Optional[str]:
    best_id: Optional[str] = None
    best_local: int = -1
    best_delta: float = float("inf")
    for ctx in NODES.values():
        try:
            metrics = ctx.sample(force=True)
        except Exception:
            continue
        if not metrics:
            continue
        running = bool(metrics.get("running"))
        container_running = metrics.get("container_running")
        if not running or (container_running is False):
            continue
        if bool(metrics.get("stalled")):
            continue
        health_text = str(metrics.get("health_text") or "").strip()
        if health_text:
            continue
        local_height = metrics.get("local_height")
        remote_height = metrics.get("remote_height")
        if not isinstance(local_height, (int, float)):
            continue
        height_delta = metrics.get("height_delta")
        if not isinstance(height_delta, (int, float)):
            if isinstance(remote_height, (int, float)):
                height_delta = remote_height - local_height
            else:
                height_delta = 0.0
        if height_delta is None:
            height_delta = 0.0
        try:
            height_delta = float(height_delta)
        except Exception:
            height_delta = float("inf")
        if height_delta < 0:
            height_delta = 0.0
        try:
            local_val = int(local_height)
        except Exception:
            local_val = -1
        if local_val > best_local or (local_val == best_local and height_delta < best_delta):
            best_local = local_val
            best_delta = height_delta
            best_id = ctx.id
    return best_id


def _auto_snapshot_worker() -> None:
    while True:
        with _AUTO_SNAPSHOT_LOCK:
            enabled = bool(_AUTO_SNAPSHOT_STATE.get("enabled"))
            interval = float(_AUTO_SNAPSHOT_STATE.get("interval") or 0.0)
            next_run = float(_AUTO_SNAPSHOT_STATE.get("next_run") or 0.0)
        if not enabled or interval <= 0.0:
            triggered = _AUTO_SNAPSHOT_EVENT.wait(timeout=600.0)
            if triggered:
                _AUTO_SNAPSHOT_EVENT.clear()
            continue
        now = time.time()
        if next_run <= 0.0:
            with _AUTO_SNAPSHOT_LOCK:
                _AUTO_SNAPSHOT_STATE["next_run"] = now + interval
            continue
        if now >= next_run:
            job = _snapshot_job_snapshot()
            if job.get("active"):
                with _AUTO_SNAPSHOT_LOCK:
                    _AUTO_SNAPSHOT_STATE["next_run"] = now + max(60.0, min(interval, 300.0))
                continue
            best_node = _select_best_snapshot_node()
            ok, message, job = _start_snapshot_job(best_node, mode="auto_snapshot", trigger="auto", quiesce_overlay=True)
            with _AUTO_SNAPSHOT_LOCK:
                if ok:
                    _AUTO_SNAPSHOT_STATE["next_run"] = now + max(interval, AUTO_SNAPSHOT_MIN_INTERVAL_SEC)
                else:
                    _AUTO_SNAPSHOT_STATE["next_run"] = now + max(AUTO_SNAPSHOT_RETRY_SEC, 120.0)
            job_details = job.get("details") if isinstance(job, dict) else {}
            if not isinstance(job_details, dict):
                job_details = {}
            node_label = (job_details or {}).get("label") or (job_details or {}).get("node")
            container = (job_details or {}).get("container")
            if ok:
                _automation_event(
                    "auto_snapshot",
                    f"Auto snapshot started{f' for {node_label}' if node_label else ''}",
                    node=job_details.get("node") if isinstance(job_details, dict) else best_node,
                    container=container,
                    status="started",
                    metadata={"message": message, "node_label": node_label},
                )
            else:
                _automation_event(
                    "auto_snapshot",
                    "Auto snapshot skipped",
                    node=job_details.get("node") if isinstance(job_details, dict) else best_node,
                    container=container,
                    status="skipped",
                    metadata={"message": message, "node_label": node_label},
                )
            try:
                if ok:
                    app.logger.info("Auto snapshot started (interval %.0fs).", interval)
                else:
                    app.logger.warning("Auto snapshot skipped: %s", message)
            except Exception:
                pass
            continue
        wait_time = max(30.0, min(300.0, next_run - now))
        triggered = _AUTO_SNAPSHOT_EVENT.wait(timeout=wait_time)
        if triggered:
            _AUTO_SNAPSHOT_EVENT.clear()


def _policy_worker() -> None:
    interval = POLICY_WORKER_INTERVAL_SEC
    while True:
        try:
            nodes = list(NODES.values())
            if nodes:
                settings = get_settings()
                for ctx in nodes:
                    try:
                        ctx.sample(force=False)
                    except Exception:
                        continue
                    _apply_node_policies(ctx, settings)
        except Exception as exc:
            try:
                app.logger.warning("policy worker encountered an error: %s", exc)
            except Exception:
                pass
        triggered = _POLICY_EVENT.wait(timeout=interval)
        if triggered:
            _POLICY_EVENT.clear()


def _update_snapshot_dir(new_dir: Path) -> bool:
    normalized = _normalize_path(new_dir)
    if not normalized:
        return False
    with _SNAPSHOT_DIR_LOCK:
        global SNAPSHOT_DIR
        if SNAPSHOT_DIR == normalized:
            return False
        SNAPSHOT_DIR = normalized
        _ensure_directory_rw(SNAPSHOT_DIR, create=True)
    return True


def _run_snapshot_job(details: Dict[str, object]) -> None:
    dest_name: Optional[str] = None
    dest_path: Optional[Path] = None
    container = (details or {}).get("container") if details else None
    quiesce_overlay = bool(details.get("quiesce_overlay", True)) if details else True
    restart_required = False
    staging_dir: Optional[Path] = None
    try:
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
            if restart_required:
                try:
                    subprocess.run(["sync"], check=False)
                except Exception:
                    pass
        overlay_targets: Set[str] = set()
        flushed_overlays: List[str] = []
        if _overclock_overlay_enabled():
            try:
                overlay_targets = _overlay_targets_for_path(data_dir)
            except Exception as exc:
                _oc_log(f"Snapshot guard: failed to enumerate overlays: {exc}")
                overlay_targets = set()
            if overlay_targets:
                try:
                    flushed_overlays = _overlay_flush_to_disk(overlay_targets)
                    if flushed_overlays:
                        labs = ", ".join(sorted(flushed_overlays))
                        _oc_log(f"Snapshot guard: overlays flushed to disk [{labs}]")
                except Exception as exc:
                    _oc_log(f"Snapshot guard: overlay flush failed: {exc}")
                    raise RuntimeError(str(exc)) from exc
            if flushed_overlays:
                details.setdefault("overlays", sorted(flushed_overlays))
        source_dir = data_dir
        if SNAPSHOT_STAGE_COPY and not SNAPSHOT_LIGHT_MODE:
            try:
                staging_dir = _stage_snapshot_source(data_dir, directory)
                source_dir = staging_dir
            except Exception as exc:
                raise RuntimeError(f"Snapshot staging failed: {exc}")
        total_bytes = _estimate_dir_size_bytes(source_dir)
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
                f"{source_dir}:/data:ro",
                "-v",
                f"{directory}:/backup",
                "busybox",
                "tar",
                "--exclude=overlay-backup",
                "--exclude=overlay-backup/*",
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
            parent = source_dir.parent
            arcname = source_dir.name
            command = [
                "tar",
                "--warning=no-file-changed",
                "--ignore-failed-read",
                "--exclude=overlay-backup",
                "--exclude=overlay-backup/*",
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
                        "--exclude=overlay-backup",
                        "--exclude=overlay-backup/*",
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
        flushed = details.get("overlays") if isinstance(details, dict) else None
        if flushed:
            message += f" (overlays flushed: {', '.join(flushed)})"
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
        if staging_dir:
            try:
                shutil.rmtree(staging_dir, ignore_errors=True)
            except Exception:
                pass
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
        if details.get("trigger") == "auto":
            _auto_snapshot_mark_result(status)
        trigger_label = (details or {}).get("trigger")
        if trigger_label == "liveness":
            _automation_event(
                "chain_restore",
                message,
                node=details.get("node"),
                container=details.get("container"),
                status=status,
                metadata={"trigger": trigger_label},
            )
        if status == "completed":
            label = (details or {}).get("label") or (details or {}).get("node")
            summary = message or (f"Snapshot job completed{f' for {label}' if label else ''}")
            _automation_event(
                "snapshot_job",
                summary,
                node=(details or {}).get("node"),
                container=(details or {}).get("container"),
                status="success",
                metadata={
                    "path": dest_name,
                    "mode": (details or {}).get("mode"),
                    "trigger": (details or {}).get("trigger"),
                },
            )


def _snapshot_post_restore_sanity(data_dir: Optional[Path]) -> List[str]:
    warnings: List[str] = []
    normalized = _normalize_path(data_dir)
    if not normalized:
        warnings.append("Restore sanity check missing data directory reference.")
        return warnings
    if not normalized.exists():
        warnings.append(f"Restore target {normalized} does not exist after extraction.")
        return warnings
    seen: Set[str] = set()

    def _check_dir(label: str, path: Path) -> None:
        key = f"dir::{path}"
        if key in seen:
            return
        seen.add(key)
        if not path.exists():
            warnings.append(f"{label} missing after restore: {path}")
            return
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                warnings.append(f"{label} directory empty after restore: {path}")
        elif path.is_file():
            try:
                if path.stat().st_size <= 0:
                    warnings.append(f"{label} file empty after restore: {path}")
            except Exception as exc:
                warnings.append(f"Unable to stat {label} at {path}: {exc}")

    def _check_file(label: str, path: Path) -> None:
        key = f"file::{path}"
        if key in seen:
            return
        seen.add(key)
        if not path.exists():
            warnings.append(f"{label} missing after restore: {path}")
            return
        try:
            size = path.stat().st_size
        except Exception as exc:
            warnings.append(f"Unable to stat {label} at {path}: {exc}")
            return
        if size <= 0:
            warnings.append(f"{label} is zero bytes after restore: {path}")

    candidate_roots = [normalized]
    testnet_root = normalized / "testnet"
    if testnet_root.exists():
        candidate_roots.append(testnet_root)
    for base in candidate_roots:
        _check_dir("BdagChain", base / "BdagChain")
        eth_dir = base / "bdageth" / "chaindata"
        _check_dir("bdageth chaindata", eth_dir)
        _check_dir("bdageth freezer", eth_dir / "ancient")
        _check_file("bdageth freezer headers", eth_dir / "ancient" / "headers")
    for base in candidate_roots:
        current_file = base / "BdagChain" / "CURRENT"
        if current_file.parent.exists():
            _check_file("BdagChain CURRENT", current_file)
    return warnings


def _snapshot_identity_source(base: Optional[Path]) -> Optional[Tuple[Path, Path]]:
    normalized = _normalize_path(base)
    if not normalized or not normalized.exists():
        return None
    for relative in _SNAPSHOT_IDENTITY_RELATIVE_PATHS:
        candidate = normalized / relative
        if candidate.exists():
            try:
                rel = candidate.relative_to(normalized)
            except Exception:
                rel = relative
            return candidate, rel
    return None


def _snapshot_identity_destination(base: Optional[Path], preferred: Optional[Path]) -> Optional[Path]:
    normalized = _normalize_path(base)
    if not normalized:
        return None
    for relative in _SNAPSHOT_IDENTITY_RELATIVE_PATHS:
        candidate = normalized / relative
        if candidate.exists():
            return candidate
    if preferred:
        return normalized / preferred
    testnet_dir = normalized / "testnet"
    if testnet_dir.exists():
        return testnet_dir / "network.key"
    return normalized / "network.key"


def _reapply_peer_identity_from_backup(
    backup_dir: Optional[Path], data_dir: Optional[Path]
) -> Optional[str]:
    source_info = _snapshot_identity_source(backup_dir)
    if not source_info:
        return None
    source_path, relative = source_info
    destination = _snapshot_identity_destination(data_dir, relative)
    if not destination:
        return None
    copied = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        copied = True
    except PermissionError as exc:
        if _copy_identity_via_docker(source_path, destination):
            copied = True
        else:
            app.logger.warning(
                "Failed to preserve peer identity from %s to %s: %s",
                source_path,
                destination,
                exc,
            )
            return None
    except Exception as exc:
        app.logger.warning(
            "Failed to preserve peer identity from %s to %s: %s",
            source_path,
            destination,
            exc,
        )
        return None
    if not copied:
        return None
    app.logger.info("Restored local peer identity from %s to %s", source_path, destination)
    return f"Preserved peer identity by restoring {destination}."


def _ensure_peer_identity(
    data_dir: Optional[Path], *, preferred: Optional[Path] = None
) -> Optional[str]:
    destination = _snapshot_identity_destination(data_dir, preferred)
    if not destination:
        return None
    if destination.exists():
        return None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        identity = secrets.token_hex(32)
        destination.write_text(identity)
        try:
            os.chmod(destination, 0o600)
        except Exception:
            pass
    except Exception as exc:
        app.logger.warning("Failed to create new peer identity at %s: %s", destination, exc)
        return None
    app.logger.info("Generated new peer identity at %s", destination)
    return f"Generated new peer identity at {destination}."


def _copy_identity_via_docker(source: Path, destination: Path) -> bool:
    if not DOCKER_BIN:
        return False
    source_parent = source.parent
    dest_parent = destination.parent
    if not source_parent.exists() or not dest_parent.exists():
        return False
    try:
        uid = os.getuid()
        gid = os.getgid()
    except Exception:
        uid = -1
        gid = -1
    if uid < 0 or gid < 0:
        return False
    src_rel = shlex.quote(f"/src/{source.name}")
    dst_rel = shlex.quote(f"/dst/{destination.name}")
    script = (
        "set -e\n"
        f"install -D -m 600 {src_rel} {dst_rel}\n"
        f"chown {uid}:{gid} {dst_rel}\n"
    )
    try:
        result = subprocess.run(
            [
                DOCKER_BIN,
                "run",
                "--rm",
                "--init",
                "-u",
                "0",
                "-v",
                f"{source_parent}:/src:ro",
                "-v",
                f"{dest_parent}:/dst",
                "busybox",
                "sh",
                "-c",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _start_snapshot_job(
    node_id: Optional[str], *, mode: Optional[str] = None, trigger: Optional[str] = None, quiesce_overlay: Optional[bool] = None
) -> Tuple[bool, str, Dict[str, object]]:
    details: Dict[str, object] = {}
    target_ctx: Optional["NodeContext"] = None
    if node_id:
        try:
            target_ctx = _resolve_node(node_id)
        except Exception:
            target_ctx = None
    if target_ctx:
        details["node"] = target_ctx.id
        if target_ctx.container:
            details["container"] = target_ctx.container
        if target_ctx.chain_data_dir:
            details["data_dir"] = str(target_ctx.chain_data_dir)
        if target_ctx.label:
            details["label"] = target_ctx.label
        health_ok, guard_message, health_info = _snapshot_health_check(target_ctx, mode=mode or "snapshot")
        if not health_ok:
            job_snapshot = _snapshot_job_snapshot()
            merged_details = dict(job_snapshot.get("details") or {})
            merged_details.update(details)
            merged_details["health"] = health_info
            job_snapshot["details"] = merged_details
            job_snapshot["status"] = "rejected"
            job_snapshot["message"] = guard_message
            if isinstance(health_info, dict):
                job_snapshot["failure"] = health_info.get("failure")
                if "wait_seconds" in health_info:
                    job_snapshot["wait_seconds"] = health_info.get("wait_seconds")
            return False, guard_message, job_snapshot
        if health_info:
            details["health"] = health_info
        metrics_source = {}
        health_metrics = health_info.get("metrics") if isinstance(health_info, dict) else None
        if isinstance(health_metrics, dict):
            metrics_source = health_metrics
        elif target_ctx.last_metrics:
            metrics_source = target_ctx.last_metrics
        if not isinstance(metrics_source, dict) or not metrics_source:
            try:
                metrics_source = target_ctx.sample(force=True)
            except Exception:
                metrics_source = target_ctx.last_metrics or {}
        height = (metrics_source or {}).get("local_height") if isinstance(metrics_source, dict) else None
        if isinstance(height, int) and height >= 0:
            details["height"] = height
    details["mode"] = mode or "snapshot"
    if quiesce_overlay is None:
        quiesce_overlay = True
    details["quiesce_overlay"] = bool(quiesce_overlay)
    if trigger:
        details["trigger"] = trigger
    job_already_active = False
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_STATE.get("active"):
            job_already_active = True
        else:
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
    if job_already_active:
        return False, "Snapshot already in progress", _snapshot_job_snapshot()
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
    job_warnings: List[str] = []
    if isinstance(details, dict):
        preflight = details.get("preflight_warnings")
        if isinstance(preflight, (list, tuple)):
            job_warnings.extend(str(item) for item in preflight if item)
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
        archive_bytes = 0
        try:
            archive_bytes = max(snapshot_path.stat().st_size, 0)
        except Exception:
            archive_bytes = 0
        expected_total = archive_bytes
        started = time.time()
        _snapshot_progress_update(
            {
                "bytes_written": 0,
                "total_bytes": expected_total,
                "pct": 0.0 if expected_total else None,
                "eta_seconds": None,
                "updated": started,
                "path": str(snapshot_path),
                "started": started,
            }
        )
        parent_dir = data_dir.parent
        if not parent_dir.exists():
            try:
                parent_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                # Parent directory will be created within docker helper if needed.
                pass
        timestamp = datetime.utcnow().strftime("%Y%m%d.%H%M%S")
        backup_name = f"{data_dir.name}.pre-restore.{timestamp}"
        can_write_parent = os.access(parent_dir, os.W_OK | os.X_OK)
        can_write_existing = not data_dir.exists() or os.access(data_dir, os.W_OK | os.X_OK)
        fallback_to_docker = False
        host_extract_error: Optional[Exception] = None
        if can_write_parent and can_write_existing and not SNAPSHOT_LIGHT_MODE:
            try:
                if data_dir.exists():
                    backup_dir = parent_dir / backup_name
                    shutil.move(str(data_dir), str(backup_dir))
                data_dir.mkdir(parents=True, exist_ok=True)
                _extract_snapshot_contents(snapshot_path, data_dir, total_bytes=expected_total, started=started)
                # Flatten nested restores that wrapped contents in an extra directory (e.g., data/data/testnet).
                nested_candidate = data_dir / "data"
                nested_testnet = nested_candidate / "testnet"
                if nested_candidate.is_dir() and nested_testnet.exists() and not (data_dir / "testnet").exists():
                    for child in nested_candidate.iterdir():
                        target_path = data_dir / child.name
                        if target_path.exists():
                            if target_path.is_dir():
                                shutil.rmtree(target_path, ignore_errors=True)
                            else:
                                target_path.unlink(missing_ok=True)
                        shutil.move(str(child), str(target_path))
                    shutil.rmtree(nested_candidate, ignore_errors=True)
            except PermissionError as exc:
                host_extract_error = exc
                fallback_to_docker = True
                job_warnings.append(
                    f"Host restore lacked permission ({exc}); retrying via Docker to finish extraction."
                )
                try:
                    shutil.rmtree(data_dir, ignore_errors=True)
                except Exception:
                    pass
                if backup_dir and backup_dir.exists():
                    try:
                        target_restore = parent_dir / data_dir.name
                        if target_restore.exists():
                            shutil.rmtree(target_restore, ignore_errors=True)
                        shutil.move(str(backup_dir), str(target_restore))
                    except Exception as move_exc:
                        job_warnings.append(f"Failed to restore original data directory after host error: {move_exc}")
                    finally:
                        backup_dir = None
        if not can_write_parent or not can_write_existing or fallback_to_docker or SNAPSHOT_LIGHT_MODE:
            _restore_via_docker(
                snapshot_path,
                data_dir,
                backup_name,
                expected_total=expected_total,
                archive_bytes=archive_bytes,
                started=started,
            )
            backup_dir = parent_dir / backup_name if (parent_dir / backup_name).exists() else backup_dir
        if backup_dir and backup_dir.exists():
            details["backup"] = str(backup_dir)
        identity_note = _reapply_peer_identity_from_backup(backup_dir, data_dir)
        if not identity_note:
            identity_note = _ensure_peer_identity(data_dir)
        if identity_note:
            details["identity_preserved"] = True
            details["identity_note"] = identity_note
        message = f"Snapshot {snapshot_path.name} restored"
        label = details.get("label") or details.get("node")
        if label:
            message = f"Snapshot {snapshot_path.name} restored to {label}"
        sanity_warnings = _snapshot_post_restore_sanity(data_dir)
        if sanity_warnings:
            details["post_restore_warnings"] = sanity_warnings
            job_warnings.extend(sanity_warnings)
        status = "completed"
        _prune_pre_restore_backups(data_dir)
    except Exception as exc:
        status = "error"
        message = f"Snapshot restore failed: {exc}"
        details["restart"] = False
        if backup_dir and backup_dir.exists():
            try:
                if data_dir.exists():
                    shutil.rmtree(data_dir, ignore_errors=True)
                shutil.move(str(backup_dir), str(data_dir))
            except Exception:
                pass
    finally:
        restart_attempted = False
        restart_error: Optional[str] = None
        if status != "error" and container:
            restart_attempted = True
            if DOCKER_BIN:
                try:
                    _start_container(container)
                    details.setdefault("restart", True)
                except Exception as exc:
                    restart_error = str(exc)
                    with _SNAPSHOT_JOB_LOCK:
                        _SNAPSHOT_JOB_STATE.setdefault("warnings", []).append(str(exc))
            else:
                try:
                    result = docker_action(container, "start")
                    if result.get("ok"):
                        details.setdefault("restart", True)
                    else:
                        restart_error = result.get("error") or result.get("output") or "docker start failed"
                        details.setdefault("restart", False)
                except Exception as exc:
                    restart_error = str(exc)
                    details.setdefault("restart", False)
        elif container and "restart" not in details:
            details.setdefault("restart", False)

        if (
            status != "error"
            and restart_attempted
            and not details.get("restart")
        ):
            warning_msg = (
                f"Snapshot restore completed but failed to restart container {container}: {restart_error or 'unknown error'}"
            )
            job_warnings.append(warning_msg)
            try:
                app.logger.warning(warning_msg)
            except Exception:
                pass
            _automation_event(
                "chain_restore",
                warning_msg,
                node=details.get("node"),
                container=container,
                status="failed",
                metadata={"reason": "restart_failed", "error": restart_error},
            )
        _snapshot_progress_update(None)
        with _SNAPSHOT_JOB_LOCK:
            _SNAPSHOT_JOB_STATE.update(
                {
                    "active": False,
                    "status": status,
                    "message": message,
                    "details": {**(details or {}), "path": details.get("snapshot")},
                    "ended": time.time(),
                    "warnings": job_warnings,
                }
            )
        if status == "completed":
            label = details.get("label") or details.get("node")
            completion_message = (
                f"Chain data recovery completed for {label}" if label else (message or "Chain data recovery completed")
            )
            _automation_event(
                "chain_restore",
                completion_message,
                node=details.get("node"),
                container=details.get("container"),
                status="success",
                metadata={
                    "snapshot": details.get("snapshot"),
                    "trigger": details.get("trigger"),
                    "restart": details.get("restart"),
                },
            )
    _dispatch_pending_restore()


def _start_restore_job(node_id: Optional[str], *, trigger: Optional[str] = None) -> Tuple[bool, str, Dict[str, object]]:
    details: Dict[str, object] = {}
    target_ctx: Optional["NodeContext"]
    preflight_warnings: List[str] = []
    health_info: Dict[str, object] = {}
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
        health_ok, guard_message, health_payload = _snapshot_health_check(target_ctx, mode="restore")
        if isinstance(health_payload, dict):
            health_info = health_payload
            details["health"] = health_payload
        metrics_source = {}
        health_metrics = health_payload.get("metrics") if isinstance(health_payload, dict) else None
        if isinstance(health_metrics, dict):
            metrics_source = health_metrics
        elif target_ctx.last_metrics:
            metrics_source = target_ctx.last_metrics
        if not isinstance(metrics_source, dict) or not metrics_source:
            try:
                metrics_source = target_ctx.sample(force=True)
            except Exception:
                metrics_source = target_ctx.last_metrics or {}
        height = (metrics_source or {}).get("local_height") if isinstance(metrics_source, dict) else None
        if isinstance(height, int) and height >= 0:
            details["height"] = height
        if not health_ok and guard_message:
            preflight_warnings.append(guard_message)
    elif SNAPSHOT_HEALTH_ENABLED:
        details["health"] = {"enabled": SNAPSHOT_HEALTH_ENABLED, "skipped": "no-context"}
    snapshot_path = _select_snapshot_for_restore()
    if not snapshot_path or not snapshot_path.exists():
        return False, "No snapshots available to restore.", _snapshot_job_snapshot()
    details["snapshot"] = snapshot_path.name
    if trigger:
        details["trigger"] = trigger
    details["mode"] = "restore"
    if preflight_warnings:
        details["preflight_warnings"] = preflight_warnings
    container = details.get("container")
    if container:
        try:
            _ensure_clean_container(container)
        except Exception as exc:
            message = f"Failed to prepare container {container} for restore: {exc}"
            return False, message, _snapshot_job_snapshot()
        suspend_window = max(LIVENESS_SNAPSHOT_GRACE_SEC, 60.0)
        _suspend_liveness(container, suspend_window, resume_on_healthy=True)
    job_already_active = False
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_STATE.get("active"):
            job_already_active = True
        else:
            _SNAPSHOT_JOB_STATE.pop("progress", None)
            _SNAPSHOT_JOB_STATE.update(
                {
                    "active": True,
                    "status": "running",
                    "message": "Snapshot restore running…",
                    "details": details,
                    "started": time.time(),
                    "ended": None,
                    "warnings": list(preflight_warnings),
                }
            )
    if job_already_active:
        _queue_pending_restore(details.get("node") or node_id, trigger)
        return False, "Snapshot already in progress", _snapshot_job_snapshot()
    thread = threading.Thread(target=_run_restore_job, args=(details,), daemon=True)
    thread.start()
    label = details.get("label") or details.get("node")
    message = f"Snapshot restore started for {label}" if label else "Snapshot restore started"
    return True, message, _snapshot_job_snapshot()


def _list_docker_containers() -> List[str]:
    if not DOCKER_BIN:
        _docker_health_record(False, "docker binary not available")
        return []
    try:
        out = subprocess.check_output(
            [DOCKER_BIN, "ps", "-a", "--format", "{{.Names}}"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
        containers = [line.strip() for line in out.splitlines() if line.strip()]
        _docker_health_record(True)
        return containers
    except subprocess.CalledProcessError as exc:
        message = _normalize_docker_error((exc.output or exc.stderr or str(exc)).strip())
        _docker_health_record(False, message)
        return []
    except Exception as exc:
        _docker_health_record(False, _normalize_docker_error(str(exc)))
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
        if _REMOTE_RPC_OVERRIDE_DEFINED:
            remote_bases = DEFAULT_REMOTE_BASES[:]
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
        peer_internal, peer_external = _extract_peer_ports_from_inspect(data)
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
                "peer_port_internal": peer_internal,
                "peer_port_external": peer_external,
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
    "peer_port_internal": _coerce_port(os.getenv("BDAG_PEER_PORT_INTERNAL")),
    "peer_port_external": _coerce_port(os.getenv("BDAG_PEER_PORT_EXTERNAL")),
}

def _balance_rpc_targets() -> List[str]:
    explicit_raw = os.getenv("BDAG_BALANCE_RPC_BASES", os.getenv("BDAG_BALANCE_RPC_BASE"))
    explicit = _parse_remote_rpc_bases(explicit_raw) if explicit_raw else []
    targets: List[str] = []

    def add_candidate(value: Optional[str]) -> None:
        if not value:
            return
        normalized = _normalize_rpc_endpoint(value)
        if normalized and normalized not in targets:
            targets.append(normalized)

    for item in explicit:
        add_candidate(item)
    add_candidate("https://rpc.awakening.bdagscan.com")
    add_candidate("https://relay.awakening.bdagscan.com")
    add_candidate(os.getenv("BDAG_RPC_BASE"))
    add_candidate(PRIMARY_REMOTE_RPC_BASE)
    add_candidate(DEFAULT_RPC_FALLBACK)
    return targets or [DEFAULT_RPC_FALLBACK]


BALANCE_RPC_TARGETS = _balance_rpc_targets()
BALANCE_RPC_BASE = BALANCE_RPC_TARGETS[0]
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
    for path in _wallet_env_candidate_paths():
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
        address = _wallet_from_env_file(path)
        if not address:
            continue
        _wallet_address_cache.update({"path": str(path), "mtime": stat.st_mtime, "address": address})
        return address, str(path)
    env_address = str(os.getenv("BDAG_WALLET_ADDRESS", "")).strip()
    if env_address:
        cached_address = _wallet_address_cache.get("address")
        cached_path = _wallet_address_cache.get("path")
        if cached_path != "env" or cached_address != env_address:
            _wallet_address_cache.update({"path": "env", "mtime": 0.0, "address": env_address})
        return env_address, "env"
    _wallet_address_cache.update({"path": None, "mtime": 0.0, "address": None})
    return None, None


def _format_balance_decimal(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_UP)
    return f"{normalized:,.2f}"


def _request_wallet_balance(endpoint: str, address: str) -> dict:
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
        endpoint,
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
        "rpc": endpoint,
    }


def _fetch_wallet_balance(address: str) -> dict:
    _collect_cpu_temperature()
    errors: List[str] = []
    for endpoint in BALANCE_RPC_TARGETS:
        try:
            return _request_wallet_balance(endpoint, address)
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
            continue
    if not errors:
        raise RuntimeError("RPC endpoint not configured")
    raise RuntimeError("; ".join(errors))


def _refresh_wallet_overview(address: Optional[str] = None, source: Optional[str] = None) -> dict:
    if not address:
        address, source = _get_wallet_address()
    if not address:
        info = {"error": "wallet not found"}
    else:
        try:
            info = _fetch_wallet_balance(address)
        except Exception as exc:
            info = {"address": address, "error": str(exc)}
        info["source"] = source
        info["timestamp"] = time.time()
        info["short"] = info.get("balance_formatted", "—")
        balance_entry: Optional[Dict[str, object]] = None
        if "balance_bdag" in info:
            try:
                balance_decimal = Decimal(str(info["balance_bdag"]))
                balance_entry = {
                    "timestamp": info["timestamp"],
                    "balance": float(balance_decimal),
                    "formatted": info.get("balance_formatted"),
                }
            except Exception:
                balance_entry = None
        if balance_entry:
            _WALLET_BALANCE_HISTORY.append(balance_entry)
            _persist_wallet_history()
        info["balance_history"] = list(_WALLET_BALANCE_HISTORY)
    info.setdefault("balance_history", list(_WALLET_BALANCE_HISTORY))
    if "timestamp" not in info:
        info["timestamp"] = time.time()
    _wallet_balance_cache["data"] = dict(info)
    _wallet_balance_cache["ts"] = info["timestamp"]
    return dict(info)


def _get_wallet_overview(*, block: bool = True) -> dict:
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
        cached_copy = dict(cached)
        cached_copy["balance_history"] = list(_WALLET_BALANCE_HISTORY)
        return cached_copy
    if not block:
        _schedule_wallet_refresh()
        if cached and isinstance(cached, dict):
            cached_copy = dict(cached)
            cached_copy["balance_history"] = list(_WALLET_BALANCE_HISTORY)
            cached_copy["stale"] = True
            return cached_copy
        return {"address": address, "source": source, "pending": True}
    return _refresh_wallet_overview(address, source)


def _wallet_refresh_worker() -> None:
    global _wallet_refresh_pending
    while True:
        _WALLET_REFRESH_EVENT.wait()
        _WALLET_REFRESH_EVENT.clear()
        try:
            _refresh_wallet_overview()
        except Exception as exc:
            try:
                app.logger.debug("Wallet refresh failed: %s", exc)
            except Exception:
                pass
        finally:
            with _WALLET_REFRESH_LOCK:
                _wallet_refresh_pending = False

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
        self.peer_port_internal = _coerce_port(merged.get("peer_port_internal"))
        self.peer_port_external = _coerce_port(merged.get("peer_port_external"))

        chain_data_dir = merged.get("chain_data_dir") or merged.get("chaindata_dir")
        self.chain_data_dir: Optional[Path]
        if chain_data_dir:
            self.chain_data_dir = _normalize_path(chain_data_dir)
        else:
            self.chain_data_dir = None

        if self.chain_data_dir:
            _ensure_directory_rw(self.chain_data_dir, create=False)

        self.lock = threading.RLock()
        self.height_series: deque = deque(maxlen=WINDOW)
        self.remote_series: deque = deque(maxlen=WINDOW)
        self.peers_series: deque = deque(maxlen=WINDOW)
        self.rpc_latency_series: deque = deque(maxlen=WINDOW)
        self.block_rate_series: deque = deque(maxlen=WINDOW)
        self.sync_progress_series: deque = deque(maxlen=WINDOW)
        self.last_metrics: Optional[dict] = None
        self.last_sample_ts: float = 0.0
        self.running: bool = False
        self.container_image: Optional[str] = None
        self.auto_discovered = auto_discovered
        self._peer_identity: Optional[str] = None
        self._peer_identity_ts: float = 0.0
        self._ever_reached_height: bool = False

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
        internal_port = meta.get("peer_port_internal")
        if internal_port is not None:
            coerced = _coerce_port(internal_port)
            if coerced != self.peer_port_internal:
                self.peer_port_internal = coerced
                changed = True
        if "peer_port_external" in meta:
            coerced_ext = _coerce_port(meta.get("peer_port_external"))
            if coerced_ext != self.peer_port_external:
                self.peer_port_external = coerced_ext
                changed = True
        chain_dir = meta.get("chain_data_dir") or meta.get("chaindata_dir")
        if chain_dir:
            normalized = _normalize_path(chain_dir)
            if normalized and normalized != self.chain_data_dir:
                self.chain_data_dir = normalized
                _ensure_directory_rw(self.chain_data_dir, create=False)
                self._peer_identity = None
                self._peer_identity_ts = 0.0
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
            "peer_port_internal": self.peer_port_internal,
            "peer_port_external": self.peer_port_external,
        }

    def _empty_metrics(self) -> dict:
        return {
            "local_height": 0,
            "remote_height": 0,
            "height_delta": 0,
            "peers": 0,
            "rpc_latency_ms": None,
            "block_rate_per_sec": None,
            "sync_progress": None,
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
            previous = dict(self.last_metrics) if self.last_metrics is not None else None
        metrics, remote_series_value = _collect_node_metrics(self)
        with self.lock:
            self.last_sample_ts = now
            self.running = metrics["running"]
            if previous:
                prev_ts = int(previous.get("last_updated") or 0)
                curr_ts = int(metrics.get("last_updated") or prev_ts)
                dt_ms = curr_ts - prev_ts
                if dt_ms > 0:
                    delta_blocks = int(metrics.get("local_height") or 0) - int(previous.get("local_height") or 0)
                    if delta_blocks >= 0:
                        metrics["block_rate_per_sec"] = delta_blocks / (dt_ms / 1000.0)
                elif "block_rate_per_sec" not in metrics:
                    metrics["block_rate_per_sec"] = None
            metrics.setdefault("block_rate_per_sec", None)
            local_height = int(metrics.get("local_height") or 0)
            remote_height = metrics.get("remote_height")
            if isinstance(remote_height, (int, float)) and remote_height > 0:
                progress = max(0.0, min(100.0, (local_height / float(remote_height)) * 100.0))
                metrics["sync_progress"] = progress
            else:
                metrics["sync_progress"] = None
            local_height = int(metrics.get("local_height") or 0)
            if local_height > 0:
                self._ever_reached_height = True
            ts = metrics["last_updated"]
            self.height_series.append((ts, local_height))
            self.remote_series.append((ts, remote_series_value))
            self.peers_series.append((ts, metrics["peers"]))
            self.rpc_latency_series.append((ts, metrics.get("rpc_latency_ms")))
            self.block_rate_series.append((ts, metrics.get("block_rate_per_sec")))
            self.sync_progress_series.append((ts, metrics.get("sync_progress")))
            stalled_reason = _detect_stalled_reason(self, metrics, previous)
            metrics["stalled"] = bool(stalled_reason)
            if stalled_reason:
                metrics["health_text"] = stalled_reason
                metrics["health_detail"] = stalled_reason
                metrics["stalled_reason"] = stalled_reason
            else:
                metrics.pop("health_text", None)
                metrics.pop("health_detail", None)
                metrics.pop("stalled_reason", None)
            self.last_metrics = dict(metrics)
            metrics["peer_id"] = self.peer_identity()
            self.last_metrics["peer_id"] = metrics["peer_id"]
            peer_ports_payload: Dict[str, int] = {}
            if self.peer_port_internal:
                peer_ports_payload["internal"] = int(self.peer_port_internal)
            if self.peer_port_external is not None:
                peer_ports_payload["external"] = int(self.peer_port_external)
            if peer_ports_payload:
                metrics["peer_ports"] = dict(peer_ports_payload)
                self.last_metrics["peer_ports"] = dict(peer_ports_payload)
            return dict(self.last_metrics)

    def snapshot(self, *, include_series: bool = False) -> dict:
        with self.lock:
            metrics = dict(self.last_metrics or self._empty_metrics())
            metrics.setdefault("running", self.running)
            metrics.setdefault("peer_id", self.peer_identity())
            if "peer_ports" not in metrics:
                peer_ports_payload: Dict[str, int] = {}
                if self.peer_port_internal:
                    peer_ports_payload["internal"] = int(self.peer_port_internal)
                if self.peer_port_external is not None:
                    peer_ports_payload["external"] = int(self.peer_port_external)
                if peer_ports_payload:
                    metrics["peer_ports"] = peer_ports_payload
            if include_series:
                labels = [ts for ts, _ in self.height_series]
                local = [int(val) if val is not None else 0 for _, val in self.height_series]
                remote_lookup = {ts: val for ts, val in self.remote_series}
                remote = [
                    remote_lookup.get(ts) if remote_lookup.get(ts) is not None else None for ts in labels
                ]
                peers_lookup = {ts: val for ts, val in self.peers_series}
                latency_lookup = {ts: val for ts, val in self.rpc_latency_series}
                block_lookup = {ts: val for ts, val in self.block_rate_series}
                progress_lookup = {ts: val for ts, val in self.sync_progress_series}
                peers_series = [peers_lookup.get(ts) if peers_lookup.get(ts) is not None else None for ts in labels]
                latency_series = [
                    latency_lookup.get(ts) if latency_lookup.get(ts) is not None else None for ts in labels
                ]
                block_series = [
                    block_lookup.get(ts) if block_lookup.get(ts) is not None else None for ts in labels
                ]
                progress_series = [
                    progress_lookup.get(ts) if progress_lookup.get(ts) is not None else None for ts in labels
                ]
                metrics["labels"] = labels
                metrics["local"] = local
                metrics["remote"] = remote
                metrics["peers_series"] = peers_series
                metrics["rpc_latency_series"] = latency_series
                metrics["block_rate_series"] = block_series
                metrics["sync_progress_series"] = progress_series
        return metrics

    def peer_identity(self) -> Optional[str]:
        now = time.time()
        if self._peer_identity is not None and (now - self._peer_identity_ts) < 60.0:
            return self._peer_identity
        identity = _read_peer_identity(self.chain_data_dir, self.container)
        self._peer_identity = identity
        self._peer_identity_ts = now
        return identity


def _read_peer_identity(chain_data_dir: Optional[Path], container: Optional[str] = None) -> Optional[str]:
    if not chain_data_dir:
        return None
    candidates: List[Path] = []
    try:
        candidates.append(Path(chain_data_dir) / "network.key")
        candidates.append(Path(chain_data_dir) / "testnet" / "network.key")
    except Exception:
        return None
    seen: Set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        raw: Optional[bytes]
        try:
            raw = candidate.read_bytes().strip()
        except Exception:
            raw = None
        if raw is None and container:
            raw = _read_peer_identity_from_container(container)
        if raw is None:
            continue
        if not raw:
            continue
        text: Optional[str] = None
        try:
            decoded = raw.decode("ascii").strip()
            if decoded and all(ch in string.hexdigits for ch in decoded):
                text = decoded.lower()
            elif decoded:
                text = decoded
        except Exception:
            text = None
        if not text:
            text = raw.hex()
        text = text.strip().lower()
        if not text:
            continue
        if len(text) > 256:
            text = text[:256]
        return text
    return None


def _read_peer_identity_from_container(container: str) -> Optional[bytes]:
    if not container or not DOCKER_BIN:
        return None
    container_paths = (
        "/bdag/data/testnet/network.key",
        "/bdag/data/network.key",
        "/opt/bdag/testnet/network.key",
    )
    for path in container_paths:
        try:
            output = subprocess.check_output(
                [DOCKER_BIN, "exec", container, "cat", path],
                stderr=subprocess.STDOUT,
                timeout=3,
            )
            data = output.strip()
            if data:
                return data
        except subprocess.CalledProcessError:
            continue
        except FileNotFoundError:
            break
        except Exception:
            continue
    return None


def _purge_policy_state(container: Optional[str]) -> None:
    if not container:
        return
    with _LOG_POLICY_LOCK:
        _LOG_POLICY_STATE.pop(container, None)
        keys = [key for key in _RECENT_LOGS_CACHE.keys() if key[0] == container]
        for key in keys:
            _RECENT_LOGS_CACHE.pop(key, None)


def _suspend_liveness(container: Optional[str], seconds: float, *, resume_on_healthy: bool = False) -> None:
    if not container or seconds <= 0:
        return
    now = time.time()
    with _LOG_POLICY_LOCK:
        state = _LOG_POLICY_STATE.setdefault(
            container,
            {
                "last_check": 0.0,
                "error_streak": 0,
                "last_restart": 0.0,
                "last_liveness": 0.0,
                "liveness_restarts": 0,
            },
        )
        record = state.get("liveness_suspend") or {}
        until = record.get("until")
        if seconds == float("inf"):
            effective_until = float("inf")
        else:
            effective_until = now + seconds
        if isinstance(until, (int, float)) and until > effective_until:
            effective_until = until
        state["liveness_suspend"] = {
            "until": effective_until,
            "resume_on_healthy": bool(record.get("resume_on_healthy")) or resume_on_healthy,
        }
    try:
        app.logger.info(
            "Suspended liveness auto-recover for %s for %s",
            container,
            "health readiness" if resume_on_healthy else f"{seconds:.0f}s",
        )
    except Exception:
        pass


def _liveness_resume_ready(metrics: Optional[dict]) -> bool:
    if not isinstance(metrics, dict):
        return False
    if metrics.get("stalled"):
        return False
    if not metrics.get("running"):
        return False
    delta = metrics.get("height_delta")
    try:
        delta = float(delta)
    except Exception:
        delta = None
    if delta is not None and delta > LIVENESS_RESUME_MAX_DELTA:
        return False
    sync_pct = metrics.get("sync_percent") or metrics.get("sync_progress")
    try:
        sync_pct = float(sync_pct)
    except Exception:
        sync_pct = None
    if sync_pct is not None and sync_pct < 95.0:
        return False
    return True


def _refresh_container_logs(container: str, limit: int) -> None:
    if not container or not DOCKER_BIN:
        return
    try:
        limit_int = max(1, min(int(limit), 200))
    except Exception:
        limit_int = LOG_ERROR_TAIL
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
    now = time.time()
    with _LOG_POLICY_LOCK:
        _RECENT_LOGS_CACHE[(container, limit_int)] = {"ts": now, "lines": lines}


def _get_recent_logs(limit: int, container: str) -> List[str]:
    if not container or not DOCKER_BIN:
        return []
    try:
        limit_int = max(1, min(int(limit), 200))
    except Exception:
        limit_int = LOG_ERROR_TAIL
    key = (container, limit_int)

    def _cached() -> Tuple[Optional[List[str]], float]:
        with _LOG_POLICY_LOCK:
            cached = _RECENT_LOGS_CACHE.get(key)
            if not cached:
                return None, 0.0
            lines = cached.get("lines")
            if not isinstance(lines, list):
                return None, 0.0
            return list(lines), float(cached.get("ts", 0.0))

    cached_lines, cached_ts = _cached()
    now = time.time()
    if cached_lines and now - cached_ts < LOG_CACHE_SEC:
        return cached_lines

    _LOG_REFRESH_EVENT.set()
    deadline = time.time() + LOG_REFRESH_WAIT_SEC
    while time.time() < deadline:
        time.sleep(0.1)
        refreshed_lines, refreshed_ts = _cached()
        if refreshed_lines and time.time() - refreshed_ts < LOG_CACHE_SEC:
            return refreshed_lines

    _refresh_container_logs(container, limit_int)
    fallback_lines, _ = _cached()
    if fallback_lines:
        return fallback_lines
    return []


def _detect_liveness_failsafe_from_logs(container: Optional[str]) -> Optional[Tuple[str, bool]]:
    """Return the most recent failsafe line and whether recovery (vs restart) is required."""
    if not container or not DOCKER_BIN or not LIVENESS_FAILSAFE_PATTERNS:
        return None
    lines = _get_recent_logs(LOG_ERROR_TAIL, container)
    if not lines:
        return None
    for line in reversed(lines):
        normalized = line.strip().lower()
        if not normalized:
            continue
        for pattern in LIVENESS_FAILSAFE_RESTART_PATTERNS:
            if pattern and pattern in normalized:
                return line.strip(), False
        for pattern in LIVENESS_FAILSAFE_RECOVERY_PATTERNS:
            if pattern and pattern in normalized:
                return line.strip(), True
    return None


def _logs_show_importing(container: Optional[str]) -> bool:
    """Detect importing/downloading activity from recent logs."""
    if not container or not DOCKER_BIN:
        return False
    lines = _get_recent_logs(LOG_ERROR_TAIL, container)
    if not lines:
        return False
    importing_markers = (
        "importing blocks",
        "downloading blocks",
        "client in initial download",
        "imported new chain segment",
        "update bdagpool snapshot",
    )
    for line in reversed(lines):
        normalized = line.strip().lower()
        if not normalized:
            continue
        for marker in importing_markers:
            if marker in normalized:
                return True
    return False


def _log_refresh_worker() -> None:
    while True:
        triggered = _LOG_REFRESH_EVENT.wait(timeout=LOG_REFRESH_INTERVAL_SEC)
        _LOG_REFRESH_EVENT.clear()
        now = time.time()
        targets: List[Tuple[str, int]] = []
        with _LOG_POLICY_LOCK:
            for (container, limit), cached in _RECENT_LOGS_CACHE.items():
                if not container:
                    continue
                ts = float(cached.get("ts", 0.0)) if cached else 0.0
                if triggered or now - ts >= LOG_CACHE_SEC:
                    targets.append((container, limit))
        if not targets:
            continue
        for container, limit in targets:
            _refresh_container_logs(container, limit)


def _restart_container_for_policy(
    ctx: "NodeContext",
    reason: str,
    *,
    source: str = "policy",
    metadata_extra: Optional[Dict[str, object]] = None,
) -> bool:
    if not ctx or not ctx.container:
        return False
    result = docker_action(ctx.container, "restart")
    metadata_extra = metadata_extra or {}
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
        restart_note = ""
        if "restart_count" in metadata_extra:
            restart_note = f" ({metadata_extra['restart_count']})"
        meta_payload: Dict[str, object] = {"reason": reason, "source": source}
        meta_payload.update(metadata_extra)
        _automation_event(
            "auto_restart",
            f"Auto restart triggered via {source}{restart_note}",
            node=ctx.id,
            container=ctx.container,
            status="success",
            metadata=meta_payload,
        )
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
    meta_payload = {"reason": reason, "source": source, "error": error_message}
    meta_payload.update(metadata_extra)
    _automation_event(
        "auto_restart",
        f"Auto restart failed via {source}",
        node=getattr(ctx, "id", None),
        container=getattr(ctx, "container", None),
        status="failed",
        metadata=meta_payload,
    )
    return False


def _trigger_restore_for_context(ctx: "NodeContext", reason: str) -> bool:
    if not ctx or not ctx.id:
        return False
    try:
        ok, message, job = _start_restore_job(ctx.id, trigger="liveness")
    except Exception as exc:
        try:
            app.logger.warning("Failed to trigger restore for %s: %s", ctx.id, exc)
        except Exception:
            pass
        _automation_event(
            "chain_restore",
            f"Chain data recovery failed to start for {ctx.id}",
            node=ctx.id,
            container=ctx.container or None,
            status="failed",
            metadata={"reason": reason, "error": str(exc)},
        )
        return False
    if ok:
        try:
            app.logger.warning(
                "Liveness auto-recover triggered restore for node %s (%s): %s",
                ctx.id,
                ctx.container or "unknown",
                reason,
            )
        except Exception:
            pass
        details = job.get("details") if isinstance(job, dict) else {}
        _automation_event(
            "chain_restore",
            f"Chain data recovery started for {ctx.id}",
            node=ctx.id,
            container=ctx.container or (details.get("container") if isinstance(details, dict) else None),
            status="started",
            metadata={"reason": reason},
        )
        return True
    try:
        app.logger.warning(
            "Liveness auto-recover failed to start restore for node %s (%s): %s",
            ctx.id,
            ctx.container or "unknown",
            message,
        )
    except Exception:
        pass
    message = message or ""
    friendly_error = message
    already_running = "already in progress" in message.lower()
    if already_running:
        friendly_error = "Another restore is already in progress"
        user_message = f"Chain data recovery deferred for {ctx.id}"
        status = "skipped"
    else:
        user_message = f"Chain data recovery failed for {ctx.id}"
        status = "failed"
    _automation_event(
        "chain_restore",
        user_message,
        node=ctx.id,
        container=ctx.container or None,
        status=status,
        metadata={"reason": reason, "error": friendly_error},
    )
    return False


def _derive_health_restart_reason(metrics: Dict[str, object]) -> Optional[str]:
    stalled_flag = bool(metrics.get("stalled"))
    if stalled_flag:
        detail = (
            metrics.get("stalled_reason")
            or metrics.get("health_detail")
            or metrics.get("health_text")
        )
        return detail or "node health status stalled"
    running = bool(metrics.get("running"))
    if not running:
        detail = metrics.get("health_detail") or metrics.get("health_text")
        return detail or "node health status offline"
    return None


def _is_restore_job_active_for_container(container: Optional[str]) -> bool:
    if not container:
        return False
    with _SNAPSHOT_JOB_LOCK:
        if not _SNAPSHOT_JOB_STATE.get("active"):
            return False
        job_details = _SNAPSHOT_JOB_STATE.get("details") or {}
        if str(job_details.get("mode") or "").lower() != "restore":
            return False
        job_container = job_details.get("container")
        return bool(job_container and job_container == container)


def _is_snapshot_job_active_for_container(container: Optional[str]) -> bool:
    if not container:
        return False
    with _SNAPSHOT_JOB_LOCK:
        if not _SNAPSHOT_JOB_STATE.get("active"):
            return False
        job_details = _SNAPSHOT_JOB_STATE.get("details") or {}
        job_container = job_details.get("container")
        if not job_container or str(job_container) != container:
            return False
        mode = str(job_details.get("mode") or "snapshot").lower()
        return mode != "restore"


def _apply_node_policies(ctx: "NodeContext", settings: Dict[str, bool]) -> None:
    if not ctx or not ctx.container or not DOCKER_BIN:
        return
    enable_error_restart = bool(settings.get("auto_restart_on_error"))
    enable_liveness = bool(settings.get("liveness_auto_recover"))
    if not (enable_error_restart or enable_liveness):
        return
    now = time.time()
    with _LOG_POLICY_LOCK:
        state = _LOG_POLICY_STATE.setdefault(
            ctx.container,
            {
                "last_check": 0.0,
                "error_streak": 0,
                "last_restart": 0.0,
                "last_liveness": 0.0,
                "liveness_restarts": 0,
            },
        )
        last_check = float(state.get("last_check", 0.0))
        if now - last_check < LOG_ERROR_CHECK_SEC:
            return
        state["last_check"] = now
    metrics_raw = getattr(ctx, "last_metrics", None)
    metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
    failsafe = _detect_liveness_failsafe_from_logs(ctx.container)
    if failsafe:
        failsafe_reason, failsafe_recovery = failsafe
        metrics["stalled"] = True
        metrics["stalled_reason"] = failsafe_reason
        metrics["health_text"] = failsafe_reason
        metrics["health_detail"] = failsafe_reason
        metrics["recovery_required"] = bool(failsafe_recovery)
    if _logs_show_importing(ctx.container):
        metrics.pop("stalled_reason", None)
        metrics.pop("health_detail", None)
        metrics["stalled"] = False
        metrics["recovery_required"] = False
        metrics["health_text"] = "Importing blocks…"
        return
    stalled_reason_text = (
        metrics.get("stalled_reason")
        or metrics.get("health_detail")
        or metrics.get("health_text")
        or ""
    )
    if _is_importing_reason(stalled_reason_text):
        return
    liveness_suspended = False
    suspension: Optional[Dict[str, object]] = None
    if enable_liveness:
        with _LOG_POLICY_LOCK:
            suspension = state.get("liveness_suspend")
        if suspension:
            resume_on_healthy = bool(suspension.get("resume_on_healthy"))
            until_raw = suspension.get("until")
            until = float(until_raw) if isinstance(until_raw, (int, float)) else float("inf")
            if resume_on_healthy:
                if _liveness_resume_ready(metrics):
                    with _LOG_POLICY_LOCK:
                        state.pop("liveness_suspend", None)
                        state["liveness_restarts"] = 0
                else:
                    liveness_suspended = True
            else:
                if until != float("inf") and now >= until:
                    with _LOG_POLICY_LOCK:
                        state.pop("liveness_suspend", None)
                else:
                    liveness_suspended = True

    if enable_liveness and not liveness_suspended:
        if _is_restore_job_active_for_container(ctx.container) or _is_snapshot_job_active_for_container(ctx.container):
            return
        stalled_flag = bool(metrics.get("stalled"))
        stall_reason = (
            metrics.get("stalled_reason")
            or metrics.get("health_detail")
            or metrics.get("health_text")
            or "stalled detection triggered"
        )
        running_flag = bool(metrics.get("running") or metrics.get("container_running"))
        uptime_seconds = float(metrics.get("uptime_seconds") or 0.0)
        if not running_flag:
            offline_reason = (
                metrics.get("health_detail")
                or metrics.get("health_text")
                or "node reported offline"
            )
            stall_reason = offline_reason
            stalled_flag = True
        if not stalled_flag:
            if running_flag and uptime_seconds >= LIVENESS_STABLE_SEC:
                with _LOG_POLICY_LOCK:
                    state["liveness_restarts"] = 0
        if stalled_flag:
            stalled_reason = stall_reason
            with _LOG_POLICY_LOCK:
                last_liveness = float(state.get("last_liveness", 0.0))
                liveness_restarts = int(state.get("liveness_restarts", 0))
                cooldown_elapsed = now - last_liveness >= LIVENESS_RECOVER_COOLDOWN_SEC
            if cooldown_elapsed:
                with _LOG_POLICY_LOCK:
                    state["last_liveness"] = now
                if liveness_restarts < LIVENESS_MAX_RESTARTS:
                    next_attempt = liveness_restarts + 1
                    if _restart_container_for_policy(
                        ctx,
                        stalled_reason,
                        source="liveness",
                        metadata_extra={"restart_count": next_attempt},
                    ):
                        with _LOG_POLICY_LOCK:
                            state["last_restart"] = now
                            state["error_streak"] = 0
                            state["liveness_restarts"] = next_attempt
                        return
                    with _LOG_POLICY_LOCK:
                        state["liveness_restarts"] = next_attempt
                    return
                metrics_check: Dict[str, object] = {}
                try:
                    metrics_check = ctx.sample(force=True) or {}
                except Exception:
                    metrics_check = metrics or {}
                healthy_now = bool(metrics_check.get("running")) and not bool(
                    metrics_check.get("stalled") or metrics_check.get("health_text")
                )
                if healthy_now:
                    try:
                        app.logger.info(
                            "Liveness aborting restore for %s (%s); node reporting healthy.",
                            ctx.id,
                            ctx.container or "unknown",
                        )
                    except Exception:
                        pass
                    with _LOG_POLICY_LOCK:
                        state["liveness_restarts"] = 0
                        state["last_liveness"] = now
                    return
                allow_restore = _should_trigger_corruption_restore(stalled_reason)
                if allow_restore:
                    if _trigger_restore_for_context(ctx, stalled_reason):
                        with _LOG_POLICY_LOCK:
                            state["last_restart"] = now
                            state["error_streak"] = 0
                            state["liveness_restarts"] = 0
                        return
                else:
                    try:
                        app.logger.info(
                            "Liveness skipping chain restore for %s; stall reason not corruption: %s",
                            ctx.id,
                            stalled_reason,
                        )
                    except Exception:
                        pass
                    with _LOG_POLICY_LOCK:
                        state["liveness_restarts"] = 0
                        state["last_liveness"] = now
                    return
                with _LOG_POLICY_LOCK:
                    state["liveness_restarts"] = 0
                    state["last_restart"] = now
                    state["error_streak"] = 0
                return
            else:
                return
    if not enable_error_restart:
        return
    reason = _derive_health_restart_reason(metrics)
    if not reason:
        return
    with _LOG_POLICY_LOCK:
        last_restart = float(state.get("last_restart", 0.0))
    if now - last_restart < AUTO_RESTART_INTERVAL_SEC:
        return
    if _restart_container_for_policy(ctx, reason, source="error_monitor"):
        with _LOG_POLICY_LOCK:
            state["last_restart"] = now
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
STALE_METRICS_SEC = max(15.0, float(SAMPLE_SEC) * 3.0)
_SAMPLE_QUEUE: deque[str] = deque()
_SAMPLE_QUEUE_LOCK = threading.Lock()
_SAMPLE_QUEUE_PENDING: Set[str] = set()
_SAMPLE_WAKE = threading.Event()


def _queue_node_sample(node: Optional["NodeContext"], *, urgent: bool = False) -> None:
    if not node or not getattr(node, "id", None):
        return
    node_id = node.id
    with _SAMPLE_QUEUE_LOCK:
        if node_id in _SAMPLE_QUEUE_PENDING:
            return
        if urgent:
            _SAMPLE_QUEUE.appendleft(node_id)
        else:
            _SAMPLE_QUEUE.append(node_id)
        _SAMPLE_QUEUE_PENDING.add(node_id)
    _SAMPLE_WAKE.set()


def _pop_queued_sample() -> Optional[str]:
    with _SAMPLE_QUEUE_LOCK:
        if not _SAMPLE_QUEUE:
            return None
        node_id = _SAMPLE_QUEUE.popleft()
        if node_id in _SAMPLE_QUEUE_PENDING:
            _SAMPLE_QUEUE_PENDING.discard(node_id)
        return node_id


def _metrics_stale(ctx: "NodeContext", *, now: Optional[float] = None) -> bool:
    if ctx.last_metrics is None:
        return True
    last_ts = float(ctx.last_sample_ts or 0.0)
    if not last_ts:
        return True
    now_val = now if now is not None else time.time()
    return (now_val - last_ts) >= STALE_METRICS_SEC


def _safe_sample_context(ctx: "NodeContext", *, force: bool) -> None:
    try:
        ctx.sample(force=force)
    except Exception as exc:
        try:
            app.logger.debug("Sampling failed for %s (force=%s): %s", ctx.id, force, exc)
        except Exception:
            pass


def _sampling_worker() -> None:
    while True:
        queued_id = _pop_queued_sample()
        if queued_id:
            ctx = NODES.get(queued_id)
            if ctx:
                _safe_sample_context(ctx, force=True)
            continue
        nodes = list(NODES.values())
        if not nodes:
            _SAMPLE_WAKE.wait(timeout=SAMPLE_SEC)
            _SAMPLE_WAKE.clear()
            continue
        for ctx in nodes:
            force = ctx.last_metrics is None
            _safe_sample_context(ctx, force=force)
        _SAMPLE_WAKE.wait(timeout=SAMPLE_SEC)
        _SAMPLE_WAKE.clear()

LOCAL_HEIGHT_METHODS = ["getBlockCount", "getblockcount", "dag_blockNumber", "bdag_blockNumber", "eth_blockNumber"]
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
        except requests.exceptions.RequestException as exc:
            if exc.response is None:
                break
            continue
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
    def _peer_count_from_bdag(payload) -> Optional[int]:
        if payload is None:
            return None

        peer_list: List[object] = []
        count_candidates: List[object] = []
        if isinstance(payload, list):
            peer_list = payload
        elif isinstance(payload, dict):
            for key in (
                "active",
                "activeCount",
                "connected",
                "connections",
                "count",
                "numPeers",
                "total",
                "peersCount",
            ):
                if key in payload:
                    count_candidates.append(payload.get(key))
            peers_field = payload.get("peers")
            if isinstance(peers_field, list):
                peer_list = peers_field
            else:
                peer_list = [payload]

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
        return None

    def _peer_info_count(methods: Iterable[str]) -> Optional[int]:
        for method in methods:
            try:
                info = _rpc_call(
                    ctx.rpc_base,
                    method,
                    [],
                    timeout=ctx.rpc_timeout,
                    auth=auth,
                    verify=ctx.rpc_verify,
                )
            except Exception:
                continue
            count = _peer_count_from_bdag(info)
            if count is not None:
                return count
        return None

    peer_info_count = _peer_info_count(("getPeerInfo", "bdag_getPeerInfo"))
    if peer_info_count is not None:
        return peer_info_count

    base_count = _try_peer_methods(PEER_COUNT_METHODS)
    if isinstance(base_count, int) and base_count > 0:
        return base_count

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


def _remote_height_from_cache(
    base: str, method: str, *, timeout: float, verify: bool
) -> Tuple[Optional[int], Optional[float]]:
    key = (base, method)
    now = time.time()
    with _REMOTE_HEIGHT_CACHE_LOCK:
        entry = _REMOTE_HEIGHT_CACHE.get(key)
        if entry:
            age = now - float(entry.get("ts", 0.0))
            if age <= _REMOTE_HEIGHT_CACHE_TTL_SEC:
                return entry.get("height"), entry.get("latency_ms")
    start = time.perf_counter()
    result = _rpc_call(
        base,
        method,
        [],
        timeout=timeout,
        auth=None,
        verify=verify,
    )
    latency_ms = (time.perf_counter() - start) * 1000.0
    height = _parse_height_value(result)
    if height is None:
        return None, None
    with _REMOTE_HEIGHT_CACHE_LOCK:
        _REMOTE_HEIGHT_CACHE[key] = {"height": height, "ts": time.time(), "latency_ms": latency_ms}
    return height, latency_ms


def _fetch_remote_height(ctx: NodeContext) -> Tuple[Optional[int], Optional[float]]:
    if not ctx.remote_rpc_bases:
        return None, None
    for base in ctx.remote_rpc_bases:
        try:
            height, latency_ms = _remote_height_from_cache(
                base,
                ctx.remote_rpc_method,
                timeout=ctx.remote_rpc_timeout,
                verify=ctx.remote_rpc_verify,
            )
            if height is not None:
                return height, latency_ms
        except Exception:
            continue
    return None, None


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


def _resolve_container_image(name: Optional[str]) -> Optional[str]:
    if not name or not DOCKER_BIN:
        return None
    try:
        out = subprocess.check_output(
            [DOCKER_BIN, "inspect", "--format", "{{.Config.Image}}", name],
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    image = (out or "").strip()
    return image or None


def _collect_node_metrics(ctx: NodeContext) -> Tuple[dict, Optional[int]]:
    if ctx and ctx.container:
        need_internal = not ctx.peer_port_internal
        need_external = ctx.peer_port_external is None
        if need_internal or need_external:
            internal, external = _lookup_peer_ports_from_container(ctx.container)
            if need_internal and internal:
                ctx.peer_port_internal = internal
            if need_external and external is not None:
                ctx.peer_port_external = external
    now_ms = int(time.time() * 1000)
    start_local = time.perf_counter()
    try:
        local_height = _fetch_local_height(ctx)
    except Exception:
        local_height = None
    finally:
        local_latency_ms = (time.perf_counter() - start_local) * 1000.0
    has_remote = bool(ctx.remote_rpc_bases)
    remote_latency_ms: Optional[float] = None
    if has_remote:
        try:
            remote_height, remote_latency_ms = _fetch_remote_height(ctx)
        except Exception:
            remote_height, remote_latency_ms = None, None
    else:
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
        "rpc_latency_ms": local_latency_ms if local_latency_ms >= 0 else None,
        "remote_latency_ms": remote_latency_ms if remote_latency_ms is not None and remote_latency_ms >= 0 else None,
        "block_rate_per_sec": None,
        "sync_progress": None,
        "running": effective_running,
        "container_running": bool(running),
        "container_exists": bool(exists),
        "uptime_seconds": uptime_seconds,
        "last_updated": now_ms,
    }
    image = ctx.container_image
    if exists and ctx.container:
        resolved_image = _resolve_container_image(ctx.container)
        if resolved_image:
            ctx.container_image = resolved_image
            image = resolved_image
    if image:
        metrics["container_image"] = image
    return metrics, remote_val


def _detect_stalled_reason(ctx: "NodeContext", metrics: dict, previous: Optional[dict]) -> Optional[str]:
    try:
        remote_height = int(metrics.get("remote_height") or 0)
    except Exception:
        remote_height = 0
    try:
        local_height = int(metrics.get("local_height") or 0)
    except Exception:
        local_height = 0
    try:
        peers = int(metrics.get("peers") or 0)
    except Exception:
        peers = 0
    try:
        uptime = int(metrics.get("uptime_seconds") or 0)
    except Exception:
        uptime = 0
    try:
        block_rate = float(metrics.get("block_rate_per_sec") or 0.0)
    except Exception:
        block_rate = 0.0
    if remote_height <= 0:
        return None

    ever_progress = ctx._ever_reached_height
    if previous and int(previous.get("local_height") or 0) > 0:
        ever_progress = True

    now_ms = int(metrics.get("last_updated") or 0)
    horizon_ms = 300_000  # five minutes
    recent_heights: List[int] = []
    recent_ts: List[int] = []
    if ctx.height_series:
        for ts, value in ctx.height_series:
            try:
                ts_int = int(ts)
            except Exception:
                continue
            if now_ms and ts_int and now_ms - ts_int > horizon_ms:
                continue
            try:
                height_val = int(value or 0)
            except Exception:
                height_val = 0
            recent_heights.append(height_val)
            recent_ts.append(ts_int)
    if not recent_heights:
        recent_heights.append(local_height)
    recent_progress = max(recent_heights) > min(recent_heights)

    if ever_progress and local_height <= 0 and peers <= 0:
        return "Container restarted but local chain data reset to zero."
    stall_uptime = 180
    if local_height <= 0 and peers <= 0 and uptime >= stall_uptime:
        return "Container has been running with zero height and no peers after a restart."
    if remote_height > local_height + 2 and (recent_progress or block_rate > 0):
        return None
    if not recent_progress and block_rate <= 0 and peers <= 0:
        stagnant_ms = 120_000
        if now_ms and recent_ts:
            oldest_recent = min(recent_ts)
            if now_ms - oldest_recent >= stagnant_ms:
                return "No block progress detected recently while remote chain is advancing."
    return None


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
            _queue_node_sample(ctx, urgent=True)

        for node_id, ctx in list(NODES.items()):
            if ctx.auto_discovered and ctx.container and ctx.container not in seen_containers:
                removed.append(node_id)
                removed_containers.append(ctx.container)
                NODES.pop(node_id, None)

    for container in removed_containers:
        _purge_policy_state(container)

    if added or removed or updated:
        _POLICY_EVENT.set()
    return added, removed, updated


def _is_importing_status(metrics: Optional[dict]) -> bool:
    if not isinstance(metrics, dict):
        return False
    if not metrics.get("running"):
        return False
    try:
        remote_height = int(metrics.get("remote_height") or 0)
    except Exception:
        remote_height = 0
    try:
        local_height = int(metrics.get("local_height") or 0)
    except Exception:
        local_height = 0
    if remote_height <= 0:
        return False
    return remote_height > local_height + 2


def _fleet_summary(nodes: List[dict]) -> dict:
    count = len(nodes)
    running = sum(1 for node in nodes if node.get("status", {}).get("running"))
    offline = max(count - running, 0)
    stalled = sum(1 for node in nodes if node.get("status", {}).get("stalled"))
    local_heights = [
        node.get("status", {}).get("local_height") or 0 for node in nodes
    ]
    remote_heights = [
        node.get("status", {}).get("remote_height") or 0 for node in nodes
    ]
    importing = sum(1 for node in nodes if _is_importing_status(node.get("status", {})))
    summary = {
        "count": count,
        "running": running,
        "offline": offline,
        "stalled": stalled,
        "importing": importing,
        "max_local_height": max(local_heights) if local_heights else 0,
        "max_remote_height": max(remote_heights) if remote_heights else 0,
        "timestamp": time.time(),
    }
    summary["docker"] = _docker_health_snapshot()
    settings = get_settings()
    wallet_enabled = bool(settings.get("display_wallet_balance"))
    summary["wallet_enabled"] = wallet_enabled
    if wallet_enabled:
        try:
            summary["wallet"] = _get_wallet_overview(block=False)
        except Exception as exc:
            summary["wallet"] = {"error": str(exc)}
    else:
        summary["wallet"] = None
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = None
    summary["host"] = {
        "hostname": hostname,
        "ip": _detect_primary_ip(),
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
APP_VERSION = os.getenv("BDAG_MANAGER_VERSION", "v1.5.8").strip() or "v1.5.8"


@app.route("/healthz")
def healthz():
    return "ok\n", 200, {"content-type": "text/plain; charset=utf-8"}


@app.before_request
def _require_login():
    if not LOGIN_ENABLED:
        return None
    if request.endpoint in {"healthz", "login", "logout", "static"}:
        return None
    if request.path.startswith("/.well-known/acme-challenge/"):
        return None
    if request.path.startswith("/api/"):
        return None
    if session.get("authenticated"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not LOGIN_ENABLED:
        return redirect(url_for("node_manager_view"))
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if hmac.compare_digest(username, LOGIN_USER) and hmac.compare_digest(password, LOGIN_PASS):
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("node_manager_view"))
        error = "Invalid username or password."
        try:
            remote_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
            app.logger.warning("Login failure for user=%s ip=%s", username or "<blank>", remote_ip)
        except Exception:
            pass
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
@app.route("/node-manager")
def node_manager_view():
    return render_template(
        "node_manager.html",
        app_version=APP_VERSION,
        app_version_display=APP_VERSION,
        cache_buster=int(time.time()),
    )


@app.route("/api/node-manager/nodes")
def api_node_manager_nodes():
    nodes_payload = []
    now = time.time()
    for ctx in NODES.values():
        stale = _metrics_stale(ctx, now=now)
        if stale:
            _queue_node_sample(ctx, urgent=True)
        nodes_payload.append(
            {
                "id": ctx.id,
                "label": ctx.label,
                "container": ctx.container,
                "auto_discovered": bool(ctx.auto_discovered),
                "status": {
                    **ctx.snapshot(include_series=False),
                    **({"pending_sample": True} if stale else {}),
                },
            }
        )
    if nodes_payload:
        summary = _fleet_summary(nodes_payload)
    else:
        summary = _fleet_summary([])
    return jsonify({"nodes": nodes_payload, "summary": summary})


@app.route("/api/node-manager/launch", methods=["POST"])
def api_node_manager_launch():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        result = launch_node(payload)
    except LaunchError as exc:
        app.logger.warning("Launchpad launch failed: %s", exc)
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        app.logger.exception("Launchpad launch errored")
        return jsonify(error="Launch failed"), 500
    return jsonify(result)


@app.route("/api/node-manager/launch/preview", methods=["POST"])
def api_node_manager_launch_preview():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        result = preview_ports(payload)
    except LaunchError as exc:
        app.logger.warning("Launchpad preview failed: %s", exc)
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        app.logger.exception("Launchpad preview errored")
        return jsonify(error="Preview failed"), 500
    return jsonify(result)


@app.route("/api/node-manager/metrics")
def api_node_manager_metrics():
    nodes_param = request.args.get("nodes", "")
    if nodes_param:
        node_ids = [item.strip() for item in nodes_param.split(",") if item.strip()]
    else:
        node_ids = list(NODES.keys())
    response = {}
    force_flag = (request.args.get("force") or "").strip().lower()
    force_refresh = force_flag in {"1", "true", "yes", "on"}
    now = time.time()
    for node_id in node_ids:
        ctx = NODES.get(node_id)
        if not ctx:
            continue
        stale = _metrics_stale(ctx, now=now)
        if force_refresh or stale:
            _queue_node_sample(ctx, urgent=True)
        payload = ctx.snapshot(include_series=True)
        if force_refresh or stale:
            payload["pending_sample"] = True
        response[ctx.id] = payload
    return jsonify({"nodes": response, "timestamp": time.time()})


def _collect_shared_temperature() -> Optional[Dict[str, object]]:
    if not _CUSTOM_TEMP_PATH or not _CUSTOM_TEMP_PATH.exists():
        return None
    try:
        text = _CUSTOM_TEMP_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not text:
        return None
    parts = text.split()
    try:
        value = float(parts[0])
    except Exception:
        return None
    return {
        "sensor": str(_CUSTOM_TEMP_PATH),
        "label": "shared temperature",
        "current": round(value, 1),
    }


def _collect_psutil_temperature() -> Optional[Dict[str, object]]:
    try:
        sensors = psutil.sensors_temperatures()
    except Exception:
        sensors = {}
    best: Optional[Tuple[float, str, str, Optional[float], Optional[float]]] = None
    for sensor_name, entries in (sensors or {}).items():
        if not entries:
            continue
        for entry in entries:
            current = getattr(entry, "current", None)
            if current is None:
                continue
            label = (entry.label or sensor_name or "CPU").strip()
            high = getattr(entry, "high", None)
            critical = getattr(entry, "critical", None)
            if best is None or current > best[0]:
                best = (float(current), sensor_name or label, label, high, critical)
    if not best:
        return None
    current, sensor_key, label, high, critical = best
    temp_info: Dict[str, object] = {
        "sensor": sensor_key,
        "label": label,
        "current": round(current, 1),
    }
    if isinstance(high, (int, float)):
        temp_info["high"] = round(float(high), 1)
    if isinstance(critical, (int, float)):
        temp_info["critical"] = round(float(critical), 1)
    return temp_info


def _collect_hwmon_temperature() -> Optional[Dict[str, object]]:
    base = Path("/sys/class/hwmon")
    if not base.exists():
        return None
    best: Optional[Tuple[float, str, str]] = None
    for hw in sorted(base.glob("hwmon*")):
        for temp_input in hw.glob("temp*_input"):
            try:
                raw = int(temp_input.read_text().strip())
            except Exception:
                continue
            value = raw / 1000.0
            label_path = temp_input.with_name(temp_input.name.replace("_input", "_label"))
            if not label_path.exists():
                label = temp_input.name
            else:
                label = label_path.read_text().strip() or temp_input.name
            score = 0
            lbl_lower = label.lower()
            if "package" in lbl_lower:
                score += 4
            if "cpu" in lbl_lower:
                score += 3
            if "core" in lbl_lower:
                score += 2
            if "die" in lbl_lower:
                score += 1
            if best is None or score > best[0] or (score == best[0] and value > best[1]):
                best = (score, value, label)
    if not best:
        return None
    _, value, label = best
    return {"sensor": label, "label": label, "current": round(value, 1)}


def _collect_cpu_temperature() -> Optional[Dict[str, object]]:
    local = _collect_psutil_temperature()
    if local:
        return local
    hwmon = _collect_hwmon_temperature()
    if hwmon:
        return hwmon
    return _collect_shared_temperature()


@app.route("/api/system")
def api_system():
    temp = _collect_cpu_temperature()
    try:
        cpu_percent = round(psutil.cpu_percent(interval=0.1), 1)
    except Exception:
        cpu_percent = 0.0
    mem = psutil.virtual_memory()
    path = str(SNAPSHOT_DATA_DIR) if SNAPSHOT_DATA_DIR else "/"
    try:
        disk = psutil.disk_usage(path)
        disk_path = path
    except Exception:
        disk = psutil.disk_usage("/")
        disk_path = "/"
    payload = {
        "cpu_percent": cpu_percent,
        "memory": {
            "total": mem.total,
            "available": mem.available,
            "used": mem.used,
            "percent": round(mem.percent, 1),
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": round(disk.percent, 1),
            "path": disk_path,
        },
    }
    if temp:
        payload["temperature"] = temp
    return jsonify(payload)


@app.route("/api/node-manager/logs")
def api_node_manager_logs():
    node_id = request.args.get("node") or None
    limit_param = request.args.get("limit")
    try:
        limit = max(1, min(int(limit_param), 200)) if limit_param is not None else LOG_ERROR_TAIL
    except Exception:
        limit = LOG_ERROR_TAIL
    ctx = _resolve_node(node_id)
    container = ctx.container or ""
    if not container:
        return jsonify({"node": ctx.id, "container": container, "lines": [], "limit": limit, "timestamp": time.time()})
    lines = _get_recent_logs(limit, container)
    return jsonify(
        {
            "node": ctx.id,
            "container": container,
            "lines": lines,
            "limit": limit,
            "timestamp": time.time(),
        }
    )


@app.route("/api/node-manager/automation/logs")
@app.route("/api/automation/logs")
def api_automation_logs():
    limit_param = request.args.get("limit")
    try:
        limit = max(1, min(int(limit_param), 200)) if limit_param is not None else 50
    except Exception:
        limit = 50
    logs = _automation_log_snapshot(limit)
    return jsonify({"logs": logs, "updated": time.time()})



@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"settings": get_settings()})
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    updated = update_settings(body)
    return jsonify({"ok": True, "settings": updated})


@app.route("/api/overclock/apply", methods=["POST"])
def api_overclock_apply():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    data_path = str(body.get("data_path") or "").strip()
    do_cpu = _coerce_bool(body.get("cpu"), True)
    do_nvme = _coerce_bool(body.get("nvme_latency"), True)
    do_sched = _coerce_bool(body.get("scheduler"), True)
    do_remount = _coerce_bool(body.get("remount"), True)

    # Auto-detect data directory when missing or invalid
    resolved_dir = _auto_detect_data_dir(data_path)
    if not resolved_dir:
        return jsonify({"ok": False, "error": "could not detect data directory; please enter it explicitly"}), 400
    data_path = str(resolved_dir)
    _oc_log(f"Detected data dir: {data_path}")
    # Resolve mountpoint using findmnt so users can pass any path under the filesystem
    mountpoint = None
    try:
        out = subprocess.check_output(["findmnt", "-no", "TARGET", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
        mountpoint = (out or "").strip() or None
    except Exception:
        mountpoint = None
    if not mountpoint:
        mountpoint = data_path if os.path.ismount(data_path) else "/"

    # Prefer bundled script inside install dir; fallback to workspace root
    script_path = (Path(__file__).resolve().parent / "scripts" / "tune_nvme_node.sh")
    if not script_path.exists():
        script_path = (Path(__file__).resolve().parents[1] / "scripts" / "tune_nvme_node.sh")
    if not script_path.exists():
        return jsonify({"ok": False, "error": f"script not found: {script_path}"}), 500

    cmd = [str(script_path), "--mountpoint", mountpoint]
    cmd.extend(["--cpu", "yes" if do_cpu else "no"]) 
    cmd.extend(["--nvme-latency", "yes" if do_nvme else "no"]) 
    cmd.extend(["--scheduler", "yes" if do_sched else "no"]) 
    cmd.extend(["--remount", "yes" if do_remount else "no"]) 

    use_sudo = os.geteuid() != 0
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        _oc_log(f"Apply: mount={mountpoint} cpu={do_cpu} nvme={do_nvme} sched={do_sched} remount={do_remount}")
        # Use a stable working directory to avoid shell-init getcwd errors
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd="/")
        ok = proc.returncode == 0
        # Stream a few lines to Overclock log buffer (filter noisy shell-init getcwd)
        noisy = "shell-init: error retrieving current directory"
        for line in (proc.stdout or "").splitlines()[:12]:
            if not line.startswith(noisy):
                _oc_log(f"apply stdout: {line}")
        for line in (proc.stderr or "").splitlines()[:12]:
            if not line.startswith(noisy):
                _oc_log(f"apply stderr: {line}")
        _oc_log(f"Apply: {'OK' if ok else 'FAILED'} (rc={proc.returncode})")
        payload = {"ok": ok, "stdout": proc.stdout, "stderr": proc.stderr, "resolved_data_path": data_path}
        if use_sudo and proc.returncode != 0:
            # Common case: sudo requires a TTY or password
            payload.update({
                "needs_root": True,
                "hint": f"sudo scripts/tune_nvme_node.sh --mountpoint {mountpoint}",
            })
        status = 200 if ok or payload.get("needs_root") else 500
        return jsonify(payload), status
    except Exception as exc:
        app.logger.error("overclock apply failed: %s", exc, exc_info=True)
        _oc_log(f"Apply: exception {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/revert", methods=["POST"])
def api_overclock_revert():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    data_path = str(body.get("data_path") or "").strip()
    resolved_dir = _auto_detect_data_dir(data_path)
    if not resolved_dir:
        return jsonify({"ok": False, "error": "could not detect data directory; please enter it explicitly"}), 400
    data_path = str(resolved_dir)
    try:
        out = subprocess.check_output(["findmnt", "-no", "TARGET", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
        mountpoint = (out or "").strip() or None
    except Exception:
        mountpoint = None
    if not mountpoint:
        mountpoint = data_path if os.path.ismount(data_path) else "/"
    script_path = (Path(__file__).resolve().parent / "scripts" / "tune_nvme_node.sh")
    if not script_path.exists():
        script_path = (Path(__file__).resolve().parents[1] / "scripts" / "tune_nvme_node.sh")
    if not script_path.exists():
        return jsonify({"ok": False, "error": f"script not found: {script_path}"}), 500
    cmd = [str(script_path), "--mountpoint", mountpoint, "--revert", "yes", "--cpu", "yes", "--nvme-latency", "yes", "--scheduler", "yes", "--remount", "yes"]
    use_sudo = os.geteuid() != 0
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        _oc_log(f"Revert: mount={mountpoint}")
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd="/")
        ok = proc.returncode == 0
        noisy = "shell-init: error retrieving current directory"
        for line in (proc.stdout or "").splitlines()[:12]:
            if not line.startswith(noisy):
                _oc_log(f"revert stdout: {line}")
        for line in (proc.stderr or "").splitlines()[:12]:
            if not line.startswith(noisy):
                _oc_log(f"revert stderr: {line}")
        _oc_log(f"Revert: {'OK' if ok else 'FAILED'} (rc={proc.returncode})")
        payload = {"ok": ok, "stdout": proc.stdout, "stderr": proc.stderr, "resolved_data_path": data_path}
        if use_sudo and proc.returncode != 0:
            payload.update({"needs_root": True, "hint": f"sudo scripts/tune_nvme_node.sh --mountpoint {mountpoint} --revert yes"})
        return jsonify(payload), (200 if ok or payload.get("needs_root") else 500)
    except Exception as exc:
        app.logger.error("overclock revert failed: %s", exc, exc_info=True)
        _oc_log(f"Revert: exception {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/verify", methods=["POST"])
def api_overclock_verify():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    data_path = str(body.get("data_path") or "").strip()
    try:
        runtime_req = int(body.get("runtime")) if body.get("runtime") is not None else 10
    except Exception:
        runtime_req = 10
    runtime = max(5, min(runtime_req, 30))
    # Auto-detect data dir, resolve mountpoint, and create a temporary directory on that filesystem
    resolved_dir = _auto_detect_data_dir(data_path)
    if not resolved_dir:
        return jsonify({"ok": False, "error": "could not detect data directory; please enter it explicitly"}), 400
    data_path = str(resolved_dir)
    try:
        out = subprocess.check_output(["findmnt", "-no", "TARGET", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
        mountpoint = (out or "").strip() or None
    except Exception:
        mountpoint = None
    if not mountpoint:
        mountpoint = data_path if os.path.ismount(data_path) else "/"
    # Place the temp directory inside the requested data_path so tests reflect overlays/dir-specific mounts
    base_dir = Path(data_path) if Path(data_path).exists() else Path(mountpoint)
    tmp_dir = base_dir / f".oc-test-{int(time.time())}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"cannot create temp dir on {mountpoint}: {exc}"}), 500
    fio_bin = shutil.which("fio")
    if not fio_bin:
        # Try to install fio automatically using apt-get or dnf
        installer = None
        use_sudo = os.geteuid() != 0
        if shutil.which("apt-get"):
            installer = ["apt-get", "update"]
            cmd_install = ["apt-get", "install", "-y", "fio"]
        elif shutil.which("dnf"):
            installer = ["dnf", "makecache"]
            cmd_install = ["dnf", "install", "-y", "fio"]
        else:
            installer = None
            cmd_install = None
        try:
            if installer and cmd_install:
                if use_sudo:
                    installer = ["sudo", "-n"] + installer
                    cmd_install = ["sudo", "-n"] + cmd_install
                subprocess.run(installer, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(cmd_install, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        fio_bin = shutil.which("fio")
        if not fio_bin:
            try:
                tmp_dir.rmdir()
            except Exception:
                pass
            hint = "sudo apt-get update && sudo apt-get install -y fio" if shutil.which("apt-get") else "sudo dnf install -y fio"
            payload = {"ok": False, "error": "fio not installed", "hint": hint}
            if use_sudo:
                payload.update({"needs_root": True})
            return jsonify(payload), 400
    cmd = [
        fio_bin,
        "--name=fsync",
        f"--directory={str(tmp_dir)}",
        "--rw=write",
        "--bs=4k",
        "--ioengine=psync",
        "--numjobs=1",
        "--size=64m",
        "--fsync=1",
        "--time_based=1",
        f"--runtime={runtime}",
        "--group_reporting=1",
        "--eta=never",
        "--output-format=json",
    ]
    try:
        _oc_log(f"Test: running fio on mount={mountpoint}")
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd="/")
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        ok = proc.returncode == 0
        # Parse metrics: write IOPS/BW + write and fsync (sync) percentiles when present
        metrics: Dict[str, str] = {}
        # Prefer JSON parsing (robust across fio versions)
        parsed = None
        try:
            parsed = json.loads(stdout)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            try:
                jobs = parsed.get("jobs") or []
                job = jobs[0] if jobs else {}
                write_sec = job.get("write") or {}
                sync_sec = job.get("sync") or {}
                iops_val = write_sec.get("iops")
                bw_kib = write_sec.get("bw")  # KiB/s
                if iops_val is not None:
                    try:
                        metrics["iops"] = f"{float(iops_val):.0f}"
                    except Exception:
                        metrics["iops"] = str(iops_val)
                if bw_kib is not None:
                    metrics["bw"] = f"{bw_kib}KiB/s"
                # Percentiles in ns — capture both write and sync (fsync)
                write_pct = (
                    (write_sec.get("clat_ns") or {}).get("percentile")
                    or (job.get("clat_ns") or {}).get("percentile")
                    or (write_sec.get("lat_ns") or {}).get("percentile")
                ) or {}
                sync_pct = (
                    (sync_sec.get("clat_ns") or {}).get("percentile")
                    or (sync_sec.get("lat_ns") or {}).get("percentile")
                ) or {}
                def _fmt_us(val_ns):
                    try:
                        us = float(val_ns) / 1000.0
                        return f"{us:.0f}us"
                    except Exception:
                        return None
                # Write (submit) lat percentiles
                if isinstance(write_pct, dict):
                    wp50_ns = write_pct.get("50.000000") or write_pct.get("50.00th")
                    wp99_ns = write_pct.get("99.000000") or write_pct.get("99.00th")
                    if wp50_ns is not None:
                        metrics["write_p50"] = _fmt_us(wp50_ns) or ""
                        # Back-compat default p50 to write_p50
                        metrics.setdefault("p50", metrics["write_p50"])  # nosec - informational only
                    if wp99_ns is not None:
                        metrics["write_p99"] = _fmt_us(wp99_ns) or ""
                        metrics.setdefault("p99", metrics["write_p99"])  # nosec
                # Fsync (sync) lat percentiles
                if isinstance(sync_pct, dict):
                    sp50_ns = sync_pct.get("50.000000") or sync_pct.get("50.00th")
                    sp99_ns = sync_pct.get("99.000000") or sync_pct.get("99.00th")
                    if sp50_ns is not None:
                        metrics["fsync_p50"] = _fmt_us(sp50_ns) or ""
                    if sp99_ns is not None:
                        metrics["fsync_p99"] = _fmt_us(sp99_ns) or ""
            except Exception:
                pass
        # Derive IOPS from bandwidth if missing
        if (not metrics.get("iops")) and metrics.get("bw"):
            try:
                # Expect format like '429KiB/s'
                bw_text = str(metrics["bw"]).lower().replace("/s", "")
                bw_val = None
                if "kib" in bw_text:
                    bw_val = float(bw_text.split("kib")[0])
                elif "kb" in bw_text:
                    bw_val = float(bw_text.split("kb")[0])
                elif "mb" in bw_text:
                    bw_val = float(bw_text.split("mb")[0]) * 1024.0
                if bw_val is not None and bw_val >= 0:
                    # bs is 4KiB; iops ≈ KiB/s / 4
                    approx_iops = max(0.0, bw_val / 4.0)
                    metrics["iops"] = f"{approx_iops:.0f}"
            except Exception:
                pass
        # Fallback: text parsing for older fio
        if not metrics.get("iops") or not metrics.get("bw"):
            try:
                for line in stdout.splitlines():
                    t = line.strip()
                    if (t.lower().startswith("write:") or t.lower().startswith("write ")) and ("iops=" in t.lower() or "bw=" in t.lower()):
                        # Example variants: write: IOPS=12.3k, BW=48.2MiB/s ... OR write: bw=..., iops=...
                        # Normalize separators
                        parts = [seg.strip() for seg in t.split(',')]
                        for part in parts:
                            pl = part.lower()
                            if pl.startswith("iops="):
                                metrics["iops"] = part.split("=",1)[1]
                            if pl.startswith("bw=") or part.startswith("BW="):
                                val = part.split("=",1)[1]
                                metrics["bw"] = val
                    if t.startswith("\t50.00th") or t.startswith("50.00th"):
                        metrics["p50"] = t.split('=')[1].strip().strip('[]')
                    if t.startswith("\t99.00th") or t.startswith("99.00th"):
                        metrics["p99"] = t.split('=')[1].strip().strip('[]')
            except Exception:
                pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        if metrics:
            _oc_log(
                "Test: "
                f"IOPS={metrics.get('iops')} BW={metrics.get('bw')} "
                f"write_p50={metrics.get('write_p50')} write_p99={metrics.get('write_p99')} "
                f"fsync_p50={metrics.get('fsync_p50')} fsync_p99={metrics.get('fsync_p99')}"
            )
        else:
            _oc_log(f"Test: rc={proc.returncode}")
        return jsonify({"ok": ok, "stdout": stdout, "stderr": stderr, "metrics": metrics, "resolved_data_path": data_path}), (200 if ok else 500)
    except Exception as exc:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        _oc_log(f"Test: exception {exc}")
    return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/preflight", methods=["POST"])
def api_overclock_preflight():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    data_path = str(body.get("data_path") or "").strip()
    resolved_dir = _auto_detect_data_dir(data_path)
    if not resolved_dir:
        return jsonify({"ok": False, "error": "could not detect data directory; please enter it explicitly"}), 400
    data_path = str(resolved_dir)
    try:
        out = subprocess.check_output(["findmnt", "-no", "SOURCE,TARGET", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
        parts = (out or "").strip().split()
        source = parts[0] if parts else None
        mountpoint = parts[1] if len(parts) > 1 else None
    except Exception:
        source = None
        mountpoint = None
    if not source:
        return jsonify({"ok": False, "error": "could not resolve device for data path"}), 400
    dev_path = str(Path(source))
    base = os.path.basename(os.path.realpath(dev_path))
    ctrl = None
    m = re.match(r"(nvme\d+)n\d+", base)
    if m:
        ctrl = f"/dev/{m.group(1)}"
    plp = "unknown"
    needs_root = False
    if not ctrl:
        return jsonify({"ok": True, "device": dev_path, "mountpoint": mountpoint, "controller": None, "plp": plp, "needs_root": needs_root, "resolved_data_path": data_path})
    # Try smartctl for PLP hint
    smartctl = shutil.which("smartctl")
    if smartctl:
        try:
            out = subprocess.check_output([smartctl, "-a", "-d", "nvme", ctrl], text=True, stderr=subprocess.STDOUT)
            low = out.lower()
            if "power loss protection" in low:
                if "power loss protection: supported" in low or "enabled" in low:
                    plp = "yes"
                elif "not supported" in low or "disabled" in low:
                    plp = "no"
        except subprocess.CalledProcessError as exc:
            needs_root = True
        except Exception:
            pass
    _oc_log(f"Preflight: dev={dev_path} ctrl={ctrl or 'n/a'} plp={plp}")
    return jsonify({"ok": True, "device": dev_path, "mountpoint": mountpoint, "controller": ctrl, "plp": plp, "needs_root": needs_root, "resolved_data_path": data_path})


@app.route("/api/overclock/detect", methods=["GET"])
def api_overclock_detect():
    try:
        resolved = _auto_detect_data_dir(None)
        if not resolved:
            return jsonify({"ok": False, "error": "not found"}), 404
        data_path = str(resolved)
        mountpoint = None
        try:
            out = subprocess.check_output(["findmnt", "-no", "TARGET", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
            mountpoint = (out or "").strip() or None
        except Exception:
            mountpoint = None
        _oc_log(f"Detect: data_dir={data_path} mount={mountpoint or 'n/a'}")
        return jsonify({"ok": True, "resolved_data_path": data_path, "mountpoint": mountpoint})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/verify-dbs", methods=["POST"])
def api_overclock_verify_dbs():
    """Run fio verify inside BdagChain and bdageth/chaindata when present.
    Body: { runtime?: int }
    Returns: { ok: true, results: { BdagChain?: {metrics}, bdageth-chaindata?: {metrics} } }
    """
    body = request.get_json(silent=True) or {}
    try:
        runtime_req = int(body.get("runtime")) if body.get("runtime") is not None else 10
    except Exception:
        runtime_req = 10
    runtime = max(5, min(runtime_req, 30))
    base = _auto_detect_data_dir(None)
    if not base:
        return jsonify({"ok": False, "error": "data directory not found"}), 404
    # Discover DB dirs
    dirs = _detect_db_dirs(base)
    results: Dict[str, object] = {}
    for name, path in dirs.items():
        try:
            if not path.exists() or not path.is_dir():
                continue
            _oc_log(f"Test: running fio in {name} ({path})")
            with app.test_request_context(json={"data_path": str(path), "runtime": runtime}):
                resp = api_overclock_verify()
            payload = None
            try:
                payload, status = resp
                data = payload.get_json() if hasattr(payload, 'get_json') else payload
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("ok"):
                results[name] = data.get("metrics") or {}
                # Log a concise line
                met = results[name]
                if isinstance(met, dict):
                    _oc_log(
                        f"Test:{name}: IOPS={met.get('iops')} BW={met.get('bw')} "
                        f"write_p50={met.get('write_p50')} write_p99={met.get('write_p99')} "
                        f"fsync_p50={met.get('fsync_p50')} fsync_p99={met.get('fsync_p99')}"
                    )
        except Exception as exc:
            _oc_log(f"Test:{name}: exception {exc}")
            continue
    return jsonify({"ok": True, "results": results, "base": str(base)})

@app.route("/api/overclock/wal-tmpfs", methods=["POST"])
def api_overclock_wal_tmpfs():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    enabled = _coerce_bool(body.get("enabled"), False)
    wal_dir = str(body.get("wal_dir") or "/dev/shm/node-wal").strip() or "/dev/shm/node-wal"
    user = os.getenv("SUDO_USER") or os.getenv("USER") or "node"
    group = None
    try:
        import grp, pwd  # noqa: F401
        group = grp.getgrgid(os.getgid()).gr_name  # type: ignore
    except Exception:
        group = None
    # Ensure directory exists and is owned by service user if possible
    try:
        cmd_mkdir = ["mkdir", "-p", wal_dir]
        cmd_chown = ["chown", f"{user}:{group or user}", wal_dir]
        # Try direct first
        try:
            subprocess.run(cmd_mkdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
            subprocess.run(cmd_chown, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
        except Exception:
            # Fallback to sudo -n if needed
            subprocess.run(["sudo", "-n", *cmd_mkdir], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
            subprocess.run(["sudo", "-n", *cmd_chown], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
        return jsonify({"ok": False, "error": "wal tmpfs disabled"}), 410
    except Exception as exc:
        _oc_log(f"WAL tmpfs error: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/wal-checkpoint", methods=["POST"])
def api_overclock_wal_checkpoint():
    return jsonify({"ok": False, "error": "wal checkpoint disabled"}), 410


@app.route("/api/overclock/wal-checkpoint/status", methods=["GET"])
def api_overclock_wal_checkpoint_status():
    return jsonify({"ok": False, "error": "wal checkpoint disabled"}), 410


@app.route("/api/overclock/wal-config", methods=["POST"])
def api_overclock_wal_config():
    """Disabled: WAL config removed."""
    return jsonify({"ok": False, "error": "wal config disabled"}), 410


@app.route("/api/overclock/vm-mode", methods=["POST"])
def api_overclock_vm_mode():
    """One-click path to emulate VM-like sync speeds without WAL features.
    Steps:
      - Apply safe tunings (CPU perf, nvme latency=0, scheduler, remount)
      - Run a short DB-scoped test and return metrics
    """
    body = request.get_json(silent=True) or {}
    runtime = 10
    try:
        if body.get("runtime") is not None:
            runtime = max(5, min(int(body.get("runtime")), 20))
    except Exception:
        runtime = 10
    # 1) Apply safe tunings
    _oc_log("VM-Mode: applying safe tunings…")
    try:
        r = api_overclock_apply()
    except Exception as exc:
        _oc_log(f"VM-Mode: apply error {exc}")
    # WAL steps removed
    # 4) Run short test on data filesystem for reference
    metrics = {}
    try:
        _oc_log("VM-Mode: running test (10s)…")
        with app.test_request_context(json={"runtime": runtime}):
            resp = api_overclock_verify()
            try:
                payload, status = resp
                if isinstance(payload, Response):
                    data = json.loads(payload.get_data(as_text=True))
                else:
                    data = payload.get_json() if hasattr(payload, 'get_json') else payload
            except Exception:
                data = None
            if isinstance(data, dict):
                metrics = data.get("metrics") or {}
    except Exception:
        pass
    return jsonify({"ok": True, "metrics": metrics})


@app.route("/api/overclock/redeploy", methods=["POST"])
def api_overclock_redeploy():
    return jsonify({"ok": False, "error": "redeploy with WAL env disabled"}), 410


@app.route("/api/overclock/vm-mode-revert", methods=["POST"])
def api_overclock_vm_mode_revert():
    """Revert VM-Mode: revert tunings and run a quick test (WAL features removed)."""
    # 1) Revert tunings
    try:
        _oc_log("VM-Mode revert: reverting tunings…")
        _ = api_overclock_revert()
    except Exception as exc:
        _oc_log(f"VM-Mode revert: revert error {exc}")
    # 2) Test after revert
    metrics = {}
    try:
        _oc_log("VM-Mode revert: running test (10s)…")
        with app.test_request_context(json={"runtime": 10}):
            resp = api_overclock_verify()
            try:
                payload, status = resp
                if isinstance(payload, Response):
                    data = json.loads(payload.get_data(as_text=True))
                else:
                    data = payload.get_json() if hasattr(payload, 'get_json') else payload
            except Exception:
                data = None
            if isinstance(data, dict):
                metrics = data.get("metrics") or {}
    except Exception:
        pass
    return jsonify({"ok": True, "metrics": metrics})


@app.route("/api/overclock/redeploy/suggest", methods=["GET"])
def api_overclock_redeploy_suggest():
    return jsonify({"ok": False, "error": "redeploy suggest disabled"}), 410


def _detect_db_dirs(data_dir: Path) -> Dict[str, Path]:
    """Detect key RocksDB directories under the data dir."""
    mapping: Dict[str, Path] = {}
    candidates = [
        ("BdagChain", data_dir / "testnet" / "BdagChain"),
        ("bdageth-chaindata", data_dir / "testnet" / "bdageth" / "chaindata"),
        ("BdagChain", data_dir / "BdagChain"),
        ("bdageth-chaindata", data_dir / "bdageth" / "chaindata"),
    ]
    for name, path in candidates:
        try:
            if path.exists() and path.is_dir():
                mapping[name] = path
        except Exception:
            continue
    return mapping


def _overlay_mapping_path() -> Path:
    return Path("/etc/overlayfs-node-manager.json")


def _overlay_state_path() -> Path:
    return Path("/var/lib/overlay-commit-state.json")


def _overclock_overlay_enabled() -> bool:
    try:
        settings = get_settings()
    except Exception:
        settings = {}
    return bool(settings.get("overclock_overlay_bdagchain")) or bool(settings.get("overclock_overlay_bdageth"))


def _overlay_read_mappings() -> Dict[str, Dict[str, object]]:
    mapping_path = _overlay_mapping_path()
    if not mapping_path.exists():
        return {}
    try:
        return json.loads(mapping_path.read_text())
    except Exception:
        return {}


def _overlay_write_mappings(mappings: Dict[str, Dict[str, object]]) -> None:
    try:
        _overlay_mapping_path().write_text(json.dumps(mappings, indent=2))
    except Exception:
        pass


def _overlay_simple_name(key: str, entry: Dict[str, object]) -> str:
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    if "@" in key:
        return key.split("@", 1)[0]
    return key


def _overlay_matches_targets(key: str, entry: Dict[str, object], targets: Optional[Iterable[str]]) -> bool:
    if targets is None:
        return True
    simple = _overlay_simple_name(key, entry)
    for target in targets:
        if not isinstance(target, str):
            continue
        if target == "all":
            return True
        if key == target or simple == target:
            return True
        if target and key.startswith(f"{target}@"):
            return True
    return False


def _overlay_commit_targets(
    targets: Optional[Iterable[str]] = None,
    *,
    only_mounted: bool = False,
    sync_backup_to_lower: bool = False,
) -> Tuple[int, List[str]]:
    mappings = _overlay_read_mappings()
    if not mappings:
        return 0, []
    state_path = _overlay_state_path()
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    committed = 0
    touched: List[str] = []
    for key, entry in mappings.items():
        if not _overlay_matches_targets(key, entry, targets):
            continue
        lower = entry.get("lower")
        backup = entry.get("backup")
        if not (lower and backup):
            continue
        lower_path = Path(str(lower))
        backup_path = Path(str(backup))
        if only_mounted:
            mounted = False
            try:
                out = subprocess.check_output(
                    ["findmnt", "-no", "FSTYPE", "--target", str(lower_path)],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                mounted = (out or "").strip() == "overlay"
            except Exception:
                mounted = False
            if not mounted:
                continue
        try:
            backup_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _oc_log(f"Overlay: committing {key} -> {backup_path}")
        subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                f"{str(lower_path).rstrip('/')}/",
                f"{str(backup_path).rstrip('/')}/",
            ],
            check=False,
            cwd="/",
        )
        state[key] = int(time.time())
        committed += 1
        touched.append(key)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
    except Exception:
        pass
    return committed, touched


def _overlay_flush_to_disk(targets: Iterable[str], *, remount: bool = True) -> List[str]:
    """Ensure overlay-backed directories are written back to disk.
    For each selected overlay:
        1. rsync the union view (mountpoint) into the overlay backup directory
        2. Unmount the overlay to expose the real filesystem again
        3. rsync the backup contents into the lower directory (now on disk)
        4. Optionally recreate empty upper/work dirs and remount the overlay
    """
    mappings = _overlay_read_mappings()
    if not mappings:
        return []
    targets_set = {str(t) for t in targets}
    flushed: List[str] = []
    for key, entry in mappings.items():
        if not _overlay_matches_targets(key, entry, targets_set):
            continue
        lower = Path(entry.get("lower", ""))
        upper = Path(entry.get("upper", ""))
        work = Path(entry.get("work", ""))
        backup = Path(entry.get("backup", ""))
        if not lower.exists():
            continue
        try:
            backup.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        simple = _overlay_simple_name(key, entry)
        label = entry.get("name") or simple
        # Step 1: copy union view to backup
        try:
            _oc_log(f"Snapshot guard: syncing overlay {label} (union -> backup)")
            subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--delete",
                    f"{str(lower).rstrip('/')}/",
                    f"{str(backup).rstrip('/')}/",
                ],
                check=True,
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"overlay sync union->backup failed for {label}: {(exc.stderr or exc.stdout or str(exc)).strip()}"
            ) from exc
        # Step 2: unmount overlay (if mounted)
        was_mounted = False
        try:
            out = subprocess.check_output(
                ["findmnt", "-no", "FSTYPE", "--target", str(lower)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            was_mounted = (out or "").strip() == "overlay"
        except Exception:
            was_mounted = False
        if was_mounted:
            subprocess.run(
                ["umount", str(lower)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        # Step 3: copy backup back to lower (now on disk)
        try:
            _oc_log(f"Snapshot guard: syncing overlay {label} (backup -> lower)")
            subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--delete",
                    f"{str(backup).rstrip('/')}/",
                    f"{str(lower).rstrip('/')}/",
                ],
                check=True,
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"overlay sync backup->lower failed for {label}: {(exc.stderr or exc.stdout or str(exc)).strip()}"
            ) from exc
        # Step 4: optionally remount overlay with fresh dirs
        if remount and upper and work:
            try:
                subprocess.run(["rm", "-rf", str(upper)], check=False, cwd="/")
                subprocess.run(["rm", "-rf", str(work)], check=False, cwd="/")
            except Exception:
                pass
            try:
                upper.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            try:
                work.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            opts = f"lowerdir={lower},upperdir={upper},workdir={work}"
            subprocess.run(
                ["mount", "-t", "overlay", "overlay", "-o", opts, str(lower)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd="/",
            )
        flushed.append(label)
    return flushed


def _overlay_targets_for_path(root: Path) -> Set[str]:
    mappings = _overlay_read_mappings()
    if not mappings:
        return set()
    try:
        root_resolved = root.resolve()
    except Exception:
        root_resolved = root
    selected: Set[str] = set()
    for key, entry in mappings.items():
        lower = entry.get("lower")
        if not lower:
            continue
        try:
            lower_path = Path(str(lower)).resolve()
        except Exception:
            continue
        try:
            lower_path.relative_to(root_resolved)
        except ValueError:
            continue
        selected.add(key)
        selected.add(_overlay_simple_name(key, entry))
    return selected


@app.route("/api/overclock/overlay/apply", methods=["POST"])
def api_overclock_overlay_apply():
    """Apply OverlayFS with tmpfs upper over DB directories to redirect writes to RAM.
    Periodically commits overlay mount view to disk backup via a systemd timer.
    """
    body = request.get_json(silent=True) or {}
    target = (body.get("target") or "BdagChain").strip()
    try:
        interval_req = body.get("interval_sec")
        interval_sec = max(5, min(int(interval_req), 600)) if interval_req is not None else 15
    except Exception:
        interval_sec = 15
    try:
        # Optional per-overlay RAM limit
        limit_bytes = None
        if body.get("limit_gib") is not None:
            try:
                gib = float(body.get("limit_gib"))
                if gib > 0:
                    limit_bytes = int(gib * (1024**3))
            except Exception:
                limit_bytes = None
        elif body.get("limit_bytes") is not None:
            try:
                limit_bytes = max(0, int(body.get("limit_bytes")))
            except Exception:
                limit_bytes = None

        data_dir = _auto_detect_data_dir(None)
        if not data_dir:
            return jsonify({"ok": False, "error": "data directory not found"}), 404
        db_map = _detect_db_dirs(data_dir)
        if target not in db_map:
            return jsonify({"ok": False, "error": f"db '{target}' not found"}), 404
        lower = db_map[target]
        upper = Path("/dev/shm/overlay") / (target + "-upper")
        work = Path("/dev/shm/overlay") / (target + "-work")
        backup = data_dir / "overlay-backup" / target
        # Create dirs
        for p in (upper, work, backup):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        # Mount overlay if not already
        mounted = False
        try:
            out = subprocess.check_output(["findmnt", "-no", "FSTYPE", "--target", str(lower)], text=True, stderr=subprocess.DEVNULL)
            mounted = (out or "").strip() == "overlay"
        except Exception:
            mounted = False
        if not mounted:
            opts = f"lowerdir={lower},upperdir={upper},workdir={work}"
            cmd = ["mount", "-t", "overlay", "overlay", "-o", opts, str(lower)]
            _oc_log(f"Overlay: mounting {target} with {opts}")
            subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/")
            # recheck
            try:
                out = subprocess.check_output(["findmnt", "-no", "FSTYPE", "--target", str(lower)], text=True, stderr=subprocess.DEVNULL)
                mounted = (out or "").strip() == "overlay"
            except Exception:
                mounted = False
        # Persist mapping
        mapping_path = Path("/etc/overlayfs-node-manager.json")
        mappings: Dict[str, Dict[str, str]] = {}
        try:
            if mapping_path.exists():
                mappings = json.loads(mapping_path.read_text())
        except Exception:
            mappings = {}
        entry = mappings.get(target) or {}
        entry.update({
            "lower": str(lower),
            "upper": str(upper),
            "work": str(work),
            "backup": str(backup),
            "interval_sec": interval_sec,
        })
        if limit_bytes is not None:
            entry["limit_bytes"] = limit_bytes
        if not entry.get("node"):
            entry["node"] = "local"
        mappings[target] = entry
        try:
            mapping_path.write_text(json.dumps(mappings, indent=2))
        except Exception:
            pass
        # Install/enable commit timer (per-entry interval handled in script state)
        try:
            script = """#!/usr/bin/env bash
set -euo pipefail
MAP=/etc/overlayfs-node-manager.json
STATE=/var/lib/overlay-commit-state.json
mkdir -p /var/lib || true
if [ ! -f "$MAP" ]; then exit 0; fi
python3 - "$MAP" <<'PY'
import json,os,sys,subprocess,time
mp=json.load(open(sys.argv[1]))
state_path = '/var/lib/overlay-commit-state.json'
try:
    st = json.load(open(state_path))
except Exception:
    st = {}
for name,entry in mp.items():
    mnt=entry.get('lower'); bkp=entry.get('backup')
    interval=entry.get('interval_sec') or 15
    if not (mnt and bkp):
        continue
    os.makedirs(bkp, exist_ok=True)
    last = st.get(name) or 0
    now = int(time.time())
    if now - int(last) >= int(interval):
        # mirror overlay mount view into backup (handles deletions)
        subprocess.run(['rsync','-a','--delete',f"{mnt.rstrip('/')}/",f"{bkp.rstrip('/')}/"], check=False)
        st[name] = now
json.dump(st, open(state_path,'w'))
PY
"""
            p = Path("/usr/local/bin/overlay-commit.sh")
            p.write_text(script)
            os.chmod(p, 0o755)
            svc = """[Unit]
Description=Commit OverlayFS mounts to disk backup
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/overlay-commit.sh
"""
            tim = f"""[Unit]
Description=Periodic OverlayFS commit

[Timer]
OnBootSec=30s
OnUnitActiveSec={interval_sec}s
Unit=overlay-commit.service

[Install]
WantedBy=timers.target
"""
            Path("/etc/systemd/system/overlay-commit.service").write_text(svc)
            Path("/etc/systemd/system/overlay-commit.timer").write_text(tim)
            subprocess.run(["systemctl","daemon-reload"], check=False)
            subprocess.run(["systemctl","enable","--now","overlay-commit.timer"], check=False)
            subprocess.run(["systemctl","restart","overlay-commit.timer"], check=False)
        except Exception as exc:
            _oc_log(f"Overlay: failed to setup commit timer: {exc}")
        return jsonify({"ok": mounted, "mounted": mounted, "target": target, "lower": str(lower), "upper": str(upper), "backup": str(backup), "interval_sec": interval_sec, "limit_bytes": limit_bytes})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/overlay/revert", methods=["POST"])
def api_overclock_overlay_revert():
    body = request.get_json(silent=True) or {}
    target = (body.get("target") or "BdagChain").strip()
    commit = _coerce_bool(body.get("commit"), True)
    try:
        mapping_path = Path("/etc/overlayfs-node-manager.json")
        if not mapping_path.exists():
            return jsonify({"ok": True, "message": "no overlay mappings"})
        mappings = json.loads(mapping_path.read_text())
        entry = mappings.get(target)
        if not entry:
            return jsonify({"ok": True, "message": "target not mapped"})
        lower = Path(entry.get("lower",""))
        upper = Path(entry.get("upper",""))
        backup = Path(entry.get("backup",""))
        # Commit backup back to lower if requested
        if commit and lower.exists() and backup.exists():
            _oc_log(f"Overlay: committing backup -> lower for {target}")
            subprocess.run(["rsync","-a","--delete",f"{str(backup).rstrip('/')}/",f"{str(lower).rstrip('/')}/"], check=False)
        # Unmount overlay
        try:
            subprocess.run(["umount", str(lower)], check=False)
        except Exception:
            pass
        # Cleanup tmpfs dirs
        try:
            subprocess.run(["rm","-rf", str(upper), str(entry.get("work",""))], check=False)
        except Exception:
            pass
        # Remove mapping
        try:
            mappings.pop(target, None)
            mapping_path.write_text(json.dumps(mappings, indent=2))
        except Exception:
            pass
        return jsonify({"ok": True, "reverted": True, "target": target})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/overlay/status", methods=["GET"])
def api_overclock_overlay_status():
    """Return overlay mappings and metrics: mounted state and upperdir size in bytes."""
    try:
        mapping_path = Path("/etc/overlayfs-node-manager.json")
        mappings = {}
        if mapping_path.exists():
            try:
                mappings = json.loads(mapping_path.read_text())
            except Exception:
                mappings = {}
        items: List[Dict[str, object]] = []
        for name, entry in (mappings or {}).items():
            lower = Path(entry.get("lower",""))
            upper = Path(entry.get("upper",""))
            backup = Path(entry.get("backup",""))
            limit_bytes = entry.get("limit_bytes")
            interval_item = entry.get("interval_sec")
            mounted = False
            fstype = None
            try:
                out = subprocess.check_output(["findmnt","-no","FSTYPE","--target", str(lower)], text=True, stderr=subprocess.DEVNULL)
                fstype = (out or "").strip()
                mounted = (fstype == "overlay")
            except Exception:
                mounted = False
            upper_bytes = 0
            try:
                out = subprocess.check_output(["du","-sb", str(upper)], text=True, stderr=subprocess.DEVNULL)
                parts = out.split()
                if parts:
                    upper_bytes = max(int(parts[0]), 0)
            except Exception:
                upper_bytes = 0
            items.append({
                "name": name,
                "lower": str(lower),
                "upper": str(upper),
                "backup": str(backup),
                "mounted": mounted,
                "fstype": fstype,
                "upper_bytes": upper_bytes,
                "limit_bytes": limit_bytes,
                "interval_sec": interval_item,
                "overlay": entry.get("name") or _overlay_simple_name(name, entry),
                "node": entry.get("node"),
            })
        # Discover current timer interval
        interval_sec = None
        try:
            text = Path("/etc/systemd/system/overlay-commit.timer").read_text()
            for line in text.splitlines():
                if line.startswith("OnUnitActiveSec="):
                    val = line.split("=",1)[1].strip()
                    if val.endswith("s"):
                        val = val[:-1]
                    interval_sec = int(val)
                    break
        except Exception:
            interval_sec = None
        return jsonify({"ok": True, "items": items, "interval_sec": interval_sec})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/overlay/align", methods=["POST"])
def api_overclock_overlay_align():
    """Ensure all blockdag-testnet-network* containers' /bdag/data bind sources have overlays for
    BdagChain and bdageth/chaindata. Accepts optional interval/limits.
    Body: { interval_bdagchain?: int, limit_bdagchain_gib?: float, interval_bdageth?: int, limit_bdageth_gib?: float }
    """
    body = request.get_json(silent=True) or {}
    def _ival(name: str, default: int) -> int:
        try:
            v = int(body.get(name)) if body.get(name) is not None else default
            return max(5, min(v, 600))
        except Exception:
            return default
    def _fval(name: str, default: float) -> float:
        try:
            v = float(body.get(name)) if body.get(name) is not None else default
            return max(0.5, min(v, 64.0))
        except Exception:
            return default
    int_b = _ival("interval_bdagchain", 30)
    lim_b = _fval("limit_bdagchain_gib", 3.0)
    int_e = _ival("interval_bdageth", 30)
    lim_e = _fval("limit_bdageth_gib", 4.0)
    binds: list[str] = []
    try:
        if not DOCKER_BIN:
            return jsonify({"ok": False, "error": "docker not available"}), 400
        out = subprocess.check_output([DOCKER_BIN, "ps", "--format", "{{.Names}}"], text=True)
        names = [n.strip() for n in out.splitlines() if n.strip().startswith("blockdag-testnet-network")]
        for name in names:
            try:
                j = json.loads(subprocess.check_output([DOCKER_BIN, "inspect", name], text=True))[0]
                for b in (j.get("HostConfig", {}).get("Binds") or []):
                    if isinstance(b, str) and b.endswith(":/bdag/data"):
                        host = b.split(":")[0]
                        if host and host not in binds:
                            binds.append(host)
            except Exception:
                continue
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    applied: list[dict] = []
    mapping_path = Path("/etc/overlayfs-node-manager.json")
    try:
        mappings = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    except Exception:
        mappings = {}
    for base in binds:
        for target, ival, lim in (("BdagChain", int_b, lim_b), ("bdageth-chaindata", int_e, lim_e)):
            lower = Path(base) / "testnet" / ("BdagChain" if target == "BdagChain" else Path("bdageth") / "chaindata")
            if not lower.exists() or not lower.is_dir():
                continue
            # unique key per base
            key = f"{target}@{abs(hash(str(lower))) & 0xffffffff:x}"
            upper = Path("/dev/shm/overlay") / f"{key}-upper"
            work = Path("/dev/shm/overlay") / f"{key}-work"
            backup = Path(base) / "overlay-backup" / key
            try:
                upper.mkdir(parents=True, exist_ok=True)
                work.mkdir(parents=True, exist_ok=True)
                backup.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            # mount if not already overlay
            mounted = False
            try:
                fstype = subprocess.check_output(["findmnt","-no","FSTYPE","--target", str(lower)], text=True, stderr=subprocess.DEVNULL).strip()
                mounted = (fstype == "overlay")
            except Exception:
                mounted = False
            if not mounted:
                opts = f"lowerdir={lower},upperdir={upper},workdir={work}"
                _oc_log(f"Overlay align: mounting {target} base={base}")
                subprocess.run(["mount","-t","overlay","overlay","-o",opts,str(lower)], check=False, cwd="/")
            # update mapping
            mappings[key] = {
                "name": target,
                "lower": str(lower),
                "upper": str(upper),
                "work": str(work),
                "backup": str(backup),
                "interval_sec": ival,
                "limit_bytes": int(lim * (1024**3)),
                "node": name,
            }
            applied.append({"base": base, "target": target, "lower": str(lower)})
    # persist mappings and (re)start timer
    try:
        mapping_path.write_text(json.dumps(mappings, indent=2))
    except Exception:
        pass
    try:
        subprocess.run(["systemctl","daemon-reload"], check=False)
        subprocess.run(["systemctl","enable","--now","overlay-commit.timer"], check=False)
        subprocess.run(["systemctl","restart","overlay-commit.timer"], check=False)
    except Exception:
        pass
    return jsonify({"ok": True, "applied": applied, "binds": binds, "count": len(applied)})


@app.route("/api/overclock/overlay/disable", methods=["POST"])
def api_overclock_overlay_disable():
    """Disable overlays across all mapped entries for a target.
    Body: { target: 'BdagChain'|'bdageth-chaindata', commit?: bool }
    """
    body = request.get_json(silent=True) or {}
    target = (body.get("target") or "").strip() or None
    do_commit = _coerce_bool(body.get("commit"), True)
    if target not in {"BdagChain", "bdageth-chaindata"}:
        return jsonify({"ok": False, "error": "invalid or missing target"}), 400
    targets = {target}
    if do_commit:
        try:
            _overlay_commit_targets(targets, only_mounted=False, sync_backup_to_lower=True)
        except Exception as exc:
            _oc_log(f"Overlay disable: commit before disable failed: {exc}")
    mapping_path = _overlay_mapping_path()
    if not mapping_path.exists():
        return jsonify({"ok": True, "reverted": 0})
    try:
        mappings = json.loads(mapping_path.read_text())
    except Exception:
        mappings = {}
    reverted = 0
    kept: dict = {}
    for key, entry in list(mappings.items()):
        name = entry.get("name") or key
        lower = entry.get("lower") or ""
        # Match by stored name or by lower path suffix
        is_target = (target in str(name)) or (target == "BdagChain" and str(lower).endswith("/BdagChain")) or (target == "bdageth-chaindata" and str(lower).endswith("/bdageth/chaindata"))
        if not is_target:
            kept[key] = entry
            continue
        # Commit backup back to lower if requested
        if do_commit:
            try:
                bkp = entry.get("backup")
                if bkp and lower:
                    subprocess.run(["rsync","-a","--delete",f"{str(bkp).rstrip('/')}/",f"{str(lower).rstrip('/')}/"], check=False)
            except Exception:
                pass
        # Unmount overlay
        try:
            subprocess.run(["umount", str(lower)], check=False)
        except Exception:
            pass
        # Cleanup tmpfs dirs
        try:
            up = entry.get("upper"); wk = entry.get("work")
            if up:
                subprocess.run(["rm","-rf", str(up)], check=False)
            if wk:
                subprocess.run(["rm","-rf", str(wk)], check=False)
        except Exception:
            pass
        reverted += 1
    try:
        mapping_path.write_text(json.dumps(kept, indent=2))
    except Exception:
        pass
    return jsonify({"ok": True, "reverted": reverted})


    return jsonify({"ok": False, "error": "unsupported action"}), 400


@app.route("/api/overclock/status", methods=["GET"])
def api_overclock_status():
    try:
        data_path_param = request.args.get("data_path") or ""
        resolved = _auto_detect_data_dir(data_path_param)
        if not resolved:
            return jsonify({"ok": False, "error": "data directory not found"}), 404
        data_path = str(resolved)
        # mountpoint
        try:
            out = subprocess.check_output(["findmnt", "-no", "SOURCE,TARGET,OPTIONS", "--target", data_path], text=True, stderr=subprocess.DEVNULL)
            parts = (out or "").strip().split()
            source = parts[0] if parts else None
            mountpoint = parts[1] if len(parts) > 1 else None
            fs_options = parts[2] if len(parts) > 2 else None
        except Exception:
            source = None
            mountpoint = None
            fs_options = None
        # base device for scheduler and controller path
        base = None
        ctrl = None
        scheduler = None
        if source:
            try:
                base_name = os.path.basename(os.path.realpath(str(source)))
                # nvmeXnYpZ -> nvmeXnY
                m = re.match(r"(nvme\d+n\d+)p\d+", base_name)
                base = m.group(1) if m else re.sub(r"\d+$", "", base_name)
                if base and os.path.isdir(f"/sys/block/{base}"):
                    # read scheduler
                    try:
                        raw = Path(f"/sys/block/{base}/queue/scheduler").read_text().strip()
                        # active scheduler is in brackets: 'mq-deadline [none]'
                        if "[" in raw and "]" in raw:
                            scheduler = raw.split("[")[-1].split("]")[0]
                        else:
                            scheduler = raw
                    except Exception:
                        scheduler = None
                m2 = re.match(r"(nvme\d+)n\d+", base_name)
                if m2:
                    ctrl = f"/dev/{m2.group(1)}"
            except Exception:
                pass
        # cpu governor (first cpu)
        try:
            cpu_gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
        except Exception:
            cpu_gov = None
        # nvme latency
        try:
            nvme_lat_us = Path("/sys/module/nvme_core/parameters/default_ps_max_latency_us").read_text().strip()
        except Exception:
            nvme_lat_us = None
        # quick booleans
        status = {
            "ok": True,
            "data_path": data_path,
            "mountpoint": mountpoint,
            "device": source,
            "base_device": base,
            "cpu_governor": cpu_gov,
            "cpu_is_performance": (cpu_gov == "performance") if cpu_gov else None,
            "nvme_latency_us": nvme_lat_us,
            "nvme_latency_is_0": (nvme_lat_us == "0") if nvme_lat_us is not None else None,
            "scheduler": scheduler,
            "fs_options": fs_options,
        }
        # status line
        bits = []
        if status["cpu_is_performance"] is not None:
            bits.append(f"CPU={'perf' if status['cpu_is_performance'] else status['cpu_governor']}")
        if status["nvme_latency_us"] is not None:
            bits.append(f"NVMe-lat={status['nvme_latency_us']}us")
        if status["scheduler"]:
            bits.append(f"sched={status['scheduler']}")
        if status["fs_options"]:
            opt = status["fs_options"]
            bits.append(f"fs={opt.split(',')[0]}")
        status_line = "Status: " + ", ".join(bits) if bits else "Status: n/a"
        _oc_log(status_line)
        status["status_line"] = status_line
        return jsonify(status)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/overlay/commit", methods=["POST"])
def api_overclock_overlay_commit():
    """Commit overlays immediately by syncing the mounted view to backup.
    Body: { target?: 'BdagChain'|'bdageth-chaindata'|'all' }
    Returns: { ok, committed, target }
    """
    body = request.get_json(silent=True) or {}
    sel = (body.get("target") or "all").strip()
    try:
        targets = None if sel == "all" else {sel}
        committed, touched = _overlay_commit_targets(targets, only_mounted=False)
        if committed == 0 and touched == [] and sel != "all":
            return jsonify({"ok": False, "error": "target not found"}), 404
        return jsonify({"ok": True, "committed": committed, "target": sel, "entries": touched})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/overclock/logs")
def api_overclock_logs():
    limit_param = request.args.get("limit")
    try:
        limit = max(1, min(int(limit_param), 500)) if limit_param is not None else 120
    except Exception:
        limit = 120
    with _OVERCLOCK_LOGS_LOCK:
        lines = list(_OVERCLOCK_LOGS)[-limit:]
    return jsonify({"lines": lines, "limit": limit, "timestamp": time.time()})





@app.route("/api/snapshots", methods=["GET"])
def api_snapshots():
    snapshots = list_snapshots()
    job = _snapshot_job_snapshot()
    with _AUTO_SNAPSHOT_LOCK:
        interval_sec = float(_AUTO_SNAPSHOT_STATE.get("interval") or 0.0)
        next_run = float(_AUTO_SNAPSHOT_STATE.get("next_run") or 0.0)
        last_run = float(_AUTO_SNAPSHOT_STATE.get("last_run") or 0.0)
        auto_snapshot_state = {
            "enabled": bool(_AUTO_SNAPSHOT_STATE.get("enabled")),
            "interval_seconds": interval_sec if interval_sec > 0.0 else None,
            "next_run": next_run if next_run > 0.0 else None,
            "last_run": last_run if last_run > 0.0 else None,
            "last_result": _AUTO_SNAPSHOT_STATE.get("last_result"),
        }
    response: Dict[str, object] = {
        "snapshots": snapshots,
        "job": job,
        "directory": str(SNAPSHOT_DIR),
        "automation": {"auto_snapshot": auto_snapshot_state},
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
    quiesce_overlay = _coerce_bool(body.get("quiesce_overlay"), True)
    ok, message, job = _start_snapshot_job(
        str(node_id) if node_id else None,
        quiesce_overlay=quiesce_overlay,
    )
    if not ok:
        failure = None
        wait_seconds = None
        if isinstance(job, dict):
            failure = job.get("failure")
            wait_seconds = job.get("wait_seconds")
            if failure is None or wait_seconds is None:
                details = job.get("details")
                health_details = details.get("health") if isinstance(details, dict) else {}
                if failure is None and isinstance(health_details, dict):
                    failure = health_details.get("failure")
                if wait_seconds is None and isinstance(health_details, dict):
                    wait_seconds = health_details.get("wait_seconds")
        return jsonify({"ok": False, "error": message, "job": job, "failure": failure, "wait_seconds": wait_seconds}), 409
    return jsonify({"ok": True, "message": message, "job": job})


@app.route("/api/snapshots/scan", methods=["POST"])
def api_snapshots_scan():
    directory = _ensure_snapshot_dir()
    message = (
        f"Snapshot directory set to {directory}. Update the path under Settings to choose a different location."
    )
    return jsonify(
        {
            "ok": True,
            "message": message,
            "directory": str(SNAPSHOT_DIR),
            "snapshots": list_snapshots(),
        }
    )


@app.route("/api/snapshots/restore", methods=["POST"])
def api_snapshots_restore():
    body = request.get_json(silent=True) or {}
    node_id = body.get("node")
    ok, message, job = _start_restore_job(str(node_id) if node_id else None, trigger="api")
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


def _restart_all_nodes_sequentially(cooldown_sec: float) -> None:
    nodes = sorted(NODES.values(), key=lambda ctx: (ctx.id or "").lower())
    if not nodes:
        return
    app.logger.info("memory-triggered restart: restarting %d node(s)", len(nodes))
    for ctx in nodes:
        container = (ctx.container or "").strip()
        if not container:
            continue
        try:
            result = docker_action(container, "restart")
            if result.get("ok"):
                app.logger.info("restarted %s via memory trigger", container)
                _automation_event(
                    "auto_restart",
                    "Memory pressure restart issued",
                    node=ctx.id,
                    container=container,
                    status="success",
                    metadata={"source": "memory", "reason": "memory usage threshold exceeded"},
                )
            else:
                app.logger.warning("memory trigger restart failed for %s: %s", container, result.get("error"))
                _automation_event(
                    "auto_restart",
                    "Memory pressure restart failed",
                    node=ctx.id,
                    container=container,
                    status="failed",
                    metadata={"source": "memory", "error": result.get("error")},
                )
        except Exception as exc:
            app.logger.exception("memory trigger restart error for %s: %s", container, exc)
            _automation_event(
                "auto_restart",
                "Memory pressure restart error",
                node=ctx.id,
                container=container,
                status="failed",
                metadata={"source": "memory", "error": str(exc)},
            )
        time.sleep(cooldown_sec)


def _memory_restart_monitor() -> None:
    global _MEMORY_RESTART_ACTIVE
    while True:
        enabled = False
        threshold = 0.0
        try:
            settings = get_settings()
            enabled = bool(settings.get("auto_restart_mem_enabled"))
            threshold = float(settings.get("auto_restart_mem_threshold") or 0.0)
        except Exception:
            pass
        if enabled and threshold > 0:
            try:
                mem_percent = psutil.virtual_memory().percent
            except Exception as exc:
                app.logger.debug("failed to read virtual memory: %s", exc)
                mem_percent = 0.0
            if mem_percent >= threshold:
                if _MEMORY_RESTART_LOCK.acquire(blocking=False):
                    try:
                        _MEMORY_RESTART_ACTIVE = True
                        _restart_all_nodes_sequentially(MEMORY_RESTART_COOLDOWN_SEC)
                    finally:
                        _MEMORY_RESTART_ACTIVE = False
                        _MEMORY_RESTART_LOCK.release()
                    time.sleep(max(MEMORY_RESTART_COOLDOWN_SEC, MEMORY_RESTART_INTERVAL_SEC))
                    continue
        time.sleep(MEMORY_RESTART_INTERVAL_SEC)


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

try:
    get_settings()
except Exception:
    pass

try:
    _ensure_sensor_packages()
except Exception:
    pass

try:
    _ensure_docker_group_membership()
except Exception:
    pass

for _ctx in NODES.values():
    _queue_node_sample(_ctx, urgent=True)
_schedule_wallet_refresh(force=True)

_LOG_REFRESH_THREAD = threading.Thread(target=_log_refresh_worker, daemon=True)
_LOG_REFRESH_THREAD.start()
_LOG_REFRESH_EVENT.set()

_AUTO_SNAPSHOT_THREAD = threading.Thread(target=_auto_snapshot_worker, daemon=True)
_AUTO_SNAPSHOT_THREAD.start()
_AUTO_SNAPSHOT_EVENT.set()

_POLICY_THREAD = threading.Thread(target=_policy_worker, daemon=True)
_POLICY_THREAD.start()
_POLICY_EVENT.set()

_MEMORY_RESTART_THREAD = threading.Thread(target=_memory_restart_monitor, daemon=True)
_MEMORY_RESTART_THREAD.start()

_SAMPLER_THREAD = threading.Thread(target=_sampling_worker, daemon=True)
_SAMPLER_THREAD.start()

_WALLET_THREAD = threading.Thread(target=_wallet_refresh_worker, daemon=True)
_WALLET_THREAD.start()
