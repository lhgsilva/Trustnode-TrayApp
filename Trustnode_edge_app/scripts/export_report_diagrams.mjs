import path from "node:path";
import { pathToFileURL } from "node:url";
import fs from "node:fs";
import { chromium } from "playwright";

const htmlPath = path.resolve("Trustnode_edge_app/docs/SCALABILITY_ARCHITECTURE_REPORT_2026-04-22_DESIGNED.html");
const outDir = path.resolve("Trustnode_edge_app/docs/assets");
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1800, height: 1300 } });
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
  try {
    await page.waitForFunction(() => window.__MERMAID_READY__ === true, { timeout: 30000 });
  } catch {}

  const cards = await page.$$(".diagram-card");
  let idx = 1;
  for (const card of cards) {
    const file = path.join(outDir, `topology_${idx}.png`);
    await card.screenshot({ path: file });
    idx += 1;
  }
  console.log(`Saved ${idx-1} diagram screenshots to ${outDir}`);
} finally {
  await browser.close();
}
