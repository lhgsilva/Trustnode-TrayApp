import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import DataSourceToggle from "./DataSourceToggle.jsx";

// --- Friendly schedule picker -----------------------------------------------
// Builds a cron expression from a few familiar choices (frequency + time +
// optional day-of-week / day-of-month). Falls back to a "Custom cron"
// text field for power users who want to type raw cron.
//
// Cron field order (5 fields): minute  hour  day-of-month  month  day-of-week
// day-of-week uses 0=Sun … 6=Sat (matches the parser in insight_scheduler.py).

const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function buildCron({ mode, time, dayOfWeek, dayOfMonth, everyN }) {
  const [hh, mm] = (time || "08:00").split(":").map((s) => parseInt(s, 10) || 0);
  switch (mode) {
    case "off":     return "";
    case "hourly":  return `0 * * * *`;
    case "every_n_minutes": {
      const n = Math.max(1, Math.min(59, Number(everyN) || 5));
      return `*/${n} * * * *`;
    }
    case "every_n_hours": {
      const n = Math.max(1, Math.min(23, Number(everyN) || 1));
      return `${mm} */${n} * * *`;
    }
    case "daily":   return `${mm} ${hh} * * *`;
    case "weekly":  return `${mm} ${hh} * * ${dayOfWeek != null ? dayOfWeek : 1}`;
    case "monthly": {
      const d = Math.max(1, Math.min(31, Number(dayOfMonth) || 1));
      return `${mm} ${hh} ${d} * *`;
    }
    case "custom":  return "";  // caller uses the raw text field
    default:        return "";
  }
}

function parseCronToPickerState(cron) {
  // Best-effort: recognise the shapes we emit above. Anything else
  // falls into 'custom' so the user can edit the raw text.
  const s = String(cron || "").trim();
  if (!s) return { mode: "off", time: "08:00", dayOfWeek: 1, dayOfMonth: 1, everyN: 5 };
  const parts = s.split(/\s+/);
  if (parts.length !== 5) return { mode: "custom", time: "08:00", dayOfWeek: 1, dayOfMonth: 1, everyN: 5 };
  const [m, h, dom, mon, dow] = parts;
  const time = (h !== "*" && !h.startsWith("*/") && m !== "*")
    ? `${String(parseInt(h, 10) || 0).padStart(2, "0")}:${String(parseInt(m, 10) || 0).padStart(2, "0")}`
    : "08:00";
  if (m === "0" && h === "*" && dom === "*" && mon === "*" && dow === "*") return { mode: "hourly", time, dayOfWeek: 1, dayOfMonth: 1, everyN: 5 };
  if (/^\*\/(\d+)$/.test(m) && h === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { mode: "every_n_minutes", time, dayOfWeek: 1, dayOfMonth: 1, everyN: parseInt(m.split("/")[1], 10) || 5 };
  }
  if (/^\d+$/.test(m) && /^\*\/(\d+)$/.test(h) && dom === "*" && mon === "*" && dow === "*") {
    return { mode: "every_n_hours", time, dayOfWeek: 1, dayOfMonth: 1, everyN: parseInt(h.split("/")[1], 10) || 1 };
  }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && dow === "*") return { mode: "daily", time, dayOfWeek: 1, dayOfMonth: 1, everyN: 5 };
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && /^\d+$/.test(dow)) return { mode: "weekly", time, dayOfWeek: parseInt(dow, 10) || 0, dayOfMonth: 1, everyN: 5 };
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && /^\d+$/.test(dom) && mon === "*" && dow === "*") return { mode: "monthly", time, dayOfWeek: 1, dayOfMonth: parseInt(dom, 10) || 1, everyN: 5 };
  return { mode: "custom", time, dayOfWeek: 1, dayOfMonth: 1, everyN: 5 };
}

function SchedulePicker({ cron, onChange, inp, lbl }) {
  // pickerState is the structured form; cron is the source of truth
  // exposed to the parent form. We re-parse cron into picker fields
  // whenever cron changes from outside (e.g. editing an existing insight).
  const [picker, setPicker] = useState(() => parseCronToPickerState(cron));
  const [rawCron, setRawCron] = useState(cron || "");

  useEffect(() => {
    setPicker(parseCronToPickerState(cron));
    setRawCron(cron || "");
  }, [cron]);

  const update = (patch) => {
    const next = { ...picker, ...patch };
    setPicker(next);
    if (next.mode === "custom") {
      // keep current rawCron; user edits in the text box
      return;
    }
    const cronOut = buildCron(next);
    setRawCron(cronOut);
    onChange(cronOut);
  };

  const previewHuman = useMemo(() => {
    const c = picker.mode === "custom" ? rawCron : buildCron(picker);
    if (!c) return "Never (schedule disabled)";
    switch (picker.mode) {
      case "hourly":          return "At the top of every hour";
      case "every_n_minutes": return `Every ${picker.everyN} minute${picker.everyN === 1 ? "" : "s"}`;
      case "every_n_hours":   return `Every ${picker.everyN} hour${picker.everyN === 1 ? "" : "s"} at minute ${picker.time.split(":")[1]}`;
      case "daily":           return `Every day at ${picker.time}`;
      case "weekly":          return `Every ${DOW_LABELS[picker.dayOfWeek]} at ${picker.time}`;
      case "monthly":         return `Day ${picker.dayOfMonth} of every month at ${picker.time}`;
      default:                return `Custom: ${c}`;
    }
  }, [picker, rawCron]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <label style={lbl}>Schedule</label>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select
          value={picker.mode}
          onChange={(e) => update({ mode: e.target.value })}
          style={{ ...inp, padding: "6px 8px", width: "auto", flex: "0 0 auto" }}
        >
          <option value="off">Off (no schedule)</option>
          <option value="every_n_minutes">Every N minutes</option>
          <option value="hourly">Every hour</option>
          <option value="every_n_hours">Every N hours</option>
          <option value="daily">Daily at…</option>
          <option value="weekly">Weekly on…</option>
          <option value="monthly">Monthly on…</option>
          <option value="custom">Custom cron…</option>
        </select>

        {(picker.mode === "every_n_minutes" || picker.mode === "every_n_hours") ? (
          <input
            type="number" min={1} max={picker.mode === "every_n_minutes" ? 59 : 23}
            value={picker.everyN}
            onChange={(e) => update({ everyN: e.target.value })}
            style={{ ...inp, width: 72, padding: "6px 8px" }}
          />
        ) : null}

        {(picker.mode === "daily" || picker.mode === "weekly" || picker.mode === "monthly" || picker.mode === "every_n_hours") ? (
          <input
            type="time" value={picker.time}
            onChange={(e) => update({ time: e.target.value || "08:00" })}
            style={{ ...inp, width: 110, padding: "6px 8px" }}
          />
        ) : null}

        {picker.mode === "weekly" ? (
          <select
            value={picker.dayOfWeek}
            onChange={(e) => update({ dayOfWeek: parseInt(e.target.value, 10) })}
            style={{ ...inp, padding: "6px 8px", width: "auto" }}
          >
            {DOW_LABELS.map((d, i) => <option key={i} value={i}>{d}</option>)}
          </select>
        ) : null}

        {picker.mode === "monthly" ? (
          <select
            value={picker.dayOfMonth}
            onChange={(e) => update({ dayOfMonth: parseInt(e.target.value, 10) })}
            style={{ ...inp, padding: "6px 8px", width: "auto" }}
          >
            {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
              <option key={d} value={d}>Day {d}</option>
            ))}
          </select>
        ) : null}
      </div>

      {picker.mode === "custom" ? (
        <input
          style={inp}
          value={rawCron}
          placeholder="m h dom mon dow   e.g. 0 8 * * *  (every day at 08:00)"
          onChange={(e) => { setRawCron(e.target.value); onChange(e.target.value); }}
        />
      ) : null}

      <small style={{ fontSize: 11, color: "var(--muted)" }}>
        {previewHuman}
        {picker.mode !== "off" && picker.mode !== "custom" ? (
          <span style={{ marginLeft: 8, opacity: 0.7 }}>
            cron: <code>{buildCron(picker)}</code>
          </span>
        ) : null}
      </small>
    </div>
  );
}

/* Modal editor for insights. Used by both the Insights page (creating
   scheduled queries from scratch) and the Chat page (one-click
   "Save as insight" from an answer).

   Rendered via React Portal to document.body so the modal escapes any
   ancestor with `transform`, `contain`, or `overflow: hidden` (the
   chat card sets overflow:hidden which previously clipped the modal).

   Props:
     initial: { title, description, prompt, tool_plan, data_source,
                schedule_cron, email_to }
     onSave({...form values}) → called when user clicks Save
     onCancel() → called when user clicks Cancel
*/
export function InsightEditor({ initial, onSave, onCancel }) {
  const [form, setForm] = useState({
    title: initial?.title || "",
    description: initial?.description || "",
    prompt: initial?.prompt || "",
    data_source: initial?.data_source || "local",
    schedule_cron: initial?.schedule_cron || "",
    email_to: initial?.email_to || "",
    tool_plan_raw: JSON.stringify(initial?.tool_plan || [], null, 2),
  });
  const [saving, setSaving] = useState(false);
  // Advanced (technical) fields — prompt + tool plan — are hidden by default.
  // They're auto-captured when saving from a chat answer, so a normal user
  // never needs to see JSON. Power users can expand to inspect/tweak.
  const [showAdvanced, setShowAdvanced] = useState(false);

  // How many data lookups this insight will re-run (for the friendly summary).
  const toolCount = (() => {
    try {
      const tp = JSON.parse(form.tool_plan_raw || "[]");
      return Array.isArray(tp) ? tp.length : 0;
    } catch { return 0; }
  })();

  const fld = { display: "flex", flexDirection: "column", gap: 4, marginBottom: 14 };
  const lbl = { fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600 };
  const inp = {
    padding: 9, borderRadius: 6,
    background: "var(--bg)", border: "1px solid var(--stroke)",
    color: "var(--text)", fontSize: 13, fontFamily: "inherit",
  };

  const doSave = async () => {
    if (saving) return;
    let tool_plan = [];
    try { tool_plan = JSON.parse(form.tool_plan_raw || "[]"); }
    catch { setShowAdvanced(true); alert("The saved query is invalid. Expand Advanced to fix it."); return; }
    if (!Array.isArray(tool_plan)) { setShowAdvanced(true); alert("The saved query must be a list."); return; }
    if (!form.title.trim()) { alert("Please give this insight a name."); return; }
    // Prompt is auto-filled from the chat; if somehow empty, use the title.
    const prompt = form.prompt.trim() || `Re-run and summarize: ${form.title.trim()}`;
    setSaving(true);
    // Operator 2026-07-02: AWAIT onSave and ALWAYS reset the busy state.
    // Previously onSave was fired without await, so if the parent's save
    // failed (or was slow), the button stuck on "Saving…" forever. Now the
    // editor recovers on failure and lets the parent unmount on success.
    try {
      const ok = await onSave({
        title: form.title.trim(), description: form.description,
        prompt, tool_plan,
        data_source: form.data_source, schedule_cron: form.schedule_cron,
        email_to: form.email_to,
      });
      if (ok === false) setSaving(false);   // failed → let the user retry
      // ok === true → parent unmounts this editor; nothing to reset.
    } catch (e) {
      setSaving(false);
    }
  };

  const node = (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget && !saving) onCancel && onCancel(); }}
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
          borderRadius: 10, padding: 22,
          width: "100%", maxWidth: 560, maxHeight: "90vh",
          overflow: "auto",
          boxShadow: "0 12px 40px rgba(0,0,0,0.45)",
        }}
      >
        <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 4 }}>
          {initial?.id ? "Edit insight" : "Save as insight"}
        </div>
        <div style={{ fontSize: 12.5, color: "var(--muted)", marginBottom: 18, lineHeight: 1.5 }}>
          Save this analysis so you can re-run it any time — optionally on a
          schedule with the results emailed to your team.
        </div>

        <div style={fld}>
          <label style={lbl}>Name</label>
          <input style={inp} value={form.title} autoFocus
                 placeholder="e.g. Tank A level — daily trend"
                 onChange={(e) => setForm({ ...form, title: e.target.value })}
                 onKeyDown={(e) => { if (e.key === "Enter") doSave(); }} />
        </div>

        <div style={fld}>
          <label style={lbl}>Notes (optional)</label>
          <input style={inp} value={form.description}
                 placeholder="What this insight is for"
                 onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </div>

        <div style={fld}>
          {/* Friendly schedule picker. Off by default → a one-off saved
              insight the user re-runs manually. */}
          <SchedulePicker
            cron={form.schedule_cron}
            onChange={(c) => setForm((f) => ({ ...f, schedule_cron: c }))}
            inp={inp}
            lbl={lbl}
          />
        </div>

        {form.schedule_cron ? (
          <div style={fld}>
            <label style={lbl}>Email results to (optional)</label>
            <input style={inp} value={form.email_to}
                   placeholder="name@company.com, other@company.com"
                   onChange={(e) => setForm({ ...form, email_to: e.target.value })} />
            <small style={{ fontSize: 11, color: "var(--muted)" }}>
              Comma-separated. Each scheduled run emails a PDF of the result.
            </small>
          </div>
        ) : null}

        {/* Friendly summary of what will run — no JSON. */}
        <div style={{
          fontSize: 12, color: "var(--muted)",
          background: "color-mix(in srgb, var(--teal, #14a89a) 8%, transparent)",
          border: "1px solid var(--stroke)", borderRadius: 6,
          padding: "8px 10px", marginBottom: 14,
        }}>
          Runs {toolCount || 1} data lookup{toolCount === 1 ? "" : "s"} against the{" "}
          {form.data_source === "cloud" ? "Cloud" : "Local"} database, then writes
          an engineering-style summary.
        </div>

        {/* Advanced (technical) — collapsed by default. */}
        <button
          type="button"
          onClick={() => setShowAdvanced((v) => !v)}
          style={{
            background: "transparent", border: "none", color: "var(--teal, #14a89a)",
            fontSize: 12, cursor: "pointer", padding: "2px 0", marginBottom: showAdvanced ? 10 : 0,
          }}
        >
          {showAdvanced ? "▾ Hide advanced" : "▸ Advanced (edit query / prompt)"}
        </button>

        {showAdvanced ? (
          <div style={{ borderTop: "1px solid var(--stroke)", paddingTop: 12 }}>
            <div style={fld}>
              <label style={lbl}>Data source</label>
              <DataSourceToggle value={form.data_source} onChange={(v) => setForm({ ...form, data_source: v })} />
            </div>
            <div style={fld}>
              <label style={lbl}>Summary prompt</label>
              <textarea style={{ ...inp, minHeight: 70, resize: "vertical" }} value={form.prompt}
                        placeholder="How the AI should narrate the results"
                        onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
            </div>
            <div style={fld}>
              <label style={lbl}>Saved query (advanced)</label>
              <textarea
                style={{ ...inp, minHeight: 120, fontFamily: "monospace", resize: "vertical", fontSize: 12 }}
                value={form.tool_plan_raw}
                onChange={(e) => setForm({ ...form, tool_plan_raw: e.target.value })}
              />
              <small style={{ fontSize: 11, color: "var(--muted)" }}>
                The data lookups captured from your chat answer. Only edit if you
                know the tool format.
              </small>
            </div>
          </div>
        ) : null}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
          <button className="btn btn-secondary" onClick={onCancel} disabled={saving}
                  style={{ padding: "9px 16px", fontSize: 13 }}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={doSave}
            disabled={saving || !form.title.trim()}
            style={{ padding: "9px 16px", fontSize: 13, fontWeight: 600 }}
          >
            {saving ? "Saving…" : "Save insight"}
          </button>
        </div>
      </div>
    </div>
  );

  if (typeof document === "undefined") return node;
  return createPortal(node, document.body);
}

export default InsightEditor;
