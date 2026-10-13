/**
 * Every report section must let the operator PICK a tag, not type one.
 *
 * 2026-10-09, reported: "in all the report sections I should be able to
 * pick the tag available and now I am having to enter manually, check for
 * all type of sections".
 *
 * Audited every branch. Most already had a picker - a TagTokenField on the
 * prose fields, gateway+tag selects on the data ones. Two did not: the
 * details table's CELLS and column headings, and the pill card's text box.
 * All three accept {TagName}, so the operator had to remember and type the
 * exact tag path.
 *
 * This walks the real editor, adds one of every section type, and fails if
 * a section offers no way to insert a tag. A source grep would not do: the
 * question is what the operator can reach on screen.
 *
 *   node frontend/test_tag_pickers_everywhere.mjs
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
  process.exit(2);
}

console.log("TrustNode - can every section pick a tag?");
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

// Types that carry no text and no data - a picker would mean nothing.
const NO_TAGS = new Set(["spacer", "page_break", "image"]);
const offered = [...addSel.options].map((o) => String(o.value || "")).filter(Boolean);
check("the picker lists section types", offered.length > 0, offered.join(", "));

const missing = [];
const covered = [];
for (const type of offered) {
  // One section at a time, so what is on screen belongs to this type.
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
  if (NO_TAGS.has(type)) continue;
  const open = doc.querySelector(".tn-section-editor:not(.is-collapsed)");
  if (!open) { missing.push(`${type} (never opened)`); continue; }
  // Either an insert-a-token button, or a dedicated tag dropdown.
  const inserts = open.querySelectorAll(".tn-tag-insert").length;
  const tagSelects = [...open.querySelectorAll("select")]
    .filter((sl) => [...sl.options].some((o) => /pick a tag|choose a tag/i.test(o.textContent || ""))).length;
  if (inserts + tagSelects > 0) covered.push(`${type}(${inserts}+${tagSelects})`);
  else missing.push(type);
}

check("every section offers a way to PICK a tag",
      missing.length === 0,
      missing.length ? `no picker in: ${missing.join(", ")}` : covered.join(" "));

// A section-level title picker would satisfy the check above even if every
// CELL still had to be typed - which was the actual complaint. Assert the
// row-level controls directly.
for (const del of doc.querySelectorAll(".tn-section-editor-tools button")) {
  if ((del.textContent || "").trim() === "×") {
    del.dispatchEvent(new window.Event("click", { bubbles: true }));
  }
}
await sleep(140);
selSetter.call(addSel, "static_table");
addSel.dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(250);
for (let i = 0; i < 3; i += 1) {
  const shut = doc.querySelectorAll(".tn-section-editor.is-collapsed");
  if (!shut.length) break;
  for (const sec of shut) {
    sec.querySelector(".tn-section-toggle")?.dispatchEvent(
      new window.Event("click", { bubbles: true }));
  }
  await sleep(180);
}
const rowPickers = [...doc.querySelectorAll(".tn-static-row")]
  .filter((r) => r.querySelector(".tn-tag-insert")).length;
check("a details-table ROW has a picker on its cells", rowPickers >= 2,
      `${rowPickers} row(s) with a picker - the cells were the complaint`);

const src = fs.readFileSync(
  path.join(HERE, "src", "components", "Reports", "ReportTemplateDesigner.jsx"), "utf8");
check("  the details table's cells use the inline picker",
      src.includes("onChange={(v) => setCell(r, c, v)}"),
      "they were plain inputs: the tag path had to be typed");
check("  so do its column headings",
      src.includes("onChange={(v) => setHeaderCell(c, v)}"));
check("  and the pill card's text box",
      src.includes('<TokenInput className="tn-pill-text"'));
check("  the inline picker inserts at the cursor, like the full one",
      src.includes("current.slice(0, el.selectionStart) + token"),
      "appending at the end would be useless mid-sentence");

console.log();
console.log(`RESULT: ${fails.length === 0 ? "PASS" : `FAIL - ${fails.join(", ")}`}`);
process.exit(fails.length === 0 ? 0 : 2);
