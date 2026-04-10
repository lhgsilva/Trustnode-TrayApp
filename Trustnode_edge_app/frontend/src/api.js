const STORAGE_MODE_KEY = "trustnode_backend_mode";
const STORAGE_CLOUD_URL_KEY = "trustnode_backend_cloud_url";
const AUTH_TOKEN_KEY = "trustnode_auth_token";
const FORCE_CLOUD_URL_RAW = normalizeBaseUrl(import.meta.env.VITE_TRUSTNODE_FORCE_CLOUD_URL || "");
const FORCE_CLOUD_URL =
  /(^https?:\/\/your-cloud-backend\.example\.com$)|(^https?:\/\/api\.example\.com$)/i.test(FORCE_CLOUD_URL_RAW)
    ? ""
    : FORCE_CLOUD_URL_RAW;
const FORCE_READONLY = String(import.meta.env.VITE_TRUSTNODE_READONLY || "").toLowerCase() === "true";

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

function withNoCache(url) {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}_ts=${Date.now()}`;
}

export function isForcedReadonlyCloudMode() {
  return Boolean(FORCE_CLOUD_URL && FORCE_READONLY);
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

export async function loginAuth(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/auth/login`, {
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
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/config`);
  if (!res.ok) throw new Error("Config fetch failed");
  return res.json();
}

export async function updateConfig(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Config update failed");
  return res.json();
}

export async function getStatus() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/status`);
  if (!res.ok) throw new Error("Status fetch failed");
  return res.json();
}

export async function startGateway() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/start`, { method: "POST" });
  if (!res.ok) throw new Error("Start failed");
  return res.json();
}

export async function stopGateway() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/stop`, { method: "POST" });
  if (!res.ok) throw new Error("Stop failed");
  return res.json();
}

export async function startGatewayInstance(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/gateways/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 30000);
  if (!res.ok) throw new Error("Gateway instance start failed");
  return res.json();
}

export async function stopGatewayInstance(gatewayId) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/gateways/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gateway_id: gatewayId })
  }, 20000);
  if (!res.ok) throw new Error("Gateway instance stop failed");
  return res.json();
}

export async function getGatewayInstanceStatuses() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/gateways/status`);
  if (!res.ok) throw new Error("Gateway instance status fetch failed");
  return res.json();
}

export async function stopAllGatewayInstances() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/gateways/stop-all`, {
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
      res = await fetchWithTimeout(`${getApiBase()}/api/plc/test-connection`, request, 20000);
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
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/discover-tags`, {
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
  const res = await fetchWithTimeout(`${getApiBase()}/api/plc/opcua/browse`, {
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
    res = await fetchWithTimeout(`${getApiBase()}/api/database/test-connection`, {
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
  const res = await fetchWithTimeout(`${getApiBase()}/api/database/provision`, {
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
  const res = await fetchWithTimeout(`${getApiBase()}/api/database/activate-sink`, {
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
  const res = await fetchWithTimeout(withNoCache(`${getApiBase()}/api/app-store/bootstrap`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" }
  });
  if (!res.ok) throw new Error("App store bootstrap fetch failed");
  return res.json();
}

export async function saveAppStoreBootstrap(data, actor = "system") {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/bootstrap`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data, actor })
  });
  if (!res.ok) throw new Error("App store bootstrap save failed");
  return res.json();
}

export async function getAppStoreTenantContext() {
  const res = await fetchWithTimeout(withNoCache(`${getApiBase()}/api/app-store/tenant/context`), {
    headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" }
  });
  if (!res.ok) throw new Error("Tenant context fetch failed");
  return res.json();
}

export async function appendAppStoreHistorian(rows) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/append/historian`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows })
  });
  if (!res.ok) throw new Error("App store historian append failed");
  return res.json();
}

export async function appendAppStoreLogs(rows) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/append/logs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows })
  });
  if (!res.ok) throw new Error("App store logs append failed");
  return res.json();
}

export async function getAppStoreHistorian(limit = 1000) {
  const res = await fetchWithTimeout(
    withNoCache(`${getApiBase()}/api/app-store/historian?limit=${encodeURIComponent(String(limit))}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store historian fetch failed");
  return res.json();
}

export async function getAppStoreLive(limit = 5000) {
  const res = await fetchWithTimeout(
    withNoCache(`${getApiBase()}/api/app-store/live?limit=${encodeURIComponent(String(limit))}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store live fetch failed");
  return res.json();
}

export async function getAppStoreLogs(limit = 2000) {
  const res = await fetchWithTimeout(
    withNoCache(`${getApiBase()}/api/app-store/logs?limit=${encodeURIComponent(String(limit))}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store logs fetch failed");
  return res.json();
}

export async function getAppStoreInspector(previewLimit = 15) {
  const res = await fetchWithTimeout(
    withNoCache(`${getApiBase()}/api/app-store/inspector?preview_limit=${encodeURIComponent(String(previewLimit))}`),
    { headers: { "Cache-Control": "no-store, no-cache, max-age=0", Pragma: "no-cache" } }
  );
  if (!res.ok) throw new Error("App store inspector fetch failed");
  return res.json();
}

export async function checkDatabaseRecovery(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/database/recovery/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Database recovery check failed");
  return res.json();
}

export async function repairDatabaseRecovery(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/database/recovery/repair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Database recovery repair failed");
  return res.json();
}

export async function getRetentionPolicy() {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/retention/policy`);
  if (!res.ok) throw new Error("Retention policy fetch failed");
  return res.json();
}

export async function updateRetentionPolicy(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/retention/policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Retention policy update failed");
  return res.json();
}

export async function runRetention(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/retention/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Retention run failed");
  return res.json();
}

export async function getRetentionRuns(limit = 20) {
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/app-store/retention/runs?limit=${encodeURIComponent(String(limit))}`
  );
  if (!res.ok) throw new Error("Retention runs fetch failed");
  return res.json();
}

export async function getAppStoreBackups(limit = 200) {
  const res = await fetchWithTimeout(
    `${getApiBase()}/api/app-store/backups?limit=${encodeURIComponent(String(limit))}`
  );
  if (!res.ok) throw new Error("Backups fetch failed");
  return res.json();
}

export async function createAppStoreBackup(payload = {}) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/backups/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Create backup failed");
  return res.json();
}

export async function restoreAppStoreBackup(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/backups/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Restore backup failed");
  return res.json();
}

export async function deleteAppStoreBackup(filename) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/backups/${encodeURIComponent(String(filename || ""))}`, {
    method: "DELETE"
  });
  if (!res.ok) throw new Error("Delete backup failed");
  return res.json();
}

export async function cleanupAppStoreData(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/cleanup-data`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Data cleanup failed");
  return res.json();
}

export async function forceAppStoreSyncNow(payload = {}) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/sync/force`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Force sync failed");
  return res.json();
}

export async function manualPeriodSyncAppStore(payload) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/sync/manual-period`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 30000);
  if (!res.ok) throw new Error("Manual period sync failed");
  return res.json();
}

export async function clearAppStoreSyncQueue(payload = {}) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/sync/queue/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Clear sync queue failed");
  return res.json();
}

export async function dropAppStoreSyncBacklog(payload = {}) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/sync/backlog/drop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error("Drop sync backlog failed");
  return res.json();
}

export async function resetAppStoreFull(payload = {}) {
  const res = await fetchWithTimeout(`${getApiBase()}/api/app-store/reset/full`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 120000);
  if (!res.ok) throw new Error("Full reset failed");
  return res.json();
}
