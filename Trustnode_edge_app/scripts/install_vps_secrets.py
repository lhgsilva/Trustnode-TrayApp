"""Install (or refresh) the VPS systemd secrets drop-in.

Writes /etc/systemd/system/trustnode-backend.service.d/10-secrets.conf
with the Supabase + cloud-DB env vars the backend needs for the user
mirror and cloud-read path.

CI deploys WILL NOT touch this file — it's the operator-managed counterpart
to the CI-managed override.conf (which holds perf knobs only). Run this
script once per VPS bootstrap, and again only if a credential rotates.

Credentials are read from Trustnode_edge_app/.env (gitignored), so this
script can be run from any developer workstation without exposing
secrets to logs or CI variables.

Usage from Trustnode_edge_app/:
    python scripts/install_vps_secrets.py        # write + restart
    python scripts/install_vps_secrets.py --dry  # show what would be written
    python scripts/install_vps_secrets.py --no-restart   # write without restart
"""
from __future__ import annotations
import argparse, io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
SECRETS_PATH = "/etc/systemd/system/trustnode-backend.service.d/10-secrets.conf"

# These keys are required — if any are missing the script aborts.
REQUIRED = (
    "TRUSTNODE_SUPABASE_URL",
    "TRUSTNODE_SUPABASE_SERVICE_KEY",
    "TRUSTNODE_CLOUD_DB_HOST",
    "TRUSTNODE_CLOUD_DB_USER",
    "TRUSTNODE_CLOUD_DB_PASSWORD",
)
# These keys are optional — written only if .env has them, with sensible
# fallbacks the backend already applies.
OPTIONAL = (
    "TRUSTNODE_SUPABASE_USER_DOMAIN",
    "TRUSTNODE_CLOUD_DB_PORT",
    "TRUSTNODE_CLOUD_DB_NAME",
    "TRUSTNODE_CLOUD_DB_SCHEMA",
    "TRUSTNODE_CLOUD_DB_SSLMODE",
    "TRUSTNODE_CLOUD_DB_TABLE",
)


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k, v = s.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def build_conf(env: dict[str, str]) -> str:
    lines = [
        "# Managed by scripts/install_vps_secrets.py — do NOT edit by hand.",
        "# CI deploys preserve this file; only the install script overwrites it.",
        "[Service]",
    ]
    for k in REQUIRED:
        lines.append(f"Environment={k}={env[k]}")
    for k in OPTIONAL:
        v = env.get(k, "")
        if v: lines.append(f"Environment={k}={v}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="print the file contents (with secrets redacted) and exit")
    ap.add_argument("--no-restart", action="store_true",
                    help="write the file but don't restart trustnode-backend")
    args = ap.parse_args()

    env = load_env(HERE / ".env")
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        print(f"ERROR: missing required keys in .env: {missing}", file=sys.stderr)
        return 2

    conf = build_conf(env)
    if args.dry:
        redacted = []
        for line in conf.splitlines():
            if line.startswith("Environment="):
                _, kv = line.split("=", 1)        # strip "Environment="
                k, _, v = kv.partition("=")
                if any(s in k for s in ("PASSWORD", "SERVICE_KEY")):
                    redacted.append(f"Environment={k}=[redacted len={len(v)}]")
                else:
                    redacted.append(line)
            else:
                redacted.append(line)
        print("\n".join(redacted))
        return 0

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
              username=env["VPS_USER"], password=env.get("VPS_PASSWORD") or None,
              key_filename=env.get("VPS_SSH_KEY") or None,
              timeout=15, allow_agent=False, look_for_keys=False)
    try:
        # Make sure the drop-in directory exists.
        c.exec_command(f"mkdir -p $(dirname {SECRETS_PATH})", timeout=15)[1].channel.recv_exit_status()
        sftp = c.open_sftp()
        with sftp.file(SECRETS_PATH, "wb") as fh:
            fh.write(conf.encode("utf-8"))
        sftp.chmod(SECRETS_PATH, 0o600)  # contains a password — tighten access
        sftp.close()
        print(f"wrote {SECRETS_PATH} ({len(conf)} bytes, chmod 600)")
        if args.no_restart:
            print("--no-restart: leaving service untouched. Run "
                  "`systemctl daemon-reload && systemctl restart trustnode-backend` "
                  "to apply.")
            return 0
        _, out, err = c.exec_command(
            "systemctl daemon-reload && "
            "systemctl restart trustnode-backend && "
            "sleep 3 && systemctl is-active trustnode-backend",
            timeout=30,
        )
        print("restart:", out.read().decode("utf-8", "replace").strip())
        e = err.read().decode("utf-8", "replace").strip()
        if e: print("stderr:", e)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
