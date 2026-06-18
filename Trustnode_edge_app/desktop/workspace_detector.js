// Workspace detector (operator 2026-06-18).
//
// First-launch UX problem: customers updating the EXE were terrified of
// losing their dashboards, users, gateway configs and license. The data
// IS preserved at the file level (SQLite under %ProgramData%\TrustNode\
// or one of two legacy paths) but nothing tells the customer that.
//
// This module runs ONCE BEFORE the splash window on every fresh install
// of the tray. It scans the known data locations, finds any existing
// workspace, and presents a dialog letting the operator pick:
//
//   1. "Continue with this workspace" — use the detected files in place.
//      Sets TRUSTNODE_DATA_DIR for the backend spawn so the existing
//      database is read/written as-is. One-time backup copy is made
//      (.backup-pre-update-{date}.db) as cheap insurance against a bad
//      schema migration.
//
//   2. "Backup the old one and start fresh" — renames the existing DB
//      to .backup-fresh-{date}.db and starts with an empty workspace.
//      Customer can manually restore the backup later if needed.
//
// "Start completely fresh" (destructive wipe) is intentionally NOT in
// this dialog — it lives in Settings → Reset Workspace with a
// type-DELETE confirm so it can't be hit by accident.
//
// The user's choice is persisted to:
//   %APPDATA%\trustnode-edge-desktop\workspace-choice.json
// On subsequent launches, if that file already exists AND the chosen
// path still has a database, the dialog is skipped entirely.
//
// Exports:
//   detectAndChooseWorkspace({ userDataDir, electron }) -> Promise<{
//     dataDir: string,            // absolute path with trailing slash
//     fresh: boolean,             // true if no existing data was found OR user chose to start fresh
//     usedExisting: boolean,      // true if we kept an existing workspace
//     backupPath: string | null,  // path of the backup we created, if any
//     detectedPaths: string[],    // every workspace we discovered
//   }>

const fs = require("fs");
const path = require("path");
const os = require("os");

// Order matters: the FIRST writable candidate becomes the default when
// nothing exists. Detection walks all of them looking for existing data.
function candidatePaths() {
  const candidates = [];
  if (process.platform === "win32") {
    const programData = process.env.PROGRAMDATA || "C:\\ProgramData";
    const localAppData = process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || os.homedir(), "AppData", "Local");
    candidates.push({
      label: "Current (ProgramData)",
      dir: path.join(programData, "TrustNode", "edge"),
      legacy: false,
    });
    candidates.push({
      label: "Legacy (LocalAppData)",
      dir: path.join(localAppData, "TrustNode", "data"),
      legacy: true,
    });
    candidates.push({
      label: "Oldest (~/.trustnode_edge)",
      dir: path.join(os.homedir(), ".trustnode_edge", "data"),
      legacy: true,
    });
  } else {
    candidates.push({
      label: "Default",
      dir: path.join(os.homedir(), ".trustnode_edge", "data"),
      legacy: false,
    });
  }
  return candidates;
}

function fileSizeSafe(p) {
  try { return fs.statSync(p).size; } catch (_) { return 0; }
}

function fileMtimeSafe(p) {
  try { return fs.statSync(p).mtimeMs; } catch (_) { return 0; }
}

// A workspace is "detected" when trustnode_app_store.db exists with a
// non-trivial size (>32 KB filters out empty stub DBs from old crashes).
function detectWorkspaces() {
  const found = [];
  for (const c of candidatePaths()) {
    const dbPath = path.join(c.dir, "trustnode_app_store.db");
    const size = fileSizeSafe(dbPath);
    if (size > 32 * 1024) {
      found.push({
        ...c,
        dbPath,
        sizeBytes: size,
        sizeMb: Math.round(size / (1024 * 1024) * 10) / 10,
        mtimeMs: fileMtimeSafe(dbPath),
      });
    }
  }
  // Sort newest-first so the dialog highlights the most-recently-used.
  found.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return found;
}

function readPersistedChoice(userDataDir) {
  try {
    const fp = path.join(userDataDir, "workspace-choice.json");
    if (!fs.existsSync(fp)) return null;
    const data = JSON.parse(fs.readFileSync(fp, "utf8"));
    if (!data || typeof data !== "object") return null;
    const dir = String(data.dataDir || "").trim();
    if (!dir) return null;
    // Verify the chosen DB still exists; if the customer wiped it
    // manually between launches, we re-prompt instead of silently
    // creating a new empty one in a stale location.
    const dbPath = path.join(dir, "trustnode_app_store.db");
    if (!fs.existsSync(dbPath)) return null;
    return { dataDir: dir, savedAt: String(data.savedAt || "") };
  } catch (_) {
    return null;
  }
}

function writePersistedChoice(userDataDir, dataDir) {
  try {
    fs.mkdirSync(userDataDir, { recursive: true });
    const fp = path.join(userDataDir, "workspace-choice.json");
    fs.writeFileSync(
      fp,
      JSON.stringify({
        dataDir,
        savedAt: new Date().toISOString(),
        format: "trustnode.workspace-choice",
        format_version: 1,
      }, null, 2),
      "utf8",
    );
  } catch (_) {
    // Non-fatal — we'll just re-prompt next launch.
  }
}

function timestampSlug() {
  const d = new Date();
  return [
    d.getUTCFullYear(),
    String(d.getUTCMonth() + 1).padStart(2, "0"),
    String(d.getUTCDate()).padStart(2, "0"),
    "-",
    String(d.getUTCHours()).padStart(2, "0"),
    String(d.getUTCMinutes()).padStart(2, "0"),
  ].join("");
}

function makeSnapshotCopy(dbPath, suffix) {
  try {
    const dir = path.dirname(dbPath);
    const base = path.basename(dbPath, path.extname(dbPath));
    const target = path.join(dir, `${base}.${suffix}-${timestampSlug()}.db`);
    if (fs.existsSync(target)) return target; // already taken this minute
    fs.copyFileSync(dbPath, target);
    return target;
  } catch (_) {
    return null;
  }
}

function ensureDir(dir) {
  try { fs.mkdirSync(dir, { recursive: true }); return true; } catch (_) { return false; }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

async function detectAndChooseWorkspace({ userDataDir, electron }) {
  // 1. Saved choice from a previous launch wins, no dialog.
  const saved = readPersistedChoice(userDataDir);
  if (saved) {
    return {
      dataDir: saved.dataDir,
      fresh: false,
      usedExisting: true,
      backupPath: null,
      detectedPaths: [saved.dataDir],
      reason: "persisted-choice",
    };
  }

  const found = detectWorkspaces();

  // 2. Nothing detected → fresh install, no prompt.
  if (found.length === 0) {
    const fallback = candidatePaths()[0].dir;
    ensureDir(fallback);
    writePersistedChoice(userDataDir, fallback);
    return {
      dataDir: fallback,
      fresh: true,
      usedExisting: false,
      backupPath: null,
      detectedPaths: [],
      reason: "no-existing-data",
    };
  }

  // 3. Existing data found → ask the user. We use Electron's built-in
  //    `dialog.showMessageBox` so we don't need a renderer window — keeps
  //    the prompt visible even if the splash hasn't rendered yet.
  const { dialog } = electron;
  const newest = found[0];
  const sizeText = newest.sizeMb >= 1 ? `${newest.sizeMb} MB` : `${(newest.sizeBytes / 1024).toFixed(0)} KB`;
  const mtimeText = newest.mtimeMs ? new Date(newest.mtimeMs).toLocaleString() : "unknown";
  const otherLocations = found.length > 1
    ? `\n\nOther workspaces were also found:\n` + found.slice(1).map((f) => `  • ${f.dir} (${f.sizeMb} MB)`).join("\n")
    : "";

  const result = await dialog.showMessageBox({
    type: "question",
    title: "TrustNode workspace detected",
    message: "An existing TrustNode workspace was found on this machine.",
    detail:
      `Location: ${newest.dir}\n` +
      `Size: ${sizeText}\n` +
      `Last modified: ${mtimeText}\n\n` +
      `This workspace contains your users, dashboards, gateway configurations, alarms, ` +
      `report templates and license activation. Choose how to proceed:` +
      otherLocations,
    buttons: [
      "Continue with this workspace",
      "Back up and start fresh",
      "Cancel",
    ],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  });

  if (result.response === 2) {
    // User cancelled — quit gracefully. The caller treats this as a hard exit.
    return null;
  }

  if (result.response === 0) {
    // Continue with existing. One-time backup as insurance.
    ensureDir(newest.dir);
    const backupPath = makeSnapshotCopy(newest.dbPath, "backup-pre-update");
    writePersistedChoice(userDataDir, newest.dir);
    return {
      dataDir: newest.dir,
      fresh: false,
      usedExisting: true,
      backupPath,
      detectedPaths: found.map((f) => f.dir),
      reason: "user-continued-existing",
    };
  }

  // response === 1 → back up and start fresh.
  // Rename the existing DB so the backend creates a new empty one alongside.
  const backupPath = makeSnapshotCopy(newest.dbPath, "backup-pre-fresh");
  try {
    // Move the live DB out of the way. Keep WAL/SHM siblings together so a
    // restore is a single file move back.
    for (const suffix of ["", "-wal", "-shm"]) {
      const live = `${newest.dbPath}${suffix}`;
      if (fs.existsSync(live)) {
        const sidekick = live.replace(/\.db(-wal|-shm)?$/i, `.cleared-${timestampSlug()}.db$1`);
        try { fs.renameSync(live, sidekick); } catch (_) {}
      }
    }
  } catch (_) {}
  ensureDir(newest.dir);
  writePersistedChoice(userDataDir, newest.dir);
  return {
    dataDir: newest.dir,
    fresh: true,
    usedExisting: false,
    backupPath,
    detectedPaths: found.map((f) => f.dir),
    reason: "user-started-fresh-with-backup",
  };
}

// Operator 2026-06-18: clears the backend's activation-receipt mirror
// in the Windows registry. Otherwise a workspace reset followed by a
// relaunch would immediately re-restore the license from the registry,
// defeating the "clean slate" intent.
function clearActivationRegistry() {
  if (process.platform !== "win32") return { cleared: false, reason: "non-windows" };
  const child = require("child_process");
  const keys = [
    "HKLM\\Software\\TrustNode\\Activation",
    "HKCU\\Software\\TrustNode\\Activation",
  ];
  const errors = [];
  for (const k of keys) {
    try {
      // reg.exe delete /f succeeds even when the key is absent on some
      // Windows versions; on others it returns 1. Either way the
      // outcome is "key is gone or was never there" — both fine.
      child.execFileSync("reg.exe", ["delete", k, "/f"], {
        stdio: ["ignore", "ignore", "ignore"],
        windowsHide: true,
        timeout: 5000,
      });
    } catch (err) {
      // Code 1 = key not found, which is the happy path.
      if (err && err.status !== 1) {
        errors.push(`${k}: ${String(err.message || err)}`);
      }
    }
  }
  return { cleared: errors.length === 0, errors };
}

// Exposed for the Settings → Reset Workspace flow. NOT called on first
// launch; the caller is responsible for the type-DELETE confirm.
function resetCurrentWorkspace(userDataDir) {
  const saved = readPersistedChoice(userDataDir);
  if (!saved) return { ok: false, reason: "no current workspace" };
  const dbPath = path.join(saved.dataDir, "trustnode_app_store.db");
  if (!fs.existsSync(dbPath)) return { ok: false, reason: "no db at current workspace" };
  const backupPath = makeSnapshotCopy(dbPath, "backup-reset");
  try {
    for (const suffix of ["", "-wal", "-shm"]) {
      const live = `${dbPath}${suffix}`;
      if (fs.existsSync(live)) {
        try { fs.unlinkSync(live); } catch (_) {}
      }
    }
  } catch (_) {}
  // Force re-prompt on next launch by clearing the persisted choice.
  try { fs.unlinkSync(path.join(userDataDir, "workspace-choice.json")); } catch (_) {}
  // Clear the activation registry mirror so the relaunch starts truly
  // clean. Failure here is non-fatal — the SQLite wipe + workspace
  // choice reset are the authoritative bits.
  const registryResult = clearActivationRegistry();
  return { ok: true, backupPath, registry: registryResult };
}

module.exports = {
  detectAndChooseWorkspace,
  resetCurrentWorkspace,
  detectWorkspaces,
};
