# -*- coding: utf-8 -*-
"""The Backup and Retention page tells one story, and backs up only on request.

2026-10-19, reported: "the database and retention page is not showing all the
information correct, it should not making backups automatcly unless set in
the policy of backups ... no duplicated data ... has to be minimum
information for the users to no get confused".

The screenshot showed the page contradicting itself. A "Database size" card
read "0 MB / 0 readings / no policy" directly above a Storage table reading
"13.49 GB / 17,306,633 readings" with the Lean policy marked Active. They
were two cards fed by two different endpoints, each repeating the same four
facts; when one endpoint was slow or failed, its zeros sat beside the other's
real numbers.

And backups happened on their own: backup_before_cleanup defaulted to True in
FOUR places, so a cleanup copied the whole database first. A separate
safety copy from the database swap was 13.85 GB on a disk with 35 GB free -
larger than the live database.

Source-level: no store, no network.
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


def read(*p):
    return io.open(os.path.join(ROOT, *p), encoding="utf-8").read()


print("TrustNode - one source per fact, and no backup nobody asked for")
print()

PANEL = read("frontend", "src", "components", "Retention", "RetentionPanel.jsx")

print("[1. the edge's numbers come from ONE place]")
check("the duplicate Database size card is gone",
      "function StorageStatusCard" not in PANEL,
      "it repeated size, readings, dead space and policy state from a "
      "second endpoint, and showed 0 MB beside the real 13.49 GB")
check("  what replaced it covers only the cloud",
      "function CloudSizeStrip" in PANEL,
      "the cloud database is the one thing the Storage table cannot know")
check("  and it draws nothing when there is no cloud database",
      "cloud.configured === false) return null" in PANEL,
      "an empty card is still clutter on a page that must stay minimal")
for gone in ("Dead space", "Automatic cleanup"):
    check("  %-18s is no longer duplicated" % ('"%s"' % gone),
          PANEL.count(gone) == 0,
          "the Storage table already reports this")

print()
print("[2. the Storage table is still the one that reports it]")
for keep in ("Full-detail history", "Disk free", "Reclaimable"):
    check("  the table still shows %-22s" % ('"%s"' % keep), keep in PANEL)

print()
print("[3. a backup happens only when asked for]")
SVC = read("backend", "app", "services", "app_store.py")
ROUT = read("backend", "app", "routers", "app_store.py")
check("the API default is OFF",
      "backup_before_cleanup: bool = False" in ROUT,
      "it was True, so any caller that omitted the flag got a full copy")
check("  the column default is OFF",
      "backup_before_cleanup INTEGER NOT NULL DEFAULT 0," in SVC)
check("  the settings default is OFF",
      '"backup_before_cleanup": False,' in SVC)
check("  an absent flag on save means OFF",
      'current.get("backup_before_cleanup", False)' in SVC)
check("  and the cleanup itself asks for OFF",
      'policy.get("backup_before_cleanup", False)' in SVC,
      "this was the one that actually wrote the file")
check("  no decision point defaults to True any more",
      'backup_before_cleanup", True' not in SVC
      and "backup_before_cleanup: bool = True" not in ROUT,
      "four separate places decided this; missing one leaves the behaviour")

print()
print("[4. a safety copy is kept only until the swap proves itself]")
ENG = read("backend", "app", "services", "retention_engine.py")
check("boot reclaims copies the live database has outlived",
      "_drop_proven_safety_copies" in ENG,
      "13.85 GB was being kept for ever as an undo nobody could use")
check("  it refuses while a swap is pending",
      '.restore_pending' in ENG and 'return' in ENG,
      "mid-operation the copy is the only way back")
check("  it refuses when the live database is NOT ok",
      "quick_check" in ENG and "the live file is suspect" in ENG,
      "a failing database is exactly when the undo matters")
check("  and it says how much it returned",
      "returned to the disk" in ENG)

print()
print("[5. nothing is backed up on its own]")
ENGINE = ENG
check("DEFAULT_BACKUPS is OFF",
      ('DEFAULT_BACKUPS = {' + chr(10) + '    "enabled": False,') in ENGINE,
      "the page said it out loud: 'Settings are backed up daily by default "
      "(14 kept), even without a retention policy'")
check("  an absent flag on READ means OFF",
      'cfg.get("enabled", False)' in ENGINE,
      "defaulting True here keeps the old behaviour for every policy saved "
      "before the flag existed")
check("  an absent flag on NORMALISE means OFF",
      'bk_in.get("enabled", False)' in ENGINE)
check("  and the page no longer promises a daily copy",
      "backed up daily by default" not in PANEL,
      "telling the operator it happens while it no longer does is its own "
      "kind of wrong")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
