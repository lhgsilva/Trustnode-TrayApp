import React, { useState, useMemo, useEffect } from "react";
import intelligenceApi from "../api.js";

/**
 * Predefined query palette — shown on the empty chat screen so the operator can
 * discover what the assistant handles and one-click a well-formed question.
 *
 * The palette is CUSTOMER-EDITABLE and loaded from the backend (/presets):
 *   - Fresh install → backend returns the shipped DEFAULT_PRESETS.
 *   - The customer edits/adds/removes/reorders queries → saved per-tenant,
 *     survives upgrades.
 * Query strings use {t1}/{t2}/{t3}/{multi} placeholders that we fill with the
 * customer's REAL tag names (from `tags`), so nothing is process-specific.
 */

const CAT_COLORS = { data: "#14b8a6", analytics: "#f59e0b", compare: "#a855f7" };
const catColor = (key, i) => CAT_COLORS[key] || ["#14b8a6", "#f59e0b", "#a855f7", "#3b82f6"][i % 4];

// Bundled DEFAULT palette — mirrors the backend DEFAULT_PRESETS. We render this
// INSTANTLY so the palette never sits in a "Loading" limbo (and never flickers
// if the /presets fetch is slow or unauthorized). The backend fetch then
// OVERRIDES this only if the customer saved a custom palette.
const BUNDLED_DEFAULTS = [
  { key: "data", label: "Data & Live Status", hint: "Instant · values, tables, what's running", queries: [
    "What tags are live right now?",
    "Which gateways are running now?",
    "What is the current value of {t1}?",
    "Show me the latest reading for every live tag",
    "List all tags being collected",
    "Trend {t1} over the last 20 readings",
    "Give me detailed information about the current gateway running",
    "Are there any alarms in the last 24 hours?",
    "What is the min, max and average of {t1} in the last hour?",
    "How many readings has the active gateway written today?",
  ]},
  { key: "analytics", label: "Process Analytics", hint: "High Effort · stability, drift, capability", queries: [
    "Is {t1} stable and in control over the last hour?",
    "Are there any anomalies or spikes in {t1} today?",
    "Is the process drifting? Analyze {t2} over the last 4 hours.",
    "Give me a process-capability assessment for {t1}.",
    "Summarize the health of the process across all live tags.",
    "Why did {t1} change over the last hour?",
    "Detect any out-of-range excursions across the live tags today.",
    "What is the standard deviation of {t3} and is it acceptable?",
    "Assess whether the gateway is collecting reliably or has gaps.",
    "Identify the noisiest tag and explain its variability.",
  ]},
  { key: "compare", label: "Compare · Multi-series · Batches", hint: "Overlays, correlation, period-over-period, batches", queries: [
    "Compare {multi} grouped by 1 minute over the last hour and show correlation",
    "Correlate {t1} and {t2} every 5 seconds over the last 30 minutes",
    "Trend {multi} in the same chart",
    "Compare {t1} this hour to the same hour yesterday",
    "Trend {multi} for the last batch",
    "Show the last 5 batches and their durations",
    "Compare {t1} across the last 3 batches",
    "Trend {t1} since the process started",
    "Trend {t1} since it last crossed a high value",
    "Which of {multi} move together? Analyze the correlation over the last hour.",
  ]},
];

// Module-level cache so remounts (screen switches) don't re-fetch or flicker.
const _PRESET_CACHE = { categories: null, isDefault: true };

// Fill {t1}/{t2}/{t3}/{multi} placeholders with the customer's real tags.
function fillTemplate(q, tags) {
  const has = Array.isArray(tags) && tags.length > 0;
  const t1 = has ? tags[0] : "my main tag";
  const t2 = has && tags[1] ? tags[1] : (has ? tags[0] : "a process tag");
  const t3 = has && tags[2] ? tags[2] : t2;
  const multi = has && tags.length >= 2 ? tags.slice(0, 3).join(", ") : `${t1}${t2 !== t1 ? ", " + t2 : ""}`;
  return String(q)
    .replaceAll("{t1}", t1).replaceAll("{t2}", t2).replaceAll("{t3}", t3).replaceAll("{multi}", multi);
}

export function PredefinedQueries({ onPick, tags }) {
  // Seed from cache if present, else the bundled defaults — so we ALWAYS have a
  // palette to show immediately (never a "Loading" limbo, never a flicker).
  const [cats, setCats] = useState(_PRESET_CACHE.categories || BUNDLED_DEFAULTS);
  const [isDefault, setIsDefault] = useState(_PRESET_CACHE.isDefault);
  const [active, setActive] = useState((_PRESET_CACHE.categories || BUNDLED_DEFAULTS)[0]?.key || "data");
  const [editing, setEditing] = useState(false);

  // Fetch the customer's saved palette ONCE (if not already cached). This only
  // OVERRIDES the bundled defaults when the customer has actually customized —
  // and it's best-effort: any failure (401 mid-login, network) leaves the
  // bundled defaults in place. Runs at most once per session (module cache).
  useEffect(() => {
    if (_PRESET_CACHE.categories) return;  // already loaded — no re-fetch, no flicker
    let cancelled = false;
    intelligenceApi.getPresets()
      .then((r) => {
        if (cancelled || !r) return;
        const list = (Array.isArray(r.categories) && r.categories.length) ? r.categories : BUNDLED_DEFAULTS;
        _PRESET_CACHE.categories = list;
        _PRESET_CACHE.isDefault = !!r.is_default;
        setCats(list);
        setIsDefault(!!r.is_default);
      })
      .catch(() => { /* keep the bundled defaults already shown */ });
    return () => { cancelled = true; };
  }, []);

  const meta = useMemo(() => (cats || []).find((c) => c.key === active) || (cats || [])[0] || null, [cats, active]);

  const saveEdits = async (next) => {
    setCats(next);
    _PRESET_CACHE.categories = next; _PRESET_CACHE.isDefault = false;
    setIsDefault(false);
    try { await intelligenceApi.savePresets(next); }
    catch { /* keep local edits; will retry on next save */ }
  };
  const resetDefaults = async () => {
    // Optimistic: show bundled defaults immediately.
    setCats(BUNDLED_DEFAULTS); setIsDefault(true);
    setActive(BUNDLED_DEFAULTS[0]?.key || "data");
    _PRESET_CACHE.categories = null; _PRESET_CACHE.isDefault = true;
    try {
      const r = await intelligenceApi.resetPresets();
      const list = (r && Array.isArray(r.categories) && r.categories.length) ? r.categories : BUNDLED_DEFAULTS;
      setCats(list);
    } catch { /* bundled defaults already shown */ }
  };

  return (
    <div style={{ maxWidth: 780, margin: "40px auto 0", color: "var(--text)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <div style={{ color: "var(--muted)", fontSize: 13 }}>
          Ask about your process data, collection status, or analytics — or pick a starting point:
        </div>
        <button
          type="button"
          onClick={() => setEditing((v) => !v)}
          title="Customize these starter queries"
          style={{
            border: "1px solid var(--stroke)", background: editing ? "color-mix(in srgb, var(--teal,#14a89a) 16%, transparent)" : "transparent",
            color: "var(--text)", cursor: "pointer", borderRadius: 6, padding: "4px 10px", fontSize: 12, whiteSpace: "nowrap",
          }}
        >
          {editing ? "Done" : "✎ Customize"}
        </button>
      </div>

      {/* Category tabs */}
      <div style={{ display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap", marginBottom: 14 }}>
        {cats.map((c, i) => {
          const on = c.key === (meta && meta.key);
          const color = catColor(c.key, i);
          return (
            <button key={c.key || i} type="button" onClick={() => setActive(c.key)}
              style={{
                border: `1px solid ${on ? color : "var(--stroke)"}`,
                background: on ? `color-mix(in srgb, ${color} 16%, transparent)` : "transparent",
                color: "var(--text)", cursor: "pointer", borderRadius: 8, padding: "6px 12px",
                fontSize: 12.5, fontWeight: 600, display: "inline-flex", flexDirection: "column", alignItems: "center", gap: 1,
              }}>
              <span>{c.label || c.key}</span>
              {c.hint ? <span style={{ fontSize: 10, fontWeight: 400, opacity: 0.7 }}>{c.hint}</span> : null}
            </button>
          );
        })}
      </div>

      {/* Queries for the active category */}
      {meta ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {(meta.queries || []).map((q, i) => {
            const color = catColor(meta.key, cats.indexOf(meta));
            const filled = fillTemplate(q, tags);
            if (editing) {
              return (
                <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <input
                    value={q}
                    onChange={(e) => {
                      const next = cats.map((c) => c.key === meta.key
                        ? { ...c, queries: c.queries.map((qq, j) => (j === i ? e.target.value : qq)) } : c);
                      setCats(next);
                    }}
                    onBlur={() => saveEdits(cats)}
                    style={{
                      flex: 1, fontSize: 13, padding: "8px 12px", borderRadius: 8,
                      background: "var(--bg)", color: "var(--text)", border: "1px solid var(--stroke)",
                    }}
                  />
                  <button type="button" title="Remove" onClick={() => {
                    const next = cats.map((c) => c.key === meta.key
                      ? { ...c, queries: c.queries.filter((_, j) => j !== i) } : c);
                    saveEdits(next);
                  }} style={delBtn}>×</button>
                </div>
              );
            }
            return (
              <button key={i} type="button" onClick={() => onPick(filled)} title="Click to ask this"
                style={{
                  textAlign: "left", border: "1px solid var(--stroke)", background: "var(--surface-elev, var(--card))",
                  color: "var(--text)", cursor: "pointer", borderRadius: 8, padding: "9px 14px", fontSize: 13,
                  lineHeight: 1.4, display: "flex", alignItems: "center", gap: 10,
                }}
                onMouseEnter={(e) => { e.currentTarget.style.borderColor = color; }}
                onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--stroke)"; }}>
                <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, flexShrink: 0 }} />
                <span style={{ minWidth: 0 }}>{filled}</span>
              </button>
            );
          })}
          {editing ? (
            <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
              <button type="button" onClick={() => {
                const next = cats.map((c) => c.key === meta.key ? { ...c, queries: [...(c.queries || []), "New query — use {t1}, {t2}, {multi} for tags"] } : c);
                saveEdits(next);
              }} style={{ ...addBtn }}>+ Add query</button>
              {!isDefault ? (
                <button type="button" onClick={resetDefaults} style={{ ...addBtn, borderColor: "var(--stroke)" }} title="Discard custom queries and restore the shipped defaults">
                  ↺ Reset to defaults
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {editing ? (
        <div style={{ marginTop: 10, fontSize: 11, color: "var(--muted)", textAlign: "center" }}>
          Tip: use <code>{"{t1}"}</code>, <code>{"{t2}"}</code>, <code>{"{t3}"}</code> or <code>{"{multi}"}</code> — they’re replaced with your real tag names. Changes save automatically.
        </div>
      ) : null}
    </div>
  );
}

const delBtn = {
  border: "1px solid var(--stroke)", background: "var(--bg)", color: "#dc2626",
  cursor: "pointer", width: 30, height: 30, borderRadius: 6, fontSize: 15, flexShrink: 0,
};
const addBtn = {
  border: "1px solid var(--teal, #14a89a)", background: "color-mix(in srgb, var(--teal,#14a89a) 12%, transparent)",
  color: "var(--text)", cursor: "pointer", borderRadius: 8, padding: "7px 12px", fontSize: 12.5, fontWeight: 600,
};

export default PredefinedQueries;
