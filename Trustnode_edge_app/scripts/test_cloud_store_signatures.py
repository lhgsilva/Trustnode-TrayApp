# -*- coding: utf-8 -*-
"""The cloud control-plane store must accept everything the SQLite one does.

ControlPlaneStoreCloud subclasses ControlPlaneStore and re-implements a
handful of methods to cope with Postgres. Every re-implementation is a
copy of a signature, and a copy drifts.

On 2026-08-22 `package_key`, `seats` and `limits` were added to
ControlPlaneStore.upsert_license. The cloud override was not updated, so
on the portal - which runs the CLOUD store - creating a licence raised

    TypeError: ControlPlaneStoreCloud.upsert_license() got an unexpected
               keyword argument 'package_key'

FastAPI turned that into a bare 500 with no detail, so the portal showed
"Internal Server Error" and nothing said which argument was wrong.
Creating a licence was impossible from 2026-08-22 until 2026-10-22, and
nothing failed on the SQLite path, which is what every test used.

This checks the whole class of defect rather than the one instance: for
every method the cloud store overrides, it must accept at least the
keyword arguments the parent accepts. Adding new ones is fine.

Import-safe: it touches classes only. No store is constructed, so no
database is opened, created or migrated.
"""
from __future__ import annotations

import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "backend"))

from app.services.control_plane_store import ControlPlaneStore            # noqa: E402
from app.services.control_plane_store_cloud import ControlPlaneStoreCloud  # noqa: E402

FAILS: list[str] = []


def kwargs_of(fn) -> set:
    """Keyword arguments a caller may pass. **kwargs accepts anything."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return set()
    names = set()
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return {"**"}
        if p.kind in (inspect.Parameter.KEYWORD_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD):
            if p.name != "self":
                names.add(p.name)
    return names


print("The cloud control-plane store accepts what the SQLite one accepts")
print()

checked = 0
for name, child_fn in sorted(vars(ControlPlaneStoreCloud).items()):
    if name.startswith("__") or not callable(child_fn):
        continue
    parent_fn = getattr(ControlPlaneStore, name, None)
    if parent_fn is None or not callable(parent_fn):
        continue  # cloud-only helper, nothing to match
    parent_kw = kwargs_of(parent_fn)
    child_kw = kwargs_of(child_fn)
    if "**" in child_kw:
        continue
    checked += 1
    missing = sorted(parent_kw - child_kw)
    status = "PASS" if not missing else "FAIL"
    print("  %-34s %s%s" % (name, status,
                            ("  drops " + ", ".join(missing)) if missing else ""))
    if missing:
        FAILS.append("%s drops %s" % (name, ", ".join(missing)))

print()
print("  %d overridden method(s) checked" % checked)
if checked == 0:
    FAILS.append("checked nothing - the comparison itself is broken")

print()
print("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + "; ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
