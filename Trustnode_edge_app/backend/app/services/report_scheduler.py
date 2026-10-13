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


def _cycle_file_name(schedule: dict[str, Any], template: dict[str, Any],
                     window: tuple[str, str] | None, report_id: str) -> str:
    """The operator's naming pattern, resolved. Empty keeps the uuid name."""
    from app.services.report_renderer import resolve_output_target
    # ONE resolver for both boxes. "Save to" used to win outright and discard
    # "File name", and a folder in Save to was then treated as the file
    # itself - so every run overwrote one PDF (2026-10-07).
    return resolve_output_target(
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
                                email_settings, window, segments, report_id)
            results.append(out)
        if len(results) == 1:
            return results[0]
        return {"ok": all(r.get("ok") for r in results), "reports": results,
                "generated": (results[0] or {}).get("generated")}

    def _run_one(self, schedule, template_id, tenant_id, triggered_by,
                 email_settings, window, segments, report_id):
        template = self.store.get_template(template_id, tenant_id=tenant_id)
        if not template:
            return {
                "ok": False,
                "error": f"Template {template_id!r} not found",
            }
        try:
            # A cycle report covers the window between its start and stop
            # conditions, and is named from the identifier captured at the
            # start. Both are optional: without them this renders exactly as
            # before.
            path, byte_count, sha = self.render(
                template, window=window, segments=segments,
                file_name=_cycle_file_name(schedule, template, window, report_id))
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
        history_lookup: Callable[[str, str, str, str], list] | None = None,
    ) -> None:
        self.runner = runner
        self.store = reports_store
        self.live_lookup = live_lookup
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

    def _conditions_hit(self, conditions: list, logic: str, since: str,
                        now_stamp: str) -> tuple[bool, str]:
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
                if str(logic or "all").strip().lower() == "all":
                    return False, ""          # one unmet condition ends it
                continue
            firsts.append(hit_at)
        if not firsts:
            return False, ""
        # ALL -> satisfied once the LAST of them came true; ANY -> the first.
        when = max(firsts) if str(logic or "all").strip().lower() == "all" else min(firsts)
        return True, when

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
        """
        sched_id = str(schedule.get("id") or "")
        stamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        started = str(schedule.get("cycle_started_utc") or "").strip()

        since = self._last_seen.get(sched_id) or ""
        self._last_seen[sched_id] = stamp
        start_ok, start_at = self._conditions_hit(
            schedule.get("tag_conditions") or [],
            schedule.get("condition_logic") or "all", since, stamp)

        if not started:
            # 2026-10-11, reported: "when a condifition is start rigger =1 but
            # this keep on until is off". The cycle closed (its stop condition
            # fired, or a stop-less cycle saw the start go false for one
            # reading) while the start bit was STILL high, so the very next
            # 15 s tick found a satisfying reading in history and opened
            # another cycle - one physical run generating a report every
            # quarter minute until the operator noticed.
            #
            # Rising edge: once a cycle has closed, the start set must be seen
            # FALSE before it can open another. Set start_edge=false on the
            # schedule for the old level-triggered behaviour.
            if self._await_low.get(sched_id):
                if self._start_true_now(schedule):
                    return
                self._await_low.pop(sched_id, None)
                # The bit has only just dropped, so nothing read during this
                # tick can be the next rising edge. Wait for the next one.
                return
            if not start_ok:
                return
            # Stamp the cycle with the READING's time, not the tick's.
            stamp = start_at or stamp
            # Capture the identifier NOW: a serial or batch number read at the
            # end of a cycle may already have moved on to the next part.
            report_id = ""
            id_tag = str(schedule.get("id_tag") or "").strip()
            if id_tag:
                # live_lookup is (gateway, tag) - the same contract the
                # condition evaluator uses. An id tag may name its gateway
                # explicitly, or come from the same gateway as the start
                # condition.
                id_gateway = str(schedule.get("id_gateway") or "").strip()
                if not id_gateway:
                    first = (schedule.get("tag_conditions") or [{}])[0]
                    id_gateway = str((first or {}).get("gateway_id") or "").strip()
                try:
                    value = self.live_lookup(id_gateway, id_tag)
                    report_id = "" if value is None else str(value).strip()
                except Exception:
                    report_id = ""
            # A new run starts with no segments; the first read of the
            # segment tag opens the first one on the next tick.
            self.store.set_cycle_state(sched_id, stamp, report_id, segments_json="[]")
            logger.info("report cycle opened: schedule=%s at %s id=%r",
                     sched_id, stamp, report_id)
            return

        # While the cycle is open, watch the segment tag. Each new value closes
        # the previous sub-period and opens the next - that is what turns "the
        # second test repeated N times" into N windows the report can address.
        segments = self._track_segment(schedule, sched_id, started, stamp)

        stop_conditions = schedule.get("stop_conditions") or []
        if not stop_conditions:
            # No stop condition means the START condition going false closes it.
            stop_ok, stop_at = (not start_ok), stamp
        else:
            stop_ok, stop_at = self._conditions_hit(
                stop_conditions, schedule.get("stop_condition_logic") or "all",
                since, stamp)
        if not stop_ok:
            return
        # Close on the reading's own timestamp, so the window is the cycle.
        stamp = stop_at or stamp

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
