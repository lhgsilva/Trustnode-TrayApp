/* OEE widgets for the main dashboard.

   2026-08-29. Seventeen widget types, all backed by the OEE module's endpoints.

   The rule that shapes this whole file: **no widget computes OEE.** Not the
   product, not availability, not performance, not quality. Two implementations
   of the same formula will disagree, and the one sitting on somebody's
   dashboard is the one nobody can trace back to a number. Every value here
   arrives already calculated, already carrying its maturity and its window.

   One shared loader, because seventeen copies of "fetch, poll, handle empty,
   handle error" is seventeen places for a loading state to be forgotten. */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ResponsiveContainer, LineChart, Line, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend,
} from "recharts";
import {
  oeeOverview, oeeTrend, oeeTimeline, oeeDowntimePareto,
  oeeEnergySummary, oeeShiftPerformance,
} from "../../api";
import { pct, duration, num } from "../OEE/OeeShared";
import { Gauge, ApqBars, MaturityBadge, StatusTimeline, DEFAULT_THRESHOLDS }
  from "../OEE/OeeVisuals";

const DEFAULT_HOURS = 24;
const DEFAULT_REFRESH_MS = 30000;

function cfgOf(widget) {
  const c = widget?.config || {};
  const machineIds = Array.isArray(c.machine_ids) ? c.machine_ids.join(",")
    : String(c.machine_ids || c.machine_id || "");
  return {
    hours: Number(c.hours || DEFAULT_HOURS),
    machineIds,
    line: String(c.line || ""),
    refreshMs: Math.max(5000, Number(c.refresh_ms || DEFAULT_REFRESH_MS)),
    showTarget: c.show_target !== false,
    target: c.target === undefined || c.target === null ? null : Number(c.target),
    thresholds: { ...DEFAULT_THRESHOLDS, ...(c.thresholds || {}) },
    title: String(c.title || ""),
  };
}

/* One loader for every widget: fetch, poll, and report the three states a
   dashboard tile must never conflate - loading, empty, and failed. */
function useOeeData(fetcher, deps, refreshMs) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const run = useCallback(async () => {
    try {
      const res = await fetcher();
      setData(res);
      setError("");
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    let stopped = false;
    const tick = () => { if (!stopped) run(); };
    tick();
    const id = setInterval(tick, refreshMs);
    return () => { stopped = true; clearInterval(id); };
  }, [run, refreshMs]);

  return { data, error, loading };
}

function Shell({ title, loading, error, empty, emptyText, children, meta }) {
  if (loading) return <div className="widget-empty muted">Loading…</div>;
  if (error) return <div className="widget-empty warn">{error}</div>;
  if (empty) return <div className="widget-empty muted">{emptyText || "No data for this period."}</div>;
  return (
    <div className="oee-widget">
      {title ? <div className="oee-widget-title">{title}</div> : null}
      {children}
      {meta ? <div className="muted oee-widget-meta">{meta}</div> : null}
    </div>
  );
}

/* ------------------------------------------------------------- KPI tiles - */
function kpiOf(totals, key) {
  const v = totals?.[key];
  return (v === null || v === undefined) ? null : v;
}

export function OeeKpiWidget({ widget, metric = "oee" }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours, ...(c.machineIds ? { machine_ids: c.machineIds } : {}),
                        ...(c.line ? { line: c.line } : {}) }),
    [c.hours, c.machineIds, c.line], c.refreshMs);

  const totals = data?.totals || {};
  const value = kpiOf(totals, metric);
  const label = { oee: "OEE", availability: "Availability",
                  performance: "Performance", quality: "Quality" }[metric] || metric;
  const goodKey = { oee: "oee_good", availability: "availability_good",
                    performance: "performance_good", quality: "quality_good" }[metric];

  return (
    <Shell title={c.title || label} loading={loading} error={error}
      empty={value === null}
      emptyText={`No ${label.toLowerCase()} for this period — the module may not have enough data yet.`}
      meta={`${totals.machines || 0} machine(s) · last ${c.hours} h`}>
      <div className="oee-widget-kpi">
        <Gauge value={value} size={130} label={label}
          thresholds={{ ...c.thresholds, oee_good: c.thresholds[goodKey] ?? c.thresholds.oee_good }} />
      </div>
    </Shell>
  );
}

/* ------------------------------------------------------- machine widgets - */
export function OeeMachineCardWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours, ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);

  const card = (data?.machines || [])[0];
  const result = (data?.results || []).find((r) => r.machine_id === card?.machine_id) || {};

  return (
    <Shell title={c.title || card?.name || "Machine"} loading={loading} error={error}
      empty={!card} emptyText="Pick a machine in the widget settings.">
      <div className="oee-widget-machine">
        <Gauge value={result.oee ?? null} size={110} label="OEE" thresholds={c.thresholds} />
        <ApqBars availability={result.availability} performance={result.performance}
          quality={result.quality} thresholds={c.thresholds} />
      </div>
      <div className="oee-widget-facts">
        <span><b>{duration(result.runtime_s)}</b> run</span>
        <span><b>{duration(result.downtime_s)}</b> down</span>
        <span><b>{num(result.total_count)}</b> made</span>
      </div>
      <MaturityBadge stage={result.stage || card?.stage}
        missing={result.missing_factors || []} assumption={result.assumption || ""} />
    </Shell>
  );
}

export function OeeMachineStatusWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: 1, ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.machineIds], c.refreshMs);
  const cards = data?.machines || [];
  return (
    <Shell title={c.title || "Machine status"} loading={loading} error={error}
      empty={!cards.length}>
      <div className="oee-widget-states">
        {cards.map((m) => (
          <div key={m.machine_id} className="oee-widget-state-row">
            <span className={`oee-state-dot oee-state-bg-${m.state}`} />
            <span className="oee-widget-state-name">{m.name}</span>
            <span className="oee-widget-state-label">{m.state}</span>
            <span className="muted">{duration(m.current_state_seconds)}</span>
          </div>
        ))}
      </div>
    </Shell>
  );
}

/* ---------------------------------------------------------------- trends - */
function trendRows(buckets) {
  return (buckets || []).map((b) => ({
    t: String(b.from_utc || b.ts_utc || "").slice(11, 16),
    oee: b.oee == null ? null : b.oee * 100,
    availability: b.availability == null ? null : b.availability * 100,
    performance: b.performance == null ? null : b.performance * 100,
    quality: b.quality == null ? null : b.quality * 100,
  }));
}

export function OeeTrendWidget({ widget, apq = false }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeTrend({ hours: c.hours, buckets: 24,
                     ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = useMemo(() => trendRows(data?.buckets), [data]);
  return (
    <Shell title={c.title || (apq ? "A / P / Q trend" : "OEE trend")}
      loading={loading} error={error} empty={!rows.length}>
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <LineChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="t" tick={{ fontSize: 10 }} />
          <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => (v == null ? "—" : `${Number(v).toFixed(1)}%`)} />
          {apq ? <Legend /> : null}
          {apq ? <>
            <Line type="monotone" dataKey="availability" name="Availability" dot={false} connectNulls />
            <Line type="monotone" dataKey="performance" name="Performance" dot={false} connectNulls />
            <Line type="monotone" dataKey="quality" name="Quality" dot={false} connectNulls />
          </> : (
            <Line type="monotone" dataKey="oee" name="OEE" strokeWidth={2} dot={false} connectNulls />
          )}
        </LineChart>
      </ResponsiveContainer>
    </Shell>
  );
}

/* -------------------------------------------------------------- downtime - */
export function OeeDowntimeParetoWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeDowntimePareto({ hours: c.hours,
                              ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.rows || []).map((r) => ({
    name: r.reason || r.category || "unknown",
    minutes: Math.round((r.seconds || 0) / 60),
  }));
  return (
    <Shell title={c.title || "Downtime reasons"} loading={loading} error={error}
      empty={!rows.length} emptyText="No downtime recorded in this period.">
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows} layout="vertical" margin={{ left: 70 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis type="number" unit=" min" tick={{ fontSize: 10 }} />
          <YAxis type="category" dataKey="name" width={110} tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => `${v} min`} />
          <Bar dataKey="minutes" name="Downtime" />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

export function OeeStatusTimelineWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeTimeline({ hours: c.hours,
                        ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const meta = data?.meta?.window || {};
  return (
    <Shell title={c.title || "Machine status timeline"} loading={loading} error={error}
      empty={!(data?.lanes || []).length}
      meta={data?.truncated ? `Truncated at ${data.max_blocks} blocks` : ""}>
      <StatusTimeline lanes={data?.lanes || []} from={meta.from_utc} to={meta.to_utc} height={20} />
    </Shell>
  );
}

/* ----------------------------------------------------- runtime / energy - */
export function OeeRuntimeDowntimeWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours, ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.results || []).map((r) => ({
    name: r.machine_name || r.machine_id,
    runtime: Math.round((r.runtime_s || 0) / 60),
    downtime: Math.round((r.downtime_s || 0) / 60),
  }));
  return (
    <Shell title={c.title || "Runtime vs downtime"} loading={loading} error={error}
      empty={!rows.length}>
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis unit=" min" tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => `${v} min`} />
          <Legend />
          <Bar dataKey="runtime" name="Runtime" stackId="a" />
          <Bar dataKey="downtime" name="Downtime" stackId="a" />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

export function OeeEnergyWidget({ widget, waste = false }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeEnergySummary({ hours: c.hours,
                             ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.rows || []).map((r) => ({
    name: r.machine_name,
    kwh: Number((waste ? r.wasted_kwh : r.total_kwh) || 0),
  }));
  const total = waste ? data?.wasted_kwh : data?.total_kwh;
  return (
    <Shell title={c.title || (waste ? "Energy waste" : "Energy usage")}
      loading={loading} error={error} empty={!rows.length}
      meta={total ? `${num(total, 1)} kWh total` : ""}>
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis unit=" kWh" tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => `${Number(v).toFixed(2)} kWh`} />
          <Bar dataKey="kwh" name={waste ? "Wasted" : "Used"} />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

export function OeeProductionCountWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours, ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.results || []).map((r) => ({
    name: r.machine_name || r.machine_id,
    good: Number(r.good_count || 0),
    reject: Number(r.reject_count || 0),
  }));
  return (
    <Shell title={c.title || "Production"} loading={loading} error={error}
      empty={!rows.length}>
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis tick={{ fontSize: 10 }} />
          <Tooltip />
          <Legend />
          <Bar dataKey="good" name="Good" stackId="p" />
          <Bar dataKey="reject" name="Reject" stackId="p" />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

export function OeeShiftPerformanceWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeShiftPerformance({ hours: c.hours,
                                ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.rows || []).map((r) => ({
    name: r.shift_name, oee: r.oee == null ? null : r.oee * 100,
  }));
  return (
    <Shell title={c.title || "OEE by shift"} loading={loading} error={error}
      empty={!rows.length} emptyText="No shifts configured.">
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => (v == null ? "—" : `${Number(v).toFixed(1)}%`)} />
          <Bar dataKey="oee" name="OEE" />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

export function OeeMachineComparisonWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours, ...(c.machineIds ? { machine_ids: c.machineIds } : {}) }),
    [c.hours, c.machineIds], c.refreshMs);
  const rows = (data?.results || []).map((r) => ({
    name: r.machine_name || r.machine_id,
    oee: r.oee == null ? null : r.oee * 100,
    availability: r.availability == null ? null : r.availability * 100,
    performance: r.performance == null ? null : r.performance * 100,
    quality: r.quality == null ? null : r.quality * 100,
  }));
  return (
    <Shell title={c.title || "Machine comparison"} loading={loading} error={error}
      empty={!rows.length}>
      <ResponsiveContainer width="100%" height="100%" minHeight={140}>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 10 }} />
          <Tooltip formatter={(v) => (v == null ? "—" : `${Number(v).toFixed(1)}%`)} />
          <Legend />
          <Bar dataKey="oee" name="OEE" />
          <Bar dataKey="availability" name="A" />
          <Bar dataKey="performance" name="P" />
          <Bar dataKey="quality" name="Q" />
        </BarChart>
      </ResponsiveContainer>
    </Shell>
  );
}

/* --------------------------------------------------------- data quality - */
/* Which machines can actually produce a trustworthy OEE. On a plant that is
   part-way through commissioning this is the most useful tile on the board:
   it says where to point the next hour of configuration. */
export function OeeDataQualityWidget({ widget }) {
  const c = cfgOf(widget);
  const { data, error, loading } = useOeeData(
    () => oeeOverview({ hours: c.hours }), [c.hours], c.refreshMs);
  const results = data?.results || [];
  const buckets = results.reduce((acc, r) => {
    const k = String(r.stage || "not_configured");
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});
  const rows = Object.entries(buckets);
  return (
    <Shell title={c.title || "OEE readiness"} loading={loading} error={error}
      empty={!rows.length} meta={`${results.length} machine(s)`}>
      <div className="oee-widget-quality">
        {rows.map(([stage, count]) => (
          <div className="oee-widget-quality-row" key={stage}>
            <MaturityBadge stage={stage} />
            <span className="oee-widget-quality-count">{count}</span>
          </div>
        ))}
      </div>
    </Shell>
  );
}
