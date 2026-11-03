# -*- coding: utf-8 -*-
"""The Reports pages open with content and no page can starve another.

2026-11-03. Runs the two pure frontend modules under node and checks the
page and the API layer on the source:
  * requestGate.js: at most 4 reads in flight while visible, 1 while hidden,
    FIFO, and a slot is released exactly once;
  * reportsCache.js: a round trip through localStorage, and the "as of" label;
  * the Reports page mounts from the cache, reads one overview request,
    refreshes on the stream's change event and once a minute otherwise;
  * fetchWithTimeout sends GETs through the gate and writes around it.

    python scripts/test_reports_ui_never_waits.py
"""
from __future__ import annotations

import os
import re
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def node(script: str) -> str:
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=60, cwd=SRC)
    return (res.stdout.strip() + ("\nERR " + res.stderr.strip() if res.returncode else "")).strip()


def main() -> int:
    print("the Reports pages open with content and no page can starve another")
    gate = os.path.join(SRC, "requestGate.js").replace("\\", "/")
    out = node(f"""
import * as g from "file:///{gate}";
const log = [];
const rel = [];
for (let i = 0; i < 6; i++) g.acquire().then((r) => {{ log.push("in" + i); rel.push(r); }});
await new Promise((r) => setTimeout(r, 10));
log.push("active=" + g.gateStats().active + " waiting=" + g.gateStats().waiting);
rel[0](); rel[0]();            // releasing twice must count once
await new Promise((r) => setTimeout(r, 10));
log.push("active=" + g.gateStats().active + " waiting=" + g.gateStats().waiting);
g.setHidden(true);
rel[1](); rel[2](); rel[3](); rel[4]();
await new Promise((r) => setTimeout(r, 10));
log.push("hidden active=" + g.gateStats().active + " waiting=" + g.gateStats().waiting);
g.setHidden(false);
await new Promise((r) => setTimeout(r, 10));
log.push("visible active=" + g.gateStats().active + " waiting=" + g.gateStats().waiting);
console.log(log.join("|"));
""")
    check("the gate admits 4 reads and queues the rest, FIFO",
          out.startswith("in0|in1|in2|in3|active=4 waiting=2|"), out[:200])
    check("  a double release counts once", "|in4|active=4 waiting=1|" in out, out[:200])
    check("  hidden: at most 1 in flight; visible again: back to 4",
          "hidden active=1 waiting=" in out and out.endswith("visible active=1 waiting=0"), out[:220])

    cache = os.path.join(SRC, "reportsCache.js").replace("\\", "/")
    out = node(f"""
const store = new Map();
globalThis.localStorage = {{ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }};
const c = await import("file:///{cache}");
const empty = c.readReportsCache();
const a = [empty.templates.length, empty.updatedAt, c.cacheAgeLabel(empty)];
c.writeReportsCache({{ templates: [{{ id: "t" }}], schedules: [], generated: [{{ id: "g" }}], anyGatewayRunning: false, updatedAt: 1000000 }});
const raw = JSON.parse(store.get("tn.reports.cache.v1"));
const b = [raw.templates.length, raw.generated.length, raw.anyGatewayRunning, c.cacheAgeLabel({{ updatedAt: 1000000 }}, 1000000 + 30000)];
console.log(JSON.stringify([a, b]));
""")
    check("the cache starts empty with no age label", out.startswith('[[0,0,""],'), out[:120])
    check("  and a write round-trips through localStorage with an age label",
          out.endswith('[1,1,false,"30 s ago"]]'), out[:160])

    page = open(os.path.join(SRC, "components", "Reports", "ScheduledReportsManager.jsx"), encoding="utf-8").read()
    check("the page mounts with the cached lists",
          "useState(() => cacheAtMountRef.current.templates)" in page
          and "useState(() => cacheAtMountRef.current.generated)" in page)
    check("  reads one overview request", "await getReportsOverview({ limit: 200 })" in page)
    check("  stores a good read back into the cache", page.count("writeReportsCache(") >= 2)
    check("  refreshes on the stream's change event with a 1 s debounce",
          "[changeTick]" in page and "setTimeout(() => { changeTimerRef.current = null; refresh(); }, 1000)" in page)
    check("  and otherwise once a minute, not every 15 s",
          "setInterval(refresh, 60000)" in page and "setInterval(refresh, 15000)" not in page)
    check("  a failed refresh keeps the lists and says how old they are",
          "Showing the lists as read" in page and "Reconnecting to the reports service" not in page)

    app = open(os.path.join(SRC, "App.jsx"), encoding="utf-8").read()
    check("App turns reports_changed into changeTick for both Reports pages",
          'data.type === "reports_changed"' in app and app.count("changeTick={reportsChangeTick}") == 2)

    api = open(os.path.join(SRC, "api.js"), encoding="utf-8").read()
    m = re.search(r"async function fetchWithTimeout\(url, options = \{\}, timeoutMs = 12000\) \{(.*?)\n\}\n", api, re.S)
    body = m.group(1) if m else ""
    check("fetchWithTimeout sends GETs through the gate and writes around it",
          '_method === "GET"' in body and "_gate.acquire()" in body and "_fetchWithTimeoutNow(" in body)
    check("  the gate follows the window's visibility", "_gate.setHidden(document.visibilityState" in api)
    check("  the overview call exists", "export async function getReportsOverview" in api)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
