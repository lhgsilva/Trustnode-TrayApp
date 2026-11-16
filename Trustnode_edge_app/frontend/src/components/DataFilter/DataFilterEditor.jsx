import React, { useEffect, useState } from "react";
import { DATA_FILTER_MAX, DATA_FILTER_OPERATORS, describeDataFilter, newDataFilterCondition } from "./dataFilter.js";

/**
 * The data filter window - one editor for dashboard widgets, report
 * sections and export connections (2026-11-16). Same shell as Computed
 * Rules: a row per condition, ALL / ANY, Clear and Done.
 *
 * props
 *   open, onClose            the window
 *   value, onChange          { logic, conditions } | null  (draft kept here, handed back on Done)
 *   gatewayOptions           [{ id, name, tags: [...] }]
 *   tagsForGateway(id)       tags of one gateway (falls back to gatewayOptions)
 *   formatTagForDisplay(tag) optional
 *   title                    optional
 */
export default function DataFilterEditor({
  open,
  onClose,
  value,
  onChange,
  gatewayOptions = [],
  tagsForGateway = null,
  formatTagForDisplay = null,
  title = "Data filter",
}) {
  const [draft, setDraft] = useState(() => normalise(value));
  useEffect(() => { if (open) setDraft(normalise(value)); }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (!open) return null;

  const tagsOf = (gw) => {
    const viaFn = typeof tagsForGateway === "function" ? (tagsForGateway(gw) || []) : null;
    if (viaFn && viaFn.length) return viaFn;
    const hit = (gatewayOptions || []).find((g) => String(g.id) === String(gw || ""));
    if (hit && Array.isArray(hit.tags) && hit.tags.length) return hit.tags;
    // "same gateway as the data": every tag of every gateway
    const all = new Set();
    for (const g of gatewayOptions || []) {
      for (const t of (typeof tagsForGateway === "function" ? (tagsForGateway(g.id) || []) : (g.tags || []))) all.add(t);
    }
    return Array.from(all);
  };
  const update = (id, patch) => setDraft((d) => ({ ...d, conditions: d.conditions.map((c) => (c.id === id ? { ...c, ...patch } : c)) }));
  const remove = (id) => setDraft((d) => ({ ...d, conditions: d.conditions.filter((c) => c.id !== id) }));
  const add = () => setDraft((d) => (d.conditions.length >= DATA_FILTER_MAX ? d : { ...d, conditions: [...d.conditions, newDataFilterCondition()] }));
  const done = () => {
    const usable = draft.conditions.filter((c) => String(c.tag_name || "").trim() && c.value1 !== "" && (c.operator !== "between" || c.value2 !== ""));
    onChange(usable.length ? { logic: draft.logic, conditions: usable } : null);
    onClose();
  };
  const summary = describeDataFilter(draft, (tag) => (formatTagForDisplay ? formatTagForDisplay(tag) : tag));

  return (
    <div className="modal-backdrop">
      <div className="modal-card dashboard-widget-modal dashboard-computed-modal tn-data-filter-modal">
        <h3>{title}</h3>
        <div className="dashboard-config-note">
          Keep a reading only when these tags, read at the same time, meet the conditions. A tag on another gateway is
          read as its latest value at that time.
        </div>
        <div className="dashboard-rules-wrap">
          <div className="dashboard-rules-head">
            <label className="tn-data-filter-logic">
              <span>Keep a reading when</span>
              <select value={draft.logic} onChange={(e) => setDraft((d) => ({ ...d, logic: e.target.value === "any" ? "any" : "all" }))}>
                <option value="all">all conditions are true (AND)</option>
                <option value="any">any condition is true (OR)</option>
              </select>
            </label>
            <button type="button" className="dashboard-type-btn" onClick={add} disabled={draft.conditions.length >= DATA_FILTER_MAX}>
              + Add condition
            </button>
          </div>
          <div className="dashboard-rules-list">
            {draft.conditions.map((c) => (
              <div key={c.id} className="dashboard-rule-row tn-data-filter-row">
                <select value={c.gateway_id || ""} onChange={(e) => update(c.id, { gateway_id: e.target.value, tag_name: "" })} title="Gateway">
                  <option value="">Same gateway as the data</option>
                  {(gatewayOptions || []).map((g) => (
                    <option key={g.id} value={g.id}>{g.name || g.id}</option>
                  ))}
                </select>
                <input
                  list={`tn-df-tags-${c.id}`}
                  value={c.tag_name || ""}
                  placeholder="Tag"
                  onChange={(e) => update(c.id, { tag_name: e.target.value })}
                  title="Tag (pick one or type its name)"
                />
                <datalist id={`tn-df-tags-${c.id}`}>
                  {tagsOf(c.gateway_id).map((t) => (
                    <option key={`${c.id}-${t}`} value={t}>{formatTagForDisplay ? formatTagForDisplay(t) : t}</option>
                  ))}
                </datalist>
                <select value={c.operator || "eq"} onChange={(e) => update(c.id, { operator: e.target.value })} title="Operator">
                  {DATA_FILTER_OPERATORS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                <input type="number" step="any" value={c.value1 ?? ""} placeholder="Value" onChange={(e) => update(c.id, { value1: e.target.value })} />
                {c.operator === "between" ? (
                  <input type="number" step="any" value={c.value2 ?? ""} placeholder="and" onChange={(e) => update(c.id, { value2: e.target.value })} />
                ) : (
                  <div />
                )}
                <button type="button" className="dashboard-widget-action-icon danger" onClick={() => remove(c.id)} title="Remove condition">
                  ×
                </button>
              </div>
            ))}
            {!draft.conditions.length ? (
              <div className="dashboard-config-note">No conditions: every reading in the time window is used.</div>
            ) : null}
          </div>
          {summary ? <div className="dashboard-config-note tn-data-filter-summary">Filter: {summary}</div> : null}
        </div>
        <div className="row modal-actions">
          <button type="button" className="btn btn-ghost" onClick={() => { onChange(null); onClose(); }}>Clear filter</button>
          <button type="button" className="btn btn-primary" onClick={done}>Done</button>
        </div>
      </div>
    </div>
  );
}

function normalise(value) {
  const src = value && typeof value === "object" ? value : {};
  const list = Array.isArray(src.conditions) ? src.conditions : [];
  return {
    logic: String(src.logic || "all") === "any" ? "any" : "all",
    conditions: list.map((c) => ({ ...newDataFilterCondition(), ...c, id: c.id || newDataFilterCondition().id })),
  };
}
