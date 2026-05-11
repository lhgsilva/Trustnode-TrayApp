import React, { useMemo } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  AreaChart,
  Area,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  buildFixedText,
  evaluateComputedRules,
  getLatestTagRow,
  getTagSeries as getTagSeriesFiltered,
} from "./dashboardAnalytics";

function parseNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function renderEmpty(text = "No data") {
  return <div className="dashboard-widget-empty">{text}</div>;
}

function shadeHex(hex, percent = 0) {
  const raw = String(hex || "").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return hex;
  const num = parseInt(raw, 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  const f = Math.max(-1, Math.min(1, percent));
  const target = f < 0 ? 0 : 255;
  const p = Math.abs(f);
  const nr = Math.round((target - r) * p + r);
  const ng = Math.round((target - g) * p + g);
  const nb = Math.round((target - b) * p + b);
  return `#${[nr, ng, nb].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function rgbaFromHex(hex, alpha = 0.2) {
  const raw = String(hex || "").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return `rgba(20, 168, 154, ${alpha})`;
  const num = parseInt(raw, 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function useCustomColor(widget) {
  return String(widget?.config?.color_mode || "default") === "custom";
}

function getWidgetAccent(widget, fallback = "#14a89a") {
  return useCustomColor(widget) ? (widget?.color || fallback) : fallback;
}

function getPiePalette(widget) {
  if (!useCustomColor(widget)) {
    return ["#14a89a", "#0e8479", "#3cd2c2", "#1f3a5f", "#6e8dd2", "#e0a050", "#e2585d", "#a78bfa"];
  }
  const base = widget?.color || "#14a89a";
  return [
    base,
    shadeHex(base, -0.16),
    shadeHex(base, 0.12),
    shadeHex(base, -0.28),
    shadeHex(base, 0.24),
    shadeHex(base, -0.40),
    shadeHex(base, 0.36),
    shadeHex(base, -0.52),
  ];
}

export function DashboardWidgetCard({
  widget,
  dataLogView,
  tagRows,
  tagRowsByGateway,
  formatTagForDisplay,
}) {
  const cfg = widget?.config || {};
  const gatewayId = cfg.gateway_id || "";
  const tagName = cfg.tag_name || "";
  const dataSourceType = String(cfg.data_source_type || "tag_direct");
  const rules = Array.isArray(cfg.compute_rules) ? cfg.compute_rules : [];

  const latest = useMemo(() => getLatestTagRow(dataLogView, gatewayId, tagName), [dataLogView, gatewayId, tagName]);
  const series = useMemo(
    () => getTagSeriesFiltered(dataLogView, gatewayId, tagName, cfg.readings_count || 120),
    [dataLogView, gatewayId, tagName, cfg.readings_count]
  );
  const computedItems = useMemo(() => evaluateComputedRules(dataLogView, rules), [dataLogView, rules]);
  const displayTag = formatTagForDisplay ? formatTagForDisplay(tagName) : tagName;

  switch (widget.type) {
    case "line_chart":
    case "line_area_chart":
      return (
        <div className="dashboard-widget-block">
          {series.length ? (
            <div className="dashboard-widget-chart">
              {widget.type === "line_area_chart" ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series}>
                    <XAxis dataKey="idx" hide />
                    <YAxis hide />
                    <Tooltip
                      formatter={(v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(3) : "-")}
                      labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                    />
                    <Area
                      type="monotone"
                      dataKey="value"
                      stroke={getWidgetAccent(widget, "#14a89a")}
                      fill={rgbaFromHex(getWidgetAccent(widget, "#14a89a"), 0.24)}
                      strokeWidth={2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={series}>
                    <XAxis dataKey="idx" hide />
                    <YAxis hide />
                    <Tooltip
                      formatter={(v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(3) : "-")}
                      labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                    />
                    <Line type="monotone" dataKey="value" stroke={getWidgetAccent(widget, "#14a89a")} strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
          ) : renderEmpty("No points")}
          <div className="dashboard-widget-foot">{displayTag}</div>
        </div>
      );
    case "bar_chart":
      return (
        <div className="dashboard-widget-block">
          {series.length ? (
            <div className="dashboard-widget-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={series}>
                  <XAxis dataKey="idx" hide />
                  <YAxis hide />
                  <Tooltip
                    formatter={(v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(3) : "-")}
                    labelFormatter={(v) => series.find((p) => p.idx === v)?.ts || String(v)}
                  />
                  <Bar dataKey="value" fill={getWidgetAccent(widget, "#1f3a5f")} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : renderEmpty("No points")}
          <div className="dashboard-widget-foot">{displayTag}</div>
        </div>
      );
    case "pie_chart": {
      const gatewayRows = (tagRowsByGateway?.[String(gatewayId)] || []).slice(0, 8);
      const pieData = dataSourceType === "computed"
        ? computedItems
            .map((it) => ({ name: it.label, value: Math.abs(Number(it.value)) || 0, color: it.color }))
            .filter((r) => r.value > 0)
        : gatewayRows
            .map((r) => ({
              name: formatTagForDisplay ? formatTagForDisplay(r.tag_name) : r.tag_name,
              value: Math.abs(Number(r.last_value)) || 0,
            }))
            .filter((r) => r.value > 0);
      const palette = getPiePalette(widget);
      return (
        <div className="dashboard-widget-block">
          {pieData.length ? (
            <div className="dashboard-widget-chart">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={40} outerRadius={70}>
                    {pieData.map((entry, idx) => (
                      <Cell key={`${entry.name}-${idx}`} fill={entry.color || palette[idx % palette.length]} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(2) : "-")} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          ) : renderEmpty("No values")}
        </div>
      );
    }
    case "meter_chart": {
      const value = dataSourceType === "computed"
        ? parseNumber(computedItems[0]?.value)
        : parseNumber(latest?.last_value);
      const pct = Math.max(0, Math.min(100, Number(value ?? 0)));
      const accent = getWidgetAccent(widget, "#0e8479");
      const gaugeData = [
        { name: "value", value: pct, fill: accent },
        { name: "remaining", value: Math.max(0, 100 - pct), fill: "var(--card-2, #232a36)" },
      ];
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-meter-gauge-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={gaugeData}
                  dataKey="value"
                  startAngle={180}
                  endAngle={0}
                  cx="50%"
                  cy="84%"
                  innerRadius="62%"
                  outerRadius="90%"
                  stroke="none"
                  paddingAngle={0}
                  isAnimationActive={false}
                >
                  {gaugeData.map((entry, idx) => (
                    <Cell key={`meter-seg-${idx}`} fill={entry.fill} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="dashboard-meter-value">{value === null ? "-" : value.toFixed(2)}</div>
          <div className="dashboard-widget-foot">{displayTag}</div>
        </div>
      );
    }
    case "text_kpi":
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-kpi-text">{displayTag || "-"}</div>
          <div className="dashboard-widget-foot">{latest?.last_ts || "-"}</div>
        </div>
      );
    case "value_kpi": {
      const value = dataSourceType === "computed"
        ? parseNumber(computedItems[0]?.value)
        : parseNumber(latest?.last_value);
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-kpi-value" style={{ color: getWidgetAccent(widget, "#14a89a") }}>
            {value === null ? "-" : value.toFixed(3)}
          </div>
          <div className="dashboard-widget-foot">{displayTag}</div>
        </div>
      );
    }
    case "fixed_text":
      return (
        <div className="dashboard-widget-block">
          <div className="dashboard-fixed-text">
            {dataSourceType === "computed" ? (buildFixedText(cfg.text || "", computedItems) || cfg.text || "-") : (cfg.text || "-")}
          </div>
        </div>
      );
    case "divider":
      return (
        <div className="dashboard-divider-block">
          <span>{cfg.text || ""}</span>
        </div>
      );
    case "table_list": {
      const rows = dataSourceType === "computed"
        ? computedItems.slice(0, Math.max(1, Number(cfg.list_limit || 8)))
        : (tagRowsByGateway?.[String(gatewayId)] || []).slice(0, Math.max(1, Number(cfg.list_limit || 8)));
      return (
        <div className="dashboard-widget-block">
          {rows.length ? (
            <div className="dashboard-table-mini-wrap">
              <table className="dashboard-table-mini">
                <thead>
                  <tr>
                    <th>{dataSourceType === "computed" ? "Label" : "Tag"}</th>
                    <th>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {dataSourceType === "computed"
                    ? rows.map((r) => (
                        <tr key={`${r.id}`}>
                          <td>{r.label}</td>
                          <td>{Number.isFinite(Number(r.value)) ? Number(r.value).toFixed(3) : "-"}</td>
                        </tr>
                      ))
                    : rows.map((r) => (
                        <tr key={`${r.gateway_id}-${r.tag_name}`}>
                          <td>{formatTagForDisplay ? formatTagForDisplay(r.tag_name) : r.tag_name}</td>
                          <td>{r.last_value ?? "-"}</td>
                        </tr>
                      ))}
                </tbody>
              </table>
            </div>
          ) : renderEmpty("No rows")}
        </div>
      );
    }
    case "image":
      return (
        <div className="dashboard-widget-block">
          {cfg.source_url ? <img src={cfg.source_url} alt={widget.title || "widget"} className="dashboard-media" /> : renderEmpty("Set image URL")}
        </div>
      );
    case "ip_camera":
      return (
        <div className="dashboard-widget-block">
          {cfg.camera_url ? (
            <iframe
              className="dashboard-media"
              src={cfg.camera_url}
              title={widget.title || "ip-camera-feed"}
              allow="autoplay; fullscreen"
            />
          ) : renderEmpty("Set camera URL")}
        </div>
      );
    default:
      return renderEmpty("Unsupported widget");
  }
}
