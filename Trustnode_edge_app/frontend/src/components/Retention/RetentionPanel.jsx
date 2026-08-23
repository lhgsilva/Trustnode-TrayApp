/* Backup & Retention — storage status, retention policy, and backups.
   Operator 2026-08-21. Backed by services/retention_engine.py.

   Replaces three legacy cards (summary strip / Snapshot Backups / Retention and
   Cleanup Policy). The legacy retention card exposed four "keep days" numbers
   that mapped onto a job which, on a live edge, deleted raw history without ever
   aggregating it. This panel talks to the tiered engine instead and always shows
   the operator what a setting will actually cost in disk.

   Every mutating control is disabled unless `canEdit` AND the user is an admin;
   the server enforces the same rule and answers 403 otherwise. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getRetentionStatus, getRetentionOptions, listRetentionPolicies,
  saveRetentionPolicyV2, activateRetentionPolicy, deactivateRetentionPolicy,
  deleteRetentionPolicy, estimateRetentionPolicy, runRetentionV2, getRetentionRunsV2,
  compactDatabase, cancelDatabaseCompaction,
  listBackupsV2, createBackupV2, restoreBackupV2, cancelBackupRestore,
  deleteBackupV2, backupDownloadUrl,
} from "../../api";

/* ------------------------------------------------------------------ utils */
function fmtBytes(n) {
  const v = Number(n || 0);
  if (!v) return "0 B";
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)} GB`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)} MB`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)} kB`;
  return `${v} B`;
}
function fmtCount(n) {
  return Number(n || 0).toLocaleString();
}
function fmtTs(ts) {
  if (!ts) return "—";
  const s = String(ts).replace("T", " ");
  return s.length > 19 ? s.slice(0, 19) : s;
}
function fmtAge(seconds) {
  const s = Number(seconds || 0);
  if (!Number.isFinite(s)) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

const DURATION_UNITS = [
  { label: "hours", s: 3600 },
  { label: "days", s: 86400 },
  { label: "weeks", s: 604800 },
  { label: "months", s: 2592000 },
  { label: "years", s: 31536000 },
];

/** "30d" -> {n: 30, unit: 86400} for the two-field editor. */
function splitDuration(text) {
  const m = String(text || "").trim().match(/^(\d+(?:\.\d+)?)(s|m|h|d|w|mo|y)$/i);
  if (!m) return { n: 30, unit: 86400 };
  const n = Number(m[1]);
  const unit = { s: 1, m: 60, h: 3600, d: 86400, w: 604800, mo: 2592000, y: 31536000 }[m[2].toLowerCase()];
  const known = DURATION_UNITS.find((u) => u.s === unit);
  if (known) return { n, unit };
  const secs = n * unit;
  for (let i = DURATION_UNITS.length - 1; i >= 0; i -= 1) {
    if (secs >= DURATION_UNITS[i].s && secs % DURATION_UNITS[i].s === 0) {
      return { n: secs / DURATION_UNITS[i].s, unit: DURATION_UNITS[i].s };
    }
  }
  return { n: Math.max(1, Math.round(secs / 3600)), unit: 3600 };
}
function joinDuration(n, unitSeconds) {
  const suffix = { 3600: "h", 86400: "d", 604800: "w", 2592000: "mo", 31536000: "y" }[unitSeconds] || "d";
  return `${Math.max(1, Math.round(Number(n) || 1))}${suffix}`;
}

function blankPolicy() {
  return {
    id: "",
    name: "New retention policy",
    raw: { keep: "2d" },
    tiers: [
      { keep: "30d", resolution: "1m", aggregate: "avg" },
      { keep: "1y", resolution: "15m", aggregate: "avg" },
    ],
    text_tags: { keep: "1y" },
    maintenance: { window_local: "", catch_up_outside_window: true, max_run_minutes: 30, pace_ms_per_batch: 20, archive_before_prune: false, archive_location: "" },
    other_data: {},
    backups: { enabled: true, config_daily_keep: 14, historian_weekly_keep: 0, location: "" },
  };
}

/** A policy in one sentence: what is kept in full, what replaces it, and for how
    long. The tier list is the part operators consistently read as jargon. */
function describePolicyPlain(policy) {
  const rawKeep = policy?.raw?.keep || "";
  const tiers = Array.isArray(policy?.tiers) ? policy.tiers : [];
  if (!rawKeep && !tiers.length) return "";
  const parts = [];
  if (rawKeep) parts.push(`every reading for ${rawKeep}`);
  tiers.forEach((t) => {
    if (!t?.keep) return;
    parts.push(`then one ${t.aggregate || "avg"} per ${t.resolution || "1m"} for ${t.keep}`);
  });
  return `Keeps ${parts.join(", ")}. Anything older is removed.`;
}

/* ------------------------------------------------------------------ panel */
export default function RetentionPanel({ canEdit = false, isAdmin = false }) {
  const editable = Boolean(canEdit && isAdmin);

  const [status, setStatus] = useState(null);
  const [options, setOptions] = useState(null);
  const [policies, setPolicies] = useState([]);
  const [backups, setBackups] = useState([]);
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState(null);           // {tone, text}
  const [editor, setEditor] = useState(null);       // policy draft or null
  const [estimate, setEstimate] = useState(null);
  const [estimateError, setEstimateError] = useState("");
  const [confirm, setConfirm] = useState(null);     // {title, message, onConfirm}
  const estimateTimer = useRef(null);
  const mounted = useRef(true);

  useEffect(() => () => { mounted.current = false; if (estimateTimer.current) clearTimeout(estimateTimer.current); }, []);

  const flash = useCallback((tone, text) => {
    if (!mounted.current) return;
    setNote({ tone, text });
    if (tone !== "error") setTimeout(() => mounted.current && setNote(null), 6000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [st, pol, bk] = await Promise.all([
        getRetentionStatus().catch(() => null),
        listRetentionPolicies().catch(() => null),
        listBackupsV2().catch(() => null),
      ]);
      if (!mounted.current) return;
      if (st?.status) setStatus(st.status);
      if (Array.isArray(pol?.policies)) setPolicies(pol.policies);
      if (Array.isArray(bk?.rows)) setBackups(bk.rows);
    } catch (err) {
      flash("error", String(err?.message || err));
    }
  }, [flash]);

  useEffect(() => {
    refresh();
    getRetentionOptions().then((o) => mounted.current && setOptions(o)).catch(() => {});
    getRetentionRunsV2(15).then((r) => mounted.current && setRuns(r?.runs || [])).catch(() => {});
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  const active = status?.policy || null;
  const noPolicy = !active;

  /* ---------------------------------------------------------- estimate */
  const requestEstimate = useCallback((draft) => {
    if (estimateTimer.current) clearTimeout(estimateTimer.current);
    estimateTimer.current = setTimeout(async () => {
      try {
        const res = await estimateRetentionPolicy(draft);
        if (!mounted.current) return;
        setEstimate(res.estimate);
        setEstimateError("");
      } catch (err) {
        if (!mounted.current) return;
        setEstimate(null);
        setEstimateError(String(err?.message || err));
      }
    }, 350);
  }, []);

  const openEditor = useCallback((policy) => {
    const draft = policy ? JSON.parse(JSON.stringify(policy)) : blankPolicy();
    delete draft.is_active; delete draft.version; delete draft.updated_utc;
    delete draft.created_utc; delete draft.updated_by;
    setEditor(draft);
    setEstimate(null);
    setEstimateError("");
    requestEstimate(draft);
  }, [requestEstimate]);

  const patchDraft = useCallback((fn) => {
    setEditor((prev) => {
      if (!prev) return prev;
      const next = JSON.parse(JSON.stringify(prev));
      fn(next);
      requestEstimate(next);
      return next;
    });
  }, [requestEstimate]);

  /* ----------------------------------------------------------- actions */
  const act = useCallback(async (label, fn, okText) => {
    setBusy(label);
    try {
      const res = await fn();
      await refresh();
      if (okText) flash("ok", typeof okText === "function" ? okText(res) : okText);
      return res;
    } catch (err) {
      flash("error", String(err?.message || err));
      return null;
    } finally {
      if (mounted.current) setBusy("");
    }
  }, [refresh, flash]);

  /* A maintenance pass on a real historian takes minutes. Start it in the
     background, then follow status.engine.busy until it lands and report what
     the run actually did. Previously this blocked one HTTP request for the
     whole pass, which the browser aborted at 12 s — the work carried on and the
     operator was told nothing had happened. */
  const runMaintenance = useCallback(async (dryRun) => {
    const label = dryRun ? "dry" : "run";
    setBusy(label);
    try {
      const kick = await runRetentionV2(dryRun, true, true);
      // The engine runs one pass at a time. When its own scheduled pass is
      // already in flight, ours is not queued — say so instead of implying the
      // click did nothing, then follow the pass that IS running.
      if (kick && kick.started === false) {
        flash("ok", "A maintenance pass was already running — following it.");
      }
      const startedAt = Date.now();
      // Poll for up to 60 minutes; the engine caps its own pass well below this.
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000));
        if (!mounted.current) return;
        let st = null;
        try { st = await getRetentionStatus(); } catch { /* keep waiting */ }
        if (st?.status) setStatus(st.status);
        const stillBusy = Boolean(st?.status?.engine?.busy);
        if (!stillBusy && Date.now() - startedAt > 3000) break;
        if (Date.now() - startedAt > 3600000) break;
      }
      const r = await getRetentionRunsV2(15).catch(() => null);
      if (mounted.current && r?.runs) setRuns(r.runs);
      const latest = (r?.runs || [])[0];
      await refresh();
      flash("ok", dryRun
        ? `Preview complete — ${describeSummary(latest?.details)}`
        : `Maintenance complete — ${describeSummary(latest?.details)}`);
    } catch (err) {
      flash("error", String(err?.message || err));
    } finally {
      if (mounted.current) setBusy("");
    }
  }, [refresh, flash]);

  const savePolicy = useCallback(async (activateAfter) => {
    if (!editor) return;
    const res = await act("save", () => saveRetentionPolicyV2({ ...editor, activate: activateAfter }),
      activateAfter ? "Policy saved and activated." : "Policy saved.");
    if (res) {
      setEditor(null);
      getRetentionRunsV2(15).then((r) => mounted.current && setRuns(r?.runs || [])).catch(() => {});
    }
  }, [editor, act]);

  const levels = status?.levels || [];
  const db = status?.database || {};
  const disk = status?.disk || {};
  const est = status?.estimate || {};
  const collection = status?.collection || {};

  /* ------------------------------------------------------------ render */
  return (
    <>
      {/* ---------------------------------------------------- 1. STORAGE */}
      <section className="card">
        <div className="row backup-card-header">
          <h3 className="card-title">Storage</h3>
          <div className="row">
            <button className="btn btn-secondary btn-sm" disabled={!!busy}
              title="Show what maintenance would remove, without changing anything"
              onClick={() => runMaintenance(true)}>
              {busy === "dry" ? "Checking…" : "Preview"}
            </button>
            <button className="btn btn-primary btn-sm" disabled={!editable || !!busy}
              title={noPolicy
                ? "Activate a retention policy first — with none, there is nothing to remove"
                : "Apply the active policy now: roll up and remove data past its keep window"}
              onClick={() => runMaintenance(false)}>
              {busy === "run" ? "Running…" : "Run maintenance now"}
            </button>
          </div>
        </div>

        <div className="table db-overview-table">
          <div className="thead">
            <span>Database</span><span>Full-detail history</span><span>Collecting</span>
            <span>Disk free</span><span>Reclaimable</span>
          </div>
          <div className="trow">
            {/* `status` is null until the first fetch lands; showing 0 B / 0
                readings then reads as "nothing stored", which is the opposite
                of the truth. Say "measuring…" until we actually know. */}
            <span className="db-cell">{status ? fmtBytes(db.size_bytes) : "measuring…"}</span>
            <span className="db-cell">
              {db.raw_rows === null || db.raw_rows === undefined
                ? "measuring…"
                : `${fmtCount(db.raw_rows)} readings`}
              {db.oldest_raw_utc ? ` · from ${fmtTs(db.oldest_raw_utc)}` : ""}
            </span>
            <span className="db-cell">
              {collection.tag_count
                ? `${collection.tag_count} tags @ ${collection.interval_s}s`
                : "—"}
            </span>
            <span className="db-cell">
              {disk.free_bytes ? `${fmtBytes(disk.free_bytes)} (${disk.free_pct}%)` : "—"}
              {disk.emergency ? " ⚠" : ""}
            </span>
            <span className="db-cell">{status ? fmtBytes(db.reclaimable_bytes) : "—"}</span>
          </div>
        </div>

        {noPolicy ? (
          <div className={disk.warn ? "error" : "info-note"} style={{ marginTop: 10 }}>
            <strong>No retention policy is active — nothing is ever deleted.</strong>{" "}
            At the current rate this stores about <strong>{fmtBytes(est.per_day_raw_bytes)} per day</strong>
            {est.no_policy_year_bytes ? ` (${fmtBytes(est.no_policy_year_bytes)} per year)` : ""}.
            {status?.days_until_full
              ? ` This disk fills in roughly ${Math.round(status.days_until_full)} days.`
              : ""}
            {editable ? " Choose a policy below to keep the detail you need and average the rest." : ""}
          </div>
        ) : null}

        {levels.length > 1 ? (
          <div className="table backup-files-table" style={{ marginTop: 10 }}>
            <div className="thead">
              <span>Level</span><span>Kept for</span><span>Stored</span><span>Covers</span><span>Up to date</span>
            </div>
            {levels.map((lv) => (
              <div className="trow" key={lv.key}>
                <span className="db-cell">{lv.label}</span>
                <span className="db-cell">{lv.keep || "—"}</span>
                <span className="db-cell">{fmtCount(lv.rows)}</span>
                <span className="db-cell">
                  {lv.oldest_utc ? `${fmtTs(lv.oldest_utc)} → ${fmtTs(lv.newest_utc)}` : "—"}
                </span>
                <span className="db-cell">
                  {lv.key === "raw"
                    ? "live"
                    : lv.lag_s == null
                      ? "not built yet"
                      : `${fmtAge(lv.lag_s)} behind`}
                  {lv.last_error ? ` ⚠ ${lv.last_error}` : ""}
                </span>
              </div>
            ))}
          </div>
        ) : null}

        {db.reclaimable_bytes > 500e6 ? (
          <div className="info-note" style={{ marginTop: 10 }}>
            {fmtBytes(db.reclaimable_bytes)} inside the database file is free space left by
            deleted history. New readings reuse it, so the file stops growing — but you can
            hand it back to the disk.
            <button className="btn btn-secondary btn-sm" style={{ marginLeft: 10 }}
              disabled={!editable || !!busy}
              onClick={() => setConfirm({
                title: "Compact the database?",
                message: `This writes a compacted copy (needs about ${fmtBytes((db.size_bytes || 0) * 1.2)} free) and swaps it in the next time TrustNode starts. Collection keeps running while it is prepared.`,
                onConfirm: () => act("compact", compactDatabase, (r) => r?.message || "Compaction staged."),
              })}>
              {busy === "compact" ? "Preparing…" : "Compact…"}
            </button>
          </div>
        ) : null}

        {status?.pending_compaction ? (
          <div className="info-note" style={{ marginTop: 10 }}>
            A compacted database is ready and will be applied at the next start.
            <button className="btn btn-danger btn-sm" style={{ marginLeft: 10 }} disabled={!editable}
              onClick={() => act("cancelcompact", cancelDatabaseCompaction, "Compaction cancelled.")}>
              Cancel
            </button>
          </div>
        ) : null}

        {note ? (
          <div className={note.tone === "error" ? "error" : "info-note"} style={{ marginTop: 10 }}>
            {note.text}
          </div>
        ) : null}
      </section>

      {/* ----------------------------------------------------- 2. POLICY */}
      <section className="card">
        <div className="row backup-card-header">
          <h3 className="card-title">Retention policy</h3>
          <div className="row">
            <button className="btn btn-primary btn-sm" disabled={!editable} onClick={() => openEditor(null)}>
              New policy…
            </button>
            {active ? (
              <button className="btn btn-danger btn-sm" disabled={!editable || !!busy}
                onClick={() => setConfirm({
                  title: "Turn retention off?",
                  message: "Nothing will be deleted from now on, and the database will grow with every reading. Aggregated history already built is kept.",
                  onConfirm: () => act("deact", deactivateRetentionPolicy, "Retention is off — nothing will be deleted."),
                })}>
                Turn off
              </button>
            ) : null}
          </div>
        </div>

        <p className="muted" style={{ marginTop: 0 }}>
          Keep full detail for as long as you need it, then store averages instead. Older data
          stays available for trends and reports — at a resolution you choose — for up to five years.
        </p>

        <div className="backup-files-table-wrap">
        <div className="table backup-files-table has-row-actions">
          <div className="thead">
            <span>Policy</span><span>Full detail</span><span>Then</span><span>Status</span><span>Actions</span>
          </div>
          {policies.map((p) => {
            const isActive = Boolean(p.is_active);
            return (
              <div className={`trow ${isActive ? "selected-row" : ""}`} key={p.id}>
                <span className="db-cell">{p.name}</span>
                <span className="db-cell">{p.raw?.keep || "—"}</span>
                <span className="db-cell">
                  {(p.tiers || []).length
                    ? (p.tiers || []).map((t) => `${t.resolution} for ${t.keep}`).join(" · ")
                    : "no aggregation"}
                </span>
                <span className="db-cell">
                  {isActive ? <span className="status-pill status-online">Active</span> : "—"}
                </span>
                <span className="row-actions db-actions-cell">
                  {!isActive ? (
                    <button className="btn btn-success btn-sm" disabled={!editable || !!busy}
                      onClick={() => setConfirm({
                        title: `Activate "${p.name}"?`,
                        message: buildActivationWarning(p, status),
                        onConfirm: () => act("activate", () => activateRetentionPolicy(p.id), "Policy activated."),
                      })}>Activate</button>
                  ) : null}
                  <button className="btn btn-secondary btn-sm" disabled={!editable}
                    onClick={() => openEditor(p)}>Edit</button>
                  <button className="btn btn-secondary btn-sm" disabled={!editable}
                    onClick={() => openEditor({ ...p, id: "", name: `${p.name} (copy)` })}>Duplicate</button>
                  {!isActive ? (
                    <button className="btn btn-danger btn-sm" disabled={!editable || !!busy}
                      onClick={() => act("del", () => deleteRetentionPolicy(p.id), "Policy deleted.")}>
                      Delete
                    </button>
                  ) : null}
                </span>
              </div>
            );
          })}
          {!policies.length ? (
            <div className="trow">
              <span className="db-cell">—</span><span className="db-cell">—</span>
              <span className="db-cell">No policies yet — start from a preset below</span>
              <span className="db-cell">—</span><span className="db-cell">—</span>
            </div>
          ) : null}
        </div>
        </div>

        {options?.presets?.length ? (
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ marginBottom: 6 }}>Start from a preset:</div>
            <div className="retention-preset-grid">
              {options.presets.map((preset) => (
                <div key={preset.id} className="retention-preset">
                  <button className="btn btn-secondary btn-sm" disabled={!editable}
                    onClick={() => openEditor({ ...preset, id: "" })}>
                    {preset.name}
                  </button>
                  {preset.description ? (
                    <div className="muted retention-preset-desc">{preset.description}</div>
                  ) : null}
                  <div className="muted retention-preset-desc">
                    {describePolicyPlain(preset)}
                  </div>
                </div>
              ))}
            </div>
            <div className="muted" style={{ marginTop: 8 }}>
              A preset only fills in the form — nothing changes until you review it and
              choose <strong>Save and activate</strong>.
            </div>
          </div>
        ) : null}
      </section>

      {/* ---------------------------------------------------- 3. BACKUPS */}
      <section className="card">
        <div className="row backup-card-header">
          <h3 className="card-title">Backups</h3>
          <div className="row">
            <button className="btn btn-primary btn-sm" disabled={!editable || !!busy}
              onClick={() => act("bkcfg", () => createBackupV2("config", "manual"),
                (r) => `Settings backup created (${fmtBytes(r?.backup?.size_bytes)}).`)}>
              {busy === "bkcfg" ? "Working…" : "Back up settings"}
            </button>
            <button className="btn btn-secondary btn-sm" disabled={!editable || !!busy}
              onClick={() => setConfirm({
                title: "Back up everything?",
                message: `This copies the whole database including history — about ${fmtBytes(db.size_bytes)}. Collection keeps running.`,
                onConfirm: () => act("bkfull", () => createBackupV2("full", "manual"),
                  (r) => `Full backup created (${fmtBytes(r?.backup?.size_bytes)}).`),
              })}>
              {busy === "bkfull" ? "Working…" : "Back up everything…"}
            </button>
          </div>
        </div>

        <p className="muted" style={{ marginTop: 0 }}>
          <strong>Settings</strong> backups hold your users, gateways, dashboards, alarms,
          report templates and licence — small enough to keep many.{" "}
          <strong>Full</strong> backups add the recorded history.
          {!active
            ? " Settings are backed up daily by default (14 kept), even without a retention policy."
            : active.backups?.enabled
              ? ` Automatic: settings daily (keeping ${active.backups.config_daily_keep})${
                  active.backups.historian_weekly_keep
                    ? `, full weekly (keeping ${active.backups.historian_weekly_keep})`
                    : ""}.`
              : " Automatic backups are turned off in this policy."}
        </p>

        {status?.pending_restore ? (
          <div className="info-note" style={{ marginBottom: 10 }}>
            <strong>Restore pending:</strong> {status.pending_restore.filename} will be applied the
            next time TrustNode starts. Your current database is kept as a safety copy first.
            <button className="btn btn-danger btn-sm" style={{ marginLeft: 10 }} disabled={!editable}
              onClick={() => act("cancelrestore", cancelBackupRestore, "Pending restore cancelled.")}>
              Cancel restore
            </button>
          </div>
        ) : null}

        <div className="backup-files-table-wrap">
        <div className="table backup-files-table has-row-actions">
          <div className="thead">
            <span>Created</span><span>Type</span><span>File</span><span>Size</span><span>Actions</span>
          </div>
          {backups.map((b) => (
            <div className="trow" key={b.filename}>
              <span className="db-cell">{fmtTs(b.modified_utc)}</span>
              <span className="db-cell">
                <span className={`status-pill ${b.kind === "config" ? "status-online" : ""}`}>
                  {b.kind === "config" ? "Settings" : b.kind === "safety" ? "Safety copy" : "Full"}
                </span>
              </span>
              <span className="db-cell" title={b.path}>{b.filename}</span>
              <span className="db-cell">{fmtBytes(b.size_bytes)}</span>
              <span className="row-actions db-actions-cell">
                <a className="btn btn-secondary btn-sm" href={backupDownloadUrl(b.filename)}
                   download={b.filename}>Download</a>
                <button className="btn btn-success btn-sm" disabled={!editable || !!busy}
                  onClick={() => setConfirm({
                    title: "Restore this backup?",
                    message: `${b.filename} will replace the current database the next time TrustNode starts. The current database is kept as a safety copy, and you can cancel until then.`,
                    onConfirm: () => act("restore", () => restoreBackupV2(b.filename),
                      (r) => r?.message || "Restore staged."),
                  })}>Restore…</button>
                <button className="btn btn-danger btn-sm" disabled={!editable || !!busy}
                  onClick={() => act("delbk", () => deleteBackupV2(b.filename), "Backup deleted.")}>
                  Delete
                </button>
              </span>
            </div>
          ))}
          {!backups.length ? (
            <div className="trow">
              <span className="db-cell">—</span><span className="db-cell">—</span>
              <span className="db-cell">No backups yet</span>
              <span className="db-cell">—</span><span className="db-cell">—</span>
            </div>
          ) : null}
        </div>
        </div>
      </section>

      {/* ---------------------------------------------------- 4. HISTORY */}
      {runs.length ? (
        <section className="card">
          <h3 className="card-title">Maintenance history</h3>
          <div className="table retention-runs-table">
            <div className="thead"><span>When</span><span>Mode</span><span>Result</span><span>What happened</span></div>
            {runs.slice(0, 12).map((r) => (
              <div className="trow" key={r.id}>
                <span className="db-cell">{fmtTs(r.run_utc)}</span>
                <span className="db-cell">{r.dry_run ? "Preview" : "Applied"}</span>
                <span className="db-cell">{r.status}</span>
                <span className="db-cell">{describeSummary(r.details)}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {/* ----------------------------------------------------- EDITOR */}
      {editor ? (
        <PolicyEditor
          draft={editor}
          options={options}
          estimate={estimate}
          estimateError={estimateError}
          busy={busy === "save"}
          onPatch={patchDraft}
          onCancel={() => setEditor(null)}
          onSave={savePolicy}
        />
      ) : null}

      {/* ---------------------------------------------------- CONFIRM */}
      {confirm ? (
        <div className="modal-backdrop">
          <div className="modal-card confirm-card">
            <h3>{confirm.title}</h3>
            <p style={{ whiteSpace: "pre-wrap" }}>{confirm.message}</p>
            <div className="row modal-actions">
              <button className="btn btn-primary" onClick={() => { const fn = confirm.onConfirm; setConfirm(null); fn && fn(); }}>
                Continue
              </button>
              <button className="btn btn-danger" onClick={() => setConfirm(null)}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

/* ------------------------------------------------------------ helpers */
function describeSummary(summary) {
  // Guard against any malformed input so a bad legacy row never takes the page down.
  try {
    if (!summary || typeof summary !== "object") return "—";

    // Rows aggregated:
    //   v2 engine  → summary.rollups is an ARRAY of {tier, rows, …}
    //   legacy path → summary.rollups is an OBJECT {minute_upserts, hour_upserts, day_upserts}
    let rolled = 0;
    const rollups = summary.rollups;
    if (Array.isArray(rollups)) {
      rolled = rollups.reduce((a, r) => a + Number(r?.rows || 0), 0);
    } else if (rollups && typeof rollups === "object") {
      // Legacy: sum all numeric values (minute_upserts, hour_upserts, day_upserts)
      rolled = Object.values(rollups).reduce(
        (a, v) => a + (Number.isFinite(Number(v)) ? Number(v) : 0), 0
      );
    }

    // Rows removed:
    //   v2 engine  → summary.prunes is an ARRAY of {tier, deleted, remaining, held_by, …}
    //   legacy path → summary.deletes is an OBJECT {raw_candidates, minute_candidates, …}
    //                 (candidate counts — rows eligible for deletion, not necessarily deleted)
    let pruned = 0;
    const prunes = summary.prunes;
    const legacyDeletes = summary.deletes;
    if (Array.isArray(prunes)) {
      pruned = prunes.reduce((a, p) => a + Number(p?.deleted || 0), 0);
    } else if (legacyDeletes && typeof legacyDeletes === "object") {
      pruned = Object.values(legacyDeletes).reduce(
        (a, v) => a + (Number.isFinite(Number(v)) ? Number(v) : 0), 0
      );
    }

    // held_by is only meaningful for the v2 prunes array.
    const held = Array.isArray(prunes) ? prunes.filter((p) => p?.held_by) : [];

    const parts = [];
    if (rolled) parts.push(`${fmtCount(rolled)} rows aggregated`);
    if (pruned) parts.push(`${fmtCount(pruned)} old readings removed`);

    // Backups:
    //   v2 → summary.backups is an ARRAY of {kind, …}
    //   legacy → may carry a backup_path string (no array)
    if (Array.isArray(summary.backups)) {
      summary.backups.forEach((b) => parts.push(`${b?.kind === "config" ? "settings" : "full"} backup`));
    } else if (summary.backup_path) {
      parts.push("full backup");
    }

    if (held.length) parts.push(held[0].held_by);

    // notes is a v2 field; may be absent or non-array in legacy rows.
    const notesArr = Array.isArray(summary.notes) ? summary.notes : [];
    if (!parts.length && notesArr.length) return notesArr[0];
    return parts.length ? parts.join(", ") : "nothing to do";
  } catch (_) {
    // Malformed row — show a safe placeholder rather than crashing.
    return "—";
  }
}

function buildActivationWarning(policy, status) {
  const raw = policy?.raw?.keep || "";
  const oldest = status?.database?.oldest_raw_utc;
  let msg = `Full-detail readings older than ${raw} will be replaced by averages.`;
  if ((policy?.tiers || []).length) {
    msg += `\n\nKept afterwards: ${(policy.tiers || []).map((t) => `${t.resolution} averages for ${t.keep}`).join(", ")}.`;
  } else {
    msg += "\n\nThis policy has no aggregate levels, so older readings are deleted outright.";
  }
  if (oldest) msg += `\n\nThe oldest reading you have now is from ${fmtTs(oldest)}.`;
  msg += "\n\nNothing is deleted until it has been aggregated, and maintenance runs in small "
       + "batches so collection is not interrupted.";
  return msg;
}

/* ------------------------------------------------------- policy editor */
function PolicyEditor({ draft, options, estimate, estimateError, busy, onPatch, onCancel, onSave }) {
  const resolutions = options?.resolutions || [];
  const aggregates = options?.aggregates || ["avg", "min", "max", "last", "first", "sum"];
  const rawKeep = splitDuration(draft.raw?.keep);

  const total = estimate?.total_bytes;
  const perDay = estimate?.per_day_raw_bytes;

  return (
    <div className="modal-backdrop">
      <div className="modal-card modal-card-wide">
        <h3>{draft.id ? "Edit retention policy" : "New retention policy"}</h3>

        <label>
          Policy name
          <input value={draft.name || ""} onChange={(e) => onPatch((d) => { d.name = e.target.value; })} />
        </label>

        <div className="muted" style={{ margin: "12px 0 6px" }}>
          How long to keep every single reading, and what to store once that window passes.
        </div>
        <div className="muted retention-editor-help">
          Data moves down this list as it ages. The first row is full detail — every
          reading exactly as collected. Each row below replaces it with one value per
          interval (an average, minimum, maximum…), which is far smaller but still
          charts and reports correctly. Data older than the last row is deleted.
          Change any number and the estimated size updates as you type.
        </div>

        <div className="table backup-files-table">
          <div className="thead">
            <span>Age of the data</span><span>What is stored</span><span>Value shown</span>
            <span>Estimated size</span><span></span>
          </div>

          <div className="trow">
            <span className="db-cell">
              Up to{" "}
              <input type="number" min="1" style={{ width: 70 }} value={rawKeep.n}
                onChange={(e) => onPatch((d) => { d.raw = { keep: joinDuration(e.target.value, rawKeep.unit) }; })} />
              {" "}
              <select style={{ width: 100 }} value={rawKeep.unit}
                onChange={(e) => onPatch((d) => { d.raw = { keep: joinDuration(rawKeep.n, Number(e.target.value)) }; })}>
                {DURATION_UNITS.filter((u) => u.s <= 31536000).map((u) => (
                  <option key={u.s} value={u.s}>{u.label}</option>
                ))}
              </select>
            </span>
            <span className="db-cell"><strong>Every reading (full detail)</strong></span>
            <span className="db-cell">as recorded</span>
            <span className="db-cell">{fmtBytes(estimate?.levels?.find((l) => l.key === "raw")?.bytes)}</span>
            <span className="db-cell" />
          </div>

          {(draft.tiers || []).map((tier, idx) => {
            const keep = splitDuration(tier.keep);
            const lvl = estimate?.levels?.[idx + 1];
            return (
              <div className="trow" key={idx}>
                <span className="db-cell">
                  Up to{" "}
                  <input type="number" min="1" style={{ width: 70 }} value={keep.n}
                    onChange={(e) => onPatch((d) => { d.tiers[idx].keep = joinDuration(e.target.value, keep.unit); })} />
                  {" "}
                  <select style={{ width: 100 }} value={keep.unit}
                    onChange={(e) => onPatch((d) => { d.tiers[idx].keep = joinDuration(keep.n, Number(e.target.value)); })}>
                    {DURATION_UNITS.map((u) => <option key={u.s} value={u.s}>{u.label}</option>)}
                  </select>
                </span>
                <span className="db-cell">
                  one value every{" "}
                  <select value={tier.resolution}
                    onChange={(e) => onPatch((d) => { d.tiers[idx].resolution = e.target.value; })}>
                    {resolutions.map((r) => <option key={r.label} value={r.label}>{r.label}</option>)}
                  </select>
                </span>
                <span className="db-cell">
                  <select value={tier.aggregate || "avg"}
                    onChange={(e) => onPatch((d) => { d.tiers[idx].aggregate = e.target.value; })}>
                    {aggregates.map((a) => <option key={a} value={a}>{a}</option>)}
                  </select>
                </span>
                <span className="db-cell">{fmtBytes(lvl?.bytes)}</span>
                <span className="row-actions db-actions-cell">
                  <button className="btn btn-danger btn-sm"
                    onClick={() => onPatch((d) => { d.tiers.splice(idx, 1); })}>Remove</button>
                </span>
              </div>
            );
          })}
        </div>

        <div className="row" style={{ marginTop: 8, justifyContent: "space-between", alignItems: "center" }}>
          <button className="btn btn-secondary btn-sm"
            disabled={(draft.tiers || []).length >= (options?.max_tiers || 6)}
            onClick={() => onPatch((d) => {
              const last = d.tiers[d.tiers.length - 1];
              const nextKeep = last ? splitDuration(last.keep) : { n: 30, unit: 86400 };
              d.tiers.push({
                keep: joinDuration(Math.max(2, nextKeep.n * 4), nextKeep.unit),
                resolution: last ? nextResolution(last.resolution, resolutions) : "1m",
                aggregate: "avg",
              });
            })}>
            + Add a level
          </button>
          <div style={{ textAlign: "right" }}>
            {total != null ? (
              <div><strong>About {fmtBytes(total)}</strong> once it settles</div>
            ) : null}
            {perDay ? (
              <div className="muted" style={{ fontSize: 12 }}>
                without a policy this would grow {fmtBytes(perDay)} every day
              </div>
            ) : null}
          </div>
        </div>

        {estimateError ? (
          <div className="status error" style={{ marginTop: 10 }}>{estimateError}</div>
        ) : null}

        <details style={{ marginTop: 14 }}>
          <summary style={{ cursor: "pointer" }}>Advanced</summary>
          <div className="trigger-form-grid" style={{ marginTop: 10 }}>
            <label>
              Text tags — keep changes for
              <input value={draft.text_tags?.keep || ""} placeholder="1y"
                onChange={(e) => onPatch((d) => { d.text_tags = { keep: e.target.value }; })} />
            </label>
            <label>
              Housekeeping window (local time)
              <input placeholder="e.g. 01:00-05:00 — leave empty for any time"
                value={draft.maintenance?.window_local || ""}
                onChange={(e) => onPatch((d) => { d.maintenance = { ...(d.maintenance || {}), window_local: e.target.value }; })} />
            </label>
            <label>
              Application log — keep for
              <input value={draft.other_data?.app_logs_keep || ""} placeholder="90d"
                onChange={(e) => onPatch((d) => { d.other_data = { ...(d.other_data || {}), app_logs_keep: e.target.value }; })} />
            </label>
            <label>
              Report files — keep for
              <input value={draft.other_data?.reports_files_keep || ""} placeholder="2y"
                onChange={(e) => onPatch((d) => { d.other_data = { ...(d.other_data || {}), reports_files_keep: e.target.value }; })} />
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input type="checkbox" style={{ width: "auto" }}
                checked={draft.backups?.enabled !== false}
                onChange={(e) => onPatch((d) => { d.backups = { ...(d.backups || {}), enabled: e.target.checked }; })} />
              Automatic backups
            </label>
            <label>
              Daily settings backups to keep
              <input type="number" min="0" max="365" value={draft.backups?.config_daily_keep ?? 14}
                onChange={(e) => onPatch((d) => { d.backups = { ...(d.backups || {}), config_daily_keep: Number(e.target.value) }; })} />
            </label>
            <label>
              Weekly full backups to keep (0 = off)
              <input type="number" min="0" max="52" value={draft.backups?.historian_weekly_keep ?? 0}
                onChange={(e) => onPatch((d) => { d.backups = { ...(d.backups || {}), historian_weekly_keep: Number(e.target.value) }; })} />
            </label>
            <label>
              Backup folder (empty = alongside the database)
              <input value={draft.backups?.location || ""}
                onChange={(e) => onPatch((d) => { d.backups = { ...(d.backups || {}), location: e.target.value }; })} />
            </label>
          </div>
        </details>

        <div className="row modal-actions" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" disabled={busy || !!estimateError}
            onClick={() => onSave(true)}>
            {busy ? "Saving…" : "Save and activate"}
          </button>
          <button className="btn btn-secondary" disabled={busy || !!estimateError}
            onClick={() => onSave(false)}>Save only</button>
          <button className="btn btn-danger" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function nextResolution(current, resolutions) {
  const idx = resolutions.findIndex((r) => r.label === current);
  if (idx >= 0 && idx + 1 < resolutions.length) {
    // step to the next resolution that is a whole multiple of the current one
    const cur = resolutions[idx].seconds;
    for (let i = idx + 1; i < resolutions.length; i += 1) {
      if (resolutions[i].seconds % cur === 0) return resolutions[i].label;
    }
  }
  return "1h";
}
