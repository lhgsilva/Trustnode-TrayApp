/**
 * A machine that cannot create its first window must still start.
 *
 * On 2026-10-22 a customer's new PC showed nothing after the workspace
 * detector and "the backend never responded". The backend is spawned
 * AFTER the splash, so anything that kills the process at the first
 * BrowserWindow presents as a backend failure - and because that death
 * is native, no JS handler fires and nothing is logged.
 *
 * main.js now leaves a breadcrumb immediately before the first window
 * and clears it once one exists. Finding it at startup means the
 * PREVIOUS launch died there, so the next launch falls back to software
 * rendering, sticky from then on.
 *
 * This lifts that decision straight out of main.js and runs it against a
 * scratch directory, so it fails if the behaviour is removed or inverted.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MAIN = path.join(HERE, "..", "desktop", "main.js");
const src = fs.readFileSync(MAIN, "utf8");

const START = "const FIRST_WINDOW_PENDING =";
const END = "})();";
const a = src.indexOf(START);
if (a < 0) throw new Error("first-window safety block not found in main.js");
const b = src.indexOf(END, src.indexOf("app.disableHardwareAcceleration", a));
if (b < 0) throw new Error("could not find the end of the safety block");
const BLOCK = src.slice(a, b + END.length);

let fails = 0;
function check(name, ok, detail = "") {
  console.log("  %s %s%s", (name + " ".repeat(52)).slice(0, 52),
    ok ? "PASS" : "FAIL", detail ? "  " + detail : "");
  if (!ok) fails += 1;
}

function run({ breadcrumb = false, marker = false, argv = [], env = {} }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tn-fw-"));
  if (breadcrumb) fs.writeFileSync(path.join(dir, "first-window-pending"), "x");
  if (marker) fs.writeFileSync(path.join(dir, "gpu-unsafe"), "x");
  const disabled = [];
  const logs = [];
  const origArgv = process.argv, origEnv = process.env;
  process.argv = ["electron.exe", ...argv];
  process.env = { ...env };
  try {
    const fn = new Function(
      "path", "fs", "app", "bootLog", "BOOT_LOG_DIR",
      `${BLOCK}\n return { markFirstWindowPending, clearFirstWindowPending };`
    );
    const api = fn(path, fs,
      { disableHardwareAcceleration: () => disabled.push(true) },
      (m) => logs.push(String(m)), dir);
    return {
      dir, api,
      gpuDisabled: disabled.length > 0,
      stickyMarker: fs.existsSync(path.join(dir, "gpu-unsafe")),
      logs,
    };
  } finally {
    process.argv = origArgv;
    process.env = origEnv;
  }
}

console.log("The app starts even on a machine that cannot create its first window");
console.log();

let r = run({});
check("healthy machine keeps GPU rendering", r.gpuDisabled === false);
check("  and is not marked unsafe", r.stickyMarker === false);

r = run({ breadcrumb: true });
check("previous launch died at the first window -> software", r.gpuDisabled === true);
check("  and the fallback is made sticky", r.stickyMarker === true,
  "otherwise it fails again on the next launch");
check("  and it says why", r.logs.some((l) => /GPU rendering DISABLED/.test(l)),
  r.logs[0] || "nothing logged");

r = run({ marker: true });
check("a machine known bad stays on software", r.gpuDisabled === true);

r = run({ argv: ["--disable-gpu"] });
check("--disable-gpu is honoured", r.gpuDisabled === true);
r = run({ argv: ["--safe-mode"] });
check("--safe-mode is honoured", r.gpuDisabled === true);
r = run({ env: { TRUSTNODE_DISABLE_GPU: "1" } });
check("TRUSTNODE_DISABLE_GPU=1 is honoured", r.gpuDisabled === true);

// the breadcrumb lifecycle itself
r = run({});
r.api.markFirstWindowPending();
const set = fs.existsSync(path.join(r.dir, "first-window-pending"));
r.api.clearFirstWindowPending();
const cleared = !fs.existsSync(path.join(r.dir, "first-window-pending"));
check("breadcrumb is written before the window", set);
check("  and removed once the window exists", cleared,
  "a breadcrumb left behind would downgrade every later launch");

console.log();
console.log("RESULT: %s", fails ? `FAIL - ${fails} case(s)` : "PASS");
process.exit(fails ? 2 : 0);
