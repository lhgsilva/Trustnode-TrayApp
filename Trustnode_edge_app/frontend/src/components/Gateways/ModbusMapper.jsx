/* Generic Modbus TCP device — register mapping.

   Rendered ONLY when gateway_type === "modbus_tcp".

   Modbus is the widest-reach protocol in industry, and also the easiest to get
   silently wrong: a wrong address, format or word order returns a plausible
   NUMBER rather than an error. So this panel does what the EtherNet/IP mapper
   does — "Read live" shows the raw registers beside the decoded value before
   anything is saved — and it shares the address parser with the power-meter
   path, where typing the datasheet's printed "30005" and reading offset 30005
   left a row at "-" for weeks.

   Addresses may be written the way the datasheet prints them:
     30001 / 40001   1-based 3x / 4x references
     3x:70 / 4x:100  explicit offsets
     0x1E            the hex start-address column
     70              a plain offset, unchanged */
import { useCallback, useMemo, useState } from "react";
import { powerParseRegisterTable, previewModbusRegisters } from "../../api";
import DeviceCatalogue from "./DeviceCatalogue";

const KINDS = ["float32", "int16", "uint16", "int32", "uint32",
               "int64", "uint64", "float64", "bool"];
const FUNCTIONS = [
  ["input", "Input (3x, FC4)"],
  ["holding", "Holding (4x, FC3)"],
  ["discrete", "Discrete input (1x, FC2)"],
  ["coil", "Coil (0x, FC1)"],
];

/* Only the TICKED registers become collected tags. An unticked row keeps its
   address, format and scale so it can be turned back on without re-importing
   the supplier's table. `enabled === undefined` counts as ticked. */
export function modbusTagNames(registers) {
  return Array.from(new Set(
    (registers || [])
      .filter((r) => r && r.enabled !== false)
      .map((r) => String(r?.name || "").trim())
      .filter(Boolean)
  ));
}

export default function ModbusMapper({ form, onChange, disabled = false }) {
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [live, setLive] = useState(null);

  const registers = useMemo(
    () => (Array.isArray(form.modbus_registers) ? form.modbus_registers : []),
    [form.modbus_registers]
  );

  const patchRegisters = useCallback((next) => {
    onChange({ modbus_registers: next, tags_text: modbusTagNames(next).join(";") });
  }, [onChange]);

  const patchRow = (idx, patch) =>
    patchRegisters(registers.map((r, i) => (i === idx ? { ...r, ...patch } : r)));

  const ticked = useMemo(
    () => registers.filter((r) => r && r.enabled !== false).length,
    [registers]
  );

  const setAllTicks = useCallback((on) => {
    patchRegisters(registers.map((r) => ({ ...r, enabled: on })));
  }, [registers, patchRegisters]);

  /* Import the supplier's register list. Reuses the parser built for power
     meters — a Weidmüller table and a VSD table are the same shape, and a
     second parser would be a second thing to keep correct. */
  const importTable = useCallback(async (text) => {
    setBusy("import"); setNote("");
    try {
      const res = await powerParseRegisterTable(text);
      const parsed = res?.registers || {};
      const rows = Object.entries(parsed).map(([name, address]) => ({
        name, address: String(address), function: "input", kind: "float32",
        scale: 1, offset: 0, unit: "", enabled: false,
      }));
      if (!rows.length) {
        setNote(res?.message || "No registers were recognised in that table.");
        return;
      }
      // Imported UNticked: a supplier table is often the whole address space,
      // and collecting all of it is how a historian fills a disk.
      const known = new Set(registers.map((r) => String(r.name)));
      const added = rows.filter((r) => !known.has(r.name));
      patchRegisters([...registers, ...added]);
      setNote(`Added ${added.length} register(s), all unticked — tick the ones `
        + `you want to collect, then use "Read live" to confirm the values.`);
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [registers, patchRegisters]);

  const onFile = useCallback((file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => importTable(String(reader.result || ""));
    reader.readAsText(file);
  }, [importTable]);

  const readLive = useCallback(async () => {
    setBusy("live"); setNote("");
    try {
      const res = await previewModbusRegisters({
        plc_ip: String(form.plc_ip || "").trim(),
        port: Number(form.modbus_port || 502),
        unit_id: Number(form.modbus_unit_id || 1),
        registers,
      });
      setLive(res);
      if (!res?.ok) setNote(res?.message || "Could not read the device.");
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.modbus_port, form.modbus_unit_id, registers]);

  return (
    <div className="gateway-span-2 ifm-panel">
      <div className="muted ifm-help">
        Read any Modbus TCP device directly — a VSD, a transmitter, a weighing
        controller, or a gateway fronting another fieldbus. Registers become
        ordinary tags and trend like any PLC tag. Addresses can be typed the way
        the datasheet prints them (30001, 4x:100, 0x1E) — they are converted to
        wire offsets for you.
      </div>

      <DeviceCatalogue
        protocol="modbus_tcp"
        disabled={disabled}
        onApply={(profile) => {
          const d = profile.defaults || {};
          // An unverified profile arrives UNTICKED: its addresses are a
          // hypothesis until they have been read live and agreed with.
          const rows = (profile.tags || []).map((t) => ({
            ...t, enabled: Boolean(profile.verified),
          }));
          onChange({
            modbus_port: Number(d.modbus_port || form.modbus_port || 502),
            modbus_unit_id: Number(d.modbus_unit_id || form.modbus_unit_id || 1),
            modbus_registers: rows,
            tags_text: modbusTagNames(rows).join(";"),
          });
        }}
      />

      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <label className="btn btn-primary btn-sm" style={{ margin: 0 }}>
          {busy === "import" ? "Reading…" : "Import register table"}
          <input type="file" accept=".csv,.txt,text/plain" style={{ display: "none" }}
            disabled={disabled}
            onChange={(e) => onFile(e.target.files && e.target.files[0])} />
        </label>
        <button type="button" className="btn btn-secondary btn-sm"
          disabled={disabled || !!busy || !registers.length} onClick={readLive}
          title="Read the device now and show the raw registers beside the decoded values">
          {busy === "live" ? "Reading…" : "Read live values"}
        </button>
      </div>

      {note ? <div className="info-note">{note}</div> : null}

      <div className="ifm-conn-grid">
        <label>
          Port
          <input type="number" min="1" max="65535" value={form.modbus_port ?? 502}
            disabled={disabled}
            onChange={(e) => onChange({ modbus_port: Number(e.target.value || 502) })} />
          <small className="hint">502 unless the device was changed.</small>
        </label>
        <label>
          Unit / slave id
          <input type="number" min="0" max="255" value={form.modbus_unit_id ?? 1}
            disabled={disabled}
            onChange={(e) => onChange({ modbus_unit_id: Number(e.target.value || 1) })} />
          <small className="hint">1 for most devices; gateways use it to pick the slave.</small>
        </label>
      </div>

      {live ? (
        <div className={`info-note ifm-preview ${live.ok ? "" : "warn"}`}>
          {live.ok ? (
            <div>
              {(live.values || []).map((v, i) => (
                <span key={i} className="ifm-preview-val">
                  <strong>{v.name}</strong>{" = "}
                  {v.error
                    ? <em>{v.error}</em>
                    : `${v.value}${v.unit ? " " + v.unit : ""}`}
                  {v.raw_words
                    ? <em style={{ opacity: .6 }}>{" ["}{v.raw_words.join(", ")}{"]"}</em>
                    : null}
                </span>
              ))}
              <div className="muted" style={{ marginTop: 4, fontSize: 11 }}>
                The numbers in brackets are the raw registers. If a value looks
                wrong but the raw words look right, the format or word order is
                what needs changing — not the address.
              </div>
            </div>
          ) : (live.message || "Could not read the device.")}
        </div>
      ) : null}

      {registers.length ? (
        <div className="row ifm-port-head">
          <strong>Values to collect</strong>
          <span className={`muted ${ticked === 0 ? "eip-none-ticked" : ""}`} style={{ fontSize: 12 }}>
            {ticked} of {registers.length} ticked
          </span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            onClick={() => setAllTicks(true)}>All</button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            onClick={() => setAllTicks(false)}>None</button>
        </div>
      ) : null}

      {registers.length && ticked === 0 ? (
        <div className="info-note warn">
          Nothing is ticked. Tick at least one register — a gateway saved with an
          empty tag list collects every mapped register instead of none.
        </div>
      ) : null}

      <div className="ifm-field-table">
        <div className="ifm-field-head modbus-head">
          <span>Collect</span><span>Tag name</span><span>Address</span><span>Function</span>
          <span>Format</span><span>Bit</span><span>Scale</span><span>Offset</span>
          <span>Unit</span><span>Swap</span><span />
        </div>
        {registers.map((reg, idx) => (
          <div className={`ifm-field-row modbus-row ${reg.enabled === false ? "eip-row-off" : ""}`}
            key={`mb-${idx}`}>
            <label className="ifm-check">
              <input type="checkbox" checked={reg.enabled !== false} disabled={disabled}
                onChange={(e) => patchRow(idx, { enabled: e.target.checked })} />
            </label>
            <input value={reg.name || ""} disabled={disabled}
              onChange={(e) => patchRow(idx, { name: e.target.value })} />
            <input value={reg.address ?? ""} disabled={disabled} placeholder="30001"
              onChange={(e) => patchRow(idx, { address: e.target.value })} />
            <select value={reg.function || "input"} disabled={disabled}
              onChange={(e) => patchRow(idx, { function: e.target.value })}>
              {FUNCTIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
            <select value={reg.kind || "float32"} disabled={disabled}
              onChange={(e) => patchRow(idx, { kind: e.target.value })}>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input type="number" min="0" max="15" value={reg.bit ?? 0}
              disabled={disabled || String(reg.kind || "") !== "bool"}
              onChange={(e) => patchRow(idx, { bit: Number(e.target.value || 0) })} />
            <input type="number" step="any" value={reg.scale ?? 1} disabled={disabled}
              onChange={(e) => patchRow(idx, { scale: Number(e.target.value || 1) })} />
            <input type="number" step="any" value={reg.offset ?? 0} disabled={disabled}
              onChange={(e) => patchRow(idx, { offset: Number(e.target.value || 0) })} />
            <input value={reg.unit || ""} placeholder="V" disabled={disabled}
              onChange={(e) => patchRow(idx, { unit: e.target.value })} />
            <label className="ifm-check" title="Swap the two 16-bit words of a 32/64-bit value">
              <input type="checkbox" checked={Boolean(reg.word_swap)} disabled={disabled}
                onChange={(e) => patchRow(idx, { word_swap: e.target.checked })} />
            </label>
            <button type="button" className="btn btn-danger btn-sm" disabled={disabled}
              onClick={() => patchRegisters(registers.filter((_, i) => i !== idx))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" className="btn btn-secondary btn-sm" style={{ marginTop: 6 }}
          disabled={disabled}
          onClick={() => patchRegisters([...registers, {
            name: `Register${registers.length + 1}`, address: "", function: "input",
            kind: "float32", bit: 0, scale: 1, offset: 0, unit: "",
            word_swap: false, enabled: true,
          }])}>
          Add register
        </button>
      </div>
    </div>
  );
}
