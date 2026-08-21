import app.boot_clock  # noqa: F401  -- FIRST import: captures process T0 for boot metrics (2026-08-21)
import os
import shutil
import sys

import uvicorn


def _smoke() -> int:
    """Self-test the bundled EXE without starting the HTTP server.
    Used by the installer post-install step + CI. Imports app.main
    and exercises the critical subsystems once. Exit 0 = clean."""
    failures = []
    try:
        import app.main  # noqa: F401
    except Exception as exc:
        failures.append(f"app.main import failed: {type(exc).__name__}: {exc}")
    try:
        from app.services.app_store import AppStore
        AppStore()
    except Exception as exc:
        failures.append(f"app_store init failed: {type(exc).__name__}: {exc}")
    try:
        from app.services.license_signature import _load_public_key
        if _load_public_key() is None:
            failures.append("license public key not bundled")
    except Exception as exc:
        failures.append(f"license_signature failed: {type(exc).__name__}: {exc}")
    if failures:
        for f in failures:
            print(f"[smoke] FAIL: {f}", flush=True)
        return 1
    print("[smoke] ALL CHECKS PASSED", flush=True)
    return 0


def _boot_probe() -> int:
    """Build-time boot self-test (operator 2026-08-21, BOOT-HEALTH FIX).

    Starts the REAL app on an ephemeral port against a THROWAWAY data dir and
    asserts /api/health answers 200 within TRUSTNODE_BOOT_PROBE_BUDGET_S
    (default 20 s). Wired into scripts/build-backend.ps1 so every build proves
    the health path cannot regress into blocking on boot work again. It never
    loads a .env (TRUSTNODE_SKIP_DOTENV), never pushes config to any cloud and
    never touches a real install's data dir.

    This is the code-path guard; the release gate (scripts/validate_release.py)
    additionally asserts the REAL install's last boot (spawn -> first 200) via
    scripts/boot_log_check.py.
    """
    import socket
    import tempfile
    import threading
    import time
    import urllib.request

    data_dir = tempfile.mkdtemp(prefix="tn-bootprobe-")
    os.environ["TRUSTNODE_DATA_DIR"] = data_dir
    # EXPLICIT app-store path: AppStore._resolve_db_path() auto-MIGRATES (copies)
    # a legacy ~/.trustnode_edge app store into any fresh TRUSTNODE_DATA_DIR —
    # on a dev box that is an 8 GB copy. The explicit override skips that path.
    os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(data_dir, "trustnode_app_store.db")
    os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
    os.environ["TRUSTNODE_DISABLE_CONFIG_PUSH"] = "1"
    os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
    os.environ.setdefault("TRUSTNODE_INTELLIGENCE", "off")
    # Strip any cloud credentials inherited from the build shell.
    for k in list(os.environ):
        if k.startswith(("TRUSTNODE_SUPABASE", "TRUSTNODE_CLOUD_DB", "SUPABASE_", "DATABASE_URL")):
            os.environ.pop(k, None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    os.environ["TRUSTNODE_HOST"] = "127.0.0.1"
    os.environ["TRUSTNODE_PORT"] = str(port)
    try:
        budget = float(os.environ.get("TRUSTNODE_BOOT_PROBE_BUDGET_S", "30") or "30")
    except Exception:
        budget = 30.0

    t0 = time.monotonic()
    print(f"[boot-probe] data_dir={data_dir} port={port} budget={budget:.0f}s", flush=True)
    try:
        import app.main  # noqa: F401
    except Exception as exc:
        print(f"[boot-probe] FAIL: app.main import failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    import_s = time.monotonic() - t0

    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning", reload=False)
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, name="boot-probe-uvicorn", daemon=True)
    th.start()

    ok_at = None
    while time.monotonic() - t0 < budget:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
                if r.status == 200:
                    ok_at = time.monotonic() - t0
                    break
        except Exception:
            pass
        time.sleep(0.2)

    server.should_exit = True
    th.join(timeout=15)
    rc = 0
    if ok_at is None:
        print(f"[boot-probe] FAIL: /api/health did not return 200 within {budget:.0f}s "
              f"(import took {import_s:.2f}s)", flush=True)
        rc = 1
    else:
        print(f"[boot-probe] PASS: /api/health 200 after {ok_at:.2f}s "
              f"(import {import_s:.2f}s, budget {budget:.0f}s)", flush=True)
    for _attempt in range(3):
        try:
            shutil.rmtree(data_dir, ignore_errors=True)
        except Exception:
            pass
        if not os.path.isdir(data_dir):
            break
        time.sleep(1.0)
    sys.stdout.flush()
    sys.stderr.flush()
    # Hard exit: background pollers started by the app must not keep the
    # build's probe process alive.
    os._exit(rc)


def main() -> None:
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    if "--boot-probe" in sys.argv:
        sys.exit(_boot_probe())
    # Settings are imported lazily so the self-test modes above can shape the
    # environment (data dir, host/port) BEFORE anything reads it.
    from app.config import settings

    # Operator 2026-06-25: NO pre-flight contract. The splash now
    # waits for /api/health (proves the backend is up + routers
    # loaded) and /api/boot-probe (proves devices/DBs reachable).
    # Those are the only two signals that matter — anything else
    # adds false-negative risk we proved we don't want.
    # Operator 2026-07-03: tune HTTP keep-alive to stop connection churn.
    # The Electron renderer opens short-lived fetch connections; with the
    # default 5s keep-alive and rapid UI requests, thousands of sockets piled
    # up in TIME_WAIT and, during a burst, exhausted the browser's ~6-conn/
    # host limit → "Failed to fetch". A longer keep-alive lets the browser
    # REUSE one connection for many requests instead of opening a new socket
    # each time.
    #
    # We DO NOT set limit_concurrency — uvicorn returns HTTP 503 when that
    # cap is exceeded, which surfaced as "Service Unavailable" on delete
    # during bursts. A large accept backlog absorbs bursts instead of
    # refusing them; requests queue briefly rather than 503.
    uvicorn.run(
        "app.main:app",
        host=settings.trustnode_host,
        port=settings.trustnode_port,
        reload=False,
        timeout_keep_alive=75,   # keep idle conns open 75s → browser reuses them
        backlog=2048,            # absorb connection bursts without refusing
    )


if __name__ == "__main__":
    main()
