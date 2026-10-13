"""Best-effort uploader that copies generated PDFs into the Supabase
`lite-reports` Storage bucket and mirrors the `generated_reports` row into
Postgres so the Lite app can list/preview/download reports without ever
touching the edge filesystem.

Both steps are no-ops when env vars are missing. The local SQLite row stays
authoritative; the cloud copy is purely for the Lite read-only viewer.

Env vars consumed:
  TRUSTNODE_SUPABASE_URL          e.g. https://tsfreqjcrgbxdwvmxeuk.supabase.co
  TRUSTNODE_SUPABASE_SERVICE_KEY  service-role JWT (NEVER ship to the browser)
  TRUSTNODE_SUPABASE_REPORTS_BUCKET  default "lite-reports"
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "lite-reports"

#: How long upload_and_mirror waits for the PDF before returning. The
#: metadata is already written by then, so a slow bucket delays the download
#: link, never the report's existence. The thread keeps running after a
#: timeout; the storage_path is then attached by the poller's backfill.
_UPLOAD_JOIN_TIMEOUT_S = 90.0
_UPLOAD_TIMEOUT = 30.0
_DB_TIMEOUT = 15.0


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _supabase_cfg() -> tuple[str, str, str] | None:
    url = _env("TRUSTNODE_SUPABASE_URL").rstrip("/")
    key = _env("TRUSTNODE_SUPABASE_SERVICE_KEY")
    if not (url and key):
        return None
    bucket = _env("TRUSTNODE_SUPABASE_REPORTS_BUCKET") or _DEFAULT_BUCKET
    return url, key, bucket


def _storage_object_path(tenant_id: str, generated_id: str, file_name: str) -> str:
    """`<tenant_id>/<generated_id>__<file_name>` — keeps PDFs scoped per tenant
    (so storage RLS by path prefix works) and avoids collisions across reruns.
    """
    safe_name = (file_name or "report.pdf").replace("/", "_").replace("\\", "_")
    return f"{tenant_id}/{generated_id}__{safe_name}"


def upload_pdf_to_storage(
    file_path: str | Path,
    *,
    tenant_id: str,
    generated_id: str,
    file_name: str,
) -> str | None:
    """Upload the given PDF to the lite-reports bucket. Returns the object
    path on success or None on any failure (caller logs nothing — the upload
    is best-effort).
    """
    cfg = _supabase_cfg()
    if not cfg:
        return None
    url, key, bucket = cfg
    path = Path(str(file_path))
    if not path.exists():
        return None
    object_key = _storage_object_path(tenant_id, generated_id, file_name)
    endpoint = f"{url}/storage/v1/object/{bucket}/{object_key}"
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/pdf",
        "x-upsert": "true",
    }
    try:
        with path.open("rb") as fh:
            resp = requests.post(endpoint, headers=headers, data=fh.read(), timeout=_UPLOAD_TIMEOUT)
        if resp.status_code in (200, 201):
            return object_key
        log.warning("Lite storage upload failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Lite storage upload error: %s", exc)
    return None


def fetch_pdf_from_storage(storage_path: str) -> bytes | None:
    """The bytes of a report already uploaded to the lite-reports bucket.

    2026-10-08, requested: "we should also be able to read the reports from
    the webview from the lite version and web version, that is why it is
    important we ship everything to the cloud vps".

    The PDFs were already being SHIPPED - every report since 2026-10-01 has
    a storage_path - but nothing could read them back. Both download routes
    served `record["file_path"]` from local disk and returned 410 "Report
    file is no longer on disk" when it was absent. That is always the case
    for a cloud/web client: the copy it can reach lives in the bucket, not
    on the edge's filesystem. One upload, no way down.

    Returns None on any failure; the caller falls back to its own error.
    """
    cfg = _supabase_cfg()
    key_path = str(storage_path or "").strip()
    if not cfg or not key_path:
        return None
    url, key, bucket = cfg
    endpoint = f"{url}/storage/v1/object/{bucket}/{key_path}"
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        resp = requests.get(endpoint, headers=headers, timeout=_UPLOAD_TIMEOUT)
        if resp.status_code == 200 and resp.content:
            return resp.content
        log.warning("Lite storage fetch failed: %s %s",
                    resp.status_code, resp.text[:200])
    except Exception as exc:                            # noqa: BLE001
        log.warning("Lite storage fetch error: %s", exc)
    return None


def mirror_generated_row(record: dict[str, Any], storage_path: str | None) -> bool:
    """Upsert a single `generated_reports` row into Supabase. Returns True on
    success. Uses the Postgres pooler the edge already configured for the
    historian — re-uses AppStore's engine helpers so we don't open a second
    connection pool.
    """
    try:
        # Use the process-wide singleton so connection-pool / engine caches
        # are shared. Constructing AppStore() fresh per call would create
        # its own SQLAlchemy engine + connection pool every time and
        # quickly exhaust Supabase's session-mode pooler.
        from app.state import app_store as store  # local import to avoid cycles
    except Exception:
        return False
    cloud = None
    try:
        cloud = store._get_cloud_database_target()  # type: ignore[attr-defined]
    except Exception:
        cloud = None
    if not cloud:
        return False
    try:
        from sqlalchemy import text  # type: ignore
        schema = str(cloud.get("schema") or "public")
        engine, _ = store._get_or_create_cloud_engine(cloud, schema)  # type: ignore[attr-defined]
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".generated_reports (
                      id, tenant_id, template_id, template_name,
                      schedule_id, schedule_name, triggered_by,
                      file_name, file_bytes, file_sha256, storage_path,
                      email_status, email_message, email_recipients_json,
                      meta_json, created_utc
                    ) VALUES (
                      :id, :tenant_id, :template_id, :template_name,
                      :schedule_id, :schedule_name, :triggered_by,
                      :file_name, :file_bytes, :file_sha256, :storage_path,
                      :email_status, :email_message, CAST(:email_recipients_json AS jsonb),
                      CAST(:meta_json AS jsonb), :created_utc
                    )
                    ON CONFLICT (tenant_id, id) DO UPDATE SET
                      template_id   = EXCLUDED.template_id,
                      template_name = EXCLUDED.template_name,
                      schedule_id   = EXCLUDED.schedule_id,
                      schedule_name = EXCLUDED.schedule_name,
                      triggered_by  = EXCLUDED.triggered_by,
                      file_name     = EXCLUDED.file_name,
                      file_bytes    = EXCLUDED.file_bytes,
                      file_sha256   = EXCLUDED.file_sha256,
                      storage_path  = COALESCE(EXCLUDED.storage_path, "{schema}".generated_reports.storage_path),
                      email_status  = EXCLUDED.email_status,
                      email_message = EXCLUDED.email_message,
                      email_recipients_json = EXCLUDED.email_recipients_json,
                      meta_json     = EXCLUDED.meta_json
                    """
                ),
                {
                    "id": str(record.get("id") or ""),
                    "tenant_id": str(record.get("tenant_id") or "default"),
                    "template_id": (str(record.get("template_id") or "") or None),
                    "template_name": (str(record.get("template_name") or "") or None),
                    "schedule_id": (str(record.get("schedule_id") or "") or None),
                    "schedule_name": (str(record.get("schedule_name") or "") or None),
                    "triggered_by": str(record.get("triggered_by") or "manual"),
                    "file_name": str(record.get("file_name") or ""),
                    "file_bytes": int(record.get("file_bytes") or 0),
                    "file_sha256": (str(record.get("file_sha256") or "") or None),
                    "storage_path": (storage_path or None),
                    "email_status": (str(record.get("email_status") or "") or None),
                    "email_message": (str(record.get("email_message") or "") or None),
                    "email_recipients_json": json.dumps(record.get("email_recipients") or [], ensure_ascii=False),
                    "meta_json": json.dumps(record.get("meta") or {}, ensure_ascii=False),
                    "created_utc": str(record.get("created_utc") or ""),
                },
            )
        return True
    except Exception as exc:
        # WARNING, not debug. This mirror was broken from the day it shipped
        # and nobody saw it: the cloud Lite showed an empty Reporting page
        # while the edge reported success, because the only trace of the
        # failure was a debug line no deployment turns on.
        log.warning("Lite generated_reports mirror failed: %s", exc)
        return False


def upload_and_mirror(record: dict[str, Any]) -> str | None:
    """Mirror the metadata and upload the PDF, IN PARALLEL.

    Returns the object key, or None when no file reached Storage.

    2026-10-13, requested: "make sure that the metadata is pushed to the
    cloud so the report can be regenerated and downloaded, and also attach
    the file in parallel".

    The previous version did neither:

      * it returned EARLY when the record had no file_path, so the metadata
        never reached the cloud at all - the report did not exist there, and
        nothing could be listed, regenerated or even known about;
      * a failing upload took the metadata down with it, because the mirror
        only ran after the upload returned.

    Metadata is the authoritative part: it is what lists the report and what
    a regeneration reads its window from. So it is written UNCONDITIONALLY
    and never waits on the bytes. The upload runs on its own thread and,
    when it lands, a second upsert records the path - safe to repeat because
    the ON CONFLICT clause COALESCEs storage_path and never nulls an existing
    one.
    """
    import threading

    file_path = record.get("file_path")
    result: dict[str, Any] = {"storage_path": None}

    def _upload() -> None:
        if not file_path:
            return
        try:
            result["storage_path"] = upload_pdf_to_storage(
                file_path,
                tenant_id=str(record.get("tenant_id") or "default"),
                generated_id=str(record.get("id") or ""),
                file_name=str(record.get("file_name") or "report.pdf"),
            )
        except Exception:
            # A storage outage must never cost us the metadata below.
            result["storage_path"] = None

    worker = threading.Thread(target=_upload, name="tn-report-upload", daemon=True)
    worker.start()

    # Runs WHILE the file uploads. The report is listed and regenerable from
    # this moment, whether or not the bytes ever arrive.
    try:
        mirror_generated_row(record, None)
    except Exception:
        pass

    worker.join(timeout=_UPLOAD_JOIN_TIMEOUT_S)
    storage_path = result.get("storage_path")
    if storage_path:
        try:
            mirror_generated_row(record, str(storage_path))
        except Exception:
            # The row is already there; the next backfill pass will attach
            # the path rather than leaving the report unlisted.
            pass
    return storage_path
