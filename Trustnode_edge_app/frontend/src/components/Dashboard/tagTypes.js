/**
 * Tag type registry — answers "can this tag be plotted on a numeric widget?"
 *
 * Two layers, most trustworthy first:
 *
 *   1. DECLARED type from the controller (authoritative). Populated from the
 *      /api/plc/discover-tags response (`types: {tag: "STRING"|"REAL"|...}`).
 *      Works BEFORE a tag has ever been collected.
 *   2. INFERRED from collected readings (fallback). The historian stores a
 *      STRING tag as value=NULL + value_text='...', so:
 *        - has value_text and never a numeric  -> text-only
 *        - has value_text and also a numeric   -> numeric-looking string ('77')
 *        - numeric only                        -> numeric
 *
 * FAIL-OPEN is the core design rule: when neither layer knows the type we
 * return "unknown" and the caller must ALLOW the selection. Blocking on a guess
 * would be worse than not blocking — a tag that is merely unread yet (or
 * temporarily unreadable) must never be mistaken for a text tag and refused.
 */

// tag kinds
export const TAG_KIND = {
  NUMERIC: "numeric",           // REAL/DINT/BOOL/... — safe everywhere
  TEXT: "text",                 // STRING with no numeric meaning — not plottable
  NUMERIC_TEXT: "numeric_text", // STRING that parses as a number ('77') — plottable
  UNKNOWN: "unknown",           // not enough information — always allowed
};

// Declared controller types that carry text.
const DECLARED_TEXT_TYPES = new Set(["STRING", "STRINGN", "SHORT_STRING", "BYTESTRING"]);
// Declared types we know are numeric/boolean.
const DECLARED_NUMERIC_TYPES = new Set([
  "REAL", "LREAL", "DINT", "INT", "SINT", "LINT",
  "USINT", "UINT", "UDINT", "ULINT", "BOOL", "BIT",
]);

// Module-level cache of declared types: { "<gatewayId>::<tag>": "STRING" }
const _declared = new Map();

const key = (gatewayId, tagName) =>
  `${String(gatewayId || "").trim()}::${String(tagName || "").trim()}`;

/** Record declared types from a discover-tags response. Safe to call repeatedly. */
export function registerDeclaredTagTypes(gatewayId, typesMap) {
  if (!typesMap || typeof typesMap !== "object") return;
  for (const [tag, dt] of Object.entries(typesMap)) {
    const t = String(dt || "").trim().toUpperCase();
    if (tag && t) _declared.set(key(gatewayId, tag), t);
  }
}

/** The declared controller type for a tag, or "" when unknown. */
export function declaredTagType(gatewayId, tagName) {
  return _declared.get(key(gatewayId, tagName)) || "";
}

/**
 * Classify a tag. `rows` is the live reading buffer (dataLogView-shaped:
 * {tag|tag_name, gateway_id, value, value_text}); it is only consulted when the
 * declared type is unavailable.
 */
export function classifyTag(gatewayId, tagName, rows) {
  const tag = String(tagName || "").trim();
  if (!tag) return TAG_KIND.UNKNOWN;

  // --- layer 1: declared type (authoritative) ---
  const declared = declaredTagType(gatewayId, tag);
  if (declared) {
    if (DECLARED_TEXT_TYPES.has(declared)) {
      // A declared STRING may still hold a numeric-looking value ('77'). If we
      // have seen a numeric for it, treat it as plottable-with-a-note.
      return sawNumeric(gatewayId, tag, rows) ? TAG_KIND.NUMERIC_TEXT : TAG_KIND.TEXT;
    }
    if (DECLARED_NUMERIC_TYPES.has(declared)) return TAG_KIND.NUMERIC;
    // STRUCT/TIMER/PID/etc. — not a scalar we can chart, but don't hard-block.
    return TAG_KIND.UNKNOWN;
  }

  // --- layer 2: infer from collected readings ---
  const seen = scanRows(gatewayId, tag, rows);
  if (!seen.any) return TAG_KIND.UNKNOWN;      // never collected -> fail open
  if (!seen.text) return TAG_KIND.NUMERIC;
  return seen.numeric ? TAG_KIND.NUMERIC_TEXT : TAG_KIND.TEXT;
}

function scanRows(gatewayId, tagName, rows) {
  const out = { any: false, text: false, numeric: false };
  if (!Array.isArray(rows) || !rows.length) return out;
  const gw = String(gatewayId || "").trim();
  const tag = String(tagName || "").trim();
  // Newest-first scan with a bounded budget — these buffers can be large and
  // this runs inside render paths.
  let checked = 0;
  for (let i = rows.length - 1; i >= 0 && checked < 4000; i -= 1) {
    const r = rows[i];
    if (!r) continue;
    if (String(r.tag || r.tag_name || "") !== tag) continue;
    if (gw && r.gateway_id && String(r.gateway_id) !== gw) continue;
    checked += 1;
    out.any = true;
    const t = r.value_text;
    if (t != null && String(t) !== "") out.text = true;
    const v = r.value;
    if (v != null && v !== "" && Number.isFinite(Number(v))) out.numeric = true;
    // Enough evidence to classify — stop early.
    if (out.text && out.numeric) break;
  }
  return out;
}

function sawNumeric(gatewayId, tagName, rows) {
  return scanRows(gatewayId, tagName, rows).numeric;
}

// NOTE: these keys MUST match widgetRegistry.js exactly — a typo would make the
// interlock silently inert. Verified against the registry's widget keys.

/** Widget types that render text fine (tiles/tables/content). */
const TEXT_CAPABLE_WIDGETS = new Set([
  "text_kpi",       // purpose-built for text
  "value_kpi",      // shows the latest value, text included
  "table_list",     // tabular rows
  "fixed_text",
  "batch_current", "batch_input", "batch_list", "batch_timeline", "batch_kpi",
  "report_card", "cloud_sync_status", "image", "ip_camera", "divider",
  "energy_tariffs",
]);

/**
 * Widget types that require a NUMBER. A text tag on these renders an empty or
 * flat chart with no explanation, because the historian stores value=NULL.
 */
const NUMERIC_ONLY_WIDGETS = new Set([
  "line_chart",
  "line_area_chart",
  "bar_chart",
  "stacked_trend",
  "pie_chart",
  "meter_chart",
]);

export function widgetAcceptsText(widgetType) {
  const t = String(widgetType || "").trim().toLowerCase();
  if (TEXT_CAPABLE_WIDGETS.has(t)) return true;
  if (NUMERIC_ONLY_WIDGETS.has(t)) return false;
  // Unknown widget type -> don't restrict.
  return true;
}

export function widgetIsNumericOnly(widgetType) {
  return NUMERIC_ONLY_WIDGETS.has(String(widgetType || "").trim().toLowerCase());
}

/**
 * The interlock decision for one (widget, tag) pair.
 * Returns { ok, severity: "none"|"warn"|"block", kind, message, suggest }.
 *
 * `block` is returned ONLY for a tag we are confident is text-only on a
 * numeric-only widget. Everything uncertain is allowed.
 */
export function checkTagForWidget(widgetType, gatewayId, tagName, rows) {
  const kind = classifyTag(gatewayId, tagName, rows);
  if (!widgetIsNumericOnly(widgetType)) {
    return { ok: true, severity: "none", kind, message: "", suggest: "" };
  }
  if (kind === TAG_KIND.TEXT) {
    return {
      ok: false,
      severity: "block",
      kind,
      message: `"${tagName}" is a text tag — it has no numeric value to plot on this widget.`,
      suggest: "Use a Value tile or a Table to show text tags.",
    };
  }
  if (kind === TAG_KIND.NUMERIC_TEXT) {
    return {
      ok: true,
      severity: "warn",
      kind,
      message: `"${tagName}" is a text tag whose value currently looks numeric — it will plot, but will show gaps if it ever contains letters.`,
      suggest: "",
    };
  }
  return { ok: true, severity: "none", kind, message: "", suggest: "" };
}
