"""Check systemd override.conf for trustnode-backend."""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
env = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s: continue
    k, v = s.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"],
          timeout=15, allow_agent=False, look_for_keys=False)

# Print structure of override.conf without printing secret values:
# show only env-var keys.
SH = r"""
echo "--- override.conf path ---"
ls -la /etc/systemd/system/trustnode-backend.service.d/ 2>/dev/null || echo "(no .d dir)"
echo
echo "--- override.conf KEYS only (values redacted) ---"
for f in /etc/systemd/system/trustnode-backend.service.d/*.conf; do
  [ -f "$f" ] || continue
  echo "## $f"
  awk '
    /^Environment=/ {
      # Extract key from Environment="KEY=VAL" or Environment=KEY=VAL
      line = $0
      sub(/^Environment=/, "", line)
      gsub(/"/, "", line)
      n = index(line, "=")
      if (n > 0) {
        key = substr(line, 1, n-1)
        val = substr(line, n+1)
        printf "  Environment=%s=[len %d]\n", key, length(val)
      } else { print "  "$0 }
      next
    }
    { print "  "$0 }
  ' "$f"
done

echo
echo "--- systemd-analyze unit (effective Environment) ---"
systemctl cat trustnode-backend | head -50
echo "..."
systemctl show trustnode-backend -p Environment | awk '{
  # Show only keys, not values
  gsub(/Environment=/, "")
  n = split($0, parts, " ")
  for (i=1; i<=n; i++) {
    eq = index(parts[i], "=")
    if (eq > 0) printf "  %s=[len %d]\n", substr(parts[i],1,eq-1), length(parts[i])-eq
  }
}'
"""

sftp = c.open_sftp()
with sftp.file("/tmp/_check_systemd.sh", "w") as f:
    f.write(SH)
sftp.close()

_, out, err = c.exec_command("bash /tmp/_check_systemd.sh", timeout=30)
print(out.read().decode("utf-8", "replace"))
e = err.read().decode("utf-8", "replace")
if e.strip(): print("stderr:", e)
c.exec_command("rm -f /tmp/_check_systemd.sh")
c.close()
