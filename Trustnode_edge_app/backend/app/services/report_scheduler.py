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

import time as _time_rs

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("trustnode.reports")
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


def _resolve_report_columns(schedule: dict[str, Any], window, gateway: str = "") -> dict:
    """Values the operator wants to SEE beside each generated report.

    2026-10-27, requested: "on the generated reports page we should be able
    to customize the columns ... add a column of the report generated using
    a tag used in the report, like work order".

    A column cannot be filled in later: the work order a report covers is
    whatever the tag held during ITS run, and by the time anyone opens the
    list that tag has moved on. So each column is resolved here, against the
    run's own window, and stored with the row.

    Each entry is {label, pattern}; the pattern is the same token language
    as the file name and the folder, so {Work_Order}, masks like
    {Result|1=PASS,0=FAIL} and {date} all work without a second syntax.
    """
    # 2026-10-27: columns may be defined ONCE for the whole edge, from the
    # Generated Reports page where the operator is actually looking at the
    # list, as well as per schedule. Defining "Work order" in one place and
    # having every report carry it is what was asked for; a schedule can
    # still add its own, and its entry wins on a clashing heading.
    cols: list[Any] = []
    try:
        from app.state import app_store as _store
        settings = _store.get_domain_scoped_fast("app_settings") or {}
        glob = settings.get("report_columns")
        if isinstance(glob, list):
            cols.extend(glob)
    except Exception:
        pass
    own = schedule.get("report_columns")
    if isinstance(own, list):
        cols.extend(own)
    if not cols:
        return {}
    from app.services.report_renderer import resolve_text_tokens, fill_run_tokens
    started, ended = (window or ("", ""))
    out: dict[str, str] = {}
    for entry in cols:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        pattern = str(entry.get("pattern") or "").strip()
        if not label or not pattern:
            continue
        # 2026-10-28, reported with a screenshot: the operator typed the tag
        # itself - `SimREAL[3]` - and got that text back, because the token
        # engine only resolves a name inside braces. Asking somebody to
        # remember `{SimREAL[3]}` when the field is labelled "Value" and sits
        # beside a tag name is a syntax tax, not a feature. A value with no
        # token in it at all is read as the tag name it plainly is; anything
        # containing a brace is left exactly as written, so a mixed pattern
        # like "WO {Work_Order}" keeps working.
        if "{" not in pattern:
            pattern = "{%s}" % pattern.strip()
        try:
            text = resolve_text_tokens(pattern, started, ended, gateway, None)
            text = fill_run_tokens(text, started, ended)
        except Exception:
            text = ""
        out[label] = str(text or "").strip()
    return out


def _cycle_file_name(schedule: dict[str, Any], template: dict[str, Any],
                     window: tuple[str, str] | None, report_id: str) -> str:
    """The operator's naming pattern, resolved. Empty keeps the uuid name."""
    from app.services.report_renderer import resolve_destination
    # ONE resolver for both boxes. "Save to" used to win outright and discard
    # "File name", and a folder in Save to was then treated as the file
    # itself - so every run overwrote one PDF (2026-10-07).
    #
    # 2026-10-23: destination rules come first. They may redirect the folder,
    # the name or both based on what a tag holds; with no rules, or none
    # matching, this resolves exactly as it did before.
    rules = (schedule or {}).get("destination_rules")
    return resolve_destination(
        rules if isinstance(rules, list) else [],
        str((schedule or {}).get("output_path") or "").strip(),
        str((schedule or {}).get("file_name_pattern") or "").strip(),
        template_name=str((template or {}).get("name") or ""),
        report_id=report_id,
        started=(window or ("", ""))[0],
        ended=(window or ("", ""))[1],
    )


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
        window: tuple[str, str] | None = None,
        segments: list[dict[str, Any]] | None = None,
        report_id: str = "",
        cycle_records: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        tenant_id = (schedule.get("tenant_id") or "default").strip() or "default"
        set_current_tenant(tenant_id)
        # One trigger may produce SEVERAL reports. 2026-10-01: "we should have
        # an option to export two different or more reports inside of the same
        # start and end trigger, maybe two different templates". template_ids
        # is the list; template_id remains the single-template shape every
        # existing schedule already has.
        wanted = [str(t).strip() for t in (schedule.get("template_ids") or [])
                  if str(t or "").strip()]
        if not wanted:
            wanted = [str(schedule.get("template_id") or "").strip()]
        results = []
        for tpl_id in wanted:
            out = self._run_one(schedule, tpl_id, tenant_id, triggered_by,
                                email_settings, window, segments, report_id,
                                record_id=(cycle_records or {}).get(tpl_id, ""))
            results.append(out)
        if len(results) == 1:
            return results[0]
        return {"ok": all(r.get("ok") for r in results), "reports": results,
                "generated": (results[0] or {}).get("generated")}

    def _wanted_templates(self, schedule: dict[str, Any]) -> list[str]:
        wanted = [str(t).strip() for t in (schedule.get("template_ids") or [])
                  if str(t or "").strip()]
        return wanted or [str(schedule.get("template_id") or "").strip()]

    def open_cycle_records(self, schedule: dict[str, Any], started: str,
                           report_id: str = "") -> dict[str, str]:
        """2026-11-17, asked: "as soon as it sees a trigger to start it should
        show on the generated page with status Processing". One record per
        template, state "collecting", from the cycle's first reading. The
        close fills the same records in. Never raises."""
        out: dict[str, str] = {}
        tenant_id = (schedule.get("tenant_id") or "default").strip() or "default"
        try:
            set_current_tenant(tenant_id)
        except Exception:
            pass
        for tpl_id in self._wanted_templates(schedule):
            try:
                template = self.store.get_template(tpl_id, tenant_id=tenant_id) or {}
                rec = self.store.insert_generated({
                    "tenant_id": tenant_id, "template_id": tpl_id,
                    "template_name": template.get("name") or tpl_id,
                    "schedule_id": schedule.get("id"), "schedule_name": schedule.get("name"),
                    "triggered_by": "cycle", "file_path": "", "file_name": "",
                    "file_bytes": 0, "file_sha256": "",
                    "meta": {"window_from": str(started or ""), "window_to": "",
                             "report_id": str(report_id or ""), "trigger": "cycle",
                             "render_state": "collecting", "collecting_since": str(started or "")},
                })
                if rec.get("id"):
                    out[tpl_id] = str(rec["id"])
            except Exception as exc:
                logger.warning("report cycle: could not list the started cycle (%s)", exc)
        return out

    def find_cycle_records(self, schedule: dict[str, Any]) -> dict[str, str]:
        """The schedule's records still collecting (after a restart)."""
        out: dict[str, str] = {}
        try:
            rows = self.store.list_generated(
                tenant_id=(schedule.get("tenant_id") or "default"), limit=50,
                schedule_id=str(schedule.get("id") or "")) or []
        except Exception:
            return out
        for r in rows:
            if str((r.get("meta") or {}).get("render_state") or r.get("render_state") or "") == "collecting":
                out.setdefault(str(r.get("template_id") or ""), str(r.get("id") or ""))
        return out

    def cancel_cycle_records(self, schedule: dict[str, Any], records: dict[str, str], reason: str) -> None:
        tenant_id = (schedule.get("tenant_id") or "default").strip() or "default"
        for gid in (records or {}).values():
            try:
                self.store.update_generated_render(gid, tenant_id=tenant_id, render_state="cancelled",
                                                   render_error=reason)
            except Exception:
                pass

    def _run_one(self, schedule, template_id, tenant_id, triggered_by,
                 email_settings, window, segments, report_id, record_id: str = ""):
        template = self.store.get_template(template_id, tenant_id=tenant_id)
        if not template:
            if record_id:
                try:
                    self.store.update_generated_render(
                        record_id, tenant_id=tenant_id, render_state="failed",
                        render_error=f"Template {template_id!r} not found")
                except Exception:
                    pass
            return {
                "ok": False,
                "error": f"Template {template_id!r} not found",
            }
        # 2026-11-02: the record FIRST. It is on the Generated Reports list
        # (and in the cloud, when there is one) before a single byte of PDF
        # exists, with the window it covers; the render then fills in the
        # file - or the failure, which used to leave no trace at all.
        file_name = _cycle_file_name(schedule, template, window, report_id)
        _t_store = _time_rs.perf_counter()
        _meta_now = {
            "window_from": str((window or ("", ""))[0] or ""),
            "window_to": str((window or ("", ""))[1] or ""),
            "segments": list(segments or [])[:200],
            "segment_count": len(segments or []),
            "report_id": str(report_id or ""),
            "trigger": str(triggered_by or ""),
            "columns": _resolve_report_columns(schedule, window),
        }
        record = None
        if record_id:
            # the cycle's record, listed as "collecting" since its start
            try:
                record = self.store.update_generated_render(
                    record_id, tenant_id=tenant_id, render_state="pending",
                    file_name=os.path.basename(str(file_name or "")) or "",
                    meta_patch=_meta_now)
            except Exception:
                record = None
        if record:
            pass
        else:
          record = self.store.insert_generated({
            "tenant_id": tenant_id,
            "template_id": template.get("id"),
            "template_name": template.get("name"),
            "schedule_id": schedule.get("id"),
            "schedule_name": schedule.get("name"),
            "triggered_by": triggered_by,
            "file_path": "",
            # The planned destination may be a full path; the list shows a NAME
            # (seen as "C:/Users/.../Job Fold..." on a customer's Generated list).
            "file_name": os.path.basename(str(file_name or "")) or "",
            "file_bytes": 0,
            "file_sha256": "",
            "meta": {
                "window_from": str((window or ("", ""))[0] or ""),
                "window_to": str((window or ("", ""))[1] or ""),
                "segments": list(segments or [])[:200],
                "segment_count": len(segments or []),
                "report_id": str(report_id or ""),
                "trigger": str(triggered_by or ""),
                "columns": _resolve_report_columns(schedule, window),
                "render_state": "pending",
            },
        })
        _store_s = _time_rs.perf_counter() - _t_store
        gid = str(record.get("id") or "")

        try:
            _t_render = _time_rs.perf_counter()
            path, byte_count, sha = self.render(
                template, window=window, segments=segments, file_name=file_name)
            _render_s = _time_rs.perf_counter() - _t_render
        except Exception as exc:
            try:
                record = self.store.update_generated_render(
                    gid, tenant_id=tenant_id, render_state="failed",
                    render_error=f"PDF render failed: {exc}") or record
            except Exception:
                pass
            return {"ok": False, "error": f"PDF render failed: {exc}", "generated": record}

        snapshot = None
        try:
            from app.services.report_renderer import build_render_snapshot
            snapshot = build_render_snapshot(template, window=window, segments=segments)
        except Exception:
            snapshot = None
        _t_store2 = _time_rs.perf_counter()
        record = self.store.update_generated_render(
            gid, tenant_id=tenant_id, render_state="ready",
            render_seconds=_render_s, file_path=str(path), file_name=path.name,
            file_bytes=byte_count, file_sha256=sha, render_data=snapshot) or record
        _store_s += _time_rs.perf_counter() - _t_store2
        if (_render_s + _store_s) >= 5.0:
            try:
                _note = ("Report %r took %.1fs: %.1fs rendering, %.1fs writing "
                         "the record. The slow part is %s."
                         % (template.get("name"), _render_s + _store_s,
                            _render_s, _store_s,
                            "the render" if _render_s >= _store_s
                            else "the database write"))
                print("[trustnode][reports] " + _note, flush=True)
                from app.state import app_store as _st
                _st.append_log_rows([{"level": "warning", "category": "system",
                                      "message": _note}])
            except Exception:
                pass

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
        live_text_lookup: Callable[[str, str], str | None] | None = None,
        email_settings_lookup: Callable[[], dict[str, Any] | None] | None = None,
        is_any_gateway_running: Callable[[], bool] | None = None,
        tick_seconds: int = 15,
        history_lookup: Callable[[str, str, str, str], list] | None = None,
    ) -> None:
        self.runner = runner
        self.store = reports_store
        self.live_lookup = live_lookup
        self.live_text_lookup = live_text_lookup
        self.email_settings_lookup = email_settings_lookup or (lambda: None)
        # Returns True when at least one PLC gateway is actively collecting.
        # Optional — schedules without `require_gateway_running` ignore this.
        self.is_any_gateway_running = is_any_gateway_running or (lambda: True)
        self.tick_seconds = max(5, int(tick_seconds))
        # Readings recorded BETWEEN ticks. Without this the cycle conditions
        # are only ever sampled at the instant the daemon happens to wake, so
        # a PLC coil that pulses high for one scan is invisible - which is why
        # toggling the inputs produced no report at all. Optional: with no
        # history the evaluator falls back to the live value, as before.
        self.history_lookup = history_lookup
        self._last_seen: dict[str, str] = {}
        #: Schedules whose cycle closed while the START condition was still
        #: true. They stay blocked until it is observed FALSE - see
        #: _consider_cycle (2026-10-11).
        self._await_low: dict[str, bool] = {}
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
                # One bad schedule must not kill the daemon - but 2026-10-30,
                # asked: "the triggers created for schedule reports need to be
                # reliable". A trigger that stops firing and says nothing is
                # indistinguishable from one that was never configured, so the
                # failure is recorded. Throttled: a schedule that fails every
                # tick must not fill the log at 4 lines a minute.
                self._note_failure("scheduler tick", "")
            self._stop_event.wait(self.tick_seconds)

    #: When each distinct failure was last logged, so a permanently broken
    #: schedule reports once a minute rather than on every tick.
    _FAILURE_LOG_EVERY_S = 60.0

    def _note_failure(self, what: str, which: str) -> None:
        try:
            key = "%s|%s" % (what, which)
            now = time.time()
            seen = getattr(self, "_failure_seen", None)
            if seen is None:
                seen = {}
                self._failure_seen = seen
            if now - float(seen.get(key, 0.0)) < self._FAILURE_LOG_EVERY_S:
                return
            seen[key] = now
            logger.warning("report scheduler: %s failed%s - the trigger did "
                           "not run this time", what,
                           (" for %s" % which) if which else "",
                           exc_info=True)
        except Exception:
            pass

    def _tick(self) -> None:
        now_utc = datetime.now(timezone.utc)
        schedules = self.store.list_enabled_schedules_all_tenants()
        if not schedules:
            return
        for schedule in schedules:
            try:
                self._consider_schedule(schedule, now_utc)
            except Exception:
                self._note_failure(
                    "schedule",
                    str((schedule or {}).get("name")
                        or (schedule or {}).get("id") or "?"))
                continue

    def _conditions_hit(self, conditions: list, logic: str, since: str,
                        now_stamp: str, live_fallback: bool = True) -> tuple[bool, str]:
        """Were the conditions satisfied at any point since `since`?

        Returns (hit, when). `when` is the reading's OWN timestamp, so a cycle
        is stamped with the moment the bit actually changed rather than the
        moment the daemon happened to wake - the window is then accurate to
        the PLC, not to the 15 s tick.

        Falls back to the live value when no history is available, which is
        what the unit tests and any caller without a historian rely on.
        """
        conds = [c for c in (conditions or []) if isinstance(c, dict)]
        if not conds:
            return False, ""
        if not self.history_lookup or not since:
            ok, _ = _evaluate_tag_conditions(conds, logic, self.live_lookup)
            return ok, now_stamp

        firsts: list[str] = []
        for c in conds:
            gw = str(c.get("gateway_id") or "").strip()
            tag = str(c.get("tag_name") or c.get("tag") or "").strip()
            if not tag:
                continue
            try:
                rows = self.history_lookup(gw, tag, since, now_stamp) or []
            except Exception:
                rows = []
            hit_at = ""
            for ts, value in rows:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                try:
                    threshold = float(c.get("value"))
                except (TypeError, ValueError):
                    continue
                second = c.get("value2")
                try:
                    second = float(second) if second not in (None, "") else None
                except (TypeError, ValueError):
                    second = None
                if _compare(num, str(c.get("operator") or ">="), threshold, second):
                    hit_at = str(ts)
                    break
            if not hit_at:
                # 2026-11-16: nothing stored since `since` (collection gated,
                # or no new row yet) - the LIVE value still answers "is it
                # true now". The 0 that ends a gated test is never stored.
                ok_live = False
                if live_fallback:
                    try:
                        ok_live, _ = _evaluate_tag_conditions([c], "all", self.live_lookup)
                    except Exception:
                        ok_live = False
                if ok_live:
                    hit_at = now_stamp
            if not hit_at:
                if str(logic or "all").strip().lower() == "all":
                    return False, ""          # one unmet condition ends it
                continue
            firsts.append(hit_at)
        if not firsts:
            return False, ""
        # ALL -> satisfied once the LAST of them came true; ANY -> the first.
        when = max(firsts) if str(logic or "all").strip().lower() == "all" else min(firsts)
        return True, when

    def _start_dropped_since(self, schedule: dict[str, Any], since: str, now_stamp: str) -> bool:
        """Did the start condition read FALSE at some point since `since`?

        2026-11-16: with start_edge, a new cycle waits for the start bit to
        drop. That was asked of the LIVE value on the next tick - a part that
        ended and the next one that began inside one 15 s tick looked like a
        bit that never dropped, and the second part was merged or missed.
        History says when it dropped, to the reading."""
        conds = [c for c in (schedule.get("tag_conditions") or []) if isinstance(c, dict)]
        if not conds or not self.history_lookup or not since:
            return not self._start_true_now(schedule)
        logic = str(schedule.get("condition_logic") or "all").strip().lower()
        considered = 0
        dropped_count = 0
        for c in conds:
            gw = str(c.get("gateway_id") or "").strip()
            tag = str(c.get("tag_name") or c.get("tag") or "").strip()
            if not tag:
                continue
            try:
                rows = self.history_lookup(gw, tag, since, now_stamp) or []
            except Exception:
                rows = []
            try:
                threshold = float(c.get("value"))
            except (TypeError, ValueError):
                continue
            second = c.get("value2")
            try:
                second = float(second) if second not in (None, "") else None
            except (TypeError, ValueError):
                second = None
            dropped = False
            for _ts, value in rows:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                if not _compare(num, str(c.get("operator") or ">="), threshold, second):
                    dropped = True
                    break
            considered += 1
            if dropped:
                if logic == "all":
                    return True       # one condition false = the set is false
                dropped_count += 1
        if considered and logic != "all" and dropped_count == considered:
            return True               # ANY: every condition read false
        return not self._start_true_now(schedule)

    def _start_true_now(self, schedule: dict[str, Any]) -> bool:
        """Is the start condition set satisfied by the LIVE values?

        _conditions_hit answers "at any point since the last tick", which is
        the right question for catching a short pulse and the wrong one for
        "has the bit dropped yet".
        """
        conds = [c for c in (schedule.get("tag_conditions") or [])
                 if isinstance(c, dict)]
        if not conds:
            return False
        try:
            ok, _ = _evaluate_tag_conditions(
                conds, schedule.get("condition_logic") or "all", self.live_lookup)
        except Exception:
            # Unreadable: assume it is still high rather than firing a report
            # we cannot justify.
            return True
        return bool(ok)

    def _consider_cycle(self, schedule: dict[str, Any], now_utc: datetime) -> None:
        """Open a cycle on the start condition, close and report on the stop.

        The open cycle lives in the schedule row (`cycle_started_utc`), so a
        restart part-way through still closes against the real start time
        instead of inventing one.

        2026-11-16: one tick may now open AND close a cycle, and open the
        next - a test shorter than the 15 s tick, or two tests inside one
        tick, used to merge or be missed. Each step continues from the last
        boundary it found (a start or a stop time), up to a bound, so a tick
        stays short.
        """
        sched_id = str(schedule.get("id") or "")
        now_stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        if schedule.get("start_edge") is not False and self.history_lookup:
            # 2026-11-17: the edge-driven machine. Level mode (start_edge
            # false) keeps the exact path below it always had.
            try:
                self._consider_cycle_edges(dict(schedule), now_utc)
                self._last_seen[sched_id] = now_stamp
                return
            except Exception as exc:          # never let a trigger stop a report
                logger.exception("report cycle: edge machine failed (%s); using the step path this tick", exc)
        since = self._last_seen.get(sched_id) or ""
        self._last_seen[sched_id] = now_stamp
        schedule = dict(schedule)
        for _step in range(20):
            boundary = self._consider_cycle_once(schedule, now_stamp, since)
            if not boundary or boundary >= now_stamp or boundary == since:
                return
            since = boundary

    # ------------------------------------------------------------ edges
    #: A tag with no stored reading for this long is read LIVE (collection
    #: gated on the test: the 0 that ends it is never stored).
    LIVE_WHEN_STALE_S = 10.0

    def _cond_true(self, conds: list[dict], held: dict, logic: str) -> bool:
        results = []
        for c in conds:
            key = (str(c.get("gateway_id") or "").strip(), str(c.get("tag_name") or c.get("tag") or "").strip())
            v = held.get(key)
            try:
                num = float(v)
                threshold = float(c.get("value"))
            except (TypeError, ValueError):
                results.append(False)
                continue
            second = c.get("value2")
            try:
                second = float(second) if second not in (None, "") else None
            except (TypeError, ValueError):
                second = None
            results.append(bool(_compare(num, str(c.get("operator") or ">="), threshold, second)))
        if not results:
            return False
        return any(results) if str(logic or "all").strip().lower() == "any" else all(results)

    def _cycle_moments(self, schedule: dict[str, Any], st: dict[str, Any], now_utc: datetime):
        """(ts, start_true, stop_true) for every stored reading of the start
        and stop tags since the last one seen, sample-and-hold; plus one
        LIVE moment when a tag has nothing stored lately."""
        now_stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        start_c = [c for c in (schedule.get("tag_conditions") or []) if isinstance(c, dict)
                   and str(c.get("tag_name") or c.get("tag") or "").strip()]
        stop_c = [c for c in (schedule.get("stop_conditions") or []) if isinstance(c, dict)
                  and str(c.get("tag_name") or c.get("tag") or "").strip()]
        logic_a = schedule.get("condition_logic") or "all"
        logic_b = schedule.get("stop_condition_logic") or "all"
        keys: list[tuple[str, str]] = []
        for c in start_c + stop_c:
            k = (str(c.get("gateway_id") or "").strip(), str(c.get("tag_name") or c.get("tag") or "").strip())
            if k not in keys:
                keys.append(k)
        held: dict = dict(st.get("held") or {})
        key_ts: dict = dict(st.get("key_ts") or {})
        first = not st.get("seen")
        since = st.get("last_ts") or ""
        sched_id = str(schedule.get("id") or "")
        if first and self._last_seen.get(sched_id):
            # the scheduler already looked at this schedule (the step path):
            # replay what happened since then, edges included
            since = self._last_seen[sched_id]
            first = False
        elif first:
            since = (now_utc - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        watch_since = st.get("watch_since") or now_stamp
        events: list[tuple[str, tuple[str, str], Any]] = []
        for k in keys:
            try:
                rows = self.history_lookup(k[0], k[1], since, now_stamp) or []
            except Exception:
                rows = []
            for ts, value in rows:
                ts = str(ts)
                # never a reading from after "now" (a lookup that ignores its
                # bounds, or a clock ahead): it is next tick's news
                if (ts > since or first) and ts[:19] <= now_stamp:
                    events.append((ts, k, value))
        events.sort(key=lambda e: e[0])
        moments: list[tuple[str, bool, bool | None]] = []
        last_ts = st.get("last_ts") or ""
        i = 0
        while i < len(events):
            ts = events[i][0]
            while i < len(events) and events[i][0] == ts:
                held[events[i][1]] = events[i][2]
                key_ts[events[i][1]] = ts
                i += 1
            last_ts = max(last_ts, ts)
            if not first:
                moments.append((ts, self._cond_true(start_c, held, logic_a),
                                self._cond_true(stop_c, held, logic_b) if stop_c else None))
        # live, for a tag whose readings are not being stored right now
        changed = False
        for k in keys:
            stale = True
            # a tag with NO stored reading yet is read live only once it has
            # been watched for a while: its first stored reading may simply
            # not be written yet, and a live value taken now would be stamped
            # earlier than the change it shows
            t_last = key_ts.get(k) or (None if watch_since >= now_stamp else watch_since)
            if t_last is None:
                continue
            if t_last:
                try:
                    age = (now_utc - datetime.strptime(str(t_last)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                           .replace(tzinfo=timezone.utc)).total_seconds()
                    stale = age > self.LIVE_WHEN_STALE_S
                except Exception:
                    stale = True
            if not stale:
                continue
            try:
                v = self.live_lookup(k[0], k[1])
            except Exception:
                v = None
            if v is not None and held.get(k) != v:
                held[k] = v
                changed = True
        if first or changed:
            moments.append((max(now_stamp, last_ts), self._cond_true(start_c, held, logic_a),
                            self._cond_true(stop_c, held, logic_b) if stop_c else None))
        st["held"], st["key_ts"], st["last_ts"], st["seen"] = held, key_ts, last_ts, True
        st["watch_since"] = watch_since
        return moments, bool(stop_c)

    def _capture_report_id(self, schedule: dict[str, Any]) -> str:
        report_id = ""
        id_tag = str(schedule.get("id_tag") or "").strip()
        if not id_tag:
            return ""
        id_gateway = str(schedule.get("id_gateway") or "").strip()
        if not id_gateway:
            first = (schedule.get("tag_conditions") or [{}])[0]
            id_gateway = str((first or {}).get("gateway_id") or "").strip()
        try:
            txt = self.live_text_lookup(id_gateway, id_tag) if self.live_text_lookup else None
            if txt not in (None, ""):
                return str(txt).strip()
            value = self.live_lookup(id_gateway, id_tag)
            if value is None:
                return ""
            try:
                f = float(value)
                return (str(int(f)) if f == int(f) else str(f)).strip()
            except (TypeError, ValueError):
                return str(value).strip()
        except Exception:
            return report_id

    def _consider_cycle_edges(self, schedule: dict[str, Any], now_utc: datetime) -> None:
        """2026-11-17: a cycle opens on the start turning true, ends per the
        schedule's `cycle_end` (start going false / the stop turning true /
        the next start pulse) and `start_drop` (ignore / end / discard).
        See services/trigger_cycles.py. A record is listed as "collecting"
        from the start; the close fills it in; a discard cancels it."""
        from app.services import trigger_cycles as _tc
        sched_id = str(schedule.get("id") or "")
        now_stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        if not hasattr(self, "_edge_state"):
            self._edge_state = {}
            self._cycle_records = {}
        st = self._edge_state.get(sched_id) or dict(_tc.new_state())
        started = str(schedule.get("cycle_started_utc") or "").strip()
        if started and not st.get("open"):
            # a cycle open in the store (a restart part-way through): carry
            # on; a stop already true must still go false and back to true
            st.update(open=True, opened_at=started, stop_armed=False, start_low_seen=False)
        if not started and st.get("open"):
            st.update(open=False, opened_at="")
        if not st.get("seen") and not started:
            # first look after a start-up: a record still "collecting" has no
            # open cycle behind it any more
            left = self.runner.find_cycle_records(schedule) if hasattr(self.runner, "find_cycle_records") else {}
            if left:
                self.runner.cancel_cycle_records(schedule, left, "The cycle was not open when the app started again; no report was made from it.")
        moments, has_stop = self._cycle_moments(schedule, st, now_utc)
        end_mode = _tc.end_mode_for(schedule.get("cycle_end"), has_stop)
        drop = _tc.drop_action_for(schedule.get("start_drop"))
        events, st2 = _tc.walk(moments, st, end_mode, drop)
        for k in ("held", "key_ts", "last_ts", "seen", "watch_since"):
            st2[k] = st.get(k)
        self._edge_state[sched_id] = st2
        for kind, ts in events:
            if kind == "open":
                stamp = str(ts)[:23]
                report_id = self._capture_report_id(schedule)
                self.store.set_cycle_state(sched_id, stamp, report_id, segments_json="[]")
                schedule["cycle_started_utc"] = stamp
                schedule["cycle_report_id"] = report_id
                schedule["cycle_segments"] = []
                recs = {}
                try:
                    recs = self.runner.open_cycle_records(schedule, stamp, report_id) if hasattr(self.runner, "open_cycle_records") else {}
                except Exception:
                    recs = {}
                self._cycle_records[sched_id] = recs
                logger.info("report cycle opened: schedule=%s at %s id=%r (%s)", sched_id, stamp, report_id, end_mode)
            elif kind == "discard":
                started_now = str(schedule.get("cycle_started_utc") or "")
                recs = self._cycle_records.pop(sched_id, None) or (
                    self.runner.find_cycle_records(schedule) if hasattr(self.runner, "find_cycle_records") else {})
                self.store.set_cycle_state(sched_id, "", "", segments_json="[]")
                schedule["cycle_started_utc"] = ""
                schedule["cycle_report_id"] = ""
                schedule["cycle_segments"] = []
                if hasattr(self.runner, "cancel_cycle_records"):
                    self.runner.cancel_cycle_records(
                        schedule, recs, "The start went false at %s before the end trigger; the cycle "
                        "was discarded (schedule option). No report was made from it." % str(ts)[:19])
                logger.info("report cycle discarded: schedule=%s %s..%s", sched_id, started_now, ts)
            elif kind == "close":
                started_now = str(schedule.get("cycle_started_utc") or "")
                stamp = str(ts)[:23]
                if not started_now or stamp < started_now:
                    continue
                segments = self._track_segment(schedule, sched_id, started_now, stamp)
                segments = self._close_segments(segments, stamp)
                logger.info("report cycle closed: schedule=%s %s..%s (%d segment(s))",
                            sched_id, started_now, stamp, len(segments))
                self.store.set_cycle_state(sched_id, "", "", segments_json="[]")
                recs = self._cycle_records.pop(sched_id, None) or (
                    self.runner.find_cycle_records(schedule) if hasattr(self.runner, "find_cycle_records") else {})
                email_settings = None
                try:
                    email_settings = self.email_settings_lookup()
                except Exception:
                    email_settings = None
                try:
                    kwargs = {"cycle_records": recs} if recs else {}
                    result = self.runner.run(
                        schedule, triggered_by="cycle", email_settings=email_settings,
                        window=(started_now, stamp), segments=segments,
                        report_id=str(schedule.get("cycle_report_id") or ""), **kwargs)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                    if recs and hasattr(self.runner, "cancel_cycle_records"):
                        for gid in recs.values():
                            try:
                                self.store.update_generated_render(gid, render_state="failed",
                                                                   render_error="Report run failed: %s" % exc)
                            except Exception:
                                pass
                try:
                    self.store.mark_schedule_run(
                        schedule_id=sched_id, last_run_utc=stamp,
                        status="success" if result.get("ok") else "error",
                        error=None if result.get("ok") else str(result.get("error") or ""),
                        next_run_utc=None)
                except Exception:
                    pass
                schedule["cycle_started_utc"] = ""
                schedule["cycle_report_id"] = ""
                schedule["cycle_segments"] = []
        # still open at the end of this tick: follow the test / cycle tag
        # (segments) up to now - once, at the right time
        if str(schedule.get("cycle_started_utc") or ""):
            self._track_segment(schedule, sched_id, str(schedule.get("cycle_started_utc")), now_stamp)

    def _consider_cycle_once(self, schedule: dict[str, Any], stamp: str, since: str) -> str:
        """One open-or-close step from `since`. Returns the boundary it found
        (the start or stop time, in the store's stamp format) so the caller
        can continue from there, or "" when nothing happened."""
        sched_id = str(schedule.get("id") or "")
        started = str(schedule.get("cycle_started_utc") or "").strip()
        # start_edge=false is the explicit LEVEL mode kept from 2026-10-11:
        # "no new reading since the tick" counts as the start being false and
        # the cycle re-opens on the next tick. It keeps that exact behaviour.
        legacy_level = schedule.get("start_edge") is False
        start_ok, start_at = self._conditions_hit(
            schedule.get("tag_conditions") or [],
            schedule.get("condition_logic") or "all", since, stamp,
            live_fallback=not legacy_level)

        if not started:
            # 2026-10-11, reported: "when a condition is start trigger =1 but
            # this keeps on until it is off". The cycle closed while the start
            # bit was STILL high, so the very next tick found a satisfying
            # reading in history and opened another cycle. Rising edge: once a
            # cycle has closed, the start set must be seen FALSE before it can
            # open another (start_edge=false restores level triggering).
            if self._await_low.get(sched_id):
                if not self._start_dropped_since(schedule, since, stamp):
                    return ""
                self._await_low.pop(sched_id, None)
                # The bit dropped - and may have risen again inside this same
                # tick (a short part): the start hit found above counts only
                # when it is AFTER the drop, which `since` guarantees for the
                # next step; for this step, require a hit newer than the close.
            if not start_ok:
                return ""
            if not legacy_level and since and start_at and start_at <= since:
                return ""             # a hit older than the last boundary is not a new rise
            # Stamp the cycle with the READING's time, not the tick's.
            stamp = start_at or stamp
            # Capture the identifier NOW: a serial or batch number read at the
            # end of a cycle may already have moved on to the next part.
            report_id = ""
            id_tag = str(schedule.get("id_tag") or "").strip()
            if id_tag:
                id_gateway = str(schedule.get("id_gateway") or "").strip()
                if not id_gateway:
                    first = (schedule.get("tag_conditions") or [{}])[0]
                    id_gateway = str((first or {}).get("gateway_id") or "").strip()
                try:
                    # 2026-11-16: text first (a part number may be text), and
                    # a whole number without ".0".
                    txt = self.live_text_lookup(id_gateway, id_tag) if self.live_text_lookup else None
                    if txt not in (None, ""):
                        report_id = str(txt).strip()
                    else:
                        value = self.live_lookup(id_gateway, id_tag)
                        if value is None:
                            report_id = ""
                        else:
                            try:
                                f = float(value)
                                report_id = (str(int(f)) if f == int(f) else str(f)).strip()
                            except (TypeError, ValueError):
                                report_id = str(value).strip()
                except Exception:
                    report_id = ""
            self.store.set_cycle_state(sched_id, stamp, report_id, segments_json="[]")
            schedule["cycle_started_utc"] = stamp
            schedule["cycle_report_id"] = report_id
            schedule["cycle_segments"] = []
            logger.info("report cycle opened: schedule=%s at %s id=%r",
                     sched_id, stamp, report_id)
            return stamp

        # While the cycle is open, watch the segment tag. Each new value closes
        # the previous sub-period and opens the next.
        segments = self._track_segment(schedule, sched_id, started, stamp)

        stop_conditions = schedule.get("stop_conditions") or []
        if not stop_conditions:
            # No stop condition means the START condition going false closes it.
            # 2026-11-16: from history when there is any - the reading's time -
            # else the tick's.
            if legacy_level or not self.history_lookup:
                stop_ok, stop_at = (not start_ok), stamp
            else:
                dropped = self._start_dropped_since(schedule, max(since, started), stamp)
                stop_ok, stop_at = dropped, (self._first_false_at(schedule, max(since, started), stamp) or stamp)
        else:
            stop_ok, stop_at = self._conditions_hit(
                stop_conditions, schedule.get("stop_condition_logic") or "all",
                max(since, started), stamp)
        if not stop_ok:
            return ""
        # Close on the reading's own timestamp, so the window is the cycle.
        stamp = stop_at or stamp
        if stamp < started:
            return ""

        segments = self._close_segments(segments, stamp)
        # Arm the rising-edge guard unless the schedule opted out.
        if schedule.get("start_edge") is not False:
            self._await_low[sched_id] = True
        logger.info("report cycle closed: schedule=%s %s..%s (%d segment(s))",
                    sched_id, started, stamp, len(segments))
        self.store.set_cycle_state(sched_id, "", "", segments_json="[]")
        email_settings = None
        try:
            email_settings = self.email_settings_lookup()
        except Exception:
            email_settings = None
        result = self.runner.run(
            schedule, triggered_by="cycle", email_settings=email_settings,
            window=(started, stamp),
            segments=segments,
            report_id=str(schedule.get("cycle_report_id") or ""))
        try:
            self.store.mark_schedule_run(
                schedule_id=sched_id, last_run_utc=stamp,
                status="success" if result.get("ok") else "error",
                error=None if result.get("ok") else str(result.get("error") or ""),
                next_run_utc=None)
        except Exception:
            pass
        schedule["cycle_started_utc"] = ""
        schedule["cycle_report_id"] = ""
        schedule["cycle_segments"] = []
        return stamp

    def _first_false_at(self, schedule: dict[str, Any], since: str, now_stamp: str) -> str:
        """The first reading since `since` at which the start set is false
        (logic ALL: any condition false), or "" when none / no history."""
        conds = [c for c in (schedule.get("tag_conditions") or []) if isinstance(c, dict)]
        if not conds or not self.history_lookup:
            return ""
        firsts: list[str] = []
        for c in conds:
            gw = str(c.get("gateway_id") or "").strip()
            tag = str(c.get("tag_name") or c.get("tag") or "").strip()
            if not tag:
                continue
            try:
                rows = self.history_lookup(gw, tag, since, now_stamp) or []
            except Exception:
                rows = []
            try:
                threshold = float(c.get("value"))
            except (TypeError, ValueError):
                continue
            second = c.get("value2")
            try:
                second = float(second) if second not in (None, "") else None
            except (TypeError, ValueError):
                second = None
            for ts, value in rows:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                if not _compare(num, str(c.get("operator") or ">="), threshold, second):
                    firsts.append(str(ts))
                    break
        if not firsts:
            return ""
        logic = str(schedule.get("condition_logic") or "all").strip().lower()
        return min(firsts) if logic == "all" else max(firsts)

    def _segment_streams(self, schedule: dict[str, Any]) -> list[dict[str, str]]:
        """The tags that divide a run, one stream each.

        A TCU functional test has three levels at once - which test is running,
        which cycle of it, and which phase within that cycle - so one tag is
        not enough. `segment_tags` is a list of {name, tag, gateway}; the older
        single `segment_tag` is still honoured as one stream.
        """
        out: list[dict[str, str]] = []
        for item in (schedule.get("segment_tags") or []):
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or item.get("tag_name") or "").strip()
            if not tag:
                continue
            out.append({
                "name": str(item.get("name") or "segment").strip() or "segment",
                "tag": tag,
                "gateway": str(item.get("gateway") or item.get("gateway_id") or "").strip(),
            })
        legacy = str(schedule.get("segment_tag") or "").strip()
        if legacy and not any(x["tag"] == legacy for x in out):
            out.append({
                "name": str(schedule.get("segment_name") or "segment").strip() or "segment",
                "tag": legacy,
                "gateway": str(schedule.get("segment_gateway") or "").strip(),
            })
        return out

    def _track_segment(self, schedule: dict[str, Any], sched_id: str,
                       started: str, stamp: str) -> list[dict[str, Any]]:
        """Divide the open run into sub-periods by watching tag values.

        Each stream is independent: a phase change closes the phase segment
        without disturbing the cycle or the test around it. Returns the segment
        list as it now stands; the open entry of each stream has no to_utc yet.

        Driven by a value CHANGE rather than by a second pair of conditions:
        the process already carries Test_Step / Cycle_Number / Phase_Name, and
        "the second one is repeated based on the number of cycles defined in
        the process" is exactly those tags counting on.
        """
        segments = list(schedule.get("cycle_segments") or [])
        streams = self._segment_streams(schedule)
        if not streams:
            # No segment tag: the whole run is one segment, so a summary table
            # still has a row and nothing else behaves differently.
            if not segments:
                segments = [{"stream": "run", "name": "run", "label": "Run",
                             "value": "run", "index": 1,
                             "from_utc": started, "to_utc": ""}]
                self._save_segments(schedule, sched_id, started, segments)
            return segments

        default_gateway = str(
            ((schedule.get("tag_conditions") or [{}])[0] or {}).get("gateway_id") or "").strip()
        changed = False
        for st in streams:
            try:
                raw = self.live_lookup(st["gateway"] or default_gateway, st["tag"])
            except Exception:
                raw = None
            if raw is None:
                continue
            value = str(raw).strip()
            # The open segment OF THIS STREAM only.
            current = None
            for sg in reversed(segments):
                if str(sg.get("stream") or "") == st["name"]:
                    current = sg
                    break
            if current is not None and str(current.get("value") or "") == value:
                continue                      # same sub-period, nothing to do
            if current is not None and not current.get("to_utc"):
                current["to_utc"] = stamp
            index = 1 + sum(1 for sg in segments
                            if str(sg.get("stream") or "") == st["name"]
                            and str(sg.get("value") or "") == value)
            segments.append({
                "stream": st["name"], "name": st["name"],
                "label": value or st["name"], "value": value, "index": index,
                "from_utc": stamp if current is not None else started,
                "to_utc": "",
            })
            changed = True
            logger.info("report segment opened: schedule=%s %s/%s=%s (#%d) at %s",
                        sched_id, st["name"], st["tag"], value, index, stamp)
        if changed:
            self._save_segments(schedule, sched_id, started, segments)
        return segments

    def _save_segments(self, schedule: dict[str, Any], sched_id: str,
                       started: str, segments: list[dict[str, Any]]) -> None:
        self.store.set_cycle_state(
            sched_id, started, str(schedule.get("cycle_report_id") or ""),
            segments_json=json.dumps(segments))

    @staticmethod
    def _close_segments(segments: list[dict[str, Any]], stamp: str) -> list[dict[str, Any]]:
        """Seal the open segment against the cycle's end."""
        # Every stream has its own open segment; all of them end with the run.
        out = [dict(sg) for sg in (segments or [])]
        for sg in out:
            if not sg.get("to_utc"):
                sg["to_utc"] = stamp
        return out

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

        # CYCLE: start condition opens a window, stop condition closes it and
        # produces one report covering exactly that window. Requested
        # 2026-09-29: "everytime we have a new start and end time a new report
        # is generated automatically".
        #
        # Edge-driven, not level-driven: a level test fires every minute the
        # condition stays true, which on a cycle that runs for an hour is sixty
        # identical reports.
        if mode == "cycle":
            self._consider_cycle(schedule, now_utc)
            return

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
