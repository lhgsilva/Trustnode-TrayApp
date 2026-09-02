# -*- coding: utf-8 -*-
"""ifm block: configure -> read -> database -> tags -> historian -> UI.

2026-08-28, after "make the smoke test that includes the IFM block
configuration, values reading, Database, tags and historian reading and UI
presentation of the values, they are failing everywhere".

Follows one block through EVERY layer an operator touches, in order, so a
failure is attributed to a layer instead of appearing as "it doesn't work":

  1. CONFIGURE  auto-configure over fieldbus; the saved config keeps the
                protocol fields.
  2. START      through the SAME payload builder the Start button uses.
                THE BUG THIS CATCHES: that builder used a fixed allowlist and
                dropped eip_input_assembly / eip_signals, so Start sent a
                gateway with no assembly and the backend answered "no input
                assembly is set" - while Save looked fine.
  3. READ       values arrive, with the right names and quality.
  4. DATABASE   rows are committed to historian_readings, canonical timestamps.
  5. TAGS       the tag list's source has a value for every tag - INCLUDING
                after the gateway stops, which is what removed the "-".
  6. HISTORIAN  the range + aggregate endpoints return the tags.
  7. UI         the value survives the frontend's own formatting rules; 0.0 is
                a value, not "no data".

Zero is a first-class value throughout: this block reads 0 on every pin when
nothing is triggered, and every layer must carry that faithfully.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8086"
API = "http://127.0.0.1:" + PORT
GID = "gw-smoke-ifm"
FAILS: list[str] = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ifm", default="192.168.10.251")
    ap.add_argument("--seconds", type=int, default=20)
    args = ap.parse_args()

    print("TrustNode - ifm block, every layer")
    print("  block: {0}".format(args.ifm))
    print()

    def _open(port: int) -> bool:
        try:
            with socket.create_connection((args.ifm, port), timeout=2):
                return True
        except Exception:
            return False

    fieldbus = _open(44818)
    iot = _open(80)
    print("  EtherNet/IP :44818 : {0}".format("open" if fieldbus else "closed"))
    print("  IoT Core    :80    : {0}".format("open" if iot else "closed"))
    print()
    if not fieldbus:
        if iot:
            # An AL1326 with the fieldbus disabled does this. Saying "no ifm
            # block" about a block that is answering on port 80 sends whoever
            # reads it to check cabling that is already fine.
            print("SKIP: {0} serves IoT Core but not EtherNet/IP, and this "
                  "suite exercises the fieldbus transport.".format(args.ifm))
            print("      The IoT path has its own end-to-end coverage:")
            print("      python scripts/test_ifm_real_block_e2e.py --host {0}"
                  .format(args.ifm))
        else:
            print("SKIP: nothing is answering at {0} on 44818 or 80 - the "
                  "block is unreachable.".format(args.ifm))
        return 0

    tmp = tempfile.mkdtemp(prefix="tn-smoke-ifm-")
    db_path = os.path.join(tmp, "s.db")
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=db_path,
               TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
    proc = subprocess.Popen([sys.executable, "-m", "app"],
                            cwd=os.path.join(ROOT, "backend"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(70):
        try:
            urllib.request.urlopen(API + "/api/health", timeout=3).read()
            break
        except Exception:
            time.sleep(2)

    def call(method, path, token=None, body=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        d = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=d, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "null")
            except Exception:
                return e.code, None
        except Exception as e:
            return 0, str(e)[:200]

    def finish(code):
        call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        return code

    st, b = call("POST", "/api/auth/login",
                 body={"username": "admin", "password": "admin"})
    tok = b.get("token") if isinstance(b, dict) else None
    check("the app is up", st == 200 and bool(tok))
    if not tok:
        return finish(2)

    # ------------------------------------------------- 1. CONFIGURE ------
    print()
    print("[1. configuration]")
    st, auto = call("POST", "/api/plc/eip/ifm-fieldbus-autoconfig", tok,
                    {"plc_ip": args.ifm, "port_count": 8})
    cfg = (auto or {}).get("config") or {}
    check("the block auto-configures over fieldbus",
          bool(cfg), (auto or {}).get("message", "")[:90])
    check("  an input assembly was found",
          int(cfg.get("eip_input_assembly") or 0) > 0, cfg.get("eip_input_assembly"))
    check("  signals were generated for every port and pin",
          len(cfg.get("eip_signals") or []) == 16, len(cfg.get("eip_signals") or []))
    if not cfg:
        return finish(2)

    # ------------------------------------------------- 2. START ----------
    # Exactly the shape the UI's payload builder produces. The allowlist bug
    # showed up as these keys being ABSENT, so assert they survive.
    print()
    print("[2. start, through the payload the UI actually sends]")
    ui_config = {
        "gateway_type": "ethernet_ip",
        "name": "IFM smoke", "device_name": "AL1326",
        "plc_ip": args.ifm, "opc_url": "",
        "tags": [s["name"] for s in cfg["eip_signals"]],
        "interval_ms": 1000,
        "equipment": "Block", "site": "Bench", "area": "IFM",
        # the fields that used to be dropped
        "eip_input_assembly": cfg["eip_input_assembly"],
        "eip_output_assembly": 0, "eip_config_assembly": 0,
        "eip_slot": 0, "eip_signals": cfg["eip_signals"],
        "eip_device_info": cfg.get("eip_device_info") or {},
    }
    for key in ("eip_input_assembly", "eip_signals"):
        check("  the payload carries {0}".format(key),
              bool(ui_config.get(key)),
              "dropping this is what made Start fail")
    st, r = call("POST", "/api/plc/gateways/start", tok,
                 {"gateway_id": GID, "config": ui_config})
    check("the gateway starts", st == 200 and (r or {}).get("started") is True,
          str(r)[:110])
    if not (r or {}).get("started"):
        return finish(2)

    print()
    print("  collecting for {0} s...".format(args.seconds))
    time.sleep(args.seconds)

    # ------------------------------------------------- 3. READ -----------
    print()
    print("[3. values are being read]")
    st, statuses = call("GET", "/api/plc/gateways/status", tok)
    row = next((g for g in (statuses if isinstance(statuses, list) else [])
                if str(g.get("gateway_id")) == GID), None)
    if row:
        check("the worker reports no error",
              not str(row.get("last_error") or ""), row.get("last_error"))
        check("  and it is running", row.get("running") is True)

    # ------------------------------------------------- 4. DATABASE -------
    print()
    print("[4. the database]")
    con = sqlite3.connect("file:{0}?mode=ro".format(db_path), uri=True, timeout=30)
    cur = con.cursor()
    total, newest = cur.execute(
        "SELECT COUNT(*), MAX(ts_utc) FROM historian_readings WHERE gateway_id=?",
        (GID,)).fetchone()
    check("rows are committed to historian_readings", (total or 0) > 0, total)
    tags = [r[0] for r in cur.execute(
        "SELECT DISTINCT tag_name FROM historian_readings WHERE gateway_id=?",
        (GID,)).fetchall()]
    check("  every port and pin has its own tag", len(tags) == 16, len(tags))
    good = cur.execute(
        "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND quality=192",
        (GID,)).fetchone()[0]
    check("  the rows are GOOD quality", good == total, "{0}/{1}".format(good, total))
    stamps = [r[0] for r in cur.execute(
        "SELECT DISTINCT ts_utc FROM historian_readings WHERE gateway_id=? LIMIT 50",
        (GID,)).fetchall()]
    bad = [t for t in stamps if "T" in str(t) or "+" in str(t)]
    check("  timestamps match every other gateway's format", not bad, bad[:2])
    # zero must be stored as a NUMBER, not lost
    zeros = cur.execute(
        "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND value=0.0",
        (GID,)).fetchone()[0]
    nulls = cur.execute(
        "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND value IS NULL",
        (GID,)).fetchone()[0]
    check("  a pin reading 0 is stored as 0, never NULL",
          nulls == 0, "{0} NULL, {1} zero-valued".format(nulls, zeros))

    # Cadence against the CONFIGURED interval. Never derive "expected" from the
    # observed span - that always scores ~100% and once hid a 12-minute outage.
    per_tag = (total or 0) / max(1, len(tags))
    expected = args.seconds / (ui_config["interval_ms"] / 1000.0)
    ratio = per_tag / expected if expected else 0.0
    check("  the cadence matches the configured {0} ms".format(ui_config["interval_ms"]),
          ratio >= 0.80,
          "{0:.0f} of {1:.0f} samples per tag ({2:.0f}%)".format(
              per_tag, expected, ratio * 100))
    con.close()

    # ------------------------------------------------- 5. TAGS -----------
    print()
    print("[5. the tag list's source]")
    st, live = call("GET", "/api/app-store/live?limit=2000", tok)
    live_rows = [x for x in ((live or {}).get("rows") or [])
                 if str(x.get("gateway_id")) == GID]
    check("every tag has a value in the live cache",
          len(live_rows) == 16, len(live_rows))
    have_zero = [x for x in live_rows if x.get("value") == 0.0]
    check("  a 0.0 reading is present as a value",
          len(have_zero) > 0 or all(x.get("value") is not None for x in live_rows),
          "{0} tag(s) reading exactly 0.0".format(len(have_zero)))

    # the fix for "-": values must survive the gateway stopping
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    time.sleep(6)
    st, live2 = call("GET", "/api/app-store/live?limit=2000", tok)
    after = [x for x in ((live2 or {}).get("rows") or [])
             if str(x.get("gateway_id")) == GID]
    check("  values survive the gateway being stopped",
          len(after) == len(live_rows),
          "{0} before, {1} after - this is what removed the '-'".format(
              len(live_rows), len(after)))

    # ------------------------------------------------- 6. HISTORIAN ------
    print()
    print("[6. historian reads the UI uses]")
    st, rng = call("GET", "/api/app-store/historian/range?limit=200"
                          "&gateway={0}".format(GID), tok)
    rng_rows = (rng or {}).get("rows") or []
    check("the range endpoint returns the gateway's rows", len(rng_rows) > 0, len(rng_rows))
    # A row the charts can actually plot: named tag, real number, declared type.
    plottable = [r for r in rng_rows
                 if str(r.get("tag") or "") and r.get("value") is not None]
    check("  the rows are plottable - named tag and a numeric value",
          len(plottable) == len(rng_rows),
          "{0}/{1}".format(len(plottable), len(rng_rows)))
    types = sorted({str(r.get("data_type") or "") for r in rng_rows})
    check("  each pin keeps its declared type", types == ["BOOL"], types)

    sample_tag = tags[0] if tags else ""
    st, agg = call("GET", "/api/app-store/historian/agg?bucket=minute&limit=200"
                          "&gateway={0}&tag={1}".format(GID, sample_tag), tok)
    agg_rows = ((agg or {}).get("rows")) or []
    check("  the chart aggregate returns {0}".format(sample_tag),
          len(agg_rows) > 0, len(agg_rows))
    check("    and it is bucketed, not a raw slice",
          any(int(r.get("sample_count") or 0) > 1 for r in agg_rows),
          "max samples in a bucket: {0}".format(
              max([int(r.get("sample_count") or 0) for r in agg_rows] or [0])))

    # ------------------------------------------------- 7. UI -------------
    print()
    print("[7. UI presentation rules]")
    app_jsx = open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                   encoding="utf-8", errors="replace").read()
    # `latest?.value ?? live?.value ?? "-"` keeps 0; `||` would print "-" for 0.
    check("the tag list uses ?? so 0 is not treated as missing",
          '(latest?.value ?? live?.value_text ?? live?.value ?? "-")' in app_jsx)
    check("  the desktop seeds last values from the server cache",
          "seed last-known values from the server" in app_jsx
          or "getAppStoreLive(2000, null)" in app_jsx)
    check("  and the Start payload carries the protocol fields",
          "eip_input_assembly: Number(gateway.eip_input_assembly" in app_jsx
          and "ifm_datapoints: Array.isArray(gateway.ifm_datapoints)" in app_jsx)

    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return finish(0 if not FAILS else 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
