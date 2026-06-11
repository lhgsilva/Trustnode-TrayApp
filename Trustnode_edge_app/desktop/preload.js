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
  }
});
