/* OEE > Planning Calendar — what production WAS supposed to happen.

   2026-08-29. The administrator plans runs, stops, maintenance and trials
   here, and those windows change the OEE denominator: planned time comes from
   shifts AND these events, planned stops can be excluded, "no production
   planned" must never count as downtime, and planned maintenance must not be
   mistaken for an unplanned stop.

   Because it changes numbers rather than pictures, `exclude_from_oee` is an
   explicit per-event choice and is never inferred from the event type.

   Rendered as a machine timeline rather than a month grid: a plant reads its
   plan as "which machine, when", and a timeline is also what the existing
   dashboard components can draw without adding a calendar library. Day and
   week are the two views that matter; month is deliberately deferred. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { oeePlanning, oeeSave, oeeDelete, oeeList } from "../../api";
import { EmptyState, Section, Toggle } from "./OeeShared";
import { toStamp } from "./OeePeriod";

export const EVENT_TYPES = [
  ["planned_production", "Planned production"],
  ["planned_stop", "Planned stop"],
  ["planned_maintenance", "Planned maintenance"],
  ["cleaning", "Cleaning"],
  ["changeover", "Changeover"],
  ["setup", "Setup"],
  ["no_production", "No production planned"],
  ["break", "Break"],
  ["meeting", "Meeting"],
  ["trial", "Trial / test run"],
  ["batch_run", "Batch run"],
  ["recipe_run", "Recipe run"],
  ["order_run", "Order run"],
];

const VIEWS = [["day", "Day"], ["week", "Week"]];

function startOfUtcDay(d) {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

function emptyEvent(machineId = "") {
  const now = new Date();
  const from = startOfUtcDay(now);
  from.setUTCHours(8, 0, 0, 0);
  const to = new Date(from);
  to.setUTCHours(16, 0, 0, 0);
  return {
    name: "", event_type: "planned_production", machine_id: machineId,
    line: "", start_utc: toStamp(from), end_utc: toStamp(to),
    exclude_from_oee: 0, counts_as_planned_stop: 0,
    expected_quantity: "", notes: "", enabled: 1,
  };
}

export default function OeePlanningCalendarPage({ canEdit = false }) {
  const [view, setView] = useState("week");
  const [dayOffset, setDayOffset] = useState(0);
  const [machines, setMachines] = useState([]);
  const [events, setEvents] = useState([]);
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const range = useMemo(() => {
    const base = startOfUtcDay(new Date());
    base.setUTCDate(base.getUTCDate() + dayOffset);
    const to = new Date(base);
    to.setUTCDate(to.getUTCDate() + (view === "week" ? 7 : 1));
    return { from: toStamp(base), to: toStamp(to), fromMs: base.getTime(), toMs: to.getTime() };
  }, [view, dayOffset]);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const [ms, ev] = await Promise.all([
        oeeList("machines").catch(() => ({ items: [] })),
        oeePlanning({ from_utc: range.from, to_utc: range.to }).catch(() => ({ events: [] })),
      ]);
      setMachines(ms?.items || []);
      setEvents(ev?.events || []);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
    }
  }, [range.from, range.to]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    if (!editing) return;
    if (!String(editing.name || "").trim()) {
      setError("Give the event a name.");
      return;
    }
    if (String(editing.end_utc) <= String(editing.start_utc)) {
      setError("The event must end after it starts.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await oeeSave("planned_events", {
        ...editing,
        exclude_from_oee: editing.exclude_from_oee ? 1 : 0,
        counts_as_planned_stop: editing.counts_as_planned_stop ? 1 : 0,
        enabled: editing.enabled ? 1 : 0,
      });
      setNote(`Saved "${editing.name}".`);
      setEditing(null);
      await load();
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (ev) => {
    if (!ev?.id) return;
    // Deleting a planned window changes what counted as planned time, so
    // the OEE for that period moves. Say so before it happens.
    const ok = window.confirm(
      `Delete planned event "${ev.name}"?`
      + `\n\nThis changes what counts as planned time, so OEE figures for `
      + `that window will change.`
    );
    if (!ok) return;
    setBusy(true);
    try {
      await oeeDelete("planned_events", ev.id);
      setNote(`Deleted "${ev.name}".`);
      await load();
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
    }
  };

  // Lanes: one per machine, plus a lane for line-wide events (machine_id null).
  const lanes = useMemo(() => {
    const rows = machines.map((m) => ({
      id: String(m.id), name: m.name || m.id,
      events: events.filter((e) => String(e.machine_id || "") === String(m.id)),
    }));
    const plantWide = events.filter((e) => !String(e.machine_id || "").trim());
    if (plantWide.length) {
      rows.unshift({ id: "", name: "Whole line / plant", events: plantWide });
    }
    return rows;
  }, [machines, events]);

  const span = range.toMs - range.fromMs;

  return (
    <div className="oee-planning">
      <div className="row oee-detail-head">
        <select value={view} onChange={(e) => setView(e.target.value)} style={{ width: 110 }}>
          {VIEWS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <div className="oee-stepper">
          <button type="button" className="btn btn-secondary btn-sm"
            onClick={() => setDayOffset((d) => d - (view === "week" ? 7 : 1))}>‹</button>
          <span className="oee-stepper-label">{range.from.slice(0, 10)}</span>
          <button type="button" className="btn btn-secondary btn-sm"
            onClick={() => setDayOffset((d) => d + (view === "week" ? 7 : 1))}>›</button>
        </div>
        <button type="button" className="btn btn-secondary btn-sm"
          onClick={() => setDayOffset(0)}>Today</button>
        <span style={{ marginLeft: "auto" }} />
        <button type="button" className="btn btn-primary btn-sm" disabled={!canEdit || busy}
          onClick={() => setEditing(emptyEvent(machines[0]?.id || ""))}
          title={canEdit ? "Add a planned event"
                         : "You have read-only access to the planning calendar"}>
          Add planned event
        </button>
      </div>

      <div className="muted" style={{ fontSize: 12 }}>
        {range.from.slice(0, 16)} → {range.to.slice(0, 16)} UTC
        {!canEdit ? " · read-only" : ""}
      </div>

      {error ? <div className="info-note warn">{error}</div> : null}
      {note ? <div className="info-note">{note}</div> : null}

      <Section title="Plan by machine" count={events.length} open>
        {lanes.length ? (
          <div className="oee-timeline oee-plan-timeline">
            {lanes.map((lane) => (
              <div className="oee-tl-lane" key={lane.id || "plant"}>
                <div className="oee-tl-name" title={lane.name}>{lane.name}</div>
                <div className="oee-tl-track" style={{ height: 32 }}>
                  {lane.events.map((ev) => {
                    const s = Date.parse(String(ev.start_utc || "").replace(" ", "T") + "Z");
                    const e = Date.parse(String(ev.end_utc || "").replace(" ", "T") + "Z");
                    if (!Number.isFinite(s) || !Number.isFinite(e)) return null;
                    const left = Math.max(0, ((s - range.fromMs) / span) * 100);
                    const width = Math.max(0.4,
                      ((Math.min(e, range.toMs) - Math.max(s, range.fromMs)) / span) * 100);
                    if (width <= 0) return null;
                    return (
                      <button type="button" key={ev.id}
                        className={`oee-tl-block oee-plan-${ev.event_type}`}
                        style={{ left: `${left}%`, width: `${width}%` }}
                        title={`${ev.name} · ${ev.event_type}`
                          + (ev.exclude_from_oee ? " · excluded from OEE" : "")}
                        onClick={() => canEdit && setEditing({ ...ev })}>
                        <span className="oee-plan-label">{ev.name}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : <EmptyState title="No machines configured">
          Add machines in OEE Configuration first — a plan needs something to plan for.
        </EmptyState>}
      </Section>

      <Section title="Planned events" count={events.length} open>
        {events.length ? (
          <div className="table db-table oee-plan-table">
            <div className="thead">
              <span>Name</span><span>Type</span><span>Machine</span><span>Start</span>
              <span>End</span><span>Excluded from OEE</span><span style={{ textAlign: "right" }}>Actions</span>
            </div>
            {events.map((ev) => (
              <div className="trow" key={ev.id}>
                <span>{ev.name}</span>
                <span className="muted">
                  {(EVENT_TYPES.find(([v]) => v === ev.event_type) || [])[1] || ev.event_type}
                </span>
                <span>{machines.find((m) => String(m.id) === String(ev.machine_id))?.name
                  || <em className="muted">whole line</em>}</span>
                <span>{String(ev.start_utc || "").slice(5, 16)}</span>
                <span>{String(ev.end_utc || "").slice(5, 16)}</span>
                <span>{ev.exclude_from_oee ? "Yes" : "No"}</span>
                <span className="row-actions">
                  <button type="button" className="btn btn-secondary btn-sm"
                    disabled={!canEdit} onClick={() => setEditing({ ...ev })}>Edit</button>
                  <button type="button" className="btn btn-danger btn-sm"
                    disabled={!canEdit} onClick={() => remove(ev)}>Delete</button>
                </span>
              </div>
            ))}
          </div>
        ) : <EmptyState title="Nothing planned in this window">
          With no plan, planned production time comes from the shift schedule alone.
        </EmptyState>}
      </Section>

      {editing ? (
        <div className="modal-backdrop" onClick={() => setEditing(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h4 style={{ marginTop: 0 }}>
              {editing.id ? "Edit planned event" : "New planned event"}
            </h4>
            <div className="form-grid">
              <label>Name
                <input value={editing.name} autoFocus
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </label>
              <label>Type
                <select value={editing.event_type}
                  onChange={(e) => setEditing({ ...editing, event_type: e.target.value })}>
                  {EVENT_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </label>
              <label>Machine
                <select value={editing.machine_id || ""}
                  onChange={(e) => setEditing({ ...editing, machine_id: e.target.value })}>
                  <option value="">Whole line / plant</option>
                  {machines.map((m) => <option key={m.id} value={m.id}>{m.name || m.id}</option>)}
                </select>
              </label>
              <label>Start (UTC)
                <input value={editing.start_utc}
                  onChange={(e) => setEditing({ ...editing, start_utc: e.target.value })} />
              </label>
              <label>End (UTC)
                <input value={editing.end_utc}
                  onChange={(e) => setEditing({ ...editing, end_utc: e.target.value })} />
              </label>
              <label>Expected quantity
                <input type="number" value={editing.expected_quantity ?? ""}
                  onChange={(e) => setEditing({ ...editing, expected_quantity: e.target.value })} />
              </label>
            </div>

            <Toggle label="Exclude this window from OEE"
              checked={Boolean(editing.exclude_from_oee)}
              onChange={(v) => setEditing({ ...editing, exclude_from_oee: v ? 1 : 0 })}
              hint="Removes the window from planned production time. This changes the OEE number, so it is an explicit choice and never inferred from the event type." />
            <Toggle label="Count as a planned stop"
              checked={Boolean(editing.counts_as_planned_stop)}
              onChange={(v) => setEditing({ ...editing, counts_as_planned_stop: v ? 1 : 0 })}
              hint="Separates this from unplanned downtime in the Pareto and the timeline." />
            <Toggle label="Enabled" checked={Boolean(editing.enabled)}
              onChange={(v) => setEditing({ ...editing, enabled: v ? 1 : 0 })} />

            <label>Notes
              <textarea rows={2} value={editing.notes || ""}
                onChange={(e) => setEditing({ ...editing, notes: e.target.value })} />
            </label>

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary btn-sm"
                onClick={() => setEditing(null)}>Cancel</button>
              <button type="button" className="btn btn-primary btn-sm"
                disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
