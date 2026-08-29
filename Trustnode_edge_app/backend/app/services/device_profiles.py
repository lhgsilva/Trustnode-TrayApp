# -*- coding: utf-8 -*-
"""The device catalogue — one profile library for every protocol.

2026-08-28. Ignition, KEPServerEX and Studio 5000 all share one shape:
**Connection -> Device profile -> Tags**. None of them writes a page per
manufacturer; they ship a profile library plus an importer, feeding one generic
editor. This is that library.

A profile is deliberately *not* a driver. It is a starting tag list for a known
device, expressed in whatever the protocol addresses by:

    modbus_tcp    -> register addresses, as the datasheet prints them
    ethernet_ip   -> CIP parameter numbers, or assembly byte offsets

**Every profile carries `verified`.** `True` means the tags in it have been read
from real hardware and checked against physics or against the device's own
display. `False` means they came from documentation and nobody has proven them
here. That distinction is the whole point: on Modbus and CIP alike a wrong
address returns a *plausible number*, not an error, so a profile that quietly
guesses is worse than no profile. The UI shows the flag, and unverified
profiles arrive with their tags UNTICKED.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.services.meter_registers import EM122_SINGLE_PHASE, EM122_THREE_PHASE

# What a tag looks like in a profile, per protocol:
#   modbus_tcp   {name, address, function, kind, scale, offset, unit, bit, word_swap}
#   ethernet_ip  {name, param, kind, scale, offset, unit}      (CIP Parameter Object)
#                {name, byte_offset, kind, bit, scale, unit}   (assembly slice)


def _modbus_tags(register_map: Dict[str, Any], unit_by_suffix: bool = True) -> List[Dict[str, Any]]:
    """A meter register map -> profile tags.

    Units are inferred from the key suffix the meter maps already use
    (`..._v`, `..._a`, `..._w`), so one naming convention serves both.
    """
    suffix_unit = {
        "_v": "V", "_a": "A", "_w": "W", "_va": "VA", "_var": "var",
        "_hz": "Hz", "_kwh": "kWh", "_kvarh": "kvarh", "_pct": "%",
    }
    out: List[Dict[str, Any]] = []
    for name, address in register_map.items():
        unit = ""
        if unit_by_suffix:
            for suffix, u in suffix_unit.items():
                if str(name).lower().endswith(suffix):
                    unit = u
                    break
        out.append({
            "name": name, "address": str(address), "function": "input",
            "kind": "float32", "scale": 1, "offset": 0, "unit": unit,
        })
    return out


# ---------------------------------------------------------------------------
# PowerFlex / Kinetix — CIP Parameter Object (class 0x0F), instance = parameter
# ---------------------------------------------------------------------------
# These come from Rockwell documentation and are NOT verified against hardware
# here. Parameter numbering differs between drive families and firmware, so
# treat them as a starting point and confirm each value against the drive's own
# HIM display before trusting a trend. The "Scan parameters" tool in the mapper
# exists precisely so this can be done without guessing.
POWERFLEX_525 = [
    {"name": "Output_Freq",    "param": 1, "kind": "REAL", "scale": 0.01, "unit": "Hz"},
    {"name": "Commanded_Freq", "param": 2, "kind": "REAL", "scale": 0.01, "unit": "Hz"},
    {"name": "Output_Current", "param": 3, "kind": "REAL", "scale": 0.01, "unit": "A"},
    {"name": "Output_Voltage", "param": 4, "kind": "REAL", "scale": 0.1,  "unit": "V"},
    {"name": "DC_Bus_Voltage", "param": 5, "kind": "REAL", "scale": 0.1,  "unit": "V"},
    {"name": "Drive_Status",   "param": 6, "kind": "UINT", "scale": 1,    "unit": ""},
    {"name": "Fault_1_Code",   "param": 7, "kind": "UINT", "scale": 1,    "unit": ""},
]

POWERFLEX_750 = [
    {"name": "Output_Freq",    "param": 1,  "kind": "REAL", "scale": 1, "unit": "Hz"},
    {"name": "Output_Current", "param": 7,  "kind": "REAL", "scale": 1, "unit": "A"},
    {"name": "Output_Voltage", "param": 8,  "kind": "REAL", "scale": 1, "unit": "V"},
    {"name": "DC_Bus_Volts",   "param": 11, "kind": "REAL", "scale": 1, "unit": "V"},
    {"name": "Output_Power",   "param": 9,  "kind": "REAL", "scale": 1, "unit": "kW"},
    {"name": "Drive_Status_1", "param": 935, "kind": "UDINT", "scale": 1, "unit": ""},
]

KINETIX_5500 = [
    {"name": "Motor_Velocity", "param": 1, "kind": "REAL", "scale": 1, "unit": "rpm"},
    {"name": "Motor_Current",  "param": 2, "kind": "REAL", "scale": 1, "unit": "A"},
    {"name": "DC_Bus_Voltage", "param": 3, "kind": "REAL", "scale": 1, "unit": "V"},
    {"name": "Axis_Fault",     "param": 4, "kind": "UDINT", "scale": 1, "unit": ""},
]


PROFILES: List[Dict[str, Any]] = [
    # ------------------------------------------------------------ Modbus TCP
    {
        "id": "weidmuller-em122-3ph",
        "manufacturer": "Weidmüller",
        "model": "EM122 — three phase",
        "protocol": "modbus_tcp",
        "category": "Power meter",
        "verified": True,
        "notes": ("Read from a real EM122 on 2026-08-28: V×I matched the meter's own "
                  "apparent power to within 1%, and |P| ≤ S held. Addresses are the "
                  "datasheet's 3x references."),
        "defaults": {"modbus_port": 502, "modbus_unit_id": 1, "interval_ms": 1000},
        "tags": _modbus_tags(EM122_THREE_PHASE),
    },
    {
        "id": "weidmuller-em122-1ph",
        "manufacturer": "Weidmüller",
        "model": "EM122 — single phase",
        "protocol": "modbus_tcp",
        "category": "Power meter",
        "verified": True,
        "notes": "Same device, the single-phase subset.",
        "defaults": {"modbus_port": 502, "modbus_unit_id": 1, "interval_ms": 1000},
        "tags": _modbus_tags(EM122_SINGLE_PHASE),
    },
    {
        "id": "generic-vsd-modbus",
        "manufacturer": "Generic",
        "model": "VSD / drive (Modbus TCP)",
        "protocol": "modbus_tcp",
        "category": "Drive / VSD",
        "verified": False,
        "notes": ("A starting shape for a drive on Modbus TCP — holding registers, "
                  "16-bit, scaled. Confirm every address against the drive's manual: "
                  "on Modbus a wrong address returns a plausible number, not an error."),
        "defaults": {"modbus_port": 502, "modbus_unit_id": 1, "interval_ms": 1000},
        "tags": [
            {"name": "Output_Freq",    "address": "40001", "function": "holding",
             "kind": "uint16", "scale": 0.01, "offset": 0, "unit": "Hz"},
            {"name": "Output_Current", "address": "40002", "function": "holding",
             "kind": "uint16", "scale": 0.01, "offset": 0, "unit": "A"},
            {"name": "Output_Voltage", "address": "40003", "function": "holding",
             "kind": "uint16", "scale": 0.1, "offset": 0, "unit": "V"},
            {"name": "DC_Bus_Voltage", "address": "40004", "function": "holding",
             "kind": "uint16", "scale": 0.1, "offset": 0, "unit": "V"},
            {"name": "Status_Word",    "address": "40005", "function": "holding",
             "kind": "uint16", "scale": 1, "offset": 0, "unit": ""},
        ],
    },

    # ----------------------------------------------------------- EtherNet/IP
    {
        "id": "rockwell-powerflex-525",
        "manufacturer": "Rockwell",
        "model": "PowerFlex 525",
        "protocol": "ethernet_ip",
        "category": "Drive / VSD",
        "verified": False,
        "notes": ("CIP Parameter Object (class 0x0F), instance = parameter number. "
                  "Numbering varies with firmware — use 'Scan parameters' in the mapper "
                  "and compare against the drive's own display before trusting a trend."),
        "defaults": {"eip_slot": 0, "interval_ms": 1000},
        "tags": POWERFLEX_525,
    },
    {
        "id": "rockwell-powerflex-753-755",
        "manufacturer": "Rockwell",
        "model": "PowerFlex 753 / 755",
        "protocol": "ethernet_ip",
        "category": "Drive / VSD",
        "verified": False,
        "notes": ("750-series host parameters over CIP. Port 0 parameters; numbering "
                  "differs from the 525 family. Verify with 'Scan parameters'."),
        "defaults": {"eip_slot": 0, "interval_ms": 1000},
        "tags": POWERFLEX_750,
    },
    {
        "id": "rockwell-kinetix-5500",
        "manufacturer": "Rockwell",
        "model": "Kinetix 5500 / 5700",
        "protocol": "ethernet_ip",
        "category": "Servo drive",
        "verified": False,
        "notes": ("Kinetix drives are normally owned by a Logix controller over CIP "
                  "Motion. These are ACYCLIC parameter reads, which coexist with that "
                  "ownership — but if a Logix PLC already has the axis, reading the "
                  "tags from the PLC is simpler and faster."),
        "defaults": {"eip_slot": 0, "interval_ms": 1000},
        "tags": KINETIX_5500,
    },
]


def list_profiles(protocol: str = "") -> List[Dict[str, Any]]:
    """The catalogue, optionally narrowed to one protocol.

    Tag lists are returned so the UI can preview a profile before applying it;
    they are copies, so an edit in the dialog cannot mutate the catalogue.
    """
    want = str(protocol or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for p in PROFILES:
        if want and str(p.get("protocol")) != want:
            continue
        out.append({
            "id": p["id"],
            "manufacturer": p["manufacturer"],
            "model": p["model"],
            "protocol": p["protocol"],
            "category": p.get("category", ""),
            "verified": bool(p.get("verified")),
            "notes": p.get("notes", ""),
            "defaults": dict(p.get("defaults") or {}),
            "tag_count": len(p.get("tags") or []),
            "tags": [dict(t) for t in (p.get("tags") or [])],
        })
    return out


def get_profile(profile_id: str) -> Dict[str, Any] | None:
    for p in list_profiles():
        if p["id"] == str(profile_id):
            return p
    return None
