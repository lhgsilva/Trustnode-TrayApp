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
function _timeoutFor(method, path) {
  if (method === "POST" && path.includes("/messages")) return 90000; // AI turn
  if (path.includes("/insights/") && path.includes("/run")) return 90000;
  return 15000;
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

  // Fetch + fully parse into a resolved value {ok, data} so shared callers
  // don't fight over a single Response body (which can only be read once).
  const doFetchAndParse = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), _timeoutFor(method, path));
    // CRITICAL latency fix: only force `cache: "no-store"` on GETs. On the
    // Electron file:// (null) origin, `no-store` makes Chromium open a BRAND
    // NEW TCP socket for every request (no connection reuse). With ~6 conns/
    // host, the AI POST would queue for 12-40s behind the mount GETs
    // (status/chats/messages) instead of reusing an open keep-alive socket.
    // GETs still bypass the HTTP cache so live data is never stale; POSTs are
    // never cached by the browser anyway, so dropping the flag is pure win —
    // the reply now returns in <1s (the request was the queue wait, not AI).
    const opts = { method, headers, signal: controller.signal };
    if (method === "GET") opts.cache = "no-store";
    if (body !== undefined) opts.body = JSON.stringify(body);
    let res;
    try {
      res = await fetch(url, opts);
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      if (res.status === 404) throw new Error("intelligence_not_licensed");
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j?.detail || j?.error || detail;
      } catch {}
      throw new Error(`intelligence_api_${res.status}: ${detail}`);
    }
    return res.json();
  };

  if (key) {
    let shared = _inflight.get(key);
    if (!shared) {
      shared = doFetchAndParse().finally(() => _inflight.delete(key));
      _inflight.set(key, shared);
    }
    return shared;
  }
  return doFetchAndParse();
}

export const intelligenceApi = {
  getStatus: () => request("GET", "/status"),
  listTools: () => request("GET", "/tools"),

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
