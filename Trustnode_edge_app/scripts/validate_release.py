"""RELEASE GATE — the standard 10-minute validation for committing a new version.

Run this against the RUNNING freshly-built app before committing/tagging a
release. It executes the full-system suite (scripts/validate_full_12h.py) for
10 minutes and exits 0 only on "OVERALL: PASS".

Release flow:
    1. build both installers      cd desktop && npm run dist
                                  (also copies them to every folder listed in
                                   scripts/build_output_paths.txt - edit that
                                   text file to add or move a destination)
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

Before that it runs the source checks, which need no hardware and no running
app, and stops on the first failure rather than spending ten minutes to learn
something a second of grep would have caught. They exist because each guards a
fault that shipped: a hook read before its declaration, a commit call made on
the wrong object, an ifm channel typed wrong, a config document that silently
changed shape, a Start payload that dropped the fields the protocol needs,
an unticked tag that got collected anyway, and a PRAGMA the source asked for
but no connection ever received.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ)
env.setdefault("VAL_DURATION_S", "600")
env.setdefault("PYTHONIOENCODING", "utf-8")

SOURCE_CHECKS = [
    ("hook order / TDZ", "test_hook_deps_tdz.py"),
    ("historian commit path", "test_historian_commit_path.py"),
    ("ifm channel typing", "test_ifm_channels_unit.py"),
    ("config document shape", "test_config_fingerprint.py"),
    ("gateway + Start payload", "test_gateway_ui_regressions.py"),
    # 2026-08-28
    ("what gets collected", "test_collect_selection.py"),
    ("sqlite write pragmas", "test_sqlite_write_pragmas.py"),
    ("config read lock-free", "test_config_read_no_write_lock.py"),
    ("config survives DB load", "test_scoped_config_survives_load.py"),
    ("modbus tcp gateway", "test_modbus_tcp_gateway.py"),
    ("device catalogue + CIP params", "test_device_catalogue.py"),
]

print("RELEASE GATE: source checks...", flush=True)
for label, script in SOURCE_CHECKS:
    rc = subprocess.call([sys.executable, os.path.join(HERE, script)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  {0:26s}: {1}".format(label, "PASS" if rc == 0 else "FAIL"), flush=True)
    if rc != 0:
        print("RELEASE GATE: FAIL - do NOT commit. Run "
              "`python scripts/{0}` for the detail.".format(script), flush=True)
        sys.exit(2)

print("RELEASE GATE: running the 10-minute full-system validation...", flush=True)
rc = subprocess.call([sys.executable, os.path.join(HERE, "validate_full_12h.py")], env=env)
print(f"\nRELEASE GATE: {'PASS - safe to commit this version' if rc == 0 else 'FAIL - do NOT commit; see scripts/validation_out/validation_report.txt'}",
      flush=True)
sys.exit(rc)
