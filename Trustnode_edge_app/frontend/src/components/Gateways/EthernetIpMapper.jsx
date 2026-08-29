/* Generic EtherNet/IP device — EDS import and assembly mapping.

   Rendered ONLY when gateway_type === "ethernet_ip".

   This is the CODESYS / ifm AE3100 workflow: import the device's EDS, pick the
   input assembly, then map byte offsets to named signals. The edge plays the
   originator role, so no PLC sits between us and the device.

   Byte offsets are the part an operator gets wrong, and a wrong offset produces
   a plausible number rather than an error — so "Read live assembly" shows the
   raw bytes beside the decoded values before anything is saved. */
import { useCallback, useMemo, useState } from "react";
import { parseEdsFile, previewEipAssembly, identifyEipDevice, generateIfmPinMap,
         scanCipParameters, previewCipParameters } from "../../api";
import DeviceCatalogue from "./DeviceCatalogue";

const CIP_TYPES = ["BOOL", "SINT", "USINT", "BYTE", "INT", "UINT", "WORD",
                   "DINT", "UDINT", "DWORD", "REAL", "LINT", "LREAL"];

/* The tags a gateway actually collects: the TICKED signals only.

   An unticked signal keeps its byte offset, type and bit - it is excluded from
   `tags`, not deleted - so turning a value back on never means scanning the
   device again. The backend already filters what it emits by this list (see
   `_read_from_ethernet_ip`: `wanted = set(self._get_read_tags())`), and one CIP
   read returns the whole assembly either way, so unticking costs nothing on the
   wire and saves a historian row per cycle per tag.

   `enabled === undefined` means ticked, so every gateway saved before this
   existed keeps collecting exactly what it collected. */
export function eipTagNames(signals) {
  return Array.from(new Set(
    (signals || [])
      .filter((s) => s && s.enabled !== false)
      .map((s) => String(s?.name || "").trim())
      .filter(Boolean)
  ));
}

export default function EthernetIpMapper({ form, onChange, disabled = false }) {
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [eds, setEds] = useState(null);
  const [live, setLive] = useState(null);
  const [portFilter, setPortFilter] = useState("all");
  const [paramScan, setParamScan] = useState(null);
  const [scanFrom, setScanFrom] = useState(1);
  const [scanTo, setScanTo] = useState(30);

  /* A drive names its values by PARAMETER NUMBER, not by assembly offset, so a
     PowerFlex or Kinetix is configured the way its manual reads. Both can be
     used at once: the assembly for bulk process data, parameters for the values
     the drive's own display names. */
  const parameters = useMemo(
    () => (Array.isArray(form.eip_parameters) ? form.eip_parameters : []),
    [form.eip_parameters]
  );

  const patchParameters = useCallback((next) => {
    const assemblyNames = (Array.isArray(form.eip_signals) ? form.eip_signals : [])
      .filter((x) => x && x.enabled !== false)
      .map((x) => String(x.name || "").trim()).filter(Boolean);
    const paramNames = next.filter((x) => x && x.enabled !== false)
      .map((x) => String(x.name || "").trim()).filter(Boolean);
    onChange({
      eip_parameters: next,
      tags_text: Array.from(new Set([...assemblyNames, ...paramNames])).join(";"),
    });
  }, [onChange, form.eip_signals]);

  const patchParam = (idx, patch) =>
    patchParameters(parameters.map((x, i) => (i === idx ? { ...x, ...patch } : x)));

  const scanParams = useCallback(async () => {
    setBusy("scan"); setNote("");
    try {
      const res = await scanCipParameters({
        plc_ip: String(form.plc_ip || "").trim(),
        slot: Number(form.eip_slot || 0),
        first: Number(scanFrom || 1),
        last: Number(scanTo || 30),
      });
      setParamScan(res);
      setNote(res?.message || "");
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.eip_slot, scanFrom, scanTo]);

  const readParams = useCallback(async () => {
    setBusy("params"); setNote("");
    try {
      const res = await previewCipParameters({
        plc_ip: String(form.plc_ip || "").trim(),
        slot: Number(form.eip_slot || 0),
        parameters,
      });
      setNote(res?.message || "");
      if (Array.isArray(res?.values)) {
        const byName = {};
        res.values.forEach((v) => { byName[v.name] = v; });
        setParamScan({ ok: true, preview: byName });
      }
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.eip_slot, parameters]);

  const signals = useMemo(
    () => (Array.isArray(form.eip_signals) ? form.eip_signals : []),
    [form.eip_signals]
  );

  const patchSignals = useCallback((next) => {
    onChange({ eip_signals: next, tags_text: eipTagNames(next).join(";") });
  }, [onChange]);

  const ticked = useMemo(
    () => signals.filter((s) => s && s.enabled !== false).length,
    [signals]
  );

  const setAllTicks = useCallback((on) => {
    patchSignals(signals.map((s) => ({ ...s, enabled: on })));
  }, [signals, patchSignals]);

  // An ifm pin map names its signals Port<N>_Pin<M>. When they are present,
  // offer one button per port - on a block where only ports 7 and 8 are wired,
  // that is the difference between ticking 2 boxes and hunting through 16.
  const ports = useMemo(() => {
    const found = new Set();
    for (const sig of signals) {
      const m = /^Port(\d+)_/i.exec(String(sig?.name || ""));
      if (m) found.add(Number(m[1]));
    }
    return Array.from(found).sort((a, b) => a - b);
  }, [signals]);

  const portOf = (sig) => {
    const m = /^Port(\d+)_/i.exec(String(sig?.name || ""));
    return m ? Number(m[1]) : null;
  };

  const tickPortOnly = useCallback((port) => {
    patchSignals(signals.map((s) => ({ ...s, enabled: portOf(s) === port })));
  }, [signals, patchSignals]);

  const shown = useMemo(() => {
    const rows = signals.map((sig, idx) => ({ sig, idx }));
    if (portFilter === "all") return rows;
    return rows.filter(({ sig }) => portOf(sig) === Number(portFilter));
  }, [signals, portFilter]);

  const importEds = useCallback(async (text) => {
    setBusy("eds"); setNote("");
    try {
      const res = await parseEdsFile(text);
      setEds(res);
      setNote(res?.message || "");
      if (res?.ok) {
        const patch = {
          eip_input_assembly: Number(res.suggested?.input_assembly || form.eip_input_assembly || 0),
          eip_output_assembly: Number(res.suggested?.output_assembly || 0),
          eip_config_assembly: Number(res.suggested?.config_assembly || 0),
          eip_device_info: res.device || {},
        };
        // 2026-08-28: for a device family whose layout is known, the import
        // also brings the TAGS. An EDS on its own can never do this - it
        // declares assembly 100 as 223 anonymous 16-bit members - so the
        // backend combines the EDS with the layout for that family. Only
        // replace existing signals when the operator has none, so an import
        // cannot silently discard a map they built by hand.
        const generated = Array.isArray(res.signals) ? res.signals : [];
        if (generated.length && signals.length === 0) {
          patch.eip_signals = generated;
          patch.tags_text = eipTagNames(generated).join(";");
        } else if (generated.length) {
          setNote((res.message || "")
            + ` This device has ${generated.length} known tag(s) available — `
            + `remove the current signals and import again to use them.`);
        }
        onChange(patch);
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

  // The ifm digital-input map, built and checked against the device rather
  // than typed in by hand. The names match what the IoT Core path discovers,
  // so a trend built one way keeps working the other way.
  const buildIfmPinMap = useCallback(async () => {
    setBusy("pinmap"); setNote("");
    try {
      const res = await generateIfmPinMap({
        plc_ip: String(form.plc_ip || "").trim(),
        instance: Number(form.eip_input_assembly || 0),
        slot: Number(form.eip_slot || 0),
        port_count: Number(form.ifm_port_count || 8),
        verify: true,
      });
      setNote(res?.message || "");
      const built = Array.isArray(res?.signals) ? res.signals : [];
      if (built.length) {
        // Keep any signal the operator already mapped by hand; the pin map
        // only ADDS the port pins, so a mixed map is not destroyed.
        const taken = new Set(built.map((x) => String(x.name)));
        const kept = signals.filter((x) => !taken.has(String(x.name)));
        patchSignals([...built, ...kept]);
      }
      if (res?.values?.length) setLive({ ok: true, raw: res.raw, values: res.values });
    } catch (err) {
      setNote(String(err?.message || err));
    } finally { setBusy(""); }
  }, [form.plc_ip, form.eip_input_assembly, form.eip_slot, form.ifm_port_count,
      signals, patchSignals]);

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

  // Width in bytes of each CIP type, so a new signal lands AFTER the last one
  // instead of on top of it. Three signals all left at offset 0 read the same
  // two bytes three times, which looks like a mapping and produces nothing.
  const widthOf = (kind) => {
    const k = String(kind || "INT").toUpperCase();
    if (["BOOL", "SINT", "USINT", "BYTE"].includes(k)) return 1;
    if (["INT", "UINT", "WORD"].includes(k)) return 2;
    if (["DINT", "UDINT", "DWORD", "REAL"].includes(k)) return 4;
    return 8;
  };

  const nextOffset = () =>
    signals.reduce(
      (max, s) => Math.max(max, Number(s?.byte_offset || 0) + widthOf(s?.kind)),
      0
    );

  // A digital-input block's input assembly is a bitfield: one bit per pin. With
  // no EDS there is nothing to import, so read the real bytes and turn each bit
  // into a tag named the way the block is labelled. Toggling an input and
  // pressing "Read live assembly" again shows which bit moved.
  const mapBitsAsTags = useCallback(() => {
    const bytes = Number(live?.size_bytes || 0);
    if (!bytes) return;
    const next = [];
    for (let b = 0; b < bytes; b += 1) {
      for (let bit = 0; bit < 8; bit += 1) {
        const port = Math.floor((b * 8 + bit) / 2) + 1;
        const pin = (b * 8 + bit) % 2 === 0 ? 2 : 4;
        next.push({
          name: `Port${port}_Pin${pin}`,
          byte_offset: b,
          kind: "BOOL",
          bit,
          scale: 1,
          offset: 0,
          unit: "",
        });
      }
    }
    // A 446-byte assembly is 3568 bits. Map them all - collecting them all
    // would put 3568 rows per cycle into the historian for a block with eight
    // wired ports. Past a pin map's worth they arrive unticked.
    const BULK = 32;
    const tickAll = next.length <= BULK;
    patchSignals(next.map((x) => ({ ...x, enabled: tickAll })));
    setNote(
      tickAll
        ? `Created ${next.length} bit tags from ${bytes} byte(s). Toggle an input and `
          + `read again to confirm which bit is which, then untick the ones you do not need.`
        : `Created ${next.length} bit tags from ${bytes} byte(s), all unticked — that is `
          + `too many to collect. Tick the ports you have wired ("Only port N" above), `
          + `or toggle an input and read again to find the bit that moved.`
    );
  }, [live, patchSignals]);

  return (
    <div className="gateway-span-2 ifm-panel">
      <div className="muted ifm-help">
        Read any EtherNet/IP device directly — an IO-Link block, remote I/O, a drive —
        with no PLC in between. Import its EDS to discover the assemblies, then map the
        bytes you want. They become ordinary tags and trend like any PLC tag.
      </div>

      <DeviceCatalogue
        protocol="ethernet_ip"
        disabled={disabled}
        onApply={(profile) => {
          const d = profile.defaults || {};
          const rows = (profile.tags || []).map((t) => ({
            ...t, enabled: Boolean(profile.verified),
          }));
          // A drive profile is parameter-based; an adapter profile is
          // assembly-based. Route by what the tags actually carry.
          if (rows.length && rows[0].param !== undefined) {
            onChange({ eip_slot: Number(d.eip_slot ?? form.eip_slot ?? 0) });
            patchParameters(rows);
          } else {
            patchSignals(rows);
          }
        }}
      />

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
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled || !!busy}
          onClick={buildIfmPinMap}
          title="Build the standard ifm port-pin map for the input assembly and check it against the device">
          {busy === "pinmap" ? "Building…" : "ifm pin map"}
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
              {live.size_bytes ? (
                <button type="button" className="btn btn-secondary btn-sm"
                  style={{ marginTop: 6 }} disabled={disabled}
                  onClick={mapBitsAsTags}>
                  Create a tag per bit ({Number(live.size_bytes) * 8} inputs)
                </button>
              ) : null}
            </>
          ) : (live.message || "Could not read the assembly.")}
        </div>
      ) : null}

      {/* ------------------------------------------------ drive parameters --
          A drive (PowerFlex, Kinetix) names its values by PARAMETER NUMBER,
          the way its own display and manual do. That is steadier than slicing
          an assembly, because the numbering survives firmware changes that
          move bytes around. Both can be used at once. */}
      <div className="row ifm-port-head" style={{ marginTop: 6 }}>
        <strong>Drive parameters (CIP)</strong>
        <span className="muted" style={{ fontSize: 12 }}>
          {parameters.filter((x) => x && x.enabled !== false).length} of {parameters.length} ticked
        </span>
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
          onClick={() => patchParameters([...parameters, {
            name: `Param${parameters.length + 1}`, param: 1, kind: "INT",
            scale: 1, offset: 0, unit: "", enabled: true,
          }])}>
          Add parameter
        </button>
        <button type="button" className="btn btn-secondary btn-sm"
          disabled={disabled || !!busy || !parameters.length} onClick={readParams}
          title="Read the mapped parameters now, with scaling applied">
          {busy === "params" ? "Reading…" : "Read parameters"}
        </button>
      </div>

      <div className="row" style={{ gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span className="muted" style={{ fontSize: 11.5 }}>Scan parameters</span>
        <input type="number" min="1" style={{ width: 70 }} value={scanFrom}
          disabled={disabled} onChange={(e) => setScanFrom(Number(e.target.value || 1))} />
        <span className="muted">to</span>
        <input type="number" min="1" style={{ width: 70 }} value={scanTo}
          disabled={disabled} onChange={(e) => setScanTo(Number(e.target.value || 30))} />
        <button type="button" className="btn btn-secondary btn-sm"
          disabled={disabled || !!busy} onClick={scanParams}
          title="Read a range of parameters and show what answers, so the numbering can be checked against the drive's own display">
          {busy === "scan" ? "Scanning…" : "Scan"}
        </button>
        <span className="muted" style={{ fontSize: 11 }}>
          Parameter numbering differs by drive family and firmware — scan, then
          compare with the drive's display before trusting a value.
        </span>
      </div>

      {paramScan?.values ? (
        <div className="info-note ifm-preview">
          {paramScan.values.filter((v) => v.ok).map((v, i) => (
            <span key={i} className="ifm-preview-val">
              <strong>#{v.param}</strong>{" = "}{v.value}
            </span>
          ))}
          {!paramScan.values.some((v) => v.ok) ? (
            <em>No parameter in that range answered. The drive may use a different
              object, or the range may be wrong.</em>
          ) : null}
        </div>
      ) : null}

      {parameters.length ? (
        <div className="ifm-field-table">
          <div className="ifm-field-head cipparam-head">
            <span>Collect</span><span>Tag name</span><span>Parameter</span><span>Type</span>
            <span>Scale</span><span>Unit</span><span>Last read</span><span />
          </div>
          {parameters.map((prm, idx) => (
            <div className={`ifm-field-row cipparam-row ${prm.enabled === false ? "eip-row-off" : ""}`}
              key={`cip-${idx}`}>
              <label className="ifm-check">
                <input type="checkbox" checked={prm.enabled !== false} disabled={disabled}
                  onChange={(e) => patchParam(idx, { enabled: e.target.checked })} />
              </label>
              <input value={prm.name || ""} disabled={disabled}
                onChange={(e) => patchParam(idx, { name: e.target.value })} />
              <input type="number" min="1" value={prm.param ?? 1} disabled={disabled}
                onChange={(e) => patchParam(idx, { param: Number(e.target.value || 1) })} />
              <select value={prm.kind || "INT"} disabled={disabled}
                onChange={(e) => patchParam(idx, { kind: e.target.value })}>
                {["INT", "UINT", "DINT", "UDINT", "REAL", "SINT", "USINT"].map(
                  (k) => <option key={k} value={k}>{k}</option>)}
              </select>
              <input type="number" step="any" value={prm.scale ?? 1} disabled={disabled}
                onChange={(e) => patchParam(idx, { scale: Number(e.target.value || 1) })} />
              <input value={prm.unit || ""} placeholder="Hz" disabled={disabled}
                onChange={(e) => patchParam(idx, { unit: e.target.value })} />
              <span className="muted" style={{ fontSize: 11.5 }}>
                {paramScan?.preview?.[prm.name]
                  ? (paramScan.preview[prm.name].quality
                      ? `${paramScan.preview[prm.name].value}`
                      : paramScan.preview[prm.name].error)
                  : "—"}
              </span>
              <button type="button" className="btn btn-danger btn-sm" disabled={disabled}
                onClick={() => patchParameters(parameters.filter((_, i) => i !== idx))}>
                Remove
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {signals.length ? (
        <div className="row ifm-port-head">
          <strong>Values to collect</strong>
          <span className={`muted ${ticked === 0 ? "eip-none-ticked" : ""}`} style={{ fontSize: 12 }}>
            {ticked} of {signals.length} ticked
          </span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            onClick={() => setAllTicks(true)}>All</button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            onClick={() => setAllTicks(false)}>None</button>
          {ports.map((p) => (
            <button key={p} type="button" className="btn btn-secondary btn-sm" disabled={disabled}
              title={`Collect port ${p} only`}
              onClick={() => tickPortOnly(p)}>Only port {p}</button>
          ))}
          {ports.length > 1 ? (
            <select value={portFilter} style={{ marginLeft: "auto", width: 130 }}
              onChange={(e) => setPortFilter(e.target.value)}>
              <option value="all">Show all ({signals.length})</option>
              {ports.map((p) => <option key={p} value={String(p)}>Port {p} only</option>)}
            </select>
          ) : null}
        </div>
      ) : null}

      {/* Untick everything and `tags` is empty - and an empty tag list means
          "no filter" to the backend, which then emits EVERY mapped signal. Say
          so rather than letting the operator discover it in the historian. */}
      {signals.length && ticked === 0 ? (
        <div className="info-note warn">
          Nothing is ticked. Tick at least one value — a gateway saved with an
          empty tag list collects every mapped signal instead of none.
        </div>
      ) : null}

      <div className="ifm-field-table">
        <div className="ifm-field-head eip-head">
          <span>Collect</span>
          <span>Tag name</span><span>Byte offset</span><span>Type</span>
          <span>Bit</span><span>Scale</span><span>Offset</span><span>Unit</span><span />
        </div>
        {shown.map(({ sig, idx }) => (
          <div className={`ifm-field-row eip-row ${sig.enabled === false ? "eip-row-off" : ""}`}
            key={`eip-${idx}`}>
            <label className="ifm-check">
              <input type="checkbox" checked={sig.enabled !== false} disabled={disabled}
                onChange={(e) => patchSignal(idx, { enabled: e.target.checked })} />
            </label>
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
            name: `Signal${signals.length + 1}`, byte_offset: nextOffset(), kind: "INT",
            bit: 0, scale: 1, offset: 0, unit: "", enabled: true,
          }])}>
          Add signal
        </button>
      </div>
    </div>
  );
}
