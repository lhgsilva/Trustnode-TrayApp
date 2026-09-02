# -*- coding: utf-8 -*-
"""A meter must collect the registers the operator ticked.

2026-09-02, reported: "the meter is not reading the correct information of the
meter's registers". Found on the live EM122 at 192.168.10.200, and the meter
was innocent - it answered 237.3 V, 0.241 A and 50.04 Hz correctly throughout.

The stored device had:

    register_profile : weidmuller_em122_all      (33 registers, THREE-phase)
    electrical_mode  : single_phase
    registers        : ["voltage_v", "current_a", ...]   <- a LIST of names

`registers` is expected to be a MAP of name -> address. A list matched neither
branch of the resolver, so it fell through to `else:` and loaded the profile
default - discarding all eight ticked registers without a word. The meter then
polled the three-phase map on a single-phase installation, writing a permanent
0.0 into the historian for voltage_l2/l3 and current_l2/l3, while the tags the
operator actually asked for were never produced at all.

WHAT MUST HOLD

  * a LIST of register names is honoured as a SELECTION, not discarded;
  * a name that belongs to a sibling profile of the SAME meter still resolves,
    because dropping it would be the same silent data loss in a new shape;
  * the operator is TOLD the profile does not match the wiring;
  * a name that exists on no profile for this meter is reported, not invented;
  * the rest of the device normalisation still happens - an early return here
    silently dropped database_id, descriptions and include_raw_tags.

The addresses asserted below were read off the real meter: offset 0 = 237.3 V,
offset 6 = 0.241 A, offset 70 = 50.04 Hz.
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


from app.services.power_manager import PowerManager      # noqa: E402

pm = PowerManager.__new__(PowerManager)

TICKED = ["active_power_w", "apparent_power_va", "current_a", "energy_wh",
          "frequency_hz", "power_factor", "reactive_power_var", "voltage_v"]

print("TrustNode - a meter collects what was ticked")
print()
print("[the live EM122 configuration, exactly as it was stored]")
live = {
    "id": "EM1", "name": "EM1", "enabled": True, "ip": "192.168.10.200",
    "port": 502, "unit_id": 1,
    "register_profile": "weidmuller_em122_all",
    "electrical_mode": "single_phase",
    "registers": list(TICKED),                     # a LIST, not a map
    "register_scales": {}, "register_enabled": {},
    "database_id": "db-1", "register_descriptions": {"voltage_v": "Mains"},
    "include_raw_tags": True,
}
out = pm._normalize_device(live)
regs = out.get("registers") or {}

check("the selection is honoured, not discarded",
      len(regs) == len(TICKED),
      "%d of %d resolved" % (len(regs), len(TICKED)))
check("  every ticked name is present",
      all(k in regs for k in TICKED),
      sorted(set(TICKED) - set(regs)) or "all present")

print()
print("[addresses match what the meter actually answers]")
for key, addr, seen in (("voltage_v", 30001, "237.3 V at offset 0"),
                        ("current_a", 30007, "0.241 A at offset 6"),
                        ("frequency_hz", 30071, "50.04 Hz at offset 70"),
                        ("active_power_w", 30013, "-29.9 W at offset 12")):
    check("%-18s -> %d" % (key, addr), int(regs.get(key, -1)) == addr, seen)

print()
print("[the operator is told the profile does not match]")
note = str(out.get("profile_error") or "")
check("a note explains the mismatch", bool(note), note[:120])
check("  and says the registers ARE being collected",
      "are being collected" in note.lower() or "ARE being collected" in note,
      "silence here is what made this look like a meter fault")
check("  and names the profile to change",
      "weidmuller_em122_all" in note)

print()
print("[the rest of the device is still normalised]")
check("database_id survives", out.get("database_id") == "db-1",
      "an early return here dropped this, and descriptions, and raw tags")
check("register_descriptions survive",
      (out.get("register_descriptions") or {}).get("voltage_v") == "Mains")
check("include_raw_tags survives", out.get("include_raw_tags") is True)
check("a scale exists for every resolved register",
      set((out.get("register_scales") or {}).keys()) == set(regs.keys()))

print()
print("[a name that exists nowhere is reported, not invented]")
bad = dict(live, registers=["voltage_v", "not_a_real_register"])
out2 = pm._normalize_device(bad)
check("the good one still resolves",
      int((out2.get("registers") or {}).get("voltage_v", -1)) == 30001)
check("  the unknown one is NOT given an address",
      "not_a_real_register" not in (out2.get("registers") or {}),
      "inventing an address is how a meter reports a confident wrong number")
check("  and it is named in the note",
      "not_a_real_register" in str(out2.get("profile_error") or ""))

print()
print("[a proper map still behaves exactly as before]")
out3 = pm._normalize_device(dict(live, registers={"voltage_v": 30001},
                                 use_custom_registers=True))
check("a custom map is still authoritative",
      list((out3.get("registers") or {}).keys()) == ["voltage_v"],
      "the list handling must not disturb the map path")

print()
print("[every meter answers the SAME six questions, whatever it calls them]")
# 2026-09-02: six profiles spell the same measurements five ways
# (energy_wh / energy_total_wh / energy_import_wh, voltage_v / voltage_l1_v /
# voltage_avg_v, ...). Power Overview therefore worked on one meter and showed
# blanks on another - a naming difference reading as a broken meter.
from app.services.meter_registers import (          # noqa: E402
    canonical_measurement, canonical_snapshot)

em122 = {"voltage_v": 237.3, "current_a": 0.241, "active_power_w": -29.9,
         "power_factor": -0.504, "frequency_hz": 50.04, "energy_wh": 12.0}
em525 = {"voltage_l1_v": 230.1, "voltage_l2_v": 229.8, "voltage_l3_v": 230.4,
         "current_l1_a": 5.1, "current_l2_a": 4.9, "current_l3_a": 5.0,
         "active_power_total_w": 3450.0, "power_factor_total": 0.98,
         "frequency_hz": 49.98, "energy_total_wh": 125000.0}

for label, src, want in (
        ("EM122 single-phase", em122,
         {"voltage_v": 237.3, "current_a": 0.241, "active_power_w": -29.9,
          "energy_wh": 12.0}),
        ("EM525 three-phase", em525,
         {"voltage_v": 230.1, "current_a": 5.0, "active_power_w": 3450.0,
          "energy_wh": 125000.0})):
    snap = canonical_snapshot(src)
    for key, expected in want.items():
        got = snap.get(key)
        check("%-18s %-16s" % (label, key),
              got is not None and abs(float(got) - expected) < 0.051,
              "got %s, expected %s" % (got, expected))

check("three-phase current is AVERAGED, not summed",
      abs(float(canonical_measurement(em525, "current_a")) - 5.0) < 0.001,
      "summing phase currents has no physical meaning - 15 A would be wrong")
check("three-phase voltage is AVERAGED, not just L1",
      abs(float(canonical_measurement(em525, "voltage_v")) - 230.1) < 0.051,
      "listing voltage_l1_v as an exact match made a 3-phase meter answer "
      "with one phase and call it the voltage")
check("three-phase power IS summed",
      abs(float(canonical_measurement(em525, "active_power_w")) - 3450.0) < 0.1)
check("a measurement the meter does not have returns None, not 0.0",
      canonical_measurement({"voltage_v": 230.0}, "energy_wh") is None,
      "a zero and 'this meter has no such register' look identical on a KPI "
      "card, and only one of them is a fact")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
