# -*- coding: utf-8 -*-
"""An update never adds report templates to a customer, and never changes one.

2026-11-16, from the owner: "when we are updating existing installations, we
should not create new templates in the customer, and never change the
existing templates on the customer database."

Found four automatic writers. This runs what a boot runs, on an ACTIVATED
edge store that holds the customer's own templates (one of them an edited
batch built-in) and, under `default`, a demo seed, a batch seed, a legacy
empty shell and one real template the customer built before 2026-11-02:

  * every customer template is byte-for-byte unchanged afterwards;
  * no demo / batch / empty template appears in the customer;
  * the customer's own pre-fix template IS moved in (theirs, not ours);
  * a fresh install (no templates at all) still gets the demo set;
  * batch built-ins are created once, on first use, and a deleted one is
    not put back;
  * main.py no longer seeds batch templates at import, and the legacy
    migration does not run on an activated edge.

    python scripts/test_update_never_touches_templates.py
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_tplupd_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-upd"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
EDGE = "tenant-cust-upd"
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def rows(tenant):
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    out = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT id, name, definition_json FROM report_templates WHERE tenant_id = ?", (tenant,))}
    con.close()
    return out


def put(tenant, tid, name, definition):
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("INSERT INTO report_templates (id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by) "
                "VALUES (?, ?, ?, '', ?, '2026-10-01 00:00:00', '2026-10-01 00:00:00', 'test')",
                (tid, tenant, name, json.dumps(definition, separators=(",", ":"))))
    con.commit()
    con.close()


def main() -> int:
    print("an update never adds or changes a customer's report templates")
    from app.state import app_store, reports_store
    from app.tenant import set_current_tenant
    from app.modules.batch_management import reports_v2
    set_current_tenant(EDGE)
    # wait for the import-time / deferred seeders of this process to settle, then start clean
    time.sleep(1.5)
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("DELETE FROM report_templates")
    con.commit()
    con.close()

    # the customer's own templates - one is an EDITED batch built-in with no batch_scope
    put(EDGE, "tpl-own-1", "Line 1 report", {"sections": [{"type": "header", "title": "Line 1"}]})
    put(EDGE, "tpl-batch-summary", "Batch Summary", {"sections": [{"type": "text", "text": "customer's own edit"}]})
    # under `default`: our seeds, a legacy shell, and one real customer template
    put("default", "tpl-demo-overview", "Demo overview", {"sections": [{"type": "header", "title": "Demo"}]})
    put("default", "tpl-batch-detail", "Batch Detail", {"sections": [{"type": "header", "title": "B"}], "batch_scope": "batch"})
    put("default", "legacy-1", "Imported template", {"sections": [], "filters": {}})
    put("default", "tpl-own-old", "Kiln report", {"sections": [{"type": "header", "title": "Kiln"}]})
    before = rows(EDGE)

    # ---- what a boot runs ---------------------------------------------------
    app_store._seed_and_reconcile_report_templates_once()          # noqa: SLF001
    time.sleep(1.5)                                                 # its thread waits 0.5 s first
    reports_v2.seed_report_templates()                              # what GET /report-templates does
    app_store.rehome_default_content(EDGE)
    after = rows(EDGE)

    unchanged = [tid for tid, v in before.items() if after.get(tid) != v]
    check("every customer template is byte-for-byte unchanged", not unchanged, repr(unchanged))
    check("  the customer's edited batch built-in kept its edit",
          "customer's own edit" in (after.get("tpl-batch-summary") or ("", ""))[1], repr(after.get("tpl-batch-summary")))
    added = sorted(set(after) - set(before))
    check("no demo, batch or empty template was added to the customer",
          not [t for t in added if t.startswith(("tpl-demo-", "tpl-batch-")) or t == "legacy-1"], repr(added))
    check("  the customer's own pre-fix template was moved in", added == ["tpl-own-old"], repr(added))

    # ---- a fresh install still gets the demo set -----------------------------
    fresh = "tenant-cust-fresh"
    orig = app_store._get_app_settings                              # noqa: SLF001
    app_store._get_app_settings = lambda: {"tenant_id": fresh}      # noqa: SLF001
    try:
        app_store._seed_and_reconcile_report_templates_once()      # noqa: SLF001
        time.sleep(1.5)
    finally:
        app_store._get_app_settings = orig                          # noqa: SLF001
    got = rows(fresh)
    check("a fresh install (no templates) still gets the demo set", any(t.startswith("tpl-demo-") for t in got), repr(sorted(got)))

    # ---- batch built-ins: once, on first use; a deleted one is not put back --
    set_current_tenant(fresh)
    n1 = reports_v2.seed_report_templates()
    n2 = reports_v2.seed_report_templates()
    batch_ids = [t for t in rows(fresh) if t.startswith("tpl-batch-")]
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("DELETE FROM report_templates WHERE tenant_id = ? AND id = ?", (fresh, batch_ids[0] if batch_ids else ""))
    con.commit()
    con.close()
    n3 = reports_v2.seed_report_templates()
    # A template id is unique across the whole file, so a built-in id already
    # used by ANOTHER tenant in this store (tpl-batch-summary, above) cannot be
    # created again; every other one is.
    check("batch built-ins are created on first use", n1 >= 3 and len(batch_ids) == n1, repr((n1, batch_ids)))
    check("  never again", n2 == 0, repr(n2))
    check("  and a deleted one is not put back", n3 == 0 and len([t for t in rows(fresh) if t.startswith("tpl-batch-")]) == len(batch_ids) - 1,
          repr(n3))

    main_src = open(os.path.join(ROOT, "backend", "app", "main.py"), encoding="utf-8").read()
    check("main.py does not seed batch templates at import", "_seed_batch_tpls()" not in main_src)
    check("the legacy migration does not run on an activated edge",
          "(not _act or _act == \"default\")" in main_src and "migrate_from_bootstrap(bootstrap" in main_src)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
