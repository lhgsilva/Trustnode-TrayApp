"""RELEASE GATE — the standard 10-minute validation for committing a new version.

Run this against the RUNNING freshly-built app before committing/tagging a
release. It executes the full-system suite (scripts/validate_full_12h.py) for
10 minutes and exits 0 only on "OVERALL: PASS".

Release flow:
    1. build both installers      cd desktop && npm run dist
    2. install/launch the build   (quit old app first; portable re-extracts)
    3. gate                       python scripts/validate_release.py
    4. on PASS (exit 0)           commit / tag / ship
       on FAIL (exit 2)           read scripts/validation_out/validation_report.txt

Covers: BOOT HEALTH of the last launch (spawn -> /api/health 200 <= 15 s, no
splash "did not respond", backend boot instrumentation present — 2026-08-21),
collection cadence + loss, chart-feed latency, historian freshness,
local DB truth, outbox depth, cloud PG lag, API + AI-module probes,
batch/report/alarm/trigger snapshots, resource trends, and the log census
(stalls, restarts, lock-watchdog starvation dumps).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ)
env.setdefault("VAL_DURATION_S", "600")
env.setdefault("PYTHONIOENCODING", "utf-8")

print("RELEASE GATE: running the 10-minute full-system validation...", flush=True)
rc = subprocess.call([sys.executable, os.path.join(HERE, "validate_full_12h.py")], env=env)
print(f"\nRELEASE GATE: {'PASS - safe to commit this version' if rc == 0 else 'FAIL - do NOT commit; see scripts/validation_out/validation_report.txt'}",
      flush=True)
sys.exit(rc)
