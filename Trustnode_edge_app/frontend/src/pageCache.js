// 2026-11-03. The last known lists of a page, so it opens with content.
//
// The Reports pages got this first (reportsCache.js); this is the same
// idea for any page that is mounted only while open and used to start from
// an empty list and wait for the network. A page reads its last lists at
// mount, refreshes behind, writes a good read back, and keeps what it has
// when a read fails. Memory for the session, localStorage across restarts.
//
// Pure module except for the optional localStorage; tested with node.

const PREFIX = "tn.page.cache.v1:";
const mem = new Map();

function _storage() {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch (_) { /* blocked storage */ }
  return null;
}

/** { value, updatedAt } or null when nothing was ever stored for `key`. */
export function readPageCache(key) {
  const k = String(key || "");
  if (!k) return null;
  if (mem.has(k)) return mem.get(k);
  const s = _storage();
  if (s) {
    try {
      const raw = s.getItem(PREFIX + k);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && "value" in parsed) {
          const entry = { value: parsed.value, updatedAt: Number(parsed.updatedAt) || 0 };
          mem.set(k, entry);
          return entry;
        }
      }
    } catch (_) { /* unreadable: start empty */ }
  }
  return null;
}

export function writePageCache(key, value, updatedAt = Date.now()) {
  const k = String(key || "");
  if (!k) return null;
  const entry = { value, updatedAt: Number(updatedAt) || Date.now() };
  mem.set(k, entry);
  const s = _storage();
  if (s) {
    try { s.setItem(PREFIX + k, JSON.stringify(entry)); } catch (_) { /* quota or blocked */ }
  }
  return entry;
}

export function clearPageCache(key) {
  const k = String(key || "");
  mem.delete(k);
  const s = _storage();
  if (s) {
    try { s.removeItem(PREFIX + k); } catch (_) { /* nothing to do */ }
  }
}
