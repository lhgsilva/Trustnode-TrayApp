# -*- coding: utf-8 -*-
"""Report metadata must always reach the cloud; the PDF rides alongside it.

2026-10-13, requested: "make sure that the metadata is pushed to the cloud so
the report can be regenerated and downloaded, and also attach the file in
parallel".

Two defects stood in the way.

1. upload_and_mirror() returned EARLY when the record carried no file_path,
   so the metadata never reached the cloud at all. The report did not exist
   there: nothing to list, nothing to regenerate, nothing to even know about.
   A failing upload had the same effect, because the mirror only ran after
   the upload returned.

2. The scheduler recorded `"meta": {}`. The mirrored row therefore said WHICH
   template ran but not over WHAT PERIOD. Regenerating from it would silently
   fall back to the template's default range and produce a different report
   under the same name - the kind of wrong that looks right.

Metadata is the authoritative half: it lists the report and carries the
window a regeneration needs. It is now written unconditionally and never
waits on the bytes; the upload runs on its own thread and attaches the
storage_path when it lands.

Drives the real function with a fake uploader and mirror. No network.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

# Scratch workspace BEFORE any app import - importing app.state instantiates
# AppStore and migrates whatever database the environment points at.
_TMP = tempfile.mkdtemp(prefix="tn-reportmeta-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "scratch.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - report metadata always lands, the file rides alongside")
print()

import app.services.reports_cloud_uploader as up                     # noqa: E402

mirrors: list[tuple] = []     # (storage_path, at_time)
uploads: list[tuple] = []     # (file_path, at_time)


def install(upload_delay=0.0, upload_result="obj/key.pdf", upload_raises=False):
    mirrors.clear(); uploads.clear()
    t0 = time.time()

    def fake_upload(file_path, **kw):
        uploads.append((file_path, time.time() - t0))
        if upload_delay:
            time.sleep(upload_delay)
        if upload_raises:
            raise RuntimeError("storage unreachable")
        return upload_result

    def fake_mirror(record, storage_path):
        mirrors.append((storage_path, time.time() - t0))
        return True

    up.upload_pdf_to_storage = fake_upload
    up.mirror_generated_row = fake_mirror
    return t0


REC = {"id": "rpt-1", "tenant_id": "tenant-a", "file_path": "/tmp/r.pdf",
       "file_name": "r.pdf", "template_id": "tpl-1"}

print("[the metadata lands even when there is no file]")
install()
sp = up.upload_and_mirror({k: v for k, v in REC.items() if k != "file_path"})
check("no file_path still mirrors the metadata", len(mirrors) >= 1,
      "it returned early before - the report did not exist in the cloud at "
      "all: %d mirror(s)" % len(mirrors))
check("  and reports no storage_path", sp is None, sp)
check("  without attempting an upload", uploads == [], uploads)

print()
print("[the metadata lands even when the upload FAILS]")
install(upload_raises=True)
sp = up.upload_and_mirror(dict(REC))
check("a storage outage does not cost us the metadata", len(mirrors) >= 1,
      "%d mirror(s)" % len(mirrors))
check("  and the failure is reported honestly", sp is None, sp)

print()
print("[the file is attached IN PARALLEL, not before]")
install(upload_delay=1.2)
t0 = time.time()
sp = up.upload_and_mirror(dict(REC))
elapsed = time.time() - t0
check("the upload started", len(uploads) == 1, uploads)
check("  the metadata was written WHILE it uploaded",
      mirrors and mirrors[0][1] < 1.0,
      "first mirror at %.2fs into a %.1fs upload - sequencing them would "
      "delay the report's existence by the whole upload"
      % (mirrors[0][1] if mirrors else -1, 1.2))
check("  the storage_path is attached once it lands", sp == "obj/key.pdf", sp)
check("  by a second upsert carrying the path",
      any(m[0] == "obj/key.pdf" for m in mirrors),
      [m[0] for m in mirrors])
check("  and the first upsert carried none (COALESCE keeps it safe)",
      mirrors[0][0] is None, mirrors[0][0])
check("  the whole call still waits for the file", elapsed >= 1.1,
      "%.2fs - the caller runs this on a background thread anyway" % elapsed)

print()
print("[the upsert cannot null an existing path]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "reports_cloud_uploader.py"), encoding="utf-8").read()
check("storage_path is COALESCEd on conflict",
      "storage_path  = COALESCE(EXCLUDED.storage_path" in SRC,
      "the metadata-first write passes NULL; without COALESCE it would "
      "erase the path of a report that already uploaded")
check("  the join has a bounded timeout",
      "_UPLOAD_JOIN_TIMEOUT_S" in SRC,
      "a hung bucket must not pin the thread for ever")

print()
print("[the window is recorded, so a report can be REGENERATED]")
SCH = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "report_scheduler.py"), encoding="utf-8").read()
check('the scheduler no longer records an empty meta',
      '"meta": {},' not in SCH,
      "it said WHICH template ran but not over what period")
for key, why in (("window_from", "the run's start"),
                 ("window_to", "the run's end"),
                 ("segment_count", "how many sub-periods it covered"),
                 ("report_id", "the serial/batch it was stamped with"),
                 ("trigger", "what fired it")):
    check("  meta carries %-14s (%s)" % (key, why), '"%s":' % key in SCH)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
