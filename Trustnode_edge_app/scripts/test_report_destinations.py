# -*- coding: utf-8 -*-
"""Where a report is written, and the promise that it never lands on another.

2026-10-23, requested: reports that file themselves into a customer's own
folder tree - a prefix, a tag value, a suffix that depends on what another
tag holds, and a number when the same work is reported more than once.

Two things drove this file.

The first is a data-loss bug that was already there: there was NO collision
handling anywhere. Two reports whose pattern resolved to the same name -
the second test of the same job, a re-run of the same day - silently wrote
over each other and the earlier PDF was gone. A report is a record.

The second is that the destination now depends on tag VALUES, so the
mapping from "what the plant said" to "where the file went" has to be
exact. A rule that matches when it should not sends a customer's report
into the wrong folder, which is worse than not filing it at all.

Nothing in here encodes any particular customer's naming. The rules, the
folders and the names are entirely the operator's; these tests use
deliberately neutral values.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
# Point every data path at a scratch dir BEFORE the import: a test must
# never touch the live workspace.
_TMP = tempfile.mkdtemp(prefix="tn-dest-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"

from app.services.report_renderer import (          # noqa: E402
    build_report_file_name, resolve_destination, unique_output_path,
)

FAILS = []


def check(name, ok, detail=""):
    print("  {0:62s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:150]) if not ok else ""))
    if not ok:
        FAILS.append(name)


# A stand-in historian: the condition and the {token} must read THIS.
VALUES = {("", "Stage"): "2", ("", "Result"): "1", ("", "Job"): "A17"}


def fetch(cond, _from, _to):
    key = (str(cond.get("gateway_id") or ""), str(cond.get("tag_name") or ""))
    if key not in VALUES:
        return []
    return [{"value": None, "value_text": VALUES[key]}]


print("Report destinations: rules, tokens, and never overwriting")
print()

# ------------------------------------------------- never overwrite
print("[a report never lands on one that already exists]")
d = Path(_TMP) / "out"
d.mkdir(parents=True, exist_ok=True)

p1 = unique_output_path(d / "report.pdf")
check("a free name is used as-is", p1.name == "report.pdf", p1.name)
p1.write_bytes(b"first")

p2 = unique_output_path(d / "report.pdf")
check("  a second run does NOT overwrite it", p2.name != "report.pdf", p2.name)
p2.write_bytes(b"second")
check("  the first file is still there and unchanged",
      (d / "report.pdf").read_bytes() == b"first",
      "losing a generated report is losing a record")

p3 = unique_output_path(d / "report.pdf")
check("  and a third gets its own name again",
      p3.name not in {p1.name, p2.name}, p3.name)

# ------------------------------------------------- the {n} token
print()
print("[{n} says WHERE the number goes]")
name = build_report_file_name("job_{n}", template_name="T", report_id="r1")
check("{n} survives sanitising", "TNSEQSLOT" in name,
      "the number is only knowable once the folder can be read: " + name)
q1 = unique_output_path(d / name)
check("  it starts at 1", q1.name == "job_1.pdf", q1.name)
q1.write_bytes(b"x")
q2 = unique_output_path(d / name)
check("  then counts up", q2.name == "job_2.pdf", q2.name)
check("  a tag is NOT mistaken for the token",
      "{n}" not in build_report_file_name("{Job}", fetch=fetch),
      "a tag called n must still resolve as a tag")

# ------------------------------------------------- rules
print()
print("[the first matching rule decides the destination]")
RULES = [
    {"enabled": True, "logic": "all",
     "conditions": [{"gateway_id": "", "tag_name": "Stage",
                     "operator": "==", "value": "1"}],
     "output_path": "C:/base/one", "file_name_pattern": "first"},
    {"enabled": True, "logic": "all",
     "conditions": [{"gateway_id": "", "tag_name": "Stage",
                     "operator": "==", "value": "2"}],
     "output_path": "C:/base/two", "file_name_pattern": "second"},
]
got = resolve_destination(RULES, "C:/fallback", "plain", fetch=fetch)
check("the matching rule wins", got == "C:/base/two/second.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [
        {"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "9"}],
      "output_path": "C:/never", "file_name_pattern": "no"}],
    "C:/fallback", "plain", fetch=fetch)
check("no rule matching falls back to the boxes above",
      got == "C:/fallback/plain.pdf", got)

got = resolve_destination([], "C:/fallback", "plain", fetch=fetch)
check("  and no rules at all behaves exactly as before",
      got == "C:/fallback/plain.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [
        {"gateway_id": "", "tag_name": "Result", "operator": "==", "value": "1"}],
      "file_name_pattern": "named"}],
    "C:/fallback", "plain", fetch=fetch)
check("a rule may change only the NAME, keeping the folder",
      got == "C:/fallback/named.pdf", got)

got = resolve_destination(
    [{"enabled": False, "conditions": [
        {"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "2"}],
      "output_path": "C:/off"}],
    "C:/fallback", "plain", fetch=fetch)
check("a disabled rule is ignored", got == "C:/fallback/plain.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [], "output_path": "C:/empty"}],
    "C:/fallback", "plain", fetch=fetch)
check("a rule with NO condition matches nothing", got == "C:/fallback/plain.pdf",
      "%s - an empty rule must not capture every report" % got)

print()
print("[ALL vs ANY]")
both = [{"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "2"},
        {"gateway_id": "", "tag_name": "Result", "operator": "==", "value": "0"}]
got = resolve_destination(
    [{"enabled": True, "logic": "all", "conditions": both, "output_path": "C:/all"}],
    "C:/fallback", "plain", fetch=fetch)
check("ALL needs every condition", got == "C:/fallback/plain.pdf", got)
got = resolve_destination(
    [{"enabled": True, "logic": "any", "conditions": both, "output_path": "C:/any"}],
    "C:/fallback", "plain", fetch=fetch)
check("ANY needs only one", got == "C:/any/plain.pdf", got)

# ------------------------------------------------- tokens in the path
print()
print("[a tag value can be any part of the folder or the name]")
got = resolve_destination([], "C:/base/{Job}", "{Job}_{Result|1=A,0=B}", fetch=fetch)
check("a tag names the folder AND the file", got == "C:/base/A17/A17_A.pdf",
      "%s - masks let a value print as a word" % got)

# ----------------------------------- the WRITER must use it, not just exist
#
# The first version of this file tested unique_output_path() directly and
# passed with the call removed from render_template_to_pdf - proving the
# function worked while nothing used it. Rendering twice is the only
# version of this check that cannot be fooled.
print()
print("[rendering the same destination twice keeps both reports]")
from app.services.report_renderer import render_template_to_pdf   # noqa: E402

TPL = {"name": "T", "definition": {"sections": [
    {"type": "text", "text": "one"}]}}
target = Path(_TMP) / "render" / "same.pdf"
try:
    a, _, _ = render_template_to_pdf(TPL, output_path=target)
    b, _, _ = render_template_to_pdf(TPL, output_path=target)
    check("the second render does not replace the first",
          a != b and a.exists() and b.exists(),
          "%s vs %s - the writer must apply the rule, not merely have it"
          % (a.name, b.name))
except Exception as exc:                                   # noqa: BLE001
    check("the second render does not replace the first", False, repr(exc))


# ------------------------------------------- the editor keeps what it is given
print()
print("[a template imported from another edge keeps its references]")
DES = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the editor collects what a section points at",
      "collectSectionRefs(section)" in DES,
      "a <select> whose value is not in its options renders BLANK, and the "
      "next edit writes the reference away")
check("  gateway pickers show references this edge does not have",
      "gatewayOptions: localGatewayOptions" in DES
      and "FOREIGN_SUFFIX" in DES,
      "shadowing the prop covers all ten pickers at once")
check("  and so do tag pickers", "sectionRefs.tags.get(key)" in DES)
check("  a foreign entry is marked, never silently blended in",
      "not on this edge" in DES)
check("opening a template does not drop keys beside the sections",
      DES.count("...(tpl?.definition || {})") >= 1
      and "definition: { sections: Array.isArray(saved" not in DES,
      "rebuilding the definition as {sections} discards every sibling key")

print()
print("[a full-width pill can have its text centred]")
check("the editor writes title_align", "title_align: e.target.value" in DES,
      "the renderer has always READ title_align for the words inside the "
      "band and defaulted it to left - nothing wrote it, so a full-width "
      "pill could never be centred")
check("  band position is disabled when it cannot do anything",
      'disabled={String(section.pill_fit || "fill") === "fill"}' in DES,
      "a control that silently does nothing is what was reported as broken")

print()
print("[the schedule editor carries the rules]")
SRM = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ScheduledReportsManager.jsx"), encoding="utf-8").read()
check("the draft carries destination_rules", "destination_rules: []" in SRM,
      "a field missing from the draft is dropped on save")
check("  there is an editor for them", "function DestinationRules(" in SRM)
check("  the destination has its own block, not the cycle grid",
      "Where the file is written" in SRM and 'className="sr-destination"' in SRM)
check("  shown for every trigger mode, not only cycle",
      SRM.index("Where the file is written") > SRM.index("Cycle options"),
      "a time-triggered schedule could not set a destination at all")
check("  the run number is documented on screen", '{"{n}"}' in SRM)

print()
print("[the store persists them - both statements, or they vanish]")
STORE = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "reports_store.py"), encoding="utf-8").read()
check("destination_rules_json is in the INSERT",
      "destination_rules_json" in STORE.split("INSERT INTO scheduled_reports")[1][:900])
check("  and in the UPDATE", "destination_rules_json = ?" in STORE)
check("  and is read back", '"destination_rules": _json_list' in STORE)
APP_STORE = io.open(os.path.join(ROOT, "backend", "app", "services",
                                 "app_store.py"), encoding="utf-8").read()
check("  and existing installs get the column",
      "destination_rules_json" in APP_STORE,
      "without the migration an upgraded edge fails every schedule save")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
