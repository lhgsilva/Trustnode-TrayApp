"""Backfill local generated_reports to Supabase AND drain the
lite_report_requests queue — without needing to restart the edge backend.

This is a one-shot bridge tool for the case where:
  - the edge backend was running before the cloud-sync feature was added
  - it can't be restarted right now (PLC collection in progress, etc.)
  - but the user still wants their Lite app to see existing reports + any
    Generate requests they have queued.

What it does on each invocation:
  1. Reads every row from local trustnode_app_store.db / generated_reports
     and upserts it into public.generated_reports on Supabase.
  2. Reads every pending row from public.lite_report_requests, renders the
     PDF using the existing report-renderer module, inserts a local
     generated_reports row (which the next run mirrors), and marks the
     queue row 'done'.

Run from the Trustnode_edge_app directory:
    python scripts/sync_reports_to_lite.py [--limit 200]

The script reads cloud-DB credentials and (optionally) the Supabase
service-role key from Trustnode_edge_app/.env.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Ensure the backend package is importable so we can re-use the renderer.
HERE = Path(__file__).resolve().parent.parent  # Trustnode_edge_app/
sys.path.insert(0, str(HERE / "backend"))

import psycopg


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def resolve_reports_dir() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "reports"
    return Path.home() / ".trustnode_edge" / "data" / "reports"


def fetch_local_reports(db_path: Path, limit: int) -> list[dict]:
    if not db_path.is_file():
        print(f"  ! local DB not found at {db_path}")
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Probe whether storage_path exists yet (added by recent migration).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(generated_reports)").fetchall()}
        select_sp = "storage_path" if "storage_path" in cols else "NULL AS storage_path"
        rows = conn.execute(
            f"""
            SELECT id, tenant_id, template_id, template_name, schedule_id, schedule_name,
                   triggered_by, file_path, file_name, file_bytes, file_sha256,
                   created_utc, email_status, email_message, email_recipients_json,
                   meta_json, {select_sp}
              FROM generated_reports
             ORDER BY created_utc DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mirror_to_supabase(env: dict[str, str], rows: list[dict]) -> int:
    if not rows:
        return 0
    ok = 0
    with psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    ) as conn:
        cur = conn.cursor()
        for r in rows:
            try:
                cur.execute(
                    """
                    INSERT INTO public.generated_reports (
                      id, tenant_id, template_id, template_name,
                      schedule_id, schedule_name, triggered_by,
                      file_name, file_bytes, file_sha256, storage_path,
                      email_status, email_message, email_recipients_json,
                      meta_json, created_utc
                    ) VALUES (
                      %(id)s, %(tenant_id)s, %(template_id)s, %(template_name)s,
                      %(schedule_id)s, %(schedule_name)s, %(triggered_by)s,
                      %(file_name)s, %(file_bytes)s, %(file_sha256)s, %(storage_path)s,
                      %(email_status)s, %(email_message)s, %(email_recipients_json)s::jsonb,
                      %(meta_json)s::jsonb, %(created_utc)s
                    )
                    ON CONFLICT (tenant_id, id) DO UPDATE SET
                      template_name = EXCLUDED.template_name,
                      schedule_name = EXCLUDED.schedule_name,
                      triggered_by  = EXCLUDED.triggered_by,
                      file_name     = EXCLUDED.file_name,
                      file_bytes    = EXCLUDED.file_bytes,
                      file_sha256   = EXCLUDED.file_sha256,
                      storage_path  = COALESCE(EXCLUDED.storage_path, public.generated_reports.storage_path),
                      email_status  = EXCLUDED.email_status,
                      email_message = EXCLUDED.email_message,
                      meta_json     = EXCLUDED.meta_json
                    """,
                    {
                        "id": r["id"],
                        "tenant_id": r.get("tenant_id") or "default",
                        "template_id": r.get("template_id"),
                        "template_name": r.get("template_name"),
                        "schedule_id": r.get("schedule_id"),
                        "schedule_name": r.get("schedule_name"),
                        "triggered_by": r.get("triggered_by") or "manual",
                        "file_name": r.get("file_name") or "",
                        "file_bytes": int(r.get("file_bytes") or 0),
                        "file_sha256": r.get("file_sha256"),
                        "storage_path": r.get("storage_path"),
                        "email_status": r.get("email_status"),
                        "email_message": r.get("email_message"),
                        "email_recipients_json": r.get("email_recipients_json") or "[]",
                        "meta_json": r.get("meta_json") or "{}",
                        "created_utc": r.get("created_utc") or "",
                    },
                )
                ok += 1
            except Exception as exc:
                print(f"  ! skipped {r.get('id')}: {exc}")
        conn.commit()
    return ok


def upload_pdf_if_possible(env: dict[str, str], local_path: Path, *, tenant_id: str,
                           generated_id: str, file_name: str) -> str | None:
    """Push the PDF into the lite-reports bucket via Supabase Storage REST.

    Skipped silently when TRUSTNODE_SUPABASE_SERVICE_KEY is not set (this is
    expected for the first run before the user adds the key).
    """
    url = env.get("TRUSTNODE_SUPABASE_URL", "").rstrip("/")
    key = env.get("TRUSTNODE_SUPABASE_SERVICE_KEY", "")
    bucket = env.get("TRUSTNODE_SUPABASE_REPORTS_BUCKET", "lite-reports")
    if not (url and key and local_path.is_file()):
        return None
    import requests
    safe = file_name.replace("/", "_").replace("\\", "_")
    obj_path = f"{tenant_id}/{generated_id}__{safe}"
    endpoint = f"{url}/storage/v1/object/{bucket}/{obj_path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/pdf",
        "x-upsert": "true",
    }
    try:
        with local_path.open("rb") as fh:
            r = requests.post(endpoint, headers=headers, data=fh.read(), timeout=30)
        if r.status_code in (200, 201):
            return obj_path
        print(f"  ! storage upload {generated_id} HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"  ! storage upload {generated_id} failed: {exc}")
    return None


def update_local_storage_path(db_path: Path, generated_id: str, storage_path: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        # PRAGMA table_info returns tuples — index [1] is the column name.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(generated_reports)").fetchall()}
        if "storage_path" not in cols:
            conn.execute("ALTER TABLE generated_reports ADD COLUMN storage_path TEXT NULL")
        conn.execute("UPDATE generated_reports SET storage_path = ? WHERE id = ?",
                     (storage_path, generated_id))
        conn.commit()
    finally:
        conn.close()


def drain_queue(env: dict[str, str], db_path: Path) -> int:
    """Process every pending lite_report_requests row: render PDF, write
    local row, mark cloud request done. Mirroring to the cloud DB then
    happens in the same script's mirror step on the next pass.
    """
    # Lazy imports because the renderer pulls heavy deps (reportlab, etc.).
    from app.services.reports_store import ReportsStore
    from app.services.report_renderer import render_template_to_pdf

    store = ReportsStore()
    done = 0
    with psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    ) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, tenant_id, template_id, template_name, requester_email
              FROM public.lite_report_requests
             WHERE status = 'pending'
             ORDER BY requested_utc
             LIMIT 50
            """
        )
        pending = cur.fetchall()
        if not pending:
            return 0
        cur.execute(
            """
            UPDATE public.lite_report_requests
               SET status = 'running', started_utc = now()
             WHERE id = ANY(%s)
            """,
            ([row[0] for row in pending],),
        )
        conn.commit()

        for row in pending:
            req_id, tenant_id, template_id, template_name, requester = row
            print(f"  - processing request {req_id} for template {template_id}")
            try:
                template = store.get_template(str(template_id), tenant_id=str(tenant_id))
                if not template:
                    raise RuntimeError(f"Template '{template_id}' not found locally.")
                path, byte_count, sha = render_template_to_pdf(template)
                record = store.insert_generated({
                    "tenant_id": str(tenant_id),
                    "template_id": template.get("id"),
                    "template_name": template.get("name") or template_name,
                    "triggered_by": f"lite:{requester or 'viewer'}"[:64],
                    "file_path": str(path),
                    "file_name": path.name,
                    "file_bytes": byte_count,
                    "file_sha256": sha,
                })
                # Optional Storage upload.
                sp = upload_pdf_if_possible(
                    env, Path(record.get("file_path") or path),
                    tenant_id=str(tenant_id),
                    generated_id=record["id"],
                    file_name=record.get("file_name") or path.name,
                )
                if sp:
                    update_local_storage_path(db_path, record["id"], sp)
                cur.execute(
                    """
                    UPDATE public.lite_report_requests
                       SET status = 'done',
                           finished_utc = now(),
                           generated_id = %s,
                           error_message = NULL
                     WHERE id = %s
                    """,
                    (record["id"], req_id),
                )
                done += 1
            except Exception as exc:
                cur.execute(
                    """
                    UPDATE public.lite_report_requests
                       SET status = 'failed',
                           finished_utc = now(),
                           error_message = %s
                     WHERE id = %s
                    """,
                    (str(exc)[:500], req_id),
                )
                print(f"    failed: {exc}")
        conn.commit()
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200,
                    help="how many recent local generated_reports to mirror (default 200)")
    ap.add_argument("--skip-drain", action="store_true",
                    help="don't process the lite_report_requests queue")
    ap.add_argument("--skip-mirror", action="store_true",
                    help="don't push local generated_reports rows to Supabase")
    args = ap.parse_args()

    env = load_env(HERE / ".env")
    if not env.get("TRUSTNODE_CLOUD_DB_HOST"):
        print("ERROR: TRUSTNODE_CLOUD_DB_* not set in .env", file=sys.stderr)
        return 2
    db_path = resolve_local_db()
    print(f"== local DB: {db_path} ==")
    print(f"== reports dir: {resolve_reports_dir()} ==")

    if not args.skip_drain:
        print("\n== Draining lite_report_requests queue ==")
        try:
            n = drain_queue(env, db_path)
            print(f"   processed {n} pending request(s)")
        except Exception as exc:
            print(f"   queue drain skipped: {exc}")

    if not args.skip_mirror:
        print(f"\n== Mirroring local generated_reports -> Supabase (limit={args.limit}) ==")
        rows = fetch_local_reports(db_path, args.limit)
        print(f"   {len(rows)} local rows to mirror")
        if rows:
            # Best-effort upload of PDFs that don't have a storage_path yet.
            sk = env.get("TRUSTNODE_SUPABASE_SERVICE_KEY", "")
            if sk:
                print("   uploading PDFs for rows without storage_path…")
                uploaded = 0
                for r in rows:
                    if r.get("storage_path"):
                        continue
                    p = Path(r.get("file_path") or "")
                    if not p.is_file():
                        continue
                    sp = upload_pdf_if_possible(env, p,
                                                tenant_id=r.get("tenant_id") or "default",
                                                generated_id=r["id"],
                                                file_name=r.get("file_name") or p.name)
                    if sp:
                        r["storage_path"] = sp
                        update_local_storage_path(db_path, r["id"], sp)
                        uploaded += 1
                print(f"   uploaded {uploaded} PDFs to lite-reports bucket")
            else:
                print("   (TRUSTNODE_SUPABASE_SERVICE_KEY not set — skipping PDF uploads)")
            ok = mirror_to_supabase(env, rows)
            print(f"   mirrored {ok}/{len(rows)} rows")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
