"""Patch the trustnode-edge nginx config on the VPS to add:
  - gzip compression for JS/CSS/JSON/SVG  (1.4 MB JS  ->  ~350 KB on the wire)
  - immutable far-future Cache-Control for /assets/* (Vite fingerprints
    the file names, so we can safely cache them for a year)

Idempotent: skips if the markers are already present. Always reloads
nginx after a successful patch and rolls back from a timestamped backup
if `nginx -t` fails.

Reads VPS_HOST / VPS_USER / VPS_PASSWORD from Trustnode_edge_app/.env.
"""
from __future__ import annotations
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko  # type: ignore


HERE = Path(__file__).resolve().parent.parent
ENV = HERE / ".env"
REMOTE_CONF = "/etc/nginx/conf.d/trustnode-edge.conf"
MARKER = "# TRUSTNODE_PERF_BEGIN"
GZIP_BLOCK = """    # TRUSTNODE_PERF_BEGIN — gzip + immutable cache for fingerprinted assets
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_min_length 1024;
    gzip_comp_level 6;
    gzip_types
        application/javascript
        application/json
        application/wasm
        application/xml
        font/woff2
        image/svg+xml
        text/css
        text/javascript
        text/plain
        text/xml;

    location ^~ /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        try_files $uri =404;
    }
    # TRUSTNODE_PERF_END
"""


def load_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run(c: paramiko.SSHClient, cmd: str, *, allow_fail: bool = False) -> tuple[int, str, str]:
    _, stdout, stderr = c.exec_command(cmd, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0 and not allow_fail:
        print(f"[!] command failed ({code}): {cmd}")
        print(f"    stdout: {out.rstrip()}")
        print(f"    stderr: {err.rstrip()}")
    return code, out, err


def main() -> int:
    env = load_env(ENV)
    host = env.get("VPS_HOST"); user = env.get("VPS_USER"); pwd = env.get("VPS_PASSWORD")
    port = int(env.get("VPS_PORT") or "22")
    if not (host and user and pwd):
        print("ERROR: VPS_HOST/VPS_USER/VPS_PASSWORD missing from .env")
        return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=port, username=user, password=pwd,
              look_for_keys=False, allow_agent=False, timeout=20)
    try:
        print(f"== Patching {REMOTE_CONF} on {host} ==")

        # Idempotency check
        code, out, _ = run(c, f"sudo grep -c '{MARKER}' {REMOTE_CONF}", allow_fail=True)
        if (out.strip() or "0") != "0":
            print(f"[=] Marker already present ({out.strip()} occurrences); nothing to do.")
            return 0

        # Backup
        run(c, f"sudo cp {REMOTE_CONF} {REMOTE_CONF}.bak.$(date +%Y%m%d%H%M%S)")

        # Inject the block AFTER the `index index.html;` line inside the
        # 443 server block. We use awk so the insertion is deterministic
        # and doesn't require a regex over multi-line context.
        sftp = c.open_sftp()
        with sftp.open("/tmp/trustnode-perf.txt", "w") as f:
            f.write(GZIP_BLOCK)
        sftp.close()
        # Insert after the first `index index.html;` line we find.
        awk_script = (
            "awk '"
            "BEGIN{added=0} "
            "{print} "
            "/^[[:space:]]*index[[:space:]]+index\\.html;/ && !added "
            "{ while ((getline line < \"/tmp/trustnode-perf.txt\") > 0) print line; "
            "close(\"/tmp/trustnode-perf.txt\"); added=1 } "
            "' " + REMOTE_CONF + " | sudo tee " + REMOTE_CONF + ".new >/dev/null"
        )
        code, _, _ = run(c, awk_script)
        if code != 0:
            print("[!] awk insertion failed; aborting before nginx -t")
            return 3
        run(c, f"sudo mv {REMOTE_CONF}.new {REMOTE_CONF}")

        # Validate
        code, out, err = run(c, "sudo nginx -t", allow_fail=True)
        print(out + err)
        if code != 0:
            # Roll back
            print("[!] nginx -t failed; rolling back from backup")
            run(c, f"sudo cp $(ls -t {REMOTE_CONF}.bak.* | head -1) {REMOTE_CONF}")
            run(c, "sudo nginx -t")
            return 4

        # Reload
        code, _, _ = run(c, "sudo systemctl reload nginx")
        if code != 0:
            print("[!] nginx reload failed")
            return 5
        print("[ok] nginx reloaded with gzip + immutable cache for /assets/")

        # Verify
        print("\n== Verify gzip and cache headers ==")
        run(c, "curl -s -I -H 'Accept-Encoding: gzip' https://trustnode.lsapps.app/assets/$(curl -s https://trustnode.lsapps.app/ | grep -oE 'index-[A-Za-z0-9]+\\.js' | head -1) | egrep -i 'HTTP/|content-encoding|cache-control|content-length'", allow_fail=True)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
