"""Second-stage VPS diagnostic — focus on the stale uvicorn and WS threadpool.

Read-only. No restarts. No config changes.
"""

from __future__ import annotations
import io, os, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko  # type: ignore


def load_env(p: Path) -> dict[str, str]:
    out = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


CMDS = [
    ("which uvicorn process is the stale one (3229546)?",
     "ps -p 3229546 -o pid,ppid,user,etime,cmd 2>&1"),
    ("how was 3229546 launched — full /proc cmdline",
     "tr '\\0' ' ' < /proc/3229546/cmdline 2>&1; echo"),
    ("3229546 working dir + ports it has open",
     "ls -l /proc/3229546/cwd 2>&1; echo; ss -ntp 2>&1 | grep 3229546 | head -20"),
    ("3229546 environment (sanitized)",
     "tr '\\0' '\\n' < /proc/3229546/environ 2>&1 | grep -iE 'TRUSTNODE|PORT|HOST|UVICORN' | head -30"),
    ("3229546 listening sockets",
     "lsof -p 3229546 2>/dev/null | grep -E 'LISTEN|TCP' | head -20"),
    ("which port is nginx actually proxying to? show vhost config",
     "grep -nE 'proxy_pass|server_name|listen' /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/* 2>/dev/null | grep -i trustnode | head -30"),
    ("FastAPI threadpool size (anyio default 40)",
     "grep -RnE 'threadpool|TOTAL_TOKENS|MAX_WORKERS|capacity_limiter' /opt/trustnode-edge/app/Trustnode_edge_app/backend/app/ 2>&1 | head -20"),
    ("active WebSocket connections to :8000",
     "ss -ntp 2>&1 | grep ':8000' | grep -c ESTAB"),
    ("active connections from nginx to uvicorn",
     "ss -ntp 2>&1 | grep ':8000' | head -40"),
    ("are there any zombie / D-state threads in main worker?",
     "ls /proc/3533405/task 2>/dev/null | while read t; do s=$(awk '/^State:/{print $2; exit}' /proc/3533405/task/$t/status 2>/dev/null); [ -n \"$s\" ] && [ \"$s\" != 'S' ] && echo \"  task $t: $s\"; done | head -20; echo 'done'"),
    ("backend log: count of cloud-live WebSocket events in last hour",
     "journalctl -u trustnode-backend --since '1 hour ago' --no-pager 2>&1 | grep -c 'cloud-live'"),
    ("backend log: HTTP errors / timeouts last hour",
     "journalctl -u trustnode-backend --since '1 hour ago' --no-pager 2>&1 | grep -iE 'error|timeout|exception|traceback' | tail -20"),
    ("nginx: requests served + 5xx last 1000 entries",
     "awk '{print $9}' /var/log/nginx/access.log 2>/dev/null | tail -1000 | sort | uniq -c | sort -rn | head"),
    ("nginx error log full tail",
     "tail -100 /var/log/nginx/error.log 2>&1"),
    ("what's the override.conf?",
     "cat /etc/systemd/system/trustnode-backend.service.d/override.conf 2>&1"),
    ("main service unit file",
     "cat /etc/systemd/system/trustnode-backend.service 2>&1"),
]


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
        username=env["VPS_USER"], password=env["VPS_PASSWORD"],
        timeout=15, banner_timeout=15, auth_timeout=15,
        allow_agent=False, look_for_keys=False,
    )
    pwd = env["VPS_PASSWORD"]
    try:
        for label, cmd in CMDS:
            print(f"\n========== {label} ==========")
            print(f"$ {cmd}")
            try:
                _, sout, serr = client.exec_command(cmd, timeout=20)
                out = sout.read().decode("utf-8", "replace").replace(pwd, "<REDACTED>")
                err = serr.read().decode("utf-8", "replace").replace(pwd, "<REDACTED>")
            except Exception as e:
                print(f"  failed: {e!r}")
                continue
            if out: print(out.rstrip())
            if err.strip():
                print("--- stderr ---"); print(err.rstrip())
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
