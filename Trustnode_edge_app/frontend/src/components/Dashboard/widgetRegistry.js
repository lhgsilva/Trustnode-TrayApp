export const DASHBOARD_GRID_COLS = 20;
export const DASHBOARD_GRID_ROWS = 20;

export const WIDGET_TYPES = [
  { key: "line_chart", label: "Line Chart", group: "Charts", defaultSize: { w: 5, h: 4 } },
  { key: "line_area_chart", label: "Line Area Chart", group: "Charts", defaultSize: { w: 5, h: 4 } },
  { key: "bar_chart", label: "Bar Chart", group: "Charts", defaultSize: { w: 5, h: 4 } },
  { key: "pie_chart", label: "Pie Chart", group: "Charts", defaultSize: { w: 4, h: 4 } },
  { key: "meter_chart", label: "Meter Chart", group: "Charts", defaultSize: { w: 4, h: 3 } },
  { key: "text_kpi", label: "Text KPI (Tag)", group: "KPI", defaultSize: { w: 3, h: 2 } },
  { key: "value_kpi", label: "Value KPI (Tag)", group: "KPI", defaultSize: { w: 3, h: 2 } },
  { key: "fixed_text", label: "Fixed Text", group: "Content", defaultSize: { w: 4, h: 2 } },
  { key: "divider", label: "Divider", group: "Layout", defaultSize: { w: 20, h: 1 } },
  { key: "table_list", label: "Table List View", group: "Content", defaultSize: { w: 6, h: 4 } },
  { key: "image", label: "Pictures", group: "Media", defaultSize: { w: 4, h: 4 } },
  { key: "ip_camera", label: "IP Camera (Live Feed)", group: "Media", defaultSize: { w: 5, h: 4 } },
  { key: "cloud_sync_status", label: "Cloud Sync Status", group: "System", defaultSize: { w: 8, h: 4 } },
  // Report card: pick a saved template, see the last generated PDF for
  // it, and trigger an on-demand render straight from the dashboard.
  // Trigger / schedule configuration lives in Scheduled Reports — the
  // widget links there so we don't duplicate that UI.
  { key: "report_card", label: "Report Card", group: "Reports", defaultSize: { w: 10, h: 10 } },
];

export function getWidgetMeta(type) {
  return WIDGET_TYPES.find((w) => w.key === type) || WIDGET_TYPES[0];
}

export function newWidgetId() {
  return `dw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
