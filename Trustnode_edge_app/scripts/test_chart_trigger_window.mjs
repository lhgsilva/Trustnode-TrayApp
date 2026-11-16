/**
 * A chart can restart its X axis on a tag, and the control is where the
 * operator can reach it.
 *
 * 2026-10-30, asked: "where should I go in the dashboard widget to configure
 * that the chart timestamp should be reset based on a tag value - the idea is
 * a chart that restarts the X timestamp when a test starts and when it ends,
 * so the chart only shows data in this window".
 *
 * The answer was "nowhere", for a reason worth keeping: the feature HAD been
 * built, in App.jsx's widget editor. That is a different, legacy editor. The
 * dialog an operator actually opens is DashboardDesigner.jsx, and the chart
 * an operator actually sees is rendered by DashboardWidgets.jsx. The config
 * key was present in the shipped bundle - verified - and still unreachable,
 * because "present in the bundle" was never the question.
 *
 * So this file checks both halves, and the first half is the lesson:
 *
 *   1. the control is in the editor that is mounted, next to the other
 *      chart-range controls, for every chart type that has a time axis;
 *   2. the reducer that moves the window does what a test rig needs - a
 *      rising edge restarts it, a stop closes it, and a second run does
 *      not inherit the first one's points.
 *
 *     node scripts/test_chart_trigger_window.mjs
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "frontend", "src");
const read = (...p) => fs.readFileSync(path.join(SRC, ...p), "utf8");

let fails = 0;
const check = (name, ok, detail = "") => {
  console.log("  %s %s%s", (name + " ".repeat(58)).slice(0, 58),
    ok ? "PASS" : "FAIL", (!ok && detail) ? "  - " + detail : "");
  if (!ok) fails += 1;
};

console.log("chart trigger window");

// ---------------------------------------------------------------- part 1
// Reachability. Not "is the string in the file" - it was, in the wrong file.
const DESIGNER = read("components", "Dashboard", "DashboardDesigner.jsx");
const WIDGETS = read("components", "Dashboard", "DashboardWidgets.jsx");
const APP = read("App.jsx");

check("the dashboard mounts DashboardDesigner",
  /<DashboardDesigner\b/.test(APP),
  "if this changes, the control may need to move with it");

check("the control lives in the editor that is mounted",
  DESIGNER.includes("window_mode"),
  "the operator opens DashboardDesigner, not App.jsx's editor");

// It has to sit with the other range controls, or it is reachable only in
// theory - that is the same failure in a smaller form.
// Anchor on the control's own label, not on the first mention of the config
// key - a derived flag near the top of the component is a mention too, and
// anchoring on it made this check measure the wrong distance.
const iWindow = DESIGNER.indexOf("Time window");
const iPoints = DESIGNER.indexOf("Reading points");
check("it sits beside the existing Reading points control",
  iWindow > 0 && iPoints > 0 && Math.abs(iPoints - iWindow) < 6000,
  `window_mode@${iWindow} readingPoints@${iPoints}`);

// Every chart type that gets Reading points must also get a window, or a
// stacked trend silently ignores the setting the operator just saved.
const CHART_TYPES = ["line_chart", "line_area_chart", "bar_chart", "stacked_trend"];
const decl = /const CHART_TYPES_WITH_WINDOW = \[([^\]]*)\]/.exec(DESIGNER);
check("every time-axis chart type offers it", !!decl
  && CHART_TYPES.every((t) => decl[1].includes(`"${t}"`)),
  decl ? decl[1].trim() : "CHART_TYPES_WITH_WINDOW not found");

// Every field the renderer reads must be written by the editor, and every
// field the editor writes must be read. A half-wired pair is exactly how
// this feature was broken the first time.
const FIELDS = ["window_mode", "window_gateway_id", "window_tag_name",
  "window_operator", "window_value", "window_stop_tag",
  "window_stop_operator", "window_stop_value"];
for (const f of FIELDS) {
  check(`${f}: written by the editor and read by the chart`,
    DESIGNER.includes(f) && WIDGETS.includes(f));
}

// The config must survive the save path. Both builders spread the config
// rather than copying an allowlist - an allowlist is what silently dropped
// series_extra keys before (see dashboard_widget_conventions).
const LAYOUT = read("components", "Dashboard", "layoutUtils.js");
check("normalisation spreads config instead of allowlisting it",
  /\.\.\.\(raw\?\.config/.test(LAYOUT),
  "an allowlist here would strip window_* on the next save");

// ---------------------------------------------------------------- part 2
// The reducer itself, driven with real row shapes.
const esbuild = await import(
  pathToFileUrl(path.join(SRC, "..", "node_modules", "esbuild", "lib", "main.js")));

function pathToFileUrl(p) { return "file:///" + p.replace(/\\/g, "/"); }

const outFile = path.join(os.tmpdir(), `tn_trigwin_${process.pid}.mjs`);
await esbuild.build({
  stdin: {
    contents: `export { triggerMatches, applyTriggerRows, boundRowsToWindow } from ${
      JSON.stringify(path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx").replace(/\\/g, "/"))};`,
    resolveDir: SRC,
    loader: "js",
  },
  bundle: true,
  format: "esm",
  platform: "node",
  outfile: outFile,
  loader: { ".js": "jsx", ".jsx": "jsx", ".css": "empty", ".png": "empty", ".svg": "empty" },
  // Vite rewrites import.meta.env at build time; node has no such thing, so
  // the bundle dies on the first module that reads a VITE_ variable. The app
  // treats every one of them as optional, which is what an empty object says.
  define: { "import.meta.env": "{}" },
  logLevel: "silent",
});
const { triggerMatches, applyTriggerRows, boundRowsToWindow } = await import(pathToFileUrl(outFile));

const row = (tag, value, ts, extra = {}) => ({
  tag, value, ts_utc: ts, gateway_id: "gw1", ...extra,
});

// A numeric start tag, the ordinary case.
const CFG = { gatewayId: "gw1", tag: "Test_Running", op: "==", value: "1",
  stopTag: "", stopOp: "==", stopValue: "1" };

function run(cfg, rows) {
  const ref = { current: { start: 0, end: 0, on: false, seen: -Infinity } };
  let win = { start: 0, end: 0 };
  applyTriggerRows(rows, cfg, ref, (w) => { win = w; });
  return { win, ref };
}

const T = (n) => `2026-10-30 10:0${n}:00`;
const ms = (n) => Date.parse(`2026-10-30T10:0${n}:00Z`);

// Nothing has happened: no window, so the chart keeps its rolling view
// rather than going blank. This is the guard that stops the feature from
// being worse than not having it.
check("no readings yet leaves the window closed",
  run(CFG, []).win.start === 0);

check("the tag sitting at 0 does not open a window",
  run(CFG, [row("Test_Running", 0, T(0)), row("Test_Running", 0, T(1))]).win.start === 0);

// The rising edge is what "restart" means.
let r = run(CFG, [row("Test_Running", 0, T(0)), row("Test_Running", 1, T(1)),
  row("Test_Running", 1, T(2))]);
check("a rising edge opens the window at that reading",
  r.win.start === ms(1) && r.win.end === 0, JSON.stringify(r.win));

// "Runs until the next start" must mean exactly that. 2026-10-30: the
// dropdown said so while the code closed the window on the falling edge, so
// on a value that crosses its threshold every cycle the chart froze halfway
// through each run. The label is what the operator reads; it wins.
r = run(CFG, [row("Test_Running", 0, T(0)), row("Test_Running", 1, T(1)),
  row("Test_Running", 0, T(3)), row("Test_Running", 0, T(4))]);
check("'runs until the next start' does NOT close on the falling edge",
  r.win.start === ms(1) && r.win.end === 0, JSON.stringify(r.win));

// Ending on the falling edge is still available - now by asking for it.
const CFG_FALL = { ...CFG, stopTag: "__start_condition_ends__" };
r = run(CFG_FALL, [row("Test_Running", 0, T(0)), row("Test_Running", 1, T(1)),
  row("Test_Running", 0, T(3))]);
check("'when the start condition ends' closes on the falling edge",
  r.win.start === ms(1) && r.win.end === ms(3), JSON.stringify(r.win));

// And that option must not need a second tag to exist.
check("  it needs no second tag to work",
  run(CFG_FALL, [row("Test_Running", 1, T(1))]).win.start === ms(1));

// THE point of the feature: run two tests, see only the second.
r = run(CFG, [row("Test_Running", 1, T(1)), row("Test_Running", 0, T(2)),
  row("Test_Running", 1, T(4)), row("Test_Running", 1, T(5))]);
check("a second run does not inherit the first run's window",
  r.win.start === ms(4) && r.win.end === 0, JSON.stringify(r.win));

// A separate stop tag.
const CFG2 = { ...CFG, stopTag: "Test_Done", stopOp: "==", stopValue: "1" };
r = run(CFG2, [row("Test_Running", 1, T(1)), row("Test_Done", 0, T(2)),
  row("Test_Done", 1, T(3))]);
check("a separate end tag closes the window",
  r.win.start === ms(1) && r.win.end === ms(3), JSON.stringify(r.win));

// Rows arrive newest-first on the live stream and oldest-first from the
// historian seed. The reducer must not care.
const shuffled = [row("Test_Running", 1, T(4)), row("Test_Running", 0, T(2)),
  row("Test_Running", 1, T(1))];
check("row order does not change the answer",
  run(CFG, shuffled).win.start === ms(4), JSON.stringify(run(CFG, shuffled).win));

// Another gateway's tag of the same name must not drive this chart - a
// cross-gateway read here is the same class of mistake as a cross-scope one.
r = run(CFG, [row("Test_Running", 1, T(1), { gateway_id: "gw9" })]);
check("a same-named tag on another gateway is ignored", r.win.start === 0);

// A text state tag, because not every rig exposes a boolean.
const CFG3 = { ...CFG, tag: "State", op: "==", value: "RUNNING" };
r = run(CFG3, [
  { tag: "State", value: null, value_text: "IDLE", ts_utc: T(0), gateway_id: "gw1" },
  { tag: "State", value: null, value_text: "Running", ts_utc: T(2), gateway_id: "gw1" },
]);
check("a text state tag works, and case does not matter",
  r.win.start === ms(2), JSON.stringify(r.win));

// Thresholds, for a rig that starts on pressure rather than a flag.
check("'greater than' compares as a number", triggerMatches({ value: 5 }, ">", "3"));
check("'greater than' is false below the threshold", !triggerMatches({ value: 2 }, ">", "3"));
check("'at least' includes the boundary", triggerMatches({ value: 3 }, ">=", "3"));
check("'not equal' on text", triggerMatches({ value_text: "FAIL" }, "!=", "PASS"));

// Incremental calls: the live effect runs on every stream update, and must
// not re-open a window from rows it has already consumed.
{
  const ref = { current: { start: 0, end: 0, on: false, seen: -Infinity } };
  let calls = 0;
  let win = { start: 0, end: 0 };
  const set = (w) => { calls += 1; win = w; };
  const batch = [row("Test_Running", 0, T(0)), row("Test_Running", 1, T(1))];
  applyTriggerRows(batch, CFG, ref, set);
  applyTriggerRows(batch, CFG, ref, set);   // same rows again
  applyTriggerRows(batch, CFG, ref, set);
  check("replayed rows do not re-open or re-render the window",
    calls === 1 && win.start === ms(1), `${calls} state update(s)`);
}

// The renderer must actually BOUND the rows, not merely know the window.
// Run the real function, because a grep passes just as happily when the
// branch is dead - it did, on the first draft of this file.
{
  const pts = [ms(0), ms(1), ms(2), ms(3), ms(4), ms(5)];
  const opts = { capacity: 60, windowMs: 60000, nowMs: ms(5) };

  const open = boundRowsToWindow(pts, { start: ms(2), end: 0 }, opts);
  check("a running test shows only points from its start",
    open.length === 4 && open[0] === ms(2) && open[3] === ms(5),
    JSON.stringify(open.map((t) => (t - ms(0)) / 60000)));

  const closed = boundRowsToWindow(pts, { start: ms(1), end: ms(3) }, opts);
  check("a finished test shows only its own window",
    closed.length === 3 && closed[0] === ms(1) && closed[2] === ms(3),
    JSON.stringify(closed.map((t) => (t - ms(0)) / 60000)));

  // Old data from before the run must not be dragged in - that is the whole
  // complaint the feature exists to answer.
  check("points before the trigger are dropped",
    !boundRowsToWindow(pts, { start: ms(3), end: 0 }, opts).includes(ms(0)));

  // And with no window the previous behaviour is byte-for-byte intact: a
  // chart nobody configured must not change at all.
  const rolling = boundRowsToWindow(pts, { start: 0, end: 0 },
    { capacity: 2, windowMs: 60000 * 60, nowMs: ms(5) });
  check("with no trigger the rolling last-N behaviour is unchanged",
    rolling.length === 2 && rolling[1] === ms(5), JSON.stringify(rolling.length));
}
check("the renderer calls the bounding function",
  /sorted = boundRowsToWindow\(/.test(WIDGETS),
  "the dataset is built without it");
check("the window is a dependency of the rendered dataset",
  /showGaps, trigWin(,|\])/.test(WIDGETS),
  "without this the chart would not redraw when a run starts");
// 2026-10-30, reported with a screenshot: the window was configured and the
// chart still showed two full cycles. "Time range: Last 15 minutes" - the
// DEFAULT - routes the widget down the historical pipeline, where the window
// is not applied, so the feature only worked for someone who first set Time
// range to None. Nothing on screen said so. A trigger window IS a time
// range, so it must win over the preset rather than lose to it.
check("a trigger window keeps the widget on the path that applies it",
  /hasTriggerWindow \|\| \(!hasTimePreset && !hasTimeRange\)/.test(WIDGETS),
  "with a time preset set, the window is computed and never used");
check("  the trigger window is detected from the saved config",
  /const hasTriggerWindow =/.test(WIDGETS)
  && /window_mode.*===\s*"trigger"/.test(WIDGETS));
check("  and the seed covers a whole run, not readings_count",
  /if \(trigCfg\) return 5000;/.test(WIDGETS),
  "a run already under way would start part-way through");
// Two controls that both claim the X axis is the 2026-10-17 lesson. The
// Time range preset is disabled while a trigger window is in use.
check("the editor offers both ways for a run to end",
  DESIGNER.includes("Runs until the next start")
  && DESIGNER.includes("__start_condition_ends__"),
  "the sentinel the renderer looks for must be what the editor writes");
check("  and the renderer agrees on the sentinel's spelling",
  WIDGETS.includes('"__start_condition_ends__"'),
  "a mismatch here silently disables the option");

check("Time range is disabled while a trigger window is in use",
  /disabled=\{triggerWindowOn\}/.test(DESIGNER)
  && /const triggerWindowOn =/.test(DESIGNER),
  "a control that silently does nothing is what gets reported as broken");

check("the ring buffer is not capped to readings_count during a run",
  /bufferCap = trigCfg \? /.test(WIDGETS),
  "a long run would lose its own early points");

try { fs.unlinkSync(outFile); } catch { /* temp file */ }

console.log("\n  %d check(s) failed", fails);
process.exit(fails ? 1 : 0);
