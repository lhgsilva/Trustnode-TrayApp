# -*- coding: utf-8 -*-
"""Power meter register addressing, models and the supplier-table importer.

2026-08-27: an EM122 was added and "the registers do not work". Two causes,
both of which read as silence rather than as an error:

  1. The app shipped Weidmuller EM525 maps only (19000-range). Pointed at an
     EM122 they connect, poll and return 0.0000 for every value.
  2. A datasheet reference is NOT a wire offset. The EM122 table prints
     "30001, 30003, 30005..."; the register carrying Phase 1 volts is offset 0.
     Typing 30005 into the custom-register field read offset 30005, which does
     not exist, so the row sat at "-" and the feature looked broken.

Proven on the meter at 192.168.10.200 (hardware section skips when absent):
    offset  0 -> 239.24 V   (datasheet 30001)
    offset 70 ->  50.03 Hz  (datasheet 30071)
    offset 19000 -> 0.0000  (the EM525 address it was configured with)
"""
from __future__ import annotations

import io
import os
import socket
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

FAILS = []
SKIPS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:140]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print("  {0:58s}: SKIP - {1}".format(name, why))
    SKIPS.append(name)


from app.services.meter_registers import (  # noqa: E402
    normalize_register_address, describe_address, parse_supplier_table,
    METER_MODELS, EM122_SINGLE_PHASE, EM122_ALL, FUNC_INPUT, FUNC_HOLDING,
    format_width,
)

# ------------------------------------------------ 1. address conversion ---
print("[1. a datasheet reference is not a wire offset]")
check("30001 is input register offset 0",
      normalize_register_address(30001) == (0, FUNC_INPUT),
      normalize_register_address(30001))
check("  30005 (the one that stayed blank) is offset 4",
      normalize_register_address(30005) == (4, FUNC_INPUT),
      normalize_register_address(30005))
check("  30071 (frequency) is offset 70",
      normalize_register_address(30071) == (70, FUNC_INPUT))
check("40001 is a HOLDING register at offset 0, function 03",
      normalize_register_address(40001) == (0, FUNC_HOLDING))
check("a plain offset passes through unchanged (EM525 keeps working)",
      normalize_register_address(19000) == (19000, FUNC_INPUT),
      normalize_register_address(19000))
check("  and so does 0",
      normalize_register_address(0) == (0, FUNC_INPUT))
check("explicit 3x:/4x: notation is accepted",
      normalize_register_address("3x:5") == (5, FUNC_INPUT)
      and normalize_register_address("4x:100") == (100, FUNC_HOLDING))
check("the datasheet's hex start-address column works",
      normalize_register_address("0x1E") == (30, FUNC_INPUT))
for bad in ("", None, "not-a-register", -1):
    try:
        normalize_register_address(bad)
        check("rejects {0!r}".format(bad), False, "accepted it")
    except ValueError:
        pass
check("bad input raises rather than reading a wrong register", True)
check("the conversion is explained to the operator",
      "offset 4" in describe_address(30005), describe_address(30005))

# --------------------------------------------------------- 2. the models --
print()
print("[2. pre-loaded supplier models]")
ids = {m["id"] for m in METER_MODELS}
check("an EM122 single-phase model exists",
      "weidmuller_em122_single_phase" in ids, sorted(ids))
check("  a three-phase one too", "weidmuller_em122_three_phase" in ids)
check("  and a full map for anything unusual", "weidmuller_em122_all" in ids)
check("every model names its vendor, model and installation",
      all(m.get("vendor") and m.get("model") and m.get("installation")
          for m in METER_MODELS))
check("the single-phase map offers no L2/L3 registers",
      not any(k.endswith(("_l2_v", "_l3_v", "_l2_a", "_l3_a"))
              for k in EM122_SINGLE_PHASE),
      [k for k in EM122_SINGLE_PHASE if "_l2" in k or "_l3" in k])
check("  because those read a real zero on a 1-phase install", True,
      "showing them fills a dashboard with honest-looking zeros")
check("the full EM122 map covers the datasheet",
      len(EM122_ALL) >= 30, len(EM122_ALL))
dupes = [a for a in EM122_ALL.values() if list(EM122_ALL.values()).count(a) > 1]
check("  with no address used twice", not dupes, sorted(set(dupes))[:4])

from app.services.power_manager import REGISTER_PROFILES  # noqa: E402
check("the models are selectable as register profiles",
      "weidmuller_em122_single_phase" in REGISTER_PROFILES,
      sorted(k for k in REGISTER_PROFILES if "em122" in k))
check("  and the EM525 profiles are untouched",
      "weidmuller_em525_single_phase_basic" in REGISTER_PROFILES
      and REGISTER_PROFILES["weidmuller_em525_single_phase_basic"]["voltage_v"] == 19000)

# ------------------------------------------------ 3. the import assistant -
print()
print("[3. importing a supplier register table]")
TABLE = """
Address (Register)  Description  Length  Data Format  Units  Hi byte  Lo byte
30001  Phase 1 line to neutral volts   4  Float  V     00  00
30003  Phase 2 line to neutral volts   4  Float  V     00  02
30007  Phase 1 current                 4  Float  A     00  06
30031  Phase 1 power factor(1)         4  Float  None  00  1E
30071  Frequency of supply voltages    4  Float  Hz    00  46
"""
res = parse_supplier_table(TABLE)
check("a table pasted out of the datasheet parses",
      res["ok"] and len(res["rows"]) == 5, res.get("message"))
by_addr = {r["address"]: r for r in res["rows"]}
check("  addresses convert to offsets",
      by_addr[30001]["offset"] == 0 and by_addr[30071]["offset"] == 70)
check("  descriptions survive", by_addr[30001]["description"]
      == "Phase 1 line to neutral volts", by_addr[30001]["description"])
check("  footnote markers are stripped from the description",
      by_addr[30031]["description"] == "Phase 1 power factor",
      by_addr[30031]["description"])
check("  units refine the tag key",
      by_addr[30001]["key"].endswith("_v") and by_addr[30071]["key"].endswith("_hz"),
      [by_addr[30001]["key"], by_addr[30071]["key"]])
check("  the header row is not mistaken for a register", 30001 in by_addr
      and len(res["rows"]) == 5)

csv = "30001,Phase 1 line to neutral volts,4,Float,V\n30007,Phase 1 current,4,Float,A"
res_csv = parse_supplier_table(csv)
check("CSV works as well as space-aligned text",
      res_csv["ok"] and len(res_csv["rows"]) == 2, res_csv.get("message"))
tsv = "30001\tPhase 1 line to neutral volts\t4\tFloat\tV"
check("  and TSV", parse_supplier_table(tsv)["ok"])
empty = parse_supplier_table("this text has no register addresses at all")
check("a table with no addresses says so instead of importing nothing",
      not empty["ok"] and "No register rows" in empty["message"],
      empty["message"])
check("duplicate descriptions get distinct keys",
      len(set(r["key"] for r in parse_supplier_table(
          "30001,Volts,4,Float,V\n30003,Volts,4,Float,V")["rows"])) == 2)

check("float32 is two registers wide", format_width("float32") == 2)
check("  int16 is one", format_width("int16") == 1)

# ------------------------------------------------------ 4. real hardware --
print()
print("[4. against the meter on the bench]")
METER = os.environ.get("TRUSTNODE_TEST_METER", "192.168.10.200")


def reachable(host, port=502, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


if not reachable(METER):
    skip("EM122 register read", "{0}:502 not reachable".format(METER))
else:
    from pymodbus.client import ModbusTcpClient
    client = ModbusTcpClient(METER, port=502, timeout=3)
    check("the meter answers Modbus TCP", client.connect())

    def read_f32(offset):
        r = client.read_input_registers(address=int(offset), count=2, slave=1)
        if r.isError():
            return None
        regs = list(r.registers or [])
        if len(regs) < 2:
            return None
        return struct.unpack(">f", struct.pack(">HH", regs[0], regs[1]))[0]

    volts = read_f32(normalize_register_address(30001)[0])
    check("datasheet 30001 reads a real mains voltage",
          volts is not None and 90.0 < volts < 300.0, volts)
    hz = read_f32(normalize_register_address(30071)[0])
    check("datasheet 30071 reads a real mains frequency",
          hz is not None and 45.0 < hz < 65.0, hz)

    # The exact failure the operator reported.
    old_way = read_f32(30005)          # typed literally, unconverted
    new_way = read_f32(normalize_register_address(30005)[0])
    check("the OLD behaviour (30005 as an offset) reads nothing usable",
          old_way is None or old_way == 0.0, old_way)
    check("  while the converted address is a real register",
          new_way is not None, new_way)

    # And the map that was actually configured.
    em525 = read_f32(19000)
    check("the EM525 address returns 0.0 on this meter (the silent failure)",
          em525 == 0.0 or em525 is None, em525)

    good = 0
    for key, addr in EM122_SINGLE_PHASE.items():
        if read_f32(normalize_register_address(addr)[0]) is not None:
            good += 1
    check("every register in the EM122 single-phase model reads",
          good == len(EM122_SINGLE_PHASE),
          "{0}/{1}".format(good, len(EM122_SINGLE_PHASE)))
    client.close()

# ------------------------------------------------------- 5. the API ------
print()
print("[5. through the real API]")
import json as _json
import subprocess as _sp
import tempfile as _tf
import time as _t
import urllib.error as _ue
import urllib.request as _ur

_PORT = "8092"
_API = "http://127.0.0.1:" + _PORT
_tmp = _tf.mkdtemp(prefix="tn-pwrmodels-")
_env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_tmp,
            TRUSTNODE_APP_STORE_PATH=os.path.join(_tmp, "s.db"),
            TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=_PORT)
_proc = _sp.Popen([sys.executable, "-m", "app"],
                  cwd=os.path.join(ROOT, "backend"), env=_env,
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
for _ in range(60):
    try:
        _ur.urlopen(_API + "/api/health", timeout=3).read()
        break
    except Exception:
        _t.sleep(2)


def _call(method, path, token=None, body=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    d = _json.dumps(body).encode() if body is not None else None
    rq = _ur.Request(_API + path, data=d, headers=h, method=method)
    try:
        with _ur.urlopen(rq, timeout=60) as r:
            return r.status, _json.loads(r.read().decode() or "null")
    except _ue.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:150]


_st, _b = _call("POST", "/api/auth/login",
                body={"username": "admin", "password": "admin"})
_tok = (_b or {}).get("token") if isinstance(_b, dict) else None
check("admin login", _st == 200 and bool(_tok))

_st, _m = _call("GET", "/api/power/meter-models", _tok)
_models = (_m or {}).get("models") or []
check("the UI can list meter models", _st == 200 and len(_models) >= 3,
      "{0} model(s)".format(len(_models)))
check("  each carries a preview of its addresses",
      all(mm.get("preview") for mm in _models))
check("  and says how an address will be read",
      any("offset" in (pv.get("reads_as") or "")
          for mm in _models for pv in mm.get("preview") or []),
      (_models[0].get("preview") or [{}])[0].get("reads_as") if _models else "")

_st, _p = _call("POST", "/api/power/parse-register-table", _tok,
                {"text": "30001,Phase 1 line to neutral volts,4,Float,V\n30071,Frequency of supply voltages,4,Float,Hz"})
check("the UI can import a supplier table",
      _st == 200 and (_p or {}).get("ok") and len(_p.get("rows") or []) == 2,
      (_p or {}).get("message"))
check("  and is told how each address resolves",
      all("offset" in (r.get("reads_as") or "") for r in (_p or {}).get("rows") or []))

_st, _h = _call("GET", "/api/power/address-help?address=30005", _tok)
check("the address helper explains the conversion",
      _st == 200 and (_h or {}).get("offset") == 4, (_h or {}).get("message"))
_st, _bad = _call("GET", "/api/power/address-help?address=nonsense", _tok)
check("  and refuses nonsense", _st == 200 and not (_bad or {}).get("ok"))

_st, _pf = _call("GET", "/api/power/profiles", _tok)
_names = list(((_pf or {}).get("profiles") or {}).keys())
check("the EM122 maps appear in the profile list",
      any("em122" in n for n in _names), [n for n in _names if "em122" in n])

try:
    _proc.terminate(); _proc.wait(timeout=20)
except Exception:
    _proc.kill()

# ------------------------------------------ 6. the UI must PERSIST -------
print()
print("[6. a register change must be saved, not just shown]")
_app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
               encoding="utf-8", errors="replace").read()

# 2026-08-28: applying the EM122 map updated React state only. The table showed
# the new addresses while the SAVED config still held the EM525 map, so the
# poller went on reading 19000-range registers and every row displayed "-".
# A register edit that does not reach savePowerConfigPayload is not an edit.
check("applying a register map persists it",
      "applyRegisterMap" in _app
      and "await savePowerConfigPayload(nextConfig)" in _app.split("applyRegisterMap")[1][:2200])
check("  and marks the map as the operator's own",
      "use_custom_registers: true" in _app.split("applyRegisterMap")[1][:2200])
check("adding a register persists it",
      "persistPowerDeviceRegisters" in _app
      and "persistPowerDeviceRegisters((d) => ({" in _app)
_remove_body = _app.split("const removePowerRegisterRow")[1][:900]     if "const removePowerRegisterRow" in _app else ""
check("removing a register persists it",
      "persistPowerDeviceRegisters(" in _remove_body)
check("  and no register edit sets state without saving",
      "setPowerConfig((prev) => {" not in _remove_body)
check("the shared helper saves through the normal path",
      "await savePowerConfigPayload(nextConfig)" in
      _app.split("const persistPowerDeviceRegisters")[1][:1200])
check("a failed save is reported, not swallowed",
      "Register change NOT saved" in _app and "Register map NOT saved" in _app)

print()
if SKIPS:
    print("SKIPPED: {0}".format(", ".join(SKIPS)))
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
