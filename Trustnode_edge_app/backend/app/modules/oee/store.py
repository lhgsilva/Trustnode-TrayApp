# -*- coding: utf-8 -*-
"""All OEE database access.

Follows the batch module's shape: one class, connections borrowed from
AppStore so the OEE tables live in the same file, share the same WAL and are
covered by the same backup/restore path. Nothing here talks HTTP.

Reads of the COLLECTION system (gateways, devices, tags, historian) go through
AppStore too - this module never opens its own connection to anything.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .schema import OEE_TABLES

# Config-entity tables that share the same simple CRUD shape.
_CRUD_TABLES = {
    "machines": "oee_machines",
    "signal_mappings": "oee_signal_mappings",
    "power_meter_mappings": "oee_power_meter_mappings",
    "power_state_rules": "oee_power_state_rules",
    "products": "oee_products",
    "orders": "oee_orders",
    "shifts": "oee_shifts",
    "planned_stops": "oee_planned_stops",
    "downtime_reasons": "oee_downtime_reasons",
    "quality_reasons": "oee_quality_reasons",
}


def _now() -> str:
    """The app's canonical historian timestamp format.

    Must match app_store's own stamp exactly. A previous incident in this code
    base (power module, 2026-08) came from writing isoformat() here and
    'YYYY-MM-DD HH:MM:SS.mmm' elsewhere: 'T' sorts after ' ', so every range
    query silently excluded the rows.
    """
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    # SQLite has no bool; the UI wants real booleans for its toggles.
    for k, v in list(d.items()):
        if k in ("enabled", "oee_enabled", "signal_enabled", "power_enabled",
                 "manual_enabled", "allow_over_100", "exclude_from_oee",
                 "show_on_dashboard", "is_planned", "requires_no_count"):
            d[k] = bool(v)
    return d


class OeeStore:
    """CRUD + queries for the OEE module."""

    def __init__(self, app_store: Any) -> None:
        self._app = app_store

    # ------------------------------------------------------------------ db
    def _connect(self):
        return self._app._connect()

    def _tenant(self) -> str:
        try:
            return self._app._current_tenant_id()
        except Exception:
            return "default"

    # -------------------------------------------------------------- config
    def list_entities(self, kind: str, machine_id: str = "",
                      include_disabled: bool = True) -> List[Dict[str, Any]]:
        table = _CRUD_TABLES.get(kind)
        if not table:
            raise ValueError(f"unknown OEE entity '{kind}'")
        sql = f"SELECT * FROM {table} WHERE tenant_id = ?"
        args: List[Any] = [self._tenant()]
        if machine_id and self._has_column(table, "machine_id"):
            sql += " AND machine_id = ?"
            args.append(machine_id)
        if not include_disabled and self._has_column(table, "enabled"):
            sql += " AND enabled = 1"
        sql += self._order_clause(table)
        with self._connect() as c:
            return [_row_to_dict(r) for r in c.execute(sql, args).fetchall()]

    def _order_clause(self, table: str) -> str:
        if table in ("oee_downtime_reasons", "oee_quality_reasons"):
            return " ORDER BY sort_order, category, reason"
        if table == "oee_power_state_rules":
            return " ORDER BY priority, name"
        if table == "oee_signal_mappings":
            return " ORDER BY priority, oee_function"
        if self._has_column(table, "name"):
            return " ORDER BY name"
        return " ORDER BY created_utc"

    _COLS_CACHE: Dict[str, set] = {}

    def _has_column(self, table: str, column: str) -> bool:
        cols = self._COLS_CACHE.get(table)
        if cols is None:
            with self._connect() as c:
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            self._COLS_CACHE[table] = cols
        return column in cols

    def get_entity(self, kind: str, entity_id: str) -> Dict[str, Any]:
        table = _CRUD_TABLES.get(kind)
        if not table:
            raise ValueError(f"unknown OEE entity '{kind}'")
        with self._connect() as c:
            row = c.execute(f"SELECT * FROM {table} WHERE id = ? AND tenant_id = ?",
                            (entity_id, self._tenant())).fetchone()
        return _row_to_dict(row)

    def save_entity(self, kind: str, payload: Dict[str, Any],
                    actor: str = "") -> Dict[str, Any]:
        """Insert or update one config row. Unknown keys are ignored, so a
        newer UI posting a field this build does not know cannot 500."""
        table = _CRUD_TABLES.get(kind)
        if not table:
            raise ValueError(f"unknown OEE entity '{kind}'")
        with self._connect() as c:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            data = {k: v for k, v in (payload or {}).items() if k in cols}
            for k, v in list(data.items()):
                if isinstance(v, bool):
                    data[k] = 1 if v else 0
            entity_id = str(data.get("id") or "").strip()
            now = _now()
            data["tenant_id"] = self._tenant()
            data["updated_utc"] = now
            if "updated_by" in cols:
                data["updated_by"] = actor

            existing = None
            if entity_id:
                existing = c.execute(
                    f"SELECT id FROM {table} WHERE id = ? AND tenant_id = ?",
                    (entity_id, self._tenant())).fetchone()

            if existing:
                sets = ", ".join(f"{k} = ?" for k in data if k != "id")
                args = [data[k] for k in data if k != "id"] + [entity_id, self._tenant()]
                c.execute(f"UPDATE {table} SET {sets} WHERE id = ? AND tenant_id = ?", args)
            else:
                data["id"] = entity_id or _uid(kind[:3])
                data["created_utc"] = now
                if "created_by" in cols:
                    data["created_by"] = actor
                names = ", ".join(data)
                marks = ", ".join("?" for _ in data)
                c.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})",
                          [data[k] for k in data])
                entity_id = data["id"]
            c.commit()
        return self.get_entity(kind, entity_id)

    def delete_entity(self, kind: str, entity_id: str) -> bool:
        table = _CRUD_TABLES.get(kind)
        if not table:
            raise ValueError(f"unknown OEE entity '{kind}'")
        with self._connect() as c:
            cur = c.execute(f"DELETE FROM {table} WHERE id = ? AND tenant_id = ?",
                            (entity_id, self._tenant()))
            c.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- events
    def open_event(self, machine_id: str, state: str, source: str,
                   confidence: str, detail: str = "",
                   is_planned: bool = False,
                   planned_stop_id: str = "",
                   ts_utc: str = "") -> Dict[str, Any]:
        """Close whatever state the machine was in and open the new one.

        The timeline is the single source of truth for every duration in the
        module, so it must never contain two open rows for one machine.
        """
        now = ts_utc or _now()
        with self._connect() as c:
            cur = c.execute(
                "SELECT id, state, start_utc FROM oee_machine_events "
                "WHERE machine_id = ? AND tenant_id = ? AND end_utc IS NULL "
                "ORDER BY start_utc DESC", (machine_id, self._tenant())).fetchall()
            for row in cur:
                if row["state"] == state and row is cur[0]:
                    # Already in this state - nothing to do, keep the duration
                    # running rather than restarting it.
                    c.commit()
                    return _row_to_dict(row)
                dur = _seconds_between(row["start_utc"], now)
                c.execute(
                    "UPDATE oee_machine_events SET end_utc = ?, duration_s = ? WHERE id = ?",
                    (now, dur, row["id"]))
            new_id = _uid("evt")
            c.execute(
                "INSERT INTO oee_machine_events "
                "(id, tenant_id, machine_id, state, status_source, confidence, "
                " start_utc, is_planned, planned_stop_id, detected_detail, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, self._tenant(), machine_id, state, source, confidence,
                 now, 1 if is_planned else 0, planned_stop_id or None, detail, now))
            c.commit()
        return self.get_event(new_id)

    def get_event(self, event_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM oee_machine_events WHERE id = ? AND tenant_id = ?",
                (event_id, self._tenant())).fetchone())

    def current_event(self, machine_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM oee_machine_events WHERE machine_id = ? AND tenant_id = ? "
                "AND end_utc IS NULL ORDER BY start_utc DESC LIMIT 1",
                (machine_id, self._tenant())).fetchone())

    def list_events(self, machine_id: str = "", from_utc: str = "",
                    to_utc: str = "", limit: int = 5000) -> List[Dict[str, Any]]:
        sql = ("SELECT e.*, r.reason AS downtime_reason, r.category AS reason_category "
               "FROM oee_machine_events e "
               "LEFT JOIN oee_downtime_reasons r ON r.id = e.downtime_reason_id "
               "WHERE e.tenant_id = ?")
        args: List[Any] = [self._tenant()]
        if machine_id:
            sql += " AND e.machine_id = ?"
            args.append(machine_id)
        if from_utc:
            # An event that STARTED before the window but is still open (or
            # ended inside it) still contributes time to the window.
            sql += " AND (e.end_utc IS NULL OR e.end_utc >= ?)"
            args.append(from_utc)
        if to_utc:
            sql += " AND e.start_utc <= ?"
            args.append(to_utc)
        sql += " ORDER BY e.start_utc ASC LIMIT ?"
        args.append(max(1, min(int(limit or 5000), 50000)))
        with self._connect() as c:
            return [_row_to_dict(r) for r in c.execute(sql, args).fetchall()]

    def confirm_downtime(self, event_id: str, reason_id: str = "",
                         category: str = "", comment: str = "",
                         actor: str = "") -> Dict[str, Any]:
        """Attach an operator's reason to a stop.

        An unconfirmed stop keeps reason NULL and is reported as "Unknown" by
        the Pareto - deliberately visible rather than quietly dropped.
        """
        with self._connect() as c:
            c.execute(
                "UPDATE oee_machine_events SET downtime_reason_id = ?, "
                "downtime_category = ?, operator_comment = ?, confirmed_by = ?, "
                "confirmed_utc = ? WHERE id = ? AND tenant_id = ?",
                (reason_id or None, category or None, comment or None,
                 actor or None, _now(), event_id, self._tenant()))
            c.commit()
        return self.get_event(event_id)

    # --------------------------------------------------------------- counts
    def add_count(self, machine_id: str, total: float = 0.0,
                  good: Optional[float] = None, reject: Optional[float] = None,
                  source: str = "manual", order_id: str = "",
                  product_id: str = "", cycle_id: str = "",
                  operator: str = "", ts_utc: str = "") -> Dict[str, Any]:
        now = ts_utc or _now()
        rec_id = _uid("cnt")
        with self._connect() as c:
            c.execute(
                "INSERT INTO oee_production_counts "
                "(id, tenant_id, machine_id, ts_utc, total_count, good_count, "
                " reject_count, source, order_id, product_id, cycle_id, operator, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec_id, self._tenant(), machine_id, now, float(total or 0.0),
                 None if good is None else float(good),
                 None if reject is None else float(reject),
                 source, order_id or None, product_id or None, cycle_id or None,
                 operator or None, now))
            c.commit()
        return {"id": rec_id, "machine_id": machine_id, "ts_utc": now,
                "total_count": total, "good_count": good, "reject_count": reject}

    def sum_counts(self, machine_id: str, from_utc: str, to_utc: str) -> Dict[str, Any]:
        with self._connect() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(total_count),0) AS total, "
                "       SUM(good_count) AS good, SUM(reject_count) AS reject, "
                "       COUNT(*) AS n "
                "FROM oee_production_counts WHERE tenant_id = ? AND machine_id = ? "
                "AND ts_utc >= ? AND ts_utc <= ?",
                (self._tenant(), machine_id, from_utc, to_utc)).fetchone()
        return {"total": float(row["total"] or 0.0),
                "good": None if row["good"] is None else float(row["good"]),
                "reject": None if row["reject"] is None else float(row["reject"]),
                "samples": int(row["n"] or 0)}

    def add_quality_result(self, machine_id: str, quantity: float,
                           result: str = "reject", reason_id: str = "",
                           operator: str = "", comment: str = "",
                           order_id: str = "", product_id: str = "",
                           cycle_id: str = "") -> Dict[str, Any]:
        now = _now()
        rec_id = _uid("qr")
        with self._connect() as c:
            c.execute(
                "INSERT INTO oee_quality_results "
                "(id, tenant_id, machine_id, ts_utc, quantity, result, "
                " quality_reason_id, order_id, product_id, cycle_id, operator, "
                " comment, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec_id, self._tenant(), machine_id, now, float(quantity or 0.0),
                 result, reason_id or None, order_id or None, product_id or None,
                 cycle_id or None, operator or None, comment or None, now))
            c.commit()
        return {"id": rec_id, "machine_id": machine_id, "quantity": quantity,
                "result": result}

    # --------------------------------------------------------------- cycles
    def start_cycle(self, machine_id: str, product_id: str = "",
                    order_id: str = "", source: str = "manual",
                    operator: str = "") -> Dict[str, Any]:
        now = _now()
        with self._connect() as c:
            # One open cycle per machine; a second Start closes the first
            # rather than leaving an orphan that never ends.
            open_rows = c.execute(
                "SELECT id, start_utc FROM oee_cycles WHERE machine_id = ? "
                "AND tenant_id = ? AND end_utc IS NULL",
                (machine_id, self._tenant())).fetchall()
            for row in open_rows:
                c.execute("UPDATE oee_cycles SET end_utc = ?, duration_s = ? WHERE id = ?",
                          (now, _seconds_between(row["start_utc"], now), row["id"]))
            cid = _uid("cyc")
            c.execute(
                "INSERT INTO oee_cycles (id, tenant_id, machine_id, product_id, "
                "order_id, start_utc, source, result, operator, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, self._tenant(), machine_id, product_id or None,
                 order_id or None, now, source, "unknown", operator or None, now))
            c.commit()
        return self.get_cycle(cid)

    def stop_cycle(self, machine_id: str, result: str = "unknown",
                   operator: str = "") -> Dict[str, Any]:
        now = _now()
        with self._connect() as c:
            row = c.execute(
                "SELECT id, start_utc FROM oee_cycles WHERE machine_id = ? "
                "AND tenant_id = ? AND end_utc IS NULL ORDER BY start_utc DESC LIMIT 1",
                (machine_id, self._tenant())).fetchone()
            if not row:
                return {}
            c.execute(
                "UPDATE oee_cycles SET end_utc = ?, duration_s = ?, result = ?, "
                "operator = COALESCE(?, operator) WHERE id = ?",
                (now, _seconds_between(row["start_utc"], now), result,
                 operator or None, row["id"]))
            c.commit()
            cid = row["id"]
        return self.get_cycle(cid)

    def get_cycle(self, cycle_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM oee_cycles WHERE id = ? AND tenant_id = ?",
                (cycle_id, self._tenant())).fetchone())

    def current_cycle(self, machine_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM oee_cycles WHERE machine_id = ? AND tenant_id = ? "
                "AND end_utc IS NULL ORDER BY start_utc DESC LIMIT 1",
                (machine_id, self._tenant())).fetchone())

    def list_cycles(self, machine_id: str = "", from_utc: str = "",
                    to_utc: str = "", limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM oee_cycles WHERE tenant_id = ?"
        args: List[Any] = [self._tenant()]
        if machine_id:
            sql += " AND machine_id = ?"
            args.append(machine_id)
        if from_utc:
            sql += " AND start_utc >= ?"
            args.append(from_utc)
        if to_utc:
            sql += " AND start_utc <= ?"
            args.append(to_utc)
        sql += " ORDER BY start_utc DESC LIMIT ?"
        args.append(max(1, min(int(limit or 500), 5000)))
        with self._connect() as c:
            return [_row_to_dict(r) for r in c.execute(sql, args).fetchall()]

    # ---------------------------------------------------------- calc cache
    def upsert_calculated(self, machine_id: str, bucket_start: str,
                          bucket_end: str, payload: Dict[str, Any]) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM oee_calculated_results WHERE tenant_id = ? "
                      "AND machine_id = ? AND bucket_start_utc = ?",
                      (self._tenant(), machine_id, bucket_start))
            c.execute(
                "INSERT INTO oee_calculated_results "
                "(id, tenant_id, machine_id, bucket_start_utc, bucket_end_utc, "
                " shift_id, order_id, product_id, planned_time_s, runtime_s, "
                " downtime_s, planned_stop_s, total_count, good_count, reject_count, "
                " availability, performance, quality, oee, energy_kwh, "
                " energy_wasted_kwh, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_uid("calc"), self._tenant(), machine_id, bucket_start, bucket_end,
                 payload.get("shift_id"), payload.get("order_id"),
                 payload.get("product_id"),
                 float(payload.get("planned_time_s") or 0.0),
                 float(payload.get("runtime_s") or 0.0),
                 float(payload.get("downtime_s") or 0.0),
                 float(payload.get("planned_stop_s") or 0.0),
                 float(payload.get("total_count") or 0.0),
                 float(payload.get("good_count") or 0.0),
                 float(payload.get("reject_count") or 0.0),
                 payload.get("availability"), payload.get("performance"),
                 payload.get("quality"), payload.get("oee"),
                 payload.get("energy_kwh"), payload.get("energy_wasted_kwh"),
                 _now()))
            c.commit()

    def upsert_energy(self, machine_id: str, bucket_start: str, bucket_end: str,
                      payload: Dict[str, Any]) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM oee_energy_summary WHERE tenant_id = ? "
                      "AND machine_id = ? AND bucket_start_utc = ?",
                      (self._tenant(), machine_id, bucket_start))
            c.execute(
                "INSERT INTO oee_energy_summary "
                "(id, tenant_id, machine_id, bucket_start_utc, bucket_end_utc, "
                " energy_total_kwh, energy_running_kwh, energy_idle_kwh, "
                " energy_stopped_kwh, energy_planned_stop_kwh, energy_wasted_kwh, "
                " avg_power_kw, peak_power_kw, created_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_uid("en"), self._tenant(), machine_id, bucket_start, bucket_end,
                 float(payload.get("energy_total_kwh") or 0.0),
                 float(payload.get("energy_running_kwh") or 0.0),
                 float(payload.get("energy_idle_kwh") or 0.0),
                 float(payload.get("energy_stopped_kwh") or 0.0),
                 float(payload.get("energy_planned_stop_kwh") or 0.0),
                 float(payload.get("energy_wasted_kwh") or 0.0),
                 payload.get("avg_power_kw"), payload.get("peak_power_kw"),
                 _now()))
            c.commit()

    # ------------------------------------------------------------ counters
    def counts(self) -> Dict[str, int]:
        """Row counts per OEE table - for diagnostics and the release gate."""
        out: Dict[str, int] = {}
        with self._connect() as c:
            for t in OEE_TABLES:
                try:
                    out[t] = int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                except Exception:
                    out[t] = -1
        return out


def _seconds_between(start_utc: str, end_utc: str) -> float:
    try:
        a = _dt.datetime.strptime(str(start_utc)[:23].replace("T", " "),
                                  "%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        try:
            a = _dt.datetime.strptime(str(start_utc)[:19].replace("T", " "),
                                      "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0
    try:
        b = _dt.datetime.strptime(str(end_utc)[:23].replace("T", " "),
                                  "%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        try:
            b = _dt.datetime.strptime(str(end_utc)[:19].replace("T", " "),
                                      "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0
    return max(0.0, (b - a).total_seconds())


def epoch_of(ts_utc: str) -> float:
    """'YYYY-MM-DD HH:MM:SS.mmm' (or ISO) to epoch seconds."""
    if not ts_utc:
        return 0.0
    text = str(ts_utc).replace("T", " ")[:23]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text[:len("2026-01-01 00:00:00.000")
                                              if "." in text else 19], fmt
                                         ).replace(tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            continue
    return 0.0


def utc_of(epoch_s: float) -> str:
    return _dt.datetime.fromtimestamp(float(epoch_s), _dt.timezone.utc
                                      ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
