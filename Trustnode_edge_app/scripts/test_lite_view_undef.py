# -*- coding: utf-8 -*-
"""Nothing lints the phone/LAN app. This does.

`backend/static/lite_view/index.html` is hand-written and served as-is: no
build reads it, no bundler parses it, `npm run lint:undef` never sees it. So
the one failure mode that cannot be spotted by reading - an identifier that
exists in one function and is used from another - ships.

2026-10-17 it had been shipping. `buildManualY` was a `const` declared inside
`ChartWidget`, and `TagMonitor` - a separate top-level function - called it:

    const monManualLeft = buildManualY(monCfg.y_axis_mode, ...);

Opening the tag monitor threw `buildManualY is not defined` before it drew
anything. The same class of fault as `liteAxisId` eleven days earlier, in the
same file, for the same reason.

This extracts the module script and runs eslint's no-undef over it, through
`frontend/eslint.liteview.config.mjs`. Inline config is ignored on purpose:
the file carries `eslint-disable` comments for rules that are not loaded
here, and what is wanted is the raw answer.

The config's `files` glob has to match the probe. The first version of this
test reused the React config, whose glob is `src/**` - the probe matched
nothing, so no rule ran and it passed over an identifier that plainly did not
exist. A lint arm that cannot fail is worse than no lint arm.

Read-only. No store, no network.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LITE = os.path.join(ROOT, "backend", "static", "lite_view", "index.html")
FRONTEND = os.path.join(ROOT, "frontend")
CONFIG = os.path.join(FRONTEND, "eslint.liteview.config.mjs")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:400]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the lite view has no undefined identifiers")
print()

for path in (LITE, CONFIG):
    if not os.path.isfile(path):
        print("  missing %s" % path)
        sys.exit(2)

html = io.open(LITE, encoding="utf-8").read()
scripts = re.findall(
    r'<script[^>]*type=["\']module["\'][^>]*>(.*?)</script>', html, re.S)
check("the page carries a module script", bool(scripts),
      "%d found" % len(scripts))
if not scripts:
    sys.exit(2)

npx = shutil.which("npx") or shutil.which("npx.cmd")
if not npx:
    print("  npx is not on PATH - cannot lint. SKIP")
    sys.exit(0)

# The probe has to sit inside frontend/ so eslint resolves its plugins from
# frontend/node_modules.
# The name matters: eslint.liteview.config.mjs matches
# "**/*.liteview.mjs". A probe the config does not match is
# linted by NOTHING and reports success - which is how the first
# version of this test passed over a deliberate "does not exist".
probe = os.path.join(FRONTEND, ".lite_view_probe.liteview.mjs")
try:
    for i, body in enumerate(scripts):
        io.open(probe, "w", encoding="utf-8", newline="").write(body)
        proc = subprocess.run(
            [npx, "eslint", "-c", CONFIG, "--no-config-lookup",
             "--no-inline-config", probe],
            cwd=FRONTEND, capture_output=True, text=True, shell=False)
        out = (proc.stdout or "") + (proc.stderr or "")
        undef = [ln.strip() for ln in out.splitlines() if "no-undef" in ln]
        check("module script %d has no undefined identifiers" % (i + 1),
              proc.returncode == 0 and not undef,
              "\n      ".join(undef) if undef else out.strip()[:300])
finally:
    try:
        os.remove(probe)
    except OSError:
        pass

print()
print("[the two that shipped, pinned by name]")
src = html
# Both must be reachable from EVERY top-level function that calls them, which
# in practice means module scope. Asserted structurally: the declaration must
# not be indented inside a function body.
for name in ("liteAxisId", "buildManualY", "liteYAxes", "LITE_AXIS_CFG"):
    check("  %-14s is declared at module scope" % name,
          re.search(r"(?m)^const %s\b" % name, src) is not None,
          "an indented declaration lives inside the function above it, and "
          "is a ReferenceError everywhere else")

check("the tag monitor and the widget share ONE axis builder",
      src.count("liteYAxes(") >= 3,
      "two copies of the axis rules is how the monitor kept the legacy "
      "left/right ids while the series moved to the four-way ones - a "
      "Recharts series whose yAxisId matches no axis draws nothing: "
      "found %d" % src.count("liteYAxes("))

print()
print("[the report card draws on Lite too]")
# 2026-10-17, reported: "the report card preview also is not being showed on
# lite version". The widget dispatch had no branch for it, so it fell through
# to the grey "unknown widget" box. The edge has had this widget since the
# Reports group was added.
check("the widget dispatch has a report_card branch",
      'type === "report_card" ? h(ReportCardWidget' in src,
      "without a branch every report card is a grey box saying 'report_card'")
check("  and the component exists",
      "function ReportCardWidget(" in src)
check("  it is not in the skip list",
      '"report_card"' not in src.split("const SKIPPED_TYPES")[1].split(")")[0],
      "a skipped type never reaches the dispatch at all")
for key in ("report_template_id", "report_view_mode", "report_refresh_minutes"):
    check("  reads %-24s - the same key the edge writes" % key, key in src,
          "a dashboard profile has to mean the same thing on both surfaces")
check("  the PDF and the summary share the Reports tab's URL builders",
      src.count("liteReportLocalUrl(") >= 2 and src.count("liteReportSignedUrl(") >= 2,
      "LAN serves the file itself, the cloud needs a signed URL - two copies "
      "of that rule is how they drift")
css = io.open(os.path.join(ROOT, "backend", "static", "lite_view", "styles.css"),
               encoding="utf-8").read()
check("  the widget has its OWN css classes",
      ".rcw-preview" in css and ".rcw-summary" in css,
      "the Reporting TAB already owns .report-card-summary; two rules on one "
      "class is how a layout breaks silently")
check("  and the embedded frame is given a size",
      ".rcw-preview .pdf-frame" in css,
      "an iframe with no height collapses to a sliver")

print()
print("[a changed shell is a new cache version]")
sw = io.open(os.path.join(ROOT, "backend", "static", "lite_view", "sw.js"),
             encoding="utf-8").read()
m = re.search(r'CACHE_VERSION = "([^"]+)"', sw)
check("the service worker names a cache version", bool(m), m.group(1) if m else None)
check("  index.html cache-busts its stylesheet",
      re.search(r'styles\.css\?v=\d+', src) is not None,
      "the shell is served cache-first; a new page under an old version "
      "reaches nobody")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
