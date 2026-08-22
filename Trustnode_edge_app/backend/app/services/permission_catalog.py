"""The single map of licence module -> feature -> per-user permission -> pages.

Why this exists (2026-08-22, see docs/access-permissions-and-surfaces-
investigation-2026-08-22.md §R3): the edge had three unrelated lists — 34 licence
modules in MODULE_CATALOG, 27 permission labels in App.jsx and 24 rendered
checkboxes, six of which were read by nothing. Seventeen modules had no per-user
control at all. Anything not in all three lists silently did nothing.

This module is the authority. The UI renders the Users and Access Control page
from it (so a new licence module cannot be forgotten), and the backend uses the
same table to decide what a permission actually governs.

Rules:
  * `module` must be a key of control_plane_store.MODULE_CATALOG, or "" for a
    feature every licence includes.
  * `key` is the per-user permission stored in the user's `permissions` dict.
  * `legacy_keys` are older spellings still present in saved user documents; a
    permission is granted when the canonical key OR any legacy key is true.
  * `pages` are the frontend page ids the permission opens.
  * `admin_only` marks a feature no non-admin may have, whatever is ticked.
  * `write_roles` names who may CHANGE the thing (reads follow the permission).
"""
from __future__ import annotations

from typing import Any, Dict, List

FEATURES: List[Dict[str, Any]] = [
    # ---------------------------------------------------------- Visualization
    {"key": "dashboard", "label": "Dashboard", "module": "dashboard",
     "group": "Visualization", "pages": ["dashboard"]},
    {"key": "custom_dashboards", "label": "Edit dashboards", "module": "custom_dashboards",
     "group": "Visualization", "pages": [], "write_roles": ["admin", "super", "engineer"]},

    # ------------------------------------------------------------------ Power
    {"key": "power_overview", "label": "Power overview", "module": "power_overview",
     "group": "Power", "pages": ["power_overview"]},
    {"key": "power_management", "label": "Power configuration", "module": "power_management",
     "group": "Power", "pages": ["power_configuration"], "write_roles": ["admin", "super", "engineer"]},
    {"key": "oee_downtime", "label": "OEE and downtime", "module": "oee_downtime",
     "group": "Power", "pages": []},

    # ------------------------------------------------------------------- Data
    {"key": "historian", "label": "Historian", "module": "historian",
     "group": "Data", "pages": ["historian"]},
    {"key": "historian_export", "label": "Export historian data", "module": "historian_export",
     "group": "Data", "pages": []},
    {"key": "triggers_and_limits", "label": "Triggers and limits", "module": "triggers_limits",
     "group": "Data", "legacy_keys": ["triggers_limits"], "pages": ["triggers_and_limits"],
     "write_roles": ["admin", "super", "engineer"]},

    # ------------------------------------------------------------- Operations
    {"key": "alarms", "label": "Alarms", "module": "alarms",
     "group": "Operations", "legacy_keys": ["client_module_alarms"], "pages": ["alarms"]},
    {"key": "email_and_notifications", "label": "E-mail and notifications",
     "module": "email_notifications", "group": "Operations",
     "legacy_keys": ["email_notifications"], "pages": ["email_and_notifications"],
     "write_roles": ["admin", "super", "engineer"]},

    # -------------------------------------------------------------- Reporting
    {"key": "reporting", "label": "Reports", "module": "reporting", "group": "Reporting",
     "legacy_keys": ["client_module_reporting"], "pages": ["reporting", "generated_reports"]},
    {"key": "scheduled_reports", "label": "Scheduled reports", "module": "scheduled_reports",
     "group": "Reporting", "pages": ["scheduled_reports"], "write_roles": ["admin", "super", "engineer"]},
    {"key": "report_templates", "label": "Report templates", "module": "report_templates",
     "group": "Reporting", "pages": [], "write_roles": ["admin", "super", "engineer"]},

    # --------------------------------------------------------------- Gateways
    {"key": "tags", "label": "Tags", "module": "tags", "group": "Gateways", "pages": ["tags"]},
    {"key": "devices", "label": "Devices", "module": "", "group": "Gateways",
     "pages": ["devices"], "write_roles": ["admin", "super", "engineer"]},
    {"key": "gateway_configuration", "label": "Gateway configuration",
     "module": "gateway_configuration", "group": "Gateways", "pages": ["gateway_configuration"],
     "write_roles": ["admin", "super", "engineer"]},
    {"key": "gateway_runtime_control", "label": "Start / stop gateways",
     "module": "gateway_runtime_control", "group": "Gateways", "pages": [],
     "write_roles": ["admin", "super", "engineer", "operator"]},

    # ------------------------------------------------------------ Applications
    {"key": "batch_management", "label": "Batch management", "module": "batch_management",
     "group": "Applications", "legacy_keys": ["batches", "batch_overview"],
     "pages": ["batch_overview", "batch_analysis"]},
    {"key": "batch_definitions", "label": "Batch definitions", "module": "batch_management",
     "group": "Applications", "pages": ["batch_definitions"],
     "write_roles": ["admin", "super", "engineer"]},

    # ---------------------------------------------------------------------- AI
    {"key": "trustnode_intelligence", "label": "TrustNode Intelligence",
     "module": "trustnode_intelligence", "group": "AI", "pages": ["trustnode_intelligence"]},

    # ------------------------------------------------------------ Connections
    {"key": "connections_overview", "label": "Connections overview", "module": "connections",
     "group": "Connections", "pages": ["connections_overview"], "admin_only": True},
    {"key": "lan_sharing", "label": "Remote access", "module": "lan_access",
     "group": "Connections", "pages": ["lan_sharing"], "admin_only": True},
    {"key": "opc_ua", "label": "OPC UA server", "module": "opcua",
     "group": "Connections", "pages": ["opc_ua"], "admin_only": True},
    {"key": "mqtt", "label": "MQTT broker", "module": "mqtt",
     "group": "Connections", "pages": ["mqtt"], "admin_only": True},

    # ------------------------------------------------------------------ Admin
    {"key": "interface", "label": "Interface", "module": "interface", "group": "Admin",
     "legacy_keys": ["client_module_interface"], "pages": ["interface"]},
    {"key": "database", "label": "Database overview", "module": "database", "group": "Admin",
     "legacy_keys": ["database_overview"], "pages": ["database"], "admin_only": True},
    {"key": "customer_database", "label": "Customer database", "module": "cloud_database",
     "group": "Admin", "pages": ["customer_database"], "admin_only": True},
    {"key": "backup_and_retention", "label": "Backup and retention", "module": "database",
     "group": "Admin", "pages": ["backup_and_retention"], "admin_only": True},
    {"key": "data_log", "label": "Logs", "module": "", "group": "Admin",
     "legacy_keys": ["logs"], "pages": ["logs", "data_log"], "admin_only": True},
    {"key": "directories", "label": "Directories", "module": "", "group": "Admin",
     "pages": ["directories"], "admin_only": True},
    {"key": "users_and_access_control", "label": "Users and access control",
     "module": "users_and_access_control", "group": "Admin",
     "pages": ["users_and_access_control"], "admin_only": True},
    {"key": "edge", "label": "Edge settings", "module": "", "group": "Admin",
     "pages": ["edge"], "admin_only": True},
    {"key": "control_plane", "label": "Control plane (portal)", "module": "",
     "group": "Admin", "pages": ["control_plane"], "admin_only": True},
]

GROUP_ORDER = ["Visualization", "Data", "Operations", "Reporting", "Power",
               "Gateways", "Applications", "AI", "Connections", "Admin"]

# Surfaces a person can be given, kept here so the UI renders them with the rest.
SURFACE_FEATURES = [
    {"key": "access_full", "label": "TrustNode Edge (full app over LAN)",
     "module": "remote_admin_lan", "group": "Remote access", "pages": []},
    {"key": "access_client", "label": "TrustNode Local View (read-only over LAN)",
     "module": "local_web_app", "group": "Remote access", "pages": []},
    {"key": "access_lite", "label": "Lite (legacy read-only)",
     "module": "local_web_app", "group": "Remote access", "pages": []},
]


def _module_licensed(module: str) -> bool:
    if not module:
        return True
    try:
        from app.services import access_policy
        return bool(access_policy.has_module(module))
    except Exception:
        try:
            from app.services import license_inspect
            return bool(license_inspect.has_module(module))
        except Exception:
            return True


def permission_keys() -> List[str]:
    return [f["key"] for f in FEATURES] + [f["key"] for f in SURFACE_FEATURES]


def feature_for_key(key: str) -> Dict[str, Any] | None:
    wanted = str(key or "").strip().lower()
    for f in FEATURES + SURFACE_FEATURES:
        if f["key"] == wanted:
            return f
    return None


def resolve(permissions: Dict[str, Any], key: str) -> bool:
    """True when the user holds `key`, honouring legacy spellings.

    OR across canonical + legacy on purpose: real user documents carry both
    spellings and they disagree, so any precedence order silently contradicts
    the checkbox the admin ticked."""
    feature = feature_for_key(key)
    if not feature:
        return bool((permissions or {}).get(key))
    perms = permissions or {}
    if perms.get(feature["key"]):
        return True
    return any(bool(perms.get(legacy)) for legacy in feature.get("legacy_keys", []))


def catalog(include_unlicensed: bool = True) -> Dict[str, Any]:
    """The whole map, annotated with what THIS licence includes, grouped for the
    Users and Access Control page."""
    try:
        from app.services.control_plane_store import MODULE_CATALOG
        labels = {m["key"]: m.get("label") or m["key"] for m in MODULE_CATALOG}
    except Exception:
        labels = {}

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for feature in FEATURES + SURFACE_FEATURES:
        module = feature.get("module") or ""
        licensed = _module_licensed(module)
        if not licensed and not include_unlicensed:
            continue
        groups.setdefault(feature["group"], []).append({
            "key": feature["key"],
            "label": feature["label"],
            "module": module,
            "module_label": labels.get(module, module),
            "licensed": licensed,
            "admin_only": bool(feature.get("admin_only")),
            "pages": list(feature.get("pages") or []),
            "legacy_keys": list(feature.get("legacy_keys") or []),
            "write_roles": list(feature.get("write_roles") or []),
        })

    ordered = [{"group": g, "features": groups[g]}
               for g in GROUP_ORDER + ["Remote access"] if g in groups]
    return {"ok": True, "groups": ordered, "keys": permission_keys()}
