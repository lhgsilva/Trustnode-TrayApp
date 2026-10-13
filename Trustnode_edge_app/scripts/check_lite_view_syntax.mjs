// Parse the phone app's inline Babel block.
//
// 2026-09-29: index.html is a 5,000-line hand-written app in one inline
// module. Nothing in the build or the release gate reads it, so a syntax
// error there ships silently and the phone renders a blank page.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// pathToFileURL: a bare Windows path is not a valid ESM specifier.
const { pathToFileURL } = await import("node:url");
const { parse } = await import(pathToFileURL(
  path.join(ROOT, "frontend", "node_modules", "@babel", "parser", "lib", "index.js")).href);

let fails = 0;
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(56)}: ${ok ? "PASS" : "FAIL"}${detail ? " - " + detail : ""}`);
  if (!ok) fails += 1;
};

console.log("TrustNode - lite_view (phone app) syntax");
console.log();

// A path may be given so the CLOUD copy (web_cloud_readonly/lite) can be
// checked with the same parser before it is pushed to the VPS.
const TARGET = process.argv[2]
  ? path.resolve(ROOT, process.argv[2])
  : path.join(ROOT, "backend/static/lite_view/index.html");
const html = fs.readFileSync(TARGET, "utf8");
console.log("  (checking " + path.basename(TARGET) + " in " + path.basename(path.dirname(TARGET)) + ")");
const blocks = [...html.matchAll(/<script type="module"[^>]*>([\s\S]*?)<\/script>/g)];
check("the app has an inline module", blocks.length > 0, `${blocks.length} block(s)`);
for (const [i, b] of blocks.entries()) {
  let err = "";
  try {
    parse(b[1], { sourceType: "module", plugins: ["jsx"], errorRecovery: false });
  } catch (e) { err = e.message; }
  check(`  block ${i + 1} parses (${b[1].split("\n").length} lines)`, !err, err);
}

const isCloud = /web_cloud_readonly/.test(TARGET);
if (isCloud) {
  check("the cloud importmap uses the real supabase-js",
    html.includes("esm.sh/@supabase/supabase-js")
      && !html.includes('"@supabase/supabase-js": "./local_supabase_shim.js"'),
    "shipping the LAN shim to the cloud would break every query");
  console.log();
  console.log(`RESULT: ${fails ? "FAIL" : "PASS"}`);
  process.exit(fails ? 2 : 0);
}
const shim = fs.readFileSync(path.join(ROOT, "backend/static/lite_view/local_supabase_shim.js"), "utf8");
let shimErr = "";
try { parse(shim, { sourceType: "module" }); } catch (e) { shimErr = e.message; }
check("the local supabase shim parses", !shimErr, shimErr);

// The stubs that made the phone's Reporting page permanently empty.
check("the shim reads the edge's generated reports",
  shim.includes("/api/lite-local/reports"),
  "it returned [] with 'reports queue is cloud-only'");
check("  and the edge's report templates",
  shim.includes("/api/lite-local/report-templates"));
check("the phone can download without object storage",
  html.includes("local_file_url"),
  "storage_path is only set by the cloud uploader, so LAN rows stayed 'UPLOADING'");
// The shell is cache-first: a new index.html under an unchanged CACHE_VERSION
// never reaches a browser that has visited before.
{
  const sw = fs.readFileSync(path.join(ROOT, "backend/static/lite_view/sw.js"), "utf8");
  const m = sw.match(/CACHE_VERSION\s*=\s*"([^"]+)"/);
  check("the service worker names a cache version", Boolean(m), m ? m[1] : "no CACHE_VERSION");
  check("  and it was bumped past v47 for this shell",
    Boolean(m) && m[1] !== "tnlite-v47-gap-nulls-no-carry-forward",
    "a new index.html under the old version cannot reach a returning browser");
}
check("the phone can generate from a template",
  html.includes("/api/lite-local/reports/generate"),
  "it only queued a cloud request the read-only shim cannot insert");

console.log();
console.log(`RESULT: ${fails ? "FAIL" : "PASS"}`);
process.exit(fails ? 2 : 0);
