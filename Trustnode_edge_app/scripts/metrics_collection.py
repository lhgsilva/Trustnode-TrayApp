"""
TrustNode — collection & chart-update METRICS harness.

Measures, against a RUNNING app, the numbers that define "responsive, no data
loss, correct cadence":

  A. CADENCE           — actual inter-sample interval per tag vs the gateway's
                         configured interval_ms. Reports p50/p95/max gap, the
                         count of gaps > 2x and > 5x interval, and % on-target.
  B. DATA-LOSS         — expected sample count over the window vs actual, per tag
                         (missed samples = cadence gaps summed).
  C. FREsHNESS/LATENCY — how far behind "now" the newest stored row is (edge
                         write latency), sampled live over a short poll.
  D. STARTUP           — time from the last backend boot to the FIRST durable
                         row, read from backend.log ("first-row ... boot_to_
                         first_row=Xs"), plus any event-loop-stale kills and
                         gateway stalls in the log (the "running but no data"
                         signature).

Usage:
    python scripts/metrics_collection.py                 # analyze recent data
    python scripts/metrics_collection.py --watch 30      # + live freshness for 30s
    python scripts/metrics_collection.py --window 10     # analyze last 10 min

Exit 0 always (this is a report, not a gate). Read the verdicts.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

DB = os.path.expanduser(os.path.join("~", ".trustnode_edge", "data", "trustnode_app_store.db"))
LOG_CANDIDATES = [
    os.path.expanduser(r"~\AppData\Roaming\trustnode-edge-desktop\backend.log"),
    os.path.expanduser(r"~\AppData\Roaming\trustnode-desktop\backend.log"),
]


def _con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5.0)


def _parse(ts: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(ts)[:26], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _gateway_interval_ms() -> dict:
    """Read each gateway's configured interval_ms from the scoped config docs."""
    out: dict[str, int] = {}
    try:
        import json
        con = _con()
        for (payload,) in con.execute(
            "SELECT payload_json FROM config_documents_scoped WHERE domain='gateway_configurations'"
        ):
            data = json.loads(payload) if isinstance(payload, str) else payload
            items = data if isinstance(data, list) else (data.get("gateways") or data.get("items") or [])
            for g in items:
                gid = str(g.get("id") or "")
                iv = int(g.get("interval_ms") or 0)
                if gid and iv:
                    out[gid] = iv
        con.close()
    except Exception:
        pass
    return out


def section(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def metric_cadence_and_loss(window_min: int) -> None:
    section(f"A + B.  CADENCE vs GATEWAY INTERVAL  &  DATA LOSS  (last {window_min} min)")
    con = _con(); con.row_factory = sqlite3.Row
    cut = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    intervals = _gateway_interval_ms()
    default_iv = min(intervals.values()) if intervals else 1000
    tags = [r["tag_name"] for r in con.execute(
        "SELECT DISTINCT tag_name FROM historian_readings WHERE ts_utc>=? ORDER BY tag_name", (cut,))]
    if not tags:
        print("  (no readings in window — is a gateway running?)"); con.close(); return

    # resolve interval per tag via its gateway
    def iv_for(tag: str) -> int:
        row = con.execute(
            "SELECT gateway_id FROM historian_readings WHERE tag_name=? AND ts_utc>=? "
            "ORDER BY ts_utc DESC LIMIT 1", (tag, cut)).fetchone()
        gid = row["gateway_id"] if row else ""
        return intervals.get(gid, default_iv)

    print(f"  gateways: {intervals or '(interval unknown -> assuming %dms)' % default_iv}\n")
    hdr = f"  {'tag':<40} {'iv':>5} {'n':>5} {'p50':>6} {'p95':>6} {'max':>7} {'>2x':>4} {'ontgt%':>7} {'lost':>5}"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    worst = []
    for tag in tags:
        ts = [r["ts_utc"] for r in con.execute(
            "SELECT ts_utc FROM historian_readings WHERE tag_name=? AND ts_utc>=? ORDER BY ts_utc", (tag, cut))]
        if len(ts) < 3:
            continue
        iv_ms = iv_for(tag); iv_s = iv_ms / 1000.0
        gaps = []
        for i in range(len(ts) - 1):
            a, b = _parse(ts[i]), _parse(ts[i + 1])
            if a and b:
                gaps.append((b - a).total_seconds())
        if not gaps:
            continue
        p50 = statistics.median(gaps); p95 = sorted(gaps)[int(len(gaps) * 0.95)]; mx = max(gaps)
        over2 = sum(1 for g in gaps if g > iv_s * 2)
        on_target = sum(1 for g in gaps if abs(g - iv_s) <= iv_s * 0.25) / len(gaps) * 100
        # data loss = extra samples that SHOULD have appeared inside each gap
        span = (_parse(ts[-1]) - _parse(ts[0])).total_seconds()
        expected = int(span / iv_s) + 1 if iv_s > 0 else len(ts)
        lost = max(0, expected - len(ts))
        print(f"  {tag[:40]:<40} {iv_ms:>5} {len(ts):>5} {p50:>6.2f} {p95:>6.2f} {mx:>7.2f} {over2:>4} {on_target:>6.0f}% {lost:>5}")
        worst.append((on_target, tag, p50, mx, lost, iv_s))

    con.close()
    if worst:
        worst.sort()
        print("\n  VERDICT:")
        for on_target, tag, p50, mx, lost, iv_s in worst[:3]:
            status = "GOOD" if on_target >= 90 and mx <= iv_s * 3 else ("FAIR" if on_target >= 70 else "POOR")
            print(f"    [{status}] {tag}: {on_target:.0f}% on-target (iv {iv_s:.1f}s), "
                  f"median {p50:.2f}s, worst gap {mx:.1f}s, ~{lost} samples lost")
        overall = statistics.mean(w[0] for w in worst)
        print(f"\n    Overall on-target: {overall:.0f}%   "
              f"(90%+ = steady cadence, <70% = irregular / stalling)")


def metric_freshness(watch_s: int) -> None:
    section(f"C.  EDGE WRITE LATENCY / FRESHNESS  (polling {watch_s}s)")
    if watch_s <= 0:
        print("  (skipped — pass --watch N to sample live freshness)"); return
    lags = []
    t_end = time.time() + watch_s
    while time.time() < t_end:
        con = _con()
        row = con.execute("SELECT MAX(ts_utc) m FROM historian_readings").fetchone()
        con.close()
        newest = _parse(row[0]) if row and row[0] else None
        if newest:
            lag = (datetime.now(timezone.utc) - newest).total_seconds()
            lags.append(lag)
        time.sleep(1.0)
    if lags:
        print(f"  newest-row lag behind now: min={min(lags):.2f}s  "
              f"median={statistics.median(lags):.2f}s  max={max(lags):.2f}s  (n={len(lags)})")
        med = statistics.median(lags)
        verdict = "GOOD (<2s)" if med < 2 else ("FAIR (2-5s)" if med < 5 else "POOR (>5s — data lagging)")
        print(f"  VERDICT: {verdict}")
        print("  note: this is edge WRITE latency. Chart draw adds the frontend "
              "refresh (~gateway interval) on top.")
    else:
        print("  (no rows sampled)")


def metric_startup() -> None:
    section("D.  STARTUP — time to first durable row + stability signature")
    log = next((p for p in LOG_CANDIDATES if os.path.exists(p)), None)
    if not log:
        print("  (backend.log not found — start the packaged app to generate it)"); return
    print(f"  log: {log}  ({os.path.getsize(log) // 1024} KB)")
    # read only the tail (last ~2 MB) — the log can be large
    with open(log, "rb") as f:
        f.seek(max(0, os.path.getsize(log) - 2_000_000))
        tail = f.read().decode("utf-8", "replace")

    first_rows = re.findall(r"first-row gateway=(\S+) boot_to_first_row=([\d.]+)s", tail)
    if first_rows:
        print("\n  boot -> first durable row (the true 'time until data'):")
        for gw, secs in first_rows[-5:]:
            s = float(secs)
            v = "GOOD" if s < 5 else ("FAIR" if s < 15 else "SLOW")
            print(f"    [{v}] {gw}: {s:.2f}s")
    else:
        print("\n  no 'first-row' metric in the log tail yet — this build adds it; "
              "restart the app once to capture it.")

    kills = tail.count("event loop stale for")
    stalls = len(re.findall(r"gateway \S+ stalled \d+s", tail))
    boots = tail.count("startup_event fired")
    print(f"\n  stability signature (log tail):")
    print(f"    backend boots        : {boots}")
    print(f"    event-loop-stale kills: {kills}   <- each one is a 'running but no data' window")
    print(f"    gateway stalls        : {stalls}")
    if kills == 0 and stalls == 0:
        print("    VERDICT: STABLE — no loop-stall kills, no gateway stalls in this window.")
    else:
        print(f"    VERDICT: UNSTABLE — {kills} process kills + {stalls} gateway stalls. "
              "Each kill blanks collection until respawn + auto-resume.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=int, default=5, help="analysis window in minutes (default 5)")
    ap.add_argument("--watch", type=int, default=0, help="live freshness poll duration in seconds")
    args = ap.parse_args()

    print("TrustNode — collection & chart-update metrics")
    print(f"db: {DB}")
    if not os.path.exists(DB):
        print("app-store DB not found."); return 0
    metric_cadence_and_loss(args.window)
    metric_freshness(args.watch)
    metric_startup()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
