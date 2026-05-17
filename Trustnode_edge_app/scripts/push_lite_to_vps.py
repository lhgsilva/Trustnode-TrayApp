"""Push the entire Lite bundle to the VPS via SFTP.

Use after a local rewrite so the production /lite/ updates without waiting
for a CI rebuild. The CI deploy will still rewrite these files on its next
run (sourcing from git), so this is for fast-iteration testing.
"""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
LITE_DIR = HERE / "web_cloud_readonly" / "lite"
REMOTE_DIR = "/var/www/trustnode/lite"
FILES = ["index.html", "styles.css", "manifest.json", "sw.js", "config.json", "trustnode_logo.png"]


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1); out[k.strip()] = v.strip()
    return out


def main() -> int:
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
