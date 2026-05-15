import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  deleteReportTemplate,
  downloadGeneratedReportBlob,
  exportAllReportTemplates,
  exportReportTemplate,
  exportSectionCsv,
  exportSectionTxt,
  getAppStoreHistorianRange,
  getGeneratedReportFileUrl,
  importReportTemplates,
  listReportTemplates,
  renderReportPreview,
  saveReportTemplate,
} from "../../api";

// ---------------------------------------------------------------------------
// section presets / option lists
// ---------------------------------------------------------------------------
const SECTION_PRESETS = [
  { type: "header", label: "Header", description: "Title + subtitle banner" },
  { type: "text", label: "Text block", description: "Plain text paragraph" },
  { type: "kpi_grid", label: "KPI grid", description: "Aggregated values per tag" },
  { type: "line_chart", label: "Line chart", description: "Tag values over time (multi-tag)" },
  { type: "area_chart", label: "Area chart", description: "Filled trend line" },
  { type: "bar_chart", label: "Bar chart", description: "Tag samples as bars" },
  { type: "pie_chart", label: "Pie chart", description: "Distribution (direct or computed rules)" },
  { type: "table", label: "Data table", description: "Historian rows with custom columns" },
  { type: "spacer", label: "Spacer", description: "Vertical gap" },
  { type: "page_break", label: "Page break", description: "Force a new page" },
];

const TIME_PRESETS = [
  { value: "none", label: "All available" },
  { value: "5m", label: "Last 5 minutes" },
  { value: "15m", label: "Last 15 minutes" },
  { value: "1h", label: "Last 1 hour" },
  { value: "6h", label: "Last 6 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom range" },
];

const KPI_AGGREGATIONS = [
  { value: "count", label: "Count" },
  { value: "sum", label: "Sum" },
  { value: "avg", label: "Average" },
  { value: "min", label: "Min" },
  { value: "max", label: "Max" },
  { value: "latest", label: "Latest" },
];

const RULE_OPERATORS = [
  { value: "any", label: "Any" },
  { value: "gt", label: ">" },
  { value: "gte", label: ">=" },
  { value: "lt", label: "<" },
  { value: "lte", label: "<=" },
  { value: "eq", label: "=" },
  { value: "ne", label: "!=" },
  { value: "between", label: "Between" },
];

const VALUE_FORMATS = [
  { value: "auto", label: "Auto" },
  { value: "int", label: "Integer" },
  { value: "2dp", label: "2 decimals" },
  { value: "3dp", label: "3 decimals" },
  { value: "scientific", label: "Scientific" },
];

const SERIES_CHART_KINDS = [
  { value: "", label: "Inherit section" },
  { value: "line", label: "Line" },
  { value: "area", label: "Area" },
  { value: "bar", label: "Bar" },
];

// Section-level time-bucket sizes for chart aggregation. "raw" keeps every
// sample (current behaviour). Anything else groups samples into fixed
// epoch-aligned buckets and applies each series' aggregation.
const BUCKET_SIZES = [
  { value: "raw", label: "Raw samples" },
  { value: "1m",  label: "1 minute" },
  { value: "5m",  label: "5 minutes" },
  { value: "15m", label: "15 minutes" },
  { value: "30m", label: "30 minutes" },
  { value: "1h",  label: "1 hour" },
  { value: "4h",  label: "4 hours" },
  { value: "12h", label: "12 hours" },
  { value: "1d",  label: "1 day" },
];

// Per-series aggregation choices. Empty string = no aggregation (each raw
// sample plotted as today). Names match the backend's _reduce_bucket() map.
const SERIES_AGGREGATIONS = [
  { value: "",       label: "None (plot raw)" },
  { value: "avg",    label: "Average" },
  { value: "min",    label: "Minimum" },
  { value: "max",    label: "Maximum" },
  { value: "last",   label: "Last" },
  { value: "sum",    label: "Sum" },
  { value: "count",  label: "Count" },
  { value: "median", label: "Median" },
];

// Value-predicate operators, matching the legacy reporting + the backend's
// _passes_value_filter() switch.
const SERIES_FILTER_OPS = [
  { value: "any",     label: "Any value" },
  { value: "eq",      label: "Equals (=)" },
  { value: "ne",      label: "Not equal (≠)" },
  { value: "gt",      label: "Greater than (>)" },
  { value: "gte",     label: "≥ (greater or equal)" },
  { value: "lt",      label: "Less than (<)" },
  { value: "lte",     label: "≤ (less or equal)" },
  { value: "between", label: "Between (inclusive)" },
];

const COLUMN_KINDS = [
  { value: "ts", label: "Timestamp" },
  { value: "tag", label: "Tag name" },
  { value: "gateway", label: "Gateway" },
  { value: "quality", label: "Quality" },
  { value: "value", label: "Series value" },
  { value: "calc", label: "Calculation" },
];

const DEFAULT_PALETTE = [
  "#14a89a", "#0e8479", "#3cd2c2", "#1f3a5f",
  "#6e8dd2", "#e0a050", "#e2585d", "#a78bfa",
];

function makeId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function pickColor(idx) {
  return DEFAULT_PALETTE[idx % DEFAULT_PALETTE.length];
}

// ---------------------------------------------------------------------------
// time-range helpers (mirror backend semantics)
// ---------------------------------------------------------------------------
const TIME_PRESET_SECONDS = {
  "5m": 5 * 60, "15m": 15 * 60, "1h": 3600, "6h": 6 * 3600,
  "24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600,
};
function toUtcFilterText(preset, fromUtc, toUtc) {
  if (!preset || preset === "none") return { from: "", to: "" };
  if (preset === "custom") return { from: String(fromUtc || ""), to: String(toUtc || "") };
  const seconds = TIME_PRESET_SECONDS[preset];
  if (!seconds) return { from: "", to: "" };
  const now = new Date();
  const start = new Date(now.getTime() - seconds * 1000);
  const fmt = (d) =>
    d.toISOString().replace("T", " ").slice(0, 19);
  return { from: fmt(start), to: fmt(now) };
}

// ---------------------------------------------------------------------------
// default section factory
// ---------------------------------------------------------------------------
// Preset starter templates so users can begin from a sensible skeleton
// instead of an empty draft. They map gateway_id / tag_name to "" so the
// operator can pick concrete tags in the section editor before saving.
function makePresetTemplate(presetKey) {
  switch (presetKey) {
    case "daily_summary":
      return {
        name: "Daily production summary",
        description: "Header + KPI grid + 24h trend + recent samples table",
        sections: [
          { ...defaultSection("header"), title: "Daily Production Summary", subtitle: "Last 24 hours" },
          {
            ...defaultSection("kpi_grid"),
            title: "Key indicators",
            columns: 4,
            items: [
              { id: makeId("kpi"), label: "Total count", gateway_id: "", tag_name: "", aggregation: "sum", operator: "any" },
              { id: makeId("kpi"), label: "Average", gateway_id: "", tag_name: "", aggregation: "avg", operator: "any" },
              { id: makeId("kpi"), label: "Peak", gateway_id: "", tag_name: "", aggregation: "max", operator: "any" },
              { id: makeId("kpi"), label: "Min", gateway_id: "", tag_name: "", aggregation: "min", operator: "any" },
            ],
          },
          { ...defaultSection("line_chart"), title: "24h trend", time_range: { preset: "24h" } },
          { ...defaultSection("table"), title: "Recent samples", time_range: { preset: "1h" }, row_limit: 50 },
        ],
      };
    case "energy_consumption":
      return {
        name: "Energy consumption",
        description: "Energy/power overview with dual-axis chart and totals",
        sections: [
          { ...defaultSection("header"), title: "Energy Consumption Report", subtitle: "Power / Current / Voltage" },
          {
            ...defaultSection("kpi_grid"),
            title: "Totals",
            columns: 3,
            items: [
              { id: makeId("kpi"), label: "kWh consumed", gateway_id: "", tag_name: "", aggregation: "sum", operator: "any" },
              { id: makeId("kpi"), label: "Avg load (kW)", gateway_id: "", tag_name: "", aggregation: "avg", operator: "any" },
              { id: makeId("kpi"), label: "Peak load (kW)", gateway_id: "", tag_name: "", aggregation: "max", operator: "any" },
            ],
          },
          {
            ...defaultSection("line_chart"),
            title: "Power vs current (dual axis)",
            time_range: { preset: "24h" },
            y_axis_label: "Power",
            y_axis_unit: "kW",
            y_axis_right_label: "Current",
            y_axis_right_unit: "A",
            series: [
              { id: makeId("ser"), label: "Power", gateway_id: "", tag_name: "", color: "#3b82f6", axis: "left", chart_type: "line", unit: "kW", multiplier: 1, offset: 0 },
              { id: makeId("ser"), label: "Current", gateway_id: "", tag_name: "", color: "#f97316", axis: "right", chart_type: "line", unit: "A", multiplier: 1, offset: 0 },
            ],
          },
        ],
      };
    case "alarm_overview":
      return {
        name: "Alarm overview",
        description: "Pie distribution + recent triggered alarms table",
        sections: [
          { ...defaultSection("header"), title: "Alarm Overview", subtitle: "Last 24 hours" },
          {
            ...defaultSection("pie_chart"),
            title: "Alarm distribution",
            data_source_type: "computed",
            time_range: { preset: "24h" },
            compute_rules: [
              { id: makeId("rule"), label: "Critical", gateway_id: "", tag_name: "", operator: "eq", value1: 3, value2: "", aggregation: "count", color: "#dc2626" },
              { id: makeId("rule"), label: "Warning", gateway_id: "", tag_name: "", operator: "eq", value1: 2, value2: "", aggregation: "count", color: "#f59e0b" },
              { id: makeId("rule"), label: "Info", gateway_id: "", tag_name: "", operator: "eq", value1: 1, value2: "", aggregation: "count", color: "#10b981" },
            ],
          },
          { ...defaultSection("table"), title: "Triggered alarms", time_range: { preset: "24h" }, row_limit: 100 },
        ],
      };
    case "tag_audit":
      return {
        name: "Tag audit",
        description: "Compact text-led report focused on a single tag",
        sections: [
          { ...defaultSection("header"), title: "Tag Audit", subtitle: "Single-tag investigation" },
          { ...defaultSection("text"), title: "Notes", text: "Use this section to document why this audit was generated and any actions taken." },
          { ...defaultSection("line_chart"), title: "Tag trend (24h)", time_range: { preset: "24h" } },
          { ...defaultSection("table"), title: "Last 200 samples", time_range: { preset: "none" }, row_limit: 200 },
        ],
      };
    case "shift_summary":
      return {
        name: "Shift summary",
        description: "8h window with totals, trend, and samples",
        sections: [
          { ...defaultSection("header"), title: "Shift Summary", subtitle: "Last 8 hours" },
          {
            ...defaultSection("kpi_grid"),
            title: "Shift totals",
            columns: 4,
            items: [
              { id: makeId("kpi"), label: "Good parts", gateway_id: "", tag_name: "", aggregation: "sum", operator: "any" },
              { id: makeId("kpi"), label: "Reject parts", gateway_id: "", tag_name: "", aggregation: "sum", operator: "any" },
              { id: makeId("kpi"), label: "Avg cycle time", gateway_id: "", tag_name: "", aggregation: "avg", operator: "any" },
              { id: makeId("kpi"), label: "Downtime events", gateway_id: "", tag_name: "", aggregation: "count", operator: "eq" },
            ],
          },
          { ...defaultSection("bar_chart"), title: "Per-hour counts", time_range: { preset: "6h" } },
        ],
      };
    case "blank":
    default:
      return {
        name: "New report",
        description: "",
        sections: [defaultSection("header")],
      };
  }
}

const TEMPLATE_PRESETS = [
  { value: "blank", label: "Blank" },
  { value: "daily_summary", label: "Daily production summary" },
  { value: "energy_consumption", label: "Energy consumption" },
  { value: "alarm_overview", label: "Alarm overview" },
  { value: "tag_audit", label: "Tag audit (single tag)" },
  { value: "shift_summary", label: "Shift summary (8h)" },
];

function defaultSection(type) {
  switch (type) {
    case "header":
      return {
        id: makeId("sec"),
        type,
        title: "Report Title",
        subtitle: "Subtitle goes here",
        show_generated_at: true,
      };
    case "text":
      return { id: makeId("sec"), type, title: "", text: "" };
    case "kpi_grid":
      return {
        id: makeId("sec"),
        type,
        title: "Key Indicators",
        columns: 4,
        items: [
          { id: makeId("kpi"), label: "Metric 1", gateway_id: "", tag_name: "", aggregation: "avg", operator: "any" },
        ],
      };
    case "line_chart":
    case "area_chart":
    case "bar_chart":
      return {
        id: makeId("sec"),
        type,
        title:
          type === "bar_chart" ? "Bar chart" :
          type === "area_chart" ? "Area chart" : "Line chart",
        time_range: { preset: "24h" },
        readings_count: 200,
        show_legend: true,
        value_format: "auto",
        x_axis_label: "Time",
        y_axis_label: "Value",
        y_axis_unit: "",
        y_axis_right_label: "",
        y_axis_right_unit: "",
        y_min: "",
        y_max: "",
        y_right_min: "",
        y_right_max: "",
        series: [
          {
            id: makeId("ser"),
            label: "",
            gateway_id: "",
            tag_name: "",
            color: pickColor(0),
            axis: "left",
            chart_type: "",
            unit: "",
            multiplier: 1,
            offset: 0,
          },
        ],
        limit_lines: [],
      };
    case "pie_chart":
      return {
        id: makeId("sec"),
        type,
        title: "Distribution",
        data_source_type: "tag_direct",
        gateway_id: "",
        tag_name: "",
        time_range: { preset: "24h" },
        query_result_aggregation: "count",
        compute_rules: [],
      };
    case "table":
      return {
        id: makeId("sec"),
        type,
        title: "Recent samples",
        time_range: { preset: "1h" },
        row_limit: 30,
        readings_count: 200,
        series: [
          { id: makeId("ser"), label: "", gateway_id: "", tag_name: "", color: pickColor(0), unit: "", multiplier: 1, offset: 0 },
        ],
        columns: [],
      };
    case "spacer":
      return { id: makeId("sec"), type, height: 8 };
    case "page_break":
      return { id: makeId("sec"), type };
    default:
      return { id: makeId("sec"), type };
  }
}

// ---------------------------------------------------------------------------
// fetch utilities (delegated to the central api.js helpers for auth + retries)
// ---------------------------------------------------------------------------
async function fetchHistorianRange({ fromUtc, toUtc, gatewayId, tagName, limit }) {
  const data = await getAppStoreHistorianRange({
    fromUtc: fromUtc || "",
    toUtc: toUtc || "",
    limit: Math.max(20, Math.min(Number(limit) || 200, 5000)),
    offset: 0,
    gateway: gatewayId || "",
    tag: tagName || "",
    preferCloud: false,
    timeoutMs: 12000,
    maxAttempts: 2,
  });
  return Array.isArray(data?.rows) ? data.rows : [];
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

// ---------------------------------------------------------------------------
// CollapsibleCard
// ---------------------------------------------------------------------------
/**
 * Resizable horizontal split pane. The divider is keyboard-accessible and
 * uses a CSS variable on the wrapper to position the left/right widths so
 * we don't fight React render cycles during the mouse drag.
 *
 * - `storageKey` persists the user's preferred split %.
 * - `minLeft` / `minRight` are minimum widths in px so neither side collapses
 *   to zero.
 */
function SplitPane({ storageKey, defaultLeftPct = 40, minLeft = 280, minRight = 320, left, right }) {
  const wrapRef = React.useRef(null);
  const [leftPct, setLeftPct] = React.useState(() => {
    if (storageKey) {
      try {
        const saved = window.localStorage.getItem(storageKey);
        const n = saved ? Number(saved) : NaN;
        if (Number.isFinite(n) && n >= 10 && n <= 90) return n;
      } catch (_) { /* noop */ }
    }
    return defaultLeftPct;
  });
  const draggingRef = React.useRef(false);

  React.useEffect(() => {
    if (!storageKey) return;
    try { window.localStorage.setItem(storageKey, String(leftPct)); } catch (_) { /* noop */ }
  }, [leftPct, storageKey]);

  const onDragMove = React.useCallback((clientX) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width <= 0) return;
    let next = ((clientX - rect.left) / rect.width) * 100;
    // Enforce min widths in px (translated back to %).
    const minLeftPct = (minLeft / rect.width) * 100;
    const minRightPct = (minRight / rect.width) * 100;
    if (next < minLeftPct) next = minLeftPct;
    if (next > 100 - minRightPct) next = 100 - minRightPct;
    setLeftPct(next);
  }, [minLeft, minRight]);

  const onMouseDown = (e) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const onMove = (ev) => { if (draggingRef.current) onDragMove(ev.clientX); };
    const onUp = () => {
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };
  const onTouchStart = (e) => {
    if (!e.touches?.length) return;
    draggingRef.current = true;
    const onMove = (ev) => {
      if (!ev.touches?.length || !draggingRef.current) return;
      onDragMove(ev.touches[0].clientX);
    };
    const onEnd = () => {
      draggingRef.current = false;
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onEnd);
    };
    window.addEventListener("touchmove", onMove, { passive: true });
    window.addEventListener("touchend", onEnd);
  };
  const onKeyDown = (e) => {
    if (e.key === "ArrowLeft") setLeftPct((v) => Math.max(15, v - 2));
    else if (e.key === "ArrowRight") setLeftPct((v) => Math.min(85, v + 2));
    else if (e.key === "Home") setLeftPct(defaultLeftPct);
  };

  return (
    <div
      ref={wrapRef}
      className="tn-split-pane"
      style={{ "--tn-split-left": `${leftPct}%` }}
    >
      <div className="tn-split-side tn-split-left">{left}</div>
      <div
        className="tn-split-divider"
        role="separator"
        aria-orientation="vertical"
        aria-valuenow={Math.round(leftPct)}
        aria-valuemin={10}
        aria-valuemax={90}
        tabIndex={0}
        onMouseDown={onMouseDown}
        onTouchStart={onTouchStart}
        onKeyDown={onKeyDown}
        title="Drag to resize"
      >
        <span className="tn-split-divider-grip" aria-hidden>⋮</span>
      </div>
      <div className="tn-split-side tn-split-right">{right}</div>
    </div>
  );
}

function CollapsibleCard({ id, title, subtitle, defaultOpen = true, headerRight, children, className = "" }) {
  const storageKey = id ? `tn_report_card_${id}` : null;
  const [open, setOpen] = useState(() => {
    if (!storageKey) return defaultOpen;
    try {
      const v = window.localStorage.getItem(storageKey);
      if (v === "0") return false;
      if (v === "1") return true;
    } catch (_) {}
    return defaultOpen;
  });
  useEffect(() => {
    if (!storageKey) return;
    try {
      window.localStorage.setItem(storageKey, open ? "1" : "0");
    } catch (_) {}
  }, [open, storageKey]);
  return (
    <section className={`tn-collapsible-card ${open ? "is-open" : "is-collapsed"} ${className}`.trim()}>
      <header className="tn-card-head">
        <button type="button" className="tn-card-head-toggle" onClick={() => setOpen((v) => !v)}>
          <span className={`tn-caret ${open ? "down" : "right"}`} aria-hidden>▾</span>
          <span className="tn-card-head-text">
            <span className="tn-card-title">{title}</span>
            {subtitle ? <span className="tn-card-subtitle">{subtitle}</span> : null}
          </span>
        </button>
        {headerRight ? <div className="tn-card-head-actions">{headerRight}</div> : null}
      </header>
      {open ? <div className="tn-card-body">{children}</div> : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// LivePreview: recharts representation of a section (multi-series, limits)
// ---------------------------------------------------------------------------
function ChartLivePreview({ section, formatTagForDisplay }) {
  const [seriesRows, setSeriesRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const reqKeyRef = useRef("");

  const series = useMemo(() => {
    const arr = Array.isArray(section?.series) ? section.series : [];
    return arr
      .filter((s) => s && s.tag_name)
      .map((s, i) => ({
        ...s,
        color: s.color || pickColor(i),
        axis: s.axis === "right" ? "right" : "left",
        chart_type: s.chart_type || "",
        multiplier: Number(s.multiplier ?? 1),
        offset: Number(s.offset ?? 0),
      }));
  }, [section]);

  const range = useMemo(() => toUtcFilterText(
    section?.time_range?.preset,
    section?.time_range?.from_utc,
    section?.time_range?.to_utc,
  ), [section?.time_range?.preset, section?.time_range?.from_utc, section?.time_range?.to_utc]);

  const limitLines = Array.isArray(section?.limit_lines) ? section.limit_lines : [];
  const readingsCount = Math.max(20, Math.min(Number(section?.readings_count) || 200, 2000));

  useEffect(() => {
    if (!series.length) {
      setSeriesRows([]);
      setLoading(false);
      setError("");
      return undefined;
    }
    let cancelled = false;
    const seriesKey = JSON.stringify(series.map((s) => ({
      g: s.gateway_id, t: s.tag_name, m: s.multiplier, o: s.offset,
    })));
    const key = JSON.stringify({ seriesKey, range, readingsCount });
    if (key === reqKeyRef.current) return undefined;
    reqKeyRef.current = key;
    setLoading(true);
    setError("");

    (async () => {
      try {
        const fetched = await Promise.all(series.map((s) =>
          fetchHistorianRange({
            fromUtc: range.from,
            toUtc: range.to,
            gatewayId: s.gateway_id,
            tagName: s.tag_name,
            limit: readingsCount,
          }).then((rows) => rows.reverse())
            .catch(() => [])
        ));
        if (cancelled) return;
        // Align by index (longest series). Build rechart-friendly objects.
        const maxLen = fetched.reduce((m, arr) => Math.max(m, arr.length), 0);
        const rows = [];
        for (let i = 0; i < maxLen; i += 1) {
          const row = { idx: i };
          let ts = "";
          for (let j = 0; j < series.length; j += 1) {
            const s = series[j];
            const point = fetched[j][i];
            if (point && point.ts && !ts) ts = String(point.ts);
            const raw = point?.value;
            const v = (raw === null || raw === undefined || Number.isNaN(Number(raw)))
              ? null
              : Number(raw) * s.multiplier + s.offset;
            row[s.id] = v;
          }
          row.ts = ts;
          rows.push(row);
        }
        setSeriesRows(rows);
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        setError(String(e?.message || e));
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [series, range, readingsCount]);

  if (!series.length) {
    return <p className="muted">Add at least one series with a tag to see a live preview.</p>;
  }
  if (loading && !seriesRows.length) {
    return <p className="muted">Loading preview…</p>;
  }
  if (error && !seriesRows.length) {
    return <p className="error-text">Preview error: {error}</p>;
  }
  if (!seriesRows.length) {
    return <p className="muted">No data points in selected range.</p>;
  }

  const sectionKind = String(section?.type || "line_chart");
  const ChartTag =
    sectionKind === "bar_chart" ? BarChart :
    sectionKind === "area_chart" ? AreaChart :
    sectionKind === "line_chart" ? LineChart : ComposedChart;
  const Container = ChartTag === LineChart || ChartTag === AreaChart || ChartTag === BarChart || ChartTag === ComposedChart
    ? ChartTag
    : ComposedChart;
  const hasRightAxis = series.some((s) => s.axis === "right");
  return (
    <div className="tn-chart-preview">
      <div className="tn-chart-preview-canvas">
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={seriesRows} margin={{ top: 12, right: hasRightAxis ? 40 : 16, left: 8, bottom: 12 }}>
            <CartesianGrid stroke="var(--line, rgba(148, 163, 184, 0.2))" strokeDasharray="3 3" />
            <XAxis
              dataKey="idx"
              tick={{ fontSize: 10, fill: "var(--ink-soft, #64748b)" }}
              axisLine={{ stroke: "var(--stroke, #e2e8f0)" }}
              tickLine={false}
              tickFormatter={(idx) => {
                const r = seriesRows[idx];
                if (!r?.ts) return "";
                return String(r.ts).slice(-8);
              }}
              label={section?.x_axis_label ? {
                value: section.x_axis_label,
                position: "insideBottom",
                offset: -4,
                style: { fontSize: 11, fill: "var(--ink, #334155)" },
              } : undefined}
            />
            <YAxis
              yAxisId="left"
              tick={{ fontSize: 10, fill: "var(--ink-soft, #64748b)" }}
              axisLine={{ stroke: "var(--stroke, #e2e8f0)" }}
              tickFormatter={(v) => `${v}${section?.y_axis_unit || ""}`}
              domain={[
                section?.y_min === "" || section?.y_min === undefined || section?.y_min === null ? "auto" : Number(section.y_min),
                section?.y_max === "" || section?.y_max === undefined || section?.y_max === null ? "auto" : Number(section.y_max),
              ]}
              label={section?.y_axis_label ? {
                value: section.y_axis_label + (section.y_axis_unit ? ` (${section.y_axis_unit})` : ""),
                angle: -90,
                position: "insideLeft",
                offset: 8,
                style: { fontSize: 11, fill: "var(--ink, #334155)" },
              } : undefined}
            />
            {hasRightAxis ? (
              <YAxis
                yAxisId="right"
                orientation="right"
                tick={{ fontSize: 10, fill: "var(--ink-soft, #64748b)" }}
                axisLine={{ stroke: "var(--stroke, #e2e8f0)" }}
                tickFormatter={(v) => `${v}${section?.y_axis_right_unit || ""}`}
                domain={[
                  section?.y_right_min === "" || section?.y_right_min === undefined || section?.y_right_min === null ? "auto" : Number(section.y_right_min),
                  section?.y_right_max === "" || section?.y_right_max === undefined || section?.y_right_max === null ? "auto" : Number(section.y_right_max),
                ]}
                label={section?.y_axis_right_label ? {
                  value: section.y_axis_right_label + (section.y_axis_right_unit ? ` (${section.y_axis_right_unit})` : ""),
                  angle: 90,
                  position: "insideRight",
                  offset: 8,
                  style: { fontSize: 11, fill: "var(--ink, #334155)" },
                } : undefined}
              />
            ) : null}
            <Tooltip
              labelFormatter={(idx) => seriesRows[idx]?.ts || ""}
              formatter={(v, name) => {
                const s = series.find((x) => x.id === name);
                const label = s ? (s.label || s.tag_name) : name;
                const unit = s?.unit ? ` ${s.unit}` : "";
                const formatted =
                  typeof v === "number" ? v.toFixed(3) : v;
                return [`${formatted}${unit}`, label];
              }}
              contentStyle={{
                background: "var(--bg-card, #fff)",
                border: "1px solid var(--stroke, #e2e8f0)",
                borderRadius: 6,
                fontSize: 12,
                color: "var(--ink, #111827)",
              }}
            />
            {section?.show_legend !== false ? (
              <Legend
                formatter={(name) => {
                  const s = series.find((x) => x.id === name);
                  if (!s) return name;
                  const tail = s.axis === "right" ? " (R)" : "";
                  const u = s.unit ? ` [${s.unit}]` : "";
                  return `${s.label || s.tag_name}${tail}${u}`;
                }}
                wrapperStyle={{ fontSize: 11, color: "var(--ink, #334155)" }}
              />
            ) : null}
            {limitLines.map((ll, idx) => {
              if (ll?.value === "" || ll?.value === undefined || ll?.value === null) return null;
              const numericVal = Number(ll.value);
              if (!Number.isFinite(numericVal)) return null;
              return (
                <ReferenceLine
                  key={`ll-${idx}`}
                  y={numericVal}
                  yAxisId={ll.axis === "right" ? "right" : "left"}
                  stroke={ll.color || "#dc2626"}
                  strokeDasharray={ll.dash === false ? "0" : "5 5"}
                  label={ll.label ? {
                    value: ll.label,
                    position: "right",
                    fill: ll.color || "#dc2626",
                    fontSize: 10,
                  } : undefined}
                />
              );
            })}
            {series.map((s, idx) => {
              const kind = (s.chart_type || sectionKind.replace("_chart", "") || "line").toLowerCase();
              const yId = s.axis === "right" ? "right" : "left";
              const color = s.color || pickColor(idx);
              if (kind === "bar") {
                return (
                  <Bar
                    key={s.id}
                    dataKey={s.id}
                    name={s.id}
                    fill={color}
                    yAxisId={yId}
                    isAnimationActive={false}
                  />
                );
              }
              if (kind === "area") {
                return (
                  <Area
                    key={s.id}
                    type="monotone"
                    dataKey={s.id}
                    name={s.id}
                    stroke={color}
                    fill={color}
                    fillOpacity={0.18}
                    yAxisId={yId}
                    isAnimationActive={false}
                    connectNulls
                  />
                );
              }
              return (
                <Line
                  key={s.id}
                  type="monotone"
                  dataKey={s.id}
                  name={s.id}
                  stroke={color}
                  strokeWidth={2}
                  dot={false}
                  yAxisId={yId}
                  isAnimationActive={false}
                  connectNulls
                />
              );
            })}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="muted small">{seriesRows.length} samples · {series.length} series</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HistorianDataCard (table preview with column customization)
// ---------------------------------------------------------------------------
function HistorianDataCard({ section, formatTagForDisplay, onNotify }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const reqKeyRef = useRef("");

  const series = useMemo(() => Array.isArray(section?.series) ? section.series.filter((s) => s.tag_name) : [], [section]);
  const range = useMemo(() => toUtcFilterText(
    section?.time_range?.preset,
    section?.time_range?.from_utc,
    section?.time_range?.to_utc,
  ), [section?.time_range?.preset, section?.time_range?.from_utc, section?.time_range?.to_utc]);
  const rowLimit = Math.max(5, Math.min(Number(section?.row_limit) || 30, 500));
  const columns = useMemo(() => {
    const cols = Array.isArray(section?.columns) ? section.columns : [];
    if (cols.length) return cols;
    const defaults = [{ id: makeId("col"), key: "ts", title: "Timestamp" }];
    for (const s of series) {
      defaults.push({
        id: makeId("col"),
        key: "value",
        series_id: s.id,
        title: s.label || s.tag_name || "Value",
        format: "3dp",
        unit: s.unit || "",
      });
    }
    return defaults;
  }, [section, series]);

  useEffect(() => {
    if (!series.length) {
      setRows([]);
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    const key = JSON.stringify({ series: series.map((s) => `${s.gateway_id}|${s.tag_name}|${s.multiplier}|${s.offset}`), range, rowLimit });
    if (key === reqKeyRef.current) return undefined;
    reqKeyRef.current = key;
    setLoading(true);
    setError("");
    (async () => {
      try {
        const fetched = await Promise.all(series.map((s) =>
          fetchHistorianRange({
            fromUtc: range.from,
            toUtc: range.to,
            gatewayId: s.gateway_id,
            tagName: s.tag_name,
            limit: rowLimit,
          }).then((r) => r.reverse()).catch(() => [])
        ));
        if (cancelled) return;
        const maxLen = fetched.reduce((m, arr) => Math.max(m, arr.length), 0);
        const out = [];
        for (let i = 0; i < maxLen; i += 1) {
          const row = { idx: i, ts: "", values: {} };
          for (let j = 0; j < series.length; j += 1) {
            const s = series[j];
            const point = fetched[j][i];
            if (point) {
              if (!row.ts) row.ts = String(point.ts || "");
              const raw = point.value;
              const v = (raw === null || raw === undefined || Number.isNaN(Number(raw)))
                ? null
                : Number(raw) * Number(s.multiplier ?? 1) + Number(s.offset ?? 0);
              row.values[s.id] = { v, tag: point.tag, gateway: point.gateway_id, quality: point.quality_label };
            } else {
              row.values[s.id] = { v: null };
            }
          }
          out.push(row);
        }
        setRows(out);
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        setError(String(e?.message || e));
        setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [series, range, rowLimit]);

  const formatNumber = (n, preset = "3dp") => {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "-";
    const v = Number(n);
    switch (preset) {
      case "int": return String(Math.round(v));
      case "2dp": return v.toFixed(2);
      case "scientific": return v.toExponential(2);
      case "auto": return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(3);
      default: return v.toFixed(3);
    }
  };

  // Evaluate a calc column on the client (mirrors backend semantics, sandbox-style).
  const evalCalc = (expr, vars) => {
    if (!expr) return null;
    if (/[A-Za-z_]{2,}/.test(expr.replace(/math\./g, "").replace(/[abcdefghijklmnop]/g, ""))) {
      // strip math.* and single-letter variables; anything left is rejected as unsafe.
    }
    const banned = /(\bimport\b|\bopen\b|\beval\b|\bexec\b|__|window|document)/;
    if (banned.test(expr)) return null;
    try {
      const fn = new Function(...Object.keys(vars), `"use strict"; return (${expr});`);
      const result = fn(...Object.values(vars));
      return Number.isFinite(Number(result)) ? Number(result) : null;
    } catch (_) {
      return null;
    }
  };

  const renderCell = (col, row) => {
    const key = (col.key || "").toLowerCase();
    if (key === "ts") return <span title={row.ts}>{(row.ts || "").slice(-19) || "-"}</span>;
    if (key === "tag" || key === "gateway" || key === "quality") {
      const sid = col.series_id;
      const cell = row.values[sid] || {};
      if (key === "tag") return cell.tag || (series.find((s) => s.id === sid)?.tag_name || "-");
      if (key === "gateway") return cell.gateway || (series.find((s) => s.id === sid)?.gateway_id || "-");
      return cell.quality || "-";
    }
    if (key === "value") {
      const sid = col.series_id;
      const cell = row.values[sid] || {};
      const text = formatNumber(cell.v, col.format || "3dp");
      return col.unit ? `${text} ${col.unit}` : text;
    }
    if (key === "calc") {
      const ids = Array.isArray(col.series_ids) ? col.series_ids : [];
      const vars = {};
      let missing = false;
      ids.forEach((sid, idx) => {
        const v = row.values?.[sid]?.v;
        vars[String.fromCharCode(97 + idx)] = v;
        if (v === null || v === undefined) missing = true;
      });
      if (missing) return "-";
      const out = evalCalc(col.expr || "", vars);
      const text = formatNumber(out, col.format || "3dp");
      return col.unit ? `${text} ${col.unit}` : text;
    }
    return "-";
  };

  const handleExportCsv = async () => {
    try {
      const blob = await exportSectionCsv(section);
      downloadBlob(blob, `${(section.title || "data").replace(/\s+/g, "_")}.csv`);
    } catch (e) {
      onNotify?.({ type: "error", message: `CSV export failed: ${e?.message || e}` });
    }
  };
  const handleExportTxt = async () => {
    try {
      const blob = await exportSectionTxt(section);
      downloadBlob(blob, `${(section.title || "data").replace(/\s+/g, "_")}.txt`);
    } catch (e) {
      onNotify?.({ type: "error", message: `TXT export failed: ${e?.message || e}` });
    }
  };

  return (
    <div className="tn-data-card">
      <div className="tn-data-card-toolbar">
        <strong>{section.title || "Historian data"}</strong>
        <div className="tn-data-card-tools">
          <button type="button" className="btn btn-secondary" onClick={handleExportCsv}>Export CSV</button>
          <button type="button" className="btn btn-secondary" onClick={handleExportTxt}>Export TXT</button>
        </div>
      </div>
      {loading && !rows.length ? <p className="muted">Loading rows…</p> : null}
      {error && !rows.length ? <p className="error-text">{error}</p> : null}
      {!series.length ? <p className="muted">Add a tag series to see historian rows.</p> : null}
      {rows.length ? (
        <div className="tn-data-card-tablewrap">
          <table className="tn-data-card-table">
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col.id || col.title}>{col.title || col.key}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.slice(-rowLimit).map((row, ri) => (
                <tr key={ri}>
                  {columns.map((col) => (
                    <td key={col.id || col.title}>{renderCell(col, row)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SectionEditor (per-type configuration UI)
// ---------------------------------------------------------------------------
function SectionEditor({
  section,
  index,
  total,
  gatewayOptions,
  tagsByGateway,
  formatTagForDisplay,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
  onNotify,
}) {
  const type = section?.type || "text";
  const preset = SECTION_PRESETS.find((p) => p.type === type);
  const tagList = (gid) => {
    const raw = tagsByGateway?.[String(gid || "")];
    return Array.isArray(raw) ? raw : [];
  };

  const updateSeries = (sid, patch) => {
    const next = (section.series || []).map((s) => (s.id === sid ? { ...s, ...patch } : s));
    onChange({ series: next });
  };
  const addSeries = () => {
    const next = [...(section.series || []), {
      id: makeId("ser"),
      label: "",
      gateway_id: "",
      tag_name: "",
      color: pickColor((section.series || []).length),
      axis: "left",
      chart_type: "",
      unit: "",
      multiplier: 1,
      offset: 0,
    }];
    onChange({ series: next });
  };
  const removeSeries = (sid) => {
    onChange({ series: (section.series || []).filter((s) => s.id !== sid) });
  };

  const updateLimit = (lid, patch) => {
    const next = (section.limit_lines || []).map((l) => (l.id === lid ? { ...l, ...patch } : l));
    onChange({ limit_lines: next });
  };
  const addLimit = () => {
    onChange({
      limit_lines: [
        ...(section.limit_lines || []),
        { id: makeId("lim"), label: "Limit", value: "", axis: "left", color: "#dc2626", dash: true },
      ],
    });
  };
  const removeLimit = (lid) => onChange({ limit_lines: (section.limit_lines || []).filter((l) => l.id !== lid) });

  const updateColumn = (cid, patch) => {
    const next = (section.columns || []).map((c) => (c.id === cid ? { ...c, ...patch } : c));
    onChange({ columns: next });
  };
  const addColumn = (kind = "value") => {
    const seed = { id: makeId("col"), key: kind, title: "" };
    if (kind === "value") {
      const firstSeries = (section.series || [])[0];
      seed.series_id = firstSeries?.id || "";
      seed.title = firstSeries?.label || firstSeries?.tag_name || "Value";
      seed.format = "3dp";
      seed.unit = firstSeries?.unit || "";
    } else if (kind === "calc") {
      seed.title = "Calculated";
      seed.series_ids = (section.series || []).slice(0, 2).map((s) => s.id);
      seed.expr = (section.series || []).length >= 2 ? "a - b" : "a";
      seed.format = "3dp";
      seed.unit = "";
    } else if (kind === "tag" || kind === "gateway" || kind === "quality") {
      seed.title = kind.charAt(0).toUpperCase() + kind.slice(1);
      seed.series_id = (section.series || [])[0]?.id || "";
    } else if (kind === "ts") {
      seed.title = "Timestamp";
    }
    onChange({ columns: [...(section.columns || []), seed] });
  };
  const removeColumn = (cid) => onChange({ columns: (section.columns || []).filter((c) => c.id !== cid) });
  const moveColumn = (cid, direction) => {
    const list = [...(section.columns || [])];
    const idx = list.findIndex((c) => c.id === cid);
    if (idx < 0) return;
    const target = idx + direction;
    if (target < 0 || target >= list.length) return;
    [list[idx], list[target]] = [list[target], list[idx]];
    onChange({ columns: list });
  };

  const isChart = type === "line_chart" || type === "area_chart" || type === "bar_chart";

  return (
    <div className="tn-section-editor">
      <header className="tn-section-editor-head">
        <span className="tn-section-pill">{preset?.label || type}</span>
        <div className="tn-section-editor-tools">
          <button type="button" className="icon-btn" onClick={onMoveUp} disabled={index === 0} title="Move up">↑</button>
          <button type="button" className="icon-btn" onClick={onMoveDown} disabled={index === total - 1} title="Move down">↓</button>
          <button type="button" className="icon-btn icon-btn-danger" onClick={onRemove} title="Remove">×</button>
        </div>
      </header>

      {type === "header" ? (
        <div className="tn-section-form">
          <div className="tn-row tn-row-2">
            <label>Title<input value={section.title || ""} onChange={(e) => onChange({ title: e.target.value })} /></label>
            <label>Subtitle<input value={section.subtitle || ""} onChange={(e) => onChange({ subtitle: e.target.value })} /></label>
          </div>
          <label className="tn-checkbox">
            <input
              type="checkbox"
              checked={section.show_generated_at !== false}
              onChange={(e) => onChange({ show_generated_at: e.target.checked })}
            />
            Show generated timestamp
          </label>
        </div>
      ) : null}

      {type === "text" ? (
        <div className="tn-section-form">
          <label>Heading<input value={section.title || ""} onChange={(e) => onChange({ title: e.target.value })} /></label>
          <label>Text
            <textarea
              rows={4}
              value={section.text || ""}
              onChange={(e) => onChange({ text: e.target.value })}
              placeholder="Plain text paragraph; line breaks are preserved."
            />
          </label>
        </div>
      ) : null}

      {type === "kpi_grid" ? (
        <div className="tn-section-form">
          <div className="tn-row tn-row-2">
            <label>Title<input value={section.title || ""} onChange={(e) => onChange({ title: e.target.value })} /></label>
            <label>Columns
              <input
                type="number" min={1} max={6}
                value={section.columns || 4}
                onChange={(e) => onChange({ columns: Math.max(1, Math.min(6, Number(e.target.value || 4))) })}
              />
            </label>
          </div>
          <div className="tn-kpi-list">
            {(section.items || []).map((item, idx) => (
              <div key={item.id || idx} className="tn-kpi-row">
                <input
                  value={item.label || ""}
                  placeholder="Label"
                  onChange={(e) => onChange({
                    items: section.items.map((it, i) => (i === idx ? { ...it, label: e.target.value } : it)),
                  })}
                />
                <select
                  value={item.gateway_id || ""}
                  onChange={(e) => onChange({
                    items: section.items.map((it, i) => (i === idx ? { ...it, gateway_id: e.target.value, tag_name: "" } : it)),
                  })}
                >
                  <option value="">Gateway...</option>
                  {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
                </select>
                <select
                  value={item.tag_name || ""}
                  onChange={(e) => onChange({
                    items: section.items.map((it, i) => (i === idx ? { ...it, tag_name: e.target.value } : it)),
                  })}
                >
                  <option value="">Tag...</option>
                  {tagList(item.gateway_id).map((t) => <option key={t} value={t}>{formatTagForDisplay(t)}</option>)}
                </select>
                <select
                  value={item.aggregation || "avg"}
                  onChange={(e) => onChange({
                    items: section.items.map((it, i) => (i === idx ? { ...it, aggregation: e.target.value } : it)),
                  })}
                >
                  {KPI_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
                </select>
                <button type="button" className="icon-btn icon-btn-danger" onClick={() => onChange({ items: section.items.filter((_, i) => i !== idx) })}>×</button>
              </div>
            ))}
            <button type="button" className="btn btn-link" onClick={() => onChange({
              items: [
                ...(section.items || []),
                { id: makeId("kpi"), label: "Metric", gateway_id: "", tag_name: "", aggregation: "avg", operator: "any" },
              ],
            })}>
              + Add KPI cell
            </button>
          </div>
        </div>
      ) : null}

      {(isChart || type === "table") ? (
        <div className="tn-section-form">
          <CollapsibleCard id={`${section.id}-basics`} title="Basics" defaultOpen>
            <div className="tn-row tn-row-2">
              <label>Title<input value={section.title || ""} onChange={(e) => onChange({ title: e.target.value })} /></label>
              {isChart ? (
                <label>Sample count
                  <input
                    type="number" min={20} max={5000}
                    value={section.readings_count || 200}
                    onChange={(e) => onChange({ readings_count: Math.max(20, Math.min(5000, Number(e.target.value || 200))) })}
                  />
                </label>
              ) : (
                <label>Row limit
                  <input
                    type="number" min={5} max={500}
                    value={section.row_limit || 30}
                    onChange={(e) => onChange({ row_limit: Math.max(5, Math.min(500, Number(e.target.value || 30))) })}
                  />
                </label>
              )}
            </div>
            <div className="tn-row tn-row-2">
              <label>Time range
                <select
                  value={section.time_range?.preset || "24h"}
                  onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), preset: e.target.value } })}
                >
                  {TIME_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
              </label>
              {section.time_range?.preset === "custom" ? (
                <div className="tn-row tn-row-2 tn-row-tight">
                  <label>From (UTC)
                    <input type="datetime-local" value={section.time_range?.from_utc || ""} onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), from_utc: e.target.value } })} />
                  </label>
                  <label>To (UTC)
                    <input type="datetime-local" value={section.time_range?.to_utc || ""} onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), to_utc: e.target.value } })} />
                  </label>
                </div>
              ) : null}
            </div>
            {/* Section-level bucket size. "Raw samples" keeps the current
                behaviour (every historian point is plotted as a chart point).
                Anything else groups samples into fixed-width buckets and each
                series reduces its bucket via its own aggregation choice. */}
            <div className="tn-row tn-row-2">
              <label>Bucket (group samples)
                <select
                  value={section.bucket || "raw"}
                  onChange={(e) => onChange({ bucket: e.target.value })}
                  title="Group raw samples into time buckets before aggregating per series"
                >
                  {BUCKET_SIZES.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
                </select>
              </label>
              <div className="tn-help-text">
                Pick a bucket size to summarise samples per minute / hour / day. Each series below
                chooses its own aggregation (avg, max, min, last…) which is applied <em>inside</em> the bucket.
              </div>
            </div>
          </CollapsibleCard>

          <CollapsibleCard id={`${section.id}-series`} title="Data series" subtitle={`${(section.series || []).length} configured`}>
            {(section.series || []).map((s, idx) => {
              const op = s.operator || "any";
              const needsV2 = op === "between";
              const showV1 = op !== "any";
              return (
                <div key={s.id} className="tn-series-block">
                  <div className="tn-series-row">
                    <input
                      value={s.label || ""}
                      placeholder={`Series ${idx + 1} label`}
                      onChange={(e) => updateSeries(s.id, { label: e.target.value })}
                    />
                    <select value={s.gateway_id || ""} onChange={(e) => updateSeries(s.id, { gateway_id: e.target.value, tag_name: "" })}>
                      <option value="">Gateway...</option>
                      {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
                    </select>
                    <select value={s.tag_name || ""} onChange={(e) => updateSeries(s.id, { tag_name: e.target.value })}>
                      <option value="">Tag...</option>
                      {tagList(s.gateway_id).map((t) => <option key={t} value={t}>{formatTagForDisplay(t)}</option>)}
                    </select>
                    {isChart ? (
                      <>
                        <select value={s.axis || "left"} onChange={(e) => updateSeries(s.id, { axis: e.target.value })} title="Axis">
                          <option value="left">Left</option>
                          <option value="right">Right</option>
                        </select>
                        <select value={s.chart_type || ""} onChange={(e) => updateSeries(s.id, { chart_type: e.target.value })} title="Chart kind">
                          {SERIES_CHART_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
                        </select>
                      </>
                    ) : null}
                    <input type="color" value={s.color || pickColor(idx)} onChange={(e) => updateSeries(s.id, { color: e.target.value })} title="Series color" />
                    <input
                      type="text" placeholder="Unit"
                      value={s.unit || ""}
                      onChange={(e) => updateSeries(s.id, { unit: e.target.value })}
                    />
                    <input
                      type="number" step="any" placeholder="× mult."
                      value={s.multiplier ?? 1}
                      onChange={(e) => updateSeries(s.id, { multiplier: Number(e.target.value) })}
                      title="Multiplier applied before plotting"
                    />
                    <input
                      type="number" step="any" placeholder="+ offset"
                      value={s.offset ?? 0}
                      onChange={(e) => updateSeries(s.id, { offset: Number(e.target.value) })}
                      title="Offset added after multiplication"
                    />
                    <button type="button" className="icon-btn icon-btn-danger" onClick={() => removeSeries(s.id)}>×</button>
                  </div>
                  {/* Analytics row: per-series aggregation + value-predicate
                      filter. Kept on its own line so the main row above does
                      not become unreadable. With the section bucket set to
                      "Raw samples" and aggregation = None the filter still
                      applies (rows that fail the predicate are dropped). */}
                  <div className="tn-series-row tn-series-row-analytics">
                    <label className="tn-inline-label" title="How to reduce samples inside a bucket. Used together with the section's Bucket setting.">
                      Aggregation
                      <select
                        value={s.aggregation || ""}
                        onChange={(e) => updateSeries(s.id, { aggregation: e.target.value })}
                      >
                        {SERIES_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
                      </select>
                    </label>
                    <label className="tn-inline-label" title="Drop samples that fail this value predicate before bucketing.">
                      Filter
                      <select
                        value={op}
                        onChange={(e) => updateSeries(s.id, { operator: e.target.value })}
                      >
                        {SERIES_FILTER_OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                      </select>
                    </label>
                    {showV1 ? (
                      <label className="tn-inline-label">
                        {needsV2 ? "Lower" : "Value"}
                        <input
                          type="number" step="any"
                          value={s.value1 ?? ""}
                          placeholder={needsV2 ? "min" : "value"}
                          onChange={(e) => updateSeries(s.id, { value1: e.target.value === "" ? null : Number(e.target.value) })}
                        />
                      </label>
                    ) : null}
                    {needsV2 ? (
                      <label className="tn-inline-label">
                        Upper
                        <input
                          type="number" step="any"
                          value={s.value2 ?? ""}
                          placeholder="max"
                          onChange={(e) => updateSeries(s.id, { value2: e.target.value === "" ? null : Number(e.target.value) })}
                        />
                      </label>
                    ) : null}
                  </div>
                </div>
              );
            })}
            <button type="button" className="btn btn-link" onClick={addSeries}>+ Add series</button>
          </CollapsibleCard>

          {isChart ? (
            <CollapsibleCard id={`${section.id}-axes`} title="Axes, units & scale" defaultOpen={false}>
              <div className="tn-row tn-row-2">
                <label>X-axis title<input value={section.x_axis_label || ""} onChange={(e) => onChange({ x_axis_label: e.target.value })} /></label>
                <label>Value format
                  <select value={section.value_format || "auto"} onChange={(e) => onChange({ value_format: e.target.value })}>
                    {VALUE_FORMATS.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
                  </select>
                </label>
              </div>
              <fieldset className="tn-fieldset">
                <legend>Primary Y-axis (left)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<input value={section.y_axis_label || ""} onChange={(e) => onChange({ y_axis_label: e.target.value })} /></label>
                  <label>Unit / suffix<input value={section.y_axis_unit || ""} onChange={(e) => onChange({ y_axis_unit: e.target.value })} placeholder="e.g. kW" /></label>
                  <label>Min<input type="number" step="any" value={section.y_min ?? ""} onChange={(e) => onChange({ y_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_max ?? ""} onChange={(e) => onChange({ y_max: e.target.value })} placeholder="auto" /></label>
                </div>
              </fieldset>
              <fieldset className="tn-fieldset">
                <legend>Secondary Y-axis (right, optional)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<input value={section.y_axis_right_label || ""} onChange={(e) => onChange({ y_axis_right_label: e.target.value })} /></label>
                  <label>Unit / suffix<input value={section.y_axis_right_unit || ""} onChange={(e) => onChange({ y_axis_right_unit: e.target.value })} placeholder="e.g. °C" /></label>
                  <label>Min<input type="number" step="any" value={section.y_right_min ?? ""} onChange={(e) => onChange({ y_right_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_right_max ?? ""} onChange={(e) => onChange({ y_right_max: e.target.value })} placeholder="auto" /></label>
                </div>
              </fieldset>
              <label className="tn-checkbox">
                <input type="checkbox" checked={section.show_legend !== false} onChange={(e) => onChange({ show_legend: e.target.checked })} />
                Show legend
              </label>
            </CollapsibleCard>
          ) : null}

          {isChart ? (
            <CollapsibleCard id={`${section.id}-limits`} title="Limit lines" subtitle={`${(section.limit_lines || []).length} configured`} defaultOpen={false}>
              {(section.limit_lines || []).map((l) => (
                <div key={l.id} className="tn-limit-row">
                  <input value={l.label || ""} placeholder="Label" onChange={(e) => updateLimit(l.id, { label: e.target.value })} />
                  <input type="number" step="any" value={l.value ?? ""} onChange={(e) => updateLimit(l.id, { value: e.target.value })} placeholder="Threshold" />
                  <select value={l.axis || "left"} onChange={(e) => updateLimit(l.id, { axis: e.target.value })}>
                    <option value="left">Left axis</option>
                    <option value="right">Right axis</option>
                  </select>
                  <input type="color" value={l.color || "#dc2626"} onChange={(e) => updateLimit(l.id, { color: e.target.value })} />
                  <label className="tn-checkbox-inline">
                    <input type="checkbox" checked={l.dash !== false} onChange={(e) => updateLimit(l.id, { dash: e.target.checked })} /> dashed
                  </label>
                  <button type="button" className="icon-btn icon-btn-danger" onClick={() => removeLimit(l.id)}>×</button>
                </div>
              ))}
              <button type="button" className="btn btn-link" onClick={addLimit}>+ Add limit line</button>
            </CollapsibleCard>
          ) : null}

          {isChart ? (
            <CollapsibleCard id={`${section.id}-preview`} title="Live chart preview" defaultOpen>
              <ChartLivePreview section={section} formatTagForDisplay={formatTagForDisplay} />
            </CollapsibleCard>
          ) : null}

          {type === "table" ? (
            <>
              <CollapsibleCard id={`${section.id}-columns`} title="Columns" subtitle={`${(section.columns || []).length || (section.series || []).length + 1} columns`}>
                {(section.columns || []).map((col, ci) => (
                  <div key={col.id} className="tn-column-row">
                    <select value={col.key || "value"} onChange={(e) => updateColumn(col.id, { key: e.target.value })}>
                      {COLUMN_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
                    </select>
                    <input value={col.title || ""} placeholder="Title" onChange={(e) => updateColumn(col.id, { title: e.target.value })} />
                    {(col.key === "value" || col.key === "tag" || col.key === "gateway" || col.key === "quality") ? (
                      <select value={col.series_id || ""} onChange={(e) => updateColumn(col.id, { series_id: e.target.value })}>
                        <option value="">Series...</option>
                        {(section.series || []).map((s) => <option key={s.id} value={s.id}>{s.label || s.tag_name || s.id}</option>)}
                      </select>
                    ) : null}
                    {col.key === "calc" ? (
                      <input
                        value={col.expr || ""}
                        placeholder="Expression (a, b, c... = series)"
                        onChange={(e) => updateColumn(col.id, { expr: e.target.value })}
                      />
                    ) : null}
                    {(col.key === "value" || col.key === "calc") ? (
                      <>
                        <select value={col.format || "3dp"} onChange={(e) => updateColumn(col.id, { format: e.target.value })}>
                          {VALUE_FORMATS.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
                        </select>
                        <input value={col.unit || ""} placeholder="Unit" onChange={(e) => updateColumn(col.id, { unit: e.target.value })} />
                      </>
                    ) : null}
                    {col.key === "calc" ? (
                      <input
                        value={(col.series_ids || []).join(",")}
                        placeholder="series IDs (a,b)"
                        onChange={(e) => updateColumn(col.id, { series_ids: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
                      />
                    ) : null}
                    <button type="button" className="icon-btn" onClick={() => moveColumn(col.id, -1)} disabled={ci === 0}>↑</button>
                    <button type="button" className="icon-btn" onClick={() => moveColumn(col.id, 1)} disabled={ci === (section.columns || []).length - 1}>↓</button>
                    <button type="button" className="icon-btn icon-btn-danger" onClick={() => removeColumn(col.id)}>×</button>
                  </div>
                ))}
                <div className="tn-row tn-row-tight">
                  <button type="button" className="btn btn-link" onClick={() => addColumn("ts")}>+ Timestamp</button>
                  <button type="button" className="btn btn-link" onClick={() => addColumn("value")}>+ Value</button>
                  <button type="button" className="btn btn-link" onClick={() => addColumn("calc")}>+ Calculation</button>
                  <button type="button" className="btn btn-link" onClick={() => addColumn("tag")}>+ Tag</button>
                  <button type="button" className="btn btn-link" onClick={() => addColumn("gateway")}>+ Gateway</button>
                  <button type="button" className="btn btn-link" onClick={() => addColumn("quality")}>+ Quality</button>
                </div>
                <p className="muted small">
                  Calculation expressions reference series in order via variables <code>a</code>, <code>b</code>, <code>c</code>… Use the series IDs box to pick which series feed which variable. Example: <code>(a + b) / 2</code> averages two series. Standard arithmetic plus <code>math.*</code> functions are allowed.
                </p>
              </CollapsibleCard>

              <CollapsibleCard id={`${section.id}-data`} title="Historian data preview">
                <HistorianDataCard section={section} formatTagForDisplay={formatTagForDisplay} onNotify={onNotify} />
              </CollapsibleCard>
            </>
          ) : null}
        </div>
      ) : null}

      {type === "pie_chart" ? (
        <div className="tn-section-form">
          <label>Title<input value={section.title || ""} onChange={(e) => onChange({ title: e.target.value })} /></label>
          <div className="tn-row tn-row-2">
            <label>Data source
              <select value={section.data_source_type || "tag_direct"} onChange={(e) => onChange({ data_source_type: e.target.value })}>
                <option value="tag_direct">All tags (direct)</option>
                <option value="computed">Computed rules</option>
              </select>
            </label>
            <label>Gateway
              <select value={section.gateway_id || ""} onChange={(e) => onChange({ gateway_id: e.target.value })}>
                <option value="">Select gateway...</option>
                {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
              </select>
            </label>
          </div>
          {section.data_source_type === "computed" ? (
            <ComputedRulesEditor
              rules={section.compute_rules || []}
              gatewayOptions={gatewayOptions}
              tagsByGateway={tagsByGateway}
              formatTagForDisplay={formatTagForDisplay}
              onChange={(rules) => onChange({ compute_rules: rules })}
              fallbackGatewayId={section.gateway_id || ""}
            />
          ) : (
            <div className="tn-row tn-row-3">
              <label>Tag (optional)
                <select value={section.tag_name || ""} onChange={(e) => onChange({ tag_name: e.target.value })}>
                  <option value="">All tags in gateway</option>
                  {tagList(section.gateway_id).map((t) => <option key={t} value={t}>{formatTagForDisplay(t)}</option>)}
                </select>
              </label>
              <label>Aggregation
                <select value={section.query_result_aggregation || "count"} onChange={(e) => onChange({ query_result_aggregation: e.target.value })}>
                  {KPI_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
                </select>
              </label>
              <label>Time range
                <select value={section.time_range?.preset || "24h"} onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), preset: e.target.value } })}>
                  {TIME_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
              </label>
            </div>
          )}
        </div>
      ) : null}

      {type === "spacer" ? (
        <div className="tn-section-form">
          <label>Height (mm)
            <input type="number" min={2} max={80} value={section.height || 8} onChange={(e) => onChange({ height: Math.max(2, Math.min(80, Number(e.target.value || 8))) })} />
          </label>
        </div>
      ) : null}

      {type === "page_break" ? (
        <p className="tn-section-caption">A page break starts the next section on a new page.</p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ComputedRulesEditor (pie chart computed mode)
// ---------------------------------------------------------------------------
function ComputedRulesEditor({ rules, gatewayOptions, tagsByGateway, formatTagForDisplay, onChange, fallbackGatewayId }) {
  const tagList = (gid) => {
    const raw = tagsByGateway?.[String(gid || "")];
    return Array.isArray(raw) ? raw : [];
  };
  const update = (idx, patch) => onChange((rules || []).map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  const remove = (idx) => onChange((rules || []).filter((_, i) => i !== idx));
  const add = () => onChange([
    ...(rules || []),
    {
      id: makeId("rule"),
      label: `Item ${(rules || []).length + 1}`,
      gateway_id: fallbackGatewayId || "",
      tag_name: "",
      operator: "gt",
      value1: "",
      value2: "",
      aggregation: "count",
      color: pickColor((rules || []).length),
    },
  ]);
  return (
    <div className="tn-rules-list">
      {(rules || []).map((r, idx) => (
        <div key={r.id || idx} className="tn-rule-row">
          <input value={r.label || ""} placeholder="Label" onChange={(e) => update(idx, { label: e.target.value })} />
          <select value={r.gateway_id || ""} onChange={(e) => update(idx, { gateway_id: e.target.value, tag_name: "" })}>
            <option value="">Gateway...</option>
            {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
          </select>
          <select value={r.tag_name || ""} onChange={(e) => update(idx, { tag_name: e.target.value })}>
            <option value="">Tag...</option>
            {tagList(r.gateway_id).map((t) => <option key={t} value={t}>{formatTagForDisplay(t)}</option>)}
          </select>
          <select value={r.operator || "gt"} onChange={(e) => update(idx, { operator: e.target.value })}>
            {RULE_OPERATORS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <input value={r.value1 ?? ""} placeholder="Value" onChange={(e) => update(idx, { value1: e.target.value })} />
          {r.operator === "between" ? (
            <input value={r.value2 ?? ""} placeholder="Value 2" onChange={(e) => update(idx, { value2: e.target.value })} />
          ) : null}
          <select value={r.aggregation || "count"} onChange={(e) => update(idx, { aggregation: e.target.value })}>
            {KPI_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
          <input type="color" value={r.color || pickColor(idx)} onChange={(e) => update(idx, { color: e.target.value })} />
          <button type="button" className="icon-btn icon-btn-danger" onClick={() => remove(idx)}>×</button>
        </div>
      ))}
      <button type="button" className="btn btn-link" onClick={add}>+ Add rule</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-level component
// ---------------------------------------------------------------------------
export function ReportTemplateDesigner({
  gatewayOptions = [],
  tagsByGateway = {},
  emailSettings = null,
  formatTagForDisplay = (x) => x,
  onNotify = () => {},
}) {
  const [templates, setTemplates] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState({
    id: "",
    name: "Untitled report",
    description: "",
    definition: { sections: [] },
  });
  const [savingState, setSavingState] = useState({ saving: false, error: "" });
  const [previewState, setPreviewState] = useState({ rendering: false, generatedId: "", error: "" });
  const [previewKey, setPreviewKey] = useState(0);

  const refreshTemplates = useCallback(async () => {
    try {
      const res = await listReportTemplates();
      const list = Array.isArray(res?.templates) ? res.templates : [];
      setTemplates(list);
      return list;
    } catch (e) {
      onNotify({ type: "error", message: `Failed to load templates: ${e?.message || e}` });
      return [];
    }
  }, [onNotify]);

  useEffect(() => { refreshTemplates(); }, [refreshTemplates]);

  const loadTemplate = (id) => {
    const tpl = templates.find((t) => String(t.id) === String(id));
    if (!tpl) return;
    setSelectedId(tpl.id);
    setDraft({
      id: tpl.id,
      name: tpl.name || "",
      description: tpl.description || "",
      definition: {
        sections: Array.isArray(tpl?.definition?.sections) ? tpl.definition.sections : [],
      },
    });
  };

  const startNew = (presetKey = "blank") => {
    const preset = makePresetTemplate(presetKey);
    setSelectedId("");
    setDraft({
      id: "",
      name: preset.name || "New report",
      description: preset.description || "",
      definition: { sections: Array.isArray(preset.sections) ? preset.sections : [] },
    });
  };

  const handleSave = async () => {
    setSavingState({ saving: true, error: "" });
    try {
      const res = await saveReportTemplate(draft);
      const saved = res?.template;
      if (saved?.id) {
        setDraft({
          id: saved.id,
          name: saved.name || "",
          description: saved.description || "",
          definition: { sections: Array.isArray(saved?.definition?.sections) ? saved.definition.sections : [] },
        });
        setSelectedId(saved.id);
      }
      await refreshTemplates();
      onNotify({ type: "success", message: "Template saved" });
    } catch (e) {
      setSavingState({ saving: false, error: String(e?.message || e) });
      onNotify({ type: "error", message: `Save failed: ${e?.message || e}` });
      return;
    }
    setSavingState({ saving: false, error: "" });
  };

  const handleDelete = async () => {
    if (!draft.id) return;
    if (!window.confirm(`Delete template "${draft.name}"? This cannot be undone.`)) return;
    try {
      await deleteReportTemplate(draft.id);
      await refreshTemplates();
      startNew();
      onNotify({ type: "success", message: "Template deleted" });
    } catch (e) {
      onNotify({ type: "error", message: `Delete failed: ${e?.message || e}` });
    }
  };

  // ----- export / import bundles -----------------------------------------
  const importInputRef = useRef(null);

  const _downloadJson = (bundle, suggestedName) => {
    try {
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const safeName = String(suggestedName || "trustnode-report-templates")
        .replace(/[^A-Za-z0-9_.-]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 80) || "trustnode-report-templates";
      a.download = `${safeName}.tnreport.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => { try { URL.revokeObjectURL(url); } catch (_) {} }, 250);
    } catch (e) {
      onNotify({ type: "error", message: `Could not save file: ${e?.message || e}` });
    }
  };

  const handleExportCurrent = async () => {
    if (!draft.id) {
      // Unsaved draft — export from local state so users can move work between PCs
      // without forcing a save first.
      _downloadJson(
        {
          kind: "trustnode.report-template-bundle",
          bundle_version: 1,
          templates: [
            {
              name: draft.name,
              description: draft.description,
              definition: draft.definition || { sections: [] },
            },
          ],
        },
        draft.name || "report-template"
      );
      return;
    }
    try {
      const bundle = await exportReportTemplate(draft.id);
      _downloadJson(bundle, draft.name || "report-template");
    } catch (e) {
      onNotify({ type: "error", message: `Export failed: ${e?.message || e}` });
    }
  };

  const handleExportAll = async () => {
    try {
      const bundle = await exportAllReportTemplates();
      _downloadJson(bundle, "trustnode-report-templates");
    } catch (e) {
      onNotify({ type: "error", message: `Export failed: ${e?.message || e}` });
    }
  };

  const handleImportClick = () => {
    if (importInputRef.current) importInputRef.current.click();
  };

  const handleImportFile = async (event) => {
    const file = event.target.files && event.target.files[0];
    // Clear the input so re-selecting the same file still fires onChange.
    event.target.value = "";
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const res = await importReportTemplates(data);
      const count = Number(res?.count || 0);
      onNotify({
        type: "success",
        message: count > 0 ? `Imported ${count} template${count === 1 ? "" : "s"}` : "No templates imported",
      });
      const list = await refreshTemplates();
      const first = Array.isArray(res?.imported) && res.imported[0];
      if (first && first.id) {
        // Load the first imported template into the editor for convenience.
        const found = list.find((t) => String(t.id) === String(first.id));
        if (found) {
          setSelectedId(found.id);
          setDraft({
            id: found.id,
            name: found.name || "",
            description: found.description || "",
            definition: { sections: Array.isArray(found?.definition?.sections) ? found.definition.sections : [] },
          });
        }
      }
    } catch (e) {
      onNotify({ type: "error", message: `Import failed: ${e?.message || e}` });
    }
  };

  // PDF preview uses a Blob URL fetched with the auth header, because the
  // browser <iframe src> cannot carry a Bearer token. The PDF stream endpoint
  // would otherwise return 401 "Authentication required".
  const [previewBlobUrl, setPreviewBlobUrl] = useState("");
  useEffect(() => () => {
    if (previewBlobUrl) {
      try { URL.revokeObjectURL(previewBlobUrl); } catch (_) {}
    }
  }, [previewBlobUrl]);

  const fetchPreviewBlob = async (generatedId) => {
    if (!generatedId) return "";
    const blob = await downloadGeneratedReportBlob(generatedId);
    return URL.createObjectURL(blob);
  };

  const handlePreview = async () => {
    setPreviewState({ rendering: true, generatedId: "", error: "" });
    try {
      const res = await renderReportPreview(draft);
      const generated = res?.generated;
      if (!generated?.id) throw new Error("Renderer did not return a report id");
      const blobUrl = await fetchPreviewBlob(generated.id);
      if (previewBlobUrl) {
        try { URL.revokeObjectURL(previewBlobUrl); } catch (_) {}
      }
      setPreviewBlobUrl(blobUrl);
      setPreviewState({ rendering: false, generatedId: generated.id, error: "" });
      setPreviewKey((k) => k + 1);
    } catch (e) {
      setPreviewState({ rendering: false, generatedId: "", error: String(e?.message || e) });
      onNotify({ type: "error", message: `Preview failed: ${e?.message || e}` });
    }
  };

  const handleDownloadPdf = async () => {
    try {
      let generatedId = previewState.generatedId;
      if (!generatedId) {
        const res = await renderReportPreview(draft);
        generatedId = res?.generated?.id;
        if (!generatedId) throw new Error("Render did not return a report id");
        setPreviewState({ rendering: false, generatedId, error: "" });
      }
      const blob = await downloadGeneratedReportBlob(generatedId);
      const fileName = `${(draft.name || "report").replace(/\s+/g, "_")}.pdf`;
      downloadBlob(blob, fileName);
    } catch (e) {
      onNotify({ type: "error", message: `Download failed: ${e?.message || e}` });
    }
  };

  const handleOpenInNewTab = async () => {
    try {
      const id = previewState.generatedId;
      if (!id) return;
      const blob = await downloadGeneratedReportBlob(id);
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener,noreferrer");
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      onNotify({ type: "error", message: `Open failed: ${e?.message || e}` });
    }
  };

  const previewUrl = previewBlobUrl ? `${previewBlobUrl}#zoom=page-width&v=${previewKey}` : "";

  // section ops -------------------------------------------------------------
  const updateSection = (sectionId, patch) => {
    setDraft((prev) => ({
      ...prev,
      definition: {
        ...prev.definition,
        sections: (prev.definition?.sections || []).map((s) =>
          String(s.id) === String(sectionId) ? { ...s, ...patch } : s
        ),
      },
    }));
  };
  const removeSection = (sectionId) => {
    setDraft((prev) => ({
      ...prev,
      definition: {
        ...prev.definition,
        sections: (prev.definition?.sections || []).filter((s) => String(s.id) !== String(sectionId)),
      },
    }));
  };
  const moveSection = (sectionId, direction) => {
    setDraft((prev) => {
      const list = [...(prev.definition?.sections || [])];
      const idx = list.findIndex((s) => String(s.id) === String(sectionId));
      if (idx < 0) return prev;
      const target = idx + direction;
      if (target < 0 || target >= list.length) return prev;
      [list[idx], list[target]] = [list[target], list[idx]];
      return { ...prev, definition: { ...prev.definition, sections: list } };
    });
  };
  const addSection = (type) => {
    setDraft((prev) => ({
      ...prev,
      definition: {
        ...prev.definition,
        sections: [...(prev.definition?.sections || []), defaultSection(type)],
      },
    }));
  };

  const sections = draft.definition?.sections || [];

  const leftPane = (
    <>
      <CollapsibleCard
        id="report-toolbar"
        title="Report builder"
        subtitle={draft.id ? `Editing: ${draft.name}` : "Compose a new template, preview it, then save"}
        defaultOpen
        headerRight={(
          <div className="tn-report-actions">
            <button type="button" className="btn btn-primary" onClick={handleSave} disabled={savingState.saving}>
              {savingState.saving ? "Saving…" : draft.id ? "Save changes" : "Save template"}
            </button>
            <button type="button" className="btn btn-success" onClick={handlePreview} disabled={previewState.rendering}>
              {previewState.rendering ? "Rendering…" : "Preview PDF"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={handleDownloadPdf} disabled={previewState.rendering}>
              Download PDF
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleExportCurrent}
              title="Export this template to a portable .tnreport.json file"
            >
              Export
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleExportAll}
              title="Export all templates as a bundle"
            >
              Export all
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleImportClick}
              title="Import a .tnreport.json bundle from another edge"
            >
              Import
            </button>
            <input
              ref={importInputRef}
              type="file"
              accept="application/json,.json,.tnreport.json"
              style={{ display: "none" }}
              onChange={handleImportFile}
            />
            {draft.id ? (
              <button type="button" className="btn btn-danger" onClick={handleDelete}>Delete</button>
            ) : null}
          </div>
        )}
      >
        <div className="tn-report-meta-row">
          <label>Template
            <select
              value={selectedId}
              onChange={(e) => {
                const next = e.target.value;
                if (next === "__new__") startNew("blank");
                else if (next) loadTemplate(next);
              }}
            >
              <option value="">Select a template…</option>
              <option value="__new__">+ New template</option>
              {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </label>
          <label>Start from preset
            <select
              value=""
              onChange={(e) => {
                const key = e.target.value;
                if (!key) return;
                startNew(key);
                e.target.value = "";
              }}
              title="Seed a new draft from a built-in template"
            >
              <option value="">+ Use preset…</option>
              {TEMPLATE_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
          </label>
          <label>Template name<input value={draft.name} onChange={(e) => setDraft((p) => ({ ...p, name: e.target.value }))} /></label>
          <label>Description<input value={draft.description} onChange={(e) => setDraft((p) => ({ ...p, description: e.target.value }))} placeholder="Internal description" /></label>
        </div>
      </CollapsibleCard>

      <CollapsibleCard
        id="report-sections"
        className="tn-sections-card"
        title="Sections"
        subtitle={`${sections.length} configured`}
        defaultOpen
        headerRight={(
          <select
            value=""
            onChange={(e) => { if (e.target.value) { addSection(e.target.value); e.target.value = ""; } }}
          >
            <option value="">+ Add section…</option>
            {SECTION_PRESETS.map((p) => <option key={p.type} value={p.type}>{p.label}</option>)}
          </select>
        )}
      >
        {sections.map((section, idx) => (
          <SectionEditor
            key={section.id}
            section={section}
            index={idx}
            total={sections.length}
            gatewayOptions={gatewayOptions}
            tagsByGateway={tagsByGateway}
            formatTagForDisplay={formatTagForDisplay}
            onChange={(patch) => updateSection(section.id, patch)}
            onRemove={() => removeSection(section.id)}
            onMoveUp={() => moveSection(section.id, -1)}
            onMoveDown={() => moveSection(section.id, 1)}
            onNotify={onNotify}
          />
        ))}
        {sections.length === 0 ? (
          <p className="muted">No sections yet. Use <strong>+ Add section</strong> in the header to start.</p>
        ) : null}
      </CollapsibleCard>
    </>
  );

  const rightPane = (
    <CollapsibleCard
      id="report-pdf-preview"
      title="Live PDF preview"
      subtitle={previewState.generatedId ? "Latest render" : "Click Preview PDF to generate"}
      defaultOpen
      headerRight={(
        <div className="tn-report-actions">
          <button
            type="button"
            className="btn btn-success"
            onClick={handlePreview}
            disabled={previewState.rendering}
            title="Render the current template to PDF"
          >
            {previewState.rendering ? "Rendering…" : "Render"}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleDownloadPdf}
            disabled={previewState.rendering}
            title="Download the rendered PDF"
          >
            Download
          </button>
          {previewState.generatedId ? (
            <button
              type="button"
              className="btn btn-link"
              onClick={handleOpenInNewTab}
              title="Open the rendered PDF in a new system viewer"
            >Open externally</button>
          ) : null}
        </div>
      )}
    >
      {previewState.error ? <p className="error-text">{previewState.error}</p> : null}
      {previewUrl ? (
        // <object> first because Chromium's PDF viewer in Electron picks it up
        // more reliably than a bare <iframe>. The fallback <iframe> inside
        // covers browsers where <object> can't host the viewer. Final fallback
        // is a friendly link so users can open the PDF in the OS viewer.
        <object
          key={previewKey}
          data={previewUrl}
          type="application/pdf"
          className="tn-pdf-preview-frame"
          title="Report preview"
        >
          <iframe
            src={previewUrl}
            title="Report preview"
            className="tn-pdf-preview-frame"
            style={{ width: "100%", height: "100%", border: 0 }}
          />
          <div className="tn-pdf-preview-fallback">
            <p className="muted">
              The embedded PDF viewer isn't available here. Use the buttons above to
              <strong> Download</strong> the file or <strong>Open externally</strong>.
            </p>
          </div>
        </object>
      ) : (
        <p className="muted">
          The rendered PDF appears here. Charts and data tables use the same SQL queries the dashboard uses, so values match exactly.
        </p>
      )}
    </CollapsibleCard>
  );

  return (
    <div className="tn-report-designer tn-report-designer-split">
      <SplitPane
        storageKey="trustnode_report_designer_split_pct"
        defaultLeftPct={40}
        minLeft={320}
        minRight={360}
        left={leftPane}
        right={rightPane}
      />
    </div>
  );
}
