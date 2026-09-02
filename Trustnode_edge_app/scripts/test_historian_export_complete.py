# -*- coding: utf-8 -*-
"""An export must contain the whole range, not a page of it.

2026-08-31: "I tried to export data using the historian export tab but it did
not export fully the data, we need it to be complete not only few hundreds of
lines."

Two defects produced that.

1. THE SILENT FALLBACK. The export wrote the Export tab's loaded rows *if any
   existed*, and otherwise fell back to `historianRows` - the LIVE tab's
   in-memory `dataLogView` buffer, a small rolling window of recent readings.
   Run the export without a successful Load and the file was the live window,
   labelled as a historian export. That is the "few hundred lines".

2. THE CAP. Even after Load, the set came from ONE request for 20 000 rows.
   This install writes ~108 rows/s: a day is ~9.3 million rows, so 20 000 is
   its first three minutes.

The export now pages the backend until the range is exhausted. The runtime half
of this test proves the backend can actually be paged - that offset works, that
pages do not overlap or skip, and that a short page reliably means "done" -
because the client's loop depends on all three.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8176"
API = "http://127.0.0.1:" + PORT
ROWS = 45_000                    # more than one page, deliberately
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[the export writes the range, not the live buffer]")
app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("the export fetches its own rows for the range",
      "fetchAllHistorianRowsForExport" in app
      and "offset += page.length" in app,
      "it used to write whatever the preview held")
check("it no longer falls back to the live tab's buffer",
      ": historianRows;" not in app.split("const buildHistorianExportRows", 1)[-1][:1500],
      "dataLogView is a rolling window of recent readings, not the historian")
check("a truncated export SAYS it was truncated",
      "EXPORT_MAX_ROWS" in app and "export limit" in app,
      "a limit an operator can see is one they can work around")
check("an empty range is reported, not written as an empty file",
      "Nothing to export" in app)

print()
print("[the backend can actually be paged]")
tmp = tempfile.mkdtemp(prefix="tn-export-")
store = os.path.join(tmp, "s.db")
import sqlite3  # noqa: E402

conn = sqlite3.connect(store)
conn.execute("""CREATE TABLE IF NOT EXISTS historian_readings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id TEXT NOT NULL DEFAULT 'default', ts_utc TEXT NOT NULL,
                  gateway_id TEXT NULL, gateway_name TEXT NULL, device_name TEXT NULL,
                  plc_ip TEXT NULL, database_name TEXT NULL, tag_name TEXT NOT NULL,
                  value REAL NULL, value_text TEXT NULL, data_type TEXT NULL,
                  quality INTEGER NULL, quality_label TEXT NULL, source TEXT NULL,
                  created_utc TEXT NOT NULL)""")
conn.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, tag_name, value,"
    " quality, created_utc) VALUES (?,?,?,?,?,?,?)",
    (("default", "2026-08-30T%02d:%02d:%02dZ" % (i // 3600 % 24, i // 60 % 60, i % 60),
      "gw-export", "T1", float(i), 192, "2026-08-30T00:00:00Z") for i in range(ROWS)))
conn.commit()
conn.close()
print("  seeded %s rows" % f"{ROWS:,}")

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=store, TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
           TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(path, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:120]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


check("the app started", up)
if not up:
    sys.exit(2)
tok = None
req = urllib.request.Request(
    API + "/api/auth/login", method="POST",
    data=json.dumps({"username": "admin", "password": "admin"}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        tok = (json.loads(r.read().decode()) or {}).get("token")
except Exception:
    pass

# Exactly what the client's loop does.
PAGE = 20000
seen_ids: set[int] = set()
pages = 0
offset = 0
while True:
    st, b = call("/api/app-store/historian/range?limit=%d&offset=%d&tag=T1" % (PAGE, offset), tok)
    rows = (b or {}).get("rows") or []
    pages += 1
    for r in rows:
        rid = r.get("id")
        if rid is not None:
            seen_ids.add(int(rid))
    if len(rows) < PAGE:
        break
    offset += len(rows)
    if pages > 10:
        break

check("paging reaches every row", len(seen_ids) == ROWS,
      "%s of %s row(s) in %d page(s)" % (f"{len(seen_ids):,}", f"{ROWS:,}", pages))
check("  a short page really does mean 'done'", pages == 3,
      "%d pages for %s rows at %s per page" % (pages, f"{ROWS:,}", f"{PAGE:,}"))
check("  and one request alone would have truncated it",
      ROWS > PAGE,
      "the old export made exactly one request of 20 000")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
