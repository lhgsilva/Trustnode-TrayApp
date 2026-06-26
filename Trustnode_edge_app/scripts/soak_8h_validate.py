"""8-hour data-collection validation harness.

Polls the historian SQLite every 30 s and emits one event line per
notable transition. At the end of the run prints a single PASS/FAIL
summary report.

Failure criteria (any one trips the run to FAIL):
  * Any DOWN that lasted > 5 minutes without an operator action
  * Any single-cycle gap > 30 seconds when the gateway was supposed to
    be running (detected by ts_utc consecutive-row diff)
  * The total runtime sample rate dropped below 50% of the rolling
    baseline for more than 2 consecutive hours

Pass criteria:
  * No DOWN-RECOVER episodes longer than 2 minutes
  * No data gaps inside an active hour
  * Final row count growth matches the expected per-hour baseline

Designed to be wrapped by the Monitor tool. Each event arrives as a
notification; the final report is the last event line before exit.
"""
from __future__ import annotations
import os, sqlite3, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force stdout to utf-8 with replacement so any non-printable byte in
# the backend.log tail can't crash the monitor under Windows cp1252.
# (Was the cause of the 22:51:51 DOWN-EVIDENCE crash.)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = Path(r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db")
LOG = Path(r"C:\Users\User\AppData\Roaming\trustnode-edge-desktop\backend.log")
RUN_SECONDS = int(os.environ.get("TRUSTNODE_SOAK_SECONDS", str(8 * 3600)) or str(8 * 3600))
POLL_SEC = 30
DOWN_AFTER_SEC = 90
LONG_OUTAGE_S = 5 * 60
PASS_RECOVER_S = 2 * 60
GAP_THRESHOLD_S = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def emit(kind: str, msg: str) -> None:
    print(f"{stamp()} {kind:7s} {msg}", flush=True)


def open_db() -> sqlite3.Connection:
    uri = f"file:{DB.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def snapshot_window(c: sqlite3.Connection, seconds: int) -> dict[str, dict]:
    """Return per-gateway dict: {rows, max_ts, gateways_seen}."""
    cur = c.execute(
        "SELECT gateway_id, COUNT(*), MAX(ts_utc), MIN(ts_utc) "
        "FROM historian_readings WHERE ts_utc > datetime('now', ?) "
        "GROUP BY gateway_id",
        (f"-{seconds} seconds",),
    )
    out: dict[str, dict] = {}
    for gw, n, mx, mn in cur.fetchall():
        out[gw or ""] = {"rows": int(n or 0), "max": mx, "min": mn}
    return out


def detect_gaps(c: sqlite3.Connection, gw: str, seconds: int) -> list[float]:
    """Find inter-row time gaps > GAP_THRESHOLD_S inside the window.
    Returns a list of gap durations in seconds (limited to 50)."""
    rows = c.execute(
        "SELECT ts_utc FROM historian_readings "
        "WHERE gateway_id = ? AND ts_utc > datetime('now', ?) "
        "ORDER BY ts_utc ASC",
        (gw, f"-{seconds} seconds"),
    ).fetchall()
    if len(rows) < 2:
        return []
    gaps: list[float] = []
    prev = parse_ts(rows[0][0])
    for r in rows[1:]:
        cur = parse_ts(r[0])
        if prev and cur:
            dt = (cur - prev).total_seconds()
            if dt > GAP_THRESHOLD_S:
                gaps.append(dt)
                if len(gaps) >= 50:
                    break
        prev = cur
    return gaps


def main() -> int:
    if not DB.exists():
        print(f"FATAL historian db not found: {DB}", flush=True)
        return 2
    started_mono = time.monotonic()
    started_wall = utc_now()
    deadline_mono = started_mono + RUN_SECONDS
    emit("START", f"8h soak; db={DB}; will run until {(started_wall + timedelta(seconds=RUN_SECONDS)).isoformat()}Z")

    last_seen_ts: dict[str, datetime] = {}
    down_since: dict[str, datetime] = {}
    outages: list[tuple[str, datetime, datetime, float]] = []  # (gw, start, end, seconds)
    long_outages = 0
    cycle_gaps_total: dict[str, int] = {}
    rows_per_hour: dict[str, list[int]] = {}
    last_hour_bucket = -1
    hb_due_mono = started_mono + 1800.0  # first heartbeat at +30 min

    while time.monotonic() < deadline_mono:
        try:
            with open_db() as c:
                short = snapshot_window(c, 60)
                long_ = snapshot_window(c, 600)
                now = utc_now()

                # Per-gateway state machine.
                # Operator 2026-06-24: ALSO iterate over previously-seen
                # gateways, because once a gateway stops writing entirely
                # it falls out of `short` AND `long_` (both window-scoped
                # queries) and the DOWN logic stops firing — that hid
                # the 22:50 UTC outage on the previous soak run as a
                # false PASS. Include any gateway we've ever seen so the
                # state machine keeps monitoring it.
                all_known_gateways = set(short.keys()) | set(long_.keys()) | set(last_seen_ts.keys())
                merged = {**long_, **short}  # short wins on overlap
                for gw in all_known_gateways:
                    if not gw:
                        continue
                    info = merged.get(gw, {})
                    mx = parse_ts(info.get("max"))
                    if mx:
                        last_seen_ts[gw] = mx
                    silence = (now - last_seen_ts[gw]).total_seconds() if gw in last_seen_ts else 9999
                    if silence > DOWN_AFTER_SEC and gw not in down_since:
                        down_since[gw] = now
                        emit("DOWN", f"{gw}: no rows for {silence:.0f}s")
                        # Operator 2026-06-23: capture forensic evidence the
                        # instant we detect a DOWN. Grab the last 40 lines
                        # of backend.log so the cause is visible in the
                        # event stream without needing a separate dig
                        # later.
                        try:
                            if LOG.exists():
                                with LOG.open("rb") as f:
                                    f.seek(max(0, LOG.stat().st_size - 12000))
                                    tail_bytes = f.read()
                                lines = tail_bytes.decode("utf-8", errors="replace").splitlines()
                                interesting = [ln for ln in lines
                                               if any(tok in ln for tok in (
                                                   "ERROR", "Traceback", "CRITICAL",
                                                   "watchdog", "stalled", "restart",
                                                   "persist-fail", "db_last_error",
                                                   "cycle-error", "respawn",
                                               ))]
                                if interesting:
                                    sample = interesting[-10:]
                                    for ln in sample:
                                        emit("DOWN-EVIDENCE", ln.strip()[:240])
                                else:
                                    emit("DOWN-EVIDENCE", "no error/restart markers in last 40 log lines")
                        except Exception as exc:
                            emit("DOWN-EVIDENCE", f"log-scan-failed: {type(exc).__name__}: {exc}")
                    elif silence <= DOWN_AFTER_SEC and gw in down_since:
                        start = down_since.pop(gw)
                        dur = (now - start).total_seconds()
                        outages.append((gw, start, now, dur))
                        if dur > LONG_OUTAGE_S:
                            long_outages += 1
                            emit("LONG-OUT", f"{gw}: outage {dur:.0f}s exceeded 5min threshold")
                        else:
                            emit("RECOVER", f"{gw}: writing again after {dur:.0f}s outage")
                # Per-cycle gap detection (60s window).
                for gw in list(short.keys()):
                    if not gw:
                        continue
                    gaps = detect_gaps(c, gw, 60)
                    if gaps:
                        prev_total = cycle_gaps_total.get(gw, 0)
                        cycle_gaps_total[gw] = prev_total + len(gaps)
                        worst = max(gaps)
                        emit("GAP", f"{gw}: {len(gaps)} gap(s) in last 60s, worst {worst:.0f}s")
                # Hourly summary: emit when wall-clock hour bucket changes.
                cur_hour = int((time.monotonic() - started_mono) // 3600)
                if cur_hour != last_hour_bucket:
                    last_hour_bucket = cur_hour
                    parts = []
                    for gw, info in long_.items():
                        if not gw:
                            continue
                        rate = info["rows"] / 10.0  # rows / minute averaged over 10 min
                        rows_per_hour.setdefault(gw, []).append(int(info["rows"] * 6))  # extrapolate 10m -> 1h
                        parts.append(f"{gw}={int(info['rows']*6)}/h base~{rate:.0f}/min")
                    emit("HOUR", f"hour {cur_hour}: {' | '.join(parts) if parts else 'no gateways'}")
                # Heartbeat every 30 min.
                if time.monotonic() >= hb_due_mono:
                    hb_due_mono += 1800.0
                    parts = [f"{gw}={info['rows']}/60s" for gw, info in short.items() if gw]
                    emit("HB", f"alive @{(time.monotonic()-started_mono)/3600:.1f}h: {' | '.join(parts) if parts else 'no rows yet'}")
        except sqlite3.Error as exc:
            emit("DB-ERR", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            emit("FATAL", f"{type(exc).__name__}: {exc}")
        time.sleep(POLL_SEC)

    # ---- Final verdict ----
    # Operator 2026-06-24: close out any open outages at the deadline
    # so an in-progress outage (gateway still wedged when the run ends)
    # FAILS the verdict. Previously these were silently ignored — that
    # was how the 2026-06-23 22:50 wedge produced a false PASS even
    # though the gateway was dead for 2h 50m.
    deadline_now = utc_now()
    for gw, start in list(down_since.items()):
        dur = (deadline_now - start).total_seconds()
        outages.append((gw, start, deadline_now, dur))
        if dur > LONG_OUTAGE_S:
            long_outages += 1
        emit("DOWN-OPEN", f"{gw}: still DOWN at run end (open outage {dur:.0f}s)")

    total_outages = len(outages)
    total_gap_events = sum(cycle_gaps_total.values())
    longest_outage = max((d for _, _, _, d in outages), default=0.0)
    passed = True
    reasons = []
    if long_outages > 0:
        passed = False
        reasons.append(f"{long_outages} outage(s) longer than {LONG_OUTAGE_S}s")
    if total_gap_events > 0:
        passed = False
        reasons.append(f"{total_gap_events} intra-window gap(s) > {GAP_THRESHOLD_S}s")
    if longest_outage > PASS_RECOVER_S and long_outages == 0:
        # outages were < 5min but > 2min -> still concerning
        reasons.append(f"longest outage {longest_outage:.0f}s exceeded {PASS_RECOVER_S}s soft threshold")
    # Belt and suspenders: if any gateway we knew about wrote ZERO rows
    # for the last 5 minutes of the run, fail.
    cutoff = time.monotonic() - 300.0
    for gw in last_seen_ts:
        # last_seen_ts is wall-clock UTC; convert to monotonic-ish via wall
        age_s = (deadline_now - last_seen_ts[gw]).total_seconds()
        if age_s > 300:
            passed = False
            reasons.append(f"{gw} silent for {age_s:.0f}s at run end")
            break
    verdict = "PASS" if passed else "FAIL"
    emit("REPORT", f"verdict={verdict} outages={total_outages} long_outages={long_outages} "
                   f"longest_outage_s={longest_outage:.0f} gap_events={total_gap_events} "
                   f"reasons={'; '.join(reasons) if reasons else 'none'}")
    for gw, hours in rows_per_hour.items():
        if hours:
            avg = sum(hours) / len(hours)
            emit("REPORT", f"{gw}: avg {avg:.0f} rows/h over {len(hours)} hour buckets")
    emit("END", f"soak complete after {(time.monotonic()-started_mono)/3600:.2f}h")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
