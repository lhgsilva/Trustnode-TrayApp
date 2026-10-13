// Screenshot the dashboard widget configuration, for a design review.
//
// Points at whatever API is given. The caller MUST start an isolated backend
// on a scratch workspace: the 2026-08-22 incident was a headless browser
// driven against the LIVE install, which blanked three dashboard widgets.
// Never aim this at port 8000.
const { chromium } = require("playwright");

const API = process.env.API;
const OUT = process.env.SHOT || "widget_config.png";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!API) {
  console.log("API not set - refusing to guess (it must NOT be the live install)");
  process.exit(2);
}

(async () => {
  const token = (await (await fetch(`${API}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: process.env.VAL_USER || "admin",
      password: process.env.VAL_PASS || "admin",
    }),
  })).json()).token;
  if (!token) { console.log("login failed"); process.exit(2); }

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1680, height: 1150 } });
  await ctx.addCookies([{ name: "tn_session", value: token, domain: "127.0.0.1", path: "/" }]);
  await ctx.addInitScript((t) => {
    try {
      localStorage.setItem("trustnode_auth_token", t);
      localStorage.setItem("trustnode_sidebar_collapsed_local", "false");
    } catch (_) {}
  }, token);
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e).slice(0, 160)));

  await page.goto(`${API}/trustnode/full/app/?backendUrl=${encodeURIComponent(API)}`,
                  { waitUntil: "domcontentloaded" });
  await sleep(9000);
  if (await page.$("#tn-user")) {
    await page.fill("#tn-user", process.env.VAL_USER || "admin");
    await page.fill("#tn-pass", process.env.VAL_PASS || "admin");
    await page.click('button:has-text("Sign in")');
    await sleep(9000);
  }

  // Dashboard -> Add Widget -> pick a line chart -> Configure.
  const clickText = async (txt) => {
    const el = await page.$(`text=${txt}`);
    if (el) { await el.click(); await sleep(1200); return true; }
    return false;
  };
  await clickText("DASHBOARD");
  await sleep(2500);

  // The add button is the "+" pill in the dashboard toolbar.
  const addBtn = await page.$('button:has-text("Add Widget")')
    || await page.$(".dashboard-pill:has-text('+')")
    || await page.$('button[title*="Add"]');
  if (addBtn) { await addBtn.click(); await sleep(1800); }

  // Choose a chart type so the Series card has something to show.
  await clickText("Line Chart");
  await sleep(1000);
  await clickText("Configure");
  await sleep(2000);

  // Optionally open the nested Series/Axis popup as well, so its format can be
  // compared with the Configure tab it was meant to replace.
  if (process.env.OPEN) {
    const b = await page.$(`button:has-text("${process.env.OPEN}")`);
    if (b) { await b.click(); await sleep(2200); }
  }

  const cards = await page.$$(".modal-card");
  const modal = cards.length ? cards[cards.length - 1] : null;
  if (modal) {
    await modal.screenshot({ path: OUT });
    console.log(`captured the modal -> ${OUT}`);
  } else {
    await page.screenshot({ path: OUT, fullPage: false });
    console.log(`modal not found; captured the page -> ${OUT}`);
  }
  console.log(errs.length ? `page errors: ${errs.join(" | ")}` : "no page errors");
  await browser.close();
})();
