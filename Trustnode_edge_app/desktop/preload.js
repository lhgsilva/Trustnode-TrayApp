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
