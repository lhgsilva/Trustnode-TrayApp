// Default Power Dashboard layout shipped with the app.
// Operator 2026-06-16: when the operator installs / enables the
// Power module and asks to load the default Power dashboard, this
// template materialises a fresh set of widgets bound to whichever
// meter gateway they choose. The `gateway_id` field in every widget
// is rewritten at materialisation time to the target meter's id.
// New widget IDs are generated so multiple loads don't collide.
//
// The layout is authored against the v2 40×40 grid (see
// widgetRegistry.js DASHBOARD_GRID_VERSION). Existing dashboards
// continue to use whatever the operator built; this template only
// applies when explicitly requested (toolbar action).

import { newWidgetId } from "./widgetRegistry";

// Raw template — gateway_id uses the sentinel "<METER>" which the
// materializer rewrites. Kept identical in shape to a stored
// dashboard export so we can diff against operator-edited copies if
// needed in the future.
export const POWER_DASHBOARD_TEMPLATE = {
  mode: "kpi",
  per_row: 4,
  tag_colors: {},
  widgets: [
    // Row 1 — eight KPIs across the top.
    {
      type: "value_kpi",
      title: "Current",
      color: "#14a89a",
      x: 0, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.current_a",
        data_source_type: "tag_direct",
        unit_suffix: "A",
        value_decimals: 3,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.6,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Active Power",
      color: "#14a89a",
      x: 5, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.active_power_kw",
        data_source_type: "tag_direct",
        unit_suffix: "kW",
        value_decimals: 3,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.6,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Power Usage",
      color: "#14a89a",
      x: 10, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.power_usage_kwh",
        data_source_type: "tag_direct",
        unit_suffix: "kWh",
        value_decimals: 3,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.6,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Energy Efficiency",
      color: "#14a89a",
      x: 15, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.energy_efficiency_pct",
        data_source_type: "tag_direct",
        unit_suffix: "%",
        value_decimals: 1,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.8,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Peak Power",
      color: "#14a89a",
      x: 20, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.peak_kw",
        data_source_type: "tag_direct",
        unit_suffix: "kW",
        value_decimals: 3,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.8,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Downtime Cost",
      color: "#14a89a",
      x: 25, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.downtime_cost_eur",
        data_source_type: "tag_direct",
        unit_suffix: "€",
        value_decimals: 2,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.8,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Total Energy",
      color: "#14a89a",
      x: 30, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.total_kwh",
        data_source_type: "tag_direct",
        unit_suffix: "kWh",
        value_decimals: 3,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.8,
        hide_widget_header: false,
      },
    },
    {
      type: "value_kpi",
      title: "Energy Cost",
      color: "#14a89a",
      x: 35, y: 0, w: 5, h: 6,
      config: {
        gateway_id: "<METER>",
        tag_name: "insight.energy_cost_eur",
        data_source_type: "tag_direct",
        unit_suffix: "€",
        value_decimals: 2,
        unit_size_scale: 1,
        header_parts: ["tag"],
        body_text_scale: 0.8,
        hide_widget_header: false,
      },
    },

    // Row 2 — current trend (small) + power trend (large).
    {
      type: "line_area_chart",
      title: "Current",
      color: "#14a89a",
      x: 0, y: 6, w: 15, h: 11,
      config: {
        gateway_id: "<METER>",
        tag_name: "current_a",
        interpolation: "natural",
        data_source_type: "tag_direct",
        chart_show_legend: false,
        chart_value_format: "auto",
        chart_line_width: 2,
        primary_unit: "A",
      },
    },
    {
      type: "line_area_chart",
      title: "Active Power",
      color: "#14a89a",
      x: 15, y: 6, w: 25, h: 23,
      config: {
        gateway_id: "<METER>",
        tag_name: "active_power_w",
        interpolation: "natural",
        data_source_type: "tag_direct",
        chart_show_legend: false,
        chart_value_format: "auto",
        chart_line_width: 2,
        primary_unit: "W",
      },
    },

    // Row 3 — second current trend + bar chart for cumulative energy.
    {
      type: "line_area_chart",
      title: "Current",
      color: "#14a89a",
      x: 0, y: 17, w: 15, h: 12,
      config: {
        gateway_id: "<METER>",
        tag_name: "current_a",
        interpolation: "natural",
        data_source_type: "tag_direct",
        chart_show_legend: false,
        chart_value_format: "auto",
        chart_line_width: 2,
        primary_unit: "A",
      },
    },
    {
      type: "bar_chart",
      title: "Energy",
      color: "#14a89a",
      x: 0, y: 29, w: 15, h: 11,
      config: {
        gateway_id: "<METER>",
        tag_name: "energy_wh",
        interpolation: "stepAfter",
        data_source_type: "tag_direct",
        chart_show_legend: false,
        chart_value_format: "auto",
        chart_line_width: 2,
        primary_unit: "Wh",
        header_parts: ["value", "tag", "title"],
      },
    },

    // Row 4 — two Energy Tariffs widgets (donut + bars by kWh).
    {
      type: "energy_tariffs",
      title: "Energy Tariffs",
      color: "#14a89a",
      x: 15, y: 29, w: 12, h: 11,
      config: {
        gateway_id: "<METER>",
        display_mode: "donut",
        tariff_value_mode: "cost",
        header_parts: ["value", "tag", "title"],
      },
    },
    {
      type: "energy_tariffs",
      title: "Energy Tariffs (kWh)",
      color: "#14a89a",
      x: 27, y: 29, w: 13, h: 11,
      config: {
        gateway_id: "<METER>",
        display_mode: "bars",
        tariff_value_mode: "kwh",
        header_parts: ["value", "tag", "title"],
      },
    },
  ],
};

// Build a fresh set of widgets bound to `meterGatewayId`. Each widget
// gets a unique id, the sentinel "<METER>" is replaced with the
// target meter, and the config is deep-cloned so the caller can
// mutate without poisoning the shared template.
export function materializePowerDashboard(meterGatewayId) {
  const meter = String(meterGatewayId || "").trim();
  if (!meter) return [];
  return POWER_DASHBOARD_TEMPLATE.widgets.map((w) => {
    const config = { ...(w.config || {}) };
    if (String(config.gateway_id || "") === "<METER>") {
      config.gateway_id = meter;
    }
    return {
      ...w,
      id: newWidgetId(),
      config,
    };
  });
}

// Convenience: build a complete dashboard payload (widgets + mode +
// per_row + tag_colors) so the caller can drop it straight into the
// dashboardWidgets state and the dashboard layout settings.
export function materializePowerDashboardPayload(meterGatewayId) {
  return {
    widgets: materializePowerDashboard(meterGatewayId),
    mode: POWER_DASHBOARD_TEMPLATE.mode,
    per_row: POWER_DASHBOARD_TEMPLATE.per_row,
    tag_colors: { ...(POWER_DASHBOARD_TEMPLATE.tag_colors || {}) },
  };
}
