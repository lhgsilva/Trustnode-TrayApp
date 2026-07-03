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
    let res;
    try {
      res = await fetch(url, opts);
    } finally {
      clearTimeout(timer);
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

export const intelligenceApi = {
  getStatus: () => request("GET", "/status"),
  listTools: () => request("GET", "/tools"),
  getCatalog: () => request("GET", "/catalog"),
  getPresets: () => request("GET", "/presets"),
  savePresets: (categories) => request("PUT", "/presets", { categories }),
  resetPresets: () => request("DELETE", "/presets"),

  // Chats
  createChat: ({ title, data_source } = {}) =>
    request("POST", "/chats", { title: title || "New chat", data_source: data_source || "local" }),
  listChats: () => request("GET", "/chats"),
  getChat: (id) => request("GET", `/chats/${encodeURIComponent(id)}`),
  renameChat: (id, title) => request("PATCH", `/chats/${encodeURIComponent(id)}`, { title }),
  deleteChat: (id) => request("DELETE", `/chats/${encodeURIComponent(id)}`),
  sendMessage: (chatId, message, dataSource, mode) =>
    request("POST", `/chats/${encodeURIComponent(chatId)}/messages`,
      { chat_id: chatId, message, data_source: dataSource || null, mode: mode || "smart" }),

  // Insights
  createInsight: (payload) => request("POST", "/insights", payload),
  listInsights: () => request("GET", "/insights"),
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
