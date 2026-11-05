/**
 * A windowed multi-series chart stays live at the gateway interval WITHOUT
 * re-querying its window every tick.
 *
 * 2026-11-05, reported at a customer: on every preset (15 min, 30 min, 1 h,
 * 6 h) a 3-series chart showed "only the last few items". Measured in the
 * code: the refresh timer fires every gateway interval (0.5 s there); on each
 * tick the primary re-queried its whole window and every extra series did
 * too - with no in-flight guard, and a failed query replacing the series
 * with []. The queries queued, expired in the browser, and the chart fell
 * back to the live tail.
 *
 * This renders the REAL widget (jsdom + esbuild) with a fake fetcher that
 * answers in 250 ms, a 200 ms gateway interval, and counts what it asks for:
 *
 *   1. over 3 s each series fetches its window at most twice (the old code:
 *      ~10 for the primary, ~15 per extra);
 *   2. every fetch carries the preset's window and the SAME bucket for all
 *      three series;
 *   3. live rows arriving every tick for 2 s cause NO further window fetch;
 *   4. a config change (Reading points) fetches again at once;
 *   5. the same holds for "6h";
 *   6. trigger mode: a run already under way when the chart opens is found in
 *      the trigger tag's history, and the series are loaded from its start.
 *
 *     node scripts/test_dashboard_charts_live_light.mjs
 *     TN_WIDGETS_FILE=<path> ... renders another copy of DashboardWidgets.jsx
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "frontend", "src");
const WIDGETS = process.env.TN_WIDGETS_FILE || path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx");
let fails = 0;
const check = (name, ok, detail = "") => {
  console.log("  %s %s%s", (name + " ".repeat(70)).slice(0, 70), ok ? "PASS" : "FAIL",
    (!ok && detail) ? "  - " + detail : "");
  if (!ok) fails += 1;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
console.log("a windowed multi-series chart is live without re-querying its window");

// --- a browser for React -----------------------------------------------------
const { JSDOM } = await import("file:///" + path.join(SRC, "..", "node_modules", "jsdom", "lib", "api.js").replace(/\\/g, "/"));
const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", { pretendToBeVisual: true });
const w = dom.window;
for (const k of ["window", "document", "navigator", "HTMLElement", "Element", "Node", "SVGElement", "getComputedStyle",
  "requestAnimationFrame", "cancelAnimationFrame", "localStorage", "sessionStorage", "MutationObserver", "Event", "CustomEvent"]) {
  try { globalThis[k] = w[k]; } catch (_) { /* read-only global */ }
}
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
globalThis.IS_REACT_ACT_ENVIRONMENT = false;

// --- the real widget ----------------------------------------------------------
const esbuild = await import("file:///" + path.join(SRC, "..", "node_modules", "esbuild", "lib", "main.js").replace(/\\/g, "/"));
const out = path.join(os.tmpdir(), "tn_widget_" + process.pid + ".mjs");
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
});
const { DashboardWidgetCard, React, createRoot } = await import("file:///" + out.replace(/\\/g, "/"));

// --- a store that answers in 250 ms ------------------------------------------
const calls = [];
const RUN_START = Math.floor(Date.now() / 1000) * 1000 - 10 * 60 * 1000;
const stamp = (ms) => new Date(ms).toISOString().slice(0, 19).replace("T", " ");
const fetchWidgetRows = async ({ fromUtc, toUtc, limit, gateway, tag, bucketSeconds }) => {
  calls.push({ tag, fromUtc, toUtc, limit, gateway, bucketSeconds: Number(bucketSeconds) || 0, at: Date.now() });
  await sleep(250);
  const a = Date.parse(fromUtc) || Date.now() - 900000;
  const b = Date.parse(toUtc) || Date.now();
  const step = (Number(bucketSeconds) || 1) * 1000;
  const rows = [];
  for (let t = a; t <= b && rows.length < 20000; t += step) {
    // the trigger tag "Run": 0, then 1 from RUN_START on (a run under way)
    const value = tag === "Run" ? (t >= RUN_START ? 1 : 0) : 1;
    rows.push({ ts: stamp(t), gateway_id: "gw1", tag_name: tag, tag, value });
  }
  return rows;
};
const widgetFor = (preset, readings = 100) => ({
  id: "w1", type: "line_chart", title: "three series",
  config: {
    gateway_id: "gw1", tag_name: "Flow", readings_count: readings, query_time_filter_preset: preset,
    series_extra: [
      { id: "s2", gateway_id: "gw1", tag_name: "Temp", chart_type: "line", color: "#c00" },
      { id: "s3", gateway_id: "gw1", tag_name: "Press", chart_type: "line", color: "#0c0" },
    ],
  },
});
const root = createRoot(document.getElementById("root"));
const render = (widget, live) => root.render(React.createElement(DashboardWidgetCard, {
  widget, dataLogView: live, tagRows: [], tagRowsByGateway: { gw1: [] }, gatewayIntervalsById: { gw1: 200 },
  fetchWidgetRows, formatTagForDisplay: (t) => t,
}));
const perTag = (from = 0) => {
  const m = { Flow: 0, Temp: 0, Press: 0 };
  for (const c of calls.slice(from)) m[c.tag] = (m[c.tag] || 0) + 1;
  return m;
};
const spanOf = (c) => Math.round((Date.parse(c.toUtc) - Date.parse(c.fromUtc)) / 1000);

// 1 + 2: fifteen minutes, three series, 3 s of 200 ms ticks
render(widgetFor("15m"), []);
await sleep(3000);
let n = perTag();
check("15 min: each series fetched its window at most twice in 3 s", Object.values(n).every((v) => v >= 1 && v <= 2),
  JSON.stringify(n) + " (old code: ~10 primary, ~15 per extra)");
const spans = calls.map(spanOf);
check("  every fetch carries the 15-minute window", spans.length > 0 && spans.every((s) => s >= 880 && s <= 920), JSON.stringify(spans));
const buckets = new Set(calls.map((c) => c.bucketSeconds));
check("  and the same bucket for all three series", buckets.size === 1 && !buckets.has(0), JSON.stringify([...buckets]));

// 3: live rows at the gateway interval, no new window fetch
const before = calls.length;
let live = [];
for (let i = 0; i < 10; i += 1) {
  const now = Date.now();
  live = live.concat(["Flow", "Temp", "Press"].map((tag) => ({ ts: new Date(now).toISOString(), gateway_id: "gw1", tag, value: i })));
  render(widgetFor("15m"), live);
  await sleep(200);
}
await sleep(300);
check("live rows every 200 ms for 2 s caused no window fetch", calls.length === before,
  (calls.length - before) + " more fetches");

// 4: a config change fetches at once
const mark = calls.length;
render(widgetFor("15m", 50), live);
await sleep(900);
n = perTag(mark);
check("changing Reading points fetched every series again, once", Object.values(n).every((v) => v === 1), JSON.stringify(n));
const b2 = new Set(calls.slice(mark).map((c) => c.bucketSeconds));
check("  with a coarser bucket for fewer points", b2.size === 1 && [...b2][0] > [...buckets][0], JSON.stringify([...b2]));

// 5: six hours
const m6 = calls.length;
render(widgetFor("6h"), live);
await sleep(1500);
n = perTag(m6);
const s6 = calls.slice(m6).map(spanOf);
check("6 h: each series fetched once, over six hours", Object.values(n).every((v) => v === 1) && s6.every((s) => s >= 21500 && s <= 21700),
  JSON.stringify({ n, s6 }));

// 6: trigger mode, the run started 10 minutes ago and is still on
const mt = calls.length;
const trig = widgetFor("none");
Object.assign(trig.config, { window_mode: "trigger", window_tag_name: "Run", window_gateway_id: "gw1",
  window_operator: "==", window_value: "1", window_stop_tag: "__start_condition_ends__" });
render(trig, []);
await sleep(1500);
const trigCalls = calls.slice(mt);
check("trigger mode: the trigger tag's history was read once", trigCalls.filter((c) => c.tag === "Run").length === 1,
  JSON.stringify(trigCalls.map((c) => c.tag)));
const fromStart = trigCalls.filter((c) => c.tag !== "Run" && Math.abs(Date.parse(c.fromUtc) - RUN_START) <= 1500);
check("  and every series was then loaded from the run's start", ["Flow", "Temp", "Press"].every((t) => fromStart.some((c) => c.tag === t)),
  JSON.stringify(trigCalls.map((c) => [c.tag, c.fromUtc])));

root.unmount();
try { fs.unlinkSync(out); } catch (_) { /* best effort */ }
console.log(fails ? "\nRESULT: FAIL (" + fails + ")" : "\nRESULT: PASS");
process.exit(fails ? 1 : 0);
