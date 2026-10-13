# -*- coding: utf-8 -*-
"""Two faults found by reading the operator's own machine, 2026-10-08.

Reported: "multiple menssages with problems with database connection or
gateways", and "we should also be able to read the reports form the webview
from the lite version and web version that why is important we ship
everything to the cloud vps".

1. THE LOG FLOOD. app_logs held 1,589,193 rows. One message - "Power meter
   EM1 read failed: Unable to connect" - was written every second, 3,600
   rows an hour, 35% of the table; a four-day Supabase outage in September
   added 1,027,988 copies of "Data sync failed: connection failed". A
   failure that repeats every second is ONE fault, not 3,600 events an
   hour, and burying every other message is how a real one gets missed.

2. REPORTS THAT ONLY EXIST IN THE CLOUD. Every report since 2026-10-01 is
   uploaded (storage_path set), but both download routes served
   record["file_path"] from local disk and returned 410 when it was absent -
   which is the normal state for a cloud/web client. Shipped up, no way
   down.

Scratch store only; the live one is never opened.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-flood-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
                  TRUSTNODE_LOG_REPEAT_WINDOW_S="2")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


import sqlite3                                              # noqa: E402
from app.state import app_store                             # noqa: E402

DB = os.environ["TRUSTNODE_APP_STORE_PATH"]
MSG = "Power meter EM1 read failed: Unable to connect"


def emit(msg=MSG, level="warning"):
    app_store.append_log_rows([{
        "level": level, "category": "power_management", "message": msg,
        "gateway_id": "EM1", "gateway_name": "EM1"}])


def count(where="1=1"):
    con = sqlite3.connect(DB)
    try:
        return con.execute("SELECT COUNT(*) FROM app_logs WHERE " + where).fetchone()[0]
    finally:
        con.close()


print("TrustNode - a repeating failure is one fault, not a flood")
print()

print("[one hour of the real meter, at one failure per second]")
for _ in range(3600):
    emit()
n1 = count("message LIKE 'Power meter EM1%'")
check("3600 identical failures write ONE row", n1 == 1,
      "%d row(s) - the live store took 3,600 an hour" % n1)

time.sleep(2.1)
emit()
n2 = count("message LIKE 'Power meter EM1%'")
con = sqlite3.connect(DB)
last = con.execute("SELECT message FROM app_logs WHERE message LIKE 'Power meter EM1%'"
                   " ORDER BY id DESC LIMIT 1").fetchone()[0]
con.close()
check("  once the window passes it reports the count", n2 == 2 and "repeated" in last,
      last[:92])
check("  so a still-failing device is never hidden", "3599" in last,
      "the first line is written immediately; this is the follow-up")

print()
print("[what must NOT be collapsed]")
emit("Power meter EM1 read failed: Read failed for all configured registers")
check("a DIFFERENT message still gets through",
      count("message LIKE '%all configured registers%'") == 1)
emit("Gateway gw-1 database write failed", "error")
check("  a different category/level too",
      count("level='error'") == 1)
for _ in range(5):
    emit("routine status", "info")
check("  info is never throttled", count("level='info'") == 5,
      "low-volume by nature; a missing info line would be a surprise")

print()
print("[reports that live only in the cloud]")
up = io.open(os.path.join(ROOT, "backend", "app", "services",
                          "reports_cloud_uploader.py"),
             encoding="utf-8", errors="replace").read()
check("there is a way to read an uploaded report back",
      "def fetch_pdf_from_storage(" in up,
      "there was an upload and no download")

for label, rel in (("the web/desktop route", ("backend", "app", "routers", "reports.py")),
                   ("the Lite route", ("backend", "app", "routers", "lite_local.py"))):
    src = io.open(os.path.join(ROOT, *rel), encoding="utf-8", errors="replace").read()
    check("%s falls back to the cloud copy" % label,
          "fetch_pdf_from_storage" in src,
          "a cloud client never has the edge's filesystem")
    check("  and only 410s when there is genuinely nothing",
          "has no cloud copy" in src,
          '"no longer on disk" was returned even when a copy existed')

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
