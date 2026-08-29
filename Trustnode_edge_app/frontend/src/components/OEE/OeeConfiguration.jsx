/* OEE Configuration — the eight sections from the spec.

   Every reference to the plant (gateway, device, tag, power meter) is CHOSEN
   from what the collection system already has; nothing here creates a device.
   The machine's feature toggles decide which sections are even shown, so a
   machine with no power meter never sees power rules.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  oeeList, oeeSave, oeeDelete, oeeMeta,
} from "../../api";
import {
  Section, Toggle, TagPicker, EmptyState, FUNCTION_LABELS, CONDITION_LABELS,
  STATE_LABELS, num,
} from "./OeeShared";

const SECTIONS = [
  ["machines", "Machines"],
  ["signal_mapping", "Signal mapping"],
  ["power_mapping", "Power meter mapping"],
  ["power_rules", "Power state rules"],
  ["products", "Products, cycles & orders"],
  ["shifts", "Shifts & planned stops"],
  ["downtime", "Downtime reasons"],
  ["quality", "Quality / reject reasons"],
];

export default function OeeConfigurationPage({
  canEdit = false, gatewayConfigs = [], devices = [],
}) {
  const [open, setOpen] = useState({ machines: true });
  const [meta, setMeta] = useState(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const [machines, setMachines] = useState([]);
  const [selectedMachine, setSelectedMachine] = useState("");
  const [signalMaps, setSignalMaps] = useState([]);
  const [powerMaps, setPowerMaps] = useState([]);
  const [powerRules, setPowerRules] = useState([]);
  const [products, setProducts] = useState([]);
  const [orders, setOrders] = useState([]);
  const [shifts, setShifts] = useState([]);
  const [plannedStops, setPlannedStops] = useState([]);
  const [downtime, setDowntime] = useState([]);
  const [quality, setQuality] = useState([]);

  const toggleSection = (id) => setOpen((p) => ({ ...p, [id]: !p[id] }));

  const reload = useCallback(async () => {
    try {
      const [m, p, o, s, ps, d, q] = await Promise.all([
        oeeList("machines"), oeeList("products"), oeeList("orders"),
        oeeList("shifts"), oeeList("planned_stops"),
        oeeList("downtime_reasons"), oeeList("quality_reasons"),
      ]);
      setMachines(m.items || []);
      setProducts(p.items || []);
      setOrders(o.items || []);
      setShifts(s.items || []);
      setPlannedStops(ps.items || []);
      setDowntime(d.items || []);
      setQuality(q.items || []);
      setSelectedMachine((prev) => prev || (m.items?.[0]?.id || ""));
      setError("");
    } catch (e) { setError(String(e?.message || e)); }
  }, []);

  const reloadMachineScoped = useCallback(async (mid) => {
    if (!mid) { setSignalMaps([]); setPowerMaps([]); setPowerRules([]); return; }
    try {
      const [sm, pm, pr] = await Promise.all([
        oeeList("signal_mappings", mid), oeeList("power_meter_mappings", mid),
        oeeList("power_state_rules", mid),
      ]);
      setSignalMaps(sm.items || []);
      setPowerMaps(pm.items || []);
      setPowerRules(pr.items || []);
    } catch (e) { setError(String(e?.message || e)); }
  }, []);

  useEffect(() => { reload(); oeeMeta().then(setMeta).catch(() => {}); }, [reload]);
  useEffect(() => { reloadMachineScoped(selectedMachine); },
            [selectedMachine, reloadMachineScoped]);

  const machine = useMemo(
    () => machines.find((m) => m.id === selectedMachine) || null,
    [machines, selectedMachine]);

  const save = useCallback(async (kind, payload, after) => {
    setError(""); setNote("");
    try {
      await oeeSave(kind, payload);
      setNote("Saved.");
      setTimeout(() => setNote(""), 2500);
      await reload();
      if (after) await after();
    } catch (e) { setError(String(e?.message || e)); }
  }, [reload]);

  const remove = useCallback(async (kind, id, after) => {
    setError("");
    try {
      await oeeDelete(kind, id);
      await reload();
      if (after) await after();
    } catch (e) { setError(String(e?.message || e)); }
  }, [reload]);

  const ro = !canEdit;

  return (
    <div className="oee-page oee-config">
      {error ? <div className="error">{error}</div> : null}
      {note ? <div className="ok-note">{note}</div> : null}
      {ro ? (
        <div className="info-note">
          You have read-only access to OEE configuration.
        </div>
      ) : null}

      {/* =================================================== 1. Machines */}
      <Section
        title="1. Machines" count={machines.length} open={open.machines}
        onToggle={() => toggleSection("machines")}
        subtitle="A machine is only measured when OEE is enabled for it"
        actions={
          <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                  onClick={() => save("machines", {
                    name: `Machine ${machines.length + 1}`,
                    oee_enabled: true, signal_enabled: true, manual_enabled: true,
                    power_enabled: false, default_status_source: "signal",
                    enabled: true,
                  })}>
            Add machine
          </button>
        }
      >
        {machines.length === 0 ? (
          <EmptyState title="No machines yet">
            Add one, then map it to a gateway tag you already collect.
          </EmptyState>
        ) : (
          <div className="oee-machine-editor">
            <div className="oee-machine-list">
              {machines.map((m) => (
                <button key={m.id} type="button"
                        className={`oee-machine-item ${m.id === selectedMachine ? "active" : ""}`}
                        onClick={() => setSelectedMachine(m.id)}>
                  <strong>{m.name}</strong>
                  <span className="muted">{m.line || "—"}</span>
                  {!m.oee_enabled ? <span className="oee-off-badge">OEE off</span> : null}
                </button>
              ))}
            </div>
            {machine ? (
              <div className="oee-machine-form">
                <div className="oee-form-grid">
                  <label>Machine name
                    <input value={machine.name || ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, name: e.target.value } : x))} /></label>
                  <label>Machine ID / code
                    <input value={machine.machine_code || ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, machine_code: e.target.value } : x))} /></label>
                  <label>Line
                    <input value={machine.line || ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, line: e.target.value } : x))} /></label>
                  <label>Area
                    <input value={machine.area || ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, area: e.target.value } : x))} /></label>
                  <label className="oee-span-2">Description
                    <input value={machine.description || ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, description: e.target.value } : x))} /></label>
                  <label>Default status source
                    <select value={machine.default_status_source || "signal"} disabled={ro}
                            onChange={(e) => setMachines((p) => p.map(
                              (x) => x.id === machine.id ? { ...x, default_status_source: e.target.value } : x))}>
                      <option value="signal">PLC / sensor</option>
                      <option value="power">Power meter</option>
                      <option value="manual">Manual</option>
                      <option value="combined">Combined</option>
                    </select></label>
                  <label>Fallback ideal cycle time (s)
                    <input type="number" step="any" value={machine.ideal_cycle_time_s ?? ""} disabled={ro}
                           title="Used when the running product has none of its own."
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, ideal_cycle_time_s: e.target.value } : x))} /></label>
                  <label>Standby power allowance (kW)
                    <input type="number" step="any" value={machine.standby_power_kw ?? ""} disabled={ro}
                           title="Power above this while stopped counts as wasted energy."
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, standby_power_kw: e.target.value } : x))} /></label>
                  <label>Idle power allowance (kW)
                    <input type="number" step="any" value={machine.idle_power_kw ?? ""} disabled={ro}
                           onChange={(e) => setMachines((p) => p.map(
                             (x) => x.id === machine.id ? { ...x, idle_power_kw: e.target.value } : x))} /></label>
                </div>

                <div className="oee-toggle-row">
                  <Toggle label="OEE enabled" checked={machine.oee_enabled} disabled={ro}
                          hint="Off: this machine is excluded from every OEE calculation."
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, oee_enabled: v } : x))} />
                  <Toggle label="PLC / sensor monitoring" checked={machine.signal_enabled} disabled={ro}
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, signal_enabled: v } : x))} />
                  <Toggle label="Power meter monitoring" checked={machine.power_enabled} disabled={ro}
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, power_enabled: v } : x))} />
                  <Toggle label="Manual operator input" checked={machine.manual_enabled} disabled={ro}
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, manual_enabled: v } : x))} />
                  <Toggle label="Allow over 100%" checked={machine.allow_over_100} disabled={ro}
                          hint="Leave off. On, a wrong ideal cycle time can report OEE above 100%."
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, allow_over_100: v } : x))} />
                  <Toggle label="Machine enabled" checked={machine.enabled} disabled={ro}
                          onChange={(v) => setMachines((p) => p.map(
                            (x) => x.id === machine.id ? { ...x, enabled: v } : x))} />
                </div>

                <div className="row" style={{ gap: 8 }}>
                  <button type="button" className="btn btn-primary" disabled={ro}
                          onClick={() => save("machines", machine,
                                              () => reloadMachineScoped(machine.id))}>
                    Save machine
                  </button>
                  <button type="button" className="btn btn-danger btn-sm" disabled={ro}
                          onClick={() => remove("machines", machine.id,
                                                () => setSelectedMachine(""))}>
                    Delete
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        )}
      </Section>

      {machine ? (
        <div className="info-note oee-scope-note">
          Sections 2–4 below configure <strong>{machine.name}</strong>. Pick a
          different machine above to configure that one.
        </div>
      ) : null}

      {/* ============================================= 2. Signal mapping */}
      {machine?.signal_enabled ? (
        <Section
          title="2. Signal mapping" count={signalMaps.length}
          open={open.signal_mapping} onToggle={() => toggleSection("signal_mapping")}
          subtitle="Existing PLC / sensor tags given an OEE meaning"
          actions={
            <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                    onClick={() => save("signal_mappings", {
                      machine_id: machine.id, enabled: true, source_type: "plc",
                      oee_function: "running_status", condition_op: "truthy",
                      priority: 100,
                    }, () => reloadMachineScoped(machine.id))}>
              Add mapping
            </button>
          }
        >
          {signalMaps.length === 0 ? (
            <EmptyState title="No signals mapped">
              Map at least a running status, and ideally a total count, so
              availability and performance can be measured.
            </EmptyState>
          ) : signalMaps.map((sm) => (
            <div key={sm.id} className="oee-row-card">
              <div className="oee-row-head">
                <Toggle label="Enabled" checked={sm.enabled} disabled={ro}
                        onChange={(v) => setSignalMaps((p) => p.map(
                          (x) => x.id === sm.id ? { ...x, enabled: v } : x))} />
                <select value={sm.source_type || "plc"} disabled={ro}
                        onChange={(e) => setSignalMaps((p) => p.map(
                          (x) => x.id === sm.id ? { ...x, source_type: e.target.value } : x))}>
                  {(meta?.source_types || ["plc", "sensor", "manual", "energy_meter"]).map(
                    (t) => <option key={t} value={t}>{t}</option>)}
                </select>
                <select value={sm.oee_function || "running_status"} disabled={ro}
                        onChange={(e) => setSignalMaps((p) => p.map(
                          (x) => x.id === sm.id ? { ...x, oee_function: e.target.value } : x))}>
                  {(meta?.oee_functions || Object.keys(FUNCTION_LABELS)).map(
                    (f) => <option key={f} value={f}>{FUNCTION_LABELS[f] || f}</option>)}
                </select>
                <button type="button" className="btn btn-danger btn-sm" disabled={ro}
                        onClick={() => remove("signal_mappings", sm.id,
                                              () => reloadMachineScoped(machine.id))}>
                  Remove
                </button>
              </div>

              <TagPicker
                gateways={gatewayConfigs} devices={devices} disabled={ro}
                value={{ gateway_id: sm.gateway_id, tag: sm.tag_name }}
                onChange={(patch) => setSignalMaps((p) => p.map((x) => x.id === sm.id
                  ? { ...x,
                      ...(patch.gateway_id !== undefined ? { gateway_id: patch.gateway_id } : {}),
                      ...(patch.device_id !== undefined ? { device_id: patch.device_id } : {}),
                      ...(patch.tag !== undefined ? { tag_name: patch.tag } : {}),
                      ...(patch.tag_name !== undefined ? { tag_name: patch.tag_name } : {}) }
                  : x))}
              />

              <div className="oee-form-grid">
                <label>Condition
                  <select value={sm.condition_op || "truthy"} disabled={ro}
                          onChange={(e) => setSignalMaps((p) => p.map(
                            (x) => x.id === sm.id ? { ...x, condition_op: e.target.value } : x))}>
                    {(meta?.condition_ops || Object.keys(CONDITION_LABELS)).map(
                      (o) => <option key={o} value={o}>{CONDITION_LABELS[o] || o}</option>)}
                  </select></label>
                <label>Value
                  <input value={sm.condition_value ?? ""} disabled={ro}
                         placeholder="only for equals / greater / less"
                         onChange={(e) => setSignalMaps((p) => p.map(
                           (x) => x.id === sm.id ? { ...x, condition_value: e.target.value } : x))} /></label>
                <label>Hold (seconds)
                  <input type="number" min="0" value={sm.hold_seconds ?? 0} disabled={ro}
                         title="The condition must stay true this long. Also the window for 'does not change'."
                         onChange={(e) => setSignalMaps((p) => p.map(
                           (x) => x.id === sm.id ? { ...x, hold_seconds: e.target.value } : x))} /></label>
                <label>Priority
                  <input type="number" value={sm.priority ?? 100} disabled={ro}
                         title="Lower wins when more than one mapping matches."
                         onChange={(e) => setSignalMaps((p) => p.map(
                           (x) => x.id === sm.id ? { ...x, priority: e.target.value } : x))} /></label>
              </div>
              <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                      onClick={() => save("signal_mappings", sm,
                                          () => reloadMachineScoped(machine.id))}>
                Save mapping
              </button>
            </div>
          ))}
          <div className="muted oee-examples">
            Examples — <em>Running</em>: MotorRunning is true, priority 50.
            <em> Faulted</em>: Fault is true, priority 10 (lower wins, so a fault
            beats running). <em>Idle</em>: TotalCount does not change for 300 s.
            <em> Cycle complete</em>: CycleComplete changes 0 → 1 (rising).
          </div>
        </Section>
      ) : null}

      {/* ========================================= 3. Power meter mapping */}
      {machine?.power_enabled ? (
        <Section
          title="3. Power meter mapping" count={powerMaps.length}
          open={open.power_mapping} onToggle={() => toggleSection("power_mapping")}
          subtitle="An existing energy meter assigned to this machine"
          actions={
            powerMaps.length === 0 ? (
              <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                      onClick={() => save("power_meter_mappings",
                                          { machine_id: machine.id, enabled: true },
                                          () => reloadMachineScoped(machine.id))}>
                Add meter
              </button>
            ) : null
          }
        >
          {powerMaps.length === 0 ? (
            <EmptyState title="No meter assigned">
              Assign the meter that feeds this machine to measure energy and to
              use power-based states.
            </EmptyState>
          ) : powerMaps.map((pm) => (
            <div key={pm.id} className="oee-row-card">
              <Toggle label="Enabled" checked={pm.enabled} disabled={ro}
                      onChange={(v) => setPowerMaps((p) => p.map(
                        (x) => x.id === pm.id ? { ...x, enabled: v } : x))} />
              <TagPicker
                gateways={gatewayConfigs} devices={devices} disabled={ro}
                tagLabel="Power tag (kW)"
                value={{ gateway_id: pm.gateway_id, tag: pm.power_tag }}
                onChange={(patch) => setPowerMaps((p) => p.map((x) => x.id === pm.id
                  ? { ...x,
                      ...(patch.gateway_id !== undefined ? { gateway_id: patch.gateway_id } : {}),
                      ...(patch.device_id !== undefined ? { device_id: patch.device_id } : {}),
                      ...(patch.tag !== undefined ? { power_tag: patch.tag } : {}) }
                  : x))}
              />
              <div className="oee-form-grid">
                {[["energy_tag", "Energy tag (kWh)"], ["current_tag", "Current tag (A)"],
                  ["voltage_tag", "Voltage tag (V)"], ["power_factor_tag", "Power factor tag"]].map(
                  ([field, label]) => {
                    const gw = gatewayConfigs.find((g) => String(g.id) === String(pm.gateway_id));
                    const tags = Array.isArray(gw?.tags) ? gw.tags : [];
                    return (
                      <label key={field}>{label}
                        <select value={pm[field] || ""} disabled={ro || !pm.gateway_id}
                                onChange={(e) => setPowerMaps((p) => p.map(
                                  (x) => x.id === pm.id ? { ...x, [field]: e.target.value } : x))}>
                          <option value="">— none —</option>
                          {tags.map((t) => <option key={t} value={t}>{t}</option>)}
                        </select></label>
                    );
                  })}
              </div>
              <div className="row" style={{ gap: 8 }}>
                <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                        onClick={() => save("power_meter_mappings", pm,
                                            () => reloadMachineScoped(machine.id))}>
                  Save meter
                </button>
                <button type="button" className="btn btn-danger btn-sm" disabled={ro}
                        onClick={() => remove("power_meter_mappings", pm.id,
                                              () => reloadMachineScoped(machine.id))}>
                  Remove
                </button>
              </div>
            </div>
          ))}
        </Section>
      ) : null}

      {/* =========================================== 4. Power state rules */}
      {machine?.power_enabled ? (
        <Section
          title="4. Power state rules" count={powerRules.length}
          open={open.power_rules} onToggle={() => toggleSection("power_rules")}
          subtitle="Machine status from power alone — for machines with no PLC"
          actions={
            <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                    onClick={() => save("power_state_rules", {
                      machine_id: machine.id, name: "New rule", enabled: true,
                      measurement: "power_kw", generated_status: "idle",
                      min_duration_s: 60, priority: 100,
                    }, () => reloadMachineScoped(machine.id))}>
              Add rule
            </button>
          }
        >
          <div className="muted oee-examples" style={{ marginBottom: 8 }}>
            Every machine draws power differently, so these bands are per machine.
            Starter rules were created when you enabled power monitoring — tune
            them against this machine's real consumption.
          </div>
          {powerRules.map((r) => (
            <div key={r.id} className="oee-rule-row">
              <Toggle label="" checked={r.enabled} disabled={ro}
                      onChange={(v) => setPowerRules((p) => p.map(
                        (x) => x.id === r.id ? { ...x, enabled: v } : x))} />
              <input className="oee-rule-name" value={r.name || ""} disabled={ro}
                     onChange={(e) => setPowerRules((p) => p.map(
                       (x) => x.id === r.id ? { ...x, name: e.target.value } : x))} />
              <select value={r.measurement || "power_kw"} disabled={ro}
                      onChange={(e) => setPowerRules((p) => p.map(
                        (x) => x.id === r.id ? { ...x, measurement: e.target.value } : x))}>
                <option value="power_kw">Power (kW)</option>
                <option value="current_a">Current (A)</option>
              </select>
              <input type="number" step="any" placeholder="min" value={r.min_value ?? ""}
                     disabled={ro} title="Minimum value (blank = no minimum)"
                     onChange={(e) => setPowerRules((p) => p.map(
                       (x) => x.id === r.id ? { ...x, min_value: e.target.value } : x))} />
              <input type="number" step="any" placeholder="max" value={r.max_value ?? ""}
                     disabled={ro} title="Maximum value (blank = no maximum)"
                     onChange={(e) => setPowerRules((p) => p.map(
                       (x) => x.id === r.id ? { ...x, max_value: e.target.value } : x))} />
              <input type="number" placeholder="hold s" value={r.min_duration_s ?? 0}
                     disabled={ro} title="Must hold for this many seconds"
                     onChange={(e) => setPowerRules((p) => p.map(
                       (x) => x.id === r.id ? { ...x, min_duration_s: e.target.value } : x))} />
              <select value={r.generated_status || "idle"} disabled={ro}
                      onChange={(e) => setPowerRules((p) => p.map(
                        (x) => x.id === r.id ? { ...x, generated_status: e.target.value } : x))}>
                {(meta?.power_statuses || []).map(
                  (s) => <option key={s} value={s}>{STATE_LABELS[s] || s}</option>)}
              </select>
              <input type="number" placeholder="prio" value={r.priority ?? 100}
                     disabled={ro} title="Lower wins"
                     onChange={(e) => setPowerRules((p) => p.map(
                       (x) => x.id === r.id ? { ...x, priority: e.target.value } : x))} />
              <button type="button" className="btn btn-primary btn-sm" disabled={ro}
                      onClick={() => save("power_state_rules", r,
                                          () => reloadMachineScoped(machine.id))}>Save</button>
              <button type="button" className="btn btn-danger btn-sm" disabled={ro}
                      onClick={() => remove("power_state_rules", r.id,
                                            () => reloadMachineScoped(machine.id))}>×</button>
            </div>
          ))}
        </Section>
      ) : null}

      {/* ========================== 5. Products / cycles / orders ======== */}
      <Section
        title="5. Products, cycles & orders"
        count={products.length + orders.length} open={open.products}
        onToggle={() => toggleSection("products")}
        subtitle="Ideal cycle time lives here — performance cannot be measured without it"
      >
        <h4 className="oee-subhead">Products</h4>
        <SimpleTable
          rows={products} disabled={ro}
          columns={[
            { key: "name", label: "Product name" },
            { key: "product_code", label: "Code" },
            { key: "sku", label: "SKU / part no." },
            { key: "ideal_cycle_time_s", label: "Ideal cycle (s)", type: "number" },
            { key: "standard_rate_per_hour", label: "Rate / h", type: "number" },
            { key: "unit", label: "Unit" },
          ]}
          onChange={setProducts}
          onSave={(row) => save("products", row)}
          onDelete={(row) => remove("products", row.id)}
          onAdd={() => save("products", { name: "New product", unit: "pcs", enabled: true })}
          addLabel="Add product"
        />

        <h4 className="oee-subhead">Orders</h4>
        <SimpleTable
          rows={orders} disabled={ro}
          columns={[
            { key: "order_number", label: "Order number" },
            { key: "machine_id", label: "Machine", type: "select",
              options: machines.map((m) => ({ value: m.id, label: m.name })) },
            { key: "product_id", label: "Product", type: "select",
              options: products.map((p) => ({ value: p.id, label: p.name })) },
            { key: "target_quantity", label: "Target qty", type: "number" },
            { key: "status", label: "Status", type: "select",
              options: (meta?.order_statuses || ["planned", "running", "completed"])
                .map((s) => ({ value: s, label: s })) },
          ]}
          onChange={setOrders}
          onSave={(row) => save("orders", row)}
          onDelete={(row) => remove("orders", row.id)}
          onAdd={() => save("orders", { order_number: `ORD-${orders.length + 1}`,
                                        status: "planned" })}
          addLabel="Add order"
        />
        <div className="muted oee-examples">
          Cycles are recorded automatically from a mapped cycle-start / cycle-stop
          tag, or by the operator on the Operator Screen. They are listed there
          rather than here, because they are data, not configuration.
        </div>
      </Section>

      {/* ============================= 6. Shifts and planned stops ======= */}
      <Section
        title="6. Shifts & planned stops"
        count={shifts.length + plannedStops.length} open={open.shifts}
        onToggle={() => toggleSection("shifts")}
        subtitle="Planned production time — with no shifts, all time counts as planned"
      >
        <h4 className="oee-subhead">Shifts</h4>
        <SimpleTable
          rows={shifts} disabled={ro}
          columns={[
            { key: "name", label: "Shift name" },
            { key: "start_time", label: "Start (HH:MM)" },
            { key: "end_time", label: "End (HH:MM)" },
            { key: "working_days", label: "Days (1=Mon…7=Sun)" },
            { key: "break_minutes", label: "Breaks (min)", type: "number" },
          ]}
          onChange={setShifts}
          onSave={(row) => save("shifts", row)}
          onDelete={(row) => remove("shifts", row.id)}
          onAdd={() => save("shifts", { name: "Day shift", start_time: "06:00",
                                        end_time: "14:00", working_days: "1,2,3,4,5",
                                        enabled: true })}
          addLabel="Add shift"
        />

        <h4 className="oee-subhead">Planned stops</h4>
        <SimpleTable
          rows={plannedStops} disabled={ro}
          columns={[
            { key: "name", label: "Name" },
            { key: "machine_id", label: "Machine (blank = all)", type: "select",
              options: [{ value: "", label: "All machines" }].concat(
                machines.map((m) => ({ value: m.id, label: m.name }))) },
            { key: "start_time", label: "Start (HH:MM)" },
            { key: "end_time", label: "End (HH:MM)" },
            { key: "repeat_rule", label: "Repeat", type: "select",
              options: (meta?.repeat_rules || ["daily"]).map((r) => ({ value: r, label: r })) },
            { key: "exclude_from_oee", label: "Exclude from OEE", type: "bool" },
            { key: "show_on_dashboard", label: "Show on dashboard", type: "bool" },
          ]}
          onChange={setPlannedStops}
          onSave={(row) => save("planned_stops", row)}
          onDelete={(row) => remove("planned_stops", row.id)}
          onAdd={() => save("planned_stops", { name: "Lunch break", start_time: "12:00",
                                               end_time: "12:30", repeat_rule: "daily",
                                               exclude_from_oee: true,
                                               show_on_dashboard: true, enabled: true })}
          addLabel="Add planned stop"
        />
        <div className="muted oee-examples">
          Planned stops are stored separately from unplanned downtime. Excluding
          one removes it from planned production time, so a scheduled break does
          not reduce availability.
        </div>
      </Section>

      {/* ================================== 7. Downtime reasons ========== */}
      <Section
        title="7. Downtime reasons" count={downtime.length} open={open.downtime}
        onToggle={() => toggleSection("downtime")}
        subtitle="What the operator can choose in the downtime prompt"
      >
        <SimpleTable
          rows={downtime} disabled={ro}
          columns={[
            { key: "category", label: "Category" },
            { key: "reason", label: "Reason" },
            { key: "description", label: "Description" },
            { key: "is_planned", label: "Planned", type: "bool" },
            { key: "sort_order", label: "Order", type: "number" },
          ]}
          onChange={setDowntime}
          onSave={(row) => save("downtime_reasons", row)}
          onDelete={(row) => remove("downtime_reasons", row.id)}
          onAdd={() => save("downtime_reasons", { category: "Unknown", reason: "New reason",
                                                  enabled: true, sort_order: 999 })}
          addLabel="Add reason"
        />
      </Section>

      {/* ================================== 8. Quality reasons =========== */}
      <Section
        title="8. Quality / reject reasons" count={quality.length} open={open.quality}
        onToggle={() => toggleSection("quality")}
        subtitle="Why a part was rejected, scrapped or reworked"
      >
        <SimpleTable
          rows={quality} disabled={ro}
          columns={[
            { key: "category", label: "Category" },
            { key: "reason", label: "Reason" },
            { key: "description", label: "Description" },
            { key: "sort_order", label: "Order", type: "number" },
          ]}
          onChange={setQuality}
          onSave={(row) => save("quality_reasons", row)}
          onDelete={(row) => remove("quality_reasons", row.id)}
          onAdd={() => save("quality_reasons", { category: "Unknown", reason: "New reason",
                                                 enabled: true, sort_order: 999 })}
          addLabel="Add reason"
        />
      </Section>
    </div>
  );
}

/* A small editable table. Every config list in this page has the same shape —
   rows of fields with Save / Delete — so it is written once. */
function SimpleTable({ rows, columns, onChange, onSave, onDelete, onAdd,
                       addLabel, disabled }) {
  const patch = (id, key, value) =>
    onChange((prev) => prev.map((r) => (r.id === id ? { ...r, [key]: value } : r)));

  return (
    <div className="oee-simple-table">
      <div className="oee-st-head"
           style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0,1fr)) 70px 140px` }}>
        {columns.map((c) => <span key={c.key}>{c.label}</span>)}
        <span>Enabled</span>
        <span />
      </div>
      {rows.map((row) => (
        <div key={row.id} className="oee-st-row"
             style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0,1fr)) 70px 140px` }}>
          {columns.map((c) => (
            <span key={c.key}>
              {c.type === "bool" ? (
                <input type="checkbox" checked={Boolean(row[c.key])} disabled={disabled}
                       onChange={(e) => patch(row.id, c.key, e.target.checked)} />
              ) : c.type === "select" ? (
                <select value={row[c.key] ?? ""} disabled={disabled}
                        onChange={(e) => patch(row.id, c.key, e.target.value)}>
                  <option value="">—</option>
                  {(c.options || []).map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>))}
                </select>
              ) : (
                <input type={c.type === "number" ? "number" : "text"} step="any"
                       value={row[c.key] ?? ""} disabled={disabled}
                       onChange={(e) => patch(row.id, c.key, e.target.value)} />
              )}
            </span>
          ))}
          <span>
            <input type="checkbox" checked={row.enabled !== false} disabled={disabled}
                   onChange={(e) => patch(row.id, "enabled", e.target.checked)} />
          </span>
          <span className="oee-st-actions">
            <button type="button" className="btn btn-primary btn-sm" disabled={disabled}
                    onClick={() => onSave(row)}>Save</button>
            <button type="button" className="btn btn-danger btn-sm" disabled={disabled}
                    onClick={() => onDelete(row)}>×</button>
          </span>
        </div>
      ))}
      <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
              style={{ marginTop: 6 }} onClick={onAdd}>
        {addLabel}
      </button>
    </div>
  );
}
