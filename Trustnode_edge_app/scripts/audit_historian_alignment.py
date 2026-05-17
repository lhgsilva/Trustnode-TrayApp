"""Audit-grade timestamp alignment report for the TrustNode historian.

Answers:
  1. Per-tag sampling rate and spacing — does the historian record at the
     cadence the gateway is configured for?
  2. Pairwise alignment — when an auditor asks "what was tag A and tag B
     at time T?", how close together (in ms) are the closest samples of
     each across the audit window?
  3. Gap analysis — how many windows are missing data, and how big are
     they relative to the gateway's poll period?

Tolerance for "aligned": half the slower gateway's poll period. So two
1-Hz gateways are aligned within 500ms; a 1-Hz tag vs a 2-Hz tag is
aligned within 500ms (driven by the slower one).

Output:
  - human-readable report to stdout
  - JSON summary at ./audit_historian_alignment_<UTC>.json next to this script

Run:
  python audit_historian_alignment.py
  python audit_historian_alignment.py --hours 4 --tags 'siemens_angle_REAL' 'SimREAL[2]'
"""
from __future__ import annotations

import argparse
import io as _io
import sys as _sys
# Force UTF-8 stdout so the unicode glyphs in the report (·, →, etc.)
# don't crash on Windows code pages.
try:
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    _sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8", line_buffering=True)
except Exception:
    pass
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--hours", type=float, default=1.0,
                   help="Audit window in hours, ending now (default 1.0)")
    p.add_argument("--tags", nargs="*",
                   help="Tag substrings to include. If omitted, the top 6 by sample count.")
    p.add_argument("--max-pairs", type=int, default=6,
                   help="Cap on pairwise comparisons to keep output readable")
    return p.parse_args()


def fmt_ms(v: float | int | None) -> str:
    if v is None or not math.isfinite(v):
        return "-"
    return f"{v:>7.1f} ms"


def fmt_count(v: int) -> str:
    return f"{v:>6d}"


def to_epoch_ms(ts: str) -> float | None:
    if not ts:
        return None
    raw = ts.replace("Z", "+00:00")
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def per_tag_stats(rows: list[tuple[float, float | None]]) -> dict:
    """Returns sample count, time span, and median/p95/p99 inter-sample deltas."""
    if len(rows) < 2:
        return {
            "count": len(rows),
            "first_ms": rows[0][0] if rows else None,
            "last_ms": rows[-1][0] if rows else None,
            "median_dt_ms": None,
            "p95_dt_ms": None,
            "p99_dt_ms": None,
            "max_dt_ms": None,
            "gaps_over_2x": 0,
        }
    rows.sort(key=lambda r: r[0])
    deltas = [rows[i][0] - rows[i - 1][0] for i in range(1, len(rows)) if rows[i][0] > rows[i - 1][0]]
    deltas.sort()
    n = len(deltas)
    median = statistics.median(deltas) if deltas else None
    p95 = deltas[max(0, int(0.95 * n) - 1)] if deltas else None
    p99 = deltas[max(0, int(0.99 * n) - 1)] if deltas else None
    mx = deltas[-1] if deltas else None
    gaps_over_2x = sum(1 for d in deltas if median and d > 2 * median)
    return {
        "count": len(rows),
        "first_ms": rows[0][0],
        "last_ms": rows[-1][0],
        "median_dt_ms": median,
        "p95_dt_ms": p95,
        "p99_dt_ms": p99,
        "max_dt_ms": mx,
        "gaps_over_2x": gaps_over_2x,
    }


def pairwise_alignment(a_rows: list[tuple[float, float | None]],
                       b_rows: list[tuple[float, float | None]],
                       tolerance_ms: float) -> dict:
    """For each sample of A, find the closest B sample. Returns nearest-
    distance stats + alignment fraction within tolerance.
    """
    if not a_rows or not b_rows:
        return {"a_count": len(a_rows), "b_count": len(b_rows), "tolerance_ms": tolerance_ms,
                "median_gap_ms": None, "p95_gap_ms": None, "p99_gap_ms": None,
                "aligned_pct": 0.0, "matched_pairs": 0}

    b_times = sorted(r[0] for r in b_rows)
    nearest_gaps = []
    matched = 0
    import bisect
    for ta, _ in a_rows:
        idx = bisect.bisect_left(b_times, ta)
        candidates = []
        if idx < len(b_times):
            candidates.append(b_times[idx])
        if idx > 0:
            candidates.append(b_times[idx - 1])
        if not candidates:
            continue
        nearest = min(candidates, key=lambda t: abs(t - ta))
        gap = abs(nearest - ta)
        nearest_gaps.append(gap)
        if gap <= tolerance_ms:
            matched += 1
    nearest_gaps.sort()
    n = len(nearest_gaps)
    median = statistics.median(nearest_gaps) if nearest_gaps else None
    p95 = nearest_gaps[max(0, int(0.95 * n) - 1)] if nearest_gaps else None
    p99 = nearest_gaps[max(0, int(0.99 * n) - 1)] if nearest_gaps else None
    return {
        "a_count": len(a_rows),
        "b_count": len(b_rows),
        "tolerance_ms": tolerance_ms,
        "matched_pairs": matched,
        "aligned_pct": (matched / len(a_rows) * 100.0) if a_rows else 0.0,
        "median_gap_ms": median,
        "p95_gap_ms": p95,
        "p99_gap_ms": p99,
        "max_gap_ms": nearest_gaps[-1] if nearest_gaps else None,
    }


def main():
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    print(f"\n=== TrustNode historian alignment audit ===")
    print(f"DB:      {db_path}")
    print(f"Window:  {start.isoformat()}  →  {end.isoformat()}  ({args.hours:.2f} h)\n")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Resolve target tags.
    if args.tags:
        like_clauses = " OR ".join([f"tag_name LIKE :p{i}" for i in range(len(args.tags))])
        params = {f"p{i}": f"%{t}%" for i, t in enumerate(args.tags)}
        params["start"] = start.strftime("%Y-%m-%d %H:%M:%S")
        params["end"] = end.strftime("%Y-%m-%d %H:%M:%S")
        tag_rows = conn.execute(
            f"""SELECT tag_name, COALESCE(gateway_id,'') AS gw, COUNT(*) AS n
                FROM historian_readings
                WHERE ts_utc >= :start AND ts_utc <= :end AND ({like_clauses})
                GROUP BY tag_name, gateway_id
                ORDER BY n DESC""",
            params,
        ).fetchall()
    else:
        tag_rows = conn.execute(
            """SELECT tag_name, COALESCE(gateway_id,'') AS gw, COUNT(*) AS n
               FROM historian_readings
               WHERE ts_utc >= :start AND ts_utc <= :end
               GROUP BY tag_name, gateway_id
               ORDER BY n DESC
               LIMIT 6""",
            {"start": start.strftime("%Y-%m-%d %H:%M:%S"),
             "end": end.strftime("%Y-%m-%d %H:%M:%S")},
        ).fetchall()

    if not tag_rows:
        sys.exit("No samples in the requested window.")

    print(f"Tags inspected:  {len(tag_rows)}")
    for r in tag_rows:
        print(f"   {r['tag_name'][:42]:<42s} | gw={r['gw'][:24]:<24s} | samples={r['n']}")
    print()

    # Pull (ts_ms, value) per tag.
    series: dict[tuple[str, str], list[tuple[float, float | None]]] = {}
    for r in tag_rows:
        key = (r["tag_name"], r["gw"])
        rows = conn.execute(
            """SELECT ts_utc, value FROM historian_readings
               WHERE tag_name = :tag AND COALESCE(gateway_id,'') = :gw
                 AND ts_utc >= :start AND ts_utc <= :end""",
            {"tag": r["tag_name"], "gw": r["gw"],
             "start": start.strftime("%Y-%m-%d %H:%M:%S"),
             "end": end.strftime("%Y-%m-%d %H:%M:%S")},
        ).fetchall()
        pts = []
        for row in rows:
            ms = to_epoch_ms(row["ts_utc"])
            if ms is None:
                continue
            pts.append((ms, row["value"]))
        pts.sort(key=lambda x: x[0])
        series[key] = pts

    # ── per-tag report ────────────────────────────────────────────────
    print("──── Per-tag sampling ────────────────────────────────────────────")
    print(f"{'Tag':<42s} {'count':>6s}  {'median':>10s}  {'p95':>10s}  {'p99':>10s}  {'max':>10s}  {'gaps>2x':>7s}")
    per_tag = {}
    for (tag, gw), pts in series.items():
        st = per_tag_stats(pts)
        per_tag[f"{tag}|{gw}"] = st
        print(f"{tag[:42]:<42s} {fmt_count(st['count'])}  "
              f"{fmt_ms(st['median_dt_ms']):>10s}  {fmt_ms(st['p95_dt_ms']):>10s}  "
              f"{fmt_ms(st['p99_dt_ms']):>10s}  {fmt_ms(st['max_dt_ms']):>10s}  "
              f"{fmt_count(st['gaps_over_2x']):>7s}")
    print()

    # ── pairwise alignment ─────────────────────────────────────────────
    print("──── Pairwise alignment (closest neighbour A → B) ────────────────")
    keys = list(series.keys())
    pair_count = 0
    pair_results = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if pair_count >= args.max_pairs:
                break
            ka, kb = keys[i], keys[j]
            # Tolerance = half the slower median sample interval.
            a_stat = per_tag.get(f"{ka[0]}|{ka[1]}", {})
            b_stat = per_tag.get(f"{kb[0]}|{kb[1]}", {})
            slower = max(a_stat.get("median_dt_ms") or 1000.0,
                         b_stat.get("median_dt_ms") or 1000.0)
            tol = slower / 2.0
            align = pairwise_alignment(series[ka], series[kb], tol)
            pair_results.append({"a": f"{ka[0]} @ {ka[1]}",
                                  "b": f"{kb[0]} @ {kb[1]}",
                                  **align})
            print(f"  A: {ka[0][:30]:<30s} ({ka[1][:18]})")
            print(f"  B: {kb[0][:30]:<30s} ({kb[1][:18]})")
            print(f"     tolerance={tol:.0f} ms · A samples={align['a_count']} · B samples={align['b_count']}")
            print(f"     matched within tolerance: {align['matched_pairs']} / {align['a_count']} "
                  f"({align['aligned_pct']:.1f} %)")
            print(f"     nearest-gap median {fmt_ms(align['median_gap_ms']).strip()}, "
                  f"p95 {fmt_ms(align['p95_gap_ms']).strip()}, "
                  f"p99 {fmt_ms(align['p99_gap_ms']).strip()}, "
                  f"max {fmt_ms(align['max_gap_ms']).strip()}")
            print()
            pair_count += 1
        if pair_count >= args.max_pairs:
            break

    # ── verdict ────────────────────────────────────────────────────────
    print("──── Verdict ──────────────────────────────────────────────────────")
    bad_pairs = [p for p in pair_results if p["aligned_pct"] < 95.0]
    bad_tags = [k for k, st in per_tag.items() if st["gaps_over_2x"] > max(2, st["count"] * 0.01)]
    if not bad_pairs and not bad_tags:
        print("PASS · historian records both per-tag cadence and pairwise alignment cleanly.")
    else:
        print("ATTENTION required:")
        for k in bad_tags:
            st = per_tag[k]
            print(f"  · {k}: {st['gaps_over_2x']} gaps > 2× median ({st['gaps_over_2x']/max(1,st['count'])*100:.2f}% of samples)")
        for p in bad_pairs:
            print(f"  · {p['a']} vs {p['b']}: only {p['aligned_pct']:.1f}% aligned within "
                  f"{p['tolerance_ms']:.0f} ms")
    print()

    out_path = Path(__file__).parent / f"audit_historian_alignment_{end.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps({
        "db": str(db_path),
        "window_utc": [start.isoformat(), end.isoformat()],
        "per_tag": per_tag,
        "pairs": pair_results,
    }, indent=2), encoding="utf-8")
    print(f"JSON written: {out_path}")


if __name__ == "__main__":
    main()
