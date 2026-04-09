import { Component, useEffect, useMemo, useRef, useState } from "react";
import {
  getHealth,
  getBackendTarget,
  getConfig,
  getStatus,
  getUiSourceConfig,
  getGatewayInstanceStatuses,
  discoverPlcTags,
  browseOpcUaNodes,
  getWsStreamUrl,
  getCloudWsStreamUrl,
  provisionDatabaseObjects,
  setUiSourceConfig,
  testDatabaseConnection,
  testUiSourceRemoteUrl,
  testPlcConnection,
  getAppStoreBootstrap,
  getAppStoreTenantContext,
  saveAppStoreBootstrap,
  appendAppStoreHistorian,
  appendAppStoreLogs,
  getAppStoreLive,
  getAppStoreHistorian,
  getAppStoreLogs,
  getAppStoreInspector,
  repairDatabaseRecovery,
  getRetentionPolicy,
  updateRetentionPolicy,
  runRetention,
  getRetentionRuns,
  getAppStoreBackups,
  createAppStoreBackup,
  restoreAppStoreBackup,
  deleteAppStoreBackup,
  cleanupAppStoreData,
  forceAppStoreSyncNow,
  manualPeriodSyncAppStore,
  clearAppStoreSyncQueue,
  dropAppStoreSyncBacklog,
  resetAppStoreFull,
  sendNotificationEmail,
  loginAuth,
  getAuthMe,
  clearAuthToken,
  isForcedReadonlyCloudMode,
  setBackendTarget,
  testNotificationEmail,
  startGatewayInstance,
  stopAllGatewayInstances,
  stopGatewayInstance,
} from "./api";
import { Bar, BarChart, ComposedChart, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const THEME_STORAGE_KEY = "trustnode_theme";
const USERS_STORAGE_KEY = "trustnode_users";
const CURRENT_USER_STORAGE_KEY = "trustnode_current_user";
const DEVICES_STORAGE_KEY = "trustnode_devices";
const DB_CONNECTIONS_STORAGE_KEY = "trustnode_db_connections";
const GATEWAY_CONFIGS_STORAGE_KEY = "trustnode_gateway_configs";
const TRIGGER_RULES_STORAGE_KEY = "trustnode_trigger_rules";
const COLLECTION_TRIGGERS_STORAGE_KEY = "trustnode_collection_triggers";
const DASHBOARD_WIDGETS_STORAGE_KEY = "trustnode_dashboard_widgets";
const DASHBOARD_LAYOUT_STORAGE_KEY = "trustnode_dashboard_layout";
const EMAIL_SETTINGS_STORAGE_KEY = "trustnode_email_settings";
const DEFAULT_LOCAL_DB_BADGE_DISMISS_KEY = "trustnode_default_local_db_badge_dismissed";
const LOCAL_DB_ENGINES = new Set(["sqlite", "csv_file", "txt_file"]);
const MAIN_LOCAL_SQLITE_FALLBACK_ID = "__main_local_sqlite__";
const KNOWN_SUPABASE_DEFAULTS = {
  host: "aws-1-eu-west-1.pooler.supabase.com",
  port: "6543",
  database: "postgres",
  username: "postgres.tsfreqjcrgbxdwvmxeuk",
  password: "Apolo020@25t",
  schema: "public",
  table: "plc_readings",
  source: "edge-01",
  site: "Limerick",
  area: "LineA",
  equipment: "MACHINE-01",
};
const KNOWN_SUPABASE_POOLER_HOST = "aws-1-eu-west-1.pooler.supabase.com";

const NAV_SECTIONS = [
  { id: "overview", title: "Overview", items: ["Dashboard"] },
  {
    id: "collection_monitoring",
    title: "Collection and Monitoring",
    items: ["Gateway Configuration", "Devices", "Tags", "Triggers and Limits"]
  },
  { id: "notifications", title: "Notifications", items: ["Alarms", "Reporting"] },
  { id: "data_log", title: "Data History", items: ["Historian", "Logs"] },
  {
    id: "settings",
    title: "Database and Backup",
    items: ["Database Overview", "Backup and Retention"]
  },
  {
    id: "administration",
    title: "Settings",
    items: ["Users and Access Control", "Email and Notifications", "Scheduled Reports"]
  }
];

const gatewayOptions = [
  { value: "allen_bradley", label: "Allen-Bradley" },
  { value: "siemens_snap7", label: "Siemens Snap7" },
  { value: "siemens_opcua", label: "Siemens OPC-UA" },
  { value: "boston", label: "Boston" }
];

function pageId(label) {
  if (label.toLowerCase() === "database overview") return "database";
  if (label.toLowerCase() === "historian") return "historian";
  if (label.toLowerCase() === "logs") return "logs";
  return label.toLowerCase().replace(/\s+/g, "_");
}

function pageTitle(page) {
  if (page === "database") return "Database Overview";
  if (page === "historian") return "Historian";
  if (page === "logs") return "Logs";
  if (page === "database_overview") return "Database Overview";
  if (page === "database_inspector") return "Database Inspector";
  if (page === "backup_and_retention") return "Backup and Retention";
  if (page === "website_and_env") return "Website and Environment";
  if (page === "email_and_notifications") return "Email and Notifications";
  if (page === "scheduled_reports") return "Scheduled Reports";
  return page.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
}

function buildOpcUrlFromIp(ip) {
  const host = (ip || "").trim();
  if (!host) return "";
  return `opc.tcp://${host}:4840`;
}

const DEFAULT_OPC_NODE_ID = "";
const REPORT_SERIES_COLORS = ["#16a34a", "#2563eb", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#0ea5e9", "#f59e0b"];
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
const GATEWAY_STATUS_POLL_MS_LOCAL = 2000;
const GATEWAY_STATUS_POLL_MS_CLOUD = 1000;
const CLOUD_LIVE_POLL_MS = 1000;
const CLOUD_AUX_POLL_MS = 3000;
const CLOUD_EDGE_ALL_KEY = "__all_edges__";
const RETENTION_PRESETS = {
  day: {
    key: "day",
    label: "Last day",
    raw_keep_days: 1,
    minute_keep_days: 3,
    hour_keep_days: 7,
    day_keep_days: 60,
  },
  week: {
    key: "week",
    label: "Last week",
    raw_keep_days: 7,
    minute_keep_days: 21,
    hour_keep_days: 90,
    day_keep_days: 365,
  },
  month: {
    key: "month",
    label: "Last month",
    raw_keep_days: 30,
    minute_keep_days: 90,
    hour_keep_days: 365,
    day_keep_days: 730,
  },
};

function detectRetentionPreset(policy) {
  const rawDays = Number(policy?.raw_keep_days || 0);
  if (rawDays <= 1) return "day";
  if (rawDays <= 7) return "week";
  if (rawDays <= 30) return "month";
  return "month";
}

function inferSupabaseProjectRef(db) {
  const username = String(db?.username || "").trim().toLowerCase();
  if (username.includes(".")) {
    const maybeRef = username.split(".").slice(1).join(".");
    if (/^[a-z0-9]{10,}$/.test(maybeRef)) return maybeRef;
  }
  const host = String(db?.host || "").trim().toLowerCase();
  const dbMatch = host.match(/^db\.([a-z0-9]{10,})\.supabase\.co$/);
  if (dbMatch?.[1]) return dbMatch[1];
  return "";
}

function resolveSupabaseConnectionProfile(mode, hasIpv4AddOn, baseDb) {
  const requestedMode = String(mode || "auto").toLowerCase();
  const effectiveMode = requestedMode === "auto"
    ? (hasIpv4AddOn ? "direct_ipv4" : "session_pooler")
    : requestedMode;
  const projectRef = inferSupabaseProjectRef(baseDb);
  const directHost = projectRef ? `db.${projectRef}.supabase.co` : "";
  if (effectiveMode === "direct_ipv4") {
    return {
      effectiveMode,
      host: directHost || String(baseDb?.host || KNOWN_SUPABASE_POOLER_HOST),
      port: "5432",
      summary: directHost
        ? "Direct PostgreSQL (IPv4 add-on)"
        : "Direct mode selected (project ref not inferred; verify host)",
    };
  }
  if (effectiveMode === "transaction_pooler") {
    return {
      effectiveMode,
      host: String(baseDb?.host || KNOWN_SUPABASE_POOLER_HOST),
      port: "6543",
      summary: "Transaction pooler (serverless/short-lived)",
    };
  }
  return {
    effectiveMode: "session_pooler",
    host: String(baseDb?.host || KNOWN_SUPABASE_POOLER_HOST),
    port: "5432",
    summary: "Session pooler (persistent edge writers)",
  };
}

function normalizeSupabaseDirectUsername(engine, host, port, username) {
  const isPg = String(engine || "").toLowerCase() === "postgresql";
  const hostText = String(host || "").trim().toLowerCase();
  const portNum = Number(port || 0);
  const userText = String(username || "").trim();
  const isSupabaseDirect = isPg && hostText.startsWith("db.") && hostText.endsWith(".supabase.co") && portNum === 5432;
  if (!isSupabaseDirect) return userText;
  return "postgres";
}

class AppErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, message: String(error?.message || error || "Unknown UI error") };
  }

  componentDidCatch(error, info) {
    try {
      localStorage.setItem("trustnode_ui_last_error", JSON.stringify({
        ts: new Date().toISOString(),
        message: String(error?.message || error || ""),
        stack: String(error?.stack || ""),
        component: String(info?.componentStack || "")
      }));
    } catch {}
  }

  clearUiCacheAndReload = () => {
    try {
      const keys = [
        USERS_STORAGE_KEY,
        CURRENT_USER_STORAGE_KEY,
        DEVICES_STORAGE_KEY,
        DB_CONNECTIONS_STORAGE_KEY,
        GATEWAY_CONFIGS_STORAGE_KEY,
        TRIGGER_RULES_STORAGE_KEY,
        COLLECTION_TRIGGERS_STORAGE_KEY,
        DASHBOARD_WIDGETS_STORAGE_KEY,
        DASHBOARD_LAYOUT_STORAGE_KEY,
      ];
      for (const k of keys) localStorage.removeItem(k);
      localStorage.removeItem("trustnode_ui_last_error");
    } catch {}
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="loading">
          <div className="loading-card" style={{ width: "min(680px, 92vw)" }}>
            <div className="loading-title">Frontend Error Recovered</div>
            <div className="loading-error">{this.state.message}</div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-primary" type="button" onClick={() => window.location.reload()}>
                Reload
              </button>
              <button className="btn btn-danger" type="button" onClick={this.clearUiCacheAndReload}>
                Clear UI Cache + Reload
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function tsNow() {
  return new Date().toISOString();
}

function rowTsMs(row) {
  const raw = String(row?.ts || row?.ts_utc || "").trim();
  if (!raw) return Number.NaN;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : Number.NaN;
}

function mergeHistorianRowsStable(incomingRows, prevRows, limit = 5000) {
  const incoming = Array.isArray(incomingRows) ? incomingRows : [];
  const prev = Array.isArray(prevRows) ? prevRows : [];
  if (!incoming.length) return prev;
  const newestIncomingMs = incoming.reduce((max, r) => {
    const ms = rowTsMs(r);
    return Number.isFinite(ms) && ms > max ? ms : max;
  }, -1);
  const newestPrevMs = prev.reduce((max, r) => {
    const ms = rowTsMs(r);
    return Number.isFinite(ms) && ms > max ? ms : max;
  }, -1);
  if (newestPrevMs > 0 && newestIncomingMs > 0 && newestIncomingMs < newestPrevMs - 1500) {
    return prev;
  }
  const merged = [];
  const seen = new Set();
  const combined = [...incoming, ...prev];
  for (const r of combined) {
    const key = `${String(r?.gateway_id || "")}::${String(r?.tag || r?.tag_name || "")}::${String(r?.ts || r?.ts_utc || "")}`;
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(r);
    if (merged.length >= limit) break;
  }
  return merged;
}

function formatElapsedFromUtc(rawTs) {
  const txt = String(rawTs || "").trim();
  if (!txt) return "-";
  const ms = Date.parse(txt);
  if (!Number.isFinite(ms)) return "-";
  const diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const mins = Math.floor(diffSec / 60);
  const secs = diffSec % 60;
  if (mins < 60) return `${mins}m ${secs}s ago`;
  const hrs = Math.floor(mins / 60);
  const remMin = mins % 60;
  return `${hrs}h ${remMin}m ago`;
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function parseSemicolonTags(raw) {
  return String(raw || "")
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseGatewayTagsByType(gatewayType, raw) {
  if (String(gatewayType || "").trim() === "siemens_opcua") {
    return parseOpcNodeIds(raw);
  }
  return parseSemicolonTags(raw);
}

function parseOpcNodeIds(raw) {
  const text = String(raw || "");
  const explicitNodeIds = Array.from(
    text.matchAll(/ns=\d+;(?:s="[^"]+"|s=[^,\n;|]+|i=\d+|g=[0-9a-fA-F-]+|b=[^,\n;|]+)/g)
  ).map((m) => m[0].trim());
  if (explicitNodeIds.length) return Array.from(new Set(explicitNodeIds));
  return text
    .split(/[,\n;|]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function qualityLabelFromCode(raw) {
  const q = Number(raw);
  if (Number.isNaN(q)) return "UNKNOWN";
  if (q >= 192) return "GOOD";
  if (q >= 64) return "UNCERTAIN";
  if (q >= 0) return "BAD";
  return "UNKNOWN";
}

function normalizeTagName(raw) {
  return String(raw || "").trim().toLowerCase();
}

function normalizeHexColor(raw, fallback = "#16a34a") {
  const v = String(raw || "").trim();
  if (HEX_COLOR_RE.test(v)) return v.toLowerCase();
  return String(fallback || "#16a34a");
}

function sanitizeReportDocuments(rawDocs) {
  if (!Array.isArray(rawDocs)) return [];
  return rawDocs
    .filter((d) => d && typeof d === "object")
    .map((d, idx) => ({
      id: String(d.id || `doc-${idx}`),
      created_utc: String(d.created_utc || ""),
      generated_by: String(d.generated_by || ""),
      summary: String(d.summary || ""),
      csv_content: String(d.csv_content || ""),
      html_content: String(d.html_content || ""),
      row_count: Number(d.row_count || 0),
      size_bytes: Number(d.size_bytes || 0),
      columns: Array.isArray(d.columns) ? d.columns.map((x) => String(x)) : [],
      preview_rows: Array.isArray(d.preview_rows) ? d.preview_rows : [],
      chart_series: Array.isArray(d.chart_series) ? d.chart_series : []
    }));
}

function toStringArray(value) {
  if (Array.isArray(value)) return value.map((x) => String(x));
  if (value === undefined || value === null || value === "") return [];
  return [String(value)];
}

function toObjectMap(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function normalizeReportFiltersShape(value, fallback = null) {
  const base = fallback && typeof fallback === "object" ? fallback : {};
  const src = value && typeof value === "object" ? value : {};
  return {
    ...base,
    ...src,
    from: String(src.from ?? base.from ?? ""),
    to: String(src.to ?? base.to ?? ""),
    batch: String(src.batch ?? base.batch ?? ""),
    max_rows: Math.max(200, Number(src.max_rows ?? base.max_rows ?? 3000) || 3000),
    selected_gateway_ids: toStringArray(src.selected_gateway_ids ?? src.gatewayId ?? base.selected_gateway_ids),
    selected_tags: toStringArray(src.selected_tags ?? src.tag ?? base.selected_tags),
    tag_axes: toObjectMap(src.tag_axes ?? base.tag_axes),
    tag_colors: toObjectMap(src.tag_colors ?? base.tag_colors),
    report_chart_type: (src.report_chart_type ?? base.report_chart_type) === "bar" ? "bar" : "line",
    selected_devices: toStringArray(src.selected_devices ?? src.device ?? base.selected_devices),
    selected_databases: toStringArray(src.selected_databases ?? base.selected_databases),
    selected_plc_ips: toStringArray(src.selected_plc_ips ?? base.selected_plc_ips),
  };
}

function dbLocationFromEngine(engine) {
  return LOCAL_DB_ENGINES.has(String(engine || "").toLowerCase()) ? "local" : "remote";
}

function normalizeDbConnection(conn) {
  const c = conn && typeof conn === "object" ? conn : {};
  const engine = String(c.engine || "postgresql");
  return {
    ...c,
    engine,
    enabled: c.enabled !== false,
    use_gateway: c.use_gateway !== false,
    use_app: Boolean(c.use_app),
    use_backup: Boolean(c.use_backup),
    cloud_sync_enabled: Boolean(c.cloud_sync_enabled),
    location: c.location === "local" || c.location === "remote" ? c.location : dbLocationFromEngine(engine),
  };
}

function normalizeDbConnections(list) {
  if (!Array.isArray(list)) return [];
  return list.map((item) => normalizeDbConnection(item));
}

function MenuIcon({ page }) {
  const common = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2" };
  switch (page) {
    case "dashboard":
      return <svg {...common}><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="10" width="7" height="11" /><rect x="3" y="12" width="7" height="9" /></svg>;
    case "devices":
      return <svg {...common}><rect x="2" y="6" width="14" height="12" rx="2" /><rect x="6" y="2" width="16" height="12" rx="2" /></svg>;
    case "tags":
      return <svg {...common}><path d="M20 10l-8 8-8-8V4h6z" /><circle cx="7.5" cy="7.5" r="1.2" /></svg>;
    case "triggers_and_limits":
      return <svg {...common}><path d="M12 3v18" /><path d="M5 9h14" /><path d="M5 15h14" /></svg>;
    case "alarms":
      return <svg {...common}><path d="M12 9v4" /><circle cx="12" cy="17" r="1" /><path d="M10 3h4l7 13H3z" /></svg>;
    case "reporting":
      return <svg {...common}><path d="M4 20h16" /><path d="M7 16V8" /><path d="M12 16V4" /><path d="M17 16v-6" /></svg>;
    case "data_log":
      return <svg {...common}><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></svg>;
    case "historian":
      return <svg {...common}><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></svg>;
    case "logs":
      return <svg {...common}><path d="M4 4h16v12H4z" /><path d="M8 20h8" /><path d="M9 8h6M9 12h6" /></svg>;
    case "gateway_configuration":
      return <svg {...common}><circle cx="12" cy="12" r="3" /><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.5-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5L9 6a7 7 0 0 0-1.7 1l-2.4-1-2 3.5L5 11a7 7 0 0 0 0 2l-2.1 1.5 2 3.5 2.4-1a7 7 0 0 0 1.7 1l.5 3h5l.5-3a7 7 0 0 0 1.7-1l2.4 1 2-3.5L18.9 13c.1-.3.1-.7.1-1z" /></svg>;
    case "database":
      return <svg {...common}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v12c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11c0 1.7 3.6 3 8 3s8-1.3 8-3" /></svg>;
    case "database_overview":
      return <svg {...common}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v12c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11c0 1.7 3.6 3 8 3s8-1.3 8-3" /><path d="M8 16h8" /></svg>;
    case "database_inspector":
      return <svg {...common}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v12c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11c0 1.7 3.6 3 8 3s8-1.3 8-3" /><circle cx="18" cy="18" r="3" /><path d="M20.2 20.2L22 22" /></svg>;
    case "backup_and_retention":
      return <svg {...common}><path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8" /><path d="M1 8h22" /><path d="M8 8V4h8v4" /><path d="M12 12v6" /><path d="M9 15h6" /></svg>;
    case "website_and_env":
      return <svg {...common}><path d="M3 5h18v14H3z" /><path d="M3 9h18" /><path d="M8 4v2M16 4v2" /></svg>;
    case "email_and_notifications":
      return <svg {...common}><path d="M4 6h16v12H4z" /><path d="M4 8l8 6 8-6" /></svg>;
    case "scheduled_reports":
      return <svg {...common}><circle cx="12" cy="12" r="8" /><path d="M12 8v5l3 2" /></svg>;
    case "frontend_source":
      return <svg {...common}><path d="M3 12h6l3-8 3 16 3-8h3" /></svg>;
    case "users_and_access_control":
      return <svg {...common}><circle cx="9" cy="8" r="3" /><path d="M3 20a6 6 0 0 1 12 0" /><circle cx="18" cy="8" r="2" /><path d="M15 20a4 4 0 0 1 6 0" /></svg>;
    default:
      return <svg {...common}><circle cx="12" cy="12" r="8" /></svg>;
  }
}

function ThemeIcon({ theme }) {
  const common = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2" };
  if (theme === "dark") {
    return <svg {...common}><circle cx="12" cy="12" r="5" /><path d="M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" /></svg>;
  }
  return <svg {...common}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>;
}

function SwitchUserIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M16 3h5v5" />
      <path d="M8 21H3v-5" />
      <path d="M21 8a9 9 0 0 0-14-3" />
      <path d="M3 16a9 9 0 0 0 14 3" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </svg>
  );
}

function HamburgerIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 6h18" />
      <path d="M3 12h18" />
      <path d="M3 18h18" />
    </svg>
  );
}

function AddIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}

function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
    </svg>
  );
}

function DeleteIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

function SaveIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <path d="M17 21v-8H7v8" />
      <path d="M7 3v5h8" />
    </svg>
  );
}

function StartIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none">
      <rect x="6" y="6" width="12" height="12" />
    </svg>
  );
}

function FullscreenIcon({ active }) {
  if (active) {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M8 3H3v5" />
        <path d="M21 8V3h-5" />
        <path d="M3 16v5h5" />
        <path d="M16 21h5v-5" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M15 3h6v6" />
      <path d="M9 21H3v-6" />
      <path d="M21 3l-7 7" />
      <path d="M3 21l7-7" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M15 17h5l-1.4-1.4a2 2 0 0 1-.6-1.4V11a6 6 0 1 0-12 0v3.2a2 2 0 0 1-.6 1.4L4 17h5" />
      <path d="M9 17a3 3 0 0 0 6 0" />
    </svg>
  );
}

function EyeIcon({ open }) {
  if (open) {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 3l18 18" />
      <path d="M10.6 10.6a3 3 0 0 0 4.2 4.2" />
      <path d="M9.9 5.2A10.9 10.9 0 0 1 12 5c6 0 10 7 10 7a18.5 18.5 0 0 1-4 4.9" />
      <path d="M6.1 6.1A18.7 18.7 0 0 0 2 12s4 7 10 7a9.9 9.9 0 0 0 4.1-.9" />
    </svg>
  );
}

function ChartIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 3v18h18" />
      <path d="M7 15l4-4 3 3 5-6" />
    </svg>
  );
}

function KpiIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 20V4" />
      <path d="M4 20h16" />
      <rect x="7" y="11" width="3" height="6" />
      <rect x="12" y="8" width="3" height="9" />
      <rect x="17" y="5" width="3" height="12" />
    </svg>
  );
}

function ListIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M8 6h13" />
      <path d="M8 12h13" />
      <path d="M8 18h13" />
      <circle cx="4" cy="6" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="4" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="4" cy="18" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  );
}

function MoveUpIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 19V5" />
      <path d="M6 11l6-6 6 6" />
    </svg>
  );
}

function MoveDownIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 5v14" />
      <path d="M18 13l-6 6-6-6" />
    </svg>
  );
}

function PreviewIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function PdfIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
      <path d="M7 15h2a1 1 0 0 0 0-2H7v4" />
      <path d="M12 17h1.5a1.5 1.5 0 0 0 0-3H12z" />
      <path d="M17 13h-2v4" />
    </svg>
  );
}

function CsvIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
      <path d="M8 14h8M8 18h8" />
      <path d="M8 10h4" />
    </svg>
  );
}

function getInitialTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

const PERMISSION_LABELS = {
  devices: "Devices",
  tags: "Tags",
  triggers_and_limits: "Triggers and Limits",
  alarms: "Alarms",
  reporting: "Reporting",
  data_log: "Data History",
  gateway_configuration: "Gateway Configuration",
  gateway_runtime_control: "Gateway Start/Stop",
  database: "Database Overview",
  database_overview: "Database Overview (Legacy)",
  database_inspector: "Database Inspector",
  backup_and_retention: "Backup and Retention",
  email_and_notifications: "Email and Notifications",
  scheduled_reports: "Scheduled Reports",
  users_and_access_control: "Users and Access"
};

const PERMISSION_GROUPS = [
  { title: "Collection and Monitoring", items: ["devices", "tags", "triggers_and_limits", "gateway_configuration"] },
  { title: "Operations", items: ["gateway_runtime_control", "alarms", "reporting", "data_log"] },
  { title: "Administration", items: ["database", "backup_and_retention", "email_and_notifications", "scheduled_reports", "users_and_access_control"] }
];

function compareByOperator(value, operator, threshold) {
  if (operator === "<") return value < threshold;
  if (operator === "<=") return value <= threshold;
  if (operator === ">") return value > threshold;
  if (operator === ">=") return value >= threshold;
  return false;
}

function buildRolePermissions(role) {
  if (role === "admin") {
    return {
      devices: true,
      tags: true,
      triggers_and_limits: true,
      alarms: true,
      reporting: true,
      data_log: true,
      gateway_configuration: true,
      gateway_runtime_control: true,
      database: true,
      database_overview: true,
      database_inspector: true,
      backup_and_retention: true,
      website_and_env: true,
      email_and_notifications: true,
      scheduled_reports: true,
      frontend_source: true,
      users_and_access_control: true
    };
  }
  if (role === "engineer") {
    return {
      devices: true,
      tags: true,
      triggers_and_limits: true,
      alarms: true,
      reporting: true,
      data_log: true,
      gateway_configuration: true,
      gateway_runtime_control: true,
      database: false,
      database_overview: false,
      database_inspector: false,
      backup_and_retention: false,
      website_and_env: false,
      email_and_notifications: false,
      scheduled_reports: false,
      frontend_source: false,
      users_and_access_control: false
    };
  }
  if (role === "operator") {
    return {
      devices: true,
      tags: true,
      triggers_and_limits: true,
      alarms: true,
      reporting: false,
      data_log: true,
      gateway_configuration: false,
      gateway_runtime_control: true,
      database: false,
      database_overview: false,
      database_inspector: false,
      backup_and_retention: false,
      website_and_env: false,
      email_and_notifications: false,
      scheduled_reports: false,
      frontend_source: false,
      users_and_access_control: false
    };
  }
  return {
    devices: false,
    tags: false,
    triggers_and_limits: false,
    alarms: false,
    reporting: false,
    data_log: true,
    gateway_configuration: false,
    gateway_runtime_control: false,
    database: false,
    database_overview: false,
    database_inspector: false,
    backup_and_retention: false,
    website_and_env: false,
    email_and_notifications: false,
    scheduled_reports: false,
    frontend_source: false,
    users_and_access_control: false
  };
}

function normalizePermissions(rawPermissions, role) {
  return {
    ...buildRolePermissions(role),
    ...(rawPermissions || {})
  };
}

function buildDefaultUsers() {
  return [
    {
      username: "admin",
      password: "admin",
      role: "admin",
      permissions: buildRolePermissions("admin")
    },
    {
      username: "operator_01",
      password: "operator",
      role: "operator",
      permissions: buildRolePermissions("operator")
    }
  ];
}

function AppShell() {
  const isReadonlyCloudMode = isForcedReadonlyCloudMode();
  const browserProtocol = String(window.location.protocol || "").toLowerCase();
  const browserHost = String(window.location.hostname || "").toLowerCase();
  const isLocalHost = browserHost === "localhost" || browserHost === "127.0.0.1" || browserHost === "::1";
  const isHostedWebClient = (browserProtocol === "https:" || browserProtocol === "http:") && !isLocalHost;
  const getFullscreenState = () => {
    const doc = document;
    return Boolean(doc.fullscreenElement || doc.webkitFullscreenElement);
  };
  const [status, setStatus] = useState(null);
  const [gatewayRuntimeStatuses, setGatewayRuntimeStatuses] = useState({});
  const [config, setConfig] = useState(null);
  const [error, setError] = useState("");
  const [readings, setReadings] = useState([]);
  const [history, setHistory] = useState([]);
  const [trendChartType, setTrendChartType] = useState("line");
  const [tagMonitorChartType, setTagMonitorChartType] = useState("line");
  const [wsState, setWsState] = useState("connecting");
  const [cloudStreamConnected, setCloudStreamConnected] = useState(false);
  const [bootState, setBootState] = useState("initializing");
  const [endpointMode, setEndpointMode] = useState("local");
  const [cloudUrl, setCloudUrl] = useState("");
  const [selectedCloudEdgeKey, setSelectedCloudEdgeKey] = useState(CLOUD_EDGE_ALL_KEY);
  const [edgeLinkState, setEdgeLinkState] = useState({ state: "unknown", message: "Not checked" });
  const [endpointVersion, setEndpointVersion] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [activePage, setActivePage] = useState("dashboard");
  const [expandedSections, setExpandedSections] = useState({
    overview: false,
    collection_monitoring: false,
    notifications: false,
    data_log: false,
    settings: false,
    administration: false
  });
  const [theme, setTheme] = useState("light");

  const [alarms, setAlarms] = useState([]);
  const [selectedAlarmIds, setSelectedAlarmIds] = useState([]);
  const [dataLog, setDataLog] = useState([]);
  const [appLogs, setAppLogs] = useState([]);
  const [historianFilters, setHistorianFilters] = useState({
    from: "",
    to: "",
    tag: "",
    gatewayId: "",
    deviceName: "",
    quality: "all"
  });
  const [logFilters, setLogFilters] = useState({
    from: "",
    to: "",
    level: "all",
    category: "all",
    gatewayId: "",
    text: ""
  });
  const [tagFilters, setTagFilters] = useState({
    gatewayId: "",
    device: "",
    tag: "",
    value: ""
  });
  const [dbConnections, setDbConnections] = useState([]);
  const [gatewayConfigs, setGatewayConfigs] = useState([]);
  const [selectedGatewayId, setSelectedGatewayId] = useState("");
  const [showGatewayModal, setShowGatewayModal] = useState(false);
  const [editingGatewayId, setEditingGatewayId] = useState(null);
  const [gatewayDiscoverBusy, setGatewayDiscoverBusy] = useState(false);
  const [gatewayDiscoverResult, setGatewayDiscoverResult] = useState("");
  const [gatewayDiscoveredTags, setGatewayDiscoveredTags] = useState([]);
  const [gatewayOpcBrowseNodes, setGatewayOpcBrowseNodes] = useState([]);
  const [gatewayOpcValidationBusy, setGatewayOpcValidationBusy] = useState(false);
  const [gatewayOpcValidationResult, setGatewayOpcValidationResult] = useState("");
  const [gatewayOpcValidationRows, setGatewayOpcValidationRows] = useState([]);
  const [gatewayOpcValidatedFor, setGatewayOpcValidatedFor] = useState("");
  const [gatewaySelectedTags, setGatewaySelectedTags] = useState([]);
  const [gatewayForm, setGatewayForm] = useState({
    name: "",
    device_id: "",
    gateway_type: "allen_bradley",
    plc_ip: "",
    opc_url: "",
    database_id: "",
    interval_ms: 1000,
    tags_text: ""
  });
  const [showDbModal, setShowDbModal] = useState(false);
  const [editingDbId, setEditingDbId] = useState(null);
  const [dbModalPresetScope, setDbModalPresetScope] = useState("gateway");
  const [showTagMonitorModal, setShowTagMonitorModal] = useState(false);
  const [tagMonitorSelection, setTagMonitorSelection] = useState(null);
  const [dashboardWidgets, setDashboardWidgets] = useState([]);
  const [dashboardMode, setDashboardMode] = useState("kpi");
  const [dashboardPerRow, setDashboardPerRow] = useState(2);
  const [showDashboardWidgetModal, setShowDashboardWidgetModal] = useState(false);
  const [editingDashboardWidgetId, setEditingDashboardWidgetId] = useState(null);
  const [dashboardWidgetForm, setDashboardWidgetForm] = useState({
    title: "",
    gateway_id: "",
    tag_name: "",
    readings_count: 120,
    color: "#16a34a",
    chart_type: "line"
  });
  const [collectionTriggers, setCollectionTriggers] = useState([]);
  const [collectionTriggerMode, setCollectionTriggerMode] = useState("any");
  const [showCollectionTriggerModal, setShowCollectionTriggerModal] = useState(false);
  const [editingCollectionTriggerId, setEditingCollectionTriggerId] = useState(null);
  const [collectionTriggerForm, setCollectionTriggerForm] = useState({
    gateway_id: "",
    tag_name: "",
    operator: ">=",
    value: "",
    trigger_type: "continuous",
    enabled: true
  });
  const [triggerRules, setTriggerRules] = useState([]);
  const [showTriggerModal, setShowTriggerModal] = useState(false);
  const [editingTriggerId, setEditingTriggerId] = useState(null);
  const [triggerForm, setTriggerForm] = useState({
    gateway_id: "",
    tag_name: "",
    lower_enabled: false,
    lower_operator: "<",
    lower_value: "",
    upper_enabled: true,
    upper_operator: ">=",
    upper_value: "",
    enabled: true
  });
  const [dbForm, setDbForm] = useState({
    name: "",
    engine: "mysql",
    host: "127.0.0.1",
    port: "3306",
    database: "",
    username: "",
    password: "",
    sqlite_path: "./data/trustnode_edge.db",
    file_path: "./data/trustnode_log.csv",
    legacy_url: "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php",
    legacy_api_token: "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3",
    source: "edge-01",
    site: "Limerick",
    area: "LineA",
    equipment: "MACHINE-01",
    schema: "",
    table: "",
    tls: true,
    enabled: true,
    use_gateway: true,
    use_app: false,
    use_backup: false,
    cloud_sync_enabled: false
  });
  const [dbTestBusy, setDbTestBusy] = useState(false);
  const [dbTestResult, setDbTestResult] = useState(null);
  const [dbProvisionBusy, setDbProvisionBusy] = useState(false);
  const [footerCollapsed, setFooterCollapsed] = useState(true);
  const [footerHeight, setFooterHeight] = useState(0);
  const [confirmDialog, setConfirmDialog] = useState({
    open: false,
    title: "",
    message: "",
    onConfirm: null
  });
  const [uiSourceMode, setUiSourceMode] = useState("local");
  const [uiSourceRemoteUrl, setUiSourceRemoteUrl] = useState("");
  const [uiSourceLocalPath, setUiSourceLocalPath] = useState("");
  const [uiSourceSavedMessage, setUiSourceSavedMessage] = useState("");
  const [uiSourceTestResult, setUiSourceTestResult] = useState("");
  const [websiteEnvText, setWebsiteEnvText] = useState("");
  const [websiteStatusResult, setWebsiteStatusResult] = useState("");
  const [databaseOverviewResult, setDatabaseOverviewResult] = useState("");
  const [databaseInspector, setDatabaseInspector] = useState(null);
  const [databaseInspectorBusy, setDatabaseInspectorBusy] = useState(false);
  const [databaseInspectorError, setDatabaseInspectorError] = useState("");
  const [appMetadata, setAppMetadata] = useState({});
  const [showDefaultLocalDbBadge, setShowDefaultLocalDbBadge] = useState(false);
  const [forceSyncBusy, setForceSyncBusy] = useState(false);
  const [forceSyncResult, setForceSyncResult] = useState("");
  const [retentionPolicy, setRetentionPolicy] = useState({
    enabled: false,
    schedule_minutes: 60,
    raw_keep_days: 7,
    minute_keep_days: 30,
    hour_keep_days: 180,
    day_keep_days: 730,
    backup_before_cleanup: true,
    max_delete_rows_per_run: 50000
  });
  const [retentionPresetKey, setRetentionPresetKey] = useState("week");
  const [cloudProviderDbId, setCloudProviderDbId] = useState("");
  const [currentTenantId, setCurrentTenantId] = useState("default");
  const [cloudAutoSyncEnabled, setCloudAutoSyncEnabled] = useState(true);
  const [tenantWebClientUrl, setTenantWebClientUrl] = useState("https://trustnode.lsapps.app");
  const [tenantCompanyName, setTenantCompanyName] = useState("");
  const [tenantLoginRealm, setTenantLoginRealm] = useState("");
  const [cloudSupabaseMode, setCloudSupabaseMode] = useState("auto");
  const [cloudSupabaseHasIpv4AddOn, setCloudSupabaseHasIpv4AddOn] = useState(true);
  const [cloudSupabaseApplyResult, setCloudSupabaseApplyResult] = useState("");
  const [dolibarrMirrorEnabled, setDolibarrMirrorEnabled] = useState(false);
  const [trustnodeCloudEnabled, setTrustnodeCloudEnabled] = useState(true);
  const [selectedCloudDbId, setSelectedCloudDbId] = useState("");
  const [selectedOtherDbId, setSelectedOtherDbId] = useState("");
  const [selectedLocalDbId, setSelectedLocalDbId] = useState("");
  const [showCloudDbPickerModal, setShowCloudDbPickerModal] = useState(false);
  const [showCloudSyncModal, setShowCloudSyncModal] = useState(false);
  const [cloudSyncForm, setCloudSyncForm] = useState({
    from_utc: "",
    to_utc: "",
    max_rows: 20000,
    include_logs: false,
    clear_queue_after: false,
    drop_backlog_after: false,
  });
  const [showOtherDbPickerModal, setShowOtherDbPickerModal] = useState(false);
  const [cloudDbPickerType, setCloudDbPickerType] = useState("supabase");
  const [otherDbPickerType, setOtherDbPickerType] = useState("sqlite");
  const [retentionRuns, setRetentionRuns] = useState([]);
  const [retentionBusy, setRetentionBusy] = useState(false);
  const [retentionResult, setRetentionResult] = useState("");
  const [backupRows, setBackupRows] = useState([]);
  const [backupBusy, setBackupBusy] = useState(false);
  const [backupResult, setBackupResult] = useState("");
  const [selectedBackupFilename, setSelectedBackupFilename] = useState("");
  const [cleanupMode, setCleanupMode] = useState("last_day");
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [cleanupResult, setCleanupResult] = useState("");
  const [retentionResultScope, setRetentionResultScope] = useState("global");
  const [cleanupResultScope, setCleanupResultScope] = useState("global");
  const [emailSettings, setEmailSettings] = useState({
    transport: "smtp",
    host: "",
    port: 587,
    username: "",
    password: "",
    sender_email: "",
    sender_name: "Trustnode Edge",
    use_tls: true,
    use_ssl: false,
    php_endpoint_url: "",
    php_api_token: "",
    php_auth_header: "X-API-TOKEN",
    php_timeout_ms: 6000,
    php_verify_tls: true,
    alarm_recipients: "",
    report_recipients: "",
    batch_recipients: "",
    alarm_subject: "[ALARM] {{gateway}} - {{tag}}",
    report_subject: "[REPORT] {{name}}",
    batch_subject: "[BATCH] {{name}}",
    alarm_template:
      "<h2 style='color:#dc2626'>Alarm Triggered</h2><p><b>Gateway:</b> {{gateway}}</p><p><b>Tag:</b> {{tag}}</p><p><b>Value:</b> {{value}}</p><p><b>Time:</b> {{ts}}</p>",
    report_template:
      "<h2>Scheduled Report</h2><p><b>Name:</b> {{name}}</p><p><b>Rows:</b> {{row_count}}</p><p><b>Created:</b> {{created_utc}}</p>",
    batch_template:
      "<h2>Batch Notification</h2><p><b>Name:</b> {{name}}</p><p><b>Status:</b> {{status}}</p><p><b>Time:</b> {{ts}}</p>"
  });
  const [emailTestTo, setEmailTestTo] = useState("");
  const [emailResult, setEmailResult] = useState("");
  const [emailProfiles, setEmailProfiles] = useState([]);
  const [activeEmailProfileId, setActiveEmailProfileId] = useState("");
  const [emailProfileName, setEmailProfileName] = useState("");
  const [emailTemplateView, setEmailTemplateView] = useState({
    alarm: "code",
    report: "code",
    batch: "code",
  });
  const [tagAlarmPrefs, setTagAlarmPrefs] = useState({});
  const [reportDocuments, setReportDocuments] = useState([]);
  const [reportLoadedRows, setReportLoadedRows] = useState([]);
  const [reportLoadedAt, setReportLoadedAt] = useState("");
  const [reportSummaryText, setReportSummaryText] = useState("");
  const [scheduledReports, setScheduledReports] = useState([]);
  const [reportFilters, setReportFilters] = useState({
    from: "",
    to: "",
    selected_gateway_ids: [],
    selected_tags: [],
    tag_axes: {},
    tag_colors: {},
    report_chart_type: "line",
    batch: "",
    max_rows: 3000
  });
  const [reportPreviewDoc, setReportPreviewDoc] = useState(null);
  const [showScheduleModal, setShowScheduleModal] = useState(false);
  const [editingScheduleId, setEditingScheduleId] = useState(null);
  const [liveTagValues, setLiveTagValues] = useState({});
  const [scheduleForm, setScheduleForm] = useState({
    name: "",
    enabled: true,
    recurrence: "daily",
    hour: "08",
    minute: "00",
    day_of_week: "1",
    day_of_month: "1",
    format: "csv",
    recipients: "",
    filters: { from: "", to: "", selected_gateway_ids: [], selected_tags: [], batch: "", max_rows: 3000 },
    last_run_utc: "",
    next_run_utc: ""
  });

  const [users, setUsers] = useState([]);
  const [currentUser, setCurrentUser] = useState(null);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(getFullscreenState);
  const [loginForm, setLoginForm] = useState({ username: "", password: "" });
  const [showLoginPassword, setShowLoginPassword] = useState(false);
  const [rememberUser, setRememberUser] = useState(true);
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [newUserForm, setNewUserForm] = useState({
    username: "",
    password: "",
    role: "viewer",
    permissions: buildRolePermissions("viewer")
  });
  const [showEditUserModal, setShowEditUserModal] = useState(false);
  const [editingUsername, setEditingUsername] = useState("");
  const [editUserForm, setEditUserForm] = useState({
    password: "",
    role: "viewer",
    permissions: buildRolePermissions("viewer")
  });
  const [devices, setDevices] = useState([]);
  const [showDeviceModal, setShowDeviceModal] = useState(false);
  const [editingDeviceId, setEditingDeviceId] = useState(null);
  const [deviceForm, setDeviceForm] = useState({
    name: "",
    gateway_type: "allen_bradley",
    plc_ip: "",
    opc_url: "",
    opc_node_id: DEFAULT_OPC_NODE_ID,
    opc_node_ids_text: DEFAULT_OPC_NODE_ID,
    notes: ""
  });
  const [deviceTestBusy, setDeviceTestBusy] = useState(false);
  const [deviceTestResult, setDeviceTestResult] = useState(null);
  const [appStoreHydrated, setAppStoreHydrated] = useState(false);
  const reconnectTimerRef = useRef(null);
  const footerRef = useRef(null);
  const historySeqRef = useRef(0);
  const userMenuRef = useRef(null);
  const devicesRef = useRef([]);
  const dbConnectionsRef = useRef([]);
  const gatewayConfigsRef = useRef([]);
  const collectionTriggersRef = useRef([]);
  const triggerRulesRef = useRef([]);
  const triggerActiveStateRef = useRef({});
  const alarmEmailGateRef = useRef({});
  const lastErrorLogRef = useRef("");
  const lastDbErrorLogRef = useRef("");
  const logDedupeRef = useRef({});
  const appStoreSaveTimerRef = useRef(null);
  const appStorePersistInFlightRef = useRef(false);
  const appStoreLastPersistSignatureRef = useRef("");
  const liveTagValuesRef = useRef({});
  const tagAlarmPrefsRef = useRef({});
  const emailSettingsRef = useRef({});
  const historianOutboxRef = useRef([]);
  const logsOutboxRef = useRef([]);
  const outboxFlushBusyRef = useRef(false);
  const dbRecoveryInFlightRef = useRef(false);
  const dbRecoveryLastSignatureRef = useRef("");
  const configRestartSignatureRef = useRef("");
  const configRestartInitializedRef = useRef(false);
  const configRestartBusyRef = useRef(false);
  const connectionLoopRef = useRef({
    devices: {},
    databases: {},
    gateways: {},
    gatewayErrors: {},
    dbErrors: {},
    collectionBlocks: {},
    wsState: "",
    bootState: "",
    lastHeartbeatMs: 0
  });
  const cloudStatusStabilityRef = useRef({
    gateway: {},
    device: {},
    database: {}
  });
  const [devicesSeeded, setDevicesSeeded] = useState(false);
  const [startupWarningsReady, setStartupWarningsReady] = useState(false);

  const buildAppStorePayload = () => ({
    app_settings: {
      theme,
      remember_user: rememberUser,
      endpoint_mode: endpointMode,
      cloud_url: cloudUrl,
      cloud_auto_sync_enabled: cloudAutoSyncEnabled,
      ui_source_mode: uiSourceMode,
      ui_source_remote_url: uiSourceRemoteUrl,
      ui_source_local_path: uiSourceLocalPath,
      website_env_text: websiteEnvText,
      tenant_web_client_url: tenantWebClientUrl,
      tenant_company_name: tenantCompanyName,
      tenant_login_realm: tenantLoginRealm,
      active_page: activePage
    },
    users_access: {
      users,
      current_user: currentUser?.username || ""
    },
    devices,
    gateway_configurations: gatewayConfigs,
    database_configurations: dbConnections,
    triggers_limits: {
      collection_triggers: collectionTriggers,
      collection_trigger_mode: collectionTriggerMode,
      trigger_rules: triggerRules
    },
    dashboard_configurations: {
      widgets: dashboardWidgets,
      mode: dashboardMode,
      per_row: dashboardPerRow
    },
    alarms_setup: {
      alarms
    },
    reporting_setup: {
      filters: reportFilters,
      documents: reportDocuments,
      schedules: scheduledReports
    },
    tags: {
      alarm_prefs: tagAlarmPrefs
    },
    email_notifications: {
      settings: emailSettings,
      profiles: emailProfiles,
      active_profile_id: activeEmailProfileId
    },
    metadata: {
      ...appMetadata,
      saved_utc: tsNow(),
      app_version: "edge-2026-02-21-db-primary-1"
    }
  });

  const applyAppStorePayload = (data) => {
    if (!data || typeof data !== "object") return;
    const appSettings = data.app_settings || {};
    const usersAccess = data.users_access || {};
    const triggers = data.triggers_limits || {};
    const dashboard = data.dashboard_configurations || {};
    const alarmsSetup = data.alarms_setup || {};
    const reportingSetup = data.reporting_setup || {};
    const tagsSetup = data.tags || {};
    const emailSetup = data.email_notifications || {};
    const metadata = data.metadata && typeof data.metadata === "object" && !Array.isArray(data.metadata) ? data.metadata : {};

    if (appSettings.theme === "light" || appSettings.theme === "dark") setTheme(appSettings.theme);
    if (typeof appSettings.remember_user === "boolean") setRememberUser(appSettings.remember_user);
    if (!isHostedWebClient && typeof appSettings.endpoint_mode === "string") {
      setEndpointMode(appSettings.endpoint_mode);
    }
    if (typeof appSettings.cloud_url === "string") {
      setCloudUrl(appSettings.cloud_url);
    }
    if (typeof appSettings.cloud_auto_sync_enabled === "boolean") {
      setCloudAutoSyncEnabled(appSettings.cloud_auto_sync_enabled);
    }
    if (isHostedWebClient) {
      const forcedCloud = String(appSettings.cloud_url || window.location.origin || "").trim().replace(/\/+$/, "");
      setEndpointMode("cloud");
      if (forcedCloud) setCloudUrl(forcedCloud);
      setBackendTarget("cloud", forcedCloud);
    }
    if (typeof appSettings.ui_source_mode === "string") setUiSourceMode(appSettings.ui_source_mode);
    if (typeof appSettings.ui_source_remote_url === "string") setUiSourceRemoteUrl(appSettings.ui_source_remote_url);
    if (typeof appSettings.ui_source_local_path === "string") setUiSourceLocalPath(appSettings.ui_source_local_path);
    if (typeof appSettings.website_env_text === "string") setWebsiteEnvText(appSettings.website_env_text);
    if (typeof appSettings.tenant_web_client_url === "string") setTenantWebClientUrl(appSettings.tenant_web_client_url);
    if (typeof appSettings.tenant_company_name === "string") setTenantCompanyName(appSettings.tenant_company_name);
    if (typeof appSettings.tenant_login_realm === "string") setTenantLoginRealm(appSettings.tenant_login_realm);
    if (typeof appSettings.active_page === "string") {
      const mappedPage = appSettings.active_page === "database_overview" ? "database" : appSettings.active_page;
      setActivePage(mappedPage);
    }

    if (Array.isArray(usersAccess.users)) {
      const normalizedUsers = usersAccess.users.map((u) => ({
        ...u,
        permissions: normalizePermissions(u.permissions, u.role)
      }));
      setUsers(normalizedUsers);
    }
    if (Array.isArray(data.devices)) setDevices(data.devices);
    if (Array.isArray(data.gateway_configurations)) setGatewayConfigs(data.gateway_configurations);
    if (Array.isArray(data.database_configurations)) setDbConnections(normalizeDbConnections(data.database_configurations));
    if (Array.isArray(triggers.collection_triggers)) setCollectionTriggers(triggers.collection_triggers);
    if (triggers.collection_trigger_mode === "any" || triggers.collection_trigger_mode === "all") {
      setCollectionTriggerMode(triggers.collection_trigger_mode);
    }
    if (Array.isArray(triggers.trigger_rules)) setTriggerRules(triggers.trigger_rules);
    if (Array.isArray(dashboard.widgets)) setDashboardWidgets(dashboard.widgets);
    if (dashboard.mode === "chart" || dashboard.mode === "kpi") setDashboardMode(dashboard.mode);
    if (dashboard.per_row !== undefined) {
      setDashboardPerRow(Math.min(4, Math.max(1, Number(dashboard.per_row || 2))));
    }
    if (Array.isArray(alarmsSetup.alarms)) setAlarms(alarmsSetup.alarms);
    if (reportingSetup.filters && typeof reportingSetup.filters === "object") {
      const incoming = reportingSetup.filters || {};
      setReportFilters((prev) => normalizeReportFiltersShape(incoming, prev));
    }
    if (Array.isArray(reportingSetup.documents)) setReportDocuments(sanitizeReportDocuments(reportingSetup.documents));
    if (Array.isArray(reportingSetup.schedules)) setScheduledReports(reportingSetup.schedules);
    if (tagsSetup.alarm_prefs && typeof tagsSetup.alarm_prefs === "object") setTagAlarmPrefs(tagsSetup.alarm_prefs);
    if (emailSetup && typeof emailSetup === "object") {
      if (emailSetup.settings && typeof emailSetup.settings === "object") {
        setEmailSettings((prev) => ({ ...prev, ...emailSetup.settings }));
      } else {
        setEmailSettings((prev) => ({ ...prev, ...emailSetup }));
      }
      if (Array.isArray(emailSetup.profiles)) {
        setEmailProfiles(emailSetup.profiles);
      }
      if (typeof emailSetup.active_profile_id === "string") {
        setActiveEmailProfileId(emailSetup.active_profile_id);
      }
    }
    setAppMetadata(metadata);
  };

  useEffect(() => {
    const seeded = Boolean(appMetadata?.default_local_db_seeded);
    const seededUtc = String(appMetadata?.default_local_db_seeded_utc || "").trim();
    const hasDefaultLocalDb = dbConnections.some((c) => String(c.id || "") === "local-sqlite-default");
    if (!seeded || !seededUtc || !hasDefaultLocalDb) {
      setShowDefaultLocalDbBadge(false);
      return;
    }
    try {
      const dismissed = localStorage.getItem(DEFAULT_LOCAL_DB_BADGE_DISMISS_KEY) || "";
      setShowDefaultLocalDbBadge(dismissed !== seededUtc);
    } catch {
      setShowDefaultLocalDbBadge(true);
    }
  }, [appMetadata, dbConnections]);

  useEffect(() => {
    setRetentionPresetKey(detectRetentionPreset(retentionPolicy));
  }, [retentionPolicy.raw_keep_days]);

  const dismissDefaultLocalDbBadge = () => {
    const seededUtc = String(appMetadata?.default_local_db_seeded_utc || "").trim();
    try {
      if (seededUtc) localStorage.setItem(DEFAULT_LOCAL_DB_BADGE_DISMISS_KEY, seededUtc);
    } catch {}
    setShowDefaultLocalDbBadge(false);
  };

  const buildDbRecoveryConnections = () => {
    return (dbConnectionsRef.current || []).map((db) => ({
      id: String(db.id || ""),
      name: String(db.name || ""),
      engine: String(db.engine || "postgresql"),
      host: String(db.host || ""),
      port: Number(db.port || 0),
      database: String(db.database || ""),
      username: String(db.username || ""),
      password: String(db.password || ""),
      sqlite_path: String(db.sqlite_path || ""),
      file_path: String(db.file_path || ""),
      legacy_url: String(db.legacy_url || ""),
      legacy_api_token: String(db.legacy_api_token || ""),
      schema: String(db.schema || "public"),
      table: String(db.table || "plc_readings"),
      tls: Boolean(db.tls ?? true),
      source: String(db.source || ""),
      site: String(db.site || ""),
      area: String(db.area || ""),
      equipment: String(db.equipment || ""),
      enabled: db.enabled !== false,
      use_gateway: Boolean(db.use_gateway),
      use_app: Boolean(db.use_app),
      use_backup: Boolean(db.use_backup),
      cloud_sync_enabled: Boolean(db.cloud_sync_enabled),
    }));
  };

  const flushAppStoreOutbox = async () => {
    if (!currentUser) return;
    if (outboxFlushBusyRef.current) return;
    if (!historianOutboxRef.current.length && !logsOutboxRef.current.length) return;
    outboxFlushBusyRef.current = true;
    let histBatch = [];
    let logBatch = [];
    try {
      if (historianOutboxRef.current.length) {
        histBatch = historianOutboxRef.current.splice(0, 400);
        await appendAppStoreHistorian(histBatch);
      }
      if (logsOutboxRef.current.length) {
        logBatch = logsOutboxRef.current.splice(0, 300);
        await appendAppStoreLogs(logBatch);
      }
    } catch (_) {
      if (histBatch.length) historianOutboxRef.current.unshift(...histBatch);
      if (logBatch.length) logsOutboxRef.current.unshift(...logBatch);
    } finally {
      outboxFlushBusyRef.current = false;
    }
  };

  const refreshGatewayRuntimes = async () => {
    const list = await getGatewayInstanceStatuses();
    const map = {};
    for (const row of list || []) {
      if (row?.gateway_id) map[row.gateway_id] = row;
    }
    setGatewayRuntimeStatuses(map);
    return map;
  };

  useEffect(() => {
    if (!isReadonlyCloudMode) return;
    const forced = getBackendTarget();
    setEndpointMode("cloud");
    setCloudUrl(forced.cloudUrl || "");
  }, [isReadonlyCloudMode]);

  const markGatewayRunningState = (gatewayIds, running) => {
    const ids = (gatewayIds || []).filter(Boolean);
    if (!ids.length) return;
    setGatewayRuntimeStatuses((prev) => {
      const next = { ...prev };
      for (const id of ids) {
        const cur = next[id] || { gateway_id: id };
        next[id] = {
          ...cur,
          gateway_id: id,
          running: Boolean(running),
          last_error: running ? null : cur.last_error,
          db_last_error: running ? null : cur.db_last_error
        };
      }
      return next;
    });
  };

  useEffect(() => {
    const initialTheme = getInitialTheme();
    setTheme(initialTheme);
  }, []);

  useEffect(() => {
    let stopped = false;
    if (endpointMode === "cloud") {
      return () => {
        stopped = true;
      };
    }
    const refresh = async () => {
      try {
        const list = await getGatewayInstanceStatuses();
        if (stopped) return;
        const map = {};
        for (const row of list || []) {
          if (row?.gateway_id) map[row.gateway_id] = row;
        }
        setGatewayRuntimeStatuses(map);
      } catch {
        if (!stopped) setGatewayRuntimeStatuses({});
      }
    };
    refresh();
    const intervalMs = endpointMode === "cloud" ? GATEWAY_STATUS_POLL_MS_CLOUD : GATEWAY_STATUS_POLL_MS_LOCAL;
    const timer = setInterval(refresh, intervalMs);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [endpointMode, endpointVersion, currentUser?.username]);

  useEffect(() => {
    const onFullscreenChange = () => {
      setIsFullscreen(getFullscreenState());
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      document.removeEventListener("webkitfullscreenchange", onFullscreenChange);
    };
  }, []);

  useEffect(() => {
    if (!showUserMenu) return;
    const onDocMouseDown = (event) => {
      if (!userMenuRef.current?.contains(event.target)) {
        setShowUserMenu(false);
      }
    };
    const onEsc = (event) => {
      if (event.key === "Escape") setShowUserMenu(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onEsc);
    };
  }, [showUserMenu]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    const savedUsers = localStorage.getItem(USERS_STORAGE_KEY);
    let sourceUsers = buildDefaultUsers();
    if (savedUsers) {
      try {
        sourceUsers = JSON.parse(savedUsers);
      } catch {
        sourceUsers = buildDefaultUsers();
        try {
          localStorage.removeItem(USERS_STORAGE_KEY);
        } catch {}
      }
    }
    const normalizedUsers = (Array.isArray(sourceUsers) ? sourceUsers : []).map((u) => ({
      ...u,
      permissions: normalizePermissions(u.permissions, u.role)
    }));
    setUsers(normalizedUsers);

    const remember = localStorage.getItem("trustnode_remember_user");
    if (remember === "false") setRememberUser(false);
    setCurrentUser(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const restoreAuthSession = async () => {
      try {
        const me = await getAuthMe();
        if (cancelled) return;
        const u = me?.user || null;
        if (!u?.username) return;
        const matched = users.find((x) => x.username === u.username) || {
          username: u.username,
          password: "",
          role: u.role || "viewer",
          permissions: normalizePermissions(u.permissions || {}, u.role || "viewer")
        };
        setCurrentUser(matched);
      } catch (_) {
        clearAuthToken();
      }
    };
    if (users.length) restoreAuthSession();
    return () => {
      cancelled = true;
    };
  }, [users]);

  useEffect(() => {
    if (!isReadonlyCloudMode) return;
    if (currentUser) return;
    const readonlyUser = {
      username: "web_readonly",
      password: "",
      role: "viewer",
      permissions: buildRolePermissions("viewer")
    };
    setCurrentUser(readonlyUser);
    if (!users.length) setUsers([readonlyUser]);
  }, [isReadonlyCloudMode, currentUser, users.length]);

  useEffect(() => {
    if (users.length) {
      localStorage.setItem(USERS_STORAGE_KEY, JSON.stringify(users));
    }
  }, [users]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(DB_CONNECTIONS_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setDbConnections(normalizeDbConnections(parsed));
          return;
        }
      }
    } catch {}
    setDbConnections([]);
  }, []);

  useEffect(() => {
    localStorage.setItem(DB_CONNECTIONS_STORAGE_KEY, JSON.stringify(dbConnections));
  }, [dbConnections]);

  useEffect(() => {
    dbConnectionsRef.current = dbConnections;
  }, [dbConnections]);

  useEffect(() => {
    tagAlarmPrefsRef.current = tagAlarmPrefs || {};
  }, [tagAlarmPrefs]);

  useEffect(() => {
    emailSettingsRef.current = emailSettings || {};
  }, [emailSettings]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(EMAIL_SETTINGS_STORAGE_KEY);
      if (!saved) return;
      const parsed = JSON.parse(saved);
      if (parsed && typeof parsed === "object") {
        if (parsed.settings && typeof parsed.settings === "object") {
          setEmailSettings((prev) => ({ ...prev, ...parsed.settings }));
        } else {
          setEmailSettings((prev) => ({ ...prev, ...parsed }));
        }
        if (Array.isArray(parsed.profiles)) setEmailProfiles(parsed.profiles);
        if (typeof parsed.active_profile_id === "string") setActiveEmailProfileId(parsed.active_profile_id);
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(
        EMAIL_SETTINGS_STORAGE_KEY,
        JSON.stringify({
          settings: emailSettings || {},
          profiles: emailProfiles || [],
          active_profile_id: activeEmailProfileId || ""
        })
      );
    } catch {}
  }, [emailSettings, emailProfiles, activeEmailProfileId]);

  useEffect(() => {
    if (!activeEmailProfileId) return;
    const profile = (emailProfiles || []).find((p) => p.id === activeEmailProfileId);
    if (!profile || !profile.settings) return;
    setEmailSettings((prev) => ({ ...prev, ...profile.settings }));
  }, [activeEmailProfileId]);

  useEffect(() => {
    liveTagValuesRef.current = liveTagValues || {};
  }, [liveTagValues]);

  useEffect(() => {
    if (!gatewayOpcValidatedFor) return;
    const plcIp = (gatewayForm.plc_ip || "").trim();
    const opcUrl = (gatewayForm.opc_url || "").trim();
    const nodeIds = parseOpcNodeIds(gatewayForm.tags_text);
    const currentKey = `opcua|${plcIp}|${opcUrl}|${nodeIds.join(";")}`;
    if (currentKey !== gatewayOpcValidatedFor) {
      setGatewayOpcValidatedFor("");
      setGatewayOpcValidationResult("");
      setGatewayOpcValidationRows([]);
    }
  }, [gatewayForm.gateway_type, gatewayForm.plc_ip, gatewayForm.opc_url, gatewayForm.tags_text, gatewayOpcValidatedFor]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(GATEWAY_CONFIGS_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setGatewayConfigs(parsed);
          return;
        }
      }
    } catch {}
    setGatewayConfigs([]);
  }, []);

  useEffect(() => {
    localStorage.setItem(GATEWAY_CONFIGS_STORAGE_KEY, JSON.stringify(gatewayConfigs));
  }, [gatewayConfigs]);

  useEffect(() => {
    gatewayConfigsRef.current = gatewayConfigs;
    if (!gatewayConfigs.length) {
      setSelectedGatewayId("");
      return;
    }
    if (!gatewayConfigs.some((g) => g.id === selectedGatewayId)) {
      setSelectedGatewayId(gatewayConfigs[0].id);
    }
  }, [gatewayConfigs, selectedGatewayId]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(DASHBOARD_WIDGETS_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setDashboardWidgets(parsed);
        }
      }
    } catch {}
    try {
      const savedLayout = JSON.parse(localStorage.getItem(DASHBOARD_LAYOUT_STORAGE_KEY) || "{}");
      const mode = savedLayout?.mode === "chart" ? "chart" : "kpi";
      const perRow = Math.min(4, Math.max(1, Number(savedLayout?.per_row || 2)));
      setDashboardMode(mode);
      setDashboardPerRow(perRow);
    } catch {}
  }, []);

  useEffect(() => {
    localStorage.setItem(DASHBOARD_WIDGETS_STORAGE_KEY, JSON.stringify(dashboardWidgets));
  }, [dashboardWidgets]);

  useEffect(() => {
    localStorage.setItem(
      DASHBOARD_LAYOUT_STORAGE_KEY,
      JSON.stringify({ mode: dashboardMode, per_row: dashboardPerRow })
    );
  }, [dashboardMode, dashboardPerRow]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(COLLECTION_TRIGGERS_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setCollectionTriggers(parsed);
          return;
        }
      }
    } catch {}
    setCollectionTriggers([]);
  }, []);

  useEffect(() => {
    localStorage.setItem(COLLECTION_TRIGGERS_STORAGE_KEY, JSON.stringify(collectionTriggers));
  }, [collectionTriggers]);

  useEffect(() => {
    collectionTriggersRef.current = collectionTriggers;
  }, [collectionTriggers]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(TRIGGER_RULES_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setTriggerRules(parsed);
          return;
        }
      }
    } catch {}
    setTriggerRules([]);
  }, []);

  useEffect(() => {
    localStorage.setItem(TRIGGER_RULES_STORAGE_KEY, JSON.stringify(triggerRules));
  }, [triggerRules]);

  useEffect(() => {
    triggerRulesRef.current = triggerRules;
  }, [triggerRules]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(DEVICES_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          setDevices(parsed);
          return;
        }
      }
    } catch {}
    setDevices([]);
  }, []);

  useEffect(() => {
    localStorage.setItem(DEVICES_STORAGE_KEY, JSON.stringify(devices));
  }, [devices]);

  useEffect(() => {
    devicesRef.current = devices;
  }, [devices]);

  useEffect(() => {
    let cancelled = false;
    const loadAppStore = async () => {
      try {
        const res = await getAppStoreBootstrap();
        if (cancelled) return;
        if (res?.tenant_id) setCurrentTenantId(String(res.tenant_id));
        if (res?.ok && res?.data && Object.keys(res.data).length) {
          applyAppStorePayload(res.data);
        }
      } catch (_) {
        // Keep localStorage fallback behavior when app-store is unavailable.
      } finally {
        if (!cancelled) setAppStoreHydrated(true);
      }
    };
    loadAppStore();
    const loadTenantCtx = async () => {
      try {
        const res = await getAppStoreTenantContext();
        if (cancelled) return;
        if (res?.tenant_id) setCurrentTenantId(String(res.tenant_id));
      } catch (_) {
        // no-op fallback
      }
    };
    loadTenantCtx();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setStartupWarningsReady(true), 8000);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!appStoreHydrated) return;
    let cancelled = false;
    const loadOperationalHistory = async () => {
      try {
        const [histRes, logRes] = await Promise.all([getAppStoreHistorian(1500), getAppStoreLogs(2500)]);
        if (cancelled) return;
        if (histRes?.ok && Array.isArray(histRes.rows)) {
          setDataLog(histRes.rows);
        }
        if (logRes?.ok && Array.isArray(logRes.rows)) {
          setAppLogs(logRes.rows);
        }
      } catch (_) {
        // Keep current in-memory flow when DB history query is unavailable.
      }
    };
    loadOperationalHistory();
    return () => {
      cancelled = true;
    };
  }, [appStoreHydrated]);

  useEffect(() => {
    if (!appStoreHydrated) return;
    let cancelled = false;
    const loadRetentionAndBackups = async () => {
      try {
        await Promise.all([refreshRetentionData(), refreshBackups()]);
      } catch (_) {
        if (!cancelled) {
          setRetentionResult("Retention/backup service unavailable on current backend.");
        }
      }
    };
    loadRetentionAndBackups();
    return () => {
      cancelled = true;
    };
  }, [appStoreHydrated, endpointVersion]);

  useEffect(() => {
    if (!appStoreHydrated) return;
    const conns = buildDbRecoveryConnections();
    if (!conns.length) return;
    const signature = JSON.stringify(
      conns.map((c) => ({
        id: c.id,
        engine: c.engine,
        host: c.host,
        port: c.port,
        database: c.database,
        schema: c.schema,
        table: c.table,
        sqlite_path: c.sqlite_path,
        file_path: c.file_path,
        legacy_url: c.legacy_url,
        enabled: c.enabled,
        use_gateway: c.use_gateway,
        use_app: c.use_app,
        use_backup: c.use_backup,
        cloud_sync_enabled: c.cloud_sync_enabled
      }))
    );
    if (signature === dbRecoveryLastSignatureRef.current) return;
    if (dbRecoveryInFlightRef.current) return;

    let cancelled = false;
    const runRecovery = async () => {
      dbRecoveryInFlightRef.current = true;
      try {
        const res = await repairDatabaseRecovery({
          connections: conns,
          activate_first_healthy: false
        });
        if (cancelled) return;
        addAppLog({
          level: res?.ok ? "info" : "warning",
          category: "database",
          message: `DB recovery: ${String(res?.summary || "completed")}`
        });
        for (const row of res?.results || []) {
          addAppLog({
            level: row?.ok ? "info" : "error",
            category: "database",
            database_name: row?.name || row?.id || "",
            message: `DB recovery [${row?.engine || "unknown"}]: ${row?.message || "no details"}`
          });
        }
        dbRecoveryLastSignatureRef.current = signature;
      } catch (err) {
        if (cancelled) return;
        addAppLog({
          level: "error",
          category: "database",
          message: `DB recovery failed: ${String(err)}`
        });
      } finally {
        dbRecoveryInFlightRef.current = false;
      }
    };
    runRecovery();
    return () => {
      cancelled = true;
    };
  }, [appStoreHydrated, dbConnections]);

  useEffect(() => {
    if (!scheduledReports.length) return;
    const timer = setInterval(async () => {
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, "0");
      const mm = String(now.getMinutes()).padStart(2, "0");
      const dow = String(now.getDay());
      const dom = String(now.getDate());
      for (const s of scheduledReports) {
        if (!s.enabled) continue;
        const runKey = `${now.toISOString().slice(0, 16)}::${s.id}`;
        if (s.__last_check_key === runKey) continue;
        const timeMatch = String(s.hour || "00").padStart(2, "0") === hh && String(s.minute || "00").padStart(2, "0") === mm;
        if (!timeMatch) continue;
        if (s.recurrence === "weekly" && String(s.day_of_week || "1") !== dow) continue;
        if (s.recurrence === "monthly" && String(s.day_of_month || "1") !== dom) continue;
        const doc = createReportDocument(s.format || "csv", s.filters || null);
        if (!doc) continue;
        const recipients = parseEmailList(s.recipients || emailSettings.report_recipients);
        if (recipients.length) {
          try {
            const context = {
              name: s.name || "Report",
              row_count: doc.row_count,
              created_utc: doc.created_utc || tsNow(),
            };
            const subject = applyTemplate(emailSettings.report_subject || "[REPORT] {{name}}", context);
            const html = applyTemplate(emailSettings.report_template || "", context);
            await sendNotificationEmail({
              ...buildEmailTransportPayload(emailSettings),
              to: recipients,
              subject,
              html_body: html,
              text_body: `Scheduled report ${context.name} generated with ${context.row_count} rows.`
            });
          } catch (err) {
            addAppLog({ level: "error", category: "reporting", message: `Scheduled report email failed: ${String(err)}` });
          }
        }
        setScheduledReports((prev) =>
          prev.map((x) =>
            x.id === s.id
              ? { ...x, last_run_utc: tsNow(), __last_check_key: runKey }
              : x
          )
        );
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [scheduledReports, emailSettings]);

  useEffect(() => {
    if (!appStoreHydrated) return;
    if (isHostedWebClient) return;
    if (appStoreSaveTimerRef.current) clearTimeout(appStoreSaveTimerRef.current);
    appStoreSaveTimerRef.current = setTimeout(async () => {
      if (appStorePersistInFlightRef.current) return;
      const payload = buildAppStorePayload();
      const signature = JSON.stringify(payload);
      // Seed baseline from hydrated data and skip first implicit write.
      // This prevents stale browser sessions from overwriting cloud config
      // immediately on page load.
      if (!appStoreLastPersistSignatureRef.current) {
        appStoreLastPersistSignatureRef.current = signature;
        return;
      }
      if (signature === appStoreLastPersistSignatureRef.current) return;
      appStorePersistInFlightRef.current = true;
      try {
        await saveAppStoreBootstrap(payload, currentUser?.username || "system");
        appStoreLastPersistSignatureRef.current = signature;
      } catch (_) {
        // Keep app responsive on transient backend/store failure.
      } finally {
        appStorePersistInFlightRef.current = false;
      }
    }, 800);
    return () => {
      if (appStoreSaveTimerRef.current) {
        clearTimeout(appStoreSaveTimerRef.current);
        appStoreSaveTimerRef.current = null;
      }
    };
  }, [
    appStoreHydrated,
    isHostedWebClient,
    theme,
    rememberUser,
    endpointMode,
    cloudUrl,
    cloudAutoSyncEnabled,
    uiSourceMode,
    uiSourceRemoteUrl,
    uiSourceLocalPath,
    tenantWebClientUrl,
    tenantCompanyName,
    tenantLoginRealm,
    activePage,
    users,
    currentUser,
    devices,
    gatewayConfigs,
    dbConnections,
    collectionTriggers,
    collectionTriggerMode,
    triggerRules,
    dashboardWidgets,
    dashboardMode,
    dashboardPerRow,
    alarms,
    reportFilters,
    reportDocuments,
    scheduledReports,
    tagAlarmPrefs,
    emailSettings,
    emailProfiles,
    activeEmailProfileId
  ]);

  useEffect(() => {
    const timer = setInterval(() => {
      flushAppStoreOutbox();
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (devicesSeeded || devices.length) return;
    if (!config?.plc_ip) return;
    setDevices([
      {
        id: "dev-primary",
        name: "Primary PLC",
        gateway_type: config.gateway_type,
        plc_ip: config.plc_ip,
        notes: "Seeded from gateway configuration",
        connection_ok: wsState === "connected",
        ping_ok: wsState === "connected",
        port_ok: wsState === "connected",
        last_test: "Initial status from gateway"
      }
    ]);
    setDevicesSeeded(true);
  }, [config, devicesSeeded, devices.length, wsState]);

  useEffect(() => {
    if (!config) return;
    if (gatewayConfigsRef.current.length) return;
    const seeded = {
      id: "gw-primary",
      name: "Primary Gateway",
      device_id: "",
      gateway_type: config.gateway_type || "allen_bradley",
      plc_ip: config.plc_ip || "",
      opc_url: config.opc_url || "",
      database_id: "",
      interval_ms: Number(config.interval_ms || 1000),
      tags: Array.isArray(config.tags) ? config.tags : []
    };
    setGatewayConfigs([seeded]);
    setSelectedGatewayId(seeded.id);
  }, [config]);

  useEffect(() => {
    if (endpointMode === "cloud") return;
    let stopped = false;
    let running = false;
    const checkDevices = async () => {
      const current = devicesRef.current;
      if (running || !current.length) return;
      running = true;
      try {
        const checks = await Promise.all(
          current.map(async (d) => {
            try {
              const res = await testPlcConnection({
                gateway_type: d.gateway_type,
                plc_ip: d.plc_ip,
                opc_url: d.opc_url || "",
                opc_node_id: d.opc_node_id || "",
                opc_node_ids: Array.isArray(d.opc_node_ids) && d.opc_node_ids.length
                  ? d.opc_node_ids
                  : parseOpcNodeIds(d.opc_node_ids_text || d.opc_node_id || ""),
                timeout_ms: d.gateway_type === "siemens_opcua" ? 7000 : 2500
              });
              const hasOpcField = Object.prototype.hasOwnProperty.call(res || {}, "opc_session_ok");
              return {
                id: d.id,
                connection_ok: res.ok,
                ping_ok: res.ping_ok,
                port_ok: res.port_ok,
                protocol_ok:
                  d.gateway_type === "siemens_opcua"
                    ? Boolean(hasOpcField ? res.opc_session_ok : res.port_ok)
                    : Boolean(res.port_ok),
                last_test: res.message
              };
            } catch (err) {
              return {
                id: d.id,
                connection_ok: false,
                ping_ok: false,
                port_ok: false,
                protocol_ok: false,
                last_test: String(err)
              };
            }
          })
        );
        if (stopped) return;
        setDevices((prev) =>
          prev.map((d) => {
            const hit = checks.find((c) => c.id === d.id);
            return hit ? { ...d, ...hit } : d;
          })
        );
      } finally {
        running = false;
      }
    };
    checkDevices();
    const timer = setInterval(checkDevices, 15000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [endpointMode]);

  useEffect(() => {
    if (endpointMode === "cloud") return;
    let stopped = false;
    let running = false;
    const checkDbConnections = async () => {
      const current = dbConnectionsRef.current;
      if (running || !current.length) return;
      running = true;
      try {
        const checks = await Promise.all(
          current.map(async (c) => {
            try {
              const res = await testDatabaseConnection({
                engine: c.engine,
                host: c.host || "",
                port: Number(c.port || 0),
                database: c.database || "",
                username: c.username || "",
                password: c.password || "",
                sqlite_path: c.sqlite_path || "",
                legacy_url: c.legacy_url || "",
                legacy_api_token: c.legacy_api_token || "",
                tls: Boolean(c.tls),
                timeout_ms: String(c.engine || "").toLowerCase() === "postgresql" ? 12000 : 4000
              });
              return { id: c.id, connection_ok: Boolean(res.ok), last_test: res.message, last_check_utc: tsNow() };
            } catch (err) {
              return { id: c.id, connection_ok: false, last_test: String(err), last_check_utc: tsNow() };
            }
          })
        );
        if (stopped) return;
        setDbConnections((prev) =>
          prev.map((c) => {
            const hit = checks.find((x) => x.id === c.id);
            return hit ? { ...c, connection_ok: hit.connection_ok, last_test: hit.last_test, last_check_utc: hit.last_check_utc } : c;
          })
        );
      } finally {
        running = false;
      }
    };
    checkDbConnections();
    const timer = setInterval(checkDbConnections, 20000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [endpointMode]);

  useEffect(() => {
    if (currentUser?.username && rememberUser) {
      localStorage.setItem(CURRENT_USER_STORAGE_KEY, currentUser.username);
    } else if (!rememberUser) {
      localStorage.removeItem(CURRENT_USER_STORAGE_KEY);
    }
    localStorage.setItem("trustnode_remember_user", rememberUser ? "true" : "false");
  }, [currentUser, rememberUser]);

  useEffect(() => {
    const target = getBackendTarget();
    let nextMode = target.mode || "local";
    let nextCloud = target.cloudUrl || "";
    if (isHostedWebClient) {
      nextMode = "cloud";
      if (!nextCloud) nextCloud = String(window.location.origin || "").replace(/\/+$/, "");
      setBackendTarget("cloud", nextCloud);
    } else if (!isHostedWebClient && nextMode !== "local") {
      nextMode = "local";
      setBackendTarget("local", nextCloud);
    }
    setEndpointMode(nextMode);
    setCloudUrl(nextCloud);

    getUiSourceConfig()
      .then((cfg) => {
        setUiSourceMode(cfg.mode || "local");
        setUiSourceRemoteUrl(cfg.remote_url || "");
        setUiSourceLocalPath(cfg.local_path || "");
      })
      .catch(() => {});
  }, [isHostedWebClient]);

  useEffect(() => {
    if (!isHostedWebClient) return;
    if (endpointMode === "cloud") return;
    const forcedCloud = String(cloudUrl || window.location.origin || "").trim().replace(/\/+$/, "");
    setEndpointMode("cloud");
    if (forcedCloud) setCloudUrl(forcedCloud);
    setBackendTarget("cloud", forcedCloud);
  }, [isHostedWebClient, endpointMode, cloudUrl]);

  useEffect(() => {
    if (!isHostedWebClient || endpointMode !== "cloud") {
      setEdgeLinkState({ state: "unknown", message: "Cloud link check disabled (local mode)" });
      return;
    }
    let stopped = false;
    const probe = async () => {
      try {
        const res = await getHealth();
        if (stopped) return;
        setEdgeLinkState({ state: "online", message: `Cloud API healthy (${res?.api_build || "ok"})` });
      } catch (e) {
        if (stopped) return;
        setEdgeLinkState({ state: "offline", message: String(e || "unreachable") });
      }
    };
    probe();
    const timer = setInterval(probe, 15000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [endpointMode, isHostedWebClient]);

  useEffect(() => {
    let cancelled = false;
    const initWithRetry = async () => {
      while (!cancelled) {
        try {
          const [cfg, st] = await Promise.all([getConfig(), getStatus()]);
          if (cancelled) return;
          setConfig(cfg);
          setStatus(st);
          setBootState("ready");
          setError("");
          return;
        } catch (e) {
          if (cancelled) return;
          setBootState("waiting_backend");
          setError(`Waiting for backend... ${String(e)}`);
          await new Promise((resolve) => setTimeout(resolve, 1200));
        }
      }
    };
    initWithRetry();
    return () => {
      cancelled = true;
    };
  }, [endpointVersion]);

  useEffect(() => {
    let stopped = false;
    if (!currentUser) {
      setWsState("disconnected");
      return () => {
        stopped = true;
      };
    }
    if (endpointMode === "cloud") {
      setWsState(cloudStreamConnected ? "connected" : "cloud_polling");
      return () => {
        stopped = true;
      };
    }
    let ws = null;
    const wsStreamUrl = getWsStreamUrl();

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(wsStreamUrl);
      ws.onopen = () => {
        setWsState("connected");
        setError("");
        addAppLog({ level: "info", category: "connectivity", message: "WebSocket stream connected" });
      };
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status) setStatus(data.status);
          if (data.type === "error") {
            const gatewayId = String(data.gateway_id || "");
            const gateway = gatewayConfigsRef.current.find((g) => g.id === gatewayId) || null;
            const msg = String(data.message || "").trim() || "Unknown gateway error";
            addAppLog({
              level: "error",
              category: "gateway",
              gateway_id: gatewayId,
              gateway_name: gateway?.name || gatewayId,
              message: `Gateway ${gateway?.name || gatewayId || "unknown"} runtime error: ${msg}`
            });
          }
          if (Array.isArray(data.readings)) {
            setReadings(data.readings);
            const first = data.readings[0];
            const gatewayId = String(data.gateway_id || "");
            const gateway = gatewayConfigsRef.current.find((g) => g.id === gatewayId) || null;
            const device = devicesRef.current.find((d) => d.id === gateway?.device_id) || null;
            const db = dbConnectionsRef.current.find((c) => c.id === gateway?.database_id) || null;
            const activeGatewayTriggers = collectionTriggersRef.current.filter((t) => t.enabled !== false);
            if (gatewayId) {
              setLiveTagValues((prev) => {
                const next = { ...prev };
                const ts = tsNow();
                for (const r of data.readings) {
                  const normTag = normalizeTagName(r.tag_name || "");
                  if (!normTag) continue;
                  const key = `${gatewayId}::${normTag}`;
                  next[key] = {
                    gateway_id: gatewayId,
                    tag: String(r.tag_name || ""),
                    ts,
                    value: r.value,
                    quality: r.quality,
                    quality_label: r.quality_label || qualityLabelFromCode(r.quality)
                  };
                }
                return next;
              });
            }
            const collectionAllowed = data.collection_allowed !== false;
            const collectionBlockReason = String(data.collection_block_reason || "").trim();
            const ts = tsNow();
            if (!collectionAllowed && activeGatewayTriggers.length) {
              const key = `${gatewayId}::${activeGatewayTriggers.length}`;
              if (connectionLoopRef.current.collectionBlocks[key] !== true) {
                connectionLoopRef.current.collectionBlocks[key] = true;
                addAppLog({
                  level: "warning",
                  category: "gateway",
                  gateway_id: gatewayId,
                  gateway_name: gateway?.name || gatewayId,
                  message:
                    `Collection blocked by trigger conditions (${activeGatewayTriggers.length} configured trigger(s)).` +
                    (collectionBlockReason ? ` ${collectionBlockReason}` : "")
                });
              }
            } else {
              const key = `${gatewayId}::${activeGatewayTriggers.length}`;
              if (connectionLoopRef.current.collectionBlocks[key] === true) {
                addAppLog({
                  level: "info",
                  category: "gateway",
                  gateway_id: gatewayId,
                  gateway_name: gateway?.name || gatewayId,
                  message: "Collection trigger is TRUE again. Collection/write resumed."
                });
              }
              connectionLoopRef.current.collectionBlocks[key] = false;
            }
            if (collectionAllowed) {
              if (first) {
                setHistory((prev) => {
                  historySeqRef.current += 1;
                  const next = [
                    ...prev,
                    {
                      idx: historySeqRef.current,
                      ts: first.ts_utc.slice(11, 19),
                      value: Number(first.value)
                    }
                  ];
                  return next.slice(-120);
                });
              }
              setDataLog((prev) => {
                const rows = data.readings.map((r) => ({
                  ts,
                  source: r.source,
                  gateway_id: gatewayId,
                  gateway_name: gateway?.name || "",
                  device_name: device?.name || "",
                  plc_ip: gateway?.plc_ip || device?.plc_ip || "",
                  database_name: db?.name || "",
                  tag: r.tag_name,
                  value: r.value,
                  quality: r.quality,
                  quality_label: r.quality_label || qualityLabelFromCode(r.quality)
                }));
                historianOutboxRef.current.push(
                  ...rows.map((row) => ({
                    ts_utc: row.ts,
                    source: row.source,
                    gateway_id: row.gateway_id,
                    gateway_name: row.gateway_name,
                    device_name: row.device_name,
                    plc_ip: row.plc_ip,
                    database_name: row.database_name,
                    tag_name: row.tag,
                    value: row.value,
                    quality: row.quality,
                    quality_label: row.quality_label
                  }))
                );
                if (historianOutboxRef.current.length > 12000) {
                  historianOutboxRef.current.splice(0, historianOutboxRef.current.length - 12000);
                }
                return [...rows, ...prev].slice(0, 500);
              });

              const activeRules = triggerRulesRef.current.filter((rule) => rule.enabled !== false);
              const newAlarms = [];
              for (const r of data.readings) {
                const valueNum = Number(r.value);
                if (Number.isNaN(valueNum)) continue;
                const tagName = String(r.tag_name || "").trim();
                if (!gatewayId || !tagName) continue;
                const tagAlarmEnabled = isTagAlarmEnabled(gatewayId, tagName);
                const matchedRules = activeRules.filter(
                  (rule) => String(rule.gateway_id) === gatewayId && String(rule.tag_name || "").trim() === tagName
                );
                for (const rule of matchedRules) {
                  const lowerHit = Boolean(rule.lower_enabled) &&
                    compareByOperator(valueNum, String(rule.lower_operator || "<"), Number(rule.lower_value));
                  const upperHit = Boolean(rule.upper_enabled) &&
                    compareByOperator(valueNum, String(rule.upper_operator || ">="), Number(rule.upper_value));
                  const violated = lowerHit || upperHit;
                  const ruleKey = `${gatewayId}:${tagName}:${rule.id}`;
                  const wasActive = Boolean(triggerActiveStateRef.current[ruleKey]);
                  const gatewayName = gateway?.name || gatewayId;
                  const lowerText = rule.lower_enabled ? `${rule.lower_operator} ${rule.lower_value}` : "-";
                  const upperText = rule.upper_enabled ? `${rule.upper_operator} ${rule.upper_value}` : "-";
                  if (violated && !wasActive) {
                    if (!tagAlarmEnabled) {
                      newAlarms.push({
                        id: `${rule.id}-${tagName}-${Date.now()}-paused`,
                        ts,
                        severity: "Info",
                        message: `[${gatewayName}] ${tagName} violated limits but alarm is PAUSED (L: ${lowerText}, U: ${upperText})`,
                        value: r.value,
                        tag: tagName,
                        alert_key: ruleKey,
                        event_type: "active",
                        gateway_id: gatewayId,
                        gateway_name: gatewayName,
                        acknowledged: true,
                        notification_paused: true,
                        paused_by_tag: true
                      });
                    } else {
                      newAlarms.push({
                        id: `${rule.id}-${tagName}-${Date.now()}`,
                        ts,
                        severity: "Critical",
                        message: `[${gatewayName}] ${tagName} violated limits (L: ${lowerText}, U: ${upperText})`,
                        value: r.value,
                        tag: tagName,
                        alert_key: ruleKey,
                        event_type: "active",
                        gateway_id: gatewayId,
                        gateway_name: gatewayName,
                        acknowledged: false,
                        notification_paused: false,
                        paused_by_tag: false
                      });
                    }
                  } else if (!violated && wasActive) {
                    if (!tagAlarmEnabled) {
                      newAlarms.push({
                        id: `${rule.id}-${tagName}-${Date.now()}-clear-paused`,
                        ts,
                        severity: "Info",
                        message: `[${gatewayName}] ${tagName} back within limits but alarm is PAUSED`,
                        value: r.value,
                        tag: tagName,
                        alert_key: ruleKey,
                        event_type: "clear",
                        gateway_id: gatewayId,
                        gateway_name: gatewayName,
                        acknowledged: true,
                        notification_paused: true,
                        paused_by_tag: true
                      });
                    } else {
                      newAlarms.push({
                        id: `${rule.id}-${tagName}-${Date.now()}-clear`,
                        ts,
                        severity: "Info",
                        message: `[${gatewayName}] ${tagName} back within limits`,
                        value: r.value,
                        tag: tagName,
                        alert_key: ruleKey,
                        event_type: "clear",
                        gateway_id: gatewayId,
                        gateway_name: gatewayName,
                        acknowledged: false,
                        notification_paused: false,
                        paused_by_tag: false
                      });
                    }
                  }
                  triggerActiveStateRef.current[ruleKey] = violated;
                }
              }
              if (newAlarms.length) {
                setAlarms((prev) => [...newAlarms, ...prev].slice(0, 300));
                newAlarms.forEach((alarm) => {
                  if (!alarm.acknowledged && !alarm.notification_paused) sendAlarmEmailNotification(alarm);
                });
              }
            }
          }
        } catch {
          setError("Invalid stream payload");
        }
      };
      ws.onclose = () => {
        if (stopped) return;
        setWsState("reconnecting");
        addAppLog({ level: "warning", category: "connectivity", message: "WebSocket stream disconnected, retrying" });
        reconnectTimerRef.current = setTimeout(connect, 1500);
      };
      ws.onerror = () => {
        setWsState("disconnected");
        addAppLog({ level: "error", category: "connectivity", message: "WebSocket stream error" });
        ws.close();
      };
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (ws) ws.close();
    };
  }, [endpointVersion, endpointMode, currentUser, cloudStreamConnected]);

  useEffect(() => {
    if (endpointMode !== "cloud") {
      setCloudStreamConnected(false);
      return;
    }
    if (!currentUser) {
      setCloudStreamConnected(false);
      return;
    }
    let stopped = false;
    let ws = null;
    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(getCloudWsStreamUrl());
      ws.onopen = () => {
        setCloudStreamConnected(true);
        setWsState("connected");
      };
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data || "{}");
          if (data.type !== "cloud_snapshot") return;
          if (Array.isArray(data.live_rows) && data.live_rows.length) {
            const nextLive = {};
            const nextReadings = [];
            const latestByGateway = {};
            const latestByDbName = {};
            const latestByPlcIp = {};
            for (const row of data.live_rows) {
              const gatewayId = String(row?.gateway_id || "");
              const rawTag = String(row?.tag || row?.tag_name || "");
              const normTag = normalizeTagName(rawTag);
              if (!normTag) continue;
              const key = `${gatewayId}::${normTag}`;
              const readingTs = String(row?.ts || tsNow());
              const quality = row?.quality;
              const qualityLabel = row?.quality_label || qualityLabelFromCode(quality);
              nextLive[key] = {
                gateway_id: gatewayId,
                tag: rawTag,
                ts: readingTs,
                value: row?.value,
                quality,
                quality_label: qualityLabel
              };
              nextReadings.push({
                ts_utc: readingTs,
                source: row?.source || "",
                gateway_id: gatewayId,
                gateway_name: row?.gateway_name || "",
                device_name: row?.device_name || "",
                plc_ip: row?.plc_ip || "",
                tag_name: rawTag,
                value: row?.value,
                quality,
                quality_label: qualityLabel
              });
              if (gatewayId && !latestByGateway[gatewayId]) latestByGateway[gatewayId] = readingTs;
              const dbName = String(row?.database_name || "").trim();
              if (dbName && !latestByDbName[dbName]) latestByDbName[dbName] = readingTs;
              const plcIp = String(row?.plc_ip || "").trim();
              if (plcIp && !latestByPlcIp[plcIp]) latestByPlcIp[plcIp] = readingTs;
            }
            setLiveTagValues(nextLive);
            setReadings(nextReadings);
            const nowMs = Date.now();
            setGatewayRuntimeStatuses((prev) => {
              const next = { ...prev };
              for (const g of gatewayConfigsRef.current || []) {
                const gid = String(g?.id || "");
                if (!gid) continue;
                const ts = latestByGateway[gid];
                const cur = next[gid] || { gateway_id: gid };
                const rawOnline = Boolean(ts) && (() => {
                  const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
                  return Number.isFinite(ageMs) ? ageMs <= 10000 : true;
                })();
                const online = getStableCloudOnline("gateway", gid, rawOnline, 1, 3);
                if (ts) {
                  next[gid] = {
                    ...cur,
                    gateway_id: gid,
                    running: online,
                    last_error: null,
                    db_last_error: null,
                    last_check_utc: ts
                  };
                } else if (cur.running || !online) {
                  next[gid] = { ...cur, gateway_id: gid, running: online };
                }
              }
              return next;
            });
            setGatewayConfigs((prev) =>
              (prev || []).map((g) => {
                const ts = latestByGateway[String(g.id || "")];
                return ts ? { ...g, last_check_utc: ts } : g;
              })
            );
            setDevices((prev) =>
              (prev || []).map((d) => {
                const relatedGw = (gatewayConfigsRef.current || []).find((g) => String(g.device_id || "") === String(d.id || ""));
                const tsFromGw = relatedGw ? latestByGateway[String(relatedGw.id || "")] : "";
                const tsFromIp = latestByPlcIp[String(d?.plc_ip || "").trim()] || "";
                const ts = tsFromGw || tsFromIp;
                const rawOnline = ts
                  ? (() => {
                      const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
                      return Number.isFinite(ageMs) ? ageMs <= 10000 : true;
                    })()
                  : false;
                const online = getStableCloudOnline("device", String(d.id || d.name || d.plc_ip || ""), rawOnline, 1, 3);
                return {
                  ...d,
                  connection_ok: online,
                  ping_ok: online,
                  port_ok: online,
                  protocol_ok: online,
                  last_test: online ? "Live cloud reading" : "No recent cloud reading",
                  last_check_utc: ts || d.last_check_utc || ""
                };
              })
            );
            setDbConnections((prev) =>
              (prev || []).map((c) => {
                const ts = latestByDbName[String(c.name || "").trim()];
                if (!ts) return c;
                const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
                const rawOnline = Number.isFinite(ageMs) ? ageMs <= 15000 : true;
                const online = getStableCloudOnline("database", String(c.id || c.name || ""), rawOnline, 1, 3);
                return {
                  ...c,
                  connection_ok: online,
                  last_test: online ? "Live cloud reading" : "No recent cloud reading",
                  last_check_utc: ts
                };
              })
            );

            const activeRules = triggerRulesRef.current.filter((rule) => rule.enabled !== false);
            const newAlarms = [];
            for (const row of data.live_rows) {
              const gatewayId = String(row?.gateway_id || "").trim();
              const tagName = String(row?.tag || row?.tag_name || "").trim();
              const valueNum = Number(row?.value);
              if (!gatewayId || !tagName || Number.isNaN(valueNum)) continue;
              const tagAlarmEnabled = isTagAlarmEnabled(gatewayId, tagName);
              const matchedRules = activeRules.filter(
                (rule) => String(rule.gateway_id) === gatewayId && String(rule.tag_name || "").trim() === tagName
              );
              for (const rule of matchedRules) {
                const lowerHit = Boolean(rule.lower_enabled) &&
                  compareByOperator(valueNum, String(rule.lower_operator || "<"), Number(rule.lower_value));
                const upperHit = Boolean(rule.upper_enabled) &&
                  compareByOperator(valueNum, String(rule.upper_operator || ">="), Number(rule.upper_value));
                const violated = lowerHit || upperHit;
                const ruleKey = `${gatewayId}:${tagName}:${rule.id}`;
                const wasActive = Boolean(triggerActiveStateRef.current[ruleKey]);
                const gatewayName = String(row?.gateway_name || gatewayId);
                const alarmTs = String(row?.ts || tsNow());
                const lowerText = rule.lower_enabled ? `${rule.lower_operator} ${rule.lower_value}` : "-";
                const upperText = rule.upper_enabled ? `${rule.upper_operator} ${rule.upper_value}` : "-";
                if (violated && !wasActive) {
                  if (!tagAlarmEnabled) {
                    newAlarms.push({
                      id: `${rule.id}-${tagName}-${Date.now()}-paused`,
                      ts: alarmTs,
                      severity: "Info",
                      message: `[${gatewayName}] ${tagName} violated limits but alarm is PAUSED (L: ${lowerText}, U: ${upperText})`,
                      value: row?.value,
                      tag: tagName,
                      alert_key: ruleKey,
                      event_type: "active",
                      gateway_id: gatewayId,
                      gateway_name: gatewayName,
                      acknowledged: true,
                      notification_paused: true,
                      paused_by_tag: true
                    });
                  } else {
                    newAlarms.push({
                      id: `${rule.id}-${tagName}-${Date.now()}`,
                      ts: alarmTs,
                      severity: "Critical",
                      message: `[${gatewayName}] ${tagName} violated limits (L: ${lowerText}, U: ${upperText})`,
                      value: row?.value,
                      tag: tagName,
                      alert_key: ruleKey,
                      event_type: "active",
                      gateway_id: gatewayId,
                      gateway_name: gatewayName,
                      acknowledged: false,
                      notification_paused: false,
                      paused_by_tag: false
                    });
                  }
                } else if (!violated && wasActive) {
                  if (!tagAlarmEnabled) {
                    newAlarms.push({
                      id: `${rule.id}-${tagName}-${Date.now()}-clear-paused`,
                      ts: alarmTs,
                      severity: "Info",
                      message: `[${gatewayName}] ${tagName} back within limits but alarm is PAUSED`,
                      value: row?.value,
                      tag: tagName,
                      alert_key: ruleKey,
                      event_type: "clear",
                      gateway_id: gatewayId,
                      gateway_name: gatewayName,
                      acknowledged: true,
                      notification_paused: true,
                      paused_by_tag: true
                    });
                  } else {
                    newAlarms.push({
                      id: `${rule.id}-${tagName}-${Date.now()}-clear`,
                      ts: alarmTs,
                      severity: "Info",
                      message: `[${gatewayName}] ${tagName} back within limits`,
                      value: row?.value,
                      tag: tagName,
                      alert_key: ruleKey,
                      event_type: "clear",
                      gateway_id: gatewayId,
                      gateway_name: gatewayName,
                      acknowledged: false,
                      notification_paused: false,
                      paused_by_tag: false
                    });
                  }
                }
                triggerActiveStateRef.current[ruleKey] = violated;
              }
            }
            if (newAlarms.length) {
              setAlarms((prev) => [...newAlarms, ...prev].slice(0, 300));
              newAlarms.forEach((alarm) => {
                if (!alarm.acknowledged && !alarm.notification_paused) sendAlarmEmailNotification(alarm);
              });
            }
          }
          if (Array.isArray(data.historian_rows) && data.historian_rows.length) {
            setDataLog((prev) => mergeHistorianRowsStable(data.historian_rows, prev, 5000));
          }
          if (Array.isArray(data.log_rows) && data.log_rows.length) setAppLogs(data.log_rows);
          if (data.inspector) setDatabaseInspector(data.inspector);
          if (Array.isArray(data.gateway_statuses)) {
            const map = {};
            for (const row of data.gateway_statuses) {
              if (row?.gateway_id) map[row.gateway_id] = row;
            }
            setGatewayRuntimeStatuses((prev) => ({ ...prev, ...map }));
          }
          setWsState("connected");
        } catch {}
      };
      ws.onclose = () => {
        if (stopped) return;
        setCloudStreamConnected(false);
        setWsState("reconnecting");
        reconnectTimerRef.current = setTimeout(connect, 1500);
      };
      ws.onerror = () => {
        setCloudStreamConnected(false);
        setWsState("reconnecting");
        ws?.close();
      };
    };
    connect();
    return () => {
      stopped = true;
      setCloudStreamConnected(false);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (ws) ws.close();
    };
  }, [endpointMode, endpointVersion, currentUser]);

  useEffect(() => {
    if (endpointMode !== "cloud") return;
    if (!currentUser) return;
    let stopped = false;
    let runningLive = false;
    let runningAux = false;
    const pollCloudLive = async () => {
      if (stopped || runningLive) return;
      runningLive = true;
      try {
        const liveRes = await getAppStoreLive(5000);
        if (stopped) return;
        if (liveRes?.ok && Array.isArray(liveRes.rows)) {
          const dbMetaByName = new Map();
          for (const db of dbConnectionsRef.current || []) {
            const nameKey = String(db?.name || "").trim().toLowerCase();
            if (!nameKey) continue;
            dbMetaByName.set(nameKey, {
              source: String(db?.source || "").trim(),
              site: String(db?.site || "").trim(),
              area: String(db?.area || "").trim(),
              equipment: String(db?.equipment || "").trim(),
            });
          }
          const nextLive = {};
          const nextReadings = [];
          const nextDataRows = [];
          const latestByGateway = {};
          const latestByDbName = {};
          const latestByPlcIp = {};
          for (const row of liveRes.rows) {
            const gatewayId = String(row?.gateway_id || "");
            const rawTag = String(row?.tag || row?.tag_name || "");
            const normTag = normalizeTagName(rawTag);
            if (!normTag) continue;
            const key = `${gatewayId}::${normTag}`;
            const readingTs = String(row?.ts || tsNow());
            const quality = row?.quality;
            const qualityLabel = row?.quality_label || qualityLabelFromCode(quality);
            const dbName = String(row?.database_name || "").trim();
            const dbMeta = dbMetaByName.get(dbName.toLowerCase()) || null;
            nextLive[key] = {
              gateway_id: gatewayId,
              tag: rawTag,
              ts: readingTs,
              value: row?.value,
              quality,
              quality_label: qualityLabel
            };
            nextReadings.push({
              ts_utc: readingTs,
              source: row?.source || "",
              gateway_id: gatewayId,
              gateway_name: row?.gateway_name || "",
              device_name: row?.device_name || "",
              plc_ip: row?.plc_ip || "",
              tag_name: rawTag,
              value: row?.value,
              quality,
              quality_label: qualityLabel
            });
            nextDataRows.push({
              ts: readingTs,
              source: dbMeta?.source || row?.source || "",
              site: dbMeta?.site || row?.site || "",
              area: dbMeta?.area || row?.area || "",
              equipment: dbMeta?.equipment || row?.equipment || "",
              gateway_id: gatewayId,
              gateway_name: row?.gateway_name || "",
              device_name: row?.device_name || "",
              plc_ip: row?.plc_ip || "",
              database_name: dbName,
              tag: rawTag,
              value: row?.value,
              quality,
              quality_label: qualityLabel
            });
            if (gatewayId && !latestByGateway[gatewayId]) latestByGateway[gatewayId] = readingTs;
            if (dbName && !latestByDbName[dbName]) latestByDbName[dbName] = readingTs;
            const plcIp = String(row?.plc_ip || "").trim();
            if (plcIp && !latestByPlcIp[plcIp]) latestByPlcIp[plcIp] = readingTs;
          }
          setLiveTagValues(nextLive);
          setReadings(nextReadings);
          const nowMs = Date.now();
          setGatewayRuntimeStatuses((prev) => {
            const next = { ...prev };
            for (const g of gatewayConfigsRef.current || []) {
              const gid = String(g?.id || "");
              if (!gid) continue;
              const ts = latestByGateway[gid];
              const cur = next[gid] || { gateway_id: gid };
              const rawOnline = Boolean(ts) && (() => {
                const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
                return Number.isFinite(ageMs) ? ageMs <= 10000 : true;
              })();
              const online = getStableCloudOnline("gateway", gid, rawOnline, 1, 3);
              if (ts) {
                next[gid] = {
                  ...cur,
                  gateway_id: gid,
                  running: online,
                  last_error: null,
                  db_last_error: null,
                  last_check_utc: ts
                };
              } else if (cur.running || !online) {
                next[gid] = { ...cur, gateway_id: gid, running: online };
              }
            }
            return next;
          });
          setGatewayConfigs((prev) =>
            (prev || []).map((g) => {
              const ts = latestByGateway[String(g.id || "")];
              return ts ? { ...g, last_check_utc: ts } : g;
            })
          );
            setDevices((prev) =>
              (prev || []).map((d) => {
                const relatedGw = (gatewayConfigsRef.current || []).find((g) => String(g.device_id || "") === String(d.id || ""));
                const tsFromGw = relatedGw ? latestByGateway[String(relatedGw.id || "")] : "";
                const tsFromIp = latestByPlcIp[String(d?.plc_ip || "").trim()] || "";
                const ts = tsFromGw || tsFromIp;
                const rawOnline = ts
                  ? (() => {
                      const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
                      return Number.isFinite(ageMs) ? ageMs <= 10000 : true;
                    })()
                  : false;
                const online = getStableCloudOnline("device", String(d.id || d.name || d.plc_ip || ""), rawOnline, 1, 3);
                return {
                  ...d,
                  connection_ok: online,
                  ping_ok: online,
                  port_ok: online,
                  protocol_ok: online,
                  last_test: online ? "Live cloud reading" : "No recent cloud reading",
                  last_check_utc: ts || d.last_check_utc || ""
                };
              })
            );
          setDbConnections((prev) =>
            (prev || []).map((c) => {
              const ts = latestByDbName[String(c.name || "").trim()];
              if (!ts) return c;
              const ageMs = Math.max(0, nowMs - new Date(ts).getTime());
              const rawOnline = Number.isFinite(ageMs) ? ageMs <= 15000 : true;
              const online = getStableCloudOnline("database", String(c.id || c.name || ""), rawOnline, 1, 3);
              return {
                ...c,
                connection_ok: online,
                last_test: online ? "Live cloud reading" : "No recent cloud reading",
                last_check_utc: ts
              };
            })
          );
          // Keep charts/historian visibly live in cloud mode even when historian replay lags.
          if (nextDataRows.length) {
            setDataLog((prev) => {
              const incoming = [...nextDataRows].sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
              const merged = [...incoming];
              const seen = new Set(
                incoming.map((r) => `${String(r.gateway_id)}::${String(r.tag)}::${String(r.ts)}`)
              );
              for (const r of prev || []) {
                const key = `${String(r.gateway_id || "")}::${String(r.tag || "")}::${String(r.ts || "")}`;
                if (seen.has(key)) continue;
                merged.push(r);
                if (merged.length >= 5000) break;
              }
              return merged;
            });
          }
        }
        setWsState("cloud_polling");
      } catch (err) {
        if (!stopped) {
          setWsState("reconnecting");
          addAppLog({
            level: "warning",
            category: "connectivity",
            message: `Cloud live polling failed: ${String(err)}`
          });
        }
      } finally {
        runningLive = false;
      }
    };
    const pollCloudAux = async () => {
      if (stopped || runningAux) return;
      runningAux = true;
      try {
        const [histRes, logRes, inspectorRes] = await Promise.all([
          getAppStoreHistorian(1500),
          getAppStoreLogs(2500),
          getAppStoreInspector(20)
        ]);
        if (stopped) return;
        if (histRes?.ok && Array.isArray(histRes.rows)) {
          setDataLog((prev) => mergeHistorianRowsStable(histRes.rows, prev, 5000));
        }
        if (logRes?.ok && Array.isArray(logRes.rows)) {
          setAppLogs(logRes.rows);
        }
        if (inspectorRes?.ok && inspectorRes?.inspector) {
          setDatabaseInspector(inspectorRes.inspector);
        }
        setWsState("cloud_polling");
      } catch (err) {
        if (!stopped) {
          setWsState("reconnecting");
          addAppLog({
            level: "warning",
            category: "connectivity",
            message: `Cloud aux polling failed: ${String(err)}`
          });
        }
      } finally {
        runningAux = false;
      }
    };

    pollCloudLive();
    pollCloudAux();
    const liveTimer = setInterval(pollCloudLive, CLOUD_LIVE_POLL_MS);
    const auxTimer = setInterval(pollCloudAux, CLOUD_AUX_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(liveTimer);
      clearInterval(auxTimer);
    };
  }, [endpointMode, endpointVersion, currentUser, cloudStreamConnected]);

  const canEditPage = (page) => {
    if (isReadonlyCloudMode) return false;
    if (!currentUser) return false;
    if (currentUser.role === "admin") return true;
    const mapped =
      page === "dashboard"
        ? "data_log"
        : page === "historian" || page === "logs"
          ? "data_log"
          : page === "database_overview"
            ? "database"
            : page === "database_inspector"
            ? "database"
            : page === "backup_and_retention"
              ? "database"
            : page === "website_and_env"
              ? "users_and_access_control"
              : page === "email_and_notifications" || page === "scheduled_reports"
                ? "users_and_access_control"
          : page;
    return Boolean(currentUser.permissions?.[mapped]);
  };

  const canOpenPage = (page) => {
    if (isReadonlyCloudMode) {
      return [
        "dashboard",
        "tags",
        "alarms",
        "reporting",
        "historian",
        "logs"
      ].includes(page);
    }
    if (page === "dashboard") return true;
    return canEditPage(page);
  };

  const canDeleteRecords = Boolean(!isReadonlyCloudMode && currentUser && currentUser.role === "admin");
  const canControlGateways = Boolean(!isReadonlyCloudMode && currentUser && (currentUser.role === "admin" || currentUser.permissions?.gateway_runtime_control));

  const addAppLog = (entry) => {
    const key = `${String(entry.level || "info")}|${String(entry.category || "system")}|${String(entry.gateway_id || "")}|${String(entry.database_name || "")}|${String(entry.message || "")}`;
    const now = Date.now();
    const last = Number(logDedupeRef.current[key] || 0);
    // Reduce log flood for repeated identical runtime errors while preserving transitions.
    if (now - last < 5000) return;
    logDedupeRef.current[key] = now;
    const row = {
      ts: tsNow(),
      level: "info",
      category: "system",
      message: "",
      gateway_id: "",
      gateway_name: "",
      device_name: "",
      database_name: "",
      ...entry
    };
    logsOutboxRef.current.push({
      ts: row.ts,
      level: row.level,
      category: row.category,
      message: row.message,
      gateway_id: row.gateway_id,
      gateway_name: row.gateway_name,
      device_name: row.device_name,
      database_name: row.database_name
    });
    if (logsOutboxRef.current.length > 8000) {
      logsOutboxRef.current.splice(0, logsOutboxRef.current.length - 8000);
    }
    setAppLogs((prev) => [row, ...prev].slice(0, 3000));
  };

  useEffect(() => {
    const nowMs = Date.now();
    const state = connectionLoopRef.current;

    const deviceStates = {};
    for (const d of devices) {
      const protocolOk = d.protocol_ok ?? d.port_ok;
      const label = d.ping_ok && protocolOk ? "ONLINE" : d.ping_ok ? "IP_OK_PROTOCOL_FAIL" : "OFFLINE";
      deviceStates[d.id] = label;
      if (state.devices[d.id] !== label) {
        addAppLog({
          level: label === "ONLINE" ? "info" : "warning",
          category: "connectivity",
          device_name: d.name || d.id,
          message: `Device ${d.name || d.id} status: ${label}`
        });
      }
    }
    state.devices = deviceStates;

    const dbStates = {};
    for (const db of dbConnections) {
      const label = db.connection_ok ? "ONLINE" : "OFFLINE";
      dbStates[db.id] = label;
      if (state.databases[db.id] !== label) {
        addAppLog({
          level: label === "ONLINE" ? "info" : "warning",
          category: "database",
          database_name: db.name || db.id,
          message: `Database ${db.name || db.id} status: ${label}`
        });
      }
    }
    state.databases = dbStates;

    const gwStates = {};
    for (const g of gatewayConfigs) {
      const rt = gatewayRuntimeStatuses[g.id] || null;
      const running = Boolean(rt?.running);
      const dbErr = String(rt?.db_last_error || "").trim();
      const connErr = String(rt?.last_error || "").trim();
      const label = running ? (dbErr || connErr ? "RUNNING_WITH_ERRORS" : "RUNNING") : "STOPPED";
      gwStates[g.id] = label;
      if (state.gateways[g.id] !== label) {
        const detail = dbErr || connErr;
        addAppLog({
          level: label === "RUNNING" ? "info" : label === "STOPPED" ? "warning" : "error",
          category: "gateway",
          gateway_id: g.id,
          gateway_name: g.name || g.id,
          message: detail
            ? `Gateway ${g.name || g.id} status: ${label} | ${detail}`
            : `Gateway ${g.name || g.id} status: ${label}`
        });
      }
      const gwErrKey = `${g.id}::conn`;
      if (state.gatewayErrors[gwErrKey] !== connErr) {
        state.gatewayErrors[gwErrKey] = connErr;
        if (connErr) {
          addAppLog({
            level: "error",
            category: "gateway",
            gateway_id: g.id,
            gateway_name: g.name || g.id,
            message: `Gateway read error: ${connErr}`
          });
        }
      }
      const dbErrKey = `${g.id}::db`;
      if (state.dbErrors[dbErrKey] !== dbErr) {
        state.dbErrors[dbErrKey] = dbErr;
        if (dbErr) {
          const db = dbConnections.find((c) => c.id === g.database_id);
          addAppLog({
            level: "error",
            category: "database",
            gateway_id: g.id,
            gateway_name: g.name || g.id,
            database_name: db?.name || "",
            message: `DB sink write error: ${dbErr}`
          });
        }
      }
    }
    state.gateways = gwStates;

    if (state.wsState !== wsState) {
      addAppLog({
        level: wsState === "connected" ? "info" : "warning",
        category: "connectivity",
        message: `WebSocket status: ${String(wsState || "").toUpperCase()}`
      });
      state.wsState = wsState;
    }

    if (state.bootState !== bootState) {
      addAppLog({
        level: bootState === "ready" ? "info" : "warning",
        category: "system",
        message: `Backend loop state: ${String(bootState || "").toUpperCase()}`
      });
      state.bootState = bootState;
    }

    if (nowMs - state.lastHeartbeatMs >= 30000) {
      const onlineDevices = devices.filter((d) => Boolean(d.ping_ok && (d.protocol_ok ?? d.port_ok))).length;
      const onlineDb = dbConnections.filter((d) => Boolean(d.connection_ok)).length;
      const runningGw = gatewayConfigs.filter((g) => Boolean(gatewayRuntimeStatuses[g.id]?.running)).length;
      addAppLog({
        level: "info",
        category: "system",
        message: `Loop heartbeat: devices ${onlineDevices}/${devices.length} online, databases ${onlineDb}/${dbConnections.length} online, gateways ${runningGw}/${gatewayConfigs.length} running, ws ${wsState}.`
      });
      state.lastHeartbeatMs = nowMs;
    }
  }, [devices, dbConnections, gatewayConfigs, gatewayRuntimeStatuses, wsState, bootState]);

  const fmtTs = (value) => {
    if (!value) return "";
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? String(value) : d.toISOString().replace("T", " ").slice(0, 19);
  };

  const inRange = (value, from, to) => {
    if (!from && !to) return true;
    const t = new Date(value).getTime();
    if (Number.isNaN(t)) return false;
    if (from) {
      const f = new Date(from).getTime();
      if (!Number.isNaN(f) && t < f) return false;
    }
    if (to) {
      const tt = new Date(to).getTime();
      if (!Number.isNaN(tt) && t > tt) return false;
    }
    return true;
  };

  const toCsv = (rows) => {
    if (!rows.length) return "";
    const keys = Object.keys(rows[0]);
    const esc = (v) => {
      const s = String(v ?? "");
      if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
      return s;
    };
    const lines = [keys.join(",")];
    for (const r of rows) lines.push(keys.map((k) => esc(r[k])).join(","));
    return lines.join("\n");
  };

  const downloadText = (filename, content, type = "text/plain;charset=utf-8") => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  useEffect(() => {
    const msg = String(error || "").trim();
    if (!msg || msg === lastErrorLogRef.current) return;
    lastErrorLogRef.current = msg;
    addAppLog({ level: "error", category: "system", message: msg });
  }, [error]);

  useEffect(() => {
    const msg = String(status?.db_last_error || "").trim();
    if (!msg || msg === lastDbErrorLogRef.current) return;
    lastDbErrorLogRef.current = msg;
    addAppLog({ level: "error", category: "database", message: `DB write error: ${msg}` });
  }, [status?.db_last_error]);

  const primaryValue = useMemo(() => {
    if (!readings.length) return "-";
    return Number(readings[0].value).toFixed(2);
  }, [readings]);

  const criticalAlarmCount = useMemo(
    () => alarms.filter((a) => a.severity === "Critical" && !a.acknowledged).length,
    [alarms]
  );

  const selectedGateway = useMemo(
    () => gatewayConfigs.find((g) => g.id === selectedGatewayId) || null,
    [gatewayConfigs, selectedGatewayId]
  );
  const triggerTagsByGateway = useMemo(() => {
    const byGateway = {};
    for (const g of gatewayConfigs) {
      byGateway[g.id] = Array.from(new Set((g.tags || []).map((t) => String(t).trim()).filter(Boolean)));
    }
    return byGateway;
  }, [gatewayConfigs]);
  const getTriggerLiveStatus = (trigger) => {
    if (!trigger?.gateway_id || !trigger?.tag_name) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    const key = `${String(trigger.gateway_id || "")}::${normalizeTagName(trigger.tag_name || "")}`;
    const latest = liveTagValuesRef.current[key] || null;
    if (!latest) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    const gw = gatewayConfigsRef.current.find((g) => String(g.id) === String(trigger.gateway_id || ""));
    const intervalMs = Math.max(200, Number(gw?.interval_ms || 1000));
    const ageMs = Date.now() - new Date(latest.ts || "").getTime();
    const staleMs = Math.max(5000, intervalMs * 4);
    if (!Number.isFinite(ageMs) || ageMs > staleMs) return { ok: null, label: "Stale", valueText: "-", ageText: "-" };
    const valueNum = Number(latest.value);
    if (Number.isNaN(valueNum)) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    const ok = compareByOperator(valueNum, String(trigger.operator || ">="), Number(trigger.value));
    const ageSec = Math.max(0, Math.floor(ageMs / 1000));
    return { ok, label: ok ? "TRUE" : "FALSE", valueText: String(valueNum), ageText: `${ageSec}s` };
  };
  const getLimitRuleLiveStatus = (rule) => {
    if (!rule?.gateway_id || !rule?.tag_name) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    if (rule.enabled === false) return { ok: null, label: "DISABLED", valueText: "-", ageText: "-" };
    const key = `${String(rule.gateway_id || "")}::${normalizeTagName(rule.tag_name || "")}`;
    const latest = liveTagValuesRef.current[key] || null;
    if (!latest) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    const gw = gatewayConfigsRef.current.find((g) => String(g.id) === String(rule.gateway_id || ""));
    const intervalMs = Math.max(200, Number(gw?.interval_ms || 1000));
    const ageMs = Date.now() - new Date(latest.ts || "").getTime();
    const staleMs = Math.max(5000, intervalMs * 4);
    if (!Number.isFinite(ageMs) || ageMs > staleMs) return { ok: null, label: "Stale", valueText: "-", ageText: "-" };
    const valueNum = Number(latest.value);
    if (Number.isNaN(valueNum)) return { ok: null, label: "No Data", valueText: "-", ageText: "-" };
    const lowerHit = Boolean(rule.lower_enabled) &&
      compareByOperator(valueNum, String(rule.lower_operator || "<"), Number(rule.lower_value));
    const upperHit = Boolean(rule.upper_enabled) &&
      compareByOperator(valueNum, String(rule.upper_operator || ">="), Number(rule.upper_value));
    const violated = lowerHit || upperHit;
    const ageSec = Math.max(0, Math.floor(ageMs / 1000));
    let label = "WITHIN LIMITS";
    if (lowerHit) label = "LOW LIMIT";
    else if (upperHit) label = "HIGH LIMIT";
    return { ok: !violated, label, valueText: String(valueNum), ageText: `${ageSec}s` };
  };
  const getGatewayHealth = (gateway) => {
    if (!gateway) return { ok: false, label: "Not Ready" };
    const rt = gatewayRuntimeStatusesView[gateway.id] || gatewayRuntimeStatuses[gateway.id] || null;
    const runtimeStoppedClean =
      endpointMode === "cloud" &&
      rt &&
      rt.running === false &&
      !String(rt.last_error || "").trim() &&
      !String(rt.db_last_error || "").trim();
    if (runtimeStoppedClean) return { ok: false, label: "Stopped" };
    const device = devices.find((d) => d.id === gateway.device_id) || null;
    const db = dbConnections.find((c) => c.id === gateway.database_id) || null;
    const deviceProtocolOk = Boolean(device && (device.protocol_ok ?? device.port_ok));
    const plcOk = Boolean(device && device.ping_ok && deviceProtocolOk);
    const dbOk = Boolean(db && db.connection_ok);
    if (!plcOk && !dbOk) return { ok: false, label: "Device + DB Fails" };
    if (!plcOk) return { ok: false, label: "Device Fails" };
    if (!dbOk) return { ok: false, label: "DB Fails" };
    return { ok: true, label: "Ready" };
  };

  const getDbWritingLabel = (dbId) => {
    const linkedGatewayIds = gatewayConfigs
      .filter((g) => g.database_id === dbId)
      .map((g) => g.id);
    if (!linkedGatewayIds.length) return "W:0 | P:0";
    let writes = 0;
    let pending = 0;
    for (const gid of linkedGatewayIds) {
      const rt = gatewayRuntimeStatuses[gid];
      if (!rt) continue;
      writes += Number(rt.db_write_count || 0);
      pending += Number(rt.db_pending_count || 0);
    }
    return `W:${writes} | P:${pending}`;
  };

  const getStableCloudOnline = (bucket, key, rawOnline, riseThreshold = 1, dropThreshold = 3) => {
    if (!key) return Boolean(rawOnline);
    const stores = cloudStatusStabilityRef.current || {};
    const bucketStore = stores[bucket] || {};
    stores[bucket] = bucketStore;
    const prev = bucketStore[key] || { stable: Boolean(rawOnline), rise: 0, drop: 0 };
    if (rawOnline) {
      prev.drop = 0;
      if (!prev.stable) {
        prev.rise += 1;
        if (prev.rise >= riseThreshold) {
          prev.stable = true;
          prev.rise = 0;
        }
      } else {
        prev.rise = 0;
      }
    } else {
      prev.rise = 0;
      if (prev.stable) {
        prev.drop += 1;
        if (prev.drop >= dropThreshold) {
          prev.stable = false;
          prev.drop = 0;
        }
      } else {
        prev.drop = 0;
      }
    }
    bucketStore[key] = prev;
    return Boolean(prev.stable);
  };

  const getDbLastCheckLabel = (dbConn) => formatElapsedFromUtc(dbConn?.last_check_utc || "");

  const getCloudSyncStatsForDb = (dbId) => {
    const targetDbId = String(databaseInspector?.cloud_target?.id || "");
    if (!targetDbId || String(dbId || "") !== targetDbId) return null;
    return {
      outboxPending: Number(databaseInspector?.sync_outbox_status?.pending || 0),
      outboxFailed: Number(databaseInspector?.sync_outbox_status?.failed || 0),
      outboxSent: Number(databaseInspector?.sync_outbox_status?.sent || 0),
      histBacklog: Number(databaseInspector?.data_sync?.historian_backlog || 0),
      logBacklog: Number(databaseInspector?.data_sync?.logs_backlog || 0),
      histSynced: Number(databaseInspector?.data_sync?.total_historian_synced || 0),
      logSynced: Number(databaseInspector?.data_sync?.total_logs_synced || 0),
      lastDataSyncUtc: String(databaseInspector?.data_sync?.last_data_sync_utc || ""),
      lastDataError: String(databaseInspector?.data_sync?.last_data_error || ""),
      lastCfgSyncUtc: String(databaseInspector?.sync_target?.last_sync_utc || ""),
      lastCfgError: String(databaseInspector?.sync_target?.last_error || ""),
    };
  };

  const getDbSyncWritingLabel = (dbConn) => {
    if (!dbConn) return "W:0 | P:0";
    const cloudStats = getCloudSyncStatsForDb(dbConn.id);
    if (cloudStats) {
      return `CFG P:${cloudStats.outboxPending} F:${cloudStats.outboxFailed} S:${cloudStats.outboxSent} | DATA H:${cloudStats.histSynced}/${cloudStats.histBacklog} L:${cloudStats.logSynced}/${cloudStats.logBacklog}`;
    }
    return getDbWritingLabel(dbConn.id);
  };
  const getDbSyncWritingTooltip = (dbConn) => {
    const cloudStats = getCloudSyncStatsForDb(dbConn?.id);
    if (!cloudStats) {
      return "Gateway runtime writes. W = writes sent by gateway runtime, P = pending store-forward queue.";
    }
    return [
      "Cloud sync status:",
      "CFG P = pending config documents, F = failed config sync rows, S = total config rows sent.",
      "DATA H = historian synced total / historian backlog, L = logs synced total / logs backlog.",
      "Backlog = local rows not yet replicated to cloud.",
    ].join(" ");
  };

  const getDbSyncLastCheckLabel = (dbConn) => {
    if (!dbConn) return "-";
    const cloudStats = getCloudSyncStatsForDb(dbConn.id);
    if (!cloudStats) return getDbLastCheckLabel(dbConn);
    if (cloudStats.lastDataError) return `ERR: ${cloudStats.lastDataError.slice(0, 72)}`;
    if (cloudStats.lastCfgError) return `ERR: ${cloudStats.lastCfgError.slice(0, 72)}`;
    if (cloudStats.lastDataSyncUtc) return `DATA ${formatElapsedFromUtc(cloudStats.lastDataSyncUtc)}`;
    if (cloudStats.lastCfgSyncUtc) return `CFG ${formatElapsedFromUtc(cloudStats.lastCfgSyncUtc)}`;
    return getDbLastCheckLabel(dbConn);
  };

  const getDbEndpointLabel = (dbConn) => {
    if (!dbConn) return "-";
    const engine = String(dbConn.engine || "").toLowerCase();
    if (engine === "legacy_http") return String(dbConn.legacy_url || "-");
    if (engine === "sqlite") return String(dbConn.sqlite_path || "./data/trustnode_edge.db");
    if (engine === "csv_file" || engine === "txt_file") return String(dbConn.file_path || "-");
    return `${String(dbConn.host || "-")}:${String(dbConn.port || "-")}`;
  };

  const getDbRoleLabel = (dbConn) => {
    const roles = [];
    if (dbConn?.use_gateway) roles.push("Gateway");
    if (dbConn?.use_app) roles.push("App");
    if (dbConn?.use_backup) roles.push("Backup");
    return roles.join(" | ") || "-";
  };

  const isAdminDatabaseUser = Boolean(!isReadonlyCloudMode && currentUser?.role === "admin");

  const localDbUsage = useMemo(() => {
    const sizeBytes = Number(databaseInspector?.db_size_bytes || 0);
    const baseline = 1024 * 1024 * 300; // 300 MB baseline
    const ratio = Math.max(0, Math.min(1, baseline > 0 ? sizeBytes / baseline : 0));
    const percent = Math.round(ratio * 100);
    if (ratio < 0.35) return { sizeBytes, percent, level: "low", label: "Low" };
    if (ratio < 0.75) return { sizeBytes, percent, level: "medium", label: "Medium" };
    return { sizeBytes, percent, level: "high", label: "High" };
  }, [databaseInspector]);

  const cloudProviderCandidates = useMemo(() => {
    const rows = (dbConnections || [])
      .filter((db) => String(db.engine || "").toLowerCase() === "postgresql")
      .filter((db) => dbLocationFromEngine(db.engine) === "remote")
      .filter((db) => db.enabled !== false);
    const supabaseRows = rows.filter((db) => String(db.host || "").toLowerCase().includes("supabase"));
    const others = rows.filter((db) => !String(db.host || "").toLowerCase().includes("supabase"));
    return [...supabaseRows, ...others];
  }, [dbConnections]);

  const dolibarrCandidates = useMemo(
    () =>
      (dbConnections || []).filter((db) => {
        const engine = String(db.engine || "").toLowerCase();
        if (engine !== "legacy_http") return false;
        return dbLocationFromEngine(db.engine) === "remote";
      }),
    [dbConnections]
  );

  const cloudDbRows = useMemo(
    () =>
      (dbConnections || []).filter((db) => {
        const engine = String(db.engine || "").toLowerCase();
        if (dbLocationFromEngine(db.engine) !== "remote") return false;
        return engine === "postgresql" || engine === "legacy_http";
      }),
    [dbConnections]
  );

  const otherDatabaseRows = useMemo(
    () => {
      const sqliteRows = (dbConnections || []).filter((db) => String(db.engine || "").toLowerCase() === "sqlite");
      const mainLocalId =
        String(
          sqliteRows.find((db) => String(db.id || "") === "local-sqlite-default")?.id ||
          sqliteRows.find((db) => Boolean(db.use_app))?.id ||
          sqliteRows[0]?.id ||
          ""
        );
      return (dbConnections || [])
        .filter((db) => Boolean(db.use_backup))
        .filter((db) => String(db.id || "") !== mainLocalId)
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
    },
    [dbConnections]
  );
  const localDatabaseRows = useMemo(
    () => {
      const sqliteRows = (dbConnections || []).filter((db) => String(db.engine || "").toLowerCase() === "sqlite");
      const preferred =
        sqliteRows.find((db) => String(db.id || "") === "local-sqlite-default") ||
        sqliteRows.find((db) => Boolean(db.use_app)) ||
        sqliteRows[0] ||
        null;
      if (preferred) return [preferred];
      return [
        {
          id: MAIN_LOCAL_SQLITE_FALLBACK_ID,
          name: "Local SQLite",
          engine: "sqlite",
          sqlite_path: String(databaseInspector?.db_path || "./data/trustnode_app_store.db"),
          table: "historian_readings",
          enabled: true,
          use_gateway: true,
          use_app: true,
          use_backup: false,
          connection_ok: Boolean(databaseInspector?.db_exists),
          last_check_utc: String(databaseInspector?.data_sync?.last_data_sync_utc || ""),
        },
      ];
    },
    [dbConnections, databaseInspector]
  );

  const getGatewayFooterAddress = (gateway) => {
    if (!gateway) return "-";
    if (gateway.gateway_type === "siemens_opcua" && gateway.opc_url) return gateway.opc_url;
    return gateway.plc_ip || "-";
  };

  const getGatewayFooterDbWriting = (gateway) => {
    if (!gateway?.database_id) return "No DB selected";
    const db = dbConnections.find((c) => c.id === gateway.database_id);
    const dbName = db?.name || "Unknown DB";
    const rt = gatewayRuntimeStatuses[gateway.id] || null;
    const writes = Number(rt?.db_write_count || 0);
    const pending = Number(rt?.db_pending_count || 0);
    return `${dbName} | Writes ${writes} | Pending ${pending}`;
  };
  const dbOverviewStats = useMemo(() => {
    const total = dbConnections.length;
    const online = dbConnections.filter((d) => d.connection_ok).length;
    const offline = total - online;
    const local = dbConnections.filter((d) => ["sqlite", "csv_file", "txt_file"].includes(String(d.engine || ""))).length;
    const cloud = dbConnections.filter((d) => ["postgresql", "mysql", "mssql", "influxdb", "legacy_http"].includes(String(d.engine || ""))).length;
    return { total, online, offline, local, cloud };
  }, [dbConnections]);
  const cloudSourceRows = useMemo(() => {
    if (endpointMode !== "cloud") return [];

    const groups = new Map();
    const dbGroupKeyByName = new Map();

    const ensureGroup = (source, site, area, equipment) => {
      const normSource = String(source || "").trim() || "unknown-source";
      const normSite = String(site || "").trim() || "unknown-site";
      const normArea = String(area || "").trim() || "unknown-area";
      const normEquipment = String(equipment || "").trim() || "unknown-equipment";
      const key = `${normSource}||${normSite}||${normArea}||${normEquipment}`.toLowerCase();
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          source: normSource,
          site: normSite,
          area: normArea,
          equipment: normEquipment,
          dbNames: new Set(),
          gatewayIds: new Set(),
          lastConfigUtc: "",
          lastLiveUtc: "",
          liveRows: 0,
        });
      }
      return groups.get(key);
    };

    for (const db of dbConnections || []) {
      if (dbLocationFromEngine(db.engine) !== "remote") continue;
      if (db.enabled === false) continue;
      if (db.cloud_sync_enabled === false) continue;
      const g = ensureGroup(db.source, db.site, db.area, db.equipment);
      const dbName = String(db.name || "").trim();
      if (dbName) {
        g.dbNames.add(dbName);
        dbGroupKeyByName.set(dbName.toLowerCase(), g.key);
      }
      if (db.last_check_utc && (!g.lastConfigUtc || String(db.last_check_utc) > String(g.lastConfigUtc))) {
        g.lastConfigUtc = String(db.last_check_utc);
      }
    }

    for (const gw of gatewayConfigs || []) {
      const db = (dbConnections || []).find((d) => String(d.id || "") === String(gw.database_id || ""));
      if (!db) continue;
      const dbName = String(db.name || "").trim();
      const groupKey = dbName ? dbGroupKeyByName.get(dbName.toLowerCase()) : "";
      if (!groupKey) continue;
      const g = groups.get(groupKey);
      if (!g) continue;
      g.gatewayIds.add(String(gw.id || ""));
    }

    for (const row of dataLog || []) {
      const dbName = String(row?.database_name || "").trim();
      const directGroup = ensureGroup(row?.source, row?.site, row?.area, row?.equipment);
      const mappedGroupKey = dbName ? dbGroupKeyByName.get(dbName.toLowerCase()) : "";
      const g = mappedGroupKey && groups.get(mappedGroupKey) ? groups.get(mappedGroupKey) : directGroup;
      g.liveRows += 1;
      if (dbName) g.dbNames.add(dbName);
      const gid = String(row?.gateway_id || "").trim();
      if (gid) g.gatewayIds.add(gid);
      const ts = String(row?.ts || row?.ts_utc || "").trim();
      if (ts && (!g.lastLiveUtc || ts > g.lastLiveUtc)) g.lastLiveUtc = ts;
    }

    return Array.from(groups.values())
      .map((g) => {
        const ageMs = g.lastLiveUtc ? Date.now() - new Date(g.lastLiveUtc).getTime() : Number.POSITIVE_INFINITY;
        const liveHealthy = Number.isFinite(ageMs) ? ageMs <= 10000 : false;
        return {
          ...g,
          dbCount: g.dbNames.size,
          gatewayCount: g.gatewayIds.size,
          liveHealthy,
          dbNamesText: Array.from(g.dbNames).sort().join(", "),
        };
      })
      .sort((a, b) => String(b.lastLiveUtc || b.lastConfigUtc || "").localeCompare(String(a.lastLiveUtc || a.lastConfigUtc || "")));
  }, [endpointMode, dbConnections, gatewayConfigs, dataLog]);
  const selectedCloudEdge = useMemo(() => {
    if (selectedCloudEdgeKey === CLOUD_EDGE_ALL_KEY) return null;
    return cloudSourceRows.find((r) => String(r.key) === String(selectedCloudEdgeKey)) || null;
  }, [selectedCloudEdgeKey, cloudSourceRows]);
  const isCloudEdgeFilterActive = Boolean(isHostedWebClient && endpointMode === "cloud" && selectedCloudEdge);
  const normEdge = (v) => String(v || "").trim().toLowerCase();
  const edgeMatches = (source, site, area, equipment) => {
    if (!isCloudEdgeFilterActive || !selectedCloudEdge) return true;
    return (
      normEdge(source) === normEdge(selectedCloudEdge.source) &&
      normEdge(site) === normEdge(selectedCloudEdge.site) &&
      normEdge(area) === normEdge(selectedCloudEdge.area) &&
      normEdge(equipment) === normEdge(selectedCloudEdge.equipment)
    );
  };
  const matchesDbEdge = (db) => {
    if (!db) return false;
    if (!isCloudEdgeFilterActive) return true;
    if (dbLocationFromEngine(db.engine) !== "remote") return false;
    return edgeMatches(db.source, db.site, db.area, db.equipment);
  };
  const dbConnectionsView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return dbConnections;
    return dbConnections.filter((db) => dbLocationFromEngine(db.engine) !== "remote" || matchesDbEdge(db));
  }, [dbConnections, isCloudEdgeFilterActive, selectedCloudEdgeKey]);
  const gatewayConfigsView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return gatewayConfigs;
    return gatewayConfigs.filter((g) => {
      const db = dbConnections.find((d) => String(d.id || "") === String(g.database_id || ""));
      return matchesDbEdge(db);
    });
  }, [gatewayConfigs, dbConnections, isCloudEdgeFilterActive, selectedCloudEdgeKey]);
  const visibleGatewayIdSet = useMemo(
    () => new Set((gatewayConfigsView || []).map((g) => String(g.id || ""))),
    [gatewayConfigsView]
  );
  const devicesView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return devices;
    return devices.filter((d) =>
      gatewayConfigsView.some((g) => String(g.device_id || "") === String(d.id || ""))
    );
  }, [devices, gatewayConfigsView, isCloudEdgeFilterActive]);
  const dataLogView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return dataLog;
    return dataLog.filter((row) => {
      const gid = String(row?.gateway_id || "");
      if (gid && visibleGatewayIdSet.has(gid)) return true;
      const dbName = String(row?.database_name || "").trim().toLowerCase();
      if (dbName) {
        const db = dbConnections.find((d) => String(d.name || "").trim().toLowerCase() === dbName);
        if (db && matchesDbEdge(db)) return true;
      }
      return edgeMatches(row?.source, row?.site, row?.area, row?.equipment);
    });
  }, [dataLog, isCloudEdgeFilterActive, visibleGatewayIdSet, dbConnections, selectedCloudEdgeKey]);
  const appLogsView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return appLogs;
    return appLogs.filter((row) => {
      const gid = String(row?.gateway_id || "");
      if (gid && visibleGatewayIdSet.has(gid)) return true;
      const dbName = String(row?.database_name || "").trim().toLowerCase();
      if (dbName) {
        const db = dbConnections.find((d) => String(d.name || "").trim().toLowerCase() === dbName);
        if (db && matchesDbEdge(db)) return true;
      }
      return false;
    });
  }, [appLogs, isCloudEdgeFilterActive, visibleGatewayIdSet, dbConnections, selectedCloudEdgeKey]);
  const liveTagValuesView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return liveTagValues;
    const next = {};
    for (const [key, val] of Object.entries(liveTagValues || {})) {
      const gid = String(val?.gateway_id || key.split("::")[0] || "");
      if (visibleGatewayIdSet.has(gid)) next[key] = val;
    }
    return next;
  }, [liveTagValues, isCloudEdgeFilterActive, visibleGatewayIdSet]);
  const gatewayRuntimeStatusesView = useMemo(() => {
    if (!isCloudEdgeFilterActive) return gatewayRuntimeStatuses;
    const next = {};
    for (const g of gatewayConfigsView) {
      const gid = String(g.id || "");
      if (gatewayRuntimeStatuses[gid]) next[gid] = gatewayRuntimeStatuses[gid];
    }
    return next;
  }, [gatewayRuntimeStatuses, gatewayConfigsView, isCloudEdgeFilterActive]);
  useEffect(() => {
    if (!(isHostedWebClient && endpointMode === "cloud")) {
      if (selectedCloudEdgeKey !== CLOUD_EDGE_ALL_KEY) setSelectedCloudEdgeKey(CLOUD_EDGE_ALL_KEY);
      return;
    }
    if (selectedCloudEdgeKey === CLOUD_EDGE_ALL_KEY) return;
    if (cloudSourceRows.some((r) => String(r.key) === String(selectedCloudEdgeKey))) return;
    setSelectedCloudEdgeKey(CLOUD_EDGE_ALL_KEY);
  }, [isHostedWebClient, endpointMode, selectedCloudEdgeKey, cloudSourceRows]);

  useEffect(() => {
    const preferredId = String(databaseInspector?.cloud_target?.id || "").trim();
    if (preferredId && cloudProviderCandidates.some((db) => String(db.id) === preferredId)) {
      setCloudProviderDbId(preferredId);
      return;
    }
    if (cloudProviderDbId && cloudProviderCandidates.some((db) => String(db.id) === String(cloudProviderDbId))) {
      return;
    }
    setCloudProviderDbId(String(cloudProviderCandidates[0]?.id || ""));
  }, [databaseInspector, cloudProviderCandidates, cloudProviderDbId]);

  useEffect(() => {
    const enabled = dolibarrCandidates.some((db) => db.enabled !== false && db.use_gateway !== false);
    setDolibarrMirrorEnabled(enabled);
  }, [dolibarrCandidates]);

  useEffect(() => {
    const selected = cloudProviderCandidates.find(
      (db) => String(db.id || "") === String(cloudProviderDbId || "")
    );
    if (!selected) return;
    const host = String(selected.host || "").toLowerCase();
    const port = Number(selected.port || 0);
    if (host.includes(".pooler.supabase.com")) {
      if (port === 6543) setCloudSupabaseMode("transaction_pooler");
      else setCloudSupabaseMode("session_pooler");
      setCloudSupabaseHasIpv4AddOn(false);
      return;
    }
    if (host.startsWith("db.") && host.endsWith(".supabase.co") && port === 5432) {
      setCloudSupabaseMode("direct_ipv4");
      setCloudSupabaseHasIpv4AddOn(true);
      return;
    }
    setCloudSupabaseMode("auto");
  }, [cloudProviderDbId, cloudProviderCandidates]);

  useEffect(() => {
    if (!cloudDbRows.length) {
      if (selectedCloudDbId) setSelectedCloudDbId("");
      if (trustnodeCloudEnabled) setTrustnodeCloudEnabled(false);
      return;
    }
    if (!selectedCloudDbId || !cloudDbRows.some((db) => String(db.id || "") === String(selectedCloudDbId))) {
      setSelectedCloudDbId(String(cloudDbRows[0].id || ""));
      return;
    }
    const selected = cloudDbRows.find((db) => String(db.id || "") === String(selectedCloudDbId)) || null;
    const enabled = Boolean(selected && selected.enabled !== false && selected.cloud_sync_enabled !== false);
    if (enabled !== trustnodeCloudEnabled) setTrustnodeCloudEnabled(enabled);
  }, [cloudDbRows, selectedCloudDbId, trustnodeCloudEnabled]);

  useEffect(() => {
    if (!localDatabaseRows.length) {
      if (selectedLocalDbId) setSelectedLocalDbId("");
      return;
    }
    if (!selectedLocalDbId || !localDatabaseRows.some((db) => String(db.id || "") === String(selectedLocalDbId))) {
      setSelectedLocalDbId(String(localDatabaseRows[0].id || ""));
    }
  }, [localDatabaseRows, selectedLocalDbId]);

  const selectedLocalDbIsMain = useMemo(() => {
    const selected = localDatabaseRows.find((db) => String(db.id || "") === String(selectedLocalDbId || ""));
    if (!selected) return true;
    return String(selected.id || "") === MAIN_LOCAL_SQLITE_FALLBACK_ID || String(selected.id || "") === "local-sqlite-default";
  }, [localDatabaseRows, selectedLocalDbId]);

  useEffect(() => {
    if (!otherDatabaseRows.length) {
      if (selectedOtherDbId) setSelectedOtherDbId("");
      return;
    }
    if (!selectedOtherDbId || !otherDatabaseRows.some((db) => String(db.id || "") === String(selectedOtherDbId))) {
      setSelectedOtherDbId(String(otherDatabaseRows[0].id || ""));
    }
  }, [otherDatabaseRows, selectedOtherDbId]);

  const getScopeDbs = (scope) => {
    const key = scope === "app" ? "use_app" : scope === "backup" ? "use_backup" : "use_gateway";
    const rows = dbConnectionsView.filter((d) => Boolean(d?.[key]));
    const local = rows.filter((d) => dbLocationFromEngine(d.engine) === "local");
    const remote = rows.filter((d) => dbLocationFromEngine(d.engine) === "remote");
    return { all: rows, local, remote };
  };
  const unknownRunningGateways = useMemo(() => {
    const known = new Set(gatewayConfigsView.map((g) => g.id));
    return Object.values(gatewayRuntimeStatusesView).filter(
      (s) => Boolean(s?.running) && !known.has(String(s?.gateway_id || ""))
    );
  }, [gatewayRuntimeStatusesView, gatewayConfigsView]);

  const isGatewayRunning = (gateway) => {
    if (!gateway) return false;
    return Boolean(gatewayRuntimeStatusesView[gateway.id]?.running);
  };
  const anyGatewayRunning = useMemo(
    () => gatewayConfigsView.some((g) => Boolean(gatewayRuntimeStatusesView[g.id]?.running)),
    [gatewayConfigsView, gatewayRuntimeStatusesView]
  );
  const contentBottomPad = useMemo(
    () => (footerCollapsed ? 16 : Math.max(footerHeight + 18, 140)),
    [footerCollapsed, footerHeight]
  );

  useEffect(() => {
    const readHeight = () => {
      const h = footerRef.current?.offsetHeight || 0;
      setFooterHeight(h);
    };
    readHeight();
    window.addEventListener("resize", readHeight);
    return () => window.removeEventListener("resize", readHeight);
  }, [footerCollapsed, gatewayConfigsView.length, gatewayRuntimeStatusesView]);

  const deviceRows = useMemo(
    () =>
      devicesView.map((d) => {
        const relatedGateways = gatewayConfigsView.filter((g) => String(g.device_id || "") === String(d.id || ""));
        const relatedRuntime = relatedGateways.map((g) => gatewayRuntimeStatusesView[g.id]).filter(Boolean);
        const hasRelated = relatedRuntime.length > 0;
        const allStoppedClean =
          endpointMode === "cloud" &&
          hasRelated &&
          relatedRuntime.every(
            (rt) =>
              rt?.running === false &&
              !String(rt?.last_error || "").trim() &&
              !String(rt?.db_last_error || "").trim()
          );
        const protocolOk = d.protocol_ok ?? d.port_ok;
        let status = "Offline";
        let statusKey = "offline";
        if (allStoppedClean) {
          status = "Stopped";
          statusKey = "warning";
        } else if (d.ping_ok && protocolOk) {
          status = "Online";
          statusKey = "online";
        } else if (d.ping_ok && !protocolOk) {
          status = "IP OK / Protocol Fail";
          statusKey = "warning";
        } else if (!d.ping_ok && protocolOk) {
          status = "Protocol OK / Ping Fail";
          statusKey = "warning";
        }
        return { ...d, protocolOk, status, statusKey };
      }),
    [devicesView, gatewayConfigsView, gatewayRuntimeStatusesView, endpointMode]
  );

  const tagRows = useMemo(() => {
    const rows = [];
    for (const gw of gatewayConfigsView) {
      const device = devicesView.find((d) => d.id === gw.device_id);
      const tags = Array.isArray(gw.tags) ? gw.tags : [];
      for (const tag of tags) {
        const latest = dataLogView.find(
          (r) =>
            String(r.tag || "") === String(tag) &&
            (!r.gateway_id || String(r.gateway_id) === String(gw.id))
        );
        const live = liveTagValuesView[`${String(gw.id)}::${normalizeTagName(tag)}`];
        rows.push({
          key: `${gw.id}::${tag}`,
          tag_name: tag,
          device_name: device?.name || "-",
          gateway_id: gw.id,
          gateway_name: gw.name || gw.id,
          period_ms: Number(gw.interval_ms || 0),
          last_value: latest?.value ?? live?.value ?? "-",
          last_ts: latest?.ts ? fmtTs(latest.ts) : live?.ts ? fmtTs(live.ts) : "-"
        });
      }
    }
    return rows;
  }, [gatewayConfigsView, devicesView, dataLogView, liveTagValuesView]);

  const filteredTagRows = useMemo(() => {
    const deviceNeedle = String(tagFilters.device || "").trim().toLowerCase();
    const tagNeedle = String(tagFilters.tag || "").trim().toLowerCase();
    const valueNeedle = String(tagFilters.value || "").trim().toLowerCase();
    return tagRows.filter((row) => {
      if (tagFilters.gatewayId && String(row.gateway_id) !== String(tagFilters.gatewayId)) return false;
      if (deviceNeedle && !String(row.device_name || "").toLowerCase().includes(deviceNeedle)) return false;
      if (tagNeedle && !String(row.tag_name || "").toLowerCase().includes(tagNeedle)) return false;
      if (valueNeedle && !String(row.last_value ?? "").toLowerCase().includes(valueNeedle)) return false;
      return true;
    });
  }, [tagRows, tagFilters]);

  const dashboardItems = useMemo(() => {
    return dashboardWidgets.map((w) => {
      const gateway = gatewayConfigsView.find((g) => String(g.id) === String(w.gateway_id)) || null;
      if (!gateway) return null;
      const device = devicesView.find((d) => String(d.id) === String(gateway?.device_id || "")) || null;
      const points = dataLogView
        .filter(
          (r) =>
            String(r.gateway_id || "") === String(w.gateway_id || "") &&
            String(r.tag || "") === String(w.tag_name || "")
        )
        .slice(0, Number(w.readings_count || 120))
        .reverse()
        .map((r, idx) => ({
          idx: idx + 1,
          ts: r.ts ? fmtTs(r.ts).slice(11, 19) : "",
          value: Number(r.value)
        }));
      const last = points.length ? points[points.length - 1] : null;
      const prev = points.length > 1 ? points[points.length - 2] : null;
      const delta = last && prev ? Number(last.value) - Number(prev.value) : 0;
      const monitorRow = {
        key: `${w.id}`,
        tag_name: w.tag_name,
        device_name: device?.name || "-",
        gateway_id: w.gateway_id,
        gateway_name: gateway?.name || w.gateway_id || "-",
        period_ms: Number(gateway?.interval_ms || 0),
        last_value: last ? Number(last.value).toFixed(3) : "-",
        last_ts: last?.ts || "-"
      };
      return {
        ...w,
        title: w.title || w.tag_name,
        gateway_name: gateway?.name || "-",
        device_name: device?.name || "-",
        last_value: last ? Number(last.value) : null,
        last_ts: last?.ts || "-",
        delta,
        series: points,
        monitorRow
      };
    }).filter(Boolean);
  }, [dashboardWidgets, gatewayConfigsView, devicesView, dataLogView]);

  const tagMonitorSeries = useMemo(() => {
    if (!tagMonitorSelection) return [];
    return dataLogView
      .filter(
        (r) =>
          String(r.tag || "") === String(tagMonitorSelection.tag_name || "") &&
          (!r.gateway_id || String(r.gateway_id) === String(tagMonitorSelection.gateway_id || ""))
      )
      .slice(0, 120)
      .reverse()
      .map((r, idx) => ({
        idx: idx + 1,
        ts: r.ts ? fmtTs(r.ts).slice(11, 19) : "",
        value: Number(r.value)
      }));
  }, [dataLogView, tagMonitorSelection]);

  const tagMonitorLatest = useMemo(() => {
    if (!tagMonitorSelection) return null;
    return dataLogView.find(
      (r) =>
        String(r.tag || "") === String(tagMonitorSelection.tag_name || "") &&
        (!r.gateway_id || String(r.gateway_id) === String(tagMonitorSelection.gateway_id || ""))
    ) || null;
  }, [dataLogView, tagMonitorSelection]);

  const tagMonitorKpi = useMemo(() => {
    if (!tagMonitorSeries.length) {
      return { last: "-", avg: "-", min: "-", max: "-", delta: "-", lastTs: "-" };
    }
    const vals = tagMonitorSeries.map((x) => Number(x.value)).filter((v) => !Number.isNaN(v));
    if (!vals.length) return { last: "-", avg: "-", min: "-", max: "-", delta: "-", lastTs: "-" };
    const last = vals[vals.length - 1];
    const prev = vals.length > 1 ? vals[vals.length - 2] : null;
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    return {
      last: last.toFixed(3),
      avg: avg.toFixed(3),
      min: min.toFixed(3),
      max: max.toFixed(3),
      delta: prev == null ? "-" : (last - prev).toFixed(3),
      lastTs: tagMonitorLatest?.ts ? fmtTs(tagMonitorLatest.ts) : (tagMonitorSelection?.last_ts || "-")
    };
  }, [tagMonitorSeries, tagMonitorSelection, tagMonitorLatest]);

  const historianRows = useMemo(() => {
    const gatewayNameById = Object.fromEntries(gatewayConfigsView.map((g) => [g.id, g.name]));
    return dataLogView.filter((row) => {
      if (!inRange(row.ts, historianFilters.from, historianFilters.to)) return false;
      if (historianFilters.tag && !String(row.tag || "").toLowerCase().includes(historianFilters.tag.toLowerCase())) return false;
      if (historianFilters.gatewayId && row.gateway_id !== historianFilters.gatewayId) return false;
      if (historianFilters.deviceName && !String(row.device_name || "").toLowerCase().includes(historianFilters.deviceName.toLowerCase())) return false;
      if (historianFilters.quality !== "all" && String(row.quality_label || "").toUpperCase() !== historianFilters.quality) return false;
      return true;
    }).map((row) => ({
      ...row,
      gateway_name: row.gateway_name || gatewayNameById[row.gateway_id] || row.gateway_id || "-"
    }));
  }, [dataLogView, historianFilters, gatewayConfigsView]);

  const filteredLogs = useMemo(() => {
    return appLogsView.filter((row) => {
      if (!inRange(row.ts, logFilters.from, logFilters.to)) return false;
      if (logFilters.level !== "all" && String(row.level || "").toLowerCase() !== logFilters.level) return false;
      if (logFilters.category !== "all" && String(row.category || "").toLowerCase() !== logFilters.category) return false;
      if (logFilters.gatewayId && row.gateway_id !== logFilters.gatewayId) return false;
      if (logFilters.text) {
        const txt = logFilters.text.toLowerCase();
        const hay = `${row.message || ""} ${row.gateway_name || ""} ${row.device_name || ""} ${row.database_name || ""}`.toLowerCase();
        if (!hay.includes(txt)) return false;
      }
      return true;
    });
  }, [appLogsView, logFilters]);

  const toggleTheme = () => setTheme((prev) => (prev === "dark" ? "light" : "dark"));
  const toggleFullscreen = async () => {
    try {
      const doc = document;
      const el = document.documentElement;
      if (!getFullscreenState()) {
        if (el.requestFullscreen) {
          await el.requestFullscreen();
        } else if (el.webkitRequestFullscreen) {
          el.webkitRequestFullscreen();
        }
      } else {
        if (doc.exitFullscreen) {
          await doc.exitFullscreen();
        } else if (doc.webkitExitFullscreen) {
          doc.webkitExitFullscreen();
        }
      }
    } catch (err) {
      setError(`Fullscreen toggle failed: ${String(err)}`);
    }
  };

  const toggleSection = (sectionId) => {
    setExpandedSections((prev) => ({ ...prev, [sectionId]: !prev[sectionId] }));
  };

  const handleNavClick = (page) => {
    if (!canOpenPage(page)) return;
    setActivePage(page);
  };

  const openTagMonitor = (row) => {
    setTagMonitorSelection(row);
    setTagMonitorChartType("line");
    setShowTagMonitorModal(true);
  };

  const openAddDashboardWidget = () => {
    if (!canEditPage("dashboard")) return;
    const gatewayId = gatewayConfigs[0]?.id || "";
    const tag = (triggerTagsByGateway[gatewayId] || [])[0] || "";
    setEditingDashboardWidgetId(null);
    setDashboardWidgetForm({
      title: "",
      gateway_id: gatewayId,
      tag_name: tag,
      readings_count: 120,
      color: "#16a34a",
      chart_type: "line"
    });
    setShowDashboardWidgetModal(true);
  };

  const openEditDashboardWidget = (item) => {
    if (!canEditPage("dashboard")) return;
    if (!item) return;
    setEditingDashboardWidgetId(item.id);
    setDashboardWidgetForm({
      title: item.title || "",
      gateway_id: item.gateway_id || "",
      tag_name: item.tag_name || "",
      readings_count: Number(item.readings_count || 120),
      color: item.color || "#16a34a",
      chart_type: item.chart_type === "bar" ? "bar" : "line"
    });
    setShowDashboardWidgetModal(true);
  };

  const saveDashboardWidget = () => {
    if (!canEditPage("dashboard")) return;
    const gatewayId = String(dashboardWidgetForm.gateway_id || "").trim();
    const tagName = String(dashboardWidgetForm.tag_name || "").trim();
    const count = Math.min(500, Math.max(20, Number(dashboardWidgetForm.readings_count || 120)));
    if (!gatewayId || !tagName) {
      setError("Dashboard item requires gateway and tag.");
      return;
    }
    const payload = {
      id: editingDashboardWidgetId || `dw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      title: dashboardWidgetForm.title.trim() || tagName,
      gateway_id: gatewayId,
      tag_name: tagName,
      readings_count: count,
      color: dashboardWidgetForm.color || "#16a34a",
      chart_type: dashboardWidgetForm.chart_type === "bar" ? "bar" : "line"
    };
    setDashboardWidgets((prev) => {
      if (editingDashboardWidgetId) return prev.map((w) => (w.id === editingDashboardWidgetId ? payload : w));
      return [...prev, payload];
    });
    setShowDashboardWidgetModal(false);
    setEditingDashboardWidgetId(null);
  };

  const removeDashboardWidget = (itemId) => {
    if (!canEditPage("dashboard")) return;
    withConfirm("Delete Dashboard Item", "Remove this dashboard item?", () => {
      setDashboardWidgets((prev) => prev.filter((w) => w.id !== itemId));
    });
  };

  const toggleDashboardWidgetChartType = (itemId) => {
    if (!canEditPage("dashboard")) return;
    setDashboardWidgets((prev) =>
      prev.map((w) =>
        w.id === itemId ? { ...w, chart_type: w.chart_type === "bar" ? "line" : "bar" } : w
      )
    );
  };

  const moveDashboardWidget = (itemId, direction) => {
    if (!canEditPage("dashboard")) return;
    setDashboardWidgets((prev) => {
      const idx = prev.findIndex((w) => w.id === itemId);
      if (idx < 0) return prev;
      const nextIdx = idx + direction;
      if (nextIdx < 0 || nextIdx >= prev.length) return prev;
      const next = [...prev];
      const [row] = next.splice(idx, 1);
      next.splice(nextIdx, 0, row);
      return next;
    });
  };

  const onApplyEndpoint = () => {
    const nextMode = isHostedWebClient ? "cloud" : (endpointMode === "cloud" ? "cloud" : "local");
    const normalizedCloud = String(cloudUrl || window.location.origin || "").trim().replace(/\/+$/, "");
    if (nextMode === "cloud" && !/^https?:\/\//i.test(normalizedCloud)) {
      setError("Cloud URL must start with http:// or https://");
      return;
    }
    if (!canEditPage("database")) return;
    setBackendTarget(nextMode, normalizedCloud);
    setConfig(null);
    setStatus(null);
    setReadings([]);
    setHistory([]);
    setBootState("initializing");
    setWsState("connecting");
    setEndpointVersion((v) => v + 1);
    setError("");
  };

  const buildGatewayRuntimePayload = (gateway) => {
    if (!gateway) throw new Error("Gateway configuration not found.");
    if (!gateway.database_id) throw new Error("Gateway has no database connection selected.");
    const db = dbConnectionsRef.current.find((c) => c.id === gateway.database_id);
    if (!db) throw new Error("Selected database connection was not found.");
    return {
      gateway_id: gateway.id,
      config: {
        gateway_type: gateway.gateway_type,
        plc_ip: gateway.plc_ip,
        opc_url: gateway.opc_url || "",
        tags: gateway.tags || [],
        collection_trigger_mode: collectionTriggerMode === "all" ? "all" : "any",
        collection_triggers: collectionTriggers
          .filter((t) => t.enabled !== false)
          .map((t) => ({
            gateway_id: String(t.gateway_id || ""),
            tag_name: String(t.tag_name || ""),
            operator: String(t.operator || ">="),
            value: Number(t.value),
            trigger_type: t.trigger_type === "one_time" ? "one_time" : "continuous",
            enabled: t.enabled !== false
          })),
        interval_ms: Number(gateway.interval_ms || 1000),
        equipment: db.equipment || "",
        site: db.site || "",
        area: db.area || ""
      },
      db_sink: {
        name: db.name || "",
        engine: db.engine,
        host: db.host || "",
        port: Number(db.port || 0),
        database: db.database || "",
        username: db.username || "",
        password: db.password || "",
        sqlite_path: db.sqlite_path || "",
        file_path: db.file_path || "",
        legacy_url: db.legacy_url || "",
        legacy_api_token: db.legacy_api_token || "",
        source: db.source || "",
        site: db.site || "",
        area: db.area || "",
        equipment: db.equipment || "",
        schema: db.schema || "public",
        table: db.table || "plc_readings",
        tls: Boolean(db.tls)
      }
    };
  };

  const startGatewayProfile = async (gateway) => {
    if (!canControlGateways) return;
    if (!gateway) {
      setError("Select a gateway configuration first.");
      return;
    }
    const db = dbConnections.find((c) => c.id === gateway.database_id);
    const activeGatewayTriggers = collectionTriggers.filter((t) => t.enabled !== false);
    let payload;
    try {
      payload = buildGatewayRuntimePayload(gateway);
    } catch (e) {
      setError(String(e));
      return;
    }
    try {
      const res = await startGatewayInstance(payload);
      if (!res?.started) {
        setError(res?.message || "Failed to start gateway");
        addAppLog({
          level: "error",
          category: "gateway",
          gateway_id: gateway.id,
          gateway_name: gateway.name || gateway.id,
          message: res?.message || "Failed to start gateway"
        });
        return;
      }
      markGatewayRunningState([gateway.id], true);
      await refreshGatewayRuntimes();
      addAppLog({
        level: "info",
        category: "gateway",
        gateway_id: gateway.id,
        gateway_name: gateway.name || gateway.id,
        database_name: db.name || "",
        message: "Gateway started"
      });
      if (activeGatewayTriggers.length) {
        addAppLog({
          level: "info",
          category: "gateway",
          gateway_id: gateway.id,
          gateway_name: gateway.name || gateway.id,
          message:
            `Trigger mode active (${activeGatewayTriggers.length} trigger(s)): ` +
            "collection/write remains paused until condition becomes TRUE."
        });
      }
      setError("");
    } catch (err) {
      setError(`Start gateway failed: ${String(err)}`);
      addAppLog({
        level: "error",
        category: "gateway",
        gateway_id: gateway.id,
        gateway_name: gateway.name || gateway.id,
        message: `Start failed: ${String(err)}`
      });
    }
  };

  const stopGatewayProfile = async (gatewayId = selectedGateway?.id) => {
    if (!canControlGateways) return;
    if (!gatewayId) return;
    try {
      await stopGatewayInstance(gatewayId);
      markGatewayRunningState([gatewayId], false);
      setLiveTagValues((prev) => {
        const next = {};
        const prefix = `${String(gatewayId)}::`;
        for (const [k, v] of Object.entries(prev || {})) {
          if (!k.startsWith(prefix)) next[k] = v;
        }
        return next;
      });
      await refreshGatewayRuntimes();
      const gw = gatewayConfigs.find((g) => g.id === gatewayId);
      addAppLog({
        level: "info",
        category: "gateway",
        gateway_id: gatewayId,
        gateway_name: gw?.name || gatewayId,
        message: "Gateway stopped"
      });
      setError("");
    } catch (err) {
      setError(`Stop gateway failed: ${String(err)}`);
      addAppLog({
        level: "error",
        category: "gateway",
        gateway_id: gatewayId,
        message: `Stop failed: ${String(err)}`
      });
    }
  };

  const stopAllGatewayProfiles = async () => {
    if (!canControlGateways) return;
    try {
      await stopAllGatewayInstances();
      markGatewayRunningState(gatewayConfigs.map((g) => g.id), false);
      await refreshGatewayRuntimes();
      addAppLog({ level: "info", category: "gateway", message: "Stop all gateways requested" });
      setError("");
    } catch (err) {
      setError(`Stop all gateways failed: ${String(err)}`);
      addAppLog({ level: "error", category: "gateway", message: `Stop all failed: ${String(err)}` });
    }
  };

  const startAllGatewayProfiles = async () => {
    if (!canControlGateways) return;
    if (!gatewayConfigs.length) return;
    for (const g of gatewayConfigs) {
      await startGatewayProfile(g);
    }
    await refreshGatewayRuntimes();
  };

  const toggleGatewayProfile = async (gateway) => {
    if (!gateway) return;
    try {
      if (isGatewayRunning(gateway)) {
        await stopGatewayProfile(gateway.id);
        return;
      }
      await startGatewayProfile(gateway);
    } catch (err) {
      setError(`Gateway toggle failed: ${String(err)}`);
    }
  };

  useEffect(() => {
    if (!appStoreHydrated) return;
    if (!currentUser) return;
    const canRestartGateways = Boolean(
      !isReadonlyCloudMode &&
      (currentUser.role === "admin" || currentUser.permissions?.gateway_runtime_control)
    );
    if (!canRestartGateways) return;

    const signature = JSON.stringify({
      gateways: gatewayConfigs.map((g) => ({
        id: g.id,
        device_id: g.device_id,
        gateway_type: g.gateway_type,
        plc_ip: g.plc_ip,
        opc_url: g.opc_url,
        database_id: g.database_id,
        interval_ms: g.interval_ms,
        tags: (g.tags || []).map((t) => String(t))
      })),
      devices: devices.map((d) => ({
        id: d.id,
        name: d.name,
        gateway_type: d.gateway_type,
        plc_ip: d.plc_ip,
        opc_url: d.opc_url,
        opc_node_ids_text: d.opc_node_ids_text || "",
      })),
      dbs: dbConnections.map((d) => ({
        id: d.id,
        engine: d.engine,
        host: d.host,
        port: d.port,
        database: d.database,
        username: d.username,
        password: d.password,
        sqlite_path: d.sqlite_path,
        file_path: d.file_path,
        legacy_url: d.legacy_url,
        legacy_api_token: d.legacy_api_token,
        schema: d.schema,
        table: d.table,
        tls: Boolean(d.tls),
        enabled: d.enabled !== false
      })),
      collectionTriggerMode,
      collectionTriggers: collectionTriggers.map((t) => ({
        id: t.id,
        gateway_id: t.gateway_id,
        tag_name: t.tag_name,
        operator: t.operator,
        value: t.value,
        trigger_type: t.trigger_type === "one_time" ? "one_time" : "continuous",
        enabled: t.enabled !== false
      })),
      triggerRules: triggerRules.map((r) => ({
        id: r.id,
        gateway_id: r.gateway_id,
        tag_name: r.tag_name,
        lower_enabled: Boolean(r.lower_enabled),
        lower_operator: r.lower_operator,
        lower_value: r.lower_value,
        upper_enabled: Boolean(r.upper_enabled),
        upper_operator: r.upper_operator,
        upper_value: r.upper_value,
        enabled: r.enabled !== false
      }))
    });

    if (!configRestartInitializedRef.current) {
      configRestartInitializedRef.current = true;
      configRestartSignatureRef.current = signature;
      return;
    }
    if (signature === configRestartSignatureRef.current) return;
    configRestartSignatureRef.current = signature;
    if (configRestartBusyRef.current) return;

    const targetGateways = gatewayConfigs.slice();
    if (!targetGateways.length) return;

    let cancelled = false;
    const restartGatewaysAfterConfigChange = async () => {
      configRestartBusyRef.current = true;
      try {
        addAppLog({
          level: "warning",
          category: "gateway",
          message: `Configuration changed. Restarting ${targetGateways.length} gateway(s) to apply updates.`
        });
        for (const g of targetGateways) {
          if (cancelled) return;
          try {
            await stopGatewayInstance(g.id);
          } catch {}
        }
        await refreshGatewayRuntimes();
        setLiveTagValues({});
        for (const g of targetGateways) {
          if (cancelled) return;
          try {
            const payload = buildGatewayRuntimePayload(g);
            const res = await startGatewayInstance(payload);
            if (!res?.started) {
              addAppLog({
                level: "error",
                category: "gateway",
                gateway_id: g.id,
                gateway_name: g.name || g.id,
                message: `Auto-restart failed: ${res?.message || "unknown error"}`
              });
            }
          } catch (e) {
            addAppLog({
              level: "error",
              category: "gateway",
              gateway_id: g.id,
              gateway_name: g.name || g.id,
              message: `Auto-restart failed: ${String(e)}`
            });
          }
        }
        await refreshGatewayRuntimes();
        setError("");
      } finally {
        configRestartBusyRef.current = false;
      }
    };
    restartGatewaysAfterConfigChange();
    return () => {
      cancelled = true;
    };
  }, [
    appStoreHydrated,
    currentUser,
    isReadonlyCloudMode,
    gatewayConfigs,
    devices,
    dbConnections,
    collectionTriggerMode,
    collectionTriggers,
    triggerRules,
    gatewayRuntimeStatuses
  ]);

  const buildGatewayTxt = (gateway) => {
    const tags = (gateway.tags || []).join(";");
    return [
      `NAME=${gateway.name || ""}`,
      `DEVICE_ID=${gateway.device_id || ""}`,
      `GATEWAY_TYPE=${gateway.gateway_type || ""}`,
      `PLC_IP=${gateway.plc_ip || ""}`,
      `OPC_URL=${gateway.opc_url || ""}`,
      `DATABASE_ID=${gateway.database_id || ""}`,
      `INTERVAL_MS=${gateway.interval_ms || 1000}`,
      `TAGS=${tags}`
    ].join("\n");
  };

  const parseGatewayTxt = (raw) => {
    const out = {};
    String(raw || "")
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean)
      .forEach((line) => {
        const idx = line.indexOf("=");
        if (idx <= 0) return;
        const key = line.slice(0, idx).trim().toUpperCase();
        const value = line.slice(idx + 1).trim();
        out[key] = value;
      });
    return {
      name: out.NAME || "",
      device_id: out.DEVICE_ID || "",
      gateway_type: out.GATEWAY_TYPE || "allen_bradley",
      plc_ip: out.PLC_IP || "",
      opc_url: out.OPC_URL || "",
      database_id: out.DATABASE_ID || "",
      interval_ms: Number(out.INTERVAL_MS || 1000),
      tags_text: out.TAGS || ""
    };
  };

  const acknowledgeAlarm = (alarmId) => {
    if (!canEditPage("alarms")) return;
    const pauseEmail = window.confirm("Pause email notifications for this alarm after acknowledgement?");
    let pausedKey = "";
    setAlarms((prev) =>
      prev.map((a) => {
        if (a.id !== alarmId) return a;
        if (pauseEmail) pausedKey = makeTagKey(a.gateway_id, a.tag);
        return { ...a, acknowledged: true, notification_paused: Boolean(pauseEmail), paused_by_tag: Boolean(pauseEmail) };
      })
    );
    if (pauseEmail && pausedKey) {
      setTagAlarmPrefs((prev) => ({ ...(prev || {}), [pausedKey]: false }));
    }
  };

  const toggleAlarmSelected = (alarmId, checked) => {
    setSelectedAlarmIds((prev) => {
      if (checked) return Array.from(new Set([...prev, alarmId]));
      return prev.filter((id) => id !== alarmId);
    });
  };

  const toggleAllAlarmSelected = (checked) => {
    if (!checked) {
      setSelectedAlarmIds([]);
      return;
    }
    setSelectedAlarmIds(alarms.map((a) => a.id));
  };

  const acknowledgeSelectedAlarms = () => {
    if (!canEditPage("alarms") || !selectedAlarmIds.length) return;
    const selected = new Set(selectedAlarmIds);
    const pauseEmail = window.confirm("Pause email notifications for selected alarms after acknowledgement?");
    const affectedTagKeys = new Set();
    setAlarms((prev) =>
      prev.map((a) => {
        if (!selected.has(a.id)) return a;
        if (pauseEmail) affectedTagKeys.add(makeTagKey(a.gateway_id, a.tag));
        return { ...a, acknowledged: true, notification_paused: Boolean(pauseEmail), paused_by_tag: Boolean(pauseEmail) };
      })
    );
    if (pauseEmail) {
      setTagAlarmPrefs((prev) => {
        const next = { ...(prev || {}) };
        for (const k of affectedTagKeys) next[k] = false;
        return next;
      });
    }
  };

  const pauseSelectedAlarmNotifications = () => {
    if (!canEditPage("alarms") || !selectedAlarmIds.length) return;
    const ok = window.confirm("Pause alarm notifications for selected alarms and disable their tag alarm checkbox?");
    if (!ok) return;
    const selected = new Set(selectedAlarmIds);
    const affectedTagKeys = new Set();
    setAlarms((prev) =>
      prev.map((a) => {
        if (!selected.has(a.id)) return a;
        affectedTagKeys.add(makeTagKey(a.gateway_id, a.tag));
        return { ...a, notification_paused: true, paused_by_tag: true };
      })
    );
    setTagAlarmPrefs((prev) => {
      const next = { ...(prev || {}) };
      for (const k of affectedTagKeys) next[k] = false;
      return next;
    });
  };

  const resumeSelectedAlarmNotifications = () => {
    if (!canEditPage("alarms") || !selectedAlarmIds.length) return;
    const ok = window.confirm("Resume alarm notifications for selected alarms and re-enable their tag alarm checkbox?");
    if (!ok) return;
    const selected = new Set(selectedAlarmIds);
    const affectedTagKeys = new Set();
    setAlarms((prev) =>
      prev.map((a) => {
        if (!selected.has(a.id)) return a;
        affectedTagKeys.add(makeTagKey(a.gateway_id, a.tag));
        return { ...a, notification_paused: false, paused_by_tag: false };
      })
    );
    setTagAlarmPrefs((prev) => {
      if (!prev || typeof prev !== "object") return prev;
      const next = { ...prev };
      for (const k of affectedTagKeys) {
        if (k in next) delete next[k];
      }
      return next;
    });
  };

  const clearSelectedAlarms = () => {
    if (!canEditPage("alarms") || !selectedAlarmIds.length) return;
    const selected = new Set(selectedAlarmIds);
    setAlarms((prev) => prev.filter((a) => !selected.has(a.id)));
    setSelectedAlarmIds([]);
  };

  const clearAllAlarms = () => {
    if (!canEditPage("alarms")) return;
    withConfirm("Clear All Alarms", "Remove all alarms from the list?", () => {
      setAlarms([]);
      setSelectedAlarmIds([]);
    });
  };

  useEffect(() => {
    const existing = new Set(alarms.map((a) => a.id));
    setSelectedAlarmIds((prev) => prev.filter((id) => existing.has(id)));
  }, [alarms]);

  const openAddTriggerRule = () => {
    if (!canEditPage("triggers_and_limits")) return;
    const defaultGatewayId = gatewayConfigs[0]?.id || "";
    const defaultTag = (triggerTagsByGateway[defaultGatewayId] || [])[0] || "";
    setEditingTriggerId(null);
    setTriggerForm({
      gateway_id: defaultGatewayId,
      tag_name: defaultTag,
      lower_enabled: false,
      lower_operator: "<",
      lower_value: "",
      upper_enabled: true,
      upper_operator: ">=",
      upper_value: "",
      enabled: true
    });
    setShowTriggerModal(true);
  };

  const openAddCollectionTrigger = () => {
    if (!canEditPage("triggers_and_limits")) return;
    const defaultGatewayId = gatewayConfigs[0]?.id || "";
    const defaultTag = (triggerTagsByGateway[defaultGatewayId] || [])[0] || "";
    setEditingCollectionTriggerId(null);
    setCollectionTriggerForm({
      gateway_id: defaultGatewayId,
      tag_name: defaultTag,
      operator: ">=",
      value: "",
      trigger_type: "continuous",
      enabled: true
    });
    setShowCollectionTriggerModal(true);
  };

  const openEditCollectionTrigger = (trigger) => {
    if (!canEditPage("triggers_and_limits")) return;
    if (!trigger) return;
    setEditingCollectionTriggerId(trigger.id);
    setCollectionTriggerForm({
      gateway_id: trigger.gateway_id || "",
      tag_name: trigger.tag_name || "",
      operator: trigger.operator || ">=",
      value: trigger.value === null || trigger.value === undefined ? "" : String(trigger.value),
      trigger_type: trigger.trigger_type === "one_time" ? "one_time" : "continuous",
      enabled: trigger.enabled !== false
    });
    setShowCollectionTriggerModal(true);
  };

  const saveCollectionTrigger = () => {
    if (!canEditPage("triggers_and_limits")) return;
    const gatewayId = String(collectionTriggerForm.gateway_id || "").trim();
    const tagName = String(collectionTriggerForm.tag_name || "").trim();
    const valueRaw = String(collectionTriggerForm.value ?? "").trim();
    const valueNum = Number(valueRaw);
    if (!gatewayId || !tagName) {
      setError("Trigger condition requires gateway and tag.");
      return;
    }
    if (!valueRaw || Number.isNaN(valueNum)) {
      setError("Trigger condition value must be numeric.");
      return;
    }
    const payload = {
      id: editingCollectionTriggerId || `ctrg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      gateway_id: gatewayId,
      tag_name: tagName,
      operator: collectionTriggerForm.operator || ">=",
      value: valueNum,
      trigger_type: collectionTriggerForm.trigger_type === "one_time" ? "one_time" : "continuous",
      enabled: Boolean(collectionTriggerForm.enabled),
      updated_at: tsNow()
    };
    setCollectionTriggers((prev) => {
      if (editingCollectionTriggerId) return prev.map((r) => (r.id === editingCollectionTriggerId ? payload : r));
      return [payload, ...prev];
    });
    setShowCollectionTriggerModal(false);
    setEditingCollectionTriggerId(null);
    setError("");
  };

  const removeCollectionTrigger = (triggerId) => {
    if (!canEditPage("triggers_and_limits")) return;
    withConfirm("Delete Trigger Condition", "Remove this trigger condition?", () => {
      setCollectionTriggers((prev) => prev.filter((r) => r.id !== triggerId));
      setError("");
    });
  };

  const openEditTriggerRule = (rule) => {
    if (!canEditPage("triggers_and_limits")) return;
    if (!rule) return;
    setEditingTriggerId(rule.id);
    setTriggerForm({
      gateway_id: rule.gateway_id || "",
      tag_name: rule.tag_name || "",
      lower_enabled: Boolean(rule.lower_enabled),
      lower_operator: rule.lower_operator || "<",
      lower_value: rule.lower_value === null || rule.lower_value === undefined ? "" : String(rule.lower_value),
      upper_enabled: Boolean(rule.upper_enabled),
      upper_operator: rule.upper_operator || ">=",
      upper_value: rule.upper_value === null || rule.upper_value === undefined ? "" : String(rule.upper_value),
      enabled: rule.enabled !== false
    });
    setShowTriggerModal(true);
  };

  const saveTriggerRule = () => {
    if (!canEditPage("triggers_and_limits")) return;
    const gatewayId = String(triggerForm.gateway_id || "").trim();
    const tagName = String(triggerForm.tag_name || "").trim();
    if (!gatewayId || !tagName) {
      setError("Trigger rule requires gateway and tag.");
      return;
    }
    if (!triggerForm.lower_enabled && !triggerForm.upper_enabled) {
      setError("Configure at least one limit (lower or upper).");
      return;
    }
    const lowerRaw = String(triggerForm.lower_value ?? "").trim();
    const upperRaw = String(triggerForm.upper_value ?? "").trim();
    const lowerValue = triggerForm.lower_enabled ? Number(lowerRaw) : null;
    const upperValue = triggerForm.upper_enabled ? Number(upperRaw) : null;
    if (triggerForm.lower_enabled && (!lowerRaw || Number.isNaN(lowerValue))) {
      setError("Lower limit must be numeric.");
      return;
    }
    if (triggerForm.upper_enabled && (!upperRaw || Number.isNaN(upperValue))) {
      setError("Upper limit must be numeric.");
      return;
    }
    const payload = {
      id: editingTriggerId || `trg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      gateway_id: gatewayId,
      tag_name: tagName,
      lower_enabled: Boolean(triggerForm.lower_enabled),
      lower_operator: triggerForm.lower_operator || "<",
      lower_value: lowerValue,
      upper_enabled: Boolean(triggerForm.upper_enabled),
      upper_operator: triggerForm.upper_operator || ">=",
      upper_value: upperValue,
      enabled: Boolean(triggerForm.enabled),
      updated_at: tsNow()
    };
    setTriggerRules((prev) => {
      if (editingTriggerId) return prev.map((r) => (r.id === editingTriggerId ? payload : r));
      return [payload, ...prev];
    });
    setShowTriggerModal(false);
    setEditingTriggerId(null);
    setError("");
  };

  const removeTriggerRule = (ruleId) => {
    if (!canEditPage("triggers_and_limits")) return;
    withConfirm("Delete Trigger Rule", "Remove this trigger/limit rule?", () => {
      setTriggerRules((prev) => prev.filter((r) => r.id !== ruleId));
      setError("");
    });
  };

  const withConfirm = (title, message, onConfirm) => {
    setConfirmDialog({
      open: true,
      title,
      message,
      onConfirm
    });
  };

  const closeConfirmDialog = () => {
    setConfirmDialog({ open: false, title: "", message: "", onConfirm: null });
  };

  const confirmAndRun = () => {
    if (confirmDialog.onConfirm) confirmDialog.onConfirm();
    closeConfirmDialog();
  };

  const openAddGatewayConfig = () => {
    if (!canEditPage("gateway_configuration")) return;
    setEditingGatewayId(null);
    setGatewayDiscoverResult("");
    setGatewayDiscoveredTags([]);
    setGatewayOpcBrowseNodes([]);
    setGatewaySelectedTags([]);
    setGatewayOpcValidationResult("");
    setGatewayOpcValidationRows([]);
    setGatewayOpcValidatedFor("");
    setGatewayForm({
      name: "",
      device_id: devices[0]?.id || "",
      gateway_type: devices[0]?.gateway_type || config?.gateway_type || "allen_bradley",
      plc_ip: devices[0]?.plc_ip || config?.plc_ip || "",
      opc_url: devices[0]?.opc_url || "",
      database_id: dbConnections[0]?.id || "",
      interval_ms: Number(config?.interval_ms || 1000),
      tags_text: Array.isArray(config?.tags) ? config.tags.join(";") : ""
    });
    setShowGatewayModal(true);
  };

  const openEditGatewayConfig = (gateway) => {
    if (!canEditPage("gateway_configuration")) return;
    setEditingGatewayId(gateway.id);
    setGatewayDiscoverResult("");
    setGatewayDiscoveredTags([]);
    setGatewayOpcBrowseNodes([]);
    setGatewaySelectedTags([]);
    setGatewayOpcValidationResult("");
    setGatewayOpcValidationRows([]);
    setGatewayOpcValidatedFor("");
    setGatewayForm({
      name: gateway.name || "",
      device_id: gateway.device_id || "",
      gateway_type: gateway.gateway_type || "allen_bradley",
      plc_ip: gateway.plc_ip || "",
      opc_url: gateway.opc_url || "",
      database_id: gateway.database_id || "",
      interval_ms: Number(gateway.interval_ms || 1000),
      tags_text: (gateway.tags || []).join(";")
    });
    setShowGatewayModal(true);
  };

  const onGatewayDeviceChange = (deviceId) => {
    const device = devices.find((d) => d.id === deviceId);
    if (!device) {
      setGatewayForm((prev) => ({ ...prev, device_id: deviceId }));
      return;
    }
    setGatewayForm((prev) => ({
      ...prev,
      device_id: deviceId,
      gateway_type: device.gateway_type,
      plc_ip: device.plc_ip || "",
      opc_url:
        device.gateway_type === "siemens_opcua"
          ? device.opc_url || buildOpcUrlFromIp(device.plc_ip)
          : "",
      tags_text:
        device.gateway_type === "siemens_opcua"
          ? (
              (Array.isArray(device.opc_node_ids) && device.opc_node_ids.length
                ? device.opc_node_ids
                : parseOpcNodeIds(device.opc_node_ids_text || device.opc_node_id || DEFAULT_OPC_NODE_ID))
            ).join(";")
          : prev.tags_text
    }));
    setGatewayOpcValidationResult("");
    setGatewayOpcValidationRows([]);
    setGatewayOpcValidatedFor("");
  };

  const onGatewayConfigFileLoad = async (file) => {
    if (!file) return;
    try {
      const content = await file.text();
      const parsed = parseGatewayTxt(content);
      setGatewayForm((prev) => ({ ...prev, ...parsed }));
      setGatewayOpcValidationResult("");
      setGatewayOpcValidationRows([]);
      setGatewayOpcValidatedFor("");
    } catch (err) {
      setError(`Gateway file load failed: ${String(err)}`);
    }
  };

  const saveGatewayConfig = () => {
    if (!canEditPage("gateway_configuration")) return;
    const name = gatewayForm.name.trim();
    const plcIp = gatewayForm.plc_ip.trim();
    const tags = parseGatewayTagsByType(gatewayForm.gateway_type, gatewayForm.tags_text);
    if (!name || !plcIp) {
      setError("Gateway name and PLC IP are required.");
      return;
    }
    if (gatewayForm.gateway_type === "siemens_opcua") {
      const nodeIds = parseOpcNodeIds(gatewayForm.tags_text);
      if (!nodeIds.length) {
        setError("At least one OPC NodeId is required for Siemens OPC-UA.");
        return;
      }
      const validationKey = `opcua|${plcIp}|${gatewayForm.opc_url.trim()}|${nodeIds.join(";")}`;
      if (gatewayOpcValidatedFor !== validationKey) {
        setError("Run OPC Node Validation for current URL and NodeIds before saving.");
        return;
      }
      if (!gatewayOpcValidationRows.length || gatewayOpcValidationRows.some((r) => !r.ok)) {
        setError("One or more OPC NodeIds failed validation. Fix failed nodes before saving.");
        return;
      }
    }
    const next = {
      id: editingGatewayId || `gw-${Date.now()}`,
      name,
      device_id: gatewayForm.device_id || "",
      gateway_type: gatewayForm.gateway_type,
      plc_ip: plcIp,
      opc_url: gatewayForm.gateway_type === "siemens_opcua" ? gatewayForm.opc_url.trim() : "",
      database_id: gatewayForm.database_id || "",
      interval_ms: Math.max(100, Number(gatewayForm.interval_ms || 1000)),
      tags
    };
    setGatewayConfigs((prev) => {
      if (editingGatewayId) return prev.map((g) => (g.id === editingGatewayId ? next : g));
      return [...prev, next];
    });
    setSelectedGatewayId(next.id);
    setShowGatewayModal(false);
    setError("");
  };

  const runGatewayTagDiscovery = async () => {
    if (!canEditPage("gateway_configuration")) return;
    if (!gatewayForm.plc_ip.trim()) {
      setGatewayDiscoverResult("PLC IP is required to discover tags.");
      return;
    }
    setGatewayDiscoverBusy(true);
    setGatewayDiscoverResult("");
    try {
      if (gatewayForm.gateway_type === "siemens_opcua") {
        const res = await browseOpcUaNodes({
          plc_ip: gatewayForm.plc_ip.trim(),
          opc_url: gatewayForm.opc_url.trim(),
          timeout_ms: 9000,
          max_nodes: 4000,
          max_depth: 10,
          variables_only: false
        });
        const nodes = Array.isArray(res.nodes) ? res.nodes : [];
        setGatewayOpcBrowseNodes(nodes);
        const variableNodeIds = nodes
          .filter((n) => Boolean(n?.is_variable))
          .map((n) => String(n.node_id || "").trim())
          .filter(Boolean);
        setGatewayDiscoveredTags(variableNodeIds);
        setGatewaySelectedTags([]);
        setGatewayDiscoverResult(
          res.message || `Browsed ${nodes.length} OPC-UA nodes (variables: ${variableNodeIds.length})`
        );
        return;
      }

      const res = await discoverPlcTags({
        gateway_type: gatewayForm.gateway_type,
        plc_ip: gatewayForm.plc_ip.trim(),
        opc_url: gatewayForm.opc_url.trim(),
        timeout_ms: 6000,
        max_tags: 5000
      });
      const tags = Array.isArray(res.tags) ? res.tags : [];
      setGatewayOpcBrowseNodes([]);
      setGatewayDiscoveredTags(tags);
      setGatewaySelectedTags([]);
      setGatewayDiscoverResult(res.message || `Discovered ${res.tags?.length || 0} tags`);
    } catch (err) {
      setGatewayDiscoverResult(String(err));
      setGatewayDiscoveredTags([]);
      setGatewayOpcBrowseNodes([]);
      setGatewaySelectedTags([]);
    } finally {
      setGatewayDiscoverBusy(false);
    }
  };

  const toggleGatewayDiscoveredTag = (tag) => {
    setGatewaySelectedTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
    );
  };

  const selectAllDiscoveredTags = () => {
    setGatewaySelectedTags(gatewayDiscoveredTags.slice());
  };

  const clearSelectedDiscoveredTags = () => {
    setGatewaySelectedTags([]);
  };

  const applySelectedDiscoveredTags = () => {
    if (!gatewaySelectedTags.length) return;
    const merged = new Set([
      ...parseGatewayTagsByType(gatewayForm.gateway_type, gatewayForm.tags_text),
      ...gatewaySelectedTags
    ]);
    setGatewayForm((prev) => ({ ...prev, tags_text: Array.from(merged).join(";") }));
    setGatewayOpcValidationResult("");
    setGatewayOpcValidationRows([]);
    setGatewayOpcValidatedFor("");
  };

  const runGatewayOpcNodeValidation = async () => {
    if (!canEditPage("gateway_configuration")) return;
    if (gatewayForm.gateway_type !== "siemens_opcua") return;
    const plcIp = gatewayForm.plc_ip.trim();
    const opcUrl = gatewayForm.opc_url.trim();
    const nodeIds = parseOpcNodeIds(gatewayForm.tags_text);
    if (!plcIp) {
      setGatewayOpcValidationResult("PLC IP is required.");
      setGatewayOpcValidationRows([]);
      return;
    }
    if (!nodeIds.length) {
      setGatewayOpcValidationResult("Add one or more OPC NodeIds in Tags before validation.");
      setGatewayOpcValidationRows([]);
      return;
    }
    setGatewayOpcValidationBusy(true);
    setGatewayOpcValidationResult("");
    try {
      const res = await testPlcConnection({
        gateway_type: "siemens_opcua",
        plc_ip: plcIp,
        opc_url: opcUrl,
        opc_node_ids: nodeIds,
        timeout_ms: 9000
      });
      const opcNodes = Array.isArray(res?.opc_nodes) ? res.opc_nodes : Array.isArray(res?.opcNodes) ? res.opcNodes : [];
      const mapped = nodeIds.map((id) => {
        const found = opcNodes.find((n) => String(n.node_id || n.nodeId || "") === id);
        return {
          node_id: id,
          ok: Boolean(found?.ok),
          message: String(found?.message || (found ? "Unknown result" : "Node was not returned by backend"))
        };
      });
      const okCount = mapped.filter((r) => r.ok).length;
      const failCount = mapped.length - okCount;
      const key = `opcua|${plcIp}|${opcUrl}|${nodeIds.join(";")}`;
      setGatewayOpcValidatedFor(key);
      setGatewayOpcValidationRows(mapped);
      setGatewayOpcValidationResult(
        failCount === 0
          ? `Validation OK: ${okCount}/${mapped.length} nodes read successfully.`
          : `Validation failed: ${okCount}/${mapped.length} nodes OK, ${failCount} failed.`
      );
    } catch (err) {
      setGatewayOpcValidationRows([]);
      setGatewayOpcValidationResult(`Validation error: ${String(err)}`);
    } finally {
      setGatewayOpcValidationBusy(false);
    }
  };

  const removeGatewayConfigProfile = (gatewayId) => {
    if (!canDeleteRecords) {
      setError("Only admin can delete gateway configuration.");
      return;
    }
    withConfirm(
      "Delete Gateway Configuration",
      "Are you sure you want to delete this gateway configuration?",
      () => setGatewayConfigs((prev) => prev.filter((g) => g.id !== gatewayId))
    );
  };

  const exportGatewayConfig = (gateway) => {
    try {
      const txt = buildGatewayTxt(gateway);
      const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${gateway.name.replace(/[^A-Za-z0-9_-]+/g, "_") || "gateway"}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(`Gateway export failed: ${String(err)}`);
    }
  };

  const openAddDevice = () => {
    if (!canEditPage("devices")) return;
    setEditingDeviceId(null);
    setDeviceForm({
      name: "",
      gateway_type: config?.gateway_type || "allen_bradley",
      plc_ip: "",
      opc_url: "",
      opc_node_id: DEFAULT_OPC_NODE_ID,
      opc_node_ids_text: DEFAULT_OPC_NODE_ID,
      notes: ""
    });
    setDeviceTestResult(null);
    setShowDeviceModal(true);
  };

  const openEditDevice = (device) => {
    if (!canEditPage("devices")) return;
    setEditingDeviceId(device.id);
    setDeviceForm({
      name: device.name || "",
      gateway_type: device.gateway_type || "allen_bradley",
      plc_ip: device.plc_ip || "",
      opc_url: device.opc_url || "",
      opc_node_id: device.opc_node_id || DEFAULT_OPC_NODE_ID,
      opc_node_ids_text:
        Array.isArray(device.opc_node_ids) && device.opc_node_ids.length
          ? device.opc_node_ids.join(";")
          : device.opc_node_ids_text || device.opc_node_id || DEFAULT_OPC_NODE_ID,
      notes: device.notes || ""
    });
    setDeviceTestResult(
      device.last_test
        ? {
            ok: Boolean(device.connection_ok),
            message: device.last_test,
            tested_for: `${device.gateway_type}|${device.plc_ip}|${device.opc_url || ""}|${
              (Array.isArray(device.opc_node_ids) && device.opc_node_ids.length
                ? device.opc_node_ids
                : parseOpcNodeIds(device.opc_node_ids_text || device.opc_node_id || "")
              ).join(";")
            }`
          }
        : null
    );
    setShowDeviceModal(true);
  };

  const removeDevice = (deviceId) => {
    if (!canDeleteRecords) {
      setError("Only admin can delete devices.");
      return;
    }
    withConfirm(
      "Delete Device",
      "Are you sure you want to delete this device?",
      () => setDevices((prev) => prev.filter((d) => d.id !== deviceId))
    );
  };

  const runDeviceConnectionTest = async () => {
    if (!canEditPage("devices")) return;
    setError("");
    setDeviceTestResult(null);
    if (!deviceForm.plc_ip.trim()) {
      setDeviceTestResult({ ok: false, message: "PLC IP is required before test." });
      return;
    }
    setDeviceTestBusy(true);
    try {
      const res = await testPlcConnection({
        gateway_type: deviceForm.gateway_type,
        plc_ip: deviceForm.plc_ip.trim(),
        opc_url: deviceForm.opc_url.trim(),
        opc_node_id: deviceForm.opc_node_id.trim(),
        opc_node_ids: parseOpcNodeIds(deviceForm.opc_node_ids_text),
        timeout_ms: deviceForm.gateway_type === "siemens_opcua" ? 7000 : 2000
      });
      const normalized = {
        ...res,
        ping_ok: Object.prototype.hasOwnProperty.call(res || {}, "ping_ok") ? res.ping_ok : res?.pingOk,
        port_ok: Object.prototype.hasOwnProperty.call(res || {}, "port_ok") ? res.port_ok : res?.portOk,
        port: Object.prototype.hasOwnProperty.call(res || {}, "port") ? res.port : res?.portNumber,
        opc_nodes: Array.isArray(res?.opc_nodes) ? res.opc_nodes : Array.isArray(res?.opcNodes) ? res.opcNodes : []
      };
      const nodeListKey = parseOpcNodeIds(deviceForm.opc_node_ids_text).join(";");
      setDeviceTestResult({
        ...normalized,
        tested_for: `${deviceForm.gateway_type}|${deviceForm.plc_ip.trim()}|${deviceForm.opc_url.trim()}|${nodeListKey}`
      });
    } catch (err) {
      setDeviceTestResult({ ok: false, message: String(err?.message || err || "Connection test failed") });
    } finally {
      setDeviceTestBusy(false);
    }
  };

  const saveDevice = () => {
    if (!canEditPage("devices")) return;
    const name = deviceForm.name.trim();
    const ip = deviceForm.plc_ip.trim();
    if (!name || !ip) {
      setDeviceTestResult({ ok: false, message: "Name and PLC IP are required." });
      return;
    }
    const testedKey = `${deviceForm.gateway_type}|${ip}|${deviceForm.opc_url.trim()}|${parseOpcNodeIds(deviceForm.opc_node_ids_text).join(";")}`;
    if (!deviceTestResult?.ok || deviceTestResult?.tested_for !== testedKey) {
      setDeviceTestResult({
        ok: false,
        message: "Run a successful connection test for current type and IP before saving."
      });
      return;
    }
    const opcNodes = parseOpcNodeIds(deviceForm.opc_node_ids_text);
    const next = {
      id: editingDeviceId || `dev-${Date.now()}`,
      name,
      gateway_type: deviceForm.gateway_type,
      plc_ip: ip,
      opc_url: deviceForm.opc_url.trim(),
      opc_node_id: (opcNodes[0] || deviceForm.opc_node_id || DEFAULT_OPC_NODE_ID).trim(),
      opc_node_ids: opcNodes,
      opc_node_ids_text: deviceForm.opc_node_ids_text.trim(),
      notes: deviceForm.notes.trim(),
      connection_ok: true,
      ping_ok: Boolean(deviceTestResult.ping_ok),
      port_ok: Boolean(deviceTestResult.port_ok),
      protocol_ok:
        deviceForm.gateway_type === "siemens_opcua"
          ? Boolean(
              Object.prototype.hasOwnProperty.call(deviceTestResult || {}, "opc_session_ok")
                ? deviceTestResult.opc_session_ok
                : deviceTestResult.port_ok
            )
          : Boolean(deviceTestResult.port_ok),
      last_test: deviceTestResult.message
    };
    setDevices((prev) => {
      if (editingDeviceId) return prev.map((d) => (d.id === editingDeviceId ? next : d));
      return [...prev, next];
    });
    setShowDeviceModal(false);
  };

  const openAddDbConnection = (scope = "gateway", overrides = null) => {
    if (!canEditPage("database")) return;
    setEditingDbId(null);
    setDbModalPresetScope(scope);
    const useGateway = scope === "gateway";
    const useApp = scope === "app";
    const useBackup = scope === "backup";
    const baseForm = {
      name: "",
      engine: scope === "app" ? "sqlite" : "postgresql",
      host: "127.0.0.1",
      port: scope === "app" || scope === "gateway" || scope === "backup" ? "5432" : "3306",
      database: "",
      username: "",
      password: "",
      sqlite_path: "./data/trustnode_edge.db",
      file_path: "./data/trustnode_log.csv",
      legacy_url: "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php",
      legacy_api_token: "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3",
      source: "edge-01",
      site: "Limerick",
      area: "LineA",
      equipment: "MACHINE-01",
      schema: "public",
      table: "plc_readings",
      tls: true,
      enabled: true,
      use_gateway: useGateway,
      use_app: useApp,
      use_backup: useBackup,
      cloud_sync_enabled: scope === "app"
    };
    setDbForm(overrides ? { ...baseForm, ...overrides } : baseForm);
    setDbTestResult(null);
    setShowDbModal(true);
  };

  const openEditDbConnection = (conn) => {
    if (!canEditPage("database")) return;
    setEditingDbId(conn.id);
    setDbModalPresetScope("gateway");
    setDbForm({
      name: conn.name || "",
      engine: conn.engine || "mysql",
      host: conn.host || "",
      port: String(conn.port || ""),
      database: conn.database || "",
      username: conn.username || "",
      password: conn.password || "",
      sqlite_path: conn.sqlite_path || "./data/trustnode_edge.db",
      file_path: conn.file_path || "./data/trustnode_log.csv",
      legacy_url: conn.legacy_url || "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php",
      legacy_api_token: conn.legacy_api_token || "",
      source: conn.source || "edge-01",
      site: conn.site || "Limerick",
      area: conn.area || "LineA",
      equipment: conn.equipment || "MACHINE-01",
      schema: conn.schema || "public",
      table: conn.table || "plc_readings",
      tls: Boolean(conn.tls),
      enabled: conn.enabled !== false,
      use_gateway: conn.use_gateway !== false,
      use_app: Boolean(conn.use_app),
      use_backup: Boolean(conn.use_backup),
      cloud_sync_enabled: Boolean(conn.cloud_sync_enabled)
    });
    setDbTestResult(
      conn.last_test
        ? {
            ok: Boolean(conn.connection_ok),
            message: conn.last_test,
            tested_for:
              conn.engine === "legacy_http"
                ? `legacy:${conn.legacy_url || ""}`
                : conn.engine === "sqlite"
                  ? `sqlite:${conn.sqlite_path || "./data/trustnode_edge.db"}`
                  : `${conn.host}:${conn.port}`
          }
        : null
    );
    setShowDbModal(true);
  };

  const runDbConnectionTest = async () => {
    if (!canEditPage("database")) return;
    setDbTestResult(null);
    const isLegacy = dbForm.engine === "legacy_http";
    const isSqlite = dbForm.engine === "sqlite";
    const isFileSink = dbForm.engine === "csv_file" || dbForm.engine === "txt_file";
    const host = dbForm.host.trim();
    const port = Number(dbForm.port);
    const normalizedUsername = normalizeSupabaseDirectUsername(
      dbForm.engine,
      host,
      port,
      dbForm.username.trim()
    );
    if (normalizedUsername !== dbForm.username.trim()) {
      setDbForm((prev) => ({ ...prev, username: normalizedUsername }));
    }
    const sqlitePath = dbForm.sqlite_path.trim();
    const filePath = dbForm.file_path.trim();
    if (!isLegacy && !isSqlite && !isFileSink && (!host || !port)) {
      setDbTestResult({ ok: false, message: "Host and Port are required for test." });
      return;
    }
    if (isSqlite && !sqlitePath) {
      setDbTestResult({ ok: false, message: "SQLite file path is required for test." });
      return;
    }
    if (isLegacy && (!dbForm.legacy_url.trim() || !dbForm.legacy_api_token.trim())) {
      setDbTestResult({ ok: false, message: "Legacy URL and API token are required for test." });
      return;
    }
    if (isFileSink && !filePath) {
      setDbTestResult({ ok: false, message: "File path is required for test." });
      return;
    }
    setDbTestBusy(true);
    try {
      const res = await testDatabaseConnection({
        engine: dbForm.engine,
        host,
        port,
        database: dbForm.database.trim(),
        username: normalizedUsername,
        password: dbForm.password,
        sqlite_path: sqlitePath,
        file_path: filePath,
        legacy_url: dbForm.legacy_url.trim(),
        legacy_api_token: dbForm.legacy_api_token.trim(),
        tls: Boolean(dbForm.tls),
        timeout_ms: dbForm.engine === "postgresql" ? 10000 : 2500
      });
      const testedFor = isLegacy
        ? `legacy:${dbForm.legacy_url.trim()}`
        : isSqlite
          ? `sqlite:${sqlitePath}`
          : isFileSink
            ? `file:${filePath}`
          : `${host}:${port}`;
      setDbTestResult({ ...res, tested_for: testedFor });
    } catch (err) {
      setDbTestResult({ ok: false, message: String(err) });
    } finally {
      setDbTestBusy(false);
    }
  };

  const saveDbConnection = () => {
    if (!canEditPage("database")) return;
    const name = dbForm.name.trim();
    const isLegacy = dbForm.engine === "legacy_http";
    const isSqlite = dbForm.engine === "sqlite";
    const isFileSink = dbForm.engine === "csv_file" || dbForm.engine === "txt_file";
    const host = dbForm.host.trim();
    const port = Number(dbForm.port);
    const normalizedUsername = normalizeSupabaseDirectUsername(
      dbForm.engine,
      host,
      port,
      dbForm.username.trim()
    );
    if (normalizedUsername !== dbForm.username.trim()) {
      setDbForm((prev) => ({ ...prev, username: normalizedUsername }));
    }
    const sqlitePath = dbForm.sqlite_path.trim();
    const filePath = dbForm.file_path.trim();
    if (!name || (!isLegacy && !isSqlite && !isFileSink && (!host || !port))) {
      setDbTestResult({ ok: false, message: "Name and valid connection fields are required." });
      return;
    }
    if (isSqlite && !sqlitePath) {
      setDbTestResult({ ok: false, message: "SQLite file path is required." });
      return;
    }
    if (isLegacy && (!dbForm.legacy_url.trim() || !dbForm.legacy_api_token.trim())) {
      setDbTestResult({ ok: false, message: "Legacy URL and API token are required." });
      return;
    }
    if (isFileSink && !filePath) {
      setDbTestResult({ ok: false, message: "File path is required." });
      return;
    }
    if (!dbForm.use_gateway && !dbForm.use_app && !dbForm.use_backup) {
      setDbTestResult({ ok: false, message: "Select at least one role: Gateway, App, or Backup." });
      return;
    }
    const testedFor = isLegacy
      ? `legacy:${dbForm.legacy_url.trim()}`
      : isSqlite
        ? `sqlite:${sqlitePath}`
        : isFileSink
          ? `file:${filePath}`
        : `${host}:${port}`;
    if (!dbTestResult?.ok || dbTestResult.tested_for !== testedFor) {
      setDbTestResult({ ok: false, message: "Run a successful test for current connection before saving." });
      return;
    }
    const finalizeSave = (provisionMsg = "") => {
      const next = {
        id: editingDbId || `db-${Date.now()}`,
        name,
        engine: dbForm.engine,
        location: dbLocationFromEngine(dbForm.engine),
        enabled: dbForm.enabled !== false,
        use_gateway: Boolean(dbForm.use_gateway),
        use_app: Boolean(dbForm.use_app),
        use_backup: Boolean(dbForm.use_backup),
        cloud_sync_enabled: Boolean(dbForm.cloud_sync_enabled),
        host: isLegacy || isSqlite ? "" : host,
        port: isLegacy || isSqlite ? 0 : port,
        database: isLegacy ? "" : dbForm.database.trim(),
        username: isLegacy || isSqlite ? "" : normalizedUsername,
        password: isLegacy || isSqlite ? "" : dbForm.password,
        sqlite_path: isSqlite ? sqlitePath : "",
        file_path: isFileSink ? filePath : "",
        legacy_url: dbForm.legacy_url.trim(),
        legacy_api_token: dbForm.legacy_api_token.trim(),
        source: dbForm.source.trim(),
        site: dbForm.site.trim(),
        area: dbForm.area.trim(),
        equipment: dbForm.equipment.trim(),
        schema: dbForm.schema.trim() || "public",
        table: dbForm.table.trim() || "plc_readings",
        tls: Boolean(dbForm.tls),
        connection_ok: true,
        last_test: provisionMsg ? `${dbTestResult.message} | ${provisionMsg}` : dbTestResult.message,
        last_check_utc: tsNow()
      };
      setDbConnections((prev) => {
        if (editingDbId) return prev.map((c) => (c.id === editingDbId ? next : c));
        return [...prev, next];
      });
      setShowDbModal(false);
    };

    const needsProvision =
      !isLegacy &&
      (dbForm.engine === "postgresql" ||
        dbForm.engine === "sqlite" ||
        dbForm.engine === "csv_file" ||
        dbForm.engine === "txt_file");
    if (!needsProvision) {
      finalizeSave();
      return;
    }

    (async () => {
      setDbProvisionBusy(true);
      try {
        const provision = await provisionDatabaseObjects({
          engine: dbForm.engine,
          host,
          port,
          database: dbForm.database.trim(),
          username: normalizedUsername,
          password: dbForm.password,
          sqlite_path: sqlitePath,
          file_path: filePath,
          schema: dbForm.schema.trim() || "public",
          table: dbForm.table.trim() || "plc_readings",
          tls: Boolean(dbForm.tls)
        });
        if (!provision.ok) {
          setDbTestResult({ ok: false, message: provision.message });
          return;
        }
        finalizeSave(provision.message);
      } catch (err) {
        setDbTestResult({ ok: false, message: String(err) });
      } finally {
        setDbProvisionBusy(false);
      }
    })();
  };

  const removeDbConnection = (dbId) => {
    if (!canDeleteRecords) {
      setError("Only admin can delete database connections.");
      return;
    }
    withConfirm(
      "Delete Database Connection",
      "Are you sure you want to delete this database connection?",
      () => setDbConnections((prev) => prev.filter((c) => c.id !== dbId))
    );
  };

  const saveUiSource = async () => {
    if (!canEditPage("website_and_env")) return;
    setUiSourceSavedMessage("");
    try {
      const saved = await setUiSourceConfig({
        mode: uiSourceMode,
        remote_url: uiSourceRemoteUrl.trim(),
        local_path: uiSourceLocalPath.trim()
      });
      setUiSourceMode(saved.mode);
      setUiSourceRemoteUrl(saved.remote_url || "");
      setUiSourceLocalPath(saved.local_path || "");
      setUiSourceSavedMessage("Frontend source saved. Restart desktop app to apply.");
      setUiSourceTestResult("");
    } catch (err) {
      setError(String(err));
    }
  };

  const runUiSourceTest = async () => {
    setUiSourceSavedMessage("");
    setUiSourceTestResult("");
    try {
      const res = await testUiSourceRemoteUrl(uiSourceRemoteUrl.trim());
      if (res.ok) {
        setUiSourceTestResult(`PASS: ${res.message}`);
      } else {
        setUiSourceTestResult(`FAIL: ${res.message}`);
      }
    } catch (err) {
      setUiSourceTestResult(`FAIL: ${String(err)}`);
    }
  };

  const parseWebsiteEnvText = () => {
    const rows = [];
    for (const line of String(websiteEnvText || "").split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const idx = trimmed.indexOf("=");
      if (idx <= 0) continue;
      const key = trimmed.slice(0, idx).trim();
      const value = trimmed.slice(idx + 1).trim();
      if (!key) continue;
      rows.push({ key, value });
    }
    return rows;
  };

  const applyWebsiteEnvToUi = () => {
    if (!canEditPage("website_and_env")) return;
    const envRows = parseWebsiteEnvText();
    const map = Object.fromEntries(envRows.map((r) => [r.key.toUpperCase(), r.value]));
    if (map.TRUSTNODE_CLOUD_API_URL) {
      setEndpointMode("cloud");
      setCloudUrl(map.TRUSTNODE_CLOUD_API_URL);
    }
    if (map.TRUSTNODE_UI_SOURCE_MODE) {
      const mode = String(map.TRUSTNODE_UI_SOURCE_MODE).toLowerCase();
      if (["local", "remote", "external"].includes(mode)) setUiSourceMode(mode);
    }
    if (map.TRUSTNODE_UI_SOURCE_REMOTE_URL) setUiSourceRemoteUrl(map.TRUSTNODE_UI_SOURCE_REMOTE_URL);
    if (map.TRUSTNODE_UI_SOURCE_LOCAL_PATH) setUiSourceLocalPath(map.TRUSTNODE_UI_SOURCE_LOCAL_PATH);
    setWebsiteStatusResult(`Applied ${envRows.length} env rows to current UI settings.`);
  };

  const runWebsiteStatusCheck = async () => {
    setWebsiteStatusResult("");
    try {
      const health = await getHealth();
      const msg = [
        `Backend: ${health?.ok ? "ONLINE" : "CHECK"}`,
        `Mode: ${String(endpointMode || "local").toUpperCase()}`,
        `UI Source: ${(uiSourceMode || "local").toUpperCase()}`
      ];
      const inCloudWebMode = Boolean(isHostedWebClient && endpointMode === "cloud");
      if (!inCloudWebMode) {
        const cfg = await getUiSourceConfig();
        msg[2] = `UI Source: ${(cfg?.mode || uiSourceMode || "local").toUpperCase()}`;
        if ((cfg?.mode || uiSourceMode) === "remote" && (cfg?.remote_url || uiSourceRemoteUrl)) {
          const t = await testUiSourceRemoteUrl((cfg?.remote_url || uiSourceRemoteUrl).trim());
          msg.push(`Website URL: ${t?.ok ? "REACHABLE" : "FAILED"}`);
        }
      } else {
        msg.push("Cloud web mode: skipping local UI-source probes");
      }
      setWebsiteStatusResult(msg.join(" | "));
    } catch (err) {
      const raw = String(err || "");
      if (isHostedWebClient && endpointMode === "cloud" && raw.toLowerCase().includes("ui source")) {
        setWebsiteStatusResult("Backend: ONLINE | Mode: CLOUD | Cloud web mode active");
        return;
      }
      setWebsiteStatusResult(`Status check failed: ${raw}`);
    }
  };

  const runDatabaseOverviewRecovery = async () => {
    if (!canEditPage("database_overview")) return;
    const conns = buildDbRecoveryConnections();
    if (!conns.length) {
      setDatabaseOverviewResult("No database connections configured.");
      return;
    }
    try {
      const res = await repairDatabaseRecovery({
        connections: conns,
        activate_first_healthy: false
      });
      const summary = String(res?.summary || "completed");
      setDatabaseOverviewResult(`Recovery finished: ${summary}`);
    } catch (err) {
      setDatabaseOverviewResult(`Recovery failed: ${String(err)}`);
    }
  };

  const getProvisionProfileConnections = (profile = "all") => {
    const rows = buildDbRecoveryConnections().filter((c) => c.enabled !== false);
    if (profile === "backup") {
      return rows.filter((c) => c.use_backup);
    }
    if (profile === "cloud") {
      const selectedId = String(cloudProviderDbId || "").trim();
      return rows.filter((c) => {
        const isPrimaryCloud = selectedId && String(c.id) === selectedId;
        const isDolibarrMirror = dolibarrMirrorEnabled && c.engine === "legacy_http" && c.use_gateway;
        return Boolean(isPrimaryCloud || isDolibarrMirror);
      });
    }
    if (profile === "all") {
      const selectedId = String(cloudProviderDbId || "").trim();
      return rows.filter((c) => {
        const isPrimaryCloud = selectedId && String(c.id) === selectedId;
        const isDolibarrMirror = dolibarrMirrorEnabled && c.engine === "legacy_http" && c.use_gateway;
        return Boolean(c.use_backup || isPrimaryCloud || isDolibarrMirror);
      });
    }
    return rows;
  };

  const runProvisionProfile = async (profile = "all") => {
    if (!isAdminDatabaseUser) return;
    const conns = getProvisionProfileConnections(profile);
    if (!conns.length) {
      setDatabaseOverviewResult(`No connections matched '${profile}' profile.`);
      return;
    }
    try {
      const res = await repairDatabaseRecovery({
        connections: conns,
        activate_first_healthy: false
      });
      const summary = String(res?.summary || "completed");
      setDatabaseOverviewResult(`Provision profile '${profile}' finished: ${summary}`);
    } catch (err) {
      setDatabaseOverviewResult(`Provision profile '${profile}' failed: ${String(err)}`);
    }
  };

  const runForceCloudSyncNow = async () => {
    if (!canEditPage("database")) return;
    setForceSyncBusy(true);
    setForceSyncResult("");
    try {
      const res = await forceAppStoreSyncNow({ actor: currentUser?.username || "manual" });
      const s = res?.summary || {};
      const parts = [
        `Config P:${Number(s.config_pending || 0)} F:${Number(s.config_failed || 0)} S:${Number(s.config_sent_total || 0)}`,
        `Data H:${Number(s.historian_synced_total || 0)}/${Number(s.historian_backlog || 0)} L:${Number(s.logs_synced_total || 0)}/${Number(s.logs_backlog || 0)}`,
      ];
      if (Array.isArray(res?.errors) && res.errors.length) {
        parts.push(`Errors: ${res.errors.join(" | ").slice(0, 240)}`);
      }
      setForceSyncResult(parts.join(" | "));
      if (res?.inspector) setDatabaseInspector(res.inspector);
      else await refreshDatabaseInspector();
    } catch (err) {
      setForceSyncResult(`Force sync failed: ${String(err)}`);
    } finally {
      setForceSyncBusy(false);
    }
  };

  const openCloudSyncModal = () => {
    const now = new Date();
    const from = new Date(now.getTime() - 60 * 60 * 1000);
    const toLocalInput = (d) => {
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    setCloudSyncForm((prev) => ({
      ...prev,
      from_utc: prev.from_utc || toLocalInput(from),
      to_utc: prev.to_utc || toLocalInput(now),
    }));
    setShowCloudSyncModal(true);
  };

  const runManualCloudPeriodSync = async () => {
    if (!canEditPage("database")) return;
    const fromUtc = String(cloudSyncForm.from_utc || "").trim();
    const toUtc = String(cloudSyncForm.to_utc || "").trim();
    if (!fromUtc || !toUtc) {
      setForceSyncResult("Select a valid sync period (From/To).");
      return;
    }
    setForceSyncBusy(true);
    setForceSyncResult("");
    try {
      const periodRes = await manualPeriodSyncAppStore({
        from_utc: new Date(fromUtc).toISOString(),
        to_utc: new Date(toUtc).toISOString(),
        max_rows: Number(cloudSyncForm.max_rows || 20000),
        include_logs: Boolean(cloudSyncForm.include_logs),
        actor: currentUser?.username || "manual",
      });
      const parts = [
        `Manual period sync: H:${Number(periodRes?.hist_rows || 0)} L:${Number(periodRes?.log_rows || 0)}`,
      ];
      if (periodRes?.message) parts.push(String(periodRes.message));

      if (cloudSyncForm.clear_queue_after) {
        const clearRes = await clearAppStoreSyncQueue({ actor: currentUser?.username || "manual", include_sent: false });
        parts.push(`Queue cleared: ${Number(clearRes?.deleted_rows || 0)}`);
      }
      if (cloudSyncForm.drop_backlog_after) {
        await dropAppStoreSyncBacklog({ actor: currentUser?.username || "manual" });
        parts.push("Backlog dropped to current head.");
      }
      setForceSyncResult(parts.join(" | "));
      setShowCloudSyncModal(false);
      await refreshDatabaseInspector();
    } catch (err) {
      setForceSyncResult(`Manual sync failed: ${String(err)}`);
    } finally {
      setForceSyncBusy(false);
    }
  };

  const runResetLocalAndSync = async () => {
    if (!canEditPage("database")) return;
    const confirmed = window.confirm(
      "This will delete local PLC readings/logs, reset app configuration, clear pending sync queue, and force a fresh sync. Continue?"
    );
    if (!confirmed) return;
    setForceSyncBusy(true);
    setForceSyncResult("");
    try {
      await stopAllGatewayInstances();
      const resetRes = await resetAppStoreFull({
        actor: currentUser?.username || "manual",
        clear_cloud_data: true,
      });
      setForceSyncResult(
        resetRes?.ok
          ? "Reset complete. Local + cloud tables cleaned and default configuration re-seeded."
          : `Reset completed with cloud warning: ${String(resetRes?.cloud?.message || "unknown cloud error")}`
      );
      await refreshDatabaseOverviewCards("Reset + Sync");
    } catch (err) {
      setForceSyncResult(`Reset + Sync failed: ${String(err)}`);
    } finally {
      setForceSyncBusy(false);
    }
  };

  const refreshDatabaseInspector = async () => {
    setDatabaseInspectorBusy(true);
    setDatabaseInspectorError("");
    try {
      const res = await getAppStoreInspector(20);
      if (res?.ok && res?.inspector) {
        setDatabaseInspector(res.inspector);
      } else {
        setDatabaseInspector(null);
        setDatabaseInspectorError("Inspector data not available on current backend.");
      }
    } catch (err) {
      setDatabaseInspectorError(String(err));
    } finally {
      setDatabaseInspectorBusy(false);
    }
  };

  const refreshRetentionData = async () => {
    const [policyRes, runsRes] = await Promise.all([
      getRetentionPolicy(),
      getRetentionRuns(20)
    ]);
    if (policyRes?.ok && policyRes?.policy) {
      const nextPolicy = { ...retentionPolicy, ...policyRes.policy };
      setRetentionPolicy(nextPolicy);
      setRetentionPresetKey(detectRetentionPreset(nextPolicy));
    }
    if (runsRes?.ok && Array.isArray(runsRes.runs)) {
      setRetentionRuns(runsRes.runs);
    }
  };

  const refreshBackups = async () => {
    const res = await getAppStoreBackups(200);
    if (res?.ok && Array.isArray(res.rows)) {
      setBackupRows(res.rows);
      if (!selectedBackupFilename && res.rows.length) {
        setSelectedBackupFilename(res.rows[0].filename);
      }
      if (selectedBackupFilename && !res.rows.some((r) => r.filename === selectedBackupFilename)) {
        setSelectedBackupFilename(res.rows[0]?.filename || "");
      }
    }
  };

  const applyRetentionPreset = (presetKey) => {
    const preset = RETENTION_PRESETS[presetKey] || RETENTION_PRESETS.week;
    setRetentionPresetKey(preset.key);
    setRetentionPolicy((prev) => ({
      ...prev,
      raw_keep_days: preset.raw_keep_days,
      minute_keep_days: preset.minute_keep_days,
      hour_keep_days: preset.hour_keep_days,
      day_keep_days: preset.day_keep_days,
    }));
  };

  const applyCloudRoutingPolicy = (selectedIdOverride = "") => {
    if (!isAdminDatabaseUser) return;
    const selectedId = String(selectedIdOverride || cloudProviderDbId || "").trim();
    if (!selectedId) {
      setDatabaseOverviewResult("Select a PostgreSQL cloud target first.");
      return;
    }
    setDbConnections((prev) =>
      prev.map((db) => {
        const engine = String(db.engine || "").toLowerCase();
        const isRemotePg = engine === "postgresql" && dbLocationFromEngine(db.engine) === "remote";
        const isSelected = String(db.id || "") === selectedId;
        if (isRemotePg) {
          if (isSelected) {
            return {
              ...db,
              enabled: true,
              use_gateway: true,
              use_app: true,
              cloud_sync_enabled: true,
              connection_ok: true,
              last_check_utc: tsNow(),
              last_test: "Trustnode cloud target applied",
            };
          }
          return {
            ...db,
            use_app: false,
            cloud_sync_enabled: false,
          };
        }
        if (engine === "legacy_http") {
          return {
            ...db,
            enabled: dolibarrMirrorEnabled ? true : db.enabled,
            use_gateway: dolibarrMirrorEnabled ? true : db.use_gateway,
          };
        }
        return db;
      })
    );
    setDatabaseOverviewResult(
      `Cloud routing applied. Primary target: ${selectedId}${dolibarrMirrorEnabled ? " | Dolibarr mirror: ON" : " | Dolibarr mirror: OFF"}`
    );
  };

  const applySupabaseBestProfile = () => {
    if (!isAdminDatabaseUser) return;
    const preferredId = String(cloudProviderDbId || selectedCloudDbId || "").trim();
    const supabaseCandidates = cloudProviderCandidates.filter((db) =>
      String(db.host || "").toLowerCase().includes("supabase")
    );
    const targetDb =
      cloudProviderCandidates.find((db) => String(db.id || "") === preferredId) ||
      supabaseCandidates[0] ||
      cloudProviderCandidates[0] ||
      null;
    if (!targetDb) {
      setCloudSupabaseApplyResult("No PostgreSQL cloud target found. Add Supabase first.");
      return;
    }
    const profile = resolveSupabaseConnectionProfile(
      cloudSupabaseMode,
      cloudSupabaseHasIpv4AddOn,
      targetDb
    );
    const normalizedUsername =
      profile.effectiveMode === "direct_ipv4"
        ? "postgres"
        : String(targetDb.username || KNOWN_SUPABASE_DEFAULTS.username);
    const targetId = String(targetDb.id || "").trim();
    setDbConnections((prev) =>
      prev.map((db) => {
        if (String(db.id || "") !== targetId) return db;
        return {
          ...db,
          engine: "postgresql",
          host: profile.host,
          port: Number(profile.port || 5432),
          username: normalizedUsername,
          tls: true,
          enabled: true,
          use_gateway: true,
          use_app: true,
          cloud_sync_enabled: true,
          connection_ok: true,
          last_check_utc: tsNow(),
          last_test: `Trustnode Cloud profile applied: ${profile.summary}`,
        };
      })
    );
    setCloudProviderDbId(targetId);
    setSelectedCloudDbId(targetId);
    applyCloudRoutingPolicy(targetId);
    setCloudSupabaseApplyResult(
      `Applied ${profile.summary} to ${targetDb.name} (${profile.host}:${profile.port}, user ${normalizedUsername}).`
    );
  };

  const refreshDatabaseOverviewCards = async (label = "Database") => {
    try {
      await Promise.all([refreshDatabaseInspector(), refreshRetentionData(), refreshBackups()]);
      setDatabaseOverviewResult(`${label} data loaded.`);
    } catch (err) {
      setDatabaseOverviewResult(`${label} load failed: ${String(err)}`);
    }
  };

  const applyTrustnodeCloudToggle = (enabled) => {
    setTrustnodeCloudEnabled(Boolean(enabled));
    if (!isAdminDatabaseUser) return;
    setDbConnections((prev) =>
      prev.map((db) => {
        const engine = String(db.engine || "").toLowerCase();
        const isCloudTarget = dbLocationFromEngine(db.engine) === "remote" && (engine === "postgresql" || engine === "legacy_http");
        if (!isCloudTarget) return db;
        if (!enabled) {
          return { ...db, cloud_sync_enabled: false };
        }
        return {
          ...db,
          enabled: true,
          cloud_sync_enabled: true,
        };
      })
    );
    setDatabaseOverviewResult(`Trustnode Cloud ${enabled ? "enabled" : "disabled"} for configured cloud targets.`);
  };

  const applyWebClientLinkSettings = () => {
    const normalizedUrl = String(tenantWebClientUrl || "").trim().replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(normalizedUrl)) {
      setDatabaseOverviewResult("Web client URL must start with http:// or https://");
      return;
    }
    setTenantWebClientUrl(normalizedUrl);
    if (endpointMode === "cloud") {
      setCloudUrl(normalizedUrl);
    }
    setDatabaseOverviewResult(
      `Web client linked for tenant '${currentTenantId}'${tenantCompanyName ? ` (${tenantCompanyName})` : ""}.`
    );
  };

  const openCloudDbPicker = () => {
    if (!isAdminDatabaseUser) return;
    setCloudDbPickerType("supabase");
    setShowCloudDbPickerModal(true);
  };

  const createCloudDbFromPicker = () => {
    if (!isAdminDatabaseUser) return;
    const mode = String(cloudDbPickerType || "supabase").toLowerCase();
    if (mode === "dolibarr") {
      const existingDolibarr = (dbConnections || []).find((db) => {
        const engine = String(db.engine || "").toLowerCase();
        return engine === "legacy_http" && dbLocationFromEngine(db.engine) === "remote";
      });
      openAddDbConnection("gateway", {
        name: String(existingDolibarr?.name || "Dolibarr"),
        engine: "legacy_http",
        legacy_url: String(existingDolibarr?.legacy_url || "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php"),
        legacy_api_token: String(existingDolibarr?.legacy_api_token || "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3"),
        source: String(existingDolibarr?.source || "edge-01"),
        site: String(existingDolibarr?.site || "Limerick"),
        area: String(existingDolibarr?.area || "LineA"),
        equipment: String(existingDolibarr?.equipment || "MACHINE-01"),
        enabled: true,
        use_gateway: true,
        use_app: true,
        use_backup: false,
        cloud_sync_enabled: true,
      });
    } else {
      const existingSupabase = (dbConnections || []).find((db) => {
        const engine = String(db.engine || "").toLowerCase();
        if (engine !== "postgresql" || dbLocationFromEngine(db.engine) !== "remote") return false;
        return String(db.host || "").toLowerCase().includes("supabase");
      });
      const source = existingSupabase || KNOWN_SUPABASE_DEFAULTS;
      const profile = resolveSupabaseConnectionProfile(
        cloudSupabaseMode,
        cloudSupabaseHasIpv4AddOn,
        source
      );
      const normalizedUsername =
        profile.effectiveMode === "direct_ipv4"
          ? "postgres"
          : String(source.username || KNOWN_SUPABASE_DEFAULTS.username);
      openAddDbConnection("gateway", {
        name: String(existingSupabase?.name || "Supabase"),
        engine: "postgresql",
        host: String(profile.host || source.host || KNOWN_SUPABASE_DEFAULTS.host),
        port: String(profile.port || source.port || KNOWN_SUPABASE_DEFAULTS.port),
        database: String(source.database || KNOWN_SUPABASE_DEFAULTS.database),
        username: normalizedUsername,
        password: String(source.password || KNOWN_SUPABASE_DEFAULTS.password),
        schema: String(source.schema || KNOWN_SUPABASE_DEFAULTS.schema),
        table: String(source.table || KNOWN_SUPABASE_DEFAULTS.table),
        source: String(source.source || KNOWN_SUPABASE_DEFAULTS.source),
        site: String(source.site || KNOWN_SUPABASE_DEFAULTS.site),
        area: String(source.area || KNOWN_SUPABASE_DEFAULTS.area),
        equipment: String(source.equipment || KNOWN_SUPABASE_DEFAULTS.equipment),
        tls: existingSupabase ? Boolean(existingSupabase.tls) : true,
        enabled: true,
        use_gateway: true,
        use_app: true,
        use_backup: false,
        cloud_sync_enabled: true,
      });
      setCloudSupabaseApplyResult(`Prepared Supabase form with ${profile.summary} (user ${normalizedUsername}).`);
    }
    setShowCloudDbPickerModal(false);
  };

  const openOtherDbPicker = () => {
    if (!isAdminDatabaseUser) return;
    setOtherDbPickerType("sqlite");
    setShowOtherDbPickerModal(true);
  };

  const createOtherDbFromPicker = () => {
    if (!isAdminDatabaseUser) return;
    const mode = String(otherDbPickerType || "sqlite").toLowerCase();
    const base = {
      enabled: true,
      use_gateway: true,
      use_app: false,
      use_backup: true,
      cloud_sync_enabled: false,
    };
    if (mode === "csv") {
      openAddDbConnection("backup", {
        ...base,
        name: "CSV Mirror",
        engine: "csv_file",
        file_path: "./data/trustnode_backup.csv",
      });
    } else if (mode === "txt") {
      openAddDbConnection("backup", {
        ...base,
        name: "TXT Mirror",
        engine: "txt_file",
        file_path: "./data/trustnode_backup.txt",
      });
    } else if (mode === "postgresql") {
      openAddDbConnection("backup", {
        ...base,
        name: "PostgreSQL Backup",
        engine: "postgresql",
        host: "127.0.0.1",
        port: "5432",
        database: "postgres",
        schema: "public",
        table: "plc_readings",
        tls: true,
      });
    } else if (mode === "mysql") {
      openAddDbConnection("backup", {
        ...base,
        name: "MySQL Backup",
        engine: "mysql",
        host: "127.0.0.1",
        port: "3306",
        database: "",
        schema: "public",
        table: "plc_readings",
        tls: true,
      });
    } else if (mode === "mssql") {
      openAddDbConnection("backup", {
        ...base,
        name: "MSSQL Backup",
        engine: "mssql",
        host: "127.0.0.1",
        port: "1433",
        database: "",
        schema: "dbo",
        table: "plc_readings",
        tls: true,
      });
    } else if (mode === "influxdb") {
      openAddDbConnection("backup", {
        ...base,
        name: "InfluxDB Backup",
        engine: "influxdb",
        host: "127.0.0.1",
        port: "8086",
        database: "trustnode",
        schema: "",
        table: "plc_readings",
        tls: false,
      });
    } else {
      openAddDbConnection("backup", {
        ...base,
        name: "Local SQLite Backup",
        engine: "sqlite",
        sqlite_path: "./data/trustnode_backup.db",
        table: "plc_readings",
      });
    }
    setShowOtherDbPickerModal(false);
  };

  const saveRetentionPolicy = async (scope = "global") => {
    if (!isAdminDatabaseUser) return;
    setRetentionResultScope(scope);
    setRetentionBusy(true);
    setRetentionResult("");
    try {
      const payload = {
        enabled: Boolean(retentionPolicy.enabled),
        schedule_minutes: Number(retentionPolicy.schedule_minutes || 60),
        raw_keep_days: Number(retentionPolicy.raw_keep_days || 7),
        minute_keep_days: Number(retentionPolicy.minute_keep_days || 30),
        hour_keep_days: Number(retentionPolicy.hour_keep_days || 180),
        day_keep_days: Number(retentionPolicy.day_keep_days || 730),
        backup_before_cleanup: Boolean(retentionPolicy.backup_before_cleanup),
        max_delete_rows_per_run: Number(retentionPolicy.max_delete_rows_per_run || 50000)
      };
      const res = await updateRetentionPolicy(payload);
      if (res?.ok && res?.policy) {
        setRetentionPolicy((prev) => ({ ...prev, ...res.policy }));
      }
      setRetentionResult("Retention policy saved.");
      await refreshRetentionData();
    } catch (err) {
      setRetentionResult(`Retention policy save failed: ${String(err)}`);
    } finally {
      setRetentionBusy(false);
    }
  };

  const executeRetentionRun = async (dryRun, scope = "global") => {
    if (!isAdminDatabaseUser) return;
    setRetentionResultScope(scope);
    setRetentionBusy(true);
    setRetentionResult("");
    try {
      const res = await runRetention({
        dry_run: Boolean(dryRun),
        actor: currentUser?.username || "ui"
      });
      const status = res?.ok ? "OK" : "FAILED";
      const deletes = res?.details?.deletes || {};
      setRetentionResult(
        `${dryRun ? "Dry run" : "Cleanup run"} ${status}. Raw: ${deletes.raw_candidates ?? 0}, Minute: ${deletes.minute_candidates ?? 0}, Hour: ${deletes.hour_candidates ?? 0}, Day: ${deletes.day_candidates ?? 0}`
      );
      await refreshRetentionData();
    } catch (err) {
      setRetentionResult(`Retention run failed: ${String(err)}`);
    } finally {
      setRetentionBusy(false);
    }
  };

  const runCreateBackup = async () => {
    if (!isAdminDatabaseUser) return;
    setBackupBusy(true);
    setBackupResult("");
    try {
      const res = await createAppStoreBackup({
        actor: currentUser?.username || "ui",
        label: "manual"
      });
      if (!res?.ok) throw new Error(res?.message || "Create backup failed");
      setBackupResult(`Backup created: ${res.filename}`);
      await refreshBackups();
    } catch (err) {
      setBackupResult(`Create backup failed: ${String(err)}`);
    } finally {
      setBackupBusy(false);
    }
  };

  const runRestoreBackup = (filename) => {
    if (!isAdminDatabaseUser) return;
    withConfirm(
      "Restore Backup",
      `Restore backup '${filename}'? A safety backup of current DB will be created automatically.`,
      async () => {
        setBackupBusy(true);
        setBackupResult("");
        try {
          const res = await restoreAppStoreBackup({
            filename,
            actor: currentUser?.username || "ui"
          });
          if (!res?.ok) throw new Error(res?.message || "Restore failed");
          setBackupResult(`${res.message} | Safety backup: ${res.safety_backup || "-"}`);
          await Promise.all([refreshBackups(), refreshRetentionData()]);
        } catch (err) {
          setBackupResult(`Restore failed: ${String(err)}`);
        } finally {
          setBackupBusy(false);
        }
      }
    );
  };

  const runDeleteBackup = (filename) => {
    if (!isAdminDatabaseUser) return;
    withConfirm(
      "Delete Backup",
      `Delete backup '${filename}'?`,
      async () => {
        setBackupBusy(true);
        setBackupResult("");
        try {
          const res = await deleteAppStoreBackup(filename);
          if (!res?.ok) throw new Error(res?.message || "Delete failed");
          setBackupResult(res.message || `Backup deleted: ${filename}`);
          await refreshBackups();
        } catch (err) {
          setBackupResult(`Delete failed: ${String(err)}`);
        } finally {
          setBackupBusy(false);
        }
      }
    );
  };

  const runDownloadBackup = (row) => {
    const rawPath = String(row?.path || "").trim();
    if (!rawPath) {
      setBackupResult("Download failed: backup file path not available.");
      return;
    }
    const href = `file:///${rawPath.replace(/\\/g, "/")}`;
    const a = document.createElement("a");
    a.href = href;
    a.download = String(row?.filename || "trustnode_backup.db");
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const runCleanupData = (scope = "global") => {
    const scopeLabel = scope === "local"
      ? "Local Data"
      : scope === "cloud"
        ? "Trustnode Cloud"
        : scope === "other"
          ? "Other Databases"
          : "Backup and Retention";
    setCleanupResultScope(scope);
    if (currentUser?.role !== "admin") {
      setCleanupResult("Only admin can run data cleanup.");
      return;
    }
    withConfirm(
      "Clean Data",
      `[${scopeLabel}] Delete data for '${cleanupMode}'? This action removes historian/log data.`,
      async () => {
        setCleanupBusy(true);
        setCleanupResult("");
        try {
          const res = await cleanupAppStoreData({
            mode: cleanupMode,
            actor: currentUser?.username || "admin"
          });
          if (!res?.ok) throw new Error(res?.message || "Cleanup failed");
          setCleanupResult(res.message || "Data cleanup completed.");
          await Promise.all([refreshBackups(), refreshRetentionData()]);
        } catch (err) {
          setCleanupResult(`Cleanup failed: ${String(err)}`);
        } finally {
          setCleanupBusy(false);
        }
      }
    );
  };

  useEffect(() => {
    if (!appStoreHydrated) return;
    if (!["database", "database_overview", "database_inspector"].includes(activePage)) return;
    let stopped = false;
    const run = async () => {
      try {
        await refreshDatabaseInspector();
      } catch (_) {
        // Keep page interactive on transient backend errors.
      }
    };
    run();
    const timer = setInterval(() => {
      if (stopped) return;
      run();
    }, 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [activePage, appStoreHydrated, endpointVersion]);

  const makeTagKey = (gatewayId, tagName) => `${String(gatewayId || "")}::${String(tagName || "").trim()}`;
  const isTagAlarmEnabled = (gatewayId, tagName) => tagAlarmPrefsRef.current[makeTagKey(gatewayId, tagName)] !== false;
  const setTagAlarmEnabled = (gatewayId, tagName, enabled) => {
    const key = makeTagKey(gatewayId, tagName);
    setTagAlarmPrefs((prev) => {
      if (enabled) {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: false };
    });
    // Keep existing alarm rows aligned with tag-level pause state.
    setAlarms((prev) =>
      prev.map((a) => {
        if (String(a.gateway_id || "") !== String(gatewayId || "")) return a;
        if (String(a.tag || "") !== String(tagName || "").trim()) return a;
        return {
          ...a,
          notification_paused: !enabled,
          paused_by_tag: !enabled
        };
      })
    );
  };

  const parseEmailList = (raw) =>
    String(raw || "")
      .split(/[;,]+/)
      .map((x) => x.trim())
      .filter(Boolean);

  const buildEmailTransportPayload = (cfg) => ({
    transport: cfg?.transport === "php_http" ? "php_http" : "smtp",
    smtp: {
      host: cfg?.host || "",
      port: Number(cfg?.port || 587),
      username: cfg?.username || "",
      password: cfg?.password || "",
      sender_email: cfg?.sender_email || "",
      sender_name: cfg?.sender_name || "Trustnode Edge",
      use_tls: Boolean(cfg?.use_tls),
      use_ssl: Boolean(cfg?.use_ssl),
    },
    php_mail: {
      endpoint_url: cfg?.php_endpoint_url || "",
      api_token: cfg?.php_api_token || "",
      auth_header: cfg?.php_auth_header || "X-API-TOKEN",
      timeout_ms: Number(cfg?.php_timeout_ms || 6000),
      verify_tls: Boolean(cfg?.php_verify_tls ?? true),
    },
  });

  const applyTemplate = (template, context) => {
    let out = String(template || "");
    for (const [k, v] of Object.entries(context || {})) {
      out = out.replaceAll(`{{${k}}}`, String(v ?? ""));
    }
    return out;
  };

  const sendAlarmEmailNotification = async (alarmRow) => {
    if (alarmRow?.acknowledged || alarmRow?.notification_paused) return;
    if (!isTagAlarmEnabled(alarmRow?.gateway_id, alarmRow?.tag)) return;
    // Safe gate: send once per alert transition state (active/clear).
    const alertKey = String(alarmRow?.alert_key || `${alarmRow?.gateway_id || ""}:${alarmRow?.tag || ""}`);
    const eventType = String(alarmRow?.event_type || "active");
    const sentState = alarmEmailGateRef.current[alertKey];
    if (sentState === eventType) return;
    const cfg = emailSettingsRef.current || {};
    const recipients = parseEmailList(cfg.alarm_recipients);
    if (!recipients.length) return;
    const context = {
      gateway: alarmRow.gateway_name || "-",
      tag: alarmRow.tag || "-",
      value: alarmRow.value ?? "-",
      ts: alarmRow.ts || tsNow(),
      severity: alarmRow.severity || "Critical",
      message: alarmRow.message || ""
    };
    const subject = applyTemplate(cfg.alarm_subject, context);
    const html = applyTemplate(cfg.alarm_template, context);
    const text = `${context.severity} | ${context.gateway} | ${context.tag} | ${context.value} | ${context.ts}`;
    try {
      const res = await sendNotificationEmail({
        ...buildEmailTransportPayload(cfg),
        to: recipients,
        subject,
        html_body: html,
        text_body: text
      });
      addAppLog({
        level: res?.ok ? "info" : "error",
        category: "notifications",
        message: res?.ok ? `Alarm email sent (${recipients.length} recipients).` : `Alarm email failed: ${res?.message || "unknown"}`
      });
      if (res?.ok) {
        alarmEmailGateRef.current[alertKey] = eventType;
      }
    } catch (err) {
      addAppLog({
        level: "error",
        category: "notifications",
        message: `Alarm email failed: ${String(err)}`
      });
    }
  };

  const runEmailTest = async () => {
    setEmailResult("");
    try {
      const res = await testNotificationEmail({
        ...buildEmailTransportPayload(emailSettings),
        to: emailTestTo,
        mode: "test"
      });
      setEmailResult(res?.ok ? `PASS: ${res.message}` : `FAIL: ${res?.message || "Test failed"}`);
    } catch (err) {
      setEmailResult(`FAIL: ${String(err)}`);
    }
  };

  const saveCurrentEmailProfile = () => {
    const inferredName =
      (emailSettings.transport === "php_http" ? "PHP API" : "SMTP") +
      " " +
      new Date().toISOString().slice(0, 16).replace("T", " ");
    const name = String(emailProfileName || "").trim() || inferredName;
    const id = `mail_${Date.now()}`;
    const profile = {
      id,
      name,
      enabled: true,
      created_utc: tsNow(),
      settings: { ...emailSettings }
    };
    setEmailProfiles((prev) => [profile, ...prev.filter((p) => p.name !== name)].slice(0, 30));
    setActiveEmailProfileId(id);
    setEmailProfileName("");
    setError("");
  };

  const activateEmailProfile = (profileId) => {
    const profile = (emailProfiles || []).find((p) => p.id === profileId);
    if (!profile) return;
    setActiveEmailProfileId(profileId);
    if (profile.settings && typeof profile.settings === "object") {
      setEmailSettings((prev) => ({ ...prev, ...profile.settings }));
    }
  };

  const toggleEmailProfileEnabled = (profileId, enabled) => {
    setEmailProfiles((prev) => prev.map((p) => (p.id === profileId ? { ...p, enabled: Boolean(enabled) } : p)));
  };

  const removeEmailProfile = (profileId) => {
    withConfirm("Delete Email Profile", "Delete this email profile?", () => {
      setEmailProfiles((prev) => prev.filter((p) => p.id !== profileId));
      if (activeEmailProfileId === profileId) setActiveEmailProfileId("");
    });
  };

  const reportFilterOptions = useMemo(() => {
    const gatewayMap = new Map();
    for (const g of gatewayConfigs || []) {
      gatewayMap.set(String(g.id), { id: String(g.id), name: String(g.name || g.id), tags: Array.isArray(g.tags) ? g.tags : [] });
    }
    for (const r of dataLog || []) {
      const id = String(r.gateway_id || "");
      if (!id) continue;
      if (!gatewayMap.has(id)) {
        gatewayMap.set(id, { id, name: String(r.gateway_name || id), tags: [] });
      }
      const gw = gatewayMap.get(id);
      if (r?.tag && !gw.tags.includes(String(r.tag))) gw.tags.push(String(r.tag));
    }
    const gateways = Array.from(gatewayMap.values()).sort((a, b) => a.name.localeCompare(b.name));
    const selected = new Set(toStringArray(reportFilters.selected_gateway_ids).map(String));
    const tagsSet = new Set();
    for (const g of gateways) {
      if (selected.size && selected.has(g.id)) {
        for (const t of g.tags || []) tagsSet.add(String(t));
      }
    }
    return {
      gateways,
      tags: Array.from(tagsSet).sort((a, b) => a.localeCompare(b)),
    };
  }, [gatewayConfigs, dataLog, reportFilters.selected_gateway_ids]);

  useEffect(() => {
    const validTags = new Set((reportFilterOptions.tags || []).map(String));
    setReportFilters((prev) => {
      const cur = Array.isArray(prev.selected_tags) ? prev.selected_tags : [];
      const next = cur.filter((t) => validTags.has(String(t)));
      const axes = prev.tag_axes && typeof prev.tag_axes === "object" ? prev.tag_axes : {};
      const colors = prev.tag_colors && typeof prev.tag_colors === "object" ? prev.tag_colors : {};
      const nextAxes = {};
      const nextColors = {};
      for (const [k, v] of Object.entries(axes)) {
        if (validTags.has(String(k))) nextAxes[k] = v === "right" ? "right" : "left";
      }
      for (const [k, v] of Object.entries(colors)) {
        if (validTags.has(String(k))) {
          const fallback = REPORT_SERIES_COLORS[Math.abs(String(k).split("").reduce((a, c) => a + c.charCodeAt(0), 0)) % REPORT_SERIES_COLORS.length];
          nextColors[k] = normalizeHexColor(v, fallback);
        }
      }
      if (
        next.length === cur.length &&
        Object.keys(nextAxes).length === Object.keys(axes).length &&
        Object.keys(nextColors).length === Object.keys(colors).length
      ) return prev;
      return { ...prev, selected_tags: next, tag_axes: nextAxes, tag_colors: nextColors };
    });
  }, [reportFilterOptions.tags]);

  const isSelected = (list, value) => Array.isArray(list) && list.includes(value);
  const toggleFilterSelection = (key, value) => {
    setReportFilters((prev) => {
      const cur = Array.isArray(prev[key]) ? prev[key] : [];
      const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value];
      return { ...prev, [key]: next };
    });
  };
  const getReportTagAxis = (tag) => {
    const axis = reportFilters?.tag_axes?.[tag];
    return axis === "right" ? "right" : "left";
  };
  const setReportTagAxis = (tag, axis) => {
    setReportFilters((prev) => ({
      ...prev,
      tag_axes: {
        ...(prev.tag_axes || {}),
        [tag]: axis === "right" ? "right" : "left"
      }
    }));
  };
  const getReportTagColor = (tag, idx = 0) => {
    const v = reportFilters?.tag_colors?.[tag];
    return normalizeHexColor(v, REPORT_SERIES_COLORS[idx % REPORT_SERIES_COLORS.length]);
  };
  const setReportTagColor = (tag, color) => {
    setReportFilters((prev) => ({
      ...prev,
      tag_colors: {
        ...(prev.tag_colors || {}),
        [tag]: normalizeHexColor(color, REPORT_SERIES_COLORS[0])
      }
    }));
  };
  const reportChartType = reportFilters?.report_chart_type === "bar" ? "bar" : "line";
  const toggleReportChartType = () => {
    setReportFilters((prev) => ({
      ...prev,
      report_chart_type: prev?.report_chart_type === "bar" ? "line" : "bar"
    }));
  };

  const getReportingRowsForFilters = (filters) => {
    const fromMs = filters?.from ? new Date(filters.from).getTime() : null;
    const toMs = filters?.to ? new Date(filters.to).getTime() : null;
    const batchNeedle = String(filters?.batch || "").trim().toLowerCase();
    const tagSet = new Set((filters?.selected_tags || []).map((x) => String(x)));
    const gwSet = new Set((filters?.selected_gateway_ids || []).map((x) => String(x)));
    const maxRows = Math.max(200, Number(filters?.max_rows || 3000));
    return (dataLog || [])
      .filter((r) => {
        const tsMs = new Date(r.ts).getTime();
        if (fromMs && Number.isFinite(fromMs) && tsMs < fromMs) return false;
        if (toMs && Number.isFinite(toMs) && tsMs > toMs) return false;
        if (tagSet.size && !tagSet.has(String(r.tag || ""))) return false;
        if (gwSet.size && !gwSet.has(String(r.gateway_id || r.gateway_name || ""))) return false;
        if (batchNeedle && !String(r.source || "").toLowerCase().includes(batchNeedle)) return false;
        return true;
      })
      .slice(0, maxRows);
  };

  const reportSelectedTags = useMemo(() => {
    const safeSelectedTags = toStringArray(reportFilters.selected_tags);
    if (safeSelectedTags.length) return safeSelectedTags;
    return reportFilterOptions.tags.slice(0, 6);
  }, [reportFilters.selected_tags, reportFilterOptions.tags]);

  const safeReportDocuments = useMemo(() => sanitizeReportDocuments(reportDocuments), [reportDocuments]);

  const reportPivotRows = useMemo(() => {
    const map = new Map();
    const tags = reportSelectedTags;
    for (const row of reportLoadedRows || []) {
      const ts = String(row.ts || "");
      if (!map.has(ts)) {
        const base = { ts };
        for (const t of tags) base[t] = "";
        base._gateway = row.gateway_name || "";
        base._device = row.device_name || "";
        base._db = row.database_name || "";
        base._plc = row.plc_ip || "";
        map.set(ts, base);
      }
      const target = map.get(ts);
      const t = String(row.tag || "");
      if (tags.includes(t)) target[t] = row.value ?? "";
    }
    return Array.from(map.values()).sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  }, [reportLoadedRows, reportSelectedTags]);

  const reportingChartData = useMemo(() => {
    const rows = reportPivotRows.slice(-240);
    return rows.map((r) => {
      const entry = { ts: String(r.ts || "").slice(11, 19) };
      for (const tag of reportSelectedTags.slice(0, 6)) {
        const v = Number(r[tag]);
        entry[tag] = Number.isNaN(v) ? null : v;
      }
      return entry;
    });
  }, [reportPivotRows, reportSelectedTags]);

  const buildReportCsv = (pivotRows, tags) => {
    const header = ["timestamp_utc", ...tags];
    const lines = [header.join(",")];
    for (const r of pivotRows) {
      const vals = [r.ts, ...tags.map((t) => r[t])].map((v) => `"${String(v ?? "").replaceAll("\"", "\"\"")}"`);
      lines.push(vals.join(","));
    }
    return lines.join("\n");
  };

  const buildReportSvgChart = (doc) => {
    const rows = (doc.preview_rows || []).slice(-120);
    const series = (doc.chart_series || []).filter((s) => (doc.columns || []).includes(s.tag));
    if (!rows.length || !series.length) return "";
    const width = 980;
    const height = 280;
    const pad = { top: 18, right: 64, bottom: 34, left: 64 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const allVals = [];
    for (const r of rows) {
      for (const s of series) {
        const v = Number(r[s.tag]);
        if (!Number.isNaN(v)) allVals.push(v);
      }
    }
    if (!allVals.length) return "";
    const minV = Math.min(...allVals);
    const maxV = Math.max(...allVals);
    const span = Math.max(1, maxV - minV);
    const toY = (v) => pad.top + (plotH - ((v - minV) / span) * plotH);
    const toX = (i) => pad.left + (rows.length <= 1 ? 0 : (i / (rows.length - 1)) * plotW);
    const esc = (x) => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    const bg = `<rect x="0" y="0" width="${width}" height="${height}" fill="#ffffff"/>`;
    const axes = [
      `<line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${pad.top + plotH}" stroke="#94a3b8" stroke-width="1"/>`,
      `<line x1="${pad.left}" y1="${pad.top + plotH}" x2="${pad.left + plotW}" y2="${pad.top + plotH}" stroke="#94a3b8" stroke-width="1"/>`,
      `<line x1="${pad.left + plotW}" y1="${pad.top}" x2="${pad.left + plotW}" y2="${pad.top + plotH}" stroke="#94a3b8" stroke-width="1"/>`,
      `<text x="${pad.left - 8}" y="${pad.top + 12}" font-size="11" text-anchor="end" fill="#475569">${maxV.toFixed(2)}</text>`,
      `<text x="${pad.left - 8}" y="${pad.top + plotH}" font-size="11" text-anchor="end" fill="#475569">${minV.toFixed(2)}</text>`
    ].join("");
    const drawings = [];
    series.forEach((s) => {
      const color = esc(s.color || "#16a34a");
      const type = s.chart_type === "bar" ? "bar" : "line";
      if (type === "bar") {
        const barW = Math.max(2, plotW / Math.max(1, rows.length) / Math.max(1, series.length) * 0.7);
        const idxInBar = series.filter((x) => (x.chart_type === "bar")).findIndex((x) => x.tag === s.tag);
        rows.forEach((r, i) => {
          const v = Number(r[s.tag]);
          if (Number.isNaN(v)) return;
          const xCenter = toX(i);
          const x = xCenter - (barW * series.length) / 2 + idxInBar * barW;
          const y = toY(v);
          drawings.push(`<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${(pad.top + plotH - y).toFixed(2)}" fill="${color}" opacity="0.8"/>`);
        });
      } else {
        const pts = rows.map((r, i) => {
          const v = Number(r[s.tag]);
          if (Number.isNaN(v)) return null;
          return `${toX(i).toFixed(2)},${toY(v).toFixed(2)}`;
        }).filter(Boolean).join(" ");
        if (pts) drawings.push(`<polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>`);
      }
    });
    const legend = series.map((s, i) => {
      const x = pad.left + (i % 4) * 220;
      const y = height - 10 - Math.floor(i / 4) * 14;
      return `<g><rect x="${x}" y="${y - 8}" width="12" height="8" fill="${esc(s.color || "#16a34a")}"/><text x="${x + 16}" y="${y}" font-size="11" fill="#334155">${esc(`${s.tag} (${s.chart_type || "line"}, ${s.axis || "left"})`)}</text></g>`;
    }).join("");
    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">${bg}${axes}${drawings.join("")}${legend}</svg>`;
  };

  const buildReportPreviewHtml = (doc) => {
    const tags = doc.columns || [];
    const rows = doc.preview_rows || [];
    const chartSvg = buildReportSvgChart(doc);
    const headerCells = ["Timestamp (UTC)", ...tags].map((x) => `<th>${String(x).replaceAll("<", "&lt;")}</th>`).join("");
    const bodyRows = rows.map((r) => {
      const cells = [r.ts, ...tags.map((t) => r[t])].map((v) => `<td>${String(v ?? "").replaceAll("<", "&lt;")}</td>`).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    return `<!doctype html><html><head><meta charset="utf-8"/><title>Trustnode Report</title><style>body{font-family:Segoe UI,Arial,sans-serif;padding:16px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d1d5db;padding:6px 8px;font-size:12px}th{background:#f3f4f6;text-align:left}.meta{margin-bottom:10px;color:#374151;font-size:13px}.chart-box{margin:12px 0;border:1px solid #d1d5db;border-radius:10px;padding:8px;background:#fff;display:flex;justify-content:center}</style></head><body><h2>Trustnode Report</h2><div class="meta"><div><b>Created:</b> ${doc.created_utc}</div><div><b>Generated By:</b> ${doc.generated_by}</div><div><b>Summary:</b> ${String(doc.summary || "").replaceAll("<", "&lt;")}</div></div>${chartSvg ? `<div class="chart-box">${chartSvg}</div>` : ""}<table><thead><tr>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table></body></html>`;
  };

  const buildReportSummary = (filters, rows, tags) => {
    const g = (filters.selected_gateway_ids || [])
      .map((id) => gatewayConfigs.find((x) => String(x.id) === String(id))?.name || id)
      .join("; ") || "ALL";
    const t = (tags || []).join("; ") || "AUTO";
    return `GW: ${g} | TAGS: ${t} | ROWS: ${rows.length}`;
  };

  const loadReportingData = (customFilters = null) => {
    const f = customFilters || reportFilters;
    if (!(f.selected_gateway_ids || []).length) {
      setReportLoadedRows([]);
      setReportLoadedAt("");
      setReportSummaryText("Select at least one gateway to load report data.");
      setReportPreviewDoc(null);
      return [];
    }
    const rows = getReportingRowsForFilters(f);
    const tags = (f.selected_tags?.length ? f.selected_tags : reportFilterOptions.tags.slice(0, 6));
    setReportLoadedRows(rows);
    setReportLoadedAt(tsNow());
    setReportSummaryText(buildReportSummary(f, rows, tags));
    setReportPreviewDoc(null);
    addAppLog({ level: "info", category: "reporting", message: `Report data loaded: ${rows.length} rows.` });
    return rows;
  };

  const createReportDocument = (format = "csv", customFilters = null) => {
    const f = customFilters || reportFilters;
    if (!(f.selected_gateway_ids || []).length) {
      setReportSummaryText("Select at least one gateway before generating a report.");
      return null;
    }
    const rows = customFilters ? getReportingRowsForFilters(f) : (reportLoadedRows.length ? reportLoadedRows : loadReportingData(f));
    const tags = (f.selected_tags?.length ? f.selected_tags : reportFilterOptions.tags.slice(0, 6));
    const pivot = (() => {
      const map = new Map();
      for (const row of rows) {
        const ts = String(row.ts || "");
        if (!map.has(ts)) {
          const base = { ts };
          for (const t of tags) base[t] = "";
          map.set(ts, base);
        }
        const target = map.get(ts);
        const t = String(row.tag || "");
        if (tags.includes(t)) target[t] = row.value ?? "";
      }
      return Array.from(map.values()).sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    })();

    const csvContent = buildReportCsv(pivot, tags);
    const summary = buildReportSummary(f, rows, tags);
    const doc = {
      id: crypto.randomUUID(),
      created_utc: tsNow(),
      generated_by: currentUser?.username || "system",
      format,
      filters: f,
      row_count: pivot.length,
      columns_count: tags.length + 1,
      columns: tags,
      chart_series: tags.map((tag, idx) => ({
        tag,
        axis: (f.tag_axes && f.tag_axes[tag] === "right") ? "right" : "left",
        color: normalizeHexColor((f.tag_colors && f.tag_colors[tag]), REPORT_SERIES_COLORS[idx % REPORT_SERIES_COLORS.length]),
        chart_type: (f.report_chart_type === "bar") ? "bar" : "line"
      })),
      summary,
      storage: endpointMode === "cloud" ? "cloud_app_store" : "local_app_store",
      csv_content: csvContent,
      preview_rows: pivot.slice(0, 1200),
    };
    const html = buildReportPreviewHtml(doc);
    doc.html_content = html;
    doc.size_bytes = Number((csvContent.length + html.length) || 0);
    setReportDocuments((prev) => [doc, ...prev].slice(0, 120));
    addAppLog({
      level: "info",
      category: "reporting",
      message: `Report generated (${format.toUpperCase()}) with ${pivot.length} rows.`
    });
    return doc;
  };

  const downloadReportCsv = (doc) => {
    if (!doc) return;
    downloadText(`trustnode_report_${String(doc.created_utc || "").replaceAll(/[: ]/g, "_")}.csv`, doc.csv_content || "", "text/csv;charset=utf-8");
  };

  const openReportPreview = (doc) => {
    if (!doc) return;
    setReportPreviewDoc(doc);
  };

  const downloadReportPdf = (doc) => {
    if (!doc) return;
    const win = window.open("", "_blank", "width=1200,height=820");
    if (!win) return;
    const html = doc.html_content || buildReportPreviewHtml(doc);
    win.document.write(`${html}<script>window.onload=function(){window.print();}<\/script>`);
    win.document.close();
  };

  const removeReportDocument = (docId) => {
    withConfirm("Delete Report", "Delete this generated report document?", () => {
      setReportDocuments((prev) => prev.filter((d) => d.id !== docId));
    });
  };

  const openScheduleCreate = () => {
    setEditingScheduleId(null);
    setScheduleForm({
      name: "",
      enabled: true,
      recurrence: "daily",
      hour: "08",
      minute: "00",
      day_of_week: "1",
      day_of_month: "1",
      format: "csv",
      recipients: emailSettings.report_recipients || "",
      filters: {
        from: reportFilters.from || "",
        to: reportFilters.to || "",
        selected_gateway_ids: [...(reportFilters.selected_gateway_ids || [])],
        selected_tags: [...(reportFilters.selected_tags || [])],
        batch: reportFilters.batch || "",
        max_rows: Number(reportFilters.max_rows || 3000),
      },
      last_run_utc: "",
      next_run_utc: ""
    });
    setShowScheduleModal(true);
  };

  const saveScheduledReport = () => {
    if (!canEditPage("scheduled_reports")) return;
    if (!String(scheduleForm.name || "").trim()) return;
    const next = {
      ...scheduleForm,
      id: editingScheduleId || crypto.randomUUID(),
      updated_utc: tsNow()
    };
    setScheduledReports((prev) => {
      if (editingScheduleId) return prev.map((r) => (r.id === editingScheduleId ? next : r));
      return [next, ...prev];
    });
    setShowScheduleModal(false);
    setEditingScheduleId(null);
  };

  const openEditScheduledReport = (row) => {
    setEditingScheduleId(row.id);
    setScheduleForm({
      ...row,
      filters: {
        from: "",
        to: "",
        selected_gateway_ids: [],
        selected_tags: [],
        batch: "",
        max_rows: 3000,
        ...(row.filters || {}),
      }
    });
    setShowScheduleModal(true);
  };

  const removeScheduledReport = (reportId) => {
    if (!canEditPage("scheduled_reports")) return;
    withConfirm("Delete Scheduled Report", "Delete this scheduled report?", () => {
      setScheduledReports((prev) => prev.filter((x) => x.id !== reportId));
    });
  };

  const runScheduledReportNow = async (row) => {
    const doc = createReportDocument(row.format || "csv", row.filters || null);
    if (!doc) return;
    const recipients = parseEmailList(row.recipients || emailSettings.report_recipients);
    if (!recipients.length) return;
    try {
      const context = {
        name: row.name || "Report",
        row_count: doc.row_count,
        created_utc: doc.created_utc || tsNow(),
      };
      await sendNotificationEmail({
        ...buildEmailTransportPayload(emailSettings),
        to: recipients,
        subject: applyTemplate(emailSettings.report_subject || "[REPORT] {{name}}", context),
        html_body: applyTemplate(emailSettings.report_template || "", context),
        text_body: `Scheduled report '${context.name}' generated with ${context.row_count} rows.`
      });
      setScheduledReports((prev) =>
        prev.map((x) => (x.id === row.id ? { ...x, last_run_utc: tsNow() } : x))
      );
    } catch (err) {
      addAppLog({ level: "error", category: "reporting", message: `Manual scheduled report send failed: ${String(err)}` });
    }
  };

  const sleepMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const isNetworkFetchError = (err) => /failed to fetch|networkerror|aborterror/i.test(String(err?.message || err || ""));
  const waitForBackendReady = async (timeoutMs = 5000, pollMs = 500) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      try {
        await getHealth();
        return true;
      } catch (_) {
        // keep polling until timeout
      }
      await sleepMs(pollMs);
    }
    return false;
  };

  const submitLogin = async () => {
    const username = String(loginForm.username || "").trim();
    const password = String(loginForm.password || "");
    if (!username || !password) {
      setLoginError("Enter username and password");
      return;
    }
    setLoginBusy(true);
    try {
      let res;
      try {
        res = await loginAuth({ username, password });
      } catch (firstErr) {
        if (!isNetworkFetchError(firstErr)) throw firstErr;
        setLoginError("Backend is starting, retrying...");
        await waitForBackendReady(6000, 500);
        res = await loginAuth({ username, password });
      }
      const u = res?.user || null;
      if (!u?.username) {
        setLoginError("Login failed");
        return;
      }
      const matched = users.find((x) => x.username === u.username) || {
        username: u.username,
        password: "",
        role: u.role || "viewer",
        permissions: normalizePermissions(u.permissions || {}, u.role || "viewer")
      };
      setCurrentUser(matched);
      setShowUserMenu(false);
      setLoginError("");
      setLoginForm({ username: "", password: "" });
    } catch (err) {
      clearAuthToken();
      if (isNetworkFetchError(err)) {
        setLoginError("Backend not ready yet. Please try again in a moment.");
      } else {
        setLoginError(String(err?.message || "Invalid username or password"));
      }
    } finally {
      setLoginBusy(false);
    }
  };

  const switchUser = () => {
    clearAuthToken();
    localStorage.removeItem(CURRENT_USER_STORAGE_KEY);
    setShowUserMenu(false);
    setCurrentUser(null);
  };

  const logout = () => {
    clearAuthToken();
    localStorage.removeItem(CURRENT_USER_STORAGE_KEY);
    setCurrentUser(null);
    setShowUserMenu(false);
  };

  const createUser = () => {
    if (currentUser?.role !== "admin") return;
    if (!newUserForm.username.trim() || !newUserForm.password.trim()) return;
    if (users.some((u) => u.username === newUserForm.username.trim())) {
      setError("User already exists");
      return;
    }
    const newUser = {
      username: newUserForm.username.trim(),
      password: newUserForm.password,
      role: newUserForm.role,
      permissions: normalizePermissions(newUserForm.permissions, newUserForm.role)
    };
    setUsers((prev) => [...prev, newUser]);
    setNewUserForm({
      username: "",
      password: "",
      role: "viewer",
      permissions: buildRolePermissions("viewer")
    });
  };

  const canManageUsers = currentUser?.username === "admin" && currentUser?.password === "admin";

  const openEditUser = (user) => {
    if (!canManageUsers || !user) return;
    setEditingUsername(String(user.username || ""));
    setEditUserForm({
      password: String(user.password || ""),
      role: String(user.role || "viewer"),
      permissions: normalizePermissions(user.permissions, user.role || "viewer")
    });
    setShowEditUserModal(true);
  };

  const saveEditedUser = () => {
    if (!canManageUsers || !editingUsername) return;
    setUsers((prev) =>
      prev.map((u) => {
        if (u.username !== editingUsername) return u;
        return {
          ...u,
          role: String(editUserForm.role || "viewer"),
          password: String(editUserForm.password || ""),
          permissions: normalizePermissions(editUserForm.permissions, editUserForm.role || "viewer")
        };
      })
    );
    if (currentUser?.username === editingUsername) {
      setCurrentUser((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          role: String(editUserForm.role || "viewer"),
          password: String(editUserForm.password || ""),
          permissions: normalizePermissions(editUserForm.permissions, editUserForm.role || "viewer")
        };
      });
    }
    setShowEditUserModal(false);
    setEditingUsername("");
  };

  const deleteUser = (username) => {
    if (!canManageUsers) return;
    const target = String(username || "").trim();
    if (!target) return;
    if (target === "admin") {
      setError("The built-in admin user cannot be deleted.");
      return;
    }
    withConfirm("Delete User", `Delete user '${target}'?`, () => {
      setUsers((prev) => prev.filter((u) => String(u.username) !== target));
      if (currentUser?.username === target) logout();
    });
  };

  if (!currentUser) {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <div className="auth-brand">
            <img src="trustnode_logo.png" alt="Trustnode" className="auth-logo" />
            <div>
              <div className="auth-title">Trustnode Edge</div>
              <div className="auth-subtitle">Secure PLC Data Gateway</div>
            </div>
          </div>
          <h3 className="auth-heading">Sign In</h3>
          <label>
            Username
            <input
              value={loginForm.username}
              onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })}
              placeholder="Enter username"
            />
          </label>
          <label>
            Password
            <div className="pw-input-wrap">
              <input
                type={showLoginPassword ? "text" : "password"}
                value={loginForm.password}
                onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
                placeholder="Enter password"
              />
              <button className="pw-icon-btn" onClick={() => setShowLoginPassword((v) => !v)} type="button" aria-label="Toggle password visibility">
                <EyeIcon open={showLoginPassword} />
              </button>
            </div>
          </label>
          <label className="remember-row">
            <input
              type="checkbox"
              checked={rememberUser}
              onChange={(e) => setRememberUser(e.target.checked)}
            />
            <span className="remember-label">Remember this user</span>
          </label>
          {loginError ? <div className="error">{loginError}</div> : null}
          <button className="btn btn-primary auth-submit" onClick={submitLogin} disabled={loginBusy}>
            {loginBusy ? "Signing in..." : "Sign In"}
          </button>
          <div className="auth-help">
            Default admin credentials: <strong>admin / admin</strong>
          </div>
        </div>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="loading">
        <div className="loading-card">
          <img src="trustnode_logo.png" alt="Trustnode" className="loading-logo" />
          <div className="loading-title">
            {bootState === "waiting_backend"
              ? "Waiting for backend service..."
              : "Loading Trustnode Edge..."}
          </div>
          <div className="loading-progress">
            <div className="loading-progress-bar" />
          </div>
          {error ? <div className="loading-error">{error}</div> : null}
        </div>
      </div>
    );
  }

  const renderLock = (page) => (!canEditPage(page) ? <span className="lock-tag">LOCK</span> : null);

  return (
    <div className="shell">
      <header className="app-header">
        <div className="header-left">
          <button className="nav-toggle-btn" onClick={() => setSidebarCollapsed((v) => !v)} aria-label="Toggle navigation">
            <HamburgerIcon />
          </button>
          <div className="brand">
            <img src="trustnode_logo.png" alt="Trustnode" className="brand-logo" />
            <div>
              <div className="brand-title">Trustnode Edge</div>
              <div className="brand-subtitle">Industrial Data Gateway</div>
            </div>
          </div>
        </div>
        <div className="header-center">
          {(isHostedWebClient || endpointMode === "cloud") ? (
            <div className="header-cloud-controls">
              <span className="header-cloud-label">Edge</span>
              <select
                className="header-edge-select"
                value={selectedCloudEdgeKey}
                onChange={(e) => setSelectedCloudEdgeKey(e.target.value)}
                title="Select which edge source to monitor"
              >
                <option value={CLOUD_EDGE_ALL_KEY}>All edges</option>
                {cloudSourceRows.map((s) => (
                  <option key={`edge-opt-${s.key}`} value={s.key}>
                    {`${s.source} | ${s.site} | ${s.area} | ${s.equipment}`}
                  </option>
                ))}
              </select>
              <span className="header-cloud-label">Edge Link</span>
              <span
                className={`status-pill ${
                  edgeLinkState.state === "online"
                    ? "status-online"
                    : edgeLinkState.state === "offline"
                      ? "status-offline"
                      : "status-warning"
                }`}
                title={edgeLinkState.message}
              >
                {edgeLinkState.state === "online" ? "HEALTHY" : edgeLinkState.state === "offline" ? "UNREACHABLE" : "UNKNOWN"}
              </span>
            </div>
          ) : null}
        </div>
        <div className="header-right">
          <button className="icon-btn" title="Notifications" onClick={() => handleNavClick("alarms")}>
            <BellIcon />
            {criticalAlarmCount > 0 ? <span className="notif-dot">{criticalAlarmCount}</span> : null}
          </button>
          <button className="icon-btn theme-btn" onClick={toggleTheme} title="Toggle light/dark mode">
            <ThemeIcon theme={theme} />
          </button>
          <button className="icon-btn theme-btn" onClick={toggleFullscreen} title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}>
            <FullscreenIcon active={isFullscreen} />
          </button>
        </div>
      </header>

      <div className={`body ${sidebarCollapsed ? "sidebar-hidden" : ""}`}>
        <aside className={`sidebar ${sidebarCollapsed ? "hidden" : ""}`}>
          <div className="sidebar-scroll">
            {NAV_SECTIONS.map((section) => (
              <div key={section.id} className="nav-section">
                <button className="nav-group-btn" onClick={() => toggleSection(section.id)}>
                  {sidebarCollapsed ? section.title.slice(0, 2).toUpperCase() : section.title}
                  {!sidebarCollapsed ? <span>{expandedSections[section.id] ? "-" : "+"}</span> : null}
                </button>
                {!sidebarCollapsed && expandedSections[section.id]
                  ? section.items.map((item) => {
                      const id = pageId(item);
                      const active = activePage === id;
                      const locked = !canOpenPage(id);
                      return (
                        <button
                          key={item}
                          className={`nav-item nav-subitem ${active ? "active" : ""}`}
                          onClick={() => handleNavClick(id)}
                          title={item}
                          disabled={locked}
                        >
                          <span className="nav-icon"><MenuIcon page={id} /></span>
                          <span>{item}</span>
                          {locked ? <span className="lock-tag">LOCK</span> : null}
                        </button>
                      );
                    })
                  : null}
                {sidebarCollapsed
                  ? section.items.map((item) => {
                      const id = pageId(item);
                      const active = activePage === id;
                      const locked = !canOpenPage(id);
                      return (
                        <button
                          key={item}
                          className={`nav-item nav-icon-only ${active ? "active" : ""}`}
                          onClick={() => handleNavClick(id)}
                          title={`${item}${locked ? " (locked)" : ""}`}
                          disabled={locked}
                        >
                          <span className="nav-icon-center"><MenuIcon page={id} /></span>
                        </button>
                      );
                    })
                  : null}
              </div>
            ))}
          </div>

          <div className="sidebar-footer">
            <div className="user-menu-wrap" ref={userMenuRef}>
              <button
                className="user-menu-btn"
                onClick={() => setShowUserMenu((v) => !v)}
                aria-expanded={showUserMenu}
                title="User menu"
              >
                <div className="user-box">
                  <div className="user-name">{currentUser.username}</div>
                  <div className="user-role">{currentUser.role}</div>
                </div>
                <span className="user-menu-caret">{showUserMenu ? "▲" : "▼"}</span>
              </button>
              {showUserMenu ? (
                <div className="user-menu-panel">
                  <button className="user-menu-item" onClick={switchUser}>
                    <SwitchUserIcon />
                    <span>Switch User</span>
                  </button>
                  <button className="user-menu-item" onClick={logout}>
                    <LogoutIcon />
                    <span>Logout</span>
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </aside>

        <main className="content">
          <div className="content-scroll" style={{ paddingBottom: `${contentBottomPad}px` }}>
          {error ? <div className="error">{error}</div> : null}
          {status?.db_last_error ? <div className="error">Database write error: {status.db_last_error}</div> : null}
          {activePage === "gateway_configuration" && appStoreHydrated && startupWarningsReady && unknownRunningGateways.length ? (
            <div className="error">
              Found running gateway workers not mapped in this page ({unknownRunningGateways.map((g) => g.gateway_id).join(", ")}).
              Use "Stop All" to stop every worker.
            </div>
          ) : null}
          <section className="page-title-row">
            <h1>
              {pageTitle(activePage)} {renderLock(activePage)}
            </h1>
          </section>

          {activePage === "dashboard" ? (
            <>
              <section className="page-tools dashboard-tools">
                <button className="btn btn-primary icon-text-btn" onClick={openAddDashboardWidget} disabled={!canEditPage("dashboard")}>
                  <AddIcon />
                  <span>Add Item</span>
                </button>
                <label className="dashboard-per-row-label">
                  Per Row
                  <select value={dashboardPerRow} onChange={(e) => setDashboardPerRow(Math.min(4, Math.max(1, Number(e.target.value || 2))))}>
                    <option value={1}>1</option>
                    <option value={2}>2</option>
                    <option value={3}>3</option>
                    <option value={4}>4</option>
                  </select>
                </label>
                <div className="dashboard-mode-toggle" role="group" aria-label="Dashboard mode">
                  <button
                    className="icon-btn icon-btn-start"
                    onClick={() => setDashboardMode((prev) => (prev === "kpi" ? "chart" : "kpi"))}
                    type="button"
                    title={dashboardMode === "kpi" ? "Switch to chart mode" : "Switch to KPI mode"}
                  >
                    {dashboardMode === "kpi" ? <ListIcon /> : <ChartIcon />}
                  </button>
                </div>
              </section>
              <section
                className={`dashboard-grid dashboard-grid-${dashboardPerRow} dashboard-mode-${dashboardMode}`}
                style={{ gridTemplateColumns: `repeat(${dashboardPerRow}, minmax(0, 1fr))` }}
              >
                {dashboardItems.map((item, idx) => (
                  <article key={item.id} className="card dashboard-card">
                    {dashboardMode === "kpi" ? (
                      <div className="dashboard-kpi-card">
                        <div className="dashboard-kpi-value" style={{ color: item.color }}>
                          <div className="dashboard-kpi-value-main">
                            {item.last_value === null ? "-" : item.last_value.toFixed(3)}
                          </div>
                          <div className="dashboard-kpi-value-last">Last: {item.last_ts}</div>
                        </div>
                        <div className="dashboard-kpi-meta">
                          <div className="dashboard-kpi-title">{item.title}</div>
                          <div>Tag: {item.tag_name || "-"}</div>
                          <div>Gateway: {item.gateway_name}</div>
                          <div>Device: {item.device_name}</div>
                        </div>
                                                <div className="dashboard-kpi-actions">
                          <div className="dashboard-kpi-main-actions">
                            <button className="icon-btn table-action-btn" onClick={() => openTagMonitor(item.monitorRow)} title="Open tag monitor">
                              <ChartIcon />
                            </button>
                            <button className="icon-btn table-action-btn" onClick={() => openEditDashboardWidget(item)} disabled={!canEditPage("dashboard")} title="Edit">
                              <EditIcon />
                            </button>
                            <button className="icon-btn table-action-btn danger" onClick={() => removeDashboardWidget(item.id)} disabled={!canEditPage("dashboard")} title="Delete">
                              <DeleteIcon />
                            </button>
                          </div>
                          <div className="dashboard-kpi-move-actions">
                            <button className="icon-btn table-action-btn" onClick={() => moveDashboardWidget(item.id, -1)} disabled={!canEditPage("dashboard") || idx === 0} title="Move up">
                              <MoveUpIcon />
                            </button>
                            <button className="icon-btn table-action-btn" onClick={() => moveDashboardWidget(item.id, 1)} disabled={!canEditPage("dashboard") || idx === dashboardItems.length - 1} title="Move down">
                              <MoveDownIcon />
                            </button>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div className="dashboard-chart-card">
                        <div className="row trend-header-row">
                          <h3>
                            {item.title}
                            <span className="dashboard-title-divider">|</span>
                            <span className="dashboard-live-inline" style={{ color: item.color }}>
                              {item.last_value === null ? "-" : item.last_value.toFixed(3)}
                            </span>
                          </h3>
                          <div className="row">
                            <button
                              className="btn btn-primary btn-sm"
                              onClick={() => toggleDashboardWidgetChartType(item.id)}
                              disabled={!canEditPage("dashboard")}
                              type="button"
                              title="Toggle line/bar"
                            >
                              {item.chart_type === "bar" ? "Line" : "Bar"}
                            </button>
                            <button className="icon-btn table-action-btn" onClick={() => openTagMonitor(item.monitorRow)} title="Open tag monitor">
                              <ChartIcon />
                            </button>
                            <button className="icon-btn table-action-btn" onClick={() => openEditDashboardWidget(item)} disabled={!canEditPage("dashboard")} title="Edit">
                              <EditIcon />
                            </button>
                            <button className="icon-btn table-action-btn danger" onClick={() => removeDashboardWidget(item.id)} disabled={!canEditPage("dashboard")} title="Delete">
                              <DeleteIcon />
                            </button>
                          </div>
                        </div>
                        <div className="meta">
                          <span>Value: {item.last_value === null ? "-" : item.last_value.toFixed(3)}</span>
                          <span>Last: {item.last_ts}</span>
                          <span>Device: {item.device_name}</span>
                          <span>Gateway: {item.gateway_name}</span>
                        </div>
                        <div className="chart-wrap">
                          {item.chart_type === "bar" ? (
                            <ResponsiveContainer width="100%" height={150}>
                              <BarChart data={item.series} margin={{ top: 8, right: 18, left: 30, bottom: 8 }} barCategoryGap="24%">
                                <XAxis
                                  dataKey="idx"
                                  type="number"
                                  tickFormatter={(v) => item.series.find((h) => h.idx === v)?.ts || ""}
                                  domain={[(min) => Number(min) - 1, (max) => Number(max) + 1]}
                                />
                                <YAxis width={60} domain={["auto", "auto"]} />
                                <Tooltip labelFormatter={(v) => item.series.find((h) => h.idx === v)?.ts || String(v)} />
                                <Bar isAnimationActive={false} dataKey="value" fill={item.color || "#16a34a"} maxBarSize={20} />
                              </BarChart>
                            </ResponsiveContainer>
                          ) : (
                            <ResponsiveContainer width="100%" height={150}>
                              <LineChart data={item.series} margin={{ top: 8, right: 18, left: 24, bottom: 8 }}>
                                <XAxis
                                  dataKey="idx"
                                  type="number"
                                  tickFormatter={(v) => item.series.find((h) => h.idx === v)?.ts || ""}
                                  domain={[(min) => Number(min) - 1, (max) => Number(max) + 1]}
                                />
                                <YAxis width={52} domain={["auto", "auto"]} />
                                <Tooltip labelFormatter={(v) => item.series.find((h) => h.idx === v)?.ts || String(v)} />
                                <Line isAnimationActive={false} type="linear" dataKey="value" stroke={item.color || "#16a34a"} strokeWidth={2} dot={false} />
                              </LineChart>
                            </ResponsiveContainer>
                          )}
                        </div>
                      </div>
                    )}
                  </article>
                ))}
                {!dashboardItems.length ? (
                  <article className="card dashboard-empty-card">
                    <p>No dashboard items configured. Use <strong>Add Item</strong> to select gateway tags and build your KPI/chart view.</p>
                  </article>
                ) : null}
              </section>
            </>
          ) : null}

          {activePage === "devices" ? (
            <>
              <section className="page-tools">
                <button className="btn btn-primary icon-text-btn" onClick={openAddDevice} disabled={!canEditPage("devices")}>
                  <AddIcon />
                  <span>Add Device</span>
                </button>
              </section>
              <section className="card">
                <div className="table devices-table">
                  <div className="thead"><span>Name</span><span>Type</span><span>IP</span><span>Status</span><span>Actions</span></div>
                  {deviceRows.map((d) => (
                    <div key={d.id || d.name} className="trow">
                      <span>{d.name}</span>
                      <span>{d.gateway_type}</span>
                      <span>{d.plc_ip}</span>
                      <span>
                        <div className={`status-pill status-${d.statusKey}`}>{d.status}</div>
                        <div className="muted status-sub" title={d.last_test || ""}>
                          <span className={d.ping_ok ? "status-chip ok" : "status-chip fail"}>IP: {d.ping_ok ? "OK" : "FAIL"}</span>
                          {" "}
                          <span className={d.protocolOk ? "status-chip ok" : "status-chip fail"}>Type: {d.protocolOk ? "OK" : "FAIL"}</span>
                        </div>
                        {d.last_test ? (
                          <div className="muted status-sub" title={d.last_test}>
                            {String(d.last_test).slice(0, 140)}
                          </div>
                        ) : null}
                      </span>
                      <span className="row-actions">
                        <button className="icon-btn table-action-btn" onClick={() => openEditDevice(d)} disabled={!canEditPage("devices")} title="Edit device">
                          <EditIcon />
                        </button>
                        <button className="icon-btn table-action-btn danger" onClick={() => removeDevice(d.id)} disabled={!canDeleteRecords} title="Delete device">
                          <DeleteIcon />
                        </button>
                      </span>
                    </div>
                  ))}
	                </div>
	              </section>
	            </>
	          ) : null}

          {activePage === "gateway_configuration" ? (
            <>
              <section className="page-tools">
                <button className="btn btn-primary icon-text-btn" onClick={openAddGatewayConfig} disabled={!canEditPage("gateway_configuration")}>
                  <AddIcon />
                  <span>Add Gateway</span>
                </button>
                <button className="btn btn-success" onClick={startAllGatewayProfiles} disabled={!canControlGateways}>
                  Start All
                </button>
                <button className="btn btn-danger" onClick={stopAllGatewayProfiles} disabled={!canControlGateways}>
                  Stop All
                </button>
              </section>
              <section className="card">
                <div className="table gateway-table">
                  <div className="thead">
                    <span>Name</span><span>Device</span><span>Protocol</span><span>Address</span><span>Database</span><span>Interval</span><span>Status</span><span>Tags</span><span>Actions</span>
                  </div>
                  {gatewayConfigsView.map((g) => {
                    const dbName = dbConnections.find((db) => db.id === g.database_id)?.name || "-";
                    const deviceName = devices.find((d) => d.id === g.device_id)?.name || "-";
                    const rt = gatewayRuntimeStatuses[g.id] || null;
                    const running = Boolean(rt?.running);
                    const pending = Number(rt?.db_pending_count || 0);
                    const writes = Number(rt?.db_write_count || 0);
                    const statusKey = rt?.db_last_error ? "offline" : running ? "online" : "warning";
                    const statusText = running ? "RUNNING" : "STOPPED";
                    return (
                      <div
                        key={g.id}
                        className={`trow ${selectedGatewayId === g.id ? "selected-row" : ""}`}
                        onClick={() => setSelectedGatewayId(g.id)}
                        onDoubleClick={() => openEditGatewayConfig(g)}
                      >
                        <span>{g.name}</span>
                        <span>{deviceName}</span>
                        <span>{g.gateway_type}</span>
                        <span>{g.gateway_type === "siemens_opcua" ? (g.opc_url || "-") : (g.plc_ip || "-")}</span>
                        <span>{dbName}</span>
                        <span>{g.interval_ms} ms</span>
                        <span>
                          <div className={`status-pill status-${statusKey}`}>{statusText}</div>
                          <div className="muted status-sub">
                            <span>W:{writes}</span>
                            {" "}
                            <span>P:{pending}</span>
                          </div>
                        </span>
                        <span className="tags-stack">
                          {(g.tags || []).length ? (g.tags || []).map((tag) => <div key={`${g.id}-${tag}`}>{tag}</div>) : <div>-</div>}
                        </span>
                        <span className="row-actions">
                          <button
                            className="icon-btn table-action-btn"
                            onClick={(e) => { e.stopPropagation(); openEditGatewayConfig(g); }}
                            disabled={!canEditPage("gateway_configuration")}
                            title="Edit gateway"
                          >
                            <EditIcon />
                          </button>
                          <button className="icon-btn table-action-btn" onClick={(e) => { e.stopPropagation(); exportGatewayConfig(g); }} title="Export TXT">
                            <SaveIcon />
                          </button>
                          <button
                            className={`icon-btn table-action-btn ${isGatewayRunning(g) ? "icon-btn-stop" : "icon-btn-start"}`}
                            onClick={(e) => { e.stopPropagation(); toggleGatewayProfile(g); }}
                            disabled={!canControlGateways}
                            title={isGatewayRunning(g) ? "Stop gateway" : "Start gateway"}
                          >
                            {isGatewayRunning(g) ? <StopIcon /> : <StartIcon />}
                          </button>
                          <button className="icon-btn table-action-btn danger" onClick={(e) => { e.stopPropagation(); removeGatewayConfigProfile(g.id); }} disabled={!canDeleteRecords} title="Delete gateway">
                            <DeleteIcon />
                          </button>
                        </span>
                      </div>
                    );
                  })}
                </div>
              </section>
            </>
          ) : null}

          {activePage === "database" ? (
            <>
              <section className="card db-simple-card">
                <div className="db-simple-head">
                  <div className="db-head-title-wrap">
                    <h3 style={{ margin: 0 }}>Local Data</h3>
                    <span className="status-pill status-online">SQLite Active</span>
                  </div>
                  <div className="db-card-top-actions">
                    <button
                      className="btn btn-primary btn-sm icon-text-btn"
                      onClick={() =>
                        openAddDbConnection("app", {
                          name: "Local SQLite",
                          engine: "sqlite",
                          sqlite_path: "./data/trustnode_edge.db",
                          table: "plc_readings",
                          enabled: true,
                          use_gateway: true,
                          use_app: true,
                          use_backup: false,
                          cloud_sync_enabled: false,
                        })
                      }
                      disabled={!isAdminDatabaseUser}
                    >
                      <AddIcon />
                      <span>Add</span>
                    </button>
                    <button className="btn btn-primary btn-sm" onClick={() => refreshDatabaseOverviewCards("Local Data")}>
                      Load
                    </button>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => {
                        if (!selectedLocalDbId) {
                          setDatabaseOverviewResult("Select a local database row to remove.");
                          return;
                        }
                        if (selectedLocalDbIsMain) {
                          setDatabaseOverviewResult("Main local SQLite cannot be removed.");
                          return;
                        }
                        removeDbConnection(selectedLocalDbId);
                      }}
                      disabled={!isAdminDatabaseUser || !selectedLocalDbId || selectedLocalDbIsMain}
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div className="table db-table local-data-table">
                  <div className="thead"><span>Name</span><span>Engine</span><span>Path / Endpoint</span><span>Retention</span><span>Size / Usage</span><span>Status</span><span>Last Check</span><span>Actions</span></div>
                  {localDatabaseRows.map((c) => (
                    <div
                      key={`local-db-${c.id}`}
                      className={`trow ${String(selectedLocalDbId || "") === String(c.id || "") ? "selected-row" : ""}`}
                      onClick={() => setSelectedLocalDbId(String(c.id || ""))}
                    >
                      <span className="db-cell">{c.name}</span>
                      <span className="db-cell">{String(c.engine || "").toUpperCase()}</span>
                      <span className="db-cell db-url-cell" title={getDbEndpointLabel(c)}>{getDbEndpointLabel(c)}</span>
                      <span className="db-cell">{`Last ${retentionPresetKey} | ${Number(retentionPolicy.schedule_minutes || 60)}m`}</span>
                      <span className="db-cell">{`${formatBytes(localDbUsage.sizeBytes)} | ${localDbUsage.label} ${localDbUsage.percent}%`}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.connection_ok ? "status-online" : "status-offline"}`}>
                          {c.connection_ok ? "ONLINE" : "OFFLINE"}
                        </span>
                      </span>
                      <span className="db-cell db-last-check-cell" title={c.last_check_utc || ""}>{getDbSyncLastCheckLabel(c)}</span>
                          <span className="row-actions db-actions-cell">
                            <button className="icon-btn table-action-btn" onClick={(e) => { e.stopPropagation(); openEditDbConnection(c); }} disabled={!isAdminDatabaseUser} title="Edit local DB"><EditIcon /></button>
                            <button
                              className="icon-btn table-action-btn danger"
                              onClick={(e) => { e.stopPropagation(); removeDbConnection(c.id); }}
                              disabled={
                                !isAdminDatabaseUser ||
                                String(c.id || "") === MAIN_LOCAL_SQLITE_FALLBACK_ID ||
                                String(c.id || "") === "local-sqlite-default"
                              }
                              title="Delete local DB"
                            >
                              <DeleteIcon />
                            </button>
                          </span>
                    </div>
                  ))}
                  {!localDatabaseRows.length ? (
                    <div className="trow"><span className="db-cell">-</span><span className="db-cell">No local databases configured</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span></div>
                  ) : null}
                </div>
              </section>

              <section className="card db-simple-card">
                <div className="db-simple-head">
                  <div className="db-head-title-wrap">
                    <h3 style={{ margin: 0 }}>Trustnode Cloud</h3>
                    <label className="remember-row db-inline-toggle">
                      <input
                        type="checkbox"
                        checked={trustnodeCloudEnabled}
                        onChange={(e) => applyTrustnodeCloudToggle(e.target.checked)}
                        disabled={!isAdminDatabaseUser || !cloudDbRows.length}
                      />
                      <span className="remember-label">Enabled</span>
                    </label>
                  </div>
                  <div className="db-card-top-actions">
                    <button className="btn btn-primary btn-sm icon-text-btn" onClick={openCloudDbPicker} disabled={!isAdminDatabaseUser}>
                      <AddIcon />
                      <span>Add</span>
                    </button>
                    <button className="btn btn-primary btn-sm" onClick={() => refreshDatabaseOverviewCards("Trustnode Cloud")}>
                      Load
                    </button>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => {
                        if (!selectedCloudDbId) {
                          setDatabaseOverviewResult("Select a cloud database row to remove.");
                          return;
                        }
                        removeDbConnection(selectedCloudDbId);
                      }}
                      disabled={!isAdminDatabaseUser || !selectedCloudDbId}
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div className="table db-table cloud-data-table">
                  <div className="thead"><span>Name</span><span>Type</span><span>Endpoint</span><span>Sync</span><span>Writing</span><span>Status</span><span>Last Check</span><span>Actions</span></div>
                  {cloudDbRows.map((c) => (
                    <div
                      key={`cloud-db-${c.id}`}
                      className={`trow ${String(selectedCloudDbId || "") === String(c.id || "") ? "selected-row" : ""}`}
                      onClick={() => {
                        setSelectedCloudDbId(String(c.id || ""));
                        if (String(c.engine || "").toLowerCase() === "postgresql") {
                          setCloudProviderDbId(String(c.id || ""));
                        }
                      }}
                    >
                      <span className="db-cell">{c.name}</span>
                      <span className="db-cell">{String(c.engine || "").toUpperCase()}</span>
                      <span className="db-cell db-url-cell" title={getDbEndpointLabel(c)}>{getDbEndpointLabel(c)}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.cloud_sync_enabled ? "status-online" : "status-offline"}`}>
                          {c.cloud_sync_enabled ? "ENABLED" : "DISABLED"}
                        </span>
                      </span>
                      <span className="db-cell db-writing-cell" title={getDbSyncWritingTooltip(c)}>{getDbSyncWritingLabel(c)}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.connection_ok ? "status-online" : "status-offline"}`}>
                          {c.connection_ok ? "ONLINE" : "OFFLINE"}
                        </span>
                      </span>
                      <span className="db-cell db-last-check-cell" title={c.last_check_utc || ""}>{getDbSyncLastCheckLabel(c)}</span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={(e) => { e.stopPropagation(); openEditDbConnection(c); }} disabled={!isAdminDatabaseUser} title="Edit cloud DB"><EditIcon /></button>
                        <button className="icon-btn table-action-btn danger" onClick={(e) => { e.stopPropagation(); removeDbConnection(c.id); }} disabled={!isAdminDatabaseUser} title="Delete cloud DB"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!cloudDbRows.length ? (
                    <div className="trow"><span className="db-cell">-</span><span className="db-cell">No cloud databases configured</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span></div>
                  ) : null}
                </div>
                <div className="db-card-bottom-actions">
                  <label>
                    Cloud API URL
                    <input
                      placeholder="https://trustnode.lsapps.app"
                      value={cloudUrl}
                      onChange={(e) => setCloudUrl(e.target.value)}
                      disabled={!isAdminDatabaseUser}
                    />
                  </label>
                  <label>
                    Primary Cloud Database
                    <select
                      value={cloudProviderDbId}
                      onChange={(e) => setCloudProviderDbId(e.target.value)}
                      disabled={!isAdminDatabaseUser || !cloudProviderCandidates.length}
                    >
                      {!cloudProviderCandidates.length ? <option value="">No PostgreSQL cloud DB found</option> : null}
                      {cloudProviderCandidates.map((db) => (
                        <option key={`cloud-db-${db.id}`} value={db.id}>
                          {db.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="db-simple-actions db-actions-row-end">
                    <label className="remember-row db-inline-toggle">
                      <input
                        type="checkbox"
                        checked={cloudAutoSyncEnabled}
                        onChange={(e) => setCloudAutoSyncEnabled(e.target.checked)}
                        disabled={!isAdminDatabaseUser}
                      />
                      <span className="remember-label">Auto Sync</span>
                    </label>
                    <button className="btn btn-primary btn-sm" onClick={openCloudSyncModal} disabled={!isAdminDatabaseUser || forceSyncBusy}>
                      Sync Period...
                    </button>
                    <button className="btn btn-success btn-sm" onClick={runForceCloudSyncNow} disabled={!isAdminDatabaseUser || forceSyncBusy}>
                      {forceSyncBusy ? "Syncing..." : "Force Sync"}
                    </button>
                    <button className="btn btn-danger btn-sm" onClick={runResetLocalAndSync} disabled={!isAdminDatabaseUser || forceSyncBusy}>
                      Reset Local + Sync
                    </button>
                    <button
                      className="btn btn-primary btn-sm"
                      onClick={() => {
                        const selected = cloudDbRows.find((db) => String(db.id || "") === String(selectedCloudDbId || ""));
                        if (!selected) {
                          setDatabaseOverviewResult("Select a cloud row to edit.");
                          return;
                        }
                        openEditDbConnection(selected);
                      }}
                      disabled={!isAdminDatabaseUser || !selectedCloudDbId}
                    >
                      Edit Selected
                    </button>
                  </div>
                </div>
                <label className="remember-row" style={{ marginTop: 2 }}>
                  <input
                    type="checkbox"
                    checked={dolibarrMirrorEnabled}
                    onChange={(e) => setDolibarrMirrorEnabled(e.target.checked)}
                    disabled={!isAdminDatabaseUser || !dolibarrCandidates.length}
                  />
                  <span className="remember-label">Enable Dolibarr mirror output</span>
                </label>
                {databaseInspector?.data_sync?.last_data_error ? (
                  <div className="error" style={{ marginTop: 8 }}>
                    {String(databaseInspector.data_sync.last_data_error)}
                  </div>
                ) : null}
                {forceSyncResult ? <div className="info-note" style={{ marginTop: 8 }}>{forceSyncResult}</div> : null}
              </section>

              <section className="card db-simple-card">
                <div className="db-simple-head">
                  <div className="db-head-title-wrap">
                    <h3 style={{ margin: 0 }}>Other Databases</h3>
                    <span className="status-pill status-online">Parallel Logging</span>
                  </div>
                  <div className="db-card-top-actions">
                    <button className="btn btn-primary btn-sm icon-text-btn" onClick={openOtherDbPicker} disabled={!isAdminDatabaseUser}>
                      <AddIcon />
                      <span>Add</span>
                    </button>
                    <button className="btn btn-primary btn-sm" onClick={() => refreshDatabaseOverviewCards("Other Databases")}>
                      Load
                    </button>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => {
                        if (!selectedOtherDbId) {
                          setDatabaseOverviewResult("Select an 'Other Databases' row to remove.");
                          return;
                        }
                        removeDbConnection(selectedOtherDbId);
                      }}
                      disabled={!isAdminDatabaseUser || !selectedOtherDbId}
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div className="table db-table">
                  <div className="thead"><span>Name</span><span>Engine</span><span>Endpoint</span><span>Role</span><span>Status</span><span>Last Check</span><span>Actions</span></div>
                  {otherDatabaseRows.map((c) => (
                    <div
                      key={`other-db-${c.id}`}
                      className={`trow ${String(selectedOtherDbId || "") === String(c.id || "") ? "selected-row" : ""}`}
                      onClick={() => setSelectedOtherDbId(String(c.id || ""))}
                    >
                      <span className="db-cell">{c.name}</span>
                      <span className="db-cell">{String(c.engine || "").toUpperCase()}</span>
                      <span className="db-cell db-url-cell" title={getDbEndpointLabel(c)}>{getDbEndpointLabel(c)}</span>
                      <span className="db-cell">{getDbRoleLabel(c)}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.connection_ok ? "status-online" : "status-offline"}`}>
                          {c.connection_ok ? "ONLINE" : "OFFLINE"}
                        </span>
                      </span>
                      <span className="db-cell db-last-check-cell" title={c.last_check_utc || ""}>{getDbSyncLastCheckLabel(c)}</span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={(e) => { e.stopPropagation(); openEditDbConnection(c); }} disabled={!isAdminDatabaseUser} title="Edit DB"><EditIcon /></button>
                        <button className="icon-btn table-action-btn danger" onClick={(e) => { e.stopPropagation(); removeDbConnection(c.id); }} disabled={!isAdminDatabaseUser} title="Delete DB"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!otherDatabaseRows.length ? (
                    <div className="trow"><span className="db-cell">-</span><span className="db-cell">No other databases configured</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span></div>
                  ) : null}
                </div>
              </section>

              {databaseOverviewResult ? <section className="card"><div className="info-note">{databaseOverviewResult}</div></section> : null}
              {showDefaultLocalDbBadge ? (
                <section className="card">
                  <div className="info-note" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
                    <span>Auto-created default local DB: <b>Local SQLite</b>. It is ready for gateway selection.</span>
                    <button className="btn btn-primary btn-sm" onClick={dismissDefaultLocalDbBadge}>Dismiss</button>
                  </div>
                </section>
              ) : null}
            </>
          ) : null}
          {activePage === "database_overview" ? (
            <>
              <section className="page-tools">
                <button
                  className="btn btn-primary icon-text-btn"
                  onClick={runDatabaseOverviewRecovery}
                  disabled={!canEditPage("database_overview")}
                >
                  <span>Run Recovery Check</span>
                </button>
              </section>
              <section className="card">
                <div className="table db-overview-table">
                  <div className="thead"><span>Total</span><span>Online</span><span>Offline</span><span>Local</span><span>Cloud</span></div>
                  <div className="trow">
                    <span>{dbOverviewStats.total}</span>
                    <span>{dbOverviewStats.online}</span>
                    <span>{dbOverviewStats.offline}</span>
                    <span>{dbOverviewStats.local}</span>
                    <span>{dbOverviewStats.cloud}</span>
                  </div>
                </div>
                {databaseOverviewResult ? <div className="info-note" style={{ marginTop: 10 }}>{databaseOverviewResult}</div> : null}
                {showDefaultLocalDbBadge ? (
                  <div className="info-note" style={{ marginTop: 10, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
                    <span>Auto-created default local DB: <b>Local SQLite</b>. It is ready for gateway selection.</span>
                    <button className="btn btn-primary btn-sm" onClick={dismissDefaultLocalDbBadge}>Dismiss</button>
                  </div>
                ) : null}
              </section>
              <section className="card">
                <div className="info-note">Retention controls moved to <b>Backup and Retention</b> submenu.</div>
              </section>
              <section className="card">
                <div className="table db-table">
                  <div className="thead"><span>Name</span><span>Engine</span><span>URL</span><span>Database</span><span>Status</span><span>Writing</span><span>Last Check</span></div>
                  {dbConnectionsView.map((c) => (
                    <div key={`overview-${c.id}`} className="trow">
                      <span className="db-cell" title={c.name}>{c.name}</span>
                      <span className="db-cell">{c.engine}</span>
                      <span className="db-cell db-url-cell" title={c.engine === "legacy_http" ? c.legacy_url : c.engine === "sqlite" ? c.sqlite_path || "./data/trustnode_edge.db" : c.engine === "csv_file" || c.engine === "txt_file" ? c.file_path || "-" : `${c.host}:${c.port}`}>
                        {c.engine === "legacy_http"
                          ? c.legacy_url
                          : c.engine === "sqlite"
                            ? c.sqlite_path || "./data/trustnode_edge.db"
                            : c.engine === "csv_file" || c.engine === "txt_file"
                              ? c.file_path || "-"
                              : `${c.host}:${c.port}`}
                      </span>
                      <span className="db-cell">{c.engine === "sqlite" ? (c.table || "plc_readings") : (c.database || "-")}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.connection_ok ? "status-online" : "status-offline"}`}>
                          {c.connection_ok ? "ONLINE" : "OFFLINE"}
                        </span>
                      </span>
                      <span className="db-cell db-writing-cell" title={getDbSyncWritingTooltip(c)}>{getDbSyncWritingLabel(c)}</span>
                      <span className="db-cell">{getDbSyncLastCheckLabel(c)}</span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : null}

          {activePage === "backup_and_retention" ? (
            <>
              <section className="card">
                <div className="table db-overview-table">
                  <div className="thead"><span>Backup Targets</span><span>Snapshots</span><span>Scheduler</span><span>Retention</span><span>Last Run</span></div>
                  <div className="trow">
                    <span>{otherDatabaseRows.length}</span>
                    <span>{backupRows.length}</span>
                    <span>{retentionPolicy.enabled ? "ENABLED" : "DISABLED"}</span>
                    <span>{`Raw ${Number(retentionPolicy.raw_keep_days || 7)}d | Min ${Number(retentionPolicy.minute_keep_days || 30)}d`}</span>
                    <span>{retentionRuns?.[0]?.run_utc || "-"}</span>
                  </div>
                </div>
              </section>

              <section className="card db-simple-card">
                <div className="db-simple-head">
                  <div className="db-head-title-wrap">
                    <h3 style={{ margin: 0 }}>Backup Databases</h3>
                    <span className="status-pill status-online">Parallel Backup Targets</span>
                  </div>
                  <div className="db-card-top-actions">
                    <button className="btn btn-primary btn-sm icon-text-btn" onClick={openOtherDbPicker} disabled={!isAdminDatabaseUser}>
                      <AddIcon />
                      <span>Add</span>
                    </button>
                    <button className="btn btn-primary btn-sm" onClick={() => refreshDatabaseOverviewCards("Backup Targets")}>
                      Load
                    </button>
                    <button
                      className="btn btn-danger btn-sm"
                      onClick={() => {
                        if (!selectedOtherDbId) {
                          setBackupResult("Select a backup database row to remove.");
                          return;
                        }
                        removeDbConnection(selectedOtherDbId);
                      }}
                      disabled={!isAdminDatabaseUser || !selectedOtherDbId}
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div className="table db-table other-data-table">
                  <div className="thead"><span>Name</span><span>Engine</span><span>Endpoint</span><span>Role</span><span>Enabled</span><span>Status</span><span>Last Check</span><span>Actions</span></div>
                  {otherDatabaseRows.map((c) => (
                    <div
                      key={`backup-target-${c.id}`}
                      className={`trow ${String(selectedOtherDbId || "") === String(c.id || "") ? "selected-row" : ""}`}
                      onClick={() => setSelectedOtherDbId(String(c.id || ""))}
                    >
                      <span className="db-cell">{c.name}</span>
                      <span className="db-cell">{String(c.engine || "").toUpperCase()}</span>
                      <span className="db-cell db-url-cell" title={getDbEndpointLabel(c)}>{getDbEndpointLabel(c)}</span>
                      <span className="db-cell">{getDbRoleLabel(c)}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.enabled !== false ? "status-online" : "status-offline"}`}>
                          {c.enabled !== false ? "ENABLED" : "DISABLED"}
                        </span>
                      </span>
                      <span className="db-cell">
                        <span className={`status-pill ${c.connection_ok ? "status-online" : "status-offline"}`}>
                          {c.connection_ok ? "ONLINE" : "OFFLINE"}
                        </span>
                      </span>
                      <span className="db-cell db-last-check-cell" title={c.last_check_utc || ""}>{getDbSyncLastCheckLabel(c)}</span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={(e) => { e.stopPropagation(); openEditDbConnection(c); }} disabled={!isAdminDatabaseUser} title="Edit backup DB"><EditIcon /></button>
                        <button className="icon-btn table-action-btn danger" onClick={(e) => { e.stopPropagation(); removeDbConnection(c.id); }} disabled={!isAdminDatabaseUser} title="Delete backup DB"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!otherDatabaseRows.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span><span className="db-cell">No backup databases configured</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span>
                    </div>
                  ) : null}
                </div>
                <div className="db-simple-actions db-actions-row-end">
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => {
                      const selected = otherDatabaseRows.find((db) => String(db.id || "") === String(selectedOtherDbId || ""));
                      if (!selected) {
                        setBackupResult("Select a backup database row to edit.");
                        return;
                      }
                      openEditDbConnection(selected);
                    }}
                    disabled={!isAdminDatabaseUser || !selectedOtherDbId}
                  >
                    Edit Selected
                  </button>
                  <button className="btn btn-primary btn-sm" onClick={() => runProvisionProfile("backup")} disabled={!isAdminDatabaseUser}>
                    Provision Template
                  </button>
                </div>
              </section>

              <section className="card">
                <div className="row backup-card-header">
                  <h3 className="card-title">Snapshot Backups</h3>
                  <div className="row">
                    <button className="btn btn-primary icon-text-btn" onClick={runCreateBackup} disabled={!canEditPage("backup_and_retention") || backupBusy}>
                      <AddIcon />
                      <span>{backupBusy ? "Working..." : "Create Snapshot"}</span>
                    </button>
                    <button
                      className="btn btn-success"
                      onClick={() => runRestoreBackup(selectedBackupFilename)}
                      disabled={!canEditPage("backup_and_retention") || backupBusy || !selectedBackupFilename}
                    >
                      Restore Selected
                    </button>
                  </div>
                </div>
                <div className="table backup-files-table">
                  <div className="thead"><span>Select</span><span>Created (UTC)</span><span>File</span><span>Size</span><span>Actions</span></div>
                  {backupRows.map((b) => (
                    <div key={`bk-${b.filename}`} className="trow">
                      <span className="db-cell">
                        <input
                          type="radio"
                          name="backup-selected"
                          checked={selectedBackupFilename === b.filename}
                          onChange={() => setSelectedBackupFilename(b.filename)}
                        />
                      </span>
                      <span className="db-cell">{b.modified_utc || "-"}</span>
                      <span className="db-cell" title={b.path || b.filename}>{b.filename}</span>
                      <span className="db-cell">{Number(b.size_bytes || 0).toLocaleString()} bytes</span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={() => runDownloadBackup(b)} title="Download backup"><CsvIcon /></button>
                        <button className="icon-btn table-action-btn danger" onClick={() => runDeleteBackup(b.filename)} disabled={!canEditPage("backup_and_retention") || backupBusy} title="Delete backup"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!backupRows.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">No backups created yet</span><span className="db-cell">-</span><span className="db-cell">-</span>
                    </div>
                  ) : null}
                </div>
                {backupResult ? <div className={backupResult.toLowerCase().includes("failed") ? "error" : "info-note"} style={{ marginTop: 10 }}>{backupResult}</div> : null}
              </section>

              <section className="card">
                <div className="row backup-card-header">
                  <h3 className="card-title">Retention and Cleanup Policy</h3>
                  <div className="row">
                    <button className="btn btn-primary" onClick={saveRetentionPolicy} disabled={!canEditPage("backup_and_retention") || retentionBusy}>Save Policy</button>
                    <button className="btn btn-success" onClick={() => executeRetentionRun(true)} disabled={!canEditPage("backup_and_retention") || retentionBusy}>Dry Run</button>
                    <button className="btn btn-danger" onClick={() => executeRetentionRun(false)} disabled={!canEditPage("backup_and_retention") || retentionBusy}>Run Cleanup</button>
                  </div>
                </div>
                <div className="retention-policy-grid">
                  <label className="remember-row">
                    <input type="checkbox" checked={Boolean(retentionPolicy.enabled)} onChange={(e) => setRetentionPolicy((p) => ({ ...p, enabled: e.target.checked }))} disabled={!canEditPage("backup_and_retention")} />
                    <span className="remember-label">Enable scheduler</span>
                  </label>
                  <label>
                    Keep PLC Tag Data
                    <select value={retentionPresetKey} onChange={(e) => applyRetentionPreset(e.target.value)} disabled={!canEditPage("backup_and_retention")}>
                      <option value="day">Last day</option>
                      <option value="week">Last week</option>
                      <option value="month">Last month</option>
                    </select>
                  </label>
                  <label>Schedule (minutes)<input type="number" min="5" value={retentionPolicy.schedule_minutes} onChange={(e) => setRetentionPolicy((p) => ({ ...p, schedule_minutes: Number(e.target.value || 60) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label>Keep Raw (days)<input type="number" min="1" value={retentionPolicy.raw_keep_days} onChange={(e) => setRetentionPolicy((p) => ({ ...p, raw_keep_days: Number(e.target.value || 7) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label>Keep Minute (days)<input type="number" min="1" value={retentionPolicy.minute_keep_days} onChange={(e) => setRetentionPolicy((p) => ({ ...p, minute_keep_days: Number(e.target.value || 30) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label>Keep Hour (days)<input type="number" min="1" value={retentionPolicy.hour_keep_days} onChange={(e) => setRetentionPolicy((p) => ({ ...p, hour_keep_days: Number(e.target.value || 180) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label>Keep Day (days)<input type="number" min="1" value={retentionPolicy.day_keep_days} onChange={(e) => setRetentionPolicy((p) => ({ ...p, day_keep_days: Number(e.target.value || 730) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label>Max Deletes / Run<input type="number" min="1000" step="1000" value={retentionPolicy.max_delete_rows_per_run} onChange={(e) => setRetentionPolicy((p) => ({ ...p, max_delete_rows_per_run: Number(e.target.value || 50000) }))} disabled={!canEditPage("backup_and_retention")} /></label>
                  <label className="remember-row">
                    <input type="checkbox" checked={Boolean(retentionPolicy.backup_before_cleanup)} onChange={(e) => setRetentionPolicy((p) => ({ ...p, backup_before_cleanup: e.target.checked }))} disabled={!canEditPage("backup_and_retention")} />
                    <span className="remember-label">Backup before cleanup</span>
                  </label>
                </div>
                {retentionResult && retentionResultScope === "global" ? <div className={retentionResult.toLowerCase().includes("failed") ? "error" : "info-note"} style={{ marginTop: 10 }}>{retentionResult}</div> : null}
                <div className="table retention-runs-table" style={{ marginTop: 12 }}>
                  <div className="thead"><span>Run UTC</span><span>Mode</span><span>Status</span><span>Delete Candidates</span><span>Backup</span></div>
                  {retentionRuns.map((r) => {
                    const d = r?.details?.deletes || {};
                    const backupText = r?.details?.backup_path ? "YES" : (r?.details?.backup_error ? "ERROR" : "-");
                    return (
                      <div key={`retention-run-backup-page-${r.id}`} className="trow">
                        <span className="db-cell">{r.run_utc || "-"}</span>
                        <span className="db-cell">{r.dry_run ? "DRY" : "EXECUTE"}</span>
                        <span className="db-cell">{String(r.status || "").toUpperCase()}</span>
                        <span className="db-cell">{`raw:${d.raw_candidates ?? 0} min:${d.minute_candidates ?? 0} hr:${d.hour_candidates ?? 0} day:${d.day_candidates ?? 0}`}</span>
                        <span className="db-cell">{backupText}</span>
                      </div>
                    );
                  })}
                  {!retentionRuns.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">No retention runs yet</span><span className="db-cell">-</span>
                    </div>
                  ) : null}
                </div>
              </section>
              <section className="card">
                <h3 className="card-title">Clean Data</h3>
                <div className="cleanup-card-row">
                  <label className="cleanup-mode-field">
                    Cleanup Scope
                    <select value={cleanupMode} onChange={(e) => setCleanupMode(e.target.value)} disabled={currentUser?.role !== "admin" || cleanupBusy}>
                      <option value="period">Period</option>
                      <option value="last_hours">Last Hour</option>
                      <option value="last_day">Last Day</option>
                      <option value="last_week">Last Week</option>
                      <option value="last_month">Last Month</option>
                      <option value="all">All</option>
                    </select>
                  </label>
                  <button className="btn btn-danger" onClick={runCleanupData} disabled={currentUser?.role !== "admin" || cleanupBusy}>
                    {cleanupBusy ? "Cleaning..." : "Clean Data"}
                  </button>
                </div>
                {cleanupResult && cleanupResultScope === "global" ? <div className={cleanupResult.toLowerCase().includes("failed") ? "error" : "info-note"} style={{ marginTop: 10 }}>{cleanupResult}</div> : null}
              </section>
            </>
          ) : null}

          {activePage === "database_inspector" ? (
            <>
              <section className="page-tools">
                <button
                  className="btn btn-primary icon-text-btn"
                  onClick={refreshDatabaseInspector}
                  disabled={databaseInspectorBusy}
                >
                  <span>{databaseInspectorBusy ? "Refreshing..." : "Refresh Inspector"}</span>
                </button>
              </section>
              <section className="card">
                <div className="table db-overview-table">
                  <div className="thead"><span>Local DB Path</span><span>Exists</span><span>Size</span><span>Tables</span><span>Config Domains</span></div>
                  <div className="trow">
                    <span className="db-cell" title={databaseInspector?.db_path || "-"}>{databaseInspector?.db_path || "-"}</span>
                    <span className="db-cell">
                      <span className={`status-pill ${databaseInspector?.db_exists ? "status-online" : "status-offline"}`}>
                        {databaseInspector?.db_exists ? "YES" : "NO"}
                      </span>
                    </span>
                    <span className="db-cell">{Number(databaseInspector?.db_size_bytes || 0).toLocaleString()} bytes</span>
                    <span className="db-cell">{databaseInspector?.table_count ?? 0}</span>
                    <span className="db-cell">{Array.isArray(databaseInspector?.config_domains_preview) ? databaseInspector.config_domains_preview.length : 0}</span>
                  </div>
                </div>
                {databaseInspectorError ? <div className="error" style={{ marginTop: 10 }}>{databaseInspectorError}</div> : null}
              </section>
              <section className="card">
                <div className="table db-overview-table">
                  <div className="thead"><span>Cloud Target</span><span>Host</span><span>Database</span><span>Schema</span><span>TLS</span></div>
                  <div className="trow">
                    <span className="db-cell">{databaseInspector?.cloud_target?.name || "-"}</span>
                    <span className="db-cell">{databaseInspector?.cloud_target?.host || "-"}</span>
                    <span className="db-cell">{databaseInspector?.cloud_target?.database || "-"}</span>
                    <span className="db-cell">{databaseInspector?.cloud_target?.schema || "-"}</span>
                    <span className="db-cell">{databaseInspector?.cloud_target ? (databaseInspector.cloud_target.tls ? "ON" : "OFF") : "-"}</span>
                  </div>
                </div>
                <div className="table db-overview-table" style={{ marginTop: 12 }}>
                  <div className="thead"><span>Sync Enabled</span><span>Pending</span><span>Failed</span><span>Sent</span><span>Last Sync / Error</span></div>
                  <div className="trow">
                    <span className="db-cell">
                      <span className={`status-pill ${databaseInspector?.sync_target?.enabled ? "status-online" : "status-offline"}`}>
                        {databaseInspector?.sync_target?.enabled ? "ENABLED" : "DISABLED"}
                      </span>
                    </span>
                    <span className="db-cell">{databaseInspector?.sync_outbox_status?.pending ?? 0}</span>
                    <span className="db-cell">{databaseInspector?.sync_outbox_status?.failed ?? 0}</span>
                    <span className="db-cell">{databaseInspector?.sync_outbox_status?.sent ?? 0}</span>
                    <span className="db-cell">
                      {databaseInspector?.sync_target?.last_sync_utc
                        ? `Last sync: ${databaseInspector.sync_target.last_sync_utc}`
                        : (databaseInspector?.sync_target?.last_error || "-")}
                    </span>
                  </div>
                </div>
              </section>
              <section className="card">
                <h3>Local Tables</h3>
                <div className="table db-table">
                  <div className="thead"><span>Table</span><span>Rows</span></div>
                  {(databaseInspector?.tables || []).map((t) => (
                    <div key={`tbl-${t.name}`} className="trow">
                      <span className="db-cell">{t.name}</span>
                      <span className="db-cell">{t.rows}</span>
                    </div>
                  ))}
                  {!databaseInspector?.tables?.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span>
                      <span className="db-cell">No table data</span>
                    </div>
                  ) : null}
                </div>
              </section>
              <section className="card">
                <h3>Config Domains (Latest)</h3>
                <div className="table db-table">
                  <div className="thead"><span>Domain</span><span>Version</span><span>Updated UTC</span></div>
                  {(databaseInspector?.config_domains_preview || []).map((d) => (
                    <div key={`dom-${d.domain}`} className="trow">
                      <span className="db-cell">{d.domain}</span>
                      <span className="db-cell">{d.version}</span>
                      <span className="db-cell">{d.updated_utc}</span>
                    </div>
                  ))}
                  {!databaseInspector?.config_domains_preview?.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span>
                      <span className="db-cell">-</span>
                      <span className="db-cell">No domains yet</span>
                    </div>
                  ) : null}
                </div>
              </section>
            </>
          ) : null}

          {activePage === "website_and_env" ? (
            <>
              <section className="card">
                <div className="form-grid">
                  <label>
                    API Mode
                    <select
                      value={endpointMode}
                      onChange={(e) => setEndpointMode(e.target.value === "cloud" ? "cloud" : "local")}
                      disabled={!canEditPage("website_and_env")}
                    >
                      <option value="local">Local Edge API</option>
                      <option value="cloud">Cloud API</option>
                    </select>
                  </label>
                  <label>
                    Cloud API URL
                    <input
                      value={cloudUrl}
                      placeholder="https://your-cloud-api.example.com"
                      onChange={(e) => setCloudUrl(e.target.value)}
                      disabled={!canEditPage("website_and_env")}
                    />
                  </label>
                </div>
                <div className="row">
                  <button className="btn btn-primary" onClick={onApplyEndpoint} disabled={!canEditPage("website_and_env")}>
                    Apply API Target
                  </button>
                  <button className="btn btn-success" onClick={runWebsiteStatusCheck}>
                    Check Website Status
                  </button>
                </div>
                {websiteStatusResult ? (
                  <div className={websiteStatusResult.includes("failed") ? "error" : "info-note"} style={{ marginTop: 10 }}>
                    {websiteStatusResult}
                  </div>
                ) : null}
              </section>
              <section className="card">
                <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <h4 className="card-title" style={{ margin: 0 }}>Saved Email Profiles</h4>
                  <div className="row">
                    <input
                      placeholder="Profile name"
                      value={emailProfileName}
                      onChange={(e) => setEmailProfileName(e.target.value)}
                      style={{ minWidth: 180 }}
                    />
                    <button className="btn btn-primary" onClick={saveCurrentEmailProfile}>Save Current as Profile</button>
                  </div>
                </div>
                <div className="table backup-files-table">
                  <div className="thead"><span>Name</span><span>Created</span><span>Status</span><span>Actions</span></div>
                  {(emailProfiles || []).map((p) => (
                    <div key={p.id} className="trow">
                      <span className="db-cell">{p.name}</span>
                      <span className="db-cell">{p.created_utc || "-"}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${p.enabled !== false ? "status-online" : "status-offline"}`}>
                          {p.enabled !== false ? (p.id === activeEmailProfileId ? "ACTIVE" : "ENABLED") : "DISABLED"}
                        </span>
                      </span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={() => activateEmailProfile(p.id)} title="Activate profile"><SaveIcon /></button>
                        <button
                          className={`icon-btn table-action-btn ${p.enabled !== false ? "icon-btn-stop" : "icon-btn-start"}`}
                          onClick={() => toggleEmailProfileEnabled(p.id, p.enabled === false)}
                          title={p.enabled !== false ? "Disable profile" : "Enable profile"}
                        >
                          {p.enabled !== false ? <StopIcon /> : <StartIcon />}
                        </button>
                        <button className="icon-btn table-action-btn danger" onClick={() => removeEmailProfile(p.id)} title="Delete profile"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!emailProfiles.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span><span className="db-cell">No profiles saved yet</span><span className="db-cell">-</span><span className="db-cell">-</span>
                    </div>
                  ) : null}
                </div>
              </section>
              <section className="card">
                <div className="form-grid">
                  <label>
                    Frontend Source Mode
                    <select
                      value={uiSourceMode}
                      onChange={(e) => setUiSourceMode(e.target.value)}
                      disabled={!canEditPage("website_and_env")}
                    >
                      <option value="local">Local bundled frontend</option>
                      <option value="remote">Remote hosted frontend</option>
                      <option value="external">External local folder</option>
                    </select>
                  </label>
                  {uiSourceMode === "remote" ? (
                    <label>
                      Remote Frontend URL
                      <input
                        placeholder="https://your-frontend-domain.example.com"
                        value={uiSourceRemoteUrl}
                        onChange={(e) => setUiSourceRemoteUrl(e.target.value)}
                        disabled={!canEditPage("website_and_env")}
                      />
                    </label>
                  ) : null}
                  {uiSourceMode === "external" ? (
                    <label>
                      Local Frontend Folder
                      <input
                        placeholder="C:\\Trustnode\\frontend-dist"
                        value={uiSourceLocalPath}
                        onChange={(e) => setUiSourceLocalPath(e.target.value)}
                        disabled={!canEditPage("website_and_env")}
                      />
                    </label>
                  ) : null}
                </div>
                <div className="row">
                  <button className="btn btn-primary" onClick={saveUiSource} disabled={!canEditPage("website_and_env")}>
                    Save Frontend Source
                  </button>
                  {uiSourceMode === "remote" ? (
                    <button
                      className="btn btn-success"
                      onClick={runUiSourceTest}
                      disabled={!canEditPage("website_and_env") || !uiSourceRemoteUrl.trim()}
                    >
                      Test Remote URL
                    </button>
                  ) : null}
                </div>
                {uiSourceSavedMessage ? <div className="info-note">{uiSourceSavedMessage}</div> : null}
                {uiSourceTestResult ? (
                  <div className={uiSourceTestResult.startsWith("PASS") ? "info-note" : "error"}>
                    {uiSourceTestResult}
                  </div>
                ) : null}
              </section>
              <section className="card">
                <label>
                  Website/Deploy Env Vars (`KEY=VALUE`, one per line)
                  <textarea
                    rows={8}
                    value={websiteEnvText}
                    onChange={(e) => setWebsiteEnvText(e.target.value)}
                    placeholder={"TRUSTNODE_CLOUD_API_URL=https://api.example.com\nTRUSTNODE_UI_SOURCE_MODE=remote\nTRUSTNODE_UI_SOURCE_REMOTE_URL=https://app.example.com/trustnode"}
                    disabled={!canEditPage("website_and_env")}
                  />
                </label>
                <div className="row">
                  <button className="btn btn-primary" onClick={applyWebsiteEnvToUi} disabled={!canEditPage("website_and_env")}>
                    Apply Env to UI
                  </button>
                </div>
                <div className="lock-note">
                  Saved env values are part of app configuration and can be used for hosted website deployment reference.
                </div>
              </section>
            </>
          ) : null}

          {activePage === "email_and_notifications" ? (
            <>
              <section className="card">
                <div className="form-grid">
                  <label>
                    Provider
                    <select
                      value={emailSettings.transport || "smtp"}
                      onChange={(e) => setEmailSettings((p) => ({ ...p, transport: e.target.value === "php_http" ? "php_http" : "smtp" }))}
                    >
                      <option value="smtp">SMTP</option>
                      <option value="php_http">Dolibarr PHP API (Token Header)</option>
                    </select>
                  </label>
                </div>
                <div className="form-grid">
                  {emailSettings.transport === "php_http" ? (
                    <>
                      <label>
                        PHP Endpoint URL
                        <input value={emailSettings.php_endpoint_url} onChange={(e) => setEmailSettings((p) => ({ ...p, php_endpoint_url: e.target.value }))} placeholder="https://yourdomain.com/custom/api_send_mail.php" />
                      </label>
                      <label>
                        API Token
                        <input type="password" value={emailSettings.php_api_token} onChange={(e) => setEmailSettings((p) => ({ ...p, php_api_token: e.target.value }))} />
                      </label>
                      <label>
                        Auth Header
                        <input value={emailSettings.php_auth_header} onChange={(e) => setEmailSettings((p) => ({ ...p, php_auth_header: e.target.value }))} placeholder="X-API-TOKEN" />
                      </label>
                      <label>
                        Timeout (ms)
                        <input type="number" value={emailSettings.php_timeout_ms} onChange={(e) => setEmailSettings((p) => ({ ...p, php_timeout_ms: Number(e.target.value || 6000) }))} />
                      </label>
                      <div className="form-note">
                        Payload format: <code>to</code>, <code>subject</code>, <code>body</code>, <code>is_html</code>, <code>from_name</code>, <code>from_email</code> with token in <code>{emailSettings.php_auth_header || "X-API-TOKEN"}</code>.
                      </div>
                    </>
                  ) : (
                    <>
                      <label>
                        SMTP Host
                        <input value={emailSettings.host} onChange={(e) => setEmailSettings((p) => ({ ...p, host: e.target.value }))} />
                      </label>
                      <label>
                        SMTP Port
                        <input type="number" value={emailSettings.port} onChange={(e) => setEmailSettings((p) => ({ ...p, port: Number(e.target.value || 587) }))} />
                      </label>
                      <label>
                        Username
                        <input value={emailSettings.username} onChange={(e) => setEmailSettings((p) => ({ ...p, username: e.target.value }))} />
                      </label>
                      <label>
                        Password
                        <input type="password" value={emailSettings.password} onChange={(e) => setEmailSettings((p) => ({ ...p, password: e.target.value }))} />
                      </label>
                    </>
                  )}
                  <label>
                    Sender Name
                    <input value={emailSettings.sender_name} onChange={(e) => setEmailSettings((p) => ({ ...p, sender_name: e.target.value }))} />
                  </label>
                  <label>
                    Sender Email
                    <input value={emailSettings.sender_email} onChange={(e) => setEmailSettings((p) => ({ ...p, sender_email: e.target.value }))} />
                  </label>
                </div>
                <div className="row">
                  {emailSettings.transport === "php_http" ? (
                    <label><input type="checkbox" checked={emailSettings.php_verify_tls} onChange={(e) => setEmailSettings((p) => ({ ...p, php_verify_tls: e.target.checked }))} /> Verify TLS certificate</label>
                  ) : (
                    <>
                      <label><input type="checkbox" checked={emailSettings.use_tls} onChange={(e) => setEmailSettings((p) => ({ ...p, use_tls: e.target.checked }))} /> Use TLS</label>
                      <label><input type="checkbox" checked={emailSettings.use_ssl} onChange={(e) => setEmailSettings((p) => ({ ...p, use_ssl: e.target.checked }))} /> Use SSL</label>
                    </>
                  )}
                </div>
                <div className="row" style={{ marginTop: 8 }}>
                  <input
                    placeholder="Method name (optional)"
                    value={emailProfileName}
                    onChange={(e) => setEmailProfileName(e.target.value)}
                    style={{ minWidth: 220 }}
                  />
                  <button className="btn btn-primary" onClick={saveCurrentEmailProfile}>Save Method</button>
                </div>
              </section>
              <section className="card">
                <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <h4 className="card-title" style={{ margin: 0 }}>Configured Email Methods</h4>
                  <label style={{ minWidth: 260 }}>
                    Active Method
                    <select
                      value={activeEmailProfileId || ""}
                      onChange={(e) => activateEmailProfile(e.target.value)}
                    >
                      <option value="">Current Editor Settings</option>
                      {(emailProfiles || [])
                        .filter((p) => p.enabled !== false)
                        .map((p) => (
                          <option key={`active-${p.id}`} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                    </select>
                  </label>
                </div>
                <div className="table email-profiles-table">
                  <div className="thead"><span>Name</span><span>Type</span><span>Created</span><span>Status</span><span>Actions</span></div>
                  {(emailProfiles || []).map((p) => (
                    <div key={p.id} className="trow">
                      <span className="db-cell">{p.name}</span>
                      <span className="db-cell">
                        {p?.settings?.transport === "php_http"
                          ? `PHP API${p?.settings?.php_endpoint_url ? ` | ${p.settings.php_endpoint_url}` : ""}`
                          : `SMTP${p?.settings?.host ? ` | ${p.settings.host}:${p.settings.port || 587}` : ""}`}
                      </span>
                      <span className="db-cell">{p.created_utc || "-"}</span>
                      <span className="db-cell">
                        <span className={`status-pill ${p.enabled !== false ? "status-online" : "status-offline"}`}>
                          {p.enabled !== false ? (p.id === activeEmailProfileId ? "ACTIVE" : "ENABLED") : "DISABLED"}
                        </span>
                      </span>
                      <span className="row-actions db-actions-cell">
                        <button className="icon-btn table-action-btn" onClick={() => activateEmailProfile(p.id)} title="Activate method"><SaveIcon /></button>
                        <button
                          className={`icon-btn table-action-btn ${p.enabled !== false ? "icon-btn-stop" : "icon-btn-start"}`}
                          onClick={() => toggleEmailProfileEnabled(p.id, p.enabled === false)}
                          title={p.enabled !== false ? "Disable method" : "Enable method"}
                        >
                          {p.enabled !== false ? <StopIcon /> : <StartIcon />}
                        </button>
                        <button className="icon-btn table-action-btn danger" onClick={() => removeEmailProfile(p.id)} title="Delete method"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                  {!emailProfiles.length ? (
                    <div className="trow">
                      <span className="db-cell">-</span><span className="db-cell">No methods saved yet</span><span className="db-cell">-</span><span className="db-cell">-</span><span className="db-cell">-</span>
                    </div>
                  ) : null}
                </div>
              </section>
              <section className="card">
                <div className="form-grid">
                  <label>
                    Alarm Recipients (; separated)
                    <input value={emailSettings.alarm_recipients} onChange={(e) => setEmailSettings((p) => ({ ...p, alarm_recipients: e.target.value }))} />
                  </label>
                  <label>
                    Report Recipients (; separated)
                    <input value={emailSettings.report_recipients} onChange={(e) => setEmailSettings((p) => ({ ...p, report_recipients: e.target.value }))} />
                  </label>
                  <label>
                    Batch Recipients (; separated)
                    <input value={emailSettings.batch_recipients} onChange={(e) => setEmailSettings((p) => ({ ...p, batch_recipients: e.target.value }))} />
                  </label>
                  <label>
                    Alarm Subject Template
                    <input value={emailSettings.alarm_subject} onChange={(e) => setEmailSettings((p) => ({ ...p, alarm_subject: e.target.value }))} />
                  </label>
                  <label>
                    Report Subject Template
                    <input value={emailSettings.report_subject || ""} onChange={(e) => setEmailSettings((p) => ({ ...p, report_subject: e.target.value }))} />
                  </label>
                  <label>
                    Batch Subject Template
                    <input value={emailSettings.batch_subject || ""} onChange={(e) => setEmailSettings((p) => ({ ...p, batch_subject: e.target.value }))} />
                  </label>
                </div>
                <div className="card" style={{ padding: 10 }}>
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <strong>Alarm HTML Template</strong>
                    <button
                      className="btn btn-primary btn-sm"
                      type="button"
                      onClick={() => setEmailTemplateView((p) => ({ ...p, alarm: p.alarm === "preview" ? "code" : "preview" }))}
                    >
                      {emailTemplateView.alarm === "preview" ? "Show HTML Code" : "Show Preview"}
                    </button>
                  </div>
                  {emailTemplateView.alarm === "preview" ? (
                    <div className="email-template-preview" dangerouslySetInnerHTML={{ __html: String(emailSettings.alarm_template || "") }} />
                  ) : (
                    <textarea rows={6} value={emailSettings.alarm_template} onChange={(e) => setEmailSettings((p) => ({ ...p, alarm_template: e.target.value }))} />
                  )}
                </div>
                <div className="card" style={{ padding: 10, marginTop: 8 }}>
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <strong>Report HTML Template</strong>
                    <button
                      className="btn btn-primary btn-sm"
                      type="button"
                      onClick={() => setEmailTemplateView((p) => ({ ...p, report: p.report === "preview" ? "code" : "preview" }))}
                    >
                      {emailTemplateView.report === "preview" ? "Show HTML Code" : "Show Preview"}
                    </button>
                  </div>
                  {emailTemplateView.report === "preview" ? (
                    <div className="email-template-preview" dangerouslySetInnerHTML={{ __html: String(emailSettings.report_template || "") }} />
                  ) : (
                    <textarea rows={6} value={emailSettings.report_template || ""} onChange={(e) => setEmailSettings((p) => ({ ...p, report_template: e.target.value }))} />
                  )}
                </div>
                <div className="card" style={{ padding: 10, marginTop: 8 }}>
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <strong>Batch HTML Template</strong>
                    <button
                      className="btn btn-primary btn-sm"
                      type="button"
                      onClick={() => setEmailTemplateView((p) => ({ ...p, batch: p.batch === "preview" ? "code" : "preview" }))}
                    >
                      {emailTemplateView.batch === "preview" ? "Show HTML Code" : "Show Preview"}
                    </button>
                  </div>
                  {emailTemplateView.batch === "preview" ? (
                    <div className="email-template-preview" dangerouslySetInnerHTML={{ __html: String(emailSettings.batch_template || "") }} />
                  ) : (
                    <textarea rows={6} value={emailSettings.batch_template || ""} onChange={(e) => setEmailSettings((p) => ({ ...p, batch_template: e.target.value }))} />
                  )}
                </div>
                <div className="row">
                  <input placeholder="test@domain.com" value={emailTestTo} onChange={(e) => setEmailTestTo(e.target.value)} />
                  <button className="btn btn-success" onClick={runEmailTest}>Send Test Email</button>
                </div>
                {emailResult ? (
                  <div className={emailResult.startsWith("PASS") ? "info-note" : "error"} style={{ marginTop: 8 }}>
                    {emailResult}
                  </div>
                ) : null}
              </section>
            </>
          ) : null}

          {activePage === "scheduled_reports" ? (
            <>
              <section className="page-tools">
                <button className="btn btn-primary icon-text-btn" onClick={openScheduleCreate} disabled={!canEditPage("scheduled_reports")}>
                  <AddIcon />
                  <span>Add Schedule</span>
                </button>
              </section>
              <section className="card card-fill">
                <div className="table scheduled-table">
                  <div className="thead">
                    <span>Name</span><span>Recurrence</span><span>Time</span><span>Format</span><span>Recipients</span><span>Last Run</span><span>Status</span><span>Actions</span>
                  </div>
                  {scheduledReports.map((s) => (
                    <div key={s.id} className="trow">
                      <span>{s.name}</span>
                      <span>{s.recurrence}</span>
                      <span>{String(s.hour || "00").padStart(2, "0")}:{String(s.minute || "00").padStart(2, "0")}</span>
                      <span>{String(s.format || "csv").toUpperCase()}</span>
                      <span title={s.recipients}>{s.recipients || "-"}</span>
                      <span>{s.last_run_utc || "-"}</span>
                      <span>
                        <span className={`status-pill ${s.enabled ? "status-online" : "status-offline"}`}>{s.enabled ? "ENABLED" : "DISABLED"}</span>
                      </span>
                      <span className="row-actions">
                        <button className="icon-btn table-action-btn icon-btn-start" onClick={() => runScheduledReportNow(s)} title="Run now"><StartIcon /></button>
                        <button className="icon-btn table-action-btn" onClick={() => openEditScheduledReport(s)} title="Edit"><EditIcon /></button>
                        <button className="icon-btn table-action-btn danger" onClick={() => removeScheduledReport(s.id)} title="Delete"><DeleteIcon /></button>
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : null}

          {activePage === "frontend_source" ? (
            <section className="card">
              <div className="form-grid">
                <label>
                  Source Mode
                  <select
                    value={uiSourceMode}
                    onChange={(e) => setUiSourceMode(e.target.value)}
                    disabled={!canEditPage("frontend_source")}
                  >
                    <option value="local">Local bundled frontend</option>
                    <option value="remote">Remote hosted frontend</option>
                    <option value="external">External local folder</option>
                  </select>
                </label>
                {uiSourceMode === "remote" ? (
                  <label>
                    Remote Frontend URL
                    <input
                      placeholder="https://your-frontend-domain.example.com"
                      value={uiSourceRemoteUrl}
                      onChange={(e) => setUiSourceRemoteUrl(e.target.value)}
                      disabled={!canEditPage("frontend_source")}
                    />
                  </label>
                ) : null}
                {uiSourceMode === "external" ? (
                  <label>
                    Local Frontend Folder
                    <input
                      placeholder="C:\\Trustnode\\frontend-dist"
                      value={uiSourceLocalPath}
                      onChange={(e) => setUiSourceLocalPath(e.target.value)}
                      disabled={!canEditPage("frontend_source")}
                    />
                  </label>
                ) : null}
              </div>
              <div className="row">
                <button className="btn btn-primary" onClick={saveUiSource} disabled={!canEditPage("frontend_source")}>
                  Save Frontend Source
                </button>
                {uiSourceMode === "remote" ? (
                  <button
                    className="btn btn-success"
                    onClick={runUiSourceTest}
                    disabled={!canEditPage("frontend_source") || !uiSourceRemoteUrl.trim()}
                  >
                    Test Remote URL
                  </button>
                ) : null}
              </div>
              {uiSourceSavedMessage ? <div className="info-note">{uiSourceSavedMessage}</div> : null}
              {uiSourceTestResult ? (
                <div className={uiSourceTestResult.startsWith("PASS") ? "info-note" : "error"}>
                  {uiSourceTestResult}
                </div>
              ) : null}
              <div className="lock-note">
                Mode behavior: `Remote hosted frontend` auto-loads website updates. `External local folder`
                loads `index.html` from your folder. Restart desktop app after saving.
              </div>
            </section>
          ) : null}

          {activePage === "alarms" ? (
            <div className="page-fill single">
              <section className="card card-fill">
                <div className="row" style={{ marginBottom: 10 }}>
                  <button
                    className="btn btn-primary"
                    onClick={acknowledgeSelectedAlarms}
                    disabled={!canEditPage("alarms") || !selectedAlarmIds.length}
                  >
                    Acknowledge Selected
                  </button>
                  <button
                    className="btn btn-success"
                    onClick={resumeSelectedAlarmNotifications}
                    disabled={!canEditPage("alarms") || !selectedAlarmIds.length}
                  >
                    Re-enable Email
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={pauseSelectedAlarmNotifications}
                    disabled={!canEditPage("alarms") || !selectedAlarmIds.length}
                  >
                    Pause Email
                  </button>
                  <button
                    className="btn btn-danger"
                    onClick={clearSelectedAlarms}
                    disabled={!canEditPage("alarms") || !selectedAlarmIds.length}
                  >
                    Clear Selected
                  </button>
                  <button className="btn btn-danger" onClick={clearAllAlarms} disabled={!canEditPage("alarms") || !alarms.length}>
                    Clear All
                  </button>
                </div>
                <div className="table-scroll fill-scroll">
                  <div className="table alarms-table">
                    <div className="thead">
                      <span>
                        <input
                          type="checkbox"
                          checked={Boolean(alarms.length) && selectedAlarmIds.length === alarms.length}
                          onChange={(e) => toggleAllAlarmSelected(e.target.checked)}
                          disabled={!alarms.length}
                        />
                      </span>
                      <span>Time</span><span>Message</span><span>Value</span><span>Status</span>
                    </div>
                    {alarms.map((a) => (
                      <div key={a.id} className="trow">
                        <span>
                          <input
                            type="checkbox"
                            checked={selectedAlarmIds.includes(a.id)}
                            onChange={(e) => toggleAlarmSelected(a.id, e.target.checked)}
                          />
                        </span>
                        <span>{a.ts}</span><span>{a.message}</span><span>{a.value}</span>
                        <span>
                          {a.paused_by_tag ? (
                            <span className="status-pill status-warning">PAUSED BY TAG</span>
                          ) : a.acknowledged ? (
                            <span className={`status-pill ${a.notification_paused ? "status-warning" : "status-online"}`}>
                              {a.notification_paused ? "ACK | EMAIL PAUSED" : "ACK | EMAIL ON"}
                            </span>
                          ) : (
                            <button className="btn btn-primary" onClick={() => acknowledgeAlarm(a.id)} disabled={!canEditPage("alarms")}>Acknowledge</button>
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {activePage === "historian" || activePage === "storian" ? (
            <div className="page-fill">
              <section className="card">
                <div className="form-grid">
                  <label>
                    From
                    <input
                      type="datetime-local"
                      value={historianFilters.from}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, from: e.target.value }))}
                    />
                  </label>
                  <label>
                    To
                    <input
                      type="datetime-local"
                      value={historianFilters.to}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, to: e.target.value }))}
                    />
                  </label>
                  <label>
                    Tag
                    <input
                      placeholder="Filter by tag"
                      value={historianFilters.tag}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, tag: e.target.value }))}
                    />
                  </label>
                  <label>
                    Gateway
                    <select
                      value={historianFilters.gatewayId}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, gatewayId: e.target.value }))}
                    >
                      <option value="">All gateways</option>
                      {gatewayConfigsView.map((g) => (
                        <option key={g.id} value={g.id}>{g.name}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Device
                    <input
                      placeholder="Filter by device"
                      value={historianFilters.deviceName}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, deviceName: e.target.value }))}
                    />
                  </label>
                  <label>
                    Quality
                    <select
                      value={historianFilters.quality}
                      onChange={(e) => setHistorianFilters((p) => ({ ...p, quality: e.target.value }))}
                    >
                      <option value="all">All</option>
                      <option value="GOOD">GOOD</option>
                      <option value="UNCERTAIN">UNCERTAIN</option>
                      <option value="BAD">BAD</option>
                    </select>
                  </label>
                </div>
                <div className="row">
                  <button
                    className="btn btn-success"
                    onClick={() =>
                      downloadText(
                        `historian_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`,
                        toCsv(
                          historianRows.map((r) => ({
                            timestamp_utc: fmtTs(r.ts),
                            gateway: r.gateway_name || "",
                            device: r.device_name || "",
                            plc_ip: r.plc_ip || "",
                            database: r.database_name || "",
                            tag: r.tag,
                            value: r.value,
                            quality: r.quality,
                            quality_label: r.quality_label,
                            source: r.source
                          }))
                        ),
                        "text/csv;charset=utf-8"
                      )
                    }
                  >
                    Export CSV
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={() =>
                      downloadText(
                        `historian_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`,
                        JSON.stringify(historianRows, null, 2),
                        "application/json;charset=utf-8"
                      )
                    }
                  >
                    Export JSON
                  </button>
                </div>
              </section>
              <section className="card card-fill">
                <div className="table-scroll fill-scroll">
                  <div className="table historian-table">
                    <div className="thead">
                      <span>Timestamp (UTC)</span><span>Tag</span><span>Value</span><span>Quality</span><span>Device</span><span>Gateway</span><span>Database</span><span>PLC</span>
                    </div>
                    {historianRows.map((row, idx) => (
                      <div key={`${row.ts}-${row.tag}-${idx}`} className="trow">
                        <span>{fmtTs(row.ts)}</span>
                        <span>{row.tag}</span>
                        <span>{row.value}</span>
                        <span>{row.quality_label} ({row.quality})</span>
                        <span>{row.device_name || "-"}</span>
                        <span>{row.gateway_name || row.gateway_id || "-"}</span>
                        <span>{row.database_name || "-"}</span>
                        <span>{row.plc_ip || "-"}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {activePage === "logs" ? (
            <div className="page-fill">
              <section className="card">
                <div className="form-grid">
                  <label>
                    From
                    <input
                      type="datetime-local"
                      value={logFilters.from}
                      onChange={(e) => setLogFilters((p) => ({ ...p, from: e.target.value }))}
                    />
                  </label>
                  <label>
                    To
                    <input
                      type="datetime-local"
                      value={logFilters.to}
                      onChange={(e) => setLogFilters((p) => ({ ...p, to: e.target.value }))}
                    />
                  </label>
                  <label>
                    Level
                    <select value={logFilters.level} onChange={(e) => setLogFilters((p) => ({ ...p, level: e.target.value }))}>
                      <option value="all">All</option>
                      <option value="info">Info</option>
                      <option value="warning">Warning</option>
                      <option value="error">Error</option>
                    </select>
                  </label>
                  <label>
                    Category
                    <select value={logFilters.category} onChange={(e) => setLogFilters((p) => ({ ...p, category: e.target.value }))}>
                      <option value="all">All</option>
                      <option value="system">System</option>
                      <option value="gateway">Gateway</option>
                      <option value="database">Database</option>
                      <option value="connectivity">Connectivity</option>
                      <option value="user">User</option>
                    </select>
                  </label>
                  <label>
                    Gateway
                    <select value={logFilters.gatewayId} onChange={(e) => setLogFilters((p) => ({ ...p, gatewayId: e.target.value }))}>
                      <option value="">All gateways</option>
                      {gatewayConfigsView.map((g) => (
                        <option key={g.id} value={g.id}>{g.name}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Search text
                    <input
                      placeholder="message, device, db..."
                      value={logFilters.text}
                      onChange={(e) => setLogFilters((p) => ({ ...p, text: e.target.value }))}
                    />
                  </label>
                </div>
                <div className="row">
                  <button
                    className="btn btn-success"
                    onClick={() =>
                      downloadText(
                        `logs_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv`,
                        toCsv(filteredLogs),
                        "text/csv;charset=utf-8"
                      )
                    }
                  >
                    Export CSV
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={() =>
                      downloadText(
                        `logs_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`,
                        JSON.stringify(filteredLogs, null, 2),
                        "application/json;charset=utf-8"
                      )
                    }
                  >
                    Export JSON
                  </button>
                </div>
              </section>
              <section className="card card-fill">
                <div className="table-scroll fill-scroll">
                  <div className="table logs-table">
                    <div className="thead">
                      <span>Timestamp (UTC)</span><span>Level</span><span>Category</span><span>Message</span><span>Gateway</span><span>Device</span><span>Database</span>
                    </div>
                    {filteredLogs.map((row, idx) => (
                      <div key={`${row.ts}-${idx}`} className="trow">
                        <span>{fmtTs(row.ts)}</span>
                        <span>{String(row.level || "").toUpperCase()}</span>
                        <span>{row.category || "-"}</span>
                        <span>{row.message || "-"}</span>
                        <span>{row.gateway_name || row.gateway_id || "-"}</span>
                        <span>{row.device_name || "-"}</span>
                        <span>{row.database_name || "-"}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {activePage === "users_and_access_control" ? (
            <div className="users-access-page">
              <section className="card">
                <div className="table-scroll users-table-scroll">
                  <div className="table users-table">
                    <div className="thead">
                      <span>User</span><span>Role</span><span>Gateway Config</span><span>Gateway Start/Stop</span><span>Database</span><span>User Admin</span><span>Actions</span>
                    </div>
                    {users.map((u) => (
                      <div key={u.username} className="trow">
                        <span>{u.username}</span>
                        <span>{u.role}</span>
                        <span>{u.permissions?.gateway_configuration ? "Yes" : "No"}</span>
                        <span>{u.permissions?.gateway_runtime_control ? "Yes" : "No"}</span>
                        <span>{u.permissions?.database ? "Yes" : "No"}</span>
                        <span>{u.permissions?.users_and_access_control ? "Yes" : "No"}</span>
                        <span className="row-actions">
                          <button className="icon-btn table-action-btn" onClick={() => openEditUser(u)} disabled={!canManageUsers} title="Edit user">
                            <EditIcon />
                          </button>
                          <button className="icon-btn table-action-btn danger" onClick={() => deleteUser(u.username)} disabled={!canManageUsers || String(u.username) === "admin"} title="Delete user">
                            <DeleteIcon />
                          </button>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>

              {canManageUsers ? (
                <section className="card">
                  <h4>Create User</h4>
                  <div className="users-create-grid">
                    <label>
                      Username
                      <input value={newUserForm.username} onChange={(e) => setNewUserForm({ ...newUserForm, username: e.target.value })} />
                    </label>
                    <label>
                      Password
                      <input type="password" value={newUserForm.password} onChange={(e) => setNewUserForm({ ...newUserForm, password: e.target.value })} />
                    </label>
                    <label>
                      Role
                      <select
                        value={newUserForm.role}
                        onChange={(e) => {
                          const role = e.target.value;
                          setNewUserForm({
                            ...newUserForm,
                            role,
                            permissions: buildRolePermissions(role)
                          });
                        }}
                      >
                        <option value="viewer">viewer</option>
                        <option value="operator">operator</option>
                        <option value="engineer">engineer</option>
                      </select>
                    </label>
                  </div>
                  <div className="users-perm-groups">
                    {PERMISSION_GROUPS.map((group) => (
                      <div key={group.title} className="perm-group-card">
                        <div className="perm-group-title">{group.title}</div>
                        <div className="perm-group-items">
                          {group.items.map((perm) => (
                            <label key={perm} className="perm-item">
                              <input
                                type="checkbox"
                                checked={Boolean(newUserForm.permissions[perm])}
                                onChange={(e) =>
                                  setNewUserForm({
                                    ...newUserForm,
                                    permissions: { ...newUserForm.permissions, [perm]: e.target.checked }
                                  })
                                }
                              />
                              <span>{PERMISSION_LABELS[perm] || perm.replace(/_/g, " ")}</span>
                            </label>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="row">
                    <button className="btn btn-primary" onClick={createUser}>Create User</button>
                  </div>
                </section>
              ) : (
                <div className="lock-note">LOCK: only admin/admin can create users and edit permissions.</div>
              )}
            </div>
          ) : null}

          {activePage === "tags" ? (
            <div className="page-fill">
              <section className="card">
                <div className="form-grid">
                  <label>
                    Gateway
                    <select
                      value={tagFilters.gatewayId}
                      onChange={(e) => setTagFilters((p) => ({ ...p, gatewayId: e.target.value }))}
                    >
                      <option value="">All gateways</option>
                      {gatewayConfigsView.map((g) => (
                        <option key={g.id} value={g.id}>{g.name}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Device
                    <input
                      placeholder="Filter by device"
                      value={tagFilters.device}
                      onChange={(e) => setTagFilters((p) => ({ ...p, device: e.target.value }))}
                    />
                  </label>
                  <label>
                    Tag
                    <input
                      placeholder="Filter by tag"
                      value={tagFilters.tag}
                      onChange={(e) => setTagFilters((p) => ({ ...p, tag: e.target.value }))}
                    />
                  </label>
                  <label>
                    Last Value
                    <input
                      placeholder="Filter by value"
                      value={tagFilters.value}
                      onChange={(e) => setTagFilters((p) => ({ ...p, value: e.target.value }))}
                    />
                  </label>
                </div>
              </section>
              <section className="card card-fill">
                <div className="table-scroll fill-scroll">
                  <div className="table tags-table">
                    <div className="thead">
                      <span>Tag Name</span><span>Device</span><span>Gateway</span><span>Last Value</span><span>Last Read</span><span>Alarms</span><span>Actions</span>
                    </div>
                    {filteredTagRows.map((row) => (
                      <div key={row.key} className="trow">
                        <span>{row.tag_name}</span>
                        <span>{row.device_name}</span>
                        <span>{row.gateway_name}</span>
                        <span>{row.last_value}</span>
                        <span>{row.last_ts}</span>
                        <span>
                          <input
                            type="checkbox"
                            checked={isTagAlarmEnabled(row.gateway_id, row.tag_name)}
                            onChange={(e) => setTagAlarmEnabled(row.gateway_id, row.tag_name, e.target.checked)}
                            disabled={!canEditPage("tags")}
                            title="Enable alarm monitoring for this tag"
                          />
                        </span>
                        <span className="row-actions tags-actions-cell">
                          <button
                            className="icon-btn table-action-btn"
                            onClick={() => openTagMonitor(row)}
                            disabled={!canOpenPage("tags")}
                            title="Monitor tag"
                          >
                            <ChartIcon />
                          </button>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {activePage === "triggers_and_limits" ? (
            <>
              <section className="page-tools compact-tools">
                <button className="btn btn-primary icon-text-btn" onClick={openAddCollectionTrigger} disabled={!canEditPage("triggers_and_limits")}>
                  <AddIcon />
                  <span>Add Trigger</span>
                </button>
                <button className="btn btn-success icon-text-btn" onClick={openAddTriggerRule} disabled={!canEditPage("triggers_and_limits")}>
                  <AddIcon />
                  <span>Add Limit Rule</span>
                </button>
              </section>
              <div className="trigger-split">
              <section className="card card-fill">
                <div className="trigger-card-head">
                  <h4>Triggers</h4>
                  <label className="trigger-mode-inline">
                    <span>Mode</span>
                    <select
                      value={collectionTriggerMode}
                      onChange={(e) => setCollectionTriggerMode(e.target.value === "all" ? "all" : "any")}
                      disabled={!canEditPage("triggers_and_limits")}
                    >
                      <option value="any">ANY true (OR)</option>
                      <option value="all">ALL true (AND)</option>
                    </select>
                  </label>
                </div>
                <div className="table-scroll fill-scroll">
                <div className="table trigger-condition-table">
                  <div className="thead">
                    <span>Gateway</span><span>Tag</span><span>Type</span><span>Condition</span><span>Status</span><span>Live Status</span><span>Actions</span>
                  </div>
                  {collectionTriggers.map((trigger) => {
                    const gatewayName = gatewayConfigs.find((g) => g.id === trigger.gateway_id)?.name || trigger.gateway_id || "-";
                    const live = trigger.enabled === false ? { ok: null, label: "DISABLED", valueText: "-", ageText: "-" } : getTriggerLiveStatus(trigger);
                    return (
                      <div key={trigger.id} className="trow">
                        <span>{gatewayName}</span>
                        <span>{trigger.tag_name}</span>
                        <span>{trigger.trigger_type === "one_time" ? "ONE-TIME" : "CONTINUOUS"}</span>
                        <span>{`${trigger.operator} ${trigger.value}`}</span>
                        <span>
                          <span className={`status-pill ${trigger.enabled !== false ? "status-online" : "status-warning"}`}>
                            {trigger.enabled !== false ? "ENABLED" : "DISABLED"}
                          </span>
                        </span>
                        <span>
                          <span className={`status-pill ${live.ok === true ? "status-online" : live.ok === false ? "status-offline" : "status-warning"}`}>
                            {live.label}
                          </span>
                          <div className="muted status-sub">{`V:${live.valueText} | Age:${live.ageText}`}</div>
                        </span>
                        <span className="row-actions">
                          <button className="icon-btn table-action-btn" onClick={() => openEditCollectionTrigger(trigger)} disabled={!canEditPage("triggers_and_limits")} title="Edit trigger">
                            <EditIcon />
                          </button>
                          <button className="icon-btn table-action-btn danger" onClick={() => removeCollectionTrigger(trigger.id)} disabled={!canEditPage("triggers_and_limits")} title="Delete trigger">
                            <DeleteIcon />
                          </button>
                        </span>
                      </div>
                    );
                  })}
                  {!collectionTriggers.length ? (
                    <div className="trow">
                      <span>-</span>
                      <span>No triggers configured yet.</span>
                      <span>-</span>
                      <span>-</span>
                      <span>-</span>
                      <span>-</span>
                      <span>-</span>
                    </div>
                  ) : null}
                </div>
                </div>
              </section>
              <section className="card card-fill">
                <div className="trigger-card-head">
                  <h4>Limits</h4>
                </div>
                <div className="table-scroll fill-scroll">
                <div className="table trigger-limit-table">
                  <div className="thead">
                    <span>Gateway</span><span>Tag</span><span>Lower Limit</span><span>Upper Limit</span><span>Status</span><span>Live Reading</span><span>Actions</span>
                  </div>
                  {triggerRules.map((rule) => {
                    const gatewayName = gatewayConfigs.find((g) => g.id === rule.gateway_id)?.name || rule.gateway_id || "-";
                    const live = getLimitRuleLiveStatus(rule);
                    return (
                      <div key={rule.id} className="trow">
                        <span>{gatewayName}</span>
                        <span>{rule.tag_name}</span>
                        <span>{rule.lower_enabled ? `${rule.lower_operator} ${rule.lower_value}` : "-"}</span>
                        <span>{rule.upper_enabled ? `${rule.upper_operator} ${rule.upper_value}` : "-"}</span>
                        <span>
                          <span className={`status-pill ${rule.enabled !== false ? "status-online" : "status-warning"}`}>
                            {rule.enabled !== false ? "ACTIVE" : "DISABLED"}
                          </span>
                        </span>
                        <span>
                          <span className={`status-pill ${live.ok === true ? "status-online" : live.ok === false ? "status-offline" : "status-warning"}`}>
                            {live.label}
                          </span>
                          <div className="muted status-sub">{`V:${live.valueText} | Age:${live.ageText}`}</div>
                        </span>
                        <span className="row-actions">
                          <button className="icon-btn table-action-btn" onClick={() => openEditTriggerRule(rule)} disabled={!canEditPage("triggers_and_limits")} title="Edit rule">
                            <EditIcon />
                          </button>
                          <button className="icon-btn table-action-btn danger" onClick={() => removeTriggerRule(rule.id)} disabled={!canEditPage("triggers_and_limits")} title="Delete rule">
                            <DeleteIcon />
                          </button>
                        </span>
                      </div>
                    );
                  })}
                  {!triggerRules.length ? (
                    <div className="trow">
                      <span>—</span>
                      <span>No trigger rules configured yet.</span>
                      <span>—</span>
                      <span>—</span>
                      <span>—</span>
                      <span>—</span>
                      <span>—</span>
                    </div>
                  ) : null}
                </div>
                </div>
              </section>
              </div>
            </>
          ) : null}

          {activePage === "reporting" ? (
            <div className="reporting-workspace">
              <div className="reporting-left">
                <section className="card">
                <h4 className="card-title">Report Filters</h4>
                <div className="reporting-filter-grid">
                  <label>
                    From
                    <input type="datetime-local" value={reportFilters.from} onChange={(e) => setReportFilters((p) => ({ ...p, from: e.target.value }))} />
                  </label>
                  <label>
                    To
                    <input type="datetime-local" value={reportFilters.to} onChange={(e) => setReportFilters((p) => ({ ...p, to: e.target.value }))} />
                  </label>
                  <label className="reporting-max-rows">
                    Max Rows
                    <input
                      type="number"
                      min="200"
                      step="200"
                      value={reportFilters.max_rows}
                      onChange={(e) => setReportFilters((p) => ({ ...p, max_rows: Number(e.target.value || 3000) }))}
                    />
                  </label>
                  <label>
                    Batch/Source
                    <input value={reportFilters.batch} onChange={(e) => setReportFilters((p) => ({ ...p, batch: e.target.value }))} placeholder="Filter by batch/source" />
                  </label>
                </div>
                <div className="reporting-select-row">
                  <div className="report-check-group gateway-col">
                    <div className="report-check-title">Gateways</div>
                    <div className="report-check-list">
                      {reportFilterOptions.gateways.map((g) => (
                        <label key={`gw-${g.id}`} className="report-check-item gateway-check-item">
                          <input type="checkbox" checked={isSelected(reportFilters.selected_gateway_ids, g.id)} onChange={() => toggleFilterSelection("selected_gateway_ids", g.id)} />
                          <span>{g.name}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="report-check-group tags-col">
                    <div className="report-check-title">Tags (columns) + Y Axis</div>
                    <div className="report-check-list">
                      {reportFilterOptions.tags.map((v) => (
                        <div key={`tag-${v}`} className="report-check-item tag-check-item">
                          <input type="checkbox" checked={isSelected(reportFilters.selected_tags, v)} onChange={() => toggleFilterSelection("selected_tags", v)} />
                          <span>{v}</span>
                          <select
                            value={getReportTagAxis(v)}
                            onChange={(e) => setReportTagAxis(v, e.target.value)}
                            title="Chart Y axis"
                          >
                            <option value="left">Y-Left</option>
                            <option value="right">Y-Right</option>
                          </select>
                          <input
                            type="color"
                            value={getReportTagColor(v, reportFilterOptions.tags.indexOf(v))}
                            onChange={(e) => setReportTagColor(v, e.target.value)}
                            title="Series color"
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="reporting-actions-wide-row">
                  <button className="btn btn-primary" onClick={() => loadReportingData()}>Load Data</button>
                  <button className="btn btn-danger" onClick={() => createReportDocument("pdf")}>Generate PDF</button>
                  <button className="btn btn-success" onClick={() => createReportDocument("csv")}>Generate CSV</button>
                </div>
              </section>
              <section className="card card-fill">
                <h4 className="card-title">Generated Reports</h4>
                <div className="table-scroll fill-scroll">
                  <div className="table reporting-docs-table">
                    <div className="thead">
                      <span>Created</span><span>By</span><span>Summary</span><span>Actions</span>
                    </div>
                    {safeReportDocuments.map((doc) => (
                      <div key={doc.id} className="trow">
                        <span>{doc.created_utc}</span>
                        <span>{doc.generated_by || "-"}</span>
                        <span className="db-cell" title={doc.summary}>{doc.summary}</span>
                        <span className="row-actions">
                          <button className="icon-btn table-action-btn" onClick={() => openReportPreview(doc)} title="Preview"><PreviewIcon /></button>
                          <button className="icon-btn table-action-btn" onClick={() => downloadReportPdf(doc)} title="Download PDF"><PdfIcon /></button>
                          <button className="icon-btn table-action-btn" onClick={() => downloadReportCsv(doc)} title="Download CSV"><CsvIcon /></button>
                          <button className="icon-btn table-action-btn danger" onClick={() => removeReportDocument(doc.id)} title="Delete"><DeleteIcon /></button>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </section>
              </div>
              <div className="reporting-right">
                {!reportPreviewDoc ? (
                  <section className="card card-fill reporting-main-card">
                    <div className="row trend-header-row">
                      <h4 className="card-title">Chart and Table</h4>
                      <button className="btn btn-primary btn-sm" type="button" onClick={toggleReportChartType}>
                        {reportChartType === "bar" ? "Line" : "Bar"}
                      </button>
                    </div>
                    <div className="muted report-summary">{reportSummaryText || "No data loaded yet. Apply filters and click Load Data."}</div>
                    <div className="muted report-summary">{reportLoadedAt ? `Loaded UTC: ${reportLoadedAt}` : "-"}</div>
                    <div className="chart-wrap reporting-chart-wrap">
                      <ResponsiveContainer width="100%" height={180}>
                        <ComposedChart data={reportingChartData} margin={{ top: 6, right: 14, left: 18, bottom: 6 }}>
                          <XAxis dataKey="ts" />
                          <YAxis yAxisId="left" width={52} domain={["auto", "auto"]} />
                          <YAxis yAxisId="right" orientation="right" width={52} domain={["auto", "auto"]} />
                          <Tooltip />
                          {reportSelectedTags.slice(0, 6).map((tag, idx) => {
                            const color = getReportTagColor(tag, idx);
                            if (reportChartType === "bar") {
                              return (
                                <Bar
                                  key={`rt-bar-${tag}`}
                                  isAnimationActive={false}
                                  yAxisId={getReportTagAxis(tag)}
                                  dataKey={tag}
                                  fill={color}
                                  fillOpacity={0.8}
                                  maxBarSize={18}
                                />
                              );
                            }
                            return (
                              <Line
                                key={`rt-line-${tag}`}
                                isAnimationActive={false}
                                type="linear"
                                yAxisId={getReportTagAxis(tag)}
                                dataKey={tag}
                                stroke={color}
                                strokeWidth={2}
                                dot={false}
                              />
                            );
                          })}
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="table-scroll fill-scroll">
                      <div className="table historian-table reporting-pivot-table">
                        <div className="thead" style={{ gridTemplateColumns: `1.4fr repeat(${Math.max(1, reportSelectedTags.length)}, minmax(120px, 1fr))` }}>
                          <span>Timestamp (UTC)</span>
                          {reportSelectedTags.map((t) => <span key={`rh-${t}`}>{t}</span>)}
                        </div>
                        {reportPivotRows.map((row, idx) => (
                          <div key={`rp-${idx}`} className="trow" style={{ gridTemplateColumns: `1.4fr repeat(${Math.max(1, reportSelectedTags.length)}, minmax(120px, 1fr))` }}>
                            <span>{row.ts}</span>
                            {reportSelectedTags.map((t) => <span key={`rv-${idx}-${t}`}>{row[t]}</span>)}
                          </div>
                        ))}
                      </div>
                    </div>
                  </section>
                ) : (
                  <section className="card card-fill reporting-preview-card">
                    <div className="row trend-header-row">
                      <h4 className="card-title">Report Preview</h4>
                      <button className="btn btn-primary btn-sm" onClick={() => setReportPreviewDoc(null)}>Back to Chart/Table</button>
                    </div>
                    <iframe
                      title="report-preview"
                      className="report-preview-frame"
                      srcDoc={reportPreviewDoc.html_content || ""}
                    />
                  </section>
                )}
              </div>
            </div>
          ) : null}
          </div>
          <footer ref={footerRef} className={`gateway-footer ${footerCollapsed ? "collapsed" : ""}`}>
            <div className="gateway-footer-title">Enabled Gateways</div>
            <div className="gateway-footer-table">
              <div className="gateway-footer-head">
                <span>Gateway Name</span><span>IP Address</span><span>Status</span><span>Interval</span><span>Database Writing Status</span><span>Actions</span>
              </div>
              {gatewayConfigsView.map((g) => {
                const health = getGatewayHealth(g);
                const running = isGatewayRunning(g);
                return (
                  <div key={`footer-${g.id}`} className="gateway-footer-row">
                    <span className="gateway-footer-cell" title={g.name}>{g.name}</span>
                    <span className="gateway-footer-cell" title={getGatewayFooterAddress(g)}>{getGatewayFooterAddress(g)}</span>
                    <span className="gateway-footer-cell">
                      <span className={`status-pill ${health.ok ? "status-online" : "status-warning"}`}>{health.label}</span>
                      <span className={`status-pill ${running ? "status-online" : "status-offline"}`} style={{ marginLeft: 6 }}>
                        {running ? "RUNNING" : "STOPPED"}
                      </span>
                    </span>
                    <span className="gateway-footer-cell">{Number(g.interval_ms || 0)} ms</span>
                    <span className="gateway-footer-cell" title={getGatewayFooterDbWriting(g)}>{getGatewayFooterDbWriting(g)}</span>
                    <span className="row-actions gateway-footer-actions">
                      <button
                        className={`icon-btn table-action-btn footer-action-btn ${running ? "" : "icon-btn-start"}`}
                        onClick={() => startGatewayProfile(g)}
                        disabled={!canControlGateways || running}
                        title={running ? "Gateway already running" : "Start gateway"}
                      >
                        <StartIcon />
                      </button>
                      <button
                        className={`icon-btn table-action-btn footer-action-btn ${running ? "icon-btn-stop" : ""}`}
                        onClick={() => stopGatewayProfile(g.id)}
                        disabled={!canControlGateways || !running}
                        title={!running ? "Gateway is stopped" : "Stop gateway"}
                      >
                        <StopIcon />
                      </button>
                    </span>
                  </div>
                );
              })}
            </div>
          </footer>
          <button
            className={`footer-toggle-fab ${anyGatewayRunning ? "running" : "stopped"} ${footerCollapsed ? "is-collapsed" : "is-expanded"}`}
            onClick={() => setFooterCollapsed((v) => !v)}
            type="button"
            title={footerCollapsed ? "Show footer" : "Hide footer"}
            style={{ bottom: footerCollapsed ? 12 : Math.max(12, footerHeight + 10) }}
          >
            {anyGatewayRunning ? "Running" : "Stopped"}
          </button>
        </main>
      </div>
      {showTagMonitorModal && tagMonitorSelection ? (
        <div className="modal-backdrop">
          <div className="modal-card tag-monitor-modal">
            <div className="row trend-header-row">
              <h3>Tag Monitor</h3>
              <div className="row tag-monitor-header-actions">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => setTagMonitorChartType((t) => (t === "line" ? "bar" : "line"))}
                  type="button"
                >
                  {tagMonitorChartType === "line" ? "Bar View" : "Line View"}
                </button>
                <button
                  className="modal-close-btn"
                  onClick={() => setShowTagMonitorModal(false)}
                  type="button"
                  aria-label="Close"
                  title="Close"
                >
                  X
                </button>
              </div>
            </div>
            <div className="form-grid">
              <label>
                Tag
                <input value={tagMonitorSelection.tag_name} disabled />
              </label>
              <label>
                Device
                <input value={tagMonitorSelection.device_name} disabled />
              </label>
              <label>
                Gateway
                <input value={tagMonitorSelection.gateway_name} disabled />
              </label>
              <label>
                Period
                <input value={`${Number(tagMonitorSelection.period_ms || 0)} ms`} disabled />
              </label>
              <label>
                Last Reading Time
                <input value={tagMonitorKpi.lastTs} disabled />
              </label>
              <label>
                Last Reading KPI
                <input value={`${tagMonitorKpi.last} (delta ${tagMonitorKpi.delta})`} disabled />
              </label>
            </div>
            <div className="meta">
              <span>Avg: {tagMonitorKpi.avg}</span>
              <span>Min: {tagMonitorKpi.min}</span>
              <span>Max: {tagMonitorKpi.max}</span>
              <span>Points: {tagMonitorSeries.length}</span>
            </div>
            <div className="chart-wrap">
              {tagMonitorChartType === "line" ? (
                <ResponsiveContainer width="100%" height={260}>
                  <LineChart data={tagMonitorSeries} margin={{ top: 8, right: 18, left: 24, bottom: 8 }}>
                    <XAxis dataKey="idx" type="number" tickFormatter={(v) => tagMonitorSeries.find((h) => h.idx === v)?.ts || ""} domain={[(min) => Number(min) - 1, (max) => Number(max) + 1]} />
                    <YAxis width={52} domain={["auto", "auto"]} />
                    <Tooltip labelFormatter={(v) => tagMonitorSeries.find((h) => h.idx === v)?.ts || String(v)} />
                    <Line isAnimationActive={false} type="linear" dataKey="value" stroke="#16a34a" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={tagMonitorSeries} margin={{ top: 8, right: 18, left: 30, bottom: 8 }} barCategoryGap="24%">
                    <XAxis dataKey="idx" type="number" tickFormatter={(v) => tagMonitorSeries.find((h) => h.idx === v)?.ts || ""} domain={[(min) => Number(min) - 1, (max) => Number(max) + 1]} />
                    <YAxis width={60} domain={["auto", "auto"]} />
                    <Tooltip labelFormatter={(v) => tagMonitorSeries.find((h) => h.idx === v)?.ts || String(v)} />
                    <Bar isAnimationActive={false} dataKey="value" fill="#0f766e" maxBarSize={22} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </div>
      ) : null}
      {showGatewayModal ? (
        <div className="modal-backdrop">
          <div className="modal-card gateway-modal-card">
            <h3>{editingGatewayId ? "Edit Gateway Configuration" : "Add Gateway Configuration"}</h3>
            <div className="gateway-form-grid">
              <label>
                Configuration Name
                <input
                  value={gatewayForm.name}
                  onChange={(e) => setGatewayForm({ ...gatewayForm, name: e.target.value })}
                  disabled={!canEditPage("gateway_configuration")}
                />
              </label>
              <label>
                PLC Device
                <select
                  value={gatewayForm.device_id}
                  onChange={(e) => onGatewayDeviceChange(e.target.value)}
                  disabled={!canEditPage("gateway_configuration")}
                >
                  <option value="">Select device</option>
                  {devices.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Protocol
                <select
                  value={gatewayForm.gateway_type}
                  onChange={(e) => {
                    const nextType = e.target.value;
                    setGatewayForm((prev) => ({
                      ...prev,
                      gateway_type: nextType,
                      opc_url: nextType === "siemens_opcua" ? (prev.opc_url || buildOpcUrlFromIp(prev.plc_ip)) : ""
                    }));
                  }}
                  disabled={!canEditPage("gateway_configuration")}
                >
                  {gatewayOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </label>
              <label>
                PLC IP
                <input
                  value={gatewayForm.plc_ip}
                  onChange={(e) => {
                    const nextIp = e.target.value;
                    setGatewayForm((prev) => ({
                      ...prev,
                      plc_ip: nextIp,
                      opc_url: prev.gateway_type === "siemens_opcua" ? buildOpcUrlFromIp(nextIp) : prev.opc_url
                    }));
                  }}
                  disabled={!canEditPage("gateway_configuration")}
                />
              </label>
              {gatewayForm.gateway_type === "siemens_opcua" ? (
                <label className="gateway-span-2">
                  OPC URL
                  <input
                    value={gatewayForm.opc_url}
                    onChange={(e) => setGatewayForm({ ...gatewayForm, opc_url: e.target.value })}
                    disabled={!canEditPage("gateway_configuration")}
                  />
                </label>
              ) : null}
              <label>
                Database Connection
                <select
                  value={gatewayForm.database_id}
                  onChange={(e) => setGatewayForm({ ...gatewayForm, database_id: e.target.value })}
                  disabled={!canEditPage("gateway_configuration")}
                >
                  <option value="">Select database</option>
                  {dbConnections.map((db) => (
                    <option key={db.id} value={db.id}>{db.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Interval (ms)
                <input
                  type="number"
                  min="100"
                  value={gatewayForm.interval_ms}
                  onChange={(e) => setGatewayForm({ ...gatewayForm, interval_ms: Number(e.target.value) })}
                  disabled={!canEditPage("gateway_configuration")}
                />
              </label>
              <label className="gateway-span-2">
                Tags (; separated)
                <input
                  placeholder="Tag_01;Tag_02;Tag_03"
                  value={gatewayForm.tags_text}
                  onChange={(e) => setGatewayForm({ ...gatewayForm, tags_text: e.target.value })}
                  disabled={!canEditPage("gateway_configuration")}
                />
              </label>
              <div className="gateway-span-2 row">
                <button
                  type="button"
                  className="btn btn-success"
                  onClick={runGatewayTagDiscovery}
                  disabled={!canEditPage("gateway_configuration") || gatewayDiscoverBusy}
                >
                  {gatewayDiscoverBusy
                    ? "Searching..."
                    : gatewayForm.gateway_type === "siemens_opcua"
                      ? "Browse OPC-UA Nodes"
                      : "Search Available Tags"}
                </button>
                {gatewayForm.gateway_type === "siemens_opcua" ? (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={runGatewayOpcNodeValidation}
                    disabled={!canEditPage("gateway_configuration") || gatewayOpcValidationBusy}
                  >
                    {gatewayOpcValidationBusy ? "Validating..." : "Validate OPC Nodes"}
                  </button>
                ) : null}
              </div>
              {gatewayDiscoverResult ? (
                <div className={`gateway-span-2 ${gatewayDiscoverResult.toLowerCase().includes("discovered") ? "info-note" : "error"}`}>
                  {gatewayDiscoverResult}
                </div>
              ) : null}
              {gatewayForm.gateway_type === "siemens_opcua" && gatewayOpcValidationResult ? (
                <div
                  className={`gateway-span-2 ${
                    gatewayOpcValidationRows.length && gatewayOpcValidationRows.every((r) => r.ok)
                      ? "ok-note"
                      : "error"
                  }`}
                >
                  {gatewayOpcValidationResult}
                </div>
              ) : null}
              {gatewayForm.gateway_type === "siemens_opcua" && gatewayOpcValidationRows.length ? (
                <div className="gateway-span-2 discovered-tags-card">
                  <div className="discovered-tags-toolbar">
                    <strong>OPC Node Validation Results</strong>
                  </div>
                  <div className="discovered-tags-list">
                    {gatewayOpcValidationRows.map((row) => (
                      <div key={row.node_id} className="opc-validate-item">
                        <span className={`opc-validate-badge ${row.ok ? "ok" : "fail"}`}>
                          {row.ok ? "OK" : "FAIL"}
                        </span>
                        <span className="opc-validate-node">
                          <code>{row.node_id}</code>
                        </span>
                        <span className="opc-validate-msg">{row.message}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
              {gatewayDiscoveredTags.length ? (
                <div className="gateway-span-2 discovered-tags-card">
                  <div className="discovered-tags-toolbar">
                    <strong>
                      {gatewayForm.gateway_type === "siemens_opcua"
                        ? `Browse Results (Variable Nodes: ${gatewayDiscoveredTags.length})`
                        : `Discovered Tags (${gatewayDiscoveredTags.length})`}
                    </strong>
                    <div className="row">
                      <button type="button" className="btn btn-primary btn-sm" onClick={selectAllDiscoveredTags}>
                        Select All
                      </button>
                      <button type="button" className="btn btn-danger btn-sm" onClick={clearSelectedDiscoveredTags}>
                        Clear
                      </button>
                      <button
                        type="button"
                        className="btn btn-success btn-sm"
                        onClick={applySelectedDiscoveredTags}
                        disabled={!gatewaySelectedTags.length}
                      >
                        Apply Selected ({gatewaySelectedTags.length})
                      </button>
                    </div>
                  </div>
                  <div className="discovered-tags-list">
                    {gatewayForm.gateway_type === "siemens_opcua" && gatewayOpcBrowseNodes.length
                      ? gatewayOpcBrowseNodes.map((node) => {
                          const tag = String(node.node_id || "");
                          const selectable = Boolean(node.is_variable);
                          return (
                            <label
                              key={`${tag}-${node.depth}`}
                              className={`discovered-tag-item ${selectable ? "" : "disabled"}`}
                              style={{ paddingLeft: `${8 + Math.max(0, Number(node.depth || 0)) * 14}px` }}
                            >
                              <input
                                type="checkbox"
                                disabled={!selectable}
                                checked={selectable && gatewaySelectedTags.includes(tag)}
                                onChange={() => selectable && toggleGatewayDiscoveredTag(tag)}
                              />
                              <span>
                                {node.display_name || node.browse_name || tag}
                                {" "}
                                <small>[{node.node_class}]</small>
                                <br />
                                <code>{tag}</code>
                              </span>
                            </label>
                          );
                        })
                      : gatewayDiscoveredTags.map((tag) => (
                          <label key={tag} className="discovered-tag-item">
                            <input
                              type="checkbox"
                              checked={gatewaySelectedTags.includes(tag)}
                              onChange={() => toggleGatewayDiscoveredTag(tag)}
                            />
                            <span>{tag}</span>
                          </label>
                        ))}
                  </div>
                </div>
              ) : null}
              <label className="gateway-span-2">
                Load Configuration TXT
                <input
                  type="file"
                  accept=".txt,text/plain"
                  onChange={(e) => onGatewayConfigFileLoad(e.target.files?.[0])}
                  disabled={!canEditPage("gateway_configuration")}
                />
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary icon-text-btn" onClick={saveGatewayConfig} disabled={!canEditPage("gateway_configuration")}>
                <SaveIcon />
                <span>OK</span>
              </button>
              <button className="btn btn-danger" onClick={() => setShowGatewayModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showDeviceModal ? (
        <div className="modal-backdrop">
          <div className="modal-card">
            <h3>{editingDeviceId ? "Edit PLC Device" : "Add PLC Device"}</h3>
            <div className="device-form-grid">
              <label>
                Device Name
                <input
                  value={deviceForm.name}
                  onChange={(e) => setDeviceForm({ ...deviceForm, name: e.target.value })}
                  disabled={!canEditPage("devices")}
                />
              </label>
              <label>
                Gateway Type
                <select
                  value={deviceForm.gateway_type}
                  onChange={(e) => {
                    const nextType = e.target.value;
                    setDeviceForm({
                      ...deviceForm,
                      gateway_type: nextType,
                      opc_url:
                        nextType === "siemens_opcua"
                          ? buildOpcUrlFromIp(deviceForm.plc_ip) || "opc.tcp://192.168.10.242:4840"
                          : deviceForm.opc_url,
                      opc_node_ids_text:
                        nextType === "siemens_opcua"
                          ? deviceForm.opc_node_ids_text || deviceForm.opc_node_id || DEFAULT_OPC_NODE_ID
                          : deviceForm.opc_node_ids_text
                    });
                  }}
                  disabled={!canEditPage("devices")}
                >
                  {gatewayOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </label>
              <label>
                PLC IP
                <input
                  placeholder="192.168.0.10"
                  value={deviceForm.plc_ip}
                  onChange={(e) => {
                    const nextIp = e.target.value;
                    setDeviceForm({
                      ...deviceForm,
                      plc_ip: nextIp,
                      opc_url:
                        deviceForm.gateway_type === "siemens_opcua"
                          ? buildOpcUrlFromIp(nextIp)
                          : deviceForm.opc_url
                    });
                  }}
                  disabled={!canEditPage("devices")}
                />
              </label>
              {deviceForm.gateway_type === "siemens_opcua" ? (
                <>
                  <label>
                    OPC URL
                    <input
                      placeholder="opc.tcp://192.168.10.242:4840"
                      value={deviceForm.opc_url}
                      onChange={(e) => setDeviceForm({ ...deviceForm, opc_url: e.target.value })}
                      disabled={!canEditPage("devices")}
                    />
                  </label>
                  <label>
                    OPC Node IDs (optional, one per line or comma separated)
                    <textarea
                      placeholder={'Leave blank to test endpoint only, or provide:\nns=3;s="tag1"\nns=3;s="tag2"'}
                      value={deviceForm.opc_node_ids_text}
                      onChange={(e) =>
                        setDeviceForm({
                          ...deviceForm,
                          opc_node_ids_text: e.target.value,
                          opc_node_id: parseOpcNodeIds(e.target.value)[0] || DEFAULT_OPC_NODE_ID
                        })
                      }
                      disabled={!canEditPage("devices")}
                      rows={3}
                    />
                  </label>
                </>
              ) : null}
              <label>
                Notes
                <input
                  value={deviceForm.notes}
                  onChange={(e) => setDeviceForm({ ...deviceForm, notes: e.target.value })}
                  disabled={!canEditPage("devices")}
                />
              </label>
            </div>
            {deviceTestResult ? (
              <div className="test-status-wrap">
                <div className={deviceTestResult.ping_ok ? "info-note compact-note" : "error compact-note"}>
                  IP Reachability (Ping): {deviceTestResult.ping_ok ? "OK" : "FAIL"}
                </div>
                <div className={deviceTestResult.port_ok ? "info-note compact-note" : "error compact-note"}>
                  Type Connection ({deviceTestResult.port ? `Port ${deviceTestResult.port}` : "Port n/a"}): {deviceTestResult.port_ok ? "OK" : "FAIL"}
                </div>
                <div className={deviceTestResult.ok ? "info-note compact-note" : "error compact-note"}>
                  Details: {deviceTestResult.message}
                </div>
                {Array.isArray(deviceTestResult.opc_nodes) && deviceTestResult.opc_nodes.length ? (
                  <div className="test-status-wrap">
                    {deviceTestResult.opc_nodes.map((node) => (
                      <div
                        key={node.node_id}
                        className={node.ok ? "info-note compact-note" : "error compact-note"}
                      >
                        {node.ok ? "OK" : "FAIL"} - {node.node_id}
                        {node.ok && node.value !== undefined ? ` = ${String(node.value)}` : ""}
                        {!node.ok && node.message ? ` (${node.message})` : ""}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
            <div className="row modal-actions">
              <button className="btn btn-success" onClick={runDeviceConnectionTest} disabled={!canEditPage("devices") || deviceTestBusy}>
                {deviceTestBusy ? "Testing..." : "Test Connection"}
              </button>
              <button className="btn btn-primary" onClick={saveDevice} disabled={!canEditPage("devices")}>
                OK
              </button>
              <button className="btn btn-danger" onClick={() => setShowDeviceModal(false)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {showCloudDbPickerModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>Add Cloud Database</h3>
            <div className="trigger-form-grid">
              <label>
                Cloud Type
                <select value={cloudDbPickerType} onChange={(e) => setCloudDbPickerType(e.target.value)} disabled={!isAdminDatabaseUser}>
                  <option value="supabase">Supabase (PostgreSQL)</option>
                  <option value="dolibarr">Dolibarr (Legacy HTTP)</option>
                </select>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={createCloudDbFromPicker} disabled={!isAdminDatabaseUser}>Continue</button>
              <button className="btn btn-danger" onClick={() => setShowCloudDbPickerModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showCloudSyncModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>Manual Cloud Sync</h3>
            <div className="trigger-form-grid">
              <label>
                From (UTC)
                <input
                  type="datetime-local"
                  value={cloudSyncForm.from_utc}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, from_utc: e.target.value }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
              </label>
              <label>
                To (UTC)
                <input
                  type="datetime-local"
                  value={cloudSyncForm.to_utc}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, to_utc: e.target.value }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
              </label>
              <label>
                Max Rows
                <input
                  type="number"
                  min="100"
                  max="200000"
                  value={cloudSyncForm.max_rows}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, max_rows: Number(e.target.value || 20000) }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
              </label>
              <label className="remember-row">
                <input
                  type="checkbox"
                  checked={Boolean(cloudSyncForm.include_logs)}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, include_logs: e.target.checked }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
                <span className="remember-label">Include app logs</span>
              </label>
              <label className="remember-row">
                <input
                  type="checkbox"
                  checked={Boolean(cloudSyncForm.clear_queue_after)}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, clear_queue_after: e.target.checked }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
                <span className="remember-label">Clear pending/failed queue after sync</span>
              </label>
              <label className="remember-row">
                <input
                  type="checkbox"
                  checked={Boolean(cloudSyncForm.drop_backlog_after)}
                  onChange={(e) => setCloudSyncForm((prev) => ({ ...prev, drop_backlog_after: e.target.checked }))}
                  disabled={!isAdminDatabaseUser || forceSyncBusy}
                />
                <span className="remember-label">Drop remaining backlog after sync</span>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={runManualCloudPeriodSync} disabled={!isAdminDatabaseUser || forceSyncBusy}>
                {forceSyncBusy ? "Syncing..." : "Run Sync"}
              </button>
              <button className="btn btn-danger" onClick={() => setShowCloudSyncModal(false)} disabled={forceSyncBusy}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {showOtherDbPickerModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>Add Other Database</h3>
            <div className="trigger-form-grid">
              <label>
                Database Type
                <select value={otherDbPickerType} onChange={(e) => setOtherDbPickerType(e.target.value)} disabled={!isAdminDatabaseUser}>
                  <option value="sqlite">SQLite (Local)</option>
                  <option value="postgresql">PostgreSQL</option>
                  <option value="mysql">MySQL</option>
                  <option value="mssql">MSSQL</option>
                  <option value="influxdb">InfluxDB</option>
                  <option value="csv">CSV File</option>
                  <option value="txt">TXT File</option>
                </select>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={createOtherDbFromPicker} disabled={!isAdminDatabaseUser}>Continue</button>
              <button className="btn btn-danger" onClick={() => setShowOtherDbPickerModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showDbModal ? (
        <div className="modal-backdrop">
          <div className="modal-card db-modal-card">
            <h3>{editingDbId ? "Edit Database Connection" : "Add Database Connection"}</h3>
            <div className="db-modal-layout">
              <section className="db-group">
                <div className="db-group-title">General</div>
                <div className="db-grid-2">
                  <label>
                    Connection Name
                    <input value={dbForm.name} onChange={(e) => setDbForm({ ...dbForm, name: e.target.value })} disabled={!canEditPage("database")} />
                  </label>
                  <label>
                    Engine
                    <select value={dbForm.engine} onChange={(e) => setDbForm({ ...dbForm, engine: e.target.value })} disabled={!canEditPage("database")}>
                      <option value="mysql">MySQL</option>
                      <option value="postgresql">PostgreSQL</option>
                      <option value="mssql">MSSQL</option>
                      <option value="influxdb">InfluxDB</option>
                      <option value="sqlite">SQLite</option>
                      <option value="csv_file">CSV File</option>
                      <option value="txt_file">TXT File</option>
                      <option value="legacy_http">Legacy Trustnode API</option>
                    </select>
                  </label>
                </div>
                <div className="db-grid-2" style={{ marginTop: 8 }}>
                  <label className="remember-row">
                    <input
                      type="checkbox"
                      checked={Boolean(dbForm.enabled)}
                      onChange={(e) => setDbForm({ ...dbForm, enabled: e.target.checked })}
                      disabled={!canEditPage("database")}
                    />
                    <span className="remember-label">Connection Enabled</span>
                  </label>
                  <label className="remember-row">
                    <input
                      type="checkbox"
                      checked={Boolean(dbForm.cloud_sync_enabled)}
                      onChange={(e) => setDbForm({ ...dbForm, cloud_sync_enabled: e.target.checked })}
                      disabled={!canEditPage("database") || dbLocationFromEngine(dbForm.engine) === "local"}
                    />
                    <span className="remember-label">Enable Local-to-Online Sync</span>
                  </label>
                </div>
                <div className="db-grid-2" style={{ marginTop: 8 }}>
                  <label className="remember-row">
                    <input
                      type="checkbox"
                      checked={Boolean(dbForm.use_gateway)}
                      onChange={(e) => setDbForm({ ...dbForm, use_gateway: e.target.checked })}
                      disabled={!canEditPage("database")}
                    />
                    <span className="remember-label">Use for Gateway Data</span>
                  </label>
                  <label className="remember-row">
                    <input
                      type="checkbox"
                      checked={Boolean(dbForm.use_app)}
                      onChange={(e) => setDbForm({ ...dbForm, use_app: e.target.checked })}
                      disabled={!canEditPage("database")}
                    />
                    <span className="remember-label">Use for App Configuration</span>
                  </label>
                  <label className="remember-row">
                    <input
                      type="checkbox"
                      checked={Boolean(dbForm.use_backup)}
                      onChange={(e) => setDbForm({ ...dbForm, use_backup: e.target.checked })}
                      disabled={!canEditPage("database")}
                    />
                    <span className="remember-label">Use for Backup/Redundancy</span>
                  </label>
                  <label>
                    Location
                    <input value={dbLocationFromEngine(dbForm.engine)} readOnly />
                  </label>
                </div>
              </section>

              {dbForm.engine !== "legacy_http" && dbForm.engine !== "sqlite" && dbForm.engine !== "csv_file" && dbForm.engine !== "txt_file" ? (
                <>
                  <section className="db-group">
                    <div className="db-group-title">Connection</div>
                    <div className="db-grid-2">
                      <label>
                        Host
                        <input value={dbForm.host} onChange={(e) => setDbForm({ ...dbForm, host: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label>
                        Port
                        <input value={dbForm.port} onChange={(e) => setDbForm({ ...dbForm, port: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                    </div>
                  </section>

                  <section className="db-group">
                    <div className="db-group-title">Credentials & Target</div>
                    <div className="db-grid-2">
                      <label>
                        Database
                        <input value={dbForm.database} onChange={(e) => setDbForm({ ...dbForm, database: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label>
                        Username
                        <input value={dbForm.username} onChange={(e) => setDbForm({ ...dbForm, username: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label className="db-span-2">
                        Password
                        <input type="password" value={dbForm.password} onChange={(e) => setDbForm({ ...dbForm, password: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label>
                        Schema
                        <input value={dbForm.schema} onChange={(e) => setDbForm({ ...dbForm, schema: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label>
                        Table
                        <input value={dbForm.table} onChange={(e) => setDbForm({ ...dbForm, table: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                    </div>
                  </section>
                </>
              ) : dbForm.engine === "sqlite" ? (
                <>
                  <section className="db-group">
                    <div className="db-group-title">Local SQLite</div>
                    <div className="db-grid-2">
                      <label className="db-span-2">
                        SQLite File Path
                        <input
                          value={dbForm.sqlite_path}
                          onChange={(e) => setDbForm({ ...dbForm, sqlite_path: e.target.value })}
                          placeholder="./data/trustnode_edge.db"
                          disabled={!canEditPage("database")}
                        />
                      </label>
                      <label>
                        Table
                        <input
                          value={dbForm.table}
                          onChange={(e) => setDbForm({ ...dbForm, table: e.target.value })}
                          disabled={!canEditPage("database")}
                        />
                      </label>
                    </div>
                  </section>
                </>
              ) : dbForm.engine === "csv_file" || dbForm.engine === "txt_file" ? (
                <>
                  <section className="db-group">
                    <div className="db-group-title">File Sink</div>
                    <div className="db-grid-2">
                      <label className="db-span-2">
                        Output File Path
                        <input
                          value={dbForm.file_path}
                          onChange={(e) => setDbForm({ ...dbForm, file_path: e.target.value })}
                          placeholder={dbForm.engine === "csv_file" ? "./data/trustnode_log.csv" : "./data/trustnode_log.txt"}
                          disabled={!canEditPage("database")}
                        />
                      </label>
                    </div>
                  </section>
                </>
              ) : (
                <>
                  <section className="db-group">
                    <div className="db-group-title">Legacy API</div>
                    <div className="db-quick-actions">
                      <button
                        className="btn btn-primary btn-sm"
                        onClick={() =>
                          setDbForm({
                            ...dbForm,
                            legacy_url: "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php",
                            legacy_api_token: "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3",
                            source: "edge-01",
                            site: "Limerick",
                            area: "LineA",
                            equipment: "MACHINE-01"
                          })
                        }
                        disabled={!canEditPage("database")}
                        type="button"
                      >
                        Use Known Legacy Defaults
                      </button>
                    </div>
                    <div className="db-grid-2">
                      <label className="db-span-2">
                        Legacy API URL
                        <input value={dbForm.legacy_url} onChange={(e) => setDbForm({ ...dbForm, legacy_url: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                      <label className="db-span-2">
                        API Token
                        <input value={dbForm.legacy_api_token} onChange={(e) => setDbForm({ ...dbForm, legacy_api_token: e.target.value })} disabled={!canEditPage("database")} />
                      </label>
                    </div>
                  </section>
                </>
              )}

              <section className="db-group">
                <div className="db-group-title">Metadata</div>
                <div className="db-grid-2">
                  <label>
                    Source
                    <input value={dbForm.source} onChange={(e) => setDbForm({ ...dbForm, source: e.target.value })} disabled={!canEditPage("database")} />
                  </label>
                  <label>
                    Site
                    <input value={dbForm.site} onChange={(e) => setDbForm({ ...dbForm, site: e.target.value })} disabled={!canEditPage("database")} />
                  </label>
                  <label>
                    Area
                    <input value={dbForm.area} onChange={(e) => setDbForm({ ...dbForm, area: e.target.value })} disabled={!canEditPage("database")} />
                  </label>
                  <label>
                    Equipment
                    <input value={dbForm.equipment} onChange={(e) => setDbForm({ ...dbForm, equipment: e.target.value })} disabled={!canEditPage("database")} />
                  </label>
                </div>
              </section>

              {dbForm.engine !== "sqlite" ? (
                <label className="remember-row">
                  <input type="checkbox" checked={dbForm.tls} onChange={(e) => setDbForm({ ...dbForm, tls: e.target.checked })} disabled={!canEditPage("database")} />
                  <span className="remember-label">Use TLS/SSL</span>
                </label>
              ) : null}
            </div>
            {dbForm.engine === "postgresql" ? (
              <div className="lock-note">
                For Supabase free tier on IPv4-only networks, use Pooler host/port (usually 6543) instead of Direct connection.
              </div>
            ) : null}
            {dbTestResult ? (
              <div className={dbTestResult.ok ? "info-note" : "error"}>
                {dbTestResult.message}
              </div>
            ) : null}
            <div className="row modal-actions">
              <button className="btn btn-success" onClick={runDbConnectionTest} disabled={!canEditPage("database") || dbTestBusy}>
                {dbTestBusy ? "Testing..." : "Test Connection"}
              </button>
              <button className="btn btn-primary" onClick={saveDbConnection} disabled={!canEditPage("database") || dbProvisionBusy}>
                {dbProvisionBusy ? "Provisioning..." : "OK"}
              </button>
              <button className="btn btn-danger" onClick={() => setShowDbModal(false)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {showEditUserModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>Edit User</h3>
            <div className="trigger-form-grid">
              <label>
                Username
                <input value={editingUsername} disabled />
              </label>
              <label>
                Password
                <input
                  type="password"
                  value={editUserForm.password}
                  onChange={(e) => setEditUserForm({ ...editUserForm, password: e.target.value })}
                  disabled={!canManageUsers}
                />
              </label>
              <label>
                Role
                <select
                  value={editUserForm.role}
                  onChange={(e) => {
                    const role = e.target.value;
                    setEditUserForm({
                      ...editUserForm,
                      role,
                      permissions: normalizePermissions(editUserForm.permissions, role)
                    });
                  }}
                  disabled={!canManageUsers}
                >
                  <option value="viewer">viewer</option>
                  <option value="operator">operator</option>
                  <option value="engineer">engineer</option>
                  <option value="admin">admin</option>
                </select>
              </label>
            </div>
            <div className="users-perm-groups" style={{ marginTop: 8 }}>
              {PERMISSION_GROUPS.map((group) => (
                <div key={`edit-${group.title}`} className="perm-group-card">
                  <div className="perm-group-title">{group.title}</div>
                  <div className="perm-group-items">
                    {group.items.map((perm) => (
                      <label key={`edit-${perm}`} className="perm-item">
                        <input
                          type="checkbox"
                          checked={Boolean(editUserForm.permissions[perm])}
                          onChange={(e) =>
                            setEditUserForm({
                              ...editUserForm,
                              permissions: { ...editUserForm.permissions, [perm]: e.target.checked }
                            })
                          }
                          disabled={!canManageUsers}
                        />
                        <span>{PERMISSION_LABELS[perm] || perm.replace(/_/g, " ")}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveEditedUser} disabled={!canManageUsers}>Save</button>
              <button className="btn btn-danger" onClick={() => setShowEditUserModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showDashboardWidgetModal ? (
        <div className="modal-backdrop">
          <div className="modal-card">
            <h3>{editingDashboardWidgetId ? "Edit Dashboard Item" : "Add Dashboard Item"}</h3>
            <div className="form-grid">
              <label>
                Title
                <input
                  value={dashboardWidgetForm.title}
                  onChange={(e) => setDashboardWidgetForm({ ...dashboardWidgetForm, title: e.target.value })}
                  placeholder="Optional display title"
                />
              </label>
              <label>
                Gateway
                <select
                  value={dashboardWidgetForm.gateway_id}
                  onChange={(e) => {
                    const nextGateway = e.target.value;
                    const nextTags = triggerTagsByGateway[nextGateway] || [];
                    const nextTag = nextTags.includes(dashboardWidgetForm.tag_name) ? dashboardWidgetForm.tag_name : (nextTags[0] || "");
                    setDashboardWidgetForm({ ...dashboardWidgetForm, gateway_id: nextGateway, tag_name: nextTag });
                  }}
                >
                  <option value="">Select gateway</option>
                  {gatewayConfigsView.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Tag
                <select
                  value={dashboardWidgetForm.tag_name}
                  onChange={(e) => setDashboardWidgetForm({ ...dashboardWidgetForm, tag_name: e.target.value })}
                >
                  <option value="">Select tag</option>
                  {(triggerTagsByGateway[dashboardWidgetForm.gateway_id] || []).map((tag) => (
                    <option key={tag} value={tag}>{tag}</option>
                  ))}
                </select>
              </label>
              <label>
                Reading Points
                <input
                  type="number"
                  min="20"
                  max="500"
                  value={dashboardWidgetForm.readings_count}
                  onChange={(e) => setDashboardWidgetForm({ ...dashboardWidgetForm, readings_count: Number(e.target.value || 120) })}
                />
              </label>
              <label>
                Chart Color
                <input
                  type="color"
                  value={dashboardWidgetForm.color}
                  onChange={(e) => setDashboardWidgetForm({ ...dashboardWidgetForm, color: e.target.value })}
                />
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveDashboardWidget}>OK</button>
              <button className="btn btn-danger" onClick={() => setShowDashboardWidgetModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showCollectionTriggerModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>{editingCollectionTriggerId ? "Edit Trigger Condition" : "Add Trigger Condition"}</h3>
            <div className="trigger-form-grid">
              <label>
                Gateway
                <select
                  value={collectionTriggerForm.gateway_id}
                  onChange={(e) => {
                    const nextGateway = e.target.value;
                    const nextTags = triggerTagsByGateway[nextGateway] || [];
                    const nextTag = nextTags.includes(collectionTriggerForm.tag_name) ? collectionTriggerForm.tag_name : (nextTags[0] || "");
                    setCollectionTriggerForm({ ...collectionTriggerForm, gateway_id: nextGateway, tag_name: nextTag });
                  }}
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="">Select gateway</option>
                  {gatewayConfigsView.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Tag
                <select
                  value={collectionTriggerForm.tag_name}
                  onChange={(e) => setCollectionTriggerForm({ ...collectionTriggerForm, tag_name: e.target.value })}
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="">Select tag</option>
                  {(triggerTagsByGateway[collectionTriggerForm.gateway_id] || []).map((tag) => (
                    <option key={tag} value={tag}>{tag}</option>
                  ))}
                </select>
              </label>
              <label>
                Condition Operator
                <select
                  value={collectionTriggerForm.operator}
                  onChange={(e) => setCollectionTriggerForm({ ...collectionTriggerForm, operator: e.target.value })}
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="<">{"<"}</option>
                  <option value="<=">{"<="}</option>
                  <option value=">">{">"}</option>
                  <option value=">=">{">="}</option>
                </select>
              </label>
              <label>
                Condition Value
                <input
                  type="number"
                  step="any"
                  value={collectionTriggerForm.value}
                  onChange={(e) => setCollectionTriggerForm({ ...collectionTriggerForm, value: e.target.value })}
                  disabled={!canEditPage("triggers_and_limits")}
                  placeholder="Value"
                />
              </label>
              <label>
                Trigger Type
                <select
                  value={collectionTriggerForm.trigger_type || "continuous"}
                  onChange={(e) =>
                    setCollectionTriggerForm({
                      ...collectionTriggerForm,
                      trigger_type: e.target.value === "one_time" ? "one_time" : "continuous"
                    })
                  }
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="continuous">Continuous</option>
                  <option value="one_time">One-Time</option>
                </select>
              </label>
              <label className="remember-row trigger-enabled-row">
                <input
                  type="checkbox"
                  checked={Boolean(collectionTriggerForm.enabled)}
                  onChange={(e) => setCollectionTriggerForm({ ...collectionTriggerForm, enabled: e.target.checked })}
                  disabled={!canEditPage("triggers_and_limits")}
                />
                <span className="remember-label">Enabled</span>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveCollectionTrigger} disabled={!canEditPage("triggers_and_limits")}>OK</button>
              <button className="btn btn-danger" onClick={() => setShowCollectionTriggerModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showTriggerModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>{editingTriggerId ? "Edit Trigger Limit" : "Add Trigger Limit"}</h3>
            <div className="trigger-form-grid">
              <label>
                Gateway
                <select
                  value={triggerForm.gateway_id}
                  onChange={(e) => {
                    const nextGateway = e.target.value;
                    const nextTags = triggerTagsByGateway[nextGateway] || [];
                    const nextTag = nextTags.includes(triggerForm.tag_name) ? triggerForm.tag_name : (nextTags[0] || "");
                    setTriggerForm({ ...triggerForm, gateway_id: nextGateway, tag_name: nextTag });
                  }}
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="">Select gateway</option>
                  {gatewayConfigsView.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Tag
                <select
                  value={triggerForm.tag_name}
                  onChange={(e) => setTriggerForm({ ...triggerForm, tag_name: e.target.value })}
                  disabled={!canEditPage("triggers_and_limits")}
                >
                  <option value="">Select tag</option>
                  {(triggerTagsByGateway[triggerForm.gateway_id] || []).map((tag) => (
                    <option key={tag} value={tag}>{tag}</option>
                  ))}
                </select>
              </label>
              <label className="trigger-limit-row">
                <span>
                  <input
                    type="checkbox"
                    checked={Boolean(triggerForm.lower_enabled)}
                    onChange={(e) => setTriggerForm({ ...triggerForm, lower_enabled: e.target.checked })}
                    disabled={!canEditPage("triggers_and_limits")}
                  />
                  {" "}
                  Lower limit
                </span>
                <div className="trigger-limit-controls">
                  <select
                    value={triggerForm.lower_operator}
                    onChange={(e) => setTriggerForm({ ...triggerForm, lower_operator: e.target.value })}
                    disabled={!canEditPage("triggers_and_limits") || !triggerForm.lower_enabled}
                  >
                    <option value="<">{"<"}</option>
                    <option value="<=">{"<="}</option>
                  </select>
                  <input
                    type="number"
                    step="any"
                    value={triggerForm.lower_value}
                    onChange={(e) => setTriggerForm({ ...triggerForm, lower_value: e.target.value })}
                    disabled={!canEditPage("triggers_and_limits") || !triggerForm.lower_enabled}
                    placeholder="Value"
                  />
                </div>
              </label>
              <label className="trigger-limit-row">
                <span>
                  <input
                    type="checkbox"
                    checked={Boolean(triggerForm.upper_enabled)}
                    onChange={(e) => setTriggerForm({ ...triggerForm, upper_enabled: e.target.checked })}
                    disabled={!canEditPage("triggers_and_limits")}
                  />
                  {" "}
                  Upper limit
                </span>
                <div className="trigger-limit-controls">
                  <select
                    value={triggerForm.upper_operator}
                    onChange={(e) => setTriggerForm({ ...triggerForm, upper_operator: e.target.value })}
                    disabled={!canEditPage("triggers_and_limits") || !triggerForm.upper_enabled}
                  >
                    <option value=">">{">"}</option>
                    <option value=">=">{">="}</option>
                  </select>
                  <input
                    type="number"
                    step="any"
                    value={triggerForm.upper_value}
                    onChange={(e) => setTriggerForm({ ...triggerForm, upper_value: e.target.value })}
                    disabled={!canEditPage("triggers_and_limits") || !triggerForm.upper_enabled}
                    placeholder="Value"
                  />
                </div>
              </label>
              <label className="remember-row trigger-enabled-row">
                <input
                  type="checkbox"
                  checked={Boolean(triggerForm.enabled)}
                  onChange={(e) => setTriggerForm({ ...triggerForm, enabled: e.target.checked })}
                  disabled={!canEditPage("triggers_and_limits")}
                />
                <span className="remember-label">Enabled</span>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveTriggerRule} disabled={!canEditPage("triggers_and_limits")}>OK</button>
              <button className="btn btn-danger" onClick={() => setShowTriggerModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {showScheduleModal ? (
        <div className="modal-backdrop">
          <div className="modal-card trigger-modal-card">
            <h3>{editingScheduleId ? "Edit Scheduled Report" : "Add Scheduled Report"}</h3>
            <div className="trigger-form-grid">
              <label>
                Name
                <input value={scheduleForm.name} onChange={(e) => setScheduleForm((p) => ({ ...p, name: e.target.value }))} />
              </label>
              <label>
                Recurrence
                <select value={scheduleForm.recurrence} onChange={(e) => setScheduleForm((p) => ({ ...p, recurrence: e.target.value }))}>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>
              <label>
                Hour
                <input type="number" min="0" max="23" value={scheduleForm.hour} onChange={(e) => setScheduleForm((p) => ({ ...p, hour: String(e.target.value || "00").padStart(2, "0") }))} />
              </label>
              <label>
                Minute
                <input type="number" min="0" max="59" value={scheduleForm.minute} onChange={(e) => setScheduleForm((p) => ({ ...p, minute: String(e.target.value || "00").padStart(2, "0") }))} />
              </label>
              {scheduleForm.recurrence === "weekly" ? (
                <label>
                  Day of Week (0-6)
                  <input type="number" min="0" max="6" value={scheduleForm.day_of_week} onChange={(e) => setScheduleForm((p) => ({ ...p, day_of_week: e.target.value }))} />
                </label>
              ) : null}
              {scheduleForm.recurrence === "monthly" ? (
                <label>
                  Day of Month (1-31)
                  <input type="number" min="1" max="31" value={scheduleForm.day_of_month} onChange={(e) => setScheduleForm((p) => ({ ...p, day_of_month: e.target.value }))} />
                </label>
              ) : null}
              <label>
                Format
                <select value={scheduleForm.format} onChange={(e) => setScheduleForm((p) => ({ ...p, format: e.target.value }))}>
                  <option value="csv">CSV</option>
                  <option value="json">Preview JSON</option>
                </select>
              </label>
              <label>
                Recipients (; separated)
                <input value={scheduleForm.recipients} onChange={(e) => setScheduleForm((p) => ({ ...p, recipients: e.target.value }))} />
              </label>
              <label className="remember-row trigger-enabled-row">
                <input type="checkbox" checked={Boolean(scheduleForm.enabled)} onChange={(e) => setScheduleForm((p) => ({ ...p, enabled: e.target.checked }))} />
                <span className="remember-label">Enabled</span>
              </label>
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveScheduledReport}>OK</button>
              <button className="btn btn-danger" onClick={() => setShowScheduleModal(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
      {confirmDialog.open ? (
        <div className="modal-backdrop">
          <div className="modal-card confirm-card">
            <h3>{confirmDialog.title}</h3>
            <p>{confirmDialog.message}</p>
            <div className="row modal-actions">
              <button className="btn btn-danger" onClick={confirmAndRun}>Confirm</button>
              <button className="btn btn-primary" onClick={closeConfirmDialog}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function App() {
  return (
    <AppErrorBoundary>
      <AppShell />
    </AppErrorBoundary>
  );
}


