"""TrustNode FULL-SYSTEM validation harness (default 12 h, read-only).

Validates, against the RUNNING app, every subsystem the release checklist
cares about — designed to be the standard soak/release gate when new
features land:

  collection   WS 'reading' stream: cadence vs gateway interval, per-event
               tag completeness, single-timestamp integrity
  charts       chart-feed latency (WS arrival - sample ts) — what a chart sees
  historian    local commit freshness + gap census (the durable store)
  databases    store-forward outbox depth, sync-state tables, CLOUD Postgres
               lag (direct read of plc_readings MAX(ts_utc) via config creds)
  recovery     every boot / stall / watchdog-restart / wedge event in the log,
               with time-to-recover derived from the data stream itself
  batches      batch tables activity (counts + newest event ts, read-only)
  reports      generated/scheduled reports activity (read-only)
  ai module    GET /api/intelligence/status probe
  alarms/trig  config presence (alarms_setup / triggers_limits docs) + the
               reading stream that drives evaluation (validated via WS)
  latency      API probe RTTs (/api/health, gateways/status, intelligence)
  resources    backend CPU / RSS / threads / connections (leak detection)

Usage:
    python scripts/validate_full_12h.py                # 12 h
    VAL_DURATION_S=3600 python scripts/validate_full_12h.py   # custom

Outputs (next to the script, in ./validation_out/):
    events.jsonl          every observation, timestamped
    summary_partial.txt   rolling summary, rewritten every 30 min
    validation_report.txt final full report (also printed to stdout)

Read-only: SELECTs on local SQLite (mode=ro), GET probes, one SELECT per
5 min on the cloud DB. Credentials: local login (dev), cloud creds read
from the app's own config documents at runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone

# ----------------------------------------------------------------- config
DURATION_S = int(os.environ.get("VAL_DURATION_S", "43200"))
GW = os.environ.get("VAL_GATEWAY", "gw-1781903248499")
INTERVAL_S = float(os.environ.get("VAL_INTERVAL_S", "1.0"))
API = "http://127.0.0.1:8000"
LOGIN = {"username": os.environ.get("VAL_USER", "admin-mari"),
         "password": os.environ.get("VAL_PASS", "Limerick2019*")}
DB = os.path.expanduser("~/.trustnode_edge/data/trustnode_app_store.db")
SF_DB = os.path.expanduser("~/.trustnode_edge/data/trustnode_store_forward.db")
LOG = os.path.expanduser(r"~\AppData\Roaming\trustnode-edge-desktop\backend.log")
# Operator 2026-08-21 (BOOT-HEALTH FIX): every gate run also asserts the
# REAL install's last boot — spawn -> first /api/health 200 within
# VAL_BOOT_MAX_HEALTH_MS (15 s) and no splash "did not respond" — so the
# boot regression that hit users on every launch can never ship again.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot_log_check as _boot  # noqa: E402
BOOT_MAX_HEALTH_MS = int(os.environ.get("VAL_BOOT_MAX_HEALTH_MS", "15000"))
boot_metrics: dict = {}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validation_out")
os.makedirs(OUT_DIR, exist_ok=True)
EV_PATH = os.path.join(OUT_DIR, "events.jsonl")
PARTIAL = os.path.join(OUT_DIR, "summary_partial.txt")
REPORT = os.path.join(OUT_DIR, "validation_report.txt")

ev_f = open(EV_PATH, "a", encoding="utf-8")


def emit(rec: dict) -> None:
    rec["at"] = time.time()
    ev_f.write(json.dumps(rec, default=str) + "\n")
    ev_f.flush()


def parse_ts(ts) -> datetime | None:
    s = str(ts)[:26].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26 if "." in s else 19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=4.0)


# ------------------------------------------------------------------- auth
_token = {"v": "", "t": 0.0}


def get_token(force: bool = False) -> str:
    import urllib.request
    if _token["v"] and not force and time.time() - _token["t"] < 6 * 3600:
        return _token["v"]
    try:
        req = urllib.request.Request(f"{API}/api/auth/login",
                                     data=json.dumps(LOGIN).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        _token["v"] = d.get("token") or d.get("access_token") or ""
        _token["t"] = time.time()
    except Exception as exc:
        emit({"t": "auth_err", "err": str(exc)[:120]})
    return _token["v"]


def api_get(path: str, timeout: float = 8.0, auth: bool = True):
    """Returns (http_status_or_0, rtt_seconds)."""
    import urllib.request
    hdrs = {}
    if auth:
        tok = get_token()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(f"{API}{path}", headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2048)
            return r.status, time.perf_counter() - t0
    except Exception:
        return 0, time.perf_counter() - t0


# ---------------------------------------------------------------- streams
ws_events: list = []      # (arrival, sample_ts, n_readings, n_stamps)
db_lags: list = []        # (epoch, lag_s)
api_probes: list = []     # (epoch, path, status, rtt)
cloud_lags: list = []     # (epoch, lag_s | None(err))
res_samples: list = []    # (epoch, cpu, rss_mb, threads, conns)
outbox_depths: list = []  # (epoch, pending)
module_checks: list = []  # (epoch, dict)
log_events: list = []     # (epoch, category, line)



async def sleep_bounded(seconds: float, stop_at: float) -> None:
    """Sleep up to `seconds` but never past stop_at (keeps run duration exact)."""
    remain = stop_at - time.time()
    if remain <= 0:
        return
    await asyncio.sleep(min(seconds, remain))


async def ws_task(stop_at: float) -> None:
    import websockets
    while time.time() < stop_at:
        tok = get_token()
        url = f"ws://127.0.0.1:8000/ws/stream?token={tok}"
        try:
            async with websockets.connect(url, ping_interval=20, max_size=10_000_000) as ws:
                emit({"t": "ws_conn"})
                while time.time() < stop_at:
                    try:
                        # never wait past stop_at (2026-08-21)
                        msg = await asyncio.wait_for(ws.recv(), timeout=max(0.5, min(15.0, stop_at - time.time())))
                    except asyncio.TimeoutError:
                        emit({"t": "ws_idle15"})
                        continue
                    arr = time.time()
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    if not isinstance(d, dict) or not isinstance(d.get("readings"), list) or not d["readings"]:
                        continue
                    if str(d.get("gateway_id") or "") not in ("", GW):
                        continue
                    stamps, newest = set(), None
                    for r in d["readings"]:
                        ts = parse_ts(r.get("ts_utc"))
                        if ts:
                            stamps.add(str(r.get("ts_utc")))
                            if newest is None or ts > newest:
                                newest = ts
                    if newest is None:
                        continue
                    ws_events.append((arr, newest.timestamp(), len(d["readings"]), len(stamps)))
                    emit({"t": "ws", "lat": arr - newest.timestamp(),
                          "n": len(d["readings"]), "stamps": len(stamps)})
        except Exception as exc:
            emit({"t": "ws_err", "err": f"{type(exc).__name__}: {str(exc)[:90]}"})
            get_token(force=True)
            await asyncio.sleep(3.0)


_TENANT = {"id": None}


def _historian_newest_ts():
    """Newest historian stamp for GW via the covering index
    (tenant_id, gateway_id, ts_utc). The old MAX(ts_utc) WHERE gateway_id=?
    query skip-scanned that index (~2.4 s on an 8 GB store) — and it ran on the
    event loop, which is what produced the phantom WS holes. 2026-08-21."""
    con = ro(DB)
    try:
        if not _TENANT["id"]:
            row = con.execute(
                "SELECT tenant_id FROM historian_readings WHERE gateway_id=? ORDER BY rowid DESC LIMIT 1",
                (GW,)).fetchone()
            _TENANT["id"] = row[0] if row and row[0] else "default"
        row = con.execute(
            "SELECT ts_utc FROM historian_readings WHERE tenant_id=? AND gateway_id=? "
            "ORDER BY ts_utc DESC LIMIT 1", (_TENANT["id"], GW)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


async def db_task(stop_at: float) -> None:
    while time.time() < stop_at:
        try:
            newest = await asyncio.to_thread(_historian_newest_ts)
            ts = parse_ts(newest) if newest else None
            if ts:
                lag = time.time() - ts.timestamp()
                db_lags.append((time.time(), lag))
                emit({"t": "db", "lag": round(lag, 2)})
        except Exception as exc:
            emit({"t": "db_err", "err": str(exc)[:90]})
        await asyncio.sleep(2.0)


async def api_task(stop_at: float) -> None:
    paths = [("/api/health", False), ("/api/plc/gateways/status", True),
             ("/api/intelligence/status", True)]
    while time.time() < stop_at:
        for path, auth in paths:
            status, rtt = await asyncio.to_thread(api_get, path, 8.0, auth)
            api_probes.append((time.time(), path, status, rtt))
            emit({"t": "api", "p": path, "s": status, "rtt": round(rtt, 3)})
        await sleep_bounded(60.0, stop_at)


def _cloud_target() -> dict | None:
    try:
        con = ro(DB)
        best = None
        for (p,) in con.execute(
                "SELECT payload_json FROM config_documents_scoped WHERE domain='database_configurations'"):
            for c in json.loads(p):
                if str(c.get("engine") or "") == "postgresql" and c.get("host"):
                    best = c
        con.close()
        return best
    except Exception:
        return None


def _cloud_lag_probe():
    """Blocking cloud PG probe — always called via asyncio.to_thread (2026-08-21)."""
    lag = None
    import psycopg2
    tgt = _cloud_target()
    if tgt:
        conn = psycopg2.connect(
            host=tgt["host"], port=int(tgt.get("port") or 5432),
            dbname=tgt.get("database") or "postgres",
            user=tgt.get("username") or "", password=tgt.get("password") or "",
            connect_timeout=8,
            options="-c statement_timeout=8000",
            sslmode="require" if tgt.get("tls", True) else "disable",
        )
        cur = conn.cursor()
        schema = str(tgt.get("schema") or "public")
        table = str(tgt.get("table") or "plc_readings")
        cur.execute(
            f'SELECT MAX(ts_utc) FROM "{schema}"."{table}" WHERE gateway_id = %s', (GW,))
        row = cur.fetchone()
        conn.close()
        ts = parse_ts(row[0]) if row and row[0] else None
        if ts:
            lag = time.time() - ts.timestamp()
    return lag


async def cloud_task(stop_at: float) -> None:
    while time.time() < stop_at:
        lag = None
        try:
            lag = await asyncio.to_thread(_cloud_lag_probe)
        except Exception as exc:
            emit({"t": "cloud_err", "err": f"{type(exc).__name__}: {str(exc)[:90]}"})
        cloud_lags.append((time.time(), lag))
        if lag is not None:
            emit({"t": "cloud", "lag": round(lag, 1)})
        await sleep_bounded(300.0, stop_at)


def _resource_sample():
    """Blocking psutil sample (cpu_percent sleeps 0.5 s) — via to_thread (2026-08-21).
    Also recognises a source-run backend (python listening on :8000) so the
    sample works against `python -m app` during diagnosis."""
    import psutil
    proc = None
    for p in psutil.process_iter(["name"]):
        if p.info["name"] and p.info["name"].lower().startswith("trustnode-service"):
            proc = p
            break
    if proc is None:
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == 8000 and c.pid:
                    proc = psutil.Process(c.pid)
                    break
        except Exception:
            proc = None
    if not proc:
        return None
    with proc.oneshot():
        cpu = proc.cpu_percent(interval=0.5)
        rss = proc.memory_info().rss / 1024 / 1024
        thr = proc.num_threads()
    try:
        conns = len(proc.net_connections())
    except Exception:
        conns = -1
    return (cpu, rss, thr, conns)


async def resources_task(stop_at: float) -> None:
    while time.time() < stop_at:
        try:
            s = await asyncio.to_thread(_resource_sample)
            if s:
                cpu, rss, thr, conns = s
                res_samples.append((time.time(), cpu, rss, thr, conns))
                emit({"t": "res", "cpu": cpu, "rss_mb": round(rss, 1),
                      "threads": thr, "conns": conns})
            else:
                emit({"t": "res_err", "err": "backend process not found"})
        except Exception as exc:
            emit({"t": "res_err", "err": str(exc)[:90]})
        await sleep_bounded(60.0, stop_at)


def _outbox_pending():
    con = ro(SF_DB)
    try:
        return con.execute("SELECT COUNT(*) FROM outbox_readings WHERE sent_remote=0").fetchone()[0]
    finally:
        con.close()


retention_samples: list = []


async def retention_task(stop_at: float) -> None:
    """Retention health (operator 2026-08-21).

    Guards what the tiered engine must hold on a RUNNING build: every configured
    level keeps being materialised, raw does not outgrow its window, and no level
    reports an error. With no policy active it records that fact — an edge
    without retention is a valid (if growing) configuration, not a failure.
    """
    while time.time() < stop_at:
        try:
            st, _rtt = api_get("/api/app-store/retention/v2/status")
            if isinstance(st, dict) and st.get("status"):
                s = st["status"]
                retention_samples.append({
                    "t": time.time(),
                    "policy": (s.get("policy") or {}).get("name"),
                    "levels": [{"key": l.get("key"), "rows": l.get("rows"),
                                "lag_s": l.get("lag_s"), "keep": l.get("keep"),
                                "err": l.get("last_error")}
                               for l in (s.get("levels") or [])],
                    "db_bytes": (s.get("database") or {}).get("size_bytes"),
                    "raw_rows": (s.get("database") or {}).get("raw_rows"),
                    "oldest_raw": (s.get("database") or {}).get("oldest_raw_utc"),
                    "cleanup": s.get("cleanup") or {},
                    "engine_running": (s.get("engine") or {}).get("running"),
                })
                emit({"kind": "retention", **retention_samples[-1]})
        except Exception:
            pass
        await sleep_bounded(120, stop_at)

async def outbox_task(stop_at: float) -> None:
    while time.time() < stop_at:
        try:
            if os.path.exists(SF_DB):
                n = await asyncio.to_thread(_outbox_pending)
                outbox_depths.append((time.time(), int(n)))
                emit({"t": "outbox", "pending": int(n)})
        except Exception as exc:
            emit({"t": "outbox_err", "err": str(exc)[:90]})
        await sleep_bounded(60.0, stop_at)


def _modules_snapshot() -> dict:
    """Blocking module counters — via to_thread (2026-08-21)."""
    snap: dict = {}
    con = ro(DB)
    try:
        for label, q in (
            ("batches_total", "SELECT COUNT(*) FROM batches"),
            ("batches_open", "SELECT COUNT(*) FROM batches WHERE status IN ('open','running','active')"),
            ("batch_events_newest", "SELECT MAX(created_utc) FROM batch_events"),
            ("reports_generated", "SELECT COUNT(*) FROM generated_reports"),
            ("reports_newest", "SELECT MAX(created_utc) FROM generated_reports"),
            ("reports_scheduled", "SELECT COUNT(*) FROM scheduled_reports"),
        ):
            try:
                snap[label] = con.execute(q).fetchone()[0]
            except Exception:
                snap[label] = "n/a"
        for dom in ("alarms_setup", "triggers_limits"):
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM config_documents_scoped WHERE domain=?",
                    (dom,)).fetchone()
                snap[dom] = int(row[0]) if row else 0
            except Exception:
                snap[dom] = "n/a"
    finally:
        con.close()
    return snap


async def modules_task(stop_at: float) -> None:
    while time.time() < stop_at:
        snap: dict = {}
        try:
            snap = await asyncio.to_thread(_modules_snapshot)
        except Exception as exc:
            snap["err"] = str(exc)[:90]
        module_checks.append((time.time(), snap))
        emit({"t": "modules", **{k: v for k, v in snap.items()}})
        await sleep_bounded(600.0, stop_at)


async def log_task(stop_at: float) -> None:
    try:
        f = open(LOG, "rb")
        f.seek(0, 2)
    except Exception:
        return
    pats = [
        ("boot_fail", re.compile(rb"did not respond|health wait TIMED OUT|BOOT ABORTED|\[boot\]\[health-watchdog\]")),
        ("recovery", re.compile(rb"Splash window created|Backend exited|respawn|first-row|auto-resumed")),
        ("stall", re.compile(rb"stalled \d+s|read-timeout|persist-timeout|event loop stale|cooldown")),
        ("engine", re.compile(rb"engine_v2|v2-dist|v2-writer|v2-reader")),
        ("boot_guard", re.compile(rb"duplicate startup_event|startup_event fired")),
        ("error", re.compile(rb"ERROR")),
        ("warning", re.compile(rb"WARNING")),
    ]
    while time.time() < stop_at:
        line = f.readline()
        if not line:
            # follow rotation: if the file shrank, reopen
            try:
                if os.path.getsize(LOG) < f.tell():
                    f.close()
                    f = open(LOG, "rb")
            except Exception:
                pass
            await asyncio.sleep(0.5)
            continue
        for cat, pat in pats:
            if pat.search(line):
                txt = line.decode("utf-8", "replace").strip()[:300]
                log_events.append((time.time(), cat, txt))
                emit({"t": "log", "cat": cat, "line": txt})
                break


# ---------------------------------------------------------------- summary
def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def build_summary(t0: float, final: bool) -> str:
    now = time.time()
    # 2026-08-21: the denominator is the MEASUREMENT WINDOW, not the report
    # time. The final report is built after every task returned, and the WS
    # reader's last recv() waits up to 15 s past stop_at, which inflated
    # expected by ~6% (600 events in a 600 s window showed as 94%).
    dur = min(now - t0, float(DURATION_S))
    exp = int(dur / INTERVAL_S)
    L: list[str] = []
    A = L.append
    A("=" * 76)
    A(f"TrustNode FULL VALIDATION {'FINAL REPORT' if final else 'partial'}  "
      f"window={dur/3600:.2f}h  gateway={GW}  interval={INTERVAL_S:.1f}s")
    A(f"generated {datetime.now(timezone.utc).isoformat()[:19]}Z")
    A("=" * 76)

    # collection + charts
    n = len(ws_events)
    A(f"\n[COLLECTION + CHARTS]  ws events={n} expected~{exp} delivery={n/max(1,exp)*100:.1f}%")
    if n >= 3:
        arrs = [e[0] for e in ws_events]
        gaps = [b - a for a, b in zip(arrs, arrs[1:])]
        lats = [e[0] - e[1] for e in ws_events]
        holes = [(a, g) for a, g in zip(arrs, gaps) if g > 3]
        A(f"  cadence: p50={pct(gaps,.5):.3f}s p95={pct(gaps,.95):.3f}s max={max(gaps):.1f}s  holes>3s={len(holes)}")
        A(f"  chart-feed latency: p50={pct(lats,.5)*1000:.0f}ms p95={pct(lats,.95)*1000:.0f}ms max={max(lats):.2f}s")
        A(f"  tags/event: min={min(e[2] for e in ws_events)} "
          f"avg={statistics.mean(e[2] for e in ws_events):.1f}; "
          f"multi-stamp events={sum(1 for e in ws_events if e[3]>1)}")
        for a, g in holes[:12]:
            A(f"    HOLE {g:.0f}s ending {datetime.fromtimestamp(a, tz=timezone.utc).strftime('%H:%M:%S')}Z")

    # historian
    if db_lags:
        lags = [l for _, l in db_lags]
        stale = sum(1 for l in lags if l > 10)
        A(f"\n[HISTORIAN] freshness p50={pct(lags,.5):.2f}s p95={pct(lags,.95):.2f}s max={max(lags):.0f}s  "
          f"samples>10s-stale={stale}/{len(lags)} ({stale/len(lags)*100:.1f}%)")

    # DB truth
    try:
        con = ro(DB)
        cut = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = list(con.execute(
            "SELECT COUNT(DISTINCT ts_utc), COUNT(*) FROM historian_readings "
            "WHERE gateway_id=? AND ts_utc>=?", (GW, cut)))
        con.close()
        stamps, total = rows[0]
        A(f"[DB TRUTH] stamps committed={stamps} expected~{exp} ({stamps/max(1,exp)*100:.1f}%)  rows={total}")
    except Exception as exc:
        A(f"[DB TRUTH] query failed: {exc}")

    # databases: outbox + cloud
    if outbox_depths:
        ds = [d for _, d in outbox_depths]
        A(f"\n[DATABASES] outbox pending: start={ds[0]} end={ds[-1]} max={max(ds)} "
          f"(rising forever = cloud sync broken)")
    good_cloud = [l for _, l in cloud_lags if l is not None]
    A(f"  cloud PG lag: checks={len(cloud_lags)} ok={len(good_cloud)} " +
      (f"p50={pct(good_cloud,.5):.0f}s max={max(good_cloud):.0f}s" if good_cloud else "(no successful checks)"))

    # api latency + AI module
    if api_probes:
        A("\n[API LATENCY / MODULES]")
        for path in sorted({p for _, p, _, _ in api_probes}):
            rows = [(s, r) for _, p, s, r in api_probes if p == path]
            ok = [r for s, r in rows if s == 200]
            fail = sum(1 for s, _ in rows if s != 200)
            A(f"  {path:<32} ok={len(ok)}/{len(rows)} fail={fail} " +
              (f"rtt p50={pct(ok,.5)*1000:.0f}ms p95={pct(ok,.95)*1000:.0f}ms" if ok else ""))

    # modules snapshot
    if module_checks:
        first, last = module_checks[0][1], module_checks[-1][1]
        A("\n[BATCHES / REPORTS / ALARMS+TRIGGERS]  (first -> last snapshot)")
        for k in ("batches_total", "batches_open", "batch_events_newest",
                  "reports_generated", "reports_newest", "reports_scheduled",
                  "alarms_setup", "triggers_limits"):
            A(f"  {k:<22} {first.get(k)} -> {last.get(k)}")

    # resources
    if res_samples:
        cpus = [c for _, c, _, _, _ in res_samples]
        rsss = [r for _, _, r, _, _ in res_samples]
        thrs = [t for _, _, _, t, _ in res_samples]
        A(f"\n[RESOURCES] cpu p50={pct(cpus,.5):.0f}% max={max(cpus):.0f}%  "
          f"rss start={rsss[0]:.0f}MB end={rsss[-1]:.0f}MB max={max(rsss):.0f}MB  "
          f"threads start={thrs[0]} end={thrs[-1]} max={max(thrs)}")
        if rsss[-1] > rsss[0] * 1.5 and rsss[-1] - rsss[0] > 200:
            A("  !! RSS grew >50% and >200MB — investigate for a leak")
        if max(thrs) > 300:
            A("  !! thread count exceeded 300 — pool exhaustion signature")

    # recovery + log census
    cats: dict = {}
    for _, cat, _ in log_events:
        cats[cat] = cats.get(cat, 0) + 1
    A(f"\n[RECOVERY / LOG] events by category: {cats or 'none'}")
    for at, cat, txt in [e for e in log_events if e[1] in ("recovery", "stall")][:25]:
        A(f"  {datetime.fromtimestamp(at, tz=timezone.utc).strftime('%H:%M:%S')}Z [{cat}] {txt[:150]}")

    # boot health (2026-08-21): the last boot block of backend.log vs SLOs
    A("\n[BOOT HEALTH] (last boot block of backend.log)")
    for ln in _boot.summary_lines(boot_metrics):
        A(ln)

    # ---- retention --------------------------------------------------------
    ret_ok = True
    ret_note = ""
    if retention_samples:
        first, last = retention_samples[0], retention_samples[-1]
        A("")
        A("[RETENTION]")
        if not last.get("policy"):
            A("  no active policy - history is kept in full (nothing is deleted)")
            ret_note = "no policy"
        else:
            A(f"  policy: {last['policy']}")
        for lv in last.get("levels") or []:
            grew = ""
            for f in first.get("levels") or []:
                if f.get("key") == lv.get("key") and f.get("rows") is not None:
                    grew = f"  (+{int(lv.get('rows') or 0) - int(f.get('rows') or 0):,} this run)"
                    break
            lag = lv.get("lag_s")
            lag_txt = ("%.0fs" % lag) if lag is not None else "-"
            A(f"  {str(lv.get('key')):>7}: rows={int(lv.get('rows') or 0):>10,}"
              f" keep={str(lv.get('keep') or '-'):>6} lag={lag_txt:>8}{grew}")
            if lv.get("err"):
                A(f"           ERROR: {lv['err']}")
                ret_ok = False
        cl = last.get("cleanup") or {}
        if cl.get("pending_rows"):
            held = (" (" + str(cl.get("held_by")) + ")") if cl.get("held_by") else ""
            A(f"  cleanup in progress: {int(cl['pending_rows']):,} readings still to remove{held}")
        db0, db1 = first.get("db_bytes") or 0, last.get("db_bytes") or 0
        if db0 and db1:
            A(f"  database: {db0/1e9:.2f} GB -> {db1/1e9:.2f} GB")
        if last.get("engine_running") is False:
            A("  engine NOT running")
            ret_ok = False
        for lv in last.get("levels") or []:
            if lv.get("key") == "raw":
                continue
            if last.get("policy") and (lv.get("rows") or 0) == 0 and lv.get("lag_s") is None:
                A(f"  level {lv.get('key')} has never been built")
                ret_ok = False

    # verdict
    A("\n[VERDICT vs SLOs]")
    ok = True
    b_ok, b_lines = _boot.verdict(boot_metrics, BOOT_MAX_HEALTH_MS)
    for ln in b_lines:
        A(ln)
    ok &= b_ok
    boot_fail_n = cats.get("boot_fail", 0)
    A(f"  zero boot failures in run : {'PASS' if boot_fail_n == 0 else f'FAIL ({boot_fail_n})'}")
    ok &= boot_fail_n == 0
    if n and exp:
        d = n / exp * 100
        A(f"  delivery >=95%           : {'PASS' if d >= 95 else 'FAIL'} ({d:.1f}%)")
        ok &= d >= 95
    if db_lags:
        f95 = pct([l for _, l in db_lags], .95)
        A(f"  historian p95 <5s        : {'PASS' if f95 < 5 else 'FAIL'} ({f95:.1f}s)")
        ok &= f95 < 5
    if retention_samples:
        A(f"  retention healthy        : {'PASS' if ret_ok else 'FAIL'}"
          f"{(' (' + ret_note + ')') if ret_note else ''}")
        ok &= ret_ok
    stall_n = cats.get("stall", 0)
    A(f"  zero stall/wedge events  : {'PASS' if stall_n == 0 else f'FAIL ({stall_n})'}")
    ok &= stall_n == 0
    if good_cloud:
        c95 = pct(good_cloud, .95)
        A(f"  cloud lag p95 <120s      : {'PASS' if c95 < 120 else f'FAIL ({c95:.0f}s)'}")
    A(f"\n  OVERALL: {'PASS' if ok else 'ATTENTION NEEDED'}")
    return "\n".join(L)


async def partial_task(t0: float, stop_at: float) -> None:
    while time.time() < stop_at:
        await sleep_bounded(1800.0, stop_at)
        if time.time() >= stop_at:
            break
        try:
            with open(PARTIAL, "w", encoding="utf-8") as f:
                f.write(build_summary(t0, final=False))
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}Z] partial summary refreshed "
                  f"(ws={len(ws_events)} stalls={sum(1 for e in log_events if e[1]=='stall')})",
                  flush=True)
        except Exception:
            pass


async def main() -> int:
    t0 = time.time()
    stop_at = t0 + DURATION_S
    emit({"t": "start", "duration": DURATION_S, "gw": GW})
    boot_metrics.update(_boot.analyze_last_boot(LOG))
    emit({"t": "boot", **{k: v for k, v in boot_metrics.items() if k != "integrity"}})
    _b_ok, _b_lines = _boot.verdict(boot_metrics, BOOT_MAX_HEALTH_MS)
    print("[BOOT HEALTH] " + ("PASS" if _b_ok else "FAIL") + "\n" + "\n".join(_b_lines), flush=True)
    print(f"validation started {datetime.now(timezone.utc).isoformat()[:19]}Z "
          f"for {DURATION_S/3600:.1f}h — gateway {GW}", flush=True)
    get_token()
    await asyncio.gather(
        ws_task(stop_at), db_task(stop_at), api_task(stop_at),
        cloud_task(stop_at), resources_task(stop_at), outbox_task(stop_at),
        modules_task(stop_at), log_task(stop_at), retention_task(stop_at),
        partial_task(t0, stop_at),
    )
    report = build_summary(t0, final=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report)
    print(report, flush=True)
    # Gate exit code for release automation: 0 = PASS, 2 = attention needed.
    return 0 if "OVERALL: PASS" in report else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
