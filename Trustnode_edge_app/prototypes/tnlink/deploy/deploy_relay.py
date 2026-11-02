# -*- coding: utf-8 -*-
"""Install the TrustNode Link relay on the VPS, in ONE ssh session.

Many rapid SSH sessions trip fail2ban on this host, so everything is
batched into a single connection and a single shell script.

Touches nothing the portal uses: its own unprivileged user, its own
directory, its own port. nginx, 443 and the existing service are not
modified.

    python prototypes/tnlink/deploy/deploy_relay.py            # preview
    python prototypes/tnlink/deploy/deploy_relay.py --apply
"""
from __future__ import annotations

import argparse
import io
import os
import posixpath
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# deploy/ -> tnlink/ -> prototypes/ -> Trustnode_edge_app
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PORT = int(os.environ.get("TNLINK_RELAY_PORT", "7443"))
REMOTE_DIR = "/opt/tnlink"


def load_env() -> dict:
    env = dict(os.environ)
    path = os.path.join(ROOT, ".env")
    try:
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass
    return env


SCRIPT = r"""set -e
echo "--- host ---"
(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || echo unknown

# useradd exists on Debian and RHEL alike; adduser --system --group is
# Debian-only and failed here with "group '--no-create-home' does not exist".
if ! getent passwd tnlink >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /sbin/nologin tnlink 2>/dev/null     || useradd -r -M -s /sbin/nologin tnlink
fi
getent passwd tnlink | cut -d: -f1,3,4,7

mkdir -p {d}
chmod 0755 {d}
chown root:root {d}/tnlink_relay.py
chmod 0644 {d}/tnlink_relay.py
install -m 0644 {d}/tnlink-relay.service /etc/systemd/system/tnlink-relay.service
systemctl daemon-reload
systemctl enable tnlink-relay >/dev/null 2>&1 || true
systemctl restart tnlink-relay
sleep 2
echo "--- is-active ---"
systemctl is-active tnlink-relay || true
echo "--- listening ---"
(ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | grep -E ":{p} " || echo "NOT LISTENING"
echo "--- firewall ---"
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port={p}/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  echo "firewalld ports: $(firewall-cmd --list-ports 2>/dev/null)"
elif command -v ufw >/dev/null 2>&1; then
  ufw allow {p}/tcp comment 'TrustNode Link relay' >/dev/null 2>&1 || true
  ufw status 2>/dev/null | grep -E "{p}" || echo "(no ufw rule shown)"
else
  echo "(no firewalld/ufw - check the provider firewall for TCP {p})"
fi
echo "--- recent log ---"
journalctl -u tnlink-relay -n 10 --no-pager 2>/dev/null || true
echo "--- untouched: portal ---"
systemctl is-active nginx 2>/dev/null || echo "(nginx not managed here)"
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually install; without it, preview only")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    env = load_env()
    missing = [k for k in ("VPS_HOST", "VPS_USER", "VPS_PASSWORD")
               if not env.get(k)]
    if missing:
        print("cannot deploy - missing from .env: %s" % ", ".join(missing))
        return 2

    host = env["VPS_HOST"]
    user = env["VPS_USER"]
    port = int(env.get("VPS_PORT") or "22")

    local_relay = os.path.join(os.path.dirname(HERE), "tnlink_relay.py")
    local_unit = os.path.join(HERE, "tnlink-relay.service")
    for p in (local_relay, local_unit):
        if not os.path.isfile(p):
            print("missing local file: %s" % p)
            return 2

    print("TrustNode Link relay - deploy")
    print("  target : %s@%s:%d" % (user, host, port))
    print("  install: %s  (port %d)" % (REMOTE_DIR, a.port))
    print("  files  : tnlink_relay.py, tnlink-relay.service")
    print("  leaves alone: nginx, 443, the portal service")
    print()
    if not a.apply:
        print("PREVIEW ONLY - re-run with --apply")
        return 0

    try:
        import paramiko
    except ImportError:
        print("paramiko is not installed (pip install paramiko)")
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, port=port, username=user,
                password=env["VPS_PASSWORD"], timeout=20)
    try:
        sftp = cli.open_sftp()
        try:
            try:
                sftp.stat(REMOTE_DIR)
            except IOError:
                sftp.mkdir(REMOTE_DIR)
            sftp.put(local_relay,
                     posixpath.join(REMOTE_DIR, "tnlink_relay.py"))
            unit = io.open(local_unit, encoding="utf-8").read()
            if a.port != 7443:
                unit = unit.replace("--port 7443", "--port %d" % a.port)
            tmp = posixpath.join(REMOTE_DIR, "tnlink-relay.service")
            with sftp.open(tmp, "w") as fh:
                fh.write(unit)
            print("  uploaded both files")
        finally:
            sftp.close()

        script = SCRIPT.format(d=REMOTE_DIR, p=a.port)
        # No PTY: with one, the remote shell echoed the entire script back
        # and ran it interactively, which buried the real output.
        chan = cli.get_transport().open_session()
        chan.exec_command("sudo -n bash -s" if user != "root"
                          else "bash -s")
        chan.sendall(script.encode())
        chan.shutdown_write()
        out = b""
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            elif chan.exit_status_ready():
                while chan.recv_ready():
                    out += chan.recv(65536)
                break
        rc = chan.recv_exit_status()
        text = out.decode("utf-8", "replace")
        # Never echo anything that could carry a secret back.
        for line in text.splitlines():
            if "PASSWORD" in line.upper() or "password" in line:
                continue
            print("  | %s" % line)
        print()
        print("  remote exit status: %d" % rc)
        return 0 if rc == 0 else 2
    finally:
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())
