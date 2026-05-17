"""One-shot read-only diagnostic over SSH against the prod VPS.

Reads VPS_HOST / VPS_PORT / VPS_USER / VPS_PASSWORD from
Trustnode_edge_app/.env (which is .gitignored). Runs a fixed read-only
command bundle and prints output. No writes, no restarts, no config
changes on the remote.
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

# Force UTF-8 on stdout so systemd's bullet glyphs etc. don't crash cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import paramiko  # type: ignore
except ImportError:
    print("paramiko is not installed", file=sys.stderr)
    sys.exit(2)


def load_env(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


DIAG_CMDS = [
    ("systemctl status", "systemctl status trustnode-backend --no-pager 2>&1 | head -40"),
    ("uptime", "uptime"),
    ("last 200 backend log lines", "journalctl -u trustnode-backend -n 200 --no-pager 2>&1 | tail -200"),
    ("processes (uvicorn/python/nginx)", "ps -eo pid,pcpu,pmem,rss,nlwp,stat,etime,comm | grep -E 'uvicorn|python|nginx' | head -20"),
    ("memory", "free -m"),
    ("kernel errors / OOM", "dmesg -T --level=err,crit,alert 2>/dev/null | tail -20"),
    ("sockets on :8000 + to supabase", "ss -ntp 2>&1 | grep -E ':8000|pooler.supabase' | head -40"),
    ("disk", "df -h / /var 2>&1"),
    ("nginx error log tail", "tail -50 /var/log/nginx/error.log 2>&1"),
    ("python thread state of uvicorn worker", "for p in $(pgrep -f 'uvicorn app.main'); do echo '--- pid' $p '---'; ls -1 /proc/$p/task | wc -l | awk '{print \"threads:\",$1}'; cat /proc/$p/status 2>/dev/null | grep -E 'VmRSS|Threads|State|voluntary'; done"),
]


def redact(text: str, password: str) -> str:
    if not password:
        return text
    return text.replace(password, "<REDACTED>")


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    host = env.get("VPS_HOST")
    user = env.get("VPS_USER")
    password = env.get("VPS_PASSWORD")
    port = int(env.get("VPS_PORT") or "22")
    if not (host and user and password):
        print("VPS_HOST / VPS_USER / VPS_PASSWORD missing in .env", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {user}@{host}:{port} ...", flush=True)
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:
        print(f"SSH connect failed: {exc!r}", file=sys.stderr)
        return 1

    try:
        for label, cmd in DIAG_CMDS:
            print(f"\n========== {label} ==========")
            print(f"$ {cmd}")
            try:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"  command failed: {exc!r}")
                continue
            if out:
                print(redact(out, password).rstrip())
            if err.strip():
                print("--- stderr ---")
                print(redact(err, password).rstrip())
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
