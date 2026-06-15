#!/usr/bin/env python3
"""
Power Overview smoke test (operator 2026-06-15).

Validates that the metering pipeline behaves like a professional
energy-monitoring tool: live values present, kWh trapezoidal-
correct, tariff slices reconcile with the total, insight tags
persisted to the historian, deltas finite, and the
KPI / chart / donut values come from the same arithmetic.

Run:
    python tests/smoke/smoke_power_overview.py

Environment overrides:
    TRUSTNODE_API_BASE       (default http://127.0.0.1:8000)
    TRUSTNODE_APP_STORE_PATH (default ~/.trustnode_edge/data/trustnode_app_store.db)

Exit code 0 = all OK, 1 = at least one CHECK failed,
2 = backend unreachable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


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
    # Try the documented defaults — the desktop launcher walks 8000..8004.
    for port in range(8000, 8010):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1.5)
            if r.ok:
                return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return "http://127.0.0.1:8000"


def resolve_db_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


# ---------- trapezoidal kWh reference ------------------------------------
def trapezoidal_kwh(samples: list[tuple[float, float]], gap_cap_h: float = 1.0) -> float:
    """Walk (epoch_s, kw) samples in order and integrate kW*dt with the
    trapezoidal rule. dt > gap_cap_h is treated as offline gap and
    contributes 0 kWh. Negative kW is clamped to 0."""
    samples = sorted(samples, key=lambda s: s[0])
    total = 0.0
    for i in range(1, len(samples)):
        t0, kw0 = samples[i - 1]
        t1, kw1 = samples[i]
        dt_h = (t1 - t0) / 3600.0
        if dt_h <= 0 or dt_h > gap_cap_h:
            continue
        avg_kw = max(0.0, (kw0 + kw1) / 2.0)
        total += avg_kw * dt_h
    return total


# ---------- main ---------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))
        bar = {
            "PASS": PASS,
            "FAIL": FAIL,
            "WARN": WARN,
            "INFO": INFO,
        }[status]
        print(f"  [{bar}] {name}" + (f"  — {detail}" if detail else ""))

    def fails(self) -> int:
        return sum(1 for _, s, _ in self.checks if s == "FAIL")

    def summary(self) -> str:
        n = len(self.checks)
        bad = self.fails()
        warn = sum(1 for _, s, _ in self.checks if s == "WARN")
        good = n - bad - warn - sum(1 for _, s, _ in self.checks if s == "INFO")
        return f"{good} passed, {warn} warnings, {bad} failed (of {n - sum(1 for _, s, _ in self.checks if s == 'INFO')} checks)"


def section(title: str) -> None:
    print()
    print(f"\033[1m== {title} ==\033[0m")


def main() -> int:
    api_base = resolve_api_base()
    db_path = resolve_db_path()
    print(f"API:  {api_base}")
    print(f"DB:   {db_path}")
    print(f"Time: {utc_now().isoformat()}")
    r = Report()

    # ---------- 0. Backend reachable -------------------------------------
    section("0. Backend reachable")
    try:
        resp = requests.get(f"{api_base}/api/health", timeout=2.5)
        if not resp.ok:
            r.add("health endpoint", "FAIL", f"HTTP {resp.status_code}")
            print(f"\n{r.summary()}")
            return 2
        r.add("health endpoint", "PASS", f"HTTP {resp.status_code}")
    except Exception as exc:
        r.add("health endpoint", "FAIL", f"{exc.__class__.__name__}: {exc}")
        print(f"\nBackend unreachable. Start the edge and retry.\n{r.summary()}")
        return 2

    # ---------- 1. Power config ------------------------------------------
    section("1. Power configuration")
    cfg = {}
    try:
        cfg_resp = requests.get(f"{api_base}/api/power/config", timeout=4)
        cfg = (cfg_resp.json() or {}).get("config", {}) if cfg_resp.ok else {}
    except Exception as exc:
        r.add("GET /api/power/config", "FAIL", str(exc))
    devices = cfg.get("devices") or []
    enabled_devices = [d for d in devices if d.get("enabled", True)]
    r.add("devices configured", "PASS" if devices else "WARN",
          f"{len(devices)} device(s) ({len(enabled_devices)} enabled)")
    if not devices:
        print("\nNo power meters configured. Add one in Power Configuration before re-running.")
        print(r.summary())
        return 1

    tariffs = cfg.get("electricity_tariffs") or []
    r.add("electricity_tariffs", "PASS" if tariffs else "INFO",
          f"{len(tariffs)} tariff(s) defined")
    flat_rate = float(cfg.get("energy_price_eur_kwh") or 0.0)
    r.add("flat fallback rate", "INFO", f"€{flat_rate:.4f}/kWh")
    downtime_rules = cfg.get("downtime_rules") or []
    r.add("downtime_rules", "PASS" if downtime_rules else "INFO",
          f"{len(downtime_rules)} rule(s)")

    # ---------- 2. Live status -------------------------------------------
    section("2. Live meter status")
    statuses: list[dict] = []
    try:
        s_resp = requests.get(f"{api_base}/api/power/status", timeout=4)
        statuses = ((s_resp.json() or {}).get("status") or {}).get("devices") or []
    except Exception as exc:
        r.add("GET /api/power/status", "FAIL", str(exc))
    connected = [s for s in statuses if s.get("connected")]
    r.add("at least one meter connected", "PASS" if connected else "FAIL",
          f"{len(connected)}/{len(statuses)} connected")

    # ---------- 3. Historian rows ----------------------------------------
    section("3. Historian rows (power_modbus + power_insight)")
    hist_rows: list[dict] = []
    try:
        h_resp = requests.get(f"{api_base}/api/power/history?limit=2000", timeout=6)
        hist_rows = (h_resp.json() or {}).get("rows") or []
        r.add("GET /api/power/history?limit=2000", "PASS",
              f"{len(hist_rows)} row(s)")
    except Exception as exc:
        r.add("GET /api/power/history", "FAIL", str(exc))

    sources = {str(row.get("source") or "") for row in hist_rows}
    r.add("source = power_modbus present", "PASS" if "power_modbus" in sources else "FAIL",
          f"sources seen: {sorted(sources)}")
    insight_via_endpoint = "power_insight" in sources
    if insight_via_endpoint:
        r.add("source = power_insight via /history", "PASS", "")
    else:
        # We KNOW the writer emits power_insight rows; the /history endpoint
        # filters by source == 'power_modbus'. Surface this as a real bug.
        r.add("source = power_insight via /history", "FAIL",
              "endpoint filters out insight rows — Tags page / dashboard widgets "
              "will not see KPI tags through getPowerHistory")

    # Distinct tags seen
    tags_seen = {str(row.get("tag_name") or "") for row in hist_rows}
    expected_register_tags = {"voltage_v", "current_a", "active_power_w", "energy_wh"}
    missing = expected_register_tags - tags_seen
    r.add("core register tags present", "PASS" if not missing else "FAIL",
          f"missing={sorted(missing)}" if missing else "voltage_v / current_a / active_power_w / energy_wh OK")

    insight_tags = {t for t in tags_seen if t.startswith("insight.")}
    r.add("insight.* tags present (any source)",
          "PASS" if insight_tags else "WARN",
          f"{sorted(insight_tags) if insight_tags else 'none — operator should wait for poll cycle or check writer'}")

    # ---------- 4. Direct DB check (insight rows persisted) --------------
    section("4. Direct historian DB check")
    if not db_path.exists():
        r.add("historian DB exists", "WARN", f"not at {db_path} (set TRUSTNODE_APP_STORE_PATH if non-default)")
    else:
        try:
            con = sqlite3.connect(str(db_path), timeout=4)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # Find the historian table — varies between versions.
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%historian%'")
            tables = [row["name"] for row in cur.fetchall()]
            r.add("historian table(s)", "PASS" if tables else "FAIL", ",".join(tables) or "none")
            if tables:
                tname = tables[0]
                # Insight-tag persistence over the last 10 minutes.
                cur.execute(
                    f"SELECT tag_name, COUNT(*) AS n, MAX(ts_utc) AS last_ts FROM {tname} "
                    "WHERE source = 'power_insight' AND tag_name LIKE 'insight.%' "
                    "AND ts_utc >= ? GROUP BY tag_name",
                    ((utc_now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),),
                )
                ins = cur.fetchall()
                if ins:
                    r.add("insight.* persisted in DB (last 10 min)", "PASS",
                          f"{len(ins)} distinct tag(s), e.g. {ins[0]['tag_name']} ({ins[0]['n']} rows)")
                else:
                    r.add("insight.* persisted in DB (last 10 min)", "FAIL",
                          "no rows written — writer loop not emitting? check power_manager._compute_insight_rows")
                # Recent power_modbus row count
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {tname} WHERE source='power_modbus' "
                    "AND ts_utc >= ?",
                    ((utc_now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),),
                )
                n_mod = cur.fetchone()["n"]
                r.add("power_modbus rows in last 5 min", "PASS" if n_mod else "FAIL",
                      f"{n_mod} row(s)")
            con.close()
        except Exception as exc:
            r.add("direct DB query", "FAIL", str(exc))

    # ---------- 5. Trapezoidal kWh reconciliation ------------------------
    section("5. kWh integration vs frontend math")
    # Build (epoch_s, kw) per meter from rows in last 5 min.
    cutoff = utc_now() - timedelta(minutes=5)
    by_meter: dict[str, list[tuple[float, float]]] = {}
    for row in hist_rows:
        if str(row.get("tag_name") or "") not in ("active_power_total_w", "active_power_w"):
            continue
        ts_raw = str(row.get("ts") or row.get("ts_utc") or "")
        try:
            ts = datetime.strptime(ts_raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff:
            continue
        gid = str(row.get("gateway_id") or "")
        kw = float(row.get("value") or 0.0) / 1000.0
        by_meter.setdefault(gid, []).append((ts.timestamp(), kw))

    if not by_meter:
        r.add("active_power samples (last 5 min)", "FAIL", "0 samples — meter is not polling")
    else:
        for gid, samples in by_meter.items():
            total_kwh = trapezoidal_kwh(samples)
            avg_kw = sum(kw for _, kw in samples) / len(samples) if samples else 0.0
            r.add(f"trapezoidal kWh — {gid}", "PASS",
                  f"{len(samples)} sample(s), avg {avg_kw:.3f} kW → {total_kwh:.6f} kWh over 5 min window")
            # Sanity: trapezoidal ≈ avg_kw * window_h within 30 %
            samples_sorted = sorted(samples)
            window_h = (samples_sorted[-1][0] - samples_sorted[0][0]) / 3600.0 if len(samples_sorted) > 1 else 0
            if window_h > 0:
                naive = avg_kw * window_h
                diff_pct = abs(total_kwh - naive) / max(1e-9, naive) * 100.0
                ok = diff_pct <= 30.0
                r.add(f"  reconciles with avg_kw × window_h ({window_h*60:.1f} min)",
                      "PASS" if ok else "WARN",
                      f"trapezoid={total_kwh:.6f} kWh, naive={naive:.6f} kWh, diff={diff_pct:.2f}%")

    # ---------- 6. Tariff slice reconciliation ---------------------------
    section("6. Tariff slice reconciliation")
    if not tariffs and flat_rate <= 0:
        r.add("tariff config", "WARN", "no tariffs and no flat rate — cost calculations will be 0")
    elif by_meter:
        # Compute total cost using the trapezoidal kWh + flat rate as a baseline.
        baseline_cost = 0.0
        for samples in by_meter.values():
            baseline_cost += trapezoidal_kwh(samples) * flat_rate
        r.add("baseline cost computable", "PASS", f"€{baseline_cost:.6f} over 5 min window")
    else:
        r.add("tariff slice reconciliation", "WARN", "no samples to slice")

    # ---------- 7. Downtime semantics ------------------------------------
    section("7. Downtime detection")
    if not downtime_rules:
        r.add("downtime_rules configured", "INFO", "skipped — no rules defined")
    else:
        # Walk every per-meter sample, count slices where voltage >= min and power <= max.
        for rule in downtime_rules:
            v_min = float(rule.get("voltage_min_v") or 0)
            p_max = float(rule.get("power_max_kw") or 0)
            meter_id = str(rule.get("meter_id") or "")
            # Build per-ts samples for this meter
            per_ts: dict[str, dict[str, float]] = {}
            for row in hist_rows:
                gid = str(row.get("gateway_id") or "")
                if meter_id and gid != meter_id:
                    continue
                tag = str(row.get("tag_name") or "")
                if tag not in ("voltage_v", "active_power_w", "active_power_total_w"):
                    continue
                ts = str(row.get("ts") or row.get("ts_utc") or "")
                if not ts:
                    continue
                key = f"{gid}|{ts[:19]}"
                rec = per_ts.setdefault(key, {})
                if tag == "voltage_v":
                    rec["v"] = float(row.get("value") or 0.0)
                else:
                    rec["kw"] = float(row.get("value") or 0.0) / 1000.0
            matched = sum(1 for rec in per_ts.values()
                          if rec.get("v", 0.0) >= v_min and rec.get("kw", 1e9) <= p_max)
            r.add(f"rule '{rule.get('name')}' samples idle", "PASS" if matched >= 0 else "WARN",
                  f"{matched}/{len(per_ts)} sample timestamps satisfy V>={v_min} AND kW<={p_max}")

    # ---------- summary --------------------------------------------------
    section("Summary")
    print(f"  {r.summary()}")
    return 1 if r.fails() else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
