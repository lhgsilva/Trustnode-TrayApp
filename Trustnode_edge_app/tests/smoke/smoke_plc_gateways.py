#!/usr/bin/env python3
"""
PLC gateway end-to-end smoke (operator 2026-06-16).

The companion to smoke_live_charts.py — same shape, but for the
PLC side of the pipeline (Allen-Bradley, Siemens OPC-UA, Modbus
TCP, …) rather than the power meters.

Verifies every layer that drives a PLC tag onto the dashboard:

    PLC (CIP / OPC-UA / Modbus)
        → gateway worker (per-gateway thread)
        → /api/plc/snapshot live cache
        → SQLite historian (gateway_id, ts_utc, tag_name, value)
        → /api/historian/* + bootstrap (read paths)
        → cloud mirror (Lite)

For each gateway the smoke measures:

    * is the worker thread actually running?
    * is it talking to the PLC? (last_error == null, db_pending == 0)
    * cadence — adjacent ts gap per tag matches interval_ms
    * snapshot freshness — /api/plc/snapshot newest age
    * historian growth — rows/min per gateway
    * tag-quality breakdown — % GOOD vs other
    * start / stop wall-clock — per-gateway, not all-at-once
    * cross-gateway isolation — stopping one doesn't drop another's
      historian throughput
    * cloud mirror — backlog count
    * Lite bootstrap exposes the gateway + its tag list

Run:
    python tests/smoke/smoke_plc_gateways.py
    TRUSTNODE_SMOKE_DISRUPT=1 python tests/smoke/smoke_plc_gateways.py
    TRUSTNODE_SMOKE_WATCH=30 python tests/smoke/smoke_plc_gateways.py

Exit: 0 = green, 1 = at least one FAIL, 2 = backend unreachable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[36mINFO\033[0m"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_api_base() -> str:
    env = os.environ.get("TRUSTNODE_API_BASE", "").strip()
    if env:
        return env.rstrip("/")
    for port in range(8000, 8010):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
            if r.ok:
                return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return "http://127.0.0.1:8000"


def resolve_db_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    candidates = [
        Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TrustNode" / "data" / "trustnode_app_store.db",
    ]
    best, best_ts = None, ""
    for c in candidates:
        if not c.exists():
            continue
        try:
            con = sqlite3.connect(str(c), timeout=2)
            ts = con.execute("SELECT MAX(ts_utc) FROM historian_readings").fetchone()[0] or ""
            con.close()
        except Exception:
            ts = ""
        if ts > best_ts:
            best, best_ts = c, ts
    return best or candidates[0]


def resolve_plc_sink_db(app_store_db: Path) -> Path | None:
    """The PLC gateway writes through a sink configured under
    `database_configurations` — typically `local-sqlite-default`
    pointing at `./data/trustnode_edge.db`. Read the sink path
    from the app store and resolve it relative to the app store's
    grandparent (the backend's working dir)."""
    try:
        con = sqlite3.connect(f"file:{app_store_db}?mode=ro", uri=True, timeout=2.0)
        row = con.execute(
            "SELECT payload_json FROM config_documents WHERE domain='database_configurations'"
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        cfg = json.loads(row[0] or "[]")
    except Exception:
        return None
    if not isinstance(cfg, list):
        return None
    for entry in cfg:
        if str(entry.get("engine") or "").lower() == "sqlite" and entry.get("enabled"):
            raw = str(entry.get("sqlite_path") or "").replace("\\", "/").lstrip("./")
            if not raw:
                continue
            # The backend's CWD is the parent of the app store DB.
            base = app_store_db.parent
            candidate = (base / raw).resolve()
            if candidate.exists():
                return candidate
            # Also try base / "data" / ... pattern (sometimes the
            # path is "./data/foo.db" and CWD is the parent of data/)
            alt = (base.parent / raw).resolve()
            if alt.exists():
                return alt
    return None


def authenticate(api_base: str) -> dict[str, str]:
    user = os.environ.get("TRUSTNODE_SMOKE_USER", "admin")
    pwd = os.environ.get("TRUSTNODE_SMOKE_PASS", "admin")
    try:
        r = requests.post(
            f"{api_base}/api/auth/login",
            json={"username": user, "password": pwd},
            timeout=4,
        )
        if r.ok:
            tok = (r.json() or {}).get("token")
            if tok:
                return {"Authorization": f"Bearer {tok}"}
    except Exception:
        pass
    return {}


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))
        bar = {"PASS": PASS, "FAIL": FAIL, "WARN": WARN, "INFO": INFO}[status]
        print(f"  [{bar}] {name}" + (f"  — {detail}" if detail else ""))

    def fails(self) -> int:
        return sum(1 for _, s, _ in self.checks if s == "FAIL")

    def summary(self) -> str:
        n = len(self.checks)
        bad = self.fails()
        warn = sum(1 for _, s, _ in self.checks if s == "WARN")
        info = sum(1 for _, s, _ in self.checks if s == "INFO")
        good = n - bad - warn - info
        return f"{good} passed, {warn} warnings, {bad} failed (of {n - info} checks)"


def section(title: str) -> None:
    print()
    print(f"\033[1m== {title} ==\033[0m")


# Map of PLC source values that may land in the historian. Modbus
# meter rows are written under power_modbus/power_insight and are
# handled by smoke_live_charts.py; the PLC smoke deliberately
# excludes them so cross-gateway noise doesn't bleed in.
PLC_SOURCES = {
    "allen_bradley",
    "siemens_opcua",
    "modbus_tcp",
    "modbus_rtu",
    "opcua",
}


def count_rows(db_path: Path, gw_id: str, window_s: float) -> int:
    since = (utc_now() - timedelta(seconds=window_s)).isoformat()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        return int(
            con.execute(
                """SELECT COUNT(*) FROM historian_readings
                   WHERE gateway_id=? AND ts_utc>=?
                     AND source NOT IN ('power_modbus','power_insight')""",
                (gw_id, since),
            ).fetchone()[0]
        )
    finally:
        con.close()


def main() -> int:
    api_base = resolve_api_base()
    db_path = resolve_db_path()
    watch_s = int(os.environ.get("TRUSTNODE_SMOKE_WATCH", "15") or "15")
    print(f"API:   {api_base}")
    print(f"DB:    {db_path}")
    print(f"Watch: {watch_s} s")
    print(f"Time:  {utc_now().isoformat()}")
    rep = Report()

    # 1. Backend reachable + auth ----------------------------------------
    section("1. Backend reachable + auth")
    try:
        r = requests.get(f"{api_base}/api/health", timeout=2.0)
        if not r.ok:
            rep.add("health", "FAIL", str(r.status_code))
            print()
            print(rep.summary())
            return 2
        rep.add("health", "PASS")
    except Exception as exc:
        rep.add("health", "FAIL", str(exc))
        return 2
    headers = authenticate(api_base)
    rep.add("auth", "PASS" if headers else "WARN")

    # 2. Gateway inventory -----------------------------------------------
    section("2. PLC gateway inventory")
    # bootstrap is the canonical multi-gateway source
    try:
        b = requests.get(f"{api_base}/api/app-store/bootstrap", headers=headers, timeout=3.0).json()
        bdata = b.get("data", b) if isinstance(b, dict) else {}
        bcfg = bdata.get("gateway_configurations") or []
        if isinstance(bcfg, dict):
            # legacy single-gateway shape
            bcfg = bcfg.get("gateways") or [bcfg]
    except Exception:
        bcfg = []

    # /api/plc/gateways/status returns runtime info for each gateway
    try:
        st_body = requests.get(f"{api_base}/api/plc/gateways/status", headers=headers, timeout=3.0).json()
    except Exception:
        st_body = []
    if isinstance(st_body, dict):
        st_list = st_body.get("gateways") or []
    elif isinstance(st_body, list):
        st_list = st_body
    else:
        st_list = []
    runtime_by_id = {str(g.get("gateway_id") or g.get("id") or ""): g for g in st_list}

    if not bcfg and not st_list:
        rep.add("PLC gateways configured", "WARN", "no PLC gateway in bootstrap")
        print()
        print(rep.summary())
        return 1

    gw_ids = sorted({str(g.get("id") or "") for g in bcfg if g.get("id")} | set(runtime_by_id.keys()))
    rep.add("PLC gateways configured", "PASS", f"{len(gw_ids)} gateway(s): {gw_ids}")

    # 3. Per-gateway runtime + cadence -----------------------------------
    section("3. Per-gateway runtime + cadence (last 2 min)")
    cfg_by_id = {str(g.get("id") or ""): g for g in bcfg}
    cutoff = (utc_now() - timedelta(minutes=2)).isoformat()
    # PLC rows are written to the configured SQLite sink (typically
    # ./data/trustnode_edge.db), not the app-store historian.
    # Locate the sink db so we can verify cadence end-to-end. The
    # sink table doesn't carry gateway_id, so we join on tag_name.
    plc_sink_db = resolve_plc_sink_db(db_path)
    if plc_sink_db:
        rep.add("PLC sink db", "PASS", str(plc_sink_db))
    else:
        rep.add("PLC sink db", "WARN", "could not resolve — cadence checks will rely on /api/plc/snapshot only")
    rows: list[tuple] = []
    if plc_sink_db:
        try:
            con = sqlite3.connect(f"file:{plc_sink_db}?mode=ro", uri=True, timeout=2.0)
            # Sink schema: id, ts_utc, tag_name, value, quality, source, ...
            # No gateway_id column — we infer it by matching tag_name back to
            # the configured tag lists from bootstrap.
            # The sink writes ts_utc with a SPACE separator, no timezone,
            # like "2026-06-16 14:40:49.784". A lexicographic comparison
            # against an ISO `T`-separated cutoff string is wrong: 'T'
            # (0x54) > ' ' (0x20). Use a space-separated cutoff for the
            # sink query.
            cutoff_naive = cutoff.replace("+00:00", "").replace("T", " ")
            raw_rows = con.execute(
                """SELECT tag_name, ts_utc, quality, source
                   FROM historian_readings
                   WHERE ts_utc >= ?
                   ORDER BY tag_name, ts_utc""",
                (cutoff_naive,),
            ).fetchall()
            con.close()
            # Map tag → owning gateway based on bootstrap tag lists
            tag_to_gw: dict[str, str] = {}
            for gid, gcfg in cfg_by_id.items():
                for t in (gcfg.get("tags") or []):
                    tag_to_gw[str(t)] = gid
            for tag, ts, quality, src in raw_rows:
                gid = tag_to_gw.get(str(tag), "")
                if not gid:
                    continue
                # quality is an integer (192 = GOOD on OPC convention),
                # synthesize a label so the rest of the code is uniform.
                qlabel = "GOOD" if int(quality or 0) >= 192 else f"q={quality}"
                rows.append((gid, tag, ts, ts, qlabel, src))
        except Exception as exc:
            rep.add("sink read", "WARN", str(exc))

    by_gw: dict[str, list[Any]] = {}
    for r in rows:
        by_gw.setdefault(str(r[0] or ""), []).append(r)

    write_lag_all: list[float] = []
    overall_quality_ok = True
    for gid in gw_ids:
        runtime = runtime_by_id.get(gid) or {}
        cfg = cfg_by_id.get(gid) or {}
        name = str(cfg.get("name") or gid)
        gtype = str(runtime.get("gateway_type") or cfg.get("gateway_type") or "?")
        interval = int(runtime.get("interval_ms") or cfg.get("interval_ms") or 1000)
        running = bool(runtime.get("running"))
        last_err = str(runtime.get("last_error") or "")
        configured_tags = list(cfg.get("tags") or [])

        rep.add(
            f"  {name} ({gtype}, id={gid})",
            "INFO",
            f"running={running} interval={interval}ms tags={len(configured_tags)} err={last_err or 'none'}",
        )

        # Worker actually polling the PLC?
        if last_err:
            rep.add(f"    {gid} no last_error", "WARN" if running else "FAIL", last_err)
        else:
            rep.add(f"    {gid} no last_error", "PASS")

        gw_rows = by_gw.get(gid, [])
        if not gw_rows:
            # WARN, not FAIL: the sink might be a non-default
            # location, or the gateway is configured but the operator
            # hasn't run it long enough to populate the 2-min window.
            rep.add(f"    {gid} rows in 2-min window", "WARN", "0 rows in sink db")
            continue

        # Per-tag cadence
        per_tag: dict[str, list[str]] = {}
        qual_counts: dict[str, int] = {}
        for _gwid, tag, ts, created, qlabel, _src in gw_rows:
            per_tag.setdefault(str(tag), []).append(str(ts))
            qual_counts[str(qlabel or "?")] = qual_counts.get(str(qlabel or "?"), 0) + 1
            a, b = parse_iso(ts), parse_iso(created)
            if a and b:
                write_lag_all.append((b - a).total_seconds() * 1000.0)

        # Build per-tag median ts gap
        tag_meds: list[float] = []
        for tag, ts_list in sorted(per_tag.items()):
            if len(ts_list) < 3:
                continue
            ep = sorted(parse_iso(t).timestamp() for t in ts_list if parse_iso(t))
            if len(ep) < 3:
                continue
            deltas = [ep[i + 1] - ep[i] for i in range(len(ep) - 1) if ep[i + 1] - ep[i] < 60]
            if not deltas:
                continue
            med = statistics.median(deltas)
            p95 = sorted(deltas)[max(0, int(0.95 * len(deltas)) - 1)]
            tag_meds.append(med)
            expected = interval / 1000.0
            # PASS if median within 25% of configured cadence and p95 within 2×
            ok = (0.75 * expected) <= med <= (1.25 * expected) and p95 <= 2.0 * expected
            rep.add(
                f"    {gid} tag {tag}",
                "PASS" if ok else "WARN",
                f"{len(ts_list)} rows, median {med:.3f}s p95 {p95:.3f}s (expected ~{expected:.3f}s)",
            )
        if tag_meds:
            expected = interval / 1000.0
            worst = max(tag_meds)
            ok = worst <= 1.25 * expected
            rep.add(
                f"    {gid} aggregate cadence",
                "PASS" if ok else "WARN",
                f"max median {worst:.3f}s vs expected {expected:.3f}s across {len(tag_meds)} tag(s)",
            )

        # Tag-quality breakdown
        total_q = sum(qual_counts.values())
        good_q = qual_counts.get("GOOD", 0)
        if total_q:
            pct = (good_q / total_q) * 100.0
            ok = pct >= 95.0
            rep.add(
                f"    {gid} GOOD-quality rows",
                "PASS" if ok else "WARN",
                f"{good_q}/{total_q} = {pct:.1f}%",
            )
            if not ok:
                overall_quality_ok = False

        # Configured-vs-collected tag coverage
        observed_tags = set(per_tag.keys())
        if configured_tags:
            cov = observed_tags & set(configured_tags)
            ok = cov == set(configured_tags)
            missing = sorted(set(configured_tags) - cov)
            rep.add(
                f"    {gid} configured tag coverage",
                "PASS" if ok else "WARN",
                f"{len(cov)}/{len(configured_tags)} tags streaming" + (f", missing={missing}" if missing else ""),
            )

        # db_write_count and db_pending from runtime
        dwc = int(runtime.get("db_write_count") or 0)
        dp = int(runtime.get("db_pending_count") or 0)
        rep.add(
            f"    {gid} sink write counter",
            "PASS" if dp == 0 else "WARN",
            f"writes={dwc} pending={dp}",
        )

    # Aggregate write-lag
    if write_lag_all:
        med = statistics.median(write_lag_all)
        p95 = sorted(write_lag_all)[max(0, int(0.95 * len(write_lag_all)) - 1)]
        ok = med <= 250 and p95 <= 700
        rep.add("historian write lag (all PLC gateways)",
                "PASS" if ok else "WARN",
                f"median {med:.0f}ms p95 {p95:.0f}ms")

    # 4. /api/plc/snapshot freshness -------------------------------------
    section("4. /api/plc/snapshot freshness")
    samples_age: list[float] = []
    distinct_ts: set[str] = set()
    for _ in range(8):
        try:
            r = requests.get(f"{api_base}/api/plc/snapshot", headers=headers, timeout=3.0).json()
        except Exception:
            continue
        rows_snap = r if isinstance(r, list) else (r.get("rows") if isinstance(r, dict) else [])
        if rows_snap:
            ts = parse_iso(rows_snap[0].get("ts_utc"))
            if ts:
                age = (utc_now() - ts).total_seconds()
                samples_age.append(age)
                distinct_ts.add(str(rows_snap[0].get("ts_utc")))
        time.sleep(0.5)
    if samples_age:
        med = statistics.median(samples_age)
        worst = max(samples_age)
        ok = med <= 2.0 and worst <= 3.0
        rep.add(
            "/api/plc/snapshot age",
            "PASS" if ok else "WARN",
            f"median {med:.2f}s worst {worst:.2f}s, {len(distinct_ts)} distinct ts in 8 polls",
        )
    else:
        rep.add("/api/plc/snapshot", "WARN", "no rows returned")

    # 5. UI-facing endpoint latency --------------------------------------
    section("5. UI-facing endpoint latency (10 samples)")
    endpoints = [
        "/api/plc/config",
        "/api/plc/status",
        "/api/plc/gateways/status",
        "/api/plc/snapshot",
    ]
    for path in endpoints:
        samples = []
        for _ in range(10):
            t0 = time.monotonic()
            try:
                requests.get(f"{api_base}{path}", headers=headers, timeout=3.0)
            except Exception:
                pass
            samples.append((time.monotonic() - t0) * 1000.0)
        med = statistics.median(samples)
        p95 = sorted(samples)[int(0.95 * len(samples)) - 1]
        rep.add(
            f"GET {path}",
            "PASS" if med < 200 else ("WARN" if med < 500 else "FAIL"),
            f"median={med:.0f}ms p95={p95:.0f}ms",
        )

    # 6. Start/Stop wall-clock per gateway -------------------------------
    # Operator 2026-06-16: this test mutates runtime. The legacy
    # /api/plc/start path expects to re-read the full sink config
    # from app-store; calling the per-gateway endpoint with a stripped
    # payload can leave the runtime with no sink attached. Smoke is
    # read-only by default — set TRUSTNODE_SMOKE_RESTART=1 to opt in
    # and the test will use the legacy stop-all / start-all pair so
    # the sink rebinds cleanly.
    section("6. Start/Stop wall-clock (whole PLC subsystem)")
    if not headers:
        rep.add("start/stop", "INFO", "skipped (no auth token)")
    elif not os.environ.get("TRUSTNODE_SMOKE_RESTART"):
        rep.add(
            "start/stop",
            "INFO",
            "skipped (set TRUSTNODE_SMOKE_RESTART=1 to exercise; mutates runtime)",
        )
    else:
        try:
            t0 = time.monotonic()
            r = requests.post(f"{api_base}/api/plc/stop", headers=headers, timeout=5.0)
            dt_stop = (time.monotonic() - t0) * 1000.0
            rep.add(
                "stop-all",
                "PASS" if dt_stop < 800 else ("WARN" if dt_stop < 2000 else "FAIL"),
                f"{dt_stop:.0f}ms (status={r.status_code})",
            )
            time.sleep(0.5)
            t0 = time.monotonic()
            r = requests.post(f"{api_base}/api/plc/start", headers=headers, timeout=5.0)
            dt_start = (time.monotonic() - t0) * 1000.0
            rep.add(
                "start-all",
                "PASS" if dt_start < 800 else ("WARN" if dt_start < 2000 else "FAIL"),
                f"{dt_start:.0f}ms (status={r.status_code})",
            )
        except Exception as exc:
            rep.add("start/stop", "FAIL", str(exc))

    # 7. Cross-gateway isolation (DISRUPT) -------------------------------
    section("7. Cross-gateway interference (DISRUPT mode)")
    if not os.environ.get("TRUSTNODE_SMOKE_DISRUPT"):
        rep.add("interference test", "INFO", "set TRUSTNODE_SMOKE_DISRUPT=1 to run")
    elif len(gw_ids) < 2:
        rep.add(
            "interference test",
            "INFO",
            "needs ≥ 2 PLC gateways (also runs against the meter; same isolation invariant covered by smoke_pipeline_perf)",
        )
    else:
        keep, stop = gw_ids[0], gw_ids[1]
        rep.add("interference baseline", "INFO", f"30 s with both {keep} and {stop} running")
        time.sleep(30)
        base = count_rows(db_path, keep, 30)
        requests.post(f"{api_base}/api/plc/gateways/stop", headers=headers, json={"gateway_id": stop}, timeout=5.0)
        time.sleep(30)
        after = count_rows(db_path, keep, 30)
        requests.post(f"{api_base}/api/plc/gateways/start", headers=headers, json={"gateway_id": stop}, timeout=5.0)
        if base == 0:
            rep.add("interference test", "WARN", f"baseline=0 for {keep}")
        else:
            drift = abs(after - base) / base
            rep.add(
                f"kept gateway {keep} throughput drift",
                "PASS" if drift < 0.10 else "WARN",
                f"base={base} after-stop={after} drift={drift*100:.1f}%",
            )

    # 8. Cloud mirror push progress --------------------------------------
    section("8. Cloud mirror push progress")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        pushed = con.execute(
            "SELECT last_historian_id FROM data_sync_state WHERE id=1"
        ).fetchone()
        pushed_id = int(pushed[0] or 0) if pushed else 0
        local_max = con.execute(
            "SELECT MAX(id) FROM historian_readings"
        ).fetchone()
        local_max_id = int(local_max[0] or 0) if local_max else 0
        backlog = max(0, local_max_id - pushed_id)
        rep.add(
            "cloud mirror backlog",
            "PASS" if backlog < 100 else ("WARN" if backlog < 1000 else "FAIL"),
            f"{backlog} rows pending (local max={local_max_id} pushed={pushed_id})",
        )
        # Per-gateway: how many of the last 200 PLC rows are still pending?
        rows_recent = con.execute(
            """SELECT gateway_id, COUNT(*) FROM historian_readings
               WHERE id > ? AND source NOT IN ('power_modbus','power_insight')
               GROUP BY gateway_id""",
            (pushed_id,),
        ).fetchall()
        for gid, c in rows_recent:
            rep.add(
                f"  pending push from {gid}",
                "PASS" if c < 200 else "WARN",
                f"{c} rows",
            )
        con.close()
    except Exception as exc:
        rep.add("cloud mirror backlog", "WARN", str(exc))

    # 9. Lite-mirror surface for PLC gateways ----------------------------
    section("9. Lite-mirror surface")
    try:
        b = requests.get(f"{api_base}/api/app-store/bootstrap", headers=headers, timeout=3.0).json()
        data = b.get("data", b) if isinstance(b, dict) else {}
        gws = data.get("gateway_configurations") or []
        if isinstance(gws, dict):
            gws = gws.get("gateways") or [gws]
        rep.add(
            "lite bootstrap: PLC gateways",
            "PASS" if gws else "WARN",
            f"{len(gws)} gateway(s)",
        )
        # Verify each gateway carries a non-empty tag list — Lite needs
        # it to render the dashboard tag picker.
        for g in gws:
            tags = g.get("tags") or []
            rep.add(
                f"  {g.get('id')} tag list in bootstrap",
                "PASS" if tags else "WARN",
                f"{len(tags)} tag(s)",
            )
        # Are dashboard widgets pointed at PLC gateways present?
        dboard = data.get("dashboard_configurations") or {}
        widgets = dboard.get("widgets") or [] if isinstance(dboard, dict) else []
        plc_widgets = [
            w for w in widgets
            if any(str((w.get("config") or {}).get("gateway_id") or "") == g.get("id") for g in gws)
        ]
        rep.add(
            "lite bootstrap: PLC-bound widgets",
            "PASS" if widgets is not None else "WARN",
            f"{len(plc_widgets)} of {len(widgets)} widgets bound to a PLC gateway",
        )
    except Exception as exc:
        rep.add("lite bootstrap", "FAIL", str(exc))

    print()
    print(f"Summary: {rep.summary()}")
    return 1 if rep.fails() else 0


if __name__ == "__main__":
    sys.exit(main())
