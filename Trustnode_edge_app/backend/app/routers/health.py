import time as _time
import threading as _threading

from fastapi import APIRouter, Request, HTTPException

from app.state import app_store

router = APIRouter(prefix="/api", tags=["health"])


def _collect_routing() -> dict:
    try:
        return app_store.get_historian_read_routing_status() or {}
    except Exception:
        return {}


def _collect_license_summary() -> dict:
    try:
        from app.services import license_inspect
        return license_inspect.get_license_summary() or {}
    except Exception:
        return {}


# Operator 2026-08-21 (BOOT-HEALTH FIX): /api/health must NEVER block.
#
# Root cause of the "Backend service did not respond within 30 seconds"
# splash on large installs: on a cold cache this handler called
# app_store.get_historian_read_routing_status() (takes app_store._lock)
# and license_inspect.get_license_summary() (-> get_bootstrap -> _lock, plus
# a direct SQLite open) INLINE on the request path, serialized behind a
# module lock. During boot that lock is contended (activation restore /
# mirror, config sync, the multi-GB quick_check saturating the disk), so
# every 500 ms splash probe timed out although uvicorn had been serving
# since +5 s. Observed 2026-08-21: uvicorn up at +5 s, first deferred step
# done at +59 s, splash failure on every launch.
#
# Now the request path only READS an in-memory snapshot that a daemon
# thread refreshes every _HEALTH_TTL seconds. No lock, no database, no
# cloud on the request path. Until the first refresh lands the payload
# says state="pending" — the splash only needs HTTP 200 — and the UI
# re-polls until state="ready" (App.jsx health consumers).
from app import boot_clock as _boot_clock

_HEALTH_TTL = 5.0
_SNAPSHOT: dict = {"at": 0.0, "routing": {}, "license": {}, "state": "pending"}
_REFRESHER_LOCK = _threading.Lock()
_REFRESHER_STARTED = False
_FIRST_SERVED = {"mono": None, "age_s": None}


def _workspace_summary() -> dict:
    """Never let a workspace probe break the health endpoint."""
    try:
        from app.services import workspace_identity
        return workspace_identity.summary()
    except Exception:
        return {}


def _refresh_once() -> None:
    global _SNAPSHOT
    routing = _collect_routing()
    license_summary = _collect_license_summary()
    _SNAPSHOT = {
        "at": _time.monotonic(),
        "routing": routing,
        "license": license_summary,
        "state": "ready",
    }


def _refresher_loop() -> None:
    t0 = _time.monotonic()
    first = True
    while True:
        try:
            _refresh_once()
            if first:
                first = False
                print(
                    f"[trustnode][boot] health snapshot ready +{_boot_clock.age_s():.2f}s "
                    f"after process start (first refresh took {_time.monotonic() - t0:.2f}s)",
                    flush=True,
                )
        except Exception:
            pass
        _time.sleep(_HEALTH_TTL)


def _ensure_refresher() -> None:
    """Start the snapshot refresher once (idempotent, thread-safe)."""
    global _REFRESHER_STARTED
    if _REFRESHER_STARTED:
        return
    with _REFRESHER_LOCK:
        if _REFRESHER_STARTED:
            return
        _REFRESHER_STARTED = True
        _threading.Thread(
            target=_refresher_loop, name="trustnode-health-refresher", daemon=True
        ).start()


def first_health_served_age_s():
    """Seconds after process start when the first /api/health was served,
    or None until it happens. Read by the boot-health watchdog (app.main)."""
    return _FIRST_SERVED["age_s"]


def snapshot_state() -> str:
    return str(_SNAPSHOT.get("state") or "pending")


# Kick the refresher at import so the snapshot is usually "ready" long
# before the UI loads; the lazy call in health() covers any other order.
try:
    _ensure_refresher()
except Exception:
    pass


@router.get("/health")
async def health() -> dict:
    # async on purpose (2026-08-21): this handler is lock-free and does zero
    # I/O, so it must NOT need a threadpool token. When app_store._lock is
    # held for seconds, the sync handlers the UI polls pile up and exhaust the
    # anyio pool (231 threads observed) — a sync health could not even be
    # scheduled and the backend looked dead to the splash and the supervisor.
    _ensure_refresher()
    if _FIRST_SERVED["mono"] is None:
        _FIRST_SERVED["mono"] = _time.monotonic()
        _FIRST_SERVED["age_s"] = round(_boot_clock.age_s(), 2)
        print(
            f"[trustnode][boot] first /api/health served +{_FIRST_SERVED['age_s']:.2f}s after process start",
            flush=True,
        )

    # Operator 2026-06-19 (L5): surface the boot-time SQLite quick_check.
    integrity = {}
    try:
        integrity = dict(getattr(app_store, "_last_integrity_results", {}) or {})
    except Exception:
        integrity = {}

    snap = _SNAPSHOT  # atomic dict swap in the refresher; never mutated in place
    return {
        "status": "ok",
        "api_build": "edge-2026-06-23-canonical-db-1",
        "capabilities": {
            "database_active_sink": True,
            "database_file_sinks": True,
            "plc_discover_tags": True,
            "plc_opcua_browse_tree": True,
            "plc_multi_gateway": True,
            "app_store_db_primary": True,
            "database_recovery_routines": True,
            "tenant_aliases": True,
            "data_continuity": True,
            "gateway_autoresume": True,
            "sink_circuit_breaker": True,
            "plc_read_timeout_guard": True,
            "canonical_customer_db_reads": True,
            "batch_management_module": True,
        },
        "integrity": integrity,
        "historian_read_routing": snap["routing"],
        "license_summary": snap["license"],
        # 2026-08-22: which workspace this process is actually serving. When a
        # data-dir override points the app at an EMPTY store while the machine's
        # usual workspace holds data, an operator sees "all my data is gone".
        # Publishing it lets the UI say what actually happened. Computed once
        # per process (workspace_identity.summary), so the health path stays
        # lock-free and database-free.
        "workspace": _workspace_summary(),
        "health_snapshot": {
            "state": snap["state"],
            "age_s": (round(_time.monotonic() - snap["at"], 1) if snap["at"] else None),
        },
        "boot": {
            "process_age_s": round(_boot_clock.age_s(), 2),
            "first_health_served_s": _FIRST_SERVED["age_s"],
        },
    }


@router.get("/boot-probe")
def boot_probe(request: Request) -> dict:
    """Public-by-design health probe used by the Electron splash.

    Operator 2026-06-25: the desktop splash needs ONE call that tells
    it whether every configured device, database, and cloud target is
    reachable — so the UI doesn't open until everything the user
    configured is healthy. Auth-free because the splash runs BEFORE
    any user login. The endpoint exposes only connectivity booleans
    (host:port reachable / not), never sensitive payloads.
    """
    import socket as _socket
    import subprocess as _subprocess
    import platform as _platform
    from urllib.parse import urlparse as _urlparse

    out = {
        "ok": True,
        "backend": True,
        "devices": [],
        "databases": [],
        "cloud": [],
        "failures": [],
    }

    def _tcp_ok(host: str, port: int, timeout_s: float = 1.5) -> tuple[bool, str]:
        try:
            with _socket.create_connection((host, int(port)), timeout=timeout_s):
                return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _ping_ok(host: str, timeout_ms: int = 800) -> bool:
        """ICMP ping — same thing the user does in CMD. If this works
        the device is on the network, period."""
        try:
            is_win = _platform.system().lower().startswith("win")
            if is_win:
                cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
            else:
                cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
            proc = _subprocess.run(
                cmd,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                timeout=max(1.5, timeout_ms / 1000.0 + 0.5),
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    # 1. Devices — from bootstrap, probe each configured PLC.
    try:
        bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        devs = bs.get("devices") or []
        gws = bs.get("gateway_configurations") or []
        dbs = bs.get("database_configurations") or []
    except Exception:
        devs, gws, dbs = [], [], []

    # Operator 2026-06-25: simplified probe. PRIMARY signal is ICMP
    # ping — the same thing the user does in CMD. If ping works, the
    # device is on the network and we report ONLINE. We DON'T require
    # the configured PLC port to be open: a PLC sim that's between
    # cycles, a firewall that drops 44818 but allows ICMP, or a
    # device that uses a non-standard port would all wrongly show as
    # FAIL under the old "TCP must succeed" model. If ping fails too,
    # try a TCP probe to the known protocol port as a fallback.
    for d in devs:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or d.get("plc_ip") or d.get("id") or "Device")
        ip = str(d.get("plc_ip") or "").strip()
        gtype = str(d.get("gateway_type") or "").strip().lower()
        if gtype == "siemens_opcua":
            url = str(d.get("opc_url") or "").strip()
            if url:
                try:
                    parsed = _urlparse(url)
                    if parsed.hostname:
                        ip = parsed.hostname
                except Exception:
                    pass
        if not ip:
            out["devices"].append({"id": d.get("id"), "name": name, "ok": False, "reason": "missing IP in configuration"})
            out["failures"].append(f"{name}: missing IP in configuration")
            out["ok"] = False
            continue
        # Primary: ICMP ping.
        if _ping_ok(ip, 800):
            out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "ok": True, "reason": f"{ip} responds to ping"})
            continue
        # Fallback: TCP probe to known protocol port.
        port_map = {"siemens_opcua": 4840, "siemens_snap7": 102, "allen_bradley": 44818, "modbus_tcp_meter": 502}
        port = port_map.get(gtype, 0)
        if port:
            ok, reason = _tcp_ok(ip, port, 1.5)
            if ok:
                out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "port": port, "ok": True, "reason": f"{ip}:{port} reachable"})
                continue
            out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "port": port, "ok": False, "reason": f"Ping + TCP {port} both failed ({reason})"})
            out["failures"].append(f"{name} ({ip}) — Ping + TCP {port} both failed")
            out["ok"] = False
            continue
        # No protocol port to fall back on.
        out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "ok": False, "reason": f"{ip} did not respond to ping"})
        out["failures"].append(f"{name} ({ip}) — did not respond to ping")
        out["ok"] = False

    # 2. Databases — SQLite always ok (the file exists or the bootstrap
    # would have failed); non-SQLite gets a TCP probe to host:port.
    for c in dbs:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or c.get("engine") or c.get("id") or "Database")
        eng = str(c.get("engine") or "").strip().lower()
        if eng == "sqlite":
            out["databases"].append({"id": c.get("id"), "name": name, "engine": "sqlite", "ok": True, "reason": ""})
            continue
        host = str(c.get("host") or "").strip()
        port = int(c.get("port") or 0)
        if not host or not port:
            out["databases"].append({"id": c.get("id"), "name": name, "engine": eng, "ok": False, "reason": "missing host/port"})
            out["failures"].append(f"{name}: missing host/port")
            out["ok"] = False
            continue
        ok, reason = _tcp_ok(host, port, 2.0)
        out["databases"].append({"id": c.get("id"), "name": name, "engine": eng, "host": host, "port": port, "ok": ok, "reason": reason})
        if not ok:
            out["failures"].append(f"{name} ({host}:{port}) — {reason}")
            out["ok"] = False

    # 3. Cloud sync — if any DB has cloud_sync_enabled, probe the cloud
    # base URL host:443. Best-effort; failures don't gate boot today.
    try:
        for c in dbs:
            if not isinstance(c, dict):
                continue
            if not c.get("cloud_sync_enabled"):
                continue
            url = str(c.get("legacy_url") or c.get("cloud_url") or "").strip()
            if not url:
                continue
            try:
                parsed = _urlparse(url)
                host = parsed.hostname
                port = parsed.port or (443 if (parsed.scheme or "").lower() == "https" else 80)
                if not host:
                    continue
                ok, reason = _tcp_ok(host, port, 2.5)
                out["cloud"].append({"name": str(c.get("name") or "Cloud"), "host": host, "port": port, "ok": ok, "reason": reason})
                if not ok:
                    out["failures"].append(f"Cloud ({host}:{port}) — {reason}")
                    out["ok"] = False
            except Exception:
                pass
    except Exception:
        pass

    return out


@router.post("/boot-probe")
def boot_probe_post(request: Request, payload: dict) -> dict:
    """Operator 2026-06-25: same probe logic as GET /api/boot-probe but
    the CALLER tells us what to probe — so we don't have to guess at
    the active tenant scope. The React UI knows what devices/DBs are
    on screen; it sends them here and we ping/probe them. Eliminates
    every tenant-scoping false negative in one shot.

    Body:
      {
        "devices":   [{"id": "...", "name": "...", "ip": "...", "gateway_type": "..."}],
        "databases": [{"id": "...", "name": "...", "engine": "...", "host": "...", "port": 5432}]
      }

    Public (auth-free) on the same justification as GET — only TCP
    connectivity is exposed.
    """
    import socket as _socket
    import subprocess as _subprocess
    import platform as _platform
    from urllib.parse import urlparse as _urlparse

    out = {"ok": True, "devices": [], "databases": [], "failures": []}

    def _tcp_ok(host: str, port: int, timeout_s: float = 1.5):
        try:
            with _socket.create_connection((host, int(port)), timeout=timeout_s):
                return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _ping_ok(host: str, timeout_ms: int = 800) -> bool:
        try:
            is_win = _platform.system().lower().startswith("win")
            if is_win:
                cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
            else:
                cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
            proc = _subprocess.run(
                cmd,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                timeout=max(1.5, timeout_ms / 1000.0 + 0.5),
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    devs = payload.get("devices") if isinstance(payload, dict) else []
    dbs = payload.get("databases") if isinstance(payload, dict) else []
    if not isinstance(devs, list): devs = []
    if not isinstance(dbs, list): dbs = []

    for d in devs:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or d.get("ip") or d.get("id") or "Device")
        ip = str(d.get("ip") or d.get("plc_ip") or "").strip()
        gtype = str(d.get("gateway_type") or "").strip().lower()
        if gtype == "siemens_opcua":
            url = str(d.get("opc_url") or "").strip()
            if url:
                try:
                    parsed = _urlparse(url)
                    if parsed.hostname:
                        ip = parsed.hostname
                except Exception:
                    pass
        if not ip:
            out["devices"].append({"id": d.get("id"), "name": name, "ok": False, "reason": "missing IP"})
            out["failures"].append(f"{name}: missing IP"); out["ok"] = False
            continue
        if _ping_ok(ip, 800):
            out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "ok": True, "reason": f"{ip} responds to ping"})
            continue
        port_map = {"siemens_opcua": 4840, "siemens_snap7": 102, "allen_bradley": 44818, "modbus_tcp_meter": 502}
        port = port_map.get(gtype, 0)
        if port:
            ok, reason = _tcp_ok(ip, port, 1.5)
            if ok:
                out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "port": port, "ok": True, "reason": f"{ip}:{port} reachable"})
                continue
            out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "port": port, "ok": False, "reason": f"Ping + TCP {port} failed ({reason})"})
            out["failures"].append(f"{name} ({ip}) — Ping + TCP {port} failed"); out["ok"] = False
            continue
        out["devices"].append({"id": d.get("id"), "name": name, "ip": ip, "ok": False, "reason": f"{ip} did not respond to ping"})
        out["failures"].append(f"{name} ({ip}) — did not respond to ping"); out["ok"] = False

    for c in dbs:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or c.get("engine") or c.get("id") or "Database")
        eng = str(c.get("engine") or "").strip().lower()
        if eng == "sqlite":
            out["databases"].append({"id": c.get("id"), "name": name, "engine": "sqlite", "ok": True, "reason": "local file"})
            continue
        host = str(c.get("host") or "").strip()
        try:
            port = int(c.get("port") or 0)
        except Exception:
            port = 0
        if not host or not port:
            out["databases"].append({"id": c.get("id"), "name": name, "engine": eng, "ok": False, "reason": "missing host/port"})
            out["failures"].append(f"{name}: missing host/port"); out["ok"] = False
            continue
        ok, reason = _tcp_ok(host, port, 2.0)
        out["databases"].append({"id": c.get("id"), "name": name, "engine": eng, "host": host, "port": port, "ok": ok, "reason": reason or f"{host}:{port} reachable"})
        if not ok:
            out["failures"].append(f"{name} ({host}:{port}) — {reason}"); out["ok"] = False

    return out


@router.post("/historian/force-sqlite-reads")
def set_force_sqlite_reads(request: Request, payload: dict) -> dict:
    """Admin toggle: route ALL historian reads through SQLite regardless
    of customer-DB readiness. Persisted via app_settings so the choice
    survives a restart. The auth middleware already restricts this to
    authenticated sessions; the route checks role=admin."""
    user = getattr(request.state, "current_user", None) or {}
    role = str((user.get("role") if isinstance(user, dict) else "") or "").lower()
    if role not in ("admin", "super"):
        raise HTTPException(status_code=403, detail="admin role required")
    enabled = bool(payload.get("enabled", False))
    try:
        settings = app_store._get_app_settings() or {}
    except Exception:
        settings = {}
    settings["force_sqlite_reads"] = enabled
    try:
        app_store.upsert_domain("app_settings", settings, actor=user.get("username") if isinstance(user, dict) else "admin")
    except Exception:
        pass
    try:
        app_store.invalidate_customer_read_target_cache()
    except Exception:
        pass
    return {"ok": True, "force_sqlite_reads": enabled}
