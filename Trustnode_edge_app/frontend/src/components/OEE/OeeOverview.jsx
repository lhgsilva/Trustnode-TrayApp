/* OEE Overview — KPI cards, machine cards, trends and Pareto.

   Reads only /api/oee; every number it shows is derived from data the existing
   gateways already collected.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ResponsiveContainer, ComposedChart, LineChart, Line, Bar, BarChart,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, AreaChart, Area,
} from "recharts";
import { oeeOverview, oeeTrend, oeeList } from "../../api";
import {
  KpiCard, StatePill, ConfidencePill, pct, duration, num, EmptyState,
  SOURCE_LABELS, usePoll,
} from "./OeeShared";

const RANGES = [
  { id: "8", label: "Last 8 h", hours: 8, buckets: 16 },
  { id: "24", label: "Last 24 h", hours: 24, buckets: 24 },
  { id: "168", label: "Last 7 days", hours: 168, buckets: 28 },
  { id: "720", label: "Last 30 days", hours: 720, buckets: 30 },
];

function chartTime(iso) {
  const t = String(iso || "").slice(5, 16).replace("T", " ");
  return t;
}

export default function OeeOverviewPage({ canEdit = false }) {
  const [rangeId, setRangeId] = useState("24");
  const [machineFilter, setMachineFilter] = useState("");
  const [lineFilter, setLineFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [data, setData] = useState(null);
  const [trend, setTrend] = useState([]);
  const [shifts, setShifts] = useState([]);
  const [shiftFilter, setShiftFilter] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const range = useMemo(
    () => RANGES.find((r) => r.id === rangeId) || RANGES[1], [rangeId]);

  const load = useCallback(async () => {
    try {
      const params = { hours: range.hours };
      if (machineFilter) params.machine_ids = machineFilter;
      if (lineFilter) params.line = lineFilter;
      const [ov, tr] = await Promise.all([
        oeeOverview(params),
        oeeTrend({ hours: range.hours, buckets: range.buckets,
                   ...(machineFilter ? { machine_ids: machineFilter } : {}) }),
      ]);
      setData(ov);
      setTrend(Array.isArray(tr?.buckets) ? tr.buckets : []);
      setError("");
    } catch (e) {
      setError(String(e?.message || e));
    }
  }, [range, machineFilter, lineFilter]);

  useEffect(() => { setBusy(true); load().finally(() => setBusy(false)); }, [load]);
  // The machine cards are live data; 10 s keeps them current without making
  // the page a poller (the heavy result maths is in the same call).
  usePoll(load, 10000, [load]);

  useEffect(() => {
    oeeList("shifts").then((r) => setShifts(r.items || [])).catch(() => {});
  }, []);

  const totals = data?.totals || {};
  const machines = useMemo(() => {
    let rows = data?.machines || [];
    if (sourceFilter) rows = rows.filter((m) => m.status_source === sourceFilter);
    return rows;
  }, [data, sourceFilter]);

  const lines = useMemo(() => {
    const set = new Set((data?.machines || []).map((m) => m.line).filter(Boolean));
    return Array.from(set);
  }, [data]);

  const trendRows = useMemo(() => trend.map((b) => ({
    t: chartTime(b.bucket_start_utc),
    OEE: b.oee === null ? null : Number((b.oee * 100).toFixed(1)),
    Availability: b.availability === null ? null : Number((b.availability * 100).toFixed(1)),
    Performance: b.performance === null ? null : Number((b.performance * 100).toFixed(1)),
    Quality: b.quality === null ? null : Number((b.quality * 100).toFixed(1)),
    Runtime: Number((b.runtime_s / 3600).toFixed(2)),
    Downtime: Number((b.downtime_s / 3600).toFixed(2)),
    Energy: Number((b.energy_kwh || 0).toFixed(2)),
    Wasted: Number((b.energy_wasted_kwh || 0).toFixed(2)),
    Produced: Number(b.total || 0),
    Rejects: Number(b.reject || 0),
  })), [trend]);

  const pareto = useMemo(() => (data?.pareto || []).map((p) => ({
    name: `${p.reason}`,
    category: p.category,
    Minutes: Number((p.seconds / 60).toFixed(1)),
    Cumulative: Number((p.cumulative * 100).toFixed(1)),
  })), [data]);

  const noMachines = data && (data.machines || []).length === 0;

  return (
    <div className="oee-page">
      {/* ------------------------------------------------------- filters */}
      <section className="card oee-filters">
        <div className="oee-filter-row">
          <label>
            Range
            <select value={rangeId} onChange={(e) => setRangeId(e.target.value)}>
              {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            </select>
          </label>
          <label>
            Line
            <select value={lineFilter} onChange={(e) => setLineFilter(e.target.value)}>
              <option value="">All lines</option>
              {lines.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </label>
          <label>
            Machine
            <select value={machineFilter} onChange={(e) => setMachineFilter(e.target.value)}>
              <option value="">All machines</option>
              {(data?.machines || []).map((m) => (
                <option key={m.machine_id} value={m.machine_id}>{m.name}</option>
              ))}
            </select>
          </label>
          <label>
            Shift
            <select value={shiftFilter} onChange={(e) => setShiftFilter(e.target.value)}>
              <option value="">All shifts</option>
              {shifts.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <label>
            Data source
            <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
              <option value="">Any source</option>
              {Object.entries(SOURCE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </label>
          <button type="button" className="btn btn-secondary btn-sm"
                  onClick={load} disabled={busy}>
            {busy ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {shiftFilter ? (
          <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
            Shift filtering narrows planned production time; machines outside the
            shift show as “Not enough data”.
          </div>
        ) : null}
      </section>

      {error ? <div className="error">{error}</div> : null}

      {noMachines ? (
        <section className="card">
          <EmptyState title="No machines configured yet">
            Open <strong>OEE → Configuration</strong> and add a machine. Point it
            at a gateway and tag you already collect, and this page fills itself in.
          </EmptyState>
        </section>
      ) : null}

      {/* ---------------------------------------------------------- KPIs */}
      <section className="card">
        <h3 className="card-title">Overall</h3>
        <div className="power-kpi-grid oee-kpi-grid">
          <KpiCard title="OEE" value={pct(totals.oee)} tone="primary"
                   sub="Availability × Performance × Quality" />
          <KpiCard title="Availability" value={pct(totals.availability)}
                   sub="Runtime / planned time" />
          <KpiCard title="Performance" value={pct(totals.performance)}
                   sub="Ideal cycle × count / runtime" />
          <KpiCard title="Quality" value={pct(totals.quality)}
                   sub="Good / total" />
          <KpiCard title="Runtime" value={duration(totals.runtime_s)} />
          <KpiCard title="Downtime" value={duration(totals.downtime_s)} tone="warn" />
          <KpiCard title="Total produced" value={num(totals.total_count)} />
          <KpiCard title="Good" value={num(totals.good_count)} />
          <KpiCard title="Rejects" value={num(totals.reject_count)} tone="warn" />
          <KpiCard title="Power now"
                   value={totals.power_kw_now ? `${num(totals.power_kw_now, 2)} kW` : "—"} />
          <KpiCard title="Energy used"
                   value={totals.energy_kwh ? `${num(totals.energy_kwh, 1)} kWh` : "—"} />
          <KpiCard title="Estimated waste"
                   value={totals.energy_wasted_kwh ? `${num(totals.energy_wasted_kwh, 1)} kWh` : "—"}
                   tone="warn"
                   hint="Energy used above the configured standby/idle allowance while not producing." />
        </div>
      </section>

      {/* ------------------------------------------------- machine cards */}
      {machines.length ? (
        <section className="card">
          <h3 className="card-title">Machines</h3>
          <div className="oee-machine-grid">
            {machines.map((m) => {
              const result = (data?.results || []).find(
                (r) => r.machine_id === m.machine_id) || {};
              return (
                <div key={m.machine_id}
                     className={`oee-machine-card oee-mc-${m.state}`}>
                  <div className="oee-mc-head">
                    <strong>{m.name}</strong>
                    <StatePill state={m.state} />
                  </div>
                  <div className="muted oee-mc-line">
                    {[m.line, m.area].filter(Boolean).join(" · ") || "—"}
                  </div>

                  <div className="oee-mc-conf">
                    <ConfidencePill confidence={m.confidence} source={m.status_source} />
                    <span className="muted">{SOURCE_LABELS[m.status_source] || m.status_source}</span>
                  </div>
                  {m.flags?.length ? (
                    <div className="oee-mc-flags">
                      {m.flags.map((f) => (
                        <span key={f} className={`oee-flag oee-flag-${f}`}>
                          {f === "energy_waste" ? "Energy waste"
                            : f === "blocked" ? "Running, no output"
                            : f === "conflict" ? "Signal conflict" : f}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  <div className="oee-mc-grid">
                    <div><span className="muted">OEE</span><strong>{pct(result.oee)}</strong></div>
                    <div><span className="muted">Runtime</span><strong>{duration(result.runtime_s)}</strong></div>
                    <div><span className="muted">Downtime</span><strong>{duration(result.downtime_s)}</strong></div>
                    <div><span className="muted">In this state</span><strong>{duration(m.current_state_seconds)}</strong></div>
                    <div><span className="muted">Power</span><strong>{m.power_kw != null ? `${num(m.power_kw, 2)} kW` : "—"}</strong></div>
                    <div><span className="muted">Produced</span><strong>{num(result.total_count)}</strong></div>
                  </div>

                  <div className="oee-mc-foot muted">
                    {m.needs_reason ? (
                      <span className="oee-flag oee-flag-needs-reason">Reason not set</span>
                    ) : m.downtime_category ? (
                      <span>Reason: {m.downtime_category}</span>
                    ) : null}
                    {m.order_number ? <span>Order {m.order_number}</span> : null}
                    {m.product_code ? <span>Product {m.product_code}</span> : null}
                    {m.cycle ? <span>Cycle open</span> : null}
                  </div>
                  {m.detail ? (
                    <div className="muted oee-mc-detail" title={m.detail}>{m.detail}</div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* -------------------------------------------------------- charts */}
      <div className="oee-chart-grid">
        <section className="card">
          <h3 className="card-title">OEE trend</h3>
          <ResponsiveContainer width="100%" height={230}>
            <LineChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
              <XAxis dataKey="t" tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="OEE" stroke="#22c55e" dot={false}
                    strokeWidth={2} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </section>

        <section className="card">
          <h3 className="card-title">Availability · Performance · Quality</h3>
          <ResponsiveContainer width="100%" height={230}>
            <LineChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
              <XAxis dataKey="t" tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="Availability" stroke="#38bdf8" dot={false} connectNulls />
              <Line type="monotone" dataKey="Performance" stroke="#a855f7" dot={false} connectNulls />
              <Line type="monotone" dataKey="Quality" stroke="#f59e0b" dot={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </section>

        <section className="card">
          <h3 className="card-title">Runtime vs downtime (hours)</h3>
          <ResponsiveContainer width="100%" height={230}>
            <BarChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
              <XAxis dataKey="t" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Bar dataKey="Runtime" stackId="a" fill="#22c55e" />
              <Bar dataKey="Downtime" stackId="a" fill="#ef4444" />
            </BarChart>
          </ResponsiveContainer>
        </section>

        <section className="card">
          <h3 className="card-title">Downtime Pareto</h3>
          {pareto.length ? (
            <ResponsiveContainer width="100%" height={230}>
              <ComposedChart data={pareto}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
                <XAxis dataKey="name" tick={{ fontSize: 10 }} interval={0}
                       angle={-18} textAnchor="end" height={60} />
                <YAxis yAxisId="l" tick={{ fontSize: 11 }} unit="m" />
                <YAxis yAxisId="r" orientation="right" domain={[0, 100]}
                       unit="%" tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend />
                <Bar yAxisId="l" dataKey="Minutes" fill="#ef4444" />
                <Line yAxisId="r" type="monotone" dataKey="Cumulative"
                      stroke="#f59e0b" dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No downtime recorded in this range">
              Stops appear here once machines report a non-running state.
            </EmptyState>
          )}
        </section>

        <section className="card">
          <h3 className="card-title">Power usage and waste (kWh)</h3>
          <ResponsiveContainer width="100%" height={230}>
            <AreaChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
              <XAxis dataKey="t" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Area type="monotone" dataKey="Energy" stroke="#38bdf8"
                    fill="#38bdf8" fillOpacity={0.18} />
              <Area type="monotone" dataKey="Wasted" stroke="#ef4444"
                    fill="#ef4444" fillOpacity={0.25} />
            </AreaChart>
          </ResponsiveContainer>
        </section>

        <section className="card">
          <h3 className="card-title">Production and rejects</h3>
          <ResponsiveContainer width="100%" height={230}>
            <ComposedChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
              <XAxis dataKey="t" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Bar dataKey="Produced" fill="#22c55e" />
              <Line type="monotone" dataKey="Rejects" stroke="#ef4444" dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </section>
      </div>
    </div>
  );
}
