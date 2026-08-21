"""In-memory session liveness tracker.

Two jobs (operator 2026-06-23, extended 2026-08-21):

1. `max_view_users` — the licence's concurrent View-session cap. A view-role
   user counts as active when seen within `ACTIVE_WINDOW_SECONDS` (5 min).
   The auth middleware bumps liveness on every authenticated request; login
   refuses a NEW view session when the cap is reached rather than evicting an
   operator who is actively working.
2. Remote Access visibility (2026-08-21) — EVERY authenticated user is now
   tracked with role, client IP and surface (desktop / lan_full / lan_client /
   lan_lite / api) so the Remote Access page can list who is connected from
   where and revoke them. The cap logic still counts view roles only.

Process-local by design: the edge is a single process, and a restart naturally
clears sessions (tokens then re-register on their next request).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

_lock = threading.Lock()
# username -> {"last_seen": monotonic, "since": epoch, "role": str, "ip": str, "surface": str}
_sessions: Dict[str, Dict[str, Any]] = {}

ACTIVE_WINDOW_SECONDS = 300.0
VIEW_ROLES = {"view", "viewer", "client", "client_operator", "kiosk"}


def _role_is_view(role: str) -> bool:
    return str(role or "").strip().lower() in VIEW_ROLES


def mark_active(username: str, role: str, ip: str = "", surface: str = "") -> None:
    """Called by the auth middleware on every authenticated request (all roles)."""
    if not username:
        return
    now = time.monotonic()
    with _lock:
        s = _sessions.get(username)
        if s is None:
            s = {"since": time.time(), "role": str(role or ""), "ip": str(ip or ""), "surface": str(surface or "")}
            _sessions[username] = s
        s["last_seen"] = now
        if role:
            s["role"] = str(role)
        if ip:
            s["ip"] = str(ip)
        if surface:
            s["surface"] = str(surface)


def _prune_locked(cutoff: float) -> None:
    for u, s in list(_sessions.items()):
        if float(s.get("last_seen") or 0.0) < cutoff:
            _sessions.pop(u, None)


def active_view_session_count(exclude_username: str = "") -> int:
    """How many view-role users have been active in the last 5 min?
    Optional `exclude_username` is used at login to count "other"
    sessions, since the user logging in is about to occupy a slot."""
    cutoff = time.monotonic() - ACTIVE_WINDOW_SECONDS
    n = 0
    with _lock:
        _prune_locked(cutoff)
        for u, s in _sessions.items():
            if exclude_username and u == exclude_username:
                continue
            if _role_is_view(str(s.get("role") or "")):
                n += 1
    return n


def check_view_login_allowed(username: str, role: str) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is for the response body when
    denied. Always (True, '') for non-view users."""
    if not _role_is_view(role):
        return (True, "")
    try:
        from app.services import license_inspect
        max_users = license_inspect.get_limit("max_view_users")
    except Exception:
        return (True, "")  # fail-open
    if not max_users or max_users <= 0:
        return (True, "")  # 0 / missing => unlimited
    current = active_view_session_count(exclude_username=username)
    if current >= max_users:
        return (False, f"License limit reached: {current}/{max_users} View sessions active. Try again later.")
    return (True, "")


def forget(username: str) -> None:
    """Called on logout / token revocation to free a slot immediately."""
    if not username:
        return
    with _lock:
        _sessions.pop(username, None)


def get_active_view_usernames() -> List[str]:
    """For diagnostics / the License Details page."""
    cutoff = time.monotonic() - ACTIVE_WINDOW_SECONDS
    with _lock:
        _prune_locked(cutoff)
        return [u for u, s in _sessions.items() if _role_is_view(str(s.get("role") or ""))]


def list_active(include_local: bool = True) -> List[Dict[str, Any]]:
    """Active sessions for the Remote Access page (all roles)."""
    cutoff = time.monotonic() - ACTIVE_WINDOW_SECONDS
    out: List[Dict[str, Any]] = []
    now_mono = time.monotonic()
    with _lock:
        _prune_locked(cutoff)
        for u, s in _sessions.items():
            ip = str(s.get("ip") or "")
            local = ip in ("127.0.0.1", "::1", "localhost", "")
            if local and not include_local:
                continue
            out.append({
                "username": u,
                "role": str(s.get("role") or ""),
                "ip": ip,
                "surface": str(s.get("surface") or ("desktop" if local else "api")),
                "since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(s.get("since") or time.time()))),
                "idle_s": int(now_mono - float(s.get("last_seen") or now_mono)),
                "local": local,
            })
    out.sort(key=lambda r: (r["local"], r["username"]))
    return out
