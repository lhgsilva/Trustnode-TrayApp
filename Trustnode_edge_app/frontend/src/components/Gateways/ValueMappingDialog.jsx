import React, { useMemo, useState } from "react";

/**
 * Describe ONE value inside a device's raw bytes, for someone who does not
 * think in bits.
 *
 * 2026-09-06, asked: how a sensor on an ifm block exposes its readings, and
 * whether the software can convert them "only using the scale and offset".
 * It cannot. An IO-Link sensor packs values that do not sit on byte
 * boundaries - the SM9400 flow meter puts a 14-bit temperature and TWO output
 * bits in one word, so the temperature needs (word >> 2). A scale of 0.025
 * fakes it only while both outputs are off, and inverts the sign of any
 * negative reading.
 *
 * Built around a LIVE PREVIEW: the operator picks a preset, sees the decoded
 * value appear beside the raw bytes, and knows it is right before saving.
 * That is what makes a bit field usable by someone who has never heard of one.
 *
 * The preview mirrors the backend rule (IODD: bit 0 is the LSB of the whole
 * record). It is only a preview - the gateway always decodes with the backend
 * implementation, which is pinned by its own tests.
 */

const PRESETS = [
  {
    id: "bit",
    label: "A single on/off bit (a digital output)",
    hint: "One bit, like OUT1 or OUT2 on an IO-Link sensor.",
    apply: (s) => ({ bit_length: 1, signed: false, scale: 1, offset: 0,
                     bit_offset: Number(s.bit_offset) || 0 }),
  },
  {
    id: "int16_tenths",
    label: "16-bit whole number, one decimal place (x 0.1)",
    hint: "A value the datasheet writes as (-3600 to 3600) * 0.1.",
    apply: () => ({ bit_length: 16, signed: true, scale: 0.1, offset: 0 }),
  },
  {
    id: "int14_tenths",
    label: "14-bit value sharing a word with flags (x 0.1)",
    hint: "The temperature case: 14 bits of value, then the output bits.",
    apply: () => ({ bit_offset: 2, bit_length: 14, signed: true, scale: 0.1, offset: 0 }),
  },
  {
    id: "float32",
    label: "32-bit decimal number, across two words",
    hint: "A totaliser or counter the datasheet calls Float32.",
    apply: () => ({ bit_length: 32, kind: "REAL", signed: false, scale: 1, offset: 0 }),
  },
  {
    id: "uint16",
    label: "16-bit whole number, as-is",
    hint: "A plain counter with no scaling.",
    apply: () => ({ bit_length: 16, signed: false, scale: 1, offset: 0 }),
  },
];

// Mirror of the backend rule: bit 0 is the LEAST significant bit of the whole
// record, so the field shifts down by its offset and masks to its width.
function decodePreview(hex, sig) {
  const text = String(hex || "").replace(/[^0-9a-fA-F]/g, "");
  if (!text) return { error: "Paste the raw bytes to see the value" };
  const bits = text.length * 4;
  const off = Number(sig.bit_offset) || 0;
  const len = Number(sig.bit_length) || 0;
  if (!len) return { error: "Choose how long the value is" };
  if (off + len > bits) {
    return { error: `Needs bits ${off}..${off + len} but only ${bits} were read` };
  }
  let raw;
  try {
    raw = (BigInt("0x" + text) >> BigInt(off)) & ((1n << BigInt(len)) - 1n);
  } catch (_) {
    return { error: "Those are not valid hexadecimal bytes" };
  }
  if (len === 1) return { value: raw === 1n, raw: raw.toString() };
  if (String(sig.kind || "").toUpperCase() === "REAL" && len === 32) {
    const buf = new ArrayBuffer(4);
    new DataView(buf).setUint32(0, Number(raw), false);
    const f = new DataView(buf).getFloat32(0, false);
    return { value: f * (Number(sig.scale) || 1) + (Number(sig.offset) || 0),
             raw: raw.toString() };
  }
  let n = raw;
  if (sig.signed && ((n >> BigInt(len - 1)) & 1n)) n -= 1n << BigInt(len);
  return {
    value: Number(n) * (Number(sig.scale) || 1) + (Number(sig.offset) || 0),
    raw: n.toString(),
  };
}

export default function ValueMappingDialog({ signal, sampleHex = "", onApply, onClose }) {
  const [sig, setSig] = useState({
    bit_offset: Number(signal?.bit_offset) || 0,
    bit_length: Number(signal?.bit_length) || 0,
    signed: Boolean(signal?.signed),
    kind: signal?.kind || "INT",
    scale: signal?.scale ?? 1,
    offset: signal?.offset ?? 0,
    unit: signal?.unit || "",
  });
  const [hex, setHex] = useState(sampleHex);
  const preview = useMemo(() => decodePreview(hex, sig), [hex, sig]);
  const set = (patch) => setSig((p) => ({ ...p, ...patch }));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card dashboard-query-modal dashboard-query-modal-wide"
        style={{ width: "min(920px, 96vw)", maxHeight: "92vh", overflowY: "auto" }}
        onClick={(e) => e.stopPropagation()}>
        <h3>Value mapping &mdash; {signal?.name || "signal"}</h3>
        <p className="dashboard-query-hint" style={{ marginTop: 0 }}>
          Where this value sits inside the bytes the device returns. Read it off the
          sensor datasheet: it names the offset, the length and the multiplier.
        </p>

        <div className="dashboard-query-sections">
          <fieldset className="dashboard-query-fieldset">
            <legend>Start from a common shape</legend>
            <div className="dashboard-query-grid">
              {PRESETS.map((p) => (
                <button key={p.id} type="button" className="btn btn-secondary btn-sm"
                  style={{ gridColumn: "1 / -1", textAlign: "left" }}
                  onClick={() => set(p.apply(sig))} title={p.hint}>
                  {p.label}
                  <small className="muted" style={{ display: "block" }}>{p.hint}</small>
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="dashboard-query-fieldset">
            <legend>Where the value is</legend>
            <div className="dashboard-query-grid">
              <label className="dashboard-query-field">
                <span>Starts at bit</span>
                <input type="number" min="0" value={sig.bit_offset}
                  onChange={(e) => set({ bit_offset: Number(e.target.value || 0) })} />
                <small className="muted">
                  Counted from the end of the data, the way the datasheet counts it.
                </small>
              </label>
              <label className="dashboard-query-field">
                <span>How many bits</span>
                <input type="number" min="1" max="64" value={sig.bit_length}
                  onChange={(e) => set({ bit_length: Number(e.target.value || 0) })} />
                <small className="muted">1 = a single on/off bit. 16 = one word.</small>
              </label>
              <label className="dashboard-query-field">
                <span>Can it be negative?</span>
                <select value={sig.signed ? "yes" : "no"}
                  onChange={(e) => set({ signed: e.target.value === "yes" })}>
                  <option value="no">No &mdash; zero or above</option>
                  <option value="yes">Yes &mdash; can go below zero</option>
                </select>
              </label>
              <label className="dashboard-query-field">
                <span>Number format</span>
                <select
                  value={String(sig.kind || "INT").toUpperCase() === "REAL" ? "REAL" : "INT"}
                  onChange={(e) => set({ kind: e.target.value })}>
                  <option value="INT">Whole number</option>
                  <option value="REAL">Decimal (32-bit float)</option>
                </select>
              </label>
            </div>
          </fieldset>

          <fieldset className="dashboard-query-fieldset">
            <legend>Turn it into engineering units</legend>
            <div className="dashboard-query-grid">
              <label className="dashboard-query-field">
                <span>Multiply by</span>
                <input type="number" step="any" value={sig.scale}
                  onChange={(e) => set({ scale: Number(e.target.value || 1) })} />
                <small className="muted">0.1 when the datasheet says (value) x 0.1.</small>
              </label>
              <label className="dashboard-query-field">
                <span>Then add</span>
                <input type="number" step="any" value={sig.offset}
                  onChange={(e) => set({ offset: Number(e.target.value || 0) })} />
              </label>
              <label className="dashboard-query-field">
                <span>Unit</span>
                <input value={sig.unit} placeholder="degC"
                  onChange={(e) => set({ unit: e.target.value })} />
              </label>
            </div>
          </fieldset>

          <fieldset className="dashboard-query-fieldset">
            <legend>Check it against real data</legend>
            <div className="dashboard-query-grid">
              <label className="dashboard-query-field" style={{ gridColumn: "1 / -1" }}>
                <span>Raw bytes from the device (hex)</span>
                <input value={hex} placeholder="4838A16000000538"
                  onChange={(e) => setHex(e.target.value)} />
                <small className="muted">
                  The last reading, or one pasted from the block. Nothing here is
                  saved &mdash; it only proves the mapping.
                </small>
              </label>
            </div>
            <div style={{ padding: "8px 10px", fontSize: 15 }}>
              {preview.error ? (
                <span className="muted">{preview.error}</span>
              ) : (
                <span>
                  reads{" "}
                  <strong>
                    {typeof preview.value === "boolean"
                      ? (preview.value ? "ON" : "OFF")
                      : Number(preview.value).toLocaleString(undefined,
                          { maximumFractionDigits: 4 })}
                  </strong>
                  {sig.unit ? ` ${sig.unit}` : ""}
                  <small className="muted"> (raw {preview.raw})</small>
                </span>
              )}
            </div>
          </fieldset>
        </div>

        <div className="row modal-actions">
          <button type="button" className="btn btn-primary"
            onClick={() => onApply({
              bit_offset: Number(sig.bit_offset) || 0,
              bit_length: Number(sig.bit_length) || 0,
              signed: Boolean(sig.signed),
              kind: sig.kind,
              scale: Number(sig.scale) || 1,
              offset: Number(sig.offset) || 0,
              unit: sig.unit,
            })}>
            Apply
          </button>
          <button type="button" className="btn btn-danger" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
