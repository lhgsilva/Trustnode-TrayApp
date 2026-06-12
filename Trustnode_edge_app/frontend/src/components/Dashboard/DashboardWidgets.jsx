import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  listReportTemplates,
  listGeneratedReports,
  listScheduledReports,
  runReportTemplateNow,
  openGeneratedReport,
  getReportTemplatePreviewData,
  getGeneratedReportFileUrl,
} from "../../api";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  AreaChart,
  Area,
  BarChart,
  Bar,
  ComposedChart,
  Legend,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  Sector,
  CartesianGrid,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";
import {
  buildFixedText,
  evaluateComputedRules,
  getLatestTagRow,
  getTagSeries as getTagSeriesFiltered,
  toTsMs,
} from "./dashboardAnalytics";
import CloudSyncStatusWidget from "./CloudSyncStatusWidget";

const LAST_WIDGET_DIRECT_STATS_CACHE = new Map();
const LAST_WIDGET_RULE_STATS_CACHE = new Map();
const LAST_WIDGET_ROWS_CACHE = new Map();

function parseNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function parseBool(v, fallback = false) {
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

// Evaluate a small arithmetic expression with single-letter variables (a-z)
// pulled from `env`. Used by the table_list "calculation" advanced columns.
// Refuses anything that looks like JS access (dot, brackets, function calls,
// or non-arithmetic words) so we don't expose globals or DOM APIs to widget
// configuration.
function evalSafeExpression(expr, env = {}) {
  if (typeof expr !== "string" || !expr.trim()) return null;
  if (/[\[\]\{\};]/.test(expr)) return null;
  if (/\b(window|document|globalThis|Function|process|require|import|eval)\b/.test(expr)) return null;
  // Allow letters a-z (vars), digits, dot (decimals), arithmetic, parens, ws.
  if (!/^[\sa-zA-Z0-9_.+\-*/()%]+$/.test(expr)) return null;
  const argNames = Object.keys(env);
  const argValues = argNames.map((k) => (Number.isFinite(env[k]) ? env[k] : null));
  // If any referenced single-letter variable in the expression is null/missing,
  // bail to "-" rather than risk producing NaN that confuses charts.
  for (const ch of expr.matchAll(/[a-zA-Z]/g)) {
    const name = ch[0];
    if (argNames.includes(name) && (env[name] === null || env[name] === undefined)) return null;
  }
  try {
    // eslint-disable-next-line no-new-func
    const fn = new Function(...argNames, `"use strict"; return (${expr});`);
    const result = fn(...argValues);
    return Number.isFinite(Number(result)) ? Number(result) : null;
  } catch (_) {
    return null;
  }
}

function renderEmpty(text = "No data") {
  return <div className="dashboard-widget-empty">{text}</div>;
}

function shadeHex(hex, percent = 0) {
  const raw = String(hex || "").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return hex;
  const num = parseInt(raw, 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  const f = Math.max(-1, Math.min(1, percent));
  const target = f < 0 ? 0 : 255;
  const p = Math.abs(f);
  const nr = Math.round((target - r) * p + r);
  const ng = Math.round((target - g) * p + g);
  const nb = Math.round((target - b) * p + b);
  return `#${[nr, ng, nb].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function rgbaFromHex(hex, alpha = 0.2) {
  const raw = String(hex || "").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return `rgba(20, 168, 154, ${alpha})`;
  const num = parseInt(raw, 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function useCustomColor(widget) {
  return String(widget?.config?.color_mode || "default") === "custom";
}

function getWidgetAccent(widget, fallback = "#14a89a") {
  return useCustomColor(widget) ? (widget?.color || fallback) : fallback;
}

function getChartInterpolation(widget) {
  const raw = String(widget?.config?.interpolation || "stepAfter");
  const allowed = new Set(["stepAfter", "linear", "monotone", "natural", "stepBefore"]);
  return allowed.has(raw) ? raw : "stepAfter";
}

function normalizeRuleOperator(opRaw) {
  const op = String(opRaw || "any").trim().toLowerCase();
  const map = {
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "==": "eq",
    "=": "eq",
    "!=": "ne",
    "<>": "ne",
    any: "any",
    eq: "eq",
    ne: "ne",
    lt: "lt",
    lte: "lte",
    gt: "gt",
    gte: "gte",
    between: "between",
  };
  return map[op] || "any";
}

function bucketMsFromInterval(interval) {
  const map = {
    none: 0,
    "1s": 1000,
    "5s": 5000,
    "10s": 10000,
    "30s": 30000,
    "1m": 60000,
    "5m": 300000,
    "15m": 900000,
    "1h": 3600000,
    "1d": 86400000,
  };
  return Number(map[String(interval || "none")] || 0);
}

function bucketRows(rows, interval) {
  // Legacy "last-write-wins" bucketing — kept for callers (pie/table) that
  // just want the most-recent row per bucket. For chart series that need
  // proper aggregation use bucketAndAggregateRows().
  const bucketMs = bucketMsFromInterval(interval);
  if (!bucketMs) return Array.isArray(rows) ? rows : [];
  const grouped = new Map();
  for (const r of Array.isArray(rows) ? rows : []) {
    const ts = toTsMs(r?.ts || r?.ts_utc);
    if (!Number.isFinite(ts)) continue;
    const key = Math.floor(ts / bucketMs) * bucketMs;
    grouped.set(key, r);
  }
  return Array.from(grouped.values()).sort(
    (a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc)
  );
}

// Bucket rows by `interval` and collapse each bucket to a single row using
// `mode` (first/last/avg/min/max/sum/count). Used by chart widgets so the
// "Group interval" + "Result aggregation" config from the editor actually
// changes what the chart plots — without this the dashboard ignored both
// settings and always rendered raw samples.
function bucketAndAggregateRows(rows, interval, mode) {
  const src = Array.isArray(rows) ? rows : [];
  if (!src.length) return src;
  const bucketMs = bucketMsFromInterval(interval);
  if (!bucketMs) return src;  // "none" — pass through

  const aggKey = String(mode || "last").toLowerCase();
  const groups = new Map();  // bucketKey -> { ts, rows: [], values: [], first, last, valid }
  for (const r of src) {
    const tsMs = toTsMs(r?.ts || r?.ts_utc);
    if (!Number.isFinite(tsMs)) continue;
    const key = Math.floor(tsMs / bucketMs) * bucketMs;
    let g = groups.get(key);
    if (!g) {
      g = { ts: tsMs, last: r, first: r, lastTs: tsMs, firstTs: tsMs, values: [], count: 0 };
      groups.set(key, g);
    }
    const numeric = Number(r?.value);
    if (Number.isFinite(numeric)) g.values.push(numeric);
    g.count += 1;
    if (tsMs > g.lastTs) { g.last = r; g.lastTs = tsMs; }
    if (tsMs < g.firstTs) { g.first = r; g.firstTs = tsMs; }
  }

  const reduce = (vals) => {
    if (!vals.length) return null;
    if (aggKey === "avg" || aggKey === "average" || aggKey === "mean") {
      return vals.reduce((a, b) => a + b, 0) / vals.length;
    }
    if (aggKey === "min") return Math.min(...vals);
    if (aggKey === "max") return Math.max(...vals);
    if (aggKey === "sum") return vals.reduce((a, b) => a + b, 0);
    if (aggKey === "count") return vals.length;
    return null;
  };

  const out = [];
  for (const [key, g] of groups.entries()) {
    let row;
    // Anchor the row to the bucket boundary so the X axis steps cleanly.
    const bucketTsIso = new Date(key + Math.floor(bucketMs / 2)).toISOString();
    if (aggKey === "first") {
      row = { ...g.first, ts: g.first?.ts || bucketTsIso };
    } else if (aggKey === "last" || aggKey === "latest") {
      row = { ...g.last, ts: g.last?.ts || bucketTsIso };
    } else {
      const v = reduce(g.values);
      row = { ...g.last, value: v, ts: bucketTsIso };
    }
    out.push(row);
  }
  return out.sort((a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc));
}

function toLocalInputMs(value) {
  const dt = new Date(String(value || ""));
  return Number.isFinite(dt.getTime()) ? dt.getTime() : Number.NaN;
}

function resolvePresetWindowMs(preset) {
  const map = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
  };
  return Number(map[String(preset || "none")] || 0);
}

function toIsoUtc(value) {
  const txt = String(value || "").trim();
  if (!txt) return "";
  const d = new Date(txt);
  return Number.isFinite(d.getTime()) ? d.toISOString() : "";
}

function resolveTimeFilterRange(cfg) {
  const preset = String(cfg?.query_time_filter_preset || "none");
  if (preset === "none") return null;
  if (preset === "custom") {
    const fromUtc = toIsoUtc(cfg?.query_time_filter_from);
    const toUtc = toIsoUtc(cfg?.query_time_filter_to);
    if (!fromUtc && !toUtc) return null;
    return { fromUtc, toUtc };
  }
  const windowMs = resolvePresetWindowMs(preset);
  if (!windowMs) return null;
  // Stabilize preset windows to avoid re-query storms on every re-render.
  // Short windows roll every 10s; long windows roll every 60s.
  const quantumMs = windowMs >= 60 * 60 * 1000 ? 60 * 1000 : 10 * 1000;
  const anchorMs = Math.floor(Date.now() / quantumMs) * quantumMs;
  const to = new Date(anchorMs);
  const from = new Date(anchorMs - windowMs);
  return { fromUtc: from.toISOString(), toUtc: to.toISOString() };
}

function applyWidgetTimeFilter(rows, cfg) {
  const src = Array.isArray(rows) ? rows : [];
  if (!src.length) return src;
  const preset = String(cfg?.query_time_filter_preset || "none");
  if (preset === "none") return src;
  const rowTsMs = (r) => toTsMs(r?.ts || r?.ts_utc || r?.created_utc || "");
  const latestRowMs = src.reduce((acc, r) => {
    const ts = rowTsMs(r);
    return Number.isFinite(ts) && ts > acc ? ts : acc;
  }, Number.NaN);
  // Preset windows should be stable even when streams are paused or clock-skewed:
  // anchor to latest dataset timestamp whenever available.
  const anchorMs = Number.isFinite(latestRowMs) ? latestRowMs : Date.now();
  let fromMs = Number.NaN;
  let toMs = Number.NaN;
  if (preset === "custom") {
    fromMs = toLocalInputMs(cfg?.query_time_filter_from);
    toMs = toLocalInputMs(cfg?.query_time_filter_to);
  } else {
    const windowMs = resolvePresetWindowMs(preset);
    if (windowMs > 0) fromMs = anchorMs - windowMs;
    toMs = anchorMs;
  }
  const filtered = src.filter((r) => {
    const ts = rowTsMs(r);
    if (!Number.isFinite(ts)) return false;
    if (Number.isFinite(fromMs) && ts < fromMs) return false;
    if (Number.isFinite(toMs) && ts > toMs) return false;
    return true;
  });
  if (filtered.length) return filtered;
  // Safety fallback: if rows exist but timestamps are partially malformed,
  // keep a small recent tail instead of showing a hard-empty widget.
  if (preset !== "custom") return src.slice(-Math.max(1, Math.min(200, src.length)));
  return filtered;
}

function getPiePalette(widget) {
  if (!useCustomColor(widget)) {
    return ["#14a89a", "#0e8479", "#3cd2c2", "#1f3a5f", "#6e8dd2", "#e0a050", "#e2585d", "#a78bfa"];
  }
  const base = widget?.color || "#14a89a";
  return [
    base,
    shadeHex(base, -0.16),
    shadeHex(base, 0.12),
    shadeHex(base, -0.28),
    shadeHex(base, 0.24),
    shadeHex(base, -0.40),
    shadeHex(base, 0.36),
    shadeHex(base, -0.52),
  ];
}

function _formatChartTickTime(ms, formatKey) {
  if (!Number.isFinite(ms)) return "";
  try {
    const d = new Date(ms);
    const opts = (() => {
      switch (String(formatKey || "hh_mm_ss").toLowerCase()) {
        case "hh_mm":
          return { hour: "2-digit", minute: "2-digit", hour12: false };
        case "hh_mm_ss":
          return { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
        case "date_hh_mm":
          return { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
        case "date_hh_mm_ss":
          return { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
        case "full_date_hh_mm":
          return { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
        case "hh_mm_12h":
          return { hour: "numeric", minute: "2-digit", hour12: true };
        default:
          return { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
      }
    })();
    return d.toLocaleTimeString([], opts);
  } catch (_) { return ""; }
}

function buildXAxisProps(series, cfg = {}) {
  const sample = Array.isArray(series) ? series.length : 0;
  const fmt = String(cfg?.chart_x_time_format || "hh_mm_ss");
  const angleRaw = Number(cfg?.chart_x_tick_angle);
  const angle = Number.isFinite(angleRaw) ? angleRaw : 0;
  const rotatedProps = angle === 0
    ? { height: 22 }
    : (() => {
        // Same heights as LiveTagChart so the heavy + light renderers
        // present the same chart footprint at any rotation. Tighter
        // than the earlier 44/60 px steps so the plot area keeps as
        // much vertical room as possible.
        const a = Math.abs(angle);
        const h = a < 35 ? 30 : a < 60 ? 40 : a < 80 ? 48 : 56;
        return {
          angle,
          textAnchor: angle < 0 ? "end" : "start",
          height: h,
        };
      })();
  if (!sample) return { dataKey: "idx", ...rotatedProps };
  const tsMsLookup = new Map();
  let hasReal = false;
  for (const p of series) {
    const raw = String(p?.ts || "");
    if (!raw) continue;
    const ms = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T"));
    if (Number.isFinite(ms)) {
      tsMsLookup.set(p.idx, ms);
      hasReal = true;
    }
  }
  if (hasReal) {
    const tsValues = Array.from(tsMsLookup.values()).sort((a, b) => a - b);
    return {
      dataKey: (row) => tsMsLookup.get(row?.idx) ?? row?.idx ?? 0,
      type: "number",
      scale: "time",
      domain: tsValues.length ? [tsValues[0], tsValues[tsValues.length - 1]] : ["auto", "auto"],
      tickFormatter: (ms) => _formatChartTickTime(ms, fmt),
      minTickGap: 24,
      tick: { fill: "var(--ink-soft, #8a98ab)", fontSize: 11 },
      axisLine: { stroke: "var(--line, rgba(255,255,255,0.07))" },
      tickLine: false,
      ...rotatedProps,
    };
  }
  return {
    dataKey: "idx",
    tickFormatter: (idx) => {
      const hit = series.find((p) => p?.idx === idx);
      const ts = String(hit?.ts || "");
      if (!ts) return String(idx);
      const ms = Date.parse(ts.includes("T") ? ts : ts.replace(" ", "T"));
      if (Number.isFinite(ms)) return _formatChartTickTime(ms, fmt);
      const t = ts.includes("T") ? ts.split("T")[1] : ts.split(" ")[1];
      return String(t || ts).slice(0, 8);
    },
    minTickGap: 18,
    tick: { fill: "var(--ink-soft, #8a98ab)", fontSize: 11 },
    axisLine: { stroke: "var(--line, rgba(255,255,255,0.07))" },
    tickLine: false,
    ...rotatedProps,
  };
}

const yAxisProps = {
  tick: { fill: "var(--ink-soft, #8a98ab)", fontSize: 11 },
  tickFormatter: (v) => {
    const n = Number(v);
    return Number.isFinite(n)
      ? n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : "";
  },
  axisLine: { stroke: "var(--line, rgba(255,255,255,0.07))" },
  tickLine: false,
  width: 58,
};

function buildAutoYDomain(series) {
  // Auto-fit Y axis — same shape Lite uses (5 % top pad, 2 % bottom pad)
  // so the operator gets a tightly framed curve instead of recharts'
  // default 0-anchored padded range. Returning dataMin/dataMax callbacks
  // lets recharts re-evaluate the domain on every data update — which is
  // what makes the chart "follow" the value as it moves up and down,
  // even when the user hasn't picked manual mode.
  const values = (Array.isArray(series) ? series : [])
    .map((p) => Number(p?.value))
    .filter((n) => Number.isFinite(n));
  if (!values.length) return ["auto", "auto"];
  return [
    (dataMin) => {
      const n = Number.isFinite(dataMin) ? dataMin : 0;
      return n - Math.abs(n) * 0.02 - 0.001;
    },
    (dataMax) => {
      const n = Number.isFinite(dataMax) ? dataMax : 1;
      return n + Math.abs(n) * 0.05 + 0.001;
    },
  ];
}

function formatByPreset(value, preset = "auto") {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  switch (String(preset || "auto")) {
    case "int":
      return n.toFixed(0);
    case "2dp":
      return n.toFixed(2);
    case "3dp":
      return n.toFixed(3);
    case "scientific":
      return n.toExponential(2);
    case "auto":
    default:
      return n.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }
}

const chartTooltipProps = {
  contentStyle: {
    background: "var(--bg-card, #111827)",
    border: "1px solid var(--stroke, rgba(255,255,255,0.14))",
    borderRadius: 8,
    color: "var(--ink, #f2f4f7)",
  },
  labelStyle: { color: "var(--ink, #f2f4f7)", fontWeight: 600 },
  itemStyle: { color: "var(--ink, #f2f4f7)" },
};

function renderActiveDonutShape(props) {
  const {
    cx,
    cy,
    innerRadius,
    outerRadius,
    startAngle,
    endAngle,
    fill,
  } = props || {};
  return (
    <g>
      <Sector
        cx={cx}
        cy={cy}
        innerRadius={innerRadius}
        outerRadius={Number(outerRadius || 0) + 6}
        startAngle={startAngle}
        endAngle={endAngle}
        fill={fill}
      />
    </g>
  );
}

function computeWidgetTextScale(widget, minPx, maxPx) {
  // body_text_scale is the universal control exposed by the widget
  // editor for every widget type (divider / fixed_text / table_list /
  // KPI). text_font_scale is the legacy chart-specific control kept for
  // backward compat with saved widgets. We multiply them together when
  // both are set so a divider with body_text_scale=1.5 inside a chart
  // template that also bumped text_font_scale=1.2 ends up at 1.8.
  const bodyScale = Number(widget?.config?.body_text_scale);
  const legacyScale = Number(widget?.config?.text_font_scale);
  const scale = (Number.isFinite(bodyScale) ? bodyScale : 1)
    * (Number.isFinite(legacyScale) ? legacyScale : 1);
  const w = Number(widget?.w || 4);
  const h = Number(widget?.h || 3);
  const areaFactor = Math.max(0.75, Math.min(1.65, Math.sqrt(Math.max(1, (w * h) / 12))));
  return Math.max(minPx, Math.min(maxPx, Math.round(maxPx * scale * areaFactor)));
}

function parseLegendLayout(v, fallback = "side") {
  const t = String(v || "").trim().toLowerCase();
  if (!t) return fallback;
  return t.includes("bottom") ? "bottom" : "side";
}

function sanitizePieHiddenMap(prevMap, pieData) {
  const validKeys = new Set((Array.isArray(pieData) ? pieData : []).map((i) => String(i?.name || "")));
  const next = {};
  for (const [k, v] of Object.entries(prevMap || {})) {
    if (validKeys.has(String(k)) && Boolean(v)) next[String(k)] = true;
  }
  const visibleCount = (Array.isArray(pieData) ? pieData : []).filter((item) => !next[String(item?.name || "")]).length;
  if (visibleCount > 0 || !validKeys.size) return next;
  const first = String((Array.isArray(pieData) ? pieData[0] : null)?.name || "");
  if (first) delete next[first];
  return next;
}

function computeMeterRanges(widget, value) {
  const rules = Array.isArray(widget?.config?.compute_rules) ? widget.config.compute_rules : [];
  const parsed = [];
  for (let idx = 0; idx < rules.length; idx += 1) {
    const r = rules[idx] || {};
    const op = normalizeRuleOperator(r?.operator || "between");
    const v1 = Number(r?.value1);
    const v2 = Number(r?.value2);
    let min = Number.NaN;
    let max = Number.NaN;
    if (op === "between") {
      if (Number.isFinite(v1) && Number.isFinite(v2)) {
        min = Math.min(v1, v2);
        max = Math.max(v1, v2);
      }
    } else if (op === "lt" || op === "lte") {
      if (Number.isFinite(v1)) {
        min = Number.NEGATIVE_INFINITY;
        max = v1;
      }
    } else if (op === "gt" || op === "gte") {
      if (Number.isFinite(v1)) {
        min = v1;
        max = Number.POSITIVE_INFINITY;
      }
    } else if (op === "eq") {
      if (Number.isFinite(v1)) {
        min = v1 - 0.5;
        max = v1 + 0.5;
      }
    }
    if (!Number.isFinite(min) && !Number.isFinite(max)) continue;
    parsed.push({
      id: String(r?.id || `range-${idx}`),
      label: String(r?.label || `Range ${idx + 1}`),
      color: String(r?.color || "#14a89a"),
      min,
      max,
    });
  }
  if (!parsed.length) {
    const v = Number.isFinite(Number(value)) ? Number(value) : 0;
    const floor = Math.floor((v - 10) / 10) * 10;
    const ceil = Math.ceil((v + 10) / 10) * 10;
    const span = Math.max(10, ceil - floor);
    const a = floor;
    const b = floor + span * 0.6;
    const c = floor + span * 0.85;
    return [
      { id: "low", label: "Low", color: "#22c55e", min: a, max: b },
      { id: "normal", label: "Normal", color: "#14a89a", min: b, max: c },
      { id: "high", label: "High", color: "#e2585d", min: c, max: ceil },
    ];
  }
  const finiteMins = parsed.map((r) => r.min).filter((n) => Number.isFinite(n));
  const finiteMaxs = parsed.map((r) => r.max).filter((n) => Number.isFinite(n));
  const cur = Number(value);
  const baseMin = finiteMins.length ? Math.min(...finiteMins) : (Number.isFinite(cur) ? cur - 10 : 0);
  const baseMax = finiteMaxs.length ? Math.max(...finiteMaxs) : (Number.isFinite(cur) ? cur + 10 : 100);
  const domainMin = Math.min(baseMin, Number.isFinite(cur) ? cur : baseMin);
  const domainMax = Math.max(baseMax, Number.isFinite(cur) ? cur : baseMax);
  return parsed
    .map((r) => ({
      ...r,
      min: Number.isFinite(r.min) ? r.min : domainMin,
      max: Number.isFinite(r.max) ? r.max : domainMax,
    }))
    .sort((a, b) => a.min - b.min);
}

function renderPieSliceLabelFactory({ showCount, showPercent }) {
  return function renderPieSliceLabel(payload = {}) {
    const {
      cx,
      cy,
      midAngle,
      outerRadius,
      percent,
      value,
      name,
      fill,
    } = payload;
    const RADIAN = Math.PI / 180;
    const r = Number(outerRadius || 0) + 16;
    const x = Number(cx || 0) + r * Math.cos(-midAngle * RADIAN);
    const y = Number(cy || 0) + r * Math.sin(-midAngle * RADIAN);
    const anchor = x > Number(cx || 0) ? "start" : "end";
    const labelName = String(name || "").trim();
    const valText = showCount ? Number(value || 0).toFixed(2) : "";
    const pctText = showPercent ? `${((Number(percent || 0) || 0) * 100).toFixed(1)}%` : "";
    const lines = [labelName, valText, pctText].filter(Boolean);
    if (!lines.length) return null;
    return (
      <text x={x} y={y} textAnchor={anchor} dominantBaseline="central">
        {lines.map((line, i) => (
          <tspan
            key={`${labelName || "slice"}-${i}`}
            x={x}
            dy={i === 0 ? 0 : 13}
            className={`dashboard-pie-label-line${i + 1}`}
            fill={fill}
          >
            {line}
          </tspan>
        ))}
      </text>
    );
  };
}

// =====================================================================
// LiveTagChart — minimal, reliable live trend renderer.
//
// Design (based on the Grafana Live / ThingsBoard / Ignition pattern
// the research phase confirmed is universal):
//
//   1. ONE seed REST fetch on mount → fills the ring buffer with the
//      last N samples per series from local historian.
//   2. Subscribe to dataLogView (the WebSocket-fed in-memory log the
//      whole app already maintains). On every render, append samples
//      whose ts > last-seen ts to the ring buffer.
//   3. NO heartbeat re-fetches. WebSocket is the only steady-state
//      writer. This kills the race between the per-widget REST fetcher
//      and the WS stream that produced every "loaded then frozen"
//      and "chart blank but historian has data" symptom.
//   4. Reseed once if the WS stream reconnects after a drop (we detect
//      this via a long gap in incoming samples).
//   5. Gap rendering: if delta-t between consecutive ring-buffer
//      samples > pollInterval × 1.5, insert a null so Recharts breaks
//      the line. No internal carry-forward.
//
// Used for the LIVE case — no time range, no grouping. The complex
// fetcher path stays for historical + grouping.
// =====================================================================
function LiveTagChart({
  widget,
  dataLogView,
  fetchWidgetRows,
  gatewayIntervalMs,
  resolvedGatewayId,
  tagName,
  formatTagForDisplay,
}) {
  const cfg = widget?.config || {};
  const widgetType = String(widget?.type || "line_chart");
  const capacity = Math.max(5, Math.min(5000, Number(cfg?.readings_count || 60)));
  // pollMs is now ONLY used as a sanity floor on the per-series gap
  // threshold (when we have fewer than 4 samples to measure cadence).
  // The real gap threshold is derived per series from the median of
  // actual sample deltas — so different gateways with different
  // intervals on the same chart each get a sensible cadence-based
  // threshold automatically.
  const pollMs = Math.max(200, Number(gatewayIntervalMs || 1000));

  // Series definitions: primary + non-limit extras. Recomputed only when
  // the operator changes the tag or extras (cheap memo key).
  const seriesDefs = useMemo(() => {
    const out = [];
    if (tagName) {
      out.push({
        id: "primary",
        gatewayId: String(resolvedGatewayId || cfg.gateway_id || ""),
        tagName: String(tagName),
        label: String(widget?.title || tagName).trim() || tagName,
        color: String(widget?.color || "#14a89a"),
        multiplier: Number.isFinite(Number(cfg.multiplier)) && Number(cfg.multiplier) !== 0
          ? Number(cfg.multiplier) : 1,
        offset: Number.isFinite(Number(cfg.offset)) ? Number(cfg.offset) : 0,
        unit: String(cfg.primary_unit || ""),
        suffix: String(cfg.primary_suffix || ""),
        axis: "left",
        chartKind: widgetType === "bar_chart" ? "bar"
          : widgetType === "line_area_chart" ? "area"
          : "line",
        lineWidth: 0,   // 0 means "use widget-wide chart_line_width"
        lineDot: "",    // "" means "use widget-wide chart_line_dot"
        barWidth: 0,
      });
    }
    const extras = Array.isArray(cfg.series_extra) ? cfg.series_extra : [];
    const palette = ["#14a89a", "#f97316", "#3b82f6", "#a855f7", "#22c55e", "#eab308"];
    extras.forEach((s, i) => {
      if (!s || !s.tag_name) return;
      if (String(s.chart_type || "").toLowerCase() === "limit") return;
      out.push({
        id: String(s.id || `extra_${i}`),
        gatewayId: String(s.gateway_id || resolvedGatewayId || ""),
        tagName: String(s.tag_name),
        label: String(s.label || s.tag_name),
        color: String(s.color || palette[(i + 1) % palette.length]),
        multiplier: Number.isFinite(Number(s.multiplier)) && Number(s.multiplier) !== 0
          ? Number(s.multiplier) : 1,
        offset: Number.isFinite(Number(s.offset)) ? Number(s.offset) : 0,
        unit: String(s.unit || ""),
        suffix: String(s.suffix || ""),
        axis: String(s.axis || "left").toLowerCase() === "right" ? "right" : "left",
        chartKind: (() => {
          const t = String(s.chart_type || "").toLowerCase();
          if (t === "bar") return "bar";
          if (t === "area") return "area";
          if (t === "line") return "line";
          // fall back to widget type
          return widgetType === "bar_chart" ? "bar"
            : widgetType === "line_area_chart" ? "area"
            : "line";
        })(),
        lineWidth: Number(s.line_width || 0),
        lineDot: String(s.line_dot || ""),
        barWidth: Number(s.bar_width || 0),
      });
    });
    return out;
  }, [
    widget?.title,
    widget?.color,
    widgetType,
    resolvedGatewayId,
    tagName,
    cfg.multiplier,
    cfg.offset,
    cfg.primary_unit,
    cfg.primary_suffix,
    cfg.gateway_id,
    JSON.stringify(cfg.series_extra || []),
    // Style knobs that affect what each series looks like even though
    // they don't change the IDENTITY of the series (gw/tag). Including
    // them here lets the chart pick up live edits without reseeding.
    cfg.chart_line_width,
    cfg.chart_line_dot,
    cfg.chart_show_legend,
    cfg.chart_show_point_labels,
    cfg.chart_value_format,
    cfg.interpolation,
    cfg.y_axis_label,
    cfg.y_axis_right_label,
    cfg.y_axis_mode,
    cfg.y_min,
    cfg.y_max,
    cfg.y_tick_step,
    cfg.y_right_axis_mode,
    cfg.y_right_min,
    cfg.y_right_max,
    cfg.y_right_tick_step,
  ]);

  // Ring buffers — one Map per series. Map<tsMs, scaledValue> so dedupe
  // is automatic. We never mutate; we replace the ref on each update so
  // the render derivation can rely on identity changes.
  const buffersRef = useRef(null);
  // Track the highest ts we've ingested per series so we can append
  // only strictly newer WS rows.
  const lastSeenTsRef = useRef(null);
  // Identity-keyed last seen timestamp for detecting WS drops + reseed.
  const lastIngestWallclockRef = useRef(Date.now());
  // Force re-render tick. Bumped whenever ring buffer changes.
  const [tick, setTick] = useState(0);
  const [seedError, setSeedError] = useState("");
  const [seedReady, setSeedReady] = useState(false);

  // CRITICAL: fetchWidgetRows is an inline async function the parent
  // recreates on every render. If we put it in any useEffect's dep
  // array directly, the seed effect re-runs forever — buffers reset,
  // "Loading..." flashes back, the chart never gets to render the
  // accumulated samples. We stash the latest callable in a ref and
  // read from there inside effects; only the SERIES IDENTITY (which
  // is stable) drives the seed.
  const fetchRowsRef = useRef(fetchWidgetRows);
  useEffect(() => { fetchRowsRef.current = fetchWidgetRows; }, [fetchWidgetRows]);
  // Same treatment for the dataLogView snapshot used by the stall
  // detector. The ingest effect reads dataLogView directly (it must,
  // to fire on new samples) but anything inside an interval timer
  // must go through the ref to stay current without re-binding the
  // timer.
  const dataLogViewRef = useRef(dataLogView);
  useEffect(() => { dataLogViewRef.current = dataLogView; });
  // Track the latest seriesDefs in a ref so effects can read the
  // current value without taking it as a dep (which would re-bind on
  // every parent render and reset every timer / cause flickering).
  const seriesDefsRef = useRef(seriesDefs);
  useEffect(() => { seriesDefsRef.current = seriesDefs; });

  // Reset buffers + reseed when the set of series identity changes.
  const seriesKey = useMemo(
    () => seriesDefs.map((s) => `${s.gatewayId}|${s.tagName}`).join("||"),
    [seriesDefs]
  );

  // Time-grouping configuration. Declared HERE (before the seed
  // effect) so the seed effect's dependency array can read them
  // without a temporal dead zone — the JSX evaluation order matters
  // even though the effect body itself runs later. The renderedData
  // useMemo below also reads these; it executes after this block so
  // it's never in TDZ.
  const groupKey = String(cfg.query_group_interval || "none").toLowerCase();
  const groupBucketMs = (() => {
    const map = {
      none: 0,
      "1s": 1000, "5s": 5000, "10s": 10000, "30s": 30000,
      "1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000,
    };
    return map[groupKey] || 0;
  })();
  const reducerKey = String(cfg.query_result_aggregation || "last").toLowerCase();

  useEffect(() => {
    const fresh = new Map();
    const fresh2 = new Map();
    for (const s of seriesDefs) {
      fresh.set(s.id, new Map());
      fresh2.set(s.id, -Infinity);
    }
    buffersRef.current = fresh;
    lastSeenTsRef.current = fresh2;
    setSeedReady(false);
    setSeedError("");
    setTick((t) => t + 1);

    const fetcher = fetchRowsRef.current;
    if (!seriesDefs.length || typeof fetcher !== "function") {
      setSeedReady(true);
      return undefined;
    }
    // Bucket-aware fetch size: when grouping is on we need
    // approximately `capacity × bucket_size / poll_ms` raw rows so
    // bucketAndAggregateRows can yield `capacity` filled buckets.
    // Hard ceiling of 5000 to keep the SQLite read snappy.
    const seedLimit = (() => {
      if (!groupBucketMs) return capacity;
      const rowsPerBucket = Math.max(1, Math.ceil(groupBucketMs / pollMs));
      return Math.min(5000, capacity * rowsPerBucket);
    })();
    // Seed each series in parallel. Local SQLite read for last N rows
    // is fast; even 4 series × 60 rows = 240 rows total comes back in
    // well under 200 ms on a populated edge.
    let cancelled = false;
    Promise.all(seriesDefs.map(async (s) => {
      try {
        const rows = await fetcher({
          fromUtc: "",
          toUtc: "",
          limit: seedLimit,
          offset: 0,
          gateway: s.gatewayId || "",
          tag: s.tagName,
          timeoutMs: 8000,
          maxAttempts: 2,
        });
        if (cancelled) return;
        const buf = buffersRef.current?.get(s.id);
        if (!buf) return;
        let newest = -Infinity;
        for (const r of Array.isArray(rows) ? rows : []) {
          const tsMs = Date.parse(String(r?.ts || r?.ts_utc || ""));
          const raw = Number(r?.value);
          if (!Number.isFinite(tsMs) || !Number.isFinite(raw)) continue;
          buf.set(tsMs, raw * s.multiplier + s.offset);
          if (tsMs > newest) newest = tsMs;
        }
        if (newest > -Infinity) {
          lastSeenTsRef.current?.set(s.id, newest);
        }
      } catch (err) {
        if (!cancelled) setSeedError(String(err?.message || err));
      }
    })).then(() => {
      if (!cancelled) {
        setSeedReady(true);
        setTick((t) => t + 1);
      }
    });
    return () => { cancelled = true; };
  // Re-seed when series identity, capacity, or grouping changes.
  // groupBucketMs / pollMs affect how many raw rows we need to pull
  // so a different bucket size DOES require a fresh fetch — but
  // changing the reducer doesn't (we can re-bucket existing rows).
  // The fetcher is read from a ref so its unstable identity doesn't
  // restart the seed on every parent render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seriesKey, capacity, groupBucketMs, pollMs]);

  // Ingest WS samples: append rows from dataLogView whose ts is strictly
  // newer than the last seen ts for that series. Runs on every parent
  // re-render — that's cheap because lastSeenTs makes the per-series
  // scan O(new rows) and the ring buffer cap stays bounded.
  useEffect(() => {
    if (!seedReady || !seriesDefs.length || !Array.isArray(dataLogView) || dataLogView.length === 0) return;
    const buffers = buffersRef.current;
    const lastSeen = lastSeenTsRef.current;
    if (!buffers || !lastSeen) return;
    // Read seriesDefs from the ref so this effect doesn't re-bind on
    // every parent render (which would just reset the loop counter
    // without actually changing what we ingest).
    const seriesNow = seriesDefsRef.current || [];
    let didAppend = false;
    for (const s of seriesNow) {
      const buf = buffers.get(s.id);
      if (!buf) continue;
      const last = lastSeen.get(s.id) || -Infinity;
      let newest = last;
      // dataLogView is sorted newest-first by mergeHistorianRowsStable.
      for (const r of dataLogView) {
        const tag = String(r?.tag || r?.tag_name || "");
        if (tag !== s.tagName) continue;
        // Match by gateway_id OR fall through if not strict (tag-only widgets).
        const gid = String(r?.gateway_id || "");
        if (s.gatewayId && gid && gid !== s.gatewayId) continue;
        const tsMs = Date.parse(String(r?.ts || r?.ts_utc || ""));
        if (!Number.isFinite(tsMs) || tsMs <= last) continue;
        const raw = Number(r?.value);
        if (!Number.isFinite(raw)) continue;
        buf.set(tsMs, raw * s.multiplier + s.offset);
        if (tsMs > newest) newest = tsMs;
        didAppend = true;
      }
      if (newest > last) {
        lastSeen.set(s.id, newest);
        lastIngestWallclockRef.current = Date.now();
      }
      // Cap ring buffer at 2× capacity so we always have a small tail
      // for gap detection without unbounded memory growth.
      if (buf.size > capacity * 2) {
        const sorted = [...buf.keys()].sort((a, b) => a - b);
        for (let i = 0; i < sorted.length - capacity * 2; i += 1) buf.delete(sorted[i]);
      }
    }
    if (didAppend) setTick((t) => t + 1);
  // seriesDefs intentionally omitted; we read its current value via
  // the ref. dataLogView and seedReady are the only signals that drive
  // ingest.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataLogView, seedReady, capacity]);

  // Re-seed when the WS appears to have stalled — heuristic: if no new
  // sample has hit the buffer for 3× the poll interval (and we ARE
  // ready), the stream is probably reconnecting. Re-pull the missing
  // tail so we recover without the operator opening DevTools.
  // seriesDefsRef + fetchRowsRef declared above keep this interval from
  // re-binding on every parent render.
  useEffect(() => {
    if (!seedReady) return undefined;
    const fetcher = fetchRowsRef.current;
    if (!seriesDefsRef.current.length || typeof fetcher !== "function") return undefined;
    const checkMs = Math.max(2000, pollMs * 3);
    const id = setInterval(() => {
      const stalledFor = Date.now() - (lastIngestWallclockRef.current || 0);
      if (stalledFor < pollMs * 3) return;
      // Stall: fetch the tail since the last seen ts for each series.
      const lastSeen = lastSeenTsRef.current;
      if (!lastSeen) return;
      const currentSeries = seriesDefsRef.current;
      const currentFetcher = fetchRowsRef.current;
      currentSeries.forEach(async (s) => {
        try {
          const since = lastSeen.get(s.id) || 0;
          const rows = await currentFetcher({
            fromUtc: since > 0 ? new Date(since).toISOString() : "",
            toUtc: "",
            limit: capacity,
            offset: 0,
            gateway: s.gatewayId || "",
            tag: s.tagName,
            timeoutMs: 6000,
            maxAttempts: 1,
          });
          const buf = buffersRef.current?.get(s.id);
          if (!buf) return;
          let newest = lastSeen.get(s.id) || -Infinity;
          for (const r of Array.isArray(rows) ? rows : []) {
            const tsMs = Date.parse(String(r?.ts || r?.ts_utc || ""));
            const raw = Number(r?.value);
            if (!Number.isFinite(tsMs) || !Number.isFinite(raw)) continue;
            if (tsMs <= newest) continue;
            buf.set(tsMs, raw * s.multiplier + s.offset);
            if (tsMs > newest) newest = tsMs;
          }
          if (newest > (lastSeen.get(s.id) || -Infinity)) {
            lastSeen.set(s.id, newest);
            lastIngestWallclockRef.current = Date.now();
            setTick((t) => t + 1);
          }
        } catch (_) { /* will retry on next stall tick */ }
      });
    }, checkMs);
    return () => clearInterval(id);
  }, [seedReady, pollMs, capacity]);

  // (groupKey / groupBucketMs / reducerKey are declared above before
  // the seed effect to avoid a temporal dead zone in its dep array.)

  // Build the rendered dataset: union of timestamps across all series,
  // optional bucketing/aggregation, last `capacity` rows, with null
  // gap inserts. O(N log N).
  const renderedData = useMemo(() => {
    const buffers = buffersRef.current;
    if (!buffers) return { rows: [], hasRightAxis: false };

    // Per-series effective entries: when grouping is on, replace each
    // series with a Map of bucketStart→reducedValue. When off, use the
    // raw ring-buffer map.
    const reduce = (vals) => {
      if (!vals.length) return null;
      if (reducerKey === "avg" || reducerKey === "mean" || reducerKey === "average") {
        return vals.reduce((a, b) => a + b, 0) / vals.length;
      }
      if (reducerKey === "min") return Math.min(...vals);
      if (reducerKey === "max") return Math.max(...vals);
      if (reducerKey === "sum") return vals.reduce((a, b) => a + b, 0);
      if (reducerKey === "first") return vals[0];
      if (reducerKey === "count") return vals.length;
      // default "last"
      return vals[vals.length - 1];
    };
    const perSeries = new Map();
    for (const s of seriesDefs) {
      const buf = buffers.get(s.id);
      if (!buf) { perSeries.set(s.id, new Map()); continue; }
      if (!groupBucketMs) {
        perSeries.set(s.id, buf);
        continue;
      }
      const grouped = new Map(); // bucketTs (center) → reduced value
      const tmp = new Map();    // bucketStart → [values...] sorted by ts
      const sortedKeys = [...buf.keys()].sort((a, b) => a - b);
      for (const ts of sortedKeys) {
        const bucketStart = Math.floor(ts / groupBucketMs) * groupBucketMs;
        if (!tmp.has(bucketStart)) tmp.set(bucketStart, []);
        tmp.get(bucketStart).push(buf.get(ts));
      }
      for (const [bucketStart, vals] of tmp.entries()) {
        const center = bucketStart + Math.floor(groupBucketMs / 2);
        const r = reduce(vals);
        if (Number.isFinite(r)) grouped.set(center, r);
      }
      perSeries.set(s.id, grouped);
    }

    // Union of timestamps across every series.
    const tsSet = new Set();
    for (const s of seriesDefs) {
      const m = perSeries.get(s.id);
      if (!m) continue;
      for (const ts of m.keys()) tsSet.add(ts);
    }
    let sorted = [...tsSet].sort((a, b) => a - b);
    if (sorted.length > capacity) sorted = sorted.slice(-capacity);

    // Per-series sorted entries so we can carry-forward by walking with
    // a pointer. Operator request 2026-06-11: "the chart should print
    // the last reading in the historian, should not matter if the last
    // readings were once every 2 second or once a day". Carry-forward
    // gives a continuous line even when one series ticks faster than
    // another; we DO break the line at real downtime gaps (see below).
    const perSeriesSorted = new Map();
    for (const s of seriesDefs) {
      const m = perSeries.get(s.id);
      if (!m) { perSeriesSorted.set(s.id, []); continue; }
      const entries = [...m.entries()].sort((a, b) => a[0] - b[0]);
      perSeriesSorted.set(s.id, entries);
    }

    // Per-series "natural cadence" = median delta between adjacent
    // samples. We use 5× that as the gap threshold so a series that
    // genuinely ticks once a day doesn't trigger a gap; a series that
    // normally ticks at 2 s only breaks the line if it stops for 10 s+.
    // Operator's other point: "different gateways with different
    // intervals" — so we derive this PER SERIES from the data itself,
    // not from any single gateway interval.
    const gapByIdMs = new Map();
    for (const s of seriesDefs) {
      const entries = perSeriesSorted.get(s.id) || [];
      if (entries.length < 4) {
        // Not enough history to estimate — be lenient: 10 × pollMs as
        // a floor so single-shot tags don't get false breaks.
        gapByIdMs.set(s.id, Math.max(60_000, pollMs * 10));
        continue;
      }
      const deltas = [];
      for (let i = 1; i < entries.length; i += 1) {
        const d = entries[i][0] - entries[i - 1][0];
        if (d > 0) deltas.push(d);
      }
      if (!deltas.length) {
        gapByIdMs.set(s.id, Math.max(60_000, pollMs * 10));
        continue;
      }
      deltas.sort((a, b) => a - b);
      const median = deltas[Math.floor(deltas.length / 2)];
      // 5× median, with a 5 s minimum so 1 s-interval noise doesn't
      // trip the detector on a single missed sample.
      gapByIdMs.set(s.id, Math.max(5000, median * 5));
    }
    // Bucket grouping override: when grouping is on, anything bigger
    // than 1.5× the bucket size is a gap.
    if (groupBucketMs > 0) {
      for (const s of seriesDefs) gapByIdMs.set(s.id, groupBucketMs * 1.5);
    }

    // Per-series carry-forward walker.
    const walkers = seriesDefs.map((s) => ({
      def: s,
      entries: perSeriesSorted.get(s.id) || [],
      ptr: 0,
      lastValue: null,
      lastTs: -Infinity,
    }));

    const rows = [];
    for (let i = 0; i < sorted.length; i += 1) {
      const tsMs = sorted[i];
      const row = { idx: rows.length + 1, tsMs, ts: new Date(tsMs).toISOString() };
      for (const w of walkers) {
        while (w.ptr < w.entries.length && w.entries[w.ptr][0] <= tsMs) {
          w.lastValue = w.entries[w.ptr][1];
          w.lastTs = w.entries[w.ptr][0];
          w.ptr += 1;
        }
        // Carry-forward: if THIS series has emitted at-or-before tsMs,
        // and that sample isn't older than the per-series gap
        // threshold, use it. Otherwise null (line breaks).
        const ageMs = tsMs - w.lastTs;
        const cap = gapByIdMs.get(w.def.id) || (pollMs * 10);
        row[w.def.id] = (w.lastTs > -Infinity && ageMs <= cap) ? w.lastValue : null;
      }
      rows.push(row);
    }
    const hasRightAxis = seriesDefs.some((s) => s.axis === "right");
    return { rows, hasRightAxis };
    // tick changes are the signal that buffers mutated.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, seriesDefs, capacity, pollMs, groupBucketMs, reducerKey]);

  // Y axis: auto (data-fit) or manual. When manual is ON but the
  // operator has not yet typed values, we still pin the axis to a
  // sensible range so the toggle has an immediate visible effect:
  //   - missing min → 0 (industrial values almost always start there)
  //   - missing max → "auto" callback that hugs the data top
  // That way the user sees the axis snap to a 0-anchored frame the
  // moment they flip the toggle, then types min/max/step to refine.
  const buildManualDomain = (modeKey, minKey, maxKey, stepKey) => {
    if (String(cfg[modeKey] || "auto").toLowerCase() !== "manual") return null;
    const loRaw = cfg[minKey];
    const hiRaw = cfg[maxKey];
    const loProvided = loRaw !== "" && loRaw !== null && loRaw !== undefined
      && Number.isFinite(Number(loRaw));
    const hiProvided = hiRaw !== "" && hiRaw !== null && hiRaw !== undefined
      && Number.isFinite(Number(hiRaw));
    // When lo is provided, honor it verbatim (positive 100, negative -50,
    // anything goes). Earlier we forced "lo = max(lo, 0)" for non-negative
    // values, which trashed the operator's explicit min when they typed
    // e.g. min=100 max=200 — the axis painted 0..200 because lo got
    // snapped to 0. The lo<0?lo:0 heuristic was meant only for the
    // PARTIAL case where the operator left min blank (so we sensibly
    // anchor at 0 instead of plotting somewhere weird).
    const lo = loProvided ? Number(loRaw) : 0;
    const hi = hiProvided ? Number(hiRaw) : null;
    if (hi === null) {
      // Partial: operator typed only the max (or only the toggle).
      // Anchor lo to 0 unless they typed a negative value; let the
      // chart's auto-domain compute the top.
      return { lo: loProvided ? lo : 0, hi: null, ticks: undefined, partial: true };
    }
    if (hi <= lo) return null;
    const step = Number(cfg[stepKey]);
    let ticks;
    if (Number.isFinite(step) && step > 0) {
      ticks = [];
      const maxTicks = 50;
      for (let v = lo, i = 0; v <= hi + step * 1e-9 && i < maxTicks; v += step, i += 1) {
        ticks.push(Number(v.toFixed(10)));
      }
    }
    return { lo, hi, ticks, partial: false };
  };
  const manualY = buildManualDomain("y_axis_mode", "y_min", "y_max", "y_tick_step");
  const manualYRight = buildManualDomain("y_right_axis_mode", "y_right_min", "y_right_max", "y_right_tick_step");
  const autoDomain = [
    (dataMin) => {
      const n = Number.isFinite(dataMin) ? dataMin : 0;
      return n - Math.abs(n) * 0.02 - 0.001;
    },
    (dataMax) => {
      const n = Number.isFinite(dataMax) ? dataMax : 1;
      return n + Math.abs(n) * 0.05 + 0.001;
    },
  ];
  // domain: if manual & both values known → fixed; if manual & only lo
  // → pin lo, let recharts auto-fit hi via the same callback used in
  // auto mode; if auto → both callbacks.
  const buildDomain = (m) => {
    if (!m) return autoDomain;
    if (m.partial) return [m.lo, autoDomain[1]];
    return [m.lo, m.hi];
  };
  const yDomainLeft = buildDomain(manualY);
  const yDomainRight = buildDomain(manualYRight);

  // Numeric format preset matches the heavy widget: int / 2dp / 3dp /
  // scientific / auto. Applied to tooltip values + optional point labels.
  const chartValueFormat = String(cfg.chart_value_format || "auto");
  const formatNumber = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    switch (chartValueFormat) {
      case "int": return n.toFixed(0);
      case "2dp": return n.toFixed(2);
      case "3dp": return n.toFixed(3);
      case "scientific": return n.toExponential(2);
      case "auto":
      default: return n.toLocaleString(undefined, { maximumFractionDigits: 3 });
    }
  };

  // Axis labels (rotated 90° inside the chart) — same as the heavy
  // widget so saved widgets look identical after the routing change.
  const primaryAxisLabel = String(cfg.y_axis_label || cfg.primary_unit || "");
  const rightAxisLabel = String(cfg.y_axis_right_label || "");

  // Style knobs the editor exposes — legend, point labels, line width,
  // line dot. Per-series overrides on extras win at render time; the
  // widget-level values are used as defaults.
  const showLegend = cfg.chart_show_legend === true || seriesDefs.length > 1;
  const showPointLabels = cfg.chart_show_point_labels === true;
  const widgetLineWidth = (() => {
    const n = Number(cfg.chart_line_width);
    return Number.isFinite(n) && n > 0 ? n : 2;
  })();
  const widgetDotPreset = String(cfg.chart_line_dot || "none");
  const dotByPreset = { none: 0, small: 2, medium: 4, large: 6 };
  const dotForSeries = (color, perSeriesDot) => {
    const preset = perSeriesDot && perSeriesDot !== "" ? perSeriesDot : widgetDotPreset;
    const r = dotByPreset[preset] || 0;
    if (!r) return false;
    return { r, fill: color, stroke: color, strokeWidth: 0 };
  };
  const interpolation = (() => {
    const t = String(cfg.interpolation || "monotone").toLowerCase();
    if (["linear", "monotone", "step", "stepafter", "stepbefore", "basis", "natural"].includes(t)) {
      if (t === "stepafter") return "stepAfter";
      if (t === "stepbefore") return "stepBefore";
      return t;
    }
    return "monotone";
  })();

  // Time format for X axis tick labels. Defaults to HH:MM:SS 24-hour
  // (industrial convention). Operator can pick HH:MM, HH:MM:SS, or
  // include the date in the editor.
  const xTimeFormat = String(cfg.chart_x_time_format || "hh_mm_ss").toLowerCase();
  const formatTickTime = (d) => {
    if (!Number.isFinite(d.getTime())) return "";
    const opts = (() => {
      switch (xTimeFormat) {
        case "hh_mm":
          return { hour: "2-digit", minute: "2-digit", hour12: false };
        case "hh_mm_ss":
          return { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
        case "date_hh_mm":
          return { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
        case "date_hh_mm_ss":
          return { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
        case "full_date_hh_mm":
          return { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
        case "hh_mm_12h":
          return { hour: "numeric", minute: "2-digit", hour12: true };
        default:
          return { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
      }
    })();
    return d.toLocaleTimeString([], opts);
  };
  const xTickFormatter = (v) => {
    const r = renderedData.rows.find((p) => p.idx === v);
    if (!r) return "";
    return formatTickTime(new Date(r.tsMs));
  };
  // X axis tick rotation. Empty / 0 = horizontal (default). 45 / 90 /
  // -45 / -90 supported. Bottom margin auto-expands when rotated to
  // give the labels room.
  const xTickAngleRaw = Number(cfg.chart_x_tick_angle);
  const xTickAngle = Number.isFinite(xTickAngleRaw) ? xTickAngleRaw : 0;
  const labelFmt = (v) => {
    const r = renderedData.rows.find((p) => p.idx === v);
    if (!r) return String(v);
    return new Date(r.tsMs).toLocaleString();
  };
  const fmtVal = (v, name) => {
    if (v == null || !Number.isFinite(Number(v))) return ["—", name];
    const s = seriesDefs.find((x) => x.id === name) || seriesDefs.find((x) => x.label === name);
    const base = formatNumber(v);
    if (s?.suffix) return [`${base}${s.suffix}`, s.label];
    if (s?.unit) return [`${base} ${s.unit}`, s.label];
    return [base, s?.label || name];
  };
  // The XAxis `height` prop (set on the <XAxis /> below) already
  // reserves enough space INSIDE the plot area for rotated labels.
  // Earlier we ALSO added a matching margin.bottom here, which made
  // recharts double-reserve the bottom strip — the chart shrunk and
  // a wide black gap appeared underneath the tick labels. Now the
  // margin only reserves the small legend gap; the axis handles its
  // own label space.
  const margin = {
    top: 4,
    right: rightAxisLabel ? 32 : 8,
    left: primaryAxisLabel ? 12 : 0,
    bottom: showLegend ? 4 : 0,
  };

  if (seedError && renderedData.rows.length === 0) {
    return (
      <div className="dashboard-widget-block dashboard-widget-block-chart">
        <div className="dashboard-widget-empty warn">
          Historian fetch failed: {seedError.slice(0, 140)}
        </div>
      </div>
    );
  }
  if (!seedReady) {
    return (
      <div className="dashboard-widget-block dashboard-widget-block-chart">
        <div className="dashboard-widget-empty muted">Loading…</div>
      </div>
    );
  }
  if (renderedData.rows.length === 0) {
    return (
      <div className="dashboard-widget-block dashboard-widget-block-chart">
        <div className="dashboard-widget-empty muted">
          No points yet — waiting for the gateway to publish samples for{" "}
          <code>{tagName || "(no tag)"}</code>.
        </div>
      </div>
    );
  }
  return (
    <div className="dashboard-widget-block dashboard-widget-block-chart">
      <div className="dashboard-widget-chart">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={renderedData.rows} margin={margin}>
            <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
            <XAxis
              dataKey="idx"
              tickFormatter={xTickFormatter}
              fontSize={10}
              interval="preserveStartEnd"
              angle={xTickAngle}
              textAnchor={xTickAngle === 0 ? "middle" : (xTickAngle < 0 ? "end" : "start")}
              /* Heights tuned so even the longest format (YYYY-MM-DD HH:MM,
                 ~16 chars at fontSize 10 ≈ 80 px rendered, projected
                 vertically at 90° ≈ 80 px) fits without the card's plot
                 area collapsing. 0° flat → tiny strip. 30/45° → mid. 60°+
                 → full vertical span. Stays under what we previously
                 reserved with margin.bottom + height combined. */
              height={(() => {
                const a = Math.abs(xTickAngle);
                if (a === 0) return 22;
                if (a < 35) return 30;
                if (a < 60) return 40;
                if (a < 80) return 48;
                return 56;
              })()}
            />
            <YAxis
              yAxisId="left"
              domain={yDomainLeft}
              ticks={manualY?.ticks}
              allowDataOverflow={!!manualY}
              tickFormatter={formatNumber}
              fontSize={10}
              label={primaryAxisLabel
                ? { value: primaryAxisLabel, angle: -90, position: "insideLeft", fill: "var(--ink-soft, #8a98ab)", fontSize: 11 }
                : undefined}
            />
            {renderedData.hasRightAxis ? (
              <YAxis
                yAxisId="right"
                orientation="right"
                domain={yDomainRight}
                ticks={manualYRight?.ticks}
                allowDataOverflow={!!manualYRight}
                tickFormatter={formatNumber}
                fontSize={10}
                label={rightAxisLabel
                  ? { value: rightAxisLabel, angle: 90, position: "insideRight", fill: "var(--ink-soft, #8a98ab)", fontSize: 11 }
                  : undefined}
              />
            ) : null}
            <Tooltip labelFormatter={labelFmt} formatter={fmtVal} />
            {showLegend ? <Legend wrapperStyle={{ fontSize: 11 }} /> : null}
            {seriesDefs.map((s) => {
              const yId = s.axis === "right" ? "right" : "left";
              // Per-series style overrides win when set; fall through to the
              // widget-wide defaults pulled out at the top of the render.
              const perSeriesLine = Number(s.lineWidth);
              const lineStrokeWidth = Number.isFinite(perSeriesLine) && perSeriesLine > 0
                ? perSeriesLine
                : widgetLineWidth;
              const dotProp = dotForSeries(s.color, s.lineDot);
              const labelProp = showPointLabels
                ? { fill: "var(--ink-soft, #8a98ab)", fontSize: 10, formatter: (v) => formatNumber(v) }
                : false;
              const common = {
                key: s.id,
                dataKey: s.id,
                name: s.label,
                yAxisId: yId,
                stroke: s.color,
                isAnimationActive: false,
                connectNulls: false,
              };
              if (s.chartKind === "bar") {
                const barProps = { ...common, fill: s.color };
                if (Number.isFinite(Number(s.barWidth)) && Number(s.barWidth) > 0) {
                  barProps.barSize = Number(s.barWidth);
                }
                return <Bar {...barProps} label={labelProp} />;
              }
              if (s.chartKind === "area") {
                return (
                  <Area
                    {...common}
                    type={interpolation}
                    fill={s.color + "33"}
                    strokeWidth={lineStrokeWidth}
                    dot={dotProp}
                    label={labelProp}
                  />
                );
              }
              return (
                <Line
                  {...common}
                  type={interpolation}
                  strokeWidth={lineStrokeWidth}
                  dot={dotProp}
                  label={labelProp}
                />
              );
            })}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function DashboardWidgetCard({
  widget,
  dataLogView,
  tagRows,
  tagRowsByGateway,
  formatTagForDisplay,
  gatewayIntervalsById = {},
  gatewaysIndex = null,
  fetchWidgetRows,
  fetchWidgetStats,
  fetchWidgetRuleStats,
  historicalMode = false,
  historicalFromLocal = "",
  historicalToLocal = "",
  onHistoricalPan = null,
}) {
  // Drag-to-scroll: when the dashboard is in Historical mode, the chart
  // gets a "grab" cursor. Pressing and dragging horizontally pans the shared
  // window. The drag uses window-level listeners so Recharts' own SVG
  // pointer handling (tooltips, hover) doesn't swallow the events.
  const panDragStartXRef = useRef(null);
  const panAccumDxRef = useRef(0);
  const PAN_THRESHOLD_PX = 40;
  const isPannableChart = ["line_chart", "line_area_chart", "bar_chart"].includes(String(widget?.type || ""));
  const panEnabled = isPannableChart && typeof onHistoricalPan === "function" && historicalMode === true;

  const onPanPointerDown = (e) => {
    if (!panEnabled) return;
    if (e.button !== undefined && e.button !== 0) return;
    panDragStartXRef.current = e.clientX;
    panAccumDxRef.current = 0;
    // Stop Recharts (or any child) from starting its own gesture so the
    // window-level listeners reliably see the rest of the drag.
    try { e.preventDefault(); } catch (_) {}
    try { e.stopPropagation(); } catch (_) {}

    // Use BOTH mouse and pointer events so we cover every browser/touch case.
    const onWinMove = (ev) => {
      if (panDragStartXRef.current === null) return;
      const newDx = ev.clientX - panDragStartXRef.current;
      const delta = newDx - panAccumDxRef.current;
      if (Math.abs(delta) >= PAN_THRESHOLD_PX) {
        onHistoricalPan(delta > 0 ? 1 : -1);
        panAccumDxRef.current = newDx;
      }
    };
    const onWinUp = () => {
      panDragStartXRef.current = null;
      panAccumDxRef.current = 0;
      window.removeEventListener("pointermove", onWinMove);
      window.removeEventListener("pointerup", onWinUp);
      window.removeEventListener("pointercancel", onWinUp);
      window.removeEventListener("mousemove", onWinMove);
      window.removeEventListener("mouseup", onWinUp);
    };
    window.addEventListener("pointermove", onWinMove);
    window.addEventListener("pointerup", onWinUp);
    window.addEventListener("pointercancel", onWinUp);
    window.addEventListener("mousemove", onWinMove);
    window.addEventListener("mouseup", onWinUp);
  };
  const onPanPointerMove = () => {};
  const onPanPointerUp = () => {};
  const cfg = widget?.config || {};
  const gatewayId = cfg.gateway_id || "";
  const tagName = cfg.tag_name || "";
  const resolvedGatewayId = useMemo(() => {
    const raw = String(gatewayId || "").trim();
    // Path 1: the saved gateway_id is still alive somewhere in the
    // historian rows the dashboard already fetched. Cheapest hit.
    if (raw && Object.prototype.hasOwnProperty.call(tagRowsByGateway || {}, raw)) return raw;
    // Path 2: index check — does the current gateway list still have a
    // gateway with this id? If yes, use it; the historian rows just
    // haven't streamed in yet.
    const idx = gatewaysIndex || null;
    const knownIds = idx && Array.isArray(idx.ids) ? new Set(idx.ids) : null;
    if (raw && knownIds && knownIds.has(raw)) return raw;
    // Path 3 (self-heal by tag name): the saved gateway_id is gone (deleted
    // + recreated, scope shifted, etc.). If a currently-configured gateway
    // has this widget's tag in its tags list, point the widget at that
    // gateway so the chart keeps updating instead of going blank. This is
    // the operator-visible "same IP + tag = same device" recovery.
    const tagKey = String(tagName || "").trim();
    if (tagKey && idx && idx.byTag && idx.byTag[tagKey]) {
      return String(idx.byTag[tagKey]);
    }
    // Path 4 (backward-compat): old widgets stored gateway_name instead
    // of gateway_id. Try to find a gateway that owns rows with this name.
    if (raw) {
      for (const [gid, rows] of Object.entries(tagRowsByGateway || {})) {
        const sampleName = String(rows?.[0]?.gateway_name || "").trim();
        if (sampleName && sampleName === raw) return String(gid || "").trim();
      }
    }
    return raw;
  }, [gatewayId, tagName, tagRowsByGateway, gatewaysIndex]);
  const dataSourceType = String(cfg.data_source_type || "tag_direct");
  const computedCapable = ["pie_chart", "meter_chart", "table_list", "fixed_text", "value_kpi", "text_kpi"].includes(String(widget?.type || ""));
  const normalizedDataSourceType = computedCapable ? dataSourceType : "tag_direct";
  const rules = Array.isArray(cfg.compute_rules) ? cfg.compute_rules : [];
  const effectiveDataSourceType =
    normalizedDataSourceType === "computed" && (!Array.isArray(rules) || rules.length === 0)
      ? "tag_direct"
      : normalizedDataSourceType;
  const rulesDepKey = useMemo(
    () =>
      JSON.stringify(
        (Array.isArray(rules) ? rules : []).map((r) => ({
          id: String(r?.id || ""),
          label: String(r?.label || ""),
          gateway_id: String(r?.gateway_id || ""),
          tag_name: String(r?.tag_name || ""),
          operator: String(r?.operator || "any"),
          value1: r?.value1 ?? "",
          value2: r?.value2 ?? "",
          aggregation: String(r?.aggregation || "count"),
          color: String(r?.color || ""),
        }))
      ),
    [rules]
  );
  const [serverQueryRows, setServerQueryRows] = useState(null);
  const [lastGoodServerQueryRows, setLastGoodServerQueryRows] = useState(null);
  const [serverQueryError, setServerQueryError] = useState("");
  const [serverQueryStats, setServerQueryStats] = useState(null);
  const [lastGoodServerQueryStats, setLastGoodServerQueryStats] = useState(() => {
    const wid = String(widget?.id || "");
    return LAST_WIDGET_DIRECT_STATS_CACHE.get(wid) || null;
  });
  const [serverQueryStatsError, setServerQueryStatsError] = useState("");
  const [serverQueryRowsLoading, setServerQueryRowsLoading] = useState(false);
  const [serverQueryStatsLoading, setServerQueryStatsLoading] = useState(false);
  const [serverRuleStats, setServerRuleStats] = useState(null);
  const [lastGoodServerRuleStats, setLastGoodServerRuleStats] = useState(() => {
    const wid = String(widget?.id || "");
    return LAST_WIDGET_RULE_STATS_CACHE.get(wid) || null;
  });
  const [serverRuleStatsError, setServerRuleStatsError] = useState("");
  const [serverRuleStatsLoading, setServerRuleStatsLoading] = useState(false);
  const [queryRefreshTick, setQueryRefreshTick] = useState(0);
  // The widget heartbeat follows the configured gateway poll interval so the
  // dashboard refresh cadence matches the rate at which fresh PLC samples land
  // in the historian. Falls back to 1000ms when no gateway is selected/known.
  const gatewayIntervalMs = useMemo(() => {
    const raw = Number(gatewayIntervalsById?.[String(resolvedGatewayId || "")] || 0);
    return Number.isFinite(raw) && raw > 0 ? Math.max(200, raw) : 1000;
  }, [gatewayIntervalsById, resolvedGatewayId]);
  // All widget refreshes fire on every gateway cycle (1:1 cadence). The in-flight
  // guards downstream skip a new request whenever a previous one is still running,
  // so this never stampedes the backend — it just keeps the UI in lockstep with
  // the PLC poll rate the user configured.
  const refreshTickFast = useMemo(() => Number(queryRefreshTick || 0), [queryRefreshTick]);
  const refreshTickMedium = refreshTickFast;
  const refreshTickSlow = refreshTickFast;
  const refreshTickPaginated = refreshTickFast;
  const queryRowsReqKeyRef = useRef("");
  const queryStatsReqKeyRef = useRef("");
  const ruleStatsReqKeyRef = useRef("");
  // In-flight guards: prevent stampede when SQL latency > refresh tick.
  // A new tick fires another effect run; without the guard we'd cancel
  // the still-running request and never see its rows in state.
  const queryRowsInFlightRef = useRef(false);
  const queryStatsInFlightRef = useRef(false);
  const ruleStatsInFlightRef = useRef(false);
  const fetchWidgetRowsRef = useRef(fetchWidgetRows);
  const fetchWidgetStatsRef = useRef(fetchWidgetStats);
  const fetchWidgetRuleStatsRef = useRef(fetchWidgetRuleStats);
  const widgetType = String(widget?.type || "");
  const cfgReadingsCount = Number(cfg?.readings_count || 120);
  const cfgRowSelection = String(cfg?.query_row_selection || "all");
  const cfgRowLimit = Number(cfg?.query_row_limit || 200);
  const cfgGroupInterval = String(cfg?.query_group_interval || "none");
  // Default aggregation for chart widgets is "last" (use the most recent
  // value in each bucket). Previously this defaulted to "count" which
  // made the chart suddenly show tiny integers (sample counts) the
  // moment the operator picked a non-"none" grouping — "grouping was
  // broken" per the operator report. count only makes sense for
  // rule-based widgets that genuinely want sample frequencies; chart
  // widgets always want a real numeric reduction of the values.
  const _isChartWidgetTypeForAggDefault = ["line_chart", "line_area_chart", "bar_chart", "value_kpi", "meter_chart"].includes(String(widget?.type || ""));
  const cfgResultAggregation = String(
    cfg?.query_result_aggregation
    || (_isChartWidgetTypeForAggDefault ? "last" : "count")
  );
  const cfgRuleLogic = String(cfg?.query_rule_logic || "any");
  const cfgTimePreset = String(cfg?.query_time_filter_preset || "none");
  const cfgTimeFrom = String(cfg?.query_time_filter_from || "");
  const cfgTimeTo = String(cfg?.query_time_filter_to || "");

  useEffect(() => {
    fetchWidgetRowsRef.current = fetchWidgetRows;
  }, [fetchWidgetRows]);
  useEffect(() => {
    fetchWidgetStatsRef.current = fetchWidgetStats;
  }, [fetchWidgetStats]);
  useEffect(() => {
    fetchWidgetRuleStatsRef.current = fetchWidgetRuleStats;
  }, [fetchWidgetRuleStats]);

  useEffect(() => {
    // Heartbeat cadence matches the gateway's configured poll interval. When the
    // gateway changes (or interval is reconfigured) we restart the timer so the
    // widget tracks the new cadence without waiting for the previous tick.
    //
    // Bump the tick once immediately on mount/restart so the extra-series
    // fetcher (and every other effect keyed on refreshTickFast) fires NOW
    // instead of after a full gateway interval. Previously a widget that
    // mounted while the gateway interval was, say, 5 s sat blank for up
    // to 5 s before showing any data — operators read that as a 30 s
    // startup hang because they were running multi-second poll rates.
    setQueryRefreshTick((v) => v + 1);
    const timer = setInterval(() => {
      setQueryRefreshTick((v) => v + 1);
    }, gatewayIntervalMs);
    return () => clearInterval(timer);
  }, [gatewayIntervalMs]);

  // Safety guard: never leave widget sections in perpetual loading if an in-flight
  // request was cancelled by a rapid parent re-render.
  useEffect(() => {
    if (!serverQueryRowsLoading) return () => {};
    const t = setTimeout(() => setServerQueryRowsLoading(false), 25000);
    return () => clearTimeout(t);
  }, [serverQueryRowsLoading]);
  useEffect(() => {
    if (!serverQueryStatsLoading) return () => {};
    const t = setTimeout(() => setServerQueryStatsLoading(false), 25000);
    return () => clearTimeout(t);
  }, [serverQueryStatsLoading]);
  useEffect(() => {
    if (!serverRuleStatsLoading) return () => {};
    const t = setTimeout(() => setServerRuleStatsLoading(false), 25000);
    return () => clearTimeout(t);
  }, [serverRuleStatsLoading]);

  useEffect(() => {
    // When the dashboard is in HISTORICAL mode the global from/to date
    // range owns the chart's data window. The per-widget time filter
    // (cfg.query_time_filter_*) used to be the only signal the fetcher
    // looked at, which meant switching the dashboard to Historical
    // mode and picking a date range had ZERO effect on the chart —
    // the widget kept fetching its own "live tail" and the filter
    // bar above it lied. Now historical from/to wins when set.
    let range = resolveTimeFilterRange(cfg);
    if (historicalMode) {
      const histFromMs = historicalFromLocal ? new Date(historicalFromLocal).getTime() : NaN;
      const histToMs = historicalToLocal ? new Date(historicalToLocal).getTime() : NaN;
      const fromIso = Number.isFinite(histFromMs) ? new Date(histFromMs).toISOString() : "";
      const toIso = Number.isFinite(histToMs) ? new Date(histToMs).toISOString() : "";
      if (fromIso || toIso) range = { fromUtc: fromIso, toUtc: toIso };
    }
    // No-time-filter must still query historian DB (from/to empty),
    // otherwise widgets fall back to in-memory live buffer and counts drift.
    const canQuery = typeof fetchWidgetRowsRef.current === "function";
    const isComputedPie = widgetType === "pie_chart" && String(effectiveDataSourceType || "tag_direct") === "computed";
    const isDirectPie = widgetType === "pie_chart" && String(effectiveDataSourceType || "tag_direct") === "tag_direct";
    if (!canQuery) {
      setServerQueryRows(null);
      setLastGoodServerQueryRows(null);
      setServerQueryError("");
      setServerQueryRowsLoading(false);
      queryRowsReqKeyRef.current = "";
      return () => {};
    }
    // Direct pie widgets read from historian stats endpoint; avoid heavy full-row scans.
    if (isDirectPie && typeof fetchWidgetStatsRef.current === "function") {
      setServerQueryRows([]);
      setServerQueryError("");
      setServerQueryRowsLoading(false);
      queryRowsReqKeyRef.current = "";
      return () => {};
    }
    if (isComputedPie && typeof fetchWidgetRuleStatsRef.current === "function") {
      setServerQueryRows([]);
      setServerQueryError("");
      setServerQueryRowsLoading(false);
      queryRowsReqKeyRef.current = "";
      return () => {};
    }
    const isTrendWidget = ["line_chart", "line_area_chart", "bar_chart", "table_list"].includes(widgetType);
    const shouldPaginateAll = Boolean(
      cfgRowSelection === "all" &&
      (
        (isComputedPie && typeof fetchWidgetRuleStatsRef.current !== "function") ||
        isDirectPie
      )
    );
    const rowSelection =
      isTrendWidget && cfgRowSelection === "all"
        ? "last_n"
        : cfgRowSelection;
    // Compute requested fetch size SO THAT after bucketing the chart
    // ends up with at least cfgReadingsCount buckets. Without this,
    // picking grouping="1m" + readings=20 produced 1 bucket (the chart
    // went blank) because the fetcher only pulled 80 raw rows.
    // Approach: rows_needed ≈ readings * (bucket_size_ms / poll_ms).
    // The poll interval is the gateway's effective interval (defaults
    // to 1 s when unknown). When grouping is "none" we keep the
    // previous behaviour (4× readings, floor 400).
    const computeBucketAwareLimit = () => {
      if (!isTrendWidget) return cfgRowLimit;
      const groupKey = String(cfgGroupInterval || "none").toLowerCase();
      const bucketMs = bucketMsFromInterval(groupKey);
      if (!bucketMs) {
        return Math.max(cfgRowLimit, cfgReadingsCount * 4, 400);
      }
      const pollMs = Math.max(200, Number(gatewayIntervalMs || 1000));
      const rowsPerBucket = Math.max(1, Math.ceil(bucketMs / pollMs));
      const projected = cfgReadingsCount * rowsPerBucket;
      // Hard ceilings: never punish the server with > 200k rows; never
      // round down below the operator's explicit row_limit.
      return Math.max(cfgRowLimit, projected, 400);
    };
    const requestedLimit = Math.min(200000, computeBucketAwareLimit());
    // Params-only key (no tick). Polling re-fires won't cancel an in-flight fetch
    // when SQL latency exceeds the refresh interval.
    const paramsKey = JSON.stringify({
      gatewayId: String(resolvedGatewayId || ""),
      tagName: String(tagName || ""),
      fromUtc: String(range?.fromUtc || ""),
      toUtc: String(range?.toUtc || ""),
      rowSel: rowSelection,
      rowLimit: requestedLimit,
      reads: cfgReadingsCount,
      grp: cfgGroupInterval,
      agg: cfgResultAggregation,
      logic: cfgRuleLogic,
      tf: cfgTimePreset,
      tfFrom: cfgTimeFrom,
      tfTo: cfgTimeTo,
    });
    const paramsChanged = paramsKey !== queryRowsReqKeyRef.current;
    if (!paramsChanged && queryRowsInFlightRef.current) {
      return () => {};
    }
    queryRowsReqKeyRef.current = paramsKey;
    queryRowsInFlightRef.current = true;
    const chartReads = cfgReadingsCount;
    const rowTagFilter =
      isComputedPie
        ? (() => {
            const ruleTags = Array.from(
              new Set(
                (Array.isArray(rules) ? rules : [])
                  .map((r) => String(r?.tag_name || "").trim())
                  .filter(Boolean)
              )
            );
            if (ruleTags.length === 1) return ruleTags[0];
            if (ruleTags.length === 0) return String(tagName || "");
            return "";
          })()
        : String(tagName || "");
    const wideScanLimit =
      rowSelection === "all"
        ? 5000
        : Math.max(requestedLimit, chartReads * 8);
    const limit = shouldPaginateAll
      ? Math.max(1000, Math.min(5000, Math.max(requestedLimit, chartReads * 10, 2000)))
      : Math.max(50, Math.min(5000, wideScanLimit));
    setServerQueryError("");
    setServerQueryRowsLoading(true);
    (async () => {
      try {
        let rows = [];
        if (!shouldPaginateAll) {
          rows = await fetchWidgetRowsRef.current({
            fromUtc: range?.fromUtc || "",
            toUtc: range?.toUtc || "",
            limit,
            offset: 0,
            gateway: String(resolvedGatewayId || ""),
            tag: rowTagFilter,
            timeoutMs: 12000,
            maxAttempts: 2,
          });
          const safeFirst = Array.isArray(rows) ? rows : [];
          // Backward-compat fallback: some saved widgets may carry stale gateway ids.
          // If scoped fetch returns empty, retry by tag-only scope.
          if (safeFirst.length === 0 && String(resolvedGatewayId || "").trim()) {
            rows = await fetchWidgetRowsRef.current({
              fromUtc: range?.fromUtc || "",
              toUtc: range?.toUtc || "",
              limit,
              offset: 0,
              gateway: "",
              tag: rowTagFilter,
              timeoutMs: 12000,
              maxAttempts: 2,
            });
          }
        } else {
          const all = [];
          let offset = 0;
          // Keep correctness first for computed rules:
          // paginate until exhaustion with a high safety cap.
          const maxRows = rowSelection === "all"
            ? 1000000
            : Math.max(5000, Math.min(250000, requestedLimit * 50 || 50000));
          while (offset < maxRows) {
            const page = await fetchWidgetRowsRef.current({
              fromUtc: range?.fromUtc || "",
              toUtc: range?.toUtc || "",
              limit,
              offset,
              gateway: String(resolvedGatewayId || ""),
              tag: rowTagFilter,
              timeoutMs: 12000,
              maxAttempts: 2,
            });
            let pageRows = Array.isArray(page) ? page : [];
            if (
              pageRows.length === 0 &&
              offset === 0 &&
              String(resolvedGatewayId || "").trim()
            ) {
              const fallbackPage = await fetchWidgetRowsRef.current({
                fromUtc: range?.fromUtc || "",
                toUtc: range?.toUtc || "",
                limit,
                offset,
                gateway: "",
                tag: rowTagFilter,
                timeoutMs: 12000,
                maxAttempts: 2,
              });
              pageRows = Array.isArray(fallbackPage) ? fallbackPage : [];
            }
            all.push(...pageRows);
            if (queryRowsReqKeyRef.current !== paramsKey || pageRows.length < limit) break;
            offset += limit;
            if (all.length >= maxRows) break;
          }
          rows = all.length > maxRows ? all.slice(0, maxRows) : all;
        }
        if (queryRowsReqKeyRef.current !== paramsKey) return;
        const safeRows = Array.isArray(rows) ? rows : [];
        setServerQueryRows(safeRows);
        if (safeRows.length > 0) {
          setLastGoodServerQueryRows(safeRows);
          const wid = String(widget?.id || "");
          if (wid) LAST_WIDGET_ROWS_CACHE.set(wid, safeRows);
        }
        setServerQueryError("");
      } catch (err) {
        if (queryRowsReqKeyRef.current !== paramsKey) return;
        setServerQueryError(String(err?.message || err || "widget_sql_query_failed"));
      } finally {
        queryRowsInFlightRef.current = false;
        setServerQueryRowsLoading(false);
      }
    })();
    return () => {
      // No-op: polling re-fires deliberately don't cancel in-flight queries.
    };
  }, [
    resolvedGatewayId,
    tagName,
    widgetType,
    dataSourceType,
    cfgReadingsCount,
    cfgRowSelection,
    cfgRowLimit,
    cfgGroupInterval,
    cfgResultAggregation,
    cfgRuleLogic,
    cfgTimePreset,
    cfgTimeFrom,
    cfgTimeTo,
    historicalMode,
    historicalFromLocal,
    historicalToLocal,
    rulesDepKey,
    refreshTickFast,
    refreshTickMedium,
    refreshTickPaginated,
  ]);

  useEffect(() => {
    const isPieDirect = widgetType === "pie_chart" && String(effectiveDataSourceType || "tag_direct") === "tag_direct";
    const canQuery = typeof fetchWidgetStatsRef.current === "function";
    // When the operator picked a non-"none" group_interval, the server
    // stats endpoint can't honour it (no group_interval param exists), so
    // we skip the server call and let the client-side fallback at
    // localDirectStats compute the bucketed aggregation properly.
    const hasGrouping = String(cfgGroupInterval || "none") !== "none";
    if (!isPieDirect || !canQuery || hasGrouping) {
      setServerQueryStats(null);
      setLastGoodServerQueryStats(null);
      setServerQueryStatsError("");
      setServerQueryStatsLoading(false);
      queryStatsReqKeyRef.current = "";
      return () => {};
    }
    const range = resolveTimeFilterRange(cfg);
    // Params-only key (no tick) so polling never cancels an in-flight request.
    const paramsKey = JSON.stringify({
      widgetId: String(widget?.id || ""),
      gatewayId: String(resolvedGatewayId || ""),
      tagName: String(tagName || ""),
      fromUtc: String(range?.fromUtc || ""),
      toUtc: String(range?.toUtc || ""),
      grp: cfgGroupInterval,
      agg: String(cfgResultAggregation || "count"),
      sel: cfgRowSelection,
      lim: cfgRowLimit,
      tf: cfgTimePreset,
      tfFrom: cfgTimeFrom,
      tfTo: cfgTimeTo,
    });
    const paramsChanged = paramsKey !== queryStatsReqKeyRef.current;
    if (!paramsChanged && queryStatsInFlightRef.current) {
      return () => {};
    }
    queryStatsReqKeyRef.current = paramsKey;
    queryStatsInFlightRef.current = true;
    setServerQueryStatsError("");
    setServerQueryStatsLoading(true);
    (async () => {
      try {
        let rows = await fetchWidgetStatsRef.current({
          fromUtc: range?.fromUtc || "",
          toUtc: range?.toUtc || "",
          gateway: String(resolvedGatewayId || ""),
          tag: String(tagName || ""),
          timeoutMs: 12000,
          maxAttempts: 2,
        });
        const safeFirst = Array.isArray(rows) ? rows : [];
        if (safeFirst.length === 0 && String(resolvedGatewayId || "").trim()) {
          rows = await fetchWidgetStatsRef.current({
            fromUtc: range?.fromUtc || "",
            toUtc: range?.toUtc || "",
            gateway: "",
            tag: String(tagName || ""),
            timeoutMs: 12000,
            maxAttempts: 2,
          });
        }
        if (queryStatsReqKeyRef.current !== paramsKey) return;
        const safeRows = Array.isArray(rows) ? rows : [];
        setServerQueryStats(safeRows);
        if (safeRows.length > 0) {
          setLastGoodServerQueryStats(safeRows);
          const wid = String(widget?.id || "");
          if (wid) LAST_WIDGET_DIRECT_STATS_CACHE.set(wid, safeRows);
        }
        setServerQueryStatsError("");
      } catch (err) {
        if (queryStatsReqKeyRef.current !== paramsKey) return;
        setServerQueryStatsError(String(err?.message || err || "widget_sql_stats_query_failed"));
      } finally {
        queryStatsInFlightRef.current = false;
        setServerQueryStatsLoading(false);
      }
    })();
    return () => {
      // No-op: polling re-fires intentionally don't cancel in-flight queries.
    };
  }, [
    widget?.id,
    dataSourceType,
    resolvedGatewayId,
    tagName,
    widgetType,
    cfgGroupInterval,
    cfgResultAggregation,
    cfgRowSelection,
    cfgRowLimit,
    cfgTimePreset,
    cfgTimeFrom,
    cfgTimeTo,
    refreshTickMedium,
  ]);

  useEffect(() => {
    const isComputedPie = widgetType === "pie_chart" && String(effectiveDataSourceType || "tag_direct") === "computed";
    const canQuery = typeof fetchWidgetRuleStatsRef.current === "function";
    if (!isComputedPie || !canQuery) {
      setServerRuleStats(null);
      setLastGoodServerRuleStats(null);
      setServerRuleStatsError("");
      setServerRuleStatsLoading(false);
      ruleStatsReqKeyRef.current = "";
      return () => {};
    }
    const range = resolveTimeFilterRange(cfg);
    // Compute "params key" — excludes the refresh tick on purpose. The tick
    // only drives polling; if params haven't changed and a request is already
    // in flight we let it finish instead of cancelling and re-firing.
    const paramsKey = JSON.stringify({
      widgetId: String(widget?.id || ""),
      gatewayId: String(resolvedGatewayId || ""),
      fromUtc: String(range?.fromUtc || ""),
      toUtc: String(range?.toUtc || ""),
      rowSel: cfgRowSelection,
      rowLimit: cfgRowLimit,
      grp: cfgGroupInterval,
      agg: cfgResultAggregation,
      logic: cfgRuleLogic,
      tf: cfgTimePreset,
      tfFrom: cfgTimeFrom,
      tfTo: cfgTimeTo,
      rules: Array.isArray(rules)
        ? rules.map((r) => ({
            id: String(r?.id || ""),
            label: String(r?.label || ""),
            gateway_id: String(r?.gateway_id || ""),
            tag_name: String(r?.tag_name || ""),
            operator: String(r?.operator || "any"),
            value1: r?.value1 ?? "",
            value2: r?.value2 ?? "",
            aggregation: String(r?.aggregation || "count"),
          }))
        : [],
    });
    const paramsChanged = paramsKey !== ruleStatsReqKeyRef.current;
    // Skip starting a new fetch when a previous request with the same params
    // is still running — avoids cancellation stampede when SQL is slow.
    if (!paramsChanged && ruleStatsInFlightRef.current) {
      return () => {};
    }
    ruleStatsReqKeyRef.current = paramsKey;
    ruleStatsInFlightRef.current = true;
    setServerRuleStatsError("");
    setServerRuleStatsLoading(true);
    (async () => {
      try {
        const normalizedRules = (Array.isArray(rules) ? rules : []).map((r) => ({
          ...r,
          operator: normalizeRuleOperator(r?.operator),
        }));
        const rows = await fetchWidgetRuleStatsRef.current({
          rules: normalizedRules,
          fromUtc: range?.fromUtc || "",
          toUtc: range?.toUtc || "",
          gateway: String(resolvedGatewayId || ""),
          timeoutMs: 12000,
          maxAttempts: 2,
        });
        // Stale-response check: if widget params changed while we were waiting
        // (e.g. user picked a new time range), this result is now invalid.
        if (ruleStatsReqKeyRef.current !== paramsKey) return;
        const safeRows = Array.isArray(rows) ? rows : [];
        setServerRuleStats(safeRows);
        if (safeRows.length > 0) {
          setLastGoodServerRuleStats(safeRows);
          const wid = String(widget?.id || "");
          if (wid) LAST_WIDGET_RULE_STATS_CACHE.set(wid, safeRows);
        }
        setServerRuleStatsError("");
      } catch (err) {
        if (ruleStatsReqKeyRef.current !== paramsKey) return;
        setServerRuleStatsError(String(err?.message || err || "widget_sql_rule_stats_query_failed"));
      } finally {
        ruleStatsInFlightRef.current = false;
        setServerRuleStatsLoading(false);
      }
    })();
    return () => {
      // No-op cleanup: we deliberately do NOT cancel the in-flight fetch on
      // re-render. Stale responses are filtered by the paramsKey check above.
    };
  }, [
    widget?.id,
    dataSourceType,
    resolvedGatewayId,
    widgetType,
    cfgGroupInterval,
    cfgResultAggregation,
    cfgRowSelection,
    cfgRowLimit,
    cfgRuleLogic,
    cfgTimePreset,
    cfgTimeFrom,
    cfgTimeTo,
    rulesDepKey,
    refreshTickMedium,
  ]);

  const effectiveRows = useMemo(() => {
    const hasHistorianFetcher = typeof fetchWidgetRowsRef.current === "function";
    if (!hasHistorianFetcher) return Array.isArray(dataLogView) ? dataLogView : [];
    // Resolve the historian / cached row set first.
    let baseRows = null;
    if (Array.isArray(serverQueryRows) && !serverQueryError) {
      baseRows = serverQueryRows;
    } else if (Array.isArray(lastGoodServerQueryRows) && lastGoodServerQueryRows.length > 0) {
      baseRows = lastGoodServerQueryRows;
    } else {
      const wid = String(widget?.id || "");
      if (wid && Array.isArray(LAST_WIDGET_ROWS_CACHE.get(wid))) {
        baseRows = LAST_WIDGET_ROWS_CACHE.get(wid);
      }
    }
    if (!baseRows) baseRows = [];
    // Operator-reported failure mode: header value updates every gateway
    // tick (it reads dataLogView at parent re-render rate), but the
    // CHART only moves when serverQueryRows comes back from the heartbeat
    // fetch — so the line stayed frozen between fetches even though the
    // value at the top of the card had already moved.
    // Fix: top up baseRows with any live broadcast samples NEWER than
    // baseRows' newest ts. Each new row in dataLogView is real PLC data,
    // so appending it is safe and keeps the chart moving in lockstep
    // with the header value.
    const live = Array.isArray(dataLogView) ? dataLogView : [];
    if (live.length === 0) return baseRows;
    // Find baseRows' newest ts so we only append strictly fresher rows.
    let newestTs = -Infinity;
    for (let i = 0; i < baseRows.length; i += 1) {
      const ts = Date.parse(String(baseRows[i]?.ts || baseRows[i]?.ts_utc || ""));
      if (Number.isFinite(ts) && ts > newestTs) newestTs = ts;
    }
    const append = [];
    for (let i = 0; i < live.length; i += 1) {
      const r = live[i];
      const ts = Date.parse(String(r?.ts || r?.ts_utc || ""));
      if (Number.isFinite(ts) && ts > newestTs) append.push(r);
    }
    return append.length ? baseRows.concat(append) : baseRows;
  }, [serverQueryRows, serverQueryError, lastGoodServerQueryRows, dataLogView, widget?.id]);

  const directScopedRows = useMemo(
    () => {
      const strict = effectiveRows
        .filter((r) => (!resolvedGatewayId || String(r?.gateway_id || "") === String(resolvedGatewayId)))
        .filter((r) => (!tagName || String(r?.tag || r?.tag_name || "") === String(tagName)));
      if (strict.length > 0) return strict;
      // Backward-compat fallback: if saved gateway ids drifted, keep chart usable by tag scope.
      if (tagName) {
        return effectiveRows.filter((r) => String(r?.tag || r?.tag_name || "") === String(tagName));
      }
      return strict;
    },
    [effectiveRows, resolvedGatewayId, tagName]
  );
  const directScopedRowsTimeFiltered = useMemo(
    () => applyWidgetTimeFilter(directScopedRows, cfg),
    [directScopedRows, cfgTimePreset, cfgTimeFrom, cfgTimeTo]
  );
  const computedRowsTimeFiltered = useMemo(
    () => applyWidgetTimeFilter(effectiveRows, cfg),
    [effectiveRows, cfgTimePreset, cfgTimeFrom, cfgTimeTo]
  );

  const latestRaw = useMemo(
    () => getLatestTagRow(directScopedRowsTimeFiltered, resolvedGatewayId, tagName),
    [directScopedRowsTimeFiltered, resolvedGatewayId, tagName]
  );
  // KPI / meter widgets show the most-recent value scaled by the widget's
  // multiplier + offset (same rule chart series use). Done by cloning the
  // row so downstream renderers don't have to know about scaling.
  const latest = useMemo(() => {
    if (!latestRaw) return latestRaw;
    const mul = Number(cfg?.multiplier);
    const off = Number(cfg?.offset);
    const m = Number.isFinite(mul) && mul !== 0 ? mul : 1;
    const o = Number.isFinite(off) ? off : 0;
    if (m === 1 && o === 0) return latestRaw;
    const v = Number(latestRaw?.last_value);
    if (!Number.isFinite(v)) return latestRaw;
    return { ...latestRaw, last_value: v * m + o };
  }, [latestRaw, cfg?.multiplier, cfg?.offset]);
  // 1) time-filter (already done in directScopedRowsTimeFiltered)
  // 2) optionally bucket-and-aggregate per query_group_interval +
  //    query_result_aggregation so the editor's Grouping + Aggregation
  //    selectors actually change what the chart plots
  // 3) extract the (ts, value) tuples for the configured tag
  // 4) apply per-widget multiplier + offset so users can rescale a raw
  //    sensor value (e.g. mV -> V, °C -> °F) without changing the source
  const directScopedRowsAggregated = useMemo(
    () => bucketAndAggregateRows(directScopedRowsTimeFiltered, cfgGroupInterval, cfgResultAggregation),
    [directScopedRowsTimeFiltered, cfgGroupInterval, cfgResultAggregation]
  );
  const primaryMultiplier = useMemo(() => {
    const m = Number(cfg?.multiplier);
    return Number.isFinite(m) && m !== 0 ? m : 1;
  }, [cfg?.multiplier]);
  const primaryOffset = useMemo(() => {
    const o = Number(cfg?.offset);
    return Number.isFinite(o) ? o : 0;
  }, [cfg?.offset]);
  const series = useMemo(() => {
    const raw = getTagSeriesFiltered(directScopedRowsAggregated, resolvedGatewayId, tagName, cfgReadingsCount);
    const scaled = (primaryMultiplier === 1 && primaryOffset === 0)
      ? raw
      : raw.map((p) => {
          const v = Number(p?.value);
          return { ...p, value: Number.isFinite(v) ? v * primaryMultiplier + primaryOffset : p?.value };
        });
    // Insert null rows at gateway-down gaps so the chart line BREAKS
    // visibly instead of drawing a diagonal across hours of downtime.
    // Threshold = 3 × gateway poll interval (anything longer than that
    // means the PLC stopped reporting, not just normal jitter).
    if (scaled.length < 2) return scaled;
    const tickMs = Math.max(500, Number(gatewayIntervalMs || 1000));
    const gapThresholdMs = tickMs * 3;
    const out = [];
    for (let i = 0; i < scaled.length; i += 1) {
      const cur = scaled[i];
      out.push(cur);
      const next = scaled[i + 1];
      if (!next) continue;
      const a = Date.parse(String(cur.ts || ""));
      const b = Date.parse(String(next.ts || ""));
      if (Number.isFinite(a) && Number.isFinite(b) && (b - a) > gapThresholdMs) {
        out.push({ idx: cur.idx + 0.5, ts: new Date(a + Math.floor((b - a) / 2)).toISOString(), value: null });
      }
    }
    return out;
  }, [directScopedRowsAggregated, resolvedGatewayId, tagName, cfgReadingsCount, primaryMultiplier, primaryOffset, gatewayIntervalMs]);

  // -------- multi-series support --------------------------------------------
  // Additional series for trend charts. Each extra entry is rendered alongside
  // the primary tag using ComposedChart so users can overlay e.g. temperature
  // (left, °C) with pressure (right, bar). Single-series widgets continue to
  // work — extraSeriesDefs is empty for them.
  // All non-primary series, regardless of kind (data trace or limit line).
  const allExtraSeries = useMemo(() => {
    const isChartWidget = ["line_chart", "line_area_chart", "bar_chart"].includes(widgetType);
    if (!isChartWidget) return [];
    const raw = Array.isArray(cfg?.series_extra) ? cfg.series_extra : [];
    return raw
      .map((s, idx) => ({
        id: String(s?.id || `s${idx + 1}`),
        gateway_id: String(s?.gateway_id || resolvedGatewayId || ""),
        tag_name: String(s?.tag_name || "").trim(),
        label: String(s?.label || "").trim(),
        color: String(s?.color || ""),
        axis: String(s?.axis || "left").toLowerCase() === "right" ? "right" : "left",
        chart_type: String(s?.chart_type || "").toLowerCase(),
        unit: String(s?.unit || "").trim(),
        suffix: String(s?.suffix || "").trim(),
        multiplier: Number(s?.multiplier ?? 1) || 1,
        offset: Number(s?.offset ?? 0) || 0,
        limit_value: s?.limit_value === undefined || s?.limit_value === null ? "" : String(s.limit_value),
        // Per-series style overrides (added 2026-05-14). Each row can carry
        // its own line thickness, dot marker, bar width, bar pattern, and
        // limit line dash style.
        line_width: Number.isFinite(Number(s?.line_width)) ? Number(s.line_width) : null,
        line_dot: String(s?.line_dot || ""),
        bar_width: Number.isFinite(Number(s?.bar_width)) ? Number(s.bar_width) : null,
        bar_pattern: String(s?.bar_pattern || ""),
        limit_dash: String(s?.limit_dash || ""),
      }));
  }, [cfg?.series_extra, widgetType, resolvedGatewayId]);

  // Data series: rows that produce a time-aligned trace on the chart.
  // Limit lines are filtered OUT here so the multi-series merge never tries
  // to fetch historian rows for them.
  const extraSeriesDefs = useMemo(
    () => allExtraSeries.filter((s) => s.chart_type !== "limit" && s.tag_name),
    [allExtraSeries]
  );

  // Limit rows: rendered as horizontal ReferenceLines. They can either pin
  // to a constant `limit_value` or follow the latest sample of a tag.
  const limitLineDefs = useMemo(
    () => allExtraSeries.filter((s) => s.chart_type === "limit"),
    [allExtraSeries]
  );

  // Server-fetched rows for each extra series, keyed by series id.
  const [extraSeriesServerRowsByDef, setExtraSeriesServerRowsByDef] = useState({});

  useEffect(() => {
    if (!extraSeriesDefs.length) {
      setExtraSeriesServerRowsByDef({});
      return;
    }
    const fetcher = fetchWidgetRowsRef.current;
    if (typeof fetcher !== "function") return;
    let cancelled = false;
    const localRange = resolveTimeFilterRange(cfg);
    const fetchAll = async () => {
      const next = {};
      // Operator request: every series in the chart should pull the SAME
      // number of points as the primary. Previously the extras fetched
      // cfgReadingsCount * 8 (so a 20-point primary loaded 160 extra-
      // series points) which made the multi-series chart's secondary
      // curves stretch way past the primary's time window — confusing
      // and visually wrong. Now extras inherit cfgReadingsCount unless
      // the operator explicitly overrode it via series_readings_count
      // (kept for backward compatibility with widgets that already
      // saved a non-zero value).
      const explicitSeriesReads = Number(cfg?.series_readings_count || 0);
      const reads = Math.max(
        20,
        Math.min(
          5000,
          explicitSeriesReads > 0
            ? explicitSeriesReads
            : Number(cfgReadingsCount || 200),
        ),
      );
      for (const def of extraSeriesDefs) {
        try {
          let rows = await fetcher({
            fromUtc: localRange?.fromUtc || "",
            toUtc: localRange?.toUtc || "",
            limit: reads,
            offset: 0,
            gateway: String(def.gateway_id || resolvedGatewayId || ""),
            tag: def.tag_name,
            timeoutMs: 12000,
            maxAttempts: 1,
          });
          if (!Array.isArray(rows) || rows.length === 0) {
            // Fallback: drop gateway scope (handles tags emitted under a
            // different gateway_id alias).
            rows = await fetcher({
              fromUtc: localRange?.fromUtc || "",
              toUtc: localRange?.toUtc || "",
              limit: reads,
              offset: 0,
              gateway: "",
              tag: def.tag_name,
              timeoutMs: 12000,
              maxAttempts: 1,
            });
          }
          if (cancelled) return;
          next[def.id] = Array.isArray(rows) ? rows : [];
        } catch {
          next[def.id] = [];
        }
      }
      if (!cancelled) setExtraSeriesServerRowsByDef(next);
    };
    fetchAll();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    JSON.stringify(extraSeriesDefs.map((d) => `${d.gateway_id}::${d.tag_name}`)),
    refreshTickFast,
    cfgTimePreset,
    cfgTimeFrom,
    cfgTimeTo,
    cfgReadingsCount,
  ]);

  // Combined source: prefer the server-fetched historian rows; fall back to
  // the live broadcast (dataLogView) so freshly-emitted samples appear
  // between fetches.
  const extraSeriesRowsByDef = useMemo(() => {
    if (!extraSeriesDefs.length) return {};
    const liveRows = Array.isArray(dataLogView) ? dataLogView : [];
    const out = {};
    for (const def of extraSeriesDefs) {
      const targetGw = String(def.gateway_id || "").trim();
      const targetTag = def.tag_name;
      const filteredLive = liveRows.filter((r) => {
        const tag = String(r?.tag || r?.tag_name || "").trim();
        if (tag !== targetTag) return false;
        if (!targetGw) return true;
        return String(r?.gateway_id || "").trim() === targetGw;
      });
      const fromServer = Array.isArray(extraSeriesServerRowsByDef[def.id]) ? extraSeriesServerRowsByDef[def.id] : [];
      // De-duplicate by ts+tag so live additions augment the server batch
      // instead of producing twin entries.
      const seen = new Set();
      const merged = [];
      for (const r of [...fromServer, ...filteredLive]) {
        const k = `${String(r?.ts || r?.ts_utc || "")}|${String(r?.tag || r?.tag_name || "")}`;
        if (seen.has(k)) continue;
        seen.add(k);
        merged.push(r);
      }
      out[def.id] = applyWidgetTimeFilter(merged, cfg);
    }
    return out;
  }, [extraSeriesDefs, extraSeriesServerRowsByDef, dataLogView, cfgTimePreset, cfgTimeFrom, cfgTimeTo]);

  // Build a combined dataset on the **union of all sample timestamps** from
  // every series. Each timestamp becomes one row; series that don't have a
  // sample at that ts get null at that position (recharts skips nulls when
  // connectNulls is on). This is the most robust approach — it guarantees
  // every actual sample point is drawn for every series regardless of
  // primary length or sample cadence mismatch.
  // Resolve each limit line to a concrete numeric value. Constant values use
  // `limit_value` directly; tag-bound limits read the most recent sample of
  // that tag from the live broadcast. Declared BEFORE multiSeriesData so
  // the latter can reference resolvedLimitLines.length without TDZ.
  const resolvedLimitLines = useMemo(() => {
    if (!limitLineDefs.length) return [];
    const liveRows = Array.isArray(dataLogView) ? dataLogView : [];
    return limitLineDefs.map((def) => {
      let value = Number.NaN;
      const constant = Number(def.limit_value);
      if (Number.isFinite(constant) && String(def.limit_value).trim() !== "") {
        value = constant * (def.multiplier || 1) + (def.offset || 0);
      } else if (def.tag_name) {
        const targetTag = def.tag_name;
        const targetGw = String(def.gateway_id || "").trim();
        let latestTs = -Infinity;
        let latestVal = null;
        for (const r of liveRows) {
          if (String(r?.tag || r?.tag_name || "").trim() !== targetTag) continue;
          if (targetGw && String(r?.gateway_id || "").trim() !== targetGw) continue;
          const ms = Date.parse(String(r?.ts || r?.ts_utc || "").replace(" ", "T"));
          if (Number.isFinite(ms) && ms > latestTs) {
            latestTs = ms;
            latestVal = r?.value;
          }
        }
        const numericLatest = Number(latestVal);
        if (Number.isFinite(numericLatest)) {
          value = numericLatest * (def.multiplier || 1) + (def.offset || 0);
        }
      }
      return { ...def, resolved_value: value };
    });
  }, [limitLineDefs, dataLogView]);

  const multiSeriesData = useMemo(() => {
    // When only limit lines are configured (no extra data series) we still
    // want to draw the ComposedChart so the limit shows up — fall back to
    // the primary series points so the X axis has a timeline.
    if (!extraSeriesDefs.length) {
      if (!resolvedLimitLines.length) return [];
      return series.map((p, i) => ({ idx: i + 1, ts: p.ts || "", value: p.value ?? null }));
    }

    // ─── Union-of-timestamps merge ────────────────────────────────────────
    // The right approach for a live SCADA dashboard, learned the hard way
    // after two failed attempts:
    //   1) "Walk primary, carry the extra series' lastVal forward" → ugly
    //      horizontal plateaus when one gateway paused.
    //   2) "Fixed tick at the primary gateway interval, take closest sample
    //      in window" → drops every real sample whose timestamp doesn't
    //      land inside a narrow tick window, producing vertical white gaps
    //      INSIDE a series that is in fact emitting continuously.
    //
    // What actually works: build the chart from the UNION of every
    // series' real timestamps. Each series fills its slot at every
    // timestamp WHERE IT HAS A SAMPLE; null at every timestamp where
    // OTHER series had a sample but this series did not. The X axis is
    // time-scaled (see buildXAxisProps) so the chart spaces points by
    // real elapsed time, AND we keep connectNulls=true on the series
    // renderers so each series draws as a continuous line through its
    // own real points — no carry-forward, no fake fills, no gaps inside
    // a series that's still publishing.
    const tsToMs = (raw) => {
      const t = String(raw || "");
      if (!t) return NaN;
      const ms = Date.parse(t);
      if (Number.isFinite(ms)) return ms;
      const iso = t.includes("T") ? t : t.replace(" ", "T");
      const ms2 = Date.parse(iso);
      return Number.isFinite(ms2) ? ms2 : NaN;
    };

    const primaryPts = series
      .map((p) => ({ ts: p.ts || "", tsMs: tsToMs(p.ts), value: p.value }))
      .filter((p) => Number.isFinite(p.tsMs));

    const extraNormalized = extraSeriesDefs.map((def) => {
      const rows = Array.isArray(extraSeriesRowsByDef[def.id]) ? extraSeriesRowsByDef[def.id] : [];
      const pts = rows
        .map((r) => {
          const numeric = Number(r?.value);
          return {
            tsMs: tsToMs(r?.ts || r?.ts_utc),
            value: Number.isFinite(numeric) ? numeric * def.multiplier + def.offset : null,
          };
        })
        .filter((p) => Number.isFinite(p.tsMs));
      return { def, pts };
    });

    // Union of every distinct timestamp across all series. A Map keyed by
    // tsMs collects which series carries which value at that exact time.
    const byTs = new Map();
    for (const p of primaryPts) {
      let row = byTs.get(p.tsMs);
      if (!row) { row = { tsMs: p.tsMs }; byTs.set(p.tsMs, row); }
      row.value = p.value;
    }
    for (const st of extraNormalized) {
      const key = `s_${st.def.id}`;
      for (const p of st.pts) {
        let row = byTs.get(p.tsMs);
        if (!row) { row = { tsMs: p.tsMs }; byTs.set(p.tsMs, row); }
        row[key] = p.value;
      }
    }
    if (byTs.size === 0) return [];
    // Sort timestamps and clamp to the operator's Readings field so the
    // chart NEVER renders more rows than they asked for. Previously the
    // union of primary + extra timestamps could blow past
    // cfgReadingsCount when an extra series had a longer history,
    // producing a 7-hour chart from a "120 readings" widget.
    let sortedTs = Array.from(byTs.keys()).sort((a, b) => a - b);
    const readingsCap = Math.max(10, Number(cfgReadingsCount || 120));
    if (sortedTs.length > readingsCap) {
      sortedTs = sortedTs.slice(-readingsCap);
    }
    // Detect "gateway-stopped" gaps. Anything bigger than 3 × gateway
    // poll interval gets a null row inserted in the middle so Recharts'
    // connectNulls=true does NOT bridge the gap. Without this the chart
    // drew a straight diagonal across hours of downtime, which made
    // every stopped-gateway period look like a smooth drift.
    const tickMs = Math.max(500, Number(gatewayIntervalMs || 1000));
    const gapThresholdMs = tickMs * 3;
    const out = [];
    for (let i = 0; i < sortedTs.length; i += 1) {
      const tsMs = sortedTs[i];
      const row = byTs.get(tsMs);
      out.push({
        idx: out.length + 1,
        ts: new Date(tsMs).toISOString(),
        value: row.value ?? null,
        ...extraNormalized.reduce((acc, st) => {
          acc[`s_${st.def.id}`] = row[`s_${st.def.id}`] ?? null;
          return acc;
        }, {}),
      });
      const next = sortedTs[i + 1];
      if (next !== undefined && (next - tsMs) > gapThresholdMs) {
        // Insert a midpoint null row. Every series's value goes null
        // so connectNulls=true visually breaks each line at the gap.
        const gapMs = tsMs + Math.floor((next - tsMs) / 2);
        const nullRow = { idx: out.length + 1, ts: new Date(gapMs).toISOString(), value: null };
        for (const st of extraNormalized) nullRow[`s_${st.def.id}`] = null;
        out.push(nullRow);
      }
    }
    return out;
  }, [series, extraSeriesDefs, extraSeriesRowsByDef, resolvedLimitLines.length, cfgReadingsCount, gatewayIntervalMs]);

  const hasMultiSeries = extraSeriesDefs.length > 0 || resolvedLimitLines.length > 0;
  const anyRightAxis =
    (extraSeriesDefs.some((d) => d.axis === "right"))
    || resolvedLimitLines.some((d) => d.axis === "right");
  const primaryAxisLabel = String(cfg?.y_axis_label || cfg?.primary_unit || "");
  const rightAxisLabel = String(cfg?.y_axis_right_label || "");
  const computedItems = useMemo(
    () => evaluateComputedRules(computedRowsTimeFiltered, rules),
    [computedRowsTimeFiltered, rulesDepKey]
  );
  const queryOptions = useMemo(
    () => ({
      group_interval: cfgGroupInterval,
      result_aggregation: cfgResultAggregation,
      row_selection: cfgRowSelection,
      row_limit: cfgRowLimit,
      rule_logic: cfgRuleLogic,
    }),
    [cfgGroupInterval, cfgResultAggregation, cfgRowSelection, cfgRowLimit, cfgRuleLogic]
  );
  const computedItemsWithQuery = useMemo(() => {
    const chosenRuleStats =
      Array.isArray(serverRuleStats) && serverRuleStats.length > 0
        ? serverRuleStats
        : Array.isArray(lastGoodServerRuleStats) && lastGoodServerRuleStats.length > 0
          ? lastGoodServerRuleStats
          : [];
    if (chosenRuleStats.length > 0) {
      return chosenRuleStats.map((row, idx) => ({
        id: String(row?.id || `rule-${idx + 1}`),
        label: String(row?.label || `Item ${idx + 1}`),
        value: Number.isFinite(Number(row?.value)) ? Number(row.value) : 0,
        color: String(row?.color || "#14a89a"),
        gateway_id: String(row?.gateway_id || ""),
        tag_name: String(row?.tag_name || ""),
        aggregation: String(row?.aggregation || "count"),
        operator: String(row?.operator || "any"),
        sample_count: Number.isFinite(Number(row?.sample_count)) ? Number(row.sample_count) : 0,
      }));
    }
    // Keep UI live: while backend rule-stats is loading/slow, compute from currently available rows.
    return evaluateComputedRules(computedRowsTimeFiltered, rules, queryOptions);
  }, [serverRuleStats, lastGoodServerRuleStats, computedRowsTimeFiltered, rulesDepKey, queryOptions]);
  const displayTag = formatTagForDisplay ? formatTagForDisplay(tagName) : tagName;
  // Y axis range: auto (computed from the data) or manual (operator
  // specifies min, max, optional tick step). Empty / invalid manual values
  // fall back to auto so the chart never goes blank when the operator
  // leaves a field empty mid-edit.
  const yAxisMode = String(cfg?.y_axis_mode || "auto").toLowerCase();
  const manualY = useMemo(() => {
    if (yAxisMode !== "manual") return null;
    const lo = Number(cfg?.y_min);
    const hi = Number(cfg?.y_max);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null;
    return { lo, hi };
  }, [yAxisMode, cfg?.y_min, cfg?.y_max]);
  const manualYTicks = useMemo(() => {
    if (!manualY) return null;
    const step = Number(cfg?.y_tick_step);
    if (!Number.isFinite(step) || step <= 0) return null;
    const ticks = [];
    // Cap at 50 ticks so a tiny step on a huge range can't render thousands
    // of grid lines and lock the browser.
    const maxTicks = 50;
    for (let v = manualY.lo, i = 0; v <= manualY.hi + step * 1e-9 && i < maxTicks; v += step, i += 1) {
      ticks.push(Number(v.toFixed(10)));
    }
    return ticks;
  }, [manualY, cfg?.y_tick_step]);
  const yDomain = useMemo(() => {
    if (manualY) {
      // Honor the operator's typed min exactly. Earlier we forced the
      // axis to start at 0 unless the typed min was negative, which
      // ignored ranges like min=100 max=200 (the chart painted 0..200
      // instead of 100..200). The form widget already exposes the
      // toggle + the three fields; if the operator typed it, that's
      // what they want.
      return [manualY.lo, manualY.hi];
    }
    return buildAutoYDomain(series);
  }, [manualY, series]);

  const yRightAxisMode = String(cfg?.y_right_axis_mode || "auto").toLowerCase();
  const manualYRight = useMemo(() => {
    if (yRightAxisMode !== "manual") return null;
    const lo = Number(cfg?.y_right_min);
    const hi = Number(cfg?.y_right_max);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null;
    return { lo, hi };
  }, [yRightAxisMode, cfg?.y_right_min, cfg?.y_right_max]);
  const manualYRightTicks = useMemo(() => {
    if (!manualYRight) return null;
    const step = Number(cfg?.y_right_tick_step);
    if (!Number.isFinite(step) || step <= 0) return null;
    const ticks = [];
    const maxTicks = 50;
    for (let v = manualYRight.lo, i = 0; v <= manualYRight.hi + step * 1e-9 && i < maxTicks; v += step, i += 1) {
      ticks.push(Number(v.toFixed(10)));
    }
    return ticks;
  }, [manualYRight, cfg?.y_right_tick_step]);
  const yRightDomain = useMemo(() => {
    if (manualYRight) {
      // Honor operator-typed min exactly (matches the left axis fix).
      return [manualYRight.lo, manualYRight.hi];
    }
    return undefined; // recharts auto-domain on the right axis when undefined
  }, [manualYRight]);
  // Bottom margin = breathing room for X-axis tick labels. Without this
  // the time labels ("01:18:57") were clipped against the card edge —
  // operator request 2026-06-11: "we need a little bit of space below
  // the charts for the time labels".
  const chartMargin = { top: 4, right: 8, left: 0, bottom: 18 };
  const interpolation = getChartInterpolation(widget);
  const chartValueFormat = String(cfg.chart_value_format || "auto");
  const showChartLegend = cfg.chart_show_legend === true;
  const showPointLabels = cfg.chart_show_point_labels === true;
  const [pieHiddenNames, setPieHiddenNames] = useState({});
  const [pieActiveIndex, setPieActiveIndex] = useState(-1);
  const [pieTooltipActive, setPieTooltipActive] = useState(false);
  const hasDirectStats =
    (Array.isArray(serverQueryStats) && serverQueryStats.length > 0) ||
    (Array.isArray(lastGoodServerQueryStats) && lastGoodServerQueryStats.length > 0);
  const hasRuleStats =
    (Array.isArray(serverRuleStats) && serverRuleStats.length > 0) ||
    (Array.isArray(lastGoodServerRuleStats) && lastGoodServerRuleStats.length > 0);
  const pieIsInitialLoading =
    effectiveDataSourceType === "computed"
      ? (serverRuleStatsLoading && !hasRuleStats && computedRowsTimeFiltered.length === 0)
      : (serverQueryStatsLoading && !hasDirectStats && directScopedRowsTimeFiltered.length === 0);
  const pieLoadError = effectiveDataSourceType === "computed" ? serverRuleStatsError : serverQueryStatsError;
  // Trend widgets (line/area/bar) show a transient "Loading..." while the historian
  // range fetch is in flight, instead of immediately flashing "No points".
  const trendIsInitialLoading =
    serverQueryRowsLoading
    && (!Array.isArray(serverQueryRows) || serverQueryRows.length === 0)
    && (!Array.isArray(lastGoodServerQueryRows) || lastGoodServerQueryRows.length === 0);
  // When the historian fetch failed (network, 5xx, or backend error), surface
  // the error inside the widget instead of silently rendering "No points".
  // Without this, charts on fresh installs stay blank with no clue why.
  const trendEmptyText =
    trendIsInitialLoading
      ? "Loading..."
      : (serverQueryError && (!Array.isArray(serverQueryRows) || serverQueryRows.length === 0))
        ? `Historian error: ${String(serverQueryError).slice(0, 120)}`
        : "No points";
  useEffect(() => {
    setPieHiddenNames({});
    setPieActiveIndex(-1);
    setPieTooltipActive(false);
  }, [widget?.id]);
  const yAxisPresetProps = useMemo(
    () => ({
      ...yAxisProps,
      tickFormatter: (v) => formatByPreset(v, chartValueFormat),
    }),
    [chartValueFormat]
  );

  switch (widget.type) {
    case "line_chart":
    case "line_area_chart":
    case "bar_chart": {
      // ── LIVE FAST PATH ──────────────────────────────────────────────
      // When no time range, no grouping, and not in historical mode,
      // route the chart through LiveTagChart. It uses the
      // research-validated pattern: ONE seed REST fetch on mount, then
      // append from the shared WebSocket-fed dataLogView, with a stall
      // detector that re-fetches the tail when the WS drops a sample.
      // No per-heartbeat REST polling, no merge race, no "loaded then
      // frozen" symptom. The heavy path below stays for historical
      // ranges and grouping / aggregation use-cases.
      // Grouping is now handled INSIDE LiveTagChart so the operator
      // can pick "5s avg over the last 60 buckets" and stay on the
      // light path. Only fall back to the heavy aggregator path when
      // the operator explicitly asks for historical data (time preset
      // or absolute time range) or when the dashboard is in Historical
      // mode — those need the full server-side query pipeline.
      const hasTimePreset = String(cfg?.query_time_filter_preset || "none").toLowerCase() !== "none";
      const hasTimeRange = !!(cfg?.query_time_filter_from || cfg?.query_time_filter_to);
      const liveModeEligible =
        !historicalMode
        && !hasTimePreset
        && !hasTimeRange
        && (resolvedLimitLines || []).length === 0;
      if (liveModeEligible) {
        return (
          <LiveTagChart
            widget={widget}
            dataLogView={dataLogView}
            fetchWidgetRows={typeof fetchWidgetRowsRef.current === "function" ? fetchWidgetRowsRef.current : null}
            gatewayIntervalMs={gatewayIntervalMs}
            resolvedGatewayId={resolvedGatewayId}
            tagName={tagName}
            formatTagForDisplay={formatTagForDisplay}
          />
        );
      }

      // Multi-series-aware renderer. When `series_extra` is configured we draw
      // every series on a ComposedChart so primary + extras can coexist with
      // independent axes / chart types. Single-series widgets still flow
      // through the simpler LineChart/AreaChart/BarChart for parity with
      // earlier behaviour.
      const widgetKind = widget.type;
      const primaryColor = getWidgetAccent(widget, widgetKind === "bar_chart" ? "#1f3a5f" : "#14a89a");
      const primaryUnit = String(cfg?.primary_unit || "");
      const primarySuffix = String(cfg?.primary_suffix || "");
      const primaryLabel = (displayTag || "Value") + (primaryUnit ? ` [${primaryUnit}]` : "");
      const primaryKind =
        widgetKind === "bar_chart" ? "bar" : widgetKind === "line_area_chart" ? "area" : "line";
      // Style options: thickness, dot markers (line/area), bar opacity.
      const styleLineWidth = Math.max(1, Math.min(8, Number(cfg?.chart_line_width) || 2));
      const dotPreset = String(cfg?.chart_line_dot || "none");
      const dotSizeByPreset = { none: 0, small: 2, medium: 4, large: 6 };
      const dotForLine = (color) => {
        const r = dotSizeByPreset[dotPreset] || 0;
        if (!r) return false;
        return { r, fill: color, stroke: color, strokeWidth: 0 };
      };
      const activeDotForLine = (color) => {
        const r = (dotSizeByPreset[dotPreset] || 0) + 2;
        return { r: Math.max(3, r), fill: color, stroke: "#ffffff", strokeWidth: 1 };
      };
      const barOpacity = Math.max(0.1, Math.min(1, Number(cfg?.chart_bar_opacity ?? 100) / 100));
      const barWidthPx = Math.max(0, Math.min(120, Number(cfg?.chart_bar_width ?? 0)));
      const barSizeProp = barWidthPx > 0 ? { barSize: barWidthPx } : {};
      const barPatternId = `tn-bar-pattern-${String(widget?.id || "w").replace(/[^a-z0-9]/gi, "")}`;
      const barPatternKind = String(cfg?.chart_bar_pattern || "solid");
      const renderBarPattern = (fillColor) => {
        if (barPatternKind === "solid") return null;
        // Inline SVG <defs> with a small repeating pattern. The Bar fill
        // points to url(#...) so each chart instance has its own pattern.
        const stroke = fillColor;
        if (barPatternKind === "stripes-diag") {
          return (
            <defs>
              <pattern id={barPatternId} patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
                <rect width="4" height="8" fill={stroke} opacity={barOpacity} />
              </pattern>
            </defs>
          );
        }
        if (barPatternKind === "stripes-vert") {
          return (
            <defs>
              <pattern id={barPatternId} patternUnits="userSpaceOnUse" width="6" height="8">
                <rect width="3" height="8" fill={stroke} opacity={barOpacity} />
              </pattern>
            </defs>
          );
        }
        if (barPatternKind === "dots") {
          return (
            <defs>
              <pattern id={barPatternId} patternUnits="userSpaceOnUse" width="6" height="6">
                <circle cx="3" cy="3" r="1.5" fill={stroke} opacity={barOpacity} />
              </pattern>
            </defs>
          );
        }
        return null;
      };
      const barFillFor = (color) => (barPatternKind === "solid" ? color : `url(#${barPatternId})`);
      const fmtValueByUnit = (v, unit, suffix) => {
        const base = formatByPreset(v, chartValueFormat);
        const u = unit ? ` ${unit}` : "";
        const s = suffix ? ` ${suffix}` : "";
        return `${base}${u}${s}`;
      };

      if (!hasMultiSeries) {
        // Original single-series fast path.
        return (
          <div
            className={`dashboard-widget-block dashboard-widget-block-chart ${panEnabled ? "is-pannable" : ""}`}
            onPointerDown={onPanPointerDown}
            onMouseDown={onPanPointerDown}
            onPointerMove={onPanPointerMove}
            onPointerUp={onPanPointerUp}
            onPointerCancel={onPanPointerUp}
            title={panEnabled ? "Drag horizontally to pan back/forward through history" : undefined}>
            {series.length ? (
              <div className="dashboard-widget-chart">
                {widgetKind === "line_area_chart" ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={series} margin={chartMargin}>
                      <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
                      <XAxis {...buildXAxisProps(series, cfg)} />
                      <YAxis {...yAxisPresetProps} domain={yDomain} ticks={manualYTicks || undefined} allowDataOverflow={!!manualY} />
                      <Tooltip
                        {...chartTooltipProps}
                        formatter={(v) => fmtValueByUnit(v, primaryUnit, primarySuffix)}
                        labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                      />
                      {showChartLegend ? <Legend /> : null}
                      <Area
                        type={interpolation}
                        dataKey="value"
                        name={primaryLabel}
                        stroke={primaryColor}
                        fill={rgbaFromHex(primaryColor, 0.24)}
                        strokeWidth={styleLineWidth}
                        dot={dotForLine(primaryColor)}
                        activeDot={activeDotForLine(primaryColor)}
                        label={showPointLabels ? { fill: "var(--ink-soft, #8a98ab)", fontSize: 10 } : false}
                        isAnimationActive={false}
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : widgetKind === "bar_chart" ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={series} margin={chartMargin}>
                      {renderBarPattern(primaryColor)}
                      <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
                      <XAxis {...buildXAxisProps(series, cfg)} />
                      <YAxis {...yAxisPresetProps} domain={yDomain} ticks={manualYTicks || undefined} allowDataOverflow={!!manualY} />
                      <Tooltip
                        {...chartTooltipProps}
                        formatter={(v) => fmtValueByUnit(v, primaryUnit, primarySuffix)}
                        labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                      />
                      {showChartLegend ? <Legend /> : null}
                      <Bar
                        dataKey="value"
                        name={primaryLabel}
                        label={showPointLabels ? { fill: "var(--ink-soft, #8a98ab)", fontSize: 10 } : false}
                        fill={barFillFor(primaryColor)}
                        fillOpacity={barPatternKind === "solid" ? barOpacity : 1}
                        stroke={primaryColor}
                        strokeWidth={barPatternKind === "solid" ? 0 : 1}
                        {...barSizeProp}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={series} margin={chartMargin}>
                      <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
                      <XAxis {...buildXAxisProps(series, cfg)} />
                      <YAxis {...yAxisPresetProps} domain={yDomain} ticks={manualYTicks || undefined} allowDataOverflow={!!manualY} />
                      <Tooltip
                        {...chartTooltipProps}
                        formatter={(v) => fmtValueByUnit(v, primaryUnit, primarySuffix)}
                        labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                      />
                      {showChartLegend ? <Legend /> : null}
                      <Line
                        type={interpolation}
                        dataKey="value"
                        name={primaryLabel}
                        stroke={primaryColor}
                        strokeWidth={styleLineWidth}
                        dot={dotForLine(primaryColor)}
                        activeDot={activeDotForLine(primaryColor)}
                        label={showPointLabels ? { fill: "var(--ink-soft, #8a98ab)", fontSize: 10 } : false}
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            ) : renderEmpty(trendEmptyText)}
          </div>
        );
      }

      // ---- Multi-series ComposedChart ---------------------------------------
      const data = multiSeriesData;
      const dataNonEmpty = data.length > 0;
      // Debug aid: count how many points each series has in the combined
      // dataset. Surfaced as a hidden HTML attribute so we can inspect via
      // the browser inspector if a series still ends up invisible.
      const seriesPointCounts = (() => {
        const counts = { value: 0 };
        for (const def of extraSeriesDefs) counts[`s_${def.id}`] = 0;
        for (const row of data) {
          for (const k of Object.keys(counts)) {
            if (row[k] !== null && row[k] !== undefined) counts[k] += 1;
          }
        }
        return counts;
      })();
      const tooltipLabelFmt = (v) => data.find((p) => p.idx === v)?.ts || String(v);
      // Hide the primary trace when the user left gateway/tag blank — they're
      // building a chart from series_extra only. Without this gate the chart
      // would draw an empty "Value" line in the legend with no data.
      const hasPrimary = Boolean(String(resolvedGatewayId || "").trim() && String(tagName || "").trim());
      const seriesDescriptors = [
        ...(hasPrimary
          ? [{
              id: "_primary",
              dataKey: "value",
              kind: primaryKind,
              name: primaryLabel,
              color: primaryColor,
              axis: "left",
              unit: primaryUnit,
              suffix: primarySuffix,
              line_width: styleLineWidth,
              line_dot: dotPreset,
              bar_width: barWidthPx,
              bar_pattern: barPatternKind,
              bar_opacity: barOpacity,
            }]
          : []),
        ...extraSeriesDefs.map((def, idx) => {
          const fallbackPalette = ["#f97316", "#3b82f6", "#a855f7", "#dc2626", "#10b981", "#f59e0b"];
          // Per-series style overrides. Fall back to the widget defaults
          // when the row was created before this feature shipped (so old
          // saved widgets keep looking the same).
          return {
            id: def.id,
            dataKey: `s_${def.id}`,
            kind: def.chart_type || primaryKind,
            name: (def.label || def.tag_name) + (def.unit ? ` [${def.unit}]` : ""),
            color: def.color || fallbackPalette[idx % fallbackPalette.length],
            axis: def.axis,
            unit: def.unit,
            suffix: def.suffix,
            line_width: Number.isFinite(Number(def.line_width)) ? Number(def.line_width) : styleLineWidth,
            line_dot: ["none", "small", "medium", "large"].includes(String(def.line_dot || ""))
              ? String(def.line_dot)
              : dotPreset,
            bar_width: Number.isFinite(Number(def.bar_width)) ? Number(def.bar_width) : barWidthPx,
            bar_pattern: ["solid", "stripes-diag", "stripes-vert", "dots"].includes(String(def.bar_pattern || ""))
              ? String(def.bar_pattern)
              : barPatternKind,
            bar_opacity: barOpacity,
          };
        }),
      ];
      const formatterByKey = {};
      for (const s of seriesDescriptors) {
        formatterByKey[s.name] = { unit: s.unit, suffix: s.suffix };
      }

      return (
        <div
          className={`dashboard-widget-block dashboard-widget-block-chart ${panEnabled ? "is-pannable" : ""}`}
          data-series-points={JSON.stringify(seriesPointCounts)}
          data-series-rows={data.length}
          onPointerDown={onPanPointerDown}
          onPointerMove={onPanPointerMove}
          onPointerUp={onPanPointerUp}
          onPointerCancel={onPanPointerUp}
          title={panEnabled ? "Drag horizontally to pan back/forward through history" : undefined}
        >
          {dataNonEmpty ? (
            <div className="dashboard-widget-chart">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={data} margin={{ ...chartMargin, right: anyRightAxis ? 32 : chartMargin.right }}>
                  {renderBarPattern(primaryColor)}
                  <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
                  <XAxis {...buildXAxisProps(data, cfg)} />
                  <YAxis
                    yAxisId="left"
                    {...yAxisPresetProps}
                    domain={yDomain}
                    ticks={manualYTicks || undefined}
                    allowDataOverflow={!!manualY}
                    label={primaryAxisLabel ? { value: primaryAxisLabel, angle: -90, position: "insideLeft", fill: "var(--ink-soft, #8a98ab)", fontSize: 11 } : undefined}
                  />
                  {anyRightAxis ? (
                    <YAxis
                      yAxisId="right"
                      orientation="right"
                      {...yAxisPresetProps}
                      domain={yRightDomain}
                      ticks={manualYRightTicks || undefined}
                      allowDataOverflow={!!manualYRight}
                      label={rightAxisLabel ? { value: rightAxisLabel, angle: 90, position: "insideRight", fill: "var(--ink-soft, #8a98ab)", fontSize: 11 } : undefined}
                    />
                  ) : null}
                  <Tooltip
                    {...chartTooltipProps}
                    formatter={(v, name) => {
                      const meta = formatterByKey[name] || {};
                      return fmtValueByUnit(v, meta.unit || "", meta.suffix || "");
                    }}
                    labelFormatter={tooltipLabelFmt}
                  />
                  {showChartLegend ? <Legend /> : null}
                  {seriesDescriptors.map((s) => {
                    const yId = s.axis === "right" ? "right" : "left";
                    // Per-series style: each series carries its own thickness/
                    // dot/bar-width/pattern (with fallback to widget defaults).
                    const seriesStrokeWidth = Math.max(1, Math.min(8, Number(s.line_width) || styleLineWidth));
                    const seriesDotPreset = String(s.line_dot || dotPreset);
                    const seriesDotSize = ({ none: 0, small: 2, medium: 4, large: 6 })[seriesDotPreset] || 0;
                    const seriesDot = seriesDotSize
                      ? { r: seriesDotSize, fill: s.color, stroke: s.color, strokeWidth: 0 }
                      : false;
                    const seriesActiveDot = { r: Math.max(3, seriesDotSize + 2), fill: s.color, stroke: "#fff", strokeWidth: 1 };
                    const seriesBarWidth = Number.isFinite(Number(s.bar_width)) ? Number(s.bar_width) : barWidthPx;
                    const seriesBarPattern = String(s.bar_pattern || barPatternKind);
                    const seriesBarPatternId = seriesBarPattern === "solid"
                      ? null
                      : `${barPatternId}-${String(s.id).replace(/[^a-z0-9]/gi, "")}`;
                    const seriesBarSizeProp = seriesBarWidth > 0 ? { barSize: seriesBarWidth } : {};
                    const renderSeriesBarPattern = () => {
                      if (!seriesBarPatternId) return null;
                      if (seriesBarPattern === "stripes-diag") {
                        return (
                          <defs key={`def-${s.id}`}>
                            <pattern id={seriesBarPatternId} patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
                              <rect width="4" height="8" fill={s.color} opacity={barOpacity} />
                            </pattern>
                          </defs>
                        );
                      }
                      if (seriesBarPattern === "stripes-vert") {
                        return (
                          <defs key={`def-${s.id}`}>
                            <pattern id={seriesBarPatternId} patternUnits="userSpaceOnUse" width="6" height="8">
                              <rect width="3" height="8" fill={s.color} opacity={barOpacity} />
                            </pattern>
                          </defs>
                        );
                      }
                      if (seriesBarPattern === "dots") {
                        return (
                          <defs key={`def-${s.id}`}>
                            <pattern id={seriesBarPatternId} patternUnits="userSpaceOnUse" width="6" height="6">
                              <circle cx="3" cy="3" r="1.5" fill={s.color} opacity={barOpacity} />
                            </pattern>
                          </defs>
                        );
                      }
                      return null;
                    };
                    if (s.kind === "bar") {
                      return (
                        <React.Fragment key={s.id}>
                          {renderSeriesBarPattern()}
                          <Bar
                            dataKey={s.dataKey}
                            name={s.name}
                            yAxisId={yId}
                            fill={seriesBarPatternId ? `url(#${seriesBarPatternId})` : s.color}
                            fillOpacity={seriesBarPatternId ? 1 : barOpacity}
                            stroke={s.color}
                            strokeWidth={seriesBarPatternId ? 1 : 0}
                            isAnimationActive={false}
                            {...seriesBarSizeProp}
                          />
                        </React.Fragment>
                      );
                    }
                    if (s.kind === "area") {
                      return (
                        <Area
                          key={s.id}
                          type={interpolation}
                          dataKey={s.dataKey}
                          name={s.name}
                          yAxisId={yId}
                          stroke={s.color}
                          fill={rgbaFromHex(s.color, 0.2)}
                          strokeWidth={seriesStrokeWidth}
                          dot={seriesDot}
                          activeDot={seriesActiveDot}
                          isAnimationActive={false}
                          connectNulls={false}
                        />
                      );
                    }
                    return (
                      <Line
                        key={s.id}
                        type={interpolation}
                        dataKey={s.dataKey}
                        name={s.name}
                        yAxisId={yId}
                        stroke={s.color}
                        strokeWidth={seriesStrokeWidth}
                        dot={seriesDot}
                        activeDot={seriesActiveDot}
                        isAnimationActive={false}
                        connectNulls={false}
                      />
                    );
                  })}
                  {resolvedLimitLines.map((ll) => {
                    if (!Number.isFinite(ll.resolved_value)) return null;
                    const yId = ll.axis === "right" ? "right" : "left";
                    const label = ll.label || (ll.tag_name ? `${ll.tag_name} limit` : "Limit");
                    const dashByStyle = {
                      solid: "0",
                      dashed: "4 4",
                      dotted: "1 4",
                    };
                    const dashArray = dashByStyle[String(ll.limit_dash || "dashed")] || "4 4";
                    return (
                      <ReferenceLine
                        key={`limit-${ll.id}`}
                        y={ll.resolved_value}
                        yAxisId={yId}
                        stroke={ll.color || "#dc2626"}
                        strokeDasharray={dashArray}
                        strokeWidth={1.5}
                        ifOverflow="extendDomain"
                        label={{
                          value: `${label}: ${ll.resolved_value}`,
                          position: "insideTopRight",
                          fill: ll.color || "#dc2626",
                          fontSize: 11,
                        }}
                      />
                    );
                  })}
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          ) : renderEmpty(trendEmptyText)}
        </div>
      );
    }
    case "pie_chart": {
      const showLegend = parseBool(cfg.pie_show_legend, true);
      const showLabels = parseBool(cfg.pie_show_labels, true);
      const showCount = parseBool(cfg.pie_show_count, true);
      const showPercent = parseBool(cfg.pie_show_percent, true);
      const pieLegendLayout = String(cfg.pie_legend_layout || "side") === "bottom" ? "bottom" : "side";
      const palette = getPiePalette(widget);
      const directRowsGrouped = bucketRows(directScopedRowsTimeFiltered, String(cfg.query_group_interval || "none"));
      const directRowsSelected = String(cfg.query_row_selection || "all") === "last_n"
        ? directRowsGrouped.slice(-Math.max(1, Number(cfg.query_row_limit || 200)))
        : directRowsGrouped;
      const localDirectStats = (() => {
        const byTag = new Map();
        for (const row of directRowsSelected) {
          const nm = String(row?.tag || row?.tag_name || "").trim();
          if (!nm) continue;
          const raw = Number(row?.value);
          const v = Number.isFinite(raw) ? raw : null;
          const acc = byTag.get(nm) || {
            name: nm,
            count: 0,
            sum: 0,
            min: Number.POSITIVE_INFINITY,
            max: Number.NEGATIVE_INFINITY,
            latest: null,
          };
          acc.count += 1;
          if (v !== null) {
            acc.sum += v;
            if (v < acc.min) acc.min = v;
            if (v > acc.max) acc.max = v;
            acc.latest = v;
          }
          byTag.set(nm, acc);
        }
        return Array.from(byTag.values()).map((s) => ({
          name: s.name,
          count: s.count,
          sum: s.sum,
          avg: s.count > 0 ? s.sum / s.count : 0,
          min: Number.isFinite(s.min) ? s.min : 0,
          max: Number.isFinite(s.max) ? s.max : 0,
          latest: Number.isFinite(Number(s.latest)) ? Number(s.latest) : 0,
        }));
      })();
      const pieData = effectiveDataSourceType === "computed"
        ? (() => {
            // Keep all configured rule groups visible (including zero-value groups)
            // so legend stays stable and users can see "no matches" instead of a vanishing slice.
            const mapped = computedItemsWithQuery.map((it, idx) => {
              const rawColor = String(it?.color || "").trim();
              const resolvedColor =
                /^#([0-9a-fA-F]{6})$/.test(rawColor) ? rawColor : palette[idx % palette.length];
              const numericValue = Number(it.value);
              return {
                name: String(it.label || `Item ${idx + 1}`),
                value: Number.isFinite(numericValue) ? Math.abs(numericValue) : 0,
                color: resolvedColor,
              };
            });
            const uniqueColors = new Set(mapped.map((m) => String(m.color || "").toLowerCase()));
            if (mapped.length > 1 && uniqueColors.size <= 1) {
              return mapped.map((m, idx) => ({ ...m, color: palette[idx % palette.length] }));
            }
            return mapped;
          })()
        : (() => {
            const agg = String(cfg.query_result_aggregation || "count");
            const effectiveStats =
              Array.isArray(serverQueryStats) && serverQueryStats.length > 0
                ? serverQueryStats
                : Array.isArray(lastGoodServerQueryStats) && lastGoodServerQueryStats.length > 0
                  ? lastGoodServerQueryStats
                  : [];
            if (!effectiveStats.length && !localDirectStats.length) return [];
            const exactTag = String(tagName || "").trim().toLowerCase();
            // Direct-pie reads aggregate values from the historian stats endpoint.
            // For "latest" we prefer the historian-reported `max(ts_utc)` value
            // (delivered as `latest` in the stats row) and fall back to the live
            // row buffer only if the backend omitted it.
            const latestByTag = new Map();
            for (const row of directRowsSelected) {
              const nm = String(row?.tag || row?.tag_name || "").trim();
              if (!nm) continue;
              const raw = Number(row?.value);
              const v = Number.isFinite(raw) ? raw : null;
              if (v !== null) latestByTag.set(nm, v);
            }
            const directStats = effectiveStats.length
              ? effectiveStats
                  .filter((r) => {
                    const nm = String(r?.tag || "").trim().toLowerCase();
                    return !exactTag || nm === exactTag;
                  })
                  .map((r) => {
                    const tag = String(r?.tag || "").trim();
                    const serverLatest = Number(r?.latest);
                    const fallbackLatest = Number(latestByTag.get(tag));
                    const latest = Number.isFinite(serverLatest)
                      ? serverLatest
                      : Number.isFinite(fallbackLatest)
                        ? fallbackLatest
                        : 0;
                    return {
                      name: tag,
                      count: Number(r?.count || 0),
                      sum: Number(r?.sum || 0),
                      avg: Number.isFinite(Number(r?.avg)) ? Number(r?.avg) : 0,
                      min: Number.isFinite(Number(r?.min)) ? Number(r?.min) : 0,
                      max: Number.isFinite(Number(r?.max)) ? Number(r?.max) : 0,
                      latest,
                    };
                  })
              : localDirectStats
                  .filter((r) => {
                    const nm = String(r?.name || "").trim().toLowerCase();
                    return !exactTag || nm === exactTag;
                  });
            return directStats
              .map((s) => {
                let out = 0;
                if (agg === "count") out = s.count;
                else if (agg === "sum") out = s.sum;
                else if (agg === "avg") out = s.avg;
                else if (agg === "min") out = s.min;
                else if (agg === "max") out = s.max;
                else if (agg === "latest") out = s.latest;
                const numeric = Number(out);
                return {
                  name: formatTagForDisplay ? formatTagForDisplay(s.name) : s.name,
                  value: Number.isFinite(numeric) ? Math.abs(numeric) : 0,
                };
              })
              .slice(0, 8);
          })();
      const visiblePieData = pieData.filter((item) => !pieHiddenNames[String(item.name || "")]);
      const total = visiblePieData.reduce((sum, item) => sum + Number(item.value || 0), 0);
      // Recharts cannot render a Pie when all values are zero; supply a uniform
      // visual fallback so zero-state groups still appear in the ring. The legend
      // continues to display the real values (zeros included).
      const renderPieData = total > 0
        ? visiblePieData
        : visiblePieData.map((item) => ({ ...item, value: 1 }));
      const compact = Number(widget?.w || 0) <= 3 || Number(widget?.h || 0) <= 3;
      const innerRadius = compact ? "50%" : "58%";
      const outerRadius = compact ? "84%" : "94%";
      const centerTotal = Number.isFinite(total) ? total.toFixed(total >= 1000 ? 0 : 2) : "0";
      const legendRows = pieData.map((item, idx) => {
        const raw = Number(item?.value || 0);
        const pct = total > 0 ? (raw / total) * 100 : 0;
        const label = String(item?.name || `Item ${idx + 1}`);
        const valueText = showCount ? raw.toFixed(2) : "";
        const percentText = showPercent ? `${pct.toFixed(1)}%` : "";
        const hidden = Boolean(pieHiddenNames[String(item?.name || "")]);
        return {
          key: String(item?.name || idx),
          hidden,
          label,
          valueText,
          percentText,
          color: item?.color || palette[idx % palette.length],
          pct,
        };
      });
      const toggleLegendRow = (name) => {
        const key = String(name || "");
        setPieHiddenNames((prev) => {
          const draft = { ...(prev || {}), [key]: !prev?.[key] };
          return sanitizePieHiddenMap(draft, pieData);
        });
        setPieActiveIndex(-1);
      };
      return (
        <div className="dashboard-widget-block">
          {pieIsInitialLoading && !pieData.length && !pieLoadError ? (
            renderEmpty("Loading values...")
          ) : pieLoadError && !pieData.length ? (
            renderEmpty("No values")
          ) : pieData.length ? (
            <div className={`dashboard-widget-chart dashboard-donut-wrap ${compact ? "compact" : ""} ${pieLegendLayout === "bottom" ? "legend-bottom" : "legend-side"} ${showLegend ? "" : "no-legend"}`}>
              <div className="dashboard-donut-canvas">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart onMouseLeave={() => { setPieActiveIndex(-1); setPieTooltipActive(false); }}>
                    <Pie
                      data={renderPieData}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={innerRadius}
                      outerRadius={outerRadius}
                      paddingAngle={1}
                      label={false}
                      labelLine={false}
                      activeIndex={pieActiveIndex}
                      activeShape={renderActiveDonutShape}
                      onMouseEnter={(_, idx) => { setPieActiveIndex(idx); setPieTooltipActive(true); }}
                      onMouseLeave={() => { setPieActiveIndex(-1); setPieTooltipActive(false); }}
                      onClick={(entry) => toggleLegendRow(String(entry?.name || ""))}
                      stroke="none"
                      isAnimationActive={false}
                    >
                      {renderPieData.map((entry, idx) => (
                        <Cell key={`${entry.name}-${idx}`} fill={entry.color || palette[idx % palette.length]} />
                      ))}
                    </Pie>
                    <text
                      x="50%"
                      y="48%"
                      textAnchor="middle"
                      className="dashboard-donut-center-value"
                    >
                      {centerTotal}
                    </text>
                    <text
                      x="50%"
                      y="58%"
                      textAnchor="middle"
                      className="dashboard-donut-center-label"
                    >
                      Total
                    </text>
                    <Tooltip
                      {...chartTooltipProps}
                      active={pieTooltipActive}
                      formatter={(_, name) => {
                        const match = visiblePieData.find((item) => String(item?.name || "") === String(name || ""));
                        const real = Number(match?.value);
                        return Number.isFinite(real) ? real.toFixed(2) : "-";
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              {showLegend ? (
                <div className="dashboard-donut-meta">
                  {pieLegendLayout === "side" ? (
                    <div
                      className="dashboard-donut-side-list"
                      style={{ gridTemplateRows: `repeat(${Math.max(1, legendRows.length)}, minmax(0, 1fr))` }}
                    >
                      {legendRows.map((row) => (
                        <button
                          type="button"
                          key={`side-${row.key}`}
                          className={`dashboard-donut-side-row ${row.hidden ? "is-hidden" : ""}`}
                          onClick={() => toggleLegendRow(row.key)}
                          title={row.hidden ? "Show this slice" : "Hide this slice"}
                        >
                          <span className="dashboard-donut-side-text">
                            {showPercent ? <span className="dashboard-donut-strip-line percent">{row.percentText}</span> : null}
                            {showCount ? <span className="dashboard-donut-strip-line value">{row.valueText}</span> : null}
                            {showLabels ? <span className="dashboard-donut-strip-line label">{row.label}</span> : null}
                          </span>
                          <span className="dashboard-donut-side-bar-wrap">
                            <span className="dashboard-donut-side-bar" style={{ background: row.color }} />
                          </span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <>
                      <div className="dashboard-donut-strip-labels">
                        <div
                          className="dashboard-donut-strip-label-grid"
                          style={{ gridTemplateColumns: `repeat(${Math.max(1, legendRows.length)}, minmax(0, 1fr))` }}
                        >
                        {legendRows.map((row) => (
                          <button
                            type="button"
                            key={`lbl-${row.key}`}
                            className={`dashboard-donut-strip-col ${row.hidden ? "is-hidden" : ""}`}
                            onClick={() => toggleLegendRow(row.key)}
                            title={row.hidden ? "Show this slice" : "Hide this slice"}
                          >
                            {showPercent ? <span className="dashboard-donut-strip-line percent">{row.percentText}</span> : null}
                            {showCount ? <span className="dashboard-donut-strip-line value">{row.valueText}</span> : null}
                            {showLabels ? <span className="dashboard-donut-strip-line label">{row.label}</span> : null}
                          </button>
                        ))}
                        </div>
                      </div>
                      <div className="dashboard-donut-segment-strip" aria-hidden>
                        {legendRows.map((row) => (
                          <span
                            key={`seg-${row.key}`}
                            className={`dashboard-donut-segment ${row.hidden ? "is-hidden" : ""}`}
                            style={{ background: row.color }}
                          />
                        ))}
                      </div>
                    </>
                  )}
                </div>
              ) : null}
            </div>
          ) : renderEmpty("No values")}
        </div>
      );
    }
    case "meter_chart": {
      const value = effectiveDataSourceType === "computed"
        ? parseNumber(computedItemsWithQuery[0]?.value)
        : parseNumber(latest?.last_value);
      const ranges = computeMeterRanges(widget, value);
      const domainMin = ranges.length ? Math.min(...ranges.map((r) => Number(r.min))) : 0;
      const domainMax = ranges.length ? Math.max(...ranges.map((r) => Number(r.max))) : 100;
      const meterLegendLayout = parseLegendLayout(cfg?.meter_legend_layout, "side");
      const showMeterLegend = parseBool(cfg?.meter_show_legend, true);
      const gaugeData = ranges
        .map((r) => ({
          name: r.label,
          color: r.color,
          value: Math.max(0, Number(r.max) - Number(r.min)),
        }))
        .filter((r) => Number.isFinite(r.value) && r.value > 0);
      const current = Number.isFinite(Number(value)) ? Number(value) : Number.NaN;
      const currentPct = Number.isFinite(current)
        ? ((Math.max(domainMin, Math.min(domainMax, current)) - domainMin) / Math.max(1e-9, (domainMax - domainMin))) * 100
        : Number.NaN;
      const pointerDeg = Number.isFinite(currentPct) ? (Math.max(0, Math.min(100, currentPct)) * 180) / 100 - 90 : -90;
      return (
        <div className={`dashboard-widget-block dashboard-widget-block-chart dashboard-meter-wrap ${meterLegendLayout === "bottom" ? "legend-bottom" : "legend-side"} ${showMeterLegend ? "" : "no-legend"}`}>
          <div className="dashboard-meter-gauge-wrap dashboard-widget-chart">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={gaugeData}
                  dataKey="value"
                  startAngle={180}
                  endAngle={0}
                  cx="50%"
                  cy="70%"
                  innerRadius="64%"
                  outerRadius="98%"
                  stroke="none"
                  paddingAngle={0.8}
                  isAnimationActive={false}
                >
                  {gaugeData.map((entry, idx) => (
                    <Cell key={`meter-seg-${idx}`} fill={entry.color} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
            <div className="dashboard-meter-pointer-wrap" aria-hidden>
              <div className="dashboard-meter-pointer" style={{ transform: `translate(-50%, 0) rotate(${pointerDeg}deg)` }} />
              <div className="dashboard-meter-pointer-cap" />
            </div>
            <div className="dashboard-meter-caption">
              <div className="dashboard-meter-caption-value">{value === null ? "-" : value.toFixed(2)}</div>
              <div className="dashboard-meter-caption-pct">{Number.isFinite(currentPct) ? `${currentPct.toFixed(1)}%` : "-"}</div>
            </div>
          </div>
          {showMeterLegend ? (
            <div className="dashboard-donut-meta">
              {meterLegendLayout === "side" ? (
                <div
                  className="dashboard-donut-side-list"
                  style={{ gridTemplateRows: `repeat(${Math.max(1, ranges.length)}, minmax(0, 1fr))` }}
                >
                  {ranges.map((row) => (
                    <div key={`meter-row-${row.id}`} className="dashboard-donut-side-row">
                      <span className="dashboard-donut-side-text">
                        <span className="dashboard-donut-strip-line percent">{row.label}</span>
                        <span className="dashboard-donut-strip-line value">{Number(row.min).toFixed(1)} - {Number(row.max).toFixed(1)}</span>
                      </span>
                      <span className="dashboard-donut-side-bar-wrap">
                        <span className="dashboard-donut-side-bar" style={{ background: row.color }} />
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <>
                  <div className="dashboard-donut-strip-labels">
                    <div
                      className="dashboard-donut-strip-label-grid"
                      style={{ gridTemplateColumns: `repeat(${Math.max(1, ranges.length)}, minmax(0, 1fr))` }}
                    >
                      {ranges.map((row) => (
                        <div key={`meter-lbl-${row.id}`} className="dashboard-donut-strip-col">
                          <span className="dashboard-donut-strip-line percent">{row.label}</span>
                          <span className="dashboard-donut-strip-line value">{Number(row.min).toFixed(1)} - {Number(row.max).toFixed(1)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="dashboard-donut-segment-strip" aria-hidden>
                    {ranges.map((row) => (
                      <span key={`meter-seg-strip-${row.id}`} className="dashboard-donut-segment" style={{ background: row.color }} />
                    ))}
                  </div>
                </>
              )}
            </div>
          ) : null}
        </div>
      );
    }
    case "text_kpi":
      {
        const textSize = computeWidgetTextScale(widget, 14, 34);
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-kpi-text" style={{ fontSize: `${textSize}px` }}>{displayTag || "-"}</div>
        </div>
      );
      }
    case "value_kpi": {
      const isComputed = effectiveDataSourceType === "computed";
      const value = isComputed
        ? parseNumber(computedItemsWithQuery[0]?.value)
        : parseNumber(latest?.last_value);
      // Text-typed tags carry their original string in last_value_text. When
      // there is no usable numeric value but we do have text, render the text
      // (operators want to see e.g. "Run", "Idle", "Fault-23" — not "-").
      const textValue = !isComputed && (value === null || value === undefined)
        ? (latest?.last_value_text || null)
        : null;
      const valueSize = computeWidgetTextScale(widget, 18, 46);
      const textSize = computeWidgetTextScale(widget, 12, 30);
      if (textValue !== null) {
        return (
          <div className="dashboard-widget-block">
            <div
              className="dashboard-kpi-value dashboard-kpi-text-value"
              style={{ color: getWidgetAccent(widget, "#14a89a"), fontSize: `${textSize}px` }}
              title={textValue}
            >
              {textValue || "-"}
            </div>
          </div>
        );
      }
      return (
        <div className="dashboard-widget-block">
          <div
            className="dashboard-kpi-value"
            style={{ color: getWidgetAccent(widget, "#14a89a"), fontSize: `${valueSize}px` }}
          >
            {value === null ? "-" : value.toFixed(3)}
          </div>
        </div>
      );
    }
    case "fixed_text":
      {
        const fixedSize = computeWidgetTextScale(widget, 12, 30);
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-fixed-text" style={{ fontSize: `${fixedSize}px` }}>
            {effectiveDataSourceType === "computed" ? (buildFixedText(cfg.text || "", computedItemsWithQuery) || cfg.text || "-") : (cfg.text || "-")}
          </div>
        </div>
      );
      }
    case "divider":
      return (
        <div className="dashboard-divider-block">
          <span>{cfg.text || ""}</span>
        </div>
      );
    case "table_list": {
      const tableColumns = Array.isArray(cfg.query_table_columns) && cfg.query_table_columns.length
        ? cfg.query_table_columns
        : ["ts", "tag", "value"];
      const tableFilterTags = Array.isArray(cfg.table_filter_tags)
        ? cfg.table_filter_tags.map((t) => String(t || "").trim()).filter(Boolean)
        : [];
      const tableFilterTagSet = new Set(tableFilterTags);
      const whereConditions = Array.isArray(cfg.query_where_conditions)
        ? cfg.query_where_conditions.filter((c) => c && c.tag && c.operator && c.enabled !== false)
        : [];
      const advancedColumns = Array.isArray(cfg.query_advanced_columns)
        ? cfg.query_advanced_columns.filter((c) => c && c.id && c.source)
        : [];

      // All historian rows in scope for this widget (already filtered by gateway/tag
      // upstream). For advanced mode we use the raw scoped rows (not the filtered
      // ones), because each column independently picks its tag.
      const baseRows = Array.isArray(effectiveRows) ? effectiveRows : [];

      // ---- where-condition evaluation ------------------------------------
      // A bucket of rows (one time bucket) passes if every condition has at
      // least one matching row inside that bucket.
      const cmpOp = (val, op, t1, t2) => {
        const a = Number(val);
        const v1 = Number(t1);
        const v2 = Number(t2);
        if (!Number.isFinite(a)) return false;
        switch (op) {
          case "eq": return Number.isFinite(v1) && a === v1;
          case "ne": return Number.isFinite(v1) && a !== v1;
          case "lt": return Number.isFinite(v1) && a < v1;
          case "lte": return Number.isFinite(v1) && a <= v1;
          case "gt": return Number.isFinite(v1) && a > v1;
          case "gte": return Number.isFinite(v1) && a >= v1;
          case "between":
            if (!Number.isFinite(v1) || !Number.isFinite(v2)) return false;
            { const lo = Math.min(v1, v2), hi = Math.max(v1, v2); return a >= lo && a <= hi; }
          default: return false;
        }
      };

      // ---- ADVANCED COLUMNS PATH -----------------------------------------
      if (advancedColumns.length > 0 && effectiveDataSourceType !== "computed") {
        // Bucket all base rows by time bucket (interval), then pick last N buckets.
        const bucketInterval = String(cfg.query_group_interval || "none");
        const buckets = (() => {
          const map = new Map();
          for (const r of baseRows) {
            const ts = toTsMs(r?.ts || r?.ts_utc);
            if (!Number.isFinite(ts)) continue;
            const tag = String(r?.tag || r?.tag_name || "").trim();
            if (tableFilterTagSet.size && !tableFilterTagSet.has(tag)) {
              // tag filter only restricts the tag column data, NOT the where lookups
              // — but we keep it broad here so where conditions can reference other tags.
              // We'll re-filter when reading the per-column tag below.
            }
            // Bucket key: ms truncated to the chosen interval (or per-sample if "none").
            let bucketMs = 0;
            if (bucketInterval === "none") bucketMs = ts;
            else {
              const intervalSeconds = {
                "1s": 1, "5s": 5, "10s": 10, "30s": 30,
                "1m": 60, "5m": 300, "15m": 900,
                "1h": 3600, "1d": 86400,
              }[bucketInterval];
              if (!intervalSeconds) bucketMs = ts;
              else bucketMs = Math.floor(ts / (intervalSeconds * 1000)) * intervalSeconds * 1000;
            }
            const arr = map.get(bucketMs);
            if (arr) arr.push(r);
            else map.set(bucketMs, [r]);
          }
          return Array.from(map.entries()).sort((a, b) => a[0] - b[0]);
        })();

        // Filter buckets by where conditions.
        const matchingBuckets = buckets.filter(([_bucketMs, rowsInBucket]) => {
          if (whereConditions.length === 0) return true;
          return whereConditions.every((cond) => {
            const t = String(cond.tag || "").trim();
            if (!t) return true;
            // most-recent row of this tag in the bucket
            const candidates = rowsInBucket.filter((r) => String(r?.tag || r?.tag_name || "").trim() === t);
            if (!candidates.length) return false;
            const newest = candidates.reduce((acc, r) => {
              const ts = toTsMs(r?.ts || r?.ts_utc);
              return (!acc || ts > toTsMs(acc?.ts || acc?.ts_utc)) ? r : acc;
            }, null);
            return cmpOp(newest?.value, cond.operator, cond.value, cond.value2);
          });
        });

        // Apply row_selection: "latest" (default — most-recent N), "oldest"
        // (first N in time), or "all" (no trim). The editor exposes this
        // option but the legacy code always took the tail, so "oldest"
        // produced the same table as "latest".
        const rowLimit = Math.max(1, Number(cfg.list_limit || cfg.query_row_limit || 8));
        const rowSel = String(cfg?.query_row_selection || "all").toLowerCase();
        const visibleBuckets =
          rowSel === "oldest" ? matchingBuckets.slice(0, rowLimit)
          : rowSel === "all" ? matchingBuckets
          : matchingBuckets.slice(-rowLimit);

        const numeric = (v) => {
          const n = Number(v);
          return Number.isFinite(n) ? n : null;
        };
        const aggregate = (rowsForTag, op) => {
          const vals = rowsForTag.map((r) => numeric(r?.value)).filter((v) => v !== null);
          if (!vals.length) return null;
          switch (op) {
            case "first": return vals[0];
            case "last": return vals[vals.length - 1];
            case "avg": return vals.reduce((a, b) => a + b, 0) / vals.length;
            case "min": return Math.min(...vals);
            case "max": return Math.max(...vals);
            case "sum": return vals.reduce((a, b) => a + b, 0);
            case "count": return vals.length;
            default: return vals[vals.length - 1];
          }
        };

        // Build display rows.
        const displayRows = visibleBuckets.map(([bucketMs, rowsInBucket]) => {
          // first pass: tag/ts columns
          const cellValues = {};
          for (const col of advancedColumns) {
            if (col.source === "ts") {
              const sample = rowsInBucket[rowsInBucket.length - 1];
              cellValues[col.id] = sample?.ts || new Date(bucketMs).toISOString();
            } else if (col.source === "tag") {
              const t = String(col.tag || "").trim();
              const sorted = rowsInBucket
                .filter((r) => String(r?.tag || r?.tag_name || "").trim() === t)
                .sort((a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc));
              cellValues[col.id] = aggregate(sorted, String(col.aggregation || "last"));
            }
          }
          // second pass: calc columns (a, b, c... map to tag columns by order)
          const tagOrCalcCols = advancedColumns.filter((c) => c.source !== "ts");
          for (const col of advancedColumns) {
            if (col.source !== "calc") continue;
            const env = {};
            tagOrCalcCols.forEach((c, i) => {
              if (i >= 26) return;
              env[String.fromCharCode(97 + i)] = numeric(cellValues[c.id]);
            });
            const expr = String(col.expression || "").trim();
            cellValues[col.id] = expr ? evalSafeExpression(expr, env) : null;
          }
          return { bucketMs, cellValues };
        });

        return (
          <div className="dashboard-widget-block">
            {displayRows.length ? (
              <div className="dashboard-table-mini-wrap">
                <table className="dashboard-table-mini">
                  <thead>
                    <tr>
                      {advancedColumns.map((col) => (
                        <th key={col.id}>{col.header || (col.source === "ts" ? "Timestamp" : col.tag || "Column")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {displayRows.map((row, idx) => (
                      <tr key={`${row.bucketMs}-${idx}`}>
                        {advancedColumns.map((col) => {
                          const cell = row.cellValues[col.id];
                          if (col.source === "ts") {
                            return <td key={`${idx}-${col.id}`}>{String(cell || "-")}</td>;
                          }
                          const n = Number(cell);
                          return (
                            <td key={`${idx}-${col.id}`}>
                              {Number.isFinite(n) ? n.toFixed(3) : "-"}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : renderEmpty("No rows match the configured conditions")}
          </div>
        );
      }

      // ---- LEGACY PATH (simple columns) ----------------------------------
      const directRowsRaw = (Array.isArray(directScopedRowsTimeFiltered) ? directScopedRowsTimeFiltered : [])
        .filter((row) => {
          if (!tableFilterTagSet.size) return true;
          const rowTag = String(row?.tag || row?.tag_name || "").trim();
          return tableFilterTagSet.has(rowTag);
        })
        .sort((a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc));
      const directRowsGrouped = bucketRows(directRowsRaw, String(cfg.query_group_interval || "none"));
      const tableSel = String(cfg.query_row_selection || "all").toLowerCase();
      const tableLimit = Math.max(1, Number(cfg.query_row_limit || cfg.list_limit || 8));
      const directRowsSelected =
        tableSel === "last_n" || tableSel === "latest" ? directRowsGrouped.slice(-tableLimit)
        : tableSel === "oldest" ? directRowsGrouped.slice(0, tableLimit)
        : directRowsGrouped;
      const rows = effectiveDataSourceType === "computed"
        ? computedItemsWithQuery.slice(0, Math.max(1, Number(cfg.list_limit || 8)))
        : directRowsSelected.slice(-Math.max(1, Number(cfg.list_limit || 8)));
      return (
        <div className="dashboard-widget-block">
          {rows.length ? (
            <div className="dashboard-table-mini-wrap">
              <table className="dashboard-table-mini">
                <thead>
                  <tr>
                    {effectiveDataSourceType === "computed"
                      ? (<><th>Label</th><th>Value</th></>)
                      : tableColumns.map((col) => (
                          <th key={col}>{col === "ts" ? "Timestamp" : col === "gateway" ? "Gateway" : col === "tag" ? "Tag" : "Value"}</th>
                        ))}
                  </tr>
                </thead>
                <tbody>
                  {effectiveDataSourceType === "computed"
                    ? rows.map((r) => (
                        <tr key={`${r.id}`}>
                          <td>{r.label}</td>
                          <td>{Number.isFinite(Number(r.value)) ? Number(r.value).toFixed(3) : "-"}</td>
                        </tr>
                      ))
                    : rows.map((r, idx) => (
                        <tr key={`${r.gateway_id || "gw"}-${r.tag_name || r.tag || "tag"}-${idx}`}>
                          {tableColumns.map((col) => {
                            if (col === "ts") return <td key={`${idx}-ts`}>{String(r?.ts || "-")}</td>;
                            if (col === "gateway") return <td key={`${idx}-gw`}>{String(r?.gateway_name || r?.gateway_id || "-")}</td>;
                            if (col === "tag") return <td key={`${idx}-tag`}>{formatTagForDisplay ? formatTagForDisplay(r?.tag || r?.tag_name || "-") : String(r?.tag || r?.tag_name || "-")}</td>;
                            // String-typed tags expose value_text; show that when present, otherwise the numeric value.
                            const txt = r?.value_text;
                            if (txt !== null && txt !== undefined && txt !== "") {
                              return <td key={`${idx}-value`} title={String(txt)}>{String(txt)}</td>;
                            }
                            return <td key={`${idx}-value`}>{Number.isFinite(Number(r?.value)) ? Number(r.value).toFixed(3) : "-"}</td>;
                          })}
                        </tr>
                      ))}
                </tbody>
              </table>
            </div>
          ) : renderEmpty("No rows")}
        </div>
      );
    }
    case "image":
      return (
        <div className="dashboard-widget-block">
          {cfg.source_url ? <img src={cfg.source_url} alt={widget.title || "widget"} className="dashboard-media" /> : renderEmpty("Set image URL")}
        </div>
      );
    case "ip_camera":
      return (
        <div className="dashboard-widget-block">
          {cfg.camera_url ? (
            <iframe
              className="dashboard-media"
              src={cfg.camera_url}
              title={widget.title || "ip-camera-feed"}
              allow="autoplay; fullscreen"
            />
          ) : renderEmpty("Set camera URL")}
        </div>
      );
    case "cloud_sync_status":
      return <CloudSyncStatusWidget widget={widget} />;
    case "report_card":
      return <ReportCardWidget widget={widget} />;
    default:
      return renderEmpty("Unsupported widget");
  }
}

// =====================================================================
// ReportCardWidget — three display modes:
//   * summary       — template name + last generated PDF link +
//                     Generate now button (the original behaviour).
//   * pdf_preview   — embeds the most recent PDF inline using an
//                     <iframe> so the operator sees the actual report.
//   * html_preview  — renders the template's sections (header + KPI
//                     grid + charts + tables + pies + text + image)
//                     as live HTML so the layout matches what the PDF
//                     prints, refreshes on every poll.
// All modes share the "Generate now" button and the optional auto-
// refresh interval (cfg.report_refresh_minutes), so scheduling stays
// in Scheduled Reports while quick triggers stay on the widget.
// =====================================================================
function ReportCardWidget({ widget }) {
  const cfg = widget?.config || {};
  const templateId = String(cfg.report_template_id || "").trim();
  const viewMode = (() => {
    const m = String(cfg.report_view_mode || "").toLowerCase();
    return ["summary", "pdf_preview", "html_preview"].includes(m) ? m : "summary";
  })();
  const refreshMin = Math.max(0, Math.min(1440, Number(cfg.report_refresh_minutes || 0)));

  const [templates, setTemplates] = useState([]);
  const [generated, setGenerated] = useState(null);
  const [scheduleSummary, setScheduleSummary] = useState("");
  const [previewData, setPreviewData] = useState(null);
  const [previewError, setPreviewError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusMsg, setStatusMsg] = useState("");

  const reloadMeta = useCallback(async () => {
    try {
      const tpls = await listReportTemplates();
      const rows = Array.isArray(tpls?.templates) ? tpls.templates
        : (Array.isArray(tpls?.rows) ? tpls.rows : []);
      setTemplates(rows);
    } catch (_) { /* keep last */ }
    if (!templateId) { setGenerated(null); setScheduleSummary(""); return; }
    try {
      const list = await listGeneratedReports({ templateId, limit: 1 });
      const rows = Array.isArray(list?.generated) ? list.generated
        : (Array.isArray(list?.rows) ? list.rows : []);
      setGenerated(rows[0] || null);
    } catch (err) { setError(String(err?.message || err)); }
    try {
      const schedules = await listScheduledReports();
      const rows = Array.isArray(schedules?.schedules) ? schedules.schedules
        : (Array.isArray(schedules?.rows) ? schedules.rows : []);
      const matches = rows.filter((s) => String(s.template_id || "") === templateId);
      if (matches.length === 0) setScheduleSummary("No schedule");
      else if (matches.length === 1) {
        const m = matches[0];
        setScheduleSummary(m.trigger_kind === "tag"
          ? `Tag trigger · ${m.trigger_tag_name || "(unset)"}`
          : `Every ${m.trigger_interval_minutes || "?"} min`);
      } else setScheduleSummary(`${matches.length} schedules`);
    } catch (_) { /* leave previous */ }
  }, [templateId]);

  const reloadPreview = useCallback(async () => {
    if (!templateId || viewMode !== "html_preview") { setPreviewData(null); setPreviewError(""); return; }
    try {
      const res = await getReportTemplatePreviewData(templateId);
      setPreviewData(res?.data || null);
      setPreviewError("");
    } catch (err) {
      setPreviewError(String(err?.message || err));
    }
  }, [templateId, viewMode]);

  useEffect(() => { reloadMeta(); }, [reloadMeta]);
  useEffect(() => { reloadPreview(); }, [reloadPreview]);

  // Operator-configurable auto-refresh: each cycle re-fetches whichever
  // view is active (PDF mode re-pulls the latest generated, HTML mode
  // rebuilds the section payload). 0 disables.
  useEffect(() => {
    if (!refreshMin) return undefined;
    const tick = () => { reloadMeta(); reloadPreview(); };
    const id = setInterval(tick, Math.max(15, refreshMin) * 60 * 1000);
    return () => clearInterval(id);
  }, [refreshMin, reloadMeta, reloadPreview]);

  const runNow = async () => {
    if (!templateId) return;
    setBusy(true); setError(""); setStatusMsg("Generating…");
    try {
      await runReportTemplateNow(templateId);
      setStatusMsg("Generated.");
      setTimeout(() => { reloadMeta(); reloadPreview(); setStatusMsg(""); }, 800);
    } catch (err) {
      setError(String(err?.message || err));
      setStatusMsg("");
    } finally { setBusy(false); }
  };

  const downloadLast = () => { if (generated?.id) openGeneratedReport(generated.id); };

  const activeTpl = templates.find((t) => String(t.id || "") === templateId);
  const pdfUrl = generated?.id ? getGeneratedReportFileUrl(generated.id, { inline: true }) : "";

  // ── Header — same shape in every mode so the operator always sees
  //    the template name + a quick-action toolbar.
  const headerStrip = (
    <div className="dashboard-report-card-row">
      <div style={{ minWidth: 0, flex: 1 }}>
        <strong>{activeTpl?.name || "Report"}</strong>
        {scheduleSummary ? <span className="dashboard-report-schedule" style={{ marginLeft: 8 }}>{scheduleSummary}</span> : null}
      </div>
      <div className="row" style={{ gap: 4 }}>
        <button type="button" className="btn btn-secondary btn-sm" disabled={!templateId || busy} onClick={runNow}>
          {busy ? "Generating…" : "Generate now"}
        </button>
        {generated ? <button type="button" className="btn btn-primary btn-sm" onClick={downloadLast}>Open PDF</button> : null}
      </div>
    </div>
  );

  const statusStrip = (
    <div className="dashboard-report-card-row">
      {statusMsg ? <span className="muted">{statusMsg}</span> : null}
      {error ? <span className="warn">{error}</span> : null}
      {generated && viewMode !== "summary" ? (
        <span className="muted" style={{ fontSize: 11 }}>
          Last: {generated.generated_utc ? new Date(generated.generated_utc).toLocaleString() : "—"}
        </span>
      ) : null}
    </div>
  );

  if (!templateId) {
    return (
      <div className="dashboard-widget-block dashboard-report-card">
        {headerStrip}
        <div className="dashboard-report-card-row dashboard-report-empty">
          Pick a template in the widget editor.
        </div>
      </div>
    );
  }

  if (viewMode === "pdf_preview") {
    return (
      <div className="dashboard-widget-block dashboard-report-card">
        {headerStrip}
        <div className="dashboard-report-pdf-frame">
          {pdfUrl ? (
            <iframe
              src={pdfUrl}
              title={`Report preview — ${activeTpl?.name || templateId}`}
              style={{ border: 0, width: "100%", height: "100%" }}
            />
          ) : (
            <div className="dashboard-report-empty">No PDF generated yet — click <strong>Generate now</strong>.</div>
          )}
        </div>
        {statusStrip}
      </div>
    );
  }

  if (viewMode === "html_preview") {
    return (
      <div className="dashboard-widget-block dashboard-report-card">
        {headerStrip}
        <div className="dashboard-report-html-frame">
          {previewError ? <div className="warn">{previewError}</div> : null}
          {previewData ? (
            <ReportHtmlPreview data={previewData} />
          ) : (
            <div className="muted">Loading preview…</div>
          )}
        </div>
        {statusStrip}
      </div>
    );
  }

  // summary (default)
  return (
    <div className="dashboard-widget-block dashboard-report-card">
      {headerStrip}
      {generated ? (
        <div className="dashboard-report-card-row dashboard-report-last">
          <div>
            <div className="dashboard-report-filename">{generated.filename || `report-${generated.id}.pdf`}</div>
            <div className="dashboard-report-meta">
              {generated.generated_utc ? new Date(generated.generated_utc).toLocaleString() : "—"}
              {Number.isFinite(Number(generated.size_bytes))
                ? ` · ${Math.round(Number(generated.size_bytes) / 1024)} KB`
                : ""}
            </div>
          </div>
        </div>
      ) : (
        <div className="dashboard-report-card-row dashboard-report-empty">
          No reports generated yet.
        </div>
      )}
      {statusStrip}
    </div>
  );
}

// ─── HTML preview renderer ──────────────────────────────────────────
// Walks the JSON section list returned by GET .../preview-data and
// renders each section as inline HTML / Recharts. Layout intentionally
// mirrors the PDF (header banner, KPI grid, chart-as-line, table, pie)
// so the operator gets the same visual story in the dashboard as they
// would in the printed report.
function ReportHtmlPreview({ data }) {
  if (!data) return null;
  const sections = Array.isArray(data.sections) ? data.sections : [];
  return (
    <div className="dashboard-report-html-doc">
      {sections.map((s, idx) => {
        switch (s.type) {
          case "header":
            return (
              <div key={idx} className="dashboard-report-html-header">
                <h2>{s.title}</h2>
                {s.subtitle ? <div className="muted">{s.subtitle}</div> : null}
                {s.show_generated_at !== false ? <div className="muted" style={{ fontSize: 11 }}>
                  Generated {new Date().toLocaleString()}
                </div> : null}
              </div>
            );
          case "text":
            return (
              <div key={idx} className="dashboard-report-html-text">
                {s.title ? <h3>{s.title}</h3> : null}
                <div style={{ whiteSpace: "pre-wrap" }}>{s.text}</div>
              </div>
            );
          case "kpi_grid": {
            const cols = Math.max(1, Math.min(6, Number(s.columns || 4)));
            return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div className="dashboard-report-html-kpi-grid"
                  style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
                  {(s.items || []).map((it, i) => (
                    <div key={i} className="dashboard-report-html-kpi-cell">
                      <div className="kpi-label">{it.label}</div>
                      <div className="kpi-value">
                        {it.value == null ? "—" : Number(it.value).toLocaleString(undefined, { maximumFractionDigits: 3 })}
                        {it.unit ? <span className="kpi-unit">{it.unit}</span> : null}
                      </div>
                      <div className="kpi-meta">{it.sample_count} samples · {it.aggregation}</div>
                    </div>
                  ))}
                </div>
              </div>
            );
          }
          case "line_chart":
          case "area_chart":
          case "bar_chart": {
            const series = Array.isArray(s.series) ? s.series : [];
            if (!series.length) return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div className="muted">No samples in range.</div>
              </div>
            );
            // Merge timestamps across series. Same union-of-ts approach
            // the dashboard chart uses so multi-series stays aligned.
            const byTs = new Map();
            series.forEach((srs, sidx) => {
              for (const [ts, v] of (srs.points || [])) {
                let row = byTs.get(ts);
                if (!row) { row = { ts }; byTs.set(ts, row); }
                row[`s${sidx}`] = v;
              }
            });
            const rows = [...byTs.values()].sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
            return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div style={{ width: "100%", height: 180 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={rows} margin={{ top: 4, right: 8, left: 0, bottom: 18 }}>
                      <CartesianGrid stroke="var(--line, rgba(255,255,255,0.07))" strokeDasharray="3 3" />
                      <XAxis dataKey="ts" tickFormatter={(v) => String(v).slice(11, 19)} fontSize={10} />
                      <YAxis fontSize={10} />
                      <Tooltip labelFormatter={(v) => String(v)} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      {series.map((srs, sidx) => {
                        const color = srs.color || ["#14a89a", "#f97316", "#3b82f6", "#a855f7"][sidx % 4];
                        const props = {
                          key: `s${sidx}`,
                          type: "monotone",
                          dataKey: `s${sidx}`,
                          name: srs.label + (srs.unit ? ` (${srs.unit})` : ""),
                          stroke: color,
                          fill: s.type === "area_chart" ? color + "33" : color,
                          isAnimationActive: false,
                          connectNulls: true,
                        };
                        if (s.type === "bar_chart") return <Bar {...props} />;
                        if (s.type === "area_chart") return <Area {...props} />;
                        return <Line {...props} dot={false} />;
                      })}
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </div>
            );
          }
          case "pie_chart": {
            const slices = Array.isArray(s.slices) ? s.slices : [];
            if (!slices.length) return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div className="muted">No data.</div>
              </div>
            );
            return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div style={{ width: "100%", height: 180 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={slices} dataKey="value" nameKey="label" outerRadius={70} isAnimationActive={false}>
                        {slices.map((sl, i) => (
                          <Cell key={i} fill={sl.color || ["#14a89a", "#f97316", "#3b82f6", "#a855f7", "#22c55e"][i % 5]} />
                        ))}
                      </Pie>
                      <Tooltip />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              </div>
            );
          }
          case "table": {
            const header = Array.isArray(s.header) ? s.header : [];
            const rows = Array.isArray(s.rows) ? s.rows : [];
            return (
              <div key={idx} className="dashboard-report-html-section">
                {s.title ? <h3>{s.title}</h3> : null}
                <div className="dashboard-report-html-table-wrap">
                  <table className="dashboard-report-html-table">
                    <thead>
                      <tr>{header.map((h, i) => <th key={i}>{h}</th>)}</tr>
                    </thead>
                    <tbody>
                      {rows.map((r, ri) => (
                        <tr key={ri}>
                          {r.map((c, ci) => <td key={ci}>{c}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {Number(s.row_count || 0) > rows.length ? (
                    <div className="muted" style={{ fontSize: 11, padding: 4 }}>
                      Showing {rows.length} of {s.row_count} rows.
                    </div>
                  ) : null}
                </div>
              </div>
            );
          }
          case "image":
            return (
              <div key={idx} className="dashboard-report-html-section" style={{ textAlign: s.align || "center" }}>
                {s.title ? <h3>{s.title}</h3> : null}
                {s.data_url ? <img src={s.data_url} alt={s.title || ""} style={{ maxWidth: "100%" }} /> : null}
                {s.caption ? <div className="muted" style={{ fontSize: 11 }}>{s.caption}</div> : null}
              </div>
            );
          case "spacer":
            return <div key={idx} style={{ height: Math.max(4, Number(s.height) * 2) }} />;
          case "page_break":
            return <hr key={idx} style={{ border: 0, borderTop: "2px dashed var(--stroke)" }} />;
          case "error":
            return <div key={idx} className="warn">Section error ({s.section_type}): {s.error}</div>;
          default:
            return <div key={idx} className="muted">Unsupported section: {s.type || s.raw_type}</div>;
        }
      })}
    </div>
  );
}
