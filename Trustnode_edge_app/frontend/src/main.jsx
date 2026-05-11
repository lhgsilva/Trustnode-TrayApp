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

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
