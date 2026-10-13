/**
 * EVERY data-bearing report section must offer a start/stop trigger.
 *
 * 2026-10-07, requested: "not all the template reports has the option to
 * pick the start and end trigger, we should have for all". The trigger
 * markup lived inline in the chart/table branch of the section editor, so a
 * KPI grid, a text block, a segment summary and a pie chart could never be
 * bound to a cycle - even though the backend applies section triggers by a
 * type-agnostic rule (apply_section_triggers / resolve_trigger_window in
 * report_renderer.py). It was purely a gap in the editor, and a gap like
 * that is invisible until someone goes looking for the control.
 *
 * This adds one section of each type to a real template designer and checks
 * the trigger control is actually in the DOM for it.
 *
 *   node frontend/test_section_triggers.mjs
 *
 * Exit 0 = every type offers it. Exit 2 = at least one does not.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.join(HERE, "dist_designer", "designer.js");

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 150)}` : ""}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(BUNDLE)) {
  console.log(`  harness bundle missing at ${BUNDLE}`);
  console.log("  build it: npx vite build --config vite.designer.config.mjs (in frontend/)");
  process.exit(2);
}

console.log("TrustNode - can every section type be bound to a cycle?");
console.log();

const virtualConsole = new VirtualConsole();
const thrown = [];
virtualConsole.on("jsdomError", (e) => thrown.push(String(e?.message || e)));

const dom = new JSDOM(`<!doctype html><html><body><div id="root"></div></body></html>`, {
  runScripts: "outside-only", pretendToBeVisual: true, virtualConsole, url: "http://localhost/",
});
const { window } = dom;
window.__harness = "reports";
window.fetch = async () => ({
  ok: true, status: 200, headers: { get: () => "application/json" },
  json: async () => ({ ok: true, templates: [], rows: [] }),
  text: async () => "{}", clone() { return this; },
});
window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
window.ResizeObserver = window.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
window.scrollTo = () => {};
window.addEventListener("unhandledrejection", (e) => { e.preventDefault?.(); });

try { window.eval(fs.readFileSync(BUNDLE, "utf8")); } catch (e) {
  check("the template designer mounted", false, String(e?.message || e));
  console.log(`\nRESULT: FAIL - ${fails.join(", ")}`);
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(400);
const doc = window.document;
check("the template designer mounted", Boolean(doc.querySelector(".tn-report-meta-row")),
      thrown[0] || "");

/** The "+ Add section…" picker. */
const addSelect = Array.from(doc.querySelectorAll("select"))
  .find((sel) => Array.from(sel.options).some((o) => /add section/i.test(o.textContent || "")));
check("the Add section picker is present", Boolean(addSelect));
if (!addSelect) {
  console.log(`\nRESULT: FAIL - ${fails.join(", ")}`);
  process.exit(2);
}

function setSelect(el, value) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
}

// Every type the picker offers, minus those that carry no data and no text
// tokens, so a window would mean nothing to them.
const NO_DATA = new Set(["spacer", "image", "page_break"]);
const offered = Array.from(addSelect.options)
  .map((o) => String(o.value || ""))
  .filter((v) => v && !/^__/.test(v) && !/add section/i.test(v));
check("the picker offers section types", offered.length > 0, offered.join(", "));

const missing = [];
const covered = [];
for (const type of offered) {
  // Remove the previous section so exactly one is open at a time.
  for (const del of doc.querySelectorAll(".tn-section-editor-tools button")) {
    if ((del.textContent || "").trim() === "×") {
      del.dispatchEvent(new window.Event("click", { bubbles: true }));
    }
  }
  await sleep(150);
  setSelect(addSelect, type);
  await sleep(220);
  // A collapsed section does not render its body at all (class
  // "tn-section-editor is-collapsed"), so the control is genuinely absent
  // from the DOM until it is opened. Expand only what is still collapsed -
  // clicking every toggle each round would close the ones already open.
  for (let pass = 0; pass < 3; pass += 1) {
    const shut = doc.querySelectorAll(".tn-section-editor.is-collapsed");
    if (!shut.length) break;
    for (const sec of shut) {
      const t = sec.querySelector(".tn-section-toggle");
      if (t) t.dispatchEvent(new window.Event("click", { bubbles: true }));
    }
    await sleep(200);
  }
  await sleep(180);
  if (NO_DATA.has(type)) continue;
  // "Start when" labels a <label> that also wraps a <select>, so it is not a
  // leaf node - an earlier version of this test looked only at childless
  // elements and reported every type as missing. Read the section's text.
  const open = doc.querySelector(".tn-section-editor:not(.is-collapsed)");
  const text = String(open?.textContent || "");
  const hasHead = /trigger/i.test(text);
  const hasStart = /start when/i.test(text);
  if (hasHead && hasStart) covered.push(type);
  else missing.push(`${type}${open ? "" : " (never expanded)"}`);
}

check("every data-bearing section offers a start/stop trigger",
      missing.length === 0,
      missing.length ? `no trigger for: ${missing.join(", ")}` : `covered: ${covered.join(", ")}`);

// The source half: one component, not five copies that can drift apart.
const src = fs.readFileSync(
  path.join(HERE, "src", "components", "Reports", "ReportTemplateDesigner.jsx"), "utf8");
check("  it comes from ONE shared component",
      src.includes("function SectionTrigger({"),
      "five inline copies would drift the moment one is edited");
check("  used by more than just the chart branch",
      src.split("<SectionTrigger").length - 1 >= 5,
      `${src.split("<SectionTrigger").length - 1} use sites`);

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
