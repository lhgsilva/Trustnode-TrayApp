"""8-HOUR SOAK GATE — the long-run sibling of scripts/validate_release.py.

The 10-minute release gate proves a build is not broken. This proves it can be
LEFT RUNNING: that collection holds its cadence for a working day, that the
store-and-forward buffer drains instead of growing without bound, that nothing
leaks, and that no gap opens which the 10-minute window is simply too short to
see.

    python scripts/validate_soak_8h.py            # 8 hours (default)
    VAL_SOAK_SECONDS=3600 python scripts/validate_soak_8h.py   # shorter trial

PREFLIGHT — why it exists (2026-08-22)
--------------------------------------
The desktop app was once launched from a shell that still had
`TRUSTNODE_DATA_DIR` pointing at a throwaway test workspace. It opened an EMPTY
store and behaved perfectly for one: no gateways, nothing collecting, every page
blank. A soak started in that state would have watched an empty database for
eight hours and reported a clean run, because there was nothing to go wrong.

So this gate refuses to start unless it can prove it is measuring the real
thing: the default workspace, a configured gateway, and a historian that is
demonstrably advancing right now. A gate that can pass while measuring nothing
is worse than no gate at all — it converts an outage into a certificate.

Exit codes: 0 PASS, 2 FAIL, 3 preflight refused (nothing was measured).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("VAL_API", "http://127.0.0.1:8000")
SOAK_SECONDS = int(os.environ.get("VAL_SOAK_SECONDS", str(8 * 3600)))
LOGIN = {"username": os.environ.get("VAL_USER", "admin-mari"),
         "password": os.environ.get("VAL_PASS", "Limerick2019*")}
GROWTH_WINDOW_S = int(os.environ.get("VAL_GROWTH_WINDOW_S", "45"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _call(method: str, path: str, token: str = "", body=None, timeout: int = 30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return 0, str(e)[:300]


def preflight() -> tuple[bool, list[str]]:
    """Prove there is something real to measure. Returns (ok, notes)."""
    notes: list[str] = []
    fatal: list[str] = []

    def ok(name, good, detail=""):
        notes.append(f"  {name:52s}: {'OK' if good else 'REFUSED'}"
                     f"{(' - ' + str(detail)[:150]) if detail else ''}")
        if not good:
            fatal.append(name)

    st, health = _call("GET", "/api/health")
    ok("backend answers /api/health", st == 200, f"status={st}")
    if st != 200:
        return False, notes

    # 1. The workspace. This is the check that would have caught 2026-08-22 in
    #    one second instead of after an eight-hour run of nothing.
    ws = (health or {}).get("workspace") if isinstance(health, dict) else None
    if not isinstance(ws, dict) or not ws:
        notes.append("  (this build predates /api/health.workspace; "
                     "workspace identity NOT verified)")
    else:
        ok("serving the machine's default workspace", bool(ws.get("is_default")),
           f"data_dir={ws.get('data_dir')} override={ws.get('override_source')}")
        ok("not hiding a populated workspace", not ws.get("hiding_real_data"),
           ws.get("warning") or "")
        ok("this workspace has collected data", ws.get("has_data_here") == 1,
           f"has_data_here={ws.get('has_data_here')}")

    st, body = _call("POST", "/api/auth/login", body=LOGIN)
    token = (body or {}).get("token") if isinstance(body, dict) else ""
    ok("operator login", st == 200 and bool(token), f"status={st}")
    if not token:
        return False, notes

    # 2. A gateway must exist AND be running — an idle edge cannot fail a soak.
    st, gws = _call("GET", "/api/plc/gateways/status", token)
    rows = gws if isinstance(gws, list) else []
    running = [g for g in rows if isinstance(g, dict) and g.get("running")]
    ok("at least one gateway is configured", bool(rows), f"{len(rows)} gateway(s)")
    ok("at least one gateway is RUNNING", bool(running),
       f"{len(running)} running of {len(rows)}")

    # 3. The historian must be advancing right now, not merely non-empty.
    def _latest() -> str:
        s, b = _call("GET", "/api/app-store/historian?limit=1", token)
        rows_ = (b or {}).get("rows") if isinstance(b, dict) else b
        if isinstance(rows_, list) and rows_:
            row = rows_[0] or {}
            # This endpoint returns `ts`/`tag`; the store's own columns are
            # `ts_utc`/`tag_name`. Accept both, because reading the wrong key
            # yields an empty string that looks exactly like "not collecting"
            # and would refuse a perfectly healthy soak.
            return str(row.get("ts") or row.get("ts_utc") or "")
        return ""

    first = _latest()
    if first:
        time.sleep(GROWTH_WINDOW_S)
        second = _latest()
        ok(f"historian advanced within {GROWTH_WINDOW_S}s", bool(second) and second != first,
           f"{first} -> {second}")
    else:
        ok("historian has readings to sample", False, "no rows returned")

    return not fatal, notes


def main() -> int:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 78)
    print(f"TRUSTNODE 8-HOUR SOAK GATE   start={started}   duration={SOAK_SECONDS}s")
    print("=" * 78)
    print("\n[PREFLIGHT] proving there is something real to measure")
    good, notes = preflight()
    for line in notes:
        print(line)
    if not good:
        print("\nPREFLIGHT REFUSED — the soak did NOT run.")
        print("Nothing was measured, so this is not a failure of the build: fix the")
        print("condition above (most often the app is serving a substitute workspace,")
        print("or no gateway is running) and start the soak again.")
        return 3

    print(f"\n[SOAK] handing over to validate_full_12h.py for {SOAK_SECONDS}s "
          f"({SOAK_SECONDS / 3600:.1f} h)\n", flush=True)
    env = dict(os.environ)
    env["VAL_DURATION_S"] = str(SOAK_SECONDS)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # -u: an 8-hour run whose output is redirected to a file is invisible for
    # eight hours if Python buffers it. Unbuffered costs nothing and lets the
    # operator watch progress instead of waiting for the verdict.
    rc = subprocess.call([sys.executable, "-u", os.path.join(HERE, "validate_full_12h.py")], env=env)

    print()
    print("=" * 78)
    if rc == 0:
        print(f"8-HOUR SOAK: PASS  (started {started}, ended {time.strftime('%Y-%m-%d %H:%M:%S')})")
    else:
        print(f"8-HOUR SOAK: FAIL (rc={rc}) — read scripts/validation_out/validation_report.txt")
    print("=" * 78)
    return 0 if rc == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
