# -*- coding: utf-8 -*-
"""Saving a schedule a SECOND time must work, and keep every field.

2026-10-30, from a customer install: "Save failed: Save scheduled report
failed (HTTP 500)". Reproduced in one call:

    CREATE a new schedule   :  OK
    UPDATE an existing one  :  sqlite3.ProgrammingError
        Incorrect number of bindings supplied.
        The current statement uses 35, and there are 34 supplied.

`report_columns_json = ?` was added to the UPDATE's SET list without a value
in the parameter tuple. The INSERT supplied it, so CREATING a schedule
worked and EDITING one always raised - which is why only the customer with
a saved schedule ever saw it.

The damage was not the error message. The operator was editing where the
file is written; the save never landed, so every run kept using the previous
destination and the reports appeared in the wrong folder. It read as "the
reports are not being generated correctly", and the real fault was a failed
save two screens away.

upsert_schedule names each column twice - once in the UPDATE, once in the
INSERT - and its own comment already warned that a field missing from either
"saves cleanly and then vanishes". A comment is not a test. This edits a
saved schedule and checks every field survives the round trip, so the two
statements cannot drift apart again.

    python scripts/test_reports_schedule_roundtrip.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_sched_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("scheduled report: save, edit, save again")
    from app.state import reports_store as rs

    # The customer's shape: a cycle-triggered schedule writing into a folder
    # named from a tag, with extra columns and a destination rule.
    first = {
        "name": "R1",
        "template_id": "tpl-1",
        "trigger_mode": "cycle",
        "enabled": True,
        "output_path": r"C:\Trustnode Reports\{Work_Order}",
        "file_name_pattern": "{Work_Order}_{date}",
        "require_gateway_running": True,
        "start_edge": True,
        "segment_tag": "SEG",
        "segment_gateway": "gw-1",
        "id_tag": "Work_Order",
        "template_ids": ["tpl-1", "tpl-2"],
        "tag_conditions": [{"gateway_id": "gw-1", "tag_name": "TEST_CYCLE", "op": "==", "value": "1"}],
        "stop_conditions": [{"gateway_id": "gw-1", "tag_name": "TEST_CYCLE", "op": "==", "value": "0"}],
        "destination_rules": [{"tag": "Result", "op": "==", "value": "FAIL",
                               "output_path": r"D:\Rejects\{Work_Order}"}],
        "report_columns": [{"label": "Work order", "pattern": "{Work_Order}"},
                           {"label": "Result", "pattern": "{Result}"}],
        "recipients": ["a@example.com"],
        "deliver_email": True,
        "attach_csv": True,
    }

    created = rs.upsert_schedule(first)
    sid = str(created.get("id") or "")
    check("a new schedule saves", bool(sid))

    # THE bug: the second save of the SAME schedule.
    edited = {**first, "id": sid, "name": "R1 edited",
              "output_path": r"D:\Somewhere Else\{Work_Order}\final",
              "file_name_pattern": "{Work_Order}-{n}"}
    try:
        again = rs.upsert_schedule(edited)
        ok, why = True, ""
    except Exception as exc:                                  # noqa: BLE001
        again, ok, why = {}, False, "%s: %s" % (type(exc).__name__, exc)
    check("editing a SAVED schedule does not raise", ok, why)
    if not ok:
        print()
        print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
        return 1

    # The edit must actually take - a save that reports success and keeps
    # the old destination is the same outage with a friendlier message.
    check("the new folder is what was stored",
          str(again.get("output_path") or "") == edited["output_path"],
          repr(again.get("output_path")))
    check("the new file name is what was stored",
          str(again.get("file_name_pattern") or "") == edited["file_name_pattern"],
          repr(again.get("file_name_pattern")))

    # And read it back from the store, not just the return value.
    stored = rs.get_schedule(sid) or {}
    check("a reload shows the edited destination",
          str(stored.get("output_path") or "") == edited["output_path"],
          repr(stored.get("output_path")))

    # Every field named in BOTH statements must survive an edit. This is the
    # list that keeps drifting; each entry is a field someone added to one
    # statement and not the other.
    for field, want in (
        ("name", "R1 edited"),
        ("trigger_mode", "cycle"),
        ("id_tag", "Work_Order"),
        ("segment_tag", "SEG"),
        ("segment_gateway", "gw-1"),
        ("template_ids", ["tpl-1", "tpl-2"]),
        ("destination_rules", first["destination_rules"]),
        ("report_columns", first["report_columns"]),
        ("tag_conditions", first["tag_conditions"]),
        ("stop_conditions", first["stop_conditions"]),
        ("recipients", ["a@example.com"]),
    ):
        check("  %s survives the edit" % field, stored.get(field) == want,
              "stored %r" % (stored.get(field),))

    for flag in ("enabled", "require_gateway_running", "start_edge",
                 "deliver_email", "attach_csv"):
        check("  %s survives the edit" % flag, bool(stored.get(flag)) is True,
              "stored %r" % (stored.get(flag),))

    # Three saves in a row, because the first edit is not the only one an
    # operator makes.
    try:
        rs.upsert_schedule({**edited, "name": "R1 third"})
        third = rs.get_schedule(sid) or {}
        check("a third save still works", str(third.get("name")) == "R1 third")
    except Exception as exc:                                  # noqa: BLE001
        check("a third save still works", False, str(exc))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
