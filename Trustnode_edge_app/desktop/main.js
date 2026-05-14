const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");
const { URL } = require("url");

let mainWindow = null;
let tray = null;
let backendProc = null;
let backendExited = false;
let backendExitCode = null;
const backendLogs = [];
let ownsBackendProcess = false;
let backendMonitorTimer = null;
const APP_DISPLAY_NAME = "Trustnode";
const APP_WINDOW_TITLE = "Trustnode";
const BACKEND_EXE_NAME = "trustnode-service.exe";
const LEGACY_USER_DATA_DIR = "trustnode-edge-desktop";
const PREVIOUS_USER_DATA_DIRS = ["trustnode-desktop"];
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

  if (process.env.TRUSTNODE_BACKEND_CMD) {
    backendProc = spawn(process.env.TRUSTNODE_BACKEND_CMD, {
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      env: {
        ...process.env,
        TRUSTNODE_HOST: currentBackendHost,
        TRUSTNODE_PORT: String(currentBackendPort)
      }
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
      env: {
        ...process.env,
        TRUSTNODE_HOST: currentBackendHost,
        TRUSTNODE_PORT: String(currentBackendPort)
      }
    });
  } else {
    const backendCwd = path.resolve(__dirname, "../backend");
    backendProc = spawn("python", ["-m", "app"], {
      cwd: backendCwd,
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        TRUSTNODE_HOST: currentBackendHost,
        TRUSTNODE_PORT: String(currentBackendPort)
      }
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
}

function monitorBackendStartup() {
  if (!app.isPackaged) return;
  // On slower or security-constrained machines the backend can take longer
  // to stabilize (or restart once) after process spawn. Keep polling first,
  // and only show an error page if it stays unhealthy for an extended period.
  const startedAt = Date.now();
  const maxWaitMs = 35000;
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
      mainWindow.loadURL(
        "data:text/html;charset=utf-8," +
          encodeURIComponent(
            `<html><body style="font-family:Segoe UI;padding:24px;color:#111827;background:#fff">
              <h2>Service temporarily unavailable</h2>
              <p>TrustNode service is restarting. Please wait a few seconds and try again.</p>
              <p>If this persists, restart the TrustNode application.</p>
            </body></html>`
          )
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
    app.setAppUserModelId("com.trustnode.app");
    // Avoid killing backend images on startup by default.
    // On some machines this creates startup races where UI loads before a stable API.
    const killOnStart = String(process.env.TRUSTNODE_KILL_STALE_BACKEND_ON_START || "0").toLowerCase();
    if (["1", "true", "yes", "on"].includes(killOnStart)) {
      killBackendImageNamesWindows();
    }
  }
  Menu.setApplicationMenu(null);
  await startBackend();
  startBackendSupervisor();
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
