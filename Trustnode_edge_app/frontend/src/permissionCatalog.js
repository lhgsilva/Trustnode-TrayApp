// 2026-11-04. Which permission opens which page - a COPY of the backend's
// app/services/permission_catalog.py FEATURES (key, legacy_keys, pages,
// admin_only, write_roles), because non-admin sessions cannot fetch the
// catalogue (it lives under the admin-only /api/control-plane/).
//
// Reported: "when we create new users as operator or admin and give them
// access in the permissions, some pages are not shown, like Scheduled
// Reports". The page rule in App.jsx never read the catalogue: it used a
// hand-written map, so Scheduled Reports and E-mail checked "Users and
// access", and the OEE, batch and power-configuration permissions the Users
// page offers were read by nothing. The page rule now asks this table.
//
// scripts/test_permissions_open_pages.py fails if this copy and the backend
// ever differ. Edit the backend first, then regenerate this file.
//
// Pure module; tested with node.

export const PERMISSION_FEATURES = [
  {"key": "dashboard", "pages": ["dashboard"]},
  {"key": "custom_dashboards", "pages": [], "write_roles": ["admin", "super", "engineer"]},
  {"key": "power_overview", "pages": ["power_overview"]},
  {"key": "power_management", "pages": ["power_configuration"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "oee_downtime", "pages": []},
  {"key": "historian", "pages": ["historian"]},
  {"key": "historian_export", "pages": []},
  {"key": "triggers_and_limits", "legacy_keys": ["triggers_limits"], "pages": ["triggers_and_limits"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "alarms", "legacy_keys": ["client_module_alarms"], "pages": ["alarms"]},
  {"key": "email_and_notifications", "legacy_keys": ["email_notifications"], "pages": ["email_and_notifications"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "reporting", "legacy_keys": ["client_module_reporting"], "pages": ["reporting", "generated_reports"]},
  {"key": "scheduled_reports", "pages": ["scheduled_reports"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "report_templates", "pages": [], "write_roles": ["admin", "super", "engineer"]},
  {"key": "tags", "pages": ["tags"]},
  {"key": "devices", "pages": ["devices"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "gateway_configuration", "pages": ["gateway_configuration"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "gateway_runtime_control", "pages": [], "write_roles": ["admin", "super", "engineer", "operator"]},
  {"key": "batch_management", "legacy_keys": ["batches", "batch_overview"], "pages": ["batch_overview", "batch_analysis"]},
  {"key": "batch_definitions", "pages": ["batch_definitions"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "oee", "pages": ["oee_overview", "oee_operator", "oee_machine_detail"]},
  {"key": "oee_configuration", "pages": ["oee_configuration"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "oee_planning", "pages": ["oee_planning"], "write_roles": ["admin", "super", "engineer"]},
  {"key": "trustnode_intelligence", "pages": ["trustnode_intelligence"]},
  {"key": "connections_overview", "pages": ["connections_overview"], "admin_only": true},
  {"key": "lan_sharing", "pages": ["lan_sharing"], "admin_only": true},
  {"key": "opc_ua", "pages": ["opc_ua"], "admin_only": true},
  {"key": "mqtt", "pages": ["mqtt"], "admin_only": true},
  {"key": "interface", "legacy_keys": ["client_module_interface"], "pages": ["interface"]},
  {"key": "database", "legacy_keys": ["database_overview"], "pages": ["database"], "admin_only": true},
  {"key": "customer_database", "pages": ["customer_database"], "admin_only": true},
  {"key": "backup_and_retention", "pages": ["backup_and_retention"], "admin_only": true},
  {"key": "data_log", "legacy_keys": ["logs"], "pages": ["logs", "data_log"], "admin_only": true},
  {"key": "directories", "pages": ["directories"], "admin_only": true},
  {"key": "users_and_access_control", "pages": ["users_and_access_control"], "admin_only": true},
  {"key": "edge", "pages": ["edge"], "admin_only": true},
  {"key": "control_plane", "pages": ["control_plane"], "admin_only": true}
];

function _granted(perms, f) {
  const p = perms || {};
  if (p[f.key]) return true;
  return (f.legacy_keys || []).some((k) => Boolean(p[k]));
}

function _featuresFor(page) {
  const id = String(page || "");
  return PERMISSION_FEATURES.filter((f) => (f.pages || []).includes(id));
}

/** For a NON-admin: true/false when the catalogue governs `page`, else null. */
export function cataloguePageOpen(page, perms) {
  const fs = _featuresFor(page);
  if (!fs.length) return null;
  if (fs.some((f) => f.admin_only)) return false;
  return fs.some((f) => _granted(perms, f));
}

/** For a NON-admin: may this role CHANGE what `page` shows? null when ungoverned. */
export function cataloguePageEdit(page, perms, role) {
  const fs = _featuresFor(page);
  if (!fs.length) return null;
  if (fs.some((f) => f.admin_only)) return false;
  const r = String(role || "").toLowerCase();
  return fs.some((f) => _granted(perms, f)
    && (!(f.write_roles || []).length || (f.write_roles || []).includes(r)));
}
