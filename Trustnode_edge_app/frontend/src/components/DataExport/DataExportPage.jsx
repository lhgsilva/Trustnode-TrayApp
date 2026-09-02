// Data Export — a guided query over the historian.
//
// 2026-08-31: "a new sub menu under data history called Data export... a full
// and complete assistant to export data from the app, selecting gateways,
// devices, tags, data range, other columns conditions, complete filtering
// system conditions, aggregation and filter features. preview the data
// format, including pivot based on the time stamp... cannot break the
// historian, it is only a query assistant to the database."
//
// LAYOUT, after the first pass was reviewed: "all the configuration on the top
// of the page, gateways devices and other should be a combobox, if there is
// nothing configured will be just empty... using width space and action button
// on the right. should be only one card on the top, then texts in subgroups
// better arranged... then a second card to show the data previewed below,
// expanding with the data."
//
// So: ONE control card, laid out as rows of full-width columns with the
// actions right-aligned - the shape of the Historian export toolbar the
// operator pointed at - and a second card that appears only once there is
// something to show, taking the rest of the height.
//
// Its own component and its own backend router, so nothing here can slow the
// historian read path that every chart and widget shares.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import "./dataExport.css";

const QUALITY_OPTIONS = [
  { value: "all", label: "All readings" },
  { value: "good", label: "Good only" },
  { value: "bad", label: "Bad only" },
];

const FORMATS = [
  { value: "csv", label: "CSV" },
  { value: "txt", label: "Text (tab separated)" },
  { value: "json", label: "JSON" },
];

const EMPTY_SPEC = {
  gateways: [], devices: [], tags: [], tag_contains: "",
  from_utc: "", to_utc: "", quality: "all",
  columns: [], conditions: [], bucket: "", aggregate: "",
  pivot: false, order: "asc", include_header: true,
};

/* A combo box over one filter. Multi-select is expressed as "All X" plus the
   individual values, because an operator picking one gateway is the common
   case and a chip cloud made the card tall before it had said anything.
   Nothing configured simply means the list is empty, which is a legitimate
   state and not an error. */
function FilterSelect({ label, options, value, onChange, allLabel, disabled }) {
  return (
    <label className="dexp-field">
      <span>{label}</span>
      <select
        value={value || ""}
        disabled={disabled || options.length === 0}
        onChange={(e) => onChange(e.target.value)}
        title={options.length === 0 ? "Nothing recorded in the historian yet" : ""}
      >
        <option value="">{options.length === 0 ? "— none recorded —" : allLabel}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </label>
  );
}

export default function DataExportPage({ api, isCloudClient = false, onError }) {
  const [spec, setSpec] = useState(EMPTY_SPEC);
  const [options, setOptions] = useState(null);
  const [sources, setSources] = useState(null);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [format, setFormat] = useState("csv");
  const [showColumns, setShowColumns] = useState(false);

  const patch = useCallback((p) => setSpec((prev) => ({ ...prev, ...p })), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [opt, src] = await Promise.all([api.exportOptions(), api.exportSources()]);
        if (cancelled) return;
        setOptions(opt || null);
        setSources(src || null);
        if (opt?.default_columns?.length) patch({ columns: opt.default_columns });
      } catch (err) {
        if (!cancelled) setStatus(`Could not load export options: ${String(err?.message || err)}`);
      }
    })();
    return () => { cancelled = true; };
  }, [api, patch]);

  const gatewayOptions = useMemo(() => {
    const names = sources?.gateway_names || {};
    return (sources?.gateways || []).map((id) => ({ value: id, label: names[id] || id }));
  }, [sources]);
  const tagOptions = useMemo(
    () => (sources?.tags || []).map((t) => ({ value: t, label: t })), [sources]);
  const deviceOptions = useMemo(
    () => (sources?.devices || []).map((d) => ({ value: d, label: d })), [sources]);

  // The spec carries lists; the combo boxes carry one value each. "" means all.
  const one = (key) => (Array.isArray(spec[key]) && spec[key].length === 1 ? spec[key][0] : "");
  const setOne = (key) => (v) => patch({ [key]: v ? [v] : [] });

  const runPreview = useCallback(async () => {
    setBusy(true);
    setStatus("Running preview…");
    try {
      const res = await api.exportPreview(spec);
      setPreview(res || null);
      setStatus(res?.ok === false
        ? `Preview failed: ${res.error || "unknown error"}`
        : `${Number(res?.total_rows || 0).toLocaleString()} row(s) match — showing `
          + `${Number(res?.preview_rows || 0).toLocaleString()}.`);
    } catch (err) {
      setStatus(`Preview failed: ${String(err?.message || err)}`);
      if (onError) onError(err);
    } finally {
      setBusy(false);
    }
  }, [api, spec, onError]);

  const runExport = useCallback(async () => {
    setBusy(true);
    setStatus("Exporting…");
    try {
      // Local edge: the backend streams the file. Cloud client: page it here,
      // because the stream would cross the internet twice to reach this same
      // browser. The operator's rule, 2026-08-31.
      const result = isCloudClient
        ? await api.exportClientSide(spec, format, (n) =>
            setStatus(`Exporting… ${n.toLocaleString()} row(s)`))
        : await api.exportServerSide(spec, format);
      setStatus(result?.message || "Export complete.");
    } catch (err) {
      setStatus(`Export failed: ${String(err?.message || err)}`);
      if (onError) onError(err);
    } finally {
      setBusy(false);
    }
  }, [api, spec, format, isCloudClient, onError]);

  const clearAll = useCallback(() => {
    setSpec({ ...EMPTY_SPEC, columns: options?.default_columns || [] });
    setPreview(null);
    setStatus("");
  }, [options]);

  const columns = preview?.columns || [];
  const rows = preview?.rows || [];

  return (
    <div className="dexp-page">
      {/* ── ONE control card, everything above the preview ─────────────── */}
      <section className="card dexp-controls">
        <div className="dexp-row">
          <label className="dexp-field">
            <span>From</span>
            <input type="datetime-local" value={spec.from_utc}
              onChange={(e) => patch({ from_utc: e.target.value })} />
          </label>
          <label className="dexp-field">
            <span>To</span>
            <input type="datetime-local" value={spec.to_utc}
              onChange={(e) => patch({ to_utc: e.target.value })} />
          </label>
          <FilterSelect label="Gateway" options={gatewayOptions} allLabel="All gateways"
            value={one("gateways")} onChange={setOne("gateways")} />
          <FilterSelect label="Tag" options={tagOptions} allLabel="All tags"
            value={one("tags")} onChange={setOne("tags")} />
          <FilterSelect label="Device" options={deviceOptions} allLabel="All devices"
            value={one("devices")} onChange={setOne("devices")} />
          <label className="dexp-field">
            <span>Quality</span>
            <select value={spec.quality} onChange={(e) => patch({ quality: e.target.value })}>
              {QUALITY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <div className="dexp-actions">
            <button type="button" className="btn" onClick={runPreview} disabled={busy}>
              {busy ? "Working…" : "Preview"}
            </button>
            <button type="button" className="btn btn-ghost" onClick={clearAll} disabled={busy}>
              Clear
            </button>
            <button type="button" className="btn btn-primary" onClick={runExport}
              disabled={busy || !preview || preview.ok === false}>
              Export
            </button>
          </div>
        </div>

        <div className="dexp-subgroup">
          <span className="dexp-subgroup-title">Shape</span>
          <div className="dexp-row">
            <label className="dexp-field">
              <span>Aggregate</span>
              <select value={spec.aggregate}
                onChange={(e) => patch({ aggregate: e.target.value })}>
                <option value="">None — every reading</option>
                {(options?.aggregates || []).map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <label className="dexp-field">
              <span>Bucket</span>
              <select value={spec.bucket} disabled={!spec.aggregate}
                onChange={(e) => patch({ bucket: e.target.value })}>
                <option value="">—</option>
                {(options?.buckets || []).map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </label>
            <label className="dexp-field">
              <span>Layout</span>
              <select value={spec.pivot ? "pivot" : "long"}
                onChange={(e) => patch({ pivot: e.target.value === "pivot" })}>
                <option value="long">Long — one row per reading</option>
                <option value="pivot">Pivot — a column per tag</option>
              </select>
            </label>
            <label className="dexp-field">
              <span>Order</span>
              <select value={spec.order} onChange={(e) => patch({ order: e.target.value })}>
                <option value="asc">Oldest first</option>
                <option value="desc">Newest first</option>
              </select>
            </label>
            <label className="dexp-field">
              <span>Format</span>
              <select value={format} onChange={(e) => setFormat(e.target.value)}>
                {FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </label>
            <label className="dexp-field">
              <span>Header row</span>
              <select value={spec.include_header ? "yes" : "no"}
                onChange={(e) => patch({ include_header: e.target.value === "yes" })}>
                <option value="yes">Include</option>
                <option value="no">Omit</option>
              </select>
            </label>
          </div>
        </div>

        <div className="dexp-subgroup">
          <span className="dexp-subgroup-title">Filter</span>
          {/* Three cells of the same shape - caption, then one control of the
              same height - so the row lines up and shares its width. */}
          <div className="dexp-row dexp-row-filter">
            <label className="dexp-field">
              <span>Tag name contains</span>
              <input value={spec.tag_contains} placeholder="optional, e.g. Temp"
                onChange={(e) => patch({ tag_contains: e.target.value })} />
            </label>

            <div className="dexp-field">
              <span>Value conditions</span>
              <button type="button" className="btn dexp-control"
                onClick={() => patch({
                  conditions: [...(spec.conditions || []), { op: "gte", value: 0 }],
                })}>
                {(spec.conditions || []).length
                  ? `${spec.conditions.length} condition${spec.conditions.length > 1 ? "s" : ""} — add another`
                  : "Add condition"}
              </button>
            </div>

            <div className="dexp-field">
              <span>Columns</span>
              <button type="button" className="btn dexp-control"
                disabled={Boolean(spec.pivot || spec.aggregate)}
                title={spec.pivot || spec.aggregate
                  ? "Aggregated and pivoted exports define their own columns"
                  : "Choose which columns the file contains"}
                onClick={() => setShowColumns((v) => !v)}>
                {spec.pivot || spec.aggregate
                  ? "set by the layout"
                  : `${(spec.columns || []).length} selected`}
              </button>
            </div>
          </div>

          {/* Expanders sit BELOW the row, so opening one cannot shove the
              aligned cells out of line. */}
          {(spec.conditions || []).length ? (
            <ConditionEditor
              conditions={spec.conditions}
              operators={options?.operators || []}
              onChange={(conditions) => patch({ conditions })}
            />
          ) : null}
          {showColumns && !spec.pivot && !spec.aggregate ? (
            <div className="dexp-chips">
              {(options?.columns || []).map((c) => {
                const on = (spec.columns || []).includes(c);
                return (
                  <button type="button" key={c}
                    className={`btn btn-sm ${on ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => patch({
                      columns: on ? spec.columns.filter((x) => x !== c)
                        : [...(spec.columns || []), c],
                    })}>
                    {c}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>

        {status ? <p className="dexp-status">{status}</p> : null}
        {preview?.truncated ? (
          <p className="dexp-status">
            Showing the first {Number(preview.preview_rows || 0).toLocaleString()} of{" "}
            {Number(preview.total_rows || 0).toLocaleString()} matching rows. The export
            contains all of them.
          </p>
        ) : null}
      </section>

      {/* ── the preview, only once there is something to show ──────────── */}
      {rows.length ? (
        <section className="card dexp-preview">
          <h4 className="wcfg-card-title">
            Preview · {Number(preview?.total_rows || 0).toLocaleString()} row(s) match
          </h4>
          <div className="dexp-preview-scroll">
            <table className="data-table">
              <thead>
                <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
              </thead>
              <tbody>
                {rows.slice(0, 200).map((r, i) => (
                  <tr key={i}>
                    {columns.map((c) => (
                      <td key={c}>{r[c] === null || r[c] === undefined ? "" : String(r[c])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}

/* Value conditions. Deliberately small: an operator wants "value above 100",
   not a query language. Several conditions are ANDed. */
function ConditionEditor({ conditions, operators, onChange }) {
  const list = Array.isArray(conditions) ? conditions : [];
  const set = (idx, p) => onChange(list.map((c, i) => (i === idx ? { ...c, ...p } : c)));
  return (
    <div className="dexp-conditions">
      {list.map((c, idx) => (
        <div className="dexp-condition" key={idx}>
          <span className="dexp-hint">value</span>
          <select value={c.op} onChange={(e) => set(idx, { op: e.target.value })}>
            {operators.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <input type="number" step="any" value={c.value}
            onChange={(e) => set(idx, { value: e.target.value })} />
          <button type="button" className="btn btn-sm btn-danger"
            onClick={() => onChange(list.filter((_, i) => i !== idx))}>
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
