/* OEE > Machine Detail — the parts this page needed.
 *
 * 2026-08-31. The page already had a header, a period bar, KPIs, a trend, a
 * Pareto, a timeline and a downtime list. These are the pieces the brief asked
 * for that did not exist:
 *
 *   OeeMachineHeader        breadcrumb, live status, the machine's current
 *                           context (order, product, line, source) and the
 *                           actions that belong to the machine, not the page
 *   OeeQuickActionsMenu     the operator actions, filtered by what the machine
 *                           is configured for AND what the user may do
 *   OeeProductionChart      total / good / reject against the configured target
 *   OeeDowntimeEventsTable  the full event record, with the actions the brief
 *                           lists, and unclassified stops made obvious
 *   OeeDowntimeEventModal   view one stop; edit it only with permission
 *
 * Named to match the existing OeeShared / OeeVisuals / OeeOverviewParts files
 * rather than the OEE* spelling in the brief - a second naming convention in
 * one folder costs more than it explains. The brief's OEEStatusBadge,
 * OEEConfidenceBadge and OEEDataMaturityBadge are the existing StatePill,
 * ConfidencePill and MaturityBadge; OEEKPICard, OEETrendChart and
 * OEEDowntimePareto are the components the Overview already uses, imported
 * here rather than rewritten.
 *
 * No OEE arithmetic lives here. Every figure comes from /api/oee.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend,
} from "recharts";
import { MaturityBadge } from "./OeeVisuals";
import {
  StatePill, ConfidencePill, EmptyState, duration, num,
  SOURCE_LABELS, STATE_LABELS,
} from "./OeeShared";

/* ------------------------------------------------------------- header */

/* One fact of the machine's current context. Rendered only when there is
   something to show: an empty "Order: —" teaches the reader nothing and
   costs a slot on a crowded line. */
function ContextItem({ label, value, title = "" }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <span className="oee-ctx-item" title={title}>
      <span className="oee-ctx-label">{label}</span>
      <span className="oee-ctx-value">{value}</span>
    </span>
  );
}

export function OeeMachineHeader({
  machine, lastUpdated, live = false, onBack, onExport, actions = null,
}) {
  const m = machine || {};
  const cycle = m.cycle || null;
  return (
    <div className="oee-detail-head">
      <nav className="oee-breadcrumb" aria-label="Breadcrumb">
        <button type="button" className="linkish" onClick={onBack}>OEE</button>
        <span aria-hidden="true">›</span>
        <button type="button" className="linkish" onClick={onBack}>Overview</button>
        <span aria-hidden="true">›</span>
        <span aria-current="page">Machine Detail</span>
      </nav>

      <div className="oee-detail-title">
        <h3>{m.name || m.machine_id || "Machine"}</h3>
        <StatePill state={m.state} />
        {m.current_state_seconds !== undefined && m.current_state_seconds !== null ? (
          <span className="muted" title={m.since_utc ? `Since ${m.since_utc} UTC` : ""}>
            {m.since_utc ? `Since ${String(m.since_utc).slice(11, 16)} ` : ""}
            ({duration(m.current_state_seconds)})
          </span>
        ) : null}
        <span className="oee-head-spacer" />
        <span className={`oee-live ${live ? "is-live" : ""}`}>
          {lastUpdated ? `Last updated ${lastUpdated}` : "—"}
          {live ? <em className="oee-live-dot" /> : null}
        </span>
        {onExport ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onExport}>
            Export
          </button>
        ) : null}
        {actions}
      </div>

      <div className="oee-ctx-row">
        <ContextItem label="Order" value={m.order_number} />
        <ContextItem label="Product" value={m.product_code} />
        {/* Recipe / batch only when the running cycle actually carries one.
            Nothing in the OEE tables holds a batch or recipe of its own, so
            this stays empty rather than inventing a field to fill. */}
        <ContextItem label="Batch" value={cycle?.batch || cycle?.notes || ""} />
        <ContextItem label="Line" value={m.line} />
        <ContextItem label="Area" value={m.area} />
        <ContextItem label="Source" value={SOURCE_LABELS[m.status_source] || m.status_source}
          title="How this machine's state is being determined" />
        <span className="oee-ctx-item">
          <ConfidencePill confidence={m.confidence} source={m.status_source} />
        </span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------ quick actions */

/* Everything an operator can do to this machine from this page. Each entry
   declares what it needs, so an action is offered only when the machine is
   configured for it AND the user is allowed to do it - a menu full of
   greyed-out entries is a worse answer than a short menu. */
export const QUICK_ACTIONS = [
  { id: "reason", label: "Add downtime reason", needs: "write",
    hint: "Classify the stop the machine is in now" },
  { id: "unknown", label: "Correct unknown downtime", needs: "write",
    hint: "Go to the oldest stop still without a reason" },
  { id: "count", label: "Add manual count", needs: "manual",
    hint: "Record pieces produced by hand" },
  { id: "reject", label: "Add reject / scrap count", needs: "manual",
    hint: "Record scrap against this machine" },
  { id: "cycle_start", label: "Start manual cycle", needs: "manual" },
  { id: "cycle_stop", label: "Stop manual cycle", needs: "manual" },
  { id: "comment", label: "Add operator comment", needs: "write" },
  { id: "planned", label: "Confirm planned stop", needs: "write",
    hint: "Mark the current stop as planned, so it does not count against availability" },
];

export function OeeQuickActionsMenu({ machine, canEdit = false, onAction }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef(null);
  const manual = Boolean(machine?.manual_enabled);

  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const allowed = QUICK_ACTIONS.filter((a) => (
    a.needs === "write" ? canEdit : (canEdit && manual)
  ));

  if (!canEdit) {
    // Not a disabled button with no explanation: say why it is not there.
    return (
      <span className="muted oee-quick-none"
            title="Quick actions change recorded production data, which needs the OEE configuration permission.">
        Read-only access
      </span>
    );
  }

  return (
    <div className="oee-quick" ref={boxRef}>
      <button type="button" className="btn btn-primary btn-sm"
              aria-haspopup="menu" aria-expanded={open}
              onClick={() => setOpen((v) => !v)}>
        Quick Actions ▾
      </button>
      {open ? (
        <div className="oee-quick-menu" role="menu">
          {allowed.map((a) => (
            <button key={a.id} type="button" role="menuitem" title={a.hint || ""}
                    onClick={() => { setOpen(false); onAction(a.id); }}>
              {a.label}
            </button>
          ))}
          {!manual ? (
            <div className="oee-quick-note">
              Manual count and cycle actions are switched off for this machine.
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------- production chart */

export function OeeProductionChart({ rows = [], height = 240 }) {
  const hasCounts = (rows || []).some(
    (r) => Number(r.total_count || 0) > 0 || Number(r.good_count || 0) > 0
      || Number(r.reject_count || 0) > 0);
  const hasTarget = (rows || []).some((r) => r.target_count !== null
    && r.target_count !== undefined);

  if (!hasCounts) {
    return (
      <EmptyState title="No production counted in this period">
        Counts arrive from a PLC tag, a sensor, or the operator screen. Until
        one of those is configured this machine can report availability but not
        performance or quality.
      </EmptyState>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={rows}>
        <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
        <XAxis dataKey="t" tick={{ fontSize: 10 }} />
        <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
        <Tooltip formatter={(v, k) => [num(v), k]} />
        <Legend />
        <Area type="monotone" dataKey="total_count" name="Total" stackId={undefined}
              stroke="#38bdf8" fill="#38bdf8" fillOpacity={0.18}
              isAnimationActive={false} />
        <Area type="monotone" dataKey="good_count" name="Good"
              stroke="#22c55e" fill="#22c55e" fillOpacity={0.22}
              isAnimationActive={false} />
        <Area type="monotone" dataKey="reject_count" name="Reject"
              stroke="#ef4444" fill="#ef4444" fillOpacity={0.3}
              isAnimationActive={false} />
        {/* Only when a cycle time is configured. A dashed line at zero would
            read as "target: make nothing". */}
        {hasTarget ? (
          <Line type="monotone" dataKey="target_count" name="Target (ideal rate)"
                stroke="#e5e7eb" strokeDasharray="6 4" strokeWidth={1.6}
                dot={false} isAnimationActive={false} connectNulls />
        ) : null}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

/* -------------------------------------------------- downtime events */

function stamp(v) {
  const s = String(v || "");
  return s.length >= 16 ? s.slice(11, 16) : (s || "—");
}

export function OeeDowntimeEventsTable({
  events = [], canEdit = false, onOpen, onAction, limit = 200,
}) {
  if (!events.length) {
    return (
      <EmptyState title="No downtime events">
        Nothing stopped this machine in the selected period.
      </EmptyState>
    );
  }
  return (
    /* Not .db-table: that class also sets grid-template-columns, and two
       rules setting the same property on the same element is how this
       codebase has silently wrapped table rows before. One owner per
       property - .oee-dt-table defines its own columns. */
    <div className="table oee-dt-table">
      <div className="thead">
        <span>Start</span><span>End</span><span>Duration</span><span>State</span>
        <span>Reason</span><span>Category</span><span>Planned</span>
        <span>Source</span><span>Confidence</span><span>Confirmed by</span>
        <span>Comment</span><span>Actions</span>
      </div>
      {events.slice(0, limit).map((e, i) => {
        const reason = e.downtime_reason || e.reason || "";
        // An unclassified stop is the one piece of work this table exists to
        // surface, so it is marked rather than left to be spotted.
        const needsReason = !reason;
        return (
          <div className={`trow${needsReason ? " oee-dt-unknown" : ""}`}
               key={e.id || i}>
            <span>{stamp(e.start_utc)}</span>
            <span>{e.end_utc ? stamp(e.end_utc) : <em className="muted">open</em>}</span>
            <span>{duration(e.duration_s)}</span>
            <span><StatePill state={e.state} /></span>
            <span>{reason || <em className="oee-needs-reason">Needs a reason</em>}</span>
            <span className="muted">{e.reason_category || e.downtime_category || "—"}</span>
            <span>{e.is_planned || e.planned ? "Planned" : "Unplanned"}</span>
            <span className="muted">{SOURCE_LABELS[e.status_source] || e.status_source || "—"}</span>
            <span className="muted">{e.confidence || "—"}</span>
            <span className="muted">{e.confirmed_by || "—"}</span>
            <span className="muted oee-dt-comment"
                  title={e.operator_comment || e.comment || ""}>
              {e.operator_comment || e.comment || "—"}
            </span>
            <span className="oee-dt-actions">
              <button type="button" className="btn btn-secondary btn-sm"
                      onClick={() => onOpen(e)}>View</button>
              {canEdit ? (
                <button type="button" className="btn btn-secondary btn-sm"
                        onClick={() => onAction(e, "reason")}>Reason</button>
              ) : null}
            </span>
          </div>
        );
      })}
      {events.length > limit ? (
        <div className="muted" style={{ padding: "6px 8px", fontSize: 12 }}>
          Showing the first {limit} of {events.length} events. Narrow the period
          to see the rest.
        </div>
      ) : null}
    </div>
  );
}

/* One stop, in full. Editing is allowed only with permission; without it the
   modal is still worth opening, because reading the record is how an operator
   finds out what already happened. */
export function OeeDowntimeEventModal({
  event, reasons = [], canEdit = false, busy = false, error = "",
  onSave, onClose,
}) {
  const [reasonId, setReasonId] = useState("");
  const [category, setCategory] = useState("");
  const [comment, setComment] = useState("");

  useEffect(() => {
    setReasonId(String(event?.downtime_reason_id || ""));
    setCategory(String(event?.downtime_category || event?.reason_category || ""));
    setComment(String(event?.operator_comment || event?.comment || ""));
  }, [event]);

  const categories = useMemo(() => Array.from(new Set(
    (reasons || []).map((r) => String(r.category || "")).filter(Boolean)
  )).sort(), [reasons]);

  const pickable = useMemo(() => (reasons || []).filter(
    (r) => !category || String(r.category || "") === category), [reasons, category]);

  if (!event) return null;
  const planned = Boolean(event.is_planned || event.planned);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h4>
          {STATE_LABELS[event.state] || event.state || "Downtime"} ·{" "}
          {duration(event.duration_s)}
        </h4>

        <div className="oee-block-detail">
          <div><b>Start</b><span>{event.start_utc || "—"}</span></div>
          <div><b>End</b><span>{event.end_utc || "open"}</span></div>
          <div><b>State</b><span>{STATE_LABELS[event.state] || event.state || "—"}</span></div>
          <div><b>Source</b><span>{SOURCE_LABELS[event.status_source] || event.status_source || "—"}</span></div>
          <div><b>Confidence</b><span>{event.confidence || "—"}</span></div>
          <div><b>Confirmed by</b><span>{event.confirmed_by || "not confirmed"}</span></div>
        </div>

        {!canEdit ? (
          <>
            <div className="oee-block-detail">
              <div><b>Reason</b><span>{event.downtime_reason || event.reason || "unassigned"}</span></div>
              <div><b>Category</b><span>{event.downtime_category || event.reason_category || "—"}</span></div>
              <div><b>Planned</b><span>{planned ? "Yes" : "No"}</span></div>
              <div><b>Comment</b><span>{event.operator_comment || event.comment || "—"}</span></div>
            </div>
            <div className="info-note">
              You have read-only access to OEE records. Classifying a stop needs
              the OEE configuration permission.
            </div>
          </>
        ) : (
          <div className="form-grid oee-dt-form">
            <label>
              <span>Category</span>
              <select value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">Any</option>
                {categories.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label>
              <span>Reason</span>
              <select value={reasonId} onChange={(e) => setReasonId(e.target.value)}>
                <option value="">Unknown (leave unclassified)</option>
                {pickable.map((r) => (
                  <option key={r.id} value={r.id}>{r.reason || r.name || r.id}</option>
                ))}
              </select>
            </label>
            <label className="span-2">
              <span>Comment</span>
              <input type="text" value={comment} maxLength={400}
                     placeholder="What actually happened"
                     onChange={(e) => setComment(e.target.value)} />
            </label>
          </div>
        )}

        {error ? <div className="info-note warn">{error}</div> : null}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
            Close
          </button>
          {canEdit ? (
            <>
              {/* Each button sends only what it changes. The endpoint leaves
                  every field it is not given alone, so marking a stop planned
                  cannot wipe the reason somebody established. */}
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy}
                      title={planned ? "Count this stop against availability again"
                                     : "Planned stops do not count against availability"}
                      onClick={() => onSave({ is_planned: !planned })}>
                {planned ? "Mark unplanned" : "Mark planned"}
              </button>
              <button type="button" className="btn btn-primary btn-sm" disabled={busy}
                      onClick={() => onSave({
                        downtime_reason_id: reasonId,
                        downtime_category: category,
                        comment,
                      })}>
                {busy ? "Saving…" : "Confirm downtime"}
              </button>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
