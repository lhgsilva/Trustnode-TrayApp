// Responsive end-to-end test of the client view portal.
// Drives a real headless Chromium at three viewport sizes (phone, tablet,
// desktop), logs in with the smoke credentials, and reports:
//   - DOM-ready / load-event timings
//   - login submit to home-screen render time
//   - whether the bottom nav (.client-mobile-nav) is visible at each size
//   - a screenshot per viewport
//
// Output:
//   diagnostics/output/responsive_report.json
//   diagnostics/output/responsive_<viewport>.png

import { chromium, devices } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const URL = process.env.TRUSTNODE_CLIENT_URL || 'https://trustnode.lsapps.app/client/client_view.html';
const USER = process.env.TRUSTNODE_USER || 'acmeadmin20260515111327';
const PASS = process.env.TRUSTNODE_PASS || 'AcmeAdmin!20260515111327';
const OUT_DIR = path.resolve(process.cwd(), 'Trustnode_edge_app', 'client_page_tests', 'diagnostics', 'output');

const VIEWPORTS = [
  // name, width, height, expect_bottom_nav
  { name: 'phone',   width: 390,  height: 844,  expect_bottom_nav: true,  ua: devices['iPhone 13'].userAgent, deviceScaleFactor: 3, isMobile: true,  hasTouch: true },
  { name: 'tablet',  width: 1024, height: 1366, expect_bottom_nav: false, ua: devices['iPad (gen 7)'].userAgent, deviceScaleFactor: 2, isMobile: true,  hasTouch: true },
  { name: 'desktop', width: 1440, height: 900,  expect_bottom_nav: false, ua: undefined, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
];

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function probe(browser, vp) {
  const ctxOpts = {
    viewport: { width: vp.width, height: vp.height },
    deviceScaleFactor: vp.deviceScaleFactor,
    isMobile: vp.isMobile,
    hasTouch: vp.hasTouch,
  };
  if (vp.ua) ctxOpts.userAgent = vp.ua;
  const context = await browser.newContext(ctxOpts);
  const page = await context.newPage();

  const result = {
    viewport: vp.name,
    width: vp.width,
    height: vp.height,
    timings: {},
    bottom_nav_visible: null,
    bottom_nav_expected: vp.expect_bottom_nav,
    bottom_nav_match: null,
    login_success: false,
    errors: [],
    console_errors: [],
  };

  page.on('pageerror', (err) => result.errors.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') result.console_errors.push(msg.text().slice(0, 200));
  });

  try {
    // ---- Load timing ---------------------------------------------------
    const navStart = Date.now();
    await page.goto(URL, { waitUntil: 'load', timeout: 60_000 });
    result.timings.goto_ms = Date.now() - navStart;

    // Wait until React mounts and the login form appears (or the home if a
    // cached session survived).
    await page.waitForFunction(() => document.body && document.body.children.length > 0, { timeout: 30_000 });
    result.timings.dom_first_content_ms = Date.now() - navStart;

    // ---- Login -------------------------------------------------------
    // The actual login form uses placeholders "e.g. m.silva@plant.io" and
    // "Enter your password", and the submit button text is
    // "Sign in to Trusnode Client View". Match against any of those.
    const userField = page
      .locator(
        'input[placeholder*="silva" i], input[placeholder*="username" i], input[placeholder*="email" i], input[type="text"]:not([readonly])'
      )
      .first();
    const passField = page.locator('input[type="password"]').first();
    const userVisible = await userField.isVisible({ timeout: 15_000 }).catch(() => false);

    if (userVisible) {
      const loginStart = Date.now();
      // Use real keystrokes so React's onChange fires reliably. .fill() can
      // skip the change event on some controlled inputs.
      await userField.click();
      await userField.pressSequentially(USER, { delay: 8 });
      await passField.click();
      await passField.pressSequentially(PASS, { delay: 8 });
      // The login form has TWO buttons matching /Login/i: an .auth-tab toggle
      // at the top and the actual submit button .auth-submit at the bottom.
      // Always target the submit button explicitly.
      const signInBtn = page.locator('button.auth-submit').first();
      await signInBtn.click({ timeout: 5_000 }).catch(async () => {
        // Fallback to text-based locator for older builds.
        await page.locator('button:has-text("Sign in to")').first().click({ timeout: 5_000 }).catch(() => {});
      });
      // Wait for any plausible post-login marker. After login the login
      // form disappears (.auth-card removed) and the app shell renders.
      const success = await Promise.race([
        page.locator('.client-mobile-nav').waitFor({ state: 'visible', timeout: 30_000 }).then(() => 'mobile_nav'),
        page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: 30_000 }).then(() => 'sidebar'),
        page.locator('main.content').waitFor({ state: 'visible', timeout: 30_000 }).then(() => 'main_content'),
        page.waitForFunction(() => !document.querySelector('.auth-card'), null, { timeout: 30_000 }).then(() => 'auth_card_gone'),
      ]).catch(() => null);
      result.timings.login_to_app_ms = Date.now() - loginStart;
      result.login_success = success !== null;
      result.login_path = success;
      // Capture login API responses for debugging.
      const loginErr = await page.locator('.error').first().textContent({ timeout: 1500 }).catch(() => null);
      if (loginErr) result.login_error_text = loginErr;
    } else {
      result.errors.push('Login form not visible within 15s');
    }

    // ---- Check bottom nav presence -----------------------------------
    const navHandle = await page.$('.client-mobile-nav');
    if (navHandle) {
      const isVisible = await navHandle.isVisible();
      result.bottom_nav_visible = isVisible;
    } else {
      result.bottom_nav_visible = false;
    }
    result.bottom_nav_match = (result.bottom_nav_visible === vp.expect_bottom_nav);

    // ---- Idle measurement --------------------------------------------
    await page.waitForLoadState('networkidle', { timeout: 30_000 }).catch(() => {});
    result.timings.network_idle_ms = Date.now() - navStart;

    // ---- Screenshot -------------------------------------------------
    const png = path.join(OUT_DIR, `responsive_${vp.name}.png`);
    await page.screenshot({ path: png, fullPage: false });
    result.screenshot = path.relative(process.cwd(), png);
  } catch (err) {
    result.errors.push(`fatal: ${err && err.message ? err.message : String(err)}`);
  } finally {
    await context.close();
  }

  return result;
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  console.log(`Responsive test target: ${URL}`);
  const browser = await chromium.launch({ headless: true });
  const all = [];
  for (const vp of VIEWPORTS) {
    console.log(`\n--- ${vp.name} ${vp.width}x${vp.height} ---`);
    const result = await probe(browser, vp);
    all.push(result);
    console.log(JSON.stringify(result.timings, null, 2));
    console.log(`bottom_nav: visible=${result.bottom_nav_visible} expected=${result.bottom_nav_expected} match=${result.bottom_nav_match}`);
    console.log(`login_success=${result.login_success} login_path=${result.login_path || '-'}`);
    if (result.errors.length) console.log('errors:', result.errors);
    if (result.console_errors.length) console.log('console_errors:', result.console_errors.slice(0, 4));
  }
  await browser.close();

  const reportPath = path.join(OUT_DIR, 'responsive_report.json');
  fs.writeFileSync(reportPath, JSON.stringify({ url: URL, generated_utc: new Date().toISOString(), results: all }, null, 2));
  console.log(`\nReport written: ${reportPath}`);

  // exit nonzero if any viewport mismatched bottom-nav expectation OR login failed
  const failed = all.filter((r) => r.bottom_nav_match === false || !r.login_success);
  if (failed.length) {
    console.error(`FAIL: ${failed.length} viewport(s) had issues.`);
    process.exit(1);
  } else {
    console.log('PASS: all viewports OK.');
  }
}

main().catch((err) => {
  console.error('script error:', err);
  process.exit(2);
});
