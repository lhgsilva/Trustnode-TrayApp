const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("windowControls", {
  minimize: () => ipcRenderer.send("window:minimize"),
  maximize: () => ipcRenderer.send("window:maximize"),
  close: () => ipcRenderer.send("window:close"),
  isMaximized: () => ipcRenderer.invoke("window:is-maximized"),
  syncTitlebarTheme: (payload) => ipcRenderer.invoke("window:sync-titlebar-theme", payload)
});

// Native folder picker. Resolves to the chosen folder path or null when the
// user cancels — callers should preserve the existing field on null.
contextBridge.exposeInMainWorld("trustnodeDialogs", {
  pickFolder: (options) => ipcRenderer.invoke("dialog:pick-folder", options || {})
});

// Operator 2026-06-18: workspace inspection + reset for the Settings
// page. The renderer never touches disk directly; everything routes
// through these IPCs so file ops always run with the tray's privileges.
// resetWorkspace requires {confirm: "DELETE"} or the main process refuses.
contextBridge.exposeInMainWorld("trustnodeWorkspace", {
  detect: () => ipcRenderer.invoke("workspace:detect"),
  current: () => ipcRenderer.invoke("workspace:current"),
  reset: (confirm) => ipcRenderer.invoke("workspace:reset", { confirm })
});

// Splash screen bridge. The splash window's inline HTML calls
// window.electronAPI.onSplashStatus to subscribe; the main UI never
// receives `splash:status` events. Exposed via the same preload because
// contextIsolation=true on both windows demands a context bridge for
// every renderer→main IPC.
contextBridge.exposeInMainWorld("electronAPI", {
  onSplashStatus: (cb) => {
    if (typeof cb !== "function") return () => {};
    const listener = (_event, msg) => cb(msg);
    ipcRenderer.on("splash:status", listener);
    return () => ipcRenderer.removeListener("splash:status", listener);
  },
  onSplashFailures: (cb) => {
    if (typeof cb !== "function") return () => {};
    const listener = (_event, payload) => cb(payload);
    ipcRenderer.on("splash:failures", listener);
    return () => ipcRenderer.removeListener("splash:failures", listener);
  },
  splashRetry: () => ipcRenderer.send("splash:retry"),
  splashSkip: () => ipcRenderer.send("splash:skip"),
  // Operator 2026-06-18: renderer pushes the signed-in user's role so
  // the tray can gate the LAN Sharing submenu to admin/super only.
  setAuthRole: (payload) => ipcRenderer.send("auth:role", payload || {})
});
