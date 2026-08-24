/* Generic EtherNet/IP device — EDS import and assembly mapping.

   Rendered ONLY when gateway_type === "ethernet_ip".

   This is the CODESYS / ifm AE3100 workflow: import the device's EDS, pick the
   input assembly, then map byte offsets to named signals. The edge plays the
   originator role, so no PLC sits between us and the device.

   Byte offsets are the part an operator gets wrong, and a wrong offset produces
   a plausible number rather than an error — so "Read live assembly" shows the
   raw bytes beside the decoded values before anything is saved. */
import { useCallback, useMemo, useState } from "react";
import { parseEdsFile, previewEipAssembly, identifyEipDevice } from "../../api";

const CIP_TYPES = ["BOOL", "SINT", "USINT", "BYTE", "INT", "UINT", "WORD",
                   "DINT", "UDINT", "DWORD", "REAL", "LINT", "LREAL"];

export function eipTagNames(signals) {
  return Array.from(new Set(
    (signals || []).map((s) => String(s?.name || "").trim()).filter(Boolean)
  ));
}

export default function EthernetIpMapper({ form, onChange, disabled = false }) {
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [eds, setEds] = useState(null);
  const [live, setLive] = useState(null);

  const signals = useMemo(
    () => (Array.isArray(form.eip_signals) ? form.eip_signals : []),
    [form.eip_signals]
  );

  const patchSignals = useCallback((next) => {
    onChange({ eip_signals: next, tags_text: eipTagNames(next).join(";") });
  }, [onChange]);

  const importEds = useCallback(async (text) => {
    setBusy("eds"); setNote("");
    try {
      const res = await parseEdsFile(text);
      setEds(res);
      setNote(res?.message || "");
      if (res?.ok) {
        onChange({
          eip_input_assembly: Number(res.suggested?.input_assembly || form.eip_input_assembly || 0),
          eip_output_assembly: Number(res.suggested?.output_assembly || 0),
          eip_config_assembly: Number(res.suggested?.config_assembly || 0),
          eip_device_info: res.device || {},
        });
      }
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [onChange, form.eip_input_assembly]);

  const onFile = useCallback((file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => importEds(String(reader.result || ""));
    reader.readAsText(file);
  }, [importEds]);

  const readLive = useCallback(async () => {
    setBusy("live"); setNote("");
    try {
      const res = await previewEipAssembly({
        plc_ip: String(form.plc_ip || "").trim(),
        instance: Number(form.eip_input_assembly || 0),
        slot: Number(form.eip_slot || 0),
        signals,
      });
      setLive(res);
      if (!res?.ok) setNote(res?.message || "Could not read the assembly.");
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.eip_input_assembly, form.eip_slot, signals]);

  const identify = useCallback(async () => {
    setBusy("id"); setNote("");
    try {
      const res = await identifyEipDevice({ plc_ip: String(form.plc_ip || "").trim(),
                                            slot: Number(form.eip_slot || 0) });
      setNote(res?.message || "");
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.eip_slot]);

  const patchSignal = (idx, patch) =>
    patchSignals(signals.map((s, i) => (i === idx ? { ...s, ...patch } : s)));

  return (
    <div className="gateway-span-2 ifm-panel">
      <div className="muted ifm-help">
        Read any EtherNet/IP device directly — an IO-Link block, remote I/O, a drive —
        with no PLC in between. Import its EDS to discover the assemblies, then map the
        bytes you want. They become ordinary tags and trend like any PLC tag.
      </div>

      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <label className="btn btn-primary btn-sm" style={{ margin: 0 }}>
          {busy === "eds" ? "Reading…" : "Import EDS file"}
          <input type="file" accept=".eds,text/plain" style={{ display: "none" }}
            disabled={disabled}
            onChange={(e) => onFile(e.target.files && e.target.files[0])} />
        </label>
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled || !!busy}
          onClick={identify}>
          {busy === "id" ? "Asking…" : "Identify device"}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled || !!busy}
          onClick={readLive}>
          {busy === "live" ? "Reading…" : "Read live assembly"}
        </button>
      </div>

      {note ? <div className="info-note">{note}</div> : null}

      {eds?.device ? (
        <div className="muted" style={{ fontSize: 12 }}>
          EDS: <strong>{eds.device.product_name || "?"}</strong>
          {eds.device.vendor_name ? ` · ${eds.device.vendor_name}` : ""}
          {(eds.device.assemblies || []).length
            ? ` · assemblies: ${(eds.device.assemblies || [])
                .map((a) => `${a.instance} (${a.name || "?"}, ${a.size_bytes}B)`).join(", ")}`
            : ""}
        </div>
      ) : null}

      <div className="ifm-conn-grid">
        <label>
          Input assembly
          <input type="number" min="0" value={form.eip_input_assembly ?? 0} disabled={disabled}
            onChange={(e) => onChange({ eip_input_assembly: Number(e.target.value || 0) })} />
          <small className="hint">The instance that carries the data you want to read.</small>
        </label>
        <label>
          Output assembly
          <input type="number" min="0" value={form.eip_output_assembly ?? 0} disabled={disabled}
            onChange={(e) => onChange({ eip_output_assembly: Number(e.target.value || 0) })} />
          <small className="hint">Recorded only — this driver never writes.</small>
        </label>
        <label>
          Slot
          <input type="number" min="0" value={form.eip_slot ?? 0} disabled={disabled}
            onChange={(e) => onChange({ eip_slot: Number(e.target.value || 0) })} />
        </label>
      </div>

      {live ? (
        <div className={`info-note ifm-preview ${live.ok ? "" : "warn"}`}>
          {live.ok ? (
            <>
              <div>Assembly returned <strong>{live.size_bytes}</strong> bytes:{" "}
                <code>{String(live.raw || "").slice(0, 96)}{String(live.raw || "").length > 96 ? "…" : ""}</code>
              </div>
              <div style={{ marginTop: 4 }}>
                {(live.values || []).map((v, i) => (
                  <span key={i} className="ifm-preview-val">
                    <strong>{v.name}</strong>{" = "}
                    {v.error ? <em>{v.error}</em> : `${v.value} ${v.unit || ""}`}
                  </span>
                ))}
              </div>
            </>
          ) : (live.message || "Could not read the assembly.")}
        </div>
      ) : null}

      <div className="ifm-field-table">
        <div className="ifm-field-head eip-head">
          <span>Tag name</span><span>Byte offset</span><span>Type</span>
          <span>Bit</span><span>Scale</span><span>Offset</span><span>Unit</span><span />
        </div>
        {signals.map((sig, idx) => (
          <div className="ifm-field-row eip-row" key={`eip-${idx}`}>
            <input value={sig.name || ""} disabled={disabled}
              onChange={(e) => patchSignal(idx, { name: e.target.value })} />
            <input type="number" min="0" value={sig.byte_offset ?? 0} disabled={disabled}
              onChange={(e) => patchSignal(idx, { byte_offset: Number(e.target.value || 0) })} />
            <select value={sig.kind || "INT"} disabled={disabled}
              onChange={(e) => patchSignal(idx, { kind: e.target.value })}>
              {CIP_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <input type="number" min="0" max="7" value={sig.bit ?? 0}
              disabled={disabled || String(sig.kind || "INT") !== "BOOL"}
              onChange={(e) => patchSignal(idx, { bit: Number(e.target.value || 0) })} />
            <input type="number" step="any" value={sig.scale ?? 1} disabled={disabled}
              onChange={(e) => patchSignal(idx, { scale: Number(e.target.value || 1) })} />
            <input type="number" step="any" value={sig.offset ?? 0} disabled={disabled}
              onChange={(e) => patchSignal(idx, { offset: Number(e.target.value || 0) })} />
            <input value={sig.unit || ""} placeholder="rpm" disabled={disabled}
              onChange={(e) => patchSignal(idx, { unit: e.target.value })} />
            <button type="button" className="btn btn-danger btn-sm" disabled={disabled}
              onClick={() => patchSignals(signals.filter((_, i) => i !== idx))}>Remove</button>
          </div>
        ))}
        <button type="button" className="btn btn-secondary btn-sm" style={{ marginTop: 6 }}
          disabled={disabled}
          onClick={() => patchSignals([...signals, {
            name: `Signal${signals.length + 1}`, byte_offset: 0, kind: "INT",
            bit: 0, scale: 1, offset: 0, unit: "",
          }])}>
          Add signal
        </button>
      </div>
    </div>
  );
}
