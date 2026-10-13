/**
 * Opening the app with a session that died overnight must show the SIGN-IN
 * screen - not a dashboard behind two red banners.
 *
 * 2026-10-09, reported with a screenshot: every launch opened on
 *   "Your session has ended. ... Sign in again to carry on."
 *   "Configuration not loaded. The edge did not return your stored setup
 *    (Session expired. Please sign in again.) ..."
 * over a rendering dashboard, signed in as admin-lucas.
 *
 * A token lives 12 h and the server renews it for 30 min past expiry, so an
 * app closed overnight ALWAYS wakes up past saving. The app restored the
 * user from that dead token anyway, then short-circuited every request -
 * nominally signed in, nothing working. That is not an incident to
 * announce; it is "please sign in", and it is decidable with no request at
 * all from the token's own exp claim.
 *
 *   node frontend/test_dead_session_startup.mjs [dist-dir]
 *
 * Lives in frontend/ so jsdom resolves from frontend/node_modules.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const distArg = process.argv[2] || "dist";
const DIST = path.isAbsolute(distArg) ? distArg : path.join(HERE, distArg);

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 140)}` : ""}`);
  if (!ok) fails.push(name);
};

const indexPath = path.join(DIST, "index.html");
if (!fs.existsSync(indexPath)) {
  console.log(`  bundle not found at ${DIST} - run the frontend build first`);
  process.exit(2);
}

console.log("TrustNode - a session that died while the app was closed");
console.log();

/** A real-shaped JWT whose exp is `hoursAgo` in the past. */
function tokenExpiredHoursAgo(hoursAgo) {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const exp = Math.floor(Date.now() / 1000) - Math.floor(hoursAgo * 3600);
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "admin-lucas", exp })}.sig`;
}

const virtualConsole = new VirtualConsole();
const dom = new JSDOM(fs.readFileSync(indexPath, "utf8"), {
  url: "http://localhost/", runScripts: "outside-only",
  pretendToBeVisual: true, virtualConsole,
});
const { window } = dom;

// The app was closed overnight: 14 h past expiry, well beyond the 30 min
// the server would still renew.
window.localStorage.setItem("trustnode_auth_token", tokenExpiredHoursAgo(14));

let authMeCalls = 0;
let refreshCalls = 0;
const jsonRes = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: () => "application/json" },
  json: async () => body, text: async () => JSON.stringify(body),
  clone() { return this; },
});
window.fetch = async (url) => {
  const u = String(url?.url || url || "");
  // /me ANSWERS. This is what makes the test discriminate: if the app does
  // not check the saved token's own expiry first, it restores the user from
  // a dead session and renders the app behind banners - which is exactly
  // what the operator photographed. A 401 here would hide the bug, because
  // then the login screen appears either way.
  if (u.includes("/api/auth/me")) {
    authMeCalls += 1;
    return jsonRes({ ok: true, user: { username: "admin-lucas", role: "admin", permissions: {} } });
  }
  if (u.includes("/api/auth/refresh")) { refreshCalls += 1; return jsonRes({ detail: "Session expired. Please sign in again." }, 401); }
  if (u.includes("/api/health")) return jsonRes({ ok: true, workspace: {}, health_snapshot: { state: "ready" } });
  return jsonRes({ ok: true, data: {}, rows: [], templates: [] });
};
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
}));
window.ResizeObserver = window.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
window.IntersectionObserver = window.IntersectionObserver || class {
  observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
};
window.scrollTo = () => {};
window.HTMLCanvasElement.prototype.getContext = () => null;
window.WebSocket = class { constructor() { this.readyState = 3; } close() {} send() {} addEventListener() {} removeEventListener() {} };
window.EventSource = class { constructor() {} close() {} addEventListener() {} removeEventListener() {} };
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });

let code = "";
for (const src of [...window.document.querySelectorAll("script[src]")].map((n) => n.getAttribute("src"))) {
  const file = path.join(DIST, src.replace(/^[./]*/, ""));
  if (fs.existsSync(file)) code += fs.readFileSync(file, "utf8") + ";\n";
}
code = code.replace(/import\.meta\.url/g, JSON.stringify("http://localhost/assets/"));
let threw = null;
try { window.eval(code); } catch (e) { threw = e; }
check("the app mounted", !threw, threw ? String(threw?.message || threw) : "");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(2500);

const text = (window.document.getElementById("root") || window.document.body).textContent || "";

check("the dead token was discarded",
      !window.localStorage.getItem("trustnode_auth_token"),
      "keeping it is what restored a user who could not read anything");

check('no "Your session has ended" banner on launch',
      !/your session has ended/i.test(text),
      "that banner is for a session that dies WHILE working");

check('no "Configuration not loaded" banner on launch',
      !/configuration not loaded/i.test(text),
      "it followed from reads that were refused before the operator did anything");

check("the sign-in screen is what the operator sees",
      /sign in|log in|username|password/i.test(text),
      `rendered: ${text.replace(/\s+/g, " ").slice(0, 110)}`);

check("  it never even asked /api/auth/me",
      authMeCalls === 0,
      `${authMeCalls} call(s) - a token this old is decidable locally`);
check("  and wasted no refresh round trip",
      refreshCalls === 0, `${refreshCalls} refresh call(s)`);

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
