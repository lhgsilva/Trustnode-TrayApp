// Operator 2026-06-16: bumped from 20×20 to 40×40 so widgets can be
// placed and sized on a finer grid — operator wanted to fit smaller
// cards. Pre-existing widget w/h/x/y are scaled in
// `migrateWidgetsToFinerGrid` at load so saved dashboards keep their
// visual layout. The DASHBOARD_GRID_VERSION marker travels with the
// widget payload so we only scale once.
export const DASHBOARD_GRID_COLS = 40;
export const DASHBOARD_GRID_ROWS = 40;
export const DASHBOARD_GRID_VERSION = 2;

export const WIDGET_TYPES = [
  { key: "line_chart", label: "Line Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
  { key: "line_area_chart", label: "Line Area Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
  { key: "bar_chart", label: "Bar Chart", group: "Charts", defaultSize: { w: 10, h: 8 } },
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
