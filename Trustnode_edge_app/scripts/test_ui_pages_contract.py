# -*- coding: utf-8 -*-
"""The page fixes the operator asked for on 2026-10-02, held in place.

Every check here is a screenshot the operator sent. These are SOURCE
assertions, not behaviour: the things that broke were layout rules and a
label, which a running backend cannot answer for, and the one thing that
must never regress is that they get quietly reverted by a later edit.

Each assertion names the symptom, so a failure says what the operator would
see rather than which selector moved.
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, "frontend", "src")

FAILS: list[str] = []


def read(*parts: str) -> str:
    return io.open(os.path.join(FE, *parts), encoding="utf-8",
                   errors="replace").read()


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  {0:62s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL",
        (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


APP = read("App.jsx")
CSS = read("styles.css")
DASH_CSS = read("components", "Dashboard", "dashboard.css")
DESIGNER = read("components", "Dashboard", "DashboardDesigner.jsx")

print("TrustNode - the 2026-10-02 page fixes")
print()

print('[gateway + database pickers say "Local" / "Cloud", never a path]')
check("a friendly label helper exists", "function friendlyDbLabel" in APP
      or "const friendlyDbLabel" in APP)
check("  the gateway table's Database column uses it",
      "if (hit) return friendlyDbLabel(hit);" in APP,
      "it printed the connection name, so a pooler hostname reached the table")
check("  the power-device table uses it too",
      "const dbName = friendlyDbLabel(" in APP)
check("  no table falls back to a raw engine name",
      '"Local SQLite (default)"' not in APP,
      "that string is the old fallback")

print()
print("[the legacy report pages are gone, not hidden behind a disclosure]")
check("the legacy schedule editor is removed",
      "legacy-scheduled-reports-fallback" not in APP)
check("the legacy ad-hoc report page is removed",
      "Legacy: ad-hoc filter-based reports" not in APP)

print()
print("[legacy database connectors are not offered to a fresh install]")
check("the Dolibarr mirror toggle is conditional on one existing",
      "{dolibarrCandidates.length ? (" in APP,
      "an always-visible, always-disabled checkbox")
check("  Add Cloud Database no longer offers legacy HTTP",
      '<option value="dolibarr">' not in APP)
check("  but the routing behind it is untouched",
      "dolibarrMirrorEnabled" in APP and "legacy_http" in APP,
      "an install that HAS a legacy sink must keep working")

print()
print("[settings pages: full width, one card below the other]")
for page in ("interface", "edge", "directories"):
    sel = '.content-scroll[data-page="%s"]' % page
    check("%s is not split into card columns" % page,
          sel + " {\n    display: grid;" not in CSS
          and "repeat(auto-fit, minmax(430px, 1fr))" not in CSS,
          "two narrow columns of cards")
check("  the width is spent on the FIELDS instead",
      "repeat(auto-fit, minmax(240px, 1fr))" in CSS,
      "a settings field needs far less than the data pages' 260px")
check("  vertical space is tightened, not default",
      '.content-scroll[data-page="directories"] .card' in CSS)

print()
print("[Directories looks like every other page]")
check("it uses the standard card title",
      '<h3 className="card-title">Directories</h3>' in APP,
      "a bare <h3> read as a different app")
check("  the help text is one line, not a paragraph",
      "override a row for a shared network folder" in APP.lower())
check("  every known folder is still listed from the backend",
      "rows.map((r) =>" in APP)

print()
print("[Directories: this edge's scope, and no setting that does nothing]")
DIRS = io.open(os.path.join(ROOT, "backend", "app", "routers",
                            "directories.py"),
               encoding="utf-8", errors="replace").read()
check("the read is SCOPED, not the legacy unscoped table",
      "get_domain_scoped_fast(\"app_settings\")" in DIRS
      and 'get_domain_fast("app_settings")' not in DIRS,
      "config_documents is shared across every customer in the file")
check("  a failed read refuses the save instead of writing blind",
      "if current is None:" in DIRS and "status_code=503" in DIRS,
      "writing back a document you failed to read is the blank-write trap")
check("  the save lands where the read looks",
      "upsert_domain_scoped(scope_key, \"app_settings\"" in DIRS,
      "an unscoped save under a scoped read shows the default straight back")
check("  an edge with NO identity still writes unscoped",
      "app_store.upsert_domain(\"app_settings\", s," in DIRS,
      "that is where its reads come from too")
check("rows declare whether the override is actually wired",
      "_HONORED_KEYS = frozenset({\"reports\"})" in DIRS
      and '"honored": key in _HONORED_KEYS,' in DIRS)
check("  and the page shows that instead of a dead input",
      'r.honored === false' in APP and "location only" in APP,
      "five of the six rows would store a path nothing reads")
check("  the docstring no longer claims they are all honoured",
      "honored by every relevant" not in DIRS,
      "it was not true")

print()
print("[Dashboard: one height across the toolbar]")
check("the toolbar sets ONE height for every control",
      ".dashboard-designer-tools .dashboard-pill," in DASH_CSS
      and "height: 32px;" in DASH_CSS,
      "four different heights on one row")
check("  the pills no longer derive height from padding",
      ".dashboard-designer-tools .dashboard-pill {\n  padding: 0 14px;" in DASH_CSS,
      "8px of padding plus font metrics is how they drifted")
check("  the profile chip is on the same line",
      ".dashboard-designer-tools .dashboard-active-profile-chip" in DASH_CSS)

print()
print("[Dashboard Configuration sizes to its content]")
check("the modal has its own size rule",
      ".dashboard-config-modal {" in CSS)
check("  height follows the content",
      "max-height: min(86vh, 720px);" in CSS,
      "it opened far taller than the three groups inside it")
check("  the long descriptions are gone",
      "Useful for templates, backups, or onboarding a new edge" not in DESIGNER
      and "Switch between them at any time" not in DESIGNER,
      "they pushed the controls off the modal")

print()
print("[Diagnostics: an unmeasured number is not a zero]")
DIAG_SVC = io.open(os.path.join(ROOT, "backend", "app", "services",
                                "diagnostics.py"),
                   encoding="utf-8", errors="replace").read()
DIAG_UI = read("components", "Diagnostics", "DiagnosticsPage.jsx")
check("the first snapshot reports CPU as unmeasured, not 0%",
      "cpu = None" in DIAG_SVC
      and '"percent": (None if cpu is None else round(float(cpu), 1)),' in DIAG_SVC,
      "a confident 0% on a pegged box is what made the page look broken")
check("  the per-core baseline is not double-primed",
      DIAG_SVC.count("psutil.cpu_percent(interval=None, percpu=True)") == 1,
      "a second call in the same snapshot deltas against the first and "
      "returns zeros forever")
check("  the UI says so instead of printing 0%",
      'cpu.percent == null ? "measuring' in DIAG_UI)
check("  and never calls .toFixed on the null share",
      "ourCpuShare.toFixed" in DIAG_UI and "ourCpuShare == null" in DIAG_UI,
      "that would blank the whole page with a render error")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
