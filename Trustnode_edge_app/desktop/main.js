const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");
const { URL } = require("url");

let mainWindow = null;
let splashWindow = null;
let tray = null;
let backendProc = null;
let backendExited = false;
let backendExitCode = null;
const backendLogs = [];
let ownsBackendProcess = false;
let backendMonitorTimer = null;
const APP_DISPLAY_NAME = "TrustNode";
const APP_WINDOW_TITLE = "TrustNode";
const BACKEND_EXE_NAME = "trustnode-service.exe";
const LEGACY_USER_DATA_DIR = "trustnode-edge-desktop";
const PREVIOUS_USER_DATA_DIRS = ["trustnode-desktop"];

// Override the app name BEFORE the single-instance request so any Windows
// shell prompts (jump list, "More info" prompts, OS dialogs) show TrustNode
// instead of the default "Electron". Setting it on the app instance alone
// is not enough — process.title also drives the right-click context menu
// label on Windows for the running process.
try { app.setName(APP_DISPLAY_NAME); } catch (_) {}
try { if (process && process.title) process.title = APP_DISPLAY_NAME; } catch (_) {}
if (process.platform === "win32") {
  try { app.setAppUserModelId("com.trustnode.edge"); } catch (_) {}
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();

if (!gotSingleInstanceLock) {
  try {
    dialog.showMessageBoxSync({
      type: "warning",
      title: "Trustnode Already Running",
      message: "Trustnode is already running (possibly minimized to tray).",
      detail:
        "Close the existing Trustnode instance from the tray or use the restart script to relaunch cleanly.",
      buttons: ["OK"],
      noLink: true,
    });
  } catch (_) {}
  app.quit();
  try {
    process.exit(0);
  } catch (_) {}
}

function dirHasFiles(dirPath) {
  try {
    return fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory() && fs.readdirSync(dirPath).length > 0;
  } catch (_) {
    return false;
  }
}

function copyDirRecursive(srcDir, destDir) {
  if (!fs.existsSync(srcDir)) return;
  fs.mkdirSync(destDir, { recursive: true });
  for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
    const srcPath = path.join(srcDir, entry.name);
    const destPath = path.join(destDir, entry.name);
    if (entry.isDirectory()) {
      copyDirRecursive(srcPath, destPath);
    } else if (entry.isFile()) {
      if (!fs.existsSync(destPath)) {
        fs.copyFileSync(srcPath, destPath);
      }
    }
  }
}

// Keep one stable profile path across renamed builds so gateways/devices/db configs are preserved.
try {
  const appDataRoot = process.env.APPDATA || app.getPath("appData");
  if (appDataRoot) {
    const targetUserData = path.join(appDataRoot, LEGACY_USER_DATA_DIR);
    // Auto-migrate configs from older app profile folders by merging missing files.
    for (const prevDirName of PREVIOUS_USER_DATA_DIRS) {
      const prevUserData = path.join(appDataRoot, prevDirName);
      if (dirHasFiles(prevUserData)) {
        copyDirRecursive(prevUserData, targetUserData);
      }
    }
    app.setPath("userData", targetUserData);
  }
} catch (_) {}

const BACKEND_HOST = process.env.TRUSTNODE_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.TRUSTNODE_BACKEND_PORT || "8000");
const BACKEND_HEALTH_PATH = "/api/health";
const UI_SOURCE_FILE = "ui-source.json";
let currentBackendHost = BACKEND_HOST;
let currentBackendPort = BACKEND_PORT;
let currentOverlayTheme = "dark";

// Pin the backend's writable data dir to a predictable per-user location so the
// portable / installed builds behave identically on a fresh Windows profile.
// IMPORTANT: existing installs already store data under ~/.trustnode_edge/data
// (the backend's historical default). If that dir exists and is populated we
// must keep using it — switching paths would orphan the historian DB, license
// state, gateway configs and dashboards.
function resolveBackendDataDir() {
  if (process.env.TRUSTNODE_DATA_DIR) return process.env.TRUSTNODE_DATA_DIR;
  const legacyDir = path.join(app.getPath("home"), ".trustnode_edge", "data");
  try {
    if (fs.existsSync(legacyDir)) {
      const entries = fs.readdirSync(legacyDir);
      if (entries.length > 0) return legacyDir;
    }
  } catch (_) {}
  if (process.platform === "win32") {
    const localAppData = process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || "", "AppData", "Local");
    if (localAppData) return path.join(localAppData, "TrustNode", "data");
  }
  return legacyDir;
}

function ensureBackendDataDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    return true;
  } catch (err) {
    logBackend(`Failed to create backend data dir ${dir}: ${String(err)}`);
    return false;
  }
}

function resolveOverlayPalette(mode) {
  const m = String(mode || "").toLowerCase() === "light" ? "light" : "dark";
  if (m === "light") {
    return { mode: m, color: "#fcfcfc", symbolColor: "#111111", height: 32 };
  }
  return { mode: m, color: "#111111", symbolColor: "#fcfcfc", height: 32 };
}

function applyOverlayTheme(modeOrPalette) {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const palette =
    modeOrPalette && typeof modeOrPalette === "object" && modeOrPalette.color
      ? {
          mode: String(modeOrPalette.mode || currentOverlayTheme || "dark"),
          color: String(modeOrPalette.color),
          symbolColor: String(modeOrPalette.symbolColor || "#fcfcfc"),
          height: Number.isFinite(Number(modeOrPalette.height)) ? Number(modeOrPalette.height) : 32
        }
      : resolveOverlayPalette(modeOrPalette || currentOverlayTheme);
  currentOverlayTheme = palette.mode === "light" ? "light" : "dark";
  try {
    mainWindow.setTitleBarOverlay({
      color: palette.color,
      symbolColor: palette.symbolColor,
      height: Math.max(28, Math.min(40, Number(palette.height) || 32))
    });
    return true;
  } catch (err) {
    logBackend(`Failed to apply overlay theme: ${String(err)}`);
    return false;
  }
}

function logBackend(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  backendLogs.push(line);
  if (backendLogs.length > 200) backendLogs.shift();
  try {
    const logFile = path.join(app.getPath("userData"), "backend.log");
    fs.appendFileSync(logFile, `${line}\n`);
  } catch (_) {}
}

function checkBackendHealth(host = currentBackendHost, port = currentBackendPort, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host,
        port,
        path: BACKEND_HEALTH_PATH,
        timeout: timeoutMs
      },
      (res) => {
        const ok = res.statusCode && res.statusCode >= 200 && res.statusCode < 300;
        res.resume();
        resolve(Boolean(ok));
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

function checkBackendHealthDetails(host = currentBackendHost, port = currentBackendPort, timeoutMs = 1800) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host,
        port,
        path: BACKEND_HEALTH_PATH,
        timeout: timeoutMs
      },
      (res) => {
        if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
          res.resume();
          resolve(null);
          return;
        }
        let body = "";
        res.on("data", (chunk) => {
          body += String(chunk);
        });
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (_) {
            resolve(null);
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
    req.on("error", () => resolve(null));
  });
}

function checkBackendOpenApiCompatibility(host, port, timeoutMs = 2200) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host,
        port,
        path: "/openapi.json",
        timeout: timeoutMs
      },
      (res) => {
        if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
          res.resume();
          resolve(false);
          return;
        }
        let body = "";
        res.on("data", (chunk) => {
          body += String(chunk);
        });
        res.on("end", () => {
          const hasCsv = body.includes('"csv_file"');
          const hasTxt = body.includes('"txt_file"');
          const hasDiscoverTags = body.includes('"/api/plc/discover-tags"');
          const hasGatewayStart = body.includes('"/api/plc/gateways/start"');
          const hasGatewayStopAll = body.includes('"/api/plc/gateways/stop-all"');
          resolve(Boolean(hasCsv && hasTxt && hasDiscoverTags && hasGatewayStart && hasGatewayStopAll));
        });
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

async function checkBackendCompatibility(host, port) {
  const details = await checkBackendHealthDetails(host, port, 1700);
  const caps = details?.capabilities || {};
  const healthCapsOk =
    details &&
    details.status === "ok" &&
    caps.database_active_sink === true &&
    caps.database_file_sinks === true &&
    caps.plc_discover_tags === true &&
    caps.plc_multi_gateway === true;
  if (healthCapsOk) return true;
  return await checkBackendOpenApiCompatibility(host, port, 2200);
}

function postStopAllGateways(host = currentBackendHost, port = currentBackendPort, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host,
        port,
        path: "/api/plc/gateways/stop-all",
        method: "POST",
        timeout: timeoutMs,
        headers: { "Content-Type": "application/json" }
      },
      (res) => {
        res.resume();
        resolve(Boolean(res.statusCode && res.statusCode >= 200 && res.statusCode < 300));
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
    req.end();
  });
}

function checkPortFree(host, port, timeoutMs = 1000) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    const done = (free) => {
      try {
        server.close();
      } catch (_) {}
      resolve(free);
    };
    server.on("error", () => done(false));
    server.listen({ host, port, exclusive: true }, () => done(true));
    setTimeout(() => done(false), timeoutMs);
  });
}

async function resolveBackendTarget() {
  const maxPortsToTry = 10;
  for (let i = 0; i < maxPortsToTry; i += 1) {
    const port = BACKEND_PORT + i;
    const alive = await checkBackendHealth(BACKEND_HOST, port, 1200);
    if (alive) {
      const compatible = await checkBackendCompatibility(BACKEND_HOST, port);
      if (compatible) {
        logBackend(`Backend already running at http://${BACKEND_HOST}:${port}; reusing compatible process.`);
        return { host: BACKEND_HOST, port, reuse: true };
      }
      logBackend(`Backend already running at http://${BACKEND_HOST}:${port} but not compatible; reserving this port.`);
      continue;
    }
    const free = await checkPortFree(BACKEND_HOST, port, 800);
    if (free) return { host: BACKEND_HOST, port, reuse: false };
  }
  return { host: BACKEND_HOST, port: BACKEND_PORT, reuse: false };
}

async function findCompatibleRunningBackend() {
  const maxPortsToTry = 10;
  for (let i = 0; i < maxPortsToTry; i += 1) {
    const port = BACKEND_PORT + i;
    const alive = await checkBackendHealth(BACKEND_HOST, port, 1000);
    if (!alive) continue;
    const compatible = await checkBackendCompatibility(BACKEND_HOST, port);
    if (compatible) return { host: BACKEND_HOST, port };
  }
  return null;
}

function killBackendImageNamesWindows() {
  if (process.platform !== "win32") return;
  const imageNames = ["trustnode-service.exe", "backend.exe"];
  for (const imageName of imageNames) {
    try {
      spawn("taskkill", ["/IM", imageName, "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore"
      });
    } catch (_) {}
  }
}

async function startBackend() {
  if (backendProc) return;

  const target = await resolveBackendTarget();
  currentBackendHost = target.host;
  currentBackendPort = target.port;

  if (target.reuse) {
    ownsBackendProcess = false;
    logBackend(`Reusing running backend at http://${currentBackendHost}:${currentBackendPort}`);
    try {
      await postStopAllGateways(currentBackendHost, currentBackendPort, 1500);
      logBackend("Sent stop-all to reused backend on startup.");
    } catch (_) {}
    return;
  }

  backendExited = false;
  backendExitCode = null;

  const dataDir = resolveBackendDataDir();
  ensureBackendDataDir(dataDir);
  // Load .env so TRUSTNODE_SUPABASE_*, TRUSTNODE_CLOUD_DB_*, etc. land
  // in the spawned backend's environment. Without this the Lite user
  // mirror (and the historian cloud sync, in cloud-only installs)
  // silently no-op because the env vars are missing. Search order:
  //   - TRUSTNODE_ENV_FILE if already set in process.env
  //   - %LOCALAPPDATA%/TrustNode/.env (operator-installable location)
  //   - next to the executable (portable/installer drop point)
  //   - source tree (dev: <repo>/Trustnode_edge_app/.env)
  const dotenvVars = {};
  const tried = new Set();
  const dotenvCandidates = [];
  if (process.env.TRUSTNODE_ENV_FILE) {
    dotenvCandidates.push(process.env.TRUSTNODE_ENV_FILE);
  }
  try {
    const localAppData = process.env.LOCALAPPDATA
      || path.join(process.env.USERPROFILE || "", "AppData", "Local");
    if (localAppData) {
      dotenvCandidates.push(path.join(localAppData, "TrustNode", ".env"));
    }
  } catch (_) {}
  try {
    const exeDir = path.dirname(process.execPath);
    dotenvCandidates.push(path.join(exeDir, ".env"));
    dotenvCandidates.push(path.join(exeDir, "..", ".env"));
    dotenvCandidates.push(path.join(exeDir, "..", "..", ".env"));
  } catch (_) {}
  try {
    // Dev tree: main.js sits in desktop/, .env lives one level up.
    dotenvCandidates.push(path.join(__dirname, "..", ".env"));
  } catch (_) {}
  for (const candidate of dotenvCandidates) {
    if (!candidate) continue;
    let resolved;
    try { resolved = path.resolve(candidate); } catch (_) { continue; }
    if (tried.has(resolved)) continue;
    tried.add(resolved);
    if (!fs.existsSync(resolved)) continue;
    try {
      const raw = fs.readFileSync(resolved, "utf-8");
      for (const line of raw.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
        const eq = trimmed.indexOf("=");
        const key = trimmed.slice(0, eq).trim();
        let value = trimmed.slice(eq + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'"))) {
          value = value.slice(1, -1);
        }
        if (key) dotenvVars[key] = value;
      }
      logBackend(`Loaded .env from ${resolved} (${Object.keys(dotenvVars).length} keys)`);
      break;
    } catch (err) {
      logBackend(`.env read failed at ${resolved}: ${err.message}`);
    }
  }
  // Real OS env wins over .env so operators can override per-launch.
  const sharedEnv = {
    ...dotenvVars,
    ...process.env,
    TRUSTNODE_HOST: currentBackendHost,
    TRUSTNODE_PORT: String(currentBackendPort),
    TRUSTNODE_DATA_DIR: dataDir
  };

  if (process.env.TRUSTNODE_BACKEND_CMD) {
    backendProc = spawn(process.env.TRUSTNODE_BACKEND_CMD, {
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      env: sharedEnv
    });
  } else if (app.isPackaged) {
    const backendExe = path.join(process.resourcesPath, "backend", BACKEND_EXE_NAME);
    if (!fs.existsSync(backendExe)) {
      logBackend(`Backend executable not found: ${backendExe}`);
      return;
    }
    backendProc = spawn(backendExe, [], {
      cwd: path.dirname(backendExe),
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: sharedEnv
    });
  } else {
    const backendCwd = path.resolve(__dirname, "../backend");
    backendProc = spawn("python", ["-m", "app"], {
      cwd: backendCwd,
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: sharedEnv
    });
  }
  ownsBackendProcess = true;

  backendProc.stdout?.on("data", (chunk) => {
    logBackend(`STDOUT ${String(chunk).trim()}`);
  });
  backendProc.stderr?.on("data", (chunk) => {
    logBackend(`STDERR ${String(chunk).trim()}`);
  });

  backendProc.on("exit", (code) => {
    backendExited = true;
    backendExitCode = code ?? null;
    logBackend(`Backend exited with code ${code ?? "null"}`);
    backendProc = null;
    if (code && code !== 0) {
      // Some packaged backends are singleton-style and exit non-zero when another
      // compatible instance already owns the local API port. Attach instead of fail.
      setTimeout(async () => {
        const running = await findCompatibleRunningBackend();
        if (!running) return;
        currentBackendHost = running.host;
        currentBackendPort = running.port;
        backendExited = false;
        backendExitCode = null;
        ownsBackendProcess = false;
        logBackend(`Attached to compatible running backend at http://${currentBackendHost}:${currentBackendPort} after local exit.`);
      }, 350);
    }
  });

  backendProc.on("error", (err) => {
    logBackend(`Backend process spawn error: ${String(err)}`);
  });
}

function stopBackend() {
  if (!backendProc || !ownsBackendProcess) return;
  try {
    if (process.platform === "win32" && backendProc.pid) {
      spawn("taskkill", ["/PID", String(backendProc.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore"
      });
    } else {
      backendProc.kill("SIGTERM");
    }
  } catch (_) {}
  backendProc = null;
  ownsBackendProcess = false;
}

function getUiSourceConfig() {
  const userConfigPath = path.join(app.getPath("userData"), UI_SOURCE_FILE);
  const bundledConfigPath = path.join(process.resourcesPath || __dirname, UI_SOURCE_FILE);
  const defaultConfig = {
    mode: "local",
    remoteUrl: "",
    localPath: ""
  };

  try {
    if (fs.existsSync(userConfigPath)) {
      const userConfig = JSON.parse(fs.readFileSync(userConfigPath, "utf8"));
      return { ...defaultConfig, ...userConfig };
    }
  } catch (err) {
    logBackend(`Invalid user UI config: ${err}`);
  }

  try {
    if (fs.existsSync(bundledConfigPath)) {
      const bundledConfig = JSON.parse(fs.readFileSync(bundledConfigPath, "utf8"));
      return { ...defaultConfig, ...bundledConfig };
    }
  } catch (err) {
    logBackend(`Invalid bundled UI config: ${err}`);
  }

  return defaultConfig;
}

function withBackendParam(url, backendUrl) {
  try {
    const parsed = new URL(url);
    if (!parsed.searchParams.get("backendUrl")) {
      parsed.searchParams.set("backendUrl", backendUrl);
    }
    return parsed.toString();
  } catch {
    return url;
  }
}

// ---------------------------------------------------------------------------
// Splash screen — shown the moment the user double-clicks the EXE, before
// the (slow) backend boot and the (large) frontend bundle load. Without
// this the user sees nothing for 5-20 s on a cold start, especially on
// SmartScreen-scanning fresh installs. The splash is a frameless 420×340
// window that:
//   * Displays the TrustNode logo + product name immediately.
//   * Shows a status line we update via IPC as the backend transitions
//     "Starting backend…" → "Waiting for service…" → "Loading UI…".
//   * Auto-closes the moment the main window's ready-to-show fires.
// ---------------------------------------------------------------------------
// Inline TrustNode wordmark SVG so the splash has no external asset
// dependency at all (the ICO read had silently failed on some packaged
// installs and the splash never opened). Coloured to match the
// in-app brand mark.
const SPLASH_LOGO_SVG = `
  <svg viewBox="0 0 64 64" width="64" height="64" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#14a89a"/>
        <stop offset="100%" stop-color="#0d5b54"/>
      </linearGradient>
    </defs>
    <rect x="6" y="6" width="52" height="52" rx="12" fill="url(#g)"/>
    <path d="M19 38 L29 28 L37 36 L46 22" stroke="#fff" stroke-width="3"
      stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    <circle cx="29" cy="28" r="2.6" fill="#fff"/>
    <circle cx="37" cy="36" r="2.6" fill="#fff"/>
    <circle cx="46" cy="22" r="2.6" fill="#fff"/>
  </svg>
`;

function buildSplashHtml() {
  const version = (() => { try { return app.getVersion(); } catch (_) { return "0.1.0"; } })();
  // SAFE escaping: no template substitution attacks possible here, but
  // keep braces literal in the CSS by not using ${...} for them.
  return `<!doctype html>
<html><head><meta charset="utf-8"/><title>TrustNode</title>
<style>
  * { box-sizing: border-box; -webkit-user-select: none; user-select: none; }
  html, body { margin: 0; padding: 0; height: 100%; width: 100%;
    font-family: 'Segoe UI', Roboto, sans-serif; color: #e9edf2;
    background: linear-gradient(160deg, #0e1a2b 0%, #14283f 65%, #0d1726 100%);
    overflow: hidden; }
  .stage { display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100%; padding: 24px 32px; gap: 14px;
    -webkit-app-region: drag; position: relative; }
  .logo { width: 96px; height: 96px; border-radius: 22px;
    background: rgba(255,255,255,0.04); display: flex; align-items: center;
    justify-content: center; box-shadow: 0 8px 28px rgba(0,0,0,0.4),
      inset 0 0 0 1px rgba(255,255,255,0.06); }
  .brand { font-size: 24px; font-weight: 700; letter-spacing: 0.04em;
    color: #ffffff; margin-top: 4px; }
  .tagline { font-size: 11px; color: #8aa0bd; margin-top: -4px;
    letter-spacing: 0.12em; text-transform: uppercase; }
  .status { font-size: 13px; color: #cfd8e6; margin-top: 4px;
    min-height: 18px; text-align: center; max-width: 360px; }
  .bar { width: 220px; height: 4px; border-radius: 999px;
    background: rgba(255,255,255,0.08); overflow: hidden; position: relative; }
  .bar::after { content: ""; position: absolute; left: -40%; top: 0;
    width: 40%; height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, transparent, #14a89a, transparent);
    animation: slide 1.4s ease-in-out infinite; }
  @keyframes slide { 0% { left: -40%; } 100% { left: 100%; } }
  .footer { position: absolute; bottom: 14px; left: 0; right: 0;
    text-align: center; font-size: 10px; color: #5b6d86;
    letter-spacing: 0.08em; }
</style>
</head><body>
  <div class="stage">
    <div class="logo">${SPLASH_LOGO_SVG}</div>
    <div class="brand">TrustNode</div>
    <div class="tagline">Industrial Edge</div>
    <div class="bar"></div>
    <div class="status" id="status">Starting up…</div>
    <div class="footer">v${version}</div>
  </div>
  <script>
    // Plain ipcRenderer would be unavailable under contextIsolation,
    // so the preload exposes window.electronAPI.onSplashStatus.
    function bind() {
      if (window.electronAPI && window.electronAPI.onSplashStatus) {
        window.electronAPI.onSplashStatus(function(msg) {
          var el = document.getElementById('status');
          if (el && typeof msg === 'string') el.textContent = msg;
        });
      } else {
        // contextIsolation may resolve a tick later — retry briefly.
        setTimeout(bind, 50);
      }
    }
    bind();
  </script>
</body></html>`;
}

function createSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) return;
  try {
    splashWindow = new BrowserWindow({
      width: 420,
      height: 340,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      frame: false,
      transparent: false,
      // Center on the primary display so the user can't miss it even
      // with multi-monitor setups.
      center: true,
      // SHOW IMMEDIATELY — previously we waited for ready-to-show,
      // which on some Windows installs never fired for data: URLs and
      // the splash never appeared. The backgroundColor below paints
      // the gradient base before HTML renders so the user sees the
      // window instantly even if the inline content races to load.
      show: true,
      alwaysOnTop: true,
      skipTaskbar: false,
      backgroundColor: "#0e1a2b",
      title: "TrustNode",
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        preload: path.join(__dirname, "preload.js"),
      },
    });
    splashWindow.setMenu(null);
    // After the splash is visible, drop alwaysOnTop so the user can
    // alt-tab away normally (e.g. if backend startup hangs and they
    // want to open another app).
    setTimeout(() => {
      try {
        if (splashWindow && !splashWindow.isDestroyed()) {
          splashWindow.setAlwaysOnTop(false);
        }
      } catch (_) {}
    }, 1500);
    splashWindow.loadURL(
      "data:text/html;charset=utf-8," + encodeURIComponent(buildSplashHtml())
    );
    splashWindow.on("closed", () => { splashWindow = null; });
    logBackend("Splash window created");
  } catch (err) {
    logBackend(`Splash window failed to create: ${err}`);
    splashWindow = null;
  }
}

function updateSplashStatus(message) {
  if (!splashWindow || splashWindow.isDestroyed() || !splashWindow.webContents) return;
  try {
    splashWindow.webContents.send("splash:status", String(message || ""));
  } catch (_) { /* ignored */ }
}

function closeSplash() {
  if (!splashWindow || splashWindow.isDestroyed()) {
    splashWindow = null;
    return;
  }
  try {
    splashWindow.close();
  } catch (_) {
    try { splashWindow.destroy(); } catch (__) {}
  }
  splashWindow = null;
}

function createWindow() {
  const windowIconPath = app.isPackaged
    ? path.join(process.resourcesPath, "trustnode_logo.ico")
    : path.resolve(__dirname, "assets", "trustnode_logo.ico");

  mainWindow = new BrowserWindow({
    title: APP_WINDOW_TITLE,
    width: 1280,
    height: 860,
    minWidth: 1024,
    minHeight: 680,
    frame: false,
    transparent: false,
    backgroundColor: "#101216",
    titleBarStyle: "hidden",
    titleBarOverlay: false,
    icon: windowIconPath,
    autoHideMenuBar: false,
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
      // `plugins: true` enables Chromium's built-in PDF viewer so report
      // previews (<iframe src="blob:..."> with application/pdf) render inline.
      // Without this flag Electron shows the raw JSON / 401 body instead.
      plugins: true,
      preload: path.join(__dirname, "preload.js")
    }
  });

  mainWindow.setMenuBarVisibility(true);

  const backendUrl = `http://${currentBackendHost}:${currentBackendPort}`;
  const uiConfig = getUiSourceConfig();
  const envUiUrl = process.env.TRUSTNODE_UI_URL || "";
  const remoteUiUrl = envUiUrl || (uiConfig.mode === "remote" ? uiConfig.remoteUrl : "");
  const externalUiPath = uiConfig.mode === "external" ? String(uiConfig.localPath || "").trim() : "";
  const localUiIndex = app.isPackaged
    ? path.join(process.resourcesPath, "frontend", "dist", "index.html")
    : path.join(__dirname, "..", "frontend", "dist", "index.html");
  const localDevUrl = `http://127.0.0.1:5173/?backendUrl=${encodeURIComponent(backendUrl)}`;
  const loadExternalUi = () => {
    if (!externalUiPath) return false;
    const externalIndex = path.join(externalUiPath, "index.html");
    if (fs.existsSync(externalIndex)) {
      logBackend(`Loading external UI from ${externalIndex}`);
      mainWindow.loadFile(externalIndex, { query: { backendUrl } });
      return true;
    }
    logBackend(`External UI not found: ${externalIndex}`);
    return false;
  };
  const loadLocalUi = () => {
    if (app.isPackaged) {
      if (fs.existsSync(localUiIndex)) {
        mainWindow.loadFile(localUiIndex, { query: { backendUrl } });
      } else {
        mainWindow.loadURL(
          "data:text/html;charset=utf-8," +
            encodeURIComponent(
              `<html><body style="font-family:Segoe UI;padding:24px"><h2>Frontend bundle not found</h2><p>Expected: ${localUiIndex}</p></body></html>`
            )
        );
      }
    } else {
      mainWindow.loadURL(localDevUrl);
    }
  };

  if (remoteUiUrl) {
    const target = withBackendParam(remoteUiUrl, backendUrl);
    mainWindow.loadURL(target);
    mainWindow.webContents.once("did-fail-load", () => {
      logBackend(`Remote UI failed to load (${target}), falling back to local UI.`);
      if (!loadExternalUi()) loadLocalUi();
    });
  } else if (uiConfig.mode === "external") {
    if (!loadExternalUi()) loadLocalUi();
  } else {
    loadLocalUi();
  }

  mainWindow.once("ready-to-show", () => {
    applyOverlayTheme(currentOverlayTheme);
    mainWindow.show();
    mainWindow.focus();
    // Splash served its purpose — close it the moment the real UI is
    // ready to paint. A small delay lets the OS finish swapping focus
    // so the user doesn't see a brief blank gap between the splash
    // disappearing and the main window appearing.
    setTimeout(() => closeSplash(), 150);
  });

  mainWindow.on("close", async (event) => {
    if (!app.isQuiting) {
      event.preventDefault();
      const result = await dialog.showMessageBox(mainWindow, {
        type: "question",
        buttons: ["Exit and stop all", "Minimize to tray"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
        title: APP_DISPLAY_NAME,
        message: `Close ${APP_DISPLAY_NAME}`,
        detail:
          "Choose 'Exit and stop all' to stop all gateways and backend processes. Choose 'Minimize to tray' to keep running."
      });
      if (result.response === 0) {
        await gracefulShutdownAndQuit();
      } else {
        mainWindow.hide();
      }
    }
  });

  ipcMain.removeAllListeners("window:minimize");
  ipcMain.removeAllListeners("window:maximize");
  ipcMain.removeAllListeners("window:close");
  ipcMain.removeAllListeners("window:is-maximized");
  ipcMain.removeAllListeners("window:sync-titlebar-theme");

  ipcMain.on("window:minimize", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.minimize();
  });
  ipcMain.on("window:maximize", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow.maximize();
    }
  });
  ipcMain.on("window:close", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.close();
  });
  ipcMain.handle("window:is-maximized", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    return mainWindow.isMaximized();
  });
  ipcMain.handle("window:sync-titlebar-theme", (_, payload = {}) => {
    const mode = String(payload?.mode || currentOverlayTheme || "dark").toLowerCase() === "light" ? "light" : "dark";
    return applyOverlayTheme(resolveOverlayPalette(mode));
  });

  ipcMain.removeHandler("dialog:pick-folder");
  ipcMain.handle("dialog:pick-folder", async (_event, options = {}) => {
    if (!mainWindow || mainWindow.isDestroyed()) return null;
    try {
      const defaultPath = String(options?.defaultPath || "").trim() || undefined;
      const result = await dialog.showOpenDialog(mainWindow, {
        title: String(options?.title || "Choose a folder"),
        properties: ["openDirectory", "createDirectory"],
        defaultPath,
        buttonLabel: String(options?.buttonLabel || "Select folder")
      });
      if (result.canceled) return null;
      const picked = Array.isArray(result.filePaths) ? result.filePaths[0] : null;
      return picked ? String(picked) : null;
    } catch (err) {
      logBackend(`Folder picker failed: ${String(err)}`);
      return null;
    }
  });
}

function monitorBackendStartup() {
  if (!app.isPackaged) return;
  // On slower or security-constrained machines the backend can take longer
  // to stabilize (or restart once) after process spawn. Keep polling first,
  // and only show an error page if it stays unhealthy for an extended period.
  const startedAt = Date.now();
  // First-launch onedir extraction + AV scanning on a fresh Windows machine
  // can take well over a minute. 90s gives the backend room to stabilize
  // before we paint a failure page.
  const maxWaitMs = 90000;
  const pollMs = 2000;
  const timer = setInterval(() => {
    if (!mainWindow || !mainWindow.webContents || mainWindow.isDestroyed()) {
      clearInterval(timer);
      return;
    }
    checkBackendHealth(currentBackendHost, currentBackendPort, 2000).then((alive) => {
      if (alive) {
        clearInterval(timer);
        return;
      }
      const elapsed = Date.now() - startedAt;
      if (elapsed < maxWaitMs) return;
      clearInterval(timer);
      if (!backendExited) {
        logBackend("Backend not healthy after extended startup grace period.");
      }
      const logPath = path.join(app.getPath("userData"), "backend.log");
      const details = backendLogs.slice(-30).join("\n");
      // Keep full diagnostics only in local log file; never expose traceback to end users.
      logBackend(
        `Backend unavailable after startup grace period (host=${currentBackendHost}:${currentBackendPort}, exitCode=${backendExitCode ?? "n/a"}). Recent logs:\n${details}`
      );
      const tailLines = backendLogs.slice(-15).join("\n");
      const safeTail = tailLines
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      const failurePage = `<html><body style="font-family:Segoe UI;padding:24px;color:#111827;background:#fff">
            <h2>TrustNode service did not start</h2>
            <p>The local backend at <code>http://${currentBackendHost}:${currentBackendPort}</code> is not responding.</p>
            <p><strong>Common causes on a fresh machine:</strong></p>
            <ul>
              <li>Windows SmartScreen or antivirus blocked <code>trustnode-service.exe</code>. Allow it and relaunch.</li>
              <li>First-launch extraction is slow — wait 30s and reopen the app.</li>
              <li>Another process is holding port ${currentBackendPort}.</li>
            </ul>
            <p>Full log: <code>${path.join(app.getPath("userData"), "backend.log").replace(/\\/g, "\\\\")}</code></p>
            <details><summary>Recent backend output</summary>
              <pre style="background:#f3f4f6;padding:12px;border-radius:6px;white-space:pre-wrap;font-size:12px">${safeTail || "(no output captured)"}</pre>
            </details>
          </body></html>`;
      mainWindow.loadURL(
        "data:text/html;charset=utf-8," + encodeURIComponent(failurePage)
      );
    });
  }, pollMs);
}

function startBackendSupervisor() {
  if (backendMonitorTimer) return;
  backendMonitorTimer = setInterval(async () => {
    const alive = await checkBackendHealth(currentBackendHost, currentBackendPort);
    if (alive) return;
    if (!backendProc) {
      logBackend("Backend health check failed; attempting restart.");
      startBackend();
    }
  }, 10000);
}

function stopBackendSupervisor() {
  if (!backendMonitorTimer) return;
  clearInterval(backendMonitorTimer);
  backendMonitorTimer = null;
}

async function gracefulShutdownAndQuit() {
  app.isQuiting = true;
  try {
    await postStopAllGateways();
  } catch (_) {}
  stopBackendSupervisor();
  stopBackend();
  killBackendImageNamesWindows();
  app.quit();
  // Safety fallback: force process exit if quit hooks are blocked.
  setTimeout(() => {
    try {
      app.exit(0);
    } catch (_) {}
  }, 1000);
}

function createTray() {
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, "trustnode_logo.ico")
    : path.resolve(__dirname, "../../trustnode_logo.png");
  const icon = nativeImage.createFromPath(iconPath);
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip(APP_DISPLAY_NAME);

  const menu = Menu.buildFromTemplate([
    {
      label: "Open",
      click: () => {
        if (mainWindow) mainWindow.show();
      }
    },
    {
      label: "Hide",
      click: () => {
        if (mainWindow) mainWindow.hide();
      }
    },
    {
      label: "Restart Backend",
      click: () => {
        stopBackend();
        startBackend();
      }
    },
    { type: "separator" },
    {
      label: "Exit",
      click: async () => {
        await gracefulShutdownAndQuit();
      }
    }
  ]);

  tray.setContextMenu(menu);
  tray.on("double-click", () => {
    if (mainWindow) mainWindow.show();
  });
}

app.whenReady().then(async () => {
  app.setName(APP_DISPLAY_NAME);
  if (process.platform === "win32") {
    // Keep the AppUserModelId aligned with electron-builder's appId so the
    // shortcut and the running process share an identity (otherwise Windows
    // shows the tray icon as a separate "Electron" app in the start menu).
    app.setAppUserModelId("com.trustnode.edge");
    // Avoid killing backend images on startup by default.
    // On some machines this creates startup races where UI loads before a stable API.
    const killOnStart = String(process.env.TRUSTNODE_KILL_STALE_BACKEND_ON_START || "0").toLowerCase();
    if (["1", "true", "yes", "on"].includes(killOnStart)) {
      killBackendImageNamesWindows();
    }
  }
  Menu.setApplicationMenu(null);
  // Show the splash IMMEDIATELY so the user gets feedback the moment
  // the EXE is launched. startBackend() can take 5–20 s on a fresh
  // SmartScreen-scanning machine; without a splash the user sees a
  // blank desktop and reasonably assumes nothing is happening.
  createSplashWindow();
  updateSplashStatus("Starting service…");
  await startBackend();
  updateSplashStatus("Service started. Waiting for health…");
  startBackendSupervisor();
  // Poll the backend health endpoint once so the splash transitions
  // to "Loading UI…" only after the backend is actually responsive.
  // 8 s ceiling so a slow backend doesn't hold the splash forever —
  // monitorBackendStartup keeps polling once the main window is up.
  const splashHealthDeadline = Date.now() + 8000;
  while (Date.now() < splashHealthDeadline) {
    try {
      const alive = await checkBackendHealth(
        currentBackendHost,
        currentBackendPort,
        1500,
      );
      if (alive) break;
    } catch (_) { /* keep polling */ }
    await new Promise((r) => setTimeout(r, 400));
  }
  updateSplashStatus("Loading interface…");
  createWindow();
  monitorBackendStartup();
  createTray();
});

app.on("second-instance", () => {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

app.on("before-quit", () => {
  app.isQuiting = true;
  // Ensure no gateway loop keeps writing if backend survives app close.
  try {
    postStopAllGateways();
  } catch (_) {}
  stopBackendSupervisor();
  stopBackend();
  killBackendImageNamesWindows();
});

app.on("window-all-closed", (event) => {
  if (!app.isQuiting) {
    event.preventDefault();
    return;
  }
});
