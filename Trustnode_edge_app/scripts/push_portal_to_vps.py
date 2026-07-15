"""Push the cloud-readonly portal bundle to the VPS via SFTP.

Mirrors push_lite_to_vps.py but for `frontend/dist_cloud_readonly` →
`/var/www/trustnode`. Use after running `npm run build:cloudro` so the
portal at https://trustnode.lsapps.app picks up new code without waiting
for CI. CI's next deploy will rewrite the same files from git, so this is
fast-iteration only.

SAFETY GATE (operator 2026-06-21): the previous behavior silently shipped
whatever was sitting in `dist_cloud_readonly/`, including bundles built from
uncommitted source changes. That bit us on Jun 21 — an experimental edit to
App.jsx broke the master-admin developer-portal in production. The gate
below refuses to push when:

  1. There are uncommitted modifications to frontend/src/ (the source the
     portal bundle is built from). Source must be committed first so we
     have a rollback point.
  2. The committed `frontend/dist_cloud_readonly/` does not match the local
     dist (i.e. the operator rebuilt without committing the rebuild). The
     git-tracked dist must be the source of truth for what's deployed.

Pass --force to bypass (logs a loud warning).
"""
from __future__ import annotations
import argparse, io, os, subprocess, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
DIST_DIR = HERE / "frontend" / "dist_cloud_readonly"
REMOTE_DIR = "/var/www/trustnode"


def _git(args: list[str]) -> tuple[int, str]:
    """Run a git command in the repo. Returns (returncode, stdout+stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(HERE),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "git not found"


def _safety_gate(force: bool) -> int:
    """Return 0 if push is allowed, non-zero if it must abort. Prints reasons."""
    # 1. Check for uncommitted changes under frontend/src/
    rc, out = _git(["status", "--porcelain", "frontend/src"])
    if rc != 0:
        print(f"WARN: git status failed: {out.strip()}")
    elif out.strip():
        print("BLOCKED: uncommitted changes under frontend/src/:")
        for line in out.strip().splitlines():
            print(f"   {line}")
        if not force:
            print("\nCommit the source first, or re-run with --force to bypass.")
            return 2

    # 2. Check that the dist matches the committed dist (i.e. someone rebuilt
    #    locally but didn't commit the new bundle).
    rc, out = _git(["status", "--porcelain", "frontend/dist_cloud_readonly"])
    if rc == 0 and out.strip():
        print("BLOCKED: uncommitted changes under frontend/dist_cloud_readonly/:")
        for line in out.strip().splitlines():
            print(f"   {line}")
        print("\nThe committed dist must match what's deployed. Either:")
        print("  - commit the rebuild:    git add frontend/dist_cloud_readonly && git commit")
        print("  - revert to last known-good:    git checkout HEAD -- frontend/dist_cloud_readonly")
        print("  - bypass at your own risk:    re-run with --force")
        if not force:
            return 3
    return 0


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def upload_tree(sftp: paramiko.SFTPClient, local_root: Path, remote_root: str) -> int:
    count = 0
    for path in local_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        # Never overwrite the Lite bundle (lives under /lite); push_lite_to_vps
        # owns that subtree.
        if rel.startswith("lite/"):
            continue
        remote_path = f"{remote_root}/{rel}"
        # Ensure parent dirs exist.
        parts = remote_path.split("/")[:-1]
        cur = ""
        for p in parts:
            if not p:
                cur = "/"
                continue
            cur = (cur.rstrip("/") + "/" + p) if cur else p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass
        sftp.put(str(path), remote_path)
        print(f"  + {rel}  ({path.stat().st_size} bytes)")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Push portal bundle to VPS")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the uncommitted-changes safety gate. Use sparingly — this is how Jun 21 broke production.",
    )
    args = parser.parse_args()

    if not DIST_DIR.is_dir():
        print(f"ERROR: {DIST_DIR} not found. Run `npm run build:cloudro` first.")
        return 1

    # Safety gate — refuse to ship uncommitted source.
    gate_rc = _safety_gate(args.force)
    if gate_rc != 0:
        return gate_rc
    if args.force:
        print("WARNING: --force used; safety gate bypassed. Hope you tested the bundle.")

    env = load_env(HERE / ".env")
    pwd = env.get("VPS_PASSWORD") or os.environ.get("VPS_PASSWORD") or ""
    host = env.get("VPS_HOST") or ""
    user = env.get("VPS_USER") or ""
    port = int(env.get("VPS_PORT") or "22")
    if not (host and user and pwd):
        print("ERROR: VPS_HOST/VPS_USER/VPS_PASSWORD missing from .env")
        return 2
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=port, username=user, password=pwd, look_for_keys=False, allow_agent=False)
    sftp = c.open_sftp()
    try:
        n = upload_tree(sftp, DIST_DIR, REMOTE_DIR)
        print(f"\nDone — {n} file(s) uploaded.")
        print("Verify: curl -I https://trustnode.lsapps.app/")
    finally:
        sftp.close()
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
