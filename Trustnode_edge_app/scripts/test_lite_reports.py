# -*- coding: utf-8 -*-
"""The phone can see, download and generate reports over a LAN link.

2026-09-29: "make sure that from the web app from the cloud the reports are
been seen / downloaded and also can be generated using the templates. I am
using the webapp in the phone and is not available there."

It was not available because the LAN surface had two halves missing:

  * the browser shim's `generated_reports` handler was a stub that returned
    [] with the note "reports queue is cloud-only", so the Reporting page was
    permanently empty even though the edge's reports_store held the rows;
  * there was no lite-local endpoint to LIST templates or to GENERATE from
    one, so the Templates tab could only queue a cloud request that the
    read-only shim can never insert.

Checked here against a real app on a scratch workspace: the endpoints exist,
they are scoped to the session's tenant, and a generate produces a PDF that
the download endpoint then serves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8183"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-lrep-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(m, path, tok=None, body=None, raw=False):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data.decode() or "null"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print("TrustNode - reports on the phone (LAN lite surface)")
print()
check("the app started", up)
if not up:
    sys.exit(2)

tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")
check("signed in", bool(tok))

print()
print("[a template the phone can name]")
st, made = call("POST", "/api/reports/templates", tok, {
    "name": "Phone Test Report",
    "description": "generated from the phone",
    "definition": {"sections": [{"type": "text", "text": "hello from the edge"}]},
})
tpl_id = ((made or {}).get("template") or {}).get("id") or (made or {}).get("id")
check("a template exists to generate from", st in (200, 201) and bool(tpl_id), tpl_id)

st, lst = call("GET", "/api/lite-local/report-templates", tok)
names = [r.get("name") for r in ((lst or {}).get("rows") or [])]
check("the phone can LIST templates", st == 200 and "Phone Test Report" in names,
      "there was no lite endpoint at all - the Templates tab had nothing to show")

print()
print("[generating from the phone]")
st, gen = call("POST", "/api/lite-local/reports/generate", tok, {"template_id": tpl_id})
rec = (gen or {}).get("generated") or {}
check("the phone can GENERATE from a template", st == 200 and bool(rec.get("id")),
      "it could only queue a cloud request the read-only shim cannot insert")
check("  it is recorded as coming from lite", rec.get("triggered_by") == "lite-manual",
      rec.get("triggered_by"))

st, missing = call("POST", "/api/lite-local/reports/generate", tok, {"template_id": "no-such"})
check("  an unknown template is refused, not rendered", st == 404, st)
st, _ = call("POST", "/api/lite-local/reports/generate", tok, {})
check("  a missing template_id is a 400", st == 400, st)

print()
print("[seeing and downloading it]")
st, rows = call("GET", "/api/lite-local/reports", tok)
ids = [r.get("id") for r in ((rows or {}).get("rows") or [])]
check("the phone SEES the generated report", st == 200 and rec.get("id") in ids,
      "the shim's handler used to return [] - 'reports queue is cloud-only'")

st, body = call("GET", "/api/lite-local/reports/%s/file" % rec.get("id"), tok, raw=True)
check("  and DOWNLOADS a real PDF",
      st == 200 and isinstance(body, bytes) and body[:5] == b"%PDF-",
      "%s / %s" % (st, (body[:12] if isinstance(body, bytes) else body)))

print()
print("[scope]")
# The generated id is a bare uuid in a URL. Serving it on id alone would let
# one tenant's phone pull another's PDF off the same edge.
src = open(os.path.join(ROOT, "backend", "app", "routers", "lite_local.py"),
           encoding="utf-8").read()
dl = src.split("def get_lite_report_file", 1)[-1].split("\ndef ", 1)[0]
check("the file download is scoped to the session's tenant",
      "tenant_id=tenant_id" in dl,
      "get_generated(id) alone crosses tenants")
for fn in ("get_lite_report_templates", "post_lite_generate_report"):
    blk = src.split("def " + fn, 1)[-1].split("\ndef ", 1)[0]
    check("  %-28s passes the tenant too" % fn, "tenant_id" in blk)

print()
print("[the phone app still parses]")
# index.html is one hand-written 5,000-line module and the shim is loaded
# straight by the browser. Neither is touched by the frontend build, so a
# syntax error in either ships silently and the phone renders a blank page.
node = subprocess.run(["node", os.path.join(ROOT, "scripts", "check_lite_view_syntax.mjs")],
                      cwd=ROOT, capture_output=True, text=True)
# Only the verdict: its own RESULT line would be mistaken for this test's.
_bad = [l.strip() for l in (node.stdout or "").splitlines() if ": FAIL" in l]
check("lite_view and its shim parse", node.returncode == 0,
      _bad[0][:140] if _bad else (node.stderr or "")[-140:])

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
