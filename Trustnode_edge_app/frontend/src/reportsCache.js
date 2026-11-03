// 2026-11-03. The Reports pages render what the app already knows, at once.
//
// The two Reports pages are mounted only while open, so every visit started
// from empty lists and waited for the network - and when the network was
// busy the page showed nothing and "Reconnecting". The last good lists now
// live here, in memory for the session and in localStorage across restarts,
// stamped with when they were read. A page mounts with them on screen and
// refreshes behind; a failed refresh keeps them and says how old they are.
//
// Pure module except for the optional localStorage; tested with node.

const KEY = "tn.reports.cache.v1";
const EMPTY = { templates: [], schedules: [], generated: [], anyGatewayRunning: true, updatedAt: 0 };

let current = null;

function _storage() {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch (_) { /* blocked storage */ }
  return null;
}

export function readReportsCache() {
  if (current) return current;
  const s = _storage();
  if (s) {
    try {
      const raw = s.getItem(KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          current = {
            ...EMPTY,
            ...parsed,
            templates: Array.isArray(parsed.templates) ? parsed.templates : [],
            schedules: Array.isArray(parsed.schedules) ? parsed.schedules : [],
            generated: Array.isArray(parsed.generated) ? parsed.generated : [],
          };
          return current;
        }
      }
    } catch (_) { /* unreadable: start empty */ }
  }
  current = { ...EMPTY };
  return current;
}

export function writeReportsCache(next) {
  current = {
    ...EMPTY,
    ...(current || {}),
    ...(next || {}),
    updatedAt: Number(next?.updatedAt) || Date.now(),
  };
  const s = _storage();
  if (s) {
    try { s.setItem(KEY, JSON.stringify(current)); } catch (_) { /* quota or blocked: memory still has it */ }
  }
  return current;
}

export function clearReportsCache() {
  current = { ...EMPTY };
  const s = _storage();
  if (s) {
    try { s.removeItem(KEY); } catch (_) { /* nothing to do */ }
  }
}

/** "as of 14:32" for the banner, or "" when nothing was ever loaded. */
export function cacheAgeLabel(cache, now = Date.now()) {
  const at = Number(cache?.updatedAt) || 0;
  if (!at) return "";
  const ageS = Math.max(0, Math.round((now - at) / 1000));
  if (ageS < 90) return `${ageS} s ago`;
  if (ageS < 5400) return `${Math.round(ageS / 60)} min ago`;
  const d = new Date(at);
  return `at ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
