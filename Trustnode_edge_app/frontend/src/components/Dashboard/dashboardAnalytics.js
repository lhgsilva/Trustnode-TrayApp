function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function toTsMs(value) {
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : Number.NaN;
}

export function filterRowsByRange(rows, fromUtc, toUtc) {
  const src = Array.isArray(rows) ? rows : [];
  if (!fromUtc && !toUtc) return src;
  const fromMs = fromUtc ? toTsMs(fromUtc) : Number.NaN;
  const toMs = toUtc ? toTsMs(toUtc) : Number.NaN;
  return src.filter((row) => {
    const ts = toTsMs(row?.ts);
    if (!Number.isFinite(ts)) return false;
    if (Number.isFinite(fromMs) && ts < fromMs) return false;
    if (Number.isFinite(toMs) && ts > toMs) return false;
    return true;
  });
}

export function getTagSeries(rows, gatewayId, tagName, readingsCount = 120) {
  const filtered = (Array.isArray(rows) ? rows : [])
    .filter(
      (r) =>
        String(r?.gateway_id || "") === String(gatewayId || "") &&
        String(r?.tag || r?.tag_name || "") === String(tagName || "")
    )
    .sort((a, b) => toTsMs(a?.ts) - toTsMs(b?.ts));
  const sliced = filtered.slice(-Math.max(10, Number(readingsCount || 120)));
  return sliced
    .map((r, idx) => ({
      idx: idx + 1,
      ts: String(r?.ts || ""),
      value: toNum(r?.value),
    }))
    .filter((p) => p.value !== null);
}

export function getLatestTagRow(rows, gatewayId, tagName) {
  const series = getTagSeries(rows, gatewayId, tagName, 5000);
  if (!series.length) return null;
  const last = series[series.length - 1];
  return { last_value: last.value, last_ts: last.ts };
}

function ruleMatch(value, op, v1, v2) {
  const n = toNum(value);
  const a = toNum(v1);
  const b = toNum(v2);
  switch (String(op || "any")) {
    case "eq":
      return n !== null && a !== null && n === a;
    case "ne":
      return n !== null && a !== null && n !== a;
    case "lt":
      return n !== null && a !== null && n < a;
    case "lte":
      return n !== null && a !== null && n <= a;
    case "gt":
      return n !== null && a !== null && n > a;
    case "gte":
      return n !== null && a !== null && n >= a;
    case "between":
      return n !== null && a !== null && b !== null && n >= Math.min(a, b) && n <= Math.max(a, b);
    case "any":
    default:
      return true;
  }
}

function aggregate(values, agg) {
  const clean = values.map((v) => toNum(v)).filter((v) => v !== null);
  const mode = String(agg || "count");
  if (mode === "count") return values.length;
  if (!clean.length) return 0;
  if (mode === "sum") return clean.reduce((a, b) => a + b, 0);
  if (mode === "avg") return clean.reduce((a, b) => a + b, 0) / clean.length;
  if (mode === "min") return Math.min(...clean);
  if (mode === "max") return Math.max(...clean);
  if (mode === "latest") return clean[clean.length - 1];
  return clean.length;
}

export function evaluateComputedRules(rows, rules) {
  const sourceRows = Array.isArray(rows) ? rows : [];
  const list = Array.isArray(rules) ? rules : [];
  return list.map((rule, idx) => {
    const gatewayId = String(rule?.gateway_id || "");
    const tagName = String(rule?.tag_name || "");
    const subset = sourceRows
      .filter((r) => (!gatewayId || String(r?.gateway_id || "") === gatewayId))
      .filter((r) => (!tagName || String(r?.tag || r?.tag_name || "") === tagName));
    const matched = subset.filter((r) => ruleMatch(r?.value, rule?.operator, rule?.value1, rule?.value2));
    const metric = aggregate(matched.map((r) => r?.value), rule?.aggregation);
    return {
      id: String(rule?.id || `rule-${idx + 1}`),
      label: String(rule?.label || `Item ${idx + 1}`),
      value: Number.isFinite(Number(metric)) ? Number(metric) : 0,
      color: String(rule?.color || "#14a89a"),
      gateway_id: gatewayId,
      tag_name: tagName,
      aggregation: String(rule?.aggregation || "count"),
      operator: String(rule?.operator || "any"),
      sample_count: matched.length,
    };
  });
}

export function buildFixedText(template, computedItems) {
  const base = String(template || "");
  if (!base.trim()) return "";
  return (computedItems || []).reduce((txt, item) => {
    const key = String(item?.label || "").trim();
    if (!key) return txt;
    const safeKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return txt.replace(new RegExp(`{{\\s*${safeKey}\\s*}}`, "gi"), String(item?.value ?? ""));
  }, base);
}
