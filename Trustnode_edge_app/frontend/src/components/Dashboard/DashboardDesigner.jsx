import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DashboardWidgetCard } from "./DashboardWidgets";
import {
  DASHBOARD_GRID_COLS,
  DASHBOARD_GRID_ROWS,
  WIDGET_TYPES,
  getWidgetMeta,
  newWidgetId,
} from "./widgetRegistry";
import { compactWidgets, findFirstFreeSpot, normalizeWidgets, reflowWidgetsForMove, reflowWidgetsForMoveToPoint, reflowWidgetsForResize } from "./layoutUtils";
import { filterRowsByRange, getLatestTagRow, toTsMs } from "./dashboardAnalytics";
import "./dashboard.css";

const TYPE_GROUPS = ["Charts", "KPI", "Content", "Layout", "Media", "System"];
const DASHBOARD_TIME_MODE_KEY = "trustnode_dashboard_time_mode";
const DASHBOARD_TIME_RANGE_KEY = "trustnode_dashboard_time_range";
const DASHBOARD_PROFILES_KEY = "trustnode_dashboard_profiles";
const DASHBOARD_ACTIVE_PROFILE_KEY = "trustnode_dashboard_active_profile";
const DASHBOARD_CAROUSEL_ENABLED_KEY = "trustnode_dashboard_carousel_enabled";
const DASHBOARD_CAROUSEL_INTERVAL_KEY = "trustnode_dashboard_carousel_interval_seconds";

function loadProfilesFromStorage() {
  try {
    const raw = localStorage.getItem(DASHBOARD_PROFILES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((p) => p && typeof p === "object" && typeof p.name === "string" && p.name.trim());
  } catch {
    return [];
  }
}

function saveProfilesToStorage(profiles) {
  try {
    localStorage.setItem(DASHBOARD_PROFILES_KEY, JSON.stringify(Array.isArray(profiles) ? profiles : []));
  } catch {
    // Storage failures are non-fatal; the in-memory list keeps working.
  }
}
const RULE_OPERATORS = [
  { value: "any", label: "Any" },
  { value: "eq", label: "=" },
  { value: "ne", label: "!=" },
  { value: "lt", label: "<" },
  { value: "lte", label: "<=" },
  { value: "gt", label: ">" },
  { value: "gte", label: ">=" },
  { value: "between", label: "Between" },
];
const RULE_AGGREGATIONS = [
  { value: "count", label: "Count" },
  { value: "sum", label: "Sum" },
  { value: "avg", label: "Average" },
  { value: "min", label: "Min" },
  { value: "max", label: "Max" },
  { value: "latest", label: "Latest" },
];
const QUERY_GROUP_OPTIONS = [
  { value: "none", label: "No grouping" },
  { value: "1s", label: "1 second" },
  { value: "5s", label: "5 seconds" },
  { value: "10s", label: "10 seconds" },
  { value: "30s", label: "30 seconds" },
  { value: "1m", label: "1 minute" },
  { value: "5m", label: "5 minutes" },
  { value: "15m", label: "15 minutes" },
  { value: "1h", label: "1 hour" },
  { value: "1d", label: "1 day" },
];
const QUERY_SELECTION_OPTIONS = [
  { value: "all", label: "All rows in range" },
  { value: "last_n", label: "Last N rows" },
];
const QUERY_TIME_PRESET_OPTIONS = [
  { value: "none", label: "No time filter" },
  { value: "5m", label: "Last 5 minutes" },
  { value: "15m", label: "Last 15 minutes" },
  { value: "1h", label: "Last 1 hour" },
  { value: "6h", label: "Last 6 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom range" },
];
const VALUE_FORMAT_OPTIONS = [
  { value: "auto", label: "Auto" },
  { value: "int", label: "Integer" },
  { value: "2dp", label: "2 decimals" },
  { value: "3dp", label: "3 decimals" },
  { value: "scientific", label: "Scientific" },
];
const TABLE_COLUMN_OPTIONS = [
  { value: "ts", label: "Timestamp" },
  { value: "gateway", label: "Gateway" },
  { value: "tag", label: "Tag" },
  { value: "value", label: "Value" },
];

// "Advanced columns" let users build a query that pivots multiple tags into one
// row. Each column is one of:
//   { source: "ts" }                                       -> row timestamp
//   { source: "tag",  tag: "<tag_name>", aggregation }     -> per-tag value
//   { source: "calc", expression: "a-b", refs: ["c1","c2"] -> derived from cols
const TABLE_ADV_SOURCES = [
  { value: "ts", label: "Timestamp" },
  { value: "tag", label: "Tag value" },
  { value: "calc", label: "Calculation" },
];
const TABLE_ADV_AGGREGATIONS = [
  { value: "last", label: "Last (most recent)" },
  { value: "first", label: "First (oldest)" },
  { value: "avg", label: "Average" },
  { value: "min", label: "Minimum" },
  { value: "max", label: "Maximum" },
  { value: "sum", label: "Sum" },
  { value: "count", label: "Count" },
];
const TABLE_WHERE_OPERATORS = [
  { value: "eq", label: "=" },
  { value: "ne", label: "!=" },
  { value: "lt", label: "<" },
  { value: "lte", label: "<=" },
  { value: "gt", label: ">" },
  { value: "gte", label: ">=" },
  { value: "between", label: "Between" },
];
const CHART_INTERPOLATION_OPTIONS = [
  { value: "stepAfter", label: "Step after" },
  { value: "linear", label: "Linear" },
  { value: "monotone", label: "Monotone" },
  { value: "natural", label: "Natural" },
  { value: "stepBefore", label: "Step before" },
];

function newRule(color = "#14a89a") {
  return {
    id: `rule-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    label: "Item",
    gateway_id: "",
    tag_name: "",
    operator: "any",
    value1: "",
    value2: "",
    aggregation: "count",
    color,
  };
}

const RULE_COLOR_PALETTE = [
  "#14a89a",
  "#0e8479",
  "#3cd2c2",
  "#1f3a5f",
  "#6e8dd2",
  "#e0a050",
  "#e2585d",
  "#a78bfa",
  "#22c55e",
  "#f59e0b",
];

function pickNextRuleColor(existingRules = []) {
  const used = new Set(
    (Array.isArray(existingRules) ? existingRules : [])
      .map((r) => String(r?.color || "").toLowerCase())
      .filter(Boolean)
  );
  for (const c of RULE_COLOR_PALETTE) {
    if (!used.has(c.toLowerCase())) return c;
  }
  const idx = Math.max(0, (Array.isArray(existingRules) ? existingRules.length : 0) % RULE_COLOR_PALETTE.length);
  return RULE_COLOR_PALETTE[idx];
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, Number(n) || min));
}

function formatHeaderValue(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return n.toFixed(3);
}

function normalizeDataSourceForType(type, sourceType) {
  const supportsComputed = ["pie_chart", "meter_chart", "table_list", "fixed_text", "value_kpi", "text_kpi"].includes(String(type || ""));
  if (!supportsComputed) return "tag_direct";
  return String(sourceType || "tag_direct") === "computed" ? "computed" : "tag_direct";
}

function parseBoolLike(v, fallback = false) {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") {
    const t = v.trim().toLowerCase();
    if (t === "true") return true;
    if (t === "false") return false;
    if (t === "1" || t === "yes" || t === "y" || t === "on") return true;
    if (t === "0" || t === "no" || t === "n" || t === "off") return false;
  }
  if (typeof v === "number") return v !== 0;
  return fallback;
}

function parseLegendLayoutLike(v, fallback = "side") {
  const t = String(v || "").trim().toLowerCase();
  if (!t) return fallback;
  return t.includes("bottom") ? "bottom" : "side";
}

function newMeterRange(existingRules = []) {
  const idx = Array.isArray(existingRules) ? existingRules.length : 0;
  const defaults = [
    { label: "Low", min: "0", max: "100", color: "#e0a050" },
    { label: "Normal", min: "100", max: "130", color: "#22c55e" },
    { label: "High", min: "130", max: "200", color: "#e2585d" },
  ];
  const d = defaults[Math.min(idx, defaults.length - 1)] || defaults[0];
  return {
    id: `meter-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    label: d.label,
    gateway_id: "",
    tag_name: "",
    operator: "between",
    value1: d.min,
    value2: d.max,
    aggregation: "count",
    color: d.color,
  };
}

function makeAdvColumnId() {
  return `col-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}
function makeWhereCondId() {
  return `whr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function TableWhereConditions({ form, setForm, selectedGatewayTags, formatTagForDisplay }) {
  const list = Array.isArray(form.config.query_where_conditions) ? form.config.query_where_conditions : [];

  const updateList = (next) => {
    setForm((p) => ({ ...p, config: { ...p.config, query_where_conditions: next } }));
  };
  const update = (idx, patch) => updateList(list.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  const remove = (idx) => updateList(list.filter((_, i) => i !== idx));
  const add = () => updateList([
    ...list,
    { id: makeWhereCondId(), tag: "", operator: "eq", value: "", value2: "", enabled: true },
  ]);

  return (
    <fieldset className="dashboard-query-fieldset">
      <legend>Where conditions</legend>
      <p className="dashboard-query-hint">
        Only show rows where another tag matches a condition. Example: "tag1 column where SimREAL[3] = 1".
      </p>
      <div className="dashboard-query-rule-list">
        {list.length === 0 ? (
          <span className="dashboard-query-empty">No conditions — every row passes.</span>
        ) : list.map((c, idx) => (
          <div key={c.id || idx} className="dashboard-query-rule-row">
            <select
              value={c.tag || ""}
              onChange={(e) => update(idx, { tag: e.target.value })}
            >
              <option value="">Tag…</option>
              {selectedGatewayTags.map((t) => (
                <option key={t} value={t}>{formatTagForDisplay ? formatTagForDisplay(t) : t}</option>
              ))}
            </select>
            <select
              value={c.operator || "eq"}
              onChange={(e) => update(idx, { operator: e.target.value })}
            >
              {[
                { value: "eq", label: "=" },
                { value: "ne", label: "!=" },
                { value: "lt", label: "<" },
                { value: "lte", label: "<=" },
                { value: "gt", label: ">" },
                { value: "gte", label: ">=" },
                { value: "between", label: "Between" },
              ].map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <input
              value={c.value ?? ""}
              placeholder="Value"
              onChange={(e) => update(idx, { value: e.target.value })}
            />
            {c.operator === "between" ? (
              <input
                value={c.value2 ?? ""}
                placeholder="Value 2"
                onChange={(e) => update(idx, { value2: e.target.value })}
              />
            ) : <span />}
            <button type="button" className="icon-btn icon-btn-danger" title="Remove" onClick={() => remove(idx)}>
              ×
            </button>
          </div>
        ))}
      </div>
      <button type="button" className="dashboard-link-btn" onClick={add}>+ Add condition</button>
    </fieldset>
  );
}

function TableAdvancedColumns({ form, setForm, selectedGatewayTags, formatTagForDisplay }) {
  const list = Array.isArray(form.config.query_advanced_columns) ? form.config.query_advanced_columns : [];

  const updateList = (next) => {
    setForm((p) => ({ ...p, config: { ...p.config, query_advanced_columns: next } }));
  };
  const update = (idx, patch) => updateList(list.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  const remove = (idx) => updateList(list.filter((_, i) => i !== idx));
  const move = (idx, dir) => {
    const target = idx + dir;
    if (target < 0 || target >= list.length) return;
    const next = [...list];
    [next[idx], next[target]] = [next[target], next[idx]];
    updateList(next);
  };
  const addTimestamp = () => updateList([
    ...list,
    { id: makeAdvColumnId(), source: "ts", header: "Timestamp" },
  ]);
  const addTag = () => updateList([
    ...list,
    {
      id: makeAdvColumnId(),
      source: "tag",
      header: "",
      tag: selectedGatewayTags[0] || "",
      aggregation: "last",
    },
  ]);
  const addCalc = () => updateList([
    ...list,
    {
      id: makeAdvColumnId(),
      source: "calc",
      header: "Calculated",
      expression: "a + b",
      refs: list.filter((c) => c.source === "tag").slice(0, 2).map((c) => c.id),
    },
  ]);

  return (
    <fieldset className="dashboard-query-fieldset">
      <legend>Advanced columns</legend>
      <p className="dashboard-query-hint">
        When at least one row exists below, the table uses these columns instead of "Simple columns".
        Each column pulls one tag value (last / avg / min / max …) or computes from earlier columns
        (reference them as <code>a</code>, <code>b</code>, <code>c</code> in the expression).
      </p>
      <div className="dashboard-query-col-list">
        {list.length === 0 ? (
          <span className="dashboard-query-empty">No advanced columns. Add one to pivot tags into one row.</span>
        ) : list.map((c, idx) => (
          <div key={c.id || idx} className="dashboard-query-col-row">
            <input
              value={c.header || ""}
              placeholder="Column header"
              onChange={(e) => update(idx, { header: e.target.value })}
            />
            <select
              value={c.source || "tag"}
              onChange={(e) => update(idx, { source: e.target.value })}
            >
              {TABLE_ADV_SOURCES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            {c.source === "tag" ? (
              <>
                <select
                  value={c.tag || ""}
                  onChange={(e) => update(idx, { tag: e.target.value })}
                >
                  <option value="">Tag…</option>
                  {selectedGatewayTags.map((t) => (
                    <option key={t} value={t}>{formatTagForDisplay ? formatTagForDisplay(t) : t}</option>
                  ))}
                </select>
                <select
                  value={c.aggregation || "last"}
                  onChange={(e) => update(idx, { aggregation: e.target.value })}
                >
                  {TABLE_ADV_AGGREGATIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </>
            ) : c.source === "calc" ? (
              <>
                <input
                  value={c.expression || ""}
                  placeholder="a + b"
                  onChange={(e) => update(idx, { expression: e.target.value })}
                  title="Reference earlier tag/calc columns as a, b, c… in their display order."
                />
                <span className="dashboard-query-col-hint">
                  Refs: {list.filter((x) => x.source !== "ts").slice(0, 6).map((x, i) => `${String.fromCharCode(97 + i)}=${x.header || x.tag || "?"}`).join(", ") || "(none)"}
                </span>
              </>
            ) : (
              <>
                <span />
                <span />
              </>
            )}
            <div className="dashboard-query-col-tools">
              <button type="button" className="icon-btn" title="Move up" onClick={() => move(idx, -1)} disabled={idx === 0}>↑</button>
              <button type="button" className="icon-btn" title="Move down" onClick={() => move(idx, 1)} disabled={idx === list.length - 1}>↓</button>
              <button type="button" className="icon-btn icon-btn-danger" title="Remove" onClick={() => remove(idx)}>×</button>
            </div>
          </div>
        ))}
      </div>
      <div className="dashboard-query-col-actions">
        <button type="button" className="dashboard-link-btn" onClick={addTimestamp}>+ Timestamp</button>
        <button type="button" className="dashboard-link-btn" onClick={addTag}>+ Tag value</button>
        <button type="button" className="dashboard-link-btn" onClick={addCalc}>+ Calculation</button>
      </div>
    </fieldset>
  );
}

function buildDefaultForm(type = "line_chart") {
  const meta = getWidgetMeta(type);
  return {
    type,
    title: meta.label,
    color: "#14a89a",
    w: meta.defaultSize.w,
    h: meta.defaultSize.h,
    x: null,
    y: null,
    config: {
      gateway_id: "",
      tag_name: "",
      readings_count: 120,
      interpolation: "stepAfter",
      data_source_type: "tag_direct",
      color_mode: "default",
      text: "",
      source_url: "",
      camera_url: "",
      list_limit: 8,
      query_group_interval: "none",
      query_result_aggregation: "count",
      query_row_selection: "all",
      query_row_limit: 200,
      query_rule_logic: "any",
      query_time_filter_preset: "none",
      query_time_filter_from: "",
      query_time_filter_to: "",
      query_table_columns: ["ts", "tag", "value"],
      compute_rules: [],
      pie_show_legend: true,
      pie_show_labels: true,
      pie_show_count: true,
      pie_show_percent: true,
      pie_legend_layout: "side",
      chart_show_legend: false,
      chart_show_point_labels: false,
      chart_value_format: "auto",
      meter_show_legend: true,
      meter_legend_layout: "side",
      text_font_scale: 1,
      table_filter_tags: [],
    },
  };
}

export function DashboardDesigner({
  canEdit,
  widgets,
  setWidgets,
  dashboardMode = "kpi",
  dashboardPerRow = 2,
  dashboardTagColors = {},
  setDashboardMode = null,
  setDashboardPerRow = null,
  setDashboardTagColors = null,
  tagRows,
  dataLogView,
  formatTagForDisplay,
  fetchHistoricalRows,
  gatewayCatalog,
  tagsByGateway,
  gatewayIntervalsById = {},
  gatewaysIndex = null,
  showGridMeta = true,
  onOpenTagMonitor,
  widgetLatencyById = {},
  diagnosticsSummary = null,
  onExportDiagnostics,
  fetchWidgetRows,
  fetchWidgetStats,
  fetchWidgetRuleStats,
}) {
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState("type");
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(buildDefaultForm("line_chart"));
  const [draggingId, setDraggingId] = useState("");
  const [menuWidgetId, setMenuWidgetId] = useState("");
  const [configModalOpen, setConfigModalOpen] = useState(false);
  const [pendingImportWidgets, setPendingImportWidgets] = useState(null);
  const [pendingImportName, setPendingImportName] = useState("");
  const [dashboardTimeMode, setDashboardTimeMode] = useState("live");
  const [dashboardFrom, setDashboardFrom] = useState("");
  const [dashboardTo, setDashboardTo] = useState("");
  // ------------------------------------------------------------------------
  // Dashboard profiles: named snapshots of the current layout (widgets +
  // grid mode / cols + tag colors). Stored client-side in localStorage so
  // they survive reloads and follow the user's machine. Each profile is
  // independent — switching profiles fully replaces the active dashboard.
  // ------------------------------------------------------------------------
  const [profiles, setProfiles] = useState(() => loadProfilesFromStorage());
  const [activeProfileName, setActiveProfileName] = useState(() => {
    try {
      return String(localStorage.getItem(DASHBOARD_ACTIVE_PROFILE_KEY) || "");
    } catch {
      return "";
    }
  });
  useEffect(() => {
    try { localStorage.setItem(DASHBOARD_ACTIVE_PROFILE_KEY, activeProfileName || ""); } catch {}
  }, [activeProfileName]);

  // Carousel mode: when enabled the dashboard cycles through every saved
  // profile on a timer. Useful for control-room TVs where you want to see
  // every shift / area in rotation without anyone manually clicking.
  const [carouselEnabled, setCarouselEnabled] = useState(() => {
    try { return localStorage.getItem(DASHBOARD_CAROUSEL_ENABLED_KEY) === "1"; } catch { return false; }
  });
  const [carouselIntervalSec, setCarouselIntervalSec] = useState(() => {
    try {
      const raw = Number(localStorage.getItem(DASHBOARD_CAROUSEL_INTERVAL_KEY) || 0);
      return Number.isFinite(raw) && raw >= 5 ? raw : 30;
    } catch { return 30; }
  });
  useEffect(() => {
    try { localStorage.setItem(DASHBOARD_CAROUSEL_ENABLED_KEY, carouselEnabled ? "1" : "0"); } catch {}
  }, [carouselEnabled]);
  useEffect(() => {
    try { localStorage.setItem(DASHBOARD_CAROUSEL_INTERVAL_KEY, String(carouselIntervalSec)); } catch {}
  }, [carouselIntervalSec]);

  const persistProfiles = useCallback((next) => {
    setProfiles(next);
    saveProfilesToStorage(next);
  }, []);

  const captureCurrentProfilePayload = useCallback((name) => ({
    name: String(name || "").trim(),
    saved_utc: new Date().toISOString(),
    widgets: Array.isArray(widgets) ? widgets : [],
    mode: dashboardMode,
    per_row: dashboardPerRow,
    tag_colors: dashboardTagColors || {},
  }), [widgets, dashboardMode, dashboardPerRow, dashboardTagColors]);

  const applyProfilePayload = useCallback((payload) => {
    if (!payload || typeof payload !== "object") return;
    if (Array.isArray(payload.widgets)) {
      setWidgets(compactWidgets(normalizeWidgets(payload.widgets)));
    }
    if (typeof payload.mode === "string" && setDashboardMode) setDashboardMode(payload.mode);
    if (Number.isFinite(Number(payload.per_row)) && setDashboardPerRow) {
      setDashboardPerRow(Number(payload.per_row));
    }
    if (payload.tag_colors && typeof payload.tag_colors === "object" && setDashboardTagColors) {
      setDashboardTagColors(payload.tag_colors);
    }
  }, [setWidgets, setDashboardMode, setDashboardPerRow, setDashboardTagColors]);

  const handleLoadProfile = useCallback((name) => {
    const cleanName = String(name || "").trim();
    if (!cleanName) {
      setActiveProfileName("");
      return;
    }
    const found = profiles.find((p) => p.name === cleanName);
    if (!found) return;
    applyProfilePayload(found);
    setActiveProfileName(cleanName);
  }, [profiles, applyProfilePayload]);

  // Electron's BrowserWindow disables window.prompt() — that's why the
  // original "Save as…" flow appeared to do nothing (prompt returned null
  // silently and we bailed out). Replace it with a tiny in-app prompt modal
  // controlled by these two state slots.
  const [profilePromptOpen, setProfilePromptOpen] = useState(false);
  const [profilePromptName, setProfilePromptName] = useState("");
  const profilePromptResolveRef = useRef(null);
  const askProfileName = useCallback((suggested = "") => {
    return new Promise((resolve) => {
      profilePromptResolveRef.current = resolve;
      setProfilePromptName(String(suggested || ""));
      setProfilePromptOpen(true);
    });
  }, []);
  const finishProfilePrompt = useCallback((value) => {
    setProfilePromptOpen(false);
    const fn = profilePromptResolveRef.current;
    profilePromptResolveRef.current = null;
    if (fn) fn(value);
  }, []);

  const handleSaveAsProfile = useCallback(async () => {
    const suggested = activeProfileName || "";
    const rawName = await askProfileName(suggested);
    const name = String(rawName || "").trim();
    if (!name) return;
    const existing = profiles.find((p) => p.name === name);
    if (existing) {
      // window.confirm works in Electron but to keep behavior consistent
      // with the new in-app prompt, use it only as a backstop. If the user
      // dismisses, abort.
      let proceed = true;
      try { proceed = window.confirm(`Overwrite existing profile "${name}"?`); } catch { proceed = true; }
      if (!proceed) return;
    }
    const payload = captureCurrentProfilePayload(name);
    const next = existing
      ? profiles.map((p) => (p.name === name ? payload : p))
      : [...profiles, payload];
    persistProfiles(next);
    setActiveProfileName(name);
  }, [activeProfileName, profiles, captureCurrentProfilePayload, persistProfiles, askProfileName]);

  const handleSaveProfile = useCallback(() => {
    const name = activeProfileName;
    if (!name) {
      handleSaveAsProfile();
      return;
    }
    const payload = captureCurrentProfilePayload(name);
    const exists = profiles.some((p) => p.name === name);
    const next = exists ? profiles.map((p) => (p.name === name ? payload : p)) : [...profiles, payload];
    persistProfiles(next);
  }, [activeProfileName, profiles, captureCurrentProfilePayload, persistProfiles, handleSaveAsProfile]);

  /**
   * Pan the dashboard's historical window left (older) or right (newer) by a
   * number of *windows*. Used by chart drag-to-scroll: a drag on any chart
   * shifts the shared dashboard from/to so every widget pans in lockstep.
   *
   * Behavior:
   *   - Only fires when in Historical time mode.
   *   - When no explicit from/to is set yet, the function snaps to "now" and
   *     scrolls back by one window (default 30 minutes) so the first drag
   *     immediately produces a visible historical view.
   *   - One drag = one window shift; the window width is preserved.
   */
  const panHistoricalWindow = useCallback((direction) => {
    const step = Math.sign(Number(direction) || 0);
    if (step === 0) return;
    if (dashboardTimeMode !== "historical") return;
    const dtLocalToMs = (s) => {
      const t = String(s || "").trim();
      if (!t) return NaN;
      const ms = Date.parse(t);
      return Number.isFinite(ms) ? ms : NaN;
    };
    const msToDtLocal = (ms) => {
      const d = new Date(ms);
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    let fromMs = dtLocalToMs(dashboardFrom);
    let toMs = dtLocalToMs(dashboardTo);
    const defaultSpan = 30 * 60 * 1000; // 30 minutes
    if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || toMs <= fromMs) {
      const now = Date.now();
      toMs = now;
      fromMs = now - defaultSpan;
    }
    const span = Math.max(60 * 1000, toMs - fromMs);
    // Drag right (positive direction) → go BACK in time (operator pulls past
    // history toward them). Drag left → go forward.
    const shift = span * step * -1;
    fromMs += shift;
    toMs += shift;
    setDashboardFrom(msToDtLocal(fromMs));
    setDashboardTo(msToDtLocal(toMs));
  }, [dashboardTimeMode, dashboardFrom, dashboardTo, setDashboardFrom, setDashboardTo]);

  const handleDeleteProfile = useCallback(() => {
    const name = activeProfileName;
    if (!name) return;
    let proceed = true;
    try { proceed = window.confirm(`Delete profile "${name}"? This cannot be undone.`); } catch { proceed = true; }
    if (!proceed) return;
    persistProfiles(profiles.filter((p) => p.name !== name));
    setActiveProfileName("");
  }, [activeProfileName, profiles, persistProfiles]);

  // Carousel driver: when enabled, advance to the next profile every N
  // seconds. Quiet when fewer than two profiles are configured (nothing to
  // rotate to) or when the user toggles it off. Pauses if the page is
  // hidden / minimized to avoid wasted re-renders on a TV that's off.
  useEffect(() => {
    if (!carouselEnabled) return;
    if (!Array.isArray(profiles) || profiles.length < 2) return;
    const intervalMs = Math.max(5, Number(carouselIntervalSec) || 30) * 1000;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.hidden) return; // pause when offscreen
      // Compute the next profile name from the current active one each tick
      // (don't capture activeProfileName at effect-mount time, otherwise
      // changing it from carousel wouldn't advance further).
      let currentName = "";
      try { currentName = String(localStorage.getItem(DASHBOARD_ACTIVE_PROFILE_KEY) || ""); } catch {}
      const idx = profiles.findIndex((p) => p.name === currentName);
      const nextIdx = idx < 0 ? 0 : (idx + 1) % profiles.length;
      const next = profiles[nextIdx];
      if (next && next.name && next.name !== currentName) {
        // Reuse the normal load path so the layout, mode, columns and tag
        // colors all swap exactly as if the operator picked the profile.
        applyProfilePayload(next);
        setActiveProfileName(next.name);
      }
    };
    const timer = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [carouselEnabled, carouselIntervalSec, profiles, applyProfilePayload]);
  const [historicalRows, setHistoricalRows] = useState([]);
  const [historicalRangeKey, setHistoricalRangeKey] = useState("");
  const [historicalLoading, setHistoricalLoading] = useState(false);
  const [historicalError, setHistoricalError] = useState("");
  const [liveBootstrapRows, setLiveBootstrapRows] = useState([]);
  const [liveBootstrapLoading, setLiveBootstrapLoading] = useState(false);
  const [queryModalOpen, setQueryModalOpen] = useState(false);
  const [computedModalOpen, setComputedModalOpen] = useState(false);
  const [resizingId, setResizingId] = useState("");
  const gridRef = useRef(null);
  const importInputRef = useRef(null);
  const liveApplyDebounceRef = useRef(null);

  const normalizedWidgets = useMemo(() => normalizeWidgets(widgets), [widgets]);
  const tagRowsByGateway = useMemo(() => {
    const out = {};
    for (const row of Array.isArray(tagRows) ? tagRows : []) {
      const key = String(row.gateway_id || "");
      if (!out[key]) out[key] = [];
      out[key].push(row);
    }
    return out;
  }, [tagRows]);

  const rangeKey = `${dashboardFrom || ""}|${dashboardTo || ""}`;
  const hasHistoricalRange = Boolean(dashboardFrom || dashboardTo);
  const localHistoricalRows = useMemo(
    () => filterRowsByRange(dataLogView, dashboardFrom, dashboardTo),
    [dataLogView, dashboardFrom, dashboardTo]
  );

  const dashboardRows = useMemo(() => {
    if (dashboardTimeMode !== "historical") {
      const liveRows = Array.isArray(dataLogView) ? dataLogView : [];
      if (liveRows.length) return liveRows;
      return Array.isArray(liveBootstrapRows) ? liveBootstrapRows : [];
    }
    if (hasHistoricalRange && historicalRangeKey === rangeKey && Array.isArray(historicalRows)) return historicalRows;
    return localHistoricalRows;
  }, [dashboardTimeMode, dataLogView, liveBootstrapRows, hasHistoricalRange, historicalRangeKey, rangeKey, historicalRows, localHistoricalRows]);

  useEffect(() => {
    let cancelled = false;
    if (dashboardTimeMode !== "live") return () => {};
    if (Array.isArray(dataLogView) && dataLogView.length > 0) {
      setLiveBootstrapRows([]);
      setLiveBootstrapLoading(false);
      return () => {};
    }
    if (typeof fetchHistoricalRows !== "function") return () => {};
    setLiveBootstrapLoading(true);
    (async () => {
      try {
        const rows = await fetchHistoricalRows({ fromUtc: "", toUtc: "", limit: 5000 });
        if (cancelled) return;
        setLiveBootstrapRows(Array.isArray(rows) ? rows : []);
      } catch {
        if (cancelled) return;
        setLiveBootstrapRows([]);
      } finally {
        if (!cancelled) setLiveBootstrapLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [dashboardTimeMode, dataLogView, fetchHistoricalRows]);

  const dashboardTagRowsByGateway = useMemo(() => {
    const byKey = new Map();
    for (const row of dashboardRows) {
      const gid = String(row?.gateway_id || "").trim();
      const tag = String(row?.tag || row?.tag_name || "").trim();
      if (!gid || !tag) continue;
      const key = `${gid}::${tag}`;
      const ts = toTsMs(row?.ts);
      const prev = byKey.get(key);
      if (!prev || ts > prev.ts) {
        byKey.set(key, {
          ts,
          row: {
            gateway_id: gid,
            gateway_name: row?.gateway_name || gid,
            tag_name: tag,
            last_value: row?.value,
            last_ts: row?.ts || "",
          },
        });
      }
    }
    const out = {};
    for (const payload of byKey.values()) {
      const gid = payload.row.gateway_id;
      if (!out[gid]) out[gid] = [];
      out[gid].push(payload.row);
    }
    return out;
  }, [dashboardRows]);

  const widgetHeaderParts = (widget) => {
    const typeLabel = String(widget?.title || getWidgetMeta(widget?.type)?.label || widget?.type || "-");
    const cfg = widget?.config || {};
    const isChartWidget = ["line_chart", "line_area_chart", "bar_chart", "pie_chart", "meter_chart"].includes(String(widget?.type || ""));
    if (!isChartWidget) return null;
    const gatewayId = String(cfg.gateway_id || "");
    const tagName = String(cfg.tag_name || "");
    const latest = getLatestTagRow(dashboardRows, gatewayId, tagName);
    const latestValue = formatHeaderValue(latest?.last_value);
    const plcTag = formatTagForDisplay ? formatTagForDisplay(tagName) : tagName || "-";
    const lastTsMs = toTsMs(latest?.last_ts || "");
    const liveLatencyMs = Number.isFinite(lastTsMs) ? Math.max(0, Date.now() - lastTsMs) : null;
    // Build a list of every visible series (primary + series_extra data
    // traces, skip limit-lines) so the title strip can show one
    // "value | tag" pair per series — including when the primary
    // gateway/tag is unset and the widget is series-only.
    const seriesItems = [];
    if (gatewayId && tagName) {
      seriesItems.push({ value: latestValue, tag: plcTag, color: String(widget?.color || "#14a89a") });
    }
    const extras = Array.isArray(cfg.series_extra) ? cfg.series_extra : [];
    const fallbackPalette = ["#f97316", "#3b82f6", "#a855f7", "#dc2626", "#10b981", "#f59e0b"];
    let paletteIdx = 0;
    for (const s of extras) {
      const chartType = String(s?.chart_type || "").toLowerCase();
      if (chartType === "limit") continue;
      const sTag = String(s?.tag_name || "").trim();
      if (!sTag) continue;
      const sGw = String(s?.gateway_id || gatewayId || "").trim();
      const sLatest = getLatestTagRow(dashboardRows, sGw, sTag);
      const sValue = formatHeaderValue(sLatest?.last_value);
      const sLabel = String(s?.label || "").trim() || (formatTagForDisplay ? formatTagForDisplay(sTag) : sTag);
      seriesItems.push({
        value: sValue,
        tag: sLabel,
        color: String(s?.color || "").trim() || fallbackPalette[paletteIdx % fallbackPalette.length],
      });
      paletteIdx += 1;
    }
    return { latestValue, plcTag, typeLabel, liveLatencyMs, seriesItems };
  };

  const formatLatencyLabel = (msValue) => {
    const ms = Number(msValue);
    if (!Number.isFinite(ms) || ms < 0) return "-";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
  };

  useEffect(() => {
    try {
      const savedMode = localStorage.getItem(DASHBOARD_TIME_MODE_KEY);
      if (savedMode === "live" || savedMode === "historical") setDashboardTimeMode(savedMode);
      const savedRangeRaw = localStorage.getItem(DASHBOARD_TIME_RANGE_KEY);
      if (savedRangeRaw) {
        const savedRange = JSON.parse(savedRangeRaw);
        setDashboardFrom(String(savedRange?.from || ""));
        setDashboardTo(String(savedRange?.to || ""));
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(DASHBOARD_TIME_MODE_KEY, dashboardTimeMode);
      localStorage.setItem(DASHBOARD_TIME_RANGE_KEY, JSON.stringify({ from: dashboardFrom, to: dashboardTo }));
    } catch {}
  }, [dashboardTimeMode, dashboardFrom, dashboardTo]);

  useEffect(() => {
    if (dashboardTimeMode !== "historical") return;
    if (!hasHistoricalRange) {
      setHistoricalRows([]);
      setHistoricalRangeKey("");
      setHistoricalError("");
      setHistoricalLoading(false);
      return;
    }
    if (typeof fetchHistoricalRows !== "function") return;

    const toUtcIso = (value) => {
      const txt = String(value || "").trim();
      if (!txt) return "";
      const dt = new Date(txt);
      if (!Number.isFinite(dt.getTime())) return "";
      return dt.toISOString();
    };

    let canceled = false;
    const targetKey = rangeKey;
    const timer = setTimeout(async () => {
      try {
        setHistoricalLoading(true);
        setHistoricalError("");
        const rows = await fetchHistoricalRows({
          fromUtc: toUtcIso(dashboardFrom),
          toUtc: toUtcIso(dashboardTo),
        });
        if (canceled) return;
        setHistoricalRows(Array.isArray(rows) ? rows : []);
        setHistoricalRangeKey(targetKey);
      } catch (err) {
        if (canceled) return;
        setHistoricalError(String(err?.message || err || "Failed to load historical dashboard data."));
      } finally {
        if (!canceled) setHistoricalLoading(false);
      }
    }, 220);

    return () => {
      canceled = true;
      clearTimeout(timer);
    };
  }, [dashboardTimeMode, hasHistoricalRange, dashboardFrom, dashboardTo, rangeKey, fetchHistoricalRows]);

  const gatewayOptions = useMemo(() => {
    const map = new Map();
    const configured = Array.isArray(gatewayCatalog) ? gatewayCatalog : [];
    const configuredTags = tagsByGateway && typeof tagsByGateway === "object" ? tagsByGateway : {};

    for (const g of configured) {
      const id = String(g?.id || "").trim();
      if (!id) continue;
      const name = String(g?.name || id).trim() || id;
      const tags = Array.from(
        new Set((Array.isArray(configuredTags[id]) ? configuredTags[id] : []).map((t) => String(t || "").trim()).filter(Boolean))
      );
      map.set(id, { id, name, tags });
    }

    for (const [id, rows] of Object.entries(tagRowsByGateway)) {
      const gid = String(id || "").trim();
      if (!gid) continue;
      const observed = Array.isArray(rows)
        ? Array.from(new Set(rows.map((r) => String(r?.tag_name || "").trim()).filter(Boolean)))
        : [];
      if (!map.has(gid)) {
        map.set(gid, {
          id: gid,
          name: String(rows?.[0]?.gateway_name || gid),
          tags: observed,
        });
        continue;
      }
      const current = map.get(gid);
      current.tags = Array.from(new Set([...(current.tags || []), ...observed]));
      if (!current.name && rows?.[0]?.gateway_name) current.name = String(rows[0].gateway_name);
      map.set(gid, current);
    }
    return Array.from(map.values());
  }, [tagRowsByGateway, gatewayCatalog, tagsByGateway]);

  const openCreate = () => {
    setEditingId(null);
    setForm(buildDefaultForm("line_chart"));
    setTab("type");
    setModalOpen(true);
  };

  const openEdit = (widget) => {
    const meta = getWidgetMeta(widget.type);
    setEditingId(widget.id);
    setForm({
      type: widget.type,
      title: widget.title || meta.label,
      color: widget.color || "#14a89a",
      w: widget.w,
      h: widget.h,
      x: widget.x,
      y: widget.y,
      config: {
        gateway_id: widget?.config?.gateway_id || "",
        tag_name: widget?.config?.tag_name || "",
        readings_count: clamp(widget?.config?.readings_count ?? 120, 20, 500),
        interpolation: CHART_INTERPOLATION_OPTIONS.some((opt) => opt.value === widget?.config?.interpolation)
          ? widget?.config?.interpolation
          : "stepAfter",
        data_source_type: normalizeDataSourceForType(widget?.type, widget?.config?.data_source_type),
        color_mode: widget?.config?.color_mode === "custom" ? "custom" : "default",
        text: widget?.config?.text || "",
        source_url: widget?.config?.source_url || "",
        camera_url: widget?.config?.camera_url || "",
        list_limit: clamp(widget?.config?.list_limit ?? 8, 1, 50),
        query_group_interval: QUERY_GROUP_OPTIONS.some((opt) => opt.value === widget?.config?.query_group_interval)
          ? widget?.config?.query_group_interval
          : "none",
        query_result_aggregation: RULE_AGGREGATIONS.some((opt) => opt.value === widget?.config?.query_result_aggregation)
          ? widget?.config?.query_result_aggregation
          : "count",
        query_row_selection: QUERY_SELECTION_OPTIONS.some((opt) => opt.value === widget?.config?.query_row_selection)
          ? widget?.config?.query_row_selection
          : "all",
        query_row_limit: clamp(widget?.config?.query_row_limit ?? 200, 10, 5000),
        query_rule_logic: String(widget?.config?.query_rule_logic || "any") === "all" ? "all" : "any",
        query_time_filter_preset: QUERY_TIME_PRESET_OPTIONS.some((opt) => opt.value === widget?.config?.query_time_filter_preset)
          ? widget?.config?.query_time_filter_preset
          : "none",
        query_time_filter_from: String(widget?.config?.query_time_filter_from || ""),
        query_time_filter_to: String(widget?.config?.query_time_filter_to || ""),
        query_table_columns: Array.isArray(widget?.config?.query_table_columns)
          ? widget.config.query_table_columns.filter((c) => TABLE_COLUMN_OPTIONS.some((opt) => opt.value === c))
          : ["ts", "tag", "value"],
        compute_rules: Array.isArray(widget?.config?.compute_rules) ? widget.config.compute_rules : [],
        pie_show_legend: parseBoolLike(widget?.config?.pie_show_legend, true),
        pie_show_labels: parseBoolLike(widget?.config?.pie_show_labels, true),
        pie_show_count: parseBoolLike(widget?.config?.pie_show_count, true),
        pie_show_percent: parseBoolLike(widget?.config?.pie_show_percent, true),
        pie_legend_layout: parseLegendLayoutLike(widget?.config?.pie_legend_layout, "side"),
        meter_show_legend: parseBoolLike(widget?.config?.meter_show_legend, true),
        meter_legend_layout: parseLegendLayoutLike(widget?.config?.meter_legend_layout, "side"),
        chart_show_legend: parseBoolLike(widget?.config?.chart_show_legend, false),
        chart_show_point_labels: parseBoolLike(widget?.config?.chart_show_point_labels, false),
        chart_value_format: VALUE_FORMAT_OPTIONS.some((opt) => opt.value === widget?.config?.chart_value_format)
          ? widget?.config?.chart_value_format
          : "auto",
        text_font_scale: clamp(widget?.config?.text_font_scale ?? 1, 0.7, 2.5),
        table_filter_tags: Array.isArray(widget?.config?.table_filter_tags)
          ? widget.config.table_filter_tags.map((t) => String(t || "")).filter(Boolean)
          : [],
        query_where_conditions: Array.isArray(widget?.config?.query_where_conditions)
          ? widget.config.query_where_conditions
          : [],
        query_advanced_columns: Array.isArray(widget?.config?.query_advanced_columns)
          ? widget.config.query_advanced_columns
          : [],
        series_extra: Array.isArray(widget?.config?.series_extra)
          ? widget.config.series_extra
          : [],
        primary_unit: String(widget?.config?.primary_unit || ""),
        primary_suffix: String(widget?.config?.primary_suffix || ""),
        y_axis_label: String(widget?.config?.y_axis_label || ""),
        y_axis_right_label: String(widget?.config?.y_axis_right_label || ""),
        chart_line_width: clamp(widget?.config?.chart_line_width ?? 2, 1, 8),
        chart_line_dot: String(widget?.config?.chart_line_dot || "none"),
        chart_bar_opacity: clamp(widget?.config?.chart_bar_opacity ?? 100, 10, 100),
        chart_bar_pattern: String(widget?.config?.chart_bar_pattern || "solid"),
        chart_bar_width: clamp(widget?.config?.chart_bar_width ?? 0, 0, 120),
      },
    });
    setTab("config");
    setModalOpen(true);
  };

  const removeWidget = (id) => {
    if (!canEdit) return;
    setWidgets((prev) => normalizeWidgets(prev).filter((w) => w.id !== id));
  };

  /**
   * Clone an existing widget with all its config (tag, axes, series_extra,
   * colors, query options). Asks the operator for an optional replacement
   * primary tag — leaving it blank keeps the same tag so they can change it
   * later from the widget editor. The new widget lands in the first free
   * grid slot to avoid overlapping the original.
   */
  const duplicateWidget = (id) => {
    if (!canEdit) return;
    const source = normalizeWidgets(widgets).find((w) => String(w.id) === String(id));
    if (!source) return;

    const currentTag = String(source?.config?.tag_name || "");
    const promptMsg = currentTag
      ? `Duplicate "${source.title || source.type}".\n\nReplacement tag (leave blank to keep "${currentTag}"; you can also change it later):`
      : `Duplicate "${source.title || source.type}".\n\nTag for the copy (optional — leave blank to set later):`;
    let replacementTag;
    try {
      replacementTag = window.prompt(promptMsg, "");
    } catch {
      replacementTag = "";
    }
    if (replacementTag === null) return; // user cancelled
    const nextTag = String(replacementTag || "").trim() || currentTag;

    const meta = getWidgetMeta(source.type);
    const sourceTitle = String(source.title || meta.label || "Widget");
    const newTitle = sourceTitle.toLowerCase().endsWith("(copy)")
      ? sourceTitle
      : `${sourceTitle} (copy)`;

    const cloned = {
      ...source,
      id: newWidgetId(),
      title: newTitle,
      // Deep-ish copy of the config so future edits to the copy don't mutate
      // the source's series_extra / compute_rules / table columns arrays.
      config: {
        ...(source.config || {}),
        tag_name: nextTag,
        series_extra: Array.isArray(source?.config?.series_extra)
          ? source.config.series_extra.map((s) => ({
              ...(s || {}),
              id: `s${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`,
            }))
          : [],
        compute_rules: Array.isArray(source?.config?.compute_rules)
          ? source.config.compute_rules.map((r) => ({ ...(r || {}) }))
          : [],
        query_table_columns: Array.isArray(source?.config?.query_table_columns)
          ? [...source.config.query_table_columns]
          : ["ts", "tag", "value"],
        table_filter_tags: Array.isArray(source?.config?.table_filter_tags)
          ? [...source.config.table_filter_tags]
          : [],
        query_where_conditions: Array.isArray(source?.config?.query_where_conditions)
          ? source.config.query_where_conditions.map((r) => ({ ...(r || {}) }))
          : [],
        query_advanced_columns: Array.isArray(source?.config?.query_advanced_columns)
          ? source.config.query_advanced_columns.map((r) => ({ ...(r || {}) }))
          : [],
      },
      // Drop the saved position so findFirstFreeSpot places the copy where
      // it won't overlap the original.
      x: null,
      y: null,
    };

    setWidgets((prev) => {
      const list = normalizeWidgets(prev);
      const placed = {
        ...cloned,
        ...findFirstFreeSpot({ w: cloned.w, h: cloned.h }, list),
      };
      return compactWidgets([...list, placed]);
    });
  };

  const applyCurrentFormConfigToEditingWidget = () => {
    if (!canEdit || !editingId) return;
    setWidgets((prev) =>
      normalizeWidgets(prev).map((w) =>
        String(w.id) === String(editingId)
          ? {
              ...w,
              config: {
                ...(w?.config || {}),
                ...(form?.config || {}),
              },
            }
          : w
      )
    );
  };

  useEffect(() => {
    if (!canEdit || !editingId || !modalOpen) return;
    if (liveApplyDebounceRef.current) clearTimeout(liveApplyDebounceRef.current);
    liveApplyDebounceRef.current = setTimeout(() => {
      applyCurrentFormConfigToEditingWidget();
    }, 120);
    return () => {
      if (liveApplyDebounceRef.current) clearTimeout(liveApplyDebounceRef.current);
    };
  }, [form?.config, editingId, canEdit, modalOpen]);

  const saveWidget = () => {
    const next = normalizeWidgets(widgets);
    const meta = getWidgetMeta(form.type);
    const candidate = {
      id: editingId || newWidgetId(),
      type: form.type,
      title: String(form.title || meta.label).trim() || meta.label,
      color: String(form.color || "#14a89a"),
      w: clamp(form.w ?? meta.defaultSize.w, 1, DASHBOARD_GRID_COLS),
      h: clamp(form.h ?? meta.defaultSize.h, 1, DASHBOARD_GRID_ROWS),
      x: Number.isFinite(Number(form.x)) ? clamp(form.x, 0, DASHBOARD_GRID_COLS - 1) : null,
      y: Number.isFinite(Number(form.y)) ? clamp(form.y, 0, DASHBOARD_GRID_ROWS - 1) : null,
      config: {
        gateway_id: String(form?.config?.gateway_id || ""),
        tag_name: String(form?.config?.tag_name || ""),
        readings_count: clamp(form?.config?.readings_count ?? 120, 20, 500),
        interpolation: CHART_INTERPOLATION_OPTIONS.some((opt) => opt.value === form?.config?.interpolation)
          ? form?.config?.interpolation
          : "stepAfter",
        data_source_type: normalizeDataSourceForType(form?.type, form?.config?.data_source_type),
        color_mode: form?.config?.color_mode === "custom" ? "custom" : "default",
        text: String(form?.config?.text || ""),
        source_url: String(form?.config?.source_url || ""),
        camera_url: String(form?.config?.camera_url || ""),
        list_limit: clamp(form?.config?.list_limit ?? 8, 1, 50),
        query_group_interval: QUERY_GROUP_OPTIONS.some((opt) => opt.value === form?.config?.query_group_interval)
          ? form?.config?.query_group_interval
          : "none",
        query_result_aggregation: RULE_AGGREGATIONS.some((opt) => opt.value === form?.config?.query_result_aggregation)
          ? form?.config?.query_result_aggregation
          : "count",
        query_row_selection: QUERY_SELECTION_OPTIONS.some((opt) => opt.value === form?.config?.query_row_selection)
          ? form?.config?.query_row_selection
          : "all",
        query_row_limit: clamp(form?.config?.query_row_limit ?? 200, 10, 5000),
        query_rule_logic: String(form?.config?.query_rule_logic || "any") === "all" ? "all" : "any",
        query_time_filter_preset: QUERY_TIME_PRESET_OPTIONS.some((opt) => opt.value === form?.config?.query_time_filter_preset)
          ? form?.config?.query_time_filter_preset
          : "none",
        query_time_filter_from: String(form?.config?.query_time_filter_from || ""),
        query_time_filter_to: String(form?.config?.query_time_filter_to || ""),
        query_table_columns: Array.isArray(form?.config?.query_table_columns)
          ? form.config.query_table_columns.filter((c) => TABLE_COLUMN_OPTIONS.some((opt) => opt.value === c))
          : ["ts", "tag", "value"],
        compute_rules: Array.isArray(form?.config?.compute_rules) ? form.config.compute_rules : [],
        pie_show_legend: parseBoolLike(form?.config?.pie_show_legend, true),
        pie_show_labels: parseBoolLike(form?.config?.pie_show_labels, true),
        pie_show_count: parseBoolLike(form?.config?.pie_show_count, true),
        pie_show_percent: parseBoolLike(form?.config?.pie_show_percent, true),
        pie_legend_layout: parseLegendLayoutLike(form?.config?.pie_legend_layout, "side"),
        meter_show_legend: parseBoolLike(form?.config?.meter_show_legend, true),
        meter_legend_layout: parseLegendLayoutLike(form?.config?.meter_legend_layout, "side"),
        chart_show_legend: parseBoolLike(form?.config?.chart_show_legend, false),
        chart_show_point_labels: parseBoolLike(form?.config?.chart_show_point_labels, false),
        chart_value_format: VALUE_FORMAT_OPTIONS.some((opt) => opt.value === form?.config?.chart_value_format)
          ? form?.config?.chart_value_format
          : "auto",
        text_font_scale: clamp(form?.config?.text_font_scale ?? 1, 0.7, 2.5),
        table_filter_tags: Array.isArray(form?.config?.table_filter_tags)
          ? form.config.table_filter_tags.map((t) => String(t || "")).filter(Boolean)
          : [],
        query_where_conditions: Array.isArray(form?.config?.query_where_conditions)
          ? form.config.query_where_conditions
          : [],
        query_advanced_columns: Array.isArray(form?.config?.query_advanced_columns)
          ? form.config.query_advanced_columns
          : [],
        // Multi-series + axis configuration for trend charts. Persisted so
        // saved widgets keep their secondary series across reloads.
        series_extra: Array.isArray(form?.config?.series_extra)
          ? form.config.series_extra
              .filter((s) => s && typeof s === "object")
              .map((s) => ({
                id: String(s.id || ""),
                gateway_id: String(s.gateway_id || ""),
                tag_name: String(s.tag_name || ""),
                label: String(s.label || ""),
                color: String(s.color || ""),
                axis: String(s.axis || "left").toLowerCase() === "right" ? "right" : "left",
                chart_type: String(s.chart_type || ""),
                unit: String(s.unit || ""),
                suffix: String(s.suffix || ""),
                multiplier: Number(s.multiplier ?? 1) || 1,
                offset: Number(s.offset ?? 0) || 0,
                limit_value: s.limit_value === undefined || s.limit_value === null ? "" : String(s.limit_value),
                // Per-series style. Each row carries its own thickness /
                // dot / bar-width / pattern so multi-series charts can
                // have, say, a thick solid trend line plus a thin dotted
                // overlay. Empty / out-of-range values fall back to the
                // widget-wide defaults at render time.
                line_width: clamp(s.line_width ?? 2, 1, 8),
                line_dot: ["none", "small", "medium", "large"].includes(String(s.line_dot || ""))
                  ? String(s.line_dot)
                  : "none",
                bar_width: clamp(s.bar_width ?? 0, 0, 120),
                bar_pattern: ["solid", "stripes-diag", "stripes-vert", "dots"].includes(String(s.bar_pattern || ""))
                  ? String(s.bar_pattern)
                  : "solid",
                limit_dash: ["dashed", "solid", "dotted"].includes(String(s.limit_dash || ""))
                  ? String(s.limit_dash)
                  : "dashed",
              }))
          : [],
        primary_unit: String(form?.config?.primary_unit || ""),
        primary_suffix: String(form?.config?.primary_suffix || ""),
        y_axis_label: String(form?.config?.y_axis_label || ""),
        y_axis_right_label: String(form?.config?.y_axis_right_label || ""),
        chart_line_width: clamp(form?.config?.chart_line_width ?? 2, 1, 8),
        chart_line_dot: ["none", "small", "medium", "large"].includes(String(form?.config?.chart_line_dot || ""))
          ? String(form?.config?.chart_line_dot)
          : "none",
        chart_bar_opacity: clamp(form?.config?.chart_bar_opacity ?? 100, 10, 100),
        chart_bar_pattern: ["solid", "stripes-diag", "stripes-vert", "dots"].includes(String(form?.config?.chart_bar_pattern || ""))
          ? String(form?.config?.chart_bar_pattern)
          : "solid",
        chart_bar_width: clamp(form?.config?.chart_bar_width ?? 0, 0, 120),
      },
    };
    const others = next.filter((w) => w.id !== candidate.id);
    const pos = candidate.x === null || candidate.y === null
      ? findFirstFreeSpot({ w: candidate.w, h: candidate.h }, others)
      : { x: candidate.x, y: candidate.y };
    candidate.x = clamp(pos.x, 0, DASHBOARD_GRID_COLS - candidate.w);
    candidate.y = clamp(pos.y, 0, DASHBOARD_GRID_ROWS - candidate.h);
    setWidgets(compactWidgets([...others, candidate]));
    setModalOpen(false);
    setEditingId(null);
  };

  // While dragging, we only TRACK the cursor's grid cell (in a ref so it
  // doesn't trigger renders). The actual reflow happens once on drop. This
  // eliminates the visual "two widgets on top of each other" flicker that
  // came from re-running the collision pass 60 times a second mid-drag.
  const dragHoverCellRef = useRef(null);
  const dragHoverTargetIdRef = useRef("");

  const onDragStart = (id) => {
    dragHoverCellRef.current = null;
    dragHoverTargetIdRef.current = "";
    setDraggingId(id);
  };

  const onDragOverWidget = (targetId) => {
    if (!canEdit || !draggingId || draggingId === targetId) return;
    dragHoverTargetIdRef.current = String(targetId || "");
  };

  const onDropOn = () => {
    if (!canEdit || !draggingId) {
      setDraggingId("");
      return;
    }
    const draggedId = draggingId;
    // Prefer the cursor's grid coordinate (set by onGridDragOver) since it
    // honors the exact drop position. Fall back to the last hovered widget
    // for keyboard / fallback drops without grid coords.
    const cell = dragHoverCellRef.current;
    const targetId = dragHoverTargetIdRef.current;
    setWidgets((prev) => {
      const base = normalizeWidgets(prev);
      if (cell) {
        return compactWidgets(reflowWidgetsForMoveToPoint(base, draggedId, cell.x, cell.y));
      }
      if (targetId) {
        return compactWidgets(reflowWidgetsForMove(base, draggedId, targetId));
      }
      return base;
    });
    dragHoverCellRef.current = null;
    dragHoverTargetIdRef.current = "";
    setDraggingId("");
  };

  const onGridDragOver = (e) => {
    if (!canEdit || !draggingId || !gridRef.current) return;
    e.preventDefault();
    const rect = gridRef.current.getBoundingClientRect();
    const cellW = rect.width / DASHBOARD_GRID_COLS;
    const cellH = rect.height / DASHBOARD_GRID_ROWS;
    const x = clamp(Math.floor((e.clientX - rect.left) / Math.max(1, cellW)), 0, DASHBOARD_GRID_COLS - 1);
    const y = clamp(Math.floor((e.clientY - rect.top) / Math.max(1, cellH)), 0, DASHBOARD_GRID_ROWS - 1);
    dragHoverCellRef.current = { x, y };
  };

  const onResizeStart = (e, widget) => {
    if (!canEdit) return;
    e.preventDefault();
    e.stopPropagation();
    const gridEl = gridRef.current;
    if (!gridEl) return;
    const rect = gridEl.getBoundingClientRect();
    const cellW = rect.width / DASHBOARD_GRID_COLS;
    const cellH = rect.height / DASHBOARD_GRID_ROWS;
    const startX = e.clientX;
    const startY = e.clientY;
    const startW = Number(widget?.w || 1);
    const startH = Number(widget?.h || 1);
    setResizingId(String(widget.id));

    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      const dw = Math.round(dx / Math.max(1, cellW));
      const dh = Math.round(dy / Math.max(1, cellH));
      const nextW = clamp(startW + dw, 1, DASHBOARD_GRID_COLS);
      const nextH = clamp(startH + dh, 1, DASHBOARD_GRID_ROWS);
      setWidgets((prev) => compactWidgets(reflowWidgetsForResize(normalizeWidgets(prev), widget.id, nextW, nextH)));
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      setResizingId("");
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const selectedGatewayTags = useMemo(() => {
    const gw = gatewayOptions.find((g) => String(g.id) === String(form?.config?.gateway_id || ""));
    return gw?.tags || [];
  }, [gatewayOptions, form?.config?.gateway_id]);

  const supportsComputed = useMemo(
    () => ["pie_chart", "meter_chart", "table_list", "fixed_text", "value_kpi", "text_kpi"].includes(form.type),
    [form.type]
  );

  const addComputeRule = () => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: (() => {
          const existing = Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : [];
          if (String(p?.type || "") === "meter_chart") {
            return [...existing, newMeterRange(existing)];
          }
          const nextColor = pickNextRuleColor(existing);
          return [...existing, newRule(nextColor)];
        })(),
      },
    }));
  };

  const updateComputeRule = (ruleId, patch) => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: (Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : []).map((r) =>
          String(r?.id) === String(ruleId) ? { ...r, ...patch } : r
        ),
      },
    }));
  };

  const removeComputeRule = (ruleId) => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: (Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : []).filter(
          (r) => String(r?.id) !== String(ruleId)
        ),
      },
    }));
  };

  const tagsForRuleGateway = (gatewayId) => {
    const gw = gatewayOptions.find((g) => String(g.id) === String(gatewayId || ""));
    return gw?.tags || [];
  };

  const exportDashboardConfig = () => {
    const payload = {
      version: 1,
      exported_at: new Date().toISOString(),
      dashboard_configurations: {
        widgets: normalizeWidgets(widgets),
        mode: String(dashboardMode || "kpi"),
        per_row: Number(dashboardPerRow || 2),
        tag_colors: dashboardTagColors && typeof dashboardTagColors === "object" ? dashboardTagColors : {},
      },
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trustnode-dashboard-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const onImportDashboardConfig = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const raw = await file.text();
      const parsed = JSON.parse(raw);
      const incomingCfg = parsed?.dashboard_configurations && typeof parsed.dashboard_configurations === "object"
        ? parsed.dashboard_configurations
        : null;
      const incoming = Array.isArray(parsed)
        ? parsed
        : Array.isArray(parsed?.widgets)
          ? parsed.widgets
          : Array.isArray(incomingCfg?.widgets)
            ? incomingCfg.widgets
            : null;
      if (!Array.isArray(incoming)) throw new Error("Invalid dashboard file: widgets list not found.");
      if (incomingCfg) {
        if (typeof setDashboardMode === "function") {
          const nextMode = String(incomingCfg.mode || "kpi").toLowerCase();
          setDashboardMode(nextMode === "chart" ? "chart" : "kpi");
        }
        if (typeof setDashboardPerRow === "function") {
          const nextPerRow = Number(incomingCfg.per_row || 2);
          setDashboardPerRow(Number.isFinite(nextPerRow) ? Math.min(4, Math.max(1, nextPerRow)) : 2);
        }
        if (typeof setDashboardTagColors === "function") {
          const colors = incomingCfg.tag_colors && typeof incomingCfg.tag_colors === "object" && !Array.isArray(incomingCfg.tag_colors)
            ? incomingCfg.tag_colors
            : {};
          setDashboardTagColors(colors);
        }
      }
      setPendingImportWidgets(incoming);
      setPendingImportName(String(file.name || "dashboard-config.json"));
      setMenuWidgetId("");
    } catch (err) {
      window.alert(String(err?.message || err || "Failed to import dashboard config."));
    } finally {
      if (importInputRef.current) importInputRef.current.value = "";
    }
  };

  const confirmLoadDashboardConfig = () => {
    if (!pendingImportWidgets) return;
    setWidgets(compactWidgets(normalizeWidgets(pendingImportWidgets)));
    setPendingImportWidgets(null);
    setPendingImportName("");
    setConfigModalOpen(false);
  };

  return (
    <div className="dashboard-designer">
      <section className="page-tools dashboard-designer-tools">
        <div className="dashboard-tools-left">
          <button className="dashboard-toolbar-icon-btn" onClick={openCreate} disabled={!canEdit} title="Add widget" aria-label="Add widget">
            <AddIcon />
          </button>
          <button
            className="dashboard-toolbar-icon-btn"
            onClick={() => setConfigModalOpen(true)}
            disabled={!canEdit}
            title="Dashboard configuration, profiles & export"
            aria-label="Dashboard configuration"
          >
            <CogIcon />
          </button>
          {activeProfileName ? (
            <span className="dashboard-active-profile-chip" title={`Active profile: ${activeProfileName}`}>
              {activeProfileName}
            </span>
          ) : null}
        </div>
        <div className="dashboard-tools-right">
          <div className="dashboard-mode-pills">
            <button
              type="button"
              className={`dashboard-pill ${dashboardTimeMode === "live" ? "active" : ""}`}
              onClick={() => setDashboardTimeMode("live")}
            >
              Live
            </button>
            <button
              type="button"
              className={`dashboard-pill ${dashboardTimeMode === "historical" ? "active" : ""}`}
              onClick={() => setDashboardTimeMode("historical")}
            >
              Historical
            </button>
          </div>
          {dashboardTimeMode === "historical" ? (
            <div className="dashboard-range-controls">
              <button
                type="button"
                className="dashboard-pill"
                onClick={() => panHistoricalWindow(1)}
                title="Pan one window back (older)"
                aria-label="Pan back"
                style={{ padding: "4px 10px" }}
              >
                ←
              </button>
              <input type="datetime-local" value={dashboardFrom} onChange={(e) => setDashboardFrom(e.target.value)} />
              <input type="datetime-local" value={dashboardTo} onChange={(e) => setDashboardTo(e.target.value)} />
              <button
                type="button"
                className="dashboard-pill"
                onClick={() => panHistoricalWindow(-1)}
                title="Pan one window forward (newer)"
                aria-label="Pan forward"
                style={{ padding: "4px 10px" }}
              >
                →
              </button>
            </div>
          ) : null}
          {showGridMeta && dashboardTimeMode === "historical" ? (
            <div className="dashboard-grid-meta">
              {historicalLoading ? "Loading history..." : (historicalError ? historicalError : `Rows: ${dashboardRows.length}`)}
            </div>
          ) : null}
          {showGridMeta ? <div className="dashboard-grid-meta">Grid: {DASHBOARD_GRID_COLS} x {DASHBOARD_GRID_ROWS}</div> : null}
          {null}
        </div>
      </section>

      <section
        ref={gridRef}
        className="dashboard-virtual-grid"
        onDragOver={onGridDragOver}
        onDrop={onDropOn}
        style={{
          gridTemplateColumns: `repeat(${DASHBOARD_GRID_COLS}, minmax(0, 1fr))`,
          gridTemplateRows: `repeat(${DASHBOARD_GRID_ROWS}, minmax(0, 1fr))`,
        }}
      >
        {normalizedWidgets.map((widget) => (
          <article
            key={widget.id}
            className={`card dashboard-widget-shell ${draggingId === widget.id ? "is-dragging" : ""} ${Boolean(widget?.config?.hide_widget_header) && !canEdit ? "is-headerless" : ""}`}
            style={{
              gridColumn: `${widget.x + 1} / span ${widget.w}`,
              gridRow: `${widget.y + 1} / span ${widget.h}`,
            }}
            draggable={false}
            onDragOver={(e) => {
              if (!canEdit) return;
              e.preventDefault();
            }}
            onDragEnter={() => onDragOverWidget(widget.id)}
            onDrop={onDropOn}
          >
            {/* Hide-header is a render-only effect; keep the head in
                editor mode so the operator can still reach Edit/Delete
                while configuring. Once the operator switches to a
                non-editing role, the title bar disappears and the chart
                gets the full card height. */}
            <div className="dashboard-widget-head" style={Boolean(widget?.config?.hide_widget_header) && !canEdit ? { display: "none" } : undefined}>
              <strong>
                {(() => {
                  const parts = widgetHeaderParts(widget);
                  if (!parts) return String(getWidgetMeta(widget.type)?.label || widget.type);
                  const items = Array.isArray(parts.seriesItems) ? parts.seriesItems : [];
                  if (items.length > 1) {
                    // Multi-series widget: render one "value | tag" pair
                    // per visible series so the operator can read every
                    // current value without opening the chart legend.
                    return (
                      <span className="dashboard-widget-head-text">
                        {items.map((it, idx) => (
                          <React.Fragment key={`hd-${idx}`}>
                            {idx > 0 ? <span className="dashboard-widget-head-sep">·</span> : null}
                            <span className="dashboard-widget-head-value" style={{ color: it.color }}>{it.value}</span>
                            <span className="dashboard-widget-head-sep">|</span>
                            <span>{it.tag}</span>
                          </React.Fragment>
                        ))}
                        <span className="dashboard-widget-head-sep">|</span>
                        <span>{parts.typeLabel}</span>
                      </span>
                    );
                  }
                  return (
                    <span className="dashboard-widget-head-text">
                      <span className="dashboard-widget-head-value" style={{ color: String(widget?.color || "#14a89a") }}>{parts.latestValue}</span>
                      <span className="dashboard-widget-head-sep">|</span>
                      <span>{parts.plcTag}</span>
                      <span className="dashboard-widget-head-sep">|</span>
                      <span>{parts.typeLabel}</span>
                    </span>
                  );
                })()}
              </strong>
              <div className="dashboard-widget-head-actions">
                {canEdit ? (
                  <>
                    {["line_chart", "line_area_chart", "bar_chart"].includes(String(widget?.type || "")) ? (
                      <button
                        type="button"
                        className="dashboard-widget-menu-btn"
                        onClick={() => {
                          if (typeof onOpenTagMonitor === "function") onOpenTagMonitor(widget);
                        }}
                        title="Open tag monitor"
                        aria-label="Open tag monitor"
                      >
                        <MonitorIcon />
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="dashboard-widget-menu-btn dashboard-widget-drag-btn"
                      title="Drag and drop widget"
                      aria-label="Drag and drop widget"
                      draggable={canEdit}
                      onDragStart={(e) => {
                        onDragStart(widget.id);
                        e.dataTransfer.effectAllowed = "move";
                      }}
                      onDragEnd={() => setDraggingId("")}
                    >
                      <MoveCrossIcon />
                    </button>
                    <button
                      type="button"
                      className="dashboard-widget-menu-btn"
                      onClick={() => setMenuWidgetId((prev) => (prev === widget.id ? "" : widget.id))}
                      title="Widget actions"
                      aria-label="Widget actions"
                    >
                      <MenuStackIcon />
                    </button>
                    {menuWidgetId === widget.id ? (
                      <div className="dashboard-widget-actions-pop">
                        <button
                          type="button"
                          className="dashboard-widget-action-icon"
                          onClick={() => {
                            openEdit(widget);
                            setMenuWidgetId("");
                          }}
                          title="Edit"
                          aria-label="Edit widget"
                        >
                          <PencilIcon />
                        </button>
                        <button
                          type="button"
                          className="dashboard-widget-action-icon"
                          onClick={() => {
                            duplicateWidget(widget.id);
                            setMenuWidgetId("");
                          }}
                          title="Duplicate (asks for a new tag, blank keeps the same)"
                          aria-label="Duplicate widget"
                        >
                          <DuplicateIcon />
                        </button>
                        <button
                          type="button"
                          className="dashboard-widget-action-icon danger"
                          onClick={() => {
                            removeWidget(widget.id);
                            setMenuWidgetId("");
                          }}
                          title="Delete"
                          aria-label="Delete widget"
                        >
                          <TrashIcon />
                        </button>
                      </div>
                    ) : null}
                  </>
                ) : null}
              </div>
            </div>
            <DashboardWidgetCard
              widget={widget}
              dataLogView={dashboardRows}
              tagRows={tagRows}
              tagRowsByGateway={dashboardTagRowsByGateway}
              formatTagForDisplay={formatTagForDisplay}
              gatewayIntervalsById={gatewayIntervalsById}
              gatewaysIndex={gatewaysIndex}
              fetchWidgetRows={fetchWidgetRows}
              fetchWidgetStats={fetchWidgetStats}
              fetchWidgetRuleStats={fetchWidgetRuleStats}
              historicalMode={dashboardTimeMode === "historical"}
              onHistoricalPan={panHistoricalWindow}
            />
            {canEdit ? (
              <button
                type="button"
                className={`dashboard-widget-resize-handle ${resizingId === String(widget.id) ? "active" : ""}`}
                onMouseDown={(e) => onResizeStart(e, widget)}
                title="Resize widget"
                aria-label="Resize widget"
              />
            ) : null}
          </article>
        ))}
        {!normalizedWidgets.length ? (
          <article className="card dashboard-widget-empty-shell">
            <p>No widgets yet. Click <strong>Add Widget</strong> to build your live dashboard.</p>
          </article>
        ) : null}
      </section>

      {modalOpen ? (
        <div className="modal-backdrop">
          <div className="modal-card dashboard-widget-modal">
            <h3>{editingId ? "Edit Dashboard Widget" : "Add Dashboard Widget"}</h3>
            <div className="dashboard-modal-tabs" role="tablist">
              <button className={`dashboard-pill ${tab === "type" ? "active" : ""}`} onClick={() => setTab("type")} type="button">
                Widget Type
              </button>
              <button className={`dashboard-pill ${tab === "config" ? "active" : ""}`} onClick={() => setTab("config")} type="button">
                Configure
              </button>
            </div>

            {tab === "type" ? (
              <div className="dashboard-type-groups">
                {TYPE_GROUPS.map((group) => (
                  <div key={group} className="dashboard-type-group">
                    <div className="dashboard-type-group-title">{group}</div>
                    <div className="dashboard-type-grid">
                      {WIDGET_TYPES.filter((t) => t.group === group).map((t) => (
                        <button
                          key={t.key}
                          type="button"
                          className={`dashboard-type-btn ${form.type === t.key ? "active" : ""}`}
                          onClick={() => {
                            setForm((prev) => ({
                              ...prev,
                              type: t.key,
                              w: t.defaultSize.w,
                              h: t.defaultSize.h,
                              title: prev.title || t.label,
                            }));
                            setTab("config");
                          }}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="form-grid dashboard-form-grid">
                <label>
                  Title
                  <input value={form.title} onChange={(e) => setForm((p) => ({ ...p, title: e.target.value }))} />
                </label>
                {["line_chart", "line_area_chart"].includes(form.type) ? (
                  <label>
                    Interpolation
                    <select
                      value={form.config.interpolation || "stepAfter"}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, interpolation: e.target.value } }))}
                    >
                      {CHART_INTERPOLATION_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
                {/* Width / Height inputs removed: the widget is resized
                    by dragging the corner handle directly on the grid,
                    so a redundant numeric input cluttered the dialog
                    without providing extra capability. The grid state
                    (form.w / form.h) is still persisted on save. */}
                {["line_chart", "line_area_chart", "bar_chart", "meter_chart", "text_kpi", "value_kpi", "pie_chart", "table_list"].includes(form.type) ? (
                  <>
                    <label>
                      Gateway
                      <select
                        value={form.config.gateway_id}
                        onChange={(e) => {
                          const gw = e.target.value;
                          const tags = (gatewayOptions.find((g) => String(g.id) === String(gw))?.tags || []);
                          setForm((p) => ({
                            ...p,
                            config: {
                              ...p.config,
                              gateway_id: gw,
                              tag_name: tags.includes(p.config.tag_name) ? p.config.tag_name : (tags[0] || ""),
                            },
                          }));
                        }}
                      >
                        <option value="">Select gateway</option>
                        {gatewayOptions.map((g) => (
                          <option key={g.id} value={g.id}>{g.name}</option>
                        ))}
                      </select>
                    </label>
                    {[
                      "line_chart",
                      "line_area_chart",
                      "bar_chart",
                      "meter_chart",
                      "text_kpi",
                      "value_kpi",
                    ].includes(form.type) ||
                    (form.type === "pie_chart" && String(form?.config?.data_source_type || "tag_direct") !== "computed") ? (
                      <label>
                        Tag
                        <select
                          value={form.config.tag_name}
                          onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, tag_name: e.target.value } }))}
                        >
                          <option value="">Select tag</option>
                          {selectedGatewayTags.map((tag) => (
                            <option key={tag} value={tag}>
                              {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}
                  </>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? (
                  <label>
                    Reading points
                    <input
                      type="number"
                      min="20"
                      max="500"
                      value={form.config.readings_count}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, readings_count: clamp(e.target.value, 20, 500) },
                        }))
                      }
                    />
                  </label>
                ) : null}

                {/* Independent reading count for the extra series. Lets the
                    operator configure a multi-series chart that pulls a
                    different history depth than the primary tag — including
                    series-only widgets where the primary gateway/tag is left
                    blank. Empty / 0 falls back to readings_count * 8. */}
                {["line_chart", "line_area_chart", "bar_chart"].includes(form.type)
                  && Array.isArray(form.config?.series_extra)
                  && form.config.series_extra.length > 0 ? (
                  <label>
                    Series reading points
                    <input
                      type="number"
                      min="0"
                      max="5000"
                      value={Number(form.config?.series_readings_count || 0)}
                      placeholder="(follow Reading points)"
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: {
                            ...p.config,
                            series_readings_count: Math.max(0, Math.min(5000, Number(e.target.value || 0))),
                          },
                        }))
                      }
                      title="Number of historical points fetched for each extra series. 0 = follow the primary Reading points field."
                    />
                  </label>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart", "pie_chart", "meter_chart"].includes(form.type) ? (
                  <label>
                    Chart colors
                    <select
                      value={form.config.color_mode}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, color_mode: e.target.value } }))}
                    >
                      <option value="default">Default brand colors</option>
                      <option value="custom">Custom widget color</option>
                    </select>
                  </label>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart", "pie_chart", "meter_chart"].includes(form.type) &&
                form.config.color_mode === "custom" ? (
                  <label>
                    Custom color
                    <input value={form.color} type="color" onChange={(e) => setForm((p) => ({ ...p, color: e.target.value }))} />
                  </label>
                ) : null}

                {supportsComputed ? (
                  <label>
                    Data source
                    <select
                      value={form.config.data_source_type}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: {
                            ...p.config,
                            data_source_type: e.target.value === "computed" ? "computed" : "tag_direct",
                          },
                        }))
                      }
                    >
                      <option value="tag_direct">Tag direct</option>
                      <option value="computed">Computed rules</option>
                    </select>
                  </label>
                ) : null}
                {["pie_chart", "table_list", "meter_chart", "text_kpi"].includes(form.type) ? (
                  <div className="dashboard-full-row">
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      onClick={() => setQueryModalOpen(true)}
                    >
                      Open Data Query Builder
                    </button>
                  </div>
                ) : null}
                {/* Trend widgets (line/area/bar) don't use the full Data Query
                    Builder, but they DO need access to the multi-series and
                    dual-axis editor that lives inside it. Surface a direct
                    button here so the option isn't hidden behind a modal
                    that's named for a different workflow. */}
                {["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? (
                  <div className="dashboard-full-row">
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      onClick={() => setQueryModalOpen(true)}
                      title="Plot multiple tags on the same chart with their own units and axes"
                    >
                      Series & Axes (multi-tag, dual axis, units)
                    </button>
                    {Array.isArray(form.config?.series_extra) && form.config.series_extra.length ? (
                      <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                        {form.config.series_extra.length} extra series configured.
                        {form.config.series_extra.some((s) => String(s?.axis || "left").toLowerCase() === "right")
                          ? " Right axis active."
                          : ""}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {["text_kpi", "value_kpi", "fixed_text"].includes(form.type) ? (
                  <label>
                    Text scale
                    <input
                      type="number"
                      min="0.7"
                      max="2.5"
                      step="0.1"
                      value={Number(form.config.text_font_scale || 1).toFixed(1)}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, text_font_scale: clamp(e.target.value, 0.7, 2.5) },
                        }))
                      }
                    />
                  </label>
                ) : null}

                {form.type === "table_list" ? (
                  <label>
                    List limit
                    <input
                      type="number"
                      min="1"
                      max="50"
                      value={form.config.list_limit}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, list_limit: clamp(e.target.value, 1, 50) } }))}
                    />
                  </label>
                ) : null}

                {(String(form.type) === "meter_chart" || (supportsComputed && form.config.data_source_type === "computed")) ? (
                  <div className="dashboard-full-row dashboard-rules-summary">
                    <div className="dashboard-rules-summary-text">
                      {String(form.type) === "meter_chart" ? "Meter ranges configured" : "Computed rules configured"}: <strong>{Array.isArray(form.config.compute_rules) ? form.config.compute_rules.length : 0}</strong>
                    </div>
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      onClick={() => setComputedModalOpen(true)}
                    >
                      {String(form.type) === "meter_chart" ? "Open Meter Ranges" : "Open Computed Rules"}
                    </button>
                  </div>
                ) : null}

                {form.type === "meter_chart" ? (
                  <div className="dashboard-full-row">
                    <div className="dashboard-pie-options">
                      <div className="dashboard-pie-option-row dashboard-pie-option-layout">
                        <span className="dashboard-pie-option-label">Legend layout</span>
                        <select
                          value={String(form?.config?.meter_legend_layout || "side")}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, meter_legend_layout: e.target.value },
                            }))
                          }
                        >
                          <option value="side">Side (vertically centered)</option>
                          <option value="bottom">Bottom (single centered row)</option>
                        </select>
                      </div>
                      <label className="dashboard-pie-option-row">
                        <input
                          type="checkbox"
                          checked={Boolean(form?.config?.meter_show_legend ?? true)}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, meter_show_legend: Boolean(e.target.checked) },
                            }))
                          }
                        />
                        <span className="dashboard-pie-option-label">Show legend</span>
                      </label>
                    </div>
                  </div>
                ) : null}

                {form.type === "fixed_text" || form.type === "divider" ? (
                  <label className="dashboard-full-row">
                    Text
                    <input
                      value={form.config.text}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, text: e.target.value } }))}
                    />
                  </label>
                ) : null}

                {form.type === "image" ? (
                  <label className="dashboard-full-row">
                    Image URL
                    <input
                      value={form.config.source_url}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, source_url: e.target.value } }))}
                    />
                  </label>
                ) : null}

                {form.type === "ip_camera" ? (
                  <label className="dashboard-full-row">
                    Camera URL
                    <input
                      value={form.config.camera_url}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, camera_url: e.target.value } }))}
                    />
                  </label>
                ) : null}

                {form.type === "cloud_sync_status" ? (
                  <>
                    <label className="dashboard-full-row">
                      Display mode
                      <select
                        value={String(form.config.display_mode || "combined")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, display_mode: e.target.value } }))}
                      >
                        <option value="combined">Combined (donut + tiles + sparkline)</option>
                        <option value="donut">Donut</option>
                        <option value="stat_tiles">Stat tiles</option>
                        <option value="progress_bar">Progress bar</option>
                        <option value="line_history">Backlog history line</option>
                      </select>
                    </label>
                    <label className="dashboard-full-row">
                      Backlog history window
                      <select
                        value={String(form.config.history_window_sec || 300)}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, history_window_sec: Number(e.target.value) } }))}
                      >
                        <option value="60">Last 60 seconds</option>
                        <option value="300">Last 5 minutes</option>
                        <option value="900">Last 15 minutes</option>
                        <option value="1800">Last 30 minutes</option>
                        <option value="3600">Last 1 hour</option>
                      </select>
                    </label>
                    <label className="dashboard-full-row">
                      Refresh interval
                      <select
                        value={String(form.config.poll_interval_ms || 2000)}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, poll_interval_ms: Number(e.target.value) } }))}
                      >
                        <option value="1000">1 second</option>
                        <option value="2000">2 seconds</option>
                        <option value="5000">5 seconds</option>
                        <option value="10000">10 seconds</option>
                      </select>
                    </label>
                    <label className="dashboard-pie-option">
                      <input
                        type="checkbox"
                        checked={form.config.include_config_outbox !== false}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, include_config_outbox: e.target.checked } }))}
                      />
                      <span className="dashboard-pie-option-label">Show config outbox counters</span>
                    </label>
                    <label className="dashboard-pie-option">
                      <input
                        type="checkbox"
                        checked={Boolean(form.config.include_telemetry_v1)}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, include_telemetry_v1: e.target.checked } }))}
                      />
                      <span className="dashboard-pie-option-label">Show legacy telemetry-v1 counters</span>
                    </label>
                  </>
                ) : null}
              </div>
            )}

            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveWidget}>Save</button>
              <button className="btn btn-danger" onClick={() => setModalOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}

      {configModalOpen ? (
        <div className="modal-backdrop">
          <div className="modal-card dashboard-config-modal">
            <h3>Dashboard Configuration</h3>

            <section className="dashboard-config-section">
              <h4>Profiles</h4>
              <p className="dashboard-config-hint">
                Profiles save the current widgets, grid mode, columns and tag colors under a name. Switch between them at any time — loading a profile fully replaces the active dashboard.
              </p>
              <div className="dashboard-profile-group">
                <select
                  className="dashboard-profile-select"
                  value={activeProfileName}
                  onChange={(e) => handleLoadProfile(e.target.value)}
                  aria-label="Load dashboard profile"
                >
                  <option value="">Default (unsaved)</option>
                  {profiles.map((p) => (
                    <option key={p.name} value={p.name}>{p.name}</option>
                  ))}
                </select>
                <button
                  type="button"
                  className="dashboard-profile-btn"
                  onClick={handleSaveProfile}
                  disabled={!canEdit}
                  title={activeProfileName ? `Save changes to "${activeProfileName}"` : "Save current layout as a new profile"}
                >
                  Save
                </button>
                <button
                  type="button"
                  className="dashboard-profile-btn"
                  onClick={handleSaveAsProfile}
                  disabled={!canEdit}
                  title="Save the current layout as a new named profile"
                >
                  Save as…
                </button>
                <button
                  type="button"
                  className="dashboard-profile-btn dashboard-profile-btn-danger"
                  onClick={handleDeleteProfile}
                  disabled={!canEdit || !activeProfileName}
                  title={activeProfileName ? `Delete profile "${activeProfileName}"` : "No profile loaded"}
                >
                  Delete
                </button>
              </div>
              <div className="dashboard-config-meta">
                {activeProfileName
                  ? `Active profile: ${activeProfileName} — ${profiles.length} profile${profiles.length === 1 ? "" : "s"} saved`
                  : `${profiles.length} profile${profiles.length === 1 ? "" : "s"} saved`}
              </div>

              {/* Carousel mode: auto-rotate through every saved profile on a
                  timer. Designed for control-room TVs where the operator
                  wants to see every shift / area dashboard in rotation. */}
              <div className="dashboard-carousel-row">
                <label className="tn-switch" title="Auto-cycle through every saved profile">
                  <input
                    type="checkbox"
                    checked={Boolean(carouselEnabled)}
                    onChange={(e) => setCarouselEnabled(e.target.checked)}
                    disabled={profiles.length < 2}
                  />
                  <span className="tn-switch-track" aria-hidden>
                    <span className="tn-switch-thumb" />
                  </span>
                  <span className="tn-switch-label">Carousel mode</span>
                </label>
                <label className="dashboard-carousel-interval">
                  <span>Switch every</span>
                  <input
                    type="number"
                    min="5"
                    max="3600"
                    step="5"
                    value={Number(carouselIntervalSec) || 30}
                    onChange={(e) => {
                      const v = Number(e.target.value || 30);
                      setCarouselIntervalSec(Math.max(5, Math.min(3600, Number.isFinite(v) ? v : 30)));
                    }}
                  />
                  <span>seconds</span>
                </label>
              </div>
              {profiles.length < 2 ? (
                <div className="dashboard-config-meta" style={{ color: "var(--muted, #94a3b8)" }}>
                  Save at least two profiles to enable carousel mode.
                </div>
              ) : carouselEnabled ? (
                <div className="dashboard-config-meta" style={{ color: "var(--teal, #14a89a)" }}>
                  Carousel running — switching profile every {Number(carouselIntervalSec) || 30}s.
                </div>
              ) : null}
            </section>

            <section className="dashboard-config-section">
              <h4>Export / Import (JSON)</h4>
              <p className="dashboard-config-hint">
                Move a dashboard between TrustNode installations as a JSON file. Useful for templates, backups, or onboarding a new edge.
              </p>
              <div className="dashboard-config-actions">
                <button
                  type="button"
                  className="dashboard-config-action-btn"
                  onClick={exportDashboardConfig}
                  title="Download current dashboard configuration"
                  aria-label="Download current dashboard configuration"
                >
                  <DownloadIcon />
                  <span>Export JSON</span>
                </button>
                <button
                  type="button"
                  className="dashboard-config-action-btn"
                  onClick={() => importInputRef.current?.click()}
                  title="Select a dashboard configuration file"
                  aria-label="Select a dashboard configuration file"
                >
                  <UploadIcon />
                  <span>Select JSON</span>
                </button>
                <input
                  ref={importInputRef}
                  type="file"
                  accept="application/json,.json"
                  className="dashboard-hidden-input"
                  onChange={onImportDashboardConfig}
                />
              </div>
              <div className="dashboard-config-note">
                {pendingImportWidgets
                  ? `Ready to load: ${pendingImportName}`
                  : "Select a JSON file first, then confirm load."}
              </div>
            </section>

            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={confirmLoadDashboardConfig} disabled={!pendingImportWidgets}>
                Confirm Load
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  setPendingImportWidgets(null);
                  setPendingImportName("");
                  setConfigModalOpen(false);
                }}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {profilePromptOpen ? (
        <div className="modal-backdrop" style={{ zIndex: 60 }}>
          <div className="modal-card" style={{ width: "min(420px, 92vw)" }}>
            <h3 style={{ marginTop: 0 }}>Profile name</h3>
            <p className="dashboard-config-hint">
              Save the current dashboard layout (widgets, grid mode, columns, tag colors) under this name.
            </p>
            <input
              autoFocus
              value={profilePromptName}
              onChange={(e) => setProfilePromptName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  finishProfilePrompt(profilePromptName);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  finishProfilePrompt("");
                }
              }}
              placeholder="e.g. Production overview"
              style={{ width: "100%", padding: "8px 10px", fontSize: 14, marginTop: 4 }}
            />
            <div className="row modal-actions" style={{ marginTop: 14, justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                className="btn btn-danger"
                onClick={() => finishProfilePrompt("")}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => finishProfilePrompt(profilePromptName)}
                disabled={!String(profilePromptName || "").trim()}
              >
                Save profile
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {queryModalOpen ? (
        <div className="modal-backdrop">
          <div className={`modal-card dashboard-widget-modal dashboard-query-modal dashboard-query-modal-wide ${["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? "dashboard-series-modal-wide" : ""}`}>
            <h3>
              {["line_chart", "line_area_chart", "bar_chart"].includes(form.type)
                ? "Series, Axes & Data Query"
                : "Widget Data Query Builder"}
            </h3>
            <div className="dashboard-query-sections">
              <fieldset className="dashboard-query-fieldset">
                <legend>Time &amp; rows</legend>
                <div className="dashboard-query-grid">
                  <label className="dashboard-query-field">
                    <span>Time grouping</span>
                    <select
                      value={form.config.query_group_interval || "none"}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, query_group_interval: e.target.value } }))}
                    >
                      {QUERY_GROUP_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="dashboard-query-field">
                    <span>Result aggregation</span>
                    <select
                      value={form.config.query_result_aggregation || "count"}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, query_result_aggregation: e.target.value } }))}
                    >
                      {RULE_AGGREGATIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="dashboard-query-field">
                    <span>Row selection</span>
                    <select
                      value={form.config.query_row_selection || "all"}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, query_row_selection: e.target.value } }))}
                    >
                      {QUERY_SELECTION_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="dashboard-query-field">
                    <span>Row limit</span>
                    <input
                      type="number"
                      min="10"
                      max="5000"
                      value={form.config.query_row_limit || 200}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, query_row_limit: clamp(e.target.value, 10, 5000) },
                        }))
                      }
                    />
                  </label>
                  <label className="dashboard-query-field">
                    <span>Rule logic</span>
                    <select
                      value={form.config.query_rule_logic || "any"}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, query_rule_logic: e.target.value === "all" ? "all" : "any" } }))}
                    >
                      <option value="any">Any condition (OR)</option>
                      <option value="all">All conditions (AND)</option>
                    </select>
                  </label>
                  <label className="dashboard-query-field">
                    <span>Time filter</span>
                    <select
                      value={form.config.query_time_filter_preset || "none"}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, query_time_filter_preset: e.target.value },
                        }))
                      }
                    >
                      {QUERY_TIME_PRESET_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  {String(form.config.query_time_filter_preset || "none") === "custom" ? (
                    <>
                      <label className="dashboard-query-field">
                        <span>Filter from</span>
                        <input
                          type="datetime-local"
                          value={form.config.query_time_filter_from || ""}
                          onChange={(e) =>
                            setForm((p) => ({ ...p, config: { ...p.config, query_time_filter_from: e.target.value } }))
                          }
                        />
                      </label>
                      <label className="dashboard-query-field">
                        <span>Filter to</span>
                        <input
                          type="datetime-local"
                          value={form.config.query_time_filter_to || ""}
                          onChange={(e) =>
                            setForm((p) => ({ ...p, config: { ...p.config, query_time_filter_to: e.target.value } }))
                          }
                        />
                      </label>
                    </>
                  ) : null}
                </div>
              </fieldset>
              {form.type === "pie_chart" ? (
                <fieldset className="dashboard-query-fieldset">
                  <legend>Pie display</legend>
                  <div className="dashboard-query-checkboxes">
                    <label className="dashboard-query-field">
                      <span>Legend layout</span>
                      <select
                        value={String(form.config?.pie_legend_layout || "side")}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: {
                              ...p.config,
                              pie_legend_layout: e.target.value === "bottom" ? "bottom" : "side",
                            },
                          }))
                        }
                      >
                        <option value="side">Side (vertically centered)</option>
                        <option value="bottom">Bottom (single centered row)</option>
                      </select>
                    </label>
                    {[
                      { key: "pie_show_legend", label: "Show legend" },
                      { key: "pie_show_labels", label: "Show labels on chart" },
                      { key: "pie_show_count", label: "Show count/value" },
                      { key: "pie_show_percent", label: "Show percent" },
                    ].map((opt) => (
                      <label key={opt.key} className="dashboard-query-checkbox">
                        <input
                          type="checkbox"
                          checked={Boolean(form.config?.[opt.key])}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, [opt.key]: e.target.checked },
                            }))
                          }
                        />
                        <span>{opt.label}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ) : null}
              {form.type === "meter_chart" ? (
                <fieldset className="dashboard-query-fieldset">
                  <legend>Meter display</legend>
                  <div className="dashboard-query-checkboxes">
                    <label className="dashboard-query-field">
                      <span>Legend layout</span>
                      <select
                        value={String(form.config?.meter_legend_layout || "side")}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: {
                              ...p.config,
                              meter_legend_layout: e.target.value === "bottom" ? "bottom" : "side",
                            },
                          }))
                        }
                      >
                        <option value="side">Side (vertically centered)</option>
                        <option value="bottom">Bottom (single centered row)</option>
                      </select>
                    </label>
                    <label className="dashboard-query-checkbox">
                      <input
                        type="checkbox"
                        checked={Boolean(form.config?.meter_show_legend ?? true)}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: { ...p.config, meter_show_legend: e.target.checked },
                          }))
                        }
                      />
                      <span>Show legend</span>
                    </label>
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      onClick={() => {
                        setQueryModalOpen(false);
                        setComputedModalOpen(true);
                      }}
                    >
                      Open meter ranges
                    </button>
                  </div>
                </fieldset>
              ) : null}
              {["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? (
                <fieldset className="dashboard-query-fieldset">
                  <legend>Chart display</legend>
                  <div className="dashboard-query-grid">
                    <label className="dashboard-query-field">
                      <span>Value format</span>
                      <select
                        value={form.config.chart_value_format || "auto"}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: { ...p.config, chart_value_format: e.target.value },
                          }))
                        }
                      >
                        {VALUE_FORMAT_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="dashboard-query-checkboxes">
                    {[
                      { key: "chart_show_legend", label: "Show legend" },
                      { key: "chart_show_point_labels", label: "Show point labels" },
                      // Hide the entire widget title strip (the value | tag |
                      // type bar at the top of the card) so the chart body
                      // gets the full card height. Useful for KPIs and
                      // historical-only charts where the title is noise.
                      { key: "hide_widget_header", label: "Hide widget title bar" },
                    ].map((opt) => (
                      <label key={opt.key} className="dashboard-query-checkbox">
                        <input
                          type="checkbox"
                          checked={Boolean(form.config?.[opt.key])}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, [opt.key]: e.target.checked },
                            }))
                          }
                        />
                        <span>{opt.label}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              ) : null}
              {["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? (
                <fieldset className="dashboard-query-fieldset">
                  <legend>Series & axes</legend>
                  <p className="dashboard-query-hint">
                    Plot multiple tags on the same chart. Each series can have its own unit, axis (left / right), chart style and color.
                  </p>
                  <div className="dashboard-query-grid">
                    <label className="dashboard-query-field">
                      <span>Primary unit</span>
                      <input
                        value={form.config.primary_unit || ""}
                        placeholder="e.g. °C"
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, primary_unit: e.target.value } }))}
                      />
                    </label>
                    <label className="dashboard-query-field">
                      <span>Primary suffix</span>
                      <input
                        value={form.config.primary_suffix || ""}
                        placeholder="optional"
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, primary_suffix: e.target.value } }))}
                      />
                    </label>
                    <label className="dashboard-query-field">
                      <span>Left axis label</span>
                      <input
                        value={form.config.y_axis_label || ""}
                        placeholder="optional"
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, y_axis_label: e.target.value } }))}
                      />
                    </label>
                    <label className="dashboard-query-field">
                      <span>Right axis label</span>
                      <input
                        value={form.config.y_axis_right_label || ""}
                        placeholder="optional"
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, y_axis_right_label: e.target.value } }))}
                      />
                    </label>
                  </div>
                  {/* Per-widget styling: line thickness, dot marker, bar fill / width.
                      Placed inside the Series & Axes fieldset (instead of the
                      separate Chart Display section above) so operators see
                      it without scrolling up through the modal. */}
                  {["line_chart", "line_area_chart"].includes(form.type) ? (
                    <div className="dashboard-query-grid" style={{ marginTop: 6 }}>
                      <label className="dashboard-query-field">
                        <span>Line thickness (1–8 px)</span>
                        <input
                          type="number"
                          min="1"
                          max="8"
                          step="1"
                          value={Number(form.config?.chart_line_width || 2)}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, chart_line_width: clamp(e.target.value, 1, 8) },
                            }))
                          }
                        />
                      </label>
                      <label className="dashboard-query-field">
                        <span>Dot marker</span>
                        <select
                          value={String(form.config?.chart_line_dot || "none")}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, chart_line_dot: e.target.value },
                            }))
                          }
                        >
                          <option value="none">No markers</option>
                          <option value="small">Small dots</option>
                          <option value="medium">Medium dots</option>
                          <option value="large">Large dots</option>
                        </select>
                      </label>
                    </div>
                  ) : null}
                  {form.type === "bar_chart" ? (
                    <div className="dashboard-query-grid" style={{ marginTop: 6 }}>
                      <label className="dashboard-query-field">
                        <span>Bar width (px, 0 = auto)</span>
                        <input
                          type="number"
                          min="0"
                          max="120"
                          step="1"
                          value={Number(form.config?.chart_bar_width ?? 0)}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, chart_bar_width: clamp(e.target.value, 0, 120) },
                            }))
                          }
                        />
                      </label>
                      <label className="dashboard-query-field">
                        <span>Bar fill opacity (%)</span>
                        <input
                          type="number"
                          min="10"
                          max="100"
                          step="5"
                          value={Number(form.config?.chart_bar_opacity ?? 100)}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, chart_bar_opacity: clamp(e.target.value, 10, 100) },
                            }))
                          }
                        />
                      </label>
                      <label className="dashboard-query-field">
                        <span>Bar pattern / fill</span>
                        <select
                          value={String(form.config?.chart_bar_pattern || "solid")}
                          onChange={(e) =>
                            setForm((p) => ({
                              ...p,
                              config: { ...p.config, chart_bar_pattern: e.target.value },
                            }))
                          }
                        >
                          <option value="solid">Solid</option>
                          <option value="stripes-diag">Diagonal stripes</option>
                          <option value="stripes-vert">Vertical stripes</option>
                          <option value="dots">Dotted</option>
                        </select>
                      </label>
                    </div>
                  ) : null}
                  {(Array.isArray(form.config.series_extra) && form.config.series_extra.length > 0) ? (
                    <div className="dashboard-series-table">
                      <div className="dashboard-series-table-head">
                        <span>Gateway</span>
                        <span>Tag</span>
                        <span>Label</span>
                        <span>Axis</span>
                        <span>Type</span>
                        <span>Unit</span>
                        <span>Suffix</span>
                        <span>Size</span>
                        <span>Style</span>
                        <span>Color</span>
                        <span></span>
                      </div>
                      {form.config.series_extra.map((row, idx) => {
                        const allowedTags = (gatewayOptions.find((g) => String(g.id) === String(row.gateway_id || form.config.gateway_id))?.tags) || [];
                        const update = (patch) => setForm((p) => {
                          const list = Array.isArray(p.config.series_extra) ? [...p.config.series_extra] : [];
                          list[idx] = { ...(list[idx] || {}), ...patch };
                          return { ...p, config: { ...p.config, series_extra: list } };
                        });
                        const remove = () => setForm((p) => {
                          const list = Array.isArray(p.config.series_extra) ? [...p.config.series_extra] : [];
                          list.splice(idx, 1);
                          return { ...p, config: { ...p.config, series_extra: list } };
                        });
                        const isLimit = String(row.chart_type || "").toLowerCase() === "limit";
                        return (
                          <div key={row.id || idx} className={`dashboard-series-table-row ${isLimit ? "dashboard-series-row-limit" : ""}`}>
                            <select
                              value={row.gateway_id || form.config.gateway_id || ""}
                              onChange={(e) => update({ gateway_id: e.target.value, tag_name: "" })}
                              title="Gateway"
                              disabled={isLimit && !row.tag_name}
                            >
                              <option value="">(same as primary)</option>
                              {gatewayOptions.map((g) => (
                                <option key={g.id} value={g.id}>{g.name || g.id}</option>
                              ))}
                            </select>
                            {isLimit ? (
                              <input
                                type="number"
                                step="any"
                                value={row.limit_value ?? ""}
                                placeholder="constant value"
                                onChange={(e) => update({ limit_value: e.target.value })}
                                title="Threshold value drawn as a horizontal line"
                              />
                            ) : (
                              <select
                                value={row.tag_name || ""}
                                onChange={(e) => update({ tag_name: e.target.value })}
                                title="Tag"
                              >
                                <option value="">Select tag</option>
                                {allowedTags.map((tag) => (
                                  <option key={tag} value={tag}>
                                    {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                                  </option>
                                ))}
                              </select>
                            )}
                            <input
                              value={row.label || ""}
                              placeholder={isLimit ? "Label (e.g. Max Pressure)" : "(tag name)"}
                              onChange={(e) => update({ label: e.target.value })}
                              title="Label"
                            />
                            <select
                              value={row.axis || "left"}
                              onChange={(e) => update({ axis: e.target.value === "right" ? "right" : "left" })}
                              title="Axis"
                            >
                              <option value="left">Left</option>
                              <option value="right">Right</option>
                            </select>
                            <select
                              value={row.chart_type || ""}
                              onChange={(e) => update({ chart_type: e.target.value })}
                              title="Chart type"
                            >
                              <option value="">(widget)</option>
                              <option value="line">Line</option>
                              <option value="area">Area</option>
                              <option value="bar">Bar</option>
                              <option value="limit">Limit line</option>
                            </select>
                            <input
                              value={row.unit || ""}
                              placeholder={isLimit ? "(optional)" : "e.g. bar"}
                              onChange={(e) => update({ unit: e.target.value })}
                              title="Unit"
                              disabled={isLimit}
                            />
                            <input
                              value={row.suffix || ""}
                              placeholder=""
                              onChange={(e) => update({ suffix: e.target.value })}
                              title="Suffix"
                              disabled={isLimit}
                            />
                            {/* Size cell: per-series thickness for line/area,
                                bar width for bar, ignored for limit lines. */}
                            {(() => {
                              const effectiveKind = (row.chart_type || "").toLowerCase()
                                || (form.type === "bar_chart" ? "bar"
                                  : form.type === "line_area_chart" ? "area" : "line");
                              if (isLimit) {
                                return <input value="" disabled title="Not applicable for limit lines" />;
                              }
                              if (effectiveKind === "bar") {
                                return (
                                  <input
                                    type="number"
                                    min="0"
                                    max="120"
                                    step="1"
                                    value={Number(row.bar_width ?? 0)}
                                    onChange={(e) => update({ bar_width: clamp(e.target.value, 0, 120) })}
                                    title="Bar width (px, 0 = auto)"
                                  />
                                );
                              }
                              return (
                                <input
                                  type="number"
                                  min="1"
                                  max="8"
                                  step="1"
                                  value={Number(row.line_width ?? 2)}
                                  onChange={(e) => update({ line_width: clamp(e.target.value, 1, 8) })}
                                  title="Line thickness (1–8 px)"
                                />
                              );
                            })()}
                            {/* Style cell: dot marker for line/area, fill
                                pattern for bar. */}
                            {(() => {
                              const effectiveKind = (row.chart_type || "").toLowerCase()
                                || (form.type === "bar_chart" ? "bar"
                                  : form.type === "line_area_chart" ? "area" : "line");
                              if (isLimit) {
                                return (
                                  <select
                                    value={String(row.limit_dash || "dashed")}
                                    onChange={(e) => update({ limit_dash: e.target.value })}
                                    title="Limit line style"
                                  >
                                    <option value="dashed">Dashed</option>
                                    <option value="solid">Solid</option>
                                    <option value="dotted">Dotted</option>
                                  </select>
                                );
                              }
                              if (effectiveKind === "bar") {
                                return (
                                  <select
                                    value={String(row.bar_pattern || "solid")}
                                    onChange={(e) => update({ bar_pattern: e.target.value })}
                                    title="Bar fill pattern"
                                  >
                                    <option value="solid">Solid</option>
                                    <option value="stripes-diag">Diagonal</option>
                                    <option value="stripes-vert">Vertical</option>
                                    <option value="dots">Dotted</option>
                                  </select>
                                );
                              }
                              return (
                                <select
                                  value={String(row.line_dot || "none")}
                                  onChange={(e) => update({ line_dot: e.target.value })}
                                  title="Dot marker"
                                >
                                  <option value="none">No dots</option>
                                  <option value="small">Small dots</option>
                                  <option value="medium">Medium dots</option>
                                  <option value="large">Large dots</option>
                                </select>
                              );
                            })()}
                            <input
                              type="color"
                              value={row.color || (isLimit ? "#dc2626" : "#f97316")}
                              onChange={(e) => update({ color: e.target.value })}
                              title="Color"
                            />
                            <button
                              type="button"
                              className="icon-btn table-action-btn danger"
                              onClick={remove}
                              title="Remove this series"
                              aria-label="Remove series"
                            >
                              <TrashIcon />
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                  <div className="row" style={{ gap: 8 }}>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => setForm((p) => {
                        const list = Array.isArray(p.config.series_extra) ? [...p.config.series_extra] : [];
                        list.push({
                          id: `s${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`,
                          gateway_id: "",
                          tag_name: "",
                          label: "",
                          color: ["#f97316", "#3b82f6", "#a855f7", "#dc2626", "#10b981", "#f59e0b"][list.length % 6],
                          axis: "right",
                          chart_type: "",
                          unit: "",
                          suffix: "",
                          multiplier: 1,
                          offset: 0,
                        });
                        return { ...p, config: { ...p.config, series_extra: list } };
                      })}
                    >
                      + Add series
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      title="Add a horizontal threshold drawn across the chart at a constant value"
                      onClick={() => setForm((p) => {
                        const list = Array.isArray(p.config.series_extra) ? [...p.config.series_extra] : [];
                        list.push({
                          id: `s${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`,
                          gateway_id: "",
                          tag_name: "",
                          label: "Limit",
                          color: "#dc2626",
                          axis: "left",
                          chart_type: "limit",
                          unit: "",
                          suffix: "",
                          multiplier: 1,
                          offset: 0,
                          limit_value: "",
                        });
                        return { ...p, config: { ...p.config, series_extra: list } };
                      })}
                    >
                      + Add limit line
                    </button>
                  </div>
                </fieldset>
              ) : null}
              {form.type === "table_list" ? (
                <>
                  <fieldset className="dashboard-query-fieldset">
                    <legend>Simple columns (legacy)</legend>
                    <p className="dashboard-query-hint">
                      Picks fixed columns from each historian row. Used when no
                      <em> advanced columns </em> are defined below.
                    </p>
                    <div className="dashboard-query-checkboxes">
                      {TABLE_COLUMN_OPTIONS.map((opt) => {
                        const checked = (form.config.query_table_columns || []).includes(opt.value);
                        return (
                          <label key={opt.value} className="dashboard-query-checkbox">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(e) => {
                                setForm((p) => {
                                  const current = Array.isArray(p.config.query_table_columns) ? p.config.query_table_columns : [];
                                  const next = e.target.checked
                                    ? Array.from(new Set([...current, opt.value]))
                                    : current.filter((v) => v !== opt.value);
                                  return {
                                    ...p,
                                    config: {
                                      ...p.config,
                                      query_table_columns: next.length ? next : ["ts", "tag", "value"],
                                    },
                                  };
                                });
                              }}
                            />
                            <span>{opt.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </fieldset>

                  <fieldset className="dashboard-query-fieldset">
                    <legend>Tag filters</legend>
                    <p className="dashboard-query-hint">
                      Limit the rows used by both simple and advanced columns to a subset of tags.
                    </p>
                    <div className="dashboard-query-checkboxes">
                      {selectedGatewayTags.length ? selectedGatewayTags.map((tag) => {
                        const checked = (form.config.table_filter_tags || []).includes(tag);
                        return (
                          <label key={`tbl-tag-${tag}`} className="dashboard-query-checkbox">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(e) => {
                                setForm((p) => {
                                  const current = Array.isArray(p.config.table_filter_tags) ? p.config.table_filter_tags : [];
                                  const next = e.target.checked
                                    ? Array.from(new Set([...current, tag]))
                                    : current.filter((v) => v !== tag);
                                  return { ...p, config: { ...p.config, table_filter_tags: next } };
                                });
                              }}
                            />
                            <span>{formatTagForDisplay ? formatTagForDisplay(tag) : tag}</span>
                          </label>
                        );
                      }) : <span className="dashboard-query-empty">No tags in selected gateway.</span>}
                    </div>
                  </fieldset>

                  <TableWhereConditions
                    form={form}
                    setForm={setForm}
                    selectedGatewayTags={selectedGatewayTags}
                    formatTagForDisplay={formatTagForDisplay}
                  />

                  <TableAdvancedColumns
                    form={form}
                    setForm={setForm}
                    selectedGatewayTags={selectedGatewayTags}
                    formatTagForDisplay={formatTagForDisplay}
                  />
                </>
              ) : null}
            </div>
            <div className="row modal-actions">
              <button
                className="btn btn-primary"
                onClick={() => {
                  // Apply query changes immediately to the live widget preview.
                  // Do not close the parent edit modal here, otherwise the latest
                  // form edits can be lost if React state is still settling.
                  applyCurrentFormConfigToEditingWidget();
                  setQueryModalOpen(false);
                }}
              >
                Done
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {computedModalOpen ? (
        <div className="modal-backdrop">
          <div className="modal-card dashboard-widget-modal dashboard-computed-modal">
            <h3>Computed Rules</h3>
            <div className="dashboard-rules-wrap">
              <div className="dashboard-rules-head">
                <strong>Rules</strong>
                <button type="button" className="dashboard-type-btn" onClick={addComputeRule}>
                  + Add rule
                </button>
              </div>
              <div className="dashboard-rules-list">
                {(form.config.compute_rules || []).map((rule) => (
                  <div
                    key={rule.id}
                    className={String(form.type) === "meter_chart" ? "dashboard-rule-row dashboard-meter-rule-row" : "dashboard-rule-row"}
                  >
                    {String(form.type) === "meter_chart" ? (
                      <>
                        <input
                          value={rule.label || ""}
                          placeholder="Range label"
                          onChange={(e) => updateComputeRule(rule.id, { label: e.target.value })}
                        />
                        <input
                          value={rule.value1 ?? ""}
                          placeholder="Min value"
                          onChange={(e) => updateComputeRule(rule.id, { value1: e.target.value, operator: "between" })}
                        />
                        <input
                          value={rule.value2 ?? ""}
                          placeholder="Max value"
                          onChange={(e) => updateComputeRule(rule.id, { value2: e.target.value, operator: "between" })}
                        />
                        <input
                          type="color"
                          value={rule.color || "#14a89a"}
                          onChange={(e) => updateComputeRule(rule.id, { color: e.target.value })}
                        />
                        <button
                          type="button"
                          className="dashboard-widget-action-icon danger"
                          onClick={() => removeComputeRule(rule.id)}
                          title="Remove range"
                        >
                          <TrashIcon />
                        </button>
                      </>
                    ) : (
                      <>
                    <input
                      value={rule.label || ""}
                      placeholder="Item"
                      onChange={(e) => updateComputeRule(rule.id, { label: e.target.value })}
                    />
                    <select
                      value={rule.gateway_id || ""}
                      onChange={(e) =>
                        updateComputeRule(rule.id, { gateway_id: e.target.value, tag_name: "" })
                      }
                    >
                      <option value="">Any gateway</option>
                      {gatewayOptions.map((g) => (
                        <option key={g.id} value={g.id}>
                          {g.name}
                        </option>
                      ))}
                    </select>
                    <select
                      value={rule.tag_name || ""}
                      onChange={(e) => updateComputeRule(rule.id, { tag_name: e.target.value })}
                    >
                      <option value="">Any tag</option>
                      {tagsForRuleGateway(rule.gateway_id).map((tag) => (
                        <option key={`${rule.id}-${tag}`} value={tag}>
                          {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                        </option>
                      ))}
                    </select>
                    <select
                      value={rule.operator || "any"}
                      onChange={(e) => updateComputeRule(rule.id, { operator: e.target.value })}
                    >
                      {RULE_OPERATORS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <input
                      value={rule.value1 ?? ""}
                      placeholder="Value 1"
                      onChange={(e) => updateComputeRule(rule.id, { value1: e.target.value })}
                    />
                    {String(rule.operator || "any") === "between" ? (
                      <input
                        value={rule.value2 ?? ""}
                        placeholder="Value 2"
                        onChange={(e) => updateComputeRule(rule.id, { value2: e.target.value })}
                      />
                    ) : (
                      <div />
                    )}
                    <select
                      value={rule.aggregation || "count"}
                      onChange={(e) => updateComputeRule(rule.id, { aggregation: e.target.value })}
                    >
                      {RULE_AGGREGATIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <input
                      type="color"
                      value={rule.color || "#14a89a"}
                      onChange={(e) => updateComputeRule(rule.id, { color: e.target.value })}
                    />
                    <button
                      type="button"
                      className="dashboard-widget-action-icon danger"
                      onClick={() => removeComputeRule(rule.id)}
                      title="Remove rule"
                    >
                      <TrashIcon />
                    </button>
                      </>
                    )}
                  </div>
                ))}
                {!form.config.compute_rules?.length ? (
                  <div className="dashboard-config-note">No rules yet. Add one to build pie/table/meter datasets.</div>
                ) : null}
              </div>
            </div>
            <div className="row modal-actions">
              <button
                className="btn btn-primary"
                onClick={() => {
                  if (editingId) {
                    saveWidget();
                    setComputedModalOpen(false);
                    return;
                  }
                  setComputedModalOpen(false);
                }}
              >
                Done
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MenuStackIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </svg>
  );
}

function MoveCrossIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2v20" />
      <path d="M2 12h20" />
      <path d="M8 6l4-4 4 4" />
      <path d="M8 18l4 4 4-4" />
      <path d="M6 8l-4 4 4 4" />
      <path d="M18 8l4 4-4 4" />
    </svg>
  );
}

function MonitorIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 4h18v12H3z" />
      <path d="M8 20h8" />
      <path d="M12 16v4" />
      <path d="M7 11l3-3 2 2 4-4" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4h6v2" />
    </svg>
  );
}

function DuplicateIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V6a2 2 0 0 1 2-2h9" />
    </svg>
  );
}

function AddIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}

function CogIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 1.54V21a2 2 0 0 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15 1.7 1.7 0 0 0 3.06 14H3a2 2 0 0 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3.06V3a2 2 0 0 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.2.32.54.52.91.54H21a2 2 0 0 1 0 4h-.69c-.37.02-.71.22-.91.54z" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" />
      <path d="M7 10l5 5 5-5" />
      <path d="M5 21h14" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 21V9" />
      <path d="M17 14l-5-5-5 5" />
      <path d="M5 3h14" />
    </svg>
  );
}
