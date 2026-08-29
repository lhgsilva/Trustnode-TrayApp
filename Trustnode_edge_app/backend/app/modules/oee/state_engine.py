# -*- coding: utf-8 -*-
"""Deciding what a machine is doing right now.

Three ways in, one answer out:

  * signal rules  - PLC/sensor tags with a condition (the primary source)
  * power rules   - a band of kW/A held for a minimum time (the fallback)
  * manual        - what the operator last told us

`resolve_state()` combines them and reports a CONFIDENCE, because a status
derived from two sources that agree is worth more than one guessed from a
current reading, and an operator deserves to know which they are looking at.

Pure functions over plain values: no DB, no HTTP, so every branch is testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- states ---
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_STOPPED = "stopped"
STATE_FAULTED = "faulted"
STATE_CHANGEOVER = "changeover"
STATE_WAITING_MATERIAL = "waiting_material"
STATE_WAITING_OPERATOR = "waiting_operator"
STATE_PLANNED_STOP = "planned_stop"
STATE_OFF = "off"
STATE_UNKNOWN = "unknown"

MACHINE_STATES = (
    STATE_RUNNING, STATE_IDLE, STATE_STOPPED, STATE_FAULTED, STATE_CHANGEOVER,
    STATE_WAITING_MATERIAL, STATE_WAITING_OPERATOR, STATE_PLANNED_STOP,
    STATE_OFF, STATE_UNKNOWN,
)

# A power rule may name "production", which is a running state by another name.
POWER_STATE_ALIASES = {
    "production": STATE_RUNNING,
    "high_consumption": STATE_RUNNING,
    "energy_waste": STATE_IDLE,
}

# ----------------------------------------------------------- confidence ---
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"
CONF_CONFLICT = "conflict"
CONF_MISSING = "missing"

# ------------------------------------------------------------- functions ---
FN_RUNNING = "running_status"
FN_STOPPED = "stopped_status"
FN_IDLE = "idle_status"
FN_FAULT = "fault_status"
FN_ALARM_CODE = "alarm_code"
FN_CYCLE_START = "cycle_start"
FN_CYCLE_STOP = "cycle_stop"
FN_CYCLE_COMPLETE = "cycle_complete"
FN_TOTAL_COUNT = "total_count"
FN_GOOD_COUNT = "good_count"
FN_REJECT_COUNT = "reject_count"
FN_SCRAP_COUNT = "scrap_count"
FN_SPEED = "current_speed"
FN_PRODUCT_CODE = "product_code"
FN_ORDER_NUMBER = "order_number"

OEE_FUNCTIONS = (
    FN_RUNNING, FN_STOPPED, FN_IDLE, FN_FAULT, FN_ALARM_CODE,
    FN_CYCLE_START, FN_CYCLE_STOP, FN_CYCLE_COMPLETE,
    FN_TOTAL_COUNT, FN_GOOD_COUNT, FN_REJECT_COUNT, FN_SCRAP_COUNT,
    FN_SPEED, FN_PRODUCT_CODE, FN_ORDER_NUMBER,
)

# Which state a status function asserts when its condition is true. Fault
# outranks the rest, which is why it carries the lowest sort key.
FUNCTION_STATE = {
    FN_FAULT: (STATE_FAULTED, 10),
    FN_RUNNING: (STATE_RUNNING, 30),
    FN_IDLE: (STATE_IDLE, 20),
    FN_STOPPED: (STATE_STOPPED, 40),
}

CONDITION_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "rising", "falling",
                 "changed", "truthy", "falsy", "stale")


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        text = str(value).strip().lower()
        if text in ("true", "on", "yes", "running", "1"):
            return 1.0
        if text in ("false", "off", "no", "stopped", "0"):
            return 0.0
        return None


def evaluate_condition(op: str,
                       current: Any,
                       expected: Any = None,
                       previous: Any = None,
                       seconds_since_change: Optional[float] = None,
                       hold_seconds: float = 0.0) -> bool:
    """Is this mapping's condition satisfied right now?

    `rising`/`falling`/`changed` need the previous value; `stale` needs how long
    the value has been unchanged, which is how "TotalCount does not increase for
    5 minutes" becomes an idle rule.
    """
    op = str(op or "truthy").strip().lower()
    cur = _as_float(current)
    exp = _as_float(expected)
    prev = _as_float(previous)

    if op == "truthy":
        return bool(cur)
    if op == "falsy":
        return not bool(cur)
    if op == "stale":
        if seconds_since_change is None:
            return False
        return float(seconds_since_change) >= float(hold_seconds or 0.0)
    if op in ("rising", "falling", "changed"):
        if cur is None or prev is None:
            return False
        if op == "rising":
            return cur > prev
        if op == "falling":
            return cur < prev
        return cur != prev
    if cur is None:
        return False
    if op == "eq":
        # String compare when either side is not numeric (product codes).
        if exp is None and expected is not None:
            return str(current).strip() == str(expected).strip()
        return exp is not None and cur == exp
    if op == "ne":
        if exp is None and expected is not None:
            return str(current).strip() != str(expected).strip()
        return exp is not None and cur != exp
    if exp is None:
        return False
    if op == "gt":
        return cur > exp
    if op == "gte":
        return cur >= exp
    if op == "lt":
        return cur < exp
    if op == "lte":
        return cur <= exp
    return False


@dataclass
class SignalReading:
    """One tag's current value plus the history the conditions need."""
    value: Any = None
    previous: Any = None
    seconds_since_change: Optional[float] = None
    stale: bool = False          # no fresh sample at all


@dataclass
class StateVerdict:
    state: str = STATE_UNKNOWN
    source: str = "signal"       # signal|power|manual|combined
    confidence: str = CONF_MISSING
    detail: str = ""
    flags: List[str] = field(default_factory=list)   # energy_waste, blocked, conflict

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "confidence": self.confidence,
            "detail": self.detail,
            "flags": list(self.flags),
        }


def evaluate_signal_state(mappings: List[Dict[str, Any]],
                          readings: Dict[str, SignalReading]) -> Optional[StateVerdict]:
    """The machine's state according to its PLC/sensor mappings.

    Returns None when nothing could be evaluated - which is different from
    "stopped". A gateway that is down must not silently report every machine as
    stopped and destroy the availability figure.
    """
    candidates: List[Tuple[int, int, str, str]] = []
    evaluated = 0

    for m in mappings or []:
        if not m.get("enabled", True):
            continue
        fn = str(m.get("oee_function") or "")
        if fn not in FUNCTION_STATE:
            continue
        key = str(m.get("tag_name") or "")
        r = readings.get(key)
        if r is None or r.stale:
            continue
        evaluated += 1
        ok = evaluate_condition(
            m.get("condition_op") or "truthy",
            r.value, m.get("condition_value"),
            previous=r.previous,
            seconds_since_change=r.seconds_since_change,
            hold_seconds=float(m.get("hold_seconds") or 0.0),
        )
        if not ok:
            continue
        state, rank = FUNCTION_STATE[fn]
        prio = int(m.get("priority") or 100)
        candidates.append((prio, rank, state, f"{fn} on {key}"))

    if not evaluated:
        return None
    if not candidates:
        # Tags read fine and nothing asserted a state. "Stopped" is the honest
        # reading of "the running condition is not met".
        return StateVerdict(state=STATE_STOPPED, source="signal",
                            confidence=CONF_MEDIUM,
                            detail="no running condition matched")
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, state, detail = candidates[0]
    return StateVerdict(state=state, source="signal", confidence=CONF_MEDIUM,
                        detail=detail)


def evaluate_power_state(rules: List[Dict[str, Any]],
                         power_kw: Optional[float],
                         current_a: Optional[float] = None,
                         seconds_in_band: Optional[Dict[str, float]] = None,
                         counts_increasing: Optional[bool] = None
                         ) -> Optional[StateVerdict]:
    """The machine's state inferred from its electrical draw.

    This is what makes an old machine with no PLC measurable at all. Each rule
    is a band (min..max) that must hold for `min_duration_s`; the caller passes
    how long each rule has currently been satisfied in `seconds_in_band`.
    """
    if power_kw is None and current_a is None:
        return None
    held = seconds_in_band or {}
    matches: List[Tuple[int, str, str]] = []

    for rule in rules or []:
        if not rule.get("enabled", True):
            continue
        measurement = str(rule.get("measurement") or "power_kw")
        value = power_kw if measurement == "power_kw" else current_a
        if value is None:
            continue
        lo = rule.get("min_value")
        hi = rule.get("max_value")
        if lo is not None and float(value) < float(lo):
            continue
        if hi is not None and float(value) > float(hi):
            continue
        need = float(rule.get("min_duration_s") or 0.0)
        if need > 0 and float(held.get(str(rule.get("id") or ""), 0.0)) < need:
            continue
        if rule.get("requires_no_count") and counts_increasing is not False:
            # "power high while production is not increasing" - only fires when
            # we positively know the count is NOT moving.
            continue
        raw = str(rule.get("generated_status") or STATE_UNKNOWN)
        state = POWER_STATE_ALIASES.get(raw, raw)
        matches.append((int(rule.get("priority") or 100), state,
                        f"power rule '{rule.get('name') or raw}'"))

    if not matches:
        return None
    matches.sort(key=lambda m: m[0])
    _, state, detail = matches[0]
    flags = ["energy_waste"] if any(
        str(r.get("generated_status") or "") == "energy_waste"
        for r in (rules or []) if r.get("enabled", True)
        and str(r.get("name") or "") in detail) else []
    return StateVerdict(state=state, source="power", confidence=CONF_MEDIUM,
                        detail=detail, flags=flags)


def resolve_state(machine: Dict[str, Any],
                  signal: Optional[StateVerdict],
                  power: Optional[StateVerdict],
                  manual: Optional[StateVerdict] = None,
                  power_kw: Optional[float] = None,
                  counts_increasing: Optional[bool] = None,
                  in_planned_stop: bool = False) -> StateVerdict:
    """One state, one confidence, from whatever sources the machine has.

    The combination rules are the ones in the spec:
      * signal is primary, power validates it;
      * power is the fallback when the signal is missing;
      * signal says stopped + power high        -> energy_waste flag;
      * signal says running + count not moving  -> blocked flag (idle);
      * power says producing + signal says stopped -> conflict;
      * both agree -> high confidence.
    """
    mode = str(machine.get("default_status_source") or "signal")
    standby = machine.get("standby_power_kw")

    # A configured planned stop outranks everything: the machine is not
    # supposed to be producing, and counting it as downtime would punish the
    # site for taking its own scheduled break.
    if in_planned_stop:
        v = StateVerdict(state=STATE_PLANNED_STOP, source="combined",
                         confidence=CONF_HIGH, detail="inside a planned stop")
        if power_kw is not None and standby is not None and float(power_kw) > float(standby):
            v.flags.append("energy_waste")
        return v

    if mode == "manual":
        return manual or StateVerdict(state=STATE_UNKNOWN, source="manual",
                                      confidence=CONF_MISSING,
                                      detail="no operator entry yet")
    if mode == "signal":
        v = signal or StateVerdict(state=STATE_UNKNOWN, source="signal",
                                   confidence=CONF_MISSING,
                                   detail="no readable signal tags")
        return _apply_flags(v, power_kw, standby, counts_increasing)
    if mode == "power":
        v = power or StateVerdict(state=STATE_UNKNOWN, source="power",
                                  confidence=CONF_MISSING,
                                  detail="no power reading")
        return _apply_flags(v, power_kw, standby, counts_increasing)

    # ---- combined --------------------------------------------------------
    if signal is None and power is None:
        v = manual or StateVerdict(state=STATE_UNKNOWN, source="combined",
                                   confidence=CONF_MISSING,
                                   detail="no signal, power or manual input")
        return v
    if signal is None:
        v = StateVerdict(state=power.state, source="power", confidence=CONF_LOW,
                         detail=f"{power.detail} (no signal data - power fallback)",
                         flags=list(power.flags))
        return _apply_flags(v, power_kw, standby, counts_increasing)
    if power is None:
        v = StateVerdict(state=signal.state, source="signal", confidence=CONF_LOW,
                         detail=f"{signal.detail} (no power data to confirm)")
        return _apply_flags(v, power_kw, standby, counts_increasing)

    sig_running = signal.state in (STATE_RUNNING,)
    pwr_running = power.state in (STATE_RUNNING,)

    if sig_running == pwr_running:
        v = StateVerdict(state=signal.state, source="combined", confidence=CONF_HIGH,
                         detail=f"{signal.detail}; confirmed by {power.detail}",
                         flags=list(power.flags))
        return _apply_flags(v, power_kw, standby, counts_increasing)

    # They disagree. Keep the SIGNAL's answer (it is the primary source) but
    # say plainly that the two do not match, so nobody trusts the number
    # without looking.
    v = StateVerdict(state=signal.state, source="combined", confidence=CONF_CONFLICT,
                     detail=f"signal says {signal.state}, power says {power.state}")
    v.flags.append("conflict")
    return _apply_flags(v, power_kw, standby, counts_increasing)


def _apply_flags(v: StateVerdict,
                 power_kw: Optional[float],
                 standby_kw: Optional[float],
                 counts_increasing: Optional[bool]) -> StateVerdict:
    """Waste and blocked flags, which are independent of the state itself."""
    not_producing = v.state in (STATE_STOPPED, STATE_IDLE, STATE_FAULTED,
                                STATE_OFF, STATE_WAITING_MATERIAL,
                                STATE_WAITING_OPERATOR, STATE_CHANGEOVER)
    if (not_producing and power_kw is not None and standby_kw is not None
            and float(power_kw) > float(standby_kw)):
        if "energy_waste" not in v.flags:
            v.flags.append("energy_waste")
    if v.state == STATE_RUNNING and counts_increasing is False:
        if "blocked" not in v.flags:
            v.flags.append("blocked")
        v.detail = (v.detail + "; running but production is not increasing").strip("; ")
    return v


def extract_counts(mappings: List[Dict[str, Any]],
                   readings: Dict[str, SignalReading]) -> Dict[str, Optional[float]]:
    """Total / good / reject / scrap from whichever tags are mapped to them."""
    out: Dict[str, Optional[float]] = {
        "total_count": None, "good_count": None,
        "reject_count": None, "scrap_count": None, "current_speed": None,
    }
    fn_key = {
        FN_TOTAL_COUNT: "total_count",
        FN_GOOD_COUNT: "good_count",
        FN_REJECT_COUNT: "reject_count",
        FN_SCRAP_COUNT: "scrap_count",
        FN_SPEED: "current_speed",
    }
    for m in mappings or []:
        if not m.get("enabled", True):
            continue
        key = fn_key.get(str(m.get("oee_function") or ""))
        if not key:
            continue
        r = readings.get(str(m.get("tag_name") or ""))
        if r is None or r.stale:
            continue
        val = _as_float(r.value)
        if val is not None:
            out[key] = val
    return out


def extract_text(mappings: List[Dict[str, Any]],
                 readings: Dict[str, SignalReading]) -> Dict[str, Optional[str]]:
    """Product code / order number, when the PLC publishes them."""
    out: Dict[str, Optional[str]] = {"product_code": None, "order_number": None}
    fn_key = {FN_PRODUCT_CODE: "product_code", FN_ORDER_NUMBER: "order_number"}
    for m in mappings or []:
        if not m.get("enabled", True):
            continue
        key = fn_key.get(str(m.get("oee_function") or ""))
        if not key:
            continue
        r = readings.get(str(m.get("tag_name") or ""))
        if r is None or r.stale or r.value is None:
            continue
        out[key] = str(r.value)
    return out
