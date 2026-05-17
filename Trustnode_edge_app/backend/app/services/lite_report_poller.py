"""Polls `public.lite_report_requests` on Supabase and renders any pending
report the Lite (cloud) app has asked for.

Architecture:

  Lite app -> INSERT pending row -> Supabase
                                       │
                                       ▼
  Edge backend ── polls every N s ─────┘
       │
       ├─ marks row 'running'
       ├─ renders the PDF via ReportRunner
       ├─ row becomes 'done' (with generated_id) on success
       └─ row becomes 'failed' (with error_message) on error

The PDF + generated_reports row mirror flow is reused (`ReportsStore.insert_generated`
schedules the upload-and-mirror automatically), so the Lite app sees the new
report appear in `generated_reports` realtime with a working `storage_path`.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class LiteReportRequestPoller:
    """Background thread that drains the `lite_report_requests` queue.

    Mirrors `ReportScheduler`'s style: starts a daemon thread, sleeps
    `tick_seconds`, scans pending rows, dispatches them through the same
    `ReportRunner` used by HTTP `/render` and the timer-based scheduler.
    """

    def __init__(
        self,
        runner,
        reports_store,
        *,
        tick_seconds: int = 10,
        get_cloud_target: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.runner = runner
        self.store = reports_store
        self.tick_seconds = max(5, int(tick_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Lazy-resolved each tick so DB target changes take effect without
        # a restart. Defer the import so we don't pull state.py during the
        # ReportScheduler module init (circular import risk).
        if get_cloud_target is None:
            def _resolve_target() -> dict[str, Any] | None:
                try:
                    from app.state import app_store  # use the singleton
                    return app_store._get_cloud_database_target()  # type: ignore[attr-defined]
                except Exception:
                    return None
            get_cloud_target = _resolve_target
        self._get_cloud_target = get_cloud_target

    # ---- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="tn-lite-report-poller", daemon=True
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

    # ---- loop ------------------------------------------------------------- #
    def _loop(self) -> None:
        # Initial small delay so the main app boot finishes first.
        if self._stop_event.wait(2.0):
            return
        # One-shot backfill: push any local generated_reports rows that have
        # never been mirrored to Supabase. Lets Lite see historical reports.
        try:
            self._backfill_existing_rows()
        except Exception as exc:
            log.debug("lite-report-poller backfill failed: %s", exc)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.debug("lite-report-poller tick failed: %s", exc)
            if self._stop_event.wait(self.tick_seconds):
                return

    def _backfill_existing_rows(self) -> None:
        """Mirror local generated_reports rows that are missing in Supabase.

        Runs once at boot. Heavily throttled because the same SQLAlchemy
        connection pool is shared with the historian writer — flooding it
        here would stall live data ingest. Skips entirely once Supabase has
        a non-trivial number of mirrored rows (idempotency).
        """
        cloud = self._get_cloud_target() if callable(self._get_cloud_target) else None
        if not cloud:
            return
        # Skip when cloud already has a healthy backlog — no need to rewrite.
        try:
            from sqlalchemy import text  # type: ignore
            schema = str(cloud.get("schema") or "public")
            try:
                from app.state import app_store
                store_engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)  # type: ignore[attr-defined]
                with store_engine.begin() as conn:
                    cur = conn.execute(text(f'SELECT count(*) FROM "{schema}".generated_reports'))
                    n_cloud = int((cur.fetchone() or (0,))[0])
            except Exception:
                n_cloud = 0
        except Exception:
            n_cloud = 0
        if n_cloud >= 50:
            log.info("lite-report-poller: cloud already has %d generated_reports — skip backfill", n_cloud)
            return

        try:
            from app.services.reports_cloud_uploader import mirror_generated_row
        except Exception:
            return
        try:
            rows = self.store.list_generated(limit=50)  # smaller cap
        except Exception:
            return
        if not rows:
            return
        ok = 0
        import time as _time
        for r in rows:
            sp = r.get("storage_path")
            try:
                if mirror_generated_row(r, sp):
                    ok += 1
            except Exception:
                continue
            # Yield between upserts so historian writes don't get starved.
            _time.sleep(0.05)
        log.info("lite-report-poller: backfilled %d/%d generated_reports rows", ok, len(rows))

    def _engine(self):
        cloud = self._get_cloud_target() if callable(self._get_cloud_target) else None
        if not cloud:
            return None, None
        try:
            # Use the singleton — sharing one engine/pool across the whole
            # process keeps Supabase's session-pooler quota from running out.
            from app.state import app_store
            schema = str(cloud.get("schema") or "public")
            engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)  # type: ignore[attr-defined]
            return engine, schema
        except Exception as exc:
            log.debug("lite-report-poller engine resolution failed: %s", exc)
            return None, None

    def _tick(self) -> None:
        engine, schema = self._engine()
        if engine is None:
            return
        from sqlalchemy import text  # type: ignore

        # Claim up to N pending requests atomically — flipping them to
        # 'running' the same moment we read them prevents a second poller
        # (or a restarted instance) from double-rendering.
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    UPDATE "{schema}".lite_report_requests
                       SET status = 'running', started_utc = now()
                     WHERE id IN (
                       SELECT id FROM "{schema}".lite_report_requests
                        WHERE status = 'pending'
                        ORDER BY requested_utc
                        LIMIT 5
                        FOR UPDATE SKIP LOCKED
                     )
                     RETURNING id, tenant_id, template_id, template_name, requester_email
                    """
                )
            ).mappings().all()

        for row in rows:
            self._process(engine, schema, dict(row))

    def _process(self, engine, schema: str, row: dict[str, Any]) -> None:
        from sqlalchemy import text  # type: ignore

        request_id = str(row.get("id") or "")
        tenant_id = str(row.get("tenant_id") or "default")
        template_id = str(row.get("template_id") or "")
        if not (request_id and template_id):
            self._mark_failed(engine, schema, request_id, "Invalid request payload")
            return

        # The template lives in the LOCAL reports_store — the edge owns the
        # source of truth for template definitions; the Supabase mirror is
        # only used by Lite for browsing. So look it up locally.
        try:
            template = self.store.get_template(template_id, tenant_id=tenant_id)
        except Exception as exc:
            self._mark_failed(engine, schema, request_id, f"Template lookup failed: {exc}")
            return
        if not template:
            self._mark_failed(engine, schema, request_id, f"Template '{template_id}' not found on edge.")
            return

        # Build a synthetic schedule shell so we can reuse ReportRunner. We
        # only need the tenant + template fields; email delivery stays off.
        triggered_by = f"lite:{row.get('requester_email') or 'viewer'}"[:64]
        synth_schedule = {
            "id": f"lite-{request_id[:8]}",
            "tenant_id": tenant_id,
            "template_id": template_id,
            "name": row.get("template_name") or template.get("name"),
            "deliver_email": False,
        }
        try:
            result = self.runner.run(synth_schedule, triggered_by=triggered_by)
        except Exception as exc:
            self._mark_failed(engine, schema, request_id, f"Render failed: {exc}")
            return

        if not result.get("ok"):
            self._mark_failed(engine, schema, request_id, str(result.get("error") or "Unknown render error"))
            return

        generated = result.get("generated") or {}
        generated_id = str(generated.get("id") or "")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE "{schema}".lite_report_requests
                       SET status = 'done',
                           finished_utc = now(),
                           generated_id = :gid,
                           error_message = NULL
                     WHERE id = :rid
                    """
                ),
                {"gid": generated_id or None, "rid": request_id},
            )

    def _mark_failed(self, engine, schema: str, request_id: str, message: str) -> None:
        if not request_id:
            return
        try:
            from sqlalchemy import text  # type: ignore
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        UPDATE "{schema}".lite_report_requests
                           SET status = 'failed',
                               finished_utc = now(),
                               error_message = :msg
                         WHERE id = :rid
                        """
                    ),
                    {"msg": message[:500], "rid": request_id},
                )
        except Exception as exc:
            log.debug("lite-report-poller failed to mark error on %s: %s", request_id, exc)
