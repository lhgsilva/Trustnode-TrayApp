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
// A value is either "module" or ["module", "module"] - ANY of them unlocks
// the page, because the catalogue sells both the Connections PAGE and the
// three individual connection modules.
const mapping = new Map(
  [...mapSrc.matchAll(/^\s{2}([a-z_]+):\s*(\[[^\]]*\]|"[a-z_]+")\s*,/gm)]
    .map((m) => [m[1], [...m[2].matchAll(/"([a-z_]+)"/g)].map((x) => x[1])]));
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
  check(`'${page}' is licence-gated`, (mapping.get(page) || []).includes(want),
    mapping.has(page) ? `maps to '${(mapping.get(page) || []).join("/")}', wanted '${want}'`
                      : "NOT MAPPED - the page shows on every edge");
}

// 2026-10-22: the catalogue sells "connections" as its own module -
// "Connections page (LAN + OPC + MQTT)". Ticking THAT box on a licence
// left the menu hidden, because the pages demanded lan_access/opcua/mqtt
// instead. A module the customer bought must reveal what it names.
for (const page of ["connections_overview", "lan_sharing", "opc_ua", "mqtt"]) {
  check(`  '${page}' also accepts the 'connections' bundle`,
    (mapping.get(page) || []).includes("connections"),
    "ticking 'Connections page' on the licence must reveal it");
}
check("the OEE pages are licence-gated",
  [...mapping.keys()].filter((k) => k.startsWith("oee_")).length >= 3,
  [...mapping.keys()].filter((k) => k.startsWith("oee_")).join(", "));

// Every mapped key must be one the backend can actually issue, or the page
// is unreachable for everyone - the opposite failure, equally silent.
for (const [page, keys] of mapping) {
  for (const key of keys) {
    check(`  '${key}' is a real catalogue module`, catalogue.has(key),
      `page '${page}' requires a module the portal never issues`);
  }
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

// --- the MENU must actually consult that map ----------------------------
//
// 2026-10-22, the second report of the same symptom. Everything above
// passed while Power Configuration, all four OEE pages, Email and
// Notifications and Scheduled Reports were still showing on an edge whose
// licence carried none of them - because canOpenPage() asked the licence
// only for SEVEN page names written out by hand:
//
//   if (!isAdmin && !isPageLicensed(page) && ["dashboard", "power_overview",
//       "historian", "alarms", "reporting", "generated_reports",
//       "interface"].includes(page)) return false;
//
// A correct map is worthless if the caller ignores it, so this checks the
// caller. The list is the bug: adding a page to MODULE_KEY_BY_PAGE must be
// enough on its own.
const openStart = APP.indexOf("const canOpenPage = useCallback");
const openSrc = APP.slice(openStart, openStart + 2200);
check("the menu asks MODULE_KEY_BY_PAGE which pages are licensed",
  /MODULE_KEY_BY_PAGE\[page\]\s*&&\s*!isPageLicensed\(page\)/.test(openSrc),
  "canOpenPage must gate every mapped page, not a hand-written few");
check("  and gates no hand-written list of page names",
  !/!isPageLicensed\(page\)\s*&&\s*\[\s*"/.test(openSrc),
  "a literal page list is how this shipped twice");

// The pages that were actually reported as wrongly visible.
for (const page of ["power_configuration", "oee_overview", "oee_configuration",
                    "email_and_notifications", "scheduled_reports"]) {
  check(`  '${page}' is covered by that gate`, mapping.has(page),
    "reported visible on an edge whose licence did not include it");
}

// --- activation must not inherit the previous licence -------------------
//
// The edge keeps a 30-day localStorage snapshot and short-circuits on it
// before any cloud call. Re-activating a machine therefore showed the OLD
// module set until something forced a re-check - "I had to refresh on the
// edge menu for the license to be loaded".
const LOGIN = fs.readFileSync(path.join(HERE, "..", "frontend", "src",
  "components", "Login", "Login.jsx"), "utf8");
check("activation clears the cached licence snapshot",
  /clearCachedLicenceSnapshot\(\)/.test(LOGIN),
  "otherwise the previous activation's modules gate the new licence");
check("  and clears it BEFORE reporting success",
  LOGIN.indexOf("clearCachedLicenceSnapshot()")
    < LOGIN.indexOf("Edge activated successfully"),
  "the app reads the snapshot as soon as the operator signs in");

// --- add-on menus outside the main app must hide too --------------------
//
// The AI menu is its own component, rendered next to the nav rather than
// through canOpenPage, and was gated by role + permission ONLY - so an
// admin saw TrustNode Intelligence on an edge whose licence never included
// it. An add-on that nobody bought must not appear.
check("the AI menu is licence-gated",
  /hasLicenseModule\("trustnode_intelligence"\)/.test(APP),
  "role and permission are not a licence");

// --- a manual refresh must survive the next launch ----------------------
//
// "Re-check now" and the activation refresh both set React state only, so
// the menu was correct until restart - when the 30-day cached snapshot
// short-circuited and the PREVIOUS module set came back.
const refreshes = (APP.match(/setEdgeLicenseSnapshot\((out|licenseOut) \|\| null\);/g) || []).length;
const persists = (APP.match(/cacheLicenceSnapshot\((out|licenseOut) \|\| null\);/g) || []).length;
check("every licence refresh persists what it fetched",
  refreshes > 0 && persists >= refreshes,
  `${refreshes} refresh path(s), ${persists} persist - a refresh that does `
  + "not write the cache is undone by the next launch");

// --- pages deliberately NOT gated ---------------------------------------
//
// A module with default_enabled:false is ABSENT from a licence created
// with defaults. Gating these would hide Gateway Configuration, Tags,
// Devices, the Database pages and user management from customers who are
// entitled to them - and lock an admin out of their own edge.
for (const page of ["tags", "devices", "gateway_configuration",
                    "users_and_access_control", "database"]) {
  check(`'${page}' stays ungated`, !mapping.has(page),
    "gating it would hide an essential page whenever the licence omits "
    + "the module - which a default licence does");
}

console.log();
console.log("RESULT: %s", fails ? `FAIL - ${fails} check(s)` : "PASS");
process.exit(fails ? 2 : 0);
