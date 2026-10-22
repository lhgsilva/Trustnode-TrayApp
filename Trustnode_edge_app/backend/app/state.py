import os
import threading
from typing import Any


def _ensure_writable_data_dir() -> None:
    """Point TRUSTNODE_DATA_DIR somewhere this process can actually write.

    2026-10-22, from a customer's brand-new machine: the installer creates
    %ProgramData%/TrustNode/edge read-only for ordinary users ON PURPOSE, so
    nobody can open the SQLite and edit licence flags. The app RUNS as such a
    user, and on a machine with no previous install the desktop shell's
    workspace detector picks exactly that directory and passes it here as
    TRUSTNODE_DATA_DIR. Every service then tried to create its database there
    and the process died at IMPORT with exit code 1, before serving anything -
    "the backend did not start". The portable build never creates that
    directory, which is why it worked on the same PC.

    app_store had a fallback for this but it only applied when the variable
    was UNSET, and TelemetryService (constructed first, below) never had one.
    Fixing it per-service is whack-a-mole: the value is corrected ONCE here,
    before any service is constructed, so telemetry, the app store, the auth
    store and reports all agree on one writable location.

    os.access() is not usable for this on Windows - it reports the read-only
    ATTRIBUTE, not the ACL, and answers True for a directory that refuses
    every write. The only honest test is to write a file.
    """
    data_dir = str(os.environ.get("TRUSTNODE_DATA_DIR", "") or "").strip()
    if not data_dir:
        return  # nothing explicit: each service keeps its own default
    probe = os.path.join(data_dir, ".tn-write-probe")
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(probe)
        return  # writable - leave it exactly as the caller asked
    except OSError:
        pass
    fallback = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
    try:
        os.makedirs(fallback, exist_ok=True)
        with open(os.path.join(fallback, ".tn-write-probe"), "w",
                  encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(os.path.join(fallback, ".tn-write-probe"))
    except OSError:
        print("[trustnode][boot] WARN: data dir %s is not writable and neither "
              "is %s - startup will almost certainly fail"
              % (data_dir, fallback), flush=True)
        return
    print("[trustnode][boot] WARN: data dir %s is not writable by this user "
          "(installer ACL); using %s instead" % (data_dir, fallback), flush=True)
    os.environ["TRUSTNODE_DATA_DIR"] = fallback


_ensure_writable_data_dir()

from app.services.plc_manager import PLCManager
from app.services.app_store import AppStore
from app.services.auth_store import AuthStore
from app.services.control_plane_store import ControlPlaneStore
from app.services.telemetry_service import TelemetryService
from app.services.ingest_store import IngestStore
from app.services.power_manager import PowerManager
from app.services.reports_store import ReportsStore
from app.services.retention_engine import RetentionEngine, set_engine as _set_retention_engine
from app.services.report_renderer import render_template_to_pdf
from app.services.report_scheduler import ReportRunner, ReportScheduler
from app.services.lite_report_poller import LiteReportRequestPoller
from app.services.cp_users_puller import CpUsersPuller, build_from_env as build_cp_users_puller
from app.routers.notifications import send_email_request

# Operator 2026-06-18: boot diagnostics. Each print is flushed so a hung
# customer machine produces a log that pinpoints WHICH service init blocked.
# Previously the only boot print was app_store_db; anything wedging between
# that and uvicorn binding looked identical from the outside.
print("[trustnode][boot] state: instantiating TelemetryService", flush=True)
telemetry_service = TelemetryService()
print("[trustnode][boot] state: instantiating IngestStore", flush=True)
ingest_store = IngestStore()
print("[trustnode][boot] state: instantiating PLCManager", flush=True)
plc_manager = PLCManager()
print("[trustnode][boot] state: instantiating AppStore", flush=True)
app_store = AppStore()
print("[trustnode][boot] state: AppStore ready, instantiating AuthStore", flush=True)
# Operator 2026-06-18: AuthStore is a dedicated SQLite file separate
# from app_store. It owns the users table and the JWT signing secret.
# Critical: AuthStore takes NONE of app_store's locks and does NO cloud
# I/O — that's what makes login latency deterministic even when the
# cloud is unreachable or app_store's lock is held by a stuck sync
# thread. See services/auth_store.py for the design.
auth_store = AuthStore()
print(f"[trustnode][boot] state: AuthStore ready at {auth_store.db_path}", flush=True)
print("[trustnode][boot] state: instantiating PowerManager", flush=True)
power_manager = PowerManager(app_store)
print("[trustnode][boot] state: PowerManager ready", flush=True)


def _build_control_plane_store():
    """Pick the control-plane backend based on TRUSTNODE_CONTROL_PLANE_BACKEND.

      - 'cloud': Supabase-canonical. All cp_* reads/writes go to the
        cloud Postgres project via the existing engine cache.
        Smoke-tested 2026-05-19.
      - 'local' (or anything else): the legacy SQLite-backed store on the
        VPS's local trustnode_app_store.db. Today's default.

    Wired here at import time so every caller (routers + workers + auth)
    transparently gets the right backend; no per-call branching needed.
    """
    # 2026-10-13: the target is a SETTING now (portal-editable), with the
    # environment kept as the break-glass path. control_plane_target.resolve()
    # owns the precedence; see that module for why a saved file wins over the
    # environment and how to force the old behaviour.
    try:
        from app.services import control_plane_target as _cp_target
        backend = _cp_target.backend()
    except Exception:
        # Never let a bad/unreadable target stop the service booting.
        backend = os.environ.get("TRUSTNODE_CONTROL_PLANE_BACKEND", "").strip().lower()
    if backend == "cloud":
        try:
            from app.services.control_plane_store_cloud import ControlPlaneStoreCloud
            return ControlPlaneStoreCloud()
        except Exception as exc:  # pragma: no cover
            # Fall back to local on any import failure so the service keeps
            # running rather than refusing to boot. The exception is logged
            # via stderr — visible in journalctl.
            import logging
            logging.getLogger(__name__).exception(
                "control_plane_store: cloud backend failed to initialise, "
                "falling back to local SQLite. error=%s", exc,
            )
            return ControlPlaneStore()
    return ControlPlaneStore()


print("[trustnode][boot] state: building ControlPlaneStore", flush=True)
control_plane_store = _build_control_plane_store()
print("[trustnode][boot] state: ControlPlaneStore ready, instantiating ReportsStore", flush=True)
reports_store = ReportsStore()
print("[trustnode][boot] state: ReportsStore ready", flush=True)


# ---------------------------------------------------------------------------
# Retention engine (operator 2026-08-21). Constructed here so the API can reach
# it, but it does NOT start work until app.main's deferred startup calls
# .start() — and even then it waits out RETENTION_BOOT_DELAY_S plus the health
# gate, so maintenance can never compete with boot.
# ---------------------------------------------------------------------------
def _retention_boot_ready() -> bool:
    """True once /api/health has actually answered a request."""
    try:
        from app.routers.health import first_health_served_age_s
        return first_health_served_age_s() is not None
    except Exception:
        return True


def _retention_tag_stats() -> dict[str, Any]:
    """Tag count + poll interval from the RUNNING gateways (in-memory, no lock),
    so the storage estimate reflects this machine rather than a guess."""
    try:
        statuses = plc_manager.list_gateway_statuses() or []
    except Exception:
        statuses = []
    tags = 0
    interval_ms = 0
    running = 0
    for st in statuses:
        data = st if isinstance(st, dict) else getattr(st, "__dict__", {}) or {}
        try:
            n = len(data.get("tags") or [])
        except Exception:
            n = 0
        tags += n
        try:
            iv = int(data.get("interval_ms") or 0)
        except Exception:
            iv = 0
        if iv > 0:
            interval_ms = iv if interval_ms == 0 else min(interval_ms, iv)
        if data.get("running"):
            running += 1
    if tags <= 0:
        return {}
    return {
        "tag_count": tags,
        "interval_s": (interval_ms / 1000.0) if interval_ms else 1.0,
        "gateways": len(statuses),
        "gateways_running": running,
        "source": "gateways",
    }


retention_engine = RetentionEngine(
    app_store._db_path,
    backup_dir_fn=app_store._get_backup_dir,
    boot_ready_fn=_retention_boot_ready,
    cloud_cursor_fn=app_store.cloud_forward_cursor_ms,
    tag_stats_fn=_retention_tag_stats,
)
_set_retention_engine(retention_engine)
print("[trustnode][boot] state: retention engine constructed (idle until deferred start)", flush=True)

# cp_users puller is created lazily on startup (main.py) once app_settings
# are loaded — we need the cloud URL + tenant from the bootstrap config.
cp_users_puller: "CpUsersPuller | None" = None


class _EmailSettingsHolder:
    """Thread-safe single-slot cache for the active email transport config.

    The scheduler reads this so timer-fired reports can be emailed without
    re-loading the user's email profile every tick. The frontend updates it
    whenever the user saves Email & Notifications settings (or when sending
    a report manually).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None

    def set(self, settings: dict[str, Any] | None) -> None:
        with self._lock:
            self._value = settings if isinstance(settings, dict) else None

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._value) if isinstance(self._value, dict) else None


scheduler_email_settings_holder = _EmailSettingsHolder()


def _scheduler_live_lookup(gateway_id: str, tag_name: str) -> float | None:
    """Latest value for (gateway_id, tag_name) using the live row cache.

    Mirrors what the dashboard live widgets see, so trigger evaluation matches
    user intuition even when historian writes lag.
    """
    gw = (gateway_id or "").strip()
    tag = (tag_name or "").strip().lower()
    if not gw or not tag:
        return None
    try:
        rows = app_store.get_live_rows(limit=2000, prefer_cloud_reads=False)
    except Exception:
        return None
    for r in rows or []:
        if str(r.get("gateway_id") or "").strip() != gw:
            continue
        if str(r.get("tag") or r.get("tag_name") or "").strip().lower() != tag:
            continue
        try:
            return float(r.get("value"))
        except Exception:
            return None
    return None


def _scheduler_history_lookup(gateway_id: str, tag_name: str,
                              from_utc: str, to_utc: str) -> list:
    """Readings for one tag BETWEEN two instants, oldest first.

    The cycle trigger used only the live value, sampled once per tick. A PLC
    coil that pulses high for a scan was therefore invisible unless the daemon
    happened to wake inside that pulse - toggling the inputs produced no
    report at all. Reading what was RECORDED catches the pulse whenever it
    happened, and carries its real timestamp.
    """
    gw = (gateway_id or "").strip()
    tag = (tag_name or "").strip()
    if not tag:
        return []
    try:
        rows = app_store.get_historian_rows_range(
            from_utc=from_utc, to_utc=to_utc, limit=2000, offset=0,
            gateway=gw, tag=tag, prefer_cloud_reads=False)
    except Exception:
        return []
    out = []
    for r in reversed(rows or []):
        ts = str(r.get("ts") or r.get("ts_utc") or "")
        if ts:
            out.append((ts, r.get("value")))
    return out


def _render_fn(template: dict[str, Any], *, window=None, segments=None,
               file_name: str = ""):
    """Render a template, optionally over a CYCLE window and under a chosen name.

    Both extras default to off, so a time- or tag-triggered schedule renders
    exactly as it always has.
    """
    output_path = None
    if file_name:
        import os as _os
        from pathlib import Path as _Path
        candidate = _Path(file_name)
        if candidate.is_absolute() or "/" in file_name or "\\" in file_name:
            # A full destination: the operator chose the folder (a local one,
            # or a synced OneDrive folder - the same thing to us). Create it,
            # and fall back to the reports folder if it cannot be written.
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                output_path = candidate
            except OSError:
                output_path = None
        if output_path is None:
            base = _os.environ.get("TRUSTNODE_DATA_DIR", "").strip() or _os.path.join(
                _os.path.expanduser("~"), ".trustnode_edge", "data")
            reports_dir = _Path(base) / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            output_path = reports_dir / candidate.name
    return render_template_to_pdf(template, output_path=output_path, window=window,
                                  segments=segments)


def _is_any_gateway_running() -> bool:
    """True when at least one configured PLC gateway is currently collecting.

    The report scheduler consults this when a schedule has the
    `require_gateway_running` flag set, so scheduled reports can be gated on
    live data actually flowing into the historian. Falls back to True on any
    exception so a transient probe failure doesn't silently suppress reports.
    """
    try:
        statuses = plc_manager.list_gateway_statuses() or []
    except Exception:
        return True
    for status in statuses:
        try:
            if bool(status.get("running")):
                return True
        except Exception:
            continue
    return False


report_runner = ReportRunner(
    reports_store=reports_store,
    render_fn=_render_fn,
    send_email_fn=send_email_request,
)
report_scheduler = ReportScheduler(
    runner=report_runner,
    reports_store=reports_store,
    live_lookup=_scheduler_live_lookup,
    history_lookup=_scheduler_history_lookup,
    email_settings_lookup=scheduler_email_settings_holder.get,
    is_any_gateway_running=_is_any_gateway_running,
    tick_seconds=15,
)

# Drains the Lite "Generate" queue (Supabase table populated by the Lite app).
# Same ReportRunner — the synthesized schedule never asks for email delivery.
lite_report_poller = LiteReportRequestPoller(
    runner=report_runner,
    reports_store=reports_store,
    tick_seconds=10,
)
