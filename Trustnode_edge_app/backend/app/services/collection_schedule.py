# -*- coding: utf-8 -*-
"""When a SCHEDULE trigger says collection may run.

2026-09-02, requested: "a trigger can be based on a tag or schedule on a
time/day and interval - run it hourly, daily, monthly, continuous or one time",
and "selected by gateways and condition, or all gateways, with both options".

A schedule is therefore just another KIND of collection trigger. It answers the
same question a tag trigger answers - may this gateway write right now, yes or
no - so it goes through the same gate and combines with tag triggers under the
same ANY/ALL mode. That is the whole reason not to build a second scheduler:
one place decides whether a row is written.

Deliberately standalone and pure: given a rule and a moment, return true or
false. No clock reading inside, no manager, no config - so every case below can
be tested at a chosen instant instead of by waiting for one.

THE WINDOW

`start` and `stop` are "HH:MM" local times and the window is [start, stop).
A stop EARLIER than start crosses midnight (22:00 -> 06:00), which is an
ordinary night shift and not a mistake.

  continuous  always true. The rule exists, imposes no time limit, and is the
              honest way to say "collect whenever the other conditions allow"
              rather than deleting the rule.
  hourly      the MINUTE window of every hour. start 00:10 stop 00:20 collects
              minutes 10-19 of each hour.
  daily       the time window, on the selected weekdays (all days if none).
  monthly     the time window, on the selected day of the month.
  one_time    the time window, on one specific date, once.

A rule that cannot be understood returns False and says why, because a
schedule that silently means "always" would write data nobody asked for.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional, Tuple

INTERVALS = ("continuous", "hourly", "daily", "monthly", "one_time")


def _parse_hhmm(text: Any, fallback: Tuple[int, int]) -> Tuple[int, int]:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    parts = raw.split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return fallback
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return fallback
    return hh, mm


def _in_window(now_min: int, start_min: int, stop_min: int, span: int) -> bool:
    """Is now inside [start, stop) on a cycle of `span` minutes?"""
    if start_min == stop_min:
        # A zero-length window collects nothing. Saying so beats treating it
        # as "always", which is the opposite of what was configured.
        return False
    if start_min < stop_min:
        return start_min <= now_min < stop_min
    # Wraps the end of the cycle - a night shift, or minute 50 to minute 10.
    return now_min >= start_min or now_min < stop_min % span


def schedule_allows(rule: Dict[str, Any], now: Optional[_dt.datetime] = None
                    ) -> Tuple[bool, str]:
    """May collection run at `now` under this schedule rule?

    Returns (allowed, reason). The reason is for the operator, so it names the
    window rather than restating the rule id.
    """
    moment = now or _dt.datetime.now()
    interval = str(rule.get("schedule_interval") or "daily").strip().lower()
    if interval not in INTERVALS:
        return False, ("Unknown schedule interval %r - collection is paused "
                       "rather than guessed." % interval)

    if interval == "continuous":
        return True, "Continuous - no time limit."

    sh, sm = _parse_hhmm(rule.get("schedule_start"), (0, 0))
    eh, em = _parse_hhmm(rule.get("schedule_stop"), (23, 59))

    if interval == "hourly":
        now_min = moment.minute
        allowed = _in_window(now_min, sm, em, 60)
        return allowed, ("Hourly window %02d-%02d min%s" %
                         (sm, em, "" if allowed else " - outside it now"))

    # The remaining intervals are a time-of-day window, possibly restricted to
    # certain days.
    now_min = moment.hour * 60 + moment.minute
    start_min = sh * 60 + sm
    stop_min = eh * 60 + em
    within_time = _in_window(now_min, start_min, stop_min, 24 * 60)
    window = "%02d:%02d-%02d:%02d" % (sh, sm, eh, em)

    if interval == "daily":
        days = rule.get("schedule_days") or []
        if days:
            try:
                wanted = {int(d) for d in days}
            except (TypeError, ValueError):
                return False, "Schedule days are not numbers - collection paused."
            # 1 = Monday .. 7 = Sunday, as the shift editor already uses.
            if (moment.weekday() + 1) not in wanted:
                return False, "Not a scheduled day (%s)." % window
        return within_time, ("Daily %s%s" % (window,
                                             "" if within_time else " - outside it now"))

    if interval == "monthly":
        try:
            day_of_month = int(rule.get("schedule_day_of_month") or 1)
        except (TypeError, ValueError):
            day_of_month = 1
        # A rule set for the 31st must still run in February. Clamp to the
        # last day of THIS month rather than skipping the month entirely.
        last_day = _last_day_of_month(moment.year, moment.month)
        effective = min(max(1, day_of_month), last_day)
        if moment.day != effective:
            return False, ("Monthly on day %d (%s)." % (effective, window))
        return within_time, ("Monthly day %d %s%s"
                             % (effective, window,
                                "" if within_time else " - outside it now"))

    # one_time
    date_raw = str(rule.get("schedule_date") or "").strip()
    if not date_raw:
        return False, "One-time schedule has no date set - collection paused."
    try:
        on = _dt.date.fromisoformat(date_raw[:10])
    except ValueError:
        return False, ("One-time schedule date %r is not YYYY-MM-DD." % date_raw)
    if moment.date() != on:
        return False, "One-time schedule set for %s (%s)." % (on.isoformat(), window)
    return within_time, ("One time on %s %s%s"
                         % (on.isoformat(), window,
                            "" if within_time else " - outside it now"))


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    return (nxt - _dt.timedelta(days=1)).day


def applies_to_gateway(rule: Dict[str, Any], gateway_id: str) -> bool:
    """Is this rule about that gateway?

    An empty scope, "*" or "all" means EVERY gateway - which is what the
    operator picks when the rule is about the plant rather than one machine.
    Anything else must match the gateway exactly.
    """
    scope = str(rule.get("gateway_id") or "").strip()
    if not scope or scope in ("*", "all", "ALL"):
        return True
    return scope == str(gateway_id or "").strip()
