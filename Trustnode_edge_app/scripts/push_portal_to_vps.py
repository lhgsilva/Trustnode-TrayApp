"""Push the cloud-readonly portal bundle to the VPS via SFTP.

Mirrors push_lite_to_vps.py but for `frontend/dist_cloud_readonly` →
`/var/www/trustnode`. Use after running `npm run build:cloudro` so the
portal at https://trustnode.lsapps.app picks up new code without waiting
for CI. CI's next deploy will rewrite the same files from git, so this is
fast-iteration only.
"""
from __future__ import annotations
import io, os, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
DIST_DIR = HERE / "frontend" / "dist_cloud_readonly"
REMOTE_DIR = "/var/www/trustnode"


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
    if not DIST_DIR.is_dir():
        print(f"ERROR: {DIST_DIR} not found. Run `npm run build:cloudro` first.")
        return 1
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
