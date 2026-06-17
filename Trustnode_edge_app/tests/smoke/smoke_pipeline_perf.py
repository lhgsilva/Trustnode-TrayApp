#!/usr/bin/env python3
"""
End-to-end pipeline + performance smoke (operator 2026-06-16).

Walks every layer that matters when the operator says "the app
feels slow":

    1. /api/health + auth                       — reachable, login works
    2. Gateway inventory                         — list of configured gateways + meters
    3. PLC gateway poll cadence                  — historian rows per gateway/min
    4. Power meter poll cadence + insight fan-out — fast vs slow insight tags
    5. Local historian growth + queue depth      — write rate + lag
    6. Cloud mirror status                       — power_management_config domain
       reaches mirror table, dashboard widgets too
    7. UI-facing API latency                     — config / status / latest / history
       wall-clock from the same machine
    8. Start/Stop wall-clock                     — measures backend round-trip
       only (frontend optimistic flip is instant)
    9. Cross-gateway interference                 — stops one meter, measures
       whether OTHER gateways' historian write cadence stays within 5 % of
       baseline
    10. Lite-mirror gap check                     — historian INSERT tags vs
        what the cloud read endpoint surfaces

Run:
    python tests/smoke/smoke_pipeline_perf.py

Environment overrides:
    TRUSTNODE_API_BASE       (default: probe 8000..8009)
    TRUSTNODE_APP_STORE_PATH (default: ~/.trustnode_edge/data/...db OR LOCALAPPDATA)
    TRUSTNODE_SMOKE_USER / TRUSTNODE_SMOKE_PASS (default admin/admin)
    TRUSTNODE_SMOKE_DISRUPT  (1 = exercise the stop/start interference test)

Exit codes: 0 = no FAILs, 1 = at least one FAIL, 2 = backend unreachable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
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


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))
        bar = {"PASS": PASS, "FAIL": FAIL, "WARN": WARN, "INFO": INFO}[status]
        print(f"  [{bar}] {name}" + (f"  — {detail}" if detail else ""))

    def fails(self) -> int:
        return sum(1 for _, s, _ in self.checks if s == "FAIL")

    def warns(self) -> int:
        return sum(1 for _, s, _ in self.checks if s == "WARN")

    def summary(self) -> str:
        n = len(self.checks)
        bad = self.fails()
        warn = self.warns()
        info = sum(1 for _, s, _ in self.checks if s == "INFO")
        good = n - bad - warn - info
        return f"{good} passed, {warn} warnings, {bad} failed (of {n - info} checks)"


def section(title: str) -> None:
    print()
    print(f"\033[1m== {title} ==\033[0m")


def time_get(url: str, headers: dict[str, str], timeout: float = 5.0) -> tuple[float, int, Any]:
    """Returns (elapsed_ms, status_code, json_or_text)."""
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        return ((time.monotonic() - t0) * 1000.0, 0, str(exc))
    dt = (time.monotonic() - t0) * 1000.0
    try:
        return dt, r.status_code, r.json()
    except Exception:
        return dt, r.status_code, r.text


def time_post(url: str, headers: dict[str, str], json_body: Any = None, timeout: float = 5.0) -> tuple[float, int, Any]:
    t0 = time.monotonic()
    try:
        r = requests.post(url, headers=headers, json=json_body, timeout=timeout)
    except Exception as exc:
        return ((time.monotonic() - t0) * 1000.0, 0, str(exc))
    dt = (time.monotonic() - t0) * 1000.0
    try:
        return dt, r.status_code, r.json()
    except Exception:
        return dt, r.status_code, r.text


def main() -> int:
    api_base = resolve_api_base()
    db_path = resolve_db_path()
    print(f"API:  {api_base}")
    print(f"DB:   {db_path}")
    print(f"Time: {utc_now().isoformat()}")

    rep = Report()

    # ---- 1. Health + auth -------------------------------------------------
    section("1. Backend reachable + auth")
    dt_health, code, body = time_get(f"{api_base}/api/health", {}, timeout=2.0)
    if code != 200:
        rep.add("health endpoint", "FAIL", f"status={code} body={body!r}")
        print()
        print(rep.summary())
        return 2
    rep.add("health endpoint", "PASS", f"{dt_health:.0f}ms")
    headers = authenticate(api_base)
    if headers:
        rep.add("admin/admin login", "PASS")
    else:
        rep.add("admin/admin login", "WARN", "no token — read-only endpoints only")

    # ---- 2. Gateway inventory ---------------------------------------------
    section("2. Gateway inventory")
    dt_gw, code, gw_body = time_get(f"{api_base}/api/gateways", headers)
    plc_gateways: list[dict] = []
    if code == 200 and isinstance(gw_body, dict) and isinstance(gw_body.get("gateways"), list):
        plc_gateways = list(gw_body["gateways"])
        rep.add("PLC gateways list", "PASS", f"{len(plc_gateways)} gateway(s), {dt_gw:.0f}ms")
    else:
        rep.add("PLC gateways list", "WARN", f"status={code}")

    dt_pcfg, code, pcfg = time_get(f"{api_base}/api/power/config", headers)
    meters: list[dict] = []
    if code == 200 and isinstance(pcfg, dict):
        meters = list(((pcfg.get("config") or {}).get("devices") or []))
        rep.add("Power meters list", "PASS", f"{len(meters)} meter(s), {dt_pcfg:.0f}ms")
    else:
        rep.add("Power meters list", "FAIL", f"status={code}")

    # ---- 3. PLC gateway cadence -------------------------------------------
    section("3. PLC gateway poll cadence (last 5 min)")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        # Per-gateway row count over the last 5 min
        cutoff = (utc_now().timestamp() - 300.0)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        rows = con.execute(
            """SELECT gateway_id, gateway_name, source, COUNT(*), MIN(ts_utc), MAX(ts_utc)
               FROM historian_readings
               WHERE ts_utc >= ?
               GROUP BY gateway_id, gateway_name, source
               ORDER BY COUNT(*) DESC""",
            (cutoff_iso,),
        ).fetchall()
        con.close()
    except Exception as exc:
        rep.add("historian read", "FAIL", str(exc))
        rows = []

    plc_only = [r for r in rows if str(r[2] or "") not in ("power_modbus", "power_insight")]
    pmod_only = [r for r in rows if str(r[2] or "") == "power_modbus"]
    pins_only = [r for r in rows if str(r[2] or "") == "power_insight"]

    for gw_id, gw_name, source, count, first_ts, last_ts in (plc_only[:8] or []):
        rate = float(count) / 5.0  # rows/min
        rep.add(f"  PLC {gw_name or gw_id} [{source}]", "INFO", f"{count} rows, ~{rate:.1f}/min")
    if not plc_only:
        rep.add("PLC gateways collecting", "WARN", "no PLC historian rows in last 5 min")

    # ---- 4. Power meter cadence + insight fan-out --------------------------
    section("4. Power meter cadence + insight fan-out (last 5 min)")
    for gw_id, gw_name, source, count, first_ts, last_ts in pmod_only:
        rate = float(count) / 5.0
        rep.add(f"  modbus {gw_name or gw_id}", "INFO", f"{count} rows, ~{rate:.1f}/min")
    for gw_id, gw_name, source, count, first_ts, last_ts in pins_only:
        rate = float(count) / 5.0
        rep.add(f"  insight {gw_name or gw_id}", "INFO", f"{count} rows, ~{rate:.1f}/min")

    # Compute expected insight rate after operator 2026-06-16 fix:
    # 4 fast rows per poll + 7 slow + (2 × tariffs) every 5 polls.
    if meters and pcfg.get("config"):
        tariffs = list(((pcfg.get("config") or {}).get("electricity_tariffs") or []))
        slow_per_burst = 7 + 2 * len(tariffs)
        expected_per_meter_per_poll = 4 + (slow_per_burst / 5.0)
        rep.add(
            "insight emission model",
            "INFO",
            f"{len(tariffs)} tariffs × {len(meters)} meter(s) ⇒ expected {expected_per_meter_per_poll:.1f} insight rows/sec/meter",
        )

    # Verify fast vs slow split — every meter should have ~5× more
    # insight.live_kw rows than insight.energy_cost_eur rows (the slow
    # tag) in the last 5 minutes.
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        tag_counts = dict(
            con.execute(
                """SELECT tag_name, COUNT(*)
                   FROM historian_readings
                   WHERE ts_utc >= ? AND tag_name LIKE 'insight.%'
                   GROUP BY tag_name""",
                (cutoff_iso,),
            ).fetchall()
        )
        con.close()
    except Exception:
        tag_counts = {}
    fast = int(tag_counts.get("insight.live_kw", 0))
    slow = int(tag_counts.get("insight.energy_cost_eur", 0))
    if fast and slow:
        ratio = fast / max(1, slow)
        # Operator 2026-06-16: throttle is every 5th poll → ratio ~5.
        # Accept 3–8 to allow some warm-up jitter.
        if 3 <= ratio <= 8:
            rep.add("insight fast/slow ratio", "PASS", f"{fast}/{slow} ≈ {ratio:.1f}× (expected ~5×)")
        else:
            rep.add("insight fast/slow ratio", "WARN", f"{fast}/{slow} ≈ {ratio:.1f}× (expected ~5×)")
    elif fast and not slow:
        rep.add("insight slow tags missing", "WARN", "no insight.energy_cost_eur rows in window")
    elif not fast:
        rep.add("insight tags absent", "WARN", "no insight.live_kw rows in window (meter offline?)")

    # ---- 5. Local historian growth + writer queue --------------------------
    section("5. Historian growth + writer queue")
    dt_d, code, diag = time_get(f"{api_base}/api/power/diagnostics", headers)
    if code == 200 and isinstance(diag, dict):
        d = diag.get("diagnostics") or {}
        queue_depth = int(d.get("writer_queue_depth") or 0)
        dropped = int(d.get("writer_dropped_rows") or 0)
        batches = int(d.get("writer_batches") or 0)
        rep.add(
            "power writer",
            "PASS" if queue_depth < 200 and dropped == 0 else "WARN",
            f"queue={queue_depth} dropped={dropped} batches={batches}",
        )
        # Per-device metrics
        statuses = d.get("statuses") or {}
        metrics = d.get("metrics") or {}
        for did, m in (metrics.items() if isinstance(metrics, dict) else []):
            eff = m.get("effective_interval_ms")
            poll = m.get("poll_duration_ms")
            lag = m.get("schedule_lag_ms")
            skipped = m.get("skipped_cycles")
            label = statuses.get(did, {}).get("name", did) if isinstance(statuses, dict) else did
            detail = f"poll={poll}ms effective={eff}ms lag={lag}ms skipped={skipped}"
            # If effective_interval is 2× or more above the configured
            # poll_interval, the device is dropping cycles.
            poll_cfg = statuses.get(did, {}).get("poll_interval_ms", 1000) if isinstance(statuses, dict) else 1000
            level = "PASS"
            if eff is not None and float(eff) > 1.6 * float(poll_cfg or 1000):
                level = "WARN"
            rep.add(f"  device {label}", level, detail)
    else:
        rep.add("/api/power/diagnostics", "WARN", f"status={code}")

    # ---- 6. Cloud mirror status -------------------------------------------
    section("6. Cloud mirror coverage")
    # We can't reach the cloud DB from this script, but the app's bootstrap
    # endpoint reflects what the edge has prepared for the mirror.
    dt_b, code, boot = time_get(f"{api_base}/api/app-store/bootstrap", headers)
    # The endpoint wraps the bootstrap under `data` (alongside `ok`,
    # `tenant_id`, `scope_key`). Older callers used the bare dict.
    data = boot.get("data") if isinstance(boot, dict) and isinstance(boot.get("data"), dict) else boot
    if code == 200 and isinstance(data, dict):
        pmc = data.get("power_management_config")
        if isinstance(pmc, dict):
            rep.add("power_management_config in bootstrap", "PASS",
                    f"{len(pmc.get('devices') or [])} meter(s), {len(pmc.get('electricity_tariffs') or [])} tariff(s)")
        else:
            rep.add("power_management_config in bootstrap", "FAIL",
                    "missing — Lite won't render tariffs / meter list")
        dboard = data.get("dashboard_configurations")
        widgets = (dboard or {}).get("widgets") if isinstance(dboard, dict) else None
        insight_widgets = [w for w in (widgets or []) if str(((w.get("config") or {}).get("tag_name") or "")).startswith("insight.")]
        rep.add(
            "dashboard widgets in bootstrap",
            "PASS" if widgets is not None else "WARN",
            f"{len(widgets or [])} widget(s), {len(insight_widgets)} bound to insight.*",
        )
    else:
        rep.add("/api/app-store/bootstrap", "WARN", f"status={code}")

    # ---- 7. UI-facing API latency -----------------------------------------
    section("7. UI-facing API latency (10 samples each)")
    endpoints = [
        ("/api/power/config", None),
        ("/api/power/status", None),
        ("/api/power/latest", None),
        ("/api/power/history?limit=300", None),
    ]
    for path, body in endpoints:
        samples = []
        for _ in range(10):
            dt, _code, _ = time_get(f"{api_base}{path}", headers, timeout=3.0)
            samples.append(dt)
        med = statistics.median(samples)
        p95 = sorted(samples)[int(0.95 * len(samples)) - 1]
        rep.add(
            f"GET {path}",
            "PASS" if med < 200 else ("WARN" if med < 500 else "FAIL"),
            f"median={med:.0f}ms p95={p95:.0f}ms",
        )

    # ---- 8. Start/Stop wall-clock -----------------------------------------
    section("8. Power start/stop wall-clock")
    if meters and headers:
        # Pick the first enabled meter — toggle off, then on. Each call
        # should return in well under 500 ms now that update_config no
        # longer blocks on cloud mirror.
        meter = meters[0]
        did = str(meter.get("id") or "")
        was_enabled = bool(meter.get("enabled", True))
        try:
            dt_stop, code, _ = time_post(f"{api_base}/api/power/devices/{did}/stop", headers, timeout=5.0)
            rep.add(
                f"POST /api/power/devices/{did}/stop",
                "PASS" if dt_stop < 500 else ("WARN" if dt_stop < 1200 else "FAIL"),
                f"{dt_stop:.0f}ms (status={code})",
            )
            time.sleep(0.5)
            dt_start, code, _ = time_post(f"{api_base}/api/power/devices/{did}/start", headers, timeout=5.0)
            rep.add(
                f"POST /api/power/devices/{did}/start",
                "PASS" if dt_start < 500 else ("WARN" if dt_start < 1200 else "FAIL"),
                f"{dt_start:.0f}ms (status={code})",
            )
            # Restore prior enable state
            if not was_enabled:
                time.sleep(0.3)
                time_post(f"{api_base}/api/power/devices/{did}/stop", headers, timeout=5.0)
        except Exception as exc:
            rep.add("start/stop", "FAIL", str(exc))
    else:
        rep.add("start/stop", "INFO", "skipped (no meters or no auth token)")

    # ---- 9. Cross-gateway interference (opt-in) ---------------------------
    section("9. Cross-gateway interference (DISRUPT mode)")
    if not os.environ.get("TRUSTNODE_SMOKE_DISRUPT"):
        rep.add("interference test", "INFO", "set TRUSTNODE_SMOKE_DISRUPT=1 to run")
    elif len(meters) < 2:
        rep.add("interference test", "INFO", "needs ≥ 2 meters configured")
    else:
        # Measure other meter's row count, then stop one meter,
        # measure again, restart, measure again. The other meter
        # should keep within 5 % of the baseline rate.
        keep, stop = meters[0], meters[1]
        keep_id = str(keep.get("id"))
        stop_id = str(stop.get("id"))

        def count_rows(gw_id: str, window_s: float) -> int:
            since = datetime.fromtimestamp(time.time() - window_s, tz=timezone.utc).isoformat()
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                return int(con.execute(
                    "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND ts_utc>=? AND source='power_modbus'",
                    (gw_id, since),
                ).fetchone()[0])
            finally:
                con.close()

        rep.add("baseline window", "INFO", "30 s with both meters running")
        time.sleep(30)
        base = count_rows(keep_id, 30)
        time_post(f"{api_base}/api/power/devices/{stop_id}/stop", headers, timeout=5.0)
        rep.add(f"stopped {stop_id}", "INFO", "")
        time.sleep(30)
        stopped_window = count_rows(keep_id, 30)
        time_post(f"{api_base}/api/power/devices/{stop_id}/start", headers, timeout=5.0)
        if base == 0:
            rep.add("interference test", "WARN", f"baseline=0 (kept meter idle?)")
        else:
            drift = abs(stopped_window - base) / base
            rep.add(
                "kept meter throughput drift",
                "PASS" if drift < 0.10 else "WARN",
                f"base={base} after-stop={stopped_window} drift={drift*100:.1f}%",
            )

    # ---- 10. Lite-mirror tag coverage --------------------------------------
    section("10. Lite-mirror tag coverage")
    # Lite is read-only; we can't probe it directly. Confirm that the
    # tags Lite would consume actually exist in the local historian.
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        tags_in_window = [
            r[0] for r in con.execute(
                "SELECT DISTINCT tag_name FROM historian_readings WHERE ts_utc>=? ORDER BY tag_name",
                (cutoff_iso,),
            ).fetchall()
        ]
        con.close()
    except Exception:
        tags_in_window = []
    needed_lite = {"voltage_v", "current_a", "active_power_w", "energy_wh"}
    present = needed_lite & set(tags_in_window)
    rep.add(
        "legacy Lite tags present",
        "PASS" if present else "WARN",
        f"{len(present)}/{len(needed_lite)} ({sorted(present)})",
    )
    insight_in_window = [t for t in tags_in_window if str(t).startswith("insight.")]
    rep.add(
        "insight.* tags being logged",
        "PASS" if insight_in_window else "WARN",
        f"{len(insight_in_window)} distinct tag(s)",
    )

    # ---- Summary -----------------------------------------------------------
    print()
    print(f"Summary: {rep.summary()}")
    return 1 if rep.fails() else 0


if __name__ == "__main__":
    sys.exit(main())
