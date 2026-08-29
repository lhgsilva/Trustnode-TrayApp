# -*- coding: utf-8 -*-
"""The device catalogue and CIP parameter reads.

2026-08-28, Phases 2-4 of `docs/FIELD_DEVICE_DRIVER_FRAMEWORK_2026-08-28.md`.

The rule the catalogue exists to enforce: **a profile that has not been proven
against hardware must not arrive collecting.** On Modbus and CIP alike a wrong
address or parameter number returns a plausible NUMBER rather than an error, so
an unverified profile is a hypothesis. It applies with its tags UNTICKED, and
the UI says so.

That distinction is the one thing here that cannot be checked by reading the
device - only by reading the code - so it is checked by reading the code.
"""
import io
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:140]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8", errors="replace").read()


# ------------------------------------------------------------- catalogue
print("[the catalogue]")
from app.services import device_profiles  # noqa: E402

profiles = device_profiles.list_profiles()
check("profiles are published", len(profiles) >= 6, len(profiles))
check("  every profile declares a protocol",
      all(p.get("protocol") for p in profiles))
check("  and carries its tag list",
      all(p.get("tag_count", 0) > 0 and p.get("tags") for p in profiles))
check("  filtering by protocol works",
      len(device_profiles.list_profiles("modbus_tcp")) >= 3
      and len(device_profiles.list_profiles("ethernet_ip")) >= 3,
      "modbus=%d eip=%d" % (len(device_profiles.list_profiles("modbus_tcp")),
                            len(device_profiles.list_profiles("ethernet_ip"))))

# Every profile is either proven or explicitly not, and says why either way.
unmarked = [p["id"] for p in profiles if "verified" not in p]
check("every profile states whether it is verified", not unmarked, unmarked)
unexplained = [p["id"] for p in profiles if not p.get("verified") and not p.get("notes")]
check("  and an UNVERIFIED one explains itself", not unexplained, unexplained)

verified = [p for p in profiles if p.get("verified")]
check("  the EM122 profiles are the verified ones",
      {p["id"] for p in verified} == {"weidmuller-em122-3ph", "weidmuller-em122-1ph"},
      sorted(p["id"] for p in verified))

# The catalogue must hand out copies; an edit in the dialog cannot be allowed
# to mutate the library for every gateway configured afterwards.
first = device_profiles.list_profiles("modbus_tcp")[0]
first["tags"][0]["name"] = "MUTATED"
again = device_profiles.list_profiles("modbus_tcp")[0]
check("  a profile is handed out as a copy, not a reference",
      again["tags"][0]["name"] != "MUTATED", again["tags"][0]["name"])

check("an unknown id returns nothing", device_profiles.get_profile("nope") is None)

# The EM122 profile must agree with the register map it was built from.
from app.services.meter_registers import EM122_THREE_PHASE  # noqa: E402
em = device_profiles.get_profile("weidmuller-em122-3ph")
addrs = {t["name"]: t["address"] for t in em["tags"]}
check("  the EM122 profile matches the verified register map",
      all(str(EM122_THREE_PHASE[k]) == addrs.get(k) for k in EM122_THREE_PHASE),
      "%d tag(s)" % len(addrs))
check("  and units were inferred from the names",
      addrs and em["tags"][0].get("unit") == "V", em["tags"][0])

# --------------------------------------------------- CIP parameter object
print()
print("[CIP parameter object]")
from app.drivers.ethernet_ip import (  # noqa: E402
    PARAMETER_CLASS, decode_parameter, parameters_from_config)

check("the ODVA parameter class is used", PARAMETER_CLASS == 0x0F, hex(PARAMETER_CLASS))
check("REAL decodes little-endian, as CIP specifies",
      abs(decode_parameter(struct.pack("<f", 50.02), "REAL") - 50.02) < 0.001)
check("  INT is signed",
      decode_parameter(struct.pack("<h", -1234), "INT") == -1234.0)
check("  UINT is not",
      decode_parameter(struct.pack("<H", 65535), "UINT") == 65535.0)
check("  DINT spans four bytes",
      decode_parameter(struct.pack("<i", -70000), "DINT") == -70000.0)
try:
    decode_parameter(b"\x01", "DINT")
    check("  a short reply is refused, not padded", False, "no error raised")
except Exception as exc:
    check("  a short reply is refused, not padded", True, str(exc)[:80])

rows = parameters_from_config([
    {"name": "Output_Freq", "param": 1, "kind": "REAL", "scale": 0.01, "unit": "Hz"},
    {"name": "Off", "param": 2, "enabled": False},
    {"name": "", "param": 3},
    {"name": "NoNumber"},
    {"name": "BadKind", "param": 4, "kind": "NONSENSE"},
])
check("config rows become parameters, junk skipped",
      [r["name"] for r in rows] == ["Output_Freq", "BadKind"],
      [r["name"] for r in rows])
check("  an unknown type falls back rather than failing",
      rows[1]["kind"] == "INT", rows[1]["kind"])

# ------------------------------------------------------- the UNTICKED rule
print()
print("[an unverified profile must not arrive collecting]")
for mapper in ("ModbusMapper.jsx", "EthernetIpMapper.jsx"):
    src = read(ROOT, "frontend", "src", "components", "Gateways", mapper)
    check("%s applies profile tags as enabled=verified" % mapper,
          "enabled: Boolean(profile.verified)" in src,
          "an unproven address must not start writing to the historian")

cat = read(ROOT, "frontend", "src", "components", "Gateways", "DeviceCatalogue.jsx")
check("the picker shows whether a profile is verified",
      "Verified on hardware" in cat and "Not verified" in cat)
check("  and says so in words, not only in colour",
      'catalogue-badge ${chosen.verified ? "ok" : "warn"}' in cat
      and "chosen.verified ? \"Verified on hardware\" : \"Not verified\"" in cat)

# The drive path must reach the backend, or a PowerFlex silently collects
# nothing - the exact trap that cost a day on 2026-08-28.
app = read(ROOT, "frontend", "src", "App.jsx")
check("eip_parameters is carried by the Start payload",
      "eip_parameters: Array.isArray(gateway.eip_parameters)" in app)
check("  saved with the gateway",
      "eip_parameters: Array.isArray(gatewayForm.eip_parameters)" in app)
check("  and restored when reopening it",
      app.count("eip_parameters: Array.isArray(gateway.eip_parameters)") >= 2)

plc = read(ROOT, "backend", "app", "services", "plc_manager.py")
check("a drive with only parameters and no assembly still starts",
      "if not signals and not params:" in plc,
      "an assembly must not be required when parameters are mapped")
check("  and parameter reads are bounded by a deadline",
      "read_parameters(params, deadline_s=budget)" in plc,
      "each parameter is its own CIP request, unlike a single assembly read")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
