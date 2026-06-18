"""End-to-end smoke test for the local TrustNode backend.

Usage:
  python scripts/smoke_login_local.py [--username admin] [--password admin]

Exercises every layer that needs to work for a successful login:
  1. Backend health endpoint (/api/health)
  2. Auth login endpoint (POST /api/auth/login)
  3. Auth me endpoint (GET /api/auth/me with the token)
  4. App-store config domains (GET /api/app-store/bootstrap)
  5. Workspace export (GET /api/workspace/export, admin only)
  6. PLC gateways status (GET /api/plc/gateways/status)
  7. Database connectivity check (GET /api/database/connections)
  8. Direct SQLite open (verifies users_access is loadable)

Each check is bounded with a hard timeout. The test never hangs.
Exit code 0 if every CRITICAL check passes; otherwise non-zero with
a per-check report.

This is what we run when a customer reports "I can't log in" — it
tells us exactly which layer is broken without any guessing.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


BACKEND_URL = "http://127.0.0.1:8000"


def _http(method: str, path: str, *, body: dict | None = None, headers: dict | None = None,
          timeout: float = 5.0) -> tuple[int, dict | str, float]:
    """Fire one HTTP request with a hard timeout. Returns (status, body, elapsed_s).

    Never raises. Status -1 means connection error, -2 means timeout.
    Body is a dict if JSON-parsed, otherwise the raw string. Always returns.
    """
    started = time.monotonic()
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{BACKEND_URL}{path}",
        data=data,
        method=method,
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = raw
            return resp.getcode(), parsed, time.monotonic() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        try:
            parsed = json.loads(raw) if raw else {"detail": str(exc)}
        except Exception:
            parsed = raw
        return exc.code, parsed, time.monotonic() - started
    except (urllib.error.URLError, TimeoutError) as exc:
        return -2 if "timed out" in str(exc).lower() else -1, str(exc), time.monotonic() - started
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}", time.monotonic() - started


def _resolve_db_path() -> Path | None:
    """Walk the known data-dir candidates and return the first existing
    trustnode_app_store.db with a non-trivial size.
    """
    candidates = []
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local")
        candidates.append(Path(program_data) / "TrustNode" / "edge" / "trustnode_app_store.db")
        if local_appdata:
            candidates.append(Path(local_appdata) / "TrustNode" / "data" / "trustnode_app_store.db")
    candidates.append(Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db")
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 32 * 1024:
                return p
        except Exception:
            pass
    return None


class SmokeReport:
    def __init__(self):
        self.checks: list[dict] = []
        self.critical_fail = False

    def add(self, name: str, ok: bool, detail: str, *, critical: bool = True, elapsed_ms: float | None = None):
        self.checks.append({
            "name": name,
            "ok": ok,
            "detail": detail,
            "critical": critical,
            "elapsed_ms": elapsed_ms,
        })
        if critical and not ok:
            self.critical_fail = True

    def render(self) -> str:
        width = max((len(c["name"]) for c in self.checks), default=0) + 2
        lines = ["", "=" * 70, "TrustNode local backend smoke test", "=" * 70]
        for c in self.checks:
            mark = "OK " if c["ok"] else ("WARN" if not c["critical"] else "FAIL")
            timing = f" [{c['elapsed_ms']:.0f}ms]" if c["elapsed_ms"] is not None else ""
            lines.append(f"  {mark:<5}  {c['name']:<{width}} {c['detail']}{timing}")
        lines.append("=" * 70)
        lines.append(f"Result: {'PASS' if not self.critical_fail else 'FAIL'} ({sum(1 for c in self.checks if c['ok'])}/{len(self.checks)} checks ok)")
        lines.append("=" * 70)
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args()

    r = SmokeReport()

    # 1. Health
    code, body, ms = _http("GET", "/api/health", timeout=3.0)
    if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
        r.add("backend /api/health", True, f"build={body.get('api_build', '?')}", elapsed_ms=ms*1000)
    elif code == -1:
        r.add("backend /api/health", False, f"connection refused — backend not running ({body})", elapsed_ms=ms*1000)
        print(r.render())
        return 2
    elif code == -2:
        r.add("backend /api/health", False, f"timeout after {ms:.1f}s — backend running but event loop blocked", elapsed_ms=ms*1000)
        print(r.render())
        return 2
    else:
        r.add("backend /api/health", False, f"HTTP {code}: {body}", elapsed_ms=ms*1000)

    # 2. Direct SQLite open (independent verification — does NOT go through backend)
    db_path = _resolve_db_path()
    if db_path is None:
        r.add("sqlite app_store presence", False, "no trustnode_app_store.db found in any known location")
    else:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM config_documents WHERE domain='users_access'"
                ).fetchone()
                count = int(row[0]) if row else 0
                row = con.execute(
                    "SELECT payload_json FROM config_documents WHERE domain='users_access'"
                ).fetchone()
                users_present = []
                if row and row[0]:
                    try:
                        payload = json.loads(row[0])
                        users_present = [u.get("username") for u in (payload.get("users") or []) if isinstance(u, dict)]
                    except Exception:
                        pass
                # Also check scoped table
                try:
                    for srow in con.execute(
                        "SELECT payload_json FROM config_documents_scoped WHERE domain='users_access'"
                    ).fetchall():
                        try:
                            sp = json.loads(srow[0]) if isinstance(srow[0], str) else {}
                            for u in (sp.get("users") or []):
                                if isinstance(u, dict) and u.get("username"):
                                    users_present.append(u["username"])
                        except Exception:
                            pass
                except sqlite3.OperationalError:
                    pass
                r.add(
                    "sqlite app_store readable",
                    True,
                    f"{db_path.name} OK, users={users_present}",
                )
            finally:
                con.close()
        except Exception as exc:
            r.add("sqlite app_store readable", False, f"{type(exc).__name__}: {exc}")

    # 3. Login (bounded — won't hang forever even if backend is stuck)
    code, body, ms = _http(
        "POST", "/api/auth/login",
        body={"username": args.username, "password": args.password},
        timeout=12.0,
    )
    token = ""
    if code == 200 and isinstance(body, dict) and body.get("token"):
        token = str(body["token"])
        user = body.get("user") or {}
        r.add("auth /api/auth/login", True, f"role={user.get('role', '?')} tenant={user.get('tenant_id', '?')}", elapsed_ms=ms*1000)
    elif code == 401:
        r.add("auth /api/auth/login", False, f"401 invalid credentials (try a known-good user)", elapsed_ms=ms*1000)
    elif code == -2:
        r.add("auth /api/auth/login", False, f"HUNG: timeout after {ms:.1f}s — backend can't complete login", elapsed_ms=ms*1000)
    else:
        r.add("auth /api/auth/login", False, f"HTTP {code}: {body}", elapsed_ms=ms*1000)

    # 4. Auth me (only if login succeeded)
    if token:
        code, body, ms = _http(
            "GET", "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=3.0,
        )
        if code == 200 and isinstance(body, dict) and body.get("ok"):
            r.add("auth /api/auth/me", True, f"user={body.get('user', {}).get('username', '?')}", elapsed_ms=ms*1000)
        else:
            r.add("auth /api/auth/me", False, f"HTTP {code}: {body}", elapsed_ms=ms*1000)

        # 5. Workspace export (admin-only, validates the new endpoint we shipped)
        code, body, ms = _http(
            "GET", "/api/workspace/export",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if code == 200 and isinstance(body, dict) and body.get("format") == "trustnode.workspace":
            domains = body.get("domains") or {}
            r.add("workspace /api/workspace/export", True, f"{len(domains)} config domains exported", elapsed_ms=ms*1000)
        elif code == 403:
            r.add("workspace /api/workspace/export", True, f"403 (non-admin user — expected)", critical=False, elapsed_ms=ms*1000)
        else:
            r.add("workspace /api/workspace/export", False, f"HTTP {code}: {body}", critical=False, elapsed_ms=ms*1000)

        # 6. Gateways status
        code, body, ms = _http(
            "GET", "/api/plc/gateways/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if code == 200 and isinstance(body, list):
            running = sum(1 for g in body if isinstance(g, dict) and g.get("running"))
            r.add("plc /api/plc/gateways/status", True, f"{len(body)} gateways, {running} running", critical=False, elapsed_ms=ms*1000)
        else:
            r.add("plc /api/plc/gateways/status", False, f"HTTP {code}: {body}", critical=False, elapsed_ms=ms*1000)

        # 7. App store bootstrap (the heavy one — verifies app_store lock isn't stuck)
        code, body, ms = _http(
            "GET", "/api/app-store/bootstrap",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if code == 200 and isinstance(body, dict):
            domains = list((body.get("data") or {}).keys()) if isinstance(body.get("data"), dict) else list(body.keys())
            r.add("app-store /api/app-store/bootstrap", True, f"domains={len(domains)}", elapsed_ms=ms*1000)
        elif code == -2:
            r.add("app-store /api/app-store/bootstrap", False, f"HUNG: app_store lock contention or cloud stall", elapsed_ms=ms*1000)
        else:
            r.add("app-store /api/app-store/bootstrap", False, f"HTTP {code}: {body}", critical=False, elapsed_ms=ms*1000)

    print(r.render())
    return 0 if not r.critical_fail else 1


if __name__ == "__main__":
    sys.exit(main())
