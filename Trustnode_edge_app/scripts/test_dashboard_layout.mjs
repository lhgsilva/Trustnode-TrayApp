// Dashboard layout: does a drop leave the rest of the board alone?
//
// 2026-10-30, reported: "when we are automatically rearranging the widgets,
// now it goes totally wrong messing with what was already in placed and
// really hard to make it organized again."
//
// The layout helpers are pure functions, so this needs no browser: build a
// board, move one widget, and count how many OTHERS moved. A drop may push a
// neighbour out of the way; it may not rewrite the board.
//
//     node scripts/test_dashboard_layout.mjs

import {
  compactWidgets,
  normalizeWidgets,
  packWidgetsReadingOrder,
  findFirstFreeSpot,
  previewDropRect,
  reflowWidgetsForResize,
  reflowWidgetsForMoveToPoint,
} from "../frontend/src/components/Dashboard/layoutUtils.js";

let pass = 0;
const fail = [];

function check(name, ok, detail = "") {
  if (ok) { pass += 1; } else { fail.push(name); }
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  - " + detail : ""}`);
}

function board() {
  // Four widgets in two tidy rows, the shape an operator actually arranges.
  return normalizeWidgets([
    { id: "a", type: "value", x: 0, y: 0, w: 3, h: 2, config: {} },
    { id: "b", type: "value", x: 3, y: 0, w: 3, h: 2, config: {} },
    { id: "c", type: "value", x: 0, y: 2, w: 3, h: 2, config: {} },
    { id: "d", type: "value", x: 3, y: 2, w: 3, h: 2, config: {} },
  ]);
}

const at = (list, id) => list.find((w) => String(w.id) === id);
const moved = (before, after, id) => {
  const x = at(before, id), y = at(after, id);
  return x.x !== y.x || x.y !== y.y;
};

console.log("dashboard layout");

// 1. A drop into EMPTY space must disturb nobody.
{
  const before = board();
  const after = reflowWidgetsForMoveToPoint(before, "a", 6, 6);
  const others = ["b", "c", "d"].filter((id) => moved(before, after, id));
  check("a drop into free space moves nothing else", others.length === 0,
        others.length ? `these moved too: ${others.join(", ")}` : "");
  check("  and the dragged widget lands where it was dropped",
        at(after, "a").x === 6 && at(after, "a").y === 6,
        `landed at ${at(after, "a").x},${at(after, "a").y}`);
}

// 2. A drop ONTO a neighbour may move that neighbour - and no one else.
{
  const before = board();
  const after = reflowWidgetsForMoveToPoint(before, "a", 3, 2);   // onto d
  const others = ["b", "c"].filter((id) => moved(before, after, id));
  check("a drop onto a neighbour leaves the far side alone", others.length === 0,
        others.length ? `these moved too: ${others.join(", ")}` : "");
}

// 3. Nothing may ever overlap, whatever the drop.
{
  const after = reflowWidgetsForMoveToPoint(board(), "d", 0, 0);
  let clash = "";
  for (const p of after) {
    for (const q of after) {
      if (p.id === q.id) continue;
      const hit = !(p.x + p.w - 1 < q.x || p.x > q.x + q.w - 1
                 || p.y + p.h - 1 < q.y || p.y > q.y + q.h - 1);
      if (hit) clash = `${p.id} overlaps ${q.id}`;
    }
  }
  check("no two widgets ever overlap after a drop", !clash, clash);
}

// 4. A displaced neighbour steps DOWN, never jumps to the top of the board.
{
  const before = normalizeWidgets([
    { id: "top", type: "value", x: 0, y: 0, w: 6, h: 2, config: {} },
    { id: "gap", type: "value", x: 0, y: 8, w: 3, h: 2, config: {} },
    { id: "low", type: "value", x: 3, y: 8, w: 3, h: 2, config: {} },
  ]);
  const after = reflowWidgetsForMoveToPoint(before, "top", 3, 8);  // onto low
  check("a displaced widget is not thrown to the top",
        at(after, "low").y >= at(before, "low").y,
        `low went from y=${at(before, "low").y} to y=${at(after, "low").y}`);
}

// 5. Tidy packs in reading order with no holes, and keeps every widget.
{
  const messy = normalizeWidgets([
    { id: "a", type: "value", x: 6, y: 9, w: 3, h: 2, config: {} },
    { id: "b", type: "value", x: 0, y: 4, w: 3, h: 2, config: {} },
    { id: "c", type: "value", x: 3, y: 4, w: 3, h: 2, config: {} },
  ]);
  const tidy = packWidgetsReadingOrder(messy);
  check("tidy keeps every widget", tidy.length === messy.length,
        `${messy.length} in, ${tidy.length} out`);
  check("tidy starts at the top-left corner",
        tidy.some((w) => w.x === 0 && w.y === 0));
  check("tidy is deterministic",
        JSON.stringify(packWidgetsReadingOrder(messy)) === JSON.stringify(tidy));
  const sizesKept = tidy.every((t) => {
    const m = at(messy, String(t.id));
    return m.w === t.w && m.h === t.h;
  });
  check("tidy changes positions only, never sizes", sizesKept);
}

// 6. The preview must predict the real landing spot, or it lies to the operator.
{
  const before = board();
  for (const [x, y] of [[6, 6], [3, 2], [0, 0], [9, 1]]) {
    const pre = previewDropRect(before, "a", x, y);
    const real = at(reflowWidgetsForMoveToPoint(before, "a", x, y), "a");
    check(`preview matches the drop at ${x},${y}`,
          pre && pre.x === real.x && pre.y === real.y,
          pre ? `preview ${pre.x},${pre.y} vs real ${real.x},${real.y}` : "no preview");
  }
}

// ---------------------------------------------------------------------------
// 2026-10-30, reported verbatim: "I have 6 widgets on the first row completing
// the full width and two in the second row completing the full width, now I am
// adding and arranging widgets in the third row and it is moving components
// from the first two rows."
//
// A row that is already full has no reason to change. This builds that exact
// board (the grid is 40 columns wide) and checks the settled rows survive.
function settledBoard() {
  const list = [];
  const widths = [6, 7, 7, 7, 7, 6];           // 40 columns exactly
  let x = 0;
  widths.forEach((w, i) => { list.push({ id: "r1_" + i, type: "value", x, y: 0, w, h: 4, config: {} }); x += w; });
  list.push({ id: "r2_0", type: "value", x: 0, y: 4, w: 20, h: 4, config: {} });
  list.push({ id: "r2_1", type: "value", x: 20, y: 4, w: 20, h: 4, config: {} });
  return normalizeWidgets(list);
}

const settledIds = (w) => String(w.id).startsWith("r1_") || String(w.id).startsWith("r2_");
const snapshot = (list) => JSON.stringify(list.filter(settledIds)
  .slice().sort((a, b) => String(a.id).localeCompare(String(b.id)))
  .map((w) => [String(w.id), w.x, w.y, w.w, w.h]));

console.log("");
console.log("settled rows must survive work in a later row");

{
  const before = settledBoard();
  const spot = findFirstFreeSpot({ w: 8, h: 4 }, before);
  const after = compactWidgets([...before, { id: "new1", type: "value", ...spot, w: 8, h: 4, config: {} }]);
  check("adding a widget leaves the two full rows alone",
        snapshot(after) === snapshot(before),
        snapshot(after) === snapshot(before) ? "" : "settled rows moved");
  check("  and the new widget lands below them",
        (at(after, "new1") || {}).y >= 8,
        "new widget at y=" + (at(after, "new1") || {}).y);
}

{
  const base = settledBoard();
  const withThird = normalizeWidgets([...base,
    { id: "t1", type: "value", x: 0, y: 8, w: 10, h: 4, config: {} },
    { id: "t2", type: "value", x: 10, y: 8, w: 10, h: 4, config: {} }]);
  const after = reflowWidgetsForMoveToPoint(withThird, "t2", 22, 8);
  check("moving a widget within the third row leaves the first two alone",
        snapshot(after) === snapshot(withThird),
        snapshot(after) === snapshot(withThird) ? "" : "settled rows moved");
}

{
  const base = settledBoard();
  const withThird = normalizeWidgets([...base,
    { id: "t1", type: "value", x: 0, y: 8, w: 10, h: 4, config: {} }]);
  const after = reflowWidgetsForMoveToPoint(withThird, "t1", 0, 0);
  const movedIds = withThird.filter(settledIds)
    .filter((w) => { const n = at(after, String(w.id)); return !n || n.x !== w.x || n.y !== w.y; })
    .map((w) => String(w.id));
  check("a deliberate drop onto row 1 pushes, it does not scatter",
        movedIds.length <= 6,
        movedIds.length + " settled widgets moved: " + movedIds.join(", "));
}

{
  // Resizing a third-row widget must not disturb the finished rows either -
  // it is the other way an operator "changes something".
  const base = settledBoard();
  const withThird = normalizeWidgets([...base,
    { id: "t1", type: "value", x: 0, y: 8, w: 10, h: 4, config: {} },
    { id: "t2", type: "value", x: 10, y: 8, w: 10, h: 4, config: {} }]);
  const after = reflowWidgetsForResize(withThird, "t1", 18, 6);
  check("resizing in the third row leaves the first two alone",
        snapshot(after) === snapshot(withThird),
        snapshot(after) === snapshot(withThird) ? "" : "settled rows moved");
  check("  and the resize actually applied",
        (at(after, "t1") || {}).w === 18);
}

{
  // 2026-10-30: a chart's settings must survive the normaliser, which runs
  // on every load, drag and reflow. interpolation was being dropped, so a
  // restart redrew every series with the widget default - reported as
  // "configurations of charts including the interpolation are being lost".
  const widget = {
    id: "c1", type: "line_chart", x: 0, y: 0, w: 10, h: 6,
    config: {
      interpolation: "monotone",
      series_extra: [
        { id: "s1", tag_name: "T1", interpolation: "step", line_width: 3 },
        { id: "s2", tag_name: "T2", interpolation: "linear", y_min: "0", y_max: "50" },
        { id: "s3", tag_name: "T3", interpolation: "" },
      ],
    },
  };
  // Twice: once is a load, twice is a load followed by a drag.
  const once = normalizeWidgets([widget]);
  const twice = normalizeWidgets(once);
  const series = (list) => (at(list, "c1") || {}).config.series_extra || [];
  check("the widget keeps its own interpolation",
        (at(twice, "c1") || {}).config.interpolation === "monotone",
        (at(twice, "c1") || {}).config.interpolation);
  check("each series keeps its own line shape",
        series(twice).map((s) => s.interpolation).join(",") === "step,linear,",
        "got: " + JSON.stringify(series(twice).map((s) => s.interpolation)));
  check("  and the other series settings survive too",
        series(twice)[0].line_width === 3 && series(twice)[1].y_max === "50");
}

console.log("");
console.log("  " + pass + " passed, " + fail.length + " failed");
for (const f of fail) console.log("    FAILED: " + f);
process.exit(fail.length ? 1 : 0);
