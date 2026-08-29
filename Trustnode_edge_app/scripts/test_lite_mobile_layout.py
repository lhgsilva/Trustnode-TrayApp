# -*- coding: utf-8 -*-
"""The Lite bundles must be usable on a phone.

2026-08-27: "I want the new web built for lite versions of the app in special
in the phone with power, reports and batch management redesigned."

The app has no shared Table component - every table is hand-rolled .thead +
.trow divs with a fixed grid-template-columns, several with an explicit
min-width in the 900-1240 px range. On a ~390 px phone that is a 6-10 column
grid squeezed into 390 px, or a horizontal scrollbar. Power was the worst: an
8-column KPI strip with a 140 px minimum, i.e. 1120 px of content.

The fix is two halves and BOTH must ship, or the pages look broken in a new
way: a runtime pass copies each column heading onto its cell as data-label
(App.jsx), and CSS restacks rows into cards using it (styles.client.css).

Checks the BUILT bundles, because that is what gets deployed.
"""
import glob
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(ROOT, "frontend")
BUNDLES = ["dist_client_view", "dist_cloud_readonly"]
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def read_bundle(name):
    root = os.path.join(FRONT, name)
    if not os.path.isdir(root):
        return None, None
    css = js = ""
    for f in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if f.endswith(".css"):
            css += io.open(f, encoding="utf-8", errors="replace").read()
        elif f.endswith((".js", ".html")):
            js += io.open(f, encoding="utf-8", errors="replace").read()
    return css, js


# --- the source contract ---------------------------------------------------
print("[the two halves must both exist]")
app = io.open(os.path.join(FRONT, "src", "App.jsx"), encoding="utf-8",
              errors="replace").read()
client_css = io.open(os.path.join(FRONT, "src", "styles.client.css"),
                     encoding="utf-8", errors="replace").read()

check("the runtime labels cells from their column heading",
      'setAttribute("data-label"' in app)
check("  it only runs on the phone layout",
      "if (!useMobileLayout) return undefined;" in app)
check("  and keeps up as live rows arrive",
      "MutationObserver" in app and "childList" in app)
check("  without rewriting labels that did not change",
      'getAttribute("data-label") !== title' in app)
check("the CSS restacks rows using those labels",
      "content: attr(data-label)" in client_css)
check("  and everything is scoped to the client phone layout",
      client_css.count(".surface-client.client-view-mobile") >= 20,
      client_css.count(".surface-client.client-view-mobile"))

# --- what actually ships ---------------------------------------------------
for bundle in BUNDLES:
    print()
    print("[{0}]".format(bundle))
    css, js = read_bundle(bundle)
    if css is None:
        check("{0} was built".format(bundle), False, "run npm run build:clientview / build:cloudro")
        continue
    flat = "".join((css + js).split())
    # clientview is a SINGLE-FILE build - vite-plugin-singlefile inlines the
    # stylesheet into index.html, so a separate .css file is legitimately absent
    styled = bool(css.strip()) or "client-view-mobile" in js
    check("the bundle carries its stylesheet",
          styled, "{0} css bytes, inlined={1}".format(
              len(css), "client-view-mobile" in js))
    check("  tables restack into cards",
          "client-view-mobile.table>.trow" in flat)
    check("  column names become row labels",
          "content:attr(data-label)" in flat)
    check("  the header row is hidden once labels move onto cells",
          "client-view-mobile.table>.thead" in flat)
    # Power: the 8-column KPI strip is the single worst offender on a phone
    check("  POWER: the KPI strip goes two-up",
          "client-view-mobile.power-kpi-grid" in flat)
    check("  POWER: charts stack instead of sitting side by side",
          "client-view-mobile.power-main-grid" in flat)
    check("  REPORTS: report rows become cards",
          "client-view-mobile.reporting-docs-table" in flat
          or "client-view-mobile.scheduled-table" in flat)
    check("  BATCH: batch actions wrap to tap targets",
          "client-view-mobile.bm-row-actions" in flat)
    check("  no table can force sideways scrolling",
          "min-width:0!important" in flat)
    check("  the runtime labeller is in the bundle", "data-label" in js)

# --- the desktop edge UI must be untouched --------------------------------
print()
print("[the desktop UI is not affected]")
# every rule added is scoped; none may leak to the plain .table selector
leaked = []
for line in client_css.split("\n"):
    stripped = line.strip()
    if stripped.startswith(".table") or stripped.startswith(".trow"):
        leaked.append(stripped[:60])
check("no unscoped .table/.trow rule was introduced", not leaked, leaked[:3])
check("  the edge stylesheet was not modified for this",
      "client-view-mobile" not in io.open(
          os.path.join(FRONT, "src", "styles.css"), encoding="utf-8",
          errors="replace").read())

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
