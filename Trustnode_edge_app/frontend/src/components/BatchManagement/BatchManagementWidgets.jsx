/* Dashboard widgets for the Batch Management module.

   Each widget is self-contained: it polls the batch-management API
   directly and renders inside a normal dashboard cell. They are
   resilient — if the license is removed mid-session the API returns
   404 and the widget renders a tiny "(unlicensed)" placeholder
   instead of crashing the dashboard.
*/
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  listBatches,
  stopBatch,
  listBatchSummaries,
  bmv2ScanBatch,
} from "../../api";
import BarcodeScanInput from "./BarcodeScanInput";


function _fmt(ts) {
  if (!ts) return "";
  try {
    const d = new Date(String(ts).includes("T") ? ts : ts.replace(" ", "T") + "Z");
    if (!isFinite(d.getTime())) return ts;
    return d.toLocaleString();
  } catch { return ts; }
}

function _durationMs(start, end) {
  if (!start) return 0;
  const s = new Date(String(start).includes("T") ? start : start.replace(" ", "T") + "Z");
  const e = end
    ? new Date(String(end).includes("T") ? end : end.replace(" ", "T") + "Z")
    : new Date();
  return Math.max(0, e.getTime() - s.getTime());
}

function _humanDuration(ms) {
  if (!ms) return "—";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m ${sec}s`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
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

function useBatchPolling(fetcher, deps = [], intervalMs = 5000) {
  const [data, setData] = useState(null);
  const [unlicensed, setUnlicensed] = useState(false);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const v = await fetcher();
        if (alive) { setData(v); setUnlicensed(false); }
      } catch (e) {
        const msg = String(e?.message || e || "");
        if (alive && msg.includes("404")) setUnlicensed(true);
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, unlicensed };
}


// ------------------------- Current Batch ------------------------------
export function BatchCurrentWidget({ widget }) {
  const cfg = widget?.config || {};
  const { data, unlicensed } = useBatchPolling(
    () => listBatches({ status: "running", limit: 1 }),
    [],
    3000,
  );
  if (unlicensed) return <div className="widget-empty">Batch module not licensed.</div>;
  const batch = (data?.rows || [])[0];
  if (!batch) {
    return (
      <div className="widget-pad">
        <div className="widget-title">{cfg.title || "Current Batch"}</div>
        <div className="muted">No batch running.</div>
      </div>
    );
  }
  return (
    <div className="widget-pad">
      <div className="widget-title">{cfg.title || "Current Batch"}</div>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 4, alignItems: "baseline" }}>
        <strong>ID:</strong><span>{batch.identifier || batch.id}</span>
        <strong>Status:</strong><span><StatusPill status={batch.status} /></span>
        <strong>Product:</strong><span>{batch.product || "—"}</span>
        <strong>Operator:</strong><span>{batch.operator || "—"}</span>
        <strong>Started:</strong><span>{_fmt(batch.started_utc)}</span>
        <strong>Duration:</strong><span>{_humanDuration(_durationMs(batch.started_utc, batch.ended_utc))}</span>
      </div>
    </div>
  );
}


// ------------------------- Batch List ---------------------------------
export function BatchListWidget({ widget }) {
  const cfg = widget?.config || {};
  const limit = Math.max(1, Math.min(Number(cfg.limit || 10), 50));
  const { data, unlicensed } = useBatchPolling(
    () => listBatches({ limit }),
    [limit],
    8000,
  );
  if (unlicensed) return <div className="widget-empty">Batch module not licensed.</div>;
  const rows = data?.rows || [];
  return (
    <div className="widget-pad" style={{ overflowY: "auto" }}>
      <div className="widget-title">{cfg.title || "Recent Batches"}</div>
      <div className="table" style={{ fontSize: 12 }}>
        <div className="thead">
          <span>ID</span><span>Status</span><span>Started</span><span>Duration</span><span>Operator</span>
        </div>
        {rows.map((b) => (
          <div key={b.id} className="trow">
            <span>{b.identifier || b.id}</span>
            <span><StatusPill status={b.status} /></span>
            <span>{_fmt(b.started_utc)}</span>
            <span>{_humanDuration(_durationMs(b.started_utc, b.ended_utc))}</span>
            <span>{b.operator || "—"}</span>
          </div>
        ))}
        {rows.length === 0 ? <div className="trow"><span>No batches.</span></div> : null}
      </div>
    </div>
  );
}


// ------------------------- Batch KPI ----------------------------------
export function BatchKpiWidget({ widget }) {
  const cfg = widget?.config || {};
  const { data, unlicensed } = useBatchPolling(
    () => listBatches({ limit: 200 }),
    [],
    15000,
  );
  if (unlicensed) return <div className="widget-empty">Batch module not licensed.</div>;
  const rows = data?.rows || [];
  const total = rows.length;
  const completed = rows.filter((b) => b.status === "completed" || b.status === "validated").length;
  const failed = rows.filter((b) => b.status === "failed").length;
  const running = rows.filter((b) => b.status === "running").length;
  const passRate = total ? Math.round((completed / total) * 100) : 0;
  const durations = rows
    .filter((b) => b.started_utc && b.ended_utc)
    .map((b) => _durationMs(b.started_utc, b.ended_utc));
  const avgDur = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : 0;
  return (
    <div className="widget-pad">
      <div className="widget-title">{cfg.title || "Batch KPI"}</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 8 }}>
        <div><strong>Total:</strong> {total}</div>
        <div><strong>Running:</strong> {running}</div>
        <div><strong>Completed:</strong> {completed}</div>
        <div><strong>Failed:</strong> {failed}</div>
        <div><strong>Pass rate:</strong> {passRate}%</div>
        <div><strong>Avg cycle:</strong> {_humanDuration(avgDur)}</div>
      </div>
    </div>
  );
}


// ------------------------- Batch Timeline -----------------------------
export function BatchTimelineWidget({ widget }) {
  const cfg = widget?.config || {};
  const limit = Math.max(5, Math.min(Number(cfg.limit || 20), 100));
  const { data, unlicensed } = useBatchPolling(
    () => listBatches({ limit }),
    [limit],
    10000,
  );
  if (unlicensed) return <div className="widget-empty">Batch module not licensed.</div>;
  const rows = (data?.rows || []).slice().reverse();
  if (!rows.length) {
    return (
      <div className="widget-pad">
        <div className="widget-title">{cfg.title || "Batch Timeline"}</div>
        <div className="muted">No batches yet.</div>
      </div>
    );
  }
  // Linear time-mapped horizontal strip. Each batch is a colored bar
  // sized to its duration.
  const startTimes = rows.map((b) => new Date((b.started_utc || b.created_utc).replace(" ", "T") + "Z").getTime());
  const endTimes = rows.map((b, i) => {
    const e = b.ended_utc;
    return e ? new Date(e.replace(" ", "T") + "Z").getTime() : Date.now();
  });
  const t0 = Math.min(...startTimes);
  const t1 = Math.max(...endTimes);
  const span = Math.max(1, t1 - t0);
  return (
    <div className="widget-pad">
      <div className="widget-title">{cfg.title || "Batch Timeline"}</div>
      <div style={{ position: "relative", height: 40, background: "#0001", borderRadius: 4, overflow: "hidden" }}>
        {rows.map((b, i) => {
          const left = ((startTimes[i] - t0) / span) * 100;
          const width = Math.max(0.5, ((endTimes[i] - startTimes[i]) / span) * 100);
          const color =
            b.status === "completed" || b.status === "validated" ? "#2c8a4a" :
            b.status === "failed" ? "#c0382b" :
            b.status === "running" ? "#2a7ab8" :
            "#888";
          return (
            <div
              key={b.id}
              title={`${b.identifier || b.id} — ${b.status}`}
              style={{
                position: "absolute", top: 6, height: 28,
                left: `${left}%`, width: `${width}%`,
                background: color, borderRadius: 2,
              }}
            />
          );
        })}
      </div>
      <div className="muted" style={{ marginTop: 6, fontSize: 11 }}>
        {_fmt(new Date(t0).toISOString())} → {_fmt(new Date(t1).toISOString())}
      </div>
    </div>
  );
}


// ------------------------- Batch Input --------------------------------
// A dashboard widget that lets an operator type/scan a batch ID and
// immediately create + start a batch. Designed for the "barcode scan
// → batch start" workflow without ever leaving the dashboard.
/* Operator 2026-07-30: reworked to the v2 scan resolver. The card is JUST the
   field + Load button — the server decides whether the code starts a batch
   (barcode-gated definition or ad-hoc) or stops the running one (barcode stop
   mode). A transient one-line note confirms what happened, then fades so the
   card returns to field+button only. The field auto-refocuses after a few
   idle seconds so a keyboard-wedge scanner always lands here. */
export function BatchInputWidget({ widget }) {
  const cfg = widget?.config || {};
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null); // {tone:"ok"|"err", text}
  const noteTimerRef = useRef(null);

  const flash = useCallback((tone, text, ms) => {
    if (noteTimerRef.current) clearTimeout(noteTimerRef.current);
    setNote({ tone, text });
    noteTimerRef.current = setTimeout(() => setNote(null), ms || (tone === "err" ? 8000 : 5000));
  }, []);
  useEffect(() => () => { if (noteTimerRef.current) clearTimeout(noteTimerRef.current); }, []);

  const submit = useCallback(async (code) => {
    if (busy) return;
    setBusy(true);
    try {
      const out = await bmv2ScanBatch({
        barcode: code,
        definition_id: cfg.definition_id || undefined,
      });
      const row = out?.row || {};
      const label = row.reference || row.id || code;
      if (out?.action === "stopped") flash("ok", `Stopped ${label}`);
      else if (out?.action === "already_running") flash("ok", `${label} already running`);
      else flash("ok", `Started ${label}`);
    } catch (e) {
      flash("err", String(e?.message || e || "Scan failed"));
    } finally {
      setBusy(false);
    }
  }, [busy, cfg.definition_id, flash]);

  return (
    <div className="widget-pad" style={{ display: "flex", flexDirection: "column", justifyContent: "center", height: "100%" }}>
      <BarcodeScanInput
        onSubmit={submit}
        busy={busy}
        buttonLabel="Load"
        placeholder="Scan or type batch code…"
        autoRefocus={cfg.autofocus !== false}
      />
      {note ? (
        <div style={{ marginTop: 6, fontSize: 12, color: note.tone === "err" ? "var(--danger, #e5484d)" : "var(--muted)" }}>
          {note.text}
        </div>
      ) : null}
    </div>
  );
}
