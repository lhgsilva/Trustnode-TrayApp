# -*- coding: utf-8 -*-
"""An admin can really delete data - only their own, only when they mean it.

2026-10-20, requested: "we also should have options to delete (real delete)
data from local databases and cloud (supabase) to save space so the customer
can control of their data when is logged with admin privilegy".

Retention decides by age on a schedule. This is the other half: remove this,
now, because I say so. Four things have to hold or it is a liability rather
than a feature.

  * ADMIN ONLY, enforced on the SERVER. The legacy cleanup routes were gated
    in the browser alone, which meant any authenticated viewer could wipe the
    historian. A 403 here is the whole point.
  * TENANT SCOPED, with no way to opt out. One SQLite file routinely holds
    several customers, and the cloud database is shared between them with the
    edge connecting as a role that BYPASSES RLS. An unscoped DELETE would
    take another company's history. That is a data-protection failure
    (CLAUDE.md 1b), not a display bug - so the tenant comes from this edge's
    own scope key and never from the request.
  * A CUTOFF IS REQUIRED. "Delete everything" must not be reachable by
    omitting a field.
  * COUNT BEFORE DELETE. dry_run is the default and confirm must be explicit;
    an operator gets to see the number before it happens.

Drives a real backend on a scratch workspace. The live store is never opened.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("PURGE_PORT", "8191")
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:190]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


TMP = tempfile.mkdtemp(prefix="tn-purge-")
ENV = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=TMP,
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
ENV.pop("TRUSTNODE_APP_STORE_PATH", None)

# A backend left over from an earlier run answers here and looks exactly
# like ours, so the test would validate someone else's workspace.
try:
    urllib.request.urlopen(API + "/api/health", timeout=3).read()
    print("REFUSING: something already serves on port %s - stop it, or set "
          "PURGE_PORT to a free port." % PORT)
    raise SystemExit(2)
except SystemExit:
    raise
except Exception:
    pass

LOG = open(os.path.join(TMP, "o.log"), "w")
PROC = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=ENV,
                        stdout=LOG, stderr=subprocess.STDOUT)


def call(method, path, body=None, token=None, timeout=120):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body is not None else None,
        headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "null")
        except Exception:
            return e.code, raw[:300]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def finish(code):
    try:
        PROC.terminate(); PROC.wait(timeout=25)
    except Exception:
        PROC.kill()
    sys.exit(code)


print("TrustNode - admin purge deletes the right rows and nobody else's")
print()

up = False
for _ in range(75):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)
if not check("the app came up", up):
    print(io.open(os.path.join(TMP, "o.log"), encoding="utf-8",
                  errors="replace").read()[-1200:])
    finish(2)

STORE = os.path.join(TMP, "trustnode_app_store.db")
check("  and it is on the scratch workspace", os.path.exists(STORE), STORE)

MINE = "default"              # what a fresh edge resolves to
THEIRS = "tenant-someone-else"
OLD = "2020-01-01 00:00:00"
NEW = (dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
CUT = "2021-01-01 00:00:00"


def seed():
    c = sqlite3.connect(STORE, timeout=30)
    c.execute("DELETE FROM historian_readings")
    for tenant in (MINE, THEIRS):
        for ts in (OLD, NEW):
            c.execute(
                "INSERT INTO historian_readings (ts_utc, created_utc, tag_name, "
                "value, quality, tenant_id) VALUES (?,?,?,?,?,?)",
                (ts, ts, "T", 1.0, 192, tenant))
    c.commit(); c.close()


def rows(tenant):
    c = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True,
                        timeout=30)
    n = c.execute("SELECT COUNT(*) FROM historian_readings WHERE tenant_id=?",
                  (tenant,)).fetchone()[0]
    c.close(); return n


seed()
check("  seeded both tenants", rows(MINE) == 2 and rows(THEIRS) == 2,
      "%s / %s" % (rows(MINE), rows(THEIRS)))

# ---- a session. The first admin is created by the recovery/bootstrap path;
# use whatever the install already has.
st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
TOKEN = (r or {}).get("token") if isinstance(r, dict) else None
check("an admin session was obtained", bool(TOKEN), "HTTP %s %s" % (st, str(r)[:110]))

print()
print("[1. it is not reachable without an admin session]")
st, _ = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "before_utc": CUT, "confirm": True})
check("no session is refused", st in (401, 403), "HTTP %s" % st)
check("  and nothing was deleted", rows(MINE) == 2, rows(MINE))

if not TOKEN:
    print()
    print("RESULT: FAIL - no admin session, cannot exercise the rest")
    finish(2)

print()
print("[1b. an AUTHENTICATED NON-ADMIN is refused]")
# The 401 above comes from the auth middleware, not from the admin gate, so
# it passes even with _require_admin deleted - a test that proves nothing.
# The real exposure is a logged-in VIEWER. Call the endpoint directly with a
# viewer payload so the gate itself is what decides.
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_DATA_DIR", TMP)
try:
    from fastapi import HTTPException as _HE

    from app.routers import retention as _ret

    class _State:
        def __init__(self, role):
            self.user_payload = {"sub": "viewer-1", "role": role}

    class _Req:
        def __init__(self, role):
            self.state = _State(role)

    body = _ret.PurgeIn(targets=["historian_raw"], before_utc=CUT,
                        dry_run=False, confirm=True)
    status = None
    try:
        _ret.purge_data(body, _Req("viewer"))
        status = 200
    except _HE as exc:
        status = exc.status_code
    check("a logged-in viewer gets 403", status == 403,
          "got %s - the browser gate is not a gate; the legacy cleanup routes "
          "were viewer-reachable for exactly this reason" % status)
    check("  and the viewer deleted nothing",
          rows(MINE) == 2 and rows(THEIRS) == 2,
          "%s / %s" % (rows(MINE), rows(THEIRS)))
    # ...while an admin payload is NOT refused by the gate (proves the 403
    # above came from the role, not from something incidental).
    try:
        _ret.purge_data(_ret.PurgeIn(targets=["historian_raw"], before_utc=CUT),
                        _Req("admin"))
        admin_ok = True
    except _HE as exc:
        admin_ok = exc.status_code != 403
    check("  an admin payload is NOT refused by the gate", admin_ok)
except Exception as exc:  # noqa: BLE001
    check("the admin gate could be exercised", False,
          "%s: %s" % (type(exc).__name__, exc))

print()
print("[2. a purge without a cutoff is refused]")
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "confirm": True}, TOKEN)
check("missing before_utc is a 400", st == 400, "HTTP %s %s" % (st, str(r)[:90]))
check("  and it says why", "cutoff" in json.dumps(r).lower()
      or "before_utc" in json.dumps(r).lower(), str(r)[:120])
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["nonsense"], "before_utc": CUT, "confirm": True}, TOKEN)
check("  an unknown target is a 400", st == 400, "HTTP %s" % st)

print()
print("[3. dry run counts, and deletes nothing]")
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "before_utc": CUT}, TOKEN)
check("the dry run is accepted", st == 200, "HTTP %s %s" % (st, str(r)[:120]))
got = ((r or {}).get("results") or {}).get("historian_raw", {}).get("local")
check("  it counts only MY old row", got == 1,
      "counted %s (the other tenant's old row must not be in this number)" % got)
check("  nothing was actually removed", rows(MINE) == 2 and rows(THEIRS) == 2,
      "%s / %s" % (rows(MINE), rows(THEIRS)))
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "before_utc": CUT, "dry_run": False},
             TOKEN)
check("  dry_run=false WITHOUT confirm still only counts",
      (r or {}).get("dry_run") is True and rows(MINE) == 2,
      "confirm must be explicit: %s" % str(r)[:110])

print()
print("[4. a confirmed purge removes my old row and only mine]")
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "before_utc": CUT,
              "dry_run": False, "confirm": True}, TOKEN)
check("the purge ran", st == 200 and (r or {}).get("dry_run") is False,
      "HTTP %s %s" % (st, str(r)[:120]))
check("  my old row is gone", rows(MINE) == 1, rows(MINE))
check("  my recent row survived", rows(MINE) == 1, rows(MINE))
check("  THE OTHER TENANT IS UNTOUCHED", rows(THEIRS) == 2,
      "one customer deleting another's history is the failure this exists to "
      "prevent - found %s rows" % rows(THEIRS))

print()
print("[5. the tenant comes from the edge, not the caller]")
seed()
st, r = call("POST", "/api/app-store/data/purge",
             {"targets": ["historian_raw"], "before_utc": CUT, "dry_run": False,
              "confirm": True, "tenant_id": THEIRS}, TOKEN)
check("a tenant_id in the body is ignored", rows(THEIRS) == 2,
      "the payload must not be able to aim the delete at another customer")
check("  and my own rows were purged as normal", rows(MINE) == 1, rows(MINE))

print()
print("[6. the source says why, for whoever edits it next]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "retention.py"),
              encoding="utf-8").read()
check("the cloud delete is tenant-scoped in SQL",
      "AND tenant_id = :t" in SRC,
      "the edge connects to a SHARED cloud database with a role that "
      "bypasses RLS - the WHERE clause is the only thing protecting the "
      "other customers")
check("  the purge is audited when it really runs",
      "append_log_rows" in SRC and "admin purge by" in SRC)
check("  and it is paced, not one giant DELETE",
      "LIMIT 5000" in SRC,
      "a multi-minute write lock stalls collection behind it")

print()
print("[7. the cloud preview answers fast enough for a browser]")
# 2026-10-21: the Delete data card showed "Failed to fetch" with Where=Both.
# That is a NETWORK error - the browser got no response. _purge_cloud ran a
# plain COUNT(*) per target against a 57 GB historian, so the request never
# returned. Bounded now: a Preview needs an order of magnitude, fast.
RSRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "retention.py"),
               encoding="utf-8").read()
pc = RSRC[RSRC.index("def _purge_cloud"):RSRC.index("@router.get(\"/data/purge/targets\")")]
check("the cloud count is capped", "LIMIT :cap" in pc,
      "an unbounded COUNT over 92M rows outlives the browser")
check("  and it says when it capped", '"capped"' in pc,
      "a capped number presented as exact would mislead the decision")
check("  a statement timeout bounds it too", "SET statement_timeout" in pc)
check("  a missing table is named, not called a timeout",
      "not in the cloud database" in pc,
      "historian_rollup is local-only; reporting that as a timeout sent "
      "someone hunting a performance problem that did not exist")
check("  and the delete is batched, not one statement",
      "LIMIT 20000" in pc,
      "one DELETE over millions of rows holds locks against the forwarder")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
