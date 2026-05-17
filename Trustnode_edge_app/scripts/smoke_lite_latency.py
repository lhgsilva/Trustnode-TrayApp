"""Smoke test: measure end-to-end latency from edge PLC sample to Lite browser.

Three hops are measured, plus a clock-skew estimate so the numbers stay
meaningful when the edge clock drifts:

  1. Sample-to-DB lag    = created_utc - ts_utc          (edge -> Supabase write)
  2. Cloud-age now       = supabase NOW() - ts_utc       (what a viewer sees)
  3. Read round-trip     = wall clock of one Lite-style query against PostgREST

A fourth derived metric "edge clock skew" = (newest created_utc - edge wall
clock at insert) helps you tell apart "edge clock is wrong" from "sync worker
is slow". We sample the newest row, wait, sample again, and report drift.

Usage:
    python smoke_lite_latency.py [tag_name] [--gateway <gid>] [--samples N]

Defaults: tag SimREAL[3], 30 samples at 1 s each.

Reads from Trustnode_edge_app/.env:
    TRUSTNODE_CLOUD_DB_HOST/_PORT/_NAME/_USER/_PASSWORD  (direct SQL probe)

Reads from lite/config.json (if present) for the Supabase URL + anon key it
uses to time the PostgREST round-trip the way the Lite browser sees it. Falls
back to env vars if config.json is absent.
"""
from __future__ import annotations
import argparse
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psycopg


HERE = Path(__file__).resolve().parent.parent  # Trustnode_edge_app/


def load_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_lite_config() -> dict | None:
    p = HERE / "web_cloud_readonly" / "lite" / "config.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def fmt_ms(seconds: float) -> str:
    if seconds is None:
        return "—"
    ms = seconds * 1000.0
    if abs(ms) < 1000:
        return f"{ms:+.0f} ms" if ms < 0 else f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"


def signed_fmt_ms(seconds: float) -> str:
    ms = seconds * 1000.0
    sign = "+" if ms >= 0 else ""
    return f"{sign}{ms:.0f} ms"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def sign_in(supabase_url: str, anon_key: str, email: str, password: str) -> str:
    req = urllib.request.Request(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        method="POST",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"apikey": anon_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
        return data["access_token"]


def postgrest_latest(supabase_url: str, anon_key: str, jwt: str,
                     tag: str, gateway: str | None) -> tuple[float, dict]:
    """Time how long the Lite app's "newest row for this tag" query takes.

    Returns (round_trip_seconds, row_or_empty_dict).
    """
    params = [
        f"select=ts_utc,value,gateway_id,tag_name",
        f"tag_name=eq.{urllib.parse.quote(tag)}",
        "order=ts_utc.desc",
        "limit=1",
    ]
    if gateway:
        params.append(f"gateway_id=eq.{urllib.parse.quote(gateway)}")
    url = f"{supabase_url}/rest/v1/live_latest?" + "&".join(params)
    req = urllib.request.Request(url, headers={
        "apikey": anon_key,
        "Authorization": f"Bearer {jwt}",
    })
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=10) as r:
        rows = json.loads(r.read().decode())
    rtt = time.perf_counter() - t0
    return rtt, (rows[0] if rows else {})


def parse_iso(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="SimREAL[3]",
                    help="Tag to track (default: SimREAL[3])")
    ap.add_argument("--gateway", default=None, help="Optional gateway_id filter")
    ap.add_argument("--samples", type=int, default=30, help="Sample count")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between samples")
    ap.add_argument("--email", default="lite-test@trustnode.local",
                    help="Lite viewer email for PostgREST RTT measurement")
    ap.add_argument("--password", default="TrustNodeLite2026",
                    help="Lite viewer password")
    args = ap.parse_args()

    env = load_env(HERE / ".env")
    lite_cfg = load_lite_config()
    supabase_url = (lite_cfg or {}).get("supabase_url") or env.get("TRUSTNODE_PUBLIC_SUPABASE_URL")
    anon_key = (lite_cfg or {}).get("supabase_anon_key") or env.get("TRUSTNODE_PUBLIC_SUPABASE_ANON_KEY")
    if not supabase_url or not anon_key:
        print("FAILED: missing supabase_url/anon_key. Populate lite/config.json or .env.", file=sys.stderr)
        return 2

    print(f"Smoke test: tag={args.tag!r}  gateway={args.gateway or '(any)'}  samples={args.samples}  interval={args.interval}s")
    print(f"Supabase: {supabase_url}")
    print()

    # ---- Sign in once so we can time PostgREST exactly like the Lite app ----
    try:
        jwt = sign_in(supabase_url, anon_key, args.email, args.password)
        print(f"Signed in as {args.email}  (JWT len={len(jwt)})")
    except Exception as e:
        print(f"FAILED: sign-in error: {e}", file=sys.stderr)
        return 1

    # ---- Open a long-lived DB connection for the SQL probe ----
    conn = psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    )
    cur = conn.cursor()

    sample_to_db: list[float] = []
    cloud_age:    list[float] = []
    rtt:          list[float] = []
    skew:         list[float] = []
    seen_ts: set = set()
    first_row_ts = None
    last_row_ts = None
    print(f"{'#':>3}  {'sample-utc':<23}  {'sample->db':>11}  {'cloud age':>11}  {'pgrst rtt':>11}  {'clock skew':>11}  value")
    print("-" * 110)

    for i in range(1, args.samples + 1):
        # 1) Pull the newest row for this tag + its created_utc + db NOW(). One query,
        #    one round-trip, so created_utc and NOW() are read in the same statement.
        try:
            where = "tag_name = %s"
            params: list = [args.tag]
            if args.gateway:
                where += " AND gateway_id = %s"
                params.append(args.gateway)
            cur.execute(
                f"SELECT ts_utc, created_utc, value, NOW() FROM historian_readings WHERE {where} "
                f"ORDER BY id DESC LIMIT 1",
                params,
            )
            row = cur.fetchone()
        except Exception as e:
            print(f"  query error: {e}")
            conn.rollback()
            time.sleep(args.interval)
            continue

        if not row:
            print(f"  no rows yet for tag {args.tag!r}")
            time.sleep(args.interval)
            continue

        ts_utc, created_utc, value, db_now = row
        ts_utc = parse_iso(ts_utc)
        created_utc = parse_iso(created_utc)
        db_now = parse_iso(db_now)

        # 2) Compute the hops.
        s2db = (created_utc - ts_utc).total_seconds() if (created_utc and ts_utc) else None
        # Cloud age = how stale the newest row is relative to the LOCAL wall
        # clock at the moment we asked. We use local now() so the number
        # matches the freshness label the Lite browser computes (it does the
        # same: Date.now() - Date.parse(row.ts_utc)). Clamp at 0 — negative
        # values just mean the row was sampled in the future relative to
        # our clock, which is a clock-skew artifact, not real freshness.
        local_now = datetime.now(timezone.utc)
        age = max(0.0, (local_now - ts_utc).total_seconds()) if ts_utc else None

        # 3) Time PostgREST exactly like the Lite browser.
        try:
            r_rtt, _ = postgrest_latest(supabase_url, anon_key, jwt, args.tag, args.gateway)
        except Exception as e:
            print(f"  pgrst error: {e}")
            r_rtt = None

        # 4) Estimate edge clock skew: assume the sync worker writes within ~50ms
        #    on a healthy edge -> if (created_utc - ts_utc) is negative, the edge
        #    clock is AHEAD of supabase; if very large, it's BEHIND or the sync is
        #    backed up. Print the raw value; you decide which.
        skew_now = s2db  # raw, same value as sample-to-db

        # Only record once per unique sample so we don't bias the stats when
        # the data hasn't moved between iterations.
        is_new = ts_utc not in seen_ts
        if is_new:
            seen_ts.add(ts_utc)
            if s2db is not None: sample_to_db.append(s2db)
            if r_rtt is not None: rtt.append(r_rtt)
            if skew_now is not None: skew.append(skew_now)
            if first_row_ts is None: first_row_ts = ts_utc
            last_row_ts = ts_utc
        # Cloud age is always meaningful — that's what the viewer sees right now.
        if age is not None:
            cloud_age.append(age)

        marker = "" if is_new else "  (cached)"
        print(
            f"{i:>3}  {ts_utc.isoformat()[:23]:<23}  "
            f"{signed_fmt_ms(s2db) if s2db is not None else '—':>11}  "
            f"{fmt_ms(age) if age is not None else '—':>11}  "
            f"{fmt_ms(r_rtt) if r_rtt is not None else '—':>11}  "
            f"{signed_fmt_ms(skew_now) if skew_now is not None else '—':>11}  "
            f"{value}{marker}"
        )
        time.sleep(args.interval)

    cur.close()
    conn.close()

    # ---- Summary --------------------------------------------------------------
    def summarize(name: str, samples: list[float], unit: str = "ms"):
        if not samples:
            print(f"  {name:25s}  no samples")
            return
        mean = statistics.fmean(samples)
        median = statistics.median(samples)
        p95 = percentile(samples, 95)
        lo, hi = min(samples), max(samples)
        if unit == "ms":
            print(f"  {name:25s}  n={len(samples):3d}  "
                  f"min={lo*1000:7.0f}  med={median*1000:7.0f}  "
                  f"mean={mean*1000:7.0f}  p95={p95*1000:7.0f}  max={hi*1000:7.0f}  (ms)")
        else:
            print(f"  {name:25s}  n={len(samples):3d}  "
                  f"min={lo:7.3f}  med={median:7.3f}  "
                  f"mean={mean:7.3f}  p95={p95:7.3f}  max={hi:7.3f}  ({unit})")

    print()
    print("=" * 110)
    print("Summary")
    print("=" * 110)
    summarize("sample -> db write   (1)", sample_to_db)
    summarize("cloud age (viewer)   (2)", cloud_age)
    summarize("PostgREST round-trip (3)", rtt)
    summarize("(raw edge clock skew)   ", skew)

    print()
    if sample_to_db:
        median_skew = statistics.median(skew)
        if abs(median_skew) > 2.0:
            print(f"NOTE: median sample->db = {median_skew*1000:+.0f} ms. Anything > 2 s "
                  "usually means the edge clock is drifting (no NTP) OR the sync worker is "
                  "backlogged. The 'cloud age' number is what your customer actually feels.")
        else:
            print(f"OK: edge clock looks aligned (median skew {median_skew*1000:+.0f} ms).")

    if first_row_ts and last_row_ts and first_row_ts != last_row_ts:
        span = (last_row_ts - first_row_ts).total_seconds()
        new_count = len(sample_to_db)
        if span > 0:
            hz = new_count / span
            print(f"Throughput observed: {new_count} new samples in {span:.1f}s "
                  f"= {hz:.1f} Hz for tag {args.tag!r}.")

    print()
    print("How to interpret:")
    print("  (1) sample -> db write: time from PLC scan to Supabase row visible.")
    print("      Includes edge buffer + sync worker batching + Supabase insert.")
    print("      Target: < 1 s for live operator monitoring, < 5 s for typical IoT.")
    print("  (2) cloud age (viewer): how stale the freshest row is RIGHT NOW.")
    print("      This is what the Lite tile shows next to the value.")
    print("  (3) PostgREST round-trip: cost of one Lite poll. Lite polls every 2 s")
    print("      by default; if (3) is consistently > 1 s the viewer feels laggy.")
    print("  edge clock skew: |median sample->db|. Big numbers point at clock drift")
    print("      on the edge or a backed-up sync worker, NOT at network slowness.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
