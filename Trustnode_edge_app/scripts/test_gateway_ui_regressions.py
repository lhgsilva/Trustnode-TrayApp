# -*- coding: utf-8 -*-
"""The seven gateway/UI faults reported on 2026-08-27.

Each check names the fault it prevents, because every one of these shipped and
was found by an operator rather than by a test:

  1. an ifm block discovered on port 44818 was labelled "EtherNet/IP
     (Allen-Bradley)" — a port number is not an identity;
  2. the gateway table printed every tag name inline, so a 49-tag row was
     ~1000 px tall and the page was unusable;
  3. every modal rendered BEHIND the app header (header z-index 60,
     backdrop 50);
  4. "Add Gateway" seeded a NEW gateway with the previous gateway's tags;
  5/6. the same 28 values were listed three times in one dialog;
  7. a gateway reporting RUNNING showed no reason while the block refused
     three quarters of every read.

Static checks against the source — no hardware, no running app.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
BACK = os.path.join(ROOT, "backend", "app")
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:130]) if detail else ""
    print("  {0:60s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8", errors="replace").read()


app = read(SRC, "App.jsx")
css = read(SRC, "styles.css")
plc_router = read(BACK, "routers", "plc.py")
plc_mgr = read(BACK, "services", "plc_manager.py")
ifm = read(BACK, "drivers", "ifm_iolink.py")

# --- 1. real device identity ----------------------------------------------
print("[1. a scanned device is identified, not guessed from its port]")
check("the scanner asks the device what it is",
      "def _identify_scanned_device" in plc_router)
check("  via CIP Identity", "def _probe_cip_identity" in plc_router)
check("  and via the ifm IoT Core", "def _probe_ifm_iot" in plc_router)
check("  the result is used instead of the port label",
      "identities.get(h)" in plc_router
      and 'ident.get("product_name")' in plc_router)
check("  the scan carries the transports it found",
      "transports: list[str]" in plc_router
      and "suggested_protocol: str" in plc_router)
check("  the UI preselects the matching driver",
      "gateway_type: d.suggested_protocol || prev.gateway_type" in app)
check("  and shows what the device said",
      "d.identity_note" in app)

# --- 2. the gateway table -------------------------------------------------
print()
print("[2. the gateway table does not print every tag inline]")
check("no gateway row renders a tag stack",
      "tags-stack" not in app,
      "tags-stack still present" if "tags-stack" in app else "")
check("  the cell is a count that opens a viewer",
      "tags-cell-btn" in app and "setTagListModal(" in app)
check("  the viewer lays tags out in reflowing columns",
      ".tag-viewer-grid" in css and "auto-fill, minmax(" in css)
check("  a long tag name cannot widen a column",
      ".tag-viewer-item" in css and "text-overflow: ellipsis" in css)
check("  and the viewer can be filtered",
      "tagListFilter" in app)

# --- 3. modal stacking ----------------------------------------------------
print()
print("[3. modals sit above the header, with room top and bottom]")


def z_of(selector):
    """Last z-index declared for a selector — the cascade winner."""
    found = None
    for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css):
        zm = re.search(r"z-index:\s*(\d+)", m.group(1))
        if zm:
            found = int(zm.group(1))
    return found


z_modal = z_of(".modal-backdrop")
z_header = z_of(".app-header")
z_hot = z_of(".app-header-hotzone")
z_fab = z_of(".footer-toggle-fab")
z_guard = z_of(".license-guard-backdrop")
check("the modal backdrop outranks the app header",
      z_modal is not None and z_header is not None and z_modal > z_header,
      "modal={0} header={1}".format(z_modal, z_header))
check("  and the header hot-zone",
      z_modal is not None and z_hot is not None and z_modal > z_hot,
      "modal={0} hotzone={1}".format(z_modal, z_hot))
check("  and the floating Running pill",
      z_modal is not None and z_fab is not None and z_modal > z_fab,
      "modal={0} fab={1}".format(z_modal, z_fab))
check("  the licence guard still outranks a modal",
      z_guard is not None and z_modal is not None and z_guard > z_modal,
      "guard={0} modal={1}".format(z_guard, z_modal))
check("  the card is inset from the viewport edges",
      "max(16px, 3vh)" in css)
check("  and scrolls internally instead of overflowing",
      "min-height: 0;" in css and "max-height: calc(100dvh - max(32px, 6vh))" in css)

# --- 4. a new gateway starts empty ---------------------------------------
print()
print("[4. a new gateway does not inherit another gateway's tags]")
check("Add Gateway no longer seeds tags from the primary config",
      'tags_text: Array.isArray(config?.tags) ? config.tags.join(";") : ""' not in app)
check("  it starts with an empty tag list",
      re.search(r'auto_recover_enabled:\s*true,\s*(//[^\n]*\n\s*)*tags_text:\s*""', app)
      is not None)
check("  and no leftover mapper state",
      "ifm_datapoints: []," in app and "eip_signals: []," in app)
check("changing protocol clears the previous device's tags",
      "const changed = String(prev.gateway_type || []) !== String(nextType)".replace("[]", '""')
      in app or "const changed = String(prev.gateway_type" in app)
check("  including the discovered-tag panels",
      "setGatewayDiscoveredTags([]);" in app)

# --- 5/6. one tag list ----------------------------------------------------
print()
print("[5+6. one tag list per dialog, not three]")
check("the dialog knows when the mapper owns the tag list",
      "gatewayMapperOwnsTags" in app)
check("  it is derived from the scan, not the protocol alone",
      "ifm_datapoints || []).length > 0" in app)
check("  the generic Selected Tags grid steps aside",
      "if (gatewayMapperOwnsTags) return null;" in app)
check("  the Discovered Tags panel steps aside",
      "gatewayDiscoveredTags.length && !gatewayMapperOwnsTags" in app)
check("  and so do Search / Manual entry",
      app.count("gatewayMapperOwnsTags ? { display: \"none\" } : undefined") >= 2)

# --- 7. a running gateway explains itself --------------------------------
print()
print("[7. RUNNING with no data must say why]")
# 2026-08-27: this check USED to assert the literal string
# 'String(st?.last_error || "").trim() ?' - which passed while the page was
# broken, because `st` is only defined in the POWER-METER row. The PLC row uses
# `runtimeStatus`, so the gateway page died with "st is not defined". Assert the
# variable that is actually in scope, and let `npm run lint:undef` (no-undef)
# catch the general case.
check("the gateway row shows the error it already had",
      "gw-row-error" in app
      and 'String(runtimeStatus?.last_error || "").trim() ?' in app)
check("  using the status object that row actually has",
      "st?.last_error" not in app.split("gatewayConfigsView.map")[-1][:4000],
      "a row-local name from another block leaked in again")
check("  clamped so one bad gateway cannot stretch the table",
      "-webkit-line-clamp: 2" in css)
check("the driver counts refusals separately from timeouts",
      "last_read_busy" in ifm)
check("  including refusals inside a BATCHED reply",
      "A per-address busy code inside a BATCHED reply" in ifm)
check("the gateway offers an actionable next step",
      "_ifm_transport_advice" in plc_mgr)
check("  naming the transport the block prefers",
      "EtherNet/IP on 44818" in plc_mgr)
check("  and it probes only once, after a sustained bad run",
      "_ifm_advice_cache" in plc_mgr and "streak < 5" in plc_mgr)
check("a tag the operator ticked is collected even if offered unticked",
      "_wanted_names" in plc_mgr and "dict(pt, enabled=True)" in plc_mgr)

# --- the concurrency backoff was measured worse and must stay out --------
print()
print("[the 1-worker backoff was measured WORSE and must not come back]")
check("the fallback worker count is not adaptive",
      "cap = 1 if busy_seen" not in ifm)
check("  and the measurement is recorded next to it",
      "MEASURED WORSE" in ifm)

# --- a saved change must reach the RUNNING worker ------------------------
print()
print("[a config change must reach the running worker]")
# 2026-08-28: an ifm gateway was saved with eip_input_assembly=100 (verified in
# the store) while the running worker kept failing "no input assembly is set" -
# it still held the config it was STARTED with. The row said RUNNING and the
# tag list showed 16 tags, so everything looked configured.
# saveGatewayConfig is long (validation + payload build); the restart is at
# its END, so the slice has to cover the whole function.
# Slice to the REAL end of the function, not a character count. A fixed 7000
# broke on 2026-08-28 the moment the function grew past it - the restart code
# was still there, the window had just stopped covering it. A test that fails
# because the file grew is a test that will be silenced rather than believed.
def _function_body(text, decl):
    if decl not in text:
        return ""
    rest = text.split(decl, 1)[1]
    end = re.search(r"^  const ", rest, re.M)   # next declaration, same indent
    return rest[:end.start()] if end else rest


_save = _function_body(app, "const saveGatewayConfig")
check("saving restarts a gateway that is running",
      "stopGatewayInstance(next.id)" in _save
      and "startGatewayProfile(next, { force: true })" in _save)
# 2026-08-28: the first version of this restart called startGatewayProfile(next)
# plainly. Its opening guard is `if (isGatewayRunning(gateway)) return;` and the
# LOCAL running flag had not caught up with the backend stop, so the start was
# swallowed - the gateway stayed down, and an explicit stop suppresses
# auto-recover, so nothing brought it back. The operator saw a gateway that
# "stopped by itself".
check("  the start is FORCED past the already-running guard",
      "opts.force" in app and "!opts.force && isGatewayRunning(gateway)" in app)
check("  and local state is cleared before starting",
      "markGatewayRunningState([next.id], false)" in _save)
check("  it checks whether it WAS running first",
      "isGatewayRunning(" in _save)
check("  a stopped gateway is left stopped",
      "editingGatewayId" in _save and "wasRunning" in _save)
check("  and a failed restart is reported, not swallowed",
      "could not be restarted" in _save)

# --- the EDS carries assemblies, never bit meanings -----------------------
print()
print("[EDS import: assemblies from the file, tag layout from the device family]")
_eip = read(BACK, "drivers", "ethernet_ip.py")
check("a known device family generates its tags on import",
      "def signals_from_eds" in _eip)
check("  port count is derived from the device's own description",
      "def ifm_ports_from_eds" in _eip and "IFM_SIZE_TO_PORTS" in _eip)
check("  and an unknown device says the EDS cannot name bits",
      "an EDS never describes what an individual bit means" in _eip
      or "never describes what an individual bit" in _eip)
_mapper = read(SRC, "components", "Gateways", "EthernetIpMapper.jsx")
check("import does not discard signals the operator already mapped",
      "signals.length === 0" in _mapper)

# --- a table's grid must have as many columns as its header row ----------
print()
print("[a table's column count must match its header row]")


def grid_cols(selector):
    """Column count of the LAST grid-template-columns for a selector."""
    n = None
    for m in re.finditer(re.escape(selector) + r"[^{]*\{([^}]*)\}", css):
        g = re.search(r"grid-template-columns:\s*([^;]+);", m.group(1))
        if g:
            n = len(g.group(1).split())
    return n


# The combined gateway table: Name Device Protocol Address Database Interval
# Status Tags Actions = 9.
check("the combined gateway table has 9 columns",
      grid_cols(".gateway-table .thead") == 9, grid_cols(".gateway-table .thead"))
# The power page table drops Database = 8.
check("  the power gateway table has its own 8",
      grid_cols(".gateway-table.gateway-table-power .thead") == 8,
      grid_cols(".gateway-table.gateway-table-power .thead"))
check("  and the power table carries the modifier class",
      'className="table gateway-table gateway-table-power"' in app)

# --- the undefined-identifier gate must stay wired into the build ---------
print()
print("[the no-undef gate runs on every build]")
pkg = io.open(os.path.join(ROOT, "frontend", "package.json"),
              encoding="utf-8").read()
check("package.json defines lint:undef", '"lint:undef"' in pkg)
check("  and the build runs it before the smoke test",
      '"build": "vite build && npm run lint:undef && npm run smoke"' in pkg)
check("  the eslint config exists",
      os.path.exists(os.path.join(ROOT, "frontend", "eslint.undef.config.mjs")))

# --- 8. the Start button must send every field the protocol needs ---------
# 2026-08-28. `buildGatewayRuntimePayload` builds its `config` from a written-
# out list of keys. It listed only the PLC-era fields, so pressing Start sent an
# EtherNet/IP gateway with no assembly and no signals and the backend answered
# "no input assembly is set" - while Save, and the supervisor's own auto-recover
# path (which rebuilds the config from the SAVED document and does carry them),
# both worked. That split is exactly why it read as "sometimes it collects".
#
# This is the allowlist-strip trap the dashboard widgets hit before. A written-
# out list cannot be trusted to stay in step by hand, so compare it against the
# model: every field the backend accepts is either sent, or listed below with
# the reason it is not.
print()
print("[the Start payload carries every protocol field]")

model = read(BACK, "models.py")
body = model.split("class GatewayConfig(BaseModel):", 1)[1]
body = re.split(r"^class ", body, maxsplit=1, flags=re.M)[0]
model_fields = re.findall(r"^    ([a-z_][a-z0-9_]*)\s*:", body, re.M)

# The `config: {` object literal inside buildGatewayRuntimePayload, by braces.
after = app.split("const buildGatewayRuntimePayload", 1)[1]
start = after.index("config: {") + len("config: {")
depth, i = 1, start
while depth:
    ch = after[i]
    if ch in "{[(":
        depth += 1
    elif ch in "}])":
        depth -= 1
    i += 1
config_block = after[start:i - 1]
sent = set(re.findall(r"^        ([a-z_][a-z0-9_]*):", config_block, re.M))

# Fields the payload deliberately does not carry, and why.
NOT_SENT = {
    "schedule_enabled": "the supervisor reads the schedule from the saved document",
    "schedule_start": "same - a start payload is a single manual start",
    "schedule_stop": "same",
    "auto_recover_enabled": "the supervisor owns recovery, not the Start click",
}
missing = [f for f in model_fields if f not in sent and f not in NOT_SENT]
check("every GatewayConfig field is sent or explained",
      not missing,
      "not sent and not explained: " + ", ".join(missing) if missing
      else "{0} sent, {1} intentionally omitted".format(len(sent), len(NOT_SENT)))

# Name the two that caused the outage, so the reason survives a refactor.
check("  eip_input_assembly is sent (its absence WAS the bug)",
      "eip_input_assembly" in sent)
check("  eip_signals is sent",  "eip_signals" in sent)
check("  ifm_datapoints is sent",
      "ifm_datapoints" in sent,
      "without it the ifm driver silently falls back to scanning by tag name")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
