import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const htmlPath = path.resolve("Trustnode_edge_app/docs/SCALABILITY_ARCHITECTURE_REPORT_2026-04-22_DESIGNED.html");
const pdfPath = path.resolve("Trustnode_edge_app/docs/SCALABILITY_ARCHITECTURE_REPORT_2026-04-22_DESIGNED.pdf");

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });

  try {
    await page.waitForFunction(() => window.__MERMAID_READY__ === true, { timeout: 30000 });
  } catch {
    // continue even if mermaid had partial render; PDF can still be generated.
  }

  await page.emulateMedia({ media: "print" });
  await page.pdf({
    path: pdfPath,
    format: "A4",
    printBackground: true,
    margin: {
      top: "10mm",
      right: "8mm",
      bottom: "10mm",
      left: "8mm"
    }
  });

  console.log(`PDF generated: ${pdfPath}`);
} finally {
  await browser.close();
}
