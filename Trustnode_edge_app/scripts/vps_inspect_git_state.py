"""Inspect VPS git state in more detail."""
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

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)
repo = "/opt/trustnode-edge/app"

CMDS = [
    "current branch and ahead/behind",
    f"cd {repo} && git rev-parse --abbrev-ref HEAD && git status -sb 2>&1 | head -2",
    "all branches",
    f"cd {repo} && git branch -a 2>&1",
    "all remotes",
    f"cd {repo} && git remote -v",
    "is deb01ee reachable on origin/main?",
    f"cd {repo} && git branch -r --contains deb01ee 2>&1",
    "commits unique to local (not on origin/main)",
    f"cd {repo} && git log --oneline origin/main..HEAD 2>&1 | head -20",
    "commits on origin/main not on local",
    f"cd {repo} && git log --oneline HEAD..origin/main 2>&1 | head -20",
    "last 10 commits on local",
    f"cd {repo} && git log --oneline -10",
]
for i in range(0, len(CMDS), 2):
    label, cmd = CMDS[i], CMDS[i+1]
    print(f"\n--- {label} ---")
    stdin,stdout,_ = client.exec_command(cmd, timeout=30)
    print(stdout.read().decode(errors="replace"))

client.close()
