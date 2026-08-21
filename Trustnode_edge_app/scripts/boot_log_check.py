"""Boot-health check for the release gate (operator 2026-08-21).

Parses the LAST boot block of the desktop backend.log and asserts the SLOs
that guarantee the splash never shows "Backend service did not respond":

  * the Electron side logged "Backend health OK after N ms"  (boot succeeded)
  * N <= MAX_HEALTH_MS (default 15 000 ms since spawn)
  * the backend logged "first /api/health served +X.XXs"  (instrumented build)
  * no "did not respond" / "health wait TIMED OUT" / health-watchdog dump
    appeared in that boot block

Standalone:  python scripts/boot_log_check.py [--max-ms 15000] [--log PATH]
Exit 0 = PASS, 2 = FAIL/missing markers. Also imported by validate_full_12h.py,
which folds the verdict into "OVERALL".
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

DEFAULT_LOG = os.path.expanduser(r"~\AppData\Roaming\trustnode-edge-desktop\backend.log")
DEFAULT_MAX_HEALTH_MS = int(os.environ.get("VAL_BOOT_MAX_HEALTH_MS", "15000"))

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\]")
_RE = {
    "boot_start": re.compile(r"Splash window created"),
    "spawned": re.compile(r"Backend spawned pid=(\d+)"),
    "uvicorn_up": re.compile(r"Uvicorn running on"),
    "health_ok": re.compile(r"Backend health OK after (\d+) ms of polling(?: \((\d+) ms since spawn\))?"),
    "first_served": re.compile(r"first /api/health served \+([0-9.]+)s"),
    "snapshot_ready": re.compile(r"health snapshot ready \+([0-9.]+)s"),
    "resumed": re.compile(r"auto-resumed (\d+) gateway"),
    "deferred_done": re.compile(r"deferred init complete \+([0-9.]+)s"),
    "did_not_respond": re.compile(r"did not respond|health wait TIMED OUT|BOOT ABORTED"),
    "watchdog_dump": re.compile(r"\[boot\]\[health-watchdog\]"),
    "exited": re.compile(r"Backend exited with code"),
    "integrity": re.compile(r"\[boot\]\[integrity\] (.*)$"),
}


def _ts(line: str):
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def analyze_last_boot(log_path: str = DEFAULT_LOG, tail_bytes: int = 4_000_000) -> dict:
    """Return metrics for the most recent boot block (from the last
    'Splash window created' to EOF). Never raises."""
    out: dict = {"log": log_path, "found": False}
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            text = f.read().decode("utf-8", "replace")
    except Exception as exc:
        out["error"] = f"cannot read log: {exc}"
        return out
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE["boot_start"].search(lines[i]):
            start = i
            break
    if start is None:
        out["error"] = "no boot block found in log tail"
        return out
    block = lines[start:]
    out["found"] = True
    out["boot_started_utc"] = (_ts(block[0]) or datetime.now(timezone.utc)).isoformat()[:19] + "Z"
    out["lines"] = len(block)
    out["health_ok_ms"] = None
    out["health_since_spawn_ms"] = None
    out["first_served_s"] = None
    out["snapshot_ready_s"] = None
    out["deferred_done_s"] = None
    out["resumed_gateways"] = None
    out["uvicorn_up_s"] = None
    out["did_not_respond"] = 0
    out["watchdog_dumps"] = 0
    out["exits"] = 0
    out["integrity"] = []
    t_spawn = None
    for ln in block:
        t = _ts(ln)
        if _RE["spawned"].search(ln):
            t_spawn = t
        if _RE["uvicorn_up"].search(ln) and t and t_spawn and out["uvicorn_up_s"] is None:
            out["uvicorn_up_s"] = round((t - t_spawn).total_seconds(), 2)
        m = _RE["health_ok"].search(ln)
        if m and out["health_ok_ms"] is None:
            out["health_ok_ms"] = int(m.group(1))
            if m.group(2):
                out["health_since_spawn_ms"] = int(m.group(2))
        m = _RE["first_served"].search(ln)
        if m and out["first_served_s"] is None:
            out["first_served_s"] = float(m.group(1))
        m = _RE["snapshot_ready"].search(ln)
        if m and out["snapshot_ready_s"] is None:
            out["snapshot_ready_s"] = float(m.group(1))
        m = _RE["deferred_done"].search(ln)
        if m and out["deferred_done_s"] is None:
            out["deferred_done_s"] = float(m.group(1))
        m = _RE["resumed"].search(ln)
        if m and out["resumed_gateways"] is None:
            out["resumed_gateways"] = int(m.group(1))
        if _RE["did_not_respond"].search(ln):
            out["did_not_respond"] += 1
        if _RE["watchdog_dump"].search(ln):
            out["watchdog_dumps"] += 1
        if _RE["exited"].search(ln):
            out["exits"] += 1
        m = _RE["integrity"].search(ln)
        if m:
            # ASCII-safe: Electron's log can carry mojibake (U+FFFD) and a
            # cp1252 console would otherwise crash the standalone printer.
            out["integrity"].append(m.group(1)[:120].encode("ascii", "replace").decode("ascii"))
    return out


def verdict(m: dict, max_health_ms: int = DEFAULT_MAX_HEALTH_MS) -> tuple[bool, list[str]]:
    """(ok, lines) - every line reads 'name : PASS/FAIL (detail)'."""
    L: list[str] = []
    ok = True
    if not m.get("found"):
        return False, [f"  boot block present          : FAIL ({m.get('error', 'missing')})"]
    ms = m.get("health_since_spawn_ms")
    if ms is None:
        ms = m.get("health_ok_ms")
    if ms is None:
        L.append("  boot health logged           : FAIL (no 'Backend health OK' marker - old build or boot never became healthy)")
        ok = False
    else:
        good = ms <= max_health_ms
        L.append(f"  boot health <= {max_health_ms / 1000:.0f}s          : {'PASS' if good else 'FAIL'} ({ms / 1000:.1f}s spawn->first 200)")
        ok &= good
    fs = m.get("first_served_s")
    if fs is None:
        L.append("  backend boot instrumentation : FAIL (no 'first /api/health served' marker - build lacks the boot-health fix)")
        ok = False
    else:
        snap = m.get("snapshot_ready_s")
        snap_txt = f"+{snap:.2f}s" if snap is not None else "pending"
        L.append(f"  backend boot instrumentation : PASS (first /api/health +{fs:.2f}s, snapshot ready {snap_txt})")
    bad = int(m.get("did_not_respond") or 0)
    L.append(f"  no splash boot failure       : {'PASS' if bad == 0 else f'FAIL ({bad} did-not-respond/timeout lines)'}")
    ok &= bad == 0
    wd = int(m.get("watchdog_dumps") or 0)
    L.append(f"  no boot-health watchdog dump : {'PASS' if wd == 0 else f'FAIL ({wd} dumps - health not served in time)'}")
    ok &= wd == 0
    return ok, L


def summary_lines(m: dict) -> list[str]:
    if not m.get("found"):
        return [f"  (no boot block: {m.get('error')})"]
    integ = "; ".join(m.get("integrity") or []) or "none"
    return [
        f"  boot started {m.get('boot_started_utc')}  uvicorn up +{m.get('uvicorn_up_s')}s  "
        f"health 200 at {m.get('health_since_spawn_ms')} ms since spawn  "
        f"gateways resumed={m.get('resumed_gateways')}  deferred done +{m.get('deferred_done_s')}s  "
        f"exits={m.get('exits')}",
        f"  integrity: {integ}",
    ]


def main(argv: list[str]) -> int:
    try:  # never let a console code page turn a verdict into a traceback
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log = DEFAULT_LOG
    max_ms = DEFAULT_MAX_HEALTH_MS
    it = iter(argv)
    for a in it:
        if a == "--log":
            log = next(it, log)
        elif a == "--max-ms":
            max_ms = int(next(it, str(max_ms)))
    m = analyze_last_boot(log)
    ok, lines = verdict(m, max_ms)
    print("[BOOT HEALTH CHECK]")
    print("\n".join(summary_lines(m)))
    print("\n".join(lines))
    print(f"  BOOT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
