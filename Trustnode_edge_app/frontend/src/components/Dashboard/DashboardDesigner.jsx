import React, { useEffect, useMemo, useRef, useState } from "react";
import { DashboardWidgetCard } from "./DashboardWidgets";
import {
  DASHBOARD_GRID_COLS,
  DASHBOARD_GRID_ROWS,
  WIDGET_TYPES,
  getWidgetMeta,
  newWidgetId,
} from "./widgetRegistry";
import { findFirstFreeSpot, normalizeWidgets, reflowWidgetsForMove } from "./layoutUtils";
import { filterRowsByRange, toTsMs } from "./dashboardAnalytics";
import "./dashboard.css";

const TYPE_GROUPS = ["Charts", "KPI", "Content", "Layout", "Media"];
const DASHBOARD_TIME_MODE_KEY = "trustnode_dashboard_time_mode";
const DASHBOARD_TIME_RANGE_KEY = "trustnode_dashboard_time_range";
const RULE_OPERATORS = [
  { value: "any", label: "Any" },
  { value: "eq", label: "=" },
  { value: "ne", label: "!=" },
  { value: "lt", label: "<" },
  { value: "lte", label: "<=" },
  { value: "gt", label: ">" },
  { value: "gte", label: ">=" },
  { value: "between", label: "Between" },
];
const RULE_AGGREGATIONS = [
  { value: "count", label: "Count" },
  { value: "sum", label: "Sum" },
  { value: "avg", label: "Average" },
  { value: "min", label: "Min" },
  { value: "max", label: "Max" },
  { value: "latest", label: "Latest" },
];

function newRule() {
  return {
    id: `rule-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    label: "Item",
    gateway_id: "",
    tag_name: "",
    operator: "any",
    value1: "",
    value2: "",
    aggregation: "count",
    color: "#14a89a",
  };
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, Number(n) || min));
}

function buildDefaultForm(type = "line_chart") {
  const meta = getWidgetMeta(type);
  return {
    type,
    title: meta.label,
    color: "#14a89a",
    w: meta.defaultSize.w,
    h: meta.defaultSize.h,
    x: null,
    y: null,
    config: {
      gateway_id: "",
      tag_name: "",
      readings_count: 120,
      data_source_type: "tag_direct",
      color_mode: "default",
      text: "",
      source_url: "",
      camera_url: "",
      list_limit: 8,
      compute_rules: [],
    },
  };
}

export function DashboardDesigner({
  canEdit,
  widgets,
  setWidgets,
  tagRows,
  dataLogView,
  formatTagForDisplay,
  fetchHistoricalRows,
  gatewayCatalog,
  tagsByGateway,
  showGridMeta = true,
}) {
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState("type");
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(buildDefaultForm("line_chart"));
  const [draggingId, setDraggingId] = useState("");
  const [menuWidgetId, setMenuWidgetId] = useState("");
  const [configModalOpen, setConfigModalOpen] = useState(false);
  const [pendingImportWidgets, setPendingImportWidgets] = useState(null);
  const [pendingImportName, setPendingImportName] = useState("");
  const [dashboardTimeMode, setDashboardTimeMode] = useState("live");
  const [dashboardFrom, setDashboardFrom] = useState("");
  const [dashboardTo, setDashboardTo] = useState("");
  const [historicalRows, setHistoricalRows] = useState([]);
  const [historicalRangeKey, setHistoricalRangeKey] = useState("");
  const [historicalLoading, setHistoricalLoading] = useState(false);
  const [historicalError, setHistoricalError] = useState("");
  const importInputRef = useRef(null);

  const normalizedWidgets = useMemo(() => normalizeWidgets(widgets), [widgets]);
  const tagRowsByGateway = useMemo(() => {
    const out = {};
    for (const row of Array.isArray(tagRows) ? tagRows : []) {
      const key = String(row.gateway_id || "");
      if (!out[key]) out[key] = [];
      out[key].push(row);
    }
    return out;
  }, [tagRows]);

  const rangeKey = `${dashboardFrom || ""}|${dashboardTo || ""}`;
  const hasHistoricalRange = Boolean(dashboardFrom || dashboardTo);
  const localHistoricalRows = useMemo(
    () => filterRowsByRange(dataLogView, dashboardFrom, dashboardTo),
    [dataLogView, dashboardFrom, dashboardTo]
  );

  const dashboardRows = useMemo(() => {
    if (dashboardTimeMode !== "historical") return Array.isArray(dataLogView) ? dataLogView : [];
    if (hasHistoricalRange && historicalRangeKey === rangeKey && Array.isArray(historicalRows)) return historicalRows;
    return localHistoricalRows;
  }, [dashboardTimeMode, dataLogView, hasHistoricalRange, historicalRangeKey, rangeKey, historicalRows, localHistoricalRows]);

  const dashboardTagRowsByGateway = useMemo(() => {
    const byKey = new Map();
    for (const row of dashboardRows) {
      const gid = String(row?.gateway_id || "").trim();
      const tag = String(row?.tag || row?.tag_name || "").trim();
      if (!gid || !tag) continue;
      const key = `${gid}::${tag}`;
      const ts = toTsMs(row?.ts);
      const prev = byKey.get(key);
      if (!prev || ts > prev.ts) {
        byKey.set(key, {
          ts,
          row: {
            gateway_id: gid,
            gateway_name: row?.gateway_name || gid,
            tag_name: tag,
            last_value: row?.value,
            last_ts: row?.ts || "",
          },
        });
      }
    }
    const out = {};
    for (const payload of byKey.values()) {
      const gid = payload.row.gateway_id;
      if (!out[gid]) out[gid] = [];
      out[gid].push(payload.row);
    }
    return out;
  }, [dashboardRows]);

  useEffect(() => {
    try {
      const savedMode = localStorage.getItem(DASHBOARD_TIME_MODE_KEY);
      if (savedMode === "live" || savedMode === "historical") setDashboardTimeMode(savedMode);
      const savedRangeRaw = localStorage.getItem(DASHBOARD_TIME_RANGE_KEY);
      if (savedRangeRaw) {
        const savedRange = JSON.parse(savedRangeRaw);
        setDashboardFrom(String(savedRange?.from || ""));
        setDashboardTo(String(savedRange?.to || ""));
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(DASHBOARD_TIME_MODE_KEY, dashboardTimeMode);
      localStorage.setItem(DASHBOARD_TIME_RANGE_KEY, JSON.stringify({ from: dashboardFrom, to: dashboardTo }));
    } catch {}
  }, [dashboardTimeMode, dashboardFrom, dashboardTo]);

  useEffect(() => {
    if (dashboardTimeMode !== "historical") return;
    if (!hasHistoricalRange) {
      setHistoricalRows([]);
      setHistoricalRangeKey("");
      setHistoricalError("");
      setHistoricalLoading(false);
      return;
    }
    if (typeof fetchHistoricalRows !== "function") return;

    const toUtcIso = (value) => {
      const txt = String(value || "").trim();
      if (!txt) return "";
      const dt = new Date(txt);
      if (!Number.isFinite(dt.getTime())) return "";
      return dt.toISOString();
    };

    let canceled = false;
    const targetKey = rangeKey;
    const timer = setTimeout(async () => {
      try {
        setHistoricalLoading(true);
        setHistoricalError("");
        const rows = await fetchHistoricalRows({
          fromUtc: toUtcIso(dashboardFrom),
          toUtc: toUtcIso(dashboardTo),
        });
        if (canceled) return;
        setHistoricalRows(Array.isArray(rows) ? rows : []);
        setHistoricalRangeKey(targetKey);
      } catch (err) {
        if (canceled) return;
        setHistoricalError(String(err?.message || err || "Failed to load historical dashboard data."));
      } finally {
        if (!canceled) setHistoricalLoading(false);
      }
    }, 220);

    return () => {
      canceled = true;
      clearTimeout(timer);
    };
  }, [dashboardTimeMode, hasHistoricalRange, dashboardFrom, dashboardTo, rangeKey, fetchHistoricalRows]);

  const gatewayOptions = useMemo(() => {
    const map = new Map();
    const configured = Array.isArray(gatewayCatalog) ? gatewayCatalog : [];
    const configuredTags = tagsByGateway && typeof tagsByGateway === "object" ? tagsByGateway : {};

    for (const g of configured) {
      const id = String(g?.id || "").trim();
      if (!id) continue;
      const name = String(g?.name || id).trim() || id;
      const tags = Array.from(
        new Set((Array.isArray(configuredTags[id]) ? configuredTags[id] : []).map((t) => String(t || "").trim()).filter(Boolean))
      );
      map.set(id, { id, name, tags });
    }

    for (const [id, rows] of Object.entries(tagRowsByGateway)) {
      const gid = String(id || "").trim();
      if (!gid) continue;
      const observed = Array.isArray(rows)
        ? Array.from(new Set(rows.map((r) => String(r?.tag_name || "").trim()).filter(Boolean)))
        : [];
      if (!map.has(gid)) {
        map.set(gid, {
          id: gid,
          name: String(rows?.[0]?.gateway_name || gid),
          tags: observed,
        });
        continue;
      }
      const current = map.get(gid);
      current.tags = Array.from(new Set([...(current.tags || []), ...observed]));
      if (!current.name && rows?.[0]?.gateway_name) current.name = String(rows[0].gateway_name);
      map.set(gid, current);
    }
    return Array.from(map.values());
  }, [tagRowsByGateway, gatewayCatalog, tagsByGateway]);

  const openCreate = () => {
    setEditingId(null);
    setForm(buildDefaultForm("line_chart"));
    setTab("type");
    setModalOpen(true);
  };

  const openEdit = (widget) => {
    const meta = getWidgetMeta(widget.type);
    setEditingId(widget.id);
    setForm({
      type: widget.type,
      title: widget.title || meta.label,
      color: widget.color || "#14a89a",
      w: widget.w,
      h: widget.h,
      x: widget.x,
      y: widget.y,
      config: {
        gateway_id: widget?.config?.gateway_id || "",
        tag_name: widget?.config?.tag_name || "",
        readings_count: clamp(widget?.config?.readings_count ?? 120, 20, 500),
        data_source_type: widget?.config?.data_source_type === "computed" ? "computed" : "tag_direct",
        color_mode: widget?.config?.color_mode === "custom" ? "custom" : "default",
        text: widget?.config?.text || "",
        source_url: widget?.config?.source_url || "",
        camera_url: widget?.config?.camera_url || "",
        list_limit: clamp(widget?.config?.list_limit ?? 8, 1, 50),
        compute_rules: Array.isArray(widget?.config?.compute_rules) ? widget.config.compute_rules : [],
      },
    });
    setTab("config");
    setModalOpen(true);
  };

  const removeWidget = (id) => {
    if (!canEdit) return;
    setWidgets((prev) => normalizeWidgets(prev).filter((w) => w.id !== id));
  };

  const saveWidget = () => {
    const next = normalizeWidgets(widgets);
    const meta = getWidgetMeta(form.type);
    const candidate = {
      id: editingId || newWidgetId(),
      type: form.type,
      title: String(form.title || meta.label).trim() || meta.label,
      color: String(form.color || "#14a89a"),
      w: clamp(form.w ?? meta.defaultSize.w, 1, DASHBOARD_GRID_COLS),
      h: clamp(form.h ?? meta.defaultSize.h, 1, DASHBOARD_GRID_ROWS),
      x: Number.isFinite(Number(form.x)) ? clamp(form.x, 0, DASHBOARD_GRID_COLS - 1) : null,
      y: Number.isFinite(Number(form.y)) ? clamp(form.y, 0, DASHBOARD_GRID_ROWS - 1) : null,
      config: {
        gateway_id: String(form?.config?.gateway_id || ""),
        tag_name: String(form?.config?.tag_name || ""),
        readings_count: clamp(form?.config?.readings_count ?? 120, 20, 500),
        data_source_type: form?.config?.data_source_type === "computed" ? "computed" : "tag_direct",
        color_mode: form?.config?.color_mode === "custom" ? "custom" : "default",
        text: String(form?.config?.text || ""),
        source_url: String(form?.config?.source_url || ""),
        camera_url: String(form?.config?.camera_url || ""),
        list_limit: clamp(form?.config?.list_limit ?? 8, 1, 50),
        compute_rules: Array.isArray(form?.config?.compute_rules) ? form.config.compute_rules : [],
      },
    };
    const others = next.filter((w) => w.id !== candidate.id);
    const pos = candidate.x === null || candidate.y === null
      ? findFirstFreeSpot({ w: candidate.w, h: candidate.h }, others)
      : { x: candidate.x, y: candidate.y };
    candidate.x = clamp(pos.x, 0, DASHBOARD_GRID_COLS - candidate.w);
    candidate.y = clamp(pos.y, 0, DASHBOARD_GRID_ROWS - candidate.h);
    setWidgets([...others, candidate]);
    setModalOpen(false);
    setEditingId(null);
  };

  const onDragStart = (id) => setDraggingId(id);
  const onDragOverWidget = (targetId) => {
    if (!canEdit || !draggingId || draggingId === targetId) return;
    setWidgets((prev) => reflowWidgetsForMove(normalizeWidgets(prev), draggingId, targetId));
  };
  const onDropOn = () => {
    setDraggingId("");
  };

  const selectedGatewayTags = useMemo(() => {
    const gw = gatewayOptions.find((g) => String(g.id) === String(form?.config?.gateway_id || ""));
    return gw?.tags || [];
  }, [gatewayOptions, form?.config?.gateway_id]);

  const supportsComputed = useMemo(
    () => ["pie_chart", "meter_chart", "table_list", "fixed_text", "value_kpi", "text_kpi"].includes(form.type),
    [form.type]
  );

  const addComputeRule = () => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: [...(Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : []), newRule()],
      },
    }));
  };

  const updateComputeRule = (ruleId, patch) => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: (Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : []).map((r) =>
          String(r?.id) === String(ruleId) ? { ...r, ...patch } : r
        ),
      },
    }));
  };

  const removeComputeRule = (ruleId) => {
    setForm((p) => ({
      ...p,
      config: {
        ...p.config,
        compute_rules: (Array.isArray(p?.config?.compute_rules) ? p.config.compute_rules : []).filter(
          (r) => String(r?.id) !== String(ruleId)
        ),
      },
    }));
  };

  const tagsForRuleGateway = (gatewayId) => {
    const gw = gatewayOptions.find((g) => String(g.id) === String(gatewayId || ""));
    return gw?.tags || [];
  };

  const exportDashboardConfig = () => {
    const payload = {
      version: 1,
      exported_at: new Date().toISOString(),
      widgets: normalizeWidgets(widgets),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trustnode-dashboard-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const onImportDashboardConfig = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const raw = await file.text();
      const parsed = JSON.parse(raw);
      const incoming = Array.isArray(parsed) ? parsed : parsed?.widgets;
      if (!Array.isArray(incoming)) throw new Error("Invalid dashboard file: widgets list not found.");
      setPendingImportWidgets(incoming);
      setPendingImportName(String(file.name || "dashboard-config.json"));
      setMenuWidgetId("");
    } catch (err) {
      window.alert(String(err?.message || err || "Failed to import dashboard config."));
    } finally {
      if (importInputRef.current) importInputRef.current.value = "";
    }
  };

  const confirmLoadDashboardConfig = () => {
    if (!pendingImportWidgets) return;
    setWidgets(normalizeWidgets(pendingImportWidgets));
    setPendingImportWidgets(null);
    setPendingImportName("");
    setConfigModalOpen(false);
  };

  return (
    <div className="dashboard-designer">
      <section className="page-tools dashboard-designer-tools">
        <div className="dashboard-tools-left">
          <button className="dashboard-toolbar-icon-btn" onClick={openCreate} disabled={!canEdit} title="Add widget" aria-label="Add widget">
            <AddIcon />
          </button>
          <button
            className="dashboard-toolbar-icon-btn"
            onClick={() => setConfigModalOpen(true)}
            disabled={!canEdit}
            title="Dashboard configuration"
            aria-label="Dashboard configuration"
          >
            <CogIcon />
          </button>
        </div>
        <div className="dashboard-tools-right">
          <div className="dashboard-mode-pills">
            <button
              type="button"
              className={`dashboard-pill ${dashboardTimeMode === "live" ? "active" : ""}`}
              onClick={() => setDashboardTimeMode("live")}
            >
              Live
            </button>
            <button
              type="button"
              className={`dashboard-pill ${dashboardTimeMode === "historical" ? "active" : ""}`}
              onClick={() => setDashboardTimeMode("historical")}
            >
              Historical
            </button>
          </div>
          {dashboardTimeMode === "historical" ? (
            <div className="dashboard-range-controls">
              <input type="datetime-local" value={dashboardFrom} onChange={(e) => setDashboardFrom(e.target.value)} />
              <input type="datetime-local" value={dashboardTo} onChange={(e) => setDashboardTo(e.target.value)} />
            </div>
          ) : null}
          {showGridMeta && dashboardTimeMode === "historical" ? (
            <div className="dashboard-grid-meta">
              {historicalLoading ? "Loading history..." : (historicalError ? historicalError : `Rows: ${dashboardRows.length}`)}
            </div>
          ) : null}
          {showGridMeta ? <div className="dashboard-grid-meta">Grid: {DASHBOARD_GRID_COLS} x {DASHBOARD_GRID_ROWS}</div> : null}
        </div>
      </section>

      <section
        className="dashboard-virtual-grid"
        style={{
          gridTemplateColumns: `repeat(${DASHBOARD_GRID_COLS}, minmax(0, 1fr))`,
          gridTemplateRows: `repeat(${DASHBOARD_GRID_ROWS}, minmax(0, 1fr))`,
        }}
      >
        {normalizedWidgets.map((widget) => (
          <article
            key={widget.id}
            className="card dashboard-widget-shell"
            style={{
              gridColumn: `${widget.x + 1} / span ${widget.w}`,
              gridRow: `${widget.y + 1} / span ${widget.h}`,
            }}
            draggable={false}
            onDragOver={(e) => {
              if (!canEdit) return;
              e.preventDefault();
            }}
            onDragEnter={() => onDragOverWidget(widget.id)}
            onDrop={onDropOn}
          >
            <div className="dashboard-widget-head">
              <strong>{getWidgetMeta(widget.type)?.label || widget.type}</strong>
              <div className="dashboard-widget-head-actions">
                {canEdit ? (
                  <>
                    <button
                      type="button"
                      className="dashboard-widget-menu-btn dashboard-widget-drag-btn"
                      title="Drag and drop widget"
                      aria-label="Drag and drop widget"
                      draggable={canEdit}
                      onDragStart={(e) => {
                        onDragStart(widget.id);
                        e.dataTransfer.effectAllowed = "move";
                      }}
                      onDragEnd={() => setDraggingId("")}
                    >
                      <MoveCrossIcon />
                    </button>
                    <button
                      type="button"
                      className="dashboard-widget-menu-btn"
                      onClick={() => setMenuWidgetId((prev) => (prev === widget.id ? "" : widget.id))}
                      title="Widget actions"
                      aria-label="Widget actions"
                    >
                      <MenuStackIcon />
                    </button>
                    {menuWidgetId === widget.id ? (
                      <div className="dashboard-widget-actions-pop">
                        <button
                          type="button"
                          className="dashboard-widget-action-icon"
                          onClick={() => {
                            openEdit(widget);
                            setMenuWidgetId("");
                          }}
                          title="Edit"
                          aria-label="Edit widget"
                        >
                          <PencilIcon />
                        </button>
                        <button
                          type="button"
                          className="dashboard-widget-action-icon danger"
                          onClick={() => {
                            removeWidget(widget.id);
                            setMenuWidgetId("");
                          }}
                          title="Delete"
                          aria-label="Delete widget"
                        >
                          <TrashIcon />
                        </button>
                      </div>
                    ) : null}
                  </>
                ) : null}
              </div>
            </div>
            <DashboardWidgetCard
              widget={widget}
              dataLogView={dashboardRows}
              tagRows={tagRows}
              tagRowsByGateway={dashboardTagRowsByGateway}
              formatTagForDisplay={formatTagForDisplay}
            />
          </article>
        ))}
        {!normalizedWidgets.length ? (
          <article className="card dashboard-widget-empty-shell">
            <p>No widgets yet. Click <strong>Add Widget</strong> to build your live dashboard.</p>
          </article>
        ) : null}
      </section>

      {modalOpen ? (
        <div className="modal-backdrop">
          <div className="modal-card dashboard-widget-modal">
            <h3>{editingId ? "Edit Dashboard Widget" : "Add Dashboard Widget"}</h3>
            <div className="dashboard-modal-tabs" role="tablist">
              <button className={`dashboard-pill ${tab === "type" ? "active" : ""}`} onClick={() => setTab("type")} type="button">
                Widget Type
              </button>
              <button className={`dashboard-pill ${tab === "config" ? "active" : ""}`} onClick={() => setTab("config")} type="button">
                Configure
              </button>
            </div>

            {tab === "type" ? (
              <div className="dashboard-type-groups">
                {TYPE_GROUPS.map((group) => (
                  <div key={group} className="dashboard-type-group">
                    <div className="dashboard-type-group-title">{group}</div>
                    <div className="dashboard-type-grid">
                      {WIDGET_TYPES.filter((t) => t.group === group).map((t) => (
                        <button
                          key={t.key}
                          type="button"
                          className={`dashboard-type-btn ${form.type === t.key ? "active" : ""}`}
                          onClick={() => {
                            setForm((prev) => ({
                              ...prev,
                              type: t.key,
                              w: t.defaultSize.w,
                              h: t.defaultSize.h,
                              title: prev.title || t.label,
                            }));
                            setTab("config");
                          }}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="form-grid dashboard-form-grid">
                <label>
                  Title
                  <input value={form.title} onChange={(e) => setForm((p) => ({ ...p, title: e.target.value }))} />
                </label>
                <label>
                  Width (cols)
                  <input
                    type="number"
                    min="1"
                    max={DASHBOARD_GRID_COLS}
                    value={form.w}
                    onChange={(e) => setForm((p) => ({ ...p, w: clamp(e.target.value, 1, DASHBOARD_GRID_COLS) }))}
                  />
                </label>
                <label>
                  Height (rows)
                  <input
                    type="number"
                    min="1"
                    max={DASHBOARD_GRID_ROWS}
                    value={form.h}
                    onChange={(e) => setForm((p) => ({ ...p, h: clamp(e.target.value, 1, DASHBOARD_GRID_ROWS) }))}
                  />
                </label>
                {["line_chart", "line_area_chart", "bar_chart", "meter_chart", "text_kpi", "value_kpi", "pie_chart", "table_list"].includes(form.type) ? (
                  <>
                    <label>
                      Gateway
                      <select
                        value={form.config.gateway_id}
                        onChange={(e) => {
                          const gw = e.target.value;
                          const tags = (gatewayOptions.find((g) => String(g.id) === String(gw))?.tags || []);
                          setForm((p) => ({
                            ...p,
                            config: {
                              ...p.config,
                              gateway_id: gw,
                              tag_name: tags.includes(p.config.tag_name) ? p.config.tag_name : (tags[0] || ""),
                            },
                          }));
                        }}
                      >
                        <option value="">Select gateway</option>
                        {gatewayOptions.map((g) => (
                          <option key={g.id} value={g.id}>{g.name}</option>
                        ))}
                      </select>
                    </label>
                    {["line_chart", "line_area_chart", "bar_chart", "meter_chart", "text_kpi", "value_kpi"].includes(form.type) ? (
                      <label>
                        Tag
                        <select
                          value={form.config.tag_name}
                          onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, tag_name: e.target.value } }))}
                        >
                          <option value="">Select tag</option>
                          {selectedGatewayTags.map((tag) => (
                            <option key={tag} value={tag}>
                              {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}
                  </>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart"].includes(form.type) ? (
                  <label>
                    Reading points
                    <input
                      type="number"
                      min="20"
                      max="500"
                      value={form.config.readings_count}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: { ...p.config, readings_count: clamp(e.target.value, 20, 500) },
                        }))
                      }
                    />
                  </label>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart", "pie_chart", "meter_chart"].includes(form.type) ? (
                  <label>
                    Chart colors
                    <select
                      value={form.config.color_mode}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, color_mode: e.target.value } }))}
                    >
                      <option value="default">Default brand colors</option>
                      <option value="custom">Custom widget color</option>
                    </select>
                  </label>
                ) : null}

                {["line_chart", "line_area_chart", "bar_chart", "pie_chart", "meter_chart"].includes(form.type) &&
                form.config.color_mode === "custom" ? (
                  <label>
                    Custom color
                    <input value={form.color} type="color" onChange={(e) => setForm((p) => ({ ...p, color: e.target.value }))} />
                  </label>
                ) : null}

                {supportsComputed ? (
                  <label>
                    Data source
                    <select
                      value={form.config.data_source_type}
                      onChange={(e) =>
                        setForm((p) => ({
                          ...p,
                          config: {
                            ...p.config,
                            data_source_type: e.target.value === "computed" ? "computed" : "tag_direct",
                          },
                        }))
                      }
                    >
                      <option value="tag_direct">Tag direct</option>
                      <option value="computed">Computed rules</option>
                    </select>
                  </label>
                ) : null}

                {form.type === "table_list" ? (
                  <label>
                    List limit
                    <input
                      type="number"
                      min="1"
                      max="50"
                      value={form.config.list_limit}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, list_limit: clamp(e.target.value, 1, 50) } }))}
                    />
                  </label>
                ) : null}

                {supportsComputed && form.config.data_source_type === "computed" ? (
                  <div className="dashboard-full-row dashboard-rules-wrap">
                    <div className="dashboard-rules-head">
                      <strong>Computed rules</strong>
                      <button type="button" className="dashboard-type-btn" onClick={addComputeRule}>
                        + Add rule
                      </button>
                    </div>
                    <div className="dashboard-rules-list">
                      {(form.config.compute_rules || []).map((rule) => (
                        <div key={rule.id} className="dashboard-rule-row">
                          <input
                            value={rule.label || ""}
                            placeholder="Label"
                            onChange={(e) => updateComputeRule(rule.id, { label: e.target.value })}
                          />
                          <select
                            value={rule.gateway_id || ""}
                            onChange={(e) =>
                              updateComputeRule(rule.id, { gateway_id: e.target.value, tag_name: "" })
                            }
                          >
                            <option value="">Any gateway</option>
                            {gatewayOptions.map((g) => (
                              <option key={g.id} value={g.id}>
                                {g.name}
                              </option>
                            ))}
                          </select>
                          <select
                            value={rule.tag_name || ""}
                            onChange={(e) => updateComputeRule(rule.id, { tag_name: e.target.value })}
                          >
                            <option value="">Any tag</option>
                            {tagsForRuleGateway(rule.gateway_id).map((tag) => (
                              <option key={`${rule.id}-${tag}`} value={tag}>
                                {formatTagForDisplay ? formatTagForDisplay(tag) : tag}
                              </option>
                            ))}
                          </select>
                          <select
                            value={rule.operator || "any"}
                            onChange={(e) => updateComputeRule(rule.id, { operator: e.target.value })}
                          >
                            {RULE_OPERATORS.map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                          <input
                            value={rule.value1 ?? ""}
                            placeholder="Value 1"
                            onChange={(e) => updateComputeRule(rule.id, { value1: e.target.value })}
                          />
                          {String(rule.operator || "any") === "between" ? (
                            <input
                              value={rule.value2 ?? ""}
                              placeholder="Value 2"
                              onChange={(e) => updateComputeRule(rule.id, { value2: e.target.value })}
                            />
                          ) : null}
                          <select
                            value={rule.aggregation || "count"}
                            onChange={(e) => updateComputeRule(rule.id, { aggregation: e.target.value })}
                          >
                            {RULE_AGGREGATIONS.map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                          <input
                            type="color"
                            value={rule.color || "#14a89a"}
                            onChange={(e) => updateComputeRule(rule.id, { color: e.target.value })}
                          />
                          <button
                            type="button"
                            className="dashboard-widget-action-icon danger"
                            onClick={() => removeComputeRule(rule.id)}
                            title="Remove rule"
                          >
                            <TrashIcon />
                          </button>
                        </div>
                      ))}
                      {!form.config.compute_rules?.length ? (
                        <div className="dashboard-config-note">No rules yet. Add one to compute chart/list outputs.</div>
                      ) : null}
                    </div>
                  </div>
                ) : null}

                {form.type === "fixed_text" || form.type === "divider" ? (
                  <label className="dashboard-full-row">
                    Text
                    <input
                      value={form.config.text}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, text: e.target.value } }))}
                    />
                  </label>
                ) : null}

                {form.type === "image" ? (
                  <label className="dashboard-full-row">
                    Image URL
                    <input
                      value={form.config.source_url}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, source_url: e.target.value } }))}
                    />
                  </label>
                ) : null}

                {form.type === "ip_camera" ? (
                  <label className="dashboard-full-row">
                    Camera URL
                    <input
                      value={form.config.camera_url}
                      onChange={(e) => setForm((p) => ({ ...p, config: { ...p.config, camera_url: e.target.value } }))}
                    />
                  </label>
                ) : null}
              </div>
            )}

            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={saveWidget}>Save</button>
              <button className="btn btn-danger" onClick={() => setModalOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}

      {configModalOpen ? (
        <div className="modal-backdrop">
          <div className="modal-card dashboard-config-modal">
            <h3>Dashboard Configuration</h3>
            <div className="dashboard-config-actions">
              <button
                type="button"
                className="dashboard-config-action-btn"
                onClick={exportDashboardConfig}
                title="Download current dashboard configuration"
                aria-label="Download current dashboard configuration"
              >
                <DownloadIcon />
                <span>Export JSON</span>
              </button>
              <button
                type="button"
                className="dashboard-config-action-btn"
                onClick={() => importInputRef.current?.click()}
                title="Select a dashboard configuration file"
                aria-label="Select a dashboard configuration file"
              >
                <UploadIcon />
                <span>Select JSON</span>
              </button>
              <input
                ref={importInputRef}
                type="file"
                accept="application/json,.json"
                className="dashboard-hidden-input"
                onChange={onImportDashboardConfig}
              />
            </div>
            <div className="dashboard-config-note">
              {pendingImportWidgets
                ? `Ready to load: ${pendingImportName}`
                : "Select a JSON file first, then confirm load."}
            </div>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={confirmLoadDashboardConfig} disabled={!pendingImportWidgets}>
                Confirm Load
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  setPendingImportWidgets(null);
                  setPendingImportName("");
                  setConfigModalOpen(false);
                }}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MenuStackIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </svg>
  );
}

function MoveCrossIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2v20" />
      <path d="M2 12h20" />
      <path d="M8 6l4-4 4 4" />
      <path d="M8 18l4 4 4-4" />
      <path d="M6 8l-4 4 4 4" />
      <path d="M18 8l4 4-4 4" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4h6v2" />
    </svg>
  );
}

function AddIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}

function CogIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 1.54V21a2 2 0 0 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15 1.7 1.7 0 0 0 3.06 14H3a2 2 0 0 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3.06V3a2 2 0 0 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.2.32.54.52.91.54H21a2 2 0 0 1 0 4h-.69c-.37.02-.71.22-.91.54z" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" />
      <path d="M7 10l5 5 5-5" />
      <path d="M5 21h14" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 21V9" />
      <path d="M17 14l-5-5-5 5" />
      <path d="M5 3h14" />
    </svg>
  );
}
