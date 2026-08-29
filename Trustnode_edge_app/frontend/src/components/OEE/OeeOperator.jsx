/* OEE Operator Screen.

   Built for someone standing at a machine: big targets, few numbers, and a
   downtime prompt that appears by itself when the machine stops. Manual
   controls are rendered ONLY when the machine has manual input enabled, which
   is what the Configuration toggle actually means.
*/
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  oeeMachinesLive, oeeList, oeeCycleStart, oeeCycleStop, oeeAddCount,
  oeeAddQuality, oeeConfirmDowntime, oeeSetState,
} from "../../api";
import {
  StatePill, ConfidencePill, duration, num, pct, EmptyState, usePoll,
  STATE_LABELS, SOURCE_LABELS, DOWNTIME_STATES,
} from "./OeeShared";

export default function OeeOperatorPage({ canEdit = true }) {
  const [machines, setMachines] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [reasons, setReasons] = useState([]);
  const [qualityReasons, setQualityReasons] = useState([]);
  const [products, setProducts] = useState([]);
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  // Manual entry fields
  const [total, setTotal] = useState("");
  const [good, setGood] = useState("");
  const [reject, setReject] = useState("");
  const [scrapQty, setScrapQty] = useState("");
  const [scrapReason, setScrapReason] = useState("");
  const [comment, setComment] = useState("");

  // Downtime modal
  const [dtOpen, setDtOpen] = useState(false);
  const [dtEventId, setDtEventId] = useState("");
  const [dtCategory, setDtCategory] = useState("");
  const [dtReasonId, setDtReasonId] = useState("");
  const [dtComment, setDtComment] = useState("");
  // A stop the operator dismissed stays dismissed until the NEXT stop, so the
  // modal cannot reappear every poll and trap them in a loop.
  const [dismissed, setDismissed] = useState(() => new Set());

  const load = useCallback(async () => {
    try {
      const r = await oeeMachinesLive();
      const rows = r.machines || [];
      setMachines(rows);
      setSelectedId((prev) => prev || (rows[0]?.machine_id || ""));
      setError("");
    } catch (e) {
      setError(String(e?.message || e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  usePoll(load, 5000, [load]);

  useEffect(() => {
    Promise.all([
      oeeList("downtime_reasons"), oeeList("quality_reasons"),
      oeeList("products"), oeeList("orders"),
    ]).then(([d, q, p, o]) => {
      setReasons((d.items || []).filter((x) => x.enabled));
      setQualityReasons((q.items || []).filter((x) => x.enabled));
      setProducts((p.items || []).filter((x) => x.enabled));
      setOrders(o.items || []);
    }).catch(() => {});
  }, []);

  const machine = useMemo(
    () => machines.find((m) => m.machine_id === selectedId) || null,
    [machines, selectedId]);

  /* The prompt the spec asks for: when the machine enters a downtime state and
     no reason has been recorded, ask for one. */
  useEffect(() => {
    if (!machine) return;
    const isDown = DOWNTIME_STATES.has(String(machine.state));
    const needs = isDown && machine.needs_reason && machine.event_id;
    if (needs && !dismissed.has(machine.event_id) && !dtOpen) {
      setDtEventId(machine.event_id);
      setDtCategory("");
      setDtReasonId("");
      setDtComment("");
      setDtOpen(true);
    }
  }, [machine, dismissed, dtOpen]);

  const categories = useMemo(() => {
    const set = new Set(reasons.map((r) => r.category).filter(Boolean));
    return Array.from(set);
  }, [reasons]);

  const reasonsInCategory = useMemo(
    () => reasons.filter((r) => !dtCategory || r.category === dtCategory),
    [reasons, dtCategory]);

  const act = useCallback(async (fn, okMessage) => {
    setNote(""); setError("");
    try {
      await fn();
      setNote(okMessage);
      await load();
      setTimeout(() => setNote(""), 3000);
    } catch (e) {
      setError(String(e?.message || e));
    }
  }, [load]);

  if (!machines.length) {
    return (
      <section className="card">
        <EmptyState title="No machines configured">
          Add a machine under <strong>OEE → Configuration</strong> first.
        </EmptyState>
      </section>
    );
  }

  const manual = Boolean(machine?.manual_enabled);

  return (
    <div className="oee-page oee-operator">
      {/* ------------------------------------------------ machine chooser */}
      <section className="card">
        <div className="oee-op-picker">
          {machines.map((m) => (
            <button
              key={m.machine_id}
              type="button"
              className={`oee-op-tab ${m.machine_id === selectedId ? "active" : ""} oee-mc-${m.state}`}
              onClick={() => setSelectedId(m.machine_id)}
            >
              <span className="oee-op-tab-name">{m.name}</span>
              <StatePill state={m.state} />
            </button>
          ))}
        </div>
      </section>

      {error ? <div className="error">{error}</div> : null}
      {note ? <div className="ok-note">{note}</div> : null}

      {machine ? (
        <>
          {/* ------------------------------------------------ status panel */}
          <section className="card oee-op-status">
            <div className="oee-op-status-main">
              <div>
                <div className="muted">Machine</div>
                <h2 className="oee-op-title">{machine.name}</h2>
                <div className="muted">
                  {[machine.line, machine.area].filter(Boolean).join(" · ") || "—"}
                </div>
              </div>
              <div className="oee-op-state">
                <StatePill state={machine.state} />
                <div className="oee-op-since">
                  {duration(machine.current_state_seconds)} in this state
                </div>
                <ConfidencePill confidence={machine.confidence}
                                source={machine.status_source} />
                <div className="muted" style={{ fontSize: 11.5 }}>
                  Source: {SOURCE_LABELS[machine.status_source] || machine.status_source}
                </div>
              </div>
            </div>
            <div className="oee-op-metrics">
              <div><span className="muted">Power</span>
                <strong>{machine.power_kw != null ? `${num(machine.power_kw, 2)} kW` : "—"}</strong></div>
              <div><span className="muted">Cycle</span>
                <strong>{machine.cycle ? "Open" : "None"}</strong></div>
              <div><span className="muted">Order</span>
                <strong>{machine.order_number || "—"}</strong></div>
              <div><span className="muted">Product</span>
                <strong>{machine.product_code || "—"}</strong></div>
            </div>
            {machine.flags?.length ? (
              <div className="oee-mc-flags">
                {machine.flags.map((f) => (
                  <span key={f} className={`oee-flag oee-flag-${f}`}>
                    {f === "energy_waste" ? "Energy waste"
                      : f === "blocked" ? "Running but no output"
                      : f === "conflict" ? "Signal conflict" : f}
                  </span>
                ))}
              </div>
            ) : null}
            {machine.needs_reason ? (
              <button type="button" className="btn btn-danger"
                      onClick={() => {
                        setDtEventId(machine.event_id);
                        setDtOpen(true);
                      }}>
                Set the reason for this stop
              </button>
            ) : null}
          </section>

          {/* ----------------------------------------------- manual actions */}
          {manual ? (
            <>
              <section className="card">
                <h3 className="card-title">Cycle</h3>
                <div className="oee-op-buttons">
                  <button type="button" className="btn btn-primary btn-lg"
                          disabled={!canEdit || Boolean(machine.cycle)}
                          onClick={() => act(
                            () => oeeCycleStart({ machine_id: machine.machine_id, source: "manual" }),
                            "Cycle started.")}>
                    Start cycle
                  </button>
                  <button type="button" className="btn btn-secondary btn-lg"
                          disabled={!canEdit || !machine.cycle}
                          onClick={() => act(
                            () => oeeCycleStop({ machine_id: machine.machine_id, result: "good" }),
                            "Cycle stopped.")}>
                    Stop cycle
                  </button>
                </div>
                <div className="oee-op-buttons" style={{ marginTop: 8 }}>
                  {["running", "idle", "stopped", "changeover",
                    "waiting_material", "waiting_operator"].map((st) => (
                    <button key={st} type="button"
                            className={`btn btn-sm ${machine.state === st ? "btn-primary" : "btn-secondary"}`}
                            disabled={!canEdit}
                            onClick={() => act(
                              () => oeeSetState({ machine_id: machine.machine_id, state: st }),
                              `Machine set to ${STATE_LABELS[st]}.`)}>
                      {STATE_LABELS[st]}
                    </button>
                  ))}
                </div>
              </section>

              <section className="card">
                <h3 className="card-title">Counts</h3>
                <div className="oee-op-fields">
                  <label>Total count
                    <input type="number" min="0" value={total}
                           onChange={(e) => setTotal(e.target.value)} /></label>
                  <label>Good count
                    <input type="number" min="0" value={good}
                           onChange={(e) => setGood(e.target.value)} /></label>
                  <label>Reject count
                    <input type="number" min="0" value={reject}
                           onChange={(e) => setReject(e.target.value)} /></label>
                </div>
                <div className="muted" style={{ fontSize: 11.5, margin: "4px 0 8px" }}>
                  Leave good blank and it is worked out as total − rejects.
                </div>
                <button type="button" className="btn btn-primary"
                        disabled={!canEdit || (!total && !good && !reject)}
                        onClick={() => act(async () => {
                          await oeeAddCount({
                            machine_id: machine.machine_id,
                            total_count: Number(total || 0),
                            good_count: good === "" ? null : Number(good),
                            reject_count: reject === "" ? null : Number(reject),
                            source: "manual",
                          });
                          setTotal(""); setGood(""); setReject("");
                        }, "Counts recorded.")}>
                  Record counts
                </button>
              </section>

              <section className="card">
                <h3 className="card-title">Scrap / rework</h3>
                <div className="oee-op-fields">
                  <label>Quantity
                    <input type="number" min="0" value={scrapQty}
                           onChange={(e) => setScrapQty(e.target.value)} /></label>
                  <label>Reason
                    <select value={scrapReason} onChange={(e) => setScrapReason(e.target.value)}>
                      <option value="">— select —</option>
                      {qualityReasons.map((q) => (
                        <option key={q.id} value={q.id}>{q.category} — {q.reason}</option>
                      ))}
                    </select></label>
                  <label>Comment
                    <input value={comment} onChange={(e) => setComment(e.target.value)} /></label>
                </div>
                <button type="button" className="btn btn-secondary"
                        disabled={!canEdit || !scrapQty}
                        onClick={() => act(async () => {
                          await oeeAddQuality({
                            machine_id: machine.machine_id,
                            quantity: Number(scrapQty || 0),
                            result: "scrap",
                            quality_reason_id: scrapReason,
                            comment,
                          });
                          setScrapQty(""); setScrapReason(""); setComment("");
                        }, "Scrap recorded.")}>
                  Record scrap
                </button>
              </section>
            </>
          ) : (
            <section className="card">
              <EmptyState title="Manual input is switched off for this machine">
                Turn on <strong>Manual input</strong> for it under OEE →
                Configuration → Machines to show the cycle and count controls here.
              </EmptyState>
            </section>
          )}
        </>
      ) : null}

      {/* -------------------------------------------------- downtime modal */}
      {dtOpen && machine ? (
        <div className="modal-backdrop" onClick={() => {
          setDtOpen(false);
          setDismissed((prev) => new Set(prev).add(dtEventId));
        }}>
          <div className="modal-card oee-dt-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Why did {machine.name} stop?</h3>

            <div className="oee-dt-facts">
              <div><span className="muted">Stopped since</span>
                <strong>{machine.since_utc ? String(machine.since_utc).slice(0, 19) : "—"}</strong></div>
              <div><span className="muted">Duration</span>
                <strong>{duration(machine.current_state_seconds)}</strong></div>
              <div><span className="muted">Detected status</span>
                <strong>{STATE_LABELS[machine.state] || machine.state}</strong></div>
              <div><span className="muted">Status source</span>
                <strong>{SOURCE_LABELS[machine.status_source] || machine.status_source}</strong></div>
            </div>
            {machine.detail ? (
              <div className="info-note" style={{ fontSize: 12 }}>
                Suggested by: {machine.detail}
              </div>
            ) : null}

            <div className="oee-op-fields">
              <label>Category
                <select value={dtCategory} onChange={(e) => {
                  setDtCategory(e.target.value); setDtReasonId("");
                }}>
                  <option value="">— all categories —</option>
                  {categories.map((c) => <option key={c} value={c}>{c}</option>)}
                </select></label>
              <label>Reason
                <select value={dtReasonId} onChange={(e) => setDtReasonId(e.target.value)}>
                  <option value="">— not set (stored as Unknown) —</option>
                  {reasonsInCategory.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.reason}{r.is_planned ? " (planned)" : ""}
                    </option>
                  ))}
                </select></label>
            </div>
            <label className="oee-dt-comment">
              Operator comment
              <textarea rows={2} value={dtComment}
                        onChange={(e) => setDtComment(e.target.value)} />
            </label>
            <div className="muted" style={{ fontSize: 11.5 }}>
              Leaving the reason blank stores this stop as <strong>Unknown</strong>.
              Unknown stops still appear in the Pareto, so they are never hidden.
            </div>

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary"
                      onClick={() => {
                        setDtOpen(false);
                        setDismissed((prev) => new Set(prev).add(dtEventId));
                      }}>
                Later
              </button>
              <button type="button" className="btn btn-primary"
                      disabled={!canEdit}
                      onClick={() => act(async () => {
                        const chosen = reasons.find((r) => r.id === dtReasonId);
                        await oeeConfirmDowntime({
                          event_id: dtEventId,
                          downtime_reason_id: dtReasonId,
                          downtime_category: chosen?.category || dtCategory || "Unknown",
                          comment: dtComment,
                        });
                        setDtOpen(false);
                        setDismissed((prev) => new Set(prev).add(dtEventId));
                      }, "Downtime reason saved.")}>
                Save reason
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
