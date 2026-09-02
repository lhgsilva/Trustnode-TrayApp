/**
 * Mount the BUILT frontend in a headless DOM and fail on any error.
 *
 * 2026-08-27. A shipped build died on load with "Cannot access 'Ue' before
 * initialization" and every check I had still passed - because they all
 * verified that code was PRESENT, never that the app RUNS. `vite build` exits
 * 0 with a temporal-dead-zone fault in place; I proved that by rebuilding with
 * the bug restored.
 *
 * This executes the real bundle: jsdom, then the app's own entry point. It
 * catches render-time ReferenceErrors, bad imports, and anything else that
 * throws before or during first paint - the whole class of fault that reached
 * the operator as a white screen.
 *
 * It does NOT need a backend. Network calls are stubbed and their failures are
 * ignored on purpose: "the API is unreachable" is not what we are testing, and
 * the app is expected to survive it.
 *
 *   node scripts/smoke_frontend.mjs [dist-dir]
 *
 * Exit 0 = the app mounted and rendered. Exit 2 = it did not.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

// Lives in frontend/ so that `jsdom` resolves from frontend/node_modules -
// ESM resolves dependencies from the SCRIPT location, not the cwd.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = HERE;
const distArg = process.argv[2] || "dist";
const DIST = path.isAbsolute(distArg) ? distArg : path.join(ROOT, distArg);

const fails = [];
const check = (name, ok, detail = "") => {
  const mark = ok ? "PASS" : "FAIL";
  const extra = detail ? ` - ${String(detail).slice(0, 160)}` : "";
  console.log(`  ${name.padEnd(56)}: ${mark}${extra}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(DIST)) {
  console.log(`  bundle not found at ${DIST} - run the frontend build first`);
  process.exit(2);
}

// --- collect everything the page throws or logs ---------------------------
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => errors.push(e));
virtualConsole.on("error", (...args) => errors.push(new Error(args.join(" "))));

const indexHtml = fs.readFileSync(path.join(DIST, "index.html"), "utf8");

const dom = new JSDOM(indexHtml, {
  url: "http://localhost/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;

// Minimal browser surface the app expects. Anything missing here would be a
// jsdom gap, not an app defect, so these are stubs rather than assertions.
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, onchange: null,
  addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
}));
window.ResizeObserver = window.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
window.IntersectionObserver = window.IntersectionObserver || class {
  observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
};
window.scrollTo = window.scrollTo || (() => {});
window.HTMLCanvasElement.prototype.getContext = () => null;
// The backend is not running and is not what this tests.
window.fetch = () => Promise.reject(new Error("offline (expected in smoke test)"));
window.WebSocket = class { constructor() { this.readyState = 3; } close() {} send() {} addEventListener() {} removeEventListener() {} };
window.EventSource = class { constructor() {} close() {} addEventListener() {} removeEventListener() {} };

// Unhandled rejections from those stubbed calls are expected noise.
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });

// --- run the app's own bundle ---------------------------------------------
const scripts = [...window.document.querySelectorAll("script[src]")].map((s) => s.getAttribute("src"));
const inline = [...window.document.querySelectorAll("script:not([src])")].map((s) => s.textContent || "");
let code = "";
for (const src of scripts) {
  const file = path.join(DIST, src.replace(/^[./]*/, "").replace(/^assets\//, "assets/"));
  if (fs.existsSync(file)) code += fs.readFileSync(file, "utf8") + "\n";
}
code += inline.join("\n");

check("the built bundle was located", code.length > 1000, `${(code.length / 1024).toFixed(0)} KB`);

// jsdom cannot run <script type="module">, and the bundle is ESM. It uses
// exactly one module-only construct, so neutralise that and evaluate it as a
// classic script. This changes nothing the app depends on - import.meta.url is
// only ever used to derive an asset base URL.
code = code.replace(/import\.meta\.url/g, JSON.stringify("http://localhost/assets/"));

let threw = null;
try {
  window.eval(code);
} catch (e) {
  threw = e;
}

// React renders synchronously on mount, but effects and lazy paths settle on
// the next ticks - give them a moment before judging.
await new Promise((r) => setTimeout(r, 1500));

const msg = (e) => String((e && (e.detail?.message || e.message)) || e || "");
const fatal = errors
  .map(msg)
  .filter((m) => m && !/offline \(expected in smoke test\)/i.test(m));

console.log("\n[the app mounts and renders]");
check("the bundle evaluated without throwing", !threw, threw ? msg(threw) : "");

// The exact class of fault that shipped: a const read inside its temporal
// dead zone. Minification renames it, so match the shape, not the name.
const tdz = [threw ? msg(threw) : "", ...fatal]
  .filter((m) => /before initialization|is not defined|Cannot access/i.test(m));
check("no temporal-dead-zone / undefined-binding error", tdz.length === 0, tdz[0] || "");

check("nothing else threw during first render", fatal.length === 0, fatal.slice(0, 2).join(" | "));

const root = window.document.getElementById("root") || window.document.body;
const html = root.innerHTML || "";
// 2026-08-31: this threshold was 200, and a patch that accidentally moved
// `export default` onto a 12-line helper component still passed - the app
// rendered ONE BUTTON (284 chars) and the gate called it a success. The real
// shell is ~2 000 characters, so anything under 1 200 means the app did not
// render, whatever else evaluated cleanly.
check("something was actually rendered into #root", html.length > 1200,
  `${html.length} chars` + (html.length <= 1200
    ? ` — expected the app shell (~2 000). Rendered: ${html.slice(0, 160)}`
    : ""));

// The app's own error boundary renders this when a render throws. If it is on
// screen the app "loaded" but the operator sees a dead page - the exact
// symptom that was reported.
const boundary = /Frontend Error Recovered|Clear UI Cache/i.test(html);
check("the error-boundary fallback is NOT showing", !boundary,
  boundary ? "the app caught a render error and showed the recovery screen" : "");

console.log(`\nRESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
