# -*- coding: utf-8 -*-
"""Number fields accept typing: the clamp runs when the field is left.

2026-11-16, reported: "in the dashboard widget configurations, report
sections and other gateway configurations the number text fields I cannot
enter any number, it is forcing me to use the increment and decrement
buttons". Every keystroke went through the clamp: typing "1" into a field
whose floor is 5 became 5, "12" became 52, and clearing a field snapped it
back to its default through `value={Number(x) || d}`.

Source check over the three dialogs' files:
  * no onChange clamps / parses e.target.value on the way in (clamp on onBlur);
  * no number field's value re-defaults while it is being cleared.

    python scripts/test_number_fields_typeable.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
FILES = ["components/Dashboard/DashboardDesigner.jsx", "components/Reports/ReportTemplateDesigner.jsx", "App.jsx"]
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-74s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def clamp_on_change_sites(text: str) -> list[str]:
    """Lines where a clamp of e.target.value sits inside an onChange handler."""
    lines = text.split("\n")
    bad = []
    for i, line in enumerate(lines):
        if "e.target.value" not in line or "onBlur" in line or 'e.target.value === ""' in line or "const n =" in line:
            continue
        clamped = ("clamp(e.target.value" in line
                   or re.search(r"Math\.(max|min)\([^;]*Number\(e\.target\.value", line)
                   or re.search(r"Number\(e\.target\.value\) \|\| [0-9.]+", line))
        if not clamped:
            continue
        j = i
        while j >= 0 and i - j < 8 and "onChange={(e) =>" not in lines[j] and "onBlur={(e) =>" not in lines[j]:
            j -= 1
        if j >= 0 and "onChange={(e) =>" in lines[j] and "onBlur={(e) =>" not in lines[j]:
            bad.append("%d: %s" % (i + 1, line.strip()[:90]))
    return bad


def main() -> int:
    print("number fields accept typing")
    for rel in FILES:
        text = open(os.path.join(SRC, rel), encoding="utf-8").read()
        bad = clamp_on_change_sites(text)
        check("%s: no number field is clamped on every keystroke" % os.path.basename(rel), not bad, "; ".join(bad[:3]))
        redef = [m.group(0) for m in re.finditer(r"value=\{Number\([^{}]*\)(?: \|\| [0-9.\"]+)?\}", text)]
        check("  no number field re-defaults while it is being cleared (value={Number(..)})", not redef, "; ".join(redef[:3]))
        blurs = text.count("onBlur={(e) =>")
        check("  the clamp still runs when the field is left (onBlur present: %d)" % blurs, blurs >= 1)
    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
