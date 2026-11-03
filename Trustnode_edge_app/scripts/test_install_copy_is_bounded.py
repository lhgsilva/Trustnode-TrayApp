# -*- coding: utf-8 -*-
"""The installer never copies a multi-gigabyte store "as insurance".

2026-11-03: on an install that keeps the existing workspace the desktop
shell copied the WHOLE app store - readings included, gigabytes on a
customer edge - synchronously before the splash, and left the copy on the
disk. Above a size cap nothing is copied; the configuration is covered by
the backend's own small config backups.

    python scripts/test_install_copy_is_bounded.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MOD = os.path.join(ROOT, "desktop", "workspace_detector.js").replace("\\", "/")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def run(db_path: str, max_mb: str) -> str:
    env = dict(os.environ, TRUSTNODE_PRE_UPDATE_COPY_MAX_MB=max_mb)
    script = ("const m = require(%r); const out = m.makeSnapshotCopy(%r, 'backup-pre-update'); "
              "console.log(JSON.stringify(out));" % (MOD, db_path.replace("\\", "/")))
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, env=env)
    return (res.stdout.strip().splitlines() or [""])[-1]


def main() -> int:
    print("the pre-update copy is bounded")
    d = tempfile.mkdtemp(prefix="tn_copy_")
    db = os.path.join(d, "trustnode_app_store.db")
    with open(db, "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024))
    out = run(db, "2")
    check("a 3 MB store is NOT copied when the cap is 2 MB", out == "null", out)
    check("  and no copy exists", not [n for n in os.listdir(d) if "backup-pre-update" in n], str(os.listdir(d)))
    out = run(db, "8")
    check("a 3 MB store IS copied when the cap is 8 MB", out not in ("null", "") and "backup-pre-update" in out, out)
    check("  and the copy exists with the same size",
          any("backup-pre-update" in n and os.path.getsize(os.path.join(d, n)) == os.path.getsize(db)
              for n in os.listdir(d)), str(os.listdir(d)))
    src = open(os.path.join(ROOT, "desktop", "workspace_detector.js"), encoding="utf-8").read()
    check("the shipped default cap is 256 MB", "TRUSTNODE_PRE_UPDATE_COPY_MAX_MB || 256" in src)
    # 2026-11-03 installer review: three more things an install must get right.
    main_js = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
    check("the writable-dir fallback is persisted (no workspace dialog on the second launch)",
          'workspaceDetector.writePersistedChoice(app.getPath("userData"), fallback)' in main_js
          and "writePersistedChoice," in src)
    state_py = open(os.path.join(ROOT, "backend", "app", "state.py"), encoding="utf-8").read()
    check("the boot build stamp uses the exe's time when packaged",
          'sys.executable if getattr(sys, "frozen", False)' in state_py)
    build_ps1 = open(os.path.join(ROOT, "scripts", "build-release.ps1"), encoding="utf-8").read()
    check("the release build refuses an artifact older than the run",
          "$distStartedAt" in build_ps1 and "a stale artifact, do not ship" in build_ps1)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
