"""Named licence seats — one place that knows who holds what.

Design notes (2026-08-22, docs/licensing-seats-and-remote-access-plan-2026-08-22.md):

* A customer buys seats per product (TrustNode Edge / Studio / View LAN /
  Cloud View). The edge admin assigns those seats to named users.
* Seats are stored inside the user's `permissions` dict (`permissions.seats`,
  `permissions.view_ui`). That dict already reaches the JWT claim, the
  users_access document, `cp_users.permissions_json` and
  `auth_store.users.permissions_json`, so seats travel everywhere the edge
  needs them without new plumbing. AuthStore additionally keeps dedicated
  columns, which is what `find_by_login()` and the uniqueness check use.
* The edge has TWO user stores: AuthStore (local accounts) and the control-plane
  store (`cp_users`, portal-pushed / multi-edge). A seat census MUST span both,
  de-duplicated by username, or the ledger under-counts and the cap leaks.
* Enforcement only happens when the licence actually carries a `seats` block
  (`license_inspect.seats_are_explicit()`). Licences issued before this feature
  keep the old concurrent-session behaviour exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.services import license_inspect

__all__ = [
    "seats_of_user",
    "view_ui_of_user",
    "census",
    "holders",
    "resolve_login_identity",
    "has_seat",
]


def _module_licensed(module: str) -> bool:
    if not module:
        return True
    try:
        from app.services import access_policy
        return bool(access_policy.has_module(module))
    except Exception:
        return bool(license_inspect.has_module(module))


def _norm_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        key = str(item or "").strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def seats_of_user(record: Dict[str, Any]) -> List[str]:
    """Seats held by a user record from either store."""
    if not isinstance(record, dict):
        return []
    direct = _norm_list(record.get("seats"))
    if direct:
        return direct
    perms = record.get("permissions")
    if isinstance(perms, dict):
        return _norm_list(perms.get("seats"))
    return []


def view_ui_of_user(record: Dict[str, Any]) -> str:
    """Which UI a View LAN seat is served for this user: 'lite' or
    'app_readonly' (the default)."""
    if not isinstance(record, dict):
        return "app_readonly"
    value = str(record.get("view_ui") or "").strip().lower()
    if not value:
        perms = record.get("permissions")
        if isinstance(perms, dict):
            value = str(perms.get("view_ui") or "").strip().lower()
    return value if value in ("lite", "app_readonly") else "app_readonly"


def _current_tenant() -> str:
    try:
        from app.tenant import normalize_tenant_id, get_current_tenant
        return normalize_tenant_id(get_current_tenant())
    except Exception:
        return "default"


def _all_users() -> List[Dict[str, Any]]:
    """Every active user across both stores, de-duplicated by username.

    AuthStore wins on conflict: it is the store the login path consults first.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    try:
        from app.state import auth_store

        for row in auth_store.list_users() or []:
            name = str(row.get("username") or "").strip().lower()
            if name:
                seen[name] = row
    except Exception:
        pass
    try:
        from app.state import control_plane_store

        rows = control_plane_store.list_users(tenant_id=_current_tenant()) or []
        for row in rows:
            name = str(row.get("username") or "").strip().lower()
            if name and name not in seen:
                seen[name] = row
    except Exception:
        pass
    return [
        row for row in seen.values()
        if str(row.get("status") or "active").strip().lower() == "active"
    ]


def holders() -> Dict[str, List[Dict[str, Any]]]:
    """product -> [{username, email, view_ui}] for every active holder."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in _all_users():
        for seat in seats_of_user(row):
            out.setdefault(seat, []).append({
                "username": str(row.get("username") or ""),
                "email": str(row.get("email") or ""),
                "view_ui": view_ui_of_user(row),
            })
    for rows in out.values():
        rows.sort(key=lambda r: r["username"].lower())
    return out


def census() -> Dict[str, int]:
    """product -> number of active users holding that seat."""
    return {product: len(rows) for product, rows in holders().items()}


def has_seat(record: Dict[str, Any], product: str) -> bool:
    return str(product or "").strip().lower() in seats_of_user(record)


def resolve_login_identity(identifier: str) -> str:
    """Map a login identifier to the internal username.

    Licensed seats are handed to people by e-mail, so the address must work as a
    login. The internal identity stays the username — the JWT `sub`, the audit
    trail, token revocation and view_sessions all key on it.

    Returns the identifier unchanged when it is not an unambiguous e-mail match.
    """
    ident = str(identifier or "").strip()
    if not ident or "@" not in ident:
        return ident
    try:
        from app.state import auth_store

        if auth_store.get_user(ident):
            return ident                       # a username that looks like an e-mail
        hit = auth_store.find_by_login(ident)
        if hit and hit.get("username"):
            return str(hit["username"])
    except Exception:
        pass
    # cp_users: try the request's tenant first, then every tenant. A login
    # arrives before the tenant is resolved, and an edge routinely holds users
    # under a customer tenant id that differs from the default — matching only
    # the current tenant made e-mail logins fail with a 401.
    for kwargs in ({"tenant_id": _current_tenant()}, {"all_tenants": True}):
        try:
            from app.state import control_plane_store

            rows = control_plane_store.list_users(**kwargs) or []
        except Exception:
            continue
        matches = [
            str(r.get("username") or "")
            for r in rows
            if str(r.get("email") or "").strip().lower() == ident.lower()
            and str(r.get("status") or "active").strip().lower() == "active"
        ]
        # exactly one owner, or the address is ambiguous and must not authenticate
        if len(matches) == 1 and matches[0]:
            return matches[0]
        if len(matches) > 1:
            return ident
    return ident


def ledger() -> Dict[str, Any]:
    """Everything the Users and Access Control page needs to render the seat
    table: what the licence grants, what is assigned, what is left."""
    counts = census()
    held = holders()
    rows = []
    for product in license_inspect.SEAT_PRODUCTS:
        licensed = int(license_inspect.seat_limit(product) or 0)
        used = int(counts.get(product, 0))
        module = license_inspect.SEAT_MODULE.get(product, "")
        rows.append({
            "product": product,
            "label": license_inspect.SEAT_LABELS.get(product, product),
            "licensed": licensed,                       # 0 = unlimited
            "assigned": used,
            "free": None if licensed <= 0 else max(0, licensed - used),
            "over_assigned": bool(licensed > 0 and used > licensed),
            "module": module,
            # access_policy.has_module carries the legacy derivation
            # (remote_admin_lan := lan_access AND local_web_app for licences
            # with no package_key). Using the raw check here would tell a
            # customer their Studio seat is unlicensed when it is not.
            "module_licensed": _module_licensed(module),
            "holders": held.get(product, []),
        })
    explicit = license_inspect.seats_are_explicit()
    return {
        "ok": True,
        "enforced": explicit,
        "note": (
            "Seat counts come from the licence."
            if explicit else
            "This licence predates named seats: the counts below are derived "
            "from its numeric limits and are not enforced per user."
        ),
        "seats": rows,
    }
