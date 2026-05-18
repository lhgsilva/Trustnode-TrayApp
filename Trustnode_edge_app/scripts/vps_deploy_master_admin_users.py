"""Deploy commit 44e089f (master-admin all-tenants user list) to the VPS.

Pulls from GitHub on the VPS, restarts trustnode-backend, then verifies:
  1. Bundle present at /var/www/trustnode/portal (or wherever nginx serves
     web_cloud_readonly/ from)
  2. Backend responds 403 to /api/cp/users?tenant_id=__all__ when called
     unauthenticated (the route exists and the gate is wired)

Run from Trustnode_edge_app/:
    python scripts/vps_deploy_master_admin_users.py
"""
from __future__ import annotations
import io, sys, time
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko, requests

HERE = Path(__file__).resolve().parent.parent
REPO_DIR = "/opt/trustnode-edge/app"


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run(c: paramiko.SSHClient, cmd: str, *, label: str = "") -> tuple[int, str, str]:
    print(f"\n$ {label or cmd}")
    stdin, stdout, stderr = c.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip(): print(out.rstrip())
    if err.strip(): print(f"stderr: {err.rstrip()}")
    print(f"  exit={rc}")
    return rc, out, err


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
        # 1) Confirm the deploy target exists
        run(c, f"test -d {REPO_DIR} && echo ok", label=f"check {REPO_DIR}")

        # 2) Pull
        run(c, f"cd {REPO_DIR} && git fetch --all 2>&1 | tail -5",
            label="git fetch")
        run(c, f"cd {REPO_DIR} && git log -1 --oneline HEAD",
            label="HEAD before pull")
        run(c, f"cd {REPO_DIR} && git pull --ff-only origin main 2>&1 | tail -20",
            label="git pull")
        run(c, f"cd {REPO_DIR} && git log -1 --oneline HEAD",
            label="HEAD after pull")

        # 3) Restart backend
        run(c, "systemctl restart trustnode-backend",
            label="restart trustnode-backend")
        time.sleep(3)
        run(c, "systemctl is-active trustnode-backend",
            label="check backend up")
        run(c, "journalctl -u trustnode-backend -n 15 --no-pager",
            label="recent logs")

        # 4) Verify the new endpoint is in the running code
        run(c, "curl -s -o /dev/null -w 'http=%{http_code}\\n' "
              "'http://127.0.0.1:8000/api/control-plane/users?tenant_id=__all__'",
            label="probe /api/cp/users?tenant_id=__all__ (expect 401/403, NOT 200/404)")

        # 5) Verify nginx is serving the new bundle hash
        run(c, "ls -la /var/www/trustnode/portal/assets/ 2>/dev/null | head -10 || "
              "find /var/www -name 'index-*.js' -mmin -60 -ls 2>/dev/null | head -10",
            label="bundle assets on disk")
    finally:
        c.close()

    # 6) End-to-end probe against the public URL
    print("\n== Public endpoint probe ==")
    try:
        r = requests.get("https://trustnode.lsapps.app/api/control-plane/users",
                         params={"tenant_id": "__all__"},
                         timeout=10, allow_redirects=False)
        print(f"  GET .../api/cp/users?tenant_id=__all__  -> HTTP {r.status_code}")
        print(f"  body: {r.text[:200]}")
        if r.status_code in (401, 403):
            print("  PASS: endpoint exists and is auth-gated")
        elif r.status_code == 404:
            print("  FAIL: endpoint still 404 — backend didn't reload?")
        else:
            print(f"  ? unexpected status {r.status_code}")
    except Exception as exc:
        print(f"  probe error: {exc}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
