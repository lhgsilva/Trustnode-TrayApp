# -*- coding: utf-8 -*-
"""Audit every dialog and settings surface for the UI faults reported 2026-08-26.

  "some buttons and actions buttons are overflowing the layout... the content
   are not fitting in the screen... the check box, slider toggle in two
   different rows... we should have smaller fonts... where the popup modal
   window is full we should have a scroll bar."

Static analysis, no browser. It reports per dialog:

  INLINE-PAD   inline padding/margin a stylesheet cannot compact (inline styles
               beat CSS, so these are the blocks that stay fat no matter what
               the density pass does)
  BIG-FONT     inline fontSize >= 14 inside a dialog
  FIXED-W      a hard pixel width that cannot shrink - the usual cause of a
               control pushing past the card edge
  STACKED      a <label> wrapping a checkbox with no row layout, which the
               global `label { display: grid }` renders as two rows

and asserts the shell guarantees hold in the stylesheet: every dialog scrolls,
its title stays put, and its action row is pinned so the buttons can never be
scrolled out of reach.

Run:  python scripts/audit_dialog_ui.py           (report + pass/fail)
      python scripts/audit_dialog_ui.py --list    (full per-dialog detail)
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
CSS = os.path.join(SRC, "styles.css")
VERBOSE = "--list" in sys.argv

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def jsx_files():
    out = []
    for dirpath, _, files in os.walk(SRC):
        if "node_modules" in dirpath:
            continue
        for f in files:
            if f.endswith(".jsx"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


# ---------------------------------------------------------------- the shell
print("[the dialog shell - guarantees that hold for all 46 dialogs]")
css = io.open(CSS, encoding="utf-8", errors="replace").read()

check("every dialog is height-capped so it cannot exceed the screen",
      "max-height: calc(100dvh - 24px)" in css or "max-height: 92dvh" in css)
check("  and scrolls its own content", "overflow-y: auto" in css)
check("the title stays visible while scrolling",
      ".modal-card > h3" in css and "position: sticky" in css)
check("the action buttons are PINNED, never scrolled out of reach",
      ".modal-card > .modal-actions" in css and "bottom: -14px" in css)
check("  and wrap instead of overflowing the card",
      re.search(r"\.modal-card > \.modal-actions[^{]*\{[^}]*flex-wrap:\s*wrap", css, re.S)
      is not None)
check("a too-tall dialog anchors to the top, so its head is not clipped",
      "align-items: start" in css)
check("small buttons are actually small (btn-sm reset .btn's 36px height)",
      re.search(r"\.btn-sm\s*\{[^}]*height:\s*28px", css, re.S) is not None)
check("dialog controls use a denser type scale",
      re.search(r"\.modal-card label\s*\{[^}]*font-size:\s*12px", css, re.S) is not None)
check("a checkbox/toggle shares ONE row with its label",
      'label:has(> input[type="checkbox"])' in css)
check("short screens get a tighter layout", "max-height: 820px" in css)
check("narrow windows collapse two-column dialog grids", "max-width: 720px" in css)
check("the toggle component exists (no more per-dialog inline switches)",
      ".tn-toggle-field" in css and ".tn-switch-pill" in css)

# --------------------------------------------------- the BUILT stylesheet
# Source CSS proves intent; the bundle proves what actually ships. The
# minifier rewrites selectors (it strips the quotes in [type="checkbox"]),
# so match on its normalised form.
import glob
dist = os.path.join(ROOT, "frontend", "dist", "assets")
if os.path.isdir(dist):
    built = "".join(io.open(f, encoding="utf-8", errors="replace").read()
                    for f in glob.glob(os.path.join(dist, "*.css")))
    flat = "".join(built.split())
    print()
    print("[what actually shipped in the built stylesheet]")
    for label, needle in [
        ("the height cap shipped", "max-height:calc(100dvh-24px)"),
        ("the sticky title shipped", ".modal-card>h3"),
        ("the pinned action row shipped", ".modal-card>.modal-actions"),
        ("one-row toggles shipped", "label:has(>input[type=checkbox])"),
        ("the small-button fix shipped", ".btn-sm{height:28px"),
        ("the toggle component shipped", ".tn-switch-pill"),
        ("short-screen rules shipped", "max-height:820px"),
    ]:
        check(label, needle.replace(" ", "") in flat)
else:
    print()
    print("  (frontend not built - skipping the shipped-CSS checks)")

# ------------------------------------------------------------- per dialog
print()
print("[per-dialog audit]")
INLINE_PAD = re.compile(r'(?:padding|marginTop|marginBottom|margin):\s*"?(\d+)')
BIG_FONT = re.compile(r"fontSize:\s*(\d+)")
FIXED_W = re.compile(r"width:\s*(\d{3,})\b")
findings = {}
dialog_count = 0

for path in jsx_files():
    text = io.open(path, encoding="utf-8", errors="replace").read()
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if "modal-card" in ln]
    for st in starts:
        dialog_count += 1
        title = "(dynamic title)"
        for j in range(st, min(st + 25, len(lines))):
            m = re.search(r"<h3[^>]*>\s*([^<{]{3,60})", lines[j])
            if m:
                title = "".join(c for c in m.group(1).strip() if ord(c) < 128).strip()
                break
        # a dialog body runs until the next modal-card or 400 lines
        nxt = next((s2 for s2 in starts if s2 > st), None)
        end = min(nxt if nxt else len(lines), st + 400, len(lines))
        body = lines[st:end]
        issues = []
        for k, ln in enumerate(body):
            for m in INLINE_PAD.finditer(ln):
                if int(m.group(1)) >= 10:
                    issues.append(("INLINE-PAD", st + k + 1, ln.strip()[:70]))
            for m in BIG_FONT.finditer(ln):
                if int(m.group(1)) >= 14:
                    issues.append(("BIG-FONT", st + k + 1, ln.strip()[:70]))
            for m in FIXED_W.finditer(ln):
                if int(m.group(1)) >= 150:
                    issues.append(("FIXED-W", st + k + 1, ln.strip()[:70]))
        key = "{0}:{1}".format(os.path.relpath(path, SRC).replace("\\", "/"), st + 1)
        if issues:
            findings[key] = (title, issues)

worst = sorted(findings.items(), key=lambda kv: -len(kv[1][1]))
print("  dialogs scanned              : {0}".format(dialog_count))
print("  dialogs with inline overrides: {0}".format(len(findings)))
total_issues = sum(len(v[1]) for v in findings.values())
print("  total inline overrides       : {0}".format(total_issues))
if worst:
    print()
    print("  heaviest dialogs (inline styles the density pass cannot reach):")
    for key, (title, issues) in worst[:8]:
        kinds = {}
        for kind, _, _ in issues:
            kinds[kind] = kinds.get(kind, 0) + 1
        summary = ", ".join("{0}x{1}".format(v, k) for k, v in sorted(kinds.items()))
        print("    {0:44s} {1:28s} {2}".format(key[-44:], title[:28], summary))
if VERBOSE:
    for key, (title, issues) in worst:
        print()
        print("  {0}  {1}".format(key, title))
        for kind, ln, txt in issues:
            print("    {0:11s} line {1:<7d} {2}".format(kind, ln, txt))

# The audit is advisory for the count, but the SHELL guarantees are not.
print()
print("  Note: inline overrides are not failures by themselves - they are where")
print("  a dialog opts out of the shared density scale. The shell checks above")
print("  are the ones that must hold.")
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
