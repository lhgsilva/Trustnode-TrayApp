/**
 * The hosted developer portal must be WRITABLE for a global admin.
 *
 * getRuntimeSurface() returns "cloud" for any page served over http(s)
 * from a non-localhost host. isReadOnlySession() treated that as
 * read-only unconditionally, so the developer portal - the one cloud
 * surface whose entire purpose is writing control-plane records - showed
 *
 *     "Read-only portal mode: this user cannot add, edit, or delete
 *      control-plane records."
 *
 * for a signed-in global admin. canEditPage() returns false before it
 * ever looks at the role, so the request never reached the backend:
 * fixing roles, tenants or licences could not clear it. It also made
 * build-web-cloud-readonly.ps1's -ReadOnly switch dead code.
 *
 * This runs the REAL functions, lifted from frontend/src/api.js, against
 * every surface that matters - so it fails if the rule is reverted, and
 * equally if a read-only surface is accidentally opened up.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "frontend", "src", "api.js");
const src = fs.readFileSync(SRC, "utf8");

function lift(name) {
  const start = src.indexOf(`export function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in api.js`);
  let i = src.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < src.length; j += 1) {
    if (src[j] === "{") depth += 1;
    else if (src[j] === "}") { depth -= 1; if (depth === 0) { end = j + 1; break; } }
  }
  if (end < 0) throw new Error(`could not bracket-match ${name}`);
  return src.slice(start, end).replace(/^export\s+/, "");
}

const LAN_RE = /^\/trustnode\/(full|client|lite)\/app(?:\/|$)/;
const body = `${lift("getRuntimeSurface")}\n${lift("isReadOnlySession")}\nreturn { getRuntimeSurface, isReadOnlySession };`;

function evaluate({ href, ua = "Mozilla/5.0", forceReadonly, clientView, role }) {
  const url = new URL(href);
  global.window = {
    location: { protocol: url.protocol, hostname: url.hostname, pathname: url.pathname, search: url.search },
    navigator: { userAgent: ua },
  };
  const fn = new Function(
    "LAN_SURFACE_PATH_RE", "FORCE_READONLY", "isClientViewMode", "_sessionRole", body
  );
  const api = fn(LAN_RE, forceReadonly, () => clientView, role || null);
  return { surface: api.getRuntimeSurface(), readOnly: api.isReadOnlySession() };
}

const CASES = [
  // the bug this guards
  ["developer portal, admin-capable build, admin", {
    href: "https://trustnode.lsapps.app/portal", forceReadonly: false, clientView: false, role: "admin" },
    { surface: "cloud", readOnly: false }],
  // the switch must still work
  ["same portal built with -ReadOnly", {
    href: "https://trustnode.lsapps.app/portal", forceReadonly: true, clientView: false, role: "admin" },
    { surface: "cloud", readOnly: true }],
  // a viewer is read-only wherever they are
  ["hosted portal, viewer role", {
    href: "https://trustnode.lsapps.app/portal", forceReadonly: false, clientView: false, role: "viewer" },
    { surface: "cloud", readOnly: true }],
  ["hosted portal, client role", {
    href: "https://trustnode.lsapps.app/portal", forceReadonly: false, clientView: false, role: "client" },
    { surface: "cloud", readOnly: true }],
  // the single-file client build keeps its own trigger
  ["client-view single-file build, admin", {
    href: "https://trustnode.lsapps.app/", forceReadonly: false, clientView: true, role: "admin" },
    { surface: "cloud", readOnly: true }],
  // LAN surfaces are decided by the URL path and must not change
  ["LAN client surface, admin", {
    href: "http://192.168.10.5/trustnode/client/app/", forceReadonly: false, clientView: false, role: "admin" },
    { surface: "lan_client", readOnly: true }],
  ["LAN lite surface, admin", {
    href: "http://192.168.10.5/trustnode/lite/app/", forceReadonly: false, clientView: false, role: "admin" },
    { surface: "lan_lite", readOnly: true }],
  ["LAN full surface, admin (already writable)", {
    href: "http://192.168.10.5/trustnode/full/app/", forceReadonly: false, clientView: false, role: "admin" },
    { surface: "lan_full", readOnly: false }],
  // the desktop app
  ["desktop app (Electron)", {
    href: "http://localhost/", ua: "Mozilla/5.0 Electron/31", forceReadonly: false, clientView: false, role: "admin" },
    { surface: "desktop", readOnly: false }],
];

let fails = 0;
console.log("The hosted portal is writable for an admin; every read-only surface stays read-only");
console.log();
for (const [label, input, want] of CASES) {
  let got;
  try { got = evaluate(input); } catch (e) { got = { surface: "THREW", readOnly: String(e.message) }; }
  const ok = got.surface === want.surface && got.readOnly === want.readOnly;
  if (!ok) fails += 1;
  console.log(
    "  %s %s",
    (label + " ".repeat(46)).slice(0, 46),
    ok ? "PASS" : `FAIL  surface=${got.surface} readOnly=${got.readOnly} (wanted surface=${want.surface} readOnly=${want.readOnly})`
  );
}
console.log();
console.log("RESULT: %s", fails ? `FAIL - ${fails} case(s)` : "PASS");
process.exit(fails ? 2 : 0);
