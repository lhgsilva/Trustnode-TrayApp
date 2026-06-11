"""Persistence layer for the reporting module.

Owns CRUD over the SQLite tables `report_templates`, `scheduled_reports`,
`generated_reports` introduced in `app_store.py`. Kept in its own module so
the existing AppStore stays focused on historian / configuration concerns.

The data shapes deliberately mirror the JSON the frontend already produces
(see `App.jsx` `sanitizeReportTemplates` / `sanitizeScheduledReports`) so the
Reporting and Scheduled Reports UIs don't need to be rewritten end-to-end —
they only swap the persistence call from bootstrap-PUT to the new endpoints.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.tenant import get_current_tenant


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _resolve_db_path() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def _resolve_reports_dir() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        out = Path(base) / "reports"
    else:
        out = Path.home() / ".trustnode_edge" / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


class ReportsStore:
    """CRUD for templates, schedules, and generated report records.

    Read methods do not hold a Python lock (SQLite WAL handles concurrent
    readers). Writes briefly hold an internal lock so the upsert + audit row
    pattern stays atomic across threads.
    """

    def __init__(self) -> None:
        self.db_path = _resolve_db_path()
        self.reports_dir = _resolve_reports_dir()
        self._write_lock = threading.Lock()

    # --- connection ---
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # --- templates ---
    def list_templates(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, description, definition_json, created_utc, updated_utc, created_by "
                "FROM report_templates WHERE tenant_id = ? ORDER BY updated_utc DESC",
                (tid,),
            ).fetchall()
        return [self._row_to_template(r) for r in rows]

    def get_template(self, template_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, description, definition_json, created_utc, updated_utc, created_by "
                "FROM report_templates WHERE tenant_id = ? AND id = ?",
                (tid, str(template_id)),
            ).fetchone()
        return self._row_to_template(row) if row else None

    def upsert_template(
        self,
        template: dict[str, Any],
        tenant_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        tpl_id = str(template.get("id") or "").strip() or _new_id("tpl")
        name = str(template.get("name") or "").strip() or "Untitled report"
        description = str(template.get("description") or "")
        definition = template.get("definition") or template.get("sections") or template.get("filters") or {}
        if isinstance(definition, list):
            # Accept the simpler array-of-sections shape.
            definition = {"sections": definition}
        if not isinstance(definition, dict):
            definition = {}
        definition_json = json.dumps(definition, ensure_ascii=False, separators=(",", ":"))
        now = _utc_now()
        author = (created_by or "").strip() or None
        with self._write_lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id, created_utc FROM report_templates WHERE tenant_id = ? AND id = ?",
                    (tid, tpl_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE report_templates SET name = ?, description = ?, definition_json = ?, updated_utc = ? "
                        "WHERE tenant_id = ? AND id = ?",
                        (name, description, definition_json, now, tid, tpl_id),
                    )
                    created_utc = str(existing["created_utc"] or now)
                else:
                    conn.execute(
                        "INSERT INTO report_templates (id, tenant_id, name, description, definition_json, "
                        "created_utc, updated_utc, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (tpl_id, tid, name, description, definition_json, now, now, author),
                    )
                    created_utc = now
                conn.commit()
        # Best-effort mirror to Supabase so the Lite app sees new/edited
        # templates without a full bootstrap fetch. Never fail the local
        # save on a cloud hiccup.
        try:
            self._mirror_template_to_cloud(
                tenant_id=tid, tpl_id=tpl_id, name=name, description=description,
                definition_json=definition_json, created_utc=created_utc,
                updated_utc=now, author=author,
            )
        except Exception:
            pass
        return {
            "id": tpl_id,
            "name": name,
            "description": description,
            "definition": definition,
            "created_utc": created_utc,
            "updated_utc": now,
            "created_by": author,
        }

    def _mirror_template_to_cloud(self, *, tenant_id: str, tpl_id: str, name: str,
                                   description: str, definition_json: str,
                                   created_utc: str, updated_utc: str,
                                   author: str | None) -> None:
        """Push a freshly-saved report template into Supabase.

        Runs in a background thread so the local save doesn't block on a
        cloud round-trip (same pattern as `_mirror_config_doc_to_cloud`).
        """
        from .app_store import app_store  # local import to avoid a cycle
        try:
            cloud = app_store._get_cloud_database_target()  # noqa: SLF001
        except Exception:
            cloud = None
        if not cloud:
            return

        kwargs = {
            "id": tpl_id, "tenant_id": tenant_id, "name": name,
            "description": description, "definition_json": definition_json,
            "created_utc": created_utc, "updated_utc": updated_utc,
            "author": author,
        }

        def _do_upsert() -> None:
            try:
                from sqlalchemy import text  # type: ignore
                schema = str(cloud.get("schema") or "public")
                engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)  # noqa: SLF001
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."report_templates"
                              (id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by)
                            VALUES
                              (:id, :tenant_id, :name, :description, :definition_json::jsonb,
                               :created_utc, :updated_utc, :author)
                            ON CONFLICT (tenant_id, id) DO UPDATE SET
                              name            = EXCLUDED.name,
                              description     = EXCLUDED.description,
                              definition_json = EXCLUDED.definition_json,
                              updated_utc     = EXCLUDED.updated_utc,
                              created_by      = EXCLUDED.created_by
                            """
                        ),
                        kwargs,
                    )
            except Exception:
                pass

        try:
            import threading
            threading.Thread(target=_do_upsert, name="tn-mirror-report-template",
                             daemon=True).start()
        except Exception:
            pass

    def reconcile_templates_to_cloud(self, tenant_id: str | None = None) -> int:
        """Bulk-push every local report_template row to Supabase.

        The per-save mirror (`_mirror_template_to_cloud`) can drop writes
        when the cloud target is unreachable at the moment of save. This
        catch-up runs on backend startup and re-upserts everything so the
        Lite app doesn't keep showing yesterday's template list after a
        local edit cycle. Idempotent — uses ON CONFLICT DO UPDATE.

        Returns the count of templates attempted.
        """
        from .app_store import app_store  # local import to avoid cycle
        try:
            cloud = app_store._get_cloud_database_target()  # noqa: SLF001
        except Exception:
            cloud = None
        if not cloud:
            return 0
        with self._connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    "SELECT id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by "
                    "FROM report_templates WHERE tenant_id = ?",
                    (str(tenant_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by "
                    "FROM report_templates"
                ).fetchall()
        if not rows:
            return 0
        try:
            from sqlalchemy import text  # type: ignore
            schema = str(cloud.get("schema") or "public")
            engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)  # noqa: SLF001
        except Exception:
            return 0
        upserted = 0
        for r in rows:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."report_templates"
                              (id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by)
                            VALUES
                              (:id, :tenant_id, :name, :description, :definition_json::jsonb,
                               :created_utc, :updated_utc, :author)
                            ON CONFLICT (tenant_id, id) DO UPDATE SET
                              name            = EXCLUDED.name,
                              description     = EXCLUDED.description,
                              definition_json = EXCLUDED.definition_json,
                              updated_utc     = EXCLUDED.updated_utc,
                              created_by      = EXCLUDED.created_by
                            """
                        ),
                        {
                            "id": str(r["id"]),
                            "tenant_id": str(r["tenant_id"]),
                            "name": str(r["name"] or ""),
                            "description": str(r["description"] or ""),
                            "definition_json": str(r["definition_json"] or "{}"),
                            "created_utc": str(r["created_utc"] or _utc_now()),
                            "updated_utc": str(r["updated_utc"] or _utc_now()),
                            "author": str(r["created_by"] or "") or None,
                        },
                    )
                upserted += 1
            except Exception:
                # Skip one bad row; keep going for the rest.
                continue
        return upserted

    def seed_demo_templates(self, tenant_id: str, created_by: str = "system") -> int:
        """Install the 5 demo report templates for a tenant if it doesn't
        already own them. Used to give customers a visible "look what the
        analytics tools can do" set the first time they reach the Reporting
        page. Idempotent — we key each demo by a stable id, so a re-run
        won't duplicate or overwrite a user-customised copy."""
        tid = str(tenant_id or "default").strip() or "default"
        demos = _build_demo_templates()
        installed = 0
        with self._write_lock:
            with self._connect() as conn:
                for tpl in demos:
                    tpl_id = str(tpl["id"])
                    existing = conn.execute(
                        "SELECT 1 FROM report_templates WHERE tenant_id = ? AND id = ?",
                        (tid, tpl_id),
                    ).fetchone()
                    if existing:
                        continue
                    now = _utc_now()
                    conn.execute(
                        "INSERT INTO report_templates (id, tenant_id, name, description, definition_json, "
                        "created_utc, updated_utc, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tpl_id, tid, str(tpl["name"]), str(tpl.get("description") or ""),
                            json.dumps(tpl["definition"], ensure_ascii=False, separators=(",", ":")),
                            now, now, created_by,
                        ),
                    )
                    installed += 1
                conn.commit()
        return installed

    def delete_template(self, template_id: str, tenant_id: str | None = None) -> bool:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM report_templates WHERE tenant_id = ? AND id = ?",
                    (tid, str(template_id)),
                )
                conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_template(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        try:
            definition = json.loads(str(row["definition_json"] or "{}"))
        except Exception:
            definition = {}
        return {
            "id": str(row["id"]),
            "name": str(row["name"] or ""),
            "description": str(row["description"] or ""),
            "definition": definition,
            "created_utc": str(row["created_utc"] or ""),
            "updated_utc": str(row["updated_utc"] or ""),
            "created_by": str(row["created_by"] or "") or None,
        }

    # --- schedules ---
    def list_schedules(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_reports WHERE tenant_id = ? ORDER BY updated_utc DESC",
                (tid,),
            ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def list_enabled_schedules_all_tenants(self) -> list[dict[str, Any]]:
        """Used by the scheduler daemon: enumerate every enabled schedule
        across tenants in one pass (no tenant filter)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_reports WHERE enabled = 1"
            ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get_schedule(self, schedule_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_reports WHERE tenant_id = ? AND id = ?",
                (tid, str(schedule_id)),
            ).fetchone()
        return self._row_to_schedule(row) if row else None

    def upsert_schedule(
        self,
        schedule: dict[str, Any],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        sid = str(schedule.get("id") or "").strip() or _new_id("sch")
        name = str(schedule.get("name") or "").strip() or "Untitled schedule"
        template_id = str(schedule.get("template_id") or "").strip()
        enabled = 1 if bool(schedule.get("enabled", True)) else 0
        trigger_mode = str(schedule.get("trigger_mode") or "time").strip().lower()
        if trigger_mode not in {"time", "tag", "both"}:
            trigger_mode = "time"
        recurrence = str(schedule.get("recurrence") or "daily").strip().lower()
        if recurrence not in {"daily", "weekly", "monthly", "hourly"}:
            recurrence = "daily"
        hour = max(0, min(23, int(schedule.get("hour") if schedule.get("hour") is not None else 8)))
        minute = max(0, min(59, int(schedule.get("minute") if schedule.get("minute") is not None else 0)))
        dow = schedule.get("day_of_week")
        dow = int(dow) if dow is not None and str(dow).strip() != "" else None
        dom = schedule.get("day_of_month")
        dom = int(dom) if dom is not None and str(dom).strip() != "" else None
        tag_conditions = schedule.get("tag_conditions") or []
        if not isinstance(tag_conditions, list):
            tag_conditions = []
        condition_logic = str(schedule.get("condition_logic") or "all").strip().lower()
        if condition_logic not in {"all", "any"}:
            condition_logic = "all"
        deliver_email = 1 if bool(schedule.get("deliver_email", False)) else 0
        recipients = schedule.get("recipients")
        if isinstance(recipients, str):
            recipients = [x.strip() for x in recipients.replace(",", ";").split(";") if x.strip()]
        if not isinstance(recipients, list):
            recipients = []
        email_subject = str(schedule.get("email_subject") or "")
        email_body = str(schedule.get("email_body") or "")
        email_profile_id = str(schedule.get("email_profile_id") or "")
        fmt = str(schedule.get("format") or "pdf").strip().lower()
        if fmt not in {"pdf"}:
            fmt = "pdf"
        require_gateway_running = 1 if bool(schedule.get("require_gateway_running", False)) else 0
        # Attachment flags: PDF defaults on (back-compat), CSV/TXT opt-in.
        attach_pdf = 1 if bool(schedule.get("attach_pdf", True)) else 0
        attach_csv = 1 if bool(schedule.get("attach_csv", False)) else 0
        attach_txt = 1 if bool(schedule.get("attach_txt", False)) else 0
        now = _utc_now()
        with self._write_lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id, created_utc, last_run_utc FROM scheduled_reports WHERE tenant_id = ? AND id = ?",
                    (tid, sid),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE scheduled_reports SET
                          name = ?, template_id = ?, enabled = ?, trigger_mode = ?,
                          recurrence = ?, hour = ?, minute = ?, day_of_week = ?, day_of_month = ?,
                          tag_conditions_json = ?, condition_logic = ?,
                          deliver_email = ?, recipients_json = ?, email_subject = ?, email_body = ?,
                          email_profile_id = ?, format = ?, require_gateway_running = ?,
                          attach_pdf = ?, attach_csv = ?, attach_txt = ?,
                          updated_utc = ?
                        WHERE tenant_id = ? AND id = ?
                        """,
                        (
                            name, template_id, enabled, trigger_mode,
                            recurrence, hour, minute, dow, dom,
                            json.dumps(tag_conditions, ensure_ascii=False),
                            condition_logic, deliver_email,
                            json.dumps(recipients, ensure_ascii=False),
                            email_subject, email_body, email_profile_id, fmt,
                            require_gateway_running,
                            attach_pdf, attach_csv, attach_txt,
                            now,
                            tid, sid,
                        ),
                    )
                    created_utc = str(existing["created_utc"] or now)
                else:
                    conn.execute(
                        """
                        INSERT INTO scheduled_reports (
                          id, tenant_id, name, template_id, enabled, trigger_mode,
                          recurrence, hour, minute, day_of_week, day_of_month,
                          tag_conditions_json, condition_logic,
                          deliver_email, recipients_json, email_subject, email_body,
                          email_profile_id, format, require_gateway_running,
                          attach_pdf, attach_csv, attach_txt,
                          created_utc, updated_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid, tid, name, template_id, enabled, trigger_mode,
                            recurrence, hour, minute, dow, dom,
                            json.dumps(tag_conditions, ensure_ascii=False),
                            condition_logic, deliver_email,
                            json.dumps(recipients, ensure_ascii=False),
                            email_subject, email_body,
                            email_profile_id, fmt, require_gateway_running,
                            attach_pdf, attach_csv, attach_txt,
                            now, now,
                        ),
                    )
                    created_utc = now
                conn.commit()
        return self.get_schedule(sid, tenant_id=tid) or {}

    def delete_schedule(self, schedule_id: str, tenant_id: str | None = None) -> bool:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM scheduled_reports WHERE tenant_id = ? AND id = ?",
                    (tid, str(schedule_id)),
                )
                conn.commit()
        return cur.rowcount > 0

    def mark_schedule_run(
        self,
        schedule_id: str,
        last_run_utc: str,
        status: str,
        error: str | None = None,
        next_run_utc: str | None = None,
    ) -> None:
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE scheduled_reports SET last_run_utc = ?, next_run_utc = ?, "
                    "last_status = ?, last_error = ? WHERE id = ?",
                    (last_run_utc, next_run_utc, status, (error or None), str(schedule_id)),
                )
                conn.commit()

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        try:
            tag_conditions = json.loads(str(row["tag_conditions_json"] or "[]"))
        except Exception:
            tag_conditions = []
        try:
            recipients = json.loads(str(row["recipients_json"] or "[]"))
        except Exception:
            recipients = []
        return {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"] or "default"),
            "name": str(row["name"] or ""),
            "template_id": str(row["template_id"] or ""),
            "enabled": bool(int(row["enabled"] or 0)),
            "trigger_mode": str(row["trigger_mode"] or "time"),
            "recurrence": str(row["recurrence"] or "daily"),
            "hour": int(row["hour"] or 0),
            "minute": int(row["minute"] or 0),
            "day_of_week": (int(row["day_of_week"]) if row["day_of_week"] is not None else None),
            "day_of_month": (int(row["day_of_month"]) if row["day_of_month"] is not None else None),
            "tag_conditions": tag_conditions,
            "condition_logic": str(row["condition_logic"] or "all"),
            "deliver_email": bool(int(row["deliver_email"] or 0)),
            "recipients": recipients,
            "email_subject": str(row["email_subject"] or ""),
            "email_body": str(row["email_body"] or ""),
            "email_profile_id": str(row["email_profile_id"] or ""),
            "format": str(row["format"] or "pdf"),
            "require_gateway_running": bool(
                int(row["require_gateway_running"] or 0)
                if "require_gateway_running" in row.keys()
                else 0
            ),
            # Attachments: PDF defaults on for existing rows, CSV/TXT off.
            "attach_pdf": bool(
                int(row["attach_pdf"] if "attach_pdf" in row.keys() and row["attach_pdf"] is not None else 1)
            ),
            "attach_csv": bool(
                int(row["attach_csv"] if "attach_csv" in row.keys() and row["attach_csv"] is not None else 0)
            ),
            "attach_txt": bool(
                int(row["attach_txt"] if "attach_txt" in row.keys() and row["attach_txt"] is not None else 0)
            ),
            "created_utc": str(row["created_utc"] or ""),
            "updated_utc": str(row["updated_utc"] or ""),
            "last_run_utc": str(row["last_run_utc"] or "") or None,
            "next_run_utc": str(row["next_run_utc"] or "") or None,
            "last_status": str(row["last_status"] or "") or None,
            "last_error": str(row["last_error"] or "") or None,
        }

    # --- generated reports ---
    def list_generated(
        self,
        tenant_id: str | None = None,
        limit: int = 200,
        schedule_id: str | None = None,
        template_id: str | None = None,
    ) -> list[dict[str, Any]]:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        lim = max(1, min(int(limit or 200), 2000))
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tid]
        if schedule_id:
            clauses.append("schedule_id = ?")
            params.append(str(schedule_id))
        if template_id:
            clauses.append("template_id = ?")
            params.append(str(template_id))
        sql = (
            "SELECT * FROM generated_reports WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_utc DESC LIMIT ?"
        )
        params.append(lim)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_generated(r) for r in rows]

    def get_generated(self, generated_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generated_reports WHERE tenant_id = ? AND id = ?",
                (tid, str(generated_id)),
            ).fetchone()
        return self._row_to_generated(row) if row else None

    def insert_generated(self, record: dict[str, Any]) -> dict[str, Any]:
        tid = (record.get("tenant_id") or get_current_tenant() or "default").strip() or "default"
        gid = str(record.get("id") or "").strip() or _new_id("rpt")
        now = _utc_now()
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO generated_reports (
                      id, tenant_id, template_id, template_name,
                      schedule_id, schedule_name, triggered_by,
                      file_path, file_name, file_bytes, file_sha256,
                      created_utc, email_status, email_message, email_recipients_json, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid, tid,
                        str(record.get("template_id") or "") or None,
                        str(record.get("template_name") or "") or None,
                        str(record.get("schedule_id") or "") or None,
                        str(record.get("schedule_name") or "") or None,
                        str(record.get("triggered_by") or "manual"),
                        str(record.get("file_path") or ""),
                        str(record.get("file_name") or ""),
                        int(record.get("file_bytes") or 0),
                        str(record.get("file_sha256") or "") or None,
                        str(record.get("created_utc") or now),
                        str(record.get("email_status") or "") or None,
                        str(record.get("email_message") or "") or None,
                        json.dumps(record.get("email_recipients") or [], ensure_ascii=False),
                        json.dumps(record.get("meta") or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()
        full = self.get_generated(gid, tenant_id=tid) or {}
        # Best-effort: push the PDF to Supabase Storage + mirror the row so
        # the Lite app can list/preview/download without touching the edge.
        # Runs in a background thread to keep the API response snappy.
        try:
            import threading
            from app.services.reports_cloud_uploader import upload_and_mirror

            def _push(rec: dict[str, Any], store: "ReportsStore") -> None:
                try:
                    storage_path = upload_and_mirror(rec)
                except Exception:
                    storage_path = None
                if storage_path:
                    try:
                        store._set_storage_path(str(rec.get("id") or ""), storage_path)
                    except Exception:
                        pass

            threading.Thread(target=_push, args=(dict(full), self), daemon=True).start()
        except Exception:
            pass
        return full

    def _set_storage_path(self, generated_id: str, storage_path: str) -> None:
        """Backfill the local `storage_path` column after a successful upload.
        Local SQLite stays authoritative even if the cloud copy lags."""
        if not (generated_id and storage_path):
            return
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE generated_reports SET storage_path = ? WHERE id = ?",
                    (storage_path, generated_id),
                )
                conn.commit()

    def update_generated_email_status(
        self,
        generated_id: str,
        status: str,
        message: str | None = None,
        recipients: list[str] | None = None,
    ) -> None:
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE generated_reports SET email_status = ?, email_message = ?, email_recipients_json = ? "
                    "WHERE id = ?",
                    (
                        status,
                        message,
                        json.dumps(recipients or [], ensure_ascii=False),
                        str(generated_id),
                    ),
                )
                conn.commit()

    def delete_generated(self, generated_id: str, tenant_id: str | None = None) -> bool:
        tid = (tenant_id or get_current_tenant() or "default").strip() or "default"
        record = self.get_generated(generated_id, tenant_id=tid)
        if not record:
            return False
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM generated_reports WHERE tenant_id = ? AND id = ?",
                    (tid, str(generated_id)),
                )
                conn.commit()
        # Best-effort file unlink.
        try:
            path = Path(record.get("file_path") or "")
            if path.exists():
                path.unlink()
        except Exception:
            pass
        return True

    @staticmethod
    def _row_to_generated(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        try:
            recipients = json.loads(str(row["email_recipients_json"] or "[]"))
        except Exception:
            recipients = []
        try:
            meta = json.loads(str(row["meta_json"] or "{}"))
        except Exception:
            meta = {}
        # storage_path is a 2026-05-18 addition; older rows lack the column.
        try:
            storage_path = str(row["storage_path"] or "") or None
        except Exception:
            storage_path = None
        return {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"] or "default"),
            "template_id": str(row["template_id"] or "") or None,
            "template_name": str(row["template_name"] or "") or None,
            "schedule_id": str(row["schedule_id"] or "") or None,
            "schedule_name": str(row["schedule_name"] or "") or None,
            "triggered_by": str(row["triggered_by"] or "manual"),
            "file_path": str(row["file_path"] or ""),
            "file_name": str(row["file_name"] or ""),
            "file_bytes": int(row["file_bytes"] or 0),
            "file_sha256": str(row["file_sha256"] or "") or None,
            "storage_path": storage_path,
            "created_utc": str(row["created_utc"] or ""),
            "email_status": str(row["email_status"] or "") or None,
            "email_message": str(row["email_message"] or "") or None,
            "email_recipients": recipients,
            "meta": meta,
        }

    # --- migration helper ---
    def migrate_from_bootstrap(self, bootstrap: dict[str, Any], tenant_id: str = "default") -> dict[str, int]:
        """One-shot migration from the legacy `reporting_setup` JSON blob.

        Called by AppStore on startup if any templates/schedules exist in the
        config_documents store but are missing from the dedicated tables.
        Returns counts so the boot path can log what moved.
        """
        moved_templates = 0
        moved_schedules = 0
        reporting = bootstrap.get("reporting_setup") if isinstance(bootstrap, dict) else None
        if not isinstance(reporting, dict):
            return {"templates": 0, "schedules": 0}
        templates = reporting.get("templates") or []
        if isinstance(templates, list):
            for tpl in templates:
                if not isinstance(tpl, dict) or not tpl.get("id"):
                    continue
                if self.get_template(str(tpl.get("id")), tenant_id=tenant_id):
                    continue
                self.upsert_template(
                    {
                        "id": tpl.get("id"),
                        "name": tpl.get("name") or "Imported template",
                        "description": tpl.get("description") or "",
                        "definition": {"sections": [], "filters": tpl.get("filters") or {}},
                    },
                    tenant_id=tenant_id,
                    created_by=str(tpl.get("created_by") or ""),
                )
                moved_templates += 1
        schedules = reporting.get("schedules") or []
        if isinstance(schedules, list):
            for sch in schedules:
                if not isinstance(sch, dict) or not sch.get("id"):
                    continue
                if self.get_schedule(str(sch.get("id")), tenant_id=tenant_id):
                    continue
                self.upsert_schedule(
                    {
                        "id": sch.get("id"),
                        "name": sch.get("name") or "Imported schedule",
                        "template_id": sch.get("template_id") or "",
                        "enabled": bool(sch.get("enabled", True)),
                        "trigger_mode": sch.get("trigger_mode") or "time",
                        "recurrence": sch.get("recurrence") or "daily",
                        "hour": sch.get("hour"),
                        "minute": sch.get("minute"),
                        "day_of_week": sch.get("day_of_week"),
                        "day_of_month": sch.get("day_of_month"),
                        "tag_conditions": sch.get("tag_conditions") or [],
                        "condition_logic": sch.get("condition_logic") or "all",
                        "deliver_email": bool(sch.get("deliver_email", False)),
                        "recipients": sch.get("recipients") or [],
                        "format": sch.get("format") or "pdf",
                    },
                    tenant_id=tenant_id,
                )
                moved_schedules += 1
        return {"templates": moved_templates, "schedules": moved_schedules}


# ---------------------------------------------------------------------------
# Demo templates
# ---------------------------------------------------------------------------
#
# Ship a small set of high-quality report templates so a fresh edge has
# something to show in customer demos without anyone having to assemble
# them by hand. Each template is keyed by a stable `tpl-demo-*` id so
# seed_demo_templates() is idempotent across restarts and won't overwrite
# whatever the customer customises after the fact.
#
# Tag/gateway placeholders use "DEMO_*" identifiers — the Reporting UI
# already shows an editor where the customer maps the demo series to
# their actual tag names; until they do that the chart panels render
# empty but the layout is the visual proof of capability.

def _demo_series(label: str, tag_name: str, color: str, *, axis: str = "left",
                 chart_type: str = "", unit: str = "",
                 multiplier: float = 1, offset: float = 0,
                 gateway_id: str = "") -> dict[str, Any]:
    return {
        "id": _new_id("ser"),
        "label": label,
        "gateway_id": gateway_id or "DEMO_GATEWAY",
        "tag_name": tag_name,
        "color": color,
        "axis": axis,
        "chart_type": chart_type,
        "unit": unit,
        "multiplier": multiplier,
        "offset": offset,
    }


def _build_demo_templates() -> list[dict[str, Any]]:
    teal = "#14a89a"
    teal_dark = "#0e8479"
    amber = "#d39d3a"
    red = "#d35454"
    blue = "#1f78d1"
    purple = "#7c3aed"
    grey = "#6b7280"

    process_batch = {
        "id": "tpl-demo-process-batch",
        "name": "Process Batch Summary",
        "description": "End-to-end record of one production batch — setpoints vs actuals, time-in-spec, deviations.",
        "definition": {
            "sections": [
                {"type": "header", "title": "Process Batch Report", "subtitle": "Batch traceability + quality at a glance"},
                {"type": "text", "text": "This template captures the full lifecycle of a single batch: temperature/pressure trends versus setpoints, KPIs for in-spec time, and a deviation log so QA can audit the run in one page."},
                {"type": "kpi_grid", "title": "Batch KPIs", "columns": 4, "items": [
                    {"label": "Batch duration (min)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_BATCH_DURATION_MIN", "operator": "any", "aggregation": "max"},
                    {"label": "Avg temp (°C)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_TEMP_C", "operator": "any", "aggregation": "avg"},
                    {"label": "Time in spec (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_TEMP_C", "operator": "between", "value1": 78, "value2": 82, "aggregation": "percent"},
                    {"label": "Deviations", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_TEMP_C", "operator": "outside", "value1": 78, "value2": 82, "aggregation": "count"},
                ]},
                {"type": "line_chart", "title": "Process temperature vs setpoint", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_TEMP_C", "time_range": {"preset": "8h"}, "readings_count": 480, "series": [
                    _demo_series("Process temp (°C)", "DEMO_PROCESS_TEMP_C", teal),
                    _demo_series("Setpoint (°C)", "DEMO_PROCESS_SETPOINT_C", amber, chart_type="line"),
                ]},
                {"type": "line_chart", "title": "Pressure and flow", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_PRESSURE_BAR", "time_range": {"preset": "8h"}, "readings_count": 480, "series": [
                    _demo_series("Pressure (bar)", "DEMO_PROCESS_PRESSURE_BAR", blue),
                    _demo_series("Flow (L/min)", "DEMO_PROCESS_FLOW_LPM", purple, axis="right"),
                ]},
                {"type": "table_list", "title": "Top deviations during batch", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_PROCESS_TEMP_C", "list_limit": 12, "time_range": {"preset": "8h"}},
            ]
        },
    }

    oee = {
        "id": "tpl-demo-oee",
        "name": "OEE — Availability · Performance · Quality",
        "description": "Classic OEE breakdown with stop-cause Pareto and shift comparison.",
        "definition": {
            "sections": [
                {"type": "header", "title": "OEE Report", "subtitle": "Availability × Performance × Quality"},
                {"type": "text", "text": "Industry-standard OEE view: the three contributing factors, their product, the dominant stop causes, and a shift-by-shift comparison so supervisors can spot the worst-performing window."},
                {"type": "kpi_grid", "title": "OEE breakdown", "columns": 4, "items": [
                    {"label": "OEE (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_TOTAL", "operator": "any", "aggregation": "avg"},
                    {"label": "Availability (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_AVAILABILITY", "operator": "any", "aggregation": "avg"},
                    {"label": "Performance (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_PERFORMANCE", "operator": "any", "aggregation": "avg"},
                    {"label": "Quality (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_QUALITY", "operator": "any", "aggregation": "avg"},
                ]},
                {"type": "bar_chart", "title": "OEE by shift", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_TOTAL", "time_range": {"preset": "7d"}, "readings_count": 21, "series": [
                    _demo_series("Shift A", "DEMO_OEE_SHIFT_A", teal),
                    _demo_series("Shift B", "DEMO_OEE_SHIFT_B", teal_dark),
                    _demo_series("Shift C", "DEMO_OEE_SHIFT_C", amber),
                ]},
                {"type": "pie_chart", "title": "Downtime by cause (last 7 days)", "data_source_type": "computed", "gateway_id": "DEMO_GATEWAY", "compute_rules": [
                    {"id": "r1", "label": "Mechanical", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_STOP_REASON_CODE", "operator": "eq", "value1": 1, "aggregation": "count", "color": red},
                    {"id": "r2", "label": "Material starvation", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_STOP_REASON_CODE", "operator": "eq", "value1": 2, "aggregation": "count", "color": amber},
                    {"id": "r3", "label": "Changeover", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_STOP_REASON_CODE", "operator": "eq", "value1": 3, "aggregation": "count", "color": blue},
                    {"id": "r4", "label": "Operator", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_STOP_REASON_CODE", "operator": "eq", "value1": 4, "aggregation": "count", "color": purple},
                    {"id": "r5", "label": "Other", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_STOP_REASON_CODE", "operator": "eq", "value1": 5, "aggregation": "count", "color": grey},
                ]},
                {"type": "line_chart", "title": "OEE trend (rolling)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_OEE_TOTAL", "time_range": {"preset": "30d"}, "readings_count": 720, "series": [
                    _demo_series("OEE (%)", "DEMO_OEE_TOTAL", teal),
                    _demo_series("Target (%)", "DEMO_OEE_TARGET", amber),
                ]},
            ]
        },
    }

    energy = {
        "id": "tpl-demo-energy",
        "name": "Energy Consumption Report",
        "description": "Power, energy, peak demand and cost per kWh broken down by line/machine.",
        "definition": {
            "sections": [
                {"type": "header", "title": "Energy Report", "subtitle": "Active power, demand peaks and cost attribution"},
                {"type": "text", "text": "How much energy each line is consuming, when peak demand spikes, and where money is being spent. Pairs naturally with the power-meter gateway."},
                {"type": "kpi_grid", "title": "Energy KPIs (24h)", "columns": 4, "items": [
                    {"label": "Total kWh", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_ENERGY_KWH", "operator": "any", "aggregation": "sum"},
                    {"label": "Peak demand (kW)", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_ACTIVE_POWER_KW", "operator": "any", "aggregation": "max"},
                    {"label": "Avg power factor", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_POWER_FACTOR", "operator": "any", "aggregation": "avg"},
                    {"label": "Cost @ 0.18 €/kWh", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_ENERGY_KWH", "operator": "any", "aggregation": "sum", "multiplier": 0.18},
                ]},
                {"type": "line_chart", "title": "Active power trend (24h)", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_ACTIVE_POWER_KW", "time_range": {"preset": "24h"}, "readings_count": 1440, "series": [
                    _demo_series("Line 1 (kW)", "DEMO_LINE1_POWER_KW", teal),
                    _demo_series("Line 2 (kW)", "DEMO_LINE2_POWER_KW", blue),
                    _demo_series("Compressor (kW)", "DEMO_COMPRESSOR_POWER_KW", purple),
                ]},
                {"type": "bar_chart", "title": "Energy by line (last 7 days, kWh)", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_ENERGY_KWH", "time_range": {"preset": "7d"}, "readings_count": 7, "series": [
                    _demo_series("Line 1", "DEMO_LINE1_ENERGY_KWH", teal),
                    _demo_series("Line 2", "DEMO_LINE2_ENERGY_KWH", blue),
                    _demo_series("Utilities", "DEMO_UTILITIES_ENERGY_KWH", purple),
                ]},
                {"type": "line_chart", "title": "Voltage / current / power factor", "gateway_id": "DEMO_POWER_METER", "tag_name": "DEMO_VOLTAGE_V", "time_range": {"preset": "24h"}, "readings_count": 1440, "series": [
                    _demo_series("Voltage (V)", "DEMO_VOLTAGE_V", teal),
                    _demo_series("Current (A)", "DEMO_CURRENT_A", amber, axis="right"),
                    _demo_series("Power factor", "DEMO_POWER_FACTOR", purple, axis="right"),
                ]},
            ]
        },
    }

    multi_series = {
        "id": "tpl-demo-multi-series",
        "name": "Multi-Series Comparison",
        "description": "Compare any 6 tags on dual axes — temperatures, flows, pressures, vibration, you name it.",
        "definition": {
            "sections": [
                {"type": "header", "title": "Multi-Series Comparison", "subtitle": "Side-by-side overlay of up to six signals"},
                {"type": "text", "text": "When the root cause of a deviation isn't obvious, overlay every plausible signal at the same time-scale. This template ships with six placeholder series on a dual-axis chart you can re-map to your tags in seconds."},
                {"type": "kpi_grid", "title": "Series headlines", "columns": 3, "items": [
                    {"label": "Series 1 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_A", "operator": "any", "aggregation": "avg"},
                    {"label": "Series 2 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_B", "operator": "any", "aggregation": "avg"},
                    {"label": "Series 3 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_C", "operator": "any", "aggregation": "avg"},
                    {"label": "Series 4 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_D", "operator": "any", "aggregation": "avg"},
                    {"label": "Series 5 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_E", "operator": "any", "aggregation": "avg"},
                    {"label": "Series 6 avg", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_F", "operator": "any", "aggregation": "avg"},
                ]},
                {"type": "line_chart", "title": "Dual-axis overlay (24h)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SIGNAL_A", "time_range": {"preset": "24h"}, "readings_count": 480, "series": [
                    _demo_series("Signal A", "DEMO_SIGNAL_A", teal),
                    _demo_series("Signal B", "DEMO_SIGNAL_B", blue),
                    _demo_series("Signal C", "DEMO_SIGNAL_C", purple),
                    _demo_series("Signal D (right axis)", "DEMO_SIGNAL_D", amber, axis="right"),
                    _demo_series("Signal E (right axis)", "DEMO_SIGNAL_E", red, axis="right"),
                    _demo_series("Signal F (right axis)", "DEMO_SIGNAL_F", grey, axis="right"),
                ]},
                {"type": "table_list", "title": "Most recent values across all series", "gateway_id": "DEMO_GATEWAY", "tag_name": "", "list_limit": 30, "time_range": {"preset": "1h"}},
            ]
        },
    }

    quality = {
        "id": "tpl-demo-quality-yield",
        "name": "Quality & Yield",
        "description": "First-pass yield, reject reasons Pareto, SPC-style limits on the key dimension.",
        "definition": {
            "sections": [
                {"type": "header", "title": "Quality & Yield Report", "subtitle": "First-pass yield, reject drivers, dimensional control"},
                {"type": "text", "text": "What the line produced, how much of it passed first time, and where the rejects came from. Includes an SPC-style chart with upper/lower control limits on the critical dimension."},
                {"type": "kpi_grid", "title": "Quality KPIs", "columns": 4, "items": [
                    {"label": "Units produced", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_UNITS_PRODUCED", "operator": "any", "aggregation": "sum"},
                    {"label": "First-pass yield (%)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_FIRST_PASS_YIELD", "operator": "any", "aggregation": "avg"},
                    {"label": "Scrap (units)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_SCRAP_UNITS", "operator": "any", "aggregation": "sum"},
                    {"label": "Cpk (estimated)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_CPK", "operator": "any", "aggregation": "avg"},
                ]},
                {"type": "pie_chart", "title": "Reject reasons (Pareto)", "data_source_type": "computed", "gateway_id": "DEMO_GATEWAY", "compute_rules": [
                    {"id": "q1", "label": "Dimensional", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_REJECT_CODE", "operator": "eq", "value1": 1, "aggregation": "count", "color": red},
                    {"id": "q2", "label": "Surface defect", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_REJECT_CODE", "operator": "eq", "value1": 2, "aggregation": "count", "color": amber},
                    {"id": "q3", "label": "Weight", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_REJECT_CODE", "operator": "eq", "value1": 3, "aggregation": "count", "color": blue},
                    {"id": "q4", "label": "Label / barcode", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_REJECT_CODE", "operator": "eq", "value1": 4, "aggregation": "count", "color": purple},
                    {"id": "q5", "label": "Other", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_REJECT_CODE", "operator": "eq", "value1": 5, "aggregation": "count", "color": grey},
                ]},
                {"type": "line_chart", "title": "Critical dimension vs UCL/LCL", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_KEY_DIMENSION_MM", "time_range": {"preset": "24h"}, "readings_count": 720, "series": [
                    _demo_series("Measured (mm)", "DEMO_KEY_DIMENSION_MM", teal),
                    _demo_series("Target", "DEMO_KEY_DIMENSION_TARGET_MM", blue),
                    _demo_series("UCL", "DEMO_KEY_DIMENSION_UCL_MM", red, chart_type="line"),
                    _demo_series("LCL", "DEMO_KEY_DIMENSION_LCL_MM", red, chart_type="line"),
                ]},
                {"type": "bar_chart", "title": "Yield by shift (last 7 days)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_FIRST_PASS_YIELD", "time_range": {"preset": "7d"}, "readings_count": 21, "series": [
                    _demo_series("Shift A", "DEMO_YIELD_SHIFT_A", teal),
                    _demo_series("Shift B", "DEMO_YIELD_SHIFT_B", blue),
                    _demo_series("Shift C", "DEMO_YIELD_SHIFT_C", purple),
                ]},
            ]
        },
    }

    downtime = {
        "id": "tpl-demo-downtime-mttr",
        "name": "Downtime & MTTR",
        "description": "Mean-time-between-failure, mean-time-to-repair, top offending assets and a stops timeline.",
        "definition": {
            "sections": [
                {"type": "header", "title": "Downtime & MTTR", "subtitle": "Where the line stops, for how long, and how fast it recovers"},
                {"type": "text", "text": "Reliability-engineering view: MTBF / MTTR per asset, total stop minutes by line, and a timeline showing exactly when each stop happened over the last week. Pairs with the alarm event log."},
                {"type": "kpi_grid", "title": "Reliability KPIs", "columns": 4, "items": [
                    {"label": "Stops (7d)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_LINE_STOP", "operator": "eq", "value1": 1, "aggregation": "count"},
                    {"label": "MTBF (h)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_MTBF_HOURS", "operator": "any", "aggregation": "avg"},
                    {"label": "MTTR (min)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_MTTR_MINUTES", "operator": "any", "aggregation": "avg"},
                    {"label": "Total downtime (min)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_DOWNTIME_MINUTES", "operator": "any", "aggregation": "sum"},
                ]},
                {"type": "bar_chart", "title": "Downtime minutes by asset (7d)", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_DOWNTIME_MINUTES", "time_range": {"preset": "7d"}, "readings_count": 7, "series": [
                    _demo_series("Filler", "DEMO_DOWNTIME_FILLER", red),
                    _demo_series("Capper", "DEMO_DOWNTIME_CAPPER", amber),
                    _demo_series("Labeller", "DEMO_DOWNTIME_LABELLER", blue),
                    _demo_series("Palletiser", "DEMO_DOWNTIME_PALLETISER", purple),
                ]},
                {"type": "line_chart", "title": "Line state timeline", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_LINE_STATE", "time_range": {"preset": "7d"}, "readings_count": 1000, "series": [
                    _demo_series("Line state (0 = down, 1 = up)", "DEMO_LINE_STATE", teal, chart_type="area"),
                ]},
                {"type": "table_list", "title": "Recent stops with duration", "gateway_id": "DEMO_GATEWAY", "tag_name": "DEMO_LINE_STOP", "list_limit": 20, "time_range": {"preset": "7d"}},
            ]
        },
    }

    return [process_batch, oee, energy, multi_series, quality, downtime]
