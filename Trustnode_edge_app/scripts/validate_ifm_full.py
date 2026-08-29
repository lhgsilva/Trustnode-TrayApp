# -*- coding: utf-8 -*-
"""FULL ifm validation: both module types, both transports, end to end.

Answers, with evidence rather than assertion:

  1. PORTS   - does each gateway type talk on the right TCP port?
               ifm_iolink  -> HTTP/JSON on 80 (or the configured ifm_http_port)
               ethernet_ip -> CIP explicit on 44818
               discovery   -> ListIdentity, UDP 44818
  2. TYPES   - STANDARD I/O module (AL40xx: /io/port[N]/pinX/digital_input)
               and MASTER (AL1xxx: /iolinkmaster/port[N]/...) both work.
  3. PER PORT- every port of every block is read, not just the first.
  4. AGREE   - the SAME master read over IoT and over CIP gives the same values.
  5. STORE   - readings reach historian_readings with the right tag, quality
               and timestamp format.
  6. CHART   - the stored rows come back through the endpoint the dashboard
               charts actually call (/api/app-store/historian/agg), as a usable
               series.

Real hardware is used when it is on the network; the standard I/O module is
exercised against a simulator because no AL40xx is on this bench, and the
report says so rather than implying hardware coverage it does not have.

    python scripts/validate_ifm_full.py
    python scripts/validate_ifm_full.py --master 192.168.10.251 --assembly 100
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8093"
API = "http://127.0.0.1:" + PORT
FAILS = []
SKIPS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print("  {0:58s}: SKIP - {1}".format(name, why))
    SKIPS.append("{0} ({1})".format(name, why))


def tcp_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


# ======================================================= a simulated AL40xx
# The STANDARD (non-master) shape: /io/port[N]/pinX/digital_input, and NO
# iolinkmaster branch - that absence is what identifies the variant.
# Flipped by the test to simulate older firmware that has no `querytree`, so
# discovery must fall back to walking gettree.
STD_STATE = {"querytree": True}
STD_INPUTS = {}
for _p in range(1, 9):
    STD_INPUTS["/io/port[{0}]/pin2/digital_input".format(_p)] = 1 if _p % 2 else 0
    STD_INPUTS["/io/port[{0}]/pin4/digital_input".format(_p)] = 0 if _p % 2 else 1


def _std_tree():
    ports = []
    for p in range(1, 9):
        pins = []
        for pin in ("pin2", "pin4"):
            pins.append({"identifier": pin, "type": "structure", "subs": [
                {"identifier": "digital_input", "type": "data",
                 "profiles": ["processdata"],
                 "subs": [{"identifier": "getdata", "type": "service"}]}]})
        ports.append({"identifier": "port[{0}]".format(p), "type": "structure",
                      "subs": pins})
    return {"identifier": "00-02-01-AA-80-94", "type": "device",
            "subs": [{"identifier": "getdatamulti", "type": "service"},
                     {"identifier": "querytree", "type": "service"},
                     {"identifier": "io", "type": "structure", "subs": ports}]}


class FakeStandardModule(BaseHTTPRequestHandler):
    """Behaves like the real hardware, including the getdatamulti address form:
    it answers the NODE address and OMITS anything still ending in /getdata."""

    def log_message(self, *a):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _value(self, node):
        if node in STD_INPUTS:
            return STD_INPUTS[node], 200
        if "productcode" in node:
            return "AL4022", 200
        if "serialnumber" in node:
            return "000900112233", 200
        return None, 404

    def do_GET(self):
        node = self.path[:-len("/getdata")] if self.path.endswith("/getdata") else self.path
        value, code = self._value(node)
        self._send({"cid": 1, "data": {"value": value}, "code": code})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode() or "{}")
        adr = body.get("adr")
        if adr == "gettree":
            self._send({"cid": -1, "code": 200, "data": _std_tree()})
            return
        # The driver sends "querytree" without a leading slash; accept both so
        # this fake cannot pass a driver that sends the wrong one.
        if adr in ("querytree", "/querytree"):
            if not STD_STATE.get("querytree", True):
                self._send({"cid": 1, "code": 400}, status=400)
                return
            profile = ((body.get("data") or {}).get("profile") or "")
            if profile == "processdata":
                self._send({"cid": 1, "code": 200,
                            "data": {"adrList": sorted(STD_INPUTS)}})
                return
            self._send({"cid": 1, "code": 200, "data": {"adrList": []}})
            return
        if adr == "/getdatamulti":
            out = {}
            for a in (body.get("data") or {}).get("datatosend") or []:
                if a.endswith("/getdata"):
                    continue          # the real block omits these
                v, c = self._value(a)
                out[a] = {"data": v, "code": c}
            self._send({"cid": 1, "code": 200, "data": out})
            return
        self._send({"cid": 1, "code": 400}, status=400)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="192.168.10.251")
    ap.add_argument("--iot-port", type=int, default=80)
    ap.add_argument("--assembly", type=int, default=100)
    ap.add_argument("--ports", type=int, default=8)
    args = ap.parse_args()

    from app.drivers.ifm_iolink import (
        IfmMasterClient, datapoints_from_config, VARIANT_IOLINK_MASTER,
        VARIANT_IO_MODULE, DEFAULT_TIMEOUT_S)
    from app.drivers.ethernet_ip import (
        EipDeviceClient, EipSignal, decode_signal, ifm_pin_signals,
        DEFAULT_CIP_PORT)

    print("TrustNode - full ifm validation")
    print()

    # =================================================== 1. the port matrix
    print("[1. each gateway type must use the right port]")
    check("the EtherNet/IP driver targets CIP port 44818",
          DEFAULT_CIP_PORT == 44818, DEFAULT_CIP_PORT)
    cli_default = IfmMasterClient(host="127.0.0.1")
    check("the ifm IoT driver defaults to HTTP port 80",
          cli_default.port == 80, cli_default.port)
    check("  and the IoT port is configurable per gateway",
          IfmMasterClient(host="127.0.0.1", port=8080).port == 8080)
    check("  IoT uses http:// by default, https:// only when asked",
          cli_default._base().startswith("http://")
          and IfmMasterClient(host="1.2.3.4", use_https=True)._base().startswith("https://"),
          cli_default._base())

    has_master_iot = tcp_open(args.master, args.iot_port)
    has_master_cip = tcp_open(args.master, 44818)
    print("      bench: {0} IoT:{1}={2} CIP:44818={3}".format(
        args.master, args.iot_port,
        "open" if has_master_iot else "closed",
        "open" if has_master_cip else "closed"))

    # ================================ 2. STANDARD (non-master) I/O module
    print()
    print("[2. STANDARD I/O module (non-master)]")
    srv = HTTPServer(("127.0.0.1", 0), FakeStandardModule)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    std_host, std_port = srv.server_address
    print("      simulated AL40xx on 127.0.0.1:{0} "
          "(no such hardware on this bench)".format(std_port))

    std = IfmMasterClient(host=std_host, port=std_port, timeout_s=4.0)
    variant = std.detect_variant()
    check("it is detected as an I/O MODULE, not a master",
          variant == VARIANT_IO_MODULE, variant)
    found = std.discover_datapoints(variant=variant, port_count=args.ports)
    pts = found.get("datapoints") or []
    names = {p["name"] for p in pts}
    check("  discovery finds its inputs", len(pts) >= 16, len(pts))
    missing = [n for p in range(1, args.ports + 1)
               for n in ("Port{0}_Pin2".format(p), "Port{0}_Pin4".format(p))
               if n not in names]
    check("  EVERY port and pin is offered (8 ports x 2 pins)",
          not missing, missing[:6])
    check("  each input is typed as a digital input",
          all(p.get("channel") == "DI" for p in pts
              if p["name"].startswith("Port")), "channels differ")

    std.begin_read(4.0)
    rows = std.read_datapoints(datapoints_from_config([dict(p) for p in pts]))
    std.end_read()
    good = [r for r in rows if r.get("quality") == 192]
    check("  every input reads GOOD", len(good) == len(rows),
          "{0}/{1}".format(len(good), len(rows)))
    by_name = {r["name"]: r["value"] for r in good}
    expected_ok = all(
        by_name.get("Port{0}_Pin2".format(p)) == (1.0 if p % 2 else 0.0)
        for p in range(1, args.ports + 1))
    check("  each port returns ITS OWN value, not port 1's", expected_ok,
          {k: by_name.get(k) for k in sorted(by_name)[:4]})
    # --- the SAME module, on firmware with no querytree -------------------
    # This is the fallback path, and it was broken until 2026-08-27: walking
    # gettree from its root prefixed every address with the device's MAC, so
    # every input read 404 and stayed permanently BAD. The querytree path above
    # hid it, which is why this second pass exists.
    STD_STATE["querytree"] = False
    std2 = IfmMasterClient(host=std_host, port=std_port, timeout_s=4.0)
    found2 = std2.discover_datapoints(variant=VARIANT_IO_MODULE,
                                      port_count=args.ports)
    pts2 = found2.get("datapoints") or []
    check("  discovery still works with NO querytree (older firmware)",
          len(pts2) >= 16, len(pts2))
    bad_adr = [p["adr"] for p in pts2 if not p["adr"].startswith("/io/")]
    check("  and its addresses are NOT prefixed with the device MAC",
          not bad_adr, bad_adr[:2])
    std2.begin_read(4.0)
    rows2 = std2.read_datapoints(datapoints_from_config([dict(p) for p in pts2]))
    std2.end_read()
    good2 = [r for r in rows2 if r.get("quality") == 192]
    check("  every input from the fallback path reads GOOD",
          len(good2) == len(rows2) and len(rows2) > 0,
          "{0}/{1}".format(len(good2), len(rows2)))
    STD_STATE["querytree"] = True

    skip("STANDARD module over EtherNet/IP",
         "no AL40xx on this bench; the CIP path is proven on the master below")

    # =========================================== 3. MASTER over IoT Core
    print()
    print("[3. MASTER over IoT Core (HTTP :{0})]".format(args.iot_port))
    iot_pins = {}
    if not has_master_iot:
        skip("master over IoT", "{0}:{1} not reachable".format(args.master, args.iot_port))
    else:
        m = IfmMasterClient(host=args.master, port=args.iot_port, timeout_s=4.0)
        v = m.detect_variant()
        check("it is detected as an IO-LINK MASTER",
              v == VARIANT_IOLINK_MASTER, v)
        disc = m.discover_datapoints(variant=v, port_count=args.ports)
        mpts = disc.get("datapoints") or []
        mnames = {p["name"] for p in mpts}
        check("  discovery returns the block's values", len(mpts) >= 16, len(mpts))
        miss = [n for p in range(1, args.ports + 1)
                for n in ("Port{0}_Pin2".format(p), "Port{0}_Pin4".format(p))
                if n not in mnames]
        check("  EVERY port and pin is offered", not miss, miss[:6])
        check("  the batched read path is in use (not one GET per address)",
              m._multi_supported is True, m._multi_supported)

        m.begin_read(4.0)
        mrows = m.read_datapoints(
            datapoints_from_config([dict(p) for p in mpts]))
        m.end_read()
        mgood = [r for r in mrows if r.get("quality") == 192]
        check("  every value reads GOOD", len(mgood) == len(mrows),
              "{0}/{1}".format(len(mgood), len(mrows)))
        iot_pins = {r["name"]: int(bool(r["value"])) for r in mgood
                    if r["name"].endswith(("_Pin2", "_Pin4"))}
        check("  all {0} pin values were read".format(args.ports * 2),
              len(iot_pins) == args.ports * 2, len(iot_pins))
        for label in ("Master_Current", "Master_Voltage", "Master_Temperature"):
            r = next((x for x in mgood if x["name"] == label), None)
            if r:
                print("      {0:20s} = {1} {2}".format(label, r["value"], r.get("unit") or ""))

    # ======================================== 4. MASTER over fieldbus/CIP
    print()
    print("[4. MASTER over fieldbus (CIP explicit, TCP 44818)]")
    fb_pins = {}
    if not has_master_cip:
        skip("master over EtherNet/IP", "{0}:44818 not reachable".format(args.master))
    else:
        eip = EipDeviceClient(host=args.master, slot=0, timeout_s=4.0)
        ident = eip.identify() or {}
        check("the device identifies itself over CIP",
              bool(ident.get("vendor")), "{0} / {1}".format(
                  ident.get("vendor"), ident.get("product_name")))
        check("  and it is an ifm device",
              "ifm" in str(ident.get("vendor") or "").lower(), ident.get("vendor"))
        data = eip.read_assembly(int(args.assembly))
        check("  the input assembly reads", len(data) > 0,
              "assembly {0} = {1} bytes".format(args.assembly, len(data)))
        sigs = ifm_pin_signals(args.ports)
        check("  the pin map covers every port and pin",
              len(sigs) == args.ports * 2, len(sigs))
        errs = []
        for spec in sigs:
            sig = EipSignal.from_dict(spec)
            try:
                fb_pins[sig.name] = int(bool(decode_signal(data, sig)))
            except Exception as exc:
                errs.append("{0}: {1}".format(sig.name, exc))
        check("  every pin decodes from the assembly", not errs, errs[:3])

    # ============================================ 5. the two must agree
    print()
    print("[5. the SAME master, read both ways, must agree]")
    if iot_pins and fb_pins:
        shared = sorted(set(iot_pins) & set(fb_pins))
        check("both transports produce the same tag names",
              len(shared) == args.ports * 2,
              "{0} shared".format(len(shared)))
        bad = [(n, iot_pins[n], fb_pins[n]) for n in shared
               if iot_pins[n] != fb_pins[n]]
        check("  and the same value on every pin", not bad,
              "; ".join("{0} IoT={1} CIP={2}".format(*b) for b in bad[:4]))
        high = sorted(n for n in shared if iot_pins[n])
        print("      inputs currently HIGH: {0}".format(", ".join(high) or "none"))
    else:
        skip("cross-transport agreement", "needs the master on BOTH transports")

    # ================================= 6. historian + charts, through the app
    print()
    print("[6. collection -> historian -> charts, through the real app]")
    tmp = tempfile.mkdtemp(prefix="tn-ifmval-")
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

    def finish(code):
        for gw in ("gw-val-std", "gw-val-iot", "gw-val-cip"):
            call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": gw})
        try:
            proc.terminate(); proc.wait(timeout=20)
        except Exception:
            proc.kill()
        srv.shutdown()
        srv.server_close()
        return code

    st, b = call("POST", "/api/auth/login",
                 body={"username": "admin", "password": "admin"})
    tok = (b or {}).get("token") if isinstance(b, dict) else None
    check("admin login", st == 200 and bool(tok))
    if not tok:
        return finish(2)

    started = []

    # --- the STANDARD module gateway (simulated hardware) ---------------
    std_tags = sorted(names)
    st, r = call("POST", "/api/plc/gateways/start", tok, {
        "gateway_id": "gw-val-std",
        "config": {"gateway_type": "ifm_iolink", "name": "Standard IO module",
                   "device_name": "AL4022", "plc_ip": std_host,
                   "ifm_http_port": std_port, "ifm_variant": "auto",
                   "ifm_datapoints": [dict(p) for p in pts],
                   "tags": std_tags, "interval_ms": 1000,
                   "site": "Bench", "area": "IFM", "equipment": "StdModule"}})
    check("a STANDARD-module gateway starts", st == 200, str(r)[:90])
    if st == 200:
        started.append("gw-val-std")

    # --- the MASTER over IoT --------------------------------------------
    if has_master_iot:
        st, r = call("POST", "/api/plc/gateways/start", tok, {
            "gateway_id": "gw-val-iot",
            "config": {"gateway_type": "ifm_iolink", "name": "Master over IoT",
                       "device_name": "ifm master", "plc_ip": args.master,
                       "ifm_http_port": args.iot_port, "ifm_variant": "auto",
                       "ifm_datapoints": [dict(p) for p in mpts],
                       "tags": sorted(mnames),
                       # 5 s: this block's IoT Core cannot serve a 1 Hz poll.
                       "interval_ms": 5000,
                       "site": "Bench", "area": "IFM", "equipment": "MasterIoT"}})
        check("a MASTER-over-IoT gateway starts", st == 200, str(r)[:90])
        if st == 200:
            started.append("gw-val-iot")

    # --- the MASTER over fieldbus ---------------------------------------
    if has_master_cip:
        st, pm = call("POST", "/api/plc/eip/ifm-pin-map", tok, {
            "plc_ip": args.master, "instance": args.assembly,
            "port_count": args.ports, "verify": True})
        signals = (pm or {}).get("signals") or []
        check("the fieldbus pin map builds and verifies",
              st == 200 and len(signals) == args.ports * 2,
              (pm or {}).get("message", "")[:80])
        st, r = call("POST", "/api/plc/gateways/start", tok, {
            "gateway_id": "gw-val-cip",
            "config": {"gateway_type": "ethernet_ip", "name": "Master over fieldbus",
                       "device_name": "ifm master", "plc_ip": args.master,
                       "eip_input_assembly": args.assembly, "eip_slot": 0,
                       "eip_signals": signals,
                       "tags": [s["name"] for s in signals], "interval_ms": 1000,
                       "site": "Bench", "area": "IFM", "equipment": "MasterCIP"}})
        check("a MASTER-over-fieldbus gateway starts", st == 200, str(r)[:90])
        if st == 200:
            started.append("gw-val-cip")

    print("      collecting for 20 s...")
    time.sleep(20)

    st, hist = call("GET", "/api/app-store/historian?limit=4000", tok)
    all_rows = ((hist or {}).get("rows")) or []

    for gw, label, want in (
            ("gw-val-std", "STANDARD module", std_tags),
            ("gw-val-iot", "MASTER over IoT", sorted(iot_pins) if iot_pins else []),
            ("gw-val-cip", "MASTER over fieldbus", sorted(fb_pins) if fb_pins else [])):
        if gw not in started:
            continue
        rows = [r for r in all_rows if str(r.get("gateway_id")) == gw]
        check("{0}: readings reach the historian".format(label),
              len(rows) > 0, "{0} row(s)".format(len(rows)))
        got = {str(r.get("tag")) for r in rows}
        pin_tags = {t for t in want if t.endswith(("_Pin2", "_Pin4"))}
        if pin_tags:
            missing_tags = sorted(pin_tags - got)
            check("  EVERY port's tag is stored, not just some",
                  not missing_tags, missing_tags[:6])
        goodq = [r for r in rows if str(r.get("quality_label") or "") == "GOOD"
                 or int(r.get("quality") or 0) == 192]
        check("  the stored rows are GOOD quality",
              len(goodq) > 0 and len(goodq) >= len(rows) * 0.5,
              "{0}/{1} GOOD".format(len(goodq), len(rows)))
        ts = str((rows[0] or {}).get("ts") or (rows[0] or {}).get("ts_utc") or "")
        check("  timestamps use the canonical historian format",
              len(ts) >= 19 and ts[4] == "-" and ts[10] == " ", ts[:23])

    # --- the CHART path -------------------------------------------------
    print()
    print("      the endpoint the dashboard charts call:")
    sample_gw = started[0] if started else ""
    sample_tag = ""
    for r in all_rows:
        if str(r.get("gateway_id")) == sample_gw and str(r.get("tag", "")).startswith("Port"):
            sample_tag = str(r.get("tag"))
            break
    if sample_tag:
        st, agg = call("GET",
                       "/api/app-store/historian/agg?bucket=minute&limit=500"
                       "&gateway={0}&tag={1}".format(sample_gw, sample_tag), tok)
        arows = ((agg or {}).get("rows")) or []
        check("charts can read the collected tag back",
              st == 200 and len(arows) > 0,
              "tag={0} rows={1}".format(sample_tag, len(arows)))
        if arows:
            first = arows[0]
            check("  each bucket carries a plottable value",
                  any(first.get(k) is not None
                      for k in ("avg_value", "value", "avg", "last_value")),
                  sorted(first)[:6])
        st, rng = call("GET",
                       "/api/app-store/historian/range?limit=200"
                       "&gateway={0}&tag={1}".format(sample_gw, sample_tag), tok)
        check("  and the raw range endpoint returns the same tag",
              st == 200 and len(((rng or {}).get("rows")) or []) > 0,
              len(((rng or {}).get("rows")) or []))
    else:
        skip("chart read-back", "no Port* tag was stored to query")

    print()
    if SKIPS:
        print("SKIPPED ({0}):".format(len(SKIPS)))
        for s_ in SKIPS:
            print("  - {0}".format(s_))
    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return finish(0 if not FAILS else 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
