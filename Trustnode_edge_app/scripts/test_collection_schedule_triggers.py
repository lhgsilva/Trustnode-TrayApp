# -*- coding: utf-8 -*-
"""Schedule triggers, and rules that apply to one gateway or to all of them.

2026-09-02, requested: "a trigger can be based on a tag or schedule on a
time/day and interval - hourly, daily, monthly, continuous or one time", and
"we should have an option to select either one gateway in the list of options
or all gateways".

A schedule is another KIND of collection trigger rather than a second
scheduler, so it goes through the same gate as a tag rule and combines with
tag rules under the same ANY/ALL mode. Two systems deciding when a gateway
collects would eventually disagree, and the operator would have no way to tell
which one paused their data.

Building it surfaced a related fault: the gate computed ONE verdict for the
whole site, so a rule written for one machine paused every gateway - meters
included. Scope is now honoured.

Everything here is evaluated at a CHOSEN instant. A schedule test that waits
for the clock is a test that runs once a day.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.collection_schedule import (          # noqa: E402
    INTERVALS, applies_to_gateway, schedule_allows)

MON = dt.datetime(2026, 9, 7, 9, 30)      # a Monday, 09:30
SAT = dt.datetime(2026, 9, 12, 9, 30)     # a Saturday, 09:30

print("TrustNode - schedule triggers")
print()
print("[a daily window]")
daily = {"schedule_interval": "daily", "schedule_start": "08:00",
         "schedule_stop": "17:00"}
check("inside the window collects", schedule_allows(daily, MON)[0])
check("  before it does not",
      not schedule_allows(daily, MON.replace(hour=7))[0])
check("  after it does not",
      not schedule_allows(daily, MON.replace(hour=18))[0])
check("  and the reason names the window",
      "08:00-17:00" in schedule_allows(daily, MON.replace(hour=18))[1],
      schedule_allows(daily, MON.replace(hour=18))[1])

print()
print("[a night shift crosses midnight]")
night = {"schedule_interval": "daily", "schedule_start": "22:00",
         "schedule_stop": "06:00"}
check("23:00 is inside", schedule_allows(night, MON.replace(hour=23))[0])
check("02:00 is inside", schedule_allows(night, MON.replace(hour=2))[0])
check("12:00 is outside",
      not schedule_allows(night, MON.replace(hour=12))[0],
      "a stop earlier than start is a night shift, not a mistake")

print()
print("[selected weekdays]")
weekdays = dict(daily, schedule_days=[1, 2, 3, 4, 5])
check("Monday collects", schedule_allows(weekdays, MON)[0])
check("  Saturday does not", not schedule_allows(weekdays, SAT)[0],
      schedule_allows(weekdays, SAT)[1])

print()
print("[hourly]")
hourly = {"schedule_interval": "hourly", "schedule_start": "00:10",
          "schedule_stop": "00:20"}
check("minute 15 of any hour collects",
      schedule_allows(hourly, MON.replace(minute=15))[0])
check("  minute 5 does not", not schedule_allows(hourly, MON.replace(minute=5))[0])
check("  and it is the same in every hour",
      schedule_allows(hourly, MON.replace(hour=3, minute=15))[0]
      and schedule_allows(hourly, MON.replace(hour=21, minute=15))[0])

print()
print("[monthly]")
monthly = {"schedule_interval": "monthly", "schedule_day_of_month": 7,
           "schedule_start": "08:00", "schedule_stop": "17:00"}
check("on the 7th it collects", schedule_allows(monthly, MON)[0])
check("  on the 8th it does not",
      not schedule_allows(monthly, MON.replace(day=8))[0])
feb = {"schedule_interval": "monthly", "schedule_day_of_month": 31,
       "schedule_start": "00:00", "schedule_stop": "23:59"}
check("a rule set for the 31st still runs in February",
      schedule_allows(feb, dt.datetime(2026, 2, 28, 10, 0))[0],
      "clamped to the last day - skipping the month entirely would lose a "
      "month of data without saying so")

print()
print("[one time]")
once = {"schedule_interval": "one_time", "schedule_date": "2026-09-07",
        "schedule_start": "08:00", "schedule_stop": "17:00"}
check("on the day it collects", schedule_allows(once, MON)[0])
check("  the day after it does not",
      not schedule_allows(once, MON.replace(day=8))[0])
check("  with no date it collects NOTHING",
      not schedule_allows({"schedule_interval": "one_time"}, MON)[0],
      "an unset date must not mean 'always'")

print()
print("[continuous]")
check("continuous always collects",
      schedule_allows({"schedule_interval": "continuous"}, MON)[0]
      and schedule_allows({"schedule_interval": "continuous"}, SAT)[0])

print()
print("[a rule that cannot be understood pauses, it does not guess]")
check("an unknown interval blocks",
      not schedule_allows({"schedule_interval": "fortnightly"}, MON)[0],
      "silently meaning 'always' would write data nobody asked for")
check("a zero-length window blocks",
      not schedule_allows({"schedule_interval": "daily",
                           "schedule_start": "08:00",
                           "schedule_stop": "08:00"}, MON)[0])
check("every documented interval is implemented",
      set(INTERVALS) == {"continuous", "hourly", "daily", "monthly", "one_time"},
      INTERVALS)

print()
print("[one gateway, or all of them]")
check("a rule scoped to a gateway applies to it",
      applies_to_gateway({"gateway_id": "gw-1"}, "gw-1"))
check("  and NOT to another",
      not applies_to_gateway({"gateway_id": "gw-1"}, "gw-2"),
      "the gate used to be one verdict for the whole site, so a rule for one "
      "machine paused every gateway including the meters")
check('"*" applies to every gateway',
      applies_to_gateway({"gateway_id": "*"}, "gw-1")
      and applies_to_gateway({"gateway_id": "*"}, "meter-7"))
check("  an empty scope also means all",
      applies_to_gateway({"gateway_id": ""}, "anything"),
      "configs written before scoping existed must keep working")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
