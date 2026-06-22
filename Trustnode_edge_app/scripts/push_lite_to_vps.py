"""Push the entire Lite bundle to the VPS via SFTP.

Use after a local rewrite so the production /lite/ updates without waiting
for a CI rebuild. The CI deploy will still rewrite these files on its next
run (sourcing from git), so this is for fast-iteration testing.

SAFETY GATE (operator 2026-06-21): refuses to push when web_cloud_readonly/lite/
has uncommitted modifications, since CI overwrites from git anyway and we
don't want to deploy an artifact that can't be reproduced from a commit.
Pass --force to bypass.
"""
from __future__ import annotations
import argparse, io, subprocess, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
LITE_DIR = HERE / "web_cloud_readonly" / "lite"
REMOTE_DIR = "/var/www/trustnode/lite"
FILES = ["index.html", "styles.css", "manifest.json", "sw.js", "config.json",
         "trustnode_logo.png", "trustnode_login_logo.png",
         "trustnode_app_icon.png", "trustnode_app_icon_180.png"]


def _git(args):
    try:
        proc = subprocess.run(["git", *args], cwd=str(HERE), capture_output=True, text=True, timeout=30)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "git not found"


def _safety_gate(force: bool) -> int:
    rc, out = _git(["status", "--porcelain", "web_cloud_readonly/lite"])
    if rc == 0 and out.strip():
        print("BLOCKED: uncommitted changes under web_cloud_readonly/lite/:")
        for line in out.strip().splitlines():
            print(f"   {line}")
        if not force:
            print("\nCommit the changes first, or re-run with --force to bypass.")
            return 2
    return 0


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1); out[k.strip()] = v.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Lite bundle to VPS")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the uncommitted-changes safety gate.")
    args = parser.parse_args()
    gate_rc = _safety_gate(args.force)
    if gate_rc != 0:
        return gate_rc
    if args.force:
        print("WARNING: --force used; safety gate bypassed.")

    env = load_env(HERE / ".env")
    pwd = env["VPS_PASSWORD"]
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
              username=env["VPS_USER"], password=pwd,
              timeout=15, banner_timeout=15, auth_timeout=15,
              allow_agent=False, look_for_keys=False)
    try:
        c.exec_command(f"mkdir -p {REMOTE_DIR}")
        sftp = c.open_sftp()
        try:
            for name in FILES:
                local = LITE_DIR / name
                if not local.is_file():
                    print(f"  - {name}  (not present locally, skip)")
                    continue
                remote = f"{REMOTE_DIR}/{name}"
                sftp.put(str(local), remote)
                sftp.chmod(remote, 0o644)
                print(f"  + {name}  ({local.stat().st_size} bytes)")
        finally:
            sftp.close()
        c.exec_command(f"chown -R nginx:nginx {REMOTE_DIR} 2>/dev/null || true")
        c.exec_command(f"restorecon -Rv {REMOTE_DIR} 2>/dev/null || true")
    finally:
        c.close()
    print(f"\nDone. Verify: curl -I https://trustnode.lsapps.app/lite/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
