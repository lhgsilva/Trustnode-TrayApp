# -*- coding: utf-8 -*-
"""An ifm block must read the SAME over IoT Core and over fieldbus.

2026-08-27: "change the software to cover both type of configurations, make
sure the full flow works, for master and not master devices ... we would like
to connect the data from the ports pins and current drawn of each pin and
expose them as part of the tags of the device, if is a DI, DO or iolink type
etc. we should be able to trend those values like normal tags."

Two completely independent transports reach the same hardware:

  * IoT Core  - HTTP/JSON on port 80, addresses like
                /iolinkmaster/port[7]/pin2in
  * fieldbus  - EtherNet/IP explicit messaging, Class 3 on TCP 44818, reading
                the input assembly and slicing bits out of it

They must produce the same tag NAMES with the same VALUES, or a dashboard
built against a block on one transport breaks when the block is moved to the
other. That is the whole point of supporting both.

    python scripts/test_ifm_dual_path.py --host 192.168.10.251

Reads only. Safe against live hardware.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.10.251")
    ap.add_argument("--iot-port", type=int, default=80)
    ap.add_argument("--assembly", type=int, default=100,
                    help="input assembly instance (from the EDS)")
    ap.add_argument("--ports", type=int, default=8)
    args = ap.parse_args()

    from app.drivers.ifm_iolink import (
        IfmMasterClient, datapoints_from_config, CHANNEL_DI, CHANNEL_DO,
        CHANNEL_IOLINK, CHANNEL_CURRENT, CHANNEL_DIAGNOSTIC)
    from app.drivers.ethernet_ip import (
        EipDeviceClient, EipSignal, decode_signal, ifm_pin_signals)

    print("TrustNode - one ifm block, both transports")
    print("  host: {0}  (IoT :{1}, EtherNet/IP :44818)".format(
        args.host, args.iot_port))
    print()

    # ---------------------------------------------------------------- IoT ---
    print("[IoT Core - discovery]")
    client = IfmMasterClient(host=args.host, port=args.iot_port, timeout_s=4.0)
    try:
        found = client.discover_datapoints(variant="auto", port_count=args.ports)
    except Exception as exc:
        check("the block answered discovery", False, exc)
        print()
        print("RESULT: FAIL - the block is not reachable over IoT Core")
        return 2
    points = found.get("datapoints") or []
    check("the block answered discovery", bool(points),
          "{0} datapoint(s)".format(len(points)))
    check("  it identified itself as a master",
          found.get("variant") == "iolink_master", found.get("variant"))

    by_channel = {}
    for pt in points:
        by_channel.setdefault(pt.get("channel") or "?", []).append(pt["name"])
    print("  channels found: {0}".format(
        ", ".join("{0}={1}".format(k, len(v)) for k, v in sorted(by_channel.items()))))

    # Every datapoint must be LABELLED - that is what the operator sorts on.
    unlabelled = [p["name"] for p in points if not p.get("channel")]
    check("  every value carries a channel type", not unlabelled, unlabelled[:4])
    known = {CHANNEL_DI, CHANNEL_DO, CHANNEL_IOLINK, CHANNEL_CURRENT,
             CHANNEL_DIAGNOSTIC}
    unknown = sorted({p.get("channel") for p in points} - known)
    check("  and the type is one we defined", not unknown, unknown)

    # Both pins of every port must be offered on a master.
    missing = [n for p in range(1, args.ports + 1) for n in
               ("Port{0}_Pin2".format(p), "Port{0}_Pin4".format(p))
               if n not in {x["name"] for x in points}]
    check("  both pins of every port are offered", not missing, missing[:6])

    # Current: per-port where the hardware has it, master total otherwise.
    names = {p["name"] for p in points}
    per_port_current = sorted(n for n in names if n.endswith("_Current")
                              and n.startswith("Port"))
    check("  a current draw value is available",
          bool(per_port_current) or "Master_Current" in names,
          "per-port: {0}".format(per_port_current or "not supported by this block")
          + ("; master total offered" if "Master_Current" in names else ""))

    print()
    print("[IoT Core - live read]")
    dps = datapoints_from_config([dict(p, enabled=True) for p in points])
    client.begin_read(4.0)
    rows = client.read_datapoints(dps)
    client.end_read()
    good = [r for r in rows if r.get("quality") == 192]
    check("every discovered value reads GOOD", len(good) == len(rows),
          "{0}/{1}".format(len(good), len(rows)))
    iot_pins = {r["name"]: int(bool(r["value"])) for r in good
                if r["name"].endswith(("_Pin2", "_Pin4"))}
    high = sorted(n for n, v in iot_pins.items() if v)
    print("  inputs currently HIGH: {0}".format(", ".join(high) or "none"))

    # ----------------------------------------------------------- fieldbus ---
    print()
    print("[fieldbus - EtherNet/IP explicit messaging]")
    eip = EipDeviceClient(host=args.host, slot=0, timeout_s=4.0)
    try:
        data = eip.read_assembly(int(args.assembly))
    except Exception as exc:
        check("assembly {0} could be read".format(args.assembly), False, exc)
        print()
        print("RESULT: FAIL - the block is not reachable over EtherNet/IP")
        return 2
    check("assembly {0} could be read".format(args.assembly), bool(data),
          "{0} bytes".format(len(data)))
    print("  first 4 bytes: {0}".format(
        " ".join("{0:02x}".format(b) for b in data[:4])))

    sigs = ifm_pin_signals(args.ports)
    check("the ifm pin map generates both pins per port",
          len(sigs) == args.ports * 2, "{0} signals".format(len(sigs)))
    fb_pins = {}
    errors = []
    for spec in sigs:
        sig = EipSignal.from_dict(spec)
        try:
            fb_pins[sig.name] = int(bool(decode_signal(data, sig)))
        except Exception as exc:
            errors.append("{0}: {1}".format(sig.name, exc))
    check("  every signal decodes from the assembly", not errors, errors[:3])

    # ------------------------------------------------------- the comparison -
    print()
    print("[the two transports must agree]")
    shared = sorted(set(iot_pins) & set(fb_pins))
    check("both transports produced the same tag names",
          len(shared) == len(sigs) and len(shared) == len(iot_pins),
          "IoT={0} fieldbus={1} shared={2}".format(
              len(iot_pins), len(fb_pins), len(shared)))

    mismatch = [(n, iot_pins[n], fb_pins[n]) for n in shared
                if iot_pins[n] != fb_pins[n]]
    check("every pin reads the same over both",
          not mismatch,
          "; ".join("{0}: IoT={1} fieldbus={2}".format(*m) for m in mismatch[:5]))

    print()
    print("  {0:14s} {1:>8s} {2:>10s}".format("TAG", "IoT", "fieldbus"))
    for n in shared:
        flag = "" if iot_pins[n] == fb_pins[n] else "   <-- MISMATCH"
        print("  {0:14s} {1:>8d} {2:>10d}{3}".format(n, iot_pins[n], fb_pins[n], flag))

    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
