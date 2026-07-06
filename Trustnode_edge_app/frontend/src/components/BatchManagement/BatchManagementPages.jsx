/* TrustNode Batch Management & Traceability — page components.

   Lives in its own folder so all module UI is in one place. The
   App.jsx dispatcher renders <BatchesPage /> / <BatchTypesPage /> /
   <BatchAuditPage /> based on activePage. License gating is done at
   the menu level (canOpenPage); these components additionally show a
   read-only banner if the license is later disabled mid-session.

   Styling reuses the project-wide CSS (card, table, btn, etc.) so the
   look matches the rest of the app exactly. No new stylesheets.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

/**
 * Render a modal via React Portal to document.body so it escapes any
 * ancestor with overflow:hidden / transform / contain (the batch detail
 * page card sets those and used to clip the editor). Also enforces the
 * host's modal class pair (.modal-backdrop + .modal-card) so dark/light
 * theme rules apply consistently with the rest of the app's modals.
 *
 * Usage:
 *   <Modal onClose={() => setEditing(null)}>
 *     <h3>…</h3>
 *     <button onClick={save}>Save</button>
 *   </Modal>
 */
function Modal({ onClose, children, width }) {
  const node = (
    <div
      className="modal-backdrop"
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", zIndex: 10000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 16,
      }}
    >
      <div
        className="modal-card"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--card)", color: "var(--text)",
          border: "1px solid var(--stroke)", borderRadius: 10,
          width: "100%", maxWidth: width || 720, maxHeight: "90vh",
          overflow: "auto", padding: 20,
          boxShadow: "0 12px 40px rgba(0,0,0,0.45)",
        }}
      >
        {children}
      </div>
    </div>
  );
  return typeof document === "undefined" ? node : createPortal(node, document.body);
}
import {
  getBatchManagementStatus,
  listBatchTypes,
  saveBatchType,
  deleteBatchType,
  listBatches,
  createBatch,
  startBatch,
  stopBatch,
  scanBatch,
  nextChildBatch,
  seedBatchDefaults,
  validateBatch,
  listBatchEvents,
  addBatchEvent,
  listBatchSummaries,
  recomputeBatchSummaries,
  listBatchHistorianRows,
  deleteBatch,
  listBatchAudit,
  getBatch,
  bmDownloadUrl,
} from "../../api";


function formatTs(s) {
  if (!s) return "";
  try {
    const d = new Date(String(s).includes("T") ? s : s.replace(" ", "T") + "Z");
    if (!isFinite(d.getTime())) return s;
    return d.toLocaleString();
  } catch { return s; }
}

function StatusPill({ status }) {
  const s = String(status || "").toLowerCase();
  const cls =
    s === "running" ? "status-online" :
    s === "completed" || s === "validated" ? "status-online" :
    s === "failed" || s === "cancelled" ? "status-offline" :
    "status-offline";
  return <span className={`status-pill ${cls}`}>{s.toUpperCase() || "—"}</span>;
}

function useToastError() {
  const [error, setError] = useState("");
  const set = useCallback((err) => {
    setError(typeof err === "string" ? err : (err?.message || String(err || "")));
  }, []);
  return [error, set];
}

// -----------------------------------------------------------------------
// Batches page — list, create, start/stop, validate, view detail
// -----------------------------------------------------------------------
export function BatchesPage({ currentUser, allGatewayOptions = [] }) {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [filterStatus, setFilterStatus] = useState("");
  const [filterSearch, setFilterSearch] = useState("");
  const [batchTypes, setBatchTypes] = useState([]);
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({
    batch_type_id: "", identifier: "", product: "", recipe: "",
    operator: currentUser?.username || "", gateway_id: "", notes: "",
    parent_batch_id: "",
  });
  const [selectedId, setSelectedId] = useState(null);
  const [error, setError] = useToastError();
  // Operator 2026-07-06: keyboard-wedge barcode scan-to-start. The scanner
  // types the code + Enter into this focused field.
  const [scanCode, setScanCode] = useState("");
  const [scanBusy, setScanBusy] = useState(false);
  const [scanMsg, setScanMsg] = useState("");

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const data = await listBatches({
        limit: 200,
        status: filterStatus || undefined,
        search: filterSearch || undefined,
      });
      setRows(Array.isArray(data?.rows) ? data.rows : []);
      setTotal(Number(data?.total || 0));
    } catch (e) { setError(e); }
    finally { setBusy(false); }
  }, [filterStatus, filterSearch, setError]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    (async () => {
      try { setBatchTypes(await listBatchTypes()); }
      catch (e) { setError(e); }
    })();
  }, [setError]);

  const handleCreate = async () => {
    try {
      const payload = { ...createForm };
      Object.keys(payload).forEach((k) => { if (payload[k] === "") delete payload[k]; });
      await createBatch(payload);
      setShowCreate(false);
      setCreateForm({
        batch_type_id: "", identifier: "", product: "", recipe: "",
        operator: currentUser?.username || "", gateway_id: "", notes: "",
        parent_batch_id: "",
      });
      await refresh();
    } catch (e) { setError(e); }
  };

  const handleStart = async (id) => {
    try {
      await startBatch(id, { operator: currentUser?.username || undefined });
      await refresh();
    } catch (e) { setError(e); }
  };
  const handleStop = async (id, result = "completed") => {
    try {
      await stopBatch(id, { result, operator: currentUser?.username || undefined });
      await refresh();
    } catch (e) { setError(e); }
  };
  const handleDelete = async (id) => {
    if (!window.confirm("Delete this batch and its membership/summary rows? Audit log is preserved.")) return;
    try { await deleteBatch(id); await refresh(); }
    catch (e) { setError(e); }
  };
  // Barcode scan → start a batch whose identifier is the code. If exactly one
  // barcode-enabled type exists, the backend picks it automatically.
  const handleScan = async (code) => {
    const c = String(code || "").trim();
    if (!c) return;
    setScanBusy(true); setScanMsg("");
    try {
      const b = await scanBatch({ code: c, action: "start", operator: currentUser?.username || undefined });
      setScanMsg(`Started ${b.identifier} (${b.status}).`);
      setScanCode("");
      await refresh();
    } catch (e) {
      setError(e);
      setScanMsg("");
    } finally { setScanBusy(false); }
  };

  if (selectedId) {
    return (
      <BatchDetailPage
        batchId={selectedId}
        onBack={() => { setSelectedId(null); refresh(); }}
        currentUser={currentUser}
        batchTypes={batchTypes}
      />
    );
  }

  return (
    <div className="page-fill">
      <section className="card">
        <div className="form-grid" style={{ alignItems: "flex-end" }}>
          <label>
            Status
            <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
              <option value="">All</option>
              <option value="created">Created</option>
              <option value="running">Running</option>
              <option value="completed">Completed</option>
              <option value="validated">Validated</option>
              <option value="failed">Failed</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <label>
            Search
            <input
              placeholder="ID, product, operator"
              value={filterSearch}
              onChange={(e) => setFilterSearch(e.target.value)}
            />
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" disabled={busy} onClick={refresh}>Refresh</button>
            <button className="btn btn-primary" onClick={() => setShowCreate(true)}>New Batch</button>
          </div>
        </div>
        {/* Operator 2026-07-06: barcode scan-to-start. Focus this field and scan;
            the wedge scanner types the code + Enter, which starts the batch. */}
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
          <span aria-hidden style={{ fontSize: 16 }}>▮▮▍</span>
          <input
            style={{ minWidth: 260 }}
            placeholder="Scan barcode to start a batch…"
            value={scanCode}
            disabled={scanBusy}
            onChange={(e) => setScanCode(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); handleScan(scanCode); } }}
          />
          <button className="btn btn-secondary btn-sm" disabled={scanBusy || !scanCode.trim()} onClick={() => handleScan(scanCode)}>
            {scanBusy ? "Starting…" : "Start from scan"}
          </button>
          {scanMsg ? <span style={{ color: "var(--muted)", fontSize: 12 }}>{scanMsg}</span> : null}
          <span style={{ color: "var(--muted)", fontSize: 11 }}>Requires a batch type with Start method = Barcode.</span>
        </div>
        {error ? <div className="alert alert-error" style={{ marginTop: 12 }}>{error}</div> : null}
      </section>

      <section className="card">
        <h3 style={{ margin: 0 }}>Batches ({total})</h3>
        <div className="table" style={{ marginTop: 12 }}>
          <div className="thead">
            <span>Identifier</span>
            <span>Type</span>
            <span>Status</span>
            <span>Started</span>
            <span>Ended</span>
            <span>Operator</span>
            <span>Actions</span>
          </div>
          {rows.map((b) => {
            const bt = batchTypes.find((t) => t.id === b.batch_type_id);
            return (
              <div key={b.id} className="trow" onClick={() => setSelectedId(b.id)} style={{ cursor: "pointer" }}>
                <span>{b.identifier || b.id}</span>
                <span>{bt?.name || "—"}</span>
                <span><StatusPill status={b.status} /></span>
                <span>{formatTs(b.started_utc)}</span>
                <span>{formatTs(b.ended_utc)}</span>
                <span>{b.operator || "—"}</span>
                <span className="row-actions" onClick={(e) => e.stopPropagation()}>
                  {b.status === "created" || b.status === "waiting"
                    ? <button className="btn btn-primary btn-sm" onClick={() => handleStart(b.id)}>Start</button>
                    : null}
                  {b.status === "running"
                    ? <button className="btn btn-secondary btn-sm" onClick={() => handleStop(b.id, "completed")}>Stop</button>
                    : null}
                  <button className="btn btn-danger btn-sm" onClick={() => handleDelete(b.id)}>Delete</button>
                </span>
              </div>
            );
          })}
          {rows.length === 0 ? (
            <div className="trow"><span>No batches.</span></div>
          ) : null}
        </div>
      </section>

      {showCreate ? (
        <Modal onClose={() => setShowCreate(false)} width={640}>
            <h3 style={{ marginTop: 0 }}>New Batch</h3>
            <div className="form-grid">
              <label>
                Batch Type
                <select
                  value={createForm.batch_type_id}
                  onChange={(e) => setCreateForm({ ...createForm, batch_type_id: e.target.value })}
                >
                  <option value="">(none)</option>
                  {batchTypes.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </label>
              <label>
                Identifier
                <input
                  placeholder="(auto-generated if blank)"
                  value={createForm.identifier}
                  onChange={(e) => setCreateForm({ ...createForm, identifier: e.target.value })}
                />
              </label>
              <label>
                Product
                <input value={createForm.product} onChange={(e) => setCreateForm({ ...createForm, product: e.target.value })} />
              </label>
              <label>
                Recipe
                <input value={createForm.recipe} onChange={(e) => setCreateForm({ ...createForm, recipe: e.target.value })} />
              </label>
              <label>
                Operator
                <input value={createForm.operator} onChange={(e) => setCreateForm({ ...createForm, operator: e.target.value })} />
              </label>
              <label>
                Gateway
                <select
                  value={createForm.gateway_id}
                  onChange={(e) => setCreateForm({ ...createForm, gateway_id: e.target.value })}
                >
                  <option value="">(any)</option>
                  {allGatewayOptions.map((g) => (
                    <option key={g.id} value={g.id}>{g.name || g.id}</option>
                  ))}
                </select>
              </label>
              <label>
                Parent batch (optional)
                <select
                  value={createForm.parent_batch_id || ""}
                  onChange={(e) => setCreateForm({ ...createForm, parent_batch_id: e.target.value })}
                >
                  <option value="">(none)</option>
                  {rows.filter((b) => b.status === "running" || b.status === "created").map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.identifier || b.id} {b.product ? `· ${b.product}` : ""}
                    </option>
                  ))}
                </select>
                <small style={{ color: "var(--muted)", fontSize: 11 }}>
                  Group this batch under a parent so a single rollup PDF can cover the shift/run.
                </small>
              </label>
              <label style={{ gridColumn: "1 / -1" }}>
                Notes
                <textarea
                  rows={3}
                  value={createForm.notes}
                  onChange={(e) => setCreateForm({ ...createForm, notes: e.target.value })}
                />
              </label>
            </div>
            <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={handleCreate}>Create</button>
            </div>
        </Modal>
      ) : null}
    </div>
  );
}


// -----------------------------------------------------------------------
// Batch Detail page — events, summaries, historian slice, validation
// -----------------------------------------------------------------------
function BatchDetailPage({ batchId, onBack, currentUser, batchTypes }) {
  const [batch, setBatch] = useState(null);
  const [events, setEvents] = useState([]);
  const [summaries, setSummaries] = useState([]);
  const [historian, setHistorian] = useState([]);
  const [eventComment, setEventComment] = useState("");
  const [validationNotes, setValidationNotes] = useState("");
  const [error, setError] = useToastError();
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [b, ev, sm, hist] = await Promise.all([
        getBatch(batchId),
        listBatchEvents(batchId, 200),
        listBatchSummaries(batchId),
        listBatchHistorianRows(batchId, 1000),
      ]);
      setBatch(b);
      setEvents(ev);
      setSummaries(sm);
      setHistorian(hist);
    } catch (e) { setError(e); }
    finally { setBusy(false); }
  }, [batchId, setError]);

  useEffect(() => { refresh(); }, [refresh]);

  const addComment = async () => {
    if (!eventComment.trim()) return;
    try {
      await addBatchEvent(batchId, { kind: "operator.comment", severity: "info", message: eventComment.trim() });
      setEventComment("");
      await refresh();
    } catch (e) { setError(e); }
  };

  const handleValidate = async (decision) => {
    try {
      await validateBatch(batchId, { decision, notes: validationNotes });
      setValidationNotes("");
      await refresh();
    } catch (e) { setError(e); }
  };

  const handleNextChild = async () => {
    try { await nextChildBatch(batchId); await refresh(); }
    catch (e) { setError(e); }
  };

  const handleRecompute = async () => {
    try { await recomputeBatchSummaries(batchId); await refresh(); }
    catch (e) { setError(e); }
  };

  if (!batch) return (
    <div className="page-fill"><section className="card">{busy ? "Loading…" : "Batch not found."}<div><button className="btn btn-secondary" onClick={onBack}>Back</button></div></section></div>
  );

  const bt = batchTypes.find((t) => t.id === batch.batch_type_id);
  return (
    <div className="page-fill">
      <section className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <button className="btn btn-secondary btn-sm" onClick={onBack}>← Back</button>
            <span style={{ marginLeft: 12, fontWeight: 600 }}>{batch.identifier || batch.id}</span>
            <span style={{ marginLeft: 12 }}><StatusPill status={batch.status} /></span>
          </div>
          <div>
            {/* Operator 2026-07-06: MULTIPLE parent → manual "roll to next child". */}
            {((bt?.batch_kind === "multiple") && batch.status === "running") ? (
              <button className="btn btn-primary btn-sm" style={{ marginRight: 8 }}
                      title="Close the current sub-batch and start the next one."
                      onClick={handleNextChild}>Next child ▸</button>
            ) : null}
            <a
              className="btn btn-secondary btn-sm"
              href={bmDownloadUrl(`/batches/${encodeURIComponent(batchId)}/report.pdf`)}
              target="_blank"
              rel="noreferrer"
              style={{ marginRight: 8 }}
            >Download PDF</a>
            {/* Parent-rollup PDF: only meaningful for a batch that has children
                under it. Always linked — the endpoint returns an empty
                "no children" page if there are none. */}
            <a
              className="btn btn-secondary btn-sm"
              href={bmDownloadUrl(`/batches/${encodeURIComponent(batchId)}/rollup-report.pdf`)}
              target="_blank"
              rel="noreferrer"
              style={{ marginRight: 8 }}
              title="If this batch has child batches, download a single PDF covering the parent + every child."
            >Rollup PDF</a>
            {/* Operator 2026-07-06: CSV exports — summaries and raw readings. */}
            <a
              className="btn btn-secondary btn-sm"
              href={bmDownloadUrl(`/batches/${encodeURIComponent(batchId)}/report.csv`)}
              target="_blank" rel="noreferrer" style={{ marginRight: 8 }}
              title="Download the per-tag summary statistics as CSV."
            >CSV (summary)</a>
            <a
              className="btn btn-secondary btn-sm"
              href={bmDownloadUrl(`/batches/${encodeURIComponent(batchId)}/historian.csv`)}
              target="_blank" rel="noreferrer" style={{ marginRight: 8 }}
              title="Download the raw historian readings captured during the batch as CSV."
            >CSV (readings)</a>
            {batch.status === "completed" ? (
              <>
                <input
                  placeholder="Validation notes…"
                  value={validationNotes}
                  onChange={(e) => setValidationNotes(e.target.value)}
                  style={{ marginRight: 8 }}
                />
                <button className="btn btn-primary btn-sm" onClick={() => handleValidate("approved")}>Approve</button>
                <button className="btn btn-danger btn-sm" onClick={() => handleValidate("rejected")}>Reject</button>
              </>
            ) : null}
          </div>
        </div>
        {error ? <div className="alert alert-error" style={{ marginTop: 12 }}>{error}</div> : null}
        <div className="form-grid" style={{ marginTop: 16 }}>
          <div><strong>Type:</strong> {bt?.name || "—"}</div>
          <div><strong>Product:</strong> {batch.product || "—"}</div>
          <div><strong>Recipe:</strong> {batch.recipe || "—"}</div>
          <div><strong>Operator:</strong> {batch.operator || "—"}</div>
          <div><strong>Gateway:</strong> {batch.gateway_id || "—"}</div>
          <div><strong>Started:</strong> {formatTs(batch.started_utc)}</div>
          <div><strong>Ended:</strong> {formatTs(batch.ended_utc)}</div>
          <div><strong>Created:</strong> {formatTs(batch.created_utc)}</div>
        </div>
        {batch.notes ? <div style={{ marginTop: 12 }}><strong>Notes:</strong> {batch.notes}</div> : null}
      </section>

      <section className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Summary ({summaries.length} tags)</h3>
          <button className="btn btn-secondary btn-sm" onClick={handleRecompute}>Recompute</button>
        </div>
        <div className="table" style={{ marginTop: 12 }}>
          <div className="thead">
            <span>Tag</span><span>Min</span><span>Max</span><span>Avg</span>
            <span>First</span><span>Last</span><span>σ</span><span>Count</span>
          </div>
          {summaries.map((s, i) => (
            <div key={i} className="trow">
              <span>{s.tag_name}</span>
              <span>{Number(s.min_value).toFixed(3)}</span>
              <span>{Number(s.max_value).toFixed(3)}</span>
              <span>{Number(s.avg_value).toFixed(3)}</span>
              <span>{Number(s.first_value).toFixed(3)}</span>
              <span>{Number(s.last_value).toFixed(3)}</span>
              <span>{Number(s.stdev_value).toFixed(3)}</span>
              <span>{s.sample_count}</span>
            </div>
          ))}
          {summaries.length === 0 ? (
            <div className="trow"><span>No summary yet. Stop the batch or click Recompute.</span></div>
          ) : null}
        </div>
      </section>

      <section className="card">
        <h3 style={{ margin: 0 }}>Events ({events.length})</h3>
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <input
            placeholder="Add operator comment…"
            value={eventComment}
            onChange={(e) => setEventComment(e.target.value)}
            style={{ flex: 1 }}
          />
          <button className="btn btn-primary btn-sm" onClick={addComment}>Add</button>
        </div>
        <div className="table" style={{ marginTop: 12 }}>
          <div className="thead"><span>Time</span><span>Kind</span><span>Severity</span><span>Actor</span><span>Message</span></div>
          {events.map((e) => (
            <div key={e.id} className="trow">
              <span>{formatTs(e.ts_utc)}</span>
              <span>{e.kind}</span>
              <span>{e.severity}</span>
              <span>{e.actor || "—"}</span>
              <span>{e.message || ""}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <h3 style={{ margin: 0 }}>Historian rows in this batch (first 1000)</h3>
        <div className="table" style={{ marginTop: 12, maxHeight: 400, overflowY: "auto" }}>
          <div className="thead"><span>Timestamp</span><span>Tag</span><span>Value</span><span>Quality</span><span>Gateway</span></div>
          {historian.map((r, i) => (
            <div key={i} className="trow">
              <span>{formatTs(r.ts_utc)}</span>
              <span>{r.tag_name}</span>
              <span>{r.value != null ? Number(r.value).toFixed(3) : (r.value_text || "")}</span>
              <span>{r.quality_label || r.quality || ""}</span>
              <span>{r.gateway_name || r.gateway_id || ""}</span>
            </div>
          ))}
          {historian.length === 0 ? (
            <div className="trow"><span>No historian rows in the batch window.</span></div>
          ) : null}
        </div>
      </section>
    </div>
  );
}


// -----------------------------------------------------------------------
// Batch Types page — admin-only CRUD
// -----------------------------------------------------------------------
// Operator 2026-07-06: a small preset-based schedule editor used for the
// per-type start / stop / report schedules. Emits the same simple dict the
// backend daemon understands: {enabled, freq, time, weekday, every_minutes}.
// No cron knowledge required — dropdowns + a time/number field.
function SchedulePreset({ label, help, value, onChange }) {
  const v = value || {};
  const set = (patch) => onChange({ ...v, ...patch });
  const freq = v.freq || "daily";
  const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return (
    <div style={{ marginBottom: 10 }}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <input type="checkbox" checked={!!v.enabled}
               onChange={(e) => set({ enabled: e.target.checked })} />
        <strong style={{ fontSize: 12 }}>{label}</strong>
      </label>
      {help ? <small style={{ color: "var(--muted)", fontSize: 11, display: "block", marginBottom: 6 }}>{help}</small> : null}
      {v.enabled ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", paddingLeft: 24 }}>
          <select value={freq} onChange={(e) => set({ freq: e.target.value })}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="hourly">Hourly</option>
            <option value="every_minutes">Every N minutes</option>
          </select>
          {freq === "weekly" ? (
            <select value={String(v.weekday ?? 0)} onChange={(e) => set({ weekday: parseInt(e.target.value, 10) })}>
              {DOW.map((d, i) => <option key={i} value={i}>{d}</option>)}
            </select>
          ) : null}
          {(freq === "daily" || freq === "weekly") ? (
            <input type="time" value={v.time || "06:00"} onChange={(e) => set({ time: e.target.value })}
                   style={{ width: 110 }} />
          ) : null}
          {freq === "hourly" ? (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12 }}>
              at minute
              <input type="number" min={0} max={59} value={v.minute ?? 0}
                     onChange={(e) => set({ minute: Math.max(0, Math.min(59, parseInt(e.target.value || "0", 10))) })}
                     style={{ width: 60 }} />
            </span>
          ) : null}
          {freq === "every_minutes" ? (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12 }}>
              every
              <input type="number" min={1} max={1440} value={v.every_minutes ?? 15}
                     onChange={(e) => set({ every_minutes: Math.max(1, parseInt(e.target.value || "1", 10)) })}
                     style={{ width: 60 }} />
              min
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function BatchTypesPage() {
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(null);
  const [error, setError] = useToastError();
  const blank = useMemo(() => ({
    name: "", description: "", start_method: "manual", end_method: "manual",
    collection_profile: "continuous", identifier_method: "auto", identifier_prefix: "",
    enabled: true, summary_tags: [],
    start_schedule: null, stop_schedule: null, report_schedule: null,
    batch_kind: "single", child_type_id: "",
  }), []);
  // Single types available to be the "child" of a Multiple type.
  const singleTypes = useMemo(
    () => rows.filter((t) => (t.batch_kind || "single") === "single" && t.id !== editing?.id),
    [rows, editing]);

  const refresh = useCallback(async () => {
    setBusy(true);
    try { setRows(await listBatchTypes()); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  }, [setError]);
  useEffect(() => { refresh(); }, [refresh]);

  const save = async () => {
    try {
      await saveBatchType(editing, editing?.id || null);
      setEditing(null);
      await refresh();
    } catch (e) { setError(e); }
  };
  const remove = async (id) => {
    if (!window.confirm("Delete this batch type? Existing batches keep their type_id reference but lose the dictionary entry.")) return;
    try { await deleteBatchType(id); await refresh(); }
    catch (e) { setError(e); }
  };

  return (
    <div className="page-fill">
      <section className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Batch Types</h3>
          <div style={{ display: "flex", gap: 8 }}>
            {rows.length === 0 ? (
              <button className="btn btn-secondary" onClick={async () => { try { await seedBatchDefaults(); await refresh(); } catch (e) { setError(e); } }}>
                Seed starter types
              </button>
            ) : null}
            <button className="btn btn-primary" onClick={() => setEditing({ ...blank })}>New Type</button>
          </div>
        </div>
        {error ? <div className="alert alert-error" style={{ marginTop: 12 }}>{error}</div> : null}
        <div className="table" style={{ marginTop: 12 }}>
          <div className="thead">
            <span>Name</span><span>Kind</span><span>Start</span><span>End</span>
            <span>Profile</span><span>Identifier</span><span>Enabled</span><span>Actions</span>
          </div>
          {rows.map((t) => (
            <div key={t.id} className="trow">
              <span>{t.name}</span>
              <span>{(t.batch_kind || "single") === "multiple" ? "Multiple" : "Single"}</span>
              <span>{t.start_method}</span>
              <span>{t.end_method}</span>
              <span>{t.collection_profile}</span>
              <span>{t.identifier_method}</span>
              <span>{t.enabled ? "Yes" : "No"}</span>
              <span className="row-actions">
                <button className="btn btn-secondary btn-sm" onClick={() => setEditing({ ...t })}>Edit</button>
                <button className="btn btn-danger btn-sm" onClick={() => remove(t.id)}>Delete</button>
              </span>
            </div>
          ))}
          {rows.length === 0 ? <div className="trow"><span>{busy ? "Loading…" : "No batch types yet — click “Seed starter types” to create Single + Multiple."}</span></div> : null}
        </div>
      </section>

      {editing ? (
        <Modal onClose={() => setEditing(null)} width={760}>
            <h3 style={{ marginTop: 0 }}>{editing.id ? "Edit Batch Type" : "New Batch Type"}</h3>
            {/* Operator 2026-07-06: Single vs Multiple is the primary choice. */}
            <div className="card" style={{ padding: 12, marginBottom: 12, background: "var(--surface-elev, var(--card))" }}>
              <div className="form-grid">
                <label>
                  Batch kind
                  <select value={editing.batch_kind || "single"}
                          onChange={(e) => setEditing({ ...editing, batch_kind: e.target.value })}>
                    <option value="single">Single — one continuous run</option>
                    <option value="multiple">Multiple — auto-spawns Single children</option>
                  </select>
                </label>
                {(editing.batch_kind === "multiple") ? (
                  <label>
                    Child type (the Single batch to spawn)
                    <select value={editing.child_type_id || ""}
                            onChange={(e) => setEditing({ ...editing, child_type_id: e.target.value })}>
                      <option value="">— pick a Single type —</option>
                      {singleTypes.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                  </label>
                ) : null}
              </div>
              <small style={{ color: "var(--muted)", fontSize: 11 }}>
                {editing.batch_kind === "multiple"
                  ? "Multiple: starting the parent opens a child Single; each start-condition fire (or the 'Next child' button) rolls to a new one; stopping the parent closes everything."
                  : "Single: collects continuously from start to stop. Start/stop can be manual, a barcode scan, or a tag condition (below)."}
              </small>
            </div>
            <div className="form-grid">
              <label>
                Name
                <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </label>
              <label>
                Identifier Method
                <select value={editing.identifier_method} onChange={(e) => setEditing({ ...editing, identifier_method: e.target.value })}>
                  <option value="auto">Auto</option>
                  <option value="manual">Manual</option>
                  <option value="plc">From PLC tag</option>
                  <option value="barcode">Barcode scan</option>
                </select>
              </label>
              <label>
                Identifier Prefix
                <input value={editing.identifier_prefix || ""} onChange={(e) => setEditing({ ...editing, identifier_prefix: e.target.value })} placeholder="BATCH" />
              </label>
              <label>
                Start Method
                <select value={editing.start_method} onChange={(e) => setEditing({ ...editing, start_method: e.target.value })}>
                  <option value="manual">Manual</option>
                  <option value="plc_trigger">PLC trigger</option>
                  <option value="scheduled">Scheduled</option>
                  <option value="barcode">Barcode</option>
                </select>
              </label>
              <label>
                End Method
                <select value={editing.end_method} onChange={(e) => setEditing({ ...editing, end_method: e.target.value })}>
                  <option value="manual">Manual</option>
                  <option value="plc_trigger">PLC trigger</option>
                  <option value="duration">Duration</option>
                  <option value="quantity">Quantity</option>
                  <option value="scheduled">Scheduled</option>
                </select>
              </label>
              <label>
                Collection Profile
                <select value={editing.collection_profile} onChange={(e) => setEditing({ ...editing, collection_profile: e.target.value })}>
                  <option value="continuous">Continuous</option>
                  <option value="trigger">Trigger</option>
                  <option value="snapshot">Snapshot</option>
                  <option value="event">Event</option>
                  <option value="pre_post">Pre/Post</option>
                </select>
              </label>
              <label style={{ gridColumn: "1 / -1" }}>
                Description
                <textarea rows={3} value={editing.description || ""} onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
              </label>
              <label style={{ gridColumn: "1 / -1" }}>
                Summary tags (comma-separated) — optional
                <input
                  value={editing._summary_tags_text != null
                    ? editing._summary_tags_text
                    : (Array.isArray(editing.summary_tags) ? editing.summary_tags.join(", ") : "")}
                  placeholder="Leave empty to summarize ALL collected tags, e.g. BT_PVA_Level, BT_PVB_Level"
                  onChange={(e) => setEditing({
                    ...editing,
                    _summary_tags_text: e.target.value,
                    summary_tags: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                  })}
                />
                <small style={{ color: "var(--muted)", fontSize: 11 }}>
                  Restricts the batch report/summaries to just these tags. Empty = every tag collected during the batch.
                </small>
              </label>
              <label>
                <input type="checkbox" checked={!!editing.enabled} onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} />
                {" "}Enabled
              </label>
            </div>

            {/* Operator 2026-06-30: email-on-close config */}
            <div className="card" style={{ marginTop: 14, padding: 12, background: "var(--surface-elev, var(--card))" }}>
              <h4 style={{ margin: "0 0 8px 0", fontSize: 13 }}>Email on close</h4>
              <div className="form-grid">
                <label>
                  <input type="checkbox" checked={!!editing.email_on_close}
                         onChange={(e) => setEditing({ ...editing, email_on_close: e.target.checked })} />
                  {" "}Send PDF report when batch closes
                </label>
                <label style={{ gridColumn: "1 / -1" }}>
                  Recipients (comma-separated)
                  <input value={editing.email_recipients || ""}
                         placeholder="qa@example.com, operator@example.com"
                         onChange={(e) => setEditing({ ...editing, email_recipients: e.target.value })} />
                  <small style={{ color: "var(--muted)", fontSize: 11 }}>
                    Uses the global SMTP settings from Settings → Notifications.
                  </small>
                </label>
              </div>
            </div>

            {/* Operator 2026-07-06: time-based schedules (start / stop / report) */}
            <div className="card" style={{ marginTop: 12, padding: 12, background: "var(--surface-elev, var(--card))" }}>
              <h4 style={{ margin: "0 0 8px 0", fontSize: 13 }}>Time schedules</h4>
              <small style={{ color: "var(--muted)", fontSize: 11, display: "block", marginBottom: 10 }}>
                Times use this machine's local clock. Runs alongside (not instead of) any PLC triggers below.
              </small>
              <SchedulePreset
                label="Auto-start on a schedule"
                help="Creates and starts a new batch of this type at the chosen time."
                value={editing.start_schedule}
                onChange={(s) => setEditing({ ...editing, start_schedule: s })}
              />
              <SchedulePreset
                label="Auto-stop on a schedule"
                help="Stops any running batch of this type at the chosen time."
                value={editing.stop_schedule}
                onChange={(s) => setEditing({ ...editing, stop_schedule: s })}
              />
              <SchedulePreset
                label="Email report on a schedule"
                help="Emails the latest batch's PDF + CSV to the recipients above."
                value={editing.report_schedule}
                onChange={(s) => setEditing({ ...editing, report_schedule: s })}
              />
            </div>

            {/* Operator 2026-06-30: PLC auto-trigger config (start + stop) */}
            <div className="card" style={{ marginTop: 12, padding: 12, background: "var(--surface-elev, var(--card))" }}>
              <h4 style={{ margin: "0 0 8px 0", fontSize: 13 }}>PLC auto-trigger conditions</h4>
              <small style={{ color: "var(--muted)", fontSize: 11, display: "block", marginBottom: 8 }}>
                JSON: <code>{`{"operator": "AND" | "OR", "rules": [{"tag": "PLC1.RUN", "kind": "rising_edge"}, ...]}`}</code>
                <br />
                Rule kinds: <code>rising_edge</code>, <code>falling_edge</code>,{" "}
                <code>{`threshold (+op: > | >= | < | <=, value, hysteresis)`}</code>, <code>{`equals (+value)`}</code>.
                Watcher polls every ~2s with 5s debounce per type.
              </small>
              <label style={{ display: "block", marginBottom: 8 }}>
                Start condition
                <textarea
                  rows={4}
                  style={{ fontFamily: "monospace", fontSize: 12 }}
                  value={editing._trigger_start_text != null
                    ? editing._trigger_start_text
                    : (editing.trigger_start ? JSON.stringify(editing.trigger_start, null, 2) : "")}
                  placeholder={`{"operator":"AND","rules":[{"tag":"PLC1.RUN","kind":"rising_edge"}]}`}
                  onChange={(e) => {
                    let parsed = null;
                    try { parsed = e.target.value.trim() ? JSON.parse(e.target.value) : null; } catch { parsed = undefined; }
                    setEditing({
                      ...editing,
                      _trigger_start_text: e.target.value,
                      trigger_start: parsed === undefined ? editing.trigger_start : parsed,
                      _trigger_start_invalid: parsed === undefined && !!e.target.value.trim(),
                    });
                  }}
                />
                {editing._trigger_start_invalid ? <small style={{ color: "#dc2626" }}>Invalid JSON — fix before saving.</small> : null}
              </label>
              <label style={{ display: "block" }}>
                Stop condition
                <textarea
                  rows={4}
                  style={{ fontFamily: "monospace", fontSize: 12 }}
                  value={editing._trigger_stop_text != null
                    ? editing._trigger_stop_text
                    : (editing.trigger_stop ? JSON.stringify(editing.trigger_stop, null, 2) : "")}
                  placeholder={`{"operator":"AND","rules":[{"tag":"PLC1.RUN","kind":"falling_edge"}]}`}
                  onChange={(e) => {
                    let parsed = null;
                    try { parsed = e.target.value.trim() ? JSON.parse(e.target.value) : null; } catch { parsed = undefined; }
                    setEditing({
                      ...editing,
                      _trigger_stop_text: e.target.value,
                      trigger_stop: parsed === undefined ? editing.trigger_stop : parsed,
                      _trigger_stop_invalid: parsed === undefined && !!e.target.value.trim(),
                    });
                  }}
                />
                {editing._trigger_stop_invalid ? <small style={{ color: "#dc2626" }}>Invalid JSON — fix before saving.</small> : null}
              </label>
            </div>

            <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn btn-primary" disabled={!!(editing._trigger_start_invalid || editing._trigger_stop_invalid)} onClick={save}>Save</button>
            </div>
        </Modal>
      ) : null}
    </div>
  );
}


// -----------------------------------------------------------------------
// Batch Audit page — read-only audit log viewer
// -----------------------------------------------------------------------
export function BatchAuditPage() {
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useToastError();

  const refresh = useCallback(async () => {
    setBusy(true);
    try { setRows(await listBatchAudit(500)); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  }, [setError]);
  useEffect(() => { refresh(); }, [refresh]);

  return (
    <div className="page-fill">
      <section className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Batch Audit Trail</h3>
          <button className="btn btn-secondary" disabled={busy} onClick={refresh}>Refresh</button>
        </div>
        {error ? <div className="alert alert-error" style={{ marginTop: 12 }}>{error}</div> : null}
        <div className="table" style={{ marginTop: 12 }}>
          <div className="thead"><span>Time</span><span>Actor</span><span>Action</span><span>Batch</span><span>Details</span></div>
          {rows.map((r) => (
            <div key={r.id} className="trow">
              <span>{formatTs(r.ts_utc)}</span>
              <span>{r.actor || "—"}</span>
              <span>{r.action}</span>
              <span>{r.batch_id || r.batch_type_id || "—"}</span>
              <span style={{ fontFamily: "monospace", fontSize: 12 }}>
                {r.after ? JSON.stringify(r.after).slice(0, 200) : ""}
              </span>
            </div>
          ))}
          {rows.length === 0 ? <div className="trow"><span>{busy ? "Loading…" : "No audit entries."}</span></div> : null}
        </div>
      </section>
    </div>
  );
}
