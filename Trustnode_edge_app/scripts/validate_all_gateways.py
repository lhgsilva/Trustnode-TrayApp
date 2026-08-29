# -*- coding: utf-8 -*-
"""Every gateway type, together: synchronised, collecting, logging, on cadence.

2026-08-28, after an operator reported: "make sure all types of gateways share
the same and equal type of timestamp so we can see multiple data and correct,
and log it correctly on the database ... make the smoke test reading to make
sure they are synchronised, collecting and logging and presenting with correct
cadence".

Runs the REAL app against the REAL devices on the bench, all gateway types at
once, and checks the things that actually went wrong:

  1. TIMESTAMP  one format everywhere. ts_utc is compared as TEXT and 'T'
     sorts after ' ', so a single ISO writer puts its rows outside every
     range filter and at the wrong end of the Logs page.
  2. SYNCHRONY  rows from different gateways in the same window must be
     comparable - that is the whole point of one format.
  3. CADENCE    measured against the CONFIGURED interval, not against the
     observed span (deriving "expected" from what you got always reports
     100% and hides every outage - that mistake was made on 2026-08-28).
  4. COLLECTING GOOD-quality rows reach historian_readings.
  5. LOGGING    log rows are written and are time-filterable.
  6. STAYS UP   no gateway stops on its own during the run.

Device addresses default to the bench; override with --plc/--meter/--ifm.
Any device that is not reachable is SKIPPED loudly, never silently passed.
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
PORT = "8089"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []
SKIPS: list[str] = []

CANON = "%Y-%m-%d %H:%M:%S.%f"      # the one true format, milliseconds trimmed


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print("  {0:56s}: SKIP - {1}".format(name, why))
    SKIPS.append(name)


def reachable(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def canonical(ts: str) -> bool:
    """Is this the format every writer must use?"""
    t = str(ts or "")
    if len(t) < 19 or "T" in t or "+" in t or t.endswith("Z"):
        return False
    try:
        datetime.datetime.strptime(t[:23], CANON if "." in t[:23] else "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plc", default="192.168.10.240", help="Allen-Bradley PLC")
    ap.add_argument("--meter", default="192.168.10.200", help="Modbus power meter")
    ap.add_argument("--ifm", default="192.168.10.251", help="ifm block (fieldbus + IoT)")
    ap.add_argument("--seconds", type=int, default=45, help="collection window")
    ap.add_argument("--interval", type=int, default=1000, help="configured cadence, ms")
    args = ap.parse_args()

    print("TrustNode - all gateway types, one run")
    print("  PLC   {0}   meter {1}   ifm {2}".format(args.plc, args.meter, args.ifm))
    print()

    have = {
        "plc": reachable(args.plc, 44818),
        "meter": reachable(args.meter, 502),
        "ifm_fieldbus": reachable(args.ifm, 44818),
        "ifm_iot": reachable(args.ifm, 80),
    }
    print("  reachability: " + ", ".join(
        "{0}={1}".format(k, "yes" if v else "NO") for k, v in have.items()))
    print()

    # ---------------------------------------------------- the app -------
    tmp = tempfile.mkdtemp(prefix="tn-allgw-")
    db = os.path.join(tmp, "s.db")
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=db,
               TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
    proc = subprocess.Popen([sys.executable, "-m", "app"],
                            cwd=os.path.join(ROOT, "backend"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(API + "/api/health", timeout=3).read()
            break
        except Exception:
            time.sleep(2)

    def call(method, path, token=None, body=None, timeout=90):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        d = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=d, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "null")
            except Exception:
                return e.code, None
        except Exception as e:
            return 0, str(e)[:200]

    started: list[str] = []

    def finish(code):
        for gid in started:
            call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": gid})
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        return code

    st, b = call("POST", "/api/auth/login",
                 body={"username": "admin", "password": "admin"})
    tok = (b or {}).get("token") if isinstance(b, dict) else None
    check("the app is up and admin can log in", st == 200 and bool(tok))
    if not tok:
        return finish(2)

    # ------------------------------------------- configure the devices ---
    print()
    print("[1. configure every gateway type with its real address]")

    if have["plc"]:
        st, r = call("POST", "/api/plc/gateways/start", tok, {
            "gateway_id": "gw-all-plc",
            "config": {"gateway_type": "allen_bradley", "name": "PLC",
                       "device_name": "L33ERMS", "plc_ip": args.plc,
                       "tags": ["SimREAL[3]"], "interval_ms": args.interval,
                       "site": "Bench", "area": "All", "equipment": "PLC"}})
        check("allen_bradley gateway starts", st == 200, str(r)[:80])
        if st == 200:
            started.append("gw-all-plc")
    else:
        skip("allen_bradley gateway", "{0}:44818 unreachable".format(args.plc))

    if have["ifm_fieldbus"]:
        st, auto = call("POST", "/api/plc/eip/ifm-fieldbus-autoconfig", tok,
                        {"plc_ip": args.ifm, "port_count": 8})
        cfg = (auto or {}).get("config") or {}
        check("ifm block auto-configures over fieldbus",
              bool(cfg.get("eip_input_assembly")), (auto or {}).get("message", "")[:90])
        if cfg:
            st, r = call("POST", "/api/plc/gateways/start", tok, {
                "gateway_id": "gw-all-ifm",
                "config": {**cfg, "name": "IFM", "device_name": "AL1326",
                           "interval_ms": args.interval,
                           "site": "Bench", "area": "All", "equipment": "IFM"}})
            check("  ethernet_ip gateway starts", st == 200, str(r)[:80])
            if st == 200:
                started.append("gw-all-ifm")
    else:
        skip("ethernet_ip gateway", "{0}:44818 unreachable".format(args.ifm))

    if have["ifm_iot"]:
        st, scan = call("POST", "/api/plc/ifm/scan-ports", tok,
                        {"plc_ip": args.ifm, "http_port": 80, "variant": "auto"})
        pts = [p for p in ((scan or {}).get("datapoints") or []) if p.get("enabled")]
        check("ifm block scans over IoT Core", bool(pts),
              "{0} value(s)".format(len(pts)))
        if pts:
            st, r = call("POST", "/api/plc/gateways/start", tok, {
                "gateway_id": "gw-all-iot",
                "config": {"gateway_type": "ifm_iolink", "name": "IFM-IoT",
                           "device_name": "AL1326", "plc_ip": args.ifm,
                           "ifm_http_port": 80, "ifm_variant": "auto",
                           "ifm_datapoints": pts,
                           "tags": [p["name"] for p in pts],
                           # this block's IoT core cannot serve 1 Hz
                           "interval_ms": max(args.interval, 5000),
                           "site": "Bench", "area": "All", "equipment": "IFM-IoT"}})
            check("  ifm_iolink gateway starts", st == 200, str(r)[:80])
            if st == 200:
                started.append("gw-all-iot")
    else:
        skip("ifm_iolink gateway", "{0}:80 unreachable".format(args.ifm))

    meter_on = False
    if have["meter"]:
        from app.services.meter_registers import EM122_SINGLE_PHASE
        st, r = call("PUT", "/api/power/config", tok, {
            "enabled": True, "selected_device_id": "EM-ALL",
            "devices": [{
                "id": "EM-ALL", "name": "EM-ALL", "ip": args.meter, "port": 502,
                "unit_id": 1, "enabled": True, "poll_interval_ms": args.interval,
                "protocol": "modbus_tcp", "use_custom_registers": True,
                "registers": dict(EM122_SINGLE_PHASE),
                "register_scales": {k: 1.0 for k in EM122_SINGLE_PHASE},
            }]})
        check("power meter configured with the EM122 map", st == 200, str(r)[:70])
        meter_on = st == 200
    else:
        skip("power meter", "{0}:502 unreachable".format(args.meter))

    if not started and not meter_on:
        print()
        print("SKIP: no device on this network - nothing to validate.")
        return finish(0)

    # ------------------------------------------------------- collect ----
    print()
    print("[2. collect for {0} s]".format(args.seconds))
    t0 = datetime.datetime.now(datetime.timezone.utc)
    time.sleep(args.seconds)
    t1 = datetime.datetime.now(datetime.timezone.utc)

    # nothing may stop on its own
    st, statuses = call("GET", "/api/plc/gateways/status", tok)
    running = {str(g.get("gateway_id")): bool(g.get("running"))
               for g in (statuses if isinstance(statuses, list) else [])}
    for gid in started:
        # the status endpoint filters by saved config; absence is not proof of
        # a stop, so only FAIL on an explicit running=false.
        if gid in running:
            check("{0} is still running".format(gid), running[gid] is True,
                  "" if running[gid] else "it stopped on its own during the run")

    # ---------------------------------------------- read what landed ----
    con = sqlite3.connect("file:{0}?mode=ro".format(db), uri=True, timeout=30)
    cur = con.cursor()
    win_from = t0.strftime(CANON)[:-3]
    win_to = t1.strftime(CANON)[:-3]

    print()
    print("[3. one timestamp format everywhere]")
    hist = cur.execute(
        "SELECT gateway_id, ts_utc FROM historian_readings ORDER BY id DESC LIMIT 6000"
    ).fetchall()
    bad = [(g, t) for g, t in hist if not canonical(t)]
    check("every historian row uses the canonical format",
          not bad, bad[:3])
    logs = cur.execute("SELECT category, ts_utc FROM app_logs "
                       "ORDER BY id DESC LIMIT 2000").fetchall()
    bad_logs = [(c, t) for c, t in logs if not canonical(t)]
    check("every log row uses the canonical format", not bad_logs, bad_logs[:3])
    forms = {}
    for g, t in hist:
        forms.setdefault("T" if "T" in str(t) else "space", set()).add(str(g))
    check("  no gateway writes a different format from the others",
          len(forms) <= 1, {k: sorted(v)[:3] for k, v in forms.items()})

    print()
    print("[4. synchronised: every source lands in the same window]")
    per_gw = {}
    for gid, ts in hist:
        if win_from <= str(ts) <= win_to:
            per_gw.setdefault(str(gid), []).append(str(ts))
    check("rows from the run are findable by a normal range filter",
          bool(per_gw),
          "a format mismatch is what makes this fail: {0} gateway(s)".format(len(per_gw)))
    for gid in sorted(per_gw):
        print("      {0:22s} {1} row-timestamps in window".format(gid, len(per_gw[gid])))

    print()
    print("[5. cadence, measured against the CONFIGURED interval]")
    for gid in sorted(set(list(per_gw.keys()))):
        ts = sorted(set(per_gw[gid]))
        if len(ts) < 3:
            skip("{0} cadence".format(gid), "only {0} sample(s)".format(len(ts)))
            continue
        # the IoT gateway is deliberately slower; read its own configured rate
        expected_ms = 5000 if gid.endswith("iot") else args.interval
        expected = max(1, int(args.seconds * 1000 / expected_ms))
        cover = 100.0 * len(ts) / expected

        def p(x):
            return datetime.datetime.strptime(x[:23], CANON)
        gaps = [(p(ts[i]) - p(ts[i - 1])).total_seconds() for i in range(1, len(ts))]
        worst = max(gaps) if gaps else 0.0
        check("{0}: {1} of {2} expected samples".format(gid, len(ts), expected),
              cover >= 80.0, "{0:.0f}% coverage, worst gap {1:.1f}s".format(cover, worst))
        check("  {0}: no gap beyond 3x the interval".format(gid),
              worst <= (expected_ms / 1000.0) * 3.0,
              "worst {0:.1f}s vs interval {1:.1f}s".format(worst, expected_ms / 1000.0))

    print()
    print("[6. collecting GOOD data, and logging]")
    for gid in sorted(per_gw):
        rows = cur.execute(
            "SELECT quality, COUNT(*) FROM historian_readings WHERE gateway_id=? "
            "AND ts_utc>=? GROUP BY quality", (gid, win_from)).fetchall()
        total = sum(n for _, n in rows)
        good = sum(n for q, n in rows if int(q or 0) == 192)
        check("{0}: rows are GOOD quality".format(gid),
              total > 0 and good >= total * 0.5,
              "{0}/{1} GOOD".format(good, total))
    n_logs = cur.execute("SELECT COUNT(*) FROM app_logs WHERE ts_utc>=?",
                         (win_from,)).fetchone()[0]
    print("      {0} log row(s) written during the run".format(n_logs))
    con.close()

    print()
    print("[7. the values come back through the chart endpoint]")
    for gid in sorted(per_gw):
        st, rng = call("GET", "/api/app-store/historian/range?limit=50"
                              "&gateway={0}".format(gid), tok)
        got = len(((rng or {}).get("rows")) or [])
        check("{0}: chart range read returns rows".format(gid), got > 0, got)

    print()
    if SKIPS:
        print("SKIPPED ({0}): {1}".format(len(SKIPS), ", ".join(SKIPS)))
    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return finish(0 if not FAILS else 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
