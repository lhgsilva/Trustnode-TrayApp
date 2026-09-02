# -*- coding: utf-8 -*-
"""The app must record its own resource use, and keep its connections cheap.

2026-08-31: a customer's install ran 24 hours and froze; a Task Manager
screenshot showing a 1.8 GB process was the only evidence anyone had. Eight
minutes of sampling from outside settled nothing - the service oscillated
between 452 MB and 602 MB with no trend - because a leak that takes a day to
matter cannot be seen in eight minutes.

Two things come from that.

1. THE APP MEASURES ITSELF. Memory, CPU, threads and handles for the service
   and the UI processes are appended to the historian every 30 s as ordinary
   tags, so every existing chart, alarm and report works on them and the
   question "what grew overnight?" has a timestamped answer.

2. CONNECTIONS ARE BOUNDED AND CHEAP. `with sqlite3.connect(...) as conn:`
   commits but does NOT close, so connections accumulated until the GC noticed,
   each holding up to 128 MB of page cache. Closing them per operation - my
   first fix - was worse: opening the live 15.9 GB store costs 1-3 s, so every
   query paid it again until the thread pool was full of threads inside
   sqlite3.connect and /api/auth/login timed out at 30 s while /api/health
   still answered in 0.10 s.

   One connection per THREAD is neither: bounded by thread count, opened once
   per thread, with a 16 MB cache that is now multiplied by tens rather than
   by an unbounded number of operations.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8174"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:54s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[connections are bounded AND cheap]")
tmp0 = tempfile.mkdtemp(prefix="tn-conn-")
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
os.environ.setdefault("TRUSTNODE_DATA_DIR", tmp0)
os.environ.setdefault("TRUSTNODE_APP_STORE_PATH", os.path.join(tmp0, "s.db"))
os.environ.setdefault("TRUSTNODE_BOOT_INTEGRITY_CHECK", "never")
from app.services.app_store import AppStore  # noqa: E402

store = AppStore.__new__(AppStore)
store._db_path = os.path.join(tmp0, "t.db")
with store._connect() as c:
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    cache_pages = int(c.execute("PRAGMA cache_size").fetchone()[0])

# The same thread gets the SAME connection back. Closing it per operation was
# my earlier fix and it wedged the app: opening the live 15.9 GB store costs
# 1-3 s, so every query paid it again until the thread pool was exhausted and
# login timed out at 30 s.
with store._connect() as c2:
    reused = c2 is c
check("a thread reuses its connection", reused,
      "opening this store costs seconds; paying it per query starved the pool")
with store._connect() as c3:
    committed = int(c3.execute("SELECT COUNT(*) FROM t").fetchone()[0]) == 1
check("  and the write was still committed", committed,
      "reuse must not cost durability")

# Bounded is the real requirement - a different thread gets its own handle,
# and the total can never exceed the number of threads that touch the store.
import threading  # noqa: E402
seen = {}


def _other():
    with store._connect() as oc:
        seen["conn"] = oc


t = threading.Thread(target=_other)
t.start()
t.join(timeout=30)
check("  a different thread gets its own", seen.get("conn") is not None
      and seen.get("conn") is not c,
      "one per thread: bounded by thread count, not by query count")

# And the second open must be cheap, which is the whole point.
import time as _t  # noqa: E402
_t0 = _t.time()
for _ in range(50):
    with store._connect() as cc:
        cc.execute("SELECT COUNT(*) FROM t").fetchone()
_elapsed_ms = (_t.time() - _t0) * 1000
check("  50 operations cost far less than one open", _elapsed_ms < 500,
      "%.0f ms for 50 - a per-operation open of this store would be 60 000 ms"
      % _elapsed_ms)
cache_mb = abs(cache_pages) / 1024.0 if cache_pages < 0 else 0
check("the per-connection page cache is bounded", 0 < cache_mb <= 32,
      "%.0f MB per connection, now multiplied by THREADS rather than by "
      "operations (was 128 MB)" % cache_mb)

src = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
              encoding="utf-8", errors="replace").read()
check("both connect paths reuse the thread's connection",
      src.count("_thread_connection(") >= 3,
      "the read-only path pays the same open cost as the write path")

print()
print("[the app records its own vitals]")
tmp = tempfile.mkdtemp(prefix="tn-metrics-")
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


def call(m, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:120]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")

st, b = call("GET", "/api/diagnostics/processes", tok)
metrics = (b or {}).get("metrics") or {}
check("/api/diagnostics/processes answers", st == 200 and bool(metrics),
      (b or {}).get("error") or "")
WANT = ("app_service_mem_mb", "app_service_cpu_pct", "app_service_threads",
        "app_service_handles", "app_ui_mem_mb", "app_total_mem_mb")
check("  it reports the vitals that matter",
      all(k in metrics for k in WANT),
      ", ".join("%s=%s" % (k, metrics.get(k)) for k in
                ("app_service_mem_mb", "app_service_threads", "app_service_handles")))
check("  and the memory figure is plausible",
      50.0 < float(metrics.get("app_service_mem_mb") or 0) < 8000.0,
      "%s MB" % metrics.get("app_service_mem_mb"))

# The stored series is the half that answers a 24-hour question.
from app.services.app_metrics import sampler, GATEWAY_ID  # noqa: E402

rows = sampler.sample_once()
st, res = call("POST", "/api/app-store/append/historian", tok, {"rows": rows})
check("the vitals store as ordinary historian rows", st == 200,
      "so every existing chart, alarm and report works on them unchanged")
st, hist = call("GET", "/api/app-store/historian/range?limit=50&gateway=" + GATEWAY_ID, tok)
hrows = (hist or {}).get("rows") or []
tags = {str(r.get("tag")) for r in hrows}
check("  and come back as chartable tags", len(hrows) >= 6
      and "app_service_mem_mb" in tags,
      "%d row(s): %s" % (len(hrows), ", ".join(sorted(tags))[:90]))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
