# -*- coding: utf-8 -*-
"""The ifm channel model, without any hardware.

test_ifm_dual_path.py and test_ifm_real_block_e2e.py need a block on the
bench. This one pins the parts that must not drift on ANY machine:

  * getdatamulti addresses a NODE, not its getter. Sending ".../getdata" made
    a real AL1326 answer code 200 with an EMPTY payload, so every address fell
    through to a separate HTTP GET and the batched path latched off for the
    life of the client - ~100 requests per discovery instead of 4.
  * the fieldbus bit map: byte 0 is pin 4, byte 1 is pin 2, bit N-1 is port N.
  * both transports must generate the SAME tag names, or a trend breaks when a
    block moves from IoT to fieldbus.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:130]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


from app.drivers.ifm_iolink import (  # noqa: E402
    _multi_adr, unit_adr_for, channel_for_leaf, _channel_point,
    port_pin2in_adr, port_pdin_adr, port_current_adrs,
    CHANNEL_DI, CHANNEL_DO, CHANNEL_IOLINK, CHANNEL_CURRENT,
    CHANNEL_DIAGNOSTIC, MULTI_CHUNK, uniquify_points, _io_point_from_adr)
from app.drivers.ethernet_ip import (  # noqa: E402
    EipSignal, decode_signal, ifm_pin_signals)

print("[getdatamulti must address the node, not its getter]")
check("a /getdata address is shortened for getdatamulti",
      _multi_adr("/iolinkmaster/port[1]/pin2in/getdata")
      == "/iolinkmaster/port[1]/pin2in",
      _multi_adr("/iolinkmaster/port[1]/pin2in/getdata"))
check("  an address without /getdata is left alone",
      _multi_adr("/processdatamaster/current") == "/processdatamaster/current")
check("  and it is not confused by 'getdata' inside a name",
      _multi_adr("/a/getdatathing") == "/a/getdatathing")
check("the batch size is worth batching", MULTI_CHUNK >= 16, MULTI_CHUNK)

print()
print("[the block declares its own units]")
check("a value's unit sits beside it",
      unit_adr_for("/processdatamaster/current/getdata")
      == "/processdatamaster/current/unit/getdata")

print()
print("[channel labelling]")
check("a digital input leaf is DI",
      channel_for_leaf("digital_input") == CHANNEL_DI)
check("a digital output leaf is DO",
      channel_for_leaf("digital_output") == CHANNEL_DO)
check("a current leaf is Current", channel_for_leaf("current") == CHANNEL_CURRENT)
check("process data is IO-Link", channel_for_leaf("pdin") == CHANNEL_IOLINK)
check("anything else is a diagnostic",
      channel_for_leaf("mastercycletime_actual") == CHANNEL_DIAGNOSTIC)

pt = _channel_point(name="Port3_Pin2", adr=port_pin2in_adr(3), channel=CHANNEL_DI,
                    kind="bool", source="port 3 pin 2")
check("a channel point carries everything the picker needs",
      pt["name"] == "Port3_Pin2" and pt["channel"] == CHANNEL_DI
      and pt["kind"] == "bool" and pt["enabled"] is True, pt)

# An I/O module (non-master) must be labelled the same way as a master.
mod = _io_point_from_adr("/io/port[3]/pin4/digital_input")
check("a non-master I/O module gets the same labels",
      mod["name"] == "Port3_Pin4" and mod["channel"] == CHANNEL_DI, mod)

print()
print("[uniquify keeps the channel]")
dupes = [_channel_point("temperature", "/deviceinfo/temperature/getdata",
                        CHANNEL_DIAGNOSTIC),
         _channel_point("temperature", "/processdatamaster/temperature/getdata",
                        CHANNEL_CURRENT)]
uniq = uniquify_points(dupes)
check("both survive with distinct names", len(uniq) == 2,
      [u["name"] for u in uniq])
check("  and neither loses its channel",
      all(u.get("channel") for u in uniq), [u.get("channel") for u in uniq])

print()
print("[the fieldbus bit map]")
sigs = ifm_pin_signals(8)
check("both pins of all 8 ports", len(sigs) == 16, len(sigs))
by_name = {s["name"]: s for s in sigs}
check("  pin 4 lives in byte 0", by_name["Port1_Pin4"]["byte_offset"] == 0)
check("  pin 2 lives in byte 1", by_name["Port1_Pin2"]["byte_offset"] == 1)
check("  bit N-1 is port N",
      by_name["Port7_Pin4"]["bit"] == 6 and by_name["Port8_Pin2"]["bit"] == 7,
      (by_name["Port7_Pin4"]["bit"], by_name["Port8_Pin2"]["bit"]))
check("  every signal is labelled DI",
      all(s["channel"] == CHANNEL_DI for s in sigs))

# The exact buffer read off the real AL1326 on 2026-08-27: port 7 pin 4 high,
# port 8 pin 2 high, everything else low.
data = bytes([0x40, 0x80, 0x00, 0x00])
decoded = {s["name"]: bool(decode_signal(data, EipSignal.from_dict(s))) for s in sigs}
high = sorted(n for n, v in decoded.items() if v)
check("the recorded AL1326 buffer decodes to the recorded state",
      high == ["Port7_Pin4", "Port8_Pin2"], high)

print()
print("[both transports must name things identically]")
# IoT names are built as Port{n}_Pin{2,4}; fieldbus must match exactly.
iot_names = set()
for n in range(1, 9):
    iot_names.add("Port{0}_Pin2".format(n))
    iot_names.add("Port{0}_Pin4".format(n))
check("the fieldbus map uses the IoT tag names",
      set(by_name) == iot_names,
      sorted(set(by_name) ^ iot_names)[:5])

print()
print("[per-port current is probed, never assumed]")
cands = port_current_adrs(3)
check("several candidate addresses are tried", len(cands) >= 3, len(cands))
check("  all of them name the right port",
      all("port[3]" in c for c in cands), cands[:2])

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
