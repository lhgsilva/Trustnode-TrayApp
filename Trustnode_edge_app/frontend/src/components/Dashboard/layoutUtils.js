import {
  DASHBOARD_GRID_COLS,
  DASHBOARD_GRID_ROWS,
  WIDGET_TYPES,
  getWidgetMeta,
  newWidgetId,
} from "./widgetRegistry";

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
      text: "",
      source_url: "",
      camera_url: "",
      list_limit: 8,
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
        text: String(raw?.config?.text || ""),
        source_url: String(raw?.config?.source_url || ""),
        camera_url: String(raw?.config?.camera_url || ""),
        list_limit: clamp(raw?.config?.list_limit ?? 8, 1, 50),
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

  return placed;
}
