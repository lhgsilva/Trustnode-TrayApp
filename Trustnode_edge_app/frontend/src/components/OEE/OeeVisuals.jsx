/* OEE visual building blocks: gauge, maturity badge, machine card, timeline.

   2026-08-29. Shared by the Overview, Machine Detail and the reusable
   dashboard widgets, so one machine card looks the same wherever it appears.

   Two rules run through all of it:

   * **Colour never carries meaning alone.** Every status colour is
     accompanied by its label, and every threshold tone by its number. The
     same rule the Diagnostics page follows.
   * **Never show a figure the data does not support.** A machine with no
     quality signal does not get an OEE with quality quietly assumed — it gets
     "Estimated OEE assuming Quality = 100%", or no number at all. */
import { useMemo } from "react";
import { pct, duration, num, StatePill, ConfidencePill } from "./OeeShared";

/* ---------------------------------------------------------- thresholds --- */
/* Defaults only. Configuration can override every one of these; hard-coding
   them would make the module useless to a plant whose "good" is 55%. */
export const DEFAULT_THRESHOLDS = {
  oee_good: 0.85, oee_warn: 0.60,
  availability_good: 0.90, performance_good: 0.95, quality_good: 0.99,
  energy_waste_high: 0.20,
};

export function toneFor(value, good, warn) {
  if (value === null || value === undefined) return "";
  if (value >= good) return "good";
  if (warn !== undefined && value >= warn) return "warn";
  return "bad";
}

/* ------------------------------------------------------- maturity label --- */
export const MATURITY_LABELS = {
  full: "Full OEE",
  availability_only: "Availability Only",
  no_quality: "OEE without Quality",
  no_performance: "OEE without Performance",
  estimated: "Estimated OEE",
  manual: "Manual Input",
  not_configured: "Not Configured",
  not_enough_data: "Not Enough Data",
  signal_conflict: "Signal Conflict",
};

/* What the module says about a result, in words the operator can act on.
   `assumption` spells out any factor taken as 100% — an unqualified 87% that
   silently assumed perfect quality is the number that gets a line blamed. */
export function MaturityBadge({ stage, missing = [], assumption = "" }) {
  const key = String(stage || "");
  if (!key) return null;
  const label = MATURITY_LABELS[key] || key;
  const partial = key !== "full";
  const detail = [
    missing.length ? `missing: ${missing.join(", ")}` : "",
    assumption || "",
  ].filter(Boolean).join(" · ");
  return (
    <span className={`oee-maturity ${partial ? "oee-maturity-partial" : "oee-maturity-full"}`}
      title={detail || label}>
      {label}{assumption ? ` — ${assumption}` : ""}
    </span>
  );
}

/* --------------------------------------------------------------- gauge --- */
/* A radial built from an SVG arc rather than a charting component: it renders
   at 44 px inside a card and at 160 px on a widget without a re-layout, and
   it costs nothing per instance on a grid of forty machines. */
export function Gauge({ value, size = 96, label = "", thresholds = DEFAULT_THRESHOLDS }) {
  const v = (value === null || value === undefined) ? null : Math.max(0, Math.min(1, value));
  const tone = toneFor(v, thresholds.oee_good, thresholds.oee_warn);
  const r = (size / 2) - 8;
  const circ = 2 * Math.PI * r;
  const dash = v === null ? 0 : circ * v;
  return (
    <div className="oee-gauge" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
        aria-label={`${label || "OEE"} ${v === null ? "no data" : pct(v)}`}>
        <circle cx={size / 2} cy={size / 2} r={r} className="oee-gauge-track" />
        {v === null ? null : (
          <circle cx={size / 2} cy={size / 2} r={r}
            className={`oee-gauge-fill ${tone ? `oee-gauge-${tone}` : ""}`}
            strokeDasharray={`${dash} ${circ - dash}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`} />
        )}
      </svg>
      <div className="oee-gauge-centre">
        <div className={`oee-gauge-value ${tone ? `oee-tone-${tone}` : ""}`}>
          {v === null ? "—" : pct(v, 0)}
        </div>
        {label ? <div className="oee-gauge-label">{label}</div> : null}
      </div>
    </div>
  );
}

/* Availability / Performance / Quality as three compact bars. Small enough for
   a card, readable from a control-room screen. */
export function ApqBars({ availability, performance, quality,
                          thresholds = DEFAULT_THRESHOLDS }) {
  const rows = [
    ["A", availability, thresholds.availability_good],
    ["P", performance, thresholds.performance_good],
    ["Q", quality, thresholds.quality_good],
  ];
  return (
    <div className="oee-apq">
      {rows.map(([k, v, good]) => (
        <div className="oee-apq-row" key={k}>
          <span className="oee-apq-key">{k}</span>
          <span className="oee-apq-track">
            <span className={`oee-apq-fill ${toneFor(v, good) ? `oee-apq-${toneFor(v, good)}` : ""}`}
              style={{ width: v === null || v === undefined ? 0 : `${Math.min(100, v * 100)}%` }} />
          </span>
          <span className="oee-apq-val">
            {v === null || v === undefined ? "—" : pct(v, 0)}
          </span>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------- machine card --- */
export function MachineCard({ card, result, onOpen, thresholds = DEFAULT_THRESHOLDS }) {
  const r = result || {};
  const oee = r.oee ?? null;
  const energy = r.energy || {};
  const wasteShare = Number(energy.total_kwh || 0) > 0
    ? Number(energy.wasted_kwh || 0) / Number(energy.total_kwh)
    : null;

  return (
    <button type="button" className="oee-machine-card" onClick={() => onOpen && onOpen(card)}
      title={`Open ${card.name || card.machine_id}`}>
      <div className="oee-mc-head">
        <div className="oee-mc-name">{card.name || card.machine_id}</div>
        <div className="muted oee-mc-line">{card.line || card.area || ""}</div>
      </div>

      <div className="oee-mc-body">
        <Gauge value={oee} size={92} label="OEE" thresholds={thresholds} />
        <div className="oee-mc-side">
          <ApqBars availability={r.availability} performance={r.performance}
            quality={r.quality} thresholds={thresholds} />
          <div className="oee-mc-state">
            <StatePill state={card.state} />
            {card.state_seconds !== undefined && card.state_seconds !== null
              ? <span className="muted oee-mc-dur">{duration(card.state_seconds)}</span>
              : null}
          </div>
        </div>
      </div>

      <div className="oee-mc-facts">
        <span><b>{duration(r.runtime_s)}</b> run</span>
        <span><b>{duration(r.downtime_s)}</b> down</span>
        <span><b>{num(r.total_count)}</b> made</span>
        {r.reject_count ? <span className="oee-mc-reject"><b>{num(r.reject_count)}</b> reject</span> : null}
        {card.power_kw !== null && card.power_kw !== undefined
          ? <span><b>{num(card.power_kw, 1)}</b> kW</span> : null}
        {wasteShare !== null && wasteShare >= thresholds.energy_waste_high
          ? <span className="oee-mc-waste">{pct(wasteShare, 0)} wasted</span> : null}
      </div>

      <div className="oee-mc-foot">
        <MaturityBadge stage={r.stage || card.stage} missing={r.missing_factors || []}
          assumption={r.assumption || ""} />
        <ConfidencePill confidence={card.confidence} source={card.source} />
      </div>
    </button>
  );
}

/* ------------------------------------------------------------ timeline --- */
/* A machine's day as coloured blocks. Positioned by percentage of the window
   rather than by pixel, so it reflows with the card and needs no measurement
   pass. Blocks are already merged server-side — a machine that ran for six
   hours is one block, not 21 600. */
export function StatusTimeline({ lanes = [], from, to, onBlockClick, height = 26 }) {
  const span = useMemo(() => {
    const a = Date.parse(String(from || "").replace(" ", "T") + "Z");
    const b = Date.parse(String(to || "").replace(" ", "T") + "Z");
    return (Number.isFinite(a) && Number.isFinite(b) && b > a) ? [a, b] : null;
  }, [from, to]);

  if (!span) return <div className="muted">No period selected.</div>;
  if (!lanes.length) return <div className="muted">No machines in this selection.</div>;
  const [a, b] = span;
  const total = b - a;

  return (
    <div className="oee-timeline">
      {lanes.map((lane) => (
        <div className="oee-tl-lane" key={lane.machine_id}>
          <div className="oee-tl-name" title={lane.machine_name}>{lane.machine_name}</div>
          <div className="oee-tl-track" style={{ height }}>
            {(lane.blocks || []).map((blk, i) => {
              const s = Date.parse(String(blk.start_utc || "").replace(" ", "T") + "Z");
              const e = blk.end_utc
                ? Date.parse(String(blk.end_utc).replace(" ", "T") + "Z") : b;
              if (!Number.isFinite(s)) return null;
              const left = Math.max(0, ((s - a) / total) * 100);
              const width = Math.max(0.2, (((Math.min(e, b)) - Math.max(s, a)) / total) * 100);
              if (width <= 0) return null;
              const title = [
                blk.state,
                blk.reason ? `reason: ${blk.reason}` : "",
                blk.planned ? "planned" : "",
                blk.source ? `source: ${blk.source}` : "",
              ].filter(Boolean).join(" · ");
              return (
                <button type="button" key={i}
                  className={`oee-tl-block oee-state-bg-${String(blk.state || "unknown")}`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={title}
                  onClick={(ev) => { ev.stopPropagation(); onBlockClick && onBlockClick(lane, blk); }} />
              );
            })}
          </div>
        </div>
      ))}
      {/* The legend is what makes the colours mean anything to a new reader. */}
      <div className="oee-tl-legend">
        {["running", "idle", "stopped", "faulted", "planned_stop", "changeover", "unknown"]
          .map((s) => (
            <span key={s} className="oee-tl-legend-item">
              <i className={`oee-state-bg-${s}`} /> {s.replace("_", " ")}
            </span>
          ))}
      </div>
    </div>
  );
}
