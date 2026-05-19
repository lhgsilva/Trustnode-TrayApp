"""Inject a CSS override into the ASSEMBLED portal HTML.

The bundler stub at /var/www/trustnode/portal/v1/index.html does
`document.documentElement.replaceWith(doc.documentElement)` so any
<style> we put in the original <head> gets blown away when the real
portal HTML loads.

Strategy: patch the bundler script itself to do a `template.replace`
on the assembled HTML template (a string) right before DOMParser
parses it. Our CSS lands inside the NEW <head> that replaces the
old document, so it survives.

Idempotent: re-running replaces the previous patch.
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
/* TrustNode portal banner-shrink overrides (2026-05-19).

   The portal-bundle CSS sets min-height:calc(100vh - 140px) on several
   page containers, which makes any sibling note element stretch via
   flexbox/grid even though the note itself has no min-height. Override
   those containers to size-to-content, and constrain the note classes
   directly so a single-line confirmation can't take 40vh.

   We intentionally use very high specificity + !important because the
   portal stylesheet ships several copies of .error/.info-note at
   different specificities and we have to win all of them. */

.control-plane-page-fill,
.control-plane-layout,
.control-plane-main,
.control-plane-activation-card,
.control-plane-workspace,
.control-plane-workspace-card,
.portal-page,
.workspace-page {
  min-height: 0 !important;
  height: auto !important;
  align-content: start !important;
}

/* The notice panels themselves: hard-cap height, no min, padding for X */
body .info-note,
body .ok-note,
body .error,
body .control-plane-workspace-card > div.info-note,
body .control-plane-workspace-card > div.ok-note,
body .control-plane-workspace-card > div.error {
  padding: 8px 40px 8px 12px !important;
  margin: 6px 0 !important;
  line-height: 1.3 !important;
  font-size: 13px !important;
  position: relative !important;
  min-height: 0 !important;
  max-height: 80px !important;        /* enough for 3 wrapped lines */
  overflow-y: auto !important;
  display: block !important;
  align-self: flex-start !important;
}

/* Dismiss-X — wired up by JS below so click actually hides the notice */
body .info-note[data-tn-dismissible="1"]::after,
body .ok-note[data-tn-dismissible="1"]::after,
body .error[data-tn-dismissible="1"]::after {
  content: "\\00D7";
  position: absolute;
  top: 2px;
  right: 6px;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 18px;
  font-weight: 700;
  opacity: 0.65;
  user-select: none;
  border-radius: 4px;
  pointer-events: none;       /* the JS shim handles clicks on the parent */
}
"""

# Tiny JS shim that:
#   1. tags every .info-note/.ok-note/.error with data-tn-dismissible="1"
#      so the CSS ::after pseudo paints the X
#   2. listens for clicks in the upper-right 30x30 corner and hides the
#      clicked notice
#   3. runs again whenever the portal mutates the DOM (single
#      MutationObserver on document.body)
JS = r"""
(function tnPortalNoticeDismiss() {
  function tag(el) {
    if (!el || el.getAttribute('data-tn-dismissible') === '1') return;
    el.setAttribute('data-tn-dismissible', '1');
  }
  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('.info-note, .ok-note, .error').forEach(tag);
  }
  function init() {
    scan(document);
    // Click handler: if the click was in the top-right corner of a
    // dismissible notice, hide that notice.
    document.addEventListener('click', function(ev) {
      var t = ev.target;
      var el = t && t.closest ? t.closest('[data-tn-dismissible="1"]') : null;
      if (!el) return;
      var rect = el.getBoundingClientRect();
      // top-right 30x30 hit area
      if (ev.clientX >= rect.right - 30 && ev.clientY <= rect.top + 30) {
        el.style.display = 'none';
        ev.preventDefault();
        ev.stopPropagation();
      }
    }, true);
    // Watch for portal renders that add new notices
    var mo = new MutationObserver(function(mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        for (var j = 0; j < m.addedNodes.length; j++) {
          var n = m.addedNodes[j];
          if (n.nodeType === 1) {
            tag(n);
            scan(n);
          }
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
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

# 1b. NOTE: previously we restored from the oldest backup here to wipe
# accumulated patches. That's now harmful because the rebundle script
# (_rebundle_portal_pngs.py) shrinks the embedded image assets and we'd
# lose that work. Step 3 below removes any previous patch markers from
# the current file before re-injecting, so we no longer need a hard
# restore.

# 2. Read current file
sftp = c.open_sftp()
remote = "/var/www/trustnode/portal/v1/index.html"
with sftp.open(remote, "r") as f:
    html = f.read().decode("utf-8", errors="replace")

# 3. Remove any previous patch (idempotency)
import re
# Drop earlier-style <style> injection if it was applied
html = re.sub(
    r'<style id="trustnode-portal-overrides">.*?</style>',
    "",
    html,
    flags=re.DOTALL,
)
# Drop earlier in-bundler patch if present
html = re.sub(
    r"// >>>> TRUSTNODE PORTAL OVERRIDES START.*?// <<<< TRUSTNODE PORTAL OVERRIDES END\n",
    "",
    html,
    flags=re.DOTALL,
)

# 4. Find the line `const doc = new DOMParser().parseFromString(template, 'text/html');`
#    and inject a template.replace() call right before it.
import json as _json
style_literal = _json.dumps("<style id='trustnode-portal-overrides'>" + CSS + "</style>")
script_literal = _json.dumps("<script id='trustnode-portal-dismiss-js'>" + JS + "</script>")
inject_js = (
    "    // >>>> TRUSTNODE PORTAL OVERRIDES START (2026-05-19)\n"
    "    // Patches the assembled portal HTML to constrain the workspace card\n"
    "    // and add an X-to-dismiss handler on .info-note / .ok-note / .error.\n"
    "    // Idempotent: removes any prior override before re-injecting.\n"
    "    template = template.replace(/<style id=['\\\"]trustnode-portal-overrides['\\\"]>[\\s\\S]*?<\\/style>/g, '');\n"
    "    template = template.replace(/<script id=['\\\"]trustnode-portal-dismiss-js['\\\"]>[\\s\\S]*?<\\/script>/g, '');\n"
    f"    template = template.replace('</head>', {style_literal} + '</head>');\n"
    f"    template = template.replace('</body>', {script_literal} + '</body>');\n"
    "    // <<<< TRUSTNODE PORTAL OVERRIDES END\n"
)

marker = "const doc = new DOMParser().parseFromString(template, 'text/html');"
if marker not in html:
    _ps("[FATAL] could not find DOMParser parseFromString line — bundler stub changed?")
    raise SystemExit(1)
html = html.replace(marker, inject_js + "    " + marker, 1)

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

# 6. Verify the patch landed
stdin, stdout, _ = c.exec_command(
    "grep -c 'TRUSTNODE PORTAL OVERRIDES START' /var/www/trustnode/portal/v1/index.html"
)
_ps(f"patch markers in file: {stdout.read().decode().strip()}")
stdin, stdout, _ = c.exec_command(
    "grep -c 'trustnode-portal-overrides' /var/www/trustnode/portal/v1/index.html"
)
_ps(f"override id references: {stdout.read().decode().strip()}")

c.close()
_ps("\n[ok] CSS override injected. Hard-refresh the portal (Ctrl+Shift+R).")
