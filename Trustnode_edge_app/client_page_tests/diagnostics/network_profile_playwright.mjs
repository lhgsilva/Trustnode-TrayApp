import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const URLS = [
  'https://trustnode.lsapps.app/client/client_test.html',
  'https://trustnode.lsapps.app/client/client_test.php',
  'https://trustnode.lsapps.app/client/client_test_db_rest.html',
  'https://trustnode.lsapps.app/client/client_test_db_php.html',
  'https://trustnode.lsapps.app/client/client_test_db.php',
];

const USERNAME = process.env.TRUSTNODE_DIAG_USER || 'admin';
const PASSWORD = process.env.TRUSTNODE_DIAG_PASS || 'admin';
const CAPTURE_SECONDS = Number(process.env.TRUSTNODE_CAPTURE_SECONDS || '25');
const OUT_DIR = path.resolve(process.cwd(), 'Trustnode_edge_app', 'client_page_tests', 'diagnostics', 'output');
const OUT_FILE = path.join(OUT_DIR, 'playwright_profile.json');

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function tryLogin(page) {
  const result = { login_attempted: false, login_success: false };
  try {
    const userInput = page.locator('input[placeholder*="Username" i], input[name="username" i]').first();
    const passInput = page.locator('input[placeholder*="Password" i], input[name="password" i], input[type="password"]').first();
    if (!(await userInput.isVisible({ timeout: 2500 }).catch(() => false))) {
      return result;
    }
    result.login_attempted = true;
    await userInput.fill(USERNAME);
    await passInput.fill(PASSWORD);
    const signInBtn = page.locator('button:has-text("Sign In"), button:has-text("Login")').first();
    await signInBtn.click({ timeout: 3000 }).catch(() => {});
    await sleep(3500);
    const stillLogin = await page.locator('button:has-text("Sign In"), button:has-text("Login")').first().isVisible({ timeout: 1500 }).catch(() => false);
    result.login_success = !stillLogin;
  } catch {
    // no-op
  }
  return result;
}

function topPathEntries(mapObj, maxItems = 12) {
  return Object.entries(mapObj)
    .sort((a, b) => b[1] - a[1])
    .slice(0, maxItems)
    .map(([k, v]) => ({ path: k, count: v }));
}

async function profileUrl(browser, url) {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();

  let requestCount = 0;
  let failedRequestCount = 0;
  let responseBytes = 0;
  let websocketCount = 0;
  const byPath = {};

  page.on('websocket', () => {
    websocketCount += 1;
  });

  page.on('requestfailed', (req) => {
    failedRequestCount += 1;
    try {
      const u = new URL(req.url());
      byPath[u.pathname] = (byPath[u.pathname] || 0) + 1;
    } catch {
      // ignore
    }
  });

  page.on('response', async (resp) => {
    requestCount += 1;
    try {
      const u = new URL(resp.url());
      byPath[u.pathname] = (byPath[u.pathname] || 0) + 1;
      const headers = resp.headers();
      if (headers['content-length']) {
        responseBytes += Number(headers['content-length']) || 0;
      } else {
        const body = await resp.body().catch(() => null);
        if (body) responseBytes += body.length;
      }
    } catch {
      // ignore
    }
  });

  let navStatus = 'ok';
  let login = { login_attempted: false, login_success: false };

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(2000);
    login = await tryLogin(page);
    await sleep(CAPTURE_SECONDS * 1000);
  } catch (e) {
    navStatus = `error:${String(e?.message || e)}`;
  }

  await context.close();

  return {
    url,
    nav_status: navStatus,
    ...login,
    request_count: requestCount,
    failed_request_count: failedRequestCount,
    response_bytes: responseBytes,
    websocket_count: websocketCount,
    top_paths: topPathEntries(byPath),
  };
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = [];

  for (const url of URLS) {
    console.log(`Profiling: ${url}`);
    const one = await profileUrl(browser, url);
    results.push(one);
  }

  await browser.close();

  const payload = {
    generated_at: new Date().toISOString(),
    capture_seconds: CAPTURE_SECONDS,
    results,
  };

  fs.writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2), 'utf-8');
  console.log(`Playwright profile saved: ${OUT_FILE}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
