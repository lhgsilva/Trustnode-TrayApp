"""Confirm VPS post-deploy state: git head, service status, health."""
import sys
from pathlib import Path
import paramiko  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")


def _print_safe(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", errors="replace").decode("ascii"))


client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)

repo = "/opt/trustnode-edge/app"
CMDS = [
    ("git HEAD", f"cd {repo} && git log --oneline -3"),
    ("uncommitted (post-stash)", f"cd {repo} && git status --short"),
    ("stash list", f"cd {repo} && git stash list"),
    ("service active?", "systemctl is-active trustnode-backend"),
    ("service status",
     "systemctl status trustnode-backend --no-pager 2>&1 | head -15"),
    ("uvicorn process",
     "ps -eo pid,etime,cmd --sort=-etime | grep uvicorn | grep -v grep | head -3"),
    ("health endpoint",
     "curl -fsS -m 5 http://127.0.0.1:8000/api/health 2>&1 || echo HEALTH_FAILED"),
    ("last 20 backend log lines",
     "journalctl -u trustnode-backend -n 20 --no-pager 2>&1 | tail -25"),
]
for label, cmd in CMDS:
    _print_safe(f"\n--- {label} ---")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode(errors="replace").rstrip()
    err = stderr.read().decode(errors="replace").rstrip()
    if out:
        _print_safe(out)
    if err:
        _print_safe(f"[stderr] {err}")

client.close()
