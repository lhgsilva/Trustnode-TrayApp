// The data filter - rows chosen by the value of OTHER tags at the same time.
//
// 2026-11-16, from the owner: "to any dashboard widget a filter option not
// only for the time but also based on a tag value ... the timestamp of the
// data row and the tag value in the same timestamp ... multiple conditions,
// and / or ... any dashboard widget, any csv engine and any report section".
//
// One shape everywhere (the backend twin is services/historian_filter.py):
//   { logic: "all" | "any",
//     conditions: [{ id, gateway_id, tag_name, operator, value1, value2 }] }
//
// A row at time T is kept when the conditions hold for the named tags in the
// same collection cycle (same gateway, same timestamp); a condition on
// another gateway reads that tag's latest value at or before T.
// The SQL lives in the backend; this file carries the shape, the editor's
// helpers and the in-browser evaluator for live rows.

export const DATA_FILTER_MAX = 8;

export const DATA_FILTER_OPERATORS = [
  { value: "eq", label: "=" },
  { value: "ne", label: "≠" },
  { value: "lt", label: "<" },
  { value: "lte", label: "≤" },
  { value: "gt", label: ">" },
  { value: "gte", label: "≥" },
  { value: "between", label: "between" },
];

const _ALIAS = { ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "=": "eq", "==": "eq", "!=": "ne", "<>": "ne" };
const _OPS = new Set(DATA_FILTER_OPERATORS.map((o) => o.value));

let _seq = 0;
export function newDataFilterCondition() {
  _seq += 1;
  return { id: `df-${Date.now().toString(36)}-${_seq}`, gateway_id: "", tag_name: "", operator: "eq", value1: "", value2: "" };
}

const _num = (v) => {
  if (v === "" || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/** The filter as the backend reads it, or null when nothing in it is usable. */
export function cleanDataFilter(raw) {
  let src = raw;
  if (typeof src === "string") {
    try { src = src.trim() ? JSON.parse(src) : null; } catch (_) { return null; }
  }
  if (!src || typeof src !== "object") return null;
  if (src.enabled === false) return null;
  const list = Array.isArray(src) ? src : (Array.isArray(src.conditions) ? src.conditions : []);
  const out = [];
  for (const c of list) {
    if (!c || typeof c !== "object") continue;
    const tag = String(c.tag_name || c.tag || "").trim();
    if (!tag) continue;
    let op = String(c.operator || c.op || "eq").trim().toLowerCase();
    op = _ALIAS[op] || op;
    if (!_OPS.has(op)) continue;
    const v1 = _num(c.value1 !== undefined ? c.value1 : c.value);
    const v2 = _num(c.value2);
    if (v1 === null || (op === "between" && v2 === null)) continue;
    out.push({ gateway_id: String(c.gateway_id || c.gateway || "").trim(), tag_name: tag, operator: op,
               value1: v1, value2: op === "between" ? v2 : null });
    if (out.length >= DATA_FILTER_MAX) break;
  }
  if (!out.length) return null;
  const logic = ["any", "or"].includes(String(src.logic || "all").toLowerCase()) ? "any" : "all";
  return { logic, conditions: out };
}

/** The request parameter: JSON text, or "" when there is no filter. */
export function dataFilterParam(raw) {
  const f = cleanDataFilter(raw);
  return f ? JSON.stringify(f) : "";
}

/** "Run = 1 and Temp ≥ 30" for a button or a card; "" when empty. */
export function describeDataFilter(raw, labelOf = null) {
  const f = cleanDataFilter(raw);
  if (!f) return "";
  const sym = Object.fromEntries(DATA_FILTER_OPERATORS.map((o) => [o.value, o.label]));
  const parts = f.conditions.map((c) => {
    const name = typeof labelOf === "function" ? (labelOf(c.tag_name, c.gateway_id) || c.tag_name) : c.tag_name;
    if (c.operator === "between") return `${name} ${c.value1}..${c.value2}`;
    return `${name} ${sym[c.operator]} ${c.value1}`;
  });
  return parts.join(f.logic === "any" ? " or " : " and ");
}

function _matches(op, value, v1, v2) {
  const n = _num(value);
  if (n === null) return false;
  switch (op) {
    case "eq": return n === v1;
    case "ne": return n !== v1;
    case "lt": return n < v1;
    case "lte": return n <= v1;
    case "gt": return n > v1;
    case "gte": return n >= v1;
    case "between": { const hi = v2 === null ? v1 : v2; return Math.min(v1, hi) <= n && n <= Math.max(v1, hi); }
    default: return false;
  }
}

/** Does one cycle (tag -> value of one gateway at one time) pass? `lookup(gateway, tag)` answers another gateway's tag. */
export function cyclePassesDataFilter(flt, cycleValues, gatewayId = "", lookup = null) {
  const f = flt && flt.conditions && flt.conditions.length && typeof flt.conditions[0].value1 === "number" ? flt : cleanDataFilter(flt);
  if (!f) return true;
  const results = f.conditions.map((c) => {
    let value;
    if (c.gateway_id && gatewayId && c.gateway_id !== gatewayId) value = typeof lookup === "function" ? lookup(c.gateway_id, c.tag_name) : undefined;
    else value = cycleValues[c.tag_name];
    return _matches(c.operator, value, c.value1, c.value2);
  });
  return f.logic === "any" ? results.some(Boolean) : results.every(Boolean);
}

/**
 * Live rows ({gateway_id, ts|ts_utc, tag|tag_name, value}) grouped into
 * cycles by (gateway, timestamp); the rows of the cycles that pass, in the
 * order given. A condition on another gateway reads that gateway's newest
 * value at or before the cycle's time among the rows given.
 */
export function rowsPassDataFilter(rows, flt) {
  const f = cleanDataFilter(flt);
  const src = Array.isArray(rows) ? rows : [];
  if (!f || !src.length) return src;
  const tsOf = (r) => { const t = r?.ts || r?.ts_utc; const n = Date.parse(String(t || "").replace(" ", "T") + (String(t || "").match(/Z|[+-]\d\d:?\d\d$/) ? "" : "Z")); return Number.isFinite(n) ? n : NaN; };
  const keyOf = (r) => `${r?.gateway_id || ""}|${r?.ts || r?.ts_utc || ""}`;
  const cycles = new Map();
  const byGwTag = new Map();   // "gw|tag" -> [[tsMs, value]] sorted ascending (for sample-and-hold)
  for (const r of src) {
    const k = keyOf(r);
    let c = cycles.get(k);
    if (!c) { c = { gateway: String(r?.gateway_id || ""), tsMs: tsOf(r), values: {} }; cycles.set(k, c); }
    c.values[String(r?.tag || r?.tag_name || "")] = r?.value;
    const gk = `${r?.gateway_id || ""}|${r?.tag || r?.tag_name || ""}`;
    if (!byGwTag.has(gk)) byGwTag.set(gk, []);
    byGwTag.get(gk).push([c.tsMs, r?.value]);
  }
  for (const list of byGwTag.values()) list.sort((a, b) => a[0] - b[0]);
  const held = (gw, tag, tsMs) => {
    const list = byGwTag.get(`${gw}|${tag}`);
    if (!list) return undefined;
    let out;
    for (const [t, v] of list) { if (t <= tsMs) out = v; else break; }
    return out;
  };
  const ok = new Set();
  for (const [k, c] of cycles.entries()) {
    if (cyclePassesDataFilter(f, c.values, c.gateway, (gw, tag) => held(gw, tag, c.tsMs))) ok.add(k);
  }
  return src.filter((r) => ok.has(keyOf(r)));
}
