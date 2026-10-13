# -*- coding: utf-8 -*-
"""A slow start-up must not leave the app empty until it is reloaded.

2026-09-30, reported: "templates are not loading and configuration of the app
when is starting up also not working, need to be reloaded all the time". The
Reporting page said:

    Failed to load templates: signal is aborted without reason

That string is the browser's raw AbortError. fetchWithTimeout aborts every
request at 12s, and at boot the backend is still finishing its deferred init,
so the first wave of reads can exceed that. Nothing retried them and nothing
translated the message, so the page stayed empty and a reload - by which time
the backend was warm - was the only fix anyone found.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


API = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
              encoding="utf-8", errors="replace").read()
fn = API.split("async function fetchWithTimeout", 1)[1].split("\nfunction sleep", 1)[0]

print("TrustNode - a slow start-up retries instead of giving up")
print()
check("the abort records that it was a TIMEOUT",
      "timedOut = true" in fn,
      "otherwise the caller cannot tell a timeout from a cancelled request")
check("a timed-out read is retried",
      "__tnTimeoutRetry" in fn and "fetchWithTimeout(" in fn.split("catch", 1)[1],
      "the first wave of boot reads had no second chance")
check("  only idempotent requests",
      'method !== "GET"' in fn,
      "retrying a POST could duplicate a write")
check("  and only once",
      "options.__tnTimeoutRetry" in fn,
      "an unbounded retry turns a slow backend into a stampede")
check("  with a longer budget the second time",
      re.search(r"timeoutMs \* 2", fn) is not None,
      "retrying with the same budget that just expired rarely helps")
check("the operator gets a message that means something",
      "Timed out after" in fn,
      '"signal is aborted without reason" told nobody anything')

# The retry must not fire while the session is over: 22 pollers each retrying
# a 12s timeout is exactly the stampede the session-expiry guard prevents.
check("the session-expired short circuit still comes first",
      fn.index("_sessionExpired") < fn.index("timedOut"),
      "a logged-out window would retry every poller")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
