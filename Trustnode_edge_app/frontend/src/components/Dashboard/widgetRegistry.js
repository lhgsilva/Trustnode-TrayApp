// Operator 2026-06-16: bumped from 20×20 to 40×40 so widgets can be
// placed and sized on a finer grid — operator wanted to fit smaller
// cards. Pre-existing widget w/h/x/y are scaled in
// `migrateWidgetsToFinerGrid` at load so saved dashboards keep their
// visual layout. The DASHBOARD_GRID_VERSION marker travels with the
// widget payload so we only scale once.
export const DASHBOARD_GRID_COLS = 40;
export const DASHBOARD_GRID_ROWS = 40;
// 2026-07-27: the dashboard grows VERTICALLY (scrolling) like professional
// dashboard tools — placement is never forced into an overlap because the
// canvas "ran out of rows". DASHBOARD_GRID_ROWS remains the visible
// first-screen row count (cell height derives from it); VIRTUAL_ROWS is the
// hard bound for how far content may extend below the fold.
export const DASHBOARD_GRID_VIRTUAL_ROWS = 400;
export const DASHBOARD_GRID_VERSION = 2;

export const WIDGET_TYPES = [
  { key: "line_chart", label: "Line Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
  { key: "line_area_chart", label: "Line Area Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
  { key: "bar_chart", label: "Bar Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
  // 2026-07-26: SCADA strip-chart — each series in its own lane, shared
  // time axis + synchronized cursor (FactoryTalk "isolated graphing",
  // Ignition "subplots").
  { key: "stacked_trend", label: "Stacked Trend (Strip Chart)", group: "Charts", defaultSize: { w: 14, h: 12 } },
  { key: "pie_chart", label: "Pie Chart", group: "Charts", defaultSize: { w: 8, h: 8 } },
  { key: "meter_chart", label: "Meter Chart", group: "Charts", defaultSize: { w: 8, h: 6 } },
  { key: "text_kpi", label: "Text KPI (Tag)", group: "KPI", defaultSize: { w: 6, h: 4 } },
  { key: "value_kpi", label: "Value KPI (Tag)", group: "KPI", defaultSize: { w: 6, h: 4 } },
  { key: "fixed_text", label: "Fixed Text", group: "Content", defaultSize: { w: 8, h: 4 } },
  { key: "divider", label: "Divider", group: "Layout", defaultSize: { w: 40, h: 2 } },
  { key: "table_list", label: "Table List View", group: "Content", defaultSize: { w: 12, h: 8 } },
  { key: "image", label: "Pictures", group: "Media", defaultSize: { w: 8, h: 8 } },
  { key: "ip_camera", label: "IP Camera (Live Feed)", group: "Media", defaultSize: { w: 10, h: 8 } },
  { key: "cloud_sync_status", label: "Cloud Sync Status", group: "System", defaultSize: { w: 16, h: 8 } },
  // Report card: pick a saved template, see the last generated PDF for
  // it, and trigger an on-demand render straight from the dashboard.
  // Trigger / schedule configuration lives in Scheduled Reports — the
  // widget links there so we don't duplicate that UI.
  { key: "report_card", label: "Report Card", group: "Reports", defaultSize: { w: 20, h: 20 } },
  // Operator 2026-06-16: Energy Tariffs widget — aggregates kWh and
  // cost per configured electricity tariff over a window and renders
  // as a donut, bar chart or table. Powered by the per-tariff
  // insight tags emitted by power_manager.
  { key: "energy_tariffs", label: "Energy Tariffs", group: "Charts", defaultSize: { w: 14, h: 10 } },

  // ----- Batch Management module widgets (2026-06-23) -----
  // Each carries `licenseModule: "batch_management"` so the designer
  // hides them in the picker when the license is off, and existing
  // dashboards that use them render an "unlicensed" placeholder.
  { key: "batch_current",   label: "Current Batch",      group: "Batch", licenseModule: "batch_management", defaultSize: { w: 12, h: 6 } },
  { key: "batch_list",      label: "Recent Batches",     group: "Batch", licenseModule: "batch_management", defaultSize: { w: 14, h: 10 } },
  { key: "batch_kpi",       label: "Batch KPI",          group: "Batch", licenseModule: "batch_management", defaultSize: { w: 10, h: 6 } },
  { key: "batch_timeline",  label: "Batch Timeline",     group: "Batch", licenseModule: "batch_management", defaultSize: { w: 20, h: 6 } },
  { key: "batch_input",     label: "Batch ID Input",     group: "Batch", licenseModule: "batch_management", defaultSize: { w: 10, h: 4 } },

  // ----- OEE module widgets (2026-08-29) -----
  // Same contract as the Batch widgets: `licenseModule` hides them in the
  // picker when the licence is off and renders an "unlicensed" placeholder on
  // dashboards that already use them.
  //
  // Every one of these CONSUMES the OEE module's endpoints. None of them
  // computes availability, performance, quality or their product: two
  // implementations of the same formula will disagree, and the one on a
  // dashboard is the one nobody can trace back to a number.
  { key: "oee_kpi",               label: "OEE KPI",              group: "OEE", licenseModule: "oee", defaultSize: { w: 8,  h: 6 } },
  { key: "oee_availability_kpi",  label: "Availability KPI",     group: "OEE", licenseModule: "oee", defaultSize: { w: 8,  h: 6 } },
  { key: "oee_performance_kpi",   label: "Performance KPI",      group: "OEE", licenseModule: "oee", defaultSize: { w: 8,  h: 6 } },
  { key: "oee_quality_kpi",       label: "Quality KPI",          group: "OEE", licenseModule: "oee", defaultSize: { w: 8,  h: 6 } },
  { key: "oee_machine_card",      label: "Machine OEE Card",     group: "OEE", licenseModule: "oee", defaultSize: { w: 12, h: 12 } },
  { key: "oee_machine_status",    label: "Machine Status",       group: "OEE", licenseModule: "oee", defaultSize: { w: 10, h: 6 } },
  { key: "oee_trend",             label: "OEE Trend",            group: "OEE", licenseModule: "oee", defaultSize: { w: 16, h: 10 } },
  { key: "oee_apq_trend",         label: "A / P / Q Trend",      group: "OEE", licenseModule: "oee", defaultSize: { w: 16, h: 10 } },
  { key: "oee_downtime_pareto",   label: "Downtime Pareto",      group: "OEE", licenseModule: "oee", defaultSize: { w: 16, h: 10 } },
  { key: "oee_status_timeline",   label: "Machine Status Timeline", group: "OEE", licenseModule: "oee", defaultSize: { w: 24, h: 10 } },
  { key: "oee_runtime_downtime",  label: "Runtime vs Downtime",  group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },
  { key: "oee_energy_usage",      label: "OEE Energy Usage",     group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },
  { key: "oee_energy_waste",      label: "OEE Energy Waste",     group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },
  { key: "oee_production_count",  label: "Production Count",     group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },
  { key: "oee_shift_performance", label: "Shift Performance",    group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },
  { key: "oee_machine_comparison", label: "Machine Comparison",  group: "OEE", licenseModule: "oee", defaultSize: { w: 18, h: 10 } },
  { key: "oee_data_quality",      label: "OEE Readiness / Data Quality", group: "OEE", licenseModule: "oee", defaultSize: { w: 14, h: 9 } },

  // ----- I/O blocks (2026-08-29) -----
  // An ifm IO-Link master (or any gateway publishing Port<N>_Pin<2|4>) as a
  // single card: supply, block state, and every port's two pins. No licence
  // module - reading a field device is core, not an add-on.
  // Sized for the block face in full plus the first rows of the tag list.
  // The face must never be clipped - it is the at-a-glance view, and on this
  // block the only two pins that are ON are 7 and 8, at the very bottom of it.
  // The LIST is what scrolls.
  { key: "io_block_status", label: "I/O Block Status", group: "I/O", defaultSize: { w: 16, h: 28 } },
];

// One-shot migration: walk a widget list and double every w/h/x/y if
// the saved payload was authored on the 20×20 grid (legacy: no
// _grid_version, or _grid_version < 2). Idempotent.
export function migrateWidgetsToFinerGrid(widgets, savedVersion) {
  const version = Number(savedVersion || 0);
  if (version >= DASHBOARD_GRID_VERSION) return Array.isArray(widgets) ? widgets : [];
  if (!Array.isArray(widgets)) return [];
  return widgets.map((w) => ({
    ...w,
    w: Math.max(1, Math.round(Number(w?.w || 0) * 2)),
    h: Math.max(1, Math.round(Number(w?.h || 0) * 2)),
    x: Math.max(0, Math.round(Number(w?.x || 0) * 2)),
    y: Math.max(0, Math.round(Number(w?.y || 0) * 2)),
  }));
}

export function getWidgetMeta(type) {
  return WIDGET_TYPES.find((w) => w.key === type) || WIDGET_TYPES[0];
}

export function newWidgetId() {
  return `dw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
