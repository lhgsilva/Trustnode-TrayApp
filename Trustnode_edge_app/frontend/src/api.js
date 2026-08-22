const STORAGE_MODE_KEY = "trustnode_backend_mode";
const STORAGE_CLOUD_URL_KEY = "trustnode_backend_cloud_url";
const AUTH_TOKEN_KEY = "trustnode_auth_token";
const FORCE_CLOUD_URL_RAW = normalizeBaseUrl(import.meta.env.VITE_TRUSTNODE_FORCE_CLOUD_URL || "");
const FORCE_CLOUD_URL =
  /(^https?:\/\/your-cloud-backend\.example\.com$)|(^https?:\/\/api\.example\.com$)/i.test(FORCE_CLOUD_URL_RAW)
    ? ""
    : FORCE_CLOUD_URL_RAW;
// 2026-07-15: no hardcoded portal host. The control-plane URL comes from a
// build-time env (VITE_TRUSTNODE_CONTROL_PLANE_URL) when set; otherwise the app
// resolves same-origin (hosted portal / cloud web client) or, on a file:// edge,
// the URL delivered by the activation code + persisted server-side. An empty
// fallback here is handled by the `if (CONTROL_PLANE_FALLBACK_URL)` guards below.
const CONTROL_PLANE_FALLBACK_URL = normalizeBaseUrl(
  import.meta.env.VITE_TRUSTNODE_CONTROL_PLANE_URL || ""
);
const FORCE_READONLY = String(import.meta.env.VITE_TRUSTNODE_READONLY || "").toLowerCase() === "true";
const FORCE_CLIENT_VIEW = String(import.meta.env.VITE_TRUSTNODE_CLIENT_VIEW || "").toLowerCase() === "true";

function normalizeBaseUrl(raw) {
  if (!raw) return "";
  return raw.trim().replace(/\/+$/, "");
}

function getDefaultLocalApiBase() {
  const fromQuery = new URLSearchParams(window.location.search).get("backendUrl");
  if (fromQuery) return normalizeBaseUrl(fromQuery);
  if (window.location.protocol === "file:") return "http://127.0.0.1:8000";
  if (window.location.protocol === "https:" || window.location.protocol === "http:") {
    // For hosted web deployments, default to same-origin API to avoid mixed-content ws:// issues.
    return normalizeBaseUrl(window.location.origin);
  }
  return "";
}

// Runtime surface detection (plan 2026-08-21 §3.3/§3.5). ONE predicate for
// "where is this bundle running", so the ~40 ad-hoc guards can converge on it:
//   desktop    — Electron shell, file:// bundle, or an explicit ?backendUrl=
//                override (dev server pointing at a local edge).
//   lan_full   — this React app served by the edge itself at
//                /trustnode/full/app/  ("TrustNode Edge" over the LAN).
//   lan_client — the clientview build served at /trustnode/client/app/
//                ("TrustNode Local View").
//   lan_lite   — the frozen vanilla Lite at /trustnode/lite/app/.
//   cloud      — any other http(s) non-localhost origin (hosted cloud portal /
//                TrustNode Cloud View on the VPS).
// lan_* surfaces are SAME-ORIGIN with the edge API: they must never be
// treated as the hosted cloud portal (that routed reads to Supabase and
// polluted localStorage with mode="cloud" — plan §2.5 landmine 6).
const LAN_SURFACE_PATH_RE = /^\/trustnode\/(full|client|lite)\/app(?:\/|$)/;

export function getRuntimeSurface() {
  try {
    const loc = window.location || {};
    const protocol = String(loc.protocol || "").toLowerCase();
    const host = String(loc.hostname || "").toLowerCase();
    const pathname = String(loc.pathname || "");
    const userAgent = String(window.navigator?.userAgent || "");
    if (/electron/i.test(userAgent)) return "desktop";
    if (protocol === "file:") return "desktop";
    if (new URLSearchParams(String(loc.search || "")).get("backendUrl")) return "desktop";
    const lan = pathname.match(LAN_SURFACE_PATH_RE);
    if (lan) return `lan_${lan[1]}`;
    const isLocalHost = host === "localhost" || host === "127.0.0.1" || host === "::1";
    if ((protocol === "https:" || protocol === "http:") && !isLocalHost) return "cloud";
    return "desktop";
  } catch {
    return "desktop";
  }
}

// True only for the LAN-served surfaces (the edge serving its own bundles).
export function isLanServedRuntime() {
  return String(getRuntimeSurface()).startsWith("lan_");
}

// True ONLY for the hosted cloud portal. A LAN-served full app is the edge
// itself (same-origin API), so it is NOT a hosted web client.
export function isHostedWebClientRuntime() {
  return getRuntimeSurface() === "cloud";
}

export function getBackendTarget() {
  if (FORCE_CLOUD_URL) {
    return { mode: "cloud", cloudUrl: FORCE_CLOUD_URL, forced: true };
  }
  if (isHostedWebClientRuntime()) {
    const cloudUrl = normalizeBaseUrl(localStorage.getItem(STORAGE_CLOUD_URL_KEY) || window.location.origin || "");
    return { mode: "cloud", cloudUrl, forced: false };
  }
  if (isLanServedRuntime()) {
    // Served by this edge over the LAN: the API is always same-origin.
    // Ignore any mode="cloud" a pre-fix session may have left in
    // localStorage so reads never get redirected to a cloud URL.
    const cloudUrl = localStorage.getItem(STORAGE_CLOUD_URL_KEY) || "";
    return { mode: "local", cloudUrl, lan: true };
  }
  const mode = localStorage.getItem(STORAGE_MODE_KEY) || "local";
  const cloudUrl = localStorage.getItem(STORAGE_CLOUD_URL_KEY) || "";
  return { mode, cloudUrl };
}

export function setBackendTarget(mode, cloudUrl = "") {
  if (FORCE_CLOUD_URL) return;
  const hosted = isHostedWebClientRuntime();
  // lan_* surfaces never persist mode="cloud": the bundle is served by the
  // edge it talks to, so "local" (same-origin) is the only valid target.
  const nextMode = hosted ? "cloud" : (isLanServedRuntime() ? "local" : mode);
  const nextCloud = hosted
    ? normalizeBaseUrl(cloudUrl || window.location.origin || "")
    : normalizeBaseUrl(cloudUrl);
  localStorage.setItem(STORAGE_MODE_KEY, nextMode);
  localStorage.setItem(STORAGE_CLOUD_URL_KEY, nextCloud);
}

function getApiBase() {
  if (FORCE_CLOUD_URL) return FORCE_CLOUD_URL;
  const { mode, cloudUrl } = getBackendTarget();
  if (mode === "cloud" && cloudUrl) return normalizeBaseUrl(cloudUrl);
  return getDefaultLocalApiBase();
}

function getControlApiBase() {
  if (FORCE_CLOUD_URL) return FORCE_CLOUD_URL;
  const queryBackend = normalizeBaseUrl(new URLSearchParams(window.location.search).get("backendUrl") || "");
  if (queryBackend) return queryBackend;
  if (!isHostedWebClientRuntime()) return getDefaultLocalApiBase();
  return getApiBase();
}

function getAppStoreApiBase() {
  // In desktop/local runtime, app-store domains (historian/live/logs/config)
  // must stay bound to the local edge backend source-of-truth.
  // Hosted web client keeps same-origin/cloud behavior.
  if (!isHostedWebClientRuntime()) return getControlApiBase();
  return getApiBase();
}

function withNoCache(url) {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}_ts=${Date.now()}`;
}

export function isForcedReadonlyCloudMode() {
  return Boolean(FORCE_CLOUD_URL && FORCE_READONLY);
}

// Single-file customer portal build: hide admin-only menus regardless of
// the JWT permissions (defense-in-depth — backend RLS + JWT remain the
// real enforcement). Toggled at build time via VITE_TRUSTNODE_CLIENT_VIEW.
export function isClientViewMode() {
  return FORCE_CLIENT_VIEW;
}

export function getWsStreamUrl() {
  const apiBase = getApiBase();
  const token = getAuthToken();
  if (!apiBase) {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/ws/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  }
  const wsBase = apiBase.startsWith("https://")
    ? apiBase.replace("https://", "wss://")
    : apiBase.replace("http://", "ws://");
  return `${wsBase}/ws/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}

export function getCloudWsStreamUrl() {
  const apiBase = getApiBase();
  const token = getAuthToken();
  if (!apiBase) {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/ws/cloud-live${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  }
  const wsBase = apiBase.startsWith("https://")
    ? apiBase.replace("https://", "wss://")
    : apiBase.replace("http://", "ws://");
  return `${wsBase}/ws/cloud-live${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
    if (token && !headers.Authorization) {
      headers.Authorization = `Bearer ${token}`;
    }
    // Operator 2026-07-03 (CONNECTION-REUSE FIX): do NOT force `cache:"no-store"`.
    // On the Electron file:// (null) origin, `no-store` makes Chromium open a
    // BRAND-NEW TCP socket for EVERY request (no keep-alive reuse). With 22
    // setInterval pollers here (several at 2-3s) each opening a fresh socket,
    // Chromium's ~6-conn/host limit is saturated continuously — so an on-demand
    // request (e.g. the Intelligence create-chat / switch-chat) QUEUES 20-40s
    // waiting for a free connection slot.
    //
    // Dropping the client `no-store` is SAFE for freshness: the backend already
    // sends `Cache-Control: no-store, no-cache, must-revalidate` on every
    // response (verified), so the browser never serves a cached API response
    // regardless. Removing the client flag just lets connections REUSE via
    // keep-alive, freeing the pool. An explicit per-call `cache` option still
    // wins if a caller sets one.
    const hasCacheOption = Object.prototype.hasOwnProperty.call(options || {}, "cache");
    const finalOptions = hasCacheOption
      ? { ...options, headers, signal: controller.signal }
      : { ...options, headers, signal: controller.signal };
    return await fetch(url, finalOptions);
  } finally {
    clearTimeout(timeout);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function isTransientFetchError(err) {
  const msg = String(err?.message || err || "").toLowerCase();
  return (
    msg.includes("aborterror") ||
    msg.includes("failed to fetch") ||
    msg.includes("networkerror") ||
    msg.includes("load failed") ||
    msg.includes("signal is aborted")
  );
}

export function setAuthToken(token) {
  if (token) localStorage.setItem(AUTH_TOKEN_KEY, String(token));
}

export function clearAuthToken() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}

export function getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY) || "";
}

export async function loginAuth(payloadOrUsername, maybePassword) {
  const payload =
    payloadOrUsername && typeof payloadOrUsername === "object"
      ? payloadOrUsername
      : {
          username: String(payloadOrUsername || ""),
          password: String(maybePassword || ""),
        };
  const request = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const timeoutMs = 20000 + (attempt - 1) * 10000;
      res = await fetchWithTimeout(`${getApiBase()}/api/auth/login`, request, timeoutMs);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 3) break;
      await sleep(250 * attempt);
    }
  }
  if (!res) {
    throw lastErr || new Error("Login failed: network timeout");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      detail = await res.text();
    }
    throw new Error(`Login failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  const data = await res.json();
  if (data?.token) setAuthToken(data.token);
  return data;
}

export async function getAuthMe() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/auth/me`);
  if (!res.ok) throw new Error("Auth session invalid");
  return res.json();
}

export async function getHealth() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/health`);
  if (!res.ok) throw new Error("Health check failed");
  return res.json();
}

// Operator 2026-06-25: lightweight TCP-probe of every configured
// device + DB. Public endpoint (no auth) — same one the Electron
// splash uses on boot. Cards poll this on a short interval to show
// ONLINE/OFFLINE without ever sitting on "Checking…".
export async function getBootProbe() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/boot-probe`, {}, 8000);
  if (!res.ok) throw new Error("Boot probe failed");
  return res.json();
}

// Operator 2026-06-25: POST variant — the UI tells the backend
// exactly which IPs/hosts to probe. Bypasses tenant-scoping mismatch
// where bootstrap couldn't see the active scope's devices.
export async function postBootProbe(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/boot-probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  }, 8000);
  if (!res.ok) throw new Error("Boot probe failed");
  return res.json();
}

// Operator 2026-06-24: admin toggle for the canonical-DB read routing.
export async function setForceSqliteReads(enabled) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/historian/force-sqlite-reads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  });
  if (!res.ok) throw new Error(`Force SQLite toggle failed (${res.status})`);
  return res.json();
}

// Operator 2026-06-18: workspace export/import — the user-visible safety
// net for "I'm afraid to update because I'll lose my data." See the
// /api/workspace router on the backend. Admin-only, JWT-gated.
export async function exportWorkspace() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/workspace/export`, {}, 60000);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail || ""; } catch (_) {}
    throw new Error(detail || `Export failed (${res.status})`);
  }
  return res.json();
}

export async function importWorkspace(payload, opts = {}) {
  const body = {
    ...payload,
    skip_domains: Array.isArray(opts?.skip_domains) ? opts.skip_domains : [],
  };
  const res = await fetchWithTimeout(`${getApiBase()}/api/workspace/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 60000);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail || ""; } catch (_) {}
    throw new Error(detail || `Import failed (${res.status})`);
  }
  return res.json();
}

export async function getConfig() {
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const timeoutMs = 15000 + (attempt - 1) * 5000;
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/config`, {}, timeoutMs);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 3) break;
      await sleep(300 * attempt);
    }
  }
  if (!res) throw lastErr || new Error("Config fetch failed");
  if (!res.ok) throw new Error("Config fetch failed");
  return res.json();
}

export async function updateConfig(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Config update failed");
  return res.json();
}

export async function getStatus() {
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const timeoutMs = 12000 + (attempt - 1) * 4000;
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/status`, {}, timeoutMs);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 2) break;
      await sleep(250 * attempt);
    }
  }
  if (!res) throw lastErr || new Error("Status fetch failed");
  if (!res.ok) throw new Error("Status fetch failed");
  return res.json();
}

export async function startGateway() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/start`, { method: "POST" });
  if (!res.ok) throw new Error("Start failed");
  return res.json();
}

export async function stopGateway() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/stop`, { method: "POST" });
  if (!res.ok) throw new Error("Stop failed");
  return res.json();
}

export async function startGatewayInstance(payload) {
  const request = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const timeoutMs = 45000 + (attempt - 1) * 15000;
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/gateways/start`, request, timeoutMs);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 3) break;
      await sleep(300 * attempt);
    }
  }
  if (!res) throw lastErr || new Error("Gateway instance start failed");
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || body?.message || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    throw new Error(`Gateway instance start failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function stopGatewayInstance(gatewayId) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/gateways/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gateway_id: gatewayId })
  }, 20000);
  if (!res.ok) throw new Error("Gateway instance stop failed");
  return res.json();
}

export async function getGatewayInstanceStatuses() {
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/gateways/status`, {}, 20000);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 2) break;
      await sleep(200 * attempt);
    }
  }
  if (!res) throw lastErr || new Error("Gateway instance status fetch failed");
  if (!res.ok) throw new Error("Gateway instance status fetch failed");
  return res.json();
}

export async function stopAllGatewayInstances() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/gateways/stop-all`, {
    method: "POST"
  }, 20000);
  if (!res.ok) throw new Error("Stop all gateways failed");
  return res.json();
}

export async function testPlcConnection(payload) {
  // Operator 2026-06-24: single attempt @ 5s. The previous 3 retries
  // × 20s was waiting 60+ seconds before giving the operator a
  // result, which felt broken. A real PLC on a local network
  // responds in <100 ms; if it doesn't respond in 5 s it's not
  // going to respond in 60 either. The periodic check timer will
  // re-fire in 15 s anyway.
  const request = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  let res;
  try {
    res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/test-connection`, request, 5000);
  } catch (err) {
    if (isTransientFetchError(err)) {
      throw new Error(
        "Connection test timed out (5s). The next periodic check will retry automatically."
      );
    }
    throw err;
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    if (res.status === 404) {
      throw new Error(
        "Backend does not support /api/plc/test-connection (404). Restart with updated backend build."
      );
    }
    throw new Error(`Connection test failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function discoverPlcTags(payload) {
  // Tag discovery on a controller with thousands of tags can take
  // 30–60 s — pylogix walks every program scope and we fan-out
  // array indices. Operator 2026-06-12: "could not fetch and
  // should". The previous 12 s default timed out the request mid-
  // walk. Allow up to 120 s here; the operator can cancel by
  // closing the modal.
  const res = await fetchWithTimeout(
    `${getControlApiBase()}/api/plc/discover-tags`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    120000,
  );
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    if (res.status === 404) {
      throw new Error(
        "Tag discovery endpoint not found (HTTP 404). You are running an old backend build. Restart/rebuild backend and desktop package."
      );
    }
    throw new Error(`Tag discovery failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function exportHistorianXlsx(payload) {
  // Returns the Blob directly so the caller can trigger a download.
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/historian/export-xlsx`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    },
    180000,
  );
  if (!res.ok) {
    let detail = "";
    try { detail = await res.text(); } catch (_) { detail = ""; }
    throw new Error(`Excel export failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.blob();
}

export async function downloadHistorianXlsxReferenceTemplate() {
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/historian/export-xlsx/reference-template`,
    {},
    30000,
  );
  if (!res.ok) {
    let detail = "";
    try { detail = await res.text(); } catch (_) { detail = ""; }
    throw new Error(`Reference template download failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.blob();
}

export async function discoverPlcNetwork(payload) {
  // Operator 2026-06-18: the network scan walks every IP on every
  // local /24 attached to the edge — on a multi-NIC plant box this
  // is several hundred TCP probes that legitimately take 20-60s.
  // The default fetchWithTimeout cap (12s) aborted the request mid-
  // scan with "signal is aborted without reason" and the UI claimed
  // "Discovery failed". Cap raised to 90s here so the operator sees
  // the real result list when the network is large and slow.
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/discover-network`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  }, 90000);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try { detail = await res.text(); } catch { detail = ""; }
    }
    if (res.status === 404) {
      throw new Error(
        "Network discovery endpoint not found (HTTP 404). Restart/rebuild the backend.",
      );
    }
    throw new Error(`Network discovery failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function browseOpcUaNodes(payload) {
  const request = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  const requestedTimeout = Math.max(12000, Number(payload?.timeout_ms || 0));
  const networkTimeoutMs = Math.min(60000, requestedTimeout + 12000);
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const attemptTimeout = networkTimeoutMs + (attempt - 1) * 6000;
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/opcua/browse`, request, attemptTimeout);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 3) break;
      await sleep(300 * attempt);
    }
  }
  if (!res) {
    if (isTransientFetchError(lastErr)) {
      throw new Error("OPC-UA browse timeout/network interruption. Check PLC route and retry.");
    }
    throw lastErr || new Error("OPC-UA browse request failed");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    if (res.status === 404) {
      throw new Error(
        "OPC-UA browse endpoint not found (HTTP 404). You are running an old backend build. Restart/rebuild backend and desktop package."
      );
    }
    throw new Error(`OPC-UA browse failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function previewCsvFormat({ csvFormat = "", csvHeader = "", sampleRows = 3 } = {}) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/database/csv-preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ csv_format: csvFormat, csv_header: csvHeader, sample_rows: sampleRows }),
  }, 8000);
  await ensureOk(res, "CSV preview failed");
  return res.json();
}

export async function testDatabaseConnection(payload) {
  let res;
  const networkTimeoutMs = Math.max(15000, Number(payload?.timeout_ms || 0) + 3000);
  try {
    res = await fetchWithTimeout(`${getControlApiBase()}/api/database/test-connection`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }, networkTimeoutMs);
  } catch (err) {
    if (isTransientFetchError(err)) {
      throw new Error(
        "Database test request failed (network interrupted). Please retry."
      );
    }
    throw err || new Error("Database test request failed");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      const raw = body?.detail ?? body;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    if (res.status === 404) {
      throw new Error(
        "Backend does not support /api/database/test-connection (404). Restart with updated backend build."
      );
    }
    if (
      res.status === 422 &&
      (payload?.engine === "csv_file" || payload?.engine === "txt_file")
    ) {
      throw new Error(
        `Database connection test failed (HTTP 422): backend build does not support '${payload.engine}' yet. Restart/rebuild backend and desktop package. Details: ${detail}`
      );
    }
    throw new Error(`Database connection test failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function provisionDatabaseObjects(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/database/provision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    throw new Error(`Database provisioning failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function activateDatabaseSink(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/database/activate-sink`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    if (res.status === 404) {
      throw new Error(
        "Backend does not support /api/database/activate-sink (404). You are connected to an old backend build."
      );
    }
    throw new Error(`Activate database sink failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function getUiSourceConfig() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/ui-source/config`);
  if (!res.ok) throw new Error("UI source fetch failed");
  return res.json();
}

export async function setUiSourceConfig(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/ui-source/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("UI source update failed");
  return res.json();
}

export async function testUiSourceRemoteUrl(remoteUrl) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/ui-source/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remote_url: remoteUrl })
  });
  if (!res.ok) throw new Error("UI source test failed");
  return res.json();
}

export async function testNotificationEmail(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/notifications/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Email test failed");
  return res.json();
}

export async function sendNotificationEmail(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/notifications/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Email send failed");
  return res.json();
}

export async function getAppStoreBootstrap() {
  let res;
  let lastErr = null;
  const request = {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" }
  };
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const timeoutMs = 20000 + (attempt - 1) * 10000;
      res = await fetchWithTimeout(withNoCache(`${getAppStoreApiBase()}/api/app-store/bootstrap`), request, timeoutMs);
      lastErr = null;
      break;
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === 3) break;
      await sleep(250 * attempt);
    }
  }
  if (!res) throw (lastErr || new Error("App store bootstrap fetch failed"));
  if (!res.ok) throw new Error("App store bootstrap fetch failed");
  return res.json();
}

// Operator 2026-06-17 (M2): customer-DB mode wrappers. Used by
// Settings → Database → Customer Database to test, activate, and revert.
// See backend/app/routers/customer_db.py.
export async function getCustomerDbStatus() {
  const res = await fetchWithTimeout(
    withNoCache(`${getAppStoreApiBase()}/api/customer-db/status`),
    { headers: { "Cache-Control": "no-store" } },
    6000
  );
  if (!res.ok) throw new Error("customer-db status failed");
  return res.json();
}
export async function testCustomerDbConnection(target) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/customer-db/test-connection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target }),
  }, 12000);
  if (!res.ok) throw new Error("customer-db test-connection failed");
  return res.json();
}
export async function activateCustomerDb(target, confirmBackup = false) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/customer-db/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, confirm_backup: !!confirmBackup }),
  }, 6000);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.detail || "customer-db activate failed");
  return body;
}
export async function deactivateCustomerDb() {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/customer-db/deactivate`, {
    method: "POST",
  }, 6000);
  if (!res.ok) throw new Error("customer-db deactivate failed");
  return res.json();
}

export async function saveAppStoreBootstrap(data, actor = "system") {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/bootstrap`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data, actor })
  });
  if (!res.ok) throw new Error("App store bootstrap save failed");
  return res.json();
}

export async function saveAppStoreDomain(domain, payload, actor = "system", options = {}) {
  // `allowEmpty` must be set ONLY by a deliberate operator action that empties a
  // collection (removing the last widget, clearing a dashboard). The server
  // refuses to blank a saved collection otherwise — on 2026-08-22 a session that
  // rendered no widgets persisted an empty list over three saved ones.
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/domain`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, payload, actor, allow_empty: Boolean(options.allowEmpty) })
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    throw new Error(`App store domain save failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

export async function getAppStoreTenantContext() {
  const res = await fetchWithTimeout(withNoCache(`${getAppStoreApiBase()}/api/app-store/tenant/context`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" }
  });
  if (!res.ok) throw new Error("Tenant context fetch failed");
  return res.json();
}

// Data Continuity (operator 2026-06-19) ----------------------------------

export async function getTenantInventory() {
  const res = await fetchWithTimeout(withNoCache(`${getAppStoreApiBase()}/api/app-store/tenants/inventory`));
  if (!res.ok) throw new Error("Tenant inventory fetch failed");
  return res.json();
}

export async function listTenantAliases(includeArchived = false) {
  const q = includeArchived ? "?include_archived=1" : "";
  const res = await fetchWithTimeout(withNoCache(`${getAppStoreApiBase()}/api/app-store/tenant-aliases${q}`));
  if (!res.ok) throw new Error("Tenant aliases fetch failed");
  return res.json();
}

export async function upsertTenantAlias({ aliasTenantId, reason = "", archived = false }) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/tenant-aliases`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ alias_tenant_id: aliasTenantId, reason, archived }),
  });
  if (!res.ok) throw new Error("Tenant alias upsert failed");
  return res.json();
}

export async function deleteTenantAlias(aliasTenantId) {
  const res = await fetchWithTimeout(
    `${getAppStoreApiBase()}/api/app-store/tenant-aliases/${encodeURIComponent(aliasTenantId)}`,
    { method: "DELETE" }
  );
  if (!res.ok) throw new Error("Tenant alias delete failed");
  return res.json();
}

export async function recordDataContinuityDecision({ choice, note = "" }) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/data-continuity/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choice, note }),
  });
  if (!res.ok) throw new Error("Data continuity decision failed");
  return res.json();
}

export async function appendAppStoreHistorian(rows) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/append/historian`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows })
  });
  if (!res.ok) throw new Error("App store historian append failed");
  return res.json();
}

export async function appendAppStoreLogs(rows) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/append/logs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows })
  });
  if (!res.ok) throw new Error("App store logs append failed");
  return res.json();
}

function appendCloudEdgeParams(url, cloudEdge = null) {
  if (!cloudEdge || typeof cloudEdge !== "object") return url;
  const params = new URLSearchParams();
  const edgeId = String(cloudEdge.edge_id || cloudEdge.key || "").trim();
  const source = String(cloudEdge.source || "").trim();
  const site = String(cloudEdge.site || "").trim();
  const area = String(cloudEdge.area || "").trim();
  const equipment = String(cloudEdge.equipment || "").trim();
  if (edgeId) params.set("edge_id", edgeId);
  if (source) params.set("source", source);
  if (site) params.set("site", site);
  if (area) params.set("area", area);
  if (equipment) params.set("equipment", equipment);
  if (!params.toString()) return url;
  return `${url}${url.includes("?") ? "&" : "?"}${params.toString()}`;
}

export async function getAppStoreHistorian(limit = 1000, cloudEdge = null) {
  const useV1Cloud = getBackendTarget().mode === "cloud";
  if (useV1Cloud) {
    try {
      const v1Url = appendCloudEdgeParams(
        `${getApiBase()}/api/v1/history?limit=${encodeURIComponent(String(limit))}`,
        cloudEdge
      );
      const resV1 = await fetchWithTimeout(
        withNoCache(v1Url),
        { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
      );
      if (resV1.ok) {
        const data = await resV1.json();
        const rows = [];
        for (const sample of Array.isArray(data?.rows) ? data.rows : []) {
          const tags = Array.isArray(sample?.tags_json) ? sample.tags_json : [];
          for (const t of tags) {
            rows.push({
              ts: sample.sample_ts_utc,
              source: sample.customer_id || sample.tenant_id || sample.plc_driver_type || "",
              site: sample.plant_id || "",
              area: sample.gateway_id || "",
              equipment: sample.machine_id || "",
              gateway_id: sample.gateway_id || "",
              gateway_name: sample.gateway_id || "",
              device_name: sample.machine_id || "",
              plc_ip: sample.plc_endpoint_id || "",
              database_name: "cloud_v1",
              tag: String(t?.tag_name || ""),
              value: t?.value ?? null,
              quality: Number(t?.quality_code ?? sample.quality_code ?? 0),
              quality_label: String(t?.quality_label || ""),
              tenant_id: sample.tenant_id || "default",
              customer_id: sample.customer_id || "",
              plant_id: sample.plant_id || "",
              machine_id: sample.machine_id || "",
              edge_monotonic_seq: Number(sample.edge_monotonic_seq || 0),
              payload_hash_sha256: sample.payload_hash_sha256 || "",
              sample_age_ms: Number.isFinite(Date.parse(String(sample.sample_ts_utc || "")))
                ? Math.max(0, Date.now() - Date.parse(String(sample.sample_ts_utc || "")))
                : Number.POSITIVE_INFINITY,
            });
          }
        }
        // If v1 endpoint is healthy but currently empty, fall back to legacy mirror
        // so cloud UI remains populated during migration/cutover.
        if (rows.length > 0) return { ok: true, rows };
      }
    } catch {
      // Fallback to legacy app-store route below.
    }
  }
  const legacyUrl = appendCloudEdgeParams(
    `${getAppStoreApiBase()}/api/app-store/historian?limit=${encodeURIComponent(String(limit))}`,
    cloudEdge
  );
  const res = await fetchWithTimeout(
    withNoCache(legacyUrl),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store historian fetch failed");
  return res.json();
}

export async function getAppStoreHistorianRange({
  fromUtc = "",
  toUtc = "",
  limit = 5000,
  offset = 0,
  gateway = "",
  device = "",
  tag = "",
  cloudEdge = null,
  preferCloud = null,
  timeoutMs = 15000,
  maxAttempts = 3,
} = {}) {
  const params = new URLSearchParams();
  if (fromUtc) params.set("from_utc", String(fromUtc));
  if (toUtc) params.set("to_utc", String(toUtc));
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (gateway) params.set("gateway", String(gateway));
  if (device) params.set("device", String(device));
  if (tag) params.set("tag", String(tag));
  if (typeof preferCloud === "boolean") params.set("prefer_cloud", preferCloud ? "true" : "false");
  const baseUrl = `${getAppStoreApiBase()}/api/app-store/historian/range?${params.toString()}`;
  const url = appendCloudEdgeParams(baseUrl, cloudEdge);
  let lastErr = null;
  const attempts = Math.max(1, Number(maxAttempts || 1));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(
        withNoCache(url),
        { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } },
        Math.max(1000, Number(timeoutMs || 15000)) + (attempt - 1) * 2000
      );
      if (!res.ok) throw new Error("App store historian range fetch failed");
      return res.json();
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === attempts) break;
      await sleep(250 * attempt);
    }
  }
  throw lastErr || new Error("App store historian range fetch failed");
}

// Operator 2026-06-17: pre-aggregated historian read. `bucket` is one
// of "minute" | "hour" | "day" and routes the query to the matching
// historian_agg_<bucket> table. Cuts wide-window payloads ~12× vs the
// raw /historian/range endpoint.
export async function getAppStoreHistorianAgg({
  bucket = "minute",
  fromUtc = "",
  toUtc = "",
  gateway = "",
  tag = "",
  source = "",
  limit = 50000,
  cloudEdge = null,
  timeoutMs = 15000,
  maxAttempts = 2,
} = {}) {
  const params = new URLSearchParams();
  params.set("bucket", String(bucket || "minute"));
  if (fromUtc) params.set("from_utc", String(fromUtc));
  if (toUtc) params.set("to_utc", String(toUtc));
  if (gateway) params.set("gateway", String(gateway));
  if (tag) params.set("tag", String(tag));
  if (source) params.set("source", String(source));
  params.set("limit", String(limit));
  const baseUrl = `${getAppStoreApiBase()}/api/app-store/historian/agg?${params.toString()}`;
  const url = appendCloudEdgeParams(baseUrl, cloudEdge);
  let lastErr = null;
  const attempts = Math.max(1, Number(maxAttempts || 1));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(
        withNoCache(url),
        { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } },
        Math.max(1000, Number(timeoutMs || 15000)) + (attempt - 1) * 2000
      );
      if (!res.ok) throw new Error("Historian agg fetch failed");
      return res.json();
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === attempts) break;
      await sleep(250 * attempt);
    }
  }
  throw lastErr || new Error("Historian agg fetch failed");
}

export async function getAppStoreHistorianStats({
  fromUtc = "",
  toUtc = "",
  gateway = "",
  device = "",
  tag = "",
  cloudEdge = null,
  preferCloud = null,
  timeoutMs = 15000,
  maxAttempts = 3,
} = {}) {
  const params = new URLSearchParams();
  if (fromUtc) params.set("from_utc", String(fromUtc));
  if (toUtc) params.set("to_utc", String(toUtc));
  if (gateway) params.set("gateway", String(gateway));
  if (device) params.set("device", String(device));
  if (tag) params.set("tag", String(tag));
  if (typeof preferCloud === "boolean") params.set("prefer_cloud", preferCloud ? "true" : "false");
  const baseUrl = `${getAppStoreApiBase()}/api/app-store/historian/stats?${params.toString()}`;
  const url = appendCloudEdgeParams(baseUrl, cloudEdge);
  let lastErr = null;
  const attempts = Math.max(1, Number(maxAttempts || 1));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(
        withNoCache(url),
        { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } },
        Math.max(1000, Number(timeoutMs || 15000)) + (attempt - 1) * 2000
      );
      if (!res.ok) throw new Error("App store historian stats fetch failed");
      return res.json();
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === attempts) break;
      await sleep(250 * attempt);
    }
  }
  throw lastErr || new Error("App store historian stats fetch failed");
}

export async function getAppStoreHistorianRuleStats({
  rules = [],
  fromUtc = "",
  toUtc = "",
  gateway = "",
  edgeId = "",
  cloudEdge = null,
  preferCloud = null,
  timeoutMs = 15000,
  maxAttempts = 3,
} = {}) {
  const body = {
    rules: Array.isArray(rules) ? rules : [],
    from_utc: fromUtc ? String(fromUtc) : "",
    to_utc: toUtc ? String(toUtc) : "",
    gateway: gateway ? String(gateway) : "",
    edge_id: edgeId ? String(edgeId) : "",
    prefer_cloud: typeof preferCloud === "boolean" ? (preferCloud ? "true" : "false") : "",
  };
  let url = `${getAppStoreApiBase()}/api/app-store/historian/rule-stats`;
  const edgeParams = new URLSearchParams();
  if (cloudEdge && typeof cloudEdge === "object") {
    const edgeId = String(cloudEdge.edgeId || "").trim();
    const source = String(cloudEdge.source || "").trim();
    const site = String(cloudEdge.site || "").trim();
    const area = String(cloudEdge.area || "").trim();
    const equipment = String(cloudEdge.equipment || "").trim();
    if (edgeId) edgeParams.set("edge_id", edgeId);
    if (source) edgeParams.set("source", source);
    if (site) edgeParams.set("site", site);
    if (area) edgeParams.set("area", area);
    if (equipment) edgeParams.set("equipment", equipment);
  }
  if (edgeParams.toString()) {
    url = `${url}?${edgeParams.toString()}`;
  }

  let lastErr = null;
  const attempts = Math.max(1, Number(maxAttempts || 1));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(
        withNoCache(url),
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Cache-Control": "no-store, no-cache, max-age=0",
            Pragma: "no-cache",
          },
          body: JSON.stringify(body),
        },
        Math.max(1000, Number(timeoutMs || 15000)) + (attempt - 1) * 2000
      );
      if (!res.ok) throw new Error("App store historian rule-stats fetch failed");
      return res.json();
    } catch (err) {
      lastErr = err;
      if (!isTransientFetchError(err) || attempt === attempts) break;
      await sleep(250 * attempt);
    }
  }
  throw lastErr || new Error("App store historian rule-stats fetch failed");
}

export async function getAppStoreLive(limit = 5000, cloudEdge = null) {
  const useV1Cloud = getBackendTarget().mode === "cloud";
  if (useV1Cloud) {
    try {
      const v1Url = appendCloudEdgeParams(
        `${getApiBase()}/api/v1/latest?limit=${encodeURIComponent(String(limit))}`,
        cloudEdge
      );
      const resV1 = await fetchWithTimeout(
        withNoCache(v1Url),
        { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
      );
      if (resV1.ok) {
        const data = await resV1.json();
        const rows = [];
        for (const state of Array.isArray(data?.rows) ? data.rows : []) {
          const tags = Array.isArray(state?.tags_json) ? state.tags_json : [];
          for (const t of tags) {
            rows.push({
              ts: state.sample_ts_utc,
              source: state.customer_id || state.tenant_id || "",
              site: state.plant_id || "",
              area: state.gateway_id || "",
              equipment: state.machine_id || "",
              gateway_id: state.gateway_id || "",
              gateway_name: state.gateway_id || "",
              device_name: state.machine_id || "",
              plc_ip: state.plc_endpoint_id || "",
              database_name: "cloud_v1",
              tag: String(t?.tag_name || ""),
              value: t?.value ?? null,
              quality: Number(t?.quality_code ?? state.quality_code ?? 0),
              quality_label: String(t?.quality_label || ""),
              tenant_id: state.tenant_id || "default",
              customer_id: state.customer_id || "",
              plant_id: state.plant_id || "",
              machine_id: state.machine_id || "",
              edge_monotonic_seq: Number(state.edge_monotonic_seq || 0),
              payload_hash_sha256: "",
              sample_age_ms: Number.isFinite(Date.parse(String(state.sample_ts_utc || "")))
                ? Math.max(0, Date.now() - Date.parse(String(state.sample_ts_utc || "")))
                : Number.POSITIVE_INFINITY,
            });
          }
        }
        // If v1 endpoint is healthy but currently empty, fall back to legacy mirror
        // so cloud UI remains populated during migration/cutover.
        if (rows.length > 0) return { ok: true, rows };
      }
    } catch {
      // Fallback to legacy app-store route below.
    }
  }
  const legacyUrl = appendCloudEdgeParams(
    `${getAppStoreApiBase()}/api/app-store/live?limit=${encodeURIComponent(String(limit))}`,
    cloudEdge
  );
  const res = await fetchWithTimeout(
    withNoCache(legacyUrl),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store live fetch failed");
  return res.json();
}

export async function getAppStoreLogs(limit = 2000, cloudEdge = null) {
  const url = appendCloudEdgeParams(
    `${getAppStoreApiBase()}/api/app-store/logs?limit=${encodeURIComponent(String(limit))}`,
    cloudEdge
  );
  const res = await fetchWithTimeout(
    withNoCache(url),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store logs fetch failed");
  return res.json();
}

export async function getAppStoreInspector(previewLimit = 15) {
  const res = await fetchWithTimeout(
    withNoCache(`${getAppStoreApiBase()}/api/app-store/inspector?preview_limit=${encodeURIComponent(String(previewLimit))}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store inspector fetch failed");
  return res.json();
}

export async function getCloudSyncStatus() {
  // Fast read-only summary of the edge's cloud-sync workers — backlog
  // depth, last sync time, last error, telemetry-v1 state. Used by the
  // header strip + the backlog-too-big popup to decide whether to prompt.
  const res = await fetchWithTimeout(
    withNoCache(`${getAppStoreApiBase()}/api/app-store/sync/status`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } },
    8000
  );
  if (!res.ok) throw new Error("Sync status fetch failed");
  return res.json();
}

export async function getEdgeIngestDiagnostics() {
  const res = await fetchWithTimeout(
    withNoCache(`${getApiBase()}/api/v1/edge/diagnostics`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } },
    20000
  );
  if (!res.ok) throw new Error("Edge diagnostics fetch failed");
  return res.json();
}

export async function checkDatabaseRecovery(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/database/recovery/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Database recovery check failed");
  return res.json();
}

export async function repairDatabaseRecovery(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/database/recovery/repair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Database recovery repair failed");
  return res.json();
}

export async function getRetentionPolicy() {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/retention/policy`);
  if (!res.ok) throw new Error("Retention policy fetch failed");
  return res.json();
}

export async function updateRetentionPolicy(payload) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/retention/policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Retention policy update failed");
  return res.json();
}

export async function runRetention(payload) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/retention/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Retention run failed");
  return res.json();
}

export async function getRetentionRuns(limit = 20) {
  const res = await fetchWithTimeout(
    `${getAppStoreApiBase()}/api/app-store/retention/runs?limit=${encodeURIComponent(String(limit))}`
  );
  if (!res.ok) throw new Error("Retention runs fetch failed");
  return res.json();
}

/* ---------------------------------------------------------------------------
   Retention / storage / backups v2 (operator 2026-08-21).
   Tiered retention engine — see docs/historian-retention-and-forwarding-
   architecture-2026-08-21.md. All mutating calls are admin-only on the server
   and return 403 for anyone else, so the UI must gate its controls too.
   --------------------------------------------------------------------------- */
async function _retentionCall(path, { method = "GET", body } = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store${path}`, {
    method,
    ...(body !== undefined
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {}),
  });
  if (!res.ok) {
    // The server sends an operator-readable sentence in `detail` for both the
    // 422 validation errors and the 403 admin gate — surface it verbatim.
    let detail = "";
    try {
      const data = await res.json();
      detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail || "");
    } catch (_) { /* non-JSON body */ }
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json();
}

export async function getRetentionStatus() {
  return _retentionCall("/retention/v2/status");
}
export async function getRetentionOptions() {
  return _retentionCall("/retention/v2/options");
}
export async function listRetentionPolicies() {
  return _retentionCall("/retention/v2/policies");
}
export async function saveRetentionPolicyV2(policy) {
  return _retentionCall("/retention/v2/policies", { method: "PUT", body: policy });
}
export async function activateRetentionPolicy(policyId) {
  return _retentionCall(`/retention/v2/policies/${encodeURIComponent(policyId)}/activate`, { method: "POST" });
}
export async function deactivateRetentionPolicy() {
  return _retentionCall("/retention/v2/deactivate", { method: "POST" });
}
export async function deleteRetentionPolicy(policyId) {
  return _retentionCall(`/retention/v2/policies/${encodeURIComponent(policyId)}`, { method: "DELETE" });
}
export async function estimateRetentionPolicy(policy) {
  return _retentionCall("/retention/v2/estimate", { method: "POST", body: policy });
}
export async function runRetentionV2(dryRun = true, force = false) {
  return _retentionCall("/retention/v2/run", { method: "POST", body: { dry_run: dryRun, force } });
}
export async function getRetentionRunsV2(limit = 25) {
  return _retentionCall(`/retention/v2/runs?limit=${encodeURIComponent(String(limit))}`);
}
export async function compactDatabase() {
  return _retentionCall("/retention/v2/compact", { method: "POST" });
}
export async function cancelDatabaseCompaction() {
  return _retentionCall("/retention/v2/compact/cancel", { method: "POST" });
}
export async function listBackupsV2(limit = 200) {
  return _retentionCall(`/backups/v2?limit=${encodeURIComponent(String(limit))}`);
}
export async function createBackupV2(kind = "config", label = "") {
  return _retentionCall("/backups/v2/create", { method: "POST", body: { kind, label } });
}
export async function restoreBackupV2(filename) {
  return _retentionCall("/backups/v2/restore", { method: "POST", body: { filename } });
}
export async function cancelBackupRestore() {
  return _retentionCall("/backups/v2/restore/cancel", { method: "POST" });
}
export async function deleteBackupV2(filename) {
  return _retentionCall(`/backups/v2/${encodeURIComponent(filename)}`, { method: "DELETE" });
}
// Absolute URL so the download works from the desktop app (file:// origin).
export function backupDownloadUrl(filename) {
  return `${getAppStoreApiBase()}/api/app-store/backups/v2/${encodeURIComponent(filename)}/download`;
}

export async function getAppStoreBackups(limit = 200) {
  const res = await fetchWithTimeout(
    `${getAppStoreApiBase()}/api/app-store/backups?limit=${encodeURIComponent(String(limit))}`
  );
  if (!res.ok) throw new Error("Backups fetch failed");
  return res.json();
}

export async function createAppStoreBackup(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/backups/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Create backup failed");
  return res.json();
}

export async function restoreAppStoreBackup(payload) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/backups/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Restore backup failed");
  return res.json();
}

export async function deleteAppStoreBackup(filename) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/backups/${encodeURIComponent(String(filename || ""))}`, {
    method: "DELETE"
  });
  if (!res.ok) throw new Error("Delete backup failed");
  return res.json();
}

export async function cleanupAppStoreData(payload) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/cleanup-data`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Data cleanup failed");
  return res.json();
}

export async function forceAppStoreSyncNow(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/force`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Force sync failed");
  return res.json();
}

export async function repairAppStoreScopeNow(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/repair_scope`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Repair scope failed");
  return res.json();
}

export async function manualPeriodSyncAppStore(payload) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/manual-period`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 30000);
  if (!res.ok) throw new Error("Manual period sync failed");
  return res.json();
}

export async function clearAppStoreSyncQueue(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/queue/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Clear sync queue failed");
  return res.json();
}

export async function dropAppStoreSyncBacklog(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/backlog/drop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Drop sync backlog failed");
  return res.json();
}

export async function clearEdgeIngestQueue(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/sync/edge-ingest/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Clear edge ingest queue failed");
  return res.json();
}

export async function resetAppStoreFull(payload = {}) {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/reset/full`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 120000);
  if (!res.ok) throw new Error("Full reset failed");
  return res.json();
}

export async function getPowerConfig() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/config`);
  if (!res.ok) throw new Error("Power config fetch failed");
  return res.json();
}

export async function getPowerProfiles() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/profiles`);
  if (!res.ok) throw new Error("Power profiles fetch failed");
  return res.json();
}

export async function updatePowerConfig(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    // Surface the backend reason so operators can see WHICH field rejected
    // (Pydantic / power_manager normalization errors land here). Without this
    // detail the UI just shows "Power config update failed" and the user
    // has no idea what to fix.
    let detail = "";
    try {
      const body = await res.clone().text();
      try {
        const j = JSON.parse(body);
        detail = String(j?.detail || j?.message || body || "");
      } catch {
        detail = body;
      }
    } catch {
      // body unreadable — fall through with generic message
    }
    const msg = `Power config update failed (HTTP ${res.status})${detail ? `: ${detail.slice(0, 400)}` : ""}`;
    throw new Error(msg);
  }
  return res.json();
}

export async function testPowerConnection(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/test-connection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 15000);
  if (!res.ok) throw new Error("Power meter connection test failed");
  return res.json();
}

export async function getPowerStatus() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/status`);
  if (!res.ok) throw new Error("Power status fetch failed");
  return res.json();
}

export async function getPowerLatest(deviceId = "") {
  const params = new URLSearchParams();
  if (deviceId) params.set("device_id", String(deviceId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/latest${suffix}`);
  if (!res.ok) throw new Error("Power latest fetch failed");
  return res.json();
}

export async function getPowerHistory(limit = 300, deviceId = "") {
  const params = new URLSearchParams({ limit: String(limit) });
  if (deviceId) params.set("device_id", String(deviceId));
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/history?${params.toString()}`);
  if (!res.ok) throw new Error("Power history fetch failed");
  return res.json();
}

export async function startPowerDevice(deviceId) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/devices/${encodeURIComponent(String(deviceId || ""))}/start`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Power device start failed");
  return res.json();
}

export async function stopPowerDevice(deviceId) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/power/devices/${encodeURIComponent(String(deviceId || ""))}/stop`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Power device stop failed");
  return res.json();
}

async function parseApiErrorDetail(res) {
  try {
    const body = await res.json();
    const parsed = String(body?.detail || body?.error || "").trim();
    if (parsed) return parsed;
  } catch {
    // ignore and fallback to text body
  }
  try {
    const txt = String(await res.text());
    return txt.trim();
  } catch {
    return "";
  }
}

async function ensureOk(res, message) {
  if (res.ok) return;
  const detail = await parseApiErrorDetail(res);
  throw new Error(`${message} (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
}

export async function getControlPlaneRuntimeContext() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/runtime-context`);
  await ensureOk(res, "Control-plane runtime-context fetch failed");
  return res.json();
}

export async function getControlPlaneEdgeBootstrapStatus() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-bootstrap-status`);
  await ensureOk(res, "Control-plane edge bootstrap status fetch failed");
  return res.json();
}

export async function getControlPlanePortalContext() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/portal-context`);
  await ensureOk(res, "Control-plane portal-context fetch failed");
  return res.json();
}

export async function getControlPlaneModuleCatalog() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/modules`);
  await ensureOk(res, "Control-plane modules fetch failed");
  return res.json();
}

export async function getControlPlaneSummary(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/summary${suffix}`);
  await ensureOk(res, "Control-plane summary fetch failed");
  return res.json();
}

export async function getControlPlaneTenants(includeSuspended = true) {
  const params = new URLSearchParams();
  params.set("include_suspended", includeSuspended ? "true" : "false");
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/tenants?${params.toString()}`);
  await ensureOk(res, "Control-plane tenants fetch failed");
  return res.json();
}

export async function upsertControlPlaneTenant(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/tenants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane tenant upsert failed");
  return res.json();
}

export async function getControlPlaneCustomers(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/customers${suffix}`);
  await ensureOk(res, "Control-plane customers fetch failed");
  return res.json();
}

export async function upsertControlPlaneCustomer(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/customers${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane customer upsert failed");
  return res.json();
}

export async function deleteControlPlaneCustomer(customerId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const cid = encodeURIComponent(String(customerId || ""));
  const deleteUrl = `${getApiBase()}/api/control-plane/customers/${cid}${suffix}`;
  let res = null;
  let needPostFallback = false;
  try {
    res = await fetchWithTimeout(deleteUrl, { method: "DELETE" });
    needPostFallback = res.status === 404 || res.status === 405;
  } catch {
    // Some reverse proxies/browser CORS paths block DELETE outright.
    needPostFallback = true;
  }
  if (needPostFallback) {
    const postFallbackUrl = `${getApiBase()}/api/control-plane/customers/${cid}/delete${suffix}`;
    res = await fetchWithTimeout(postFallbackUrl, { method: "POST" });
  }
  await ensureOk(res, "Control-plane customer delete failed");
  return res.json();
}

export async function listControlPlaneDashboardProfiles(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/dashboard-profiles${suffix}`);
  await ensureOk(res, "Dashboard profiles fetch failed");
  return res.json();
}

export async function deleteControlPlaneDashboardProfile(scopeKey, tenantId = "") {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/dashboard-profiles/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_key: String(scopeKey || ""), tenant_id: String(tenantId || "") }),
  });
  await ensureOk(res, "Dashboard profile delete failed");
  return res.json();
}

export async function getControlPlaneEdges(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges${suffix}`);
  await ensureOk(res, "Control-plane edges fetch failed");
  return res.json();
}

export async function upsertControlPlaneEdge(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane edge upsert failed");
  return res.json();
}

export async function deleteControlPlaneEdge(edgeId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const deleteUrl = `${getApiBase()}/api/control-plane/edges/${eid}${suffix}`;
  let res = null;
  let needPostFallback = false;
  try {
    res = await fetchWithTimeout(deleteUrl, { method: "DELETE" });
    needPostFallback = res.status === 404 || res.status === 405;
  } catch {
    needPostFallback = true;
  }
  if (needPostFallback) {
    const postFallbackUrl = `${getApiBase()}/api/control-plane/edges/${eid}/delete${suffix}`;
    res = await fetchWithTimeout(postFallbackUrl, { method: "POST" });
  }
  await ensureOk(res, "Control-plane edge delete failed");
  return res.json();
}

// --- Read-only Client View share links ---
// A view link grants no-login read-only Lite access to a single edge.
// The portal "Client View" column drives create/rotate/revoke.

function _viewLinkUrl(token) {
  // Resolves to the Lite app's read-only viewer route. The link is opened
  // by anonymous browsers so we anchor at the host the portal was loaded
  // from (which is also where Lite is served from).
  const host = String(window.location.origin || "").replace(/\/+$/, "");
  return `${host}/lite/view/${encodeURIComponent(token)}`;
}

export async function getEdgeViewLink(edgeId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/view-link${suffix}`);
  await ensureOk(res, "Fetching edge view link failed");
  return res.json();
}

export async function createEdgeViewLink(edgeId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/view-link${suffix}`, {
    method: "POST",
  });
  await ensureOk(res, "Creating edge view link failed");
  return res.json();
}

export async function rotateEdgeViewLink(edgeId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/view-link/rotate${suffix}`, {
    method: "POST",
  });
  await ensureOk(res, "Rotating edge view link failed");
  return res.json();
}

export async function revokeEdgeViewLink(edgeId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/view-link${suffix}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Revoking edge view link failed");
  return res.json();
}

export function buildEdgeViewLinkUrl(token) {
  return token ? _viewLinkUrl(token) : "";
}

// --- Per-user Lite view-links (operator 2026-06-17) ---
// Admin-only. Each row in the Users page can mint/copy/rotate its own
// Lite token. NULL user_id = legacy edge-wide link (still shown on the
// Edges page); non-NULL = per-user link. Same backend table.

export async function getEdgeUserViewLink(edgeId, userId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const uid = encodeURIComponent(String(userId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/users/${uid}/view-link${suffix}`);
  await ensureOk(res, "Fetching user view link failed");
  return res.json();
}

export async function createEdgeUserViewLink(edgeId, userId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const uid = encodeURIComponent(String(userId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/users/${uid}/view-link${suffix}`, {
    method: "POST",
  });
  await ensureOk(res, "Creating user view link failed");
  return res.json();
}

export async function rotateEdgeUserViewLink(edgeId, userId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const uid = encodeURIComponent(String(userId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/users/${uid}/view-link/rotate${suffix}`, {
    method: "POST",
  });
  await ensureOk(res, "Rotating user view link failed");
  return res.json();
}

export async function revokeEdgeUserViewLink(edgeId, userId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const eid = encodeURIComponent(String(edgeId || ""));
  const uid = encodeURIComponent(String(userId || ""));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/${eid}/users/${uid}/view-link${suffix}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Revoking user view link failed");
  return res.json();
}

// LOCAL-LITE URL: the per-user token is meant for the LAN /lite/?token=...
// landing page (not the cloud /lite/view/<token> deep link). We fetch the
// LAN port from /api/lan-sharing/status and build URLs for each IP; if the
// LAN socket isn't running, we fall back to the loopback URL the admin can
// share by pasting the token straight into the Lite page.
export async function getLiteLocalShareTargets() {
  try {
    const res = await fetchWithTimeout(`${getApiBase()}/api/lan-sharing/status`);
    if (!res.ok) return { urls: [], port: 0, running: false };
    const body = await res.json();
    return {
      urls: Array.isArray(body?.lite_urls) ? body.lite_urls : [],
      port: Number(body?.lan_port || body?.port || 0),
      running: !!body?.running,
    };
  } catch (_) {
    return { urls: [], port: 0, running: false };
  }
}

export function buildLiteLocalUrl(baseUrl, token) {
  if (!token) return "";
  const base = String(baseUrl || "").replace(/\/+$/, "");
  if (!base) return "";
  return `${base}?token=${encodeURIComponent(token)}`;
}

// Operator 2026-06-18: build a LAN URL for a specific UI variant.
// baseUrl is something like "http://10.7.0.1:8088/trustnode/lite/".
// variant is "full" | "lite" | "client". The base URL's last segment
// is swapped to match the variant. If the base doesn't have a known
// trustnode/<variant>/ tail, returns as-is + ?token=.
export function buildLanUrlForVariant(baseUrl, token, variant) {
  if (!token || !baseUrl) return "";
  const known = ["full", "lite", "client"];
  const v = known.includes(String(variant)) ? variant : "lite";
  let base = String(baseUrl).replace(/\/+$/, "");
  // Replace the trailing /trustnode/<known>/ with /trustnode/<v>/.
  base = base.replace(/\/trustnode\/(full|lite|client)$/, `/trustnode/${v}`);
  // If the base didn't have /trustnode/<variant>, append it.
  if (!/\/trustnode\/(full|lite|client)$/.test(base)) {
    base = `${base}/trustnode/${v}`;
  }
  return `${base}/?token=${encodeURIComponent(token)}`;
}

// Pick the user's preferred LAN variant given their permission flags.
// Preference: full → lite → client. Returns null if user has no access.
export function pickLanVariantForUser(permissions) {
  const p = permissions || {};
  if (p.access_full) return "full";
  if (p.access_lite) return "lite";
  if (p.access_client) return "client";
  return null;
}

// --- Remote Access (LAN sharing) — plan 2026-08-21 §3.5 ---
// Every call goes through fetchWithTimeout + getControlApiBase() so the
// Bearer token is attached and the request targets the edge that serves
// this bundle. The previous page used bare relative fetch("/api/lan-sharing/…")
// which the auth middleware only waives for loopback → 401 from any LAN PC.
// Fields beyond the legacy status shape (view_urls, hostname_urls, https,
// licensed, sessions, http_enabled) may be absent on older backends; callers
// must treat them as optional.

export async function getLanSharingStatus() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/lan-sharing/status`, {}, 8000);
  await ensureOk(res, "Fetching Remote Access status failed");
  return res.json();
}

export async function enableLanSharing() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/lan-sharing/enable`, { method: "POST" }, 20000);
  await ensureOk(res, "Turning Remote Access on failed");
  return res.json().catch(() => ({}));
}

export async function disableLanSharing() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/lan-sharing/disable`, { method: "POST" }, 20000);
  await ensureOk(res, "Turning Remote Access off failed");
  return res.json().catch(() => ({}));
}

// payload: { https_only?: bool, http_enabled?: bool, bind_host?: string }
export async function updateLanSharingConfig(payload) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/lan-sharing/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  }, 20000);
  await ensureOk(res, "Saving Remote Access settings failed");
  return res.json().catch(() => ({}));
}

export async function revokeLanSharingSession(username) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/lan-sharing/sessions/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: String(username || "") }),
  });
  await ensureOk(res, "Revoking remote session failed");
  return res.json().catch(() => ({}));
}

// Absolute URL of the self-signed certificate (PEM download, public route).
export function getLanSharingCertificateUrl(certUrl = "/api/lan-sharing/certificate") {
  const path = String(certUrl || "/api/lan-sharing/certificate");
  if (/^https?:\/\//i.test(path)) return path;
  return `${getControlApiBase()}${path.startsWith("/") ? "" : "/"}${path}`;
}

// --- Outbound connections (OPC UA / MQTT, operator 2026-06-17) ---

export async function getOpcuaStatus() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/opcua/status`);
  await ensureOk(res, "Fetching OPC UA status failed");
  return res.json();
}

export async function setOpcuaEnabled(config) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/opcua/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config || {}),
  });
  await ensureOk(res, "Enabling OPC UA server failed");
  return res.json();
}

export async function setOpcuaDisabled() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/opcua/disable`, {
    method: "POST",
  });
  await ensureOk(res, "Disabling OPC UA server failed");
  return res.json();
}

export async function getMqttStatus() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/mqtt/status`);
  await ensureOk(res, "Fetching MQTT status failed");
  return res.json();
}

export async function setMqttEnabled(config) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/mqtt/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config || {}),
  });
  await ensureOk(res, "Enabling MQTT broker failed");
  return res.json();
}

export async function setMqttDisabled() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/connections/mqtt/disable`, {
    method: "POST",
  });
  await ensureOk(res, "Disabling MQTT broker failed");
  return res.json();
}

// --- Directories (operator 2026-06-18) ---

export async function getDirectories() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/directories`);
  await ensureOk(res, "Fetching directories failed");
  return res.json();
}

export async function setDirectories(overrides) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/directories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides: overrides || {} }),
  });
  await ensureOk(res, "Saving directories failed");
  return res.json();
}

export async function resetDirectory(key) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/directories/reset/${encodeURIComponent(key)}`, {
    method: "POST",
  });
  await ensureOk(res, "Resetting directory failed");
  return res.json();
}

export async function openDirectory(key) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/directories/open/${encodeURIComponent(key)}`, {
    method: "POST",
  });
  await ensureOk(res, "Opening directory failed");
  return res.json();
}

export async function heartbeatControlPlaneEdge(edgeId, payload = {}, tenantId = "") {
  const params = new URLSearchParams();
  params.set("edge_id", String(edgeId || ""));
  if (tenantId) params.set("tenant_id", String(tenantId));
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edges/heartbeat?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane edge heartbeat failed");
  return res.json();
}

export async function getControlPlaneLicenses(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/licenses${suffix}`);
  await ensureOk(res, "Control-plane licenses fetch failed");
  return res.json();
}

// Infrastructure Endpoints (developer-admin) — the single source of truth for
// where the deployment's services live. Values flow into activation codes so
// edges self-configure without any hardcoded URL. 2026-07-15.
export async function getInfrastructureEndpoints(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/infrastructure-endpoints${suffix}`);
  await ensureOk(res, "Infrastructure endpoints fetch failed");
  return res.json();
}

export async function saveInfrastructureEndpoints(endpoints, tenantId = "") {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/infrastructure-endpoints`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoints: endpoints || {}, tenant_id: tenantId || "" }),
  });
  await ensureOk(res, "Infrastructure endpoints save failed");
  return res.json();
}

export async function upsertControlPlaneLicense(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/licenses${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane license upsert failed");
  return res.json();
}

export async function deleteControlPlaneLicense(licenseId, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const lid = encodeURIComponent(String(licenseId || ""));
  const deleteUrl = `${getApiBase()}/api/control-plane/licenses/${lid}${suffix}`;
  let res = null;
  let needPostFallback = false;
  try {
    res = await fetchWithTimeout(deleteUrl, { method: "DELETE" });
    needPostFallback = res.status === 404 || res.status === 405;
  } catch {
    needPostFallback = true;
  }
  if (needPostFallback) {
    const postFallbackUrl = `${getApiBase()}/api/control-plane/licenses/${lid}/delete${suffix}`;
    res = await fetchWithTimeout(postFallbackUrl, { method: "POST" });
  }
  await ensureOk(res, "Control-plane license delete failed");
  return res.json();
}

export async function getControlPlaneLicenseModules(licenseId) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/licenses/${encodeURIComponent(String(licenseId || ""))}/modules`);
  await ensureOk(res, "Control-plane license modules fetch failed");
  return res.json();
}

export async function setControlPlaneLicenseModules(licenseId, payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/licenses/${encodeURIComponent(String(licenseId || ""))}/modules`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || { modules: [] })
  });
  await ensureOk(res, "Control-plane license modules update failed");
  return res.json();
}

// AI Endpoint config (TrustNode Intelligence). Read by every authed
// portal user (so the card can render its current values); write is
// gated to global admin on the backend.
export async function getAIEndpointConfig() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/ai-endpoint`);
  await ensureOk(res, "AI endpoint config fetch failed");
  return res.json();
}

export async function setAIEndpointConfig(cfg) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/ai-endpoint`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg || {}),
  });
  await ensureOk(res, "AI endpoint config save failed");
  return res.json();
}

export async function getControlPlaneUsers(tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/users${suffix}`);
  await ensureOk(res, "Control-plane users fetch failed");
  return res.json();
}

export async function upsertControlPlaneUser(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/users${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane user upsert failed");
  return res.json();
}

export async function setControlPlaneUserPassword(username, password, mustChange = false, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const uname = encodeURIComponent(String(username || ""));
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/control-plane/users/${uname}/password${suffix}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: String(password || ""), must_change: !!mustChange }),
    },
  );
  await ensureOk(res, "Set password failed");
  return res.json();
}

export async function generateControlPlaneUserTempPassword(username, length = 14, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const uname = encodeURIComponent(String(username || ""));
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/control-plane/users/${uname}/password/temp${suffix}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ length: Number(length || 14) }),
    },
  );
  await ensureOk(res, "Temp password generation failed");
  return res.json();
}

export async function changeOwnPassword(currentPassword, newPassword) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/auth/change-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: String(currentPassword || ""), new_password: String(newPassword || "") }),
  });
  await ensureOk(res, "Change password failed");
  return res.json();
}

export async function deleteControlPlaneUser(username, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const uname = encodeURIComponent(String(username || ""));
  const deleteUrl = `${getApiBase()}/api/control-plane/users/${uname}${suffix}`;
  let res = null;
  let needPostFallback = false;
  try {
    res = await fetchWithTimeout(deleteUrl, { method: "DELETE" });
    needPostFallback = res.status === 404 || res.status === 405;
  } catch {
    needPostFallback = true;
  }
  if (needPostFallback) {
    const postFallbackUrl = `${getApiBase()}/api/control-plane/users/${uname}/delete${suffix}`;
    res = await fetchWithTimeout(postFallbackUrl, { method: "POST" });
  }
  await ensureOk(res, "Control-plane user delete failed");
  return res.json();
}

// Licence seat ledger (plan 2026-08-22, Phase 1).
// Returns null when the backend does not yet expose the endpoint (404) so the
// UI degrades gracefully on older builds.
export async function getLicenseSeats() {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/control-plane/license/seats`, {}, 8000);
  if (res.status === 404) return null;
  await ensureOk(res, "License seats fetch failed");
  return res.json();
}

// Send (or render without sending) the per-user access invitation e-mail.
// body: { username, temp_password, send }
// Returns the rendered text and metadata even when SMTP is not configured.
// Non-ok responses throw with the backend detail verbatim.
export async function sendUserAccessEmail({ username, tempPassword = "", send = true } = {}) {
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/control-plane/users/access-email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: String(username || ""),
      temp_password: String(tempPassword || ""),
      send: Boolean(send),
    }),
  }, 15000);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = String(body?.detail || body?.error || "").trim();
    } catch {
      try { detail = (await res.text()).trim(); } catch { detail = ""; }
    }
    throw new Error(detail || `Send access email failed (HTTP ${res.status})`);
  }
  return res.json();
}

export async function issueControlPlaneActivationCode(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/activation-code/issue${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane activation code issue failed");
  return res.json();
}

export async function applyControlPlaneActivationCode(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/activation-code/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane activation code apply failed");
  return res.json();
}

export async function bootstrapControlPlaneEdgeLink(payload) {
  const req = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  };
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-link/bootstrap`, req, 20000);
  await ensureOk(res, "Control-plane edge-link bootstrap failed");
  return res.json();
}

export async function getControlPlaneActivationCodes(tenantId = "", customerId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  if (customerId) params.set("customer_id", String(customerId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/activation-codes${suffix}`);
  // Older deployed backends may not expose activation-codes yet.
  if (res.status === 404) return { ok: true, rows: [] };
  await ensureOk(res, "Control-plane activation-codes fetch failed");
  return res.json();
}

export async function updateControlPlaneActivationCode(rowId, payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/activation-codes/${encodeURIComponent(String(rowId || ""))}${suffix}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Control-plane activation-code update failed");
  return res.json();
}

export async function deleteControlPlaneActivationCode(rowId, tenantId = "") {
  const rid = encodeURIComponent(String(rowId || ""));
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const deleteUrl = `${getApiBase()}/api/control-plane/activation-codes/${rid}${suffix}`;
  let res;
  try {
    res = await fetchWithTimeout(deleteUrl, { method: "DELETE" });
  } catch {
    res = null;
  }
  if (!res || res.status === 404 || res.status === 405 || res.status === 501) {
    const postFallbackUrl = `${getApiBase()}/api/control-plane/activation-codes/${rid}/delete${suffix}`;
    res = await fetchWithTimeout(postFallbackUrl, { method: "POST" });
  }
  await ensureOk(res, "Control-plane activation-code delete failed");
  return res.json();
}

export async function registerControlPlaneEdgeLink(payload) {
  const req = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  };
  const primaryBase = normalizeBaseUrl(getApiBase());
  const candidates = [];
  const storedCloud = normalizeBaseUrl(localStorage.getItem(STORAGE_CLOUD_URL_KEY) || "");
  const pushUnique = (base) => {
    const normalized = normalizeBaseUrl(base || "");
    if (!normalized) return;
    if (!candidates.includes(normalized)) candidates.push(normalized);
  };

  const hostedRuntime = isHostedWebClientRuntime();
  if (hostedRuntime) {
    // Hosted runtime should prefer same-origin API first.
    pushUnique(primaryBase);
    if (storedCloud && storedCloud !== primaryBase) pushUnique(storedCloud);
    if (CONTROL_PLANE_FALLBACK_URL && CONTROL_PLANE_FALLBACK_URL !== primaryBase) {
      pushUnique(CONTROL_PLANE_FALLBACK_URL);
    }
  } else {
    // Desktop/local runtime: activation codes are issued in cloud control-plane.
    // Prefer cloud first, then local as fallback.
    if (storedCloud) pushUnique(storedCloud);
    if (CONTROL_PLANE_FALLBACK_URL) pushUnique(CONTROL_PLANE_FALLBACK_URL);
    pushUnique(primaryBase);
  }

  let lastErr = null;
  const tryLocalFinalize = async (sourceBase, dataLike) => {
    const row = (dataLike && typeof dataLike === "object" && dataLike.row && typeof dataLike.row === "object")
      ? dataLike.row
      : (dataLike && typeof dataLike === "object" ? dataLike : {});
    const lic = (row && typeof row.license === "object" && row.license) ? row.license : {};
    const finalizeRes = await fetchWithTimeout(`${primaryBase}/api/control-plane/edge-link/local-finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: String(dataLike?.tenant_id || row?.tenant_id || ""),
        edge_id: String(dataLike?.edge_id || row?.edge_id || payload?.edge_id || ""),
        edge_name: String(payload?.edge_name || dataLike?.edge_name || row?.edge_name || dataLike?.edge_id || row?.edge_id || ""),
        customer_id: String(dataLike?.customer_id || row?.customer_id || ""),
        license_id: String(dataLike?.license_id || row?.license_id || lic?.license_id || ""),
        license_status: String(lic?.status || "active"),
        license_plan_code: String(lic?.plan_code || "standard"),
        license_start_utc: String(lic?.start_utc || ""),
        license_end_utc: String(lic?.end_utc || ""),
        license_max_edges: Number(lic?.max_edges || 0),
        license_max_users: Number(lic?.max_users || 0),
        license_modules: Array.isArray(lic?.modules) ? lic.modules : [],
        cloud_api_url: String(dataLike?.cloud_api_url || row?.cloud_api_url || sourceBase || ""),
        primary_domain: String(dataLike?.primary_domain || row?.primary_domain || ""),
        admin_username: String(payload?.admin_username || "admin"),
        admin_password: String(payload?.admin_password || ""),
      }),
    }, 20000);
    await ensureOk(finalizeRes, "Control-plane edge-link local finalize failed");
  };

  const cloudRecoveryBases = Array.from(
    new Set(
      [storedCloud, CONTROL_PLANE_FALLBACK_URL]
        .map((v) => normalizeBaseUrl(v || ""))
        .filter((v) => v && v !== primaryBase)
    )
  );
  const tryUsedCodeRecovery = async () => {
    for (const cbase of cloudRecoveryBases) {
      try {
        const bootstrapRes = await fetchWithTimeout(`${cbase}/api/control-plane/edge-link/bootstrap`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            activation_code: String(payload?.activation_code || "").trim(),
            edge_id: String(payload?.edge_id || "").trim(),
            edge_name: String(payload?.edge_name || "").trim(),
            site: String(payload?.site || "").trim(),
            area: String(payload?.area || "").trim(),
            equipment: String(payload?.equipment || "").trim(),
          }),
        }, 20000);
        if (!bootstrapRes.ok) continue;
        const bootstrapData = await bootstrapRes.json().catch(() => ({}));
        await tryLocalFinalize(cbase, bootstrapData);
        return {
          ok: true,
          ...bootstrapData,
          edge_id: String(bootstrapData?.edge_id || bootstrapData?.row?.edge_id || payload?.edge_id || ""),
          customer_id: String(bootstrapData?.customer_id || bootstrapData?.row?.customer_id || ""),
          license_id: String(bootstrapData?.license_id || bootstrapData?.row?.license_id || bootstrapData?.row?.license?.license_id || ""),
          recovered_from_used_code: true,
        };
      } catch {
        // continue next cloud base
      }
    }
    return null;
  };

  for (const base of candidates) {
    try {
      const res = await fetchWithTimeout(`${base}/api/control-plane/edge-link/register`, req, 20000);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = String(data?.detail || data?.error || "").toLowerCase();
        const isActivationCodeUsed = detail.includes("activation_code_used");
        // Login-page recovery flow:
        // If code already used in cloud, bootstrap scope by code and finalize locally
        // so admin user creation still succeeds for this workstation.
        if (isActivationCodeUsed) {
          const recovered = await tryUsedCodeRecovery();
          if (recovered) return recovered;
        }
        await ensureOk(res, "Control-plane edge-link register failed");
      }
      // Cloud fallback succeeded: finalize local bootstrap/auth so desktop login works immediately.
      if (primaryBase && primaryBase !== base) {
        await tryLocalFinalize(base, data);
      }
      return data;
    } catch (err) {
      lastErr = err;
      // Try next candidate.
    }
  }
  throw lastErr || new Error("Control-plane edge-link register failed");
}

// Login-page activation flow (local edge app):
// - Cloud-first register to consume fresh activation codes.
// - If code is already used, recover via cloud bootstrap and local finalize
//   so the local admin user can still be created for this workstation.
export async function registerControlPlaneEdgeLinkLogin(payload) {
  const req = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  };
  const primaryBase = normalizeBaseUrl(getApiBase());
  const storedCloud = normalizeBaseUrl(localStorage.getItem(STORAGE_CLOUD_URL_KEY) || "");
  const cloudBases = Array.from(
    new Set(
      [storedCloud, CONTROL_PLANE_FALLBACK_URL]
        .map((v) => normalizeBaseUrl(v || ""))
        .filter(Boolean)
    )
  );
  const cloudFirst = cloudBases[0] || CONTROL_PLANE_FALLBACK_URL;

  const tryLocalFinalize = async (sourceBase, dataLike) => {
    const row = (dataLike && typeof dataLike === "object" && dataLike.row && typeof dataLike.row === "object")
      ? dataLike.row
      : (dataLike && typeof dataLike === "object" ? dataLike : {});
    const lic = (row && typeof row.license === "object" && row.license) ? row.license : {};
    const finalizeRes = await fetchWithTimeout(`${primaryBase}/api/control-plane/edge-link/local-finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: String(dataLike?.tenant_id || row?.tenant_id || ""),
        edge_id: String(dataLike?.edge_id || row?.edge_id || payload?.edge_id || ""),
        edge_name: String(payload?.edge_name || dataLike?.edge_name || row?.edge_name || dataLike?.edge_id || row?.edge_id || ""),
        customer_id: String(dataLike?.customer_id || row?.customer_id || ""),
        license_id: String(dataLike?.license_id || row?.license_id || lic?.license_id || ""),
        license_status: String(lic?.status || "active"),
        license_plan_code: String(lic?.plan_code || "standard"),
        license_start_utc: String(lic?.start_utc || ""),
        license_end_utc: String(lic?.end_utc || ""),
        license_max_edges: Number(lic?.max_edges || 0),
        license_max_users: Number(lic?.max_users || 0),
        license_modules: Array.isArray(lic?.modules) ? lic.modules : [],
        cloud_api_url: String(dataLike?.cloud_api_url || row?.cloud_api_url || sourceBase || ""),
        primary_domain: String(dataLike?.primary_domain || row?.primary_domain || ""),
        admin_username: String(payload?.admin_username || "admin"),
        admin_password: String(payload?.admin_password || ""),
      }),
    }, 20000);
    await ensureOk(finalizeRes, "Control-plane edge-link local finalize failed");
  };

  const ensureActivationScope = async (base, dataLike) => {
    const row = (dataLike && typeof dataLike === "object" && dataLike.row && typeof dataLike.row === "object")
      ? dataLike.row
      : (dataLike && typeof dataLike === "object" ? dataLike : {});
    const lic = (row && typeof row.license === "object" && row.license) ? row.license : {};
    const edgeId = String(dataLike?.edge_id || row?.edge_id || "").trim();
    const customerId = String(dataLike?.customer_id || row?.customer_id || "").trim();
    const licenseId = String(dataLike?.license_id || row?.license_id || lic?.license_id || "").trim();
    if (edgeId && customerId && licenseId) return dataLike;

    const bootstrapRes = await fetchWithTimeout(`${base}/api/control-plane/edge-link/bootstrap`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        activation_code: String(payload?.activation_code || "").trim(),
        edge_id: edgeId,
        edge_name: String(payload?.edge_name || "").trim(),
        site: String(payload?.site || "").trim(),
        area: String(payload?.area || "").trim(),
        equipment: String(payload?.equipment || "").trim(),
      }),
    }, 20000);
    await ensureOk(bootstrapRes, "Control-plane edge-link bootstrap failed");
    const bootstrapData = await bootstrapRes.json().catch(() => ({}));
    const brow = (bootstrapData && typeof bootstrapData === "object" && bootstrapData.row && typeof bootstrapData.row === "object")
      ? bootstrapData.row
      : (bootstrapData && typeof bootstrapData === "object" ? bootstrapData : {});
    return {
      ...(dataLike || {}),
      edge_id: String(dataLike?.edge_id || row?.edge_id || bootstrapData?.edge_id || brow?.edge_id || ""),
      edge_name: String(dataLike?.edge_name || row?.edge_name || bootstrapData?.edge_name || brow?.edge_name || payload?.edge_name || ""),
      customer_id: String(dataLike?.customer_id || row?.customer_id || bootstrapData?.customer_id || brow?.customer_id || ""),
      license_id: String(dataLike?.license_id || row?.license_id || bootstrapData?.license_id || brow?.license_id || (brow?.license || {}).license_id || ""),
      license: {
        ...((row && typeof row.license === "object") ? row.license : {}),
        ...((brow && typeof brow.license === "object") ? brow.license : {}),
      },
      cloud_api_url: String(dataLike?.cloud_api_url || row?.cloud_api_url || bootstrapData?.cloud_api_url || brow?.cloud_api_url || base || ""),
      tenant_id: String(dataLike?.tenant_id || row?.tenant_id || bootstrapData?.tenant_id || brow?.tenant_id || ""),
      primary_domain: String(dataLike?.primary_domain || row?.primary_domain || bootstrapData?.primary_domain || brow?.primary_domain || ""),
    };
  };

  let lastErr = null;
  for (const base of (cloudFirst ? [cloudFirst, ...cloudBases.filter((b) => b !== cloudFirst)] : cloudBases)) {
    try {
      const registerRes = await fetchWithTimeout(`${base}/api/control-plane/edge-link/register`, req, 20000);
      const registerData = await registerRes.json().catch(() => ({}));
      if (registerRes.ok) {
        const scopedData = await ensureActivationScope(base, registerData);
        await tryLocalFinalize(base, scopedData);
        return scopedData;
      }
      const detail = String(registerData?.detail || registerData?.error || "").toLowerCase();
      if (detail.includes("activation_code_used")) {
        const bootstrapRes = await fetchWithTimeout(`${base}/api/control-plane/edge-link/bootstrap`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            activation_code: String(payload?.activation_code || "").trim(),
            edge_id: String(payload?.edge_id || "").trim(),
            edge_name: String(payload?.edge_name || "").trim(),
            site: String(payload?.site || "").trim(),
            area: String(payload?.area || "").trim(),
            equipment: String(payload?.equipment || "").trim(),
          }),
        }, 20000);
        await ensureOk(bootstrapRes, "Control-plane edge-link bootstrap failed");
        const bootstrapData = await bootstrapRes.json().catch(() => ({}));
        const scopedData = await ensureActivationScope(base, bootstrapData);
        await tryLocalFinalize(base, scopedData);
        return {
          ok: true,
          ...scopedData,
          recovered_from_used_code: true,
        };
      }
      await ensureOk(registerRes, "Control-plane edge-link register failed");
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr || new Error("Control-plane edge-link login activation failed");
}

export async function unlinkControlPlaneEdgeLink() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-link/unlink`, {
    method: "POST",
  });
  await ensureOk(res, "Control-plane edge-link unlink failed");
  return res.json();
}

export async function checkControlPlaneEdgeLicense(edgeId = "", tenantId = "") {
  const params = new URLSearchParams();
  if (edgeId) params.set("edge_id", String(edgeId));
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-link/license-check${suffix}`);
  await ensureOk(res, "Control-plane edge license check failed");
  const data = await res.json();
  const licenseId = String(data?.license?.license_id || "").trim();
  const hasModules = Array.isArray(data?.license?.modules) && data.license.modules.length > 0;
  if (licenseId && !hasModules) {
    try {
      const modulesRes = await getControlPlaneLicenseModules(licenseId);
      const rows = Array.isArray(modulesRes?.rows) ? modulesRes.rows : [];
      return {
        ...(data || {}),
        license: {
          ...(data?.license || {}),
          modules: rows,
        },
      };
    } catch {
      // Keep original check response if module-list lookup fails.
    }
  }
  return data;
}

export async function startControlPlaneEdgeTrial(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-link/trial/start${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Start trial failed");
  return res.json();
}

export async function listControlPlaneEdgeTrialHistory({ edgeId = "", licenseId = "", tenantId = "", limit = 200 } = {}) {
  const params = new URLSearchParams();
  if (edgeId) params.set("edge_id", String(edgeId));
  if (licenseId) params.set("license_id", String(licenseId));
  if (tenantId) params.set("tenant_id", String(tenantId));
  if (limit) params.set("limit", String(limit));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/edge-link/trial/history${suffix}`);
  await ensureOk(res, "Trial history fetch failed");
  return res.json();
}

export async function issueControlPlanePasswordReset(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/password-reset/issue${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane password reset issue failed");
  return res.json();
}

export async function applyControlPlanePasswordReset(payload, tenantId = "") {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", String(tenantId));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/password-reset/apply${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane password reset apply failed");
  return res.json();
}

export async function issuePublicPasswordReset(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/password-reset/public/issue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Password reset code issue failed");
  return res.json();
}

export async function applyPublicPasswordReset(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/password-reset/public/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Password reset apply failed");
  return res.json();
}

// Operator 2026-06-24: email-based reset (edge-local, no portal).
// Sends a reset link to the user's email via the configured SMTP.
export async function emailPasswordReset(identifier) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/auth/forgot-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier: String(identifier || "") }),
  });
  await ensureOk(res, "Forgot password failed");
  return res.json();
}

export async function applyEmailPasswordReset(token, newPassword) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/auth/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: String(token || ""), new_password: String(newPassword || "") }),
  });
  await ensureOk(res, "Reset password failed");
  return res.json();
}

export async function provisionControlPlaneCustomerBundle(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/control-plane/provision/customer-bundle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {})
  });
  await ensureOk(res, "Control-plane customer bundle provision failed");
  return res.json();
}

// ---------------------------------------------------------------------------
// Reporting module: templates, schedules, generated reports.
// Backed by /api/reports/* on the FastAPI edge backend (see routers/reports.py).
// ---------------------------------------------------------------------------
function _reportApiBase() {
  return getAppStoreApiBase();
}

export async function listReportTemplates() {
  const res = await fetchWithTimeout(withNoCache(`${_reportApiBase()}/api/reports/templates`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0" },
  });
  await ensureOk(res, "List report templates failed");
  return res.json();
}

export async function getCompanyLogo() {
  const res = await fetchWithTimeout(withNoCache(`${_reportApiBase()}/api/reports/branding/company-logo`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0" },
  });
  await ensureOk(res, "Get company logo failed");
  return res.json();
}

export async function setCompanyLogo(dataUrl) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/branding/company-logo`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: String(dataUrl || "") }),
  });
  await ensureOk(res, "Save company logo failed");
  return res.json();
}

export async function deleteCompanyLogo() {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/branding/company-logo`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete company logo failed");
  return res.json();
}

export async function getReportTemplate(templateId) {
  const res = await fetchWithTimeout(withNoCache(`${_reportApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}`));
  await ensureOk(res, "Get report template failed");
  return res.json();
}

export async function saveReportTemplate(template) {
  const id = String(template?.id || "").trim();
  const url = id
    ? `${_reportApiBase()}/api/reports/templates/${encodeURIComponent(id)}`
    : `${_reportApiBase()}/api/reports/templates`;
  const method = id ? "PUT" : "POST";
  const res = await fetchWithTimeout(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(template || {}),
  });
  await ensureOk(res, "Save report template failed");
  return res.json();
}

export async function deleteReportTemplate(templateId) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete report template failed");
  return res.json();
}

export async function exportReportTemplate(templateId) {
  const res = await fetchWithTimeout(
    `${_reportApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}/export`
  );
  await ensureOk(res, "Export report template failed");
  return res.json();
}

export async function exportAllReportTemplates() {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/templates-export-all`);
  await ensureOk(res, "Export report templates failed");
  return res.json();
}

export async function importReportTemplates(bundle) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/templates/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bundle || {}),
  });
  await ensureOk(res, "Import report templates failed");
  return res.json();
}

export async function listScheduledReports() {
  const res = await fetchWithTimeout(withNoCache(`${_reportApiBase()}/api/reports/schedules`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0" },
  });
  await ensureOk(res, "List scheduled reports failed");
  return res.json();
}

export async function saveScheduledReport(schedule) {
  const id = String(schedule?.id || "").trim();
  const url = id
    ? `${_reportApiBase()}/api/reports/schedules/${encodeURIComponent(id)}`
    : `${_reportApiBase()}/api/reports/schedules`;
  const method = id ? "PUT" : "POST";
  const res = await fetchWithTimeout(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(schedule || {}),
  });
  await ensureOk(res, "Save scheduled report failed");
  return res.json();
}

export async function deleteScheduledReport(scheduleId) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete scheduled report failed");
  return res.json();
}

/**
 * Trigger a one-off run of a saved schedule. When the schedule has
 * `require_gateway_running` set, the backend rejects the call with HTTP 409
 * unless `force=true`. Callers should surface that error so the user can
 * choose to bypass.
 */
export async function runScheduledReport(scheduleId, emailSettings = null, { force = false } = {}) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/schedules/${encodeURIComponent(scheduleId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email_settings: emailSettings || null, force: !!force }),
  }, 60000);
  if (res.status === 409) {
    let detail = "Gateway is not running.";
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch { /* keep default */ }
    const err = new Error(detail);
    err.code = "GATEWAY_REQUIRED";
    err.status = 409;
    throw err;
  }
  await ensureOk(res, "Run scheduled report failed");
  return res.json();
}

export async function getReportSchedulerStatus() {
  const res = await fetchWithTimeout(withNoCache(`${_reportApiBase()}/api/reports/scheduler/status`), {
    headers: { "Cache-Control": "no-store" },
  });
  await ensureOk(res, "Scheduler status fetch failed");
  return res.json();
}

export async function listGeneratedReports({ limit = 200, scheduleId = "", templateId = "" } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (scheduleId) params.set("schedule_id", String(scheduleId));
  if (templateId) params.set("template_id", String(templateId));
  const res = await fetchWithTimeout(
    withNoCache(`${_reportApiBase()}/api/reports/generated?${params.toString()}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0" } },
  );
  await ensureOk(res, "List generated reports failed");
  return res.json();
}

export async function deleteGeneratedReport(generatedId) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/generated/${encodeURIComponent(generatedId)}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete generated report failed");
  return res.json();
}

export async function emailGeneratedReport(generatedId, {
  recipients,
  subject = "",
  htmlBody = "",
  textBody = "",
  emailSettings,
  attachPdf = true,
  attachCsv = false,
  attachTxt = false,
} = {}) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/generated/${encodeURIComponent(generatedId)}/email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recipients: Array.isArray(recipients) ? recipients : [],
      subject,
      html_body: htmlBody,
      text_body: textBody,
      email_settings: emailSettings || null,
      attach_pdf: !!attachPdf,
      attach_csv: !!attachCsv,
      attach_txt: !!attachTxt,
    }),
  }, 60000);
  await ensureOk(res, "Email generated report failed");
  return res.json();
}

export async function exportSectionCsv(section) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/export/csv`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section: section || {} }),
  }, 60000);
  if (!res.ok) throw new Error(`CSV export failed (HTTP ${res.status})`);
  return res.blob();
}

export async function exportSectionTxt(section) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/export/txt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section: section || {} }),
  }, 60000);
  if (!res.ok) throw new Error(`TXT export failed (HTTP ${res.status})`);
  return res.blob();
}

export async function getReportTemplatePreviewData(templateId) {
  const res = await fetchWithTimeout(
    withNoCache(`${_reportApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}/preview-data`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0" } },
    20000,
  );
  await ensureOk(res, "Report preview data fetch failed");
  return res.json();
}

export async function runReportTemplateNow(templateId) {
  const res = await fetchWithTimeout(
    `${_reportApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}/generate`,
    { method: "POST", headers: { "Content-Type": "application/json" } },
    60000,
  );
  await ensureOk(res, "Generate report failed");
  return res.json();
}

export function openGeneratedReport(generatedId, { inline = true } = {}) {
  const url = getGeneratedReportFileUrl(generatedId, { inline });
  try { window.open(url, "_blank", "noopener,noreferrer"); }
  catch (_) { window.location.href = url; }
}

export async function renderReportPreview(template) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/render`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ template: template || {} }),
  }, 60000);
  await ensureOk(res, "Report preview render failed");
  return res.json();
}

export function getGeneratedReportFileUrl(generatedId, { inline = false } = {}) {
  const token = getAuthToken();
  const inlineFlag = inline ? "&inline=true" : "";
  const auth = token ? `?token=${encodeURIComponent(token)}${inlineFlag}` : (inlineFlag ? `?inline=true` : "");
  // The browser will send the auth header via the fetch API for downloads,
  // but `<a href>` and `<iframe src>` need a same-origin request with token
  // baked into the URL. Use this helper from places that need a raw URL.
  return `${_reportApiBase()}/api/reports/generated/${encodeURIComponent(generatedId)}/file${auth}`;
}

export async function downloadGeneratedReportBlob(generatedId) {
  const res = await fetchWithTimeout(
    `${_reportApiBase()}/api/reports/generated/${encodeURIComponent(generatedId)}/file`,
    { headers: { Accept: "application/pdf" } },
    60000,
  );
  if (!res.ok) {
    throw new Error(`Download failed (HTTP ${res.status})`);
  }
  return res.blob();
}

export async function pushSchedulerEmailSettings(emailSettings) {
  const res = await fetchWithTimeout(`${_reportApiBase()}/api/reports/scheduler/email-settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(emailSettings || {}),
  });
  await ensureOk(res, "Push scheduler email settings failed");
  return res.json();
}


// ====================================================================
// Batch Management & Traceability module (2026-06-23)
// Every endpoint is gated server-side by require_batch_management_license.
// When the customer doesn't have the license, every call returns 404 with
// {detail:{module:"batch_management", reason:"not_in_license"}}. The
// frontend hides the menu in that case (canOpenPage), so these helpers
// are only invoked when the license is active.
// ====================================================================
const _BM_BASE = () => `${getControlApiBase()}/api/batch-management`;

// Operator 2026-07-06: absolute URL for a batch download link (PDF/CSV). The
// desktop shell loads the UI from file://, so a relative "/api/..." href would
// resolve against file:// and never reach the backend — download links MUST be
// absolute (built off the same base the fetch calls use, which honors
// ?backendUrl=). Use this for every <a href> that hits the batch API.
export function bmDownloadUrl(path) {
  const p = String(path || "");
  return `${_BM_BASE()}${p.startsWith("/") ? p : "/" + p}`;
}

export async function getBatchManagementStatus() {
  const res = await fetchWithTimeout(`${_BM_BASE()}/status`, {}, 6000);
  if (!res.ok) return { module: "batch_management", enabled: false };
  return res.json();
}

export async function listBatchTypes() {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batch-types`);
  await ensureOk(res, "List batch types failed");
  return (await res.json()).rows || [];
}

export async function saveBatchType(payload, id = null) {
  const url = id ? `${_BM_BASE()}/batch-types/${encodeURIComponent(id)}` : `${_BM_BASE()}/batch-types`;
  const res = await fetchWithTimeout(url, {
    method: id ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Save batch type failed");
  return (await res.json()).row;
}

export async function deleteBatchType(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batch-types/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete batch type failed");
  return true;
}

export async function listBatches(params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    qs.set(k, String(v));
  }
  const url = `${_BM_BASE()}/batches${qs.toString() ? `?${qs.toString()}` : ""}`;
  const res = await fetchWithTimeout(url);
  await ensureOk(res, "List batches failed");
  return res.json();
}

export async function getBatch(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}`);
  await ensureOk(res, "Get batch failed");
  return (await res.json()).row;
}

export async function createBatch(payload) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Create batch failed");
  return (await res.json()).row;
}

export async function startBatch(id, payload = {}) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await ensureOk(res, "Start batch failed");
  return (await res.json()).row;
}

export async function stopBatch(id, payload = {}) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await ensureOk(res, "Stop batch failed");
  return (await res.json()).row;
}

// Operator 2026-07-06: keyboard-wedge barcode scan → start (default) or stop a
// batch whose identifier IS the scanned code.
export async function scanBatch(payload) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Barcode scan failed");
  return (await res.json()).row;
}

// Operator 2026-07-06: MULTIPLE parent — close current child, open the next.
export async function nextChildBatch(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/next-child`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  await ensureOk(res, "Next child failed");
  return (await res.json()).child;
}

// Seed the two starter types (Single + Multiple) if none exist.
export async function seedBatchDefaults() {
  const res = await fetchWithTimeout(`${_BM_BASE()}/seed-defaults`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  await ensureOk(res, "Seed defaults failed");
  return (await res.json());
}

// Distinct tags that had data during a batch's window (report-builder pick-list).
export async function getBatchCollectedTags(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/collected-tags`, {}, 8000);
  await ensureOk(res, "Collected tags failed");
  return (await res.json()).tags || [];
}

// Operator 2026-07-09: manual operator entries + report context (limits/result).
export async function getBatchManualEntries(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/manual-entries`, {}, 8000);
  await ensureOk(res, "List manual entries failed");
  return (await res.json()).rows || [];
}

export async function saveBatchManualEntries(id, entries) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/manual-entries`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entries: entries || [] }),
  });
  await ensureOk(res, "Save manual entries failed");
  return (await res.json()).rows || [];
}

export async function getBatchReportContext(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/report-context`, {}, 10000);
  await ensureOk(res, "Batch report context failed");
  return await res.json();
}

// Downsampled per-tag series within the batch window (for the in-UI charts).
export async function getBatchChart(id, tags, maxPoints = 400) {
  const q = encodeURIComponent((tags || []).join(","));
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/chart?tags=${q}&max_points=${maxPoints}`, {}, 15000);
  await ensureOk(res, "Batch chart failed");
  return await res.json();
}

export async function validateBatch(id, payload) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Validate batch failed");
  return (await res.json()).row;
}

export async function listBatchEvents(id, limit = 200) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/events?limit=${limit}`);
  await ensureOk(res, "List batch events failed");
  return (await res.json()).rows || [];
}

export async function addBatchEvent(id, payload) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  await ensureOk(res, "Add batch event failed");
  return res.json();
}

export async function listBatchSummaries(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/summaries`);
  await ensureOk(res, "List batch summaries failed");
  return (await res.json()).rows || [];
}

export async function recomputeBatchSummaries(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/recompute-summaries`, {
    method: "POST",
  });
  await ensureOk(res, "Recompute summaries failed");
  return res.json();
}

export async function listBatchHistorianRows(id, limit = 5000) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}/historian?limit=${limit}`);
  await ensureOk(res, "List batch historian rows failed");
  return (await res.json()).rows || [];
}

export async function deleteBatch(id) {
  const res = await fetchWithTimeout(`${_BM_BASE()}/batches/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  await ensureOk(res, "Delete batch failed");
  return true;
}

export async function listBatchAudit(limit = 200, batchId = null) {
  const qs = new URLSearchParams();
  qs.set("limit", String(limit));
  if (batchId) qs.set("batch_id", String(batchId));
  const res = await fetchWithTimeout(`${_BM_BASE()}/audit?${qs.toString()}`);
  await ensureOk(res, "List batch audit failed");
  return (await res.json()).rows || [];
}

/* ======================================================================
 *  Batch Management v2 (clean rebuild) — spec-named API.
 *  Base: /api/batch-management/v2. The legacy fns above remain for the
 *  old (now-inert) pages; the redesigned UI uses these.
 *  Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
 * ==================================================================== */
const _BMV2 = () => `${getControlApiBase()}/api/batch-management/v2`;

function _qs(params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

async function _bmGet(path) {
  const res = await fetchWithTimeout(`${_BMV2()}${path}`);
  await ensureOk(res, `GET ${path} failed`);
  return res.json();
}

async function _bmSend(path, method, body) {
  const res = await fetchWithTimeout(`${_BMV2()}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  await ensureOk(res, `${method} ${path} failed`);
  return res.json();
}

export function bmv2FileUrl(generatedId, inline = true) {
  // reuse the EXISTING Report module's file endpoint for preview/download.
  // An iframe src / <a download> can't set an Authorization header, so the
  // JWT rides as ?access_token= (the auth middleware accepts it for this
  // file route only). Without it the preview iframe showed
  // {"detail":"Authentication required"}.
  const tok = getAuthToken();
  const params = [];
  if (inline) params.push("inline=true");
  if (tok) params.push(`access_token=${encodeURIComponent(tok)}`);
  const qs = params.length ? `?${params.join("&")}` : "";
  return `${getControlApiBase()}/api/reports/generated/${encodeURIComponent(generatedId)}/file${qs}`;
}
export function bmv2PreviewDataUrl(templateId) {
  const tok = getAuthToken();
  const qs = tok ? `?access_token=${encodeURIComponent(tok)}` : "";
  return `${getControlApiBase()}/api/reports/templates/${encodeURIComponent(templateId)}/preview-data${qs}`;
}

export async function bmv2Status() {
  // The /status endpoint always returns HTTP 200 with {enabled: true|false};
  // "not licensed" is a 200 body, never an error. So any THROW here is a
  // transient failure (network / timeout / aborted fetch on fast navigation /
  // 5xx while the backend is still booting). We must PROPAGATE it so the
  // caller (useLicense) keeps its last-known-good state instead of downgrading.
  // Returning a fabricated {enabled:false} here was the bug that made the
  // pages flash "not licensed" after a rebuild/restart or quick navigation.
  return await _bmGet("/status");
}
export async function bmv2SeedReportTemplates() { return _bmSend("/seed-report-templates", "POST", {}); }
// Report templates the definition wizard can offer, {batch:[...], group:[...]},
// including custom customer templates from the Reports module.
export async function bmv2ReportTemplates() { return _bmGet("/report-templates"); }
// Duplicate a batch/group report template into a new editable+exportable one
// (lands in the shared Reports store, keeps its batch/group scope).
export async function bmv2DuplicateReportTemplate(templateId, name) {
  return _bmSend(`/report-templates/${encodeURIComponent(templateId)}/duplicate`, "POST", name ? { name } : {});
}

// ---- Definitions ----
export async function bmv2ListDefinitions() { return (await _bmGet("/definitions")).rows || []; }
export async function bmv2GetDefinition(id, versionId = null) {
  return (await _bmGet(`/definitions/${encodeURIComponent(id)}${_qs({ version_id: versionId })}`)).row;
}
export async function bmv2SaveDefinition(payload, id = null) {
  const r = id
    ? await _bmSend(`/definitions/${encodeURIComponent(id)}`, "PUT", payload)
    : await _bmSend(`/definitions`, "POST", payload);
  return r.row;
}
export async function bmv2DeleteDefinition(id) { return (await _bmSend(`/definitions/${encodeURIComponent(id)}`, "DELETE", {})).ok; }
export async function bmv2ValidateDefinition(id) { return _bmSend(`/definitions/${encodeURIComponent(id)}/validate`, "POST", {}); }
export async function bmv2PublishDefinition(id) { return (await _bmSend(`/definitions/${encodeURIComponent(id)}/publish`, "POST", {})).row; }
export async function bmv2ListVersions(id) { return (await _bmGet(`/definitions/${encodeURIComponent(id)}/versions`)).rows || []; }
export async function bmv2NewVersion(id) { return (await _bmSend(`/definitions/${encodeURIComponent(id)}/versions`, "POST", {})).row; }

// ---- Batches ----
export async function bmv2ListBatches(params = {}) { return _bmGet(`/batches${_qs(params)}`); }
export async function bmv2GetBatch(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}`)).row; }
export async function bmv2CreateBatch(payload) { return (await _bmSend(`/batches`, "POST", payload)).row; }
export async function bmv2BatchAction(id, action, payload = {}) {
  return (await _bmSend(`/batches/${encodeURIComponent(id)}/${action}`, "POST", payload)).row;
}

// Operator 2026-07-30: one-shot barcode resolver — the server decides whether
// the scanned/typed code stops a barcode-gated running batch, starts a
// planned/ready one, or creates+starts from a published barcode-start
// definition. Returns {ok, action: started|stopped|already_running, row}.
export async function bmv2ScanBatch(payload) {
  return await _bmSend(`/batches/scan`, "POST", payload);
}
export async function bmv2DeleteBatch(id) { return _bmSend(`/batches/${encodeURIComponent(id)}`, "DELETE"); }
export async function bmv2DeleteBatchReport(batchId, refId) { return _bmSend(`/batches/${encodeURIComponent(batchId)}/reports/${encodeURIComponent(refId)}`, "DELETE"); }
export async function bmv2AddComment(id, message, actor = null) {
  return _bmSend(`/batches/${encodeURIComponent(id)}/comments`, "POST", { message, actor });
}
export async function bmv2BatchEvents(id, limit = 200) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/events${_qs({ limit })}`)).rows || []; }
export async function bmv2BatchTrends(id, tags = "", maxPoints = 400) {
  return (await _bmGet(`/batches/${encodeURIComponent(id)}/trends${_qs({ tags, max_points: maxPoints })}`)).series || [];
}
export async function bmv2BatchKpis(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/kpis`)).rows || []; }
export async function bmv2RecomputeBatch(id) { return _bmSend(`/batches/${encodeURIComponent(id)}/recompute`, "POST", {}); }
export async function bmv2BatchExcursions(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/excursions`)).rows || []; }
export async function bmv2BatchCollectedTags(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/collected-tags`)).tags || []; }
// Custom batch properties (barcode / order # / equipment / ...).
export async function bmv2BatchProperties(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/properties`)).rows || []; }
// Aligned tag matrix (rows=timestamps, cols=tags, per-row in-limits), downsampled.
export async function bmv2BatchMatrix(id, tags = "", maxRows = 200) {
  return _bmGet(`/batches/${encodeURIComponent(id)}/matrix${_qs({ tags, max_rows: maxRows })}`);
}
export async function bmv2BatchDefinitionProperties(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/definition-properties`)).rows || []; }
export async function bmv2ListBatchReports(id) { return (await _bmGet(`/batches/${encodeURIComponent(id)}/reports`)).rows || []; }
export async function bmv2GenerateBatchReport(id, templateId = null) {
  return _bmSend(`/batches/${encodeURIComponent(id)}/reports`, "POST", { template_id: templateId });
}
export async function bmv2EmailBatchReport(id, referenceId, payload) {
  return _bmSend(`/batches/${encodeURIComponent(id)}/reports/${encodeURIComponent(referenceId)}/email`, "POST", payload || {});
}

// ---- Groups ----
export async function bmv2ListGroups(params = {}) { return _bmGet(`/groups${_qs(params)}`); }
export async function bmv2GetGroup(id) { return (await _bmGet(`/groups/${encodeURIComponent(id)}`)).row; }
export async function bmv2CreateGroup(payload) { return (await _bmSend(`/groups`, "POST", payload)).row; }
export async function bmv2CompleteGroup(id) { return (await _bmSend(`/groups/${encodeURIComponent(id)}/complete`, "POST", {})).row; }
export async function bmv2AbortGroup(id) { return (await _bmSend(`/groups/${encodeURIComponent(id)}/abort`, "POST", {})).row; }
export async function bmv2DeleteGroup(id) { return _bmSend(`/groups/${encodeURIComponent(id)}`, "DELETE"); }
export async function bmv2DeleteGroupReport(groupId, refId) { return _bmSend(`/groups/${encodeURIComponent(groupId)}/reports/${encodeURIComponent(refId)}`, "DELETE"); }
export async function bmv2GroupBatches(id) { return (await _bmGet(`/groups/${encodeURIComponent(id)}/batches`)).rows || []; }
export async function bmv2GroupKpis(id) { return (await _bmGet(`/groups/${encodeURIComponent(id)}/kpis`)).rows || []; }
export async function bmv2ListGroupReports(id) { return (await _bmGet(`/groups/${encodeURIComponent(id)}/reports`)).rows || []; }
export async function bmv2GenerateGroupReport(id, templateId = null) {
  return _bmSend(`/groups/${encodeURIComponent(id)}/reports`, "POST", { template_id: templateId });
}
export async function bmv2EmailGroupReport(id, referenceId, payload) {
  return _bmSend(`/groups/${encodeURIComponent(id)}/reports/${encodeURIComponent(referenceId)}/email`, "POST", payload || {});
}

// ---- Analysis ----
export async function bmv2AnalysisExcursions(limit = 500) { return (await _bmGet(`/analysis/excursions${_qs({ limit })}`)).rows || []; }
export async function bmv2AckExcursion(id, payload) { return (await _bmSend(`/analysis/excursions/${encodeURIComponent(id)}/ack`, "POST", payload || {})).row; }
export async function bmv2AnalysisComparison(batchIds = [], tags = [], maxPoints = 400) {
  return (await _bmGet(`/analysis/comparison${_qs({ batch_ids: batchIds.join(","), tags: tags.join(","), max_points: maxPoints })}`)).batches || [];
}

// Normalize already-loaded gatewayConfigs (passed from App.jsx) into
// {id,name,tags:[{name,unit,data_type}]} for the definition builder's tag picker.
// No network — reuses the config the app already holds.
export function bmv2NormalizeGatewayTags(gatewayConfigs = []) {
  // A gateway's `tags` may be EITHER a plain array of names
  // (["BT_PVA_Level", ...] — what the gateway config actually stores) OR an
  // array of objects ({name, unit, data_type}). The old code only handled the
  // object form, so `t.name` was undefined for string tags, every tag got
  // filtered out, and the batch-definition tag picker fell back to a free-text
  // box instead of listing the gateway's real tags. Handle both shapes.
  const toTag = (t) => {
    if (typeof t === "string") return { name: t.trim(), unit: "", data_type: "" };
    if (t && typeof t === "object") {
      const name = String(t.name ?? t.tag_name ?? t.tag ?? t.id ?? "").trim();
      return { name, unit: t.unit || t.engineering_unit || "", data_type: t.data_type || t.type || "" };
    }
    return { name: "", unit: "", data_type: "" };
  };
  return (gatewayConfigs || []).map((g) => ({
    id: String(g.id || ""),
    name: String(g.name || g.id || ""),
    tags: (g.tags || []).map(toTag).filter((t) => t.name),
  }));
}
