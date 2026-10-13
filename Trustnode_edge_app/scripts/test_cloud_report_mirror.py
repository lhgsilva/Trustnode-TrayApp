# -*- coding: utf-8 -*-
"""The cloud mirror's SQL actually binds its parameters.

2026-09-30, operator: the phone's cloud Reporting page must show reports and
templates "lively... not a copy". It showed nothing: Supabase held 0
generated_reports and 1 report_template while the edge had thousands.

The cause was four statements written as `:param::jsonb`. SQLAlchemy's
text() reads that as a bind parameter whose name absorbs part of the cast -
`:meta_json::jsonb` parses as the parameter `meta_jso` followed by stray SQL
- so the statement never parsed, psycopg2 raised a syntax error, and the only
record of it was a log.debug nobody enables. Every mirror had failed since
the feature shipped, while the PDF upload (a REST call, no SQL) succeeded and
made it look like the pipeline worked.

This test needs no database: the defect is in how text() parses the string,
so parsing the real statements out of the real modules proves it.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


import sqlalchemy as sa  # noqa: E402

print("TrustNode - the cloud report mirror binds what it says it binds")
print()

SOURCES = {
    "reports_cloud_uploader.py": os.path.join(
        ROOT, "backend", "app", "services", "reports_cloud_uploader.py"),
    "reports_store.py": os.path.join(
        ROOT, "backend", "app", "services", "reports_store.py"),
}

print("[no statement may use the ::cast form on a bind parameter]")
for label, path in SOURCES.items():
    src = io.open(path, encoding="utf-8", errors="replace").read()
    # Strip comments: the explanation of the bug names the broken form.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    bad = re.findall(r":[a-z_0-9]+::[a-z]+", code)
    check("%-28s has no :param::cast" % label, not bad, bad[:3])

print()
print("[the statements parse, and every parameter survives]")


def statements(src):
    """Each text(f\"\"\"...\"\"\") block that inserts into a cloud table."""
    for m in re.finditer(r'text\(\s*f?"""(.*?)"""', src, re.S):
        body = m.group(1)
        if "INSERT INTO" in body.upper():
            yield body.replace("{schema}", "public")


total = 0
for label, path in SOURCES.items():
    src = io.open(path, encoding="utf-8", errors="replace").read()
    for i, body in enumerate(statements(src), 1):
        total += 1
        try:
            binds = set(sa.text(body)._bindparams.keys())
            err = ""
        except Exception as exc:
            binds, err = set(), str(exc)[:120]
        # Every :name written in the statement must come back intact. A
        # truncated name is the signature of the bug.
        written = set(re.findall(r"(?<!:):([a-z_][a-z_0-9]*)\b", body))
        written = {w for w in written if w not in ("jsonb",)}
        missing = sorted(written - binds)
        check("%-22s stmt %d: parameters intact" % (label, i),
              not err and not missing,
              err or ("truncated/lost: %s" % missing))

check("both modules were actually inspected", total >= 3, "%d statement(s)" % total)

print()
print("[a mirror failure is visible]")
up = io.open(SOURCES["reports_cloud_uploader.py"], encoding="utf-8",
             errors="replace").read()
check("mirror failures log at WARNING, not debug",
      'log.warning("Lite generated_reports mirror failed' in up,
      "a debug-level failure is how this went unnoticed for the life "
      "of the feature")
check("  upload failures too",
      'log.debug("Lite storage upload' not in up)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
