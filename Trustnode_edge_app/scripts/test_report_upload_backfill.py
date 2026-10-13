# -*- coding: utf-8 -*-
"""A report whose PDF never uploaded must be able to recover.

2026-10-13, reported with a screenshot: TrustNode Lite listed a customer's
reports with every card stuck at "UPLOADING" and Preview/Download dead.

Measured in the live cloud:

    tenant-cust-e5916328 (Mari)      32 reports /  32 with a stored PDF
    tenant-cust-b47b1b83 (Lucas-02) 269 reports /   0 with a stored PDF

"UPLOADING" is not a status column - the Lite UI shows it whenever
storage_path is empty and there is no local_file_url. It was telling the
truth: those PDFs were never in Storage, so there was nothing to fetch.

storage_path is set by exactly one thing: the fire-and-forget upload thread
in ReportsStore.insert_generated. If that fails - Supabase unreachable, the
bucket misconfigured, an older build - nothing ever retried it, and this
poller's backfill made it UNRECOVERABLE:

  * it returned early once the cloud held >= 50 rows, i.e. exactly when a
    backlog existed;
  * it called mirror_generated_row(r, r["storage_path"]) - writing the SAME
    empty storage_path back - so the row could never heal.

The fix retries the upload for any row missing a storage_path, capped per
boot, while leaving the metadata mirror's original throttle alone.

Drives the real method with a fake store and fake uploader. No network, no
Supabase, nothing written outside the test.
"""
from __future__ import annotations

import io
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

# Point the workspace at a scratch directory BEFORE importing anything that
# can reach app.state. Importing it instantiates AppStore, which runs
# _ensure_schema() and ALTERs whatever database the environment points at.
# The first run of this file did exactly that against the LIVE store and was
# saved only by the file being read-only.
import tempfile                                                      # noqa: E402
_TMP = tempfile.mkdtemp(prefix="tn-backfill-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "scratch.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - a report stuck at UPLOADING can recover")
print()

SRC_PATH = os.path.join(ROOT, "backend", "app", "services", "lite_report_poller.py")
SRC = io.open(SRC_PATH, encoding="utf-8").read()

print("[the defect, asserted at the source]")
check("the >=50 early return is gone",
      "skip backfill\", n_cloud)\n            return" not in SRC
      and "mirror_metadata = n_cloud < 50" in SRC,
      "it skipped precisely when there was a backlog worth fixing")
check("  a missing PDF is retried, not re-mirrored as missing",
      "upload_and_mirror(r)" in SRC,
      "mirror_generated_row(r, r['storage_path']) wrote the same empty "
      "path back, so the row could never heal")
check("  the local storage_path is written back on success",
      "_set_storage_path(" in SRC)
check("  retries are capped per boot",
      "_BACKFILL_UPLOAD_CAP" in SRC,
      "the upload shares a pool with the historian writer")

# ---------------------------------------------------------------- behaviour
print()
print("[drive the real method]")

import app.services.lite_report_poller as poller_mod                 # noqa: E402

uploaded: list[str] = []
mirrored: list[tuple] = []
set_paths: list[tuple] = []


def fake_upload_and_mirror(rec):
    uploaded.append(str(rec.get("id")))
    return "tenant/%s.pdf" % rec.get("id")


def fake_mirror_generated_row(rec, sp):
    mirrored.append((str(rec.get("id")), sp))
    return True


fake_uploader = types.ModuleType("app.services.reports_cloud_uploader")
fake_uploader.upload_and_mirror = fake_upload_and_mirror
fake_uploader.mirror_generated_row = fake_mirror_generated_row
sys.modules["app.services.reports_cloud_uploader"] = fake_uploader


class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def list_generated(self, limit=50):
        return [dict(r) for r in self._rows[:limit]]

    def _set_storage_path(self, gid, sp):
        set_paths.append((gid, sp))


def run(rows, n_cloud):
    uploaded.clear(); mirrored.clear(); set_paths.clear()
    p = poller_mod.LiteReportRequestPoller.__new__(poller_mod.LiteReportRequestPoller)
    p.store = FakeStore(rows)
    p._get_cloud_target = lambda: {"schema": "public"}

    # Stub the cloud row-count probe: the method computes n_cloud through
    # app_store's engine, which we are not standing up here.
    import app.services.lite_report_poller as m
    real_engine_probe = getattr(m, "_test_n_cloud", None)
    p._n_cloud_override = n_cloud
    src = io.open(SRC_PATH, encoding="utf-8").read()
    # Execute the method body with n_cloud forced, by monkeypatching the
    # app_store import it uses.
    import app.state as state
    class _Conn:
        def execute(self, *_a, **_k):
            class _C:
                def fetchone(self_inner):
                    return (n_cloud,)
            return _C()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Eng:
        def begin(self): return _Conn()
    state.app_store._get_or_create_cloud_engine = lambda *a, **k: (_Eng(), None)
    poller_mod.LiteReportRequestPoller._backfill_existing_rows(p)
    _ = real_engine_probe
    return uploaded[:], mirrored[:], set_paths[:]


ROWS_BACKLOG = (
    [{"id": "r%d" % i, "storage_path": "", "file_path": "/tmp/r%d.pdf" % i}
     for i in range(5)]
    + [{"id": "ok%d" % i, "storage_path": "t/ok%d.pdf" % i,
        "file_path": "/tmp/ok%d.pdf" % i} for i in range(3)]
)

up, mir, sets = run(ROWS_BACKLOG, n_cloud=269)
check("with a large cloud backlog, missing PDFs are STILL uploaded",
      sorted(up) == ["r0", "r1", "r2", "r3", "r4"],
      "this is the reported case: 269 rows in cloud, 0 with a PDF: %s" % (up,))
check("  and the local storage_path is recorded", len(sets) == 5, sets)
check("  rows that already have a PDF are not re-uploaded",
      not any(u.startswith("ok") for u in up), up)
check("  the metadata mirror stays throttled off at that size",
      mir == [], "mirroring 200 rows every boot was never the intent: %s" % (mir,))

up2, mir2, _s2 = run(ROWS_BACKLOG, n_cloud=3)
check("on a sparse cloud it mirrors metadata as before",
      len(mir2) >= 3, mir2)
check("  and still uploads the missing PDFs", sorted(up2) == sorted(up), up2)

print()
print("[edge cases]")
no_file = [{"id": "x1", "storage_path": "", "file_path": ""}]
up3, mir3, s3 = run(no_file, n_cloud=3)
check("a row with no local file is not uploaded", up3 == [], up3)
check("  but its metadata still mirrors, so it appears in the list",
      len(mir3) == 1, mir3)
check("  and no bogus storage_path is written", s3 == [], s3)

many = [{"id": "m%d" % i, "storage_path": "", "file_path": "/tmp/m%d.pdf" % i}
        for i in range(60)]
up4, _m4, _s4 = run(many, n_cloud=269)
cap = poller_mod.LiteReportRequestPoller._BACKFILL_UPLOAD_CAP
check("a long backlog is capped per boot", len(up4) == cap,
      "%d uploaded, cap is %d - it drains over restarts rather than "
      "stalling live ingest once" % (len(up4), cap))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
