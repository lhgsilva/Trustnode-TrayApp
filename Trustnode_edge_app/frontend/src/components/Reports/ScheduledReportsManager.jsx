import React, { useEffect, useMemo, useState, useRef } from "react";
import {
  deleteGeneratedReport,
  deleteScheduledReport,
  downloadGeneratedReportBlob,
  emailGeneratedReport,
  getReportSchedulerStatus,
  listGeneratedReports,
  listReportTemplates,
  listScheduledReports,
  runScheduledReport,
  saveScheduledReport,
} from "../../api";

// ---------------------------------------------------------------------------
// inline SVG icons (kept local so we don't bind to the App.jsx icon set)
// ---------------------------------------------------------------------------
const ICON_STROKE = { stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" };
const Icon = ({ children, size = 16, title }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden={!title} role={title ? "img" : "presentation"}>
    {title ? <title>{title}</title> : null}
    {children}
  </svg>
);
const IconEye = (p) => (
  <Icon {...p}>
    <path d="M2 12c2.5-4.5 6-7 10-7s7.5 2.5 10 7c-2.5 4.5-6 7-10 7S4.5 16.5 2 12Z" {...ICON_STROKE} />
    <circle cx="12" cy="12" r="3" {...ICON_STROKE} />
  </Icon>
);
const IconDownload = (p) => (
  <Icon {...p}>
    <path d="M12 4v11" {...ICON_STROKE} />
    <path d="M7 11l5 5 5-5" {...ICON_STROKE} />
    <path d="M5 19h14" {...ICON_STROKE} />
  </Icon>
);
const IconMail = (p) => (
  <Icon {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2" {...ICON_STROKE} />
    <path d="m3 7 9 7 9-7" {...ICON_STROKE} />
  </Icon>
);
const IconTrash = (p) => (
  <Icon {...p}>
    <path d="M4 7h16" {...ICON_STROKE} />
    <path d="M9 7V4h6v3" {...ICON_STROKE} />
    <path d="M6 7v13h12V7" {...ICON_STROKE} />
    <path d="M10 11v6M14 11v6" {...ICON_STROKE} />
  </Icon>
);
const IconRefresh = (p) => (
  <Icon {...p}>
    <path d="M3 12a9 9 0 0 1 15.5-6.2L21 8" {...ICON_STROKE} />
    <path d="M21 3v5h-5" {...ICON_STROKE} />
    <path d="M21 12a9 9 0 0 1-15.5 6.2L3 16" {...ICON_STROKE} />
    <path d="M3 21v-5h5" {...ICON_STROKE} />
  </Icon>
);
const IconPlay = (p) => (
  <Icon {...p}>
    <path d="M7 5l12 7-12 7Z" {...ICON_STROKE} />
  </Icon>
);
const IconPlus = (p) => (
  <Icon {...p}>
    <path d="M12 5v14M5 12h14" {...ICON_STROKE} />
  </Icon>
);

const RECURRENCES = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];
const TRIGGER_MODES = [
  { value: "time", label: "Time only" },
  { value: "tag", label: "Tag-condition only" },
  { value: "both", label: "Time AND tag conditions" },
];
const CONDITION_LOGICS = [
  { value: "all", label: "All conditions" },
  { value: "any", label: "Any condition" },
];
const RULE_OPERATORS = [
  { value: "gt", label: ">" },
  { value: "gte", label: ">=" },
  { value: "lt", label: "<" },
  { value: "lte", label: "<=" },
  { value: "eq", label: "=" },
  { value: "ne", label: "!=" },
  { value: "between", label: "Between" },
];

function makeId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function emptySchedule(templateId = "") {
  return {
    id: "",
    name: "New schedule",
    template_id: templateId,
    enabled: true,
    trigger_mode: "time",
    recurrence: "daily",
    hour: 8,
    minute: 0,
    day_of_week: 0,
    day_of_month: 1,
    tag_conditions: [],
    condition_logic: "all",
    deliver_email: false,
    recipients: [],
    email_subject: "",
    email_body: "",
    require_gateway_running: false,
    // Attachment selection: PDF on by default (back-compat), CSV/TXT opt-in.
    // The CSV bundles every chart + data-table section as a single file; the
    // TXT is the same content as pipe-delimited plain text.
    attach_pdf: true,
    attach_csv: false,
    attach_txt: false,
  };
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

async function openPdfInTab(generatedId) {
  const blob = await downloadGeneratedReportBlob(generatedId);
  const url = URL.createObjectURL(blob);
  const win = window.open(url, "_blank", "noopener,noreferrer");
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return win;
}

// Resolve the TrustNode logo URL — mirrors App.jsx logic so the same file is
// served for the header and the generated-reports card branding.
function resolveLogoSrc() {
  try {
    const protocol = String(window.location?.protocol || "");
    if (protocol === "file:") return "trustnode_logo.png";
    return `${String(window.location?.origin || "").replace(/\/+$/, "")}/trustnode_logo.png`;
  } catch {
    return "/trustnode_logo.png";
  }
}

export function ScheduledReportsManager({
  gatewayOptions = [],
  tagsByGateway = {},
  emailSettings = null,
  formatTagForDisplay = (x) => x,
  onNotify = () => {},
  // Which cards to render: "all" (default) = schedule config + generated list;
  // "schedule" = config only; "generated" = generated list only. Lets the same
  // component power both the Scheduled Reports page and a standalone Generated
  // Reports page without duplicating any fetch/handler logic.
  mode = "all",
}) {
  const showSchedule = mode === "all" || mode === "schedule";
  const showGenerated = mode === "all" || mode === "generated";
  const [templates, setTemplates] = useState([]);
  const [schedules, setSchedules] = useState([]);
  const [generated, setGenerated] = useState([]);
  const [draft, setDraft] = useState(emptySchedule());
  const [recipientsInput, setRecipientsInput] = useState("");
  const [busy, setBusy] = useState("");
  const [filterScheduleId, setFilterScheduleId] = useState("");
  // Live "any PLC gateway currently collecting?" flag. Refreshed alongside the
  // schedule list so the "Only when running" checkbox shows live state.
  const [anyGatewayRunning, setAnyGatewayRunning] = useState(true);
  const logoSrc = useMemo(resolveLogoSrc, []);

  // Item 9 (2026-08-22): one dropped request used to raise a red banner and
  // leave the page empty — which is what "reports not working in any version"
  // looked like from a LAN client whose first poll landed mid-reconnect. A
  // transient failure now retries quietly and only becomes visible after it
  // stops being transient. Whatever loaded stays on screen throughout.
  const [loadState, setLoadState] = useState({ failures: 0, message: "" });
  const failuresRef = useRef(0);
  const retryRef = useRef(null);

  const refresh = async () => {
    try {
      const [tplRes, schRes, genRes, statusRes] = await Promise.all([
        listReportTemplates(),
        listScheduledReports(),
        listGeneratedReports({ limit: 200 }),
        getReportSchedulerStatus().catch(() => null),
      ]);
      setTemplates(Array.isArray(tplRes?.templates) ? tplRes.templates : []);
      setSchedules(Array.isArray(schRes?.schedules) ? schRes.schedules : []);
      setGenerated(Array.isArray(genRes?.generated) ? genRes.generated : []);
      if (statusRes && typeof statusRes.any_gateway_running === "boolean") {
        setAnyGatewayRunning(statusRes.any_gateway_running);
      }
      failuresRef.current = 0;
      setLoadState((prev) => (prev.failures || prev.message ? { failures: 0, message: "" } : prev));
    } catch (e) {
      const n = failuresRef.current + 1;
      failuresRef.current = n;
      setLoadState({ failures: n, message: String(e?.message || e) });
      // 2s, 4s, 8s … capped at 30s. The 15s poll below keeps running, so this
      // only adds the fast early retries that ride out a brief hiccup.
      if (n <= 5) {
        const delay = Math.min(30000, 2000 * Math.pow(2, n - 1));
        if (retryRef.current) clearTimeout(retryRef.current);
        retryRef.current = setTimeout(() => { retryRef.current = null; refresh(); }, delay);
      }
      // Only shout once it is clearly not transient, and never more than once.
      if (n === 4) {
        onNotify({ type: "error", message: `Reports could not be loaded: ${e?.message || e}` });
      }
    }
  };

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => {
      clearInterval(timer);
      if (retryRef.current) clearTimeout(retryRef.current);
    };
  }, []);

  const tagListForGateway = (gatewayId) => {
    const raw = tagsByGateway?.[String(gatewayId || "")];
    return Array.isArray(raw) ? raw : [];
  };

  const loadSchedule = (sched) => {
    if (!sched) return;
    setDraft({
      ...emptySchedule(sched.template_id),
      ...sched,
      recipients: Array.isArray(sched.recipients) ? sched.recipients : [],
      tag_conditions: Array.isArray(sched.tag_conditions) ? sched.tag_conditions : [],
    });
    setRecipientsInput((sched.recipients || []).join(", "));
  };

  const handleNewSchedule = () => {
    const defaultTemplateId = templates[0]?.id || "";
    setDraft(emptySchedule(defaultTemplateId));
    setRecipientsInput("");
  };

  const handleSave = async () => {
    setBusy("save");
    try {
      const payload = {
        ...draft,
        recipients: recipientsInput
          .split(/[;,\n]+/)
          .map((s) => s.trim())
          .filter(Boolean),
      };
      const res = await saveScheduledReport(payload);
      const saved = res?.schedule;
      if (saved) loadSchedule(saved);
      await refresh();
      onNotify({ type: "success", message: "Schedule saved" });
    } catch (e) {
      onNotify({ type: "error", message: `Save failed: ${e?.message || e}` });
    }
    setBusy("");
  };

  const handleDelete = async () => {
    if (!draft.id) return;
    if (!window.confirm(`Delete schedule "${draft.name}"?`)) return;
    setBusy("delete");
    try {
      await deleteScheduledReport(draft.id);
      handleNewSchedule();
      await refresh();
      onNotify({ type: "success", message: "Schedule deleted" });
    } catch (e) {
      onNotify({ type: "error", message: `Delete failed: ${e?.message || e}` });
    }
    setBusy("");
  };

  const handleRunNow = async ({ force = false } = {}) => {
    if (!draft.id) {
      onNotify({ type: "error", message: "Save the schedule before running it." });
      return;
    }
    setBusy("run");
    try {
      const res = await runScheduledReport(draft.id, emailSettings, { force });
      await refresh();
      onNotify({
        type: "success",
        message: `Report generated. ${res?.email?.message ? `Email: ${res.email.message}` : ""}`,
      });
    } catch (e) {
      if (e?.code === "GATEWAY_REQUIRED") {
        const proceed = window.confirm(
          `${e.message}\n\nThe data shown in the report will reflect what was last collected.\n\nGenerate it anyway?`
        );
        if (proceed) {
          setBusy("");
          return handleRunNow({ force: true });
        }
        onNotify({ type: "error", message: "Run cancelled: gateway is not running." });
      } else {
        onNotify({ type: "error", message: `Run failed: ${e?.message || e}` });
      }
    }
    setBusy("");
  };

  // ---- generated-reports actions (icon-button row) -----------------------
  const handlePreviewGenerated = async (gid) => {
    try { await openPdfInTab(gid); }
    catch (e) { onNotify({ type: "error", message: `Preview failed: ${e?.message || e}` }); }
  };
  const handleDownloadGenerated = async (g) => {
    try {
      const blob = await downloadGeneratedReportBlob(g.id);
      downloadBlob(blob, g.file_name || `${(g.template_name || "report").replace(/\s+/g, "_")}.pdf`);
    } catch (e) {
      onNotify({ type: "error", message: `Download failed: ${e?.message || e}` });
    }
  };
  // Lightweight email modal: pick attachment formats + recipients without a
  // second window.prompt call. Stored in state so the user can tweak before
  // hitting "Send".
  const [emailDialog, setEmailDialog] = useState(null); // { generatedId, target, recipients, subject, pdf, csv, txt }
  const handleEmailGenerated = async (g) => {
    const target = generated.find((row) => row.id === g.id);
    if (!target) return;
    setEmailDialog({
      generatedId: g.id,
      target,
      recipientsText: "",
      subject: `Report: ${target.template_name || target.file_name}`,
      pdf: true,
      csv: false,
      txt: false,
      busy: false,
    });
  };
  const handleEmailDialogSend = async () => {
    if (!emailDialog) return;
    const recipients = (emailDialog.recipientsText || "")
      .split(/[;,\n]+/).map((s) => s.trim()).filter(Boolean);
    if (!recipients.length) {
      onNotify({ type: "error", message: "Provide at least one recipient." });
      return;
    }
    if (!(emailDialog.pdf || emailDialog.csv || emailDialog.txt)) {
      onNotify({ type: "error", message: "Select at least one attachment format." });
      return;
    }
    setEmailDialog((p) => p ? { ...p, busy: true } : p);
    try {
      const res = await emailGeneratedReport(emailDialog.generatedId, {
        recipients,
        subject: emailDialog.subject || `Report: ${emailDialog.target.template_name || emailDialog.target.file_name}`,
        htmlBody: `<p>Trustnode report attached: <b>${emailDialog.target.file_name}</b></p>`,
        emailSettings,
        attachPdf: !!emailDialog.pdf,
        attachCsv: !!emailDialog.csv,
        attachTxt: !!emailDialog.txt,
      });
      if (res?.ok) onNotify({ type: "success", message: `Email sent: ${res.message || ""}` });
      else onNotify({ type: "error", message: `Email failed: ${res?.message || "Unknown error"}` });
      await refresh();
      setEmailDialog(null);
    } catch (e) {
      onNotify({ type: "error", message: `Email failed: ${e?.message || e}` });
      setEmailDialog((p) => p ? { ...p, busy: false } : p);
    }
  };
  const handleDeleteGenerated = async (g) => {
    if (!window.confirm("Delete this generated report? The PDF file will be removed.")) return;
    try {
      await deleteGeneratedReport(g.id);
      await refresh();
    } catch (e) {
      onNotify({ type: "error", message: `Delete failed: ${e?.message || e}` });
    }
  };

  const filteredGenerated = useMemo(() => {
    if (!filterScheduleId) return generated;
    return generated.filter((g) => String(g.schedule_id || "") === String(filterScheduleId));
  }, [generated, filterScheduleId]);

  return (
    <div className="scheduled-reports-page">
      {loadState.failures > 0 ? (
        <div className={`status ${loadState.failures >= 4 ? "error" : "warn"}`} style={{ marginBottom: 8 }}>
          {loadState.failures >= 4
            ? `Reports are not loading (${loadState.message}). Retrying every 15 seconds.`
            : "Reconnecting to the reports service…"}
        </div>
      ) : null}
      {showSchedule && (
      <section className="tn-collapsible-card is-open scheduled-reports-card">
        <header className="tn-card-head">
          <div className="tn-card-head-text">
            <span className="tn-card-title">Scheduled report configuration</span>
            <span className="tn-card-subtitle">
              {schedules.length} saved schedule{schedules.length === 1 ? "" : "s"}
            </span>
          </div>
        </header>
        <div className="tn-card-body">
          <div className="scheduled-reports-grid">
            <aside className="scheduled-reports-list">
              <div className="scheduled-reports-list-head">
                <strong>Saved schedules</strong>
                <button type="button" className="btn btn-link" onClick={handleNewSchedule}>
                  <IconPlus /> New
                </button>
              </div>
              {schedules.length === 0 ? (
                <p className="muted">No schedules yet. Create one on the right.</p>
              ) : (
                <ul className="schedule-list">
                  {schedules.map((s) => (
                    <li
                      key={s.id}
                      className={`schedule-list-row ${String(draft.id) === String(s.id) ? "active" : ""}`}
                    >
                      <button
                        type="button"
                        className="schedule-list-btn"
                        onClick={() => loadSchedule(s)}
                      >
                        <div className="schedule-list-name">
                          <span className={`schedule-dot ${s.enabled ? "on" : "off"}`} />
                          {s.name}
                        </div>
                        <div className="schedule-list-meta">
                          {s.recurrence} {s.hour}:{String(s.minute).padStart(2, "0")} • {s.trigger_mode}
                          {s.deliver_email ? (() => {
                            const fmts = [];
                            if (s.attach_pdf !== false) fmts.push("PDF");
                            if (s.attach_csv) fmts.push("CSV");
                            if (s.attach_txt) fmts.push("TXT");
                            return ` • email ${fmts.join("+") || "—"}`;
                          })() : ""}
                          {s.require_gateway_running ? " • gw-gated" : ""}
                        </div>
                        {s.last_run_utc ? (
                          <div className="schedule-list-meta">Last: {s.last_run_utc} ({s.last_status || "ok"})</div>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </aside>

            <div className="scheduled-reports-editor">
              <h4 className="scheduled-reports-editor-title">
                {draft.id ? "Edit schedule" : "New schedule"}
              </h4>

              <div className="schedule-form-grid">
                <label>
                  Name
                  <input value={draft.name} onChange={(e) => setDraft((p) => ({ ...p, name: e.target.value }))} />
                </label>
                <label>
                  Template
                  <select
                    value={draft.template_id || ""}
                    onChange={(e) => setDraft((p) => ({ ...p, template_id: e.target.value }))}
                  >
                    <option value="">Select template…</option>
                    {templates.map((t) => (
                      <option key={t.id} value={t.id}>{t.name}</option>
                    ))}
                  </select>
                </label>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={!!draft.enabled}
                    onChange={(e) => setDraft((p) => ({ ...p, enabled: e.target.checked }))}
                  />
                  Enabled
                </label>
                <label>
                  Trigger mode
                  <select
                    value={draft.trigger_mode || "time"}
                    onChange={(e) => setDraft((p) => ({ ...p, trigger_mode: e.target.value }))}
                  >
                    {TRIGGER_MODES.map((m) => (
                      <option key={m.value} value={m.value}>{m.label}</option>
                    ))}
                  </select>
                </label>
                <label
                  className="checkbox-row"
                  title="When ticked, the scheduler skips this report whenever every configured PLC gateway is stopped, so you don't email empty PDFs on offline systems."
                >
                  <input
                    type="checkbox"
                    checked={!!draft.require_gateway_running}
                    onChange={(e) => setDraft((p) => ({ ...p, require_gateway_running: e.target.checked }))}
                  />
                  Only when a PLC gateway is running
                  <span
                    className={`gateway-live-badge ${anyGatewayRunning ? "is-running" : "is-stopped"}`}
                    title={anyGatewayRunning
                      ? "At least one PLC gateway is currently collecting."
                      : "No PLC gateway is currently collecting; gated schedules will be skipped."}
                  >
                    {anyGatewayRunning ? "live" : "stopped"}
                  </span>
                </label>
              </div>

              {(draft.trigger_mode === "time" || draft.trigger_mode === "both") ? (
                <fieldset className="schedule-fieldset">
                  <legend>Time trigger</legend>
                  <div className="schedule-form-grid">
                    <label>
                      Recurrence
                      <select
                        value={draft.recurrence || "daily"}
                        onChange={(e) => setDraft((p) => ({ ...p, recurrence: e.target.value }))}
                      >
                        {RECURRENCES.map((r) => (
                          <option key={r.value} value={r.value}>{r.label}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Hour (UTC)
                      <input
                        type="number" min={0} max={23}
                        value={draft.hour ?? 0}
                        onChange={(e) => setDraft((p) => ({ ...p, hour: Number(e.target.value || 0) }))}
                      />
                    </label>
                    <label>
                      Minute
                      <input
                        type="number" min={0} max={59}
                        value={draft.minute ?? 0}
                        onChange={(e) => setDraft((p) => ({ ...p, minute: Number(e.target.value || 0) }))}
                      />
                    </label>
                    {draft.recurrence === "weekly" ? (
                      <label>
                        Day of week
                        <select
                          value={String(draft.day_of_week ?? 0)}
                          onChange={(e) => setDraft((p) => ({ ...p, day_of_week: Number(e.target.value) }))}
                        >
                          {["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"].map((d, i) => (
                            <option key={d} value={i}>{d}</option>
                          ))}
                        </select>
                      </label>
                    ) : null}
                    {draft.recurrence === "monthly" ? (
                      <label>
                        Day of month
                        <input
                          type="number" min={1} max={28}
                          value={draft.day_of_month ?? 1}
                          onChange={(e) => setDraft((p) => ({ ...p, day_of_month: Number(e.target.value || 1) }))}
                        />
                      </label>
                    ) : null}
                  </div>
                </fieldset>
              ) : null}

              {(draft.trigger_mode === "tag" || draft.trigger_mode === "both") ? (
                <fieldset className="schedule-fieldset">
                  <legend>Tag conditions</legend>
                  <div className="schedule-form-grid">
                    <label>
                      Match logic
                      <select
                        value={draft.condition_logic || "all"}
                        onChange={(e) => setDraft((p) => ({ ...p, condition_logic: e.target.value }))}
                      >
                        {CONDITION_LOGICS.map((c) => (
                          <option key={c.value} value={c.value}>{c.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="tag-condition-list">
                    {(draft.tag_conditions || []).map((c, idx) => (
                      <div key={c.id || idx} className="tag-condition-row">
                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={c.enabled !== false}
                            onChange={(e) =>
                              setDraft((p) => ({
                                ...p,
                                tag_conditions: p.tag_conditions.map((cc, i) =>
                                  i === idx ? { ...cc, enabled: e.target.checked } : cc
                                ),
                              }))
                            }
                          />
                        </label>
                        <select
                          value={c.gateway_id || ""}
                          onChange={(e) =>
                            setDraft((p) => ({
                              ...p,
                              tag_conditions: p.tag_conditions.map((cc, i) =>
                                i === idx ? { ...cc, gateway_id: e.target.value, tag_name: "" } : cc
                              ),
                            }))
                          }
                        >
                          <option value="">Gateway…</option>
                          {gatewayOptions.map((g) => (
                            <option key={g.id} value={g.id}>{g.name || g.id}</option>
                          ))}
                        </select>
                        <select
                          value={c.tag_name || ""}
                          onChange={(e) =>
                            setDraft((p) => ({
                              ...p,
                              tag_conditions: p.tag_conditions.map((cc, i) =>
                                i === idx ? { ...cc, tag_name: e.target.value } : cc
                              ),
                            }))
                          }
                        >
                          <option value="">Tag…</option>
                          {tagListForGateway(c.gateway_id).map((t) => (
                            <option key={t} value={t}>{formatTagForDisplay(t)}</option>
                          ))}
                        </select>
                        <select
                          value={c.operator || "gt"}
                          onChange={(e) =>
                            setDraft((p) => ({
                              ...p,
                              tag_conditions: p.tag_conditions.map((cc, i) =>
                                i === idx ? { ...cc, operator: e.target.value } : cc
                              ),
                            }))
                          }
                        >
                          {RULE_OPERATORS.map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                          ))}
                        </select>
                        <input
                          value={c.value ?? c.value1 ?? ""}
                          placeholder="Threshold"
                          onChange={(e) =>
                            setDraft((p) => ({
                              ...p,
                              tag_conditions: p.tag_conditions.map((cc, i) =>
                                i === idx ? { ...cc, value: e.target.value } : cc
                              ),
                            }))
                          }
                        />
                        <button
                          type="button"
                          className="icon-btn icon-btn-danger"
                          title="Remove condition"
                          onClick={() =>
                            setDraft((p) => ({
                              ...p,
                              tag_conditions: p.tag_conditions.filter((_, i) => i !== idx),
                            }))
                          }
                        >
                          <IconTrash />
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn btn-link"
                      onClick={() =>
                        setDraft((p) => ({
                          ...p,
                          tag_conditions: [
                            ...(p.tag_conditions || []),
                            { id: makeId("cnd"), enabled: true, gateway_id: "", tag_name: "", operator: "gt", value: "" },
                          ],
                        }))
                      }
                    >
                      <IconPlus /> Add condition
                    </button>
                  </div>
                </fieldset>
              ) : null}

              <fieldset className="schedule-fieldset">
                <legend>Email delivery</legend>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={!!draft.deliver_email}
                    onChange={(e) => setDraft((p) => ({ ...p, deliver_email: e.target.checked }))}
                  />
                  Email when this schedule fires
                </label>
                {draft.deliver_email ? (
                  <>
                    <div className="schedule-attachments-row">
                      <span className="schedule-attachments-label">Attach:</span>
                      <label className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={draft.attach_pdf !== false}
                          onChange={(e) => setDraft((p) => ({ ...p, attach_pdf: e.target.checked }))}
                        />
                        PDF
                      </label>
                      <label className="checkbox-row" title="Bundles every chart + data-table section as a single CSV file.">
                        <input
                          type="checkbox"
                          checked={!!draft.attach_csv}
                          onChange={(e) => setDraft((p) => ({ ...p, attach_csv: e.target.checked }))}
                        />
                        CSV (raw data with timestamps)
                      </label>
                      <label className="checkbox-row" title="Pipe-delimited plain-text version of the CSV.">
                        <input
                          type="checkbox"
                          checked={!!draft.attach_txt}
                          onChange={(e) => setDraft((p) => ({ ...p, attach_txt: e.target.checked }))}
                        />
                        TXT
                      </label>
                    </div>
                    {!(draft.attach_pdf !== false || draft.attach_csv || draft.attach_txt) ? (
                      <p className="muted warning">Select at least one attachment format.</p>
                    ) : null}
                    <label>
                      Recipients (semicolon or comma separated)
                      <input
                        value={recipientsInput}
                        onChange={(e) => setRecipientsInput(e.target.value)}
                        placeholder="ops@example.com; ceo@example.com"
                      />
                    </label>
                    <label>
                      Subject (optional)
                      <input
                        value={draft.email_subject || ""}
                        onChange={(e) => setDraft((p) => ({ ...p, email_subject: e.target.value }))}
                        placeholder="Defaults to: Report: <template name>"
                      />
                    </label>
                    <label>
                      Body HTML (optional)
                      <textarea
                        rows={3}
                        value={draft.email_body || ""}
                        onChange={(e) => setDraft((p) => ({ ...p, email_body: e.target.value }))}
                        placeholder="<p>See attached PDF.</p>"
                      />
                    </label>
                    {!emailSettings?.smtp?.host && !emailSettings?.php_mail?.endpoint_url ? (
                      <p className="muted warning">
                        No email transport is configured yet. Set up SMTP or PHP relay under Email and Notifications first.
                      </p>
                    ) : null}
                  </>
                ) : null}
              </fieldset>

              <div className="scheduled-reports-actions">
                <button type="button" className="btn btn-primary" onClick={handleSave} disabled={busy === "save"}>
                  {busy === "save" ? "Saving…" : draft.id ? "Save changes" : "Create schedule"}
                </button>
                <button type="button" className="btn btn-success" onClick={() => handleRunNow()} disabled={busy === "run" || !draft.id}>
                  <IconPlay /> {busy === "run" ? "Running…" : "Run now"}
                </button>
                {draft.id ? (
                  <button type="button" className="btn btn-danger" onClick={handleDelete} disabled={busy === "delete"}>
                    <IconTrash /> Delete schedule
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      </section>
      )}

      {showGenerated && (
      <section className="tn-collapsible-card is-open generated-reports-card">
        <header className="tn-card-head">
          <div className="tn-card-head-text">
            <span className="tn-card-title">Generated reports</span>
            <span className="tn-card-subtitle">
              {generated.length} PDF{generated.length === 1 ? "" : "s"} on disk
            </span>
          </div>
          <div className="tn-card-head-actions">
            <label className="generated-reports-filter">
              <span>Filter</span>
              <select
                value={filterScheduleId}
                onChange={(e) => setFilterScheduleId(e.target.value)}
              >
                <option value="">All schedules</option>
                {schedules.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </label>
            <button type="button" className="icon-btn" onClick={refresh} title="Refresh list" aria-label="Refresh">
              <IconRefresh />
            </button>
            {logoSrc ? (
              <img
                src={logoSrc}
                alt="TrustNode"
                className="generated-reports-logo"
                onError={(e) => {
                  const img = e?.currentTarget;
                  if (img) img.style.display = "none";
                }}
              />
            ) : null}
          </div>
        </header>
        <div className="tn-card-body">
          {filteredGenerated.length === 0 ? (
            <p className="muted">No reports have been generated yet. Run a schedule or preview a template to see entries here.</p>
          ) : (
            <div className="generated-reports-tablewrap">
              <table className="generated-reports-table">
                <thead>
                  <tr>
                    <th>Generated</th>
                    <th>Template</th>
                    <th>Schedule</th>
                    <th>Trigger</th>
                    <th>Size</th>
                    <th>Email</th>
                    <th className="generated-reports-action-col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredGenerated.map((g) => (
                    <tr key={g.id}>
                      <td>{g.created_utc || "-"}</td>
                      <td>{g.template_name || g.template_id || "-"}</td>
                      <td>{g.schedule_name || "-"}</td>
                      <td>{g.triggered_by || "-"}</td>
                      <td>{Math.round((g.file_bytes || 0) / 1024)} KB</td>
                      <td>{g.email_status || "-"}</td>
                      <td className="generated-reports-action-col">
                        <div className="generated-reports-actions-row">
                          <button
                            type="button"
                            className="icon-btn"
                            title="Preview in new tab"
                            aria-label="Preview"
                            onClick={() => handlePreviewGenerated(g.id)}
                          >
                            <IconEye />
                          </button>
                          <button
                            type="button"
                            className="icon-btn"
                            title="Download PDF"
                            aria-label="Download"
                            onClick={() => handleDownloadGenerated(g)}
                          >
                            <IconDownload />
                          </button>
                          <button
                            type="button"
                            className="icon-btn"
                            title="Email this report"
                            aria-label="Email"
                            onClick={() => handleEmailGenerated(g)}
                          >
                            <IconMail />
                          </button>
                          <button
                            type="button"
                            className="icon-btn icon-btn-danger"
                            title="Delete this report"
                            aria-label="Delete"
                            onClick={() => handleDeleteGenerated(g)}
                          >
                            <IconTrash />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
      )}

      {emailDialog ? (
        <div
          className="modal-backdrop"
          onClick={() => { if (!emailDialog.busy) setEmailDialog(null); }}
        >
          <div
            className="modal-card schedule-email-dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <h3>Email report</h3>
            <p className="muted small">
              Sending <strong>{emailDialog.target.template_name || emailDialog.target.file_name}</strong>
              {" "}({Math.round((emailDialog.target.file_bytes || 0) / 1024)} KB PDF)
            </p>
            <label>
              Recipients (semicolon or comma separated)
              <input
                value={emailDialog.recipientsText}
                onChange={(e) => setEmailDialog((p) => ({ ...p, recipientsText: e.target.value }))}
                placeholder="ops@example.com; ceo@example.com"
                autoFocus
              />
            </label>
            <label>
              Subject
              <input
                value={emailDialog.subject}
                onChange={(e) => setEmailDialog((p) => ({ ...p, subject: e.target.value }))}
              />
            </label>
            <div className="schedule-attachments-row">
              <span className="schedule-attachments-label">Attach:</span>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={!!emailDialog.pdf}
                  onChange={(e) => setEmailDialog((p) => ({ ...p, pdf: e.target.checked }))}
                />
                PDF
              </label>
              <label className="checkbox-row" title="Rebuilds from the source template — every chart + data table as one CSV.">
                <input
                  type="checkbox"
                  checked={!!emailDialog.csv}
                  onChange={(e) => setEmailDialog((p) => ({ ...p, csv: e.target.checked }))}
                />
                CSV (raw data with timestamps)
              </label>
              <label className="checkbox-row" title="Pipe-delimited plain-text version of the CSV.">
                <input
                  type="checkbox"
                  checked={!!emailDialog.txt}
                  onChange={(e) => setEmailDialog((p) => ({ ...p, txt: e.target.checked }))}
                />
                TXT
              </label>
            </div>
            <div className="schedule-email-dialog-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setEmailDialog(null)}
                disabled={emailDialog.busy}
              >Cancel</button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleEmailDialogSend}
                disabled={emailDialog.busy}
              >
                {emailDialog.busy ? "Sending…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
