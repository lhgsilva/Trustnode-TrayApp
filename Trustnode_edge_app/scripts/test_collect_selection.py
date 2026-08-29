# -*- coding: utf-8 -*-
"""Choosing WHAT to collect - ifm/EtherNet-IP tags and power-meter registers.

2026-08-28, for "be able to select what tag we want to see, collected,
monitored, and when we remove some of them it still should work fine" and the
same for meters, "we might not need all the values to be collected and send to
the database".

The shared rule both sides follow: **the map is never trimmed, the selection
is separate.** Unticking a value keeps its address / byte offset / scale, so
turning it back on costs nothing. An absent flag means ticked, so nothing
configured before this existed changes behaviour.

The trap this guards: an EMPTY tag list means "no filter" to the gateway read
path, so unticking everything would collect EVERYTHING - the exact inverse of
the operator's intent. The UI must refuse that save.
"""
import io
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


def read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- meters
print("[power meters - only the ticked registers are polled]")
from app.services.power_manager import enabled_registers, PowerManager  # noqa: E402

FULL = {"voltage_v": 0, "current_a": 6, "power_w": 12, "freq_hz": 70}

check("no selection at all collects the whole map",
      enabled_registers({"registers": FULL}) == FULL)
check("  an empty selection map is the same as none",
      enabled_registers({"registers": FULL, "register_enabled": {}}) == FULL)

trimmed = enabled_registers({"registers": FULL,
                             "register_enabled": {"current_a": False, "freq_hz": False}})
check("unticked registers are not polled",
      sorted(trimmed) == ["power_w", "voltage_v"], sorted(trimmed))
check("  the ticked ones keep their addresses",
      trimmed.get("voltage_v") == 0 and trimmed.get("power_w") == 12, trimmed)

check("a key ticked back ON is collected again",
      "current_a" in enabled_registers(
          {"registers": FULL, "register_enabled": {"freq_hz": False}}))
check("  unticking everything polls nothing",
      enabled_registers({"registers": FULL,
                         "register_enabled": {k: False for k in FULL}}) == {})

# The address map must SURVIVE a round trip through the config normalizer -
# that is what makes re-ticking free. Losing it here would mean re-importing
# the supplier's table to turn one value back on.
mgr = PowerManager.__new__(PowerManager)
dev = PowerManager._normalize_device(mgr, {
    "id": "m1", "name": "M1", "use_custom_registers": True,
    "registers": dict(FULL),
    "register_enabled": {"current_a": False},
})
check("an unticked register keeps its address in the saved config",
      dev["registers"].get("current_a") == 6, dev["registers"].get("current_a"))
check("  and its OFF state is recorded",
      dev["register_enabled"] == {"current_a": False}, dev["register_enabled"])
check("  while ticked keys are not recorded at all",
      "voltage_v" not in dev["register_enabled"], dev["register_enabled"])
check("  so the poller sees exactly the ticked ones",
      sorted(enabled_registers(dev)) == ["freq_hz", "power_w", "voltage_v"],
      sorted(enabled_registers(dev)))

# ------------------------------------------------------------ ifm / EIP
print()
print("[EtherNet/IP - only the ticked signals become tags]")
mapper = read(ROOT, "frontend", "src", "components", "Gateways", "EthernetIpMapper.jsx")

check("eipTagNames filters on the tick",
      ".filter((s) => s && s.enabled !== false)" in mapper)
check("  an undefined flag still counts as ticked",
      "enabled !== false" in mapper,
      "otherwise every gateway saved before this stops collecting")
check("  the row carries a Collect checkbox",
      'patchSignal(idx, { enabled: e.target.checked })' in mapper)
check("  with All / None and per-port shortcuts",
      "setAllTicks" in mapper and "tickPortOnly" in mapper)
check("  a signal added by hand arrives ticked",
      'unit: "", enabled: true,' in mapper)

# 3568 bit-tags on a 446-byte assembly is a mapping aid, not a collection plan.
check("a bulk bit map above 32 tags arrives UNticked",
      "const BULK = 32;" in mapper and "enabled: tickAll" in mapper)

# The inverse-of-intent trap.
app = read(ROOT, "frontend", "src", "App.jsx")
check("saving with nothing ticked is refused",
      "gatewayMapperOwnsTags && !tags.length" in app,
      "an empty tag list means NO FILTER to the backend, i.e. collect everything")
check("  and the refusal says why",
      "collects every mapped value instead of none" in app)

# Removing a tag must not silently break what pointed at it.
check("dropping a tag names what stops updating",
      "const tagReferences = useCallback" in app and "tagReferences(editingGatewayId, dropped)" in app)
check("  covering widgets, limit rules and collection triggers",
      "Dashboard widget" in app and "Limit rule on" in app and "Collection trigger on" in app)

# The backend side of "removing some still works": the read path filters what
# it EMITS by the tag list, and one CIP read covers the assembly either way -
# so unticking costs nothing on the wire.
plc = read(ROOT, "backend", "app", "services", "plc_manager.py")
check("the gateway read path filters emissions by the tag list",
      "wanted = set(self._get_read_tags() or [])" in plc
      and "if not name or (wanted and name not in wanted):" in plc)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
