// LS-Link interface shell.
//
// 2026-10-28. The window shows what the core says and asks the core to do
// things - it holds no state of its own. The core writes its port and a
// per-run token to %APPDATA%\TrustNodeLink\api.json; without that file
// there is nothing to talk to, and the window says so rather than
// pretending to be connected to something.
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const fs = require("fs");
const path = require("path");
const os = require("os");

// 2026-10-28, reported: "the browser version is working but the electron
// app is not". The browser was opened AT the live port; this window looked
// the endpoint up in a file - and a second LS-Link rewrites that file, so
// the window talked to a core that had gone. The core now hands its port
// and token to the window directly, and the file is only the fallback.
function endpoint() {
  const port = Number(process.env.TNLINK_PORT || 0);
  const token = process.env.TNLINK_TOKEN || "";
  if (port && token) return { port, token };
  const p = path.join(process.env.APPDATA || os.homedir(),
                      "TrustNodeLink", "api.json");
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch (_) { return null; }
}

async function callCore(route, method, body) {
  const ep = endpoint();
  if (!ep) throw new Error("LS-Link is not running");
  try {
    const res = await fetch(`http://127.0.0.1:${ep.port}${route}`, {
      method: method || "GET",
      headers: { "X-TNLink-Token": ep.token, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`core on 127.0.0.1:${ep.port} returned ${res.status}`);
    return res.json();
  } catch (err) {
    // Name the host and the route. A dead core and a refused request look
    // identical in a screenshot otherwise.
    console.error(`[lslink] ${method || "GET"} ${route} -> ${err.message}`);
    throw err;
  }
}

let win = null;
function createWindow() {
  const iconFile = path.join(__dirname, "lslink.png");   // rendered by the build from the tray icon
  win = new BrowserWindow({
    width: 1040, height: 720, minWidth: 880, minHeight: 600,
    title: "LS-Link",
    ...(require("fs").existsSync(iconFile) ? { icon: iconFile } : {}),
    backgroundColor: "#0f1115",
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"),
                      contextIsolation: true, nodeIntegration: false },
  });
  // Whatever the page logs comes out on stdout, so a window that renders
  // but shows nothing can be diagnosed from the log instead of guessed at.
  win.webContents.on("console-message", (_e, level, message) => {
    if (level >= 2) console.error(`[page] ${message}`);
  });
  win.webContents.on("did-fail-load", (_e, code, desc) => {
    console.error(`[lslink] page failed to load: ${desc} (${code})`);
  });
  win.loadFile(path.join(__dirname, "index.html"));
  // Closing the WINDOW must not stop the service: the plant PC keeps the
  // session alive and the tray stays in charge. Quitting is a tray action.
  win.on("closed", () => { win = null; });
}

ipcMain.handle("core:state", () => callCore("/state"));
ipcMain.handle("core:logs", () => callCore("/logs"));
ipcMain.handle("core:do", (_e, name, body) => callCore(`/do/${name}`, "POST", body || {}));
ipcMain.handle("core:alive", () => Boolean(endpoint()));

app.whenReady().then(createWindow);
app.on("window-all-closed", () => { app.quit(); });   // the SERVICE lives on in the tray
