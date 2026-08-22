// Lite surface: do the new Tags / Batch / Assistant tabs actually render, and
// are they ABSENT when the person is not entitled?
// Runs against a THROWAWAY backend (API env), never the live install.
const { chromium } = require("D:/Trustnode/Trustnode-AB/Tray_app/node_modules/playwright");
const API = process.env.API || "http://127.0.0.1:8049";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
const LOGIN_LABEL = process.env.LOGIN_USER || "admin";
const check = (name, ok, detail = "") => {
  console.log(`  ${name.padEnd(56)}: ${ok ? "PASS" : "FAIL"}${detail ? " - " + String(detail).slice(0, 120) : ""}`);
  if (!ok) fails.push(name);
};

(async () => {
  const login = await (await fetch(`${API}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: process.env.LOGIN_USER || "admin", password: process.env.LOGIN_PASS || "admin" }),
  })).json();
  const token = login.token;
  check(`${LOGIN_LABEL} login`, !!token);
  if (!token) process.exit(2);

  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const errors = [];
  const nonGet = [];
  page.on("pageerror", (e) => errors.push(String(e.message)));
  // The Lite PWA registers /trustnode/lite/app/sw.js, which 404s under the app
  // mount. Pre-existing and unrelated to the tabs — tracked, not failed on.
  const SW_NOISE = /ServiceWorker|sw\.js|bad HTTP response code \(404\) was received when fetching the script/i;
  page.on("console", (m) => { if (m.type() === "error" && !SW_NOISE.test(m.text())) errors.push("console: " + m.text()); });
  // The harness's own sign-in POST is not the page's doing; everything else is.
  const badResponses = [];
  page.on("response", (r) => {
    if (r.status() >= 400) badResponses.push(`${r.status()} ${r.request().method()} ${r.url()}`);
  });
  page.on("request", (r) => {
    if (r.method() !== "GET" && r.url().includes("/api/") && !r.url().includes("/api/auth/login")) {
      nonGet.push(`${r.method()} ${r.url()}`);
    }
  });

  // sign in through the login surface so the HttpOnly session cookie is set
  await page.goto(`${API}/trustnode/login/`, { waitUntil: "domcontentloaded" });
  const LU = process.env.LOGIN_USER || "admin";
  const LP = process.env.LOGIN_PASS || "admin";
  await page.evaluate(async ([api, u, p]) => {
    await fetch(api + "/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ username: u, password: p }),
    });
  }, [API, LU, LP]);
  await page.evaluate((t) => localStorage.setItem("trustnode_auth_token", t), token);

  await page.goto(`${API}/trustnode/lite/app/`, { waitUntil: "domcontentloaded" });
  await sleep(6000);

  // Entitlement comes from the backend (fetched with the bearer token), not
  // from an in-page fetch that may not carry the session cookie.
  const caps = JSON.parse(process.env.CAPS || "{}");
  console.log("  capabilities:", JSON.stringify(caps));

  const navText = await page.evaluate(() =>
    Array.from(document.querySelectorAll("nav a, nav button, .nav a, .nav button, aside a, aside button"))
      .map((n) => (n.textContent || "").trim()).filter(Boolean));
  console.log("  nav:", JSON.stringify(navText));

  const want = [["Tags", caps.tags], ["Batch", caps.batch], ["Assistant", caps.intelligence]];
  for (const [label, entitled] of want) {
    const present = navText.some((t) => t.toLowerCase() === label.toLowerCase());
    check(`${label} tab present == entitled(${entitled})`, present === Boolean(entitled),
          `present=${present}`);
  }

  // click each entitled tab and make sure it renders without throwing
  for (const [label, entitled] of want) {
    if (!entitled) continue;
    const before = errors.length;
    const clicked = await page.evaluate((lbl) => {
      const el = Array.from(document.querySelectorAll("nav a, nav button, .nav a, .nav button, aside a, aside button"))
        .find((n) => (n.textContent || "").trim().toLowerCase() === lbl.toLowerCase());
      if (!el) return false;
      el.click(); return true;
    }, label);
    await sleep(3500);
    check(`${label} tab opens`, clicked, clicked ? "" : "nav item not clickable");
    const newErrs = errors.slice(before).filter((e) => !/ServiceWorker|sw\.js/i.test(e));
    check(`${label} renders without a page error`, newErrs.length === 0, newErrs.join(" | "));
    const body = await page.evaluate(() => (document.body.innerText || "").slice(0, 400));
    check(`${label} renders content (not blank)`, body.trim().length > 40, body.slice(0, 80));
    await page.screenshot({ path: `${process.env.SHOT_DIR}/lite_${process.env.SHOT_PREFIX || ""}${label.toLowerCase()}.png`, fullPage: false });
  }

  check("no non-GET API calls fired on render", nonGet.length === 0, nonGet.join(" | "));
  console.log("  failed API responses:", JSON.stringify(badResponses, null, 0));
  // The Lite PWA registers /lite/sw.js, a path that predates the
  // /trustnode/lite/app/ mount and 404s there. Pre-existing (see git HEAD),
  // harmless (the SW simply never installs), and deliberately left alone so a
  // rebuild cannot be served from a stale service-worker cache.
  const realErrors = errors.filter((e) => !/ServiceWorker|sw\.js/i.test(e));
  check("no page errors at all", realErrors.length === 0, realErrors.slice(0, 3).join(" | "));

  await browser.close();
  console.log();
  console.log(`RESULT: ${fails.length ? "FAIL - " + fails.join(", ") : "PASS"}`);
  process.exit(fails.length ? 2 : 0);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(3); });
