/* OEE Overview — the parts the page was missing.
 *
 * 2026-08-31, implementing OEE > Overview to spec. The page already had KPI
 * cards, a trend, machine cards, a status timeline and a Pareto; these are the
 * pieces the brief asked for that did not exist yet:
 *
 *   OeeRefreshControl  the refresh interval and the live indicator. NOT a
 *                      whole toolbar: OeePeriodBar already gives Today, a day
 *                      either side, a shift either side and the shift
 *                      selector, and it is shared with Machine Detail
 *   OeeKpiCard         a KPI with a previous-period delta, a sparkline, an
 *                      explanation of how it is calculated, and a maturity tag
 *   OeeTrendChart      selectable legend lines, and a tooltip that carries the
 *                      whole picture rather than one number
 *   OeeDowntimePareto  duration/stops toggle and grouping
 *
 * Named after the existing OEE components (OeeShared, OeeVisuals) rather than
 * the OEE* spelling in the brief, because a second naming convention in one
 * folder costs more than it explains. The existing KpiCard, MachineCard,
 * StatusTimeline, MaturityBadge, StatePill and ConfidencePill are reused, not
 * duplicated — the brief's OEEKPICard, OEEMachineCard,
 * OEEMachineStatusTimeline, OEEDataMaturityBadge, OEEStatusBadge and
 * OEEConfidenceBadge are those components.
 *
 * No OEE arithmetic lives here. Every number arrives from /api/oee; the only
 * maths is a percentage difference between two figures the service returned.
 */
import { useMemo } from "react";
import {
  ResponsiveContainer, ComposedChart, Line, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend, LineChart,
} from "recharts";
import { MaturityBadge, MATURITY_LABELS } from "./OeeVisuals";
import { duration, num } from "./OeeShared";

/* ------------------------------------------------------------- KPI card */

/* How each KPI is calculated, in the operator's words. A number nobody can
   trace is a number nobody trusts, and OEE has more definitions than it has
   letters. */
export const KPI_HINTS = {
  oee: "Availability × Performance × Quality, over the selected window. "
    + "Machines without a figure are left out rather than counted as zero.",
  availability: "Runtime ÷ planned production time. Planned stops do not count "
    + "against it.",
  performance: "Actual output ÷ the theoretical output of the ideal cycle time "
    + "over the same runtime.",
  quality: "Good count ÷ total count.",
  runtime: "Time the machine reported running inside the window.",
  downtime: "Time in a non-running state, planned stops excluded.",
  production: "Total pieces counted, with good and reject split out.",
  energy: "Energy consumed while not producing, priced at the configured "
    + "tariff. Shown only when energy monitoring is configured.",
};

/* A delta against the previous period. Returns null when either side is
   missing, because "0% change" and "we have nothing to compare" are different
   statements and only one of them is honest. */
export function deltaOf(current, previous) {
  const a = Number(current);
  const b = Number(previous);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b === 0) return null;
  return ((a - b) / Math.abs(b)) * 100;
}

export function OeeKpiCard({
  title, value, unit = "", hint = "", tone = "",
  current, previous, previousLabel = "previous period",
  spark = [], maturity = null, note = "",
}) {
  const delta = deltaOf(current, previous);
  const empty = value === null || value === undefined || value === "—";
  const up = delta != null && delta >= 0;
  return (
    <div className={`stat-card oee-kpi ${tone ? `oee-kpi-${tone}` : ""}`}>
      <div className="stat-title" title={hint}>{title}</div>
      <div className={`stat-value ${empty ? "oee-kpi-empty" : ""}`}>
        {/* Never a zero standing in for missing data: a plant reading 0% OEE
            and a plant nobody has configured look identical in a number. */}
        {empty ? (note || "Not enough data") : value}
        {!empty && unit ? <span className="oee-kpi-unit"> {unit}</span> : null}
      </div>

      {delta != null ? (
        <div className={`oee-kpi-delta ${up ? "is-up" : "is-down"}`}
             title={`Compared with the ${previousLabel}`}>
          {up ? "▲" : "▼"} {Math.abs(delta).toFixed(1)}% vs {previousLabel}
        </div>
      ) : (
        <div className="oee-kpi-delta is-none">no comparison available</div>
      )}

      {spark && spark.length > 1 ? (
        <div className="oee-kpi-spark">
          <ResponsiveContainer width="100%" height={18}>
            <LineChart data={spark.map((v, i) => ({ i, v }))}>
              <Line type="monotone" dataKey="v" stroke="currentColor"
                    strokeWidth={1.4} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : null}

      {maturity ? (
        <div className="oee-kpi-maturity">
          <MaturityBadge stage={maturity.stage} missing={maturity.missing || []}
                         assumption={maturity.assumption || ""} />
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------- toolbar */

export const REFRESH_OPTIONS = [
  { value: 0, label: "Manual" },
  { value: 10000, label: "10 s" },
  { value: 30000, label: "30 s" },
  { value: 60000, label: "1 min" },
  { value: 300000, label: "5 min" },
];

/* The refresh interval and the live indicator.
 *
 * Deliberately NOT a full filter toolbar: OeePeriodBar already gives Today,
 * a day either side, a shift either side and the shift selector, and it is
 * shared with Machine Detail and the Operator screen. Duplicating it here
 * would create two toolbars to keep in step. */
export function OeeRefreshControl({
  refreshMs, onRefreshMs, lastUpdated, busy = false,
}) {
  const live = Number(refreshMs) > 0;
  return (
    <div className="oee-refresh">
      <label className="oee-toolbar-field">
        <span>Refresh</span>
        <select value={String(refreshMs)}
                onChange={(e) => onRefreshMs(Number(e.target.value))}>
          {REFRESH_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <span className={`oee-live ${live ? "is-live" : ""}`}
            title={live ? "Refreshing automatically" : "Manual refresh only"}>
        {/* "11:14 PM", not "11:14:54 PM" - seconds on a last-updated stamp
            cost 40px of a row that has to fit. */}
        {busy ? "updating…" : (lastUpdated
          ? String(lastUpdated).replace(/:\d\d(?=(\s|$))/, "") : "—")}
        {live ? <em className="oee-live-dot" /> : null}
      </span>
    </div>
  );
}

/* --------------------------------------------------------- trend chart */

/* The lines the trend can show. Availability, Performance and Quality are off
   by default: four overlapping percentage lines is a picture of nothing. */
export const TREND_SERIES = [
  { key: "oee", label: "Overall OEE", colour: "#22c55e", on: true },
  { key: "availability", label: "Availability", colour: "#38bdf8", on: false },
  { key: "performance", label: "Performance", colour: "#a78bfa", on: false },
  { key: "quality", label: "Quality", colour: "#f59e0b", on: false },
];

function TrendTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  const row = payload[0]?.payload || {};
  const line = (k, v) => (v === null || v === undefined
    ? null
    : <div key={k} className="oee-tt-row"><span>{k}</span><b>{v}</b></div>);
  return (
    <div className="oee-tooltip">
      <div className="oee-tt-time">{label}</div>
      {/* Already percentages by the time they reach here - pct() would
          multiply by 100 a second time and report 7640%. */}
      {line("OEE", row.oee != null ? `${Number(row.oee).toFixed(1)}%` : null)}
      {line("Availability", row.availability != null ? `${Number(row.availability).toFixed(1)}%` : null)}
      {line("Performance", row.performance != null ? `${Number(row.performance).toFixed(1)}%` : null)}
      {line("Quality", row.quality != null ? `${Number(row.quality).toFixed(1)}%` : null)}
      {line("Runtime", row.runtime_s != null ? duration(row.runtime_s) : null)}
      {line("Downtime", row.downtime_s != null ? duration(row.downtime_s) : null)}
      {line("Count", row.total_count != null ? num(row.total_count) : null)}
      {/* The label, not the key: "no_quality" is a database value, not
          something to put in front of a shift supervisor. */}
      {line("Maturity", row.maturity
        ? (MATURITY_LABELS[row.maturity] || row.maturity) : null)}
      {/* How many machines actually produced a figure in this bucket. A dip
          caused by a machine dropping out of the average is a different
          story from a dip caused by the plant slowing down. */}
      {line("Machines", row.machines_total
        ? `${row.machines_counted || 0} of ${row.machines_total}` : null)}
    </div>
  );
}

export function OeeTrendChart({ rows = [], visible = {}, onToggle, height = 260 }) {
  const series = TREND_SERIES.filter((sr) => visible[sr.key]);
  return (
    <>
      <div className="oee-legend">
        {TREND_SERIES.map((sr) => (
          <label key={sr.key} className="oee-legend-item">
            <input type="checkbox" checked={Boolean(visible[sr.key])}
                   onChange={() => onToggle(sr.key)} />
            <span className="oee-legend-dot" style={{ background: sr.colour }} />
            {sr.label}
          </label>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="t" tick={{ fontSize: 10 }} />
          <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
          <Tooltip content={<TrendTooltip />} />
          {series.map((sr) => (
            <Line key={sr.key} type="monotone" dataKey={sr.key} name={sr.label}
                  stroke={sr.colour} strokeWidth={1.8} dot={false}
                  isAnimationActive={false} connectNulls={false} />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </>
  );
}

/* ------------------------------------------------------------- Pareto */

export const PARETO_GROUP_LABELS = {
  reason: "Reason",
  category: "Category",
  machine: "Machine",
  line: "Line",
};

export function OeeDowntimePareto({
  rows = [], metric = "duration", onMetric,
  groupBy = "reason", onGroupBy, groups = ["reason"], onBarClick, height = 230,
}) {
  const byStops = metric === "stops";
  const data = useMemo(() => (rows || []).map((r) => ({
    name: String(r.label || r.reason || "Unknown"),
    value: byStops ? Number(r.stops || 0) : Number(r.seconds || 0) / 60,
    cumulative: (byStops ? Number(r.stops_cumulative || 0) : Number(r.cumulative || 0)) * 100,
    seconds: Number(r.seconds || 0),
    stops: Number(r.stops || 0),
    raw: r,
  })), [rows, byStops]);

  return (
    <>
      <div className="oee-pareto-controls">
        <div className="seg">
          <button type="button" className={!byStops ? "active" : ""}
                  onClick={() => onMetric("duration")}>Duration</button>
          <button type="button" className={byStops ? "active" : ""}
                  onClick={() => onMetric("stops")}>Stops</button>
        </div>
        <label className="oee-toolbar-field">
          <span>Group by</span>
          <select value={groupBy} onChange={(e) => onGroupBy(e.target.value)}>
            {/* Only what the service says it can group by. An event carries no
                product or order, so those are not offered. */}
            {groups.map((g) => (
              <option key={g} value={g}>{PARETO_GROUP_LABELS[g] || g}</option>
            ))}
          </select>
        </label>
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} layout="vertical"
                       margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis type="number" tick={{ fontSize: 10 }}
                 unit={byStops ? "" : "m"} />
          <YAxis type="category" dataKey="name" width={140}
                 tick={{ fontSize: 10 }} />
          <Tooltip
            formatter={(v, k, p) => (k === "value"
              ? [byStops ? `${p.payload.stops} stops` : `${v.toFixed(0)} min`,
                 byStops ? "Stops" : "Duration"]
              : [`${Number(v).toFixed(0)}%`, "Cumulative"])}
          />
          <Legend />
          <Bar dataKey="value" name={byStops ? "Stops" : "Minutes"} fill="#ef4444"
               isAnimationActive={false}
               onClick={(d) => onBarClick && onBarClick(d?.payload?.raw)}
               cursor={onBarClick ? "pointer" : "default"} />
        </ComposedChart>
      </ResponsiveContainer>
    </>
  );
}
