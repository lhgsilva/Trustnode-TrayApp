# -*- coding: utf-8 -*-
"""A REAL ifm block must collect into the historian on BOTH transports.

test_ifm_master_e2e.py drives a simulated block, which proves the pipeline but
not the hardware. This one runs the whole app against the block actually on the
bench, twice:

    1. gateway_type = ifm_iolink    (IoT Core, HTTP/JSON on port 80)
    2. gateway_type = ethernet_ip   (fieldbus, CIP explicit messaging)

and requires that BOTH land rows in the historian under the SAME tag names -
Port7_Pin4 collected over IoT and Port7_Pin4 collected over fieldbus are the
same signal, so a trend survives moving a block from one to the other.

    python scripts/test_ifm_real_block_e2e.py --host 192.168.1.250

Skips cleanly (exit 0) when no block is on the bench, so it can sit in the
release gate on machines that have no hardware attached.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8099"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def reachable(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.250")
    ap.add_argument("--iot-port", type=int, default=80)
    ap.add_argument("--assembly", type=int, default=100)
    ap.add_argument("--ports", type=int, default=8)
    args = ap.parse_args()

    print("TrustNode - real ifm block, end to end, both transports")
    print("  host: {0}".format(args.host))
    print()

    has_iot = reachable(args.host, args.iot_port)
    has_eip = reachable(args.host, 44818)
    print("  IoT Core :{0}      : {1}".format(
        args.iot_port, "reachable" if has_iot else "not reachable"))
    print("  EtherNet/IP :44818 : {0}".format(
        "reachable" if has_eip else "not reachable"))
    if not has_iot and not has_eip:
        print()
        print("SKIP: no ifm block on this machine's network - nothing to test.")
        return 0
    print()

    # --- what to collect, straight from the block --------------------------
    from app.drivers.ifm_iolink import IfmMasterClient
    from app.drivers.ethernet_ip import ifm_pin_signals

    iot_points = []
    if has_iot:
        cli = IfmMasterClient(host=args.host, port=args.iot_port, timeout_s=4.0)
        iot_points = (cli.discover_datapoints(
            variant="auto", port_count=args.ports).get("datapoints") or [])
    # Collect the pins and the block's own current - the values the operator
    # asked to trend. Diagnostics that discovery leaves switched off stay off.
    chosen = [p for p in iot_points if p.get("enabled")]

    # --- the whole app, on a throwaway workspace ---------------------------
    tmp = tempfile.mkdtemp(prefix="tn-ifmreal-")
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
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
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:400]
        except Exception as e:
            return 0, str(e)[:300]

    def finish(code):
        for gw in ("gw-real-iot", "gw-real-fieldbus"):
            call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": gw})
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        return code

    st, b = call("POST", "/api/auth/login", body={"username": "admin",
                                                  "password": "admin"})
    admin = (b or {}).get("token") if isinstance(b, dict) else None
    check("admin login", st == 200 and bool(admin))
    if not admin:
        return finish(2)

    def save_gateways(cfgs):
        """Persist gateway configs exactly as the gateway dialog does.

        /gateways/status deliberately lists only gateways that exist in the
        saved gateway_configurations document, so a worker started without one
        collects happily but never shows as Running in the UI. Saving first is
        what the real flow does.
        """
        return call("PUT", "/api/app-store/domain", admin,
                    {"domain": "gateway_configurations", "payload": cfgs,
                     "actor": "test_ifm_real_block_e2e"})

    def rows_for(gw_id):
        st, hist = call("GET", "/api/app-store/historian?limit=800", admin)
        return [x for x in (((hist or {}).get("rows")) or [])
                if str(x.get("gateway_id")) == gw_id]

    # ---------------------------------------------------- 1. IoT Core ------
    iot_tags = set()
    if has_iot:
        print("[1. IoT Core transport]")
        check("the block offered values to collect", len(chosen) >= 4, len(chosen))
        iot_cfg = {"gateway_id": "gw-real-iot", "gateway_type": "ifm_iolink",
                   "name": "ifm over IoT", "device_name": "ifm block",
                   "plc_ip": args.host, "ifm_http_port": args.iot_port,
                   "ifm_variant": "auto", "ifm_datapoints": chosen,
                   "tags": [d["name"] for d in chosen], "interval_ms": 1000,
                   "site": "Bench", "area": "IFM", "equipment": "Block",
                   "enabled": True}
        st, _ = save_gateways([iot_cfg])
        check("the gateway config saves", st == 200, st)
        st, r = call("POST", "/api/plc/gateways/start", admin, {
            "gateway_id": "gw-real-iot",
            "config": {"gateway_type": "ifm_iolink", "name": "ifm over IoT",
                       "device_name": "ifm block", "plc_ip": args.host,
                       "ifm_http_port": args.iot_port, "ifm_variant": "auto",
                       "ifm_datapoints": chosen,
                       "tags": [d["name"] for d in chosen],
                       "interval_ms": 1000, "site": "Bench", "area": "IFM",
                       "equipment": "Block"}})
        check("the gateway starts", st == 200,
              "status={0} {1}".format(st, str(r)[:140]))
        time.sleep(10)
        rows = rows_for("gw-real-iot")
        check("readings reach the historian", len(rows) > 0, "{0} row(s)".format(len(rows)))
        iot_tags = set(str(x.get("tag")) for x in rows)
        check("  both pins of a port are trended",
              "Port7_Pin4" in iot_tags and "Port7_Pin2" in iot_tags,
              sorted(t for t in iot_tags if t.startswith("Port7")))
        check("  the block's own current draw is trended",
              "Master_Current" in iot_tags,
              sorted(t for t in iot_tags if t.startswith("Master")))
        st, gws = call("GET", "/api/plc/gateways/status", admin)
        listing = gws if isinstance(gws, list) else []
        entry = next((g for g in listing
                      if str(g.get("gateway_id")) == "gw-real-iot"), {})
        check("  the gateway reports itself running",
              bool(entry.get("running")),
              "running={0} last_error={1}".format(
                  entry.get("running"), entry.get("last_error") or "none")
              if entry else "not in /gateways/status")
        check("  with no read-timeout error from priming",
              "exceeded" not in str(entry.get("last_error") or ""),
              entry.get("last_error") or "none")
        call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-real-iot"})
        print()

    # ---------------------------------------------------- 2. fieldbus ------
    fb_tags = set()
    if has_eip:
        print("[2. fieldbus (EtherNet/IP) transport]")
        st, pm = call("POST", "/api/plc/eip/ifm-pin-map", admin,
                      {"plc_ip": args.host, "instance": args.assembly,
                       "port_count": args.ports, "verify": True})
        signals = (pm or {}).get("signals") or []
        check("the pin map builds from the block's assembly",
              st == 200 and len(signals) == args.ports * 2,
              (pm or {}).get("message"))
        check("  and verifies live against the device",
              bool((pm or {}).get("values")), (pm or {}).get("message"))

        fb_cfg = {"gateway_id": "gw-real-fieldbus", "gateway_type": "ethernet_ip",
                  "name": "ifm over fieldbus", "device_name": "ifm block",
                  "plc_ip": args.host, "eip_input_assembly": args.assembly,
                  "eip_slot": 0, "eip_signals": signals,
                  "tags": [s2["name"] for s2 in signals], "interval_ms": 1000,
                  "site": "Bench", "area": "IFM", "equipment": "BlockFB",
                  "enabled": True}
        st, _ = save_gateways(([iot_cfg] if has_iot else []) + [fb_cfg])
        check("the gateway config saves", st == 200, st)
        st, r = call("POST", "/api/plc/gateways/start", admin, {
            "gateway_id": "gw-real-fieldbus",
            "config": {"gateway_type": "ethernet_ip", "name": "ifm over fieldbus",
                       "device_name": "ifm block", "plc_ip": args.host,
                       "eip_input_assembly": args.assembly, "eip_slot": 0,
                       "eip_signals": signals,
                       "tags": [s["name"] for s in signals],
                       "interval_ms": 1000, "site": "Bench", "area": "IFM",
                       "equipment": "BlockFB"}})
        check("the gateway starts", st == 200,
              "status={0} {1}".format(st, str(r)[:140]))
        time.sleep(10)
        rows = rows_for("gw-real-fieldbus")
        check("readings reach the historian", len(rows) > 0,
              "{0} row(s)".format(len(rows)))
        fb_tags = set(str(x.get("tag")) for x in rows)
        check("  both pins of a port are trended",
              "Port7_Pin4" in fb_tags and "Port7_Pin2" in fb_tags,
              sorted(t for t in fb_tags if t.startswith("Port7")))
        st, gws = call("GET", "/api/plc/gateways/status", admin)
        listing = gws if isinstance(gws, list) else []
        entry = next((g for g in listing
                      if str(g.get("gateway_id")) == "gw-real-fieldbus"), {})
        check("  the gateway reports itself running",
              bool(entry.get("running")),
              "running={0} last_error={1}".format(
                  entry.get("running"), entry.get("last_error") or "none")
              if entry else "not in /gateways/status")
        check("  with no read-timeout error from priming",
              "exceeded" not in str(entry.get("last_error") or ""),
              entry.get("last_error") or "none")
        call("POST", "/api/plc/gateways/stop", admin,
             {"gateway_id": "gw-real-fieldbus"})
        print()

    # ---------------------------------------------- 3. they must agree -----
    if has_iot and has_eip:
        print("[3. the same block, the same tag names, either way]")
        shared = iot_tags & fb_tags
        pins = {t for t in shared if t.endswith(("_Pin2", "_Pin4"))}
        check("both transports trend the same pin tags",
              len(pins) == args.ports * 2,
              "{0} shared pin tag(s)".format(len(pins)))

    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return finish(0 if not FAILS else 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
