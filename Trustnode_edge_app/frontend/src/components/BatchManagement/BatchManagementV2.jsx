/* TrustNode Batch Management v2 (clean rebuild) — page components.

   Three main pages per spec:
     <BatchOverviewV2Page/>     -> /batches         (active batches + groups, actions, report preview)
     <BatchDefinitionsV2Page/>  -> /batch-definitions (guided multi-step builder)
     <BatchAnalysisV2Page/>     -> /batch-analysis   (reports, comparison, group perf, excursions)
   plus detail views (batch + group) reached from the overview, and a shared
   <ReportPreviewModal/>.

   Styling reuses the project-wide CSS tokens/classes only (card, table, btn,
   status-pill, form-grid, modal-*), so dark AND light mode work with zero new
   stylesheets. Permission gating via the `canEdit` prop (from canEditPage).

   Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
*/
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  Legend, ReferenceLine,
} from "recharts";
import {
  bmv2Status, bmv2SeedReportTemplates,
  bmv2ListDefinitions, bmv2GetDefinition, bmv2SaveDefinition, bmv2DeleteDefinition,
  bmv2ValidateDefinition, bmv2PublishDefinition, bmv2ListVersions, bmv2NewVersion,
  bmv2ListBatches, bmv2GetBatch, bmv2CreateBatch, bmv2BatchAction, bmv2AddComment,
  bmv2BatchEvents, bmv2BatchTrends, bmv2BatchKpis, bmv2RecomputeBatch, bmv2BatchExcursions,
  bmv2BatchCollectedTags, bmv2ListBatchReports, bmv2GenerateBatchReport, bmv2EmailBatchReport,
  bmv2ListGroups, bmv2GetGroup, bmv2CreateGroup, bmv2CompleteGroup, bmv2AbortGroup,
  bmv2GroupBatches, bmv2GroupKpis, bmv2ListGroupReports, bmv2GenerateGroupReport, bmv2EmailGroupReport,
  bmv2AnalysisExcursions, bmv2AckExcursion, bmv2AnalysisComparison,
  bmv2NormalizeGatewayTags, bmv2FileUrl, bmv2PreviewDataUrl,
} from "../../api";

/* --------------------------------------------------------------------- */
/*  shared primitives                                                    */
/* --------------------------------------------------------------------- */
function Modal({ onClose, children, width }) {
  const node = (
    <div className="modal-backdrop" onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", zIndex: 10000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}
        style={{ background: "var(--card)", color: "var(--text)", border: "1px solid var(--stroke)",
          borderRadius: 10, width: "100%", maxWidth: width || 720, maxHeight: "92vh",
          overflow: "auto", padding: 20, boxShadow: "0 12px 40px rgba(0,0,0,0.45)" }}>
        {children}
      </div>
    </div>
  );
  return typeof document === "undefined" ? node : createPortal(node, document.body);
}

const STATUS_CLASS = {
  running: "status-online", active: "status-online", completed: "status-online",
  within_specification: "status-online", good: "status-online",
  held: "status-warning", ready: "status-warning", planned: "status-warning",
  with_warnings: "status-warning", good_with_warnings: "status-warning", incomplete: "status-warning",
  aborted: "status-offline", invalid: "status-offline", failed: "status-offline",
  out_of_specification: "status-offline", data_incomplete: "status-offline",
};
function Pill({ value }) {
  const s = String(value || "").toLowerCase();
  const cls = STATUS_CLASS[s] || "status-warning";
  return <span className={`status-pill ${cls}`}>{(s || "—").replace(/_/g, " ").toUpperCase()}</span>;
}

function fmtTs(s) {
  const raw = String(s || "").trim();
  if (!raw) return "—";
  try {
    const d = new Date(raw.includes("T") ? raw : raw.replace(" ", "T") + (raw.endsWith("Z") ? "" : "Z"));
    if (isNaN(d.getTime())) return raw;
    return d.toLocaleString();
  } catch { return raw; }
}
function humanDur(startS, endS) {
  if (!startS) return "—";
  const a = new Date(String(startS).replace(" ", "T") + "Z").getTime();
  const b = endS ? new Date(String(endS).replace(" ", "T") + "Z").getTime() : Date.now();
  if (isNaN(a) || isNaN(b) || b < a) return "—";
  let s = Math.floor((b - a) / 1000);
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  return `${h ? h + "h " : ""}${m ? m + "m " : ""}${s}s`;
}
function fmtNum(v, unit) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  const t = Math.abs(n) >= 100 ? n.toFixed(0) : Math.abs(n) >= 1 ? n.toFixed(2) : n.toFixed(3);
  return `${t}${unit && unit !== "count" ? " " + unit : ""}`;
}

function Card({ title, actions, children, style }) {
  return (
    <section className="card" style={{ marginBottom: 14, ...(style || {}) }}>
      {(title || actions) && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, gap: 8, flexWrap: "wrap" }}>
          {title ? <h3 style={{ margin: 0 }}>{title}</h3> : <span />}
          {actions ? <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>{actions}</div> : null}
        </div>
      )}
      {children}
    </section>
  );
}

function SummaryCard({ label, value, tone }) {
  const color = tone === "warn" ? "var(--danger)" : tone === "ok" ? "var(--teal)" : "var(--text)";
  return (
    <div className="mini-card" style={{ background: "var(--card)", border: "1px solid var(--stroke)",
      borderRadius: 12, padding: "12px 14px", minWidth: 120, flex: "1 1 120px" }}>
      <div style={{ fontSize: 12, color: "var(--muted)" }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, color }}>{value}</div>
    </div>
  );
}

function Banner({ children, tone }) {
  const bg = tone === "error" ? "var(--error-bg)" : "var(--card)";
  const col = tone === "error" ? "var(--error-text)" : "var(--muted)";
  const bd = tone === "error" ? "var(--error-border)" : "var(--stroke)";
  return <div style={{ background: bg, color: col, border: `1px solid ${bd}`, borderRadius: 8, padding: "8px 12px", fontSize: 13, marginBottom: 10 }}>{children}</div>;
}

/* --------------------------------------------------------------------- */
/*  Report Preview Modal (shared for batch + group reports)              */
/* --------------------------------------------------------------------- */
function ReportPreviewModal({ report, onClose, onRegenerate, onEmail, canEdit }) {
  // `report` = a batch_report_reference row enriched with generated_report_id.
  const gid = report?.generated_report_id;
  const [emailing, setEmailing] = useState(false);
  const [recips, setRecips] = useState("");
  const [showEmail, setShowEmail] = useState(false);
  const [msg, setMsg] = useState("");
  const pdfUrl = gid ? bmv2FileUrl(gid, true) : null;

  const doEmail = async () => {
    setEmailing(true); setMsg("");
    try {
      const r = await onEmail(report.id, recips.split(/[,;\s]+/).filter(Boolean));
      setMsg(r?.ok ? "Sent." : `Failed: ${r?.message || "email error"}`);
    } catch (e) { setMsg(`Failed: ${e?.message || e}`); }
    finally { setEmailing(false); }
  };

  return (
    <Modal onClose={onClose} width={980}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, gap: 8 }}>
        <div>
          <h3 style={{ margin: 0 }}>Report preview</h3>
          <div style={{ fontSize: 12, color: "var(--muted)" }}>
            {(report?.report_kind || "").replace(/_/g, " ")} · generated {fmtTs(report?.generated_utc)}
            {" · "}<Pill value={report?.report_status} />
          </div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
      </div>

      {report?.report_status !== "generated" || !gid ? (
        <Banner tone={report?.report_status === "failed" ? "error" : undefined}>
          {report?.report_status === "failed"
            ? `Report generation failed: ${report?.report_error || "unknown error"}`
            : "No generated file yet. Use “Regenerate”."}
        </Banner>
      ) : (
        <div style={{ height: "64vh", border: "1px solid var(--stroke)", borderRadius: 8, overflow: "hidden", background: "#fff" }}>
          <iframe title="report" src={pdfUrl} style={{ width: "100%", height: "100%", border: "none" }} />
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
        {gid && <a className="btn btn-secondary btn-sm" href={bmv2FileUrl(gid, false)} target="_blank" rel="noreferrer">Download</a>}
        {canEdit && <button className="btn btn-primary btn-sm" onClick={onRegenerate}>Regenerate</button>}
        {canEdit && gid && <button className="btn btn-secondary btn-sm" onClick={() => setShowEmail((v) => !v)}>Send by email</button>}
        {msg && <span style={{ fontSize: 12, color: "var(--muted)" }}>{msg}</span>}
      </div>
      {showEmail && (
        <div className="form-grid" style={{ marginTop: 10, gridTemplateColumns: "1fr auto" }}>
          <label>Recipients (comma-separated)
            <input value={recips} onChange={(e) => setRecips(e.target.value)} placeholder="ops@plant.com, qa@plant.com" />
          </label>
          <button className="btn btn-primary btn-sm" style={{ alignSelf: "end" }} disabled={emailing || !recips.trim()} onClick={doEmail}>
            {emailing ? "Sending…" : "Send"}
          </button>
        </div>
      )}
    </Modal>
  );
}

/* --------------------------------------------------------------------- */
/*  Trend chart (Recharts) — shared by batch detail + comparison         */
/* --------------------------------------------------------------------- */
const SERIES_COLORS = ["#14a89a", "#4f8ef7", "#e0a63c", "#d1497f", "#8b5cf6", "#2ea043"];
function TrendChart({ series, xKey = "ts", height = 260, limitLines = [] }) {
  // series: [{tag, points:[{ts|elapsed_s, value}]}]
  const data = useMemo(() => {
    const byX = new Map();
    (series || []).forEach((s) => {
      (s.points || []).forEach((p) => {
        const x = p[xKey] ?? p.ts ?? p.elapsed_s;
        if (!byX.has(x)) byX.set(x, { x });
        byX.get(x)[s.tag] = p.value;
      });
    });
    return Array.from(byX.values()).sort((a, b) => (a.x > b.x ? 1 : -1));
  }, [series, xKey]);
  if (!series || !series.length) return <div style={{ color: "var(--muted)", fontSize: 13, padding: 12 }}>No trend data.</div>;
  return (
    <div className="chart-wrap" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--stroke)" />
          <XAxis dataKey="x" tick={{ fontSize: 10 }} tickFormatter={(v) => (xKey === "ts" ? String(v).slice(11, 19) : `${v}s`)} minTickGap={40} />
          <YAxis tick={{ fontSize: 10 }} width={44} />
          <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--stroke)", color: "var(--text)", fontSize: 12 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {(series || []).map((s, i) => (
            <Line key={s.tag} type="monotone" dataKey={s.tag} stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              dot={false} strokeWidth={1.6} isAnimationActive={false} connectNulls />
          ))}
          {(limitLines || []).map((l, i) => (
            <ReferenceLine key={i} y={l.value} stroke={l.color || "var(--danger)"} strokeDasharray="5 4"
              label={{ value: l.label, fontSize: 10, fill: l.color || "var(--danger)" }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/* --------------------------------------------------------------------- */
/*  License gate wrapper                                                  */
/* --------------------------------------------------------------------- */
function useLicense() {
  const [ok, setOk] = useState(null);
  useEffect(() => { let m = true; bmv2Status().then((s) => m && setOk(!!s.enabled)).catch(() => m && setOk(false)); return () => { m = false; }; }, []);
  return ok;
}
function Unlicensed() {
  return <Card><Banner>Batch Management is not licensed on this edge. Contact your administrator.</Banner></Card>;
}

/* --------------------------------------------------------------------- */
/*  PAGE 1 — Batch Overview                                               */
/* --------------------------------------------------------------------- */
export function BatchOverviewV2Page({ canEdit = false }) {
  const lic = useLicense();
  const [tab, setTab] = useState("batches");
  const [batches, setBatches] = useState([]);
  const [groups, setGroups] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [openBatch, setOpenBatch] = useState(null);   // batch id -> detail
  const [openGroup, setOpenGroup] = useState(null);
  const [creating, setCreating] = useState(null);      // "batch" | "group"
  const [err, setErr] = useState("");
  const [defs, setDefs] = useState([]);

  const load = useCallback(async () => {
    setErr("");
    try {
      const [b, g] = await Promise.all([
        bmv2ListBatches({ limit: 200, status: statusFilter, search }),
        bmv2ListGroups({ limit: 100 }),
      ]);
      setBatches(b.rows || []); setGroups(g.rows || []);
    } catch (e) { setErr(e?.message || String(e)); }
  }, [statusFilter, search]);

  useEffect(() => { if (lic) { load(); bmv2ListDefinitions().then(setDefs).catch(() => {}); } }, [lic, load]);
  useEffect(() => { if (!lic) return; const t = setInterval(load, 8000); return () => clearInterval(t); }, [lic, load]);

  if (lic === false) return <Unlicensed />;
  if (lic === null) return <Card><div style={{ color: "var(--muted)" }}>Loading…</div></Card>;

  if (openBatch) return <BatchDetailV2 batchId={openBatch} canEdit={canEdit} onBack={() => { setOpenBatch(null); load(); }} onOpenGroup={(gid) => { setOpenBatch(null); setOpenGroup(gid); }} />;
  if (openGroup) return <BatchGroupDetailV2 groupId={openGroup} canEdit={canEdit} onBack={() => { setOpenGroup(null); load(); }} onOpenBatch={(bid) => { setOpenGroup(null); setOpenBatch(bid); }} />;

  const activeBatches = batches.filter((b) => ["running", "held", "ready"].includes(b.status)).length;
  const activeGroups = groups.filter((g) => g.status === "active").length;
  const today = new Date().toISOString().slice(0, 10);
  const completedToday = batches.filter((b) => b.status === "completed" && String(b.ended_utc || "").slice(0, 10) === today).length;
  const warnings = batches.filter((b) => ["with_warnings", "out_of_specification", "data_incomplete"].includes(b.quality_status)).length;

  return (
    <>
      {err && <Banner tone="error">{err}</Banner>}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 14 }}>
        <SummaryCard label="Active batches" value={activeBatches} tone="ok" />
        <SummaryCard label="Active groups" value={activeGroups} tone="ok" />
        <SummaryCard label="Completed today" value={completedToday} />
        <SummaryCard label="With warnings" value={warnings} tone={warnings ? "warn" : undefined} />
      </div>

      <Card
        title="Batches"
        actions={
          <>
            <div style={{ display: "flex", gap: 6 }}>
              <button className={`btn btn-sm ${tab === "batches" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("batches")}>Batches</button>
              <button className={`btn btn-sm ${tab === "groups" ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab("groups")}>Batch Groups</button>
            </div>
            {canEdit && tab === "batches" && <button className="btn btn-primary btn-sm" onClick={() => setCreating("batch")}>+ Batch</button>}
            {canEdit && tab === "groups" && <button className="btn btn-primary btn-sm" onClick={() => setCreating("group")}>+ Batch Group</button>}
          </>
        }
      >
        {tab === "batches" && (
          <>
            <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ maxWidth: 180 }}>
                <option value="">All statuses</option>
                {["planned", "ready", "running", "held", "completed", "aborted", "invalid"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <input placeholder="Search reference / product…" value={search} onChange={(e) => setSearch(e.target.value)} style={{ maxWidth: 260 }} />
            </div>
            <BatchTable rows={batches} onOpen={setOpenBatch} />
          </>
        )}
        {tab === "groups" && <GroupTable rows={groups} onOpen={setOpenGroup} />}
      </Card>

      {creating === "batch" && <CreateBatchModal defs={defs} onClose={() => setCreating(null)} onCreated={() => { setCreating(null); load(); }} />}
      {creating === "group" && <CreateGroupModal defs={defs} onClose={() => setCreating(null)} onCreated={() => { setCreating(null); load(); }} />}
    </>
  );
}

function BatchTable({ rows, onOpen }) {
  if (!rows.length) return <div style={{ color: "var(--muted)", fontSize: 13, padding: 8 }}>No batches yet.</div>;
  return (
    <div className="table" style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1.2fr 0.9fr 0.9fr", gap: 0 }}>
      <div className="thead"><span>Reference</span><span>Status</span><span>Quality</span><span>Started</span><span>Duration</span><span>Data</span></div>
      {rows.map((b) => (
        <div className="trow" key={b.id} style={{ cursor: "pointer" }} onClick={() => onOpen(b.id)}>
          <span className="db-cell" title={b.id}>{b.reference || b.id}</span>
          <span className="db-cell"><Pill value={b.status} /></span>
          <span className="db-cell"><Pill value={b.quality_status} /></span>
          <span className="db-cell">{fmtTs(b.started_utc)}</span>
          <span className="db-cell">{humanDur(b.started_utc, b.ended_utc)}</span>
          <span className="db-cell"><Pill value={b.data_quality_status} /></span>
        </div>
      ))}
    </div>
  );
}

function GroupTable({ rows, onOpen }) {
  if (!rows.length) return <div style={{ color: "var(--muted)", fontSize: 13, padding: 8 }}>No batch groups yet.</div>;
  return (
    <div className="table" style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1fr 1.2fr", gap: 0 }}>
      <div className="thead"><span>Reference</span><span>Status</span><span>Children</span><span>Progress</span><span>Started</span></div>
      {rows.map((g) => {
        const prog = g.expected_child_count ? Math.round((100 * (g.actual_child_count || 0)) / g.expected_child_count) : null;
        return (
          <div className="trow" key={g.id} style={{ cursor: "pointer" }} onClick={() => onOpen(g.id)}>
            <span className="db-cell" title={g.id}>{g.reference || g.id}</span>
            <span className="db-cell"><Pill value={g.status} /></span>
            <span className="db-cell">{g.actual_child_count || 0}{g.expected_child_count ? ` / ${g.expected_child_count}` : ""}</span>
            <span className="db-cell">{prog === null ? "—" : `${prog}%`}</span>
            <span className="db-cell">{fmtTs(g.started_utc)}</span>
          </div>
        );
      })}
    </div>
  );
}

function CreateBatchModal({ defs, onClose, onCreated }) {
  const [form, setForm] = useState({ definition_id: "", reference: "", product: "", notes: "" });
  const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const published = (defs || []).filter((d) => d.status === "published");
  const save = async () => {
    setBusy(true); setErr("");
    try { await bmv2CreateBatch(form); onCreated(); }
    catch (e) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  return (
    <Modal onClose={onClose} width={560}>
      <h3 style={{ marginTop: 0 }}>Create batch</h3>
      {err && <Banner tone="error">{err}</Banner>}
      <div className="form-grid">
        <label>Definition
          <select value={form.definition_id} onChange={(e) => setForm({ ...form, definition_id: e.target.value })}>
            <option value="">(none — ad-hoc batch)</option>
            {published.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </label>
        <label>Reference (blank = auto)<input value={form.reference} onChange={(e) => setForm({ ...form, reference: e.target.value })} /></label>
        <label>Product<input value={form.product} onChange={(e) => setForm({ ...form, product: e.target.value })} /></label>
        <label>Notes<input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></label>
      </div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={save}>{busy ? "Creating…" : "Create"}</button>
      </div>
    </Modal>
  );
}

function CreateGroupModal({ defs, onClose, onCreated }) {
  const [form, setForm] = useState({ definition_id: "", reference: "", expected_child_count: "" });
  const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const published = (defs || []).filter((d) => d.status === "published");
  const save = async () => {
    setBusy(true); setErr("");
    try {
      await bmv2CreateGroup({ ...form, expected_child_count: form.expected_child_count ? Number(form.expected_child_count) : null });
      onCreated();
    } catch (e) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  return (
    <Modal onClose={onClose} width={560}>
      <h3 style={{ marginTop: 0 }}>Create batch group</h3>
      {err && <Banner tone="error">{err}</Banner>}
      <div className="form-grid">
        <label>Definition
          <select value={form.definition_id} onChange={(e) => setForm({ ...form, definition_id: e.target.value })}>
            <option value="">(none)</option>
            {published.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </label>
        <label>Reference (blank = auto)<input value={form.reference} onChange={(e) => setForm({ ...form, reference: e.target.value })} /></label>
        <label>Expected child count<input type="number" value={form.expected_child_count} onChange={(e) => setForm({ ...form, expected_child_count: e.target.value })} /></label>
      </div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={save}>{busy ? "Creating…" : "Create"}</button>
      </div>
    </Modal>
  );
}

/* --------------------------------------------------------------------- */
/*  Batch Detail                                                         */
/* --------------------------------------------------------------------- */
const BATCH_ACTIONS = {
  planned: [["start", "Start", "btn-primary"], ["abort", "Abort", "btn-danger"]],
  ready: [["start", "Start", "btn-primary"], ["abort", "Abort", "btn-danger"]],
  running: [["hold", "Hold", "btn-warning"], ["stop", "Stop", "btn-primary"], ["abort", "Abort", "btn-danger"]],
  held: [["resume", "Resume", "btn-primary"], ["stop", "Stop", "btn-secondary"], ["abort", "Abort", "btn-danger"]],
};

function BatchDetailV2({ batchId, canEdit, onBack, onOpenGroup }) {
  const [batch, setBatch] = useState(null);
  const [kpis, setKpis] = useState([]);
  const [excursions, setExcursions] = useState([]);
  const [events, setEvents] = useState([]);
  const [series, setSeries] = useState([]);
  const [reports, setReports] = useState([]);
  const [tags, setTags] = useState([]);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [comment, setComment] = useState("");
  const [confirmAction, setConfirmAction] = useState(null);

  const load = useCallback(async () => {
    setErr("");
    try {
      const b = await bmv2GetBatch(batchId);
      setBatch(b);
      const [k, x, e, r, tg] = await Promise.all([
        bmv2BatchKpis(batchId), bmv2BatchExcursions(batchId), bmv2BatchEvents(batchId, 100),
        bmv2ListBatchReports(batchId), bmv2BatchCollectedTags(batchId),
      ]);
      setKpis(k); setExcursions(x); setEvents(e); setReports(r); setTags(tg);
      if (tg.length) setSeries(await bmv2BatchTrends(batchId, tg.slice(0, 4).join(","), 400));
    } catch (ex) { setErr(ex?.message || String(ex)); }
  }, [batchId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!batch || ["completed", "aborted", "invalid"].includes(batch.status)) return;
    const t = setInterval(load, 6000); return () => clearInterval(t);
  }, [batch, load]);

  const doAction = async (action) => {
    setBusy(action); setErr("");
    try { await bmv2BatchAction(batchId, action, {}); await load(); }
    catch (ex) { setErr(ex?.message || String(ex)); } finally { setBusy(""); setConfirmAction(null); }
  };
  const genReport = async () => {
    setBusy("report"); setErr("");
    try { const r = await bmv2GenerateBatchReport(batchId); await load(); if (r?.reference) setPreview(r.reference); }
    catch (ex) { setErr(ex?.message || String(ex)); } finally { setBusy(""); }
  };
  const addComment = async () => {
    if (!comment.trim()) return;
    try { await bmv2AddComment(batchId, comment.trim()); setComment(""); await load(); } catch (ex) { setErr(ex?.message || String(ex)); }
  };

  if (!batch) return <Card><div style={{ color: "var(--muted)" }}>{err ? <Banner tone="error">{err}</Banner> : "Loading…"}</div></Card>;

  const actions = BATCH_ACTIONS[batch.status] || [];
  const limitLines = []; // populated from excursions' limit values for context
  excursions.forEach((x) => { if (x.limit_value != null) limitLines.push({ value: Number(x.limit_value), label: `${x.tag_name} ${x.limit_type}`, color: "var(--danger)" }); });

  return (
    <>
      {err && <Banner tone="error">{err}</Banner>}
      <Card
        title={<span>{batch.reference || batch.id} <Pill value={batch.status} /></span>}
        actions={
          <>
            <button className="btn btn-ghost btn-sm" onClick={onBack}>← Back</button>
            {batch.batch_group_id && <button className="btn btn-secondary btn-sm" onClick={() => onOpenGroup(batch.batch_group_id)}>↑ Group</button>}
            {canEdit && actions.map(([a, label, cls]) => (
              <button key={a} className={`btn ${cls} btn-sm`} disabled={busy === a}
                onClick={() => (a === "abort" ? setConfirmAction(a) : doAction(a))}>{busy === a ? "…" : label}</button>
            ))}
            {canEdit && <button className="btn btn-secondary btn-sm" disabled={busy === "report"} onClick={genReport}>{busy === "report" ? "…" : "Generate report"}</button>}
          </>
        }
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, fontSize: 13 }}>
          <Field label="Quality"><Pill value={batch.quality_status} /></Field>
          <Field label="Data quality"><Pill value={batch.data_quality_status} /></Field>
          <Field label="Equipment">{batch.equipment_id || "—"}</Field>
          <Field label="Started">{fmtTs(batch.started_utc)}</Field>
          <Field label="Ended">{fmtTs(batch.ended_utc)}</Field>
          <Field label="Duration">{humanDur(batch.started_utc, batch.ended_utc)}</Field>
          <Field label="Product">{batch.product || "—"}</Field>
          <Field label="Start reason">{batch.start_reason || "—"}</Field>
          <Field label="Stop reason">{batch.stop_reason || "—"}</Field>
        </div>
      </Card>

      <Card title="KPIs">
        {kpis.length ? (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {kpis.filter((k) => k.numeric_value != null).slice(0, 16).map((k) => (
              <div key={k.id} className="mini-card" style={{ border: "1px solid var(--stroke)", borderRadius: 10, padding: "8px 12px", minWidth: 120 }}>
                <div style={{ fontSize: 11, color: "var(--muted)" }}>{k.label || k.kpi_code}</div>
                <div style={{ fontSize: 18, fontWeight: 700 }}>{fmtNum(k.numeric_value, k.unit)}</div>
                {k.quality_status !== "valid" && <div style={{ fontSize: 10, color: "var(--danger)" }}>{k.quality_status}</div>}
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No KPIs computed yet. {canEdit && <button className="btn btn-ghost btn-sm" onClick={async () => { await bmv2RecomputeBatch(batchId); load(); }}>Recompute</button>}</div>}
      </Card>

      {tags.length > 0 && <Card title="Process trends"><TrendChart series={series} xKey="ts" limitLines={limitLines} /></Card>}

      <Card title={`Excursions (${excursions.length})`}>
        {excursions.length ? (
          <div className="table" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 0.8fr 0.8fr 0.8fr 0.8fr", gap: 0 }}>
            <div className="thead"><span>Tag</span><span>Limit</span><span>Value</span><span>Min</span><span>Max</span><span>Severity</span></div>
            {excursions.map((x) => (
              <div className="trow" key={x.id}>
                <span className="db-cell">{x.tag_name}</span>
                <span className="db-cell">{x.limit_type}</span>
                <span className="db-cell">{fmtNum(x.limit_value)}</span>
                <span className="db-cell">{fmtNum(x.actual_minimum)}</span>
                <span className="db-cell">{fmtNum(x.actual_maximum)}</span>
                <span className="db-cell"><Pill value={x.severity === "error" || x.severity === "critical" ? "out_of_specification" : "with_warnings"} /></span>
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No excursions.</div>}
      </Card>

      <Card title="Reports">
        {reports.length ? (
          <div className="table" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1.4fr", gap: 0 }}>
            <div className="thead"><span>Kind</span><span>Status</span><span>Generated</span><span>Actions</span></div>
            {reports.map((r) => (
              <div className="trow" key={r.id}>
                <span className="db-cell">{(r.report_kind || "").replace(/_/g, " ")}</span>
                <span className="db-cell"><Pill value={r.report_status} /></span>
                <span className="db-cell">{fmtTs(r.generated_utc)}</span>
                <span className="db-cell" style={{ display: "flex", gap: 6 }}>
                  <button className="btn btn-secondary btn-sm" onClick={() => setPreview(r)}>Preview</button>
                </span>
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No reports generated.</div>}
      </Card>

      <Card title="Event timeline">
        {canEdit && (
          <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
            <input placeholder="Add a comment…" value={comment} onChange={(e) => setComment(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addComment()} />
            <button className="btn btn-secondary btn-sm" onClick={addComment}>Comment</button>
          </div>
        )}
        <div style={{ maxHeight: 260, overflow: "auto" }}>
          {events.map((ev) => (
            <div key={ev.id} style={{ display: "flex", gap: 10, fontSize: 12, padding: "4px 0", borderBottom: "1px solid var(--stroke)" }}>
              <span style={{ color: "var(--muted)", minWidth: 140 }}>{fmtTs(ev.event_utc)}</span>
              <span style={{ fontWeight: 600, minWidth: 130 }}>{ev.event_type}</span>
              <span>{ev.message || ""}</span>
            </div>
          ))}
          {!events.length && <div style={{ color: "var(--muted)", fontSize: 13 }}>No events.</div>}
        </div>
      </Card>

      {preview && <ReportPreviewModal report={preview} canEdit={canEdit} onClose={() => setPreview(null)} onRegenerate={genReport}
        onEmail={(refId, recips) => bmv2EmailBatchReport(batchId, refId, { recipients: recips })} />}
      {confirmAction && (
        <Modal onClose={() => setConfirmAction(null)} width={420}>
          <h3 style={{ marginTop: 0 }}>Confirm {confirmAction}</h3>
          <p style={{ color: "var(--muted)", fontSize: 14 }}>This will {confirmAction} the batch. This cannot be undone.</p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="btn btn-ghost btn-sm" onClick={() => setConfirmAction(null)}>Cancel</button>
            <button className="btn btn-danger btn-sm" onClick={() => doAction(confirmAction)}>{confirmAction}</button>
          </div>
        </Modal>
      )}
    </>
  );
}

function Field({ label, children }) {
  return <div><div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div><div>{children}</div></div>;
}

/* --------------------------------------------------------------------- */
/*  Batch Group Detail                                                   */
/* --------------------------------------------------------------------- */
function BatchGroupDetailV2({ groupId, canEdit, onBack, onOpenBatch }) {
  const [group, setGroup] = useState(null);
  const [children, setChildren] = useState([]);
  const [kpis, setKpis] = useState([]);
  const [reports, setReports] = useState([]);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(""); const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setErr("");
    try {
      const [g, ch, k, r] = await Promise.all([
        bmv2GetGroup(groupId), bmv2GroupBatches(groupId), bmv2GroupKpis(groupId), bmv2ListGroupReports(groupId),
      ]);
      setGroup(g); setChildren(ch); setKpis(k); setReports(r);
    } catch (ex) { setErr(ex?.message || String(ex)); }
  }, [groupId]);
  useEffect(() => { load(); }, [load]);

  const act = async (which) => {
    setBusy(which); setErr("");
    try { which === "complete" ? await bmv2CompleteGroup(groupId) : await bmv2AbortGroup(groupId); await load(); }
    catch (ex) { setErr(ex?.message || String(ex)); } finally { setBusy(""); }
  };
  const genReport = async () => {
    setBusy("report");
    try { const r = await bmv2GenerateGroupReport(groupId); await load(); if (r?.reference) setPreview(r.reference); }
    catch (ex) { setErr(ex?.message || String(ex)); } finally { setBusy(""); }
  };

  if (!group) return <Card><div style={{ color: "var(--muted)" }}>{err ? <Banner tone="error">{err}</Banner> : "Loading…"}</div></Card>;

  return (
    <>
      {err && <Banner tone="error">{err}</Banner>}
      <Card
        title={<span>{group.reference || group.id} <Pill value={group.status} /></span>}
        actions={
          <>
            <button className="btn btn-ghost btn-sm" onClick={onBack}>← Back</button>
            {canEdit && group.status === "active" && <button className="btn btn-primary btn-sm" disabled={busy === "complete"} onClick={() => act("complete")}>Complete</button>}
            {canEdit && group.status === "active" && <button className="btn btn-danger btn-sm" disabled={busy === "abort"} onClick={() => act("abort")}>Abort</button>}
            {canEdit && <button className="btn btn-secondary btn-sm" disabled={busy === "report"} onClick={genReport}>Generate report</button>}
          </>
        }
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, fontSize: 13 }}>
          <Field label="Children">{group.actual_child_count || 0}{group.expected_child_count ? ` / ${group.expected_child_count}` : ""}</Field>
          <Field label="Started">{fmtTs(group.started_utc)}</Field>
          <Field label="Completed">{fmtTs(group.completed_utc)}</Field>
        </div>
      </Card>

      <Card title="Group KPIs">
        {kpis.length ? (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {kpis.filter((k) => k.numeric_value != null).map((k) => (
              <div key={k.id} className="mini-card" style={{ border: "1px solid var(--stroke)", borderRadius: 10, padding: "8px 12px", minWidth: 120 }}>
                <div style={{ fontSize: 11, color: "var(--muted)" }}>{k.label || k.kpi_code}</div>
                <div style={{ fontSize: 18, fontWeight: 700 }}>{fmtNum(k.numeric_value, k.unit)}</div>
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No KPIs.</div>}
      </Card>

      <Card title={`Child batches (${children.length})`}>
        <BatchTable rows={children} onOpen={onOpenBatch} />
      </Card>

      <Card title="Reports">
        {reports.length ? (
          <div className="table" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 0 }}>
            <div className="thead"><span>Kind</span><span>Status</span><span>Generated</span><span>Actions</span></div>
            {reports.map((r) => (
              <div className="trow" key={r.id}>
                <span className="db-cell">{(r.report_kind || "").replace(/_/g, " ")}</span>
                <span className="db-cell"><Pill value={r.report_status} /></span>
                <span className="db-cell">{fmtTs(r.generated_utc)}</span>
                <span className="db-cell"><button className="btn btn-secondary btn-sm" onClick={() => setPreview(r)}>Preview</button></span>
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No reports.</div>}
      </Card>

      {preview && <ReportPreviewModal report={preview} canEdit={canEdit} onClose={() => setPreview(null)} onRegenerate={genReport}
        onEmail={(refId, recips) => bmv2EmailGroupReport(groupId, refId, { recipients: recips })} />}
    </>
  );
}

/* --------------------------------------------------------------------- */
/*  PAGE 2 — Batch Definitions (list + guided multi-step builder)        */
/* --------------------------------------------------------------------- */
const WIZ_STEPS = ["General", "Structure", "Identification", "Start", "Stop", "Tags & Limits", "KPIs", "Reports & Email", "Publish"];
const KPI_CHOICES = [
  ["cycle_time", "Cycle time"], ["running_time", "Running time"], ["hold_time", "Hold time"],
  ["avg", "Average (per tag)"], ["min", "Min (per tag)"], ["max", "Max (per tag)"],
  ["excursion_count", "Excursion count"], ["total", "Total (per tag)"], ["count", "Sample count"],
];
const LIMIT_TYPES = ["spec_lower", "spec_upper", "warning_lower", "warning_upper", "operating_lower", "operating_upper"];
const TAG_CATEGORIES = ["process_value", "setpoint", "count", "energy", "flow", "pressure", "temperature", "electrical", "machine_state", "alarm", "status", "test_result"];

export function BatchDefinitionsV2Page({ canEdit = false, gatewayConfigs = [] }) {
  const lic = useLicense();
  const [defs, setDefs] = useState([]);
  const [editing, setEditing] = useState(null);   // definition id or "new"
  const [err, setErr] = useState("");
  const gateways = useMemo(() => bmv2NormalizeGatewayTags(gatewayConfigs), [gatewayConfigs]);

  const load = useCallback(async () => {
    try { setDefs(await bmv2ListDefinitions()); } catch (e) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { if (lic) load(); }, [lic, load]);

  if (lic === false) return <Unlicensed />;
  if (lic === null) return <Card><div style={{ color: "var(--muted)" }}>Loading…</div></Card>;

  return (
    <>
      {err && <Banner tone="error">{err}</Banner>}
      <Card title="Batch Definitions" actions={canEdit && <button className="btn btn-primary btn-sm" onClick={() => setEditing("new")}>+ New definition</button>}>
        {defs.length ? (
          <div className="table" style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 0.8fr 0.8fr 1fr", gap: 0 }}>
            <div className="thead"><span>Name</span><span>Status</span><span>Version</span><span>Equipment</span><span>Actions</span></div>
            {defs.map((d) => (
              <div className="trow" key={d.id}>
                <span className="db-cell">{d.name}</span>
                <span className="db-cell"><Pill value={d.status} /></span>
                <span className="db-cell">v{d.cur_version_number || d.version_number || 1}</span>
                <span className="db-cell">{d.equipment_id || "—"}</span>
                <span className="db-cell" style={{ display: "flex", gap: 6 }}>
                  <button className="btn btn-secondary btn-sm" onClick={() => setEditing(d.id)}>{canEdit ? "Edit" : "View"}</button>
                </span>
              </div>
            ))}
          </div>
        ) : <div style={{ color: "var(--muted)", fontSize: 13 }}>No definitions yet. {canEdit && "Create one to start monitoring batches."}</div>}
      </Card>
      {editing && <DefinitionWizard definitionId={editing === "new" ? null : editing} gateways={gateways} canEdit={canEdit}
        onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </>
  );
}

function DefinitionWizard({ definitionId, gateways, canEdit, onClose, onSaved }) {
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(!!definitionId);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [validation, setValidation] = useState(null);
  const [published, setPublished] = useState(false);
  const [form, setForm] = useState({
    name: "", code: "", description: "", plant: "", area: "", equipment_id: "", product: "", owner: "",
    config: {
      batch_mode: "individual", group_config: { expected_child_count: null, naming: "" },
      identification: { method: "auto", prefix: "", suffix: "" },
      start_config: { method: "manual" }, stop_config: { method: "manual" },
      report_config: {}, batch_report_template_id: "tpl-batch-detailed", batch_group_report_template_id: "tpl-batch-group-summary",
      auto_generate_batch_report: false, auto_email_batch_report: false, email_config: { recipients: "", subject: "", body: "" },
      tags: [], triggers: [], kpis: [],
    },
  });

  useEffect(() => {
    if (!definitionId) return;
    let m = true;
    bmv2GetDefinition(definitionId).then((d) => {
      if (!m || !d) return;
      setPublished(d.status === "published");
      setForm({
        name: d.name || "", code: d.code || "", description: d.description || "", plant: d.plant || "",
        area: d.area || "", equipment_id: d.equipment_id || "", product: d.product || "", owner: d.owner || "",
        config: {
          batch_mode: d.config?.batch_mode || "individual",
          group_config: d.config?.group_config || { expected_child_count: null, naming: "" },
          identification: d.config?.identification || { method: "auto", prefix: "", suffix: "" },
          start_config: d.config?.start_config || { method: "manual" },
          stop_config: d.config?.stop_config || { method: "manual" },
          report_config: d.config?.report_config || {},
          batch_report_template_id: d.config?.batch_report_template_id || "tpl-batch-detailed",
          batch_group_report_template_id: d.config?.batch_group_report_template_id || "tpl-batch-group-summary",
          auto_generate_batch_report: !!d.config?.auto_generate_batch_report,
          auto_email_batch_report: !!d.config?.auto_email_batch_report,
          email_config: d.config?.email_config || { recipients: "", subject: "", body: "" },
          tags: (d.config?.tags || []).map((t) => ({ ...t, limits: t.limits || [] })),
          triggers: d.config?.triggers || [], kpis: d.config?.kpis || [],
        },
      });
      setLoading(false);
    }).catch((e) => { if (m) { setErr(e?.message || String(e)); setLoading(false); } });
    return () => { m = false; };
  }, [definitionId]);

  const setCfg = (patch) => setForm((f) => ({ ...f, config: { ...f.config, ...patch } }));
  const readOnly = published || !canEdit;

  const save = async () => {
    setBusy(true); setErr("");
    try {
      // normalize email recipients to a list
      const payload = { ...form, config: { ...form.config,
        email_config: { ...form.config.email_config,
          recipients: String(form.config.email_config.recipients || "").split(/[,;\s]+/).filter(Boolean) } } };
      const saved = await bmv2SaveDefinition(payload, definitionId || null);
      onSaved(saved);
    } catch (e) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  const doValidate = async () => {
    if (!definitionId) { setErr("Save the draft first, then validate."); return; }
    try { setValidation(await bmv2ValidateDefinition(definitionId)); } catch (e) { setErr(e?.message || String(e)); }
  };
  const doPublish = async () => {
    if (!definitionId) { setErr("Save the draft first."); return; }
    setBusy(true); setErr("");
    try { await bmv2PublishDefinition(definitionId); onSaved(); } catch (e) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  const newVersion = async () => {
    setBusy(true);
    try { await bmv2NewVersion(definitionId); setPublished(false); const d = await bmv2GetDefinition(definitionId); setForm((f) => ({ ...f })); }
    catch (e) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };

  if (loading) return <Modal onClose={onClose} width={900}><div style={{ color: "var(--muted)" }}>Loading…</div></Modal>;

  return (
    <Modal onClose={onClose} width={980}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>{definitionId ? (readOnly ? "View" : "Edit") : "New"} batch definition</h3>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
      </div>
      {published && <Banner>This version is <b>published</b> and immutable. {canEdit && <button className="btn btn-ghost btn-sm" onClick={newVersion}>Create new draft version</button>}</Banner>}
      {err && <Banner tone="error">{err}</Banner>}

      {/* step indicator */}
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 14 }}>
        {WIZ_STEPS.map((s, i) => (
          <button key={s} className={`btn btn-sm ${i === step ? "btn-primary" : "btn-ghost"}`} onClick={() => setStep(i)} style={{ fontSize: 11 }}>
            {i + 1}. {s}
          </button>
        ))}
      </div>

      <div style={{ minHeight: 260 }}>
        {step === 0 && <StepGeneral form={form} setForm={setForm} readOnly={readOnly} />}
        {step === 1 && <StepStructure cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 2 && <StepIdentification cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 3 && <StepCondition which="start_config" title="Start" cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 4 && <StepCondition which="stop_config" title="Stop" cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 5 && <StepTags cfg={form.config} setCfg={setCfg} gateways={gateways} readOnly={readOnly} />}
        {step === 6 && <StepKpis cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 7 && <StepReports cfg={form.config} setCfg={setCfg} readOnly={readOnly} />}
        {step === 8 && <StepPublish validation={validation} onValidate={doValidate} onPublish={doPublish} canEdit={canEdit} published={published} busy={busy} />}
      </div>

      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", marginTop: 14, borderTop: "1px solid var(--stroke)", paddingTop: 12 }}>
        <div>
          <button className="btn btn-ghost btn-sm" disabled={step === 0} onClick={() => setStep((s) => s - 1)}>← Prev</button>
          <button className="btn btn-ghost btn-sm" disabled={step === WIZ_STEPS.length - 1} onClick={() => setStep((s) => s + 1)}>Next →</button>
        </div>
        {!readOnly && <button className="btn btn-primary btn-sm" disabled={busy || !form.name.trim()} onClick={save}>{busy ? "Saving…" : (definitionId ? "Save draft" : "Create draft")}</button>}
      </div>
    </Modal>
  );
}

function Lbl({ label, children }) { return <label>{label}{children}</label>; }

function StepGeneral({ form, setForm, readOnly }) {
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
      <Lbl label="Name *"><input disabled={readOnly} value={form.name} onChange={(e) => set("name", e.target.value)} /></Lbl>
      <Lbl label="Code"><input disabled={readOnly} value={form.code} onChange={(e) => set("code", e.target.value)} /></Lbl>
      <Lbl label="Plant"><input disabled={readOnly} value={form.plant} onChange={(e) => set("plant", e.target.value)} /></Lbl>
      <Lbl label="Area"><input disabled={readOnly} value={form.area} onChange={(e) => set("area", e.target.value)} /></Lbl>
      <Lbl label="Equipment (gateway id)"><input disabled={readOnly} value={form.equipment_id} onChange={(e) => set("equipment_id", e.target.value)} /></Lbl>
      <Lbl label="Product / process"><input disabled={readOnly} value={form.product} onChange={(e) => set("product", e.target.value)} /></Lbl>
      <Lbl label="Owner"><input disabled={readOnly} value={form.owner} onChange={(e) => set("owner", e.target.value)} /></Lbl>
      <Lbl label="Description"><input disabled={readOnly} value={form.description} onChange={(e) => set("description", e.target.value)} /></Lbl>
    </div>
  );
}

function StepStructure({ cfg, setCfg, readOnly }) {
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
      <Lbl label="Batch mode">
        <select disabled={readOnly} value={cfg.batch_mode} onChange={(e) => setCfg({ batch_mode: e.target.value })}>
          <option value="individual">Individual batch</option>
          <option value="group">Batch group (with children)</option>
          <option value="both">Both</option>
        </select>
      </Lbl>
      {cfg.batch_mode !== "individual" && (
        <>
          <Lbl label="Expected child count">
            <input type="number" disabled={readOnly} value={cfg.group_config?.expected_child_count ?? ""}
              onChange={(e) => setCfg({ group_config: { ...cfg.group_config, expected_child_count: e.target.value ? Number(e.target.value) : null } })} />
          </Lbl>
          <Lbl label="Child naming pattern">
            <input disabled={readOnly} value={cfg.group_config?.naming || ""} placeholder="{GroupRef}-B{Seq}"
              onChange={(e) => setCfg({ group_config: { ...cfg.group_config, naming: e.target.value } })} />
          </Lbl>
        </>
      )}
    </div>
  );
}

function StepIdentification({ cfg, setCfg, readOnly }) {
  const id = cfg.identification || {};
  const set = (k, v) => setCfg({ identification: { ...id, [k]: v } });
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
      <Lbl label="Reference method">
        <select disabled={readOnly} value={id.method || "auto"} onChange={(e) => set("method", e.target.value)}>
          {["auto", "manual", "barcode", "plc_tag", "combined"].map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </Lbl>
      <Lbl label="Prefix"><input disabled={readOnly} value={id.prefix || ""} onChange={(e) => set("prefix", e.target.value)} /></Lbl>
      <Lbl label="Suffix"><input disabled={readOnly} value={id.suffix || ""} onChange={(e) => set("suffix", e.target.value)} /></Lbl>
      <Lbl label="Validation pattern (regex, optional)"><input disabled={readOnly} value={id.pattern || ""} onChange={(e) => set("pattern", e.target.value)} /></Lbl>
    </div>
  );
}

function StepCondition({ which, title, cfg, setCfg, readOnly }) {
  const c = cfg[which] || { method: "manual" };
  const set = (patch) => setCfg({ [which]: { ...c, ...patch } });
  const scope = which === "start_config" ? "BATCH_START" : "BATCH_STOP";
  const trigger = (cfg.triggers || []).find((t) => t.trigger_scope === scope) || { trigger_scope: scope, condition: { operator: "AND", rules: [] }, enabled: true };
  const setTrigger = (next) => {
    const others = (cfg.triggers || []).filter((t) => t.trigger_scope !== scope);
    setCfg({ triggers: [...others, next] });
  };
  const rules = trigger.condition?.rules || [];
  const setRule = (i, patch) => {
    const nr = rules.map((r, j) => (j === i ? { ...r, ...patch } : r));
    setTrigger({ ...trigger, condition: { ...trigger.condition, rules: nr } });
  };
  return (
    <div>
      <div className="form-grid" style={{ gridTemplateColumns: "1fr" }}>
        <Lbl label={`${title} method`}>
          <select disabled={readOnly} value={c.method || "manual"} onChange={(e) => set({ method: e.target.value })}>
            <option value="manual">Manual</option>
            <option value="gateway_trigger">Existing gateway trigger (tag condition)</option>
            {which === "stop_config" && <option value="duration">Elapsed time</option>}
          </select>
        </Lbl>
      </div>
      {c.method === "gateway_trigger" && (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 12, color: "var(--muted)" }}>Combine rules with</span>
            <select disabled={readOnly} value={trigger.condition?.operator || "AND"}
              onChange={(e) => setTrigger({ ...trigger, condition: { ...trigger.condition, operator: e.target.value } })} style={{ maxWidth: 100 }}>
              <option>AND</option><option>OR</option>
            </select>
            {!readOnly && <button className="btn btn-ghost btn-sm" onClick={() => setTrigger({ ...trigger, condition: { ...trigger.condition, rules: [...rules, { tag: "", kind: "rising_edge" }] } })}>+ Rule</button>}
          </div>
          {rules.map((r, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 0.8fr 0.8fr auto", gap: 6, marginBottom: 6 }}>
              <input disabled={readOnly} placeholder="tag name" value={r.tag || ""} onChange={(e) => setRule(i, { tag: e.target.value })} />
              <select disabled={readOnly} value={r.kind || "rising_edge"} onChange={(e) => setRule(i, { kind: e.target.value })}>
                {["rising_edge", "falling_edge", "threshold", "equals"].map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
              {r.kind === "threshold" ? (
                <><select disabled={readOnly} value={r.op || ">"} onChange={(e) => setRule(i, { op: e.target.value })}>{[">", "<", ">=", "<="].map((o) => <option key={o}>{o}</option>)}</select>
                  <input disabled={readOnly} placeholder="value" value={r.value ?? ""} onChange={(e) => setRule(i, { value: e.target.value })} /></>
              ) : r.kind === "equals" ? (<><span /><input disabled={readOnly} placeholder="value" value={r.value ?? ""} onChange={(e) => setRule(i, { value: e.target.value })} /></>) : (<><span /><span /></>)}
              {!readOnly && <button className="btn btn-danger btn-sm" onClick={() => setTrigger({ ...trigger, condition: { ...trigger.condition, rules: rules.filter((_, j) => j !== i) } })}>×</button>}
            </div>
          ))}
          <div style={{ fontSize: 11, color: "var(--muted)" }}>These reference the existing gateway tags; the batch daemon evaluates them (it does not re-run PLC logic).</div>
        </div>
      )}
      {which === "stop_config" && c.method === "duration" && (
        <div className="form-grid" style={{ gridTemplateColumns: "1fr", marginTop: 10 }}>
          <Lbl label="Duration (seconds)"><input type="number" disabled={readOnly} value={c.seconds ?? ""} onChange={(e) => set({ seconds: Number(e.target.value) })} /></Lbl>
        </div>
      )}
    </div>
  );
}

function StepTags({ cfg, setCfg, gateways, readOnly }) {
  const tags = cfg.tags || [];
  const addTag = () => setCfg({ tags: [...tags, { tag_name: "", gateway_id: "", tag_category: "process_value", required: false, trend_enabled: true, report_enabled: true, limits: [] }] });
  const setTag = (i, patch) => setCfg({ tags: tags.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const allTagNames = gateways.flatMap((g) => g.tags.map((t) => t.name));
  return (
    <div>
      {!readOnly && <button className="btn btn-ghost btn-sm" onClick={addTag} style={{ marginBottom: 10 }}>+ Add tag</button>}
      {!tags.length && <div style={{ color: "var(--muted)", fontSize: 13 }}>No tags selected. Add the historian tags to monitor for this batch.</div>}
      {tags.map((t, i) => (
        <div key={i} style={{ border: "1px solid var(--stroke)", borderRadius: 8, padding: 10, marginBottom: 8 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr auto auto auto", gap: 6, alignItems: "center" }}>
            <input disabled={readOnly} placeholder="tag name" list="bmv2-tags" value={t.tag_name} onChange={(e) => setTag(i, { tag_name: e.target.value })} />
            <input disabled={readOnly} placeholder="gateway id" value={t.gateway_id || ""} onChange={(e) => setTag(i, { gateway_id: e.target.value })} />
            <select disabled={readOnly} value={t.tag_category || "process_value"} onChange={(e) => setTag(i, { tag_category: e.target.value })}>
              {TAG_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <label style={{ display: "flex", gap: 4, fontSize: 11, alignItems: "center" }}><input type="checkbox" disabled={readOnly} checked={!!t.required} onChange={(e) => setTag(i, { required: e.target.checked })} />req</label>
            <label style={{ display: "flex", gap: 4, fontSize: 11, alignItems: "center" }}><input type="checkbox" disabled={readOnly} checked={t.trend_enabled !== false} onChange={(e) => setTag(i, { trend_enabled: e.target.checked })} />trend</label>
            {!readOnly && <button className="btn btn-danger btn-sm" onClick={() => setCfg({ tags: tags.filter((_, j) => j !== i) })}>×</button>}
          </div>
          <TagLimits tag={t} onChange={(limits) => setTag(i, { limits })} readOnly={readOnly} />
        </div>
      ))}
      <datalist id="bmv2-tags">{allTagNames.map((n) => <option key={n} value={n} />)}</datalist>
    </div>
  );
}

function TagLimits({ tag, onChange, readOnly }) {
  const limits = tag.limits || [];
  const add = () => onChange([...limits, { limit_type: "spec_upper", limit_value: "", severity: "error", enabled: true }]);
  const set = (i, patch) => onChange(limits.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  return (
    <div style={{ marginTop: 8, paddingLeft: 8 }}>
      {limits.map((l, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 0.8fr 1fr auto", gap: 6, marginBottom: 4 }}>
          <select disabled={readOnly} value={l.limit_type} onChange={(e) => set(i, { limit_type: e.target.value })}>{LIMIT_TYPES.map((x) => <option key={x} value={x}>{x}</option>)}</select>
          <input disabled={readOnly} placeholder="value" value={l.limit_value ?? ""} onChange={(e) => set(i, { limit_value: e.target.value })} />
          <select disabled={readOnly} value={l.severity || "warning"} onChange={(e) => set(i, { severity: e.target.value })}>{["info", "warning", "error", "critical"].map((s) => <option key={s}>{s}</option>)}</select>
          {!readOnly && <button className="btn btn-danger btn-sm" onClick={() => onChange(limits.filter((_, j) => j !== i))}>×</button>}
        </div>
      ))}
      {!readOnly && <button className="btn btn-ghost btn-sm" onClick={add} style={{ fontSize: 11 }}>+ limit</button>}
    </div>
  );
}

function StepKpis({ cfg, setCfg, readOnly }) {
  const selected = new Set((cfg.kpis || []).map((k) => k.code));
  const toggle = (code, name) => {
    if (readOnly) return;
    const next = selected.has(code) ? (cfg.kpis || []).filter((k) => k.code !== code) : [...(cfg.kpis || []), { code, name, scope: "batch", enabled: true }];
    setCfg({ kpis: next });
  };
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 10 }}>Select the KPIs to compute per batch. (Batch-level KPIs like cycle/hold time are always computed; these add per-tag stats.)</div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {KPI_CHOICES.map(([code, name]) => (
          <button key={code} className={`btn btn-sm ${selected.has(code) ? "btn-primary" : "btn-ghost"}`} onClick={() => toggle(code, name)}>{name}</button>
        ))}
      </div>
    </div>
  );
}

function StepReports({ cfg, setCfg, readOnly }) {
  const ec = cfg.email_config || {};
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
      <Lbl label="Batch report template">
        <select disabled={readOnly} value={cfg.batch_report_template_id || ""} onChange={(e) => setCfg({ batch_report_template_id: e.target.value })}>
          <option value="tpl-batch-summary">Batch Summary</option>
          <option value="tpl-batch-detailed">Batch Detailed</option>
        </select>
      </Lbl>
      <Lbl label="Group report template">
        <select disabled={readOnly} value={cfg.batch_group_report_template_id || ""} onChange={(e) => setCfg({ batch_group_report_template_id: e.target.value })}>
          <option value="tpl-batch-group-summary">Group Summary</option>
          <option value="tpl-batch-group-detailed">Group Detailed</option>
        </select>
      </Lbl>
      <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
        <input type="checkbox" disabled={readOnly} checked={!!cfg.auto_generate_batch_report} onChange={(e) => setCfg({ auto_generate_batch_report: e.target.checked })} />
        Auto-generate a report when a batch completes
      </label>
      <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
        <input type="checkbox" disabled={readOnly} checked={!!cfg.auto_email_batch_report} onChange={(e) => setCfg({ auto_email_batch_report: e.target.checked })} />
        Auto-email the report (uses the existing email settings)
      </label>
      <Lbl label="Email recipients (comma-separated)"><input disabled={readOnly} value={ec.recipients || ""} onChange={(e) => setCfg({ email_config: { ...ec, recipients: e.target.value } })} /></Lbl>
      <Lbl label="Email subject"><input disabled={readOnly} value={ec.subject || ""} onChange={(e) => setCfg({ email_config: { ...ec, subject: e.target.value } })} /></Lbl>
    </div>
  );
}

function StepPublish({ validation, onValidate, onPublish, canEdit, published, busy }) {
  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <button className="btn btn-secondary btn-sm" onClick={onValidate}>Validate</button>
        {canEdit && !published && <button className="btn btn-primary btn-sm" disabled={busy || (validation && !validation.ok)} onClick={onPublish}>{busy ? "Publishing…" : "Publish"}</button>}
      </div>
      {validation && (
        <div>
          <Banner tone={validation.ok ? undefined : "error"}>{validation.ok ? "Validation passed — ready to publish." : "Validation failed — fix the errors below."}</Banner>
          {(validation.errors || []).map((e, i) => <div key={i} style={{ color: "var(--danger)", fontSize: 12 }}>• {e}</div>)}
          {(validation.warnings || []).map((w, i) => <div key={i} style={{ color: "var(--muted)", fontSize: 12 }}>⚠ {w}</div>)}
        </div>
      )}
      {published && <Banner>Published versions are immutable. Create a new draft version to make changes.</Banner>}
      <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 10 }}>Publishing freezes this version; every batch started from it keeps a reference to the exact version used.</div>
    </div>
  );
}

/* --------------------------------------------------------------------- */
/*  PAGE 3 — Batch Analysis (tabs)                                        */
/* --------------------------------------------------------------------- */
export function BatchAnalysisV2Page({ canEdit = false }) {
  const lic = useLicense();
  const [tab, setTab] = useState("reports");
  if (lic === false) return <Unlicensed />;
  if (lic === null) return <Card><div style={{ color: "var(--muted)" }}>Loading…</div></Card>;
  return (
    <>
      <Card title="Batch Reports & Analysis" actions={
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {[["reports", "Reports"], ["comparison", "Batch Comparison"], ["excursions", "Excursions"]].map(([k, l]) => (
            <button key={k} className={`btn btn-sm ${tab === k ? "btn-primary" : "btn-ghost"}`} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
      }>
        {tab === "reports" && <AnalysisReports canEdit={canEdit} />}
        {tab === "comparison" && <AnalysisComparison />}
        {tab === "excursions" && <AnalysisExcursions canEdit={canEdit} />}
      </Card>
    </>
  );
}

function AnalysisReports({ canEdit }) {
  // aggregate report references across recent batches + groups
  const [rows, setRows] = useState([]);
  const [preview, setPreview] = useState(null);
  const [previewOwner, setPreviewOwner] = useState(null); // {kind, id}
  const [err, setErr] = useState("");
  useEffect(() => {
    (async () => {
      try {
        const [bl, gl] = await Promise.all([bmv2ListBatches({ limit: 50 }), bmv2ListGroups({ limit: 30 })]);
        const acc = [];
        for (const b of (bl.rows || [])) {
          const rs = await bmv2ListBatchReports(b.id);
          rs.forEach((r) => acc.push({ ...r, owner_ref: b.reference || b.id, owner_kind: "batch", owner_id: b.id }));
        }
        for (const g of (gl.rows || [])) {
          const rs = await bmv2ListGroupReports(g.id);
          rs.forEach((r) => acc.push({ ...r, owner_ref: g.reference || g.id, owner_kind: "group", owner_id: g.id }));
        }
        acc.sort((a, b) => String(b.created_utc).localeCompare(String(a.created_utc)));
        setRows(acc);
      } catch (e) { setErr(e?.message || String(e)); }
    })();
  }, []);
  if (err) return <Banner tone="error">{err}</Banner>;
  if (!rows.length) return <div style={{ color: "var(--muted)", fontSize: 13 }}>No reports generated yet.</div>;
  return (
    <>
      <div className="table" style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr 1fr 1fr 1fr 0.8fr", gap: 0 }}>
        <div className="thead"><span>Reference</span><span>Kind</span><span>Status</span><span>Email</span><span>Generated</span><span>Actions</span></div>
        {rows.map((r) => (
          <div className="trow" key={r.id}>
            <span className="db-cell">{r.owner_ref}</span>
            <span className="db-cell">{(r.report_kind || "").replace(/_/g, " ")}</span>
            <span className="db-cell"><Pill value={r.report_status} /></span>
            <span className="db-cell"><Pill value={r.email_status === "sent" ? "good" : r.email_status === "failed" ? "failed" : "planned"} /></span>
            <span className="db-cell">{fmtTs(r.generated_utc)}</span>
            <span className="db-cell"><button className="btn btn-secondary btn-sm" onClick={() => { setPreview(r); setPreviewOwner({ kind: r.owner_kind, id: r.owner_id }); }}>Preview</button></span>
          </div>
        ))}
      </div>
      {preview && <ReportPreviewModal report={preview} canEdit={canEdit} onClose={() => setPreview(null)}
        onRegenerate={async () => { previewOwner.kind === "batch" ? await bmv2GenerateBatchReport(previewOwner.id) : await bmv2GenerateGroupReport(previewOwner.id); }}
        onEmail={(refId, recips) => previewOwner.kind === "batch" ? bmv2EmailBatchReport(previewOwner.id, refId, { recipients: recips }) : bmv2EmailGroupReport(previewOwner.id, refId, { recipients: recips })} />}
    </>
  );
}

function AnalysisComparison() {
  const [batches, setBatches] = useState([]);
  const [picked, setPicked] = useState([]);
  const [tags, setTags] = useState("");
  const [data, setData] = useState([]);
  const [err, setErr] = useState("");
  useEffect(() => { bmv2ListBatches({ limit: 50, status: "completed" }).then((r) => setBatches(r.rows || [])).catch((e) => setErr(e?.message || String(e))); }, []);
  const run = async () => {
    try { setData(await bmv2AnalysisComparison(picked, tags.split(/[,;\s]+/).filter(Boolean))); }
    catch (e) { setErr(e?.message || String(e)); }
  };
  const toggle = (id) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length < 6 ? [...p, id] : p));
  return (
    <div>
      {err && <Banner tone="error">{err}</Banner>}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <div style={{ flex: "1 1 260px" }}>
          <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 4 }}>Pick up to 6 completed batches</div>
          <div style={{ maxHeight: 120, overflow: "auto", border: "1px solid var(--stroke)", borderRadius: 8, padding: 6 }}>
            {batches.map((b) => (
              <label key={b.id} style={{ display: "flex", gap: 6, fontSize: 12, padding: "2px 0" }}>
                <input type="checkbox" checked={picked.includes(b.id)} onChange={() => toggle(b.id)} /> {b.reference || b.id}
              </label>
            ))}
          </div>
        </div>
        <div style={{ flex: "1 1 200px" }}>
          <Lbl label="Tags (comma-separated, blank = all)"><input value={tags} onChange={(e) => setTags(e.target.value)} /></Lbl>
          <button className="btn btn-primary btn-sm" onClick={run} disabled={!picked.length} style={{ marginTop: 8 }}>Compare</button>
        </div>
      </div>
      {data.length > 0 && data.map((d) => (
        <Card key={d.batch_id} title={d.reference || d.batch_id}><TrendChart series={d.series} xKey="elapsed_s" height={220} /></Card>
      ))}
    </div>
  );
}

function AnalysisExcursions({ canEdit }) {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const load = useCallback(() => { bmv2AnalysisExcursions(500).then(setRows).catch((e) => setErr(e?.message || String(e))); }, []);
  useEffect(() => { load(); }, [load]);
  const ack = async (id) => { try { await bmv2AckExcursion(id, { acknowledged: true }); load(); } catch (e) { setErr(e?.message || String(e)); } };
  if (err) return <Banner tone="error">{err}</Banner>;
  if (!rows.length) return <div style={{ color: "var(--muted)", fontSize: 13 }}>No excursions recorded.</div>;
  return (
    <div className="table" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 0.8fr 0.8fr 1fr 0.8fr 0.8fr", gap: 0 }}>
      <div className="thead"><span>Tag</span><span>Limit</span><span>Value</span><span>Actual</span><span>Started</span><span>Severity</span><span>Ack</span></div>
      {rows.map((x) => (
        <div className="trow" key={x.id}>
          <span className="db-cell">{x.tag_name}</span>
          <span className="db-cell">{x.limit_type}</span>
          <span className="db-cell">{fmtNum(x.limit_value)}</span>
          <span className="db-cell">{fmtNum(x.actual_minimum)}…{fmtNum(x.actual_maximum)}</span>
          <span className="db-cell">{fmtTs(x.started_utc)}</span>
          <span className="db-cell">{x.severity}</span>
          <span className="db-cell">{x.acknowledged ? "✓" : (canEdit ? <button className="btn btn-ghost btn-sm" onClick={() => ack(x.id)}>Ack</button> : "—")}</span>
        </div>
      ))}
    </div>
  );
}

export { Modal, Pill, fmtTs, humanDur, fmtNum, Card, SummaryCard, Banner, ReportPreviewModal, TrendChart };
