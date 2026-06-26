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
                       summary_tags_json, enabled, created_utc, updated_utc
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
                       summary_tags_json, enabled, created_utc, updated_utc
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
        )
        with self._connect() as c:
            if is_new:
                c.execute(
                    """
                    INSERT INTO batch_types
                    (id, tenant_id, name, description, parent_type_id, start_method, end_method,
                     start_config_json, end_config_json, collection_profile, report_template_id,
                     identifier_method, identifier_prefix, summary_tags_json, enabled,
                     created_utc, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        updated_utc = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        record[2], record[3], record[4], record[5], record[6], record[7],
                        record[8], record[9], record[10], record[11], record[12], record[13],
                        record[14], now, tid, bt_id,
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
        # Best-effort summary compute. Failures are non-fatal — operator can
        # recompute manually.
        try:
            self.compute_summaries(batch_id)
        except Exception:
            pass
        return self.get_batch(batch_id)  # type: ignore[return-value]

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
        windows. Returns the number of summary rows written."""
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
                return 0
            tag_data: dict[tuple[str, str], list[float]] = {}
            first_values: dict[tuple[str, str], float] = {}
            last_values: dict[tuple[str, str], float] = {}
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
                    try:
                        v = float(r["value"])
                    except Exception:
                        continue
                    tag_data.setdefault(key, []).append(v)
                    first_values.setdefault(key, v)
                    last_values[key] = v
        rows_written = 0
        now = _utc_now()
        with self._connect() as c:
            c.execute("DELETE FROM batch_summaries WHERE batch_id = ?", (batch_id,))
            for (gw, tag), values in tag_data.items():
                if not values:
                    continue
                try:
                    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
                except Exception:
                    stdev = 0.0
                c.execute(
                    """
                    INSERT INTO batch_summaries
                    (batch_id, tenant_id, tag_name, gateway_id, sample_count,
                     min_value, max_value, avg_value, first_value, last_value,
                     stdev_value, computed_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id, tid, tag, gw or None, len(values),
                        min(values), max(values), sum(values) / len(values),
                        first_values.get((gw, tag)), last_values.get((gw, tag)),
                        stdev, now,
                    ),
                )
                rows_written += 1
            c.commit()
        return rows_written

    def list_summaries(self, batch_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT tag_name, gateway_id, sample_count, min_value, max_value,
                       avg_value, first_value, last_value, stdev_value, computed_utc
                FROM batch_summaries
                WHERE tenant_id = ? AND batch_id = ?
                ORDER BY tag_name COLLATE NOCASE
                """,
                (tid, batch_id),
            ).fetchall()
        return [dict(r) for r in rows]

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
    )
    return {k: v for k, v in zip(keys, record)}
