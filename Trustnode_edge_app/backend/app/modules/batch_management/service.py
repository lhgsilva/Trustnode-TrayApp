"""BatchService — all SQLite access for the Batch Management module.

Lives in its own module so:
  * everything can be reviewed/disabled in one place,
  * the existing app_store.py stays focused on the historian/config,
  * future enhancements (cloud sync, multi-site) bolt on without
    cluttering the core store.

Important guarantees:
  * Never writes to historian_readings, gateway_configurations, or any
    table outside batch_*. The module is purely additive.
  * Every mutation writes to batch_audit_log on the same connection
    before commit, so a partial commit cannot lose the audit row.
  * Every read filters by tenant_id, matching the rest of the app.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except Exception:
        return None


def _json_load(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _opt_num(value: Any) -> Optional[float]:
    """Parse an optional numeric (limit/threshold) value; None if blank/invalid."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _percentile(sorted_vals: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile over an already-sorted list. pct in [0,100]."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    import math as _m
    rank = _m.ceil((pct / 100.0) * n)
    idx = min(max(rank - 1, 0), n - 1)
    return sorted_vals[idx]


def _window_seconds(start: str, end: str) -> float:
    """Seconds between two 'YYYY-MM-DD HH:MM:SS.fff' timestamps; 0 on any error."""
    from datetime import datetime as _dt
    def _p(s: str):
        s = str(s or "")[:23]
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return _dt.strptime(s[:len(fmt) + 4] if ".%f" in fmt else s[:19], fmt)
            except Exception:
                continue
        return None
    a, b = _p(start), _p(end)
    if not a or not b:
        return 0.0
    try:
        return max(0.0, (b - a).total_seconds())
    except Exception:
        return 0.0


class BatchService:
    """Per-process service. Holds a reference to the AppStore so it can
    reuse the read/write SQLite connections + tenant context, but adds
    NO global state of its own."""

    def __init__(self, app_store) -> None:
        self._app_store = app_store

    # -- connection helpers -------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return self._app_store._connect()

    def _connect_readonly(self) -> sqlite3.Connection:
        return self._app_store._connect_readonly()

    def _tenant(self) -> str:
        return self._app_store._current_tenant_id()

    # -- batch types ---------------------------------------------------
    def list_batch_types(self) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT id, name, description, parent_type_id, start_method, end_method,
                       start_config_json, end_config_json, collection_profile,
                       report_template_id, identifier_method, identifier_prefix,
                       summary_tags_json, enabled, created_utc, updated_utc,
                       email_on_close, email_recipients,
                       trigger_start_json, trigger_stop_json,
                       start_schedule_json, stop_schedule_json, report_schedule_json,
                       last_scheduled_start_utc, last_scheduled_stop_utc, last_report_utc,
                       batch_kind, child_type_id,
                       pass_rule, manual_fields_json, chart_config_json
                FROM batch_types
                WHERE tenant_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (tid,),
            ).fetchall()
        return [self._row_to_batch_type(r) for r in rows]

    def get_batch_type(self, batch_type_id: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            row = c.execute(
                """
                SELECT id, name, description, parent_type_id, start_method, end_method,
                       start_config_json, end_config_json, collection_profile,
                       report_template_id, identifier_method, identifier_prefix,
                       summary_tags_json, enabled, created_utc, updated_utc,
                       email_on_close, email_recipients,
                       trigger_start_json, trigger_stop_json,
                       start_schedule_json, stop_schedule_json, report_schedule_json,
                       last_scheduled_start_utc, last_scheduled_stop_utc, last_report_utc,
                       batch_kind, child_type_id,
                       pass_rule, manual_fields_json, chart_config_json
                FROM batch_types
                WHERE tenant_id = ? AND id = ?
                """,
                (tid, batch_type_id),
            ).fetchone()
        return self._row_to_batch_type(row) if row else None

    def save_batch_type(self, payload: dict[str, Any], *, actor: Optional[str] = None,
                        batch_type_id: Optional[str] = None) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        is_new = not batch_type_id
        bt_id = batch_type_id or _new_id("bt")
        record = (
            bt_id, tid,
            str(payload.get("name") or "").strip() or "Untitled",
            payload.get("description"),
            payload.get("parent_type_id"),
            payload.get("start_method") or "manual",
            payload.get("end_method") or "manual",
            _json_or_none(payload.get("start_config")),
            _json_or_none(payload.get("end_config")),
            payload.get("collection_profile") or "continuous",
            payload.get("report_template_id"),
            payload.get("identifier_method") or "auto",
            payload.get("identifier_prefix"),
            _json_or_none(payload.get("summary_tags")),
            1 if payload.get("enabled", True) else 0,
            now, now,
            # Operator 2026-06-30: new email + trigger columns.
            1 if payload.get("email_on_close") else 0,
            (payload.get("email_recipients") or None),
            _json_or_none(payload.get("trigger_start")),
            _json_or_none(payload.get("trigger_stop")),
            # Operator 2026-07-06: schedule columns. last_*_utc are NOT set here
            # (the daemon owns them); on create they start NULL, and on update we
            # deliberately leave them untouched via COALESCE below.
            _json_or_none(payload.get("start_schedule")),
            _json_or_none(payload.get("stop_schedule")),
            _json_or_none(payload.get("report_schedule")),
            # Operator 2026-07-06: single/multiple model.
            (payload.get("batch_kind") or "single"),
            (payload.get("child_type_id") or None),
            (payload.get("pass_rule") or "any_out_of_spec"),
            _json_or_none(payload.get("manual_fields")),
            _json_or_none(payload.get("chart_config")),
        )
        with self._connect() as c:
            if is_new:
                c.execute(
                    """
                    INSERT INTO batch_types
                    (id, tenant_id, name, description, parent_type_id, start_method, end_method,
                     start_config_json, end_config_json, collection_profile, report_template_id,
                     identifier_method, identifier_prefix, summary_tags_json, enabled,
                     created_utc, updated_utc,
                     email_on_close, email_recipients,
                     trigger_start_json, trigger_stop_json,
                     start_schedule_json, stop_schedule_json, report_schedule_json,
                     batch_kind, child_type_id,
                     pass_rule, manual_fields_json, chart_config_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    record,
                )
                self._audit(c, tid=tid, actor=actor, batch_type_id=bt_id,
                            action="batch_type.create", after=record_to_dict(record))
            else:
                before = c.execute(
                    "SELECT * FROM batch_types WHERE tenant_id = ? AND id = ?",
                    (tid, bt_id),
                ).fetchone()
                if not before:
                    raise KeyError(bt_id)
                c.execute(
                    """
                    UPDATE batch_types
                    SET name = ?, description = ?, parent_type_id = ?, start_method = ?,
                        end_method = ?, start_config_json = ?, end_config_json = ?,
                        collection_profile = ?, report_template_id = ?, identifier_method = ?,
                        identifier_prefix = ?, summary_tags_json = ?, enabled = ?,
                        updated_utc = ?,
                        email_on_close = ?, email_recipients = ?,
                        trigger_start_json = ?, trigger_stop_json = ?,
                        start_schedule_json = ?, stop_schedule_json = ?, report_schedule_json = ?,
                        batch_kind = ?, child_type_id = ?,
                        pass_rule = ?, manual_fields_json = ?, chart_config_json = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        record[2], record[3], record[4], record[5], record[6], record[7],
                        record[8], record[9], record[10], record[11], record[12], record[13],
                        record[14], now,
                        record[17], record[18], record[19], record[20],
                        record[21], record[22], record[23],
                        record[24], record[25],
                        record[26], record[27], record[28],
                        tid, bt_id,
                    ),
                )
                self._audit(c, tid=tid, actor=actor, batch_type_id=bt_id,
                            action="batch_type.update",
                            before=dict(before) if before else None,
                            after=record_to_dict(record))
            c.commit()
        out = self.get_batch_type(bt_id)
        assert out is not None
        return out

    def delete_batch_type(self, batch_type_id: str, *, actor: Optional[str] = None) -> bool:
        tid = self._tenant()
        with self._connect() as c:
            before = c.execute(
                "SELECT * FROM batch_types WHERE tenant_id = ? AND id = ?",
                (tid, batch_type_id),
            ).fetchone()
            if not before:
                return False
            c.execute("DELETE FROM batch_types WHERE tenant_id = ? AND id = ?", (tid, batch_type_id))
            self._audit(c, tid=tid, actor=actor, batch_type_id=batch_type_id,
                        action="batch_type.delete", before=dict(before))
            c.commit()
        return True

    def seed_default_types(self) -> bool:
        """On a tenant that has NO batch types yet, create the two starter
        types so the module works out of the box:
          • 'Single Batch'   — one continuous run, manual start/stop.
          • 'Multiple Batch'  — a parent that auto-spawns 'Single Batch' children.
        Idempotent: does nothing if any type already exists. Returns True if it
        seeded. Operator 2026-07-06."""
        try:
            existing = self.list_batch_types()
        except Exception:
            return False
        if existing:
            return False
        try:
            single = self.save_batch_type({
                "name": "Single Batch",
                "description": "One continuous collection run with a clear start and stop "
                               "(manual button, barcode, or a tag condition).",
                "batch_kind": "single",
                "start_method": "manual",
                "end_method": "manual",
                "collection_profile": "continuous",
                "identifier_method": "auto",
                "identifier_prefix": "SINGLE",
                "enabled": True,
            }, actor="system:seed")
            self.save_batch_type({
                "name": "Multiple Batch",
                "description": "A parent run that automatically starts a new 'Single Batch' each "
                               "time its start condition fires, until the parent is stopped.",
                "batch_kind": "multiple",
                "child_type_id": single["id"],
                "start_method": "manual",
                "end_method": "manual",
                "collection_profile": "continuous",
                "identifier_method": "auto",
                "identifier_prefix": "MULTI",
                "enabled": True,
            }, actor="system:seed")
            return True
        except Exception:
            return False

    # -- per-type spec limits (operator 2026-07-09) -------------------
    def list_type_limits(self, batch_type_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT tag_name, lower_limit, upper_limit, warn_lower, warn_upper, "
                "in_spec_pct_min, unit, enabled FROM batch_type_limits "
                "WHERE tenant_id = ? AND batch_type_id = ? ORDER BY tag_name COLLATE NOCASE",
                (tid, batch_type_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_type_limits(self, batch_type_id: str, limits: list[dict[str, Any]],
                        *, actor: Optional[str] = None) -> list[dict[str, Any]]:
        """Replace the full set of spec limits for a type (one row per tag)."""
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            c.execute("DELETE FROM batch_type_limits WHERE tenant_id = ? AND batch_type_id = ?",
                      (tid, batch_type_id))
            for lim in (limits or []):
                tag = str(lim.get("tag_name") or "").strip()
                if not tag:
                    continue
                c.execute(
                    """
                    INSERT INTO batch_type_limits
                    (batch_type_id, tenant_id, tag_name, lower_limit, upper_limit,
                     warn_lower, warn_upper, in_spec_pct_min, unit, enabled, created_utc, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_type_id, tid, tag,
                        _opt_num(lim.get("lower_limit")), _opt_num(lim.get("upper_limit")),
                        _opt_num(lim.get("warn_lower")), _opt_num(lim.get("warn_upper")),
                        _opt_num(lim.get("in_spec_pct_min")),
                        (str(lim.get("unit")) if lim.get("unit") else None),
                        0 if lim.get("enabled") is False else 1, now, now,
                    ),
                )
            self._audit(c, tid=tid, actor=actor, batch_type_id=batch_type_id,
                        action="batch_type.set_limits", after={"count": len(limits or [])})
            c.commit()
        return self.list_type_limits(batch_type_id)

    def _limits_map_for_batch(self, batch_id: str) -> dict[str, dict[str, Any]]:
        """{tag_lower -> limit dict} for the batch's TYPE. Empty if none."""
        b = self.get_batch(batch_id)
        bt_id = (b or {}).get("batch_type_id")
        if not bt_id:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for lim in self.list_type_limits(bt_id):
            if lim.get("enabled") is False:
                continue
            out[str(lim.get("tag_name") or "").lower()] = lim
        return out

    # -- manual entries (operator 2026-07-09) ------------------------
    def list_manual_entries(self, batch_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT field_key, field_label, value_text, value_num, entered_by, entered_utc "
                "FROM batch_manual_entries WHERE tenant_id = ? AND batch_id = ? "
                "ORDER BY entered_utc ASC",
                (tid, batch_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_manual_entries(self, batch_id: str, entries: list[dict[str, Any]],
                           *, actor: Optional[str] = None) -> list[dict[str, Any]]:
        """Replace the manual-entry set for a batch (idempotent full-set save)."""
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            c.execute("DELETE FROM batch_manual_entries WHERE tenant_id = ? AND batch_id = ?",
                      (tid, batch_id))
            for e in (entries or []):
                key = str(e.get("field_key") or e.get("key") or "").strip()
                if not key:
                    continue
                vt = e.get("value_text")
                vt = str(vt) if vt is not None else None
                c.execute(
                    """
                    INSERT INTO batch_manual_entries
                    (batch_id, tenant_id, field_key, field_label, value_text, value_num,
                     entered_by, entered_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (batch_id, tid, key, (str(e.get("field_label")) if e.get("field_label") else None),
                     vt, _opt_num(e.get("value_num")), actor, now),
                )
            self._event(c, batch_id=batch_id, kind="batch.manual_entry", actor=actor,
                        message=f"{len(entries or [])} manual field(s) saved")
            c.commit()
        return self.list_manual_entries(batch_id)

    # -- batches -------------------------------------------------------
    def list_batches(self, *, limit: int = 200, offset: int = 0,
                     status_filter: Optional[str] = None,
                     batch_type_id: Optional[str] = None,
                     parent_batch_id: Optional[str] = None,
                     search: Optional[str] = None) -> tuple[list[dict[str, Any]], int]:
        tid = self._tenant()
        where = "WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if status_filter:
            where += " AND status = ?"
            params.append(status_filter)
        if batch_type_id:
            where += " AND batch_type_id = ?"
            params.append(batch_type_id)
        if parent_batch_id:
            where += " AND parent_batch_id = ?"
            params.append(parent_batch_id)
        if search:
            where += " AND (identifier LIKE ? OR product LIKE ? OR operator LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        with self._connect_readonly() as c:
            total = int(c.execute(f"SELECT COUNT(*) FROM batches {where}", params).fetchone()[0])
            rows = c.execute(
                f"""
                SELECT * FROM batches
                {where}
                ORDER BY COALESCE(started_utc, created_utc) DESC
                LIMIT ? OFFSET ?
                """,
                (*params, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [self._row_to_batch(r) for r in rows], total

    def get_batch(self, batch_id: str) -> Optional[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE tenant_id = ? AND id = ?",
                (tid, batch_id),
            ).fetchone()
        return self._row_to_batch(row) if row else None

    def create_batch(self, payload: dict[str, Any], *, actor: Optional[str] = None) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        bid = _new_id("b")
        identifier = payload.get("identifier")
        ident_method = payload.get("identifier_method")
        bt_id = payload.get("batch_type_id")
        # Auto-identifier if requested
        if not identifier:
            bt = self.get_batch_type(bt_id) if bt_id else None
            prefix = (bt or {}).get("identifier_prefix") or "BATCH"
            date_token = datetime.now(timezone.utc).strftime("%Y%m%d")
            identifier = f"{prefix}-{date_token}-{bid[-6:]}"
            ident_method = ident_method or "auto"
        record = (
            bid, tid, bt_id, payload.get("parent_batch_id"),
            identifier, ident_method, "created",
            None, None,
            payload.get("operator"), payload.get("source") or "api",
            payload.get("gateway_id"), payload.get("product"), payload.get("recipe"),
            payload.get("notes"), _json_or_none(payload.get("metadata")),
            now, now,
        )
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO batches
                (id, tenant_id, batch_type_id, parent_batch_id, identifier, identifier_method,
                 status, started_utc, ended_utc, operator, source, gateway_id, product, recipe,
                 notes, metadata_json, created_utc, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                record,
            )
            self._event(c, batch_id=bid, kind="batch.created", actor=actor,
                        message=f"Batch created (id={identifier})")
            self._audit(c, tid=tid, actor=actor, batch_id=bid,
                        action="batch.create", after={"id": bid, "identifier": identifier})
            c.commit()
        out = self.get_batch(bid)
        assert out is not None
        return out

    def start_batch(self, batch_id: str, *, operator: Optional[str] = None,
                    notes: Optional[str] = None, gateway_id: Optional[str] = None,
                    actor: Optional[str] = None, source: str = "manual") -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE tenant_id = ? AND id = ?",
                (tid, batch_id),
            ).fetchone()
            if not row:
                raise KeyError(batch_id)
            if row["status"] in ("running",):
                # Idempotent
                return self.get_batch(batch_id)  # type: ignore[return-value]
            c.execute(
                """
                UPDATE batches
                SET status = 'running', started_utc = COALESCE(started_utc, ?),
                    operator = COALESCE(?, operator), gateway_id = COALESCE(?, gateway_id),
                    notes = COALESCE(?, notes), source = COALESCE(?, source),
                    updated_utc = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (now, operator, gateway_id, notes, source, now, tid, batch_id),
            )
            # Open a membership window for the historian join.
            c.execute(
                """
                INSERT INTO batch_membership
                (batch_id, tenant_id, gateway_id, ts_utc_start, ts_utc_end, created_utc)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (batch_id, tid, gateway_id or row["gateway_id"], now, now),
            )
            self._event(c, batch_id=batch_id, kind="batch.started", actor=actor,
                        message=f"Batch started via {source}")
            self._audit(c, tid=tid, actor=actor, batch_id=batch_id,
                        action="batch.start", after={"started_utc": now, "source": source})
            c.commit()
        # Operator 2026-07-06: if a MULTIPLE parent was just started (and this
        # isn't the internal spawn of a child), open its first child so the run
        # begins collecting immediately. source=='multiple' means we ARE the
        # child being started — never recurse in that case.
        if source != "multiple":
            try:
                bt = self.get_batch_type(row["batch_type_id"]) if row["batch_type_id"] else None
                if bt and str(bt.get("batch_kind")) == "multiple":
                    self.spawn_child_for_parent(batch_id, actor=actor or "system:multiple")
            except Exception:
                pass
        return self.get_batch(batch_id)  # type: ignore[return-value]

    def stop_batch(self, batch_id: str, *, result: str = "completed",
                   operator: Optional[str] = None, notes: Optional[str] = None,
                   actor: Optional[str] = None, source: str = "manual") -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        if result not in ("completed", "failed", "cancelled"):
            result = "completed"
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE tenant_id = ? AND id = ?",
                (tid, batch_id),
            ).fetchone()
            if not row:
                raise KeyError(batch_id)
            if row["status"] not in ("running", "waiting"):
                return self.get_batch(batch_id)  # type: ignore[return-value]
            c.execute(
                """
                UPDATE batches
                SET status = ?, ended_utc = ?,
                    operator = COALESCE(?, operator),
                    notes = COALESCE(?, notes),
                    updated_utc = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (result, now, operator, notes, now, tid, batch_id),
            )
            # Close the open membership window.
            c.execute(
                """
                UPDATE batch_membership SET ts_utc_end = ?
                WHERE batch_id = ? AND ts_utc_end IS NULL
                """,
                (now, batch_id),
            )
            self._event(c, batch_id=batch_id, kind=f"batch.{result}", actor=actor,
                        message=f"Batch ended ({result}) via {source}")
            self._audit(c, tid=tid, actor=actor, batch_id=batch_id,
                        action="batch.stop",
                        after={"ended_utc": now, "result": result, "source": source})
            c.commit()
        # Operator 2026-07-06: if this is a MULTIPLE parent, cascade-close its
        # currently-open child so stopping the parent finalizes the whole run.
        try:
            if (row["open_child_batch_id"] if "open_child_batch_id" in row.keys() else None):
                self.close_open_child(batch_id, result=result, actor=actor)
        except Exception:
            pass
        # Operator 2026-07-06: reshape the membership window per the type's
        # collection_profile (snapshot / pre_post) BEFORE computing summaries so
        # the stats reflect the intended scope. No-op for continuous.
        try:
            self._apply_collection_profile(batch_id)
        except Exception:
            pass
        # Best-effort summary compute. Failures are non-fatal — operator can
        # recompute manually.
        try:
            self.compute_summaries(batch_id)
        except Exception:
            pass
        # Operator 2026-06-30: fire email-on-close if the batch_type asked for it.
        # Best-effort; failure does not block the batch state transition.
        try:
            self._maybe_send_close_email(batch_id)
        except Exception as _email_exc:
            try:
                with self._connect() as c:
                    self._event(c, batch_id=batch_id, kind="batch.email_failed",
                                severity="warning",
                                message=f"Close-email failed: {type(_email_exc).__name__}: {_email_exc}")
                    c.commit()
            except Exception:
                pass
        return self.get_batch(batch_id)  # type: ignore[return-value]

    def _maybe_send_close_email(self, batch_id: str) -> None:
        """If the batch's type has email_on_close=True and has recipients,
        send the PDF report. Reuses the global SMTP/PHP transport config
        from app_settings → email_notifications.settings."""
        batch = self.get_batch(batch_id)
        if not batch:
            return
        bt = self.get_batch_type(batch.get("batch_type_id") or "") if batch.get("batch_type_id") else None
        if not bt or not bt.get("email_on_close"):
            return
        recipients = [s.strip() for s in str(bt.get("email_recipients") or "").replace(";", ",").split(",") if s.strip()]
        if not recipients:
            return
        # Pull current email settings from app_settings.
        try:
            bs = self._app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            email_block = bs.get("email_notifications") or {}
            email_settings = email_block.get("settings") or email_block or {}
        except Exception:
            email_settings = {}
        # Build PDF in-memory.
        try:
            from .reports import render_single_batch_pdf
            events = self.list_events(batch_id, limit=2000)
            summaries = self.list_summaries(batch_id)
            manual = self.list_manual_entries(batch_id)
            pdf_bytes = render_single_batch_pdf(batch, bt, events, summaries, manual)
        except Exception as exc:
            with self._connect() as c:
                self._event(c, batch_id=batch_id, kind="batch.email_failed",
                            severity="warning",
                            message=f"PDF build failed: {exc}")
                c.commit()
            return
        # Send via shared notifications helper.
        try:
            import base64 as _b64
            from app.routers.notifications import (
                EmailRequest, SMTPConfig, PHPMailConfig, EmailAttachment, send_email_request,
            )
        except Exception as exc:
            return
        transport = str(email_settings.get("transport") or "smtp").strip().lower()
        smtp = email_settings.get("smtp") or {}
        php = email_settings.get("php_mail")
        subject = f"Batch {batch.get('identifier') or batch.get('id')} — {batch.get('status') or 'closed'}"
        body = (
            f"<p>Batch <b>{batch.get('identifier') or batch.get('id')}</b> closed with status "
            f"<b>{batch.get('status')}</b>.</p>"
            f"<p>Product: {batch.get('product') or '—'} · Recipe: {batch.get('recipe') or '—'}</p>"
            f"<p>Started: {batch.get('started_utc') or '—'} → Ended: {batch.get('ended_utc') or '—'} (UTC)</p>"
            f"<p>PDF attached.</p>"
        )
        try:
            attach = EmailAttachment(
                filename=f"batch_{batch.get('identifier') or batch.get('id')}.pdf",
                content_b64=_b64.b64encode(pdf_bytes).decode("ascii"),
                content_type="application/pdf",
            )
        except Exception:
            return
        req = EmailRequest(
            transport="php_http" if transport == "php_http" else "smtp",
            smtp=SMTPConfig(**(smtp if isinstance(smtp, dict) else {})),
            php_mail=PHPMailConfig(**(php if isinstance(php, dict) else {})) if isinstance(php, dict) else None,
            to=recipients,
            cc=[], bcc=[],
            subject=subject,
            html_body=body,
            text_body=f"Batch {batch.get('identifier') or batch.get('id')} closed",
            attachments=[attach],
        )
        outcome = send_email_request(req)
        ok = bool(getattr(outcome, "ok", False))
        with self._connect() as c:
            self._event(c, batch_id=batch_id,
                        kind="batch.email_sent" if ok else "batch.email_failed",
                        severity="info" if ok else "warning",
                        message=(f"Close-email sent to {', '.join(recipients)}" if ok
                                 else f"Email failed: {getattr(outcome, 'message', '?')}"))
            c.commit()

    # -- Single/Multiple auto-spawn (operator 2026-07-06) --------------
    def _set_open_child(self, parent_id: str, child_id: Optional[str]) -> None:
        tid = self._tenant()
        with self._connect() as c:
            c.execute(
                "UPDATE batches SET open_child_batch_id = ?, updated_utc = ? WHERE tenant_id = ? AND id = ?",
                (child_id, _utc_now(), tid, parent_id),
            )
            c.commit()

    def spawn_child_for_parent(self, parent_id: str, *, actor: str = "system:multiple") -> Optional[dict[str, Any]]:
        """Open a NEW child Single batch under a running Multiple parent, if the
        parent has no currently-open child. The child's type is the parent
        type's child_type_id. Returns the child batch dict (or None if not
        applicable / a child is already open). Called when the parent's START
        condition fires again (each fire = a new sub-batch)."""
        parent = self.get_batch(parent_id)
        if not parent or parent.get("status") != "running":
            return None
        # Already have an open child? Don't double-open.
        open_id = parent.get("open_child_batch_id")
        if open_id:
            oc = self.get_batch(open_id)
            if oc and oc.get("status") == "running":
                return None
        ptype = self.get_batch_type(parent.get("batch_type_id") or "") if parent.get("batch_type_id") else None
        child_type_id = (ptype or {}).get("child_type_id")
        child = self.create_batch({
            "batch_type_id": child_type_id,
            "parent_batch_id": parent_id,
            "operator": parent.get("operator"),
            "gateway_id": parent.get("gateway_id"),
            "product": parent.get("product"),
            "recipe": parent.get("recipe"),
            "source": "multiple",
            "metadata": {"source": "multiple", "parent": parent_id},
        }, actor=actor)
        self.start_batch(child["id"], gateway_id=parent.get("gateway_id"),
                         actor=actor, source="multiple")
        self._set_open_child(parent_id, child["id"])
        return self.get_batch(child["id"])

    def close_open_child(self, parent_id: str, *, result: str = "completed",
                         actor: str = "system:multiple") -> Optional[dict[str, Any]]:
        """Close the parent's currently-open child (if any) and clear the
        pointer, so the next start-fire opens a fresh child. Called when the
        child's STOP condition fires."""
        parent = self.get_batch(parent_id)
        if not parent:
            return None
        open_id = parent.get("open_child_batch_id")
        if not open_id:
            return None
        oc = self.get_batch(open_id)
        if oc and oc.get("status") == "running":
            self.stop_batch(open_id, result=result, actor=actor, source="multiple")
        self._set_open_child(parent_id, None)
        return self.get_batch(open_id)

    def running_multiple_parents(self) -> list[dict[str, Any]]:
        """All currently-running batches whose TYPE is batch_kind='multiple'.
        Used by the trigger daemon to know which parents to service."""
        rows, _ = self.list_batches(limit=200, status_filter="running")
        out = []
        for b in rows:
            bt = self.get_batch_type(b.get("batch_type_id") or "") if b.get("batch_type_id") else None
            if bt and str(bt.get("batch_kind")) == "multiple":
                out.append(b)
        return out

    def delete_batch(self, batch_id: str, *, actor: Optional[str] = None) -> bool:
        tid = self._tenant()
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE tenant_id = ? AND id = ?",
                (tid, batch_id),
            ).fetchone()
            if not row:
                return False
            c.execute("DELETE FROM batches WHERE tenant_id = ? AND id = ?", (tid, batch_id))
            c.execute("DELETE FROM batch_membership WHERE batch_id = ?", (batch_id,))
            c.execute("DELETE FROM batch_summaries WHERE batch_id = ?", (batch_id,))
            c.execute("DELETE FROM batch_events WHERE batch_id = ?", (batch_id,))
            self._audit(c, tid=tid, actor=actor, batch_id=batch_id,
                        action="batch.delete", before=dict(row))
            c.commit()
        return True

    # -- events --------------------------------------------------------
    def add_event(self, batch_id: str, payload: dict[str, Any], *,
                  actor: Optional[str] = None) -> dict[str, Any]:
        with self._connect() as c:
            self._event(
                c,
                batch_id=batch_id,
                kind=str(payload.get("kind") or "user"),
                severity=str(payload.get("severity") or "info"),
                actor=actor,
                message=payload.get("message"),
                meta=payload.get("meta"),
            )
            c.commit()
        return {"ok": True}

    def list_events(self, batch_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT id, batch_id, ts_utc, kind, severity, actor, source, message, meta_json
                FROM batch_events
                WHERE tenant_id = ? AND batch_id = ?
                ORDER BY ts_utc DESC
                LIMIT ?
                """,
                (tid, batch_id, max(1, min(limit, 2000))),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "batch_id": r["batch_id"],
                "ts_utc": r["ts_utc"],
                "kind": r["kind"],
                "severity": r["severity"],
                "actor": r["actor"],
                "source": r["source"],
                "message": r["message"],
                "meta": _json_load(r["meta_json"]),
            }
            for r in rows
        ]

    # -- validation ----------------------------------------------------
    def validate_batch(self, batch_id: str, decision: str, notes: Optional[str],
                       *, actor: Optional[str] = None) -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM batches WHERE tenant_id = ? AND id = ?",
                (tid, batch_id),
            ).fetchone()
            if not row:
                raise KeyError(batch_id)
            c.execute(
                """
                INSERT INTO batch_validation
                (batch_id, tenant_id, decision, actor, decided_utc, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, tid, decision, actor or "unknown", now, notes),
            )
            new_status = "validated" if decision == "approved" else "failed"
            c.execute(
                "UPDATE batches SET status = ?, updated_utc = ? WHERE tenant_id = ? AND id = ?",
                (new_status, now, tid, batch_id),
            )
            self._event(c, batch_id=batch_id, kind=f"batch.{decision}", actor=actor,
                        message=notes or f"Batch {decision}")
            self._audit(c, tid=tid, actor=actor, batch_id=batch_id,
                        action="batch.validate", after={"decision": decision})
            c.commit()
        return self.get_batch(batch_id)  # type: ignore[return-value]

    # -- summaries -----------------------------------------------------
    def compute_summaries(self, batch_id: str) -> int:
        """Compute min/max/avg/first/last/stdev/count for every tag that
        has at least one historian sample inside the batch's membership
        windows. Returns the number of summary rows written.

        Operator 2026-07-06: if the batch's TYPE defines `summary_tags`, only
        those tags are summarized (the report focuses on the tags that matter
        for this recipe instead of every tag the gateway happened to collect).
        An empty/absent summary_tags list means "all tags" (unchanged behavior).
        """
        tid = self._tenant()
        # Resolve the type's summary-tag allow-list (normalized for matching).
        allow: Optional[set] = None
        pass_rule = "any_out_of_spec"
        try:
            b = self.get_batch(batch_id)
            bt = self.get_batch_type(b.get("batch_type_id") or "") if b and b.get("batch_type_id") else None
            st = (bt or {}).get("summary_tags") or []
            wanted = [str(t).strip() for t in st if str(t).strip()]
            if wanted:
                allow = {t.lower() for t in wanted}
            pass_rule = str((bt or {}).get("pass_rule") or "any_out_of_spec")
        except Exception:
            allow = None
        # Operator 2026-07-09: per-tag spec limits for this batch's type (empty
        # if none defined → pass/fail stays 'na' and behavior is unchanged).
        limits = self._limits_map_for_batch(batch_id)
        with self._connect_readonly() as c:
            windows = c.execute(
                """
                SELECT gateway_id, ts_utc_start, ts_utc_end
                FROM batch_membership
                WHERE batch_id = ? AND tenant_id = ?
                """,
                (batch_id, tid),
            ).fetchall()
            if not windows:
                return 0
            tag_data: dict[tuple[str, str], list[float]] = {}
            first_values: dict[tuple[str, str], float] = {}
            last_values: dict[tuple[str, str], float] = {}
            # window duration (seconds) across all membership windows
            _dur_s = 0.0
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["ts_utc_start"] or "")
                end = str(w["ts_utc_end"] or _utc_now())
                _dur_s += _window_seconds(start, end)
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"
                    params.append(gw)
                rows = c.execute(
                    f"""
                    SELECT tag_name, value, gateway_id, ts_utc
                    FROM historian_readings
                    WHERE tenant_id = ?
                      AND ts_utc >= ?
                      AND ts_utc <= ?
                      {gw_clause}
                      AND value IS NOT NULL
                    ORDER BY ts_utc ASC
                    """,
                    params,
                ).fetchall()
                for r in rows:
                    key = (str(r["gateway_id"] or ""), str(r["tag_name"] or ""))
                    if not key[1]:
                        continue
                    # summary_tags allow-list (type-level): skip tags not wanted.
                    if allow is not None and key[1].lower() not in allow:
                        continue
                    try:
                        v = float(r["value"])
                    except Exception:
                        continue
                    tag_data.setdefault(key, []).append(v)
                    first_values.setdefault(key, v)
                    last_values[key] = v
        rows_written = 0
        now = _utc_now()
        # Batch-level pass/fail roll-up accumulators (operator 2026-07-09).
        _pass_tags = 0
        _fail_tags = 0
        _any_limit = False
        with self._connect() as c:
            c.execute("DELETE FROM batch_summaries WHERE batch_id = ?", (batch_id,))
            for (gw, tag), values in tag_data.items():
                if not values:
                    continue
                try:
                    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
                except Exception:
                    stdev = 0.0
                svals = sorted(values)
                p95 = _percentile(svals, 95.0)
                # --- spec-limit evaluation (only if this tag has a limit) ---
                lim = limits.get(tag.lower())
                lo = up = None
                in_cnt = out_cnt = 0
                in_pct = None
                tag_pf = "na"
                if lim:
                    _any_limit = True
                    lo = _opt_num(lim.get("lower_limit"))
                    up = _opt_num(lim.get("upper_limit"))
                    if lo is not None or up is not None:
                        for v in values:
                            ok = True
                            if lo is not None and v < lo:
                                ok = False
                            if up is not None and v > up:
                                ok = False
                            if ok:
                                in_cnt += 1
                            else:
                                out_cnt += 1
                        total = in_cnt + out_cnt
                        in_pct = round((in_cnt / total) * 100.0, 3) if total else None
                        # per-tag pass/fail depends on the type's rule
                        if pass_rule == "in_spec_pct":
                            thr = _opt_num(lim.get("in_spec_pct_min"))
                            thr = thr if thr is not None else 100.0
                            tag_pf = "pass" if (in_pct is not None and in_pct >= thr) else "fail"
                        else:  # any_out_of_spec
                            tag_pf = "pass" if out_cnt == 0 else "fail"
                        if tag_pf == "pass":
                            _pass_tags += 1
                        elif tag_pf == "fail":
                            _fail_tags += 1
                c.execute(
                    """
                    INSERT INTO batch_summaries
                    (batch_id, tenant_id, tag_name, gateway_id, sample_count,
                     min_value, max_value, avg_value, first_value, last_value,
                     stdev_value, computed_utc,
                     lower_limit, upper_limit, in_spec_count, out_of_spec_count,
                     in_spec_pct, pass_fail, p95_value, duration_s)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id, tid, tag, gw or None, len(values),
                        min(values), max(values), sum(values) / len(values),
                        first_values.get((gw, tag)), last_values.get((gw, tag)),
                        stdev, now,
                        lo, up, in_cnt, out_cnt, in_pct, tag_pf, p95, round(_dur_s, 1),
                    ),
                )
                rows_written += 1
            # Roll up to the batch. If NO tag had a limit, result stays 'na'
            # (unchanged behavior for existing batches). Otherwise pass unless
            # any limited tag failed.
            if _any_limit:
                result = "fail" if _fail_tags > 0 else "pass"
            else:
                result = "na"
            c.execute(
                "UPDATE batches SET result = ?, pass_tag_count = ?, fail_tag_count = ?, "
                "updated_utc = ? WHERE tenant_id = ? AND id = ?",
                (result, _pass_tags, _fail_tags, now, tid, batch_id),
            )
            c.commit()
        return rows_written

    def list_summaries(self, batch_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT tag_name, gateway_id, sample_count, min_value, max_value,
                       avg_value, first_value, last_value, stdev_value, computed_utc,
                       lower_limit, upper_limit, in_spec_count, out_of_spec_count,
                       in_spec_pct, pass_fail, p95_value, duration_s
                FROM batch_summaries
                WHERE tenant_id = ? AND batch_id = ?
                ORDER BY tag_name COLLATE NOCASE
                """,
                (tid, batch_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- parent / child rollup ----------------------------------------
    def list_child_batches(self, parent_batch_id: str) -> list[dict[str, Any]]:
        """All batches that name this batch as their parent (newest first)."""
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT * FROM batches
                WHERE tenant_id = ? AND parent_batch_id = ?
                ORDER BY COALESCE(started_utc, created_utc) DESC
                """,
                (tid, parent_batch_id),
            ).fetchall()
        return [self._row_to_batch(r) for r in rows]

    def rollup_children(self, parent_batch_id: str) -> dict[str, Any]:
        """Aggregate per-tag statistics across every child batch of a parent.

        Returns:
            {
              parent: <batch dict>,
              children: [<batch dict>, ...],   # newest first
              tags: [
                {tag_name, gateway_id, sample_count, min_value, max_value,
                 avg_value, contributing_children},
                ...
              ],
              totals: {child_count, completed, failed, cancelled, running}
            }

        Aggregation: weighted average by per-child sample_count. min/max
        are global. Used by the Insights / UI rollup view and a future
        parent-batch PDF (single export covering an entire shift/day).
        """
        parent = self.get_batch(parent_batch_id)
        if not parent:
            raise KeyError(parent_batch_id)
        children = self.list_child_batches(parent_batch_id)
        # Collect per-child summaries.
        agg: dict[tuple[str, str | None], dict[str, Any]] = {}
        for ch in children:
            for s in self.list_summaries(ch["id"]):
                key = (str(s.get("tag_name") or ""), s.get("gateway_id"))
                cnt = int(s.get("sample_count") or 0)
                if cnt <= 0:
                    continue
                bucket = agg.setdefault(key, {
                    "tag_name": key[0], "gateway_id": key[1],
                    "sample_count": 0, "min_value": None, "max_value": None,
                    "_weighted_sum": 0.0, "contributing_children": 0,
                })
                bucket["sample_count"] += cnt
                bucket["contributing_children"] += 1
                # Weighted avg accumulator
                try:
                    avg = float(s.get("avg_value") or 0.0)
                    bucket["_weighted_sum"] += avg * cnt
                except Exception:
                    pass
                # min / max global
                try:
                    mn = float(s.get("min_value"))
                    bucket["min_value"] = mn if bucket["min_value"] is None else min(bucket["min_value"], mn)
                except Exception:
                    pass
                try:
                    mx = float(s.get("max_value"))
                    bucket["max_value"] = mx if bucket["max_value"] is None else max(bucket["max_value"], mx)
                except Exception:
                    pass
        tags = []
        for b in agg.values():
            n = b["sample_count"] or 1
            b["avg_value"] = (b.pop("_weighted_sum") / n) if n else None
            tags.append(b)
        tags.sort(key=lambda x: (x.get("tag_name") or "").lower())
        # Status totals
        totals = {"child_count": len(children),
                  "completed": 0, "failed": 0, "cancelled": 0, "running": 0,
                  "validated": 0, "created": 0}
        for ch in children:
            st = str(ch.get("status") or "")
            if st in totals:
                totals[st] += 1
        return {"parent": parent, "children": children, "tags": tags, "totals": totals}

    # -- audit ---------------------------------------------------------
    def list_audit(self, *, limit: int = 200, batch_id: Optional[str] = None) -> list[dict[str, Any]]:
        tid = self._tenant()
        where = "WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if batch_id:
            where += " AND batch_id = ?"
            params.append(batch_id)
        with self._connect_readonly() as c:
            rows = c.execute(
                f"""
                SELECT id, ts_utc, actor, action, batch_id, batch_type_id,
                       before_json, after_json, meta_json
                FROM batch_audit_log
                {where}
                ORDER BY ts_utc DESC
                LIMIT ?
                """,
                (*params, max(1, min(limit, 2000))),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "ts_utc": r["ts_utc"],
                "actor": r["actor"],
                "action": r["action"],
                "batch_id": r["batch_id"],
                "batch_type_id": r["batch_type_id"],
                "before": _json_load(r["before_json"]),
                "after": _json_load(r["after_json"]),
                "meta": _json_load(r["meta_json"]),
            }
            for r in rows
        ]

    # -- historian-by-batch helper ------------------------------------
    def historian_rows_for_batch(self, batch_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return historian rows that fall inside any of the batch's
        membership windows. Read-only; never touches the worker."""
        tid = self._tenant()
        with self._connect_readonly() as c:
            windows = c.execute(
                """
                SELECT gateway_id, ts_utc_start, ts_utc_end
                FROM batch_membership
                WHERE batch_id = ? AND tenant_id = ?
                """,
                (batch_id, tid),
            ).fetchall()
            if not windows:
                return []
            all_rows: list[dict[str, Any]] = []
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["ts_utc_start"] or "")
                end = str(w["ts_utc_end"] or _utc_now())
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"
                    params.append(gw)
                rows = c.execute(
                    f"""
                    SELECT ts_utc, tag_name, value, value_text, quality, quality_label,
                           gateway_id, gateway_name, device_name
                    FROM historian_readings
                    WHERE tenant_id = ?
                      AND ts_utc >= ? AND ts_utc <= ?
                      {gw_clause}
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (*params, max(1, min(limit, 50000))),
                ).fetchall()
                all_rows.extend(dict(r) for r in rows)
        return all_rows[: max(1, min(limit, 50000))]

    # -- schedule cursors (daemon dedupe) -----------------------------
    def mark_schedule_ran(self, batch_type_id: str, slot: str, ts: str) -> None:
        """Record that a schedule slot ('start'|'stop'|'report') fired at `ts`
        so the daemon won't re-fire within the same due minute across restarts."""
        col = {"start": "last_scheduled_start_utc",
               "stop": "last_scheduled_stop_utc",
               "report": "last_report_utc"}.get(slot)
        if not col:
            return
        tid = self._tenant()
        with self._connect() as c:
            c.execute(
                f"UPDATE batch_types SET {col} = ? WHERE tenant_id = ? AND id = ?",
                (ts, tid, batch_type_id),
            )
            c.commit()

    def latest_batch_for_type(self, batch_type_id: str) -> Optional[dict[str, Any]]:
        """Most-recent batch of a type (any status), for scheduled reports."""
        rows, _ = self.list_batches(limit=1, batch_type_id=batch_type_id)
        return rows[0] if rows else None

    def resolve_gateway_for_tag(self, tag: str) -> str:
        """Return the gateway_id that most-recently reported `tag`, or ''.
        Used to stamp a condition-triggered batch with the gateway of its
        trigger tag, so two batches on different machinery stay isolated
        (each membership window filters the historian by that gateway).
        Operator 2026-07-09. Read-only."""
        tag = str(tag or "").strip()
        if not tag:
            return ""
        tid = self._tenant()
        try:
            with self._connect_readonly() as c:
                row = c.execute(
                    "SELECT gateway_id FROM historian_readings "
                    "WHERE tenant_id = ? AND tag_name = ? AND gateway_id IS NOT NULL "
                    "ORDER BY ts_utc DESC LIMIT 1",
                    (tid, tag),
                ).fetchone()
                return str(row[0]) if row and row[0] else ""
        except Exception:
            return ""

    def gateway_for_type_trigger(self, batch_type: dict[str, Any]) -> str:
        """Best-effort gateway for a type: the gateway of the FIRST tag in its
        trigger_start (or trigger_stop) rules. '' if none resolvable."""
        for slot in ("trigger_start", "trigger_stop"):
            cond = batch_type.get(slot)
            if isinstance(cond, dict):
                for r in (cond.get("rules") or []):
                    if isinstance(r, dict) and r.get("tag"):
                        gw = self.resolve_gateway_for_tag(str(r["tag"]))
                        if gw:
                            return gw
        return ""

    def collected_tags_in_window(self, batch_id: str) -> list[str]:
        """Distinct tag names that have at least one historian reading inside the
        batch's membership window(s). This is what the Reports builder offers as
        the pick-list for a batch-sourced report. Operator 2026-07-06."""
        tid = self._tenant()
        tags: set[str] = set()
        with self._connect_readonly() as c:
            windows = c.execute(
                "SELECT gateway_id, ts_utc_start, ts_utc_end FROM batch_membership "
                "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid),
            ).fetchall()
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["ts_utc_start"] or "")
                end = str(w["ts_utc_end"] or _utc_now())
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"; params.append(gw)
                try:
                    for r in c.execute(
                        f"SELECT DISTINCT tag_name FROM historian_readings "
                        f"WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ?{gw_clause}",
                        params,
                    ):
                        if r[0]:
                            tags.add(str(r[0]))
                except Exception:
                    continue
        return sorted(tags)

    def chart_series_for_batch(self, batch_id: str, tags: list[str],
                               *, max_points: int = 400) -> dict[str, Any]:
        """Return downsampled per-tag time-series WITHIN the batch's window(s),
        for the in-UI trend charts. Read-only over historian_readings by
        (tenant, gateway, ts-range) — the same window join used everywhere in
        this module; never modifies the historian. Operator 2026-07-09.

        Returns {from, to, series:[{tag, points:[{ts_ms, value}], min, max, avg,
                 lower_limit, upper_limit}]}."""
        tid = self._tenant()
        want = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if not want:
            return {"series": []}
        want_set = {t.lower() for t in want}
        limits = self._limits_map_for_batch(batch_id)
        per_tag: dict[str, list[tuple[str, float]]] = {}
        from_ts = to_ts = ""
        with self._connect_readonly() as c:
            windows = c.execute(
                "SELECT gateway_id, ts_utc_start, ts_utc_end FROM batch_membership "
                "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid),
            ).fetchall()
            for w in windows:
                gw = str(w["gateway_id"] or "")
                start = str(w["ts_utc_start"] or "")
                end = str(w["ts_utc_end"] or _utc_now())
                if not from_ts or start < from_ts:
                    from_ts = start
                if end > to_ts:
                    to_ts = end
                params: list[Any] = [tid, start, end]
                gw_clause = ""
                if gw:
                    gw_clause = " AND gateway_id = ?"; params.append(gw)
                ph = ",".join(["?"] * len(want))
                params2 = list(params) + want
                try:
                    for r in c.execute(
                        f"SELECT ts_utc, tag_name, value FROM historian_readings "
                        f"WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc <= ?{gw_clause} "
                        f"AND tag_name IN ({ph}) AND value IS NOT NULL ORDER BY ts_utc ASC",
                        params2,
                    ):
                        tn = str(r[1] or "")
                        if tn.lower() not in want_set:
                            continue
                        try:
                            v = float(r[2])
                        except Exception:
                            continue
                        per_tag.setdefault(tn, []).append((str(r[0]), v))
                except Exception:
                    continue
        # Downsample each tag to ~max_points (stride) + compute min/max/avg.
        import datetime as _dt
        def _ms(ts: str) -> int:
            s = str(ts)[:23]
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    d = _dt.datetime.strptime(s if ".%f" in fmt else s[:19], fmt)
                    return int(d.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
                except Exception:
                    continue
            return 0
        series = []
        for tn in want:
            pts = per_tag.get(tn) or []
            if not pts:
                series.append({"tag": tn, "points": [], "min": None, "max": None, "avg": None})
                continue
            vals = [v for _, v in pts]
            import math as _m
            stride = max(1, _m.ceil(len(pts) / max(1, max_points)))
            ds = [{"ts_ms": _ms(ts), "value": v} for i, (ts, v) in enumerate(pts) if i % stride == 0]
            # Always include the last point so the trend reaches the window end.
            if pts and (len(pts) - 1) % stride != 0:
                ds.append({"ts_ms": _ms(pts[-1][0]), "value": pts[-1][1]})
            lim = limits.get(tn.lower()) or {}
            series.append({
                "tag": tn, "points": ds,
                "min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals),
                "lower_limit": _opt_num(lim.get("lower_limit")),
                "upper_limit": _opt_num(lim.get("upper_limit")),
            })
        return {"from": from_ts, "to": to_ts, "series": series}

    def batch_report_context(self, batch_id: str) -> dict[str, Any]:
        """One call that gives the Reports layer everything it needs for a
        batch-sourced report: the batch, its type, resolved window, collected
        tags, summaries, and (if a multiple parent) the child rollup."""
        b = self.get_batch(batch_id)
        if not b:
            return {}
        bt = self.get_batch_type(b.get("batch_type_id") or "") if b.get("batch_type_id") else None
        start = str(b.get("started_utc") or "")
        end = str(b.get("ended_utc") or "")
        ctx = {
            "batch": b,
            "batch_type": bt,
            "from_utc": start,
            "to_utc": end or _utc_now(),
            "collected_tags": self.collected_tags_in_window(batch_id),
            "summaries": self.list_summaries(batch_id),
            # Operator 2026-07-09: limits + manual entries + roll-up result so the
            # report and detail UI show pass/fail without extra round-trips.
            "limits": self.list_type_limits(b.get("batch_type_id") or "") if b.get("batch_type_id") else [],
            "manual_entries": self.list_manual_entries(batch_id),
            "result": b.get("result"),
            "pass_tag_count": b.get("pass_tag_count"),
            "fail_tag_count": b.get("fail_tag_count"),
        }
        if bt and str(bt.get("batch_kind")) == "multiple":
            try:
                ctx["rollup"] = self.rollup_children(batch_id)
            except Exception:
                ctx["rollup"] = None
        return ctx

    # -- collection-profile window adjustment (applied on stop) -------
    def _apply_collection_profile(self, batch_id: str) -> None:
        """Reshape the batch's membership window according to its TYPE's
        collection_profile, so summaries/historian reflect the intended scope:

          continuous (default) → leave the full start→stop window as-is.
          snapshot             → collapse to a 2s window at the START instant
                                  (captures the point-in-time values only).
          pre_post             → pad the window by 30s before start and after
                                  stop (captures ramp-up / settle context).
          trigger / event      → treated as continuous here (the PLC-trigger
                                  windows already bound them precisely).

        Only touches THIS module's batch_membership rows; never the historian.
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        b = self.get_batch(batch_id)
        if not b:
            return
        bt = self.get_batch_type(b.get("batch_type_id") or "") if b.get("batch_type_id") else None
        profile = str((bt or {}).get("collection_profile") or "continuous").lower()
        if profile not in ("snapshot", "pre_post"):
            return

        def _parse(ts: str):
            try:
                return _dt.strptime(str(ts)[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_tz.utc)
            except Exception:
                try:
                    return _dt.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
                except Exception:
                    return None

        def _fmt(d) -> str:
            return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        tid = self._tenant()
        with self._connect() as c:
            wins = c.execute(
                "SELECT id, ts_utc_start, ts_utc_end FROM batch_membership "
                "WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid),
            ).fetchall()
            for w in wins:
                s = _parse(w["ts_utc_start"])
                e = _parse(w["ts_utc_end"]) if w["ts_utc_end"] else None
                if not s:
                    continue
                if profile == "snapshot":
                    new_start, new_end = s, s + _td(seconds=2)
                else:  # pre_post
                    new_start = s - _td(seconds=30)
                    new_end = (e or s) + _td(seconds=30)
                c.execute(
                    "UPDATE batch_membership SET ts_utc_start = ?, ts_utc_end = ? WHERE id = ?",
                    (_fmt(new_start), _fmt(new_end), w["id"]),
                )
            c.commit()

    # -- barcode scan (keyboard-wedge) --------------------------------
    def scan_batch(self, code: str, *, batch_type_id: Optional[str] = None,
                   action: str = "start", operator: Optional[str] = None,
                   gateway_id: Optional[str] = None, product: Optional[str] = None,
                   recipe: Optional[str] = None, notes: Optional[str] = None,
                   actor: Optional[str] = None) -> dict[str, Any]:
        """Handle a scanned barcode from a keyboard-wedge scanner.

        action='start': create + start a batch whose identifier IS the scanned
          code, tagged source='barcode'. If batch_type_id is omitted we pick the
          single type whose start_method OR identifier_method is 'barcode'; if
          there are several, the caller must pass one.
        action='stop': stop the running batch whose identifier matches the code.
        Idempotent-ish: scanning the same code twice while running returns the
        existing running batch instead of creating a duplicate.
        """
        code = str(code or "").strip()
        if not code:
            raise ValueError("empty barcode")
        if action == "stop":
            rows, _ = self.list_batches(limit=5, status_filter="running", search=code)
            match = next((b for b in rows if str(b.get("identifier") or "") == code), None)
            if not match:
                raise KeyError(code)
            return self.stop_batch(match["id"], result="completed", operator=operator,
                                   actor=actor, source="barcode")
        # action == start
        # Don't double-start: if a running batch already has this identifier, return it.
        rows, _ = self.list_batches(limit=5, status_filter="running", search=code)
        existing = next((b for b in rows if str(b.get("identifier") or "") == code), None)
        if existing:
            return existing
        # Resolve batch type.
        bt_id = batch_type_id
        if not bt_id:
            barcode_types = [
                t for t in self.list_batch_types()
                if t.get("enabled") and (t.get("start_method") == "barcode" or t.get("identifier_method") == "barcode")
            ]
            if len(barcode_types) == 1:
                bt_id = barcode_types[0]["id"]
            elif len(barcode_types) == 0:
                raise ValueError("no barcode-enabled batch type configured")
            else:
                raise ValueError("multiple barcode batch types — specify batch_type_id")
        created = self.create_batch({
            "batch_type_id": bt_id,
            "identifier": code,
            "identifier_method": "barcode",
            "operator": operator,
            "gateway_id": gateway_id,
            "product": product,
            "recipe": recipe,
            "notes": notes,
            "source": "barcode",
            "metadata": {"source": "barcode", "scanned_code": code},
        }, actor=actor or "barcode")
        return self.start_batch(created["id"], operator=operator, gateway_id=gateway_id,
                                actor=actor or "barcode", source="barcode")

    # -- CSV export ---------------------------------------------------
    def batch_summary_csv(self, batch_id: str) -> str:
        """CSV of the per-tag summaries for a batch (one row per tag)."""
        import csv as _csv
        import io as _io
        batch = self.get_batch(batch_id)
        summaries = self.list_summaries(batch_id)
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["batch_id", "identifier", "status", "started_utc", "ended_utc",
                    "tag_name", "gateway_id", "sample_count",
                    "min_value", "max_value", "avg_value", "first_value", "last_value", "stdev_value"])
        ident = (batch or {}).get("identifier") or batch_id
        for s in summaries:
            w.writerow([
                batch_id, ident, (batch or {}).get("status"),
                (batch or {}).get("started_utc"), (batch or {}).get("ended_utc"),
                s.get("tag_name"), s.get("gateway_id"), s.get("sample_count"),
                s.get("min_value"), s.get("max_value"), s.get("avg_value"),
                s.get("first_value"), s.get("last_value"), s.get("stdev_value"),
            ])
        return buf.getvalue()

    def batch_historian_csv(self, batch_id: str, *, limit: int = 50000) -> str:
        """CSV of the raw historian readings inside the batch window."""
        import csv as _csv
        import io as _io
        rows = self.historian_rows_for_batch(batch_id, limit=limit)
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["ts_utc", "tag_name", "value", "value_text", "quality_label",
                    "gateway_id", "gateway_name", "device_name"])
        for r in rows:
            w.writerow([
                r.get("ts_utc"), r.get("tag_name"), r.get("value"), r.get("value_text"),
                r.get("quality_label"), r.get("gateway_id"), r.get("gateway_name"), r.get("device_name"),
            ])
        return buf.getvalue()

    def rollup_csv(self, parent_batch_id: str) -> str:
        """CSV of the parent rollup: aggregated per-tag stats across children."""
        import csv as _csv
        import io as _io
        rollup = self.rollup_children(parent_batch_id)
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["parent_batch_id", "tag_name", "gateway_id", "contributing_children",
                    "sample_count", "min_value", "max_value", "avg_value"])
        for t in (rollup.get("tags") or []):
            w.writerow([
                parent_batch_id, t.get("tag_name"), t.get("gateway_id"),
                t.get("contributing_children"), t.get("sample_count"),
                t.get("min_value"), t.get("max_value"), t.get("avg_value"),
            ])
        return buf.getvalue()

    # -- scheduled-report helper (used by the schedule daemon) --------
    def send_batch_report_email(self, batch_id: str, recipients: list[str], *,
                                attach_pdf: bool = True, attach_csv: bool = False,
                                subject: Optional[str] = None) -> bool:
        """Email the batch's report (PDF and/or CSV) to `recipients`, reusing the
        global SMTP/PHP transport. Returns True on success. Used by the schedule
        daemon's report jobs and could back a manual 'email now' button."""
        batch = self.get_batch(batch_id)
        if not batch or not recipients:
            return False
        bt = self.get_batch_type(batch.get("batch_type_id") or "") if batch.get("batch_type_id") else None
        events = self.list_events(batch_id, limit=2000)
        summaries = self.list_summaries(batch_id)
        manual = self.list_manual_entries(batch_id)
        attachments_spec: list[tuple[str, bytes, str]] = []
        if attach_pdf:
            try:
                from .reports import render_single_batch_pdf
                pdf = render_single_batch_pdf(batch, bt, events, summaries, manual)
                attachments_spec.append((f"batch_{batch.get('identifier') or batch_id}.pdf", pdf, "application/pdf"))
            except Exception:
                pass
        if attach_csv:
            try:
                csv_text = self.batch_summary_csv(batch_id)
                attachments_spec.append((f"batch_{batch.get('identifier') or batch_id}.csv",
                                         csv_text.encode("utf-8"), "text/csv"))
            except Exception:
                pass
        if not attachments_spec:
            return False
        return self._send_email_with_attachments(
            recipients,
            subject or f"Batch {batch.get('identifier') or batch_id} report",
            (f"<p>Scheduled report for batch <b>{batch.get('identifier') or batch_id}</b> "
             f"(status <b>{batch.get('status')}</b>).</p>"),
            attachments_spec,
        )

    def _send_email_with_attachments(self, recipients: list[str], subject: str,
                                     html_body: str,
                                     attachments_spec: list[tuple[str, bytes, str]]) -> bool:
        """Shared email sender used by close-email and scheduled reports."""
        try:
            bs = self._app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            email_block = bs.get("email_notifications") or {}
            email_settings = email_block.get("settings") or email_block or {}
        except Exception:
            email_settings = {}
        try:
            import base64 as _b64
            from app.routers.notifications import (
                EmailRequest, SMTPConfig, PHPMailConfig, EmailAttachment, send_email_request,
            )
        except Exception:
            return False
        transport = str(email_settings.get("transport") or "smtp").strip().lower()
        smtp = email_settings.get("smtp") or {}
        php = email_settings.get("php_mail")
        attachments = []
        for (fname, data, ctype) in attachments_spec:
            try:
                attachments.append(EmailAttachment(
                    filename=fname,
                    content_b64=_b64.b64encode(data).decode("ascii"),
                    content_type=ctype,
                ))
            except Exception:
                continue
        if not attachments:
            return False
        req = EmailRequest(
            transport="php_http" if transport == "php_http" else "smtp",
            smtp=SMTPConfig(**(smtp if isinstance(smtp, dict) else {})),
            php_mail=PHPMailConfig(**(php if isinstance(php, dict) else {})) if isinstance(php, dict) else None,
            to=list(recipients), cc=[], bcc=[],
            subject=subject, html_body=html_body,
            text_body=subject, attachments=attachments,
        )
        outcome = send_email_request(req)
        return bool(getattr(outcome, "ok", False))

    # -- internal helpers ---------------------------------------------
    def _event(self, c: sqlite3.Connection, *, batch_id: str, kind: str,
               severity: str = "info", actor: Optional[str] = None,
               source: Optional[str] = None, message: Optional[str] = None,
               meta: Optional[dict] = None) -> None:
        c.execute(
            """
            INSERT INTO batch_events
            (batch_id, tenant_id, ts_utc, kind, severity, actor, source, message, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id, self._tenant(), _utc_now(), kind, severity,
                actor, source, message, _json_or_none(meta),
            ),
        )

    def _audit(self, c: sqlite3.Connection, *, tid: str, action: str,
               actor: Optional[str] = None, batch_id: Optional[str] = None,
               batch_type_id: Optional[str] = None,
               before: Any = None, after: Any = None,
               meta: Any = None) -> None:
        c.execute(
            """
            INSERT INTO batch_audit_log
            (tenant_id, batch_id, batch_type_id, ts_utc, actor, action,
             before_json, after_json, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid, batch_id, batch_type_id, _utc_now(), actor, action,
                _json_or_none(before), _json_or_none(after), _json_or_none(meta),
            ),
        )

    # -- row decoders --------------------------------------------------
    def _row_to_batch_type(self, row) -> dict[str, Any]:
        if not row:
            return {}  # type: ignore[return-value]
        d = dict(row)
        return {
            "id": d["id"],
            "name": d["name"],
            "description": d.get("description"),
            "parent_type_id": d.get("parent_type_id"),
            "start_method": d.get("start_method") or "manual",
            "end_method": d.get("end_method") or "manual",
            "start_config": _json_load(d.get("start_config_json")),
            "end_config": _json_load(d.get("end_config_json")),
            "collection_profile": d.get("collection_profile") or "continuous",
            "report_template_id": d.get("report_template_id"),
            "identifier_method": d.get("identifier_method") or "auto",
            "identifier_prefix": d.get("identifier_prefix"),
            "summary_tags": _json_load(d.get("summary_tags_json")) or [],
            "enabled": bool(d.get("enabled", 1)),
            "email_on_close": bool(d.get("email_on_close", 0)),
            "email_recipients": d.get("email_recipients"),
            "trigger_start": _json_load(d.get("trigger_start_json")),
            "trigger_stop": _json_load(d.get("trigger_stop_json")),
            "start_schedule": _json_load(d.get("start_schedule_json")),
            "stop_schedule": _json_load(d.get("stop_schedule_json")),
            "report_schedule": _json_load(d.get("report_schedule_json")),
            "last_scheduled_start_utc": d.get("last_scheduled_start_utc"),
            "last_scheduled_stop_utc": d.get("last_scheduled_stop_utc"),
            "last_report_utc": d.get("last_report_utc"),
            "batch_kind": d.get("batch_kind") or "single",
            "child_type_id": d.get("child_type_id"),
            "pass_rule": d.get("pass_rule") or "any_out_of_spec",
            "manual_fields": _json_load(d.get("manual_fields_json")) or [],
            "chart_config": _json_load(d.get("chart_config_json")),
            "created_utc": d["created_utc"],
            "updated_utc": d["updated_utc"],
        }

    def _row_to_batch(self, row) -> dict[str, Any]:
        if not row:
            return {}  # type: ignore[return-value]
        d = dict(row)
        return {
            "id": d["id"],
            "tenant_id": d.get("tenant_id") or "default",
            "batch_type_id": d.get("batch_type_id"),
            "parent_batch_id": d.get("parent_batch_id"),
            "identifier": d.get("identifier"),
            "identifier_method": d.get("identifier_method"),
            "status": d.get("status") or "created",
            "started_utc": d.get("started_utc"),
            "ended_utc": d.get("ended_utc"),
            "operator": d.get("operator"),
            "source": d.get("source"),
            "gateway_id": d.get("gateway_id"),
            "product": d.get("product"),
            "recipe": d.get("recipe"),
            "notes": d.get("notes"),
            "metadata": _json_load(d.get("metadata_json")),
            "open_child_batch_id": d.get("open_child_batch_id"),
            "result": d.get("result"),
            "pass_tag_count": d.get("pass_tag_count"),
            "fail_tag_count": d.get("fail_tag_count"),
            "created_utc": d["created_utc"],
            "updated_utc": d["updated_utc"],
        }


# -- helpers -------------------------------------------------------------
def record_to_dict(record: tuple) -> dict[str, Any]:
    """Cheap snapshot for audit_log.after when we don't yet have a SELECT
    row to feed dict() with."""
    keys = (
        "id", "tenant_id", "name", "description", "parent_type_id", "start_method",
        "end_method", "start_config_json", "end_config_json", "collection_profile",
        "report_template_id", "identifier_method", "identifier_prefix",
        "summary_tags_json", "enabled", "created_utc", "updated_utc",
        "email_on_close", "email_recipients",
        "trigger_start_json", "trigger_stop_json",
        "start_schedule_json", "stop_schedule_json", "report_schedule_json",
        "batch_kind", "child_type_id",
        "pass_rule", "manual_fields_json", "chart_config_json",
    )
    return {k: v for k, v in zip(keys, record)}
