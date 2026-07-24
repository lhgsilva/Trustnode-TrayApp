function toNum(v) {
  // null/undefined/"" are ABSENT values, not zero. Number(null) === 0 and
  // passes isFinite, so without this guard a failed PLC read (stored as NULL)
  // would plot as a real 0 on the chart — exactly the false "flat zero" that
  // hid unreadable tags. Absent stays absent so charts show a gap.
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function norm(v) {
  return String(v ?? "").trim().toLowerCase();
}

export function toTsMs(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    // Accept Unix seconds or milliseconds.
    return value < 1e12 ? value * 1000 : value;
  }
  const raw = String(value ?? "").trim();
  if (!raw) return Number.NaN;
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const n = Number(raw);
    if (Number.isFinite(n)) return n < 1e12 ? n * 1000 : n;
  }
  // CRITICAL: do NOT use `new Date(raw).getTime()` as a fast path
  // for ISO-like strings. Chrome interprets "2026-06-12 13:42:10"
  // (the format the backend emits for ts_utc) as LOCAL wall-clock
  // time, so the chart was systematically 1 hour off in BST/CET.
  // Fall through to the explicit isoLike branch below which
  // treats no-TZ strings as UTC (matches the ts_utc field name).
  // Only fall back to `new Date(raw).getTime()` for FORMATS the
  // regex doesn't recognize (e.g. RFC2822 / GMT-style strings).

  // Fallback 1: deterministic parse for
  // YYYY-MM-DD[ T]HH:mm:ss(.fraction)?(Z|±HH:MM)?
  // (with optional microseconds and explicit zone handling)
  const isoLike = raw.match(
    /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?(?:\s*(Z|[+\-]\d{2}:\d{2}))?$/i
  );
  if (isoLike) {
    const yyyy = Number(isoLike[1]);
    const mm = Number(isoLike[2]) - 1;
    const dd = Number(isoLike[3]);
    const hh = Number(isoLike[4] || 0);
    const mi = Number(isoLike[5] || 0);
    const ss = Number(isoLike[6] || 0);
    const fracRaw = String(isoLike[7] || "");
    const msPart = Number((fracRaw + "000").slice(0, 3) || 0); // trim/pad to ms
    const zone = String(isoLike[8] || "").toUpperCase();

    const utcBase = Date.UTC(yyyy, mm, dd, hh, mi, ss, msPart);
    if (zone === "Z") return utcBase;
    if (/^[+\-]\d{2}:\d{2}$/.test(zone)) {
      const sign = zone.startsWith("-") ? -1 : 1;
      const zh = Number(zone.slice(1, 3));
      const zm = Number(zone.slice(4, 6));
      const offsetMs = sign * ((zh * 60 + zm) * 60 * 1000);
      // Local clock in provided offset -> normalize to UTC epoch.
      return utcBase - offsetMs;
    }
    // No timezone provided: treat as UTC. The backend's
    // historian timestamps are stored as UTC (field name is
    // literally ts_utc) but the on-the-wire format
    // "YYYY-MM-DD HH:mm:ss" omits the Z marker, so a naive parse
    // used to be 1 hour off in BST / CET. Operator 2026-06-12:
    // "my computer time is one hour after what is printed on the
    // X time labels, why?" — this fix shifts the wall-clock
    // labels back to the operator's local zone.
    return utcBase;
  }

  // Fallback 2: "DD/MM/YYYY HH:mm:ss" (local clock)
  const m = raw.match(/^(\d{2})\/(\d{2})\/(\d{4})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (m) {
    const dd = Number(m[1]);
    const mm = Number(m[2]) - 1;
    const yyyy = Number(m[3]);
    const hh = Number(m[4]);
    const mi = Number(m[5]);
    const ss = Number(m[6] || 0);
    const localMs = new Date(yyyy, mm, dd, hh, mi, ss).getTime();
    return Number.isFinite(localMs) ? localMs : Number.NaN;
  }

  // Last-resort: hand off to the platform Date parser. Covers
  // RFC2822 / GMT-style strings the regex above doesn't model.
  // Safe because we ONLY reach this branch when the explicit
  // ISO and DD/MM/YYYY patterns didn't match — there's no
  // ISO-like format here to be misread as local.
  const fallback = new Date(raw).getTime();
  return Number.isFinite(fallback) ? fallback : Number.NaN;
}

export function filterRowsByRange(rows, fromUtc, toUtc) {
  const src = Array.isArray(rows) ? rows : [];
  if (!fromUtc && !toUtc) return src;
  const fromMs = fromUtc ? toTsMs(fromUtc) : Number.NaN;
  const toMs = toUtc ? toTsMs(toUtc) : Number.NaN;
  return src.filter((row) => {
    const ts = toTsMs(row?.ts || row?.ts_utc);
    if (!Number.isFinite(ts)) return false;
    if (Number.isFinite(fromMs) && ts < fromMs) return false;
    if (Number.isFinite(toMs) && ts > toMs) return false;
    return true;
  });
}

export function getTagSeries(rows, gatewayId, tagName, readingsCount = 120) {
  const src = Array.isArray(rows) ? rows : [];
  const gwNeedle = norm(gatewayId);
  const tagNeedle = norm(tagName);
  const byTag = src.filter((r) => norm(r?.tag || r?.tag_name || "") === tagNeedle);
  const strict = byTag.filter((r) => norm(r?.gateway_id || "") === gwNeedle);
  const filtered = (strict.length > 1 ? strict : byTag).sort(
    (a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc)
  );
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
  // The latest reading is the NEWEST raw row for this tag — value and
  // value_text must come from the SAME row.
  //
  // The previous version took last_value from a numeric-filtered series and
  // value_text from the newest raw row independently. When a tag had HISTORICAL
  // numeric rows (e.g. a STRING tag that used to be stored as 0.0 before the
  // type fix) plus current NULL+text rows, the numeric series' newest entry was
  // a stale 0.0 while value_text was current — so a KPI saw value=0 (not null)
  // and rendered "0.000" instead of the live text. Reading both from the single
  // newest row makes the value and the text always consistent.
  const src = Array.isArray(rows) ? rows : [];
  const gwNeedle = norm(gatewayId);
  const tagNeedle = norm(tagName);
  const byTag = src.filter((r) => norm(r?.tag || r?.tag_name || "") === tagNeedle);
  const strict = byTag.filter((r) => norm(r?.gateway_id || "") === gwNeedle);
  const pool = strict.length > 1 ? strict : byTag;
  if (!pool.length) return null;
  let lastRaw = pool[0];
  let lastMs = toTsMs(lastRaw?.ts || lastRaw?.ts_utc);
  for (let i = 1; i < pool.length; i += 1) {
    const ms = toTsMs(pool[i]?.ts || pool[i]?.ts_utc);
    if (ms >= lastMs) { lastMs = ms; lastRaw = pool[i]; }
  }
  const num = toNum(lastRaw?.value);           // null when the newest row is text/absent
  const lastText = lastRaw.value_text != null && lastRaw.value_text !== ""
    ? String(lastRaw.value_text)
    : null;
  return {
    last_value: num,
    last_value_text: lastText,
    last_ts: String(lastRaw?.ts || lastRaw?.ts_utc || ""),
  };
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

function bucketMsFromInterval(interval) {
  const map = {
    none: 0,
    "1s": 1000,
    "5s": 5000,
    "10s": 10000,
    "30s": 30000,
    "1m": 60000,
    "5m": 300000,
    "15m": 900000,
    "1h": 3600000,
    "1d": 86400000,
  };
  return Number(map[String(interval || "none")] || 0);
}

function bucketRows(rows, interval) {
  const bucketMs = bucketMsFromInterval(interval);
  if (!bucketMs) return Array.isArray(rows) ? rows : [];
  const grouped = new Map();
  for (const r of Array.isArray(rows) ? rows : []) {
    const ts = toTsMs(r?.ts || r?.ts_utc);
    if (!Number.isFinite(ts)) continue;
    const key = Math.floor(ts / bucketMs) * bucketMs;
    grouped.set(key, r);
  }
  return Array.from(grouped.values()).sort(
    (a, b) => toTsMs(a?.ts || a?.ts_utc) - toTsMs(b?.ts || b?.ts_utc)
  );
}

export function evaluateComputedRules(rows, rules, options = {}) {
  const sourceRows = Array.isArray(rows) ? rows : [];
  const list = Array.isArray(rules) ? rules : [];
  const rowSelection = String(options?.row_selection || "all");
  const rowLimit = Math.max(10, Number(options?.row_limit || 200));
  const groupInterval = String(options?.group_interval || "none");
  const resultAgg = String(options?.result_aggregation || "");
  const ruleLogic = String(options?.rule_logic || "any") === "all" ? "all" : "any";

  const selectedRows =
    rowSelection === "last_n"
      ? sourceRows.slice(-rowLimit)
      : sourceRows;
  const groupedRows = bucketRows(selectedRows, groupInterval);

  return list.map((rule, idx) => {
    const gatewayId = String(rule?.gateway_id || "");
    const tagName = String(rule?.tag_name || "");
    const subset = groupedRows
      .filter((r) => (!gatewayId || String(r?.gateway_id || "") === gatewayId))
      .filter((r) => (!tagName || String(r?.tag || r?.tag_name || "") === tagName));
    let matched = subset;
    if (ruleLogic === "any") {
      matched = subset.filter((r) => ruleMatch(r?.value, rule?.operator, rule?.value1, rule?.value2));
    } else {
      matched = subset.filter((r) => ruleMatch(r?.value, rule?.operator, rule?.value1, rule?.value2));
    }
    const effectiveAgg = resultAgg || String(rule?.aggregation || "count");
    const metric = aggregate(matched.map((r) => r?.value), effectiveAgg);
    return {
      id: String(rule?.id || `rule-${idx + 1}`),
      label: String(rule?.label || `Item ${idx + 1}`),
      value: Number.isFinite(Number(metric)) ? Number(metric) : 0,
      color: String(rule?.color || "#14a89a"),
      gateway_id: gatewayId,
      tag_name: tagName,
      aggregation: effectiveAgg,
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
