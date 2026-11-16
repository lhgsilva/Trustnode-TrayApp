/**
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
    contents: "export { DashboardWidgetCard } from " + JSON.stringify(WIDGETS.replace(/\\/g, "/")) + ";\n"
      + "export { default as React } from \"react\"; export { createRoot } from \"react-dom/client\";",
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
const { DashboardWidgetCard, React, createRoot } = await import("file:///" + out.replace(/\\/g, "/"));

const NOW = Date.now();
const RUN_START = NOW - 12 * 60 * 1000;          // a trigger run that started 12 min ago
const stampNoZone = (ms, frac) => new Date(ms).toISOString().slice(0, frac ? 23 : 19).replace("T", " ");
const HOLE = [NOW - 40 * 60 * 1000, NOW - 20 * 60 * 1000];
globalThis.__hole = false;
const inHole = (t) => globalThis.__hole && t >= HOLE[0] && t < HOLE[1];
const valueAt = (tag, t) => tag === "Run" ? (t >= RUN_START ? 1 : 0) : (tag === "Flow" ? 10 : tag === "Temp" ? 20 : 30) + (t / 1000 % 60);
const calls = [];
const fetchWidgetRows = async ({ fromUtc, toUtc, limit, tag, bucketSeconds }) => {
  calls.push({ tag, fromUtc, toUtc, limit, bucketSeconds });
  await sleep(30);
  const a = Date.parse(fromUtc) || NOW - 8 * 3600 * 1000;   // the store holds 8 h of 0.5 s data
  const b = Math.min(Date.parse(toUtc) || Date.now(), Date.now());   // the store serves rows up to now, like the backend
  const bs = Number(bucketSeconds) || 0;
  const rows = [];
  if (bs > 0) {
    const step = bs * 1000;
    for (let t = Math.floor(a / step) * step; t <= b; t += step) if (!inHole(t)) rows.push({ ts: stampNoZone(t), gateway_id: "gw1", tag_name: tag, tag, value: valueAt(tag, t) });
  } else {
    for (let t = b; t >= a && rows.length < (limit || 2000); t -= 500) rows.unshift({ ts: stampNoZone(t, true), gateway_id: "gw1", tag_name: tag, tag, value: valueAt(tag, t) });
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
  console.log("  %s %s%s", (name + " ".repeat(72)).slice(0, 72), ok ? "PASS" : "FAIL", (!ok && detail) ? "  - " + detail : "");
  if (!ok) fails += 1;
};
const tsOf = (row) => row.tsMs ?? Date.parse(String(row.ts || ""));
async function render(extra, settle = 1500) {
  const root = createRoot(document.getElementById("root"));
  globalThis.__rec = [];
  calls.length = 0;
  root.render(React.createElement(DashboardWidgetCard, {
    widget: widgetFor(extra), dataLogView: live, tagRows: [], tagRowsByGateway: { gw1: [] },
    gatewayIntervalsById: { gw1: 500 }, fetchWidgetRows, formatTagForDisplay: (t) => t,
  }));
  await sleep(settle);
  const recs = globalThis.__rec;
  const charts = recs.filter((r) => /Chart$/.test(r.kind) && Array.isArray(r.props.data));
  const xs = recs.filter((r) => r.kind === "XAxis");
  const lines = recs.filter((r) => r.kind === "Line" || r.kind === "Area");
  root.unmount();
  const data = charts.length ? charts[charts.length - 1].props.data : [];
  const keys = [...new Set(lines.slice(-3).map((l) => String(l.props.dataKey)))];
  const per = {};
  for (const k of keys) {
    const pts = data.filter((r) => r[k] !== null && r[k] !== undefined).map(tsOf);
    // a segment needs two consecutive drawable rows
    let segs = 0;
    for (let i = 1; i < data.length; i += 1) if (data[i][k] != null && data[i - 1][k] != null) segs += 1;
    per[k] = { n: pts.length, from: Math.min(...pts), to: Math.max(...pts), segs, nulls: data.length - pts.length };
  }
  const x = xs.length ? xs[xs.length - 1].props.domain : null;
  return { data, per, keys, x };
}
const near = (a, b, tol) => Math.abs(a - b) <= tol;

for (const tz of ["UTC", "America/Sao_Paulo"]) {
  process.env.TZ = tz;
  console.log("\n[" + tz + ", offset " + new Date().getTimezoneOffset() + " min]");
  for (const [preset, ms] of [["15m", 15 * 60e3], ["1h", 3600e3], ["6h", 6 * 3600e3]]) {
    const r = await render({ query_time_filter_preset: preset });
    const ok3 = r.keys.length === 3;
    check(preset + ": three series are drawn", ok3, JSON.stringify(r.keys));
    const bad = Object.entries(r.per).filter(([, v]) => v.from > NOW - ms + ms * 0.05 || v.to < NOW - ms * 0.06 - 60e3);
    check("  every series spans the window", ok3 && !bad.length, JSON.stringify(r.per));
    const unjoined = Object.entries(r.per).filter(([, v]) => v.segs < v.n - 2);
    check("  and its points join into a line (no gap between every pair)", ok3 && !unjoined.length,
      JSON.stringify(Object.fromEntries(Object.entries(r.per).map(([k, v]) => [k, { pts: v.n, segments: v.segs, nulls: v.nulls }]))));
    // A live window ends at its newest point, which on a bucketed window is
    // the start of the bucket being filled - up to one bucket before now.
    const _bms = (calls.find((c) => c.tag === "Flow")?.bucketSeconds || 0) * 1000;
    // The axis may start at the first point when it lands up to three buckets
    // into the window (bucket rounding, not a gap) - 2026-11-16 edge rule.
    check("  the time axis is the window", Array.isArray(r.x) && (r.x[1] - r.x[0]) <= ms + 1000
      && (r.x[1] - r.x[0]) >= ms - Math.max(120e3, 3 * _bms + 1000)
      && near(r.x[1], Date.now(), Math.max(120e3, _bms + 5000)), JSON.stringify(r.x));
    // 2026-11-16, reported with screenshots: a blank strip on the right. The
    // axis must end where the data ends, and the data must reach the newest
    // live reading (the bucket being filled right now included).
    const lastPt = Math.max(...Object.values(r.per).map((v) => v.to));
    const bucketMs = (calls.find((c) => c.tag === "Flow")?.bucketSeconds || 0) * 1000;
    check("  no blank strip on the right: the axis ends at the newest point",
      Array.isArray(r.x) && near(r.x[1], lastPt, 1000), JSON.stringify({ axisEnd: r.x && r.x[1], lastPt }));
    check("  and the newest point is the bucket being filled now",
      // against the clock NOW, not the script's start: the second timezone
      // pass runs a minute later and its newest bucket is rightly newer
      near(lastPt, Date.now(), Math.max(2000, 2 * bucketMs + 2000)), JSON.stringify({ lastPt: new Date(lastPt).toISOString(), now: new Date().toISOString(), bucketMs }));
  }
  const g = await render({ query_time_filter_preset: "1h", show_gaps: false });
  check("'Show gaps' off: no gap rows at all", Object.values(g.per).every((v) => v.nulls === 0), JSON.stringify(g.per));
  globalThis.__hole = true;
  const h = await render({ query_time_filter_preset: "1h" });
  globalThis.__hole = false;
  const breaks = Object.values(h.per).map((v) => v.n - 1 - v.segs);
  check("a real 20-minute collection hole still breaks every line once", breaks.length === 3 && breaks.every((b) => b === 1),
    JSON.stringify(breaks));
  const t = await render({ window_mode: "trigger", window_tag_name: "Run", window_gateway_id: "gw1", window_operator: "==",
    window_value: "1", window_stop_tag: "__start_condition_ends__" }, 2500);
  const tfrom = Math.min(...Object.values(t.per).map((v) => v.from));
  check("trigger: the series start at the run's start (12 min ago), not at the first live reading",
    t.keys.length === 3 && Object.values(t.per).every((v) => near(v.from, RUN_START, 20e3)),
    JSON.stringify({ runStart: new Date(RUN_START).toISOString(), firstPoint: new Date(tfrom).toISOString() }));
  check("  and the time axis starts at the run's start", Array.isArray(t.x) && near(t.x[0], RUN_START, 20e3), JSON.stringify(t.x));
  check("  an open run's axis ends at its newest point (no blank strip)", Array.isArray(t.x) && t.x[1] === "dataMax", JSON.stringify(t.x));
  const roll = await render({});
  check("rolling (no window) is unchanged: last readings, axis follows the data",
    roll.keys.length === 3 && JSON.stringify(roll.x) === JSON.stringify(["dataMin", "dataMax"]), JSON.stringify(roll.x));
}
console.log(fails ? "\nRESULT: FAIL (" + fails + ")" : "\nRESULT: PASS");
process.exit(fails ? 1 : 0);
