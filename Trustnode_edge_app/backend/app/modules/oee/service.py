# -*- coding: utf-8 -*-
"""The OEE service: turns configuration + collected data into answers.

This is the only place that joins the OEE tables to the COLLECTION system.
It reads the historian and the live cache through AppStore; it never opens a
gateway, never polls a PLC and never writes a tag. OEE is a consumer of the
existing collection engine, exactly as specified.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import calc
from .state_engine import (
    SignalReading, evaluate_power_state, evaluate_signal_state, extract_counts,
    extract_text, resolve_state, STATE_UNKNOWN,
)
from .store import OeeStore, epoch_of, utc_of, _now as _store_now

_LOG = logging.getLogger("trustnode.oee")

# How old a tag sample may be before the machine's state is "missing data"
# rather than whatever the last value said. A stale reading that keeps a
# machine "running" for hours is how an availability figure becomes a lie.
STALE_AFTER_S = 120.0


class OeeService:
    def __init__(self, app_store: Any) -> None:
        self._app = app_store
        self.store = OeeStore(app_store)

    # ------------------------------------------------------------ helpers
    def _machines(self, only_enabled: bool = True) -> List[Dict[str, Any]]:
        rows = self.store.list_entities("machines")
        if only_enabled:
            rows = [m for m in rows if m.get("enabled") and m.get("oee_enabled")]
        return rows

    def _live_values(self) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """The newest value per (gateway_id, tag) from the app store's cache."""
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            cache = getattr(self._app, "_local_live_latest_cache", {}) or {}
            for key, row in cache.items():
                try:
                    _tenant, gw, tag = key
                except Exception:
                    continue
                out[(str(gw), str(tag))] = dict(row)
        except Exception:
            pass
        return out

    def _readings_for(self, mappings: List[Dict[str, Any]],
                      live: Dict[Tuple[str, str], Dict[str, Any]]
                      ) -> Dict[str, SignalReading]:
        """Current value + age for every tag a machine's mappings reference."""
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        out: Dict[str, SignalReading] = {}
        for m in mappings or []:
            tag = str(m.get("tag_name") or "")
            gw = str(m.get("gateway_id") or "")
            if not tag:
                continue
            row = live.get((gw, tag))
            if row is None:
                # Fall back to any gateway carrying that tag name - a machine
                # re-pointed at a rebuilt gateway should not go dark.
                for (g, t), r in live.items():
                    if t == tag:
                        row = r
                        break
            if row is None:
                out[tag] = SignalReading(stale=True)
                continue
            ts = epoch_of(str(row.get("ts_utc") or ""))
            age = now - ts if ts else None
            out[tag] = SignalReading(
                value=row.get("value") if row.get("value") is not None
                else row.get("value_text"),
                previous=None,
                seconds_since_change=age,
                stale=bool(age is not None and age > STALE_AFTER_S),
            )
        return out

    # ------------------------------------------------- planned time / shifts
    def planned_windows(self, from_epoch: float, to_epoch: float
                        ) -> List[Tuple[float, float]]:
        """Shift intervals overlapping the window, in epoch seconds.

        With NO shifts configured the whole window is planned production time.
        That default matters: otherwise a fresh install shows "not enough data"
        on every machine until somebody visits the shift page, and the module
        looks broken when it is merely unconfigured.
        """
        shifts = [s for s in self.store.list_entities("shifts") if s.get("enabled")]
        if not shifts:
            return [(from_epoch, to_epoch)]

        windows: List[Tuple[float, float]] = []
        day = _dt.datetime.fromtimestamp(from_epoch, _dt.timezone.utc).date()
        last = _dt.datetime.fromtimestamp(to_epoch, _dt.timezone.utc).date()
        while day <= last + _dt.timedelta(days=1):
            iso_dow = day.isoweekday()
            for s in shifts:
                days = {int(x) for x in str(s.get("working_days") or "").split(",")
                        if str(x).strip().isdigit()}
                if days and iso_dow not in days:
                    continue
                start = _parse_hhmm(day, str(s.get("start_time") or "00:00"))
                end = _parse_hhmm(day, str(s.get("end_time") or "23:59"))
                if end <= start:                      # shift crosses midnight
                    end += _dt.timedelta(days=1)
                ws, we = start.timestamp(), end.timestamp()
                brk = float(s.get("break_minutes") or 0.0) * 60.0
                if brk > 0:
                    we = max(ws, we - brk)            # breaks shorten planned time
                if we > from_epoch and ws < to_epoch:
                    windows.append((max(ws, from_epoch), min(we, to_epoch)))
            day += _dt.timedelta(days=1)
        return windows or [(from_epoch, to_epoch)]

    def excluded_stops(self, machine_id: str, from_epoch: float,
                       to_epoch: float) -> List[Tuple[float, float]]:
        """Planned stops that come OUT of planned production time."""
        out: List[Tuple[float, float]] = []
        for ps in self.store.list_entities("planned_stops"):
            if not ps.get("enabled") or not ps.get("exclude_from_oee"):
                continue
            mid = str(ps.get("machine_id") or "")
            if mid and mid != machine_id:
                continue
            if ps.get("start_utc") and ps.get("end_utc"):
                a, b = epoch_of(ps["start_utc"]), epoch_of(ps["end_utc"])
                if b > from_epoch and a < to_epoch:
                    out.append((a, b))
                continue
            start_t, end_t = ps.get("start_time"), ps.get("end_time")
            if not start_t or not end_t:
                continue
            rule = str(ps.get("repeat_rule") or "daily")
            day = _dt.datetime.fromtimestamp(from_epoch, _dt.timezone.utc).date()
            last = _dt.datetime.fromtimestamp(to_epoch, _dt.timezone.utc).date()
            while day <= last:
                if rule == "weekdays" and day.isoweekday() > 5:
                    day += _dt.timedelta(days=1)
                    continue
                a = _parse_hhmm(day, str(start_t)).timestamp()
                b = _parse_hhmm(day, str(end_t)).timestamp()
                if b <= a:
                    b += 86400
                if b > from_epoch and a < to_epoch:
                    out.append((a, b))
                day += _dt.timedelta(days=1)
        return out

    def in_planned_stop(self, machine_id: str, at_epoch: float) -> bool:
        for a, b in self.excluded_stops(machine_id, at_epoch - 1, at_epoch + 1):
            if a <= at_epoch <= b:
                return True
        return False

    # ------------------------------------------------------- live machine
    def machine_snapshot(self, machine: Dict[str, Any],
                         live: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
                         record: bool = False) -> Dict[str, Any]:
        """Everything the Overview machine card and Operator screen show."""
        mid = str(machine.get("id") or "")
        live = live if live is not None else self._live_values()
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()

        mappings = [m for m in self.store.list_entities("signal_mappings", machine_id=mid)
                    if m.get("enabled")] if machine.get("signal_enabled") else []
        readings = self._readings_for(mappings, live)

        signal_v = evaluate_signal_state(mappings, readings) if mappings else None
        counts_now = extract_counts(mappings, readings)
        texts = extract_text(mappings, readings)

        power_kw = current_a = None
        power_v = None
        if machine.get("power_enabled"):
            pmm = self.store.list_entities("power_meter_mappings", machine_id=mid)
            pmm = pmm[0] if pmm else {}
            gw = str(pmm.get("gateway_id") or "")
            if pmm.get("power_tag"):
                row = live.get((gw, str(pmm["power_tag"])))
                power_kw = _num(row.get("value")) if row else None
            if pmm.get("current_tag"):
                row = live.get((gw, str(pmm["current_tag"])))
                current_a = _num(row.get("value")) if row else None
            rules = self.store.list_entities("power_state_rules", machine_id=mid)
            # `seconds_in_band` needs per-rule history we do not keep between
            # calls; the open event's age is a good proxy for "how long has it
            # been like this", which is what the durations are guarding.
            cur = self.store.current_event(mid)
            held_s = (now - epoch_of(cur.get("start_utc", ""))) if cur else 0.0
            held = {str(r.get("id")): held_s for r in rules}
            power_v = evaluate_power_state(rules, power_kw, current_a, held,
                                           counts_increasing=None)

        manual_v = None
        cur_event = self.store.current_event(mid)
        if str(cur_event.get("status_source") or "") == "manual":
            from .state_engine import StateVerdict
            manual_v = StateVerdict(state=str(cur_event.get("state") or STATE_UNKNOWN),
                                    source="manual", confidence="medium",
                                    detail="set by operator")

        verdict = resolve_state(
            machine, signal_v, power_v, manual_v,
            power_kw=power_kw,
            counts_increasing=None,
            in_planned_stop=self.in_planned_stop(mid, now),
        )

        # Persist a state CHANGE so the timeline (and every duration) is real.
        if record and verdict.state != str(cur_event.get("state") or ""):
            try:
                self.store.open_event(mid, verdict.state, verdict.source,
                                      verdict.confidence, verdict.detail,
                                      is_planned=(verdict.state == "planned_stop"))
                cur_event = self.store.current_event(mid)
            except Exception as exc:      # never let telemetry break a page load
                _LOG.warning("oee: could not record state for %s: %s", mid, exc)

        stop_seconds = 0.0
        if cur_event.get("start_utc"):
            stop_seconds = max(0.0, now - epoch_of(cur_event["start_utc"]))

        cycle = self.store.current_cycle(mid)
        return {
            "machine_id": mid,
            "name": machine.get("name"),
            "line": machine.get("line"),
            "area": machine.get("area"),
            "state": verdict.state,
            "status_source": verdict.source,
            "confidence": verdict.confidence,
            "detail": verdict.detail,
            "flags": verdict.flags,
            "since_utc": cur_event.get("start_utc"),
            "current_state_seconds": stop_seconds,
            "downtime_reason_id": cur_event.get("downtime_reason_id"),
            "downtime_category": cur_event.get("downtime_category"),
            "needs_reason": bool(
                cur_event.get("state") in calc.DOWNTIME_STATES
                and not cur_event.get("downtime_reason_id")),
            "event_id": cur_event.get("id"),
            "power_kw": power_kw,
            "current_a": current_a,
            "counts": counts_now,
            "product_code": texts.get("product_code"),
            "order_number": texts.get("order_number"),
            "cycle": cycle or None,
            "manual_enabled": bool(machine.get("manual_enabled")),
            "power_enabled": bool(machine.get("power_enabled")),
            "signal_enabled": bool(machine.get("signal_enabled")),
        }

    # ------------------------------------------------------------- results
    def machine_result(self, machine: Dict[str, Any], from_utc: str,
                       to_utc: str) -> Dict[str, Any]:
        """OEE for one machine over one window."""
        mid = str(machine.get("id") or "")
        a, b = epoch_of(from_utc), epoch_of(to_utc)
        events = self.store.list_events(mid, from_utc, to_utc)
        ev = [{"state": e.get("state"),
               "start": epoch_of(e.get("start_utc") or ""),
               "end": epoch_of(e["end_utc"]) if e.get("end_utc") else None,
               "downtime_category": e.get("reason_category") or e.get("downtime_category"),
               "downtime_reason": e.get("downtime_reason")} for e in events]

        timeline = calc.reduce_timeline(
            ev, a, b,
            planned_windows=self.planned_windows(a, b),
            excluded_stops=self.excluded_stops(mid, a, b))

        counts = self.store.sum_counts(mid, from_utc, to_utc)
        product = self._active_product(machine, mid)
        ict = None
        if product:
            ict = product.get("ideal_cycle_time_s")
        if not ict:
            ict = machine.get("ideal_cycle_time_s")

        result = calc.compute_oee(calc.OeeInputs(
            planned_time_s=timeline["planned_time_s"],
            runtime_s=timeline["runtime_s"],
            downtime_s=timeline["downtime_s"],
            planned_stop_s=timeline["planned_stop_s"],
            total_count=counts["total"],
            good_count=counts["good"],
            reject_count=counts["reject"],
            ideal_cycle_time_s=ict,
            allow_over_100=bool(machine.get("allow_over_100")),
        ))

        energy = self.machine_energy(machine, ev, a, b, timeline["by_state"])
        out = result.to_dict()
        out.update({
            "machine_id": mid,
            "machine_name": machine.get("name"),
            "line": machine.get("line"),
            "from_utc": from_utc, "to_utc": to_utc,
            "by_state": timeline["by_state"],
            "pareto": calc.downtime_pareto(ev, a, b),
            "energy": energy,
            "ideal_cycle_time_s": ict,
        })
        return out

    def _active_product(self, machine: Dict[str, Any], machine_id: str
                        ) -> Optional[Dict[str, Any]]:
        cyc = self.store.current_cycle(machine_id)
        pid = str((cyc or {}).get("product_id") or "")
        if not pid:
            for o in self.store.list_entities("orders", machine_id=machine_id):
                if str(o.get("status") or "") == "running":
                    pid = str(o.get("product_id") or "")
                    break
        if not pid:
            return None
        return self.store.get_entity("products", pid) or None

    def machine_energy(self, machine: Dict[str, Any], events: List[Dict[str, Any]],
                       from_epoch: float, to_epoch: float,
                       state_seconds: Dict[str, float]) -> Dict[str, Any]:
        """Energy split by state plus the wasted estimate, from the historian."""
        mid = str(machine.get("id") or "")
        if not machine.get("power_enabled"):
            return {"total_kwh": 0.0, "by_state": {}, "wasted_kwh": 0.0,
                    "available": False}
        pmm = self.store.list_entities("power_meter_mappings", machine_id=mid)
        pmm = pmm[0] if pmm else {}
        tag = str(pmm.get("power_tag") or "")
        if not tag:
            return {"total_kwh": 0.0, "by_state": {}, "wasted_kwh": 0.0,
                    "available": False}

        samples = self._power_samples(str(pmm.get("gateway_id") or ""), tag,
                                      from_epoch, to_epoch)
        if not samples:
            return {"total_kwh": 0.0, "by_state": {}, "wasted_kwh": 0.0,
                    "available": False}

        by_state = calc.energy_by_state(samples, events, from_epoch, to_epoch)
        total = calc.integrate_energy(samples, from_epoch, to_epoch)
        waste = calc.estimate_wasted_energy(
            by_state, state_seconds,
            machine.get("standby_power_kw"), machine.get("idle_power_kw"))
        kws = [v for _t, v in samples]
        return {
            "total_kwh": total,
            "by_state": by_state,
            "wasted_kwh": waste["total_kwh"],
            "waste_by_state": waste["by_state"],
            "avg_power_kw": (sum(kws) / len(kws)) if kws else None,
            "peak_power_kw": max(kws) if kws else None,
            "available": True,
        }

    def _power_samples(self, gateway_id: str, tag: str, from_epoch: float,
                       to_epoch: float) -> List[Tuple[float, float]]:
        """(epoch, kW) from the historian - the SAME rows the charts use."""
        try:
            rows = self._app.get_historian_rows_range(
                from_utc=utc_of(from_epoch), to_utc=utc_of(to_epoch),
                limit=20000, gateway=gateway_id or "", tag=tag)
        except Exception as exc:
            _LOG.warning("oee: historian read failed for %s/%s: %s",
                         gateway_id, tag, exc)
            return []
        out: List[Tuple[float, float]] = []
        for r in rows or []:
            v = _num(r.get("value"))
            ts = epoch_of(str(r.get("ts_utc") or r.get("ts") or ""))
            if v is not None and ts:
                out.append((ts, v))
        out.sort()
        return out

    # ------------------------------------------------------------- rollups
    def trend(self, machine_ids: List[str], from_utc: str, to_utc: str,
              buckets: int = 24) -> List[Dict[str, Any]]:
        """OEE per time bucket across the window, for the trend charts."""
        a, b = epoch_of(from_utc), epoch_of(to_utc)
        if b <= a:
            return []
        n = max(1, min(int(buckets or 24), 200))
        step = (b - a) / n
        machines = {str(m["id"]): m for m in self._machines()
                    if not machine_ids or str(m["id"]) in machine_ids}
        out: List[Dict[str, Any]] = []
        for i in range(n):
            bs, be = a + i * step, a + (i + 1) * step
            agg = {"runtime_s": 0.0, "downtime_s": 0.0, "planned_time_s": 0.0,
                   "total": 0.0, "good": 0.0, "reject": 0.0,
                   "energy_kwh": 0.0, "energy_wasted_kwh": 0.0}
            oees: List[float] = []
            avails: List[float] = []
            perfs: List[float] = []
            quals: List[float] = []
            bucket_results: List[Dict[str, Any]] = []
            for m in machines.values():
                r = self.machine_result(m, utc_of(bs), utc_of(be))
                bucket_results.append(r)
                agg["runtime_s"] += r["runtime_s"]
                agg["downtime_s"] += r["downtime_s"]
                agg["planned_time_s"] += r["planned_time_s"]
                agg["total"] += r["total_count"]
                agg["good"] += r["good_count"]
                agg["reject"] += r["reject_count"]
                agg["energy_kwh"] += float((r.get("energy") or {}).get("total_kwh") or 0.0)
                agg["energy_wasted_kwh"] += float((r.get("energy") or {}).get("wasted_kwh") or 0.0)
                if r["oee"] is not None:
                    oees.append(r["oee"])
                if r["availability"] is not None:
                    avails.append(r["availability"])
                if r["performance"] is not None:
                    perfs.append(r["performance"])
                if r["quality"] is not None:
                    quals.append(r["quality"])
            out.append({
                "bucket_start_utc": utc_of(bs),
                "bucket_end_utc": utc_of(be),
                "oee": (sum(oees) / len(oees)) if oees else None,
                "availability": (sum(avails) / len(avails)) if avails else None,
                "performance": (sum(perfs) / len(perfs)) if perfs else None,
                "quality": (sum(quals) / len(quals)) if quals else None,
                **agg,
                **self._rollup_maturity(bucket_results),
                # What the bucket COULD have made at the configured ideal
                # cycle time. This is the target Performance is measured
                # against, so the Production chart and the Performance KPI
                # cannot disagree. None when no cycle time is configured -
                # a dashed line at zero would read as "target: make nothing".
                "target_count": self._target_count(bucket_results),
            })
        return out

    def _previous_window(self, from_utc: str, to_utc: str) -> Tuple[str, str]:
        """The equal-length window ending where this one begins.

        `epoch_of` / `utc_of` are the module's own conversions, so the previous
        window is expressed exactly like the current one - a comparison built
        on a second date format is a comparison waiting to be wrong.
        """
        a = float(epoch_of(from_utc) or 0.0)
        b = float(epoch_of(to_utc) or 0.0)
        span = max(1.0, b - a)
        return utc_of(a - span), utc_of(a)

    @staticmethod
    def _target_count(results: List[Dict[str, Any]]) -> Optional[float]:
        """Pieces the configured ideal cycle time allows in this window."""
        total = 0.0
        seen = False
        for r in results:
            ict = r.get("ideal_cycle_time_s")
            try:
                ict = float(ict or 0.0)
            except (TypeError, ValueError):
                ict = 0.0
            if ict <= 0:
                continue
            seen = True
            total += float(r.get("planned_time_s") or 0.0) / ict
        return round(total, 2) if seen else None

    # Worst first: a roll-up must report the weakest measurement it contains,
    # never the best one it can find.
    _STAGE_ORDER = ("not_enough_data", "availability_only", "no_performance",
                    "no_quality", "full")

    def _rollup_maturity(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """One maturity label for a set of machine results."""
        stages = [str(r.get("stage") or "not_enough_data") for r in results]
        counted = sum(1 for r in results if r.get("oee") is not None)
        missing: List[str] = []
        for r in results:
            for f in (r.get("missing_factors") or []):
                if f not in missing:
                    missing.append(f)
        if not stages:
            worst = "not_configured"
        else:
            worst = min(stages, key=lambda st: (self._STAGE_ORDER.index(st)
                                                if st in self._STAGE_ORDER else 0))
        return {"stage": worst,
                "missing_factors": missing,
                "machines_counted": counted,
                "machines_total": len(results)}

    def overview(self, from_utc: str, to_utc: str,
                 machine_ids: Optional[List[str]] = None,
                 line: str = "", with_previous: bool = False) -> Dict[str, Any]:
        """The whole Overview page in one call."""
        machines = self._machines()
        if machine_ids:
            machines = [m for m in machines if str(m["id"]) in set(machine_ids)]
        if line:
            machines = [m for m in machines if str(m.get("line") or "") == line]

        live = self._live_values()
        cards, results = [], []
        for m in machines:
            cards.append(self.machine_snapshot(m, live, record=True))
            results.append(self.machine_result(m, from_utc, to_utc))

        # Roll the machines up. OEE is averaged across machines that HAVE a
        # figure; summing would be meaningless and averaging in a None as zero
        # would punish a line for a machine that was not scheduled.
        def _avg(key: str) -> Optional[float]:
            vals = [r[key] for r in results if r.get(key) is not None]
            return (sum(vals) / len(vals)) if vals else None

        totals = {
            "oee": _avg("oee"),
            "availability": _avg("availability"),
            "performance": _avg("performance"),
            "quality": _avg("quality"),
            "runtime_s": sum(r["runtime_s"] for r in results),
            "downtime_s": sum(r["downtime_s"] for r in results),
            "planned_time_s": sum(r["planned_time_s"] for r in results),
            "total_count": sum(r["total_count"] for r in results),
            "good_count": sum(r["good_count"] for r in results),
            "reject_count": sum(r["reject_count"] for r in results),
            "energy_kwh": sum(float((r.get("energy") or {}).get("total_kwh") or 0.0)
                              for r in results),
            "energy_wasted_kwh": sum(float((r.get("energy") or {}).get("wasted_kwh") or 0.0)
                                     for r in results),
            "power_kw_now": sum(float(c.get("power_kw") or 0.0) for c in cards),
            "machines": len(machines),
        }
        # How complete the plant figure is. The average above already skips
        # machines with no OEE, so the number itself is sound - but a plant
        # where four of nine machines have no cycle time configured must not
        # present its average with the same confidence as one where all nine
        # report. The weakest stage present is the honest headline.
        totals.update(self._rollup_maturity(results))

        # One Pareto for the whole selection.
        merged: Dict[Tuple[str, str], float] = {}
        for r in results:
            for row in r.get("pareto") or []:
                key = (row["category"], row["reason"])
                merged[key] = merged.get(key, 0.0) + row["seconds"]
        pareto = [{"category": c, "reason": r, "seconds": s}
                  for (c, r), s in merged.items()]
        pareto.sort(key=lambda x: x["seconds"], reverse=True)
        grand = sum(p["seconds"] for p in pareto) or 1.0
        run = 0.0
        for p in pareto:
            run += p["seconds"]
            p["share"] = p["seconds"] / grand
            p["cumulative"] = run / grand

        out = {"ok": True, "from_utc": from_utc, "to_utc": to_utc,
               "totals": totals, "machines": cards, "results": results,
               "pareto": pareto[:12]}
        if with_previous:
            # The window immediately before this one, of equal length, through
            # the SAME code path - so "vs previous" compares like with like
            # rather than against a differently-computed number.
            try:
                prev_from, prev_to = self._previous_window(from_utc, to_utc)
                prev = self.overview(prev_from, prev_to, machine_ids=machine_ids,
                                     line=line, with_previous=False)
                out["previous"] = {
                    "from_utc": prev_from, "to_utc": prev_to,
                    "totals": prev.get("totals") or {},
                }
            except Exception as exc:  # noqa: BLE001 - a comparison is a nicety
                out["previous"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
        return out


    # ================================================================
    # Dashboard aggregates (2026-08-29)
    # ================================================================

    def _meta(self, from_utc: str, to_utc: str, shift_id: str = "",
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The metadata every dashboard response carries.

        A dashboard that shows a number without saying what window, which
        shift, and how sure it is invites the reader to trust it more than the
        data deserves.
        """
        out = {
            "window": {"from_utc": from_utc, "to_utc": to_utc},
            "shift_id": shift_id or "",
            "timezone": "UTC",
            "generated_utc": _store_now(),
        }
        if extra:
            out.update(extra)
        return out

    def status_timeline(self, machine_ids: Optional[List[str]], from_utc: str,
                        to_utc: str, max_blocks: int = 2000) -> Dict[str, Any]:
        """State blocks per machine, for the timeline strip.

        Merges consecutive identical states so a machine that sat Running for
        six hours is one block, not 21 600. `max_blocks` is a hard cap and the
        response SAYS when it truncated - a silently shortened timeline reads
        as "nothing happened after lunch".
        """
        machines = self._machines()
        if machine_ids:
            wanted = set(machine_ids)
            machines = [m for m in machines if str(m["id"]) in wanted]

        lanes: List[Dict[str, Any]] = []
        truncated = False
        for m in machines:
            events = self.store.list_events(machine_id=str(m["id"]),
                                            from_utc=from_utc, to_utc=to_utc)
            blocks: List[Dict[str, Any]] = []
            for ev in events:
                state = str(ev.get("state") or "unknown")
                start = str(ev.get("start_utc") or "")
                end = str(ev.get("end_utc") or "")
                if not start:
                    continue
                if blocks and blocks[-1]["state"] == state and not blocks[-1]["end_utc"]:
                    continue
                if blocks and blocks[-1]["state"] == state:
                    blocks[-1]["end_utc"] = end or blocks[-1]["end_utc"]
                    continue
                if len(blocks) >= max_blocks:
                    truncated = True
                    break
                blocks.append({
                    "state": state,
                    "start_utc": start,
                    "end_utc": end,
                    "reason": str(ev.get("downtime_reason") or ev.get("reason") or ""),
                    "category": str(ev.get("reason_category") or ev.get("category") or ""),
                    "planned": bool(ev.get("planned")),
                    "source": str(ev.get("source") or ""),
                    "confidence": ev.get("confidence"),
                    "comment": str(ev.get("comment") or ""),
                })
            lanes.append({"machine_id": str(m["id"]),
                          "machine_name": str(m.get("name") or m["id"]),
                          "line": str(m.get("line") or ""),
                          "blocks": blocks})
        return {"ok": True, "lanes": lanes, "truncated": truncated,
                "max_blocks": max_blocks,
                "meta": self._meta(from_utc, to_utc,
                                   extra={"truncated": truncated})}

    #: What the Pareto can honestly be grouped by. Product, order and shift are
    #: absent on purpose: oee_machine_events has no product or order column, and
    #: a grouping built on a join that does not exist would look authoritative
    #: and be wrong.
    PARETO_GROUPS = ("reason", "category", "machine", "line")

    def downtime_pareto(self, machine_ids: Optional[List[str]], from_utc: str,
                        to_utc: str, limit: int = 12,
                        group_by: str = "reason",
                        metric: str = "duration") -> Dict[str, Any]:
        """Downtime ranked by duration or by number of stops.

        `group_by` is one of PARETO_GROUPS. `metric` is "duration" or "stops" -
        the ordering changes, both figures are always returned, so the UI can
        toggle without another round trip.
        """
        group = str(group_by or "reason").strip().lower()
        if group not in self.PARETO_GROUPS:
            group = "reason"
        rank_by_stops = str(metric or "duration").strip().lower() == "stops"

        machines = self._machines()
        if machine_ids:
            wanted = set(machine_ids)
            machines = [m for m in machines if str(m["id"]) in wanted]

        merged: Dict[str, Dict[str, Any]] = {}
        for m in machines:
            res = self.machine_result(m, from_utc, to_utc)
            for row in res.get("pareto") or []:
                if group == "category":
                    label = str(row.get("category") or "Unknown")
                elif group == "machine":
                    label = str(m.get("name") or m.get("id") or "Unknown")
                elif group == "line":
                    label = str(m.get("line") or "") or "No line"
                else:
                    label = str(row.get("reason") or "Unknown")
                slot = merged.setdefault(label, {
                    "label": label, "category": str(row.get("category") or "Unknown"),
                    "reason": str(row.get("reason") or "Unknown"),
                    "seconds": 0.0, "stops": 0,
                })
                slot["seconds"] += float(row.get("seconds") or 0.0)
                slot["stops"] += int(row.get("stops") or 0)

        rows = list(merged.values())
        rows.sort(key=lambda x: (x["stops"] if rank_by_stops else x["seconds"]), reverse=True)
        grand = sum(r["seconds"] for r in rows) or 1.0
        grand_stops = sum(r["stops"] for r in rows) or 1
        run = 0.0
        run_stops = 0
        for r in rows:
            run += r["seconds"]
            run_stops += r["stops"]
            r["share"] = r["seconds"] / grand
            r["cumulative"] = run / grand
            r["stops_share"] = r["stops"] / grand_stops
            r["stops_cumulative"] = run_stops / grand_stops
        return {"ok": True, "rows": rows[:limit], "total_seconds": grand,
                "total_stops": sum(r["stops"] for r in rows),
                "reason_count": len(rows),
                "group_by": group, "metric": "stops" if rank_by_stops else "duration",
                "groups_supported": list(self.PARETO_GROUPS),
                "meta": self._meta(from_utc, to_utc)}

    def energy_summary(self, machine_ids: Optional[List[str]], from_utc: str,
                       to_utc: str) -> Dict[str, Any]:
        """Energy used and estimated waste, per machine."""
        machines = self._machines()
        if machine_ids:
            wanted = set(machine_ids)
            machines = [m for m in machines if str(m["id"]) in wanted]
        rows, total, wasted = [], 0.0, 0.0
        for m in machines:
            res = self.machine_result(m, from_utc, to_utc)
            e = res.get("energy") or {}
            kwh = float(e.get("total_kwh") or 0.0)
            waste = float(e.get("wasted_kwh") or 0.0)
            total += kwh
            wasted += waste
            rows.append({"machine_id": str(m["id"]),
                         "machine_name": str(m.get("name") or m["id"]),
                         "line": str(m.get("line") or ""),
                         "total_kwh": kwh, "wasted_kwh": waste,
                         "waste_share": (waste / kwh) if kwh else None})
        rows.sort(key=lambda r: r["wasted_kwh"], reverse=True)
        return {"ok": True, "rows": rows, "total_kwh": total,
                "wasted_kwh": wasted,
                "waste_share": (wasted / total) if total else None,
                "meta": self._meta(from_utc, to_utc)}

    def shift_performance(self, machine_ids: Optional[List[str]],
                          from_utc: str, to_utc: str) -> Dict[str, Any]:
        """OEE per configured shift across the window.

        `machine_result` has no shift argument - it takes a window - so each
        shift is resolved to its own intervals and the machine is evaluated
        over those. Reusing the same HH:MM / working-days / break arithmetic as
        `planned_windows` rather than writing a second copy of it, because two
        implementations of a shift boundary will disagree at midnight.
        """
        shifts = [s for s in self.store.list_entities("shifts") if s.get("enabled")]
        machines = self._machines()
        if machine_ids:
            wanted = set(machine_ids)
            machines = [m for m in machines if str(m["id"]) in wanted]
        a, b = epoch_of(from_utc), epoch_of(to_utc)

        rows = []
        for sh in shifts:
            spans: List[Tuple[float, float]] = []
            day = _dt.datetime.fromtimestamp(a, _dt.timezone.utc).date()
            last = _dt.datetime.fromtimestamp(b, _dt.timezone.utc).date()
            while day <= last + _dt.timedelta(days=1):
                days = {int(x) for x in str(sh.get("working_days") or "").split(",")
                        if str(x).strip().isdigit()}
                if not days or day.isoweekday() in days:
                    start = _parse_hhmm(day, str(sh.get("start_time") or "00:00"))
                    end = _parse_hhmm(day, str(sh.get("end_time") or "23:59"))
                    if end <= start:
                        end += _dt.timedelta(days=1)
                    ws, we = start.timestamp(), end.timestamp()
                    if we > a and ws < b:
                        spans.append((max(ws, a), min(we, b)))
                day += _dt.timedelta(days=1)

            vals, run_s, down_s = [], 0.0, 0.0
            for ws, we in spans:
                w_from, w_to = utc_of(ws), utc_of(we)
                for m in machines:
                    res = self.machine_result(m, w_from, w_to)
                    if res.get("oee") is not None:
                        vals.append(res["oee"])
                    run_s += float(res.get("runtime_s") or 0.0)
                    down_s += float(res.get("downtime_s") or 0.0)
            rows.append({"shift_id": str(sh.get("id") or ""),
                         "shift_name": str(sh.get("name") or sh.get("id") or ""),
                         "oee": (sum(vals) / len(vals)) if vals else None,
                         "runtime_s": run_s, "downtime_s": down_s,
                         "spans": len(spans),
                         "machines_with_oee": len(vals)})
        return {"ok": True, "rows": rows, "meta": self._meta(from_utc, to_utc)}

    def planned_events(self, from_utc: str, to_utc: str,
                       machine_ids: Optional[List[str]] = None,
                       line: str = "") -> Dict[str, Any]:
        """Planning-calendar events overlapping the window."""
        rows = self.store.list_entities("planned_events", include_disabled=False)
        wanted = set(machine_ids or [])
        out = []
        for r in rows:
            start, end = str(r.get("start_utc") or ""), str(r.get("end_utc") or "")
            if not start or not end:
                continue
            if to_utc and start > to_utc:
                continue
            if from_utc and end < from_utc:
                continue
            if wanted and str(r.get("machine_id") or "") not in wanted:
                continue
            if line and str(r.get("line") or "") != line:
                continue
            out.append(r)
        return {"ok": True, "events": out, "meta": self._meta(from_utc, to_utc)}


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return 1.0 if value is True else (0.0 if value is False else None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_hhmm(day: _dt.date, hhmm: str) -> _dt.datetime:
    try:
        h, m = str(hhmm).split(":")[:2]
        return _dt.datetime(day.year, day.month, day.day, int(h), int(m),
                            tzinfo=_dt.timezone.utc)
    except Exception:
        return _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
