"""In-memory active-View-session tracker for license enforcement.

Goal: enforce the `max_view_users` numeric license limit at the auth
layer. Only sessions whose role is `view` / `viewer` / `client` count
against the limit. Admins, engineers, and the master-admin are not
counted — they have their own admin-count limit (`max_studio_admins`).

A session is considered "active" when its last activity timestamp is
within `ACTIVE_WINDOW_SECONDS` (default 5 minutes). The auth
middleware bumps the timestamp on every authenticated request.

If the limit is exceeded:
  * Login is REJECTED with 429 + a clear reason.
  * No eviction: refusing the new login is safer than kicking the
    operator who's actually working. The new user can retry once
    another session goes idle.

Failure modes are fail-OPEN: if the license helper throws, this
module lets the login through. Same philosophy as the rest of the
license-enforcement code — never break a customer because of our
bug.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
# username -> last activity monotonic
_sessions_last_seen: dict[str, float] = {}

ACTIVE_WINDOW_SECONDS = 300.0
VIEW_ROLES = {"view", "viewer", "client", "client_operator", "kiosk"}


def _role_is_view(role: str) -> bool:
    return str(role or "").strip().lower() in VIEW_ROLES


def mark_active(username: str, role: str) -> None:
    """Called by the auth middleware on every authenticated request.
    Only meaningful for view-role users; others are ignored cheaply."""
    if not username or not _role_is_view(role):
        return
    with _lock:
        _sessions_last_seen[username] = time.monotonic()


def active_view_session_count(exclude_username: str = "") -> int:
    """How many view-role users have been active in the last 5 min?
    Optional `exclude_username` is used at login to count "other"
    sessions, since the user logging in is about to occupy a slot."""
    cutoff = time.monotonic() - ACTIVE_WINDOW_SECONDS
    n = 0
    with _lock:
        for u, ts in list(_sessions_last_seen.items()):
            if ts < cutoff:
                _sessions_last_seen.pop(u, None)
                continue
            if exclude_username and u == exclude_username:
                continue
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
    """Called on logout / token expiry to free a slot immediately."""
    if not username:
        return
    with _lock:
        _sessions_last_seen.pop(username, None)


def get_active_view_usernames() -> list[str]:
    """For diagnostics / the License Details page."""
    cutoff = time.monotonic() - ACTIVE_WINDOW_SECONDS
    out: list[str] = []
    with _lock:
        for u, ts in _sessions_last_seen.items():
            if ts >= cutoff:
                out.append(u)
    return sorted(out)
