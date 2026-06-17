#!/usr/bin/env python3
"""
Customer DB mode + Lite endpoint smoke (operator 2026-06-17, M8).

What it covers:
  * /api/customer-db/status returns mode + supported engines.
  * /api/customer-db/test-connection refuses bogus creds cleanly.
  * /api/lan-sharing/status reports current bind host + IPs.
  * /api/lite-local/validate rejects missing tokens.
  * /lite/ static page is reachable.

What it does NOT cover (needs a real Postgres):
  * /api/customer-db/activate path (requires creds + actual DB).
  * Schema bootstrap.
  * Power-meter / PLC mirror to customer DB.

Pass an actual Postgres DSN via env to exercise the activate path:

  TRUSTNODE_SMOKE_PG_HOST=localhost \\
  TRUSTNODE_SMOKE_PG_USER=postgres \\
  TRUSTNODE_SMOKE_PG_PASS=secret \\
  TRUSTNODE_SMOKE_PG_DB=trustnode_test \\
  python tests/smoke/smoke_customer_db_mode.py
"""
from __future__ import annotations

import os
import sys
import time

import requests


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[36mINFO\033[0m"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def resolve_api_base() -> str:
    env = os.environ.get("TRUSTNODE_API_BASE", "").strip()
    if env:
        return env.rstrip("/")
    for port in range(8000, 8010):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
            if r.ok:
                return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return "http://127.0.0.1:8000"


def authenticate(api_base: str) -> dict:
    user = os.environ.get("TRUSTNODE_SMOKE_USER", "admin")
    pwd = os.environ.get("TRUSTNODE_SMOKE_PASS", "admin")
    try:
        r = requests.post(f"{api_base}/api/auth/login",
                          json={"username": user, "password": pwd}, timeout=4)
        if r.ok:
            tok = (r.json() or {}).get("token")
            if tok:
                return {"Authorization": f"Bearer {tok}"}
    except Exception:
        pass
    return {}


def add(name: str, status: str, detail: str = "") -> bool:
    bar = {"PASS": PASS, "FAIL": FAIL, "WARN": WARN, "INFO": INFO}[status]
    print(f"  [{bar}] {name}" + (f"  — {detail}" if detail else ""))
    return status != "FAIL"


def main() -> int:
    api = resolve_api_base()
    print(f"API: {api}")
    headers = authenticate(api)
    fails = 0
    if not headers:
        fails += 1 if not add("admin/admin login", "FAIL", "no token") else 0
        print("aborting — cannot exercise admin endpoints without auth.")
        return 2

    add("admin/admin login", "PASS")

    # 1. /api/customer-db/status -----------------------------------------
    print("\n== 1. /api/customer-db/status ==")
    try:
        r = requests.get(f"{api}/api/customer-db/status", headers=headers, timeout=4).json()
        ok = bool(r.get("ok")) and r.get("mode") in ("local_sqlite", "customer_sql")
        add("status", "PASS" if ok else "FAIL", f"mode={r.get('mode')} engines={r.get('supported_engines')}")
        if not ok:
            fails += 1
    except Exception as exc:
        add("status", "FAIL", str(exc))
        fails += 1

    # 2. Test connection — bogus creds should fail cleanly ---------------
    print("\n== 2. /api/customer-db/test-connection (bogus) ==")
    try:
        r = requests.post(f"{api}/api/customer-db/test-connection", headers=headers, json={
            "target": {
                "engine": "postgresql",
                "host": "127.0.0.1",
                "port": 1,          # nothing listens here
                "database": "x",
                "username": "x",
                "password": "x",
                "schema": "public",
                "tls": False,
            }
        }, timeout=10).json()
        # We expect ok=False with a non-empty error and a numeric latency.
        ok = (r.get("ok") is False and r.get("error"))
        add("bogus connect rejected", "PASS" if ok else "FAIL", f"ok={r.get('ok')} err={r.get('error', '')[:60]}")
        if not ok:
            fails += 1
    except Exception as exc:
        add("bogus connect", "FAIL", str(exc))
        fails += 1

    # 3. Real Postgres (optional) ---------------------------------------
    pg_host = os.environ.get("TRUSTNODE_SMOKE_PG_HOST", "").strip()
    if pg_host:
        print("\n== 3. /api/customer-db/test-connection (real PG) ==")
        target = {
            "engine": "postgresql",
            "host": pg_host,
            "port": int(os.environ.get("TRUSTNODE_SMOKE_PG_PORT", "5432") or "5432"),
            "database": os.environ.get("TRUSTNODE_SMOKE_PG_DB", "postgres"),
            "username": os.environ.get("TRUSTNODE_SMOKE_PG_USER", "postgres"),
            "password": os.environ.get("TRUSTNODE_SMOKE_PG_PASS", ""),
            "schema": os.environ.get("TRUSTNODE_SMOKE_PG_SCHEMA", "public"),
            "tls": os.environ.get("TRUSTNODE_SMOKE_PG_TLS", "0") in ("1", "true", "True"),
        }
        try:
            r = requests.post(f"{api}/api/customer-db/test-connection", headers=headers,
                              json={"target": target}, timeout=15).json()
            ok = bool(r.get("ok"))
            add("real PG test-connection", "PASS" if ok else "FAIL", f"latency={r.get('latency_ms')}ms err={r.get('error', '')[:80]}")
            if ok:
                r = requests.post(f"{api}/api/customer-db/activate", headers=headers,
                                  json={"target": target, "confirm_backup": True}, timeout=20)
                ok2 = r.ok
                detail = r.json() if r.headers.get("content-type","" ).startswith("application/json") else r.text
                add("real PG activate (incl. schema bootstrap)", "PASS" if ok2 else "FAIL", str(detail)[:120])
                if not ok2:
                    fails += 1
        except Exception as exc:
            add("real PG", "FAIL", str(exc))
            fails += 1
    else:
        add("real PG", "INFO", "skipped (set TRUSTNODE_SMOKE_PG_HOST to exercise)")

    # 4. /api/lan-sharing/status ----------------------------------------
    print("\n== 4. /api/lan-sharing/status ==")
    try:
        r = requests.get(f"{api}/api/lan-sharing/status", headers=headers, timeout=4).json()
        add("lan-sharing status", "PASS",
            f"enabled={r.get('enabled')} bind={r.get('bind_host')} ips={r.get('ips')}")
    except Exception as exc:
        add("lan-sharing status", "FAIL", str(exc))
        fails += 1

    # 5. /api/lite-local/validate (no token) -----------------------------
    print("\n== 5. /api/lite-local/validate (missing token) ==")
    try:
        r = requests.post(f"{api}/api/lite-local/validate", json={"token": "definitely-not-a-real-token"}, timeout=4)
        # Expect 404 (view link not found) — auth middleware doesn't block.
        ok = r.status_code in (403, 404)
        add("missing token rejected", "PASS" if ok else "FAIL", f"status={r.status_code}")
        if not ok:
            fails += 1
    except Exception as exc:
        add("missing token", "FAIL", str(exc))
        fails += 1

    # 6. /lite/ static --------------------------------------------------
    print("\n== 6. /lite/ static page ==")
    try:
        r = requests.get(f"{api}/lite/", timeout=4)
        ok = r.ok and "TrustNode Local Lite" in r.text
        add("/lite/ reachable", "PASS" if ok else "WARN", f"status={r.status_code} bytes={len(r.text)}")
    except Exception as exc:
        add("/lite/", "WARN", str(exc))

    print()
    if fails:
        print(f"FAILED: {fails} check(s) failed.")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
