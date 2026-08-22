// UI smoke test: load the built app and walk the main pages, failing on any
// page error or the error boundary. This is the check that would have caught
// "page is not defined" before it shipped.
const { chromium } = require("playwright");
const API = process.env.API || "http://127.0.0.1:8000";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PAGES = [
  ["OVERVIEW", "dashboard"],
  ["DATABASE AND BACKUP", "database overview"],
  ["DATABASE AND BACKUP", "backup and retention"],
  ["GATEWAY AND EDGE CONTROL", "gateway configuration"],
  ["DATA HISTORY", "historian"],
  ["CONNECTIONS", "remote access"],
  // 2026-08-22: the pages this release actually rewrote. A page that is never
  // opened is a page whose render errors ship.
  ["SETTINGS", "users and access control"],
  ["REPORTING", "reports"],
  ["REPORTING", "scheduled reports"],
  ["GATEWAY AND EDGE CONTROL", "tags"],
  ["DATA HISTORY", "logs"],
];

(async () => {
  const token = (await (await fetch(`${API}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: process.env.VAL_USER || "admin-mari", password: process.env.VAL_PASS || "Limerick2019*" }),
  })).json()).token;
  if (!token) { console.log("login failed"); process.exit(2); }

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await ctx.addCookies([{ name: "tn_session", value: token, domain: "127.0.0.1", path: "/" }]);
  await ctx.addInitScript((t) => {
    try {
      localStorage.setItem("trustnode_auth_token", t);
      localStorage.setItem("trustnode_sidebar_collapsed_local", "false");
    } catch (_) {}
  }, token);
  const page = await ctx.newPage();
  const fatal = [];
  page.on("pageerror", (e) => fatal.push(String(e).slice(0, 200)));

  await page.goto(`${API}/trustnode/full/app/?backendUrl=${encodeURIComponent(API)}`, { waitUntil: "domcontentloaded" });
  await sleep(10000);

  let failures = 0;
  const boundary = async () => page.evaluate(() => document.body.innerText.includes("Frontend Error Recovered"));
  if (await boundary()) { console.log("  initial load                   : FAIL — error boundary"); failures++; }
  else console.log("  initial load                   : PASS");
  if (fatal.length) { console.log("    page errors:", fatal.slice(0, 3)); failures++; }

  for (const [group, item] of PAGES) {
    fatal.length = 0;
    // Expand groups until the wanted item is visible, then click it. Clicking a
    // group that is already open collapses it, so probe before toggling.
    const opened = await page.evaluate(async ([g, it]) => {
      const norm = (e) => (e.innerText || "").trim().toLowerCase();
      const find = (label) => [...document.querySelectorAll("button")].find((b) => norm(b) === label);
      let target = find(it);
      if (!target) {
        const grp = [...document.querySelectorAll("button")]
          .find((b) => norm(b).replace(/\s*[+-]\s*$/, "") === g.toLowerCase());
        if (grp) {
          grp.click();
          await new Promise((r) => setTimeout(r, 600));
          target = find(it);
          if (!target) {           // it was open and we just closed it: reopen
            grp.click();
            await new Promise((r) => setTimeout(r, 600));
            target = find(it);
          }
        }
      }
      if (target) { target.click(); return true; }
      return false;
    }, [group, item]);
    await sleep(7000);
    const broke = await boundary();
    const ok = opened && !broke && fatal.length === 0;
    if (!ok) failures++;
    console.log(`  ${item.padEnd(30)} : ${ok ? "PASS" : "FAIL"}${opened ? "" : " (nav item not found)"}${broke ? " (error boundary)" : ""}${fatal.length ? " errors: " + fatal.slice(0, 2).join(" | ") : ""}`);
    if (broke) {
      await page.evaluate(() => { const b = [...document.querySelectorAll("button")].find((x) => /reload/i.test(x.innerText)); if (b) b.click(); });
      await sleep(8000);
    }
  }
  await page.screenshot({ path: process.env.SHOT || "ui_smoke.png" });
  await browser.close();
  console.log(`\nUI SMOKE: ${failures ? "FAIL (" + failures + ")" : "PASS"}`);
  process.exit(failures ? 2 : 0);
})().catch((e) => { console.log("crashed:", e && e.stack || e); process.exit(3); });
