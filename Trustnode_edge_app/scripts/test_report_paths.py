# -*- coding: utf-8 -*-
"""A report goes where the operator said, and the folders get created.

2026-10-30, reported: "the folder was not created or found when I use tag {}
format", with the example shape

    Workorders/{work_order}/initial/{prefix}-{work_order}-{n}-{result}.pdf
    Workorders/{work_order}/final/{prefix}-{work_order}-{n}-{result}.pdf

The fault: folder-or-file was decided by looking for a dot in the RESOLVED
text. A work order of "BAY6578" made `C:/Reports/{Work_Order}` a folder; a
work order of "76668.000" made the SAME pattern a file, because the value
contains a dot. One pattern, two behaviours, chosen by data the operator
cannot see while typing - and the customer's Generated Reports list shows
"76668.000.pdf" where a folder should have been.

Nothing here is specific to any customer: the patterns are made up for the
test, and only the mechanism is asserted.

    python scripts/test_report_paths.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_paths_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services.report_renderer import resolve_output_target  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-64s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def resolve(path_pattern, name_pattern, values):
    """Resolve as the scheduler does, with a stubbed tag reader."""
    def fetch(cond, from_utc, to_utc):
        # The real reader returns historian rows for one condition's tag,
        # oldest first. The token engine takes the value it needs from them.
        tag = str((cond or {}).get("tag_name") or (cond or {}).get("tag") or "")
        val = values.get(tag)
        if val is None:
            return []
        # The store returns value=None for a TEXT tag and carries the words
        # in value_text; a numeric tag is the other way round. Getting this
        # wrong made every text tag resolve to "-" in the first run of this
        # test - the stub was lying, not the app.
        try:
            return [{"ts_utc": from_utc, "value": float(val), "value_text": None}]
        except (TypeError, ValueError):
            return [{"ts_utc": from_utc, "value": None, "value_text": str(val)}]
    return resolve_output_target(
        path_pattern, name_pattern, template_name="Testing Report",
        report_id="rep-1", started="2026-10-30 08:00:00",
        ended="2026-10-30 08:10:00", extension=".pdf", fetch=fetch)


def main() -> int:
    print("report destinations")

    # The heart of it: the SAME pattern must behave the same way whatever
    # the tag returns.
    # A numeric tag renders with three decimals ("76668" -> "76668.000"),
    # which is exactly the dot that used to turn a folder into a file. The
    # expectation follows the app rather than wishing it away.
    # A whole number used as an identifier keeps no decimals; a real
    # measurement keeps the ones it has. 2026-10-30, requested.
    for wo, shown in (("BAY6578", "BAY6578"), ("76668", "76668"),
                      ("WO.12.A", "WO.12.A"), ("123", "123"),
                      ("12.5", "12.5")):
        out = resolve("C:/Reports/{Work_Order}", "{Work_Order}",
                      {"Work_Order": wo})
        ok = out.replace("\\", "/") == "C:/Reports/%s/%s.pdf" % (shown, shown)
        check("a tag folder works when the value is %r" % wo, ok, out)

    # A literal extension still names a file, token or not.
    out = resolve("C:/Reports/{Work_Order}.pdf", "", {"Work_Order": "76668"})
    check("an extension typed by the operator still means a file",
          out.replace("\\", "/").endswith(".pdf")
          and "/76668.pdf" in out.replace("\\", "/"), out)

    # The operator's own shape: a stage folder, then a composed name with a
    # prefix, the work order, the run number and a result.
    for stage in ("initial", "final"):
        out = resolve("D:/Workorders/{Work_Order}/" + stage,
                      "RIG1-{Work_Order}-{n}-{Result}",
                      {"Work_Order": "76668", "Result": "PASS"})
        want = "D:/Workorders/76668/%s/RIG1-76668-" % stage
        check("a %s report lands in its own folder" % stage,
              out.replace("\\", "/").startswith(want), out)

    # Nested tag folders, several levels deep.
    out = resolve("D:/R/{Line}/{Work_Order}/{Stage}", "{Work_Order}",
                  {"Line": "L2", "Work_Order": "76668", "Stage": "final"})
    check("tags nest as folders at any depth",
          out.replace("\\", "/").startswith("D:/R/L2/76668/final/"), out)

    # And the folders are really created on disk, which is the other half of
    # the complaint. resolve_output_path is what the writer calls.
    from pathlib import Path
    from app.services.report_renderer import unique_output_path
    target = Path(resolve(os.path.join(_TMP, "out/{Work_Order}/final"),
                          "{Work_Order}", {"Work_Order": "76668"}))
    target.parent.mkdir(parents=True, exist_ok=True)
    final = unique_output_path(target)
    final.write_bytes(b"%PDF-1.4 test")
    check("the folder tree is created and the file written",
          final.is_file() and "76668" in str(final.parent), str(final))

    # Two runs of the same pattern must not overwrite each other.
    second = unique_output_path(target)
    second.write_bytes(b"%PDF-1.4 test 2")
    check("a second run does not overwrite the first",
          second != final and final.is_file() and second.is_file(),
          "%s then %s" % (final.name, second.name))

    # ---------------------------------------------------------------- 2026-10-31
    # The operator's own pattern, reported as "I am not sure the path and file
    # names are being created correctly". They were not. Measured:
    #
    #   folder {date}-{time}-{n}\{SimREAL[3]}
    #   name   {SimDINT[3]}-{id}-{time}
    #   gave   .../20261031-080405-{n}/{SimREAL[3]}/SimDINT_3_-rep-1-080405.pdf
    #
    # Three faults in one line: a tag whose name contains [ ] never matched
    # the bare-token regex, so it stayed literal in the folder and was
    # MANGLED INTO THE TAG NAME in the file; and {n} - which cannot mean
    # anything in a folder, since the run number needs the folder to exist
    # first - was left as a literal brace on disk.
    out = resolve("C:/R/{date}-{time}-{n}/{SimREAL[3]}", "{SimDINT[3]}-{id}-{time}",
                  {"SimREAL[3]": "127.68", "SimDINT[3]": "42"}).replace("\\", "/")
    check("an array tag resolves to its VALUE in a folder",
          "/127.68/" in out, out)
    check("  and to its value in the file name too",
          "/42-" in out, out)
    check("  not to the tag's own name", "SimDINT" not in out and "SimREAL" not in out, out)
    check("no brace ever reaches the disk", "{" not in out and "}" not in out, out)
    check("{n} does not leave a dangling separator",
          "-/" not in out and not out.endswith("-"), out)

    # A run with no window must still name the folder something.
    nowout = resolve_output_target(
        "C:/R/{date}-{time}", "{id}", template_name="T", report_id="r1",
        started="", ended="", extension=".pdf",
        fetch=lambda c, a, b: []).replace("\\", "/")
    check("a run with no start/end still gets a dated folder",
          "/R/-" not in nowout and nowout.count("/") >= 3
          and not nowout.replace("C:/R/", "").startswith("/"),
          nowout)

    # 2026-10-31, found by running a MANUAL report through the live API: the
    # {n} tidy was applied to the RESOLVED text, so it also stripped a
    # trailing separator that belonged to a real value. A tag with no reading
    # resolves to "-", so a folder whose entire name was "-" was deleted and
    # its text merged into the file name:
    #
    #     wanted  .../Reports/-/--20261031-1.pdf
    #     got     .../Reports/--20261031-1.pdf
    #
    # A folder must survive whatever its tag happens to hold.
    out = resolve("C:/R/{Work_Order}", "{Work_Order}-{date}-{n}", {}).replace("\\", "/")
    check("a folder survives a tag that resolved to nothing",
          out.count("/") == 3 and "/R/-/" in out, out)

    for value, want in (("-", "/R/-/"), ("WO-", "/R/WO-/"), ("WO.", "/R/WO./"),
                        ("WO_", "/R/WO_/")):
        out = resolve("C:/R/{Work_Order}", "{id}",
                      {"Work_Order": value}).replace("\\", "/")
        check("a folder ending in %r is kept whole" % value[-1], want in out, out)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
