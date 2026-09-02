// I/O block status — an ifm IO-Link master (or any block whose gateway
// publishes Port<N>_Pin<2|4> tags) as one card: supply, link, and a row per
// port showing both pins with their live state.
//
// 2026-08-29. Written the day an AL1326 sat behind a green status for hours
// writing nothing but nulls, so the one rule this widget will not break is:
//
//     a BAD reading is NOT an OFF reading.
//
// quality < 192 renders as "—" in the muted colour and drives the port to
// Fault. It never renders as OFF, because "the input is low" and "we did not
// hear from the block" look identical to an operator otherwise, and only one
// of them means go and check the cable. Everything here is a consumer of tags
// the ifm driver already collects; nothing is computed or inferred.
import React, { useMemo } from "react";

const QUALITY_GOOD = 192;
// A block polled at 1 s that has said nothing for 30 s is not "OFF" either.
const STALE_MS = 30000;

const STATE = {
  ok: { label: "OK", cls: "ok" },
  warn: { label: "Warning", cls: "warn" },
  fault: { label: "Fault", cls: "fault" },
  idle: { label: "Inactive", cls: "idle" },
};

function rowTag(r) {
  return String(r?.tag_name || r?.tag || "");
}

function rowMs(r) {
  const t = r?.ts || r?.last_ts || r?.ts_utc || r?.timestamp || "";
  if (!t) return 0;
  const ms = Date.parse(String(t).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(t)) ? "" : "Z"));
  return Number.isFinite(ms) ? ms : 0;
}

/** Latest reading per tag, carrying quality and age — not just the value. */
function latestByTag(rows) {
  const out = {};
  for (const r of rows || []) {
    const tag = rowTag(r);
    if (!tag) continue;
    const ms = rowMs(r);
    const prev = out[tag];
    if (!prev || ms >= prev.ms) {
      out[tag] = {
        ms,
        // The dashboard's live projection calls these last_value / last_ts;
        // the app-store rows call them value / ts. Accept both rather than
        // depending on which layer handed us the row.
        value: r?.value ?? r?.last_value,
        text: r?.value_text,
        quality: Number(r?.quality ?? 0),
      };
    }
  }
  return out;
}

/** GOOD, and recent enough to still mean something.
 *
 * A row with no `quality` at all falls to "bad" rather than "good". That is
 * the deliberate direction to fail in: erring toward "go and look at the
 * block" is recoverable, erring toward a green card on a dead device is what
 * this widget exists to prevent. Both feeds that reach here were checked on
 * 2026-08-29 and do carry it - /api/app-store/live and .../historian.
 */
function readState(entry, nowMs) {
  if (!entry) return "missing";
  if (!(Number(entry.quality) >= QUALITY_GOOD)) return "bad";
  if (entry.ms && nowMs - entry.ms > STALE_MS) return "stale";
  return "good";
}

function fmtPin(entry, nowMs) {
  const st = readState(entry, nowMs);
  if (st === "missing") return { text: "—", sub: "", cls: "idle", title: "not collected" };
  if (st === "bad") {
    return {
      text: "—", sub: "no read", cls: "fault",
      title: "BAD quality — the block did not answer. This is NOT an OFF signal.",
    };
  }
  const raw = entry.value;
  const num = Number(raw);
  if (raw === null || raw === undefined || !Number.isFinite(num)) {
    const t = String(entry.text ?? "").trim();
    return { text: t || "—", sub: "", cls: t ? "on" : "idle", title: t };
  }
  // Digital pins are the common case: render them as ON/OFF with the raw
  // number underneath, the way the block's own web UI does.
  if (num === 0 || num === 1) {
    return {
      text: num === 1 ? "ON" : "OFF",
      sub: `(${num})`,
      cls: num === 1 ? "on" : "off",
      title: st === "stale" ? "last value is stale" : "",
    };
  }
  const shown = Math.abs(num) >= 100 ? num.toFixed(0) : num.toFixed(2);
  return { text: shown, sub: "", cls: "num", title: st === "stale" ? "last value is stale" : "" };
}

function fmtSupply(entry, kind, nowMs) {
  if (readState(entry, nowMs) !== "good") return null;
  const n = Number(entry.value);
  if (!Number.isFinite(n)) return null;
  // ifm masters report supply in mV and mA. 23517 -> 23.5 V, 66 -> 66 mA.
  if (kind === "voltage") return `${(n > 1000 ? n / 1000 : n).toFixed(1)} V`;
  if (kind === "current") return n >= 1000 ? `${(n / 1000).toFixed(2)} A` : `${n.toFixed(0)} mA`;
  return `${n.toFixed(0)} °C`;
}

/** Which interface the readings actually came in over.
 *
 * Evidence, not configuration: every row carries the driver that produced it.
 * A block has both a fieldbus and an IoT socket and we can only claim the one
 * the data is demonstrably arriving through.
 */
function transportOf(rows) {
  const seen = {};
  for (const r of rows || []) {
    const src = String(r?.source || "").trim().toLowerCase();
    if (src) seen[src] = (seen[src] || 0) + 1;
  }
  const best = Object.keys(seen).sort((a, b) => seen[b] - seen[a])[0] || "";
  if (best.includes("ifm") || best.includes("iot")) return "iot";
  if (best.includes("ethernet_ip") || best.includes("profinet")
      || best.includes("modbus") || best.includes("eip")) return "fieldbus";
  return "";
}

/** ON / OFF / unread, for a lamp. Same source of truth as the list. */
function lampState(entry, nowMs) {
  const st = readState(entry, nowMs);
  if (st === "missing") return "none";
  if (st === "bad") return "unread";
  const n = Number(entry.value);
  if (!Number.isFinite(n)) return "none";
  return n ? "on" : "off";
}

function Lamp({ pin, tag, state, onOpen, canChart }) {
  const words = { on: "ON", off: "OFF", unread: "no read", none: "not collected" }[state];
  return (
    <button
      type="button"
      className={`io-lamp ${state}`}
      title={`${tag} — ${words}${canChart ? " (click to chart)" : ""}`}
      aria-label={`${tag} ${words}`}
      onClick={() => onOpen(tag)}
      disabled={!canChart || state === "none"}
    >
      <i className="io-lamp-dot" />
      <span className="io-lamp-pin">{pin}</span>
    </button>
  );
}

export default function IoBlockWidget({
  widget,
  tagRowsByGateway,
  onOpenTagMonitor = null,
  gatewaysIndex = null,
}) {
  const cfg = widget?.config || {};
  const gid = String(cfg.gateway_id || "");
  const rows = (tagRowsByGateway && typeof tagRowsByGateway === "object" && tagRowsByGateway[gid])
    ? tagRowsByGateway[gid]
    : [];

  const model = useMemo(() => {
    const nowMs = Date.now();
    const seen = latestByTag(rows);

    // Keep only what is still being collected. The live cache never forgets a
    // tag, so an unticked port lingers with its last value and would be drawn
    // as a confident OFF. A tag is "still collected" when it is recent
    // RELATIVE to the newest tag on this block - absolute age would empty the
    // whole card the moment the block went offline, which is exactly when the
    // operator needs to see it.
    const newestMs = Object.values(seen).reduce((mx, e) => Math.max(mx, e.ms || 0), 0);
    const latest = {};
    let droppedTags = 0;
    for (const [tag, entry] of Object.entries(seen)) {
      if (newestMs && entry.ms && (newestMs - entry.ms) > STALE_MS) {
        droppedTags += 1;
        continue;
      }
      latest[tag] = entry;
    }
    const tags = Object.keys(latest);

    // Ports come from the tags that exist, so a block with 4 ports shows 4
    // rows and one with 8 shows 8 — no hard-coded port count.
    const portNums = new Set();
    for (const t of tags) {
      const m = /^Port(\d+)_Pin([24])$/.exec(t);
      if (m) portNums.add(Number(m[1]));
    }
    const ports = [...portNums].sort((a, b) => a - b).map((n) => {
      const pin4 = latest[`Port${n}_Pin4`];
      const pin2 = latest[`Port${n}_Pin2`];
      const present = [pin4, pin2].filter(Boolean);
      const states = present.map((e) => readState(e, nowMs));
      let status = STATE.idle;
      if (!present.length) status = STATE.idle;
      else if (states.every((s) => s === "bad")) status = STATE.fault;
      else if (states.some((s) => s === "bad" || s === "stale")) status = STATE.warn;
      else status = STATE.ok;
      // Mode is only shown when the gateway actually collects it — this
      // widget renders what was read and does not guess a port's mode.
      const modeEntry = latest[`Port${n}_Mode`];
      const modeTxt = modeEntry && readState(modeEntry, nowMs) === "good"
        ? ({ 0: "DEACTIVATED", 1: "DI", 2: "DO", 3: "IO-Link" }[Number(modeEntry.value)] ?? String(modeEntry.value))
        : "";
      return {
        n, status, modeTxt,
        pin4: { entry: pin4, tag: `Port${n}_Pin4`, fmt: fmtPin(pin4, nowMs),
                lamp: lampState(pin4, nowMs) },
        pin2: { entry: pin2, tag: `Port${n}_Pin2`, fmt: fmtPin(pin2, nowMs),
                lamp: lampState(pin2, nowMs) },
      };
    });

    // Everything that is NOT a port pin - the master supply, temperature, and
    // anything a future block adds. These keep their tag name and their chart
    // button in a strip at the foot of the face; the header summary above is a
    // glance, not a substitute for the tag.
    const portTagSet = new Set();
    for (const p of ports) {
      for (const pin of [p.pin4, p.pin2]) if (pin.entry) portTagSet.add(pin.tag);
    }
    // Supply and temperature have their own places now (X31 and the header),
    // so they must NOT also appear here. Anything else a block publishes
    // still gets a row - the strip is the catch-all, not the main event.
    const HOMED = new Set(["Master_Voltage", "Master_Current", "Master_Temperature"]);
    const deviceRows = tags.slice().sort()
      .filter((t) => !portTagSet.has(t) && !HOMED.has(t) && !/^Port\d+_Mode$/.test(t))
      .map((t) => ({ tag: t, fmt: fmtPin(latest[t], nowMs) }));

    const volts = fmtSupply(latest.Master_Voltage, "voltage", nowMs);
    const amps = fmtSupply(latest.Master_Current, "current", nowMs);
    const degs = fmtSupply(latest.Master_Temperature, "temp", nowMs);

    // Block-level verdict. Every reading BAD is the unplugged case and must
    // read as Fault, not as a block full of OFF inputs.
    const all = tags.map((t) => readState(latest[t], nowMs));
    let link = STATE.idle;
    if (!all.length) link = STATE.idle;
    else if (all.every((s) => s === "bad")) link = STATE.fault;
    else if (all.some((s) => s === "bad" || s === "stale")) link = STATE.warn;
    else link = STATE.ok;

    const transport = transportOf(rows);
    // lampState already answers this: "on" when the supply reads GOOD and
    // non-zero, "unread" when it came back BAD, "none" when it is not
    // collected. A zero-volt supply correctly reads "off".
    const powerLamp = lampState(latest.Master_Voltage, nowMs);
    const newest = tags.reduce((mx, t) => Math.max(mx, latest[t]?.ms || 0), 0);
    const ageS = newest ? Math.max(0, Math.round((nowMs - newest) / 1000)) : null;
    // Which supply tags actually exist, so the chart buttons on X31 and the
    // header are only offered for values we really collect.
    const hasV = Boolean(latest.Master_Voltage);
    const hasA = Boolean(latest.Master_Current);
    const hasT = Boolean(latest.Master_Temperature);
    return { ports, deviceRows, volts, amps, degs, link, ageS, transport,
             powerLamp, hasV, hasA, hasT, droppedTags, tagCount: tags.length };
  }, [rows]);

  const gwName = String(
    (gatewaysIndex && gid && (gatewaysIndex[gid]?.name || gatewaysIndex[gid]?.gateway_name)) || cfg.block_name || ""
  );

  const openTag = (tagName) => {
    if (typeof onOpenTagMonitor !== "function" || !gid || !tagName) return;
    // A synthetic widget: the handler builds its series from config, so this
    // reuses the dashboard's existing tag-monitor path rather than adding a
    // second way to open a chart.
    onOpenTagMonitor({
      id: `${widget?.id || "io"}-${tagName}`,
      type: "line_chart",
      title: `${tagName}${gwName ? ` — ${gwName}` : ""}`,
      config: {
        gateway_id: gid,
        tag_name: tagName,
        readings_count: Number(cfg.readings_count || 120),
        interpolation: "stepAfter",
      },
    });
  };

  if (!gid) {
    return <div className="io-block-empty">Pick the block's gateway in the widget settings.</div>;
  }
  if (!model.tagCount) {
    return <div className="io-block-empty">No readings yet for this gateway.</div>;
  }

  const canChart = typeof onOpenTagMonitor === "function";
  // Body colour. Orange by default because that is how the device is
  // recognised on the wall; "auto" follows the app's light/dark theme for
  // operators who would rather the card sat quietly in the dashboard.
  const bodyColor = ["orange", "grey", "black", "auto"].includes(String(cfg.io_block_color || ""))
    ? String(cfg.io_block_color)
    : "orange";

  const ChartBtn = ({ tag, disabled }) => (
    <button
      type="button"
      className="io-block-chart-btn"
      title={`Open ${tag} in the tag monitor`}
      aria-label={`Open ${tag} in the tag monitor`}
      onClick={() => openTag(tag)}
      disabled={disabled}
    >
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none"
           stroke="currentColor" strokeWidth="2" strokeLinecap="round"
           strokeLinejoin="round" aria-hidden="true">
        <path d="M3 3v18h18" />
        <path d="m7 14 3-4 3 3 5-7" />
      </svg>
    </button>
  );

  const PinLine = ({ pin, num }) => (
    <div className="io-pin">
      <Lamp pin={num} tag={pin.tag} state={pin.lamp} onOpen={openTag} canChart={canChart} />
      <span className="io-pin-tag" title={pin.tag}>{pin.tag}</span>
      <span className={`io-pin-val ${pin.fmt.cls}`} title={pin.fmt.title}>
        {pin.fmt.text}
        {pin.fmt.sub ? <em className="io-block-pin-sub">{pin.fmt.sub}</em> : null}
      </span>
      <ChartBtn tag={pin.tag} disabled={!canChart || !pin.entry} />
    </div>
  );

  return (
    <div className="io-block">
      <div className="io-face" aria-label="Block front, ports in physical order">
        <div className={`io-face-body io-${bodyColor}`}>
          <div className="io-face-head">
            {gwName ? <div className="io-face-name">{gwName}</div> : null}
            <div className="io-face-stats">
              <div className="io-block-stat">
                <span className="io-block-stat-k">Temperature</span>
                <span className="io-block-stat-v">
                  {model.degs || "—"}
                  {model.hasT ? <ChartBtn tag="Master_Temperature" disabled={!canChart} /> : null}
                </span>
              </div>
              <div className="io-block-stat">
                <span className="io-block-stat-k">Block</span>
                <span className="io-block-stat-v">{model.link.label}</span>
              </div>
              <div className="io-block-stat">
                <span className="io-block-stat-k">Last read</span>
                <span className="io-block-stat-v">{model.ageS === null ? "—" : `${model.ageS}s ago`}</span>
              </div>
            </div>
          </div>
          <div className="io-face-conns">
            {[
              // Fieldbus pair on the top row, then power / IoT beneath, in the
              // order they sit on the block.
              { id: "X21", kind: "Fieldbus", lit: model.transport === "fieldbus", pins: 4 },
              { id: "X22", kind: "Fieldbus", lit: model.transport === "fieldbus", pins: 4 },
              // Power left, IoT right on the second row.
              { id: "X31", kind: "Power",    lit: model.powerLamp === "on",       pins: 5,
                note: model.volts ? `${model.volts}${model.amps ? ` · ${model.amps}` : ""}` : "",
                charts: [model.hasV ? "Master_Voltage" : "", model.hasA ? "Master_Current" : ""] },
              { id: "X23", kind: "IoT",      lit: model.transport === "iot",      pins: 4 },
            ].map((c) => {
              // Fresh data over this interface is the only thing that lights
              // it. Anything else is "not collected" - the block publishes no
              // link tag, and a green lamp we cannot justify is worse than none.
              const state = c.lit && model.link.cls === "ok" ? "on"
                : c.lit ? "unread" : "none";
              const why = state === "on"
                ? "readings are arriving over this interface"
                : state === "unread"
                  ? "this is the interface in use, but nothing is reading"
                  : "not collected - the block publishes no link tag for this socket";
              return (
                <div key={c.id} className={`io-conn ${state}`} title={`${c.id} ${c.kind} — ${why}`}>
                  <span className={`io-port-socket io-conn-socket p${c.pins}`} aria-hidden="true">
                    <i /><i /><i /><i />{c.pins === 5 ? <i /> : null}
                  </span>
                  {/* Lamp immediately after the socket, where the port pins
                      put theirs - the eye then finds every lamp on the face
                      in the same place. */}
                  <i className="io-lamp-dot io-conn-lamp" />
                  <span className="io-conn-text">
                    <span className="io-conn-id">{c.id}</span>
                    <span className="io-conn-kind">{c.note || c.kind}</span>
                  </span>
                  {(c.charts || []).filter(Boolean).length ? (
                    <span className="io-conn-acts">
                      {(c.charts || []).filter(Boolean).map((t) => (
                        <ChartBtn key={t} tag={t} disabled={!canChart} />
                      ))}
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
          {model.ports.map((p) => (
            <div key={p.n} className="io-port">
              <div className="io-port-head">
                <span className="io-port-socket" aria-hidden="true">
                  <i /><i /><i /><i /><i />
                </span>
                <span className="io-port-label">X{p.n}</span>
                {p.modeTxt ? <span className="io-port-mode">{p.modeTxt}</span> : null}
              </div>
              <div className="io-port-pins">
                <PinLine pin={p.pin4} num="4" />
                <PinLine pin={p.pin2} num="2" />
              </div>
            </div>
          ))}
          {model.droppedTags ? (
          <div className="io-face-note">
            {model.droppedTags} tag(s) not currently collected are hidden
          </div>
        ) : null}
        {model.deviceRows.length ? (
          <div className="io-face-device">
            {model.deviceRows.map((r) => (
              <div key={r.tag} className="io-pin io-pin-device">
                <span className="io-pin-tag" title={r.tag}>{r.tag}</span>
                <span className={`io-pin-val ${r.fmt.cls}`} title={r.fmt.title}>
                  {r.fmt.text}
                  {r.fmt.sub ? <em className="io-block-pin-sub">{r.fmt.sub}</em> : null}
                </span>
                <ChartBtn tag={r.tag} disabled={!canChart} />
              </div>
            ))}
          </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
