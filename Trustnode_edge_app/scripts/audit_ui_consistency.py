# -*- coding: utf-8 -*-
"""Is the app one interface, or several that happen to share a window?

2026-08-29, after a full UI review. Each check below is a real inconsistency
that was found in the shipped app, not a style preference:

  * five button classes were USED but never DEFINED - `btn-ghost` alone
    appeared 42 times and rendered as an unstyled default, which is what
    "the button colours are not standardised" actually was;
  * the Add-Widget picker had NO styles at all, so it fell back to the 430 px
    base modal with its widget list in a single column;
  * four different monospace stacks;
  * tables forced `min-width` up to 1300 px, which is what pushed the power
    register list off the side of the screen.

Static: reads the source, needs no app. Run it before shipping UI work.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
_CSS_RAW = io.open(os.path.join(SRC, "styles.css"), encoding="utf-8", errors="replace").read()
# Strip /* ... */ before scanning. Comments here explain PAST fixes and quote
# the old values ("forced min-width:1240px"), which a naive scan reads as a
# live declaration and reports as a defect that was already fixed.
CSS = re.sub(r"/\*.*?\*/", "", _CSS_RAW, flags=re.S)
FAILS: list[str] = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:160]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def jsx_sources():
    out = []
    for base, _dirs, files in os.walk(SRC):
        for f in files:
            if f.endswith((".jsx", ".js")):
                out.append(os.path.join(base, f))
    return out


ALL_JSX = "\n".join(
    io.open(p, encoding="utf-8", errors="replace").read() for p in jsx_sources()
)

# ---------------------------------------------------------------- buttons
print("[buttons: every variant used must exist]")
used = set(re.findall(r"btn-([a-z]+)\b", ALL_JSX))
used -= {"sm", "lg"}                      # size modifiers, not colours
defined = set(re.findall(r"\.btn-([a-z]+)\b", CSS))
missing = sorted(v for v in used if v not in defined)
check("no button class is used without being defined",
      not missing,
      "undefined: %s" % ", ".join("btn-" + m for m in missing) if missing
      else "%d variant(s), all defined" % len(used))

# Action buttons sit in rows; without a shared gap each caller invents one.
check("  action rows have one shared spacing rule",
      ".row-actions" in CSS and re.search(r"\.row-actions\s*\{[^}]*gap:", CSS) is not None)

# ------------------------------------------------------------------ fonts
print()
print("[fonts: one family for prose, one for numbers]")
stacks = set(re.findall(r"font-family:\s*([^;]+);", CSS))
adhoc = [s.strip() for s in stacks
         if "var(--font" not in s and "inherit" not in s]
check("font families come from tokens, not ad-hoc stacks",
      len(adhoc) == 0,
      "%d ad-hoc stack(s): %s" % (len(adhoc), " | ".join(sorted(adhoc)[:3])) if adhoc
      else "all via var(--font-*)")
check("  the tokens themselves are declared",
      "--font-sans:" in CSS and "--font-mono:" in CSS)

# ------------------------------------------------------- the widget picker
print()
print("[the Add Widget picker]")
_has_picker = ".dashboard-widget-modal" in CSS
check("the picker modal has a width of its own", _has_picker,
      "" if _has_picker else "without a rule it falls back to the 430 px base modal")
_cols = (".dashboard-type-grid" in CSS
         and re.search(r"\.dashboard-type-grid\s*\{[^}]*grid-template-columns", CSS) is not None)
check("  and lays its widgets out in columns", _cols,
      "" if _cols else "a single column makes a 40-widget library unusable")

# ------------------------------------------------------------- list widths
print()
print("[lists must fit the screen, with the menu open or closed]")
# `@media (min-width: 1180px)` is a BREAKPOINT, not a table that must fit.
# Scanning it as one reported a defect that does not exist - the second false
# positive this check produced, after comments.
_no_media = re.sub(r"@media[^{]*\{", "{", CSS)
# Three digits, not four: matching only 4-digit values made the check pass by
# finding NOTHING once the offenders dropped to 940/960, which is a vacuous
# pass. Report the real widest so a regression into the 1000s is caught.
wide = [int(m) for m in re.findall(r"min-width:\s*(\d{3,})px", _no_media)]
worst = max(wide) if wide else 0
# The app targets 1280-wide laptops; with the 260 px sidebar open a table has
# roughly 980 px. Anything forcing more than that scrolls sideways forever.
check("no table forces a width a laptop cannot show",
      worst <= 1100,
      "widest forced min-width is %dpx (sidebar leaves ~980px)" % worst if worst > 1100
      else "widest is %dpx" % worst)
check("  wide tables scroll inside their own container",
      ".table-scroll" in CSS or "overflow-x: auto" in CSS)

# ------------------------------------------------- charts: one style, one set
print()
print("[charts look and configure the same on every page]")
_app = io.open(os.path.join(SRC, "App.jsx"), encoding="utf-8", errors="replace").read()
_dw = io.open(os.path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx"),
              encoding="utf-8", errors="replace").read()
_style_mod = os.path.join(SRC, "components", "Dashboard", "chartStyle.js")
check("there is one shared chart-style module", os.path.exists(_style_mod))
check("  the power page uses it, rather than its own sizes",
      "from \"./components/Dashboard/chartStyle\"" in _app,
      "each page hard-coding its axis font is how the two drifted apart")
# A hard-coded axis tick size on the power page means it is back to its own
# scale and the font controls do nothing.
_hard = _app.count("tick={{ fontSize: 11, fill: powerChartAxisColor }}")
check("  no hard-coded axis font remains on the power charts",
      _hard == 0, "%d hard-coded tick size(s)" % _hard)
# The edit options the dashboard offers must exist on the power page too.
for _opt in ("chart_line_width", "chart_x_tick_angle", "font_axis_scale",
             "font_labels_scale", "font_legend_scale"):
    check("  power charts can set %s" % _opt, _opt in _app,
          "the dashboard offers it; the power page must too")

# --------------------------------------------- one grid per table, not two
print()
print("[each table defines its columns once]")
# 2026-08-29: `.power-register-table .thead, .trow` was declared TWICE with
# different track counts. Identical specificity means the LATER one wins, so
# adding a column to the earlier one changed nothing and the extra cell
# wrapped onto a second line. A duplicate like that is invisible in review and
# only shows up as a wrong-looking screenshot.
def _strip_media(css):
    """Remove whole @media blocks, braces balanced."""
    out = []
    i = 0
    while True:
        m = re.search(r"@media[^{]*\{", css[i:])
        if not m:
            out.append(css[i:])
            break
        start = i + m.start()
        out.append(css[i:start])
        j = i + m.end()
        depth = 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        i = j
    return "".join(out)


_top_level = _strip_media(CSS)
_grid_rules = re.findall(
    r"([.][a-z0-9-]+-table)\s+[.]thead,\s*[^{]*[{][^}]*grid-template-columns",
    _top_level)
_counts = {}
for _sel in _grid_rules:
    _counts[_sel] = _counts.get(_sel, 0) + 1
_dupes = sorted("%s (%d)" % (k, v) for k, v in _counts.items() if v > 1)
check("no table defines grid-template-columns twice",
      not _dupes,
      ", ".join(_dupes) if _dupes else "%d table(s) checked" % len(_counts))

# ------------------------------------------------------------- row density
print()
print("[list density]")
# Several .trow rules is fine - they cascade - but the padding that finally
# wins should be the compact one, not the 8px original.
paddings = re.findall(r"\.trow\s*\{[^}]*padding:\s*([^;]+);", CSS)
check("the row padding that wins is the compact one",
      bool(paddings) and paddings[-1].strip().startswith("6px"),
      "last .trow padding = %r" % (paddings[-1].strip() if paddings else None))

print()
print("[a dialog opened from a dialog must be ON TOP of it]")
# 2026-09-02, reported: "when opening new small popups after opening the first
# one to configure, it goes behind the main window ... in fact cannot be seen".
# .modal-backdrop is z-index 200, but three dialogs in the dashboard designer
# carried inline z-indexes of 60, 70 and 75 - behind every ordinary dialog, and
# behind the app header. The click still landed on the invisible dialog, which
# is why the configuration appeared to do nothing.
import re as _re_z  # noqa: E402

_css = io.open(os.path.join(SRC, "styles.css"), encoding="utf-8",
               errors="replace").read()
_m = _re_z.search(r"\.modal-backdrop\s*\{[^}]*?z-index:\s*(\d+)", _css, _re_z.S)
_base = int(_m.group(1)) if _m else 200
_low = []
for _p in jsx_sources():
    _txt = io.open(_p, encoding="utf-8", errors="replace").read()
    for _mm in _re_z.finditer(
            r'className="modal-backdrop"[^>]*?zIndex:\s*([A-Za-z0-9_.]+)', _txt):
        _val = _mm.group(1)
        if _val.isdigit() and int(_val) < _base:
            _low.append("%s: zIndex %s < %d"
                        % (os.path.relpath(_p, SRC).replace("\\", "/"), _val, _base))
check("no modal is stacked below the shared backdrop", not _low,
      "; ".join(_low[:3]) or ".modal-backdrop base is z-index %d" % _base)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
