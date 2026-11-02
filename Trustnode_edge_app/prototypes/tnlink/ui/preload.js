const { contextBridge, ipcRenderer } = require("electron");
// A narrow bridge: the page can read state, read logs and name an action.
// It cannot reach the filesystem or the network directly.
contextBridge.exposeInMainWorld("core", {
  state: () => ipcRenderer.invoke("core:state"),
  logs: () => ipcRenderer.invoke("core:logs"),
  run: (name, body) => ipcRenderer.invoke("core:do", name, body),
  alive: () => ipcRenderer.invoke("core:alive"),
});
