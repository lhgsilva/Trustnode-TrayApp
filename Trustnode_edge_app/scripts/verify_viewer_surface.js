// Consolidated viewer-surface proof. The sidebar is an ACCORDION: only one group
// is open at a time, so each group must be opened and read in turn.
const { chromium } = require("playwright");
const API = "http://127.0.0.1:8038";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FAILS = [];
const check = (n, ok, d = "") => { console.log(`  ${n.padEnd(52)}: ${ok ? "PASS" : "FAIL"}${d ? " — " + d : ""}`); if (!ok) FAILS.push(n); };

(async () => {
  const token = (await (await fetch(`${API}/api/auth/login`, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "lucas-like", password: "LucasLike-2026-aa" }) })).json()).token;
  const b = await chromium.launch({ headless: true });
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1100 } });
  await ctx.addCookies([{ name: "tn_session", value: token, domain: "127.0.0.1", path: "/" }]);
  await ctx.addInitScript((t) => { try {
    localStorage.setItem("trustnode_auth_token", t);
    localStorage.setItem("trustnode_sidebar_collapsed_local", "false"); } catch (_) {} }, token);
  const p = await ctx.newPage();
  const BENIGN = [/\/api\/lite-local\/check-access/, /\/api\/boot-probe/, /\/api\/auth\//];
  const writes = [];
  p.on("request", (r) => {
    if (["GET", "HEAD", "OPTIONS"].includes(r.method())) return;
    const u = r.url().replace(API, "");
    if (!BENIGN.some((re) => re.test(u))) writes.push(`${r.method()} ${u}`);
  });
  const errs = [];
  p.on("pageerror", (e) => errs.push(String(e).slice(0, 140)));

  await p.goto(`${API}/trustnode/client/app/`, { waitUntil: "domcontentloaded" });
  await sleep(25000);   // long enough for every autosave debounce to have fired

  const groups = await p.evaluate(() => [...document.querySelectorAll(".nav-group-btn")].map((e) => e.innerText.trim().split("\n")[0].trim()));
  const found = {};
  for (const g of groups) {
    try {
      await p.locator(".nav-group-btn", { hasText: g }).first().click({ force: true, timeout: 4000 });
      await sleep(600);
      found[g] = await p.evaluate(() => [...document.querySelectorAll(".nav-item")].map((e) => e.innerText.trim().replace(/\s+/g, " ")).filter(Boolean));
    } catch (_) { found[g] = []; }
  }
  const all = Object.values(found).flat().map((s) => s.toLowerCase());
  console.log("  nav groups :", JSON.stringify(groups));
  console.log("  nav items  :", JSON.stringify(found));
  console.log();
  check("Alarms visible despite the disagreeing key pair", all.includes("alarms"));
  check("Historian visible", all.includes("historian"));
  check("Logs hidden (admin-only)", !all.includes("logs"));
  check("Users and Access Control hidden", !all.some((x) => x.includes("users and access")));
  check("Database pages hidden", !all.some((x) => x.includes("database")));
  check("Remote Access / Connections hidden", !all.some((x) => x.includes("remote access") || x.includes("connections")));
  check("NO config writes in a 25 s session", writes.length === 0, writes.slice(0, 4).join(" | "));
  check("no page errors", errs.length === 0, errs.slice(0, 2).join(" | "));
  const logs = await fetch(`${API}/api/app-store/logs?limit=5`, { headers: { Authorization: `Bearer ${token}` } });
  check("server refuses the log API for a viewer", logs.status === 403, `status=${logs.status}`);
  await p.screenshot({ path: process.env.SHOT || "viewer_final.png" });
  await b.close();
  console.log(`\nRESULT: ${FAILS.length ? "FAIL — " + FAILS.join(", ") : "PASS"}`);
  process.exit(FAILS.length ? 2 : 0);
})().catch((e) => { console.log("crashed:", String(e).slice(0, 300)); process.exit(3); });
