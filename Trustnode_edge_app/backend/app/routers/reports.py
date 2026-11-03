"""HTTP API for the reporting module.

Three resource groups:

  * Templates:        /api/reports/templates ...
  * Schedules:        /api/reports/schedules ...
  * Generated reports /api/reports/generated ...

The Reporting UI calls /templates to save/load layouts. The Scheduled Reports
UI uses /schedules to wire triggers and recipients. Both pages list previously
generated PDFs through /generated, and the file viewer streams the PDF bytes
via /generated/{id}/file.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, PlainTextResponse

from app.state import report_runner, reports_store, scheduler_email_settings_holder
from app.tenant import get_current_tenant

router = APIRouter(prefix="/api/reports", tags=["reports"])

# Background report runs: {job_id: {state, result|error, started}}.
# Bounded, because a dict that only grows is a leak with a nicer name.
import threading as _threading_rpt                          # noqa: E402
import time as _time_rpt                                    # noqa: E402
import uuid as _uuid_rpt                                    # noqa: E402

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = _threading_rpt.Lock()
_JOBS_KEEP = 40


def _start_run_job(sch: dict, email_settings: Any) -> str:
    job_id = _uuid_rpt.uuid4().hex[:12]
    job = {"state": "running", "started": _time_rpt.time(),
           "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}

    def _work():
        try:
            res = report_runner.run(sch, triggered_by="manual",
                                    email_settings=email_settings)
            if res.get("ok"):
                job["result"] = {k: v for k, v in res.items() if k != "ok"}
                job["state"] = "done"
            else:
                job["error"] = str(res.get("error") or "Run failed")
                job["state"] = "error"
        except Exception as exc:                            # noqa: BLE001
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
            job["state"] = "error"

    with _JOBS_LOCK:
        _JOBS[job_id] = job
        # Keep the most recent few; a run nobody collected is still worth
        # seeing for a while, but not for ever.
        if len(_JOBS) > _JOBS_KEEP:
            for old in sorted(_JOBS, key=lambda k: _JOBS[k].get("started", 0))[:-_JOBS_KEEP]:
                _JOBS.pop(old, None)
    _threading_rpt.Thread(target=_work, name="tn-report-run-%s" % job_id,
                          daemon=True).start()
    return job_id


def _username_from_request(request: Request) -> str:
    payload = getattr(request.state, "user_payload", None) or {}
    return str(payload.get("sub") or "").strip() or "system"


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
@router.get("/templates")
def list_templates() -> dict[str, Any]:
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "templates": reports_store.list_templates(),
    }


@router.get("/overview")
def reports_overview(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    """Everything the Reports pages show, in ONE read.

    2026-11-03: the pages fired four requests on every mount and every 15 s.
    The browser gives the edge six connections, so on a busy page the
    fourth read could expire client-side with nothing wrong here. One read,
    on one read-only handle, is one socket and one place to measure."""
    settings = None
    try:
        settings = scheduler_email_settings_holder.get()
    except Exception:
        settings = None
    from app.state import _is_any_gateway_running
    try:
        any_running = bool(_is_any_gateway_running())
    except Exception:
        any_running = False
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "templates": reports_store.list_templates(),
        "schedules": reports_store.list_schedules(),
        "generated": reports_store.list_generated(limit=limit),
        "scheduler": {
            "email_transport_configured": bool(settings),
            "any_gateway_running": any_running,
        },
    }


@router.get("/templates/{template_id}")
def get_template(template_id: str) -> dict[str, Any]:
    tpl = reports_store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True, "template": tpl}


@router.post("/templates")
def create_template(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    saved = reports_store.upsert_template(
        payload,
        created_by=_username_from_request(request) if request else None,
    )
    return {"ok": True, "template": saved}


@router.put("/templates/{template_id}")
def update_template(template_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    payload = {**payload, "id": template_id}
    saved = reports_store.upsert_template(
        payload,
        created_by=_username_from_request(request) if request else None,
    )
    return {"ok": True, "template": saved}


@router.delete("/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, Any]:
    ok = reports_store.delete_template(template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True}


# Portable bundle format shared by export/import so users can move templates
# between TrustNode edges. Keep this stable; bump `bundle_version` if you
# make breaking changes to the schema.
_BUNDLE_VERSION = 1
_BUNDLE_KIND = "trustnode.report-template-bundle"


@router.get("/templates/{template_id}/export")
def export_template(template_id: str) -> dict[str, Any]:
    tpl = reports_store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return {
        "kind": _BUNDLE_KIND,
        "bundle_version": _BUNDLE_VERSION,
        "exported_utc": datetime.now(timezone.utc).isoformat(),
        "templates": [
            {
                "name": tpl.get("name"),
                "description": tpl.get("description"),
                "definition": tpl.get("definition") or {},
            }
        ],
    }


@router.get("/templates-export-all")
def export_all_templates() -> dict[str, Any]:
    rows = reports_store.list_templates()
    return {
        "kind": _BUNDLE_KIND,
        "bundle_version": _BUNDLE_VERSION,
        "templates": [
            {
                "name": r.get("name"),
                "description": r.get("description"),
                "definition": r.get("definition") or {},
            }
            for r in rows
        ],
    }


@router.post("/templates/import")
def import_templates(
    payload: dict[str, Any] = Body(...),
    request: Request = None,
) -> dict[str, Any]:
    """Accept a bundle (or a single template) and persist it as new template(s).

    Behavior:
      * Always allocates new IDs so an imported template never clobbers an
        existing one on the target edge.
      * If a template with the same name already exists, the imported copy is
        renamed with a numeric suffix to keep both.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")

    items: list[dict[str, Any]] = []
    # Accept either a bundle ({templates: [...]}) or a single template
    # ({name, definition}) for convenience.
    if isinstance(payload.get("templates"), list):
        for entry in payload["templates"]:
            if isinstance(entry, dict):
                items.append(entry)
    elif payload.get("name") or payload.get("definition"):
        items.append(payload)
    else:
        raise HTTPException(status_code=400, detail="No templates found in payload")

    existing_names = {str(r.get("name") or "").strip().lower() for r in reports_store.list_templates()}
    created_by = _username_from_request(request) if request else None
    imported: list[dict[str, Any]] = []
    for entry in items:
        raw_name = str(entry.get("name") or "Untitled report").strip() or "Untitled report"
        name = raw_name
        suffix = 2
        while name.lower() in existing_names:
            name = f"{raw_name} ({suffix})"
            suffix += 1
        existing_names.add(name.lower())
        saved = reports_store.upsert_template(
            {
                "name": name,
                "description": str(entry.get("description") or ""),
                "definition": entry.get("definition") or {},
            },
            created_by=created_by,
        )
        imported.append(saved)
    return {"ok": True, "imported": imported, "count": len(imported)}


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
@router.get("/schedules")
def list_schedules() -> dict[str, Any]:
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "schedules": reports_store.list_schedules(),
    }


@router.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: str) -> dict[str, Any]:
    sch = reports_store.get_schedule(schedule_id)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True, "schedule": sch}


@router.post("/schedules")
def create_schedule(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    saved = reports_store.upsert_schedule(payload)
    return {"ok": True, "schedule": saved}


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    payload = {**payload, "id": schedule_id}
    saved = reports_store.upsert_schedule(payload)
    return {"ok": True, "schedule": saved}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str) -> dict[str, Any]:
    ok = reports_store.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True}


@router.post("/schedules/{schedule_id}/run")
def run_schedule_now(
    schedule_id: str,
    payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    sch = reports_store.get_schedule(schedule_id)
    if not sch:
        raise HTTPException(status_code=404, detail="Schedule not found")
    # Optional override of email settings (caller can post the active profile so
    # the report is delivered using the same SMTP/PHP transport the alarms use).
    email_settings = None
    force = False
    if isinstance(payload, dict):
        email_settings = payload.get("email_settings") or payload.get("email") or None
        # `force: true` overrides the require_gateway_running gate so an
        # operator can deliberately render an offline report (e.g. for tests).
        force = bool(payload.get("force", False))

    # Honour the "Only when a PLC gateway is running" gate by default so a
    # manual Run-now behaves the same way the scheduler does. The frontend
    # offers an explicit confirmation when the user wants to override it.
    if not force and bool(sch.get("require_gateway_running")):
        from app.state import _is_any_gateway_running
        try:
            running = bool(_is_any_gateway_running())
        except Exception:
            running = True
        if not running:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This schedule is set to run only when a PLC gateway is "
                    "running, and none is currently collecting. Pass force=true "
                    "to bypass this gate."
                ),
            )

    # 2026-10-31. Rendering a report took the whole request with it: the
    # browser held one of its SIX connections per origin for up to 60
    # seconds, and anything else the page asked for in that window queued
    # behind it and expired on its own 12-second timeout. The customer's log
    # shows exactly that - "Run failed" and "templates, scheduled reports
    # could not be loaded" landing in the SAME SECOND, with the heartbeat
    # healthy either side of it. The edge was never slow; it was busy
    # holding a socket.
    #
    # The run now happens on a worker thread and the request returns a job
    # id straight away. The page polls a cheap endpoint instead of holding a
    # connection open. The synchronous path is kept for the scheduler and
    # anything else that calls run() directly.
    if isinstance(payload, dict) and payload.get("background"):
        job_id = _start_run_job(sch, email_settings)
        return {"ok": True, "job_id": job_id, "state": "running"}

    result = report_runner.run(sch, triggered_by="manual", email_settings=email_settings)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Run failed")
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@router.get("/jobs/{job_id}")
def report_job(job_id: str) -> dict[str, Any]:
    """How is that run going? Cheap on purpose - the page polls it."""
    job = _JOBS.get(str(job_id or ""))
    if not job:
        raise HTTPException(status_code=404, detail="No such run")
    out = {"ok": True, "job_id": job_id, "state": job.get("state"),
           "started_utc": job.get("started_utc"),
           "seconds": round(_time_rpt.time() - float(job.get("started", 0)), 1)}
    if job.get("state") == "done":
        out["result"] = job.get("result") or {}
    if job.get("state") == "error":
        out["error"] = str(job.get("error") or "Run failed")
    return out


# --------------------------------------------------------------------------- #
# generated reports
# --------------------------------------------------------------------------- #
@router.get("/generated")
def list_generated(
    limit: int = Query(default=200, ge=1, le=2000),
    schedule_id: str = "",
    template_id: str = "",
) -> dict[str, Any]:
    rows = reports_store.list_generated(
        limit=limit,
        schedule_id=schedule_id or None,
        template_id=template_id or None,
    )
    return {"ok": True, "tenant_id": get_current_tenant(), "generated": rows}


@router.get("/generated/{generated_id}")
def get_generated(generated_id: str) -> dict[str, Any]:
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"ok": True, "generated": record}


def _render_into_record(template: dict[str, Any], *, triggered_by: str,
                        generated_id: str = "",
                        window: tuple[str, str] | None = None,
                        segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record first, PDF second - the same shape as a scheduled run.

    With `generated_id` the existing record is regenerated in place: it goes
    back to "pending", the PDF is rebuilt for the SAME window and segments
    the record was made with, and the row returns to "ready" or "failed"."""
    import time as _t
    from app.services.report_renderer import build_render_snapshot, render_template_to_pdf
    if generated_id:
        record = reports_store.update_generated_render(
            generated_id, render_state="pending") or {}
    else:
        record = reports_store.insert_generated({
            "template_id": template.get("id"),
            "template_name": template.get("name"),
            "triggered_by": triggered_by,
            "file_path": "", "file_name": "", "file_bytes": 0, "file_sha256": "",
            "meta": {"window_from": str((window or ("", ""))[0] or ""),
                     "window_to": str((window or ("", ""))[1] or ""),
                     "segments": list(segments or [])[:200],
                     "segment_count": len(segments or []),
                     "trigger": triggered_by, "render_state": "pending"},
        })
    gid = str(record.get("id") or generated_id or "")
    t0 = _t.perf_counter()
    try:
        path, byte_count, sha = render_template_to_pdf(template, window=window, segments=segments)
    except Exception as exc:
        return reports_store.update_generated_render(
            gid, render_state="failed", render_error=f"Render failed: {exc}") or record
    try:
        snapshot = build_render_snapshot(template, window=window, segments=segments)
    except Exception:
        snapshot = None
    return reports_store.update_generated_render(
        gid, render_state="ready", render_seconds=_t.perf_counter() - t0,
        file_path=str(path), file_name=path.name, file_bytes=byte_count,
        file_sha256=sha, render_data=snapshot) or record


def _record_window(record: dict[str, Any]) -> tuple[tuple[str, str] | None, list[dict[str, Any]]]:
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    w_from = str(meta.get("window_from") or "")
    w_to = str(meta.get("window_to") or "")
    segs = meta.get("segments") if isinstance(meta.get("segments"), list) else []
    return ((w_from, w_to) if (w_from and w_to) else None), segs


@router.get("/generated/{generated_id}/preview-data")
def generated_preview_data(generated_id: str) -> dict[str, Any]:
    """The report's own data - what was in it when it was generated - for
    the HTML preview. Works with or without the PDF. Falls back to
    re-reading the record's window from the historian when the record
    predates snapshots."""
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    data = reports_store.get_generated_render_data(generated_id)
    source = "snapshot"
    if data is None:
        template = reports_store.get_template(str(record.get("template_id") or ""))
        if not template:
            raise HTTPException(status_code=410, detail=(
                "This report has no stored data and its template no longer exists."))
        window, segs = _record_window(record)
        try:
            from app.services.report_renderer import build_render_snapshot
            data = build_render_snapshot(template, window=window, segments=segs)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Preview build failed: {exc}")
        source = "historian"
    return {"ok": True, "generated_id": generated_id, "source": source,
            "render_state": record.get("render_state"), "data": data}


@router.post("/generated/{generated_id}/regenerate")
def regenerate_generated(generated_id: str) -> dict[str, Any]:
    """Rebuild this report's PDF for the window it was generated with. Runs
    in the background; the row shows "pending" until it lands."""
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    template = reports_store.get_template(str(record.get("template_id") or ""))
    if not template:
        raise HTTPException(status_code=410, detail="The template this report was made from no longer exists.")
    window, segs = _record_window(record)
    tenant = get_current_tenant()
    job_id = _uuid_rpt.uuid4().hex[:12]
    job = {"state": "running", "started": _time_rpt.time(),
           "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}

    def _work():
        from app.tenant import set_current_tenant
        set_current_tenant(tenant)
        try:
            rec = _render_into_record(template, triggered_by=str(record.get("triggered_by") or "manual"),
                                      generated_id=generated_id, window=window, segments=segs)
            if rec.get("render_state") == "ready":
                job["result"] = {"generated": rec}
                job["state"] = "done"
            else:
                job["error"] = str(rec.get("render_error") or "Render failed")
                job["state"] = "error"
        except Exception as exc:                            # noqa: BLE001
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
            job["state"] = "error"

    with _JOBS_LOCK:
        _JOBS[job_id] = job
    _threading_rpt.Thread(target=_work, name="tn-report-regen-%s" % job_id, daemon=True).start()
    return {"ok": True, "job_id": job_id, "state": "running", "generated_id": generated_id}


@router.get("/generated/{generated_id}/file")
def download_generated(generated_id: str, inline: bool = Query(default=False)) -> Response:
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    path = Path(record.get("file_path") or "")
    disposition = "inline" if inline else "attachment"
    state = str(record.get("render_state") or "ready")
    if state == "pending" and not (record.get("file_path") and path.exists()):
        raise HTTPException(status_code=409, detail="This report is still being generated - its data can be previewed now; the PDF follows.")
    if state == "failed" and not (record.get("file_path") and path.exists()):
        raise HTTPException(status_code=409, detail="Generating this report's PDF failed: %s. Preview its data, or regenerate it."
                            % (record.get("render_error") or "unknown error"))
    if not path.exists():
        # Same rule as the Lite route: a cloud/web deployment never has the
        # edge's filesystem, so "not on disk" is its normal state. Serve the
        # uploaded copy rather than a 410 (2026-10-08).
        from app.services.reports_cloud_uploader import fetch_pdf_from_storage
        blob = fetch_pdf_from_storage(str(record.get("storage_path") or ""))
        if blob:
            name = record.get("file_name") or path.name or "report.pdf"
            return Response(
                content=blob, media_type="application/pdf",
                headers={"Content-Disposition": f'{disposition}; filename="{name}"'})
        raise HTTPException(
            status_code=410,
            detail="This report is not on this machine and has no cloud copy.")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        filename=record.get("file_name") or path.name,
        headers={"Content-Disposition": f'{disposition}; filename="{record.get("file_name") or path.name}"'},
    )


@router.post("/generated/{generated_id}/email")
def email_generated(generated_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Re-send (or first-send) a previously generated PDF as an email attachment.

    Body shape:
        {
          "recipients": ["a@b.com", ...],
          "subject": "...",            (optional)
          "html_body": "...",          (optional)
          "attach_pdf": bool,          (default true)
          "attach_csv": bool,          (default false)
          "attach_txt": bool,          (default false)
          "email_settings": {transport, smtp, php_mail}
        }

    CSV/TXT are regenerated from the *template* that produced this report so
    the user can re-export the same dataset without re-rendering the PDF.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    pdf_path = Path(record.get("file_path") or "")
    if not pdf_path.exists():
        raise HTTPException(status_code=410, detail="Report file is no longer on disk")
    recipients = payload.get("recipients") or []
    if not isinstance(recipients, list) or not recipients:
        raise HTTPException(status_code=400, detail="Recipients are required")
    settings = payload.get("email_settings") or payload.get("email") or {}
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="email_settings must be an object")

    want_pdf = bool(payload.get("attach_pdf", True))
    want_csv = bool(payload.get("attach_csv", False))
    want_txt = bool(payload.get("attach_txt", False))
    if not (want_pdf or want_csv or want_txt):
        raise HTTPException(status_code=400, detail="At least one attachment format must be selected.")

    import base64
    from app.routers.notifications import (
        EmailRequest, SMTPConfig, PHPMailConfig, EmailAttachment, send_email_request,
    )
    from app.services.report_renderer import build_template_dataset_files

    attachments: list[EmailAttachment] = []
    attachment_summary: list[str] = []
    if want_pdf:
        attachments.append(EmailAttachment(
            filename=record.get("file_name") or "report.pdf",
            content_b64=base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
            content_type="application/pdf",
        ))
        attachment_summary.append("PDF")

    if want_csv or want_txt:
        template_id = record.get("template_id") or ""
        template = reports_store.get_template(template_id) if template_id else None
        if not template:
            raise HTTPException(
                status_code=409,
                detail="Cannot rebuild CSV/TXT — the source template is missing.",
            )
        try:
            companions = build_template_dataset_files(
                template, output_dir=pdf_path.parent, base_name=pdf_path.stem,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Dataset export failed: {exc}")
        if want_csv:
            csv_path = companions.get("csv")
            if csv_path and Path(csv_path).exists():
                attachments.append(EmailAttachment(
                    filename=csv_path.name,
                    content_b64=base64.b64encode(csv_path.read_bytes()).decode("ascii"),
                    content_type="text/csv",
                ))
                attachment_summary.append("CSV")
        if want_txt:
            txt_path = companions.get("txt")
            if txt_path and Path(txt_path).exists():
                attachments.append(EmailAttachment(
                    filename=txt_path.name,
                    content_b64=base64.b64encode(txt_path.read_bytes()).decode("ascii"),
                    content_type="text/plain",
                ))
                attachment_summary.append("TXT")

    if not attachments:
        raise HTTPException(status_code=409, detail="No attachments produced for the selected formats.")

    transport = str(settings.get("transport") or "smtp").strip().lower()
    html_default = (
        f"<p>Trustnode report attached: <b>{record.get('file_name')}</b></p>"
        f"<p>Formats: {', '.join(attachment_summary)}</p>"
    )
    request = EmailRequest(
        transport="php_http" if transport == "php_http" else "smtp",
        smtp=SMTPConfig(**(settings.get("smtp") or {})),
        php_mail=PHPMailConfig(**(settings.get("php_mail") or {})) if settings.get("php_mail") else None,
        to=[str(x).strip() for x in recipients if str(x).strip()],
        subject=str(payload.get("subject") or f"Report: {record.get('template_name') or record.get('file_name')}"),
        html_body=str(payload.get("html_body") or html_default),
        text_body=str(payload.get("text_body") or f"Trustnode report ({', '.join(attachment_summary)}): {record.get('file_name')}"),
        attachments=attachments,
    )
    outcome = send_email_request(request)
    reports_store.update_generated_email_status(
        generated_id,
        status="sent" if outcome.ok else "failed",
        message=str(outcome.message or ""),
        recipients=list(outcome.recipients or []),
    )
    return {"ok": bool(outcome.ok), "message": outcome.message, "recipients": list(outcome.recipients or [])}


@router.delete("/generated/{generated_id}")
def delete_generated(generated_id: str) -> dict[str, Any]:
    ok = reports_store.delete_generated(generated_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# preview & ad-hoc rendering
# --------------------------------------------------------------------------- #
def _table_section_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Locate a single table-style section in the payload.

    The frontend posts either {section:{...}} to export that specific section,
    or {template:{...}, section_index:N} to pick by index.
    """
    section = payload.get("section")
    if isinstance(section, dict):
        return section
    template = payload.get("template") or {}
    defn = template.get("definition") if isinstance(template, dict) else None
    sections = (defn or {}).get("sections") if isinstance(defn, dict) else None
    idx = int(payload.get("section_index") or 0)
    if isinstance(sections, list) and 0 <= idx < len(sections):
        s = sections[idx]
        if isinstance(s, dict):
            return s
    return {}


@router.post("/export/csv")
def export_section_csv(payload: dict[str, Any] = Body(...)) -> Response:
    """Render the rows of a table/data section to CSV.

    Body shape: {section: {...}}  OR  {template: {...}, section_index: 3}
    """
    from app.services.report_renderer import build_data_table_rows
    section = _table_section_from_payload(payload)
    if not section:
        raise HTTPException(status_code=400, detail="No section provided")
    header, body = build_data_table_rows(section)
    if not header:
        # Empty table is still a valid file (headers only).
        header = ["Timestamp", "Value"]
        body = []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in body:
        writer.writerow(row)
    filename = f"{(section.get('title') or 'data').replace(' ', '_')[:60]}.csv"
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/export/txt")
def export_section_txt(payload: dict[str, Any] = Body(...)) -> Response:
    """Render the rows of a table/data section to a pipe-delimited TXT."""
    from app.services.report_renderer import build_data_table_rows
    section = _table_section_from_payload(payload)
    if not section:
        raise HTTPException(status_code=400, detail="No section provided")
    header, body = build_data_table_rows(section)
    if not header:
        header = ["Timestamp", "Value"]
        body = []
    lines = [" | ".join(str(h) for h in header)]
    for row in body:
        lines.append(" | ".join(str(c) for c in row))
    filename = f"{(section.get('title') or 'data').replace(' ', '_')[:60]}.txt"
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/templates/{template_id}/preview-data")
def template_preview_data(template_id: str) -> dict[str, Any]:
    """Render a saved template's sections as a JSON structure ready for
    an in-browser HTML preview (used by the Dashboard's Report Card
    widget in 'html_preview' mode). Same data the PDF renderer pulls
    — KPI values, chart series, table rows, pie slices — just expressed
    as JSON instead of reportlab flowables. Refreshes whenever the
    dashboard widget polls so the preview stays in sync with live data."""
    template = reports_store.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        from app.services.report_renderer import build_template_render_data
        data = build_template_render_data(template)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Preview build failed: {exc}")
    return {"ok": True, "template_id": template_id, "data": data}


@router.post("/templates/{template_id}/generate")
def generate_template(template_id: str) -> dict[str, Any]:
    """Render a saved template by id and store it as a generated report.
    Used by the dashboard's Report Card widget to give operators a
    one-click "Generate now" button. Returns the new generated record so
    the widget can immediately surface the file name and download link.
    """
    template = reports_store.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    record = _render_into_record(template, triggered_by="dashboard-manual")
    if record.get("render_state") == "failed":
        raise HTTPException(status_code=500, detail=f"Render failed: {record.get('render_error')}")
    return {"ok": True, "generated": record}


@router.post("/render")
def render_inline(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Render an arbitrary template (not necessarily saved) to PDF and store it
    as a "manual" generated report. Useful for the UI's "Preview as PDF" button.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    template = payload.get("template")
    if not isinstance(template, dict):
        raise HTTPException(status_code=400, detail="template payload is required")
    if not template.get("id"):
        template["id"] = "preview"
    record = _render_into_record(template, triggered_by="manual")
    if record.get("render_state") == "failed":
        raise HTTPException(status_code=500, detail=f"Render failed: {record.get('render_error')}")
    return {"ok": True, "generated": record}


# --------------------------------------------------------------------------- #
# scheduler diagnostics
# --------------------------------------------------------------------------- #
@router.get("/scheduler/status")
def scheduler_status() -> dict[str, Any]:
    settings = None
    try:
        settings = scheduler_email_settings_holder.get()
    except Exception:
        settings = None
    # Expose whether any PLC gateway is currently collecting so the Scheduled
    # Reports UI can show a live "gateways: stopped" badge next to the
    # require_gateway_running checkbox.
    from app.state import _is_any_gateway_running
    try:
        any_running = bool(_is_any_gateway_running())
    except Exception:
        any_running = False
    return {
        "ok": True,
        "email_transport_configured": bool(settings),
        "any_gateway_running": any_running,
    }


@router.post("/scheduler/email-settings")
def set_scheduler_email_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Push the active email profile into the scheduler so timer-fired reports
    can be emailed with attachments. The frontend invokes this whenever the
    user changes Email & Notifications settings.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    scheduler_email_settings_holder.set(payload)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# branding (company logo on PDF reports)
# --------------------------------------------------------------------------- #
import base64 as _b64
import os as _os
from app.state import app_store as _app_store


def _company_logo_dir() -> Path:
    """Return the data directory where the company logo lives.
    Matches the path the report renderer probes."""
    base = Path(
        _os.environ.get("TRUSTNODE_DATA_DIR")
        or (Path.home() / ".trustnode_edge" / "data")
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


@router.get("/branding/company-logo")
def get_company_logo() -> dict[str, Any]:
    """Return the current company-logo metadata + a data URL the frontend
    can use to preview the image. Designed to be cheap (a few KB at most;
    the logo is intentionally small for PDF embedding)."""
    base = _company_logo_dir()
    candidate = None
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
        path = base / f"company_logo{ext}"
        if path.is_file() and path.stat().st_size > 0:
            candidate = path
            break
    if candidate is None:
        return {"ok": True, "present": False}
    try:
        raw = candidate.read_bytes()
    except Exception:
        return {"ok": True, "present": False}
    ext = candidate.suffix.lower().lstrip(".")
    mime = "image/svg+xml" if ext == "svg" else f"image/{'jpeg' if ext == 'jpg' else ext}"
    data_url = f"data:{mime};base64,{_b64.b64encode(raw).decode('ascii')}"
    return {
        "ok": True,
        "present": True,
        "filename": candidate.name,
        "size_bytes": len(raw),
        "data_url": data_url,
    }


@router.put("/branding/company-logo")
def set_company_logo(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace the current company logo from a base64 data URL.
    Payload: { data_url: 'data:image/png;base64,XXXX' }.

    The image is saved as `company_logo.<ext>` under TRUSTNODE_DATA_DIR
    where the report renderer auto-picks it up. We also stamp the
    chosen path into app_settings.company_logo_path so the renderer
    can still find it when the operator uses a non-default location."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    data_url = str(payload.get("data_url") or "").strip()
    if not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="data_url must be a base64 data URL")
    try:
        header, b64data = data_url.split(",", 1)
        mime = header.split(":", 1)[1].split(";", 1)[0]
        raw = _b64.b64decode(b64data, validate=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"data_url decode failed: {exc}")
    if not raw:
        raise HTTPException(status_code=400, detail="empty image payload")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="logo file too large (>5 MB)")
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }
    ext = ext_map.get(mime.lower())
    if not ext:
        raise HTTPException(status_code=400, detail=f"unsupported image type: {mime}")
    base = _company_logo_dir()
    # Drop any previous variants so /branding/company-logo doesn't
    # return a stale extension match.
    for old_ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
        old = base / f"company_logo{old_ext}"
        try:
            if old.is_file():
                old.unlink()
        except Exception:
            pass
    target = base / f"company_logo{ext}"
    target.write_bytes(raw)
    # Record the chosen path in app_settings so the renderer can pick it up
    # even when TRUSTNODE_DATA_DIR points elsewhere. Best-effort: never
    # block the upload on a settings save failure.
    try:
        _app_store.upsert_domain(
            "app_settings",
            {"company_logo_path": str(target)},
            actor="branding.upload",
        )
    except Exception:
        pass
    return {"ok": True, "filename": target.name, "size_bytes": len(raw)}


@router.delete("/branding/company-logo")
def delete_company_logo() -> dict[str, Any]:
    """Remove the company logo so PDFs return to the default layout
    (TrustNode logo on the right only)."""
    base = _company_logo_dir()
    removed = []
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
        path = base / f"company_logo{ext}"
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except Exception:
            pass
    try:
        _app_store.upsert_domain(
            "app_settings",
            {"company_logo_path": ""},
            actor="branding.delete",
        )
    except Exception:
        pass
    return {"ok": True, "removed": removed}
