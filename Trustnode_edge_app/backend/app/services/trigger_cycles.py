# -*- coding: utf-8 -*-
"""One start / stop state machine for every trigger in the report module.

2026-11-17, from the owner: "it has a problem to identify rising up and down
of the signal of triggers ... start when it is 1 ... an option if the start
went to 0 before the end trigger ... all options covered, including one tag
to start and stop, and a signal staying 1 or only a pulse ... we cannot have
a report fail to generate". The schedule's cycle trigger and the section
triggers each had their own rules, and neither had these:

  * the STOP reacted to its LEVEL: an end bit still 1 from the last part
    closed the next cycle on its first reading;
  * with no stop set, a START pulse closed the cycle half a second later;
  * nothing said what a start dropping before the stop means;
  * a start and stop on ONE pulsed tag (pulse = start, next pulse = stop) was
    not possible.

Both now walk the same moments through `walk()`:

  start   opens on the start condition turning TRUE (a rising edge). With no
          state known yet (first look), a start already true opens - a test
          already under way is not missed.
  end     "start_false"  the start condition turning false ends it
                         (one signal held while running)
          "stop"         the stop condition turning TRUE ends it - a rising
                         edge after the start: a stop already true when the
                         cycle opens must go false and back to true
          "next_start"   one pulsed signal: the next start pulse ends it
  start_drop (end = "stop" only) - the start goes false before the stop:
          "ignore"   keep running until the stop (default)
          "end"      the cycle ends there
          "discard"  the cycle is dropped; no report is made from it

`walk()` is pure: no store, no clock. It cannot raise on bad input - an
unknown option reads as its default - so a trigger can never be the reason a
report is not generated.
"""
from __future__ import annotations

from typing import Any, Iterable

END_MODES = ("start_false", "stop", "next_start")
DROP_ACTIONS = ("ignore", "end", "discard")


def end_mode_for(raw: Any, has_stop: bool) -> str:
    m = str(raw or "").strip().lower()
    if m in END_MODES:
        if m == "stop" and not has_stop:
            return "start_false"          # nothing to wait for
        return m
    return "stop" if has_stop else "start_false"


def drop_action_for(raw: Any) -> str:
    a = str(raw or "").strip().lower()
    return a if a in DROP_ACTIONS else "ignore"


def new_state() -> dict[str, Any]:
    return {"open": False, "opened_at": "", "prev_start": None, "prev_stop": None,
            "stop_armed": False, "start_low_seen": False}


def walk(moments: Iterable[tuple[str, bool, bool | None]], state: dict[str, Any] | None,
         end_mode: str, start_drop: str = "ignore") -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Feed (timestamp, start_true, stop_true) moments, oldest first.

    Returns (events, state). Events are ("open" | "close" | "discard", ts).
    `stop_true` may be None when there are no stop conditions.
    """
    st = dict(new_state())
    st.update(state or {})
    mode = end_mode if end_mode in END_MODES else "start_false"
    drop = start_drop if start_drop in DROP_ACTIONS else "ignore"
    events: list[tuple[str, str]] = []
    for ts, start, stop in moments:
        start = bool(start)
        stop = bool(stop) if stop is not None else False
        prev_start = st.get("prev_start")
        if not st["open"]:
            rising = start and prev_start is not True
            if rising:
                st.update(open=True, opened_at=ts, stop_armed=not stop, start_low_seen=False)
                events.append(("open", ts))
        else:
            if mode == "start_false":
                if not start:
                    events.append(("close", ts))
                    st.update(open=False, opened_at="")
            elif mode == "next_start":
                if not start:
                    st["start_low_seen"] = True
                elif st.get("start_low_seen"):
                    events.append(("close", ts))
                    st.update(open=False, opened_at="", start_low_seen=False)
            else:  # "stop"
                closed = False
                if not stop:
                    st["stop_armed"] = True
                elif st.get("stop_armed"):
                    events.append(("close", ts))
                    st.update(open=False, opened_at="")
                    closed = True
                if not closed and not start and prev_start is not False:
                    if drop == "end":
                        events.append(("close", ts))
                        st.update(open=False, opened_at="")
                    elif drop == "discard":
                        events.append(("discard", ts))
                        st.update(open=False, opened_at="")
        st["prev_start"] = start
        st["prev_stop"] = stop
    return events, st


def moments_from_timeline(timeline: list[tuple[str, list[Any]]], start_eval, stop_eval) -> list[tuple[str, bool, bool | None]]:
    """(ts, held values) -> (ts, start_true, stop_true); the evaluators take
    the held value list. A failing evaluator reads as False, never raises."""
    out: list[tuple[str, bool, bool | None]] = []
    for ts, values in timeline or []:
        try:
            s = bool(start_eval(values))
        except Exception:
            s = False
        if stop_eval is None:
            p = None
        else:
            try:
                p = bool(stop_eval(values))
            except Exception:
                p = False
        out.append((ts, s, p))
    return out
