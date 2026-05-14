import {
  DASHBOARD_GRID_COLS,
  DASHBOARD_GRID_ROWS,
  WIDGET_TYPES,
  getWidgetMeta,
  newWidgetId,
} from "./widgetRegistry";

const CHART_INTERPOLATION_VALUES = new Set(["stepAfter", "linear", "monotone", "natural", "stepBefore"]);
const QUERY_GROUP_VALUES = new Set(["none", "1s", "5s", "10s", "30s", "1m", "5m", "15m", "1h", "1d"]);
const QUERY_AGG_VALUES = new Set(["count", "sum", "avg", "min", "max", "latest"]);
const QUERY_SELECTION_VALUES = new Set(["all", "last_n"]);
const QUERY_TIME_PRESET_VALUES = new Set(["none", "5m", "15m", "1h", "6h", "24h", "7d", "30d", "custom"]);
const TABLE_COL_VALUES = new Set(["ts", "gateway", "tag", "value"]);
const VALUE_FORMAT_VALUES = new Set(["auto", "int", "2dp", "3dp", "scientific"]);

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, Number(n) || min));
}

function canPlace(candidate, placed) {
  const x1 = candidate.x;
  const y1 = candidate.y;
  const x2 = x1 + candidate.w - 1;
  const y2 = y1 + candidate.h - 1;
  if (x1 < 0 || y1 < 0 || x2 >= DASHBOARD_GRID_COLS || y2 >= DASHBOARD_GRID_ROWS) return false;
  for (const p of placed) {
    const px1 = p.x;
    const py1 = p.y;
    const px2 = px1 + p.w - 1;
    const py2 = py1 + p.h - 1;
    const overlap = !(x2 < px1 || x1 > px2 || y2 < py1 || y1 > py2);
    if (overlap) return false;
  }
  return true;
}

function placeAtOrNextFree(widget, preferred, placed) {
  const candidate = {
    ...widget,
    x: clamp(preferred?.x ?? widget.x ?? 0, 0, DASHBOARD_GRID_COLS - widget.w),
    y: clamp(preferred?.y ?? widget.y ?? 0, 0, DASHBOARD_GRID_ROWS - widget.h),
  };
  if (canPlace(candidate, placed)) return candidate;
  const free = findFirstFreeSpot({ w: widget.w, h: widget.h }, placed);
  return { ...widget, x: free.x, y: free.y };
}

export function compactWidgets(widgets) {
  const current = normalizeWidgets(widgets);
  const ordered = [...current].sort((a, b) => {
    if (a.y !== b.y) return a.y - b.y;
    if (a.x !== b.x) return a.x - b.x;
    return String(a.id).localeCompare(String(b.id));
  });
  const placed = [];
  for (const item of ordered) {
    const nextPlaced = placeAtOrNextFree({ ...item }, { x: item.x, y: item.y }, placed);
    placed.push(nextPlaced);
  }
  return placed;
}

export function findFirstFreeSpot(widgetSize, placed) {
  const w = clamp(widgetSize?.w ?? 3, 1, DASHBOARD_GRID_COLS);
  const h = clamp(widgetSize?.h ?? 2, 1, DASHBOARD_GRID_ROWS);
  for (let y = 0; y <= DASHBOARD_GRID_ROWS - h; y += 1) {
    for (let x = 0; x <= DASHBOARD_GRID_COLS - w; x += 1) {
      const candidate = { x, y, w, h };
      if (canPlace(candidate, placed)) return candidate;
    }
  }
  return { x: 0, y: 0, w, h };
}

function migrateLegacyWidget(old, placed) {
  const legacyType = String(old?.chart_type || "line") === "bar" ? "bar_chart" : "line_chart";
  const meta = getWidgetMeta(legacyType);
  const pos = findFirstFreeSpot(meta.defaultSize, placed);
  return {
    id: String(old?.id || newWidgetId()),
    type: legacyType,
    title: String(old?.title || old?.tag_name || meta.label),
    color: String(old?.color || "#16a34a"),
    x: pos.x,
    y: pos.y,
    w: pos.w,
    h: pos.h,
    config: {
      gateway_id: String(old?.gateway_id || ""),
      tag_name: String(old?.tag_name || ""),
      readings_count: clamp(old?.readings_count ?? 120, 20, 500),
      interpolation: "stepAfter",
      data_source_type: "tag_direct",
      color_mode: "default",
      text: "",
      source_url: "",
      camera_url: "",
      list_limit: 8,
      query_group_interval: "none",
      query_result_aggregation: "count",
      query_row_selection: "all",
      query_row_limit: 200,
      query_rule_logic: "any",
      query_time_filter_preset: "none",
      query_time_filter_from: "",
      query_time_filter_to: "",
      query_table_columns: ["ts", "tag", "value"],
      compute_rules: [],
      pie_show_legend: true,
      pie_show_labels: true,
      pie_show_count: true,
      pie_show_percent: true,
      pie_legend_layout: "side",
      chart_show_legend: false,
      chart_show_point_labels: false,
      chart_value_format: "auto",
      text_font_scale: 1,
      table_filter_tags: [],
    },
  };
}

export function normalizeWidgets(rawWidgets) {
  const input = Array.isArray(rawWidgets) ? rawWidgets : [];
  const normalized = [];
  for (const raw of input) {
    if (!raw || typeof raw !== "object") continue;
    // Legacy model: { gateway_id, tag_name, chart_type, ... }.
    if (!("type" in raw)) {
      const migrated = migrateLegacyWidget(raw, normalized);
      normalized.push(migrated);
      continue;
    }
    const type = WIDGET_TYPES.some((x) => x.key === raw.type) ? raw.type : "line_chart";
    const meta = getWidgetMeta(type);
    const w = clamp(raw.w ?? meta.defaultSize.w, 1, DASHBOARD_GRID_COLS);
    const h = clamp(raw.h ?? meta.defaultSize.h, 1, DASHBOARD_GRID_ROWS);
    const x = clamp(raw.x ?? 0, 0, DASHBOARD_GRID_COLS - w);
    const y = clamp(raw.y ?? 0, 0, DASHBOARD_GRID_ROWS - h);
    const candidate = { x, y, w, h };
    const pos = canPlace(candidate, normalized) ? candidate : findFirstFreeSpot({ w, h }, normalized);
    normalized.push({
      id: String(raw.id || newWidgetId()),
      type,
      title: String(raw.title || meta.label),
      color: String(raw.color || "#16a34a"),
      x: pos.x,
      y: pos.y,
      w: pos.w,
      h: pos.h,
      config: {
        gateway_id: String(raw?.config?.gateway_id || raw?.gateway_id || ""),
        tag_name: String(raw?.config?.tag_name || raw?.tag_name || ""),
        readings_count: clamp(raw?.config?.readings_count ?? raw?.readings_count ?? 120, 20, 500),
        interpolation: CHART_INTERPOLATION_VALUES.has(String(raw?.config?.interpolation || ""))
          ? String(raw?.config?.interpolation)
          : "stepAfter",
        data_source_type: String(raw?.config?.data_source_type || "tag_direct") === "computed" ? "computed" : "tag_direct",
        color_mode: String(raw?.config?.color_mode || "default") === "custom" ? "custom" : "default",
        text: String(raw?.config?.text || ""),
        source_url: String(raw?.config?.source_url || ""),
        camera_url: String(raw?.config?.camera_url || ""),
        list_limit: clamp(raw?.config?.list_limit ?? 8, 1, 50),
        query_group_interval: QUERY_GROUP_VALUES.has(String(raw?.config?.query_group_interval || ""))
          ? String(raw?.config?.query_group_interval)
          : "none",
        query_result_aggregation: QUERY_AGG_VALUES.has(String(raw?.config?.query_result_aggregation || ""))
          ? String(raw?.config?.query_result_aggregation)
          : "count",
        query_row_selection: QUERY_SELECTION_VALUES.has(String(raw?.config?.query_row_selection || ""))
          ? String(raw?.config?.query_row_selection)
          : "all",
        query_row_limit: clamp(raw?.config?.query_row_limit ?? 200, 10, 5000),
        query_rule_logic: String(raw?.config?.query_rule_logic || "any") === "all" ? "all" : "any",
        query_time_filter_preset: QUERY_TIME_PRESET_VALUES.has(String(raw?.config?.query_time_filter_preset || ""))
          ? String(raw?.config?.query_time_filter_preset)
          : "none",
        query_time_filter_from: String(raw?.config?.query_time_filter_from || ""),
        query_time_filter_to: String(raw?.config?.query_time_filter_to || ""),
        query_table_columns: Array.isArray(raw?.config?.query_table_columns)
          ? raw.config.query_table_columns.filter((c) => TABLE_COL_VALUES.has(String(c)))
          : ["ts", "tag", "value"],
        compute_rules: Array.isArray(raw?.config?.compute_rules) ? raw.config.compute_rules : [],
        pie_show_legend: raw?.config?.pie_show_legend !== false,
        pie_show_labels: raw?.config?.pie_show_labels !== false,
        pie_show_count: raw?.config?.pie_show_count !== false,
        pie_show_percent: raw?.config?.pie_show_percent !== false,
        pie_legend_layout: String(raw?.config?.pie_legend_layout || "side") === "bottom" ? "bottom" : "side",
        chart_show_legend: raw?.config?.chart_show_legend === true,
        chart_show_point_labels: raw?.config?.chart_show_point_labels === true,
        chart_value_format: VALUE_FORMAT_VALUES.has(String(raw?.config?.chart_value_format || ""))
          ? String(raw?.config?.chart_value_format)
          : "auto",
        text_font_scale: clamp(raw?.config?.text_font_scale ?? 1, 0.7, 2.5),
        table_filter_tags: Array.isArray(raw?.config?.table_filter_tags)
          ? raw.config.table_filter_tags.map((t) => String(t || "")).filter(Boolean)
          : [],
      },
    });
  }
  return normalized;
}

export function swapWidgetPositions(widgets, draggedId, targetId) {
  if (!draggedId || !targetId || draggedId === targetId) return widgets;
  const next = [...widgets];
  const a = next.find((w) => w.id === draggedId);
  const b = next.find((w) => w.id === targetId);
  if (!a || !b) return widgets;
  const ax = a.x;
  const ay = a.y;
  a.x = b.x;
  a.y = b.y;
  b.x = ax;
  b.y = ay;
  return next;
}

export function reflowWidgetsForMove(widgets, draggedId, targetId) {
  if (!draggedId || !targetId || draggedId === targetId) return widgets;
  const current = normalizeWidgets(widgets);
  const dragged = current.find((w) => w.id === draggedId);
  const target = current.find((w) => w.id === targetId);
  if (!dragged || !target) return current;

  const placed = [];
  const draggedPlaced = placeAtOrNextFree(
    { ...dragged },
    { x: target.x, y: target.y },
    placed
  );
  placed.push(draggedPlaced);

  for (const item of current) {
    if (item.id === draggedId) continue;
    const preferred = item.id === targetId ? { x: dragged.x, y: dragged.y } : { x: item.x, y: item.y };
    const nextPlaced = placeAtOrNextFree({ ...item }, preferred, placed);
    placed.push(nextPlaced);
  }

  return compactWidgets(placed);
}

export function reflowWidgetsForMoveToPoint(widgets, draggedId, targetX, targetY) {
  if (!draggedId) return normalizeWidgets(widgets);
  const current = normalizeWidgets(widgets);
  const dragged = current.find((w) => w.id === draggedId);
  if (!dragged) return current;

  const placed = [];
  const draggedPlaced = placeAtOrNextFree(
    { ...dragged },
    { x: clamp(targetX, 0, DASHBOARD_GRID_COLS - dragged.w), y: clamp(targetY, 0, DASHBOARD_GRID_ROWS - dragged.h) },
    placed
  );
  placed.push(draggedPlaced);

  for (const item of current) {
    if (item.id === draggedId) continue;
    const nextPlaced = placeAtOrNextFree({ ...item }, { x: item.x, y: item.y }, placed);
    placed.push(nextPlaced);
  }
  return compactWidgets(placed);
}

export function reflowWidgetsForResize(widgets, resizedId, nextW, nextH) {
  if (!resizedId) return normalizeWidgets(widgets);
  const current = normalizeWidgets(widgets);
  const resized = current.find((w) => w.id === resizedId);
  if (!resized) return current;

  const patched = {
    ...resized,
    w: clamp(nextW, 1, DASHBOARD_GRID_COLS),
    h: clamp(nextH, 1, DASHBOARD_GRID_ROWS),
  };
  patched.x = clamp(patched.x, 0, DASHBOARD_GRID_COLS - patched.w);
  patched.y = clamp(patched.y, 0, DASHBOARD_GRID_ROWS - patched.h);

  const placed = [];
  const first = placeAtOrNextFree(patched, { x: patched.x, y: patched.y }, placed);
  placed.push(first);

  for (const item of current) {
    if (item.id === resizedId) continue;
    const nextPlaced = placeAtOrNextFree({ ...item }, { x: item.x, y: item.y }, placed);
    placed.push(nextPlaced);
  }
  return compactWidgets(placed);
}
