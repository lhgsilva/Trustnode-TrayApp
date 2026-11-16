// 2026-11-16, asked: on dashboard charts "when we select auto and manual we
// should also be able to change if only min or max should be auto ... and if
// it should be linked to a tag too, same for the tick options".
//
// ONE rule, used by the editor, the rolling/trigger chart and the windowed
// chart (they each had their own copy, and disagreed). Every bound - Min, Max
// and Tick step, on every axis - has its own source:
//
//   auto    from the data (the default)
//   manual  the number in the existing field (y_min, y_max, y_tick_step ...)
//   tag     a tag's value: latest, or its max / min / avg over the chart's
//           window - `${field}_tag` = "gatewayId::tagName",
//           `${field}_tag_agg` = latest | max | min | avg
//
// The source lives in `${field}_src`. A widget saved before this has no
// `_src` and keeps its old meaning: the axis-wide Auto/Manual switch, with a
// manual bound only where a number was entered.
//
// Pure module; tested with node.

export const AXIS_BOUND_KEYS = [
  { id: "left1", mode: "y_axis_mode", min: "y_min", max: "y_max", step: "y_tick_step" },
  { id: "right1", mode: "y_right_axis_mode", min: "y_right_min", max: "y_right_max", step: "y_right_tick_step" },
  { id: "left2", mode: "y_left2_axis_mode", min: "y_left2_min", max: "y_left2_max", step: "y_left2_tick_step" },
  { id: "right2", mode: "y_right2_axis_mode", min: "y_right2_min", max: "y_right2_max", step: "y_right2_tick_step" },
];
export const AXIS_TAG_AGGS = [
  { value: "latest", label: "Latest value" },
  { value: "max", label: "Maximum in window" },
  { value: "min", label: "Minimum in window" },
  { value: "avg", label: "Average in window" },
];

function _num(v) {
  if (v === "" || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** "auto" | "manual" | "tag" for one bound field. */
export function axisBoundSource(cfg, field, legacyModeKey) {
  const src = String((cfg || {})[`${field}_src`] || "").toLowerCase();
  if (src === "auto" || src === "manual" || src === "tag") return src;
  const legacyManual = String((cfg || {})[legacyModeKey] || "auto").toLowerCase() === "manual";
  return legacyManual && _num((cfg || {})[field]) !== null ? "manual" : "auto";
}

/** The tag a bound is linked to, as { key, gatewayId, tagName, agg } - or null. */
export function axisBoundTag(cfg, field) {
  const ref = String((cfg || {})[`${field}_tag`] || "").trim();
  if (!ref) return null;
  const cut = ref.indexOf("::");
  const gatewayId = cut >= 0 ? ref.slice(0, cut) : "";
  const tagName = cut >= 0 ? ref.slice(cut + 2) : ref;
  if (!tagName) return null;
  const a = String((cfg || {})[`${field}_tag_agg`] || "latest").toLowerCase();
  const agg = ["latest", "max", "min", "avg"].includes(a) ? a : "latest";
  return { key: `${gatewayId}::${tagName}|${agg}`, gatewayId, tagName, agg };
}

/** Every tag-linked bound of a config (for fetching their values). */
export function axisTagSpecs(cfg) {
  const out = new Map();
  for (const ax of AXIS_BOUND_KEYS) {
    for (const field of [ax.min, ax.max, ax.step]) {
      if (axisBoundSource(cfg, field, ax.mode) !== "tag") continue;
      const t = axisBoundTag(cfg, field);
      if (t) out.set(t.key, t);
    }
  }
  return Array.from(out.values());
}

/**
 * { lo, hi, step, ticks } for one axis; null in lo / hi / step = automatic.
 * `tagValues` maps a spec key ("gw::tag|agg") to its number.
 */
export function resolveAxisScale(cfg, axis, tagValues = {}) {
  const one = (field) => {
    const src = axisBoundSource(cfg, field, axis.mode);
    if (src === "manual") return _num((cfg || {})[field]);
    if (src === "tag") {
      const t = axisBoundTag(cfg, field);
      return t ? _num(tagValues[t.key]) : null;
    }
    return null;
  };
  let lo = one(axis.min);
  let hi = one(axis.max);
  let step = one(axis.step);
  if (lo !== null && hi !== null && hi <= lo) hi = null;      // an impossible pair: the max goes auto
  if (step !== null && !(step > 0)) step = null;
  let ticks;
  if (lo !== null && hi !== null && step !== null && (hi - lo) / step <= 100) {
    ticks = [];
    for (let v = lo, i = 0; v <= hi + step * 1e-9 && i <= 100; v += step, i += 1) ticks.push(Number(v.toFixed(10)));
  }
  return { lo, hi, step, ticks, fixed: lo !== null || hi !== null };
}
