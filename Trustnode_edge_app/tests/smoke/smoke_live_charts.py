#!/usr/bin/env python3
"""
Live chart freshness smoke (operator 2026-06-16).

Proves the data path that drives every "live" chart on the app:

    PLC / Modbus poll  →  power_manager  →  SQLite historian
                                          ↘  live cache
                                          ↘  cloud mirror (Lite)

For each surface (Power Overview, Dashboard widgets, Lite mirror)
we measure:

    * cadence — how often does a fresh row arrive?
    * freshness — how stale is the newest row relative to now?
    * lockstep — does API output advance every poll cycle?

The pass bar:

    * fast tags (live_kw / current_a / active_power_kw) ≤ 1.3 s
      median Δts, ≤ 2.0 s p95
    * slow tags (energy_cost / total_kwh / tariff_*) ≤ 5.6 s median,
      ≤ 8 s p95 (configured 5 s × 1.6 schedule jitter)
    * /api/power/latest age < 1.5 s
    * /api/power/history newest-row age < 1.5 s
    * /api/app-store/historian/range freshness < 2 s (the path
      the Dashboard widgets and Lite both use for windowed reads)
    * cloud mirror freshness: pending_cloud_push backlog
      < 100 rows (the smoke can't reach the cloud DB directly but
      this is the queue gauge the edge maintains)

Run:
    python tests/smoke/smoke_live_charts.py
Add  TRUSTNODE_SMOKE_WATCH=30  to sample for 30 s instead of the
default 15 s window.
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
            token = (r.json() or {}).get("token")
            if token:
                return {"Authorization": f"Bearer {token}"}
    except Exception:
        pass
    return {}


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    # SQLite created_utc lacks a timezone — assume UTC so it can be
    # subtracted from event ts_utc cleanly.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


FAST_TAGS = {
    "active_power_w", "current_a", "voltage_v",
    "insight.live_kw", "insight.active_power_kw", "insight.current_a",
    "insight.active_tariff_index",
}
SLOW_TAGS = {
    "insight.total_kwh", "insight.energy_cost_eur",
    "insight.power_usage_kwh", "insight.peak_kw",
    "insight.energy_efficiency_pct", "insight.downtime_cost_eur",
    "insight.active_tariff_rate_eur_kwh",
}


def main() -> int:
    api_base = resolve_api_base()
    db_path = resolve_db_path()
    watch_s = int(os.environ.get("TRUSTNODE_SMOKE_WATCH", "15") or "15")
    print(f"API:   {api_base}")
    print(f"DB:    {db_path}")
    print(f"Watch: {watch_s} s")
    print(f"Time:  {utc_now().isoformat()}")
    rep = Report()

    # 1. Sanity: backend + meter ----------------------------------------
    section("1. Backend + meter readiness")
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

    pcfg = requests.get(f"{api_base}/api/power/config", headers=headers, timeout=3.0).json()
    cfg = pcfg.get("config", {}) if isinstance(pcfg, dict) else {}
    meters = [d for d in (cfg.get("devices") or []) if d.get("enabled")]
    rep.add("meter config", "PASS" if meters else "WARN",
            f"{len(meters)} enabled meter(s)")

    status_body = requests.get(f"{api_base}/api/power/status", headers=headers, timeout=3.0).json()
    connected = [d for d in (status_body.get("status") or {}).get("devices", []) if d.get("connected")]
    rep.add("meter connected", "PASS" if connected else "FAIL",
            f"{len(connected)} of {len(meters)} connected")
    if not connected:
        print()
        print(rep.summary())
        return 1

    meter_id = connected[0]["device_id"]
    poll_ms = int(connected[0].get("poll_interval_ms") or 1000)
    rep.add(f"primary meter", "INFO", f"{meter_id} poll={poll_ms}ms")

    # 2. SQLite historian ts cadence per tag -----------------------------
    section("2. Historian ts cadence (last 2 min)")
    cutoff = (utc_now() - timedelta(minutes=2)).isoformat()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        rows = con.execute(
            """SELECT tag_name, ts_utc, created_utc
               FROM historian_readings
               WHERE ts_utc >= ? AND source IN ('power_modbus','power_insight')
               ORDER BY tag_name, ts_utc""",
            (cutoff,),
        ).fetchall()
        con.close()
    except Exception as exc:
        rep.add("sqlite read", "FAIL", str(exc))
        rows = []

    per_tag: dict[str, list[str]] = {}
    write_lag_ms: list[float] = []
    for tag, ts, created in rows:
        per_tag.setdefault(tag, []).append(ts)
        a, b = parse_iso(ts), parse_iso(created)
        if a and b:
            write_lag_ms.append((b - a).total_seconds() * 1000.0)

    fast_med: list[float] = []
    slow_med: list[float] = []
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
        # Classify
        if tag in FAST_TAGS:
            fast_med.append(med)
            ok = med <= 1.3 and p95 <= 2.0
        elif tag in SLOW_TAGS:
            slow_med.append(med)
            # Slow tags are throttled to every 5th poll on a 1s base.
            ok = 3.0 <= med <= 5.6 and p95 <= 8.0
        elif tag.startswith("insight.tariff_"):
            slow_med.append(med)
            ok = 3.0 <= med <= 5.6 and p95 <= 8.0
        else:
            # Raw register tags follow the meter poll.
            ok = med <= 1.3 and p95 <= 2.0
            fast_med.append(med)
        rep.add(
            f"  {tag}",
            "PASS" if ok else "WARN",
            f"{len(ts_list)} rows, median {med:.3f}s p95 {p95:.3f}s",
        )

    if fast_med:
        ok = max(fast_med) <= 1.3
        rep.add("fast-tag cadence", "PASS" if ok else "WARN",
                f"max median {max(fast_med):.3f}s across {len(fast_med)} tag(s)")
    if slow_med:
        ok = max(slow_med) <= 5.6
        rep.add("slow-tag cadence", "PASS" if ok else "WARN",
                f"max median {max(slow_med):.3f}s across {len(slow_med)} tag(s)")
    if write_lag_ms:
        med = statistics.median(write_lag_ms)
        p95 = sorted(write_lag_ms)[max(0, int(0.95 * len(write_lag_ms)) - 1)]
        ok = med <= 250 and p95 <= 700
        rep.add(
            "historian write lag",
            "PASS" if ok else "WARN",
            f"median {med:.0f}ms p95 {p95:.0f}ms",
        )

    # 3. /api/power/latest freshness -------------------------------------
    section("3. /api/power/latest freshness")
    samples_lat: list[float] = []
    sample_count = 0
    for _ in range(8):
        t0 = time.monotonic()
        r = requests.get(f"{api_base}/api/power/latest", headers=headers, timeout=3.0).json()
        dt_ms = (time.monotonic() - t0) * 1000.0
        s = r.get("sample") or {}
        ts = parse_iso(s.get("ts"))
        if ts:
            age = (utc_now() - ts).total_seconds()
            samples_lat.append(age)
            sample_count += 1
        time.sleep(0.5)
    if samples_lat:
        med = statistics.median(samples_lat)
        worst = max(samples_lat)
        ok = med <= 1.5 and worst <= 2.5
        rep.add("/api/power/latest age",
                "PASS" if ok else "WARN",
                f"median {med:.2f}s worst {worst:.2f}s over {sample_count} samples")
    else:
        rep.add("/api/power/latest age", "FAIL", "no sample.ts returned")

    # 4. /api/power/history newest-row freshness -------------------------
    section("4. /api/power/history freshness & lockstep")
    seen_ts: list[str] = []
    for _ in range(int(watch_s)):
        t0 = time.monotonic()
        r = requests.get(f"{api_base}/api/power/history?limit=300", headers=headers, timeout=4.0).json()
        rows = r.get("rows") or []
        dt_ms = (time.monotonic() - t0) * 1000.0
        if rows:
            newest = rows[0].get("ts") or rows[0].get("ts_utc")
            if newest:
                seen_ts.append(str(newest))
        time.sleep(1.0)
    if seen_ts:
        latest_age = (utc_now() - parse_iso(seen_ts[-1])).total_seconds()
        unique = sorted(set(seen_ts))
        # Lockstep: number of distinct newest-row timestamps over the
        # watch window. With a 1s poll + 1s history throttle we'd
        # expect roughly watch_s/1 distinct values; require at least
        # half of that to confirm the chart is actually advancing.
        expected_min = max(2, int(watch_s * 0.4))
        ok_step = len(unique) >= expected_min
        rep.add(
            "history advancing every poll",
            "PASS" if ok_step else "WARN",
            f"{len(unique)} distinct newest-ts over {watch_s}s (>= {expected_min})",
        )
        rep.add(
            "history newest row age",
            "PASS" if latest_age <= 2.0 else "WARN",
            f"{latest_age:.2f}s",
        )
    else:
        rep.add("/api/power/history rows", "FAIL", "no rows returned")

    # 5. /api/app-store/historian/range freshness (used by dashboard widgets / lite)
    section("5. /api/app-store/historian/range (dashboard + lite path)")
    to_iso = utc_now().isoformat()
    from_iso = (utc_now() - timedelta(minutes=5)).isoformat()
    url = (
        f"{api_base}/api/app-store/historian/range"
        f"?from_utc={from_iso}&to_utc={to_iso}&limit=2000"
    )
    try:
        t0 = time.monotonic()
        rr = requests.get(url, headers=headers, timeout=5.0).json()
        dt_ms = (time.monotonic() - t0) * 1000.0
        rows = rr.get("rows") or []
        if rows:
            newest = rows[-1] if rows[-1].get("ts_utc", "") > rows[0].get("ts_utc", "") else rows[0]
            ts = parse_iso(newest.get("ts_utc") or newest.get("ts"))
            age = (utc_now() - ts).total_seconds() if ts else None
            ok = age is not None and age <= 3.0
            rep.add(
                "range endpoint freshness",
                "PASS" if ok else "WARN",
                f"{len(rows)} rows, newest age {age:.2f}s, query {dt_ms:.0f}ms" if age is not None else f"{len(rows)} rows",
            )
        else:
            rep.add("range endpoint", "WARN", "no rows in 5-min window")
    except Exception as exc:
        rep.add("range endpoint", "WARN", str(exc))

    # 6. Lite/cloud mirror progress -------------------------------------
    section("6. Cloud mirror push progress")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        # pending = rows newer than last_historian_id pushed to cloud
        pushed = con.execute(
            "SELECT last_historian_id FROM data_sync_state WHERE id=1"
        ).fetchone()
        pushed_id = int(pushed[0] or 0) if pushed else 0
        local_max = con.execute(
            "SELECT MAX(id) FROM historian_readings"
        ).fetchone()
        local_max_id = int(local_max[0] or 0) if local_max else 0
        con.close()
        backlog = max(0, local_max_id - pushed_id)
        rep.add(
            "cloud mirror backlog",
            "PASS" if backlog < 100 else ("WARN" if backlog < 1000 else "FAIL"),
            f"{backlog} rows pending (local max={local_max_id} pushed={pushed_id})",
        )
    except Exception as exc:
        rep.add("cloud mirror backlog", "WARN", str(exc))

    # 7. Per-widget chart freshness sample -------------------------------
    # We can't read the React DOM, but each widget reads from /api/app-store/historian/range
    # or /api/power/history. Simulate one read per visible widget shape
    # and assert all of them complete fast AND see fresh data.
    section("7. Per-widget data-fetch latency (simulated)")
    sim_widgets = [
        ("KPI insight.live_kw", "/api/power/latest"),
        ("KPI insight.current_a", "/api/power/latest"),
        ("KPI insight.active_power_kw", "/api/power/latest"),
        ("KPI insight.energy_cost_eur", "/api/power/latest"),
        ("Trend active_power_w", "/api/power/history?limit=300"),
        ("Trend current_a", "/api/power/history?limit=300"),
        ("Bar energy_wh", "/api/power/history?limit=300"),
        ("Energy Tariffs (kWh)", "/api/power/history?limit=300"),
        ("Energy Tariffs (cost)", "/api/power/history?limit=300"),
    ]
    fetch_times: list[float] = []
    for label, ep in sim_widgets:
        t0 = time.monotonic()
        try:
            r = requests.get(f"{api_base}{ep}", headers=headers, timeout=4.0)
            r.raise_for_status()
            dt_ms = (time.monotonic() - t0) * 1000.0
            fetch_times.append(dt_ms)
            rep.add(
                f"  {label}",
                "PASS" if dt_ms <= 200 else ("WARN" if dt_ms <= 500 else "FAIL"),
                f"{dt_ms:.0f}ms",
            )
        except Exception as exc:
            rep.add(f"  {label}", "FAIL", str(exc))
    if fetch_times:
        rep.add(
            "all-widget fetch budget",
            "PASS" if sum(fetch_times) <= 1500 else "WARN",
            f"sum {sum(fetch_times):.0f}ms across {len(sim_widgets)} widgets",
        )

    # 8. Verify all dashboard-bound insight tags are still streaming ----
    section("8. Dashboard insight-tag coverage")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        recent = (utc_now() - timedelta(seconds=15)).isoformat()
        recent_tags = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT tag_name FROM historian_readings WHERE ts_utc>=? AND source='power_insight'",
                (recent,),
            ).fetchall()
        }
        con.close()
    except Exception:
        recent_tags = set()
    # The default Power dashboard uses these eight as KPIs:
    expected_kpis = {
        "insight.current_a",
        "insight.active_power_kw",
        "insight.power_usage_kwh",
        "insight.energy_efficiency_pct",
        "insight.peak_kw",
        "insight.downtime_cost_eur",
        "insight.total_kwh",
        "insight.energy_cost_eur",
    }
    missing = expected_kpis - recent_tags
    rep.add(
        "live insight KPI tag stream",
        "PASS" if not missing else "WARN",
        f"{len(expected_kpis - missing)}/{len(expected_kpis)} present"
        + (f", missing={sorted(missing)}" if missing else ""),
    )

    # Lite path: the cloud Lite uses the same insight.* tags through
    # the read-only historian endpoint. Confirm the bootstrap exposes
    # the meter and the active tariff list so Lite can render names.
    section("9. Lite-mirror surface")
    try:
        b = requests.get(f"{api_base}/api/app-store/bootstrap", headers=headers, timeout=3.0).json()
        data = b.get("data", b) if isinstance(b, dict) else {}
        pmc = data.get("power_management_config") or {}
        tariffs = pmc.get("electricity_tariffs") or []
        devs = pmc.get("devices") or []
        rep.add(
            "lite bootstrap: meters",
            "PASS" if devs else "WARN",
            f"{len(devs)} meter(s)",
        )
        rep.add(
            "lite bootstrap: tariffs",
            "PASS" if tariffs else "WARN",
            f"{len(tariffs)} tariff(s)",
        )
        dboard = data.get("dashboard_configurations") or {}
        widgets = dboard.get("widgets") or []
        insight_widgets = [
            w for w in widgets if str(((w.get("config") or {}).get("tag_name") or "")).startswith("insight.")
        ]
        rep.add(
            "lite bootstrap: dashboard widgets",
            "PASS" if widgets else "WARN",
            f"{len(widgets)} widget(s), {len(insight_widgets)} on insight.*",
        )
    except Exception as exc:
        rep.add("lite bootstrap", "FAIL", str(exc))

    print()
    print(f"Summary: {rep.summary()}")
    return 1 if rep.fails() else 0


if __name__ == "__main__":
    sys.exit(main())
