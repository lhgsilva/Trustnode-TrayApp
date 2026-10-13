/**
 * A failing templates read must RETRY, not LOOP.
 *
 * 2026-10-07. I shipped a retry for "problems to load the templates" whose
 * effect depended on `refreshTemplates`. That callback is rebuilt whenever
 * `onNotify` changes, and App.jsx passes onNotify as an inline arrow - a new
 * identity on every render. So: fetch fails -> onNotify sets a banner ->
 * App re-renders -> new onNotify -> new refreshTemplates -> the effect tears
 * down and re-runs -> fetch immediately -> fails...
 *
 * The operator saw it within hours: a flickering page and "Failed to load
 * templates: Failed to fetch". The second half is the giveaway - "Failed to
 * fetch" is not a timeout, it is the renderer running out of its ~6
 * connections per host, which this repo already documents in
 * backend/app/__main__.py.
 *
 * Source assertions alone would not have caught it: the retry LOOKED right.
 * This mounts the real component with a parent that behaves exactly like
 * App.jsx, fails every templates read, and counts the fetches.
 *
 *   node frontend/test_reports_no_fetch_loop.mjs
 *
 * Exit 0 = backed off. Exit 2 = it is looping.
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

console.log("TrustNode - a failing templates read must back off, not loop");
console.log();

let templateFetches = 0;
const times = [];
const T0 = Date.now();

const virtualConsole = new VirtualConsole();
const dom = new JSDOM(`<!doctype html><html><body><div id="root"></div></body></html>`, {
  runScripts: "outside-only", pretendToBeVisual: true, virtualConsole, url: "http://localhost/",
});
const { window } = dom;

window.__harness = "reports";
// The real parent re-renders many times a second; that is what a
// callback-dependent effect latches onto. Must be set BEFORE the bundle runs.
window.__churn = true;
window.fetch = async (url) => {
  const u = String(url?.url || url || "");
  if (u.includes("/api/reports/templates")) {
    templateFetches += 1;
    times.push(Date.now() - T0);
    // What the operator actually saw. Not a timeout - a dead connection.
    throw new TypeError("Failed to fetch");
  }
  return {
    ok: true, status: 200, headers: { get: () => "application/json" },
    json: async () => ({ ok: true, templates: [], rows: [] }),
    text: async () => "{}", clone() { return this; },
  };
};
window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
window.ResizeObserver = window.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
window.scrollTo = () => {};
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });

let threw = null;
try { window.eval(fs.readFileSync(BUNDLE, "utf8")); } catch (e) { threw = e; }
check("the reports designer mounted", !threw && window.__mounted === true,
      threw ? String(threw?.message || threw) : "");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(9000);

const elapsedS = (Date.now() - T0) / 1000;
check("it tried to load the templates", templateFetches >= 1, `${templateFetches} attempt(s)`);

// The backoff is 2s, 4s, 8s... so ~3 attempts in 9 seconds. The loop made
// hundreds. The bound is deliberately loose - this separates orders of
// magnitude, not neighbouring values.
check("it BACKS OFF instead of looping",
      templateFetches <= 8,
      `${templateFetches} attempts in ${elapsedS.toFixed(0)}s at ${times.join("ms, ")}ms`);

check("  and it keeps trying, so a late backend still recovers",
      templateFetches >= 2,
      "one attempt and then silence is the bug this retry was added for");

// A banner per attempt re-renders the whole app - that is the flicker.
check("  the retry did not follow the parent's render rate",
      Number(window.__renders || 0) > 20
      && templateFetches < Number(window.__renders || 0) / 4,
      `${window.__renders} parent renders vs ${templateFetches} attempts - `
      + "the bug made these track each other");

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
