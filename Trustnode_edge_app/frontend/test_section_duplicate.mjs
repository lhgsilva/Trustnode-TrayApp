/**
 * Duplicating a report section must place the copy RIGHT AFTER the original
 * and leave the two independent.
 *
 * 2026-10-09, requested: "we also should have a option to duplicate the
 * section and to be added right after the original one to make easy the
 * things". Building six near-identical cycle charts meant configuring each
 * one from scratch.
 *
 * The part worth a test is not the button: it is that the copy is a
 * SEPARATE thing. The editor addresses nested items by id -
 * updateSeries(s.id, ...), updatePill(pill.id, ...) - so a duplicate that
 * reused those ids would edit both sections at once, and the operator would
 * watch the original change while typing into the copy. Every id inside is
 * regenerated; this drives the real editor to prove it.
 *
 *   node frontend/test_section_duplicate.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.join(HERE, "dist_designer", "designer.js");

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(58)}: ${ok ? "PASS" : "FAIL"}${detail ? ` - ${String(detail).slice(0, 140)}` : ""}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(BUNDLE)) {
  console.log(`  harness bundle missing at ${BUNDLE}`);
  console.log("  build it: npx vite build --config vite.designer.config.mjs (in frontend/)");
  process.exit(2);
}

console.log("TrustNode - duplicating a report section");
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
const setText = (el, v) => {
  const proto = el instanceof window.HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
};
const expandAll = async () => {
  for (let i = 0; i < 3; i += 1) {
    const shut = doc.querySelectorAll(".tn-section-editor.is-collapsed");
    if (!shut.length) break;
    for (const sec of shut) {
      sec.querySelector(".tn-section-toggle")?.dispatchEvent(
        new window.Event("click", { bubbles: true }));
    }
    await sleep(200);
  }
};
const editors = () => [...doc.querySelectorAll(".tn-section-editor")];
const kindOf = (e) => (e.querySelector(".tn-section-pill")?.textContent || "?").trim();
const firstPillLabel = (e) => e.querySelector(".tn-pill-row input")?.value;

await sleep(400);
const addSel = [...doc.querySelectorAll("select")]
  .find((s) => [...s.options].some((o) => /add section/i.test(o.textContent || "")));
check("the designer mounted", Boolean(addSel));
if (!addSel) { console.log(`\nRESULT: FAIL - ${fails.join(", ")}`); process.exit(2); }

for (const t of ["pill_card", "line_chart"]) {
  selSetter.call(addSel, t);
  addSel.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(250);
}
check("two sections were added", editors().length === 2,
      editors().map(kindOf).join(" | "));

const dupButtons = [...doc.querySelectorAll("button")]
  .filter((b) => (b.title || "").toLowerCase().includes("duplicate"));
check("every section offers Duplicate", dupButtons.length === 2, dupButtons.length);

// Duplicate the FIRST one; the copy must land second, not last.
dupButtons[0].dispatchEvent(new window.Event("click", { bubbles: true }));
await sleep(350);
const order = editors().map(kindOf);
check("the copy lands directly after the original",
      order.length === 3 && order[0] === order[1] && order[2] !== order[1],
      order.join(" | "));

await expandAll();
const before = editors();
check("the copy starts identical to the original",
      firstPillLabel(before[0]) === firstPillLabel(before[1]),
      `${firstPillLabel(before[0])} / ${firstPillLabel(before[1])}`);

setText(before[1].querySelector(".tn-pill-row input"), "CHANGED-ON-COPY");
await sleep(300);
const after = editors();
check("editing the COPY does not touch the original",
      firstPillLabel(after[0]) === "Result"
      && firstPillLabel(after[1]) === "CHANGED-ON-COPY",
      `original=${firstPillLabel(after[0])} copy=${firstPillLabel(after[1])}`);

const src = fs.readFileSync(
  path.join(HERE, "src", "components", "Reports", "ReportTemplateDesigner.jsx"), "utf8");
check("  because every nested id is regenerated",
      src.includes("function reidSection(") && src.includes('makeId("dup")'),
      "sharing a series/pill id would edit both sections at once");

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
