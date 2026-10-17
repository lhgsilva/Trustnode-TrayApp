# -*- coding: utf-8 -*-
"""Gate entry for the historian export's local -> UTC window conversion.

2026-10-17, reported: "the historian export is not working, not loading,
preview, export". It ran, and correctly returned nothing: the datetime-local
inputs hand back LOCAL wall time and that string went into a field named
`from_utc`, so on this UTC+1 machine every query was an hour in the future.
Measured on the live store: the typed window 17:08:40-17:12:47 returned 0
rows; the same window shifted -1h returned 27,234.

The assertions live in frontend/test_export_window_utc.mjs, because the
conversion is JavaScript and the only honest test is to run it. This wrapper
exists so the release gate - which runs Python - includes it, and runs it
twice: in the machine's own zone, where it must shift, and under TZ=UTC,
where it must not.

Touches no store and no network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = os.path.join(ROOT, "frontend", "test_export_window_utc.mjs")

print("TrustNode - the export window reaches the API in UTC")
print()

node = shutil.which("node")
if not node:
    # A build machine always has node (the frontend is built with it); a bare
    # runtime might not. Say so rather than passing quietly.
    print("  node is not on PATH - cannot run the conversion. SKIP")
    sys.exit(0)
if not os.path.isfile(TEST):
    print("  missing %s" % TEST)
    sys.exit(2)

fails = []
for label, tz in (("the machine's own zone", None), ("TZ=UTC", "UTC")):
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    else:
        env.pop("TZ", None)
    proc = subprocess.run([node, TEST], env=env, cwd=ROOT,
                          capture_output=True, text=True)
    print("  [%s]" % label)
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            print("  " + line)
    if proc.returncode != 0:
        fails.append(label)
        for line in (proc.stderr or "").splitlines()[-6:]:
            print("    ! " + line)
    print()

print("RESULT: %s" % ("PASS" if not fails else "FAIL - " + ", ".join(fails)))
sys.exit(0 if not fails else 2)
