# -*- coding: utf-8 -*-
"""CSV and TXT exports must actually contain rows - filtered or not.

2026-08-30, reported: "the parallel database logging using text and csv files
is not loading the data anymore."

The sinks themselves were fine. `_filter_readings_for_sink` resolved the
current gateway as

    gateway_id = self.config.gateway_id or self.config.id

and GatewayConfig carries NEITHER field - the id belongs to the WORKER and is
passed separately to start_gateway(). So it was always "", and a sink with a
`gateway_filters` list could never match: the filter returned [] and the export
silently wrote nothing. No error, no warning, an empty file.

That filter is exactly what the UI sets when a CSV/TXT connection is scoped to
a gateway, which is why this looked like "file logging stopped working".

Covers both shapes an operator can configure:
  * the gateway's PRIMARY sink is the file, and
  * the file is a PARALLEL sink beside the historian,
each with and without a gateway filter.

Needs no hardware: a gateway pointed at TEST-NET-2 still produces BAD readings
every cycle, and a BAD reading is still a row an export must contain.
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
PORT = "8162"
API = "http://127.0.0.1:" + PORT
GID = "gw-file-sink"
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[the filter compares the worker's id, not a field that does not exist]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "plc_manager.py"),
              encoding="utf-8", errors="replace").read()
blk = src.split("def _filter_readings_for_sink", 1)[-1][:2200]
check("the gateway id comes from the worker",
      'getattr(self, "gateway_id"' in blk,
      "GatewayConfig has no gateway_id/id - reading it there yielded \"\" forever")

tmp = tempfile.mkdtemp(prefix="tn-sinks-")
out = os.path.join(tmp, "out")
os.makedirs(out, exist_ok=True)
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(70):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print()
print("[a CSV and a TXT export, with and without a gateway filter]")
tok = None
check("the app started", up)
if not up:
    sys.exit(2)
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")


def run_case(label, filtered):
    """Collect for a few seconds and report how many rows each file got."""
    csv_path = os.path.join(out, "%s.csv" % label)
    txt_path = os.path.join(out, "%s.txt" % label)
    gw_filter = [GID] if filtered else []
    csv_sink = {"id": "sink-csv-%s" % label, "name": "CSV", "engine": "csv_file",
                "enabled": True, "use_gateway": True, "file_path": csv_path,
                "gateway_filters": gw_filter, "tag_filters": []}
    txt_sink = {"id": "sink-txt-%s" % label, "name": "TXT", "engine": "txt_file",
                "enabled": True, "use_gateway": True, "file_path": txt_path,
                "gateway_filters": gw_filter, "tag_filters": []}
    # EtherNet/IP, not Modbus: the Modbus driver RAISES on a failed connect, so
    # a cycle against an unreachable address produces no readings at all and
    # there is nothing for an export to contain. The EtherNet/IP reader returns
    # a BAD reading per signal instead - still rows, which is what this test
    # needs to see land in a file.
    # 127.0.0.1 with nothing listening, NOT a black-hole address. A TEST-NET
    # target drops packets, so every cycle waits out a ~5 s TCP timeout: the
    # 12 s window then yields one row where the check wants two, and the test
    # fails on timeout luck rather than on the behaviour it is testing. A
    # refused connection fails instantly, so the reads are BAD immediately and
    # the rows - which is what an export must contain - arrive at the
    # configured cadence.
    cfg = {"gateway_type": "ethernet_ip", "name": "sink probe", "device_name": "T",
           "plc_ip": "127.0.0.1", "eip_input_assembly": 100, "eip_slot": 0,
           "eip_signals": [{"name": "R1", "byte_offset": 0, "kind": "BOOL", "bit": 0}],
           "tags": ["R1"], "interval_ms": 1000,
           "site": "T", "area": "T", "equipment": "T",
           "database_id": csv_sink["id"]}
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    st, r = call("POST", "/api/plc/gateways/start", tok, {
        "gateway_id": GID, "config": cfg,
        # PRIMARY = the CSV file; the TXT file rides alongside as a parallel
        # sink. Both shapes an operator can configure, in one run.
        "db_sink": csv_sink, "db_sinks": [csv_sink, txt_sink]})
    if (r or {}).get("started") is not True:
        return None, None, str(r)[:80]
    time.sleep(12)
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    time.sleep(1)

    def lines(path):
        if not os.path.exists(path):
            return 0
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for ln in fh if ln.strip())
    return lines(csv_path), lines(txt_path), None


for label, filtered in (("nofilter", False), ("filtered", True)):
    csv_n, txt_n, err = run_case(label, filtered)
    tag = "with a gateway filter" if filtered else "with no filter"
    if err:
        check("the gateway starts (%s)" % tag, False, err)
        continue
    check("CSV export has rows %s" % tag, (csv_n or 0) >= 2,
          "%s line(s)" % csv_n)
    check("TXT export has rows %s" % tag, (txt_n or 0) >= 1,
          "%s line(s)" % txt_n)

# A filter that names a DIFFERENT gateway must still exclude - the fix must not
# turn the feature off.
csv_path = os.path.join(out, "other.csv")
other = {"id": "sink-other", "name": "CSV", "engine": "csv_file", "enabled": True,
         "use_gateway": True, "file_path": csv_path,
         "gateway_filters": ["some-other-gateway"], "tag_filters": []}
cfg = {"gateway_type": "ethernet_ip", "name": "sink probe", "device_name": "T",
       "plc_ip": "127.0.0.1", "eip_input_assembly": 100, "eip_slot": 0,
       "eip_signals": [{"name": "R1", "byte_offset": 0, "kind": "BOOL", "bit": 0}],
       "tags": ["R1"], "interval_ms": 1000, "site": "T", "area": "T",
       "equipment": "T", "database_id": "local-sqlite-default"}
call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": GID, "config": cfg,
    "db_sink": {"id": "local-sqlite-default", "engine": "sqlite",
                "sqlite_path": os.path.join(tmp, "s.db"), "table": "plc_readings"},
    "db_sinks": [other]})
time.sleep(10)
call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
check("a filter naming another gateway still excludes",
      not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0,
      "the fix must not disable the filter, only make it comparable")

# 2026-08-31: "we also should not have the metadata automatically loaded or
# hard coded". A sink that specifies nothing used to emit eleven columns with a
# header, site/area/equipment among them.
src = io.open(os.path.join(ROOT, "backend", "app", "services", "plc_manager.py"),
              encoding="utf-8", errors="replace").read()
def _fn_body(text, name):
    """Just that function, so a neighbour's code cannot answer for it."""
    start = text.find("def %s" % name)
    if start < 0:
        return ""
    nxt = text.find("\n    def ", start + 10)
    return text[start:nxt if nxt > 0 else start + 4000]

csv_body = _fn_body(src, "_persist_csv_file_for_sink")
txt_body = _fn_body(src, "_persist_txt_file_for_sink")
# A SQL sink writing site/area into its own table columns is fine - those are
# fields of a row, chosen by the schema. A FILE export is what the operator
# objected to carrying them unasked.
check("the CSV file writer hardcodes no plant metadata",
      "r.site" not in csv_body and "r.equipment" not in csv_body,
      csv_body[:0] or "site/area/equipment belong in a column list")
check("the TXT file writer hardcodes no plant metadata",
      "{r.site}" not in txt_body and "r.equipment" not in txt_body)
check("both sinks share one column rule",
      src.count("self._sink_columns(sink)") == 2,
      "two writers with two ideas of a row is how they drift")
check("the default carries no plant taxonomy",
      'DEFAULT_SINK_COLUMNS = ("ts_local", "tag_name", "value", "quality_label")' in src)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
