import sys

import uvicorn

from app.config import settings


def _smoke() -> int:
    """Phase 3e (operator 2026-06-18): self-test the bundled EXE.

    Runs without starting the HTTP server. Returns exit code 0 if every
    critical subsystem boots cleanly, non-zero on first failure with a
    descriptive print so a CI / installer step can fail-fast on a
    broken EXE.

    Tests:
      1. app.main imports without exception (catches missing hidden imports,
         deprecated routers, etc.)
      2. SQLite app_store opens + writes to the configured location
      3. License signature module loads the bundled PUBLIC key
      4. license_gate evaluates without crashing
    """
    failures = []

    # 1. Module imports
    try:
        import app.main  # noqa: F401
        print("[smoke] app.main imports OK")
    except Exception as exc:
        failures.append(f"app.main import failed: {type(exc).__name__}: {exc}")

    # 2. App store reachable
    try:
        from app.services.app_store import AppStore
        store = AppStore()
        path = store._db_path  # type: ignore[attr-defined]
        print(f"[smoke] app_store at {path}")
    except Exception as exc:
        failures.append(f"app_store init failed: {type(exc).__name__}: {exc}")

    # 3. License public key loadable
    try:
        from app.services.license_signature import _load_public_key
        pk = _load_public_key()
        if pk is None:
            failures.append("license public key NOT bundled (license_signing_public.pem missing)")
        else:
            print("[smoke] license public key loaded")
    except Exception as exc:
        failures.append(f"license_signature failed: {type(exc).__name__}: {exc}")

    # 4. License gate runs
    try:
        from app.services.license_gate import is_data_writes_allowed
        allowed, reason = is_data_writes_allowed(force_refresh=True)
        print(f"[smoke] license_gate: allowed={allowed} reason={reason}")
    except Exception as exc:
        failures.append(f"license_gate crashed: {type(exc).__name__}: {exc}")

    if failures:
        print("\n[smoke] FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n[smoke] ALL CHECKS PASSED")
    return 0


def main() -> None:
    # Phase 3e: --smoke runs a self-test and exits without launching the
    # HTTP server. Used by the installer's post-install validation step
    # and by CI smoke tests.
    if "--smoke" in sys.argv:
        sys.exit(_smoke())

    # The primary server always binds to 127.0.0.1 (or whatever the
    # operator/env overrides). LAN sharing is provided by a *second*
    # uvicorn server started in-process (see services/lan_socket.py)
    # so toggling LAN never requires a restart.
    uvicorn.run(
        "app.main:app",
        host=settings.trustnode_host,
        port=settings.trustnode_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
