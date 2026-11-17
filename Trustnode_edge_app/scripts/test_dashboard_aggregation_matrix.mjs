/**
 * AGGREGATION MATRIX + speed + per-bound Y axis + card size (2026-11-16).
 *
 * Reported: a bar chart aggregated every 5 min "did not change and did not
 * create a single bar for the 5 minutes" (the setting did not re-fetch - the
 * chart kept the old buckets until a 30 s tick); no 2 s / custom option (the
 * query dialog's "Time grouping" was a second control without them); gaps at
 * both ends of an aggregated plot; slow dashboards at 1 000 - 5 000 points
 * (~130 ms of widget work per live tick); per-bound Y axis; a widget's card
 * resized when its type changed.
 *
 * Renders the REAL widget (jsdom; recharts recorded) for every chart type,
 * one and two series, every aggregation and mode, and checks what is drawn.
 *
 * (The block below is shared with test_dashboard_charts_draw_window.mjs.)
 * A dashboard chart DRAWS its window: every series, the whole span, and lines
 * that can actually be drawn.
 *
 * 2026-11-16, measured with this harness: on 15 min / 1 h / 6 h a 3-series
 * chart handed the chart library a null between every pair of points (gap
 * markers judged against 3 gateway intervals while the points were buckets
 * 10 s - 5 min apart), so no segment could be drawn and "only the last few
 * items" showed; the trigger chart began the run at the first live reading
 * (1 min instead of 12); and the time axis spanned the data, not the window.
 * The earlier tests counted fetches and points - not what can be drawn.
 *
 * Renders the REAL DashboardWidgetCard in jsdom; "recharts" is replaced by a
 * recorder that keeps the props each chart / axis / series receives. The fake
 * store answers like the backend: bucketed rows stamped 'YYYY-MM-DD HH:MM:SS'
 * (UTC, no zone), raw rows 'YYYY-MM-DD HH:MM:SS.fff', newest first.
 *
 *   node scripts/test_dashboard_charts_draw_window.mjs          (UTC and UTC-3)
 *   TN_WIDGETS_FILE=<path> ... renders another copy of DashboardWidgets.jsx
 */
if (process.env.TN_TZ) process.env.TZ = process.env.TN_TZ;
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "frontend", "src");
const WIDGETS = process.env.TN_WIDGETS_FILE || path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const { JSDOM } = await import("file:///" + path.join(SRC, "..", "node_modules", "jsdom", "lib", "api.js").replace(/\\/g, "/"));
const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", { pretendToBeVisual: true });
const w = dom.window;
for (const k of ["window", "document", "navigator", "HTMLElement", "Element", "Node", "SVGElement", "getComputedStyle",
  "requestAnimationFrame", "cancelAnimationFrame", "localStorage", "sessionStorage", "MutationObserver", "Event", "CustomEvent"]) {
  try { globalThis[k] = w[k]; } catch (_) { /* read-only */ }
}
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
globalThis.__rec = [];

const STUB = `
import React from "react";
const rec = (kind) => (props) => {
  const { children, ...rest } = props || {};
  globalThis.__rec.push({ kind, props: rest });
  return React.createElement("div", { "data-kind": kind }, children);
};
export const ResponsiveContainer = (p) => React.createElement("div", null, typeof p.children === "function" ? p.children({ width: 600, height: 300 }) : p.children);
for (const k of []) {}
export const LineChart = rec("LineChart"), AreaChart = rec("AreaChart"), BarChart = rec("BarChart"),
  ComposedChart = rec("ComposedChart"), PieChart = rec("PieChart");
export const Line = rec("Line"), Area = rec("Area"), Bar = rec("Bar"), Pie = rec("Pie"), Cell = rec("Cell"),
  XAxis = rec("XAxis"), YAxis = rec("YAxis"), Tooltip = rec("Tooltip"), Legend = rec("Legend"),
  CartesianGrid = rec("CartesianGrid"), ReferenceLine = rec("ReferenceLine"), Sector = rec("Sector"),
  Brush = rec("Brush"), ReferenceArea = rec("ReferenceArea"), Scatter = rec("Scatter"), ScatterChart = rec("ScatterChart"),
  RadialBarChart = rec("RadialBarChart"), RadialBar = rec("RadialBar"), PolarAngleAxis = rec("PolarAngleAxis"),
  Label = rec("Label"), LabelList = rec("LabelList"), ReferenceDot = rec("ReferenceDot"), ZAxis = rec("ZAxis");
`;
const esbuild = await import("file:///" + path.join(SRC, "..", "node_modules", "esbuild", "lib", "main.js").replace(/\\/g, "/"));
const out = path.join(os.tmpdir(), "tn_probe_" + process.pid + ".mjs");
await esbuild.build({
  stdin: {
    contents: "export { DashboardWidgetCard, _formatChartTickTime } from " + JSON.stringify(WIDGETS.replace(/\\/g, "/")) + ";\n"
      + "export { default as React } from \"react\"; export { createRoot } from \"react-dom/client\"; export { flushSync } from \"react-dom\";",
    resolveDir: SRC, loader: "js",
  },
  bundle: true, format: "esm", platform: "browser", outfile: out,
  absWorkingDir: path.join(SRC, ".."), nodePaths: [path.join(SRC, "..", "node_modules")],
  loader: { ".js": "jsx", ".jsx": "jsx", ".css": "empty", ".png": "empty", ".svg": "empty" },
  define: { "import.meta.env": "{}", "process.env.NODE_ENV": "\"production\"" }, logLevel: "silent",
  plugins: [{
    name: "recharts-recorder",
    setup(b) {
      b.onResolve({ filter: /^recharts$/ }, () => ({ path: "recharts-stub", namespace: "stub" }));
      b.onLoad({ filter: /.*/, namespace: "stub" }, () => ({ contents: STUB, loader: "js", resolveDir: SRC }));
    },
  }],
});
const { DashboardWidgetCard, _formatChartTickTime, React, createRoot, flushSync } = await import("file:///" + out.replace(/\\/g, "/"));

const NOW = Date.now();
const RUN_START = NOW - 12 * 60 * 1000;          // a trigger run that started 12 min ago
const stampNoZone = (ms, frac) => new Date(ms).toISOString().slice(0, frac ? 23 : 19).replace("T", " ");
const HOLE = [NOW - 40 * 60 * 1000, NOW - 20 * 60 * 1000];
globalThis.__hole = false;
const inHole = (t) => globalThis.__hole && t >= HOLE[0] && t < HOLE[1];
const valueAt = (tag, t) => tag === "Run" ? (t >= RUN_START ? 1 : 0) : (tag === "Flow" ? 10 : tag === "Temp" ? 20 : 30) + (t / 1000 % 60);
const calls = [];
const fetchWidgetRows = async ({ fromUtc, toUtc, limit, tag, bucketSeconds, dataFilter }) => {
  calls.push({ tag, fromUtc, toUtc, limit, bucketSeconds, dataFilter });
  await sleep(30);
  const a = Date.parse(fromUtc) || NOW - 8 * 3600 * 1000;   // the store holds 8 h of 0.5 s data
  const b = Math.min(Date.parse(toUtc) || Date.now(), Date.now());   // the store serves rows up to now, like the backend
  const bs = Number(bucketSeconds) || 0;
  // the backend applies a data filter in SQL before its LIMIT; so does the fake store
  const c0 = dataFilter && dataFilter.conditions && dataFilter.conditions[0];
  const passes = (t) => !c0 || (c0.operator === "eq" ? valueAt(c0.tag_name, t) === Number(c0.value1) : true);
  const rows = [];
  if (bs > 0) {
    const step = bs * 1000;
    for (let t = Math.floor(a / step) * step; t <= b; t += step) if (!inHole(t) && passes(t)) rows.push({ ts: stampNoZone(t), gateway_id: "gw1", tag_name: tag, tag, value: valueAt(tag, t) });
  } else {
    for (let t = b; t >= a && rows.length < (limit || 2000); t -= 500) if (passes(t)) rows.unshift({ ts: stampNoZone(t, true), gateway_id: "gw1", tag_name: tag, tag, value: valueAt(tag, t) });
  }
  return rows.reverse().slice(0, Math.max(500, (limit || 2000) * 4));          // newest first, as App's fetcher returns
};
const live = [];
for (let t = NOW - 60000; t <= NOW; t += 500) for (const tag of ["Flow", "Temp", "Press", "Run"]) live.push({ ts: new Date(t).toISOString(), gateway_id: "gw1", tag, tag_name: tag, value: valueAt(tag, t) });

const widgetFor = (extra) => ({
  id: "w1", type: "line_chart", title: "three series",
  config: Object.assign({
    gateway_id: "gw1", tag_name: "Flow", readings_count: 100,
    series_extra: [
      { id: "s2", gateway_id: "gw1", tag_name: "Temp", chart_type: "line", color: "#c00" },
      { id: "s3", gateway_id: "gw1", tag_name: "Press", chart_type: "line", color: "#0c0" },
    ],
  }, extra),
});


let fails = 0;
const check = (name, ok, detail = "") => {
  console.log("  %s %s%s", (name + " ".repeat(74)).slice(0, 74), ok ? "PASS" : "FAIL", (!ok && detail) ? "  - " + detail : "");
  if (!ok) fails += 1;
};
process.env.TZ = "UTC";
const tsOf = (row) => row.tsMs ?? (Number.isFinite(Number(row.ts)) ? Number(row.ts) : Date.parse(String(row.ts || "")));
// Each render is its own widget, as on a real dashboard: the chart keeps a
// per-widget cache of its last rows, so a shared id would show the PREVIOUS
// case's buckets until the new fetch lands.
let _wseq = 0;
let _wid = "w0";
const mk = (type, cfg, two) => ({ id: _wid, type, title: "agg", config: Object.assign({ gateway_id: "gw1", tag_name: "Flow", readings_count: 100 },
  two ? { series_extra: [{ id: "s2", gateway_id: "gw1", tag_name: "Temp", chart_type: type === "bar_chart" ? "bar" : "line", color: "#c00" }] } : {}, cfg) });
function snapshot(recs) {
  const charts = recs.filter((r) => /Chart$/.test(r.kind) && Array.isArray(r.props.data));
  const data = charts.length ? charts[charts.length - 1].props.data : [];
  const marks = recs.filter((r) => ["Line", "Area", "Bar"].includes(r.kind));
  const keys = [...new Set(marks.map((m) => String(m.props.dataKey)))];
  const xs = recs.filter((r) => r.kind === "XAxis"); const x = xs.length ? xs[xs.length - 1].props : {};
  const ys = recs.filter((r) => r.kind === "YAxis"); const y = ys.length ? ys[ys.length - 1].props : {};
  const first = keys[0] || "value";
  const pts = data.filter((r) => r[first] != null).map(tsOf).filter(Number.isFinite).sort((a, b) => a - b);
  const gaps = []; for (let i = 1; i < pts.length; i++) gaps.push(pts[i] - pts[i - 1]); gaps.sort((a, b) => a - b);
  return { data, keys, pts, spacing: gaps.length ? gaps[Math.floor(gaps.length / 2)] / 1000 : null, x, y, ys };
}
const statCalls = [];
async function fetchWidgetStats({ tag }) { statCalls.push(tag); await sleep(20); return [{ tag, tag_name: tag, max: 250, min: -40, avg: 10, latest: 77 }]; }
async function show(type, cfg, two, settle = 1800) {
  _wid = "w" + (++_wseq);
  const el = document.createElement("div"); document.body.appendChild(el);
  const root = createRoot(el);
  globalThis.__rec = []; calls.length = 0;
  const node = (c) => React.createElement(DashboardWidgetCard, { widget: mk(type, c, two), dataLogView: live, tagRows: [], tagRowsByGateway: { gw1: [] },
    gatewayIntervalsById: { gw1: 500 }, fetchWidgetRows, fetchWidgetStats, formatTagForDisplay: (t) => t });
  root.render(node(cfg)); await sleep(settle);
  return { root, el, node, snap: () => snapshot(globalThis.__rec) };
}

// ---------------------------------------------------------------- 1. the matrix
console.log("\n[one bar / point per bucket, every type, series count, period and mode]");
const AGGS = [["2s", 2, {}], ["1m", 60, {}], ["5m", 300, {}], ["15m", 900, {}], ["custom", 45, { bucket_custom_seconds: 45 }]];
const MODES = [["rolling", {}, 0], ["15m", { query_time_filter_preset: "15m" }, 900], ["1h", { query_time_filter_preset: "1h" }, 3600], ["6h", { query_time_filter_preset: "6h" }, 21600]];
let combos = 0; const bad = [];
for (const type of ["bar_chart", "line_chart", "line_area_chart"]) {
  for (const two of [false, true]) {
    for (const [aname, asec, aextra] of AGGS) {
      for (const [mname, mextra, wsec] of MODES) {
        if (wsec && wsec / asec < 3) continue;                       // fewer than 3 buckets in the window
        if (wsec && wsec / asec > 2000) continue;                     // 2 s over 6 h: more buckets than the draw cap - covered by thinning
        const cfg = Object.assign({ bucket_interval: aname }, aextra, mextra);
        const v = await show(type, cfg, two, 1200);
        const s = v.snap(); v.root.unmount(); v.el.remove();
        combos += 1;
        const label = `${type}/${two ? 2 : 1} series/${aname}/${mname}`;
        if (s.spacing !== asec) { bad.push(`${label}: spacing ${s.spacing}s (want ${asec})`); continue; }
        if (!wsec && s.pts.length > 100 + 2) bad.push(`${label}: rolling drew ${s.pts.length} buckets (Reading points 100)`);
        const reqs = calls.filter((c) => c.tag === "Flow");
        if (!wsec && reqs.some((c) => !c.fromUtc)) bad.push(`${label}: rolling fetched all of history`);
        // 2026-11-16: a bar is centred on its stamp, so a bar chart's time
        // axis carries half a bucket of room at both ends (the edge bars
        // used to straddle the axis lines); a line chart carries none.
        const barRoom = type === "bar_chart" && two ? asec * 500 : 0;
        if (wsec && Array.isArray(s.x.domain) && typeof s.x.domain[0] === "number" && s.pts.length) {
          const lead = s.pts[0] - s.x.domain[0] - barRoom;
          const tail = s.x.domain[1] - s.pts[s.pts.length - 1] - barRoom;
          if (barRoom && (lead < -1 || tail < -1)) bad.push(`${label}: an edge bar sits outside the plot (lead ${lead} ms, tail ${tail} ms)`);
          if (lead > 500) {
            bad.push(`${label}: ${Math.round(lead / 1000)}s blank at the start`);
            if (process.env.TN_DEBUG) {
              const f = (q) => typeof q === "number" ? new Date(q).toISOString().slice(11, 19) : String(q);
              console.log("    DEBUG", label, "req:", calls.filter((c) => c.tag === "Flow").map((c) => `${f(Date.parse(c.fromUtc))}..${c.toUtc ? f(Date.parse(c.toUtc)) : "-"} b=${c.bucketSeconds} lim=${c.limit}`).join(" "),
                "| rows", s.data.length, "pts", s.pts.length, "first", f(s.pts[0]), "2nd", f(s.pts[1]), "3rd", f(s.pts[2]), "last", f(s.pts[s.pts.length - 1]), "domain", s.x.domain.map(f).join(".."), "now", f(Date.now()),
                "| ptsAfterReqEnd:", s.pts.filter((q) => q > Date.parse((calls.find((c) => c.tag === "Flow") || {}).toUtc || 0)).length, "liveNewest", f(Math.max(...live.map((r) => Date.parse(r.ts)))), "| gaps>2s:", (() => { const g = []; for (let i = 1; i < s.pts.length; i++) if (s.pts[i] - s.pts[i - 1] > 2000) g.push(`${f(s.pts[i - 1])}->${f(s.pts[i])}`); return g.slice(0, 4).join(","); })());
            }
          }
          if (tail > 500) bad.push(`${label}: ${Math.round(tail / 1000)}s blank at the end`);
        }
      }
    }
  }
}
check(`one bar/point per bucket, rolling capped, no blank ends (${combos} combinations)`, !bad.length, bad.slice(0, 5).join(" | "));

// ---------------------------------------------------------------- 1b. the reading count, aggregated, two series
console.log("\n[no preset: the reading count is a count of BUCKETS, with two series too]");
{
  // Reported with a screenshot (2026-11-16): two aggregated series, 50 points
  // at 5 s, drew 8 bars over 35 s - the recency trim counted gateway ticks.
  const bad2 = [];
  for (const type of ["bar_chart", "line_chart"]) for (const two of [false, true]) for (const cfg of [{ bucket_interval: "5s" }, { query_group_interval: "5s" }]) {
    const v = await show(type, Object.assign({ readings_count: 50 }, cfg), two, 1500);
    const s = v.snap(); v.root.unmount(); v.el.remove();
    const label = `${type}/${two ? 2 : 1} series/${Object.keys(cfg)[0]}`;
    if (s.spacing !== 5) bad2.push(`${label}: spacing ${s.spacing}s`);
    else if (s.pts.length < 48 || s.pts.length > 52) bad2.push(`${label}: ${s.pts.length} points (want 50)`);
  }
  check("50 points at 5 s draws 50 buckets, one or two series, bar or line", !bad2.length, bad2.join(" | "));
  // a 15 min window asked for 50 points buckets to 20 s -> 46 points (was 30 s -> 31)
  const v = await show("line_chart", { readings_count: 50, query_time_filter_preset: "15m" }, false, 1500);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  check("15 min / 50 points buckets to 20 s and draws the whole window", s.spacing === 20 && s.pts.length >= 44 && s.pts.length <= 47, `spacing ${s.spacing}s pts ${s.pts.length}`);
  // mixed line + bar on the time axis: the bar gets its room as well
  const m = await show("line_chart", { readings_count: 50, query_time_filter_preset: "15m", bucket_interval: "1m",
    series_extra: [{ id: "s2", gateway_id: "gw1", tag_name: "Temp", chart_type: "bar", color: "#c00" }] }, false, 1500);
  const ms = m.snap(); m.root.unmount(); m.el.remove();
  const room = ms.pts.length && Array.isArray(ms.x.domain) ? [ms.pts[0] - ms.x.domain[0], ms.x.domain[1] - ms.pts[ms.pts.length - 1]] : [0, 0];
  check("a line + bar chart keeps half a bucket of room at both ends for the bars", ms.spacing === 60 && room[0] >= 29000 && room[1] >= 29000, `spacing ${ms.spacing}s room ${room.map((q) => Math.round(q / 1000)).join("/")}s`);
  // Reported with a screenshot (2026-11-16): "show gaps" off puts a live line
  // / area chart on the categorical axis, which carried 6 px of bar padding -
  // a blank strip at both Y axes. Only bars keep the room.
  for (const [type, want] of [["line_area_chart", 0], ["line_chart", 0], ["bar_chart", 6]]) {
    const g = await show(type, { readings_count: 50, show_gaps: false }, true, 1200);
    const xs = globalThis.__rec.filter((r) => r.kind === "XAxis"); const xp = xs.length ? xs[xs.length - 1].props : {};
    g.root.unmount(); g.el.remove();
    check(`show gaps off, ${type}: the axis padding is ${want} px (data starts and ends on the Y axes)`,
      xp.dataKey === "idx" && Number(xp.padding?.left || 0) === want && Number(xp.padding?.right || 0) === want, JSON.stringify({ dataKey: String(xp.dataKey), padding: xp.padding }));
  }
}

// ---------------------------------------------------------------- 1c. the computed pie
console.log("\n[computed pie: the legend names the condition, the ring stays in its box, size is an option]");
{
  // Reported with a screenshot (2026-11-16): every legend entry read "Item",
  // the ring drew over the card header (its buttons could not be clicked)
  // and there was no size control.
  const rules = [{ id: "r1", tag_name: "Flow", operator: "gt", value1: 10, aggregation: "count" },
                 { id: "r2", tag_name: "Temp", operator: "between", value1: 5, value2: 9, aggregation: "count" },
                 { id: "r3", tag_name: "Press", operator: "any", aggregation: "count", label: "All pressure" }];
  const pie = (extra) => ({ id: "pie" + (++_wseq), type: "pie_chart", title: "pie", w: 6, h: 6, config: Object.assign({ data_source_type: "computed", compute_rules: rules, readings_count: 100, pie_legend_layout: "bottom" }, extra) });
  const showPie = async (extra) => {
    const el = document.createElement("div"); document.body.appendChild(el); const root = createRoot(el); globalThis.__rec = [];
    root.render(React.createElement(DashboardWidgetCard, { widget: pie(extra), dataLogView: live, tagRows: [], tagRowsByGateway: { gw1: [] },
      gatewayIntervalsById: { gw1: 500 }, fetchWidgetRows, fetchWidgetStats, formatTagForDisplay: (t) => t }));
    await sleep(1200);
    const txt = el.textContent || ""; const pies = globalThis.__rec.filter((r) => r.kind === "Pie"); const p = pies.length ? pies[pies.length - 1].props : {};
    root.unmount(); el.remove(); return { txt, p };
  };
  const a = await showPie({});
  check("an unlabelled rule is named by its condition in the legend", a.txt.includes("Flow > 10") && a.txt.includes("Temp 5..9") && a.txt.includes("All pressure") && !/Item \d/.test(a.txt), a.txt.slice(0, 160));
  check("  default ring: outer radius 94 %", String(a.p.outerRadius) === "94%", String(a.p.outerRadius));
  const b = await showPie({ pie_size: 60 });
  check("  Chart size 60 %: the ring is scaled inside its box", String(b.p.outerRadius) === "56%" && String(b.p.innerRadius) === "35%", `${b.p.outerRadius} / ${b.p.innerRadius}`);
  // Reported (2026-11-16): "data aggregation is not working". The server rule
  // stats (raw rows, no grouping / last N) were preferred whenever they
  // answered, so Time grouping and Row selection did nothing on an edge.
  const ruleStatCalls = [];
  const fetchWidgetRuleStats = async ({ rules: rs }) => { ruleStatCalls.push(rs); await sleep(20); return rs.map((r, i) => ({ id: r.id, label: r.label || "", value: 999, color: "#123456", aggregation: "count" })); };
  const showPie2 = async (extra, stats) => {
    const el = document.createElement("div"); document.body.appendChild(el); const root = createRoot(el); globalThis.__rec = []; calls.length = 0;
    root.render(React.createElement(DashboardWidgetCard, { widget: pie(extra), dataLogView: live, tagRows: [], tagRowsByGateway: { gw1: [] },
      gatewayIntervalsById: { gw1: 500 }, fetchWidgetRows, fetchWidgetStats: stats || fetchWidgetStats, fetchWidgetRuleStats, formatTagForDisplay: (t) => t }));
    await sleep(1500);
    const txt = el.textContent || ""; root.unmount(); el.remove(); return txt;
  };
  const plain = await showPie2({ query_time_filter_preset: "15m" });
  check("plain computed pie over 15 min: the server rule stats answer (999 per rule)", /999\.00/.test(plain), plain.slice(0, 120));
  const grouped = await showPie2({ query_time_filter_preset: "15m", query_group_interval: "1m", compute_rules: [Object.assign({}, rules[0], { value1: 5 })] });   // a minute's last value sits on the boundary: exactly 10
  const n = Number((grouped.match(/(\d+)\.00/) || [])[1]);
  const req = calls.find((c) => c.tag === "Flow") || {};
  check("  with Time grouping 1 min: one row per minute is fetched and the GROUPS are counted (not 999)", n >= 14 && n <= 17 && Number(req.bucketSeconds) === 60, `count ${n} bucket ${req.bucketSeconds}`);
  const lastN = await showPie2({ query_time_filter_preset: "15m", query_row_selection: "last_n", query_row_limit: 20, compute_rules: [rules[0]] });
  const n2 = Number((lastN.match(/(\d+)\.00/) || [])[1]);
  check("  with Row selection last 20: the 20 newest rows are evaluated (not 999)", n2 >= 1 && n2 <= 20, `count ${n2}`);
  const directStats = async ({ tag }) => { await sleep(20); return [{ tag, tag_name: tag, count: 5000, sum: 1, avg: 1, min: 0, max: 2, latest: 1 }]; };
  const direct = await showPie2({ data_source_type: "tag_direct", compute_rules: [], gateway_id: "gw1", tag_name: "Flow", query_time_filter_preset: "15m", query_group_interval: "1m", query_result_aggregation: "count" }, directStats);
  const n3 = Number((direct.match(/(\d+)\.00/) || [])[1]);
  check("  a direct pie with Time grouping 1 min counts the groups, not the server's raw 5000", n3 >= 14 && n3 <= 17, `count ${n3}`);
  const css = (await import("node:fs")).readFileSync(path.join(SRC, "components", "Dashboard", "dashboard.css"), "utf-8");
  const canvas = css.slice(css.indexOf(".dashboard-donut-canvas {"), css.indexOf("}", css.indexOf(".dashboard-donut-canvas {")));
  check("  the donut canvas is no longer scaled past its box (it covered the header)", !/transform:\s*scale/.test(canvas), canvas.replace(/\s+/g, " "));
}

// ---------------------------------------------------------------- 1d. the data filter on a widget
console.log("\n[the data filter: other tags' values at the same time]");
{
  // 2026-11-16, from the owner: a filter "based on a tag value ... the same
  // timestamp". The server applies it to every historian request; the live
  // rows obey it in the browser. Run = 1 only from RUN_START in the live buffer.
  const flt = { logic: "all", conditions: [{ gateway_id: "", tag_name: "Run", operator: "eq", value1: 1 }] };
  const v = await show("line_chart", { readings_count: 100, data_filter: flt }, false, 1500);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  const req = calls.find((c) => c.tag === "Flow") || {};
  check("every historian request of the widget carries the filter", req.dataFilter && req.dataFilter.conditions && req.dataFilter.conditions[0].tag_name === "Run", JSON.stringify(req.dataFilter || null));
  const before = s.pts.filter((t) => t < RUN_START - 1).length;
  check("  live rows read while Run = 0 are not drawn", s.pts.length > 0 && before === 0, `${before} points before the run started of ${s.pts.length}`);
  const w = await show("line_chart", { readings_count: 100, data_filter: { logic: "all", conditions: [{ tag_name: "Run", operator: "eq", value1: 0 }] } }, false, 1500);
  const s2 = w.snap(); w.root.unmount(); w.el.remove();
  check("  and Run = 0 keeps only the rows before the run (the two filters are disjoint)", s2.pts.length > 0 && s2.pts.every((t) => t < RUN_START + 1), `${s2.pts.length} pts`);
}

// ---------------------------------------------------------------- 1e. tick labels are cheap
console.log("\n[tick labels: cached formatters]");
{
  // 2026-11-16, DevTools profile on the window with seven widgets: 43% of all
  // renderer time was the X tick formatter (a new Intl.DateTimeFormat per
  // call, thousands of calls a second); 9 frames a second.
  const t0 = performance.now();
  for (let i = 0; i < 50000; i += 1) _formatChartTickTime(1700000000000 + (i % 2000) * 500, i % 2 ? "hh_mm_ss" : "date_hh_mm");
  const ms = performance.now() - t0;
  check("50 000 tick labels format in under 400 ms (was seconds: one Intl formatter per call)", ms < 400, `${Math.round(ms)} ms`);
  check("  and the label is unchanged", /^\d\d:\d\d:\d\d$/.test(_formatChartTickTime(1700000000000, "hh_mm_ss")), _formatChartTickTime(1700000000000, "hh_mm_ss"));
}

// ---------------------------------------------------------------- 2. a change applies at once
console.log("\n[changing the aggregation re-draws at once]");
for (const [type, label, a, b, want] of [
  ["bar_chart", "1 h bar: none -> 5 min", { query_time_filter_preset: "1h" }, { query_time_filter_preset: "1h", bucket_interval: "5m" }, 300],
  ["bar_chart", "1 h bar: 1 min -> 5 min", { query_time_filter_preset: "1h", bucket_interval: "1m" }, { query_time_filter_preset: "1h", bucket_interval: "5m" }, 300],
  ["line_chart", "6 h line: 5 min -> 15 min", { query_time_filter_preset: "6h", bucket_interval: "5m" }, { query_time_filter_preset: "6h", bucket_interval: "15m" }, 900],
  ["line_area_chart", "rolling area: 2 s -> custom 7 s", { bucket_interval: "2s" }, { bucket_interval: "custom", bucket_custom_seconds: 7 }, 7],
]) {
  const v = await show(type, a, false);
  globalThis.__rec = [];
  v.root.render(v.node(b)); await sleep(1500);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  check(`${label}: drawn at the new period within 1.5 s`, s.spacing === want, `spacing ${s.spacing}s`);
}
{
  const v = await show("bar_chart", { query_time_filter_preset: "1h", bucket_interval: "5m" }, false);
  const n0 = calls.length; globalThis.__rec = [];
  v.root.render(v.node({ query_time_filter_preset: "1h", bucket_interval: "5m", bucket_aggregation: "max" })); await sleep(1500);
  const re = calls.slice(n0).filter((c) => c.tag === "Flow").length; v.root.unmount(); v.el.remove();
  check("changing 'Using' (avg -> max) fetches again at once", re >= 1, `${re} fetches`);
}
{
  const v = await show("bar_chart", { query_time_filter_preset: "1h", query_group_interval: "5m" }, false);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  const req = calls.find((c) => c.tag === "Flow");
  check("a saved 'Time grouping 5 min' on a chart is its 'Aggregate every' (one grouping, server side)",
    s.spacing === 300 && req && req.bucketSeconds === 300, JSON.stringify({ spacing: s.spacing, bucket: req && req.bucketSeconds }));
}

// ---------------------------------------------------------------- 3. Y axis per bound
console.log("\n[each Y bound on its own: Auto / Manual / Tag]");
{
  const v = await show("line_chart", { query_time_filter_preset: "1h", bucket_interval: "1m", y_min_src: "manual", y_min: 0, y_max_src: "auto" }, false);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  const d = s.y.domain || [];
  check("Min manual 0, Max auto: the axis starts at 0 and the top follows the data", d[0] === 0 && typeof d[1] === "function", JSON.stringify(d.map((q) => typeof q)));
}
{
  const v = await show("line_chart", { query_time_filter_preset: "1h", bucket_interval: "1m", y_max_src: "tag", y_max_tag: "gw1::Limit", y_max_tag_agg: "max",
    y_min_src: "tag", y_min_tag: "gw1::Flow", y_min_tag_agg: "latest" }, false, 2200);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  const d = s.y.domain || [];
  const latestFlow = [...live].reverse().find((r) => r.tag === "Flow").value;
  check("Max linked to a tag's maximum in the window (250)", d[1] === 250, JSON.stringify(d));
  check("  Min linked to a tag's latest live value", d[0] === latestFlow, JSON.stringify({ d0: d[0], latestFlow }));
}
{
  const v = await show("line_chart", { y_axis_mode: "manual", y_min: 10, y_max: 90, y_tick_step: 20 }, false);
  const s = v.snap(); v.root.unmount(); v.el.remove();
  const yl = s.ys[0] || { props: {} };
  check("a widget saved before this (axis Manual 10..90, step 20) draws exactly as before",
    JSON.stringify(yl.props.domain) === "[10,90]" && JSON.stringify(yl.props.ticks) === "[10,30,50,70,90]",
    JSON.stringify({ domain: yl.props.domain, ticks: yl.props.ticks }));
}

// ---------------------------------------------------------------- 4. speed
console.log("\n[speed at 1 000 - 5 000 points]");
function liveRows(n) { const out = []; const t0 = Date.now() - n * 500; for (let i = 0; i < n; i++) for (const tag of ["Flow", "Temp", "Press"]) out.push({ ts: new Date(t0 + i * 500).toISOString(), gateway_id: "gw1", tag, tag_name: tag, value: i % 50 }); return out; }
async function cost(cfg, n) {
  _wid = "w" + (++_wseq);
  const el = document.createElement("div"); document.body.appendChild(el);
  const root = createRoot(el);
  let lv = liveRows(n);
  const props = (rows) => ({ widget: mk("line_chart", Object.assign({ readings_count: n, series_extra: [
    { id: "s2", gateway_id: "gw1", tag_name: "Temp", chart_type: "line" }, { id: "s3", gateway_id: "gw1", tag_name: "Press", chart_type: "line" }] }, cfg), false),
    dataLogView: rows, tagRows: [], tagRowsByGateway: { gw1: [] }, gatewayIntervalsById: { gw1: 500 }, fetchWidgetRows, formatTagForDisplay: (t) => t });
  globalThis.__rec = [];
  flushSync(() => root.render(React.createElement(DashboardWidgetCard, props(lv))));
  await sleep(1500);
  const ticks = [];
  for (let k = 0; k < 12; k++) {
    const now = Date.now();
    lv = lv.slice(3).concat(["Flow", "Temp", "Press"].map((tag) => ({ ts: new Date(now).toISOString(), gateway_id: "gw1", tag, tag_name: tag, value: k })));
    const t = performance.now(); flushSync(() => root.render(React.createElement(DashboardWidgetCard, props(lv)))); ticks.push(performance.now() - t);
    await sleep(30);
  }
  ticks.sort((a, b) => a - b);
  const recs = globalThis.__rec.filter((r) => /Chart$/.test(r.kind) && Array.isArray(r.props.data));
  const drawn = recs.length ? recs[recs.length - 1].props.data.length : 0;
  root.unmount(); el.remove();
  return { med: ticks[6], drawn };
}
for (const n of [1000, 5000]) {
  const r = await cost({}, n);
  check(`rolling, 3 series, ${n} points: under 20 ms per live update`, r.med < 20, `${r.med.toFixed(1)} ms`);
  check("  and at most 1 500 rows drawn", r.drawn <= 1500 && r.drawn > 0, `${r.drawn} rows`);
}

// ---------------------------------------------------------------- 5. the editor
const designer = (await import("node:fs")).readFileSync(path.join(SRC, "components", "Dashboard", "DashboardDesigner.jsx"), "utf8");
check("changing an existing widget's type keeps its card size",
  designer.includes("w: editingId && Number.isFinite(Number(prev.w)) ? prev.w : t.defaultSize.w,")
  && designer.includes("h: editingId && Number.isFinite(Number(prev.h)) ? prev.h : t.defaultSize.h,"));
check("'Aggregate every' offers 2 s and Custom seconds",
  designer.includes('{ value: "2s", label: "2 seconds" }') && designer.includes('{ value: "custom", label: "Custom (seconds)" }')
  && designer.includes("bucket_custom_seconds"));
check("the axis editor offers Auto / Manual / Tag on Min, Max and Tick step",
  designer.includes('label="Min"') && designer.includes('label="Max"') && designer.includes('label="Tick step"')
  && designer.includes('<option value="tag">Tag</option>'));

console.log(fails ? "\nRESULT: FAIL (" + fails + ")" : "\nRESULT: PASS");
process.exit(fails ? 1 : 0);
