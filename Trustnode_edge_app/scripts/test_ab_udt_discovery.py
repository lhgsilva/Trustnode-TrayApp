# -*- coding: utf-8 -*-
"""Allen-Bradley tag discovery must expand UDT members, not just arrays.

2026-10-09, reported with screenshots: a UDT built for the project -
R1_TRUSTNODE_RESULTS, holding Work_Order, Asset_ID, TCU_PN, Start_Time,
End_time, Duration, Type, Check_List[10], Dead_Head_Pressure,
Max_Flow_Rate, Max_Temperature, Max_Pressure, Heating_Rate, Cooling_Rate,
Test_Cycle_Started[100] and Test_Cycle_Ended[100] - appeared in the gateway
picker as ONE row. Discovery reported "1637 valid AB tags (arrays expanded:
1096)", so arrays were being enumerated and struct MEMBERS never were. The
operator could not tick the member they wanted and had to type every path
by hand.

pycomm3 returns the members under data_type["internal_tags"], so the same
expansion the top level already did works recursively.

This drives the real discovery helpers with a tag list shaped exactly like
pycomm3's, so no PLC is needed and nothing is read from the live store.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


# The discovery helpers are defined inside the route so they can close over
# the live connection. Lift the block out and exec it, which keeps this test
# honest - it runs the SHIPPING code, not a copy of it.
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "plc.py"),
              encoding="utf-8", errors="replace").read()
start = SRC.index("                ARRAY_ELEMENT_CAP = 256")
end = SRC.index("                for td in tag_defs:")
block = "\n".join(line[16:] if line.startswith(" " * 16) else line
                  for line in SRC[start:end].split("\n"))
ns: dict = {}
exec(compile(block, "plc_discovery_block", "exec"), ns)       # noqa: S102

_base_name = ns["_base_name"]
_array_dims = ns["_array_dims"]
_enumerate_indexed_names = ns["_enumerate_indexed_names"]
_is_struct = ns["_is_struct"]
_expand_struct = ns["_expand_struct"]

print("TrustNode - Allen-Bradley UDT discovery")
print()


def member(dtype, dims=None, internal=None):
    d: dict = {"data_type": {"name": dtype} if internal is None
               else {"name": dtype, "internal_tags": internal}}
    if dims:
        d["dimensions"] = dims
    return d


# Exactly the UDT in the screenshot.
UDT = {
    "Work_Order": member("STRING", internal={"LEN": member("DINT"),
                                             "DATA": member("SINT", [82])}),
    "Asset_ID": member("STRING", internal={"LEN": member("DINT"),
                                           "DATA": member("SINT", [82])}),
    "TCU_PN": member("STRING"),
    "Start_Time": member("STRING"),
    "End_time": member("STRING"),
    "Duration": member("STRING"),
    "Type": member("STRING"),
    "Check_List": member("DINT", [10]),
    "Dead_Head_Pressure": member("REAL"),
    "Max_Flow_Rate": member("REAL"),
    "Max_Temperature": member("REAL"),
    "Max_Pressure": member("REAL"),
    "Heating_Rate": member("REAL"),
    "Cooling_Rate": member("REAL"),
    "Test_Cycle_Started": member("DINT", [100]),
    "Test_Cycle_Ended": member("DINT", [100]),
    "ZZZZZZZZZZPADDING": member("SINT"),
}
TAG = {"tag_name": "R1_TRUSTNODE_RESULTS", "tag_type": "struct",
       "data_type": {"name": "TRUSTNODE_RESULTS", "internal_tags": UDT}}

check("the UDT is recognised as a struct", _is_struct(TAG))
names = _expand_struct(_base_name(TAG), TAG)
check("  its members are expanded at all", len(names) > 0,
      "this is the whole bug: it produced nothing")

print()
print("[every member the operator can see in Studio 5000]")
for want in ("Work_Order", "Asset_ID", "TCU_PN", "Start_Time", "End_time",
             "Duration", "Type", "Dead_Head_Pressure", "Max_Flow_Rate",
             "Max_Temperature", "Max_Pressure", "Heating_Rate", "Cooling_Rate"):
    check("  R1_TRUSTNODE_RESULTS.%s" % want,
          "R1_TRUSTNODE_RESULTS.%s" % want in names)

print()
print("[arrays INSIDE the UDT are enumerated too]")
check("Check_List[10] becomes ten elements",
      all("R1_TRUSTNODE_RESULTS.Check_List[%d]" % i in names for i in range(10)),
      "an operator logging one check cannot read the whole array")
check("  and the bare array name is not offered on its own",
      "R1_TRUSTNODE_RESULTS.Check_List" not in names)
check("Test_Cycle_Started[100] is enumerated",
      "R1_TRUSTNODE_RESULTS.Test_Cycle_Started[0]" in names
      and "R1_TRUSTNODE_RESULTS.Test_Cycle_Started[99]" in names)

print()
print("[what must NOT be expanded]")
check("a STRING is a leaf, not LEN + DATA[82]",
      "R1_TRUSTNODE_RESULTS.Work_Order.LEN" not in names
      and not any(".Work_Order.DATA" in n for n in names),
      "AB models STRING as a struct; expanding it buries the real members")
check("Rockwell's internal padding is skipped",
      not any("ZZZZZZZZZZ" in n.upper() for n in names))

print()
print("[bounded, so one fat UDT cannot drown the picker]")
deep = {"L1": member("UDT_A", internal={"L2": member("UDT_B", internal={
    "L3": member("UDT_C", internal={"L4": member("REAL")})})})}
dtag = {"tag_name": "DEEP", "tag_type": "struct",
        "data_type": {"name": "DEEP_T", "internal_tags": deep}}
dnames = _expand_struct("DEEP", dtag)
check("recursion stops at a sane depth",
      any(n == "DEEP.L1.L2.L3" for n in dnames)
      and not any(n.count(".") > 3 for n in dnames),
      sorted(dnames))
wide = {("M%03d" % i): member("DINT", [10]) for i in range(200)}
wtag = {"tag_name": "WIDE", "tag_type": "struct",
        "data_type": {"name": "WIDE_T", "internal_tags": wide}}
check("  and a very wide UDT is capped", len(_expand_struct("WIDE", wtag)) <= 512,
      len(_expand_struct("WIDE", wtag)))

print()
print("[nothing that already worked has changed]")
check("a plain scalar is still just itself",
      _enumerate_indexed_names("SETPOINT_CONVERTED", []) == ["SETPOINT_CONVERTED"])
check("  a plain array still expands by index",
      _enumerate_indexed_names("SimREAL", [3])
      == ["SimREAL[0]", "SimREAL[1]", "SimREAL[2]"])
check("  a non-struct is not treated as one",
      not _is_struct({"tag_name": "X", "data_type": "REAL"}))
check("  and expanding a non-struct yields nothing",
      _expand_struct("X", {"tag_name": "X", "data_type": "REAL"}) == [])

print()
print("[the candidate builder still offers the whole UDT as well]")
loop = SRC[SRC.index("                for td in tag_defs:"):
           SRC.index("                if not candidates:")]
check("the bare UDT name is still a candidate",
      "_enumerate_indexed_names(base, dims)" in loop,
      "reading the whole structure at once stays legitimate")
check("  members are added only for a SCALAR struct",
      "if is_struct and not dims and len(candidates) < max_tags:" in loop,
      "an array of UDTs would multiply out of hand")
check("  and the max_tags cap still applies",
      loop.count("if len(candidates) >= max_tags:") >= 3)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
