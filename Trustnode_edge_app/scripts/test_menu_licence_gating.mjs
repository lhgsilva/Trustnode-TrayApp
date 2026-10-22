/**
 * The menu must list what the customer bought - and nothing else.
 *
 * 2026-10-22, from a new customer: a licence with a subset of modules
 * ticked still showed Power Management, OEE, Email Notifications,
 * Connections, Remote Access, OPC UA and MQTT. Two independent causes:
 *
 *  1. isPageLicensed() opens with `if (!required) return true`, so any
 *     page MISSING from MODULE_KEY_BY_PAGE is visible on every edge
 *     whatever the licence says. power_configuration,
 *     email_and_notifications and scheduled_reports had no entry at all.
 *
 *  2. hasClientModuleAccess() let admins skip the licence gate entirely.
 *     The customer's own administrator is who uses the edge, so in
 *     practice nothing was ever gated. Batch Management was the one
 *     module that hid correctly - because it calls hasLicenseModule()
 *     directly and never reached that early return.
 *
 * Checks the real source, so it fails if either returns.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = fs.readFileSync(path.join(HERE, "..", "frontend", "src", "App.jsx"), "utf8");
const STORE = fs.readFileSync(
  path.join(HERE, "..", "backend", "app", "services", "control_plane_store.py"), "utf8");

let fails = 0;
const check = (name, ok, detail = "") => {
  // Detail explains a FAILURE. Printing it beside PASS made a healthy run
  // read like a list of problems.
  console.log("  %s %s%s", (name + " ".repeat(56)).slice(0, 56),
    ok ? "PASS" : "FAIL", (!ok && detail) ? "  " + detail : "");
  if (!ok) fails += 1;
};

// --- the module keys the backend actually issues -------------------------
const catalogue = new Set(
  [...STORE.matchAll(/"key":\s*"([a-z_]+)"/g)].map((m) => m[1]));
check("read the backend module catalogue", catalogue.size > 20,
  `${catalogue.size} keys`);

// --- the page -> module map ----------------------------------------------
const mapStart = APP.indexOf("const MODULE_KEY_BY_PAGE = {");
const mapEnd = APP.indexOf("};", mapStart);
const mapSrc = APP.slice(mapStart, mapEnd);
const mapping = new Map(
  [...mapSrc.matchAll(/^\s{2}([a-z_]+):\s*"([a-z_]+)",/gm)].map((m) => [m[1], m[2]]));
check("read MODULE_KEY_BY_PAGE", mapping.size > 10, `${mapping.size} pages`);

// Every page that corresponds to a sold module must be mapped. A page
// missing here is ALWAYS visible - that is the defect, not a style point.
const MUST_MAP = {
  power_configuration: "power_management",
  email_and_notifications: "email_notifications",
  scheduled_reports: "scheduled_reports",
  opc_ua: "opcua",
  mqtt: "mqtt",
  connections_overview: "lan_access",
  lan_sharing: "lan_access",
};
for (const [page, want] of Object.entries(MUST_MAP)) {
  check(`'${page}' is licence-gated`, mapping.get(page) === want,
    mapping.has(page) ? `maps to '${mapping.get(page)}', wanted '${want}'`
                      : "NOT MAPPED - the page shows on every edge");
}
check("the OEE pages are licence-gated",
  [...mapping.keys()].filter((k) => k.startsWith("oee_")).length >= 3,
  [...mapping.keys()].filter((k) => k.startsWith("oee_")).join(", "));

// Every mapped key must be one the backend can actually issue, or the page
// is unreachable for everyone - the opposite failure, equally silent.
for (const [page, key] of mapping) {
  check(`  '${key}' is a real catalogue module`, catalogue.has(key),
    `page '${page}' requires a module the portal never issues`);
}

// --- admins must not be exempt from a licence we can read ----------------
const fnStart = APP.indexOf("const hasClientModuleAccess = useCallback");
const fnSrc = APP.slice(fnStart, fnStart + 2600);
check("admins are exempt ONLY while the licence is unknown",
  /isAdmin\s*&&\s*!licenceKnown/.test(fnSrc),
  "a blanket `if (isAdmin) return true` ungates every module for the "
  + "customer's own administrator");
check("  and the licence gate runs before that exemption",
  fnSrc.indexOf("isPageLicensed(page)") < fnSrc.indexOf("if (isAdmin) return true;"),
  "the gate must be evaluated, not skipped");

console.log();
console.log("RESULT: %s", fails ? `FAIL - ${fails} check(s)` : "PASS");
process.exit(fails ? 2 : 0);
