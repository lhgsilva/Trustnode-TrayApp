import React from "react";

/* Pill toggle: Local SQLite ⟷ Cloud Database. */
export function DataSourceToggle({ value, onChange, disabled = false }) {
  const opts = [
    { key: "local", label: "Local DB" },
    { key: "cloud", label: "Cloud DB" },
  ];
  return (
    <div style={{
      display: "inline-flex", border: "1px solid var(--stroke)",
      borderRadius: 999, padding: 2, gap: 0,
      background: "var(--bg)",
    }}>
      {opts.map((o) => {
        const active = value === o.key;
        return (
          <button
            key={o.key}
            disabled={disabled}
            onClick={() => onChange && onChange(o.key)}
            style={{
              padding: "5px 14px", fontSize: 12, fontWeight: 600,
              border: "none", borderRadius: 999, cursor: disabled ? "not-allowed" : "pointer",
              background: active ? "var(--teal, #14a89a)" : "transparent",
              color: active ? "#fff" : "var(--muted)",
              transition: "background 0.12s, color 0.12s",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export default DataSourceToggle;
