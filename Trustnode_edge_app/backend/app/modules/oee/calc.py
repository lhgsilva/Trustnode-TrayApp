# -*- coding: utf-8 -*-
"""OEE arithmetic, kept free of I/O so it can be tested on plain values.

    OEE = Availability x Performance x Quality

    Availability = Runtime / PlannedProductionTime
    Performance  = (IdealCycleTime x TotalCount) / Runtime
    Quality      = GoodCount / TotalCount

Every function returns None rather than 0.0 when the inputs cannot support an
answer. That distinction is the whole point: a machine that produced nothing
because it was off has NO performance figure, and showing it as 0% makes a
shift look catastrophic when it was simply not scheduled. The UI renders None
as "Not enough data".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# States that count as productive time.
RUNNING_STATES = frozenset({"running", "production"})

# States that are downtime when they happen inside planned production time.
DOWNTIME_STATES = frozenset({
    "idle", "stopped", "faulted", "changeover",
    "waiting_material", "waiting_operator", "unknown",
})

# Not downtime: the machine was not supposed to be producing.
NEUTRAL_STATES = frozenset({"planned_stop", "off"})

ALL_STATES = RUNNING_STATES | DOWNTIME_STATES | NEUTRAL_STATES


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    """A ratio, or None when the denominator cannot support one.

    Returning None (not 0.0) is deliberate - see the module docstring.
    """
    try:
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    try:
        n = float(numerator)
    except (TypeError, ValueError):
        return None
    return n / d


def clamp_factor(value: Optional[float], allow_over_100: bool = False) -> Optional[float]:
    """Keep a factor in 0..1 unless the machine is configured to allow more.

    A performance figure above 100% means the ideal cycle time is wrong, not
    that the machine beat physics. Clamping keeps one bad configuration from
    producing an OEE of 340% on a dashboard; the raw value stays available in
    the detail payload so the mistake is still findable.
    """
    if value is None:
        return None
    v = float(value)
    if v < 0:
        return 0.0
    if not allow_over_100 and v > 1.0:
        return 1.0
    return v


@dataclass
class OeeInputs:
    """Everything one OEE result needs, already reduced to numbers."""
    planned_time_s: float = 0.0     # scheduled time minus excluded planned stops
    runtime_s: float = 0.0          # time in a running state inside planned time
    downtime_s: float = 0.0
    planned_stop_s: float = 0.0
    total_count: float = 0.0
    good_count: Optional[float] = None
    reject_count: Optional[float] = None
    ideal_cycle_time_s: Optional[float] = None
    allow_over_100: bool = False


@dataclass
class OeeResult:
    availability: Optional[float] = None
    performance: Optional[float] = None
    quality: Optional[float] = None
    oee: Optional[float] = None
    runtime_s: float = 0.0
    downtime_s: float = 0.0
    planned_time_s: float = 0.0
    planned_stop_s: float = 0.0
    total_count: float = 0.0
    good_count: float = 0.0
    reject_count: float = 0.0
    # Why a factor is missing, so the UI can say something better than "-".
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "availability": self.availability,
            "performance": self.performance,
            "quality": self.quality,
            "oee": self.oee,
            "runtime_s": self.runtime_s,
            "downtime_s": self.downtime_s,
            "planned_time_s": self.planned_time_s,
            "planned_stop_s": self.planned_stop_s,
            "total_count": self.total_count,
            "good_count": self.good_count,
            "reject_count": self.reject_count,
            "notes": list(self.notes),
        }


def resolve_counts(total: float,
                   good: Optional[float],
                   reject: Optional[float]) -> Tuple[float, float, float]:
    """Fill in whichever of total/good/reject was not measured.

    Rule from the spec: when good count is missing, Good = Total - Reject. The
    reverse is just as common on a line that only counts scrap, so it is
    handled too. Nothing is allowed to go negative.
    """
    t = max(0.0, float(total or 0.0))
    g = None if good is None else max(0.0, float(good))
    r = None if reject is None else max(0.0, float(reject))

    if g is None and r is None:
        # Nothing but a total: assume it is all good, and say so upstream.
        return t, t, 0.0
    if g is None:
        g = max(0.0, t - (r or 0.0))
        return t, g, (r or 0.0)
    if r is None:
        r = max(0.0, t - g)
        return t, g, r
    # Both measured. If they disagree with the total, the total wins for
    # Quality's denominator but the measured parts are kept as reported.
    return t, g, r


def compute_oee(inp: OeeInputs) -> OeeResult:
    """One OEE result from one window of already-reduced numbers."""
    res = OeeResult(
        runtime_s=float(inp.runtime_s or 0.0),
        downtime_s=float(inp.downtime_s or 0.0),
        planned_time_s=float(inp.planned_time_s or 0.0),
        planned_stop_s=float(inp.planned_stop_s or 0.0),
    )

    total, good, reject = resolve_counts(inp.total_count, inp.good_count, inp.reject_count)
    res.total_count, res.good_count, res.reject_count = total, good, reject

    # --- Availability -----------------------------------------------------
    if res.planned_time_s <= 0:
        res.notes.append("No planned production time in this window.")
    res.availability = clamp_factor(
        _ratio(res.runtime_s, res.planned_time_s), inp.allow_over_100)

    # --- Performance ------------------------------------------------------
    ict = inp.ideal_cycle_time_s
    if ict is None or float(ict) <= 0:
        res.notes.append("No ideal cycle time configured for the running product.")
        res.performance = None
    elif res.runtime_s <= 0:
        # Spec: if runtime is zero, performance is zero or unavailable. With no
        # runtime there is nothing to have performed against, so: unavailable.
        res.notes.append("No runtime recorded, so performance cannot be measured.")
        res.performance = None
    else:
        res.performance = clamp_factor(
            _ratio(float(ict) * total, res.runtime_s), inp.allow_over_100)

    # --- Quality ----------------------------------------------------------
    if total <= 0:
        res.notes.append("No production counted, so quality cannot be measured.")
        res.quality = None
    else:
        res.quality = clamp_factor(_ratio(good, total), inp.allow_over_100)

    # --- OEE --------------------------------------------------------------
    factors = (res.availability, res.performance, res.quality)
    if any(f is None for f in factors):
        res.oee = None
        if not res.notes:
            res.notes.append("Not enough data for a complete OEE figure.")
    else:
        res.oee = clamp_factor(factors[0] * factors[1] * factors[2], inp.allow_over_100)
    return res


# ---------------------------------------------------------------------------
# Timeline reduction — machine events to runtime / downtime
# ---------------------------------------------------------------------------
def _overlap_s(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Seconds shared by two [start, end) intervals."""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, hi - lo)


def reduce_timeline(events: Iterable[Dict[str, Any]],
                    window_start: float,
                    window_end: float,
                    planned_windows: Optional[List[Tuple[float, float]]] = None,
                    excluded_stops: Optional[List[Tuple[float, float]]] = None
                    ) -> Dict[str, float]:
    """Turn a state timeline into runtime / downtime / planned-stop seconds.

    `events` are dicts with epoch-second `start` / `end` and a `state`. An event
    with no end is treated as running up to `window_end`, which is what makes
    the CURRENT stop duration correct on a live dashboard.

    `planned_windows` are the shift intervals. When none are given the whole
    window counts as planned, which is the right default for a site that has
    not configured shifts yet - otherwise every OEE would read "not enough
    data" until somebody filled in the shift page.

    `excluded_stops` are planned stops with exclude_from_oee set; their overlap
    is removed from planned production time.
    """
    windows = list(planned_windows or [(window_start, window_end)])
    excluded = list(excluded_stops or [])

    planned_total = sum(_overlap_s(s, e, window_start, window_end) for s, e in windows)
    excluded_total = 0.0
    for xs, xe in excluded:
        for s, e in windows:
            excluded_total += _overlap_s(xs, xe, max(s, window_start), min(e, window_end))
    planned_time = max(0.0, planned_total - excluded_total)

    runtime = downtime = planned_stop = 0.0
    by_state: Dict[str, float] = {}

    for ev in events or []:
        state = str(ev.get("state") or "unknown")
        s = float(ev.get("start") or 0.0)
        e = ev.get("end")
        e = float(e) if e is not None else float(window_end)
        if e <= s:
            continue
        # Seconds of this event that fall inside a planned window.
        inside = 0.0
        for ws, we in windows:
            inside += _overlap_s(s, e, max(ws, window_start), min(we, window_end))
        # ...minus any excluded planned stop.
        for xs, xe in excluded:
            inside -= _overlap_s(s, e, max(xs, window_start), min(xe, window_end))
        inside = max(0.0, inside)

        by_state[state] = by_state.get(state, 0.0) + inside
        if state in RUNNING_STATES:
            runtime += inside
        elif state == "planned_stop":
            planned_stop += inside
        elif state in DOWNTIME_STATES:
            downtime += inside
        # "off" counts as neither - the machine was not scheduled.

    return {
        "planned_time_s": planned_time,
        "runtime_s": runtime,
        "downtime_s": downtime,
        "planned_stop_s": planned_stop,
        "by_state": by_state,
    }


def downtime_pareto(events: Iterable[Dict[str, Any]],
                    window_start: float,
                    window_end: float,
                    top_n: int = 10) -> List[Dict[str, Any]]:
    """Downtime seconds grouped by reason, biggest first.

    An unconfirmed stop is grouped as "Unknown" rather than dropped - the
    reason a Pareto exists is to make the size of "Unknown" impossible to
    ignore.
    """
    totals: Dict[Tuple[str, str], float] = {}
    for ev in events or []:
        state = str(ev.get("state") or "unknown")
        if state not in DOWNTIME_STATES:
            continue
        s = float(ev.get("start") or 0.0)
        e = ev.get("end")
        e = float(e) if e is not None else float(window_end)
        secs = _overlap_s(s, e, window_start, window_end)
        if secs <= 0:
            continue
        category = str(ev.get("downtime_category") or "") or "Unknown"
        reason = str(ev.get("downtime_reason") or "") or "Unknown"
        key = (category, reason)
        totals[key] = totals.get(key, 0.0) + secs

    rows = [{"category": c, "reason": r, "seconds": secs}
            for (c, r), secs in totals.items()]
    rows.sort(key=lambda x: x["seconds"], reverse=True)
    rows = rows[: max(1, int(top_n))]
    grand = sum(x["seconds"] for x in rows) or 1.0
    running = 0.0
    for row in rows:
        running += row["seconds"]
        row["share"] = row["seconds"] / grand
        row["cumulative"] = running / grand
    return rows


# ---------------------------------------------------------------------------
# Energy and waste
# ---------------------------------------------------------------------------
def integrate_energy(samples: List[Tuple[float, float]],
                     window_start: float,
                     window_end: float) -> float:
    """kWh from (epoch_seconds, kW) samples, by trapezoid.

    A power meter reports kW at intervals; energy is the area under that. The
    trapezoid rule is used rather than sample x interval because a 1 s meter
    and a 60 s meter must give the same answer for the same physical machine.
    """
    if not samples:
        return 0.0
    pts = sorted((float(t), float(v)) for t, v in samples if t is not None and v is not None)
    total_kwh = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        lo, hi = max(t0, window_start), min(t1, window_end)
        if hi <= lo:
            continue
        # Average power across the pair, applied to the clipped span.
        avg_kw = (v0 + v1) / 2.0
        total_kwh += avg_kw * ((hi - lo) / 3600.0)
    return max(0.0, total_kwh)


def energy_by_state(samples: List[Tuple[float, float]],
                    events: Iterable[Dict[str, Any]],
                    window_start: float,
                    window_end: float) -> Dict[str, float]:
    """Split energy across the machine's states.

    Answers "how much did we spend while it was stopped?", which is the
    question that turns a power meter into a cost argument.
    """
    out: Dict[str, float] = {}
    for ev in events or []:
        state = str(ev.get("state") or "unknown")
        s = max(float(ev.get("start") or 0.0), window_start)
        e = ev.get("end")
        e = min(float(e) if e is not None else window_end, window_end)
        if e <= s:
            continue
        out[state] = out.get(state, 0.0) + integrate_energy(samples, s, e)
    return out


def estimate_wasted_energy(by_state: Dict[str, float],
                           state_seconds: Dict[str, float],
                           standby_power_kw: Optional[float],
                           idle_power_kw: Optional[float],
                           blocked_energy_kwh: float = 0.0) -> Dict[str, Any]:
    """Energy the machine should not have used.

    Waste is the energy consumed ABOVE the configured standby/idle allowance
    while the machine was not producing - not the whole figure. A machine that
    legitimately keeps a controller and a heater alive while stopped is not
    wasting all of it, and calling it waste would make the number useless.

    Returns the breakdown as well as the total, so a KPI card can be explained.
    """
    detail: Dict[str, float] = {}
    total = 0.0

    def _excess(state: str, allowance_kw: Optional[float]) -> float:
        used = float(by_state.get(state, 0.0))
        secs = float(state_seconds.get(state, 0.0))
        if used <= 0 or secs <= 0:
            return 0.0
        if allowance_kw is None:
            # No allowance configured: every kWh in this state is avoidable.
            return used
        allowed = float(allowance_kw) * (secs / 3600.0)
        return max(0.0, used - allowed)

    for state, allowance in (("stopped", standby_power_kw),
                             ("off", standby_power_kw),
                             ("faulted", standby_power_kw),
                             ("planned_stop", standby_power_kw),
                             ("idle", idle_power_kw),
                             ("waiting_material", idle_power_kw),
                             ("waiting_operator", idle_power_kw),
                             ("changeover", idle_power_kw)):
        e = _excess(state, allowance)
        if e > 0:
            detail[state] = detail.get(state, 0.0) + e
            total += e

    if blocked_energy_kwh > 0:
        detail["running_no_output"] = blocked_energy_kwh
        total += blocked_energy_kwh

    return {"total_kwh": total, "by_state": detail}
