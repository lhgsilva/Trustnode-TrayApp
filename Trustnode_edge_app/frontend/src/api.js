const STORAGE_MODE_KEY = "trustnode_backend_mode";
const STORAGE_CLOUD_URL_KEY = "trustnode_backend_cloud_url";
const AUTH_TOKEN_KEY = "trustnode_auth_token";
const FORCE_CLOUD_URL_RAW = normalizeBaseUrl(import.meta.env.VITE_TRUSTNODE_FORCE_CLOUD_URL || "");
const FORCE_CLOUD_URL =
  /(^https?:\/\/your-cloud-backend\.example\.com$)|(^https?:\/\/api\.example\.com$)/i.test(FORCE_CLOUD_URL_RAW)
    ? ""
    : FORCE_CLOUD_URL_RAW;
const CONTROL_PLANE_FALLBACK_URL = normalizeBaseUrl(
  import.meta.env.VITE_TRUSTNODE_CONTROL_PLANE_URL || "https://trustnode.lsapps.app"
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

function isHostedWebClientRuntime() {
  const protocol = String(window.location.protocol || "").toLowerCase();
  const host = String(window.location.hostname || "").toLowerCase();
  const isLocalHost = host === "localhost" || host === "127.0.0.1" || host === "::1";
  const userAgent = String(window.navigator?.userAgent || "");
  const isElectronRuntime = /electron/i.test(userAgent);
  if (isElectronRuntime) return false;
  const hasDesktopBackendOverride = Boolean(new URLSearchParams(window.location.search).get("backendUrl"));
  if (hasDesktopBackendOverride) return false;
  return (protocol === "https:" || protocol === "http:") && !isLocalHost;
}

export function getBackendTarget() {
  if (FORCE_CLOUD_URL) {
    return { mode: "cloud", cloudUrl: FORCE_CLOUD_URL, forced: true };
  }
  if (isHostedWebClientRuntime()) {
    const cloudUrl = normalizeBaseUrl(localStorage.getItem(STORAGE_CLOUD_URL_KEY) || window.location.origin || "");
    return { mode: "cloud", cloudUrl, forced: false };
  }
  const mode = localStorage.getItem(STORAGE_MODE_KEY) || "local";
  const cloudUrl = localStorage.getItem(STORAGE_CLOUD_URL_KEY) || "";
  return { mode, cloudUrl };
}

export function setBackendTarget(mode, cloudUrl = "") {
  if (FORCE_CLOUD_URL) return;
  const hosted = isHostedWebClientRuntime();
  const nextMode = hosted ? "cloud" : mode;
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
    const hasCacheOption = Object.prototype.hasOwnProperty.call(options || {}, "cache");
    const finalOptions = hasCacheOption
      ? { ...options, headers, signal: controller.signal }
      : { ...options, headers, signal: controller.signal, cache: "no-store" };
    return await fetch(url, finalOptions);
  } finally {
    clearTimeout(timeout);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isTransientFetchError(err) {
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
  const request = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  let res;
  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      // PLC checks can be slow on first hit (ARP/NIC wakeups/edge startup).
      res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/test-connection`, request, 20000);
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
      throw new Error(
        "Connection test transport timeout. Backend may still be starting or local network route is unstable. Please retry."
      );
    }
    throw lastErr || new Error("Connection test request failed");
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
  const res = await fetchWithTimeout(`${getControlApiBase()}/api/plc/discover-tags`, {
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
        "Tag discovery endpoint not found (HTTP 404). You are running an old backend build. Restart/rebuild backend and desktop package."
      );
    }
    throw new Error(`Tag discovery failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`);
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

export async function saveAppStoreBootstrap(data, actor = "system") {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/bootstrap`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data, actor })
  });
  if (!res.ok) throw new Error("App store bootstrap save failed");
  return res.json();
}

export async function saveAppStoreDomain(domain, payload, actor = "system") {
  const res = await fetchWithTimeout(`${getAppStoreApiBase()}/api/app-store/domain`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domain, payload, actor })
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

export async function listGeneratedReports({ limit = 200, scheduleId = "" } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (scheduleId) params.set("schedule_id", String(scheduleId));
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
