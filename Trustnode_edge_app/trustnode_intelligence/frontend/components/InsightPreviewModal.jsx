import React, { useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { ChatMessage } from "./ChatMessage.jsx";

/* Insight preview modal — reused by:
   1. Insights page (eye button on a saved insight or a historical run)
   2. Chat page (after "Save as insight" → shows preview then offers Save)

   Renders the assistant content with ChatMessage so markdown, tables,
   and the inline SVG `trustnode-chart` block all look identical to the
   chat bubble.

   Top-right action icons:
     - 📄 PDF  : opens the system print dialog scoped to the preview body
                 (browser-native, no PDF lib needed). User picks "Save as PDF".
     - ⤓  CSV  : if the content has a ```trustnode-chart block, downloads
                 ts,value rows from its `series` array.
     - ✕      : close

   Optional `actionFooter` slot lets the caller append a Save button
   (the chat page uses this for the Save-as-Insight flow).
*/
export function InsightPreviewModal({
  title,
  subtitle,
  content,
  toolLog,
  actionFooter,
  onClose,
}) {
  const bodyRef = useRef(null);

  // Find a trustnode-chart fenced block (if any) for CSV export.
  const chartData = useMemo(() => {
    if (!content) return null;
    const m = /```trustnode-chart\s*\n([\s\S]*?)\n```/.exec(String(content));
    if (!m) return null;
    try { return JSON.parse(m[1]); } catch { return null; }
  }, [content]);

  const exportCsv = () => {
    if (!chartData || !Array.isArray(chartData.series) || chartData.series.length === 0) {
      alert("No chart data found in this insight to export.");
      return;
    }
    const tag = String(chartData.tag || "tag").replace(/[^A-Za-z0-9_.-]/g, "_");
    const rows = [["ts_iso", "ts_ms", "value"]];
    for (const p of chartData.series) {
      const ms = Number(p.ts);
      const iso = isFinite(ms) ? new Date(ms).toISOString() : "";
      rows.push([iso, String(ms), String(p.value)]);
    }
    const csv = rows.map((r) => r.map((c) => {
      const s = String(c == null ? "" : c);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(",")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${tag}_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const exportPdf = () => {
    // Native browser print of just the preview body. We open a new
    // window, copy the rendered HTML, and trigger print. No external
    // PDF lib; user selects "Save as PDF" in the print dialog.
    const html = bodyRef.current ? bodyRef.current.innerHTML : "";
    const win = window.open("", "_blank", "width=900,height=700");
    if (!win) {
      alert("Could not open print window — check popup blocker.");
      return;
    }
    win.document.write(`<!doctype html><html><head><meta charset="utf-8">
      <title>${(title || "TrustNode Insight").replace(/</g, "&lt;")}</title>
      <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 24px; color: #111; background: #fff; }
        h1, h2, h3, h4, h5 { margin: 12px 0 6px; }
        table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12px; }
        th, td { padding: 5px 10px; border-bottom: 1px solid #ddd; text-align: left; }
        th { color: #0d8d80; font-weight: 600; }
        code { background: #eef9f7; padding: 1px 5px; border-radius: 3px; font-size: 0.92em; }
        svg { max-width: 100%; height: auto; }
        details { margin-top: 8px; font-size: 10px; color: #777; }
        @page { size: A4; margin: 14mm; }
      </style></head><body>
        <h2 style="margin-top:0">${(title || "TrustNode Insight").replace(/</g, "&lt;")}</h2>
        ${subtitle ? `<div style="color:#666;font-size:12px;margin-bottom:8px">${String(subtitle).replace(/</g, "&lt;")}</div>` : ""}
        ${html}
      </body></html>`);
    win.document.close();
    // Give the new window a moment to layout, then trigger print.
    setTimeout(() => { try { win.focus(); win.print(); } catch (_) {} }, 200);
  };

  const iconBtn = {
    border: "1px solid var(--stroke)",
    background: "transparent",
    color: "var(--text)",
    borderRadius: 4,
    padding: "4px 10px",
    fontSize: 12,
    cursor: "pointer",
  };

  const node = (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose && onClose(); }}
      style={{
        position: "fixed", inset: 0,
        background: "rgba(0,0,0,0.65)",
        zIndex: 10000,
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 16,
      }}
    >
      <div
        className="modal-card"
        style={{
          background: "var(--card)", border: "1px solid var(--stroke)",
          color: "var(--text)",
          borderRadius: 10,
          width: "100%", maxWidth: 880, maxHeight: "90vh",
          display: "flex", flexDirection: "column",
          boxShadow: "0 12px 40px rgba(0,0,0,0.45)",
        }}
      >
        {/* Header */}
        <div style={{
          padding: "12px 16px", borderBottom: "1px solid var(--stroke)",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {title || "Insight preview"}
            </div>
            {subtitle ? (
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {subtitle}
              </div>
            ) : null}
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button type="button" onClick={exportPdf} title="Export to PDF (via system print)" style={iconBtn}>📄 PDF</button>
            <button type="button" onClick={exportCsv} title={chartData ? "Export chart series to CSV" : "No chart data in this insight"} disabled={!chartData} style={{ ...iconBtn, opacity: chartData ? 1 : 0.5, cursor: chartData ? "pointer" : "not-allowed" }}>⤓ CSV</button>
            <button type="button" onClick={onClose} title="Close" style={iconBtn}>✕</button>
          </div>
        </div>

        {/* Body — reuses ChatMessage so chart + markdown render the same */}
        <div
          ref={bodyRef}
          style={{
            flex: 1, overflowY: "auto",
            padding: "16px 20px",
            background: "var(--bg)",
          }}
        >
          {content ? (
            <ChatMessage role="assistant" content={content} toolLog={toolLog} />
          ) : (
            <div style={{ color: "var(--muted)", fontSize: 13, textAlign: "center", padding: 40 }}>
              (No content)
            </div>
          )}
        </div>

        {/* Optional footer (e.g. Save-as-Insight from chat) */}
        {actionFooter ? (
          <div style={{
            padding: "10px 16px", borderTop: "1px solid var(--stroke)",
            display: "flex", justifyContent: "flex-end", gap: 10,
            background: "var(--surface-elev, var(--card))",
          }}>
            {actionFooter}
          </div>
        ) : null}
      </div>
    </div>
  );

  if (typeof document === "undefined") return node;
  return createPortal(node, document.body);
}

export default InsightPreviewModal;
