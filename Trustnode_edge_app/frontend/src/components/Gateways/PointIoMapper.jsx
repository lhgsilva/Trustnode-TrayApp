// POINT I/O rack: scan it, then decide what each point is called, whether it
// is collected, and how its raw value becomes an engineering one.
//
// Scaling is applied at the READING, so what lands in the historian - and
// therefore on every chart - is 10.0 mA rather than 8191 counts. The suggested
// scale comes from the module's own name and is shown as a suggestion, because
// the data format is a per-module setting: on this rack a generator at
// 10.000 mA read 8191 counts (0-20 mA over 0..16383), but a card configured
// differently would need a different number, and a silently wrong scale is
// worse than an obviously raw one.
import React, { useState } from "react";
import { scanPointIoRack } from "../../api";

export default function PointIoMapper({ form, disabled, onChange }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const modules = Array.isArray(form?.point_io_modules) ? form.point_io_modules : [];
  const points = Array.isArray(form?.point_io_points) ? form.point_io_points : [];

  const patchPoint = (addr, patch) => {
    const next = points.map((p) =>
      String(p.address || p.name) === addr ? { ...p, ...patch } : p);
    onChange({
      point_io_points: next,
      // `tags` is what the worker collects; keep it in step with the ticks so
      // unticking a point actually stops it being read.
      tags_text: next.filter((p) => p.enabled !== false)
                     .map((p) => String(p.name || p.address)).join(";"),
    });
  };

  const scan = async () => {
    setBusy(true);
    setNotice("Scanning the backplane…");
    try {
      const r = await scanPointIoRack(String(form?.plc_ip || "").trim());
      if (!r?.ok) {
        setNotice(String(r?.message || "scan failed"));
      } else {
        const dps = r.datapoints || [];
        onChange({
          point_io_modules: r.modules || [],
          point_io_points: dps,
          // ";" is the separator the gateway form parses. Joining with a
          // newline produced ONE tag holding every point name, and a gateway
          // that collected nothing.
          tags_text: dps.map((d) => d.name).join(";"),
        });
        setNotice(String(r.message || ""));
      }
    } catch (err) {
      setNotice(`Scan failed: ${String(err?.message || err)}`);
    } finally {
      setBusy(false);
    }
  };

  const analogSuggestion = points.find((p) => p.scale_source && p.scale_source !== "none");

  return (
    <div className="form-grid" style={{ gridColumn: "1 / -1" }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <button
          type="button"
          className="btn"
          disabled={disabled || busy || !String(form?.plc_ip || "").trim()}
          onClick={scan}
        >
          {busy ? "Scanning…" : "Scan rack"}
        </button>
        {notice ? <span className="muted" style={{ fontSize: 12 }}>{notice}</span> : null}
      </div>

      {modules.length ? (
        <div className="db-table" style={{ gridColumn: "1 / -1" }}>
          <div className="trow thead">
            <span>Slot</span><span>Module</span><span>Points</span><span>Status</span>
          </div>
          {modules.map((m) => (
            <div className="trow" key={m.slot}>
              <span>{m.slot}</span>
              <span>{m.name}</span>
              <span>{m.points || 0}</span>
              <span title={m.unreadable || ""}>
                {m.unreadable ? "not readable" : (m.health?.healthy ? "OK" : "check")}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {points.length ? (
        <>
          <div className="muted" style={{ gridColumn: "1 / -1", fontSize: 12 }}>
            Untick a point to stop collecting it. The scale is applied before the
            value is stored, so charts and reports show engineering units.
            {analogSuggestion ? ` Analog scale ${analogSuggestion.scale_source}.` : ""}
          </div>
          <div className="db-table pio-points" style={{ gridColumn: "1 / -1" }}>
            <div className="trow thead">
              <span>Collect</span><span>Terminal</span><span>Tag name</span>
              <span>Type</span><span>Scale</span><span>Offset</span><span>Unit</span>
            </div>
            {points.map((p) => {
              const addr = String(p.address || p.name);
              const analog = String(p.channel || "").startsWith("A");
              return (
                <div className="trow" key={addr}>
                  <span>
                    <input
                      type="checkbox"
                      disabled={disabled}
                      checked={p.enabled !== false}
                      onChange={(e) => patchPoint(addr, { enabled: e.target.checked })}
                    />
                  </span>
                  <span title={p.source || ""}>{addr}</span>
                  <span>
                    <input
                      value={String(p.name || "")}
                      disabled={disabled}
                      onChange={(e) => patchPoint(addr, { name: e.target.value })}
                    />
                  </span>
                  <span>{p.channel} · {p.kind}</span>
                  <span>
                    {/* Scale only means something for an analog channel; a
                        discrete point is already 0 or 1. */}
                    {analog ? (
                      <input
                        value={String(p.scale ?? 1)}
                        disabled={disabled}
                        onChange={(e) => patchPoint(addr, { scale: e.target.value })}
                      />
                    ) : <span className="muted">—</span>}
                  </span>
                  <span>
                    {analog ? (
                      <input
                        value={String(p.offset ?? 0)}
                        disabled={disabled}
                        onChange={(e) => patchPoint(addr, { offset: e.target.value })}
                      />
                    ) : <span className="muted">—</span>}
                  </span>
                  <span>
                    {analog ? (
                      <input
                        value={String(p.unit || "")}
                        disabled={disabled}
                        onChange={(e) => patchPoint(addr, { unit: e.target.value })}
                      />
                    ) : <span className="muted">—</span>}
                  </span>
                </div>
              );
            })}
          </div>
        </>
      ) : null}
    </div>
  );
}
