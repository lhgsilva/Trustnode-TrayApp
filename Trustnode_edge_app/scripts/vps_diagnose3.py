"""Check VPS-side cloud DB target config & live query behavior."""
from __future__ import annotations
import io, os, sys
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import paramiko

def load_env(p):
    out={}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,v=line.split("=",1); out[k.strip()]=v.strip()
    return out

CMDS = [
    ("env vars the backend runs under",
     "cat /proc/$(pgrep -f 'uvicorn app.main' | head -1)/environ | tr '\\0' '\\n' | grep -iE 'TRUSTNODE|SUPABASE|DB_' | sort"),
    ("override.conf currently in use",
     "cat /etc/systemd/system/trustnode-backend.service.d/override.conf"),
    ("VPS SQLite app-store: does it have a Supabase cloud DB row?",
     "sqlite3 /opt/trustnode-edge/data/trustnode_app_store.db \"SELECT version, updated_utc, length(payload_json) FROM config_documents WHERE domain='database_configurations'\" 2>&1"),
    ("VPS app-store: count of scoped database_configurations docs",
     "sqlite3 /opt/trustnode-edge/data/trustnode_app_store.db \"SELECT scope_key, version, length(payload_json) FROM config_documents_scoped WHERE domain='database_configurations'\" 2>&1"),
    ("VPS app-store: app_settings endpoint_mode",
     "sqlite3 /opt/trustnode-edge/data/trustnode_app_store.db \"SELECT substr(payload_json, 1, 200) FROM config_documents WHERE domain='app_settings'\" 2>&1"),
    ("Hit /api/app-store/historian/range as anon (proves auth + path) and as bearer",
     "curl -s -m 10 -w '\\nHTTP %{http_code}\\n' 'http://127.0.0.1:8000/api/app-store/historian/range?limit=2&tag=SimREAL%5B3%5D' 2>&1 | head -c 600"),
    ("Recent backend log lines containing 'historian' or 'cloud'",
     "journalctl -u trustnode-backend -n 600 --no-pager 2>&1 | grep -iE 'historian|cloud|supabase|sqlalchemy' | tail -30"),
    ("Backend log: any TIMEOUT / Connection reset since restart",
     "journalctl -u trustnode-backend --since '20 min ago' --no-pager 2>&1 | grep -iE 'timeout|connection|operationalerror|undefinedtable' | tail -30"),
]

def main():
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    pwd = env["VPS_PASSWORD"]
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
              username=env["VPS_USER"], password=pwd, timeout=15,
              banner_timeout=15, auth_timeout=15, allow_agent=False, look_for_keys=False)
    try:
        for label, cmd in CMDS:
            print(f"\n========== {label} ==========")
            print(f"$ {cmd}")
            _,sout,serr = c.exec_command(cmd, timeout=30)
            out = sout.read().decode("utf-8","replace").replace(pwd,"<REDACTED>")
            err = serr.read().decode("utf-8","replace").replace(pwd,"<REDACTED>")
            if out: print(out.rstrip())
            if err.strip(): print("--- stderr ---"); print(err.rstrip())
    finally:
        c.close()

if __name__ == "__main__": main()
