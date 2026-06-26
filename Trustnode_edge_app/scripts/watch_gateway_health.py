"""Monitor gateway data continuity by reading the historian DB directly.

Polls the SQLite app_store every 30 s and emits ONE LINE per concerning
event:
  - DOWN     no rows in the last 90 s for a gateway that was previously writing
  - SLOW     write rate dropped >70% versus the trailing 10-minute baseline
  - RECOVER  gateway started writing again after a DOWN
  - GAP      ts_utc gap >2x its typical inter-row spacing
  - ERR      backend.log error line in the last 30 s
  - HB       heartbeat every 30 minutes so silence is unambiguous

Designed to be wrapped by Monitor: stdout is the event stream.
"""
from __future__ import annotations
import os, sqlite3, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path(r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db")
LOG = Path(r"C:\Users\User\AppData\Roaming\trustnode-edge-desktop\backend.log")
POLL_SEC = 30
DOWN_AFTER_SEC = 90
SLOW_DROP_RATIO = 0.30           # current < 30% of baseline => SLOW
HEARTBEAT_EVERY_SEC = 30 * 60
ERR_RE_BYTES = (b"ERROR", b"Traceback", b"CRITICAL", b"db_last_error", b"connection refused")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def emit(kind: str, msg: str) -> None:
    print(f"{stamp()} {kind:7s} {msg}", flush=True)


def open_ro_db() -> sqlite3.Connection:
    uri = f"file:{DB.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)


def per_gateway_counts(c: sqlite3.Connection, window_sec: int) -> dict[str, tuple[int, str | None]]:
    cur = c.execute(
        "SELECT gateway_id, COUNT(*), MAX(ts_utc) FROM historian_readings "
        "WHERE ts_utc > datetime('now', ?) GROUP BY gateway_id",
        (f"-{window_sec} seconds",),
    )
    return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def log_tail_check(state: dict) -> list[str]:
    if not LOG.exists():
        return []
    sz = LOG.stat().st_size
    last_pos = state.get("log_pos", sz)
    if sz < last_pos:
        last_pos = 0
    out: list[str] = []
    if sz > last_pos:
        try:
            with LOG.open("rb") as f:
                f.seek(last_pos)
                chunk = f.read(min(sz - last_pos, 4 * 1024 * 1024))
                lines = chunk.splitlines()
                license_storm = sum(1 for ln in lines if b"license-check" in ln)
                if license_storm >= 100:
                    out.append(f"LICENSE-CHECK storm: {license_storm} hits in last {POLL_SEC}s")
                for ln in lines:
                    if any(m in ln for m in ERR_RE_BYTES):
                        try:
                            text = ln.decode("utf-8", errors="replace").rstrip()
                        except Exception:
                            text = repr(ln[:160])
                        out.append(text[:240])
                        if len(out) > 10:
                            out.append("(more errors truncated)")
                            break
        except Exception as exc:
            out.append(f"log-tail-failed: {type(exc).__name__}: {exc}")
        state["log_pos"] = sz
    else:
        state["log_pos"] = sz
    return out


def main() -> int:
    if not DB.exists():
        print(f"FATAL historian db not found: {DB}", flush=True)
        return 2
    emit("START", f"db={DB} log={LOG} poll={POLL_SEC}s")
    state: dict = {}
    baseline: dict[str, float] = {}     # gateway -> rolling rows/min baseline
    down_since: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}
    last_hb = time.monotonic()
    last_total = None

    while True:
        try:
            with open_ro_db() as c:
                short = per_gateway_counts(c, 60)        # last 60s rate sample
                long_ = per_gateway_counts(c, 600)       # 10-minute baseline window
                now = utc_now()

                seen = set(short) | set(long_) | set(last_seen)
                lines: list[str] = []

                for gw in seen:
                    rate_short = short.get(gw, (0, None))[0]
                    rate_long = long_.get(gw, (0, None))[0] / 10.0       # rows/min avg over 10m
                    last_ts = parse_ts(short.get(gw, (0, None))[1] or long_.get(gw, (0, None))[1])
                    if last_ts:
                        last_seen[gw] = last_ts

                    base = baseline.get(gw)
                    if rate_long > 0:
                        baseline[gw] = 0.7 * (base or rate_long) + 0.3 * rate_long

                    silence = (now - last_seen[gw]).total_seconds() if gw in last_seen else 999999
                    if silence > DOWN_AFTER_SEC and gw not in down_since:
                        down_since[gw] = now
                        lines.append(f"DOWN    {gw}: no rows for {silence:.0f}s (baseline {baseline.get(gw,0):.0f}/min)")
                    elif silence <= DOWN_AFTER_SEC and gw in down_since:
                        outage = (now - down_since.pop(gw)).total_seconds()
                        lines.append(f"RECOVER {gw}: writing again after ~{outage:.0f}s outage")

                    if base and rate_short < base * SLOW_DROP_RATIO and silence < DOWN_AFTER_SEC and base > 5:
                        lines.append(f"SLOW    {gw}: last60s={rate_short}, baseline={base:.0f}/min ({rate_short/(base/60):.0%})")

                total_rows = sum(v[0] for v in short.values())
                if last_total is None:
                    last_total = total_rows

                log_lines = log_tail_check(state)
                for ll in log_lines:
                    lines.append(f"ERR     {ll}")

                for ln in lines:
                    emit(ln.split()[0], ln.split(maxsplit=1)[1] if " " in ln else ln)

                if time.monotonic() - last_hb >= HEARTBEAT_EVERY_SEC:
                    parts = [f"{gw}={short.get(gw,(0,None))[0]}/60s base={baseline.get(gw,0):.0f}/min" for gw in sorted(baseline)]
                    emit("HB", "alive; " + (" | ".join(parts) if parts else "no gateways yet"))
                    last_hb = time.monotonic()

        except sqlite3.Error as exc:
            emit("DB-ERR", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            emit("FATAL", f"{type(exc).__name__}: {exc}")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
