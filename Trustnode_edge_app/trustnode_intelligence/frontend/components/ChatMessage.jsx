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

function TrustnodeChart({ data }) {
  const W = 640, H = 220, pad = { l: 50, r: 50, t: 18, b: 28 };
  const norm = useMemo(() => _toMultiShape(data), [data]);
  const series = useMemo(() => (norm ? _assignAxes(norm.series) : []), [norm]);

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
      margin: "8px 0", padding: 10,
      border: "1px solid var(--stroke)", borderRadius: 8,
      background: "var(--bg)",
    }}>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
        <span>
          {(norm.from && norm.to) ? <>{fmtWindowSide(norm.from)} → {fmtWindowSide(norm.to)}</> : null}
        </span>
        {series.map((s, i) => (
          <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11 }}>
            <span style={{
              width: 10, height: 2, background: s.color, display: "inline-block", borderRadius: 1,
            }} />
            <strong style={{ color: "var(--text)" }}>{s.tag}</strong>
            {s.gateway_name ? <span style={{ opacity: 0.7 }}>@{s.gateway_name}</span> : null}
            {rightAxis && s.axis === "right" ? <span style={{ opacity: 0.6 }}>(R)</span> : null}
          </span>
        ))}
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
      const isChartShape = (o) => o && typeof o === "object"
        && Array.isArray(o.series) && o.series.length > 0;

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
