// The read-only work must not strip an ADMIN's own controls on the desktop /
// full-app surface. Also checks the redesigned Users page renders the catalogue.
const { chromium } = require("playwright");
const API = "http://127.0.0.1:8038";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FAILS = [];
const check = (n, ok, d = "") => { console.log(`  ${n.padEnd(50)}: ${ok ? "PASS" : "FAIL"}${d ? " — " + d : ""}`); if (!ok) FAILS.push(n); };

(async () => {
  const token = (await (await fetch(`${API}/api/auth/login`, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: process.env.VAL_USER || "admin", password: process.env.VAL_PASS || "admin" }) })).json()).token;
  const b = await chromium.launch({ headless: true });
  const ctx = await b.newContext({ viewport: { width: 1600, height: 1050 } });
  await ctx.addCookies([{ name: "tn_session", value: token, domain: "127.0.0.1", path: "/" }]);
  await ctx.addInitScript((t) => { try {
    localStorage.setItem("trustnode_auth_token", t);
    localStorage.setItem("trustnode_sidebar_collapsed_local", "false"); } catch (_) {} }, token);
  const p = await ctx.newPage();
  const errs = []; p.on("pageerror", (e) => errs.push(String(e).slice(0, 140)));

  // FULL app surface (what the desktop and a Studio seat over LAN get)
  await p.goto(`${API}/trustnode/full/app/?backendUrl=${encodeURIComponent(API)}`, { waitUntil: "domcontentloaded" });
  await sleep(12000);

  const dash = await p.evaluate(() => {
    const txt = document.body.innerText;
    return {
      addWidget: /add widget/i.test(txt),
      boundary: txt.includes("Frontend Error Recovered"),
      buttons: [...document.querySelectorAll("button")].filter((b) => !b.disabled).length,
    };
  });
  check("admin: dashboard offers Add Widget (not read-only)", dash.addWidget, `enabled buttons=${dash.buttons}`);
  check("admin: no error boundary", !dash.boundary);

  // Users and Access Control: the redesigned page + catalogue-driven permissions
  await p.evaluate(async () => {
    const norm = (e) => (e.innerText || "").trim().toLowerCase();
    for (const g of [...document.querySelectorAll(".nav-group-btn")]) {
      if (norm(g).startsWith("settings")) { g.click(); await new Promise((r) => setTimeout(r, 700)); }
    }
    const it = [...document.querySelectorAll("button")].find((b) => norm(b).includes("users and access"));
    if (it) it.click();
  });
  await sleep(9000);

  const users = await p.evaluate(() => {
    const txt = document.body.innerText;
    return {
      boundary: txt.includes("Frontend Error Recovered"),
      seatLedger: /seat/i.test(txt),
      addUser: /add user/i.test(txt),
      groupsShown: [...document.querySelectorAll(".perm-catalog-group-header")].map(function (e) { return String(e.innerText || "").trim().split(String.fromCharCode(10))[0]; }).slice(0, 12),
      permRows: document.querySelectorAll(".perm-item, .perm-catalog-group-items > *").length,
    };
  });
  check("users page renders (no boundary)", !users.boundary);
  check("seat ledger present", users.seatLedger);
  check("Add User action present", users.addUser);
  console.log("     catalogue groups visible on the page:", JSON.stringify(users.groupsShown));

  // open the create-user modal and measure the permission layout
  await p.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /add user/i.test(x.innerText || ""));
    if (b) b.click();
  });
  await sleep(2500);
  const modal = await p.evaluate(() => {
    const rows = [...document.querySelectorAll(".perm-item")];
    const sameLine = rows.filter((r) => {
      const cb = r.querySelector("input[type=checkbox]");
      if (!cb) return false;
      const rb = r.getBoundingClientRect(), cbb = cb.getBoundingClientRect();
      return Math.abs((rb.top + rb.height / 2) - (cbb.top + cbb.height / 2)) < 14 && rb.height <= 44;
    });
    return {
      total: rows.length,
      sameLine: sameLine.length,
      avgHeight: rows.length ? Math.round(rows.reduce((a, r) => a + r.getBoundingClientRect().height, 0) / rows.length) : 0,
      unlicensedShown: document.querySelectorAll(".perm-unlicensed, .perm-hint-badge").length,
    };
  });
  console.log(`     permission rows: ${modal.total}, checkbox+label on one line: ${modal.sameLine}, avg row height: ${modal.avgHeight}px, unlicensed hints: ${modal.unlicensedShown}`);
  check("permission rows render", modal.total >= 20, `${modal.total}`);
  check("checkbox and label share a row", modal.total > 0 && modal.sameLine === modal.total, `${modal.sameLine}/${modal.total}`);
  check("rows are compact (<= 44px)", modal.avgHeight > 0 && modal.avgHeight <= 44, `${modal.avgHeight}px`);
  check("no page errors", errs.length === 0, errs.slice(0, 2).join(" | "));

  await p.screenshot({ path: process.env.SHOT || "admin_users.png" });
  await b.close();
  console.log(`\nRESULT: ${FAILS.length ? "FAIL — " + FAILS.join(", ") : "PASS"}`);
  process.exit(FAILS.length ? 2 : 0);
})().catch((e) => { console.log("crashed:", String(e).slice(0, 300)); process.exit(3); });
