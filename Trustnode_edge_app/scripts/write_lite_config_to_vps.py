"""Push a working /lite/config.json to the VPS via SSH.

Reads supabase_url + supabase_anon_key from the LOCAL lite/config.json
(which we wrote during the laptop test), then SCPs the same file up to
/var/www/trustnode/lite/config.json on the VPS so https://trustnode.lsapps.app/lite/
serves it.

This is a temporary bridge: the proper solution is to add
TRUSTNODE_PUBLIC_SUPABASE_URL + TRUSTNODE_PUBLIC_SUPABASE_ANON_KEY to the
repo's GitHub Secrets so every CI deploy regenerates the file. Until that's
done, run this script after every CI deploy that doesn't carry those secrets.
"""
from __future__ import annotations
import io
import json
import sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko


HERE = Path(__file__).resolve().parent.parent


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if not p.is_file(): return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); out[k.strip()] = v.strip()
    return out


def main() -> int:
    env = load_env(HERE / ".env")
    lite_cfg_path = HERE / "web_cloud_readonly" / "lite" / "config.json"
    if not lite_cfg_path.is_file():
        print(f"ERROR: {lite_cfg_path} missing — populate it locally first.", file=sys.stderr)
        return 2
    cfg = json.loads(lite_cfg_path.read_text(encoding="utf-8"))
    body = json.dumps(cfg, indent=2)
    print(f"Pushing {len(body)} bytes to /var/www/trustnode/lite/config.json")

    pwd = env["VPS_PASSWORD"]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
        username=env["VPS_USER"], password=pwd,
        timeout=15, banner_timeout=15, auth_timeout=15,
        allow_agent=False, look_for_keys=False,
    )
    try:
        sftp = client.open_sftp()
        try:
            try:
                sftp.stat("/var/www/trustnode/lite/")
            except IOError:
                client.exec_command("mkdir -p /var/www/trustnode/lite")
            remote_path = "/var/www/trustnode/lite/config.json"
            with sftp.file(remote_path, "wb") as f:
                f.write(body.encode("utf-8"))
            sftp.chmod(remote_path, 0o644)
            client.exec_command("chown nginx:nginx /var/www/trustnode/lite/config.json 2>/dev/null || true")
        finally:
            sftp.close()

        # Verify it landed and has the expected size.
        _, sout, _ = client.exec_command("ls -la /var/www/trustnode/lite/config.json && head -c 200 /var/www/trustnode/lite/config.json")
        out = sout.read().decode("utf-8", "replace").replace(pwd, "<REDACTED>")
        print("\nremote file:")
        print(out)
    finally:
        client.close()

    print("\nDone. Test: curl -i https://trustnode.lsapps.app/lite/config.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
