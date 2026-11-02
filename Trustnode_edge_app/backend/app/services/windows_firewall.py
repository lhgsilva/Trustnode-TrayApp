"""Windows Firewall self-heal for the LAN-sharing path.

The NSIS installer creates an inbound TCP rule for trustnode-service.exe
at install time. Three reasons that rule can still be missing when the
backend tries to bind 0.0.0.0:
  1. Customer is running the portable EXE (no installer ever ran).
  2. Customer or IT manually deleted the rule.
  3. Pre-2026-06-18 installer where the netsh command was malformed
     (broken backslash-line-continuation inside nsExec::Exec) and
     silently failed.

When (3) was in production, every backend boot triggered a Windows
Defender prompt with the full install path — exactly the dialog we
went out of our way to suppress.

This module fixes that at runtime: just before the first
0.0.0.0 bind, we check if a matching firewall rule exists for the
current trustnode-service.exe path. If not, we add one. If the call
needs elevation (non-admin tray launch) we skip silently — the user
will see the prompt once more, then never again.

Public API:
  * ensure_backend_rule() -> dict
      Returns {ok, already_present, added, reason}. Never raises.

Cost: one ~50 ms `netsh advfirewall show rule` probe per process,
cached so repeated toggles don't re-probe.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_RULE_NAME = "TrustNode Backend"
_cached_result: Optional[dict] = None


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _current_exe_path() -> str:
    """Path of the running trustnode-service.exe.

    PyInstaller sets sys.executable to the bundled .exe; in dev (running
    `python -m app`) we fall back to the python interpreter. The
    firewall rule matches by program path, so dev hits the prompt for
    python.exe — fine, only PyInstaller builds need silent boot.
    """
    try:
        return os.path.abspath(sys.executable)
    except Exception:
        return ""


def _rule_exists_for_path(exe_path: str) -> bool:
    """Probe netsh: does ANY rule with our name target this exe?

    netsh prints "No rules match the specified criteria." when missing.
    A matching rule's output contains "Program:" followed by the path
    (case-insensitive on Windows).

    2026-10-30, MEASURED on a machine whose rule was correct and present:

        plain    rc=0 bytes= 610  has 'Program:'=False  path found=False
        verbose  rc=0 bytes= 950  has 'Program:'=True   path found=True

    `show rule name=X` does NOT print the Program line. Without `verbose`
    this scanned output that could never contain the path, so it returned
    False for a rule that existed and matched. Two consequences, both seen:

      * the Remote Access page permanently warned "no inbound rule allows
        this program - other computers cannot reach it" while the LAN was
        in fact reachable, which is the kind of false alarm that sends
        somebody hunting a fault that is the measurement itself;
      * ensure_backend_rule() short-circuits on this check, so it ran
        `netsh advfirewall firewall add rule` on EVERY start. Unelevated
        that fails; elevated it appends a duplicate every time. That is
        where a machine ends up with hundreds of identical rules.
    """
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={_RULE_NAME}", "verbose"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=0x08000000 if _is_windows() else 0,  # CREATE_NO_WINDOW
        )
    except Exception as exc:
        logger.debug("netsh show probe failed: %s", exc)
        return False
    if proc.returncode != 0:
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    if not out:
        return False
    norm = out.replace("/", "\\").lower()
    return exe_path.replace("/", "\\").lower() in norm


def _add_rule(exe_path: str) -> tuple[bool, str]:
    """Create the inbound TCP allow-rule. Returns (ok, reason).

    Requires admin. When called from a non-elevated tray, netsh exits
    with code 1 and prints "Access denied." We surface that verbatim so
    the operator can see the reason in /api/lan-sharing/status if they
    look. The next admin launch (or installer re-run) heals it.
    """
    try:
        proc = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={_RULE_NAME}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"program={exe_path}",
                "enable=yes",
                "profile=private,domain",  # 2026-08-21: never open the listener on Public networks
                "description=Allow TrustNode local backend (Lite + control APIs).",
            ],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=0x08000000 if _is_windows() else 0,
        )
    except Exception as exc:
        return False, f"netsh exec failed: {type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, "added"
    msg = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (msg[0] if msg else f"netsh returned {proc.returncode}")


def ensure_backend_rule(force: bool = False) -> dict:
    """Make sure a firewall rule exists for the current backend exe.

    Cached per-process. Pass force=True to re-probe (useful after the
    operator runs the installer or manually adjusts rules).

    Returns:
        {ok, already_present, added, reason, exe_path}
    """
    global _cached_result
    if _cached_result is not None and not force:
        return _cached_result
    result: dict = {"ok": False, "already_present": False, "added": False, "reason": "", "exe_path": ""}
    if not _is_windows():
        result.update(ok=True, reason="not windows")
        _cached_result = result
        return result
    exe = _current_exe_path()
    result["exe_path"] = exe
    if not exe or not os.path.isfile(exe):
        result["reason"] = "exe path not resolvable"
        _cached_result = result
        return result
    if _rule_exists_for_path(exe):
        result.update(ok=True, already_present=True, reason="rule present")
        _cached_result = result
        return result
    ok, reason = _add_rule(exe)
    result.update(ok=ok, added=ok, reason=reason)
    if not ok:
        # Don't cache failures — a later elevated call (installer rerun,
        # operator-as-admin tray relaunch) should still get a chance.
        return result
    _cached_result = result
    return result

def add_rule_elevated() -> dict:
    """Add the inbound rule, asking Windows for elevation (one UAC prompt).

    2026-10-27, requested: remote access must work on BOTH installers. The
    NSIS installer runs elevated and gets its rule; the PORTABLE build runs
    as the operator and `_add_rule` fails every single time with

        "The requested operation requires elevation (Run as administrator)."

    so LAN sharing listens on 0.0.0.0 and Windows silently drops every
    packet from every other computer. This is the same netsh command the
    installer runs, launched through ShellExecute's `runas` verb so the
    operator confirms it once and it persists.

    Deliberately NOT called automatically: a prompt nobody asked for, on a
    machine the operator may not administer, is how an app gets uninstalled.
    The Remote Access page offers it when the status says the rule is
    missing.
    """
    result = {"ok": False, "added": False, "reason": "", "exe_path": ""}
    if not _is_windows():
        result.update(ok=True, reason="not windows")
        return result
    exe = _current_exe_path()
    result["exe_path"] = exe
    if not exe or not os.path.isfile(exe):
        result["reason"] = "exe path not resolvable"
        return result
    if _rule_exists_for_path(exe):
        result.update(ok=True, reason="rule already present")
        return result
    args = (
        'advfirewall firewall add rule name="%s" dir=in action=allow '
        'program="%s" enable=yes profile=private,domain '
        'description="Allow TrustNode Edge on the local network."'
        % (_RULE_NAME, exe)
    )
    ps = (
        "$p = Start-Process -FilePath netsh -ArgumentList '%s' "
        "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
        % args.replace("'", "''")
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=120, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:                         # noqa: BLE001
        result["reason"] = "%s: %s" % (type(exc).__name__, exc)
        return result
    if _rule_exists_for_path(exe):
        global _cached_result
        _cached_result = None                        # re-probe next time
        result.update(ok=True, added=True, reason="rule created")
        return result
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    low = out.lower()
    if "canceled" in low or "cancelled" in low or r.returncode == 1223:
        result["reason"] = "the administrator prompt was declined"
    else:
        result["reason"] = out[:200] or "netsh did not create the rule"
    return result
