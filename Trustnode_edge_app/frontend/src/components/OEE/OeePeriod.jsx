/* The OEE period selection — one component, every page.

   2026-08-29. Overview, Machine Detail and the Planning Calendar all answer
   "what am I looking at, and when". Three copies of that logic would drift,
   and the drift would show up as two pages disagreeing about the same shift.

   The selection is a plain object so a page can lift it into shared state and
   carry it through navigation: selecting Shift 2 on 2026-08-29 and clicking a
   machine must open THAT machine for THAT shift, not reset to "last 24 h".

   Windows are resolved to explicit from/to timestamps in the app's canonical
   format. A dashboard whose period is ambiguous is a dashboard that gets
   misread, so the resolved window is always printed. */
import { useCallback, useMemo } from "react";

export const OEE_PRESETS = [
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
  { id: "shift", label: "Current shift" },
  { id: "prev_shift", label: "Previous shift" },
  { id: "7d", label: "Last 7 days" },
  { id: "30d", label: "Last 30 days" },
  { id: "custom", label: "Custom range" },
];

/* The app's canonical timestamp: "YYYY-MM-DD HH:MM:SS.mmm", UTC, no zone
   suffix. Every store in this product compares ts_utc as TEXT, and a 'T'
   sorts after a space — a single isoformat writer once put its rows outside
   every range filter. */
export function toStamp(d) {
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
    + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}.`
    + `${p(d.getUTCMilliseconds(), 3)}`;
}

/* The datetime-local input speaks local wall-clock; every stamp in this
   product is UTC in "YYYY-MM-DD HH:MM:SS.mmm". Convert at the edge so no UTC
   stamp is ever built from a local-time string by accident. */
export function toLocalInput(stamp) {
  if (!stamp) return "";
  const d = new Date(String(stamp).replace(" ", "T") + "Z");
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fromLocalInput(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : toStamp(d);
}

function startOfUtcDay(d) {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

export function defaultSelection() {
  return { preset: "today", dayOffset: 0, shiftId: "", from: "", to: "",
           machineIds: [], line: "", area: "" };
}

/* Resolve a selection into { from, to, label }.

   `shifts` are the configured oee_shifts rows; when one is selected its
   HH:MM boundaries are applied to the chosen day. Shift arithmetic lives on
   the server for the calculation — this is only what the page asks FOR. */
export function resolveWindow(sel, shifts = []) {
  const s = sel || defaultSelection();
  const now = new Date();
  const day = startOfUtcDay(now);
  day.setUTCDate(day.getUTCDate() + Number(s.dayOffset || 0));

  let from = new Date(day);
  let to = new Date(day);
  to.setUTCDate(to.getUTCDate() + 1);
  let label = "";

  if (s.preset === "custom" && s.from && s.to) {
    return { from: s.from, to: s.to, label: `${s.from.slice(0, 16)} → ${s.to.slice(0, 16)}` };
  }
  if (s.preset === "yesterday") {
    from.setUTCDate(from.getUTCDate() - 1);
    to.setUTCDate(to.getUTCDate() - 1);
    label = "Yesterday";
  } else if (s.preset === "7d") {
    from = new Date(now.getTime() - 7 * 86400000);
    to = now;
    label = "Last 7 days";
  } else if (s.preset === "30d") {
    from = new Date(now.getTime() - 30 * 86400000);
    to = now;
    label = "Last 30 days";
  } else {
    label = Number(s.dayOffset || 0) === 0 ? "Today"
      : `${day.toISOString().slice(0, 10)}`;
  }

  // A chosen shift narrows the day to that shift's hours.
  const shift = (shifts || []).find((x) => String(x.id) === String(s.shiftId || ""));
  if (shift && s.preset !== "7d" && s.preset !== "30d") {
    const parse = (hhmm, base) => {
      const [h, m] = String(hhmm || "00:00").split(":").map((x) => Number(x) || 0);
      const d = new Date(base);
      d.setUTCHours(h, m, 0, 0);
      return d;
    };
    const ws = parse(shift.start_time, day);
    let we = parse(shift.end_time, day);
    if (we <= ws) we.setUTCDate(we.getUTCDate() + 1);   // crosses midnight
    from = ws;
    to = we;
    label = `${label} · ${shift.name || shift.id}`;
  }

  return { from: toStamp(from), to: toStamp(to), label };
}

export default function OeePeriodBar({
  selection, onChange, shifts = [], machines = [],
  showMachineFilter = true, disabled = false,
}) {
  const sel = selection || defaultSelection();
  const window = useMemo(() => resolveWindow(sel, shifts), [sel, shifts]);

  const patch = useCallback((next) => onChange({ ...sel, ...next }), [sel, onChange]);

  const stepDay = (delta) => patch({
    dayOffset: Number(sel.dayOffset || 0) + delta,
    // Stepping a day out of "last 7 days" means nothing; land on that day.
    preset: (sel.preset === "7d" || sel.preset === "30d") ? "today" : sel.preset,
  });

  const stepShift = (delta) => {
    const ids = (shifts || []).map((x) => String(x.id));
    if (!ids.length) return;
    const at = ids.indexOf(String(sel.shiftId || ""));
    let next = at + delta;
    let dayOffset = Number(sel.dayOffset || 0);
    // Walking off either end of the day rolls into the next/previous one, the
    // way a shift pattern actually runs.
    if (next < 0) { next = ids.length - 1; dayOffset -= 1; }
    if (next >= ids.length) { next = 0; dayOffset += 1; }
    patch({ shiftId: ids[next], dayOffset });
  };

  const lines = useMemo(
    () => Array.from(new Set((machines || []).map((m) => String(m.line || "")).filter(Boolean))).sort(),
    [machines]
  );

  return (
    <div className="oee-period-bar">
      <div className="row" style={{ gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select value={sel.preset} disabled={disabled} style={{ width: 150 }}
          onChange={(e) => patch({ preset: e.target.value })}>
          {OEE_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>

        <div className="oee-stepper" role="group" aria-label="Day">
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            title="Previous day" onClick={() => stepDay(-1)}>‹</button>
          <span className="oee-stepper-label">Day</span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            title="Next day" onClick={() => stepDay(1)}>›</button>
        </div>

        <div className="oee-stepper" role="group" aria-label="Shift">
          <button type="button" className="btn btn-secondary btn-sm"
            disabled={disabled || !shifts.length}
            title="Previous shift" onClick={() => stepShift(-1)}>‹</button>
          <select value={sel.shiftId || ""} disabled={disabled || !shifts.length}
            style={{ width: 150 }}
            onChange={(e) => patch({ shiftId: e.target.value })}>
            <option value="">All shifts</option>
            {(shifts || []).map((s) => (
              <option key={s.id} value={s.id}>{s.name || s.id}</option>
            ))}
          </select>
          <button type="button" className="btn btn-secondary btn-sm"
            disabled={disabled || !shifts.length}
            title="Next shift" onClick={() => stepShift(1)}>›</button>
        </div>

        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
          onClick={() => patch({ preset: "today", dayOffset: 0 })}>Today</button>

        {/* resolveWindow has always understood a custom range, but nothing
            ever rendered the two dates - so picking "Custom range" quietly
            behaved like Today. The inputs appear only for that preset. */}
        {sel.preset === "custom" ? (
          <div className="oee-stepper" role="group" aria-label="Date range">
            <input type="datetime-local" disabled={disabled}
              value={toLocalInput(sel.from)}
              onChange={(e) => patch({ from: fromLocalInput(e.target.value) })} />
            <span className="oee-stepper-label">to</span>
            <input type="datetime-local" disabled={disabled}
              value={toLocalInput(sel.to)}
              onChange={(e) => patch({ to: fromLocalInput(e.target.value) })} />
          </div>
        ) : null}

        {showMachineFilter && lines.length ? (
          <select value={sel.line || ""} disabled={disabled} style={{ width: 150 }}
            onChange={(e) => patch({ line: e.target.value })}>
            <option value="">All lines</option>
            {lines.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        ) : null}
      </div>

      {/* The resolved window, always. Never make the reader infer it. */}
      {/* Compact by ~90px: the year is the current one and "UTC" is on the
          tooltip. The bar has to fit one line at 1366px or the page spends a
          second row on controls before showing a machine. */}
      <div className="muted oee-period-window"
           title={`${window.from} → ${window.to} UTC`}>
        {window.label} · {window.from.slice(5, 16)} → {window.to.slice(5, 16)}
      </div>
    </div>
  );
}
