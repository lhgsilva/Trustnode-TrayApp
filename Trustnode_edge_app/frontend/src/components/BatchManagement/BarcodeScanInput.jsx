/* Shared keyboard-wedge barcode input (operator 2026-07-30).

   One component gives the dashboard Batch ID widget, the Batch Overview page
   and the batch detail header identical scanner behavior:

   * Enter submits (scanners configured with a CR/Enter suffix).
   * BURST DETECTION — scanners without a suffix "type" the code with ~1-20 ms
     between characters. When >=4 chars arrive that fast, the code is submitted
     automatically ~160 ms after the last character. A human typing (>90 ms
     gaps) never trips this; they press Enter or the button.
   * IDLE AUTO-REFOCUS — after `refocusDelayMs` with no keyboard/pointer
     activity anywhere, the field reclaims focus so the NEXT scan always lands
     here, even if the operator clicked around the dashboard meanwhile. It
     never steals focus while a modal is open (widget editors, batch forms)
     and pauses whenever the user is actively interacting.

   Uncontrolled by default (internal value, cleared on submit). Pass
   `value`/`onValueChange` for controlled use (batch detail keeps the code in
   state so its Start/Stop buttons can read it).
*/
import { useCallback, useEffect, useRef, useState } from "react";

const BURST_GAP_MS = 45;      // max inter-char gap that still looks like a scanner
const BURST_MIN_CHARS = 4;    // chars at scanner speed before auto-submit arms
const BURST_SETTLE_MS = 160;  // quiet time after the last char -> submit

export default function BarcodeScanInput({
  onSubmit,
  busy = false,
  disabled = false,
  placeholder = "Scan or type code…",
  buttonLabel = "Load",
  showButton = true,
  autoRefocus = true,
  refocusDelayMs = 5000,
  value: valueProp,
  onValueChange,
  style,
  inputStyle,
  inputClassName,
}) {
  const controlled = valueProp !== undefined;
  const [inner, setInner] = useState("");
  const value = controlled ? String(valueProp ?? "") : inner;
  const inputRef = useRef(null);
  const changeTimesRef = useRef([]);   // perf timestamps of recent char inserts
  const settleTimerRef = useRef(null);
  const lastActivityRef = useRef(Date.now());
  const submitRef = useRef(() => {});

  const setValue = useCallback((v) => {
    if (!controlled) setInner(v);
    if (onValueChange) onValueChange(v);
  }, [controlled, onValueChange]);

  const doSubmit = useCallback(() => {
    if (settleTimerRef.current) { clearTimeout(settleTimerRef.current); settleTimerRef.current = null; }
    const code = value.trim();
    if (!code || busy || disabled) return;
    setValue("");
    changeTimesRef.current = [];
    onSubmit && onSubmit(code);
  }, [value, busy, disabled, onSubmit, setValue]);
  submitRef.current = doSubmit;

  const onChange = (e) => {
    const next = e.target.value;
    const grew = next.length > value.length;
    setValue(next);
    if (settleTimerRef.current) { clearTimeout(settleTimerRef.current); settleTimerRef.current = null; }
    if (!grew) { changeTimesRef.current = []; return; }
    const now = performance.now();
    const times = changeTimesRef.current;
    times.push(now);
    if (times.length > 12) times.shift();
    // Scanner burst: the last BURST_MIN_CHARS inserts all landed within
    // BURST_GAP_MS of each other -> schedule an auto-submit once input settles.
    if (times.length >= BURST_MIN_CHARS) {
      const recent = times.slice(-BURST_MIN_CHARS);
      let burst = true;
      for (let i = 1; i < recent.length; i += 1) {
        if (recent[i] - recent[i - 1] > BURST_GAP_MS) { burst = false; break; }
      }
      if (burst) settleTimerRef.current = setTimeout(() => submitRef.current(), BURST_SETTLE_MS);
    }
  };

  // Idle auto-refocus loop.
  useEffect(() => {
    if (!autoRefocus) return undefined;
    const bump = () => { lastActivityRef.current = Date.now(); };
    window.addEventListener("pointerdown", bump, true);
    window.addEventListener("keydown", bump, true);
    window.addEventListener("wheel", bump, true);
    const tick = setInterval(() => {
      const el = inputRef.current;
      if (!el || disabled) return;
      if (document.activeElement === el) return;
      if (Date.now() - lastActivityRef.current < refocusDelayMs) return;
      if (!el.offsetParent) return;                      // hidden/collapsed
      if (document.querySelector(".modal-backdrop")) return;  // a dialog is open
      try { el.focus({ preventScroll: true }); } catch { /* no-op */ }
    }, 1000);
    return () => {
      window.removeEventListener("pointerdown", bump, true);
      window.removeEventListener("keydown", bump, true);
      window.removeEventListener("wheel", bump, true);
      clearInterval(tick);
    };
  }, [autoRefocus, refocusDelayMs, disabled]);

  useEffect(() => () => { if (settleTimerRef.current) clearTimeout(settleTimerRef.current); }, []);

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", ...style }}>
      <input
        ref={inputRef}
        className={inputClassName}
        placeholder={placeholder}
        value={value}
        onChange={onChange}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); doSubmit(); } }}
        disabled={disabled}
        autoFocus={autoRefocus}
        style={{ flex: 1, minWidth: 0, ...inputStyle }}
      />
      {showButton && (
        <button
          className="btn btn-primary btn-sm"
          disabled={busy || disabled || !value.trim()}
          onClick={doSubmit}
        >
          {busy ? "…" : buttonLabel}
        </button>
      )}
    </div>
  );
}
