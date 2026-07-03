import sys

import uvicorn

from app.config import settings


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


def main() -> None:
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
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
