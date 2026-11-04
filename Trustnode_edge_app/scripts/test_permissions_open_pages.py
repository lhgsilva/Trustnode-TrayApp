# -*- coding: utf-8 -*-
"""A permission ticked on the Users page opens exactly the pages it names.

2026-11-04, reported: "when we create new users as operator or admin and give
them access in the permissions, some pages are not shown, like Scheduled
Reports". The page rule never read the permission catalogue the Users page is
drawn from: Scheduled Reports and E-mail checked "Users and access", and the
OEE, batch and power-configuration permissions were read by nothing.

  * the frontend copy of the catalogue equals the backend's (no drift);
  * for every non-admin permission: holding it opens its pages, not holding
    it keeps them closed, and an admin-only page stays closed whatever is
    ticked;
  * editing follows the roles the backend lets change the thing;
  * App.jsx asks the catalogue, and the role defaults give a new engineer
    Scheduled Reports, E-mail, OEE, batches and power configuration.

    python scripts/test_permissions_open_pages.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def node(script: str) -> str:
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=60, cwd=SRC)
    return (res.stdout.strip() + ("\nERR " + res.stderr.strip() if res.returncode else "")).strip()


def main() -> int:
    print("a ticked permission opens exactly its pages")
    from app.services.permission_catalog import FEATURES
    keep = ("key", "legacy_keys", "pages", "admin_only", "write_roles")
    backend = [{k: f[k] for k in keep if k in f} for f in FEATURES]
    mod = os.path.join(SRC, "permissionCatalog.js").replace("\\", "/")
    out = node('import { PERMISSION_FEATURES } from "file:///%s"; console.log(JSON.stringify(PERMISSION_FEATURES));' % mod)
    try:
        frontend = json.loads(out)
    except Exception:
        frontend = None
    check("the frontend catalogue equals the backend's", frontend == backend,
          "regenerate frontend/src/permissionCatalog.js from permission_catalog.py")

    cases = []
    for f in backend:
        for page in f.get("pages") or []:
            cases.append((f["key"], page, bool(f.get("admin_only"))))
    out = node('''
import { cataloguePageOpen, cataloguePageEdit } from "file:///%s";
const cases = %s;
const res = cases.map(([key, page, adminOnly]) => [key, page, adminOnly,
  cataloguePageOpen(page, { [key]: true }), cataloguePageOpen(page, {})]);
const edit = [
  cataloguePageEdit("scheduled_reports", { scheduled_reports: true }, "engineer"),
  cataloguePageEdit("scheduled_reports", { scheduled_reports: true }, "operator"),
  cataloguePageOpen("scheduled_reports", { scheduled_reports: true }),
  cataloguePageOpen("scheduled_reports", { users_and_access_control: true }),
  cataloguePageOpen("email_and_notifications", { email_notifications: true }),
  cataloguePageOpen("data_export", {}),
];
console.log(JSON.stringify({ res, edit }));
''' % (mod, json.dumps(cases)))
    try:
        got = json.loads(out)
    except Exception:
        got = {"res": [], "edit": []}
        check("the catalogue functions run", False, out[:200])
    wrong = []
    for key, page, admin_only, with_it, without in got.get("res", []):
        if admin_only:
            if with_it is not False:
                wrong.append("%s opens admin-only %s" % (key, page))
        elif not (with_it is True and without is False):
            wrong.append("%s -> %s: with=%s without=%s" % (key, page, with_it, without))
    check("every non-admin permission opens its pages and only with it (%d page links)" % len(cases),
          not wrong and len(got.get("res", [])) == len(cases), "; ".join(wrong[:4]))
    e = got.get("edit", [])
    check("an engineer with Scheduled Reports may edit schedules", e[:1] == [True], repr(e))
    check("  an operator with it may open the page but not change schedules",
          e[1:3] == [False, True], repr(e))
    check("  'Users and access' alone no longer opens Scheduled Reports", e[3:4] == [False], repr(e))
    check("  the older spelling email_notifications still opens E-mail", e[4:5] == [True], repr(e))
    check("  a page the catalogue does not cover is left to the old rule (null)", e[5:6] == [None], repr(e))

    app = open(os.path.join(SRC, "App.jsx"), encoding="utf-8").read()
    check("App.jsx opens pages for non-admins through the catalogue",
          "cataloguePageOpen(page, currentUser?.permissions)" in app and 'from "./permissionCatalog.js"' in app)
    check("  and no longer sends Scheduled Reports to 'Users and access'",
          'page === "email_and_notifications" || page === "scheduled_reports"' not in app
          or "CATALOGUE_EDIT_PAGES.has(page)" in app)
    check("  new engineers start with Scheduled Reports, E-mail, OEE, batches and power configuration",
          all(k in app.split("const ROLE_PERMISSION_ADDITIONS = {", 1)[1].split("operator:", 1)[0]
              for k in ("scheduled_reports: true", "email_and_notifications: true", "oee: true",
                        "batch_management: true", "power_management: true")))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
