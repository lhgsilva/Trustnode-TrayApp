// The developer portal's licence editor: does the Control Plane page render,
// and does the licence modal show the new Package + Seats inputs?
// Playwright lives in the repo root's node_modules, not this folder; resolve it
// from PLAYWRIGHT_MODULE when set so the harness is not tied to one machine.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || "D:/Trustnode/Trustnode-AB/Tray_app/node_modules/playwright");
const API = process.env.API || "http://127.0.0.1:8055";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
const check = (n, ok, d = "") => { console.log(`  ${n.padEnd(50)}: ${ok ? "PASS" : "FAIL"}${d ? " - " + String(d).slice(0, 130) : ""}`); if (!ok) fails.push(n); };

(async () => {
  const token = (await (await fetch(`${API}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "admin" }),
  })).json()).token;
  check("admin login", !!token);
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await ctx.addCookies([{ name: "tn_session", value: token, domain: "127.0.0.1", path: "/" }]);
  await ctx.addInitScript((t) => { try { localStorage.setItem("trustnode_auth_token", t); } catch (_) {} }, token);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));

  // The portal is the SAME bundle, served by the VPS at /portal. This edge has
  // no such route, so serve the built index there and let its ./assets/* land
  // on the full-app mount — that makes isPortalOnly() true at mount, which is
  // the only way to render the Control Plane page.
  const fs = require("fs");
  await page.route((url) => url.pathname === "/portal", async (route) => {
    // Read the BUILT index from disk: fetching the mount without a session
    // follows a redirect chain and lands somewhere else entirely.
    let html = fs.readFileSync(
      "D:/Trustnode/Trustnode-AB/Tray_app/Trustnode_edge_app/frontend/dist/index.html", "utf-8");
    html = html.replace(/\.\/assets\//g, "/trustnode/full/app/assets/");
    await route.fulfill({ status: 200, contentType: "text/html", body: html });
  });
  await page.goto(`${API}/portal?backendUrl=${encodeURIComponent(API)}`, { waitUntil: "domcontentloaded" });
  await sleep(10000);

  const body = await page.evaluate(() => (document.body.innerText || ""));
  check("portal page renders", body.length > 50 && !body.includes("Frontend Error Recovered"), body.slice(0, 100));
  check("no page errors on the portal", errors.length === 0, errors.slice(0, 2).join(" | "));

  // walk to the LICENSES section first
  await page.evaluate(async () => {
    const el = [...document.querySelectorAll("button, a, li, div")]
      .find((n) => (n.innerText || "").trim().toUpperCase() === "LICENSES");
    if (el) { el.click(); await new Promise((r) => setTimeout(r, 1200)); }
  });
  await sleep(2500);
  const labels = await page.evaluate(() =>
    [...document.querySelectorAll("button")].map((b) => (b.innerText || "").trim()).filter(Boolean).slice(0, 40));
  console.log("  buttons:", JSON.stringify(labels));

  const opened = await page.evaluate(async () => {
    const btns = [...document.querySelectorAll("button")];
    // the create control is icon-only: it identifies itself by aria-label
    const gen = btns.find((b) => /add license/i.test(b.getAttribute("aria-label") || b.getAttribute("title") || ""));
    if (!gen) return false;
    gen.click();
    await new Promise((r) => setTimeout(r, 1200));
    return true;
  });
  if (opened) {
    const modal = await page.evaluate(() => {
      const card = document.querySelector(".modal-card");
      return card ? (card.innerText || "") : "";
    });
    check("licence modal opens", modal.length > 20, modal.slice(0, 80));
    check("modal offers a Package field", /package/i.test(modal), modal.slice(0, 200));
    check("modal offers the four seats", ["TrustNode Edge", "TrustNode Studio", "TrustNode View LAN", "TrustNode Cloud View"]
      .every((l) => modal.includes(l)), modal.replace(/\n/g, " | ").slice(0, 300));
  } else {
    check("licence modal reachable", false, "no Generate License button found");
  }
  const geom = await page.evaluate(() => {
    const c = document.querySelector(".modal-card");
    if (!c) return null;
    const r = c.getBoundingClientRect();
    return { top: Math.round(r.top), height: Math.round(r.height), scrollTop: c.scrollTop,
             scrollHeight: c.scrollHeight, viewport: window.innerHeight };
  });
  console.log("  modal geometry:", JSON.stringify(geom));
  check("modal top edge is on screen", geom && geom.top >= 0, JSON.stringify(geom));
  check("modal opens scrolled to its title", geom && geom.scrollTop === 0, JSON.stringify(geom));
  await page.screenshot({ path: `${process.env.SHOT_DIR}/portal_license_modal.png` });
  await browser.close();
  console.log();
  console.log(`RESULT: ${fails.length ? "FAIL - " + fails.join(", ") : "PASS"}`);
  process.exit(fails.length ? 2 : 0);
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(3); });
