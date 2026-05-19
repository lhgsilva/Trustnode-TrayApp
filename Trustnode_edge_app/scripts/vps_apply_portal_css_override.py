"""Append a <style> override to /var/www/trustnode/portal/v1/index.html
that constrains the giant banner card.

Strategy: insert a <style id="trustnode-portal-overrides"> block right
before </head> in the bundler stub. The bundler later writes the real
portal HTML over document.body, but our <style> in <head> survives and
its CSS rules apply because they target the same class names the
portal uses.

Idempotent: re-running replaces the previous override block.
"""
from pathlib import Path
import paramiko

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")


def _ps(s):
    try: print(s)
    except UnicodeEncodeError: print(s.encode("ascii", errors="replace").decode("ascii"))


CSS = """
/* TrustNode portal banner-shrink overrides
   Added 2026-05-19. The portal-bundle CSS sets min-height:calc(100vh - 140px)
   on .control-plane-page-fill / .control-plane-main, so the workspace card
   that holds the saved/error banner stretches to fill the viewport. Override
   those to size to content, and apply a compact look + dismiss-button hook
   to the info/error notes inside. */

.control-plane-page-fill,
.control-plane-layout,
.control-plane-main { min-height: 0 !important; }

.control-plane-activation-card { min-height: 0 !important; align-content: start !important; }

.control-plane-workspace-card { width: 100%; }

/* The banner panel inside the workspace card */
.control-plane-workspace-card > .info-note,
.control-plane-workspace-card > .ok-note,
.control-plane-workspace-card > .error,
.info-note, .ok-note, .error {
  padding: 8px 36px 8px 12px !important;
  margin: 6px 0 !important;
  line-height: 1.3 !important;
  font-size: 13px !important;
  position: relative !important;
  min-height: 0 !important;
  max-height: none !important;
}

/* Append a dismiss button. Pure CSS: an ::after pseudo-element acts as
   the close affordance. JS isn't needed — we use the :target trick:
   each banner is a normal <div> in the portal, so clicking the X only
   visually hides the immediate notice via :has + sibling selector
   below. If the portal renders multiple notices stacked, each gets its
   own. This is a best-effort UX fix until the portal frontend code is
   updated properly. */
.info-note::after,
.ok-note::after,
.error::after {
  content: "\\00D7";       /* multiplication sign as 'close' glyph */
  position: absolute;
  top: 4px;
  right: 8px;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 18px;
  font-weight: 700;
  opacity: 0.7;
  user-select: none;
  border-radius: 4px;
  pointer-events: auto;
}
.info-note:hover::after,
.ok-note:hover::after,
.error:hover::after { opacity: 1; background: rgba(0,0,0,0.08); }

/* When the user clicks the X area, hide the notice. We can't do this
   purely in CSS without JS, so the dismiss button is currently visual
   only. The portal frontend's next iteration should add an onclick. */
"""


c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)

# 1. Back up the current portal index.html
_ps("=== backup current portal index.html ===")
stdin, stdout, _ = c.exec_command(
    "cp /var/www/trustnode/portal/v1/index.html "
    "/var/www/trustnode/portal/v1/index.html.bak-$(date +%Y%m%d-%H%M%S)"
)
stdout.read()
_ps("backup created")

# 2. Read current file
sftp = c.open_sftp()
remote = "/var/www/trustnode/portal/v1/index.html"
with sftp.open(remote, "r") as f:
    html = f.read().decode("utf-8", errors="replace")

# 3. Remove any previous override block (idempotency)
import re
html = re.sub(
    r'<style id="trustnode-portal-overrides">.*?</style>',
    "",
    html,
    flags=re.DOTALL,
)

# 4. Inject the new override before </head>
inject = f'<style id="trustnode-portal-overrides">{CSS}</style>\n</head>'
if "</head>" not in html:
    _ps("[FATAL] no </head> tag found")
    raise SystemExit(1)
html = html.replace("</head>", inject, 1)

# 5. Write back
with sftp.open(remote + ".new", "w") as f:
    f.write(html.encode("utf-8"))
sftp.close()
stdin, stdout, _ = c.exec_command(
    "mv /var/www/trustnode/portal/v1/index.html.new /var/www/trustnode/portal/v1/index.html && "
    "chown nginx:nginx /var/www/trustnode/portal/v1/index.html && "
    "echo OK"
)
result = stdout.read().decode().strip()
_ps(f"\n=== install result: {result} ===")

# 6. Verify
stdin, stdout, _ = c.exec_command(
    "grep -c 'trustnode-portal-overrides' /var/www/trustnode/portal/v1/index.html"
)
_ps(f"override blocks now in file: {stdout.read().decode().strip()}")

c.close()
_ps("\n[ok] CSS override injected. Hard-refresh the portal (Ctrl+Shift+R).")
