/**
 * A status counter must not look like a configuration change.
 *
 * 2026-10-31, from a customer's log - a healthy edge saving config every ten
 * seconds while nobody edited anything:
 *
 *   13:26:01 ERROR config  Database connections was NOT saved
 *   13:26:01 ERROR config  Devices was NOT saved
 *   13:26:14 INFO  system  Loop heartbeat: devices 1/1 online, databases 1/1
 *                          online, gateways 1/1 running, ws connected.
 *   13:26:21 ERROR config  Database connections was NOT saved
 *   13:26:31 ERROR config  Database connections was NOT saved
 *
 * Each saver asked "has this changed?" with JSON.stringify over its whole
 * React state, and that state carries runtime counters - db_write_count
 * climbs with every historian row. The backend throws those away
 * (_strip_runtime_fields_for_config_sync), so the STORED document never
 * changed, but the signature did, every poll, for ever. On an idle bench
 * nothing moves and it never shows; on a machine collecting at 500 ms it
 * never stops - and the retries exhaust the six sockets a browser allows per
 * origin, which is why templates and generated reports then timed out too.
 *
 *     node scripts/test_config_save_loop.mjs
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "frontend", "src");
const PY = fs.readFileSync(
  path.join(HERE, "..", "backend", "app", "services", "app_store.py"), "utf8");

let fails = 0;
const check = (name, ok, detail = "") => {
  console.log("  %s %s%s", (name + " ".repeat(58)).slice(0, 58),
    ok ? "PASS" : "FAIL", (!ok && detail) ? "  - " + detail : "");
  if (!ok) fails += 1;
};

console.log("a status counter is not a configuration change");

// --- the two lists must stay identical -----------------------------------
const APP = fs.readFileSync(path.join(SRC, "App.jsx"), "utf8");
const jsList = /const CONFIG_TRANSIENT_KEYS = new Set\(\[([\s\S]*?)\]\)/.exec(APP);
const pyList = /transient_keys = \{([\s\S]*?)\}/.exec(PY);
const keysOf = (blob) => new Set((blob || "").match(/"([a-z_]+)"/g)?.map(x => x.slice(1, -1)) || []);
const js = keysOf(jsList && jsList[1]);
const py = keysOf(pyList && pyList[1]);
check("the frontend knows which fields the backend discards", js.size > 0 && py.size > 0,
  `js=${js.size} py=${py.size}`);
const missing = [...py].filter(k => !js.has(k));
const extra = [...js].filter(k => !py.has(k));
check("  the two lists are identical", missing.length === 0 && extra.length === 0,
  `missing from the frontend: ${missing} | not in the backend: ${extra}`);

// --- the behaviour, against the real helpers -----------------------------
const esbuild = await import("file:///" + path.join(
  SRC, "..", "node_modules", "esbuild", "lib", "main.js").replace(/\\/g, "/"));
const out = path.join(os.tmpdir(), `tn_sig_${process.pid}.mjs`);
await esbuild.build({
  stdin: {
    contents: `export { configSignature, stripRuntimeFields } from ${
      JSON.stringify(path.join(SRC, "App.jsx").replace(/\\/g, "/"))};`,
    resolveDir: SRC, loader: "js",
  },
  bundle: true, format: "esm", platform: "node", outfile: out,
  // App.jsx imports modules that live OUTSIDE frontend/, and those cannot
  // find react from where they sit. Resolve from the frontend root, which
  // is what vite does.
  absWorkingDir: path.join(SRC, ".."),
  nodePaths: [path.join(SRC, "..", "node_modules")],
  loader: { ".js": "jsx", ".jsx": "jsx", ".css": "empty", ".png": "empty", ".svg": "empty" },
  define: { "import.meta.env": "{}" }, logLevel: "silent",
});
const { configSignature } = await import("file:///" + out.replace(/\\/g, "/"));

const base = [{
  id: "db1", name: "Local SQLite", kind: "sqlite", enabled: true,
  db_write_count: 11820, db_pending_count: 0,
  db_last_write_utc: "2026-10-31 13:26:01", connection_ok: true,
  ping_ok: true, last_check_utc: "2026-10-31 13:26:01", last_error: "",
}];
const laterTick = [{
  ...base[0],
  db_write_count: 11999, db_pending_count: 3,
  db_last_write_utc: "2026-10-31 13:26:31", connection_ok: false,
  ping_ok: false, last_check_utc: "2026-10-31 13:26:31", last_error: "timeout",
}];
check("a tick of the write counter does NOT look like a change",
  configSignature(base) === configSignature(laterTick),
  "this is the save loop: every poll would fire another PUT");

const renamed = [{ ...base[0], name: "Local SQLite (main)" }];
check("  but a real edit still does",
  configSignature(base) !== configSignature(renamed),
  "change-detection that never fires is worse than one that fires too often");

const added = [...base, { id: "db2", name: "Postgres", kind: "postgres" }];
check("  and so does adding a connection", configSignature(base) !== configSignature(added));

check("  a gateway's running flag is not a config change",
  configSignature([{ id: "gw1", name: "PLC", running: true }])
  === configSignature([{ id: "gw1", name: "PLC", running: false }]));

check("  nested runtime fields are ignored too",
  configSignature([{ id: "d", devices: [{ id: "x", ping_ok: true }] }])
  === configSignature([{ id: "d", devices: [{ id: "x", ping_ok: false }] }]));

// Every saver must use it - one left on JSON.stringify keeps the loop.
check("every saver compares the stored shape",
  (APP.match(/const signature = configSignature\(payload\);/g) || []).length === 8
  && !/const signature = JSON\.stringify\(payload\);/.test(APP),
  "a saver still comparing the live state would loop on its own");

try { fs.unlinkSync(out); } catch { /* temp */ }
console.log("\n  %d check(s) failed", fails);
process.exit(fails ? 1 : 0);
