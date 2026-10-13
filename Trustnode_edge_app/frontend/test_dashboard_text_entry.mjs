/**
 * Can the operator actually TYPE into a dashboard widget's text fields?
 *
 * 2026-10-07, reported: "text field for the dashboard widgets text dont let
 * enter the texts". Source review could not settle it. A React input that is
 * controlled by `value={...}` but cannot accept a keystroke - a missing
 * onChange, a handler that writes to a different key, a parent that resets
 * state on every render - looks IDENTICAL in JSX to one that works. The only
 * honest test is to type into it and read the value back.
 *
 * This mounts the real DashboardDesigner (built by vite.designer.config.mjs)
 * in jsdom, opens Add widget, picks a type, and dispatches genuine input
 * events at every text field in the config form - the same event React
 * listens for. A field whose value does not change afterwards is frozen.
 *
 *   node frontend/test_dashboard_text_entry.mjs
 *
 * Lives in frontend/ so `jsdom` resolves from frontend/node_modules -
 * ESM resolves dependencies from the SCRIPT location, not the cwd (the
 * same reason smoke_frontend.mjs sits there).
 *
 * Exit 0 = every field accepted text. Exit 2 = at least one did not.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.join(HERE, "dist_designer", "designer.js");

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 150)}` : ""}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(BUNDLE)) {
  console.log(`  harness bundle missing at ${BUNDLE}`);
  console.log("  build it: npx vite build --config vite.designer.config.mjs (in frontend/)");
  process.exit(2);
}

const virtualConsole = new VirtualConsole();
const thrown = [];
virtualConsole.on("jsdomError", (e) => thrown.push(String(e?.message || e)));
virtualConsole.on("error", (...a) => thrown.push(a.map(String).join(" ")));

const dom = new JSDOM(
  `<!doctype html><html><body><div id="root"></div></body></html>`,
  { runScripts: "outside-only", pretendToBeVisual: true, virtualConsole, url: "http://localhost/" },
);
const { window } = dom;
window.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
window.localStorage.clear();
// Make the parent re-render while we type, the way the live Dashboard page
// does (tag values land about once a second). Set BEFORE the bundle runs.
window.__churn = true;

console.log("TrustNode - can you type into a widget's text fields?");
console.log();

try {
  window.eval(fs.readFileSync(BUNDLE, "utf8"));
} catch (err) {
  check("the designer harness mounted", false, String(err?.message || err));
  console.log(`\nRESULT: FAIL - ${fails.join(", ")}`);
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(300);

const doc = window.document;
check("the designer harness mounted", window.__mounted === true);
check("  it rendered without throwing", thrown.length === 0, thrown[0] || "");

/** React tracks its own value on the DOM node; set it the way a user would. */
function typeInto(el, text) {
  const proto = el instanceof window.HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, text);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}

const byText = (sel, text) => Array.from(doc.querySelectorAll(sel))
  .find((n) => (n.textContent || "").trim().toLowerCase() === String(text).toLowerCase());

// --- open the widget editor ----------------------------------------------
const addBtn = doc.querySelector(".dashboard-toolbar-icon-btn");
check("the Add widget button is present", Boolean(addBtn));
if (!addBtn) {
  console.log(`\nRESULT: FAIL - ${fails.join(", ")}`);
  process.exit(2);
}
addBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
await sleep(200);
check("  it opens the widget editor", Boolean(doc.querySelector(".modal-backdrop, .modal-card")));

// --- pick the Fixed Text type -------------------------------------------
// Expand collapsed categories. ONLY the group's own header button: a
// broader selector also matches the TYPE buttons inside each group, which
// selects a random widget and leaves the picker entirely (that cost me a
// false "the picker never offered it" the first time this ran).
for (const g of doc.querySelectorAll(".dashboard-type-group > button")) {
  const grid = g.parentElement.querySelector(".dashboard-type-grid");
  if (grid && grid.hidden) g.dispatchEvent(new window.Event("click", { bubbles: true }));
}
await sleep(200);

async function pickType(label) {
  const btn = byText(".dashboard-type-btn", label);
  if (!btn) return false;
  btn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await sleep(250);
  return true;
}

const picked = await pickType("Fixed Text");
check("the Fixed Text widget type is selectable", picked,
      picked ? "" : "the picker never offered it");

// --- every text field in the config form must accept text ---------------
/** Label text -> the input inside that label. */
function fieldsByLabel() {
  const out = [];
  for (const lab of doc.querySelectorAll(".modal-card label")) {
    const el = lab.querySelector("input, textarea");
    if (!el) continue;
    const t = String(el.getAttribute("type") || "text").toLowerCase();
    if (!["text", "textarea", "", "url", "search"].includes(t)) continue;
    if (el.disabled || el.readOnly) continue;
    // The label's own text, minus the control's value.
    const name = (lab.textContent || "").replace(el.value || "", "").trim();
    out.push({ name: name || "(unnamed)", el });
  }
  return out;
}

const fields = fieldsByLabel();
check("the config form exposes text fields", fields.length > 0, `${fields.length} found`);

let frozen = [];
for (const { name, el } of fields) {
  const probe = `probe-${Math.random().toString(36).slice(2, 7)}`;
  typeInto(el, probe);
  await sleep(120);
  // Re-read from the live DOM: React may have replaced the node.
  const again = Array.from(doc.querySelectorAll(".modal-card input, .modal-card textarea"))
    .find((n) => n === el) || el;
  if (String(again.value) !== probe) frozen.push(`${name} (held "${again.value}")`);
}
check("every text field accepted what was typed", frozen.length === 0,
      frozen.length ? frozen.join("; ") : `${fields.length} fields, all writable`);

// The Text field specifically - the one the operator named.
const textField = fields.find((f) => /^text$/i.test(f.name));
check("the Fixed Text widget's own Text field is present", Boolean(textField),
      textField ? "" : `labels seen: ${fields.map((f) => f.name).join(", ").slice(0, 120)}`);
if (textField) {
  typeInto(textField.el, "hello operator");
  await sleep(150);
  check("  and it holds the text that was typed",
        String(textField.el.value) === "hello operator",
        `value is "${textField.el.value}"`);
}

check("the parent really was re-rendering throughout",
      Number(window.__renders || 0) > 20,
      `${window.__renders} renders - with no churn this proves nothing`);

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
