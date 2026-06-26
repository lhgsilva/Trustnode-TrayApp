"""TrustNode external watchdog sidecar.

Runs as a separate OS process alongside trustnode-service.exe. Its
ONE job: make sure historian data is still being written. If the
backend stops writing for too long, kill it. Electron's existing
auto-respawn brings it back.

Design rules (deliberately strict):
  * No asyncio. No socket I/O. No PLC drivers. No Python deps that
    could themselves wedge.
  * Reads exactly ONE file (heartbeat.txt). Issues exactly ONE
    command (taskkill). Logs to ONE file (watchdog.log).
  * If the heartbeat file is missing/empty during boot, give the
    backend 5 minutes to come up before declaring it dead.
  * Once the heartbeat has been seen at least once, the staleness
    threshold drops to 90 seconds.
  * Self-throttle: after a kill, wait 60 s before checking again so
    Electron's respawn has time to land a new process.

Heartbeat file shape:
    <iso-utc-timestamp> <reason>\n
    e.g.  2026-06-24T11:30:42Z rows=32

Sidecar reads only the first line; ignores anything else.

Exit code:
    0   — never (sidecar runs forever)
    2   — fatal startup error (path resolution failed)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---- config ------------------------------------------------------------
# Heartbeat file location. Resolved relative to %APPDATA% by default so
# the sidecar finds the SAME file the backend writes. Env override for
# CI/dev runs.
def _resolve_heartbeat_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_HEARTBEAT_FILE", "").strip()
    if env_path:
        return Path(env_path)
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "trustnode-edge-desktop" / "heartbeat.txt"


HEARTBEAT_FILE = _resolve_heartbeat_path()
LOG_FILE = HEARTBEAT_FILE.parent / "watchdog.log"

# Threshold AFTER first heartbeat seen.
STALE_AFTER_SECONDS = float(os.environ.get("TRUSTNODE_WATCHDOG_STALE_S", "90") or "90")
# Grace period at sidecar startup, before any heartbeat has been seen.
BOOT_GRACE_SECONDS = float(os.environ.get("TRUSTNODE_WATCHDOG_BOOT_GRACE_S", "300") or "300")
# Cooldown after a kill — gives Electron time to respawn.
POST_KILL_COOLDOWN_S = float(os.environ.get("TRUSTNODE_WATCHDOG_POST_KILL_S", "60") or "60")
# Polling cadence.
POLL_INTERVAL_S = float(os.environ.get("TRUSTNODE_WATCHDOG_POLL_S", "15") or "15")
# Process name to kill on staleness.
BACKEND_PROCESS_NAME = os.environ.get("TRUSTNODE_BACKEND_PROCESS", "trustnode-service.exe")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    line = f"{_utc_now_iso()} {msg}\n"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    # Also print to stdout in case the sidecar is being run from a console.
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def _read_heartbeat_age_seconds() -> tuple[float, str]:
    """Returns (age_in_seconds, reason_string). Age is +inf if the
    file is missing or unreadable. Reason is the second token in the
    file's first line, or '' if missing."""
    try:
        if not HEARTBEAT_FILE.is_file():
            return (float("inf"), "missing")
        raw = HEARTBEAT_FILE.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return (float("inf"), "empty")
        first_line = raw.splitlines()[0]
        parts = first_line.split(maxsplit=1)
        ts_str = parts[0]
        reason = parts[1] if len(parts) > 1 else ""
        # Parse the ISO timestamp.
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return (max(0.0, age), reason)
    except Exception as exc:
        return (float("inf"), f"parse-fail:{type(exc).__name__}")


def _kill_backend() -> bool:
    """Force-kill the backend by image name. Returns True if taskkill
    reported success. Best-effort — we log the outcome either way."""
    try:
        # /F = force; /T = include child processes; /IM = by image name.
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", BACKEND_PROCESS_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            _log(f"KILL ok process={BACKEND_PROCESS_NAME} stdout={proc.stdout.strip()[:200]}")
            return True
        else:
            _log(f"KILL fail process={BACKEND_PROCESS_NAME} rc={proc.returncode} "
                 f"stderr={proc.stderr.strip()[:200]}")
            return False
    except subprocess.TimeoutExpired:
        _log(f"KILL timeout process={BACKEND_PROCESS_NAME}")
        return False
    except Exception as exc:
        _log(f"KILL exception {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    _log(f"START sidecar pid={os.getpid()} heartbeat_file={HEARTBEAT_FILE} "
         f"stale_after_s={STALE_AFTER_SECONDS} boot_grace_s={BOOT_GRACE_SECONDS} "
         f"poll_s={POLL_INTERVAL_S}")
    sidecar_started_mono = time.monotonic()
    ever_seen_heartbeat = False
    last_kill_mono = 0.0

    while True:
        try:
            time.sleep(POLL_INTERVAL_S)
            now_mono = time.monotonic()
            # Cooldown after a kill — give Electron time to respawn.
            if last_kill_mono and (now_mono - last_kill_mono) < POST_KILL_COOLDOWN_S:
                continue

            age, reason = _read_heartbeat_age_seconds()
            sidecar_uptime = now_mono - sidecar_started_mono

            if age == float("inf"):
                if sidecar_uptime < BOOT_GRACE_SECONDS:
                    # Still booting — log occasionally, don't kill.
                    if int(sidecar_uptime) % 30 == 0:
                        _log(f"WAIT boot grace: heartbeat {reason}, uptime={sidecar_uptime:.0f}s")
                    continue
                _log(f"STALE-NO-FILE heartbeat {reason}, uptime={sidecar_uptime:.0f}s — killing backend")
                _kill_backend()
                last_kill_mono = now_mono
                continue

            ever_seen_heartbeat = True
            if age > STALE_AFTER_SECONDS:
                _log(f"STALE heartbeat age={age:.0f}s > {STALE_AFTER_SECONDS:.0f}s last_reason={reason!r} "
                     f"— killing backend")
                _kill_backend()
                last_kill_mono = now_mono
                continue

            # Periodic OK log (every ~5 min) so the watchdog log shows
            # the sidecar is actually running.
            if int(now_mono) % 300 < POLL_INTERVAL_S:
                _log(f"OK heartbeat age={age:.0f}s reason={reason!r}")
        except KeyboardInterrupt:
            _log("SIGINT received — sidecar exiting cleanly")
            return 0
        except Exception as exc:
            _log(f"LOOP exception {type(exc).__name__}: {exc}")
            # Never let the sidecar itself die silently.
            try:
                time.sleep(5.0)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
