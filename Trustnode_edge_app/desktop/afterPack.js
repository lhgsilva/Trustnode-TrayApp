// Stamp TrustNode's identity onto the packaged executable.
//
// 2026-08-31: a customer's Task Manager showed "Electron", published by
// "GitHub, Inc.", version 31.7.7 — because the shipped exe still carried
// Electron's own version resource. electron-builder normally rewrites that
// with rcedit, but the build sets `signAndEditExecutable: false`, which turns
// OFF both the signing and the editing. That flag is not a mistake: enabling
// it makes electron-builder extract the whole winCodeSign toolchain, which
// contains macOS symlinks that Windows refuses to create without elevation —
// the build fails outright ("Cannot create symbolic link ... libcrypto.dylib").
//
// So the editing is done here instead, with the same rcedit electron-builder
// would have used, and nothing is asked of the signing toolchain.
//
// If rcedit cannot be found this warns loudly and lets the build finish: a
// developer without the cache should still be able to build. The release gate
// checks the SHIPPED exe's metadata, so an unbranded build cannot quietly
// become a release.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const STRINGS = {
  CompanyName: "TrustNode",
  FileDescription: "TrustNode",
  ProductName: "TrustNode",
  LegalCopyright: "TrustNode",
  OriginalFilename: "TrustNode.exe",
  InternalName: "TrustNode",
};

function findRcedit() {
  const cache = path.join(
    process.env.LOCALAPPDATA || "", "electron-builder", "Cache", "winCodeSign");
  if (!fs.existsSync(cache)) return null;
  // Newest cache entry first: the toolchain is versioned by directory name.
  const dirs = fs.readdirSync(cache)
    .map((d) => path.join(cache, d))
    .filter((d) => { try { return fs.statSync(d).isDirectory(); } catch { return false; } })
    .sort()
    .reverse();
  for (const dir of dirs) {
    const exe = path.join(dir, "rcedit-x64.exe");
    if (fs.existsSync(exe)) return exe;
  }
  return null;
}

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "win32") return;

  const exeName = (context.packager.appInfo.productFilename || "TrustNode") + ".exe";
  const exePath = path.join(context.appOutDir, exeName);
  if (!fs.existsSync(exePath)) {
    console.warn(`[afterPack] ${exeName} not found in ${context.appOutDir} — skipping branding`);
    return;
  }

  const rcedit = findRcedit();
  if (!rcedit) {
    console.warn("[afterPack] rcedit not found; the exe will keep Electron's "
      + "version resource. The release gate checks this, so it cannot ship "
      + "unnoticed.");
    return;
  }

  const version = context.packager.appInfo.version || "0.1.0";
  const args = [exePath];
  for (const [key, value] of Object.entries(STRINGS)) {
    args.push("--set-version-string", key, value);
  }
  args.push("--set-file-version", version);
  args.push("--set-product-version", version);

  const icon = path.join(__dirname, "assets", "trustnode_logo.ico");
  if (fs.existsSync(icon)) args.push("--set-icon", icon);

  try {
    execFileSync(rcedit, args, { stdio: "pipe" });
    console.log(`[afterPack] branded ${exeName} as TrustNode ${version}`
      + (fs.existsSync(icon) ? " (with logo)" : ""));
  } catch (err) {
    // Loud, but not fatal: a failed stamp must not cost a working build.
    console.warn(`[afterPack] rcedit failed: ${String(err && err.message).slice(0, 200)}`);
  }
};
