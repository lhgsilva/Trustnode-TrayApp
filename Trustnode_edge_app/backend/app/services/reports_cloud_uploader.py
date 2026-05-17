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
        log.debug("Lite storage upload failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.debug("Lite storage upload error: %s", exc)
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
                      :email_status, :email_message, :email_recipients_json::jsonb,
                      :meta_json::jsonb, :created_utc
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
        log.debug("Lite generated_reports mirror failed: %s", exc)
        return False


def upload_and_mirror(record: dict[str, Any]) -> str | None:
    """Convenience: upload the PDF, then mirror the row carrying the
    resulting storage_path. Returns the object key (or None).

    Designed to be called fire-and-forget from `ReportsStore.insert_generated`
    so a Supabase outage never breaks local report generation.
    """
    file_path = record.get("file_path")
    if not file_path:
        return None
    storage_path = upload_pdf_to_storage(
        file_path,
        tenant_id=str(record.get("tenant_id") or "default"),
        generated_id=str(record.get("id") or ""),
        file_name=str(record.get("file_name") or "report.pdf"),
    )
    mirror_generated_row(record, storage_path)
    return storage_path
