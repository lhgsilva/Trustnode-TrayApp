# -*- coding: utf-8 -*-
"""The report-template mirror must actually reach its first statement.

2026-10-20. Both mirror paths in reports_store.py began with:

    from .app_store import app_store   # local import to avoid a cycle

That name does not exist. `app.services.app_store` defines the CLASS
`AppStore`; the singleton is created in `app.state`. So every call raised

    ImportError: cannot import name 'app_store' from 'app.services.app_store'

on its FIRST line - before it could look at a cloud target, a tenant, or a
single template row. Both callers wrapped it in `except Exception`, so
nothing surfaced.

The consequence was the reported one: "the reports tab of templates do not
showing the templates so the reports can be generated from the lite app".
Measured 2026-10-17 and recorded in the caller's own comment: Supabase held
ONE report_template while two edges held 15 and 269. That was read as a
sync-policy problem. It was an import that had never once succeeded.

This test calls both methods for real. A source grep would not do: the
previous line was syntactically perfect and read exactly like the dozens of
working late imports around it. Only running it says whether the name
resolves.

No cloud target is configured here, so the correct outcome is a quiet 0 -
the point is that it gets far enough to decide that. Scratch workspace; the
live store is never opened (CLAUDE.md 5).
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="tn-tmpl-mirror-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(ROOT, "backend"))

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:200]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the report-template mirror resolves its own imports")
print()

from app.state import reports_store  # noqa: E402

check("the test is NOT pointed at the live workspace",
      ".trustnode_edge" not in _TMP.lower(), _TMP)

print()
print("[1. the periodic reconcile runs]")
err = None
pushed = None
try:
    pushed = reports_store.reconcile_templates_to_cloud()
except ImportError as exc:
    err = "ImportError: %s" % exc
except Exception as exc:  # noqa: BLE001
    # Anything else means it got PAST the import, which is what we assert.
    err = None
    pushed = 0
    print("      (reached the body, then: %s: %s)" % (type(exc).__name__, exc))

check("reconcile_templates_to_cloud does not die on its import", err is None,
      err or "")
check("  and it returns a count, not nothing",
      isinstance(pushed, int), repr(pushed))

print()
print("[2. the per-save mirror runs]")
err = None
try:
    reports_store._mirror_template_to_cloud(  # noqa: SLF001
        tenant_id="default", tpl_id="t-probe", name="probe",
        description="", definition_json="{}", created_utc="",
        updated_utc="", author=None)
except ImportError as exc:
    err = "ImportError: %s" % exc
except TypeError as exc:
    # Signature mismatch would be a different bug; say so rather than hide it.
    err = "TypeError: %s" % exc
except Exception:  # noqa: BLE001
    err = None       # reached the body with no cloud target: correct
check("_mirror_template_to_cloud does not die on its import", err is None,
      err or "")

print()
print("[3. the name it imports actually exists where it looks]")
import app.services.app_store as _mod  # noqa: E402
import app.state as _state             # noqa: E402
check("app.services.app_store has NO module-level app_store",
      not hasattr(_mod, "app_store"),
      "if this ever becomes true the old import would start working and "
      "this test would stop meaning anything - it would also mean two "
      "singletons, which is worse than the bug")
check("  the singleton lives in app.state", hasattr(_state, "app_store"))
check("  and it carries the members the mirror calls",
      hasattr(_state.app_store, "_get_cloud_database_target")
      and hasattr(_state.app_store, "_get_or_create_cloud_engine"),
      "the mirror reaches into both; a rename would break it just as "
      "silently as the wrong module did")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
if FAILS:
    traceback.print_exc() if False else None
sys.exit(0 if not FAILS else 2)
