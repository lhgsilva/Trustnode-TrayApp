import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./styles/navigation.css";
import "./styles/buttons.css";
import "./styles/window-bar.css";
import "./styles.local.css";
import "./styles.portal.css";
import "./styles.client.css";
import "./styles/compact-tokens.css";

// Operator 2026-06-18 — LAN variant access gate.
// When the React app is served from /trustnode/{full|lite|client}/app/
// (i.e. by the local edge backend over the LAN), check access_<variant>
// before rendering. If the user lacks the permission, redirect to the
// shared login page with the variant + return path baked in.
// On the desktop tray (Electron / file://) the path doesn't start with
// /trustnode/ so we skip the gate entirely.
async function _enforceLanVariantAccess() {
  const p = String(window.location.pathname || "");
  const m = p.match(/^\/trustnode\/(full|lite|client)\/app\//);
  if (!m) return true;
  const variant = m[1];
  let jwt = "";
  try { jwt = localStorage.getItem("trustnode_auth_token") || ""; } catch (_) {}
  if (!jwt) {
    window.location.replace(
      `/trustnode/login/?variant=${variant}&return=${encodeURIComponent(p)}`
    );
    return false;
  }
  try {
    const res = await fetch("/api/lite-local/check-access", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${jwt}` },
      body: JSON.stringify({ variant }),
    });
    if (!res.ok) {
      window.location.replace(
        `/trustnode/login/?variant=${variant}&return=${encodeURIComponent(p)}`
      );
      return false;
    }
    return true;
  } catch (_) {
    // Network blip — allow render.
    return true;
  }
}

(async () => {
  const ok = await _enforceLanVariantAccess();
  if (!ok) return;
  createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
})();
