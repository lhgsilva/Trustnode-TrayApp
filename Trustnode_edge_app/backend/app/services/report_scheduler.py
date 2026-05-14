"""Backend daemon that fires scheduled reports.

Runs in a single daemon thread. Wakes every `tick_seconds` (default 15) and:

  1. For each enabled schedule, recompute the *next time the time-trigger
     should fire* (`_next_time_fire`) and the latest tag values.
  2. If trigger conditions are met (time and/or tag, depending on mode), and
     the schedule hasn't run in the same minute (debounce), call
     `ReportRunner.run(schedule, triggered_by="time"|"tag")`.
  3. Persist the resulting PDF record, optionally email it.

The runner is also reusable from the HTTP "run now" endpoint, so the same
code path produces the same artifact whether invoked manually or by the timer.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.tenant import set_current_tenant


# --------------------------------------------------------------------------- #
# trigger evaluation helpers (mirror frontend semantics)
# --------------------------------------------------------------------------- #
def _compare(value: float, operator: str, threshold: float, threshold2: float | None = None) -> bool:
    op = (operator or "").strip().lower()
    if op in {">", "gt"}:
        return value > threshold
    if op in {">=", "gte"}:
        return value >= threshold
    if op in {"<", "lt"}:
        return value < threshold
    if op in {"<=", "lte"}:
        return value <= threshold
    if op in {"==", "=", "eq"}:
        return value == threshold
    if op in {"!=", "<>", "ne"}:
        return value != threshold
    if op == "between" and threshold2 is not None:
        lo, hi = sorted([threshold, threshold2])
        return lo <= value <= hi
    return False


def _evaluate_tag_conditions(
    conditions: list[dict[str, Any]],
    logic: str,
    live_lookup: Callable[[str, str], float | None],
) -> tuple[bool, list[dict[str, Any]]]:
    """Returns (overall, per-condition-result-list)."""
    if not conditions:
        return False, []
    details: list[dict[str, Any]] = []
    satisfied = 0
    enabled = 0
    for c in conditions:
        if not isinstance(c, dict):
            continue
        if not bool(c.get("enabled", True)):
            continue
        enabled += 1
        gw = str(c.get("gateway_id") or "").strip()
        tag = str(c.get("tag_name") or "").strip()
        try:
            threshold = float(c.get("value")) if c.get("value") is not None else float(c.get("value1"))
        except Exception:
            details.append({"gateway_id": gw, "tag": tag, "result": False, "reason": "invalid threshold"})
            continue
        threshold2 = None
        if c.get("value2") not in (None, ""):
            try:
                threshold2 = float(c.get("value2"))
            except Exception:
                threshold2 = None
        live = live_lookup(gw, tag)
        if live is None:
            details.append({"gateway_id": gw, "tag": tag, "result": False, "reason": "no live value"})
            continue
        op = str(c.get("operator") or ">=")
        try:
            hit = _compare(float(live), op, threshold, threshold2)
        except Exception:
            hit = False
        details.append({"gateway_id": gw, "tag": tag, "result": hit, "value": live, "operator": op, "threshold": threshold})
        if hit:
            satisfied += 1
    if enabled == 0:
        return False, details
    if (logic or "all").strip().lower() == "any":
        return satisfied > 0, details
    return satisfied == enabled, details


def _next_time_fire(schedule: dict[str, Any], now: datetime) -> datetime | None:
    """Next moment a time-based schedule should fire, at or after `now`."""
    recurrence = (schedule.get("recurrence") or "daily").strip().lower()
    hour = int(schedule.get("hour") or 0)
    minute = int(schedule.get("minute") or 0)
    if recurrence == "hourly":
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(hours=1)
        return candidate
    if recurrence == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate
    if recurrence == "weekly":
        dow = schedule.get("day_of_week")
        if dow is None:
            return None
        target_dow = int(dow) % 7  # 0=Mon
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (target_dow - candidate.weekday()) % 7
        candidate = candidate + timedelta(days=delta)
        if candidate <= now:
            candidate = candidate + timedelta(days=7)
        return candidate
    if recurrence == "monthly":
        dom = int(schedule.get("day_of_month") or 1)
        candidate = now.replace(day=min(28, dom), hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            month = candidate.month + 1
            year = candidate.year
            if month > 12:
                month, year = 1, year + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate
    return None


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class ReportRunner:
    """Stateless helper: turn a schedule into a generated PDF + optional email."""

    def __init__(self, reports_store, render_fn, send_email_fn) -> None:
        self.store = reports_store
        self.render = render_fn          # callable(template_dict) -> (Path, bytes, sha)
        self.send_email = send_email_fn  # callable(EmailRequest) -> EmailResult

    def run(
        self,
        schedule: dict[str, Any],
        *,
        triggered_by: str = "manual",
        email_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tenant_id = (schedule.get("tenant_id") or "default").strip() or "default"
        set_current_tenant(tenant_id)
        template = self.store.get_template(schedule.get("template_id") or "", tenant_id=tenant_id)
        if not template:
            return {
                "ok": False,
                "error": f"Template {schedule.get('template_id')!r} not found",
            }
        try:
            path, byte_count, sha = self.render(template)
        except Exception as exc:
            return {"ok": False, "error": f"PDF render failed: {exc}"}

        record = self.store.insert_generated({
            "tenant_id": tenant_id,
            "template_id": template.get("id"),
            "template_name": template.get("name"),
            "schedule_id": schedule.get("id"),
            "schedule_name": schedule.get("name"),
            "triggered_by": triggered_by,
            "file_path": str(path),
            "file_name": path.name,
            "file_bytes": byte_count,
            "file_sha256": sha,
            "meta": {},
        })

        result = {"ok": True, "generated": record}

        if bool(schedule.get("deliver_email")) and schedule.get("recipients"):
            email_outcome = self._send_with_attachment(
                schedule=schedule,
                template=template,
                file_path=path,
                email_settings=email_settings,
            )
            try:
                self.store.update_generated_email_status(
                    record["id"],
                    status="sent" if email_outcome.get("ok") else "failed",
                    message=email_outcome.get("message"),
                    recipients=schedule.get("recipients") or [],
                )
            except Exception:
                pass
            result["email"] = email_outcome
        return result

    def _send_with_attachment(
        self,
        *,
        schedule: dict[str, Any],
        template: dict[str, Any],
        file_path: Path,
        email_settings: dict[str, Any] | None,
    ) -> dict[str, Any]:
        settings = email_settings or {}
        transport = str(settings.get("transport") or "smtp").strip().lower()
        smtp = settings.get("smtp") or {}
        php = settings.get("php_mail")
        recipients = [str(x).strip() for x in (schedule.get("recipients") or []) if str(x).strip()]
        if not recipients:
            return {"ok": False, "message": "No recipients configured."}

        # Local import to avoid module load order issues during boot.
        from app.routers.notifications import EmailRequest, SMTPConfig, PHPMailConfig, EmailAttachment, send_email_request
        from app.services.report_renderer import build_template_dataset_files

        # Decide which attachments the schedule asked for. PDF defaults on for
        # backwards-compat — the user must explicitly opt out by setting
        # attach_pdf=false. CSV/TXT are opt-in.
        want_pdf = bool(schedule.get("attach_pdf", True))
        want_csv = bool(schedule.get("attach_csv", False))
        want_txt = bool(schedule.get("attach_txt", False))

        attachments: list = []
        attachment_summary: list[str] = []

        if want_pdf:
            try:
                pdf_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
                attachments.append(EmailAttachment(
                    filename=file_path.name,
                    content_b64=pdf_b64,
                    content_type="application/pdf",
                ))
                attachment_summary.append(f"PDF ({file_path.stat().st_size // 1024} KB)")
            except Exception as exc:
                return {"ok": False, "message": f"Cannot read PDF for attachment: {exc}"}

        if want_csv or want_txt:
            # Build the CSV/TXT companions in the same reports dir using the
            # PDF's base name so the bundle is grouped on disk too.
            try:
                base_name = file_path.stem
                companions = build_template_dataset_files(
                    template, output_dir=file_path.parent, base_name=base_name
                )
            except Exception as exc:
                companions = {}
                attachment_summary.append(f"dataset export failed: {exc}")
            if want_csv and "csv" in companions:
                csv_path = companions["csv"]
                try:
                    b64 = base64.b64encode(csv_path.read_bytes()).decode("ascii")
                    attachments.append(EmailAttachment(
                        filename=csv_path.name,
                        content_b64=b64,
                        content_type="text/csv",
                    ))
                    attachment_summary.append(f"CSV ({csv_path.stat().st_size // 1024} KB)")
                except Exception:
                    pass
            if want_txt and "txt" in companions:
                txt_path = companions["txt"]
                try:
                    b64 = base64.b64encode(txt_path.read_bytes()).decode("ascii")
                    attachments.append(EmailAttachment(
                        filename=txt_path.name,
                        content_b64=b64,
                        content_type="text/plain",
                    ))
                    attachment_summary.append(f"TXT ({txt_path.stat().st_size // 1024} KB)")
                except Exception:
                    pass

        if not attachments:
            return {"ok": False, "message": "No attachments selected for delivery."}

        subject = (schedule.get("email_subject") or f"Report: {template.get('name') or 'Trustnode'}").strip()
        body_extra = ""
        if attachment_summary:
            body_extra = "<p>Attachments: " + ", ".join(attachment_summary) + "</p>"
        body = schedule.get("email_body") or (
            f"<p>Attached: <b>{template.get('name') or 'report'}</b>.</p>"
            f"<p>Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}.</p>"
            + body_extra
        )
        request = EmailRequest(
            transport="php_http" if transport == "php_http" else "smtp",
            smtp=SMTPConfig(**(smtp if isinstance(smtp, dict) else {})),
            php_mail=PHPMailConfig(**(php if isinstance(php, dict) else {})) if isinstance(php, dict) else None,
            to=recipients,
            cc=[],
            bcc=[],
            subject=subject,
            html_body=body,
            text_body=f"Trustnode report: {template.get('name') or ''}",
            attachments=attachments,
        )
        outcome = send_email_request(request)
        return {"ok": bool(outcome.ok), "message": str(outcome.message or ""), "recipients": list(outcome.recipients or [])}


# --------------------------------------------------------------------------- #
# daemon
# --------------------------------------------------------------------------- #
class ReportScheduler:
    """Background thread that ticks schedules.

    `live_lookup` is supplied by the caller (App composition root) and reads
    the latest value for a (gateway_id, tag_name) pair — typically wrapping
    `app_store.get_live_rows()` so the same source feeds the UI live charts
    and the trigger evaluator.

    `email_settings_lookup` returns the currently-active transport config
    (SMTP/PHP) so the daemon can email reports without re-parsing the user's
    saved email profile every tick. Returning `None` disables email delivery
    even if the schedule asked for it.
    """

    def __init__(
        self,
        runner: ReportRunner,
        reports_store,
        *,
        live_lookup: Callable[[str, str], float | None],
        email_settings_lookup: Callable[[], dict[str, Any] | None] | None = None,
        is_any_gateway_running: Callable[[], bool] | None = None,
        tick_seconds: int = 15,
    ) -> None:
        self.runner = runner
        self.store = reports_store
        self.live_lookup = live_lookup
        self.email_settings_lookup = email_settings_lookup or (lambda: None)
        # Returns True when at least one PLC gateway is actively collecting.
        # Optional — schedules without `require_gateway_running` ignore this.
        self.is_any_gateway_running = is_any_gateway_running or (lambda: True)
        self.tick_seconds = max(5, int(tick_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Debounce: per-schedule "last fire minute" so we don't fire twice when
        # the daemon ticks faster than once a minute.
        self._last_fire_key: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="tn-report-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._thread = None

    def _loop(self) -> None:
        # Slight delay before the first tick so app_store schema migrations
        # have time to finish on cold boots.
        time.sleep(2.0)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                # Swallow per-tick errors; one bad schedule shouldn't kill the daemon.
                pass
            self._stop_event.wait(self.tick_seconds)

    def _tick(self) -> None:
        now_utc = datetime.now(timezone.utc)
        schedules = self.store.list_enabled_schedules_all_tenants()
        if not schedules:
            return
        for schedule in schedules:
            try:
                self._consider_schedule(schedule, now_utc)
            except Exception:
                continue

    def _consider_schedule(self, schedule: dict[str, Any], now_utc: datetime) -> None:
        sched_id = str(schedule.get("id") or "")
        mode = (schedule.get("trigger_mode") or "time").strip().lower()
        fire_key = now_utc.strftime("%Y-%m-%d %H:%M")
        if self._last_fire_key.get(sched_id) == fire_key:
            return  # already fired this minute

        time_hit = False
        tag_hit = False

        if mode in {"time", "both"}:
            time_hit = self._is_time_fire(schedule, now_utc)
        if mode in {"tag", "both"}:
            ok, _details = _evaluate_tag_conditions(
                schedule.get("tag_conditions") or [],
                schedule.get("condition_logic") or "all",
                self.live_lookup,
            )
            tag_hit = ok

        if mode == "time":
            fire = time_hit
        elif mode == "tag":
            fire = tag_hit
        else:  # both
            fire = time_hit and tag_hit

        if not fire:
            return

        # Opt-in gate: when the user ticks "Only when a gateway is running",
        # skip this firing if every PLC gateway is currently stopped. The
        # debounce key is still set so we don't re-evaluate the same minute
        # over and over.
        if bool(schedule.get("require_gateway_running")):
            try:
                running = bool(self.is_any_gateway_running())
            except Exception:
                running = True  # fail-open if probing the manager errors
            if not running:
                self._last_fire_key[sched_id] = fire_key
                try:
                    self.store.mark_schedule_run(
                        schedule_id=sched_id,
                        last_run_utc=now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                        status="skipped",
                        error="No PLC gateway is running",
                        next_run_utc=None,
                    )
                except Exception:
                    pass
                return

        self._last_fire_key[sched_id] = fire_key
        email_settings = None
        try:
            email_settings = self.email_settings_lookup()
        except Exception:
            email_settings = None
        result = self.runner.run(schedule, triggered_by=mode, email_settings=email_settings)
        status = "success" if result.get("ok") else "error"
        error = None if result.get("ok") else str(result.get("error") or "")
        try:
            self.store.mark_schedule_run(
                schedule_id=sched_id,
                last_run_utc=now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                status=status,
                error=error,
                next_run_utc=None,
            )
        except Exception:
            pass

    @staticmethod
    def _is_time_fire(schedule: dict[str, Any], now_utc: datetime) -> bool:
        """True when the schedule's configured hour/minute matches now (UTC)."""
        recurrence = (schedule.get("recurrence") or "daily").strip().lower()
        hour = int(schedule.get("hour") or 0)
        minute = int(schedule.get("minute") or 0)
        if recurrence == "hourly":
            return now_utc.minute == minute
        if recurrence == "daily":
            return now_utc.hour == hour and now_utc.minute == minute
        if recurrence == "weekly":
            dow = schedule.get("day_of_week")
            if dow is None:
                return False
            return now_utc.weekday() == int(dow) and now_utc.hour == hour and now_utc.minute == minute
        if recurrence == "monthly":
            dom = int(schedule.get("day_of_month") or 1)
            return now_utc.day == dom and now_utc.hour == hour and now_utc.minute == minute
        return False
