/**
 * Every report section must offer an inner trigger, with real gateway and
 * tag pickers.
 *
 * 2026-10-11, reported with a screenshot: "not for all section I am able to
 * pick a inner trigger for that section, I cannnot select the gateway and
 * tags, check if all sections have this feature working". The screenshot
 * showed a Trigger block whose Gateway select had NO options and whose Tag
 * select said "No trigger" with nothing to choose.
 *
 * Two separate things, and only one of them is a code defect:
 *
 *   1. Some section types never render a trigger at all. A source grep finds
 *      eight <SectionTrigger> call sites, but there are more section types
 *      than that, and reading JSX cannot tell you which branch an operator
 *      actually lands on.
 *
 *   2. An EMPTY Gateway dropdown looks identical whether the section forgot
 *      to pass its options down or the app simply has no gateways loaded
 *      (which is what happens when the backend is unreachable). The old
 *      harness passed gatewayOptions={[]}, so it could never tell those
 *      apart - every picker looked broken and none of them failed a test.
 *      The harness now supplies two real gateways and their tags.
 *
 * This walks the real editor, adds one of every section type, and fails if
 * a section offers no trigger or offers one that cannot be filled in.
 *
 *   node frontend/test_section_triggers_everywhere.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.join(HERE, "dist_designer", "designer.js");

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(56)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 150)}` : ""}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(BUNDLE)) {
  console.log(`  harness bundle missing at ${BUNDLE}`);
  process.exit(2);
}

console.log("TrustNode - can every section carry its own trigger?");
console.log();

const dom = new JSDOM(`<!doctype html><html><body><div id="root"></div></body></html>`, {
  runScripts: "outside-only", pretendToBeVisual: true,
  virtualConsole: new VirtualConsole(), url: "http://localhost/",
});
const { window } = dom;
window.__harness = "reports";
window.fetch = async () => ({
  ok: true, status: 200, headers: { get: () => "application/json" },
  json: async () => ({ ok: true, templates: [], rows: [] }),
  text: async () => "{}", clone() { return this; },
});
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
window.scrollTo = () => {};
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });
window.eval(fs.readFileSync(BUNDLE, "utf8"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const doc = window.document;
const selSetter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;

await sleep(400);
const addSel = [...doc.querySelectorAll("select")]
  .find((s) => [...s.options].some((o) => /add section/i.test(o.textContent || "")));
check("the designer mounted", Boolean(addSel));
if (!addSel) { console.log(`\nRESULT: FAIL - ${fails.join(", ")}`); process.exit(2); }

// Types that carry no data and no window. A section trigger RESOLVES A TIME
// RANGE (apply_section_triggers rewrites section.time_range); it does not
// show or hide a section. These three read no historian rows, so a trigger
// on them would be a control that changes nothing - worse than no control,
// because an operator would set it and believe it took effect.
const NO_TRIGGER = new Set(["spacer", "page_break", "image"]);
const offered = [...addSel.options].map((o) => String(o.value || "")).filter(Boolean);
check("the picker lists section types", offered.length > 0, `${offered.length} types`);

async function addOnly(type) {
  for (const del of doc.querySelectorAll(".tn-section-editor-tools button")) {
    if ((del.textContent || "").trim() === "×") {
      del.dispatchEvent(new window.Event("click", { bubbles: true }));
    }
  }
  await sleep(140);
  selSetter.call(addSel, type);
  addSel.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(220);
  for (let i = 0; i < 3; i += 1) {
    const shut = doc.querySelectorAll(".tn-section-editor.is-collapsed");
    if (!shut.length) break;
    for (const sec of shut) {
      sec.querySelector(".tn-section-toggle")?.dispatchEvent(
        new window.Event("click", { bubbles: true }));
    }
    await sleep(180);
  }
  return doc.querySelector(".tn-section-editor:not(.is-collapsed)");
}

/** The Start-when gateway select inside a section's Trigger block. */
function triggerSelects(open) {
  const rows = [...open.querySelectorAll(".tn-row")].filter((r) => {
    const txt = (r.textContent || "").toLowerCase();
    return txt.includes("start when") || txt.includes("stop when")
      || txt.includes("and when") || txt.includes("or when");
  });
  if (!rows.length) return null;
  const sels = [...rows[0].querySelectorAll("select")];
  const gw = sels.find((s) => [...s.options].some((o) => /gateway/i.test(o.textContent || "")));
  const tag = sels.find((s) => [...s.options].some((o) => /no trigger/i.test(o.textContent || "")));
  return { gw, tag };
}

const noTrigger = [];
const noGateways = [];
const ok = [];
for (const type of offered) {
  if (NO_TRIGGER.has(type)) continue;
  const open = await addOnly(type);
  if (!open) { noTrigger.push(`${type} (never opened)`); continue; }
  const t = triggerSelects(open);
  if (!t || !t.gw) { noTrigger.push(type); continue; }
  // An option list of just the "Gateway…" placeholder is the reported
  // symptom: a control that exists and cannot be used.
  const realGateways = [...t.gw.options].filter((o) => String(o.value || "")).length;
  if (realGateways < 1) { noGateways.push(type); continue; }
  ok.push(`${type}(${realGateways})`);
}

console.log();
check("every section offers an inner trigger", noTrigger.length === 0,
      noTrigger.length ? `no trigger in: ${noTrigger.join(", ")}` : `${ok.length} types`);
check("  and its gateway list is actually populated", noGateways.length === 0,
      noGateways.length ? `empty gateway picker in: ${noGateways.join(", ")}` : ok.join(" "));

// Picking a gateway must then populate the TAG list - the second half of
// "I cannnot select the gateway and tags".
console.log();
console.log("[choosing a gateway fills the tag list]");
const open = await addOnly("line_chart");
const t = triggerSelects(open);
if (t?.gw && t?.tag) {
  check("the tag list starts empty", [...t.tag.options].filter((o) => o.value).length === 0,
        "no gateway chosen yet, so there is nothing to list");
  selSetter.call(t.gw, "gw-plc");
  t.gw.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(220);
  const t2 = triggerSelects(doc.querySelector(".tn-section-editor:not(.is-collapsed)"));
  const tags = [...(t2?.tag?.options || [])].map((o) => String(o.value || "")).filter(Boolean);
  check("  choosing a gateway lists its tags", tags.length >= 4, tags.join(", "));
  check("  and they are THAT gateway's tags", tags.includes("Cycle_Active"), tags.join(", "));
} else {
  check("the trigger exposes a gateway and a tag select", false,
        "could not find them on a line chart");
}

// The harness itself: a test that passes [] proves nothing about a picker.
console.log();
console.log("[the harness supplies real data, or none of the above means anything]");
const entry = fs.readFileSync(path.join(HERE, "test_designer_entry.jsx"), "utf8");
check("the reports harness passes real gateways",
      entry.includes("gatewayOptions={HARNESS_GATEWAYS}")
      && !entry.includes("gatewayOptions={[]}"),
      "with [] every picker in the page looks broken and no test can tell");
check("  and real tags for them", entry.includes("tagsByGateway={HARNESS_TAGS}"));

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
