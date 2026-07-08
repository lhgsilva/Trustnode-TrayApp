// API wrappers for the TrustNode Intelligence module.
// Auth: pull the bearer token from the same localStorage key the host
// app uses, so authenticated calls (chats, messages, insights) carry
// the user's JWT.

const PATH_BASE = "/api/intelligence";

// Host-app key. Must match AUTH_TOKEN_KEY in frontend/src/api.js.
function _authToken() {
  try {
    return localStorage.getItem("trustnode_auth_token") || "";
  } catch { return ""; }
}

// Operator 2026-06-30: in the packaged Electron build the SPA loads from
// `file://` so a bare `fetch("/api/...")` resolves to file:///api/... and
// fails with "Failed to fetch". The host bootloader passes the real
// backend origin as `?backendUrl=...` (see desktop/main.js loadFile call);
// we mirror the host api.js logic: query-param wins, otherwise fall back
// to window.location.origin (works in browser/dev), otherwise localhost.
function _apiBase() {
  try {
    const q = new URLSearchParams(window.location.search || "").get("backendUrl") || "";
    if (q) return String(q).replace(/\/+$/, "");
  } catch {}
  try {
    const origin = (window.location && window.location.origin) || "";
    if (origin && origin !== "null" && !origin.startsWith("file:")) {
      return origin.replace(/\/+$/, "");
    }
  } catch {}
  return "http://127.0.0.1:8000";
}

// Operator 2026-07-03: de-dupe concurrent identical GETs. The UI can fire
// the same /status or /chats request from multiple effects at once; sharing
// one in-flight promise avoids opening redundant connections (which piled up
// as TIME_WAIT and, in bursts, hit the browser's ~6-conn/host limit →
// "Failed to fetch"). Only GETs are de-duped (POST/DELETE are unique).
const _inflight = new Map();

// Operator 2026-07-08 (LIGHT + POLITE): the host app runs ~22 background
// pollers that share Chromium's ~6 sockets-per-host under the Electron file://
// origin. On a BUSY edge (gateways collecting) those pollers keep the pool hot,
// so an intelligence request queues behind them. Two things keep this module
// fast + light WITHOUT touching the host app:
//   1. A small CONCURRENCY GATE so this module never opens more than
//      _MAX_CONCURRENT quick requests at once — it stays polite to the shared
//      pool AND its own list/status/get calls don't self-compete for sockets.
//      Long AI turns (send / insight run) BYPASS the gate: they're few, and
//      holding a gate slot for 90s would block the snappy CRUD calls.
//   2. A tiny in-memory response CACHE for list/status GETs (below) so repeat
//      loads are INSTANT and a momentary fetch failure falls back to the last
//      good value instead of surfacing "Failed to fetch".
const _MAX_CONCURRENT = 2;
let _active = 0;
const _waiters = [];
function _acquireSlot() {
  if (_active < _MAX_CONCURRENT) { _active++; return Promise.resolve(); }
  return new Promise((resolve) => _waiters.push(resolve));
}
function _releaseSlot() {
  _active = Math.max(0, _active - 1);
  const next = _waiters.shift();
  if (next) { _active++; next(); }
}

// Lightweight response cache for idempotent GETs (list/status/catalog/presets).
// Value + timestamp; callers read it as an instant fallback. Not a substitute
// for the server response — just a smoothing layer so the UI is never blank or
// errored while a refresh is in flight.
const _respCache = new Map();      // url -> {value, at}
function _cacheGet(url) { return _respCache.get(url); }
function _cacheSet(url, value) { _respCache.set(url, { value, at: Date.now() }); }

// Per-request timeout so a slow/hung call NEVER holds a connection slot
// forever. AI sends get a long budget; everything else is snappy.
// Operator 2026-07-03: raised the CRUD timeout 15s -> 30s. Under momentary
// connection-pool pressure a create/list/get can spend a few seconds just
// waiting for a socket; a 15s abort was firing DURING that wait and surfacing
// as "signal is aborted / Failed to fetch". 30s + the retry wrapper below
// gives the connection time to establish, then the request itself is <100ms.
function _timeoutFor(method, path) {
  if (method === "POST" && path.includes("/messages")) return 90000; // AI turn
  if (path.includes("/insights/") && path.includes("/run")) return 90000;
  return 30000;
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = _authToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  // No `credentials: include` — the auth token is in the Authorization
  // header, and credentials mode breaks CORS preflight for POST/DELETE from
  // the Electron file:// (null) origin. See the host app's api.js.
  const url = `${_apiBase()}${PATH_BASE}${path}`;
  const key = method === "GET" && body === undefined ? `${method} ${url}` : null;

  // Operator 2026-07-03 (UI-TIMING INSTRUMENTATION): the operator reports
  // create-chat ~20s and chat-switch ~40s in the UI though the backend +
  // network are <15ms. Log the real per-request lifecycle to the console so
  // we can see EXACTLY where the time goes: queued (waiting for a connection
  // slot / event loop), fetch (network+server), parse (reading the body).
  const _t0 = (typeof performance !== "undefined" ? performance.now() : Date.now());
  const _tlog = (phase, extra) => {
    try {
      const dt = ((typeof performance !== "undefined" ? performance.now() : Date.now()) - _t0);
      // eslint-disable-next-line no-console
      console.log(`[tn-intel-timing] ${method} ${path} ${phase} +${dt.toFixed(0)}ms${extra ? " " + extra : ""}`);
    } catch {}
  };

  // Fetch + fully parse into a resolved value {ok, data} so shared callers
  // don't fight over a single Response body (which can only be read once).
  const doFetchAndParse = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), _timeoutFor(method, path));
    _tlog("fetch-start");
    // CRITICAL latency fix (2026-07-03 update): do NOT force `cache:"no-store"`
    // at all — not even on GETs. On the Electron file:// (null) origin,
    // `no-store` makes Chromium open a BRAND-NEW TCP socket per request (no
    // keep-alive reuse), so requests queue on the ~6-conn/host limit behind the
    // host app's many pollers. The backend already sends `Cache-Control:
    // no-store, no-cache, must-revalidate` on every response (verified), so
    // freshness is guaranteed server-side regardless. Omitting the client flag
    // lets ALL intelligence requests (create/switch/send) reuse keep-alive
    // connections instead of competing for new sockets.
    const opts = { method, headers, signal: controller.signal };
    if (body !== undefined) opts.body = JSON.stringify(body);
    // Long AI turns bypass the concurrency gate (they're few and would block
    // quick CRUD for up to 90s if they held a slot); everything else waits for
    // a free slot so this module opens at most _MAX_CONCURRENT sockets.
    const _isLongAi = _timeoutFor(method, path) >= 90000;
    let res;
    if (_isLongAi) {
      try { res = await fetch(url, opts); } finally { clearTimeout(timer); }
    } else {
      await _acquireSlot();
      try {
        res = await fetch(url, opts);
      } finally {
        clearTimeout(timer);
        _releaseSlot();
      }
    }
    _tlog("fetch-done", `status=${res.status}`);
    if (!res.ok) {
      if (res.status === 404) throw new Error("intelligence_not_licensed");
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j?.detail || j?.error || detail;
      } catch {}
      throw new Error(`intelligence_api_${res.status}: ${detail}`);
    }
    const parsed = await res.json();
    _tlog("parse-done");
    return parsed;
  };

  // Operator 2026-07-03 (RESILIENCE): retry on transient CONNECTION failures.
  // Under host-app socket churn the ephemeral-port pool can be momentarily
  // exhausted, so fetch() fails with "Failed to fetch" / "signal is aborted"
  // (the connection never established). These are NOT server errors — a short
  // wait lets a TIME_WAIT port free, and the retry usually succeeds instantly.
  // We do NOT retry HTTP errors (4xx/5xx come back as a real response) — only
  // connection-level failures. Bounded so a genuinely-down backend still fails.
  const _isTransientConnErr = (e) => {
    const m = String(e?.message || e || "").toLowerCase();
    return m.includes("failed to fetch")
      || m.includes("aborted")
      || m.includes("network")
      || m.includes("load failed")
      || m.includes("err_")               // ERR_INSUFFICIENT_RESOURCES, etc.
      || m.includes("insufficient");
  };
  const _withRetry = async () => {
    const MAX = 3;
    for (let attempt = 1; attempt <= MAX; attempt++) {
      try {
        return await doFetchAndParse();
      } catch (e) {
        if (attempt < MAX && _isTransientConnErr(e)) {
          _tlog(`retry ${attempt}/${MAX - 1}`, String(e?.message || e).slice(0, 40));
          // Short, escalating wait so a churned port pool can recover.
          await new Promise((r) => setTimeout(r, 250 * attempt));
          continue;
        }
        throw e;
      }
    }
  };

  if (key) {
    let shared = _inflight.get(key);
    if (!shared) {
      shared = _withRetry().finally(() => _inflight.delete(key));
      _inflight.set(key, shared);
    }
    return shared;
  }
  return _withRetry();
}

// Cache-first GET for idempotent list/status calls. Behavior:
//   - Always attempts the network (through the de-dup + gate + retry above).
//   - On SUCCESS: updates the cache and returns fresh data.
//   - On FAILURE (transient connection error under a saturated pool): returns
//     the last cached value if we have one, so the UI keeps showing the prior
//     list instead of "Failed to fetch". If there's no cache, the error
//     propagates so the caller can decide (first load with no data).
// This makes chat/insight lists feel INSTANT + RELIABLE on a busy edge without
// changing anything outside this module.
async function requestCached(method, path) {
  const url = `${_apiBase()}${PATH_BASE}${path}`;
  try {
    const fresh = await request(method, path);
    _cacheSet(url, fresh);
    return fresh;
  } catch (e) {
    const cached = _cacheGet(url);
    if (cached) return cached.value;   // graceful degrade to last good data
    throw e;
  }
}

// Synchronous peek at the cache (no network) — lets a page paint instantly from
// the last good value while requestCached refreshes in the background.
export function peekCached(path) {
  const url = `${_apiBase()}${PATH_BASE}${path}`;
  const c = _cacheGet(url);
  return c ? c.value : null;
}

export const intelligenceApi = {
  // Idempotent GETs go through requestCached: instant on repeat + graceful
  // fallback to last-good data if a refresh momentarily fails under pool load.
  getStatus: () => requestCached("GET", "/status"),
  listTools: () => requestCached("GET", "/tools"),
  getCatalog: () => requestCached("GET", "/catalog"),
  getPresets: () => requestCached("GET", "/presets"),
  savePresets: (categories) => request("PUT", "/presets", { categories }),
  resetPresets: () => request("DELETE", "/presets"),
  // Sync cache peek so pages can paint instantly before the network returns.
  peekChats: () => peekCached("/chats"),
  peekInsights: () => peekCached("/insights"),

  // Chats
  createChat: ({ title, data_source } = {}) =>
    request("POST", "/chats", { title: title || "New chat", data_source: data_source || "local" }),
  listChats: () => requestCached("GET", "/chats"),
  getChat: (id) => request("GET", `/chats/${encodeURIComponent(id)}`),
  renameChat: (id, title) => request("PATCH", `/chats/${encodeURIComponent(id)}`, { title }),
  deleteChat: (id) => request("DELETE", `/chats/${encodeURIComponent(id)}`),
  sendMessage: (chatId, message, dataSource, mode) =>
    request("POST", `/chats/${encodeURIComponent(chatId)}/messages`,
      { chat_id: chatId, message, data_source: dataSource || null, mode: mode || "smart" }),

  // Insights
  createInsight: (payload) => request("POST", "/insights", payload),
  listInsights: () => requestCached("GET", "/insights"),
  getInsight: (id) => request("GET", `/insights/${encodeURIComponent(id)}`),
  runInsight: (id) => request("POST", `/insights/${encodeURIComponent(id)}/run`),
  deleteInsight: (id) => request("DELETE", `/insights/${encodeURIComponent(id)}`),
  // Per-insight run history (right column of Insights page).
  listInsightRuns: (id, limit = 100) =>
    request("GET", `/insights/${encodeURIComponent(id)}/runs?limit=${encodeURIComponent(limit)}`),
  deleteInsightRun: (insightId, runId) =>
    request("DELETE", `/insights/${encodeURIComponent(insightId)}/runs/${encodeURIComponent(runId)}`),
};

export default intelligenceApi;
