"""Targeted diagnostic for two reported issues:

1. Portal "Dashboard Profiles" page → 502 from VPS backend with
   psycopg.errors.ConnectionTimeout. Suspect: VPS .env points at direct
   Supabase Postgres (port 5432) instead of the transaction pooler
   (port 6543). Direct connections from non-allowlisted IPs hang for
   the full connect_timeout (we set 6s) → backend returns 502.

2. Portal page taking too long to load. Cascade on Workspace open:
   /summary, /tenants, /customers, /edges, /licenses, /modules,
   /activation-codes, /users — eight serial-ish calls via the same
   backend → upstream Supabase. Need to know which one is slow.

Reads VPS_HOST / VPS_USER / VPS_PASSWORD from Trustnode_edge_app/.env.
Read-only over SSH + plain HTTPS probes. No writes, no restarts.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import paramiko  # type: ignore
    import requests  # type: ignore
except ImportError as exc:
    print(f"missing dependency: {exc}", file=sys.stderr)
    sys.exit(2)


HERE = Path(__file__).resolve().parent.parent
ENV_FILE = HERE / ".env"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def section(title: str) -> None:
    print(f"\n{'='*72}\n{title}\n{'='*72}")


def ssh_run(client: paramiko.SSHClient, cmd: str) -> str:
    _, out, err = client.exec_command(cmd, timeout=20)
    stdout = out.read().decode("utf-8", errors="replace").rstrip()
    stderr = err.read().decode("utf-8", errors="replace").rstrip()
    return (stdout + (f"\n[stderr] {stderr}" if stderr else "")).rstrip()


def main() -> int:
    env = load_env(ENV_FILE)
    host = env.get("VPS_HOST") or ""
    user = env.get("VPS_USER") or ""
    pwd = env.get("VPS_PASSWORD") or ""
    port = int(env.get("VPS_PORT") or "22")
    if not (host and user and pwd):
        print("ERROR: VPS_HOST/VPS_USER/VPS_PASSWORD missing from .env")
        return 2

    print(f"VPS  : {user}@{host}:{port}")

    # ---- Issue 1: cloud DB host in the VPS systemd env ------------------------
    section("VPS service env: which Supabase host is the backend using?")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=port, username=user, password=pwd,
              look_for_keys=False, allow_agent=False, timeout=20)
    try:
        print(ssh_run(c,
            "sudo systemctl show trustnode-backend.service -p Environment "
            "| tr ' ' '\\n' | grep -E 'CLOUD_DB|SUPABASE|TRUSTNODE_' | sort"
        ))
        # Also check the override drop-in for the merged env file
        print("\n--- /etc/systemd/system/trustnode-backend.service.d/10-secrets.conf (filtered) ---")
        print(ssh_run(c,
            "sudo cat /etc/systemd/system/trustnode-backend.service.d/10-secrets.conf 2>/dev/null "
            "| grep -E 'CLOUD_DB|SUPABASE|TRUSTNODE_' | sed 's/PASSWORD=.*/PASSWORD=***REDACTED***/'"
        ))
        # And the actual .env at the repo root that the script consumes
        print("\n--- /opt/trustnode-edge/app/Trustnode_edge_app/.env (filtered) ---")
        print(ssh_run(c,
            "sudo grep -E 'CLOUD_DB|SUPABASE' /opt/trustnode-edge/app/Trustnode_edge_app/.env 2>/dev/null "
            "| sed 's/PASSWORD=.*/PASSWORD=***REDACTED***/' || echo '(no repo .env on VPS)'"
        ))

        # ---- Issue 1b: can the VPS resolve + reach the pooler? ----------------
        section("VPS network: DNS + TCP to Supabase pooler (port 6543)")
        # Try the standard pooler host inferred from any host we saw
        print(ssh_run(c,
            "set -e; "
            "HOST=$(sudo systemctl show trustnode-backend.service -p Environment | tr ' ' '\\n' "
            "    | grep TRUSTNODE_CLOUD_DB_HOST= | cut -d= -f2); "
            "PORT=$(sudo systemctl show trustnode-backend.service -p Environment | tr ' ' '\\n' "
            "    | grep TRUSTNODE_CLOUD_DB_PORT= | cut -d= -f2 || echo 5432); "
            "echo \"HOST=$HOST PORT=$PORT\"; "
            "echo '--- DNS ---'; getent hosts \"$HOST\" || echo '(no DNS resolution)'; "
            "echo '--- TCP connect (timeout 5s) ---'; "
            "timeout 5 bash -c \"</dev/tcp/$HOST/$PORT && echo 'TCP $HOST:$PORT OK' || echo 'TCP $HOST:$PORT FAIL'\""
        ))

        # ---- Issue 1c: timing the offending backend endpoint -----------------
        section("VPS-local: how long does the backend take to answer the failing call?")
        print(ssh_run(c,
            "curl -s -o /tmp/dp.json -w 'time_total=%{time_total}s http_code=%{http_code}\\n' "
            "http://127.0.0.1:8000/api/control-plane/dashboard-profiles; "
            "echo '--- body ---'; head -c 400 /tmp/dp.json; echo"
        ))

        # ---- Issue 1d: backend journal tail for any psycopg/timeout markers --
        section("Backend journal (last 80 lines, filtered for timeout/psycopg)")
        print(ssh_run(c,
            "sudo journalctl -u trustnode-backend.service -n 200 --no-pager "
            "| grep -iE 'timeout|psycopg|connection|cloud_query' | tail -n 40 || echo '(no matches)'"
        ))

        # ---- Issue 2: portal asset sizes + first-byte ------------------------
        section("Portal load: asset sizes + first-byte from nginx")
        print(ssh_run(c,
            "for p in / /portal/ /assets/index-*.js /assets/index-*.css; do "
            "  curl -s -o /dev/null -w \"$p time=%{time_total}s size=%{size_download} ttfb=%{time_starttransfer}s code=%{http_code}\\n\" "
            "    \"https://trustnode.lsapps.app$p\" || true; done"
        ))

    finally:
        c.close()

    # ---- Issue 2b: time each control-plane API call (anonymous, will 401) ----
    section("Portal API cascade (anonymous probes - we expect 401, looking at latency only)")
    base = "https://trustnode.lsapps.app/api/control-plane"
    endpoints = [
        "/summary?tenant_id=default",
        "/tenants",
        "/customers?tenant_id=default",
        "/edges?tenant_id=default",
        "/licenses?tenant_id=default",
        "/modules",
        "/activation-codes?tenant_id=default",
        "/users?tenant_id=default",
        "/dashboard-profiles?tenant_id=default",
    ]
    for ep in endpoints:
        t0 = time.perf_counter()
        try:
            r = requests.get(base + ep, timeout=15)
            dt_ms = int((time.perf_counter() - t0) * 1000)
            print(f"  {dt_ms:5d} ms  HTTP {r.status_code}  {ep}")
        except Exception as exc:
            dt_ms = int((time.perf_counter() - t0) * 1000)
            print(f"  {dt_ms:5d} ms  ERROR {exc}  {ep}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
