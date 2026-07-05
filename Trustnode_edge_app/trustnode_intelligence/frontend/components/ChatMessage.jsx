import React, { useMemo } from "react";

// Extract one or more top-level JSON objects from a free-form string by
// tracking brace depth + string literals. Used to recover multiple chart
// payloads when the LLM concatenates them inside a single fenced block.
function extractJsonObjects(src) {
  const out = [];
  if (!src) return out;
  const s = String(src);
  let i = 0, depth = 0, start = -1, inStr = false, esc = false;
  while (i < s.length) {
    const c = s[i];
    if (inStr) {
      if (esc) { esc = false; }
      else if (c === "\\") { esc = true; }
      else if (c === '"') { inStr = false; }
    } else {
      if (c === '"') inStr = true;
      else if (c === "{") {
        if (depth === 0) start = i;
        depth++;
      } else if (c === "}") {
        depth--;
        if (depth === 0 && start >= 0) {
          const chunk = s.slice(start, i + 1);
          try { out.push(JSON.parse(chunk)); } catch { /* skip */ }
          start = -1;
        }
      }
    }
    i++;
  }
  return out;
}

// Format a UTC ISO-ish string ('2026-06-30 16:02:00Z' or '2026-06-30T16:02:00Z')
// as a friendly LOCAL-time label for chart subtitles. Falls back to the raw
// string if parsing fails. Browser/Electron timezone is the user's local TZ.
function fmtWindowSide(s) {
  if (!s) return "";
  try {
    let str = String(s).trim();
    // Tool returns "YYYY-MM-DD HH:MM:SSZ" (sqlite text). Make it ISO-friendly.
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z?$/.test(str)) {
      str = str.replace(" ", "T");
      if (!str.endsWith("Z")) str += "Z";
    }
    const d = new Date(str);
    if (isNaN(d.getTime())) return String(s);
    return d.toLocaleString(undefined, {
      month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return String(s);
  }
}

// SVG line chart with multi-series + dual-axis support — no external dep.
// Operator 2026-06-30: the LLM targets this via ```trustnode-chart fenced
// blocks. Two payload shapes are accepted:
//
//   SINGLE:  {tag, gateway_name, from, to, min, max, avg, series:[{ts,value},...]}
//   MULTI:   {from, to, series:[
//              {tag, gateway_name, axis:'left'|'right'?, color?, series:[{ts,value},...]},
//              ... up to ~6
//            ]}
//
// Dual-axis is auto-applied if any series' value range is >5x different
// from the dominant range, unless the LLM explicitly set axis: 'right'.
const CHART_PALETTE = [
  "#14b8a6", "#f97316", "#3b82f6", "#a855f7", "#eab308", "#ef4444",
];

function _toMultiShape(data) {
  // Normalize to: { from, to, series: [{tag, gateway_name, axis?, color?, points:[{ts,value}]}], ... }
  if (!data || typeof data !== "object") return null;
  if (Array.isArray(data.series) && data.series.length && data.series[0] && Array.isArray(data.series[0].series)) {
    // Already multi-series.
    return {
      from: data.from, to: data.to,
      series: data.series.map((s, i) => ({
        tag: s.tag || s.name || `series ${i + 1}`,
        gateway_name: s.gateway_name || "",
        color: s.color || CHART_PALETTE[i % CHART_PALETTE.length],
        axisHint: s.axis === "right" || s.axis === "left" ? s.axis : null,
        points: (s.series || []).map((p) => ({ ts: Number(p.ts), value: Number(p.value) }))
          .filter((p) => isFinite(p.ts) && isFinite(p.value)),
      })),
    };
  }
  // Single-series: wrap into multi shape.
  if (Array.isArray(data.series) && data.series.length && data.series[0] && "value" in data.series[0]) {
    return {
      from: data.from, to: data.to,
      series: [{
        tag: data.tag || "series",
        gateway_name: data.gateway_name || "",
        color: CHART_PALETTE[0],
        axisHint: null,
        points: data.series.map((p) => ({ ts: Number(p.ts), value: Number(p.value) }))
          .filter((p) => isFinite(p.ts) && isFinite(p.value)),
        min: data.min, max: data.max, avg: data.avg,
      }],
    };
  }
  return null;
}

function _assignAxes(seriesList) {
  // Honour explicit axisHint first. For the rest, put on 'left' by default,
  // then move any series whose range midpoint is >5x off from the median
  // left-axis midpoint over to 'right'.
  const ranges = seriesList.map((s) => {
    if (!s.points.length) return { mid: 0, span: 0 };
    const vs = s.points.map((p) => p.value);
    const mn = Math.min(...vs), mx = Math.max(...vs);
    return { mid: (mn + mx) / 2, span: mx - mn || 1 };
  });
  const result = seriesList.map((s, i) => ({ ...s, axis: s.axisHint || "left", _range: ranges[i] }));
  // Find the median midpoint of the left series.
  const leftMids = result.filter((s) => s.axis === "left").map((s) => Math.abs(s._range.mid) + 1);
  if (leftMids.length === 0) return result;
  leftMids.sort((a, b) => a - b);
  const medianLeft = leftMids[Math.floor(leftMids.length / 2)];
  for (const s of result) {
    if (s.axisHint) continue; // user-set wins
    const mid = Math.abs(s._range.mid) + 1;
    const ratio = mid / medianLeft;
    if (ratio > 5 || ratio < 0.2) {
      s.axis = "right";
    }
  }
  return result;
}

// Compact numeric formatter shared by bar/donut labels.
function _fmtVal(v) {
  const n = Number(v);
  if (!isFinite(n)) return String(v ?? "");
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (Math.abs(n) >= 10) return n.toFixed(1);
  return n.toFixed(2);
}

// ---- BAR chart (categorical or time-bucketed) --------------------------
// Accepts:
//   slices form: {chart_type:'bar', slices:[{label,value}], agg?}
//   series form: {chart_type:'bar', tag, series:[{ts,value}]}  (bucketed bars)
function BarChart({ data, big = false }) {
  const W = big ? 1100 : 640;
  const H = big ? 460 : 260;
  const pad = { l: big ? 64 : 52, r: big ? 24 : 16, t: big ? 24 : 18, b: big ? 96 : 74 };

  // Normalize to [{label, value, color}].
  let bars = [];
  if (Array.isArray(data.slices) && data.slices.length) {
    bars = data.slices
      .map((s, i) => ({ label: String(s.label ?? `#${i + 1}`), value: Number(s.value), color: CHART_PALETTE[i % CHART_PALETTE.length] }))
      .filter((b) => isFinite(b.value));
  } else if (Array.isArray(data.series) && data.series.length) {
    // Bucketed time series → bars per bucket. Label each by local time.
    const spanH = data.series.length > 1
      ? (Number(data.series[data.series.length - 1].ts) - Number(data.series[0].ts)) / 3600000 : 0;
    bars = data.series
      .map((p, i) => {
        let lab = "";
        try {
          const d = new Date(Number(p.ts));
          lab = d.toLocaleString(undefined, spanH > 24
            ? { month: "short", day: "2-digit", hour: "2-digit" }
            : { hour: "2-digit", minute: "2-digit" });
        } catch { lab = String(i + 1); }
        return { label: lab, value: Number(p.value), color: CHART_PALETTE[0] };
      })
      .filter((b) => isFinite(b.value));
  }

  if (!bars.length) {
    return (
      <div style={{ border: "1px solid var(--stroke)", borderRadius: 8, padding: 16, color: "var(--muted)", fontSize: 12 }}>
        Bar chart: no data to plot.
      </div>
    );
  }

  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const vals = bars.map((b) => b.value);
  let vMax = Math.max(...vals, 0);
  let vMin = Math.min(...vals, 0);
  if (vMax === vMin) { vMax += 1; }
  const yOf = (v) => pad.t + innerH - ((v - vMin) / (vMax - vMin)) * innerH;
  const zeroY = yOf(0);
  const n = bars.length;
  const slot = innerW / n;
  const bw = Math.max(4, Math.min(slot * 0.7, big ? 90 : 60));
  const ticks = [vMax, (vMax + vMin) / 2, vMin];
  // Show every k-th label if crowded.
  const labStep = Math.ceil(n / (big ? 24 : 14));

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: "block" }}>
      {/* Y grid + labels */}
      {ticks.map((t, i) => {
        const y = yOf(t);
        return (
          <g key={`g${i}`}>
            <line x1={pad.l} y1={y} x2={W - pad.r} y2={y} stroke="var(--stroke)" strokeWidth="1" strokeDasharray="2,3" opacity="0.6" />
            <text x={pad.l - 6} y={y + 3} fill="var(--muted)" fontSize="10" textAnchor="end" fontFamily="sans-serif">{_fmtVal(t)}</text>
          </g>
        );
      })}
      {/* zero baseline (if data spans negatives) */}
      {vMin < 0 ? <line x1={pad.l} y1={zeroY} x2={W - pad.r} y2={zeroY} stroke="var(--muted)" strokeWidth="1" opacity="0.8" /> : null}
      {bars.map((b, i) => {
        const x = pad.l + i * slot + (slot - bw) / 2;
        const top = yOf(Math.max(b.value, 0));
        const bot = yOf(Math.min(b.value, 0));
        const h = Math.max(1, bot - top);
        const showLab = (i % labStep) === 0;
        return (
          <g key={`b${i}`}>
            <rect x={x} y={top} width={bw} height={h} fill={b.color} rx="2">
              <title>{b.label}: {_fmtVal(b.value)}</title>
            </rect>
            {/* value on top of bar */}
            <text x={x + bw / 2} y={top - 4} fill="var(--text)" fontSize={big ? 11 : 9} textAnchor="middle" fontFamily="sans-serif">{_fmtVal(b.value)}</text>
            {/* category label (rotated when many) */}
            {showLab ? (
              <text
                x={x + bw / 2} y={H - pad.b + 14}
                fill="var(--muted)" fontSize={big ? 11 : 9.5}
                textAnchor="end" fontFamily="sans-serif"
                transform={`rotate(-40 ${x + bw / 2} ${H - pad.b + 14})`}
              >{b.label.length > 22 ? b.label.slice(0, 21) + "…" : b.label}</text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

// ---- DONUT / PIE chart -------------------------------------------------
// Accepts {chart_type:'donut', slices:[{label,value,pct?}], total?}.
function DonutChart({ data, big = false }) {
  const size = big ? 320 : 200;
  const cx = size / 2, cy = size / 2;
  const rOuter = size / 2 - 6;
  const rInner = rOuter * 0.58; // donut hole (0 would be a full pie)
  const raw = (data.slices || [])
    .map((s, i) => ({ label: String(s.label ?? `#${i + 1}`), value: Math.max(0, Number(s.value) || 0),
                      pct: (typeof s.pct === "number" ? s.pct : null), color: CHART_PALETTE[i % CHART_PALETTE.length] }))
    .filter((s) => s.value > 0);
  const total = raw.reduce((a, b) => a + b.value, 0);

  if (!raw.length || total <= 0) {
    return (
      <div style={{ border: "1px solid var(--stroke)", borderRadius: 8, padding: 16, color: "var(--muted)", fontSize: 12 }}>
        Donut: no data to plot.
      </div>
    );
  }

  // Build arc paths.
  const arc = (startFrac, endFrac) => {
    const a0 = startFrac * 2 * Math.PI - Math.PI / 2;
    const a1 = endFrac * 2 * Math.PI - Math.PI / 2;
    const large = (endFrac - startFrac) > 0.5 ? 1 : 0;
    const x0 = cx + rOuter * Math.cos(a0), y0 = cy + rOuter * Math.sin(a0);
    const x1 = cx + rOuter * Math.cos(a1), y1 = cy + rOuter * Math.sin(a1);
    const xi1 = cx + rInner * Math.cos(a1), yi1 = cy + rInner * Math.sin(a1);
    const xi0 = cx + rInner * Math.cos(a0), yi0 = cy + rInner * Math.sin(a0);
    return `M ${x0} ${y0} A ${rOuter} ${rOuter} 0 ${large} 1 ${x1} ${y1} `
         + `L ${xi1} ${yi1} A ${rInner} ${rInner} 0 ${large} 0 ${xi0} ${yi0} Z`;
  };
  let acc = 0;
  const segs = raw.map((s) => {
    const start = acc / total;
    acc += s.value;
    const end = acc / total;
    // A single 100% slice can't draw as an arc (start==end after wrap) — draw a ring instead.
    const isFull = raw.length === 1 || (end - start) >= 0.9999;
    return { ...s, start, end, isFull, pctCalc: (s.pct != null ? s.pct : (s.value / total) * 100) };
  });

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: big ? 28 : 16, justifyContent: "center" }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ flex: "0 0 auto" }}>
        {segs.map((s, i) => (
          s.isFull ? (
            <g key={`f${i}`}>
              <circle cx={cx} cy={cy} r={rOuter} fill={s.color} />
              <circle cx={cx} cy={cy} r={rInner} fill="var(--card)" />
            </g>
          ) : (
            <path key={`s${i}`} d={arc(s.start, s.end)} fill={s.color} stroke="var(--card)" strokeWidth="1.5">
              <title>{s.label}: {_fmtVal(s.value)} ({s.pctCalc.toFixed(1)}%)</title>
            </path>
          )
        ))}
        {/* center total */}
        <text x={cx} y={cy - 2} fill="var(--text)" fontSize={big ? 16 : 12} fontWeight="700" textAnchor="middle" fontFamily="sans-serif">{_fmtVal(total)}</text>
        <text x={cx} y={cy + (big ? 16 : 13)} fill="var(--muted)" fontSize={big ? 11 : 9} textAnchor="middle" fontFamily="sans-serif">total</text>
      </svg>
      {/* legend */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 140 }}>
        {segs.map((s, i) => (
          <div key={`l${i}`} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: big ? 13 : 11.5 }}>
            <span style={{ width: 11, height: 11, borderRadius: 2, background: s.color, flex: "0 0 auto" }} />
            <span style={{ color: "var(--text)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.label}</span>
            <span style={{ color: "var(--muted)", fontVariantNumeric: "tabular-nums" }}>{s.pctCalc.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TrustnodeChart({ data, big = false }) {
  // Operator 2026-07-03: `big` renders a larger canvas for the expand modal.
  // The small inline chart stays 640x220; the modal uses a much taller/wider
  // viewBox so operators can read the trend for analysis. Everything else
  // (scales, axes, series paths) is identical — only the dimensions change.
  const W = big ? 1100 : 640;
  const H = big ? 460 : 220;
  const pad = { l: big ? 64 : 50, r: big ? 64 : 50, t: big ? 24 : 18, b: big ? 38 : 28 };
  const [expanded, setExpanded] = React.useState(false);
  // Hooks must run unconditionally (before any early return) — compute the
  // line-shape memos here even if we end up rendering a bar/donut instead.
  const norm = useMemo(() => _toMultiShape(data), [data]);
  const series = useMemo(() => (norm ? _assignAxes(norm.series) : []), [norm]);

  // DISPATCH on chart_type. Absent/'line' → the SVG line chart below (unchanged).
  // 'bar' / 'donut' render their own components, wrapped in the same card chrome
  // + expand-to-modal button so the UX is consistent across chart types.
  const ctype = String(data?.chart_type || "line").toLowerCase();
  if (ctype === "bar" || ctype === "donut") {
    const title = (() => {
      if (ctype === "donut") {
        const by = data.by ? ` by ${data.by === "value_bands" ? "value band" : data.by}` : "";
        return `Breakdown${by}${data.tag ? ` — ${data.tag}` : ""}`;
      }
      const agg = data.agg ? `${String(data.agg).charAt(0).toUpperCase()}${String(data.agg).slice(1)} ` : "";
      return data.tag ? `${data.tag} — ${agg}bars` : `${agg}by tag`;
    })();
    const inner = ctype === "donut"
      ? <DonutChart data={data} big={big} />
      : <BarChart data={data} big={big} />;
    return (
      <div style={{
        margin: "8px 0", padding: big ? 4 : 10,
        border: big ? "none" : "1px solid var(--stroke)", borderRadius: 8,
        background: big ? "transparent" : "var(--bg)",
      }}>
        <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6, display: "flex", alignItems: "center", gap: 12 }}>
          <strong style={{ color: "var(--text)" }}>{title}</strong>
          {(data.from && data.to) ? <span>{fmtWindowSide(data.from)} → {fmtWindowSide(data.to)}</span> : null}
          {!big ? (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              title="Open chart in a larger view"
              style={{
                marginLeft: "auto", border: "1px solid var(--stroke)",
                background: "color-mix(in srgb, var(--teal, #14a89a) 12%, transparent)",
                color: "var(--text)", cursor: "pointer", fontSize: 11,
                padding: "2px 8px", borderRadius: 5, display: "inline-flex", alignItems: "center", gap: 5,
              }}
            ><span style={{ fontSize: 13, lineHeight: 1 }}>⤢</span> Expand</button>
          ) : null}
        </div>
        {inner}
        {expanded && !big ? (
          <ChartModal onClose={() => setExpanded(false)}>
            <TrustnodeChart data={data} big />
          </ChartModal>
        ) : null}
      </div>
    );
  }

  if (!norm || !series.length || series.every((s) => !s.points.length)) {
    return (
      <div style={{
        border: "1px solid var(--stroke)", borderRadius: 8,
        padding: 16, color: "var(--muted)", fontSize: 12,
      }}>
        Chart: no samples in the requested window
        {data?.tag ? ` for ${data.tag}` : ""}.
      </div>
    );
  }

  // Compute x range across all series + y ranges per axis.
  const allXs = series.flatMap((s) => s.points.map((p) => p.ts));
  const xMin = Math.min(...allXs), xMax = Math.max(...allXs);
  const axes = { left: [], right: [] };
  for (const s of series) axes[s.axis].push(...s.points.map((p) => p.value));
  const computeAxis = (vals) => {
    if (!vals.length) return null;
    let mn = Math.min(...vals), mx = Math.max(...vals);
    if (mn === mx) { mn -= 1; mx += 1; }
    const pad = (mx - mn) * 0.08 || 1;
    return { min: mn - pad, max: mx + pad };
  };
  const leftAxis = computeAxis(axes.left);
  const rightAxis = computeAxis(axes.right);

  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const xScale = (v) => pad.l + (xMax === xMin ? 0 : ((v - xMin) / (xMax - xMin)) * innerW);
  const yScaleFor = (axisName) => {
    const a = axisName === "right" ? rightAxis : leftAxis;
    if (!a) return () => pad.t + innerH / 2;
    return (v) => pad.t + innerH - ((v - a.min) / (a.max - a.min)) * innerH;
  };

  const fmtTs = (ms) => {
    try {
      const d = new Date(ms);
      // Span >24h → show day; else show time only.
      const spanH = (xMax - xMin) / 3600000;
      return d.toLocaleString(undefined, spanH > 24
        ? { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }
        : { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch { return ""; }
  };
  const fmtY = (v) => {
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    if (Math.abs(n) >= 1000) return n.toFixed(0);
    if (Math.abs(n) >= 10) return n.toFixed(1);
    return n.toFixed(2);
  };

  const ticksFor = (a) => a ? [a.max, (a.max + a.min) / 2, a.min] : [];
  const leftTicks = ticksFor(leftAxis);
  const rightTicks = ticksFor(rightAxis);

  return (
    <div style={{
      margin: "8px 0", padding: big ? 4 : 10,
      border: big ? "none" : "1px solid var(--stroke)", borderRadius: 8,
      background: big ? "transparent" : "var(--bg)",
    }}>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
        <span>
          {(norm.from && norm.to) ? <>{fmtWindowSide(norm.from)} → {fmtWindowSide(norm.to)}</> : null}
        </span>
        {series.map((s, i) => (
          <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: big ? 13 : 11 }}>
            <span style={{
              width: 10, height: 2, background: s.color, display: "inline-block", borderRadius: 1,
            }} />
            <strong style={{ color: "var(--text)" }}>{s.tag}</strong>
            {s.gateway_name ? <span style={{ opacity: 0.7 }}>@{s.gateway_name}</span> : null}
            {rightAxis && s.axis === "right" ? <span style={{ opacity: 0.6 }}>(R)</span> : null}
          </span>
        ))}
        {/* Expand-to-modal button (inline chart only). */}
        {!big ? (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            title="Open chart in a larger view for analysis"
            style={{
              marginLeft: "auto", border: "1px solid var(--stroke)",
              background: "color-mix(in srgb, var(--teal, #14a89a) 12%, transparent)",
              color: "var(--text)", cursor: "pointer", fontSize: 11,
              padding: "2px 8px", borderRadius: 5, display: "inline-flex",
              alignItems: "center", gap: 5,
            }}
          >
            <span style={{ fontSize: 13, lineHeight: 1 }}>⤢</span> Expand
          </button>
        ) : null}
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: "block" }}>
        {/* Left axis grid + labels */}
        {leftTicks.map((t, i) => {
          const y = yScaleFor("left")(t);
          return (
            <g key={`L${i}`}>
              <line x1={pad.l} y1={y} x2={W - pad.r} y2={y}
                    stroke="var(--stroke)" strokeWidth="1" strokeDasharray="2,3" opacity="0.6" />
              <text x={pad.l - 6} y={y + 3} fill="var(--muted)" fontSize="10" textAnchor="end" fontFamily="sans-serif">{fmtY(t)}</text>
            </g>
          );
        })}
        {/* Right axis labels (if any) */}
        {rightTicks.map((t, i) => {
          const y = yScaleFor("right")(t);
          return (
            <text key={`R${i}`} x={W - pad.r + 6} y={y + 3} fill="var(--muted)" fontSize="10" textAnchor="start" fontFamily="sans-serif">{fmtY(t)}</text>
          );
        })}
        {/* X labels */}
        <text x={pad.l} y={H - 6} fill="var(--muted)" fontSize="10" textAnchor="start" fontFamily="sans-serif">{fmtTs(xMin)}</text>
        <text x={W - pad.r} y={H - 6} fill="var(--muted)" fontSize="10" textAnchor="end" fontFamily="sans-serif">{fmtTs(xMax)}</text>
        {/* Each series as its own path */}
        {series.map((s, i) => {
          if (!s.points.length) return null;
          const ys = yScaleFor(s.axis);
          const d = s.points.map((p, j) =>
            `${j === 0 ? "M" : "L"} ${xScale(p.ts).toFixed(1)} ${ys(p.value).toFixed(1)}`
          ).join(" ");
          return (
            <g key={`s${i}`}>
              <path d={d} fill="none" stroke={s.color} strokeWidth="1.6" />
            </g>
          );
        })}
      </svg>
      {/* Per-series stats footer (only if single series carries them; else summarize) */}
      <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4, display: "flex", flexWrap: "wrap", gap: 12 }}>
        {series.map((s, i) => {
          const vs = s.points.map((p) => p.value);
          if (!vs.length) return null;
          const mn = Math.min(...vs), mx = Math.max(...vs);
          const avg = vs.reduce((a, b) => a + b, 0) / vs.length;
          return (
            <span key={i}>
              <strong style={{ color: s.color }}>{s.tag}</strong>: min {fmtY(mn)} · max {fmtY(mx)} · avg {fmtY(avg)} · {vs.length} pts
            </span>
          );
        })}
      </div>

      {/* Expand modal: a large, centered popup of the SAME chart for analysis.
          Click the backdrop or the × (or press Esc) to close. Rendered only
          for the inline chart (big=false) so the modal's own chart doesn't
          recurse another modal. */}
      {expanded && !big ? (
        <ChartModal onClose={() => setExpanded(false)}>
          <TrustnodeChart data={data} big />
        </ChartModal>
      ) : null}
    </div>
  );
}

// Full-screen centered modal for the expanded chart. Closes on backdrop click,
// the × button, or the Escape key. No external dependency.
function ChartModal({ children, onClose }) {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    // Prevent background scroll while open.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 9999,
        background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "relative", width: "min(1200px, 96vw)", maxHeight: "92vh",
          overflow: "auto", background: "var(--card)",
          border: "1px solid var(--stroke)", borderRadius: 12,
          padding: 18, boxShadow: "0 12px 48px rgba(0,0,0,0.5)",
        }}
      >
        <button
          type="button"
          onClick={onClose}
          title="Close (Esc)"
          style={{
            position: "absolute", top: 10, right: 12, zIndex: 1,
            border: "1px solid var(--stroke)", background: "var(--bg)",
            color: "var(--text)", cursor: "pointer", fontSize: 16,
            width: 30, height: 30, borderRadius: 6, lineHeight: 1,
          }}
        >×</button>
        {children}
      </div>
    </div>
  );
}

/* Lightweight Markdown renderer — no external dep. Handles:
   - headings (#, ##, ###)
   - bold **text** + inline `code`
   - bullet lists (- or *)
   - tables (| col | col |)
   - paragraphs
   - fenced trustnode-chart blocks → inline SVG chart
   Enough for the engineering-memo style we ask the LLM to produce. */
function renderMarkdown(src) {
  if (!src) return null;
  const text = String(src).replace(/\r\n/g, "\n");

  // Inline transforms: **bold**, `code`
  const renderInline = (s) => {
    const out = [];
    let i = 0, key = 0;
    while (i < s.length) {
      if (s[i] === "*" && s[i + 1] === "*") {
        const end = s.indexOf("**", i + 2);
        if (end > 0) {
          out.push(<strong key={`b${key++}`}>{s.slice(i + 2, end)}</strong>);
          i = end + 2;
          continue;
        }
      }
      if (s[i] === "`") {
        const end = s.indexOf("`", i + 1);
        if (end > 0) {
          out.push(
            <code key={`c${key++}`} style={{
              background: "color-mix(in srgb, var(--teal, #14a89a) 15%, transparent)",
              color: "var(--text)",
              padding: "1px 5px",
              borderRadius: 3, fontSize: "0.92em",
            }}>{s.slice(i + 1, end)}</code>
          );
          i = end + 1;
          continue;
        }
      }
      // Text run until next special char.
      const nextStar = s.indexOf("**", i);
      const nextTick = s.indexOf("`", i);
      let nextSpecial = Math.min(
        nextStar < 0 ? Infinity : nextStar,
        nextTick < 0 ? Infinity : nextTick,
      );
      if (!isFinite(nextSpecial)) nextSpecial = s.length;
      out.push(s.slice(i, nextSpecial));
      i = nextSpecial;
    }
    return out;
  };

  const lines = text.split("\n");
  const blocks = [];
  let i = 0;
  let blockKey = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line → flush
    if (!line.trim()) { i++; continue; }

    // Fenced code block. We try hardest to render charts:
    //   1. ```trustnode-chart ...```  (preferred, explicit)
    //   2. ```json ...``` OR ``` ...```  → if the JSON parses AND has a
    //      `series` array AND a `tag` string, treat it as a chart anyway.
    //      Belt + suspenders for when the LLM forgets the language tag.
    //   3. Otherwise render as a plain <pre>.
    const fenceOpen = /^```\s*([A-Za-z0-9_-]*)\s*$/.exec(line);
    if (fenceOpen) {
      const tag = (fenceOpen[1] || "").toLowerCase();
      const buf = [];
      let j = i + 1;
      // Closing fence: tolerate a trailing language tag (some models emit
      // ```trustnode-chart as BOTH the open and close), so ``` with or
      // without a language token both close the block. Otherwise the rest
      // of the message gets swallowed and the JSON fails to parse.
      while (j < lines.length && !/^```\s*(?:[A-Za-z0-9_-]*)?\s*$/.test(lines[j])) {
        buf.push(lines[j]);
        j++;
      }
      const body = buf.join("\n");
      i = j + 1; // skip past closing fence

      // Try to parse JSON regardless of language tag. Three accepted forms:
      //   1. Single object with `series`+`tag`
      //   2. Multi-series object {series:[{tag, series:[...]}, ...]}
      //   3. Multiple concatenated objects in one block (LLM sometimes emits
      //      one tag-object per chart back-to-back). We extract each {...} run
      //      and render one chart per valid chart-shaped object.
      // A chart payload has either a time-series (`series`) OR categorical
      // slices (`slices`, used by donut + per-tag bar). Both render as charts.
      const isChartShape = (o) => o && typeof o === "object"
        && ((Array.isArray(o.series) && o.series.length > 0)
            || (Array.isArray(o.slices) && o.slices.length > 0));

      let chartObjects = [];
      let parsedWhole = null;
      try { parsedWhole = JSON.parse(body); } catch { parsedWhole = null; }
      if (isChartShape(parsedWhole)) {
        chartObjects = [parsedWhole];
      } else if (Array.isArray(parsedWhole)) {
        chartObjects = parsedWhole.filter(isChartShape);
      } else {
        // Multi-object fallback: scan top-level {...} chunks by depth.
        chartObjects = extractJsonObjects(body).filter(isChartShape);
      }

      if (tag === "trustnode-chart" || chartObjects.length > 0) {
        if (chartObjects.length > 0) {
          for (const obj of chartObjects) {
            blocks.push(<TrustnodeChart key={`tnc${blockKey++}`} data={obj} />);
          }
          continue;
        }
        // Fall through if no parseable chart shape.
      }
      blocks.push(
        <pre key={`code${blockKey++}`} style={{
          margin: "8px 0", padding: 10,
          background: "var(--bg)", color: "var(--text)",
          border: "1px solid var(--stroke)", borderRadius: 6,
          fontSize: 12, overflow: "auto", whiteSpace: "pre-wrap",
        }}>{body}</pre>
      );
      continue;
    }

    // Heading
    const hMatch = /^(#{1,3})\s+(.*)$/.exec(line);
    if (hMatch) {
      const level = hMatch[1].length;
      const Tag = level === 1 ? "h3" : level === 2 ? "h4" : "h5";
      blocks.push(
        <Tag key={`h${blockKey++}`} style={{
          margin: level === 1 ? "12px 0 6px" : "10px 0 4px",
          fontSize: level === 1 ? 15 : level === 2 ? 14 : 13,
          fontWeight: 600,
        }}>
          {renderInline(hMatch[2])}
        </Tag>
      );
      i++;
      continue;
    }

    // Table — must have at least 2 rows; second is the separator
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s\-:|]+\|?\s*$/.test(lines[i + 1])) {
      const headers = line.split("|").map((c) => c.trim()).filter((c, idx, arr) => !(idx === 0 && c === "") && !(idx === arr.length - 1 && c === ""));
      i += 2; // skip header + separator
      const rows = [];
      while (i < lines.length && lines[i].includes("|")) {
        const cells = lines[i].split("|").map((c) => c.trim()).filter((c, idx, arr) => !(idx === 0 && c === "") && !(idx === arr.length - 1 && c === ""));
        rows.push(cells);
        i++;
      }
      blocks.push(
        <div key={`t${blockKey++}`} style={{ margin: "8px 0", overflowX: "auto" }}>
          <table style={{
            borderCollapse: "collapse", width: "100%", fontSize: 12.5,
          }}>
            <thead>
              <tr>
                {headers.map((h, hi) => (
                  <th key={hi} style={{
                    textAlign: "left", padding: "5px 10px",
                    borderBottom: "2px solid var(--teal, #14a89a)",
                    color: "var(--teal, #14a89a)", fontWeight: 700,
                  }}>{renderInline(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} style={{
                  background: ri % 2 ? "color-mix(in srgb, var(--text) 4%, transparent)" : "transparent",
                }}>
                  {r.map((c, ci) => (
                    <td key={ci} style={{
                      padding: "5px 10px",
                      borderBottom: "1px solid var(--stroke)",
                      color: "var(--text)",
                      fontVariantNumeric: "tabular-nums",
                    }}>{renderInline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      blocks.push(
        <ul key={`u${blockKey++}`} style={{ margin: "6px 0 6px 18px", padding: 0 }}>
          {items.map((it, ii) => (
            <li key={ii} style={{ marginBottom: 3 }}>{renderInline(it)}</li>
          ))}
        </ul>
      );
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push(
        <ol key={`o${blockKey++}`} style={{ margin: "6px 0 6px 18px", padding: 0 }}>
          {items.map((it, ii) => (
            <li key={ii} style={{ marginBottom: 3 }}>{renderInline(it)}</li>
          ))}
        </ol>
      );
      continue;
    }

    // Paragraph — collect consecutive non-blank, non-special lines
    const paraLines = [line];
    let j = i + 1;
    while (j < lines.length && lines[j].trim() &&
           !/^#{1,3}\s/.test(lines[j]) &&
           !/^\s*[-*]\s+/.test(lines[j]) &&
           !/^\s*\d+\.\s+/.test(lines[j]) &&
           !lines[j].includes("|")) {
      paraLines.push(lines[j]);
      j++;
    }
    blocks.push(
      <p key={`p${blockKey++}`} style={{ margin: "6px 0", lineHeight: 1.55 }}>
        {paraLines.map((ln, idx) => (
          <React.Fragment key={idx}>
            {renderInline(ln)}
            {idx < paraLines.length - 1 ? <br /> : null}
          </React.Fragment>
        ))}
      </p>
    );
    i = j;
  }

  return blocks;
}


// Operator 2026-07-02 (PERF): renderMarkdown re-parses the full message and
// rebuilds any SVG chart from scratch. Without memoization it ran on EVERY
// parent re-render (clicking a chat, typing, a status tick), which made the
// UI feel frozen when a chart message was on screen. Memoize on `content`
// so a message only re-renders when its own text changes.
const RenderedMarkdown = React.memo(function RenderedMarkdown({ content }) {
  const rendered = useMemo(() => renderMarkdown(content), [content]);
  return <div>{rendered}</div>;
});

// Detect a "did you mean / pick an option" assistant message and extract the
// numbered choices so we can render instant clickable buttons. Matches lines
// like "1) BT_PVC_Level", "2. SimREAL[3]", etc. Only activates when the
// message clearly asks the user to choose.
function parseDisambiguation(content) {
  const s = String(content || "");
  if (!/did you mean|which .*would you like|please (confirm|choose|pick|select)/i.test(s)) {
    return null;
  }
  const opts = [];
  const re = /^\s*(\d+)[).\]]\s+(.+?)\s*$/gm;
  let m;
  while ((m = re.exec(s)) !== null) {
    const label = m[2].trim();
    if (label && label.length <= 120) opts.push(label);
  }
  return opts.length >= 1 ? opts : null;
}

// Format an ISO/utc timestamp as local HH:MM:SS. Falls back to "" on error.
function fmtClock(ts) {
  if (!ts) return "";
  try {
    let s = String(ts).trim();
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s) && !s.includes("T")) {
      s = s.replace(" ", "T");
      if (!/[Z+]/.test(s.slice(10))) s += "Z";
    }
    const d = new Date(s);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  } catch { return ""; }
}

function ChatMessageInner({ role, content, toolLog, onSaveAsInsight, onPickOption, createdUtc, latencyMs }) {
  const isUser = role === "user";
  // Save-as-insight is only meaningful for an assistant message that
  // actually used data lookups (so we have a tool_plan to replay) AND
  // the caller passed us a handler (chat page wires it; insights page
  // wouldn't).
  const canSave = !isUser
    && typeof onSaveAsInsight === "function"
    && Array.isArray(toolLog) && toolLog.length > 0;
  // Clickable disambiguation options (instant pick instead of typing).
  const options = !isUser && typeof onPickOption === "function"
    ? parseDisambiguation(content) : null;
  const clock = fmtClock(createdUtc);
  return (
    <div style={{
      display: "flex", justifyContent: isUser ? "flex-end" : "flex-start",
      margin: "10px 0",
    }}>
      <div style={{
        maxWidth: "82%",
        background: isUser
          ? "color-mix(in srgb, var(--teal, #14a89a) 16%, var(--card))"
          : "var(--card)",
        border: `1px solid ${isUser
          ? "color-mix(in srgb, var(--teal, #14a89a) 45%, var(--stroke))"
          : "var(--stroke)"}`,
        color: "var(--text)",
        borderRadius: 10, padding: "10px 14px",
        fontSize: 13, lineHeight: 1.55,
      }}>
        {isUser ? (
          <div style={{ whiteSpace: "pre-wrap" }}>{content}</div>
        ) : (
          <RenderedMarkdown content={content} />
        )}
        {options ? (
          <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 8 }}>
            {options.map((opt, oi) => (
              <button
                key={oi}
                type="button"
                onClick={() => onPickOption(opt)}
                style={{
                  border: "1px solid var(--teal, #14a89a)",
                  background: "color-mix(in srgb, var(--teal, #14a89a) 14%, transparent)",
                  color: "var(--text)",
                  fontSize: 12.5, fontWeight: 600,
                  padding: "6px 12px", borderRadius: 6, cursor: "pointer",
                }}
              >
                {opt}
              </button>
            ))}
          </div>
        ) : null}
        {canSave ? (
          <div style={{ marginTop: 8, display: "flex", justifyContent: "flex-end" }}>
            <button
              type="button"
              onClick={() => onSaveAsInsight({ content, toolLog })}
              title="Save this as a reusable insight (optionally scheduled + emailed)"
              style={{
                border: "1px solid var(--teal, #14a89a)",
                background: "color-mix(in srgb, var(--teal, #14a89a) 12%, transparent)",
                color: "var(--teal, #14a89a)",
                fontSize: 11,
                fontWeight: 600,
                padding: "3px 8px",
                borderRadius: 4,
                cursor: "pointer",
              }}
            >
              + Save as insight
            </button>
          </div>
        ) : null}
        {Array.isArray(toolLog) && toolLog.length > 0 ? (
          <details style={{ marginTop: 8, fontSize: 10, color: "var(--muted)", opacity: 0.7 }}>
            <summary style={{ cursor: "pointer", listStyle: "none" }}>
              ▸ {toolLog.length} data lookup{toolLog.length === 1 ? "" : "s"}
            </summary>
            <ul style={{ marginTop: 6, paddingLeft: 16 }}>
              {toolLog.map((t, i) => (
                <li key={i} style={{ marginBottom: 4 }}>
                  <code>{t.name}</code>({Object.keys(t.args || {}).map((k) => `${k}=${JSON.stringify(t.args[k])}`).join(", ")}){t.cached ? <span style={{ marginLeft: 6, opacity: 0.6 }}>(cached)</span> : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        {/* Timestamp (local HH:MM) + assistant response latency. */}
        {(clock || (!isUser && latencyMs)) ? (
          <div style={{
            marginTop: 6, fontSize: 10, color: "var(--muted)",
            display: "flex", gap: 8, justifyContent: isUser ? "flex-end" : "flex-start",
          }}>
            {clock ? <span>{clock}</span> : null}
            {(!isUser && latencyMs) ? <span title="Time from your message to this reply">· replied in {(latencyMs / 1000).toFixed(1)}s</span> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// Memoized so scrolling / clicking / typing elsewhere doesn't re-parse
// markdown or rebuild charts for every message on every render. The parent
// re-creates the handler props on each render, so we compare only the fields
// that actually affect output (role/content/toolLog) plus whether pick/save
// handlers are present at all — not their identity.
function _msgEqual(a, b) {
  return a.role === b.role
    && a.content === b.content
    && a.toolLog === b.toolLog
    && a.createdUtc === b.createdUtc
    && a.latencyMs === b.latencyMs
    && (typeof a.onPickOption === "function") === (typeof b.onPickOption === "function")
    && (typeof a.onSaveAsInsight === "function") === (typeof b.onSaveAsInsight === "function");
}
export const ChatMessage = React.memo(ChatMessageInner, _msgEqual);

export default ChatMessage;
