/**
 * The historian export has to ask for the window the operator typed.
 *
 * 2026-10-17, reported: "the historian export is not working, not loading,
 * preview, export". Nothing was broken in the sense of throwing - it was
 * asking the right question about the wrong hour and correctly getting
 * nothing back. Measured on the live store that afternoon:
 *
 *     typed  17:08:40 - 17:12:47  ->          0 rows
 *     same window shifted -1h     ->     27,234 rows
 *     machine offset              ->     UTC+1
 *
 * `<input type="datetime-local">` hands back LOCAL wall time with no zone
 * ("2026-10-17T17:08:40"), and that string was written straight into a field
 * named `from_utc` and compared against the historian's UTC timestamps. On a
 * UTC+1 machine every request was an hour in the future, so an empty preview
 * and an empty export were the honest answer. The field NAME is what hid it:
 * nothing in the code looked wrong.
 *
 * This takes the conversion out of the page source and runs it, because the
 * defect was arithmetic, not wiring. It is run twice - once with TZ=UTC,
 * where a correct conversion changes nothing, and once in the machine's own
 * zone, where it must shift by exactly the machine's offset.
 *
 *   node frontend/test_export_window_utc.mjs
 *
 * Exit 0 = the window reaching the API is UTC. Exit 2 = it is not.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "src", "components", "DataExport", "DataExportPage.jsx");

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 150)}` : ""}`);
  if (!ok) fails.push(name);
};

console.log("TrustNode - the historian export asks in UTC");
console.log("");

const src = fs.readFileSync(SRC, "utf8");

// Lift the real function out of the page rather than restating it here: a
// copy in the test would keep passing after the page lost the conversion.
const start = src.indexOf("const toUtcStamp = (local) => {");
const end = src.indexOf("\n  };", start);
check("the page still HAS a local -> UTC conversion", start > 0 && end > start,
      "deleting it is the defect; a test that cannot find it must fail");
if (start < 0 || end < 0) {
  console.log("");
  console.log("RESULT: FAIL - no conversion in the page");
  process.exit(2);
}
const body = src.slice(start, end + 4).replace(/^const /, "") + ";";
const toUtcStamp = new Function(`"use strict"; const ${body} return toUtcStamp;`)();

const offsetH = -new Date("2026-10-17T12:00:00").getTimezoneOffset() / 60;
console.log(`  [running in ${process.env.TZ || "the machine's zone"}, offset UTC${offsetH >= 0 ? "+" : ""}${offsetH}]`);
console.log("");

const TYPED = "2026-10-17T17:08:40";
const out = toUtcStamp(TYPED);

console.log("[1. the shape the API expects]");
check("a space, not a T, and seconds kept",
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(out), out);

console.log("");
console.log("[2. the same INSTANT, expressed in UTC]");
// Read back as UTC it must be the very moment the operator picked locally.
// This is the whole assertion: it holds in every zone, which a hardcoded
// expected string would not.
const wanted = new Date(TYPED).getTime();
const got = new Date(out.replace(" ", "T") + "Z").getTime();
check("the converted stamp is the instant that was typed", got === wanted,
      `${out}Z is ${(got - wanted) / 3600000}h away from ${TYPED} local`);

console.log("");
console.log("[3. it really shifts when the machine is not on UTC]");
if (Math.abs(offsetH) < 0.01) {
  check("on UTC the wall time is unchanged", out === TYPED.replace("T", " "),
        `${out} - a conversion that moved the hour here would be wrong too`);
} else {
  check("the hour moves by the machine's offset", out !== TYPED.replace("T", " "),
        `${TYPED} -> ${out}; sending the typed hour as from_utc is what ` +
        `returned 0 rows out of 27,234`);
  check("  and by exactly the offset, not a guess",
        Math.abs((wanted - got) / 3600000) < 0.001,
        `${(got - wanted) / 3600000}h`);
}

console.log("");
console.log("[4. the awkward inputs]");
check("an empty box stays empty - it means 'everything'", toUtcStamp("") === "");
check("  so does an undefined one", toUtcStamp(undefined) === "");
check("  a half-typed date is passed through, not silently dropped",
      toUtcStamp("2026-13-99T99:99") === "2026-13-99T99:99",
      "the server rejecting a bad date is better than exporting the wrong one");
check("  a stamp with no seconds still converts",
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:00$/.test(toUtcStamp("2026-10-17T17:08")),
      toUtcStamp("2026-10-17T17:08"));
check("  midnight does not roll into the wrong day",
      new Date(toUtcStamp("2026-01-01T00:00:00").replace(" ", "T") + "Z").getTime()
      === new Date("2026-01-01T00:00:00").getTime(),
      toUtcStamp("2026-01-01T00:00:00"));

console.log("");
console.log("[5. every path to the API goes through it]");
// Preview and the two export routes are three separate calls; fixing one and
// not the others is how this survived - a preview that matched the export
// would at least have looked consistent.
for (const [label, call] of [
  ["preview", "api.exportPreview(wireSpec())"],
  ["client-side export", "api.exportClientSide(wireSpec()"],
  ["server-side export", "api.exportServerSide(wireSpec()"],
]) {
  check(`  ${label} sends the converted window`, src.includes(call), call);
}
check("  nothing sends the raw spec any more",
      !/api\.export(Preview|ClientSide|ServerSide)\(spec[,)]/.test(src),
      "one unconverted call is one silently empty export");
check("  the inputs still SHOW local time",
      src.includes('type="datetime-local"'),
      "an operator picks the time on the clock on the wall; only the wire " +
      "is UTC");

console.log("");
console.log("[6. a report section's Custom range, same defect, same day]");
// The report designer wrote time_range.from_utc from a datetime-local box
// too. It needs BOTH directions: a stored UTC stamp shown in a local box
// without converting back would be re-converted on the next save and walk
// an hour further every time the section was edited.
const DES = fs.readFileSync(
  path.join(HERE, "src", "components", "Reports", "ReportTemplateDesigner.jsx"), "utf8");

const lift = (name) => {
  const at = DES.indexOf(`const ${name} = (`);
  if (at < 0) return null;
  const stop = DES.indexOf(["", "};"].join("\n"), at);
  if (stop < 0) return null;
  return DES.slice(at, stop + 3).replace(/^const /, "");
};
const fnFrom = lift("localInputToUtc");
const fnTo = lift("utcToLocalInput");
check("the designer converts in BOTH directions", Boolean(fnFrom && fnTo),
      "one direction alone makes an edited range walk an hour per save");

if (fnFrom && fnTo) {
  const [localInputToUtc, utcToLocalInput] = new Function(
    `"use strict"; const ${fnFrom} const ${fnTo} return [localInputToUtc, utcToLocalInput];`)();

  const typed = "2026-10-17T17:08:40";
  const stored = localInputToUtc(typed);
  check("  a typed range is stored as a UTC stamp",
        /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(stored), stored);
  check("  and it is the instant that was typed",
        new Date(stored.replace(" ", "T") + "Z").getTime() === new Date(typed).getTime(),
        stored);
  check("  reopening the section shows the typed time again",
        utcToLocalInput(stored) === typed,
        `${stored} came back as ${utcToLocalInput(stored)} - an asymmetric pair ` +
        `moves the range every time it is saved`);
  check("  saving twice does not move it",
        localInputToUtc(utcToLocalInput(stored)) === stored,
        "the round trip has to be stable or editing anything else on the " +
        "section drags the window along");
  check("  an empty range stays empty",
        localInputToUtc("") === "" && utcToLocalInput("") === "");
  check("  a stamp that already carries a zone is not shifted twice",
        utcToLocalInput("2026-10-17 16:08:40Z") === utcToLocalInput("2026-10-17 16:08:40"),
        "a trailing Z must not add a second offset");
  check("  the inputs are wired to the pair, not to the raw value",
        DES.includes("value={utcToLocalInput(section.time_range?.from_utc)}")
        && DES.includes("from_utc: localInputToUtc(e.target.value)")
        && DES.includes("value={utcToLocalInput(section.time_range?.to_utc)}")
        && DES.includes("to_utc: localInputToUtc(e.target.value)"),
        "both boxes, both directions");
}

console.log("");
console.log(`RESULT: ${fails.length ? "FAIL - " + fails.join(", ") : "PASS"}`);
process.exit(fails.length ? 2 : 0);
