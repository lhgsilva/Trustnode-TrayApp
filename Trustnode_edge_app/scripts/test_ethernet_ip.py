# -*- coding: utf-8 -*-
"""Generic EtherNet/IP driver: EDS parsing, assembly decoding, and the CIP read
path against a stubbed pycomm3. No hardware, no product wiring."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.drivers.ethernet_ip import (  # noqa: E402
    AssemblyDecodeError, EdsParseError, EipDeviceClient, EipSignal,
    decode_signal, guess_assemblies, parse_eds, signals_from_config,
)

FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:120]) if detail else ''}")
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------------------- EDS
print("[EDS]")

# Shaped like a real vendor file: $ comments, quoted strings, entries ending
# in ';', and assembly entries with the size as the first bare number.
EDS = """
$ EDS for a test device
[File]
    DescText = "Test device EDS";
    CreateDate = 01-01-2026;

[Device]
    VendCode = 310;
    VendName = "ifm electronic gmbh";
    ProdType = 12;
    ProdCode = 4711;
    MajRev = 1;
    MinRev = 1;
    ProdName = "AL1326";
    Catalog = "AL1326";

[Assembly]
    Assem100 =
        "Output Data",
        ,
        32,
        0x0000,
        ,,
        4,
        "Data";
    Assem101 =
        "Input Data",
        ,
        64,
        0x0000,
        ,,
        4,
        "Data";
    Assem102 =
        "Config Data",
        ,
        10,
        0x0000;
"""

parsed = parse_eds(EDS)
check("vendor id", parsed["vendor_id"] == 310, parsed["vendor_id"])
check("vendor name", parsed["vendor_name"] == "ifm electronic gmbh", parsed["vendor_name"])
check("product name", parsed["product_name"] == "AL1326", parsed["product_name"])
check("product code", parsed["product_code"] == 4711, parsed["product_code"])
check("finds all three assemblies", len(parsed["assemblies"]) == 3,
      [a["instance"] for a in parsed["assemblies"]])
inp = next(a for a in parsed["assemblies"] if a["instance"] == 101)
check("assembly name", inp["name"] == "Input Data", inp)
check("assembly SIZE in bytes (not the 0x0000 that follows it)",
      inp["size_bytes"] == 64, inp["size_bytes"])

guess = guess_assemblies(parsed)
check("input assembly guessed from its name", guess.get("input_assembly") == 101, guess)
check("output assembly guessed", guess.get("output_assembly") == 100, guess)
check("config assembly guessed", guess.get("config_assembly") == 102, guess)

# a file with no direction words still offers the biggest as input
plain = parse_eds("""
[Device]
    VendCode = 1;
    ProdName = "Drive";
[Assembly]
    Assem20 = "Params", , 4, 0x0000;
    Assem70 = "Status", , 32, 0x0000;
""")
check("without direction words it offers the largest assembly",
      guess_assemblies(plain).get("input_assembly") == 70, guess_assemblies(plain))

# comments must not eat quoted '$'
q = parse_eds('[Device]\n VendCode = 5;\n ProdName = "A$B";\n[Assembly]\n Assem1 = "In", , 8, 0;\n')
check("a '$' inside quotes is not treated as a comment", q["product_name"] == "A$B",
      q["product_name"])

try:
    parse_eds("not an eds at all")
    check("a non-EDS file is refused", False, "no error")
except EdsParseError:
    check("a non-EDS file is refused", True)


# -------------------------------------------------------------- decoding
print("\n[ASSEMBLY DECODING]")
# CIP assembly data is little-endian.
data = struct.pack("<hHif", -1234, 40000, 70000, 3.5) + bytes([0b00000101])
# offsets:            0      2      4      8            12

check("INT (signed, little-endian)", decode_signal(data, EipSignal("a", 0, "INT")) == -1234.0,
      decode_signal(data, EipSignal("a", 0, "INT")))
check("UINT", decode_signal(data, EipSignal("b", 2, "UINT")) == 40000.0)
check("DINT", decode_signal(data, EipSignal("c", 4, "DINT")) == 70000.0)
check("REAL", abs(decode_signal(data, EipSignal("d", 8, "REAL")) - 3.5) < 1e-6)
check("scale and offset applied",
      decode_signal(data, EipSignal("e", 0, "INT", scale=0.1, offset=5.0)) == -118.4,
      decode_signal(data, EipSignal("e", 0, "INT", scale=0.1, offset=5.0)))
check("BOOL bit 0 set", decode_signal(data, EipSignal("f", 12, "BOOL", bit=0)) is True)
check("BOOL bit 1 clear", decode_signal(data, EipSignal("g", 12, "BOOL", bit=1)) is False)
check("BOOL bit 2 set", decode_signal(data, EipSignal("h", 12, "BOOL", bit=2)) is True)

try:
    decode_signal(data, EipSignal("over", 12, "DINT"))
    check("a signal past the end is refused", False, "no error")
except AssemblyDecodeError as exc:
    check("a signal past the end is refused", "only 13 bytes" in str(exc), str(exc)[:80])
try:
    decode_signal(data, EipSignal("bad", 0, "WIDGET"))
    check("an unknown type is refused", False, "no error")
except AssemblyDecodeError as exc:
    check("an unknown type is refused", "unknown type" in str(exc), str(exc)[:60])

sigs = signals_from_config([
    {"name": "Speed", "byte_offset": 0, "kind": "INT", "scale": 0.1, "unit": "rpm"},
    {"name": "", "byte_offset": 2, "kind": "INT"},          # unnamed -> dropped
    {"byte_offset": 4},                                      # nameless -> dropped
])
check("config keeps only named signals", len(sigs) == 1 and sigs[0].name == "Speed",
      [s.name for s in sigs])


# ------------------------------------------------------------ CIP read path
print("\n[CIP READ against a stubbed pycomm3]")

import types  # noqa: E402

calls = {"n": 0, "args": None}


class _Reply:
    def __init__(self, value): self.value = value; self.error = None
    def __bool__(self): return True


class _FailReply:
    value = None
    error = "Connection timed out"
    def __bool__(self): return False


class _StubDriver:
    def __init__(self, path): self.path = path
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def generic_message(self, **kw):
        calls["n"] += 1
        calls["args"] = kw
        if calls.get("fail"):
            return _FailReply()
        return _Reply(data)


stub = types.ModuleType("pycomm3")
stub.CIPDriver = _StubDriver
stub.Services = types.SimpleNamespace(get_attribute_single=b"\x0e")
stub.ClassCode = types.SimpleNamespace(assembly=b"\x04")
sys.modules["pycomm3"] = stub

client = EipDeviceClient(host="192.168.1.250")
raw = client.read_assembly(101)
check("reads the assembly bytes", raw == data, len(raw))
check("asks the ASSEMBLY class", calls["args"]["class_code"] == b"\x04", calls["args"]["class_code"])
check("asks instance 101", calls["args"]["instance"] == 101, calls["args"]["instance"])
check("asks attribute 3 (the data)", calls["args"]["attribute"] == 3, calls["args"]["attribute"])
check("uses UNCONNECTED (explicit) messaging", calls["args"]["connected"] is False,
      calls["args"]["connected"])

calls["n"] = 0
rows = client.read_signals(101, [
    EipSignal("Speed", 0, "INT", scale=0.1, unit="rpm"),
    EipSignal("Count", 4, "DINT"),
    EipSignal("Running", 12, "BOOL", bit=0),
])
check("one assembly read serves EVERY signal", calls["n"] == 1, calls["n"])
by = {r["name"]: r for r in rows}
check("Speed decoded", abs(by["Speed"]["value"] - (-123.4)) < 1e-6, by["Speed"]["value"])
check("Count decoded", by["Count"]["value"] == 70000.0, by["Count"]["value"])
check("Running decoded as a bit", by["Running"]["value"] == 1.0 and by["Running"]["is_bool"],
      by["Running"])
check("good signals carry GOOD quality", all(r["quality"] == 192 for r in rows))

# a signal that does not fit is reported on its own, not as a dead device
rows = client.read_signals(101, [EipSignal("Ok", 0, "INT"), EipSignal("TooFar", 200, "INT")])
check("a bad signal map fails only that signal",
      by_ok := (rows[0]["quality"] == 192 and rows[1]["quality"] == 0), rows[1]["error"])

# a device that stops answering marks every tag BAD with the reason
calls["fail"] = True
rows = client.read_signals(101, [EipSignal("Speed", 0, "INT")])
check("an unreachable device marks tags BAD, with the reason",
      rows[0]["quality"] == 0 and bool(rows[0]["error"]), rows[0]["error"])
calls["fail"] = False

print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
