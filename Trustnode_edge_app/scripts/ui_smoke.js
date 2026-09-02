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
  // 2026-08-28: the new Diagnostics page. It renders psutil output and every
  // gateway's stamps, so it has more shapes to get wrong than most.
  ["SETTINGS", "diagnostics"],
  // 2026-08-29: the OEE dashboards. Machine Detail is reached by clicking a
  // machine, not from the menu, so it is exercised by the page's own render
  // rather than by this walk.
  ["OEE", "oee overview"],
  ["OEE", "planning calendar"],
  // 2026-08-29 UI review: the three pages the operator called out.
  ["SETTINGS", "interface"],
  ["POWER MANAGEMENT", "power configuration"],
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
  await sleep(8000);

  // Two gates, both optional. The injected token above is no longer enough on
  // its own: an unauthenticated visit redirects to /trustnode/login/, and the
  // React app then renders a login view of its own. Skipped silently when a
  // session already exists, so this still works against a live install.
  const user = process.env.VAL_USER || "admin-mari";
  const pass = process.env.VAL_PASS || "Limerick2019*";

  // Idempotent, and called AGAIN after the reload below: the reload can land
  // back on a login screen, and a walk that then reports sixteen missing nav
  // items reads exactly like sixteen broken pages. Retrying costs a few
  // seconds; a flaky check in the release gate costs trust in the gate.
  const ensureSignedIn = async () => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (await page.$("#tn-user")) {
        await page.fill("#tn-user", user);
        await page.fill("#tn-pass", pass);
        await page.click('button:has-text("Sign in")');
        await sleep(9000);
        continue;
      }
      if (await page.$('button:has-text("Sign in to TrustNode Edge")')) {
        const fields = await page.$$("input");
        if (fields.length < 2) return false;
        await fields[0].fill(user);
        await fields[1].fill(pass);
        await page.click('button:has-text("Sign in to TrustNode Edge")');
        await sleep(11000);
        continue;
      }
      return true;
    }
    return false;
  };

  if (!(await ensureSignedIn())) {
    console.log("  login                          : FAIL - still on a login screen");
    console.log("UI SMOKE: FAIL (login)");
    await browser.close();
    process.exit(2);
  }

  // The sidebar defaults to collapsed, which reduces every menu entry to a
  // two-letter stub - and a walk that clicks BY LABEL then finds nothing.
  await page.evaluate(() => {
    try { localStorage.setItem("trustnode_sidebar_collapsed_local", "false"); } catch (_) {}
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await sleep(9000);

  if (!(await ensureSignedIn())) {
    console.log("  login                          : FAIL - lost the session on reload");
    console.log("UI SMOKE: FAIL (login)");
    await browser.close();
    process.exit(2);
  }

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
