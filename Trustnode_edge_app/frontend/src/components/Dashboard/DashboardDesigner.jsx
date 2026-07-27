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
import { DASHBOARD_GRID_VIRTUAL_ROWS } from "./widgetRegistry";
import { materializePowerDashboardPayload } from "./powerDashboardTemplate";
import { filterRowsByRange, getLatestTagRow, toTsMs } from "./dashboardAnalytics";
import { classifyTag, checkTagForWidget, widgetIsNumericOnly, TAG_KIND } from "./tagTypes";
import { listReportTemplates } from "../../api";
import "./dashboard.css";

// Normalize a series' axis assignment to one of the FOUR canonical axes.
// Legacy configs stored only "left"/"right"; the multi-axis UI stores
// left1/left2/right1/right2. Both spellings must survive save.
function normSeriesAxis4(a) {
  const s = String(a || "left1").toLowerCase();
  if (s === "left2") return "left2";
  if (s === "right2") return "right2";
  if (s === "right" || s === "right1") return "right1";
  return "left1";
}

// ---------------------------------------------------------------------------
// Axis configuration — ONE place to configure each Y axis.
// Every axis's settings live under the config keys below (kept EXACTLY as they
// were so existing widgets keep working); the modal just presents them per axis
// instead of as a flat wall of fields, and only shows axes actually in use.
// ---------------------------------------------------------------------------
const AXIS_DEFS = [
  { id: "left1",  label: "Left 1",  side: "left",
    keys: { label: "y_axis_label",       unit: "primary_unit",      prefix: "y_axis_prefix",
            suffix: "primary_suffix",    decimals: "y_axis_decimals", format: "y_axis_format",
            mode: "y_axis_mode",         min: "y_min",              max: "y_max", step: "y_tick_step" } },
  { id: "left2",  label: "Left 2",  side: "left",
    keys: { label: "y_left2_label",      unit: "y_left2_unit",      prefix: "y_left2_prefix",
            suffix: "y_left2_suffix",    decimals: "y_left2_decimals", format: "y_left2_format",
            mode: "y_left2_axis_mode",   min: "y_left2_min",        max: "y_left2_max", step: "y_left2_tick_step" } },
  { id: "right1", label: "Right 1", side: "right",
    keys: { label: "y_axis_right_label", unit: "y_axis_right_unit", prefix: "y_axis_right_prefix",
            suffix: "y_axis_right_suffix", decimals: "y_axis_right_decimals", format: "y_axis_right_format",
            mode: "y_right_axis_mode",   min: "y_right_min",        max: "y_right_max", step: "y_right_tick_step" } },
  { id: "right2", label: "Right 2", side: "right",
    keys: { label: "y_right2_label",     unit: "y_right2_unit",     prefix: "y_right2_prefix",
            suffix: "y_right2_suffix",   decimals: "y_right2_decimals", format: "y_right2_format",
            mode: "y_right2_axis_mode",  min: "y_right2_min",       max: "y_right2_max", step: "y_right2_tick_step" } },
];

// Which axes a chart actually uses: left1 is always present (the primary
// series); the rest appear only when a series is assigned to them.
function axesInUseFromConfig(cfg) {
  const used = new Set(["left1"]);
  const extras = Array.isArray(cfg?.series_extra) ? cfg.series_extra : [];
  for (const s of extras) {
    if (String(s?.chart_type || "").toLowerCase() === "limit") continue;
    used.add(normSeriesAxis4(s?.axis));
  }
  return used;
}

// One modal to configure every Y axis in use: label, prefix/unit/suffix,
// decimals, value format, and the scale (auto or manual min/max/tick).
function AxisConfigModal({ config, onChange, onClose, widgetValueFormat, widgetType }) {
  const isStacked = String(widgetType || "") === "stacked_trend";
  const used = axesInUseFromConfig(config);
  const shown = isStacked ? [] : AXIS_DEFS.filter((a) => used.has(a.id));
  // Stacked Trend lanes: ONE ROW PER TAG (primary + each data series).
  // Bounds write to primary_lane_min/max and the series rows' y_min/y_max —
  // exactly the fields the lane renderer reads, so changes apply on save.
  const stackedLanes = isStacked
    ? [
        ...(String(config?.tag_name || "").trim()
          ? [{ kind: "primary", label: String(config?.tag_name || "Primary"), min: config?.primary_lane_min ?? "", max: config?.primary_lane_max ?? "" }]
          : []),
        ...((Array.isArray(config?.series_extra) ? config.series_extra : [])
          .map((s, idx) => ({ s, idx }))
          .filter(({ s }) => s && String(s.chart_type || "") !== "limit" && String(s.tag_name || "").trim())
          .map(({ s, idx }) => ({
            kind: "series", idx,
            label: String(s.label || s.tag_name || `Series ${idx + 1}`),
            color: String(s.color || ""),
            min: s.y_min ?? "", max: s.y_max ?? "",
          }))),
      ]
    : [];
  const setLaneBound = (lane, field, value) => {
    if (lane.kind === "primary") {
      onChange({ [field === "min" ? "primary_lane_min" : "primary_lane_max"]: value });
      return;
    }
    const list = Array.isArray(config?.series_extra) ? [...config.series_extra] : [];
    list[lane.idx] = { ...(list[lane.idx] || {}), [field === "min" ? "y_min" : "y_max"]: value };
    onChange({ series_extra: list });
  };
  const set = (key, value) => onChange({ [key]: value });
  const txt = (k, ph, title) => (
    <label className="dashboard-query-field" title={title || ""}>
      <span>{ph.label}</span>
      <input value={config?.[k] ?? ""} placeholder={ph.placeholder || "optional"}
        onChange={(e) => set(k, e.target.value)} />
    </label>
  );
  const num = (k, label, placeholder, extra = {}) => (
    <label className="dashboard-query-field">
      <span>{label}</span>
      <input type="number" step="any" value={config?.[k] ?? ""} placeholder={placeholder || ""}
        onChange={(e) => set(k, e.target.value === "" ? "" : Number(e.target.value))} {...extra} />
    </label>
  );
  return (
    <div className="modal-backdrop" style={{ zIndex: 70 }} onClick={onClose}>
      <div className="modal-card dashboard-query-modal dashboard-query-modal-wide"
        style={{ width: "min(1000px, 96vw)", maxHeight: "92vh", overflowY: "auto" }}
        onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
          <h3 style={{ margin: 0 }}>Axis configuration</h3>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>
        <p className="dashboard-query-hint" style={{ marginTop: 0 }}>
          {isStacked
            ? "Each configured tag renders as its own lane. Set the lane's Y range here (blank = auto)."
            : "Only axes used by a series are shown. Assign a series to Left 2 / Right 1 / Right 2 in Series & Axes to configure it here."}
        </p>
        {isStacked ? (
          <fieldset className="dashboard-query-fieldset">
            <legend>Lanes (one per tag)</legend>
            {stackedLanes.length ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ display: "grid", gridTemplateColumns: "minmax(220px, 1fr) 140px 140px", gap: 8, fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", opacity: 0.7, padding: "0 4px" }}>
                  <span>Lane / Tag</span><span>Y Min</span><span>Y Max</span>
                </div>
                {stackedLanes.map((lane) => (
                  <div key={`${lane.kind}-${lane.idx ?? "p"}`}
                    style={{ display: "grid", gridTemplateColumns: "minmax(220px, 1fr) 140px 140px", gap: 8, alignItems: "center", padding: "2px 4px" }}>
                    <span style={{ fontWeight: 600, color: lane.color || undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={lane.label}>
                      {lane.label}
                    </span>
                    <input type="number" step="any" value={lane.min} placeholder="auto"
                      onChange={(e) => setLaneBound(lane, "min", e.target.value)} />
                    <input type="number" step="any" value={lane.max} placeholder="auto"
                      onChange={(e) => setLaneBound(lane, "max", e.target.value)} />
                  </div>
                ))}
              </div>
            ) : (
              <p className="dashboard-query-hint">No lanes yet — pick a primary Tag or add series in the Series editor.</p>
            )}
          </fieldset>
        ) : null}
        {shown.map((ax) => {
          const k = ax.keys;
          const manual = String(config?.[k.mode] || "auto").toLowerCase() === "manual";
          return (
            <fieldset key={ax.id} className="dashboard-query-fieldset" style={{ marginBottom: 10 }}>
              <legend>{ax.label} <span style={{ opacity: 0.6, fontWeight: 400 }}>({ax.side})</span></legend>
              {/* Row 1 — identity + formatting */}
              <div className="dashboard-query-grid">
                {txt(k.label,  { label: "Axis label", placeholder: "e.g. Temperature" })}
                {txt(k.prefix, { label: "Prefix", placeholder: "e.g. $" })}
                {txt(k.unit,   { label: "Unit", placeholder: "e.g. °C" })}
                {txt(k.suffix, { label: "Suffix", placeholder: "optional" })}
                {num(k.decimals, "Decimals", "auto", { min: 0, max: 6, step: 1 })}
                <label className="dashboard-query-field">
                  <span>Data format</span>
                  <select value={String(config?.[k.format] || "")}
                    onChange={(e) => set(k.format, e.target.value)}>
                    <option value="">(widget: {String(widgetValueFormat || "auto")})</option>
                    <option value="auto">Auto</option>
                    <option value="int">Integer</option>
                    <option value="2dp">2 decimals</option>
                    <option value="3dp">3 decimals</option>
                    <option value="scientific">Scientific</option>
                  </select>
                </label>
              </div>
              {/* Row 2 — scale */}
              <div className="dashboard-query-grid" style={{ marginTop: 6 }}>
                <label className="dashboard-query-field">
                  <span>Scale</span>
                  <select value={manual ? "manual" : "auto"}
                    onChange={(e) => set(k.mode, e.target.value === "manual" ? "manual" : "auto")}>
                    <option value="auto">Auto (from data)</option>
                    <option value="manual">Manual (min / max / step)</option>
                  </select>
                </label>
                {manual ? (
                  <>
                    {num(k.min, "Min", "")}
                    {num(k.max, "Max", "")}
                    {num(k.step, "Tick step", "auto", { min: 0 })}
                  </>
                ) : null}
              </div>
            </fieldset>
          );
        })}
        <div className="row modal-actions">
          <button type="button" className="btn btn-primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}

const TYPE_GROUPS = ["Charts", "KPI", "Content", "Layout", "Media", "Reports", "System", "Batch"];
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

function formatHeaderValue(value, decimals = 3) {
  // null/undefined/"" are ABSENT, not zero. Number(null) === 0 and passes
  // isFinite, so without this guard a text tag (stored value=NULL) rendered as
  // "0.000" in the widget header — an absent value must never look like a
  // real reading.
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  const d = Number.isFinite(Number(decimals))
    ? Math.max(0, Math.min(6, Math.floor(Number(decimals))))
    : 3;
  return n.toFixed(d);
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

// =====================================================================
// ReportCardEditor — picks a saved report template for the dashboard's
// Report Card widget. Schedule + trigger configuration lives in
// Scheduled Reports; the editor links there so we don't duplicate the
// form here.
// =====================================================================
function ReportCardEditor({ config, onChange }) {
  const [templates, setTemplates] = useState([]);
  const [loadErr, setLoadErr] = useState("");
  useEffect(() => {
    (async () => {
      try {
        const res = await listReportTemplates();
        const rows = Array.isArray(res?.templates) ? res.templates
          : (Array.isArray(res?.rows) ? res.rows : []);
        setTemplates(rows);
      } catch (err) {
        setLoadErr(String(err?.message || err));
      }
    })();
  }, []);
  const value = String(config?.report_template_id || "");
  const viewMode = String(config?.report_view_mode || "summary");
  const refreshMin = Number(config?.report_refresh_minutes || 0);
  return (
    <>
      <label className="dashboard-full-row">
        Report template
        <select
          value={value}
          onChange={(e) => onChange({ report_template_id: e.target.value })}
        >
          <option value="">(pick a template)</option>
          {templates.map((t) => (
            <option key={t.id} value={t.id}>{t.name || t.id}</option>
          ))}
        </select>
      </label>
      <label className="dashboard-full-row">
        Widget view
        <select
          value={viewMode}
          onChange={(e) => onChange({ report_view_mode: e.target.value })}
        >
          <option value="summary">Summary — name, last PDF, Generate button</option>
          <option value="pdf_preview">Embed last PDF in the card</option>
          <option value="html_preview">Render the report as live HTML</option>
        </select>
      </label>
      <label className="dashboard-full-row">
        Auto-refresh (minutes; 0 = manual only)
        <input
          type="number"
          min={0}
          max={1440}
          value={refreshMin}
          onChange={(e) => {
            const n = Math.max(0, Math.min(1440, Math.round(Number(e.target.value || 0))));
            onChange({ report_refresh_minutes: n });
          }}
          placeholder="0"
        />
      </label>
      <p className="dashboard-query-hint">
        <strong>Summary</strong> shows the template name, last generated
        PDF, and a Generate-now button (best in a compact 5×3 slot).
        {" "}<strong>Embed last PDF</strong> renders the latest PDF in
        an iframe — re-run Generate to refresh. <strong>Render as live
        HTML</strong> walks the template sections (KPIs, charts, tables)
        and renders them inline so the report layout stays in sync with
        live data. Trigger (time-based or tag-based) and email delivery
        are still configured per template under Scheduled Reports.
      </p>
      {loadErr ? <p className="dashboard-query-hint warn">{loadErr}</p> : null}
    </>
  );
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
      unit_suffix: "",
      // Operator 2026-06-16: KPI value precision in the body and the
      // header strip. 0..6 — clamped by the renderer.
      value_decimals: 3,
      // Operator 2026-06-16: unit-suffix size multiplier of the value
      // font. 1 = same size inline.
      unit_size_scale: 1,
      // Operator 2026-06-16: which pieces appear in the title bar
      // (value | tag | title). New widgets start with all three.
      header_parts: ["value", "tag", "title"],
      // Energy Tariffs widget (operator 2026-06-16) defaults.
      display_mode: "donut",
      tariff_value_mode: "cost",
      source_url: "",
      camera_url: "",
      list_limit: 8,
      query_group_interval: "none",
      // Default to "last" for chart widgets so picking a time grouping
      // shows actual values not sample counts. "count" is only useful
      // for rule-based widgets.
      query_result_aggregation: ["line_chart", "line_area_chart", "bar_chart", "stacked_trend", "value_kpi", "meter_chart"].includes(String(type))
        ? "last"
        : "count",
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
  // Optional gating: returns true iff the given license-module key is
  // active for this customer. Used to hide module-locked widgets in the
  // picker. Defaults to "always true" when not provided (existing
  // dashboards keep working in non-edge contexts like cloud preview).
  isLicenseModuleEnabled = null,
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

  // Power-default dashboard preset (operator 2026-06-15: "if the
  // power management module is enabled we should have a new
  // dashboard profile (default) available in the dashboard, create
  // one that replicates the power overview pages without the
  // filters"). Builds 6 stat tiles + 2 chart cards for the first
  // power meter in the catalog. Bound to REAL historian tags so the
  // widgets actually fetch data — earlier version pointed at
  // insight.* virtual tags which the historian doesn't carry, so
  // every widget rendered "Historian fetch failed".
  const buildPowerDefaultPayload = useCallback(() => {
    const meters = (Array.isArray(gatewayCatalog) ? gatewayCatalog : []).filter((g) => g?.power_meter);
    const meter = meters[0] || null;
    if (!meter) return null;
    const gwId = String(meter.id || "");
    // Operator 2026-06-16: load the richer Power dashboard template
    // (8 KPI strip + 2 power trend charts + Energy Tariffs donut +
    // bars). The template is shared so the same layout can be
    // re-applied to any meter the operator chooses.
    const payload = materializePowerDashboardPayload(gwId);
    return { name: "Power Default", ...payload };
  }, [gatewayCatalog]);

  const applyPowerDefaultProfile = useCallback(() => {
    const payload = buildPowerDefaultPayload();
    if (!payload) return;
    applyProfilePayload(payload);
    setActiveProfileName("Power Default");
    // Operator 2026-06-17: persist Power Default as a regular
    // profile so it shows in the dropdown and the operator can
    // edit / delete widgets and have the changes survive a
    // reload. Without this it lived only as a transient name
    // attached to dashboardWidgets, which confused operators
    // who expected the profile dropdown to behave like the
    // other entries.
    if (!profiles.some((p) => p.name === "Power Default")) {
      persistProfiles([...profiles, { ...payload, name: "Power Default" }]);
    }
  }, [applyProfilePayload, buildPowerDefaultPayload, profiles, persistProfiles]);

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
  // Dedicated axis-configuration modal (all Y axes, one card per axis in use).
  const [axisModalOpen, setAxisModalOpen] = useState(false);
  // Live rows used ONLY to infer a tag's type when the controller's declared
  // type isn't known (see tagTypes.js). Never used for rendering.
  const tagRowsForTypes = Array.isArray(dataLogView) ? dataLogView : (Array.isArray(tagRows) ? tagRows : []);
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
    const isChartWidget = ["line_chart", "line_area_chart", "bar_chart", "stacked_trend", "pie_chart", "meter_chart"].includes(String(widget?.type || ""));
    // Operator 2026-06-16: KPI widgets (text_kpi / value_kpi) also
    // get the same "value | tag | title" header treatment as charts
    // so the operator can see at a glance which tag a KPI maps to.
    const isKpi = ["text_kpi", "value_kpi"].includes(String(widget?.type || ""));
    if (!isChartWidget && !isKpi) return null;
    const gatewayId = String(cfg.gateway_id || "");
    const tagName = String(cfg.tag_name || "");
    const latest = getLatestTagRow(dashboardRows, gatewayId, tagName);
    const headerDecimals = cfg.value_decimals;
    // TEXT-typed tags (PLC STRING) store value=NULL + the string in
    // value_text. Show the text — it IS the tag's value — instead of the
    // numeric formatter's placeholder. Numeric tags are unaffected.
    const latestValue = latest?.last_value_text != null && String(latest.last_value_text) !== ""
      ? String(latest.last_value_text)
      : formatHeaderValue(latest?.last_value, headerDecimals);
    const plcTag = formatTagForDisplay ? formatTagForDisplay(tagName) : tagName || "-";
    const lastTsMs = toTsMs(latest?.last_ts || "");
    const liveLatencyMs = Number.isFinite(lastTsMs) ? Math.max(0, Date.now() - lastTsMs) : null;
    // Build a list of every visible series (primary + series_extra data
    // traces, skip limit-lines) so the title strip can show one
    // "value | tag" pair per series — including when the primary
    // gateway/tag is unset and the widget is series-only.
    // Apply the saved unit / suffix when rendering the live value in
    // the header. Operator config order:
    //   - suffix wins (treated as a glyph that hugs the number, e.g. %),
    //   - unit appended with a single space when no suffix,
    //   - bare value when neither configured.
    // Same convention the tooltip uses inside LiveTagChart so the
    // header value and the chart tooltip stay consistent.
    const decorateValue = (rawText, unitTxt, suffixTxt) => {
      if (rawText === "-" || rawText === "—" || !rawText) return rawText;
      const suf = String(suffixTxt || "").trim();
      if (suf) return `${rawText}${suf}`;
      const u = String(unitTxt || "").trim();
      if (u) return `${rawText} ${u}`;
      return rawText;
    };
    // KPI widgets store the unit under cfg.unit_suffix (operator
    // 2026-06-16). Treat it as a unit (space-separated) so the
    // header value reads "0.350 A" instead of "0.350A".
    const primaryUnit = String(cfg.primary_unit || cfg.unit_suffix || "");
    const primarySuffix = String(cfg.primary_suffix || "");
    const seriesItems = [];
    if (gatewayId && tagName) {
      seriesItems.push({
        value: decorateValue(latestValue, primaryUnit, primarySuffix),
        tag: plcTag,
        color: String(widget?.color || "#14a89a"),
      });
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
      // Same text-first rule as the primary series (see above).
      const sValue = sLatest?.last_value_text != null && String(sLatest.last_value_text) !== ""
        ? String(sLatest.last_value_text)
        : formatHeaderValue(sLatest?.last_value);
      const sLabel = String(s?.label || "").trim() || (formatTagForDisplay ? formatTagForDisplay(sTag) : sTag);
      seriesItems.push({
        value: decorateValue(sValue, s?.unit, s?.suffix),
        tag: sLabel,
        color: String(s?.color || "").trim() || fallbackPalette[paletteIdx % fallbackPalette.length],
      });
      paletteIdx += 1;
    }
    // Decorate the single-series fallback display too so the
    // single-series header path matches the multi-series rendering.
    const decoratedLatest = decorateValue(latestValue, primaryUnit, primarySuffix);
    return { latestValue: decoratedLatest, plcTag, typeLabel, liveLatencyMs, seriesItems };
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
        // Spread the existing widget config first so EVERY field the
        // operator ever saved (Y axis manual mode + min/max + tick
        // step, right-axis variants, hide_widget_header, every chart
        // styling knob, multiplier/offset, etc.) loads into the form
        // intact. The explicit defaults below still normalize the
        // known fields, but un-listed fields no longer get silently
        // wiped on open.
        ...(widget?.config || {}),
        gateway_id: widget?.config?.gateway_id || "",
        tag_name: widget?.config?.tag_name || "",
        readings_count: clamp(widget?.config?.readings_count ?? 120, 5, 5000),
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
    const target = normalizeWidgets(widgets).find((w) => String(w.id) === String(id));
    if (!target) return;
    const labelBits = [
      String(target?.title || "").trim(),
      String(target?.config?.tag_name || "").trim(),
      String(target?.config?.gateway_id || "").trim(),
    ].filter(Boolean);
    const labelTxt = labelBits.length ? `\n\n${labelBits.join("  ·  ")}` : "";
    // Browser confirm() is the right tool: blocking + native + carries
    // the operator's full attention. Anything inline would let an
    // accidental enter-press resolve the dialog. Operator request
    // 2026-06-12: "we need to confirm to avoid accidentally delete a
    // configured widget".
    const ok = window.confirm(
      `Delete this widget?${labelTxt}\n\nThis action cannot be undone.`,
    );
    if (!ok) return;
    setWidgets((prev) => normalizeWidgets(prev).filter((w) => w.id !== id));
  };

  // Download the full widget configuration as a JSON file. Operator
  // request 2026-06-12: "we need an option to download the widget
  // configuration completed". The file format mirrors the import
  // payload the dashboard already understands ({widgets:[...]}) so an
  // exported widget can be imported on another edge or pasted into a
  // git-tracked dashboard profile.
  const exportWidget = (id) => {
    const target = normalizeWidgets(widgets).find((w) => String(w.id) === String(id));
    if (!target) return;
    const payload = {
      schema: "trustnode.dashboard.widget/1",
      exported_utc: new Date().toISOString(),
      widget: target,
    };
    try {
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const safeName = String(target?.title || target?.type || "widget")
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 60) || "widget";
      a.href = url;
      a.download = `${safeName}-${target.id}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Revoke after the click cycle so the browser has finished the download.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      console.warn("exportWidget failed", err);
    }
  };

  // Import a widget configuration from a previously-downloaded JSON
  // file (the per-widget export button writes one). Merges the file's
  // payload into the currently-open form, preserving id/position so
  // the widget doesn't jump or replace another. Used from the modal
  // tab strip via the small upload icon next to Configure.
  const widgetImportFileRef = useRef(null);
  const [widgetImportNotice, setWidgetImportNotice] = useState("");
  const onImportWidgetFile = async (e) => {
    const file = e?.target?.files?.[0];
    if (!file) return;
    setWidgetImportNotice("");
    try {
      const txt = await file.text();
      const parsed = JSON.parse(txt);
      const incoming = parsed && typeof parsed === "object" && parsed.widget && typeof parsed.widget === "object"
        ? parsed.widget
        : parsed && typeof parsed === "object" && parsed.config && typeof parsed.config === "object"
          ? parsed
          : null;
      if (!incoming) throw new Error("File does not contain a widget object");
      const incomingCfg = (incoming.config && typeof incoming.config === "object") ? incoming.config : {};
      setForm((p) => ({
        ...p,
        // Adopt the imported type / title / color / size but never the
        // imported id (would clash if the operator imports back into the
        // same dashboard) and never the imported position (let the grid
        // keep where the widget already lives, OR fill the first slot for
        // a brand-new widget — both work because form.x/y stay as-is).
        type: typeof incoming.type === "string" && incoming.type ? incoming.type : p.type,
        title: typeof incoming.title === "string" && incoming.title ? incoming.title : p.title,
        color: typeof incoming.color === "string" && incoming.color ? incoming.color : p.color,
        w: Number.isFinite(Number(incoming.w)) ? Number(incoming.w) : p.w,
        h: Number.isFinite(Number(incoming.h)) ? Number(incoming.h) : p.h,
        config: { ...(p.config || {}), ...incomingCfg },
      }));
      setTab("config");
      setWidgetImportNotice(`Imported from ${file.name}`);
    } catch (err) {
      setWidgetImportNotice(`Failed: ${String(err?.message || err || "import error").slice(0, 120)}`);
    } finally {
      // Reset value so re-picking the SAME file fires onChange again.
      if (widgetImportFileRef.current) widgetImportFileRef.current.value = "";
    }
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
    // Fire while EITHER the main editor modal or the nested
    // "Series, Axes & Data Query" modal is open. Previously the
    // effect only fired with modalOpen=true — when the operator
    // edited Y axis fields inside the nested query modal, the live
    // preview behind it never picked up the change until they
    // explicitly clicked Done.
    if (!canEdit || !editingId) return;
    if (!modalOpen && !queryModalOpen) return;
    if (liveApplyDebounceRef.current) clearTimeout(liveApplyDebounceRef.current);
    liveApplyDebounceRef.current = setTimeout(() => {
      applyCurrentFormConfigToEditingWidget();
    }, 120);
    return () => {
      if (liveApplyDebounceRef.current) clearTimeout(liveApplyDebounceRef.current);
    };
  }, [form?.config, editingId, canEdit, modalOpen, queryModalOpen]);

  const saveWidget = () => {
    // TAG TYPE INTERLOCK — a numeric-only widget (chart/gauge/pie) cannot plot
    // a TEXT tag: the historian stores those with value=NULL, so the widget
    // would render an empty/flat chart with no explanation. Block ONLY when we
    // are confident the tag carries text (declared STRING, or observed
    // text-only). Anything unknown is allowed through by design — a tag that
    // simply hasn't been collected yet must never be refused.
    if (widgetIsNumericOnly(form.type)) {
      const offenders = [];
      const primaryGwId = String(form?.config?.gateway_id || "").trim();
      const primaryTag = String(form?.config?.tag_name || "").trim();
      if (primaryTag) {
        const v = checkTagForWidget(form.type, primaryGwId, primaryTag, tagRowsForTypes);
        if (v.severity === "block") offenders.push(v.message);
      }
      for (const s of (Array.isArray(form?.config?.series_extra) ? form.config.series_extra : [])) {
        const tg = String(s?.tag_name || "").trim();
        if (!tg) continue;
        if (String(s?.chart_type || "").toLowerCase() === "limit") continue;
        const v = checkTagForWidget(form.type, String(s?.gateway_id || "").trim() || primaryGwId, tg, tagRowsForTypes);
        if (v.severity === "block") offenders.push(v.message);
      }
      if (offenders.length) {
        const msg = "This widget type can only plot numeric tags:\n\n"
          + offenders.map((m) => `• ${m}`).join("\n")
          + "\n\nShow text tags with a Value KPI or a Table widget instead.";
        try { window.alert(msg); } catch (_) {}
        return;
      }
    }
    // Operator 2026-06-16: trend widgets can carry multiple series
    // (primary + series_extra) — but the X-axis is a single shared
    // time axis. If two series come from gateways with different
    // poll intervals (e.g. PLC at 1s, meter at 10s), the chart looks
    // gappy because the slower series has missing union timestamps.
    // Block save with a clear message instead of letting the
    // operator chase visual artefacts.
    if (["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(String(form.type))) {
      const primaryGw = String(form?.config?.gateway_id || "").trim();
      const extras = Array.isArray(form?.config?.series_extra) ? form.config.series_extra : [];
      const intervalOf = (gid) => {
        if (!gid) return null;
        const g = (gatewayCatalog || []).find((x) => String(x.id || "") === String(gid));
        const interval = Number(g?.interval_ms || g?.poll_interval_ms || 0);
        return Number.isFinite(interval) && interval > 0 ? interval : null;
      };
      const primaryInterval = intervalOf(primaryGw);
      const mismatches = [];
      for (const s of extras) {
        const sid = String(s?.gateway_id || "").trim() || primaryGw;
        const si = intervalOf(sid);
        if (primaryInterval && si && si !== primaryInterval) {
          mismatches.push(`${sid} (${si} ms) ≠ ${primaryGw} (${primaryInterval} ms)`);
        }
      }
      if (mismatches.length) {
        const msg =
          "This chart mixes gateways with different poll intervals:\n\n" +
          mismatches.map((m) => `• ${m}`).join("\n") +
          "\n\nA shared chart needs gateways that poll at the same rate, otherwise the slower series will look gappy. Match the intervals on the Gateway pages or use separate widgets.";
        try { window.alert(msg); } catch (_) {}
        return;
      }
    }
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
      y: Number.isFinite(Number(form.y)) ? clamp(form.y, 0, DASHBOARD_GRID_VIRTUAL_ROWS - 1) : null,
      config: {
        // Spread the form's full config first so ANY field the operator
        // touched (Y axis mode/min/max/tick_step, right-axis variants,
        // hide_widget_header, body_text_scale, chart_show_legend,
        // multiplier/offset, plc_endpoint, etc.) survives the save.
        // Previously saveWidget rebuilt the object from an explicit
        // allowlist and silently DROPPED every field not in the list —
        // that's why "configure Y axis manual + min/max" never took
        // effect: the form held it, the save erased it. The explicit
        // overrides below still sanitize the known fields (defaults,
        // clamping, enum coercion).
        ...form?.config,
        gateway_id: String(form?.config?.gateway_id || ""),
        tag_name: String(form?.config?.tag_name || ""),
        readings_count: clamp(form?.config?.readings_count ?? 120, 5, 5000),
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
        // Chart widgets default to "last" so picking a time grouping
        // produces a chart with real values (most recent in bucket),
        // not sample counts. Rule widgets keep "count".
        query_result_aggregation: RULE_AGGREGATIONS.some((opt) => opt.value === form?.config?.query_result_aggregation)
          ? form?.config?.query_result_aggregation
          : (["line_chart", "line_area_chart", "bar_chart", "stacked_trend", "value_kpi", "meter_chart"].includes(String(form?.type || ""))
              ? "last"
              : "count"),
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
                axis: normSeriesAxis4(s.axis),
                chart_type: String(s.chart_type || ""),
                unit: String(s.unit || ""),
                suffix: String(s.suffix || ""),
                multiplier: Number(s.multiplier ?? 1) || 1,
                offset: Number(s.offset ?? 0) || 0,
                limit_value: s.limit_value === undefined || s.limit_value === null ? "" : String(s.limit_value),
                // Per-lane Y bounds (Stacked Trend). The allowlist used to
                // DROP these on save — the operator's axis changes rendered
                // until reopen, then vanished. "" = auto.
                y_min: s.y_min === undefined || s.y_min === null ? "" : String(s.y_min),
                y_max: s.y_max === undefined || s.y_max === null ? "" : String(s.y_max),
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
    candidate.y = clamp(pos.y, 0, DASHBOARD_GRID_VIRTUAL_ROWS - candidate.h);
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

  // Rows the canvas must render: at least one full screen, growing with the
  // lowest widget so there is always spare space to drop into.
  const gridRowsNeeded = useMemo(() => {
    const lowest = (Array.isArray(widgets) ? widgets : []).reduce(
      (m, w) => Math.max(m, (Number(w?.y) || 0) + (Number(w?.h) || 1)), 0);
    return Math.min(DASHBOARD_GRID_VIRTUAL_ROWS, Math.max(DASHBOARD_GRID_ROWS, lowest + 6));
  }, [widgets]);

  // Scroll-aware cell math shared by drag-over and resize: the grid content
  // can be taller than the viewport now, so Y offsets must include
  // scrollTop and divide by the REAL row pitch (row height + gap).
  const gridCellMetrics = () => {
    const el = gridRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const gap = 8;
    const rows = Math.max(1, gridRowsNeeded);
    const rowPitch = (el.scrollHeight + gap) / rows; // cell + gap
    const cellW = rect.width / DASHBOARD_GRID_COLS;
    return { el, rect, gap, rows, rowPitch, cellW };
  };

  const onGridDragOver = (e) => {
    if (!canEdit || !draggingId || !gridRef.current) return;
    e.preventDefault();
    const m = gridCellMetrics();
    if (!m) return;
    const x = clamp(Math.floor((e.clientX - m.rect.left) / Math.max(1, m.cellW)), 0, DASHBOARD_GRID_COLS - 1);
    const y = clamp(
      Math.floor((e.clientY - m.rect.top + m.el.scrollTop) / Math.max(1, m.rowPitch)),
      0, DASHBOARD_GRID_VIRTUAL_ROWS - 1);
    dragHoverCellRef.current = { x, y };
  };

  const onResizeStart = (e, widget) => {
    if (!canEdit) return;
    e.preventDefault();
    e.stopPropagation();
    const gridEl = gridRef.current;
    if (!gridEl) return;
    const m0 = gridCellMetrics();
    const cellW = m0 ? m0.cellW : gridEl.getBoundingClientRect().width / DASHBOARD_GRID_COLS;
    const cellH = m0 ? m0.rowPitch : gridEl.getBoundingClientRect().height / DASHBOARD_GRID_ROWS;
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
          // 2026-07-27: the canvas GROWS vertically (and scrolls) instead of
          // hard-capping at one screen. Row height stays what a 40-row
          // first screen implies (identical look for existing layouts);
          // extra rows render at the same height below the fold, so
          // placement never has to overlap because it "ran out of rows".
          gridTemplateRows: `repeat(${gridRowsNeeded}, max(8px, calc((100vh - 220px - ${(DASHBOARD_GRID_ROWS - 1) * 8}px) / ${DASHBOARD_GRID_ROWS})))`,
        }}
      >
        {normalizedWidgets.map((widget) => (
          <article
            key={widget.id}
            className={`card dashboard-widget-shell ${draggingId === widget.id ? "is-dragging" : ""} ${Boolean(widget?.config?.hide_widget_header) ? "is-headerless" : ""}`}
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
            {/* Hide-header applies for every role. The Edit / Delete /
                Drag controls are still reachable: the operator can click
                anywhere on the widget body to bring up the actions
                overlay via the dashboard menu, and the resize handle
                stays in the bottom-right corner. Previous version gated
                the hide on !canEdit which meant admins NEVER saw the
                effect; the operator request was the opposite. */}
            <div className="dashboard-widget-head" style={Boolean(widget?.config?.hide_widget_header) ? { display: "none" } : undefined}>
              <strong>
                {(() => {
                  const parts = widgetHeaderParts(widget);
                  if (!parts) {
                    // Operator 2026-06-16: non-chart widgets (KPIs,
                    // text, table, image, …) should honour the
                    // operator-edited title. Falling back to the
                    // widget-type label ("Value KPI (Tag)") ignored
                    // the title field entirely.
                    return String(widget?.title || getWidgetMeta(widget.type)?.label || widget.type);
                  }
                  // Operator 2026-06-16: which header pieces show is
                  // configurable per widget. cfg.header_parts is an
                  // array of: "value" | "tag" | "title" (the
                  // operator-edited widget title). Empty/missing →
                  // legacy default of all three.
                  const cfgParts = widget?.config?.header_parts;
                  const enabled = Array.isArray(cfgParts) && cfgParts.length
                    ? new Set(cfgParts.map((s) => String(s).toLowerCase()))
                    : new Set(["value", "tag", "title"]);
                  const showValue = enabled.has("value");
                  const showTag = enabled.has("tag");
                  const showTitle = enabled.has("title");
                  const joiner = (key) => <span className="dashboard-widget-head-sep">|</span>;
                  const items = Array.isArray(parts.seriesItems) ? parts.seriesItems : [];
                  if (items.length > 1) {
                    // Multi-series widget: render one "value | tag" pair
                    // per visible series so the operator can read every
                    // current value without opening the chart legend.
                    return (
                      <span className="dashboard-widget-head-text">
                        {items.map((it, idx) => {
                          const pieces = [];
                          if (idx > 0) pieces.push(<span key={`sep-${idx}`} className="dashboard-widget-head-sep">·</span>);
                          if (showValue) pieces.push(<span key={`v-${idx}`} className="dashboard-widget-head-value" style={{ color: it.color }}>{it.value}</span>);
                          if (showValue && showTag) pieces.push(<span key={`vt-${idx}`} className="dashboard-widget-head-sep">|</span>);
                          if (showTag) pieces.push(<span key={`t-${idx}`}>{it.tag}</span>);
                          return <React.Fragment key={`hd-${idx}`}>{pieces}</React.Fragment>;
                        })}
                        {showTitle && (showValue || showTag) ? joiner("title") : null}
                        {showTitle ? <span>{parts.typeLabel}</span> : null}
                      </span>
                    );
                  }
                  const segments = [];
                  if (showValue) segments.push(
                    <span key="v" className="dashboard-widget-head-value" style={{ color: String(widget?.color || "#14a89a") }}>{parts.latestValue}</span>
                  );
                  if (showTag) segments.push(<span key="t">{parts.plcTag}</span>);
                  if (showTitle) segments.push(<span key="ti">{parts.typeLabel}</span>);
                  return (
                    <span className="dashboard-widget-head-text">
                      {segments.flatMap((node, i) => i === 0 ? [node] : [<span key={`sep-${i}`} className="dashboard-widget-head-sep">|</span>, node])}
                    </span>
                  );
                })()}
              </strong>
              <div className="dashboard-widget-head-actions">
                {canEdit ? (
                  <>
                    {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(String(widget?.type || "")) ? (
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
                          className="dashboard-widget-action-icon"
                          onClick={() => {
                            exportWidget(widget.id);
                            setMenuWidgetId("");
                          }}
                          title="Download widget configuration (JSON)"
                          aria-label="Download widget configuration"
                        >
                          {/* Inline download icon — small enough to live in
                              the action strip without dragging in another
                              icon import. Matches the stroke weight of the
                              TrashIcon / DuplicateIcon next to it. */}
                          <svg
                            viewBox="0 0 24 24"
                            width="14"
                            height="14"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          >
                            <path d="M12 3v12" />
                            <path d="m7 10 5 5 5-5" />
                            <path d="M5 21h14" />
                          </svg>
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
            {/* Floating actions overlay — shown only when the head is
                hidden. Without this an admin who ticked "Hide widget
                title bar" had no way to reach Edit / Delete / Drag
                anymore. The overlay sits in the top-right corner, fades
                in on hover, and re-exposes the same three controls. */}
            {Boolean(widget?.config?.hide_widget_header) && canEdit ? (
              <div className="dashboard-widget-headerless-actions">
                <button
                  type="button"
                  className="dashboard-widget-menu-btn"
                  title="Edit widget"
                  aria-label="Edit widget"
                  onClick={() => openEdit(widget)}
                >
                  <PencilIcon />
                </button>
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
                  title="Delete widget"
                  aria-label="Delete widget"
                  onClick={() => removeWidget(widget.id)}
                >
                  <TrashIcon />
                </button>
              </div>
            ) : null}
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
              historicalFromLocal={dashboardFrom}
              historicalToLocal={dashboardTo}
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
              {/* Operator request 2026-06-12: a small import icon next
                  to Configure that loads a JSON file produced by the
                  per-widget download button. Reads + parses on the
                  client, then merges fields into form (preserving the
                  widget's own id/position so importing doesn't move
                  it). Hidden <input type="file"> keeps the trigger
                  visually a clean icon button. */}
              <input
                ref={widgetImportFileRef}
                type="file"
                accept="application/json,.json"
                style={{ display: "none" }}
                onChange={onImportWidgetFile}
              />
              <button
                type="button"
                className="dashboard-pill"
                onClick={() => widgetImportFileRef.current && widgetImportFileRef.current.click()}
                title="Import widget configuration (JSON file)"
                aria-label="Import widget configuration"
                style={{ padding: "6px 10px" }}
              >
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 3v12" />
                  <path d="m17 8-5-5-5 5" />
                  <path d="M5 21h14" />
                </svg>
              </button>
              {widgetImportNotice ? (
                <span className={`muted ${widgetImportNotice.startsWith("Failed") ? "error" : ""}`} style={{ fontSize: 12, alignSelf: "center", marginLeft: 6 }}>
                  {widgetImportNotice}
                </span>
              ) : null}
            </div>

            {tab === "type" ? (
              <div className="dashboard-type-groups">
                {TYPE_GROUPS.map((group) => {
                  // Operator 2026-06-23: hide license-locked widgets in
                  // the picker. The same registry still ships every
                  // widget so existing dashboards render correctly; we
                  // just don't offer them in the New Widget menu when
                  // the module is off.
                  const groupWidgets = WIDGET_TYPES.filter((t) => t.group === group).filter((t) => {
                    if (!t.licenseModule) return true;
                    if (typeof isLicenseModuleEnabled !== "function") return true;
                    return Boolean(isLicenseModuleEnabled(t.licenseModule));
                  });
                  if (!groupWidgets.length) return null;
                  return (
                  <div key={group} className="dashboard-type-group">
                    <div className="dashboard-type-group-title">{group}</div>
                    <div className="dashboard-type-grid">
                      {groupWidgets.map((t) => (
                        <button
                          key={t.key}
                          type="button"
                          className={`dashboard-type-btn ${form.type === t.key ? "active" : ""}`}
                          onClick={() => {
                            setForm((prev) => {
                              // Operator 2026-06-16: if the title is
                              // still the previous widget type's
                              // default label, swap it for the new
                              // type's label so the operator doesn't
                              // see "Line Chart" stuck after picking
                              // Value KPI. Custom titles are kept.
                              const prevDefault = getWidgetMeta(prev.type)?.label || "";
                              const titleIsDefault = !prev.title || prev.title === prevDefault;
                              return {
                                ...prev,
                                type: t.key,
                                w: t.defaultSize.w,
                                h: t.defaultSize.h,
                                title: titleIsDefault ? t.label : prev.title,
                              };
                            });
                            setTab("config");
                          }}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  );
                })}
              </div>
            ) : (
              <div className="form-grid dashboard-form-grid">
                <label>
                  Title
                  <input value={form.title} onChange={(e) => setForm((p) => ({ ...p, title: e.target.value }))} />
                </label>
                {/* Universal card-display controls — apply to every widget
                    type (charts, KPIs, dividers, fixed text, tables).
                    Lets the operator strip the card chrome so the body
                    uses the full footprint, which is what an industrial
                    HMI usually wants. */}
                {/* Slide toggle: <div> wrapper instead of <label> so the
                    global `label { display: grid }` rule doesn't fight
                    our flex layout. Click flips the underlying hidden
                    checkbox. */}
                <div
                  className="dashboard-slide-toggle dashboard-full-row"
                  onClick={() => setForm((p) => ({
                    ...p,
                    config: { ...p.config, hide_widget_header: !Boolean(p.config?.hide_widget_header) },
                  }))}
                  role="switch"
                  aria-checked={Boolean(form.config?.hide_widget_header)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === " " || e.key === "Enter") {
                      e.preventDefault();
                      setForm((p) => ({
                        ...p,
                        config: { ...p.config, hide_widget_header: !Boolean(p.config?.hide_widget_header) },
                      }));
                    }
                  }}
                >
                  <input
                    type="checkbox"
                    checked={Boolean(form.config?.hide_widget_header)}
                    onChange={() => {}}
                    tabIndex={-1}
                    aria-hidden="true"
                  />
                  <span className="slide-track" aria-hidden="true" />
                  <span className="slide-label">Hide widget title bar</span>
                </div>

                {/* Operator 2026-06-16: pick which pieces show in the
                    title bar separated by "|". Available pieces:
                      • value — current live value (with unit suffix)
                      • tag   — PLC tag name
                      • title — operator-edited widget title
                    Default (no boxes ticked is treated as legacy
                    behaviour = all three). */}
                {!Boolean(form.config?.hide_widget_header) ? (
                  <fieldset className="dashboard-full-row" style={{ border: "1px solid var(--stroke, rgba(255,255,255,0.1))", borderRadius: 6, padding: "8px 10px", margin: 0 }}>
                    <legend style={{ fontSize: 11, color: "var(--muted)", padding: "0 6px" }}>Title bar — show</legend>
                    {(() => {
                      const stored = Array.isArray(form.config?.header_parts) ? form.config.header_parts : ["value", "tag", "title"];
                      const has = (k) => stored.includes(k);
                      const toggle = (k) => setForm((p) => {
                        const cur = Array.isArray(p.config?.header_parts) ? p.config.header_parts : ["value", "tag", "title"];
                        const next = cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k];
                        return { ...p, config: { ...p.config, header_parts: next } };
                      });
                      const cell = (k, label) => (
                        <label className="pwr-check" style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 16, fontSize: 12 }}>
                          <input type="checkbox" checked={has(k)} onChange={() => toggle(k)} />
                          <span>{label}</span>
                        </label>
                      );
                      return (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                          {cell("value", "Live value")}
                          {cell("tag", "Tag name")}
                          {cell("title", "Widget title")}
                        </div>
                      );
                    })()}
                  </fieldset>
                ) : null}

                {/* Body text scale used by dividers / fixed_text / table_list
                    captions / KPI labels. Range 0.6..2.5 covers the usual
                    "shrink to fit a card" and "make this label readable
                    from across the room" cases. */}
                {["fixed_text", "divider", "table_list", "value_kpi", "text_kpi"].includes(form.type) ? (
                  <label>
                    Body text size
                    <input
                      type="number"
                      min="0.6"
                      max="2.5"
                      step="0.1"
                      value={Number(form.config?.body_text_scale || 1).toFixed(1)}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, body_text_scale: clamp(e.target.value, 0.6, 2.5) },
                        }))
                      }
                    />
                  </label>
                ) : null}
                {["line_chart", "line_area_chart", "stacked_trend"].includes(form.type) ? (
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
                {/* Operator 2026-06-20: gap visibility toggle. When ON
                    (default), every period where the gateway wasn't
                    collecting renders as a visible break in the line —
                    the operator can see "data is missing here". When OFF,
                    the chart bridges across the gap with a straight line,
                    showing only the actual collected samples connected
                    chronologically. Useful for batch processes where the
                    operator only cares about "when collection was on". */}
                {["line_chart", "line_area_chart", "stacked_trend"].includes(form.type) ? (
                  <label className="row" style={{ alignItems: "center", gap: 12, justifyContent: "space-between" }}>
                    <span>Show Disconnected Periods</span>
                    <span
                      role="switch"
                      aria-checked={form.config.show_gaps !== false}
                      onClick={() => setForm((p) => ({
                        ...p,
                        config: { ...p.config, show_gaps: !(p.config?.show_gaps !== false) ? true : false },
                      }))}
                      style={{
                        cursor: "pointer",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 8,
                        userSelect: "none",
                      }}
                    >
                      <span
                        style={{
                          position: "relative",
                          width: 38,
                          height: 20,
                          borderRadius: 10,
                          background: form.config.show_gaps !== false ? "var(--accent, #14a89a)" : "var(--border, #555)",
                          transition: "background 0.15s",
                        }}
                      >
                        <span
                          style={{
                            position: "absolute",
                            top: 2,
                            left: form.config.show_gaps !== false ? 20 : 2,
                            width: 16,
                            height: 16,
                            borderRadius: "50%",
                            background: "#fff",
                            transition: "left 0.15s",
                          }}
                        />
                      </span>
                      <span style={{ fontSize: 12 }}>
                        {form.config.show_gaps !== false ? "Show gaps" : "Hide gaps"}
                      </span>
                    </span>
                  </label>
                ) : null}
                {/* Width / Height inputs removed: the widget is resized
                    by dragging the corner handle directly on the grid,
                    so a redundant numeric input cluttered the dialog
                    without providing extra capability. The grid state
                    (form.w / form.h) is still persisted on save. */}
                {form.type === "bar_chart" ? (
                  <>
                    {/* 2026-07-26: Grafana-style bar-gauge mode — one bar per
                        tag showing a single reduced value over the window. */}
                    <label>
                      Bar Mode
                      <select
                        value={String(form.config.bar_mode || "timeseries")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, bar_mode: e.target.value } }))}
                      >
                        <option value="timeseries">Time series (history bars)</option>
                        <option value="latest_per_tag">Latest per tag (one bar per tag)</option>
                      </select>
                    </label>
                    {String(form.config.bar_mode || "timeseries") === "latest_per_tag" ? (
                      <label>
                        Calculation
                        <select
                          value={String(form.config.bar_calc || "last")}
                          onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, bar_calc: e.target.value } }))}
                        >
                          <option value="last">Last value</option>
                          <option value="min">Minimum</option>
                          <option value="max">Maximum</option>
                          <option value="avg">Average</option>
                          <option value="sum">Sum</option>
                        </select>
                      </label>
                    ) : null}
                  </>
                ) : null}
                {form.type === "table_list" ? (
                  <>
                    <label>
                      Table Mode
                      <select
                        value={String(form.config.table_mode || "rows")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, table_mode: e.target.value } }))}
                      >
                        <option value="rows">Historian rows (default)</option>
                        <option value="latest_per_tag">Latest per tag (one row per tag)</option>
                      </select>
                    </label>
                    {String(form.config.table_mode || "rows") === "latest_per_tag" ? (
                      <label>
                        Calculation
                        <select
                          value={String(form.config.table_calc || "last")}
                          onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, table_calc: e.target.value } }))}
                        >
                          <option value="last">Last value</option>
                          <option value="min">Minimum</option>
                          <option value="max">Maximum</option>
                          <option value="avg">Average</option>
                          <option value="sum">Sum</option>
                        </select>
                      </label>
                    ) : null}
                  </>
                ) : null}
                {["line_chart", "line_area_chart", "bar_chart", "stacked_trend", "meter_chart", "text_kpi", "value_kpi", "pie_chart", "table_list", "energy_tariffs"].includes(form.type) ? (
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
                          onChange={(e) => setForm((p) => {
                            const tag = e.target.value;
                            // Operator 2026-06-16: when the title is
                            // still the widget-type default (or
                            // empty), adopt the tag name so the new
                            // widget gets an informative label
                            // instead of "Value KPI". Custom titles
                            // are preserved.
                            const typeDefault = getWidgetMeta(p.type)?.label || "";
                            const titleIsDefault = !p.title || p.title === typeDefault;
                            const nextTitle = (tag && titleIsDefault)
                              ? (formatTagForDisplay ? formatTagForDisplay(tag) : tag)
                              : p.title;
                            return {
                              ...p,
                              title: nextTitle,
                              config: { ...p.config, tag_name: tag },
                            };
                          })}
                        >
                          <option value="">Select tag</option>
                          {selectedGatewayTags.map((tag) => {
                            // Mark text tags so the operator can see, at a
                            // glance, which ones a numeric widget can't plot.
                            const k = classifyTag(form.config?.gateway_id, tag, tagRowsForTypes);
                            const suffix = k === TAG_KIND.TEXT ? "  · TEXT"
                              : k === TAG_KIND.NUMERIC_TEXT ? "  · TEXT (numeric)"
                              : "";
                            return (
                              <option key={tag} value={tag}>
                                {(formatTagForDisplay ? formatTagForDisplay(tag) : tag) + suffix}
                              </option>
                            );
                          })}
                        </select>
                        {/* Interlock notice. Never blocks an unknown tag —
                            only a tag we are confident carries text. */}
                        {(() => {
                          const v = checkTagForWidget(form.type, form.config?.gateway_id, form.config?.tag_name, tagRowsForTypes);
                          if (v.severity === "none") return null;
                          const isBlock = v.severity === "block";
                          return (
                            <div style={{
                              marginTop: 6, padding: "6px 9px", borderRadius: 6, fontSize: 12,
                              border: `1px solid ${isBlock ? "var(--danger, #d9534f)" : "var(--warning, #d99a00)"}`,
                              color: isBlock ? "var(--danger, #d9534f)" : "var(--warning, #d99a00)",
                            }}>
                              {isBlock ? "⚠ " : "ℹ "}{v.message}
                              {v.suggest ? <div style={{ opacity: 0.85, marginTop: 2 }}>{v.suggest}</div> : null}
                            </div>
                          );
                        })()}
                      </label>
                    ) : null}
                  </>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? (
                  <label>
                    Reading points
                    <input
                      type="number"
                      min="5"
                      max="5000"
                      value={form.config.readings_count}
                      onBlur={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, readings_count: clamp(e.target.value, 5, 5000) },
                        }))
                      }
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: {
                            ...p.config,
                            // Free typing: keep the raw text while editing; the
                            // clamp runs on blur and again at save. Clamping per
                            // keystroke made "120" impossible to type (the "1"
                            // snapped to the minimum before the user finished).
                            readings_count: e.target.value,
                            // Reset any saved override so the new value
                            // applies to extras too. Operator wants ONE
                            // knob to control the chart's depth.
                            series_readings_count: 0,
                          },
                        }))
                      }
                    />
                  </label>
                ) : null}

                {/* Series reading points was a separate field on older
                    builds. Operator request 2026-06-11: "we should have
                    a interlock to have one one number of the reading
                    field doesn't matter if multi or single". The single
                    "Reading points" above now drives every series in the
                    widget; we keep the saved series_readings_count for
                    backward compat (the runtime still respects an
                    explicit non-zero value) but no longer expose the
                    second input. */}

                {/* Y-axis scale toggle — operator-requested 2026-06-11:
                    a slide toggle in the main Configure tab (similar to
                    the Lite view's auto-fit toggle) so the most common
                    axis decision is one click instead of opening the
                    Series, Axes & Data Query inner modal.
                    UI structure:
                      Row 1: [checkbox] Manual Y axis scale  (one row)
                      Row 2: Left axis  | min | max | tick step
                      Row 3: Right axis | min | max | tick step (only
                              when a right axis series exists). */}
                {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? (() => {
                  const manualOn = String(form.config?.y_axis_mode || "auto").toLowerCase() === "manual";
                  const hasRightAxis = Array.isArray(form.config?.series_extra)
                    && form.config.series_extra.some((s) => String(s?.axis || "left").toLowerCase() === "right");
                  const numField = (key, placeholder) => (
                    <input
                      type="number"
                      step="any"
                      placeholder={placeholder}
                      value={form.config?.[key] ?? ""}
                      onChange={(e) => setForm((p) => ({
                        ...p,
                        config: {
                          ...p.config,
                          [key]: e.target.value === "" ? "" : Number(e.target.value),
                        },
                      }))}
                      style={{ flex: 1, minWidth: 0 }}
                    />
                  );
                  const axisRow = (label, kMin, kMax, kStep) => (
                    <div className="dashboard-axis-row" style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      marginTop: 6,
                    }}>
                      <span className="muted" style={{ flex: "0 0 100px", fontSize: 12, fontWeight: 600 }}>{label}</span>
                      {numField(kMin, "min")}
                      {numField(kMax, "max")}
                      {numField(kStep, "tick step")}
                    </div>
                  );
                  const toggleManualY = () => {
                    setForm((p) => {
                      const now = String(p.config?.y_axis_mode || "auto").toLowerCase() === "manual";
                      const next = !now;
                      return {
                        ...p,
                        config: {
                          ...p.config,
                          y_axis_mode: next ? "manual" : "auto",
                          y_right_axis_mode: next ? "manual" : "auto",
                        },
                      };
                    });
                  };
                  return (
                    <div className="dashboard-full-row">
                      <div
                        className="dashboard-slide-toggle"
                        onClick={toggleManualY}
                        role="switch"
                        aria-checked={manualOn}
                        tabIndex={0}
                        onKeyDown={(e) => {
                          if (e.key === " " || e.key === "Enter") {
                            e.preventDefault();
                            toggleManualY();
                          }
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={manualOn}
                          onChange={() => {}}
                          tabIndex={-1}
                          aria-hidden="true"
                        />
                        <span className="slide-track" aria-hidden="true" />
                        <span className="slide-label">Manual Y axis scale</span>
                      </div>
                      {manualOn ? (
                        <>
                          {axisRow("Left axis", "y_min", "y_max", "y_tick_step")}
                          {hasRightAxis ? axisRow("Right axis", "y_right_min", "y_right_max", "y_right_tick_step") : null}
                        </>
                      ) : null}
                    </div>
                  );
                })() : null}

                {["line_chart", "line_area_chart", "bar_chart", "stacked_trend", "pie_chart", "meter_chart"].includes(form.type) ? (
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
                {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? (
                  <div className="dashboard-full-row">
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      onClick={() => setQueryModalOpen(true)}
                      title="Plot multiple tags on the same chart with their own units and axes"
                    >
                      Series & Axes (multi-tag, dual axis, units)
                    </button>
                    {/* Axis settings are reachable WITHOUT opening the series
                        modal — the common single-series case. */}
                    <button
                      type="button"
                      className="dashboard-type-btn"
                      style={{ marginTop: 6 }}
                      onClick={() => setAxisModalOpen(true)}
                      title="Label, prefix, unit, suffix, decimals, data format and scale for each Y axis in use"
                    >
                      Axis configuration (units, scale, decimals)
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
                      value={form.config.text_font_scale ?? 1}
                      onBlur={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, text_font_scale: clamp(e.target.value, 0.7, 2.5) },
                        }))
                      }
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          // Raw while typing (see readings_count note); clamped
                          // on blur + save.
                          config: { ...p.config, text_font_scale: e.target.value },
                        }))
                      }
                    />
                  </label>
                ) : null}

                {/* Operator 2026-06-16: optional unit suffix appended
                    after the KPI's value (e.g. "A", "W", "kWh", "%"). */}
                {["text_kpi", "value_kpi"].includes(form.type) ? (
                  <label>
                    Unit suffix
                    <input
                      type="text"
                      placeholder="e.g. A, W, kWh, %"
                      value={String(form.config.unit_suffix || "")}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, unit_suffix: e.target.value } }))}
                    />
                  </label>
                ) : null}
                {/* Operator 2026-06-16: decimal places for the
                    numeric KPI value (0..6). Applies to both the body
                    value and the live value in the title strip. */}
                {form.type === "value_kpi" ? (
                  <label>
                    Decimals
                    <input
                      type="number"
                      min="0"
                      max="6"
                      step="1"
                      value={Number.isFinite(Number(form.config.value_decimals)) ? Number(form.config.value_decimals) : 3}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, value_decimals: clamp(e.target.value, 0, 6) } }))}
                    />
                  </label>
                ) : null}

                {/* Operator 2026-06-16: per-widget text colours.
                    "Value color" recolours the big number / tag text
                    in the body; "Unit color" recolours just the unit
                    suffix span. Applies to text/value KPIs and
                    fixed_text widgets. Leave blank for theme default. */}
                {["text_kpi", "value_kpi", "fixed_text"].includes(form.type) ? (
                  <>
                    <label>
                      Value color
                      <input
                        type="color"
                        value={String(form.config.value_color || "#14a89a")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, value_color: e.target.value } }))}
                      />
                    </label>
                    {["text_kpi", "value_kpi"].includes(form.type) ? (
                      <>
                        <label>
                          Unit color
                          <input
                            type="color"
                            value={String(form.config.unit_color || "#94a3b8")}
                            onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, unit_color: e.target.value } }))}
                          />
                        </label>
                        {/* Operator 2026-06-16: unit-suffix font size
                            independent of the value size. Expressed as
                            a multiplier of the value font (0.3–2.0); 1
                            matches the value. Default 1 — same size
                            inline as the user originally requested. */}
                        <label>
                          Unit text size
                          <input
                            type="number"
                            min="0.3"
                            max="2"
                            step="0.05"
                            value={Number.isFinite(Number(form.config.unit_size_scale)) ? Number(form.config.unit_size_scale) : 1}
                            onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, unit_size_scale: clamp(e.target.value, 0.3, 2) } }))}
                          />
                        </label>
                      </>
                    ) : null}
                  </>
                ) : null}

                {/* Operator 2026-06-16: Energy Tariffs widget — pick
                    a render mode and what to plot (€ cost or kWh). */}
                {form.type === "energy_tariffs" ? (
                  <>
                    <label>
                      View
                      <select
                        value={String(form.config.display_mode || "donut")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, display_mode: e.target.value } }))}
                      >
                        <option value="donut">Donut chart</option>
                        <option value="bars">Horizontal bars</option>
                        <option value="table">Table</option>
                      </select>
                    </label>
                    <label>
                      Show
                      <select
                        value={String(form.config.tariff_value_mode || "cost")}
                        onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, tariff_value_mode: e.target.value } }))}
                      >
                        <option value="cost">€ Cost</option>
                        <option value="kwh">kWh</option>
                      </select>
                    </label>
                  </>
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

                {form.type === "report_card" ? (
                  <ReportCardEditor
                    config={form.config || {}}
                    onChange={(patch) => setForm((p) => ({ ...p, config: { ...p.config, ...patch } }))}
                  />
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
                {/* Operator 2026-06-17: create a new empty profile.
                    Asks for a name, then clears the canvas to zero
                    widgets and persists the empty profile so the
                    operator can start building from scratch instead
                    of having to delete every widget from a loaded
                    profile first. */}
                <button
                  type="button"
                  className="dashboard-profile-btn"
                  onClick={async () => {
                    const rawName = await askProfileName("");
                    const name = String(rawName || "").trim();
                    if (!name) return;
                    if (profiles.some((p) => p.name === name)) {
                      try {
                        if (!window.confirm(`Overwrite existing profile "${name}"?`)) return;
                      } catch (_) {}
                    }
                    // Replace canvas with an empty payload so the
                    // operator sees a blank slate immediately.
                    applyProfilePayload({ widgets: [], mode: dashboardMode, per_row: dashboardPerRow, tag_colors: {} });
                    const payload = { name, widgets: [], mode: dashboardMode, per_row: dashboardPerRow, tag_colors: {} };
                    const next = profiles.some((p) => p.name === name)
                      ? profiles.map((p) => (p.name === name ? payload : p))
                      : [...profiles, payload];
                    persistProfiles(next);
                    setActiveProfileName(name);
                  }}
                  disabled={!canEdit}
                  title="Create a new empty dashboard profile from scratch"
                >
                  New empty…
                </button>
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
                {/* Power-default preset (operator 2026-06-15). Loads a
                    6-stat + 2-chart layout aimed at one power meter.
                    Doesn't write to localStorage until the user
                    presses Save / Save as. */}
                <button
                  type="button"
                  className="dashboard-profile-btn"
                  onClick={applyPowerDefaultProfile}
                  disabled={!canEdit || !(Array.isArray(gatewayCatalog) && gatewayCatalog.some((g) => g?.power_meter))}
                  title="Replace the current layout with the Power Overview preset"
                >
                  Apply Power Default
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

      {/* Axis configuration — one card per axis IN USE. Opens from the main
          Configure page (single-series) and from Series & Axes (multi-series). */}
      {axisModalOpen && form ? (
        <AxisConfigModal
          config={form.config || {}}
          widgetType={form.type}
          widgetValueFormat={form.config?.chart_value_format || "auto"}
          onChange={(patch) => setForm((p) => ({ ...p, config: { ...p.config, ...patch } }))}
          onClose={() => setAxisModalOpen(false)}
        />
      ) : null}

      {queryModalOpen ? (
        <div className="modal-backdrop">
          <div className={`modal-card dashboard-widget-modal dashboard-query-modal dashboard-query-modal-wide ${["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? "dashboard-series-modal-wide" : ""}`}>
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
                      value={form.config.query_result_aggregation
                        || (["line_chart", "line_area_chart", "bar_chart", "value_kpi", "meter_chart"].includes(String(form?.type || ""))
                            ? "last"
                            : "count")}
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
              {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? (
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
                    <label className="dashboard-query-field">
                      <span>X-axis time format</span>
                      <select
                        value={String(form.config.chart_x_time_format || "hh_mm_ss")}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: { ...p.config, chart_x_time_format: e.target.value },
                          }))
                        }
                      >
                        <option value="hh_mm">HH:MM (24 h)</option>
                        <option value="hh_mm_ss">HH:MM:SS (24 h)</option>
                        <option value="hh_mm_12h">hh:mm AM/PM</option>
                        <option value="date_hh_mm">MM/DD HH:MM</option>
                        <option value="date_hh_mm_ss">MM/DD HH:MM:SS</option>
                        <option value="full_date_hh_mm">YYYY-MM-DD HH:MM</option>
                      </select>
                    </label>
                    <label className="dashboard-query-field">
                      <span>X-axis tick angle</span>
                      <select
                        value={String(form.config.chart_x_tick_angle ?? 0)}
                        onChange={(e) =>
                          setForm((p) => ({
                            ...p,
                            config: { ...p.config, chart_x_tick_angle: Number(e.target.value) },
                          }))
                        }
                      >
                        <option value="0">Horizontal (0°)</option>
                        <option value="-30">Tilted -30°</option>
                        <option value="-45">Tilted -45°</option>
                        <option value="-60">Tilted -60°</option>
                        <option value="-90">Vertical -90°</option>
                        <option value="45">Tilted 45°</option>
                        <option value="90">Vertical 90°</option>
                      </select>
                    </label>
                  </div>
                  <div className="dashboard-query-checkboxes">
                    {[
                      { key: "chart_show_legend", label: "Show legend" },
                      { key: "chart_show_point_labels", label: "Show point labels" },
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
              {["line_chart", "line_area_chart", "bar_chart", "stacked_trend"].includes(form.type) ? (
                <fieldset className="dashboard-query-fieldset">
                  <legend>Series & axes</legend>
                  <p className="dashboard-query-hint">
                    Plot multiple tags on the same chart. Each series can have its own unit, axis (left / right), chart style and color.
                  </p>
                  <div className="dashboard-query-grid">
                    <label className="dashboard-query-field" style={{ gridColumn: "1 / -1" }}>
                      <span>Axes</span>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => setAxisModalOpen(true)}
                        title="Configure every Y axis in use: label, prefix, unit, suffix, decimals, data format and scale"
                      >
                        Axis configuration…
                      </button>
                    </label>
                  </div>
                  {/* Per-widget styling: line thickness, dot marker, bar fill / width.
                      Placed inside the Series & Axes fieldset (instead of the
                      separate Chart Display section above) so operators see
                      it without scrolling up through the modal. */}
                  {["line_chart", "line_area_chart", "stacked_trend"].includes(form.type) ? (
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
                            >
                              <option value="">(same as primary)</option>
                              {gatewayOptions.map((g) => (
                                <option key={g.id} value={g.id}>{g.name || g.id}</option>
                              ))}
                            </select>
                            {isLimit ? (
                              // Limit lines support two modes:
                              //   1. Constant — operator types a number here.
                              //   2. Follow tag — picks a tag below; runtime
                              //      reads its most recent value every poll
                              //      cycle so the limit "moves" with the
                              //      live PLC value (e.g. a set-point from
                              //      another tag).
                              // When the operator selects a tag we clear
                              // limit_value, and vice-versa. Both inputs
                              // are visible so the choice is obvious.
                              <div className="dashboard-limit-source">
                                <input
                                  type="number"
                                  step="any"
                                  value={row.limit_value ?? ""}
                                  placeholder="constant value"
                                  onChange={(e) => update({ limit_value: e.target.value, tag_name: "" })}
                                  title="Constant threshold drawn as a horizontal line"
                                  disabled={!!row.tag_name}
                                />
                                <span className="dashboard-limit-or">or</span>
                                <select
                                  value={row.tag_name || ""}
                                  onChange={(e) => update({ tag_name: e.target.value, limit_value: "" })}
                                  title="Follow a tag (limit moves with the latest tag value)"
                                  disabled={String(row.limit_value || "").trim() !== ""}
                                >
                                  <option value="">(follow tag)</option>
                                  {allowedTags.map((tag) => (
                                    <option key={tag} value={tag}>
                                      {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                                    </option>
                                  ))}
                                </select>
                              </div>
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
                              value={(() => { const a = String(row.axis || "left").toLowerCase(); return a === "left" ? "left1" : a === "right" ? "right1" : (["left1","left2","right1","right2"].includes(a) ? a : "left1"); })()}
                              onChange={(e) => update({ axis: e.target.value })}
                              title="Axis (up to 2 left + 2 right)"
                            >
                              <option value="left1">Left 1</option>
                              <option value="left2">Left 2</option>
                              <option value="right1">Right 1</option>
                              <option value="right2">Right 2</option>
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
