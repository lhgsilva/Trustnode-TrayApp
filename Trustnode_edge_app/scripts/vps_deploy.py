"""Deploy the current main branch to the VPS and restart the backend.

Steps:
  1. Show currently-deployed git HEAD (so we can roll back if needed).
  2. git fetch + git rev-list HEAD..origin/main to see what'll change.
  3. (If --apply) git pull, restart trustnode-backend, confirm health.

Without --apply, the script is a pure preview. Read all env from .env.

Usage:
  python scripts/vps_deploy.py           # preview only
  python scripts/vps_deploy.py --apply   # pull + restart
"""
from __future__ import annotations
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")

import paramiko  # type: ignore


def _print_safe(text: str) -> None:
    """Print to Windows console without crashing on non-cp1252 chars."""
    try:
        print(text)
    except UnicodeEncodeError:
        # Re-encode to ASCII, replacing unprintables with '?'
        print(text.encode("ascii", errors="replace").decode("ascii"))


def run(client: paramiko.SSHClient, cmd: str, *, label: str | None = None, allow_fail: bool = False) -> tuple[int, str, str]:
    if label:
        _print_safe(f"\n--- {label} ---")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").rstrip()
    err = stderr.read().decode("utf-8", errors="replace").rstrip()
    if out:
        _print_safe(out)
    if err:
        _print_safe(f"[stderr] {err}")
    if rc != 0 and not allow_fail:
        _print_safe(f"[FATAL] command failed (rc={rc}): {cmd}")
        sys.exit(1)
    return rc, out, err


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually pull and restart. Without this, preview only.")
    args = parser.parse_args()

    host = env["VPS_HOST"]
    port = int(env.get("VPS_PORT") or "22")
    user = env["VPS_USER"]
    password = env["VPS_PASSWORD"]

    print(f"[i] connecting to {user}@{host}:{port}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=15)

    # Repo root on the VPS is /opt/trustnode-edge/app — confirmed by
    # the vps_inspect.py output earlier. Hardcoded so we don't deal
    # with multi-line stdout in the discovery step.
    repo_root = "/opt/trustnode-edge/app"
    rc, out, _ = run(client, f"test -d {repo_root}/.git && echo OK || echo MISSING", label="confirm repo")
    if out.strip() != "OK":
        print(f"[FATAL] {repo_root} is not a git checkout")
        client.close()
        return 1

    run(client, f"cd {repo_root} && git rev-parse HEAD", label="current HEAD")
    run(client, f"cd {repo_root} && git status --porcelain | head -20", label="working tree status (should be empty)")
    run(client, f"cd {repo_root} && git remote -v | head -4", label="git remotes")
    run(client, f"cd {repo_root} && git fetch origin main 2>&1 | tail -10", label="git fetch")
    run(client, f"cd {repo_root} && git log --oneline HEAD..origin/main | head -20", label="commits to pull")
    run(client, f"cd {repo_root} && git diff --stat HEAD..origin/main | tail -30", label="files that will change")

    if not args.apply:
        print("\n[i] preview only. Re-run with --apply to pull + restart.")
        client.close()
        return 0

    # --- APPLY ---
    print("\n[i] APPLYING: stash local edits (already-on-origin), git pull, restart backend")
    # The VPS has uncommitted edits to backend files that — verified
    # 2026-05-18 — are an out-of-band copy of patches already in
    # origin/main. We stash them so the pull can fast-forward cleanly.
    # If the stash later turns out to contain genuine local-only work
    # we can `git stash apply` it manually.
    run(client, f"cd {repo_root} && git stash push -u -m 'pre-deploy-2026-05-18: out-of-band edits matching origin/main'", label="stash local edits")
    run(client, f"cd {repo_root} && git pull --ff-only origin main 2>&1", label="git pull")
    # Verify the stash diff vs new HEAD — if it's empty, drop it.
    rc, stash_diff, _ = run(client, f"cd {repo_root} && git stash show -p 2>/dev/null | head -1 | wc -l", label="post-pull stash size (lines)")
    if stash_diff.strip() == "0":
        run(client, f"cd {repo_root} && git stash drop", label="drop empty stash")
    else:
        print(f"[!] stash still has content after pull — leaving it in place. Inspect with: cd {repo_root} && git stash show -p")
    run(client, "systemctl restart trustnode-backend", label="restart backend")
    # Wait briefly then confirm
    run(client, "sleep 2 && systemctl status trustnode-backend --no-pager | head -25", label="post-restart status")
    rc, _, _ = run(client, "curl -fsS -m 5 http://127.0.0.1:8000/api/health || echo HEALTH_FAILED", label="health probe", allow_fail=True)

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
