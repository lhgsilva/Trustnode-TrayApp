"""Batch Management v2 — calculation engine (KPIs, excursions, data quality).

Runs after a batch reaches a terminal state (or on demand). Reads the historian by
the batch's data window(s) ONLY (never the collection path), computes:
  * per-tag stats + the fixed KPI set (spec §7),
  * limit excursions (spec 'Excursions'),
  * a data-quality assessment (spec §19),
  * the batch quality_status roll-up,
and persists them to batch_kpi_result / batch_excursion + updates the batch row.

Group KPIs (spec §7) aggregate child results.

Reuses: the execution service's window reads + limit definitions loaded from the
batch's definition version. All math is dependency-free (statistics stdlib +
helpers from service.py). Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

import sqlite3
import statistics
from typing import Any, Optional

from .service import _utc_now, _new_id, _json_load, _opt_num, _percentile, _window_seconds
from .service_v2 import _BatchV2Base, BatchExecutionService


# limit_type -> whether it's an upper or lower bound (for excursion direction)
_UPPER_LIMITS = {"operating_upper", "warning_upper", "spec_upper"}
_LOWER_LIMITS = {"operating_lower", "warning_lower", "spec_lower"}
# which limit types define the pass/fail spec envelope
_SPEC_UPPER = "spec_upper"
_SPEC_LOWER = "spec_lower"
_WARN_TYPES = {"warning_upper", "warning_lower", "operating_upper", "operating_lower"}


class BatchCalcService(_BatchV2Base):
    def __init__(self, app_store) -> None:
        super().__init__(app_store)
        self._exe = BatchExecutionService(app_store)

    # ------------------------------------------------------------------ #
    #  main entry: compute everything for one batch
    # ------------------------------------------------------------------ #
    def compute_batch(self, batch_id: str) -> dict[str, Any]:
        """Compute KPIs + excursions + data quality for a batch and persist.
        Returns a summary {kpis, excursions, quality_status, data_quality_status}."""
        tid = self._tenant()
        batch = self._exe.get_batch(batch_id)
        if not batch:
            raise ValueError("batch not found")

        limits = self._limits_for_batch(batch)            # {tag: [{limit_type,limit_value,severity,persistence_seconds}]}
        required_tags = self._required_tags_for_batch(batch)  # set[str]
        per_tag = self._gather_per_tag(batch_id)          # {tag: {vals:[...], first, last, ts_first, ts_last, bad, total}}
        windows = self._exe.windows_for_batch(batch_id)
        total_duration = sum(_window_seconds(w["window_start"], w["window_end"] or _utc_now()) for w in windows)
        hold_time = self._hold_time(batch_id)

        kpi_rows: list[dict[str, Any]] = []
        excursion_rows: list[dict[str, Any]] = []
        any_out_of_spec = False
        any_warning = False

        pass_tag_count = 0
        fail_tag_count = 0
        for tag, agg in per_tag.items():
            vals = agg["vals"]
            tag_limits = limits.get(tag, [])
            # --- KPIs per tag ---
            if vals:
                svals = sorted(vals)
                kpi_rows += self._tag_kpis(tag, agg, svals, total_duration, tag_limits)
            # --- excursions per tag ---
            exc, out_spec, warn = self._tag_excursions(batch, tag, agg, tag_limits)
            excursion_rows += exc
            any_out_of_spec = any_out_of_spec or out_spec
            any_warning = any_warning or warn
            # --- pass/fail per tag (only tags that HAVE a spec limit count toward pass%) ---
            has_spec = any(str(l.get("limit_type")) in (_SPEC_UPPER, _SPEC_LOWER) for l in tag_limits)
            if has_spec and vals:
                if out_spec:
                    fail_tag_count += 1
                else:
                    pass_tag_count += 1

        # --- batch-level KPIs ---
        kpi_rows.append(self._kpi("cycle_time", "Cycle Time", total_duration, "s"))
        kpi_rows.append(self._kpi("running_time", "Running Time", max(0.0, total_duration - hold_time), "s"))
        kpi_rows.append(self._kpi("hold_time", "Hold Time", hold_time, "s"))
        kpi_rows.append(self._kpi("excursion_count", "Excursions", float(len(excursion_rows)), "count"))

        # --- data quality (spec §19) ---
        dq = self._assess_data_quality(batch, per_tag, required_tags)

        # --- quality roll-up ---
        if dq["status"] in ("incomplete", "invalid"):
            quality = "data_incomplete"
        elif any_out_of_spec:
            quality = "out_of_specification"
        elif any_warning:
            quality = "with_warnings"
        elif per_tag:
            quality = "within_specification"
        else:
            quality = "not_evaluated"

        # mark KPI quality NOT VALID if required data missing
        if dq["status"] in ("incomplete", "invalid"):
            for k in kpi_rows:
                if k["quality_status"] == "valid":
                    k["quality_status"] = "incomplete"

        # persist (single tx: clear old + insert new + update batch)
        with self._connect() as c:
            c.execute("DELETE FROM batch_kpi_result WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid))
            c.execute("DELETE FROM batch_excursion WHERE batch_id = ? AND tenant_id = ?", (batch_id, tid))
            for k in kpi_rows:
                self._insert_kpi(c, tid, batch_id, None, k)
            for e in excursion_rows:
                self._insert_excursion(c, tid, batch_id, batch.get("batch_group_id"), e)
            self._exe.set_quality(c, tid, batch_id, quality=quality, data_quality=dq["status"])
            # 2026-07-15: stash the tag pass/fail counts on the batch metadata so the
            # Overview can show a Result (Passed/Failed) + pass% without extra queries.
            from .service import _json_load, _json_or_none
            _md = _json_load(batch.get("metadata")) if isinstance(batch.get("metadata"), str) else (batch.get("metadata") or {})
            if not isinstance(_md, dict):
                _md = {}
            _md["pass_tag_count"] = pass_tag_count
            _md["fail_tag_count"] = fail_tag_count
            _md["result"] = ("fail" if fail_tag_count > 0 else ("pass" if pass_tag_count > 0 else "na"))
            c.execute("UPDATE batch SET metadata_json = ? WHERE id = ? AND tenant_id = ?",
                      (_json_or_none(_md), batch_id, tid))
            self._event(c, batch_id=batch_id, batch_group_id=batch.get("batch_group_id"),
                        event_type="batch.calculated", source="system",
                        message=f"quality={quality} data_quality={dq['status']} kpis={len(kpi_rows)} excursions={len(excursion_rows)}")
            c.commit()

        return {
            "kpis": kpi_rows, "excursions": excursion_rows,
            "quality_status": quality, "data_quality_status": dq["status"],
            "data_quality_detail": dq,
        }

    # ------------------------------------------------------------------ #
    #  reads
    # ------------------------------------------------------------------ #
    def list_kpis(self, batch_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT * FROM batch_kpi_result WHERE batch_id = ? AND tenant_id = ? ORDER BY kpi_code",
                (batch_id, tid)).fetchall()
        return [dict(r) for r in rows]

    def list_excursions(self, *, batch_id: Optional[str] = None, batch_group_id: Optional[str] = None,
                        limit: int = 500) -> list[dict[str, Any]]:
        tid = self._tenant()
        where = ["tenant_id = ?"]
        params: list[Any] = [tid]
        if batch_id:
            where.append("batch_id = ?"); params.append(batch_id)
        if batch_group_id:
            where.append("batch_group_id = ?"); params.append(batch_group_id)
        wc = " AND ".join(where)
        with self._connect_readonly() as c:
            rows = c.execute(
                f"SELECT * FROM batch_excursion WHERE {wc} ORDER BY started_utc DESC LIMIT ?",
                (*params, max(1, min(limit, 5000)))).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_excursion(self, excursion_id: str, *, acknowledged: bool = True,
                              actor: Optional[str] = None, comment: Optional[str] = None) -> dict[str, Any]:
        tid = self._tenant()
        now = _utc_now()
        with self._connect() as c:
            c.execute(
                "UPDATE batch_excursion SET acknowledged=?, acknowledged_by=?, acknowledged_utc=?, comment=COALESCE(?, comment) "
                "WHERE id = ? AND tenant_id = ?",
                (1 if acknowledged else 0, actor, now if acknowledged else None, comment, excursion_id, tid))
            c.commit()
            r = c.execute("SELECT * FROM batch_excursion WHERE id = ? AND tenant_id = ?", (excursion_id, tid)).fetchone()
        return dict(r) if r else {}

    # ------------------------------------------------------------------ #
    #  group aggregation (spec §7 group KPIs)
    # ------------------------------------------------------------------ #
    def compute_group(self, group_id: str) -> dict[str, Any]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            children = c.execute(
                "SELECT id, status, started_utc, ended_utc FROM batch WHERE batch_group_id = ? AND tenant_id = ?",
                (group_id, tid)).fetchall()
        total = len(children)
        completed = sum(1 for b in children if str(b["status"]) == "completed")
        aborted = sum(1 for b in children if str(b["status"]) == "aborted")
        cycle_times = [
            _window_seconds(b["started_utc"], b["ended_utc"])
            for b in children if b["started_utc"] and b["ended_utc"]
        ]
        # roll up child excursion counts + energy/production totals from child KPIs
        with self._connect_readonly() as c:
            exc_total = c.execute(
                "SELECT COUNT(*) FROM batch_excursion WHERE batch_group_id = ? AND tenant_id = ?",
                (group_id, tid)).fetchone()[0]
            energy = c.execute(
                "SELECT COALESCE(SUM(numeric_value),0) FROM batch_kpi_result "
                "WHERE batch_group_id IS NULL AND kpi_code='total_energy' AND tenant_id = ? "
                "AND batch_id IN (SELECT id FROM batch WHERE batch_group_id = ? AND tenant_id = ?)",
                (tid, group_id, tid)).fetchone()[0]
            prod = c.execute(
                "SELECT COALESCE(SUM(numeric_value),0) FROM batch_kpi_result "
                "WHERE kpi_code='production_qty' AND tenant_id = ? "
                "AND batch_id IN (SELECT id FROM batch WHERE batch_group_id = ? AND tenant_id = ?)",
                (tid, group_id, tid)).fetchone()[0]

        krows = [
            self._kpi("total_child_batches", "Total Batches", float(total), "count"),
            self._kpi("completed_child_batches", "Completed Batches", float(completed), "count"),
            self._kpi("aborted_child_batches", "Aborted Batches", float(aborted), "count"),
            self._kpi("total_excursions", "Total Excursions", float(exc_total), "count"),
            self._kpi("total_energy", "Total Energy", float(energy), None),
            self._kpi("total_production_qty", "Total Production", float(prod), None),
        ]
        if cycle_times:
            krows += [
                self._kpi("avg_cycle_time", "Avg Cycle Time", statistics.fmean(cycle_times), "s"),
                self._kpi("min_cycle_time", "Min Cycle Time", min(cycle_times), "s"),
                self._kpi("max_cycle_time", "Max Cycle Time", max(cycle_times), "s"),
            ]
        exp = None
        with self._connect_readonly() as c:
            g = c.execute("SELECT expected_child_count FROM batch_group WHERE id = ? AND tenant_id = ?",
                          (group_id, tid)).fetchone()
            exp = g["expected_child_count"] if g else None
        pct = (100.0 * completed / exp) if exp else (100.0 * completed / total if total else 0.0)
        krows.append(self._kpi("completion_pct", "Completion %", round(pct, 1), "%"))

        with self._connect() as c:
            c.execute("DELETE FROM batch_kpi_result WHERE batch_group_id = ? AND tenant_id = ?", (group_id, tid))
            for k in krows:
                self._insert_kpi(c, tid, None, group_id, k)
            self._event(c, batch_group_id=group_id, event_type="group.calculated", source="system",
                        message=f"children={total} completed={completed} excursions={exc_total}")
            c.commit()
        return {"kpis": krows}

    def list_group_kpis(self, group_id: str) -> list[dict[str, Any]]:
        tid = self._tenant()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT * FROM batch_kpi_result WHERE batch_group_id = ? AND tenant_id = ? ORDER BY kpi_code",
                (group_id, tid)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  internals
    # ------------------------------------------------------------------ #
    def _gather_per_tag(self, batch_id: str) -> dict[str, dict[str, Any]]:
        """One pass over the window rows -> per-tag numeric series + quality tally.
        Rows come newest-first from historian_rows_for_batch; we track first/last
        by timestamp."""
        rows = self._exe.historian_rows_for_batch(batch_id, limit=50000)
        per: dict[str, dict[str, Any]] = {}
        for r in rows:
            tag = str(r.get("tag_name") or "")
            if not tag:
                continue
            slot = per.setdefault(tag, {"vals": [], "total": 0, "bad": 0,
                                        "first": None, "last": None,
                                        "ts_first": None, "ts_last": None})
            slot["total"] += 1
            q = r.get("quality")
            # OPC "good" is typically 192; treat None as unknown-good, <64 as bad
            if q is not None and isinstance(q, (int, float)) and q < 64:
                slot["bad"] += 1
            v = r.get("value")
            if v is None:
                continue
            try:
                fv = float(v)
            except Exception:
                continue
            slot["vals"].append(fv)
            ts = str(r.get("ts_utc") or "")
            # rows are DESC, so the first time we see the tag = latest; keep extremes
            if slot["ts_last"] is None or ts > slot["ts_last"]:
                slot["ts_last"] = ts; slot["last"] = fv
            if slot["ts_first"] is None or ts < slot["ts_first"]:
                slot["ts_first"] = ts; slot["first"] = fv
        return per

    def _tag_kpis(self, tag, agg, svals, total_duration, tag_limits) -> list[dict[str, Any]]:
        n = len(svals)
        out = [
            self._kpi("min", f"{tag} Min", svals[0], None, tag=tag),
            self._kpi("max", f"{tag} Max", svals[-1], None, tag=tag),
            self._kpi("avg", f"{tag} Avg", statistics.fmean(svals), None, tag=tag),
            self._kpi("first", f"{tag} First", agg["first"], None, tag=tag),
            self._kpi("last", f"{tag} Last", agg["last"], None, tag=tag),
            self._kpi("count", f"{tag} Count", float(n), "count", tag=tag),
            self._kpi("total", f"{tag} Total", float(sum(svals)), None, tag=tag),
        ]
        # time within / outside spec envelope (approx: fraction of samples * duration)
        lo = self._limit_value(tag_limits, _SPEC_LOWER)
        hi = self._limit_value(tag_limits, _SPEC_UPPER)
        if (lo is not None or hi is not None) and n:
            within = sum(1 for v in svals if (lo is None or v >= lo) and (hi is None or v <= hi))
            frac = within / n
            out.append(self._kpi("time_within", f"{tag} Time In-Spec", round(frac * total_duration, 1), "s", tag=tag))
            out.append(self._kpi("time_outside", f"{tag} Time Out-of-Spec", round((1 - frac) * total_duration, 1), "s", tag=tag))
        return out

    def _tag_excursions(self, batch, tag, agg, tag_limits) -> tuple[list[dict[str, Any]], bool, bool]:
        """Detect excursions vs each enabled limit. Returns (rows, any_out_of_spec, any_warning).
        Simplified v1: one excursion record per breached limit over the whole window,
        with actual min/max. (Per-sample interval merging is deferred.)"""
        vals = agg["vals"]
        if not vals:
            return [], False, False
        vmin, vmax = min(vals), max(vals)
        rows: list[dict[str, Any]] = []
        out_of_spec = False
        warning = False
        for lim in tag_limits:
            lt = str(lim.get("limit_type") or "")
            lv = _opt_num(lim.get("limit_value"))
            if lv is None:
                continue
            breached = False
            if lt in _UPPER_LIMITS and vmax > lv:
                breached = True
            elif lt in _LOWER_LIMITS and vmin < lv:
                breached = True
            if not breached:
                continue
            sev = str(lim.get("severity") or "warning")
            rows.append({
                "tag_name": tag, "gateway_id": batch.get("equipment_id"),
                "limit_type": lt, "limit_value": lv,
                "actual_minimum": vmin, "actual_maximum": vmax,
                "started_utc": agg.get("ts_first"), "ended_utc": agg.get("ts_last"),
                "duration_seconds": _window_seconds(agg.get("ts_first") or "", agg.get("ts_last") or ""),
                "severity": sev,
            })
            if lt in (_SPEC_UPPER, _SPEC_LOWER):
                out_of_spec = True
            else:
                warning = True
        return rows, out_of_spec, warning

    def _assess_data_quality(self, batch, per_tag, required_tags) -> dict[str, Any]:
        """spec §19 basic data-quality assessment."""
        actual_samples = sum(a["total"] for a in per_tag.values())
        bad_samples = sum(a["bad"] for a in per_tag.values())
        present = set(per_tag.keys())
        missing_required = sorted(required_tags - present)
        detail = {
            "actual_sample_count": actual_samples,
            "bad_sample_count": bad_samples,
            "missing_required_tags": missing_required,
            "tags_with_data": len(present),
        }
        if missing_required:
            detail["status"] = "incomplete"
        elif actual_samples == 0:
            detail["status"] = "incomplete" if required_tags else "not_evaluated"
        elif bad_samples > 0 and (bad_samples / max(1, actual_samples)) > 0.10:
            detail["status"] = "good_with_warnings"
        elif bad_samples > 0:
            detail["status"] = "good_with_warnings"
        else:
            detail["status"] = "good"
        return detail

    def _hold_time(self, batch_id: str) -> float:
        """Sum of time spent HELD, derived from batch_event timeline (held -> next running/terminal)."""
        tid = self._tenant()
        with self._connect_readonly() as c:
            evs = c.execute(
                "SELECT event_type, event_utc FROM batch_event WHERE batch_id = ? AND tenant_id = ? "
                "AND event_type IN ('batch.held','batch.running','batch.completed','batch.aborted') "
                "ORDER BY event_utc ASC", (batch_id, tid)).fetchall()
        total = 0.0
        held_at: Optional[str] = None
        for e in evs:
            et = str(e["event_type"])
            if et == "batch.held":
                held_at = str(e["event_utc"])
            elif held_at is not None:
                total += _window_seconds(held_at, str(e["event_utc"]))
                held_at = None
        return total

    def _limits_for_batch(self, batch) -> dict[str, list[dict[str, Any]]]:
        """Load the definition version's per-tag limits: {tag: [limit dicts]}."""
        tid = self._tenant()
        ver = batch.get("definition_version_id")
        out: dict[str, list[dict[str, Any]]] = {}
        if not ver:
            return out
        with self._connect_readonly() as c:
            rows = c.execute(
                """
                SELECT t.tag_name AS tag_name, l.limit_type, l.limit_value, l.severity, l.persistence_seconds
                FROM batch_definition_tag t
                JOIN batch_limit_definition l ON l.definition_tag_id = t.id
                WHERE t.definition_version_id = ? AND l.enabled = 1
                """,
                (ver,)).fetchall()
        for r in rows:
            out.setdefault(str(r["tag_name"]), []).append(dict(r))
        return out

    def _required_tags_for_batch(self, batch) -> set[str]:
        tid = self._tenant()
        ver = batch.get("definition_version_id")
        if not ver:
            return set()
        with self._connect_readonly() as c:
            rows = c.execute(
                "SELECT tag_name FROM batch_definition_tag WHERE definition_version_id = ? AND required = 1",
                (ver,)).fetchall()
        return {str(r["tag_name"]) for r in rows}

    @staticmethod
    def _limit_value(tag_limits, limit_type) -> Optional[float]:
        for l in tag_limits:
            if str(l.get("limit_type")) == limit_type:
                return _opt_num(l.get("limit_value"))
        return None

    @staticmethod
    def _kpi(code, label, value, unit, *, tag: Optional[str] = None, quality: str = "valid") -> dict[str, Any]:
        return {"kpi_code": code, "label": label, "numeric_value": value,
                "unit": unit, "quality_status": quality, "tag": tag}

    def _insert_kpi(self, c, tid, batch_id, group_id, k) -> None:
        c.execute(
            """
            INSERT INTO batch_kpi_result
              (id, tenant_id, batch_id, batch_group_id, kpi_definition_id, kpi_code,
               label, numeric_value, text_value, unit, quality_status, calculated_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (_new_id("bkpi"), tid, batch_id, group_id, None, k["kpi_code"],
             k.get("label"), _opt_num(k.get("numeric_value")), k.get("text_value"),
             k.get("unit"), k.get("quality_status") or "valid", _utc_now()),
        )

    def _insert_excursion(self, c, tid, batch_id, group_id, e) -> None:
        c.execute(
            """
            INSERT INTO batch_excursion
              (id, tenant_id, batch_id, batch_group_id, definition_tag_id, tag_name,
               gateway_id, limit_type, limit_value, actual_minimum, actual_maximum,
               started_utc, ended_utc, duration_seconds, severity, acknowledged, created_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (_new_id("bexc"), tid, batch_id, group_id, None, e["tag_name"],
             e.get("gateway_id"), e["limit_type"], _opt_num(e.get("limit_value")),
             _opt_num(e.get("actual_minimum")), _opt_num(e.get("actual_maximum")),
             e.get("started_utc"), e.get("ended_utc"), _opt_num(e.get("duration_seconds")),
             e.get("severity") or "warning", 0, _utc_now()),
        )
