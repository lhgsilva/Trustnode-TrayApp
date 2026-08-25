/* IFM IO-Link master — port scan and tag mapping, inside the gateway dialog.

   Rendered ONLY when gateway_type === "ifm_iolink", so every other gateway type
   renders exactly the markup it did before.

   What this is for: an ifm block hands us raw hex process data per port, and the
   layout of those bits is defined by the connected sensor's IODD. Rather than
   make an operator count bits blind, this asks the block what is plugged in
   (gettree + a port scan), suggests a built-in profile when it recognises the
   sensor, and decodes the LIVE value next to the raw hex so a mapping can be
   confirmed before it is saved. That is the same idea as installing a device
   description in CODESYS and getting named channels back.

   The component owns no persistence: it hands the finished port mapping and the
   resulting tag names back to the dialog, which saves them like any other
   gateway field. */
import { useCallback, useMemo, useState } from "react";
import { scanIfmPorts, previewIfmPort, readIfmDatapoints } from "../../api";

const KINDS = [
  { value: "uint", label: "Unsigned" },
  { value: "int", label: "Signed" },
  { value: "bool", label: "Bit (on/off)" },
  { value: "float32", label: "Float 32" },
];

function blankField(port, index) {
  return {
    name: `Port${port}_Value${index > 0 ? index + 1 : ""}`,
    bit_offset: 0,
    bit_length: 16,
    kind: "uint",
    scale: 1,
    offset: 0,
    unit: "",
  };
}

/** Tag names from the unified datapoint list (every ifm device kind). */
export function ifmDatapointNames(datapoints) {
  return Array.from(new Set(
    (datapoints || [])
      .filter((d) => d && d.enabled !== false)
      .map((d) => String(d.name || "").trim())
      .filter(Boolean)
  ));
}

/** Every tag name this mapping will produce — what the dialog stores in `tags`. */
export function ifmTagNames(ifmPorts) {
  const out = [];
  (ifmPorts || []).forEach((p) => {
    if (!p || p.enabled === false) return;
    (p.fields || []).forEach((f) => {
      const name = String(f?.name || "").trim();
      if (name) out.push(`${p.prefix || ""}${name}`);
    });
  });
  return Array.from(new Set(out));
}

export default function IfmPortMapper({
  form,
  onChange,            // (patch) => void  — merged into gatewayForm
  disabled = false,
}) {
  const [scanning, setScanning] = useState(false);
  const [scan, setScan] = useState(null);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState({});      // { [port]: {raw, values} }

  const ports = useMemo(
    () => (Array.isArray(form.ifm_ports) ? form.ifm_ports : []),
    [form.ifm_ports]
  );

  const connection = useMemo(() => ({
    plc_ip: String(form.plc_ip || "").trim(),
    http_port: Number(form.ifm_http_port || 80),
    use_https: Boolean(form.ifm_use_https),
    verify_tls: Boolean(form.ifm_verify_tls),
    username: String(form.ifm_username || ""),
    password: String(form.ifm_password || ""),
    port_count: Number(form.ifm_port_count || 8),
  }), [form.plc_ip, form.ifm_http_port, form.ifm_use_https, form.ifm_verify_tls,
       form.ifm_username, form.ifm_password, form.ifm_port_count]);

  const patchPorts = useCallback((next) => {
    onChange({ ifm_ports: next, tags_text: ifmTagNames(next).join(";") });
  }, [onChange]);

  const doScan = useCallback(async () => {
    if (!connection.plc_ip) { setNote("Enter the block's IoT address first."); return; }
    setScanning(true);
    setNote("");
    try {
      const res = await scanIfmPorts({ ...connection, variant: form.ifm_variant || "auto" });
      setScan(res);
      setNote(res?.message || "");
      if (res?.ok) {
        // The unified datapoint list covers every ifm device kind — an I/O
        // module's digital inputs and an IO-Link master's decoded values arrive
        // in the same shape, so the operator sees one table either way.
        // Anything already ticked keeps its settings.
        const previous = new Map((form.ifm_datapoints || []).map((d) => [String(d.name), d]));
        const merged = (res.datapoints || []).map((d) => previous.get(String(d.name)) || d);
        onChange({
          ifm_variant: res.variant || form.ifm_variant || "auto",
          ifm_datapoints: merged,
          tags_text: ifmDatapointNames(merged).join(";"),
        });
      }
      if (res?.ok) {
        // Pre-fill a row for every port that has something connected, using the
        // suggested profile where the sensor is recognised. Ports the operator
        // has already mapped are left exactly as they are.
        const existing = new Map(ports.map((p) => [Number(p.port), p]));
        const next = (res.ports || [])
          .filter((p) => p.connected)
          .map((p) => existing.get(Number(p.port)) || {
            port: Number(p.port),
            enabled: true,
            prefix: "",
            profile: p.suggested_profile || "",
            fields: (((res.profiles || []).find((x) => x.id === p.suggested_profile) || {}).fields || [])
              .map((f) => ({ ...f, name: `Port${p.port}_${f.name}` })),
          });
        const untouched = ports.filter(
          (p) => !(res.ports || []).some((s) => Number(s.port) === Number(p.port) && s.connected)
        );
        patchPorts([...next, ...untouched]);
      }
    } catch (err) {
      setNote(String(err?.message || err));
    } finally {
      setScanning(false);
    }
  }, [connection, ports, patchPorts]);

  const doPreview = useCallback(async (port) => {
    const entry = ports.find((p) => Number(p.port) === Number(port));
    if (!entry) return;
    try {
      const res = await previewIfmPort({ ...connection, port: Number(port), fields: entry.fields || [] });
      setPreview((prev) => ({ ...prev, [port]: res }));
    } catch (err) {
      setPreview((prev) => ({ ...prev, [port]: { ok: false, message: String(err?.message || err) } }));
    }
  }, [connection, ports]);

  const datapoints = Array.isArray(form.ifm_datapoints) ? form.ifm_datapoints : [];

  const patchDatapoints = useCallback((next) => {
    onChange({ ifm_datapoints: next, tags_text: ifmDatapointNames(next).join(";") });
  }, [onChange]);

  const patchPoint = (idx, patch) =>
    patchDatapoints(datapoints.map((d, i) => (i === idx ? { ...d, ...patch } : d)));

  const [liveValues, setLiveValues] = useState(null);
  const readLive = useCallback(async () => {
    try {
      const res = await readIfmDatapoints({
        ...connection,
        datapoints: datapoints.filter((d) => d.enabled !== false),
      });
      setLiveValues(res);
      if (!res?.ok) setNote(res?.message || "Read failed.");
    } catch (err) {
      setNote(String(err?.message || err));
    }
  }, [connection, datapoints]);

  const patchField = (portNo, idx, patch) => {
    patchPorts(ports.map((p) => (Number(p.port) !== Number(portNo) ? p : {
      ...p,
      fields: (p.fields || []).map((f, i) => (i === idx ? { ...f, ...patch } : f)),
    })));
  };

  const scannedFor = (portNo) =>
    ((scan || {}).ports || []).find((s) => Number(s.port) === Number(portNo)) || null;

  return (
    <div className="gateway-span-2 ifm-panel">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 12 }}>
        <div className="muted ifm-help">
          This block serves its data as JSON over HTTP — no PLC and no EtherNet/IP
          configuration needed, and no EDS file. Scan it, tick the values you want, and
          they become ordinary tags: they trend, chart, report and alarm like any PLC tag.
        </div>
        <button type="button" className="btn btn-primary btn-sm" disabled={disabled || scanning}
          onClick={doScan}>
          {scanning ? "Scanning…" : "Scan block"}
        </button>
      </div>

      <div className="ifm-conn-grid">
        <label>
          Device kind
          <select value={form.ifm_variant || "auto"} disabled={disabled}
            onChange={(e) => onChange({ ifm_variant: e.target.value })}>
            {(scan?.variants || [
              { id: "auto", label: "Detect automatically" },
              { id: "iolink_master", label: "IO-Link master (AL13xx / AL14xx)" },
              { id: "io_module", label: "I/O module (AL40xx, e.g. AL4022)" },
            ]).map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
          </select>
          <small className="hint">
            Leave on Detect — the block is asked what it is when you scan.
          </small>
        </label>
      </div>

      <div className="ifm-conn-grid">
        <label>
          IoT port
          <input type="number" min="1" max="65535" value={form.ifm_http_port ?? 80}
            disabled={disabled}
            onChange={(e) => onChange({ ifm_http_port: Number(e.target.value || 80) })} />
        </label>
        <label>
          Ports on the block
          <input type="number" min="1" max="16" value={form.ifm_port_count ?? 8}
            disabled={disabled}
            onChange={(e) => onChange({ ifm_port_count: Number(e.target.value || 8) })} />
          <small className="hint">Only used if the block does not report its own.</small>
        </label>
        <label className="ifm-check">
          <input type="checkbox" checked={Boolean(form.ifm_use_https)} disabled={disabled}
            onChange={(e) => onChange({ ifm_use_https: e.target.checked })} />
          <span>Use HTTPS</span>
        </label>
        {form.ifm_use_https ? (
          <>
            <label>
              User
              <input value={form.ifm_username || ""} disabled={disabled}
                onChange={(e) => onChange({ ifm_username: e.target.value })} />
            </label>
            <label>
              Password
              <input type="password" value={form.ifm_password || ""} disabled={disabled}
                onChange={(e) => onChange({ ifm_password: e.target.value })} />
            </label>
          </>
        ) : null}
      </div>

      {note ? <div className="info-note" style={{ marginTop: 8 }}>{note}</div> : null}
      {scan?.device?.product_code ? (
        <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
          Block: <strong>{scan.device.product_code}</strong>
          {scan.device.serial_number ? ` · serial ${scan.device.serial_number}` : ""}
        </div>
      ) : null}

      {datapoints.length ? (
        <div className="ifm-port-card">
          <div className="row ifm-port-head">
            <strong>Values to collect</strong>
            <span className="muted" style={{ fontSize: 12 }}>
              {datapoints.filter((d) => d.enabled !== false).length} of {datapoints.length} ticked
            </span>
            <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
              style={{ marginLeft: "auto" }} onClick={readLive}>
              Read live values
            </button>
          </div>

          {liveValues?.values ? (
            <div className="info-note ifm-preview">
              {liveValues.values.map((v, i) => (
                <span key={i} className="ifm-preview-val">
                  <strong>{v.name}</strong>{" = "}
                  {v.error ? <em>{v.error}</em> : `${v.value}${v.unit ? " " + v.unit : ""}`}
                </span>
              ))}
            </div>
          ) : null}

          <div className="ifm-field-table">
            <div className="ifm-field-head ifm-dp-head">
              <span>Collect</span><span>Tag name</span><span>Source</span>
              <span>Scale</span><span>Unit</span>
            </div>
            {datapoints.map((d, idx) => (
              <div className="ifm-field-row ifm-dp-row" key={`dp-${d.name}-${idx}`}>
                <label className="ifm-check">
                  <input type="checkbox" checked={d.enabled !== false} disabled={disabled}
                    onChange={(e) => patchPoint(idx, { enabled: e.target.checked })} />
                </label>
                <input value={d.name || ""} disabled={disabled}
                  onChange={(e) => patchPoint(idx, { name: e.target.value })} />
                <span className="muted ifm-dp-adr" title={d.adr}>{d.adr}</span>
                <input type="number" step="any" value={d.scale ?? 1} disabled={disabled}
                  onChange={(e) => patchPoint(idx, { scale: Number(e.target.value || 1) })} />
                <input value={d.unit || ""} disabled={disabled} placeholder=""
                  onChange={(e) => patchPoint(idx, { unit: e.target.value })} />
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {ports.length && !datapoints.length ? ports.map((p) => {
        const found = scannedFor(p.port);
        const pv = preview[p.port];
        return (
          <div className="ifm-port-card" key={`ifm-port-${p.port}`}>
            <div className="row ifm-port-head">
              <label className="ifm-check">
                <input type="checkbox" checked={p.enabled !== false} disabled={disabled}
                  onChange={(e) => patchPorts(ports.map((x) =>
                    Number(x.port) !== Number(p.port) ? x : { ...x, enabled: e.target.checked }))} />
                <span><strong>Port {p.port}</strong></span>
              </label>
              <span className="muted" style={{ fontSize: 12 }}>
                {found?.product_name ? `${found.product_name}` : "not scanned"}
                {found?.vendor_id ? ` · vendor ${found.vendor_id}/${found.device_id}` : ""}
              </span>
              <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
                style={{ marginLeft: "auto" }} onClick={() => doPreview(p.port)}>
                Preview live value
              </button>
            </div>

            {pv ? (
              <div className={`info-note ifm-preview ${pv.ok ? "" : "warn"}`}>
                {pv.ok
                  ? <>Raw <code>{pv.raw}</code> decodes to{" "}
                      {(pv.values || []).map((v, i) => (
                        <span key={i} className="ifm-preview-val">
                          <strong>{v.name}</strong>{" = "}
                          {v.error ? <em>{v.error}</em> : `${v.value} ${v.unit || ""}`}
                        </span>
                      ))}
                    </>
                  : (pv.message || "Could not read this port.")}
              </div>
            ) : null}

            <div className="ifm-field-table">
              <div className="ifm-field-head">
                <span>Tag name</span><span>Bit offset</span><span>Bits</span>
                <span>Type</span><span>Scale</span><span>Offset</span><span>Unit</span><span />
              </div>
              {(p.fields || []).map((f, idx) => (
                <div className="ifm-field-row" key={`f-${p.port}-${idx}`}>
                  <input value={f.name || ""} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { name: e.target.value })} />
                  <input type="number" min="0" value={f.bit_offset ?? 0} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { bit_offset: Number(e.target.value || 0) })} />
                  <input type="number" min="1" max="64" value={f.bit_length ?? 16} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { bit_length: Number(e.target.value || 1) })} />
                  <select value={f.kind || "uint"} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { kind: e.target.value })}>
                    {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
                  </select>
                  <input type="number" step="any" value={f.scale ?? 1} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { scale: Number(e.target.value || 1) })} />
                  <input type="number" step="any" value={f.offset ?? 0} disabled={disabled}
                    onChange={(e) => patchField(p.port, idx, { offset: Number(e.target.value || 0) })} />
                  <input value={f.unit || ""} disabled={disabled} placeholder="degC"
                    onChange={(e) => patchField(p.port, idx, { unit: e.target.value })} />
                  <button type="button" className="btn btn-danger btn-sm" disabled={disabled}
                    onClick={() => patchPorts(ports.map((x) => (Number(x.port) !== Number(p.port) ? x : {
                      ...x, fields: (x.fields || []).filter((_, i) => i !== idx),
                    })))}>Remove</button>
                </div>
              ))}
              <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
                style={{ marginTop: 6 }}
                onClick={() => patchPorts(ports.map((x) => (Number(x.port) !== Number(p.port) ? x : {
                  ...x, fields: [...(x.fields || []), blankField(p.port, (x.fields || []).length)],
                })))}>
                Add value
              </button>
            </div>
          </div>
        );
      }) : null}

      {!datapoints.length && !ports.length ? (
        <div className="info-note" style={{ marginTop: 8 }}>
          Nothing selected yet. Enter the block's address above and click
          <strong> Scan block</strong> — it will report what it can measure and tick
          everything for you.
        </div>
      ) : null}
    </div>
  );
}
