# -*- coding: utf-8 -*-
"""Every registered widget is reachable, and a BAD pin never renders as OFF.

Two faults, both found on 2026-08-29.

1. `TYPE_GROUPS` in DashboardDesigner was a hand-maintained list of category
   names that had to be kept in step with the `group` field in
   widgetRegistry.js. It wasn't. Seventeen OEE widgets were added to the
   registry under a new group and the picker never listed one of them - no
   error, no warning, they were simply unreachable while looking entirely
   present in the source. Any hard-coded mirror of data that already exists
   is this bug waiting for the next group.

2. The I/O block widget shows digital pins. A pin whose reading came back BAD
   must NOT render as OFF: the same day, an ifm block sat unplugged for hours
   writing 2 872 null rows an hour while every pin would have read a confident
   0. "The input is low" and "we never heard back from the block" must not
   look identical - only one of them means go and check the cable.

Source-level, no app and no hardware.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, "frontend", "src", "components", "Dashboard")
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


def read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8", errors="replace").read()


registry = read(FE, "widgetRegistry.js")
designer = read(FE, "DashboardDesigner.jsx")
widgets = read(FE, "DashboardWidgets.jsx")
ioblock = read(FE, "IoBlockWidget.jsx")

# --- 1. every widget in the registry can actually be picked ---------------
print("[every registered widget is reachable in the picker]")
entries = re.findall(r'\{\s*key:\s*"([a-z0-9_]+)"[^}]*?group:\s*"([^"]+)"', registry)
keys = [k for k, _ in entries]
groups = sorted({g for _, g in entries})
check("the registry parses", len(entries) > 30, "%d widget(s), %d group(s)" % (len(entries), len(groups)))

check("the picker derives its groups FROM the registry",
      "orderedTypeGroups(WIDGET_TYPES)" in designer,
      "a hard-coded group list is what hid the OEE widgets")
check("  and no hard-coded TYPE_GROUPS list survives",
      "const TYPE_GROUPS =" not in designer,
      "the list that had to be kept in step, and wasn't")

# Simulate orderedTypeGroups: preferred order first, everything else appended.
m = re.search(r"const TYPE_GROUP_ORDER = \[(.*?)\]", designer, re.S)
order = re.findall(r'"([^"]+)"', m.group(1)) if m else []
covered = [g for g in order if g in groups] + sorted(g for g in groups if g not in order)
missing = [g for g in groups if g not in covered]
check("  so no group can be dropped", not missing, missing or "all %d group(s) render" % len(groups))
check("  including OEE, the one that was invisible", "OEE" in covered)
check("  and the new I/O group", "I/O" in covered)

# --- 2. the I/O block widget is wired end to end -------------------------
print()
print("[the I/O block widget is wired end to end]")
check("registered in the widget registry", "io_block_status" in registry)
check("  dispatched by the renderer",
      'case "io_block_status"' in widgets and "IoBlockWidget" in widgets)
check("  offered a gateway picker in the config tab",
      re.search(r'"io_block_status"\]\.includes\(form\.type\)', designer) is not None,
      "without a gateway it has nothing to read")
check("  the tag-monitor hook reaches the widget body",
      "onOpenTagMonitor = null," in widgets and "onOpenTagMonitor={onOpenTagMonitor}" in designer,
      "the per-pin chart button needs it")

# --- 3. the rule that matters: BAD is not OFF ----------------------------
print()
print("[a pin that did not read is not a pin that reads OFF]")
check("quality is checked against the GOOD threshold",
      "192" in ioblock and "quality" in ioblock)
# Scope to fmtPin's body. Searching the whole file made the header comment
# ("a BAD reading is NOT an OFF reading") count as an OFF literal, so the
# check failed against correct code - it was reading prose, not behaviour.
_fn = re.search("function fmtPin" + chr(92) + "(.*?" + chr(92) + "n}", ioblock, re.S)
body = _fn.group(0) if _fn else ""
check("  fmtPin is where a pin becomes text", bool(body))
bad_idx = body.find('st === "bad"')
off_idx = body.find('"OFF"')
check("  a BAD reading returns before any ON/OFF mapping",
      bad_idx != -1 and off_idx != -1 and bad_idx < off_idx,
      "in fmtPin: bad-branch at %d, OFF literal at %d" % (bad_idx, off_idx))

# The bad branch must render a dash, not a zero-ish state.
bad_block = body[bad_idx:bad_idx + 400] if bad_idx != -1 else ""
check("  and it renders as a dash, not OFF",
      '"—"' in bad_block and '"OFF"' not in bad_block,
      bad_block.strip().replace("\n", " ")[:90])
check("  the reason is stated where the next reader will look",
      "NOT an OFF" in ioblock or "not an OFF" in ioblock)
check("  staleness is treated as doubt, not as a value",
      "STALE_MS" in ioblock and '"stale"' in ioblock)
check("  an all-BAD block reads as Fault, not a block of OFF pins",
      'every((s) => s === "bad")' in ioblock)

# Ports are discovered, not assumed: a 4-port block must not render 8 rows.
check("  ports come from the tags that exist",
      "portNums" in ioblock and "Port(\\d+)_Pin([24])" in ioblock.replace("\\\\", "\\"),
      "no hard-coded port count")

# --- 4. body colours: every variant carries its own readable palette -----
print()
print("[each block colour brings its own palette]")
CSS = read(os.path.join(ROOT, "frontend", "src"), "styles.css")
variants = re.findall(r"\.io-face-body\.io-(orange|grey|black|auto)[^{]*\{([^}]*)\}", CSS)
names = [v for v, _ in variants]
check("all four bodies are defined",
      set(["orange", "grey", "black", "auto"]).issubset(set(names)), sorted(set(names)))
# The on-dark palette (pale green on #7dfaa8, pale blue) is invisible on light
# grey. A variant that does not restate these inherits whatever came before.
NEEDED = ("--io-ink", "--io-ink-2", "--io-on", "--io-num", "--io-bad",
          "--io-inset", "--io-inset-2", "--io-edge")
for name, body in variants:
    missing = [t for t in NEEDED if t not in body]
    check("  io-%s defines its own ink and value colours" % name,
          not missing, missing or "all %d tokens" % len(NEEDED))
check("  auto follows the app theme",
      '[data-theme="dark"] .io-face-body.io-auto' in CSS,
      "otherwise auto is just a second grey")
check("  the widget applies the chosen body class",
      'io-face-body io-${bodyColor}' in ioblock and '"orange", "grey", "black", "auto"' in ioblock)
check("  and the designer offers the setting",
      "io_block_color" in designer and "Block colour" in designer)

# --- 5. the device connectors are lit by evidence, not by assumption -----
print()
print("[the block's own four M12s]")
for cid in ("X21", "X22", "X23", "X31"):
    check("  %s is on the face" % cid, '"%s"' % cid in ioblock)
check("  the lit interface is the one the data ARRIVED over",
      "function transportOf" in ioblock and 'r?.source' in ioblock,
      "each row records the driver that produced it")
check("  an interface with no evidence is 'not collected', never green",
      'c.lit ? "unread" : "none"' in ioblock and "publishes no link tag" in ioblock,
      "a green lamp for a socket we have never spoken to is the lie this widget avoids")
check("  and `source` survives the dashboard projection",
      "source: row?.source" in designer,
      "without it every interface would look uncollected")
check("  power reads from the real supply tag",
      "lampState(latest.Master_Voltage" in ioblock)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
