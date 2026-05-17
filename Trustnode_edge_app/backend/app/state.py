import threading
from typing import Any

from app.services.plc_manager import PLCManager
from app.services.app_store import AppStore
from app.services.control_plane_store import ControlPlaneStore
from app.services.telemetry_service import TelemetryService
from app.services.ingest_store import IngestStore
from app.services.power_manager import PowerManager
from app.services.reports_store import ReportsStore
from app.services.report_renderer import render_template_to_pdf
from app.services.report_scheduler import ReportRunner, ReportScheduler
from app.services.lite_report_poller import LiteReportRequestPoller
from app.routers.notifications import send_email_request

telemetry_service = TelemetryService()
ingest_store = IngestStore()
plc_manager = PLCManager()
app_store = AppStore()
power_manager = PowerManager(app_store)
control_plane_store = ControlPlaneStore()
reports_store = ReportsStore()


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
