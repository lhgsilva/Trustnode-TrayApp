/* One chart style for the whole app.

   2026-08-29, reported: "the power view should have the same style as the
   dashboard with all chart configuration applied to it, text fonts and
   library."

   Both already use recharts - the difference was that each page hard-coded its
   own axis font, grid stroke and tooltip, and the dashboard exposed editing
   options (font scales, line width, dots, tick angle) that Power did not.

   This module holds the shared values and the shared option shape. It is
   deliberately data, not components: the two pages compose their own charts,
   and forcing them through one wrapper would mean rewriting the dashboard's
   widget chrome to fix the power page's fonts. */

/* The defaults every chart starts from. Sizes are the dashboard's existing
   ones, so adopting this module changes nothing on the dashboard - it only
   brings Power into line. */
export const CHART_BASE = {
  axis_font_px: 10,
  label_font_px: 11,
  legend_font_px: 12,
  grid_stroke: "var(--line, rgba(255,255,255,0.07))",
  grid_dash: "3 3",
  line_width: 2,
  area_fill_opacity: 0.16,
  x_tick_angle: 0,
};

/* A scale multiplier from a config object, clamped so a typo cannot make a
   chart unreadable. Same rule the dashboard already applies. */
export function fontScale(cfg, key) {
  const v = Number(cfg?.[key]);
  return Number.isFinite(v) && v > 0 ? Math.max(0.3, Math.min(4, v)) : 1;
}

export function axisFontPx(cfg) {
  return Math.round(CHART_BASE.axis_font_px * fontScale(cfg, "font_axis_scale"));
}
export function labelFontPx(cfg) {
  return Math.round(CHART_BASE.label_font_px * fontScale(cfg, "font_labels_scale"));
}
export function legendFontPx(cfg) {
  return Math.round(CHART_BASE.legend_font_px * fontScale(cfg, "font_legend_scale"));
}

/* Props for an axis tick, so every axis in the app is set the same way.
   `colour` is passed in because Power themes its axes from a resolved token
   while the dashboard inherits. */
export function axisTick(cfg, colour) {
  const size = axisFontPx(cfg);
  return colour ? { fontSize: size, fill: colour } : { fontSize: size };
}

/* The grid, identical everywhere. */
export function gridProps() {
  return { stroke: CHART_BASE.grid_stroke, strokeDasharray: CHART_BASE.grid_dash };
}

export function legendStyle(cfg) {
  return { fontSize: legendFontPx(cfg) };
}

/* Curve shapes, in the order an operator thinks about them. `stepAfter` is the
   honest default for sampled process data: a 1 Hz meter reading is a value
   held until the next sample, not a straight ramp between two readings. */
export const CURVE_TYPES = [
  ["stepAfter", "Step (hold to next sample)"],
  ["monotone", "Smooth (monotone)"],
  ["linear", "Straight lines"],
  ["basis", "Smoothed (basis)"],
  ["stepBefore", "Step (before)"],
];

/* The editable chart options both pages offer. Keeping the list here is what
   stops the two settings dialogs drifting apart again. */
export const CHART_EDIT_FIELDS = [
  { key: "interpolation", label: "Curve", type: "select", options: CURVE_TYPES,
    hint: "How the line is drawn between samples. Step is truthful for sampled data." },
  { key: "chart_line_width", label: "Line width", type: "number", min: 1, max: 6, step: 0.5 },
  { key: "chart_line_dot", label: "Show points", type: "toggle" },
  { key: "show_legend", label: "Show legend", type: "toggle" },
  { key: "chart_x_tick_angle", label: "X label angle", type: "number", min: -90, max: 90, step: 15 },
  { key: "font_axis_scale", label: "Axis text size", type: "number", min: 0.3, max: 4, step: 0.1 },
  { key: "font_labels_scale", label: "Label text size", type: "number", min: 0.3, max: 4, step: 0.1 },
  { key: "font_legend_scale", label: "Legend text size", type: "number", min: 0.3, max: 4, step: 0.1 },
];
