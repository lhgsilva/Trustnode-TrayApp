const electron = require("electron");
const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, ipcMain } = electron;
const { spawn, execFileSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const { URL } = require("url");
const workspaceDetector = require("./workspace_detector");

// Set by detectAndChooseWorkspace() before backend spawn. Read by
// resolveBackendDataDir() (legacy auto-detect kept as fallback). Forces
// the backend to a known, user-chosen location.
let chosenWorkspaceDataDir = "";

// Phase 3d (operator 2026-06-18): initialize Sentry as early as possible
// so any uncaught error in main.js boot is captured. Opt-in via the
// TRUSTNODE_SENTRY_DSN env var (set by the installer or systemd unit
// on customer machines, NOT bundled in the EXE). Free tier suffices.
try {
  const dsn = String(process.env.TRUSTNODE_SENTRY_DSN || "").trim();
  if (dsn) {
    const Sentry = require("@sentry/electron/main");
    Sentry.init({
      dsn,
      release: process.env.TRUSTNODE_RELEASE_TAG || "edge-dev",
      environment: process.env.TRUSTNODE_SENTRY_ENV || "production",
      tracesSampleRate: 0.0,
      sendDefaultPii: false,
      // Only ship errors and fatals — preserve the 5K/month budget.
      beforeSend(event) {
        const lvl = String(event.level || "error").toLowerCase();
        if (lvl === "error" || lvl === "fatal") return event;
        return null;
      },
    });
  }
} catch (_) { /* never block boot on telemetry */ }

// ── Early boot-error logger (operator 2026-06-15: "the exe never
// even shows the splash on the other PC"). Anything that throws
// before the splash window opens lands in
// %LOCALAPPDATA%\TrustNode\boot-error.log so we have something to
// inspect on customer machines where the EXE silently exits.
const BOOT_LOG_DIR = (() => {
  try {
    const base = process.env.LOCALAPPDATA || os.homedir();
    const dir = path.join(base, "TrustNode");
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  } catch (_) { return os.tmpdir(); }
})();
const BOOT_LOG_PATH = path.join(BOOT_LOG_DIR, "boot-error.log");
function bootLog(line) {
  try {
    fs.appendFileSync(BOOT_LOG_PATH, `[${new Date().toISOString()}] ${line}\n`);
  } catch (_) { /* writing the log must never crash boot */ }
}
process.on("uncaughtException", (err) => {
  bootLog(`uncaughtException: ${err && err.stack || err}`);
});
process.on("unhandledRejection", (reason) => {
  bootLog(`unhandledRejection: ${reason && reason.stack || reason}`);
});
bootLog(`boot start v${(() => { try { return app.getVersion(); } catch { return "?"; } })()} pid=${process.pid}`);

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

// Sweep stale backend processes left over from a previous version's
// crash / unclean exit. Operator 2026-06-15: upgrading on top of an
// existing install left a zombie trustnode-service.exe holding the
// file lock, so the installer couldn't overwrite it and the new
// Electron shell couldn't bind to its port. Kill silently — if the
// process truly belongs to a running instance, the singleton lock
// below will pick that up. ONLY runs on Windows.
// Probe whether <port> is currently bound on 127.0.0.1. We use this
// after a kill sweep to make sure Windows fully released the socket
// before we spawn the new backend (Windows TIME_WAIT can keep the
// port "in use" for a few hundred ms after the owner exits).
function isPortBoundSync(port, host = "127.0.0.1", timeoutMs = 250) {
  return new Promise((resolve) => {
    const sk = new net.Socket();
    let done = false;
    const finish = (bound) => { if (done) return; done = true; try { sk.destroy(); } catch (_) {} resolve(bound); };
    sk.setTimeout(timeoutMs);
    sk.once("connect", () => finish(true));
    sk.once("timeout", () => finish(false));
    sk.once("error", () => finish(false));
    try { sk.connect(port, host); } catch (_) { finish(false); }
  });
}

// Find PIDs listening on <port> via netstat. Returns an array of
// numeric PIDs (deduped). Used to take down a zombie that taskkill
// /IM missed because the exe was renamed or running under a wrapper.
function findPidsListeningOnPort(port) {
  if (process.platform !== "win32") return [];
  try {
    const out = execFileSync("netstat.exe", ["-ano", "-p", "TCP"], {
      encoding: "utf8",
      timeout: 3000,
    });
    const pids = new Set();
    const portTok = `:${port}`;
    for (const line of out.split(/\r?\n/)) {
      if (!line.includes("LISTENING")) continue;
      if (!line.includes(portTok)) continue;
      const parts = line.trim().split(/\s+/);
      const pid = parseInt(parts[parts.length - 1], 10);
      if (Number.isFinite(pid) && pid > 0) pids.add(pid);
    }
    return Array.from(pids);
  } catch (_) { return []; }
}

function killPidsHard(pids) {
  if (process.platform !== "win32" || !pids.length) return;
  for (const pid of pids) {
    try {
      execFileSync("taskkill.exe", ["/F", "/T", "/PID", String(pid)], {
        stdio: ["ignore", "ignore", "ignore"],
        timeout: 3000,
      });
    } catch (_) {}
  }
}

function sweepStaleBackend() {
  if (process.platform !== "win32") return;
  // Operator 2026-06-18: kill ALL legacy backend image names. Older
  // builds shipped as "backend.exe"; the current name is
  // "trustnode-service.exe". A machine upgraded from <0.0.9 may have a
  // zombie of the old name still holding port 8000 and (worse) a write
  // lock on the legacy app_store SQLite — which would otherwise make the
  // freshly-spawned new backend hang on its first DB write.
  const legacyImageNames = [BACKEND_EXE_NAME, "backend.exe", "trustnode-edge.exe"];
  for (const imageName of legacyImageNames) {
    try {
      execFileSync("taskkill.exe", ["/F", "/T", "/IM", imageName], {
        stdio: ["ignore", "ignore", "ignore"],
        timeout: 4000,
      });
      bootLog(`stale backend swept: ${imageName}`);
    } catch (_) { /* nothing to kill is the happy path */ }
  }
}
try { sweepStaleBackend(); } catch (err) { bootLog(`sweep failed: ${err && err.message || err}`); }

const gotSingleInstanceLock = app.requestSingleInstanceLock();
bootLog(`singleton lock acquired=${gotSingleInstanceLock}`);

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
  // Operator 2026-06-18: the workspace detector (workspace_detector.js)
  // runs BEFORE the backend spawn and writes its choice here. When set,
  // it's the authoritative source — overrides every legacy auto-detect.
  if (chosenWorkspaceDataDir) return chosenWorkspaceDataDir;
  if (process.env.TRUSTNODE_DATA_DIR) return process.env.TRUSTNODE_DATA_DIR;
  // Fallback path retained so launches that bypass the detector (dev mode,
  // TRUSTNODE_BACKEND_CMD, harness scripts) still locate data sensibly.
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
  // Operator 2026-06-18 (revert of Phase 4a-1): the port-free-first order
  // turned out to misbehave on machines with a previous TrustNode install:
  // an older trustnode-service.exe leftover from a prior version could be
  // holding port 8000 in a half-dead state where checkPortFree said "busy"
  // but the old backend was no longer responding to /api/health and not
  // identifying as compatible — so the new tray would walk past it to
  // port 8001+, but then the old backend would die, then the new spawn at
  // 8001 would not be what the renderer tried to load. Worse, on the
  // FIRST iteration the brief gap between "old exited" and "new bound"
  // sometimes made the port appear free → new backend spawned at 8000 but
  // fought against the legacy app_store SQLite file lock.
  //
  // Going back to the original order (health-check first, then port-free)
  // gives the system a proper chance to identify and reuse a running
  // compatible backend. We pay an extra ~1.2s in the cold-boot case where
  // nothing is on the port (we noticed this and shipped 4a-1 to fix it),
  // but correctness on existing installs is non-negotiable. Phase 4a-2
  // and 4a-3 still buy us most of the boot speedup.
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
      // Synchronous so before-quit actually finishes the kill before
      // Electron tears the process down. Async spawn was racing the
      // event loop shutdown and leaving zombies behind.
      execFileSync("taskkill.exe", ["/IM", imageName, "/T", "/F"], {
        windowsHide: true,
        stdio: ["ignore", "ignore", "ignore"],
        timeout: 3000,
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

  // Pre-spawn port check: if the target port is bound by an unowned
  // process (e.g. a zombie trustnode-service.exe from a previous
  // crash, or a Python dev server left behind), clear it. We already
  // ran sweepStaleBackend() at boot, but that's name-based and misses
  // renamed/wrapped processes — netstat-by-port catches whatever is
  // actually holding the socket.
  if (process.platform === "win32") {
    const bound = await isPortBoundSync(currentBackendPort, currentBackendHost);
    if (bound) {
      const pids = findPidsListeningOnPort(currentBackendPort);
      logBackend(`Port ${currentBackendPort} busy at spawn; pids=${pids.join(",") || "?"}; killing`);
      killPidsHard(pids);
      // Wait up to 3s for the OS to actually release the socket.
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 100));
        if (!(await isPortBoundSync(currentBackendPort, currentBackendHost))) break;
      }
      const stillBound = await isPortBoundSync(currentBackendPort, currentBackendHost);
      if (stillBound) {
        logBackend(`Port ${currentBackendPort} still bound after kill — backend spawn will likely fail`);
        try {
          dialog.showMessageBox({
            type: "warning",
            title: "TrustNode port is busy",
            message: `Port ${currentBackendPort} is still in use after the cleanup attempt.`,
            detail:
              `Another process is holding port ${currentBackendPort} and we could not stop it (it may require admin rights).\n\n` +
              `PIDs detected: ${pids.join(", ") || "(unknown)"}\n\n` +
              "Open Task Manager → End Task on those PIDs, then relaunch TrustNode.",
          });
        } catch (_) {}
      } else {
        logBackend(`Port ${currentBackendPort} freed; proceeding with spawn`);
      }
    }
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
  const pid = backendProc.pid;
  try {
    if (process.platform === "win32" && pid) {
      // Synchronous so before-quit actually waits for the tree to die
      // (the async spawn() variant would race the process exit).
      try {
        execFileSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
          windowsHide: true,
          stdio: ["ignore", "ignore", "ignore"],
          timeout: 3000,
        });
      } catch (_) {
        // Fallback to async — better than nothing if execFileSync was blocked.
        try { spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }); } catch (_) {}
      }
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
// Inline TrustNode wordmark SVG kept as a hard fallback when the
// bundled brand PNG can't be read for any reason (packaged install
// missing the extraResource, dev run without assets present, etc.).
// Coloured to match the in-app brand mark.
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

function readSplashBrandDataUri() {
  // Use the official brand PNG that ships alongside the app icon.
  // Bundled via electron-builder extraResources (see package.json).
  try {
    const brandPath = app.isPackaged
      ? path.join(process.resourcesPath, "trustnode_brand.png")
      : path.resolve(__dirname, "assets", "trustnode_brand.png");
    if (!fs.existsSync(brandPath)) return "";
    const buf = fs.readFileSync(brandPath);
    return `data:image/png;base64,${buf.toString("base64")}`;
  } catch (_) {
    return "";
  }
}

function buildSplashHtml() {
  const version = (() => { try { return app.getVersion(); } catch (_) { return "0.1.0"; } })();
  const brandUri = readSplashBrandDataUri();
  // Use the real brand mark when available; fall back to the inline
  // SVG so the splash always shows something even if the resource
  // path resolution fails.
  const brandMark = brandUri
    ? `<img src="${brandUri}" alt="TrustNode" />`
    : SPLASH_LOGO_SVG;
  // SAFE escaping: no template substitution attacks possible here, but
  // keep braces literal in the CSS by not using ${...} for them.
  return `<!doctype html>
<html><head><meta charset="utf-8"/><title>TrustNode</title>
<style>
  * { box-sizing: border-box; -webkit-user-select: none; user-select: none; }
  html, body { margin: 0; padding: 0; height: 100%; width: 100%;
    font-family: 'Segoe UI', Roboto, sans-serif; color: #0f172a;
    background: linear-gradient(160deg, #f6f8fb 0%, #ffffff 60%, #e9f1f0 100%);
    overflow: hidden; }
  .stage { display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100%; padding: 24px 32px; gap: 18px;
    -webkit-app-region: drag; position: relative; }
  .logo { width: 240px; height: 140px; display: flex;
    align-items: center; justify-content: center; }
  .logo img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .logo svg { width: 120px; height: 120px; }
  .status { font-size: 12px; color: #475569; margin-top: 2px;
    min-height: 16px; text-align: center; max-width: 360px; }
  .bar { width: 220px; height: 3px; border-radius: 999px;
    background: rgba(15,23,42,0.08); overflow: hidden; position: relative; }
  .bar::after { content: ""; position: absolute; left: -40%; top: 0;
    width: 40%; height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, transparent, #14a89a, transparent);
    animation: slide 1.4s ease-in-out infinite; }
  @keyframes slide { 0% { left: -40%; } 100% { left: 100%; } }
  .footer { position: absolute; bottom: 12px; left: 0; right: 0;
    text-align: center; font-size: 10px; color: #94a3b8;
    letter-spacing: 0.08em; }
</style>
</head><body>
  <div class="stage">
    <div class="logo">${brandMark}</div>
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
  // Use the same .ico the main window uses so the Windows taskbar
  // shows the TrustNode brand during startup, not the default
  // Electron logo (operator 2026-06-15).
  const splashIconPath = app.isPackaged
    ? path.join(process.resourcesPath, "trustnode_logo.ico")
    : path.resolve(__dirname, "assets", "trustnode_logo.ico");
  try {
    splashWindow = new BrowserWindow({
      // Slightly wider to fit the wordmark brand logo without
      // squeezing the status text underneath.
      width: 480,
      height: 360,
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
      backgroundColor: "#f6f8fb",
      title: "TrustNode",
      icon: splashIconPath,
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
  // Operator 2026-06-18: renderer pushes auth state changes here. The
  // tray no longer TRUSTS the role field directly — it stores the JWT
  // and re-verifies via /api/auth/me on every right-click (Phase 3a).
  // This closes the spoof path where a compromised renderer could just
  // send {role:"admin"} to unlock LAN sharing.
  ipcMain.on("auth:role", async (_e, payload = {}) => {
    try {
      currentAuthToken = String(payload?.token || "");
      currentUsername  = String(payload?.username || "");
      // Best-effort initial verify — the right-click handler does the
      // authoritative check before showing the menu.
      await refreshAuthRole();
      try { rebuildTrayMenu(); } catch (_) {}
    } catch (_) {}
  });

  // Operator 2026-06-18: workspace inspection + reset for the
  // Settings page. The renderer NEVER touches disk directly; everything
  // routes through these IPCs so the file operations always run with
  // the tray's privileges (matters for ProgramData).
  ipcMain.removeHandler("workspace:detect");
  ipcMain.handle("workspace:detect", async () => {
    try {
      return { ok: true, workspaces: workspaceDetector.detectWorkspaces() };
    } catch (err) {
      return { ok: false, error: String(err && err.message || err) };
    }
  });
  ipcMain.removeHandler("workspace:current");
  ipcMain.handle("workspace:current", async () => {
    return { ok: true, dataDir: chosenWorkspaceDataDir || "" };
  });
  ipcMain.removeHandler("workspace:reset");
  ipcMain.handle("workspace:reset", async (_event, payload = {}) => {
    // Caller must echo the literal string "DELETE" so a renderer XSS or
    // an errant click can't wipe a workspace silently. Real confirmation
    // UI lives in the Settings page.
    if (String(payload?.confirm || "") !== "DELETE") {
      return { ok: false, error: "confirmation token missing" };
    }
    try {
      const userDataDir = app.getPath("userData");
      const result = workspaceDetector.resetCurrentWorkspace(userDataDir);
      // Force the tray to relaunch so the user picks a workspace fresh.
      if (result.ok) {
        setTimeout(() => {
          try { app.relaunch(); } catch (_) {}
          try { app.exit(0); } catch (_) {}
        }, 250);
      }
      return result;
    } catch (err) {
      return { ok: false, error: String(err && err.message || err) };
    }
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

// Operator 2026-06-18: tray remembers the renderer's auth state so the
// LAN Sharing submenu can be gated to admin/super only. The React app
// pushes updates via ipcMain "auth:role" on login/logout. Defaults to
// "" so the menu shows "(admin login required)" until the operator
// signs into the app.
let currentUserRole = "";
let currentUsername = "";
let currentAuthToken = "";
let lastAuthVerifyAt = 0;

// Phase 3a (operator 2026-06-18): server-verify the renderer-supplied
// JWT via /api/auth/me. Resets currentUserRole and currentUsername to
// whatever the BACKEND says — not what the renderer claims. Cheap
// enough to call on every right-click; the backend resolves the JWT
// against its own cp_users table.
async function refreshAuthRole() {
  if (!currentAuthToken) {
    currentUserRole = "";
    currentUsername = "";
    return;
  }
  try {
    const port = currentBackendPort || BACKEND_PORT || 8000;
    const url = `http://127.0.0.1:${port}/api/auth/me`;
    const fetchFn = global.fetch || (await import("node-fetch")).default;
    const res = await fetchFn(url, {
      method: "GET",
      headers: { Authorization: `Bearer ${currentAuthToken}` },
      timeout: 1500,
    });
    if (!res.ok) {
      // Invalid / expired token — clear cached state.
      currentUserRole = "";
      currentUsername = "";
      return;
    }
    const body = await res.json();
    const u = body?.user || body || {};
    currentUserRole = String(u?.role || "").toLowerCase();
    currentUsername = String(u?.username || "");
    lastAuthVerifyAt = Date.now();
  } catch (_) {
    // Network blip / backend just restarted. Don't blank the cached
    // role — better to keep the previous truthy state for ~5s than
    // flicker the menu disabled mid-right-click.
    if (Date.now() - lastAuthVerifyAt > 5000) {
      currentUserRole = "";
      currentUsername = "";
    }
  }
}

// Operator 2026-06-17 (M11): cached LAN sharing state so the tray
// menu can render the right submenu labels + the right URLs without
// re-fetching on every right-click.
let lanSharingState = {
  enabled: false,
  running: false,
  bind_host: "127.0.0.1",
  ips: [],
  lite_urls: [],
  port: 8000,
  primary_port: 8000,
  lan_port: 0,
  candidates_tried: [],
  note: "",
  last_error: "",
  restart_required: false,
};

async function refreshLanSharingState() {
  try {
    const fetch = global.fetch || (await import("node-fetch")).default;
    const res = await fetch(`http://127.0.0.1:${currentBackendPort || BACKEND_PORT || 8000}/api/lan-sharing/status`, { method: "GET" });
    if (res.ok) {
      const body = await res.json();
      // Merge — don't replace. Replacing dropped `candidates_tried`,
      // `last_error`, etc. when the status route omitted them, which
      // is why the error dialog said "Candidates tried: (none)".
      lanSharingState = Object.assign({}, lanSharingState, body || {});
    }
  } catch (err) {
    lanSharingState.last_error = `status fetch failed: ${err && err.message || err}`;
  }
}

async function postLanSharing(action) {
  const url = `http://127.0.0.1:${currentBackendPort || BACKEND_PORT || 8000}/api/lan-sharing/${action}`;
  try {
    const fetch = global.fetch || (await import("node-fetch")).default;
    const res = await fetch(url, { method: "POST" });
    const text = await res.text();
    let body = {};
    try { body = JSON.parse(text); } catch (_) { body = { _raw: text }; }
    if (!res.ok) {
      lanSharingState = Object.assign({}, lanSharingState, body || {}, {
        last_error: `HTTP ${res.status}: ${(body && (body.detail || body.note)) || text.slice(0, 200)}`,
      });
      return null;
    }
    lanSharingState = Object.assign({}, lanSharingState, body || {});
    // After enable, fetch fresh status so candidates_tried + last_error
    // reflect the server's current view (the POST body returns the
    // start() result, but status returns the LAN socket's authoritative
    // state including any post-start failure).
    if (action === "enable") {
      await refreshLanSharingState();
    }
    return body || {};
  } catch (err) {
    lanSharingState.last_error = `request failed: ${err && err.message || err} (${url})`;
    return null;
  }
}

function createTray() {
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, "trustnode_logo.ico")
    : path.resolve(__dirname, "../../trustnode_logo.png");
  const icon = nativeImage.createFromPath(iconPath);
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip(APP_DISPLAY_NAME);
  rebuildTrayMenu();
  tray.on("double-click", () => {
    if (mainWindow) mainWindow.show();
  });
  // Refresh sharing state when the menu is about to be shown so URLs /
  // IPs stay current.
  tray.on("right-click", async () => {
    // Operator 2026-06-18 (Phase 3a): server-verify the auth role on
    // EVERY right-click instead of trusting whatever the renderer
    // pushed last. A compromised renderer can't spoof admin past
    // this gate because we re-read from the backend each time.
    await Promise.all([refreshLanSharingState(), refreshAuthRole()]);
    rebuildTrayMenu();
    if (process.platform === "win32" && tray.popUpContextMenu) {
      try { tray.popUpContextMenu(); } catch (_) {}
    }
  });
  // First-time fetch so the menu is already populated.
  setTimeout(() => { refreshLanSharingState().then(rebuildTrayMenu); }, 2000);
}

function rebuildTrayMenu() {
  if (!tray) return;
  // Build a per-URL submenu so the operator can click to copy.
  const liteSubmenu = (lanSharingState.lite_urls || []).map((u) => ({
    label: u,
    click: () => {
      try {
        const { clipboard, shell } = require("electron");
        clipboard.writeText(u);
        // Soft confirm via shell.openExternal so the URL opens in a browser
        // — useful if the operator is testing locally.
        shell.openExternal(u).catch(() => {});
      } catch (_) {}
    }
  }));
  if (!liteSubmenu.length) {
    liteSubmenu.push({ label: "(no LAN IPs detected)", enabled: false });
  }
  const lanSubmenu = [
    {
      label: lanSharingState.running
        ? `LAN sharing: ON (port ${lanSharingState.lan_port || lanSharingState.port || 8000})`
        : (lanSharingState.enabled ? "LAN sharing: bind failed" : "LAN sharing: OFF"),
      enabled: false,
    },
    { type: "separator" },
    {
      label: "Turn ON",
      enabled: !lanSharingState.running,
      click: async () => {
        await postLanSharing("enable");
        // Give the backend ~300 ms to bind the second socket and
        // surface the new IPs in /status before we re-render the menu.
        await new Promise((r) => setTimeout(r, 300));
        await refreshLanSharingState();
        rebuildTrayMenu();
        if (!lanSharingState.running) {
          const { dialog: dlg } = require("electron");
          const tried = (lanSharingState.candidates_tried || []).join(", ");
          const err = lanSharingState.last_error || lanSharingState.note || "unknown error";
          dlg.showMessageBox({
            type: "warning",
            title: "LAN sharing failed to start",
            message: "LAN sharing could not bind to any candidate port.",
            detail:
              `Candidates tried: ${tried || "(none)"}\n\n` +
              `Backend reported: ${err}\n\n` +
              "Most common causes:\n" +
              "  • Another process is already listening on those ports — close it and try again.\n" +
              "  • Windows Firewall blocked the bind: allow this app on Private + Public networks.\n" +
              "  • Antivirus / endpoint protection is in the way.",
          });
        } else {
          // Print the actual URLs the operator should share.
          const port = lanSharingState.lan_port || lanSharingState.port;
          console.log(`LAN sharing live on port ${port}, URLs:`, lanSharingState.lite_urls);
        }
      }
    },
    {
      label: "Turn OFF",
      enabled: lanSharingState.running,
      click: async () => {
        await postLanSharing("disable");
        await new Promise((r) => setTimeout(r, 200));
        await refreshLanSharingState();
        rebuildTrayMenu();
      }
    },
    { type: "separator" },
    {
      label: "Lite URLs (click to copy + open):",
      enabled: false,
    },
    ...liteSubmenu,
  ];

  const menu = Menu.buildFromTemplate([
    {
      label: "Open",
      click: () => { if (mainWindow) mainWindow.show(); }
    },
    {
      label: "Hide",
      click: () => { if (mainWindow) mainWindow.hide(); }
    },
    { type: "separator" },
    // Operator 2026-06-18: LAN Sharing is admin-only. When no admin has
    // signed into the app yet, the item is shown but disabled with a
    // hint so the operator knows what to do.
    (() => {
      const isAdmin = currentUserRole === "admin" || currentUserRole === "super";
      if (isAdmin) {
        return { label: `LAN Sharing  (${currentUsername || "admin"})`, submenu: lanSubmenu };
      }
      return {
        label: currentUsername
          ? `LAN Sharing — admin only (signed in as ${currentUsername})`
          : "LAN Sharing — sign in as admin",
        enabled: false,
      };
    })(),
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
      click: async () => { await gracefulShutdownAndQuit(); }
    }
  ]);
  tray.setContextMenu(menu);
  // Update tooltip with the LAN status for at-a-glance state.
  const tip = lanSharingState.enabled
    ? `${APP_DISPLAY_NAME} | LAN sharing ON (${(lanSharingState.ips || []).join(", ") || "no IPs"})`
    : `${APP_DISPLAY_NAME} | LAN sharing OFF`;
  try { tray.setToolTip(tip); } catch (_) {}
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

  // Operator 2026-06-18: workspace detection runs BEFORE the splash so
  // the user makes the keep-existing-or-fresh choice on a clean dialog
  // (not over a splash). The dialog only shows on installs that find
  // existing data AND haven't been answered yet; subsequent launches go
  // straight through.
  try {
    const userDataDir = app.getPath("userData");
    const choice = await workspaceDetector.detectAndChooseWorkspace({
      userDataDir,
      electron,
    });
    if (choice === null) {
      // User picked "Cancel" — quit gracefully without spawning backend.
      bootLog("workspace detector: user cancelled at first-launch dialog");
      app.quit();
      return;
    }
    chosenWorkspaceDataDir = String(choice.dataDir || "");
    bootLog(
      `workspace detector: dataDir=${chosenWorkspaceDataDir} ` +
      `reason=${choice.reason} ` +
      `fresh=${choice.fresh} ` +
      `backup=${choice.backupPath || "(none)"} ` +
      `detected=${(choice.detectedPaths || []).length}`,
    );
  } catch (err) {
    // Detector must never block boot. On unexpected failure, fall back
    // to the legacy auto-detect path inside resolveBackendDataDir().
    bootLog(`workspace detector failed (continuing with legacy auto-detect): ${String(err && err.message || err)}`);
  }

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
  // Ceiling raised to 12 s (from 8 s) to cover AV-heavy machines;
  // monitorBackendStartup keeps polling once the main window is up.
  // Per-attempt timeout cut to 500 ms (loopback ECONNREFUSED arrives
  // in <1 ms, so 1500 ms was pure wasted wall-clock). Inter-poll sleep
  // cut to 100 ms so a backend that starts in ~3 s is detected within
  // ~3.6 s instead of up to ~5.9 s with the old 400 ms cadence.
  const splashHealthDeadline = Date.now() + 12000;
  while (Date.now() < splashHealthDeadline) {
    try {
      const alive = await checkBackendHealth(
        currentBackendHost,
        currentBackendPort,
        500,
      );
      if (alive) break;
    } catch (_) { /* keep polling */ }
    await new Promise((r) => setTimeout(r, 100));
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
