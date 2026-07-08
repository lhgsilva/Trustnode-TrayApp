import React, { useEffect, useState } from "react";
import intelligenceApi from "./api.js";
import { InsightEditor } from "./components/InsightEditor.jsx";
import { InsightPreviewModal } from "./components/InsightPreviewModal.jsx";

/* Two-column layout (operator 2026-06-30):
 *   LEFT  = saved insights (templates) — what is configured/scheduled
 *   RIGHT = generated insights (runs/history) — what was actually produced
 *           when each saved insight ran (manually or on schedule).
 *
 * Click an insight on the left → right column shows ITS run history.
 * Eye 👁 button on either side opens InsightPreviewModal with the same
 * formatted view used in the chat bubble (markdown + tables + chart),
 * plus PDF + CSV export icons.
 */
export default function IntelligenceInsightsPage() {
  // Operator 2026-07-08 (FAST + RELIABLE): paint instantly from the last-good
  // cached list so the page is never blank while the network call is queued
  // behind the host app's pollers on a busy edge. requestCached (in api.js)
  // also falls back to cache if a refresh momentarily fails, so a transient
  // pool-saturation timeout no longer surfaces as "Failed to fetch".
  const _cachedInsights = (() => {
    try { const c = intelligenceApi.peekInsights(); return (c && c.insights) || []; } catch { return []; }
  })();
  const [insights, setInsights] = useState(_cachedInsights);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(null);

  const [selectedId, setSelectedId] = useState(_cachedInsights[0]?.id || null);
  const [runs, setRuns] = useState([]);
  const [runsBusy, setRunsBusy] = useState(false);

  // Preview-modal state: { kind: 'saved'|'run', insight, run? }
  const [preview, setPreview] = useState(null);

  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    if (!selectedId) { setRuns([]); return; }
    refreshRuns(selectedId);
  }, [selectedId]);

  // Softer error surface: a bare "Failed to fetch" here almost always means the
  // list GET was momentarily queued/timed out on the saturated socket pool (or
  // the AI endpoint is still syncing on a fresh install). Show that plainly and
  // schedule a background retry instead of a scary raw error. Falls through to
  // the raw message for genuine (non-connection) errors.
  function surfaceError(e) {
    const s = String(e?.message || e || "");
    if (/failed to fetch|networkerror|load failed|aborted|err_|insufficient/i.test(s)) {
      setError("Loading… (the AI service is busy or syncing — retrying).");
      // one background retry shortly after; requestCached will use cache if it
      // still can't reach the server.
      setTimeout(() => { refresh().catch(() => {}); }, 1500);
      return;
    }
    setError(s);
  }

  async function refresh() {
    try {
      const r = await intelligenceApi.listInsights();
      const list = r.insights || [];
      setInsights(list);
      setError("");
      // Auto-select the first one so the right column has something.
      if (!selectedId && list.length) setSelectedId(list[0].id);
    } catch (e) {
      surfaceError(e);
    }
  }

  async function refreshRuns(insightId) {
    setRunsBusy(true);
    try {
      const r = await intelligenceApi.listInsightRuns(insightId, 100);
      setRuns(r.runs || []);
      setError("");
    } catch (e) {
      surfaceError(e);
    } finally {
      setRunsBusy(false);
    }
  }

  async function runNow(id) {
    try {
      await intelligenceApi.runInsight(id);
      await refresh();
      if (selectedId === id) await refreshRuns(id);
    } catch (e) {
      surfaceError(e);
    }
  }

  async function remove(id) {
    if (!window.confirm("Delete this insight and all its run history?")) return;
    try {
      await intelligenceApi.deleteInsight(id);
      if (selectedId === id) setSelectedId(null);
      await refresh();
    } catch (e) {
      surfaceError(e);
    }
  }

  async function removeRun(runId) {
    if (!window.confirm("Delete this run from history?")) return;
    try {
      await intelligenceApi.deleteInsightRun(selectedId, runId);
      await refreshRuns(selectedId);
    } catch (e) {
      surfaceError(e);
    }
  }

  const selected = insights.find((i) => i.id === selectedId) || null;

  // Operator 2026-07-03: panel cards are FIXED to the viewport height and
  // scroll their content INTERNALLY. Previously the cards grew with their
  // content and pushed the whole page taller than the screen (so the user had
  // to scroll the page). Now the card is a flex column: a fixed header + a
  // flex:1 body that scrolls on overflow — the page itself never scrolls.
  const cardStyle = {
    padding: "16px 20px",
    background: "var(--card)", color: "var(--text)",
    border: "1px solid var(--stroke)", borderRadius: "var(--radius-lg, 12px)",
    display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "calc(100vh - 120px)", overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text)" }}>TrustNode Intelligence — Insights</div>
        <button
          className="btn btn-primary"
          onClick={() => setEditing({ title: "", description: "", prompt: "", tool_plan: [], data_source: "local", schedule_cron: "", email_to: "" })}
          style={{ padding: "8px 14px", fontSize: 13, fontWeight: 500 }}
        >
          + New insight
        </button>
      </div>

      {error ? <div style={{ color: "#dc2626" }}>{error}</div> : null}

      <div style={{
        display: "grid",
        gridTemplateColumns: "minmax(320px, 1fr) minmax(320px, 1.4fr)",
        gap: 16,
        alignItems: "stretch",
        flex: 1,
        minHeight: 0,
      }}>
        {/* ============ LEFT CARD: Configured insights ============ */}
        <div className="card" style={cardStyle}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 12, flexShrink: 0 }}>
            Configured insights
          </div>
          {insights.length === 0 ? (
            <div style={{ color: "var(--muted)", fontSize: 13, padding: 20, textAlign: "center" }}>
              No saved insights yet. Create one from a chat answer (via "Save as insight"),
              or click "New insight" above to define a scheduled analysis directly.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
              {insights.map((ins) => {
                const active = ins.id === selectedId;
                return (
                  <div
                    key={ins.id}
                    onClick={() => setSelectedId(ins.id)}
                    style={{
                      background: active
                        ? "color-mix(in srgb, var(--teal, #14a89a) 14%, var(--card))"
                        : "var(--surface-elev, var(--card))",
                      border: `1px solid ${active ? "var(--teal, #14a89a)" : "var(--stroke)"}`,
                      borderRadius: 8, padding: 12,
                      cursor: "pointer",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {ins.title}
                        </div>
                        {ins.description ? (
                          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {ins.description}
                          </div>
                        ) : null}
                      </div>
                      <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                        <button onClick={(e) => { e.stopPropagation(); setPreview({ kind: "saved", insight: ins }); }} style={btnIcon} title="Preview last result">👁</button>
                        <button onClick={(e) => { e.stopPropagation(); runNow(ins.id); }} style={btnIcon} title="Run now">▶</button>
                        <button onClick={(e) => { e.stopPropagation(); setEditing(ins); }} style={btnIcon} title="Edit">✎</button>
                        <button onClick={(e) => { e.stopPropagation(); remove(ins.id); }} style={{ ...btnIcon, color: "#dc2626" }} title="Delete">×</button>
                      </div>
                    </div>
                    <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 6, display: "flex", flexWrap: "wrap", gap: 8 }}>
                      <span>Source: {ins.data_source}</span>
                      <span>·</span>
                      <span>{ins.schedule_cron ? <>Sched: <code>{ins.schedule_cron}</code></> : "No schedule"}</span>
                      {ins.email_to ? <><span>·</span><span>Email: {ins.email_to}</span></> : null}
                      {ins.last_run_utc ? <><span>·</span><span>Last: {ins.last_run_utc}</span></> : null}
                      {ins.last_error ? <><span>·</span><span style={{ color: "#dc2626" }}>error</span></> : null}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ============ RIGHT CARD: Generated insights (history of runs) ============ */}
        <div className="card" style={cardStyle}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 12, flexShrink: 0 }}>
            Generated insights {selected ? <span style={{ color: "var(--text)", textTransform: "none", letterSpacing: 0, fontWeight: 600, marginLeft: 6 }}>· {selected.title}</span> : null}
          </div>

          {!selected ? (
            <div style={{ color: "var(--muted)", fontSize: 13, padding: 20, textAlign: "center" }}>
              Select an insight on the left to see its run history.
            </div>
          ) : runsBusy ? (
            <div style={{ color: "var(--muted)", fontSize: 12, padding: 12 }}>Loading runs…</div>
          ) : runs.length === 0 ? (
            <div style={{ color: "var(--muted)", fontSize: 13, padding: 20, textAlign: "center" }}>
              No runs yet for this insight. Click ▶ on the left to run it once,
              or set a schedule.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
              {runs.map((run) => (
                <div
                  key={run.id}
                  style={{
                    background: "var(--surface-elev, var(--card))",
                    border: "1px solid var(--stroke)",
                    borderRadius: 6, padding: "8px 12px",
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    gap: 10,
                  }}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 12, color: "var(--text)" }}>
                      {run.started_utc}
                      <span style={{ marginLeft: 8, opacity: 0.7, fontSize: 11 }}>
                        ({run.triggered_by === "schedule" ? "scheduled" : "manual"})
                      </span>
                      {!run.ok ? <span style={{ marginLeft: 8, color: "#dc2626", fontSize: 11 }}>error</span> : null}
                    </div>
                    {run.error ? (
                      <div style={{ fontSize: 11, color: "#dc2626", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {run.error}
                      </div>
                    ) : null}
                  </div>
                  <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                    <button onClick={() => setPreview({ kind: "run", insight: selected, run })} style={btnIcon} title="Preview this run">👁</button>
                    <button onClick={() => removeRun(run.id)} style={{ ...btnIcon, color: "#dc2626" }} title="Delete this run">×</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ============ Editor modal (create / edit) ============ */}
      {editing ? (
        <InsightEditor
          initial={editing}
          onCancel={() => setEditing(null)}
          onSave={async (payload) => {
            try {
              await intelligenceApi.createInsight(payload);
              setEditing(null);
              await refresh();
            } catch (e) {
              surfaceError(e);
            }
          }}
        />
      ) : null}

      {/* ============ Preview modal ============ */}
      {preview ? (
        <InsightPreviewModal
          title={preview.insight?.title || "Insight"}
          subtitle={
            preview.kind === "run"
              ? `Run ${preview.run?.started_utc || ""} (${preview.run?.triggered_by || "manual"})`
              : preview.insight?.last_run_utc ? `Last run: ${preview.insight.last_run_utc}` : "No runs yet"
          }
          content={
            preview.kind === "run"
              ? (preview.run?.content || preview.run?.error || "(no content)")
              : (preview.insight?.last_result || preview.insight?.last_error || "(no result yet — click ▶ Run now)")
          }
          toolLog={preview.kind === "run" ? (preview.run?.tool_results || []) : []}
          onClose={() => setPreview(null)}
        />
      ) : null}
    </div>
  );
}

const btnIcon = {
  border: "1px solid var(--stroke)", background: "var(--bg)",
  color: "var(--text)", borderRadius: 4, width: 26, height: 26,
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  cursor: "pointer", fontSize: 12,
};
