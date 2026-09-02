/* OEE Overview — KPI cards, machine cards, trends and Pareto.

   Reads only /api/oee; every number it shows is derived from data the existing
   gateways already collected.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ResponsiveContainer, ComposedChart, LineChart, Line, Bar, BarChart,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, AreaChart, Area,
} from "recharts";
import { oeeOverview, oeeTrend, oeeList, oeeTimeline, oeeDowntimePareto } from "../../api";
import {
  OeeKpiCard, OeeRefreshControl, OeeTrendChart, OeeDowntimePareto, KPI_HINTS,
  TREND_SERIES,
} from "./OeeOverviewParts";
import OeePeriodBar, { resolveWindow } from "./OeePeriod";
import { Gauge, ApqBars, MaturityBadge, StatusTimeline, DEFAULT_THRESHOLDS }
  from "./OeeVisuals";
import {
  KpiCard, StatePill, ConfidencePill, pct, duration, num, EmptyState,
  SOURCE_LABELS, usePoll,
} from "./OeeShared";

function chartTime(iso) {
  const t = String(iso || "").slice(5, 16).replace("T", " ");
  return t;
}

export default function OeeOverviewPage({
  canEdit = false, selection, onSelectionChange, onOpenMachine,
  thresholds = DEFAULT_THRESHOLDS,
}) {
  const [machineFilter, setMachineFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [data, setData] = useState(null);
  const [trend, setTrend] = useState([]);
  const [shifts, setShifts] = useState([]);
  const [lanes, setLanes] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  //: How often the page re-reads. "Manual" (0) is a real choice on a plant
  //: screen that is being watched rather than driven.
  const [refreshMs, setRefreshMs] = useState(10000);
  const [lastUpdated, setLastUpdated] = useState("");
  //: Which trend lines are drawn. Four overlapping percentage lines is a
  //: picture of nothing, so only Overall OEE starts on.
  const [trendVisible, setTrendVisible] = useState(() => {
    const out = {};
    TREND_SERIES.forEach((sr) => { out[sr.key] = sr.on; });
    return out;
  });
  const [paretoMetric, setParetoMetric] = useState("duration");
  const [paretoGroup, setParetoGroup] = useState("reason");
  const [paretoData, setParetoData] = useState(null);

  const win = useMemo(
    () => (selection ? resolveWindow(selection, shifts) : null),
    [selection, shifts]
  );

  // Trend resolution follows the window the user chose. It used to come from
  // a separate "Range" dropdown that did not set the window, so the two could
  // disagree and the dropdown was the one that looked authoritative.
  const buckets = useMemo(() => {
    if (!win) return 24;
    const hours = (Date.parse(`${win.to}Z`) - Date.parse(`${win.from}Z`)) / 3600000;
    if (!Number.isFinite(hours) || hours <= 0) return 24;
    if (hours <= 8) return 16;
    if (hours <= 24) return 24;
    if (hours <= 168) return 28;
    return 30;
  }, [win]);

  // The window comes from the SHARED selection when the page is given one, so
  // the period survives navigation into Machine Detail and back. Falling back
  // to the local range keeps the page usable if it is ever mounted alone.

  const load = useCallback(async () => {
    try {
      const params = win
        ? { from_utc: win.from, to_utc: win.to }
        : { hours: 24 };
      const effLine = selection?.line || "";
      if (machineFilter) params.machine_ids = machineFilter;
      if (effLine) params.line = effLine;
      const [ov, tr, tl, pa] = await Promise.all([
        // compare=1 brings the previous equal-length window, which is what the
        // KPI deltas are measured against. The page does not compute it.
        oeeOverview({ ...params, compare: 1 }),
        oeeTrend({ ...params, buckets }),
        oeeTimeline(params).catch(() => ({ lanes: [] })),
        oeeDowntimePareto({ ...params, group_by: paretoGroup, metric: paretoMetric })
          .catch(() => null),
      ]);
      setData(ov);
      setTrend(Array.isArray(tr?.buckets) ? tr.buckets : []);
      setLanes(Array.isArray(tl?.lanes) ? tl.lanes : []);
      setParetoData(pa);
      setLastUpdated(new Date().toLocaleTimeString());
      setError("");
    } catch (e) {
      setError(String(e?.message || e));
    }
  }, [win, buckets, machineFilter, selection, paretoGroup, paretoMetric]);

  useEffect(() => { setBusy(true); load().finally(() => setBusy(false)); }, [load]);
  // The machine cards are live data; 10 s keeps them current without making
  // the page a poller (the heavy result maths is in the same call).
  // 0 = manual. usePoll always re-arms, so a very long interval stands in for
  // "do not poll" without teaching the hook a second mode.
  usePoll(load, refreshMs > 0 ? refreshMs : 24 * 60 * 60 * 1000, [load, refreshMs]);

  useEffect(() => {
    oeeList("shifts").then((r) => setShifts(r.items || [])).catch(() => {});
  }, []);

  const totals = data?.totals || {};
  //: The service computed this over the equal-length window before this one.
  //: The page only subtracts; it does not recompute OEE.
  const prevTotals = (data?.previous?.totals) || {};
  const prevLabel = win?.shiftLabel ? "previous shift" : "previous period";
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
    // Lower-case keys drive the selectable lines and the rich tooltip; the
    // capitalised ones remain for the other charts on this page.
    oee: b.oee === null ? null : Number((b.oee * 100).toFixed(1)),
    availability: b.availability === null ? null : Number((b.availability * 100).toFixed(1)),
    performance: b.performance === null ? null : Number((b.performance * 100).toFixed(1)),
    quality: b.quality === null ? null : Number((b.quality * 100).toFixed(1)),
    runtime_s: b.runtime_s,
    downtime_s: b.downtime_s,
    total_count: b.total,
    maturity: b.maturity || b.stage || "",
    machines_counted: b.machines_counted,
    machines_total: b.machines_total,
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

  //: How complete the plant figure is - the service's own roll-up, not a
  //: guess made here. A plant averaging four instrumented machines out of
  //: nine must not present that average as if all nine reported.
  //: A sparkline is the trend the page already has, not a second query.
  const sparkOf = useCallback((key) => (trendRows || [])
    .map((r) => r[key])
    .filter((v) => v !== null && v !== undefined), [trendRows]);

  const plantMaturity = useMemo(() => {
    const t = data?.totals || {};
    if (!t.stage) return null;
    const counted = Number(t.machines_counted);
    const all = Number(t.machines_total);
    const short = Number.isFinite(counted) && Number.isFinite(all) && counted < all
      ? `${counted} of ${all} machines have a complete figure`
      : "";
    return { stage: t.stage, missing: t.missing_factors || [], assumption: short };
  }, [data]);

  //: The dedicated endpoint answers with the chosen grouping and ranking, and
  //: carries BOTH seconds and stops so the toggle needs no round trip. The
  //: overview's own pareto is the fallback for an edge that has not restarted
  //: into a build with the grouped endpoint.
  const paretoRows = useMemo(() => {
    const rows = paretoData?.rows;
    if (Array.isArray(rows) && rows.length) return rows;
    return (data?.pareto || []).map((p) => ({
      label: p.reason, category: p.category, reason: p.reason,
      seconds: p.seconds, stops: p.stops || 0,
      share: p.share, cumulative: p.cumulative,
    }));
  }, [paretoData, data]);

  const noMachines = data && (data.machines || []).length === 0;

  return (
    <div className="oee-page">
      {/* --------------------------------------------------- one toolbar */}
      {/* The period bar owns the window, the day, the shift and the line -
          it is shared with Machine Detail, so it is the one that has to be
          right. These two narrow the machine CARDS and are the only filters
          the page ever really had. */}
      <div className="oee-period-row">
        <OeePeriodBar selection={selection} onChange={onSelectionChange}
          shifts={shifts} machines={data?.machines || []} />
        {/* The period bar alone fills the row at 1366px. Machine and Source
            narrow the CARDS rather than the query, so they sit behind a
            disclosure - the same pattern as Machine Detail - and the default
            view spends one line on controls instead of two.
            A dot marks the button when a filter is actually applied, so a
            hidden filter can never quietly change what is on screen. */}
        <button type="button"
                className={`btn btn-secondary btn-sm${showFilters ? " active" : ""}`}
                aria-expanded={showFilters}
                onClick={() => setShowFilters((v) => !v)}>
          Filters{(machineFilter || sourceFilter) ? " •" : ""}
        </button>
        <OeeRefreshControl refreshMs={refreshMs} onRefreshMs={setRefreshMs}
                           lastUpdated={lastUpdated} busy={busy} />
      </div>

      {showFilters ? (
        <div className="oee-filter-extra">
          <label className="oee-toolbar-field">
            <span>Machine</span>
            <select value={machineFilter}
                    onChange={(e) => setMachineFilter(e.target.value)}>
              <option value="">All machines</option>
              {(data?.machines || []).map((m) => (
                <option key={m.machine_id} value={m.machine_id}>{m.name}</option>
              ))}
            </select>
          </label>
          <label className="oee-toolbar-field">
            <span>Source</span>
            <select value={sourceFilter}
                    onChange={(e) => setSourceFilter(e.target.value)}>
              <option value="">Any source</option>
              {Object.entries(SOURCE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </label>
          {(machineFilter || sourceFilter) ? (
            <button type="button" className="btn btn-secondary btn-sm"
                    onClick={() => { setMachineFilter(""); setSourceFilter(""); }}>
              Clear
            </button>
          ) : null}
        </div>
      ) : null}

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
          <OeeKpiCard title="Overall OEE" value={pct(totals.oee)} tone="primary"
                      hint={KPI_HINTS.oee}
                      current={totals.oee} previous={prevTotals.oee}
                      previousLabel={prevLabel}
                      spark={sparkOf("oee")} maturity={plantMaturity} />
          <OeeKpiCard title="Availability" value={pct(totals.availability)}
                      hint={KPI_HINTS.availability}
                      current={totals.availability} previous={prevTotals.availability}
                      previousLabel={prevLabel}
                      spark={sparkOf("availability")} maturity={plantMaturity} />
          <OeeKpiCard title="Performance" value={pct(totals.performance)}
                      hint={KPI_HINTS.performance}
                      current={totals.performance} previous={prevTotals.performance}
                      previousLabel={prevLabel}
                      spark={sparkOf("performance")} maturity={plantMaturity} />
          <OeeKpiCard title="Quality" value={pct(totals.quality)}
                      hint={KPI_HINTS.quality}
                      current={totals.quality} previous={prevTotals.quality}
                      previousLabel={prevLabel}
                      spark={sparkOf("quality")} maturity={plantMaturity} />
          <OeeKpiCard title="Runtime" value={duration(totals.runtime_s)}
                      hint={KPI_HINTS.runtime}
                      current={totals.runtime_s} previous={prevTotals.runtime_s}
                      previousLabel={prevLabel} />
          <OeeKpiCard title="Downtime" value={duration(totals.downtime_s)} tone="warn"
                      hint={KPI_HINTS.downtime}
                      current={totals.downtime_s} previous={prevTotals.downtime_s}
                      previousLabel={prevLabel} />
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
              const openable = typeof onOpenMachine === "function";
              return (
                <div key={m.machine_id}
                     className={`oee-machine-card oee-mc-${m.state}`
                       + (openable ? " oee-mc-clickable" : "")}
                     role={openable ? "button" : undefined}
                     tabIndex={openable ? 0 : undefined}
                     title={openable ? `Open ${m.name}` : undefined}
                     onClick={openable ? () => onOpenMachine(m) : undefined}
                     onKeyDown={openable ? (e) => {
                       // A card that only responds to a mouse is unusable on a
                       // control-room panel driven by keyboard or touch.
                       if (e.key === "Enter" || e.key === " ") {
                         e.preventDefault();
                         onOpenMachine(m);
                       }
                     } : undefined}>
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

                  <div className="oee-mc-gauge-row">
                    <Gauge value={result.oee ?? null} size={84} label="OEE"
                      thresholds={thresholds} />
                    <ApqBars availability={result.availability}
                      performance={result.performance} quality={result.quality}
                      thresholds={thresholds} />
                  </div>
                  <MaturityBadge stage={result.stage || m.stage}
                    missing={result.missing_factors || []}
                    assumption={result.assumption || ""} />

                  <div className="oee-mc-grid">
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

      {/* ------------------------------------------- status timeline (all) */}
      {lanes.length ? (
        <section className="card">
          <h3 className="card-title">Machine status through the period</h3>
          <StatusTimeline lanes={lanes} from={win?.from} to={win?.to} height={24} />
        </section>
      ) : null}

      {/* -------------------------------------------------------- charts */}
      <div className="oee-chart-grid">
        <section className="card">
          <h3 className="card-title">OEE trend</h3>
          {/* Selectable lines: Overall OEE on, the three factors available but
              off, because four overlapping percentage lines is a picture of
              nothing. The tooltip carries the whole bucket - runtime, count,
              maturity, confidence - not just the number under the cursor. */}
          <OeeTrendChart
            rows={trendRows}
            visible={trendVisible}
            onToggle={(key) => setTrendVisible((p) => ({ ...p, [key]: !p[key] }))}
          />
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
          {(paretoRows || []).length ? (
            <OeeDowntimePareto
              rows={paretoRows}
              metric={paretoMetric}
              onMetric={setParetoMetric}
              groupBy={paretoGroup}
              onGroupBy={setParetoGroup}
              // Only the groupings the service says the data supports. An
              // event has no product or order column, so those never appear.
              groups={paretoData?.groups_supported || ["reason"]}
              onBarClick={(row) => {
                // Narrow the page to that reason's machine where the grouping
                // makes that meaningful; otherwise leave the selection alone
                // rather than pretend the click did something.
                if (paretoGroup !== "machine" || !row?.label) return;
                // onOpenMachine takes the whole card - the same object the
                // machine grid hands it - not an id.
                const hit = (data?.machines || []).find(
                  (m) => String(m.name) === String(row.label)
                      || String(m.machine_id) === String(row.label));
                if (hit && onOpenMachine) onOpenMachine(hit);
              }}
            />
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
