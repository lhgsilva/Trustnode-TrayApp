/* The device catalogue — pick the device you have, get its tags.

   The same step Ignition, KEPServerEX and Studio 5000 all put in front of you:
   choose a known device and its tag list appears, instead of typing addresses
   from a manual. Shared by every protocol mapper, so one picker serves Modbus,
   EtherNet/IP and whatever comes next.

   The `verified` badge is the important part. On Modbus and CIP alike a wrong
   address returns a plausible NUMBER rather than an error, so a profile nobody
   has proven against hardware is a hypothesis, not a fact. Unverified profiles
   apply with their tags UNTICKED — nothing is collected until the operator has
   read the values live and agreed with them. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { listDeviceProfiles } from "../../api";

export default function DeviceCatalogue({ protocol, disabled = false, onApply }) {
  const [profiles, setProfiles] = useState([]);
  const [selected, setSelected] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stopped = false;
    (async () => {
      try {
        const res = await listDeviceProfiles(protocol);
        if (!stopped) setProfiles(Array.isArray(res?.profiles) ? res.profiles : []);
      } catch (err) {
        if (!stopped) setNote(String(err?.message || err));
      }
    })();
    return () => { stopped = true; };
  }, [protocol]);

  const chosen = useMemo(
    () => profiles.find((p) => p.id === selected) || null,
    [profiles, selected]
  );

  const apply = useCallback(() => {
    if (!chosen || !onApply) return;
    setBusy(true);
    try {
      onApply(chosen);
      setNote(
        chosen.verified
          ? `Applied ${chosen.tag_count} tag(s) from ${chosen.model}. `
            + `Use "Read live" to confirm before saving.`
          : `Applied ${chosen.tag_count} tag(s) from ${chosen.model}, all UNTICKED. `
            + `This profile has not been verified against hardware here — read the `
            + `values live and compare them with the device's own display, then tick `
            + `the ones that are right.`
      );
    } finally { setBusy(false); }
  }, [chosen, onApply]);

  if (!profiles.length) return null;

  // Grouped so a long catalogue stays navigable as it grows.
  const groups = profiles.reduce((acc, p) => {
    const key = p.category || "Other";
    (acc[key] = acc[key] || []).push(p);
    return acc;
  }, {});

  return (
    <div className="device-catalogue">
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 13 }}>Device catalogue</strong>
        <select value={selected} disabled={disabled}
          style={{ minWidth: 260 }}
          onChange={(e) => { setSelected(e.target.value); setNote(""); }}>
          <option value="">Choose a device…</option>
          {Object.entries(groups).map(([category, items]) => (
            <optgroup key={category} label={category}>
              {items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.manufacturer} {p.model} {p.verified ? "✓" : "(unverified)"}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <button type="button" className="btn btn-primary btn-sm"
          disabled={disabled || busy || !chosen} onClick={apply}>
          Apply profile
        </button>
        {chosen ? (
          <span className={`catalogue-badge ${chosen.verified ? "ok" : "warn"}`}>
            {chosen.verified ? "Verified on hardware" : "Not verified"}
          </span>
        ) : null}
      </div>

      {chosen?.notes ? (
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4, lineHeight: 1.45 }}>
          {chosen.notes}
        </div>
      ) : null}
      {note ? <div className="info-note" style={{ marginTop: 6 }}>{note}</div> : null}
    </div>
  );
}
