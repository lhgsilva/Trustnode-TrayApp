# -*- coding: utf-8 -*-
"""An ifm master on fieldbus must give up more than two bytes.

2026-09-02, reported: "when configured in the fieldbus port ... it is not able
to read the digital inputs or IO-Link status ... for both IFM devices we should
be able to read all the IO of the block using IoT or fieldbus, master or not."

Correct. TrustNode mapped bytes 0 and 1 of a 446-byte input assembly - the
digital input image - and nothing else. IO-Link identity and process data were
in the other 444 bytes and no code knew where.

WHERE THE LAYOUT CAME FROM

Not from an EDS, which declares an assembly's instance and size and never says
what a bit means. From the operator's own AL1326, read twice over CIP - once
with an SM9400 on port 8 and once without - and cross-checked against the SAME
block's IoT Core answers in the same session:

    port 4   record bytes 1e 04  ->  1054   IoT deviceid 1054   DV2120
    port 5   record bytes 66 02  ->   614   IoT deviceid  614   DP1223
    port 7   record bytes ba 04  ->  1210   IoT deviceid 1210   PV8004
    port 8   record bytes 87 01  ->   391   IoT deviceid  391   SM9400

Every populated record carried vendor 0x0136 = 310, ifm's IO-Link vendor id.
Unplugging port 8 turned its record into 07 00 and its IoT status from 2 to 0,
in the same capture. Four independent agreements plus a state change is why
this is a mapping and not a guess - the EM122 shipped 0.000 V from a
healthy-looking meter in August because somebody guessed offsets.

The bytes below are that real capture. If this test ever fails, the layout has
been changed away from what the hardware actually does.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.drivers.ethernet_ip import (          # noqa: E402
    EipSignal, decode_signal, ifm_master_signals, ifm_pdin_offset,
    ifm_port_status_offset)


def _capture(port8_present: bool) -> bytes:
    """Assembly 100 as the operator's AL1326 actually returned it."""
    empty = "0700" + "00" * 16
    def rec(dev_hex):
        return "05203601" + dev_hex + "00" * 12
    body = ("0080" if port8_present else "0000") + "00" * 44
    body += empty + empty + empty          # ports 1-3: nothing plugged in
    body += rec("1e04")                    # port 4: DV2120
    body += rec("6602")                    # port 5: DP1223
    body += empty                          # port 6
    body += rec("ba04")                    # port 7: PV8004
    body += rec("8701") if port8_present else empty
    raw = bytearray(bytes.fromhex(body))
    raw.extend(bytes(446 - len(raw)))
    if port8_present:
        # Port 7 and port 8 process data, word-swapped as the block sends it.
        # Exactly 8 bytes each. Writing 9 into an 8-byte slice GROWS a
        # bytearray, which shifted every later offset and failed three checks
        # that were testing the fixture rather than the layout.
        p7 = bytes.fromhex("0c030002500f00fe")   # dump offsets 382..389
        p8 = bytes.fromhex("2348c6effc00f006")   # dump offsets 414..421
        assert len(p7) == 8 and len(p8) == 8
        raw[ifm_pdin_offset(7):ifm_pdin_offset(7) + 8] = p7
        raw[ifm_pdin_offset(8):ifm_pdin_offset(8) + 8] = p8
    return bytes(raw)


def value(raw, signals, name):
    spec = next((s for s in signals if s["name"] == name), None)
    if spec is None:
        return None
    return decode_signal(raw, EipSignal.from_dict(spec))


def num(raw, signals, name):
    """The decoded number, or -1 when the signal is missing.

    NOT `value(...) or -1`: an empty port decodes to 0.0, which is falsy, so
    that idiom turned a CORRECT reading of zero into -1 and failed the two
    checks that exist precisely to prove an empty port reads zero.
    """
    got = value(raw, signals, name)
    return -1 if got is None else int(got)


print("TrustNode - ifm fieldbus layout, against a real AL1326 capture")
raw_on = _capture(True)
raw_off = _capture(False)
sigs = ifm_master_signals(port_count=8, assembly_size=446)
check("the assembly is the size the block reports", len(raw_on) == 446)

print()
print("[the offsets close the assembly exactly]")
check("port records start at 46, 18 bytes apart",
      ifm_port_status_offset(1) == 46 and ifm_port_status_offset(8) == 172)
check("process data starts at 190, 32 bytes apart",
      ifm_pdin_offset(1) == 190 and ifm_pdin_offset(8) == 414)
check("  and 190 + 8 x 32 is exactly the assembly size",
      ifm_pdin_offset(8) + 32 == 446,
      "a layout that does not close is a layout that is wrong somewhere")

print()
print("[the device ids match what the block said over IoT Core]")
for port, dev, name in ((4, 1054, "DV2120"), (5, 614, "DP1223"),
                        (7, 1210, "PV8004"), (8, 391, "SM9400")):
    got = value(raw_on, sigs, "Port%d_DeviceId" % port)
    check("port %d reads %d (%s)" % (port, dev, name), int(got) == dev, got)
check("  every populated port reports ifm's vendor id 310",
      all(num(raw_on, sigs, "Port%d_VendorId" % p) == 310
          for p in (4, 5, 7, 8)))
check("  an empty port reads device id 0, not a stale number",
      all(num(raw_on, sigs, "Port%d_DeviceId" % p) == 0
          for p in (1, 2, 3, 6)),
      "07 00 means nothing is plugged in")

print()
print("[unplugging a sensor is visible]")
check("port 8 device id drops to 0 when the SM9400 is removed",
      num(raw_off, sigs, "Port8_DeviceId") == 0,
      "this is the change that identified the record in the first place")
check("  and its digital input bit follows",
      value(raw_on, sigs, "Port8_Pin2") is True
      and value(raw_off, sigs, "Port8_Pin2") is False)
check("  while the other ports are untouched",
      num(raw_off, sigs, "Port4_DeviceId") == 1054
      and num(raw_off, sigs, "Port7_DeviceId") == 1210)

print()
print("[process data is offered, and honestly labelled]")
pdin = [s for s in sigs if "_PDIN_W" in s["name"]]
check("every port offers process-data words", len(pdin) == 8 * 4, len(pdin))
check("  the first word of port 8 decodes from the right bytes",
      num(raw_on, sigs, "Port8_PDIN_W1") == 0x4823,
      "byte-swapped against the IoT pdin, which is how the block sends it")
check("  and the source text says the byte order is swapped",
      all("swapped" in s.get("source", "") for s in pdin),
      "an operator comparing the two transports must not think one is wrong")
check("process data is NOT ticked by default",
      all(s.get("enabled") is False for s in pdin),
      "a raw word means nothing without the sensor's IODD; offering it is "
      "honest, collecting it unasked is not")

print()
print("[the digital inputs still work]")
check("the pin map is still there",
      value(raw_on, sigs, "Port7_Pin4") is not None
      and value(raw_on, sigs, "Port1_Pin2") is False,
      "the new mapping must add to the old one, not replace it")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
