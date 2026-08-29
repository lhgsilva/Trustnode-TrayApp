/* OEE — pieces shared by the Overview, Operator and Configuration pages.

   Everything here uses the app's existing classes (.card, .table, .btn,
   .status-pill, .modal-card) and its CSS variables, so OEE inherits dark and
   light mode without a second theme to maintain.
*/
import { useEffect, useMemo, useState } from "react";

/* ------------------------------------------------------------------ states */
export const STATE_LABELS = {
  running: "Running",
  idle: "Idle",
  stopped: "Stopped",
  faulted: "Faulted",
  changeover: "Changeover",
  waiting_material: "Waiting for material",
  waiting_operator: "Waiting for operator",
  planned_stop: "Planned stop",
  off: "Off",
  unknown: "Unknown",
};

/* Downtime states, mirrored from calc.py. Kept in sync by
   scripts/test_oee_module.py, which reads both. */
export const DOWNTIME_STATES = new Set([
  "idle", "stopped", "faulted", "changeover",
  "waiting_material", "waiting_operator", "unknown",
]);

export const SOURCE_LABELS = {
  signal: "PLC / sensor",
  power: "Power meter",
  manual: "Manual",
  combined: "Combined",
};

export const CONFIDENCE_LABELS = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
  conflict: "Signal conflict",
  missing: "Missing data",
};

export const FUNCTION_LABELS = {
  running_status: "Running status",
  stopped_status: "Stopped status",
  idle_status: "Idle status",
  fault_status: "Fault status",
  alarm_code: "Alarm code",
  cycle_start: "Cycle start",
  cycle_stop: "Cycle stop",
  cycle_complete: "Cycle complete",
  total_count: "Total count",
  good_count: "Good count",
  reject_count: "Reject count",
  scrap_count: "Scrap count",
  current_speed: "Current speed",
  product_code: "Product code",
  order_number: "Order number",
};

export const CONDITION_LABELS = {
  truthy: "is true / non-zero",
  falsy: "is false / zero",
  eq: "equals",
  ne: "does not equal",
  gt: "greater than",
  gte: "greater or equal",
  lt: "less than",
  lte: "less or equal",
  rising: "changes 0 → 1 (rising)",
  falling: "changes 1 → 0 (falling)",
  changed: "changes at all",
  stale: "does not change for (hold seconds)",
};

/* ---------------------------------------------------------------- format */
export function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

export function duration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/* A KPI that has no value must say so. "Not enough data" is the honest answer
   and it is what the spec asks for; rendering 0% would report a catastrophe
   where there is simply nothing to measure. */
export function KpiCard({ title, value, sub, tone = "", hint = "" }) {
  const empty = value === "—" || value === null || value === undefined;
  return (
    <div className={`stat-card oee-kpi ${tone ? `oee-kpi-${tone}` : ""}`} title={hint}>
      <div className="stat-title">{title}</div>
      <div className={`stat-value ${empty ? "oee-kpi-empty" : ""}`}>
        {empty ? "Not enough data" : value}
      </div>
      {sub ? <div className="muted oee-kpi-sub">{sub}</div> : null}
    </div>
  );
}

export function StatePill({ state }) {
  const key = String(state || "unknown");
  return (
    <span className={`status-pill oee-state oee-state-${key}`}>
      {STATE_LABELS[key] || key}
    </span>
  );
}

export function ConfidencePill({ confidence, source }) {
  const key = String(confidence || "missing");
  return (
    <span
      className={`oee-conf oee-conf-${key}`}
      title={`${CONFIDENCE_LABELS[key] || key}${source ? ` · source: ${SOURCE_LABELS[source] || source}` : ""}`}
    >
      {CONFIDENCE_LABELS[key] || key}
    </span>
  );
}

/* ------------------------------------------------------- toggle (one row) */
/* The app's dialog convention: a checkbox and its label on ONE row, never two
   — see the density pass in styles.css. */
export function Toggle({ label, checked, onChange, disabled, hint }) {
  return (
    <label className="tn-toggle-field" title={hint || ""}>
      <input
        type="checkbox"
        checked={Boolean(checked)}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

/* ------------------------------------------- gateway → device → tag picker */
/* THE point of the module: OEE never invents its own devices. This picker
   offers only the gateways, devices and tags the collection system already
   has, so a mapping can only ever reference something real. */
export function TagPicker({
  gateways = [], devices = [], value = {}, onChange, disabled,
  tagLabel = "Tag", allowEmptyTag = false,
}) {
  const gatewayId = String(value.gateway_id || "");
  const gateway = useMemo(
    () => gateways.find((g) => String(g.id) === gatewayId) || null,
    [gateways, gatewayId]
  );
  const tags = useMemo(
    () => (gateway && Array.isArray(gateway.tags) ? gateway.tags : []),
    [gateway]
  );
  const device = useMemo(
    () => devices.find((d) => String(d.id) === String(gateway?.device_id || "")) || null,
    [devices, gateway]
  );

  return (
    <div className="oee-tagpicker">
      <label>
        Gateway
        <select
          value={gatewayId}
          disabled={disabled}
          onChange={(e) => {
            const gw = gateways.find((g) => String(g.id) === e.target.value);
            onChange({
              gateway_id: e.target.value,
              device_id: gw?.device_id || "",
              // The old tag belongs to the old gateway; carrying it over is how
              // a mapping ends up pointing at a tag this gateway never reads.
              [tagLabel === "Tag" ? "tag_name" : tagLabel]: "",
            });
          }}
        >
          <option value="">— select a gateway —</option>
          {gateways.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name || g.id} {g.plc_ip ? `(${g.plc_ip})` : ""}
            </option>
          ))}
        </select>
      </label>
      <label>
        Device
        <input
          value={device?.name || gateway?.device_name || "—"}
          disabled
          title="Comes from the gateway you selected — devices are configured on the Devices page."
        />
      </label>
      <label>
        {tagLabel}
        <select
          value={String(value.tag || "")}
          disabled={disabled || !gatewayId}
          onChange={(e) => onChange({ tag: e.target.value })}
        >
          <option value="">{allowEmptyTag ? "— none —" : "— select a tag —"}</option>
          {tags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      {gatewayId && tags.length === 0 ? (
        <div className="info-note oee-span-all">
          This gateway has no tags yet. Add them on Gateway Configuration first —
          OEE can only use values that are actually collected.
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ small table */
export function EmptyState({ title, children }) {
  return (
    <div className="oee-empty">
      <strong>{title}</strong>
      <div className="muted">{children}</div>
    </div>
  );
}

/* A section that can be collapsed, used by the Configuration page so eight
   sections fit on one screen instead of scrolling forever. */
export function Section({ title, subtitle, count, open, onToggle, actions, children }) {
  return (
    <section className="card oee-section">
      <div className="oee-section-head" onClick={onToggle} role="button" tabIndex={0}
           onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onToggle(); }}>
        <span className={`oee-caret ${open ? "open" : ""}`}>▸</span>
        <h3 className="card-title" style={{ margin: 0 }}>{title}</h3>
        {count !== undefined ? <span className="oee-count">{count}</span> : null}
        {subtitle ? <span className="muted oee-section-sub">{subtitle}</span> : null}
        <span className="oee-section-actions" onClick={(e) => e.stopPropagation()}>
          {actions}
        </span>
      </div>
      {open ? <div className="oee-section-body">{children}</div> : null}
    </section>
  );
}

/* Poll helper: keeps a page live without every component writing its own
   interval-and-cleanup dance. */
export function usePoll(fn, intervalMs, deps = []) {
  useEffect(() => {
    let alive = true;
    let timer = null;
    const tick = async () => {
      if (!alive) return;
      try { await fn(); } catch (_) { /* a failed poll must not kill the page */ }
      if (alive) timer = setTimeout(tick, intervalMs);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
