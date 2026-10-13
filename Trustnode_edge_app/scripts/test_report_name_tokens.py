# -*- coding: utf-8 -*-
"""A bare {TagName} in a report file name or path must resolve.

2026-10-07, reported: 'the name of the fiels also to be use d the "{" is not
working'. The operator set a file name of "{R1_Work_Order_string}" and got a
file called exactly that. Only the prefixed form `{tag:Name}` was ever
matched; anything else passed through untouched, so the braces reached the
filesystem.

Writing the tag name on its own is the obvious thing to try, so it now works
- without swallowing the run-time placeholders ({template}, {date}, ...),
which are filled in by a later pass and would otherwise be read as "a tag
called template".
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-nametok-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services import report_renderer as rr            # noqa: E402

ROWS = {
    "R1_Work_Order_string": [{"value": None, "value_text": "WO-4471"}],
    "TCU_Temp": [{"value": 80.0}, {"value": 85.0}],
}


def fake(cond, from_utc, to_utc):
    return ROWS.get(str(cond.get("tag_name") or ""), [])


W = ("2026-10-07 00:00:00", "2026-10-07 23:59:59")

print("TrustNode - {tokens} in a report name")
print()

print("[a bare tag name]")
check("{R1_Work_Order_string} resolves to the tag value",
      rr.resolve_text_tokens("{R1_Work_Order_string}", *W, "", fake) == "WO-4471",
      "this is what the operator typed and got back literally")
check("  it matches the prefixed form exactly",
      rr.resolve_text_tokens("{R1_Work_Order_string}", *W, "", fake)
      == rr.resolve_text_tokens("{tag:R1_Work_Order_string}", *W, "", fake))
check("  aggregates still need their prefix",
      rr.resolve_text_tokens("{max:TCU_Temp}", *W, "", fake) == "85.000")

print()
print("[run-time placeholders are NOT eaten as tag names]")
for token in ("{template}", "{id}", "{date}", "{time}", "{start}", "{end}"):
    check("  %s survives for the later pass" % token,
          rr.resolve_text_tokens(token, *W, "", fake) == token,
          "reading it as a tag would blank every existing pattern")

print()
print("[the whole destination path]")
got = rr.resolve_output_path(
    "C:/Users/Lucas/OneDrive/Trustnode/{R1_Work_Order_string}.pdf",
    template_name="Testing Report", report_id="rep-99",
    started="2026-10-07 14:05:44", ended="2026-10-07 14:25:10", fetch=fake)
check("a folder plus a bare tag name becomes a real path",
      got == "C:/Users/Lucas/OneDrive/Trustnode/WO-4471.pdf", got)
got2 = rr.resolve_output_path(
    "C:/reports/{date}/{R1_Work_Order_string}_{time}.pdf",
    template_name="T", report_id="r",
    started="2026-10-07 14:05:44", ended="2026-10-07 14:25:10", fetch=fake)
check("  mixed with run-time placeholders",
      got2 == "C:/reports/20261007/WO-4471_140544.pdf", got2)
check("  and the braces never reach the filesystem",
      "{" not in got and "{" not in got2,
      "a file literally named {R1_Work_Order_string} is the bug")

print()
print("[Save to + File name are TWO settings, not a contest]")
rr._trigger_rows = fake          # resolve_output_target uses the default fetch
BS = chr(92)
folder_win = "C:" + BS + "Users" + BS + "Lucas" + BS + "OneDrive" + BS + "Trustnode"


def target(folder, name):
    return rr.resolve_output_target(
        folder, name, template_name="Testing Report", report_id="rep-1",
        started="2026-10-07 14:05:44", ended="2026-10-07 14:25:10")


got = target(folder_win, "{R1_Work_Order_string}")
check("a FOLDER plus a name files INTO that folder",
      got == "C:/Users/Lucas/OneDrive/Trustnode/WO-4471.pdf", got)
check("  the folder is not mistaken for the file",
      not got.endswith("/Trustnode.pdf"),
      "every run overwrote OneDrive/Trustnode.pdf - that was the bug")
check("  a trailing separator works the same",
      target("C:/Reports/", "{R1_Work_Order_string}_{date}")
      == "C:/Reports/WO-4471_20261007.pdf")
check("a folder with NO name pattern still gets a readable name",
      target("C:/Reports", "") == "C:/Reports/Testing_Report_20261007_140544.pdf",
      "a folder full of uuids is what naming was added to fix")
check("a Save to that names a FILE is still a complete destination",
      target("C:/Reports/{R1_Work_Order_string}.pdf", "") == "C:/Reports/WO-4471.pdf")
check("a name with no folder keeps the default reports folder",
      target("", "{R1_Work_Order_string}") == "WO-4471.pdf")
check("  and neither set changes nothing", target("", "") == "",
      "existing schedules must keep their uuid behaviour")

print()
print("[the editor says which tokens exist]")
import io as _io                                          # noqa: E402
ui = _io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ScheduledReportsManager.jsx"),
              encoding="utf-8", errors="replace").read()
check("the File name field lists them on screen",
      "sr-token-hint" in ui and "{TagName}" in ui,
      "the syntax was only in a title attribute nobody hovers")
check("the Save to field has a folder picker",
      "pickOutputFolder" in ui and "Browse" in ui)
check("  and is validated as it is typed",
      "validateDirectoryPath" in ui and "sr-path-bad" in ui,
      "a typo used to surface only when a scheduled run failed overnight")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
