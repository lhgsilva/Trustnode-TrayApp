/**
 * A failed startup read must RECOVER on its own, without the operator
 * reloading the window.
 *
 * 2026-10-07, reported as three separate faults: "startup needing to
 * reload", "problems to load the templates", "problems with saving dashboard
 * profiles". All three were one thing - the bootstrap read ran exactly ONCE.
 * At boot the backend is still finishing deferred init against a
 * multi-gigabyte store, so that read often times out, and the app then held
 * React's empty initial state for the whole session. Worse, every saver is
 * (correctly) gated on "did a read succeed", so nothing could be saved
 * either: a dashboard profile looked saved and was gone on the next start.
 *
 * Source assertions cannot settle this - a retry that is present but
 * mis-wired reads the same. So: mount the real bundle in jsdom, make the
 * bootstrap endpoint FAIL the first few times, and watch whether the app
 * asks again.
 *
 *   node frontend/test_startup_recovers.mjs [dist-dir]
 *
 * Lives in frontend/ so `jsdom` resolves from frontend/node_modules - ESM
 * resolves dependencies from the SCRIPT location, not the cwd.
 *
 * Exit 0 = the app retried and recovered. Exit 2 = it needs a reload.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// Accepts an absolute path, so this can be pointed at the PACKAGED bundle
// (desktop/dist/win-unpacked/resources/frontend/dist) and not just the
// source build - "it works in dist" is not "it works in the exe".
const distArg = process.argv[2] || "dist";
const DIST = path.isAbsolute(distArg) ? distArg : path.join(HERE, distArg);

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 150)}` : ""}`);
  if (!ok) fails.push(name);
};

const indexPath = path.join(DIST, "index.html");
if (!fs.existsSync(indexPath)) {
  console.log(`  bundle not found at ${DIST} - run the frontend build first`);
  process.exit(2);
}
const indexHtml = fs.readFileSync(indexPath, "utf8");

console.log("TrustNode - does a failed startup read recover by itself?");
console.log();

// --- a backend that is down, then comes up --------------------------------
const BOOTSTRAP = "/api/app-store/bootstrap";
// Three. getAppStoreBootstrap() has its OWN retry loop (up to 3 fetches per
// logical read) and fetchWithTimeout retries a timed-out GET once, so the
// first few failures are absorbed BELOW this effect. Measured 2026-10-07:
// with the effect-level retry deleted the app stops after 3 reads (316ms,
// 321ms, 2826ms) and never recovers; with it, reads continue at 2.4s, 2.9s,
// 6.4s, 14.4s. Failing 3 therefore leaves recovery possible ONLY through the
// retry this test is about.
const FAIL_FIRST = 3;
let bootstrapCalls = 0;
let servedOkAt = 0;
const T0 = Date.now();
// WHEN each read was asked for. Counting calls alone proved nothing: several
// unrelated effects also read the bootstrap at mount, so the count passed
// even with the retry deleted. What is unique to the retry is a read that
// arrives LATE - after the backoff - with no reload and no user action.
const callTimes = [];
// EVERY fetch, not just the bootstrap. A retry effect that depends on a
// callback the parent rebuilds each render turns into an unthrottled loop:
// fail -> set an error -> parent re-renders -> new callback -> effect
// re-runs -> fetch again. That shipped on 2026-10-07, hammered the backend
// until the renderer ran out of its ~6 connections per host, and surfaced
// as "Failed to fetch" plus a visibly flickering page. Count them.
let totalFetches = 0;

const virtualConsole = new VirtualConsole();
const domErrors = [];
virtualConsole.on("jsdomError", (e) => domErrors.push(String(e?.message || e)));

const dom = new JSDOM(indexHtml, {
  url: "http://localhost/", runScripts: "outside-only",
  pretendToBeVisual: true, virtualConsole,
});
const { window } = dom;

const jsonRes = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: () => "application/json" },
  json: async () => body,
  text: async () => JSON.stringify(body),
  clone() { return this; },
});

window.fetch = async (url) => {
  const u = String(url?.url || url || "");
  totalFetches += 1;
  if (u.includes(BOOTSTRAP)) {
    bootstrapCalls += 1;
    callTimes.push(Date.now() - T0);
    if (bootstrapCalls <= FAIL_FIRST) {
      // What a timeout actually looks like to the caller.
      const err = new Error("The operation was aborted due to timeout");
      err.name = "TimeoutError";
      throw err;
    }
    if (!servedOkAt) servedOkAt = bootstrapCalls;
    return jsonRes({
      ok: true, tenant_id: "default", versions: { dashboard_configurations: 7 },
      data: {
        app_settings: {}, users_access: {},
        dashboard_configurations: { widgets: [], profiles: [{ id: "p1", name: "Line 1" }] },
      },
    });
  }
  if (u.includes("/api/health")) {
    return jsonRes({ ok: true, workspace: {}, health_snapshot: { state: "ready" } });
  }
  return jsonRes({ ok: true, rows: [], templates: [], items: [] });
};
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
window.WebSocket = class { constructor() { this.readyState = 3; } close() {} send() {} addEventListener() {} removeEventListener() {} };
window.EventSource = class { constructor() {} close() {} addEventListener() {} removeEventListener() {} };
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });

// Same execution path as smoke_frontend.mjs: jsdom cannot run
// <script type="module">, so concatenate the bundle and neutralise the one
// module-only construct it uses.
let code = "";
for (const src of [...window.document.querySelectorAll("script[src]")].map((n) => n.getAttribute("src"))) {
  const file = path.join(DIST, src.replace(/^[./]*/, ""));
  if (fs.existsSync(file)) code += fs.readFileSync(file, "utf8") + ";\n";
}
code = code.replace(/import\.meta\.url/g, JSON.stringify("http://localhost/assets/"));

let threw = null;
try { window.eval(code); } catch (e) { threw = e; }
check("the app bundle evaluated", !threw, threw ? String(threw?.message || threw) : "");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// First read happens on mount.
await sleep(500);
check("the app issued a bootstrap read on startup", bootstrapCalls >= 1,
      `${bootstrapCalls} call(s)`);

// The retry schedule is 2s, 4s, 8s... Wait long enough for two retries.
const deadline = Date.now() + 20000;
while (Date.now() < deadline && !servedOkAt) await sleep(250);
// Linger, so "it stopped asking" is measured rather than assumed.
await sleep(3000);

// The first backoff is 2s. A read arriving later than 1.5s cannot be one of
// the mount-time effects - it is the retry.
const lateReads = callTimes.filter((t) => t > 1500);
check("a read was re-issued AFTER the lower retry layers gave up",
      bootstrapCalls > FAIL_FIRST,
      `reads at ${callTimes.join("ms, ")}ms - without this fix it stops at ${FAIL_FIRST}`);
check("  and a later read SUCCEEDED, so the session recovered",
      servedOkAt > 0, servedOkAt ? `recovered on call ${servedOkAt}` : "never got a good read");
check("  the retry stopped once the configuration arrived",
      callTimes.filter((t) => t > (lateReads[lateReads.length - 1] || 0) + 16000).length === 0,
      "it must not keep polling the bootstrap for ever");

// --- nothing may hammer a failing backend --------------------------------
// Everything except the bootstrap answers 200 here, so a healthy app settles
// into polling. A loop shows up as an order-of-magnitude difference, not a
// borderline one: measured ~60-120 over this window when well behaved.
const elapsedS = (Date.now() - T0) / 1000;
const perSecond = totalFetches / elapsedS;
check("the app does not hammer the backend",
      perSecond < 25,
      `${totalFetches} fetches in ${elapsedS.toFixed(0)}s = ${perSecond.toFixed(1)}/s`);

// --- the source half: the save lock must still be a separate flag --------
const appSrc = fs.readFileSync(path.join(HERE, "src", "App.jsx"), "utf8");
check("hydration and the SAVE lock stay separate flags",
      appSrc.includes("appStorePayloadEverHydratedRef.current = true")
      && appSrc.includes("if (!appStorePayloadEverHydratedRef.current) return;"),
      "a retry must not license writing before a read has landed");
check("the dashboard save reports failure instead of swallowing it",
      !appSrc.includes("// Keep UI responsive; bootstrap save loop remains fallback persistence path.")
      && appSrc.includes("Dashboard could not be saved:"),
      "a silent failure is why a profile vanished overnight");
check("  and it sends AND tracks the document version",
      appSrc.includes('baseVersion: domainVersionsRef.current?.["dashboard_configurations"]')
      && appSrc.includes('...domainVersionsRef.current, "dashboard_configurations": _v'),
      "sending a version without tracking the response = one save per session");

// Neither may the export page: `api` is passed as an object and its loader
// keyed on it, so the pickers re-fetched on every App render and never
// settled ("not loading the data to be filters", 2026-10-08).
const expSrc = fs.readFileSync(
  path.join(HERE, "src", "components", "DataExport", "DataExportPage.jsx"), "utf8");
check("the export page loads its pickers once",
      expSrc.includes("apiRef.current.exportSources()")
      && !expSrc.includes("}, [api, patch]);"),
      "depending on an inline api object re-ran the load every render");
check("  and retries a failed read instead of staying blank",
      expSrc.includes("Math.min(15000, 2000 * Math.pow(2, attempt - 1))")
      && expSrc.includes("Try again"),
      "one failed attempt used to leave the filters empty all session");
const appStable = fs.readFileSync(path.join(HERE, "src", "App.jsx"), "utf8");
check("  the api object itself is stable",
      appStable.includes("const dataExportApi = useMemo(")
      && appStable.includes("api={dataExportApi}"),
      "fixing only the consumer leaves the next one to trip over it");

// The template retry must never take a per-render callback as a dependency.
const tplSrc = fs.readFileSync(
  path.join(HERE, "src", "components", "Reports", "ReportTemplateDesigner.jsx"), "utf8");
check("the template retry is not re-armed by every render",
      tplSrc.includes("await refreshRef.current?.()")
      && !tplSrc.includes("const got = await refreshTemplates();"),
      "depending on refreshTemplates (rebuilt whenever onNotify changes) is the loop");
check("  and a repeated failure raises ONE banner, not one per attempt",
      tplSrc.includes("if (notifiedErrRef.current !== msg) {"),
      "each banner re-renders the app, which is what the operator saw flicker");

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
