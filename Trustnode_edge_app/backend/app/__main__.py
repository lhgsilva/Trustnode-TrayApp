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
    uvicorn.run(
        "app.main:app",
        host=settings.trustnode_host,
        port=settings.trustnode_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
