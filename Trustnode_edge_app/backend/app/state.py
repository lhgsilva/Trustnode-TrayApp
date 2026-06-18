import os
import threading
from typing import Any

from app.services.plc_manager import PLCManager
from app.services.app_store import AppStore
from app.services.auth_store import AuthStore
from app.services.control_plane_store import ControlPlaneStore
from app.services.telemetry_service import TelemetryService
from app.services.ingest_store import IngestStore
from app.services.power_manager import PowerManager
from app.services.reports_store import ReportsStore
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


def _render_fn(template: dict[str, Any]):
    return render_template_to_pdf(template)


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
