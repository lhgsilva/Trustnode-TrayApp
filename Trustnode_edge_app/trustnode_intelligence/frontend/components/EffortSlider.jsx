import React from "react";

/**
 * Effort toggle — a two-position pill, styled IDENTICALLY to DataSourceToggle
 * (Local DB / Cloud DB) so the composer bar looks consistent:
 *   Instant → fast, direct data answers.
 *   High    → deeper engineering analysis.
 *
 * Does NOT reveal the local-vs-AI mechanic to the customer. Uses the host
 * palette (teal active, --muted inactive) → matches light + dark themes.
 */
export function EffortSlider({ mode, onChange, disabled = false }) {
  const opts = [
    { key: "instant", label: "Instant" },
    { key: "high", label: "High Effort" },
  ];
  return (
    <div style={{
      display: "inline-flex", border: "1px solid var(--stroke)",
      borderRadius: 999, padding: 2, gap: 0,
      background: "var(--bg)",
    }}>
      {opts.map((o) => {
        const active = mode === o.key;
        return (
          <button
            key={o.key}
            disabled={disabled}
            onClick={() => onChange && onChange(o.key)}
            title={o.key === "instant"
              ? "Instant: fast, direct data answers."
              : "High Effort: deeper engineering analysis (a little slower)."}
            style={{
              padding: "5px 14px", fontSize: 12, fontWeight: 600,
              border: "none", borderRadius: 999, cursor: disabled ? "not-allowed" : "pointer",
              background: active ? "var(--teal, #14a89a)" : "transparent",
              color: active ? "#fff" : "var(--muted)",
              transition: "background 0.12s, color 0.12s",
              whiteSpace: "nowrap",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export default EffortSlider;
