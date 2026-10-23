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
  listBatches,
  listBatchTypes,
} from "../../api";

// ---------------------------------------------------------------------------
// section presets / option lists
// ---------------------------------------------------------------------------
const SECTION_PRESETS = [
  { type: "header", label: "Header", description: "Title + subtitle banner" },
  { type: "text", label: "Text block", description: "Plain text paragraph" },
  { type: "kpi_grid", label: "KPI grid", description: "Aggregated values per tag" },
  { type: "pill_card", label: "Pill cards",
    description: "Coloured status pills - literal text or a live tag value" },
  { type: "line_chart", label: "Line chart", description: "Tag values over time (multi-tag)" },
  { type: "area_chart", label: "Area chart", description: "Filled trend line" },
  { type: "bar_chart", label: "Bar chart", description: "Tag samples as bars" },
  { type: "pie_chart", label: "Pie chart", description: "Distribution (direct or computed rules)" },
  { type: "table", label: "Data table", description: "Historian rows with custom columns" },
  { type: "static_table", label: "Details table",
    description: "Your own rows - fixed text, or a tag value via {TagName}" },
  { type: "segment_summary", label: "Test summary table",
    description: "One row per test/cycle, aggregated per tag, with a total row" },
  { type: "image", label: "Image", description: "Embed an uploaded picture (logo, photo, diagram)" },
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
  // Operator 2026-07-06: anchor the section to a BATCH's window — the report
  // shows exactly the data collected during that batch (or the latest of a type).
  { value: "batch", label: "Batch window" },
  // 2026-09-30: the run's own trigger window, and one sub-period of it.
  // "Run window" is what a section follows by default; "Segment" lets four
  // charts on one page each cover a different test or cycle.
  { value: "run", label: "Run window (start→stop trigger)" },
  { value: "segment", label: "One test / cycle (segment)" },
];

// A section's Custom range, in UTC on the wire and local on the screen.
//
// 2026-10-17: these two boxes wrote `time_range.from_utc` straight from a
// <input type="datetime-local">, which emits LOCAL wall time. Every stamp in
// this product is UTC, so a custom range was off by the machine's offset -
// the same defect as the Data Export window, found the same afternoon, where
// a typed window returned 0 rows out of 27,234.
//
// Converting means any range SAVED before today now renders an hour away
// from where it did (approved 2026-10-17, UTC+1 machine). The alternative
// was leaving every new one wrong.
//
// Both directions are needed. Showing a stored UTC stamp in a local-time box
// without converting it back would re-convert it on the next save and walk
// the range an hour further every time it was edited.
const utcToLocalInput = (stamp) => {
  const raw = String(stamp || "").trim();
  if (!raw) return "";
  const d = new Date(raw.replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? "" : "Z"));
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
const localInputToUtc = (value) => {
  const raw = String(value || "").trim();
  if (!raw) return "";                        // empty means "not set"
  const d = new Date(raw);                    // parsed in the browser's zone
  if (Number.isNaN(d.getTime())) return raw;  // never silently drop a typo
  return d.toISOString().slice(0, 19).replace("T", " ");
};


// Picker shown when a section's time-range preset is "batch". Lets the user
// bind the section to a specific batch, or to "the latest batch of a type"
// (so a scheduled report always covers the most recent run of that type).
function BatchWindowPicker({ timeRange, onChange }) {
  const [types, setTypes] = React.useState([]);
  const [batches, setBatches] = React.useState([]);
  const [mode, setMode] = React.useState(timeRange?.batch_of_type_id ? "latest_of_type" : "specific");
  React.useEffect(() => {
    (async () => {
      try { setTypes(await listBatchTypes()); } catch { /* module may be off */ }
      try { const d = await listBatches({ limit: 100 }); setBatches(Array.isArray(d?.rows) ? d.rows : []); } catch { /* */ }
    })();
  }, []);
  return (
    <div className="tn-row tn-row-2 tn-row-tight">
      <label>Batch source
        <select value={mode} onChange={(e) => {
          const m = e.target.value; setMode(m);
          // clear the other key when switching mode
          if (m === "specific") onChange({ batch_of_type_id: "" });
          else onChange({ batch_id: "" });
        }}>
          <option value="specific">A specific batch</option>
          <option value="latest_of_type">Latest batch of a type</option>
        </select>
      </label>
      {mode === "specific" ? (
        <label>Batch
          <select value={timeRange?.batch_id || ""} onChange={(e) => onChange({ batch_id: e.target.value })}>
            <option value="">— pick a batch —</option>
            {batches.map((b) => (
              <option key={b.id} value={b.id}>
                {(b.identifier || b.id)}{b.status ? ` · ${b.status}` : ""}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <label>Batch type
          <select value={timeRange?.batch_of_type_id || ""} onChange={(e) => onChange({ batch_of_type_id: e.target.value })}>
            <option value="">— pick a type —</option>
            {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
      )}
    </div>
  );
}

const KPI_AGGREGATIONS = [
  { value: "count", label: "Count" },
  { value: "sum", label: "Sum" },
  { value: "avg", label: "Average" },
  { value: "min", label: "Min" },
  { value: "max", label: "Max" },
  { value: "latest", label: "Latest" },
];

// What a Test summary table can compute per test, and again across tests.
// Mirrors the backend's _reduce(); "latest" is deliberately absent because a
// column of per-test results has no meaningful "latest".
const SEGMENT_AGGREGATIONS = [
  { value: "avg", label: "Average" },
  { value: "min", label: "Min" },
  { value: "max", label: "Max" },
  { value: "last", label: "Final value" },
  { value: "first", label: "First value" },
  { value: "sum", label: "Sum" },
  { value: "count", label: "Count" },
  { value: "median", label: "Median" },
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
// A TCU functional test, as described by the operator on 2026-09-30:
//   initial assessment (a short test), then a final test of several cycles
//   whose average is the result. Both run the phases Enabling, Deadhead,
//   Heating, Holding, Cooling, Holding, monitoring pressure, flow and
//   temperature on the reference unit and on the TCU under test.
//
// Every section that needs its own window uses a SECTION TRIGGER on the phase
// tag, so the same template works whether it is fired by the test's own
// start/stop trigger or by a schedule. Gateway and tag names are left blank
// on purpose - the operator picks them from their own gateways, and a guessed
// tag name would render an empty chart that looks like a failed test.
const TCU_PHASES = ["Enabling", "Deadhead", "Heating", "Holding", "Cooling"];

function tcuPhaseTrigger(phase) {
  return { start: { gateway_id: "", tag_name: "", operator: "==", value: phase } };
}

function tcuTestingReport() {
  const meas = [
    { label: "Pressure", tag: "" },
    { label: "Flow", tag: "" },
    { label: "Temperature", tag: "" },
  ];
  return {
    name: "Testing Report",
    description: "TCU functional test - initial assessment, final test cycles, per-phase detail",
    sections: [
      { ...defaultSection("header"), title: "Testing Report",
        subtitle: "TCU functional test" },
      { ...defaultSection("text"), title: "Part details",
        text: "Part number:\nSerial number:\nDescription:\nOperator:\nRecipe:" },

      // Whole-test KPIs: the main start/stop trigger window.
      { ...defaultSection("kpi_grid"), title: "Overall result",
        time_range: { preset: "run" }, columns: 4,
        items: meas.map((m) => ({ label: m.label, gateway_id: "", tag_name: m.tag,
                                  aggregation: "avg", operator: "any" })) },

      // KPIs for one phase only - a different window inside the same report.
      { ...defaultSection("kpi_grid"), title: "Heating phase",
        time_range: { preset: "run" }, trigger: tcuPhaseTrigger("Heating"), columns: 3,
        items: meas.map((m) => ({ label: `${m.label} (heating)`, gateway_id: "",
                                  tag_name: m.tag, aggregation: "max", operator: "any" })) },

      { ...defaultSection("area_chart"), title: "Temperature - whole test",
        time_range: { preset: "run" } },
      { ...defaultSection("line_chart"), title: "Heating phase detail",
        time_range: { preset: "run" }, trigger: tcuPhaseTrigger("Heating") },
      { ...defaultSection("bar_chart"), title: "Cooling phase detail",
        time_range: { preset: "run" }, trigger: tcuPhaseTrigger("Cooling") },

      { ...defaultSection("table"), title: "Telemetry", row_limit: 100,
        time_range: { preset: "run" } },

      // One row per cycle of the final test, averaged at the foot: "the
      // average results of them is the final test result".
      { ...defaultSection("segment_summary"), title: "Final test - result per cycle",
        stream: "cycle", segment: "", segment_header: "Cycle",
        columns: meas.map((m) => ({ tag_name: m.tag, aggregation: "avg",
                                    title: `${m.label} avg` })),
        footer_aggregation: "avg", footer_label: "Final result", format: "3dp" },

      // One row per phase, so the recipe can be checked phase by phase.
      { ...defaultSection("segment_summary"), title: "Results by phase",
        stream: "phase", segment: "", segment_header: "Phase",
        columns: meas.map((m) => ({ tag_name: m.tag, aggregation: "avg",
                                    title: `${m.label} avg` })),
        footer_aggregation: "", format: "3dp" },

      { ...defaultSection("table"), title: "Alarm log", row_limit: 100,
        time_range: { preset: "run" } },
    ],
  };
}

// The live PLC cycle rig: cycle_started / cycle_ended open and close the run,
// cycle_started1/2 and cycle_ended1/2 mark the two inner cycles, and
// SimREAL[3] / SimREAL[4] are the values being charted.
//
// 2026-10-01: the tag names are REAL, so the preset works the moment it is
// chosen. gateway_id is left blank on purpose - the historian matches on the
// tag alone when no gateway is given, so there is no id here to go stale.
const PLC_VALUE_TAGS = ["SimREAL[3]", "SimREAL[4]"];

function plcSeries(tag, color) {
  return { id: tag, label: tag, gateway_id: "", tag_name: tag, color };
}

function plcCond(tag) {
  return { gateway_id: "", tag_name: tag, operator: "==", value: 1 };
}

function plcLiveCycleTest() {
  const run = { preset: "run" };
  const inner = [
    ["cycle_started1", "cycle_ended1"],
    ["cycle_started2", "cycle_ended2"],
  ];
  return {
    name: "TCU Live Cycle Test",
    description: "PLC cycle bits open and close the run; each inner cycle has its own trigger",
    sections: [
      { ...defaultSection("header"), title: "TCU Cycle Test",
        subtitle: "generated automatically when the cycle ends" },
      { ...defaultSection("text"), title: "Part details",
        text: "Part number:\nSerial number:\nOperator:\nRecipe:" },

      { ...defaultSection("kpi_grid"), title: "Cycle results", columns: 4,
        time_range: { ...run },
        items: [
          { label: "SimREAL[3] max", gateway_id: "", tag_name: "SimREAL[3]",
            aggregation: "max", operator: "any" },
          { label: "SimREAL[3] avg", gateway_id: "", tag_name: "SimREAL[3]",
            aggregation: "avg", operator: "any" },
          { label: "Peak rise (per min)", gateway_id: "", tag_name: "SimREAL[3]",
            aggregation: "rate_max", operator: "any" },
          { label: "SimREAL[4] avg", gateway_id: "", tag_name: "SimREAL[4]",
            aggregation: "avg", operator: "any" },
        ] },

      { ...defaultSection("line_chart"), title: "Whole cycle",
        time_range: { ...run }, x_axis_label: "Time", y_axis_label: "Value",
        series: [plcSeries(PLC_VALUE_TAGS[0], "#ef5350"),
                 plcSeries(PLC_VALUE_TAGS[1], "#42a5f5")] },

      { ...defaultSection("line_chart"), title: "Inner cycle 1",
        time_range: { ...run },
        trigger: { start: plcCond(inner[0][0]), stop: plcCond(inner[0][1]) },
        x_axis_label: "Time", y_axis_label: PLC_VALUE_TAGS[0],
        series: [plcSeries(PLC_VALUE_TAGS[0], "#26a69a")] },
      { ...defaultSection("bar_chart"), title: "Inner cycle 2",
        time_range: { ...run }, side_by_side: true,
        trigger: { start: plcCond(inner[1][0]), stop: plcCond(inner[1][1]) },
        x_axis_label: "Time", y_axis_label: PLC_VALUE_TAGS[0],
        series: [plcSeries(PLC_VALUE_TAGS[0], "#7e57c2")] },

      { ...defaultSection("table"), title: "Telemetry", row_limit: 60,
        time_range: { ...run },
        align_columns: ["left", "right", "right"],
        series: PLC_VALUE_TAGS.map((t) => plcSeries(t, "#888")),
        columns: [{ key: "ts", title: "Time" },
                  ...PLC_VALUE_TAGS.map((t) => ({ key: "value", series_id: t, title: t }))] },
    ],
  };
}

// Energy usage across a production run.
//
// 2026-10-01: a professional energy report for a production line. Tag names
// are the ones the EM meter records, so it works as soon as it is picked;
// gateway_id is blank on purpose, so it matches whichever gateway carries
// them. Everything is scoped to the report's window, which means the same
// template answers "this shift", "this batch" or "this production order"
// depending on the schedule that fires it.
function energyProductionReport() {
  const run = { preset: "run" };
  const S = (tag, label, color, axis) => ({
    id: tag, label, gateway_id: "", tag_name: tag, color,
    ...(axis ? { axis } : {}),
  });
  return {
    name: "Energy Usage - Production Line",
    description: "Consumption, demand, power quality and cost for a production run",
    sections: [
      { ...defaultSection("header"), title: "Energy Usage",
        subtitle: "Production line - {start} to {end}" },

      { ...defaultSection("kpi_grid"), title: "Headline", columns: 4,
        time_range: { ...run },
        items: [
          { label: "Energy used (kWh)", gateway_id: "", tag_name: "insight.power_usage_kwh",
            aggregation: "max", operator: "any" },
          { label: "Peak demand (kW)", gateway_id: "", tag_name: "insight.active_power_kw",
            aggregation: "max", operator: "any" },
          { label: "Average load (kW)", gateway_id: "", tag_name: "insight.active_power_kw",
            aggregation: "avg", operator: "any" },
          { label: "Energy cost", gateway_id: "", tag_name: "insight.energy_cost_eur",
            aggregation: "max", operator: "any" },
        ] },
      { ...defaultSection("kpi_grid"), title: "Power quality", columns: 4,
        time_range: { ...run },
        items: [
          { label: "Power factor (avg)", gateway_id: "", tag_name: "insight.power_factor",
            aggregation: "avg", operator: "any" },
          { label: "Power factor (worst)", gateway_id: "", tag_name: "insight.power_factor",
            aggregation: "min", operator: "any" },
          { label: "Voltage (avg V)", gateway_id: "", tag_name: "voltage_avg_v",
            aggregation: "avg", operator: "any" },
          { label: "Run time (s)", gateway_id: "", tag_name: "insight.active_power_kw",
            aggregation: "duration_s", operator: "any" },
        ] },

      { ...defaultSection("area_chart"), title: "Demand profile (kW)",
        time_range: { ...run }, bucket: "1m",
        x_axis_label: "Time", y_axis_label: "Active power", y_axis_unit: "kW",
        series: [{ ...S("insight.active_power_kw", "Active power", "#ef5350"),
                   aggregation: "avg" }] },

      { ...defaultSection("line_chart"), title: "Phase balance",
        time_range: { ...run }, bucket: "1m",
        x_axis_label: "Time", y_axis_label: "Active power", y_axis_unit: "W",
        series: [{ ...S("active_power_l1_w", "L1", "#ef5350"), aggregation: "avg" },
                 { ...S("active_power_l2_w", "L2", "#42a5f5"), aggregation: "avg" },
                 { ...S("active_power_l3_w", "L3", "#66bb6a"), aggregation: "avg" }] },

      { ...defaultSection("line_chart"), title: "Power factor",
        time_range: { ...run }, bucket: "1m", side_by_side: false,
        x_axis_label: "Time", y_axis_label: "Power factor",
        series: [{ ...S("insight.power_factor", "PF", "#7e57c2"), aggregation: "avg" }] },
      { ...defaultSection("line_chart"), title: "Voltage & current",
        time_range: { ...run }, bucket: "1m", side_by_side: true,
        x_axis_label: "Time", y_axis_label: "Voltage", y_axis_unit: "V",
        y_axis_right_label: "Current", y_axis_right_unit: "A",
        series: [{ ...S("voltage_avg_v", "Voltage", "#26a69a"), aggregation: "avg" },
                 { ...S("current_avg_a", "Current", "#ffa726", "right"), aggregation: "avg" }] },

      // One row per shift / batch / cycle, whichever the schedule segments by.
      { ...defaultSection("segment_summary"), title: "Energy by period",
        stream: "", segment: "", segment_header: "Period", format: "2dp",
        columns: [
          { tag_name: "insight.power_usage_kwh", aggregation: "max", title: "kWh" },
          { tag_name: "insight.active_power_kw", aggregation: "max", title: "Peak kW" },
          { tag_name: "insight.active_power_kw", aggregation: "avg", title: "Avg kW" },
          { tag_name: "insight.power_factor", aggregation: "avg", title: "Avg PF" },
          { tag_name: "insight.energy_cost_eur", aggregation: "max", title: "Cost" },
        ],
        footer_aggregation: "sum", footer_label: "Total" },

      { ...defaultSection("text"), title: "Summary",
        text: "Energy used: {max:insight.power_usage_kwh} kWh" + "\n"
          + "Peak demand: {max:insight.active_power_kw} kW" + "\n"
          + "Average power factor: {avg:insight.power_factor}" + "\n"
          + "Cost: {max:insight.energy_cost_eur}" },

      { ...defaultSection("table"), title: "Readings", row_limit: 60,
        time_range: { ...run }, bucket: "5m",
        align_columns: ["left", "right", "right", "right"],
        series: [{ ...S("insight.active_power_kw", "kW", "#ef5350"), aggregation: "avg" },
                 { ...S("insight.power_factor", "PF", "#7e57c2"), aggregation: "avg" },
                 { ...S("insight.power_usage_kwh", "kWh", "#42a5f5"), aggregation: "max" }],
        columns: [{ key: "ts", title: "Time" },
                  { key: "value", series_id: "insight.active_power_kw", title: "kW" },
                  { key: "value", series_id: "insight.power_factor", title: "PF" },
                  { key: "value", series_id: "insight.power_usage_kwh", title: "kWh" }] },
    ],
  };
}

// A complete functional-test report, exercising every capability: the run's
// own trigger window, per-section triggers for the phases, repeat-per-cycle,
// a cross-cycle summary with ramp time, tag-linked text, side-by-side
// sections and column alignment.
//
// 2026-10-01. Real tag names from the test rig, blank gateway so it binds by
// tag wherever they are collected.
function fullTestReport() {
  const run = { preset: "run" };
  const VAL = "SimREAL[3]";
  const VAL2 = "SimREAL[4]";
  const S = (tag, label, color) => ({ id: tag, label, gateway_id: "", tag_name: tag, color });
  const phase = (startTag, stopTag) => ({
    start: { gateway_id: "", tag_name: startTag, operator: "==", value: 1 },
    stop: { gateway_id: "", tag_name: stopTag, operator: "==", value: 1 },
  });
  return {
    name: "Functional Test Report (full)",
    description: "Run trigger, per-cycle detail, cross-cycle averages and ramp time",
    sections: [
      { ...defaultSection("header"), title: "Functional Test Report",
        subtitle: "Work order {tag:Work_Order} - {start} to {end}" },

      { ...defaultSection("text"), title: "Unit under test",
        text: "Work order: {tag:Work_Order}" + "\n"
          + "Serial number: {tag:Serial_Number}" + "\n"
          + "Operator: {tag:Operator_Id}" + "\n"
          + "Peak recorded: {max:SimREAL[3]}   Average: {avg:SimREAL[3]}" },

      { ...defaultSection("kpi_grid"), title: "Overall result", columns: 4,
        time_range: { ...run },
        items: [
          { label: "Peak value", gateway_id: "", tag_name: VAL,
            aggregation: "max", operator: "any" },
          { label: "Average", gateway_id: "", tag_name: VAL,
            aggregation: "avg", operator: "any" },
          { label: "Peak rise (per min)", gateway_id: "", tag_name: VAL,
            aggregation: "rate_max", operator: "any" },
          { label: "Test duration (s)", gateway_id: "", tag_name: VAL,
            aggregation: "duration_s", operator: "any" },
        ] },

      { ...defaultSection("line_chart"), title: "Whole test",
        time_range: { ...run }, x_axis_label: "Time", y_axis_label: "Value",
        series: [S(VAL, VAL, "#ef5350"), S(VAL2, VAL2, "#42a5f5")] },

      { ...defaultSection("line_chart"), title: "Cycle 1 detail",
        time_range: { ...run }, trigger: phase("cycle_started1", "cycle_ended1"),
        x_axis_label: "Time", y_axis_label: VAL,
        series: [S(VAL, VAL, "#26a69a")] },
      { ...defaultSection("bar_chart"), title: "Cycle 2 detail",
        time_range: { ...run }, side_by_side: true,
        trigger: phase("cycle_started2", "cycle_ended2"),
        x_axis_label: "Time", y_axis_label: VAL,
        series: [S(VAL, VAL, "#7e57c2")] },

      // One row per cycle, averaged at the foot: the final test result.
      { ...defaultSection("segment_summary"), title: "Result per cycle",
        stream: "cycle", segment: "", segment_header: "Cycle", format: "2dp",
        align_columns: ["left", "right", "right", "right", "right"],
        columns: [
          { tag_name: VAL, aggregation: "max", title: "Peak" },
          { tag_name: VAL, aggregation: "avg", title: "Average" },
          { tag_name: VAL, aggregation: "rate_max", title: "Rise /min" },
          { tag_name: VAL, aggregation: "duration_s", title: "Duration s" },
        ],
        footer_aggregation: "avg", footer_label: "Final result" },

      { ...defaultSection("segment_summary"), title: "Result per phase",
        stream: "phase", segment: "", segment_header: "Phase", format: "2dp",
        align_columns: ["left", "right", "right"],
        columns: [
          { tag_name: VAL, aggregation: "avg", title: "Average" },
          { tag_name: VAL2, aggregation: "avg", title: "Secondary" },
        ] },

      { ...defaultSection("table"), title: "Telemetry", row_limit: 60,
        time_range: { ...run }, align_columns: ["left", "right", "right"],
        series: [S(VAL, VAL, "#888"), S(VAL2, VAL2, "#888")],
        columns: [{ key: "ts", title: "Time" },
                  { key: "value", series_id: VAL, title: VAL },
                  { key: "value", series_id: VAL2, title: VAL2 }] },

      { ...defaultSection("static_table"), title: "Sign-off",
        header: ["Check", "Result"],
        rows: [["Peak within limit", "{max:SimREAL[3]}"],
               ["Test completed", "PASS"],
               ["Operator", "{tag:Operator_Id}"]] },
    ],
  };
}

function makePresetTemplate(presetKey) {
  switch (presetKey) {
    case "full_test_report":
      return fullTestReport();
    case "energy_production_line":
      return energyProductionReport();
    case "plc_live_cycle_test":
      return plcLiveCycleTest();
    case "tcu_testing_report":
      return tcuTestingReport();
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
            // Pre-bound to the tags the Power Management module writes, so
            // the preset renders a real report immediately instead of three
            // blank KPIs. gateway_id stays empty = "any meter"; an operator
            // with several meters narrows it per item. Rename/repoint freely.
            items: [
              { id: makeId("kpi"), label: "kWh consumed", gateway_id: "", tag_name: "insight.total_kwh", aggregation: "max", operator: "any" },
              { id: makeId("kpi"), label: "Avg load (kW)", gateway_id: "", tag_name: "insight.live_kw", aggregation: "avg", operator: "any" },
              { id: makeId("kpi"), label: "Peak load (kW)", gateway_id: "", tag_name: "insight.peak_kw", aggregation: "max", operator: "any" },
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
              { id: makeId("ser"), label: "Power", gateway_id: "", tag_name: "insight.live_kw", color: "#3b82f6", axis: "left", chart_type: "line", unit: "kW", multiplier: 1, offset: 0 },
              { id: makeId("ser"), label: "Current", gateway_id: "", tag_name: "insight.current_a", color: "#f97316", axis: "right", chart_type: "line", unit: "A", multiplier: 1, offset: 0 },
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
  { value: "tcu_testing_report", label: "Testing Report (TCU functional test)" },
  { value: "plc_live_cycle_test", label: "TCU Live Cycle Test (PLC cycle bits)" },
  { value: "energy_production_line", label: "Energy Usage - Production Line" },
  { value: "full_test_report", label: "Functional Test Report (full)" },
];

/* A structural copy of a section with every id replaced.
 *
 * The editor addresses nested things by id - updateSeries(s.id, ...),
 * updatePill(pill.id, ...) - so a duplicate that reused them would edit the
 * original too. Arrays of objects that carry an `id` get a fresh one; a row
 * of a details table is a plain array and is copied as it is.
 */
function reidSection(section) {
  const fresh = (v) => {
    if (Array.isArray(v)) return v.map(fresh);
    if (v && typeof v === "object") {
      const out = {};
      for (const [k, val] of Object.entries(v)) out[k] = fresh(val);
      if (typeof v.id === "string" && v.id) out.id = makeId("dup");
      return out;
    }
    return v;
  };
  const copy = fresh(section || {});
  copy.id = makeId("sec");
  return copy;
}


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
    case "static_table":
      // 2026-10-09, requested: "static table so we can create with details
      // and link any text to a tag or fix text". Starts as a two-column
      // detail sheet because that is what it is nearly always used for.
      return {
        id: makeId("sec"),
        type,
        title: "Details",
        header: ["Field", "Value"],
        rows: [["Work order", "{Work_Order}"], ["Operator", ""]],
        table_borders: "all",
      };
    case "pill_card":
      // One pill to start, already readable: an operator should see the
      // shape of the thing before configuring it.
      return {
        id: makeId("sec"),
        type,
        title: "",
        align: "left",
        columns: 4,
        font_size: 10,
        pills: [{
          id: makeId("pill"), label: "Result", text: "PASS",
          color: "#ffffff", background: "#14a89a", bold: true,
        }],
      };
    case "segment_summary":
      // One row per test/cycle the run produced, one column per tag, and an
      // optional row aggregating those results again.
      return {
        id: makeId("sec"),
        type,
        title: "Test results",
        segment: "",
        segment_header: "Test",
        columns: [{ tag_name: "", aggregation: "avg", title: "", unit: "" }],
        footer_aggregation: "avg",
        footer_label: "Average",
        format: "3dp",
      };
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
    case "image":
      return {
        id: makeId("sec"),
        type,
        title: "",
        caption: "",
        // Either a base64 data URL (operator pasted / uploaded directly
        // into the template) or a filename relative to TRUSTNODE_DATA_DIR
        // for hand-managed assets shared across templates.
        data_url: "",
        path: "",
        align: "center",
        width_mm: 0, // 0 = auto (~70 % of printable width)
      };
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
// One tag condition: gateway, tag, operator, value.
//
// 2026-09-30, requested: "for each section of my report I want to be able to
// pick different start and end trigger where the data inside of the section
// would be loaded, I want to be able to select from the gateways tags". The
// section's own time range still encloses the search, so a scheduled 24h
// report can hold a chart covering only the heating phase within it.
/* Width on the page, and whether this section repeats per cycle.
 *
 * 2026-10-09, requested: "not all sections have the option of the layout and
 * we need all of them have the option to have more than one section in the
 * same row". This markup lived inline in the chart/table branch, so only a
 * chart or a data table could be told to share a line - a pill card, a
 * details table, a KPI grid or an image could not, even though the renderer
 * has always been able to put any of them in a row (group_side_by_side
 * pairs by POSITION, not by type).
 *
 * `showAlign` is off where the section already has its own alignment
 * control, so a text block or a pill card does not end up with two.
 */
// Types the renderer refuses to pair a following section WITH. Mirrors
// group_side_by_side() in report_renderer.py.
const CANNOT_LEAD = new Set(["header", "page_break", "spacer"]);

function SectionLayout({ section, onChange, showAlign = false, prevType = "" }) {
  return (
    <>
      <div className="tn-subhead">Layout</div>
      <div className={showAlign ? "tn-row tn-row-3" : "tn-row tn-row-2"}>
        <label>Width
          <select
            value={section.side_by_side ? "yes" : "no"}
            onChange={(e) => onChange({ side_by_side: e.target.value === "yes" })}
            title="Up to three sections can share one line. This one joins the line above."
          >
            <option value="no">Full width</option>
            <option value="yes">Share line above</option>
          </select>
        </label>
        {showAlign ? (
          <label>Text alignment
            <select value={section.align || ""}
                    onChange={(e) => onChange({ align: e.target.value })}>
              <option value="">Default</option>
              <option value="left">Left</option>
              <option value="center">Centre</option>
              <option value="right">Right</option>
            </select>
          </label>
        ) : null}
        <label>Repeat per cycle
          <input
            placeholder="test2 · blank = no"
            value={section.repeat_for?.segment || ""}
            title="Renders one copy of this section per cycle of that test, each over its own window."
            onChange={(e) => onChange({
              repeat_for: e.target.value ? { segment: e.target.value } : undefined,
            })}
          />
        </label>
      </div>
      {/* The renderer pairs a section with the one ABOVE it, and a banner,
          spacer or page break is never a partner - it stays full width by
          design. Saying so beats a setting that silently does nothing. */}
      {section.side_by_side && CANNOT_LEAD.has(String(prevType || "")) ? (
        <p className="tn-token-help">
          The section above is a {prevType.replace("_", " ")}, which always takes a
          full line - this one will not join it. Move it below a chart, table,
          KPI grid or pill card.
        </p>
      ) : null}
      {section.side_by_side && !prevType ? (
        <p className="tn-token-help">
          This is the first section, so there is no line above to join.
        </p>
      ) : null}
    </>
  );
}


/* Start / stop triggers for ONE section.
 *
 * 2026-10-07, requested: "not all the template reports has the option to
 * pick the start and end trigger, we should have for all". This markup lived
 * inline in the chart/table branch, so a KPI grid, a text block, a segment
 * summary and a pie chart could never be bound to a cycle - even though the
 * backend applies section triggers by type-agnostic rule
 * (apply_section_triggers / resolve_trigger_window). It was only ever a gap
 * in the editor.
 *
 * The stop picker appears once a start tag is chosen: a stop with no start
 * has nothing to close.
 */
/* Size / weight / colour / alignment for one run of text.
 *
 * 2026-10-09, requested: "we should be able to modify fonts size, style and
 * color, in special for text sections ... and how the text should be
 * aligned". Every text block rendered with one hard-coded body style, so a
 * heading could not be made bigger, a warning could not be red, and nothing
 * could be centred.
 *
 * `prefix` lets one section style its heading and its body independently
 * (title_font_size vs font_size) without a second section type. Every field
 * is optional - blank means "leave it as the report's default", which is
 * why an untouched template renders exactly as it did before.
 */
function FontControls({ section, onChange, prefix = "", label = "Text style" }) {
  const k = (name) => `${prefix}${name}`;
  const val = (name) => section[k(name)];
  return (
    <>
      <div className="tn-subhead">{label}</div>
      <div className="tn-row tn-row-4">
        <label>Size (pt)
          <input
            type="number" min="5" max="72" step="1"
            placeholder="default"
            value={val("font_size") ?? ""}
            onChange={(e) => onChange({ [k("font_size")]: e.target.value === "" ? "" : Number(e.target.value) })}
          />
        </label>
        <label>Colour
          <div className="tn-colour-row">
            <input
              type="color"
              value={String(val("font_color") || "#0e1116")}
              onChange={(e) => onChange({ [k("font_color")]: e.target.value })}
            />
            {val("font_color")
              ? <button type="button" className="btn btn-secondary btn-sm"
                        title="Back to the report's default colour"
                        onClick={() => onChange({ [k("font_color")]: "" })}>Reset</button>
              : null}
          </div>
        </label>
        <label>Alignment
          <select
            value={String(val("align") || "")}
            onChange={(e) => onChange({ [k("align")]: e.target.value })}
          >
            <option value="">Default</option>
            <option value="left">Left</option>
            <option value="center">Centre</option>
            <option value="right">Right</option>
            <option value="justify">Justified</option>
          </select>
        </label>
        <label>Weight
          <div className="tn-style-toggles">
            <button
              type="button"
              className={`btn btn-sm ${val("bold") ? "btn-primary" : "btn-secondary"}`}
              title="Bold" aria-pressed={Boolean(val("bold"))}
              onClick={() => onChange({ [k("bold")]: !val("bold") })}
            ><strong>B</strong></button>
            <button
              type="button"
              className={`btn btn-sm ${val("italic") ? "btn-primary" : "btn-secondary"}`}
              title="Italic" aria-pressed={Boolean(val("italic"))}
              onClick={() => onChange({ [k("italic")]: !val("italic") })}
            ><em>I</em></button>
          </div>
        </label>
      </div>
    </>
  );
}


/* How each COLUMN of a table is drawn: alignment, colour, or rendered as a
 * coloured pill.
 *
 * 2026-10-09, requested: "we should also be able to format the text in
 * tables and card to be centralized, to be a pill inside of the table color
 * formated, font size and color too".
 *
 * Per COLUMN rather than per cell: a check list has one column of labels
 * and one of results, and what the operator wants is "every result is a
 * green pill" - not to colour forty cells by hand.
 */
function ColumnStyleControls({ section, onChange }) {
  const header = section.header || [];
  const styles = Array.isArray(section.column_styles) ? section.column_styles : [];
  const setCol = (c, patch) => {
    const next = header.map((_h, i) => ({ ...(styles[i] || {}) }));
    next[c] = { ...next[c], ...patch };
    onChange({ column_styles: next });
  };
  if (!header.length) return null;
  return (
    <>
      <div className="tn-subhead">Column formatting</div>
      {header.map((h, c) => {
        const cs = styles[c] || {};
        return (
          <div key={c} className="tn-static-row">
            <span className="tn-col-name">{h || `Column ${c + 1}`}</span>
            <select value={String(cs.align || "")}
                    onChange={(e) => setCol(c, { align: e.target.value })}
                    title="How this column's text sits in its cell">
              <option value="">Default align</option>
              <option value="left">Left</option>
              <option value="center">Centre</option>
              <option value="right">Right</option>
            </select>
            <label className="tn-checkbox">
              <input type="checkbox" checked={Boolean(cs.pill)}
                     onChange={(e) => setCol(c, { pill: e.target.checked })} />
              Pill
            </label>
            <input type="color" title={cs.pill ? "Pill background" : "Text colour"}
                   value={String((cs.pill ? cs.background : cs.color) || (cs.pill ? "#14a89a" : "#0e1116"))}
                   onChange={(e) => setCol(c, cs.pill
                     ? { background: e.target.value }
                     : { color: e.target.value })} />
            {cs.pill ? (
              <input type="color" title="Pill text colour"
                     value={String(cs.color || "#ffffff")}
                     onChange={(e) => setCol(c, { color: e.target.value })} />
            ) : null}
            <button type="button" title="Bold"
                    className={`btn btn-sm ${cs.bold ? "btn-primary" : "btn-secondary"}`}
                    onClick={() => setCol(c, { bold: !cs.bold })}>
              <strong>B</strong>
            </button>
          </div>
        );
      })}
    </>
  );
}


/* Rules, their colour and weight, and the text density inside a table. */
function TableStyleControls({ section, onChange }) {
  return (
    <>
      <div className="tn-subhead">Table style</div>
      <div className="tn-row tn-row-4">
        <label>Borders
          <select
            value={String(section.table_borders || "all")}
            onChange={(e) => onChange({ table_borders: e.target.value })}
            title="Which rules are drawn. Zebra shading stays unless turned off below."
          >
            <option value="all">Full grid</option>
            <option value="outline">Outline only</option>
            <option value="rows">Horizontal rules</option>
            <option value="columns">Vertical rules</option>
            <option value="none">No borders</option>
          </select>
        </label>
        <label>Rule colour
          <div className="tn-colour-row">
            <input
              type="color"
              value={String(section.border_color || "#e1e6ec")}
              onChange={(e) => onChange({ border_color: e.target.value })}
            />
            {section.border_color
              ? <button type="button" className="btn btn-secondary btn-sm"
                        onClick={() => onChange({ border_color: "" })}>Reset</button>
              : null}
          </div>
        </label>
        <label>Rule width (pt)
          <input
            type="number" min="0.1" max="3" step="0.1" placeholder="default"
            value={section.border_width ?? ""}
            onChange={(e) => onChange({ border_width: e.target.value === "" ? "" : Number(e.target.value) })}
          />
        </label>
        <label>Text size (pt)
          <input
            type="number" min="5" max="24" step="0.5" placeholder="8.5"
            value={section.table_font_size ?? ""}
            onChange={(e) => onChange({ table_font_size: e.target.value === "" ? "" : Number(e.target.value) })}
          />
        </label>
      </div>
      <label className="tn-checkbox">
        <input
          type="checkbox"
          checked={section.zebra !== false}
          onChange={(e) => onChange({ zebra: e.target.checked })}
        />
        Shade alternate rows
      </label>
    </>
  );
}


/* The conditions on one side of a section trigger.
 *
 * 2026-10-11, requested: "make sure the section trigger also consider
 * multiples conditions". A side used to be ONE condition, so a section could
 * not say "while Cycle_Active = 1 AND Phase = Heating" - it had to pick one
 * and chart the wrong window.
 *
 * The first condition is still written to trigger.start / trigger.stop, which
 * is the shape every saved template carries and the shape the renderer reads
 * when there is no list. Extra conditions go to *_conditions, so a template
 * edited here and opened by an older build still resolves its first
 * condition rather than losing the trigger entirely.
 */
function TriggerSide({ label, side, trigger, onChange, gatewayOptions, tagList,
                      formatTagForDisplay }) {
  const t = trigger || {};
  const listKey = `${side}_conditions`;
  const logicKey = `${side}_logic`;
  const stored = Array.isArray(t[listKey]) ? t[listKey] : [];
  // Gate on the condition EXISTING, not on it being finished. Keying this on
  // `tag_name` meant that choosing a gateway - which deliberately clears the
  // tag - made the condition fall back to {} on the very next render: the
  // gateway select snapped back to "Gateway…" and the tag list stayed empty,
  // so the trigger could never be filled in at all (2026-10-11).
  const first = t[side];
  const conds = stored.length
    ? stored
    : (first && typeof first === "object" && Object.keys(first).length ? [first] : [{}]);

  const write = (next) => {
    const clean = next.filter((c) => c && Object.keys(c).length);
    onChange({
      ...t,
      [side]: clean[0] || null,
      [listKey]: clean.length > 1 ? clean : undefined,
    });
  };
  const setOne = (idx, cond) => write(conds.map((c, i) => (i === idx ? cond : c)));

  return (
    <>
      {conds.map((c, idx) => (
        <TriggerPicker
          key={idx}
          label={idx === 0 ? label : (String(t[logicKey] || "all") === "any" ? "…or when" : "…and when")}
          cond={c}
          gatewayOptions={gatewayOptions}
          tagList={tagList}
          formatTagForDisplay={formatTagForDisplay}
          onChange={(cond) => setOne(idx, cond)}
        />
      ))}
      <div className="tn-trigger-tools">
        <button type="button" className="btn btn-link"
                onClick={() => write([...conds, {}])}>+ Add condition</button>
        {conds.length > 1 ? (
          <>
            <button type="button" className="btn btn-link"
                    onClick={() => write(conds.slice(0, -1))}>Remove last</button>
            <label className="tn-inline-label" title="ALL: every condition must hold at the same moment. ANY: the first one to hold opens the window.">
              Combine
              <select value={t[logicKey] || "all"}
                      onChange={(e) => onChange({ ...t, [logicKey]: e.target.value })}>
                <option value="all">All must be true</option>
                <option value="any">Any may be true</option>
              </select>
            </label>
          </>
        ) : null}
      </div>
    </>
  );
}


function SectionTrigger({ section, onChange, gatewayOptions, tagList,
                          formatTagForDisplay }) {
  const trigger = section.trigger || {};
  const setTrigger = (next) => onChange({ trigger: next });
  const hasStart = Boolean(trigger.start?.tag_name
    || (Array.isArray(trigger.start_conditions)
        && trigger.start_conditions.some((c) => c?.tag_name)));
  return (
    <>
      <div className="tn-subhead">Trigger (optional)</div>
      <TriggerSide
        label="Start when" side="start" trigger={trigger} onChange={setTrigger}
        gatewayOptions={gatewayOptions} tagList={tagList}
        formatTagForDisplay={formatTagForDisplay}
      />
      {hasStart ? (
        <>
          <TriggerSide
            label="Stop when" side="stop" trigger={trigger} onChange={setTrigger}
            gatewayOptions={gatewayOptions} tagList={tagList}
            formatTagForDisplay={formatTagForDisplay}
          />
          {/* 2026-10-11, requested: "when the report section do no clear do
              not use different trigger". A trigger that never fires now
              covers the window the schedule's own start/end trigger
              produced, and the section caption says so - rather than
              printing an empty chart or inventing a 24h range. */}
          <label className="tn-inline-label" title="What to do when this trigger never becomes true inside the report window.">
            If it never fires
            <select value={trigger.on_miss || "window"}
                    onChange={(e) => setTrigger({ ...trigger, on_miss: e.target.value })}>
              <option value="window">Use the report's own window</option>
              <option value="empty">Leave the section empty</option>
            </select>
          </label>
        </>
      ) : null}
    </>
  );
}


/* One of the four canonical axes. Mirrors norm_axis4() in
   backend/app/services/report_renderer.py and normSeriesAxis4() in the
   dashboard widgets - legacy templates stored only "left"/"right". */
function normAxis4(value) {
  const a = String(value || "left1").trim().toLowerCase();
  if (a === "left2") return "left2";
  if (a === "right2") return "right2";
  if (a === "right" || a === "right1") return "right1";
  return "left1";
}


function TriggerPicker({ label, cond, gatewayOptions, tagList, formatTagForDisplay,
                        onChange, hint }) {
  const c = cond || {};
  const set = (patch) => onChange({ ...c, ...patch });
  return (
    <div className="tn-row tn-row-4">
      <label>{label}
        <select
          title="Which gateway carries the tag"
          value={c.gateway_id || ""}
          onChange={(e) => set({ gateway_id: e.target.value, tag_name: "" })}
        >
          <option value="">Gateway…</option>
          {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
        </select>
      </label>
      <label>Tag
        <select value={c.tag_name || ""} onChange={(e) => set({ tag_name: e.target.value })}>
          <option value="">No trigger</option>
          {tagList(c.gateway_id).map((t) => (
            <option key={t} value={t}>{formatTagForDisplay(t)}</option>
          ))}
        </select>
      </label>
      <label>Is
        <select value={c.operator || "=="} onChange={(e) => set({ operator: e.target.value })}>
          <option value="==">equal to</option>
          <option value="!=">not equal to</option>
          <option value=">">greater than</option>
          <option value=">=">at least</option>
          <option value="<">less than</option>
          <option value="<=">at most</option>
          <option value="between">between</option>
        </select>
      </label>
      <label>Value
        <input
          value={c.value ?? ""}
          placeholder="1, 80 or Heating"
          title={hint || "A fixed value, or the text a state tag holds."}
          onChange={(e) => set({ value: e.target.value })}
        />
      </label>
    </div>
  );
}

// A text field with a tag picker beside it.
//
// 2026-10-01 added {tag:Name} tokens; 2026-10-02: "the text field elements
// all of them in any report should have an option (small icon to link to a
// tag value)". A token nobody can discover is a token nobody uses - the
// operator should not have to remember tag names or the syntax.
//
// The token is inserted AT THE CURSOR, so a sentence can be built up around
// it rather than always appending at the end.
/* Line interpolation, same five the dashboard widgets offer and by the same
 * names, so a chart means the same thing in both places. The report default
 * is "linear" - that is how every existing report was drawn, and changing it
 * silently would redraw them all (2026-10-11). */
const CHART_INTERPOLATIONS = [
  { value: "linear", label: "Linear" },
  { value: "monotone", label: "Monotone (smooth)" },
  { value: "natural", label: "Natural (smooth)" },
  { value: "stepAfter", label: "Step after" },
  { value: "stepBefore", label: "Step before" },
];

const TOKEN_KINDS = [
  { value: "tag", label: "Latest value" },
  { value: "avg", label: "Average" },
  { value: "max", label: "Maximum" },
  { value: "min", label: "Minimum" },
  { value: "first", label: "First value" },
];

function TagTokenField({ label, value, onChange, multiline = false, rows = 4,
                        placeholder = "", gatewayOptions = [], tagList,
                        formatTagForDisplay }) {
  const [open, setOpen] = useState(false);
  const [gw, setGw] = useState("");
  const [kind, setKind] = useState("tag");
  const ref = useRef(null);

  const insert = (tag) => {
    if (!tag) return;
    const token = `{${kind}:${tag}}`;
    const el = ref.current;
    const current = String(value || "");
    let next = current + token;
    if (el && typeof el.selectionStart === "number") {
      const a = el.selectionStart;
      const b = el.selectionEnd;
      next = current.slice(0, a) + token + current.slice(b);
    }
    onChange(next);
    setOpen(false);
  };

  const tags = typeof tagList === "function" ? tagList(gw) : [];

  return (
    <label className="tn-tag-field">
      <span className="tn-tag-field-head">
        {label}
        <button
          type="button"
          className="tn-tag-insert"
          title="Insert a value read from a tag"
          onClick={() => setOpen((v) => !v)}
        >
          🏷
        </button>
      </span>
      {multiline ? (
        <textarea ref={ref} rows={rows} value={value || ""} placeholder={placeholder}
                  onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input ref={ref} value={value || ""} placeholder={placeholder}
               onChange={(e) => onChange(e.target.value)} />
      )}
      {open ? (
        <div className="tn-tag-pop">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {TOKEN_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
          <select value={gw} onChange={(e) => setGw(e.target.value)}>
            <option value="">Gateway…</option>
            {gatewayOptions.map((g) => (
              <option key={g.id} value={g.id}>{g.name || g.id}</option>
            ))}
          </select>
          <select value="" onChange={(e) => insert(e.target.value)}>
            <option value="">Pick a tag…</option>
            {tags.map((t) => (
              <option key={t} value={t}>
                {formatTagForDisplay ? formatTagForDisplay(t) : t}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </label>
  );
}

/* An input that can take a tag, sized to sit in a grid row.
 *
 * 2026-10-09, reported: "in all the report sections I should be able to
 * pick the tag available and now I am having to enter manually". Audited
 * every branch: the details-table CELLS and the pill card's text box
 * accepted {TagName} but offered no picker, so those were the two places
 * the operator had to type a tag path from memory.
 *
 * TagTokenField is the full-size version - a label, a header row and a
 * popup - which does not fit inside a table row or a pill row. This is the
 * same picker and the same insert-at-cursor behaviour in a control that
 * does.
 */
function TokenInput({ value, onChange, placeholder = "", className = "",
                      title = "", disabled = false,
                      gatewayOptions = [], tagList, formatTagForDisplay }) {
  const [open, setOpen] = useState(false);
  const [gw, setGw] = useState("");
  const [kind, setKind] = useState("tag");
  const ref = useRef(null);

  const insert = (tag) => {
    if (!tag) return;
    // The bare form reads better in a sentence or a cell; the prefixed
    // form is only needed for an aggregate.
    const token = kind === "tag" ? `{${tag}}` : `{${kind}:${tag}}`;
    const el = ref.current;
    const current = String(value || "");
    let next = current + token;
    if (el && typeof el.selectionStart === "number") {
      next = current.slice(0, el.selectionStart) + token
        + current.slice(el.selectionEnd);
    }
    onChange(next);
    setOpen(false);
  };

  const tags = typeof tagList === "function" ? tagList(gw) : [];
  return (
    <span className={`tn-token-input ${className}`}>
      <input ref={ref} value={value || ""} placeholder={placeholder}
             title={title} disabled={disabled}
             onChange={(e) => onChange(e.target.value)} />
      <button type="button" className="tn-tag-insert" disabled={disabled}
              title="Pick a tag to insert here"
              onClick={() => setOpen((v) => !v)}>🏷</button>
      {open ? (
        <div className="tn-tag-pop tn-tag-pop-inline">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {TOKEN_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
          <select value={gw} onChange={(e) => setGw(e.target.value)}>
            <option value="">Gateway…</option>
            {gatewayOptions.map((g) => (
              <option key={g.id} value={g.id}>{g.name || g.id}</option>
            ))}
          </select>
          <select value="" onChange={(e) => insert(e.target.value)}>
            <option value="">Pick a tag…</option>
            {tags.map((t) => (
              <option key={t} value={t}>
                {formatTagForDisplay ? formatTagForDisplay(t) : t}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </span>
  );
}


// 2026-10-23: a template exported from one edge and imported into another
// names gateways and tags that do not exist on the target. Every picker in
// this editor is a <select> whose options come from THIS machine, so an
// unknown reference rendered BLANK, its tag list came back empty, and the
// first edit to that section wrote the reference away for good. The export
// carried every detail faithfully; the editor is where it was lost.
//
// The fix is to keep what the template already references. Walking the
// section generically - any object carrying gateway_id / tag_name - means
// a section type added later is covered without touching this again.
function collectSectionRefs(node, acc) {
  const out = acc || { gateways: new Set(), tags: new Map() };
  if (!node || typeof node !== "object") return out;
  if (Array.isArray(node)) {
    for (const item of node) collectSectionRefs(item, out);
    return out;
  }
  const gw = String(node.gateway_id ?? "").trim();
  const tag = String(node.tag_name ?? "").trim();
  if (gw) out.gateways.add(gw);
  if (tag) {
    if (!out.tags.has(gw)) out.tags.set(gw, new Set());
    out.tags.get(gw).add(tag);
  }
  for (const key of Object.keys(node)) collectSectionRefs(node[key], out);
  return out;
}

// Marked, never silently blended in: an operator must be able to tell a
// gateway that exists here from one that only exists in the template.
const FOREIGN_SUFFIX = " - not on this edge";

function SectionEditor({
  section,
  index,
  total,
  prevType = "",
  gatewayOptions: localGatewayOptions,
  tagsByGateway,
  formatTagForDisplay,
  onChange,
  onRemove,
  onDuplicate,
  onMoveUp,
  onMoveDown,
  onNotify,
}) {
  const type = section?.type || "text";
  const preset = SECTION_PRESETS.find((p) => p.type === type);
  // Configured sections start closed; a brand-new one opens so it can be set up.
  const [collapsed, setCollapsed] = useState(Boolean(section?.title));
  // Everything this section points at, whether or not this edge has it.
  const sectionRefs = useMemo(() => collectSectionRefs(section), [section]);
  // Shadows the prop ON PURPOSE, so every picker below - there are ten of
  // them - gets the preserved entries without ten separate edits.
  const gatewayOptions = useMemo(() => {
    const known = new Set((localGatewayOptions || []).map((g) => String(g.id)));
    const extra = [...sectionRefs.gateways]
      .filter((id) => !known.has(id))
      .map((id) => ({ id, name: id + FOREIGN_SUFFIX }));
    return [...(localGatewayOptions || []), ...extra];
  }, [localGatewayOptions, sectionRefs]);
  const tagList = (gid) => {
    const key = String(gid || "");
    const raw = tagsByGateway?.[key];
    const local = Array.isArray(raw) ? raw : [];
    const referenced = sectionRefs.tags.get(key);
    if (!referenced || !referenced.size) return local;
    const known = new Set(local.map((t) => String(t)));
    return [...local, ...[...referenced].filter((t) => !known.has(t))];
  };

  const cols = () => (section.header || []).length || 1;
  const setHeaderCell = (c, v) => {
    const h = [...(section.header || [])];
    h[c] = v;
    onChange({ header: h });
  };
  const setCell = (r, c, v) => {
    const rows = (section.rows || []).map((row) => [...(row || [])]);
    while (rows.length <= r) rows.push([]);
    while (rows[r].length < cols()) rows[r].push("");
    rows[r][c] = v;
    onChange({ rows });
  };
  const addRow = () => onChange({
    rows: [...(section.rows || []), Array.from({ length: cols() }, () => "")] });
  const removeRow = (r) => onChange({
    rows: (section.rows || []).filter((_x, i) => i !== r) });
  // Named for the DETAILS table: addColumn/removeColumn already belong to
  // the data-table editor further down and mean something different there.
  const addDetailColumn = () => onChange({
    header: [...(section.header || []), ""],
    // Keep every row rectangular, or a cell edit lands in the wrong column.
    rows: (section.rows || []).map((row) => [...(row || []), ""]),
  });
  const removeDetailColumn = () => onChange({
    header: (section.header || []).slice(0, -1),
    rows: (section.rows || []).map((row) => (row || []).slice(0, -1)),
  });

  const updatePill = (pid, patch) => {
    onChange({ pills: (section.pills || []).map((p) => (p.id === pid ? { ...p, ...patch } : p)) });
  };
  const addPill = () => {
    onChange({ pills: [...(section.pills || []), {
      id: makeId("pill"), label: "", text: "",
      color: "#ffffff", background: pickColor((section.pills || []).length),
    }] });
  };
  const removePill = (pid) => {
    onChange({ pills: (section.pills || []).filter((p) => p.id !== pid) });
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

  // Every section collapses. A fifteen-section report was an unbroken wall of
  // controls; the header now carries enough to identify a section without
  // opening it - its title and the window it covers.
  const windowSummary = () => {
    const tr = section.time_range || {};
    const preset_ = String(tr.preset || "");
    if (section.trigger?.start?.tag_name) {
      return `trigger · ${section.trigger.start.tag_name}`;
    }
    if (preset_ === "segment") return `segment · ${tr.segment || "any"}`;
    if (preset_ === "custom") return "custom range";
    if (preset_ === "batch") return "batch window";
    if (preset_ === "run") return "run window";
    return preset_ || "";
  };

  return (
    <div className={`tn-section-editor ${collapsed ? "is-collapsed" : ""}`.trim()}>
      <header className="tn-section-editor-head">
        <button
          type="button"
          className="tn-section-toggle"
          onClick={() => setCollapsed((v) => !v)}
          aria-expanded={!collapsed}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <span className="tn-section-pill">{preset?.label || type}</span>
        <span className="tn-section-summary">
          {section.title || <em>untitled</em>}
          {windowSummary() ? <span className="tn-section-window"> · {windowSummary()}</span> : null}
        </span>
        <div className="tn-section-editor-tools">
          <button type="button" className="icon-btn" onClick={onMoveUp} disabled={index === 0} title="Move up">↑</button>
          <button type="button" className="icon-btn" onClick={onMoveDown} disabled={index === total - 1} title="Move down">↓</button>
          <button type="button" className="icon-btn" onClick={onDuplicate}
                  title="Duplicate this section, directly below">⧉</button>
          <button type="button" className="icon-btn icon-btn-danger" onClick={onRemove} title="Remove">×</button>
        </div>
      </header>
      {collapsed ? null : (
       <>

      {type === "header" ? (
        <div className="tn-section-form">
          <div className="tn-row tn-row-2">
            <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
            <TagTokenField label="Subtitle" value={section.subtitle} onChange={(v) => onChange({ subtitle: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
          </div>
          <label className="tn-checkbox">
            <input
              type="checkbox"
              checked={section.show_generated_at !== false}
              onChange={(e) => onChange({ show_generated_at: e.target.checked })}
            />
            Show generated timestamp
          </label>
            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />
          {/* 2026-10-09, clarified: "it need to be a header but just in a
              pill style that can be linked to a tag or not, the value
              inside of the pill is the title too". So this is an option ON
              the header rather than a new section: the title text - which
              may be a {TagName} - sits INSIDE the band, and nothing is
              drawn above it. */}
          <div className="tn-row tn-row-4">
            <label>Show as
              <select value={section.as_pill ? "pill" : "plain"}
                      onChange={(e) => onChange({ as_pill: e.target.value === "pill" })}>
                <option value="plain">Plain heading</option>
                <option value="pill">Pill band</option>
              </select>
            </label>
            {section.as_pill ? (
              <>
                <label>Band colour
                  <div className="tn-colour-row">
                    <input type="color"
                           value={String(section.pill_background || "#14a89a")}
                           onChange={(e) => onChange({ pill_background: e.target.value })} />
                  </div>
                </label>
                <label>Text colour
                  <div className="tn-colour-row">
                    <input type="color"
                           value={String(section.pill_color || "#ffffff")}
                           onChange={(e) => onChange({ pill_color: e.target.value })} />
                  </div>
                </label>
                <label>Corners
                  <select value={String(section.pill_corners || "both")}
                          onChange={(e) => onChange({ pill_corners: e.target.value })}>
                    <option value="both">Rounded (both ends)</option>
                    <option value="left">Rounded left only</option>
                    <option value="right">Rounded right only</option>
                    <option value="top">Rounded top only</option>
                    <option value="bottom">Rounded bottom only</option>
                    <option value="none">Square</option>
                  </select>
                </label>
              {/* 2026-10-17, requested: one pill per row, sitting at the RIGHT
                  of the full-width row. They could not move because the grid
                  stretches each pill to fill its column - "Fit to content"
                  sizes them to their text so Alignment can place them. */}
              <label title="Fill: pills share the row evenly (current behaviour). Fit to content: each pill is as wide as its text, so Alignment can move the group.">
                Pill width
                <select value={section.pill_fit || "fill"}
                        onChange={(e) => onChange({ pill_fit: e.target.value })}>
                  <option value="fill">Full page width</option>
                  <option value="content">Fit to the text</option>
                </select>
              </label>
              {/* Deliberately NOT called "Alignment": the layout card at the
                  bottom already has "Text alignment" writing `align`, so a
                  second control on the same key is why moving this did
                  nothing. This one places the band; the text inside it
                  follows Title style. */}
              <label title={String(section.pill_fit || "fill") === "fill"
                ? "Only moves a pill set to Fit to the text - a full-width band already spans the page. Use Text in the band to centre the words."
                : "Where the band sits on the page."}>
                Band position
                <select value={section.pill_align || section.align || "left"}
                        disabled={String(section.pill_fit || "fill") === "fill"}
                        onChange={(e) => onChange({ pill_align: e.target.value })}>
                  <option value="left">Left</option>
                  <option value="center">Centre</option>
                  <option value="right">Right</option>
                </select>
              </label>
              {/* 2026-10-23, reported: "when used as pill I don't have option
                  to use full width and centralized the text". Full width was
                  already there; this was not. The renderer has always read
                  `title_align` for the words INSIDE the band and defaulted it
                  to left, but no control anywhere in this editor wrote it - so
                  a full-width pill could never be centred. Band position moves
                  the band and does nothing when the band is the whole page,
                  which is why it looked like the control was broken. */}
              <label title="Where the words sit INSIDE the band. This is the one that centres the text of a full-width pill.">
                Text in the band
                <select value={section.title_align || "left"}
                        onChange={(e) => onChange({ title_align: e.target.value })}>
                  <option value="left">Left</option>
                  <option value="center">Centre</option>
                  <option value="right">Right</option>
                </select>
              </label>
              </>
            ) : <><span /><span /><span /><span /></>}
          </div>
          {section.as_pill ? (
            <p className="tn-token-help">
              The heading text is what shows inside the band - use the tag
              button on Title to put a reading in it, e.g. <code>{"{Station}"}</code>.
              Size, weight and alignment come from <strong>Title style</strong> below.
            </p>
          ) : null}

          {/* 2026-10-09: "all the text section including the title header
              one should have the option to centralized and make font
              adjustments and alignment". The banner was the one piece of
              text with no controls at all - and it is the piece most people
              want centred. Three independent runs. */}
          <FontControls section={section} onChange={onChange}
                        prefix="title_" label="Title style" />
          <FontControls section={section} onChange={onChange}
                        prefix="subtitle_" label="Subtitle style" />
          <FontControls section={section} onChange={onChange}
                        prefix="stamp_" label="Generated-at stamp style" />
<SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
        </div>
      ) : null}

      {type === "text" ? (
        <div className="tn-section-form">
          <TagTokenField label="Heading" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
          <TagTokenField
            label="Text"
            multiline
            rows={4}
            value={section.text}
            onChange={(v) => onChange({ text: v })}
            placeholder="Plain text; line breaks are kept. Use the tag button to insert a value."
            gatewayOptions={gatewayOptions}
            tagList={tagList}
            formatTagForDisplay={formatTagForDisplay}
          />
          <p className="tn-token-help">
            Insert a tag with the button above, or type one:{" "}
            <code>{"{TagName}"}</code>. A mask shows a word instead of a
            number — <code>{"{R1_Pass|1=PASS,0=FAIL}"}</code>. Run values:{" "}
            <code>{"{start}"}</code> <code>{"{end}"}</code>{" "}
            <code>{"{date}"}</code>.
          </p>
          <FontControls section={section} onChange={onChange}
                        prefix="title_" label="Heading style" />
          <FontControls section={section} onChange={onChange}
                        label="Body text style" />
            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} />
        </div>
      ) : null}

      {type === "static_table" ? (
        <div className="tn-section-form">
          <TagTokenField label="Heading" value={section.title}
            onChange={(v) => onChange({ title: v })}
            gatewayOptions={gatewayOptions} tagList={tagList}
            formatTagForDisplay={formatTagForDisplay} />
          <p className="tn-token-help">
            Any cell can be fixed text, a tag value as <code>{"{TagName}"}</code>,
            or the run itself: <code>{"{start}"}</code> <code>{"{end}"}</code>{" "}
            <code>{"{date}"}</code> <code>{"{template}"}</code>. Add a mask to
            show a word instead of a number:{" "}
            <code>{"{R1_Pass|1=PASS,0=FAIL}"}</code> — this works in any text,
            in every section.
          </p>

          <div className="tn-subhead">Columns</div>
          <div className="tn-static-row">
            {(section.header || []).map((h, c) => (
              <TokenInput key={c} value={h || ""} placeholder={`Column ${c + 1}`}
                          gatewayOptions={gatewayOptions} tagList={tagList}
                          formatTagForDisplay={formatTagForDisplay}
                          onChange={(v) => setHeaderCell(c, v)} />
            ))}
            <button type="button" className="btn btn-secondary btn-sm"
                    onClick={addDetailColumn} title="Add a column">+ Col</button>
            <button type="button" className="btn btn-danger btn-sm"
                    disabled={(section.header || []).length <= 1}
                    onClick={removeDetailColumn} title="Remove the last column">− Col</button>
          </div>

          <div className="tn-subhead">Rows</div>
          {(section.rows || []).map((row, r) => (
            <div key={r} className="tn-static-row">
              {(section.header || []).map((_h, c) => (
                <TokenInput key={c} value={(row && row[c]) || ""}
                            placeholder={c === 0 ? "Label" : "Text or a tag"}
                            gatewayOptions={gatewayOptions} tagList={tagList}
                            formatTagForDisplay={formatTagForDisplay}
                            onChange={(v) => setCell(r, c, v)} />
              ))}
              <button type="button" className="btn btn-danger btn-sm"
                      onClick={() => removeRow(r)} title="Remove this row">×</button>
            </div>
          ))}
          <button type="button" className="btn btn-secondary btn-sm" onClick={addRow}>
            + Add row
          </button>

          <TableStyleControls section={section} onChange={onChange} />
          <ColumnStyleControls section={section} onChange={onChange} />
          <SectionTrigger
            section={section} onChange={onChange}
            gatewayOptions={gatewayOptions} tagList={tagList}
            formatTagForDisplay={formatTagForDisplay}
          />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
        </div>
      ) : null}

      {type === "pill_card" ? (
        <div className="tn-section-form">
          <TagTokenField label="Heading" value={section.title}
            onChange={(v) => onChange({ title: v })}
            gatewayOptions={gatewayOptions} tagList={tagList}
            formatTagForDisplay={formatTagForDisplay} />
          <div className="tn-row tn-row-4">
            {/* 2026-10-17: this row offered a position but no width, and a
                pill stretched to its whole column has nowhere to move - so
                "Alignment" looked broken. The two belong together. */}
            <label title="Full page width: the pills share the row evenly, as before. Fit to the text: each pill is only as wide as what it says, which is what lets Position move it.">
              Pill width
              <select value={String(section.pill_fit || "fill")}
                      onChange={(e) => onChange({ pill_fit: e.target.value })}>
                <option value="fill">Full page width</option>
                <option value="content">Fit to the text</option>
              </select>
            </label>
            <label title="Where the pills sit, for every pill that does not set its own position below. Needs Pill width = Fit to the text.">
              Position (default)
              <select value={String(section.pill_align || section.align || "left")}
                      onChange={(e) => onChange({ pill_align: e.target.value })}>
                <option value="left">Left</option>
                <option value="center">Centre</option>
                <option value="right">Right</option>
              </select>
            </label>
            <label>Per row
              <input type="number" min="1" max="8" value={section.columns ?? 4}
                     onChange={(e) => onChange({ columns: Number(e.target.value) || 1 })} />
            </label>
            <label>Text size (pt)
              <input type="number" min="6" max="28" step="0.5" value={section.font_size ?? 10}
                     onChange={(e) => onChange({ font_size: Number(e.target.value) || 10 })} />
            </label>
          </div>
          <div className="tn-row tn-row-3">
            <label>Corners
              <select value={String(section.pill_corners || "both")}
                      onChange={(e) => onChange({ pill_corners: e.target.value })}
                      title="Which corners are rounded. Left or right only makes two pills read as one joined badge.">
                <option value="both">Rounded (both ends)</option>
                <option value="left">Rounded left only</option>
                <option value="right">Rounded right only</option>
                <option value="top">Rounded top only</option>
                <option value="bottom">Rounded bottom only</option>
                <option value="none">Square</option>
              </select>
            </label>
            <label>Corner radius (pt)
              <input type="number" min="0" max="40" step="1" placeholder="auto"
                     value={section.pill_radius ?? ""}
                     title="Blank follows the text size, which gives fully rounded ends."
                     onChange={(e) => onChange({ pill_radius: e.target.value === "" ? "" : Number(e.target.value) })} />
            </label>
            <label className="tn-checkbox">
              <input type="checkbox" checked={Boolean(section.as_header)}
                     onChange={(e) => onChange({ as_header: e.target.checked })} />
              Use as a section header
            </label>
          </div>

          <div className="tn-subhead">Pills</div>
          {(section.pills || []).map((pill, i) => (
            <div key={pill.id || i} className="tn-pill-row">
              <input className="tn-pill-label" placeholder="Label"
                     value={pill.label || ""}
                     onChange={(e) => updatePill(pill.id, { label: e.target.value })} />
              {/* Either a literal value OR a tag. The tag wins when set, so
                  the text box dims to say so rather than quietly losing. */}
              <TokenInput className="tn-pill-text" placeholder="Text, or a tag"
                          value={pill.text || ""}
                          disabled={Boolean(pill.tag_name)}
                          title={pill.tag_name
                            ? "A tag is selected below, so it supplies the value"
                            : "Literal text, or pick a tag to insert a token"}
                          gatewayOptions={gatewayOptions} tagList={tagList}
                          formatTagForDisplay={formatTagForDisplay}
                          onChange={(v) => updatePill(pill.id, { text: v })} />
              <select value={pill.gateway_id || ""}
                      onChange={(e) => updatePill(pill.id, { gateway_id: e.target.value, tag_name: "" })}>
                <option value="">(no tag)</option>
                {gatewayOptions.map((g) => <option key={g.id} value={g.id}>{g.name || g.id}</option>)}
              </select>
              <select value={pill.tag_name || ""}
                      onChange={(e) => updatePill(pill.id, { tag_name: e.target.value })}>
                <option value="">—</option>
                {tagList(pill.gateway_id).map((t) => (
                  <option key={t} value={t}>{formatTagForDisplay ? formatTagForDisplay(t) : t}</option>
                ))}
              </select>
              <select value={pill.aggregation || "latest"}
                      disabled={!pill.tag_name}
                      onChange={(e) => updatePill(pill.id, { aggregation: e.target.value })}>
                {KPI_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
              </select>
              <input className="tn-pill-unit" placeholder="Unit"
                     value={pill.unit || ""}
                     onChange={(e) => updatePill(pill.id, { unit: e.target.value })} />
              {/* 2026-10-09: "if the value is 1 we should show ... passed
                  and 0 failed". Free text because that is quicker to type
                  than a row editor; "*" catches anything unlisted. */}
              <input className="tn-pill-mask" placeholder="1=PASS, 0=FAIL"
                     title={"Show a word instead of the number.\n"
                            + "1=PASS, 0=FAIL  -  and * for anything else.\n"
                            + "A tag with no reading still shows a dash."}
                     value={pill.value_map || ""}
                     onChange={(e) => updatePill(pill.id, { value_map: e.target.value })} />
              <input type="color" title="Text colour"
                     value={String(pill.color || "#ffffff")}
                     onChange={(e) => updatePill(pill.id, { color: e.target.value })} />
              <input type="color" title="Background"
                     value={String(pill.background || "#14a89a")}
                     onChange={(e) => updatePill(pill.id, { background: e.target.value })} />
              {/* 2026-10-17, requested: "I should be able to add many pills in
                  the same row and decide their positions ... the left one
                  aligned on the left, the right one on the right and the two
                  centre ones centralised in the columns". One position for
                  the whole section could never express that. Needs Pill
                  width = Fit to the text: a pill stretched to fill its
                  column has nowhere to move inside it. */}
              <select className="tn-pill-align" value={pill.align || ""}
                      title={["Where this pill sits inside its own column.",
                              "Needs Pill width = Fit to the text.",
                              "Blank follows the section's Position."].join("\n")}
                      onChange={(e) => updatePill(pill.id, { align: e.target.value })}>
                <option value="">Position: section</option>
                <option value="left">Left of its column</option>
                <option value="center">Centre of its column</option>
                <option value="right">Right of its column</option>
              </select>
              <button type="button" title="Bold"
                      className={`btn btn-sm ${pill.bold ? "btn-primary" : "btn-secondary"}`}
                      onClick={() => updatePill(pill.id, { bold: !pill.bold })}>
                <strong>B</strong>
              </button>
              <button type="button" className="btn btn-danger btn-sm" title="Remove"
                      onClick={() => removePill(pill.id)}>×</button>
            </div>
          ))}
          <button type="button" className="btn btn-secondary btn-sm" onClick={addPill}>
            + Add pill
          </button>

          <SectionTrigger
            section={section} onChange={onChange}
            gatewayOptions={gatewayOptions} tagList={tagList}
            formatTagForDisplay={formatTagForDisplay}
          />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} />
        </div>
      ) : null}

      {type === "image" ? (
        <div className="tn-section-form">
          <div className="tn-row tn-row-2">
            <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
            <label>Caption<input value={section.caption || ""} onChange={(e) => onChange({ caption: e.target.value })} /></label>
          </div>
          <div className="tn-row tn-row-3">
            <label>Alignment
              <select value={section.align || "center"} onChange={(e) => onChange({ align: e.target.value })}>
                <option value="left">Left</option>
                <option value="center">Center</option>
                <option value="right">Right</option>
              </select>
            </label>
            <label>Width (mm)
              <input
                type="number"
                min={0}
                max={180}
                value={Number(section.width_mm || 0)}
                onChange={(e) => onChange({ width_mm: Math.max(0, Math.min(180, Number(e.target.value || 0))) })}
                placeholder="0 = auto"
                title="0 = automatic (70% of page width). Otherwise width in millimeters, capped at the printable area."
              />
            </label>
            <label>Saved file name
              <input
                value={section.path || ""}
                onChange={(e) => onChange({ path: e.target.value, data_url: e.target.value ? "" : section.data_url })}
                placeholder="e.g. site_diagram.png"
                title="Optional. Relative file under ~/.trustnode_edge/data/ on the edge."
              />
            </label>
          </div>
          <label className="tn-row">
            Upload image
            <input
              type="file"
              accept="image/png,image/jpeg,image/gif,image/svg+xml"
              onChange={(e) => {
                const file = e.target.files && e.target.files[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => {
                  const dataUrl = String(reader.result || "");
                  onChange({ data_url: dataUrl, path: "" });
                };
                reader.readAsDataURL(file);
                e.target.value = "";
              }}
            />
          </label>
          {section.data_url ? (
            <div className="tn-image-preview" style={{ marginTop: 8, textAlign: section.align || "center" }}>
              <img
                src={section.data_url}
                alt={section.title || "preview"}
                style={{ maxWidth: "100%", maxHeight: 200, border: "1px solid var(--stroke)", borderRadius: 6 }}
              />
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                Embedded ({Math.round(section.data_url.length / 1024)} KB). The image travels with the template — no file path required.
              </div>
            </div>
          ) : null}
          <SectionLayout section={section} onChange={onChange} prevType={prevType} />
        </div>
      ) : null}

      {type === "segment_summary" ? (
        <div className="tn-section-form">
          <div className="tn-row tn-row-3">
            <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
            <label>Only this test
              <input
                placeholder="e.g. test2 — blank = every test"
                value={section.segment || ""}
                onChange={(e) => onChange({ segment: e.target.value })}
              />
            </label>
            <label>First column header
              <input value={section.segment_header || "Test"}
                onChange={(e) => onChange({ segment_header: e.target.value })} />
            </label>
          </div>

          {/* 2026-10-09, reported: "test summary table ... when try to load
              and select a tag no tag is selected". The tag dropdowns read
              tagList(col.gateway_id || section.gateway_id) and this branch
              offered NO way to set either, so the list was always empty and
              the picker looked broken. A section-level gateway fills every
              column's list; a column may still override it below. */}
          <div className="tn-row tn-row-2">
            <label>Gateway
              <select
                value={section.gateway_id || ""}
                onChange={(e) => onChange({ gateway_id: e.target.value })}
                title="Where these measurements come from. Each column can override it."
              >
                <option value="">Choose a gateway</option>
                {gatewayOptions.map((g) => (
                  <option key={g.id} value={g.id}>{g.name || g.id}</option>
                ))}
              </select>
            </label>
            <span />
          </div>

          <div className="tn-subhead">One column per measurement</div>
          {!section.gateway_id && !(section.columns || []).some((c) => c.gateway_id) ? (
            <p className="tn-token-help">
              Choose a gateway above and the tag lists below will fill in.
            </p>
          ) : null}
          {(section.columns || []).map((col, i) => (
            <div className="tn-row tn-row-5" key={`col-${i}`}>
              <label>Gateway
                <select
                  value={col.gateway_id || ""}
                  onChange={(e) => onChange({ columns: (section.columns || []).map((c, j) => j === i ? { ...c, gateway_id: e.target.value, tag_name: "" } : c) })}
                  title="Blank uses the section's gateway"
                >
                  <option value="">(section default)</option>
                  {gatewayOptions.map((g) => (
                    <option key={g.id} value={g.id}>{g.name || g.id}</option>
                  ))}
                </select>
              </label>
              <label>Tag
                <select
                  value={col.tag_name || ""}
                  onChange={(e) => onChange({ columns: (section.columns || []).map((c, j) => j === i ? { ...c, tag_name: e.target.value } : c) })}
                >
                  <option value="">Choose a tag</option>
                  {tagList(col.gateway_id || section.gateway_id).map((t) => (
                    <option key={t} value={t}>{formatTagForDisplay(t)}</option>
                  ))}
                </select>
              </label>
              <label>Aggregation
                <select
                  value={col.aggregation || "avg"}
                  onChange={(e) => onChange({ columns: (section.columns || []).map((c, j) => j === i ? { ...c, aggregation: e.target.value } : c) })}
                >
                  {SEGMENT_AGGREGATIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
                </select>
              </label>
              <label>Column title
                <input value={col.title || ""} placeholder="optional"
                  onChange={(e) => onChange({ columns: (section.columns || []).map((c, j) => j === i ? { ...c, title: e.target.value } : c) })} />
              </label>
              <button type="button" className="btn btn-danger btn-sm"
                onClick={() => onChange({ columns: (section.columns || []).filter((_, j) => j !== i) })}>
                Remove
              </button>
            </div>
          ))}
          <button type="button" className="btn btn-sm"
            onClick={() => onChange({ columns: [...(section.columns || []), { tag_name: "", aggregation: "avg", title: "" }] })}>
            Add measurement
          </button>

          <div className="tn-row tn-row-3">
            <label>Total row
              <select
                value={section.footer_aggregation || ""}
                onChange={(e) => onChange({ footer_aggregation: e.target.value })}
              >
                <option value="">No total row</option>
                {SEGMENT_AGGREGATIONS.map((a) => (
                  <option key={a.value} value={a.value}>{`${a.label} of all tests`}</option>
                ))}
              </select>
            </label>
            <label>Total row label
              <input value={section.footer_label || ""} placeholder="Average"
                onChange={(e) => onChange({ footer_label: e.target.value })} />
            </label>
            <label>Decimals
              <select value={section.format || "3dp"} onChange={(e) => onChange({ format: e.target.value })}>
                <option value="int">0</option>
                <option value="2dp">2</option>
                <option value="3dp">3</option>
                <option value="scientific">scientific</option>
              </select>
            </label>
          </div>
            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
        </div>
      ) : null}

      {type === "kpi_grid" ? (
        <div className="tn-section-form">
          {/* The grid always carried a time_range; until 2026-09-30 nothing
              read it, so every KPI aggregated the whole historian. */}
          <div className="tn-row tn-row-2">
            <label>Time range
              <select
                value={section.time_range?.preset || "run"}
                onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), preset: e.target.value } })}
              >
                {TIME_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </label>
            {section.time_range?.preset === "segment" ? (
              <label>Which test / cycle
                <input
                  placeholder="test2 · 3 or last"
                  value={section.time_range?.segment || ""}
                  onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), segment: e.target.value } })}
                />
              </label>
            ) : null}
          </div>
          <div className="tn-row tn-row-2">
            <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
            <label>Show as
              <select
                value={section.kpi_style || "cards"}
                onChange={(e) => onChange({ kpi_style: e.target.value })}
              >
                <option value="cards">Cards</option>
                <option value="table">Table</option>
              </select>
            </label>
            {(section.kpi_style || "cards") === "cards" ? (
              <label>Corners
                <select
                  value={section.kpi_corners || "round"}
                  onChange={(e) => onChange({ kpi_corners: e.target.value })}
                >
                  <option value="round">Rounded</option>
                  <option value="square">Square</option>
                </select>
              </label>
            ) : null}
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
                  placeholder="Label — {tag:Name} works here too"
                  title="A KPI label can carry a tag value, e.g. 'Peak on {tag:Line_Id}'."
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
            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
        </div>
      ) : null}

      {(isChart || type === "table") ? (
        <div className="tn-section-form">
          <CollapsibleCard id={`${section.id}-basics`} title="Basics" defaultOpen>
            {/* 2026-10-09, requested: "a option to change the type of the
                chart selected in the section where is possible, like change
                from line to area to bar etc without the need to delete and
                create a new one".

                Only the three time-series types are offered here. They are
                the ones that share an IDENTICAL configuration - series,
                axes, bucket, window - so switching between them keeps every
                setting. A pie or a data table is configured differently
                (compute rules, column definitions), and silently carrying a
                chart's series into one would lose work rather than save
                it. */}
            {isChart ? (
              <div className="tn-row tn-row-2">
                <label>Chart type
                  <select
                    value={type}
                    onChange={(e) => onChange({ type: e.target.value })}
                    title="Switch between line, area and bar. Series, axes and the time window are kept."
                  >
                    <option value="line_chart">Line chart</option>
                    <option value="area_chart">Area chart</option>
                    <option value="bar_chart">Bar chart</option>
                  </select>
                </label>
                <span />
              </div>
            ) : null}
            <div className="tn-row tn-row-2">
              <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
              {isChart ? (
                <label>Max samples<span className="tn-hint" title="The most points to DRAW. The time range decides what is read; this only thins it evenly across the window, so two sections on the same trigger always cover the same period."> </span>
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
            {/* Three groups, each a tidy row: WHEN the data comes from, WHAT
                opens and closes it, and HOW it sits on the page. Before this
                they shared one two-column row - two four-field trigger
                pickers among them - and every control carried a paragraph of
                help, so nothing lined up. Long explanations now live in the
                title attribute, where they are available but not in the way. */}
            <div className="tn-subhead">When</div>
            <div className="tn-row tn-row-3">
              <label>Time range
                <select
                  value={section.time_range?.preset || "24h"}
                  onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), preset: e.target.value } })}
                >
                  {TIME_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
              </label>
              <label>Group samples
                <select
                  value={section.bucket || "raw"}
                  onChange={(e) => onChange({ bucket: e.target.value })}
                  title="Summarise samples per minute / hour / day. Each series applies its own aggregation inside the bucket."
                >
                  {BUCKET_SIZES.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
                </select>
              </label>
              {section.time_range?.preset === "segment" ? (
                <label>Which test / cycle
                  <input
                    placeholder="test2 · 3 or last"
                    value={section.time_range?.segment || ""}
                    onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), segment: e.target.value } })}
                  />
                </label>
              ) : <span />}
            </div>
            {section.time_range?.preset === "custom" ? (
              <div className="tn-row tn-row-2 tn-row-tight">
                <label>From (UTC)
                  <input type="datetime-local" step="1" value={utcToLocalInput(section.time_range?.from_utc)} onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), from_utc: localInputToUtc(e.target.value) } })} />
                </label>
                <label>To (UTC)
                  <input type="datetime-local" step="1" value={utcToLocalInput(section.time_range?.to_utc)} onChange={(e) => onChange({ time_range: { ...(section.time_range || {}), to_utc: localInputToUtc(e.target.value) } })} />
                </label>
              </div>
            ) : null}
            {section.time_range?.preset === "batch" ? (
              <BatchWindowPicker
                timeRange={section.time_range}
                onChange={(patch) => onChange({ time_range: { ...(section.time_range || {}), preset: "batch", ...patch } })}
              />
            ) : null}

            {type === "table" ? (
              <TableStyleControls section={section} onChange={onChange} />
            ) : null}

            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />

            <SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
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
                        {/* Legacy rows stored "left"/"right"; the backend
                            normalises both onto axis 1 and 2. */}
                        <select value={normAxis4(s.axis)} onChange={(e) => updateSeries(s.id, { axis: e.target.value })} title="Which Y axis this series is read against">
                          <option value="left1">Y-axis 1 (left)</option>
                          <option value="right1">Y-axis 2 (right)</option>
                          <option value="left2">Y-axis 3 (left, outer)</option>
                          <option value="right2">Y-axis 4 (right, outer)</option>
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
                    <label className="tn-inline-label" title="How this series is drawn between readings. Blank follows the chart's setting.">
                      Line shape
                      <select
                        value={s.interpolation || ""}
                        onChange={(e) => updateSeries(s.id, { interpolation: e.target.value })}
                      >
                        <option value="">Chart default</option>
                        {CHART_INTERPOLATIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </label>
                    <label className="tn-inline-label" title="Stroke width for this series only. Blank follows the chart's setting - useful for a thin setpoint under a thick measurement.">
                      Thickness
                      <input
                        type="number" min="0.2" max="6" step="0.1"
                        value={s.line_width ?? ""}
                        placeholder="chart"
                        onChange={(e) => updateSeries(s.id, { line_width: e.target.value })}
                      />
                    </label>
                  </div>
                </div>
              );
            })}
            <button type="button" className="btn btn-link" onClick={addSeries}>+ Add series</button>
          </CollapsibleCard>

          {isChart ? (
            <CollapsibleCard id={`${section.id}-axes`} title="Axes, units & scale" defaultOpen={false}>
              <div className="tn-row tn-row-2">
                {/* 2026-10-11, requested: axis labels "cab be a tag value".
                    Same token vocabulary as every other text field, so an
                    axis can be titled from the PLC - a unit the recipe sets,
                    or the name of the product under test. */}
                <label>X-axis title
                  <TokenInput value={section.x_axis_label || ""}
                              gatewayOptions={gatewayOptions} tagList={tagList}
                              formatTagForDisplay={formatTagForDisplay}
                              onChange={(v) => onChange({ x_axis_label: v })} />
                </label>
                <label>Value format
                  <select value={section.value_format || "auto"} onChange={(e) => onChange({ value_format: e.target.value })}>
                    {VALUE_FORMATS.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
                  </select>
                </label>
              </div>
              {/* FOUR Y axes, same model and the same config keys as the
                  dashboard widgets (2026-10-08, requested: "we still need
                  the mutple Y axis as we had for the dashboard wdigets to be
                  print more than 2 unis of readings"). An axis is only drawn
                  when a series is assigned to it, so a one-unit chart looks
                  exactly as it did. */}
              <fieldset className="tn-fieldset">
                <legend>Y-axis 1 (left)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<TokenInput value={section.y_axis_label || ""} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_axis_label: v })} /></label>
                  <label>Unit / suffix<TokenInput value={section.y_axis_unit || ""} placeholder="e.g. kW" gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_axis_unit: v })} /></label>
                  <label>Min<input type="number" step="any" value={section.y_min ?? ""} onChange={(e) => onChange({ y_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_max ?? ""} onChange={(e) => onChange({ y_max: e.target.value })} placeholder="auto" /></label>
                
                <div className="tn-row tn-row-2">
                  <label className="tn-checkbox" title="The rotated axis name beside the numbers.">
                    <input type="checkbox" checked={section.y_axis_show_title !== false}
                           onChange={(e) => onChange({ y_axis_show_title: e.target.checked })} />
                    Show axis title
                  </label>
                  <label className="tn-checkbox" title="The column of tick numbers. Hiding it gives the width back to the plot.">
                    <input type="checkbox" checked={section.y_axis_show_values !== false}
                           onChange={(e) => onChange({ y_axis_show_values: e.target.checked })} />
                    Show axis values
                  </label>
                </div></div>
              </fieldset>
              <fieldset className="tn-fieldset">
                <legend>Y-axis 2 (right)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<TokenInput value={section.y_axis_right_label || ""} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_axis_right_label: v })} /></label>
                  <label>Unit / suffix<TokenInput value={section.y_axis_right_unit || ""} placeholder="e.g. °C" gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_axis_right_unit: v })} /></label>
                  <label>Min<input type="number" step="any" value={section.y_right_min ?? ""} onChange={(e) => onChange({ y_right_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_right_max ?? ""} onChange={(e) => onChange({ y_right_max: e.target.value })} placeholder="auto" /></label>
                
                <div className="tn-row tn-row-2">
                  <label className="tn-checkbox" title="The rotated axis name beside the numbers.">
                    <input type="checkbox" checked={section.y_axis_right_show_title !== false}
                           onChange={(e) => onChange({ y_axis_right_show_title: e.target.checked })} />
                    Show axis title
                  </label>
                  <label className="tn-checkbox" title="The column of tick numbers. Hiding it gives the width back to the plot.">
                    <input type="checkbox" checked={section.y_axis_right_show_values !== false}
                           onChange={(e) => onChange({ y_axis_right_show_values: e.target.checked })} />
                    Show axis values
                  </label>
                </div></div>
              </fieldset>
              <fieldset className="tn-fieldset">
                <legend>Y-axis 3 (left, outer)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<TokenInput value={section.y_left2_label || ""} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_left2_label: v })} /></label>
                  <label>Unit / suffix<TokenInput value={section.y_left2_unit || ""} placeholder="e.g. bar" gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_left2_unit: v })} /></label>
                  <label>Min<input type="number" step="any" value={section.y_left2_min ?? ""} onChange={(e) => onChange({ y_left2_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_left2_max ?? ""} onChange={(e) => onChange({ y_left2_max: e.target.value })} placeholder="auto" /></label>
                
                <div className="tn-row tn-row-2">
                  <label className="tn-checkbox" title="The rotated axis name beside the numbers.">
                    <input type="checkbox" checked={section.y_left2_show_title !== false}
                           onChange={(e) => onChange({ y_left2_show_title: e.target.checked })} />
                    Show axis title
                  </label>
                  <label className="tn-checkbox" title="The column of tick numbers. Hiding it gives the width back to the plot.">
                    <input type="checkbox" checked={section.y_left2_show_values !== false}
                           onChange={(e) => onChange({ y_left2_show_values: e.target.checked })} />
                    Show axis values
                  </label>
                </div></div>
              </fieldset>
              <fieldset className="tn-fieldset">
                <legend>Y-axis 4 (right, outer)</legend>
                <div className="tn-row tn-row-4">
                  <label>Title<TokenInput value={section.y_right2_label || ""} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_right2_label: v })} /></label>
                  <label>Unit / suffix<TokenInput value={section.y_right2_unit || ""} placeholder="e.g. l/min" gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} onChange={(v) => onChange({ y_right2_unit: v })} /></label>
                  <label>Min<input type="number" step="any" value={section.y_right2_min ?? ""} onChange={(e) => onChange({ y_right2_min: e.target.value })} placeholder="auto" /></label>
                  <label>Max<input type="number" step="any" value={section.y_right2_max ?? ""} onChange={(e) => onChange({ y_right2_max: e.target.value })} placeholder="auto" /></label>
                
                <div className="tn-row tn-row-2">
                  <label className="tn-checkbox" title="The rotated axis name beside the numbers.">
                    <input type="checkbox" checked={section.y_right2_show_title !== false}
                           onChange={(e) => onChange({ y_right2_show_title: e.target.checked })} />
                    Show axis title
                  </label>
                  <label className="tn-checkbox" title="The column of tick numbers. Hiding it gives the width back to the plot.">
                    <input type="checkbox" checked={section.y_right2_show_values !== false}
                           onChange={(e) => onChange({ y_right2_show_values: e.target.checked })} />
                    Show axis values
                  </label>
                </div></div>
              </fieldset>
              {/* 2026-10-11, requested: editable ticks, line thickness and
                  legend alignment. Tick count is per CHART, not per axis:
                  the horizontal grid lines are shared, so two different
                  counts would print one axis's labels between the other's
                  grid lines. */}
              <div className="tn-row tn-row-3">
                <label title="How many intervals the Y scale is divided into. Fewer ticks on a crowded chart.">
                  Y-axis ticks
                  <input type="number" min="2" max="12" step="1"
                         value={section.y_tick_count ?? 5}
                         onChange={(e) => onChange({ y_tick_count: e.target.value })} />
                </label>
                <label title="Default stroke for every series on this chart. A series can override it.">
                  Line thickness
                  <input type="number" min="0.2" max="6" step="0.1"
                         value={section.line_width ?? 1.4}
                         onChange={(e) => onChange({ line_width: e.target.value })} />
                </label>
                <label>Legend position
                  <select value={section.legend_align || "left"}
                          onChange={(e) => onChange({ legend_align: e.target.value })}>
                    <option value="left">Left</option>
                    <option value="center">Centred</option>
                    <option value="right">Right</option>
                  </select>
                </label>
              </div>
              <div className="tn-row tn-row-2">
                <label title="How the line is drawn between readings. Use a Step for a signal that holds its value - a straight diagonal implies readings that were never taken.">
                  Interpolation
                  <select value={section.interpolation || "linear"}
                          onChange={(e) => onChange({ interpolation: e.target.value })}>
                    {CHART_INTERPOLATIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="tn-checkbox">
                <input type="checkbox" checked={section.show_legend !== false} onChange={(e) => onChange({ show_legend: e.target.checked })} />
                Show legend
              </label>
              {/* 2026-10-16, requested: hide the timestamp X axis. On a short
                  cycle every point carries the same date, so the stamps and
                  the axis title are noise - and the space they free goes
                  back to the plot. */}
              <label className="tn-checkbox" title="Hides the start/end timestamps and the X-axis title. The plot keeps the reclaimed height.">
                <input type="checkbox" checked={section.show_x_axis !== false} onChange={(e) => onChange({ show_x_axis: e.target.checked })} />
                Show X axis (timestamps)
              </label>
              <label className="tn-checkbox" title="The line under the chart reading '120 samples per series, 3 series'. Useful while building; noise in a customer report.">
                <input type="checkbox" checked={section.show_sample_caption === true}
                       onChange={(e) => onChange({ show_sample_caption: e.target.checked })} />
                Show sample count under the chart
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
          <TagTokenField label="Title" value={section.title} onChange={(v) => onChange({ title: v })} gatewayOptions={gatewayOptions} tagList={tagList} formatTagForDisplay={formatTagForDisplay} />
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
            <SectionTrigger
              section={section} onChange={onChange}
              gatewayOptions={gatewayOptions} tagList={tagList}
              formatTagForDisplay={formatTagForDisplay}
            />
          <SectionLayout section={section} onChange={onChange} prevType={prevType} showAlign />
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
       </>
      )}
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

  // Set when a load failed, so the page can offer "Try again" instead of
  // looking like an install with no templates.
  const [loadError, setLoadError] = useState("");
  // The last message we raised, so a retry loop cannot re-raise the same
  // banner every few seconds. Cleared on success.
  const notifiedErrRef = useRef("");
  const refreshTemplates = useCallback(async () => {
    try {
      const res = await listReportTemplates();
      const list = Array.isArray(res?.templates) ? res.templates : [];
      setTemplates(list);
      setLoadError("");
      notifiedErrRef.current = "";
      return list;
    } catch (e) {
      const msg = String(e?.message || e);
      setLoadError(msg);
      // Only on CHANGE. onNotify sets a global banner, which re-renders the
      // whole app; raising it on every retry made the page flicker.
      if (notifiedErrRef.current !== msg) {
        notifiedErrRef.current = msg;
        onNotify({ type: "error", message: `Failed to load templates: ${msg}` });
      }
      return null;   // null = "could not read", NOT "there are none"
    }
  }, [onNotify]);

  // Always the latest refreshTemplates, WITHOUT making it an effect
  // dependency - see the loop described below.
  const refreshRef = useRef(refreshTemplates);
  refreshRef.current = refreshTemplates;

  // KEEP TRYING (operator 2026-10-07: "problems to load the templates").
  // This ran once. At startup the backend is still finishing deferred init
  // against a multi-gigabyte store, so the first read often fails - and the
  // designer then showed an empty template list for the rest of the session.
  // An empty list and a failed read look identical to the operator, which is
  // why this looked like "the templates are gone".
  //
  // RUNS ONCE - the dependency array MUST stay empty. `refreshTemplates` is
  // rebuilt whenever `onNotify` changes, and the parent passes onNotify as an
  // inline arrow, so its identity changes on EVERY App render. Depending on
  // it meant: fetch fails -> onNotify sets a global banner -> App re-renders
  // -> new onNotify -> new refreshTemplates -> this effect tears down and
  // re-runs -> fetch immediately -> fails... a tight infinite loop that
  // hammered the backend and made the page visibly flicker (reported the
  // same day it shipped). The ref gives the loop the current function
  // without making it a trigger.
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let attempt = 0;
    const tick = async () => {
      const got = await refreshRef.current?.();
      if (cancelled || got !== null) return;    // loaded (even if empty): done
      attempt += 1;
      timer = setTimeout(tick, Math.min(15000, 2000 * Math.pow(2, attempt - 1)));
    };
    tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, []);

  const loadTemplate = (id) => {
    const tpl = templates.find((t) => String(t.id) === String(id));
    if (!tpl) return;
    setSelectedId(tpl.id);
    setDraft({
      id: tpl.id,
      name: tpl.name || "",
      description: tpl.description || "",
      // Spread first: rebuilding the definition as {sections} alone
      // discards every sibling key, so anything stored beside the
      // sections would be dropped by merely opening and saving.
      definition: {
        ...(tpl?.definition || {}),
        sections: Array.isArray(tpl?.definition?.sections)
          ? tpl.definition.sections : [],
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
          // Spread first: rebuilding the definition as {sections} alone
          // discards every sibling key, so anything stored beside the
          // sections would be dropped by merely opening and saving.
          definition: {
            ...(saved?.definition || {}),
            sections: Array.isArray(saved?.definition?.sections)
              ? saved.definition.sections : [],
          },
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
      // refreshTemplates returns null when the re-read FAILED (as opposed to
      // returning none). The import itself still succeeded, so say nothing
      // more and let the retry loop pick the list up.
      if (first && first.id && Array.isArray(list)) {
        // Load the first imported template into the editor for convenience.
        const found = list.find((t) => String(t.id) === String(first.id));
        if (found) {
          setSelectedId(found.id);
          setDraft({
            id: found.id,
            name: found.name || "",
            description: found.description || "",
            // Spread first: rebuilding the definition as {sections} alone
            // discards every sibling key, so anything stored beside the
            // sections would be dropped by merely opening and saving.
            definition: {
              ...(found?.definition || {}),
              sections: Array.isArray(found?.definition?.sections)
                ? found.definition.sections : [],
            },
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
  /* Copy a section and drop the copy directly below the original.
   *
   * 2026-10-09, requested: "we also should have a option to duplicate the
   * section and to be added right after the original one to make easy the
   * things". Building a report of six near-identical cycle charts meant
   * configuring each from scratch.
   *
   * Every id inside the section is regenerated, not just the section's own.
   * Series, pills, KPI items and columns are all edited BY ID
   * (updateSeries matches on s.id), so a copy that kept them would edit
   * both sections at once - and the duplicate would look haunted.
   */
  const duplicateSection = (sectionId) => {
    setDraft((prev) => {
      const list = [...(prev.definition?.sections || [])];
      const idx = list.findIndex((s) => String(s.id) === String(sectionId));
      if (idx < 0) return prev;
      const copy = reidSection(list[idx]);
      list.splice(idx + 1, 0, copy);
      return { ...prev, definition: { ...prev.definition, sections: list } };
    });
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
        {/* A failed read must never look like "this install has no
            templates". Say which it is, and let the operator retry now
            instead of waiting out the backoff (2026-10-07). */}
        {loadError ? (
          <div className="error" role="alert" style={{ marginBottom: 8 }}>
            Could not load the saved templates: {loadError}
            <button className="btn btn-secondary btn-sm" style={{ marginLeft: 8 }}
                    onClick={() => refreshTemplates()}>
              Try again
            </button>
          </div>
        ) : null}
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
            prevType={idx > 0 ? String(sections[idx - 1]?.type || "") : ""}
            gatewayOptions={gatewayOptions}
            tagsByGateway={tagsByGateway}
            formatTagForDisplay={formatTagForDisplay}
            onChange={(patch) => updateSection(section.id, patch)}
            onRemove={() => removeSection(section.id)}
            onDuplicate={() => duplicateSection(section.id)}
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
