/* OEE > Overview > Machine Detail — one machine, one period.

   2026-08-29, rebuilt 2026-08-31 to the Machine Detail brief. Opened by
   clicking a machine card on the Overview. It stays INSIDE the OEE module:
   the period, shift and filters are handed in, so selecting Shift 2 on a
   given day and clicking Machine 3 opens Machine 3 for that shift on that
   day rather than resetting to "last 24 hours".

   The downtime record at the bottom is the point of the page. Everything
   above it says how the machine did; the timeline and the events table say
   WHEN, and that is where an operator attaches the reason.

   Every number comes from /api/oee. The only arithmetic here is picking a
   sparkline out of the trend the service already returned. */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  oeeMachineResult, oeeMachineEvents, oeeTimeline, oeeDowntimePareto, oeeTrend,
  oeeMachinesLive, oeeList, oeeConfirmDowntime, oeeAddCount, oeeAddQuality,
  oeeCycleStart, oeeCycleStop,
} from "../../api";
import {
  pct, duration, num, EmptyState, Section, usePoll,
} from "./OeeShared";
import { StatusTimeline } from "./OeeVisuals";
import OeePeriodBar, { resolveWindow } from "./OeePeriod";
import {
  OeeKpiCard, OeeRefreshControl, OeeTrendChart, OeeDowntimePareto,
  KPI_HINTS, TREND_SERIES,
} from "./OeeOverviewParts";
import {
  OeeMachineHeader, OeeQuickActionsMenu, OeeProductionChart,
  OeeDowntimeEventsTable, OeeDowntimeEventModal,
} from "./OeeMachineDetailParts";

function chartTime(stamp) {
  const s = String(stamp || "");
  return s.length >= 16 ? s.slice(11, 16) : s;
}

/* The brief asks for one tab in this first version. It is still a tab strip,
   because Timeline / Downtime / Production / Quality / Energy are named as the
   next ones and a strip that grows is less disruptive than a strip that
   appears. */
const TABS = [{ id: "summary", label: "Summary" }];

export default function OeeMachineDetailPage({
  machine, selection, onSelectionChange, machines = [],
  onBack, canEdit = false,
}) {
  /* The shifts are fetched here rather than handed down. Without them
     resolveWindow cannot apply a shift's HH:MM boundaries, so a selection
     carrying shiftId would quietly widen to the whole day - the page would
     claim to be showing Shift 2 and show 24 hours. */
  const [shifts, setShifts] = useState([]);
  const [live, setLive] = useState(null);
  const [result, setResult] = useState(null);
  const [previous, setPrevious] = useState(null);
  const [events, setEvents] = useState([]);
  const [lanes, setLanes] = useState([]);
  const [paretoData, setParetoData] = useState(null);
  const [trend, setTrend] = useState([]);
  const [reasons, setReasons] = useState([]);
  const [orders, setOrders] = useState([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState("");

  const [tab, setTab] = useState("summary");
  const [showFilters, setShowFilters] = useState(false);
  const [refreshMs, setRefreshMs] = useState(30000);
  const [trendVisible, setTrendVisible] = useState(
    () => Object.fromEntries(TREND_SERIES.map((s) => [s.key, s.on])));
  const [paretoMetric, setParetoMetric] = useState("duration");
  const [paretoGroup, setParetoGroup] = useState("reason");

  const [openEvent, setOpenEvent] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [notice, setNotice] = useState("");

  const machineId = String(machine?.machine_id || machine?.id || "");
  const win = useMemo(() => resolveWindow(selection, shifts), [selection, shifts]);

  const load = useCallback(async () => {
    if (!machineId) return;
    setBusy(true);
    setError("");
    const params = { from_utc: win.from, to_utc: win.to };
    try {
      const [res, evs, tl, par, tr, lv] = await Promise.all([
        // compare=1 asks the service for the equal-length window before this
        // one. The KPI deltas are then a difference between two figures the
        // service produced, not a second opinion worked out in the browser.
        oeeMachineResult(machineId, { ...params, compare: 1 })
          .catch((e) => ({ __err: String(e?.message || e) })),
        oeeMachineEvents(machineId, params).catch(() => ({ events: [] })),
        oeeTimeline({ ...params, machine_ids: machineId }).catch(() => ({ lanes: [] })),
        oeeDowntimePareto({
          ...params, machine_ids: machineId,
          group_by: paretoGroup, metric: paretoMetric,
        }).catch(() => null),
        oeeTrend({ ...params, machine_ids: machineId, buckets: 24 })
          .catch(() => ({ buckets: [] })),
        oeeMachinesLive().catch(() => ({ machines: [] })),
      ]);
      if (res?.__err) setError(res.__err);
      setResult(res?.result || null);
      setPrevious(res?.previous?.result || null);
      setEvents(evs?.events || evs?.items || []);
      setLanes(tl?.lanes || []);
      setParetoData(par);
      setTrend(tr?.buckets || tr?.rows || []);
      const card = (lv?.machines || []).find(
        (m) => String(m.machine_id) === machineId);
      if (card) setLive(card);
      setLastUpdated(new Date().toLocaleTimeString());
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
      setLoaded(true);
    }
  }, [machineId, win.from, win.to, paretoGroup, paretoMetric]);

  useEffect(() => { load(); }, [load]);
  usePoll(load, refreshMs > 0 ? refreshMs : 24 * 60 * 60 * 1000, [load, refreshMs]);

  // Reference data changes on the configuration page, not while this one is
  // open, so it is fetched once rather than on every refresh.
  useEffect(() => {
    Promise.all([oeeList("downtime_reasons"), oeeList("orders"), oeeList("shifts")])
      .then(([d, o, sh]) => {
        setReasons((d.items || []).filter((x) => x.enabled !== 0));
        setOrders((o.items || []).filter((x) => String(x.machine_id || "") === machineId
          || !x.machine_id));
        setShifts(sh.items || []);
      })
      .catch(() => {});
  }, [machineId]);

  /* ------------------------------------------------------------- derived */

  // The live card is the machine's state NOW; `machine` is what the Overview
  // handed over, which may be a minute old by the time this page is read.
  const head = useMemo(() => ({ ...(machine || {}), ...(live || {}) }),
    [machine, live]);

  const r = result || {};
  const prev = previous || {};
  const energy = r.energy || {};
  const prevEnergy = prev.energy || {};

  const trendRows = useMemo(() => (trend || []).map((b) => ({
    t: chartTime(b.bucket_start_utc || b.from_utc || b.ts_utc),
    oee: b.oee === null || b.oee === undefined ? null : Number((b.oee * 100).toFixed(1)),
    availability: b.availability === null || b.availability === undefined
      ? null : Number((b.availability * 100).toFixed(1)),
    performance: b.performance === null || b.performance === undefined
      ? null : Number((b.performance * 100).toFixed(1)),
    quality: b.quality === null || b.quality === undefined
      ? null : Number((b.quality * 100).toFixed(1)),
    runtime_s: b.runtime_s,
    downtime_s: b.downtime_s,
    total_count: b.total,
    good_count: b.good,
    reject_count: b.reject,
    target_count: b.target_count,
    maturity: b.stage || b.maturity || "",
    machines_counted: b.machines_counted,
    machines_total: b.machines_total,
  })), [trend]);

  // A sparkline is the trend the service already returned, read down one
  // column. Nothing is recomputed to draw it.
  const sparkOf = useCallback(
    (key) => trendRows.map((row) => row[key]).filter((v) => v !== null && v !== undefined),
    [trendRows]);

  const paretoRows = paretoData?.rows || [];
  const downtimeEvents = useMemo(() => (events || []).filter(
    (e) => !["running", "production"].includes(String(e.state || ""))
  ), [events]);
  const unclassified = useMemo(
    () => downtimeEvents.filter((e) => !(e.downtime_reason || e.reason)),
    [downtimeEvents]);

  const maturity = useMemo(() => (r.stage ? {
    stage: r.stage,
    missing: r.missing_factors || [],
    assumption: "",
  } : null), [r.stage, r.missing_factors]);

  const prevLabel = useMemo(() => (
    selection?.shiftId ? "previous shift" : "previous period"), [selection]);

  /* ------------------------------------------------------------- actions */

  const saveEvent = useCallback(async (patch) => {
    if (!openEvent?.id) return;
    setSaving(true);
    setSaveError("");
    try {
      // Only the fields this action changes. The endpoint leaves everything
      // it was not given alone, so adding a comment cannot erase a reason.
      await oeeConfirmDowntime({ event_id: openEvent.id, ...patch });
      setOpenEvent(null);
      await load();
    } catch (err) {
      setSaveError(String(err?.message || err));
    } finally {
      setSaving(false);
    }
  }, [openEvent, load]);

  const quickAction = useCallback(async (id) => {
    setNotice("");
    setSaveError("");
    const currentEventId = head.event_id || "";
    try {
      if (id === "reason" || id === "planned" || id === "comment") {
        // These are edits to a specific stop, so they open the stop rather
        // than acting blind on whatever the machine is doing right now.
        const target = (id === "reason" || id === "comment")
          ? (downtimeEvents.find((e) => e.id === currentEventId)
             || unclassified[0] || downtimeEvents[0])
          : downtimeEvents.find((e) => e.id === currentEventId);
        if (!target) {
          setNotice(id === "planned"
            ? "This machine is not stopped, so there is no stop to mark planned."
            : "There is no downtime event in this period to annotate.");
          return;
        }
        setOpenEvent(target);
        return;
      }
      if (id === "unknown") {
        if (!unclassified.length) {
          setNotice("Every stop in this period already has a reason.");
          return;
        }
        setOpenEvent(unclassified[0]);
        return;
      }
      if (id === "count" || id === "reject") {
        const raw = window.prompt(id === "count"
          ? "Pieces produced (good):" : "Pieces scrapped:");
        const qty = Number(raw);
        if (!raw || !Number.isFinite(qty) || qty <= 0) return;
        if (id === "count") {
          await oeeAddCount({ machine_id: machineId, total_count: qty,
                              good_count: qty, source: "manual" });
        } else {
          await oeeAddQuality({ machine_id: machineId, quantity: qty,
                                result: "reject" });
        }
        setNotice(`Recorded ${num(qty)} ${id === "count" ? "pieces" : "rejects"}.`);
      }
      if (id === "cycle_start") {
        await oeeCycleStart({ machine_id: machineId, source: "manual" });
        setNotice("Manual cycle started.");
      }
      if (id === "cycle_stop") {
        await oeeCycleStop({ machine_id: machineId, result: "good" });
        setNotice("Manual cycle stopped.");
      }
      await load();
    } catch (err) {
      setSaveError(String(err?.message || err));
    }
  }, [head, machineId, downtimeEvents, unclassified, load]);

  /* -------------------------------------------------------------- render */

  if (!machineId) {
    return (
      <EmptyState title="No machine selected">
        Pick a machine from the OEE Overview.
      </EmptyState>
    );
  }

  return (
    <div className="oee-detail">
      <OeeMachineHeader
        machine={head}
        lastUpdated={lastUpdated}
        live={refreshMs > 0}
        onBack={onBack}
        actions={
          <OeeQuickActionsMenu machine={head} canEdit={canEdit}
                               onAction={quickAction} />
        }
      />

      {/* ------------------------------------------------ filter toolbar */}
      <div className="oee-period-row">
        <OeePeriodBar selection={selection} onChange={onSelectionChange}
          shifts={shifts} machines={machines} showMachineFilter={false} />
        <button type="button"
          className={`btn btn-secondary btn-sm${showFilters ? " active" : ""}`}
          aria-expanded={showFilters}
          onClick={() => setShowFilters((v) => !v)}>
          Filters
        </button>
        <OeeRefreshControl refreshMs={refreshMs} onRefreshMs={setRefreshMs}
                           lastUpdated={lastUpdated} busy={busy} />
      </div>

      {showFilters ? (
        <div className="oee-filter-extra">
          <label className="oee-toolbar-field">
            <span>Order</span>
            {/* Choosing an order moves the PERIOD to when that order actually
                ran. It is navigation, not a filter: the service computes
                availability from a window, so an order "filter" that left the
                window alone would report the wrong OEE. */}
            <select value=""
              onChange={(e) => {
                const o = orders.find((x) => String(x.id) === e.target.value);
                if (!o) return;
                if (!o.actual_start_utc) {
                  setNotice(`Order ${o.order_number} has not started, so it has `
                    + "no window to show.");
                  return;
                }
                onSelectionChange({
                  ...(selection || {}), preset: "custom",
                  from: o.actual_start_utc,
                  to: o.actual_end_utc || win.to,
                });
              }}>
              <option value="">Go to an order&apos;s run…</option>
              {orders.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.order_number}{o.status ? ` · ${o.status}` : ""}
                </option>
              ))}
            </select>
          </label>
          <span className="muted" style={{ fontSize: 12 }}>
            Product and batch are shown in the header as the machine&apos;s current
            context. They are not offered as filters here because a run of one
            product is a set of disjoint intervals, and an OEE figure over a
            union of intervals is not something the service computes.
          </span>
        </div>
      ) : null}

      {error ? <div className="info-note warn">{error}</div> : null}
      {saveError ? <div className="info-note warn">{saveError}</div> : null}
      {notice ? (
        <div className="info-note">
          {notice}
          <button type="button" className="btn btn-secondary btn-sm"
                  style={{ marginLeft: 8 }} onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {!loaded ? (
        <div className="info-note">Loading this machine…</div>
      ) : null}

      {/* -------------------------------------------------------- KPIs */}
      <div className="kpi-grid oee-kpi-grid">
        <OeeKpiCard title="OEE" value={pct(r.oee)} tone="primary"
          hint={KPI_HINTS.oee} current={r.oee} previous={prev.oee}
          previousLabel={prevLabel} spark={sparkOf("oee")} maturity={maturity} />
        <OeeKpiCard title="Availability" value={pct(r.availability)}
          hint={KPI_HINTS.availability} current={r.availability}
          previous={prev.availability} previousLabel={prevLabel}
          spark={sparkOf("availability")} />
        <OeeKpiCard title="Performance" value={pct(r.performance)}
          hint={KPI_HINTS.performance} current={r.performance}
          previous={prev.performance} previousLabel={prevLabel}
          spark={sparkOf("performance")}
          note={r.ideal_cycle_time_s ? "" : "No cycle time configured"} />
        <OeeKpiCard title="Quality" value={pct(r.quality)}
          hint={KPI_HINTS.quality} current={r.quality} previous={prev.quality}
          previousLabel={prevLabel} spark={sparkOf("quality")}
          note={r.total_count ? "" : "Nothing counted"} />
        <OeeKpiCard title="Runtime" value={duration(r.runtime_s)}
          hint={KPI_HINTS.runtime} current={r.runtime_s} previous={prev.runtime_s}
          previousLabel={prevLabel} />
        <OeeKpiCard title="Downtime" value={duration(r.downtime_s)} tone="warn"
          hint={KPI_HINTS.downtime} current={r.downtime_s}
          previous={prev.downtime_s} previousLabel={prevLabel} />
        <OeeKpiCard title="Production count"
          value={r.total_count ? num(r.total_count) : null}
          unit="pcs" hint={KPI_HINTS.production}
          current={r.total_count} previous={prev.total_count}
          previousLabel={prevLabel} spark={sparkOf("total_count")}
          note="No counts recorded" />
        <OeeKpiCard title="Energy used"
          value={energy.total_kwh ? num(energy.total_kwh, 1) : null}
          unit="kWh" hint={KPI_HINTS.energy}
          current={energy.total_kwh} previous={prevEnergy.total_kwh}
          previousLabel={prevLabel}
          note="Energy monitoring not configured" />
      </div>

      {/* -------------------------------------------------------- tabs */}
      <div className="tabs oee-detail-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab"
                  aria-selected={tab === t.id}
                  className={`oee-tab${tab === t.id ? " active" : ""}`}
                  onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ----------------------------------------------- summary: row 1 */}
      <div className="oee-detail-row">
        <section className="card">
          <h3 className="card-title">OEE trend</h3>
          {trendRows.length ? (
            <OeeTrendChart rows={trendRows} visible={trendVisible}
              onToggle={(key) => setTrendVisible((p) => ({ ...p, [key]: !p[key] }))} />
          ) : (
            <EmptyState title="No trend yet">
              Nothing has been calculated for this window.
            </EmptyState>
          )}
        </section>

        <section className="card">
          <h3 className="card-title">Production count</h3>
          <OeeProductionChart rows={trendRows} />
        </section>
      </div>

      {/* ----------------------------------------------- summary: row 2 */}
      <div className="oee-detail-row">
        <section className="card">
          <h3 className="card-title">Machine status timeline</h3>
          <StatusTimeline lanes={lanes} from={win.from} to={win.to}
            onBlockClick={(_lane, blk) => {
              // The block carries what the timeline needed to draw it; the
              // events list carries the record. Match them so the modal can
              // edit a real row rather than a drawing of one.
              const hit = downtimeEvents.find(
                (e) => String(e.start_utc) === String(blk.start_utc)) || blk;
              setOpenEvent(hit);
            }}
            height={34} />
        </section>

        <section className="card">
          <h3 className="card-title">Downtime Pareto</h3>
          {paretoRows.length ? (
            <OeeDowntimePareto
              rows={paretoRows}
              metric={paretoMetric} onMetric={setParetoMetric}
              groupBy={paretoGroup} onGroupBy={setParetoGroup}
              groups={paretoData?.groups_supported || ["reason"]}
              onBarClick={(row) => {
                // Show the events behind the bar rather than navigating away.
                const label = String(row?.label || "");
                const hit = downtimeEvents.find(
                  (e) => String(e.downtime_reason || e.reason || "") === label);
                if (hit) setOpenEvent(hit);
              }}
            />
          ) : (
            <EmptyState title="No downtime recorded">
              Either the machine ran, or nothing has been classified yet.
            </EmptyState>
          )}
        </section>
      </div>

      {/* ----------------------------------------------- summary: row 3 */}
      <Section title="Downtime events" count={downtimeEvents.length} open
        subtitle={unclassified.length
          ? `${unclassified.length} still need a reason`
          : ""}>
        <OeeDowntimeEventsTable events={downtimeEvents} canEdit={canEdit}
          onOpen={setOpenEvent} onAction={(e) => setOpenEvent(e)} />
      </Section>

      <OeeDowntimeEventModal event={openEvent} reasons={reasons}
        canEdit={canEdit} busy={saving} error={saveError}
        onSave={saveEvent} onClose={() => { setOpenEvent(null); setSaveError(""); }} />
    </div>
  );
}
