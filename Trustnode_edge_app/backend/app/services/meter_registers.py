# -*- coding: utf-8 -*-
"""Modbus register addressing and supplier register maps for power meters.

Two jobs, both learned the hard way on a Weidmuller EM122 (2026-08-27):

1. ADDRESS CONVENTION. A supplier datasheet lists "30001, 30003, 30005...".
   Those are 1-based 3x references, NOT wire offsets. The register that
   actually carries "Phase 1 line to neutral volts" is offset 0. Typing 30005
   into the register field read offset 30005, which does not exist, so the row
   sat at "-" for ever and the feature looked broken.

   Proven against the meter at 192.168.10.200:
       offset  0 -> 239.24 V      (supplier 30001)
       offset 70 ->  50.03 Hz     (supplier 30071)
   while the EM525 profile's 19000-range addresses all returned 0.0000.

2. ONE PROFILE DOES NOT FIT EVERY METER. The app shipped only EM525 maps
   (19000-range). Pointing them at an EM122 connects, reads, and returns
   zeros - the worst kind of failure, because nothing errors.

Everything here is pure: no I/O, so the conversions are testable on their own.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Modbus data-model prefixes, as printed on every vendor datasheet.
#   3xxxx = input registers    (function code 04)
#   4xxxx = holding registers  (function code 03)
INPUT_BASE = 30001
INPUT_MAX = 39999
HOLDING_BASE = 40001
HOLDING_MAX = 49999

FUNC_INPUT = "input"      # FC04
FUNC_HOLDING = "holding"  # FC03


def normalize_register_address(value: Any) -> Tuple[int, str]:
    """A datasheet reference -> (wire offset, function).

    Accepts what an operator actually has in front of them:

        30001      -> (0, input)     1-based 3x reference
        40001      -> (0, holding)   1-based 4x reference
        "3x:5"     -> (5, input)     explicit, already an offset
        "4x:100"   -> (100, holding)
        "0x1E"     -> (30, input)    the datasheet's hex start-address column
        19000      -> (19000, input) a plain offset, unchanged

    The plain-offset fallback is what keeps every existing EM525 configuration
    working: 19000 is below the 3x range, so it passes through untouched.
    """
    if value is None:
        raise ValueError("register address is empty")

    if isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        if not text:
            raise ValueError("register address is empty")
        m = re.match(r"^([34])x:?(\d+)$", text)
        if m:
            off = int(m.group(2))
            return off, (FUNC_INPUT if m.group(1) == "3" else FUNC_HOLDING)
        if text.startswith("0x"):
            return int(text, 16), FUNC_INPUT
        try:
            value = int(float(text))
        except ValueError:
            raise ValueError(f"'{value}' is not a register address")

    n = int(value)
    if n < 0:
        raise ValueError("register address cannot be negative")
    if INPUT_BASE <= n <= INPUT_MAX:
        return n - INPUT_BASE, FUNC_INPUT
    if HOLDING_BASE <= n <= HOLDING_MAX:
        return n - HOLDING_BASE, FUNC_HOLDING
    return n, FUNC_INPUT


def describe_address(value: Any) -> str:
    """How an address will actually be read - shown next to the field so the
    conversion is never a surprise."""
    try:
        off, func = normalize_register_address(value)
    except ValueError as exc:
        return str(exc)
    if str(value).strip().isdigit() and INPUT_BASE <= int(value) <= HOLDING_MAX:
        kind = "input register" if func == FUNC_INPUT else "holding register"
        return f"{value} = {kind} offset {off} (function code {'04' if func == FUNC_INPUT else '03'})"
    return f"offset {off}, function code {'04' if func == FUNC_INPUT else '03'}"


# ---------------------------------------------------------------------------
# Data formats
# ---------------------------------------------------------------------------
# width in registers (16-bit words) per format
FORMAT_WIDTH: Dict[str, int] = {
    "float32": 2, "float32_le": 2,
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2,
    "int64": 4, "uint64": 4,
    "float64": 4,
}
DEFAULT_FORMAT = "float32"


def format_width(fmt: str) -> int:
    return FORMAT_WIDTH.get(str(fmt or DEFAULT_FORMAT).lower(), 2)


# ---------------------------------------------------------------------------
# Supplier register maps, keyed by model
# ---------------------------------------------------------------------------
# Addresses are written EXACTLY as the datasheet prints them, so a map can be
# checked against the PDF line by line. normalize_register_address turns them
# into wire offsets.
#
# EM122: Weidmuller, input registers, function code 04, all Float (4 bytes).
# Transcribed from the supplier's "Input Registers, Function code 04" table.
EM122_ALL: Dict[str, Any] = {
    "voltage_l1_v": 30001,
    "voltage_l2_v": 30003,
    "voltage_l3_v": 30005,
    "current_l1_a": 30007,
    "current_l2_a": 30009,
    "current_l3_a": 30011,
    "active_power_l1_w": 30013,
    "active_power_l2_w": 30015,
    "active_power_l3_w": 30017,
    "apparent_power_l1_va": 30019,
    "apparent_power_l2_va": 30021,
    "apparent_power_l3_va": 30023,
    "reactive_power_l1_var": 30025,
    "reactive_power_l2_var": 30027,
    "reactive_power_l3_var": 30029,
    "power_factor_l1": 30031,
    "power_factor_l2": 30033,
    "power_factor_l3": 30035,
    "phase_angle_l1_deg": 30037,
    "phase_angle_l2_deg": 30039,
    "phase_angle_l3_deg": 30041,
    "voltage_avg_v": 30043,
    "current_avg_a": 30047,
    "current_sum_a": 30049,
    "active_power_total_w": 30053,
    "apparent_power_total_va": 30057,
    "reactive_power_total_var": 30061,
    "power_factor_total": 30063,
    "phase_angle_total_deg": 30067,
    "frequency_hz": 30071,
    "energy_import_wh": 30073,
    "energy_export_wh": 30075,
    "energy_import_varh": 30077,
}

# The single-phase (1Ø 2W) subset the datasheet marks with a dot in the last
# column - offering L2/L3 on a single-phase install is how a dashboard ends up
# full of honest-looking zeros.
EM122_SINGLE_PHASE: Dict[str, Any] = {
    "voltage_v": 30001,
    "current_a": 30007,
    "active_power_w": 30013,
    "apparent_power_va": 30019,
    "reactive_power_var": 30025,
    "power_factor": 30031,
    "frequency_hz": 30071,
    "energy_wh": 30073,
}

EM122_THREE_PHASE: Dict[str, Any] = {
    "voltage_l1_v": 30001,
    "voltage_l2_v": 30003,
    "voltage_l3_v": 30005,
    "current_l1_a": 30007,
    "current_l2_a": 30009,
    "current_l3_a": 30011,
    "active_power_total_w": 30053,
    "apparent_power_total_va": 30057,
    "reactive_power_total_var": 30061,
    "power_factor_total": 30063,
    "frequency_hz": 30071,
    "energy_total_wh": 30073,
}

# Human labels for the tag keys above, used by the register table.
REGISTER_LABELS: Dict[str, str] = {
    "voltage_v": "Voltage L-N", "voltage_l1_v": "Phase 1 line to neutral volts",
    "voltage_l2_v": "Phase 2 line to neutral volts",
    "voltage_l3_v": "Phase 3 line to neutral volts",
    "voltage_avg_v": "Average line to neutral volts",
    "current_a": "Current", "current_l1_a": "Phase 1 current",
    "current_l2_a": "Phase 2 current", "current_l3_a": "Phase 3 current",
    "current_avg_a": "Average line current", "current_sum_a": "Sum of line currents",
    "active_power_w": "Active power", "active_power_l1_w": "Phase 1 active power",
    "active_power_l2_w": "Phase 2 active power", "active_power_l3_w": "Phase 3 active power",
    "active_power_total_w": "Total system power",
    "apparent_power_va": "Apparent power", "apparent_power_total_va": "Total system volt amps",
    "reactive_power_var": "Reactive power", "reactive_power_total_var": "Total system VAr",
    "power_factor": "Power factor", "power_factor_l1": "Phase 1 power factor",
    "power_factor_total": "Total system power factor",
    "frequency_hz": "Frequency of supply voltages",
    "energy_wh": "Import Wh since last reset",
    "energy_import_wh": "Import Wh since last reset",
    "energy_export_wh": "Export Wh since last reset",
    "energy_total_wh": "Import Wh since last reset",
}

# What the operator picks from when adding a meter. `installation` describes
# the wiring the map assumes, because the same model wired 1Ø 2W and 3Ø 4W does
# not expose the same registers.
METER_MODELS: List[Dict[str, Any]] = [
    {
        "id": "weidmuller_em122_single_phase",
        "vendor": "Weidmuller", "model": "EM122",
        "installation": "1-phase, 2-wire (1Ø 2W)",
        "function": FUNC_INPUT, "format": "float32",
        "notes": "Input registers, function code 04, Float. Datasheet numbering "
                 "(30001...) is converted to wire offsets automatically.",
        "registers": EM122_SINGLE_PHASE,
    },
    {
        "id": "weidmuller_em122_three_phase",
        "vendor": "Weidmuller", "model": "EM122",
        "installation": "3-phase, 4-wire (3Ø 4W)",
        "function": FUNC_INPUT, "format": "float32",
        "notes": "Input registers, function code 04, Float.",
        "registers": EM122_THREE_PHASE,
    },
    {
        "id": "weidmuller_em122_all",
        "vendor": "Weidmuller", "model": "EM122",
        "installation": "3-phase, every documented register",
        "function": FUNC_INPUT, "format": "float32",
        "notes": "Everything the EM122 datasheet lists. Trim what you do not need.",
        "registers": EM122_ALL,
    },
]


# ---------------------------------------------------------------------------
# Supplier table import
# ---------------------------------------------------------------------------
# Operators have the register list as a table - a PDF page, a spreadsheet, a
# copy-paste. Retyping 33 rows is where mistakes come from, so parse it.
_ADDR_RE = re.compile(r"\b(3\d{4}|4\d{4})\b")
_NUM_RE = re.compile(r"^\d+$")


def _slug(text: str) -> str:
    """A tag key from a datasheet description."""
    t = str(text or "").strip().lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t or "register"


_UNIT_SUFFIX = {
    "v": "_v", "a": "_a", "w": "_w", "va": "_va", "var": "_var",
    "hz": "_hz", "kwh": "_wh", "wh": "_wh", "kvarh": "_varh",
    "varh": "_varh", "degrees": "_deg",
}


def parse_supplier_table(text: str, max_rows: int = 400) -> Dict[str, Any]:
    """Turn a pasted supplier register table into registers we can read.

    Deliberately tolerant: vendor tables arrive as CSV, TSV, or text pasted out
    of a PDF where the columns are just runs of spaces. The rule that survives
    all three is "a line containing a 3xxxx/4xxxx address is a register row;
    the longest run of letters on it is the description; a trailing unit token
    refines the tag name".

    Returns {registers, rows, skipped, message} - never raises on a messy line,
    because one unparseable row must not lose the other 32.
    """
    registers: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    skipped = 0
    seen_keys: Dict[str, int] = {}

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or len(rows) >= max_rows:
            continue
        m = _ADDR_RE.search(line)
        if not m:
            continue
        address = int(m.group(1))

        # Split on tabs/commas when present, otherwise on runs of 2+ spaces.
        if "\t" in line:
            cells = [c.strip() for c in line.split("\t")]
        elif line.count(",") >= 2:
            cells = [c.strip() for c in line.split(",")]
        else:
            cells = [c.strip() for c in re.split(r"\s{2,}", line)]
        cells = [c for c in cells if c]

        # The description is the longest cell that is not just a number.
        desc_cells = [c for c in cells
                      if not _NUM_RE.match(c) and not _ADDR_RE.fullmatch(c)]
        description = max(desc_cells, key=len) if desc_cells else f"Register {address}"
        description = re.sub(r"\(\d+\)$", "", description).strip()

        # A short alphabetic cell that looks like a unit refines the key.
        unit = ""
        for c in cells:
            token = c.strip().lower()
            if token in _UNIT_SUFFIX and token != description.strip().lower():
                unit = token
                break

        key = _slug(description)
        suffix = _UNIT_SUFFIX.get(unit, "")
        if suffix and not key.endswith(suffix):
            key = key + suffix
        if key in seen_keys:
            seen_keys[key] += 1
            key = f"{key}_{seen_keys[key]}"
        else:
            seen_keys[key] = 1

        offset, func = normalize_register_address(address)
        registers[key] = address
        rows.append({
            "key": key, "address": address, "offset": offset,
            "function": func, "description": description,
            "unit": unit.upper() if unit else "",
        })

    if not rows:
        return {"ok": False, "registers": {}, "rows": [], "skipped": skipped,
                "message": "No register rows found. Paste the supplier table "
                           "including its address column (30001, 30003, ...)."}
    return {
        "ok": True, "registers": registers, "rows": rows, "skipped": skipped,
        "message": (f"{len(rows)} register(s) read from the supplier table. "
                    f"Addresses are datasheet numbering and are converted to "
                    f"wire offsets automatically."),
    }
